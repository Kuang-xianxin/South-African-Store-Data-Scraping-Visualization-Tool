from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from takealot_ops.erp.anomaly_products import (
    AnomalyProductPayloadCache,
    build_anomaly_product_payload,
    load_cached_anomaly_product_payload,
)
from takealot_ops.erp.permissions import STORE_VIEW
from takealot_ops.erp.web import (
    _required_permission,
    _requires_connected_store_access,
)
from takealot_ops.metrics.service import DashboardDataset
from takealot_ops.storage.models import (
    Base,
    CollectionRun,
    DailyProductMetric,
    DailySalesMetricState,
    OfferSnapshot,
)
from takealot_ops.storage.store_context import store_scope


THROUGH = date(2026, 8, 14)


def _dataset(
    offers: list[dict[str, object]],
    metrics: list[dict[str, object]],
    history: list[dict[str, object]] | None = None,
) -> DashboardDataset:
    return DashboardDataset(
        store_daily=pd.DataFrame(),
        product_daily=pd.DataFrame(metrics),
        offer_current=pd.DataFrame(offers),
        anomalies=pd.DataFrame(),
        quality_events=pd.DataFrame(),
        offer_history=pd.DataFrame(history or []),
    )


def _offer(
    offer_id: str,
    *,
    status: str = "buyable",
    total_stock: int = 5,
    receiving: int = 0,
    on_way: int = 0,
) -> dict[str, object]:
    return {
        "offer_id": offer_id,
        "productline_id": f"PLID-{offer_id}",
        "tsin_id": f"TSIN-{offer_id}",
        "sku": f"SKU-{offer_id}",
        "title": f"Product {offer_id}",
        "image_url": f"https://example.invalid/{offer_id}.jpg",
        "selling_price": 199.0,
        "page_views_30_days": 300,
        "conversion_percentage_30_days": 2.5,
        "status": status,
        "total_stock": total_stock,
        "takealot_available_stock": total_stock,
        "seller_available_stock": 0,
        "takealot_stock_in_receiving": receiving,
        "takealot_stock_on_way": on_way,
    }


def _metric(offer_id: str, metric_date: date, units: int | None) -> dict[str, object]:
    return {
        "offer_id": offer_id,
        "metric_date": metric_date,
        "ordered_units": units,
    }


def _snapshot(
    offer_id: str,
    snapshot_date: date,
    stock: int | None,
) -> dict[str, object]:
    return {
        "offer_id": offer_id,
        "snapshot_date": snapshot_date,
        "total_stock": stock,
        "takealot_available_stock": stock,
        "seller_available_stock": 0 if stock is not None else None,
    }


def _verified_dates(days: int) -> set[date]:
    return {THROUGH - timedelta(days=offset) for offset in range(days)}


def test_anomaly_route_requires_store_permission_and_connected_store() -> None:
    path = "/api/erp/anomaly-products"

    assert _required_permission(path, "GET") == STORE_VIEW
    assert _requires_connected_store_access(path) is True


def test_sudden_sales_stop_requires_strong_baseline_then_three_verified_zeros() -> None:
    dates = [THROUGH - timedelta(days=offset) for offset in range(9, -1, -1)]
    metrics = [
        _metric("stop", metric_date, units)
        for metric_date, units in zip(dates, [1, 1, 2, 1, 1, 1, 2, 0, 0, 0], strict=True)
    ]

    payload = build_anomaly_product_payload(
        _dataset([_offer("stop")], metrics),
        requested_as_of=THROUGH,
        completed_through=THROUGH,
        verified_dates=set(dates),
    )

    assert payload["summary"]["sudden_sales_stop"] == 1
    item = payload["sudden_sales_stop"][0]
    assert item["stop_started_on"] == "2026-08-12"
    assert item["zero_sales_dates"] == [
        "2026-08-12",
        "2026-08-13",
        "2026-08-14",
    ]
    assert item["baseline_total_units"] == 9
    assert item["baseline_selling_days"] == 7
    assert item["no_sales_days"] == 3
    assert item["last_sale_on"] == "2026-08-11"


def test_unverified_day_is_not_treated_as_zero_sales() -> None:
    dates = [THROUGH - timedelta(days=offset) for offset in range(9, -1, -1)]
    metrics = [
        _metric("gap", metric_date, 1 if metric_date <= date(2026, 8, 11) else 0)
        for metric_date in dates
    ]
    verified = set(dates) - {date(2026, 8, 13)}

    payload = build_anomaly_product_payload(
        _dataset([_offer("gap")], metrics),
        requested_as_of=THROUGH,
        completed_through=THROUGH,
        verified_dates=verified,
    )

    assert payload["sudden_sales_stop"] == []
    assert payload["slow_moving"] == []


def test_non_buyable_inventory_statuses_require_sellable_stock_only() -> None:
    offers = [
        _offer("not", status="not_buyable", total_stock=0, receiving=4),
        _offer("takealot", status="disabled_by_takealot", total_stock=3),
        _offer(
            "seller",
            status="disabled_by_seller",
            total_stock=1,
            receiving=2,
            on_way=5,
        ),
        _offer(
            "transit-only",
            status="disabled_by_seller",
            total_stock=0,
            on_way=9,
        ),
    ]

    payload = build_anomaly_product_payload(
        _dataset(offers, []),
        requested_as_of=THROUGH,
        completed_through=THROUGH,
        verified_dates=set(),
    )

    groups = payload["stock_status_anomalies"]
    assert groups["not_buyable"] == []
    assert [item["offer_id"] for item in groups["disabled_by_takealot"]] == [
        "takealot"
    ]
    assert [item["offer_id"] for item in groups["disabled_by_seller"]] == [
        "seller"
    ]
    assert groups["disabled_by_takealot"][0]["available_stock"] == 3
    assert groups["disabled_by_takealot"][0]["anomaly_label"] == (
        "平台已停用但仍有可售库存"
    )
    assert groups["disabled_by_seller"][0]["inventory_units"] == 1
    assert groups["disabled_by_seller"][0]["receiving_stock"] == 2
    assert groups["disabled_by_seller"][0]["on_way_stock"] == 5
    assert payload["rules"]["stock_status_requires_available_stock"] is True
    assert payload["rules"]["stock_status_excluded_inventory"] == [
        "receiving",
        "on_way",
    ]
    assert payload["slow_moving"] == []


def test_slow_moving_counts_use_actual_consecutive_zero_days() -> None:
    dates = [THROUGH - timedelta(days=offset) for offset in range(12, -1, -1)]
    metrics = [
        _metric("slow", metric_date, 1 if index == 0 else 0)
        for index, metric_date in enumerate(dates)
    ]
    history = [_snapshot("slow", metric_date, 8) for metric_date in dates]

    payload = build_anomaly_product_payload(
        _dataset(
            [_offer("slow", status="buyable", total_stock=8)],
            metrics,
            history,
        ),
        requested_as_of=THROUGH,
        completed_through=THROUGH,
        verified_dates=set(dates),
    )

    item = payload["slow_moving"][0]
    assert item["no_sales_days"] == 12
    assert item["no_sales_days_exact"] is True
    assert item["slow_moving_started_on"] == "2026-08-03"
    assert item["last_sale_on"] == "2026-08-02"
    assert payload["summary"]["slow_moving_by_days"] == {
        "4": 1,
        "7": 1,
        "10": 1,
        "15": 0,
        "20": 0,
        "30": 0,
    }


def test_slow_moving_starts_when_stock_becomes_available() -> None:
    dates = [THROUGH - timedelta(days=offset) for offset in range(11, -1, -1)]
    metrics = [_metric("restocked", metric_date, 0) for metric_date in dates]
    history = [
        _snapshot(
            "restocked",
            metric_date,
            6 if metric_date >= date(2026, 8, 11) else 0,
        )
        for metric_date in dates
    ]

    payload = build_anomaly_product_payload(
        _dataset(
            [_offer("restocked", status="buyable", total_stock=6)],
            metrics,
            history,
        ),
        requested_as_of=THROUGH,
        completed_through=THROUGH,
        verified_dates=set(dates),
    )

    item = payload["slow_moving"][0]
    assert item["no_sales_days"] == 4
    assert item["no_sales_days_exact"] is True
    assert item["slow_moving_started_on"] == "2026-08-11"
    assert item["last_sale_on"] is None
    assert payload["summary"]["slow_moving_by_days"] == {
        "4": 1,
        "7": 0,
        "10": 0,
        "15": 0,
        "20": 0,
        "30": 0,
    }


def test_slow_moving_requires_four_complete_stocked_zero_sale_days() -> None:
    dates = [THROUGH - timedelta(days=offset) for offset in range(9, -1, -1)]
    metrics = [_metric("new-stock", metric_date, 0) for metric_date in dates]
    history = [
        _snapshot(
            "new-stock",
            metric_date,
            5 if metric_date >= date(2026, 8, 12) else 0,
        )
        for metric_date in dates
    ]

    payload = build_anomaly_product_payload(
        _dataset(
            [_offer("new-stock", status="buyable", total_stock=5)],
            metrics,
            history,
        ),
        requested_as_of=THROUGH,
        completed_through=THROUGH,
        verified_dates=set(dates),
    )

    assert payload["slow_moving"] == []
    assert payload["summary"]["slow_moving_by_days"]["4"] == 0


def test_missing_stock_history_does_not_extend_slow_moving_days() -> None:
    dates = [THROUGH - timedelta(days=offset) for offset in range(9, -1, -1)]
    metrics = [_metric("stock-gap", metric_date, 0) for metric_date in dates]
    history = [
        _snapshot("stock-gap", metric_date, 5)
        for metric_date in dates
        if metric_date >= date(2026, 8, 10)
    ]

    payload = build_anomaly_product_payload(
        _dataset(
            [_offer("stock-gap", status="buyable", total_stock=5)],
            metrics,
            history,
        ),
        requested_as_of=THROUGH,
        completed_through=THROUGH,
        verified_dates=set(dates),
    )

    item = payload["slow_moving"][0]
    assert item["no_sales_days"] == 5
    assert item["no_sales_days_exact"] is False
    assert item["slow_moving_started_on"] == "2026-08-10"


def test_current_stock_does_not_backfill_missing_historical_stock() -> None:
    dates = [THROUGH - timedelta(days=offset) for offset in range(9, -1, -1)]
    metrics = [_metric("no-stock-history", metric_date, 0) for metric_date in dates]

    payload = build_anomaly_product_payload(
        _dataset(
            [_offer("no-stock-history", status="buyable", total_stock=5)],
            metrics,
        ),
        requested_as_of=THROUGH,
        completed_through=THROUGH,
        verified_dates=set(dates),
    )

    assert payload["slow_moving"] == []
    assert payload["summary"]["slow_moving_by_days"]["4"] == 0


def test_narrow_anomaly_cache_reuses_payload_and_invalidates_after_refresh() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured_at = datetime(2026, 8, 14, 8, tzinfo=UTC)
    with store_scope("current"), Session(engine) as session:
        session.add_all(
            [
                CollectionRun(
                    run_id="offers-1",
                    run_type="offers",
                    scope_date=THROUGH,
                    started_at=captured_at,
                    finished_at=captured_at,
                    status="success",
                    counts={},
                ),
                OfferSnapshot(
                    snapshot_date=THROUGH,
                    offer_id="cached",
                    productline_id="12345678",
                    title="Cached Product",
                    status="not_buyable",
                    captured_at=captured_at,
                    total_stock=2,
                    takealot_available_stock=2,
                    seller_available_stock=0,
                    takealot_stock_in_receiving=5,
                    takealot_stock_on_way=7,
                ),
                DailyProductMetric(
                    metric_date=THROUGH,
                    offer_id="cached",
                    ordered_units=0,
                ),
                DailySalesMetricState(
                    metric_date=THROUGH,
                    ordered_units=0,
                    ordered_revenue=0,
                    source_kind="takealot_sales_api",
                    source_run_id="sales-1",
                    source_details={"verified_at": captured_at.isoformat()},
                    verified_at=captured_at,
                    first_published_at=captured_at,
                    updated_at=captured_at,
                    revision_count=0,
                ),
            ]
        )
        session.commit()
        cache = AnomalyProductPayloadCache(max_entries=4)

        first = load_cached_anomaly_product_payload(
            session,
            cache=cache,
            store_code="current",
            requested_as_of=THROUGH,
            completed_through=THROUGH,
        )
        second = load_cached_anomaly_product_payload(
            session,
            cache=cache,
            store_code="current",
            requested_as_of=THROUGH,
            completed_through=THROUGH,
        )

        assert second is first
        assert first["summary"]["not_buyable_with_stock"] == 1
        assert first["stock_status_anomalies"]["not_buyable"][0][
            "inventory_units"
        ] == 2
        assert cache.stats() == {"entries": 1, "hits": 1, "misses": 1}

        snapshot = session.get(OfferSnapshot, 1)
        assert snapshot is not None
        snapshot.total_stock = 0
        snapshot.takealot_available_stock = 0
        session.add(
            CollectionRun(
                run_id="offers-2",
                run_type="offers",
                scope_date=THROUGH,
                started_at=captured_at + timedelta(minutes=1),
                finished_at=captured_at + timedelta(minutes=1),
                status="success",
                counts={},
            )
        )
        session.commit()

        refreshed = load_cached_anomaly_product_payload(
            session,
            cache=cache,
            store_code="current",
            requested_as_of=THROUGH,
            completed_through=THROUGH,
        )

        assert refreshed is not first
        assert refreshed["summary"]["not_buyable_with_stock"] == 0
        assert cache.stats() == {"entries": 2, "hits": 1, "misses": 2}
    engine.dispose()
