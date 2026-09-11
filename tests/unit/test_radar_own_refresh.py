"""Store refreshes keep dated private previews without weakening public masking."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from threading import Event

from fastapi.testclient import TestClient
from sqlalchemy import event, insert, update

from takealot_ops.erp.live_updates import DataRevisionReader
from takealot_ops.erp.radar_materialized import radar_date_bounds
from takealot_ops.erp.web import create_app
from takealot_ops.storage.migrations import create_engine_for_database_url
from takealot_ops.storage.models import (
    Base, CompetitorSnapshot, ErpStore, OfferCurrent, StoreOfferBaseline, StoreOfferObservation,
)
from test_radar_materialized import query, setup_cache


def test_legacy_public_endpoint_cannot_preview_across_ownership_change(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'api.db'}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("TAKEALOT_WEB_ONLY", "1")
    app = create_app(tmp_path)
    engine = create_engine_for_database_url(database_url)
    try:
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            assert client.post("/api/auth/bootstrap", json={
                "username": "kxx", "display_name": "Admin", "password": "test-pass-123",
            }).status_code == 200
            path = "/api/competitors?own_store_scope=all&include_own_store=false&prefer_cached=true"
            first = client.get(path)
            assert first.status_code == 200 and first.headers["x-erp-refreshing"] == "0"
            with engine.begin() as db:
                db.execute(insert(OfferCurrent.__table__).values(
                    store_code="current", offer_id="new", productline_id="123", captured_at=datetime(2026, 9, 11),
                ))
            app.state.data_revisions.invalidate()
            second = client.get(path)
            assert second.status_code == 200
            assert second.headers["x-erp-refreshing"] == "0"  # No stale public snapshot may cross this change.
    finally:
        engine.dispose()


def test_own_identity_refresh_reuses_complete_preview_but_permissions_and_public_mask_do_not(tmp_path):
    engine = create_engine_for_database_url(f"sqlite:///{tmp_path / 'stores.db'}")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 11)
    with engine.begin() as db:
        db.execute(insert(ErpStore.__table__).values(
            id=1, code="one", display_name="One", active=True, data_connected=True,
            created_at=now, updated_at=now,
        ))
        db.execute(insert(OfferCurrent.__table__).values(
            store_code="one", offer_id="1", productline_id="1", sku="old", captured_at=now,
        ))
    revisions = DataRevisionReader(engine, ttl_seconds=0)
    cache, _, _, _, calls, options = setup_cache(tmp_path, 1)
    entered, finish = Event(), Event()
    original_loader = options["loader"]
    def access(**kw):
        return revisions.radar_access_token(("one",), permissions="user-one", **kw)
    initial_public = access()
    options.update(boundary=access(own=True), version=access)

    def load(plids):
        entered.set()
        assert finish.wait(5)
        result = original_loader(plids)
        result["items"][0]["商品"] = "updated product"
        return result

    try:
        first, _, generated = cache.page(**options, query=query(), prefer_cached=False)
        with engine.begin() as db:
            db.execute(update(OfferCurrent.__table__).values(sku="new", productline_id="2"))
        assert access() != initial_public
        assert access(own=True) == options["boundary"]
        options["loader"] = load
        with ThreadPoolExecutor() as pool:
            preview = pool.submit(cache.page, **options, query=query(), prefer_cached=True)
            result, refreshing, timestamp = preview.result(timeout=2)
            assert entered.wait(2) and refreshing and result == first and timestamp == generated
            assert len(cache._works) == 1
            finish.set()
        fresh, refreshing, _ = cache.page(**options, query=query(), prefer_cached=False)
        assert not refreshing and fresh["items"][0]["商品"] == "updated product"
        assert len(calls) == 2
        changed_permissions = revisions.radar_access_token(("one",), permissions="revoked-store", own=True)
        assert changed_permissions != options["boundary"]
        cache.page(**(options | {"boundary": changed_permissions}), query=query(), prefer_cached=True)
        assert len(calls) == 3  # A different authorization must load its own projection.
        with engine.begin() as db:
            db.execute(update(ErpStore.__table__).values(data_connected=False))
        assert access(own=True) != options["boundary"]
    finally:
        finish.set()
        cache.close()
        engine.dispose()


def test_date_bounds_batch_stores_preserves_scope_and_official_history(tmp_path):
    engine = create_engine_for_database_url(f"sqlite:///{tmp_path / 'dates.db'}")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 11)
    with engine.begin() as db:
        for i, code in enumerate(("one", "two", "hidden", "disabled"), 1):
            db.execute(insert(ErpStore.__table__).values(
                id=i, code=code, display_name=code, active=code != "disabled", data_connected=True,
                created_at=now, updated_at=now,
            ))
            db.execute(insert(OfferCurrent.__table__).values(
                store_code=code, offer_id=str(i), productline_id=str(i), captured_at=now,
            ))
            for table in (StoreOfferBaseline.__table__, StoreOfferObservation.__table__):
                db.execute(insert(table).values(
                    store_code=code, offer_id=str(i), productline_id=str(i), captured_at=now,
                    display_date=date(2026, 9, i),
                ))
        # An unrelated PLID's earlier history must not widen the selected range.
        db.execute(insert(StoreOfferObservation.__table__).values(
            store_code="one", offer_id="unlisted", productline_id="999", captured_at=now,
            display_date=date(2025, 1, 1),
        ))
        db.execute(insert(CompetitorSnapshot.__table__).values(
            plid="2", collected_at=datetime(2026, 8, 25, 12), title="public evidence",
            url="https://www.takealot.com/example/PLID2", stock_method="unknown",
            review_count=0, fetched_review_count=0, positive_reviews=0, neutral_reviews=0,
            negative_reviews=0, lifetime_sales_min=0, lifetime_sales_max=0,
            trend_label="unknown", trend_note="",
        ))
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, u, s, p, x, m: statements.append(s))
    try:
        assert radar_date_bounds(engine, own=True, store_codes={"one", "two", "disabled"}) == (
            date(2026, 8, 25), date(2026, 9, 2),
        )
        assert len(statements) == 5  # Catalog, narrow identities, three date aggregates.
        assert radar_date_bounds(engine, own=True, store_codes={"one"}) == (date(2026, 9, 1), date(2026, 9, 1))
        assert radar_date_bounds(engine, own=True, store_codes=set()) == (None, None)
    finally:
        engine.dispose()
