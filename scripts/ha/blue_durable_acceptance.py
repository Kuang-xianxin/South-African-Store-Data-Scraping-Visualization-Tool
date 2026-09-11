"""Real BLUE MySQL lost-ACK probe; no crawling, snapshots or new jobs created."""
from __future__ import annotations

import argparse
import json
import sys

import blue_node
import blue_runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"apply": False, "scope": "BLUE receipt only; no crawling"}))
        return
    blue_runtime.prepare_environment(worker=True)
    from blue_crawl_delivery import DeliveryQueue
    from blue_crawl_journal import Journal
    from takealot_ops.storage.migrations import create_engine_for_database_url

    cfg = blue_node.config()
    engine = create_engine_for_database_url(blue_runtime.database_url(worker=True))
    journal = Journal(blue_node.ROOT / "state/receipt-acceptance/journal.sqlite3", cfg["node"])
    queue = DeliveryQueue(engine, cfg["node"])
    evidence = blue_node.ROOT / "state/durable-receipt-acceptance.json"
    try:
        if evidence.exists():
            print(evidence.read_text(encoding="utf-8"))
            return
        roster = queue.roster()
        candidates = [row for row in roster if row["status"] == "succeeded"]
        if not candidates or any(row["status"] in {"pending", "retry", "leased"} for row in roster):
            raise RuntimeError("Receipt acceptance needs idle BLUE history; refusing new tasks")
        journal.sync(roster)
        pending = journal.pending()
        if not pending:
            spec = candidates[0]
            identity = journal.begin(spec)
            journal.save(dict(version=1, node=cfg["node"], attempt_id=identity,
                task={key: spec[key] for key in ("job_id", "batch_id", "plid", "url", "followers_only", "with_stock_probe")},
                lease_token=None, observation=None, error="acceptance-no-observation"))
        pending = journal.pending()[0]
        with engine.connect() as conn:
            snapshots = conn.exec_driver_sql("SELECT COUNT(*) FROM competitor_snapshots").scalar_one()
        first = queue.submit(pending["body"], pending["digest"])
        # Deliberately don't acknowledge locally: model a lost response after COMMIT.
        second = queue.submit(pending["body"], pending["digest"])
        if not second["reused"] or second["succeeded"]:
            raise RuntimeError("Receipt replay or failure classification mismatch")
        journal.acknowledge(second["attempt_id"], second["sha256"], succeeded=False)
        with engine.connect() as conn:
            after = conn.exec_driver_sql("SELECT COUNT(*) FROM competitor_snapshots").scalar_one()
        if after != snapshots or queue.roster() != roster:
            raise RuntimeError("Receipt-only acceptance unexpectedly changed task/snapshot state")
        result = {"node": cfg["node"], "attempt_id": second["attempt_id"],
                  "first_reused": first["reused"], "second_reused": second["reused"],
                  "snapshot_count": after, "business_snapshots_added": 0, "tasks_unchanged": True,
                  "journal": journal.counts(), "automatic_failover": False}
        evidence.write_text(json.dumps(result), encoding="utf-8")
        print(json.dumps(result))
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__}))
        sys.exit(1)
