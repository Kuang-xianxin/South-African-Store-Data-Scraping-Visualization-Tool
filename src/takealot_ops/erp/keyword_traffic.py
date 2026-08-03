"""Keyword change tracking over the platform's rolling 30-day page-view metric."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from takealot_ops.storage.models import (
    OfferCurrent,
    OfferSnapshot,
    ProductKeywordSnapshot,
)


SAST = ZoneInfo("Africa/Johannesburg")
MAX_KEYWORDS = 50
MAX_KEYWORD_LENGTH = 100
TREND_FLAT_THRESHOLD = 0.5


class KeywordTrafficInputError(ValueError):
    """Raised when a keyword snapshot cannot be recorded safely."""


class KeywordTrafficConflictError(KeywordTrafficInputError):
    """Raised when a new row would contradict an existing keyword timeline."""


def normalize_keywords(values: Sequence[str]) -> list[str]:
    """Trim and case-insensitively deduplicate a complete keyword set."""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = " ".join(str(raw).strip().split())
        if not value:
            continue
        if len(value) > MAX_KEYWORD_LENGTH:
            raise KeywordTrafficInputError(
                f"单个关键词不能超过 {MAX_KEYWORD_LENGTH} 个字符"
            )
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(value)
    if not normalized:
        raise KeywordTrafficInputError("请至少填写一个关键词")
    if len(normalized) > MAX_KEYWORDS:
        raise KeywordTrafficInputError(f"每次最多记录 {MAX_KEYWORDS} 个关键词")
    return normalized


def record_keyword_snapshot(
    engine: Engine,
    *,
    offer_id: str,
    effective_date: date,
    keywords: Sequence[str],
    note: str | None,
    actor_user_id: int | None,
    actor_username: str,
    today: date | None = None,
) -> dict[str, Any]:
    """Append a daily keyword state without rewriting earlier operator evidence."""
    normalized_offer_id = offer_id.strip()
    if not normalized_offer_id:
        raise KeywordTrafficInputError("商品编号不能为空")
    normalized_keywords = normalize_keywords(keywords)
    current_day = today or datetime.now(SAST).date()
    if effective_date > current_day:
        raise KeywordTrafficInputError("关键词生效日期不能晚于今天")
    normalized_note = str(note or "").strip() or None
    if normalized_note and len(normalized_note) > 500:
        raise KeywordTrafficInputError("备注不能超过 500 个字符")

    with Session(engine) as session, session.begin():
        offer = session.get(OfferCurrent, normalized_offer_id)
        if offer is None:
            raise KeywordTrafficInputError("没有找到对应的店铺商品")
        same_day = session.scalar(
            select(ProductKeywordSnapshot.id).where(
                ProductKeywordSnapshot.offer_id == normalized_offer_id,
                ProductKeywordSnapshot.effective_date == effective_date,
            )
        )
        if same_day is not None:
            raise KeywordTrafficConflictError("该商品当天已经记录关键词，请选择实际变更日期")
        latest = session.scalar(
            select(ProductKeywordSnapshot)
            .where(ProductKeywordSnapshot.offer_id == normalized_offer_id)
            .order_by(
                ProductKeywordSnapshot.effective_date.desc(),
                ProductKeywordSnapshot.id.desc(),
            )
            .limit(1)
        )
        if latest is not None and effective_date < latest.effective_date:
            raise KeywordTrafficConflictError(
                "不能在已有后续节点之前插入记录，请按实际发生顺序追加关键词变更"
            )
        previous = session.scalar(
            select(ProductKeywordSnapshot)
            .where(
                ProductKeywordSnapshot.offer_id == normalized_offer_id,
                ProductKeywordSnapshot.effective_date < effective_date,
            )
            .order_by(
                ProductKeywordSnapshot.effective_date.desc(),
                ProductKeywordSnapshot.id.desc(),
            )
            .limit(1)
        )
        previous_keywords = list(previous.keywords) if previous is not None else []
        if previous is not None and _keyword_key_set(previous_keywords) == _keyword_key_set(
            normalized_keywords
        ):
            raise KeywordTrafficConflictError("关键词与上一次记录一致，没有形成变更节点")

        snapshot = ProductKeywordSnapshot(
            offer_id=normalized_offer_id,
            effective_date=effective_date,
            keywords=normalized_keywords,
            note=normalized_note,
            recorded_by_user_id=actor_user_id,
            recorded_by_username=actor_username.strip() or "unknown",
            recorded_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(snapshot)
        session.flush()
        return _event_payload(
            snapshot,
            previous_keywords=previous_keywords,
            history_by_date={},
            comparison_days=7,
            as_of=current_day,
        )


def build_keyword_product_list(session: Session, *, as_of: date) -> dict[str, Any]:
    """Return one compact monitoring row for every current store offer."""
    offers = list(session.scalars(select(OfferCurrent).order_by(OfferCurrent.offer_id)))
    latest_dates = (
        select(
            OfferSnapshot.offer_id.label("offer_id"),
            func.max(OfferSnapshot.snapshot_date).label("snapshot_date"),
        )
        .where(OfferSnapshot.snapshot_date <= as_of)
        .group_by(OfferSnapshot.offer_id)
        .subquery()
    )
    latest_snapshots = list(
        session.scalars(
            select(OfferSnapshot).join(
                latest_dates,
                (OfferSnapshot.offer_id == latest_dates.c.offer_id)
                & (OfferSnapshot.snapshot_date == latest_dates.c.snapshot_date),
            )
        )
    )
    latest_by_offer = {row.offer_id: row for row in latest_snapshots}
    snapshots = list(
        session.scalars(
            select(ProductKeywordSnapshot)
            .where(ProductKeywordSnapshot.effective_date <= as_of)
            .order_by(
                ProductKeywordSnapshot.offer_id,
                ProductKeywordSnapshot.effective_date,
                ProductKeywordSnapshot.id,
            )
        )
    )
    events_by_offer: dict[str, list[ProductKeywordSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        events_by_offer[snapshot.offer_id].append(snapshot)

    items: list[dict[str, Any]] = []
    for offer in offers:
        latest = latest_by_offer.get(offer.offer_id)
        keyword_events = events_by_offer.get(offer.offer_id, [])
        last_event = keyword_events[-1] if keyword_events else None
        items.append(
            {
                "offer_id": offer.offer_id,
                "sku": offer.sku,
                "title": offer.title,
                "image_url": offer.image_url,
                "latest_page_views_30_days": (
                    latest.page_views_30_days if latest is not None else None
                ),
                "latest_snapshot_date": (
                    latest.snapshot_date.isoformat() if latest is not None else None
                ),
                "keyword_event_count": len(keyword_events),
                "keyword_change_count": max(0, len(keyword_events) - 1),
                "last_keyword_change_date": (
                    last_event.effective_date.isoformat() if last_event is not None else None
                ),
                "current_keywords": list(last_event.keywords) if last_event is not None else [],
            }
        )

    items.sort(
        key=lambda item: (
            item["keyword_event_count"] == 0,
            item["latest_page_views_30_days"] is None,
            -(
                item["latest_page_views_30_days"]
                if item["latest_page_views_30_days"] is not None
                else 0
            ),
            str(item["title"] or item["sku"] or item["offer_id"]).casefold(),
        )
    )
    return {
        "as_of": as_of.isoformat(),
        "items": items,
        "summary": {
            "product_count": len(items),
            "with_traffic_count": sum(
                item["latest_page_views_30_days"] is not None for item in items
            ),
            "tracked_keyword_count": sum(item["keyword_event_count"] > 0 for item in items),
            "keyword_change_count": sum(item["keyword_change_count"] for item in items),
        },
    }


def build_keyword_product_detail(
    session: Session,
    *,
    offer_id: str,
    as_of: date,
    history_days: int,
    comparison_days: int,
) -> dict[str, Any] | None:
    """Build a gap-preserving history and event-centered traffic comparison."""
    offer = session.get(OfferCurrent, offer_id)
    if offer is None:
        return None
    start = as_of - timedelta(days=history_days - 1)
    rows = list(
        session.scalars(
            select(OfferSnapshot)
            .where(
                OfferSnapshot.offer_id == offer_id,
                OfferSnapshot.snapshot_date >= start,
                OfferSnapshot.snapshot_date <= as_of,
            )
            .order_by(OfferSnapshot.snapshot_date)
        )
    )
    observed = {row.snapshot_date: row.page_views_30_days for row in rows}
    history: list[dict[str, Any]] = []
    cursor = start
    while cursor <= as_of:
        history.append(
            {
                "date": cursor.isoformat(),
                "page_views_30_days": observed.get(cursor),
            }
        )
        cursor += timedelta(days=1)

    keyword_rows = list(
        session.scalars(
            select(ProductKeywordSnapshot)
            .where(
                ProductKeywordSnapshot.offer_id == offer_id,
                ProductKeywordSnapshot.effective_date <= as_of,
            )
            .order_by(
                ProductKeywordSnapshot.effective_date,
                ProductKeywordSnapshot.id,
            )
        )
    )
    events: list[dict[str, Any]] = []
    previous_keywords: list[str] = []
    for keyword_row in keyword_rows:
        events.append(
            _event_payload(
                keyword_row,
                previous_keywords=previous_keywords,
                history_by_date=observed,
                comparison_days=comparison_days,
                as_of=as_of,
            )
        )
        previous_keywords = list(keyword_row.keywords)

    return {
        "as_of": as_of.isoformat(),
        "history_days": history_days,
        "comparison_days": comparison_days,
        "product": {
            "offer_id": offer.offer_id,
            "sku": offer.sku,
            "title": offer.title,
            "image_url": offer.image_url,
            "current_keywords": previous_keywords,
        },
        "history": history,
        "events": events,
        "metric_notice": (
            "曲线记录平台返回的近30天浏览量滚动窗口；相邻变化是窗口净变化，"
            "不是精确当天流量或独立访客数。"
        ),
    }


def _event_payload(
    snapshot: ProductKeywordSnapshot,
    *,
    previous_keywords: Sequence[str],
    history_by_date: dict[date, int | None],
    comparison_days: int,
    as_of: date,
) -> dict[str, Any]:
    current_keywords = list(snapshot.keywords)
    previous_by_key = {value.casefold(): value for value in previous_keywords}
    current_by_key = {value.casefold(): value for value in current_keywords}
    added = [value for key, value in current_by_key.items() if key not in previous_by_key]
    removed = [value for key, value in previous_by_key.items() if key not in current_by_key]
    return {
        "id": snapshot.id,
        "effective_date": snapshot.effective_date.isoformat(),
        "event_kind": "change" if previous_keywords else "baseline",
        "keywords": current_keywords,
        "previous_keywords": list(previous_keywords),
        "added_keywords": added,
        "removed_keywords": removed,
        "note": snapshot.note,
        "recorded_by_username": snapshot.recorded_by_username,
        "recorded_at": snapshot.recorded_at.isoformat(),
        "comparison": _comparison_payload(
            effective_date=snapshot.effective_date,
            history_by_date=history_by_date,
            comparison_days=comparison_days,
            as_of=as_of,
        ),
    }


def _comparison_payload(
    *,
    effective_date: date,
    history_by_date: dict[date, int | None],
    comparison_days: int,
    as_of: date,
) -> dict[str, Any]:
    before_start = effective_date - timedelta(days=comparison_days)
    before_end = effective_date - timedelta(days=1)
    after_start = effective_date + timedelta(days=1)
    after_end = effective_date + timedelta(days=comparison_days)
    before_points = _points(history_by_date, before_start, before_end)
    after_points = _points(history_by_date, after_start, min(after_end, as_of))
    before = _window_payload(before_start, before_end, before_points)
    after = _window_payload(after_start, after_end, after_points)
    before_value = before["last_value"]
    after_value = after["last_value"]
    delta: int | None = None
    delta_percent: float | None = None
    traffic_direction = "unavailable"
    if before_value is not None and after_value is not None:
        delta = int(after_value) - int(before_value)
        traffic_direction = "up" if delta > 0 else "down" if delta < 0 else "flat"
        if before_value != 0:
            delta_percent = round(delta / before_value * 100, 1)

    before_slope = before["slope_per_day"]
    after_slope = after["slope_per_day"]
    slope_change: float | None = None
    trend_change = "insufficient"
    if before_slope is not None and after_slope is not None:
        slope_change = round(after_slope - before_slope, 2)
        before_direction = _trend_direction(before_slope)
        after_direction = _trend_direction(after_slope)
        if before_direction != "up" and after_direction == "up":
            trend_change = "reversal_up"
        elif before_direction != "down" and after_direction == "down":
            trend_change = "reversal_down"
        elif slope_change > TREND_FLAT_THRESHOLD:
            trend_change = "improving"
        elif slope_change < -TREND_FLAT_THRESHOLD:
            trend_change = "weakening"
        else:
            trend_change = "stable"

    if as_of < after_start:
        status = "waiting"
    elif as_of < after_end:
        status = "collecting"
    elif before_value is None or after_value is None:
        status = "data_missing"
    else:
        status = "complete"
    observed_after_days = max(0, min(comparison_days, (as_of - effective_date).days))
    return {
        "status": status,
        "comparison_days": comparison_days,
        "observed_after_days": observed_after_days,
        "before": before,
        "after": after,
        "traffic_direction": traffic_direction,
        "traffic_delta": delta,
        "traffic_delta_percent": delta_percent,
        "trend_change": trend_change,
        "slope_change": slope_change,
    }


def _points(
    history_by_date: dict[date, int | None],
    start: date,
    end: date,
) -> list[tuple[date, int]]:
    if end < start:
        return []
    result: list[tuple[date, int]] = []
    cursor = start
    while cursor <= end:
        value = history_by_date.get(cursor)
        if value is not None:
            result.append((cursor, int(value)))
        cursor += timedelta(days=1)
    return result


def _window_payload(
    start: date,
    end: date,
    points: Sequence[tuple[date, int]],
) -> dict[str, Any]:
    slope = _linear_slope(points)
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "available_days": len(points),
        "first_value": points[0][1] if points else None,
        "last_value": points[-1][1] if points else None,
        "window_net_change": points[-1][1] - points[0][1] if len(points) >= 2 else None,
        "slope_per_day": round(slope, 2) if slope is not None else None,
        "trend_direction": _trend_direction(slope),
    }


def _linear_slope(points: Sequence[tuple[date, int]]) -> float | None:
    if len(points) < 2:
        return None
    origin = points[0][0].toordinal()
    xs = [float(point_date.toordinal() - origin) for point_date, _ in points]
    ys = [float(value) for _, value in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denominator = sum((value - mean_x) ** 2 for value in xs)
    if denominator == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    slope /= denominator
    return slope if isfinite(slope) else None


def _trend_direction(slope: float | None) -> str:
    if slope is None:
        return "unavailable"
    if slope > TREND_FLAT_THRESHOLD:
        return "up"
    if slope < -TREND_FLAT_THRESHOLD:
        return "down"
    return "flat"


def _keyword_key_set(values: Sequence[str]) -> set[str]:
    return {value.casefold() for value in values}
