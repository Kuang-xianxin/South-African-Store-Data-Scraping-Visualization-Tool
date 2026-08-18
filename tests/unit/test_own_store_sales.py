from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from takealot_ops.competitors.own_store_sales import build_own_store_sales_series
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import (
    DailySalesMetricState,
    ErpStore,
    OfferCurrent,
    OfferSnapshot,
    SaleItem,
    SalesRevenueRevision,
)
from takealot_ops.storage.store_context import store_scope


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    now = datetime(2026, 8, 6, 1)
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                ErpStore(
                    code="store-a",
                    display_name="Store A",
                    active=True,
                    data_connected=True,
                    created_at=now,
                    updated_at=now,
                ),
                ErpStore(
                    code="store-b",
                    display_name="Store B",
                    active=True,
                    data_connected=True,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
    return engine


def _state(
    metric_date: date,
    *,
    source_kind: str = "takealot_sales_api",
    verified: bool = True,
) -> DailySalesMetricState:
    recorded_at = datetime(2026, 8, 6, 2, tzinfo=UTC)
    return DailySalesMetricState(
        metric_date=metric_date,
        ordered_units=0,
        ordered_revenue=0,
        source_kind=source_kind,
        source_run_id="sales-run" if verified else None,
        source_details=(
            {"kind": source_kind, "collected_at": recorded_at.isoformat()}
            if verified
            else {"kind": source_kind}
        ),
        verified_at=recorded_at if verified else None,
        first_published_at=recorded_at,
        updated_at=recorded_at,
        revision_count=0,
    )


def test_series_aggregates_own_offers_and_keeps_missing_days_distinct_from_zero() -> None:
    engine = _engine()
    with store_scope("store-a"), Session(engine) as session, session.begin():
        session.add_all(
            [
                OfferCurrent(
                    offer_id="offer-a1",
                    productline_id="123",
                    sku="SKU-A1",
                    created_at=datetime(2026, 8, 1, 23, tzinfo=UTC),
                    captured_at=datetime(2026, 8, 5, 1, tzinfo=UTC),
                ),
                OfferCurrent(
                    offer_id="offer-a2",
                    productline_id="123",
                    sku="SKU-A2",
                    created_at=datetime(2026, 8, 2, 1, tzinfo=UTC),
                    captured_at=datetime(2026, 8, 5, 1, tzinfo=UTC),
                ),
                SaleItem(
                    order_item_id="order-item-a1",
                    order_date=datetime(2026, 8, 2, 1, tzinfo=UTC),
                    sales_day=date(2026, 8, 2),
                    offer_id="offer-a1",
                    quantity=2,
                    raw_payload={},
                ),
                SaleItem(
                    order_item_id="order-item-a2",
                    order_date=datetime(2026, 8, 1, 18, tzinfo=UTC),
                    sales_day=date(2026, 8, 1),
                    offer_id="offer-a2",
                    quantity=1,
                    raw_payload={},
                ),
                SaleItem(
                    order_item_id="order-item-a3",
                    order_date=datetime(2026, 8, 4, 1, tzinfo=UTC),
                    sales_day=date(2026, 8, 4),
                    offer_id="offer-a1",
                    quantity=9,
                    raw_payload={},
                ),
                _state(date(2026, 8, 1)),
                _state(date(2026, 8, 2)),
                _state(date(2026, 8, 3)),
                _state(
                    date(2026, 8, 4),
                    source_kind="local_metric_rebuild",
                    verified=False,
                ),
            ]
        )
    with store_scope("store-b"), Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="offer-b",
                productline_id="other-plid",
                sku="SKU-B",
                captured_at=datetime(2026, 8, 5, 1, tzinfo=UTC),
            )
        )

    with Session(engine) as session:
        payload = build_own_store_sales_series(
            session,
            plid="123",
            store_codes={"store-a", "store-b"},
            through=date(2026, 8, 5),
        )

    assert len(payload) == 1
    series = payload[0]
    assert series["store_code"] == "store-a"
    assert series["store_name"] == "Store A"
    assert series["offer_ids"] == ["offer-a1", "offer-a2"]
    assert series["listing_date"] == "2026-08-02"
    assert series["listing_date_source"] == "platform"
    assert series["total_ordered_units"] == 3
    assert series["covered_days"] == 2
    assert series["missing_days"] == 2
    assert [point["ordered_units"] for point in series["points"]] == [3, 0, None, None]
    assert [point["data_status"] for point in series["points"]] == [
        "verified",
        "verified",
        "missing",
        "missing",
    ]
    engine.dispose()


def test_series_falls_back_to_earliest_local_record_and_accepts_source_proof() -> None:
    engine = _engine()
    with store_scope("store-a"), Session(engine) as session, session.begin():
        session.add_all(
            [
                OfferCurrent(
                    offer_id="offer-a",
                    productline_id="456",
                    sku="SKU-A",
                    created_at=None,
                    captured_at=datetime(2026, 8, 5, 1, tzinfo=UTC),
                ),
                OfferSnapshot(
                    snapshot_date=date(2026, 8, 3),
                    offer_id="offer-a",
                    productline_id="456",
                    sku="SKU-A",
                    created_at=None,
                    captured_at=datetime(2026, 8, 2, 17, tzinfo=UTC),
                ),
                SaleItem(
                    order_item_id="order-item-a",
                    order_date=datetime(2026, 8, 2, 18, tzinfo=UTC),
                    sales_day=date(2026, 8, 2),
                    offer_id="offer-a",
                    quantity=5,
                    raw_payload={},
                ),
                _state(date(2026, 8, 2)),
                _state(date(2026, 8, 3), verified=False),
            ]
        )
        state = session.scalar(
            select(DailySalesMetricState).where(
                DailySalesMetricState.metric_date == date(2026, 8, 3)
            )
        )
        assert state is not None
        state.source_kind = "takealot_sales_api"
        state.source_details = {
            "kind": "takealot_sales_api",
            "collected_at": "2026-08-04T00:00:00+00:00",
        }

    with Session(engine) as session:
        payload = build_own_store_sales_series(
            session,
            plid="456",
            store_codes={"store-a"},
            through=date(2026, 8, 4),
        )

    assert payload[0]["listing_date"] == "2026-08-03"
    assert payload[0]["listing_date_source"] == "first_observed"
    assert payload[0]["coverage_start"] == "2026-08-03"
    assert payload[0]["points"] == [
        {
            "date": "2026-08-03",
            "ordered_units": 5,
            "data_status": "verified",
            "revision_count": 0,
        },
        {
            "date": "2026-08-04",
            "ordered_units": None,
            "data_status": "missing",
            "revision_count": 0,
        },
    ]
    engine.dispose()


def test_unfinished_china_day_is_partial_even_after_a_successful_sales_pull() -> None:
    engine = _engine()
    with store_scope("store-a"), Session(engine) as session, session.begin():
        session.add_all(
            [
                OfferCurrent(
                    offer_id="offer-current-day",
                    productline_id="789",
                    sku="SKU-CURRENT",
                    created_at=datetime(2026, 8, 5, 17, tzinfo=UTC),
                    captured_at=datetime(2026, 8, 6, 2, tzinfo=UTC),
                ),
                SaleItem(
                    order_item_id="order-item-current-day",
                    order_date=datetime(2026, 8, 5, 18, tzinfo=UTC),
                    sales_day=date(2026, 8, 5),
                    offer_id="offer-current-day",
                    quantity=4,
                    raw_payload={},
                ),
                _state(date(2026, 8, 5)),
                _state(date(2026, 8, 6)),
            ]
        )

    with Session(engine) as session:
        payload = build_own_store_sales_series(
            session,
            plid="789",
            store_codes={"store-a"},
            through=date(2026, 8, 6),
        )

    assert payload[0]["covered_days"] == 0
    assert payload[0]["partial_days"] == 1
    assert payload[0]["missing_days"] == 0
    assert payload[0]["total_ordered_units"] == 4
    assert payload[0]["points"] == [
        {
            "date": "2026-08-06",
            "ordered_units": 4,
            "data_status": "partial",
            "revision_count": 0,
        }
    ]
    engine.dispose()


def test_series_counts_only_post_close_sales_revisions() -> None:
    engine = _engine()
    with store_scope("store-a"), Session(engine) as session, session.begin():
        session.add_all(
            [
                OfferCurrent(
                    offer_id="offer-revised",
                    productline_id="revision-plid",
                    sku="SKU-REVISION",
                    created_at=datetime(2026, 8, 2, 18, tzinfo=UTC),
                    captured_at=datetime(2026, 8, 5, 1, tzinfo=UTC),
                ),
                SaleItem(
                    order_item_id="order-item-revised",
                    order_date=datetime(2026, 8, 2, 18, tzinfo=UTC),
                    sales_day=date(2026, 8, 2),
                    offer_id="offer-revised",
                    quantity=1,
                    raw_payload={},
                ),
                _state(date(2026, 8, 2)),
                _state(date(2026, 8, 3)),
                SalesRevenueRevision(
                    metric_date=date(2026, 8, 2),
                    change_type="corrected",
                    before_ordered_units=0,
                    after_ordered_units=1,
                    before_ordered_revenue=0,
                    after_ordered_revenue=100,
                    revenue_delta=100,
                    units_delta=1,
                    before_source={
                        "kind": "takealot_sales_api",
                        "collected_at": "2026-08-02T10:00:00+00:00",
                    },
                    after_source={
                        "kind": "takealot_sales_api",
                        "collected_at": "2026-08-03T02:00:00+00:00",
                    },
                    source_run_id="provisional-finalization",
                    detected_at=datetime(2026, 8, 3, 2, tzinfo=UTC),
                ),
                SalesRevenueRevision(
                    metric_date=date(2026, 8, 3),
                    change_type="corrected",
                    before_ordered_units=1,
                    after_ordered_units=2,
                    before_ordered_revenue=100,
                    after_ordered_revenue=200,
                    revenue_delta=100,
                    units_delta=1,
                    before_source={
                        "kind": "takealot_sales_api",
                        "collected_at": "2026-08-04T02:00:00+00:00",
                    },
                    after_source={
                        "kind": "takealot_sales_api",
                        "collected_at": "2026-08-05T02:00:00+00:00",
                    },
                    source_run_id="true-history-revision",
                    detected_at=datetime(2026, 8, 5, 2, tzinfo=UTC),
                ),
            ]
        )

    with Session(engine) as session:
        payload = build_own_store_sales_series(
            session,
            plid="revision-plid",
            store_codes={"store-a"},
            through=date(2026, 8, 3),
        )

    assert payload[0]["points"] == [
        {
            "date": "2026-08-03",
            "ordered_units": 1,
            "data_status": "verified",
            "revision_count": 1,
        }
    ]
    engine.dispose()
