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
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from takealot_ops.storage.store_context import StoreScopedMixin, current_store_code


class Base(DeclarativeBase):
    """Base class for all persistent entities."""


class CollectionRun(StoreScopedMixin, Base):
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


class OfferCurrent(StoreScopedMixin, Base):
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
    conversion_percentage_previous_30_days: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    page_views_30_days: Mapped[int | None] = mapped_column(Integer)
    quantity_returned_30_days: Mapped[int | None] = mapped_column(Integer)
    total_wishlist: Mapped[int | None] = mapped_column(Integer)
    wishlist_30_days: Mapped[int | None] = mapped_column(Integer)
    listing_quality: Mapped[str | None] = mapped_column(String(100))
    discount_percentage: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_stock: Mapped[int | None] = mapped_column(Integer)
    takealot_available_stock: Mapped[int | None] = mapped_column(Integer)
    seller_available_stock: Mapped[int | None] = mapped_column(Integer)
    takealot_stock_in_receiving: Mapped[int | None] = mapped_column(Integer)
    takealot_stock_on_way: Mapped[int | None] = mapped_column(Integer)


class OfferSnapshot(StoreScopedMixin, Base):
    """Daily historical offer state and traffic snapshot."""

    __tablename__ = "offer_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "store_code",
            "snapshot_date",
            "offer_id",
            name="uq_offer_snapshots_store_date_offer",
        ),
    )

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
    conversion_percentage_previous_30_days: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    page_views_30_days: Mapped[int | None] = mapped_column(Integer)
    quantity_returned_30_days: Mapped[int | None] = mapped_column(Integer)
    total_wishlist: Mapped[int | None] = mapped_column(Integer)
    wishlist_30_days: Mapped[int | None] = mapped_column(Integer)
    listing_quality: Mapped[str | None] = mapped_column(String(100))
    discount_percentage: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_stock: Mapped[int | None] = mapped_column(Integer)
    takealot_available_stock: Mapped[int | None] = mapped_column(Integer)
    seller_available_stock: Mapped[int | None] = mapped_column(Integer)
    takealot_stock_in_receiving: Mapped[int | None] = mapped_column(Integer)
    takealot_stock_on_way: Mapped[int | None] = mapped_column(Integer)


class SearchRankingAnalysis(StoreScopedMixin, Base):
    """One auditable image-to-keyword analysis and ranking collection run."""

    __tablename__ = "search_ranking_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    productline_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    sku: Mapped[str | None] = mapped_column(String(255))
    source_title: Mapped[str] = mapped_column(Text, nullable=False)
    source_image_url: Mapped[str] = mapped_column(Text, nullable=False)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    product_name: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    vision_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    vision_reused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    title_suggestion: Mapped[str | None] = mapped_column(Text)
    title_reason: Mapped[str | None] = mapped_column(Text)
    title_validation: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class SearchRankingKeywordResult(StoreScopedMixin, Base):
    """One platform-validated keyword and its immutable organic position evidence."""

    __tablename__ = "search_ranking_keyword_results"
    __table_args__ = (
        UniqueConstraint(
            "analysis_id",
            "candidate_order",
            name="uq_search_ranking_result_analysis_order",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(
        ForeignKey("search_ranking_analyses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    keyword: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    candidate_order: Mapped[int] = mapped_column(Integer, nullable=False)
    relevance_status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    relevance_score: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    validation_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    total_num_found: Mapped[int | None] = mapped_column(Integer)
    pages_scanned: Mapped[int] = mapped_column(Integer, nullable=False)
    found: Mapped[bool] = mapped_column(Boolean, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    page_rank: Mapped[int | None] = mapped_column(Integer)
    organic_rank: Mapped[int | None] = mapped_column(Integer)
    row_number: Mapped[int | None] = mapped_column(Integer)
    column_number: Mapped[int | None] = mapped_column(Integer)
    columns_per_row: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    target_url: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class StoreOfferBaseline(StoreScopedMixin, Base):
    """Earliest Seller API offer pull retained for one Beijing display day."""

    __tablename__ = "store_offer_baselines"
    __table_args__ = (
        UniqueConstraint(
            "store_code",
            "display_date",
            "offer_id",
            name="uq_store_offer_baselines_store_date_offer",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    display_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    offer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    productline_id: Mapped[str | None] = mapped_column(String(100), index=True)
    sku: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    selling_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    status: Mapped[str | None] = mapped_column(String(100))
    total_stock: Mapped[int | None] = mapped_column(Integer)
    takealot_available_stock: Mapped[int | None] = mapped_column(Integer)
    seller_available_stock: Mapped[int | None] = mapped_column(Integer)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StoreOfferObservation(StoreScopedMixin, Base):
    """One immutable Seller API offer observation from every complete refresh."""

    __tablename__ = "store_offer_observations"
    __table_args__ = (
        UniqueConstraint(
            "store_code",
            "captured_at",
            "offer_id",
            name="uq_store_offer_observations_store_time_offer",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    display_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    offer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    productline_id: Mapped[str | None] = mapped_column(String(100), index=True)
    sku: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    selling_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    status: Mapped[str | None] = mapped_column(String(100))
    total_stock: Mapped[int | None] = mapped_column(Integer)
    takealot_available_stock: Mapped[int | None] = mapped_column(Integer)
    seller_available_stock: Mapped[int | None] = mapped_column(Integer)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SaleItem(StoreScopedMixin, Base):
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


class ReturnItem(StoreScopedMixin, Base):
    """A seller return item reserved for return collection."""

    __tablename__ = "return_items"

    seller_return_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    order_item_id: Mapped[str | None] = mapped_column(String(100))
    offer_id: Mapped[str | None] = mapped_column(String(100))
    return_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    return_status: Mapped[str | None] = mapped_column(String(100))
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class DailyProductMetric(StoreScopedMixin, Base):
    """Precomputed daily product metrics for dashboard queries."""

    __tablename__ = "daily_product_metrics"
    __table_args__ = (
        UniqueConstraint(
            "store_code",
            "metric_date",
            "offer_id",
            name="uq_daily_product_metrics_store_date_offer",
        ),
    )

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
    conversion_percentage_previous_30_days: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    conversion_change_points: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    total_stock: Mapped[int | None] = mapped_column(Integer)
    offer_status: Mapped[str | None] = mapped_column(String(100))


class AnomalyEvent(StoreScopedMixin, Base):
    """A date-stamped anomaly classification for an offer."""

    __tablename__ = "anomaly_events"
    __table_args__ = (
        UniqueConstraint(
            "store_code",
            "event_date",
            "offer_id",
            "anomaly_type",
            name="uq_anomaly_events_store_date_offer_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    offer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    anomaly_type: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str | None] = mapped_column(String(30))
    explanation: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DataQualityEvent(StoreScopedMixin, Base):
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
    offer_group_plid: Mapped[str | None] = mapped_column(String(30), index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CompetitorTargetAudit(Base):
    """One user-initiated competitor target list change."""

    __tablename__ = "competitor_target_audits"

    id: Mapped[int] = mapped_column(primary_key=True)
    plid: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    old_url: Mapped[str | None] = mapped_column(Text)
    new_url: Mapped[str | None] = mapped_column(Text)
    actor_user_id: Mapped[int | None] = mapped_column(Integer)
    actor_username: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )


class CompetitorLinkHealth(Base):
    """Persistent validation state for a submitted competitor product link."""

    __tablename__ = "competitor_link_health"

    plid: Mapped[str] = mapped_column(String(30), primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="healthy", index=True)
    confirmed_not_found_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_not_found_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_evidence_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    control_plid: Mapped[str | None] = mapped_column(String(30))
    control_check_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_error: Mapped[str | None] = mapped_column(Text)


class OwnStoreFollowerTracking(Base):
    """Automatic public-page check state for one current own-store PLID."""

    __tablename__ = "own_store_follower_tracking"

    plid: Mapped[str] = mapped_column(String(30), primary_key=True)
    last_attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    last_succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str] = mapped_column(String(30), nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_message: Mapped[str | None] = mapped_column(Text)


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


class CompetitorVariantSnapshot(Base):
    """One variant's stock result attached to a product collection snapshot."""

    __tablename__ = "competitor_variant_snapshots"
    __table_args__ = (UniqueConstraint("snapshot_id", "variant_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    plid: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    variant_key: Mapped[str] = mapped_column(String(500), nullable=False)
    variant_label: Mapped[str] = mapped_column(Text, nullable=False)
    image_url: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    sku: Mapped[str | None] = mapped_column(String(100))
    seller_id: Mapped[str | None] = mapped_column(String(100))
    seller_name: Mapped[str | None] = mapped_column(String(255))
    price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    stock_status: Mapped[str | None] = mapped_column(String(100))
    is_leadtime: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stock_quantity: Mapped[int | None] = mapped_column(Integer)
    stock_exact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stock_method: Mapped[str] = mapped_column(String(100), nullable=False)
    stock_note: Mapped[str | None] = mapped_column(Text)
    customer_purchase_limit: Mapped[int | None] = mapped_column(Integer)


class ErpStore(Base):
    """One store identity available for account-level access assignment."""

    __tablename__ = "erp_stores"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
    )
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    data_connected: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ErpUser(Base):
    """A local ERP user with a permission template and optional overrides."""

    __tablename__ = "erp_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    permissions_json: Mapped[str | None] = mapped_column(Text)
    store_access_all: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class ErpUserStore(Base):
    """One explicit store assignment for an account with limited scope."""

    __tablename__ = "erp_user_stores"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("erp_users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    store_id: Mapped[int] = mapped_column(
        ForeignKey("erp_stores.id", ondelete="CASCADE"),
        primary_key=True,
    )


class CompetitorPersonalWatchlist(Base):
    """One competitor saved to one ERP account's personal watchlist."""

    __tablename__ = "competitor_personal_watchlist"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("erp_users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    plid: Mapped[str] = mapped_column(
        ForeignKey("competitor_targets.plid", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class OwnStorePersonalWatchlist(Base):
    """One connected-store product saved to one account's personal watchlist."""

    __tablename__ = "own_store_personal_watchlist"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("erp_users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    plid: Mapped[str] = mapped_column(String(30), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class PersonalWatchlistLibrary(Base):
    """One account-defined type library for organizing personal watchlist cards."""

    __tablename__ = "personal_watchlist_libraries"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_watchlist_library_user_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("erp_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class PersonalWatchlistLibraryItem(Base):
    """Assign one personal watchlist PLID to one account-owned type library."""

    __tablename__ = "personal_watchlist_library_items"

    library_id: Mapped[int] = mapped_column(
        ForeignKey("personal_watchlist_libraries.id", ondelete="CASCADE"),
        primary_key=True,
    )
    plid: Mapped[str] = mapped_column(String(30), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class PersonalWatchlistPreference(Base):
    """Persist whether an account selected a default type library or no library."""

    __tablename__ = "personal_watchlist_preferences"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("erp_users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    default_configured: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    default_library_id: Mapped[int | None] = mapped_column(
        ForeignKey("personal_watchlist_libraries.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class ErpSession(Base):
    """A revocable server-side ERP browser session."""

    __tablename__ = "erp_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("erp_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class LogisticsShipmentLink(Base):
    """One operator-confirmed relationship between W8 inbound and Takealot shipment."""

    __tablename__ = "logistics_shipment_links"
    __table_args__ = (
        UniqueConstraint(
            "store_id",
            "w8_order_no",
            "takealot_shipment_id",
            name="uq_logistics_shipment_link_pair",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("erp_stores.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    w8_order_no: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    takealot_shipment_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    confirmed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("erp_users.id", ondelete="SET NULL"),
        index=True,
    )
    confirmed_by_username: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("erp_users.id", ondelete="SET NULL"),
    )
    revoked_by_username: Mapped[str | None] = mapped_column(String(64))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoke_note: Mapped[str | None] = mapped_column(Text)


class LogisticsShipmentLinkAudit(Base):
    """Append-only confirmation and revocation history for a logistics link."""

    __tablename__ = "logistics_shipment_link_audits"

    id: Mapped[int] = mapped_column(primary_key=True)
    link_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_shipment_links.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("erp_users.id", ondelete="SET NULL"),
    )
    actor_username: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class LogisticsProviderSnapshot(StoreScopedMixin, Base):
    """Latest successful provider payload retained for offline logistics reads."""

    __tablename__ = "logistics_provider_snapshots"

    store_code: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=current_store_code,
        server_default="current",
    )
    provider: Mapped[str] = mapped_column(String(30), primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class PlatformWarehouseDraft(StoreScopedMixin, Base):
    """One audited platform-warehouse draft and its upstream task state."""

    __tablename__ = "platform_warehouse_drafts"
    __table_args__ = (
        UniqueConstraint(
            "store_code",
            "draft_number",
            name="uq_platform_warehouse_draft_store_number",
        ),
        UniqueConstraint(
            "store_code",
            "client_request_id",
            name="uq_platform_warehouse_draft_store_request",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    client_request_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    upstream_mode: Mapped[str] = mapped_column(
        String(30), nullable=False, default="local_only"
    )
    po_number: Mapped[str | None] = mapped_column(String(80), index=True)
    platform_shipment_id: Mapped[int | None] = mapped_column(Integer, index=True)
    tracking_reference: Mapped[str | None] = mapped_column(String(200))
    review_task_id: Mapped[int | None] = mapped_column(Integer, index=True)
    review_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    review_payload_hash: Mapped[str | None] = mapped_column(String(64))
    review_approval_hash: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    create_task_id: Mapped[int | None] = mapped_column(Integer, index=True)
    upstream_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_error: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("erp_users.id", ondelete="SET NULL"),
        index=True,
    )
    created_by_username: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    po_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)


class PlatformWarehouseDraftLine(StoreScopedMixin, Base):
    """A product line frozen into a local platform-warehouse draft."""

    __tablename__ = "platform_warehouse_draft_lines"
    __table_args__ = (
        UniqueConstraint(
            "draft_id",
            "offer_id",
            name="uq_platform_warehouse_draft_line_offer",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("platform_warehouse_drafts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    offer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    sku: Mapped[str | None] = mapped_column(String(255))
    tsin_id: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    cpt_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    jhb_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dbn_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PlatformWarehouseDraftAudit(StoreScopedMixin, Base):
    """Append-only operator audit for one local platform-warehouse draft."""

    __tablename__ = "platform_warehouse_draft_audits"

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("platform_warehouse_drafts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("erp_users.id", ondelete="SET NULL"),
    )
    actor_username: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class PlatformWarehouseShipment(StoreScopedMixin, Base):
    """One Takealot shipment created from an audited platform-warehouse draft."""

    __tablename__ = "platform_warehouse_shipments"
    __table_args__ = (
        UniqueConstraint(
            "store_code",
            "platform_shipment_id",
            name="uq_platform_warehouse_shipment_store_platform_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("platform_warehouse_drafts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    platform_shipment_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    region: Mapped[str | None] = mapped_column(String(20), index=True)
    facility_code: Mapped[str | None] = mapped_column(String(50), index=True)
    facility_id: Mapped[int | None] = mapped_column(Integer)
    reference: Mapped[str | None] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    po_number: Mapped[str | None] = mapped_column(String(80), index=True)
    tracking_reference: Mapped[str | None] = mapped_column(String(200))
    last_task_id: Mapped[int | None] = mapped_column(Integer, index=True)
    raw_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    po_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)


class ErpRefreshState(StoreScopedMixin, Base):
    """Persistent per-store cooldown state for the ERP full-refresh action."""

    __tablename__ = "erp_refresh_state"

    store_code: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=current_store_code,
        server_default="current",
    )
    action_key: Mapped[str] = mapped_column(String(50), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_by: Mapped[str | None] = mapped_column(String(64))
    last_success_display_name: Mapped[str | None] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class DailyReportRun(StoreScopedMixin, Base):
    """One immutable scheduled or manual capture used by the operations daily report."""

    __tablename__ = "daily_report_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    business_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    slot: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    counts: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class DailyInventorySnapshot(StoreScopedMixin, Base):
    """Preferred next-day inventory, with delayed same-day recovery when needed."""

    __tablename__ = "daily_inventory_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "store_code",
            "inventory_date",
            "offer_id",
            name="uq_daily_inventory_store_date_offer",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    inventory_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    offer_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("daily_report_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    platform_stock: Mapped[int | None] = mapped_column(Integer)
    stock_source: Mapped[str | None] = mapped_column(String(50))


class DailyReportObservation(StoreScopedMixin, Base):
    """One product value set frozen at a daily-report capture time."""

    __tablename__ = "daily_report_observations"
    __table_args__ = (
        UniqueConstraint(
            "store_code",
            "run_id",
            "offer_id",
            name="uq_daily_report_observation_store_run_offer",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("daily_report_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    offer_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    sku: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(Text)
    page_views_30_days: Mapped[int | None] = mapped_column(Integer)
    ordered_units: Mapped[int] = mapped_column(Integer, nullable=False)
    platform_stock: Mapped[int | None] = mapped_column(Integer)
    stock_source: Mapped[str | None] = mapped_column(String(50))


class DailyReportResolution(StoreScopedMixin, Base):
    """Human candidate and confirmed final values for one product and business day."""

    __tablename__ = "daily_report_resolutions"
    __table_args__ = (
        UniqueConstraint(
            "store_code",
            "business_date",
            "offer_id",
            name="uq_daily_report_resolution_store_date_offer",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    business_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    offer_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    manual_page_views_30_days: Mapped[int | None] = mapped_column(Integer)
    manual_ordered_units: Mapped[int | None] = mapped_column(Integer)
    manual_platform_stock: Mapped[int | None] = mapped_column(Integer)
    manual_reason: Mapped[str | None] = mapped_column(String(50))
    manual_note: Mapped[str | None] = mapped_column(Text)
    manual_by: Mapped[int | None] = mapped_column(ForeignKey("erp_users.id"))
    manual_at: Mapped[datetime | None] = mapped_column(DateTime)
    selected_source: Mapped[str | None] = mapped_column(String(20))
    final_page_views_30_days: Mapped[int | None] = mapped_column(Integer)
    final_ordered_units: Mapped[int | None] = mapped_column(Integer)
    final_platform_stock: Mapped[int | None] = mapped_column(Integer)
    confirm_note: Mapped[str | None] = mapped_column(Text)
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("erp_users.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    stock_alert_dismissed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stock_alert_note: Mapped[str | None] = mapped_column(Text)
    stock_alert_dismissed_by: Mapped[int | None] = mapped_column(ForeignKey("erp_users.id"))
    stock_alert_dismissed_at: Mapped[datetime | None] = mapped_column(DateTime)
    operator_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class DailyReportAudit(StoreScopedMixin, Base):
    """Append-only audit trail for report edits, confirmations, and alert handling."""

    __tablename__ = "daily_report_audits"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    offer_id: Mapped[str | None] = mapped_column(String(100), index=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("erp_users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class DailyReportDeadlineSnapshot(StoreScopedMixin, Base):
    """Persistent unresolved-work snapshot made at the daily confirmation deadline."""

    __tablename__ = "daily_report_deadline_snapshots"

    store_code: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=current_store_code,
        server_default="current",
    )
    business_date: Mapped[date] = mapped_column(Date, primary_key=True)
    snapped_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    unresolved_count: Mapped[int] = mapped_column(Integer, nullable=False)
    details: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
