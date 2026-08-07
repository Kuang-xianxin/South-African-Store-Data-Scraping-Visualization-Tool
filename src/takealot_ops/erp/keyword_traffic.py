"""Automatic title-keyword change detection over rolling 30-day page views."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, timedelta
from math import isfinite
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from takealot_ops.storage.models import OfferCurrent, OfferSnapshot


TREND_FLAT_THRESHOLD = 0.5
TITLE_TERM_PATTERN = re.compile(r"[^\W_]+(?:['’.-][^\W_]+)*", re.UNICODE)


def extract_title_keywords(title: str | None) -> list[str]:
    """Return unique visible terms from the official Seller Offer title."""
    keywords: list[str] = []
    seen: set[str] = set()
    for match in TITLE_TERM_PATTERN.finditer(str(title or "")):
        value = match.group(0).strip("-.'’")
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        keywords.append(value)
    return keywords


def build_keyword_product_list(session: Session, *, as_of: date) -> dict[str, Any]:
    """Return automatic title-keyword archive status for every current offer."""
    offers = list(session.scalars(select(OfferCurrent).order_by(OfferCurrent.offer_id)))
    snapshots = list(
        session.scalars(
            select(OfferSnapshot)
            .where(OfferSnapshot.snapshot_date <= as_of)
            .order_by(
                OfferSnapshot.offer_id,
                OfferSnapshot.snapshot_date,
                OfferSnapshot.id,
            )
        )
    )
    snapshots_by_offer: dict[str, list[OfferSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        snapshots_by_offer[snapshot.offer_id].append(snapshot)

    items: list[dict[str, Any]] = []
    for offer in offers:
        offer_snapshots = snapshots_by_offer.get(offer.offer_id, [])
        latest = offer_snapshots[-1] if offer_snapshots else None
        title_states = _title_states(offer_snapshots)
        latest_state = title_states[-1] if title_states else None
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
                "keyword_event_count": len(title_states),
                "keyword_change_count": max(0, len(title_states) - 1),
                "last_keyword_change_date": (
                    title_states[-1]["snapshot"].snapshot_date.isoformat()
                    if len(title_states) > 1
                    else None
                ),
                "current_keywords": (
                    list(latest_state["keywords"]) if latest_state is not None else []
                ),
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
            "archived_product_count": sum(
                item["keyword_event_count"] > 0 for item in items
            ),
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
    """Build gap-preserving traffic history and automatic title change events."""
    offer = session.get(OfferCurrent, offer_id)
    if offer is None:
        return None
    all_rows = list(
        session.scalars(
            select(OfferSnapshot)
            .where(
                OfferSnapshot.offer_id == offer_id,
                OfferSnapshot.snapshot_date <= as_of,
            )
            .order_by(OfferSnapshot.snapshot_date, OfferSnapshot.id)
        )
    )
    observed = {row.snapshot_date: row.page_views_30_days for row in all_rows}
    start = as_of - timedelta(days=history_days - 1)
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

    title_states = _title_states(all_rows)
    events: list[dict[str, Any]] = []
    previous_state: dict[str, Any] | None = None
    for state in title_states:
        events.append(
            _event_payload(
                state,
                previous_state=previous_state,
                history_by_date=observed,
                comparison_days=comparison_days,
                as_of=as_of,
            )
        )
        previous_state = state

    current_keywords = (
        list(title_states[-1]["keywords"]) if title_states else extract_title_keywords(offer.title)
    )
    return {
        "as_of": as_of.isoformat(),
        "history_days": history_days,
        "comparison_days": comparison_days,
        "product": {
            "offer_id": offer.offer_id,
            "sku": offer.sku,
            "title": offer.title,
            "image_url": offer.image_url,
            "current_keywords": current_keywords,
        },
        "history": history,
        "events": events,
        "metric_notice": (
            "Seller Offers 当前没有独立 keyword/search-term 字段；关键词节点来自每日完整"
            " Offer 快照中的官方商品标题词。曲线是近30天浏览量滚动窗口，不是精确"
            "当天流量或独立访客数；节点附近变化只表示观察关联，不证明因果。"
        ),
    }


def _title_states(rows: Sequence[OfferSnapshot]) -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    previous_signature: tuple[str, ...] | None = None
    for snapshot in rows:
        signature = _title_signature(snapshot.title)
        if not signature:
            continue
        if previous_signature is None or signature != previous_signature:
            states.append(
                {
                    "snapshot": snapshot,
                    "title": " ".join(str(snapshot.title or "").split()),
                    "signature": signature,
                    "keywords": extract_title_keywords(snapshot.title),
                }
            )
        previous_signature = signature
    return states


def _title_signature(title: str | None) -> tuple[str, ...]:
    return tuple(
        match.group(0).strip("-.'’").casefold()
        for match in TITLE_TERM_PATTERN.finditer(str(title or ""))
        if match.group(0).strip("-.'’")
    )


def _event_payload(
    state: dict[str, Any],
    *,
    previous_state: dict[str, Any] | None,
    history_by_date: dict[date, int | None],
    comparison_days: int,
    as_of: date,
) -> dict[str, Any]:
    snapshot = state["snapshot"]
    current_keywords = list(state["keywords"])
    previous_keywords = (
        list(previous_state["keywords"]) if previous_state is not None else []
    )
    previous_by_key = {value.casefold(): value for value in previous_keywords}
    current_by_key = {value.casefold(): value for value in current_keywords}
    added = [value for key, value in current_by_key.items() if key not in previous_by_key]
    removed = [value for key, value in previous_by_key.items() if key not in current_by_key]
    event_kind = "change" if previous_state is not None else "baseline"
    return {
        "id": snapshot.id,
        "effective_date": snapshot.snapshot_date.isoformat(),
        "event_kind": event_kind,
        "event_source": "offer_title",
        "change_label": _change_label(event_kind, added, removed),
        "keywords": current_keywords,
        "previous_keywords": previous_keywords,
        "added_keywords": added,
        "removed_keywords": removed,
        "source_title": state["title"],
        "previous_source_title": (
            previous_state["title"] if previous_state is not None else None
        ),
        "detected_at": snapshot.captured_at.isoformat(),
        "comparison": _comparison_payload(
            effective_date=snapshot.snapshot_date,
            history_by_date=history_by_date,
            comparison_days=comparison_days,
            as_of=as_of,
        ),
    }


def _change_label(event_kind: str, added: Sequence[str], removed: Sequence[str]) -> str:
    if event_kind == "baseline":
        return "基线｜首次完整标题快照"
    if added and removed:
        return f"变化｜新增 {len(added)} 词，移除 {len(removed)} 词"
    if added:
        return f"变化｜新增 {len(added)} 词"
    if removed:
        return f"变化｜移除 {len(removed)} 词"
    return "变化｜标题词顺序或写法变化"


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
