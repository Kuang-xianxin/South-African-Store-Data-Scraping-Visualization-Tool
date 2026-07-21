from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from takealot_ops.domain import OfferRecord, SaleRecord, sast_date


def test_sast_date_uses_south_african_day() -> None:
    value = datetime.fromisoformat("2026-07-19T23:30:00+00:00")

    assert sast_date(value).isoformat() == "2026-07-20"


def test_offer_record_maps_api_payload_and_keeps_capture_time() -> None:
    captured_at = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
    payload = {
        "offer_id": "offer-1",
        "tsin_id": "tsin-1",
        "sku": "SKU-1",
        "barcode": "1234567890123",
        "title": "Example offer",
        "selling_price": "199.99",
        "rrp": "249.99",
        "benchmark_price": "209.99",
        "status": "buyable",
        "image_url": "https://example.test/image.jpg",
        "productline_id": "productline-1",
        "conversion_percentage_30_days": "2.5",
        "conversion_percentage_previous_30_days": "1.5",
        "page_views_30_days": 120,
        "quantity_returned_30_days": 3,
        "total_wishlist": 10,
        "wishlist_30_days": 2,
        "listing_quality": "good",
        "discount_percentage": "20",
        "updated_at": "2026-07-20T07:30:00+00:00",
        "seller_warehouse_stock": [
            {"seller_warehouse_id": 1, "quantity_available": 4}
        ],
        "takealot_warehouse_stock": [
            {
                "region": "CPT",
                "quantity_available": 2,
                "stock_in_receiving": 3,
                "stock_on_way": 1,
            },
            {
                "region": "JHB",
                "quantity_available": 5,
                "stock_in_receiving": 0,
                "stock_on_way": 2,
            },
        ],
    }

    record = OfferRecord.from_api(payload, captured_at)

    assert record.offer_id == "offer-1"
    assert record.sku == "SKU-1"
    assert record.selling_price == Decimal("199.99")
    assert record.page_views_30_days == 120
    assert record.updated_at == datetime(2026, 7, 20, 7, 30, tzinfo=UTC)
    assert record.captured_at == captured_at
    assert record.takealot_available_stock == 7
    assert record.total_stock == 7
    assert record.seller_available_stock == 4
    assert record.takealot_stock_in_receiving == 3
    assert record.takealot_stock_on_way == 3

    with pytest.raises(FrozenInstanceError):
        record.sku = "changed"  # type: ignore[misc]


def test_offer_record_keeps_stock_unknown_when_expands_were_not_requested() -> None:
    captured_at = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
    record = OfferRecord.from_api({"offer_id": "offer-1"}, captured_at)

    assert record.total_stock is None
    assert record.takealot_available_stock is None
    assert record.seller_available_stock is None


def test_sale_record_maps_api_payload_and_groups_by_sast_day() -> None:
    payload = {
        "order_item_id": "item-1",
        "order_id": "order-1",
        "order_date": "2026-07-19T23:30:00+00:00",
        "sale_status": "completed",
        "offer_id": "offer-1",
        "tsin_id": "tsin-1",
        "sku": "SKU-1",
        "selling_price": "199.99",
        "quantity": 2,
        "success_fee": "20.00",
        "fulfillment_fee": "10.00",
        "courier_collection_fee": "5.00",
        "total_fees": "35.00",
        "stock_transfer_fee": "0.00",
        "sales_region": "Gauteng",
        "stock_source_region": "Western Cape",
    }

    record = SaleRecord.from_api(payload)

    assert record.order_item_id == "item-1"
    assert record.selling_price == Decimal("199.99")
    assert record.quantity == 2
    assert record.order_date == datetime(2026, 7, 19, 23, 30, tzinfo=UTC)
    assert record.order_date.tzinfo is not None
    assert record.sales_day.isoformat() == "2026-07-20"


def test_sast_date_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        sast_date(datetime(2026, 7, 20, 8, 0))


def test_sale_record_rejects_a_naive_order_date_on_direct_construction() -> None:
    with pytest.raises(ValueError, match="order_date must be timezone-aware"):
        SaleRecord(
            order_item_id="item-1",
            order_id="order-1",
            order_date=datetime(2026, 7, 20, 8, 0),
            sale_status="completed",
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
