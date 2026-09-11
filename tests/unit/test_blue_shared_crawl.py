from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import importlib
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, insert, select

from takealot_ops.competitors.distributed_queue import DistributedCompetitorQueue, DistributedJobTarget
from takealot_ops.storage.models import Base, CompetitorCollectionJob, CompetitorTarget


@pytest.fixture
def shared(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "scripts/ha"))
    module = importlib.import_module("blue_shared_crawl")
    engine = create_engine(f"sqlite:///{tmp_path / 'blue.sqlite'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(insert(CompetitorTarget), [dict(
            plid=plid, url=f"https://www.takealot.com/p/PLID{plid}",
            active=True, created_at=now, updated_at=now,
        ) for plid in ("100", "200")])
    monkeypatch.setattr(module, "connected_store_plids", lambda _: set())

    @contextmanager
    def local_test_lock(conn):
        yield

    monkeypatch.setattr(module, "dispatch_lock", local_test_lock)
    yield module, module.BlueSharedCrawl(engine), engine
    engine.dispose()


def payload(module, plids=None, token="a"):
    return module.StartRequest(request_id=token * 32, plids=plids or ["100", "200"])


def test_shared_start_is_atomic_and_idempotent(shared):
    module, controller, engine = shared
    first = controller.start(payload(module))
    assert first == {"batch_id": "blue-web-" + "a" * 32, "total": 2, "reused": False}
    assert controller.start(payload(module))["reused"] is True
    with engine.connect() as conn:
        rows = conn.execute(select(CompetitorCollectionJob.__table__)).mappings().all()
    assert len(rows) == 2
    assert {row["max_attempts"] for row in rows} == {1}
    assert all(row["with_stock_probe"] and not row["followers_only"] for row in rows)


def test_second_node_cannot_start_overlapping_batch(shared):
    module, controller, engine = shared
    controller.start(payload(module))
    with pytest.raises(HTTPException) as failure:
        module.BlueSharedCrawl(engine).start(payload(module, token="b"))
    assert failure.value.status_code == 409


def test_same_request_cannot_change_targets(shared):
    module, controller, _ = shared
    controller.start(payload(module))
    with pytest.raises(HTTPException) as failure:
        controller.start(payload(module, ["100"]))
    assert failure.value.status_code == 409


@pytest.mark.parametrize("plids", [["999"], ["100", "999"]])
def test_unknown_targets_do_not_partially_enqueue(shared, plids):
    module, controller, engine = shared
    with pytest.raises(HTTPException) as failure:
        controller.start(payload(module, plids))
    assert failure.value.status_code == 422
    with engine.connect() as conn:
        assert conn.execute(select(CompetitorCollectionJob.id)).first() is None


def test_own_store_is_not_silently_included(shared, monkeypatch):
    module, controller, _ = shared
    monkeypatch.setattr(module, "connected_store_plids", lambda _: {"100"})
    with pytest.raises(HTTPException) as failure:
        controller.start(payload(module))
    assert failure.value.status_code == 422


def test_stop_cancels_pending_but_drains_current(shared):
    module, controller, engine = shared
    batch = controller.start(payload(module))["batch_id"]
    queue = DistributedCompetitorQueue(engine)
    lease = queue.claim("main", batch_id=batch)
    assert lease is not None
    assert controller.stop(batch)["cancelled_pending"] == 1
    assert controller.stop(batch)["cancelled_pending"] == 0
    assert queue.claim("laptop", batch_id=batch) is None
    status = queue.batch_status(batch)
    assert status.leased == 1 and status.cancelled == 1


def test_last_attempt_expiry_is_terminal_not_infinite_reclaim(shared):
    _, _, engine = shared
    now = [datetime.now(UTC)]
    queue = DistributedCompetitorQueue(engine, clock=lambda: now[0])
    queue.enqueue("expired", [DistributedJobTarget(0, "100", "url", max_attempts=1)])
    assert queue.claim("main", batch_id="expired", lease_seconds=30) is not None
    now[0] += timedelta(seconds=31)
    assert queue.claim("laptop", batch_id="expired") is None
    assert queue.batch_status("expired").terminal == 1


@pytest.mark.parametrize("values", [[], ["123"] * 21, ["https://example.com"], ["-1"]])
def test_input_boundaries(shared, values):
    module, _, _ = shared
    with pytest.raises(ValueError):
        module.normalized_plids(values)


@pytest.mark.parametrize("username,permission,status", [
    ("someone", True, 403), ("kxx", False, 403), (None, True, 401),
])
def test_operator_gate(shared, username, permission, status):
    module, _, _ = shared
    user = None if username is None else SimpleNamespace(username=username, can=lambda _: permission)
    with pytest.raises(HTTPException) as failure:
        module.require_operator(SimpleNamespace(state=SimpleNamespace(erp_user=user)), write=True)
    assert failure.value.status_code == status


def test_http_start_same_queue_manual_preserved_and_only_schedule_disabled(shared, tmp_path):
    module, _, engine = shared
    app = FastAPI()
    app.state.session_cookie_name = "takealot_blue_stage_session"
    app.state.scheduled_competitor_runner = SimpleNamespace(start=lambda: pytest.fail("local driver"))
    manual_calls = []

    def original_manual_route():
        manual_calls.append(True)
        return {"original_manual": True}

    original_routes = ("/api/competitors/collect", "/api/competitors/batch-events",
                       "/api/competitors/batch-resume")
    for route in original_routes:
        app.add_api_route(route, original_manual_route, methods=["POST"])

    @app.middleware("http")
    async def fake_test_identity(request, call_next):
        request.state.erp_user = SimpleNamespace(username="kxx", can=lambda _: True)
        return await call_next(request)

    module.install(app, tmp_path, engine)
    with TestClient(app) as client:
        first = client.post("/api/competitors/distributed/start", json=payload(module).model_dump())
        assert first.status_code == 200
        again = client.post("/api/competitors/distributed/start", json=payload(module).model_dump())
        assert again.json()["reused"] is True
        for route in original_routes:
            response = client.post(route, json={})
            assert response.status_code == 200
            assert response.json() == {"original_manual": True}
        assert len(manual_calls) == len(original_routes)
        assert client.post("/api/internal/competitors/scheduled-trigger", json={}).status_code == 409
        app.state.scheduled_competitor_runner.start()


def test_install_refuses_green(shared, tmp_path):
    module, _, engine = shared
    app = FastAPI()
    app.state.session_cookie_name = "takealot_erp_session"
    with pytest.raises(RuntimeError, match="outside BLUE"):
        module.install(app, tmp_path, engine)


def test_real_erp_cookie_and_csrf_middleware_protect_new_routes(shared, tmp_path, monkeypatch):
    from takealot_ops.erp.web import create_app

    module, _, engine = shared
    root = Path(__file__).resolve().parents[2]
    (tmp_path / "config").mkdir()
    for name in ("anomaly_rules.yaml", "sale_status_rules.yaml"):
        (tmp_path / "config" / name).write_bytes((root / "config" / name).read_bytes())
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", str(engine.url))
    monkeypatch.setenv("TAKEALOT_WEB_ONLY", "1")
    monkeypatch.setenv("TAKEALOT_READ_ONLY_TEST_MODE", "0")
    monkeypatch.setenv("TAKEALOT_SESSION_COOKIE_NAME", "takealot_blue_stage_session")
    app = create_app(tmp_path)
    module.install(app, tmp_path, engine)
    calls = []
    monkeypatch.setattr(app.state.blue_shared_crawl, "start",
                        lambda request: calls.append(request) or {"accepted": True})
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        url = "/api/competitors/distributed/start"
        assert client.post(url, json=payload(module).model_dump()).status_code == 401
        login = client.post("/api/auth/bootstrap", json={
            "username": "kxx", "display_name": "Isolated test", "password": "isolated-test-password",
        })
        assert login.status_code == 200
        assert client.cookies.get("takealot_blue_stage_session")
        assert client.post(url, json=payload(module).model_dump()).status_code == 403
        assert calls == []
        response = client.post(url, json=payload(module).model_dump(),
                               headers={"X-CSRF-Token": login.json()["csrf_token"]})
        assert response.status_code == 200
        assert len(calls) == 1
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        full_calls = []
        monkeypatch.setattr(app.state.blue_shared_crawl, "start_full",
                            lambda payload, stores: full_calls.append(stores) or {"accepted": True})
        full_url = "/api/competitors/distributed/start-full"
        assert client.post(full_url, json={"request_id": "f" * 32}).status_code == 403
        assert full_calls == []
        assert client.post(full_url, headers=headers, json={"request_id": "f" * 32}).status_code == 200
        assert len(full_calls) == 1
        manual = client.post("/api/competitors/batch-events", headers=headers, json={
            "batch_id": "blue-manual-isolated", "client_id": "isolated-client",
            "event": "start", "completed": 0, "total": 1, "pending": 1,
        })
        assert manual.status_code == 200
        assert manual.json()["status"]["batch_id"] == "blue-manual-isolated"
        # Empty input reaches the original schema validator, not the removed
        # blanket 409 blocker; this never starts an external collection request.
        assert client.post("/api/competitors/collect", headers=headers, json={}).status_code == 422
        assert client.post("/api/internal/competitors/scheduled-trigger", headers=headers,
                           json={}).status_code == 409
