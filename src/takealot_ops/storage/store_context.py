"""Per-request store context and automatic ORM tenant isolation."""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
from typing import Any

from sqlalchemy import String, event
from sqlalchemy.orm import Mapped, Session, mapped_column, with_loader_criteria


DEFAULT_STORE_CODE = "current"
STORE_CODE_HEADER = "X-Store-Code"
_STORE_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_store_code: ContextVar[str] = ContextVar(
    "takealot_store_code",
    default=DEFAULT_STORE_CODE,
)


def normalize_store_code(value: str | None) -> str:
    """Return a safe canonical store code."""
    code = str(value or DEFAULT_STORE_CODE).strip().casefold()
    if not _STORE_CODE_RE.fullmatch(code):
        raise ValueError("invalid store code")
    return code


def current_store_code() -> str:
    """Return the store selected for the current execution context."""
    return _store_code.get()


@contextmanager
def store_scope(store_code: str) -> Iterator[str]:
    """Temporarily select one store for all store-scoped ORM work."""
    code = normalize_store_code(store_code)
    token = _store_code.set(code)
    try:
        yield code
    finally:
        _store_code.reset(token)


class StoreScopedMixin:
    """Marker and discriminator shared by store-owned business records."""

    store_code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default=current_store_code,
        server_default=DEFAULT_STORE_CODE,
        index=True,
    )


@event.listens_for(Session, "before_flush")
def _stamp_new_store_records(session: Session, _: Any, __: Any) -> None:
    code = current_store_code()
    for instance in session.new:
        if isinstance(instance, StoreScopedMixin) and not instance.store_code:
            instance.store_code = code


@event.listens_for(Session, "do_orm_execute")
def _isolate_store_orm_operations(state: Any) -> None:
    code = current_store_code()
    if state.is_select:
        state.statement = state.statement.options(
            with_loader_criteria(
                StoreScopedMixin,
                lambda model: model.store_code == code,
                include_aliases=True,
            )
        )
        return
    if not (state.is_update or state.is_delete):
        return
    mapper = state.bind_arguments.get("mapper")
    model = getattr(mapper, "class_", None)
    if isinstance(model, type) and issubclass(model, StoreScopedMixin):
        state.statement = state.statement.where(model.store_code == code)
