from __future__ import annotations

import hashlib
import threading
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from takealot_ops.competitors.api import CompetitorNetworkError
from takealot_ops.competitors.batch import (
    CollectionBatchBusyError,
    CollectionBatchRegistry,
    CollectionRequestCoordinator,
)
from takealot_ops.competitors.service import (
    CompetitorCollectionResult,
    CompetitorDiscoveredTarget,
)
from takealot_ops.erp.daily_report import capture_daily_report
from takealot_ops.erp.auth import StoreIdentity
from takealot_ops.exchange_rates import ExchangeRateQuote
from takealot_ops.erp.web import (
    _aggregate_logistics_payloads,
    _aggregate_platform_warehouse_payloads,
    _aggregate_store_revenue_series,
    create_app,
)
from takealot_ops.logistics.service import LogisticsOverviewService
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import (
    CollectionRun,
    CompetitorTarget,
    CompetitorSnapshot,
    DailyReportRun,
    DailySalesMetricState,
    ErpStore,
    ErpSession,
    ErpUserStore,
    LogisticsProviderSnapshot,
    OfferCurrent,
    OfferSnapshot,
    OwnStorePersonalWatchlist,
    ReturnItem,
    SalesRevenueRevision,
    StoreOfferBaseline,
)
from takealot_ops.storage.store_context import current_store_code, store_scope


PROJECT_ROOT = Path(__file__).parents[2]
TRUSTED_PRODUCT_IMAGE_URL = (
    "https://takealot.s3.amazonaws.com/covers_images/37b5fc661b694ed5969280cc0cea2ce4/s.file"
)


def test_store_revenue_series_excludes_the_open_sast_day() -> None:
    series = {
        "current": [
            {"metric_date": "2026-08-10", "ordered_revenue": 100},
            {"metric_date": "2026-08-11", "ordered_revenue": 10},
        ],
        "store-02": [
            {"metric_date": "2026-08-10", "ordered_revenue": 200},
            {"metric_date": "2026-08-11", "ordered_revenue": 20},
        ],
    }

    points = _aggregate_store_revenue_series(
        series,
        completed_through=date(2026, 8, 10),
    )

    assert [point["metric_date"] for point in points] == ["2026-08-10"]
    assert points[0]["total_ordered_revenue"] == 300


def test_store_revenue_series_keeps_every_date_in_an_explicit_viewport() -> None:
    first_date = date(2026, 7, 1)
    series = {
        store_code: [
            {
                "metric_date": (first_date + timedelta(days=index)).isoformat(),
                "ordered_revenue": (index + 1) * multiplier,
            }
            for index in range(40)
        ]
        for store_code, multiplier in (("current", 10), ("store-02", 20))
    }

    points = _aggregate_store_revenue_series(
        series,
        start_date=first_date,
        completed_through=date(2026, 8, 9),
        limit=None,
    )

    assert len(points) == 40
    assert points[0]["metric_date"] == "2026-07-01"
    assert points[-1]["metric_date"] == "2026-08-09"
    assert points[-1]["total_ordered_revenue"] == 1200


def test_store_revenue_series_returns_available_sum_for_partial_coverage() -> None:
    points = _aggregate_store_revenue_series(
        {
            "current": [
                {"metric_date": "2026-07-29", "ordered_revenue": 88722},
            ],
            "store-06": [],
        },
        completed_through=date(2026, 7, 29),
    )

    assert points == [
        {
            "metric_date": "2026-07-29",
            "total_ordered_revenue": 88722.0,
            "covered_store_count": 1,
            "store_count": 2,
            "missing_store_count": 1,
            "data_status": "pending",
            "source_verified_store_count": 0,
            "pending_reconciliation_store_count": 0,
            "unverified_source_store_count": 2,
            "revised_store_count": 0,
            "revision_count": 0,
            "latest_sales_verified_at": None,
            "latest_revision_at": None,
        }
    ]


def _bootstrap(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/auth/bootstrap",
        json={
            "username": "kxx",
            "display_name": "KXX Admin",
            "password": "pass-123",
        },
    )
    assert response.status_code == 200
    return response.json()


def _create_operator(
    client: TestClient,
    csrf: str,
    *,
    username: str,
) -> None:
    response = client.post(
        "/api/auth/users",
        headers={"X-CSRF-Token": csrf},
        json={
            "username": username,
            "display_name": username.replace(".", " ").title(),
            "password": "operator-password-123",
            "role": "operator",
        },
    )
    assert response.status_code == 200


def test_competitor_detail_requests_only_the_selected_plid(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict[str, object]] = []
    sales_calls: list[dict[str, object]] = []
    traffic_calls: list[dict[str, object]] = []
    return_calls: list[dict[str, object]] = []
    profitability_calls: list[dict[str, object]] = []
    database_path = tmp_path / "detail-route.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )

    def load_detail_dataset(_root: Path, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            history=pd.DataFrame(),
            reviews=pd.DataFrame(),
            variants=pd.DataFrame(),
            category_paths={
                "101163999": [
                    {
                        "name": "Camping & Outdoor",
                        "id": "27895",
                        "type": "category",
                        "slug": "family-tents-27895",
                    }
                ]
            },
            store_current=pd.DataFrame([{"plid": "101163999"}]),
            store_history=pd.DataFrame(),
        )

    monkeypatch.setattr(
        "takealot_ops.erp.web._load_competitor_dataset",
        load_detail_dataset,
    )

    def load_sales(_root: Path, **kwargs):
        sales_calls.append(kwargs)
        return []

    monkeypatch.setattr(
        "takealot_ops.erp.web._load_own_store_sales",
        load_sales,
    )

    def load_traffic(_root: Path, **kwargs):
        traffic_calls.append(kwargs)
        return []

    monkeypatch.setattr(
        "takealot_ops.erp.web._load_own_store_traffic",
        load_traffic,
    )

    def load_returns(_root: Path, **kwargs):
        return_calls.append(kwargs)
        return {"data_status": "collected", "items": []}

    monkeypatch.setattr(
        "takealot_ops.erp.web._load_own_store_returns",
        load_returns,
    )

    def load_profitability(_root: Path, **kwargs):
        profitability_calls.append(kwargs)
        return {
            "items": [],
            "store_codes": ["current"],
            "message": "test profitability",
        }

    monkeypatch.setattr(
        "takealot_ops.erp.web._load_own_store_profitability",
        load_profitability,
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        _bootstrap(client)
        response = client.get(
            "/api/competitors/101163999?start_date=2026-08-01&end_date=2026-08-14"
        )

    assert response.status_code == 200
    assert response.json() == {
        "category_path": [
            {
                "name": "Camping & Outdoor",
                "id": "27895",
                "type": "category",
                "slug": "family-tents-27895",
            }
        ],
        "history": [],
        "reviews": [],
        "variants": [],
        "own_store_sales": [],
        "own_store_traffic": [],
        "own_store_returns": {"data_status": "collected", "items": []},
        "own_store_profitability": {
            "items": [],
            "store_codes": ["current"],
            "message": "test profitability",
        },
        "company_inventory": {
            "items": [],
            "store_codes": ["current"],
            "company_sku_count": 0,
            "w8_shared_once": True,
            "stage_totals_are_additive": False,
            "message": "该自有链接的平台 SKU 尚未关联公司 SKU。",
        },
    }
    assert calls == [
        {
            "start_date": date(2026, 8, 1),
            "end_date": date(2026, 8, 14),
            "own_store_codes": {"current"},
            "plids": {"101163999"},
            "engine": app.state.read_engine,
        }
    ]
    assert len(sales_calls) == 1
    assert sales_calls[0]["plid"] == "101163999"
    assert sales_calls[0]["own_store_codes"] == {"current"}
    assert isinstance(sales_calls[0]["through"], date)
    assert sales_calls[0]["engine"] is app.state.read_engine
    assert traffic_calls == [
        {
            "plid": "101163999",
            "own_store_codes": {"current"},
            "start_date": date(2026, 8, 1),
            "end_date": date(2026, 8, 14),
            "engine": app.state.read_engine,
        }
    ]
    assert return_calls == [
        {
            "plid": "101163999",
            "own_store_codes": {"current"},
            "start_date": date(2026, 8, 1),
            "end_date": date(2026, 8, 14),
            "engine": app.state.read_engine,
        }
    ]
    assert len(profitability_calls) == 1
    assert profitability_calls[0]["plid"] == "101163999"
    assert profitability_calls[0]["own_store_codes"] == {"current"}
    assert profitability_calls[0]["rate_service"] is app.state.cny_zar_rate_service
    assert isinstance(profitability_calls[0]["cost_as_of"], date)
    assert isinstance(profitability_calls[0]["fee_window_end"], date)
    assert profitability_calls[0]["engine"] is app.state.read_engine


def test_returns_route_exposes_detail_and_collection_coverage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "returns-route.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(tmp_path)
    captured_at = datetime(2026, 8, 17, 8, tzinfo=UTC)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        _bootstrap(client)
        engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        with store_scope("current"), Session(engine) as session, session.begin():
            session.add(
                OfferCurrent(
                    offer_id="offer-return-1",
                    sku="SKU-RETURN-1",
                    title="Return Route Product",
                    productline_id="99887766",
                    quantity_returned_30_days=5,
                    captured_at=captured_at,
                )
            )
            session.add(
                ReturnItem(
                    seller_return_id="seller-return-1",
                    order_id="order-return-1",
                    offer_id="offer-return-1",
                    sku="SKU-RETURN-1",
                    return_reference_number="RRN-ROUTE-1",
                    quantity=2,
                    return_date=datetime(2026, 8, 12),
                    return_status="removal_order",
                    return_reason="defective_or_damaged",
                    outcomes=[{"status": "removal_order"}],
                    transactions=[
                        {"transaction_type": "refund", "amount_incl_vat": "-50.00"}
                    ],
                    captured_at=captured_at,
                    raw_payload={"seller_return_id": "seller-return-1"},
                )
            )
            session.add(
                CollectionRun(
                    run_id="returns-route-run",
                    run_type="returns",
                    scope_date=date(2026, 8, 17),
                    started_at=captured_at,
                    finished_at=captured_at,
                    status="success",
                    counts={
                        "records": 1,
                        "requested_start_ordinal": date(2026, 8, 1).toordinal(),
                        "requested_end_ordinal": date(2026, 8, 17).toordinal(),
                    },
                    error=None,
                )
            )
        engine.dispose()

        response = client.get(
            "/api/erp/returns"
            "?start_date=2026-08-01&end_date=2026-08-17&query=RRN-ROUTE"
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["data_status"] == "collected"
    assert payload["offer_returned_30_days"]["units"] == 5
    assert payload["summary"]["return_units"] == 2
    assert payload["summary"]["removal_order_units"] == 2
    assert payload["total"] == 1
    assert payload["items"][0]["return_reason_label"] == "商品有缺陷或损坏"
    assert payload["items"][0]["product_title"] == "Return Route Product"
    assert payload["store_statuses"][0]["record_count"] == 1


def test_personal_watchlist_overview_projects_only_visible_membership_plids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict[str, object]] = []
    database_path = tmp_path / "personal-watchlist-overview.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    monkeypatch.setenv(
        "TAKEALOT_STORES",
        "current|Alpha Store|STORE_KEY_ALPHA;store-02|Beta Store|STORE_KEY_BETA",
    )
    monkeypatch.setenv("STORE_KEY_ALPHA", "alpha-key")
    monkeypatch.setenv("STORE_KEY_BETA", "beta-key")

    def load_personal_dataset(_root: Path, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            current=(
                pd.DataFrame(
                    [{"plid": "12345678", "商品": "Hydrated competitor"}]
                )
                if kwargs["plids"]
                else pd.DataFrame()
            ),
            store_current=pd.DataFrame(),
            own_follower_events=[],
            date_range_payload=lambda: {
                "available_start": "2026-08-01",
                "available_end": "2026-08-10",
                "selected_start": "2026-08-02",
                "selected_end": "2026-08-09",
            },
        )

    monkeypatch.setattr(
        "takealot_ops.erp.web._load_competitor_dataset",
        load_personal_dataset,
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        create_schema(engine)
        now = datetime(2026, 8, 11, 5, tzinfo=UTC)
        with Session(engine) as session, session.begin():
            current_store = session.scalar(
                select(ErpStore).where(ErpStore.code == "current")
            )
            assert current_store is not None
            current_store.display_name = "Alpha Store"
            current_store.active = True
            current_store.data_connected = True
            session.add(
                ErpStore(
                        code="store-02",
                        display_name="Beta Store",
                        active=True,
                        data_connected=True,
                        created_at=now,
                        updated_at=now,
                )
            )
        engine.dispose()
        session = _bootstrap(client)
        empty_response = client.get(
            "/api/competitors/personal-watchlist/overview"
        )
        assert empty_response.status_code == 200
        assert empty_response.json()["items"] == []
        assert calls[-1]["plids"] == set()
        created = client.post(
            "/api/competitors/targets",
            headers={"X-CSRF-Token": str(session["csrf_token"])},
            json={"url": "https://www.takealot.com/example/PLID12345678"},
        )
        assert created.status_code == 200
        calls.clear()

        response = client.get(
            "/api/competitors/personal-watchlist/overview"
            "?start_date=2026-08-02&end_date=2026-08-09&own_store_scope=current"
        )

    assert response.status_code == 200
    assert response.json()["items"] == [
        {"plid": "12345678", "商品": "Hydrated competitor"}
    ]
    assert response.json()["own_follower_events"] == []
    assert calls == [
        {
            "start_date": date(2026, 8, 2),
            "end_date": date(2026, 8, 9),
            "own_store_codes": {"current", "store-02"},
            "plids": {"12345678"},
            "include_detail_frames": False,
            "engine": app.state.read_engine,
        }
    ]


def test_competitor_radar_returns_automatic_store_targets_and_separate_items(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp-store-radar.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv(
        "TAKEALOT_STORES",
        "current|Alpha Store|STORE_KEY_ALPHA;store-02|Beta Store|STORE_KEY_BETA",
    )
    monkeypatch.setenv("STORE_KEY_ALPHA", "alpha-key")
    monkeypatch.setenv("STORE_KEY_BETA", "beta-key")
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        issued = _bootstrap(client)
        engine = create_engine(database_url)
        now = datetime(2026, 8, 2, 1, tzinfo=UTC)
        with Session(engine) as session, session.begin():
            current_store = session.scalar(
                select(ErpStore).where(ErpStore.code == "current")
            )
            assert current_store is not None
            current_store.display_name = "Alpha Store"
            current_store.active = True
            current_store.data_connected = True
            session.add(
                ErpUserStore(
                    user_id=int(issued["user"]["id"]),
                    store_id=current_store.id,
                )
            )
            session.add(
                ErpStore(
                    code="store-02",
                    display_name="Beta Store",
                    active=True,
                    data_connected=True,
                    created_at=now,
                    updated_at=now,
                )
            )
        with store_scope("current"), Session(engine) as session, session.begin():
            session.add_all(
                [
                OfferCurrent(
                    offer_id="own-offer",
                    productline_id="12345678",
                    sku="OWN-SKU",
                    title="Own Product",
                    selling_price=99,
                    total_stock=5,
                    captured_at=now,
                ),
                CompetitorTarget(
                    plid="99999999",
                    offer_group_plid="99999999",
                    url="https://www.takealot.com/p/PLID99999999",
                    title="True competitor",
                    active=True,
                    created_at=now,
                    updated_at=now,
                ),
                CompetitorTarget(
                    plid="87654321",
                    offer_group_plid="87654321",
                    url="https://www.takealot.com/p/PLID87654321",
                    title="Should become own store",
                    active=True,
                    created_at=now,
                    updated_at=now,
                ),
                ]
            )
        with store_scope("store-02"), Session(engine) as session, session.begin():
            session.add_all(
                [
                    OfferCurrent(
                        offer_id="shared-own-offer",
                        productline_id="12345678",
                        sku="SHARED-SKU",
                        title="Shared Own Product",
                        selling_price=101,
                        total_stock=7,
                        captured_at=now + timedelta(minutes=1),
                    ),
                    OfferCurrent(
                        offer_id="beta-own-offer",
                        productline_id="87654321",
                        sku="BETA-SKU",
                        title="Beta Own Product",
                        selling_price=199,
                        total_stock=3,
                        captured_at=now + timedelta(minutes=2),
                    ),
                    StoreOfferBaseline(
                        display_date=date(2026, 8, 2),
                        offer_id="beta-own-offer",
                        productline_id="87654321",
                        sku="BETA-SKU",
                        title="Beta Own Product",
                        image_url=None,
                        selling_price=199,
                        status="buyable",
                        total_stock=3,
                        takealot_available_stock=3,
                        seller_available_stock=0,
                        captured_at=now + timedelta(minutes=2),
                    ),
                ]
            )
        create_schema(engine)
        engine.dispose()

        started_batch = client.post(
            "/api/competitors/batch-events",
            headers={"X-CSRF-Token": str(issued["csrf_token"])},
            json={
                "batch_id": "combined-batch",
                "client_id": "combined-client",
                "event": "start",
                "completed": 0,
                "total": 3,
                "pending": 3,
            },
        )
        private_priority = client.post(
            "/api/competitors/targets/12345678/prioritize",
            headers={"X-CSRF-Token": str(issued["csrf_token"])},
        )

        automatic_targets = client.get("/api/competitors/store-targets")
        all_store_targets = client.get(
            "/api/competitors/store-targets?own_store_scope=all"
        )
        operating_store_targets = client.get(
            "/api/competitors/store-targets?own_store_scope=operating"
        )
        beta_targets = client.get(
            "/api/competitors/store-targets",
            headers={"X-Store-Code": "store-02"},
        )
        competitor_targets = client.get("/api/competitors/targets")
        overview = client.get("/api/competitors")
        true_competitor_overview = client.get(
            "/api/competitors?include_own_store=false"
        )
        all_store_overview = client.get("/api/competitors?own_store_scope=all")
        operating_store_overview = client.get(
            "/api/competitors?own_store_scope=operating"
        )
        own_store_overview = client.get("/api/competitors/own-store")
        all_store_own_store_overview = client.get(
            "/api/competitors/own-store?own_store_scope=all"
        )
        one_plid_own_store_overview = client.get(
            "/api/competitors/own-store?own_store_scope=all&plid=87654321"
        )
        operating_own_store_overview = client.get(
            "/api/competitors/own-store?own_store_scope=operating"
        )
        current_store_detail = client.get("/api/competitors/12345678")
        own_default_library_response = client.post(
            "/api/competitors/personal-watchlist/libraries",
            headers={"X-CSRF-Token": str(issued["csrf_token"])},
            json={"name": "Own Products"},
        )
        assert own_default_library_response.status_code == 200
        own_default_library_id = own_default_library_response.json()["library"]["id"]
        own_default_saved = client.put(
            "/api/competitors/personal-watchlist/settings",
            headers={"X-CSRF-Token": str(issued["csrf_token"])},
            json={"default_library_id": own_default_library_id},
        )
        assert own_default_saved.status_code == 200
        private_add = client.post(
            "/api/competitors/targets",
            headers={"X-CSRF-Token": str(issued["csrf_token"])},
            json={"url": "https://www.takealot.com/p/PLID87654321"},
        )
        competitor_add = client.post(
            "/api/competitors/targets",
            headers={"X-CSRF-Token": str(issued["csrf_token"])},
            json={"url": "https://www.takealot.com/p/PLID77777777"},
        )
        private_watchlist = client.get("/api/competitors/personal-watchlist")

    assert started_batch.status_code == 200
    assert current_store_detail.status_code == 200
    assert current_store_detail.json()["company_inventory"]["store_codes"] == [
        "current",
        "store-02",
    ]
    assert private_priority.status_code == 200
    assert private_priority.json()["accepted"] is True
    assert private_priority.json()["status"]["priority_targets"][0]["plid"] == "12345678"
    assert automatic_targets.status_code == 200
    assert automatic_targets.json() == {
        "items": [
            {
                "plid": "12345678",
                "url": "https://www.takealot.com/p/PLID12345678",
                "title": "Own Product",
                "offer_count": 1,
                "store_count": 1,
                "store_names": ["Alpha Store"],
                "captured_at": "2026-08-02T01:00:00",
            },
        ],
        "scope": "current",
        "selected_store_count": 1,
        "selected_membership_count": 1,
        "all_store_count": 2,
        "all_store_unique_count": 2,
        "all_store_membership_count": 3,
    }
    assert all_store_targets.status_code == 200
    assert all_store_targets.json()["items"] == [
        {
            "plid": "12345678",
            "url": "https://www.takealot.com/p/PLID12345678",
            "title": "Own Product",
            "offer_count": 2,
            "store_count": 2,
            "store_names": ["Alpha Store", "Beta Store"],
            "captured_at": "2026-08-02T01:00:00",
        },
        {
            "plid": "87654321",
            "url": "https://www.takealot.com/p/PLID87654321",
            "title": "Beta Own Product",
            "offer_count": 1,
            "store_count": 1,
            "store_names": ["Beta Store"],
            "captured_at": "2026-08-02T01:02:00",
        },
    ]
    assert all_store_targets.json() | {"items": []} == {
        "items": [],
        "scope": "all",
        "selected_store_count": 2,
        "selected_membership_count": 3,
        "all_store_count": 2,
        "all_store_unique_count": 2,
        "all_store_membership_count": 3,
    }
    assert operating_store_targets.status_code == 200
    assert operating_store_targets.json() == {
        **automatic_targets.json(),
        "scope": "operating",
    }
    assert beta_targets.status_code == 200
    assert {item["plid"] for item in beta_targets.json()["items"]} == {
        "12345678",
        "87654321",
    }
    assert all(item["store_names"] == ["Beta Store"] for item in beta_targets.json()["items"])
    assert competitor_targets.status_code == 200
    assert [item["plid"] for item in competitor_targets.json()["items"]] == ["99999999"]
    assert overview.status_code == 200
    assert [item["plid"] for item in overview.json()["items"]] == []
    assert {item["plid"] for item in overview.json()["store_items"]} == {
        "12345678",
    }
    assert overview.json()["own_follower_events"] == []
    assert true_competitor_overview.status_code == 200
    assert true_competitor_overview.json()["items"] == overview.json()["items"]
    assert true_competitor_overview.json()["store_items"] == []
    assert true_competitor_overview.json()["own_follower_events"] == []
    assert all_store_overview.status_code == 200
    assert {item["plid"] for item in all_store_overview.json()["store_items"]} == {
        "12345678",
        "87654321",
    }
    assert all_store_overview.json()["own_follower_events"] == []
    assert operating_store_overview.status_code == 200
    assert {
        item["plid"]
        for item in operating_store_overview.json()["store_items"]
    } == {"12345678"}
    assert own_store_overview.status_code == 200
    assert {item["plid"] for item in own_store_overview.json()["store_items"]} == {
        "12345678"
    }
    assert all_store_own_store_overview.status_code == 200
    assert {
        item["plid"]
        for item in all_store_own_store_overview.json()["store_items"]
    } == {"12345678", "87654321"}
    assert one_plid_own_store_overview.status_code == 200
    assert [
        item["plid"]
        for item in one_plid_own_store_overview.json()["store_items"]
    ] == ["87654321"]
    assert operating_own_store_overview.status_code == 200
    assert {
        item["plid"]
        for item in operating_own_store_overview.json()["store_items"]
    } == {"12345678"}
    assert "items" not in own_store_overview.json()
    assert "own_follower_events" not in own_store_overview.json()
    assert all(item["来源"] == "own_store" for item in overview.json()["store_items"])
    assert private_add.status_code == 200
    private_payload = private_add.json()
    assert private_payload | {"personal_watchlist_item": None} == {
        "item": None,
        "queued_to_active_batch": False,
        "automatic_store_target": True,
        "store_names": ["Beta Store"],
        "personal_watchlist_member": True,
        "personal_watchlist_item": None,
    }
    assert private_payload["personal_watchlist_item"] | {"added_at": None} == {
        "plid": "87654321",
        "added_at": None,
        "source": "own_store",
        "library_ids": [own_default_library_id],
    }
    assert private_watchlist.status_code == 200
    assert any(
        item["plid"] == "87654321" and item["source"] == "own_store"
        for item in private_watchlist.json()["items"]
    )
    assert competitor_add.status_code == 200
    assert competitor_add.json()["item"]["plid"] == "77777777"
    assert competitor_add.json()["automatic_store_target"] is False


def test_personal_watchlist_blocks_own_store_plids_outside_user_store_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "personal-watchlist-store-scope.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as admin:
        engine = create_engine(database_url)
        create_schema(engine)
        now = datetime(2026, 8, 11, 5, tzinfo=UTC)
        with Session(engine) as session, session.begin():
            current_store = session.scalar(
                select(ErpStore).where(ErpStore.code == "current")
            )
            assert current_store is not None
            current_store.display_name = "Alpha Store"
            current_store.active = True
            current_store.data_connected = True
            current_store_id = current_store.id
            session.add(
                ErpStore(
                        code="store-02",
                        display_name="Beta Store",
                        active=True,
                        data_connected=True,
                        created_at=now,
                        updated_at=now,
                )
            )
        with store_scope("current"), Session(engine) as session, session.begin():
            session.add(
                OfferCurrent(
                    offer_id="shared-alpha-offer",
                    productline_id="22222222",
                    sku="ALPHA-SHARED",
                    title="Shared Authorized Product",
                    selling_price=100,
                    total_stock=3,
                    captured_at=now,
                )
            )
        with store_scope("store-02"), Session(engine) as session, session.begin():
            session.add_all(
                [
                    OfferCurrent(
                        offer_id="beta-private-offer",
                        productline_id="11111111",
                        sku="BETA-PRIVATE",
                        title="Unauthorized Own Product",
                        selling_price=200,
                        total_stock=4,
                        captured_at=now,
                    ),
                    OfferCurrent(
                        offer_id="shared-beta-offer",
                        productline_id="22222222",
                        sku="BETA-SHARED",
                        title="Shared Unauthorized Store Copy",
                        selling_price=101,
                        total_stock=2,
                        captured_at=now,
                    ),
                ]
            )
        engine.dispose()
        issued = _bootstrap(admin)
        admin_csrf = str(issued["csrf_token"])

        created_user = admin.post(
            "/api/auth/users",
            headers={"X-CSRF-Token": admin_csrf},
            json={
                "username": "alpha.operator",
                "display_name": "Alpha Operator",
                "password": "operator-password-123",
                "role": "operator",
                "permissions": ["competitors.view", "competitors.collect"],
                "all_stores": False,
                "store_ids": [current_store_id],
            },
        )
        assert created_user.status_code == 200
        operator_user_id = int(created_user.json()["user"]["id"])
        private_membership = admin.put(
            "/api/competitors/personal-watchlist/11111111",
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert private_membership.status_code == 200
        private_library_response = admin.post(
            "/api/competitors/personal-watchlist/libraries",
            headers={"X-CSRF-Token": admin_csrf},
            json={"name": "Private Shared Library"},
        )
        assert private_library_response.status_code == 200
        private_library_id = int(private_library_response.json()["library"]["id"])
        assigned_private = admin.put(
            "/api/competitors/personal-watchlist/11111111/libraries",
            headers={"X-CSRF-Token": admin_csrf},
            json={"library_ids": [private_library_id]},
        )
        assert assigned_private.status_code == 200
        shared_private = admin.put(
            f"/api/competitors/personal-watchlist/libraries/{private_library_id}/shares",
            headers={"X-CSRF-Token": admin_csrf},
            json={"shares": [{"user_id": operator_user_id, "permission": "read"}]},
        )
        assert shared_private.status_code == 200

        with TestClient(app, client=("192.168.1.8", 50001)) as operator:
            login = operator.post(
                "/api/auth/login",
                json={
                    "username": "alpha.operator",
                    "password": "operator-password-123",
                },
            )
            assert login.status_code == 200
            assert [
                store["code"] for store in login.json()["user"]["accessible_stores"]
            ] == ["current"]
            headers = {"X-CSRF-Token": str(login.json()["csrf_token"])}

            denied_personal = operator.put(
                "/api/competitors/personal-watchlist/11111111",
                headers=headers,
            )
            denied_target = operator.post(
                "/api/competitors/targets",
                headers=headers,
                json={"url": "https://www.takealot.com/private/PLID11111111"},
            )
            allowed_shared = operator.post(
                "/api/competitors/targets",
                headers=headers,
                json={"url": "https://www.takealot.com/shared/PLID22222222"},
            )
            personal = operator.get("/api/competitors/personal-watchlist")

        for denied in (denied_personal, denied_target):
            assert denied.status_code == 403
            assert "无权查看店铺的自有商品" in denied.json()["detail"]
            assert "不能加入个人监控池" in denied.json()["detail"]
            assert "Beta Store" not in denied.json()["detail"]
        assert allowed_shared.status_code == 200
        assert allowed_shared.json()["automatic_store_target"] is True
        assert allowed_shared.json()["store_names"] == ["Alpha Store"]
        assert personal.status_code == 200
        assert [item["plid"] for item in personal.json()["items"]] == ["22222222"]
        assert personal.json()["shared_items"] == [
            {
                "plid": "11111111",
                "added_at": personal.json()["shared_items"][0]["added_at"],
                "library_ids": [private_library_id],
                "source": "own_store",
                "detail_access": "store_access_denied",
            }
        ]

        engine = create_engine(database_url)
        with Session(engine) as session:
            assert session.get(
                OwnStorePersonalWatchlist,
                (operator_user_id, "11111111"),
            ) is None
            assert session.get(CompetitorTarget, "11111111") is None
        engine.dispose()


def test_collect_routes_own_store_plid_to_follower_only_collection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict[str, object]] = []

    class RoutingCollector:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def collect(
            self,
            url: str,
            *,
            with_stock_probe: bool,
            visible_browser: bool = False,
            followers_only: bool = False,
        ) -> CompetitorCollectionResult:
            calls.append(
                {
                    "url": url,
                    "with_stock_probe": with_stock_probe,
                    "visible_browser": visible_browser,
                    "followers_only": followers_only,
                }
            )
            return CompetitorCollectionResult(
                plid="12345678",
                title="Own Product",
                succeeded=True,
                message="仅采集跟卖",
            )

    database_path = tmp_path / "erp-own-store-collect.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv(
        "TAKEALOT_STORES",
        "current|Alpha Store|STORE_KEY_ALPHA;store-02|Beta Store|STORE_KEY_BETA",
    )
    monkeypatch.setenv("STORE_KEY_ALPHA", "alpha-key")
    monkeypatch.setenv("STORE_KEY_BETA", "beta-key")
    monkeypatch.setattr("takealot_ops.erp.web.CompetitorCollector", RoutingCollector)
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        engine = create_engine(database_url)
        now = datetime(2026, 8, 3, 1, tzinfo=UTC)
        with Session(engine) as database_session, database_session.begin():
            current_store = database_session.scalar(
                select(ErpStore).where(ErpStore.code == "current")
            )
            assert current_store is not None
            current_store.display_name = "Alpha Store"
            current_store.active = True
            current_store.data_connected = True
            database_session.add(
                ErpStore(
                    code="store-02",
                    display_name="Beta Store",
                    active=True,
                    data_connected=True,
                    created_at=now,
                    updated_at=now,
                )
            )
        with store_scope("store-02"), Session(engine) as database_session, database_session.begin():
            database_session.add(
                OfferCurrent(
                    offer_id="own-offer",
                    productline_id="12345678",
                    sku="OWN-SKU",
                    title="Own Product",
                    selling_price=99,
                    total_stock=5,
                    captured_at=now,
                )
            )
        engine.dispose()

        response = client.post(
            "/api/competitors/collect",
            headers={"X-CSRF-Token": str(session["csrf_token"])},
            json={
                "url": "https://www.takealot.com/own-product/PLID12345678",
                "with_stock_probe": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["added_target_count"] == 0
    assert calls == [
        {
            "url": "https://www.takealot.com/own-product/PLID12345678",
            "with_stock_probe": True,
            "visible_browser": False,
            "followers_only": True,
        }
    ]


def test_product_thumbnail_is_authenticated_and_rejects_untrusted_hosts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        unauthorized = client.get(
            "/api/erp/product-thumbnail",
            params={"image_url": TRUSTED_PRODUCT_IMAGE_URL},
        )
        assert unauthorized.status_code == 401
        _bootstrap(client)

        rejected = client.get(
            "/api/erp/product-thumbnail",
            params={"image_url": "https://example.com/image.jpg"},
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"] == "只允许读取 Takealot 官方商品图片"

        invalid_size = client.get(
            "/api/erp/product-thumbnail",
            params={"image_url": TRUSTED_PRODUCT_IMAGE_URL, "size": 512},
        )
        assert invalid_size.status_code == 422
        assert invalid_size.json()["detail"] == "缩略图尺寸只支持 192、384、640 像素"

        thumbnail = tmp_path / "thumbnail.jpg"
        thumbnail.write_bytes(b"\xff\xd8\xff\xd9")
        requested_urls: list[str] = []

        requested_sizes: list[int] = []

        def fake_thumbnail_path(image_url: str, size: int) -> Path:
            requested_urls.append(image_url)
            requested_sizes.append(size)
            return thumbnail

        monkeypatch.setattr(
            app.state.product_thumbnail_cache,
            "thumbnail_path",
            fake_thumbnail_path,
        )
        response = client.get(
            "/api/erp/product-thumbnail",
            params={"image_url": TRUSTED_PRODUCT_IMAGE_URL, "size": 640},
        )

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == ("private, max-age=604800, immutable")
        assert requested_urls == [TRUSTED_PRODUCT_IMAGE_URL]
        assert requested_sizes == [640]


def test_product_thumbnail_without_store_header_supports_limited_store_account(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp-thumbnail-store-scope.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as admin:
        session = _bootstrap(admin)
        csrf = str(session["csrf_token"])
        created_store = admin.post(
            "/api/auth/stores",
            headers={"X-CSRF-Token": csrf},
            json={"code": "store-03", "display_name": "Store 03"},
        )
        assert created_store.status_code == 200
        store_id = int(created_store.json()["store"]["id"])
        created_user = admin.post(
            "/api/auth/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "limited.images",
                "display_name": "Limited Images",
                "password": "operator-password-123",
                "role": "operator",
                "all_stores": False,
                "store_ids": [store_id],
            },
        )
        assert created_user.status_code == 200

        thumbnail = tmp_path / "thumbnail.jpg"
        thumbnail.write_bytes(b"\xff\xd8\xff\xd9")
        requested_urls: list[str] = []

        def fake_thumbnail_path(image_url: str, _size: int) -> Path:
            requested_urls.append(image_url)
            return thumbnail

        monkeypatch.setattr(
            app.state.product_thumbnail_cache,
            "thumbnail_path",
            fake_thumbnail_path,
        )

        with TestClient(app, client=("192.168.1.8", 50001)) as operator:
            login = operator.post(
                "/api/auth/login",
                json={
                    "username": "limited.images",
                    "password": "operator-password-123",
                },
            )
            assert login.status_code == 200
            assert [
                store["code"] for store in login.json()["user"]["accessible_stores"]
            ] == ["store-03"]

            response = operator.get(
                "/api/erp/product-thumbnail",
                params={"image_url": TRUSTED_PRODUCT_IMAGE_URL},
            )

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert requested_urls == [TRUSTED_PRODUCT_IMAGE_URL]


def test_erp_requires_login_and_bootstraps_only_from_loopback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for config_name in ("anomaly_rules.yaml", "sale_status_rules.yaml"):
        (config_dir / config_name).write_bytes(
            (PROJECT_ROOT / "config" / config_name).read_bytes()
        )
    app = create_app(tmp_path)

    with TestClient(app, client=("192.168.1.8", 50000)) as remote:
        status = remote.get("/api/auth/status")
        assert status.json() == {
            "setup_required": True,
            "bootstrap_allowed": False,
        }
        denied = remote.post(
            "/api/auth/bootstrap",
            json={
                "username": "remoteadmin",
                "display_name": "Remote",
                "password": "correct-horse-battery",
            },
        )
        assert denied.status_code == 403
        assert remote.get("/api/erp/summary?as_of=2026-07-20").status_code == 401

    with TestClient(app, client=("127.0.0.1", 50001)) as local:
        too_short = local.post(
            "/api/auth/bootstrap",
            json={
                "username": "localadmin",
                "display_name": "Local Admin",
                "password": "pass123",
            },
        )
        assert too_short.status_code == 422
        assert too_short.json()["detail"] == "密码至少需要 8 个字符"
        session = _bootstrap(local)
        assert session["user"]["role"] == "admin"
        assert "users.manage" in session["user"]["permissions"]
        assert session["user"]["permissions_customized"] is False
        summary = local.get("/api/erp/summary?as_of=2026-07-20")
        assert summary.status_code == 200
        assert summary.json()["latest_metric_date"] is None
        assert database_path.exists()


def test_store_assignments_scale_and_all_store_accounts_include_future_stores(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp-store-access.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as admin:
        session = _bootstrap(admin)
        csrf = str(session["csrf_token"])
        assert session["user"]["all_stores"] is True
        assert len(session["user"]["accessible_stores"]) == 1

        initial_stores = admin.get("/api/auth/stores")
        assert initial_stores.status_code == 200
        current_store = initial_stores.json()["items"][0]
        assert current_store["code"] == "current"
        assert current_store["data_connected"] is True

        planned_stores: list[dict[str, object]] = []
        for number in range(2, 7):
            created = admin.post(
                "/api/auth/stores",
                headers={"X-CSRF-Token": csrf},
                json={
                    "code": f"shop-{number:02d}",
                    "display_name": f"店铺 {number}",
                },
            )
            assert created.status_code == 200
            planned_stores.append(created.json()["store"])

        duplicate = admin.post(
            "/api/auth/stores",
            headers={"X-CSRF-Token": csrf},
            json={"code": "shop-02", "display_name": "重复店铺"},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"] == "该店铺代码已存在"

        admin_session = admin.get("/api/auth/session")
        assert admin_session.status_code == 200
        assert len(admin_session.json()["user"]["accessible_stores"]) == 6

        current_store_id = int(current_store["id"])
        planned_ids = [int(store["id"]) for store in planned_stores]
        operator_one = admin.post(
            "/api/auth/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "operator.one",
                "display_name": "运营一",
                "password": "operator-password-123",
                "role": "operator",
                "all_stores": False,
                "store_ids": [current_store_id, planned_ids[0]],
            },
        )
        assert operator_one.status_code == 200
        operator_one_user = operator_one.json()["user"]
        assert operator_one_user["all_stores"] is False
        assert operator_one_user["assigned_store_ids"] == [
            current_store_id,
            planned_ids[0],
        ]

        operator_two = admin.post(
            "/api/auth/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "operator.two",
                "display_name": "运营二",
                "password": "operator-password-123",
                "role": "operator",
                "all_stores": False,
                "store_ids": planned_ids[1:3],
            },
        )
        assert operator_two.status_code == 200

        owner = admin.post(
            "/api/auth/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "owner.master",
                "display_name": "大师（老板）",
                "password": "owner-password-123",
                "role": "viewer",
                "all_stores": True,
                "store_ids": planned_ids[:2],
            },
        )
        assert owner.status_code == 200
        assert len(owner.json()["user"]["accessible_stores"]) == 6
        assert owner.json()["user"]["assigned_store_ids"] == planned_ids[:2]

        unknown_store = admin.post(
            "/api/auth/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "invalid.store",
                "display_name": "无效店铺",
                "password": "invalid-password-123",
                "role": "viewer",
                "all_stores": False,
                "store_ids": [999999],
            },
        )
        assert unknown_store.status_code == 422
        assert unknown_store.json()["detail"] == "店铺不存在：999999"

        protected_current = admin.patch(
            f"/api/auth/stores/{current_store_id}",
            headers={"X-CSRF-Token": csrf},
            json={"active": False},
        )
        assert protected_current.status_code == 409
        assert protected_current.json()["detail"] == "当前已接入数据的店铺不能停用"

        with TestClient(app, client=("192.168.1.8", 50001)) as first_operator:
            login = first_operator.post(
                "/api/auth/login",
                json={
                    "username": "operator.one",
                    "password": "operator-password-123",
                },
            )
            assert login.status_code == 200
            accessible_ids = {
                store["id"]
                for store in login.json()["user"]["accessible_stores"]
            }
            assert accessible_ids == {current_store_id, planned_ids[0]}
            assert first_operator.get("/api/erp/freshness").status_code == 200

            reassigned = admin.patch(
                f"/api/auth/users/{operator_one_user['id']}",
                headers={"X-CSRF-Token": csrf},
                json={
                    "all_stores": False,
                    "store_ids": planned_ids[3:5],
                },
            )
            assert reassigned.status_code == 200
            assert first_operator.get("/api/auth/session").status_code == 401

        with TestClient(app, client=("192.168.1.8", 50002)) as second_operator:
            login = second_operator.post(
                "/api/auth/login",
                json={
                    "username": "operator.two",
                    "password": "operator-password-123",
                },
            )
            assert login.status_code == 200
            denied = second_operator.get("/api/erp/freshness")
            assert denied.status_code == 403
            assert (
                denied.json()["detail"]
                == "当前账号未获授权访问已接入数据的店铺"
            )

        with TestClient(app, client=("192.168.1.8", 50003)) as owner_client:
            login = owner_client.post(
                "/api/auth/login",
                json={
                    "username": "owner.master",
                    "password": "owner-password-123",
                },
            )
            assert login.status_code == 200
            assert len(login.json()["user"]["accessible_stores"]) == 6
            assert owner_client.get("/api/erp/freshness").status_code == 200

            future = admin.post(
                "/api/auth/stores",
                headers={"X-CSRF-Token": csrf},
                json={"code": "shop-07", "display_name": "店铺 7"},
            )
            assert future.status_code == 200
            refreshed_scope = owner_client.get("/api/auth/session")
            assert refreshed_scope.status_code == 200
            assert len(refreshed_scope.json()["user"]["accessible_stores"]) == 7


def test_store_summary_compares_only_accessible_connected_stores(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp-store-summary.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as admin:
        session = _bootstrap(admin)
        csrf = str(session["csrf_token"])
        current_store = admin.get("/api/auth/stores").json()["items"][0]
        created_stores: list[dict[str, object]] = []
        for number in range(2, 5):
            response = admin.post(
                "/api/auth/stores",
                headers={"X-CSRF-Token": csrf},
                json={
                    "code": f"store-{number:02d}",
                    "display_name": f"Store {number}",
                },
            )
            assert response.status_code == 200
            created_stores.append(response.json()["store"])

        engine = create_engine(database_url)
        with Session(engine) as database_session, database_session.begin():
            connected_codes = {"store-02", "store-04"}
            stores = database_session.scalars(select(ErpStore)).all()
            for store in stores:
                if store.code in connected_codes:
                    store.data_connected = True
            captured_at = datetime(2026, 8, 7, 1, tzinfo=UTC)
            database_session.add_all(
                [
                    OfferCurrent(
                        store_code="current",
                        offer_id="summary-current-offer",
                        captured_at=captured_at,
                        takealot_available_stock=10,
                        takealot_stock_on_way=5,
                        takealot_stock_in_receiving=2,
                    ),
                    OfferCurrent(
                        store_code="store-02",
                        offer_id="summary-store-02-offer",
                        captured_at=captured_at,
                        takealot_available_stock=20,
                        takealot_stock_on_way=7,
                        takealot_stock_in_receiving=3,
                    ),
                    LogisticsProviderSnapshot(
                        store_code="current",
                        provider="w8",
                        fetched_at=captured_at,
                        payload={
                            "connected": True,
                            "warehouse": {"name": "Shared W8"},
                            "summary": {
                                "stock_total": 100,
                                "usable_stock": 70,
                                "locked_stock": 30,
                                "outbound_allocated": 8,
                                "transit_stock": 4,
                                "defective_stock": 1,
                            },
                        },
                    ),
                    DailyReportRun(
                        store_code="current",
                        run_id="summary-current-pre-close-failed",
                        business_date=date(2026, 8, 6),
                        slot="pre_close",
                        captured_at=datetime(2026, 8, 7, 1),
                        status="failed",
                        counts={"final_reason": "current period-end transport failure"},
                        created_at=datetime(2026, 8, 7, 1),
                    ),
                    DailyReportRun(
                        store_code="store-02",
                        run_id="summary-store-02-pre-close-failed",
                        business_date=date(2026, 8, 6),
                        slot="pre_close",
                        captured_at=datetime(2026, 8, 7, 1),
                        status="failed",
                        counts={"final_reason": "store-02 period-end transport failure"},
                        created_at=datetime(2026, 8, 7, 1),
                    ),
                    DailySalesMetricState(
                        store_code="current",
                        metric_date=date(2026, 8, 5),
                        ordered_units=5,
                        ordered_revenue=Decimal("500.00"),
                        source_kind="takealot_sales_api",
                        source_run_id="current-recovery-run",
                        source_details={
                            "kind": "takealot_sales_api",
                            "label": "Takealot Seller Sales API /sales 成功批次",
                            "run_id": "current-recovery-run",
                            "requested_start": "2026-07-08",
                            "requested_end": "2026-08-06",
                            "collected_at": "2026-08-07T02:00:00+00:00",
                        },
                        verified_at=datetime(2026, 8, 7, 2, tzinfo=UTC),
                        first_published_at=captured_at,
                        updated_at=datetime(2026, 8, 7, 2, tzinfo=UTC),
                        revision_count=1,
                    ),
                    DailySalesMetricState(
                        store_code="current",
                        metric_date=date(2026, 8, 6),
                        ordered_units=10,
                        ordered_revenue=Decimal("1000.00"),
                        source_kind="takealot_sales_api",
                        source_run_id="current-recovery-run",
                        source_details={
                            "kind": "takealot_sales_api",
                            "label": "Takealot Seller Sales API /sales 成功批次",
                            "run_id": "current-recovery-run",
                            "requested_start": "2026-07-08",
                            "requested_end": "2026-08-06",
                            "collected_at": "2026-08-07T02:00:00+00:00",
                        },
                        verified_at=datetime(2026, 8, 7, 2, tzinfo=UTC),
                        first_published_at=captured_at,
                        updated_at=datetime(2026, 8, 7, 2, tzinfo=UTC),
                        revision_count=0,
                    ),
                    DailySalesMetricState(
                        store_code="store-02",
                        metric_date=date(2026, 8, 5),
                        ordered_units=10,
                        ordered_revenue=Decimal("1000.00"),
                        source_kind="takealot_sales_api",
                        source_run_id="store-02-before-failure",
                        source_details={
                            "kind": "takealot_sales_api",
                            "label": "Takealot Seller Sales API /sales 成功批次",
                            "run_id": "store-02-before-failure",
                            "requested_start": "2026-07-08",
                            "requested_end": "2026-08-06",
                            "collected_at": "2026-08-07T00:30:00+00:00",
                        },
                        verified_at=datetime(2026, 8, 7, 0, 30, tzinfo=UTC),
                        first_published_at=captured_at,
                        updated_at=datetime(2026, 8, 7, 0, 30, tzinfo=UTC),
                        revision_count=0,
                    ),
                    SalesRevenueRevision(
                        store_code="current",
                        metric_date=date(2026, 8, 5),
                        change_type="corrected",
                        before_ordered_units=4,
                        after_ordered_units=5,
                        before_ordered_revenue=Decimal("400.00"),
                        after_ordered_revenue=Decimal("500.00"),
                        revenue_delta=Decimal("100.00"),
                        units_delta=1,
                        before_source={
                            "kind": "takealot_sales_api",
                            "label": "Earlier Sales API batch",
                            "run_id": "current-before-run",
                            "collected_at": "2026-08-06T02:00:00+00:00",
                        },
                        after_source={
                            "kind": "takealot_sales_api",
                            "label": "Takealot Seller Sales API /sales 成功批次",
                            "run_id": "current-recovery-run",
                            "requested_start": "2026-07-08",
                            "requested_end": "2026-08-06",
                            "collected_at": "2026-08-07T02:00:00+00:00",
                        },
                        source_run_id="current-recovery-run",
                        detected_at=datetime(2026, 8, 7, 2, tzinfo=UTC),
                    ),
                    SalesRevenueRevision(
                        store_code="current",
                        metric_date=date(2026, 8, 6),
                        change_type="corrected",
                        before_ordered_units=1,
                        after_ordered_units=10,
                        before_ordered_revenue=Decimal("100.00"),
                        after_ordered_revenue=Decimal("1000.00"),
                        revenue_delta=Decimal("900.00"),
                        units_delta=9,
                        before_source={
                            "kind": "takealot_sales_api",
                            "label": "Intraday Sales API batch",
                            "run_id": "current-intraday-run",
                            "collected_at": "2026-08-06T02:00:00+00:00",
                        },
                        after_source={
                            "kind": "takealot_sales_api",
                            "label": "First post-close Sales API baseline",
                            "run_id": "current-recovery-run",
                            "collected_at": "2026-08-07T02:00:00+00:00",
                        },
                        source_run_id="current-recovery-run",
                        detected_at=datetime(2026, 8, 7, 2, tzinfo=UTC),
                    ),
                ]
            )

        operator = admin.post(
            "/api/auth/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "summary.operator",
                "display_name": "Summary Operator",
                "password": "operator-password-123",
                "role": "operator",
                "all_stores": False,
                "store_ids": [
                    int(current_store["id"]),
                    int(created_stores[0]["id"]),
                    int(created_stores[1]["id"]),
                ],
            },
        )
        assert operator.status_code == 200

        loaded_single_store_codes: list[str] = []
        loaded_store_scopes: list[tuple[str, ...]] = []

        def fake_load_dataset(_settings, _as_of, *, engine=None):
            assert engine is not None
            store_code = current_store_code()
            loaded_single_store_codes.append(store_code)
            return store_code

        def fake_summary_payload(store_code, as_of, *, start_date=None):
            assert start_date == date(2026, 8, 1)
            multiplier = 1 if store_code == "current" else 2
            return {
                "as_of": as_of.isoformat(),
                "range_start": start_date.isoformat(),
                "range_end": as_of.isoformat(),
                "latest_metric_date": "2026-08-06",
                "kpis": {
                    "latest_ordered_units": 10 * multiplier,
                    "latest_ordered_revenue": 1000 * multiplier,
                    "seven_day_ordered_units": 50 * multiplier,
                    "latest_anomaly_products": 999 if store_code == "current" else 0,
                    "page_views_30_days": 100 * multiplier,
                    "median_conversion": 2.5 * multiplier,
                    "selling_products": 3 * multiplier,
                    "stockout_products": multiplier,
                },
                "sales_series": [
                    {
                        "metric_date": "2026-08-05",
                        "ordered_units": 5 * multiplier,
                        "effective_units": 5 * multiplier,
                        "ordered_revenue": 500 * multiplier,
                    },
                    *(
                        [
                            {
                                "metric_date": "2026-08-06",
                                "ordered_units": 10,
                                "effective_units": 10,
                                "ordered_revenue": 1000,
                            }
                        ]
                        if store_code == "current"
                        else []
                    ),
                ],
            }

        def fake_traffic_series(_engine, *, as_of, days=30):
            assert days == 6
            multiplier = 1 if current_store_code() == "current" else 2
            return [
                {
                    "business_date": as_of.isoformat(),
                    "captured_at": "2026-08-07T01:00:00+00:00",
                    "status": "success",
                    "page_views_30_days_total": 200 * multiplier,
                    "product_count": 4,
                    "missing_product_count": 0,
                    "reference": None,
                }
            ]

        def fake_store_metric_projections(
            _engine,
            store_codes,
            *,
            as_of,
            start_date=None,
        ):
            loaded_store_scopes.append(tuple(store_codes))
            return {
                store_code: fake_summary_payload(
                    store_code,
                    as_of,
                    start_date=start_date,
                )
                for store_code in store_codes
            }

        def fake_store_traffic_series(
            _engine,
            store_codes,
            *,
            as_of,
            days=30,
        ):
            assert days == 6
            return {
                store_code: [
                    {
                        "business_date": as_of.isoformat(),
                        "captured_at": "2026-08-07T01:00:00+00:00",
                        "status": "success",
                        "page_views_30_days_total": (
                            200 if store_code == "current" else 400
                        ),
                        "product_count": 4,
                        "missing_product_count": 0,
                        "reference": None,
                    }
                ]
                for store_code in store_codes
            }

        monkeypatch.setattr(
            "takealot_ops.erp.web.load_erp_dataset",
            fake_load_dataset,
        )
        monkeypatch.setattr(
            "takealot_ops.erp.web.build_summary_payload",
            fake_summary_payload,
        )
        monkeypatch.setattr(
            "takealot_ops.erp.web.period_end_traffic_series",
            fake_traffic_series,
        )
        monkeypatch.setattr(
            "takealot_ops.erp.web.load_store_metric_projections",
            fake_store_metric_projections,
        )
        monkeypatch.setattr(
            "takealot_ops.erp.web.load_store_traffic_series",
            fake_store_traffic_series,
        )
        with TestClient(app, client=("192.168.1.8", 50001)) as operator_client:
            login = operator_client.post(
                "/api/auth/login",
                json={
                    "username": "summary.operator",
                    "password": "operator-password-123",
                },
            )
            assert login.status_code == 200
            response = operator_client.get(
                "/api/erp/summary/stores?as_of=2026-08-06&start_date=2026-08-01",
                headers={"X-Store-Code": "current"},
            )
            operating_response = operator_client.get(
                "/api/erp/summary/stores"
                "?as_of=2026-08-06&start_date=2026-08-01"
                "&store_scope=operating",
                headers={"X-Store-Code": "current"},
            )
            single_store_response = operator_client.get(
                "/api/erp/summary?as_of=2026-08-06&start_date=2026-08-01",
                headers={"X-Store-Code": "current"},
            )
            invalid_range_response = operator_client.get(
                "/api/erp/summary/stores?as_of=2026-08-06&start_date=2026-08-07",
                headers={"X-Store-Code": "current"},
            )
            audit_response = operator_client.get(
                "/api/erp/summary/stores/sales-revisions",
                params={"start_date": "2026-08-05", "end_date": "2026-08-06"},
                headers={"X-Store-Code": "current"},
            )

        assert response.status_code == 200
        payload = response.json()
        assert operating_response.status_code == 200
        assert {
            item["store_code"]
            for item in operating_response.json()["stores"]
        } == {"current", "store-02"}
        assert payload["range_start"] == "2026-08-01"
        assert payload["range_end"] == "2026-08-06"
        assert payload["store_count"] == 2
        assert payload["sales_revenue_completed_through"] == "2026-08-06"
        stores_by_code = {
            item["store_code"]: item
            for item in payload["stores"]
        }
        assert set(stores_by_code) == {"current", "store-02"}
        assert stores_by_code["store-02"]["kpis"]["latest_ordered_units"] == 20
        assert (
            stores_by_code["store-02"]["latest_traffic_point"][
                "page_views_30_days_total"
            ]
            == 400
        )
        assert stores_by_code["current"]["operators"] == [
            {
                "user_id": operator.json()["user"]["id"],
                "display_name": "Summary Operator",
                "role": "operator",
            }
        ]
        assert stores_by_code["store-02"]["inventory"] == {
            "captured_at": "2026-08-07T01:00:00",
            "offer_count": 1,
            "platform_available_stock": 20,
            "platform_available_coverage": 1,
            "platform_stock_on_way": 7,
            "platform_stock_on_way_coverage": 1,
            "platform_stock_in_receiving": 3,
            "platform_stock_in_receiving_coverage": 1,
        }
        assert stores_by_code["store-02"]["health"]["state"] == "attention"
        assert stores_by_code["store-02"]["health"]["business_reasons"] == [
            "缺货商品 2 个"
        ]
        assert stores_by_code["current"]["health"]["business_reasons"] == [
            "缺货商品 1 个"
        ]
        assert stores_by_code["current"]["sales_reconciliation"]["status"] == (
            "recovered"
        )
        assert stores_by_code["store-02"]["sales_reconciliation"]["status"] == (
            "pending"
        )
        assert (
            "周期末失败后销售额尚待新的 Sales API 成功批次核验"
            in stores_by_code["store-02"]["health"]["data_reasons"]
        )
        assert payload["stores"][0]["store_code"] == "store-02"
        assert payload["health_summary"] == {
            "attention": 2,
            "data_gap": 0,
            "healthy": 0,
        }
        assert payload["sales_revenue_series"] == [
            {
                "metric_date": "2026-08-05",
                "total_ordered_revenue": 1500.0,
                "covered_store_count": 2,
                "store_count": 2,
                "missing_store_count": 0,
                "data_status": "pending",
                "source_verified_store_count": 2,
                "pending_reconciliation_store_count": 1,
                "unverified_source_store_count": 0,
                "revised_store_count": 1,
                "revision_count": 1,
                "latest_sales_verified_at": "2026-08-07T02:00:00",
                "latest_revision_at": "2026-08-07T02:00:00",
            },
            {
                "metric_date": "2026-08-06",
                "total_ordered_revenue": 1000.0,
                "covered_store_count": 1,
                "store_count": 2,
                "missing_store_count": 1,
                "data_status": "pending",
                "source_verified_store_count": 1,
                "pending_reconciliation_store_count": 1,
                "unverified_source_store_count": 1,
                "revised_store_count": 0,
                "revision_count": 0,
                "latest_sales_verified_at": "2026-08-07T02:00:00",
                "latest_revision_at": None,
            },
        ]
        assert payload["sales_reconciliation"] == {
            "period_end_business_date": "2026-08-06",
            "failed_store_count": 2,
            "pending_store_count": 1,
            "recovered_store_count": 1,
            "verified_store_count": 0,
            "unverified_store_count": 0,
            "revision_count": 1,
            "latest_sales_verified_at": "2026-08-07T02:00:00",
            "latest_revision_at": "2026-08-07T02:00:00",
            "stores": payload["sales_reconciliation"]["stores"],
        }
        assert payload["logistics"]["overseas_warehouse"]["stock_total"] == 100
        assert payload["logistics"]["platform_warehouse"] == {
            "captured_at": "2026-08-07T01:00:00",
            "store_count": 2,
            "store_count_with_offers": 2,
            "offer_count": 2,
            "platform_available_stock": 30,
            "platform_available_coverage": 2,
            "platform_stock_on_way": 12,
            "platform_stock_on_way_coverage": 2,
            "platform_stock_in_receiving": 5,
            "platform_stock_in_receiving_coverage": 2,
        }
        assert single_store_response.status_code == 200
        assert single_store_response.json()["range_start"] == "2026-08-01"
        assert single_store_response.json()["operators"][0]["display_name"] == (
            "Summary Operator"
        )
        assert loaded_single_store_codes == ["current"]
        assert len(loaded_store_scopes) == 1
        assert set(loaded_store_scopes[0]) == {"current", "store-02"}
        assert audit_response.status_code == 200
        assert invalid_range_response.status_code == 422
        audit = audit_response.json()
        assert audit["total"] == 1
        assert audit["items"][0]["store_code"] == "current"
        assert audit["items"][0]["before_ordered_revenue"] == 400.0
        assert audit["items"][0]["after_ordered_revenue"] == 500.0
        assert audit["items"][0]["before_source"]["run_id"] == "current-before-run"
        assert audit["items"][0]["after_source"]["run_id"] == "current-recovery-run"


def test_public_competitor_module_does_not_require_store_assignment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp-public-module.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as admin:
        session = _bootstrap(admin)
        csrf = str(session["csrf_token"])
        created = admin.post(
            "/api/auth/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "public.competitors",
                "display_name": "公共竞品账号",
                "password": "competitor-password-123",
                "role": "viewer",
                "permissions": [
                    "store.view",
                    "competitors.view",
                    "competitors.collect",
                    "nft102.manage",
                ],
                "all_stores": False,
                "store_ids": [],
            },
        )
        assert created.status_code == 200
        assert created.json()["user"]["accessible_stores"] == []

        with TestClient(app, client=("192.168.1.8", 50001)) as public_user:
            login = public_user.post(
                "/api/auth/login",
                json={
                    "username": "public.competitors",
                    "password": "competitor-password-123",
                },
            )
            assert login.status_code == 200
            assert login.json()["user"]["accessible_stores"] == []
            public_csrf = str(login.json()["csrf_token"])

            assert public_user.get("/api/competitors").status_code == 200
            invalid_collect = public_user.post(
                "/api/competitors/collect",
                headers={"X-CSRF-Token": public_csrf},
                json={"url": "invalid"},
            )
            assert invalid_collect.status_code == 403
            assert "仅限 kxx 账号" in invalid_collect.json()["detail"]
            assert public_user.get("/api/erp/product-thumbnail").status_code == 422
            assert (
                public_user.post(
                    "/api/erp/nft102/inspect",
                    headers={"X-CSRF-Token": public_csrf},
                ).status_code
                == 422
            )

            store_data = public_user.get("/api/erp/summary?as_of=2026-07-24")
            assert store_data.status_code == 403
            assert (
                store_data.json()["detail"]
                == "当前账号未获授权访问已接入数据的店铺"
            )
            freshness = public_user.get("/api/erp/freshness")
            assert freshness.status_code == 403
            assert (
                freshness.json()["detail"]
                == "当前账号未获授权访问已接入数据的店铺"
            )
            logistics = public_user.get("/api/erp/logistics")
            assert logistics.status_code == 403
            assert (
                logistics.json()["detail"]
                == "当前账号未获授权访问已接入数据的店铺"
            )


def test_session_lasts_seven_days_and_slides_after_activity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        issued = client.post(
            "/api/auth/bootstrap",
            json={
                "username": "localadmin",
                "display_name": "Local Admin",
                "password": "pass-123",
            },
        )
        assert issued.status_code == 200
        assert "max-age=604800" in issued.headers["set-cookie"].lower()
        initial_expiry = datetime.fromisoformat(issued.json()["expires_at"])
        assert timedelta(days=6, hours=23) < initial_expiry - datetime.utcnow()

        session_token = client.cookies.get("takealot_erp_session")
        assert session_token
        token_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        previous_expiry = datetime.utcnow() + timedelta(days=1)
        engine = create_engine(database_url)
        try:
            with Session(engine) as session, session.begin():
                record = session.get(ErpSession, token_hash)
                assert record is not None
                record.last_seen_at = datetime.utcnow()
                record.expires_at = previous_expiry

            restored = client.get("/api/auth/session")
            assert restored.status_code == 200
            assert "max-age=604800" in restored.headers["set-cookie"].lower()
            restored_expiry = datetime.fromisoformat(restored.json()["expires_at"])
            assert restored_expiry > previous_expiry + timedelta(days=5)

            immediate = client.get("/api/auth/session")
            assert immediate.status_code == 200
            assert "set-cookie" not in immediate.headers
            assert datetime.fromisoformat(immediate.json()["expires_at"]) == (restored_expiry)

            with Session(engine) as session, session.begin():
                record = session.get(ErpSession, token_hash)
                assert record is not None
                record.last_seen_at = datetime.utcnow() - timedelta(hours=23)
                record.expires_at = datetime.utcnow() + timedelta(days=6, hours=1)

            before_interval = client.get("/api/erp/freshness")
            assert before_interval.status_code == 200
            assert "set-cookie" not in before_interval.headers

            with Session(engine) as session, session.begin():
                record = session.get(ErpSession, token_hash)
                assert record is not None
                record.last_seen_at = datetime.utcnow() - timedelta(days=1, minutes=1)
                record.expires_at = datetime.utcnow() + timedelta(days=7)

            protected = client.get("/api/erp/freshness")
            assert protected.status_code == 200
            assert "max-age=604800" in protected.headers["set-cookie"].lower()
        finally:
            engine.dispose()


def test_viewer_can_read_but_cannot_run_actions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as admin:
        session = _bootstrap(admin)
        csrf = str(session["csrf_token"])
        created = admin.post(
            "/api/auth/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "viewer.one",
                "display_name": "Viewer One",
                "password": "viewer-password-123",
                "role": "viewer",
            },
        )
        assert created.status_code == 200

    with TestClient(app, client=("192.168.1.8", 50001)) as viewer:
        login = viewer.post(
            "/api/auth/login",
            json={"username": "viewer.one", "password": "viewer-password-123"},
        )
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        assert viewer.get("/api/erp/freshness").status_code == 200
        assert viewer.get("/api/erp/daily-report?business_date=2026-07-24").status_code == 200
        assert viewer.get("/api/erp/daily-report/export?through=2026-07-24").status_code == 200
        denied = viewer.post(
            "/api/competitors/collect",
            headers={"X-CSRF-Token": csrf},
            json={"url": "https://www.takealot.com/example/PLID12345678"},
        )
        assert denied.status_code == 403
        assert denied.json()["detail"] == "当前账号不能采集竞品"
        denied_daily_action = viewer.post(
            "/api/erp/daily-report/2026-07-24/offer-a/manual",
            headers={"X-CSRF-Token": csrf},
            json={
                "ordered_units": 1,
                "reason": "platform_delay",
                "note": "查看员不应写入",
            },
        )
        assert denied_daily_action.status_code == 403
        assert denied_daily_action.json()["detail"] == "当前账号可以查看运营日报，但不能处理待办"
        denied_export = viewer.post(
            "/api/erp/daily-report/export",
            headers={"X-CSRF-Token": csrf},
            json={"as_of": "2026-07-24"},
        )
        assert denied_export.status_code == 403
        assert denied_export.json()["detail"] == "当前账号不能生成运营日报 Excel"
        denied_logistics_link = viewer.post(
            "/api/erp/logistics/links",
            headers={"X-CSRF-Token": csrf},
            json={"w8_order_no": "CR260716002374", "takealot_shipment_id": 8434254},
        )
        assert denied_logistics_link.status_code == 403
        assert (
            denied_logistics_link.json()["detail"]
            == "当前账号可以查看物流数据，但不能确认或撤销物流关联"
        )
        assert viewer.get("/api/erp/keyword-traffic?as_of=2026-08-03").status_code == 200
        removed_manual_route = viewer.post(
            "/api/erp/keyword-traffic",
            headers={"X-CSRF-Token": csrf},
            json={},
        )
        assert removed_manual_route.status_code == 405
        assert viewer.get("/api/auth/users").status_code == 403


def test_keyword_traffic_routes_detect_title_change(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp-keyword-traffic.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        _bootstrap(client)
        engine = create_engine(database_url)
        with Session(engine) as session, session.begin():
            session.add(
                OfferCurrent(
                    offer_id="offer-keyword",
                    sku="SKU-KEYWORD",
                    title="Memory Foam Queen Mattress",
                    created_at=datetime(2026, 1, 15, 10, 34, tzinfo=UTC),
                    captured_at=datetime(2026, 8, 3, 1, tzinfo=UTC),
                    page_views_30_days=160,
                )
            )
            for snapshot_date, page_views, title, total_stock in (
                (date(2026, 7, 31), 100, "Memory Foam Mattress", 5),
                (date(2026, 8, 1), 110, "Memory Foam Queen Mattress", 8),
                (date(2026, 8, 2), 135, "Memory Foam Queen Mattress", 7),
                (date(2026, 8, 3), 160, "Memory Foam Queen Mattress", 9),
            ):
                session.add(
                    OfferSnapshot(
                        snapshot_date=snapshot_date,
                        offer_id="offer-keyword",
                        sku="SKU-KEYWORD",
                        title=title,
                        captured_at=datetime.combine(
                            snapshot_date,
                            datetime.min.time(),
                            tzinfo=UTC,
                        ),
                        page_views_30_days=page_views,
                        total_stock=total_stock,
                    )
                )
        engine.dispose()

        listing = client.get("/api/erp/keyword-traffic?as_of=2026-08-03")
        detail = client.get(
            "/api/erp/keyword-traffic/offer-keyword"
            "?as_of=2026-08-03&history_days=30&comparison_days=3"
        )

    assert listing.status_code == 200
    assert listing.json()["summary"]["archived_product_count"] == 1
    assert listing.json()["items"][0]["latest_page_views_30_days"] == 160
    assert detail.status_code == 200
    assert detail.json()["product"]["first_listed_at"] == "2026-01-15 12:34"
    assert detail.json()["product"]["first_listed_source"] == "platform"
    assert detail.json()["product"]["latest_restock_date"] == "2026-08-03 08:00"
    assert detail.json()["product"]["latest_restock_increase"] == 2
    assert detail.json()["product"]["current_keywords"] == [
        "Memory",
        "Foam",
        "Queen",
        "Mattress",
    ]
    assert detail.json()["events"][1]["change_label"] == "变化｜新增 1 词"
    assert detail.json()["history"][-1] == {
        "date": "2026-08-03",
        "page_views_30_days": 160,
        "source_title": "Memory Foam Queen Mattress",
    }


def test_operator_can_confirm_and_revoke_logistics_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    calls: list[tuple[str, object]] = []

    def confirm_candidate(
        self: LogisticsOverviewService,
        **values: object,
    ) -> dict[str, object]:
        del self
        calls.append(("confirmed", values))
        return {"id": 9, "active": True}

    def revoke_link(
        self: LogisticsOverviewService,
        link_id: int,
        **values: object,
    ) -> dict[str, object]:
        del self
        calls.append(("revoked", {"link_id": link_id, **values}))
        return {"id": link_id, "active": False}

    monkeypatch.setattr(LogisticsOverviewService, "confirm_candidate", confirm_candidate)
    monkeypatch.setattr(LogisticsOverviewService, "revoke_link", revoke_link)
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as admin:
        session = _bootstrap(admin)
        _create_operator(admin, str(session["csrf_token"]), username="operator.logistics")

    with TestClient(app, client=("192.168.1.8", 50001)) as operator:
        login = operator.post(
            "/api/auth/login",
            json={
                "username": "operator.logistics",
                "password": "operator-password-123",
            },
        )
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        confirmed = operator.post(
            "/api/erp/logistics/links",
            headers={"X-CSRF-Token": csrf},
            json={"w8_order_no": "CR260716002374", "takealot_shipment_id": 8434254},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["link"] == {"id": 9, "active": True}
        revoked = operator.post(
            "/api/erp/logistics/links/9/revoke",
            headers={"X-CSRF-Token": csrf},
            json={"note": "人工核对后发现并非同一批货"},
        )
        assert revoked.status_code == 200
        assert revoked.json()["link"] == {"id": 9, "active": False}

    assert calls[0][0] == "confirmed"
    assert calls[0][1] == {
        "w8_order_no": "CR260716002374",
        "takealot_shipment_id": 8434254,
        "actor_user_id": 2,
        "actor_username": "operator.logistics",
    }
    assert calls[1][0] == "revoked"
    assert calls[1][1] == {
        "link_id": 9,
        "actor_user_id": 2,
        "actor_username": "operator.logistics",
        "note": "人工核对后发现并非同一批货",
    }


def test_selection_template_and_account_permission_overrides(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as admin:
        session = _bootstrap(admin)
        csrf = str(session["csrf_token"])
        created = admin.post(
            "/api/auth/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "selection.one",
                "display_name": "Selection One",
                "password": "selection-password-123",
                "role": "selection",
            },
        )
        assert created.status_code == 200
        selection = created.json()["user"]
        assert selection["role"] == "selection"
        assert selection["permissions_customized"] is False
        assert set(selection["permissions"]) == {
            "competitors.view",
            "competitors.collect",
            "daily_report.view",
        }

        with TestClient(app, client=("192.168.1.8", 50001)) as default_selection:
            login = default_selection.post(
                "/api/auth/login",
                json={
                    "username": "selection.one",
                    "password": "selection-password-123",
                },
            )
            assert login.status_code == 200
            selection_csrf = login.json()["csrf_token"]
            blocked_collect = default_selection.post(
                "/api/competitors/collect",
                headers={"X-CSRF-Token": selection_csrf},
                json={"url": "invalid"},
            )
            assert blocked_collect.status_code == 403
            assert "仅限 kxx 账号" in blocked_collect.json()["detail"]
            denied_pending = default_selection.post(
                "/api/erp/daily-report/2026-07-24/not-found/manual",
                headers={"X-CSRF-Token": selection_csrf},
                json={
                    "ordered_units": 1,
                    "reason": "platform_delay",
                    "note": "选品模板不能处理待办",
                },
            )
            assert denied_pending.status_code == 403
            assert denied_pending.json()["detail"] == "当前账号可以查看运营日报，但不能处理待办"

        customized = admin.patch(
            f"/api/auth/users/{selection['id']}",
            headers={"X-CSRF-Token": csrf},
            json={
                "permissions": [
                    "competitors.view",
                    "daily_report.manage",
                ]
            },
        )
        assert customized.status_code == 200
        customized_user = customized.json()["user"]
        assert customized_user["permissions_customized"] is True
        assert set(customized_user["permissions"]) == {
            "competitors.view",
            "daily_report.view",
            "daily_report.manage",
        }

        with TestClient(app, client=("192.168.1.8", 50001)) as selection_client:
            login = selection_client.post(
                "/api/auth/login",
                json={
                    "username": "selection.one",
                    "password": "selection-password-123",
                },
            )
            assert login.status_code == 200
            selection_csrf = login.json()["csrf_token"]
            assert selection_client.get("/api/competitors").status_code == 200
            denied_collect = selection_client.post(
                "/api/competitors/collect",
                headers={"X-CSRF-Token": selection_csrf},
                json={"url": "invalid"},
            )
            assert denied_collect.status_code == 403
            assert (
                selection_client.get("/api/erp/daily-report?business_date=2026-07-24").status_code
                == 200
            )
            allowed_daily_write = selection_client.post(
                "/api/erp/daily-report/2026-07-24/not-found/manual",
                headers={"X-CSRF-Token": selection_csrf},
                json={
                    "ordered_units": 1,
                    "reason": "platform_delay",
                    "note": "自定义权限验证",
                },
            )
            assert allowed_daily_write.status_code != 403
            assert selection_client.get("/api/erp/summary?as_of=2026-07-24").status_code == 403

        reset = admin.patch(
            f"/api/auth/users/{selection['id']}",
            headers={"X-CSRF-Token": csrf},
            json={
                "role": "selection",
                "permissions": [
                    "competitors.view",
                    "competitors.collect",
                    "daily_report.view",
                ],
            },
        )
        assert reset.status_code == 200
        assert reset.json()["user"]["permissions_customized"] is False


def test_competitor_network_failure_returns_retryable_service_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FailingCollector:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            raise CompetitorNetworkError("Takealot 当前无法访问，请检查梯子或代理连接后重试")

        async def __aexit__(self, *_: object) -> None:
            pass

    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web.CompetitorCollector",
        FailingCollector,
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        response = client.post(
            "/api/competitors/collect",
            headers={"X-CSRF-Token": str(session["csrf_token"])},
            json={"url": "https://www.takealot.com/example/PLID12345678"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == ("Takealot 当前无法访问，请检查梯子或代理连接后重试")


@pytest.mark.parametrize(
    ("failure_kind", "expected_status"),
    [
        ("validation-uncertain", 409),
        ("stock-unprobed", 424),
        ("suspected-invalid", 404),
        ("confirmed-invalid", 410),
    ],
)
def test_competitor_link_validation_returns_distinct_status(
    tmp_path: Path,
    monkeypatch,
    failure_kind: str,
    expected_status: int,
) -> None:
    class LinkStateCollector:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def collect(self, *_: object, **__: object) -> CompetitorCollectionResult:
            return CompetitorCollectionResult(
                plid="12345678",
                title="PLID12345678",
                succeeded=False,
                message="链接复核状态",
                failure_kind=failure_kind,
            )

    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web.CompetitorCollector",
        LinkStateCollector,
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        response = client.post(
            "/api/competitors/collect",
            headers={"X-CSRF-Token": str(session["csrf_token"])},
            json={"url": "https://www.takealot.com/example/PLID12345678"},
        )

    assert response.status_code == expected_status
    assert response.json()["detail"] == "链接复核状态"


def test_competitor_batch_metadata_is_idempotent_and_logged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = 0

    class SuccessfulCollector:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def collect(self, *_: object, **__: object) -> CompetitorCollectionResult:
            nonlocal calls
            calls += 1
            return CompetitorCollectionResult(
                plid="12345678",
                title="Example product",
                succeeded=True,
                message="采集成功",
            )

    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web.CompetitorCollector",
        SuccessfulCollector,
    )
    app = create_app(tmp_path)
    payload = {
        "url": "https://www.takealot.com/example/PLID12345678",
        "batch_id": "batch-1",
        "request_id": "request-1",
        "item_index": 2,
        "total_items": 5,
    }

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        headers = {"X-CSRF-Token": str(session["csrf_token"])}
        first = client.post("/api/competitors/collect", headers=headers, json=payload)
        second = client.post("/api/competitors/collect", headers=headers, json=payload)
        event = client.post(
            "/api/competitors/batch-events",
            headers=headers,
            json={
                "batch_id": "batch-1",
                "event": "auto_resume",
                "completed": 2,
                "total": 5,
                "pending": 3,
                "reason": "page reload",
            },
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert event.status_code == 200
    assert calls == 1
    log_text = (tmp_path / "logs" / "competitor-collection.log").read_text(encoding="utf-8")
    assert "link_start batch=batch-1 request=request-1 item=3/5 plid=12345678" in log_text
    assert "link_reused batch=batch-1 request=request-1 item=3/5 plid=12345678" in log_text
    assert "batch_event batch=batch-1 event=auto_resume completed=2 total=5 pending=3" in log_text


def test_erp_reuses_and_recycles_hidden_competitor_browser(
    tmp_path: Path,
    monkeypatch,
) -> None:
    public_clients: list[object] = []
    collector_clients: list[object] = []
    link_delays: list[float] = []
    link_delay_ranges: list[tuple[float, float]] = []

    async def fake_link_cooldown(seconds: float) -> None:
        link_delays.append(seconds)

    def choose_link_cooldown(min_seconds: float, max_seconds: float) -> float:
        link_delay_ranges.append((min_seconds, max_seconds))
        return (min_seconds + max_seconds) / 2

    class FakePublicClient:
        def __init__(self) -> None:
            self.close_calls = 0
            public_clients.append(self)

        async def close(self) -> None:
            self.close_calls += 1

    class FakeCollector:
        def __init__(self, *, client: object, **_: object) -> None:
            self.client = client
            collector_clients.append(client)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def collect(
            self,
            url: str,
            **_: object,
        ) -> CompetitorCollectionResult:
            plid = url.rsplit("PLID", 1)[-1]
            if plid == "33333333":
                return CompetitorCollectionResult(
                    plid=plid,
                    title=f"PLID{plid}",
                    succeeded=False,
                    message="临时网络失败",
                    retryable=True,
                    failure_kind="network",
                )
            return CompetitorCollectionResult(
                plid=plid,
                title=f"PLID{plid}",
                succeeded=True,
                message="采集成功",
            )

    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web.CompetitorPublicClient",
        FakePublicClient,
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web.CompetitorCollector",
        FakeCollector,
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web._competitor_link_cooldown_seconds",
        choose_link_cooldown,
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web._sleep_competitor_link_cooldown",
        fake_link_cooldown,
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        headers = {"X-CSRF-Token": str(session["csrf_token"])}
        statuses = [
            client.post(
                "/api/competitors/collect",
                headers=headers,
                json={"url": f"https://www.takealot.com/example/PLID{plid}"},
            ).status_code
            for plid in ("11111111", "22222222", "33333333", "44444444")
        ]

    assert statuses == [200, 200, 503, 200]
    assert len(public_clients) == 2
    assert collector_clients[:3] == [public_clients[0]] * 3
    assert collector_clients[3] is public_clients[1]
    assert [client.close_calls for client in public_clients] == [1, 1]
    assert link_delay_ranges == [(2.0, 5.0)] * 3
    assert link_delays == [3.5, 3.5, 3.5]


def test_collect_returns_locked_when_another_link_is_still_active(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )

    def reject_parallel_link(*_: object, **__: object) -> None:
        raise CollectionBatchBusyError(
            "PLID12345678 仍在检测；已阻止另一页面并发启动新链接"
        )

    monkeypatch.setattr(
        "takealot_ops.erp.web.CollectionBatchRegistry.start_link",
        reject_parallel_link,
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        response = client.post(
            "/api/competitors/collect",
            headers={"X-CSRF-Token": str(session["csrf_token"])},
            json={
                "url": "https://www.takealot.com/example/PLID87654321",
                "batch_id": "batch-1",
                "client_id": "client-1",
                "request_id": "request-2",
                "item_index": 1,
                "total_items": 2,
            },
        )

    assert response.status_code == 423
    assert "阻止另一页面并发" in response.json()["detail"]


def test_manual_stop_cancels_active_request_and_closes_shared_browser(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    cancelled_requests: list[str | None] = []
    browser_close_calls: list[bool] = []
    cleanup_order: list[str] = []

    def stopped_batch(
        _registry: CollectionBatchRegistry,
        **_: object,
    ) -> dict[str, object]:
        return {
            "active": True,
            "event": "manual_stop",
            "current_request_id": "request-stop",
            "source": "manual",
        }

    async def cancel_request(
        _coordinator: CollectionRequestCoordinator[object],
        request_id: str | None,
    ) -> bool:
        cancelled_requests.append(request_id)
        cleanup_order.append("cancel")
        return True

    async def close_browser(_client: object) -> None:
        browser_close_calls.append(True)
        cleanup_order.append("close")

    def complete_stop(
        _registry: CollectionBatchRegistry,
        **_: object,
    ) -> dict[str, object]:
        cleanup_order.append("release")
        return {
            "active": False,
            "event": "manual_stop",
            "current_request_id": None,
            "source": "manual",
        }

    monkeypatch.setattr(CollectionBatchRegistry, "stop", stopped_batch)
    monkeypatch.setattr(CollectionBatchRegistry, "complete_stop", complete_stop)
    monkeypatch.setattr(CollectionRequestCoordinator, "cancel", cancel_request)
    monkeypatch.setattr(
        "takealot_ops.erp.web._SharedCompetitorPublicClient.close",
        close_browser,
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        response = client.post(
            "/api/competitors/batch-events",
            headers={"X-CSRF-Token": str(session["csrf_token"])},
            json={
                "batch_id": "batch-stop",
                "client_id": "client-stop",
                "event": "manual_stop",
                "completed": 1,
                "total": 3,
                "pending": 2,
            },
        )

        assert response.status_code == 200
        assert cancelled_requests == ["request-stop"]
        assert browser_close_calls == [True]
        assert cleanup_order == ["cancel", "close", "release"]


def test_manual_stop_releases_batch_when_scheduled_journal_write_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    cleanup_order: list[str] = []

    def stopped_batch(
        _registry: CollectionBatchRegistry,
        **_: object,
    ) -> dict[str, object]:
        return {
            "active": True,
            "event": "manual_stop",
            "current_request_id": "request-stop",
            "source": "scheduled",
        }

    async def fail_mark_stopped(*_: object, **__: object) -> bool:
        cleanup_order.append("mark")
        raise RuntimeError("scheduled journal unavailable")

    async def cancel_request(*_: object, **__: object) -> bool:
        cleanup_order.append("cancel")
        return True

    async def close_browser(*_: object, **__: object) -> None:
        cleanup_order.append("close")

    def complete_stop(*_: object, **__: object) -> dict[str, object]:
        cleanup_order.append("release")
        return {
            "active": False,
            "event": "manual_stop",
            "current_request_id": None,
            "source": "scheduled",
        }

    monkeypatch.setattr(CollectionBatchRegistry, "stop", stopped_batch)
    monkeypatch.setattr(CollectionBatchRegistry, "complete_stop", complete_stop)
    monkeypatch.setattr(CollectionRequestCoordinator, "cancel", cancel_request)
    monkeypatch.setattr(
        "takealot_ops.erp.web.ScheduledCompetitorBatchRunner.mark_stopped",
        fail_mark_stopped,
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web._SharedCompetitorPublicClient.close",
        close_browser,
    )
    app = create_app(tmp_path)

    with TestClient(
        app,
        client=("127.0.0.1", 50000),
        raise_server_exceptions=False,
    ) as client:
        session = _bootstrap(client)
        response = client.post(
            "/api/competitors/batch-stop",
            headers={"X-CSRF-Token": str(session["csrf_token"])},
            json={
                "batch_id": "batch-stop",
                "reason": "scheduled stop test",
            },
        )

        assert response.status_code == 500
        assert cleanup_order == ["mark", "cancel", "close", "release"]


def test_loopback_schedule_starts_visible_batch_and_kxx_can_stop_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "scheduled-visible.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )

    class BlockingCollector:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def collect(self, *_: object, **__: object) -> CompetitorCollectionResult:
            import asyncio

            await asyncio.Event().wait()
            raise AssertionError("cancelled scheduled collection must not finish normally")

    monkeypatch.setattr("takealot_ops.erp.web.CompetitorCollector", BlockingCollector)
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        headers = {"X-CSRF-Token": str(session["csrf_token"])}
        created = client.post(
            "/api/competitors/targets",
            headers=headers,
            json={"url": "https://www.takealot.com/p/PLID12345678"},
        )
        assert created.status_code == 200

        triggered = client.post("/api/internal/competitors/scheduled-trigger", json={})
        assert triggered.status_code == 200
        assert triggered.json()["accepted"] is True

        deadline = time.monotonic() + 2
        status: dict[str, object] = {}
        while time.monotonic() < deadline:
            status = client.get("/api/competitors/batch-status").json()
            if status.get("active") and status.get("current_request_id"):
                break
            time.sleep(0.01)
        assert status["source"] == "scheduled"
        assert status["owner_display_name"] == "每日 09:00 自动任务"
        assert status["total"] == 1
        assert status["current_plid"] == "12345678"

        stopped = client.post(
            "/api/competitors/batch-stop",
            headers=headers,
            json={
                "batch_id": status["batch_id"],
                "reason": "scheduled stop test",
            },
        )
        assert stopped.status_code == 200
        stopped_status = stopped.json()["status"]
        assert stopped_status["active"] is False
        assert stopped_status["event"] == "manual_stop"
        assert stopped_status["reason"] == "scheduled stop test"
        assert app.state.scheduled_competitor_runner.status()["run_status"] == "stopped"
        resumable_status = client.get("/api/competitors/batch-status").json()
        assert resumable_status["scheduled_resume_available"] is True
        assert resumable_status["scheduled_resume_pending"] == 1

        repeated_stop = client.post(
            "/api/competitors/batch-stop",
            headers=headers,
            json={
                "batch_id": status["batch_id"],
                "reason": "duplicate stop must be idempotent",
            },
        )
        assert repeated_stop.status_code == 200
        assert repeated_stop.json()["status"]["reason"] == "scheduled stop test"

        repeated = client.post("/api/internal/competitors/scheduled-trigger", json={})
        assert repeated.status_code == 200
        assert repeated.json()["accepted"] is False
        assert repeated.json()["state"] == "already_handled"

        resumed = client.post(
            "/api/competitors/batch-resume",
            headers=headers,
            json={"batch_id": status["batch_id"]},
        )
        assert resumed.status_code == 200
        resumed_status = resumed.json()["status"]
        assert resumed_status["active"] is True
        assert resumed_status["batch_id"] == status["batch_id"]
        assert resumed_status["event"] == "resume"
        assert resumed_status["scheduled_resume_available"] is False

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            resumed_status = client.get("/api/competitors/batch-status").json()
            if resumed_status.get("current_request_id"):
                break
            time.sleep(0.01)
        assert resumed_status["current_plid"] == "12345678"

        stopped_again = client.post(
            "/api/competitors/batch-stop",
            headers=headers,
            json={
                "batch_id": status["batch_id"],
                "reason": "cleanup resumed scheduled test",
            },
        )
        assert stopped_again.status_code == 200
        assert stopped_again.json()["status"]["active"] is False


def test_kxx_changes_scheduled_visible_browser_from_the_next_link(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "scheduled-visible-options.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    first_started = threading.Event()
    release_first = threading.Event()
    observed_visible_browser: list[bool] = []

    class RecordingCollector:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def collect(
            self,
            url: str,
            *,
            visible_browser: bool = False,
            **_: object,
        ) -> CompetitorCollectionResult:
            import asyncio

            call_index = len(observed_visible_browser)
            observed_visible_browser.append(visible_browser)
            if call_index == 0:
                first_started.set()
                await asyncio.to_thread(release_first.wait)
            plid = url.rsplit("PLID", 1)[1]
            return CompetitorCollectionResult(
                plid=plid,
                title=f"Product {plid}",
                succeeded=True,
                message="采集成功",
            )

    monkeypatch.setattr("takealot_ops.erp.web.CompetitorCollector", RecordingCollector)
    monkeypatch.setattr(
        "takealot_ops.erp.web._competitor_link_cooldown_seconds",
        lambda *_: 0.0,
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        csrf = str(session["csrf_token"])
        headers = {"X-CSRF-Token": csrf}
        created_admin = client.post(
            "/api/auth/users",
            headers=headers,
            json={
                "username": "admin.two",
                "display_name": "Admin Two",
                "password": "operator-password-123",
                "role": "admin",
            },
        )
        assert created_admin.status_code == 200
        for plid in ("12345678", "87654321"):
            created = client.post(
                "/api/competitors/targets",
                headers=headers,
                json={"url": f"https://www.takealot.com/p/PLID{plid}"},
            )
            assert created.status_code == 200

        try:
            triggered = client.post("/api/internal/competitors/scheduled-trigger", json={})
            assert triggered.status_code == 200
            assert triggered.json()["accepted"] is True
            assert first_started.wait(2)

            status = client.get("/api/competitors/batch-status").json()
            assert status["source"] == "scheduled"
            assert status["current_plid"] == "12345678"
            assert observed_visible_browser == [False]
            options = client.post(
                "/api/competitors/batch-options",
                headers=headers,
                json={"batch_id": status["batch_id"], "visible_browser": True},
            )
            assert options.status_code == 200
            assert options.json()["status"]["visible_browser"] is True
            assert observed_visible_browser == [False]

            logged_out = client.post("/api/auth/logout", headers=headers)
            assert logged_out.status_code == 200
            login = client.post(
                "/api/auth/login",
                json={
                    "username": "admin.two",
                    "password": "operator-password-123",
                },
            )
            assert login.status_code == 200
            blocked = client.post(
                "/api/competitors/batch-options",
                headers={"X-CSRF-Token": str(login.json()["csrf_token"])},
                json={"batch_id": status["batch_id"], "visible_browser": False},
            )
            assert blocked.status_code == 403

            release_first.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and len(observed_visible_browser) < 2:
                time.sleep(0.01)
            assert observed_visible_browser == [False, True]
        finally:
            release_first.set()


def test_schedule_trigger_rejects_non_loopback_client(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'remote-trigger.db').as_posix()}",
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("192.0.2.10", 50000)) as client:
        response = client.post("/api/internal/competitors/scheduled-trigger", json={})

    assert response.status_code == 403


def test_only_kxx_controls_batch_while_other_admin_can_add_and_prioritize(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as admin:
        session = _bootstrap(admin)
        admin_csrf = str(session["csrf_token"])
        created_admin = admin.post(
            "/api/auth/users",
            headers={"X-CSRF-Token": admin_csrf},
            json={
                "username": "admin.two",
                "display_name": "Admin Two",
                "password": "operator-password-123",
                "role": "admin",
            },
        )
        assert created_admin.status_code == 200
        started = admin.post(
            "/api/competitors/batch-events",
            headers={"X-CSRF-Token": admin_csrf},
            json={
                "batch_id": "batch-admin",
                "client_id": "client-admin",
                "event": "start",
                "completed": 0,
                "total": 12,
                "pending": 12,
                "succeeded": 0,
                "failed": 0,
                "terminal": 0,
            },
        )
        assert started.status_code == 200
        options = admin.post(
            "/api/competitors/batch-options",
            headers={"X-CSRF-Token": admin_csrf},
            json={"batch_id": "batch-admin", "visible_browser": True},
        )
        assert options.status_code == 200
        assert options.json()["status"]["visible_browser"] is True
        takeover = admin.post(
            "/api/competitors/batch-takeover",
            headers={"X-CSRF-Token": admin_csrf},
            json={
                "batch_id": "batch-admin",
                "client_id": "client-admin-takeover",
            },
        )
        assert takeover.status_code == 200
        assert takeover.json()["ready"] is True

    with TestClient(app, client=("192.168.1.8", 50001)) as other_admin:
        login = other_admin.post(
            "/api/auth/login",
            json={
                "username": "admin.two",
                "password": "operator-password-123",
            },
        )
        assert login.status_code == 200
        operator_csrf = str(login.json()["csrf_token"])
        shared = other_admin.get("/api/competitors/batch-status")
        assert shared.status_code == 200
        assert shared.json()["active"] is True
        assert shared.json()["owner_username"] == "kxx"
        blocked = other_admin.post(
            "/api/competitors/batch-events",
            headers={"X-CSRF-Token": operator_csrf},
            json={
                "batch_id": "batch-other-admin",
                "client_id": "client-other-admin",
                "event": "start",
                "completed": 0,
                "total": 3,
                "pending": 3,
            },
        )
        assert blocked.status_code == 403
        assert "仅限 kxx 账号" in blocked.json()["detail"]
        collect_blocked = other_admin.post(
            "/api/competitors/collect",
            headers={"X-CSRF-Token": operator_csrf},
            json={"url": "https://www.takealot.com/example/PLID12345678"},
        )
        assert collect_blocked.status_code == 403
        resume_blocked = other_admin.post(
            "/api/competitors/batch-resume",
            headers={"X-CSRF-Token": operator_csrf},
            json={"batch_id": "batch-admin"},
        )
        assert resume_blocked.status_code == 403
        assert "仅限 kxx 账号" in resume_blocked.json()["detail"]
        created_target = other_admin.post(
            "/api/competitors/targets",
            headers={"X-CSRF-Token": operator_csrf},
            json={"url": "https://www.takealot.com/example/PLID12345678"},
        )
        assert created_target.status_code == 200
        assert created_target.json()["queued_to_active_batch"] is True
        prioritized = other_admin.post(
            "/api/competitors/targets/12345678/prioritize",
            headers={"X-CSRF-Token": operator_csrf},
        )
        assert prioritized.status_code == 200
        stop_blocked = other_admin.post(
            "/api/competitors/batch-events",
            headers={"X-CSRF-Token": operator_csrf},
            json={
                "batch_id": "batch-admin",
                "client_id": "client-admin-takeover",
                "event": "manual_stop",
                "completed": 0,
                "total": 13,
                "pending": 13,
            },
        )
        assert stop_blocked.status_code == 403

    with TestClient(app, client=("127.0.0.1", 50002)) as admin:
        login = admin.post(
            "/api/auth/login",
            json={"username": "kxx", "password": "pass-123"},
        )
        admin_csrf = str(login.json()["csrf_token"])
        completed = admin.post(
            "/api/competitors/batch-events",
            headers={"X-CSRF-Token": admin_csrf},
            json={
                "batch_id": "batch-admin",
                "client_id": "client-admin-takeover",
                "event": "completed",
                "completed": 12,
                "total": 12,
                "pending": 0,
                "succeeded": 11,
                "failed": 1,
                "terminal": 1,
            },
        )
        assert completed.status_code == 200
        assert completed.json()["status"]["active"] is False


def test_operator_only_adds_targets_and_manages_personal_watchlist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "operator-competitor-workspace.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as admin:
        session = _bootstrap(admin)
        _create_operator(
            admin,
            str(session["csrf_token"]),
            username="operator.competitors",
        )

    with TestClient(app, client=("192.168.1.8", 50001)) as operator:
        login = operator.post(
            "/api/auth/login",
            json={
                "username": "operator.competitors",
                "password": "operator-password-123",
            },
        )
        assert login.status_code == 200
        headers = {"X-CSRF-Token": str(login.json()["csrf_token"])}

        created = operator.post(
            "/api/competitors/targets",
            headers=headers,
            json={"url": "https://www.takealot.com/example/PLID12345678"},
        )
        assert created.status_code == 200
        assert created.json()["personal_watchlist_member"] is True
        assert operator.get("/api/competitors/personal-watchlist").json()["count"] == 1
        assert operator.get("/api/competitors/targets").status_code == 200

        restricted_responses = [
            operator.patch(
                "/api/competitors/targets/12345678",
                headers=headers,
                json={
                    "url": (
                        "https://www.takealot.com/example/PLID12345678?variant=blue"
                    )
                },
            ),
            operator.post(
                "/api/competitors/targets/12345678/prioritize",
                headers=headers,
            ),
            operator.get("/api/competitors/target-audits"),
            operator.get("/api/competitors/listing-operations"),
            operator.get("/api/competitors/listing-operations/1/items"),
            operator.get("/api/competitors/link-health"),
        ]
        assert [response.status_code for response in restricted_responses] == [
            403,
            403,
            403,
            403,
            403,
            403,
        ]
        assert all(
            "仅限管理员" in response.json()["detail"]
            for response in restricted_responses
        )
        delete_rejected = operator.delete(
            "/api/competitors/targets/12345678",
            headers=headers,
        )
        assert delete_rejected.status_code == 405
        assert [
            item["plid"]
            for item in operator.get("/api/competitors/targets").json()["items"]
        ] == ["12345678"]


def test_collect_auto_adds_and_groups_new_offer_targets_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    origin_url = "https://www.takealot.com/example/PLID12345678"
    offer_url = "https://www.takealot.com/example-offer/PLID87654321"
    own_offer_url = "https://www.takealot.com/own-offer/PLID55555555"

    class OfferCollector:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def collect(self, *_: object, **__: object) -> CompetitorCollectionResult:
            return CompetitorCollectionResult(
                plid="12345678",
                title="Grouped product",
                succeeded=True,
                message="采集成功",
                discovered_targets=(
                    CompetitorDiscoveredTarget(
                        plid="12345678",
                        url=origin_url,
                        title="Grouped product",
                        seller_name="Seller One",
                        price=100.0,
                        selected=True,
                    ),
                    CompetitorDiscoveredTarget(
                        plid="87654321",
                        url=offer_url,
                        title="Grouped product",
                        seller_name="Seller Two",
                        price=110.0,
                        selected=False,
                    ),
                    CompetitorDiscoveredTarget(
                        plid="55555555",
                        url=own_offer_url,
                        title="Own grouped product",
                        seller_name="Our Store",
                        price=90.0,
                        selected=False,
                    ),
                ),
            )

    database_path = tmp_path / "offer-targets.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    monkeypatch.setattr("takealot_ops.erp.web.CompetitorCollector", OfferCollector)
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        headers = {"X-CSRF-Token": str(session["csrf_token"])}
        engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        with Session(engine) as database_session, database_session.begin():
            database_session.add(
                OfferCurrent(
                    offer_id="own-discovered-offer",
                    productline_id="55555555",
                    sku="OWN-DISCOVERED-SKU",
                    title="Own grouped product",
                    selling_price=90,
                    total_stock=5,
                    captured_at=datetime(2026, 8, 2, 1, tzinfo=UTC),
                )
            )
        engine.dispose()
        started = client.post(
            "/api/competitors/batch-events",
            headers=headers,
            json={
                "batch_id": "offer-batch",
                "client_id": "offer-client",
                "event": "start",
                "completed": 0,
                "total": 1,
                "pending": 1,
            },
        )
        assert started.status_code == 200
        first = client.post(
            "/api/competitors/collect",
            headers=headers,
            json={
                "url": origin_url,
                "batch_id": "offer-batch",
                "client_id": "offer-client",
                "request_id": "offer-request-1",
                "item_index": 0,
                "total_items": 1,
            },
        )
        second = client.post(
            "/api/competitors/collect",
            headers=headers,
            json={
                "url": origin_url,
                "batch_id": "offer-batch",
                "client_id": "offer-client",
                "request_id": "offer-request-2",
                "item_index": 0,
                "total_items": 2,
            },
        )

        assert first.status_code == 200
        assert first.json()["added_target_count"] == 1
        assert "加入 1 条跟卖链接" in first.json()["message"]
        assert second.status_code == 200
        assert second.json()["added_target_count"] == 0
        listed = client.get("/api/competitors/targets").json()["items"]
        assert {item["plid"] for item in listed} == {"12345678", "87654321"}
        assert "55555555" not in {item["plid"] for item in listed}
        assert {item["offer_group_plid"] for item in listed} == {"12345678"}
        queued = client.get("/api/competitors/batch-status").json()["queued_targets"]
        assert [item["plid"] for item in queued] == ["87654321"]
        audits = client.get("/api/competitors/target-audits").json()["items"]
        assert [item["action"] for item in audits] == ["auto_discover"]


def test_competitor_target_add_update_audit_and_active_batch_tail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(tmp_path)
    original_url = "https://www.takealot.com/example/PLID12345678"
    updated_url = f"{original_url}?variant=blue"

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        headers = {"X-CSRF-Token": str(session["csrf_token"])}
        started = client.post(
            "/api/competitors/batch-events",
            headers=headers,
            json={
                "batch_id": "batch-1",
                "client_id": "client-1",
                "event": "start",
                "completed": 0,
                "total": 1,
                "pending": 1,
            },
        )
        assert started.status_code == 200

        created = client.post(
            "/api/competitors/targets",
            headers=headers,
            json={"url": original_url},
        )
        assert created.status_code == 200
        assert created.json()["item"]["plid"] == "12345678"
        assert created.json()["queued_to_active_batch"] is True

        shared = client.get("/api/competitors/batch-status").json()
        assert shared["total"] == 2
        assert shared["pending"] == 2
        assert shared["queued_targets"][0]["url"] == original_url
        prioritized = client.post(
            "/api/competitors/targets/12345678/prioritize",
            headers=headers,
        )
        assert prioritized.status_code == 200
        priority_status = prioritized.json()["status"]
        assert priority_status["priority_targets"][0]["plid"] == "12345678"
        assert priority_status["prioritized_targets"][0]["plid"] == "12345678"
        assert priority_status["prioritized_targets"][0]["source"] == "manual"
        assert (
            priority_status["prioritized_targets"][0]["requested_by"]
            == "KXX Admin"
        )

        listed = client.get("/api/competitors/targets")
        assert listed.status_code == 200
        assert [item["plid"] for item in listed.json()["items"]] == ["12345678"]
        assert listed.json()["items"][0]["has_history"] is False

        engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        with Session(engine) as database_session:
            database_session.add(
                CompetitorSnapshot(
                    plid="12345678",
                    collected_at=datetime.now(UTC),
                    url=original_url,
                    title="Example product",
                    image_url=None,
                    stock_quantity=None,
                    stock_exact=False,
                    stock_method="not_probed",
                    review_count=0,
                    fetched_review_count=0,
                    positive_reviews=0,
                    neutral_reviews=0,
                    negative_reviews=0,
                    lifetime_sales_min=0,
                    lifetime_sales_max=0,
                    trend_label="待建立基线",
                    trend_note="首次观测",
                )
            )
            database_session.commit()
        engine.dispose()
        assert client.get("/api/competitors/targets").json()["items"][0]["has_history"] is True

        duplicate = client.post(
            "/api/competitors/targets",
            headers=headers,
            json={"url": original_url},
        )
        assert duplicate.status_code == 409

        updated = client.patch(
            "/api/competitors/targets/12345678",
            headers=headers,
            json={"url": updated_url},
        )
        assert updated.status_code == 200
        assert updated.json()["item"]["url"] == updated_url
        changed_plid = client.patch(
            "/api/competitors/targets/12345678",
            headers=headers,
            json={"url": "https://www.takealot.com/other/PLID87654321"},
        )
        assert changed_plid.status_code == 422
        invalid_host = client.post(
            "/api/competitors/targets",
            headers=headers,
            json={"url": "https://example.com/item/PLID87654321"},
        )
        assert invalid_host.status_code == 422

        delete_rejected = client.delete(
            "/api/competitors/targets/12345678",
            headers=headers,
        )
        assert delete_rejected.status_code == 405
        listed_after_delete_attempt = client.get("/api/competitors/targets").json()[
            "items"
        ]
        assert [item["plid"] for item in listed_after_delete_attempt] == ["12345678"]
        assert listed_after_delete_attempt[0]["url"] == updated_url

        audits = client.get("/api/competitors/target-audits")
        assert audits.status_code == 200
        audit_payload = audits.json()
        assert [item["action"] for item in audit_payload["items"]] == [
            "update",
            "add",
        ]
        assert all(item["actor_username"] == "kxx" for item in audit_payload["items"])
        available_date = audit_payload["date_range"]["available_start"]
        filtered = client.get(
            "/api/competitors/target-audits",
            params={"start_date": available_date, "end_date": available_date},
        )
        assert len(filtered.json()["items"]) == 2
        first_page = client.get(
            "/api/competitors/target-audits",
            params={
                "start_date": available_date,
                "end_date": available_date,
                "page": 1,
                "page_size": 2,
            },
        ).json()
        second_page = client.get(
            "/api/competitors/target-audits",
            params={
                "start_date": available_date,
                "end_date": available_date,
                "page": 2,
                "page_size": 2,
            },
        ).json()
        assert first_page["total"] == 2
        assert first_page["page"] == 1
        assert len(first_page["items"]) == 2
        assert second_page["page"] == 2
        assert second_page["items"] == []


def test_competitor_personal_watchlist_is_account_scoped_and_viewer_editable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp-personal-watchlist.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(tmp_path)
    target_url = "https://www.takealot.com/example/PLID12345678"

    with TestClient(app, client=("127.0.0.1", 50000)) as admin:
        session = _bootstrap(admin)
        csrf = str(session["csrf_token"])
        created = admin.post(
            "/api/competitors/targets",
            headers={"X-CSRF-Token": csrf},
            json={"url": target_url},
        )
        assert created.status_code == 200
        assert created.json()["personal_watchlist_member"] is True
        assert admin.get("/api/competitors/personal-watchlist").json() == {
            "items": [
                {
                    "plid": "12345678",
                    "added_at": created.json()["item"]["created_at"],
                    "source": "competitor",
                    "library_ids": [],
                }
            ],
            "count": 1,
            "shared_items": [],
            "libraries": [],
            "default_library_configured": False,
            "default_library_id": None,
        }
        for username, role in (
            ("selection.watchlist", "selection"),
            ("viewer.watchlist", "viewer"),
        ):
            user = admin.post(
                "/api/auth/users",
                headers={"X-CSRF-Token": csrf},
                json={
                    "username": username,
                    "display_name": username,
                    "password": "watchlist-password-123",
                    "role": role,
                },
            )
            assert user.status_code == 200

    with TestClient(app, client=("192.168.1.8", 50001)) as selection_client:
        login = selection_client.post(
            "/api/auth/login",
            json={
                "username": "selection.watchlist",
                "password": "watchlist-password-123",
            },
        )
        assert login.status_code == 200
        selection_csrf = str(login.json()["csrf_token"])
        assert selection_client.get(
            "/api/competitors/personal-watchlist"
        ).json()["items"] == []
        cache_generation = app.state.read_projection_cache.generation
        duplicate = selection_client.post(
            "/api/competitors/targets",
            headers={"X-CSRF-Token": selection_csrf},
            json={"url": target_url},
        )
        assert duplicate.status_code == 409
        assert app.state.read_projection_cache.generation == cache_generation + 1
        assert [
            item["plid"]
            for item in selection_client.get(
                "/api/competitors/personal-watchlist"
            ).json()["items"]
        ] == ["12345678"]

    with TestClient(app, client=("192.168.1.8", 50002)) as viewer:
        login = viewer.post(
            "/api/auth/login",
            json={
                "username": "viewer.watchlist",
                "password": "watchlist-password-123",
            },
        )
        assert login.status_code == 200
        viewer_csrf = str(login.json()["csrf_token"])
        assert viewer.get("/api/competitors/personal-watchlist").json()["count"] == 0
        added = viewer.put(
            "/api/competitors/personal-watchlist/12345678",
            headers={"X-CSRF-Token": viewer_csrf},
        )
        assert added.status_code == 200
        assert added.json()["created"] is True
        repeated = viewer.put(
            "/api/competitors/personal-watchlist/12345678",
            headers={"X-CSRF-Token": viewer_csrf},
        )
        assert repeated.status_code == 200
        assert repeated.json()["created"] is False
        removed = viewer.delete(
            "/api/competitors/personal-watchlist/12345678",
            headers={"X-CSRF-Token": viewer_csrf},
        )
        assert removed.status_code == 200
        assert removed.json()["removed"] is True
        assert viewer.get("/api/competitors/personal-watchlist").json()["count"] == 0
        assert [
            item["plid"]
            for item in viewer.get("/api/competitors/targets").json()["items"]
        ] == ["12345678"]

    with TestClient(app, client=("127.0.0.1", 50003)) as admin:
        login = admin.post(
            "/api/auth/login",
            json={"username": "kxx", "password": "pass-123"},
        )
        assert login.status_code == 200
        admin_csrf = str(login.json()["csrf_token"])
        assert [
            item["plid"]
            for item in admin.get(
                "/api/competitors/personal-watchlist"
            ).json()["items"]
        ] == ["12345678"]
        global_delete_rejected = admin.delete(
            "/api/competitors/targets/12345678",
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert global_delete_rejected.status_code == 405
        assert [
            item["plid"]
            for item in admin.get("/api/competitors/targets").json()["items"]
        ] == ["12345678"]
        assert [
            item["plid"]
            for item in admin.get(
                "/api/competitors/personal-watchlist"
            ).json()["items"]
        ] == ["12345678"]


def test_personal_watchlist_type_libraries_are_account_scoped_and_multi_selectable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp-personal-watchlist-libraries.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as admin:
        session = _bootstrap(admin)
        csrf = str(session["csrf_token"])
        headers = {"X-CSRF-Token": csrf}
        viewer_user = admin.post(
            "/api/auth/users",
            headers=headers,
            json={
                "username": "viewer.libraries",
                "display_name": "Viewer Libraries",
                "password": "watchlist-password-123",
                "role": "viewer",
            },
        )
        assert viewer_user.status_code == 200

        focus_response = admin.post(
            "/api/competitors/personal-watchlist/libraries",
            headers=headers,
            json={"name": "  Focus   Items  "},
        )
        secondary_response = admin.post(
            "/api/competitors/personal-watchlist/libraries",
            headers=headers,
            json={"name": "Secondary"},
        )
        assert focus_response.status_code == 200
        assert secondary_response.status_code == 200
        focus_library = focus_response.json()["library"]
        secondary_library = secondary_response.json()["library"]
        assert focus_library["name"] == "Focus Items"
        duplicate = admin.post(
            "/api/competitors/personal-watchlist/libraries",
            headers=headers,
            json={"name": "focus items"},
        )
        assert duplicate.status_code == 409

        renamed = admin.patch(
            f"/api/competitors/personal-watchlist/libraries/{secondary_library['id']}",
            headers=headers,
            json={"name": "Price Changes"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["library"]["name"] == "Price Changes"
        default_saved = admin.put(
            "/api/competitors/personal-watchlist/settings",
            headers=headers,
            json={"default_library_id": focus_library["id"]},
        )
        assert default_saved.json() == {
            "default_library_configured": True,
            "default_library_id": focus_library["id"],
        }

        created = admin.post(
            "/api/competitors/targets",
            headers=headers,
            json={"url": "https://www.takealot.com/example/PLID12345678"},
        )
        assert created.status_code == 200
        assert created.json()["personal_watchlist_item"]["library_ids"] == [focus_library["id"]]
        assigned = admin.put(
            "/api/competitors/personal-watchlist/12345678/libraries",
            headers=headers,
            json={
                "library_ids": [
                    secondary_library["id"],
                    focus_library["id"],
                    secondary_library["id"],
                ]
            },
        )
        assert assigned.status_code == 200
        assert assigned.json()["library_ids"] == sorted(
            [focus_library["id"], secondary_library["id"]]
        )
        admin_payload = admin.get("/api/competitors/personal-watchlist").json()
        assert admin_payload["items"][0]["library_ids"] == sorted(
            [focus_library["id"], secondary_library["id"]]
        )
        assert {item["name"]: item["item_count"] for item in admin_payload["libraries"]} == {
            "Focus Items": 1,
            "Price Changes": 1,
        }

    with TestClient(app, client=("192.168.1.8", 50001)) as viewer:
        login = viewer.post(
            "/api/auth/login",
            json={
                "username": "viewer.libraries",
                "password": "watchlist-password-123",
            },
        )
        assert login.status_code == 200
        viewer_headers = {"X-CSRF-Token": str(login.json()["csrf_token"])}
        assert viewer.get("/api/competitors/personal-watchlist").json()["libraries"] == []
        assert (
            viewer.patch(
                f"/api/competitors/personal-watchlist/libraries/{focus_library['id']}",
                headers=viewer_headers,
                json={"name": "Hijacked"},
            ).status_code
            == 404
        )
        assert (
            viewer.put(
                "/api/competitors/personal-watchlist/settings",
                headers=viewer_headers,
                json={"default_library_id": focus_library["id"]},
            ).status_code
            == 404
        )
        added = viewer.put(
            "/api/competitors/personal-watchlist/12345678",
            headers=viewer_headers,
        )
        assert added.status_code == 200
        assert added.json()["item"]["library_ids"] == []
        assert (
            viewer.put(
                "/api/competitors/personal-watchlist/12345678/libraries",
                headers=viewer_headers,
                json={"library_ids": [focus_library["id"]]},
            ).status_code
            == 404
        )

    with TestClient(app, client=("127.0.0.1", 50002)) as admin:
        login = admin.post(
            "/api/auth/login",
            json={"username": "kxx", "password": "pass-123"},
        )
        assert login.status_code == 200
        headers = {"X-CSRF-Token": str(login.json()["csrf_token"])}
        deleted_secondary = admin.delete(
            f"/api/competitors/personal-watchlist/libraries/{secondary_library['id']}",
            headers=headers,
        )
        assert deleted_secondary.status_code == 200
        assert deleted_secondary.json() == {
            "ok": True,
            "default_library_configured": True,
            "default_library_id": focus_library["id"],
        }
        explicitly_unclassified = admin.put(
            "/api/competitors/personal-watchlist/settings",
            headers=headers,
            json={"default_library_id": None},
        )
        assert explicitly_unclassified.json() == {
            "default_library_configured": True,
            "default_library_id": None,
        }
        admin.put(
            "/api/competitors/personal-watchlist/settings",
            headers=headers,
            json={"default_library_id": focus_library["id"]},
        )
        deleted_default = admin.delete(
            f"/api/competitors/personal-watchlist/libraries/{focus_library['id']}",
            headers=headers,
        )
        assert deleted_default.json() == {
            "ok": True,
            "default_library_configured": False,
            "default_library_id": None,
        }
        payload_after_library_delete = admin.get("/api/competitors/personal-watchlist").json()
        assert payload_after_library_delete["count"] == 1
        assert payload_after_library_delete["items"][0]["library_ids"] == []
        assert payload_after_library_delete["default_library_configured"] is False
        removed = admin.delete(
            "/api/competitors/personal-watchlist/12345678",
            headers=headers,
        )
        assert removed.json()["removed"] is True
        assert admin.get("/api/competitors/personal-watchlist").json()["count"] == 0
        assert [item["plid"] for item in admin.get("/api/competitors/targets").json()["items"]] == [
            "12345678"
        ]


def test_personal_watchlist_library_shares_enforce_read_and_edit_permissions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp-personal-watchlist-library-shares.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50100)) as admin:
        session = _bootstrap(admin)
        headers = {"X-CSRF-Token": str(session["csrf_token"])}
        created_users: dict[str, dict[str, object]] = {}
        for username, display_name in (
            ("library.reader", "Library Reader"),
            ("library.editor", "Library Editor"),
            ("library.inactive", "Inactive Recipient"),
        ):
            response = admin.post(
                "/api/auth/users",
                headers=headers,
                json={
                    "username": username,
                    "display_name": display_name,
                    "password": "watchlist-password-123",
                    "role": "viewer",
                },
            )
            assert response.status_code == 200
            created_users[username] = response.json()["user"]
        inactive_id = int(created_users["library.inactive"]["id"])
        deactivated = admin.patch(
            f"/api/auth/users/{inactive_id}",
            headers=headers,
            json={"active": False},
        )
        assert deactivated.status_code == 200

        library_response = admin.post(
            "/api/competitors/personal-watchlist/libraries",
            headers=headers,
            json={"name": "Shared Focus"},
        )
        assert library_response.status_code == 200
        library = library_response.json()["library"]
        assert library["access"] == "owner"
        assert library["shares"] == []
        library_id = int(library["id"])

        for plid in ("12345678", "87654321"):
            created = admin.post(
                "/api/competitors/targets",
                headers=headers,
                json={"url": f"https://www.takealot.com/example/PLID{plid}"},
            )
            assert created.status_code == 200
        assigned = admin.put(
            "/api/competitors/personal-watchlist/12345678/libraries",
            headers=headers,
            json={"library_ids": [library_id]},
        )
        assert assigned.status_code == 200
        removed_second_personal_item = admin.delete(
            "/api/competitors/personal-watchlist/87654321",
            headers=headers,
        )
        assert removed_second_personal_item.json()["removed"] is True

        reader_id = int(created_users["library.reader"]["id"])
        editor_id = int(created_users["library.editor"]["id"])
        share_response = admin.put(
            f"/api/competitors/personal-watchlist/libraries/{library_id}/shares",
            headers=headers,
            json={
                "shares": [
                    {"user_id": reader_id, "permission": "read"},
                    {"user_id": editor_id, "permission": "edit"},
                    {"user_id": inactive_id, "permission": "read"},
                ]
            },
        )
        assert share_response.status_code == 200
        shared_library = share_response.json()["library"]
        assert shared_library["share_count"] == 3
        assert {
            (share["username"], share["permission"], share["active"])
            for share in shared_library["shares"]
        } == {
            ("library.reader", "read", True),
            ("library.editor", "edit", True),
            ("library.inactive", "read", False),
        }
        assert (
            admin.put(
                f"/api/competitors/personal-watchlist/libraries/{library_id}/shares",
                headers=headers,
                json={
                    "shares": [
                        {"user_id": reader_id, "permission": "read"},
                        {"user_id": reader_id, "permission": "edit"},
                    ]
                },
            ).status_code
            == 422
        )

    with TestClient(app, client=("192.168.1.8", 50101)) as reader:
        login = reader.post(
            "/api/auth/login",
            json={
                "username": "library.reader",
                "password": "watchlist-password-123",
            },
        )
        assert login.status_code == 200
        reader_headers = {"X-CSRF-Token": str(login.json()["csrf_token"])}
        read_only_default = reader.put(
            "/api/competitors/personal-watchlist/settings",
            headers=reader_headers,
            json={"default_library_id": library_id},
        )
        assert read_only_default.status_code == 403
        assert "只读共享类型库" in read_only_default.json()["detail"]
        candidate_users = reader.get(
            "/api/competitors/personal-watchlist/share-users"
        )
        assert candidate_users.status_code == 200
        assert {item["username"] for item in candidate_users.json()["items"]} >= {
            "kxx",
            "library.editor",
            "library.inactive",
        }
        reader_payload = reader.get("/api/competitors/personal-watchlist").json()
        assert reader_payload["count"] == 0
        assert [item["plid"] for item in reader_payload["shared_items"]] == ["12345678"]
        assert reader_payload["shared_items"][0]["source"] == "competitor"
        assert reader_payload["shared_items"][0]["detail_access"] == "public"
        reader_library = reader_payload["libraries"][0]
        assert reader_library["access"] == "read"
        assert reader_library["owner_username"] == "kxx"
        assert reader_library["shares"] == []
        assert (
            reader.delete(
                f"/api/competitors/personal-watchlist/libraries/{library_id}/items/12345678",
                headers=reader_headers,
            ).status_code
            == 403
        )
        reader_add = reader.put(
            "/api/competitors/personal-watchlist/87654321",
            headers=reader_headers,
        )
        assert reader_add.status_code == 200
        assert (
            reader.put(
                "/api/competitors/personal-watchlist/87654321/libraries",
                headers=reader_headers,
                json={"library_ids": [library_id]},
            ).status_code
            == 403
        )

    with TestClient(app, client=("192.168.1.9", 50102)) as editor:
        login = editor.post(
            "/api/auth/login",
            json={
                "username": "library.editor",
                "password": "watchlist-password-123",
            },
        )
        assert login.status_code == 200
        editor_headers = {"X-CSRF-Token": str(login.json()["csrf_token"])}
        editor_default = editor.put(
            "/api/competitors/personal-watchlist/settings",
            headers=editor_headers,
            json={"default_library_id": library_id},
        )
        assert editor_default.status_code == 200
        assert editor_default.json()["default_library_id"] == library_id
        editor_payload = editor.get("/api/competitors/personal-watchlist").json()
        assert editor_payload["libraries"][0]["access"] == "edit"
        assert editor_payload["default_library_configured"] is True
        assert editor_payload["default_library_id"] == library_id
        editor_add = editor.put(
            "/api/competitors/personal-watchlist/87654321",
            headers=editor_headers,
        )
        assert editor_add.status_code == 200
        assert editor_add.json()["item"]["library_ids"] == [library_id]
        editor_assign = editor.put(
            "/api/competitors/personal-watchlist/87654321/libraries",
            headers=editor_headers,
            json={"library_ids": [library_id]},
        )
        assert editor_assign.status_code == 200
        assert editor_assign.json()["library_ids"] == [library_id]
        removed_from_shared_library = editor.delete(
            f"/api/competitors/personal-watchlist/libraries/{library_id}/items/12345678",
            headers=editor_headers,
        )
        assert removed_from_shared_library.status_code == 200
        assert removed_from_shared_library.json()["removed"] is True
        assert removed_from_shared_library.json()["library"]["item_count"] == 1
        assert (
            editor.patch(
                f"/api/competitors/personal-watchlist/libraries/{library_id}",
                headers=editor_headers,
                json={"name": "Editor Renamed"},
            ).status_code
            == 404
        )
        assert (
            editor.put(
                f"/api/competitors/personal-watchlist/libraries/{library_id}/shares",
                headers=editor_headers,
                json={"shares": []},
            ).status_code
            == 404
        )

    with TestClient(app, client=("127.0.0.1", 50103)) as admin:
        login = admin.post(
            "/api/auth/login",
            json={"username": "kxx", "password": "pass-123"},
        )
        assert login.status_code == 200
        headers = {"X-CSRF-Token": str(login.json()["csrf_token"])}
        owner_payload = admin.get("/api/competitors/personal-watchlist").json()
        assert [item["plid"] for item in owner_payload["shared_items"]] == ["87654321"]
        revoked_reader = admin.put(
            f"/api/competitors/personal-watchlist/libraries/{library_id}/shares",
            headers=headers,
            json={"shares": [{"user_id": editor_id, "permission": "edit"}]},
        )
        assert revoked_reader.status_code == 200
        assert revoked_reader.json()["library"]["share_count"] == 1
        downgraded_editor = admin.put(
            f"/api/competitors/personal-watchlist/libraries/{library_id}/shares",
            headers=headers,
            json={"shares": [{"user_id": editor_id, "permission": "read"}]},
        )
        assert downgraded_editor.status_code == 200

    with TestClient(app, client=("192.168.1.8", 50104)) as reader:
        login = reader.post(
            "/api/auth/login",
            json={
                "username": "library.reader",
                "password": "watchlist-password-123",
            },
        )
        assert login.status_code == 200
        revoked_payload = reader.get("/api/competitors/personal-watchlist").json()
        assert revoked_payload["libraries"] == []
        assert revoked_payload["shared_items"] == []
        assert revoked_payload["count"] == 1

    with TestClient(app, client=("192.168.1.9", 50105)) as editor:
        login = editor.post(
            "/api/auth/login",
            json={
                "username": "library.editor",
                "password": "watchlist-password-123",
            },
        )
        assert login.status_code == 200
        downgraded_payload = editor.get("/api/competitors/personal-watchlist").json()
        assert downgraded_payload["libraries"][0]["access"] == "read"
        assert downgraded_payload["default_library_configured"] is False
        assert downgraded_payload["default_library_id"] is None


def test_competitor_manual_retry_priority_is_audited_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp-manual-retry.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(tmp_path)
    target_url = "https://www.takealot.com/example/PLID12345678"

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        headers = {"X-CSRF-Token": str(session["csrf_token"])}
        created = client.post(
            "/api/competitors/targets",
            headers=headers,
            json={"url": target_url},
        )
        assert created.status_code == 200
        started = client.post(
            "/api/competitors/batch-events",
            headers=headers,
            json={
                "batch_id": "batch-manual-retry",
                "client_id": "client-manual-retry",
                "event": "start",
                "completed": 1,
                "total": 2,
                "pending": 1,
                "failed": 1,
            },
        )
        assert started.status_code == 200

        retried = client.post(
            "/api/competitors/targets/12345678/prioritize",
            headers=headers,
            json={"source": "manual_retry"},
        )
        duplicate = client.post(
            "/api/competitors/targets/12345678/prioritize",
            headers=headers,
            json={"source": "manual_retry"},
        )

        assert retried.status_code == 200
        assert retried.json()["accepted"] is True
        assert duplicate.status_code == 200
        assert duplicate.json()["accepted"] is False
        status = retried.json()["status"]
        assert status["priority_targets"][0]["source"] == "manual_retry"
        assert status["prioritized_targets"][0]["source"] == "manual_retry"
        audits = client.get("/api/competitors/target-audits").json()["items"]
        assert [item["action"] for item in audits] == ["manual_retry", "add"]


def test_refresh_is_kxx_only_and_always_targets_all_configured_stores(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[bool] = []

    def successful_refresh(_: Path, *, all_stores: bool = False):
        calls.append(all_stores)
        return SimpleNamespace(succeeded=True, message="刷新成功")

    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web.run_dashboard_refresh",
        successful_refresh,
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as admin:
        session = _bootstrap(admin)
        admin_csrf = str(session["csrf_token"])
        _create_operator(admin, admin_csrf, username="operator.one")

    with TestClient(app, client=("192.168.1.8", 50001)) as operator:
        login = operator.post(
            "/api/auth/login",
            json={
                "username": "operator.one",
                "password": "operator-password-123",
            },
        )
        operator_csrf = str(login.json()["csrf_token"])
        status = operator.get("/api/erp/refresh-status")
        assert status.status_code == 200
        assert status.json()["can_refresh"] is False
        blocked = operator.post(
            "/api/erp/refresh",
            headers={"X-CSRF-Token": operator_csrf},
        )
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == "刷新全部店铺数据仅限 kxx 账号"
        assert calls == []

    with TestClient(app, client=("127.0.0.1", 50002)) as admin:
        login = admin.post(
            "/api/auth/login",
            json={"username": "kxx", "password": "pass-123"},
        )
        admin_csrf = str(login.json()["csrf_token"])
        status = admin.get("/api/erp/refresh-status")
        assert status.json()["admin_exempt"] is True
        assert status.json()["can_refresh"] is True
        refreshed = admin.post(
            "/api/erp/refresh",
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["message"] == "刷新成功"
        assert refreshed.json()["refresh_status"]["can_refresh"] is True

    assert calls == [True]


def test_csrf_and_last_admin_protection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        admin_id = session["user"]["id"]
        assert (
            client.post(
                "/api/auth/users",
                json={
                    "username": "operator.one",
                    "display_name": "Operator",
                    "password": "operator-password-1",
                    "role": "operator",
                },
            ).status_code
            == 403
        )

        response = client.patch(
            f"/api/auth/users/{admin_id}",
            headers={"X-CSRF-Token": str(session["csrf_token"])},
            json={"role": "viewer"},
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "必须保留至少一个可管理用户权限的启用账号"


def test_erp_rejects_unsupported_quadrant_percentile_after_login(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'erp.db').as_posix()}",
    )
    app = create_app(tmp_path)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        _bootstrap(client)
        response = client.get("/api/erp/quadrants?as_of=2026-07-20&percentile=40")

    assert response.status_code == 422
    assert "25" in response.json()["detail"]


def test_operator_can_use_all_daily_report_reconciliation_actions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    app = create_app(tmp_path)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        admin_session = _bootstrap(client)
        _create_operator(
            client,
            str(admin_session["csrf_token"]),
            username="operator.daily",
        )
        engine = create_engine(database_url)
        try:
            with Session(engine) as session, session.begin():
                session.add_all(
                    [
                        OfferCurrent(
                            offer_id="offer-a",
                            sku="9900000000001",
                            title="Product A",
                            captured_at=datetime(2026, 7, 24, 2, tzinfo=UTC),
                            page_views_30_days=10,
                            takealot_available_stock=5,
                        ),
                        OfferCurrent(
                            offer_id="offer-b",
                            sku="9900000000002",
                            title="Product B",
                            captured_at=datetime(2026, 7, 24, 2, tzinfo=UTC),
                            page_views_30_days=12,
                            takealot_available_stock=7,
                        ),
                    ]
                )
            for slot, hour in (("morning", 2), ("evening", 10)):
                capture_daily_report(
                    engine,
                    business_date=date(2026, 7, 24),
                    slot=slot,
                    captured_at=datetime(2026, 7, 25, hour, tzinfo=UTC),
                )
        finally:
            engine.dispose()

        login = client.post(
            "/api/auth/login",
            json={
                "username": "operator.daily",
                "password": "operator-password-123",
            },
        )
        assert login.status_code == 200
        assert login.json()["user"]["role"] == "operator"
        csrf = str(login.json()["csrf_token"])

        report = client.get("/api/erp/daily-report?business_date=2026-07-24")
        assert report.status_code == 200
        assert report.headers["content-encoding"] == "gzip"
        assert report.json()["counts"]["ready"] == 2
        assert report.json()["capture_issue_range"]["selected_start"] == "2026-07-22"
        assert report.json()["capture_issue_range"]["selected_end"] == "2026-07-24"
        ranged_report = client.get(
            "/api/erp/daily-report",
            params={
                "business_date": "2026-07-24",
                "capture_start": "2026-07-24",
                "capture_end": "2026-07-24",
            },
        )
        assert ranged_report.status_code == 200
        assert ranged_report.json()["capture_issue_range"]["selected_start"] == (
            "2026-07-24"
        )
        inverted_range = client.get(
            "/api/erp/daily-report",
            params={
                "business_date": "2026-07-24",
                "capture_start": "2026-07-25",
                "capture_end": "2026-07-24",
            },
        )
        assert inverted_range.status_code == 422
        assert client.get("/api/erp/daily-report/reminders").status_code == 200
        assert client.get("/api/erp/daily-report/export?through=2026-07-24").status_code == 200
        noted = client.post(
            "/api/erp/daily-report/2026-07-24/offer-a/note",
            headers={"X-CSRF-Token": csrf},
            json={
                "note": "运营员追加一条独立备注",
                "issue_type": "general",
            },
        )
        assert noted.status_code == 200
        noted_report = client.get("/api/erp/daily-report?business_date=2026-07-24").json()
        assert noted_report["items"][0]["operator_notes"][0]["note"] == ("运营员追加一条独立备注")
        note_id = noted_report["items"][0]["operator_notes"][0]["id"]
        updated = client.patch(
            f"/api/erp/daily-report/2026-07-24/offer-a/note/{note_id}",
            headers={"X-CSRF-Token": csrf},
            json={
                "note": "运营员修改后的通用备注",
                "issue_type": "general",
            },
        )
        assert updated.status_code == 200
        updated_note = client.get("/api/erp/daily-report?business_date=2026-07-24").json()["items"][
            0
        ]["operator_notes"][0]
        assert updated_note["note"] == "运营员修改后的通用备注"
        assert updated_note["issue_type"] == "general"
        assert updated_note["updated_by"] == "Operator Daily"
        deleted = client.request(
            "DELETE",
            f"/api/erp/daily-report/2026-07-24/offer-a/note/{note_id}",
            headers={"X-CSRF-Token": csrf},
            json={},
        )
        assert deleted.status_code == 200
        after_delete = client.get("/api/erp/daily-report?business_date=2026-07-24").json()
        assert after_delete["items"][0]["operator_notes"] == []
        assert after_delete["handled_actions"][0]["action_type"] == ("operator_note_deleted")
        assert after_delete["handled_actions"][0]["note"] is None
        assert after_delete["handled_actions"][0]["detail"]["deleted_note"] == (
            "运营员修改后的通用备注"
        )

        manual = client.post(
            "/api/erp/daily-report/2026-07-24/offer-a/manual",
            headers={"X-CSRF-Token": csrf},
            json={
                "ordered_units": 1,
                "reason": "platform_delay",
            },
        )
        assert manual.status_code == 200
        manual_report = client.get("/api/erp/daily-report?business_date=2026-07-24").json()
        assert manual_report["items"][0]["manual_note"] is None
        missing_confirm_note = client.post(
            "/api/erp/daily-report/2026-07-24/offer-a/confirm",
            headers={"X-CSRF-Token": csrf},
            json={"source": "manual", "note": ""},
        )
        assert missing_confirm_note.status_code == 422
        missing_stock_note = client.post(
            "/api/erp/daily-report/2026-07-24/offer-a/stock-alert",
            headers={"X-CSRF-Token": csrf},
            json={"note": ""},
        )
        assert missing_stock_note.status_code == 422
        confirmed_manual = client.post(
            "/api/erp/daily-report/2026-07-24/offer-a/confirm",
            headers={"X-CSRF-Token": csrf},
            json={"source": "manual", "note": "采用运营员复核后的人工值"},
        )
        assert confirmed_manual.status_code == 200

        confirmed = client.post(
            "/api/erp/daily-report/2026-07-24/confirm-ready",
            headers={"X-CSRF-Token": csrf},
            json={"note": "运营员确认其余早晚一致商品"},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["confirmed"] == 1
        assert confirmed.json()["exported"] is True
        generated = client.post(
            "/api/erp/daily-report/export",
            headers={"X-CSRF-Token": csrf},
            json={"as_of": "2026-07-24"},
        )
        assert generated.status_code == 200
        download = client.get("/api/erp/daily-report/export/download?through=2026-07-24")
        assert download.status_code == 200
        assert (
            download.headers["content-type"]
            == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

        reverted = client.post(
            "/api/erp/daily-report/2026-07-24/offer-a/revert-confirmation",
            headers={"X-CSRF-Token": csrf},
            json={"note": "运营员复核后发现需要重新选择版本"},
        )
        assert reverted.status_code == 200
        reopened = client.get("/api/erp/daily-report?business_date=2026-07-24").json()
        assert reopened["items"][0]["status"] == "needs_review"
        reopened_issue_types = {issue["type"] for issue in reopened["items"][0]["review_issues"]}
        assert reopened_issue_types == {"capture_difference"}
        assert reopened["items"][0]["confirmation_revert"]["reverted_by"] == ("Operator Daily")
        repeated_revert = client.post(
            "/api/erp/daily-report/2026-07-24/offer-a/revert-confirmation",
            headers={"X-CSRF-Token": csrf},
            json={"note": "重复撤销"},
        )
        assert repeated_revert.status_code == 409
        exported = tmp_path / "exports" / "operations-daily" / "2026-07-24"
        assert any(exported.glob("*.xlsx"))


def test_daily_report_stock_difference_can_be_confirmed_logged_and_reopened(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp-stock-audit.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    app = create_app(tmp_path)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        admin_session = _bootstrap(client)
        _create_operator(
            client,
            str(admin_session["csrf_token"]),
            username="operator.stock",
        )
        engine = create_engine(database_url)
        try:
            with Session(engine) as session, session.begin():
                session.add(
                    OfferCurrent(
                        offer_id="offer-stock",
                        sku="9900000000099",
                        title="Stock Audit Product",
                        captured_at=datetime(2026, 7, 24, 2, tzinfo=UTC),
                        page_views_30_days=10,
                        takealot_available_stock=9,
                    )
                )
            for slot, hour in (("morning", 2), ("evening", 10)):
                capture_daily_report(
                    engine,
                    business_date=date(2026, 7, 24),
                    slot=slot,
                    captured_at=datetime(2026, 7, 25, hour, tzinfo=UTC),
                )
            with Session(engine) as session, session.begin():
                session.get(
                    OfferCurrent,
                    "offer-stock",
                ).takealot_available_stock = 8
            for slot, hour in (("morning", 2), ("evening", 10)):
                capture_daily_report(
                    engine,
                    business_date=date(2026, 7, 25),
                    slot=slot,
                    captured_at=datetime(2026, 7, 26, hour, tzinfo=UTC),
                )
        finally:
            engine.dispose()

        login = client.post(
            "/api/auth/login",
            json={
                "username": "operator.stock",
                "password": "operator-password-123",
            },
        )
        assert login.status_code == 200
        assert login.json()["user"]["role"] == "operator"
        csrf = str(login.json()["csrf_token"])

        before = client.get("/api/erp/daily-report?business_date=2026-07-25").json()
        assert before["pending_actions"][0]["offer_id"] == "offer-stock"
        handled = client.post(
            "/api/erp/daily-report/2026-07-25/offer-stock/stock-alert",
            headers={"X-CSRF-Token": csrf},
            json={"note": "确认属于平台库存调整"},
        )
        assert handled.status_code == 200
        after = client.get("/api/erp/daily-report?business_date=2026-07-25").json()
        assert after["pending_actions"] == []
        assert after["items"][0]["stock_check"]["mismatch"] is True
        assert after["items"][0]["stock_check"]["dismissed"] is True
        assert after["handled_actions"][0]["active"] is True
        assert after["handled_actions"][0]["handled_by"] == "Operator Stock"

        reopened = client.post(
            "/api/erp/daily-report/2026-07-25/offer-stock/stock-alert/reopen",
            headers={"X-CSRF-Token": csrf},
            json={"note": "误操作，恢复待办"},
        )
        assert reopened.status_code == 200
        final = client.get("/api/erp/daily-report?business_date=2026-07-25").json()
        assert final["pending_actions"][0]["offer_id"] == "offer-stock"
        assert final["handled_actions"][0]["action_type"] == ("stock_alert_reopened")
        assert final["handled_actions"][0]["note"] == "误操作，恢复待办"
        original = next(
            row for row in final["handled_actions"] if row["action_type"] == "stock_difference"
        )
        assert original["active"] is False
        assert original["reversal"]["note"] == ("误操作，恢复待办")

        corrected = client.post(
            "/api/erp/daily-report/2026-07-25/offer-stock/manual",
            headers={"X-CSRF-Token": csrf},
            json={
                "platform_stock": 9,
                "reason": "stock_adjustment",
                "note": "盘点后修正为连续库存9",
            },
        )
        assert corrected.status_code == 200
        corrected_payload = client.get("/api/erp/daily-report?business_date=2026-07-25").json()
        assert (
            corrected_payload["pending_actions"][0]["stock_check"]["resolution_action"]
            == "eliminate"
        )
        eliminated = client.post(
            ("/api/erp/daily-report/2026-07-25/offer-stock/stock-alert/eliminate"),
            headers={"X-CSRF-Token": csrf},
            json={"note": "采用修正库存并消除差异"},
        )
        assert eliminated.status_code == 200
        eliminated_payload = client.get("/api/erp/daily-report?business_date=2026-07-25").json()
        assert eliminated_payload["pending_actions"] == []
        assert eliminated_payload["items"][0]["stock_check"]["mismatch"] is False
        assert eliminated_payload["handled_actions"][0]["action_type"] == ("stock_eliminated")


def test_products_all_store_scope_reads_every_authorized_connected_store(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp-products-all-stores.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    app = create_app(tmp_path)

    loaded_store_codes: list[str] = []

    def fake_load_dataset(_settings, _as_of, *, engine=None):
        assert engine is not None
        store_code = current_store_code()
        loaded_store_codes.append(store_code)
        return store_code

    def fake_products_payload(store_code, _as_of):
        return {
            "latest_metric_date": "2026-08-16",
            "items": [
                {
                    "metric_date": "2026-08-16",
                    "offer_id": f"offer-{store_code}",
                    "sku": f"sku-{store_code}",
                    "title": f"Product {store_code}",
                    "ordered_units": 1,
                    "page_views_30_days": 10,
                }
            ],
        }

    monkeypatch.setattr(
        "takealot_ops.erp.web.load_product_list_dataset",
        fake_load_dataset,
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web.build_products_payload",
        fake_products_payload,
    )

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        session = _bootstrap(client)
        csrf = str(session["csrf_token"])
        created = client.post(
            "/api/auth/stores",
            headers={"X-CSRF-Token": csrf},
            json={"code": "store-02", "display_name": "第二店铺"},
        )
        assert created.status_code == 200

        engine = create_engine(database_url)
        with Session(engine) as database_session, database_session.begin():
            store = database_session.scalar(
                select(ErpStore).where(ErpStore.code == "store-02")
            )
            assert store is not None
            store.data_connected = True
        engine.dispose()

        response = client.get(
            "/api/erp/products?as_of=2026-08-16&store_scope=all",
            headers={"X-Store-Code": "current"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["store_scope"] == "all"
    assert payload["store_count"] == 2
    assert set(loaded_store_codes) == {"current", "store-02"}
    assert {
        (item["store_code"], item["store_name"], item["store_scope_key"])
        for item in payload["items"]
    } == {
        ("current", "当前店铺", "current:offer-current"),
        ("store-02", "第二店铺", "store-02:offer-store-02"),
    }


def test_product_detail_converts_rmb_cost_with_the_latest_reference_rate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp-product-cost-conversion.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web.load_product_detail_dataset",
        lambda _settings, _as_of, _offer_id, *, engine=None: object(),
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web.build_product_detail_payload",
        lambda _dataset, _as_of, offer_id: {
            "identity": {"offer_id": offer_id, "sku": "9900000000001"},
            "kpis": {},
            "history": [],
        },
    )
    monkeypatch.setattr(
        "takealot_ops.erp.web._product_master_records",
        lambda _root, records, **_kwargs: [
            {
                **dict(records[0]),
                "company_sku": "COMPANY-001",
                "cost_rmb": 268.7917,
                "cost_effective_date": "2026-08-15",
            }
        ],
    )
    app = create_app(tmp_path)
    app.state.cny_zar_rate_service = SimpleNamespace(
        latest=lambda: ExchangeRateQuote(
            rate=Decimal("2.3971"),
            rate_date=date(2026, 8, 17),
            fetched_at=datetime(2026, 8, 18, 2, 30, tzinfo=UTC),
        )
    )

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        _bootstrap(client)
        response = client.get(
            "/api/erp/products/offer-cost?as_of=2026-08-18",
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["identity"]["cost_rmb"] == 268.7917
    assert payload["cost_conversion"] == {
        "base_currency": "CNY",
        "quote_currency": "ZAR",
        "cost_rmb": 268.7917,
        "cost_zar": 644.32,
        "rate": 2.3971,
        "rate_date": "2026-08-17",
        "fetched_at": "2026-08-18T02:30:00+00:00",
        "source": "Frankfurter 机构参考汇率",
        "status": "converted",
        "message": "按最新发布的机构参考汇率换算，仅供成本估算，非交易结算价。",
    }


def test_all_store_logistics_keeps_shared_w8_once_and_tags_takealot_rows() -> None:
    stores = (
        StoreIdentity(1, "store-01", "第一店铺", True, True),
        StoreIdentity(2, "store-02", "第二店铺", True, True),
    )
    first_payload = {
        "generated_at": "2026-08-17T01:00:00+00:00",
        "cache_ttl_seconds": 900,
        "cache_age_seconds": 20,
        "w8": {
            "data_source": "local_database",
            "synced_at": "2026-08-17T02:00:00+00:00",
            "summary": {"inbound_orders": 2, "stock_total": 50},
        },
        "takealot": {
            "connected": True,
            "live_connected": False,
            "data_source": "local_database",
            "synced_at": "2026-08-17T00:50:00+00:00",
            "snapshot_saved": True,
            "refresh_attempted": False,
            "summary": {"shipments": 1, "units": 5},
            "recent_shipments": [
                {"shipment_id": "shipment-a", "created_at": "2026-08-17"}
            ],
            "warnings": [],
        },
        "matching": {
            "items": [
                {
                    "takealot_shipment_id": "shipment-a",
                    "w8_order_no": "w8-a",
                }
            ],
            "warnings": [],
        },
        "boundaries": ["原始边界"],
    }
    second_payload = {
        "generated_at": "2026-08-17T02:00:00+00:00",
        "cache_ttl_seconds": 900,
        "cache_age_seconds": 30,
        # W8 is a company-wide shared feed and must not be summed per store.
        "w8": {
            "data_source": "local_database",
            "synced_at": "2026-08-17T01:00:00+00:00",
            "summary": {"inbound_orders": 999, "stock_total": 999},
        },
        "takealot": {
            "connected": True,
            "live_connected": False,
            "data_source": "local_database",
            "synced_at": "2026-08-17T01:50:00+00:00",
            "snapshot_saved": True,
            "refresh_attempted": False,
            "summary": {"shipments": 2, "units": 7},
            "recent_shipments": [
                {"shipment_id": "shipment-b", "created_at": "2026-08-16"}
            ],
            "warnings": [],
        },
        "matching": {
            "confirmed_links": [
                {
                    "id": 8,
                    "takealot_shipment_id": "shipment-b",
                    "w8_order_no": "w8-b",
                }
            ],
            "warnings": [],
        },
        "boundaries": ["原始边界"],
    }

    payload = _aggregate_logistics_payloads(
        [(stores[0], first_payload), (stores[1], second_payload)],
        "all",
    )

    assert payload["store_count"] == 2
    assert payload["w8"]["summary"] == {"inbound_orders": 2, "stock_total": 50}
    assert payload["takealot"]["summary"] == {"shipments": 3, "units": 12}
    assert {
        (row["store_code"], row["store_scope_key"])
        for row in payload["takealot"]["recent_shipments"]
    } == {
        ("store-01", "store-01:shipment-a"),
        ("store-02", "store-02:shipment-b"),
    }
    assert payload["matching"]["confirmed_links"][0]["store_code"] == "store-02"
    assert payload["automatic_page_refresh"] is False
    assert "W8 共享仓只展示一次" in payload["boundaries"][0]


def test_all_store_platform_warehouse_is_aggregated_but_read_only() -> None:
    stores = (
        StoreIdentity(1, "store-01", "第一店铺", True, True),
        StoreIdentity(2, "store-02", "第二店铺", True, True),
    )
    common = {
        "capability": {
            "write_mode": "explicit_opt_in",
            "official_shipment_write_supported": True,
            "message": "single store",
        },
        "portal": {
            "enabled": True,
            "authenticated": True,
            "requires_otp": True,
        },
    }
    payload = _aggregate_platform_warehouse_payloads(
        [
            (
                stores[0],
                {
                    **common,
                    "generated_at": "2026-08-17T01:00:00+00:00",
                    "offers": [{"offer_id": "offer-a"}],
                    "drafts": [{"id": 1, "draft_number": "draft-a"}],
                    "platform_shipments": [{"shipment_id": "shipment-a"}],
                    "platform_snapshot_synced_at": "2026-08-17T00:00:00+00:00",
                },
            ),
            (
                stores[1],
                {
                    **common,
                    "generated_at": "2026-08-17T02:00:00+00:00",
                    "offers": [{"offer_id": "offer-b"}],
                    "drafts": [{"id": 2, "draft_number": "draft-b"}],
                    "platform_shipments": [{"shipment_id": "shipment-b"}],
                    "platform_snapshot_synced_at": "2026-08-17T01:00:00+00:00",
                },
            ),
        ],
        "all",
    )

    assert payload["store_count"] == 2
    assert {row["store_code"] for row in payload["offers"]} == {
        "store-01",
        "store-02",
    }
    assert payload["capability"]["official_shipment_write_supported"] is False
    assert payload["portal"]["enabled"] is False
    assert payload["portal"]["authenticated"] is False
    assert "必须先切换到明确单店" in payload["capability"]["message"]
    assert payload["platform_snapshot_synced_at"] == "2026-08-17T01:00:00+00:00"
def test_frontend_shell_revalidates_while_hashed_assets_are_immutable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "asset-cache.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    frontend_dist = tmp_path / "frontend" / "competitor" / "dist"
    assets = frontend_dist / "assets"
    assets.mkdir(parents=True)
    (frontend_dist / "index.html").write_text(
        '<!doctype html><script src="/assets/index-contenthash.js"></script>',
        encoding="utf-8",
    )
    (assets / "index-contenthash.js").write_text("export {};", encoding="utf-8")

    app = create_app(tmp_path)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        shell = client.get("/")
        asset = client.get("/assets/index-contenthash.js")

    assert shell.status_code == 200
    assert shell.headers["cache-control"] == "no-cache"
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
