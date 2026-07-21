"""Typed records shared by API, storage, and metrics modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping
from zoneinfo import ZoneInfo


SAST = ZoneInfo("Africa/Johannesburg")


def sast_date(value: datetime) -> date:
    """Return the South African calendar date for a timezone-aware datetime."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("value must be timezone-aware")
    return value.astimezone(SAST).date()


@dataclass(frozen=True)
class OfferRecord:
    offer_id: str
    tsin_id: str | None
    sku: str | None
    barcode: str | None
    title: str | None
    selling_price: Decimal | None
    rrp: Decimal | None
    benchmark_price: Decimal | None
    status: str | None
    image_url: str | None
    productline_id: str | None
    conversion_percentage_30_days: Decimal | None
    conversion_percentage_previous_30_days: Decimal | None
    page_views_30_days: int | None
    quantity_returned_30_days: int | None
    total_wishlist: int | None
    wishlist_30_days: int | None
    listing_quality: str | None
    discount_percentage: Decimal | None
    updated_at: datetime | None
    captured_at: datetime
    total_stock: int | None = None

    @classmethod
    def from_api(cls, payload: Mapping[str, Any], captured_at: datetime) -> OfferRecord:
        """Convert one Takealot Offers API payload into a typed record."""
        return cls(
            offer_id=str(payload["offer_id"]),
            tsin_id=_optional_string(payload.get("tsin_id")),
            sku=_optional_string(payload.get("sku")),
            barcode=_optional_string(payload.get("barcode")),
            title=_optional_string(payload.get("title")),
            selling_price=_optional_decimal(payload.get("selling_price")),
            rrp=_optional_decimal(payload.get("rrp")),
            benchmark_price=_optional_decimal(payload.get("benchmark_price")),
            status=_optional_string(payload.get("status")),
            image_url=_optional_string(payload.get("image_url")),
            productline_id=_optional_string(payload.get("productline_id")),
            conversion_percentage_30_days=_optional_decimal(
                payload.get("conversion_percentage_30_days")
            ),
            conversion_percentage_previous_30_days=_optional_decimal(
                payload.get("conversion_percentage_previous_30_days")
            ),
            page_views_30_days=_optional_int(payload.get("page_views_30_days")),
            quantity_returned_30_days=_optional_int(payload.get("quantity_returned_30_days")),
            total_wishlist=_optional_int(payload.get("total_wishlist")),
            wishlist_30_days=_optional_int(payload.get("wishlist_30_days")),
            listing_quality=_optional_string(payload.get("listing_quality")),
            discount_percentage=_optional_decimal(payload.get("discount_percentage")),
            updated_at=_optional_datetime(payload.get("updated_at")),
            captured_at=_require_aware_datetime(captured_at, "captured_at"),
            total_stock=_optional_int(payload.get("total_stock")),
        )


@dataclass(frozen=True)
class SaleRecord:
    order_item_id: str
    order_id: str | None
    order_date: datetime
    sale_status: str | None
    offer_id: str | None
    tsin_id: str | None
    sku: str | None
    selling_price: Decimal | None
    quantity: int
    success_fee: Decimal | None
    fulfillment_fee: Decimal | None
    courier_collection_fee: Decimal | None
    total_fees: Decimal | None
    stock_transfer_fee: Decimal | None
    sales_region: str | None
    stock_source_region: str | None

    def __post_init__(self) -> None:
        """Reject naive timestamps regardless of how the record was constructed."""
        _require_aware_datetime(self.order_date, "order_date")

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> SaleRecord:
        """Convert one Takealot Sales API payload into a typed record."""
        return cls(
            order_item_id=str(payload["order_item_id"]),
            order_id=_optional_string(payload.get("order_id")),
            order_date=_parse_datetime(payload["order_date"], "order_date"),
            sale_status=_optional_string(payload.get("sale_status")),
            offer_id=_optional_string(payload.get("offer_id")),
            tsin_id=_optional_string(payload.get("tsin_id")),
            sku=_optional_string(payload.get("sku")),
            selling_price=_optional_decimal(payload.get("selling_price")),
            quantity=int(payload["quantity"]),
            success_fee=_optional_decimal(payload.get("success_fee")),
            fulfillment_fee=_optional_decimal(payload.get("fulfillment_fee")),
            courier_collection_fee=_optional_decimal(payload.get("courier_collection_fee")),
            total_fees=_optional_decimal(payload.get("total_fees")),
            stock_transfer_fee=_optional_decimal(payload.get("stock_transfer_fee")),
            sales_region=_optional_string(payload.get("sales_region")),
            stock_source_region=_optional_string(payload.get("stock_source_region")),
        )

    @property
    def sales_day(self) -> date:
        """Return the record's sales day in South African Standard Time."""
        return sast_date(self.order_date)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    return _parse_datetime(value, "updated_at")


def _parse_datetime(value: Any, field_name: str) -> datetime:
    parsed_value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return _require_aware_datetime(parsed_value, field_name)


def _require_aware_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value
