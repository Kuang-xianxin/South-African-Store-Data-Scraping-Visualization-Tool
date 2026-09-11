"""Explicit BLUE-only receipt-table preparation and idle activation, no promotion."""
from __future__ import annotations

import argparse
import json
import os
import sys

import blue_node
import blue_test_db
from blue_crawl_journal import MODE, canonical


def preflight() -> dict:
    cfg = blue_node.config()
    root = blue_node.ROOT
    if cfg["auto_failover"] or cfg["mysql_port"] != 3307:
        raise RuntimeError("Unexpected BLUE database mode")
    for name in ("blue_crawl_journal.py", "blue_crawl_delivery.py", "blue_resilient_worker.py"):
        if not (root / name).is_file():
            raise RuntimeError("Durable release is incomplete")
    for relative in ("app/logs/competitor-batch-queue.json", "app/logs/competitor-scheduled-batch.json"):
        path = root / relative
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
            if (state.get("queued_targets") or state.get("priority_targets")
                    or state.get("run_status") in {"running", "retry_wait"} or state.get("pending")):
                raise RuntimeError("BLUE local batch is active; preserve it")
    with blue_test_db.primary_connection("blue_worker_" + cfg["node"], writable=True) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM takealot_ops.competitor_collection_jobs WHERE status IN ('pending','retry','leased')")
        if cursor.fetchone()[0]:
            raise RuntimeError("BLUE shared queue is not idle; preserve it")
    return cfg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare-schema", "activate"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    cfg = preflight()
    if not args.apply:
        print(json.dumps({"node": cfg["node"], "action": args.action, "apply": False}))
        return
    if args.action == "prepare-schema":
        if cfg["node"] != "main":
            raise RuntimeError("Schema preparation only on main BLUE writer")
        sys.path.insert(0, str(blue_node.ROOT / "app/src"))
        from sqlalchemy import create_engine
        from blue_crawl_delivery import metadata

        # Explicit local BLUE root connection validates hostname/id/port/datadir.
        def connection():
            conn = blue_node.connection()
            conn.select_db("takealot_ops")
            return conn

        engine = create_engine("mysql+pymysql://", creator=connection)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql("USE takealot_ops")
                metadata.create_all(conn)
        finally:
            engine.dispose()
        with blue_node.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT User,Host FROM mysql.user WHERE User IN ('blue_worker_main','blue_worker_laptop')")
            if set(cursor.fetchall()) != {("blue_worker_main", "localhost"), ("blue_worker_laptop", "localhost")}:
                raise RuntimeError("Unexpected worker account scope; refusing grants")
            # Local role privilege only; do not replay account GRANTs on the replica.
            cursor.execute("SET SESSION sql_log_bin=0")
            for user in ("blue_worker_main", "blue_worker_laptop"):
                cursor.execute(f"GRANT SELECT,INSERT ON takealot_ops.blue_crawl_delivery_receipts TO '{user}'@'localhost'")
    else:
        with blue_test_db.primary_connection("blue_worker_" + cfg["node"], writable=True) as conn, conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM takealot_ops.blue_crawl_delivery_receipts")
        marker = blue_node.ROOT / "state/durable-crawl.json"
        if marker.exists() and json.loads(marker.read_text(encoding="utf-8")) != MODE:
            raise RuntimeError("Refusing to replace a different activation mode")
        temporary = marker.with_suffix(".next")
        temporary.write_text(canonical(MODE), encoding="utf-8")
        os.replace(temporary, marker)
    print(json.dumps({"node": cfg["node"], "action": args.action, "green_changed": False,
                      "automatic_failover": False}))


if __name__ == "__main__":
    main()
