"""Read-only seller-return projections for ERP list and own-link detail views."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from takealot_ops.product_search import matches_product_search
from takealot_ops.storage.models import (
    CollectionRun,
    OfferCurrent,
    OfferSnapshot,
    ReturnItem,
)


SAST = ZoneInfo("Africa/Johannesburg")

RETURN_REASON_LABELS: dict[str, str] = {
    "changed_my_mind": "客户改变主意",
    "customer_cancellation": "客户取消",
    "defective_or_damaged": "商品有缺陷或损坏",
    "exception": "异常",
    "exchange_for_a_different_size_colour": "更换尺寸或颜色",
    "failed_delivery": "配送失败",
    "not_what_i_ordered": "与下单商品不符",
}

RETURN_OUTCOME_LABELS: dict[str, str] = {
    "pending_removal_order": "待创建移除单",
    "removal_order": "已进入移除单",
    "pending_sellable_stock": "待转可售库存",
    "sellable_stock": "已转可售库存",
}


def load_store_return_rows(
    session: Session,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    plid: str | None = None,
) -> list[dict[str, Any]]:
    """Read one store's local history, optionally bounded by SAST business dates."""
    statement = select(ReturnItem)
    if start_date is not None:
        range_start = datetime.combine(start_date, time.min, tzinfo=SAST)
        statement = statement.where(ReturnItem.return_date >= range_start)
    if end_date is not None:
        range_end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=SAST)
        statement = statement.where(ReturnItem.return_date < range_end)

    plid_identities: list[OfferCurrent | OfferSnapshot] = []
    if plid is not None:
        normalized_plid = str(plid).strip()
        current_matches = list(
            session.scalars(
                select(OfferCurrent).where(
                    OfferCurrent.productline_id == normalized_plid
                )
            )
        )
        snapshot_matches = list(
            session.scalars(
                select(OfferSnapshot)
                .where(OfferSnapshot.productline_id == normalized_plid)
                .order_by(OfferSnapshot.captured_at.desc())
            )
        )
        plid_identities = [*current_matches, *snapshot_matches]
        offer_ids, tsin_ids, skus = _identity_values(plid_identities)
        conditions: list[Any] = []
        if offer_ids:
            conditions.append(ReturnItem.offer_id.in_(offer_ids))
        if tsin_ids:
            conditions.append(
                ReturnItem.offer_id.is_(None) & ReturnItem.tsin_id.in_(tsin_ids)
            )
        if skus:
            conditions.append(
                ReturnItem.offer_id.is_(None)
                & ReturnItem.tsin_id.is_(None)
                & ReturnItem.sku.in_(skus)
            )
        if not conditions:
            return []
        statement = statement.where(or_(*conditions))

    return_items = list(
        session.scalars(
            statement.order_by(
                ReturnItem.return_date.desc(),
                ReturnItem.seller_return_id.desc(),
            )
        )
    )
    identities = _offer_identities(session, return_items, plid_identities)
    return [_return_payload(row, identities) for row in return_items]


def load_return_collection_status(
    session: Session,
    *,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """Describe complete, partial, stale, failed, or absent range coverage."""
    attempts = list(
        session.scalars(
        select(CollectionRun)
        .where(CollectionRun.run_type == "returns")
        .order_by(CollectionRun.started_at.desc(), CollectionRun.run_id.desc())
        )
    )
    latest_attempt = attempts[0] if attempts else None
    ranged_attempts = [
        (run, requested_range)
        for run in attempts
        if (requested_range := _collection_requested_range(run)) is not None
    ]
    relevant_attempt = next(
        (
            run
            for run, requested_range in ranged_attempts
            if requested_range[0] <= end_date and requested_range[1] >= start_date
        ),
        None,
    )
    covering_success = next(
        (
            run
            for run, requested_range in ranged_attempts
            if run.status == "success"
            and requested_range[0] <= start_date
            and requested_range[1] >= end_date
        ),
        None,
    )
    overlapping_success = next(
        (
            run
            for run, requested_range in ranged_attempts
            if run.status == "success"
            and requested_range[0] <= end_date
            and requested_range[1] >= start_date
        ),
        None,
    )
    reference_success = covering_success or overlapping_success
    if latest_attempt is None:
        data_status = "uncollected"
    elif relevant_attempt is not None and relevant_attempt.status != "success":
        data_status = "stale" if covering_success is not None else "failed"
    elif covering_success is not None:
        data_status = "collected"
    elif overlapping_success is not None:
        data_status = "partial"
    else:
        data_status = "uncollected"
    reference_range = (
        _collection_requested_range(reference_success)
        if reference_success is not None
        else None
    )
    return {
        "data_status": data_status,
        "last_attempt_at": _iso_datetime(
            relevant_attempt.finished_at or relevant_attempt.started_at
            if relevant_attempt is not None
            else None
        ),
        "last_success_at": _iso_datetime(
            reference_success.finished_at if reference_success is not None else None
        ),
        "requested_from": (
            reference_range[0].isoformat() if reference_range is not None else None
        ),
        "requested_through": (
            reference_range[1].isoformat() if reference_range is not None else None
        ),
        "record_count": (
            int((reference_success.counts or {}).get("records", 0))
            if reference_success is not None
            else None
        ),
        "latest_error": (
            relevant_attempt.error
            if relevant_attempt is not None and relevant_attempt.status != "success"
            else None
        ),
    }


def _collection_requested_range(run: CollectionRun | None) -> tuple[date, date] | None:
    if run is None:
        return None
    counts = run.counts or {}
    try:
        requested_start = date.fromordinal(int(counts["requested_start_ordinal"]))
        requested_end = date.fromordinal(int(counts["requested_end_ordinal"]))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if requested_start > requested_end:
        return None
    return requested_start, requested_end


def load_offer_returned_30_day_counter(
    session: Session,
    *,
    plid: str | None = None,
) -> dict[str, Any]:
    """Read the separate rolling Offer counter without calling it return detail."""
    statement = select(OfferCurrent)
    if plid is not None:
        statement = statement.where(OfferCurrent.productline_id == str(plid).strip())
    offers = list(session.scalars(statement))
    values = [
        int(offer.quantity_returned_30_days)
        for offer in offers
        if offer.quantity_returned_30_days is not None
    ]
    captured = [offer.captured_at for offer in offers if offer.captured_at is not None]
    return {
        "units": sum(values) if values else None,
        "covered_offer_count": len(values),
        "offer_count": len(offers),
        "captured_at": _iso_datetime(max(captured) if captured else None),
        "metric": "quantity_returned_30_days",
        "window": "rolling_30_days",
    }


def filter_return_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    query: str | None = None,
    reason: str | None = None,
    outcome: str | None = None,
) -> list[dict[str, Any]]:
    """Apply exact facets plus fuzzy product-name and identity substring search."""
    normalized_reason = str(reason or "").strip()
    normalized_outcome = str(outcome or "").strip()
    filtered: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        raw_lifecycle = row.get("removal_lifecycle")
        lifecycle: Mapping[str, Any] = (
            raw_lifecycle if isinstance(raw_lifecycle, Mapping) else {}
        )
        if normalized_reason and row.get("return_reason") != normalized_reason:
            continue
        statuses = {
            str(value).strip()
            for value in row.get("outcome_statuses", [])
            if str(value).strip()
        }
        if normalized_outcome and normalized_outcome not in statuses:
            continue
        if not matches_product_search(
            query,
            product_names=(
                row.get("product_title"),
                row.get("company_product_name"),
            ),
            other_values=(
                row.get("sku"),
                row.get("company_sku"),
                row.get("offer_id"),
                row.get("tsin_id"),
                row.get("seller_return_id"),
                row.get("return_reference_number"),
                row.get("order_id"),
                row.get("customer_comment"),
                row.get("store_name"),
                lifecycle.get("po_reference"),
                lifecycle.get("removal_order_id"),
                lifecycle.get("instruction_id"),
            ),
        ):
            continue
        filtered.append(row)
    filtered.sort(
        key=lambda item: (
            str(item.get("return_date") or ""),
            str(item.get("seller_return_id") or ""),
        ),
        reverse=True,
    )
    return filtered


def summarize_return_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize only the detailed rows in the selected filter window."""
    units = sum(max(0, int(row.get("quantity") or 0)) for row in rows)
    quality_reasons = {"defective_or_damaged", "not_what_i_ordered"}
    quality_units = sum(
        max(0, int(row.get("quantity") or 0))
        for row in rows
        if row.get("return_reason") in quality_reasons
    )
    sellable_units = sum(
        max(0, int(row.get("quantity") or 0))
        for row in rows
        if "sellable_stock" in row.get("outcome_statuses", [])
    )
    removal_units = sum(
        max(0, int(row.get("quantity") or 0))
        for row in rows
        if any(
            outcome in {"pending_removal_order", "removal_order"}
            for outcome in row.get("outcome_statuses", [])
        )
    )
    transaction_total = sum(
        (_decimal(row.get("transaction_total_incl_vat")) or Decimal("0"))
        for row in rows
    )
    products = {
        (
            str(row.get("store_code") or ""),
            str(
                row.get("offer_id")
                or row.get("sku")
                or row.get("tsin_id")
                or row.get("seller_return_id")
            ),
        )
        for row in rows
    }
    return {
        "return_count": len(rows),
        "return_units": units,
        "affected_product_count": len(products),
        "quality_related_units": quality_units,
        "sellable_stock_units": sellable_units,
        "removal_order_units": removal_units,
        "transaction_total_incl_vat": float(transaction_total),
    }


def return_filter_options(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return documented facets plus observed counts for the selected date/store set."""
    reason_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    for row in rows:
        reason = str(row.get("return_reason") or "").strip()
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        for raw_status in row.get("outcome_statuses", []):
            status = str(raw_status or "").strip()
            if status:
                outcome_counts[status] = outcome_counts.get(status, 0) + 1
    return {
        "reasons": [
            {
                "value": value,
                "label": RETURN_REASON_LABELS.get(value, value),
                "count": reason_counts.get(value, 0),
            }
            for value in dict.fromkeys([*RETURN_REASON_LABELS, *reason_counts])
        ],
        "outcomes": [
            {
                "value": value,
                "label": RETURN_OUTCOME_LABELS.get(value, value),
                "count": outcome_counts.get(value, 0),
            }
            for value in dict.fromkeys([*RETURN_OUTCOME_LABELS, *outcome_counts])
        ],
    }


def _offer_identities(
    session: Session,
    return_items: Sequence[ReturnItem],
    seed: Sequence[OfferCurrent | OfferSnapshot],
) -> dict[str, dict[str, OfferCurrent | OfferSnapshot]]:
    offer_ids = {str(row.offer_id) for row in return_items if row.offer_id}
    tsin_ids = {str(row.tsin_id) for row in return_items if row.tsin_id}
    skus = {str(row.sku) for row in return_items if row.sku}
    conditions = []
    if offer_ids:
        conditions.append(OfferCurrent.offer_id.in_(offer_ids))
    if tsin_ids:
        conditions.append(OfferCurrent.tsin_id.in_(tsin_ids))
    if skus:
        conditions.append(OfferCurrent.sku.in_(skus))
    current = list(seed)
    if conditions:
        current.extend(session.scalars(select(OfferCurrent).where(or_(*conditions))))

    snapshot_conditions = []
    if offer_ids:
        snapshot_conditions.append(OfferSnapshot.offer_id.in_(offer_ids))
    if tsin_ids:
        snapshot_conditions.append(OfferSnapshot.tsin_id.in_(tsin_ids))
    if skus:
        snapshot_conditions.append(OfferSnapshot.sku.in_(skus))
    if snapshot_conditions:
        current.extend(
            session.scalars(
                select(OfferSnapshot)
                .where(or_(*snapshot_conditions))
                .order_by(OfferSnapshot.captured_at.desc())
            )
        )

    maps: dict[str, dict[str, OfferCurrent | OfferSnapshot]] = {
        "offer_id": {},
        "tsin_id": {},
        "sku": {},
    }
    for offer in current:
        for field in maps:
            raw_value = getattr(offer, field, None)
            if raw_value in (None, ""):
                continue
            key = _identity_key(raw_value, field)
            maps[field].setdefault(key, offer)
    return maps


def _identity_values(
    identities: Sequence[OfferCurrent | OfferSnapshot],
) -> tuple[set[str], set[str], set[str]]:
    return (
        {str(row.offer_id) for row in identities if row.offer_id},
        {str(row.tsin_id) for row in identities if row.tsin_id},
        {str(row.sku) for row in identities if row.sku},
    )


def _return_payload(
    row: ReturnItem,
    identities: Mapping[str, Mapping[str, OfferCurrent | OfferSnapshot]],
) -> dict[str, Any]:
    identity = None
    for field in ("offer_id", "tsin_id", "sku"):
        raw_value = getattr(row, field, None)
        if raw_value in (None, ""):
            continue
        identity = identities.get(field, {}).get(_identity_key(raw_value, field))
        if identity is not None:
            break
    outcomes = _json_objects(row.outcomes)
    transactions = _json_objects(row.transactions)
    statuses = list(
        dict.fromkeys(
            str(outcome.get("status") or outcome.get("outcome") or "").strip()
            for outcome in outcomes
            if str(outcome.get("status") or outcome.get("outcome") or "").strip()
        )
    )
    if not statuses and row.return_status:
        statuses = [
            value.strip()
            for value in str(row.return_status).split(",")
            if value.strip()
        ]
    transaction_total = sum(
        (
            _decimal(
                transaction.get("amount_incl_vat")
                if "amount_incl_vat" in transaction
                else transaction.get("amount")
            )
            or Decimal("0")
        )
        for transaction in transactions
    )
    return_date = row.return_date.date().isoformat() if row.return_date else None
    return {
        "seller_return_id": row.seller_return_id,
        "order_id": row.order_id,
        "order_item_id": row.order_item_id,
        "offer_id": row.offer_id,
        "tsin_id": row.tsin_id,
        "sku": row.sku,
        "return_reference_number": row.return_reference_number,
        "quantity": int(row.quantity or 0),
        "return_date": return_date,
        "return_region": row.return_region,
        "return_reason": row.return_reason,
        "return_reason_label": RETURN_REASON_LABELS.get(
            str(row.return_reason or ""),
            row.return_reason or "未提供原因",
        ),
        "customer_comment": row.customer_comment,
        "outcome_statuses": statuses,
        "outcome_labels": [RETURN_OUTCOME_LABELS.get(status, status) for status in statuses],
        "outcomes": outcomes,
        "transactions": transactions,
        "transaction_total_incl_vat": float(transaction_total),
        "captured_at": _iso_datetime(row.captured_at),
        "productline_id": getattr(identity, "productline_id", None),
        "product_title": getattr(identity, "title", None),
        "image_url": getattr(identity, "image_url", None),
        "offer_quantity_returned_30_days": getattr(
            identity,
            "quantity_returned_30_days",
            None,
        ),
    }


def _identity_key(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    return normalized.casefold() if field == "sku" else normalized


def _json_objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _iso_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
