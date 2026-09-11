"""Capture a stopped, read-only BLUE branch without changing any MySQL role.

Requires both read-only switches, zero other transactions and stopped replication.
It deliberately cannot stop replication, promote a server, or merge business data.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import re

from blue_branch_merge import CLUSTER, KNOWN_TABLES, SnapshotWriter, plan, verify_snapshot


ROOT = Path("D:/TakealotBlue")
DATABASE = "takealot_ops"
UUIDS = {"main": "49de5abf-a8cc-11f1-9fc1-00e2699c0656", "laptop": "fa1344dd-a8cd-11f1-b652-088fc3fac162"}
SEED = "44c951b6497b71dceb39fb701f9c1f87737b5fe07abdccaf904867da5175ae8d"


def identifier(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
        raise ValueError("unsupported-sql-identifier")
    return "`" + name + "`"


def archive_path(path: Path, *, create_parent=False) -> Path:
    allowed = ROOT / "state/branches"
    target = path.resolve()
    if not target.is_relative_to(allowed.resolve()) or target.suffix != ".sqlite3":
        raise ValueError("archive-must-be-in-protected-blue-branch-directory")
    for item in (path, *path.parents):
        if item == ROOT.parent:
            break
        if item.is_symlink() or (item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("archive-path-must-not-traverse-links")
    if create_parent:
        target.parent.mkdir(parents=True, exist_ok=True)
    return target


def facts(conn, node: str) -> dict:
    with conn.cursor() as cursor:
        cursor.execute("SELECT @@server_uuid,@@global.gtid_executed,@@global.read_only,@@global.super_read_only")
        server_uuid, gtid, read_only, super_read_only = cursor.fetchone()
        cursor.execute("SELECT seed_sha256 FROM takealot_ops.blue_environment_marker WHERE id=1")
        if server_uuid != UUIDS[node] or cursor.fetchone()[0] != SEED:
            raise ValueError("blue-branch-uuid-or-seed-mismatch")
        cursor.execute("SELECT COUNT(*) FROM information_schema.innodb_trx WHERE trx_mysql_thread_id<>CONNECTION_ID()")
        transactions = cursor.fetchone()[0]
        cursor.execute("SHOW REPLICA STATUS")
        row = cursor.fetchone()
        replication = dict(zip([c[0] for c in cursor.description], row, strict=True)) if row else {}
    reasons = []
    if (read_only, super_read_only) != (1, 1):
        reasons.append("both-read-only-switches-required")
    if transactions:
        reasons.append("other-transactions-not-drained")
    if replication and (replication.get("Replica_IO_Running"), replication.get("Replica_SQL_Running")) != ("No", "No"):
        reasons.append("replication-must-already-be-stopped")
    return {"node": node, "server_uuid": server_uuid, "seed_sha256": SEED, "gtid": gtid,
            "capture_ready": not reasons, "reasons": reasons}


def schemas(conn) -> dict:
    with conn.cursor() as cursor:
        cursor.execute("SELECT TABLE_NAME,ENGINE,TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s ORDER BY TABLE_NAME", (DATABASE,))
        tables = cursor.fetchall()
        if not tables or any(engine != "InnoDB" or kind != "BASE TABLE" for _, engine, kind in tables):
            raise ValueError("unsupported-engine-or-view-in-branch")
        if {table for table, _, _ in tables} - KNOWN_TABLES:
            raise ValueError("unreviewed-table-inventory")
        result = {}
        for table, _, _ in tables:
            cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (DATABASE, table))
            columns = [row[0] for row in cursor.fetchall()]
            cursor.execute("SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND CONSTRAINT_NAME='PRIMARY' ORDER BY ORDINAL_POSITION", (DATABASE, table))
            key = [row[0] for row in cursor.fetchall()]
            if not key:
                raise ValueError("table-without-primary-key")
            cursor.execute(f"SHOW CREATE TABLE {identifier(DATABASE)}.{identifier(table)}")
            # The next generated ID is row state, not a DDL difference between branches.
            ddl = re.sub(r"\sAUTO_INCREMENT=\d+(?=\s|$)", "", cursor.fetchone()[1])
            cursor.execute("""SELECT k.CONSTRAINT_NAME,k.COLUMN_NAME,k.REFERENCED_TABLE_SCHEMA,
                k.REFERENCED_TABLE_NAME,k.REFERENCED_COLUMN_NAME,r.DELETE_RULE,r.UPDATE_RULE
                FROM information_schema.KEY_COLUMN_USAGE k JOIN information_schema.REFERENTIAL_CONSTRAINTS r
                ON k.CONSTRAINT_SCHEMA=r.CONSTRAINT_SCHEMA AND k.CONSTRAINT_NAME=r.CONSTRAINT_NAME AND k.TABLE_NAME=r.TABLE_NAME
                WHERE k.TABLE_SCHEMA=%s AND k.TABLE_NAME=%s AND k.REFERENCED_TABLE_NAME IS NOT NULL
                ORDER BY k.CONSTRAINT_NAME,k.ORDINAL_POSITION""", (DATABASE, table))
            foreign_keys = []
            for name, column, ref_schema, ref_table, ref_column, delete_rule, update_rule in cursor.fetchall():
                if ref_schema != DATABASE:
                    raise ValueError("cross-schema-foreign-key")
                foreign_keys.append({"name": name, "column": column, "referenced_table": ref_table,
                    "referenced_column": ref_column, "delete_rule": delete_rule, "update_rule": update_rule})
            result[table] = {"columns": columns, "primary_key": key, "ddl": ddl, "foreign_keys": foreign_keys}
    return result


def capture(path: Path, *, epoch: int, baseline: Path | None = None) -> dict:
    import blue_node
    from pymysql.cursors import SSCursor

    if type(epoch) is not int or epoch <= 0:
        raise ValueError("positive-failover-epoch-required")
    node = blue_node.config()["node"]
    # blue_node.connection verifies hostname, server_id, port AND data directory.
    with closing(blue_node.connection()) as conn:
        before = facts(conn, node)
        if not before["capture_ready"]:
            raise ValueError("branch-not-frozen")
        schema = schemas(conn)
        identity = {"cluster": CLUSTER, "epoch": epoch, "role": "branch" if baseline else "baseline",
                    **{key: before[key] for key in ("node", "server_uuid", "seed_sha256", "gtid")}}
        if baseline:
            base_header, base_seal = verify_snapshot(archive_path(baseline))
            if (base_header["identity"].get("role") != "baseline" or base_header["identity"].get("epoch") != epoch
                    or base_header["identity"].get("cluster") != CLUSTER or base_header["schemas"] != schema):
                raise ValueError("baseline-epoch-or-schema-mismatch")
            with conn.cursor() as cursor:
                cursor.execute("SELECT GTID_SUBSET(%s,@@global.gtid_executed)", (base_header["identity"]["gtid"],))
                if cursor.fetchone()[0] != 1:
                    raise ValueError("branch-does-not-contain-baseline-gtid")
            identity["baseline_seal"] = base_seal
        elif node != "laptop":
            raise ValueError("takeover-baseline-must-be-captured-on-laptop")
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        writer = SnapshotWriter(archive_path(path, create_parent=True), identity, schema)
        count = 0
        try:
            for table, definition in schema.items():
                with conn.cursor(SSCursor) as cursor:
                    columns = ",".join(identifier(column) for column in definition["columns"])
                    cursor.execute(f"SELECT {columns} FROM {identifier(DATABASE)}.{identifier(table)}")
                    while rows := cursor.fetchmany(500):
                        for row in rows:
                            writer.append(table, row)
                        writer.checkpoint()
                        count += len(rows)
            after = facts(conn, node)
            if after != before or schemas(conn) != schema:
                raise ValueError("branch-changed-during-capture")
            seal = writer.seal()
        finally:
            writer.close()
            conn.rollback()  # Only read-only MySQL work took place.
    return {"snapshot_seal": seal, "tables": len(schema), "rows": count,
            "node": node, "epoch": epoch, "mysql_writes": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("preflight")
    command = commands.add_parser("capture")
    command.add_argument("output", type=Path)
    command.add_argument("--epoch", type=int, required=True)
    command.add_argument("--baseline", type=Path)
    command = commands.add_parser("plan")
    for name in ("baseline", "old", "current", "output"):
        command.add_argument(name, type=Path)
    args = parser.parse_args()
    if args.action == "preflight":
        import blue_node
        with closing(blue_node.connection()) as conn:
            result = facts(conn, blue_node.config()["node"])
            result["tables"] = len(schemas(conn))
    elif args.action == "capture":
        result = capture(args.output, epoch=args.epoch, baseline=args.baseline)
    else:
        result = plan(archive_path(args.baseline), archive_path(args.old), archive_path(args.current),
                      archive_path(args.output, create_parent=True))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "mysql_writes": 0}))
        raise SystemExit(1) from None
