"""Daily BLUE scope and durable queue regressions; all data stays in SQLite fixtures."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
import importlib
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, func, insert, select, update
from sqlalchemy.orm import Session

from takealot_ops.competitors.distributed_queue import DistributedCompetitorQueue
from takealot_ops.storage.models import (
    Base, CompetitorCollectionJob, CompetitorLinkHealth, CompetitorSnapshot,
    CompetitorTarget, ErpStore, OfferCurrent,
)
from test_blue_durable_crawl import envelope, observation


@pytest.fixture
def full(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/ha"))
    m, j, d, w = (importlib.import_module(name) for name in (
        "blue_shared_crawl", "blue_crawl_journal", "blue_crawl_delivery", "blue_resilient_worker"))
    engine = create_engine(f"sqlite:///{tmp_path / 'full.sqlite'}")
    Base.metadata.create_all(engine)
    d.metadata.create_all(engine)
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(insert(ErpStore), [dict(code=code, display_name=code, active=active,
            data_connected=True, created_at=now, updated_at=now)
            for code, active in (("allowed", True), ("private", True), ("disabled", False))])
        conn.execute(insert(OfferCurrent), [dict(store_code=code, offer_id=offer, sku=sku,
            productline_id=plid, captured_at=now) for code, offer, sku, plid in (
                ("allowed", "OWN-A", " Own Sku ", "100"),
                ("private", "OWN-B", "other-store-sku", "100"),
                ("private", "PRIVATE", "private-sku", "200"),
                ("disabled", "OLD", "disabled-sku", "300"))])
        conn.execute(insert(CompetitorTarget), [dict(plid=str(plid), url=f"https://www.takealot.com/p/PLID{plid}",
            active=True, created_at=now, updated_at=now) for plid in (100, 200, *range(1000, 1030))])
    heartbeat = DistributedCompetitorQueue(engine)
    for node in ("main", "laptop"):
        heartbeat.heartbeat(worker_id="blue-" + node, node_name=node, egress_label=node,
            state="idle", started_at=now, last_error="durable-v1 full-v1 pending_uploads=0")

    @contextmanager
    def lock(conn):
        yield

    monkeypatch.setattr(m, "dispatch_lock", lock)
    yield SimpleNamespace(m=m, j=j, d=d, w=w, engine=engine,
        controller=m.BlueSharedCrawl(engine, durable=True), queue=d.DeliveryQueue(engine, "main"), root=tmp_path)
    engine.dispose()


def start(full, token="a"):
    return full.controller.start_full(full.m.FullStartRequest(request_id=token * 32), {"allowed"})


def own_spec(full):
    start(full)
    return next(row for row in full.queue.roster() if row["plid"] == "100")


def own_observation(full, *, no_followers=False):
    value = observation()
    skipped = dict(quantity=None, exact=False, method="skipped", note="Seller API only")
    value["stock"] = skipped
    value["variant_stocks"][0]["stock"] = skipped
    def offer(offer_id, sku, seller):
        return dict(selected=False, offer_id=offer_id, sku=sku, seller_id=seller, seller_name=seller,
            price=99.0, stock_status="in stock", variant_key="default", is_follower_offer=False)
    ours = [offer("own-a", "own sku", "ours"), offer("own-b", "other-store-sku", "ours-other")]
    other = offer("FOLLOWER", "follower-sku", "competitor")
    value["product"]["offers"] = ours if no_followers else [*ours, other]
    value["offer_stocks"] = [] if no_followers else [dict(offer=other,
        stock=dict(quantity=7, exact=True, method="exact", note="fixture"))]
    return value


def test_full_dispatch_has_no_twenty_limit_and_freezes_authorized_roster(full):
    assert full.controller.preview_full({"allowed"}) == {"total": 31, "competitors": 30, "own": 1}
    first = start(full)
    assert first["total"] == 31
    rows = full.queue.roster()
    assert {row["plid"] for row in rows} == {"100", *map(str, range(1000, 1030))}
    own = next(row for row in rows if row["followers_only"])
    assert set(own["own_offer_scopes"]) == {"own-a", "own sku", "own-b", "other-store-sku"}
    with full.engine.begin() as conn:
        conn.execute(update(CompetitorTarget).values(active=False))
    assert start(full) == dict(first, reused=True)
    with pytest.raises(HTTPException) as blocked:
        start(full, "b")
    assert blocked.value.status_code == 409


@pytest.mark.parametrize("no_followers", [True, False])
def test_own_followers_keep_own_inventory_unknown_and_never_register_target(full, no_followers):
    spec = own_spec(full)
    with full.engine.begin() as conn:
        conn.execute(CompetitorTarget.__table__.delete().where(CompetitorTarget.plid == "100"))
    value = own_observation(full, no_followers=no_followers)
    _, body, digest = envelope(full.j, spec, obs=value)
    assert full.queue.submit(body, digest)["succeeded"]
    with full.engine.connect() as conn:
        assert conn.scalar(select(CompetitorTarget.plid).where(CompetitorTarget.plid == "100")) is None
        snapshot = conn.execute(select(CompetitorSnapshot.__table__)).mappings().one()
        assert snapshot["stock_quantity"] is None and not snapshot["stock_exact"]


def test_missing_own_identity_or_follower_probe_cannot_be_success(full):
    spec = own_spec(full)
    value = own_observation(full)
    value["offer_stocks"] = []
    with pytest.raises(ValueError, match="offer scope"):
        envelope_data = envelope(full.j, spec, obs=value)
        full.queue.submit(envelope_data[1], envelope_data[2])
    value = own_observation(full)
    value["stock"].update(quantity=0, exact=True)
    with pytest.raises(ValueError, match="Seller-API"):
        full.d.Observation.model_validate(value).complete(spec)
    spec["own_offer_scopes"] = []
    with pytest.raises(ValueError, match="identity unavailable"):
        full.d.Observation.model_validate(own_observation(full)).complete(spec)


def test_removed_own_identity_cannot_infer_a_missing_offer_has_zero_stock(full):
    spec = own_spec(full)
    value = own_observation(full)
    with full.engine.begin() as conn:
        conn.execute(OfferCurrent.__table__.delete().where(OfferCurrent.offer_id == "OWN-B"))
    _, body, digest = envelope(full.j, spec, obs=value)
    assert not full.queue.submit(body, digest)["succeeded"]
    with full.engine.connect() as conn:
        assert conn.scalar(select(CompetitorCollectionJob.status).where(
            CompetitorCollectionJob.id == spec["job_id"])) == "retry"


def test_resume_preserves_late_success_same_batch_and_current_store_permissions(full):
    spec = own_spec(full)
    batch = spec["batch_id"]
    full.controller.stop(batch)
    _, body, digest = envelope(full.j, spec, obs=own_observation(full))
    assert full.queue.submit(body, digest)["succeeded"]
    with pytest.raises(HTTPException) as blocked:
        full.controller.resume(batch, set())
    assert blocked.value.status_code == 409
    resumed = full.controller.resume(batch, {"allowed"})
    assert resumed["batch_id"] == batch and resumed["resumed"] == 30
    with full.engine.connect() as conn:
        assert conn.scalar(select(CompetitorCollectionJob.status).where(
            CompetitorCollectionJob.id == spec["job_id"])) == "succeeded"
        assert conn.scalar(select(func.count()).select_from(full.d.receipts)) == 1


def test_active_daily_roster_is_not_truncated_at_five_thousand(full):
    start(full)
    now = datetime.now(UTC)
    with full.engine.begin() as conn:
        conn.execute(insert(CompetitorCollectionJob), [dict(batch_id="blue-full-" + "b" * 32,
            item_index=i, plid=str(20000 + i), url=f"https://www.takealot.com/p/PLID{20000+i}",
            followers_only=False, with_stock_probe=True, status="succeeded", priority=0,
            attempt_count=1, max_attempts=2147483647, available_at=now, created_at=now, updated_at=now)
            for i in range(5001)])
    rows = full.queue.roster()
    assert len(rows) == 5031
    assert sum(row["status"] == "pending" for row in rows) == 31


def test_auxiliary_identity_refresh_preserves_legacy_task_keys(full):
    spec = own_spec(full)
    journal = full.j.Journal(full.root / "journal.sqlite", "main")
    journal.sync([spec])
    original = full.j.task_key(spec)
    spec["own_offer_scopes"].append("new-own-offer")
    journal.sync([spec])
    assert full.j.task_key(spec) == original
    assert "new-own-offer" in journal.candidate()["own_offer_scopes"]


def test_404_requires_spaced_control_evidence_and_late_failure_cannot_undo_health(full):
    from takealot_ops.competitors.repository import NOT_FOUND_CONFIRMATION_INTERVAL, NOT_FOUND_CONFIRMATION_COUNT
    start(full)
    spec = next(row for row in full.queue.roster() if row["plid"] == "1000")
    now = datetime.now(UTC)
    def submit(when, control):
        value, _, _ = envelope(full.j, spec, obs=None)
        value["error"] = f"not-found|{when.isoformat()}|{control}"
        body = full.j.canonical(value)
        return full.queue.submit(body, sha256(body.encode()).hexdigest())
    base = now - NOT_FOUND_CONFIRMATION_INTERVAL * (NOT_FOUND_CONFIRMATION_COUNT + 1)
    submit(base, "")
    submit(base, "1001")
    submit(base, "1001")
    with Session(full.engine) as session:
        assert session.get(CompetitorLinkHealth, "1000").confirmed_not_found_count == 1
    for i in range(1, NOT_FOUND_CONFIRMATION_COUNT):
        submit(base + NOT_FOUND_CONFIRMATION_INTERVAL * i, "1001")
    with Session(full.engine) as session:
        assert session.get(CompetitorCollectionJob, spec["job_id"]).status == "terminal"
        health = session.get(CompetitorLinkHealth, "1000")
        health.status, health.last_success_at, health.last_checked_at = "healthy", now, now
        health.confirmed_not_found_count = 0
        session.commit()
    submit(base, "1001")
    with Session(full.engine) as session:
        assert session.get(CompetitorLinkHealth, "1000").status == "healthy"


def test_http_full_start_and_legacy_controls_do_not_run_together(full, monkeypatch):
    app = FastAPI()
    app.state.session_cookie_name = "takealot_blue_stage_session"
    app.state.scheduled_competitor_runner = SimpleNamespace(start=lambda: None)
    local = {"active": True, "batch_id": "original-checkpoint"}
    app.add_api_route("/api/competitors/batch-status", lambda **kwargs: local, methods=["GET"])
    for path in ("collect", "batch-resume", "batch-events"):
        app.add_api_route("/api/competitors/" + path, lambda: {"original": True}, methods=["POST"])
    @app.middleware("http")
    async def identity(request, call_next):
        request.state.erp_user = SimpleNamespace(username="kxx", can=lambda _: True,
            accessible_stores=[SimpleNamespace(code="allowed", data_connected=True)])
        return await call_next(request)
    monkeypatch.setattr(full.j, "enabled", lambda _: True)
    full.m.install(app, full.root, full.engine)
    with TestClient(app) as client:
        path = "/api/competitors/distributed/start-full"
        assert client.post(path, json={"request_id": "a" * 32}).status_code == 409
        local["active"] = False
        assert client.post(path, json={"request_id": "a" * 32}).json()["total"] == 31
        for suffix, payload in (("collect", {}), ("batch-resume", {}), ("batch-events", {"event": "start"})):
            assert client.post("/api/competitors/" + suffix, json=payload).status_code == 409
        assert client.post("/api/competitors/batch-events", json={"event": "stop"}).status_code == 200


def test_worker_filters_all_own_offers_before_probe_and_persists_before_close(full, monkeypatch):
    spec = own_spec(full)
    value = own_observation(full)
    model = full.d.Observation.model_validate(value)
    events = []
    class Client:
        def __init__(self, **kwargs): pass
        async def fetch_product(self, url): return model.product
        async def fetch_all_reviews(self, plid): return model.reviews
        async def close(self): events.append("closed")
    class Collector:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): events.append("cleanup")
        async def _collect_product_stocks(self, product, **kwargs):
            assert kwargs["followers_only"] and kwargs["visible_browser"] is False
            assert [offer.offer_id for offer in product.offers] == ["FOLLOWER"]
            return [], model.offer_stocks
    monkeypatch.setattr(full.w, "CompetitorPublicClient", Client)
    monkeypatch.setattr(full.w, "CompetitorCollector", Collector)
    captured = []
    def persist(value):
        captured.append(value)
        events.append("persisted")
    asyncio.run(full.w.collect_observation(spec, full.root, "", persist))
    assert events == ["persisted", "cleanup", "closed"]
    assert full.d.Observation.model_validate(captured[0]).complete(spec)
