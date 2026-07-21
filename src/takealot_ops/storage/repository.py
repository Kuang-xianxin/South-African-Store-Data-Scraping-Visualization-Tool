"""Session-scoped persistence operations with idempotent writes."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from takealot_ops.domain import OfferRecord, SaleRecord
from takealot_ops.storage.models import CollectionRun, OfferCurrent, OfferSnapshot, SaleItem


class Repository:
    """Persist operations data inside a caller-owned SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def begin_run(self, run_type: str) -> str:
        """Stage a collection run and return its identifier without committing."""
        run_id = str(uuid4())
        self._session.add(
            CollectionRun(
                run_id=run_id,
                run_type=run_type,
                started_at=datetime.now(UTC),
                finished_at=None,
                status=None,
                counts=None,
                error=None,
            )
        )
        return run_id

    def upsert_offer_snapshot(self, record: OfferRecord, snapshot_date: date) -> None:
        """Stage the current offer state and its unique daily snapshot."""
        values = _offer_values(record)
        self._upsert_offer_current(values)
        self._upsert_offer_snapshot(values, snapshot_date)

    def upsert_sale(self, record: SaleRecord, raw_payload: Mapping[str, Any]) -> None:
        """Stage the latest state for an order item without stringifying JSON."""
        values = _sale_values(record, raw_payload)
        existing = self._session.get(SaleItem, record.order_item_id)
        if existing is None:
            self._session.add(SaleItem(**values))
            return
        _set_values(existing, values)

    def finish_run(
        self, run_id: str, status: str, counts: Mapping[str, int], error: str | None
    ) -> None:
        """Stage the terminal status for a collection run without committing."""
        run = self._session.get(CollectionRun, run_id)
        if run is None:
            raise ValueError(f"unknown collection run: {run_id}")
        run.status = status
        run.counts = dict(counts)
        run.error = error
        run.finished_at = datetime.now(UTC)

    def _upsert_offer_current(self, values: dict[str, Any]) -> None:
        existing = self._session.get(OfferCurrent, values["offer_id"])
        if existing is None:
            self._session.add(OfferCurrent(**values))
            return
        _set_values(existing, values)

    def _upsert_offer_snapshot(self, values: dict[str, Any], snapshot_date: date) -> None:
        existing = self._session.scalar(
            select(OfferSnapshot).where(
                OfferSnapshot.snapshot_date == snapshot_date,
                OfferSnapshot.offer_id == values["offer_id"],
            )
        )
        snapshot_values = {"snapshot_date": snapshot_date, **values}
        if existing is None:
            self._session.add(OfferSnapshot(**snapshot_values))
            return
        _set_values(existing, snapshot_values)


def _offer_values(record: OfferRecord) -> dict[str, Any]:
    return {
        "offer_id": record.offer_id,
        "tsin_id": record.tsin_id,
        "sku": record.sku,
        "barcode": record.barcode,
        "title": record.title,
        "selling_price": record.selling_price,
        "rrp": record.rrp,
        "benchmark_price": record.benchmark_price,
        "status": record.status,
        "image_url": record.image_url,
        "productline_id": record.productline_id,
        "conversion_percentage_30_days": record.conversion_percentage_30_days,
        "conversion_percentage_previous_30_days": record.conversion_percentage_previous_30_days,
        "page_views_30_days": record.page_views_30_days,
        "quantity_returned_30_days": record.quantity_returned_30_days,
        "total_wishlist": record.total_wishlist,
        "wishlist_30_days": record.wishlist_30_days,
        "listing_quality": record.listing_quality,
        "discount_percentage": record.discount_percentage,
        "updated_at": record.updated_at,
        "captured_at": record.captured_at,
    }


def _sale_values(record: SaleRecord, raw_payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "order_item_id": record.order_item_id,
        "order_id": record.order_id,
        "order_date": record.order_date,
        "sales_day": record.sales_day,
        "sale_status": record.sale_status,
        "offer_id": record.offer_id,
        "tsin_id": record.tsin_id,
        "sku": record.sku,
        "selling_price": record.selling_price,
        "quantity": record.quantity,
        "success_fee": record.success_fee,
        "fulfillment_fee": record.fulfillment_fee,
        "courier_collection_fee": record.courier_collection_fee,
        "total_fees": record.total_fees,
        "stock_transfer_fee": record.stock_transfer_fee,
        "sales_region": record.sales_region,
        "stock_source_region": record.stock_source_region,
        "raw_payload": dict(raw_payload),
    }


def _set_values(instance: object, values: Mapping[str, Any]) -> None:
    for field_name, value in values.items():
        setattr(instance, field_name, value)
