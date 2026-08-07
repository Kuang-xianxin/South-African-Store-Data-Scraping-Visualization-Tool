"""Schema setup and engine creation for supported database backends."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, create_engine, event, insert, inspect, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from takealot_ops.storage.models import (
    Base,
    ErpStore,
    ErpUser,
    OfferCurrent,
    StoreOfferBaseline,
    StoreOfferObservation,
)
from takealot_ops.storage.store_context import store_scope


class DatabaseSettings(Protocol):
    @property
    def database_url(self) -> str: ...


class StoreSettings(Protocol):
    @property
    def code(self) -> str: ...

    @property
    def display_name(self) -> str: ...


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
    _add_store_scope_columns_and_keys(engine)
    _add_offer_created_at_columns(engine)
    _add_erp_user_permissions_column(engine)
    _add_erp_user_store_access_column(engine)
    _ensure_default_erp_store(engine)
    _add_competitor_target_group_column(engine)
    _add_competitor_variant_observation_columns(engine)
    _add_platform_warehouse_upstream_columns(engine)
    if engine.dialect.name == "sqlite":
        _add_sqlite_offer_stock_columns(engine)
    _seed_store_offer_baselines(engine)
    _seed_store_offer_observations(engine)


def sync_configured_erp_stores(
    engine: Engine,
    stores: Sequence[StoreSettings],
) -> None:
    """Register configured credentials as connected stores and widen administrators."""
    now = datetime.utcnow()
    with Session(engine) as session, session.begin():
        existing = {
            store.code: store
            for store in session.scalars(select(ErpStore)).all()
        }
        configured_codes: set[str] = set()
        for configured in stores:
            configured_codes.add(configured.code)
            store = existing.get(configured.code)
            if store is None:
                session.add(
                    ErpStore(
                        code=configured.code,
                        display_name=configured.display_name,
                        active=True,
                        data_connected=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
                continue
            store.display_name = configured.display_name
            store.active = True
            store.data_connected = True
            store.updated_at = now
        for store in existing.values():
            if store.code not in configured_codes and store.code != "current":
                store.data_connected = False
                store.updated_at = now
        for user in session.scalars(select(ErpUser).where(ErpUser.role == "admin")):
            user.store_access_all = True


_STORE_SCOPED_TABLES = (
    "collection_runs",
    "offer_current",
    "offer_snapshots",
    "search_ranking_analyses",
    "search_ranking_keyword_results",
    "store_offer_baselines",
    "store_offer_observations",
    "sale_items",
    "return_items",
    "daily_product_metrics",
    "anomaly_events",
    "data_quality_events",
    "logistics_provider_snapshots",
    "platform_warehouse_drafts",
    "platform_warehouse_draft_lines",
    "platform_warehouse_draft_audits",
    "platform_warehouse_shipments",
    "erp_refresh_state",
    "daily_report_runs",
    "daily_inventory_snapshots",
    "daily_report_observations",
    "daily_report_resolutions",
    "daily_report_audits",
    "daily_report_deadline_snapshots",
)

_STORE_UNIQUE_UPGRADES = {
    "offer_snapshots": (
        ("snapshot_date", "offer_id"),
        ("store_code", "snapshot_date", "offer_id"),
        "uq_offer_snapshots_store_date_offer",
    ),
    "store_offer_baselines": (
        ("display_date", "offer_id"),
        ("store_code", "display_date", "offer_id"),
        "uq_store_offer_baselines_store_date_offer",
    ),
    "store_offer_observations": (
        ("captured_at", "offer_id"),
        ("store_code", "captured_at", "offer_id"),
        "uq_store_offer_observations_store_time_offer",
    ),
    "daily_product_metrics": (
        ("metric_date", "offer_id"),
        ("store_code", "metric_date", "offer_id"),
        "uq_daily_product_metrics_store_date_offer",
    ),
    "anomaly_events": (
        ("event_date", "offer_id", "anomaly_type"),
        ("store_code", "event_date", "offer_id", "anomaly_type"),
        "uq_anomaly_events_store_date_offer_type",
    ),
    "daily_inventory_snapshots": (
        ("inventory_date", "offer_id"),
        ("store_code", "inventory_date", "offer_id"),
        "uq_daily_inventory_store_date_offer",
    ),
    "daily_report_observations": (
        ("run_id", "offer_id"),
        ("store_code", "run_id", "offer_id"),
        "uq_daily_report_observation_store_run_offer",
    ),
    "daily_report_resolutions": (
        ("business_date", "offer_id"),
        ("store_code", "business_date", "offer_id"),
        "uq_daily_report_resolution_store_date_offer",
    ),
}

_STORE_COMPOSITE_PRIMARY_KEYS = {
    "logistics_provider_snapshots": ("store_code", "provider"),
    "erp_refresh_state": ("store_code", "action_key"),
    "daily_report_deadline_snapshots": ("store_code", "business_date"),
}


def _add_store_scope_columns_and_keys(engine: Engine) -> None:
    """Backfill the original dataset as ``current`` and enable per-store records."""
    with engine.begin() as connection:
        schema = inspect(connection)
        preparer = connection.dialect.identifier_preparer
        for table_name in _STORE_SCOPED_TABLES:
            if not schema.has_table(table_name):
                continue
            columns = {str(column["name"]) for column in schema.get_columns(table_name)}
            table = preparer.quote(table_name)
            store_column = preparer.quote("store_code")
            if "store_code" not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {store_column} "
                    "VARCHAR(64) NOT NULL DEFAULT 'current'"
                )
            indexes = inspect(connection).get_indexes(table_name)
            if not any(index.get("column_names") == ["store_code"] for index in indexes):
                index_name = preparer.quote(f"ix_{table_name}_store_code")
                connection.exec_driver_sql(
                    f"CREATE INDEX {index_name} ON {table} ({store_column})"
                )

        if engine.dialect.name != "mysql":
            return
        for table_name, (old_columns, new_columns, new_name) in _STORE_UNIQUE_UPGRADES.items():
            _replace_mysql_unique_constraint(
                connection,
                table_name=table_name,
                old_columns=old_columns,
                new_columns=new_columns,
                new_name=new_name,
            )
        for table_name, primary_columns in _STORE_COMPOSITE_PRIMARY_KEYS.items():
            primary_key = inspect(connection).get_pk_constraint(table_name)
            if tuple(primary_key.get("constrained_columns") or ()) == primary_columns:
                continue
            table = preparer.quote(table_name)
            quoted_columns = ", ".join(
                preparer.quote(column) for column in primary_columns
            )
            connection.exec_driver_sql(
                f"ALTER TABLE {table} DROP PRIMARY KEY, ADD PRIMARY KEY ({quoted_columns})"
            )


def _replace_mysql_unique_constraint(
    connection: Any,
    *,
    table_name: str,
    old_columns: tuple[str, ...],
    new_columns: tuple[str, ...],
    new_name: str,
) -> None:
    schema = inspect(connection)
    constraints = schema.get_unique_constraints(table_name)
    if any(tuple(item.get("column_names") or ()) == new_columns for item in constraints):
        return
    preparer = connection.dialect.identifier_preparer
    table = preparer.quote(table_name)
    for constraint in constraints:
        if tuple(constraint.get("column_names") or ()) != old_columns:
            continue
        old_name = str(constraint.get("name") or "")
        if old_name:
            connection.exec_driver_sql(
                f"ALTER TABLE {table} DROP INDEX {preparer.quote(old_name)}"
            )
        break
    quoted_columns = ", ".join(preparer.quote(column) for column in new_columns)
    connection.exec_driver_sql(
        f"ALTER TABLE {table} ADD CONSTRAINT {preparer.quote(new_name)} "
        f"UNIQUE ({quoted_columns})"
    )


def _seed_store_offer_baselines(engine: Engine) -> None:
    """Seed the feature's first retained baseline from the current Seller API state."""
    display_timezone = ZoneInfo("Asia/Shanghai")
    with Session(engine) as session, session.begin():
        if session.scalar(select(StoreOfferBaseline.id).limit(1)) is not None:
            return
        for offer in session.scalars(select(OfferCurrent)):
            productline_id = str(offer.productline_id or "").strip()
            if not productline_id:
                continue
            captured_at = offer.captured_at
            if captured_at.tzinfo is None:
                captured_at = captured_at.replace(tzinfo=UTC)
            display_date = captured_at.astimezone(display_timezone).date()
            exists = session.scalar(
                select(StoreOfferBaseline.id).where(
                    StoreOfferBaseline.display_date == display_date,
                    StoreOfferBaseline.offer_id == offer.offer_id,
                )
            )
            if exists is not None:
                continue
            session.add(
                StoreOfferBaseline(
                    display_date=display_date,
                    offer_id=offer.offer_id,
                    productline_id=productline_id,
                    sku=offer.sku,
                    title=offer.title,
                    image_url=offer.image_url,
                    selling_price=offer.selling_price,
                    status=offer.status,
                    total_stock=offer.total_stock,
                    takealot_available_stock=offer.takealot_available_stock,
                    seller_available_stock=offer.seller_available_stock,
                    captured_at=captured_at,
                )
            )


def _seed_store_offer_observations(engine: Engine) -> None:
    """Seed retained baselines and the latest current Seller API state."""
    display_timezone = ZoneInfo("Asia/Shanghai")
    with Session(engine) as session, session.begin():
        store_codes = list(
            session.scalars(
                select(ErpStore.code)
                .where(ErpStore.active.is_(True), ErpStore.data_connected.is_(True))
                .order_by(ErpStore.code)
            )
        ) or ["current"]
        for store_code in store_codes:
            with store_scope(store_code):
                if session.scalar(select(StoreOfferObservation.id).limit(1)) is not None:
                    continue
                for baseline in session.scalars(select(StoreOfferBaseline)):
                    exists = session.scalar(
                        select(StoreOfferObservation.id).where(
                            StoreOfferObservation.captured_at == baseline.captured_at,
                            StoreOfferObservation.offer_id == baseline.offer_id,
                        )
                    )
                    if exists is not None:
                        continue
                    session.add(
                        StoreOfferObservation(
                            store_code=store_code,
                            display_date=baseline.display_date,
                            offer_id=baseline.offer_id,
                            productline_id=baseline.productline_id,
                            sku=baseline.sku,
                            title=baseline.title,
                            image_url=baseline.image_url,
                            selling_price=baseline.selling_price,
                            status=baseline.status,
                            total_stock=baseline.total_stock,
                            takealot_available_stock=baseline.takealot_available_stock,
                            seller_available_stock=baseline.seller_available_stock,
                            captured_at=baseline.captured_at,
                        )
                    )
                for offer in session.scalars(select(OfferCurrent)):
                    productline_id = str(offer.productline_id or "").strip()
                    if not productline_id:
                        continue
                    captured_at = offer.captured_at
                    if captured_at.tzinfo is None:
                        captured_at = captured_at.replace(tzinfo=UTC)
                    exists = session.scalar(
                        select(StoreOfferObservation.id).where(
                            StoreOfferObservation.captured_at == captured_at,
                            StoreOfferObservation.offer_id == offer.offer_id,
                        )
                    )
                    if exists is not None:
                        continue
                    session.add(
                        StoreOfferObservation(
                            store_code=store_code,
                            display_date=captured_at.astimezone(display_timezone).date(),
                            offer_id=offer.offer_id,
                            productline_id=productline_id,
                            sku=offer.sku,
                            title=offer.title,
                            image_url=offer.image_url,
                            selling_price=offer.selling_price,
                            status=offer.status,
                            total_stock=offer.total_stock,
                            takealot_available_stock=offer.takealot_available_stock,
                            seller_available_stock=offer.seller_available_stock,
                            captured_at=captured_at,
                        )
                    )


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


def _add_platform_warehouse_upstream_columns(engine: Engine) -> None:
    """Upgrade retained local drafts for the guarded Seller Portal workflow."""
    table_name = "platform_warehouse_drafts"
    with engine.begin() as connection:
        schema = inspect(connection)
        if not schema.has_table(table_name):
            return
        existing = {str(column["name"]) for column in schema.get_columns(table_name)}
        preparer = connection.dialect.identifier_preparer
        table = preparer.quote(table_name)
        json_type = "JSON" if engine.dialect.name == "mysql" else "JSON"
        additions = {
            "client_request_id": "VARCHAR(36) NULL",
            "upstream_mode": "VARCHAR(30) NOT NULL DEFAULT 'local_only'",
            "review_task_id": "INTEGER NULL",
            "review_payload": f"{json_type} NULL",
            "review_payload_hash": "VARCHAR(64) NULL",
            "review_approval_hash": "VARCHAR(64) NULL",
            "reviewed_at": "DATETIME NULL",
            "review_expires_at": "DATETIME NULL",
            "create_task_id": "INTEGER NULL",
            "upstream_result": f"{json_type} NULL",
            "last_error": "TEXT NULL",
        }
        for name, column_type in additions.items():
            if name in existing:
                continue
            column = preparer.quote(name)
            connection.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
            )
        indexes = {str(index["name"]) for index in inspect(connection).get_indexes(table_name)}
        index_name = "uq_platform_warehouse_draft_store_request"
        if index_name not in indexes:
            quoted_index = preparer.quote(index_name)
            store_column = preparer.quote("store_code")
            request_column = preparer.quote("client_request_id")
            connection.exec_driver_sql(
                f"CREATE UNIQUE INDEX {quoted_index} ON {table} "
                f"({store_column}, {request_column})"
            )


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


def _add_competitor_target_group_column(engine: Engine) -> None:
    """Group an original target and its crawlable public offer targets."""
    with engine.begin() as connection:
        if not inspect(connection).has_table("competitor_targets"):
            return
        existing = {
            str(column["name"])
            for column in inspect(connection).get_columns("competitor_targets")
        }
        preparer = connection.dialect.identifier_preparer
        table = preparer.quote("competitor_targets")
        group_column = preparer.quote("offer_group_plid")
        plid_column = preparer.quote("plid")
        if "offer_group_plid" not in existing:
            connection.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN {group_column} VARCHAR(30) NULL"
            )
        connection.exec_driver_sql(
            f"UPDATE {table} SET {group_column} = {plid_column} "
            f"WHERE {group_column} IS NULL OR {group_column} = ''"
        )
        existing_indexes = inspect(connection).get_indexes("competitor_targets")
        if not any(
            index.get("column_names") == ["offer_group_plid"]
            for index in existing_indexes
        ):
            index_name = preparer.quote("ix_competitor_targets_offer_group_plid")
            connection.exec_driver_sql(
                f"CREATE INDEX {index_name} ON {table} ({group_column})"
            )


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
