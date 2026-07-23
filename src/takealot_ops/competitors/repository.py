"""Persistence operations for competitor targets, snapshots, and reviews."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from takealot_ops.competitors.domain import (
    CompetitorProduct,
    CompetitorReviewRecord,
    PreviousObservation,
    ReviewSummary,
    SalesSignal,
    StockProbeResult,
    VariantStockObservation,
)
from takealot_ops.storage.models import (
    CompetitorReview,
    CompetitorSnapshot,
    CompetitorTarget,
    CompetitorVariantSnapshot,
)


class CompetitorRepository:
    """Session-scoped competitor persistence with idempotent review updates."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def latest_compatible_snapshot(
        self,
        product: CompetitorProduct,
    ) -> PreviousObservation | None:
        candidates = self._session.scalars(
            select(CompetitorSnapshot)
            .where(CompetitorSnapshot.plid == product.plid)
            .order_by(CompetitorSnapshot.collected_at.desc())
        )
        current_scope = {
            (variant.key, variant.sku, variant.seller_id)
            for variant in product.variants
        }
        row: CompetitorSnapshot | None = None
        for candidate in candidates:
            prior_scope = {
                (variant.variant_key, variant.sku or "", variant.seller_id or "")
                for variant in self._session.scalars(
                    select(CompetitorVariantSnapshot).where(
                        CompetitorVariantSnapshot.snapshot_id == candidate.id
                    )
                )
            }
            if prior_scope == current_scope:
                row = candidate
                break
        if row is None:
            return None
        return PreviousObservation(
            snapshot_id=row.id,
            collected_at=row.collected_at,
            stock_quantity=row.stock_quantity,
            stock_exact=row.stock_exact,
            review_count=row.review_count,
        )

    def save_observation(
        self,
        *,
        product: CompetitorProduct,
        reviews: list[CompetitorReviewRecord],
        review_summary: ReviewSummary,
        stock: StockProbeResult,
        variant_stocks: list[VariantStockObservation],
        lifetime_sales: tuple[int, int],
        signal: SalesSignal,
        collected_at: datetime,
    ) -> CompetitorSnapshot:
        now = collected_at.astimezone(UTC)
        target = self._session.get(CompetitorTarget, product.plid)
        if target is None:
            target = CompetitorTarget(
                plid=product.plid,
                url=product.url,
                title=product.title,
                active=True,
                created_at=now,
                updated_at=now,
            )
            self._session.add(target)
        else:
            target.url = product.url
            target.title = product.title
            target.active = True
            target.updated_at = now

        snapshot = CompetitorSnapshot(
            plid=product.plid,
            collected_at=now,
            url=product.url,
            title=product.title,
            image_url=product.image_url,
            sku=product.sku,
            seller_id=product.seller_id,
            seller_name=product.seller_name,
            price=Decimal(str(product.price)),
            stock_status=product.stock_status,
            stock_quantity=stock.quantity,
            stock_exact=stock.exact,
            stock_method=stock.method,
            stock_note=stock.note,
            review_count=product.review_count,
            fetched_review_count=review_summary.total,
            rating=Decimal(str(product.rating)),
            positive_reviews=review_summary.positive,
            neutral_reviews=review_summary.neutral,
            negative_reviews=review_summary.negative,
            lifetime_sales_min=lifetime_sales[0],
            lifetime_sales_max=lifetime_sales[1],
            previous_snapshot_id=signal.previous_snapshot_id,
            observed_stock_outflow=signal.observed_stock_outflow,
            review_delta=signal.review_delta,
            period_sales_min=signal.period_sales_min,
            period_sales_max=signal.period_sales_max,
            trend_label=signal.trend_label,
            trend_note=signal.trend_note,
            offers=[
                {
                    "selected": offer.selected,
                    "sku": offer.sku,
                    "seller_id": offer.seller_id,
                    "seller_name": offer.seller_name,
                    "price": offer.price,
                    "stock_status": offer.stock_status,
                }
                for offer in product.offers
            ],
        )
        self._session.add(snapshot)
        self._session.flush()
        for observation in variant_stocks:
            variant = observation.variant
            variant_stock = observation.stock
            self._session.add(
                CompetitorVariantSnapshot(
                    snapshot_id=snapshot.id,
                    plid=product.plid,
                    collected_at=now,
                    variant_key=variant.key,
                    variant_label=variant.label,
                    url=variant.url,
                    sku=variant.sku,
                    seller_id=variant.seller_id,
                    seller_name=variant.seller_name,
                    price=Decimal(str(variant.price)),
                    stock_status=variant.stock_status,
                    is_leadtime=variant.is_leadtime,
                    stock_quantity=variant_stock.quantity,
                    stock_exact=variant_stock.exact,
                    stock_method=variant_stock.method,
                    stock_note=variant_stock.note,
                )
            )
        self._upsert_reviews(product.plid, reviews, now)
        self._session.flush()
        return snapshot

    def _upsert_reviews(
        self,
        plid: str,
        reviews: list[CompetitorReviewRecord],
        seen_at: datetime,
    ) -> None:
        existing = {
            row.review_id: row
            for row in self._session.scalars(
                select(CompetitorReview).where(CompetitorReview.plid == plid)
            )
        }
        for review in reviews:
            row = existing.get(review.review_id)
            if row is None:
                self._session.add(
                    CompetitorReview(
                        plid=plid,
                        review_id=review.review_id,
                        rating=review.rating,
                        title=review.title,
                        body=review.body,
                        customer_name=review.customer_name,
                        review_date=review.review_date,
                        first_seen_at=seen_at,
                        last_seen_at=seen_at,
                    )
                )
                continue
            row.rating = review.rating
            row.title = review.title
            row.body = review.body
            row.customer_name = review.customer_name
            row.review_date = review.review_date
            row.last_seen_at = seen_at
