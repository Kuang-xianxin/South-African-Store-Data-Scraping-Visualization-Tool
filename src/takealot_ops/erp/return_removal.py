"""Conservative Takealot removal-order and W8 return lifecycle projections.

The Marketplace ``/returns`` resource does not expose the full Manage Removal
Orders module.  This module keeps a safe local snapshot of all three portal tabs
and all portal order types.  A separate projection exposes every PO even when it
does not link to a seller-return row; return and W8 joins still require exact
identifiers, with W8 requiring both the PO reference and an item SKU.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


SAST = ZoneInfo("Africa/Johannesburg")
REMOVAL_SNAPSHOT_PROVIDER = "takealot_removal_orders"
REMOVAL_STAGES = ("submitted", "pickup_ready", "closed")
REMOVAL_STAGE_LABELS = {
    "submitted": "Submitted",
    "pickup_ready": "Ready For Pickup",
    "closed": "Closed",
}
REMOVAL_ORDER_TYPES = {
    1: "Removal Order",
    2: "Takealot Removal Order",
    3: "Returns Removal Order",
}
REMOVAL_STATUS_LABELS = {
    1: "Processing",
    2: "Processing",
    3: "Failed to Process",
    4: "Processing",
    5: "Submitted",
    6: "Order Being Prepared",
    7: "Processing",
    8: "Ready for Pickup",
    9: "Processing",
    10: "Fully Collected",
    11: "Partially Collected",
    12: "Expired",
    13: "Processing",
    14: "Expired (Disposed)",
    15: "Cancelled",
    16: "Failed",
    17: "Collection in Progress",
}
_REFERENCE_TOKEN = re.compile(r"(?<!\d)\d{6,}(?!\d)")


def collect_removal_order_snapshot(client: Any, token: str) -> dict[str, Any]:
    """Read and sanitize the full Manage Removal Orders module."""
    orders: list[dict[str, Any]] = []
    warnings: list[str] = []
    for stage in REMOVAL_STAGES:
        page = 1
        while page <= 100:
            payload = client.removal_orders(
                token,
                stage,
                page_number=page,
                page_size=100,
            )
            rows = _result_rows(payload)
            for source in rows:
                order = _sanitize_order(source, stage)
                if order.get("order_type_id") not in {None, *REMOVAL_ORDER_TYPES}:
                    warnings.append(
                        f"{REMOVAL_STAGE_LABELS[stage]} 返回未知 Order Type，已按原值保留"
                    )
                removal_order_id = order["removal_order_id"]
                if not removal_order_id:
                    warnings.append(f"{stage} 列表存在缺少 removal_order_id 的记录，已跳过")
                    continue
                item_rows: list[dict[str, Any]] = []
                item_page = 1
                while item_page <= 100:
                    item_payload = client.removal_order_items(
                        token,
                        stage,
                        removal_order_id,
                        page_number=item_page,
                        page_size=100,
                    )
                    page_items = _result_rows(item_payload)
                    item_rows.extend(page_items)
                    item_total = _result_total(item_payload)
                    if item_total is not None and item_page * 100 >= item_total:
                        break
                    if len(page_items) < 100:
                        break
                    item_page += 1
                if item_page > 100:
                    warnings.append(
                        f"移除单 {removal_order_id} 商品超过 100 页，已停止继续读取"
                    )
                order["items"] = [_sanitize_item(item) for item in item_rows]
                orders.append(order)
            total = _result_total(payload)
            if total is not None and page * 100 >= total:
                break
            if len(rows) < 100:
                break
            page += 1
        if page > 100:
            warnings.append(f"{stage} 列表超过 100 页，已停止继续读取")

    return {
        "schema_version": 2,
        "connected": True,
        "provider": "Takealot Seller Portal removal orders",
        "order_type_filter": "All",
        "order_type_ids": sorted(REMOVAL_ORDER_TYPES),
        "orders": orders,
        "counts": {
            stage: sum(1 for order in orders if order.get("stage") == stage)
            for stage in REMOVAL_STAGES
        },
        "warnings": list(dict.fromkeys(warnings)),
    }


def attach_removal_lifecycles(
    rows: Sequence[Mapping[str, Any]],
    *,
    removal_snapshot: Mapping[str, Any] | None,
    w8_return_orders: Sequence[Mapping[str, Any]] = (),
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Attach one explicit lifecycle to every return row without fuzzy matching."""
    orders = _snapshot_orders(removal_snapshot)
    seller_index = _unique_item_index(orders, "seller_return_ids")
    rrn_index = _unique_item_index(orders, "return_reference_numbers")
    result: list[dict[str, Any]] = []
    effective_today = today or datetime.now(SAST).date()
    for source in rows:
        row = dict(source)
        statuses = {
            _text(value).casefold()
            for value in row.get("outcome_statuses", [])
            if _text(value)
        }
        seller_return_id = _text(row.get("seller_return_id"))
        rrn = _text(row.get("return_reference_number"))
        candidate = seller_index.get(seller_return_id) if seller_return_id else None
        link_basis = "seller_return_id" if candidate else None
        if candidate is None and rrn:
            candidate = rrn_index.get(rrn)
            link_basis = "return_reference_number" if candidate else None
        if candidate is None:
            row["removal_lifecycle"] = _unlinked_lifecycle(statuses)
            result.append(row)
            continue
        order, item = candidate
        lifecycle = _linked_lifecycle(
            order,
            item,
            link_basis=link_basis or "unknown",
            today=effective_today,
        )
        lifecycle["w8"] = _match_w8_return(
            row,
            order,
            item,
            w8_return_orders,
        )
        row["removal_lifecycle"] = lifecycle
        result.append(row)
    return result


def summarize_removal_lifecycles(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Summarize lifecycle evidence at return-record level; missing is never zero-filled."""
    summary = {
        "relevant_count": 0,
        "pending_creation_count": 0,
        "linked_po_count": 0,
        "ready_count": 0,
        "collectable_count": 0,
        "expired_count": 0,
        "expiring_count": 0,
        "booked_count": 0,
        "fully_collected_count": 0,
        "w8_received_count": 0,
        "w8_pending_shelf_units": 0,
        "w8_shelved_units": 0,
        "w8_defective_units": 0,
        "unknown_after_pickup_count": 0,
    }
    seen_w8_lines: set[tuple[str, str, str]] = set()
    for row in rows:
        lifecycle = row.get("removal_lifecycle")
        if not isinstance(lifecycle, Mapping) or lifecycle.get("stage") == "not_applicable":
            continue
        summary["relevant_count"] += 1
        stage = _text(lifecycle.get("stage"))
        if stage == "pending_creation":
            summary["pending_creation_count"] += 1
        if lifecycle.get("linked") is True:
            summary["linked_po_count"] += 1
        if stage == "pickup_ready":
            summary["ready_count"] += 1
        if lifecycle.get("can_collect") is True:
            summary["collectable_count"] += 1
        expiry_status = lifecycle.get("expiry_status")
        if expiry_status == "expired":
            summary["expired_count"] += 1
        elif expiry_status == "expiring":
            summary["expiring_count"] += 1
        if lifecycle.get("has_booking") is True:
            summary["booked_count"] += 1
        if lifecycle.get("collection_status") == "fully_collected":
            summary["fully_collected_count"] += 1
        w8 = lifecycle.get("w8")
        if not isinstance(w8, Mapping):
            continue
        w8_line = (
            _text(w8.get("order_no")),
            _text(w8.get("platform_sku")).casefold(),
            _text(w8.get("company_sku")).casefold(),
        )
        is_new_w8_line = w8.get("match_status") == "linked" and w8_line not in seen_w8_lines
        if is_new_w8_line:
            seen_w8_lines.add(w8_line)
        if is_new_w8_line and w8.get("received") is True:
            summary["w8_received_count"] += 1
        if is_new_w8_line:
            summary["w8_pending_shelf_units"] += _nonnegative_int(
                w8.get("pending_shelf_quantity")
            )
            summary["w8_shelved_units"] += _nonnegative_int(
                w8.get("shelved_quantity")
            )
            summary["w8_defective_units"] += _nonnegative_int(
                w8.get("defective_quantity")
            )
        if (
            lifecycle.get("collection_status") in {"fully_collected", "partly_collected"}
            and w8.get("match_status") != "linked"
        ):
            summary["unknown_after_pickup_count"] += 1
    return summary


def removal_tracking_status(
    store_code: str,
    store_name: str,
    snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload = snapshot.get("payload") if isinstance(snapshot, Mapping) else None
    orders = _snapshot_orders(payload if isinstance(payload, Mapping) else None)
    return {
        "store_code": store_code,
        "store_name": store_name,
        "data_status": "synced" if snapshot is not None else "uncollected",
        "synced_at": snapshot.get("fetched_at") if isinstance(snapshot, Mapping) else None,
        "order_count": len(orders),
        "counts": {
            stage: sum(1 for order in orders if order.get("stage") == stage)
            for stage in REMOVAL_STAGES
        },
        "message": (
            "已读取本地 Seller Portal 全部移除单快照"
            if snapshot is not None
            else "尚无 Seller Portal 移除单快照"
        ),
    }


def project_removal_orders(
    store_code: str,
    store_name: str,
    snapshot: Mapping[str, Any] | None,
    *,
    w8_return_orders: Sequence[Mapping[str, Any]] = (),
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Project every locally snapshotted PO independently of ``/returns`` filters."""
    raw_payload = snapshot.get("payload") if isinstance(snapshot, Mapping) else None
    payload = raw_payload if isinstance(raw_payload, Mapping) else snapshot
    orders = _snapshot_orders(payload if isinstance(payload, Mapping) else None)
    synced_at = snapshot.get("fetched_at") if isinstance(snapshot, Mapping) else None
    effective_today = today or datetime.now(SAST).date()
    projected: list[dict[str, Any]] = []
    for source in orders:
        order = _normalize_snapshot_order(source)
        stage = _text(order.get("stage")) or "submitted"
        expiry_status, days_until_expiry = _expiry(order, effective_today)
        status_id = _optional_int(order.get("status_id"))
        if stage == "pickup_ready":
            can_collect: bool | None = (
                expiry_status != "expired" and status_id not in {7, 12}
            )
        elif stage in {"submitted", "closed"}:
            can_collect = False
        else:
            can_collect = None

        projected_items: list[dict[str, Any]] = []
        for raw_item in order.get("items", []):
            if not isinstance(raw_item, Mapping):
                continue
            item = _normalize_snapshot_item(raw_item)
            item["collection_status"] = _collection_status(
                stage,
                _optional_int(item.get("quantity_requested")),
                _optional_int(item.get("quantity_collected")),
                status_id=status_id,
            )
            item["w8"] = _match_w8_return({}, order, item, w8_return_orders)
            projected_items.append(item)

        projected_order = {
            **order,
            "store_code": store_code,
            "store_name": store_name,
            "store_scope_key": f"{store_code}:{order.get('removal_order_id') or ''}",
            "synced_at": synced_at,
            "stage": stage,
            "stage_label": REMOVAL_STAGE_LABELS.get(stage, stage),
            "expiry_status": expiry_status,
            "days_until_expiry": days_until_expiry,
            "can_collect": can_collect,
            "collection_status": _collection_status(
                stage,
                _optional_int(order.get("quantity_requested")),
                _optional_int(order.get("quantity_collected")),
                status_id=status_id,
            ),
            "items": projected_items,
            "w8_summary": _summarize_order_w8(projected_items),
        }
        projected.append(projected_order)
    return projected


def removal_snapshot_warnings(snapshot: Mapping[str, Any] | None) -> list[str]:
    """Return only plain warning strings from one persisted removal snapshot."""
    raw_payload = snapshot.get("payload") if isinstance(snapshot, Mapping) else None
    payload = raw_payload if isinstance(raw_payload, Mapping) else snapshot
    if not isinstance(payload, Mapping) or not isinstance(payload.get("warnings"), list):
        return []
    return [text for value in payload["warnings"] if (text := _text(value))]


def _summarize_order_w8(items: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    summary = {
        "item_count": len(items),
        "matched_item_count": 0,
        "received_item_count": 0,
        "awaiting_receipt_item_count": 0,
        "unresolved_item_count": 0,
        "pending_shelf_units": 0,
        "shelved_units": 0,
        "defective_units": 0,
    }
    seen_lines: set[tuple[str, str, str]] = set()
    for item in items:
        w8 = item.get("w8")
        if not isinstance(w8, Mapping) or w8.get("match_status") != "linked":
            summary["unresolved_item_count"] += 1
            continue
        line_key = (
            _text(w8.get("order_no")),
            _normalize_sku(w8.get("platform_sku")),
            _normalize_sku(w8.get("company_sku")),
        )
        if line_key in seen_lines:
            continue
        seen_lines.add(line_key)
        summary["matched_item_count"] += 1
        if w8.get("received") is True:
            summary["received_item_count"] += 1
        elif w8.get("received") is False:
            summary["awaiting_receipt_item_count"] += 1
        summary["pending_shelf_units"] += _nonnegative_int(
            w8.get("pending_shelf_quantity")
        )
        summary["shelved_units"] += _nonnegative_int(w8.get("shelved_quantity"))
        summary["defective_units"] += _nonnegative_int(w8.get("defective_quantity"))
    return summary


def _normalize_snapshot_order(source: Mapping[str, Any]) -> dict[str, Any]:
    stage = _text(source.get("stage")) or "submitted"
    normalized = _sanitize_order(source, stage)
    raw_items = source.get("items")
    normalized["items"] = (
        [dict(item) for item in raw_items if isinstance(item, Mapping)]
        if isinstance(raw_items, list)
        else []
    )
    return normalized


def _normalize_snapshot_item(source: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(source)
    for field in (
        "removal_order_id",
        "removal_order_item_id",
        "offer_id",
        "sku",
        "tsin_id",
        "product_title",
        "image_url",
        "product_url",
        "offer_status",
        "warehouse_id",
    ):
        item.setdefault(field, None)
    for field in (
        "offer_status_id",
        "quantity_requested",
        "quantity_prepared",
        "quantity_collected",
        "handling_fee_cents",
    ):
        item.setdefault(field, None)
    item.setdefault("storage_fee_eligible", None)
    item.setdefault("has_item_mismatch", False)
    for field in ("seller_return_ids", "return_reference_numbers", "return_informations"):
        values = item.get(field)
        item[field] = values if isinstance(values, list) else []
    return item


def _sanitize_order(source: Mapping[str, Any], stage: str) -> dict[str, Any]:
    raw_flags = source.get("flags")
    flags: Mapping[str, Any] = raw_flags if isinstance(raw_flags, Mapping) else {}
    raw_urgency = source.get("urgency")
    urgency: Mapping[str, Any] = (
        raw_urgency if isinstance(raw_urgency, Mapping) else {}
    )
    order_type = source.get("order_type")
    order_type_id = (
        _mapping_int(order_type, "id", "order_type_id")
        or _optional_int(source.get("order_type_id"))
        or _optional_int(order_type)
    )
    order_type_label = _mapping_label(order_type) or _non_numeric_text(order_type)
    if order_type_id is None:
        order_type_id = _label_id(order_type_label, REMOVAL_ORDER_TYPES)
    status = source.get("status")
    status_id = (
        _mapping_int(status, "id", "status_id")
        or _optional_int(source.get("status_id"))
        or _optional_int(status)
    )
    status_label = _mapping_label(status) or _non_numeric_text(status)
    warehouse = source.get("warehouse_id")
    boxes = _first_int(source, "number_of_boxes", "total_boxes", "boxes")
    return {
        "stage": stage,
        "removal_order_id": _text(source.get("removal_order_id")),
        "instruction_id": _text(source.get("instruction_id")) or None,
        "reference": _text(source.get("reference")) or None,
        "order_type": order_type_label
        or (REMOVAL_ORDER_TYPES.get(order_type_id) if order_type_id is not None else None)
        or None,
        "order_type_id": order_type_id,
        "status": status_label
        or (REMOVAL_STATUS_LABELS.get(status_id) if status_id is not None else None)
        or None,
        "status_id": status_id,
        "removal_reason": _mapping_label(source.get("removal_reason"))
        or _mapping_label(source.get("reason"))
        or _text(source.get("removal_reason") or source.get("reason"))
        or None,
        "date_submitted": _date_text(source.get("date_submitted")),
        "ship_by_date": _date_text(source.get("ship_by_date")),
        "disposal_date": _date_text(source.get("disposal_date")),
        "pickup_date_start": _datetime_text(source.get("pickup_date_start")),
        "pickup_date_end": _datetime_text(source.get("pickup_date_end")),
        "date_closed": _date_text(source.get("date_closed")),
        "days_until_expiry": _optional_int(source.get("days_until_expiry")),
        "expired": _optional_bool(flags.get("expired")),
        "hide_booking": _optional_bool(flags.get("hide_booking")),
        "has_booking": _optional_bool(source.get("has_booking")),
        "missed": _optional_bool(urgency.get("missed")),
        "urgent": _optional_bool(urgency.get("urgent")),
        "quantity_requested": _first_int(
            source,
            "total_quantity_to_remove",
            "quantity_to_remove",
            "quantity_requested",
        ),
        "quantity_prepared": _first_int(
            source,
            "total_quantity_prepared",
            "quantity_prepared",
        ),
        "quantity_collected": _first_int(
            source,
            "total_quantity_removed",
            "quantity_removed",
            "quantity_collected",
        ),
        "number_of_boxes": boxes,
        "boxes": boxes,
        "total_offers": _first_int(source, "total_offers"),
        "total_weight_grams": _first_int(source, "total_weight_grams"),
        "total_handling_fee_cents": _first_int(
            source,
            "total_handling_fee_cents",
        ),
        "warehouse_id": _mapping_label(warehouse) or _text(warehouse) or None,
        "returns_region_id": _text(source.get("returns_region_id")) or None,
        "returns_facility_code": _text(source.get("returns_facility_code")) or None,
        "returns_leadtime_days": _optional_int(source.get("returns_leadtime_days")),
        "failure_reason": _text(source.get("failure_reason")) or None,
        "items": [],
    }


def _sanitize_item(source: Mapping[str, Any]) -> dict[str, Any]:
    raw_offer = source.get("offer")
    offer: Mapping[str, Any] = raw_offer if isinstance(raw_offer, Mapping) else {}
    raw_tsin = offer.get("tsin")
    tsin: Mapping[str, Any] = raw_tsin if isinstance(raw_tsin, Mapping) else {}
    return_information = source.get("return_informations")
    if not isinstance(return_information, list):
        return_information = source.get("return_information")
    info_rows = (
        [item for item in return_information if isinstance(item, Mapping)]
        if isinstance(return_information, list)
        else []
    )
    seller_ids = {
        _text(value)
        for value in [
            source.get("seller_return_id"),
            *(item.get("seller_return_id") for item in info_rows),
        ]
        if _text(value)
    }
    rrns = {
        _text(value)
        for value in [source.get("rrn"), *(item.get("rrn") for item in info_rows)]
        if _text(value)
    }
    sanitized_information = [
        {
            "id": _text(item.get("id")) or None,
            "rrn": _text(item.get("rrn")) or None,
            "seller_return_id": _text(item.get("seller_return_id")) or None,
            "has_item_mismatch": _optional_bool(item.get("has_item_mismatch")),
            "created_at": _datetime_text(item.get("created_at")),
            "modified_at": _datetime_text(item.get("modified_at")),
        }
        for item in info_rows
    ]
    offer_status = offer.get("offer_status")
    warehouse = source.get("warehouse_id")
    return {
        "removal_order_id": _text(source.get("removal_order_id")) or None,
        "removal_order_item_id": _text(source.get("removal_order_item_id")) or None,
        "offer_id": _text(offer.get("offer_id") or source.get("offer_id")) or None,
        "sku": _text(offer.get("sku") or source.get("sku")) or None,
        "tsin_id": _text(
            tsin.get("tsin_id") or tsin.get("id") or source.get("tsin_id")
        )
        or None,
        "product_title": _text(tsin.get("title") or source.get("product_title"))
        or None,
        "image_url": _text(tsin.get("image_url") or source.get("image_url")) or None,
        "product_url": _text(tsin.get("product_url") or source.get("product_url"))
        or None,
        "offer_status": _mapping_label(offer_status)
        or _non_numeric_text(offer_status)
        or None,
        "offer_status_id": _mapping_int(offer_status, "id", "offer_status_id")
        or _optional_int(offer.get("offer_status_id")),
        "storage_fee_eligible": _optional_bool(offer.get("storage_fee_eligible")),
        "warehouse_id": _mapping_label(warehouse) or _text(warehouse) or None,
        "seller_return_ids": sorted(seller_ids),
        "return_reference_numbers": sorted(rrns),
        "return_informations": sanitized_information,
        "has_item_mismatch": any(
            item.get("has_item_mismatch") is True for item in info_rows
        ),
        "quantity_requested": _first_int(source, "quantity_to_remove"),
        "quantity_prepared": _first_int(source, "quantity_prepared"),
        "quantity_collected": _first_int(source, "quantity_removed"),
        "handling_fee_cents": _first_int(source, "handling_fee_cents"),
    }


def _linked_lifecycle(
    order: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    link_basis: str,
    today: date,
) -> dict[str, Any]:
    stage = _text(order.get("stage")) or "unlinked"
    requested = _optional_int(item.get("quantity_requested"))
    prepared = _optional_int(item.get("quantity_prepared"))
    collected = _optional_int(item.get("quantity_collected"))
    order_requested = _optional_int(order.get("quantity_requested"))
    order_prepared = _optional_int(order.get("quantity_prepared"))
    order_collected = _optional_int(order.get("quantity_collected"))
    expiry_status, days_until_expiry = _expiry(order, today)
    if stage == "pickup_ready":
        can_collect: bool | None = expiry_status != "expired"
    elif stage in {"submitted", "closed"}:
        can_collect = False
    else:
        can_collect = None
    return {
        "linked": True,
        "link_basis": link_basis,
        "stage": stage,
        "po_reference": order.get("reference"),
        "removal_order_id": order.get("removal_order_id"),
        "instruction_id": order.get("instruction_id"),
        "status": order.get("status"),
        "removal_reason": order.get("removal_reason"),
        "expiry_status": expiry_status,
        "disposal_date": order.get("disposal_date"),
        "days_until_expiry": days_until_expiry,
        "can_collect": can_collect,
        "has_booking": order.get("has_booking"),
        "pickup_date_start": order.get("pickup_date_start"),
        "pickup_date_end": order.get("pickup_date_end"),
        "date_submitted": order.get("date_submitted"),
        "date_closed": order.get("date_closed"),
        "quantity_requested": requested,
        "quantity_prepared": prepared,
        "quantity_collected": collected,
        "collection_status": _collection_status(stage, requested, collected),
        "order_quantity_requested": order_requested,
        "order_quantity_prepared": order_prepared,
        "order_quantity_collected": order_collected,
        "order_collection_status": _collection_status(
            stage,
            order_requested,
            order_collected,
        ),
        "item_mismatch": item.get("has_item_mismatch") is True,
        "w8": _empty_w8("unlinked", "尚未关联长睿退货单"),
    }


def _unlinked_lifecycle(statuses: set[str]) -> dict[str, Any]:
    if "pending_removal_order" in statuses:
        stage = "pending_creation"
        message = "Takealot 尚未创建移除 PO"
        can_collect: bool | None = False
    elif "removal_order" in statuses:
        stage = "unlinked"
        message = "已进入移除单，但本地 PO 快照未精确关联"
        can_collect = None
    else:
        stage = "not_applicable"
        message = "当前退货不涉及移除 PO"
        can_collect = None
    return {
        "linked": False,
        "link_basis": None,
        "stage": stage,
        "po_reference": None,
        "removal_order_id": None,
        "instruction_id": None,
        "status": None,
        "removal_reason": None,
        "expiry_status": "unknown",
        "disposal_date": None,
        "days_until_expiry": None,
        "can_collect": can_collect,
        "has_booking": None,
        "pickup_date_start": None,
        "pickup_date_end": None,
        "date_submitted": None,
        "date_closed": None,
        "quantity_requested": None,
        "quantity_prepared": None,
        "quantity_collected": None,
        "collection_status": "unknown",
        "order_quantity_requested": None,
        "order_quantity_prepared": None,
        "order_quantity_collected": None,
        "order_collection_status": "unknown",
        "item_mismatch": False,
        "message": message,
        "w8": _empty_w8("unlinked", "没有可用于长睿关联的移除 PO"),
    }


def _match_w8_return(
    row: Mapping[str, Any],
    order: Mapping[str, Any],
    item: Mapping[str, Any],
    w8_orders: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    reference = _normalize_reference(order.get("reference"))
    if not reference:
        return _empty_w8("unlinked", "移除单没有可核对的 PO reference")
    platform_skus = {
        _normalize_sku(value)
        for value in (row.get("sku"), item.get("sku"))
        if _normalize_sku(value)
    }
    company_skus = {
        _normalize_sku(value)
        for value in (row.get("company_sku"),)
        if _normalize_sku(value)
    }
    candidates: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for w8_order in w8_orders:
        references = {
            _normalize_reference(value)
            for value in w8_order.get("po_references", [])
            if _normalize_reference(value)
        }
        if reference not in references:
            continue
        for w8_item in w8_order.get("items", []):
            if not isinstance(w8_item, Mapping):
                continue
            platform_match = bool(
                platform_skus
                and _normalize_sku(w8_item.get("platform_sku")) in platform_skus
            )
            company_match = bool(
                company_skus
                and _normalize_sku(w8_item.get("company_sku")) in company_skus
            )
            if platform_match or company_match:
                candidates.append((w8_order, w8_item))
    if not candidates:
        return _empty_w8("unlinked", "长睿未找到 PO + SKU 双重一致的退货单")
    if len(candidates) != 1:
        return _empty_w8("ambiguous", "长睿存在多个 PO + SKU 候选，未自动合并")
    w8_order, w8_item = candidates[0]
    inbound_quantity = _nonnegative_int(w8_item.get("inbound_quantity"))
    inbound_date = _text(w8_item.get("inbound_date")) or _text(w8_order.get("inbound_date"))
    received = bool(inbound_date or inbound_quantity > 0)
    shelved = max(
        _nonnegative_int(w8_item.get("shelf_quantity")),
        _nonnegative_int(w8_item.get("total_shelf_quantity")),
    )
    defective = max(
        _nonnegative_int(w8_item.get("defective_quantity")),
        _nonnegative_int(w8_item.get("total_defective_quantity")),
    )
    pending_shelf = _nonnegative_int(w8_item.get("pending_shelf_quantity")) if received else 0
    if not received:
        disposition = "awaiting_receipt"
    elif shelved and defective:
        disposition = "mixed"
    elif defective:
        disposition = "defective"
    elif shelved:
        disposition = "shelved"
    elif pending_shelf:
        disposition = "pending_shelf"
    else:
        disposition = "received_unresolved"
    return {
        "match_status": "linked",
        "message": "PO reference 与 SKU 均精确一致",
        "order_no": w8_order.get("order_no"),
        "platform_sku": w8_item.get("platform_sku"),
        "company_sku": w8_item.get("company_sku"),
        "status": w8_order.get("status"),
        "forecast_quantity": _nonnegative_int(w8_item.get("returned_quantity")),
        "inbound_quantity": inbound_quantity,
        "received": received,
        "inbound_date": inbound_date or None,
        "shelf_date": _text(w8_item.get("shelf_date"))
        or _text(w8_order.get("shelf_date"))
        or None,
        "pending_shelf_quantity": pending_shelf,
        "shelved_quantity": shelved,
        "defective_quantity": defective,
        "disposition": disposition,
    }


def _empty_w8(status: str, message: str) -> dict[str, Any]:
    return {
        "match_status": status,
        "message": message,
        "order_no": None,
        "platform_sku": None,
        "company_sku": None,
        "status": None,
        "forecast_quantity": 0,
        "inbound_quantity": None,
        "received": None,
        "inbound_date": None,
        "shelf_date": None,
        "pending_shelf_quantity": 0,
        "shelved_quantity": 0,
        "defective_quantity": 0,
        "disposition": "unknown",
    }


def _unique_item_index(
    orders: Sequence[Mapping[str, Any]], field: str
) -> dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]]:
    candidates: dict[str, list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = {}
    for order in orders:
        for item in order.get("items", []):
            if not isinstance(item, Mapping):
                continue
            for raw_value in item.get(field, []):
                value = _text(raw_value)
                if value:
                    candidates.setdefault(value, []).append((order, item))
    return {key: values[0] for key, values in candidates.items() if len(values) == 1}


def _snapshot_orders(snapshot: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(snapshot, Mapping):
        return []
    rows = snapshot.get("orders")
    return [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _expiry(order: Mapping[str, Any], today: date) -> tuple[str, int | None]:
    disposal_date = _parse_date(order.get("disposal_date"))
    days = (
        (disposal_date - today).days
        if disposal_date is not None
        else _optional_int(order.get("days_until_expiry"))
    )
    status_id = _optional_int(order.get("status_id"))
    status = _text(order.get("status")).casefold()
    if (
        order.get("expired") is True
        or status_id in {12, 14}
        or "expired" in status
        or (days is not None and days < 0)
    ):
        return "expired", days
    if days is not None and days <= 3:
        return "expiring", days
    if days is not None or disposal_date is not None:
        return "active", days
    return "unknown", None


def _collection_status(
    stage: str,
    requested: int | None,
    collected: int | None,
    *,
    status_id: int | None = None,
) -> str:
    if status_id == 10:
        return "fully_collected"
    if status_id == 11:
        return "partly_collected"
    if collected is not None and collected > 0:
        if requested is not None and requested > 0 and collected >= requested:
            return "fully_collected"
        return "partly_collected"
    if stage == "closed" and collected == 0:
        return "not_collected"
    if stage in {"submitted", "pickup_ready"}:
        return "not_collected"
    return "unknown"


def _result_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in ("results", "items", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(row) for row in value if isinstance(row, Mapping)]
    data = payload.get("data")
    if isinstance(data, Mapping):
        return _result_rows(data)
    return []


def _result_total(payload: Mapping[str, Any]) -> int | None:
    for key in ("total", "total_count", "count"):
        parsed = _optional_int(payload.get(key))
        if parsed is not None:
            return parsed
    data = payload.get("data")
    return _result_total(data) if isinstance(data, Mapping) else None


def _mapping_label(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    return _text(value.get("name") or value.get("label") or value.get("description"))


def _mapping_int(value: Any, *keys: str) -> int | None:
    if not isinstance(value, Mapping):
        return None
    for key in keys:
        parsed = _optional_int(value.get(key))
        if parsed is not None:
            return parsed
    return None


def _first_int(source: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key in source:
            parsed = _optional_int(source.get(key))
            if parsed is not None:
                return max(0, parsed)
    return None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _nonnegative_int(value: Any) -> int:
    return max(0, _optional_int(value) or 0)


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (0, "0", "false", "False"):
        return False
    if value in (1, "1", "true", "True"):
        return True
    return None


def _label_id(label: str, labels: Mapping[int, str]) -> int | None:
    normalized = label.casefold()
    return next(
        (identifier for identifier, value in labels.items() if value.casefold() == normalized),
        None,
    )


def _non_numeric_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return ""
    text = _text(value)
    if not text:
        return ""
    try:
        float(text)
    except ValueError:
        return text
    return ""


def _epoch_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, (bool, date, datetime)):
        return None
    text = _text(value)
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return None
    try:
        timestamp = float(text)
        if abs(timestamp) >= 100_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=SAST)
    except (OverflowError, OSError, ValueError):
        return None


def _date_text(value: Any) -> str | None:
    if isinstance(value, datetime):
        effective = value if value.tzinfo else value.replace(tzinfo=SAST)
        return effective.astimezone(SAST).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    epoch = _epoch_datetime(value)
    if epoch is not None:
        return epoch.date().isoformat()
    text = _text(value)
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        effective = parsed if parsed.tzinfo else parsed.replace(tzinfo=SAST)
        return effective.astimezone(SAST).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            return text


def _datetime_text(value: Any) -> str | None:
    if isinstance(value, datetime):
        effective = value if value.tzinfo else value.replace(tzinfo=SAST)
        return effective.astimezone(SAST).isoformat()
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=SAST).isoformat()
    epoch = _epoch_datetime(value)
    if epoch is not None:
        return epoch.isoformat()
    text = _text(value)
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        effective = parsed if parsed.tzinfo else parsed.replace(tzinfo=SAST)
        return effective.astimezone(SAST).isoformat()
    except ValueError:
        return text


def _parse_date(value: Any) -> date | None:
    text = _date_text(value) or ""
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _normalize_reference(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", _text(value).upper())


def reference_tokens(value: Any) -> list[str]:
    """Return explicit long numeric tokens carried by a W8 remark/reference field."""
    text = _text(value)
    values = {_normalize_reference(match.group(0)) for match in _REFERENCE_TOKEN.finditer(text)}
    normalized = _normalize_reference(text)
    if normalized and re.fullmatch(r"\d{6,}", normalized):
        values.add(normalized)
    return sorted(values)


def _normalize_sku(value: Any) -> str:
    return _text(value).casefold()


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""
