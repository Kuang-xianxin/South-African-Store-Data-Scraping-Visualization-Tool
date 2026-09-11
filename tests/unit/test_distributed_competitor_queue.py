from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

from sqlalchemy import create_engine

from takealot_ops.competitors.distributed_queue import (
    DistributedCompetitorQueue,
    DistributedJobOutcome,
    DistributedJobTarget,
)
from takealot_ops.storage.models import Base


def _queue(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'distributed-queue.sqlite3').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    return engine, DistributedCompetitorQueue(engine)


def test_enqueue_is_idempotent_per_batch_and_plid(tmp_path) -> None:
    engine, queue = _queue(tmp_path)
    try:
        targets = [
            DistributedJobTarget(0, "100", "https://www.takealot.com/p/PLID100"),
            DistributedJobTarget(1, "100", "https://www.takealot.com/p/PLID100?again=1"),
            DistributedJobTarget(2, "200", "https://www.takealot.com/p/PLID200"),
        ]
        assert queue.enqueue("batch-a", targets) == 2
        assert queue.enqueue("batch-a", targets) == 0
        assert queue.enqueue("batch-b", targets) == 2
        assert queue.batch_status("batch-a").total == 2
    finally:
        engine.dispose()


def test_atomic_claim_allows_only_one_worker_to_own_a_job(tmp_path) -> None:
    engine, queue = _queue(tmp_path)
    try:
        queue.enqueue(
            "batch-a",
            [DistributedJobTarget(0, "100", "https://www.takealot.com/p/PLID100")],
        )
        barrier = Barrier(2)

        def claim(worker_id: str):
            local_queue = DistributedCompetitorQueue(engine)
            barrier.wait(timeout=5)
            return local_queue.claim(worker_id, batch_id="batch-a")

        with ThreadPoolExecutor(max_workers=2) as pool:
            leases = list(pool.map(claim, ("main-worker", "laptop-worker")))
        winners = [lease for lease in leases if lease is not None]
        assert len(winners) == 1
        assert winners[0].worker_id in {"main-worker", "laptop-worker"}
        assert queue.batch_status("batch-a").leased == 1
    finally:
        engine.dispose()


def test_expired_lease_is_reclaimed_and_stale_token_cannot_finish(tmp_path) -> None:
    now = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    engine = create_engine(f"sqlite:///{(tmp_path / 'reclaim.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    clock = [now]
    queue = DistributedCompetitorQueue(engine, clock=lambda: clock[0])
    try:
        queue.enqueue(
            "batch-a",
            [DistributedJobTarget(0, "100", "https://www.takealot.com/p/PLID100")],
        )
        first = queue.claim("main-worker", batch_id="batch-a", lease_seconds=30)
        assert first is not None
        clock[0] = now + timedelta(seconds=31)
        outcome = DistributedJobOutcome(True, "title", "ok", None, False)
        assert queue.finish(first, outcome) is None
        second = queue.claim("laptop-worker", batch_id="batch-a", lease_seconds=30)
        assert second is not None
        assert second.job_id == first.job_id
        assert second.attempt == 2
        outcome = DistributedJobOutcome(True, "title", "ok", None, False)
        assert queue.finish(first, outcome) is None
        assert queue.finish(second, outcome) == "succeeded"
    finally:
        engine.dispose()


def test_retry_stays_pending_until_max_attempts_then_becomes_terminal(tmp_path) -> None:
    now = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    engine = create_engine(f"sqlite:///{(tmp_path / 'retry.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    clock = [now]
    queue = DistributedCompetitorQueue(engine, clock=lambda: clock[0])
    try:
        queue.enqueue(
            "batch-a",
            [
                DistributedJobTarget(
                    0,
                    "100",
                    "https://www.takealot.com/p/PLID100",
                    max_attempts=2,
                )
            ],
        )
        failure = DistributedJobOutcome(False, None, "temporary", "network", True)
        first = queue.claim("worker", batch_id="batch-a")
        assert first is not None
        assert queue.finish(first, failure, retry_delay_seconds=60) == "retry"
        assert queue.claim("worker", batch_id="batch-a") is None
        clock[0] = now + timedelta(seconds=61)
        second = queue.claim("worker", batch_id="batch-a")
        assert second is not None
        assert queue.finish(second, failure) == "terminal"
        status = queue.batch_status("batch-a")
        assert status.terminal == 1
        assert status.finished == 1
    finally:
        engine.dispose()


def test_worker_heartbeat_is_upserted(tmp_path) -> None:
    engine, queue = _queue(tmp_path)
    started = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    try:
        queue.heartbeat(
            worker_id="laptop-worker",
            node_name="LAPTOP-2T5MN8EU",
            egress_label="clash-node-b",
            state="idle",
            started_at=started,
        )
        queue.heartbeat(
            worker_id="laptop-worker",
            node_name="LAPTOP-2T5MN8EU",
            egress_label="clash-node-b",
            state="collecting",
            started_at=started,
            current_job_id=7,
            current_plid="100",
        )
    finally:
        engine.dispose()
