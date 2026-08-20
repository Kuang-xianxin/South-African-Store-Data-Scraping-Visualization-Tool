"""Durable server-side driver for the visible daily competitor batch."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from takealot_ops.competitors.batch import (
    CollectionBatchBusyError,
    CollectionBatchRegistry,
)


BEIJING = ZoneInfo("Asia/Shanghai")
SCHEDULED_OWNER_USERNAME = "scheduled-task"
SCHEDULED_OWNER_DISPLAY_NAME = "每日 09:00 自动任务"
SCHEDULED_CLIENT_ID = "scheduled-runner"
PENDING_RETRY_DELAY_SECONDS = 600.0
PENDING_RETRY_ROUND_LIMIT = 3


@dataclass(frozen=True)
class ScheduledCollectionTarget:
    """One deduplicated URL in the same order as a manual full batch."""

    plid: str
    url: str


@dataclass(frozen=True)
class ScheduledCollectionAttempt:
    """Normalized result returned by the ERP's shared one-link collector."""

    plid: str
    title: str | None
    message: str
    succeeded: bool
    failure_kind: str | None = None
    retryable: bool = False
    added_target_count: int = 0


TargetLoader = Callable[[], Awaitable[list[ScheduledCollectionTarget]]]
TargetCollector = Callable[
    [str, str, str, int, int, str | None, int | None],
    Awaitable[ScheduledCollectionAttempt],
]
Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


def register_scheduled_trigger(
    project_root: Path,
    *,
    now: datetime | None = None,
) -> tuple[str, bool]:
    """Durably register today's trigger even when the ERP process is offline."""
    observed_at = now or datetime.now(UTC)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    requested_for = observed_at.astimezone(BEIJING).date().isoformat()
    trigger_dir = project_root / "logs" / "competitor-scheduled-triggers"
    trigger_dir.mkdir(parents=True, exist_ok=True)
    trigger_path = trigger_dir / f"{requested_for}.json"
    payload = {
        "version": 1,
        "requested_for": requested_for,
        "requested_at": observed_at.astimezone(UTC).isoformat(),
    }
    try:
        with trigger_path.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except FileExistsError:
        return requested_for, False
    return requested_for, True


class ScheduledCompetitorBatchRunner:
    """Register once per Beijing day and run through the shared batch registry.

    Windows Task Scheduler only calls ``trigger``.  The long-running work stays
    inside the ERP process, so every browser reads the same progress and the
    existing server-side cancellation path can stop the current link.
    """

    def __init__(
        self,
        *,
        registry: CollectionBatchRegistry,
        journal_path: Path | None,
        trigger_dir: Path | None,
        load_targets: TargetLoader,
        collect_target: TargetCollector,
        logger: logging.Logger,
        clock: Clock | None = None,
        sleeper: Sleeper = asyncio.sleep,
        busy_poll_seconds: float = 5.0,
        inter_target_seconds: float = 1.0,
        network_pause_seconds: float = 600.0,
        pending_retry_delay_seconds: float = PENDING_RETRY_DELAY_SECONDS,
        pending_retry_round_limit: int = PENDING_RETRY_ROUND_LIMIT,
    ) -> None:
        self._registry = registry
        self._journal_path = journal_path
        self._trigger_dir = trigger_dir
        self._load_targets = load_targets
        self._collect_target = collect_target
        self._logger = logger
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper
        self._busy_poll_seconds = busy_poll_seconds
        self._inter_target_seconds = inter_target_seconds
        self._network_pause_seconds = network_pause_seconds
        self._pending_retry_delay_seconds = max(0.0, pending_retry_delay_seconds)
        self._pending_retry_round_limit = max(0, pending_retry_round_limit)
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._state = self._load_journal()

    def start(self) -> None:
        """Resume a durable pending or interrupted scheduled run on ERP startup."""
        self._import_trigger_files()
        self._restore_completed_pending_wait()
        if self._needs_driver():
            self._ensure_driver()

    async def close(self) -> None:
        """Stop the in-process driver while retaining its durable checkpoint."""
        self._closing = True
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def trigger(self, requested_for: str | None = None) -> dict[str, object]:
        """Idempotently register today's automatic start request."""
        today = self._beijing_date()
        trigger_date = requested_for or today
        try:
            datetime.strptime(trigger_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("定时触发日期必须使用 YYYY-MM-DD") from exc
        async with self._lock:
            handled = self._handled_trigger_dates()
            if trigger_date in handled:
                return self._trigger_payload("already_handled", accepted=False)
            if trigger_date == today and self._state.get("last_started_on") == today:
                handled.add(trigger_date)
                self._state["handled_trigger_dates"] = sorted(handled)
                self._persist_journal()
                return self._trigger_payload("already_started", accepted=False)
            if self._state.get("pending"):
                active_status = self._active_batch_status()
                if not active_status.get("active"):
                    return self._trigger_payload("already_pending", accepted=False)
                pending_for = self._optional_text(self._state.get("pending_for"))
                self._skip_pending_due_to_busy(active_status)
                if pending_for == trigger_date:
                    return self._trigger_payload("skipped_busy", accepted=False)
            self._state["pending"] = True
            self._state["pending_for"] = trigger_date
            self._state["requested_at"] = self._utc_iso()
            self._state["last_error"] = ""
            active_status = self._active_batch_status()
            if active_status.get("active"):
                self._skip_pending_due_to_busy(active_status)
                return self._trigger_payload("skipped_busy", accepted=False)
            self._persist_journal()
        self._logger.info("scheduled_trigger date=%s state=accepted", trigger_date)
        self._ensure_driver()
        return self._trigger_payload("accepted", accepted=True)

    async def mark_stopped(
        self,
        batch_id: str,
        *,
        stopped_by: str,
        reason: str = "",
    ) -> bool:
        """Persist that kxx stopped this scheduled batch; never restart it today."""
        async with self._lock:
            if (
                self._state.get("run_status")
                not in {"running", "paused", "retry_wait"}
                or self._state.get("batch_id") != batch_id
            ):
                return False
            self._state["run_status"] = "stopped"
            self._state["pending"] = False
            self._state["active_item"] = None
            self._state["resume_after"] = None
            self._state["network_failures"] = []
            self._state["run_revision"] = self._run_revision() + 1
            self._state["stopped_at"] = self._utc_iso()
            self._state["stopped_by"] = stopped_by
            self._state["stop_reason"] = self._single_line(reason)
            self._persist_journal()
        self._logger.info(
            "scheduled_batch batch=%s event=manual_stop user=%s",
            batch_id,
            stopped_by,
        )
        return True

    async def resume_stopped(
        self,
        batch_id: str,
        *,
        resumed_by: str,
    ) -> dict[str, object]:
        """Explicitly resume a stopped or exhausted scheduled batch checkpoint."""
        async with self._lock:
            previous_run_status = str(self._state.get("run_status") or "idle")
            if previous_run_status not in {"stopped", "completed_with_pending"} or (
                self._state.get("batch_id") != batch_id
            ):
                raise ValueError("当前没有与该编号匹配的可继续 09:00 自动批次断点")

            shared_status = self._registry.status()
            if shared_status.get("active"):
                raise CollectionBatchBusyError("当前已有竞品批次运行，不能同时恢复自动批次")

            metrics = self._metrics()
            resume_queue = self._resume_queue(
                deferred_failures=True,
                retry_round=1,
            )
            if metrics["pending"] <= 0 or not resume_queue:
                raise ValueError("该 09:00 自动批次已经没有待重试或未完成链接")

            previous_state = dict(self._state)
            self._state.update(
                {
                    "run_status": "running",
                    "pending": False,
                    "queue": resume_queue,
                    "active_item": None,
                    "resume_after": None,
                    "network_failures": [],
                    "pending_retry_round": (
                        0
                        if previous_run_status == "completed_with_pending"
                        else self._pending_retry_round()
                    ),
                    "run_revision": self._run_revision() + 1,
                    "last_resumed_at": self._utc_iso(),
                    "last_resumed_by": resumed_by,
                    "explicit_resume_count": (
                        self._optional_int(self._state.get("explicit_resume_count")) or 0
                    )
                    + 1,
                }
            )
            try:
                self._persist_journal()
                status = self._registry.event(
                    batch_id=batch_id,
                    client_id=SCHEDULED_CLIENT_ID,
                    event="resume",
                    username=SCHEDULED_OWNER_USERNAME,
                    display_name=SCHEDULED_OWNER_DISPLAY_NAME,
                    completed=metrics["completed"],
                    total=metrics["total"],
                    pending=metrics["pending"],
                    succeeded=metrics["succeeded"],
                    failed=metrics["failed"],
                    terminal=metrics["terminal"],
                    reason=(
                        f"{resumed_by} 已从服务端断点继续今日 09:00 自动批次，"
                        f"保留原批次顺序并续爬 {metrics['pending']} 条"
                    ),
                    with_stock_probe=True,
                    visible_browser=False,
                    source="scheduled",
                    results=self._result_rows(),
                    errors=self._error_rows(),
                )
            except Exception:
                self._state = previous_state
                self._persist_journal()
                raise

        self._logger.info(
            "scheduled_batch batch=%s event=manual_resume user=%s pending=%s",
            batch_id,
            resumed_by,
            metrics["pending"],
        )
        self._ensure_driver()
        return status

    def status(self) -> dict[str, object]:
        """Return a small copy suitable for the loopback trigger response."""
        run_status = str(self._state.get("run_status") or "idle")
        resumable_pending = (
            self._metrics()["pending"]
            if run_status in {"stopped", "completed_with_pending"}
            else 0
        )
        wait_kind = (
            "network"
            if run_status == "paused"
            else "pending_retry"
            if run_status == "retry_wait"
            else None
        )
        return {
            "pending": bool(self._state.get("pending")),
            "pending_for": self._state.get("pending_for"),
            "run_status": run_status,
            "batch_id": self._state.get("batch_id"),
            "last_started_on": self._state.get("last_started_on"),
            "last_started_at": self._state.get("last_started_at"),
            "last_completed_at": self._state.get("last_completed_at"),
            "last_error": self._state.get("last_error", ""),
            "last_skipped_on": self._state.get("last_skipped_on"),
            "last_skipped_at": self._state.get("last_skipped_at"),
            "last_skip_reason": self._state.get("last_skip_reason", ""),
            "resume_available": resumable_pending > 0,
            "resumable_pending": resumable_pending,
            "wait_kind": wait_kind,
            "resume_after": self._state.get("resume_after"),
            "pending_retry_round": self._pending_retry_round(),
            "pending_retry_round_limit": self._pending_retry_round_limit,
        }

    def stopped_checkpoint_status(
        self,
        *,
        include_details: bool = True,
        result_offset: int = 0,
        error_offset: int = 0,
        detail_limit: int | None = None,
    ) -> dict[str, object] | None:
        """Project any durable, explicitly resumable scheduled checkpoint."""
        run_status = str(self._state.get("run_status") or "idle")
        batch_id = self._optional_text(self._state.get("batch_id"))
        if run_status not in {"stopped", "completed_with_pending"} or batch_id is None:
            return None
        metrics = self._metrics()
        if metrics["pending"] <= 0:
            return None
        stopped = run_status == "stopped"
        stopped_by = self._optional_text(self._state.get("stopped_by")) or "kxx"
        if stopped:
            reason = self._optional_text(self._state.get("stop_reason")) or (
                "已手动中断今日 09:00 自动批次；服务端断点已保留，可由 kxx "
                "显式继续；今天不会自动再次启动，明天 09:00 照常。"
            )
        else:
            reason = self._optional_text(self._state.get("completion_reason")) or (
                f"延时自动重试后仍有 {metrics['pending']} 条未解决；"
                "可由 kxx 继续同一服务端断点"
            )
        result_count = self._row_count("results")
        error_count = self._row_count("errors")
        return {
            "active": False,
            "batch_id": batch_id,
            "owner_username": SCHEDULED_OWNER_USERNAME,
            "owner_display_name": SCHEDULED_OWNER_DISPLAY_NAME,
            "source": "scheduled",
            "event": "manual_stop" if stopped else "completed",
            "completed": metrics["completed"],
            "total": metrics["total"],
            "pending": metrics["pending"],
            "succeeded": metrics["succeeded"],
            "failed": metrics["failed"],
            "terminal": metrics["terminal"],
            "current_index": None,
            "current_plid": None,
            "current_request_id": None,
            "current_stage": None,
            "current_retry_kind": None,
            "current_retry_attempt": None,
            "with_stock_probe": True,
            "visible_browser": False,
            "takeover_pending": False,
            "reason": reason,
            "started_at": self._state.get("last_started_at"),
            "updated_at": (
                self._state.get("stopped_at")
                if stopped
                else self._state.get("last_completed_at")
            ),
            "queued_targets": [],
            "priority_targets": [],
            "prioritized_targets": [],
            "result_count": result_count,
            "error_count": error_count,
            "results": (
                self._result_rows(offset=result_offset, limit=detail_limit)
                if include_details
                else []
            ),
            "errors": (
                self._error_rows(offset=error_offset, limit=detail_limit)
                if include_details
                else []
            ),
            "stopped_by": stopped_by if stopped else None,
        }

    async def _drive(self) -> None:
        try:
            while not self._closing:
                self._import_trigger_files()
                if self._state.get("pending"):
                    active_status = self._active_batch_status()
                    if active_status.get("active"):
                        self._skip_pending_due_to_busy(active_status)
                run_status = str(self._state.get("run_status") or "idle")
                if run_status == "running":
                    await self._resume_current_run()
                    continue
                if run_status == "paused":
                    await self._wait_for_network_resume()
                    continue
                if run_status == "retry_wait":
                    await self._wait_for_pending_retry()
                    continue
                if not self._state.get("pending"):
                    return
                active_status = self._active_batch_status()
                if active_status.get("active"):
                    self._skip_pending_due_to_busy(active_status)
                    continue
                try:
                    targets = await self._load_targets()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._state["last_error"] = self._single_line(str(exc))
                    self._persist_journal()
                    self._logger.exception("scheduled_target_load_failed")
                    await self._sleep(self._busy_poll_seconds)
                    continue
                if not targets:
                    self._state["last_error"] = "当前没有可采集的真正竞品或自有店铺链接"
                    self._persist_journal()
                    await self._sleep(self._busy_poll_seconds)
                    continue
                try:
                    started = await self._begin_run(targets)
                except CollectionBatchBusyError:
                    self._skip_pending_due_to_busy(self._registry.status())
                    continue
                if not started:
                    await self._sleep(self._busy_poll_seconds)
                    continue
                await self._run_current_queue()
        finally:
            if self._task is asyncio.current_task():
                self._task = None

    def _active_batch_status(self) -> dict[str, object]:
        """Include a durable scheduled run before it reacquires the registry."""
        active_status = self._registry.status()
        if active_status.get("active"):
            return active_status
        run_status = str(self._state.get("run_status") or "idle")
        batch_id = self._optional_text(self._state.get("batch_id"))
        if run_status not in {"running", "paused", "retry_wait"} or batch_id is None:
            return active_status
        return {
            **active_status,
            "active": True,
            "batch_id": batch_id,
            "source": "scheduled",
            "owner_username": SCHEDULED_OWNER_USERNAME,
            "owner_display_name": SCHEDULED_OWNER_DISPLAY_NAME,
        }

    def _skip_pending_due_to_busy(
        self,
        active_status: dict[str, object],
    ) -> None:
        """Consume one scheduled trigger when another shared batch owns the slot."""
        trigger_date = self._optional_text(self._state.get("pending_for")) or (
            self._beijing_date()
        )
        active_source = self._optional_text(active_status.get("source")) or "manual"
        active_batch_id = self._optional_text(active_status.get("batch_id"))
        active_owner = self._optional_text(
            active_status.get("owner_display_name")
        ) or self._optional_text(active_status.get("owner_username"))
        active_label = (
            "另一场09:00自动批次"
            if active_source == "scheduled"
            else "手动批次"
        )
        details = ""
        if active_batch_id:
            details = f"（批次 {active_batch_id}"
            if active_owner:
                details += f"，{active_owner}"
            details += "）"
        reason = (
            f"{trigger_date} 09:00 自动批次未启动：触发或取得采集权时已有"
            f"{active_label}运行{details}；本次按冲突跳过，当天不排队、"
            "不在现有批次结束后补跑，次日09:00再按计划尝试。"
        )[:500]
        handled = self._handled_trigger_dates()
        handled.add(trigger_date)
        self._state.update(
            {
                "pending": False,
                "pending_for": None,
                "last_error": "",
                "last_skipped_on": trigger_date,
                "last_skipped_at": self._utc_iso(),
                "last_skip_reason": reason,
                "handled_trigger_dates": sorted(handled),
            }
        )
        self._persist_journal()
        self._logger.info(
            "scheduled_trigger date=%s state=skipped_busy active_batch=%s "
            "active_source=%s active_owner=%s",
            trigger_date,
            active_batch_id or "-",
            active_source,
            active_owner or "-",
        )

    async def _begin_run(self, targets: list[ScheduledCollectionTarget]) -> bool:
        deduplicated: list[ScheduledCollectionTarget] = []
        seen: set[str] = set()
        for target in targets:
            if not target.plid or target.plid in seen:
                continue
            seen.add(target.plid)
            deduplicated.append(target)
        if not deduplicated:
            return False

        today = self._beijing_date()
        pending_for = str(self._state.get("pending_for") or today)
        batch_id = f"scheduled-{pending_for.replace('-', '')}-{uuid4().hex[:12]}"
        queue = [
            {"index": index, "url": target.url}
            for index, target in enumerate(deduplicated)
        ]
        self._registry.event(
            batch_id=batch_id,
            client_id=SCHEDULED_CLIENT_ID,
            event="start",
            username=SCHEDULED_OWNER_USERNAME,
            display_name=SCHEDULED_OWNER_DISPLAY_NAME,
            completed=0,
            total=len(queue),
            pending=len(queue),
            succeeded=0,
            failed=0,
            terminal=0,
            reason="Windows 计划任务已在 09:00 自动开始同一共享采集批次",
            with_stock_probe=True,
            visible_browser=False,
            source="scheduled",
        )

        async with self._lock:
            handled = self._handled_trigger_dates()
            handled.add(pending_for)
            self._state.update(
                {
                    "pending": False,
                    "pending_for": None,
                    "run_status": "running",
                    "batch_id": batch_id,
                    # This is the trigger date, not merely the wall-clock date on
                    # which an offline/backlogged run eventually acquires the slot.
                    # Keeping those dates separate prevents today's request from
                    # being swallowed when yesterday's durable trigger starts first.
                    "last_started_on": pending_for,
                    "last_started_at": self._utc_iso(),
                    "last_completed_at": None,
                    "last_error": "",
                    "handled_trigger_dates": sorted(handled),
                    "targets": [
                        {"index": index, **asdict(target)}
                        for index, target in enumerate(deduplicated)
                    ],
                    "queue": queue,
                    "attempted_indexes": [],
                    "failed_indexes": [],
                    "terminal_indexes": [],
                    "stock_unprobed_indexes": [],
                    "results": [],
                    "errors": [],
                    "accepted_priority_keys": [],
                    "last_target_refresh_completed": 0,
                    "resume_after": None,
                    "network_failures": [],
                    "pending_retry_round": 0,
                    "completion_reason": "",
                    "active_item": None,
                    "run_revision": self._run_revision() + 1,
                }
            )
            self._persist_journal()
        self._logger.info(
            "scheduled_batch batch=%s event=start total=%s date=%s",
            batch_id,
            len(queue),
            today,
        )
        return True

    async def _resume_current_run(self) -> None:
        batch_id = str(self._state.get("batch_id") or "")
        if not batch_id:
            self._state["run_status"] = "failed"
            self._state["last_error"] = "定时批次断点缺少批次编号"
            self._persist_journal()
            return
        status = self._registry.status()
        if status.get("active") and status.get("batch_id") != batch_id:
            await self._sleep(self._busy_poll_seconds)
            return
        if not status.get("active"):
            metrics = self._metrics()
            try:
                self._registry.event(
                    batch_id=batch_id,
                    client_id=SCHEDULED_CLIENT_ID,
                    event="resume",
                    username=SCHEDULED_OWNER_USERNAME,
                    display_name=SCHEDULED_OWNER_DISPLAY_NAME,
                    completed=metrics["completed"],
                    total=metrics["total"],
                    pending=metrics["pending"],
                    succeeded=metrics["succeeded"],
                    failed=metrics["failed"],
                    terminal=metrics["terminal"],
                    reason="ERP 重启后自动恢复 09:00 定时批次断点",
                    with_stock_probe=True,
                    visible_browser=False,
                    source="scheduled",
                    results=self._result_rows(),
                    errors=self._error_rows(),
                )
            except CollectionBatchBusyError:
                await self._sleep(self._busy_poll_seconds)
                return
        await self._run_current_queue()

    async def _run_current_queue(self) -> None:
        batch_id = str(self._state.get("batch_id") or "")
        while not self._closing and self._state.get("run_status") == "running":
            self._merge_server_queue()
            queue = self._queue()
            if not queue:
                await self._refresh_latest_targets(batch_id)
                if self._state.get("run_status") != "running":
                    return
                self._merge_server_queue()
                queue = self._queue()
            if not queue:
                if self._state.get("run_status") != "running":
                    return
                if await self._pause_for_pending_retry(batch_id):
                    return
                await self._complete_run(batch_id)
                return

            run_revision = self._run_revision()
            item = queue.pop(0)
            self._state["queue"] = queue
            self._state["active_item"] = item
            self._persist_journal()
            index = self._required_int(item.get("index"))
            url = str(item["url"])
            retry_kind = self._optional_text(item.get("retry_kind"))
            retry_attempt = self._optional_int(item.get("retry_attempt"))
            priority = bool(item.get("priority"))
            request_id = f"scheduled-request-{uuid4().hex}"
            try:
                attempt = await self._collect_target(
                    url,
                    batch_id,
                    request_id,
                    index,
                    len(self._targets()),
                    retry_kind,
                    retry_attempt,
                )
            except asyncio.CancelledError:
                if (
                    self._state.get("run_status") != "running"
                    or self._run_revision() != run_revision
                ):
                    return
                self._prepend_active_item(item)
                raise
            except Exception as exc:
                attempt = ScheduledCollectionAttempt(
                    plid=self._plid_from_url(url),
                    title=None,
                    message=self._single_line(str(exc)) or type(exc).__name__,
                    succeeded=False,
                    failure_kind="network",
                    retryable=True,
                )

            if (
                self._state.get("run_status") != "running"
                or self._run_revision() != run_revision
            ):
                return
            self._state["active_item"] = None
            self._apply_attempt(index, url, item, attempt, priority=priority)
            self._persist_journal()
            self._registry.record_outcome(
                batch_id=batch_id,
                plid=attempt.plid or self._plid_from_url(url),
                url=url,
                title=attempt.title,
                message=attempt.message,
                succeeded=attempt.succeeded,
            )
            metrics = self._metrics()
            last_refresh_completed = self._optional_int(
                self._state.get("last_target_refresh_completed")
            ) or 0
            if metrics["completed"] - last_refresh_completed >= 25:
                await self._refresh_latest_targets(batch_id)
                if self._state.get("run_status") != "running":
                    return
                self._merge_server_queue()
                metrics = self._metrics()
            try:
                self._registry.event(
                    batch_id=batch_id,
                    client_id=SCHEDULED_CLIENT_ID,
                    event="progress",
                    username=SCHEDULED_OWNER_USERNAME,
                    display_name=SCHEDULED_OWNER_DISPLAY_NAME,
                    completed=metrics["completed"],
                    total=metrics["total"],
                    pending=metrics["pending"],
                    succeeded=metrics["succeeded"],
                    failed=metrics["failed"],
                    terminal=metrics["terminal"],
                    reason=attempt.message,
                    with_stock_probe=True,
                    visible_browser=False,
                    source="scheduled",
                    results=self._result_rows(),
                    errors=self._error_rows(),
                )
            except CollectionBatchBusyError:
                if self._state.get("run_status") == "stopped":
                    return
                self._prepend_active_item(item)
                raise
            if self._update_network_failure_streak(attempt):
                await self._pause_after_network_failures(batch_id)
                return
            if self._queue():
                await self._sleep(self._inter_target_seconds)

    async def _pause_after_network_failures(self, batch_id: str) -> None:
        failures = self._network_failure_rows()
        resume_after = self._clock_utc() + timedelta(
            seconds=self._network_pause_seconds
        )
        reason = self._network_pause_reason(failures, self._network_pause_seconds)
        self._state["run_status"] = "paused"
        self._state["resume_after"] = resume_after.isoformat()
        self._persist_journal()
        metrics = self._metrics()
        self._registry.event(
            batch_id=batch_id,
            client_id=SCHEDULED_CLIENT_ID,
            event="scheduled_pause",
            username=SCHEDULED_OWNER_USERNAME,
            display_name=SCHEDULED_OWNER_DISPLAY_NAME,
            completed=metrics["completed"],
            total=metrics["total"],
            pending=metrics["pending"],
            succeeded=metrics["succeeded"],
            failed=metrics["failed"],
            terminal=metrics["terminal"],
            reason=reason,
            with_stock_probe=True,
            visible_browser=False,
            source="scheduled",
            results=self._result_rows(),
            errors=self._error_rows(),
        )
        self._logger.warning(
            "scheduled_batch batch=%s event=network_pause resume_after=%s failures=%s",
            batch_id,
            resume_after.isoformat(),
            json.dumps(failures, ensure_ascii=False),
        )

    async def _pause_for_pending_retry(self, batch_id: str) -> bool:
        """Wait before a bounded retry wave instead of stranding tail failures."""
        metrics = self._metrics()
        current_round = self._pending_retry_round()
        if (
            metrics["pending"] <= 0
            or current_round >= self._pending_retry_round_limit
        ):
            return False
        retry_round = current_round + 1
        retry_queue = self._resume_queue(
            deferred_failures=True,
            retry_round=retry_round,
        )
        if not retry_queue:
            return False
        resume_after = self._clock_utc() + timedelta(
            seconds=self._pending_retry_delay_seconds
        )
        reason = self._pending_retry_wait_reason(
            metrics["pending"],
            retry_round,
            self._pending_retry_delay_seconds,
        )
        self._state.update(
            {
                "run_status": "retry_wait",
                "queue": retry_queue,
                "resume_after": resume_after.isoformat(),
                "pending_retry_round": retry_round,
                "network_failures": [],
            }
        )
        self._persist_journal()
        self._registry.event(
            batch_id=batch_id,
            client_id=SCHEDULED_CLIENT_ID,
            event="scheduled_pause",
            username=SCHEDULED_OWNER_USERNAME,
            display_name=SCHEDULED_OWNER_DISPLAY_NAME,
            completed=metrics["completed"],
            total=metrics["total"],
            pending=metrics["pending"],
            succeeded=metrics["succeeded"],
            failed=metrics["failed"],
            terminal=metrics["terminal"],
            reason=reason,
            with_stock_probe=True,
            visible_browser=False,
            source="scheduled",
            results=self._result_rows(),
            errors=self._error_rows(),
        )
        self._logger.info(
            "scheduled_batch batch=%s event=pending_retry_wait round=%s/%s "
            "pending=%s resume_after=%s",
            batch_id,
            retry_round,
            self._pending_retry_round_limit,
            metrics["pending"],
            resume_after.isoformat(),
        )
        return True

    async def _wait_for_network_resume(self) -> None:
        batch_id = str(self._state.get("batch_id") or "")
        if not batch_id:
            self._state["run_status"] = "failed"
            self._state["last_error"] = "定时批次暂停断点缺少批次编号"
            self._persist_journal()
            return
        while not self._closing and self._state.get("run_status") == "paused":
            remaining = self._pause_remaining()
            event = "scheduled_pause" if remaining > 0 else "auto_resume"
            reason = (
                self._network_pause_reason(
                    self._network_failure_rows(),
                    remaining,
                )
                if remaining > 0
                else "连续网络失败暂停已结束，继续 09:00 自动批次"
            )
            metrics = self._metrics()
            try:
                self._registry.event(
                    batch_id=batch_id,
                    client_id=SCHEDULED_CLIENT_ID,
                    event=event,
                    username=SCHEDULED_OWNER_USERNAME,
                    display_name=SCHEDULED_OWNER_DISPLAY_NAME,
                    completed=metrics["completed"],
                    total=metrics["total"],
                    pending=metrics["pending"],
                    succeeded=metrics["succeeded"],
                    failed=metrics["failed"],
                    terminal=metrics["terminal"],
                    reason=reason,
                    with_stock_probe=True,
                    visible_browser=False,
                    source="scheduled",
                    results=self._result_rows(),
                    errors=self._error_rows(),
                )
            except CollectionBatchBusyError:
                await self._sleep(self._busy_poll_seconds)
                continue
            if remaining <= 0:
                self._state["run_status"] = "running"
                self._state["resume_after"] = None
                self._state["network_failures"] = []
                self._persist_journal()
                self._logger.info(
                    "scheduled_batch batch=%s event=network_auto_resume",
                    batch_id,
                )
                return
            await self._sleep(min(self._busy_poll_seconds, remaining))

    async def _wait_for_pending_retry(self) -> None:
        batch_id = str(self._state.get("batch_id") or "")
        if not batch_id:
            self._state["run_status"] = "failed"
            self._state["last_error"] = "延时重试断点缺少批次编号"
            self._persist_journal()
            return
        while not self._closing and self._state.get("run_status") == "retry_wait":
            remaining = self._pause_remaining()
            retry_round = self._pending_retry_round()
            metrics = self._metrics()
            event = "scheduled_pause" if remaining > 0 else "auto_resume"
            reason = self._pending_retry_wait_reason(
                metrics["pending"],
                retry_round,
                remaining,
            )
            try:
                self._registry.event(
                    batch_id=batch_id,
                    client_id=SCHEDULED_CLIENT_ID,
                    event=event,
                    username=SCHEDULED_OWNER_USERNAME,
                    display_name=SCHEDULED_OWNER_DISPLAY_NAME,
                    completed=metrics["completed"],
                    total=metrics["total"],
                    pending=metrics["pending"],
                    succeeded=metrics["succeeded"],
                    failed=metrics["failed"],
                    terminal=metrics["terminal"],
                    reason=reason,
                    with_stock_probe=True,
                    visible_browser=False,
                    source="scheduled",
                    results=self._result_rows(),
                    errors=self._error_rows(),
                )
            except CollectionBatchBusyError:
                await self._sleep(self._busy_poll_seconds)
                continue
            if remaining <= 0:
                self._state["run_status"] = "running"
                self._state["resume_after"] = None
                self._persist_journal()
                self._logger.info(
                    "scheduled_batch batch=%s event=pending_retry_auto_resume "
                    "round=%s/%s pending=%s",
                    batch_id,
                    retry_round,
                    self._pending_retry_round_limit,
                    metrics["pending"],
                )
                return
            await self._sleep(min(self._busy_poll_seconds, remaining))

    async def _refresh_latest_targets(self, batch_id: str) -> None:
        """Append links exposed by a Seller Offers refresh while this batch runs."""
        try:
            latest_targets = await self._load_targets()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception(
                "scheduled_target_refresh_failed batch=%s",
                self._state.get("batch_id") or "-",
            )
            return
        if self._state.get("run_status") != "running":
            return
        status = self._registry.status()
        if not status.get("active") or status.get("batch_id") != batch_id:
            return
        known_plids = {
            self._plid_from_url(str(item.get("url") or ""))
            for item in self._targets()
        }
        added = 0
        for target in latest_targets:
            if not target.plid or target.plid in known_plids:
                continue
            if self._registry.enqueue_target(
                plid=target.plid,
                url=target.url,
                batch_id=batch_id,
            ):
                known_plids.add(target.plid)
                added += 1
        self._state["last_target_refresh_completed"] = self._metrics()["completed"]
        self._persist_journal()
        if added:
            self._logger.info(
                "scheduled_targets_appended batch=%s added=%s",
                self._state.get("batch_id") or "-",
                added,
            )

    async def _complete_run(self, batch_id: str) -> None:
        if self._state.get("run_status") != "running":
            return
        metrics = self._metrics()
        retry_round = self._pending_retry_round()
        reason = (
            f"09:00 自动批次已完成 {retry_round} 轮延时自动重试，仍有 "
            f"{metrics['pending']} 个待重试或未完成链接；可继续同一服务端断点"
            if metrics["pending"]
            else "09:00 自动批次全部链接已检查"
        )
        self._registry.event(
            batch_id=batch_id,
            client_id=SCHEDULED_CLIENT_ID,
            event="completed",
            username=SCHEDULED_OWNER_USERNAME,
            display_name=SCHEDULED_OWNER_DISPLAY_NAME,
            completed=metrics["completed"],
            total=metrics["total"],
            pending=metrics["pending"],
            succeeded=metrics["succeeded"],
            failed=metrics["failed"],
            terminal=metrics["terminal"],
            reason=reason,
            with_stock_probe=True,
            visible_browser=False,
            source="scheduled",
            results=self._result_rows(),
            errors=self._error_rows(),
        )
        self._state["run_status"] = (
            "completed_with_pending" if metrics["pending"] else "completed"
        )
        self._state["last_completed_at"] = self._utc_iso()
        self._state["active_item"] = None
        self._state["resume_after"] = None
        self._state["network_failures"] = []
        self._state["completion_reason"] = reason
        self._persist_journal()
        self._logger.info(
            "scheduled_batch batch=%s event=completed completed=%s total=%s "
            "pending=%s succeeded=%s failed=%s terminal=%s",
            batch_id,
            metrics["completed"],
            metrics["total"],
            metrics["pending"],
            metrics["succeeded"],
            metrics["failed"],
            metrics["terminal"],
        )

    def _merge_server_queue(self) -> None:
        status = self._registry.status()
        if status.get("batch_id") != self._state.get("batch_id"):
            return
        targets = self._targets()
        queue = self._queue()
        known_indexes = {self._required_int(item.get("index")) for item in targets}
        plid_to_index = {
            self._plid_from_url(str(item["url"])): self._required_int(item.get("index"))
            for item in targets
        }
        next_index = max(known_indexes, default=-1) + 1
        raw_queued_targets = status.get("queued_targets")
        for raw in raw_queued_targets if isinstance(raw_queued_targets, list) else []:
            if not isinstance(raw, dict):
                continue
            plid = self._optional_text(raw.get("plid"))
            url = self._optional_text(raw.get("url"))
            if not plid or not url or plid in plid_to_index:
                continue
            target = {"index": next_index, "plid": plid, "url": url}
            targets.append(target)
            queue.append({"index": next_index, "url": url})
            plid_to_index[plid] = next_index
            next_index += 1

        raw_accepted = self._state.get("accepted_priority_keys")
        accepted = {
            str(value) for value in raw_accepted
        } if isinstance(raw_accepted, list) else set()
        priority_items: list[dict[str, object]] = []
        raw_priority_targets = status.get("priority_targets")
        for raw in raw_priority_targets if isinstance(raw_priority_targets, list) else []:
            if not isinstance(raw, dict):
                continue
            plid = self._optional_text(raw.get("plid"))
            url = self._optional_text(raw.get("url"))
            requested_at = self._optional_text(raw.get("requested_at"))
            if not plid or not url or not requested_at:
                continue
            key = f"{plid}|{requested_at}"
            if key in accepted:
                continue
            index = plid_to_index.get(plid)
            if index is None:
                index = next_index
                next_index += 1
                targets.append({"index": index, "plid": plid, "url": url})
                queue.append({"index": index, "url": url})
                plid_to_index[plid] = index
            priority_items.append({"index": index, "url": url, "priority": True})
            accepted.add(key)
        if priority_items:
            queue = [*priority_items, *queue]
        self._state["targets"] = targets
        self._state["queue"] = queue
        self._state["accepted_priority_keys"] = sorted(accepted)
        self._persist_journal()

    def _apply_attempt(
        self,
        index: int,
        url: str,
        item: dict[str, object],
        attempt: ScheduledCollectionAttempt,
        *,
        priority: bool,
    ) -> None:
        attempted = self._index_set("attempted_indexes")
        failed = self._index_set("failed_indexes")
        terminal = self._index_set("terminal_indexes")
        stock_unprobed = self._index_set("stock_unprobed_indexes")
        results = self._result_rows()
        errors = self._error_rows()
        plid = attempt.plid or self._plid_from_url(url)

        if attempt.succeeded:
            results = [row for row in results if row.get("plid") != plid]
            results.append(
                {
                    "plid": plid,
                    "url": url,
                    "title": attempt.title or "",
                    "message": attempt.message,
                    "added_target_count": attempt.added_target_count,
                }
            )
            errors = [row for row in errors if row.get("plid") != plid]
            failed.discard(index)
            terminal.discard(index)
            stock_unprobed.discard(index)
        else:
            errors = [row for row in errors if row.get("plid") != plid]
            errors.append({"plid": plid, "url": url, "message": attempt.message})
            if not priority:
                if attempt.failure_kind == "confirmed-invalid":
                    failed.discard(index)
                    terminal.add(index)
                    stock_unprobed.discard(index)
                else:
                    failed.add(index)
                    terminal.discard(index)
                    if attempt.failure_kind == "stock-unprobed":
                        stock_unprobed.add(index)
                    else:
                        stock_unprobed.discard(index)
                    self._schedule_retry(index, url, item, attempt)
        if not priority:
            attempted.add(index)
        self._state["attempted_indexes"] = sorted(attempted)
        self._state["failed_indexes"] = sorted(failed)
        self._state["terminal_indexes"] = sorted(terminal)
        self._state["stock_unprobed_indexes"] = sorted(stock_unprobed)
        self._state["results"] = results
        self._state["errors"] = errors

    def _schedule_retry(
        self,
        index: int,
        url: str,
        item: dict[str, object],
        attempt: ScheduledCollectionAttempt,
    ) -> None:
        if bool(item.get("deferred_retry")):
            return
        if attempt.failure_kind == "suspected-invalid":
            # The invalid-link evidence counter cannot advance again until the
            # bounded delayed wave, so an inline retry only repeats the same read.
            return
        queue = self._queue()
        retry_kind: str | None = None
        limit = 0
        if attempt.failure_kind == "stock-unprobed":
            retry_kind = "stock"
            limit = 2
        elif attempt.retryable or attempt.failure_kind in {
            "network",
            "validation-uncertain",
        }:
            retry_kind = "automatic"
            limit = 3
        if retry_kind is None:
            return
        previous_attempt = (
            self._optional_int(item.get("retry_attempt"))
            if item.get("retry_kind") == retry_kind
            else 0
        ) or 0
        next_attempt = previous_attempt + 1
        if next_attempt > limit:
            return
        gap = 2 ** (next_attempt - 1)
        if len(queue) < gap:
            return
        queue.insert(
            gap,
            {
                "index": index,
                "url": url,
                "retry_kind": retry_kind,
                "retry_attempt": next_attempt,
            },
        )
        self._state["queue"] = queue

    def _metrics(self) -> dict[str, int]:
        targets = self._targets()
        attempted = self._index_set("attempted_indexes")
        failed = self._index_set("failed_indexes")
        terminal = self._index_set("terminal_indexes")
        successful_plids = {
            str(row.get("plid") or "") for row in self._result_rows()
        }
        pending = 0
        for target in targets:
            index = self._required_int(target.get("index"))
            plid = self._plid_from_url(str(target["url"]))
            if index in terminal:
                continue
            if index in failed or index not in attempted or plid not in successful_plids:
                pending += 1
        return {
            "completed": len(attempted),
            "total": len(targets),
            "pending": pending,
            "succeeded": len(self._result_rows()),
            "failed": len(failed),
            "terminal": len(terminal),
        }

    def _resume_queue(
        self,
        *,
        deferred_failures: bool = False,
        retry_round: int = 1,
    ) -> list[dict[str, object]]:
        """Rebuild failed and unattempted work in the frozen target order."""
        attempted = self._index_set("attempted_indexes")
        failed = self._index_set("failed_indexes")
        terminal = self._index_set("terminal_indexes")
        successful_plids = {
            str(row.get("plid") or "") for row in self._result_rows()
        }
        queue: list[dict[str, object]] = []
        for target in sorted(
            self._targets(),
            key=lambda item: self._required_int(item.get("index")),
        ):
            index = self._required_int(target.get("index"))
            url = str(target["url"])
            plid = self._plid_from_url(url)
            if index in terminal:
                continue
            if index in failed or index not in attempted or plid not in successful_plids:
                item: dict[str, object] = {"index": index, "url": url}
                if deferred_failures and index in failed:
                    item.update(
                        {
                            "retry_kind": "automatic",
                            "retry_attempt": max(1, min(retry_round, 10)),
                            "deferred_retry": True,
                        }
                    )
                queue.append(item)
        return queue

    def _prepend_active_item(self, item: dict[str, object]) -> None:
        self._state["active_item"] = None
        self._state["queue"] = [item, *self._queue()]
        self._persist_journal()

    def _needs_driver(self) -> bool:
        return bool(
            self._state.get("pending")
            or self._state.get("run_status")
            in {"running", "paused", "retry_wait"}
        )

    def _update_network_failure_streak(
        self,
        attempt: ScheduledCollectionAttempt,
    ) -> bool:
        network_like = attempt.failure_kind == "network" or (
            attempt.retryable
            and attempt.failure_kind
            not in {
                "validation-uncertain",
                "stock-unprobed",
                "suspected-invalid",
                "confirmed-invalid",
            }
        )
        if attempt.succeeded or not network_like:
            self._state["network_failures"] = []
            return False
        failures = self._network_failure_rows()
        failures.append(
            {
                "plid": attempt.plid,
                "message": self._single_line(attempt.message),
            }
        )
        self._state["network_failures"] = failures[-2:]
        self._persist_journal()
        return len(failures) >= 2

    def _network_failure_rows(self) -> list[dict[str, str]]:
        raw = self._state.get("network_failures")
        if not isinstance(raw, list):
            return []
        return [
            {
                "plid": str(item.get("plid") or ""),
                "message": self._single_line(str(item.get("message") or "")),
            }
            for item in raw
            if isinstance(item, dict)
        ]

    def _pause_remaining(self) -> float:
        raw = self._optional_text(self._state.get("resume_after"))
        if raw is None:
            return 0.0
        try:
            resume_after = datetime.fromisoformat(raw)
        except ValueError:
            return 0.0
        if resume_after.tzinfo is None:
            resume_after = resume_after.replace(tzinfo=UTC)
        return max(0.0, (resume_after - self._clock_utc()).total_seconds())

    @staticmethod
    def _network_pause_reason(
        failures: list[dict[str, str]],
        remaining_seconds: float,
    ) -> str:
        failure_text = "；".join(
            f"PLID{item['plid']}：{item['message']}" for item in failures
        )
        wait_seconds = max(0, int(round(remaining_seconds)))
        return (
            f"连续2条网络连接失败，自动暂停 {wait_seconds} 秒后续爬"
            + (f"；{failure_text}" if failure_text else "")
        )[:500]

    def _pending_retry_wait_reason(
        self,
        pending: int,
        retry_round: int,
        remaining_seconds: float,
    ) -> str:
        wait_seconds = max(0, int(round(remaining_seconds)))
        if wait_seconds > 0:
            return (
                f"仍有 {pending} 条待重试，自动等待 {wait_seconds} 秒后开始第 "
                f"{retry_round}/{self._pending_retry_round_limit} 轮延时重试；"
                "疑似失效链接按至少10分钟间隔复核"
            )[:500]
        return (
            f"等待间隔已结束，开始第 {retry_round}/{self._pending_retry_round_limit} "
            f"轮延时重试，共 {pending} 条"
        )[:500]

    def _restore_completed_pending_wait(self) -> None:
        """Upgrade an older completed-with-pending journal into a retry wait."""
        if self._state.get("run_status") != "completed_with_pending":
            return
        metrics = self._metrics()
        current_round = self._pending_retry_round()
        if (
            metrics["pending"] <= 0
            or current_round >= self._pending_retry_round_limit
        ):
            return
        retry_round = current_round + 1
        retry_queue = self._resume_queue(
            deferred_failures=True,
            retry_round=retry_round,
        )
        if not retry_queue:
            return
        completed_at = self._clock_utc()
        completed_raw = self._optional_text(self._state.get("last_completed_at"))
        if completed_raw is not None:
            try:
                completed_at = datetime.fromisoformat(completed_raw)
            except ValueError:
                pass
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=UTC)
            completed_at = completed_at.astimezone(UTC)
        resume_after = completed_at + timedelta(
            seconds=self._pending_retry_delay_seconds
        )
        self._state.update(
            {
                "run_status": "retry_wait",
                "queue": retry_queue,
                "resume_after": resume_after.isoformat(),
                "pending_retry_round": retry_round,
                "network_failures": [],
            }
        )
        self._persist_journal()
        self._logger.info(
            "scheduled_batch batch=%s event=restore_completed_pending "
            "round=%s/%s pending=%s resume_after=%s",
            self._state.get("batch_id") or "-",
            retry_round,
            self._pending_retry_round_limit,
            metrics["pending"],
            resume_after.isoformat(),
        )

    def _import_trigger_files(self) -> None:
        if self._trigger_dir is None or not self._trigger_dir.is_dir():
            return
        handled = self._handled_trigger_dates()
        changed = False
        today = self._beijing_date()
        for path in sorted(self._trigger_dir.glob("????-??-??.json")):
            trigger_date = path.stem
            try:
                datetime.strptime(trigger_date, "%Y-%m-%d")
            except ValueError:
                continue
            if trigger_date in handled:
                continue
            if trigger_date == today and self._state.get("last_started_on") == today:
                handled.add(trigger_date)
                changed = True
                continue
            if self._state.get("pending"):
                break
            self._state["pending"] = True
            self._state["pending_for"] = trigger_date
            self._state["requested_at"] = self._utc_iso()
            changed = True
            break
        if changed:
            self._state["handled_trigger_dates"] = sorted(handled)
            self._persist_journal()

    def _ensure_driver(self) -> None:
        if self._closing:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drive())

    def _trigger_payload(self, state: str, *, accepted: bool) -> dict[str, object]:
        return {"ok": True, "accepted": accepted, "state": state, **self.status()}

    def _load_journal(self) -> dict[str, Any]:
        default = self._default_state()
        if self._journal_path is None or not self._journal_path.is_file():
            return default
        try:
            payload = json.loads(self._journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return default
        default.update(payload)
        active_item = default.get("active_item")
        if default.get("run_status") == "running" and isinstance(active_item, dict):
            queue = default.get("queue")
            default["queue"] = [active_item, *(queue if isinstance(queue, list) else [])]
            default["active_item"] = None
        return default

    def _persist_journal(self) -> None:
        if self._journal_path is None:
            return
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._journal_path.with_suffix(f"{self._journal_path.suffix}.tmp")
        temporary.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self._journal_path)

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "version": 1,
            "pending": False,
            "pending_for": None,
            "requested_at": None,
            "run_status": "idle",
            "batch_id": None,
            "last_started_on": None,
            "last_started_at": None,
            "last_completed_at": None,
            "last_error": "",
            "last_skipped_on": None,
            "last_skipped_at": None,
            "last_skip_reason": "",
            "targets": [],
            "queue": [],
            "attempted_indexes": [],
            "failed_indexes": [],
            "terminal_indexes": [],
            "stock_unprobed_indexes": [],
            "results": [],
            "errors": [],
            "accepted_priority_keys": [],
            "last_target_refresh_completed": 0,
            "resume_after": None,
            "network_failures": [],
            "pending_retry_round": 0,
            "completion_reason": "",
            "handled_trigger_dates": [],
            "active_item": None,
            "run_revision": 0,
            "explicit_resume_count": 0,
        }

    def _targets(self) -> list[dict[str, object]]:
        raw = self._state.get("targets")
        return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def _queue(self) -> list[dict[str, object]]:
        raw = self._state.get("queue")
        return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def _result_rows(
        self,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        raw = self._state.get("results")
        if not isinstance(raw, list):
            return []
        rows = [item for item in raw if isinstance(item, dict)]
        end = None if limit is None else max(0, offset) + max(0, limit)
        return [dict(item) for item in rows[max(0, offset):end]]

    def _error_rows(
        self,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, str]]:
        raw = self._state.get("errors")
        if not isinstance(raw, list):
            return []
        rows = [item for item in raw if isinstance(item, dict)]
        end = None if limit is None else max(0, offset) + max(0, limit)
        return [
            {str(key): str(value) for key, value in item.items()}
            for item in rows[max(0, offset):end]
        ]

    def _row_count(self, key: str) -> int:
        raw = self._state.get(key)
        if not isinstance(raw, list):
            return 0
        return sum(1 for item in raw if isinstance(item, dict))

    def _index_set(self, key: str) -> set[int]:
        raw = self._state.get(key)
        if not isinstance(raw, list):
            return set()
        return {int(value) for value in raw if isinstance(value, int)}

    def _handled_trigger_dates(self) -> set[str]:
        raw = self._state.get("handled_trigger_dates")
        if not isinstance(raw, list):
            return set()
        return {str(value) for value in raw}

    def _run_revision(self) -> int:
        return self._optional_int(self._state.get("run_revision")) or 0

    def _pending_retry_round(self) -> int:
        return self._optional_int(self._state.get("pending_retry_round")) or 0

    def _beijing_date(self) -> str:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return now.astimezone(BEIJING).date().isoformat()

    def _utc_iso(self) -> str:
        return self._clock_utc().isoformat()

    def _clock_utc(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return now.astimezone(UTC)

    @staticmethod
    def _plid_from_url(url: str) -> str:
        matches = list(re.finditer(r"PLID(?P<plid>\d+)", url, flags=re.IGNORECASE))
        return matches[-1].group("plid") if matches else ""

    @staticmethod
    def _single_line(value: str) -> str:
        return " ".join(value.split())[:500]

    @staticmethod
    def _optional_text(value: object) -> str | None:
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _optional_int(value: object) -> int | None:
        return value if isinstance(value, int) else None

    @staticmethod
    def _required_int(value: object) -> int:
        if not isinstance(value, int):
            raise ValueError("定时竞品批次断点索引无效")
        return value
