from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import zlib
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import Session

from takealot_ops.competitors.service import CompetitorDataset
from takealot_ops.erp.competitor_match_catalog import load_match_catalog, read_precomputed_match_cards
from takealot_ops.erp.radar_materialized import MaterializedRadar
from takealot_ops.erp.web import create_app
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import CompetitorSnapshot, CompetitorTarget, ErpStore, OfferCurrent


def snapshot(plid, at, **values):
    return CompetitorSnapshot(
        plid=plid, collected_at=at, url=f"https://www.takealot.com/item/PLID{plid}",
        title=f"Cat Tree {plid}", stock_exact=False, stock_method="unknown", review_count=0,
        fetched_review_count=0, positive_reviews=0, neutral_reviews=0, negative_reviews=0,
        lifetime_sales_min=0, lifetime_sales_max=0, period_sales_min=0, period_sales_max=0,
        trend_label="unknown", trend_note="", **values,
    )


def seed(engine):
    now = datetime(2026, 9, 11, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        store = session.scalar(select(ErpStore).where(ErpStore.code == "current"))
        assert store is not None
        store.active = store.data_connected = True
        session.add(ErpStore(code="private", display_name="Other store", active=True, data_connected=True,
                             created_at=now, updated_at=now))
        # Core inserts intentionally create fixtures in both tenants.
        session.execute(OfferCurrent.__table__.insert(), [
            dict(store_code="current", offer_id="a", productline_id="101", title="Owned Cat Tree", captured_at=now),
            dict(store_code="private", offer_id="b", productline_id="102", title="Secret Cat Tree", captured_at=now),
        ])
        for plid in ("101", "102", "103", "104"):
            session.add(CompetitorTarget(plid=plid, url=f"https://www.takealot.com/item/PLID{plid}",
                                         active=True, created_at=now, updated_at=now))
        session.add_all([
            snapshot("101", now, category_path=[{"name": "Scratchers", "id": "1"}]),
            snapshot("102", now),
            snapshot("103", now - timedelta(days=1), category_path=[{"name": "Cat Furniture", "id": "2"}]),
            snapshot("103", now, category_path=None),
        ])


@pytest.fixture
def database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    create_schema(engine)
    seed(engine)
    yield engine
    engine.dispose()


def test_directory_preserves_last_captured_category_and_global_ownership(database):
    payload = load_match_catalog(database, {"current"})
    items = {item["plid"]: item for item in payload["items"]}
    assert set(items) == {"101", "103"}  # unmonitored/unauthorized links cannot leak through public path
    assert items["101"]["来源"] == "own_store"
    assert items["103"]["类目路径"][0]["name"] == "Cat Furniture"
    assert all("跟卖报价" not in item and "自有官方销量" not in item for item in items.values())
    assert {item["plid"] for item in load_match_catalog(database, {"private"})["items"]} == {"102", "103"}


def test_inventory_updates_do_not_change_match_identity_revision(database):
    before = load_match_catalog(database, {"current"})
    with database.begin() as conn:
        conn.execute(update(OfferCurrent.__table__).values(total_stock=123, selling_price=500))
    assert load_match_catalog(database, {"current"})["revision"] == before["revision"]
    with database.begin() as conn:
        conn.execute(update(OfferCurrent.__table__).where(OfferCurrent.store_code == "current").values(title="Different Cat Tower"))
    assert load_match_catalog(database, {"current"})["revision"] != before["revision"]


def test_directory_does_not_select_historical_quotes_or_sales(database):
    statements = []
    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.lower())
    event.listen(database, "before_cursor_execute", record)
    try:
        load_match_catalog(database, {"current"})
    finally:
        event.remove(database, "before_cursor_execute", record)
    assert len(statements) <= 8
    assert not any("competitor_snapshots.offers" in s or "sales" in s or "variant_snapshots" in s for s in statements)


def test_precomputed_cards_require_exact_permission_date_and_version(tmp_path, monkeypatch):
    cache = MaterializedRadar(tmp_path / "cards.sqlite3", namespace="test")
    cache._initialize()
    key = ("own", ("current",), "2026-09-01", "2026-09-11")
    boundary = "account-and-ownership"
    digest = hashlib.sha256(repr((cache.namespace, key, boundary)).encode()).hexdigest()
    with sqlite3.connect(cache.path) as db:
        db.execute("INSERT INTO scopes VALUES (?,?,?,?)", (digest, "version", time.time(), "{}"))
        for plid in ("101", "102"):
            db.execute("INSERT INTO cards VALUES (?,?,?,?,?,?)", (digest, plid, "fp", "{}", zlib.compress(json.dumps({"plid": plid}).encode()), "{}"))
    kwargs = dict(key=key, boundary=boundary, version="version", plids={"101"})
    assert read_precomputed_match_cards(cache, **kwargs) == {"101": {"plid": "101"}}
    assert read_precomputed_match_cards(cache, **(kwargs | {"boundary": "other-user"})) == {}
    assert read_precomputed_match_cards(cache, **(kwargs | {"version": "new"})) == {}
    assert read_precomputed_match_cards(cache, **(kwargs | {"key": (*key[:-1], "2026-09-10")})) == {}
    # Missing caches do not initialize disk state or launch a whole-list worker.
    assert cache._executor is None


def test_matching_routes_are_authenticated_bounded_and_keep_projection_failures_visible(tmp_path, monkeypatch):
    database_path = tmp_path / "api.db"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("TAKEALOT_WEB_ONLY", "1")
    app = create_app(tmp_path)
    with TestClient(app, client=("127.0.0.1", 50000), raise_server_exceptions=False) as client:
        assert client.get("/api/competitors/matching/catalog").status_code == 401
        assert client.post("/api/auth/bootstrap", json={"username": "kxx", "display_name": "Admin", "password": "pass-123"}).status_code == 200
        engine = create_engine(f"sqlite:///{database_path}")
        seed(engine)
        app.state.data_revisions.invalidate()
        response = client.get("/api/competitors/matching/catalog")
        assert response.status_code == 200
        assert "Cookie" in response.headers["Vary"]
        assert "private" in response.headers["Cache-Control"]
        calls = []
        def loader(_root, **kwargs):
            calls.append(kwargs)
            return CompetitorDataset(history=pd.DataFrame(), reviews=pd.DataFrame(), variants=pd.DataFrame(), current=pd.DataFrame([{"plid": "103", "商品": "Cat Tree", "来源": "competitor"}]))
        monkeypatch.setattr("takealot_ops.erp.web._load_competitor_dataset", loader)
        response = client.get("/api/competitors/matching/cards", params={"plids": "103", "start_date": "2026-09-01", "end_date": "2026-09-11"})
        assert response.status_code == 200
        assert calls[-1]["plids"] == {"103"} and calls[-1]["strict_read"] is True
        assert not calls[-1]["include_detail_frames"]
        # Repeat joins the same prepared result, never rehydrates a full directory.
        assert client.get("/api/competitors/matching/cards", params={"plids": "103", "start_date": "2026-09-01", "end_date": "2026-09-11"}).status_code == 200
        assert len(calls) == 1
        assert client.get("/api/competitors/matching/cards", params={"plids": ",".join(str(i) for i in range(21))}).status_code == 422
        assert client.get("/api/competitors/matching/cards", params={"plids": "103);drop"}).status_code == 422
        assert client.get("/api/competitors/matching/cards", params={"plids": "999"}).status_code == 404
        assert client.get("/api/competitors/matching/cards", params={"plids": "103", "start_date": "2026-09-11", "end_date": "2026-09-01"}).status_code == 422
        def failed(_root, **kwargs):
            raise RuntimeError("database unavailable")
        monkeypatch.setattr("takealot_ops.erp.web._load_competitor_dataset", failed)
        response = client.get("/api/competitors/matching/cards", params={"plids": "103", "start_date": "2026-09-02", "end_date": "2026-09-11"})
        assert response.status_code == 500  # failure must never be cached as an empty successful result
        engine.dispose()
