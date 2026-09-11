"""Verify reviewed branch merges and exercise atomic writes on an isolated MySQL.

The SQL executor is deliberately reachable only through a synthetic rehearsal
target. Production promotion still needs transaction evidence and a role guard.
No archive-supplied DDL or SQL is executed; foreign-key checks stay enabled.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import hashlib
from itertools import zip_longest
import json
from pathlib import Path
import re
import sqlite3

from blue_branch_capture import UUIDS, identifier
from blue_branch_merge import (
    APPEND_ONLY, CLUSTER, GUARDED, KNOWN_TABLES, VERSION, _key, _snapshot_seal,
    canonical, classify, decode, digest, encode, verify_snapshot,
)


MARKER = "_blue_merge_rehearsal"
RECEIPTS = "_blue_merge_receipts"
RECEIPT_DDL = f"""CREATE TABLE {RECEIPTS} (
    plan_seal CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    resolution_seal CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    result_seal CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL
) ENGINE=InnoDB"""


class MergeRejected(RuntimeError):
    """Safe diagnostic code only: never include a SQL row or database exception."""


class CommitUncertain(MergeRejected):
    """Reopen and look up the transactional receipt; never blind-replay writes."""


class Review:
    def __init__(self, db, header, schemas, plan_seal, resolution_seal):
        self.db, self.header, self.schemas = db, header, schemas
        self.plan_seal, self.resolution_seal = plan_seal, resolution_seal

    def rows(self, table, *, desired):
        if table not in self.schemas:
            raise MergeRejected("unknown-table")
        if not desired:
            return self.db.execute(
                "SELECT key,payload FROM current_branch.rows WHERE table_name=? ORDER BY key", (table,))
        return self.db.execute("""
            WITH keys AS (
                SELECT key FROM current_branch.rows WHERE table_name=?
                UNION SELECT key FROM resolved WHERE table_name=?)
            SELECT k.key,CASE WHEN r.key IS NOT NULL THEN r.payload ELSE c.payload END AS value
            FROM keys k
            LEFT JOIN current_branch.rows c ON c.table_name=? AND c.key=k.key
            LEFT JOIN resolved r ON r.table_name=? AND r.key=k.key
            WHERE CASE WHEN r.key IS NOT NULL THEN r.payload ELSE c.payload END IS NOT NULL
            ORDER BY k.key
        """, (table,) * 4)

    def mutations(self, table, *, deletes):
        condition = "IS NULL" if deletes else "IS NOT NULL"
        return self.db.execute(f"""SELECT key,current,payload FROM resolved
            WHERE table_name=? AND payload {condition} ORDER BY key""", (table,))

    def result_seal(self):
        result = hashlib.sha256()
        for table in sorted(self.schemas):
            for key, payload in self.rows(table, desired=True):
                result.update(canonical([table, key, digest(payload)]).encode() + b"\n")
        return result.hexdigest()


@contextmanager
def open_review(base_path: Path, old_path: Path, current_path: Path, plan_path: Path,
                resolutions: dict | None = None):
    """Hold read transactions and recompute the complete plan from all three inputs.

    Hashes detect damage, not approval. Conflict choices must explicitly reference
    this plan. Security/execution state and history rewrites cannot select old data
    through this generic tool; those need a domain-specific resolution workflow.
    """
    inputs = [verify_snapshot(path) for path in (base_path, old_path, current_path)]
    (base, base_seal), (old, old_seal), (current, current_seal) = inputs
    identity = base["identity"]
    schemas = base["schemas"]
    if (identity.get("role") != "baseline" or type(identity.get("epoch")) is not int
            or identity["epoch"] <= 0 or not schemas or set(schemas) - KNOWN_TABLES
            or {old["identity"].get("node"), current["identity"].get("node")} != {"main", "laptop"}
            or any(item["identity"].get("cluster") != CLUSTER for item, _ in inputs)
            or any(item["schemas"] != schemas for item, _ in inputs)):
        raise MergeRejected("branch-identity-or-schema-mismatch")
    for item in (old, current):
        if (item["identity"].get("role") != "branch"
                or item["identity"].get("baseline_seal") != base_seal
                or item["identity"].get("epoch") != identity["epoch"]
                or item["identity"].get("seed_sha256") != identity.get("seed_sha256")):
            raise MergeRejected("branch-common-baseline-mismatch")
    db = sqlite3.connect(":memory:", uri=True)
    try:
        db.execute("""CREATE TABLE resolved (table_name TEXT,key TEXT,current TEXT,payload TEXT,
            PRIMARY KEY(table_name,key)) WITHOUT ROWID""")
        for alias, path in zip(("baseline", "old_branch", "current_branch", "reviewed"),
                                (base_path, old_path, current_path, plan_path), strict=True):
            db.execute(f"ATTACH DATABASE ? AS {alias}", (path.resolve().as_uri() + "?mode=ro",))
        db.execute("BEGIN")
        for alias, (header, seal) in zip(("baseline", "old_branch", "current_branch"), inputs, strict=True):
            actual = db.execute(f"SELECT header,seal FROM {alias}.metadata WHERE id=1").fetchone()
            if actual != (canonical(header), seal) or _snapshot_seal(db, header, alias) != seal:
                raise MergeRejected("snapshot-changed-before-review")
        metadata = db.execute("SELECT header,seal FROM reviewed.metadata WHERE id=1").fetchone()
        if not metadata or not metadata[1]:
            raise MergeRejected("plan-incomplete")
        submitted, plan_seal = json.loads(metadata[0]), metadata[1]
        header = {"version": VERSION, "kind": "blue-branch-merge-plan", "baseline": base_seal,
                  "old": old_seal, "current": current_seal, "epoch": identity["epoch"]}
        choices = {}
        if resolutions is not None:
            if (set(resolutions) != {"version", "plan_seal", "choices"}
                    or resolutions["version"] != 1 or resolutions["plan_seal"] != plan_seal
                    or not isinstance(resolutions["choices"], list)):
                raise MergeRejected("resolution-plan-mismatch")
            for choice in resolutions["choices"]:
                if (not isinstance(choice, dict) or set(choice) != {"table", "key", "take", "reason"}
                        or choice["take"] not in {"old", "current"}
                        or not isinstance(choice["reason"], str)
                        or not 1 <= len(choice["reason"].strip()) <= 1000
                        or not isinstance(choice["key"], str) or choice["table"] not in schemas):
                    raise MergeRejected("invalid-resolution")
                key = (choice["table"], choice["key"])
                if key in choices:
                    raise MergeRejected("duplicate-resolution")
                choices[key] = choice
        referenced = {fk["referenced_table"] for schema in schemas.values()
                      for fk in schema.get("foreign_keys", [])}
        counts = {"keep": 0, "apply": 0, "conflict": 0}
        computed = hashlib.sha256(canonical(header).encode())
        recorded = iter(db.execute("SELECT * FROM reviewed.changes ORDER BY table_name,key"))
        rows = db.execute("""
            WITH keys AS (SELECT table_name,key FROM baseline.rows
                UNION SELECT table_name,key FROM old_branch.rows
                UNION SELECT table_name,key FROM current_branch.rows)
            SELECT k.table_name,k.key,b.payload,o.payload,c.payload FROM keys k
            LEFT JOIN baseline.rows b USING(table_name,key)
            LEFT JOIN old_branch.rows o USING(table_name,key)
            LEFT JOIN current_branch.rows c USING(table_name,key) ORDER BY k.table_name,k.key
        """)
        for table, key, b, o, c in rows:
            decision, reason = classify(table, b, o, c, referenced=table in referenced)
            counts[decision] += 1
            if decision == "keep":
                continue
            expected = (table, key, decision, reason, b, o, c)
            if next(recorded, None) != expected:
                raise MergeRejected("plan-not-complete-source-comparison")
            computed.update(canonical(expected).encode() + b"\n")
            payload = o
            if decision == "conflict":
                choice = choices.pop((table, key), None)
                if choice is None:
                    raise MergeRejected("unresolved-conflict")
                if choice["take"] == "current":
                    continue
                if table in GUARDED or (table in APPEND_ONLY and b is not None):
                    raise MergeRejected("domain-specific-resolution-required")
            if payload != c:
                db.execute("INSERT INTO resolved VALUES (?,?,?,?)", (table, key, c, payload))
        if next(recorded, None) is not None or choices:
            raise MergeRejected("extra-plan-row-or-resolution")
        summary = {**counts, "conflict_free": counts["conflict"] == 0,
                   "requires_mysql_constraint_validation": True, "mysql_apply_available": False}
        computed.update(canonical(summary).encode())
        header["summary"] = summary
        if submitted != header or computed.hexdigest() != plan_seal:
            raise MergeRejected("plan-seal-or-summary-mismatch")
        resolution_seal = digest(canonical(resolutions or {"version": 1, "plan_seal": plan_seal, "choices": []}))
        yield Review(db, header, schemas, plan_seal, resolution_seal)
    finally:
        db.close()


def _table_order(schemas):
    """Parents first; cycles require a separate migration, not disabled FK checks."""
    remaining = {table: {fk["referenced_table"] for fk in schema.get("foreign_keys", [])}
                 for table, schema in schemas.items()}
    ordered = []
    while remaining:
        ready = sorted(table for table, parents in remaining.items() if not parents)
        if not ready:
            raise MergeRejected("cyclic-or-external-foreign-key")
        for table in ready:
            ordered.append(table)
            del remaining[table]
        for parents in remaining.values():
            parents.difference_update(ready)
    return ordered


def _verify_mysql_schema(cursor, database, schemas):
    cursor.execute("SELECT TABLE_NAME,ENGINE,TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s", (database,))
    inventory = cursor.fetchall()
    if (set(row[0] for row in inventory) != set(schemas) | {MARKER, RECEIPTS}
            or any(row[1:] != ("InnoDB", "BASE TABLE") for row in inventory)):
        raise MergeRejected("mysql-table-inventory-mismatch")
    cursor.execute("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=%s", (database,))
    if cursor.fetchone()[0]:
        raise MergeRejected("trigger-needs-separate-review")
    cursor.execute("SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA=%s", (database,))
    if cursor.fetchone()[0]:
        raise MergeRejected("event-needs-separate-review")
    cursor.execute("""SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE
        WHERE REFERENCED_TABLE_SCHEMA=%s AND TABLE_SCHEMA<>%s""", (database, database))
    if cursor.fetchone()[0]:
        raise MergeRejected("external-inbound-foreign-key")
    for table, schema in schemas.items():
        cursor.execute(f"SHOW CREATE TABLE {identifier(database)}.{identifier(table)}")
        ddl = re.sub(r"\sAUTO_INCREMENT=\d+(?=\s|$)", "", cursor.fetchone()[1])
        cursor.execute("""SELECT COLUMN_NAME,EXTRA FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION""", (database, table))
        columns = cursor.fetchall()
        if (ddl != schema.get("ddl") or [row[0] for row in columns] != schema["columns"]
                or any("VIRTUAL GENERATED" in row[1] or "STORED GENERATED" in row[1] for row in columns)):
            raise MergeRejected("mysql-schema-mismatch-or-generated-column")


def _assert_rows(conn, review, database, *, desired):
    """Compare every row, not just modified keys; detect cascades and unrelated drift."""
    from pymysql.cursors import SSCursor

    # Sorting uses archive canonical keys rather than database collation/order.
    review.db.execute("CREATE TEMP TABLE IF NOT EXISTS actual (key TEXT PRIMARY KEY,payload TEXT) WITHOUT ROWID")
    for table, schema in sorted(review.schemas.items()):
        review.db.execute("DELETE FROM actual")
        with conn.cursor(SSCursor) as cursor:
            columns = ",".join(identifier(column) for column in schema["columns"])
            cursor.execute(f"SELECT {columns} FROM {identifier(database)}.{identifier(table)}")
            for values in cursor:
                row = [encode(value) for value in values]
                review.db.execute("INSERT INTO actual VALUES (?,?)", (_key(schema, row), canonical(row)))
        actual = review.db.execute("SELECT key,payload FROM actual ORDER BY key")
        if any(a != b for a, b in zip_longest(actual, review.rows(table, desired=desired))):
            raise MergeRejected("mysql-final-state-mismatch" if desired else "current-branch-has-drifted")


def _write_rows(cursor, review, database, order):
    for deletes, tables in ((True, list(reversed(order))), (False, order)):
        for table in tables:
            schema = review.schemas[table]
            qualified = f"{identifier(database)}.{identifier(table)}"
            predicate = " AND ".join(f"{identifier(column)}=%s" for column in schema["primary_key"])
            for key, current, payload in review.mutations(table, deletes=deletes):
                keys = tuple(_sql_value(value) for value in json.loads(key))
                if deletes:
                    cursor.execute(f"DELETE FROM {qualified} WHERE {predicate}", keys)
                else:
                    values = tuple(_sql_value(value) for value in json.loads(payload))
                    if current is None:
                        columns = ",".join(identifier(column) for column in schema["columns"])
                        placeholders = ",".join("%s" for _ in values)
                        cursor.execute(f"INSERT INTO {qualified} ({columns}) VALUES ({placeholders})", values)
                    else:
                        assignments = ",".join(f"{identifier(column)}=%s" for column in schema["columns"])
                        cursor.execute(f"UPDATE {qualified} SET {assignments} WHERE {predicate}", values + keys)
                if cursor.rowcount != 1:
                    raise MergeRejected("unexpected-affected-rows")


def _sql_value(encoded):
    value = decode(encoded)
    if isinstance(value, timedelta):
        # PyMySQL's timedelta encoder combines signed days with unsigned seconds,
        # which changes negative fractional TIME values. Bind an exact TIME string.
        total = (value.days * 86400 + value.seconds) * 1000000 + value.microseconds
        seconds, micros = divmod(abs(total), 1000000)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        return f"{'-' if total < 0 else ''}{hours:02d}:{minutes:02d}:{seconds:02d}.{micros:06d}"
    return value


def rehearse(conn, review: Review, *, database: str, server_uuid: str, datadir: Path,
             commit: bool = False) -> dict:
    """Write only a caller-created synthetic MySQL, verified before any DML.

    Uses one connection and one transaction with explicit table locks. The receipt
    commits with the changes; a lost COMMIT response is resolved by the next call's
    receipt lookup. Connection ownership remains with the caller.
    """
    if not re.fullmatch(r"blue_merge_rehearsal_[0-9a-f]{32}", database):
        raise MergeRejected("synthetic-rehearsal-database-required")
    def qualified(table):
        return f"{identifier(database)}.{identifier(table)}"

    locked, commit_started, managed = False, False, False
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT @@server_uuid,@@port,@@datadir,@@server_id")
            actual_uuid, port, actual_dir, server_id = cursor.fetchone()
            if (actual_uuid != server_uuid or actual_uuid in UUIDS.values() or port in {3306, 3307, 13317}
                    or server_id in {101, 102} or Path(actual_dir).resolve() != datadir.resolve()
                    or "TakealotBlue" in str(datadir) or not conn.get_autocommit()):
                raise MergeRejected("synthetic-rehearsal-instance-required")
            cursor.execute(f"SELECT nonce FROM {qualified(MARKER)} WHERE id=1")
            if cursor.fetchone() != (database.removeprefix("blue_merge_rehearsal_"),):
                raise MergeRejected("synthetic-rehearsal-marker-required")
            _verify_mysql_schema(cursor, database, review.schemas)
            order = _table_order(review.schemas)
            managed = True  # Preflight failures must not roll back a caller's transaction.
            cursor.execute("SET SESSION lock_wait_timeout=5")
            cursor.execute("SET SESSION innodb_lock_wait_timeout=5")
            cursor.execute("SET SESSION sql_mode='STRICT_ALL_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION,NO_AUTO_VALUE_ON_ZERO'")
            cursor.execute("SET SESSION foreign_key_checks=1,unique_checks=1,innodb_table_locks=1")
            conn.autocommit(False)
            # START TRANSACTION would release the explicit table locks.
            cursor.execute("LOCK TABLES " + ",".join(qualified(table) + " WRITE"
                for table in sorted(set(review.schemas) | {MARKER, RECEIPTS})))
            locked = True
            _verify_mysql_schema(cursor, database, review.schemas)
            expected_receipt = (review.resolution_seal, review.result_seal())
            cursor.execute(f"SELECT resolution_seal,result_seal FROM {qualified(RECEIPTS)} WHERE plan_seal=%s",
                           (review.plan_seal,))
            receipt = cursor.fetchone()
            if receipt:
                if receipt != expected_receipt:
                    raise MergeRejected("committed-plan-has-different-resolution")
                conn.rollback()
                return {"status": "already-committed", "plan_seal": review.plan_seal,
                        "result_seal": receipt[1], "writes_replayed": False}
            _assert_rows(conn, review, database, desired=False)
            _write_rows(cursor, review, database, order)
            _assert_rows(conn, review, database, desired=True)
            cursor.execute(f"INSERT INTO {qualified(RECEIPTS)} VALUES (%s,%s,%s)",
                           (review.plan_seal, *expected_receipt))
            if commit:
                commit_started = True
                conn.commit()
            else:
                conn.rollback()
            return {"status": "committed" if commit else "validated-and-rolled-back",
                    "plan_seal": review.plan_seal, "resolution_seal": review.resolution_seal,
                    "result_seal": expected_receipt[1], "production": False}
    except BaseException as exc:
        if managed:
            try:
                conn.rollback()
            except Exception:
                pass
        if commit_started:
            raise CommitUncertain("commit-outcome-requires-receipt-lookup") from None
        if isinstance(exc, (MergeRejected, KeyboardInterrupt, SystemExit)):
            raise
        raise MergeRejected("mysql-merge-rejected") from None
    finally:
        # Never let UNLOCK TABLES implicitly commit failed work. If rollback cannot
        # be confirmed, close the connection instead of sending an unlock.
        if managed:
            try:
                conn.rollback()
                if locked:
                    with conn.cursor() as cursor:
                        cursor.execute("UNLOCK TABLES")
                conn.autocommit(True)
            except Exception:
                conn.close()
