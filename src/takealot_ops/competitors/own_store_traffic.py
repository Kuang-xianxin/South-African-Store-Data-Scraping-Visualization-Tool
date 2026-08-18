"""Own-store rolling traffic snapshots for competitor detail views."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from takealot_ops.erp.keyword_traffic import extract_title_keywords
from takealot_ops.storage.models import ErpStore, OfferCurrent, StoreOfferObservation
from takealot_ops.storage.store_context import normalize_store_code, store_scope

def build_own_store_traffic_series(
    session: Session,
    *,
    plid: str,
    store_codes: set[str],
    start_date: date | None,
    end_date: date | None,
) -> list[dict[str, Any]]:
    """Return one exact-refresh rolling-30-day view series per current Offer.

    The selected dates follow the competitor page's Beijing observation range.
    Values remain the Seller Offers ``page_views_30_days`` rolling metric; they
    are never converted into daily traffic or visitors.
    """
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期")
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
            current_offers = list(
                session.scalars(
                    select(OfferCurrent)
                    .where(OfferCurrent.productline_id == normalized_plid)
                    .order_by(OfferCurrent.offer_id)
                )
            )
            if not current_offers:
                continue
            current_offer_ids = {
                str(offer.offer_id).strip()
                for offer in current_offers
                if str(offer.offer_id or "").strip()
            }
            observations = list(
                session.scalars(
                    select(StoreOfferObservation)
                    .where(StoreOfferObservation.offer_id.in_(current_offer_ids))
                    .order_by(
                        StoreOfferObservation.offer_id,
                        StoreOfferObservation.captured_at,
                        StoreOfferObservation.id,
                    )
                )
            )

        observations_by_offer: dict[str, list[StoreOfferObservation]] = {}
        for observation in observations:
            observations_by_offer.setdefault(str(observation.offer_id), []).append(
                observation
            )
        for offer in current_offers:
            result.append(
                _offer_traffic_series(
                    store_code=store_code,
                    store_name=store_names.get(store_code, store_code),
                    plid=normalized_plid,
                    offer=offer,
                    observations=observations_by_offer.get(str(offer.offer_id), []),
                    start_date=start_date,
                    end_date=end_date,
                )
            )
    return result


def _offer_traffic_series(
    *,
    store_code: str,
    store_name: str,
    plid: str,
    offer: OfferCurrent,
    observations: Sequence[StoreOfferObservation],
    start_date: date | None,
    end_date: date | None,
) -> dict[str, Any]:
    ordered = sorted(
        observations,
        key=lambda row: (_captured_at_utc(row.captured_at), row.id),
    )
    title_change_by_observation_id: dict[int, tuple[bool, str | None]] = {}
    previous_title: str | None = None
    previous_signature: tuple[str, ...] | None = None
    for observation in ordered:
        title = _clean_title(observation.title)
        signature = _title_signature(title)
        changed = bool(
            signature
            and previous_signature is not None
            and signature != previous_signature
        )
        title_change_by_observation_id[observation.id] = (
            changed,
            previous_title if changed else None,
        )
        if signature:
            previous_signature = signature
            previous_title = title

    available_dates = sorted({row.display_date for row in ordered})
    selected_start = start_date or (available_dates[0] if available_dates else None)
    selected_end = end_date or (available_dates[-1] if available_dates else None)
    if selected_start is not None and selected_end is None:
        selected_end = selected_start
    if selected_end is not None and selected_start is None:
        selected_start = selected_end

    selected_observations = [
        row
        for row in ordered
        if (selected_start is None or row.display_date >= selected_start)
        and (selected_end is None or row.display_date <= selected_end)
    ]
    points: list[dict[str, Any]] = []
    for observation in selected_observations:
        title_changed, prior_title = title_change_by_observation_id.get(
            observation.id,
            (False, None),
        )
        traffic_recorded = bool(observation.page_views_30_days_recorded)
        points.append(
            {
                "date": observation.display_date.isoformat(),
                "captured_at": _captured_at_utc(observation.captured_at).isoformat(),
                "page_views_30_days": (
                    observation.page_views_30_days if traffic_recorded else None
                ),
                "title": _clean_title(observation.title),
                "title_changed": title_changed,
                "previous_title": prior_title,
                "data_status": "observed" if traffic_recorded else "missing",
            }
        )

    observed_count = sum(point["data_status"] == "observed" for point in points)
    traffic_count = sum(point["page_views_30_days"] is not None for point in points)
    return {
        "store_code": store_code,
        "store_name": store_name,
        "plid": plid,
        "offer_id": str(offer.offer_id),
        "sku": offer.sku,
        "range_start": selected_start.isoformat() if selected_start is not None else None,
        "range_end": selected_end.isoformat() if selected_end is not None else None,
        "observed_count": observed_count,
        "traffic_count": traffic_count,
        "missing_count": max(0, len(points) - observed_count),
        "points": points,
        "metric_notice": (
            "每次完整 Seller Offer 刷新原样记录的近30天滚动浏览量；"
            "不是精确当天流量或独立访客数，历史无法证实的刷新点保留缺口。"
        ),
    }


def _captured_at_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _clean_title(value: str | None) -> str | None:
    normalized = " ".join(str(value or "").split())
    return normalized or None


def _title_signature(value: str | None) -> tuple[str, ...]:
    return tuple(keyword.casefold() for keyword in extract_title_keywords(value))
