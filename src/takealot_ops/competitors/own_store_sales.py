"""Official own-store daily sales history for competitor detail views."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from collections import defaultdict
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from takealot_ops.domain import SAST
from takealot_ops.storage.models import (
    DailySalesMetricState,
    ErpStore,
    OfferCurrent,
    OfferSnapshot,
    SaleItem,
    SalesRevenueRevision,
    StoreOfferBaseline,
    StoreOfferObservation,
)
from takealot_ops.storage.repository import is_closed_day_sales_revision
from takealot_ops.storage.store_context import normalize_store_code, store_scope


CHINA = ZoneInfo("Asia/Shanghai")
OWN_STORE_SALES_WINDOW_DAYS = (7, 15, 30, 60, 90)


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
    link_series, _ = _build_own_store_sales_detail(
        session,
        plid=plid,
        store_codes=store_codes,
        through=through,
        include_variant_series=False,
    )
    return link_series


def build_own_store_sales_detail(
    session: Session,
    *,
    plid: str,
    store_codes: set[str],
    through: date,
) -> dict[str, list[dict[str, Any]]]:
    """Return whole-link and exact-current-Offer sales series together."""
    link_series, variant_series = _build_own_store_sales_detail(
        session,
        plid=plid,
        store_codes=store_codes,
        through=through,
        include_variant_series=True,
    )
    return {
        "link_series": link_series,
        "variant_series": variant_series,
    }


def _build_own_store_sales_detail(
    session: Session,
    *,
    plid: str,
    store_codes: set[str],
    through: date,
    include_variant_series: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    normalized_plid = str(plid or "").strip()
    normalized_codes = sorted(
        {
            normalize_store_code(store_code)
            for store_code in store_codes
            if str(store_code or "").strip()
        }
    )
    if not normalized_plid or not normalized_codes:
        return [], []

    store_names = {
        str(store.code): str(store.display_name)
        for store in session.scalars(
            select(ErpStore).where(ErpStore.code.in_(normalized_codes))
        )
    }
    link_series: list[dict[str, Any]] = []
    variant_series: list[dict[str, Any]] = []
    for store_code in normalized_codes:
        store_variant_series: list[dict[str, Any]] = []
        with store_scope(store_code):
            series = _store_sales_series(
                session,
                plid=normalized_plid,
                store_code=store_code,
                store_name=store_names.get(store_code, store_code),
                through=through,
                variant_series=(
                    store_variant_series if include_variant_series else None
                ),
            )
        if series is not None:
            link_series.append(series)
            variant_series.extend(store_variant_series)
    return link_series, variant_series


def build_own_store_sales_series_bulk(
    session: Session,
    *,
    plids: Iterable[str],
    store_codes: set[str],
    through: date,
    start: date | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return the same daily series as the single-PLID loader in bulk.

    Container-selection evaluates dozens of PLIDs at once. Loading each PLID
    separately repeats the same per-store state and revision queries hundreds
    of times, so this projection groups the identical evidence in memory after
    one bounded query set per store.
    """
    normalized_plids = sorted(
        {
            str(plid or "").strip()
            for plid in plids
            if str(plid or "").strip()
        }
    )
    normalized_codes = sorted(
        {
            normalize_store_code(store_code)
            for store_code in store_codes
            if str(store_code or "").strip()
        }
    )
    result: dict[str, list[dict[str, Any]]] = {
        plid: [] for plid in normalized_plids
    }
    if not normalized_plids or not normalized_codes:
        return result

    store_names = {
        str(store.code): str(store.display_name)
        for store in session.scalars(
            select(ErpStore).where(ErpStore.code.in_(normalized_codes))
        )
    }
    for store_code in normalized_codes:
        with store_scope(store_code):
            store_series = _store_sales_series_bulk(
                session,
                plids=normalized_plids,
                store_code=store_code,
                store_name=store_names.get(store_code, store_code),
                through=through,
                start=start,
            )
        for plid, series in store_series.items():
            result[plid].append(series)
    return result


def aggregate_own_store_sales_series(
    series: Iterable[Mapping[str, Any]],
    *,
    store_name: str | None = None,
    start: date | None = None,
) -> dict[str, Any] | None:
    """Combine every visible store's own-link series into one scope total.

    Stores that had not listed the PLID yet are excluded from earlier days. If
    at least one active store has a known value while another store is missing
    or partial, the visible subtotal is retained but the aggregate day remains
    partial. This avoids both dropping known orders and presenting incomplete
    cross-store coverage as verified.
    """

    rows: list[tuple[Mapping[str, Any], date, date, dict[date, Mapping[str, Any]]]] = []
    for item in series:
        listing_date = _iso_date(item.get("listing_date"))
        through_date = _iso_date(item.get("through_date"))
        if listing_date is None or through_date is None or listing_date > through_date:
            continue
        points_by_date: dict[date, Mapping[str, Any]] = {}
        raw_points = item.get("points")
        if isinstance(raw_points, list):
            for raw_point in raw_points:
                if not isinstance(raw_point, Mapping):
                    continue
                point_date = _iso_date(raw_point.get("date"))
                if point_date is not None:
                    points_by_date[point_date] = raw_point
        rows.append((item, listing_date, through_date, points_by_date))
    if not rows:
        return None

    listing_date = min(item[1] for item in rows)
    through_date = max(item[2] for item in rows)
    series_start_date = max(listing_date, start) if start is not None else listing_date
    if series_start_date > through_date:
        return None
    store_codes = sorted(
        {
            str(item[0].get("store_code") or "").strip()
            for item in rows
            if str(item[0].get("store_code") or "").strip()
        }
    )
    offer_ids = sorted(
        {
            str(offer_id).strip()
            for item, *_ in rows
            for offer_id in _list_items(item.get("offer_ids"))
            if str(offer_id).strip()
        }
    )
    skus = sorted(
        {
            str(sku).strip()
            for item, *_ in rows
            for sku in _list_items(item.get("skus"))
            if str(sku).strip()
        }
    )
    aggregate_points: list[dict[str, Any]] = []
    for metric_date in _date_range(series_start_date, through_date):
        active_rows = [row for row in rows if metric_date >= row[1]]
        active_points = [row[3].get(metric_date) for row in active_rows]
        known_points = [
            point
            for point in active_points
            if point is not None and isinstance(point.get("ordered_units"), int)
        ]
        ordered_units = (
            sum(int(point["ordered_units"]) for point in known_points)
            if known_points
            else None
        )
        fully_verified = len(known_points) == len(active_rows) and all(
            point is not None and point.get("data_status") == "verified"
            for point in active_points
        )
        data_status = (
            "verified"
            if fully_verified
            else "partial"
            if known_points
            else "missing"
        )
        aggregate_points.append(
            {
                "date": metric_date.isoformat(),
                "ordered_units": ordered_units,
                "data_status": data_status,
                "revision_count": sum(
                    int(point.get("revision_count") or 0)
                    for point in active_points
                    if point is not None
                ),
            }
        )

    covered_dates = [
        point["date"]
        for point in aggregate_points
        if point["data_status"] == "verified"
    ]
    partial_dates = [
        point["date"]
        for point in aggregate_points
        if point["data_status"] == "partial"
    ]
    missing_dates = [
        point["date"]
        for point in aggregate_points
        if point["data_status"] == "missing"
    ]
    known_totals = [
        int(point["ordered_units"])
        for point in aggregate_points
        if isinstance(point.get("ordered_units"), int)
    ]
    earliest_rows = [item for item, start, *_ in rows if start == listing_date]
    listing_date_source = (
        "platform"
        if any(item.get("listing_date_source") == "platform" for item in earliest_rows)
        else "first_observed"
    )
    listing_times = sorted(
        str(item.get("listing_at") or "").strip()
        for item, *_ in rows
        if str(item.get("listing_at") or "").strip()
    )
    image_url = next(
        (
            str(item.get("image_url"))
            for item, *_ in rows
            if item.get("image_url")
        ),
        None,
    )
    resolved_store_name = store_name or (
        str(rows[0][0].get("store_name") or rows[0][0].get("store_code") or "当前店铺")
        if len(store_codes) <= 1
        else f"当前范围全部自有店铺（{len(store_codes)}店合计）"
    )
    return {
        "store_code": "__scope__",
        "store_name": resolved_store_name,
        "store_count": len(store_codes),
        "plid": str(rows[0][0].get("plid") or "").strip(),
        "offer_ids": offer_ids,
        "image_url": image_url,
        "skus": skus,
        "listing_date": listing_date.isoformat(),
        **(
            {"series_start_date": series_start_date.isoformat()}
            if start is not None
            else {}
        ),
        "listing_date_source": listing_date_source,
        "listing_at": listing_times[0] if listing_times else None,
        "through_date": through_date.isoformat(),
        "date_basis": "Asia/Shanghai",
        "source_date_basis": "Africa/Johannesburg",
        "total_ordered_units": sum(known_totals) if known_totals else None,
        "covered_days": len(covered_dates),
        "partial_days": len(partial_dates),
        "missing_days": len(missing_dates),
        "coverage_start": covered_dates[0] if covered_dates else None,
        "coverage_end": covered_dates[-1] if covered_dates else None,
        "points": aggregate_points,
    }


def summarize_own_store_sales_windows(
    series: Mapping[str, Any],
) -> dict[str, int | None]:
    """Return the fixed list-card windows from a scope aggregate series."""

    listing_date = _iso_date(series.get("listing_date"))
    through_date = _iso_date(series.get("through_date"))
    raw_points = series.get("points")
    if (
        listing_date is None
        or through_date is None
        or listing_date > through_date
        or not isinstance(raw_points, list)
    ):
        return {str(days): None for days in OWN_STORE_SALES_WINDOW_DAYS}
    points: list[tuple[date, int]] = []
    for raw_point in raw_points:
        if not isinstance(raw_point, Mapping):
            continue
        point_date = _iso_date(raw_point.get("date"))
        ordered_units = raw_point.get("ordered_units")
        if point_date is None or not isinstance(ordered_units, int):
            continue
        points.append((point_date, ordered_units))
    result: dict[str, int | None] = {}
    for days in OWN_STORE_SALES_WINDOW_DAYS:
        start_date = max(listing_date, through_date - timedelta(days=days - 1))
        values = [
            units
            for point_date, units in points
            if start_date <= point_date <= through_date
        ]
        result[str(days)] = sum(values) if values else None
    return result


def _iso_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError:
        return None


def _list_items(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _store_sales_series_bulk(
    session: Session,
    *,
    plids: list[str],
    store_code: str,
    store_name: str,
    through: date,
    start: date | None,
) -> dict[str, dict[str, Any]]:
    current_offers = list(
        session.execute(
            select(
                OfferCurrent.productline_id,
                OfferCurrent.offer_id,
                OfferCurrent.sku,
                OfferCurrent.created_at,
                OfferCurrent.captured_at,
            )
            .where(OfferCurrent.productline_id.in_(plids))
            .order_by(OfferCurrent.productline_id, OfferCurrent.offer_id)
        ).all()
    )
    current_by_plid: dict[str, list[Any]] = defaultdict(list)
    for row in current_offers:
        plid = str(row.productline_id or "").strip()
        if plid:
            current_by_plid[plid].append(row)
    active_plids = sorted(current_by_plid)
    if not active_plids:
        return {}

    snapshot_statement = select(
        OfferSnapshot.productline_id,
        OfferSnapshot.offer_id,
        OfferSnapshot.sku,
        func.min(OfferSnapshot.created_at).label("created_at"),
        func.min(OfferSnapshot.captured_at).label("captured_at"),
    ).where(
        OfferSnapshot.productline_id.in_(active_plids),
        OfferSnapshot.snapshot_date <= through,
    ).group_by(
        OfferSnapshot.productline_id,
        OfferSnapshot.offer_id,
        OfferSnapshot.sku,
    )
    baseline_statement = select(
        StoreOfferBaseline.productline_id,
        StoreOfferBaseline.offer_id,
        StoreOfferBaseline.sku,
        func.min(StoreOfferBaseline.display_date).label("display_date"),
        func.min(StoreOfferBaseline.captured_at).label("captured_at"),
    ).where(
        StoreOfferBaseline.productline_id.in_(active_plids),
        StoreOfferBaseline.display_date <= through,
    ).group_by(
        StoreOfferBaseline.productline_id,
        StoreOfferBaseline.offer_id,
        StoreOfferBaseline.sku,
    )
    observation_statement = select(
        StoreOfferObservation.productline_id,
        StoreOfferObservation.offer_id,
        StoreOfferObservation.sku,
        func.min(StoreOfferObservation.display_date).label("display_date"),
        func.min(StoreOfferObservation.captured_at).label("captured_at"),
    ).where(
        StoreOfferObservation.productline_id.in_(active_plids),
        StoreOfferObservation.display_date <= through,
    ).group_by(
        StoreOfferObservation.productline_id,
        StoreOfferObservation.offer_id,
        StoreOfferObservation.sku,
    )
    if start is not None:
        snapshot_statement = snapshot_statement.where(OfferSnapshot.snapshot_date >= start)
        baseline_statement = baseline_statement.where(
            StoreOfferBaseline.display_date >= start
        )
        observation_statement = observation_statement.where(
            StoreOfferObservation.display_date >= start
        )
    snapshots = list(
        session.execute(
            snapshot_statement.order_by(
                OfferSnapshot.productline_id,
                OfferSnapshot.offer_id,
            )
        ).all()
    )
    baselines = list(
        session.execute(
            baseline_statement.order_by(
                StoreOfferBaseline.productline_id,
                StoreOfferBaseline.offer_id,
            )
        ).all()
    )
    observations = list(
        session.execute(
            observation_statement.order_by(
                StoreOfferObservation.productline_id,
                StoreOfferObservation.offer_id,
            )
        ).all()
    )

    snapshots_by_plid: dict[str, list[Any]] = defaultdict(list)
    baselines_by_plid: dict[str, list[Any]] = defaultdict(list)
    observations_by_plid: dict[str, list[Any]] = defaultdict(list)
    for snapshot in snapshots:
        snapshots_by_plid[str(snapshot.productline_id or "").strip()].append(snapshot)
    for baseline in baselines:
        baselines_by_plid[str(baseline.productline_id or "").strip()].append(baseline)
    for observation in observations:
        observations_by_plid[str(observation.productline_id or "").strip()].append(
            observation
        )

    prepared: dict[str, dict[str, Any]] = {}
    plids_by_offer_id: dict[str, set[str]] = defaultdict(set)
    all_offer_ids: set[str] = set()
    required_source_dates: set[date] = set()
    for plid in active_plids:
        plid_current = current_by_plid[plid]
        plid_snapshots = snapshots_by_plid.get(plid, [])
        plid_baselines = baselines_by_plid.get(plid, [])
        plid_observations = observations_by_plid.get(plid, [])
        offer_ids = sorted(
            {str(row.offer_id).strip() for row in plid_current if row.offer_id}
            | {str(row.offer_id).strip() for row in plid_snapshots if row.offer_id}
            | {str(row.offer_id).strip() for row in plid_baselines if row.offer_id}
            | {str(row.offer_id).strip() for row in plid_observations if row.offer_id}
        )
        skus = sorted(
            {str(row.sku).strip() for row in plid_current if row.sku}
            | {str(row.sku).strip() for row in plid_snapshots if row.sku}
            | {str(row.sku).strip() for row in plid_baselines if row.sku}
            | {str(row.sku).strip() for row in plid_observations if row.sku}
        )
        platform_datetimes = [
            listed_at
            for row in plid_current
            if (listed_at := _china_datetime(row.created_at)) is not None
        ] + [
            listed_at
            for row in plid_snapshots
            if (listed_at := _china_datetime(row.created_at)) is not None
        ]
        platform_dates = [listed_at.date() for listed_at in platform_datetimes]
        observed_datetimes = [
            observed_at
            for row in plid_current
            if (observed_at := _china_datetime(row.captured_at)) is not None
        ] + [
            observed_at
            for row in plid_snapshots
            if (observed_at := _china_datetime(row.captured_at)) is not None
        ]
        observed_dates = [
            *(observed_at.date() for observed_at in observed_datetimes),
            *(row.display_date for row in plid_baselines),
            *(row.display_date for row in plid_observations),
        ]
        listing_date = (
            min(platform_dates)
            if platform_dates
            else min(observed_dates)
            if observed_dates
            else None
        )
        if listing_date is None:
            continue
        series_start_date = max(listing_date, start) if start is not None else listing_date
        if series_start_date > through:
            continue
        listing_date_source = "platform" if platform_dates else "first_observed"
        listing_at = (
            min(platform_datetimes).isoformat()
            if platform_datetimes
            else min(
                observed_at
                for observed_at in observed_datetimes
                if observed_at.date() == listing_date
            ).isoformat()
            if any(
                observed_at.date() == listing_date
                for observed_at in observed_datetimes
            )
            else None
        )
        for offer_id in offer_ids:
            plids_by_offer_id[offer_id].add(plid)
        all_offer_ids.update(offer_ids)
        for display_date in _date_range(series_start_date, through):
            required_source_dates.update(_sast_dates_for_china_day(display_date))
        prepared[plid] = {
            "offer_ids": offer_ids,
            "skus": skus,
            "listing_date": listing_date,
            "listing_date_source": listing_date_source,
            "listing_at": listing_at,
            "series_start_date": series_start_date,
        }

    units_by_plid_and_date: dict[str, dict[date, int]] = defaultdict(dict)
    if all_offer_ids:
        sale_statement = select(
            SaleItem.offer_id,
            SaleItem.order_date,
            SaleItem.quantity,
        ).where(
            SaleItem.offer_id.in_(sorted(all_offer_ids)),
            SaleItem.order_date
            < datetime.combine(
                through + timedelta(days=1),
                time.min,
                tzinfo=CHINA,
            ).astimezone(UTC),
        )
        if start is not None:
            sale_statement = sale_statement.where(
                SaleItem.order_date
                >= datetime.combine(start, time.min, tzinfo=CHINA).astimezone(UTC)
            )
        sale_rows = session.execute(
            sale_statement.order_by(SaleItem.order_date, SaleItem.order_item_id)
        ).all()
        for offer_id, order_date, quantity in sale_rows:
            sales_date = _china_day(order_date)
            if (
                sales_date is None
                or sales_date > through
                or (start is not None and sales_date < start)
            ):
                continue
            for plid in plids_by_offer_id.get(str(offer_id or "").strip(), set()):
                units = units_by_plid_and_date[plid]
                units[sales_date] = units.get(sales_date, 0) + int(quantity or 0)

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
    revision_counts: dict[date, int] = {}
    if required_source_dates:
        for revision in session.scalars(
            select(SalesRevenueRevision).where(
                SalesRevenueRevision.metric_date.in_(required_source_dates)
            )
        ):
            if not is_closed_day_sales_revision(revision):
                continue
            revision_counts[revision.metric_date] = (
                revision_counts.get(revision.metric_date, 0) + 1
            )

    result: dict[str, dict[str, Any]] = {}
    for plid, evidence in prepared.items():
        listing_date = evidence["listing_date"]
        series_start_date = evidence["series_start_date"]
        units_by_date = units_by_plid_and_date.get(plid, {})
        points: list[dict[str, Any]] = []
        covered_dates: list[date] = []
        partial_dates: list[date] = []
        total_ordered_units = 0
        for metric_date in _date_range(series_start_date, through):
            source_dates = _sast_dates_for_china_day(metric_date)
            source_states = [states.get(source_date) for source_date in source_dates]
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
                units_by_date.get(metric_date, 0)
                if data_status != "missing"
                else None
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
                        revision_counts.get(source_date, 0)
                        for source_date in source_dates
                    ),
                }
            )
        result[plid] = {
            "store_code": store_code,
            "store_name": store_name,
            "plid": plid,
            "offer_ids": evidence["offer_ids"],
            "skus": evidence["skus"],
            "listing_date": listing_date.isoformat(),
            "listing_date_source": evidence["listing_date_source"],
            "listing_at": evidence["listing_at"],
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
                (through - series_start_date).days
                + 1
                - len(covered_dates)
                - len(partial_dates),
            ),
            "coverage_start": covered_dates[0].isoformat() if covered_dates else None,
            "coverage_end": covered_dates[-1].isoformat() if covered_dates else None,
            "points": points,
        }
    return result


def _store_sales_series(
    session: Session,
    *,
    plid: str,
    store_code: str,
    store_name: str,
    through: date,
    variant_series: list[dict[str, Any]] | None = None,
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
                SaleItem.offer_id,
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
    units_by_offer_and_date: dict[str, dict[date, int]] = defaultdict(dict)
    for sale_offer_id, order_date, quantity in sale_rows:
        sales_date = _china_day(order_date)
        if sales_date is None or sales_date > through:
            continue
        units = int(quantity or 0)
        units_by_date[sales_date] = units_by_date.get(sales_date, 0) + units
        normalized_offer_id = str(sale_offer_id or "").strip()
        if normalized_offer_id:
            offer_units = units_by_offer_and_date[normalized_offer_id]
            offer_units[sales_date] = offer_units.get(sales_date, 0) + units

    listing_evidence = _listing_evidence(
        current_offers=current_offers,
        snapshots=snapshots,
        baselines=baselines,
        observations=observations,
        sales_dates=units_by_date,
    )
    if listing_evidence is None:
        return None
    listing_date, listing_date_source, listing_at = listing_evidence

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
    revision_counts: dict[date, int] = {}
    if required_source_dates:
        for revision in session.scalars(
            select(SalesRevenueRevision).where(
                SalesRevenueRevision.metric_date.in_(required_source_dates)
            )
        ):
            if not is_closed_day_sales_revision(revision):
                continue
            revision_counts[revision.metric_date] = (
                revision_counts.get(revision.metric_date, 0) + 1
            )
    link_series = _build_sales_series_payload(
        store_code=store_code,
        store_name=store_name,
        plid=plid,
        offer_ids=offer_ids,
        skus=skus,
        listing_date=listing_date,
        listing_date_source=listing_date_source,
        listing_at=listing_at,
        through=through,
        units_by_date=units_by_date,
        states=states,
        revision_counts=revision_counts,
    )

    if variant_series is not None:
        for current_offer in current_offers:
            offer_id = str(current_offer.offer_id or "").strip()
            if not offer_id:
                continue
            exact_snapshots = [row for row in snapshots if row.offer_id == offer_id]
            exact_baselines = [row for row in baselines if row.offer_id == offer_id]
            exact_observations = [row for row in observations if row.offer_id == offer_id]
            exact_units = units_by_offer_and_date.get(offer_id, {})
            exact_listing_evidence = _listing_evidence(
                current_offers=[current_offer],
                snapshots=exact_snapshots,
                baselines=exact_baselines,
                observations=exact_observations,
                sales_dates=exact_units,
            )
            if exact_listing_evidence is None:
                continue
            exact_listing_date, exact_listing_source, exact_listing_at = (
                exact_listing_evidence
            )
            exact_skus = sorted(
                (
                    {str(current_offer.sku).strip() if current_offer.sku else ""}
                    | {str(row.sku).strip() for row in exact_snapshots if row.sku}
                    | {str(row.sku).strip() for row in exact_baselines if row.sku}
                    | {str(row.sku).strip() for row in exact_observations if row.sku}
                )
                - {""}
            )
            current_sku = str(current_offer.sku or "").strip() or None
            exact_series = _build_sales_series_payload(
                store_code=store_code,
                store_name=store_name,
                plid=plid,
                offer_ids=[offer_id],
                skus=exact_skus,
                listing_date=exact_listing_date,
                listing_date_source=exact_listing_source,
                listing_at=exact_listing_at,
                through=through,
                units_by_date=exact_units,
                states=states,
                revision_counts=revision_counts,
            )
            exact_series.update(
                {
                    "offer_id": offer_id,
                    "sku": current_sku,
                }
            )
            variant_series.append(exact_series)

    return link_series


def _listing_evidence(
    *,
    current_offers: Iterable[Any],
    snapshots: Iterable[Any],
    baselines: Iterable[Any],
    observations: Iterable[Any],
    sales_dates: Iterable[date],
) -> tuple[date, str, str | None] | None:
    current_rows = list(current_offers)
    snapshot_rows = list(snapshots)
    baseline_rows = list(baselines)
    observation_rows = list(observations)
    platform_datetimes = [
        listed_at
        for row in current_rows
        if (listed_at := _china_datetime(row.created_at)) is not None
    ] + [
        listed_at
        for row in snapshot_rows
        if (listed_at := _china_datetime(row.created_at)) is not None
    ]
    platform_dates = [listed_at.date() for listed_at in platform_datetimes]
    observed_datetimes = [
        observed_at
        for row in current_rows
        if (observed_at := _china_datetime(row.captured_at)) is not None
    ] + [
        observed_at
        for row in snapshot_rows
        if (observed_at := _china_datetime(row.captured_at)) is not None
    ]
    observed_dates = [
        *(observed_at.date() for observed_at in observed_datetimes),
        *(row.display_date for row in baseline_rows),
        *(row.display_date for row in observation_rows),
        *sales_dates,
    ]
    if platform_dates:
        listing_date = min(platform_dates)
        listing_date_source = "platform"
    elif observed_dates:
        listing_date = min(observed_dates)
        listing_date_source = "first_observed"
    else:
        return None
    listing_at = (
        min(platform_datetimes).isoformat()
        if platform_datetimes
        else min(
            observed_at
            for observed_at in observed_datetimes
            if observed_at.date() == listing_date
        ).isoformat()
        if any(
            observed_at.date() == listing_date
            for observed_at in observed_datetimes
        )
        else None
    )
    return listing_date, listing_date_source, listing_at


def _build_sales_series_payload(
    *,
    store_code: str,
    store_name: str,
    plid: str,
    offer_ids: list[str],
    skus: list[str],
    listing_date: date,
    listing_date_source: str,
    listing_at: str | None,
    through: date,
    units_by_date: dict[date, int],
    states: dict[date, DailySalesMetricState],
    revision_counts: dict[date, int],
) -> dict[str, Any]:
    points: list[dict[str, Any]] = []
    covered_dates: list[date] = []
    partial_dates: list[date] = []
    total_ordered_units = 0
    for metric_date in _date_range(listing_date, through):
        source_dates = _sast_dates_for_china_day(metric_date)
        source_states = [states.get(source_date) for source_date in source_dates]
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
                    revision_counts.get(source_date, 0)
                    for source_date in source_dates
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
        "listing_at": listing_at,
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
    normalized = _china_datetime(value)
    return normalized.date() if normalized is not None else None


def _china_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    normalized = value
    if normalized.tzinfo is None or normalized.utcoffset() is None:
        normalized = normalized.replace(tzinfo=UTC)
    return normalized.astimezone(CHINA)


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
