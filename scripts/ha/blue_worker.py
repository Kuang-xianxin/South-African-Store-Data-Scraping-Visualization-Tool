"""Dedicated BLUE worker; verified TLS + exact BLUE identity on every connection."""
from __future__ import annotations

import asyncio
import json

import blue_node
import blue_runtime
import blue_test_db


def validate_engine(engine) -> None:
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(text("SELECT @@hostname,@@server_id,@@port,@@datadir,@@server_uuid,"
                                "@@global.read_only,@@global.super_read_only")).one()
        blue_test_db.validate_primary(tuple(row), writable=True)


async def main() -> None:
    # Check the small marker without importing the app before release isolation.
    durable_path = blue_node.ROOT / "state/durable-crawl.json"
    state = blue_runtime.prepare_environment(worker=True, allow_offline_spool=durable_path.exists())
    from takealot_ops.competitors.distributed_worker import DistributedCompetitorWorker, DistributedWorkerSettings
    from takealot_ops.storage.migrations import create_engine_for_database_url

    cfg = blue_node.config()
    url = blue_runtime.database_url(worker=True)
    engine = create_engine_for_database_url(url)
    if durable_path.exists():
        from blue_resilient_worker import run

        print(json.dumps(state), flush=True)
        try:
            await run(blue_node.ROOT, engine, cfg['node'], cfg['computer'], blue_runtime.local_proxy(cfg['node']))
        finally:
            engine.dispose()
        return
    worker = DistributedCompetitorWorker(
        DistributedWorkerSettings(
            project_root=blue_node.ROOT / "app", database_url=url,
            worker_id=f"blue-{cfg['node']}", node_name=cfg['computer'],
            egress_label=f"blue-{cfg['node']}-local-proxy",
            proxy_server=blue_runtime.local_proxy(cfg['node']), idle_poll_seconds=5, lease_seconds=90,
        ), engine=engine, engine_validator=validate_engine,
    )
    print(json.dumps(state), flush=True)
    try:
        await worker.run()
    finally:
        engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
