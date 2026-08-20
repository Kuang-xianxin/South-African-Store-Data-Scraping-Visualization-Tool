from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from takealot_ops.erp.service import build_quadrant_payload, load_quadrant_dataset
from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import CollectionRun, DailyProductMetric, OfferSnapshot


def test_quadrant_projection_loads_only_required_frames_and_keeps_plid(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "quadrants.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url)
    create_schema(engine)
    with Session(engine) as session:
        session.add(
            CollectionRun(
                run_id="offers-current",
                run_type="offers",
                scope_date=date(2026, 7, 20),
                started_at=datetime(2026, 7, 20, 8, tzinfo=UTC),
                finished_at=datetime(2026, 7, 20, 9, tzinfo=UTC),
                status="success",
                counts={"offers": 1},
            )
        )
        session.add_all(
            [
                DailyProductMetric(
                    metric_date=date(2026, 7, 19),
                    offer_id="offer-a",
                    sku="SKU-A",
                    ordered_units=2,
                    page_views_30_days=90,
                    total_stock=3,
                    offer_status="buyable",
                ),
                DailyProductMetric(
                    metric_date=date(2026, 7, 20),
                    offer_id="offer-a",
                    sku="SKU-A",
                    ordered_units=4,
                    page_views_30_days=120,
                    total_stock=7,
                    offer_status="buyable",
                ),
                OfferSnapshot(
                    snapshot_date=date(2026, 7, 19),
                    offer_id="offer-a",
                    sku="SKU-A",
                    title="测试商品",
                    productline_id="12345678",
                    image_url="https://media.takealot.com/covers_images/test.jpg",
                    created_at=datetime(2026, 1, 1, 8, tzinfo=UTC),
                    captured_at=datetime(2026, 7, 19, 9, tzinfo=UTC),
                    total_stock=3,
                ),
                OfferSnapshot(
                    snapshot_date=date(2026, 7, 20),
                    offer_id="offer-a",
                    sku="SKU-A",
                    title="测试商品",
                    productline_id="12345678",
                    image_url="https://media.takealot.com/covers_images/test.jpg",
                    created_at=datetime(2026, 1, 1, 8, tzinfo=UTC),
                    captured_at=datetime(2026, 7, 20, 9, tzinfo=UTC),
                    total_stock=7,
                ),
            ]
        )
        session.commit()
    engine.dispose()

    settings = DashboardSettings(
        project_root=tmp_path,
        database_url=database_url,
        dashboard_host="127.0.0.1",
        dashboard_port=8501,
    )
    dataset = load_quadrant_dataset(settings, date(2026, 7, 20))
    payload = build_quadrant_payload(dataset, date(2026, 7, 20), 50)

    assert len(dataset.product_daily) == 2
    assert len(dataset.offer_current) == 1
    assert len(dataset.offer_history) == 2
    assert dataset.anomalies.empty
    assert dataset.quality_events.empty
    assert len(payload["items"]) == 1
    assert payload["items"][0]["productline_id"] == "12345678"
    assert payload["items"][0]["ordered_units"] == 6
    assert payload["items"][0]["latest_restock_increase"] == 4
