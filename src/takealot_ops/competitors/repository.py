"""Persistence operations for competitor targets, snapshots, and reviews."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from takealot_ops.competitors.domain import (
    CompetitorOffer,
    CompetitorProduct,
    CompetitorReviewRecord,
    OfferStockObservation,
    PreviousObservation,
    ReviewSummary,
    SalesSignal,
    StockProbeResult,
    VariantStockObservation,
    competitor_offer_stock_state,
)
from takealot_ops.storage.models import (
    CompetitorLinkHealth,
    CompetitorReview,
    CompetitorSnapshot,
    CompetitorTarget,
    CompetitorVariantSnapshot,
)

NOT_FOUND_CONFIRMATION_COUNT = 3
NOT_FOUND_CONFIRMATION_INTERVAL = timedelta(minutes=10)


@dataclass(frozen=True)
class LinkHealthDecision:
    """Result of persisting one cross-checked 404 observation."""

    status: str
    confirmed_not_found_count: int
    evidence_counted: bool


def _normalized_offer_scope(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _matching_buybox_stock(
    offer: CompetitorOffer,
    observations: list[VariantStockObservation],
) -> StockProbeResult | None:
    """Bind a cart probe only to the buybox variant that was actually tested."""

    if not offer.is_buybox:
        return None
    for observation in observations:
        variant = observation.variant
        if _normalized_offer_scope(variant.key) != _normalized_offer_scope(
            offer.variant_key
        ):
            continue
        if _normalized_offer_scope(variant.sku) != _normalized_offer_scope(offer.sku):
            continue
        offer_seller_id = _normalized_offer_scope(offer.seller_id)
        variant_seller_id = _normalized_offer_scope(variant.seller_id)
        if offer_seller_id and variant_seller_id:
            if offer_seller_id != variant_seller_id:
                continue
        elif _normalized_offer_scope(variant.seller_name) != _normalized_offer_scope(
            offer.seller_name
        ):
            continue
        return observation.stock
    return None


def _matching_offer_stock(
    offer: CompetitorOffer,
    observations: list[OfferStockObservation],
) -> StockProbeResult | None:
    """Bind only a probe collected from this exact follower-offer record."""

    for observation in observations:
        observed_offer = observation.offer
        if observed_offer == offer:
            return observation.stock
        if offer.identity_key and observed_offer.identity_key:
            if _normalized_offer_scope(offer.identity_key) == _normalized_offer_scope(
                observed_offer.identity_key
            ):
                return observation.stock
    return None


def _offer_stock_payload(
    offer: CompetitorOffer,
    variant_observations: list[VariantStockObservation],
    offer_observations: list[OfferStockObservation],
) -> dict[str, object]:
    probe = (
        _matching_buybox_stock(offer, variant_observations)
        if offer.is_buybox
        else _matching_offer_stock(offer, offer_observations)
    )
    if probe is None:
        return {
            "stock_state": competitor_offer_stock_state(
                offer.stock_status,
                is_leadtime=offer.is_leadtime,
                is_add_to_cart_available=offer.is_add_to_cart_available,
            ),
            "stock_quantity": None,
            "stock_exact": False,
            "stock_method": "public-offer-status",
            "stock_note": (
                "公开报价只返回库存状态，未提供可核验数量；数量保留缺失，不补 0。"
            ),
        }
    return {
        "stock_state": competitor_offer_stock_state(
            offer.stock_status,
            is_leadtime=offer.is_leadtime,
            is_add_to_cart_available=offer.is_add_to_cart_available,
            exact_quantity=(
                probe.quantity
                if probe.exact or (probe.quantity is not None and probe.quantity > 0)
                else None
            ),
        ),
        "stock_quantity": probe.quantity,
        "stock_exact": probe.exact,
        "stock_method": probe.method,
        "stock_note": probe.note,
    }


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
            (variant.key, variant.sku, variant.seller_id) for variant in product.variants
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

    def latest_control_product(
        self,
        *,
        exclude_plid: str,
    ) -> tuple[str, str] | None:
        """Return the most recently collected different product as a control."""
        row = self._session.execute(
            select(CompetitorTarget.plid, CompetitorTarget.url)
            .join(
                CompetitorSnapshot,
                CompetitorSnapshot.plid == CompetitorTarget.plid,
            )
            .where(
                CompetitorTarget.active.is_(True),
                CompetitorTarget.plid != exclude_plid,
            )
            .order_by(CompetitorSnapshot.collected_at.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        return str(row.plid), str(row.url)

    def is_confirmed_invalid(self, plid: str) -> bool:
        """Return whether prior durable evidence already confirmed this link."""
        row = self._session.get(CompetitorLinkHealth, plid)
        return row is not None and row.status == "confirmed_invalid"

    def record_not_found(
        self,
        *,
        plid: str,
        url: str,
        checked_at: datetime,
        control_plid: str | None,
        control_check_ok: bool,
    ) -> LinkHealthDecision:
        """Persist one 404 without confirming invalidity too quickly."""
        now = checked_at.astimezone(UTC)
        row = self._session.get(CompetitorLinkHealth, plid)
        was_confirmed = row is not None and row.status == "confirmed_invalid"
        if row is None:
            row = CompetitorLinkHealth(
                plid=plid,
                url=url,
                status="suspected_invalid",
                confirmed_not_found_count=0,
                first_not_found_at=now,
                last_evidence_at=None,
                last_checked_at=now,
                last_success_at=None,
                control_plid=control_plid,
                control_check_ok=control_check_ok,
                last_error="Takealot 商品数据返回 404",
            )
            self._session.add(row)

        evidence_counted = False
        last_evidence = _as_utc(row.last_evidence_at)
        if control_check_ok and (
            last_evidence is None or now - last_evidence >= NOT_FOUND_CONFIRMATION_INTERVAL
        ):
            row.confirmed_not_found_count += 1
            row.last_evidence_at = now
            evidence_counted = True

        row.url = url
        row.last_checked_at = now
        if control_check_ok or not was_confirmed:
            row.control_plid = control_plid
            row.control_check_ok = control_check_ok
        row.last_error = "Takealot 商品数据返回 404"
        if row.first_not_found_at is None:
            row.first_not_found_at = now
        row.status = (
            "confirmed_invalid"
            if row.confirmed_not_found_count >= NOT_FOUND_CONFIRMATION_COUNT
            else "suspected_invalid"
        )
        self._session.flush()
        return LinkHealthDecision(
            status=row.status,
            confirmed_not_found_count=row.confirmed_not_found_count,
            evidence_counted=evidence_counted,
        )

    def mark_link_healthy(
        self,
        *,
        plid: str,
        url: str,
        checked_at: datetime,
    ) -> None:
        """Reset any previous 404 suspicion after a successful collection."""
        now = checked_at.astimezone(UTC)
        row = self._session.get(CompetitorLinkHealth, plid)
        if row is None:
            row = CompetitorLinkHealth(
                plid=plid,
                url=url,
                status="healthy",
                confirmed_not_found_count=0,
                first_not_found_at=None,
                last_evidence_at=None,
                last_checked_at=now,
                last_success_at=now,
                control_plid=None,
                control_check_ok=None,
                last_error=None,
            )
            self._session.add(row)
            return
        row.url = url
        row.status = "healthy"
        row.confirmed_not_found_count = 0
        row.first_not_found_at = None
        row.last_evidence_at = None
        row.last_checked_at = now
        row.last_success_at = now
        row.control_plid = None
        row.control_check_ok = None
        row.last_error = None

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
        offer_stocks: list[OfferStockObservation] | None = None,
        register_target: bool = True,
    ) -> CompetitorSnapshot:
        now = collected_at.astimezone(UTC)
        if register_target:
            target = self._session.get(CompetitorTarget, product.plid)
            if target is None:
                target = CompetitorTarget(
                    plid=product.plid,
                    offer_group_plid=product.plid,
                    url=product.url,
                    title=product.title,
                    active=True,
                    created_at=now,
                    updated_at=now,
                )
                self._session.add(target)
            else:
                if not target.offer_group_plid:
                    target.offer_group_plid = target.plid
                target.url = product.url
                target.title = product.title
                target.active = True
                target.updated_at = now
            self.mark_link_healthy(
                plid=product.plid,
                url=product.url,
                checked_at=now,
            )

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
                    "is_buybox": offer.is_buybox,
                    "is_leadtime": offer.is_leadtime,
                    "is_add_to_cart_available": offer.is_add_to_cart_available,
                    "plid": offer.plid,
                    "url": offer.url,
                    "offer_id": offer.offer_id,
                    "condition": offer.condition,
                    "variant_key": offer.variant_key,
                    "variant_label": offer.variant_label,
                    "identity_key": offer.identity_key,
                    "buybox_rank": offer.buybox_rank,
                    "is_follower_offer": offer.is_follower_offer,
                    **_offer_stock_payload(offer, variant_stocks, offer_stocks or []),
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
                    image_url=variant.image_url,
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
                    customer_purchase_limit=variant_stock.customer_purchase_limit,
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


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
