"""Official own-store daily sales history for competitor detail views."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from takealot_ops.domain import SAST
from takealot_ops.storage.models import (
    DailySalesMetricState,
    ErpStore,
    OfferCurrent,
    OfferSnapshot,
    SaleItem,
    StoreOfferBaseline,
    StoreOfferObservation,
)
from takealot_ops.storage.store_context import normalize_store_code, store_scope


CHINA = ZoneInfo("Asia/Shanghai")


def build_own_store_sales_series(
    session: Session,
    *,
    plid: str,
    store_codes: set[str],
    through: date,
) -> list[dict[str, Any]]:
    """Return one Beijing daily ordered-unit series per visible connected store.

    Orders are bucketed into Beijing calendar days. A zero is published only
    when ``DailySalesMetricState`` proves all SAST source dates intersecting
    that Beijing day came from Seller Sales ``/sales`` batches collected after
    the Beijing day ended. Earlier successful pulls remain explicitly partial.
    """
    normalized_plid = str(plid or "").strip()
    normalized_codes = sorted(
        {
            normalize_store_code(store_code)
            for store_code in store_codes
            if str(store_code or "").strip()
        }
    )
    if not normalized_plid or not normalized_codes:
        return []

    store_names = {
        str(store.code): str(store.display_name)
        for store in session.scalars(
            select(ErpStore).where(ErpStore.code.in_(normalized_codes))
        )
    }
    result: list[dict[str, Any]] = []
    for store_code in normalized_codes:
        with store_scope(store_code):
            series = _store_sales_series(
                session,
                plid=normalized_plid,
                store_code=store_code,
                store_name=store_names.get(store_code, store_code),
                through=through,
            )
        if series is not None:
            result.append(series)
    return result


def _store_sales_series(
    session: Session,
    *,
    plid: str,
    store_code: str,
    store_name: str,
    through: date,
) -> dict[str, Any] | None:
    current_offers = list(
        session.scalars(
            select(OfferCurrent)
            .where(OfferCurrent.productline_id == plid)
            .order_by(OfferCurrent.offer_id)
        )
    )
    # Only a currently connected own Offer makes this PLID an own-store link
    # for the selected store. Historical rows alone must not expose a series.
    if not current_offers:
        return None

    snapshots = list(
        session.scalars(
            select(OfferSnapshot)
            .where(OfferSnapshot.productline_id == plid)
            .order_by(OfferSnapshot.snapshot_date, OfferSnapshot.offer_id)
        )
    )
    baselines = list(
        session.scalars(
            select(StoreOfferBaseline)
            .where(StoreOfferBaseline.productline_id == plid)
            .order_by(StoreOfferBaseline.display_date, StoreOfferBaseline.offer_id)
        )
    )
    observations = list(
        session.scalars(
            select(StoreOfferObservation)
            .where(StoreOfferObservation.productline_id == plid)
            .order_by(
                StoreOfferObservation.display_date,
                StoreOfferObservation.offer_id,
            )
        )
    )

    offer_ids = sorted(
        {str(row.offer_id).strip() for row in current_offers if row.offer_id}
        | {str(row.offer_id).strip() for row in snapshots if row.offer_id}
        | {str(row.offer_id).strip() for row in baselines if row.offer_id}
        | {str(row.offer_id).strip() for row in observations if row.offer_id}
    )
    skus = sorted(
        {str(row.sku).strip() for row in current_offers if row.sku}
        | {str(row.sku).strip() for row in snapshots if row.sku}
        | {str(row.sku).strip() for row in baselines if row.sku}
        | {str(row.sku).strip() for row in observations if row.sku}
    )
    sale_rows = list(
        session.execute(
            select(
                SaleItem.order_date,
                SaleItem.quantity,
            )
            .where(
                SaleItem.offer_id.in_(offer_ids),
            )
            .order_by(SaleItem.order_date, SaleItem.order_item_id)
        ).all()
    )
    units_by_date: dict[date, int] = {}
    for order_date, quantity in sale_rows:
        sales_date = _china_day(order_date)
        if sales_date is None or sales_date > through:
            continue
        units_by_date[sales_date] = units_by_date.get(sales_date, 0) + int(quantity or 0)

    platform_dates = [
        listed_day
        for row in current_offers
        if (listed_day := _china_day(row.created_at)) is not None
    ] + [
        listed_day
        for row in snapshots
        if (listed_day := _china_day(row.created_at)) is not None
    ]
    observed_dates = [
        *(
            observed_day
            for row in current_offers
            if (observed_day := _china_day(row.captured_at)) is not None
        ),
        *(
            observed_day
            for row in snapshots
            if (observed_day := _china_day(row.captured_at)) is not None
        ),
        *(row.display_date for row in baselines),
        *(row.display_date for row in observations),
        *(units_by_date),
    ]
    if platform_dates:
        listing_date = min(platform_dates)
        listing_date_source = "platform"
    elif observed_dates:
        listing_date = min(observed_dates)
        listing_date_source = "first_observed"
    else:
        return None

    required_source_dates = {
        source_date
        for display_date in _date_range(listing_date, through)
        for source_date in _sast_dates_for_china_day(display_date)
    }
    states = (
        {
            state.metric_date: state
            for state in session.scalars(
                select(DailySalesMetricState).where(
                    DailySalesMetricState.metric_date.in_(required_source_dates)
                )
            )
        }
        if required_source_dates
        else {}
    )
    points: list[dict[str, Any]] = []
    covered_dates: list[date] = []
    partial_dates: list[date] = []
    total_ordered_units = 0
    for metric_date in _date_range(listing_date, through):
        source_states = [
            states.get(source_date)
            for source_date in _sast_dates_for_china_day(metric_date)
        ]
        source_verified = all(
            state is not None and _state_is_sales_api_verified(state)
            for state in source_states
        )
        fully_verified = source_verified and all(
            state is not None
            and (verified_at := _state_verified_at(state)) is not None
            and verified_at >= _china_day_end_utc(metric_date)
            for state in source_states
        )
        data_status = (
            "verified"
            if fully_verified
            else "partial"
            if source_verified
            else "missing"
        )
        ordered_units = (
            units_by_date.get(metric_date, 0) if data_status != "missing" else None
        )
        if fully_verified:
            covered_dates.append(metric_date)
        elif data_status == "partial":
            partial_dates.append(metric_date)
        if ordered_units is not None:
            total_ordered_units += int(ordered_units or 0)
        points.append(
            {
                "date": metric_date.isoformat(),
                "ordered_units": ordered_units,
                "data_status": data_status,
                "revision_count": sum(
                    int(state.revision_count or 0)
                    for state in source_states
                    if state is not None
                ),
            }
        )

    return {
        "store_code": store_code,
        "store_name": store_name,
        "plid": plid,
        "offer_ids": offer_ids,
        "skus": skus,
        "listing_date": listing_date.isoformat(),
        "listing_date_source": listing_date_source,
        "through_date": through.isoformat(),
        "date_basis": "Asia/Shanghai",
        "source_date_basis": "Africa/Johannesburg",
        "total_ordered_units": (
            total_ordered_units if covered_dates or partial_dates else None
        ),
        "covered_days": len(covered_dates),
        "partial_days": len(partial_dates),
        "missing_days": max(
            0,
            (through - listing_date).days
            + 1
            - len(covered_dates)
            - len(partial_dates),
        ),
        "coverage_start": covered_dates[0].isoformat() if covered_dates else None,
        "coverage_end": covered_dates[-1].isoformat() if covered_dates else None,
        "points": points,
    }


def _china_day(value: datetime | None) -> date | None:
    if value is None:
        return None
    normalized = value
    if normalized.tzinfo is None or normalized.utcoffset() is None:
        normalized = normalized.replace(tzinfo=UTC)
    return normalized.astimezone(CHINA).date()


def _sast_dates_for_china_day(display_date: date) -> tuple[date, ...]:
    start = datetime.combine(display_date, time.min, tzinfo=CHINA).astimezone(SAST)
    end = (
        datetime.combine(display_date + timedelta(days=1), time.min, tzinfo=CHINA)
        - timedelta(microseconds=1)
    ).astimezone(SAST)
    if start.date() == end.date():
        return (start.date(),)
    return (start.date(), end.date())


def _state_is_sales_api_verified(state: DailySalesMetricState) -> bool:
    if state.source_kind != "takealot_sales_api":
        return False
    return _state_verified_at(state) is not None


def _state_verified_at(state: DailySalesMetricState) -> datetime | None:
    raw_value: object = state.verified_at
    if raw_value is None:
        source = state.source_details if isinstance(state.source_details, dict) else {}
        raw_value = source.get("verified_at") or source.get("collected_at")
    if isinstance(raw_value, datetime):
        parsed = raw_value
    elif isinstance(raw_value, str):
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _china_day_end_utc(display_date: date) -> datetime:
    return datetime.combine(
        display_date + timedelta(days=1),
        time.min,
        tzinfo=CHINA,
    ).astimezone(UTC)


def _date_range(start: date, end: date) -> list[date]:
    if start > end:
        return []
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
