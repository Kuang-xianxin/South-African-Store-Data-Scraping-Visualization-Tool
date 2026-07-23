"""SQLAlchemy models for durable operations data."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all persistent entities."""


class CollectionRun(Base):
    """One collection attempt and its outcome."""

    __tablename__ = "collection_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_type: Mapped[str] = mapped_column(String(50), nullable=False)
    scope_date: Mapped[date | None] = mapped_column(Date)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(String(30))
    counts: Mapped[dict[str, int] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)


class OfferCurrent(Base):
    """Latest known state for a seller offer."""

    __tablename__ = "offer_current"

    offer_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    tsin_id: Mapped[str | None] = mapped_column(String(100))
    sku: Mapped[str | None] = mapped_column(String(255))
    barcode: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str | None] = mapped_column(Text)
    selling_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    rrp: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    benchmark_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    status: Mapped[str | None] = mapped_column(String(100))
    image_url: Mapped[str | None] = mapped_column(Text)
    productline_id: Mapped[str | None] = mapped_column(String(100))
    conversion_percentage_30_days: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    conversion_percentage_previous_30_days: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4)
    )
    page_views_30_days: Mapped[int | None] = mapped_column(Integer)
    quantity_returned_30_days: Mapped[int | None] = mapped_column(Integer)
    total_wishlist: Mapped[int | None] = mapped_column(Integer)
    wishlist_30_days: Mapped[int | None] = mapped_column(Integer)
    listing_quality: Mapped[str | None] = mapped_column(String(100))
    discount_percentage: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_stock: Mapped[int | None] = mapped_column(Integer)
    takealot_available_stock: Mapped[int | None] = mapped_column(Integer)
    seller_available_stock: Mapped[int | None] = mapped_column(Integer)
    takealot_stock_in_receiving: Mapped[int | None] = mapped_column(Integer)
    takealot_stock_on_way: Mapped[int | None] = mapped_column(Integer)


class OfferSnapshot(Base):
    """Daily historical offer state and traffic snapshot."""

    __tablename__ = "offer_snapshots"
    __table_args__ = (UniqueConstraint("snapshot_date", "offer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    offer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    tsin_id: Mapped[str | None] = mapped_column(String(100))
    sku: Mapped[str | None] = mapped_column(String(255))
    barcode: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str | None] = mapped_column(Text)
    selling_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    rrp: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    benchmark_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    status: Mapped[str | None] = mapped_column(String(100))
    image_url: Mapped[str | None] = mapped_column(Text)
    productline_id: Mapped[str | None] = mapped_column(String(100))
    conversion_percentage_30_days: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    conversion_percentage_previous_30_days: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4)
    )
    page_views_30_days: Mapped[int | None] = mapped_column(Integer)
    quantity_returned_30_days: Mapped[int | None] = mapped_column(Integer)
    total_wishlist: Mapped[int | None] = mapped_column(Integer)
    wishlist_30_days: Mapped[int | None] = mapped_column(Integer)
    listing_quality: Mapped[str | None] = mapped_column(String(100))
    discount_percentage: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_stock: Mapped[int | None] = mapped_column(Integer)
    takealot_available_stock: Mapped[int | None] = mapped_column(Integer)
    seller_available_stock: Mapped[int | None] = mapped_column(Integer)
    takealot_stock_in_receiving: Mapped[int | None] = mapped_column(Integer)
    takealot_stock_on_way: Mapped[int | None] = mapped_column(Integer)


class SaleItem(Base):
    """A sale line item, kept current by order item identifier."""

    __tablename__ = "sale_items"

    order_item_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    order_id: Mapped[str | None] = mapped_column(String(100))
    order_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sales_day: Mapped[date] = mapped_column(Date, nullable=False)
    sale_status: Mapped[str | None] = mapped_column(String(100))
    offer_id: Mapped[str | None] = mapped_column(String(100))
    tsin_id: Mapped[str | None] = mapped_column(String(100))
    sku: Mapped[str | None] = mapped_column(String(255))
    selling_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    success_fee: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    fulfillment_fee: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    courier_collection_fee: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    total_fees: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    stock_transfer_fee: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    sales_region: Mapped[str | None] = mapped_column(String(100))
    stock_source_region: Mapped[str | None] = mapped_column(String(100))
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ReturnItem(Base):
    """A seller return item reserved for return collection."""

    __tablename__ = "return_items"

    seller_return_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    order_item_id: Mapped[str | None] = mapped_column(String(100))
    offer_id: Mapped[str | None] = mapped_column(String(100))
    return_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    return_status: Mapped[str | None] = mapped_column(String(100))
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class DailyProductMetric(Base):
    """Precomputed daily product metrics for dashboard queries."""

    __tablename__ = "daily_product_metrics"
    __table_args__ = (UniqueConstraint("metric_date", "offer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    offer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    sku: Mapped[str | None] = mapped_column(String(255))
    ordered_units: Mapped[int | None] = mapped_column(Integer)
    effective_units: Mapped[int | None] = mapped_column(Integer)
    ordered_revenue: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    page_views_30_days: Mapped[int | None] = mapped_column(Integer)
    page_views_30_day_average: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    page_views_window_net_change: Mapped[int | None] = mapped_column(Integer)
    conversion_percentage_30_days: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    conversion_percentage_previous_30_days: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4)
    )
    conversion_change_points: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    total_stock: Mapped[int | None] = mapped_column(Integer)
    offer_status: Mapped[str | None] = mapped_column(String(100))


class AnomalyEvent(Base):
    """A date-stamped anomaly classification for an offer."""

    __tablename__ = "anomaly_events"
    __table_args__ = (UniqueConstraint("event_date", "offer_id", "anomaly_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    offer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    anomaly_type: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str | None] = mapped_column(String(30))
    explanation: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DataQualityEvent(Base):
    """A quality issue observed during collection or metric generation."""

    __tablename__ = "data_quality_events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str | None] = mapped_column(String(30))
    offer_id: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CompetitorTarget(Base):
    """A Takealot product explicitly added to the competitor watch list."""

    __tablename__ = "competitor_targets"

    plid: Mapped[str] = mapped_column(String(30), primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CompetitorSnapshot(Base):
    """One timestamped competitor observation and bounded sales estimate."""

    __tablename__ = "competitor_snapshots"
    __table_args__ = (UniqueConstraint("plid", "collected_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plid: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    image_url: Mapped[str | None] = mapped_column(Text)
    sku: Mapped[str | None] = mapped_column(String(100))
    seller_id: Mapped[str | None] = mapped_column(String(100))
    seller_name: Mapped[str | None] = mapped_column(String(255))
    price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    stock_status: Mapped[str | None] = mapped_column(String(100))
    stock_quantity: Mapped[int | None] = mapped_column(Integer)
    stock_exact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stock_method: Mapped[str] = mapped_column(String(100), nullable=False)
    stock_note: Mapped[str | None] = mapped_column(Text)
    review_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_review_count: Mapped[int] = mapped_column(Integer, nullable=False)
    rating: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    positive_reviews: Mapped[int] = mapped_column(Integer, nullable=False)
    neutral_reviews: Mapped[int] = mapped_column(Integer, nullable=False)
    negative_reviews: Mapped[int] = mapped_column(Integer, nullable=False)
    lifetime_sales_min: Mapped[int] = mapped_column(Integer, nullable=False)
    lifetime_sales_max: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_snapshot_id: Mapped[int | None] = mapped_column(Integer)
    observed_stock_outflow: Mapped[int | None] = mapped_column(Integer)
    review_delta: Mapped[int | None] = mapped_column(Integer)
    period_sales_min: Mapped[int | None] = mapped_column(Integer)
    period_sales_max: Mapped[int | None] = mapped_column(Integer)
    trend_label: Mapped[str] = mapped_column(String(100), nullable=False)
    trend_note: Mapped[str] = mapped_column(Text, nullable=False)
    offers: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)


class CompetitorReview(Base):
    """Latest known public review body for a watched competitor product."""

    __tablename__ = "competitor_reviews"
    __table_args__ = (UniqueConstraint("plid", "review_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plid: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    review_id: Mapped[str] = mapped_column(String(200), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    customer_name: Mapped[str | None] = mapped_column(String(255))
    review_date: Mapped[str | None] = mapped_column(String(100))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
