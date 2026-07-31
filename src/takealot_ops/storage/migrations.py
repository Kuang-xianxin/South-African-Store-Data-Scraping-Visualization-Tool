"""Schema setup and engine creation for supported database backends."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Engine, create_engine, event, insert, inspect, select, update
from sqlalchemy.engine import make_url

from takealot_ops.storage.models import Base, ErpStore


class DatabaseSettings(Protocol):
    @property
    def database_url(self) -> str: ...


def create_engine_for_settings(settings: DatabaseSettings) -> Engine:
    """Create a writable engine for the configured synchronous database."""
    return create_engine_for_database_url(settings.database_url)


def create_engine_for_database_url(database_url: str) -> Engine:
    """Create a writable engine with backend-specific reliability settings."""
    url = make_url(database_url)
    supported = {"sqlite", "sqlite+pysqlite", "mysql+pymysql"}
    if url.drivername not in supported:
        raise ValueError("database must use sqlite+pysqlite or mysql+pymysql synchronous driver")
    if url.drivername in {"sqlite", "sqlite+pysqlite"} and url.database not in {
        None,
        ":memory:",
    }:
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {}
    if url.get_backend_name() == "mysql":
        options.update(pool_pre_ping=True, pool_recycle=1800)
    engine = create_engine(database_url, **options)
    if url.get_backend_name() == "sqlite":
        _configure_sqlite(engine)
    return engine


def create_read_only_engine(database_url: str) -> Engine:
    """Create a dashboard engine that rejects writes at the database session level."""
    engine = create_engine_for_database_url(database_url)
    backend = make_url(database_url).get_backend_name()

    @event.listens_for(engine, "connect")
    def _set_read_only(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        if backend == "sqlite":
            cursor.execute("PRAGMA query_only=ON")
        elif backend == "mysql":
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
        else:
            cursor.close()
            raise ValueError(f"unsupported database backend: {backend}")
        cursor.close()

    return engine


def create_schema(engine: Engine) -> None:
    """Create the current schema and apply retained in-place upgrades."""
    Base.metadata.create_all(engine)
    _add_offer_created_at_columns(engine)
    _add_erp_user_permissions_column(engine)
    _add_erp_user_store_access_column(engine)
    _ensure_default_erp_store(engine)
    _add_competitor_variant_observation_columns(engine)
    if engine.dialect.name == "sqlite":
        _add_sqlite_offer_stock_columns(engine)


def _add_offer_created_at_columns(engine: Engine) -> None:
    """Add the documented platform listing timestamp to existing offer tables."""
    with engine.begin() as connection:
        preparer = connection.dialect.identifier_preparer
        for table_name in ("offer_current", "offer_snapshots"):
            existing = {
                str(column["name"]) for column in inspect(connection).get_columns(table_name)
            }
            if "created_at" not in existing:
                table = preparer.quote(table_name)
                column = preparer.quote("created_at")
                connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} DATETIME NULL")


def _add_erp_user_permissions_column(engine: Engine) -> None:
    """Add account-level permissions without changing legacy role defaults."""
    with engine.begin() as connection:
        if not inspect(connection).has_table("erp_users"):
            return
        existing = {str(column["name"]) for column in inspect(connection).get_columns("erp_users")}
        if "permissions_json" in existing:
            return
        preparer = connection.dialect.identifier_preparer
        table = preparer.quote("erp_users")
        column = preparer.quote("permissions_json")
        connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NULL")


def _add_erp_user_store_access_column(engine: Engine) -> None:
    """Preserve existing accounts as all-store users during the additive upgrade."""
    with engine.begin() as connection:
        if not inspect(connection).has_table("erp_users"):
            return
        existing = {
            str(column["name"])
            for column in inspect(connection).get_columns("erp_users")
        }
        if "store_access_all" in existing:
            return
        preparer = connection.dialect.identifier_preparer
        table = preparer.quote("erp_users")
        column = preparer.quote("store_access_all")
        connection.exec_driver_sql(
            f"ALTER TABLE {table} ADD COLUMN {column} "
            "BOOLEAN NOT NULL DEFAULT 1"
        )


def _ensure_default_erp_store(engine: Engine) -> None:
    """Register the current single-store dataset without inventing future stores."""
    with engine.begin() as connection:
        if not inspect(connection).has_table("erp_stores"):
            return
        connected_store_id = connection.scalar(
            select(ErpStore.id)
            .where(ErpStore.data_connected.is_(True))
            .limit(1)
        )
        if connected_store_id is not None:
            return
        now = datetime.utcnow()
        current_store_id = connection.scalar(
            select(ErpStore.id).where(ErpStore.code == "current").limit(1)
        )
        if current_store_id is not None:
            connection.execute(
                update(ErpStore)
                .where(ErpStore.id == current_store_id)
                .values(
                    active=True,
                    data_connected=True,
                    updated_at=now,
                )
            )
            return
        connection.execute(
            insert(ErpStore).values(
                code="current",
                display_name="当前店铺",
                active=True,
                data_connected=True,
                created_at=now,
                updated_at=now,
            )
        )


def _add_competitor_variant_observation_columns(engine: Engine) -> None:
    """Add retained per-variant image and customer-limit evidence."""
    with engine.begin() as connection:
        if not inspect(connection).has_table("competitor_variant_snapshots"):
            return
        existing = {
            str(column["name"])
            for column in inspect(connection).get_columns("competitor_variant_snapshots")
        }
        preparer = connection.dialect.identifier_preparer
        table = preparer.quote("competitor_variant_snapshots")
        columns = {
            "image_url": "TEXT NULL",
            "customer_purchase_limit": "INTEGER NULL",
        }
        for column_name, column_type in columns.items():
            if column_name in existing:
                continue
            column = preparer.quote(column_name)
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


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
                        f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {column_type}'
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
