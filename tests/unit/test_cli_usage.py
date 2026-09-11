from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select, update

from takealot_ops.search_ranking.cli_usage import (
    batch_usage_actor, current_usage_actor, start_usage_attempt, usage_actor,
    usage_operation, usage_summary,
)
from takealot_ops.search_ranking.codex_cli import (
    CodexAppServerClient, CodexCliProviderError, CodexCliQuotaExceededError,
    CodexWeeklyQuotaGuard,
)
from takealot_ops.storage.migrations import create_engine_for_database_url
from takealot_ops.storage.models import Base, ErpCliUsageEvent, ErpStore, ErpUser
from takealot_ops.storage.store_context import store_scope


@pytest.fixture
def database(tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'usage.db').as_posix()}"
    engine = create_engine_for_database_url(url)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for user_id, name in [(1, "alice"), (2, "bob")]:
            conn.execute(insert(ErpUser).values(id=user_id, username=name, display_name=name,
                password_hash="unused", role="operator", active=True,
                created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1)))
    yield url, engine
    engine.dispose()


class ProtocolClient(CodexAppServerClient):
    def __init__(self, path: Path, *, user: str, tokens: int | None = 100,
                 failure: str | None = None, blocked: bool = False):
        super().__init__(path/'codex', project_root=path,
                         quota_guard=CodexWeeklyQuotaGuard(path/'quota.json'), timeout_seconds=1)
        self.turn_id = f"turn-{user}"
        self.failure = failure
        self.blocked = blocked
        self.methods: list[str] = []
        self.messages: list[dict[str, Any]] = []
        if tokens is not None:
            usage = {"method": "thread/tokenUsage/updated", "params": {
                "turnId": self.turn_id, "tokenUsage": {"last": {
                    "inputTokens": tokens-20, "cachedInputTokens": 30,
                    "outputTokens": 20, "reasoningOutputTokens": 10, "totalTokens": tokens,
                }},
            }}
            self.messages.extend([usage, usage])  # Repeated notification must not add spend.
        self.messages.extend([
            {"method": "item/completed", "params": {"turnId": self.turn_id, "item": {
                "type": "agentMessage", "phase": "final_answer",
                "text": "invalid-json" if failure == "json" else '{"ok":true}',
            }}},
            {"method": "turn/completed", "params": {"threadId": f"thread-{user}", "turn": {
                "id": self.turn_id, "status": "failed" if failure == "provider" else "completed",
            }}},
        ])

    async def _refresh_quota(self):
        return {"status": "exhausted" if self.blocked else "active", "current_used_percent": 100 if self.blocked else 95}

    async def _request(self, method, params):
        self.methods.append(method)
        await asyncio.sleep(0)
        if method == "thread/start":
            return {"thread": {"id": self.turn_id.replace("turn", "thread"), "modelProvider": "openai"}}
        if method == "turn/start":
            return {"turn": {"id": self.turn_id}}
        raise AssertionError(method)

    async def _next_message(self):
        await asyncio.sleep(0)
        if self.failure == "cancel" and len(self.messages) <= 2:
            raise asyncio.CancelledError()
        return self.messages.pop(0)


async def run(client: ProtocolClient):
    return await client.run_structured_turn(stage="image_title_fusion", system_prompt="Return JSON",
        user_text="Synthetic regression input", image_path=None,
        output_schema={"type":"object", "properties":{"ok":{"type":"boolean"}}})


def test_interleaved_accounts_stores_and_repeated_notifications_are_independent(database, tmp_path):
    url, engine = database
    async def account(uid, name, store, tokens):
        with usage_actor(uid, name), store_scope(store), usage_operation(url, f"offer-{uid}"):
            await run(ProtocolClient(tmp_path, user=name, tokens=tokens))
    async def both():
        await asyncio.gather(account(1, "alice", "store-a", 100), account(2, "bob", "store-b", 200))
    asyncio.run(both())
    assert current_usage_actor().user_id is None
    alice = usage_summary(engine, user_id=1, period="all")
    bob = usage_summary(engine, user_id=2, period="all")
    assert (alice["total"]["total_tokens"], bob["total"]["total_tokens"]) == (100, 200)
    assert alice["total"]["requests"] == bob["total"]["requests"] == 1
    assert alice["total"]["input_tokens"] == 80  # Cached 30 is a subset, not an extra charge.
    assert alice["total"]["cached_input_tokens"] == 30
    assert [row["store_code"] for row in alice["by_store"]] == ["store-a"]
    assert {row["actor_user_id"] for row in alice["recent"]} == {1}
    assert usage_summary(engine, user_id=None, period="all")["total"]["total_tokens"] == 300


@pytest.mark.parametrize("failure", ["provider", "json", "cancel"])
def test_partial_usage_survives_provider_json_and_cancellation_failures(database, tmp_path, failure):
    url, engine = database
    with usage_actor(1, "alice"), usage_operation(url, "offer-1"):
        with pytest.raises((CodexCliProviderError, asyncio.CancelledError)):
            asyncio.run(run(ProtocolClient(tmp_path, user="alice", failure=failure)))
    total = usage_summary(engine, user_id=1, period="all")["total"]
    assert total["failed"] == total["requests"] == 1
    assert total["total_tokens"] == 100 and total["unknown_usage_requests"] == 0


def test_blocked_preflight_is_not_counted_as_a_model_request(database, tmp_path):
    url, engine = database
    client = ProtocolClient(tmp_path, user="alice", blocked=True)
    with usage_actor(1, "alice"), usage_operation(url, "offer-1"):
        with pytest.raises(CodexCliQuotaExceededError):
            asyncio.run(run(client))
    total = usage_summary(engine, user_id=1, period="all")["total"]
    assert total["blocked"] == 1
    assert total["requests"] == total["unknown_usage_requests"] == total["total_tokens"] == 0
    assert client.methods == []


def test_missing_usage_is_unknown_and_not_replaced_by_zero(database, tmp_path):
    url, engine = database
    with usage_actor(1, "alice"), usage_operation(url, "offer-1"):
        asyncio.run(run(ProtocolClient(tmp_path, user="alice", tokens=None)))
    result = usage_summary(engine, user_id=1, period="all")
    assert result["total"]["unknown_usage_requests"] == 1
    assert result["total"]["input_tokens"] is None
    assert result["total"]["total_tokens"] is None
    assert result["recent"][0]["total_tokens"] is None


def test_batch_owner_overrides_resuming_admin_and_old_names_are_time_checked(database):
    url, engine = database
    for state, expected in [
        ({"owner_user_id":1, "owner_username":"alice"}, 1),
        ({"owner_username":"alice", "created_at":"2026-08-01T00:00:00Z"}, 1),
        ({"owner_username":"alice", "created_at":"2025-08-01T00:00:00Z"}, None),
    ]:
        with usage_actor(2, "bob"), batch_usage_actor(state | {"batch_id":"batch-old"}), usage_operation(url,"offer"):
            attempt = start_usage_attempt(model="gpt-5.6-sol", stage="image_title_fusion")
            attempt.dispatch()
            attempt.observe({"inputTokens":80,"outputTokens":20,"totalTokens":100})
            attempt.finish(error=None)
            with engine.connect() as conn:
                assert conn.execute(select(ErpCliUsageEvent.actor_user_id).where(ErpCliUsageEvent.id==attempt.id)).scalar() == expected
    assert usage_summary(engine, user_id=2, period="all")["total"]["requests"] == 0
    assert usage_summary(engine, user_id=1, period="all")["total"]["requests"] == 2


def test_cache_read_does_not_create_a_request_and_unfinished_usage_stays_visible(database):
    url, engine = database
    with usage_actor(1, "alice"), usage_operation(url, "cached-offer"):
        pass  # A reused result bypasses the CLI transport entirely.
    assert usage_summary(engine, user_id=1, period="all")["total"]["requests"] == 0
    with usage_actor(1, "alice"), usage_operation(url, "unfinished-offer"):
        attempt = start_usage_attempt(model="gpt-5.6-sol", stage="image_title_fusion")
        attempt.dispatch()
        attempt.observe({"totalTokens":120})
        attempt.engine.dispose()  # Simulate a process ending before finalization.
    total = usage_summary(engine, user_id=1, period="all")["total"]
    assert total["unfinished"] == 1 and total["total_tokens"] == 120
    assert total["input_tokens"] is None and total["unknown_usage_requests"] == 1


def test_out_of_order_reports_and_repeated_finalization_do_not_change_totals(database):
    url, engine = database
    with usage_actor(1, "alice"), usage_operation(url, "offer"):
        attempt = start_usage_attempt(model="gpt-5.6-sol", stage="image_title_fusion")
        attempt.dispatch()
        attempt.observe({"inputTokens":80,"outputTokens":20,"totalTokens":100})
        attempt.observe({"inputTokens":70,"outputTokens":10,"totalTokens":80})
        attempt.finish(error=None)
        attempt.observe({"totalTokens":200})
        attempt.finish(error=RuntimeError("late callback"))
    total = usage_summary(engine,user_id=1,period="all")["total"]
    assert total["requests"] == total["completed"] == 1
    assert total["total_tokens"] == 100 and total["failed"] == 0


def test_unavailable_ledger_stops_before_any_cli_request(tmp_path):
    url = f"sqlite:///{(tmp_path/'missing-schema.db').as_posix()}"
    client = ProtocolClient(tmp_path,user="alice")
    with usage_actor(1,"alice"), usage_operation(url,"offer"):
        from sqlalchemy.exc import OperationalError
        with pytest.raises(OperationalError):
            asyncio.run(run(client))
    assert client.methods == []


def test_period_uses_beijing_midnight_and_does_not_expose_other_account_first_time(database):
    url, engine = database
    for uid, moment in [(1, datetime(2026,9,11,15,59)), (2, datetime(2026,9,11,16,0))]:
        with usage_actor(uid, "alice" if uid==1 else "bob"), usage_operation(url, "offer"):
            attempt = start_usage_attempt(model="gpt-5.6-sol", stage="image_title_fusion")
            attempt.dispatch()
            attempt.observe({"totalTokens":100})
            attempt.finish(error=None)
        with engine.begin() as conn:
            conn.execute(update(ErpCliUsageEvent).where(ErpCliUsageEvent.id==attempt.id).values(created_at=moment))
    today = datetime(2026,9,12,1,tzinfo=UTC)
    assert usage_summary(engine,user_id=1,period="today",now=today)["total"]["requests"] == 0
    result = usage_summary(engine,user_id=2,period="today",now=today)
    assert result["total"]["requests"] == 1
    assert result["first_recorded_at"] == "2026-09-11T16:00:00+00:00"


def test_usage_api_requires_authentication_and_user_management_for_other_accounts(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from takealot_ops.erp.web import create_app

    url = f"sqlite:///{(tmp_path/'api.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", url)
    monkeypatch.setenv("TAKEALOT_WEB_ONLY", "1")
    app = create_app(tmp_path)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/erp/cli-usage").status_code == 401
        bootstrap = client.post("/api/auth/bootstrap", json={"username":"kxx", "display_name":"Admin", "password":"test-pass-123"})
        assert bootstrap.status_code == 200
        admin_id = bootstrap.json()["user"]["id"]
        created = client.post("/api/auth/users", headers={"X-CSRF-Token":bootstrap.json()["csrf_token"]},
            json={"username":"usage-only", "display_name":"Usage", "password":"test-pass-456",
                  "role":"selection", "permissions":["competitors.view"], "all_stores":False,"store_ids":[]})
        assert created.status_code == 200
        accounts = client.get("/api/auth/users").json()["items"]
        member_id = next(row["id"] for row in accounts if row["username"] == "usage-only")
        for uid, name, tokens in [(admin_id,"kxx",80), (member_id,"usage-only",40)]:
            with usage_actor(uid,name), usage_operation(url,"offer"):
                attempt = start_usage_attempt(model="gpt-5.6-sol",stage="image_title_fusion")
                attempt.dispatch()
                attempt.observe({"inputTokens":tokens-10,"outputTokens":10,"totalTokens":tokens})
                attempt.finish(error=None)
        overview = client.get("/api/erp/cli-usage?all_users=true")
        assert overview.status_code == 200 and overview.json()["total"]["total_tokens"] == 120
        assert client.get(f"/api/erp/cli-usage?all_users=true&user_id={admin_id}").status_code == 422
        assert client.get("/api/erp/cli-usage?user_id=999999").status_code == 404
        login = client.post("/api/auth/login", json={"username":"usage-only", "password":"test-pass-456"})
        assert login.status_code == 200
        own = client.get("/api/erp/cli-usage")
        assert own.status_code == 200
        assert len(own.json()["by_user"]) == 1
        assert own.json()["by_user"][0]["username"] == "usage-only"
        assert own.json()["total"]["total_tokens"] == 40
        assert {row["actor_user_id"] for row in own.json()["recent"]} == {member_id}
        assert client.get(f"/api/erp/cli-usage?user_id={admin_id}").status_code == 403
        assert client.get("/api/erp/cli-usage?all_users=true").status_code == 403
        assert client.get("/api/erp/cli-usage?period=invalid").status_code == 422


def test_analyze_http_request_stamps_authenticated_user_and_selected_store(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from takealot_ops.erp.web import create_app
    from takealot_ops.search_ranking.service import SearchRankingProviderError

    url = f"sqlite:///{(tmp_path/'request.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", url)
    monkeypatch.setenv("TAKEALOT_WEB_ONLY", "1")
    app = create_app(tmp_path)
    model = ProtocolClient(tmp_path, user="request", tokens=90, failure="provider")
    async def analyze(offer_id):
        with usage_operation(url, offer_id):
            try:
                await run(model)
            except CodexCliProviderError as exc:
                raise SearchRankingProviderError("simulated provider failure") from exc
    monkeypatch.setattr(app.state.search_ranking_service, "analyze_offer", analyze)
    with TestClient(app, client=("127.0.0.1",50000)) as client:
        issued = client.post("/api/auth/bootstrap",json={"username":"kxx","display_name":"Admin","password":"test-pass-123"}).json()
        engine = create_engine_for_database_url(url)
        with engine.begin() as connection:
            connection.execute(insert(ErpStore).values(code="second",display_name="Second",active=True,
                data_connected=True,created_at=datetime(2026,1,1),updated_at=datetime(2026,1,1)))
        engine.dispose()
        response = client.post("/api/erp/search-ranking/offer-test/analyze",json={"actor_user_id":99999},
            headers={"X-CSRF-Token":issued["csrf_token"],"X-Store-Code":"second"})
        assert response.status_code == 502
        result = client.get("/api/erp/cli-usage?period=all").json()
        assert result["total"]["total_tokens"] == 90
        assert result["total"]["failed"] == 1
        assert result["recent"][0]["actor_user_id"] == issued["user"]["id"]
        assert result["recent"][0]["store_code"] == "second"
        assert client.post("/api/erp/search-ranking/offer-test/analyze").status_code == 403
        assert client.get("/api/erp/cli-usage").json()["total"]["requests"] == 1
        assert model.methods == ["thread/start","turn/start"]


def test_empty_or_invalid_usage_notifications_remain_unknown(database):
    url, engine = database
    with usage_actor(1, "alice"), usage_operation(url, "offer"):
        attempt = start_usage_attempt(model="gpt-5.6-sol", stage="image_title_fusion")
        attempt.dispatch()
        attempt.observe({})
        attempt.observe({"inputTokens": True, "outputTokens": -1, "totalTokens": "100"})
        attempt.finish(error=None)
    total = usage_summary(engine, user_id=1, period="all")["total"]
    assert total["requests"] == total["completed"] == total["unknown_usage_requests"] == 1
    assert total["total_tokens"] is None
