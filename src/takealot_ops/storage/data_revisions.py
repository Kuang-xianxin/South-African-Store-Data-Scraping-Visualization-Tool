"""Coalesce changed domains into the transaction that commits their data.

SQLAlchemy's commit event runs before DBAPI commit. Revision upserts therefore
commit or roll back with the business writes, across ERP and CLI processes.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, event
from sqlalchemy.engine import Connection
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from takealot_ops.storage.models import Base, ErpDataRevision
from takealot_ops.storage.store_context import current_store_code


TABLE_TOPICS = {
    "seller_home_snapshots": "home",
    **dict.fromkeys((
        "offer_current", "offer_snapshots", "store_offer_baselines",
        "store_offer_observations", "sale_items", "daily_product_metrics",
        "daily_sales_metric_states", "sales_revenue_revisions", "anomaly_events",
        "collection_runs", "daily_report_runs", "daily_inventory_snapshots",
        "daily_report_observations", "daily_report_resolutions", "daily_report_audits",
        "daily_report_deadline_snapshots",
    ), "store"),
    "return_items": "returns",
    **dict.fromkeys((
        "competitor_targets", "competitor_target_audits", "competitor_link_health",
        "competitor_snapshots", "competitor_variant_snapshots", "competitor_reviews",
        "own_store_follower_tracking", "competitor_listing_operations",
        "competitor_listing_operation_items",
    ), "competitors"),
    **dict.fromkeys((
        "competitor_personal_watchlist", "own_store_personal_watchlist",
        "personal_watchlist_libraries", "personal_watchlist_library_shares",
        "personal_watchlist_library_items", "personal_watchlist_preferences",
    ), "watchlists"),
    **dict.fromkeys((
        "search_ranking_analyses", "search_ranking_keyword_results",
        "search_ranking_product_facts", "search_ranking_decision_parameter_confirmations",
        "google_search_autocomplete_captures", "google_search_autocomplete_current",
        "search_autocomplete_cache", "search_autocomplete_snapshots",
    ), "search"),
    **dict.fromkeys((
        "logistics_shipment_links", "logistics_shipment_link_audits",
        "logistics_provider_snapshots", "platform_warehouse_drafts",
        "platform_warehouse_draft_lines", "platform_warehouse_draft_audits",
        "platform_warehouse_shipments",
    ), "logistics"),
    **dict.fromkeys((
        "company_products", "platform_sku_mappings", "company_product_costs",
        "product_master_import_batches", "product_master_import_rows",
    ), "master"),
    **dict.fromkeys(("erp_users", "erp_stores", "erp_user_stores"), "users"),
}
_PENDING = "erp_pending_data_revisions"
_SAVEPOINTS = "erp_revision_savepoints"
_WRITE_TABLE = re.compile(
    r"^\s*(?:INSERT\s+(?:IGNORE\s+)?INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)"
    r"\s+[`\"\[]?([a-zA-Z_][a-zA-Z_0-9]*)",
    re.IGNORECASE,
)


def install_data_revision_tracking(engine: Engine) -> None:
    """Register once on each application engine; reads never write a marker."""
    if event.contains(engine, "after_cursor_execute", _record_write):
        return
    event.listen(engine, "after_cursor_execute", _record_write)
    event.listen(engine, "commit", _write_revisions)
    event.listen(engine, "rollback", _discard_revisions)
    event.listen(engine, "savepoint", _savepoint)
    event.listen(engine, "rollback_savepoint", _rollback_savepoint)
    event.listen(engine, "release_savepoint", _release_savepoint)


def _record_write(
    connection: Connection, cursor: Any, statement: str, parameters: Any,
    context: Any, executemany: bool,
) -> None:
    compiled = getattr(context, "compiled", None)
    sql = getattr(compiled, "statement", None)
    table = getattr(sql, "table", None)
    is_write = any(getattr(context, name, False) for name in ("isinsert", "isupdate", "isdelete"))
    if is_write and table is not None:
        name = table.name
    else:
        match = _WRITE_TABLE.match(statement)
        if match is None:
            return
        name = match.group(1).casefold()
    topic = TABLE_TOPICS.get(name)
    if topic is None or cursor.rowcount == 0:
        return
    metadata_table = Base.metadata.tables.get(name)
    scoped = metadata_table is not None and "store_code" in metadata_table.c
    scopes = {"*"}
    if scoped:
        scopes = set()
        for values in getattr(context, "compiled_parameters", ()) or ():
            for key, value in values.items():
                if (key == "store_code" or key.startswith("store_code_")) and isinstance(value, str):
                    scopes.add(value)
        if not scopes:
            scopes = {current_store_code()}
    pending = connection.info.setdefault(_PENDING, set())
    pending.update((scope, topic) for scope in scopes)
    if name == "offer_current":
        # True competitors exclude PLIDs owned by ANY connected store.
        pending.add(("*", "own-identities"))


def _write_revisions(connection: Connection) -> None:
    pending = connection.info.pop(_PENDING, set())
    connection.info.pop(_SAVEPOINTS, None)
    if not pending:
        return
    # Compatibility with a legacy/partial schema during an additive upgrade.
    # Never create schema from an ordinary data transaction or a read request.
    if not connection.info.get("erp_revision_table_ready"):
        if not connection.dialect.has_table(connection, ErpDataRevision.__tablename__):
            return
        connection.info["erp_revision_table_ready"] = True
    rows = [dict(scope=scope, topic=topic, revision=uuid4().hex) for scope, topic in sorted(pending)]
    table = Base.metadata.tables[ErpDataRevision.__tablename__]
    if connection.dialect.name == "mysql":
        mysql_statement = mysql_insert(table)
        connection.execute(mysql_statement.on_duplicate_key_update(
            revision=mysql_statement.inserted.revision,
        ), rows)
    else:
        sqlite_statement = sqlite_insert(table)
        connection.execute(sqlite_statement.on_conflict_do_update(
            index_elements=[table.c.scope, table.c.topic],
            set_={"revision": sqlite_statement.excluded.revision},
        ), rows)


def _discard_revisions(connection: Connection) -> None:
    connection.info.pop(_PENDING, None)
    connection.info.pop(_SAVEPOINTS, None)


def _savepoint(connection: Connection, name: str | None) -> None:
    connection.info.setdefault(_SAVEPOINTS, []).append(set(connection.info.get(_PENDING, set())))


def _rollback_savepoint(connection: Connection, name: str, context: Any) -> None:
    stack = connection.info.get(_SAVEPOINTS, [])
    if stack:
        connection.info[_PENDING] = stack.pop()


def _release_savepoint(connection: Connection, name: str, context: Any) -> None:
    stack = connection.info.get(_SAVEPOINTS, [])
    if stack:
        stack.pop()
