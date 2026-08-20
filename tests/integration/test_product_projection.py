from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from takealot_ops.erp.service import (
    build_product_detail_payload,
    build_products_payload,
    load_erp_dataset,
    load_product_detail_dataset,
    load_product_list_dataset,
)
from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import CollectionRun, DailyProductMetric, OfferSnapshot


def test_product_projections_match_the_canonical_dataset(tmp_path: Path) -> None:
    database_path = tmp_path / "products.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url)
    create_schema(engine)
    with Session(engine) as session:
        session.add(
            CollectionRun(
                run_id="offers-current",
                run_type="offers",
                scope_date=date(2026, 8, 20),
                started_at=datetime(2026, 8, 20, 8, tzinfo=UTC),
                finished_at=datetime(2026, 8, 20, 9, tzinfo=UTC),
                status="success",
                counts={"offers": 2},
            )
        )
        for metric_date, units, views in (
            (date(2026, 8, 19), 2, 90),
            (date(2026, 8, 20), 4, 120),
        ):
            session.add(
                DailyProductMetric(
                    metric_date=metric_date,
                    offer_id="offer-a",
                    sku="SKU-A",
                    ordered_units=units,
                    effective_units=units,
                    ordered_revenue=units * 100,
                    page_views_30_days=views,
                    conversion_percentage_30_days=3.5,
                    total_stock=7,
                    offer_status="buyable",
                )
            )
        session.add(
            DailyProductMetric(
                metric_date=date(2026, 8, 20),
                offer_id="offer-b",
                sku="SKU-B",
                ordered_units=1,
                effective_units=1,
                ordered_revenue=80,
                page_views_30_days=30,
                conversion_percentage_30_days=2.0,
                total_stock=2,
                offer_status="buyable",
            )
        )
        for snapshot_date in (date(2026, 8, 19), date(2026, 8, 20)):
            session.add(
                OfferSnapshot(
                    snapshot_date=snapshot_date,
                    offer_id="offer-a",
                    sku="SKU-A",
                    title="Product A",
                    productline_id="12345678",
                    selling_price=100,
                    rrp=120,
                    status="buyable",
                    image_url="https://example.test/a.jpg",
                    captured_at=datetime(2026, 8, 20, 9, tzinfo=UTC),
                    total_stock=7,
                )
            )
        session.add(
            OfferSnapshot(
                snapshot_date=date(2026, 8, 20),
                offer_id="offer-b",
                sku="SKU-B",
                title="Product B",
                productline_id="87654321",
                selling_price=80,
                rrp=90,
                status="buyable",
                image_url="https://example.test/b.jpg",
                captured_at=datetime(2026, 8, 20, 9, tzinfo=UTC),
                total_stock=2,
            )
        )
        session.commit()
    engine.dispose()

    settings = DashboardSettings(
        project_root=Path.cwd(),
        database_url=database_url,
        dashboard_host="127.0.0.1",
        dashboard_port=8501,
    )
    as_of = date(2026, 8, 20)
    canonical = load_erp_dataset(settings, as_of)
    product_list = load_product_list_dataset(settings, as_of)
    product_detail = load_product_detail_dataset(settings, as_of, "offer-a")

    assert build_products_payload(product_list, as_of) == build_products_payload(
        canonical,
        as_of,
    )
    assert build_product_detail_payload(
        product_detail,
        as_of,
        "offer-a",
    ) == build_product_detail_payload(canonical, as_of, "offer-a")
    assert len(product_list.product_daily) == 2
    assert len(product_detail.product_daily) == 2
    assert product_list.anomalies.empty
    assert product_detail.quality_events.empty
