"""Immutable lightweight rows for historical radar reads (no ORM change tracking)."""

from datetime import datetime
from decimal import Decimal
from typing import Any, NamedTuple


class SnapshotRow(NamedTuple):
    id: int
    plid: str
    collected_at: datetime
    url: str
    title: str
    image_url: str | None
    category_path: list[dict[str, Any]] | None
    sku: str | None
    seller_id: str | None
    seller_name: str | None
    price: Decimal | None
    stock_status: str | None
    stock_quantity: int | None
    stock_exact: bool
    stock_method: str
    stock_note: str | None
    review_count: int
    fetched_review_count: int
    rating: Decimal | None
    positive_reviews: int
    neutral_reviews: int
    negative_reviews: int
    lifetime_sales_min: int
    lifetime_sales_max: int
    previous_snapshot_id: int | None
    observed_stock_outflow: int | None
    review_delta: int | None
    period_sales_min: int | None
    period_sales_max: int | None
    trend_label: str
    trend_note: str
    offers: list[dict[str, Any]] | None


class VariantRow(NamedTuple):
    id: int
    snapshot_id: int
    plid: str
    collected_at: datetime
    variant_key: str
    variant_label: str
    image_url: str | None
    url: str
    sku: str | None
    seller_id: str | None
    seller_name: str | None
    price: Decimal | None
    stock_status: str | None
    is_leadtime: bool
    stock_quantity: int | None
    stock_exact: bool
    stock_method: str
    stock_note: str | None
    customer_purchase_limit: int | None
