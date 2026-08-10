"""In-process idempotency and durable file logging for browser collection batches."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import RLock
from typing import Any, Generic, TypeVar


T = TypeVar("T")
COLLECTION_STALE_AFTER = timedelta(minutes=2)
NONBLOCKING_PAUSE_REASON_PREFIXES = (
    "监控清单新增了 ",
    "PLID",
)


class CollectionBatchBusyError(RuntimeError):
    """Raised when another browser already owns the global collection slot."""


@dataclass
class CollectionBatchStatus:
    """Process-wide batch state projected to every logged-in ERP browser."""

    active: bool = False
    batch_id: str | None = None
    owner_username: str | None = None
    owner_display_name: str | None = None
    source: str = "manual"
    event: str = "idle"
    completed: int = 0
    total: int = 0
    pending: int = 0
    succeeded: int = 0
    failed: int = 0
    terminal: int = 0
    current_index: int | None = None
    current_plid: str | None = None
    current_request_id: str | None = None
    current_stage: str | None = None
    current_retry_kind: str | None = None
    current_retry_attempt: int | None = None
    with_stock_probe: bool = True
    visible_browser: bool = False
    takeover_pending: bool = False
    reason: str = ""
    started_at: str | None = None
    updated_at: str | None = None
    queued_targets: list[dict[str, object]] = field(default_factory=list)
    priority_targets: list[dict[str, object]] = field(default_factory=list)
    prioritized_targets: list[dict[str, object]] = field(default_factory=list)
    results: list[dict[str, object]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)


class CollectionBatchRegistry:
    """Synchronize one active competitor batch across all ERP users."""

    def __init__(self, journal_path: Path | None = None) -> None:
        self._lock = RLock()
        self._state = CollectionBatchStatus()
        self._release_after_request = False
        self._owner_client_id: str | None = None
        self._takeover_client_id: str | None = None
        self._journal_path = journal_path
        self._journal = self._load_journal()

    def status(self) -> dict[str, object]:
        with self._lock:
            self._expire_if_abandoned()
            return asdict(self._state)

    def stop(self, *, batch_id: str, reason: str) -> dict[str, object]:
        """Stop either a page-owned or scheduled batch without changing counters."""
        with self._lock:
            self._expire_if_abandoned()
            if self._state.batch_id == batch_id and self._state.event == "manual_stop":
                return asdict(self._state)
            if not self._state.active or self._state.batch_id != batch_id:
                raise CollectionBatchBusyError("当前竞品批次已结束或批次编号不匹配")
            self._state.event = "manual_stop"
            self._state.reason = reason
            self._state.updated_at = _utc_iso()
            self._takeover_client_id = None
            self._state.takeover_pending = False
            # Keep the global slot occupied until request cancellation *and*
            # both browser cleanup paths have completed.  Releasing here lets
            # another batch start while the old stop handler is still closing
            # shared browser state.
            self._release_after_request = True
            self._persist_journal()
            return asdict(self._state)

    def complete_stop(self, *, batch_id: str) -> dict[str, object]:
        """Release a stopped batch only after all asynchronous cleanup is done."""
        with self._lock:
            if (
                self._state.batch_id == batch_id
                and self._state.event == "manual_stop"
                and not self._state.active
            ):
                return asdict(self._state)
            if self._state.batch_id != batch_id or self._state.event != "manual_stop":
                raise CollectionBatchBusyError("当前竞品批次停止状态已变化")
            self._state.active = False
            self._state.current_index = None
            self._state.current_plid = None
            self._state.current_request_id = None
            self._state.current_stage = None
            self._state.current_retry_kind = None
            self._state.current_retry_attempt = None
            self._state.takeover_pending = False
            self._release_after_request = False
            self._owner_client_id = None
            self._takeover_client_id = None
            self._state.updated_at = _utc_iso()
            self._persist_journal()
            return asdict(self._state)

    def update_options(
        self,
        *,
        batch_id: str,
        username: str,
        visible_browser: bool,
    ) -> dict[str, object]:
        """Update options that may safely take effect from the next link."""
        with self._lock:
            self._expire_if_abandoned()
            if not self._state.active or self._state.batch_id != batch_id:
                raise CollectionBatchBusyError("当前竞品批次已结束或批次编号不匹配")
            owner_matches = (
                self._state.owner_username is not None
                and self._state.owner_username.casefold() == username.casefold()
            )
            scheduled_controller = (
                self._state.source == "scheduled" and username.casefold() == "kxx"
            )
            if not owner_matches and not scheduled_controller:
                raise CollectionBatchBusyError(self._busy_message())
            if self._release_after_request:
                raise CollectionBatchBusyError("当前商品正在完成停止清理，不能再修改批次设置")
            self._state.visible_browser = visible_browser
            self._state.updated_at = _utc_iso()
            self._persist_journal()
            return asdict(self._state)

    def request_takeover(
        self,
        *,
        batch_id: str,
        client_id: str,
        username: str,
    ) -> tuple[dict[str, object], bool]:
        """Transfer same-account control at a link boundary, never mid-request."""
        with self._lock:
            self._expire_if_abandoned()
            if not self._state.active or self._state.batch_id != batch_id:
                raise CollectionBatchBusyError("当前竞品批次已结束或批次编号不匹配")
            if self._state.owner_username != username:
                raise CollectionBatchBusyError(self._busy_message())
            if self._release_after_request:
                raise CollectionBatchBusyError("当前商品正在完成停止清理，请稍后从断点继续")
            if self._owner_client_id == client_id:
                self._takeover_client_id = None
                self._state.takeover_pending = False
                return asdict(self._state), True
            if (
                self._takeover_client_id is not None
                and self._takeover_client_id != client_id
            ):
                raise CollectionBatchBusyError("本账号已有另一个页面正在申请接管该批次")
            if self._state.current_request_id is not None:
                self._takeover_client_id = client_id
                self._state.takeover_pending = True
                self._state.updated_at = _utc_iso()
                return asdict(self._state), False
            self._owner_client_id = client_id
            self._takeover_client_id = None
            self._state.takeover_pending = False
            self._state.event = "takeover_ready"
            self._state.updated_at = _utc_iso()
            return asdict(self._state), True

    def collection_options(
        self,
        *,
        batch_id: str | None,
        fallback_with_stock_probe: bool,
        fallback_visible_browser: bool,
    ) -> tuple[bool, bool]:
        """Freeze effective options when one link request starts."""
        if not batch_id:
            return fallback_with_stock_probe, fallback_visible_browser
        with self._lock:
            if self._state.active and self._state.batch_id == batch_id:
                return self._state.with_stock_probe, self._state.visible_browser
        return fallback_with_stock_probe, fallback_visible_browser

    def enqueue_target(
        self,
        *,
        plid: str,
        url: str,
        batch_id: str | None = None,
    ) -> bool:
        """Append a newly persisted target to the active batch tail."""
        with self._lock:
            self._expire_if_abandoned()
            if (
                not self._state.active
                or self._release_after_request
                or (batch_id is not None and self._state.batch_id != batch_id)
            ):
                return False
            if self._state.current_plid == plid or any(
                item["plid"] == plid for item in self._state.queued_targets
            ):
                return False
            queued_at = _utc_iso()
            self._state.queued_targets.append(
                {
                    "plid": plid,
                    "url": url,
                    "queued_at": queued_at,
                },
            )
            self._state.total += 1
            self._state.pending += 1
            self._state.updated_at = _utc_iso()
            self._persist_journal()
            return True

    def record_outcome(
        self,
        *,
        batch_id: str | None,
        plid: str,
        url: str,
        title: str | None,
        message: str,
        succeeded: bool,
    ) -> None:
        """Publish per-link results so a scheduled batch has the same visible detail."""
        if not batch_id or not plid:
            return
        with self._lock:
            if self._state.batch_id != batch_id:
                return
            self._state.results = [
                item for item in self._state.results if str(item.get("plid")) != plid
            ]
            self._state.errors = [
                item for item in self._state.errors if item.get("plid") != plid
            ]
            if succeeded:
                self._state.results.append(
                    {
                        "plid": plid,
                        "url": url,
                        "title": title or "",
                        "message": message,
                    }
                )
            else:
                self._state.errors.append(
                    {
                        "plid": plid,
                        "url": url,
                        "message": message,
                    }
                )
            self._state.updated_at = _utc_iso()
            self._persist_journal()

    def prioritize_target(
        self,
        *,
        plid: str,
        url: str,
        requested_by: str,
        source: str = "manual",
    ) -> tuple[dict[str, object], bool]:
        """Request one extra priority attempt without removing the original slot."""
        if source not in {"manual", "manual_retry"}:
            raise ValueError(f"Unsupported priority source: {source}")
        with self._lock:
            self._expire_if_abandoned()
            if not self._state.active or self._release_after_request:
                raise CollectionBatchBusyError("当前没有可插队的运行中竞品批次")
            if any(item["plid"] == plid for item in self._state.priority_targets):
                return asdict(self._state), False
            if source != "manual_retry" and any(
                item["plid"] == plid for item in self._state.prioritized_targets
            ):
                return asdict(self._state), False
            if self._state.current_plid == plid:
                raise CollectionBatchBusyError(f"PLID{plid} 当前已经在探测，无需重复插队")
            requested_at = _utc_iso()
            self._state.priority_targets = [
                item for item in self._state.priority_targets if item["plid"] != plid
            ]
            self._state.priority_targets.append(
                {
                    "plid": plid,
                    "url": url,
                    "requested_at": requested_at,
                    "requested_by": requested_by,
                    "source": source,
                }
            )
            self._state.prioritized_targets.insert(
                0,
                {
                    "plid": plid,
                    "url": url,
                    "requested_at": requested_at,
                    "requested_by": requested_by,
                    "source": source,
                },
            )
            self._state.updated_at = _utc_iso()
            self._persist_journal()
            return asdict(self._state), True

    def event(
        self,
        *,
        batch_id: str,
        client_id: str | None,
        event: str,
        username: str,
        display_name: str,
        completed: int,
        total: int,
        pending: int,
        succeeded: int,
        failed: int,
        terminal: int,
        reason: str,
        with_stock_probe: bool = True,
        visible_browser: bool = False,
        source: str = "manual",
        results: list[dict[str, object]] | None = None,
        errors: list[dict[str, str]] | None = None,
    ) -> dict[str, object]:
        with self._lock:
            self._expire_if_abandoned()
            if event == "paused" and _is_nonblocking_pause_reason(reason):
                event = "progress"
            active_event = event in {
                "start",
                "resume",
                "auto_resume",
                "progress",
                "heartbeat",
                "scheduled_pause",
            }
            if active_event and self._state.active and self._state.batch_id != batch_id:
                raise CollectionBatchBusyError(self._busy_message())
            if (
                self._state.active
                and self._state.batch_id == batch_id
                and (
                    self._state.owner_username != username
                    or (self._owner_client_id is not None and client_id != self._owner_client_id)
                )
            ):
                raise CollectionBatchBusyError(self._busy_message())
            if active_event and self._state.batch_id == batch_id and self._release_after_request:
                raise CollectionBatchBusyError("当前商品正在完成停止清理，请等待浏览器探测退出")
            if (
                self._release_after_request
                and self._state.batch_id == batch_id
                and event != "manual_stop"
            ):
                raise CollectionBatchBusyError("当前批次正在完成停止清理")

            now = _utc_iso()
            if active_event:
                if not self._state.active:
                    self._release_after_request = False
                    self._owner_client_id = client_id
                    restored = self._journal_for_batch(batch_id)
                    restored_visible_browser = restored.get("visible_browser")
                    self._state = CollectionBatchStatus(
                        active=True,
                        batch_id=batch_id,
                        owner_username=username,
                        owner_display_name=display_name,
                        source=source,
                        event=event,
                        started_at=now,
                        with_stock_probe=with_stock_probe,
                        visible_browser=(
                            restored_visible_browser
                            if isinstance(restored_visible_browser, bool)
                            else visible_browser
                        ),
                        queued_targets=restored["queued_targets"],
                        priority_targets=restored["priority_targets"],
                        prioritized_targets=restored["prioritized_targets"],
                        results=(
                            [dict(item) for item in results]
                            if results is not None
                            else restored["results"]
                        ),
                        errors=[
                            {str(key): str(value) for key, value in item.items()}
                            for item in (
                                errors if errors is not None else restored["errors"]
                            )
                        ],
                    )
                self._state.active = True
                self._state.event = event
            elif self._state.batch_id == batch_id:
                self._state.event = event
                if self._state.current_request_id is not None:
                    self._release_after_request = True
                else:
                    self._state.active = False
                    self._state.current_index = None
                    self._state.current_plid = None
                    self._state.current_request_id = None
                    self._state.current_stage = None
                    self._state.current_retry_kind = None
                    self._state.current_retry_attempt = None
                    self._state.takeover_pending = False
                    self._takeover_client_id = None

            if self._state.batch_id == batch_id:
                self._state.completed = completed
                self._state.total = max(self._state.total, total)
                self._state.pending = max(
                    pending,
                    self._state.total - completed,
                )
                self._state.succeeded = succeeded
                self._state.failed = failed
                self._state.terminal = terminal
                self._state.reason = reason
                if results is not None:
                    self._state.results = [dict(item) for item in results]
                if errors is not None:
                    self._state.errors = [dict(item) for item in errors]
                self._state.updated_at = now
                if event == "completed" and pending == 0:
                    self._clear_journal()
                else:
                    self._persist_journal()
            return asdict(self._state)

    def start_link(
        self,
        *,
        batch_id: str | None,
        client_id: str | None,
        request_id: str | None,
        username: str,
        display_name: str,
        item_index: int | None,
        total_items: int | None,
        plid: str,
        retry_kind: str | None = None,
        retry_attempt: int | None = None,
        with_stock_probe: bool = True,
        visible_browser: bool = False,
    ) -> None:
        if not batch_id:
            return
        with self._lock:
            self._expire_if_abandoned()
            if self._state.active and self._state.batch_id != batch_id:
                raise CollectionBatchBusyError(self._busy_message())
            if self._release_after_request:
                raise CollectionBatchBusyError("当前批次正在完成停止清理")
            if self._state.active and (
                self._state.owner_username != username
                or (self._owner_client_id is not None and client_id != self._owner_client_id)
            ):
                raise CollectionBatchBusyError(self._busy_message())
            if (
                self._state.current_request_id is not None
                or self._state.current_plid is not None
            ):
                rejoining_same_request = (
                    request_id is not None
                    and self._state.current_request_id == request_id
                    and self._state.current_plid == plid
                )
                if rejoining_same_request:
                    return
                current = (
                    f"PLID{self._state.current_plid}"
                    if self._state.current_plid
                    else "上一条商品"
                )
                raise CollectionBatchBusyError(
                    f"{current} 仍在检测；已阻止另一页面并发启动新链接"
                )
            now = _utc_iso()
            if not self._state.active:
                self._release_after_request = False
                self._owner_client_id = client_id
                self._state = CollectionBatchStatus(
                    active=True,
                    batch_id=batch_id,
                    owner_username=username,
                    owner_display_name=display_name,
                    event="collecting",
                    started_at=now,
                    with_stock_probe=with_stock_probe,
                    visible_browser=visible_browser,
                )
            self._state.event = "collecting"
            self._state.priority_targets = [
                item for item in self._state.priority_targets if item["plid"] != plid
            ]
            self._state.queued_targets = [
                item for item in self._state.queued_targets if item["plid"] != plid
            ]
            self._state.current_index = item_index
            self._state.current_plid = plid
            self._state.current_request_id = request_id
            self._state.current_stage = "正在登记采集链接"
            self._state.current_retry_kind = retry_kind
            self._state.current_retry_attempt = retry_attempt
            if total_items is not None:
                self._state.total = total_items
            self._state.updated_at = now
            self._persist_journal()

    def update_link_stage(
        self,
        *,
        batch_id: str | None,
        request_id: str | None,
        stage: str,
    ) -> None:
        """Publish the active link's current backend stage to every ERP page."""
        if not batch_id:
            return
        with self._lock:
            if (
                self._state.batch_id != batch_id
                or self._state.current_request_id != request_id
            ):
                return
            self._state.current_stage = stage
            self._state.updated_at = _utc_iso()

    def finish_link(
        self,
        *,
        batch_id: str | None,
        request_id: str | None,
        reason: str,
    ) -> None:
        if not batch_id:
            return
        with self._lock:
            if self._state.batch_id == batch_id and self._state.current_request_id == request_id:
                self._state.current_index = None
                self._state.current_plid = None
                self._state.current_request_id = None
                self._state.current_stage = None
                self._state.current_retry_kind = None
                self._state.current_retry_attempt = None
                releasing_after_manual_stop = (
                    self._release_after_request
                    and self._state.event == "manual_stop"
                )
                if not releasing_after_manual_stop:
                    self._state.reason = reason
                self._state.updated_at = _utc_iso()
                if self._release_after_request:
                    self._takeover_client_id = None
                    self._state.takeover_pending = False
                elif self._takeover_client_id is not None:
                    self._owner_client_id = self._takeover_client_id
                    self._takeover_client_id = None
                    self._state.takeover_pending = False
                    self._state.event = "takeover_ready"
                self._persist_journal()

    def _load_journal(self) -> dict[str, object]:
        if self._journal_path is None or not self._journal_path.is_file():
            return {}
        try:
            payload = json.loads(self._journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _journal_for_batch(self, batch_id: str) -> dict[str, Any]:
        if self._journal.get("batch_id") != batch_id:
            self._journal = {}
            return {
                "queued_targets": [],
                "priority_targets": [],
                "prioritized_targets": [],
                "results": [],
                "errors": [],
                "visible_browser": None,
            }
        restored_visible_browser = self._journal.get("visible_browser")
        result: dict[str, Any] = {
            "visible_browser": (
                restored_visible_browser
                if isinstance(restored_visible_browser, bool)
                else None
            )
        }
        for key in (
            "queued_targets",
            "priority_targets",
            "prioritized_targets",
            "results",
            "errors",
        ):
            raw_items = self._journal.get(key)
            result[key] = [
                {str(name): value for name, value in item.items()}
                for item in raw_items
                if isinstance(item, dict)
            ] if isinstance(raw_items, list) else []
        return result

    def _persist_journal(self) -> None:
        if self._journal_path is None or not self._state.batch_id:
            return
        self._journal = {
            "batch_id": self._state.batch_id,
            "queued_targets": self._state.queued_targets,
            "priority_targets": self._state.priority_targets,
            "prioritized_targets": self._state.prioritized_targets,
            "results": self._state.results,
            "errors": self._state.errors,
            "visible_browser": self._state.visible_browser,
        }
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._journal_path.with_suffix(
            f"{self._journal_path.suffix}.tmp"
        )
        temporary_path.write_text(
            json.dumps(self._journal, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(self._journal_path)

    def _clear_journal(self) -> None:
        self._journal = {}
        if self._journal_path is not None:
            self._journal_path.unlink(missing_ok=True)

    def _expire_if_abandoned(self) -> None:
        if (
            not self._state.active
            or self._release_after_request
            or self._state.current_request_id is not None
            or self._state.updated_at is None
        ):
            return
        updated = datetime.fromisoformat(self._state.updated_at)
        if datetime.now(UTC) - updated > COLLECTION_STALE_AFTER:
            self._state.active = False
            self._state.event = "stale"
            self._takeover_client_id = None
            self._state.takeover_pending = False
            self._state.reason = "采集页面长时间未发送进度，已自动释放全局采集占用"
            self._state.updated_at = _utc_iso()

    def _busy_message(self) -> str:
        owner = self._state.owner_display_name or self._state.owner_username or "其他用户"
        progress = f"{self._state.completed}/{self._state.total}" if self._state.total else "准备中"
        return f"{owner} 正在采集竞品（{progress}），请等待当前批次结束或暂停"


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _is_nonblocking_pause_reason(reason: str) -> bool:
    normalized = " ".join(reason.split())
    if normalized.startswith(NONBLOCKING_PAUSE_REASON_PREFIXES[0]):
        return True
    return (
        normalized.startswith(NONBLOCKING_PAUSE_REASON_PREFIXES[1])
        and "库存仍未探测" in normalized
        and "复探" in normalized
    )


class CollectionRequestCoordinator(Generic[T]):
    """Let a reloaded page rejoin the same in-flight link request safely."""

    def __init__(self, *, max_completed: int = 2_000) -> None:
        self._max_completed = max_completed
        self._lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Task[T]] = {}
        self._completed: OrderedDict[str, T] = OrderedDict()

    async def run(
        self,
        request_id: str | None,
        operation: Callable[[], Awaitable[T]],
    ) -> tuple[T, bool]:
        """Run once per request id and report whether existing work was reused."""
        if not request_id:
            return await operation(), False

        async with self._lock:
            cached = self._completed.get(request_id)
            if cached is not None:
                self._completed.move_to_end(request_id)
                return cached, True
            task = self._inflight.get(request_id)
            reused = task is not None
            if task is None:
                task = asyncio.create_task(self._execute(request_id, operation))
                self._inflight[request_id] = task

        return await asyncio.shield(task), reused

    async def cancel(self, request_id: str | None) -> bool:
        """Cancel one explicit in-flight request while preserving reload shielding."""
        if not request_id:
            return False
        async with self._lock:
            task = self._inflight.get(request_id)
            if task is None:
                return False
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    async def _execute(
        self,
        request_id: str,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        try:
            result = await operation()
        except BaseException:
            async with self._lock:
                self._inflight.pop(request_id, None)
            raise

        async with self._lock:
            self._inflight.pop(request_id, None)
            self._completed[request_id] = result
            self._completed.move_to_end(request_id)
            while len(self._completed) > self._max_completed:
                self._completed.popitem(last=False)
        return result


def configure_collection_logger(project_root: Path) -> logging.Logger:
    """Create one rotating competitor collection log for the project."""
    log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    target = (log_dir / "competitor-collection.log").resolve()
    logger = logging.getLogger(f"takealot_ops.competitors.collection.{abs(hash(str(target)))}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(
        isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).resolve() == target
        for handler in logger.handlers
    ):
        handler = RotatingFileHandler(
            target,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger
