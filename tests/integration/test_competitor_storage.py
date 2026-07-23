from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from takealot_ops.competitors.domain import (
    CompetitorOffer,
    CompetitorProduct,
    CompetitorReviewRecord,
    analyze_sales_signal,
    estimate_lifetime_sales,
    summarize_reviews,
)
from takealot_ops.competitors.repository import CompetitorRepository
from takealot_ops.competitors.service import load_competitor_dataset
from takealot_ops.competitors.stock import skipped_stock_probe
from takealot_ops.competitors.web import create_app
from takealot_ops.storage.migrations import create_schema


def test_competitor_observation_persists_snapshot_and_deduplicated_reviews(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'competitors.db').as_posix()}")
    create_schema(engine)
    product = CompetitorProduct(
        plid="72189176",
        url="https://www.takealot.com/example/PLID72189176",
        title="Laser Lipo",
        image_url=None,
        sku="SKU-1",
        seller_id="seller-1",
        seller_name="Seller One",
        price=6597.0,
        stock_status="In stock",
        is_leadtime=False,
        review_count=1,
        rating=5.0,
        offers=(
            CompetitorOffer(
                selected=True,
                sku="SKU-1",
                seller_id="seller-1",
                seller_name="Seller One",
                price=6597.0,
                stock_status="In stock",
            ),
        ),
    )
    reviews = [
        CompetitorReviewRecord(
            review_id="review-1",
            rating=5,
            title="Great",
            body="Works",
            customer_name="Buyer",
            review_date="2026-07-20",
        )
    ]

    for collected_at in (
        datetime(2026, 7, 22, 8, tzinfo=UTC),
        datetime(2026, 7, 23, 8, tzinfo=UTC),
    ):
        with Session(engine) as session, session.begin():
            repository = CompetitorRepository(session)
            previous = repository.latest_compatible_snapshot(product)
            repository.save_observation(
                product=product,
                reviews=reviews,
                review_summary=summarize_reviews(reviews),
                stock=skipped_stock_probe(),
                lifetime_sales=estimate_lifetime_sales(product.review_count),
                signal=analyze_sales_signal(
                    previous,
                    current_stock_quantity=None,
                    current_stock_exact=False,
                    current_review_count=product.review_count,
                ),
                collected_at=collected_at,
            )

    dataset = load_competitor_dataset(engine)
    engine.dispose()

    assert len(dataset.current) == 1
    assert len(dataset.history) == 2
    assert len(dataset.reviews) == 1
    assert dataset.current.iloc[0]["累计销量估算"] == "20–50"
    assert dataset.current.iloc[0]["趋势判断"] == "暂未观察到净流出"


def test_competitor_api_reads_the_shared_sqlite(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "empty.db"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    app = create_app(tmp_path)

    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        assert client.get("/api/competitors").json() == {"items": []}
        invalid = client.post(
            "/api/competitors/collect",
            json={"url": "https://www.takealot.com/not-a-product"},
        )
        assert invalid.status_code == 422
        assert "PLID" in invalid.json()["detail"]
