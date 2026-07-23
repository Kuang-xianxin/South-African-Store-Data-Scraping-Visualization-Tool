from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from takealot_ops.domain import OfferRecord, SaleRecord
from takealot_ops.metrics.service import (
    METRIC_METADATA,
    PRODUCT_DAILY_COLUMNS,
    MetricService,
    build_quadrant_window,
    classify_quadrants,
)
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import AnomalyEvent, DataQualityEvent
from takealot_ops.storage.repository import Repository


PROJECT_ROOT = Path(__file__).parents[2]
ANOMALY_RULES = PROJECT_ROOT / "config" / "anomaly_rules.yaml"
STATUS_RULES = PROJECT_ROOT / "config" / "sale_status_rules.yaml"


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
        sale_status_rules_path=_write_status_rules(tmp_path, included=included, excluded=excluded),
        now=lambda: now,
    )


def test_project_status_rules_classify_observed_takealot_statuses(tmp_path: Path) -> None:
    as_of = date(2026, 7, 20)
    sales = [
        _sale(
            "shipped",
            datetime(2026, 7, 20, 8, tzinfo=UTC),
            status="Shipped to Customer",
            quantity=2,
        ),
        _sale(
            "preparing",
            datetime(2026, 7, 20, 8, tzinfo=UTC),
            status="Preparing for Customer",
            quantity=3,
        ),
        _sale(
            "transfer",
            datetime(2026, 7, 20, 8, tzinfo=UTC),
            status="Inter DC Transfer",
            quantity=4,
        ),
        _sale(
            "returned",
            datetime(2026, 7, 20, 8, tzinfo=UTC),
            status="Returned",
            quantity=5,
        ),
    ]
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        _seed(session, sales=sales, offers=[(_offer(), as_of)])
        service = MetricService(
            Repository(session),
            anomaly_rules_path=ANOMALY_RULES,
            sale_status_rules_path=STATUS_RULES,
            now=lambda: datetime(2026, 7, 20, 12, tzinfo=UTC),
        )

        service.rebuild(as_of, as_of)
        dataset = service.dashboard_dataset(as_of)

    row = dataset.product_daily.iloc[0]
    assert row["ordered_units"] == 14
    assert row["effective_units"] == 9
    assert "unknown_sale_status" not in dataset.anomalies["anomaly_type"].tolist()
    assert "unknown_sale_status" not in dataset.quality_events["event_type"].tolist()


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
        offers_by_date: dict[date, list[OfferRecord]] = {}
        for offer, snapshot_date in offers or []:
            offers_by_date.setdefault(snapshot_date, []).append(offer)
        for snapshot_date, dated_offers in offers_by_date.items():
            run_id = repository.begin_run("offers", scope_date=snapshot_date)
            repository.prune_offer_snapshot(
                snapshot_date, [offer.offer_id for offer in dated_offers]
            )
            for offer in dated_offers:
                repository.upsert_offer_snapshot(offer, snapshot_date)
            repository.finish_run(run_id, "success", {"records": len(dated_offers)}, None)


def _seed_offer_batch(session: Session, batch_date: date, offers: list[OfferRecord]) -> None:
    with session.begin():
        repository = Repository(session)
        run_id = repository.begin_run("offers", scope_date=batch_date)
        repository.prune_offer_snapshot(batch_date, [offer.offer_id for offer in offers])
        for offer in offers:
            repository.upsert_offer_snapshot(offer, batch_date)
        repository.finish_run(run_id, "success", {"records": len(offers)}, None)


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
        service = _service(session, tmp_path, included=("included",), excluded=("excluded",))

        service.rebuild(metric_date, metric_date)
        row = service.dashboard_dataset(metric_date).product_daily.iloc[0]

    assert row["ordered_units"] == 6
    assert row["effective_units"] == 1


def test_ordered_revenue_uses_api_line_value_without_multiplying_quantity(
    tmp_path: Path,
) -> None:
    metric_date = date(2026, 7, 20)
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        _seed(
            session,
            sales=[
                _sale(
                    "two-units",
                    datetime(2026, 7, 20, 8, tzinfo=UTC),
                    quantity=2,
                    price="704.00",
                )
            ],
        )
        service = _service(session, tmp_path, included=("included",))

        service.rebuild(metric_date, metric_date)
        row = service.dashboard_dataset(metric_date).product_daily.iloc[0]

    assert row["ordered_units"] == 2
    assert row["ordered_revenue"] == Decimal("704.00")


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
        first_event_id = dataset.quality_events.iloc[0]["event_id"]
        service.rebuild(metric_date, metric_date)
        rebuilt = service.dashboard_dataset(metric_date)

    assert dataset.product_daily.iloc[0]["effective_units"] == 0
    assert dataset.quality_events["event_type"].tolist() == ["unknown_sale_status"]
    assert rebuilt.quality_events.iloc[0]["event_id"] == first_event_id
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
    assert METRIC_METADATA["page_views_window_net_change"]["label"] == "30天浏览量窗口净变化"
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


def test_quadrant_window_uses_latest_traffic_and_seven_calendar_day_units() -> None:
    frame = pd.DataFrame(
        {
            "metric_date": [
                date(2026, 7, 15),
                date(2026, 7, 16),
                date(2026, 7, 22),
                date(2026, 7, 22),
            ],
            "offer_id": ["a", "a", "a", "b"],
            "sku": ["sku-a", "sku-a", "sku-a", "sku-b"],
            "page_views_30_days": [70, 80, 100, 50],
            "ordered_units": [9, 2, 3, None],
        }
    )

    result = build_quadrant_window(frame, date(2026, 7, 23), days=7).set_index("offer_id")

    assert result.loc["a", "page_views_30_days"] == 100
    assert result.loc["a", "ordered_units"] == 5
    assert pd.isna(result.loc["b", "ordered_units"])
    assert result.attrs["window_start"] == date(2026, 7, 16)
    assert result.attrs["window_end"] == date(2026, 7, 22)


def test_anomaly_rules_cover_spike_traffic_stock_status_and_staleness(tmp_path: Path) -> None:
    as_of = date(2026, 7, 20)
    yesterday = datetime(2026, 7, 20, 8, tzinfo=UTC)
    sales = [
        _sale("spike-today", yesterday, offer_id="spike", quantity=4),
        _sale(
            "stock-sale",
            datetime(2026, 7, 19, 8, tzinfo=UTC),
            offer_id="stock-sold",
            quantity=1,
        ),
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


def test_complete_offer_batches_control_historical_membership_and_empty_scope(
    tmp_path: Path,
) -> None:
    day_one = date(2026, 7, 20)
    day_two = date(2026, 7, 21)
    day_three = date(2026, 7, 22)
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        _seed_offer_batch(session, day_one, [_offer("a"), _offer("b")])
        _seed_offer_batch(session, day_two, [_offer("a")])
        _seed_offer_batch(session, day_three, [])
        service = _service(session, tmp_path)

        service.rebuild(day_one, day_three)
        on_day_one = service.dashboard_dataset(day_one)
        on_day_two = service.dashboard_dataset(day_two)
        on_day_three = service.dashboard_dataset(day_three)

    assert on_day_one.offer_current["offer_id"].tolist() == ["a", "b"]
    assert on_day_two.offer_current["offer_id"].tolist() == ["a"]
    assert on_day_three.offer_current.empty
    day_two_rows = on_day_three.product_daily.loc[
        on_day_three.product_daily["metric_date"] == day_two, "offer_id"
    ].tolist()
    day_three_rows = on_day_three.product_daily.loc[
        on_day_three.product_daily["metric_date"] == day_three, "offer_id"
    ].tolist()
    assert day_two_rows == ["a"]
    assert day_three_rows == []
    assert (
        "b"
        not in on_day_three.anomalies.loc[
            on_day_three.anomalies["anomaly_type"] == "stale_offer_snapshot", "offer_id"
        ].tolist()
    )


def test_rebuild_preserves_external_anomaly_events(tmp_path: Path) -> None:
    metric_date = date(2026, 7, 20)
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        with session.begin():
            session.add(
                AnomalyEvent(
                    event_date=metric_date,
                    offer_id="external",
                    anomaly_type="external_check",
                    severity="warning",
                    explanation=None,
                    details=None,
                    created_at=datetime(2026, 7, 20, 8, tzinfo=UTC),
                )
            )
        service = _service(session, tmp_path)

        service.rebuild(metric_date, metric_date)
        dataset = service.dashboard_dataset(metric_date)

    assert dataset.anomalies["anomaly_type"].tolist() == ["external_check"]


def test_stockout_uses_the_prior_seven_calendar_days(tmp_path: Path) -> None:
    as_of = date(2026, 7, 20)
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        _seed_offer_batch(
            session,
            as_of,
            [_offer("stock", total_stock=0, status="paused")],
        )
        _seed(
            session,
            sales=[
                _sale(
                    "seven-days-prior",
                    datetime(2026, 7, 13, 8, tzinfo=UTC),
                    offer_id="stock",
                )
            ],
        )
        service = _service(session, tmp_path, included=("included",))

        service.rebuild(as_of, as_of)
        anomalies = service.dashboard_dataset(as_of).anomalies

    assert ("stock", "suspected_stockout") in set(
        anomalies[["offer_id", "anomaly_type"]].itertuples(index=False, name=None)
    )


def test_null_offer_status_is_non_buyable(tmp_path: Path) -> None:
    as_of = date(2026, 7, 20)
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        _seed_offer_batch(session, as_of, [_offer("missing-status", status=None)])
        service = _service(session, tmp_path)

        service.rebuild(as_of, as_of)
        anomalies = service.dashboard_dataset(as_of).anomalies

    assert ("missing-status", "non_buyable") in set(
        anomalies[["offer_id", "anomaly_type"]].itertuples(index=False, name=None)
    )


def test_stale_snapshot_normalizes_sast_offset_before_comparison(tmp_path: Path) -> None:
    as_of = date(2026, 7, 20)
    sast = ZoneInfo("Africa/Johannesburg")
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        _seed_offer_batch(
            session,
            as_of,
            [
                _offer(
                    "offset-stale",
                    captured_at=datetime(2026, 7, 19, 11, 30, tzinfo=sast),
                )
            ],
        )
        service = _service(
            session,
            tmp_path,
            now=datetime(2026, 7, 20, 12, tzinfo=UTC),
        )

        service.rebuild(as_of, as_of)
        anomalies = service.dashboard_dataset(as_of).anomalies

    assert ("offset-stale", "stale_offer_snapshot") in set(
        anomalies[["offer_id", "anomaly_type"]].itertuples(index=False, name=None)
    )


def test_collection_gap_carries_offer_state_without_fabricating_traffic(
    tmp_path: Path,
) -> None:
    day_one = date(2026, 7, 20)
    day_two = date(2026, 7, 21)
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        _seed_offer_batch(
            session,
            day_one,
            [
                _offer(
                    "gap-offer",
                    page_views=900,
                    conversion="4.5",
                    previous_conversion="3.5",
                    total_stock=5,
                    status="buyable",
                )
            ],
        )
        service = _service(
            session,
            tmp_path,
            now=datetime(2026, 7, 21, 12, tzinfo=UTC),
        )

        service.rebuild(day_two, day_two)
        dataset = service.dashboard_dataset(day_two)

    row = dataset.product_daily.loc[dataset.product_daily["metric_date"] == day_two].iloc[0]
    current = dataset.offer_current.iloc[0]
    assert row["offer_status"] == current["status"] == "buyable"
    assert row["total_stock"] == current["total_stock"] == 5
    assert row["sku"] == current["sku"] == "SKU-gap-offer"
    assert row["page_views_30_days"] is None
    assert row["page_views_30_day_average"] is None
    assert row["page_views_window_net_change"] is None
    assert row["conversion_percentage_30_days"] is None
    assert row["conversion_percentage_previous_30_days"] is None
    assert row["conversion_change_points"] is None
    anomaly_types = dataset.anomalies["anomaly_type"].tolist()
    assert "non_buyable" not in anomaly_types
    assert "stale_offer_snapshot" in anomaly_types
