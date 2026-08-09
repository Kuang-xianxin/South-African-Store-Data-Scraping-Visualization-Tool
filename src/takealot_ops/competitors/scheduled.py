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
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._state = self._load_journal()

    def start(self) -> None:
        """Resume a durable pending or interrupted scheduled run on ERP startup."""
        self._import_trigger_files()
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
                return self._trigger_payload("already_pending", accepted=False)
            self._state["pending"] = True
            self._state["pending_for"] = trigger_date
            self._state["requested_at"] = self._utc_iso()
            self._state["last_error"] = ""
            self._persist_journal()
        self._logger.info("scheduled_trigger date=%s state=accepted", trigger_date)
        self._ensure_driver()
        return self._trigger_payload("accepted", accepted=True)

    async def mark_stopped(self, batch_id: str, *, stopped_by: str) -> bool:
        """Persist that kxx stopped this scheduled batch; never restart it today."""
        async with self._lock:
            if (
                self._state.get("run_status") not in {"running", "paused"}
                or self._state.get("batch_id") != batch_id
            ):
                return False
            self._state["run_status"] = "stopped"
            self._state["pending"] = False
            self._state["active_item"] = None
            self._state["resume_after"] = None
            self._state["network_failures"] = []
            self._state["stopped_at"] = self._utc_iso()
            self._state["stopped_by"] = stopped_by
            self._persist_journal()
        self._logger.info(
            "scheduled_batch batch=%s event=manual_stop user=%s",
            batch_id,
            stopped_by,
        )
        return True

    def status(self) -> dict[str, object]:
        """Return a small copy suitable for the loopback trigger response."""
        return {
            "pending": bool(self._state.get("pending")),
            "pending_for": self._state.get("pending_for"),
            "run_status": self._state.get("run_status", "idle"),
            "batch_id": self._state.get("batch_id"),
            "last_started_on": self._state.get("last_started_on"),
            "last_started_at": self._state.get("last_started_at"),
            "last_completed_at": self._state.get("last_completed_at"),
            "last_error": self._state.get("last_error", ""),
        }

    async def _drive(self) -> None:
        try:
            while not self._closing:
                self._import_trigger_files()
                run_status = str(self._state.get("run_status") or "idle")
                if run_status == "running":
                    await self._resume_current_run()
                    continue
                if run_status == "paused":
                    await self._wait_for_network_resume()
                    continue
                if not self._state.get("pending"):
                    return
                if self._registry.status().get("active"):
                    await self._sleep(self._busy_poll_seconds)
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
                if not await self._begin_run(targets):
                    await self._sleep(self._busy_poll_seconds)
                    continue
                await self._run_current_queue()
        finally:
            if self._task is asyncio.current_task():
                self._task = None

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
        try:
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
        except CollectionBatchBusyError:
            return False

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
                    "active_item": None,
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
                await self._complete_run(batch_id)
                return

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
                if self._state.get("run_status") == "stopped":
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

            if self._state.get("run_status") == "stopped":
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

    async def _wait_for_network_resume(self) -> None:
        batch_id = str(self._state.get("batch_id") or "")
        if not batch_id:
            self._state["run_status"] = "failed"
            self._state["last_error"] = "定时批次暂停断点缺少批次编号"
            self._persist_journal()
            return
        while not self._closing and self._state.get("run_status") == "paused":
            remaining = self._network_pause_remaining()
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
        reason = (
            f"09:00 自动批次结束，仍有 {metrics['pending']} 个待重试或未完成链接"
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
        queue = self._queue()
        retry_kind: str | None = None
        limit = 0
        if attempt.failure_kind == "stock-unprobed":
            retry_kind = "stock"
            limit = 2
        elif attempt.retryable or attempt.failure_kind in {
            "network",
            "validation-uncertain",
            "suspected-invalid",
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

    def _prepend_active_item(self, item: dict[str, object]) -> None:
        self._state["active_item"] = None
        self._state["queue"] = [item, *self._queue()]
        self._persist_journal()

    def _needs_driver(self) -> bool:
        return bool(
            self._state.get("pending")
            or self._state.get("run_status") in {"running", "paused"}
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

    def _network_pause_remaining(self) -> float:
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
            "handled_trigger_dates": [],
            "active_item": None,
        }

    def _targets(self) -> list[dict[str, object]]:
        raw = self._state.get("targets")
        return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def _queue(self) -> list[dict[str, object]]:
        raw = self._state.get("queue")
        return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def _result_rows(self) -> list[dict[str, object]]:
        raw = self._state.get("results")
        return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def _error_rows(self) -> list[dict[str, str]]:
        raw = self._state.get("errors")
        if not isinstance(raw, list):
            return []
        return [
            {str(key): str(value) for key, value in item.items()}
            for item in raw
            if isinstance(item, dict)
        ]

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
