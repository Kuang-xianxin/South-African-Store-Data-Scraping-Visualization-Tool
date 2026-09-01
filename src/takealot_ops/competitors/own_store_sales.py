"""Official own-store daily sales history for competitor detail views."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from collections import defaultdict
from typing import Any, Iterable
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
    SalesRevenueRevision,
    StoreOfferBaseline,
    StoreOfferObservation,
)
from takealot_ops.storage.repository import is_closed_day_sales_revision
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


def build_own_store_sales_series_bulk(
    session: Session,
    *,
    plids: Iterable[str],
    store_codes: set[str],
    through: date,
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
            )
        for plid, series in store_series.items():
            result[plid].append(series)
    return result


def _store_sales_series_bulk(
    session: Session,
    *,
    plids: list[str],
    store_code: str,
    store_name: str,
    through: date,
) -> dict[str, dict[str, Any]]:
    current_offers = list(
        session.scalars(
            select(OfferCurrent)
            .where(OfferCurrent.productline_id.in_(plids))
            .order_by(OfferCurrent.productline_id, OfferCurrent.offer_id)
        )
    )
    current_by_plid: dict[str, list[OfferCurrent]] = defaultdict(list)
    for row in current_offers:
        plid = str(row.productline_id or "").strip()
        if plid:
            current_by_plid[plid].append(row)
    active_plids = sorted(current_by_plid)
    if not active_plids:
        return {}

    snapshots = list(
        session.scalars(
            select(OfferSnapshot)
            .where(OfferSnapshot.productline_id.in_(active_plids))
            .order_by(
                OfferSnapshot.productline_id,
                OfferSnapshot.snapshot_date,
                OfferSnapshot.offer_id,
            )
        )
    )
    baselines = list(
        session.scalars(
            select(StoreOfferBaseline)
            .where(StoreOfferBaseline.productline_id.in_(active_plids))
            .order_by(
                StoreOfferBaseline.productline_id,
                StoreOfferBaseline.display_date,
                StoreOfferBaseline.offer_id,
            )
        )
    )
    observations = list(
        session.scalars(
            select(StoreOfferObservation)
            .where(StoreOfferObservation.productline_id.in_(active_plids))
            .order_by(
                StoreOfferObservation.productline_id,
                StoreOfferObservation.display_date,
                StoreOfferObservation.offer_id,
            )
        )
    )

    snapshots_by_plid: dict[str, list[OfferSnapshot]] = defaultdict(list)
    baselines_by_plid: dict[str, list[StoreOfferBaseline]] = defaultdict(list)
    observations_by_plid: dict[str, list[StoreOfferObservation]] = defaultdict(list)
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
        platform_dates = [
            listed_day
            for row in plid_current
            if (listed_day := _china_day(row.created_at)) is not None
        ] + [
            listed_day
            for row in plid_snapshots
            if (listed_day := _china_day(row.created_at)) is not None
        ]
        observed_dates = [
            *(
                observed_day
                for row in plid_current
                if (observed_day := _china_day(row.captured_at)) is not None
            ),
            *(
                observed_day
                for row in plid_snapshots
                if (observed_day := _china_day(row.captured_at)) is not None
            ),
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
        listing_date_source = "platform" if platform_dates else "first_observed"
        for offer_id in offer_ids:
            plids_by_offer_id[offer_id].add(plid)
        all_offer_ids.update(offer_ids)
        for display_date in _date_range(listing_date, through):
            required_source_dates.update(_sast_dates_for_china_day(display_date))
        prepared[plid] = {
            "offer_ids": offer_ids,
            "skus": skus,
            "listing_date": listing_date,
            "listing_date_source": listing_date_source,
        }

    units_by_plid_and_date: dict[str, dict[date, int]] = defaultdict(dict)
    if all_offer_ids:
        sale_rows = session.execute(
            select(
                SaleItem.offer_id,
                SaleItem.order_date,
                SaleItem.quantity,
            )
            .where(SaleItem.offer_id.in_(sorted(all_offer_ids)))
            .order_by(SaleItem.order_date, SaleItem.order_item_id)
        ).all()
        for offer_id, order_date, quantity in sale_rows:
            sales_date = _china_day(order_date)
            if sales_date is None or sales_date > through:
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
        units_by_date = units_by_plid_and_date.get(plid, {})
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
                    revision_counts.get(source_date, 0)
                    for source_date in _sast_dates_for_china_day(metric_date)
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
