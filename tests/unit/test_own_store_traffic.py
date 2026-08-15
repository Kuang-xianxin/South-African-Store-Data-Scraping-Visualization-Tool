from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from takealot_ops.competitors.own_store_traffic import (
    build_own_store_traffic_series,
)
from takealot_ops.storage.migrations import (
    backfill_store_offer_observation_traffic,
    create_schema,
)
from takealot_ops.storage.models import (
    CollectionRun,
    DailyReportObservation,
    DailyReportRun,
    ErpStore,
    OfferCurrent,
    OfferSnapshot,
    StoreOfferObservation,
)
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


def _observation(
    *,
    display_date: date,
    captured_at: datetime,
    title: str,
    page_views: int | None,
    recorded: bool = True,
) -> StoreOfferObservation:
    return StoreOfferObservation(
        display_date=display_date,
        offer_id="offer-a",
        productline_id="123",
        sku="SKU-A",
        title=title,
        page_views_30_days=page_views,
        page_views_30_days_recorded=recorded,
        captured_at=captured_at,
    )


def test_traffic_series_keeps_every_refresh_and_marks_unrecoverable_history() -> None:
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
                _observation(
                    display_date=date(2026, 7, 31),
                    captured_at=datetime(2026, 7, 31, 18, tzinfo=UTC),
                    title="Old Product Name",
                    page_views=90,
                ),
                _observation(
                    display_date=date(2026, 8, 2),
                    captured_at=datetime(2026, 8, 2, 1, tzinfo=UTC),
                    title="Old Product Name",
                    page_views=100,
                ),
                _observation(
                    display_date=date(2026, 8, 2),
                    captured_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
                    title="Old Product Name",
                    page_views=110,
                ),
                _observation(
                    display_date=date(2026, 8, 3),
                    captured_at=datetime(2026, 8, 3, 2, tzinfo=UTC),
                    title="New Product Name",
                    page_views=None,
                    recorded=False,
                ),
                _observation(
                    display_date=date(2026, 8, 4),
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
    assert series["observed_count"] == 3
    assert series["traffic_count"] == 3
    assert series["missing_count"] == 1
    assert [point["page_views_30_days"] for point in series["points"]] == [
        100,
        110,
        None,
        140,
    ]
    assert [point["data_status"] for point in series["points"]] == [
        "observed",
        "observed",
        "missing",
        "observed",
    ]
    assert series["points"][2]["title_changed"] is True
    assert series["points"][2]["previous_title"] == "Old Product Name"
    assert series["points"][2]["title"] == "New Product Name"
    assert [point["captured_at"] for point in series["points"][:2]] == [
        "2026-08-02T01:00:00+00:00",
        "2026-08-02T10:00:00+00:00",
    ]
    engine.dispose()


def test_backfill_uses_daily_report_offer_run_and_leaves_unmatched_point_missing() -> None:
    engine = _engine()
    captured_at = datetime(2026, 8, 5, 2, 5, 4, tzinfo=UTC)
    unmatched_at = datetime(2026, 8, 5, 8, 15, 2, tzinfo=UTC)
    snapshot_at = datetime(2026, 8, 5, 10, 0, 4, tzinfo=UTC)
    with store_scope("store-a"), Session(engine) as session, session.begin():
        session.add_all(
            [
                _observation(
                    display_date=date(2026, 8, 5),
                    captured_at=captured_at,
                    title="Product",
                    page_views=None,
                    recorded=False,
                ),
                _observation(
                    display_date=date(2026, 8, 5),
                    captured_at=unmatched_at,
                    title="Product",
                    page_views=None,
                    recorded=False,
                ),
                _observation(
                    display_date=date(2026, 8, 5),
                    captured_at=snapshot_at,
                    title="Product",
                    page_views=None,
                    recorded=False,
                ),
                OfferSnapshot(
                    snapshot_date=date(2026, 8, 5),
                    offer_id="offer-a",
                    productline_id="123",
                    sku="SKU-A",
                    title="Product",
                    page_views_30_days=130,
                    captured_at=snapshot_at,
                ),
                CollectionRun(
                    run_id="offer-run",
                    run_type="offers",
                    scope_date=date(2026, 8, 5),
                    started_at=datetime(2026, 8, 5, 2, 5, 5, tzinfo=UTC),
                    finished_at=datetime(2026, 8, 5, 2, 5, 10, tzinfo=UTC),
                    status="success",
                    counts={"records": 1},
                ),
                DailyReportRun(
                    run_id="report-run",
                    business_date=date(2026, 8, 5),
                    slot="morning",
                    captured_at=datetime(2026, 8, 5, 2, 6, tzinfo=UTC),
                    status="success",
                    counts={
                        "attempts": [
                            {
                                "status": "success",
                                "offer_run_id": "offer-run",
                            }
                        ]
                    },
                    created_at=datetime(2026, 8, 5, 2, 6, tzinfo=UTC),
                ),
                DailyReportObservation(
                    run_id="report-run",
                    offer_id="offer-a",
                    sku="SKU-A",
                    title="Product",
                    page_views_30_days=123,
                    ordered_units=0,
                ),
            ]
        )

    stats = backfill_store_offer_observation_traffic(engine)

    with store_scope("store-a"), Session(engine) as session:
        rows = list(
            session.scalars(
                select(StoreOfferObservation).order_by(
                    StoreOfferObservation.captured_at
                )
            )
        )
    assert stats == {
        "daily_report_rows": 1,
        "offer_snapshot_rows": 1,
        "unmatched_rows": 1,
    }
    assert rows[0].page_views_30_days == 123
    assert rows[0].page_views_30_days_recorded is True
    assert rows[1].page_views_30_days is None
    assert rows[1].page_views_30_days_recorded is False
    assert rows[2].page_views_30_days == 130
    assert rows[2].page_views_30_days_recorded is True
    engine.dispose()


def test_create_schema_adds_traffic_columns_to_legacy_observations(
    tmp_path: Path,
) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'legacy-observations.db').as_posix()}"
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE store_offer_observations (
                id INTEGER PRIMARY KEY,
                store_code VARCHAR(64) NOT NULL DEFAULT 'current',
                display_date DATE NOT NULL,
                offer_id VARCHAR(100) NOT NULL,
                productline_id VARCHAR(100),
                sku VARCHAR(255),
                title TEXT,
                image_url TEXT,
                selling_price NUMERIC(14, 2),
                status VARCHAR(100),
                total_stock INTEGER,
                takealot_available_stock INTEGER,
                seller_available_stock INTEGER,
                captured_at DATETIME NOT NULL
            )
            """
        )

    create_schema(engine)

    columns = {
        str(column["name"]): column
        for column in inspect(engine).get_columns("store_offer_observations")
    }
    assert "page_views_30_days" in columns
    assert "page_views_30_days_recorded" in columns
    assert columns["page_views_30_days_recorded"]["nullable"] is False
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
