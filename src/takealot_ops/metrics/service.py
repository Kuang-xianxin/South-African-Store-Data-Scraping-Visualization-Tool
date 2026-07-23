"""Daily sales, offer, traffic, and anomaly calculations."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pandas as pd
import yaml

from takealot_ops.storage.models import (
    AnomalyEvent,
    DailyProductMetric,
    DataQualityEvent,
    OfferSnapshot,
    SaleItem,
)
from takealot_ops.storage.repository import Repository


PRODUCT_DAILY_COLUMNS = (
    "metric_date",
    "offer_id",
    "sku",
    "ordered_units",
    "effective_units",
    "ordered_revenue",
    "page_views_30_days",
    "page_views_30_day_average",
    "page_views_window_net_change",
    "conversion_percentage_30_days",
    "conversion_percentage_previous_30_days",
    "conversion_change_points",
    "total_stock",
    "offer_status",
)

METRIC_METADATA: dict[str, dict[str, str]] = {
    "page_views_30_days": {"label": "近30天浏览量", "nature": "API 30-day window value"},
    "page_views_30_day_average": {
        "label": "近30天日均浏览量",
        "nature": "derived average of the 30-day window",
    },
    "page_views_window_net_change": {
        "label": "30天浏览量窗口净变化",
        "nature": "snapshot-window net change for trend reference",
    },
}

METRIC_ANOMALY_TYPES = (
    "sales_drop",
    "sales_spike",
    "high_views_low_conversion",
    "low_views_high_conversion",
    "suspected_stockout",
    "non_buyable",
    "stale_offer_snapshot",
    "unknown_sale_status",
)

_STORE_DAILY_COLUMNS = (
    "metric_date",
    "ordered_units",
    "effective_units",
    "ordered_revenue",
)
_OFFER_CURRENT_COLUMNS = (
    "offer_id",
    "tsin_id",
    "sku",
    "barcode",
    "title",
    "selling_price",
    "rrp",
    "benchmark_price",
    "status",
    "image_url",
    "productline_id",
    "conversion_percentage_30_days",
    "conversion_percentage_previous_30_days",
    "page_views_30_days",
    "quantity_returned_30_days",
    "total_wishlist",
    "wishlist_30_days",
    "listing_quality",
    "discount_percentage",
    "updated_at",
    "captured_at",
    "total_stock",
)
_ANOMALY_COLUMNS = (
    "event_date",
    "offer_id",
    "anomaly_type",
    "severity",
    "explanation",
    "details",
    "created_at",
)
_QUALITY_COLUMNS = (
    "event_id",
    "event_date",
    "event_type",
    "severity",
    "offer_id",
    "details",
    "created_at",
)


@dataclass(frozen=True)
class DashboardDataset:
    """Stable data-frame bundle consumed by dashboards and exports."""

    store_daily: pd.DataFrame
    product_daily: pd.DataFrame
    offer_current: pd.DataFrame
    anomalies: pd.DataFrame
    quality_events: pd.DataFrame


def latest_metric_anomalies(
    dataset: DashboardDataset,
) -> tuple[date | None, pd.DataFrame]:
    """Return anomaly records for the latest available product metric date only."""
    product_daily = dataset.product_daily
    anomalies = dataset.anomalies
    if product_daily.empty or "metric_date" not in product_daily.columns:
        return None, anomalies.iloc[0:0].copy()
    metric_dates = pd.to_datetime(product_daily["metric_date"], errors="coerce").dt.date
    valid_metric_dates = metric_dates.dropna()
    if valid_metric_dates.empty:
        return None, anomalies.iloc[0:0].copy()
    latest_date = valid_metric_dates.max()
    if anomalies.empty or "event_date" not in anomalies.columns:
        return latest_date, anomalies.iloc[0:0].copy()
    event_dates = pd.to_datetime(anomalies["event_date"], errors="coerce").dt.date
    return latest_date, anomalies.loc[event_dates == latest_date].copy()


@dataclass(frozen=True)
class _AnomalyRules:
    drop_baseline_days: int
    drop_minimum_baseline: float
    drop_fraction: float
    spike_baseline_days: int
    spike_minimum_quantity: int
    spike_fraction: float
    high_views_percentile: int
    low_conversion_percentile: int
    low_views_percentile: int
    high_conversion_percentile: int
    stale_hours: int


@dataclass
class _SaleAggregate:
    ordered_units: int = 0
    effective_units: int = 0
    ordered_revenue: Decimal = Decimal("0")


class MetricService:
    """Build durable daily metrics from typed repository rows."""

    def __init__(
        self,
        repository: Repository,
        *,
        anomaly_rules_path: Path = Path("config/anomaly_rules.yaml"),
        sale_status_rules_path: Path = Path("config/sale_status_rules.yaml"),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._rules = _load_anomaly_rules(anomaly_rules_path)
        self._included, self._excluded = _load_status_rules(sale_status_rules_path)
        self._now = now or (lambda: datetime.now(UTC))

    def rebuild(self, start: date, end: date) -> int:
        """Atomically replace daily metrics and events for an inclusive range."""
        if start > end:
            raise ValueError("start must be on or before end")
        lookback_days = max(self._rules.drop_baseline_days, self._rules.spike_baseline_days)
        with self._repository.transaction():
            sales = self._repository.list_sales(start - timedelta(days=lookback_days), end)
            snapshots = self._repository.list_offer_snapshots_through(end)
            scope_dates = self._repository.list_successful_offer_scope_dates(end)
            product_rows, anomalies, quality_events = self._calculate(
                start, end, sales, snapshots, scope_dates
            )
            self._repository.replace_metric_range(
                start,
                end,
                product_metrics=product_rows,
                anomalies=anomalies,
                quality_events=quality_events,
                anomaly_types=METRIC_ANOMALY_TYPES,
            )
        return len(product_rows)

    def dashboard_dataset(self, as_of: date) -> DashboardDataset:
        """Return stable frames containing only information known by ``as_of``."""
        with self._repository.transaction():
            product_rows = self._repository.list_daily_product_metrics(as_of)
            snapshots = self._repository.list_offer_snapshots_through(as_of)
            scope_dates = self._repository.list_successful_offer_scope_dates(as_of)
            anomalies = self._repository.list_anomalies(as_of)
            quality_events = self._repository.list_quality_events(as_of)
            product_daily = _product_frame(product_rows)
            offer_current = _offer_frame(
                _batch_snapshots(snapshots, _latest_scope(scope_dates, as_of))
            )
            anomaly_frame = _anomaly_frame(anomalies)
            quality_frame = _quality_frame(quality_events)
        return DashboardDataset(
            store_daily=_store_frame(product_daily),
            product_daily=product_daily,
            offer_current=offer_current,
            anomalies=anomaly_frame,
            quality_events=quality_frame,
        )

    def _calculate(
        self,
        start: date,
        end: date,
        sales: Sequence[SaleItem],
        snapshots: Sequence[OfferSnapshot],
        scope_dates: Sequence[date],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        aggregates: dict[tuple[date, str], _SaleAggregate] = {}
        unknown_statuses: dict[tuple[date, str], set[str | None]] = {}
        sale_skus: dict[str, str | None] = {}
        for sale in sales:
            if sale.offer_id is None:
                continue
            key = (sale.sales_day, sale.offer_id)
            aggregate = aggregates.setdefault(key, _SaleAggregate())
            aggregate.ordered_units += sale.quantity
            # The Sales API returns the full order-item line value, already
            # reflecting ``quantity``. Multiplying it again overstates multi-unit sales.
            aggregate.ordered_revenue += sale.selling_price or Decimal("0")
            if sale.sale_status in self._included:
                aggregate.effective_units += sale.quantity
            elif sale.sale_status not in self._excluded:
                unknown_statuses.setdefault(key, set()).add(sale.sale_status)
            sale_skus.setdefault(sale.offer_id, sale.sku)

        snapshots_by_key = {
            (snapshot.snapshot_date, snapshot.offer_id): snapshot for snapshot in snapshots
        }
        product_rows: list[dict[str, Any]] = []
        for metric_date in _date_range(start, end):
            active_batch = _batch_snapshots(snapshots, _latest_scope(scope_dates, metric_date))
            known_by_date = {snapshot.offer_id: snapshot for snapshot in active_batch}
            daily_sale_ids = {
                offer_id for sales_day, offer_id in aggregates if sales_day == metric_date
            }
            for offer_id in sorted(set(known_by_date) | daily_sale_ids):
                exact_snapshot = snapshots_by_key.get((metric_date, offer_id))
                previous = snapshots_by_key.get((metric_date - timedelta(days=1), offer_id))
                aggregate = aggregates.get((metric_date, offer_id), _SaleAggregate())
                product_rows.append(
                    _product_values(
                        metric_date,
                        offer_id,
                        aggregate,
                        exact_snapshot,
                        previous,
                        known_by_date.get(offer_id),
                        sale_skus.get(offer_id),
                    )
                )

        created_at = _aware_utc(self._now())
        quality_events = _quality_values(unknown_statuses, start, end, created_at)
        anomalies = self._anomaly_values(
            start,
            end,
            product_rows,
            aggregates,
            unknown_statuses,
            {
                snapshot.offer_id: snapshot
                for snapshot in _batch_snapshots(snapshots, _latest_scope(scope_dates, end))
            },
            created_at,
        )
        return product_rows, anomalies, quality_events

    def _anomaly_values(
        self,
        start: date,
        end: date,
        product_rows: Sequence[Mapping[str, Any]],
        aggregates: Mapping[tuple[date, str], _SaleAggregate],
        unknown_statuses: Mapping[tuple[date, str], set[str | None]],
        current_by_offer: Mapping[str, OfferSnapshot],
        created_at: datetime,
    ) -> list[dict[str, Any]]:
        anomalies: list[dict[str, Any]] = []
        rows_by_date: dict[date, list[Mapping[str, Any]]] = {}
        for row in product_rows:
            metric_date = row["metric_date"]
            if isinstance(metric_date, date):
                rows_by_date.setdefault(metric_date, []).append(row)

        for metric_date in _date_range(start, end):
            dated_rows = rows_by_date.get(metric_date, [])
            views = [
                float(row["page_views_30_days"])
                for row in dated_rows
                if row["page_views_30_days"] is not None
            ]
            conversions = [
                float(row["conversion_percentage_30_days"])
                for row in dated_rows
                if row["conversion_percentage_30_days"] is not None
            ]
            high_views = _quantile(views, self._rules.high_views_percentile)
            low_views = _quantile(views, self._rules.low_views_percentile)
            low_conversion = _quantile(conversions, self._rules.low_conversion_percentile)
            high_conversion = _quantile(conversions, self._rules.high_conversion_percentile)
            for row in dated_rows:
                offer_id = str(row["offer_id"])
                today_units = aggregates.get(
                    (metric_date, offer_id), _SaleAggregate()
                ).ordered_units
                drop_baseline = _average_units(
                    aggregates,
                    offer_id,
                    metric_date,
                    self._rules.drop_baseline_days,
                )
                if (
                    drop_baseline >= self._rules.drop_minimum_baseline
                    and today_units <= drop_baseline * (1 - self._rules.drop_fraction)
                ):
                    anomalies.append(
                        _anomaly(
                            metric_date,
                            offer_id,
                            "sales_drop",
                            "Ordered units dropped against the previous baseline.",
                            {"baseline_daily_units": drop_baseline, "ordered_units": today_units},
                            created_at,
                        )
                    )
                spike_baseline = _average_units(
                    aggregates,
                    offer_id,
                    metric_date,
                    self._rules.spike_baseline_days,
                )
                if (
                    today_units >= self._rules.spike_minimum_quantity
                    and today_units >= spike_baseline * (1 + self._rules.spike_fraction)
                ):
                    anomalies.append(
                        _anomaly(
                            metric_date,
                            offer_id,
                            "sales_spike",
                            "Ordered units increased against the previous baseline.",
                            {"baseline_daily_units": spike_baseline, "ordered_units": today_units},
                            created_at,
                        )
                    )
                _append_traffic_anomalies(
                    anomalies,
                    row,
                    metric_date,
                    offer_id,
                    high_views,
                    low_views,
                    low_conversion,
                    high_conversion,
                    created_at,
                )
                total_stock = row["total_stock"]
                offer_status = row["offer_status"]
                recent_sales = sum(
                    aggregates.get(
                        (metric_date - timedelta(days=offset), offer_id), _SaleAggregate()
                    ).ordered_units
                    for offset in range(1, 8)
                )
                if total_stock == 0 and (offer_status == "buyable" or recent_sales > 0):
                    anomalies.append(
                        _anomaly(
                            metric_date,
                            offer_id,
                            "suspected_stockout",
                            "Visible stock is zero while the offer is sellable or recently sold.",
                            {"recent_7_day_units": recent_sales},
                            created_at,
                        )
                    )
                if offer_status != "buyable":
                    anomalies.append(
                        _anomaly(
                            metric_date,
                            offer_id,
                            "non_buyable",
                            "Offer status is not buyable.",
                            {"offer_status": offer_status},
                            created_at,
                        )
                    )

        for (event_date, offer_id), statuses in unknown_statuses.items():
            if start <= event_date <= end:
                anomalies.append(
                    _anomaly(
                        event_date,
                        offer_id,
                        "unknown_sale_status",
                        "One or more sale statuses are not configured.",
                        {"sale_statuses": _display_statuses(statuses)},
                        created_at,
                    )
                )
        for offer_id, current in current_by_offer.items():
            captured_at = _aware_utc(current.captured_at)
            if created_at - captured_at > timedelta(hours=self._rules.stale_hours):
                anomalies.append(
                    _anomaly(
                        end,
                        offer_id,
                        "stale_offer_snapshot",
                        "The latest offer snapshot is older than the configured threshold.",
                        {"stale_hours_threshold": self._rules.stale_hours},
                        created_at,
                    )
                )
        return anomalies


def classify_quadrants(frame: pd.DataFrame, percentile: int = 50) -> pd.DataFrame:
    """Classify products using a deterministic 25th, 50th, or 75th boundary."""
    if percentile not in {25, 50, 75}:
        raise ValueError("percentile must be one of 25, 50, or 75")
    result = frame.copy()
    if result.empty:
        result["quadrant"] = pd.Series(dtype="object")
        result.attrs = {
            "page_views_boundary": None,
            "ordered_units_boundary": None,
            "percentile": percentile,
        }
        return result
    view_values = pd.to_numeric(result["page_views_30_days"], errors="coerce")
    unit_values = pd.to_numeric(result["ordered_units"], errors="coerce")
    view_boundary = float(view_values.dropna().quantile(percentile / 100, interpolation="lower"))
    positive_units = unit_values.loc[unit_values > 0].dropna()
    unit_boundary = (
        float(positive_units.quantile(percentile / 100, interpolation="lower"))
        if not positive_units.empty
        else 1.0
    )
    view_ranks = view_values.rank(method="average", pct=True).mul(100)
    unit_ranks = pd.Series(float("nan"), index=result.index, dtype="float64")
    unit_ranks.loc[unit_values.notna() & (unit_values <= 0)] = 0.0
    unit_ranks.loc[positive_units.index] = positive_units.rank(method="average", pct=True).mul(100)
    quadrants: list[str] = []
    for view_value, unit_value in zip(view_values, unit_values, strict=True):
        if pd.isna(view_value) or pd.isna(unit_value):
            quadrants.append("unclassified")
        elif float(view_value) >= view_boundary and float(unit_value) >= unit_boundary:
            quadrants.append("star")
        elif float(view_value) >= view_boundary:
            quadrants.append("conversion_issue")
        elif float(unit_value) >= unit_boundary:
            quadrants.append("potential")
        else:
            quadrants.append("optimize")
    result["quadrant"] = quadrants
    result["page_views_rank"] = view_ranks
    result["ordered_units_rank"] = unit_ranks
    view_rank_boundary = view_ranks.loc[view_values >= view_boundary].min()
    unit_rank_boundary = unit_ranks.loc[unit_values >= unit_boundary].min()
    result.attrs = {
        "page_views_boundary": view_boundary,
        "ordered_units_boundary": unit_boundary,
        "page_views_rank_boundary": float(view_rank_boundary),
        "ordered_units_rank_boundary": (
            float(unit_rank_boundary) if pd.notna(unit_rank_boundary) else 50.0
        ),
        "percentile": percentile,
    }
    return result


def build_quadrant_window(frame: pd.DataFrame, as_of: date, days: int = 7) -> pd.DataFrame:
    """Combine latest traffic snapshots with calendar-window ordered-unit totals."""
    required = {"metric_date", "offer_id", "ordered_units", "page_views_30_days"}
    if frame.empty or not required.issubset(frame.columns):
        return frame.iloc[0:0].copy()
    scoped = frame.copy()
    metric_dates = pd.to_datetime(scoped["metric_date"], errors="coerce").dt.date
    scoped = scoped.loc[metric_dates <= as_of].copy()
    metric_dates = metric_dates.loc[scoped.index]
    if scoped.empty or metric_dates.dropna().empty:
        return scoped.iloc[0:0].copy()
    window_end = metric_dates.dropna().max()
    window_start = window_end - timedelta(days=days - 1)
    latest = (
        scoped.assign(_metric_date=metric_dates)
        .sort_values("_metric_date")
        .drop_duplicates("offer_id", keep="last")
        .drop(columns="_metric_date")
    )
    window = scoped.loc[(metric_dates >= window_start) & (metric_dates <= window_end)].copy()
    window["ordered_units"] = pd.to_numeric(window["ordered_units"], errors="coerce")
    totals = (
        window.groupby("offer_id")["ordered_units"]
        .sum(min_count=1)
        .rename("ordered_units_window")
        .reset_index()
    )
    latest = latest.drop(columns="ordered_units").merge(totals, on="offer_id", how="left")
    latest = latest.rename(columns={"ordered_units_window": "ordered_units"})
    latest.attrs = {
        "window_start": window_start,
        "window_end": window_end,
        "window_days": days,
    }
    return latest


def _product_values(
    metric_date: date,
    offer_id: str,
    aggregate: _SaleAggregate,
    snapshot: OfferSnapshot | None,
    previous: OfferSnapshot | None,
    current: OfferSnapshot | None,
    sale_sku: str | None,
) -> dict[str, Any]:
    offer_state = snapshot or current
    page_views = snapshot.page_views_30_days if snapshot is not None else None
    previous_views = previous.page_views_30_days if previous is not None else None
    conversion = snapshot.conversion_percentage_30_days if snapshot is not None else None
    previous_conversion = (
        snapshot.conversion_percentage_previous_30_days if snapshot is not None else None
    )
    return {
        "metric_date": metric_date,
        "offer_id": offer_id,
        "sku": (
            snapshot.sku
            if snapshot is not None
            else current.sku
            if current is not None
            else sale_sku
        ),
        "ordered_units": aggregate.ordered_units,
        "effective_units": aggregate.effective_units,
        "ordered_revenue": aggregate.ordered_revenue,
        "page_views_30_days": page_views,
        "page_views_30_day_average": (
            Decimal(page_views) / Decimal(30) if page_views is not None else None
        ),
        "page_views_window_net_change": (
            page_views - previous_views
            if page_views is not None and previous_views is not None
            else None
        ),
        "conversion_percentage_30_days": conversion,
        "conversion_percentage_previous_30_days": previous_conversion,
        "conversion_change_points": (
            conversion - previous_conversion
            if conversion is not None and previous_conversion is not None
            else None
        ),
        "total_stock": offer_state.total_stock if offer_state is not None else None,
        "offer_status": offer_state.status if offer_state is not None else None,
    }


def _append_traffic_anomalies(
    anomalies: list[dict[str, Any]],
    row: Mapping[str, Any],
    metric_date: date,
    offer_id: str,
    high_views: float | None,
    low_views: float | None,
    low_conversion: float | None,
    high_conversion: float | None,
    created_at: datetime,
) -> None:
    page_views = row["page_views_30_days"]
    conversion = row["conversion_percentage_30_days"]
    if page_views is None or conversion is None:
        return
    if (
        high_views is not None
        and low_conversion is not None
        and float(page_views) >= high_views
        and float(conversion) < low_conversion
    ):
        anomalies.append(
            _anomaly(
                metric_date,
                offer_id,
                "high_views_low_conversion",
                "30-day views are high while conversion is below the lower quartile.",
                {},
                created_at,
            )
        )
    if (
        low_views is not None
        and high_conversion is not None
        and float(page_views) < low_views
        and float(conversion) >= high_conversion
    ):
        anomalies.append(
            _anomaly(
                metric_date,
                offer_id,
                "low_views_high_conversion",
                "30-day views are low while conversion is in the upper quartile.",
                {},
                created_at,
            )
        )


def _anomaly(
    event_date: date,
    offer_id: str,
    anomaly_type: str,
    explanation: str,
    details: dict[str, Any],
    created_at: datetime,
) -> dict[str, Any]:
    return {
        "event_date": event_date,
        "offer_id": offer_id,
        "anomaly_type": anomaly_type,
        "severity": "warning",
        "explanation": explanation,
        "details": details,
        "created_at": created_at,
    }


def _quality_values(
    unknown_statuses: Mapping[tuple[date, str], set[str | None]],
    start: date,
    end: date,
    created_at: datetime,
) -> list[dict[str, Any]]:
    return [
        {
            "event_id": str(
                uuid5(
                    NAMESPACE_URL,
                    "takealot-ops:unknown_sale_status:"
                    f"{event_date.isoformat()}:{offer_id}:"
                    f"{','.join(_display_statuses(statuses))}",
                )
            ),
            "event_date": event_date,
            "event_type": "unknown_sale_status",
            "severity": "warning",
            "offer_id": offer_id,
            "details": {"sale_statuses": _display_statuses(statuses)},
            "created_at": created_at,
        }
        for (event_date, offer_id), statuses in sorted(unknown_statuses.items())
        if start <= event_date <= end
    ]


def _display_statuses(statuses: set[str | None]) -> list[str]:
    return sorted("<missing>" if status is None else status for status in statuses)


def _average_units(
    aggregates: Mapping[tuple[date, str], _SaleAggregate],
    offer_id: str,
    metric_date: date,
    days: int,
) -> float:
    total = sum(
        aggregates.get(
            (metric_date - timedelta(days=offset), offer_id), _SaleAggregate()
        ).ordered_units
        for offset in range(1, days + 1)
    )
    return total / days


def _quantile(values: Sequence[float], percentile: int) -> float | None:
    if not values:
        return None
    return float(pd.Series(values, dtype="float64").quantile(percentile / 100))


def _date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _load_anomaly_rules(path: Path) -> _AnomalyRules:
    config = _load_yaml_mapping(path)
    drop = _mapping_value(config, "sales_drop")
    spike = _mapping_value(config, "sales_spike")
    traffic = _mapping_value(config, "traffic_conversion")
    return _AnomalyRules(
        drop_baseline_days=_integer_value(drop, "baseline_days"),
        drop_minimum_baseline=float(_integer_value(drop, "minimum_baseline_daily_quantity")),
        drop_fraction=_integer_value(drop, "drop_percentage") / 100,
        spike_baseline_days=_integer_value(spike, "baseline_days"),
        spike_minimum_quantity=_integer_value(spike, "minimum_daily_quantity"),
        spike_fraction=_integer_value(spike, "increase_percentage") / 100,
        high_views_percentile=_integer_value(traffic, "high_page_views_percentile"),
        low_conversion_percentile=_integer_value(traffic, "low_conversion_percentile"),
        low_views_percentile=_integer_value(traffic, "low_page_views_percentile"),
        high_conversion_percentile=_integer_value(traffic, "high_conversion_percentile"),
        stale_hours=_integer_value(config, "stale_offer_snapshot_hours"),
    )


def _load_status_rules(path: Path) -> tuple[frozenset[str | None], frozenset[str | None]]:
    config = _load_yaml_mapping(path)
    included = frozenset(_string_list(config, "included"))
    excluded = frozenset(_string_list(config, "excluded"))
    if included & excluded:
        raise ValueError("sale statuses cannot be both included and excluded")
    return included, excluded


def _load_yaml_mapping(path: Path) -> Mapping[str, object]:
    loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, Mapping):
        raise ValueError(f"configuration must contain a mapping: {path}")
    return loaded


def _mapping_value(config: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = config.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"configuration field must be a mapping: {key}")
    return value


def _integer_value(config: Mapping[str, object], key: str) -> int:
    value = config.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"configuration field must be an integer: {key}")
    return value


def _string_list(config: Mapping[str, object], key: str) -> list[str]:
    value = config.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"configuration field must be a string list: {key}")
    return [item for item in value if isinstance(item, str)]


def _product_frame(rows: Sequence[DailyProductMetric]) -> pd.DataFrame:
    values = [
        {column: _metric_attribute(row, column) for column in PRODUCT_DAILY_COLUMNS} for row in rows
    ]
    return pd.DataFrame(values, columns=PRODUCT_DAILY_COLUMNS)


def _metric_attribute(row: DailyProductMetric, column: str) -> object:
    value = getattr(row, column)
    if isinstance(value, Decimal):
        return float(value)
    return value


def _offer_frame(rows: Sequence[OfferSnapshot]) -> pd.DataFrame:
    values = [
        {column: _plain_attribute(row, column) for column in _OFFER_CURRENT_COLUMNS} for row in rows
    ]
    return pd.DataFrame(values, columns=_OFFER_CURRENT_COLUMNS)


def _latest_scope(scope_dates: Sequence[date], as_of: date) -> date | None:
    eligible = [scope_date for scope_date in scope_dates if scope_date <= as_of]
    return max(eligible) if eligible else None


def _batch_snapshots(
    snapshots: Sequence[OfferSnapshot], scope_date: date | None
) -> list[OfferSnapshot]:
    if scope_date is None:
        return []
    return [snapshot for snapshot in snapshots if snapshot.snapshot_date == scope_date]


def _anomaly_frame(rows: Sequence[AnomalyEvent]) -> pd.DataFrame:
    values = [
        {column: _plain_attribute(row, column) for column in _ANOMALY_COLUMNS} for row in rows
    ]
    return pd.DataFrame(values, columns=_ANOMALY_COLUMNS)


def _quality_frame(rows: Sequence[DataQualityEvent]) -> pd.DataFrame:
    values = [
        {column: _plain_attribute(row, column) for column in _QUALITY_COLUMNS} for row in rows
    ]
    return pd.DataFrame(values, columns=_QUALITY_COLUMNS)


def _plain_attribute(row: object, column: str) -> object:
    value = getattr(row, column)
    if isinstance(value, Decimal):
        return float(value)
    return value


def _store_frame(product_daily: pd.DataFrame) -> pd.DataFrame:
    if product_daily.empty:
        return pd.DataFrame(columns=_STORE_DAILY_COLUMNS)
    grouped = product_daily.groupby("metric_date", as_index=False)[
        ["ordered_units", "effective_units", "ordered_revenue"]
    ].sum()
    return pd.DataFrame(grouped, columns=_STORE_DAILY_COLUMNS)
