"""Own-store rolling traffic snapshots for competitor detail views."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from takealot_ops.erp.keyword_traffic import extract_title_keywords
from takealot_ops.storage.models import ErpStore, OfferCurrent, OfferSnapshot
from takealot_ops.storage.store_context import normalize_store_code, store_scope


CHINA = ZoneInfo("Asia/Shanghai")


def build_own_store_traffic_series(
    session: Session,
    *,
    plid: str,
    store_codes: set[str],
    start_date: date | None,
    end_date: date | None,
) -> list[dict[str, Any]]:
    """Return one gap-preserving rolling-30-day view series per current Offer.

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
            snapshots = list(
                session.scalars(
                    select(OfferSnapshot)
                    .where(OfferSnapshot.offer_id.in_(current_offer_ids))
                    .order_by(
                        OfferSnapshot.offer_id,
                        OfferSnapshot.captured_at,
                        OfferSnapshot.id,
                    )
                )
            )

        snapshots_by_offer: dict[str, list[OfferSnapshot]] = defaultdict(list)
        for snapshot in snapshots:
            snapshots_by_offer[str(snapshot.offer_id)].append(snapshot)
        for offer in current_offers:
            result.append(
                _offer_traffic_series(
                    store_code=store_code,
                    store_name=store_names.get(store_code, store_code),
                    plid=normalized_plid,
                    offer=offer,
                    snapshots=snapshots_by_offer.get(str(offer.offer_id), []),
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
    snapshots: Sequence[OfferSnapshot],
    start_date: date | None,
    end_date: date | None,
) -> dict[str, Any]:
    ordered = sorted(
        snapshots,
        key=lambda row: (_captured_at_utc(row.captured_at), row.id),
    )
    latest_by_display_date: dict[date, OfferSnapshot] = {}
    title_change_by_snapshot_id: dict[int, tuple[bool, str | None]] = {}
    previous_title: str | None = None
    previous_signature: tuple[str, ...] | None = None
    for snapshot in ordered:
        display_date = _china_day(snapshot.captured_at) or snapshot.snapshot_date
        latest_by_display_date[display_date] = snapshot
        title = _clean_title(snapshot.title)
        signature = _title_signature(title)
        changed = bool(
            signature
            and previous_signature is not None
            and signature != previous_signature
        )
        title_change_by_snapshot_id[snapshot.id] = (
            changed,
            previous_title if changed else None,
        )
        if signature:
            previous_signature = signature
            previous_title = title

    available_dates = sorted(latest_by_display_date)
    selected_start = start_date or (available_dates[0] if available_dates else None)
    selected_end = end_date or (available_dates[-1] if available_dates else None)
    if selected_start is not None and selected_end is None:
        selected_end = selected_start
    if selected_end is not None and selected_start is None:
        selected_start = selected_end

    points: list[dict[str, Any]] = []
    if selected_start is not None and selected_end is not None:
        for display_date in _date_range(selected_start, selected_end):
            daily_snapshot = latest_by_display_date.get(display_date)
            title_changed, prior_title = (
                title_change_by_snapshot_id.get(daily_snapshot.id, (False, None))
                if daily_snapshot is not None
                else (False, None)
            )
            points.append(
                {
                    "date": display_date.isoformat(),
                    "captured_at": (
                        _captured_at_utc(daily_snapshot.captured_at).isoformat()
                        if daily_snapshot is not None
                        else None
                    ),
                    "page_views_30_days": (
                        daily_snapshot.page_views_30_days
                        if daily_snapshot is not None
                        else None
                    ),
                    "title": (
                        _clean_title(daily_snapshot.title)
                        if daily_snapshot is not None
                        else None
                    ),
                    "title_changed": title_changed,
                    "previous_title": prior_title,
                    "data_status": (
                        "observed" if daily_snapshot is not None else "missing"
                    ),
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
        "missing_count": max(0, len(points) - traffic_count),
        "points": points,
        "metric_notice": (
            "每日完整 Seller Offer 快照中的近30天滚动浏览量；不是精确当天流量或独立访客数。"
        ),
    }


def _captured_at_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _china_day(value: datetime | None) -> date | None:
    if value is None:
        return None
    return _captured_at_utc(value).astimezone(CHINA).date()


def _clean_title(value: str | None) -> str | None:
    normalized = " ".join(str(value or "").split())
    return normalized or None


def _title_signature(value: str | None) -> tuple[str, ...]:
    return tuple(keyword.casefold() for keyword in extract_title_keywords(value))


def _date_range(start: date, end: date) -> list[date]:
    if start > end:
        return []
    return [
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
    ]
