from __future__ import annotations

from datetime import date
from typing import Any

from takealot_ops.erp.return_removal import (
    attach_removal_lifecycles,
    collect_removal_order_snapshot,
    project_removal_orders,
    summarize_removal_lifecycles,
)


class _RemovalClient:
    def __init__(self) -> None:
        self.list_calls: list[tuple[str, int, int]] = []
        self.item_calls: list[tuple[str, str, int, int]] = []

    def removal_orders(
        self,
        token: str,
        stage: str,
        *,
        page_number: int,
        page_size: int,
    ) -> dict[str, Any]:
        assert token == "memory-token"
        self.list_calls.append((stage, page_number, page_size))
        if stage == "submitted":
            order = {
                "removal_order_id": 77,
                "instruction_id": 186192254,
                "reference": "RO-29902559-2026-08-29-JHB",
                "order_type": {"id": 1, "name": "Removal Order"},
                "status_id": 5,
                "date_submitted": 1787954400,
                "warehouse_id": "JHB",
                "total_weight_grams": 147120,
                "total_quantity_to_remove": 6,
                "total_handling_fee_cents": 8970,
                "pickup_address": {"line_1": "must not persist"},
            }
        elif stage == "pickup_ready":
            order = {
                    "removal_order_id": 88,
                    "reference": "12345678",
                    "order_type": {"id": 3, "name": "Returns Removal Order"},
                    "status_id": 8,
                    "disposal_date": "2026-08-27",
                    "days_until_expiry": 2,
                    "flags": {"expired": False},
                    "has_booking": True,
                    "pickup_date_start": "2026-08-25T08:00:00+02:00",
                    "pickup_date_end": "2026-08-25T12:00:00+02:00",
                    "total_quantity_to_remove": 1,
                    "total_quantity_prepared": 1,
                    "total_quantity_removed": 0,
                    "pickup_address": {"line_1": "must not persist"},
            }
        else:
            order = {
                "removal_order_id": 99,
                "reference": "TRO-99-CPT",
                "order_type": {"id": 2, "name": "Takealot Removal Order"},
                "status_id": 10,
                "date_closed": "2026-08-24",
                "number_of_boxes": 2,
                "total_offers": 1,
                "total_quantity_to_remove": 3,
                "total_quantity_prepared": 3,
                "total_quantity_removed": 3,
                "total_handling_fee_cents": 4500,
            }
        return {"results": [order], "total": 1}

    def removal_order_items(
        self,
        token: str,
        stage: str,
        removal_order_id: str,
        *,
        page_number: int,
        page_size: int,
    ) -> dict[str, Any]:
        assert token == "memory-token"
        self.item_calls.append((stage, removal_order_id, page_number, page_size))
        if removal_order_id == "88":
            item = {
                "removal_order_id": 88,
                "removal_order_item_id": "item-88",
                "offer": {
                    "offer_id": "offer-1",
                    "sku": "SKU-1",
                    "tsin": {
                        "tsin_id": 501,
                        "title": "Ready return product",
                        "image_url": "https://example.invalid/ready.jpg",
                    },
                },
                "quantity_to_remove": 1,
                "quantity_prepared": 1,
                "quantity_removed": 0,
                "handling_fee_cents": 1200,
                "return_informations": [
                    {
                        "id": 700,
                        "seller_return_id": "SR-1",
                        "rrn": "RRN-1",
                        "has_item_mismatch": False,
                        "created_at": 1787608800,
                    }
                ],
            }
        elif removal_order_id == "77":
            item = {
                "removal_order_id": 77,
                "removal_order_item_id": "item-77",
                "offer": {
                    "offer_id": "offer-submitted",
                    "sku": "SKU-SUBMITTED",
                    "tsin": {"tsin_id": 502, "title": "Submitted product"},
                },
                "quantity_to_remove": 6,
                "quantity_prepared": 0,
                "quantity_removed": 0,
            }
        else:
            item = {
                "removal_order_id": 99,
                "removal_order_item_id": "item-99",
                "offer": {
                    "offer_id": "offer-closed",
                    "sku": "SKU-CLOSED",
                    "tsin": {"tsin_id": 503, "title": "Closed product"},
                },
                "quantity_to_remove": 3,
                "quantity_prepared": 3,
                "quantity_removed": 3,
                "handling_fee_cents": 4500,
            }
        return {
            "results": [
                item
            ]
        }


def test_collect_removal_snapshot_keeps_all_types_fields_and_sanitizes_pii() -> None:
    client = _RemovalClient()

    snapshot = collect_removal_order_snapshot(client, "memory-token")

    assert client.list_calls == [
        ("submitted", 1, 100),
        ("pickup_ready", 1, 100),
        ("closed", 1, 100),
    ]
    assert client.item_calls == [
        ("submitted", "77", 1, 100),
        ("pickup_ready", "88", 1, 100),
        ("closed", "99", 1, 100),
    ]
    assert snapshot["order_type_filter"] == "All"
    assert snapshot["order_type_ids"] == [1, 2, 3]
    assert snapshot["counts"] == {"submitted": 1, "pickup_ready": 1, "closed": 1}
    submitted = snapshot["orders"][0]
    assert submitted["reference"] == "RO-29902559-2026-08-29-JHB"
    assert submitted["order_type"] == "Removal Order"
    assert submitted["status"] == "Submitted"
    assert submitted["date_submitted"] == "2026-08-29"
    assert submitted["total_weight_grams"] == 147120
    assert submitted["total_handling_fee_cents"] == 8970
    assert "pickup_address" not in submitted
    ready = snapshot["orders"][1]
    assert ready["items"][0]["seller_return_ids"] == ["SR-1"]
    assert ready["items"][0]["product_title"] == "Ready return product"
    assert ready["items"][0]["tsin_id"] == "501"
    assert ready["items"][0]["return_informations"][0]["id"] == "700"
    closed = snapshot["orders"][2]
    assert closed["number_of_boxes"] == 2
    assert closed["quantity_collected"] == 3


def test_full_po_projection_keeps_unlinked_orders_and_exact_w8_disposition() -> None:
    snapshot = collect_removal_order_snapshot(_RemovalClient(), "memory-token")
    w8_orders = [
        {
            "order_no": "RB-CLOSED",
            "status": "已入库",
            "po_references": ["TRO-99-CPT"],
            "items": [
                {
                    "platform_sku": "SKU-CLOSED",
                    "returned_quantity": 3,
                    "inbound_quantity": 3,
                    "pending_shelf_quantity": 0,
                    "total_shelf_quantity": 2,
                    "total_defective_quantity": 1,
                    "inbound_date": "2026-08-25 09:00:00",
                }
            ],
        }
    ]

    projected = project_removal_orders(
        "current",
        "Current Store",
        {"payload": snapshot, "fetched_at": "2026-08-25T10:00:00+08:00"},
        w8_return_orders=w8_orders,
        today=date(2026, 8, 25),
    )

    assert [order["stage"] for order in projected] == [
        "submitted",
        "pickup_ready",
        "closed",
    ]
    submitted = projected[0]
    assert submitted["reference"] == "RO-29902559-2026-08-29-JHB"
    assert submitted["can_collect"] is False
    assert submitted["items"][0]["w8"]["match_status"] == "unlinked"
    closed = projected[2]
    assert closed["collection_status"] == "fully_collected"
    assert closed["items"][0]["w8"]["match_status"] == "linked"
    assert closed["items"][0]["w8"]["inbound_quantity"] == 3
    assert closed["items"][0]["w8"]["disposition"] == "mixed"
    assert closed["w8_summary"] == {
        "item_count": 1,
        "matched_item_count": 1,
        "received_item_count": 1,
        "awaiting_receipt_item_count": 0,
        "unresolved_item_count": 0,
        "pending_shelf_units": 0,
        "shelved_units": 2,
        "defective_units": 1,
    }


def test_lifecycle_requires_unique_return_id_and_po_plus_sku_for_w8() -> None:
    snapshot = _RemovalClient()
    portal = collect_removal_order_snapshot(snapshot, "memory-token")
    rows = [
        {
            "seller_return_id": "SR-1",
            "return_reference_number": "RRN-1",
            "sku": "SKU-1",
            "company_sku": "COMPANY-1",
            "outcome_statuses": ["removal_order"],
        },
        {
            "seller_return_id": "SR-PENDING",
            "return_reference_number": "RRN-PENDING",
            "sku": "SKU-2",
            "company_sku": "COMPANY-2",
            "outcome_statuses": ["pending_removal_order"],
        },
    ]
    w8_orders = [
        {
            "order_no": "RB-1",
            "status": "已入库",
            "po_references": ["12345678"],
            "items": [
                {
                    "platform_sku": "SKU-1",
                    "company_sku": "COMPANY-1",
                    "returned_quantity": 1,
                    "inbound_quantity": 1,
                    "pending_shelf_quantity": 0,
                    "total_shelf_quantity": 0,
                    "total_defective_quantity": 1,
                    "inbound_date": "2026-08-25 09:00:00",
                }
            ],
        }
    ]

    attached = attach_removal_lifecycles(
        rows,
        removal_snapshot=portal,
        w8_return_orders=w8_orders,
        today=date(2026, 8, 25),
    )

    linked = attached[0]["removal_lifecycle"]
    assert linked["linked"] is True
    assert linked["link_basis"] == "seller_return_id"
    assert linked["stage"] == "pickup_ready"
    assert linked["expiry_status"] == "expiring"
    assert linked["can_collect"] is True
    assert linked["has_booking"] is True
    assert linked["w8"]["match_status"] == "linked"
    assert linked["w8"]["received"] is True
    assert linked["w8"]["disposition"] == "defective"
    assert linked["w8"]["defective_quantity"] == 1

    pending = attached[1]["removal_lifecycle"]
    assert pending["stage"] == "pending_creation"
    assert pending["po_reference"] is None
    assert pending["can_collect"] is False

    summary = summarize_removal_lifecycles(attached)
    assert summary["pending_creation_count"] == 1
    assert summary["linked_po_count"] == 1
    assert summary["collectable_count"] == 1
    assert summary["w8_received_count"] == 1
    assert summary["w8_defective_units"] == 1

    duplicated_line = summarize_removal_lifecycles([attached[0], attached[0]])
    assert duplicated_line["w8_received_count"] == 1
    assert duplicated_line["w8_defective_units"] == 1


def test_w8_forecast_is_not_treated_as_received_and_sku_mismatch_does_not_link() -> None:
    portal = collect_removal_order_snapshot(_RemovalClient(), "memory-token")
    rows = [
        {
            "seller_return_id": "SR-1",
            "return_reference_number": "RRN-1",
            "sku": "SKU-1",
            "company_sku": "COMPANY-1",
            "outcome_statuses": ["removal_order"],
        }
    ]
    mismatch = [
        {
            "order_no": "RB-1",
            "po_references": ["12345678"],
            "items": [
                {
                    "platform_sku": "DIFFERENT-SKU",
                    "company_sku": "DIFFERENT-COMPANY",
                    "returned_quantity": 3,
                    "pending_shelf_quantity": 3,
                }
            ],
        }
    ]
    lifecycle = attach_removal_lifecycles(
        rows,
        removal_snapshot=portal,
        w8_return_orders=mismatch,
        today=date(2026, 8, 25),
    )[0]["removal_lifecycle"]
    assert lifecycle["w8"]["match_status"] == "unlinked"
    assert lifecycle["w8"]["received"] is None

    forecast_only = [
        {
            "order_no": "RB-2",
            "po_references": ["12345678"],
            "items": [
                {
                    "platform_sku": "SKU-1",
                    "returned_quantity": 3,
                    "pending_shelf_quantity": 3,
                    "inbound_quantity": 0,
                }
            ],
        }
    ]
    lifecycle = attach_removal_lifecycles(
        rows,
        removal_snapshot=portal,
        w8_return_orders=forecast_only,
        today=date(2026, 8, 25),
    )[0]["removal_lifecycle"]
    assert lifecycle["w8"]["match_status"] == "linked"
    assert lifecycle["w8"]["received"] is False
    assert lifecycle["w8"]["pending_shelf_quantity"] == 0
    assert lifecycle["w8"]["disposition"] == "awaiting_receipt"
