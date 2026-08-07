"""Authenticated FastAPI application for the unified local ERP."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, Literal, NoReturn
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.middleware.gzip import GZipMiddleware

from takealot_ops.competitors.api import (
    CompetitorNetworkError,
    CompetitorPublicClient,
    extract_plid,
)
from takealot_ops.competitors.batch import (
    CollectionBatchBusyError,
    CollectionBatchRegistry,
    CollectionRequestCoordinator,
    configure_collection_logger,
)
from takealot_ops.competitors.service import (
    CompetitorCollectionResult,
    CompetitorCollector,
    CompetitorDataset,
    CompetitorDiscoveredTarget,
    load_competitor_dataset,
    load_competitor_link_health,
)
from takealot_ops.dashboard.refresh import run_dashboard_refresh
from takealot_ops.erp.auth import (
    SESSION_COOKIE,
    SESSION_LIFETIME,
    AuthConflictError,
    AuthInputError,
    AuthManager,
    IssuedSession,
    StoreIdentity,
    UserIdentity,
)
from takealot_ops.erp.coordination import RefreshBusyError, RefreshCoordinator
from takealot_ops.erp.daily_report import (
    DailyReportConflictError,
    DailyReportInputError,
    confirm_entry,
    confirm_ready_entries,
    daily_report_payload,
    delete_operator_note,
    dismiss_stock_alert,
    eliminate_stock_alert,
    export_operations_workbook,
    operations_business_date,
    period_end_traffic_series,
    reminder_payload,
    revert_confirmation,
    reopen_stock_alert,
    save_manual_candidate,
    save_operator_note,
    unresolved_locations,
    update_operator_note,
)
from takealot_ops.erp.daily_report_live import daily_report_event_stream
from takealot_ops.erp.keyword_traffic import (
    build_keyword_product_detail,
    build_keyword_product_list,
)
from takealot_ops.competitors.own_store import (
    ConnectedStoreOffer,
    connected_store_plids,
    load_connected_store_offers,
)
from takealot_ops.erp.permissions import (
    COMPETITORS_COLLECT,
    COMPETITORS_VIEW,
    DAILY_REPORT_EXPORT,
    DAILY_REPORT_MANAGE,
    DAILY_REPORT_VIEW,
    LOGISTICS_MANAGE,
    NFT102_MANAGE,
    REFRESH_RUN,
    REPORTS_GENERATE,
    REPORTS_VIEW,
    SEARCH_RANKING_RUN,
    STORE_VIEW,
    USERS_MANAGE,
    permissions_from_storage,
)
from takealot_ops.erp.product_images import (
    DEFAULT_MAX_DIMENSION,
    ProductImageInputError,
    ProductImageUnavailableError,
    ProductThumbnailCache,
)
from takealot_ops.erp.service import (
    build_product_detail_payload,
    build_products_payload,
    build_quadrant_payload,
    build_risk_payload,
    build_summary_payload,
    create_read_only_erp_engine,
    frame_records,
    load_erp_dataset,
    sqlite_database_path,
)
from takealot_ops.logistics import LogisticsLinkError, LogisticsOverviewService
from takealot_ops.logistics.snapshots import load_provider_snapshot
from takealot_ops.platform_warehouse import (
    PlatformWarehouseConflictError,
    PlatformWarehouseInputError,
    PlatformWarehouseNotFoundError,
    PlatformWarehouseService,
    PortalAuthenticationError,
    PortalDisabledError,
    PortalError,
)
from takealot_ops.nft102_portal import (
    generate_nft102_from_baseline,
    inspect_nft102_upload,
    persist_nft102_baseline,
)
from takealot_ops.reporting import generate_daily_reports
from takealot_ops.scheduler import verify_database_integrity
from takealot_ops.search_ranking import (
    SearchRankingConfigurationError,
    SearchRankingInputError,
    SearchRankingProviderError,
    SearchRankingService,
)
from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.migrations import create_engine_for_settings, create_schema
from takealot_ops.storage.models import (
    CollectionRun,
    CompetitorPersonalWatchlist,
    CompetitorSnapshot,
    CompetitorTarget,
    CompetitorTargetAudit,
    DailyProductMetric,
    ErpUser,
    ErpUserStore,
    OfferCurrent,
)
from takealot_ops.storage.store_context import (
    STORE_CODE_HEADER,
    current_store_code,
    store_scope,
)


class CollectCompetitorRequest(BaseModel):
    """One explicit competitor collection request."""

    url: str = Field(min_length=1)
    with_stock_probe: bool = True
    visible_browser: bool = False
    batch_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    request_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    client_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    item_index: int | None = Field(default=None, ge=0)
    total_items: int | None = Field(default=None, ge=1)
    retry_kind: str | None = Field(default=None, pattern=r"^(stock|automatic)$")
    retry_attempt: int | None = Field(default=None, ge=1, le=10)


class CompetitorBatchEventRequest(BaseModel):
    """One operator-page batch lifecycle event written to the server log."""

    batch_id: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    client_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    event: str = Field(
        min_length=1,
        max_length=40,
        pattern=r"^[a-z_]+$",
    )
    completed: int = Field(ge=0)
    total: int = Field(ge=0)
    pending: int = Field(ge=0)
    succeeded: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    terminal: int = Field(default=0, ge=0)
    with_stock_probe: bool = True
    visible_browser: bool = False
    reason: str = Field(default="", max_length=500)


class CompetitorBatchOptionsRequest(BaseModel):
    """One same-account update to a running batch's safe options."""

    batch_id: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    visible_browser: bool


class CompetitorBatchTakeoverRequest(BaseModel):
    """Request control transfer to another page of the same account."""

    batch_id: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    client_id: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class CompetitorTargetRequest(BaseModel):
    """One persisted Takealot competitor product URL."""

    url: str = Field(min_length=1, max_length=2000)


class CompetitorTargetPriorityRequest(BaseModel):
    """Describe why an operator is adding one priority collection attempt."""

    source: Literal["manual", "manual_retry"] = "manual"


class ExportRequest(BaseModel):
    """One explicit report export request."""

    as_of: date


class LogisticsLinkConfirmRequest(BaseModel):
    w8_order_no: str = Field(min_length=1, max_length=80)
    takealot_shipment_id: int = Field(ge=1)


class LogisticsLinkRevokeRequest(BaseModel):
    note: str = Field(min_length=1, max_length=500)


class PlatformWarehouseDraftLineRequest(BaseModel):
    offer_id: str = Field(min_length=1, max_length=100)
    cpt_quantity: int = Field(default=0, ge=0, le=1_000_000)
    jhb_quantity: int = Field(default=0, ge=0, le=1_000_000)
    dbn_quantity: int = Field(default=0, ge=0, le=1_000_000)


class PlatformWarehouseDirectCreateRequest(BaseModel):
    lines: list[PlatformWarehouseDraftLineRequest] = Field(min_length=1, max_length=1000)
    note: str = Field(default="", max_length=2000)
    client_request_id: str = Field(min_length=36, max_length=36)


class PlatformWarehouseConfirmPoRequest(BaseModel):
    po_number: str = Field(min_length=1, max_length=80)
    platform_shipment_id: int | None = Field(default=None, ge=1)
    note: str = Field(default="", max_length=2000)


class PlatformWarehouseConfirmShippedRequest(BaseModel):
    tracking_reference: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=2000)


class PlatformWarehouseArchiveRequest(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class PlatformWarehousePortalOtpRequest(BaseModel):
    otp: str = Field(min_length=1, max_length=12)


class PlatformWarehousePrepareActionRequest(BaseModel):
    action: Literal["confirm_po", "confirm_shipped", "archive"]


class PlatformWarehouseExecuteActionRequest(PlatformWarehousePrepareActionRequest):
    approval_token: str = Field(min_length=32, max_length=128)
    confirmation_text: str = Field(min_length=1, max_length=80)
    tracking_reference: str = Field(default="", max_length=200)
    my_soh_decrease_warehouse_id: int | None = Field(default=None, ge=1)


def _default_operations_business_date() -> date:
    return operations_business_date(datetime.now(UTC))


class DailyReportManualRequest(BaseModel):
    page_views_30_days: int | None = Field(default=None, ge=0)
    ordered_units: int | None = Field(default=None, ge=0)
    platform_stock: int | None = Field(default=None, ge=0)
    reason: str
    note: str = Field(default="", max_length=2000)


class DailyReportConfirmRequest(BaseModel):
    source: str
    note: str = Field(min_length=1, max_length=2000)


class DailyReportRevertRequest(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class DailyReportNoteRequest(BaseModel):
    note: str = Field(min_length=1, max_length=2000)
    issue_type: str = "general"


class DailyReportDeleteNoteRequest(BaseModel):
    note: str = Field(default="", max_length=2000)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class BootstrapRequest(LoginRequest):
    display_name: str = Field(default="", max_length=100)


class StoreCreateRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=100)


class StoreUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    active: bool | None = None


class UserCreateRequest(BootstrapRequest):
    role: str
    permissions: list[str] | None = None
    all_stores: bool | None = None
    store_ids: list[int] | None = None


class UserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    password: str | None = Field(default=None, max_length=128)
    role: str | None = None
    permissions: list[str] | None = None
    all_stores: bool | None = None
    store_ids: list[int] | None = None
    active: bool | None = None


class _LoginLimiter:
    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def allowed(self, source: str) -> bool:
        now = time.monotonic()
        with self._lock:
            recent = [stamp for stamp in self._failures[source] if now - stamp < 300]
            self._failures[source] = recent
            return len(recent) < 5

    def failure(self, source: str) -> None:
        with self._lock:
            self._failures[source].append(time.monotonic())

    def success(self, source: str) -> None:
        with self._lock:
            self._failures.pop(source, None)


class _CompetitorPublicClientLease:
    def __init__(self, client: CompetitorPublicClient) -> None:
        self.client = client
        self.reusable = True

    def invalidate(self) -> None:
        self.reusable = False


def _competitor_link_cooldown_seconds(
    min_seconds: float,
    max_seconds: float,
) -> float:
    return random.uniform(min_seconds, max_seconds)


async def _sleep_competitor_link_cooldown(seconds: float) -> None:
    await asyncio.sleep(seconds)


class _SharedCompetitorPublicClient:
    """Serialize and bound reuse of the hidden public-data browser."""

    def __init__(
        self,
        *,
        max_uses: int = 25,
        min_link_delay_seconds: float = 5.0,
        max_link_delay_seconds: float = 10.0,
    ) -> None:
        if max_uses < 1:
            raise ValueError("max_uses must be at least 1")
        if min_link_delay_seconds < 0:
            raise ValueError("min_link_delay_seconds cannot be negative")
        if max_link_delay_seconds < min_link_delay_seconds:
            raise ValueError(
                "max_link_delay_seconds must be at least min_link_delay_seconds"
            )
        self._max_uses = max_uses
        self._min_link_delay_seconds = min_link_delay_seconds
        self._max_link_delay_seconds = max_link_delay_seconds
        self._uses = 0
        self._has_previous_lease = False
        self._client: CompetitorPublicClient | None = None
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def lease(
        self,
        *,
        wait_callback: Callable[[float], None] | None = None,
    ) -> AsyncIterator[_CompetitorPublicClientLease]:
        async with self._lock:
            if self._has_previous_lease:
                delay_seconds = _competitor_link_cooldown_seconds(
                    self._min_link_delay_seconds,
                    self._max_link_delay_seconds,
                )
                if wait_callback is not None:
                    wait_callback(delay_seconds)
                await _sleep_competitor_link_cooldown(delay_seconds)
            if self._client is None or self._uses >= self._max_uses:
                await self._close_current()
                self._client = CompetitorPublicClient()
            client = self._client
            lease = _CompetitorPublicClientLease(client)
            try:
                yield lease
            except BaseException:
                lease.invalidate()
                raise
            finally:
                self._has_previous_lease = True
                self._uses += 1
                if not lease.reusable:
                    await self._close_current()

    async def close(self) -> None:
        async with self._lock:
            await self._close_current()

    async def _close_current(self) -> None:
        client = self._client
        self._client = None
        self._uses = 0
        if client is None:
            return
        try:
            await client.close()
        except Exception:
            logging.getLogger(__name__).warning(
                "Failed to close the reusable competitor public browser",
                exc_info=True,
            )


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _empty_store_inventory() -> dict[str, Any]:
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


def _store_inventory_snapshot(engine: Engine) -> dict[str, Any]:
    """Summarize the current store's local offer inventory snapshot."""
    with Session(engine) as session:
        row = session.execute(
            select(
                func.count(OfferCurrent.offer_id).label("offer_count"),
                func.max(OfferCurrent.captured_at).label("captured_at"),
                func.sum(OfferCurrent.takealot_available_stock).label(
                    "platform_available_stock"
                ),
                func.count(OfferCurrent.takealot_available_stock).label(
                    "platform_available_coverage"
                ),
                func.sum(OfferCurrent.takealot_stock_on_way).label(
                    "platform_stock_on_way"
                ),
                func.count(OfferCurrent.takealot_stock_on_way).label(
                    "platform_stock_on_way_coverage"
                ),
                func.sum(OfferCurrent.takealot_stock_in_receiving).label(
                    "platform_stock_in_receiving"
                ),
                func.count(OfferCurrent.takealot_stock_in_receiving).label(
                    "platform_stock_in_receiving_coverage"
                ),
            )
        ).mappings().one()
    captured_at = row["captured_at"]
    return {
        "captured_at": captured_at.isoformat() if captured_at is not None else None,
        "offer_count": int(row["offer_count"] or 0),
        "platform_available_stock": _optional_int(row["platform_available_stock"]),
        "platform_available_coverage": int(
            row["platform_available_coverage"] or 0
        ),
        "platform_stock_on_way": _optional_int(row["platform_stock_on_way"]),
        "platform_stock_on_way_coverage": int(
            row["platform_stock_on_way_coverage"] or 0
        ),
        "platform_stock_in_receiving": _optional_int(
            row["platform_stock_in_receiving"]
        ),
        "platform_stock_in_receiving_coverage": int(
            row["platform_stock_in_receiving_coverage"] or 0
        ),
    }


def _responsible_users_by_store(
    engine: Engine,
    stores: Sequence[StoreIdentity],
) -> dict[str, list[dict[str, Any]]]:
    """Return active non-admin users who can view each requested store."""
    result: dict[str, list[dict[str, Any]]] = {
        store.code: [] for store in stores
    }
    if not stores:
        return result
    store_by_id = {store.id: store.code for store in stores}
    with Session(engine) as session:
        users = session.scalars(
            select(ErpUser)
            .where(
                ErpUser.active.is_(True),
                ErpUser.role != "admin",
            )
            .order_by(ErpUser.display_name, ErpUser.id)
        ).all()
        assignments = session.execute(
            select(ErpUserStore.user_id, ErpUserStore.store_id).where(
                ErpUserStore.store_id.in_(tuple(store_by_id))
            )
        ).all()
    assigned_by_user: dict[int, set[int]] = defaultdict(set)
    for user_id, store_id in assignments:
        assigned_by_user[int(user_id)].add(int(store_id))
    for user in users:
        try:
            permissions = permissions_from_storage(
                user.role,
                user.permissions_json,
            )
        except ValueError:
            continue
        if STORE_VIEW not in permissions:
            continue
        user_store_ids = (
            set(store_by_id)
            if user.store_access_all
            else assigned_by_user.get(user.id, set())
        )
        for store_id in user_store_ids:
            store_code = store_by_id.get(store_id)
            if store_code is None:
                continue
            result[store_code].append(
                {
                    "user_id": user.id,
                    "display_name": user.display_name.strip() or user.username,
                    "role": user.role,
                }
            )
    role_priority = {"operator": 0, "viewer": 1, "selection": 2}
    for users_for_store in result.values():
        users_for_store.sort(
            key=lambda item: (
                role_priority.get(str(item["role"]), 9),
                str(item["display_name"]).casefold(),
                int(item["user_id"]),
            )
        )
    return result


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


def _shared_overseas_inventory(
    engine: Engine,
    stores: Sequence[StoreIdentity],
) -> dict[str, Any]:
    """Read the newest accessible W8 snapshot and count that shared warehouse once."""
    latest: dict[str, Any] | None = None
    for store in stores:
        with store_scope(store.code):
            snapshot = load_provider_snapshot(engine, "w8")
        if snapshot is None:
            continue
        if latest is None or str(snapshot["fetched_at"]) > str(latest["fetched_at"]):
            latest = snapshot
    if latest is None:
        return _empty_overseas_inventory()
    payload = latest.get("payload")
    if not isinstance(payload, Mapping) or not payload.get("connected"):
        return _empty_overseas_inventory()
    summary = payload.get("summary")
    if not isinstance(summary, Mapping):
        return _empty_overseas_inventory()
    warehouse = payload.get("warehouse")
    warehouse_name = None
    if isinstance(warehouse, Mapping):
        warehouse_name = str(
            warehouse.get("name") or warehouse.get("code") or ""
        ).strip() or None
    return {
        "snapshot_at": str(latest["fetched_at"]),
        "warehouse_name": warehouse_name,
        "stock_total": _optional_int(summary.get("stock_total")),
        "usable_stock": _optional_int(summary.get("usable_stock")),
        "locked_stock": _optional_int(summary.get("locked_stock")),
        "outbound_allocated": _optional_int(summary.get("outbound_allocated")),
        "transit_stock": _optional_int(summary.get("transit_stock")),
        "defective_stock": _optional_int(summary.get("defective_stock")),
        "shared_across_stores": True,
    }


def _aggregate_platform_inventory(
    items: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    fields = (
        ("platform_available_stock", "platform_available_coverage"),
        ("platform_stock_on_way", "platform_stock_on_way_coverage"),
        ("platform_stock_in_receiving", "platform_stock_in_receiving_coverage"),
    )
    result: dict[str, Any] = {
        "captured_at": None,
        "store_count": len(items),
        "store_count_with_offers": 0,
        "offer_count": 0,
    }
    captured_values: list[str] = []
    for item in items:
        inventory = item.get("inventory")
        if not isinstance(inventory, Mapping):
            continue
        offer_count = int(inventory.get("offer_count") or 0)
        result["offer_count"] += offer_count
        if offer_count:
            result["store_count_with_offers"] += 1
        captured_at = inventory.get("captured_at")
        if captured_at:
            captured_values.append(str(captured_at))
    result["captured_at"] = max(captured_values) if captured_values else None
    for value_field, coverage_field in fields:
        known_values: list[int] = []
        coverage = 0
        for item in items:
            inventory = item.get("inventory")
            if not isinstance(inventory, Mapping):
                continue
            value = _optional_int(inventory.get(value_field))
            if value is not None:
                known_values.append(value)
            coverage += int(inventory.get(coverage_field) or 0)
        result[value_field] = sum(known_values) if known_values else None
        result[coverage_field] = coverage
    return result


def _store_health(item: Mapping[str, Any]) -> dict[str, Any]:
    """Classify one store from explicit risk and completeness signals only."""
    business_reasons: list[str] = []
    data_reasons: list[str] = []
    latest_metric_date = item.get("latest_metric_date")
    kpis = item.get("kpis")
    if not isinstance(kpis, Mapping):
        kpis = {}
    if latest_metric_date:
        stockouts = _optional_int(kpis.get("stockout_products")) or 0
        if stockouts:
            business_reasons.append(f"缺货商品 {stockouts} 个")
    else:
        data_reasons.append("暂无经营指标日")

    traffic = item.get("latest_traffic_point")
    if not isinstance(traffic, Mapping):
        data_reasons.append("周期末近30天浏览量暂无可用合计")
    else:
        official_value = _optional_int(traffic.get("page_views_30_days_total"))
        reference = traffic.get("reference")
        if official_value is None:
            if isinstance(reference, Mapping):
                data_reasons.append("周期末浏览量使用同日参考")
                missing_products = _optional_int(reference.get("missing_product_count")) or 0
            else:
                data_reasons.append("周期末近30天浏览量暂无可用合计")
                missing_products = 0
        else:
            missing_products = _optional_int(traffic.get("missing_product_count")) or 0
        if missing_products:
            data_reasons.append(f"周期末浏览量缺失 {missing_products} 个商品")

    inventory = item.get("inventory")
    if not isinstance(inventory, Mapping):
        inventory = _empty_store_inventory()
    offer_count = int(inventory.get("offer_count") or 0)
    if not offer_count:
        data_reasons.append("平台库存暂无商品快照")
    else:
        coverage_labels = (
            ("platform_available_coverage", "平台可售库存"),
            ("platform_stock_on_way_coverage", "平台在途库存"),
            ("platform_stock_in_receiving_coverage", "平台收货中库存"),
        )
        for coverage_field, label in coverage_labels:
            coverage = int(inventory.get(coverage_field) or 0)
            if coverage < offer_count:
                data_reasons.append(f"{label}缺失 {offer_count - coverage} 个商品")

    if business_reasons:
        state = "attention"
        label = "需关注"
        priority = 2
    elif data_reasons:
        state = "data_gap"
        label = "数据待补"
        priority = 1
    else:
        state = "healthy"
        label = "当前口径正常"
        priority = 0
    return {
        "state": state,
        "label": label,
        "priority": priority,
        "business_reasons": business_reasons,
        "data_reasons": data_reasons,
    }


def _health_rollup(items: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    result = {"attention": 0, "data_gap": 0, "healthy": 0}
    for item in items:
        health = item.get("health")
        state = health.get("state") if isinstance(health, Mapping) else None
        if state in result:
            result[state] += 1
    return result


def create_app(project_root: Path | None = None) -> FastAPI:
    """Create the unified ERP API and attach its built Vue application."""
    root = (project_root or Path(os.environ.get("TAKEALOT_PROJECT_ROOT", Path.cwd()))).resolve()
    auth = AuthManager(root)
    limiter = _LoginLimiter()
    competitor_logger = configure_collection_logger(root)
    collection_coordinator = CollectionRequestCoordinator[CompetitorCollectionResult]()
    competitor_public_client = _SharedCompetitorPublicClient(max_uses=25)
    database_url = DashboardSettings.from_env(root).database_url
    collection_registry = CollectionBatchRegistry(
        None
        if database_url.startswith("sqlite")
        else root / "logs" / "competitor-batch-queue.json"
    )
    refresh_coordinator = RefreshCoordinator(root)
    product_thumbnails = ProductThumbnailCache(root)
    logistics_overview = LogisticsOverviewService(root)
    platform_warehouse = PlatformWarehouseService(root)
    search_ranking = SearchRankingService(root)
    search_ranking_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await competitor_public_client.close()
            refresh_coordinator.close()
            product_thumbnails.close()
            auth.close()

    app = FastAPI(
        title="Takealot 本地运营 ERP",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    app.state.auth_manager = auth
    app.state.product_thumbnail_cache = product_thumbnails
    app.state.search_ranking_service = search_ranking

    def require_competitor_batch_controller(request: Request) -> None:
        user = request.state.erp_user
        if user.username.casefold() != "kxx":
            raise HTTPException(
                status_code=403,
                detail=(
                    "竞品批次的开始、继续和停止仅限 kxx 账号；"
                    "当前账号仍可新增链接和插队"
                ),
            )

    @app.middleware("http")
    async def enforce_permissions(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        public_paths = {
            "/api/health",
            "/api/auth/status",
            "/api/auth/session",
            "/api/auth/login",
            "/api/auth/bootstrap",
        }
        if not path.startswith("/api/") or path in public_paths:
            return await call_next(request)

        session_token = request.cookies.get(SESSION_COOKIE)
        session = await run_in_threadpool(
            auth.resolve_session,
            session_token,
        )
        if session is None:
            return JSONResponse(status_code=401, content={"detail": "请先登录"})
        request.state.erp_user = session.user
        request.state.erp_session = session

        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf_token = request.headers.get("X-CSRF-Token", "")
            if not csrf_token or csrf_token != session.csrf_token:
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "请求校验失败，请刷新页面后重试"},
                )
                return _renew_session_cookie(
                    response,
                    request,
                    session_token,
                    renewed=session.renewed,
                )
        required_permission = _required_permission(path, request.method)
        if required_permission and not _can_access_required_permission(
            session.user,
            required_permission,
        ):
            response = JSONResponse(
                status_code=403,
                content={
                    "detail": _permission_denied_message(required_permission),
                },
            )
            return _renew_session_cookie(
                response,
                request,
                session_token,
                renewed=session.renewed,
            )
        requested_store_code = (
            request.headers.get(STORE_CODE_HEADER)
            or request.query_params.get("store_code")
            or "current"
        ).strip().casefold()
        accessible_store = next(
            (
                store
                for store in session.user.accessible_stores
                if store.code == requested_store_code
            ),
            None,
        )
        requires_store = _requires_connected_store_access(path)
        if accessible_store is None and requires_store:
            response = JSONResponse(
                status_code=403,
                content={
                    "detail": "当前账号未获授权访问已接入数据的店铺",
                },
            )
            return _renew_session_cookie(
                response,
                request,
                session_token,
                renewed=session.renewed,
            )
        if accessible_store is None and session.user.accessible_stores:
            response = JSONResponse(
                status_code=403,
                content={"detail": "当前账号未获授权访问所选店铺"},
            )
            return _renew_session_cookie(
                response,
                request,
                session_token,
                renewed=session.renewed,
            )
        if requires_store and accessible_store is not None and not accessible_store.data_connected:
            response = JSONResponse(
                status_code=403,
                content={"detail": "所选店铺尚未完成数据接入"},
            )
            return _renew_session_cookie(
                response,
                request,
                session_token,
                renewed=session.renewed,
            )
        request.state.erp_store = accessible_store
        scoped_store_code = accessible_store.code if accessible_store is not None else "current"
        with store_scope(scoped_store_code):
            downstream_response = await call_next(request)
        return _renew_session_cookie(
            downstream_response,
            request,
            session_token,
            renewed=session.renewed,
        )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "application": "takealot-erp"}

    @app.get("/api/auth/status")
    def auth_status(request: Request) -> dict[str, bool]:
        setup_required = auth.user_count() == 0
        return {
            "setup_required": setup_required,
            "bootstrap_allowed": setup_required and _is_loopback_request(request),
        }

    @app.get("/api/auth/session")
    def auth_session(request: Request) -> Response:
        session_token = request.cookies.get(SESSION_COOKIE)
        resolved = auth.resolve_session(session_token)
        if resolved is None:
            raise HTTPException(status_code=401, detail="请先登录")
        response = JSONResponse(
            {
                "user": resolved.user.as_dict(),
                "csrf_token": resolved.csrf_token,
                "expires_at": resolved.expires_at.isoformat(),
            }
        )
        return _renew_session_cookie(
            response,
            request,
            session_token,
            renewed=resolved.renewed,
        )

    @app.post("/api/auth/bootstrap")
    def auth_bootstrap(request: Request, payload: BootstrapRequest) -> Response:
        if not _is_loopback_request(request):
            raise HTTPException(
                status_code=403,
                detail="首个管理员只能在服务器本机通过 127.0.0.1 初始化",
            )
        try:
            issued = auth.bootstrap(
                username=payload.username,
                display_name=payload.display_name,
                password=payload.password,
            )
        except AuthInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except AuthConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _session_response(request, issued)

    @app.post("/api/auth/login")
    def auth_login(request: Request, payload: LoginRequest) -> Response:
        source = request.client.host if request.client else "unknown"
        if not limiter.allowed(source):
            raise HTTPException(status_code=429, detail="登录失败次数过多，请 5 分钟后重试")
        try:
            issued = auth.login(payload.username, payload.password)
        except AuthInputError:
            issued = None
        if issued is None:
            limiter.failure(source)
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        limiter.success(source)
        return _session_response(request, issued)

    @app.post("/api/auth/logout")
    def auth_logout(request: Request) -> Response:
        auth.logout(request.cookies.get(SESSION_COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/auth/users")
    def auth_users() -> dict[str, Any]:
        return {"items": auth.list_users()}

    @app.get("/api/auth/stores")
    def auth_stores() -> dict[str, Any]:
        return {"items": auth.list_stores()}

    @app.post("/api/auth/stores")
    def auth_create_store(payload: StoreCreateRequest) -> dict[str, Any]:
        try:
            store = auth.create_store(
                code=payload.code,
                display_name=payload.display_name,
            )
        except AuthInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except AuthConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"store": store}

    @app.patch("/api/auth/stores/{store_id}")
    def auth_update_store(
        store_id: int,
        payload: StoreUpdateRequest,
    ) -> dict[str, Any]:
        try:
            store = auth.update_store(
                store_id,
                display_name=payload.display_name,
                active=payload.active,
            )
        except AuthInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except AuthConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"store": store}

    @app.post("/api/auth/users")
    def auth_create_user(payload: UserCreateRequest) -> dict[str, Any]:
        try:
            user = auth.create_user(
                username=payload.username,
                display_name=payload.display_name,
                password=payload.password,
                role=payload.role,
                permissions=payload.permissions,
                all_stores=payload.all_stores,
                store_ids=payload.store_ids,
            )
        except AuthInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except AuthConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"user": user}

    @app.patch("/api/auth/users/{user_id}")
    def auth_update_user(user_id: int, payload: UserUpdateRequest) -> dict[str, Any]:
        try:
            user = auth.update_user(
                user_id,
                display_name=payload.display_name,
                password=payload.password,
                role=payload.role,
                permissions=payload.permissions,
                permissions_provided="permissions" in payload.model_fields_set,
                all_stores=payload.all_stores,
                store_ids=payload.store_ids,
                store_ids_provided="store_ids" in payload.model_fields_set,
                active=payload.active,
            )
        except AuthInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except AuthConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"user": user}

    @app.get("/api/erp/freshness")
    def freshness() -> dict[str, str | None]:
        settings = DashboardSettings.from_env(root)
        path = sqlite_database_path(settings.database_url)
        if path is not None and not path.exists():
            return {"last_collection_at": None, "latest_metric_date": None}
        engine = create_read_only_erp_engine(settings.database_url)
        try:
            with Session(engine) as session:
                last_collection = session.scalar(
                    select(func.max(CollectionRun.finished_at)).where(
                        CollectionRun.status == "success",
                        CollectionRun.run_type.in_(("offers", "sales")),
                    )
                )
                latest_metric = session.scalar(select(func.max(DailyProductMetric.metric_date)))
        except SQLAlchemyError:
            return {"last_collection_at": None, "latest_metric_date": None}
        finally:
            engine.dispose()
        return {
            "last_collection_at": (
                last_collection.isoformat() if last_collection is not None else None
            ),
            "latest_metric_date": (
                latest_metric.isoformat() if latest_metric is not None else None
            ),
        }

    @app.get("/api/erp/summary")
    def summary(
        request: Request,
        as_of: date = Query(default_factory=date.today),
    ) -> dict[str, Any]:
        settings = DashboardSettings.from_env(root)
        payload = build_summary_payload(load_erp_dataset(settings, as_of), as_of)
        engine = create_read_only_erp_engine(settings.database_url)
        try:
            store = request.state.erp_store
            try:
                payload["traffic_series"] = period_end_traffic_series(
                    engine,
                    as_of=as_of,
                )
            except SQLAlchemyError:
                payload["traffic_series"] = []
            try:
                payload["operators"] = _responsible_users_by_store(
                    engine,
                    (store,),
                ).get(store.code, [])
            except SQLAlchemyError:
                payload["operators"] = []
        finally:
            engine.dispose()
        return payload

    @app.get("/api/erp/summary/stores")
    def store_summaries(
        request: Request,
        as_of: date = Query(default_factory=date.today),
    ) -> dict[str, Any]:
        """Return a compact comparison for every connected store visible to the user."""
        settings = DashboardSettings.from_env(root)
        stores = tuple(
            store
            for store in request.state.erp_user.accessible_stores
            if store.active and store.data_connected
        )
        items: list[dict[str, Any]] = []
        engine = create_read_only_erp_engine(settings.database_url)
        try:
            try:
                operators_by_store = _responsible_users_by_store(engine, stores)
            except SQLAlchemyError:
                operators_by_store = {store.code: [] for store in stores}
            for store in stores:
                with store_scope(store.code):
                    payload = build_summary_payload(
                        load_erp_dataset(settings, as_of),
                        as_of,
                    )
                    try:
                        traffic_series = period_end_traffic_series(
                            engine,
                            as_of=as_of,
                        )
                    except SQLAlchemyError:
                        traffic_series = []
                    try:
                        inventory = _store_inventory_snapshot(engine)
                    except SQLAlchemyError:
                        inventory = _empty_store_inventory()
                item = {
                    "store_code": store.code,
                    "store_name": store.display_name,
                    "latest_metric_date": payload["latest_metric_date"],
                    "kpis": payload["kpis"],
                    "latest_traffic_point": (
                        traffic_series[-1] if traffic_series else None
                    ),
                    "operators": operators_by_store.get(store.code, []),
                    "inventory": inventory,
                }
                item["health"] = _store_health(item)
                items.append(item)
            items.sort(
                key=lambda item: (
                    -int(item["health"]["priority"]),
                    -int(item["kpis"].get("stockout_products") or 0),
                    str(item["store_name"]).casefold(),
                )
            )
            try:
                overseas_inventory = _shared_overseas_inventory(engine, stores)
            except SQLAlchemyError:
                overseas_inventory = _empty_overseas_inventory()
        finally:
            engine.dispose()
        return {
            "as_of": as_of.isoformat(),
            "store_count": len(items),
            "health_summary": _health_rollup(items),
            "logistics": {
                "overseas_warehouse": overseas_inventory,
                "platform_warehouse": _aggregate_platform_inventory(items),
            },
            "stores": items,
        }

    @app.get("/api/erp/products")
    def products(as_of: date = Query(default_factory=date.today)) -> dict[str, Any]:
        settings = DashboardSettings.from_env(root)
        return build_products_payload(load_erp_dataset(settings, as_of), as_of)

    @app.get("/api/erp/products/{offer_id}")
    def product_detail(
        offer_id: str,
        as_of: date = Query(default_factory=date.today),
    ) -> dict[str, Any]:
        settings = DashboardSettings.from_env(root)
        return build_product_detail_payload(
            load_erp_dataset(settings, as_of),
            as_of,
            offer_id,
        )

    @app.get("/api/erp/keyword-traffic")
    def keyword_traffic_products(
        as_of: date = Query(default_factory=date.today),
    ) -> dict[str, Any]:
        settings = DashboardSettings.from_env(root)
        engine = create_read_only_erp_engine(settings.database_url)
        try:
            with Session(engine) as session:
                return build_keyword_product_list(session, as_of=as_of)
        finally:
            engine.dispose()

    @app.get("/api/erp/keyword-traffic/{offer_id}")
    def keyword_traffic_product_detail(
        offer_id: str,
        as_of: date = Query(default_factory=date.today),
        history_days: int = Query(90, ge=30, le=365),
        comparison_days: int = Query(7, ge=3, le=30),
    ) -> dict[str, Any]:
        settings = DashboardSettings.from_env(root)
        engine = create_read_only_erp_engine(settings.database_url)
        try:
            with Session(engine) as session:
                payload = build_keyword_product_detail(
                    session,
                    offer_id=offer_id,
                    as_of=as_of,
                    history_days=history_days,
                    comparison_days=comparison_days,
                )
        finally:
            engine.dispose()
        if payload is None:
            raise HTTPException(status_code=404, detail="没有找到对应的店铺商品")
        return payload

    @app.get("/api/erp/search-ranking")
    def search_ranking_products(request: Request) -> dict[str, Any]:
        service: SearchRankingService = request.app.state.search_ranking_service
        return service.list_payload()

    @app.get("/api/erp/search-ranking/{offer_id}")
    def search_ranking_product_detail(
        offer_id: str,
        request: Request,
    ) -> dict[str, Any]:
        service: SearchRankingService = request.app.state.search_ranking_service
        payload = service.detail_payload(offer_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="没有找到对应的店铺商品")
        return payload

    @app.post("/api/erp/search-ranking/{offer_id}/analyze")
    async def analyze_search_ranking(
        offer_id: str,
        request: Request,
    ) -> dict[str, Any]:
        if search_ranking_lock.locked():
            raise HTTPException(
                status_code=409,
                detail="另一个搜索定位任务正在运行；为控制模型成本和平台访问频率，请稍后重试",
            )
        service: SearchRankingService = request.app.state.search_ranking_service
        try:
            async with search_ranking_lock:
                return await service.analyze_offer(offer_id)
        except SearchRankingInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SearchRankingConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (SearchRankingProviderError, CompetitorNetworkError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/erp/quadrants")
    def quadrants(
        as_of: date = Query(default_factory=date.today),
        percentile: int = Query(50),
    ) -> dict[str, Any]:
        if percentile not in {25, 50, 75}:
            raise HTTPException(status_code=422, detail="分位数只能是25、50或75")
        settings = DashboardSettings.from_env(root)
        return build_quadrant_payload(
            load_erp_dataset(settings, as_of),
            as_of,
            percentile,
        )

    @app.get("/api/erp/product-thumbnail")
    def product_thumbnail(
        image_url: Annotated[str, Query(min_length=1, max_length=2048)],
        size: int = DEFAULT_MAX_DIMENSION,
    ) -> FileResponse:
        try:
            path = product_thumbnails.thumbnail_path(image_url, size)
        except ProductImageInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ProductImageUnavailableError as exc:
            raise HTTPException(
                status_code=502,
                detail="商品缩略图暂时不可用",
            ) from exc
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "private, max-age=604800, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/erp/risks")
    def risks(as_of: date = Query(default_factory=date.today)) -> dict[str, Any]:
        settings = DashboardSettings.from_env(root)
        return build_risk_payload(load_erp_dataset(settings, as_of), as_of)

    @app.get("/api/erp/logistics")
    def logistics(refresh: bool = Query(False)) -> dict[str, Any]:
        """Return a cached, sanitized, read-only W8 and Takealot shipment overview."""
        return logistics_overview.load(force=refresh)

    @app.post("/api/erp/logistics/links")
    def confirm_logistics_link(
        payload: LogisticsLinkConfirmRequest,
        request: Request,
    ) -> dict[str, Any]:
        """Persist one current graded candidate after operator confirmation."""
        user = request.state.erp_user
        try:
            link = logistics_overview.confirm_candidate(
                w8_order_no=payload.w8_order_no.strip(),
                takealot_shipment_id=payload.takealot_shipment_id,
                actor_user_id=user.id,
                actor_username=user.username,
            )
        except LogisticsLinkError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"link": link}

    @app.post("/api/erp/logistics/links/{link_id}/revoke")
    def revoke_logistics_link(
        link_id: int,
        payload: LogisticsLinkRevokeRequest,
        request: Request,
    ) -> dict[str, Any]:
        """Revoke an operator-confirmed link while retaining its audit history."""
        user = request.state.erp_user
        try:
            link = logistics_overview.revoke_link(
                link_id,
                actor_user_id=user.id,
                actor_username=user.username,
                note=payload.note,
            )
        except LogisticsLinkError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"link": link}

    @app.get("/api/erp/platform-warehouse")
    def platform_warehouse_overview() -> dict[str, Any]:
        """Return guarded drafts plus the latest read-only Takealot shipment snapshot."""
        return platform_warehouse.load()

    @app.get("/api/erp/platform-warehouse/portal/status")
    def platform_warehouse_portal_status(request: Request) -> dict[str, Any]:
        _require_platform_warehouse_loopback(request)
        return {"portal": platform_warehouse.portal_status()}

    @app.post("/api/erp/platform-warehouse/portal/logout")
    def platform_warehouse_portal_logout(request: Request) -> dict[str, Any]:
        _require_platform_warehouse_loopback(request)
        return {"portal": platform_warehouse.portal_logout()}

    @app.post("/api/erp/platform-warehouse/create-direct")
    def create_platform_warehouse_direct(
        payload: PlatformWarehouseDirectCreateRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_platform_warehouse_loopback(request)
        user = request.state.erp_user
        try:
            return platform_warehouse.create_platform_draft_direct(
                [line.model_dump() for line in payload.lines],
                client_request_id=payload.client_request_id,
                actor_user_id=user.id,
                actor_username=user.username,
                note=payload.note,
            )
        except PlatformWarehouseInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PlatformWarehouseNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlatformWarehouseConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (PortalAuthenticationError, PortalDisabledError, PortalError) as exc:
            _raise_platform_warehouse_portal_error(exc)

    @app.post("/api/erp/platform-warehouse/drafts/{draft_id}/verify-otp-and-create")
    def verify_platform_warehouse_otp_and_create(
        draft_id: int,
        payload: PlatformWarehousePortalOtpRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_platform_warehouse_loopback(request)
        user = request.state.erp_user
        try:
            return platform_warehouse.verify_otp_and_continue_create(
                draft_id,
                payload.otp,
                actor_user_id=user.id,
                actor_username=user.username,
            )
        except PlatformWarehouseInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PlatformWarehouseNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlatformWarehouseConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (PortalAuthenticationError, PortalDisabledError, PortalError) as exc:
            _raise_platform_warehouse_portal_error(exc)

    @app.post("/api/erp/platform-warehouse/shipments/{shipment_id}/prepare-action")
    def prepare_platform_warehouse_shipment_action(
        shipment_id: int,
        payload: PlatformWarehousePrepareActionRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_platform_warehouse_loopback(request)
        try:
            return platform_warehouse.prepare_shipment_action(shipment_id, payload.action)
        except PlatformWarehouseNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlatformWarehouseConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (PortalAuthenticationError, PortalDisabledError, PortalError) as exc:
            _raise_platform_warehouse_portal_error(exc)

    @app.post("/api/erp/platform-warehouse/shipments/{shipment_id}/execute-action")
    def execute_platform_warehouse_shipment_action(
        shipment_id: int,
        payload: PlatformWarehouseExecuteActionRequest,
        request: Request,
    ) -> dict[str, Any]:
        _require_platform_warehouse_loopback(request)
        user = request.state.erp_user
        try:
            draft = platform_warehouse.execute_shipment_action(
                shipment_id,
                payload.action,
                approval_token=payload.approval_token,
                confirmation_text=payload.confirmation_text,
                tracking_reference=payload.tracking_reference,
                my_soh_decrease_warehouse_id=payload.my_soh_decrease_warehouse_id,
                actor_user_id=user.id,
                actor_username=user.username,
            )
        except PlatformWarehouseInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PlatformWarehouseNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlatformWarehouseConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (PortalAuthenticationError, PortalDisabledError, PortalError) as exc:
            _raise_platform_warehouse_portal_error(exc)
        return {"draft": draft}

    @app.post("/api/erp/platform-warehouse/drafts/{draft_id}/confirm-po")
    def confirm_platform_warehouse_po(
        draft_id: int,
        payload: PlatformWarehouseConfirmPoRequest,
        request: Request,
    ) -> dict[str, Any]:
        user = request.state.erp_user
        try:
            draft = platform_warehouse.confirm_po(
                draft_id,
                po_number=payload.po_number,
                platform_shipment_id=payload.platform_shipment_id,
                actor_user_id=user.id,
                actor_username=user.username,
                note=payload.note,
            )
        except PlatformWarehouseInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PlatformWarehouseNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlatformWarehouseConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"draft": draft}

    @app.post("/api/erp/platform-warehouse/drafts/{draft_id}/confirm-shipped")
    def confirm_platform_warehouse_shipped(
        draft_id: int,
        payload: PlatformWarehouseConfirmShippedRequest,
        request: Request,
    ) -> dict[str, Any]:
        user = request.state.erp_user
        try:
            draft = platform_warehouse.confirm_shipped(
                draft_id,
                tracking_reference=payload.tracking_reference,
                actor_user_id=user.id,
                actor_username=user.username,
                note=payload.note,
            )
        except PlatformWarehouseInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PlatformWarehouseNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlatformWarehouseConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"draft": draft}

    @app.post("/api/erp/platform-warehouse/drafts/{draft_id}/archive")
    def archive_platform_warehouse_draft(
        draft_id: int,
        payload: PlatformWarehouseArchiveRequest,
        request: Request,
    ) -> dict[str, Any]:
        user = request.state.erp_user
        try:
            draft = platform_warehouse.archive(
                draft_id,
                actor_user_id=user.id,
                actor_username=user.username,
                note=payload.note,
            )
        except PlatformWarehouseInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PlatformWarehouseNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlatformWarehouseConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"draft": draft}

    @app.get("/api/erp/refresh-status")
    def refresh_status(request: Request) -> dict[str, object]:
        return refresh_coordinator.status(role=_refresh_coordination_role(request.state.erp_user))

    @app.post("/api/erp/refresh")
    def refresh(request: Request) -> dict[str, object]:
        user = request.state.erp_user
        coordination_role = _refresh_coordination_role(user)
        try:
            refresh_coordinator.begin(
                username=user.username,
                display_name=user.display_name,
                role=coordination_role,
            )
        except RefreshBusyError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        try:
            selected_store_code = current_store_code()
            result = (
                run_dashboard_refresh(root)
                if selected_store_code == "current"
                else run_dashboard_refresh(root, store_code=selected_store_code)
            )
        except BaseException:
            refresh_coordinator.finish(
                username=user.username,
                display_name=user.display_name,
                succeeded=False,
                role=coordination_role,
            )
            raise
        status = refresh_coordinator.finish(
            username=user.username,
            display_name=user.display_name,
            succeeded=result.succeeded,
            role=coordination_role,
        )
        return {
            "succeeded": result.succeeded,
            "message": result.message,
            "refresh_status": status,
        }

    @app.get("/api/erp/daily-report")
    def operations_daily_report(
        business_date: date = Query(default_factory=_default_operations_business_date),
        capture_start: date | None = Query(default=None),
        capture_end: date | None = Query(default=None),
    ) -> dict[str, Any]:
        effective_capture_end = min(capture_end or business_date, business_date)
        if capture_start is not None and capture_start > effective_capture_end:
            raise HTTPException(
                status_code=422,
                detail="数据完整性说明开始日期不能晚于结束日期",
            )
        settings = DashboardSettings.from_env(root)
        engine = create_read_only_erp_engine(settings.database_url)
        try:
            return daily_report_payload(
                engine,
                business_date,
                capture_start=capture_start,
                capture_end=capture_end,
            )
        finally:
            engine.dispose()

    @app.get("/api/erp/daily-report/events")
    def operations_daily_report_events(request: Request) -> StreamingResponse:
        settings = DashboardSettings.from_env(root)
        engine = create_read_only_erp_engine(settings.database_url)

        async def stream() -> AsyncIterator[str]:
            try:
                async for event in daily_report_event_stream(
                    engine,
                    is_disconnected=request.is_disconnected,
                    business_date=_default_operations_business_date,
                ):
                    yield event
            finally:
                engine.dispose()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/erp/daily-report/reminders")
    def operations_daily_report_reminders() -> dict[str, Any]:
        settings = DashboardSettings.from_env(root)
        engine = create_read_only_erp_engine(settings.database_url)
        try:
            return reminder_payload(engine)
        finally:
            engine.dispose()

    @app.post("/api/erp/daily-report/{business_date}/{offer_id}/manual")
    def operations_daily_report_manual(
        business_date: date,
        offer_id: str,
        payload: DailyReportManualRequest,
        request: Request,
    ) -> dict[str, object]:
        values = payload.model_dump(
            include={"page_views_30_days", "ordered_units", "platform_stock"},
            exclude_unset=True,
        )
        _write_daily_report(
            root,
            lambda engine: save_manual_candidate(
                engine,
                business_date=business_date,
                offer_id=offer_id,
                values=values,
                reason=payload.reason,
                note=payload.note,
                user_id=request.state.erp_user.id,
            ),
        )
        return {"ok": True}

    @app.post("/api/erp/daily-report/{business_date}/{offer_id}/confirm")
    def operations_daily_report_confirm(
        business_date: date,
        offer_id: str,
        payload: DailyReportConfirmRequest,
        request: Request,
    ) -> dict[str, object]:
        _write_daily_report(
            root,
            lambda engine: confirm_entry(
                engine,
                business_date=business_date,
                offer_id=offer_id,
                source=payload.source,
                note=payload.note,
                user_id=request.state.erp_user.id,
            ),
        )
        return {
            "ok": True,
            "exported": (
                request.state.erp_user.can(DAILY_REPORT_EXPORT)
                and _auto_export_operations_if_ready(root, business_date)
            ),
        }

    @app.post("/api/erp/daily-report/{business_date}/{offer_id}/revert-confirmation")
    def operations_daily_report_revert_confirmation(
        business_date: date,
        offer_id: str,
        payload: DailyReportRevertRequest,
        request: Request,
    ) -> dict[str, object]:
        _write_daily_report(
            root,
            lambda engine: revert_confirmation(
                engine,
                business_date=business_date,
                offer_id=offer_id,
                note=payload.note,
                user_id=request.state.erp_user.id,
            ),
        )
        return {"ok": True}

    @app.post("/api/erp/daily-report/{business_date}/confirm-ready")
    def operations_daily_report_confirm_ready(
        business_date: date,
        payload: DailyReportNoteRequest,
        request: Request,
    ) -> dict[str, object]:
        confirmed = int(
            _write_daily_report(
                root,
                lambda engine: confirm_ready_entries(
                    engine,
                    business_date=business_date,
                    note=payload.note,
                    user_id=request.state.erp_user.id,
                ),
            )
            or 0
        )
        return {
            "ok": True,
            "confirmed": confirmed,
            "exported": (
                request.state.erp_user.can(DAILY_REPORT_EXPORT)
                and _auto_export_operations_if_ready(root, business_date)
            ),
        }

    @app.post("/api/erp/daily-report/{business_date}/{offer_id}/stock-alert")
    def operations_daily_report_stock_alert(
        business_date: date,
        offer_id: str,
        payload: DailyReportNoteRequest,
        request: Request,
    ) -> dict[str, object]:
        _write_daily_report(
            root,
            lambda engine: dismiss_stock_alert(
                engine,
                business_date=business_date,
                offer_id=offer_id,
                note=payload.note,
                user_id=request.state.erp_user.id,
            ),
        )
        return {"ok": True}

    @app.post("/api/erp/daily-report/{business_date}/{offer_id}/stock-alert/eliminate")
    def operations_daily_report_eliminate_stock_alert(
        business_date: date,
        offer_id: str,
        payload: DailyReportNoteRequest,
        request: Request,
    ) -> dict[str, object]:
        _write_daily_report(
            root,
            lambda engine: eliminate_stock_alert(
                engine,
                business_date=business_date,
                offer_id=offer_id,
                note=payload.note,
                user_id=request.state.erp_user.id,
            ),
        )
        return {"ok": True}

    @app.post("/api/erp/daily-report/{business_date}/{offer_id}/stock-alert/reopen")
    def operations_daily_report_reopen_stock_alert(
        business_date: date,
        offer_id: str,
        payload: DailyReportNoteRequest,
        request: Request,
    ) -> dict[str, object]:
        _write_daily_report(
            root,
            lambda engine: reopen_stock_alert(
                engine,
                business_date=business_date,
                offer_id=offer_id,
                note=payload.note,
                user_id=request.state.erp_user.id,
            ),
        )
        return {"ok": True}

    @app.post("/api/erp/daily-report/{business_date}/{offer_id}/note")
    def operations_daily_report_note(
        business_date: date,
        offer_id: str,
        payload: DailyReportNoteRequest,
        request: Request,
    ) -> dict[str, object]:
        _write_daily_report(
            root,
            lambda engine: save_operator_note(
                engine,
                business_date=business_date,
                offer_id=offer_id,
                note=payload.note,
                user_id=request.state.erp_user.id,
                issue_type=payload.issue_type,
            ),
        )
        return {"ok": True}

    @app.patch("/api/erp/daily-report/{business_date}/{offer_id}/note/{note_id}")
    def operations_daily_report_note_update(
        business_date: date,
        offer_id: str,
        note_id: int,
        payload: DailyReportNoteRequest,
        request: Request,
    ) -> dict[str, object]:
        _write_daily_report(
            root,
            lambda engine: update_operator_note(
                engine,
                business_date=business_date,
                offer_id=offer_id,
                note_id=note_id,
                note=payload.note,
                user_id=request.state.erp_user.id,
                issue_type=payload.issue_type,
            ),
        )
        return {"ok": True}

    @app.delete("/api/erp/daily-report/{business_date}/{offer_id}/note/{note_id}")
    def operations_daily_report_note_delete(
        business_date: date,
        offer_id: str,
        note_id: int,
        payload: DailyReportDeleteNoteRequest,
        request: Request,
    ) -> dict[str, object]:
        _write_daily_report(
            root,
            lambda engine: delete_operator_note(
                engine,
                business_date=business_date,
                offer_id=offer_id,
                note_id=note_id,
                note=payload.note,
                user_id=request.state.erp_user.id,
            ),
        )
        return {"ok": True}

    @app.get("/api/erp/daily-report/export")
    def operations_daily_report_export_status(
        through: date = Query(default_factory=_default_operations_business_date),
    ) -> dict[str, Any]:
        settings = DashboardSettings.from_env(root)
        engine = create_read_only_erp_engine(settings.database_url)
        try:
            unresolved = unresolved_locations(engine, through)
        finally:
            engine.dispose()
        path = _operations_export_path(root, through)
        return {
            "through": through.isoformat(),
            "blocked": bool(unresolved),
            "unresolved": unresolved,
            "exists": path.is_file(),
            "download_url": (
                f"/api/erp/daily-report/export/download?through={through.isoformat()}"
                if path.is_file()
                else None
            ),
        }

    @app.post("/api/erp/daily-report/export")
    def operations_daily_report_export(payload: ExportRequest) -> dict[str, Any]:
        settings = DashboardSettings.from_env(root)
        engine = create_engine_for_settings(settings)
        try:
            create_schema(engine)
            path = export_operations_workbook(
                engine,
                business_date=payload.as_of,
                destination=_operations_export_path(root, payload.as_of),
            )
        except (DailyReportConflictError, DailyReportInputError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            engine.dispose()
        return {
            "through": payload.as_of.isoformat(),
            "blocked": False,
            "unresolved": [],
            "exists": True,
            "download_url": (
                f"/api/erp/daily-report/export/download?through={payload.as_of.isoformat()}"
            ),
            "name": path.name,
        }

    @app.get("/api/erp/daily-report/export/download")
    def operations_daily_report_export_download(through: date) -> FileResponse:
        path = _operations_export_path(root, through)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="运营日报表格不存在")
        return FileResponse(path, filename=path.name)

    @app.get("/api/erp/exports")
    def exports(as_of: date = Query(default_factory=date.today)) -> dict[str, Any]:
        return _export_payload(root, as_of)

    @app.post("/api/erp/exports")
    def export_reports(request: ExportRequest) -> dict[str, Any]:
        settings = DashboardSettings.from_env(root)
        verify_database_integrity(settings)
        dataset = load_erp_dataset(settings, request.as_of)
        try:
            paths = generate_daily_reports(dataset, root / "exports", request.as_of)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=500, detail="报表生成失败，请检查本地运行环境") from exc
        payload = _export_payload(root, request.as_of)
        payload["png_error"] = paths.png_error
        return payload

    @app.get("/api/erp/exports/download")
    def download_export(
        as_of: date,
        kind: str,
    ) -> FileResponse:
        suffixes = {"html": ".html", "excel": ".xlsx", "png": ".png"}
        suffix = suffixes.get(kind)
        if suffix is None:
            raise HTTPException(status_code=404, detail="未知报表类型")
        basename = f"Takealot运营日报_{as_of.isoformat()}"
        path = root / "exports" / as_of.isoformat() / f"{basename}{suffix}"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="报表文件不存在")
        return FileResponse(path, filename=path.name)

    @app.post("/api/erp/nft102/inspect")
    async def inspect_nft102(
        file: Annotated[UploadFile, File()],
    ) -> dict[str, Any]:
        content = await file.read()
        try:
            inspection = inspect_nft102_upload(file.filename or "", content)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "filename": inspection.filename,
            "size_bytes": inspection.size_bytes,
            "sha256": inspection.sha256,
            "latest_report_date": inspection.latest_report_date.isoformat(),
            "suggested_report_date": inspection.suggested_report_date.isoformat(),
            "product_columns": inspection.product_columns,
        }

    @app.post("/api/erp/nft102/generate")
    async def generate_nft102(
        file: Annotated[UploadFile, File()],
        report_date: Annotated[date, Form()],
    ) -> dict[str, Any]:
        content = await file.read()
        try:
            inspection = inspect_nft102_upload(file.filename or "", content)
            if report_date <= inspection.latest_report_date:
                raise ValueError("新增日期必须晚于表内最新日期。")
            if report_date > datetime.now(ZoneInfo("Asia/Shanghai")).date():
                raise ValueError("不能提前生成未来日期。")
            baseline = persist_nft102_baseline(root, inspection, content)
            result = generate_nft102_from_baseline(root, baseline, report_date)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "report_date": result.report_date.isoformat(),
            "workbook_name": result.workbook_path.name,
            "audit_name": result.audit_text_path.name,
            "workbook_url": _nft_download_url(result.report_date, result.workbook_path.name),
            "audit_url": _nft_download_url(result.report_date, result.audit_text_path.name),
        }

    @app.get("/api/erp/nft102/download")
    def download_nft102(
        report_date: date,
        name: str,
    ) -> FileResponse:
        folder = (root / "outputs" / "nft102-daily" / report_date.isoformat()).resolve()
        path = (folder / Path(name).name).resolve()
        if path.parent != folder or not path.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        return FileResponse(path, filename=path.name)

    @app.get("/api/competitors")
    def competitors(
        request: Request,
        start_date: date | None = Query(default=None),
        end_date: date | None = Query(default=None),
        own_store_scope: Literal["current", "all"] = Query(default="current"),
    ) -> dict[str, object]:
        dataset = _load_competitor_dataset(
            root,
            start_date=start_date,
            end_date=end_date,
            own_store_codes=_own_store_codes_for_request(request, own_store_scope),
        )
        return {
            "items": frame_records(dataset.current),
            "store_items": frame_records(dataset.store_current),
            "own_follower_events": dataset.own_follower_events,
            "date_range": dataset.date_range_payload(),
        }

    @app.get("/api/competitors/link-health")
    def competitor_link_health() -> dict[str, list[dict[str, Any]]]:
        settings = DashboardSettings.from_env(root)
        engine = create_read_only_erp_engine(settings.database_url)
        try:
            return {"items": load_competitor_link_health(engine)}
        finally:
            engine.dispose()

    @app.get("/api/competitors/batch-status")
    def competitor_batch_status() -> dict[str, object]:
        return collection_registry.status()

    @app.post("/api/competitors/batch-options")
    def update_competitor_batch_options(
        payload: CompetitorBatchOptionsRequest,
        request: Request,
    ) -> dict[str, object]:
        require_competitor_batch_controller(request)
        user = request.state.erp_user
        try:
            status = collection_registry.update_options(
                batch_id=payload.batch_id,
                username=user.username,
                visible_browser=payload.visible_browser,
            )
        except CollectionBatchBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        competitor_logger.info(
            "batch_options batch=%s visible_browser=%s user=%s",
            payload.batch_id,
            payload.visible_browser,
            user.username,
        )
        return {"ok": True, "status": status}

    @app.post("/api/competitors/batch-takeover")
    def takeover_competitor_batch(
        payload: CompetitorBatchTakeoverRequest,
        request: Request,
    ) -> dict[str, object]:
        require_competitor_batch_controller(request)
        user = request.state.erp_user
        try:
            status, ready = collection_registry.request_takeover(
                batch_id=payload.batch_id,
                client_id=payload.client_id,
                username=user.username,
            )
        except CollectionBatchBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        competitor_logger.info(
            "batch_takeover batch=%s ready=%s current_plid=%s user=%s",
            payload.batch_id,
            ready,
            status.get("current_plid") or "-",
            user.username,
        )
        return {"ok": True, "ready": ready, "status": status}

    @app.get("/api/competitors/targets")
    def competitor_targets() -> dict[str, list[dict[str, object]]]:
        settings = DashboardSettings.from_env(root)
        engine = create_read_only_erp_engine(settings.database_url)
        try:
            with Session(engine) as session:
                store_plids = connected_store_plids(session)
                has_history = (
                    select(CompetitorSnapshot.id)
                    .where(CompetitorSnapshot.plid == CompetitorTarget.plid)
                    .exists()
                )
                statement = select(
                    CompetitorTarget,
                    has_history.label("has_history"),
                ).where(CompetitorTarget.active.is_(True))
                if store_plids:
                    statement = statement.where(CompetitorTarget.plid.not_in(store_plids))
                target_rows = session.execute(
                    statement
                    .order_by(
                        CompetitorTarget.created_at.asc(),
                        CompetitorTarget.plid.asc(),
                    )
                ).all()
                return {
                    "items": [
                        _competitor_target_payload(
                            target,
                            has_history=bool(target_has_history),
                        )
                        for target, target_has_history in target_rows
                    ]
                }
        finally:
            engine.dispose()

    @app.get("/api/competitors/personal-watchlist")
    def competitor_personal_watchlist(
        request: Request,
    ) -> dict[str, object]:
        """Return only the current account's saved true competitors."""
        user = request.state.erp_user
        settings = DashboardSettings.from_env(root)
        engine = create_read_only_erp_engine(settings.database_url)
        try:
            with Session(engine) as session:
                store_plids = connected_store_plids(session)
                statement = select(CompetitorPersonalWatchlist).where(
                    CompetitorPersonalWatchlist.user_id == user.id
                )
                if store_plids:
                    statement = statement.where(
                        CompetitorPersonalWatchlist.plid.not_in(store_plids)
                    )
                items = session.scalars(
                    statement.order_by(
                        CompetitorPersonalWatchlist.added_at.desc(),
                        CompetitorPersonalWatchlist.plid.asc(),
                    )
                ).all()
                return {
                    "items": [
                        _competitor_personal_watchlist_payload(item)
                        for item in items
                    ],
                    "count": len(items),
                }
        finally:
            engine.dispose()

    @app.put("/api/competitors/personal-watchlist/{plid}")
    def add_competitor_personal_watchlist_item(
        plid: str,
        request: Request,
    ) -> dict[str, object]:
        """Idempotently save one true competitor for the current account."""
        normalized_plid = _validated_competitor_plid(plid)
        user = request.state.erp_user
        settings = DashboardSettings.from_env(root)
        engine = create_engine_for_settings(settings)
        try:
            create_schema(engine)
            with Session(engine) as session:
                target = session.get(CompetitorTarget, normalized_plid)
                if target is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"PLID{normalized_plid} 不是真正竞品记录",
                    )
                if normalized_plid in connected_store_plids(session):
                    raise HTTPException(
                        status_code=409,
                        detail="自有店铺商品不加入个人竞品监控池",
                    )
                item, created = _ensure_competitor_personal_watchlist_item(
                    session,
                    user_id=user.id,
                    plid=normalized_plid,
                    added_at=datetime.now(UTC),
                )
                session.commit()
                result = _competitor_personal_watchlist_payload(item)
        finally:
            engine.dispose()
        competitor_logger.info(
            "personal_watchlist action=add plid=%s user=%s created=%s",
            normalized_plid,
            user.username,
            created,
        )
        return {"item": result, "created": created}

    @app.delete("/api/competitors/personal-watchlist/{plid}")
    def delete_competitor_personal_watchlist_item(
        plid: str,
        request: Request,
    ) -> dict[str, object]:
        """Remove one saved competitor without changing global collection."""
        normalized_plid = _validated_competitor_plid(plid)
        user = request.state.erp_user
        settings = DashboardSettings.from_env(root)
        engine = create_engine_for_settings(settings)
        removed = False
        try:
            create_schema(engine)
            with Session(engine) as session:
                item = session.get(
                    CompetitorPersonalWatchlist,
                    (user.id, normalized_plid),
                )
                if item is not None:
                    session.delete(item)
                    removed = True
                session.commit()
        finally:
            engine.dispose()
        competitor_logger.info(
            "personal_watchlist action=delete plid=%s user=%s removed=%s",
            normalized_plid,
            user.username,
            removed,
        )
        return {"ok": True, "removed": removed}

    @app.post("/api/competitors/targets")
    def create_competitor_target(
        payload: CompetitorTargetRequest,
        request: Request,
    ) -> dict[str, object]:
        plid, url = _validated_competitor_target_url(payload.url)
        user = request.state.erp_user
        settings = DashboardSettings.from_env(root)
        engine = create_engine_for_settings(settings)
        try:
            create_schema(engine)
            now = datetime.now(UTC)
            with Session(engine) as session:
                private_store_rows = [
                    row
                    for row in load_connected_store_offers(session)
                    if str(row.offer.productline_id or "").strip() == plid
                ]
                if private_store_rows:
                    return {
                        "item": None,
                        "queued_to_active_batch": False,
                        "automatic_store_target": True,
                        "store_names": sorted(
                            {row.store_name for row in private_store_rows}
                        ),
                        "personal_watchlist_member": False,
                    }
                target = session.get(CompetitorTarget, plid)
                if target is not None and target.active:
                    _, personal_created = _ensure_competitor_personal_watchlist_item(
                        session,
                        user_id=user.id,
                        plid=plid,
                        added_at=now,
                    )
                    session.commit()
                    competitor_logger.info(
                        "personal_watchlist action=auto_add_existing plid=%s user=%s created=%s",
                        plid,
                        user.username,
                        personal_created,
                    )
                    raise HTTPException(status_code=409, detail=f"PLID{plid} 已在监控清单中")
                old_url = target.url if target is not None else None
                if target is None:
                    target = CompetitorTarget(
                        plid=plid,
                        offer_group_plid=plid,
                        url=url,
                        title=None,
                        active=True,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(target)
                else:
                    if not target.offer_group_plid:
                        target.offer_group_plid = target.plid
                    target.url = url
                    target.active = True
                    target.updated_at = now
                session.add(
                    _competitor_target_audit(
                        plid=plid,
                        action="add",
                        old_url=old_url,
                        new_url=url,
                        user=user,
                        changed_at=now,
                    )
                )
                session.flush()
                _ensure_competitor_personal_watchlist_item(
                    session,
                    user_id=user.id,
                    plid=plid,
                    added_at=now,
                )
                session.commit()
                result = _competitor_target_payload(
                    target,
                    has_history=_target_has_history(session, plid),
                )
        finally:
            engine.dispose()
        queued = collection_registry.enqueue_target(plid=plid, url=url)
        competitor_logger.info(
            "target_change action=add plid=%s user=%s queued=%s",
            plid,
            user.username,
            queued,
        )
        return {
            "item": result,
            "queued_to_active_batch": queued,
            "automatic_store_target": False,
            "store_names": [],
            "personal_watchlist_member": True,
        }

    @app.patch("/api/competitors/targets/{plid}")
    def update_competitor_target(
        plid: str,
        payload: CompetitorTargetRequest,
        request: Request,
    ) -> dict[str, object]:
        normalized_plid, url = _validated_competitor_target_url(payload.url)
        if normalized_plid != plid:
            raise HTTPException(
                status_code=422,
                detail="修改链接不能改变 PLID；请删除旧链接后再新增",
            )
        user = request.state.erp_user
        settings = DashboardSettings.from_env(root)
        engine = create_engine_for_settings(settings)
        try:
            create_schema(engine)
            now = datetime.now(UTC)
            with Session(engine) as session:
                target = session.get(CompetitorTarget, plid)
                if target is None or not target.active:
                    raise HTTPException(status_code=404, detail=f"PLID{plid} 不在监控清单中")
                if target.url == url:
                    raise HTTPException(status_code=409, detail="链接没有变化")
                old_url = target.url
                target.url = url
                target.updated_at = now
                session.add(
                    _competitor_target_audit(
                        plid=plid,
                        action="update",
                        old_url=old_url,
                        new_url=url,
                        user=user,
                        changed_at=now,
                    )
                )
                session.commit()
                result = _competitor_target_payload(
                    target,
                    has_history=_target_has_history(session, plid),
                )
        finally:
            engine.dispose()
        competitor_logger.info(
            "target_change action=update plid=%s user=%s",
            plid,
            user.username,
        )
        return {"item": result}

    @app.delete("/api/competitors/targets/{plid}")
    def delete_competitor_target(
        plid: str,
        request: Request,
    ) -> dict[str, object]:
        user = request.state.erp_user
        settings = DashboardSettings.from_env(root)
        engine = create_engine_for_settings(settings)
        try:
            create_schema(engine)
            now = datetime.now(UTC)
            with Session(engine) as session:
                target = session.get(CompetitorTarget, plid)
                if target is None or not target.active:
                    raise HTTPException(status_code=404, detail=f"PLID{plid} 不在监控清单中")
                old_url = target.url
                target.active = False
                target.updated_at = now
                session.add(
                    _competitor_target_audit(
                        plid=plid,
                        action="delete",
                        old_url=old_url,
                        new_url=None,
                        user=user,
                        changed_at=now,
                    )
                )
                session.commit()
        finally:
            engine.dispose()
        competitor_logger.info(
            "target_change action=delete plid=%s user=%s",
            plid,
            user.username,
        )
        return {
            "ok": True,
            "history_retained": True,
        }

    @app.post("/api/competitors/targets/{plid}/prioritize")
    def prioritize_competitor_target(
        plid: str,
        request: Request,
        payload: CompetitorTargetPriorityRequest | None = None,
    ) -> dict[str, object]:
        user = request.state.erp_user
        source = payload.source if payload is not None else "manual"
        settings = DashboardSettings.from_env(root)
        engine = create_read_only_erp_engine(settings.database_url)
        is_true_competitor = False
        try:
            with Session(engine) as session:
                target = session.get(CompetitorTarget, plid)
                if target is not None and target.active:
                    is_true_competitor = True
                    url = target.url
                else:
                    accessible_store_codes = _own_store_codes_for_request(
                        request,
                        "all",
                    )
                    own_store_match = next(
                        (
                            row
                            for row in load_connected_store_offers(session)
                            if row.store_code in accessible_store_codes
                            and str(row.offer.productline_id or "").strip() == plid
                        ),
                        None,
                    )
                    if own_store_match is None:
                        raise HTTPException(
                            status_code=404,
                            detail=f"PLID{plid} 不在可访问的真正竞品或自有链接中",
                        )
                    url = f"https://www.takealot.com/p/PLID{plid}"
        finally:
            engine.dispose()

        try:
            status, accepted = collection_registry.prioritize_target(
                plid=plid,
                url=url,
                requested_by=user.display_name or user.username,
                source=source,
            )
        except CollectionBatchBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if accepted and source == "manual_retry" and is_true_competitor:
            audit_engine = create_engine_for_settings(settings)
            try:
                create_schema(audit_engine)
                with Session(audit_engine) as session:
                    session.add(
                        _competitor_target_audit(
                            plid=plid,
                            action="manual_retry",
                            old_url=None,
                            new_url=url,
                            user=user,
                            changed_at=datetime.now(UTC),
                        )
                    )
                    session.commit()
            finally:
                audit_engine.dispose()
        competitor_logger.info(
            "target_priority plid=%s user=%s batch=%s source=%s target_type=%s accepted=%s",
            plid,
            user.username,
            status["batch_id"],
            source,
            "competitor" if is_true_competitor else "own_store",
            accepted,
        )
        return {"ok": True, "accepted": accepted, "status": status}

    @app.get("/api/competitors/store-targets")
    def competitor_store_targets(
        request: Request,
        own_store_scope: Literal["current", "all"] = Query(default="current"),
    ) -> dict[str, object]:
        """Return private PLIDs for the selected store or authorized all-store view."""
        settings = DashboardSettings.from_env(root)
        engine = create_read_only_erp_engine(settings.database_url)
        try:
            with Session(engine) as session:
                rows = load_connected_store_offers(session)
            accessible_codes = _own_store_codes_for_request(request, "all")
            accessible_rows = [row for row in rows if row.store_code in accessible_codes]
            selected_codes = _own_store_codes_for_request(request, own_store_scope)
            rows = [row for row in accessible_rows if row.store_code in selected_codes]
            grouped: dict[str, list[ConnectedStoreOffer]] = defaultdict(list)
            for row in rows:
                plid = str(row.offer.productline_id or "").strip()
                if plid:
                    grouped[plid].append(row)
            items = [
                {
                    "plid": plid,
                    "url": f"https://www.takealot.com/p/PLID{plid}",
                    "title": next(
                        (row.offer.title for row in offers if row.offer.title),
                        f"PLID{plid}",
                    ),
                    "offer_count": len(offers),
                    "store_count": len({row.store_code for row in offers}),
                    "store_names": sorted({row.store_name for row in offers}),
                    "captured_at": min(
                        row.offer.captured_at for row in offers
                    ).isoformat(),
                }
                for plid, offers in sorted(grouped.items())
            ]
            accessible_memberships = {
                (row.store_code, str(row.offer.productline_id).strip())
                for row in accessible_rows
                if str(row.offer.productline_id or "").strip()
            }
            selected_memberships = {
                (row.store_code, str(row.offer.productline_id).strip())
                for row in rows
                if str(row.offer.productline_id or "").strip()
            }
            accessible_plids = {plid for _, plid in accessible_memberships}
            return {
                "items": items,
                "scope": own_store_scope,
                "selected_store_count": len(selected_codes),
                "selected_membership_count": len(selected_memberships),
                "all_store_count": len(accessible_codes),
                "all_store_unique_count": len(accessible_plids),
                "all_store_membership_count": len(accessible_memberships),
            }
        finally:
            engine.dispose()

    @app.get("/api/competitors/target-audits")
    def competitor_target_audits(
        start_date: date | None = Query(default=None),
        end_date: date | None = Query(default=None),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, object]:
        if start_date is not None and end_date is not None and start_date > end_date:
            raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
        settings = DashboardSettings.from_env(root)
        engine = create_read_only_erp_engine(settings.database_url)
        try:
            with Session(engine) as session:
                available_start, available_end = session.execute(
                    select(
                        func.min(CompetitorTargetAudit.changed_at),
                        func.max(CompetitorTargetAudit.changed_at),
                    )
                ).one()
                statement = select(CompetitorTargetAudit)
                count_statement = select(func.count()).select_from(
                    CompetitorTargetAudit
                )
                if start_date is not None:
                    start_condition = (
                        CompetitorTargetAudit.changed_at >= _beijing_day_start_utc(start_date)
                    )
                    statement = statement.where(start_condition)
                    count_statement = count_statement.where(start_condition)
                if end_date is not None:
                    end_condition = (
                        CompetitorTargetAudit.changed_at
                        < _beijing_day_start_utc(end_date + timedelta(days=1))
                    )
                    statement = statement.where(end_condition)
                    count_statement = count_statement.where(end_condition)
                total = int(session.scalar(count_statement) or 0)
                audits = session.scalars(
                    statement.order_by(
                        CompetitorTargetAudit.changed_at.desc(),
                        CompetitorTargetAudit.id.desc(),
                    )
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                ).all()
        finally:
            engine.dispose()
        return {
            "items": [_competitor_target_audit_payload(audit) for audit in audits],
            "total": total,
            "page": page,
            "page_size": page_size,
            "date_range": {
                "available_start": _beijing_date_iso(available_start),
                "available_end": _beijing_date_iso(available_end),
                "selected_start": start_date.isoformat() if start_date else None,
                "selected_end": end_date.isoformat() if end_date else None,
            },
        }

    @app.get("/api/competitors/{plid}")
    def competitor_detail(
        plid: str,
        request: Request,
        start_date: date | None = Query(default=None),
        end_date: date | None = Query(default=None),
        own_store_scope: Literal["current", "all"] = Query(default="current"),
    ) -> dict[str, list[dict[str, Any]]]:
        dataset = _load_competitor_dataset(
            root,
            start_date=start_date,
            end_date=end_date,
            own_store_codes=_own_store_codes_for_request(request, own_store_scope),
        )
        history = dataset.history
        reviews = dataset.reviews
        variants = dataset.variants
        store_item = None
        if not dataset.store_current.empty:
            matching_store_items = dataset.store_current.loc[
                dataset.store_current["plid"].astype(str) == plid
            ]
            if not matching_store_items.empty:
                store_item = matching_store_items.iloc[0]
        if not history.empty:
            history = history.loc[history["plid"].astype(str) == plid]
        if not reviews.empty:
            reviews = reviews.loc[reviews["plid"].astype(str) == plid]
        if not variants.empty:
            variants = variants.loc[variants["plid"].astype(str) == plid]
        if store_item is not None:
            history = dataset.store_history
            if not history.empty:
                history = history.loc[history["plid"].astype(str) == plid]
        return {
            "history": frame_records(history),
            "reviews": frame_records(reviews),
            "variants": frame_records(variants),
        }

    @app.post("/api/competitors/collect")
    async def collect_competitor(
        payload: CollectCompetitorRequest,
        request: Request,
    ) -> dict[str, object]:
        require_competitor_batch_controller(request)
        try:
            plid = extract_plid(payload.url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        user = request.state.erp_user
        try:
            collection_registry.start_link(
                batch_id=payload.batch_id,
                client_id=payload.client_id,
                request_id=payload.request_id,
                username=user.username,
                display_name=user.display_name,
                item_index=payload.item_index,
                total_items=payload.total_items,
                plid=plid,
                retry_kind=payload.retry_kind,
                retry_attempt=payload.retry_attempt,
                with_stock_probe=payload.with_stock_probe,
                visible_browser=payload.visible_browser,
            )
        except CollectionBatchBusyError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc
        effective_with_stock_probe, effective_visible_browser = (
            collection_registry.collection_options(
                batch_id=payload.batch_id,
                fallback_with_stock_probe=payload.with_stock_probe,
                fallback_visible_browser=payload.visible_browser,
            )
        )

        async def execute_collection() -> CompetitorCollectionResult:
            registry_reason = ""

            def report_stage(stage: str) -> None:
                collection_registry.update_link_stage(
                    batch_id=payload.batch_id,
                    request_id=payload.request_id,
                    stage=stage,
                )
                competitor_logger.info(
                    "link_stage batch=%s request=%s item=%s/%s plid=%s stage=%s",
                    payload.batch_id or "-",
                    payload.request_id or "-",
                    _display_item_number(payload.item_index),
                    payload.total_items or "-",
                    plid,
                    _single_line(stage),
                )

            competitor_logger.info(
                "link_start batch=%s request=%s item=%s/%s plid=%s",
                payload.batch_id or "-",
                payload.request_id or "-",
                _display_item_number(payload.item_index),
                payload.total_items or "-",
                plid,
            )
            settings = DashboardSettings.from_env(root)
            engine = create_engine_for_settings(settings)
            try:
                create_schema(engine)
                with Session(engine) as session:
                    followers_only = plid in connected_store_plids(session)
                try:
                    async with competitor_public_client.lease(
                        wait_callback=lambda delay_seconds: report_stage(
                            f"正在随机节流 {delay_seconds:.1f} 秒，降低访问频率"
                        )
                    ) as public_client_lease:
                        async with CompetitorCollector(
                            engine=engine,
                            project_root=root,
                            client=public_client_lease.client,
                            progress_callback=report_stage,
                        ) as collector:
                            result = await collector.collect(
                                payload.url,
                                with_stock_probe=effective_with_stock_probe,
                                visible_browser=effective_visible_browser,
                                followers_only=followers_only,
                            )
                        added_targets = (
                            ()
                            if followers_only
                            else _sync_discovered_competitor_targets(
                                engine,
                                origin_plid=plid,
                                discovered_targets=result.discovered_targets,
                                user=user,
                            )
                        )
                        queued_target_count = sum(
                            collection_registry.enqueue_target(
                                plid=target.plid,
                                url=target.url,
                            )
                            for target in added_targets
                        )
                        if added_targets:
                            addition_note = (
                                f"另发现并加入 {len(added_targets)} 条跟卖链接"
                            )
                            result = replace(
                                result,
                                message=f"{result.message}；{addition_note}",
                                added_target_count=len(added_targets),
                            )
                            competitor_logger.info(
                                "offer_targets_discovered origin_plid=%s added=%s queued=%s user=%s",
                                plid,
                                len(added_targets),
                                queued_target_count,
                                user.username,
                            )
                        if result.failure_kind in {"network", "other"}:
                            public_client_lease.invalidate()
                except CompetitorNetworkError as exc:
                    registry_reason = _single_line(str(exc))
                    competitor_logger.warning(
                        "link_failure batch=%s request=%s item=%s/%s "
                        "plid=%s kind=network reason=%s",
                        payload.batch_id or "-",
                        payload.request_id or "-",
                        _display_item_number(payload.item_index),
                        payload.total_items or "-",
                        plid,
                        _single_line(str(exc)),
                    )
                    raise
                registry_reason = _single_line(result.message)
            except BaseException as exc:
                registry_reason = registry_reason or _single_line(str(exc))
                if not isinstance(exc, CompetitorNetworkError):
                    competitor_logger.error(
                        "link_exception batch=%s request=%s item=%s/%s plid=%s type=%s reason=%s",
                        payload.batch_id or "-",
                        payload.request_id or "-",
                        _display_item_number(payload.item_index),
                        payload.total_items or "-",
                        plid,
                        type(exc).__name__,
                        _single_line(str(exc)),
                    )
                raise
            finally:
                engine.dispose()
                collection_registry.finish_link(
                    batch_id=payload.batch_id,
                    request_id=payload.request_id,
                    reason=registry_reason,
                )
            competitor_logger.info(
                "link_result batch=%s request=%s item=%s/%s plid=%s "
                "succeeded=%s kind=%s retryable=%s reason=%s",
                payload.batch_id or "-",
                payload.request_id or "-",
                _display_item_number(payload.item_index),
                payload.total_items or "-",
                result.plid,
                result.succeeded,
                result.failure_kind or "-",
                result.retryable,
                _single_line(result.message),
            )
            return result

        try:
            result, reused = await collection_coordinator.run(
                payload.request_id,
                execute_collection,
            )
        except asyncio.CancelledError as exc:
            raise HTTPException(
                status_code=409,
                detail="采集已停止，当前浏览器探测已中断并关闭",
            ) from exc
        except CompetitorNetworkError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if reused:
            competitor_logger.info(
                "link_reused batch=%s request=%s item=%s/%s plid=%s",
                payload.batch_id or "-",
                payload.request_id or "-",
                _display_item_number(payload.item_index),
                payload.total_items or "-",
                result.plid,
            )
            collection_registry.finish_link(
                batch_id=payload.batch_id,
                request_id=payload.request_id,
                reason=_single_line(result.message),
            )
        if not result.succeeded:
            status_code = _collection_failure_status(
                result.failure_kind,
                retryable=result.retryable,
            )
            raise HTTPException(status_code=status_code, detail=result.message)
        return {
            "plid": result.plid,
            "title": result.title,
            "message": result.message,
            "added_target_count": result.added_target_count,
        }

    @app.post("/api/competitors/batch-events")
    async def competitor_batch_event(
        payload: CompetitorBatchEventRequest,
        request: Request,
    ) -> dict[str, object]:
        require_competitor_batch_controller(request)
        user = request.state.erp_user
        try:
            status = collection_registry.event(
                batch_id=payload.batch_id,
                client_id=payload.client_id,
                event=payload.event,
                username=user.username,
                display_name=user.display_name,
                completed=payload.completed,
                total=payload.total,
                pending=payload.pending,
                succeeded=payload.succeeded,
                failed=payload.failed,
                terminal=payload.terminal,
                reason=payload.reason,
                with_stock_probe=payload.with_stock_probe,
                visible_browser=payload.visible_browser,
            )
        except CollectionBatchBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        cancelled_request_id = (
            str(status.get("current_request_id") or "")
            if payload.event == "manual_stop"
            else ""
        )
        if payload.event == "manual_stop":
            cancelled = await collection_coordinator.cancel(cancelled_request_id)
            # The cancelled request normally invalidates and closes the shared
            # public browser through its lease.  Close once more here so a stop
            # between links also leaves no reusable browser process behind.
            await competitor_public_client.close()
            status = collection_registry.status()
            competitor_logger.info(
                "batch_cancel batch=%s request=%s cancelled=%s user=%s",
                payload.batch_id,
                cancelled_request_id or "-",
                cancelled,
                user.username,
            )
        effective_event = str(status["event"])
        if effective_event != payload.event:
            competitor_logger.info(
                "batch_event batch=%s event=%s submitted_event=%s completed=%s "
                "total=%s pending=%s succeeded=%s failed=%s terminal=%s user=%s "
                "reason=%s",
                payload.batch_id,
                effective_event,
                payload.event,
                payload.completed,
                payload.total,
                payload.pending,
                payload.succeeded,
                payload.failed,
                payload.terminal,
                user.username,
                _single_line(payload.reason),
            )
        else:
            competitor_logger.info(
                "batch_event batch=%s event=%s completed=%s total=%s pending=%s "
                "succeeded=%s failed=%s terminal=%s user=%s reason=%s",
                payload.batch_id,
                payload.event,
                payload.completed,
                payload.total,
                payload.pending,
                payload.succeeded,
                payload.failed,
                payload.terminal,
                user.username,
                _single_line(payload.reason),
            )
        return {"ok": True, "status": status}

    frontend_dist = root / "frontend" / "competitor" / "dist"
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
    else:

        @app.get("/", response_class=HTMLResponse)
        def missing_frontend() -> str:
            return (
                "<h1>ERP 前端尚未构建</h1><p>请在 frontend/competitor 目录执行 npm run build。</p>"
            )

    return app


def _validated_competitor_target_url(value: str) -> tuple[str, str]:
    url = value.strip()
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").casefold()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="链接格式无效") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or (hostname != "takealot.com" and not hostname.endswith(".takealot.com"))
    ):
        raise HTTPException(status_code=422, detail="请输入 Takealot 商品链接")
    try:
        plid = extract_plid(url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return plid, url


def _validated_competitor_plid(value: str) -> str:
    plid = value.strip()
    if not plid.isdigit() or len(plid) > 30:
        raise HTTPException(status_code=422, detail="PLID 必须是数字")
    return plid


def _ensure_competitor_personal_watchlist_item(
    session: Session,
    *,
    user_id: int,
    plid: str,
    added_at: datetime,
) -> tuple[CompetitorPersonalWatchlist, bool]:
    item = session.get(CompetitorPersonalWatchlist, (user_id, plid))
    if item is not None:
        return item, False
    item = CompetitorPersonalWatchlist(
        user_id=user_id,
        plid=plid,
        added_at=added_at,
    )
    session.add(item)
    return item, True


def _competitor_personal_watchlist_payload(
    item: CompetitorPersonalWatchlist,
) -> dict[str, object]:
    return {
        "plid": item.plid,
        "added_at": item.added_at.isoformat(),
    }


def _competitor_target_payload(
    target: CompetitorTarget,
    *,
    has_history: bool,
) -> dict[str, object]:
    return {
        "plid": target.plid,
        "offer_group_plid": target.offer_group_plid or target.plid,
        "url": target.url,
        "title": target.title,
        "created_at": target.created_at.isoformat(),
        "updated_at": target.updated_at.isoformat(),
        "has_history": has_history,
    }


def _sync_discovered_competitor_targets(
    engine: Engine,
    *,
    origin_plid: str,
    discovered_targets: tuple[CompetitorDiscoveredTarget, ...],
    user: UserIdentity,
) -> tuple[CompetitorDiscoveredTarget, ...]:
    """Persist only new/reactivated crawlable offers and merge their target groups."""
    if not discovered_targets:
        return ()
    now = datetime.now(UTC)
    added: list[CompetitorDiscoveredTarget] = []
    with Session(engine) as session:
        store_plids = connected_store_plids(session)
        unique_targets = {
            target.plid: target
            for target in discovered_targets
            if target.plid not in store_plids
        }
        origin = unique_targets.get(origin_plid)
        if origin is None:
            return ()
        origin_row = session.get(CompetitorTarget, origin_plid)
        if origin_row is None:
            origin_row = CompetitorTarget(
                plid=origin.plid,
                offer_group_plid=origin.plid,
                url=origin.url,
                title=origin.title,
                active=True,
                created_at=now,
                updated_at=now,
            )
            session.add(origin_row)
            session.flush()
        group_plid = origin_row.offer_group_plid or origin_row.plid
        origin_row.offer_group_plid = group_plid

        existing_rows = {
            target_plid: session.get(CompetitorTarget, target_plid)
            for target_plid in unique_targets
        }
        merged_group_ids = {
            row.offer_group_plid or row.plid
            for row in existing_rows.values()
            if row is not None
        }
        if merged_group_ids:
            for group_member in session.scalars(
                select(CompetitorTarget).where(
                    CompetitorTarget.offer_group_plid.in_(merged_group_ids)
                )
            ):
                group_member.offer_group_plid = group_plid

        for target_plid, discovered in unique_targets.items():
            target_row = existing_rows.get(target_plid)
            added_now = False
            if target_row is None:
                target_row = CompetitorTarget(
                    plid=target_plid,
                    offer_group_plid=group_plid,
                    url=discovered.url,
                    title=discovered.title,
                    active=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(target_row)
                if target_plid != origin_plid:
                    added.append(discovered)
                    added_now = True
            else:
                target_row.offer_group_plid = group_plid
                if not target_row.title:
                    target_row.title = discovered.title
                if not target_row.active:
                    target_row.url = discovered.url
                    target_row.active = True
                    target_row.updated_at = now
                    if target_plid != origin_plid:
                        added.append(discovered)
                        added_now = True
            if added_now:
                session.add(
                    _competitor_target_audit(
                        plid=target_plid,
                        action="auto_discover",
                        old_url=None,
                        new_url=discovered.url,
                        user=user,
                        changed_at=now,
                    )
                )
        session.commit()
    return tuple(added)


def _target_has_history(session: Session, plid: str) -> bool:
    return bool(
        session.scalar(
            select(func.count())
            .select_from(CompetitorSnapshot)
            .where(CompetitorSnapshot.plid == plid)
        )
    )


def _competitor_target_audit(
    *,
    plid: str,
    action: str,
    old_url: str | None,
    new_url: str | None,
    user: UserIdentity,
    changed_at: datetime,
) -> CompetitorTargetAudit:
    return CompetitorTargetAudit(
        plid=plid,
        action=action,
        old_url=old_url,
        new_url=new_url,
        actor_user_id=user.id,
        actor_username=user.username,
        actor_display_name=user.display_name,
        changed_at=changed_at,
    )


def _competitor_target_audit_payload(
    audit: CompetitorTargetAudit,
) -> dict[str, object]:
    return {
        "id": audit.id,
        "plid": audit.plid,
        "action": audit.action,
        "old_url": audit.old_url,
        "new_url": audit.new_url,
        "actor_username": audit.actor_username,
        "actor_display_name": audit.actor_display_name,
        "changed_at": audit.changed_at.isoformat(),
    }


def _beijing_day_start_utc(value: date) -> datetime:
    return datetime.combine(
        value,
        datetime.min.time(),
        tzinfo=ZoneInfo("Asia/Shanghai"),
    ).astimezone(UTC)


def _beijing_date_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _required_permission(path: str, method: str) -> str | tuple[str, ...] | None:
    """Map every authenticated API route to its server-enforced permission."""
    safe_method = method in {"GET", "HEAD", "OPTIONS"}
    if path == "/api/auth/logout":
        return None
    if path.startswith(("/api/auth/users", "/api/auth/stores")):
        return USERS_MANAGE
    if path in {"/api/erp/freshness", "/api/erp/refresh-status"}:
        return None
    if path == "/api/erp/product-thumbnail":
        return STORE_VIEW, COMPETITORS_VIEW, DAILY_REPORT_VIEW
    if path == "/api/erp/refresh":
        return REFRESH_RUN
    if path.startswith("/api/erp/logistics/links"):
        return STORE_VIEW if safe_method else LOGISTICS_MANAGE
    if path.startswith("/api/erp/platform-warehouse"):
        return STORE_VIEW if safe_method else LOGISTICS_MANAGE
    if path.startswith("/api/erp/keyword-traffic"):
        return STORE_VIEW
    if path.startswith("/api/erp/search-ranking"):
        return STORE_VIEW if safe_method else SEARCH_RANKING_RUN
    if path.startswith("/api/erp/daily-report/export"):
        return DAILY_REPORT_VIEW if safe_method else DAILY_REPORT_EXPORT
    if path.startswith("/api/erp/daily-report"):
        return DAILY_REPORT_VIEW if safe_method else DAILY_REPORT_MANAGE
    if path.startswith("/api/erp/exports"):
        return REPORTS_VIEW if safe_method else REPORTS_GENERATE
    if path.startswith("/api/erp/nft102"):
        return NFT102_MANAGE
    if path.startswith("/api/competitors/personal-watchlist"):
        return COMPETITORS_VIEW
    if path.startswith("/api/competitors"):
        return COMPETITORS_VIEW if safe_method else COMPETITORS_COLLECT
    if path.startswith(
        (
            "/api/erp/summary",
            "/api/erp/products",
            "/api/erp/quadrants",
            "/api/erp/risks",
            "/api/erp/logistics",
        )
    ):
        return STORE_VIEW
    return STORE_VIEW if safe_method else "__unsupported_write__"


def _requires_connected_store_access(path: str) -> bool:
    """Gate the current single-store dataset behind an assigned connected store."""
    return (
        path
        in {
            "/api/erp/freshness",
            "/api/erp/refresh-status",
            "/api/erp/refresh",
        }
        or path.startswith(
            (
                "/api/erp/summary",
                "/api/erp/products",
                "/api/erp/keyword-traffic",
                "/api/erp/search-ranking",
                "/api/erp/quadrants",
                "/api/erp/risks",
                "/api/erp/logistics",
                "/api/erp/platform-warehouse",
                "/api/erp/daily-report",
                "/api/erp/exports",
            )
        )
    )


def _can_access_required_permission(
    user: UserIdentity,
    permission: str | tuple[str, ...],
) -> bool:
    if isinstance(permission, tuple):
        return any(user.can(candidate) for candidate in permission)
    return user.can(permission)


def _permission_denied_message(permission: str | tuple[str, ...]) -> str:
    if permission == USERS_MANAGE:
        return "当前账号没有用户权限管理权限"
    if permission == DAILY_REPORT_MANAGE:
        return "当前账号可以查看运营日报，但不能处理待办"
    if permission == DAILY_REPORT_EXPORT:
        return "当前账号不能生成运营日报 Excel"
    if permission == COMPETITORS_COLLECT:
        return "当前账号不能采集竞品"
    if permission == REFRESH_RUN:
        return "当前账号不能刷新全部数据"
    if permission == LOGISTICS_MANAGE:
        return "当前账号可以查看物流数据，但不能确认或撤销物流关联"
    if permission == SEARCH_RANKING_RUN:
        return "当前账号可以查看搜索定位，但不能调用模型或采集搜索排名"
    if permission in {REPORTS_GENERATE, NFT102_MANAGE}:
        return "当前账号不能执行报表生成或续写"
    return "当前账号没有访问此模块的权限"


def _refresh_coordination_role(user: UserIdentity) -> str:
    if not user.can(REFRESH_RUN):
        return "viewer"
    return "admin" if user.role == "admin" else "operator"


def _collection_failure_status(
    failure_kind: str | None,
    *,
    retryable: bool,
) -> int:
    if failure_kind == "network":
        return 503
    if failure_kind == "validation-uncertain":
        return 409
    if failure_kind == "stock-unprobed":
        return 424
    if failure_kind == "suspected-invalid":
        return 404
    if failure_kind == "confirmed-invalid":
        return 410
    return 503 if retryable else 422


def _display_item_number(item_index: int | None) -> int | str:
    return item_index + 1 if item_index is not None else "-"


def _single_line(value: str) -> str:
    return " ".join(value.split())[:500]


def _load_competitor_dataset(
    project_root: Path,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    own_store_codes: set[str] | None = None,
) -> CompetitorDataset:
    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    settings = DashboardSettings.from_env(project_root)
    path = sqlite_database_path(settings.database_url)
    if path is not None and not path.exists():
        return CompetitorDataset(
            current=pd.DataFrame(),
            history=pd.DataFrame(),
            reviews=pd.DataFrame(),
            variants=pd.DataFrame(),
            selected_start_date=start_date,
            selected_end_date=end_date,
        )
    engine = create_read_only_erp_engine(settings.database_url)
    try:
        try:
            return load_competitor_dataset(
                engine,
                start_date=start_date,
                end_date=end_date,
                own_store_codes=own_store_codes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        engine.dispose()


def _own_store_codes_for_request(
    request: Request,
    own_store_scope: Literal["current", "all"],
) -> set[str]:
    """Resolve connected own-store visibility without exposing unauthorized stores."""
    accessible_codes = {
        store.code
        for store in request.state.erp_user.accessible_stores
        if store.active and store.data_connected
    }
    if own_store_scope == "all":
        return accessible_codes
    selected_store = getattr(request.state, "erp_store", None)
    if selected_store is None or selected_store.code not in accessible_codes:
        return set()
    return {selected_store.code}


def _write_daily_report(
    project_root: Path,
    operation: Callable[[Engine], Any],
) -> Any:
    settings = DashboardSettings.from_env(project_root)
    engine = create_engine_for_settings(settings)
    try:
        create_schema(engine)
        return operation(engine)
    except DailyReportInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DailyReportConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        engine.dispose()


def _operations_export_path(project_root: Path, through: date) -> Path:
    return (
        project_root
        / "exports"
        / "operations-daily"
        / through.isoformat()
        / f"运营日报_{through.isoformat()}.xlsx"
    )


def _auto_export_operations_if_ready(project_root: Path, through: date) -> bool:
    settings = DashboardSettings.from_env(project_root)
    engine = create_engine_for_settings(settings)
    try:
        create_schema(engine)
        if unresolved_locations(engine, through):
            return False
        export_operations_workbook(
            engine,
            business_date=through,
            destination=_operations_export_path(project_root, through),
        )
        return True
    finally:
        engine.dispose()


def _export_payload(project_root: Path, as_of: date) -> dict[str, Any]:
    basename = f"Takealot运营日报_{as_of.isoformat()}"
    partition = project_root / "exports" / as_of.isoformat()
    specs = {
        "html": (partition / f"{basename}.html", "离线网页"),
        "excel": (partition / f"{basename}.xlsx", "电子表格"),
        "png": (partition / f"{basename}.png", "图片"),
    }
    return {
        "as_of": as_of.isoformat(),
        "files": [
            {
                "kind": kind,
                "label": label,
                "exists": path.is_file(),
                "name": path.name,
                "download_url": (
                    f"/api/erp/exports/download?as_of={as_of.isoformat()}&kind={kind}"
                    if path.is_file()
                    else None
                ),
            }
            for kind, (path, label) in specs.items()
        ],
    }


def _nft_download_url(report_date: date, name: str) -> str:
    return f"/api/erp/nft102/download?report_date={report_date.isoformat()}&name={quote(name)}"


def _is_loopback_request(request: Request) -> bool:
    return bool(request.client and request.client.host in {"127.0.0.1", "::1", "localhost"})


def _require_platform_warehouse_loopback(request: Request) -> None:
    if not _is_loopback_request(request):
        raise HTTPException(
            status_code=403,
            detail="Seller Portal 登录、预审和写入只允许从 ERP 服务器本机执行",
        )


def _raise_platform_warehouse_portal_error(exc: PortalError) -> NoReturn:
    if isinstance(exc, PortalDisabledError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, PortalAuthenticationError):
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    raise HTTPException(status_code=502, detail=str(exc)) from exc


def _session_response(request: Request, issued: IssuedSession) -> Response:
    response = JSONResponse(
        {
            "user": issued.user.as_dict(),
            "csrf_token": issued.csrf_token,
            "expires_at": issued.expires_at.isoformat(),
        }
    )
    _set_session_cookie(response, request, issued.token)
    return response


def _set_session_cookie(
    response: Response,
    request: Request,
    token: str,
) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )


def _renew_session_cookie(
    response: Response,
    request: Request,
    token: str | None,
    *,
    renewed: bool,
) -> Response:
    if renewed and token:
        _set_session_cookie(response, request, token)
    return response


app = create_app()
