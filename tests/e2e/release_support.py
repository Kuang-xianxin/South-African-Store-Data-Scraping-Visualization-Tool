from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import Session

from takealot_ops.domain import OfferRecord, SaleRecord
from takealot_ops.exports.excel import export_excel
from takealot_ops.exports.html import export_html
from takealot_ops.metrics.service import MetricService
from takealot_ops.settings import Settings
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import (
    AnomalyEvent,
    DailyProductMetric,
    DataQualityEvent,
    OfferCurrent,
    OfferSnapshot,
    SaleItem,
)
from takealot_ops.storage.repository import Repository


FIXTURE_API_KEY = "release-fixture-super-secret"
BUSINESS_MODELS = (
    OfferCurrent,
    OfferSnapshot,
    SaleItem,
    DailyProductMetric,
    AnomalyEvent,
    DataQualityEvent,
)


@dataclass(frozen=True)
class ReleaseFixture:
    root: Path
    settings: Settings
    database_path: Path
    start_date: date
    report_date: date
    html_path: Path
    excel_path: Path
    source_traffic: dict[tuple[date, str], int | None]


def build_release_fixture(root: Path) -> ReleaseFixture:
    root.mkdir(parents=True, exist_ok=True)
    database_path = root / "data" / "takealot.db"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        project_root=root,
        api_key=FIXTURE_API_KEY,
        base_url="https://example.invalid/v1",
        database_url=f"sqlite:///{database_path.as_posix()}",
        request_timeout_seconds=1.0,
        dashboard_host="127.0.0.1",
        dashboard_port=8501,
    )
    _write_rules(root)
    engine = create_engine(settings.database_url)
    create_schema(engine)
    start_date = date(2026, 6, 21)
    report_date = date(2026, 7, 21)
    source_traffic = _seed_release_data(engine, start_date, report_date)
    dataset = _rebuild(engine, root, start_date, report_date)
    report_dir = root / "exports" / report_date.isoformat()
    html_path = export_html(dataset, report_dir / "release-report.html")
    excel_path = export_excel(dataset, report_dir / "release-report.xlsx")
    engine.dispose()
    return ReleaseFixture(
        root=root,
        settings=settings,
        database_path=database_path,
        start_date=start_date,
        report_date=report_date,
        html_path=html_path,
        excel_path=excel_path,
        source_traffic=source_traffic,
    )


def reseed_and_rebuild(fixture: ReleaseFixture) -> None:
    engine = create_engine(fixture.settings.database_url)
    _seed_release_data(engine, fixture.start_date, fixture.report_date)
    _rebuild(engine, fixture.root, fixture.start_date, fixture.report_date)
    engine.dispose()


def business_state(database_url: str) -> tuple[dict[str, int], str]:
    engine = create_engine(database_url)
    payload: dict[str, list[dict[str, object]]] = {}
    counts: dict[str, int] = {}
    with Session(engine) as session:
        for model in BUSINESS_MODELS:
            rows = session.scalars(select(model)).all()
            name = model.__tablename__
            counts[name] = len(rows)
            columns = [column.key for column in inspect(model).columns if column.key != "id"]
            values = [
                {column: _plain(getattr(row, column)) for column in columns}
                for row in rows
            ]
            payload[name] = sorted(values, key=lambda value: json.dumps(value, sort_keys=True))
    engine.dispose()
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return counts, hashlib.sha256(encoded).hexdigest()


def _seed_release_data(
    engine: Engine, start_date: date, report_date: date
) -> dict[tuple[date, str], int | None]:
    source_traffic: dict[tuple[date, str], int | None] = {}
    with Session(engine) as session:
        repository = Repository(session)
        for offset in range(31):
            snapshot_date = start_date + timedelta(days=offset)
            offers = [
                _offer(index, snapshot_date, offset, source_traffic)
                for index in range(1, 11)
            ]
            with repository.transaction():
                run_id = repository.begin_run("offers", scope_date=snapshot_date)
                repository.prune_offer_snapshot(
                    snapshot_date, [offer.offer_id for offer in offers]
                )
                for offer in offers:
                    repository.upsert_offer_snapshot(offer, snapshot_date)
                repository.finish_run(run_id, "success", {"records": len(offers)}, None)

        sales = [
            _sale(
                f"base-{index}",
                index,
                datetime.combine(report_date, time(8), tzinfo=UTC),
                quantity=index,
                status="included",
            )
            for index in range(1, 11)
        ]
        sales.extend(
            [
                _sale(
                    "boundary-before",
                    1,
                    datetime(2026, 7, 20, 21, 59, tzinfo=UTC),
                    quantity=2,
                    status="included",
                ),
                _sale(
                    "boundary-after",
                    2,
                    datetime(2026, 7, 20, 22, 0, tzinfo=UTC),
                    quantity=3,
                    status="included",
                ),
                _sale(
                    "repeated-item",
                    4,
                    datetime.combine(report_date, time(9), tzinfo=UTC),
                    quantity=4,
                    status="included",
                ),
                _sale(
                    "unknown-item",
                    3,
                    datetime.combine(report_date, time(10), tzinfo=UTC),
                    quantity=1,
                    status="new-marketplace-status",
                ),
            ]
        )
        with repository.transaction():
            for sale in sales:
                repository.upsert_sale(sale, _sale_payload(sale))
            updated = _sale(
                "repeated-item",
                4,
                datetime.combine(report_date, time(9), tzinfo=UTC),
                quantity=4,
                status="excluded",
            )
            repository.upsert_sale(updated, _sale_payload(updated))
    return source_traffic


def _rebuild(engine: Engine, root: Path, start_date: date, report_date: date):
    with Session(engine) as session:
        service = MetricService(
            Repository(session),
            anomaly_rules_path=root / "config" / "anomaly_rules.yaml",
            sale_status_rules_path=root / "config" / "sale_status_rules.yaml",
            now=lambda: datetime(2026, 7, 21, 12, tzinfo=UTC),
        )
        service.rebuild(start_date, report_date)
        return service.dashboard_dataset(report_date)


def _offer(
    index: int,
    snapshot_date: date,
    offset: int,
    source_traffic: dict[tuple[date, str], int | None],
) -> OfferRecord:
    offer_id = f"offer-{index:02d}"
    page_views = None if index == 9 else index * 100 + offset
    source_traffic[(snapshot_date, offer_id)] = page_views
    return OfferRecord(
        offer_id=offer_id,
        tsin_id=f"tsin-{index:02d}",
        sku=f"SKU-{index:02d}",
        barcode=f"600000000{index:03d}",
        title=f"Synthetic product {index:02d}",
        selling_price=Decimal(index * 10),
        rrp=Decimal(index * 12),
        benchmark_price=Decimal(index * 11),
        status="buyable",
        image_url=None,
        productline_id=f"line-{index:02d}",
        conversion_percentage_30_days=Decimal("3.5"),
        conversion_percentage_previous_30_days=Decimal("3.0"),
        page_views_30_days=page_views,
        quantity_returned_30_days=0,
        total_wishlist=index,
        wishlist_30_days=index,
        listing_quality="good",
        discount_percentage=Decimal("10"),
        updated_at=datetime.combine(snapshot_date, time(7), tzinfo=UTC),
        captured_at=datetime.combine(snapshot_date, time(8), tzinfo=UTC),
        total_stock=0 if index == 10 and offset == 30 else 20 - index,
    )


def _sale(
    item_id: str,
    index: int,
    ordered_at: datetime,
    *,
    quantity: int,
    status: str,
) -> SaleRecord:
    return SaleRecord(
        order_item_id=item_id,
        order_id=f"order-{item_id}",
        order_date=ordered_at,
        sale_status=status,
        offer_id=f"offer-{index:02d}",
        tsin_id=f"tsin-{index:02d}",
        sku=f"SKU-{index:02d}",
        selling_price=Decimal(index * 10),
        quantity=quantity,
        success_fee=Decimal("1"),
        fulfillment_fee=Decimal("1"),
        courier_collection_fee=Decimal("1"),
        total_fees=Decimal("3"),
        stock_transfer_fee=Decimal("0"),
        sales_region="CPT",
        stock_source_region="JHB",
    )


def _sale_payload(sale: SaleRecord) -> dict[str, Any]:
    return {
        "order_item_id": sale.order_item_id,
        "sale_status": sale.sale_status,
        "quantity": sale.quantity,
    }


def _write_rules(root: Path) -> None:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "anomaly_rules.yaml").write_text(
        """sales_trend:
  short_window_days: 3
  long_window_days: 15
traffic_conversion:
  high_page_views_percentile: 50
  low_conversion_percentile: 25
  low_page_views_percentile: 25
  high_conversion_percentile: 75
stale_offer_snapshot_hours: 26
""",
        encoding="utf-8",
    )
    (config / "sale_status_rules.yaml").write_text(
        "included:\n  - included\nexcluded:\n  - excluded\nunknown: []\n",
        encoding="utf-8",
    )


def _plain(value: object) -> object:
    if isinstance(value, (date, datetime, Decimal)):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value
