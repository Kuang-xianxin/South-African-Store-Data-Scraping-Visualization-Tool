"""Per-ERP-account CLI usage. Official account percentages are never apportioned."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, case, func, insert, select, true, update

from takealot_ops.storage.migrations import create_engine_for_database_url
from takealot_ops.storage.models import ErpCliUsageEvent, ErpStore, ErpUser
from takealot_ops.storage.store_context import current_store_code


@dataclass(frozen=True)
class UsageActor:
    user_id: int | None
    username: str | None
    batch_id: str | None = None
    batch_created_at: datetime | None = None


@dataclass(frozen=True)
class UsageOperation:
    database_url: str
    offer_id: str


_actor: ContextVar[UsageActor] = ContextVar("cli_usage_actor", default=UsageActor(None, None))
_operation: ContextVar[UsageOperation | None] = ContextVar("cli_usage_operation", default=None)


def current_usage_actor() -> UsageActor:
    return _actor.get()


@contextmanager
def usage_actor(user_id: int | None, username: str | None, *, batch_id: str | None = None,
                batch_created_at: datetime | None = None) -> Iterator[None]:
    token = _actor.set(UsageActor(user_id, username, batch_id, batch_created_at))
    try:
        yield
    finally:
        _actor.reset(token)


@contextmanager
def usage_operation(database_url: str, offer_id: str) -> Iterator[None]:
    token = _operation.set(UsageOperation(database_url, offer_id))
    try:
        yield
    finally:
        _operation.reset(token)


@contextmanager
def batch_usage_actor(state: Mapping[str, Any]) -> Iterator[None]:
    """Persisted ownership overrides the identity of an administrator resuming a batch."""
    try:
        parsed = datetime.fromisoformat(str(state.get("created_at")))
        created = parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else None
    except (ValueError, TypeError):
        created = None
    owner_id = state.get("owner_user_id")
    with usage_actor(owner_id if isinstance(owner_id, int) and not isinstance(owner_id, bool) and owner_id > 0 else None,
                     str(state.get("owner_username") or "") or None,
                     batch_id=str(state.get("batch_id") or "") or None, batch_created_at=created):
        yield


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _counter(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class UsageAttempt:
    def __init__(self, operation: UsageOperation, *, model: str, stage: str) -> None:
        self.engine = create_engine_for_database_url(operation.database_url)
        self.id = str(uuid4())
        self.dispatched = False
        actor = current_usage_actor()
        try:
            with self.engine.begin() as connection:
                user_id = actor.user_id
                if user_id is None and actor.username and actor.batch_created_at:
                    users = ErpUser.__table__
                    user_id = connection.execute(select(users.c.id).where(
                        users.c.username == actor.username,
                        users.c.created_at <= actor.batch_created_at,
                    )).scalar()
                connection.execute(insert(ErpCliUsageEvent).values(
                    id=self.id, actor_user_id=user_id, actor_username=actor.username,
                    store_code=current_store_code(), offer_id=operation.offer_id,
                    batch_id=actor.batch_id, model=model, stage=stage,
                    status="running", created_at=_now(),
                ))
        except BaseException:
            self.engine.dispose()
            raise  # Do not dispatch an unrecorded model request.

    def _write(self, **values: Any) -> None:
        table = ErpCliUsageEvent.__table__
        with self.engine.begin() as connection:
            connection.execute(update(ErpCliUsageEvent).where(table.c.id == self.id, table.c.status == "running").values(**values))

    def dispatch(self) -> None:
        self._write(dispatched_at=_now())
        self.dispatched = True

    def bind_turn(self, turn_id: str) -> None:
        self._write(turn_id=turn_id)

    def observe(self, raw: Mapping[str, Any]) -> None:
        fields = {"input_tokens": "inputTokens", "cached_input_tokens": "cachedInputTokens",
                  "output_tokens": "outputTokens", "reasoning_output_tokens": "reasoningOutputTokens",
                  "total_tokens": "totalTokens"}
        values = {key: _counter(raw.get(camel, raw.get(key))) for key, camel in fields.items()}
        if values["total_tokens"] is None and values["input_tokens"] is not None and values["output_tokens"] is not None:
            values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
        # Notifications report cumulative totals for this turn: replace, never add.
        table = ErpCliUsageEvent.__table__
        known = {key: case((table.c[key].is_(None) | (table.c[key] < value), value),
                           else_=table.c[key])
                 for key, value in values.items() if value is not None}
        if known:
            self._write(**known)

    def finish(self, *, error: BaseException | None) -> None:
        try:
            status = "completed" if error is None else ("failed" if self.dispatched else "blocked")
            self._write(status=status, finished_at=_now(), error_kind=type(error).__name__ if error else None)
        finally:
            self.engine.dispose()


def start_usage_attempt(*, model: str, stage: str) -> UsageAttempt | None:
    operation = _operation.get()
    return UsageAttempt(operation, model=model, stage=stage) if operation else None


def usage_summary(engine: Engine, *, user_id: int | None, period: str, now: datetime | None = None) -> dict[str, Any]:
    """Caller enforces user selection. No prompts, credentials or product content are returned."""
    now = now or datetime.now(UTC)
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    days = {"today": 1, "7d": 7, "30d": 30, "all": None}[period]
    start = (local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days-1)).astimezone(UTC).replace(tzinfo=None) if days else None
    end = now.astimezone(UTC).replace(tzinfo=None)
    table = ErpCliUsageEvent.__table__
    where = [table.c.created_at <= end]
    if user_id is not None:
        where.append(table.c.actor_user_id == user_id)
    if start is not None:
        where.append(table.c.created_at >= start)
    token_fields = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
    fields = [
        func.count().label("attempts"),
        func.sum(case((table.c.dispatched_at.is_not(None), 1), else_=0)).label("requests"),
        func.sum(case((table.c.status == "completed", 1), else_=0)).label("completed"),
        func.sum(case((table.c.status == "failed", 1), else_=0)).label("failed"),
        func.sum(case((table.c.status == "blocked", 1), else_=0)).label("blocked"),
        func.sum(case((table.c.status == "running", 1), else_=0)).label("unfinished"),
        func.sum(case((table.c.dispatched_at.is_not(None) & (table.c.total_tokens.is_(None)
            | table.c.input_tokens.is_(None) | table.c.output_tokens.is_(None)), 1), else_=0)).label("unknown_usage_requests"),
        *[func.sum(table.c[name]).label(name) for name in token_fields],
        *[func.count(table.c[name]).label(name+"_reports") for name in token_fields],
    ]
    def numbers(row: Mapping[Any, Any]) -> dict[str, int | None]:
        values: dict[str, int | None] = {field.key: int(row.get(field.key) or 0) for field in fields
                                       if not field.key.endswith("_reports")}
        for name in token_fields:
            if values["requests"] and not row.get(name+"_reports"):
                values[name] = None
        return values

    with engine.connect() as connection:
        total = numbers(connection.execute(select(*fields).where(*where)).mappings().one())
        store_rows = connection.execute(select(table.c.store_code, *fields).where(*where).group_by(table.c.store_code)).mappings().all()
        names = {str(row[0]): str(row[1]) for row in connection.execute(select(ErpStore.__table__.c.code, ErpStore.__table__.c.display_name))}
        by_store = [{"store_code": row["store_code"], "display_name": names.get(row["store_code"], row["store_code"]), **numbers(row)} for row in store_rows]
        users = connection.execute(select(ErpUser.__table__.c.id, ErpUser.__table__.c.username, ErpUser.__table__.c.display_name).where(
            ErpUser.__table__.c.id == user_id if user_id is not None else true())).mappings().all()
        actor_rows = {row["actor_user_id"]: numbers(row) for row in connection.execute(select(table.c.actor_user_id, *fields).where(*where).group_by(table.c.actor_user_id)).mappings()}
        by_user = [{"user_id": row["id"], "username": row["username"], "display_name": row["display_name"], **actor_rows.pop(row["id"], numbers({}))} for row in users]
        by_user.extend({"user_id": actor_id, "username": None, "display_name": "未归属或已删除账号", **values} for actor_id, values in actor_rows.items())
        recent = [dict(row) for row in connection.execute(select(
            table.c.id, table.c.actor_user_id, table.c.actor_username, table.c.store_code,
            table.c.model, table.c.stage, table.c.status, table.c.created_at, table.c.dispatched_at,
            table.c.input_tokens, table.c.output_tokens, table.c.total_tokens,
        ).where(*where).order_by(table.c.created_at.desc(), table.c.id.desc()).limit(20)).mappings()]
        first = connection.execute(select(func.min(table.c.created_at)).where(
            table.c.actor_user_id == user_id if user_id is not None else true())).scalar()
    for row in recent:
        for key in ("created_at", "dispatched_at"):
            row[key] = row[key].replace(tzinfo=UTC).isoformat() if row[key] else None
    return {"period": period, "timezone": "Asia/Shanghai", "start": start.replace(tzinfo=UTC).isoformat() if start else None,
            "as_of": now.isoformat(), "first_recorded_at": first.replace(tzinfo=UTC).isoformat() if first else None,
            "history_scope": "recorded_cli_requests_only", "total": total, "by_user": by_user,
            "by_store": by_store, "recent": recent}
