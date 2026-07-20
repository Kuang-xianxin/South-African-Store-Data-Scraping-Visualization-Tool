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
    }

    record = OfferRecord.from_api(payload, captured_at)

    assert record.offer_id == "offer-1"
    assert record.sku == "SKU-1"
    assert record.selling_price == Decimal("199.99")
    assert record.page_views_30_days == 120
    assert record.updated_at == datetime(2026, 7, 20, 7, 30, tzinfo=UTC)
    assert record.captured_at == captured_at

    with pytest.raises(FrozenInstanceError):
        record.sku = "changed"  # type: ignore[misc]


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
