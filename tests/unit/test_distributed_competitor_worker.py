from __future__ import annotations

import asyncio
from types import SimpleNamespace
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from takealot_ops.competitors.distributed_queue import (
    DistributedCompetitorQueue,
    DistributedJobTarget,
)
from takealot_ops.competitors.distributed_worker import (
    DistributedCompetitorWorker,
    DistributedWorkerSettings,
    _validated_loopback_proxy,
    validate_primary_worker_engine,
)
from takealot_ops.competitors.service import (
    CompetitorCollectionResult,
    CompetitorDiscoveredTarget,
)
from takealot_ops.competitors.target_sync import sync_discovered_competitor_targets
from takealot_ops.storage.models import (
    Base,
    CompetitorTarget,
    CompetitorTargetAudit,
    CompetitorWorkerHeartbeat,
)


class _FakeClient:
    async def close(self) -> None:
        return None


class _FakeCollector:
    calls: list[str] = []

    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def collect(self, url: str, **_: object) -> CompetitorCollectionResult:
        self.calls.append(url)
        return CompetitorCollectionResult(
            plid="100",
            title="Test product",
            succeeded=True,
            message="saved",
        )


def test_proxy_must_be_an_unauthenticated_loopback_socks_url() -> None:
    assert _validated_loopback_proxy("socks5://localhost:7897") == (
        "socks5://127.0.0.1:7897"
    )
    for invalid in (
        "http://127.0.0.1:7897",
        "socks5://192.168.1.10:7897",
        "socks5://user:pass@127.0.0.1:7897",
    ):
        with pytest.raises(ValueError):
            _validated_loopback_proxy(invalid)


def test_primary_preflight_rejects_non_mysql_engine() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with pytest.raises(RuntimeError, match="must be MySQL"):
            validate_primary_worker_engine(engine)
    finally:
        engine.dispose()


@pytest.mark.parametrize("failure", ["renew", "heartbeat"])
async def test_renewal_database_failure_marks_lease_lost(monkeypatch, failure):
    import logging

    async def no_wait(_):
        return None

    def fail(*_, **__):
        raise ConnectionError("simulated database disconnect")

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    worker = object.__new__(DistributedCompetitorWorker)
    worker._settings = SimpleNamespace(lease_seconds=30)
    worker._logger = logging.getLogger("blue-renew-test")
    lease = SimpleNamespace(job_id=1, plid="100")
    worker._queue = SimpleNamespace(renew=fail if failure == "renew" else lambda *a, **k: lease)
    worker._heartbeat = fail
    lost = asyncio.Event()
    await worker._renew_lease(lease, lost)
    assert lost.is_set()


async def test_worker_cancellation_also_cancels_collection():
    worker = object.__new__(DistributedCompetitorWorker)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def never_renew(*_):
        await asyncio.Event().wait()

    async def collect(*_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    worker._renew_lease = never_renew
    worker._collect_one = collect
    running = asyncio.create_task(worker._collect_with_renewal(None, None))
    await asyncio.wait_for(started.wait(), 1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert cancelled.is_set()


async def test_worker_claims_one_job_and_publishes_success(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'worker.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    queue = DistributedCompetitorQueue(engine)
    queue.enqueue(
        "canary",
        [DistributedJobTarget(0, "100", "https://www.takealot.com/p/PLID100")],
    )
    settings = DistributedWorkerSettings(
        project_root=tmp_path,
        database_url=str(engine.url),
        worker_id="laptop-worker",
        node_name="LAPTOP-2T5MN8EU",
        egress_label="clash-node-b",
        proxy_server="socks5://127.0.0.1:7897",
        batch_id="canary",
        lease_seconds=30,
        idle_poll_seconds=0.01,
        max_jobs=1,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.distributed_worker.validate_primary_worker_engine",
        lambda _: None,
    )
    monkeypatch.setattr(
        "takealot_ops.competitors.distributed_worker.CompetitorCollector",
        _FakeCollector,
    )
    _FakeCollector.calls.clear()
    worker = DistributedCompetitorWorker(
        settings,
        engine=engine,
        client_factory=_FakeClient,
    )
    try:
        assert await worker.run() == 1
        assert _FakeCollector.calls == ["https://www.takealot.com/p/PLID100"]
        status = queue.batch_status("canary")
        assert status.succeeded == 1
        with Session(engine) as session:
            heartbeat = session.get(CompetitorWorkerHeartbeat, "laptop-worker")
            assert heartbeat is not None
            assert heartbeat.state == "stopped"
            assert heartbeat.current_job_id is None
    finally:
        engine.dispose()


def test_discovered_targets_preserve_grouping_and_audit(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'targets.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    origin = CompetitorDiscoveredTarget(
        plid="100",
        url="https://www.takealot.com/p/PLID100",
        title="Origin",
        seller_name="Seller A",
        price=10.0,
        selected=True,
    )
    follower = CompetitorDiscoveredTarget(
        plid="200",
        url="https://www.takealot.com/p/PLID200",
        title="Follower",
        seller_name="Seller B",
        price=11.0,
        selected=False,
    )
    try:
        added = sync_discovered_competitor_targets(
            engine,
            origin_plid="100",
            discovered_targets=(origin, follower),
            actor_username="laptop-worker",
            actor_display_name="Distributed laptop worker",
            changed_at=now,
        )
        assert [item.plid for item in added] == ["200"]
        with Session(engine) as session:
            targets = list(
                session.scalars(select(CompetitorTarget).order_by(CompetitorTarget.plid))
            )
            assert [(item.plid, item.offer_group_plid) for item in targets] == [
                ("100", "100"),
                ("200", "100"),
            ]
            audit = session.scalar(select(CompetitorTargetAudit))
            assert audit is not None
            assert audit.plid == "200"
            assert audit.action == "auto_discover"
            assert audit.actor_username == "laptop-worker"
    finally:
        engine.dispose()
