"""Offline, version-bound three-way planning for complete BLUE database branches.

These SQLite files are immutable recovery evidence, never an ERP database fallback.
The planner never connects to MySQL, executes SQL from an archive, or applies part
of a conflicted transaction. Keep archives in the restricted BLUE recovery folder.
"""
from __future__ import annotations

import base64
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3


CLUSTER = "takealot-blue-3307-v1"
VERSION = 1

# State with side effects or security semantics needs an explicit decision even
# if only the disconnected old primary changed it. No permission union or quota
# reset can be inferred from an ordinary row diff.
GUARDED = frozenset("""
blue_codex_quota blue_environment_marker competitor_collection_jobs
competitor_listing_operations competitor_listing_operation_items
competitor_worker_heartbeats daily_report_runs erp_refresh_state erp_sessions
erp_stores erp_user_stores erp_users personal_watchlist_library_shares
personal_watchlist_preferences
""".split())
APPEND_ONLY = frozenset("""
anomaly_events blue_codex_quota_audit blue_crawl_delivery_receipts collection_runs
competitor_reviews competitor_snapshots competitor_target_audits
competitor_variant_snapshots daily_inventory_snapshots daily_report_audits
daily_report_deadline_snapshots daily_report_observations daily_report_resolutions
data_quality_events erp_data_revisions google_search_autocomplete_captures
logistics_provider_snapshots logistics_shipment_link_audits offer_snapshots
platform_warehouse_draft_audits product_keyword_snapshots
product_master_import_batches product_master_import_rows
recovery_daily_report_observations_20260727 sales_revenue_revisions
search_autocomplete_snapshots search_ranking_analyses
search_ranking_decision_parameter_confirmations search_ranking_keyword_results
search_ranking_product_facts seller_home_snapshots store_offer_baselines
store_offer_observations
""".split())
ORDINARY = frozenset("""
company_product_costs company_products competitor_link_health
competitor_personal_watchlist competitor_targets daily_product_metrics
daily_sales_metric_states google_search_autocomplete_current logistics_shipment_links
offer_current own_store_follower_tracking own_store_personal_watchlist
personal_watchlist_libraries personal_watchlist_library_items platform_sku_mappings
platform_warehouse_draft_lines platform_warehouse_drafts platform_warehouse_shipments
return_items sale_items search_autocomplete_cache
""".split())
KNOWN_TABLES = GUARDED | APPEND_ONLY | ORDINARY


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def encode(value):
    """Retain types, exact money, binary values and NULL; never stringify all SQL values."""
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, Decimal) and value.is_finite():
        return ["decimal", str(value)]
    if isinstance(value, float) and math.isfinite(value):
        return ["float", value.hex()]
    if isinstance(value, datetime):
        return ["datetime", value.isoformat(timespec="microseconds")]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    if isinstance(value, timedelta):
        return ["timedelta", str((value.days * 86400 + value.seconds) * 1000000 + value.microseconds)]
    if isinstance(value, bytes):
        return ["bytes", base64.b64encode(value).decode("ascii")]
    if isinstance(value, str):
        return ["str", value]
    raise ValueError("unsupported-or-nonfinite-sql-value")


def decode(value):
    tag, *rest = value
    if tag == "null" and not rest:
        return None
    if len(rest) != 1:
        raise ValueError("invalid-encoded-sql-value")
    raw = rest[0]
    converters = {"bool": lambda x: x, "int": int, "decimal": Decimal,
                  "float": float.fromhex, "datetime": datetime.fromisoformat,
                  "date": date.fromisoformat, "timedelta": lambda x: timedelta(microseconds=int(x)),
                  "bytes": lambda x: base64.b64decode(x, validate=True), "str": lambda x: x}
    if tag not in converters:
        raise ValueError("unknown-sql-value-tag")
    decoded = converters[tag](raw)
    if encode(decoded) != value:
        raise ValueError("noncanonical-sql-value")
    return decoded


def exclusive_database(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    db = sqlite3.connect(path, uri=True)
    db.execute("PRAGMA synchronous=FULL")
    return db


def readonly(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _key(schema: dict, row: list) -> str:
    columns = schema["columns"]
    if len(row) != len(columns) or not schema["primary_key"] or len(columns) != len(set(columns)):
        raise ValueError("invalid-row-or-schema")
    values = [row[columns.index(column)] for column in schema["primary_key"]]
    if any(item == ["null"] for item in values):
        raise ValueError("null-primary-key")
    return canonical(values)


class SnapshotWriter:
    """Streaming writer. Missing final seal means an incomplete, unusable snapshot."""
    def __init__(self, path: Path, identity: dict, schemas: dict):
        if (identity.get("cluster") != CLUSTER or identity.get("node") not in {"main", "laptop"}
                or identity.get("role") not in {"baseline", "branch"} or not identity.get("gtid")):
            raise ValueError("snapshot-identity-invalid")
        if not schemas or set(schemas) - KNOWN_TABLES:
            raise ValueError("unreviewed-table-inventory")
        self.db = exclusive_database(path)
        self.schemas = schemas
        self.header = {"version": VERSION, "kind": "blue-branch-snapshot", "identity": identity,
                       "schemas": schemas}
        self.db.executescript("""
            CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK(id=1), header TEXT NOT NULL, seal TEXT);
            CREATE TABLE rows (table_name TEXT NOT NULL, key TEXT NOT NULL, hash TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(table_name,key)) WITHOUT ROWID;
        """)
        self.db.execute("INSERT INTO metadata VALUES (1,?,NULL)", (canonical(self.header),))
        self.db.commit()

    def append(self, table: str, values) -> None:
        row = [encode(value) for value in values]
        payload = canonical(row)
        self.db.execute("INSERT INTO rows VALUES (?,?,?,?)",
                        (table, _key(self.schemas[table], row), digest(payload), payload))

    def checkpoint(self) -> None:
        self.db.commit()  # Still unusable until the final seal is committed.

    def seal(self) -> str:
        seal = _snapshot_seal(self.db, self.header)
        self.db.execute("UPDATE metadata SET seal=? WHERE id=1 AND seal IS NULL", (seal,))
        self.db.commit()
        return seal

    def close(self) -> None:
        self.db.close()


def _snapshot_seal(db, header, alias="main") -> str:
    if alias not in {"main", "baseline", "old_branch", "current_branch"}:
        raise ValueError("invalid-archive-alias")
    result = hashlib.sha256(canonical(header).encode("utf-8"))
    for table, key, row_hash, payload in db.execute(f"SELECT * FROM {alias}.rows ORDER BY table_name,key"):
        schema = header["schemas"].get(table)
        if schema is None:
            raise ValueError("row-table-not-in-manifest")
        row = json.loads(payload)
        for value in row:
            decode(value)
        if canonical(row) != payload or _key(schema, row) != key or digest(payload) != row_hash:
            raise ValueError("snapshot-row-integrity-failed")
        result.update(canonical([table, key, row_hash]).encode("utf-8") + b"\n")
    return result.hexdigest()


def verify_snapshot(path: Path) -> tuple[dict, str]:
    with closing(readonly(path)) as db:
        row = db.execute("SELECT header,seal FROM metadata WHERE id=1").fetchone()
        if not row or not row[1]:
            raise ValueError("snapshot-incomplete")
        header = json.loads(row[0])
        if header.get("version") != VERSION or header.get("kind") != "blue-branch-snapshot":
            raise ValueError("snapshot-format-invalid")
        if row[1] != _snapshot_seal(db, header):
            raise ValueError("snapshot-seal-mismatch")
        return header, row[1]


def classify(table: str, base: str | None, old: str | None, current: str | None,
             *, referenced: bool) -> tuple[str, str]:
    if old == current:
        return "keep", "identical"
    if old == base:
        return "keep", "old-unchanged"
    if current != base:
        return "conflict", "both-branches-changed"
    if table in GUARDED:
        return "conflict", "security-or-execution-state-review"
    if table in APPEND_ONLY and base is not None:
        return "conflict", "immutable-history-changed"
    if old is None and referenced:
        return "conflict", "parent-delete-needs-related-row-review"
    return "apply", "unambiguous-old-change"


def plan(base_path: Path, old_path: Path, current_path: Path, output: Path) -> dict:
    """Write a reviewable, immutable plan. This function performs zero MySQL writes."""
    inputs = [verify_snapshot(path) for path in (base_path, old_path, current_path)]
    (baseline, base_seal), (old, old_seal), (current, current_seal) = inputs
    identity = baseline["identity"]
    if (identity.get("role") != "baseline" or old["identity"].get("node") == current["identity"].get("node")
            or any(item["identity"].get("cluster") != CLUSTER for item, _ in inputs)
            or any(item["schemas"] != baseline["schemas"] for item, _ in inputs)
            or set(baseline["schemas"]) - KNOWN_TABLES):
        raise ValueError("branch-identity-or-schema-mismatch")
    for item in (old, current):
        if (item["identity"].get("role") != "branch" or item["identity"].get("baseline_seal") != base_seal
                or item["identity"].get("epoch") != identity.get("epoch")
                or item["identity"].get("seed_sha256") != identity.get("seed_sha256")):
            raise ValueError("branch-common-baseline-mismatch")
    referenced = {fk["referenced_table"] for schema in baseline["schemas"].values()
                  for fk in schema.get("foreign_keys", [])}
    header = {"version": VERSION, "kind": "blue-branch-merge-plan", "baseline": base_seal,
              "old": old_seal, "current": current_seal, "epoch": identity["epoch"]}
    counts = {"keep": 0, "apply": 0, "conflict": 0}
    with closing(exclusive_database(output)) as db, db:
        db.executescript("""
            CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK(id=1), header TEXT NOT NULL, seal TEXT);
            CREATE TABLE changes (table_name TEXT NOT NULL,key TEXT NOT NULL,decision TEXT NOT NULL,
                reason TEXT NOT NULL,base TEXT,old TEXT,current TEXT,PRIMARY KEY(table_name,key)) WITHOUT ROWID;
        """)
        # Fixed aliases and bound read-only URIs. Never execute SQL from a snapshot.
        for alias, path in zip(("baseline", "old_branch", "current_branch"),
                               (base_path, old_path, current_path), strict=True):
            db.execute(f"ATTACH DATABASE ? AS {alias}", (path.resolve().as_uri() + "?mode=ro",))
        db.execute("BEGIN")
        for alias, (verified, expected_seal) in zip(("baseline", "old_branch", "current_branch"), inputs, strict=True):
            stored = db.execute(f"SELECT header,seal FROM {alias}.metadata WHERE id=1").fetchone()
            if (not stored or stored != (canonical(verified), expected_seal)
                    or _snapshot_seal(db, verified, alias) != expected_seal):
                raise ValueError("snapshot-changed-before-planning")
        rows = db.execute("""
            WITH keys AS (
                SELECT table_name,key FROM baseline.rows UNION SELECT table_name,key FROM old_branch.rows
                UNION SELECT table_name,key FROM current_branch.rows)
            SELECT k.table_name,k.key,b.payload,o.payload,c.payload FROM keys k
            LEFT JOIN baseline.rows b USING(table_name,key)
            LEFT JOIN old_branch.rows o USING(table_name,key)
            LEFT JOIN current_branch.rows c USING(table_name,key) ORDER BY k.table_name,k.key
        """)
        seal = hashlib.sha256(canonical(header).encode("utf-8"))
        for table, key, b, o, c in rows:
            decision, reason = classify(table, b, o, c, referenced=table in referenced)
            counts[decision] += 1
            if decision != "keep":
                record = (table, key, decision, reason, b, o, c)
                db.execute("INSERT INTO changes VALUES (?,?,?,?,?,?,?)", record)
                seal.update(canonical(record).encode("utf-8") + b"\n")
        # Preserve all conflicts and versions. No subset is declared ready when a
        # conflict might belong to the same original multi-row transaction.
        summary = {**counts, "conflict_free": counts["conflict"] == 0,
                   "requires_mysql_constraint_validation": True, "mysql_apply_available": False}
        header["summary"] = summary
        seal.update(canonical(summary).encode("utf-8"))
        db.execute("INSERT INTO metadata VALUES (1,?,?)", (canonical(header), seal.hexdigest()))
    return {**summary, "plan_seal": seal.hexdigest()}
