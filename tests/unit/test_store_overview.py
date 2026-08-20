from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from takealot_ops.erp.store_overview import (
    load_store_inventory_projections,
    load_store_metric_projections,
    load_store_traffic_series,
)
from takealot_ops.storage.models import (
    AnomalyEvent,
    Base,
    DailyProductMetric,
    DailyReportObservation,
    DailyReportRun,
    OfferCurrent,
)


def test_store_metric_and_inventory_projections_are_narrow_and_scope_bounded() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured_at = datetime(2026, 8, 19, 2, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                DailyProductMetric(
                    store_code="current",
                    metric_date=date(2026, 8, 18),
                    offer_id="current-a",
                    ordered_units=3,
                    effective_units=2,
                    ordered_revenue=Decimal("30.00"),
                ),
                DailyProductMetric(
                    store_code="current",
                    metric_date=date(2026, 8, 19),
                    offer_id="current-a",
                    ordered_units=5,
                    effective_units=4,
                    ordered_revenue=Decimal("100.00"),
                    page_views_30_days=100,
                    conversion_percentage_30_days=Decimal("1.00"),
                    total_stock=0,
                ),
                DailyProductMetric(
                    store_code="current",
                    metric_date=date(2026, 8, 19),
                    offer_id="current-b",
                    ordered_units=0,
                    effective_units=0,
                    ordered_revenue=Decimal("0.00"),
                    page_views_30_days=None,
                    conversion_percentage_30_days=Decimal("3.00"),
                    total_stock=2,
                ),
                DailyProductMetric(
                    store_code="store-02",
                    metric_date=date(2026, 8, 18),
                    offer_id="store-02-a",
                    ordered_units=7,
                    effective_units=7,
                    ordered_revenue=Decimal("70.00"),
                    page_views_30_days=40,
                    conversion_percentage_30_days=Decimal("4.00"),
                    total_stock=0,
                ),
                DailyProductMetric(
                    store_code="secret-store",
                    metric_date=date(2026, 8, 19),
                    offer_id="secret-a",
                    ordered_units=999,
                    effective_units=999,
                    ordered_revenue=Decimal("9999.00"),
                    page_views_30_days=999,
                    conversion_percentage_30_days=Decimal("99.00"),
                    total_stock=0,
                ),
                AnomalyEvent(
                    store_code="current",
                    event_date=date(2026, 8, 19),
                    offer_id="current-a",
                    anomaly_type="sales_drop",
                    created_at=captured_at,
                ),
                AnomalyEvent(
                    store_code="current",
                    event_date=date(2026, 8, 19),
                    offer_id="current-a",
                    anomaly_type="suspected_stockout",
                    created_at=captured_at,
                ),
                OfferCurrent(
                    store_code="current",
                    offer_id="current-a",
                    captured_at=captured_at,
                    takealot_available_stock=5,
                    takealot_stock_on_way=None,
                    takealot_stock_in_receiving=2,
                ),
                OfferCurrent(
                    store_code="store-02",
                    offer_id="store-02-a",
                    captured_at=captured_at,
                    takealot_available_stock=8,
                    takealot_stock_on_way=3,
                    takealot_stock_in_receiving=None,
                ),
                OfferCurrent(
                    store_code="secret-store",
                    offer_id="secret-a",
                    captured_at=captured_at,
                    takealot_available_stock=999,
                    takealot_stock_on_way=999,
                    takealot_stock_in_receiving=999,
                ),
            ]
        )

    metrics = load_store_metric_projections(
        engine,
        ("current", "store-02"),
        as_of=date(2026, 8, 19),
        start_date=date(2026, 8, 18),
    )
    inventory = load_store_inventory_projections(
        engine,
        ("current", "store-02"),
    )

    assert set(metrics) == {"current", "store-02"}
    assert metrics["current"]["latest_metric_date"] == "2026-08-19"
    assert metrics["current"]["kpis"] == {
        "latest_ordered_units": 5,
        "latest_ordered_revenue": 100.0,
        "seven_day_ordered_units": 8,
        "latest_anomaly_products": 1,
        "page_views_30_days": 100,
        "median_conversion": 2.0,
        "selling_products": 1,
        "stockout_products": 1,
    }
    assert metrics["current"]["sales_series"] == [
        {
            "metric_date": "2026-08-18",
            "ordered_units": 3,
            "effective_units": 2,
            "ordered_revenue": 30.0,
        },
        {
            "metric_date": "2026-08-19",
            "ordered_units": 5,
            "effective_units": 4,
            "ordered_revenue": 100.0,
        },
    ]
    assert inventory["current"]["platform_available_stock"] == 5
    assert inventory["current"]["platform_stock_on_way"] is None
    assert inventory["current"]["platform_stock_on_way_coverage"] == 0
    assert inventory["store-02"]["platform_available_stock"] == 8
    assert sum(item["platform_available_stock"] or 0 for item in inventory.values()) == 13
    engine.dispose()


def test_store_traffic_series_batches_success_and_failed_reference_by_store() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                DailyReportRun(
                    store_code="current",
                    run_id="current-pre-close",
                    business_date=date(2026, 8, 19),
                    slot="pre_close",
                    captured_at=datetime(2026, 8, 20, 1),
                    status="failed",
                    counts={},
                    created_at=datetime(2026, 8, 20, 1),
                ),
                DailyReportRun(
                    store_code="current",
                    run_id="current-morning",
                    business_date=date(2026, 8, 19),
                    slot="morning",
                    captured_at=datetime(2026, 8, 19, 1),
                    status="success",
                    counts={},
                    created_at=datetime(2026, 8, 19, 1),
                ),
                DailyReportRun(
                    store_code="store-02",
                    run_id="store-02-pre-close",
                    business_date=date(2026, 8, 19),
                    slot="pre_close",
                    captured_at=datetime(2026, 8, 20, 1),
                    status="success",
                    counts={},
                    created_at=datetime(2026, 8, 20, 1),
                ),
                DailyReportObservation(
                    store_code="current",
                    run_id="current-morning",
                    offer_id="current-a",
                    page_views_30_days=40,
                    ordered_units=0,
                ),
                DailyReportObservation(
                    store_code="store-02",
                    run_id="store-02-pre-close",
                    offer_id="store-02-a",
                    page_views_30_days=70,
                    ordered_units=0,
                ),
            ]
        )

    series = load_store_traffic_series(
        engine,
        ("current", "store-02"),
        as_of=date(2026, 8, 19),
        days=1,
    )

    assert series["current"][0]["status"] == "failed"
    assert series["current"][0]["page_views_30_days_total"] is None
    assert series["current"][0]["reference"] == {
        "source_slot": "morning",
        "captured_at": "2026-08-19T01:00:00",
        "page_views_30_days_total": 40,
        "product_count": 1,
        "missing_product_count": 0,
    }
    assert series["store-02"][0]["page_views_30_days_total"] == 70
    engine.dispose()
