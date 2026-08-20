"""Narrow, authorization-bounded read projections for the multi-store overview."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Engine

from takealot_ops.storage.models import (
    AnomalyEvent,
    DailyProductMetric,
    DailyReportObservation,
    DailyReportRun,
    DailySalesMetricState,
    LogisticsProviderSnapshot,
    OfferCurrent,
    SalesRevenueRevision,
)
from takealot_ops.storage.repository import changes_closed_sast_sales_baseline
from takealot_ops.storage.store_context import normalize_store_code


def load_store_metric_projections(
    engine: Engine,
    store_codes: Sequence[str],
    *,
    as_of: date,
    start_date: date | None = None,
) -> dict[str, dict[str, Any]]:
    """Read only latest product rows and requested daily totals for exact stores."""
    codes = _normalized_codes(store_codes)
    result = {code: _empty_metric_projection() for code in codes}
    if not codes:
        return result

    metric = DailyProductMetric.__table__
    anomaly = AnomalyEvent.__table__
    latest_dates = (
        select(
            metric.c.store_code.label("store_code"),
            func.max(metric.c.metric_date).label("latest_metric_date"),
        )
        .where(
            metric.c.store_code.in_(codes),
            metric.c.metric_date <= as_of,
        )
        .group_by(metric.c.store_code)
        .subquery("overview_latest_metric_dates")
    )
    latest_statement = (
        select(
            metric.c.store_code,
            metric.c.metric_date,
            metric.c.offer_id,
            metric.c.ordered_units,
            metric.c.ordered_revenue,
            metric.c.page_views_30_days,
            metric.c.conversion_percentage_30_days,
            metric.c.total_stock,
        )
        .select_from(
            metric.join(
                latest_dates,
                and_(
                    metric.c.store_code == latest_dates.c.store_code,
                    metric.c.metric_date == latest_dates.c.latest_metric_date,
                ),
            )
        )
        .order_by(metric.c.store_code, metric.c.offer_id)
    )
    recent_start = as_of - timedelta(days=6)
    series_start = start_date or (as_of - timedelta(days=29))
    query_start = min(recent_start, series_start)
    daily_statement = (
        select(
            metric.c.store_code,
            metric.c.metric_date,
            func.sum(metric.c.ordered_units).label("ordered_units"),
            func.sum(metric.c.effective_units).label("effective_units"),
            func.sum(metric.c.ordered_revenue).label("ordered_revenue"),
        )
        .where(
            metric.c.store_code.in_(codes),
            metric.c.metric_date >= query_start,
            metric.c.metric_date <= as_of,
        )
        .group_by(metric.c.store_code, metric.c.metric_date)
        .order_by(metric.c.store_code, metric.c.metric_date)
    )
    anomaly_statement = (
        select(
            anomaly.c.store_code,
            func.count(func.distinct(anomaly.c.offer_id)).label("product_count"),
        )
        .select_from(
            anomaly.join(
                latest_dates,
                and_(
                    anomaly.c.store_code == latest_dates.c.store_code,
                    anomaly.c.event_date == latest_dates.c.latest_metric_date,
                ),
            )
        )
        .group_by(anomaly.c.store_code)
    )

    with engine.connect() as connection:
        latest_rows = [dict(row) for row in connection.execute(latest_statement).mappings()]
        daily_rows = [dict(row) for row in connection.execute(daily_statement).mappings()]
        anomaly_rows = [dict(row) for row in connection.execute(anomaly_statement).mappings()]

    latest_by_store: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in latest_rows:
        latest_by_store[str(row["store_code"])].append(row)
    daily_by_store: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in daily_rows:
        daily_by_store[str(row["store_code"])].append(row)
    anomaly_counts = {
        str(row["store_code"]): int(row["product_count"] or 0) for row in anomaly_rows
    }

    for code in codes:
        latest = latest_by_store.get(code, [])
        daily = daily_by_store.get(code, [])
        latest_date = latest[0]["metric_date"] if latest else None
        ordered_units = [row["ordered_units"] for row in latest]
        ordered_revenue = [row["ordered_revenue"] for row in latest]
        page_views = [row["page_views_30_days"] for row in latest]
        conversion = [
            float(row["conversion_percentage_30_days"])
            for row in latest
            if row["conversion_percentage_30_days"] is not None
        ]
        recent_units = [row["ordered_units"] for row in daily if row["metric_date"] >= recent_start]
        sales_series = [
            {
                "metric_date": row["metric_date"].isoformat(),
                "ordered_units": _integer_or_zero(row["ordered_units"]),
                "effective_units": _integer_or_zero(row["effective_units"]),
                "ordered_revenue": _float_or_zero(row["ordered_revenue"]),
            }
            for row in daily
            if row["metric_date"] >= series_start
        ]
        result[code] = {
            "latest_metric_date": latest_date.isoformat() if latest_date else None,
            "kpis": {
                "latest_ordered_units": _optional_sum(ordered_units, integer=True),
                "latest_ordered_revenue": _optional_sum(ordered_revenue),
                "seven_day_ordered_units": _optional_sum(recent_units, integer=True),
                "latest_anomaly_products": anomaly_counts.get(code, 0),
                "page_views_30_days": _optional_sum(page_views, integer=True),
                "median_conversion": float(median(conversion)) if conversion else None,
                "selling_products": len(
                    {
                        str(row["offer_id"])
                        for row in latest
                        if row["ordered_units"] is not None and float(row["ordered_units"]) > 0
                    }
                ),
                "stockout_products": sum(
                    1
                    for row in latest
                    if row["total_stock"] is not None and float(row["total_stock"]) == 0
                ),
            },
            "sales_series": sales_series,
        }
    return result


def load_store_inventory_projections(
    engine: Engine,
    store_codes: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Aggregate current platform inventory for all exact stores in one query."""
    codes = _normalized_codes(store_codes)
    result = {code: _empty_inventory_projection() for code in codes}
    if not codes:
        return result
    offer = OfferCurrent.__table__
    statement = (
        select(
            offer.c.store_code,
            func.count(offer.c.offer_id).label("offer_count"),
            func.max(offer.c.captured_at).label("captured_at"),
            func.sum(offer.c.takealot_available_stock).label("platform_available_stock"),
            func.count(offer.c.takealot_available_stock).label("platform_available_coverage"),
            func.sum(offer.c.takealot_stock_on_way).label("platform_stock_on_way"),
            func.count(offer.c.takealot_stock_on_way).label("platform_stock_on_way_coverage"),
            func.sum(offer.c.takealot_stock_in_receiving).label("platform_stock_in_receiving"),
            func.count(offer.c.takealot_stock_in_receiving).label(
                "platform_stock_in_receiving_coverage"
            ),
        )
        .where(offer.c.store_code.in_(codes))
        .group_by(offer.c.store_code)
    )
    with engine.connect() as connection:
        rows = connection.execute(statement).mappings()
        for row in rows:
            code = str(row["store_code"])
            captured_at = row["captured_at"]
            result[code] = {
                "captured_at": captured_at.isoformat() if captured_at else None,
                "offer_count": int(row["offer_count"] or 0),
                "platform_available_stock": _optional_int(row["platform_available_stock"]),
                "platform_available_coverage": int(row["platform_available_coverage"] or 0),
                "platform_stock_on_way": _optional_int(row["platform_stock_on_way"]),
                "platform_stock_on_way_coverage": int(row["platform_stock_on_way_coverage"] or 0),
                "platform_stock_in_receiving": _optional_int(row["platform_stock_in_receiving"]),
                "platform_stock_in_receiving_coverage": int(
                    row["platform_stock_in_receiving_coverage"] or 0
                ),
            }
    return result


def load_store_traffic_series(
    engine: Engine,
    store_codes: Sequence[str],
    *,
    as_of: date,
    days: int = 30,
) -> dict[str, list[dict[str, object]]]:
    """Batch the period-end traffic projection without changing fallback semantics."""
    if days < 1:
        raise ValueError("days must be at least 1")
    codes = _normalized_codes(store_codes)
    result: dict[str, list[dict[str, object]]] = {code: [] for code in codes}
    if not codes:
        return result

    window_start = as_of - timedelta(days=days - 1)
    china_zone = ZoneInfo("Asia/Shanghai")
    capture_start = (
        datetime.combine(window_start + timedelta(days=1), time.min, tzinfo=china_zone)
        .astimezone(UTC)
        .replace(tzinfo=None)
    )
    capture_end = (
        datetime.combine(as_of + timedelta(days=2), time.min, tzinfo=china_zone)
        .astimezone(UTC)
        .replace(tzinfo=None)
    )
    reference_start = (
        datetime.combine(window_start, time.min, tzinfo=china_zone)
        .astimezone(UTC)
        .replace(tzinfo=None)
    )
    reference_end = (
        datetime.combine(as_of + timedelta(days=1), time.min, tzinfo=china_zone)
        .astimezone(UTC)
        .replace(tzinfo=None)
    )

    run = DailyReportRun.__table__
    observation = DailyReportObservation.__table__
    pre_close_statement = (
        select(
            run.c.store_code,
            run.c.run_id,
            run.c.slot,
            run.c.captured_at,
            run.c.created_at,
            run.c.status,
        )
        .where(
            run.c.store_code.in_(codes),
            run.c.slot == "pre_close",
            run.c.captured_at >= capture_start,
            run.c.captured_at < capture_end,
        )
        .order_by(run.c.store_code, run.c.captured_at.desc(), run.c.created_at.desc())
    )
    with engine.connect() as connection:
        pre_close_rows = [dict(row) for row in connection.execute(pre_close_statement).mappings()]

        runs_by_store_date: dict[tuple[str, date], list[Mapping[str, Any]]] = defaultdict(list)
        for row in pre_close_rows:
            period_date = _period_end_date(row["captured_at"])
            if window_start <= period_date <= as_of:
                runs_by_store_date[(str(row["store_code"]), period_date)].append(row)

        selected_by_store: dict[str, list[tuple[date, Mapping[str, Any]]]] = {
            code: [] for code in codes
        }
        failed_keys: set[tuple[str, date]] = set()
        for (code, period_date), candidates in runs_by_store_date.items():
            selected = next(
                (candidate for candidate in candidates if candidate["status"] == "success"),
                candidates[0],
            )
            selected_by_store[code].append((period_date, selected))
            if selected["status"] != "success":
                failed_keys.add((code, period_date))
        for selected_rows_for_store in selected_by_store.values():
            selected_rows_for_store.sort(key=lambda item: item[0])

        references_by_key: dict[tuple[str, date], list[Mapping[str, Any]]] = defaultdict(list)
        if failed_keys:
            reference_statement = (
                select(
                    run.c.store_code,
                    run.c.run_id,
                    run.c.slot,
                    run.c.captured_at,
                    run.c.created_at,
                    run.c.status,
                )
                .where(
                    run.c.store_code.in_(codes),
                    run.c.status == "success",
                    run.c.slot != "pre_close",
                    run.c.captured_at >= reference_start,
                    run.c.captured_at < reference_end,
                )
                .order_by(
                    run.c.store_code,
                    run.c.captured_at.desc(),
                    run.c.created_at.desc(),
                )
            )
            for reference_row in connection.execute(reference_statement).mappings():
                key = (
                    str(reference_row["store_code"]),
                    _beijing_capture_date(reference_row["captured_at"]),
                )
                if key in failed_keys:
                    references_by_key[key].append(dict(reference_row))

        successful_keys = {
            (code, str(selected["run_id"]))
            for code, selected_rows in selected_by_store.items()
            for _, selected in selected_rows
            if selected["status"] == "success"
        }
        successful_keys.update(
            (code, str(candidate["run_id"]))
            for (code, _), candidates in references_by_key.items()
            for candidate in candidates
        )
        aggregates: dict[tuple[str, str], tuple[int, int, int | None]] = {}
        if successful_keys:
            run_ids = tuple(sorted({run_id for _, run_id in successful_keys}))
            aggregate_statement = (
                select(
                    observation.c.store_code,
                    observation.c.run_id,
                    func.count(observation.c.id).label("product_count"),
                    func.count(observation.c.page_views_30_days).label("available_count"),
                    func.sum(observation.c.page_views_30_days).label("total"),
                )
                .where(
                    observation.c.store_code.in_(codes),
                    observation.c.run_id.in_(run_ids),
                )
                .group_by(observation.c.store_code, observation.c.run_id)
            )
            aggregates = {
                (str(row["store_code"]), str(row["run_id"])): (
                    int(row["product_count"] or 0),
                    int(row["available_count"] or 0),
                    int(row["total"]) if row["total"] is not None else None,
                )
                for row in connection.execute(aggregate_statement).mappings()
            }

    references: dict[tuple[str, date], tuple[Mapping[str, Any], tuple[int, int, int | None]]] = {}
    for key, candidates in references_by_key.items():
        code, _ = key
        for candidate in candidates:
            aggregate = aggregates.get((code, str(candidate["run_id"])), (0, 0, None))
            product_count, available_count, total = aggregate
            if product_count > 0 and available_count > 0 and total is not None:
                references[key] = (candidate, aggregate)
                break

    for code, selected_rows in selected_by_store.items():
        points: list[dict[str, object]] = []
        for period_date, selected in selected_rows:
            product_count, available_count, partial_total = aggregates.get(
                (code, str(selected["run_id"])), (0, 0, None)
            )
            observed_total = (
                partial_total
                if selected["status"] == "success" and product_count > 0 and available_count > 0
                else None
            )
            reference_payload: dict[str, object] | None = None
            reference = references.get((code, period_date))
            if selected["status"] != "success" and reference is not None:
                reference_run, reference_aggregate = reference
                reference_products, reference_available, reference_total = reference_aggregate
                reference_payload = {
                    "source_slot": reference_run["slot"],
                    "captured_at": reference_run["captured_at"].isoformat(),
                    "page_views_30_days_total": reference_total,
                    "product_count": reference_products,
                    "missing_product_count": reference_products - reference_available,
                }
            points.append(
                {
                    "business_date": period_date.isoformat(),
                    "captured_at": selected["captured_at"].isoformat(),
                    "status": selected["status"],
                    "page_views_30_days_total": observed_total,
                    "product_count": product_count,
                    "missing_product_count": product_count - available_count,
                    "reference": reference_payload,
                }
            )
        result[code] = points
    return result


def load_store_sales_metric_states(
    engine: Engine,
    store_codes: Sequence[str],
    *,
    as_of: date,
    start_date: date | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Batch current store-day sales provenance used by the overview graph."""
    codes = _normalized_codes(store_codes)
    result: dict[str, dict[str, dict[str, Any]]] = {code: {} for code in codes}
    if not codes:
        return result
    state_start = start_date or (as_of - timedelta(days=119))
    state = DailySalesMetricState.__table__
    revision = SalesRevenueRevision.__table__
    state_statement = (
        select(
            state.c.store_code,
            state.c.metric_date,
            state.c.source_kind,
            state.c.source_run_id,
            state.c.source_details,
            state.c.verified_at,
        )
        .where(
            state.c.store_code.in_(codes),
            state.c.metric_date >= state_start,
            state.c.metric_date <= as_of,
        )
        .order_by(state.c.store_code, state.c.metric_date.desc())
    )
    revision_statement = (
        select(
            revision.c.store_code,
            revision.c.metric_date,
            revision.c.before_source,
            revision.c.after_source,
            revision.c.detected_at,
            revision.c.id,
        )
        .where(
            revision.c.store_code.in_(codes),
            revision.c.metric_date >= state_start,
            revision.c.metric_date <= as_of,
        )
        .order_by(
            revision.c.store_code,
            revision.c.metric_date,
            revision.c.detected_at,
            revision.c.id,
        )
    )
    with engine.connect() as connection:
        state_rows = [dict(row) for row in connection.execute(state_statement).mappings()]
        revision_rows = [dict(row) for row in connection.execute(revision_statement).mappings()]

    revision_summaries: dict[tuple[str, date], dict[str, Any]] = {}
    for row in revision_rows:
        if not changes_closed_sast_sales_baseline(
            row["metric_date"], row["before_source"], row["after_source"]
        ):
            continue
        key = (str(row["store_code"]), row["metric_date"])
        summary = revision_summaries.setdefault(key, {"count": 0, "latest_at": None})
        summary["count"] = int(summary["count"]) + 1
        summary["latest_at"] = row["detected_at"].isoformat()
    for row in state_rows:
        code = str(row["store_code"])
        metric_date = row["metric_date"]
        revision_summary = revision_summaries.get((code, metric_date), {})
        verified_at = row["verified_at"]
        result[code][metric_date.isoformat()] = {
            "source_kind": row["source_kind"],
            "source_run_id": row["source_run_id"],
            "source": dict(row["source_details"] or {}),
            "verified_at": verified_at.isoformat() if verified_at else None,
            "revision_count": int(revision_summary.get("count") or 0),
            "latest_revision_at": revision_summary.get("latest_at"),
        }
    return result


def load_store_sales_reconciliations(
    engine: Engine,
    stores: Mapping[str, str],
    *,
    as_of: date,
) -> dict[str, dict[str, Any]]:
    """Batch the existing period-end recovery classification for exact stores."""
    codes = _normalized_codes(tuple(stores))
    result = {code: _empty_sales_reconciliation(code, stores.get(code, code)) for code in codes}
    if not codes:
        return result
    range_start = as_of - timedelta(days=29)
    report = DailyReportRun.__table__
    state = DailySalesMetricState.__table__
    metric = DailyProductMetric.__table__
    revision = SalesRevenueRevision.__table__

    ranked_reports = (
        select(
            report.c.store_code,
            report.c.business_date,
            report.c.captured_at,
            report.c.created_at,
            report.c.status,
            report.c.counts,
            func.row_number()
            .over(
                partition_by=report.c.store_code,
                order_by=(report.c.captured_at.desc(), report.c.created_at.desc()),
            )
            .label("row_number"),
        )
        .where(
            report.c.store_code.in_(codes),
            report.c.slot == "pre_close",
            report.c.business_date <= as_of,
        )
        .subquery("overview_latest_pre_close")
    )
    report_statement = select(ranked_reports).where(ranked_reports.c.row_number == 1)
    state_statement = select(
        state.c.store_code,
        state.c.metric_date,
        state.c.source_kind,
        state.c.verified_at,
    ).where(
        state.c.store_code.in_(codes),
        state.c.metric_date >= range_start,
        state.c.metric_date <= as_of,
    )
    metric_dates_statement = (
        select(metric.c.store_code, metric.c.metric_date)
        .where(
            metric.c.store_code.in_(codes),
            metric.c.metric_date >= range_start,
            metric.c.metric_date <= as_of,
        )
        .distinct()
    )
    revision_statement = (
        select(
            revision.c.store_code,
            revision.c.metric_date,
            revision.c.before_source,
            revision.c.after_source,
            revision.c.detected_at,
            revision.c.id,
        )
        .where(
            revision.c.store_code.in_(codes),
            revision.c.metric_date <= as_of,
        )
        .order_by(revision.c.store_code, revision.c.detected_at, revision.c.id)
    )
    with engine.connect() as connection:
        report_rows = [dict(row) for row in connection.execute(report_statement).mappings()]
        state_rows = [dict(row) for row in connection.execute(state_statement).mappings()]
        metric_date_rows = [
            dict(row) for row in connection.execute(metric_dates_statement).mappings()
        ]
        revision_rows = [dict(row) for row in connection.execute(revision_statement).mappings()]

    reports_by_store = {str(row["store_code"]): row for row in report_rows}
    states_by_store: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in state_rows:
        states_by_store[str(row["store_code"])].append(row)
    metric_dates_by_store: dict[str, set[date]] = defaultdict(set)
    for row in metric_date_rows:
        metric_dates_by_store[str(row["store_code"])].add(row["metric_date"])
    revisions_by_store: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in revision_rows:
        if changes_closed_sast_sales_baseline(
            row["metric_date"], row["before_source"], row["after_source"]
        ):
            revisions_by_store[str(row["store_code"])].append(row)

    for code in codes:
        period_end = reports_by_store.get(code)
        states = states_by_store.get(code, [])
        takealot_states = [
            row
            for row in states
            if row["source_kind"] == "takealot_sales_api" and row["verified_at"] is not None
        ]
        latest_verified_at = _latest_datetime([row["verified_at"] for row in takealot_states])
        tracked_dates = {row["metric_date"] for row in states}
        metric_date_count = max(len(metric_dates_by_store.get(code, set())), len(tracked_dates))
        verified_after_failure_dates: set[date] = set()
        if period_end is not None and period_end["status"] == "failed":
            captured_at = period_end["captured_at"]
            verified_after_failure_dates = {
                row["metric_date"]
                for row in takealot_states
                if _datetime_after(row["verified_at"], captured_at)
            }
        meaningful_revisions = revisions_by_store.get(code, [])
        latest_revision_at = _latest_datetime([row["detected_at"] for row in meaningful_revisions])
        if period_end is None:
            status = "unverified"
        elif period_end["status"] != "failed":
            status = "verified"
        elif metric_date_count > 0 and len(verified_after_failure_dates) == metric_date_count:
            status = "recovered"
        else:
            status = "pending"
        counts = period_end["counts"] if period_end is not None else None
        reason = None
        if isinstance(counts, Mapping):
            reason = (
                str(counts.get("final_reason") or counts.get("missing_reason") or "").strip()
                or None
            )
        result[code] = {
            "store_code": code,
            "store_name": stores.get(code, code),
            "status": status,
            "period_end_business_date": (
                period_end["business_date"].isoformat() if period_end is not None else None
            ),
            "period_end_status": period_end["status"] if period_end is not None else None,
            "period_end_captured_at": (
                period_end["captured_at"].isoformat() if period_end is not None else None
            ),
            "period_end_failure_reason": (
                reason if period_end is not None and period_end["status"] == "failed" else None
            ),
            "latest_sales_verified_at": (
                latest_verified_at.isoformat() if latest_verified_at else None
            ),
            "metric_date_count": metric_date_count,
            "verified_after_failure_count": len(verified_after_failure_dates),
            "revision_count": len(meaningful_revisions),
            "latest_revision_at": (latest_revision_at.isoformat() if latest_revision_at else None),
        }
    return result


def load_shared_overseas_inventory(
    engine: Engine,
    store_codes: Sequence[str],
) -> dict[str, Any]:
    """Read the newest accessible W8 snapshot once across the authorized stores."""
    codes = _normalized_codes(store_codes)
    if not codes:
        return _empty_overseas_inventory()
    snapshot = LogisticsProviderSnapshot.__table__
    statement = select(
        snapshot.c.store_code,
        snapshot.c.fetched_at,
        snapshot.c.payload,
    ).where(
        snapshot.c.store_code.in_(codes),
        snapshot.c.provider == "w8",
    )
    with engine.connect() as connection:
        rows = list(connection.execute(statement).mappings())
    if not rows:
        return _empty_overseas_inventory()
    latest = max(rows, key=lambda row: _datetime_sort_key(row["fetched_at"]))
    payload = latest["payload"]
    if not isinstance(payload, Mapping) or not payload.get("connected"):
        return _empty_overseas_inventory()
    summary = payload.get("summary")
    if not isinstance(summary, Mapping):
        return _empty_overseas_inventory()
    warehouse = payload.get("warehouse")
    warehouse_name = None
    if isinstance(warehouse, Mapping):
        warehouse_name = str(warehouse.get("name") or warehouse.get("code") or "").strip() or None
    fetched_at = latest["fetched_at"]
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    return {
        "snapshot_at": fetched_at.isoformat(),
        "warehouse_name": warehouse_name,
        "stock_total": _optional_int(summary.get("stock_total")),
        "usable_stock": _optional_int(summary.get("usable_stock")),
        "locked_stock": _optional_int(summary.get("locked_stock")),
        "outbound_allocated": _optional_int(summary.get("outbound_allocated")),
        "transit_stock": _optional_int(summary.get("transit_stock")),
        "defective_stock": _optional_int(summary.get("defective_stock")),
        "shared_across_stores": True,
    }


def _normalized_codes(store_codes: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({normalize_store_code(code) for code in store_codes}))


def _empty_metric_projection() -> dict[str, Any]:
    return {
        "latest_metric_date": None,
        "kpis": {
            "latest_ordered_units": None,
            "latest_ordered_revenue": None,
            "seven_day_ordered_units": None,
            "latest_anomaly_products": 0,
            "page_views_30_days": None,
            "median_conversion": None,
            "selling_products": 0,
            "stockout_products": 0,
        },
        "sales_series": [],
    }


def _empty_inventory_projection() -> dict[str, Any]:
    return {
        "captured_at": None,
        "offer_count": 0,
        "platform_available_stock": None,
        "platform_available_coverage": 0,
        "platform_stock_on_way": None,
        "platform_stock_on_way_coverage": 0,
        "platform_stock_in_receiving": None,
        "platform_stock_in_receiving_coverage": 0,
    }


def _empty_sales_reconciliation(store_code: str, store_name: str) -> dict[str, Any]:
    return {
        "store_code": store_code,
        "store_name": store_name,
        "status": "unverified",
        "period_end_business_date": None,
        "period_end_status": None,
        "period_end_captured_at": None,
        "period_end_failure_reason": None,
        "latest_sales_verified_at": None,
        "metric_date_count": 0,
        "verified_after_failure_count": 0,
        "revision_count": 0,
        "latest_revision_at": None,
    }


def _empty_overseas_inventory() -> dict[str, Any]:
    return {
        "snapshot_at": None,
        "warehouse_name": None,
        "stock_total": None,
        "usable_stock": None,
        "locked_stock": None,
        "outbound_allocated": None,
        "transit_stock": None,
        "defective_stock": None,
        "shared_across_stores": True,
    }


def _optional_sum(values: Sequence[Any], *, integer: bool = False) -> int | float | None:
    known = [float(value) for value in values if value is not None]
    if not known:
        return None
    total = sum(known)
    return int(total) if integer else float(total)


def _integer_or_zero(value: Any) -> int:
    return int(value or 0)


def _float_or_zero(value: Any) -> float:
    return float(value or 0)


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _beijing_capture_date(captured_at: datetime) -> date:
    utc_value = (
        captured_at.replace(tzinfo=UTC)
        if captured_at.tzinfo is None
        else captured_at.astimezone(UTC)
    )
    return utc_value.astimezone(ZoneInfo("Asia/Shanghai")).date()


def _period_end_date(captured_at: datetime) -> date:
    return _beijing_capture_date(captured_at) - timedelta(days=1)


def _datetime_sort_key(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _latest_datetime(values: Sequence[datetime]) -> datetime | None:
    return max(values, key=_datetime_sort_key) if values else None


def _datetime_after(value: datetime, boundary: datetime) -> bool:
    return _datetime_sort_key(value) > _datetime_sort_key(boundary)
