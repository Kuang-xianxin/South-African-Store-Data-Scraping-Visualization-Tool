"""Preflight and persist an explicitly manual, fixed-primary BLUE testing mode."""
from __future__ import annotations

import json
import sys

import blue_node
import blue_runtime
import blue_test_db


def preflight() -> dict[str, object]:
    cfg = blue_node.config()
    for relative in ("blue_runtime.py", "blue_web.py", "blue_worker.py", "run_blue_worker.ps1",
                     "app/src/takealot_ops/competitors/distributed_worker.py"):
        if not (blue_node.ROOT / relative).is_file():
            raise RuntimeError(f"BLUE release file missing: {relative}")
    worker_source = (blue_node.ROOT / "app/src/takealot_ops/competitors/distributed_worker.py").read_text(encoding="utf-8")
    if "self._engine_validator(self._engine)" not in worker_source:
        raise RuntimeError("BLUE worker validator override has not been deployed")
    with blue_test_db.primary_connection(f"blue_web_{cfg['node']}", writable=True):
        pass
    with blue_test_db.primary_connection(f"blue_worker_{cfg['node']}", writable=True):
        pass
    with blue_test_db.primary_connection(f"blue_web_{cfg['node']}", writable=True) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM takealot_ops.competitor_collection_jobs WHERE status='leased'")
        if cursor.fetchone()[0]:
            raise RuntimeError("BLUE crawl jobs are active; wait before restarting BLUE tasks")
    blue_runtime.integration_values()
    if cfg["node"] == "laptop":
        with blue_node.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SHOW REPLICA STATUS FOR CHANNEL 'blue_test_main'")
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("BLUE replication is missing")
            status = dict(zip((item[0] for item in cursor.description), row))
            if (status["Replica_IO_Running"], status["Replica_SQL_Running"], status["Seconds_Behind_Source"]) != ("Yes", "Yes", 0):
                raise RuntimeError("BLUE replica is not healthy and caught up")
    return {"mode": "fixed-primary-blue-test", "primary_server_id": 101, "automatic_failover": False}


if __name__ == "__main__":
    if sys.argv[1:] not in (["preflight"], ["activate"]):
        raise SystemExit("preflight or activate required")
    state = preflight()
    if sys.argv[1] == "activate":
        (blue_node.ROOT / "state/writable-test.json").write_text(json.dumps(state), encoding="utf-8")
    print(json.dumps(state))
