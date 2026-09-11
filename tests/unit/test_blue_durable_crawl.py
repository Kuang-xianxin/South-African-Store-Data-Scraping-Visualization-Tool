from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import importlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, func, insert, select, update

from takealot_ops.storage.models import (
    Base, CompetitorCollectionJob, CompetitorReview, CompetitorSnapshot, CompetitorTarget,
)


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/ha"))
    return tuple(importlib.import_module(name) for name in
                 ("blue_crawl_journal", "blue_crawl_delivery", "blue_resilient_worker"))


@pytest.fixture
def setup(modules, tmp_path):
    journal, delivery, worker = modules
    engine = create_engine(f"sqlite:///{tmp_path / 'central-test.sqlite'}")
    Base.metadata.create_all(engine)
    delivery.metadata.create_all(engine)
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(insert(CompetitorTarget).values(plid="100", url="https://www.takealot.com/p/PLID100",
                     active=True, created_at=now, updated_at=now))
        for index, letter in enumerate(("a", "b"), 1):
            conn.execute(insert(CompetitorCollectionJob).values(id=index, batch_id="blue-web-" + letter * 32,
                item_index=0, plid="100", url="https://www.takealot.com/p/PLID100", followers_only=False,
                with_stock_probe=True, status="pending", priority=0, attempt_count=0, max_attempts=3,
                available_at=now, created_at=now, updated_at=now))
    queue = delivery.DeliveryQueue(engine, "main")
    yield journal, delivery, worker, engine, queue, tmp_path
    engine.dispose()


def observation(*, when=None, title="Product", failed=False, review="new"):
    variant = dict(key="default", label="Default", url="https://www.takealot.com/p/PLID100", title=title,
                   sku="sku1", seller_id="seller1", seller_name="Seller", price=99.0,
                   stock_status="in stock", is_leadtime=False, is_add_to_cart_available=True)
    stock = dict(quantity=None if failed else 7, exact=not failed,
                 method="failed" if failed else "exact", note="isolated fixture")
    return dict(product=dict(plid="100", url=variant["url"], title=title, image_url=None, sku="sku1",
                seller_id="seller1", seller_name="Seller", price=99.0, stock_status="in stock",
                is_leadtime=False, review_count=1, rating=5.0, offers=[], variants=[variant], category_path=[]),
                reviews=[dict(review_id="r1", rating=5, title=review, body=review,
                              customer_name="fixture", review_date="2026-09-01")],
                stock=stock, variant_stocks=[dict(variant=variant, stock=stock)], offer_stocks=[],
                collected_at=(when or datetime.now(UTC)).isoformat())


def envelope(journal, spec, *, node="main", identity=None, obs=True, token=None):
    data = dict(version=1, attempt_id=identity or uuid4().hex, node=node, task=journal.task_spec(spec),
                lease_token=token, observation=observation() if obs is True else obs, error="" if obs else "NetworkError")
    body = journal.canonical(data)
    return data, body, sha256(body.encode()).hexdigest()


def count(engine, table):
    with engine.connect() as conn:
        return conn.scalar(select(func.count()).select_from(table))


def test_union_accepts_two_attempts_but_deduplicates_each_delivery(setup):
    j, d, _, engine, queue, _ = setup
    spec = queue.roster()[-1]
    _, body, digest = envelope(j, spec)
    assert queue.submit(body, digest)["succeeded"]
    assert queue.submit(body, digest)["reused"]
    _, second, second_digest = envelope(j, spec, node="laptop")
    assert d.DeliveryQueue(engine, "laptop").submit(second, second_digest)["succeeded"]
    assert count(engine, CompetitorSnapshot) == 2
    assert count(engine, d.receipts) == 2
    with engine.connect() as conn:
        assert dict(conn.execute(select(CompetitorCollectionJob.id, CompetitorCollectionJob.status)).all()) == {1: "succeeded", 2: "pending"}


def test_failed_late_attempt_does_not_undo_other_success(setup):
    j, _, _, engine, queue, _ = setup
    spec = queue.roster()[-1]
    _, body, digest = envelope(j, spec)
    queue.submit(body, digest)
    _, failed, digest = envelope(j, spec, obs=None)
    assert not queue.submit(failed, digest)["succeeded"]
    with engine.connect() as conn:
        assert conn.scalar(select(CompetitorCollectionJob.status).where(CompetitorCollectionJob.id == 1)) == "succeeded"


def test_partial_snapshot_is_saved_but_not_in_success_union(setup):
    j, _, _, engine, queue, _ = setup
    _, body, digest = envelope(j, queue.roster()[-1], obs=observation(failed=True))
    assert not queue.submit(body, digest)["succeeded"]
    assert count(engine, CompetitorSnapshot) == 1
    with engine.connect() as conn:
        assert conn.scalar(select(CompetitorCollectionJob.status).where(CompetitorCollectionJob.id == 1)) == "retry"


def test_missing_variants_cannot_be_reported_as_success(setup):
    j, _, _, engine, queue, _ = setup
    obs = observation()
    obs["variant_stocks"] = []
    _, body, digest = envelope(j, queue.roster()[-1], obs=obs)
    with pytest.raises(ValueError, match="scope"):
        queue.submit(body, digest)
    assert count(engine, CompetitorSnapshot) == 0


def test_receipt_snapshot_and_completion_rollback_together(setup):
    j, d, _, engine, queue, _ = setup
    _, body, digest = envelope(j, queue.roster()[-1])

    def fail_receipt(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO blue_crawl_delivery_receipts"):
            raise RuntimeError("injected before receipt commit")

    event.listen(engine, "before_cursor_execute", fail_receipt)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            queue.submit(body, digest)
    finally:
        event.remove(engine, "before_cursor_execute", fail_receipt)
    assert count(engine, CompetitorSnapshot) == count(engine, d.receipts) == 0
    assert queue.submit(body, digest)["succeeded"]
    assert count(engine, CompetitorSnapshot) == 1


def test_late_result_keeps_capture_time_and_newer_review(setup):
    j, _, _, engine, queue, _ = setup
    spec = queue.roster()[-1]
    now = datetime.now(UTC)
    for when, body_text in ((now, "new"), (now - timedelta(hours=1), "old")):
        _, body, digest = envelope(j, spec, obs=observation(when=when, review=body_text))
        queue.submit(body, digest)
    with engine.connect() as conn:
        review = conn.execute(select(CompetitorReview.__table__)).mappings().one()
        assert review["body"] == "new"
        assert review["first_seen_at"].replace(tzinfo=UTC) == now - timedelta(hours=1)
        assert review["last_seen_at"].replace(tzinfo=UTC) == now
        old = conn.execute(select(CompetitorSnapshot.__table__).order_by(CompetitorSnapshot.id.desc())).mappings().first()
        assert old["collected_at"].replace(tzinfo=UTC) == now - timedelta(hours=1)
        assert old["previous_snapshot_id"] is None


@pytest.mark.parametrize("kind", ["hash", "node", "task", "attempt"])
def test_rejects_conflicting_identity(setup, kind):
    j, _, _, engine, queue, _ = setup
    data, body, digest = envelope(j, queue.roster()[-1])
    queue.submit(body, digest)
    if kind == "hash":
        digest = "0" * 64
    else:
        if kind == "node":
            data["node"] = "laptop"
        elif kind == "task":
            data["task"]["job_id"] = 2
        else:
            data["observation"]["product"]["title"] = "changed"
        body = j.canonical(data)
        digest = sha256(body.encode()).hexdigest()
    with pytest.raises(ValueError):
        queue.submit(body, digest)
    assert count(engine, CompetitorSnapshot) == 1


def test_cancelled_job_keeps_received_observation_without_reopening(setup):
    j, _, _, engine, queue, _ = setup
    spec = queue.roster()[-1]
    with engine.begin() as conn:
        conn.execute(update(CompetitorCollectionJob).where(CompetitorCollectionJob.id == 1).values(status="cancelled"))
    _, body, digest = envelope(j, spec)
    queue.submit(body, digest)
    assert queue.roster()[-1]["status"] == "cancelled"
    assert count(engine, CompetitorSnapshot) == 1


def test_expired_lease_can_be_reclaimed_even_after_old_attempt_budget(setup):
    _, _, _, engine, queue, _ = setup
    spec = queue.roster()[-1]
    token = queue.claim(spec)
    with engine.begin() as conn:
        conn.execute(update(CompetitorCollectionJob).where(CompetitorCollectionJob.id == 1).values(
            attempt_count=3, lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert queue.claim(spec) not in {None, token}


def test_disk_spool_survives_restart_and_only_ack_marks_done(setup):
    j, _, _, _, queue, tmp = setup
    path = tmp / "outbox.sqlite"
    local = j.Journal(path, "main")
    local.sync(queue.roster())
    spec = local.candidate()
    identity = local.begin(spec)
    data, body, digest = envelope(j, spec, identity=identity)
    local.save(data)
    recovered = j.Journal(path, "main")
    assert recovered.pending()[0]["body"] == body
    assert recovered.counts() == {"pending": 1}
    reply = queue.submit(body, digest)
    recovered.acknowledge(identity, digest, succeeded=reply["succeeded"])
    assert recovered.counts() == {"acked": 1}
    with recovered.connect() as db:
        assert db.execute("SELECT body FROM attempts WHERE attempt_id=?", (identity,)).fetchone()[0] == body


def test_roster_identity_and_clock_window_are_guarded(setup):
    j, _, _, _, queue, tmp = setup
    clock = [1000.0]
    local = j.Journal(tmp / "outbox.sqlite", "main", clock=lambda: clock[0])
    rows = queue.roster()
    local.sync(rows)
    assert local.candidate()
    clock[0] += 1801
    assert local.candidate() is None
    clock[0] = 999
    assert local.candidate() is None
    rows[0]["with_stock_probe"] = False
    with pytest.raises(ValueError, match="changed"):
        local.sync(rows)
    with pytest.raises(ValueError, match="identity"):
        j.Journal(tmp / "outbox.sqlite", "laptop")


def test_retry_delay_does_not_block_other_ready_tasks(setup):
    j, _, _, _, queue, tmp = setup
    local = j.Journal(tmp / "outbox.sqlite", "main")
    rows = queue.roster()
    rows[-1]["available_after"] = 120
    local.sync(rows)
    assert local.candidate()["job_id"] == 2


def test_running_attempt_recovery_does_not_discard_pending_result(setup):
    j, _, _, _, queue, tmp = setup
    local = j.Journal(tmp / "outbox.sqlite", "main")
    local.sync(queue.roster())
    spec = local.candidate()
    local.begin(spec)
    assert local.recover() == 1
    identity = local.begin(spec)
    data, _, _ = envelope(j, spec, identity=identity)
    local.save(data)
    assert local.recover() == 0
    assert local.counts() == {"interrupted": 1, "pending": 1}


def test_worker_keeps_result_when_network_fails_during_collection(setup):
    j, _, w, engine, queue, tmp = setup
    local = j.Journal(tmp / "outbox.sqlite", "main")
    original = queue.submit
    calls = []

    async def collect(spec, persist):
        calls.append(spec)
        queue.submit = lambda *_: (_ for _ in ()).throw(ConnectionError("injected outage"))
        return observation()

    worker = w.ResilientWorker(journal=local, queue=queue, collect=collect)
    assert asyncio.run(worker.tick())
    assert local.counts() == {"pending": 1}
    assert count(engine, CompetitorSnapshot) == 0
    queue.submit = original
    assert asyncio.run(worker.synchronize())
    assert local.counts() == {"acked": 1}
    assert count(engine, CompetitorSnapshot) == 1
    assert len(calls) == 1


def test_committed_but_lost_ack_replays_without_duplicate_snapshot(setup):
    j, _, w, engine, queue, tmp = setup
    local = j.Journal(tmp / "outbox.sqlite", "main")
    local.sync(queue.roster())
    spec = local.candidate()
    identity = local.begin(spec)
    data, _, _ = envelope(j, spec, identity=identity)
    local.save(data)
    original = queue.submit

    def lost_ack(body, digest):
        original(body, digest)
        raise ConnectionError("injected lost ack after commit")

    queue.submit = lost_ack
    worker = w.ResilientWorker(journal=local, queue=queue, collect=None)
    assert not asyncio.run(worker.synchronize())
    assert local.counts() == {"pending": 1}
    queue.submit = original
    assert asyncio.run(worker.synchronize())
    assert local.counts() == {"acked": 1}
    assert count(engine, CompetitorSnapshot) == 1


def test_disconnected_worker_can_use_cached_tasks(setup):
    j, _, w, _, queue, tmp = setup
    local = j.Journal(tmp / "outbox.sqlite", "laptop")
    local.sync(queue.roster())
    queue.roster = lambda: (_ for _ in ()).throw(ConnectionError("offline"))
    queue.submit = lambda *_: (_ for _ in ()).throw(ConnectionError("offline"))

    async def collect(_, persist):
        return observation()

    worker = w.ResilientWorker(journal=local, queue=queue, collect=collect)
    assert asyncio.run(worker.tick())
    assert local.counts() == {"pending": 1}


def test_marker_is_separate_from_database_promotion(modules, tmp_path):
    j, _, _ = modules
    assert not j.enabled(tmp_path)
    (tmp_path / "state").mkdir()
    marker = tmp_path / "state/durable-crawl.json"
    marker.write_text(json.dumps(j.MODE))
    assert j.enabled(tmp_path)
    marker.write_text(json.dumps(dict(j.MODE, automatic_failover=True)))
    with pytest.raises(RuntimeError):
        j.enabled(tmp_path)


def test_invalid_delivery_is_retained_without_blocking_other_tasks(setup):
    j, _, w, engine, queue, tmp = setup
    local = j.Journal(tmp / "outbox.sqlite", "main")
    local.sync(queue.roster())
    for spec in queue.roster():
        data, _, _ = envelope(j, spec, identity=local.begin(spec))
        if spec["job_id"] == 1:
            data["observation"]["variant_stocks"] = []
        local.save(data)
    worker = w.ResilientWorker(journal=local, queue=queue, collect=None)
    assert asyncio.run(worker.synchronize())
    assert local.counts() == {"acked": 1, "blocked": 1}
    assert count(engine, CompetitorSnapshot) == 1
    with local.connect() as db:
        assert db.execute("SELECT LENGTH(body) FROM attempts WHERE state='blocked'").fetchone()[0] > 0


def test_browser_cleanup_failure_cannot_discard_captured_result(setup):
    j, _, w, engine, queue, tmp = setup
    local = j.Journal(tmp / "outbox.sqlite", "main")

    async def collect(spec, persist):
        persist(observation())
        raise RuntimeError("injected browser cleanup failure")

    worker = w.ResilientWorker(journal=local, queue=queue, collect=collect)
    assert asyncio.run(worker.tick())
    assert local.counts() == {"acked": 1}
    assert count(engine, CompetitorSnapshot) == 1


def test_unknown_result_ack_and_mutation_cannot_remove_pending(setup):
    j, _, _, _, queue, tmp = setup
    local = j.Journal(tmp / "outbox.sqlite", "main")
    local.sync(queue.roster())
    spec = local.candidate()
    data, _, digest = envelope(j, spec, identity=local.begin(spec))
    local.save(data)
    with pytest.raises(ValueError, match="ACK"):
        local.acknowledge(data["attempt_id"], "0" * 64, succeeded=True)
    data["error"] = "changed"
    with pytest.raises(ValueError, match="immutable"):
        local.save(data)
    assert local.pending()[0]["digest"] == digest


def test_new_dispatch_waits_for_both_worker_versions(setup):
    from fastapi import HTTPException
    from takealot_ops.competitors.distributed_queue import DistributedCompetitorQueue

    _, _, _, engine, _, _ = setup
    shared = importlib.import_module("blue_shared_crawl")
    controller = shared.BlueSharedCrawl(engine, durable=True)
    request = shared.StartRequest(request_id="c" * 32, plids=["100"])
    with pytest.raises(HTTPException) as error:
        controller.start(request)
    assert error.value.status_code == 409
    assert count(engine, CompetitorCollectionJob) == 2
    heartbeats = DistributedCompetitorQueue(engine)
    for node in ("main", "laptop"):
        heartbeats.heartbeat(worker_id="blue-" + node, node_name=node, egress_label=node,
            state="idle", started_at=datetime.now(UTC), last_error="durable-v1 pending_uploads=0")
    # Both version records pass the deployment gate. Stop before any real dispatch.
    original = controller._start_locked
    try:
        controller._start_locked = lambda *_: {"versions_passed": True}
        from contextlib import contextmanager

        @contextmanager
        def lock(conn):
            yield

        old_lock = shared.dispatch_lock
        shared.dispatch_lock = lock
        try:
            assert controller.start(request) == {"versions_passed": True}
        finally:
            shared.dispatch_lock = old_lock
    finally:
        controller._start_locked = original
