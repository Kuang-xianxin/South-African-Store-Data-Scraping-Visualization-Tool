from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from takealot_ops.collectors.home import collect_home_finance, warehouse_payload
from takealot_ops.erp.home_dashboard import build_home_dashboard
from takealot_ops.erp.home_dashboard import _verified_day
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import DailySalesMetricState, OfferCurrent, SaleItem, SellerHomeSnapshot
from takealot_ops.storage.repository import Repository
from takealot_ops.storage.store_context import store_scope


TODAY = date(2026, 9, 10)
NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


def test_intraday_pull_does_not_verify_a_later_closed_day():
    row = {"metric_date": date(2026, 9, 9), "source_kind": "takealot_sales_api", "verified_at": datetime(2026, 9, 9, 12)}
    assert not _verified_day(row, TODAY)
    row["verified_at"] = datetime(2026, 9, 9, 22)
    assert _verified_day(row, TODAY)


def seeded():
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session, session.begin():
        for code, item, order, qty, price, sku in [
            ("current", "a", "shared", 2, 120, "SKU-A"),
            ("current", "b", "shared", 1, 80, "SKU-B"),
            ("second", "c", "shared", 1, 99, "SKU-A"),
            ("secret", "d", "other", 20, 9999, "SECRET"),
        ]:
            session.add(SaleItem(store_code=code, order_item_id=item, order_id=order,
                sales_day=TODAY, order_date=NOW, quantity=qty, selling_price=price,
                sku=sku, offer_id=item, raw_payload={}))
        session.add(OfferCurrent(store_code="current", offer_id="a", sku="SKU-A",
            selling_price=60, takealot_available_stock=10, captured_at=NOW))
        for code in ["current", "second"]:
            session.add(DailySalesMetricState(store_code=code, metric_date=TODAY,
                ordered_units=3, ordered_revenue=200, source_kind="takealot_sales_api",
                source_details={}, verified_at=NOW, first_published_at=NOW, updated_at=NOW))
    return engine


def test_orders_are_distinct_per_store_and_amount_is_not_multiplied(tmp_path):
    data = build_home_dashboard(seeded(), {"current": "One", "second": "Two"}, tmp_path, today=TODAY)
    today = data["periods"][1]
    assert (today["orders"], today["units"], today["revenue"], today["sold_skus"]) == (2, 4, 299, 3)
    assert all(row["date"] != TODAY.isoformat() for row in data["daily"])
    assert len(data["monthly"]) == 12
    assert data["monthly"][-1]["revenue"] == 299
    assert today["inventory_sell_through"] is None  # second store lacks a closing stock snapshot


def test_no_data_is_not_zero_and_missing_cost_is_not_profit(tmp_path):
    engine = seeded()
    data = build_home_dashboard(engine, {"current": "One"}, tmp_path, today=TODAY)
    assert data["periods"][2]["orders"] is None
    assert data["daily"][0]["revenue"] is None
    assert data["finance"]["totals"]["current"] is None
    assert data["warehouse"]["totals"]["gross_zar"] is None
    assert data["warehouse"]["totals"]["cost_rmb"] is None
    assert data["warehouse"]["missing_cost_skus"] == 1


def test_historical_sales_skus_are_in_denominator_and_rates_do_not_exceed_100(tmp_path):
    data = build_home_dashboard(seeded(), {"current": "One"}, tmp_path, today=TODAY)
    today = data["periods"][1]
    assert today["sold_skus"] == 2
    assert today["sku_denominator"] == 2
    assert today["sku_selling_rate"] == 100
    assert today["inventory_sell_through"] == 23.08  # 3 / (3 + 10)


def test_authorized_empty_scope_cannot_fall_back_to_current_store(tmp_path):
    data = build_home_dashboard(seeded(), {}, tmp_path, today=TODAY)
    assert data["periods"][1]["orders"] is None
    assert data["warehouse"]["stores"] == []
    assert data["finance"]["stores"] == []


def test_gross_uses_current_unit_price_times_stock_and_keeps_negative_margin(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    cache = tmp_path / "data/runtime-cache/exchange-rates/cny-zar.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"base": "CNY", "quote": "ZAR", "rate": 2, "date": "2026-09-10", "fetched_at": NOW.isoformat()}))
    monkeypatch.setattr("takealot_ops.erp.home_dashboard.load_product_master_links", lambda *a, **kw: {"sku-a": SimpleNamespace(cost_rmb=Decimal(31))})
    data = build_home_dashboard(seeded(), {"current": "One"}, tmp_path, today=TODAY)
    assert data["warehouse"]["totals"]["cost_rmb"] == 310
    assert data["warehouse"]["totals"]["gross_zar"] == -20


def test_missing_order_identity_does_not_become_zero_orders(tmp_path):
    engine = seeded()
    with Session(engine) as session, session.begin():
        for row in session.scalars(select(SaleItem).where(SaleItem.store_code == "current")):
            row.order_id = None
    data = build_home_dashboard(engine, {"current": "One"}, tmp_path, today=TODAY)
    assert data["periods"][1]["orders"] is None
    assert data["periods"][1]["missing_order_ids"] == 2


def test_region_stock_ignores_on_way_and_receiving_and_preserves_missing():
    result = warehouse_payload([
        {"takealot_warehouse_stock": [{"region": "CPT", "quantity_available": 3, "stock_on_way": 50, "stock_in_receiving": 8}]},
        {"takealot_warehouse_stock": []}, {},
    ])
    assert result["regions"] == {"CPT": 3}
    assert result["missing_count"] == 1


class FinanceClient:
    fail = False

    def get_balances(self):
        return {"balances": {"current": "120", "held_back": "20", "available": "100"}}

    def iter_items(self, path, params):
        if self.fail:
            raise RuntimeError("upstream failure with private credentials")
        for key, amount, kind in [(1, -100, "disbursement-disbursement"), (2, 20, "reversal-disbursement")]:
            yield {"transaction_id": key, "transaction_type": kind, "amount_incl_vat": amount, "created_at": NOW.isoformat()}


def test_payout_sign_reversal_idempotence_and_failed_refresh_keeps_success():
    engine = seeded()
    client = FinanceClient()
    with store_scope("current"), Session(engine) as session:
        repo = Repository(session)
        assert collect_home_finance(client, repo, NOW).succeeded
        assert collect_home_finance(client, repo, NOW).succeeded
        client.fail = True
        assert not collect_home_finance(client, repo, NOW).succeeded
        rows = {r.kind: r for r in session.scalars(select(SellerHomeSnapshot))}
        assert Decimal(rows["payouts"].payload["total"]) == 80
        assert len(rows["payouts"].payload["transactions"]) == 2
        assert Decimal(rows["balances"].payload["current"]) == 120


def test_home_endpoint_rechecks_scope_and_rejects_unbounded_dates(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from takealot_ops.erp.web import create_app
    from takealot_ops.storage.models import ErpStore

    database_url = f"sqlite:///{tmp_path / 'home-auth.db'}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    app = create_app(tmp_path)
    with TestClient(app, client=("127.0.0.1", 55001)) as browser:
        assert browser.get("/api/erp/summary/home?store_scope=all").status_code == 401
        login = browser.post("/api/auth/bootstrap", json={"username": "kxx", "display_name": "Test", "password": "test-password-123"})
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        engine = create_engine(database_url)
        with Session(engine) as session, session.begin():
            current = session.scalar(select(ErpStore).where(ErpStore.code == "current"))
            current.data_connected = True
            current_id = current.id
            session.add(ErpStore(code="secret", display_name="Private Store", active=True, data_connected=True, created_at=NOW, updated_at=NOW))
        created = browser.post("/api/auth/users", headers={"X-CSRF-Token": csrf}, json={
            "username": "home.viewer", "display_name": "Home Viewer", "password": "viewer-password-123",
            "role": "operator", "store_access_all": False, "store_ids": [current_id],
        })
        assert created.status_code == 200
        signed = browser.post("/api/auth/login", json={"username": "home.viewer", "password": "viewer-password-123"})
        assert signed.status_code == 200
        result = browser.get("/api/erp/summary/home?store_scope=all")
        assert result.status_code == 200
        assert result.json()["store_count"] == 1
        assert result.json()["finance"]["stores"][0]["store_code"] == "current"
        assert "Private Store" not in result.text
        assert browser.get("/api/erp/summary/home?store_scope=all", headers={"X-Store-Code": "secret"}).status_code == 403
        assert browser.get("/api/erp/summary/home?start_date=1900-01-01").status_code == 422


def test_home_snapshot_commit_refreshes_overview_without_invalidating_radar(tmp_path):
    from takealot_ops.collectors.home import save_snapshot
    from takealot_ops.erp.live_updates import DataRevisionReader
    from takealot_ops.storage.migrations import create_engine_for_database_url
    from takealot_ops.storage.models import Base

    engine = create_engine_for_database_url(f"sqlite:///{tmp_path / 'home-revisions.db'}")
    Base.metadata.create_all(engine)
    reader = DataRevisionReader(engine, ttl_seconds=0)
    key = ("home-dashboard-v1", (("current", "One"),), TODAY)
    before = reader.cache_token(key)
    radar = reader.token("competitors", ("current",))
    with store_scope("current"), Session(engine) as session, session.begin():
        save_snapshot(session, "balances", {"current": "100"}, NOW)
    assert reader.cache_token(key) != before
    assert reader.token("competitors", ("current",)) == radar
