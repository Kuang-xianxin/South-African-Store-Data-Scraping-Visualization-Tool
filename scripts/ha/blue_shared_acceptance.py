"""Bounded real-MySQL acceptance of the same controller used by the BLUE panel."""
from __future__ import annotations

import json
from uuid import uuid4

import blue_node
import blue_runtime
import blue_test_db


def main():
    blue_test_db.require_main()
    blue_runtime.prepare_environment()
    from blue_shared_crawl import BlueSharedCrawl, StartRequest
    from takealot_ops.storage.migrations import create_engine_for_database_url

    evidence_path = blue_node.ROOT / "state/shared-web-acceptance.json"
    if evidence_path.exists():
        raise RuntimeError("Inspect existing acceptance request; do not dispatch another batch")
    with blue_test_db.primary_connection("blue_web_main", writable=True) as conn, conn.cursor() as q:
        q.execute("SELECT worker_id FROM takealot_ops.competitor_worker_heartbeats "
                  "WHERE state='idle' AND worker_id IN ('blue-main','blue-laptop') "
                  "AND last_seen_at >= UTC_TIMESTAMP() - INTERVAL 45 SECOND")
        if {row[0] for row in q.fetchall()} != {"blue-main", "blue-laptop"}:
            raise RuntimeError("Both reviewed workers must be fresh and idle")
    previous = json.loads((blue_node.ROOT / "state/crawl-acceptance.json").read_text(encoding="utf-8"))
    request = StartRequest(request_id=uuid4().hex, plids=previous["targets"])
    if len(request.plids) != 2:
        raise RuntimeError("Exactly two previously bounded targets are required")
    evidence = {"request": request.model_dump(), "state": "prepared", "green_changed": False}
    with evidence_path.open("x", encoding="utf-8") as output:
        json.dump(evidence, output, indent=2)
    engine = create_engine_for_database_url(blue_runtime.database_url())
    try:
        controller = BlueSharedCrawl(engine)
        evidence["first"] = controller.start(request)
        evidence["duplicate"] = controller.start(request)
        assert evidence["duplicate"]["reused"] is True
        evidence["state"] = "committed"
        evidence_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(json.dumps(evidence))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
