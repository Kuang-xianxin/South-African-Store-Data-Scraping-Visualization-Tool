from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from takealot_ops.domain import OfferRecord, SaleRecord
from takealot_ops.metrics.service import (
    METRIC_METADATA,
    PRODUCT_DAILY_COLUMNS,
    MetricService,
    classify_quadrants,
)
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import DataQualityEvent
from takealot_ops.storage.repository import Repository


PROJECT_ROOT = Path(__file__).parents[2]
ANOMALY_RULES = PROJECT_ROOT / "config" / "anomaly_rules.yaml"


def _sale(
    item_id: str,
    ordered_at: datetime,
    *,
    offer_id: str = "offer-1",
    status: str = "included",
    quantity: int = 1,
    price: str = "10.00",
) -> SaleRecord:
    return SaleRecord(
        order_item_id=item_id,
        order_id=f"order-{item_id}",
        order_date=ordered_at,
        sale_status=status,
        offer_id=offer_id,
        tsin_id=None,
        sku=f"SKU-{offer_id}",
        selling_price=Decimal(price),
        quantity=quantity,
        success_fee=None,
        fulfillment_fee=None,
        courier_collection_fee=None,
        total_fees=None,
        stock_transfer_fee=None,
        sales_region=None,
        stock_source_region=None,
    )


def _offer(
    offer_id: str = "offer-1",
    *,
    captured_at: datetime = datetime(2026, 7, 20, 8, tzinfo=UTC),
    page_views: int | None = None,
    conversion: str | None = None,
    previous_conversion: str | None = None,
    total_stock: int | None = None,
    status: str | None = "buyable",
) -> OfferRecord:
    return OfferRecord(
        offer_id=offer_id,
        tsin_id=None,
        sku=f"SKU-{offer_id}",
        barcode=None,
        title=f"Offer {offer_id}",
        selling_price=Decimal("10.00"),
        rrp=None,
        benchmark_price=None,
        status=status,
        image_url=None,
        productline_id=None,
        conversion_percentage_30_days=(Decimal(conversion) if conversion is not None else None),
        conversion_percentage_previous_30_days=(
            Decimal(previous_conversion) if previous_conversion is not None else None
        ),
        page_views_30_days=page_views,
        quantity_returned_30_days=None,
        total_wishlist=None,
        wishlist_30_days=None,
        listing_quality=None,
        discount_percentage=None,
        updated_at=None,
        captured_at=captured_at,
        total_stock=total_stock,
    )


def _write_status_rules(
    tmp_path: Path, *, included: tuple[str, ...] = (), excluded: tuple[str, ...] = ()
) -> Path:
    rules_path = tmp_path / "sale_status_rules.yaml"
    included_yaml = (
        "included:\n" + "".join(f"  - {value}\n" for value in included)
        if included
        else "included: []\n"
    )
    excluded_yaml = (
        "excluded:\n" + "".join(f"  - {value}\n" for value in excluded)
        if excluded
        else "excluded: []\n"
    )
    rules_path.write_text(
        included_yaml + excluded_yaml + "unknown: []\n",
        encoding="utf-8",
    )
    return rules_path


def _service(
    session: Session,
    tmp_path: Path,
    *,
    included: tuple[str, ...] = (),
    excluded: tuple[str, ...] = (),
    now: datetime = datetime(2026, 7, 20, 12, tzinfo=UTC),
) -> MetricService:
    return MetricService(
        Repository(session),
        anomaly_rules_path=ANOMALY_RULES,
        sale_status_rules_path=_write_status_rules(
            tmp_path, included=included, excluded=excluded
        ),
        now=lambda: now,
    )


def _seed(
    session: Session,
    *,
    sales: list[SaleRecord] | None = None,
    offers: list[tuple[OfferRecord, date]] | None = None,
) -> None:
    with session.begin():
        repository = Repository(session)
        for sale in sales or []:
            repository.upsert_sale(sale, {"order_item_id": sale.order_item_id})
        for offer, snapshot_date in offers or []:
            repository.upsert_offer_snapshot(offer, snapshot_date)


def test_sales_are_grouped_by_sast_day(tmp_path: Path) -> None:
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        _seed(
            session,
            sales=[
                _sale("one", datetime(2026, 7, 19, 22, 30, tzinfo=UTC), quantity=2),
                _sale("two", datetime(2026, 7, 20, 21, 59, tzinfo=UTC), quantity=3),
            ],
        )
        service = _service(session, tmp_path, included=("included",))

        assert service.rebuild(date(2026, 7, 20), date(2026, 7, 20)) == 1
        row = service.dashboard_dataset(date(2026, 7, 20)).product_daily.iloc[0]

    assert row["metric_date"] == date(2026, 7, 20)
    assert row["ordered_units"] == 5


def test_ordered_units_include_all_statuses(tmp_path: Path) -> None:
    metric_date = date(2026, 7, 20)
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        _seed(
            session,
            sales=[
                _sale("included", datetime(2026, 7, 20, 8, tzinfo=UTC), quantity=1),
                _sale(
                    "excluded",
                    datetime(2026, 7, 20, 9, tzinfo=UTC),
                    status="excluded",
                    quantity=2,
                ),
                _sale(
                    "unknown",
                    datetime(2026, 7, 20, 10, tzinfo=UTC),
                    status="not-configured",
                    quantity=3,
                ),
            ],
        )
        service = _service(
            session, tmp_path, included=("included",), excluded=("excluded",)
        )

        service.rebuild(metric_date, metric_date)
        row = service.dashboard_dataset(metric_date).product_daily.iloc[0]

    assert row["ordered_units"] == 6
    assert row["effective_units"] == 1


def test_unknown_status_is_excluded_from_effective_units_and_flagged(tmp_path: Path) -> None:
    metric_date = date(2026, 7, 20)
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        _seed(
            session,
            sales=[
                _sale(
                    "unknown",
                    datetime(2026, 7, 20, 8, tzinfo=UTC),
                    status="new-status",
                    quantity=4,
                )
            ],
        )
        service = _service(session, tmp_path)

        service.rebuild(metric_date, metric_date)
        dataset = service.dashboard_dataset(metric_date)

    assert dataset.product_daily.iloc[0]["effective_units"] == 0
    assert dataset.quality_events["event_type"].tolist() == ["unknown_sale_status"]
    assert "unknown_sale_status" in dataset.anomalies["anomaly_type"].tolist()


def test_traffic_daily_average_is_page_views_divided_by_30(tmp_path: Path) -> None:
    metric_date = date(2026, 7, 20)
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        _seed(session, offers=[(_offer(page_views=1500), metric_date)])
        service = _service(session, tmp_path)

        service.rebuild(metric_date, metric_date)
        row = service.dashboard_dataset(metric_date).product_daily.iloc[0]

    assert row["page_views_30_day_average"] == pytest.approx(50.0)


def test_window_net_change_is_not_named_daily_traffic() -> None:
    assert METRIC_METADATA["page_views_30_days"]["label"] == "近30天浏览量"
    assert METRIC_METADATA["page_views_30_day_average"]["label"] == "近30天日均浏览量"
    assert (
        METRIC_METADATA["page_views_window_net_change"]["label"]
        == "30天浏览量窗口净变化"
    )
    exported_text = " ".join(
        value for metadata in METRIC_METADATA.values() for value in metadata.values()
    )
    assert "精确每日流量" not in exported_text
    assert "访客" not in exported_text


def test_sales_drop_rule_requires_baseline_of_two_units(tmp_path: Path) -> None:
    as_of = date(2026, 7, 20)
    sales: list[SaleRecord] = []
    for offset in range(1, 8):
        day = as_of - timedelta(days=offset)
        ordered_at = datetime(day.year, day.month, day.day, 8, tzinfo=UTC)
        sales.extend(
            [
                _sale(f"below-{offset}", ordered_at, offer_id="below", quantity=1),
                _sale(f"eligible-{offset}", ordered_at, offer_id="eligible", quantity=2),
            ]
        )
    sales.append(
        _sale(
            "eligible-today",
            datetime(2026, 7, 20, 8, tzinfo=UTC),
            offer_id="eligible",
            quantity=1,
        )
    )
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        _seed(
            session,
            sales=sales,
            offers=[(_offer("below"), as_of), (_offer("eligible"), as_of)],
        )
        service = _service(session, tmp_path, included=("included",))

        service.rebuild(as_of, as_of)
        anomalies = service.dashboard_dataset(as_of).anomalies

    drops = anomalies.loc[anomalies["anomaly_type"] == "sales_drop", "offer_id"].tolist()
    assert drops == ["eligible"]


def test_four_quadrants_use_configured_quantiles() -> None:
    frame = pd.DataFrame(
        {
            "offer_id": ["zero", "one", "two", "three", "four"],
            "page_views_30_days": [0, 10, 20, 30, 40],
            "ordered_units": [0, 10, 20, 30, 40],
        }
    )

    at_25 = classify_quadrants(frame, percentile=25).set_index("offer_id")
    at_50 = classify_quadrants(frame).set_index("offer_id")
    at_75 = classify_quadrants(frame, percentile=75).set_index("offer_id")

    assert at_25.loc["one", "quadrant"] == "star"
    assert at_50.loc["two", "quadrant"] == "star"
    assert at_75.loc["three", "quadrant"] == "star"
    assert at_50.attrs["page_views_boundary"] == 20.0
    assert at_50.attrs["ordered_units_boundary"] == 20.0


def test_four_quadrants_handle_empty_and_constant_data_deterministically() -> None:
    empty = pd.DataFrame(columns=["offer_id", "page_views_30_days", "ordered_units"])
    constant = pd.DataFrame(
        {
            "offer_id": ["a", "b"],
            "page_views_30_days": [5, 5],
            "ordered_units": [2, 2],
        }
    )

    empty_result = classify_quadrants(empty)
    constant_result = classify_quadrants(constant)

    assert empty_result.empty
    assert empty_result.attrs == {
        "page_views_boundary": None,
        "ordered_units_boundary": None,
        "percentile": 50,
    }
    assert constant_result["quadrant"].tolist() == ["star", "star"]


def test_anomaly_rules_cover_spike_traffic_stock_status_and_staleness(tmp_path: Path) -> None:
    as_of = date(2026, 7, 20)
    yesterday = datetime(2026, 7, 20, 8, tzinfo=UTC)
    sales = [
        _sale("spike-today", yesterday, offer_id="spike", quantity=4),
        _sale("stock-sale", yesterday, offer_id="stock-sold", quantity=1),
    ]
    for offset in range(1, 8):
        day = as_of - timedelta(days=offset)
        sales.append(
            _sale(
                f"spike-{offset}",
                datetime(day.year, day.month, day.day, 8, tzinfo=UTC),
                offer_id="spike",
                quantity=2,
            )
        )
    offers = [
        (_offer("spike", page_views=20, conversion="5"), as_of),
        (_offer("high-low", page_views=100, conversion="1"), as_of),
        (_offer("low-high", page_views=1, conversion="10"), as_of),
        (_offer("stock-buyable", total_stock=0), as_of),
        (_offer("stock-sold", total_stock=0, status="paused"), as_of),
        (_offer("non-buyable", status="paused"), as_of),
        (
            _offer(
                "stale",
                captured_at=datetime(2026, 7, 19, 8, tzinfo=UTC),
                status="buyable",
            ),
            as_of,
        ),
    ]
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        _seed(session, sales=sales, offers=offers)
        service = _service(
            session,
            tmp_path,
            included=("included",),
            now=datetime(2026, 7, 20, 12, tzinfo=UTC),
        )

        service.rebuild(as_of, as_of)
        anomalies = service.dashboard_dataset(as_of).anomalies

    pairs = set(anomalies[["offer_id", "anomaly_type"]].itertuples(index=False, name=None))
    assert ("spike", "sales_spike") in pairs
    assert ("high-low", "high_views_low_conversion") in pairs
    assert ("low-high", "low_views_high_conversion") in pairs
    assert ("stock-buyable", "suspected_stockout") in pairs
    assert ("stock-sold", "suspected_stockout") in pairs
    assert ("non-buyable", "non_buyable") in pairs
    assert ("stale", "stale_offer_snapshot") in pairs


def test_dashboard_dataset_has_stable_product_schema_when_empty(tmp_path: Path) -> None:
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        dataset = _service(session, tmp_path).dashboard_dataset(date(2026, 7, 20))

    assert dataset.product_daily.columns.tolist() == list(PRODUCT_DAILY_COLUMNS)
    assert dataset.store_daily.empty
    assert dataset.offer_current.empty
    assert dataset.anomalies.empty
    assert dataset.quality_events.empty


def test_rebuild_and_dashboard_do_not_expose_offers_first_seen_after_as_of(
    tmp_path: Path,
) -> None:
    as_of = date(2026, 7, 20)
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        _seed(
            session,
            offers=[
                (
                    _offer(
                        "future",
                        captured_at=datetime(2026, 7, 21, 8, tzinfo=UTC),
                    ),
                    date(2026, 7, 21),
                )
            ],
        )
        service = _service(session, tmp_path)

        rebuilt = service.rebuild(as_of, as_of)
        dataset = service.dashboard_dataset(as_of)

    assert rebuilt == 0
    assert dataset.product_daily.empty
    assert dataset.offer_current.empty


def test_rebuild_preserves_unrelated_quality_events(tmp_path: Path) -> None:
    metric_date = date(2026, 7, 20)
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        with session.begin():
            session.add(
                DataQualityEvent(
                    event_id="unrelated",
                    event_date=metric_date,
                    event_type="missing_sku",
                    severity="warning",
                    offer_id=None,
                    details=None,
                    created_at=datetime(2026, 7, 20, 8, tzinfo=UTC),
                )
            )
        service = _service(session, tmp_path)

        service.rebuild(metric_date, metric_date)
        dataset = service.dashboard_dataset(metric_date)

    assert dataset.quality_events["event_type"].tolist() == ["missing_sku"]
