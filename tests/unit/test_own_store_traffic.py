from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from takealot_ops.competitors.own_store_traffic import (
    build_own_store_traffic_series,
)
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import ErpStore, OfferCurrent, OfferSnapshot
from takealot_ops.storage.store_context import store_scope


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    now = datetime(2026, 8, 1, tzinfo=UTC)
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


def _snapshot(
    *,
    snapshot_date: date,
    captured_at: datetime,
    title: str,
    page_views: int | None,
) -> OfferSnapshot:
    return OfferSnapshot(
        snapshot_date=snapshot_date,
        offer_id="offer-a",
        productline_id="123",
        sku="SKU-A",
        title=title,
        page_views_30_days=page_views,
        captured_at=captured_at,
    )


def test_traffic_series_uses_beijing_days_keeps_gaps_and_marks_title_changes() -> None:
    engine = _engine()
    with store_scope("store-a"), Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="offer-a",
                productline_id="123",
                sku="SKU-A",
                title="New Product Name",
                page_views_30_days=140,
                captured_at=datetime(2026, 8, 3, 18, tzinfo=UTC),
            )
        )
        session.add_all(
            [
                _snapshot(
                    snapshot_date=date(2026, 7, 31),
                    captured_at=datetime(2026, 7, 31, 18, tzinfo=UTC),
                    title="Old Product Name",
                    page_views=90,
                ),
                _snapshot(
                    snapshot_date=date(2026, 8, 1),
                    captured_at=datetime(2026, 8, 1, 18, tzinfo=UTC),
                    title="Old Product Name",
                    page_views=100,
                ),
                _snapshot(
                    snapshot_date=date(2026, 8, 3),
                    captured_at=datetime(2026, 8, 3, 18, tzinfo=UTC),
                    title="New Product Name",
                    page_views=140,
                ),
            ]
        )
    with store_scope("store-b"), Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="offer-b",
                productline_id="123",
                sku="SKU-B",
                title="Other Store Product",
                captured_at=datetime(2026, 8, 3, 18, tzinfo=UTC),
            )
        )

    with Session(engine) as session:
        payload = build_own_store_traffic_series(
            session,
            plid="123",
            store_codes={"store-a"},
            start_date=date(2026, 8, 2),
            end_date=date(2026, 8, 4),
        )

    assert len(payload) == 1
    series = payload[0]
    assert series["store_code"] == "store-a"
    assert series["store_name"] == "Store A"
    assert series["offer_id"] == "offer-a"
    assert series["range_start"] == "2026-08-02"
    assert series["range_end"] == "2026-08-04"
    assert series["observed_count"] == 2
    assert series["traffic_count"] == 2
    assert series["missing_count"] == 1
    assert [point["page_views_30_days"] for point in series["points"]] == [
        100,
        None,
        140,
    ]
    assert [point["data_status"] for point in series["points"]] == [
        "observed",
        "missing",
        "observed",
    ]
    assert series["points"][2]["title_changed"] is True
    assert series["points"][2]["previous_title"] == "Old Product Name"
    assert series["points"][2]["title"] == "New Product Name"
    engine.dispose()


def test_traffic_series_rejects_an_inverted_range() -> None:
    engine = _engine()
    with Session(engine) as session:
        try:
            build_own_store_traffic_series(
                session,
                plid="123",
                store_codes={"store-a"},
                start_date=date(2026, 8, 4),
                end_date=date(2026, 8, 2),
            )
        except ValueError as exc:
            assert str(exc) == "开始日期不能晚于结束日期"
        else:
            raise AssertionError("expected an inverted range to be rejected")
    engine.dispose()
