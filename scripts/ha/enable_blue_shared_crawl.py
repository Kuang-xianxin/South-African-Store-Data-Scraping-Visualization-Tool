"""Enable the reviewed small-batch BLUE adapter only at an idle checkpoint."""
from __future__ import annotations

import json

import blue_node
import blue_test_db


def enable() -> dict:
    cfg = blue_node.config()
    for relative in ("blue_shared_crawl.py", "blue_shared_crawl.html", "blue_web.py"):
        if not (blue_node.ROOT / relative).is_file():
            raise RuntimeError("BLUE shared release is incomplete")
    for name in ("competitor-batch-queue.json", "competitor-scheduled-batch.json"):
        path = blue_node.ROOT / "app/logs" / name
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8-sig"))
            if state.get("active") or state.get("pending") or state.get("run_status") in {
                "running", "paused", "retry_wait",
            }:
                raise RuntimeError("An existing BLUE local batch must be handled before switching dispatch")
    with blue_test_db.primary_connection(f"blue_web_{cfg['node']}", writable=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM takealot_ops.competitor_collection_jobs "
                           "WHERE status IN ('pending','retry','leased')")
            if cursor.fetchone()[0]:
                raise RuntimeError("BLUE queue must be idle before reloading workers")
    state = {"version": 2, "mode": "small-shared-crawl", "max_targets": 20,
             "legacy_crawl_disabled": False, "scheduled_crawl_disabled": True,
             "automatic_failover": False}
    path = blue_node.ROOT / "state/shared-crawl-enabled.json"
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
        if current != state:
            previous = {"version": 1, "mode": "small-shared-crawl", "max_targets": 20,
                        "legacy_crawl_disabled": True, "automatic_failover": False}
            if current != previous:
                raise RuntimeError("Unrecognized BLUE shared activation marker")
            backup = path.with_name("shared-crawl-v1-before-manual.json")
            with backup.open("x", encoding="utf-8") as output:
                json.dump(current, output, indent=2)
            staging = path.with_name("shared-crawl-enabled-v2.next")
            with staging.open("x", encoding="utf-8") as output:
                json.dump(state, output, indent=2)
            staging.replace(path)
    else:
        with path.open("x", encoding="utf-8") as output:
            json.dump(state, output, indent=2)
    return {"node": cfg["node"], **state}


if __name__ == "__main__":
    print(json.dumps(enable()))
