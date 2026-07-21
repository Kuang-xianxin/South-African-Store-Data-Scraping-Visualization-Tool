"""Schema setup and engine creation for supported database backends."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url

from takealot_ops.storage.models import Base


class DatabaseSettings(Protocol):
    @property
    def database_url(self) -> str: ...


def create_engine_for_settings(settings: DatabaseSettings) -> Engine:
    """Create an engine, applying SQLite's local-operation safety settings."""
    url = make_url(settings.database_url)
    if url.drivername in {"sqlite", "sqlite+pysqlite"} and url.database not in {
        None,
        ":memory:",
    }:
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_url)
    if settings.database_url.startswith("sqlite"):
        _configure_sqlite(engine)
    return engine


def create_schema(engine: Engine) -> None:
    """Create the current schema and apply additive SQLite upgrades."""
    Base.metadata.create_all(engine)
    if engine.dialect.name == "sqlite":
        _add_sqlite_offer_stock_columns(engine)


def _add_sqlite_offer_stock_columns(engine: Engine) -> None:
    columns = {
        "takealot_available_stock": "INTEGER",
        "seller_available_stock": "INTEGER",
        "takealot_stock_in_receiving": "INTEGER",
        "takealot_stock_on_way": "INTEGER",
    }
    with engine.begin() as connection:
        for table_name in ("offer_current", "offer_snapshots"):
            existing = {
                str(row[1])
                for row in connection.exec_driver_sql(
                    f'PRAGMA table_info("{table_name}")'
                ).fetchall()
            }
            for column_name, column_type in columns.items():
                if column_name not in existing:
                    connection.exec_driver_sql(
                        f'ALTER TABLE "{table_name}" ADD COLUMN '
                        f'"{column_name}" {column_type}'
                    )


def _configure_sqlite(engine: Engine) -> None:
    """Register SQLite connection pragmas without leaking them to business code."""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
