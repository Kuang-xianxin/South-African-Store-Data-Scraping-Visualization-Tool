from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import pytest
from sqlalchemy import Engine, Select, select, text
from sqlalchemy.orm import Session

from takealot_ops.domain import OfferRecord, SaleRecord
from takealot_ops.settings import Settings
from takealot_ops.storage.migrations import create_engine_for_settings, create_schema
from takealot_ops.storage.models import OfferCurrent, OfferSnapshot, SaleItem
from takealot_ops.storage.repository import Repository


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    settings = Settings(
        project_root=tmp_path,
        api_key="test-api-key",
        base_url="https://example.test/v1",
        database_url=f"sqlite:///{(tmp_path / 'takealot.db').as_posix()}",
        request_timeout_seconds=30.0,
        dashboard_host="127.0.0.1",
        dashboard_port=8501,
    )
    database_engine = create_engine_for_settings(settings)
    create_schema(database_engine)
    yield database_engine
    database_engine.dispose()


@pytest.fixture
def offer() -> OfferRecord:
    return OfferRecord(
        offer_id="offer-1",
        tsin_id="tsin-1",
        sku="SKU-1",
        barcode="1234567890123",
        title="Original title",
        selling_price=Decimal("199.99"),
        rrp=Decimal("249.99"),
        benchmark_price=Decimal("209.99"),
        status="buyable",
        image_url="https://example.test/image.jpg",
        productline_id="productline-1",
        conversion_percentage_30_days=Decimal("2.5"),
        conversion_percentage_previous_30_days=Decimal("1.5"),
        page_views_30_days=120,
        quantity_returned_30_days=3,
        total_wishlist=10,
        wishlist_30_days=2,
        listing_quality="good",
        discount_percentage=Decimal("20"),
        updated_at=datetime(2026, 7, 20, 7, 30, tzinfo=UTC),
        captured_at=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
    )


@pytest.fixture
def sale() -> SaleRecord:
    return SaleRecord(
        order_item_id="item-1",
        order_id="order-1",
        order_date=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
        sale_status="pending",
        offer_id="offer-1",
        tsin_id="tsin-1",
        sku="SKU-1",
        selling_price=Decimal("199.99"),
        quantity=2,
        success_fee=Decimal("20.00"),
        fulfillment_fee=Decimal("10.00"),
        courier_collection_fee=Decimal("5.00"),
        total_fees=Decimal("35.00"),
        stock_transfer_fee=Decimal("0.00"),
        sales_region="Gauteng",
        stock_source_region="Western Cape",
    )


def test_offer_snapshot_is_unique_by_day_and_offer_id(engine: Engine, offer: OfferRecord) -> None:
    snapshot_date = date(2026, 7, 20)
    updated_offer = replace(offer, title="Updated title", selling_price=Decimal("189.99"))

    with Session(engine) as session:
        with session.begin():
            repository = Repository(session)
            repository.upsert_offer_snapshot(offer, snapshot_date)
            repository.upsert_offer_snapshot(updated_offer, snapshot_date)

    with Session(engine) as session:
        snapshots = session.scalars(select(OfferSnapshot)).all()

    assert len(snapshots) == 1
    assert snapshots[0].title == "Updated title"
    assert snapshots[0].selling_price == Decimal("189.99")


def test_repeated_sale_updates_status_without_duplicate_order_item(
    engine: Engine, sale: SaleRecord
) -> None:
    raw_payload: dict[str, Any] = {"source": "first", "nested": {"page": 1}}
    updated_sale = replace(sale, sale_status="completed")

    with Session(engine) as session:
        with session.begin():
            repository = Repository(session)
            repository.upsert_sale(sale, raw_payload)
            repository.upsert_sale(updated_sale, {"source": "second", "nested": {"page": 2}})

    with Session(engine) as session:
        sales = session.scalars(select(SaleItem)).all()

    assert len(sales) == 1
    assert sales[0].sale_status == "completed"
    assert sales[0].raw_payload == {"source": "second", "nested": {"page": 2}}


def test_failed_transaction_rolls_back_all_rows(engine: Engine, offer: OfferRecord) -> None:
    with pytest.raises(RuntimeError, match="stop collection"):
        with Session(engine) as session:
            with session.begin():
                repository = Repository(session)
                repository.upsert_offer_snapshot(offer, date(2026, 7, 20))
                raise RuntimeError("stop collection")

    with Session(engine) as session:
        current_offers = session.scalars(select(OfferCurrent)).all()
        snapshots = session.scalars(select(OfferSnapshot)).all()

    assert current_offers == []
    assert snapshots == []


def test_sqlite_engine_uses_wal_and_busy_timeout(engine: Engine) -> None:
    with engine.connect() as connection:
        journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
        busy_timeout = connection.execute(text("PRAGMA busy_timeout")).scalar_one()
        foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar_one()

    assert str(journal_mode).lower() == "wal"
    assert busy_timeout == 5000
    assert foreign_keys == 1


def test_repository_works_with_plain_sqlalchemy_selects(
    engine: Engine, offer: OfferRecord, sale: SaleRecord
) -> None:
    with Session(engine) as session:
        with session.begin():
            repository = Repository(session)
            repository.upsert_offer_snapshot(offer, date(2026, 7, 20))
            repository.upsert_sale(sale, {"order_item_id": sale.order_item_id})

    snapshot_statement: Select[tuple[OfferSnapshot]] = select(OfferSnapshot).where(
        OfferSnapshot.offer_id == offer.offer_id
    )
    sale_statement: Select[tuple[SaleItem]] = select(SaleItem).where(
        SaleItem.order_item_id == sale.order_item_id
    )
    with Session(engine) as session:
        snapshot = session.scalar(snapshot_statement)
        persisted_sale = session.scalar(sale_statement)

    assert snapshot is not None
    assert snapshot.sku == "SKU-1"
    assert persisted_sale is not None
    assert persisted_sale.sales_day == date(2026, 7, 20)
