"""Typed records and conservative rules for competitor observations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime


REVIEW_RATE_LOW = 0.02
REVIEW_RATE_HIGH = 0.05


@dataclass(frozen=True)
class CompetitorOffer:
    """A compact public offer shown on the product page."""

    selected: bool
    sku: str
    seller_id: str
    seller_name: str
    price: float
    stock_status: str
    plid: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class CompetitorVariant:
    """One purchasable selector combination under a product PLID."""

    key: str
    label: str
    url: str
    title: str
    sku: str
    seller_id: str
    seller_name: str
    price: float
    stock_status: str
    is_leadtime: bool
    is_add_to_cart_available: bool
    image_url: str | None = None


@dataclass(frozen=True)
class CompetitorProduct:
    """Public product fields required by the MVP."""

    plid: str
    url: str
    title: str
    image_url: str | None
    sku: str
    seller_id: str
    seller_name: str
    price: float
    stock_status: str
    is_leadtime: bool
    review_count: int
    rating: float
    offers: tuple[CompetitorOffer, ...]
    variants: tuple[CompetitorVariant, ...]


@dataclass(frozen=True)
class CompetitorReviewRecord:
    """One public product review."""

    review_id: str
    rating: int
    title: str
    body: str
    customer_name: str
    review_date: str


@dataclass(frozen=True)
class ReviewSummary:
    """Fixed 4–5 / 3 / 1–2 star grouping."""

    total: int
    positive: int
    neutral: int
    negative: int


@dataclass(frozen=True)
class StockProbeResult:
    """Anonymous-cart availability result with explicit scope."""

    quantity: int | None
    exact: bool
    method: str
    note: str
    customer_purchase_limit: int | None = None


@dataclass(frozen=True)
class VariantStockObservation:
    """One variant and the platform-warehouse stock result collected for it."""

    variant: CompetitorVariant
    stock: StockProbeResult


@dataclass(frozen=True)
class PreviousObservation:
    """Comparable fields read from the latest prior snapshot."""

    snapshot_id: int
    collected_at: datetime
    stock_quantity: int | None
    stock_exact: bool
    review_count: int


@dataclass(frozen=True)
class SalesSignal:
    """Bounded period signal derived only from comparable quantity evidence."""

    previous_snapshot_id: int | None
    observed_stock_outflow: int | None
    review_delta: int | None
    period_sales_min: int | None
    period_sales_max: int | None
    trend_label: str
    trend_note: str


def summarize_reviews(reviews: list[CompetitorReviewRecord]) -> ReviewSummary:
    """Apply the project-wide fixed sentiment buckets."""
    positive = sum(review.rating >= 4 for review in reviews)
    neutral = sum(review.rating == 3 for review in reviews)
    negative = sum(review.rating <= 2 for review in reviews)
    return ReviewSummary(
        total=len(reviews),
        positive=positive,
        neutral=neutral,
        negative=negative,
    )


def estimate_lifetime_sales(review_count: int) -> tuple[int, int]:
    """Estimate only an order-of-magnitude range using a disclosed 2%–5% review rate."""
    count = max(0, int(review_count))
    if count == 0:
        return 0, 0
    return math.ceil(count / REVIEW_RATE_HIGH), math.ceil(count / REVIEW_RATE_LOW)


def analyze_sales_signal(
    previous: PreviousObservation | None,
    *,
    current_stock_quantity: int | None,
    current_stock_exact: bool,
    current_review_count: int,
) -> SalesSignal:
    """Compare two compatible snapshots without claiming official sales."""
    if previous is None:
        return SalesSignal(
            previous_snapshot_id=None,
            observed_stock_outflow=None,
            review_delta=None,
            period_sales_min=None,
            period_sales_max=None,
            trend_label="待建立基线",
            trend_note="首次采集只能建立基线；后续在相近时段再次采集后才能观察变化。",
        )

    review_delta = max(0, current_review_count - previous.review_count)
    exact_stock_pair = (
        previous.stock_exact
        and current_stock_exact
        and previous.stock_quantity is not None
        and current_stock_quantity is not None
    )
    stock_change: int | None = None
    if exact_stock_pair:
        assert current_stock_quantity is not None
        assert previous.stock_quantity is not None
        stock_change = current_stock_quantity - previous.stock_quantity
    outflow = max(0, -stock_change) if stock_change is not None else None
    review_range = estimate_lifetime_sales(review_delta) if review_delta > 0 else None

    if stock_change is not None and stock_change > 0:
        label = "检测到补货"
        note = "库存上升表明期间可能补货，不能用首尾库存反推观察期销量。"
        period_min = review_range[0] if review_range else None
        period_max = review_range[1] if review_range else None
    elif outflow and review_delta > 0:
        label = "两个独立正向信号"
        note = (
            "同时观察到匿名购物车库存净流出和新增评论；仍可能受补货、购物车占用、"
            "评论延迟及跨卖家/变体评论影响。"
        )
        period_min = max(outflow, review_range[0] if review_range else 0)
        period_max = review_range[1] if review_range else outflow
    elif outflow:
        label = "库存净流出（待验证）"
        note = "仅有库存下降信号，可能包含购物车占用、取消和补货影响，不等于官方销量。"
        period_min = outflow
        period_max = outflow
    elif review_delta > 0 and review_range is not None:
        label = "新增评论（待验证）"
        note = "仅按新增评论和2%–5%假设评论率估算，评论可能延迟且跨卖家或变体。"
        period_min, period_max = review_range
    else:
        label = "暂未观察到净流出"
        note = "首尾库存和评论不变不代表没有销售；期间销售与补货可能相互抵消。"
        period_min = None
        period_max = None

    return SalesSignal(
        previous_snapshot_id=previous.snapshot_id,
        observed_stock_outflow=outflow,
        review_delta=review_delta,
        period_sales_min=period_min,
        period_sales_max=period_max,
        trend_label=label,
        trend_note=note,
    )
