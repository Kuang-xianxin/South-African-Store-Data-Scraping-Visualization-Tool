"""Scope sharing, incremental store revisions, and release interruption boundaries."""
import asyncio
import importlib.util
import json
import os
import time
from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import insert, update
from starlette.responses import StreamingResponse

from takealot_ops.erp.radar_materialized import radar_fingerprints
from takealot_ops.erp.release_gate import ReleaseDrainMiddleware, ReleaseGate
from takealot_ops.erp.web import create_app
from takealot_ops.storage.migrations import create_engine_for_database_url
from takealot_ops.storage.models import Base, ErpStore, ErpUser, OfferCurrent, StoreOfferBaseline
from test_radar_materialized import query, setup_cache


def test_store_update_rebuilds_only_current_or_historical_contributors(tmp_path):
    engine = create_engine_for_database_url(f"sqlite:///{tmp_path / 'data.db'}")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 11)
    with engine.begin() as db:
        db.execute(insert(ErpStore.__table__), [dict(id=i, code=code, display_name=code,
            active=True, data_connected=True, created_at=now, updated_at=now)
            for i, code in enumerate(("one", "two", "three"), 1)])
        db.execute(insert(OfferCurrent.__table__), [dict(store_code=code, offer_id=p,
            productline_id=p, captured_at=now) for code, p in (("one", "1"), ("two", "2"), ("one", "3"))])
        db.execute(insert(StoreOfferBaseline.__table__).values(store_code="two", offer_id="old",
            productline_id="3", display_date=date(2026, 9, 1), captured_at=now))
    versions = {"one": "1", "two": "1", "three": "1"}
    def fingerprints():
        return radar_fingerprints(engine, own=True, store_codes=set(versions),
                                  store_version="model/day/range", store_versions=versions)
    first = fingerprints()
    versions["two"] = "2"
    second = fingerprints()
    assert {p for p in ("1", "2", "3") if first[p] != second[p]} == {"2", "3"}
    cache, _, _, revision, calls, options = setup_cache(tmp_path, 3)
    options["fingerprints"] = fingerprints
    try:
        cache.page(**options, query=query(), prefer_cached=False)
        calls.clear()
        versions["three"] = "2"  # A changed store with no contributing product.
        revision[0] = "2"
        cache.page(**options, query=query(), prefer_cached=False)
        assert not calls
        versions["one"] = "2"
        revision[0] = "3"
        cache.page(**options, query=query(), prefer_cached=False)
        assert calls == [{"1", "3"}]
    finally:
        cache.close()
        engine.dispose()


def test_http_users_share_only_identical_effective_scopes_and_revocation_blocks_read(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'api.db'}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", url)
    monkeypatch.setenv("TAKEALOT_WEB_ONLY", "1")
    app = create_app(tmp_path)
    captures = []
    def page(**kwargs):
        captures.append(kwargs)
        return {"store_items": [], "date_range": {}, "pagination": {"total": 0}}, False, 1
    monkeypatch.setattr(app.state.radar_own_materialized, "page", page)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        assert client.post("/api/auth/bootstrap", json={"username": "kxx", "display_name": "Admin",
                           "password": "test-pass-123"}).status_code == 200
        endpoint = "/api/competitors/own-store?own_store_scope=all&page=1"
        assert client.get(endpoint).status_code == 200
        other = app.state.auth_manager.create_user(username="another", display_name="Other",
            password="test-pass-123", role="viewer", all_stores=True)
        assert client.post("/api/auth/login", json={"username": "another", "password": "test-pass-123"}).status_code == 200
        assert client.get(endpoint).status_code == 200
        assert captures[0]["key"] == captures[1]["key"]
        assert captures[0]["boundary"] == captures[1]["boundary"]
        engine = create_engine_for_database_url(url)
        with engine.begin() as db:
            db.execute(update(ErpUser.__table__).where(ErpUser.id == other["id"]).values(active=False))
        engine.dispose()
        assert client.get(endpoint).status_code == 401
        assert len(captures) == 2


async def test_drain_waits_for_response_body_and_never_replays_mutations(tmp_path):
    import httpx
    gate = ReleaseGate(tmp_path / "lease")
    app = FastAPI()
    entered, finish = asyncio.Event(), asyncio.Event()
    effects = []
    @app.post("/work")
    async def work():
        effects.append("one write")
        async def stream():
            entered.set()
            yield b"first"
            await finish.wait()
            yield b"last"
        return StreamingResponse(stream())
    app.add_middleware(ReleaseDrainMiddleware, gate=gate)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        pending = asyncio.create_task(client.post("/work"))
        await entered.wait()
        gate.path.touch()
        assert gate.active_requests == 1
        denied = await client.post("/work")
        assert denied.status_code == 503 and denied.headers["retry-after"] == "3"
        assert effects == ["one write"]
        finish.set()
        assert (await pending).content == b"firstlast"
        assert gate.active_requests == 0
        os.utime(gate.path, (time.time() - 16, time.time() - 16))
        assert not gate.held()  # Crashed release helper cannot pause work forever.


async def test_collector_release_hold_keeps_persisted_result_queue_and_revision(tmp_path):
    from takealot_ops.competitors.batch import CollectionBatchRegistry
    from takealot_ops.competitors.scheduled import ScheduledCollectionAttempt, ScheduledCollectionTarget
    from test_scheduled_competitor_batch import _runner, _wait_for
    hold = [False]
    completed = []
    async def targets():
        return [ScheduledCollectionTarget(p, f"https://takealot.com/p/PLID{p}") for p in ("11111111", "22222222")]
    async def collect(url, *_):
        plid = url.rsplit("PLID", 1)[1]
        completed.append(plid)
        hold[0] = len(completed) == 1
        return ScheduledCollectionAttempt(plid=plid, title=plid, message="ok", succeeded=True)
    runner = _runner(tmp_path, registry=CollectionBatchRegistry(), load_targets=targets, collect_target=collect)
    runner._suspend_dispatch = lambda: hold[0]
    try:
        await runner.trigger()
        await _wait_for(lambda: len(completed) == 1 and runner._state["active_item"] is None)
        before = json.loads((tmp_path / "scheduled.json").read_text())
        await asyncio.sleep(0.03)
        after = json.loads((tmp_path / "scheduled.json").read_text())
        assert after == before and len(after["queue"]) == 1
        hold[0] = False
        await _wait_for(lambda: len(completed) == 2)
        assert runner._state["batch_id"] == before["batch_id"]
        assert runner._state["run_revision"] == before["run_revision"]
    finally:
        await runner.close()


def test_bridge_routes_reads_only_and_rejects_unexpected_config():
    spec = importlib.util.spec_from_file_location("green_bridge", Path(__file__).parents[2] / "scripts/green_release_bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = "server { location / { proxy_pass http://takealot_erp_ha; } }"
    changed = module.bridge_config(original, 18507)
    assert "default takealot_erp_ha;" in changed
    import re
    route = re.search(r"~(.*?) takealot_release_read;", changed).group(1)
    assert re.search(route, "GET:/api/competitors/own-store")
    assert re.search(route, "HEAD:/assets/app.js")
    assert not re.search(route, "GET:/api/erp/search-ranking/batch/status")
    assert not re.search(route, "GET:/api/competitors/batch-status")
    assert not re.search(route, "POST:/api/competitors/own-store")
    assert "POST takealot_release_read" not in changed
    with pytest.raises(RuntimeError):
        module.bridge_config(changed, 18507)


def test_standby_must_finish_preparation_before_readiness_and_blocks_writes(tmp_path, monkeypatch):
    import takealot_ops.erp.web as web
    import takealot_ops.erp.restart_standby as standby
    ready = tmp_path / "ready.json"
    monkeypatch.setenv("TAKEALOT_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("TAKEALOT_RELEASE_READY", str(ready))
    monkeypatch.setenv("TAKEALOT_WEB_ONLY", "1")
    monkeypatch.setenv("TAKEALOT_READ_ONLY_TEST_MODE", "1")
    monkeypatch.setattr(standby, "materialized_code_fingerprint", lambda _: "a" * 64)
    monkeypatch.setattr(web, "create_app", lambda *args, **kwargs: web.app)
    failed = FastAPI()
    def fail():
        raise RuntimeError("prepare failure")
    failed.state.prepare_radar_release = fail
    monkeypatch.setattr(web, "app", failed)
    with pytest.raises(RuntimeError, match="prepare failure"), TestClient(standby.create_standby()):
        pass
    assert not ready.exists()
    prepared = FastAPI()
    prepared.state.prepare_radar_release = lambda: [{"total": 42}]
    monkeypatch.setattr(web, "app", prepared)
    with TestClient(standby.create_standby()) as client:
        assert json.loads(ready.read_text())["scopes"] == [{"total": 42}]
        assert client.get("/api/health").json()["mode"] == "release-read-bridge"
        assert client.post("/api/refresh").status_code == 503


def test_release_reader_accepts_live_sessions_without_renewal_and_obeys_revocation(tmp_path, monkeypatch):
    from sqlalchemy import delete, select
    from takealot_ops.erp.auth import AuthManager
    from takealot_ops.erp.restart_standby import create_standby
    from takealot_ops.storage.models import ErpSession
    url = f"sqlite:///{tmp_path / 'shared.db'}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", url)
    monkeypatch.setenv("TAKEALOT_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("TAKEALOT_WEB_ONLY", "1")
    monkeypatch.delenv("TAKEALOT_READ_ONLY_TEST_MODE", raising=False)
    normal = create_app(tmp_path)
    with TestClient(normal, client=("127.0.0.1", 5000)) as client:
        assert client.post("/api/auth/bootstrap", json={"username": "kxx", "display_name": "Admin",
                           "password": "test-pass-123"}).status_code == 200
        cookie = client.cookies.get("takealot_erp_session")
    engine = create_engine_for_database_url(url)
    with engine.connect() as db:
        before = list(db.execute(select(ErpSession.__table__)).tuples())
    blue = AuthManager(tmp_path, read_only_test_mode=True)
    try:
        assert blue.resolve_session(cookie) is None  # Existing BLUE isolation remains.
    finally:
        blue.close()
    monkeypatch.setenv("TAKEALOT_READ_ONLY_TEST_MODE", "1")
    monkeypatch.setenv("TAKEALOT_RELEASE_READY", str(tmp_path / "ready.json"))
    reader = create_standby()
    reader.state.prepare_radar_release = lambda: []
    try:
        with TestClient(reader) as client:
            client.cookies.set("takealot_erp_session", cookie)
            assert client.get("/api/auth/session").status_code == 200
            assert client.get("/api/competitors/own-store?own_store_scope=all&page=1").status_code == 200
            with engine.connect() as db:
                assert list(db.execute(select(ErpSession.__table__)).tuples()) == before
            with engine.begin() as db:
                db.execute(delete(ErpSession.__table__))
            assert client.get("/api/auth/session").status_code == 401
    finally:
        engine.dispose()


def test_preparation_copy_ignores_uncommitted_writes_and_does_not_share_writer(tmp_path):
    import sqlite3
    from takealot_ops.erp.release_cache import copy_radar_cache
    live, prepared = tmp_path / "live", tmp_path / "prepared"
    live.mkdir()
    namespace = "a" * 64
    name = f"radar-own-{namespace}.sqlite3"
    writer = sqlite3.connect(live / name)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE rows (value INTEGER)")
        writer.execute("INSERT INTO rows VALUES (1)")
        writer.commit()
        writer.execute("INSERT INTO rows VALUES (2)")
        assert copy_radar_cache(live, prepared, namespace) == [name]
        with sqlite3.connect(prepared / name) as candidate:
            assert candidate.execute("SELECT value FROM rows").fetchall() == [(1,)]
            candidate.execute("INSERT INTO rows VALUES (3)")
        writer.commit()
        assert writer.execute("SELECT value FROM rows").fetchall() == [(1,), (2,)]
    finally:
        writer.close()
    with pytest.raises(ValueError):
        copy_radar_cache(live, live, namespace)
    with pytest.raises(ValueError):
        copy_radar_cache(live, prepared, "../invalid")
