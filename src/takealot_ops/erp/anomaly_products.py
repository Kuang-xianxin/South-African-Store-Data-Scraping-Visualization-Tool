"""Read-only, mutually separated anomaly-product projections for the Vue ERP."""

from __future__ import annotations

import math
from collections import OrderedDict, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Lock
from typing import Any, TypeGuard, cast
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from takealot_ops.dashboard.labels import OFFER_STATUS_LABELS
from takealot_ops.erp.returns import (
    RETURN_REASON_LABELS,
    load_return_collection_status,
    load_store_return_rows,
)
from takealot_ops.metrics.service import DashboardDataset
from takealot_ops.product_master import enrich_product_master_records
from takealot_ops.storage.models import (
    CompanyProduct,
    CollectionRun,
    CompetitorReview,
    DailyProductMetric,
    DailySalesMetricState,
    OfferSnapshot,
    PlatformSkuMapping,
    ReturnItem,
)


SLOW_DAY_OPTIONS = (4, 7, 10, 15, 20, 30)
SALES_STOP_ZERO_DAYS = 3
SALES_STOP_BASELINE_DAYS = 7
SALES_STOP_MIN_SELLING_DAYS = 5
SALES_STOP_MIN_BASELINE_UNITS = 7
BAD_REVIEW_RATING_BELOW = 5
POOR_REVIEW_MIN_BAD_COUNT = 5
POOR_REVIEW_MIN_BAD_RATE = 0.20
RETURN_WINDOW_DAYS = 30
HIGH_RETURN_MIN_UNITS = 5
SAST = ZoneInfo("Africa/Johannesburg")
CHINA = ZoneInfo("Asia/Shanghai")
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
    "captured_at",
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
    latest_review_seen_at: datetime | None
    review_count: int
    latest_return_capture_at: datetime | None
    return_item_count: int
    latest_return_run_at: datetime | None
    return_run_count: int
    latest_product_master_at: datetime | None
    product_master_revision_count: int


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
    current_plids = {
        normalized
        for value in dataset.offer_current.get(
            "productline_id",
            pd.Series(dtype="object"),
        )
        if (normalized := _text(value))
    }
    review_rows = _load_anomaly_review_rows(session, current_plids)
    return_start = requested_as_of - timedelta(days=RETURN_WINDOW_DAYS - 1)
    return_coverage = load_return_collection_status(
        session,
        start_date=return_start,
        end_date=requested_as_of,
    )
    return_coverage.update(
        {
            "window_start": return_start.isoformat(),
            "window_end": requested_as_of.isoformat(),
            "window_days": RETURN_WINDOW_DAYS,
        }
    )
    return_rows: list[dict[str, Any]] = []
    if return_coverage.get("data_status") in {"collected", "partial", "stale"}:
        return_rows = enrich_product_master_records(
            session,
            load_store_return_rows(
                session,
                start_date=return_start,
                end_date=requested_as_of,
            ),
            as_of_date=requested_as_of,
        )
    payload = build_anomaly_product_payload(
        dataset,
        requested_as_of=requested_as_of,
        completed_through=completed_through,
        verified_dates=verified_sales_metric_dates(states),
        review_rows=review_rows,
        return_rows=return_rows,
        return_coverage=return_coverage,
        collection_times=_revision_collection_times(revision),
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
        ).where(OfferSnapshot.snapshot_date <= requested_as_of)
    ).one()
    metric_state = session.execute(
        select(
            func.max(DailySalesMetricState.updated_at),
            func.count(DailySalesMetricState.id),
        ).where(DailySalesMetricState.metric_date <= requested_as_of)
    ).one()
    product_metric_count = int(
        session.scalar(
            select(func.count(DailyProductMetric.id)).where(
                DailyProductMetric.metric_date <= completed_through
            )
        )
        or 0
    )
    review_revision = session.execute(
        select(
            func.max(CompetitorReview.last_seen_at),
            func.count(CompetitorReview.id),
        )
    ).one()
    return_revision = session.execute(
        select(
            func.max(ReturnItem.captured_at),
            func.count(ReturnItem.seller_return_id),
        )
    ).one()
    return_run_revision = session.execute(
        select(
            func.max(func.coalesce(CollectionRun.finished_at, CollectionRun.started_at)),
            func.count(CollectionRun.run_id),
        ).where(CollectionRun.run_type == "returns")
    ).one()
    mapping_revision = session.execute(
        select(
            func.max(PlatformSkuMapping.updated_at),
            func.count(PlatformSkuMapping.id),
        )
    ).one()
    company_revision = session.execute(
        select(
            func.max(CompanyProduct.updated_at),
            func.count(CompanyProduct.id),
        )
    ).one()
    product_master_dates = [
        value
        for value in (mapping_revision[0], company_revision[0])
        if isinstance(value, datetime)
    ]
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
        latest_review_seen_at=(
            review_revision[0]
            if isinstance(review_revision[0], datetime)
            else None
        ),
        review_count=int(review_revision[1] or 0),
        latest_return_capture_at=(
            return_revision[0]
            if isinstance(return_revision[0], datetime)
            else None
        ),
        return_item_count=int(return_revision[1] or 0),
        latest_return_run_at=(
            return_run_revision[0]
            if isinstance(return_run_revision[0], datetime)
            else None
        ),
        return_run_count=int(return_run_revision[1] or 0),
        latest_product_master_at=(
            max(product_master_dates) if product_master_dates else None
        ),
        product_master_revision_count=(
            int(mapping_revision[1] or 0) + int(company_revision[1] or 0)
        ),
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
                OfferSnapshot.captured_at,
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


def _revision_collection_times(
    revision: AnomalyProductDataRevision,
) -> dict[str, str | None]:
    """Expose source pull timestamps; the client renders them in Beijing time."""

    return _normalize_collection_times(
        {
            "offers_at": _latest_datetime(
                revision.latest_offer_run_at,
                revision.latest_offer_capture_at,
            ),
            "sales_at": revision.latest_metric_state_at,
            "reviews_at": revision.latest_review_seen_at,
            "returns_at": _latest_datetime(
                revision.latest_return_capture_at,
                revision.latest_return_run_at,
            ),
        }
    )


def _load_anomaly_review_rows(
    session: Session,
    plids: set[str],
) -> list[dict[str, Any]]:
    """Load only review evidence linked to current own-store PLIDs."""

    if not plids:
        return []
    rows = session.execute(
        select(
            CompetitorReview.plid,
            CompetitorReview.review_id,
            CompetitorReview.rating,
            CompetitorReview.title,
            CompetitorReview.body,
            CompetitorReview.customer_name,
            CompetitorReview.review_date,
            CompetitorReview.first_seen_at,
            CompetitorReview.last_seen_at,
        )
        .where(CompetitorReview.plid.in_(plids))
        .order_by(
            CompetitorReview.plid,
            CompetitorReview.first_seen_at,
            CompetitorReview.review_id,
        )
    ).mappings()
    return [dict(row) for row in rows]


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
    review_rows: Iterable[Mapping[str, Any]] = (),
    return_rows: Iterable[Mapping[str, Any]] = (),
    return_coverage: Mapping[str, Any] | None = None,
    collection_times: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build independent anomaly groups without changing legacy risk records."""

    product_daily = _normalized_product_daily(dataset.product_daily, completed_through)
    offer_current = _normalized_offer_current(dataset.offer_current)
    normalized_collection_times = _normalize_collection_times(collection_times)
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
    base_items_by_offer: dict[str, dict[str, Any]] = {}
    base_items_by_plid: dict[str, dict[str, Any]] = {}
    base_items_by_sku: dict[str, dict[str, Any]] = {}

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
        item.update(
            {
                "offer_collected_at": (
                    _iso_datetime(row.get("captured_at"))
                    or normalized_collection_times["offers_at"]
                ),
                "sales_collected_at": normalized_collection_times["sales_at"],
                "review_collected_at": normalized_collection_times["reviews_at"],
                "return_collected_at": normalized_collection_times["returns_at"],
            }
        )
        base_items_by_offer[offer_id] = item
        _remember_representative(base_items_by_plid, plid, item)
        sku_key = _identity_key(item.get("sku"))
        if sku_key:
            _remember_representative(base_items_by_sku, sku_key, item)

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
    review_groups = _build_review_anomaly_groups(
        review_rows,
        base_items_by_plid,
        requested_as_of=requested_as_of,
    )
    normalized_return_coverage = _normalize_return_coverage(
        return_coverage,
        requested_as_of=requested_as_of,
    )
    return_product_totals = _build_return_product_totals(
        return_rows,
        base_items_by_offer=base_items_by_offer,
        base_items_by_plid=base_items_by_plid,
        base_items_by_sku=base_items_by_sku,
        coverage=normalized_return_coverage,
    )
    high_returns = [
        item
        for item in return_product_totals
        if int(item.get("return_units_30_days") or 0) >= HIGH_RETURN_MIN_UNITS
    ]

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
        "collection_times": normalized_collection_times,
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
            "bad_review_rating_below": BAD_REVIEW_RATING_BELOW,
            "daily_bad_review_basis": "first_seen_after_plid_review_baseline",
            "poor_review_min_bad_count": POOR_REVIEW_MIN_BAD_COUNT,
            "poor_review_min_bad_rate_percentage": round(
                POOR_REVIEW_MIN_BAD_RATE * 100,
                2,
            ),
            "poor_review_identity": "plid",
            "return_window_days": RETURN_WINDOW_DAYS,
            "high_return_min_units": HIGH_RETURN_MIN_UNITS,
            "high_return_identity": "company_sku",
            "high_return_source": "seller_returns_detail",
            "uncollected_returns_are_zero": False,
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
            "daily_bad_reviews": len(review_groups["daily_bad_reviews"]),
            "poor_review_quality": len(review_groups["poor_review_quality"]),
            "high_returns": len(high_returns),
        },
        "sudden_sales_stop": sudden_sales_stop,
        "stock_status_anomalies": stock_status_anomalies,
        "slow_moving": slow_moving,
        "daily_bad_reviews": review_groups["daily_bad_reviews"],
        "poor_review_quality": review_groups["poor_review_quality"],
        "review_discovery_through": review_groups["review_discovery_through"],
        "return_coverage": normalized_return_coverage,
        "high_returns": high_returns,
        # The web layer merges these company-SKU totals across authorized stores
        # before applying the threshold. Keeping sub-threshold totals here avoids
        # losing a cross-store 3 + 2 = 5 return anomaly.
        "return_product_totals": return_product_totals,
    }


def _remember_representative(
    target: dict[str, dict[str, Any]],
    key: str,
    item: Mapping[str, Any],
) -> None:
    current = target.get(key)
    candidate = dict(item)
    if current is None or _representative_score(candidate) > _representative_score(
        current
    ):
        target[key] = candidate


def _representative_score(item: Mapping[str, Any]) -> tuple[int, int, int, str]:
    return (
        int(bool(_text(item.get("plid")))),
        int(_text(item.get("offer_status")) == "buyable"),
        _non_negative_integer(item.get("available_stock")),
        _text(item.get("offer_id")),
    )


def _build_review_anomaly_groups(
    review_rows: Iterable[Mapping[str, Any]],
    base_items_by_plid: Mapping[str, Mapping[str, Any]],
    *,
    requested_as_of: date,
) -> dict[str, Any]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_review_keys: set[tuple[str, str]] = set()
    discovery_dates: list[date] = []
    for source in review_rows:
        plid = _text(source.get("plid"))
        review_id = _text(source.get("review_id"))
        first_seen_at = _datetime_or_none(source.get("first_seen_at"))
        rating = _integer_or_none(source.get("rating"))
        if (
            not plid
            or plid not in base_items_by_plid
            or not review_id
            or first_seen_at is None
            or rating is None
        ):
            continue
        first_seen_on = first_seen_at.astimezone(CHINA).date()
        if first_seen_on > requested_as_of:
            continue
        review_key = (plid, review_id)
        if review_key in seen_review_keys:
            continue
        seen_review_keys.add(review_key)
        row = dict(source)
        row["_first_seen_at"] = first_seen_at
        row["_first_seen_on"] = first_seen_on
        row["_rating"] = rating
        grouped[plid].append(row)
        discovery_dates.append(first_seen_on)

    daily_items: list[dict[str, Any]] = []
    quality_items: list[dict[str, Any]] = []
    for plid, rows in grouped.items():
        baseline_seen_at = min(row["_first_seen_at"] for row in rows)
        bad_rows = [
            row for row in rows if int(row["_rating"]) < BAD_REVIEW_RATING_BELOW
        ]
        daily_bad_rows = [
            row
            for row in bad_rows
            if row["_first_seen_on"] == requested_as_of
            and row["_first_seen_at"] > baseline_seen_at
        ]
        bad_rate = len(bad_rows) / len(rows) if rows else 0.0
        rating_counts = {
            str(rating): sum(int(row["_rating"]) == rating for row in bad_rows)
            for rating in range(1, BAD_REVIEW_RATING_BELOW)
        }
        recent_bad_reviews = [
            _review_record(row)
            for row in sorted(
                bad_rows,
                key=lambda row: (
                    row["_first_seen_at"],
                    _text(row.get("review_id")),
                ),
                reverse=True,
            )[:5]
        ]
        shared = dict(base_items_by_plid[plid])
        shared.update(
            {
                "review_count": len(rows),
                "bad_review_count": len(bad_rows),
                "bad_review_rate_percentage": round(bad_rate * 100, 2),
                "bad_review_rating_counts": rating_counts,
                "review_baseline_first_seen_at": _iso_datetime(baseline_seen_at),
                "recent_bad_reviews": recent_bad_reviews,
            }
        )
        if daily_bad_rows:
            daily_item = dict(shared)
            daily_reviews = [
                _review_record(row)
                for row in sorted(
                    daily_bad_rows,
                    key=lambda row: (
                        int(row["_rating"]),
                        row["_first_seen_at"],
                        _text(row.get("review_id")),
                    ),
                )
            ]
            daily_item.update(
                {
                    "anomaly_type": "daily_bad_review",
                    "anomaly_label": "当日新发现低于五星评论",
                    "new_bad_review_count": len(daily_reviews),
                    "new_bad_reviews": daily_reviews,
                    "review_discovered_on": requested_as_of.isoformat(),
                }
            )
            daily_items.append(daily_item)
        if (
            len(bad_rows) >= POOR_REVIEW_MIN_BAD_COUNT
            and bad_rate >= POOR_REVIEW_MIN_BAD_RATE
        ):
            quality_item = dict(shared)
            quality_item.update(
                {
                    "anomaly_type": "poor_review_quality",
                    "anomaly_label": "累计低于五星评论偏高",
                }
            )
            quality_items.append(quality_item)

    daily_items.sort(
        key=lambda item: (
            -int(item.get("new_bad_review_count") or 0),
            -int(item.get("bad_review_count") or 0),
            _text(item.get("title")),
        )
    )
    quality_items.sort(
        key=lambda item: (
            -int(item.get("bad_review_count") or 0),
            -float(item.get("bad_review_rate_percentage") or 0),
            _text(item.get("title")),
        )
    )
    return {
        "daily_bad_reviews": daily_items,
        "poor_review_quality": quality_items,
        "review_discovery_through": (
            max(discovery_dates).isoformat() if discovery_dates else None
        ),
    }


def _review_record(row: Mapping[str, Any]) -> dict[str, Any]:
    first_seen_at = _datetime_or_none(row.get("_first_seen_at"))
    first_seen_on = row.get("_first_seen_on")
    return {
        "review_id": _text(row.get("review_id")),
        "rating": _integer_or_none(row.get("_rating")),
        "title": _text(row.get("title")) or None,
        "body": _text(row.get("body")) or None,
        "customer_name": _text(row.get("customer_name")) or None,
        "review_date": _text(row.get("review_date")) or None,
        "first_seen_at": _iso_datetime(first_seen_at),
        "first_seen_on": (
            first_seen_on.isoformat() if isinstance(first_seen_on, date) else None
        ),
    }


def _normalize_return_coverage(
    coverage: Mapping[str, Any] | None,
    *,
    requested_as_of: date,
) -> dict[str, Any]:
    window_start = requested_as_of - timedelta(days=RETURN_WINDOW_DAYS - 1)
    normalized = dict(coverage or {})
    status = _text(normalized.get("data_status"))
    if status not in {"collected", "partial", "stale", "failed", "uncollected"}:
        status = "uncollected"
    normalized.update(
        {
            "data_status": status,
            "window_start": _text(normalized.get("window_start"))
            or window_start.isoformat(),
            "window_end": _text(normalized.get("window_end"))
            or requested_as_of.isoformat(),
            "window_days": RETURN_WINDOW_DAYS,
            "source": "seller_returns_detail",
            "uncollected_is_zero": False,
        }
    )
    return normalized


def _build_return_product_totals(
    return_rows: Iterable[Mapping[str, Any]],
    *,
    base_items_by_offer: Mapping[str, Mapping[str, Any]],
    base_items_by_plid: Mapping[str, Mapping[str, Any]],
    base_items_by_sku: Mapping[str, Mapping[str, Any]],
    coverage: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if coverage.get("data_status") not in {"collected", "partial", "stale"}:
        return []
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for source in return_rows:
        company_sku = _text(source.get("company_sku"))
        quantity = _non_negative_integer(source.get("quantity"))
        if not company_sku or quantity <= 0:
            continue
        row = dict(source)
        row["_quantity"] = quantity
        grouped[_identity_key(company_sku)].append(row)

    totals: list[dict[str, Any]] = []
    for rows in grouped.values():
        representative_row = max(
            rows,
            key=lambda row: (
                int(bool(_text(row.get("productline_id")))),
                int(row.get("_quantity") or 0),
                _text(row.get("return_date")),
            ),
        )
        base = _base_item_for_return(
            representative_row,
            base_items_by_offer=base_items_by_offer,
            base_items_by_plid=base_items_by_plid,
            base_items_by_sku=base_items_by_sku,
            data_through=_text(coverage.get("window_end")) or None,
        )
        company_sku = _text(representative_row.get("company_sku"))
        company_product_name = next(
            (
                _text(row.get("company_product_name"))
                for row in rows
                if _text(row.get("company_product_name"))
            ),
            "",
        )
        platform_skus = sorted(
            {_text(row.get("sku")) for row in rows if _text(row.get("sku"))}
        )
        offer_ids = sorted(
            {
                _text(row.get("offer_id"))
                for row in rows
                if _text(row.get("offer_id"))
            }
        )
        plids = sorted(
            {
                _text(row.get("productline_id"))
                for row in rows
                if _text(row.get("productline_id"))
            }
        )
        reason_groups: defaultdict[str, dict[str, Any]] = defaultdict(
            lambda: {"units": 0, "records": 0}
        )
        for row in rows:
            reason = _text(row.get("return_reason")) or "unknown"
            reason_group = reason_groups[reason]
            reason_group["units"] += int(row.get("_quantity") or 0)
            reason_group["records"] += 1
        reason_counts: list[dict[str, Any]] = [
            {
                "reason": reason,
                "label": RETURN_REASON_LABELS.get(reason, reason),
                "units": int(values["units"]),
                "records": int(values["records"]),
            }
            for reason, values in reason_groups.items()
        ]
        reason_counts.sort(
            key=lambda item: (-int(item["units"]), -int(item["records"]), item["label"])
        )
        recent_returns = [
            {
                "seller_return_id": _text(row.get("seller_return_id")),
                "return_date": _text(row.get("return_date")) or None,
                "quantity": int(row.get("_quantity") or 0),
                "return_reason": _text(row.get("return_reason")) or None,
                "return_reason_label": _text(row.get("return_reason_label"))
                or "未提供原因",
                "customer_comment": _text(row.get("customer_comment")) or None,
                "sku": _text(row.get("sku")) or None,
                "plid": _text(row.get("productline_id")) or None,
            }
            for row in sorted(
                rows,
                key=lambda row: (
                    _text(row.get("return_date")),
                    _text(row.get("seller_return_id")),
                ),
                reverse=True,
            )[:5]
        ]
        base.update(
            {
                "anomaly_type": "high_return_volume",
                "anomaly_label": "近30日公司SKU退货偏高",
                "company_sku": company_sku,
                "company_product_name": company_product_name or None,
                "title": company_product_name or base.get("title") or company_sku,
                "return_units_30_days": sum(
                    int(row.get("_quantity") or 0) for row in rows
                ),
                "return_record_count": len(rows),
                "affected_platform_sku_count": len(platform_skus),
                "platform_skus": platform_skus,
                "offer_ids": offer_ids,
                "plids": plids,
                "return_reason_counts": reason_counts,
                "recent_returns": recent_returns,
                "return_window_start": coverage.get("window_start"),
                "return_window_end": coverage.get("window_end"),
                "return_data_status": coverage.get("data_status"),
            }
        )
        totals.append(base)
    totals.sort(
        key=lambda item: (
            -int(item.get("return_units_30_days") or 0),
            _text(item.get("company_sku")),
        )
    )
    return totals


def _base_item_for_return(
    row: Mapping[str, Any],
    *,
    base_items_by_offer: Mapping[str, Mapping[str, Any]],
    base_items_by_plid: Mapping[str, Mapping[str, Any]],
    base_items_by_sku: Mapping[str, Mapping[str, Any]],
    data_through: str | None,
) -> dict[str, Any]:
    offer_id = _text(row.get("offer_id"))
    plid = _text(row.get("productline_id"))
    sku = _text(row.get("sku"))
    matched = (
        base_items_by_offer.get(offer_id)
        or base_items_by_plid.get(plid)
        or base_items_by_sku.get(_identity_key(sku))
    )
    if matched is not None:
        return dict(matched)
    return {
        "offer_id": offer_id,
        "plid": plid,
        "tsin_id": _text(row.get("tsin_id")) or None,
        "sku": sku or None,
        "title": _text(row.get("product_title")) or "未命名商品",
        "image_url": _text(row.get("image_url")) or None,
        "selling_price": None,
        "page_views_30_days": None,
        "conversion_percentage_30_days": None,
        "offer_status": "unknown",
        "offer_status_label": "身份来自退货明细",
        "available_stock": 0,
        "takealot_available_stock": 0,
        "seller_available_stock": 0,
        "receiving_stock": 0,
        "on_way_stock": 0,
        "inventory_units": 0,
        "data_through": data_through,
        "latest_ordered_units": None,
        "no_sales_days": 0,
        "no_sales_days_exact": False,
        "last_sale_on": None,
    }


def merge_review_anomaly_items(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Deduplicate global PLID review evidence repeated across selected stores."""

    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        plid = _text(record.get("plid"))
        if plid:
            grouped[plid].append(dict(record))
    merged: list[dict[str, Any]] = []
    for plid, items in grouped.items():
        representative = max(items, key=_representative_score)
        result = dict(representative)
        store_codes = sorted(
            {_text(item.get("store_code")) for item in items if _text(item.get("store_code"))}
        )
        store_names = sorted(
            {_text(item.get("store_name")) for item in items if _text(item.get("store_name"))}
        )
        platform_skus = sorted(
            {_text(item.get("sku")) for item in items if _text(item.get("sku"))}
        )
        company_skus = sorted(
            {
                _text(item.get("company_sku"))
                for item in items
                if _text(item.get("company_sku"))
            }
        )
        result.update(
            {
                "store_codes": store_codes,
                "store_names": store_names,
                "store_name": "、".join(store_names) or result.get("store_name"),
                "platform_skus": platform_skus,
                "company_skus": company_skus,
                "store_scope_key": f"reviews:{plid}",
                "new_bad_reviews": _dedupe_review_records(
                    items,
                    field="new_bad_reviews",
                ),
                "recent_bad_reviews": _dedupe_review_records(
                    items,
                    field="recent_bad_reviews",
                )[:5],
                "review_count": max(
                    int(item.get("review_count") or 0) for item in items
                ),
                "bad_review_count": max(
                    int(item.get("bad_review_count") or 0) for item in items
                ),
            }
        )
        result["new_bad_review_count"] = len(result["new_bad_reviews"])
        review_count = int(result.get("review_count") or 0)
        result["bad_review_rate_percentage"] = round(
            int(result.get("bad_review_count") or 0) / review_count * 100,
            2,
        ) if review_count else 0.0
        merged.append(result)
    merged.sort(
        key=lambda item: (
            -int(item.get("new_bad_review_count") or 0),
            -int(item.get("bad_review_count") or 0),
            _text(item.get("title")),
        )
    )
    return merged


def _dedupe_review_records(
    items: Sequence[Mapping[str, Any]],
    *,
    field: str,
) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for item in items:
        values = item.get(field)
        if not isinstance(values, list):
            continue
        for raw_review in values:
            if not isinstance(raw_review, Mapping):
                continue
            review_id = _text(raw_review.get("review_id"))
            if review_id:
                deduped.setdefault(review_id, dict(raw_review))
    return sorted(
        deduped.values(),
        key=lambda review: (
            _integer_or_none(review.get("rating")) or 0,
            _text(review.get("first_seen_at")),
            _text(review.get("review_id")),
        ),
    )


def merge_return_anomaly_items(
    records: Sequence[Mapping[str, Any]],
    *,
    minimum_units: int = HIGH_RETURN_MIN_UNITS,
) -> list[dict[str, Any]]:
    """Combine store totals by company SKU, then apply the return threshold."""

    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        company_sku = _text(record.get("company_sku"))
        if company_sku:
            grouped[_identity_key(company_sku)].append(dict(record))
    merged: list[dict[str, Any]] = []
    for items in grouped.values():
        total_units = sum(int(item.get("return_units_30_days") or 0) for item in items)
        if total_units < minimum_units:
            continue
        representative = max(
            items,
            key=lambda item: (
                int(bool(_text(item.get("plid")))),
                int(item.get("return_units_30_days") or 0),
                _representative_score(item),
            ),
        )
        result = dict(representative)
        store_codes = sorted(
            {_text(item.get("store_code")) for item in items if _text(item.get("store_code"))}
        )
        store_names = sorted(
            {_text(item.get("store_name")) for item in items if _text(item.get("store_name"))}
        )
        platform_skus = sorted(
            {
                _text(value)
                for item in items
                for value in (item.get("platform_skus") or [])
                if _text(value)
            }
        )
        plids = sorted(
            {
                _text(value)
                for item in items
                for value in (item.get("plids") or [])
                if _text(value)
            }
        )
        reason_totals: defaultdict[str, dict[str, Any]] = defaultdict(
            lambda: {"units": 0, "records": 0, "label": ""}
        )
        recent_returns: dict[str, dict[str, Any]] = {}
        store_statuses: dict[str, str] = {}
        for item in items:
            store_code = _text(item.get("store_code"))
            if store_code:
                store_statuses[store_code] = _text(item.get("return_data_status"))
            for raw_reason in item.get("return_reason_counts") or []:
                if not isinstance(raw_reason, Mapping):
                    continue
                reason = _text(raw_reason.get("reason")) or "unknown"
                target = reason_totals[reason]
                target["label"] = _text(raw_reason.get("label")) or reason
                target["units"] += int(raw_reason.get("units") or 0)
                target["records"] += int(raw_reason.get("records") or 0)
            for raw_return in item.get("recent_returns") or []:
                if not isinstance(raw_return, Mapping):
                    continue
                seller_return_id = _text(raw_return.get("seller_return_id"))
                key = f"{store_code}:{seller_return_id}"
                decorated = dict(raw_return)
                decorated["store_code"] = store_code or None
                decorated["store_name"] = item.get("store_name")
                recent_returns.setdefault(key, decorated)
        reason_counts = [
            {
                "reason": reason,
                "label": values["label"],
                "units": int(values["units"]),
                "records": int(values["records"]),
            }
            for reason, values in reason_totals.items()
        ]
        reason_counts.sort(
            key=lambda item: (-int(item["units"]), -int(item["records"]), item["label"])
        )
        recent = sorted(
            recent_returns.values(),
            key=lambda item: (
                _text(item.get("return_date")),
                _text(item.get("seller_return_id")),
            ),
            reverse=True,
        )[:5]
        result.update(
            {
                "return_units_30_days": total_units,
                "return_record_count": sum(
                    int(item.get("return_record_count") or 0) for item in items
                ),
                "affected_platform_sku_count": len(platform_skus),
                "platform_skus": platform_skus,
                "plids": plids,
                "return_reason_counts": reason_counts,
                "recent_returns": recent,
                "store_codes": store_codes,
                "store_names": store_names,
                "store_name": "、".join(store_names) or result.get("store_name"),
                "return_store_statuses": store_statuses,
                "return_data_status": (
                    "partial"
                    if "partial" in store_statuses.values()
                    else "stale"
                    if "stale" in store_statuses.values()
                    else "collected"
                ),
                "store_scope_key": f"returns:{_identity_key(result.get('company_sku'))}",
            }
        )
        merged.append(result)
    merged.sort(
        key=lambda item: (
            -int(item.get("return_units_30_days") or 0),
            _text(item.get("company_sku")),
        )
    )
    return merged


def merge_return_coverage(
    store_coverages: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    statuses = [_text(item.get("data_status")) or "uncollected" for item in store_coverages]
    covered = {"collected", "stale"}
    covered_count = sum(status in covered for status in statuses)
    if statuses and all(status == "collected" for status in statuses):
        data_status = "collected"
    elif statuses and covered_count == len(statuses):
        data_status = "stale"
    elif covered_count or "partial" in statuses:
        data_status = "partial"
    elif "failed" in statuses:
        data_status = "failed"
    else:
        data_status = "uncollected"
    first = store_coverages[0] if store_coverages else {}
    return {
        "data_status": data_status,
        "window_start": first.get("window_start"),
        "window_end": first.get("window_end"),
        "window_days": RETURN_WINDOW_DAYS,
        "source": "seller_returns_detail",
        "uncollected_is_zero": False,
        "covered_store_count": covered_count,
        "store_count": len(store_coverages),
        "stores": [dict(item) for item in store_coverages],
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


def _normalize_collection_times(
    values: Mapping[str, Any] | None,
) -> dict[str, str | None]:
    source = values or {}
    normalized = {
        key: _iso_datetime(source.get(key))
        for key in ("offers_at", "sales_at", "reviews_at", "returns_at")
    }
    normalized["latest_at"] = _iso_datetime(
        _latest_datetime(source.get("latest_at"), *normalized.values())
    )
    return normalized


def _latest_datetime(*values: object) -> datetime | None:
    normalized = [
        parsed for value in values if (parsed := _datetime_or_none(value)) is not None
    ]
    return max(normalized) if normalized else None


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def _identity_key(value: object) -> str:
    return _text(value).casefold()


def _datetime_or_none(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso_datetime(value: object) -> str | None:
    parsed = _datetime_or_none(value)
    return parsed.isoformat() if parsed is not None else None


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
