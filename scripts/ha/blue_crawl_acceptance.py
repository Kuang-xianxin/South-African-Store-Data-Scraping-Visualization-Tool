"""Enqueue two bounded BLUE canaries after both dedicated workers are fresh."""
from __future__ import annotations

import json
from pathlib import Path
import re
from uuid import uuid4

import blue_node
import blue_runtime
import blue_test_db


def main() -> None:
    blue_test_db.require_main()
    blue_runtime.prepare_environment()
    from takealot_ops.competitors.distributed_queue import DistributedCompetitorQueue, DistributedJobTarget
    from takealot_ops.competitors.own_store import connected_store_plids
    from takealot_ops.storage.migrations import create_engine_for_database_url
    from sqlalchemy.orm import Session

    path = blue_node.ROOT / "state/crawl-acceptance.json"
    if path.exists():
        raise RuntimeError("A BLUE canary already exists; inspect it before adding work")
    excluded = set()
    journal = Path("D:/南非店铺数据抓取/logs/competitor-scheduled-batch.json")
    if journal.is_file():
        value = json.loads(journal.read_text(encoding="utf-8"))
        candidates = [value.get("active_item"), *(value.get("queue") or [])[:5]]
        for item in candidates:
            if isinstance(item, dict):
                match = re.search(r"PLID(\d+)", str(item.get("url", "")), re.I)
                if match:
                    excluded.add(match[1])
    identity_engine = create_engine_for_database_url(blue_runtime.database_url())
    try:
        with Session(identity_engine) as session:
            excluded.update(connected_store_plids(session))
    finally:
        identity_engine.dispose()
    with blue_test_db.primary_connection("blue_web_main", writable=True) as conn, conn.cursor() as q:
        q.execute("SELECT worker_id FROM takealot_ops.competitor_worker_heartbeats "
                  "WHERE worker_id IN ('blue-main','blue-laptop') AND state='idle' "
                  "AND egress_label=CONCAT(worker_id,'-local-proxy') "
                  "AND last_seen_at >= UTC_TIMESTAMP() - INTERVAL 45 SECOND")
        if {row[0] for row in q.fetchall()} != {"blue-main", "blue-laptop"}:
            raise RuntimeError("Both BLUE workers must be fresh, idle and on the reviewed proxy configuration")
        q.execute("SELECT COUNT(*) FROM takealot_ops.competitor_collection_jobs "
                  "WHERE status IN ('pending','retry','leased')")
        if q.fetchone()[0]:
            raise RuntimeError("BLUE queue already has unfinished work; do not add another canary")
        q.execute("SELECT t.plid,t.url FROM takealot_ops.competitor_targets t "
                  "JOIN (SELECT plid,MAX(id) id FROM takealot_ops.competitor_snapshots GROUP BY plid) s ON s.plid=t.plid "
                  "LEFT JOIN takealot_ops.competitor_link_health h ON h.plid=t.plid "
                  "WHERE t.active=1 AND (h.status='healthy' OR h.plid IS NULL) "
                  "AND (SELECT COUNT(*) FROM takealot_ops.competitor_variant_snapshots v WHERE v.snapshot_id=s.id)<=3 "
                  "ORDER BY s.id DESC LIMIT 30")
        targets = [row for row in q.fetchall() if row[0] not in excluded][:2]
        if len(targets) != 2:
            raise RuntimeError("Insufficient non-overlapping healthy BLUE targets")
        q.execute("SELECT MAX(id) FROM takealot_ops.competitor_snapshots")
        before = q.fetchone()[0]
    batch_id = "blue-test-" + uuid4().hex
    evidence = {"batch_id": batch_id, "targets": [row[0] for row in targets], "snapshot_id_before": before,
                "maximum_tasks": 2, "maximum_attempts_each": 1, "dispatch_state": "prepared"}
    # Persist the identity BEFORE dispatch: an uncertain SQL response must not create
    # a different batch on retry. Inspect this exact ID instead of blindly re-running.
    with path.open("x", encoding="utf-8") as output:
        json.dump(evidence, output, indent=2)
    engine = create_engine_for_database_url(blue_runtime.database_url())
    try:
        added = DistributedCompetitorQueue(engine).enqueue(batch_id, [
            DistributedJobTarget(item_index=i, plid=plid, url=url, with_stock_probe=True, max_attempts=1)
            for i, (plid, url) in enumerate(targets)
        ])
        evidence["enqueued"] = added
        evidence["dispatch_state"] = "committed"
        path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(json.dumps(evidence))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
