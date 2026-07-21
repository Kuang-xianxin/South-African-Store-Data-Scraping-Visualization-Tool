"""Session-scoped persistence operations with idempotent writes."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from takealot_ops.domain import OfferRecord, SaleRecord
from takealot_ops.storage.models import (
    AnomalyEvent,
    CollectionRun,
    DailyProductMetric,
    DataQualityEvent,
    OfferCurrent,
    OfferSnapshot,
    SaleItem,
)


class Repository:
    """Persist operations data inside a caller-owned SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Expose a caller-controlled atomic boundary without implicit commits."""
        with self._session.begin():
            yield

    def begin_run(self, run_type: str, scope_date: date | None = None) -> str:
        """Stage a collection run and return its identifier without committing."""
        run_id = str(uuid4())
        self._session.add(
            CollectionRun(
                run_id=run_id,
                run_type=run_type,
                scope_date=scope_date,
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

    def prune_offer_snapshot(self, snapshot_date: date, retained_offer_ids: Sequence[str]) -> None:
        """Stage removal of offers absent from a complete current-offer response."""
        snapshot_delete = delete(OfferSnapshot).where(
            OfferSnapshot.snapshot_date == snapshot_date
        )
        current_delete = delete(OfferCurrent)
        if retained_offer_ids:
            snapshot_delete = snapshot_delete.where(
                OfferSnapshot.offer_id.not_in(retained_offer_ids)
            )
            current_delete = current_delete.where(OfferCurrent.offer_id.not_in(retained_offer_ids))
        self._session.execute(snapshot_delete)
        self._session.execute(current_delete)

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

    def list_sales(self, start: date, end: date) -> list[SaleItem]:
        """Return sale items in an inclusive SAST business-date range."""
        return list(
            self._session.scalars(
                select(SaleItem)
                .where(SaleItem.sales_day >= start, SaleItem.sales_day <= end)
                .order_by(SaleItem.sales_day, SaleItem.order_item_id)
            )
        )

    def list_offer_snapshots(self, start: date, end: date) -> list[OfferSnapshot]:
        """Return offer snapshots in an inclusive snapshot-date range."""
        return list(
            self._session.scalars(
                select(OfferSnapshot)
                .where(
                    OfferSnapshot.snapshot_date >= start,
                    OfferSnapshot.snapshot_date <= end,
                )
                .order_by(OfferSnapshot.snapshot_date, OfferSnapshot.offer_id)
            )
        )

    def list_offer_snapshots_through(self, as_of: date) -> list[OfferSnapshot]:
        """Return every offer snapshot known through an inclusive date."""
        return list(
            self._session.scalars(
                select(OfferSnapshot)
                .where(OfferSnapshot.snapshot_date <= as_of)
                .order_by(OfferSnapshot.snapshot_date, OfferSnapshot.offer_id)
            )
        )

    def list_successful_offer_scope_dates(self, as_of: date) -> list[date]:
        """Return durable complete Offer batch dates through ``as_of``."""
        values = self._session.scalars(
            select(CollectionRun.scope_date)
            .where(
                CollectionRun.run_type == "offers",
                CollectionRun.status == "success",
                CollectionRun.scope_date.is_not(None),
                CollectionRun.scope_date <= as_of,
            )
            .distinct()
            .order_by(CollectionRun.scope_date)
        ).all()
        return [value for value in values if value is not None]

    def list_offer_current(self) -> list[OfferCurrent]:
        """Return the latest persisted row for every offer."""
        return list(self._session.scalars(select(OfferCurrent).order_by(OfferCurrent.offer_id)))

    def replace_metric_range(
        self,
        start: date,
        end: date,
        *,
        product_metrics: Sequence[Mapping[str, Any]],
        anomalies: Sequence[Mapping[str, Any]],
        quality_events: Sequence[Mapping[str, Any]],
        anomaly_types: Sequence[str],
    ) -> None:
        """Stage a complete replacement of calculated outputs for a date range."""
        self._session.execute(
            delete(DailyProductMetric).where(
                DailyProductMetric.metric_date >= start,
                DailyProductMetric.metric_date <= end,
            )
        )
        self._session.execute(
            delete(AnomalyEvent).where(
                AnomalyEvent.event_date >= start,
                AnomalyEvent.event_date <= end,
                AnomalyEvent.anomaly_type.in_(anomaly_types),
            )
        )
        self._session.execute(
            delete(DataQualityEvent).where(
                DataQualityEvent.event_date >= start,
                DataQualityEvent.event_date <= end,
                DataQualityEvent.event_type == "unknown_sale_status",
            )
        )
        self._session.add_all(DailyProductMetric(**dict(row)) for row in product_metrics)
        self._session.add_all(AnomalyEvent(**dict(row)) for row in anomalies)
        self._session.add_all(DataQualityEvent(**dict(row)) for row in quality_events)

    def list_daily_product_metrics(self, as_of: date) -> list[DailyProductMetric]:
        """Return all calculated product rows through an inclusive date."""
        return list(
            self._session.scalars(
                select(DailyProductMetric)
                .where(DailyProductMetric.metric_date <= as_of)
                .order_by(DailyProductMetric.metric_date, DailyProductMetric.offer_id)
            )
        )

    def list_anomalies(self, as_of: date) -> list[AnomalyEvent]:
        """Return all anomaly events through an inclusive date."""
        return list(
            self._session.scalars(
                select(AnomalyEvent)
                .where(AnomalyEvent.event_date <= as_of)
                .order_by(AnomalyEvent.event_date, AnomalyEvent.offer_id, AnomalyEvent.anomaly_type)
            )
        )

    def list_quality_events(self, as_of: date) -> list[DataQualityEvent]:
        """Return all data-quality events through an inclusive date."""
        return list(
            self._session.scalars(
                select(DataQualityEvent)
                .where(DataQualityEvent.event_date <= as_of)
                .order_by(DataQualityEvent.event_date, DataQualityEvent.event_id)
            )
        )

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
        "captured_at": record.captured_at.astimezone(UTC),
        "total_stock": record.total_stock,
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
