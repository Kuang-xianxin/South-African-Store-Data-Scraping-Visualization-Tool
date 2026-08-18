"""Read-only, mutually separated anomaly-product projections for the Vue ERP."""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Lock
from typing import Any, TypeGuard, cast

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from takealot_ops.dashboard.labels import OFFER_STATUS_LABELS
from takealot_ops.metrics.service import DashboardDataset
from takealot_ops.storage.models import (
    CollectionRun,
    DailyProductMetric,
    DailySalesMetricState,
    OfferSnapshot,
)


SLOW_DAY_OPTIONS = (4, 7, 10, 15, 20, 30)
SALES_STOP_ZERO_DAYS = 3
SALES_STOP_BASELINE_DAYS = 7
SALES_STOP_MIN_SELLING_DAYS = 5
SALES_STOP_MIN_BASELINE_UNITS = 7
STOCK_STATUS_TYPES = (
    "not_buyable",
    "disabled_by_takealot",
    "disabled_by_seller",
)
_ANOMALY_PRODUCT_DAILY_COLUMNS = (
    "metric_date",
    "offer_id",
    "ordered_units",
)
_ANOMALY_OFFER_CURRENT_COLUMNS = (
    "offer_id",
    "tsin_id",
    "sku",
    "title",
    "selling_price",
    "status",
    "image_url",
    "productline_id",
    "conversion_percentage_30_days",
    "page_views_30_days",
    "total_stock",
    "takealot_available_stock",
    "seller_available_stock",
    "takealot_stock_in_receiving",
    "takealot_stock_on_way",
)
_ANOMALY_OFFER_HISTORY_COLUMNS = (
    "snapshot_date",
    "offer_id",
    "total_stock",
    "takealot_available_stock",
    "seller_available_stock",
)


@dataclass(frozen=True)
class AnomalyProductDataRevision:
    """Cheap source-table fingerprint used to invalidate cached projections."""

    offer_scope_date: date | None
    latest_offer_run_at: datetime | None
    offer_run_count: int
    latest_offer_capture_at: datetime | None
    offer_snapshot_count: int
    latest_metric_state_at: datetime | None
    metric_state_count: int
    product_metric_count: int


class AnomalyProductPayloadCache:
    """Small process-local LRU keyed by store, date, and durable data revision."""

    def __init__(self, *, max_entries: int = 64) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple[object, ...], dict[str, Any]] = OrderedDict()
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: tuple[object, ...]) -> dict[str, Any] | None:
        with self._lock:
            payload = self._entries.pop(key, None)
            if payload is None:
                self._misses += 1
                return None
            self._entries[key] = payload
            self._hits += 1
            return payload

    def put(self, key: tuple[object, ...], payload: dict[str, Any]) -> None:
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = payload
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "hits": self._hits,
                "misses": self._misses,
            }


def load_cached_anomaly_product_payload(
    session: Session,
    *,
    cache: AnomalyProductPayloadCache,
    store_code: str,
    requested_as_of: date,
    completed_through: date,
) -> dict[str, Any]:
    """Load the narrow anomaly projection, reusing it until source data changes."""

    revision = _anomaly_product_data_revision(
        session,
        requested_as_of=requested_as_of,
        completed_through=completed_through,
    )
    cache_key = (
        store_code,
        requested_as_of,
        completed_through,
        revision,
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    dataset, states = _load_anomaly_product_dataset(
        session,
        offer_scope_date=revision.offer_scope_date,
        completed_through=completed_through,
    )
    payload = build_anomaly_product_payload(
        dataset,
        requested_as_of=requested_as_of,
        completed_through=completed_through,
        verified_dates=verified_sales_metric_dates(states),
    )
    cache.put(cache_key, payload)
    return payload


def verified_sales_metric_dates(
    states: Iterable[DailySalesMetricState],
) -> set[date]:
    """Return SAST business dates backed by a successful Sales API source."""

    result: set[date] = set()
    for state in states:
        if state.source_kind != "takealot_sales_api":
            continue
        if _state_verified_at(state) is None:
            continue
        result.add(state.metric_date)
    return result


def _anomaly_product_data_revision(
    session: Session,
    *,
    requested_as_of: date,
    completed_through: date,
) -> AnomalyProductDataRevision:
    offer_run = session.execute(
        select(
            func.max(CollectionRun.scope_date),
            func.max(CollectionRun.finished_at),
            func.count(CollectionRun.run_id),
        ).where(
            CollectionRun.run_type == "offers",
            CollectionRun.status == "success",
            CollectionRun.scope_date.is_not(None),
            CollectionRun.scope_date <= requested_as_of,
        )
    ).one()
    offer_scope_date = offer_run[0]
    offer_snapshot = session.execute(
        select(
            func.max(OfferSnapshot.captured_at),
            func.count(OfferSnapshot.id),
        ).where(OfferSnapshot.snapshot_date <= completed_through)
    ).one()
    metric_state = session.execute(
        select(
            func.max(DailySalesMetricState.updated_at),
            func.count(DailySalesMetricState.id),
        ).where(DailySalesMetricState.metric_date <= completed_through)
    ).one()
    product_metric_count = int(
        session.scalar(
            select(func.count(DailyProductMetric.id)).where(
                DailyProductMetric.metric_date <= completed_through
            )
        )
        or 0
    )
    return AnomalyProductDataRevision(
        offer_scope_date=offer_scope_date if isinstance(offer_scope_date, date) else None,
        latest_offer_run_at=(
            offer_run[1] if isinstance(offer_run[1], datetime) else None
        ),
        offer_run_count=int(offer_run[2] or 0),
        latest_offer_capture_at=(
            offer_snapshot[0]
            if isinstance(offer_snapshot[0], datetime)
            else None
        ),
        offer_snapshot_count=int(offer_snapshot[1] or 0),
        latest_metric_state_at=(
            metric_state[0] if isinstance(metric_state[0], datetime) else None
        ),
        metric_state_count=int(metric_state[1] or 0),
        product_metric_count=product_metric_count,
    )


def _load_anomaly_product_dataset(
    session: Session,
    *,
    offer_scope_date: date | None,
    completed_through: date,
) -> tuple[DashboardDataset, list[DailySalesMetricState]]:
    product_daily = _query_frame(
        session,
        select(
            DailyProductMetric.metric_date,
            DailyProductMetric.offer_id,
            DailyProductMetric.ordered_units,
        )
        .where(DailyProductMetric.metric_date <= completed_through)
        .order_by(DailyProductMetric.metric_date, DailyProductMetric.offer_id),
        _ANOMALY_PRODUCT_DAILY_COLUMNS,
    )
    if offer_scope_date is None:
        offer_current = pd.DataFrame(columns=_ANOMALY_OFFER_CURRENT_COLUMNS)
    else:
        offer_current = _query_frame(
            session,
            select(
                OfferSnapshot.offer_id,
                OfferSnapshot.tsin_id,
                OfferSnapshot.sku,
                OfferSnapshot.title,
                OfferSnapshot.selling_price,
                OfferSnapshot.status,
                OfferSnapshot.image_url,
                OfferSnapshot.productline_id,
                OfferSnapshot.conversion_percentage_30_days,
                OfferSnapshot.page_views_30_days,
                OfferSnapshot.total_stock,
                OfferSnapshot.takealot_available_stock,
                OfferSnapshot.seller_available_stock,
                OfferSnapshot.takealot_stock_in_receiving,
                OfferSnapshot.takealot_stock_on_way,
            )
            .where(OfferSnapshot.snapshot_date == offer_scope_date)
            .order_by(OfferSnapshot.offer_id),
            _ANOMALY_OFFER_CURRENT_COLUMNS,
        )
    offer_history = _query_frame(
        session,
        select(
            OfferSnapshot.snapshot_date,
            OfferSnapshot.offer_id,
            OfferSnapshot.total_stock,
            OfferSnapshot.takealot_available_stock,
            OfferSnapshot.seller_available_stock,
        )
        .where(OfferSnapshot.snapshot_date <= completed_through)
        .order_by(OfferSnapshot.snapshot_date, OfferSnapshot.offer_id),
        _ANOMALY_OFFER_HISTORY_COLUMNS,
    )
    states = list(
        session.scalars(
            select(DailySalesMetricState)
            .where(DailySalesMetricState.metric_date <= completed_through)
            .order_by(DailySalesMetricState.metric_date)
        )
    )
    empty = pd.DataFrame()
    return (
        DashboardDataset(
            store_daily=empty.copy(),
            product_daily=product_daily,
            offer_current=offer_current,
            anomalies=empty.copy(),
            quality_events=empty.copy(),
            offer_history=offer_history,
        ),
        states,
    )


def _query_frame(
    session: Session,
    statement: Any,
    columns: tuple[str, ...],
) -> pd.DataFrame:
    rows = session.execute(statement).mappings().all()
    values = [
        {
            column: (
                float(value)
                if isinstance((value := row.get(column)), Decimal)
                else value
            )
            for column in columns
        }
        for row in rows
    ]
    return pd.DataFrame(values, columns=columns)


def build_anomaly_product_payload(
    dataset: DashboardDataset,
    *,
    requested_as_of: date,
    completed_through: date,
    verified_dates: set[date],
) -> dict[str, Any]:
    """Build independent anomaly groups without changing legacy risk records."""

    product_daily = _normalized_product_daily(dataset.product_daily, completed_through)
    offer_current = _normalized_offer_current(dataset.offer_current)
    available_metric_dates = {
        metric_date
        for metric_date in product_daily.get("_metric_date", pd.Series(dtype="object"))
        if isinstance(metric_date, date)
    }
    eligible_dates = {
        metric_date
        for metric_date in verified_dates
        if metric_date <= completed_through and metric_date in available_metric_dates
    }
    data_through = max(eligible_dates) if eligible_dates else None
    contiguous_dates = _contiguous_dates(data_through, verified_dates)
    sales_by_offer = _sales_by_offer(product_daily, data_through, eligible_dates)
    stock_by_offer = _stock_by_offer(dataset.offer_history, data_through)

    sudden_sales_stop: list[dict[str, Any]] = []
    stock_status_anomalies: dict[str, list[dict[str, Any]]] = {
        status: [] for status in STOCK_STATUS_TYPES
    }
    slow_moving: list[dict[str, Any]] = []

    for row in offer_current.to_dict(orient="records"):
        offer_id = _text(row.get("offer_id"))
        plid = _text(row.get("productline_id"))
        if not offer_id or not plid:
            continue
        daily_sales = sales_by_offer.get(offer_id, {})
        zero_streak = _zero_sales_streak(daily_sales, contiguous_dates)
        stocked_zero_streak = _stocked_zero_sales_streak(
            daily_sales,
            stock_by_offer.get(offer_id, {}),
            contiguous_dates,
        )
        item = _base_item(
            cast("Mapping[str, Any]", row),
            data_through,
            daily_sales,
            zero_streak,
        )

        status = item["offer_status"]
        if status in stock_status_anomalies and item["available_stock"] > 0:
            stock_item = dict(item)
            stock_item["anomaly_type"] = f"{status}_with_stock"
            stock_item["anomaly_label"] = (
                f"{item['offer_status_label']}但仍有可售库存"
            )
            stock_status_anomalies[status].append(stock_item)

        sudden_evidence = _sudden_sales_stop_evidence(daily_sales, data_through)
        if sudden_evidence is not None:
            sudden_item = dict(item)
            sudden_item.update(sudden_evidence)
            sudden_item["anomaly_type"] = "sudden_sales_stop"
            sudden_item["anomaly_label"] = "动销突然中断"
            sudden_sales_stop.append(sudden_item)

        if (
            status == "buyable"
            and item["available_stock"] > 0
            and stocked_zero_streak["days"] >= min(SLOW_DAY_OPTIONS)
        ):
            slow_item = dict(item)
            slow_item["no_sales_days"] = stocked_zero_streak["days"]
            slow_item["no_sales_days_exact"] = stocked_zero_streak["exact"]
            slow_item["slow_moving_started_on"] = stocked_zero_streak["started_on"]
            slow_item["anomaly_type"] = "slow_moving"
            slow_item["anomaly_label"] = "有库存滞销"
            slow_moving.append(slow_item)

    sudden_sales_stop.sort(
        key=lambda item: (
            -int(item.get("baseline_total_units") or 0),
            str(item.get("title") or ""),
        )
    )
    for items in stock_status_anomalies.values():
        items.sort(
            key=lambda item: (
                -int(item.get("inventory_units") or 0),
                str(item.get("title") or ""),
            )
        )
    slow_moving.sort(
        key=lambda item: (
            -int(item.get("no_sales_days") or 0),
            -int(item.get("available_stock") or 0),
            str(item.get("title") or ""),
        )
    )

    slow_counts = {
        str(days): sum(
            int(item["no_sales_days"]) >= days for item in slow_moving
        )
        for days in SLOW_DAY_OPTIONS
    }
    return {
        "requested_as_of": requested_as_of.isoformat(),
        "completed_through": completed_through.isoformat(),
        "data_through": data_through.isoformat() if data_through else None,
        "date_basis": "Africa/Johannesburg",
        "sales_zero_evidence": "verified_complete_business_days_only",
        "rules": {
            "sales_stop_zero_days": SALES_STOP_ZERO_DAYS,
            "sales_stop_baseline_days": SALES_STOP_BASELINE_DAYS,
            "sales_stop_min_selling_days": SALES_STOP_MIN_SELLING_DAYS,
            "sales_stop_min_baseline_units": SALES_STOP_MIN_BASELINE_UNITS,
            "slow_day_options": list(SLOW_DAY_OPTIONS),
            "slow_moving_requires_status": "buyable",
            "slow_moving_requires_available_stock": True,
            "slow_moving_day_basis": "verified_zero_sales_and_positive_stock_days",
            "stock_status_requires_available_stock": True,
            "stock_status_excluded_inventory": ["receiving", "on_way"],
        },
        "summary": {
            "sudden_sales_stop": len(sudden_sales_stop),
            "not_buyable_with_stock": len(stock_status_anomalies["not_buyable"]),
            "disabled_by_takealot_with_stock": len(
                stock_status_anomalies["disabled_by_takealot"]
            ),
            "disabled_by_seller_with_stock": len(
                stock_status_anomalies["disabled_by_seller"]
            ),
            "slow_moving_by_days": slow_counts,
        },
        "sudden_sales_stop": sudden_sales_stop,
        "stock_status_anomalies": stock_status_anomalies,
        "slow_moving": slow_moving,
    }


def _normalized_product_daily(frame: pd.DataFrame, through: date) -> pd.DataFrame:
    required = {"metric_date", "offer_id", "ordered_units"}
    if frame.empty or not required.issubset(frame.columns):
        return pd.DataFrame(columns=[*required, "_metric_date"])
    result = frame.copy()
    result["_metric_date"] = pd.to_datetime(
        result["metric_date"], errors="coerce"
    ).dt.date
    result["ordered_units"] = pd.to_numeric(
        result["ordered_units"], errors="coerce"
    )
    return result.loc[
        result["offer_id"].notna()
        & result["_metric_date"].notna()
        & (result["_metric_date"] <= through)
    ].sort_values(["offer_id", "_metric_date"])


def _normalized_offer_current(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "offer_id" not in frame.columns:
        return pd.DataFrame(columns=["offer_id", "productline_id"])
    result = frame.copy()
    if "productline_id" not in result.columns:
        result["productline_id"] = None
    return result.drop_duplicates("offer_id", keep="last")


def _sales_by_offer(
    frame: pd.DataFrame,
    through: date | None,
    allowed_dates: set[date],
) -> dict[str, dict[date, int | None]]:
    if through is None or frame.empty:
        return {}
    result: dict[str, dict[date, int | None]] = {}
    for row in frame.to_dict(orient="records"):
        offer_id = _text(row.get("offer_id"))
        metric_date = row.get("_metric_date")
        if (
            not offer_id
            or not isinstance(metric_date, date)
            or metric_date > through
            or metric_date not in allowed_dates
        ):
            continue
        units_value = row.get("ordered_units")
        units = (
            max(0, int(units_value))
            if _finite_number(units_value)
            else None
        )
        result.setdefault(offer_id, {})[metric_date] = units
    return result


def _stock_by_offer(
    frame: pd.DataFrame,
    through: date | None,
) -> dict[str, dict[date, int | None]]:
    """Return daily available-stock evidence for each exact Offer identity."""

    required = {"snapshot_date", "offer_id"}
    if through is None or frame.empty or not required.issubset(frame.columns):
        return {}
    result: dict[str, dict[date, int | None]] = {}
    normalized = frame.copy()
    normalized["_snapshot_date"] = pd.to_datetime(
        normalized["snapshot_date"], errors="coerce"
    ).dt.date
    normalized = normalized.loc[
        normalized["offer_id"].notna()
        & normalized["_snapshot_date"].notna()
        & (normalized["_snapshot_date"] <= through)
    ].drop_duplicates(["offer_id", "_snapshot_date"], keep="last")
    for row in normalized.to_dict(orient="records"):
        offer_id = _text(row.get("offer_id"))
        snapshot_date = row.get("_snapshot_date")
        if not offer_id or not isinstance(snapshot_date, date):
            continue
        result.setdefault(offer_id, {})[snapshot_date] = _available_stock_or_none(
            cast("Mapping[str, Any]", row)
        )
    return result


def _contiguous_dates(
    through: date | None,
    verified_dates: set[date],
) -> list[date]:
    if through is None:
        return []
    result: list[date] = []
    cursor = through
    while cursor in verified_dates:
        result.append(cursor)
        cursor -= timedelta(days=1)
    return result


def _zero_sales_streak(
    daily_sales: Mapping[date, int | None],
    contiguous_dates: list[date],
) -> dict[str, Any]:
    count = 0
    encountered_positive = False
    for metric_date in contiguous_dates:
        units = daily_sales.get(metric_date)
        if units is None:
            break
        if units > 0:
            encountered_positive = True
            break
        count += 1
    positive_dates = [
        metric_date
        for metric_date, units in daily_sales.items()
        if units is not None and units > 0
    ]
    return {
        "days": count,
        "exact": encountered_positive,
        "last_sale_on": max(positive_dates).isoformat() if positive_dates else None,
    }


def _stocked_zero_sales_streak(
    daily_sales: Mapping[date, int | None],
    daily_stock: Mapping[date, int | None],
    contiguous_dates: list[date],
) -> dict[str, Any]:
    """Count only consecutive verified zero-sale days with positive stock evidence."""

    count = 0
    boundary_observed = False
    started_on: date | None = None
    for metric_date in contiguous_dates:
        units = daily_sales.get(metric_date)
        if units is None:
            break
        if units > 0:
            boundary_observed = True
            break
        stock = daily_stock.get(metric_date)
        if stock is None:
            break
        if stock <= 0:
            boundary_observed = True
            break
        count += 1
        started_on = metric_date
    return {
        "days": count,
        "exact": boundary_observed,
        "started_on": started_on.isoformat() if started_on else None,
    }


def _sudden_sales_stop_evidence(
    daily_sales: Mapping[date, int | None],
    through: date | None,
) -> dict[str, Any] | None:
    if through is None:
        return None
    zero_dates = [
        through - timedelta(days=offset)
        for offset in range(SALES_STOP_ZERO_DAYS - 1, -1, -1)
    ]
    baseline_end = zero_dates[0] - timedelta(days=1)
    baseline_dates = [
        baseline_end - timedelta(days=offset)
        for offset in range(SALES_STOP_BASELINE_DAYS - 1, -1, -1)
    ]
    zero_values = [daily_sales.get(metric_date) for metric_date in zero_dates]
    baseline_values = [daily_sales.get(metric_date) for metric_date in baseline_dates]
    if any(value is None for value in [*zero_values, *baseline_values]):
        return None
    normalized_zero = [int(value or 0) for value in zero_values]
    normalized_baseline = [int(value or 0) for value in baseline_values]
    baseline_total = sum(normalized_baseline)
    baseline_selling_days = sum(value > 0 for value in normalized_baseline)
    if (
        any(value != 0 for value in normalized_zero)
        or normalized_baseline[-1] <= 0
        or baseline_selling_days < SALES_STOP_MIN_SELLING_DAYS
        or baseline_total < SALES_STOP_MIN_BASELINE_UNITS
    ):
        return None
    return {
        "stop_started_on": zero_dates[0].isoformat(),
        "zero_sales_dates": [metric_date.isoformat() for metric_date in zero_dates],
        "baseline_start_on": baseline_dates[0].isoformat(),
        "baseline_end_on": baseline_dates[-1].isoformat(),
        "baseline_total_units": baseline_total,
        "baseline_selling_days": baseline_selling_days,
        "baseline_daily_average": round(
            baseline_total / SALES_STOP_BASELINE_DAYS,
            2,
        ),
    }


def _base_item(
    row: Mapping[str, Any],
    data_through: date | None,
    daily_sales: Mapping[date, int | None],
    zero_streak: Mapping[str, Any],
) -> dict[str, Any]:
    status = _text(row.get("status")) or "unknown"
    total_stock = _non_negative_integer(row.get("total_stock"))
    takealot_available = _non_negative_integer(row.get("takealot_available_stock"))
    seller_available = _non_negative_integer(row.get("seller_available_stock"))
    receiving = _non_negative_integer(row.get("takealot_stock_in_receiving"))
    on_way = _non_negative_integer(row.get("takealot_stock_on_way"))
    available_stock = max(total_stock, takealot_available + seller_available)
    latest_units = daily_sales.get(data_through) if data_through else None
    return {
        "offer_id": _text(row.get("offer_id")),
        "plid": _text(row.get("productline_id")),
        "tsin_id": _text(row.get("tsin_id")) or None,
        "sku": _text(row.get("sku")) or None,
        "title": _text(row.get("title")) or "未命名商品",
        "image_url": _text(row.get("image_url")) or None,
        "selling_price": _number(row.get("selling_price")),
        "page_views_30_days": _integer_or_none(row.get("page_views_30_days")),
        "conversion_percentage_30_days": _number(
            row.get("conversion_percentage_30_days")
        ),
        "offer_status": status,
        "offer_status_label": OFFER_STATUS_LABELS.get(status, status or "未知"),
        "available_stock": available_stock,
        "takealot_available_stock": takealot_available,
        "seller_available_stock": seller_available,
        "receiving_stock": receiving,
        "on_way_stock": on_way,
        # Only immediately sellable units make a non-buyable status anomalous.
        # Receiving and on-way units remain visible context but never count.
        "inventory_units": available_stock,
        "data_through": data_through.isoformat() if data_through else None,
        "latest_ordered_units": latest_units,
        "no_sales_days": int(zero_streak.get("days") or 0),
        "no_sales_days_exact": bool(zero_streak.get("exact")),
        "last_sale_on": zero_streak.get("last_sale_on"),
    }


def _available_stock_or_none(row: Mapping[str, Any]) -> int | None:
    total_stock = _optional_non_negative_integer(row.get("total_stock"))
    takealot_available = _optional_non_negative_integer(
        row.get("takealot_available_stock")
    )
    seller_available = _optional_non_negative_integer(
        row.get("seller_available_stock")
    )
    if (
        total_stock is None
        and takealot_available is None
        and seller_available is None
    ):
        return None
    component_stock = (takealot_available or 0) + (seller_available or 0)
    return max(total_stock or 0, component_stock)


def _state_verified_at(state: DailySalesMetricState) -> datetime | None:
    raw_value: object = state.verified_at
    if raw_value is None:
        details = state.source_details if isinstance(state.source_details, dict) else {}
        raw_value = details.get("verified_at") or details.get("collected_at")
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


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def _finite_number(value: object) -> TypeGuard[int | float]:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _non_negative_integer(value: object) -> int:
    return max(0, int(value)) if _finite_number(value) else 0


def _optional_non_negative_integer(value: object) -> int | None:
    return max(0, int(value)) if _finite_number(value) else None


def _integer_or_none(value: object) -> int | None:
    return int(value) if _finite_number(value) else None


def _number(value: object) -> float | None:
    return float(value) if _finite_number(value) else None
