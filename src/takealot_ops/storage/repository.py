"""Session-scoped persistence operations with idempotent writes."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from takealot_ops.domain import OfferRecord, SaleRecord
from takealot_ops.storage.models import (
    AnomalyEvent,
    CollectionRun,
    DailySalesMetricState,
    DailyProductMetric,
    DataQualityEvent,
    OfferCurrent,
    OfferSnapshot,
    SaleItem,
    SalesRevenueRevision,
    StoreOfferBaseline,
    StoreOfferObservation,
)
from takealot_ops.storage.store_context import current_store_code


STORE_DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")


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
        self._insert_store_offer_baseline(values)
        self._insert_store_offer_observation(values)

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
        sales_source: Mapping[str, Any] | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        """Stage a complete replacement of calculated outputs for a date range."""
        self._reconcile_daily_sales_metric_states(
            start,
            end,
            product_metrics,
            sales_source=sales_source,
            observed_at=observed_at,
        )
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

    def _reconcile_daily_sales_metric_states(
        self,
        start: date,
        end: date,
        product_metrics: Sequence[Mapping[str, Any]],
        *,
        sales_source: Mapping[str, Any] | None,
        observed_at: datetime | None,
    ) -> None:
        """Refresh store-day provenance and append audits only when totals changed."""
        detected_at = _aware_utc(observed_at or datetime.now(UTC))
        store_code = current_store_code()
        source = _sales_source_details(
            sales_source,
            start=start,
            end=end,
            detected_at=detected_at,
        )
        source_kind = str(source["kind"])
        source_run_id = str(source.get("run_id") or "").strip() or None
        source_verified_at = _source_verified_at(source)
        new_totals = _daily_sales_totals(product_metrics)
        if not new_totals:
            return

        existing_totals = {
            metric_date: (
                int(ordered_units) if ordered_units is not None else None,
                Decimal(ordered_revenue) if ordered_revenue is not None else None,
            )
            for metric_date, ordered_units, ordered_revenue in self._session.execute(
                select(
                    DailyProductMetric.metric_date,
                    func.sum(DailyProductMetric.ordered_units),
                    func.sum(DailyProductMetric.ordered_revenue),
                )
                .where(
                    DailyProductMetric.store_code == store_code,
                    DailyProductMetric.metric_date >= start,
                    DailyProductMetric.metric_date <= end,
                )
                .group_by(DailyProductMetric.metric_date)
            ).all()
        }
        states = {
            row.metric_date: row
            for row in self._session.scalars(
                select(DailySalesMetricState).where(
                    DailySalesMetricState.store_code == store_code,
                    DailySalesMetricState.metric_date >= start,
                    DailySalesMetricState.metric_date <= end,
                )
            )
        }

        for metric_date, (after_units, after_revenue) in sorted(new_totals.items()):
            state = states.get(metric_date)
            legacy_totals = existing_totals.get(metric_date)
            if state is not None:
                before_units = state.ordered_units
                before_revenue = state.ordered_revenue
                before_source = dict(state.source_details or {})
            elif legacy_totals is not None:
                before_units, before_revenue = legacy_totals
                before_source = {
                    "kind": "legacy_metric_snapshot",
                    "label": "启用修订审计前已发布的 daily_product_metrics 值",
                    "table": "daily_product_metrics",
                    "metric_date": metric_date.isoformat(),
                    "verified_at": None,
                }
            else:
                before_units = None
                before_revenue = None
                before_source = {
                    "kind": "not_previously_published",
                    "label": "此前没有已发布的店铺日销售指标",
                    "metric_date": metric_date.isoformat(),
                    "verified_at": None,
                }

            changed = state is not None or legacy_totals is not None
            changed = changed and (
                before_units != after_units or before_revenue != after_revenue
            )
            if changed:
                revenue_delta = (
                    after_revenue - before_revenue
                    if after_revenue is not None and before_revenue is not None
                    else None
                )
                units_delta = (
                    after_units - before_units
                    if after_units is not None and before_units is not None
                    else None
                )
                self._session.add(
                    SalesRevenueRevision(
                        metric_date=metric_date,
                        change_type=(
                            "backfilled"
                            if before_revenue is None and before_units is None
                            else "corrected"
                        ),
                        before_ordered_units=before_units,
                        after_ordered_units=after_units,
                        before_ordered_revenue=before_revenue,
                        after_ordered_revenue=after_revenue,
                        revenue_delta=revenue_delta,
                        units_delta=units_delta,
                        before_source=before_source,
                        after_source=source,
                        source_run_id=source_run_id,
                        detected_at=detected_at,
                    )
                )
                logging.getLogger("takealot_ops.cli").warning(
                    "sales_revenue_revision store=%s metric_date=%s "
                    "before_revenue=%s after_revenue=%s revenue_delta=%s "
                    "before_units=%s after_units=%s source_kind=%s source_run_id=%s "
                    "source_range=%s..%s",
                    store_code,
                    metric_date,
                    before_revenue,
                    after_revenue,
                    revenue_delta,
                    before_units,
                    after_units,
                    source_kind,
                    source_run_id,
                    source.get("requested_start"),
                    source.get("requested_end"),
                )

            if state is None:
                state = DailySalesMetricState(
                    metric_date=metric_date,
                    ordered_units=after_units,
                    ordered_revenue=after_revenue,
                    source_kind=source_kind,
                    source_run_id=source_run_id,
                    source_details=source,
                    verified_at=source_verified_at,
                    first_published_at=detected_at,
                    updated_at=detected_at,
                    revision_count=1 if changed else 0,
                )
                self._session.add(state)
                states[metric_date] = state
                continue

            state.ordered_units = after_units
            state.ordered_revenue = after_revenue
            state.updated_at = detected_at
            if sales_source is not None or changed:
                state.source_kind = source_kind
                state.source_run_id = source_run_id
                state.source_details = source
                state.verified_at = source_verified_at
            if changed:
                state.revision_count += 1

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

    def _insert_store_offer_baseline(self, values: dict[str, Any]) -> None:
        """Keep the first available Seller API pull for each Beijing display day."""
        productline_id = str(values.get("productline_id") or "").strip()
        if not productline_id:
            return
        captured_at = values["captured_at"]
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=UTC)
        display_date = captured_at.astimezone(STORE_DISPLAY_TIMEZONE).date()
        existing = self._session.scalar(
            select(StoreOfferBaseline.id).where(
                StoreOfferBaseline.display_date == display_date,
                StoreOfferBaseline.offer_id == values["offer_id"],
            )
        )
        if existing is not None:
            return
        self._session.add(
            StoreOfferBaseline(
                display_date=display_date,
                offer_id=values["offer_id"],
                productline_id=productline_id,
                sku=values.get("sku"),
                title=values.get("title"),
                image_url=values.get("image_url"),
                selling_price=values.get("selling_price"),
                status=values.get("status"),
                total_stock=values.get("total_stock"),
                takealot_available_stock=values.get("takealot_available_stock"),
                seller_available_stock=values.get("seller_available_stock"),
                captured_at=captured_at,
            )
        )

    def _insert_store_offer_observation(self, values: dict[str, Any]) -> None:
        """Keep every complete Seller API offer pull as an immutable history point."""
        productline_id = str(values.get("productline_id") or "").strip()
        if not productline_id:
            return
        captured_at = values["captured_at"]
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=UTC)
        display_date = captured_at.astimezone(STORE_DISPLAY_TIMEZONE).date()
        existing = self._session.scalar(
            select(StoreOfferObservation.id).where(
                StoreOfferObservation.captured_at == captured_at,
                StoreOfferObservation.offer_id == values["offer_id"],
            )
        )
        if existing is not None:
            return
        self._session.add(
            StoreOfferObservation(
                display_date=display_date,
                offer_id=values["offer_id"],
                productline_id=productline_id,
                sku=values.get("sku"),
                title=values.get("title"),
                image_url=values.get("image_url"),
                selling_price=values.get("selling_price"),
                status=values.get("status"),
                total_stock=values.get("total_stock"),
                takealot_available_stock=values.get("takealot_available_stock"),
                seller_available_stock=values.get("seller_available_stock"),
                captured_at=captured_at,
            )
        )


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
        "created_at": (
            record.created_at.astimezone(UTC) if record.created_at is not None else None
        ),
        "updated_at": record.updated_at,
        "captured_at": record.captured_at.astimezone(UTC),
        "total_stock": record.total_stock,
        "takealot_available_stock": record.takealot_available_stock,
        "seller_available_stock": record.seller_available_stock,
        "takealot_stock_in_receiving": record.takealot_stock_in_receiving,
        "takealot_stock_on_way": record.takealot_stock_on_way,
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


def _daily_sales_totals(
    rows: Sequence[Mapping[str, Any]],
) -> dict[date, tuple[int | None, Decimal | None]]:
    totals: dict[date, dict[str, Any]] = {}
    for row in rows:
        metric_date = row.get("metric_date")
        if not isinstance(metric_date, date):
            continue
        total = totals.setdefault(
            metric_date,
            {
                "units": 0,
                "revenue": Decimal("0"),
                "has_units": False,
                "has_revenue": False,
            },
        )
        units = row.get("ordered_units")
        if units is not None:
            total["units"] += int(units)
            total["has_units"] = True
        revenue = row.get("ordered_revenue")
        if revenue is not None:
            total["revenue"] += Decimal(str(revenue))
            total["has_revenue"] = True
    return {
        metric_date: (
            int(total["units"]) if total["has_units"] else None,
            Decimal(total["revenue"]) if total["has_revenue"] else None,
        )
        for metric_date, total in totals.items()
    }


def _sales_source_details(
    source: Mapping[str, Any] | None,
    *,
    start: date,
    end: date,
    detected_at: datetime,
) -> dict[str, Any]:
    details = {
        str(key): _json_value(value)
        for key, value in (source or {}).items()
    }
    if source is None:
        details.setdefault("kind", "local_metric_rebuild")
        details.setdefault("label", "本地 sale_items 指标重建（未绑定新的上游拉取）")
        details.setdefault("table", "sale_items")
    else:
        details.setdefault("kind", "takealot_sales_api")
        details.setdefault("label", "Takealot Seller Sales API /sales 成功批次")
        details.setdefault("endpoint", "/sales")
    details.setdefault("requested_start", start.isoformat())
    details.setdefault("requested_end", end.isoformat())
    details.setdefault("recorded_at", detected_at.isoformat())
    details.setdefault(
        "verified_at",
        details.get("collected_at")
        if details.get("kind") == "takealot_sales_api"
        else None,
    )
    return details


def _source_verified_at(source: Mapping[str, Any]) -> datetime | None:
    raw_value = source.get("verified_at") or source.get("collected_at")
    if not raw_value:
        return None
    if isinstance(raw_value, datetime):
        return _aware_utc(raw_value)
    try:
        return _aware_utc(datetime.fromisoformat(str(raw_value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _aware_utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    return value
