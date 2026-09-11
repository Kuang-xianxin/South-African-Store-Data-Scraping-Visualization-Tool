"""Exercise HTTP paging against actual persisted snapshots and authorization."""
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from takealot_ops.erp.web import create_app
from takealot_ops.storage.models import CompetitorSnapshot, CompetitorTarget


def test_next_day_reuses_true_competitor_history_but_rebuilds_own_day(tmp_path, monkeypatch):
    import sqlite3
    from takealot_ops.erp import web

    day = [9]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, day[0], 12, tzinfo=tz)

    monkeypatch.setenv("TAKEALOT_DATABASE_URL", f"sqlite:///{(tmp_path / 'source.db').as_posix()}")
    monkeypatch.setenv("TAKEALOT_WEB_ONLY", "1")
    monkeypatch.setattr(web, "datetime", Clock)
    app = create_app(tmp_path)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.post("/api/auth/bootstrap", json={
            "username": "kxx", "display_name": "Admin", "password": "pass-123"}).status_code == 200
        with Session(app.state.auth_manager._get_engine()) as session, session.begin():
            stamp = datetime(2026, 9, 8, 3)
            session.add(CompetitorTarget(plid="10000001", url="https://www.takealot.com/p/PLID10000001", active=True, created_at=stamp, updated_at=stamp))
            session.add(CompetitorSnapshot(plid="10000001", collected_at=stamp, url="https://www.takealot.com/p/PLID10000001",
                title="Kettle", stock_quantity=3, stock_exact=True, stock_method="test", review_count=0,
                fetched_review_count=0, positive_reviews=0, neutral_reviews=0, negative_reviews=0,
                lifetime_sales_min=0, lifetime_sales_max=0, trend_label="test", trend_note="test"))
        calls = []
        original = web._load_competitor_dataset

        def counted(*args, **kwargs):
            calls.append(kwargs.get("own_store_only", False))
            return original(*args, **kwargs)

        monkeypatch.setattr(web, "_load_competitor_dataset", counted)
        params = dict(start_date="2026-09-07", end_date="2026-09-08", page=1)
        first = client.get("/api/competitors", params=params | {"include_own_store": "false"})
        assert first.status_code == 200
        assert client.get("/api/competitors/own-store", params=params).status_code == 200
        assert calls == [False]
        # Unrelated account configuration / Offer identity notifications must not
        # discard content whose actual ownership and access boundary is unchanged.
        original_topic = app.state.data_revisions._topic_token
        unrelated = [0]

        def topic(topics, codes):
            result = original_topic(topics, codes)
            return result + str(unrelated[0]) if topics & {"users", "own-identities"} else result

        monkeypatch.setattr(app.state.data_revisions, "_topic_token", topic)
        unrelated[0] = 1
        assert client.get("/api/competitors", params=params | {"include_own_store": "false"}).status_code == 200
        assert client.get("/api/competitors/own-store", params=params).status_code == 200
        assert calls == [False], "Configuration-only revisions must not reload unchanged true history"
        versions_before = {}
        for cache in (app.state.radar_materialized, app.state.radar_own_materialized):
            with sqlite3.connect(cache.path) as db:
                versions_before.update(db.execute("SELECT key,version FROM scopes"))
                # A real overnight return has an old generation, not a 180s hot cache.
                db.execute("UPDATE scopes SET generated=generated-86400")
        day[0] = 10
        second = client.get("/api/competitors", params=params | {"include_own_store": "false"})
        assert second.status_code == 200 and second.json() == first.json()
        assert calls == [False], "Unchanged competitor history must not reload just because midnight passed"
        assert client.get("/api/competitors/own-store", params=params).status_code == 200
        versions_after = {}
        for cache in (app.state.radar_materialized, app.state.radar_own_materialized):
            with sqlite3.connect(cache.path) as db:
                versions_after.update(db.execute("SELECT key,version FROM scopes"))
        assert len(versions_after) == 2 and versions_after.keys() == versions_before.keys()
        changed = [(versions_before[k], v) for k, v in versions_after.items()
                   if versions_before[k] != v]
        assert len(changed) == 1
        assert changed[0][0].endswith("2026-09-09")
        assert changed[0][1].endswith("2026-09-10")


def test_paginated_api_preserves_complete_card_metrics_and_rejects_invalid_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", f"sqlite:///{(tmp_path / 'source.db').as_posix()}")
    monkeypatch.setenv("TAKEALOT_WEB_ONLY", "1")
    app = create_app(tmp_path)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/competitors?page=1&include_own_store=false").status_code == 401
        assert client.post("/api/auth/bootstrap", json={
            "username": "kxx", "display_name": "Admin", "password": "pass-123"}).status_code == 200
        with Session(app.state.auth_manager._get_engine()) as session, session.begin():
            for number in range(1, 26):
                plid = str(10000000 + number)
                now = datetime(2026, 9, 7, 3, tzinfo=UTC)
                url = f"https://www.takealot.com/product/PLID{plid}"
                session.add(CompetitorTarget(plid=plid, url=url, active=True, created_at=now, updated_at=now))
                for day, stock in [(0, 10), (1, 7)]:
                    session.add(CompetitorSnapshot(
                        plid=plid, collected_at=now + timedelta(days=day), url=url,
                        title=f"Kettle {number}", stock_quantity=stock, stock_exact=True,
                        stock_method="test-exact", review_count=day, fetched_review_count=0,
                        positive_reviews=day, neutral_reviews=0, negative_reviews=0,
                        lifetime_sales_min=0, lifetime_sales_max=0, trend_label="test", trend_note="test"))
        params = dict(start_date="2026-09-07", end_date="2026-09-08", include_own_store="false")
        full = client.get("/api/competitors", params=params)
        assert full.status_code == 200
        first = client.get("/api/competitors", params=params | {"page": 1})
        assert first.status_code == 200, first.text
        second = client.get("/api/competitors", params=params | {"page": 2})
        assert second.status_code == 200, second.text
        assert len(first.json()["items"]) == 20
        assert len(second.json()["items"]) == 5
        assert first.json()["pagination"]["total"] == 25
        assert {i["plid"]: i for i in full.json()["items"]} == {
            i["plid"]: i for i in first.json()["items"] + second.json()["items"]}
        assert first.json()["date_range"] == full.json()["date_range"]
        found = client.get("/api/competitors", params=params | {"page": 1, "q": "PLID10000001"})
        assert [i["plid"] for i in found.json()["items"]] == ["10000001"]
        assert client.get("/api/competitors", params=params | {"page": 0}).status_code == 422
        assert client.get("/api/competitors", params=params | {"page": 1, "page_size": 101}).status_code == 422
        assert client.get("/api/competitors", params=params | {"page": 1, "sort": "sales_14"}).status_code == 422
        for window in ("7", "15", "30", "60", "90", "total"):
            for direction in ("asc", "desc"):
                response = client.get("/api/competitors", params=params | {
                    "page": 1, "sort": f"sales_{window}", "direction": direction})
                assert response.status_code == 200, response.text
                assert response.json()["pagination"]["total"] == 25
        with Session(app.state.read_engine) as session:
            assert len(session.scalars(select(CompetitorSnapshot)).all()) == 50
