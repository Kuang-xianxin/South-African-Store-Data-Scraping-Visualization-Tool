"""Schema setup and engine creation for supported database backends."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, create_engine, event

from takealot_ops.settings import Settings
from takealot_ops.storage.models import Base


def create_engine_for_settings(settings: Settings) -> Engine:
    """Create an engine, applying SQLite's local-operation safety settings."""
    engine = create_engine(settings.database_url)
    if settings.database_url.startswith("sqlite"):
        _configure_sqlite(engine)
    return engine


def create_schema(engine: Engine) -> None:
    """Create the current schema for a new local database."""
    Base.metadata.create_all(engine)


def _configure_sqlite(engine: Engine) -> None:
    """Register SQLite connection pragmas without leaking them to business code."""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
