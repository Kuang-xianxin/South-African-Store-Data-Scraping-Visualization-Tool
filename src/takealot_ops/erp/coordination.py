"""Cross-user coordination for expensive ERP actions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.migrations import create_engine_for_settings, create_schema
from takealot_ops.storage.models import ErpRefreshState
from takealot_ops.storage.store_context import DEFAULT_STORE_CODE, store_scope


REFRESH_ACTION_KEY = "full_store_refresh"
REFRESH_COOLDOWN = timedelta(hours=1)


class RefreshBusyError(RuntimeError):
    """Raised when a refresh is running or the global cooldown is active."""


class RefreshCoordinator:
    """Enforce one refresh at a time and persist the shared cooldown."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root
        self._lock = Lock()
        self._engine: Engine | None = None
        self._in_progress = False
        self._in_progress_by: str | None = None
        self._in_progress_display_name: str | None = None
        self._started_at: datetime | None = None

    def close(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.dispose()
                self._engine = None

    def status(self, *, role: str) -> dict[str, object]:
        with self._lock:
            return self._status_locked(role=role)

    def begin(
        self,
        *,
        username: str,
        display_name: str,
        role: str,
    ) -> dict[str, object]:
        with self._lock:
            status = self._status_locked(role=role)
            if self._in_progress:
                owner = self._in_progress_display_name or self._in_progress_by or "其他用户"
                raise RefreshBusyError(f"{owner} 正在刷新全部数据，请等待本次刷新完成")
            remaining_value = status["cooldown_remaining_seconds"]
            remaining = remaining_value if isinstance(remaining_value, int) else 0
            if role != "admin" and remaining > 0:
                minutes, seconds = divmod(remaining, 60)
                raise RefreshBusyError(
                    f"刷新全部数据处于全员冷却中，还需等待 {minutes:02d}:{seconds:02d}"
                )
            self._in_progress = True
            self._in_progress_by = username
            self._in_progress_display_name = display_name
            self._started_at = _utc_now()
            return self._status_locked(role=role)

    def finish(
        self,
        *,
        username: str,
        display_name: str,
        succeeded: bool,
        role: str,
    ) -> dict[str, object]:
        with self._lock:
            if succeeded:
                now = _utc_now()
                with store_scope(DEFAULT_STORE_CODE):
                    with Session(self._get_engine()) as session:
                        state = session.get(
                            ErpRefreshState,
                            (DEFAULT_STORE_CODE, REFRESH_ACTION_KEY),
                        )
                        if state is None:
                            state = ErpRefreshState(
                                store_code=DEFAULT_STORE_CODE,
                                action_key=REFRESH_ACTION_KEY,
                                updated_at=now,
                            )
                            session.add(state)
                        state.last_success_at = now
                        state.last_success_by = username
                        state.last_success_display_name = display_name
                        state.updated_at = now
                        session.commit()
            self._in_progress = False
            self._in_progress_by = None
            self._in_progress_display_name = None
            self._started_at = None
            return self._status_locked(role=role)

    def _status_locked(self, *, role: str) -> dict[str, object]:
        now = _utc_now()
        with store_scope(DEFAULT_STORE_CODE):
            with Session(self._get_engine()) as session:
                state = session.get(
                    ErpRefreshState,
                    (DEFAULT_STORE_CODE, REFRESH_ACTION_KEY),
                )
                last_success_at = state.last_success_at if state is not None else None
                last_success_by = state.last_success_by if state is not None else None
                last_success_display_name = (
                    state.last_success_display_name if state is not None else None
                )
        if last_success_at is not None and last_success_at.tzinfo is None:
            last_success_at = last_success_at.replace(tzinfo=UTC)
        cooldown_until = (
            last_success_at + REFRESH_COOLDOWN if last_success_at is not None else None
        )
        remaining = (
            max(0, int((cooldown_until - now).total_seconds() + 0.999))
            if cooldown_until is not None
            else 0
        )
        can_operate = role in {"operator", "admin"}
        can_refresh = (
            can_operate
            and not self._in_progress
            and (role == "admin" or remaining == 0)
        )
        return {
            "in_progress": self._in_progress,
            "in_progress_by": self._in_progress_by,
            "in_progress_display_name": self._in_progress_display_name,
            "started_at": _iso(self._started_at),
            "last_success_at": _iso(last_success_at),
            "last_success_by": last_success_by,
            "last_success_display_name": last_success_display_name,
            "cooldown_until": _iso(cooldown_until),
            "cooldown_remaining_seconds": remaining,
            "cooldown_seconds": int(REFRESH_COOLDOWN.total_seconds()),
            "admin_exempt": role == "admin",
            "can_refresh": can_refresh,
        }

    def _get_engine(self) -> Engine:
        if self._engine is None:
            settings = DashboardSettings.from_env(self._project_root)
            engine = create_engine_for_settings(settings)
            create_schema(engine)
            self._engine = engine
        return self._engine


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()
