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
from dataclasses import dataclass, replace
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
    read_collection_round_summaries,
)
from takealot_ops.competitors.listings import (
    BALANCED_LISTING_SELECTION_RULE,
    MAX_LISTING_PRODUCTS,
    CompetitorListingInputError,
    CompetitorListingPreviewExpiredError,
    CompetitorListingPreviewRegistry,
    CompetitorListingProviderError,
    build_competitor_listing_url,
    finalize_competitor_listing_preview,
    parse_competitor_listing_source,
    preview_competitor_listing,
)
from takealot_ops.competitors.own_store_sales import (
    OWN_STORE_SALES_WINDOW_DAYS,
    aggregate_own_store_sales_series,
    build_own_store_sales_detail,
    build_own_store_sales_series,
    build_own_store_sales_series_bulk,
    summarize_own_store_sales_windows,
)
from takealot_ops.competitors.own_store_traffic import (
    build_own_store_traffic_series,
)
from takealot_ops.competitors.service import (
    CompetitorCollectionResult,
    CompetitorCollector,
    CompetitorDataset,
    CompetitorDiscoveredTarget,
    load_competitor_dataset,
    load_competitor_link_health,
)
from takealot_ops.competitors.scheduled import (
    SCHEDULED_CLIENT_ID,
    SCHEDULED_OWNER_DISPLAY_NAME,
    SCHEDULED_OWNER_USERNAME,
    ScheduledCollectionAttempt,
    ScheduledCollectionTarget,
    ScheduledCompetitorBatchRunner,
)
from takealot_ops.dashboard.refresh import run_dashboard_refresh
from takealot_ops.container_selection import load_container_selection_payload
from takealot_ops.domain import sast_date
from takealot_ops.exchange_rates import (
    CnyZarRateService,
    product_cost_conversion_payload,
)
from takealot_ops.profitability import (
    empty_own_store_profitability,
    load_own_store_profitability as load_own_store_profitability_payload,
)
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
from takealot_ops.erp.anomaly_products import (
    AnomalyProductPayloadCache,
    load_cached_anomaly_product_payload,
    merge_return_anomaly_items,
    merge_return_coverage,
    merge_review_anomaly_items,
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
    is_connected_store_plid,
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
from takealot_ops.erp.read_cache import ReadProjectionCache
from takealot_ops.erp.return_removal import (
    REMOVAL_SNAPSHOT_PROVIDER,
    attach_removal_lifecycles,
    project_removal_orders,
    removal_snapshot_warnings,
    removal_tracking_status,
    summarize_removal_lifecycles,
)
from takealot_ops.erp.returns import (
    filter_return_rows,
    load_offer_returned_30_day_counter,
    load_return_collection_status,
    load_store_return_rows,
    return_filter_options,
    summarize_return_rows,
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
    load_product_detail_dataset,
    load_product_list_dataset,
    load_quadrant_dataset,
    sqlite_database_path,
)
from takealot_ops.erp.store_overview import (
    load_shared_overseas_inventory,
    load_store_inventory_projections,
    load_store_metric_projections,
    load_store_sales_metric_states,
    load_store_sales_reconciliations,
    load_store_traffic_series,
)
from takealot_ops.logistics import LogisticsLinkError, LogisticsOverviewService
from takealot_ops.logistics.snapshots import load_provider_snapshot
from takealot_ops.metrics.service import DashboardDataset
from takealot_ops.platform_warehouse import (
    PlatformWarehouseConflictError,
    PlatformWarehouseInputError,
    PlatformWarehouseNotFoundError,
    PlatformWarehouseService,
    PortalAuthenticationError,
    PortalDisabledError,
    PortalError,
)
from takealot_ops.product_master import (
    enrich_product_master_records,
    load_company_inventory_for_plid,
)
from takealot_ops.nft102_portal import (
    generate_nft102_from_baseline,
    inspect_nft102_upload,
    persist_nft102_baseline,
)
from takealot_ops.reporting import generate_daily_reports
from takealot_ops.scheduler import verify_database_integrity
from takealot_ops.search_ranking import (
    DecisionParameterChoice,
    DecisionParameterConfirmation,
    ProductFactConfirmation,
    ProductFactInput,
    ProductFactRevocation,
    SearchRankingBatchConflictError,
    SearchRankingBatchController,
    SearchRankingBatchInputError,
    SearchRankingBatchPermissionError,
    SearchRankingConfigurationError,
    SearchRankingInputError,
    SearchRankingProviderError,
    SearchRankingService,
)
from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.migrations import create_engine_for_settings, create_schema
from takealot_ops.storage.models import (
    CollectionRun,
    CompetitorListingOperation,
    CompetitorListingOperationItem,
    CompetitorPersonalWatchlist,
    CompetitorSnapshot,
    CompetitorTarget,
    CompetitorTargetAudit,
    DailyProductMetric,
    ErpStore,
    ErpUser,
    ErpUserStore,
    OwnStorePersonalWatchlist,
    PersonalWatchlistLibrary,
    PersonalWatchlistLibraryItem,
    PersonalWatchlistLibraryShare,
    PersonalWatchlistPreference,
    SalesRevenueRevision,
)
from takealot_ops.storage.repository import is_closed_day_sales_revision
from takealot_ops.storage.store_context import (
    STORE_CODE_HEADER,
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


class CompetitorBatchStopRequest(BaseModel):
    """Stop the currently visible shared batch without relying on a page checkpoint."""

    batch_id: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    reason: str = Field(default="已由 kxx 手动停止采集", max_length=500)


class CompetitorBatchResumeRequest(BaseModel):
    """Resume one manually stopped server-owned scheduled batch checkpoint."""

    batch_id: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class ScheduledCompetitorTriggerRequest(BaseModel):
    """Optional date written by the local Windows trigger command."""

    requested_for: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )


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


class CompetitorListingPreviewRequest(BaseModel):
    """One human-reviewed seller/category filter and selection request."""

    source_type: Literal["seller", "category"]
    url: str = Field(min_length=1, max_length=2000)
    price_min: int | None = Field(default=None, ge=0, le=10_000_000)
    price_max: int | None = Field(default=None, ge=0, le=10_000_000)
    sorts: list[str] = Field(default_factory=list, max_length=5)
    product_limit: int | None = Field(
        default=None,
        ge=1,
        le=MAX_LISTING_PRODUCTS,
    )


class CompetitorListingCommitRequest(BaseModel):
    """Commit one server-frozen listing preview after human confirmation."""

    preview_token: str = Field(min_length=20, max_length=200)
    library_id: int = Field(ge=1)
    product_limit: int | None = Field(
        default=None,
        ge=1,
        le=MAX_LISTING_PRODUCTS,
    )


class PersonalWatchlistLibraryRequest(BaseModel):
    """Create or rename one current-account watchlist type library."""

    name: str = Field(min_length=1, max_length=40)


class PersonalWatchlistSettingsRequest(BaseModel):
    """Persist the selected default library, including explicit no-library."""

    default_library_id: int | None = Field(default=None, ge=1)


class PersonalWatchlistLibraryAssignmentsRequest(BaseModel):
    """Replace one personal watchlist card's current-account library membership."""

    library_ids: list[int] = Field(default_factory=list, max_length=100)


class PersonalWatchlistLibraryShareInput(BaseModel):
    """One recipient and their library-content permission."""

    user_id: int = Field(ge=1)
    permission: Literal["read", "edit"]


class PersonalWatchlistLibrarySharesRequest(BaseModel):
    """Replace all recipients for one library owned by the current account."""

    shares: list[PersonalWatchlistLibraryShareInput] = Field(
        default_factory=list,
        max_length=500,
    )


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


class ProductFactItemRequest(BaseModel):
    fact_type: Literal[
        "product_type",
        "construction",
        "material",
        "function",
        "packaging",
        "usage",
    ]
    fact_term: str = Field(
        min_length=2,
        max_length=100,
        pattern=r"^[A-Za-z0-9]+(?: [A-Za-z0-9]+){0,5}$",
    )
    statement: str = Field(default="", max_length=500)


class ProductFactConfirmRequest(BaseModel):
    source_analysis_id: int = Field(gt=0)
    reason_code: str = Field(min_length=2, max_length=240)
    facts: list[ProductFactItemRequest] = Field(min_length=1, max_length=6)
    confirmed: Literal[True]
    acknowledged_fact_accuracy: Literal[True]
    acknowledged_ranking_revalidation: Literal[True]


class ProductFactRevokeRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=500)


class DecisionParameterChoiceRequest(BaseModel):
    parameter_key: str = Field(min_length=1, max_length=100)
    is_decision_parameter: bool


class DecisionParameterConfirmRequest(BaseModel):
    choices: list[DecisionParameterChoiceRequest] = Field(max_length=12)
    confirmed_current_title: Literal[True]
    acknowledged_search_validation: Literal[True]
    acknowledged_no_ranking_guarantee: Literal[True]


class SearchRankingBatchStartRequest(BaseModel):
    snapshot_id: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    confirmed_paid_model_calls: Literal[True]
    confirmed_public_takealot_requests: Literal[True]
    confirmed_strict_serial_no_retry: Literal[True]


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
        min_link_delay_seconds: float = 2.0,
        max_link_delay_seconds: float = 5.0,
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


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
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


def _empty_store_metric_projection() -> dict[str, Any]:
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


def _empty_store_sales_reconciliation(store: StoreIdentity) -> dict[str, Any]:
    return {
        "store_code": store.code,
        "store_name": store.display_name,
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


def _responsible_users_by_store(
    engine: Engine,
    stores: Sequence[StoreIdentity],
) -> dict[str, list[dict[str, Any]]]:
    """Return active users explicitly assigned to operate each requested store."""
    result: dict[str, list[dict[str, Any]]] = {
        store.code: [] for store in stores
    }
    if not stores:
        return result
    store_by_id = {store.id: store.code for store in stores}
    with Session(engine) as session:
        users = session.scalars(
            select(ErpUser)
            .where(ErpUser.active.is_(True))
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
        user_store_ids = assigned_by_user.get(user.id, set())
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
    role_priority = {"operator": 0, "admin": 1, "viewer": 2, "selection": 3}
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


def _sales_reconciliation_summary(
    items: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    failed = [item for item in items if item.get("period_end_status") == "failed"]
    pending = [item for item in items if item.get("status") == "pending"]
    recovered = [item for item in items if item.get("status") == "recovered"]
    verified = [item for item in items if item.get("status") == "verified"]
    unverified = [item for item in items if item.get("status") == "unverified"]
    business_dates = [
        str(item["period_end_business_date"])
        for item in items
        if item.get("period_end_business_date")
    ]
    verified_times = [
        str(item["latest_sales_verified_at"])
        for item in items
        if item.get("latest_sales_verified_at")
    ]
    revision_times = [
        str(item["latest_revision_at"])
        for item in items
        if item.get("latest_revision_at")
    ]
    return {
        "period_end_business_date": max(business_dates) if business_dates else None,
        "failed_store_count": len(failed),
        "pending_store_count": len(pending),
        "recovered_store_count": len(recovered),
        "verified_store_count": len(verified),
        "unverified_store_count": len(unverified),
        "revision_count": sum(int(item.get("revision_count") or 0) for item in items),
        "latest_sales_verified_at": max(verified_times) if verified_times else None,
        "latest_revision_at": max(revision_times) if revision_times else None,
        "stores": [dict(item) for item in items],
    }


def _aggregate_store_revenue_series(
    store_series: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    store_states: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    reconciliation_by_store: Mapping[str, Mapping[str, Any]] | None = None,
    start_date: date | None = None,
    completed_through: date | None = None,
    limit: int | None = 30,
) -> list[dict[str, Any]]:
    """Combine completed SAST days without zero-filling or smearing store alerts."""
    store_count = len(store_series)
    values_by_store: dict[str, dict[str, float | None]] = {}
    metric_dates: set[str] = set()
    for store_code, series in store_series.items():
        values: dict[str, float | None] = {}
        for row in series:
            metric_date = str(row.get("metric_date") or "").strip()
            if not metric_date:
                continue
            raw_revenue = row.get("ordered_revenue")
            try:
                revenue = None if raw_revenue is None else float(raw_revenue)
            except (TypeError, ValueError):
                revenue = None
            values[metric_date] = revenue
            metric_dates.add(metric_date)
        values_by_store[store_code] = values

    completed_through_text = completed_through.isoformat() if completed_through else None
    start_date_text = start_date.isoformat() if start_date else None
    selected_dates = sorted(
        metric_date
        for metric_date in metric_dates
        if (start_date_text is None or metric_date >= start_date_text)
        and (completed_through_text is None or metric_date <= completed_through_text)
    )
    if limit is not None:
        selected_dates = selected_dates[-limit:]
    result: list[dict[str, Any]] = []
    for metric_date in selected_dates:
        revenues: list[float] = []
        source_verified_store_count = 0
        pending_reconciliation_store_count = 0
        unverified_source_store_count = 0
        revised_store_count = 0
        revision_count = 0
        latest_revision_values: list[str] = []
        latest_verified_values: list[str] = []
        for store_code, values in values_by_store.items():
            revenue = values.get(metric_date)
            if revenue is not None:
                revenues.append(revenue)
            state = (store_states or {}).get(store_code, {}).get(metric_date)
            if not isinstance(state, Mapping):
                unverified_source_store_count += 1
            else:
                if state.get("source_kind") == "takealot_sales_api" and state.get(
                    "verified_at"
                ):
                    source_verified_store_count += 1
                    latest_verified_values.append(str(state["verified_at"]))
                state_revision_count = int(state.get("revision_count") or 0)
                if state_revision_count:
                    revised_store_count += 1
                    revision_count += state_revision_count
                if state.get("latest_revision_at"):
                    latest_revision_values.append(str(state["latest_revision_at"]))
            reconciliation = (reconciliation_by_store or {}).get(store_code)
            if (
                isinstance(reconciliation, Mapping)
                and reconciliation.get("status") == "pending"
                and str(reconciliation.get("period_end_business_date") or "")
                == metric_date
            ):
                pending_reconciliation_store_count += 1
        covered_store_count = len(revenues)
        if pending_reconciliation_store_count or unverified_source_store_count:
            data_status = "pending"
        elif revision_count:
            data_status = "revised"
        else:
            data_status = "verified"
        result.append(
            {
                "metric_date": metric_date,
                "total_ordered_revenue": (
                    round(sum(revenues), 2)
                    if covered_store_count > 0
                    else None
                ),
                "covered_store_count": covered_store_count,
                "store_count": store_count,
                "missing_store_count": store_count - covered_store_count,
                "data_status": data_status,
                "source_verified_store_count": source_verified_store_count,
                "pending_reconciliation_store_count": pending_reconciliation_store_count,
                "unverified_source_store_count": unverified_source_store_count,
                "revised_store_count": revised_store_count,
                "revision_count": revision_count,
                "latest_sales_verified_at": (
                    max(latest_verified_values) if latest_verified_values else None
                ),
                "latest_revision_at": (
                    max(latest_revision_values) if latest_revision_values else None
                ),
            }
        )
    return result


def _sales_revenue_revision_rows(
    engine: Engine,
    *,
    stores: Sequence[StoreIdentity],
    start_date: date | None,
    end_date: date | None,
) -> list[dict[str, Any]]:
    """Return post-close revisions for the caller's visible connected stores."""
    output: list[dict[str, Any]] = []
    for store in stores:
        with store_scope(store.code), Session(engine) as session:
            statement = select(SalesRevenueRevision)
            if start_date is not None:
                statement = statement.where(
                    SalesRevenueRevision.metric_date >= start_date
                )
            if end_date is not None:
                statement = statement.where(SalesRevenueRevision.metric_date <= end_date)
            rows = session.scalars(
                statement.order_by(
                    SalesRevenueRevision.detected_at.desc(),
                    SalesRevenueRevision.id.desc(),
                )
            ).all()
        rows = [row for row in rows if is_closed_day_sales_revision(row)]
        output.extend(
            {
                "id": row.id,
                "store_code": store.code,
                "store_name": store.display_name,
                "metric_date": row.metric_date.isoformat(),
                "change_type": row.change_type,
                "before_ordered_units": row.before_ordered_units,
                "after_ordered_units": row.after_ordered_units,
                "before_ordered_revenue": _optional_float(
                    row.before_ordered_revenue
                ),
                "after_ordered_revenue": _optional_float(row.after_ordered_revenue),
                "revenue_delta": _optional_float(row.revenue_delta),
                "units_delta": row.units_delta,
                "before_source": dict(row.before_source or {}),
                "after_source": dict(row.after_source or {}),
                "source_run_id": row.source_run_id,
                "detected_at": row.detected_at.isoformat(),
            }
            for row in rows
        )
    output.sort(key=lambda item: (str(item["detected_at"]), int(item["id"])), reverse=True)
    return output


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

    sales_reconciliation = item.get("sales_reconciliation")
    if (
        isinstance(sales_reconciliation, Mapping)
        and sales_reconciliation.get("status") == "pending"
    ):
        data_reasons.append("周期末失败后销售额尚待新的 Sales API 成功批次核验")

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
    collection_stop_lock = asyncio.Lock()
    competitor_public_client = _SharedCompetitorPublicClient(max_uses=25)
    database_url = DashboardSettings.from_env(root).database_url
    read_engine = create_read_only_erp_engine(database_url)
    read_projection_cache = ReadProjectionCache(ttl_seconds=20.0, max_entries=48)
    collection_registry = CollectionBatchRegistry(
        None
        if database_url.startswith("sqlite")
        else root / "logs" / "competitor-batch-queue.json"
    )
    listing_preview_registry = CompetitorListingPreviewRegistry()
    refresh_coordinator = RefreshCoordinator(root)
    product_thumbnails = ProductThumbnailCache(root)
    cny_zar_rates = CnyZarRateService(
        cache_path=root / "data" / "runtime-cache" / "exchange-rates" / "cny-zar.json"
    )
    anomaly_product_cache = AnomalyProductPayloadCache()
    logistics_overview = LogisticsOverviewService(root)
    platform_warehouse = PlatformWarehouseService(root)
    search_ranking = SearchRankingService(root)
    search_ranking_lock = asyncio.Lock()
    search_ranking_batch = SearchRankingBatchController(
        root,
        service=search_ranking,
        analysis_lock=search_ranking_lock,
    )
    scheduled_competitor_runner: ScheduledCompetitorBatchRunner | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if scheduled_competitor_runner is not None:
            scheduled_competitor_runner.start()
        try:
            yield
        finally:
            await search_ranking_batch.close()
            if scheduled_competitor_runner is not None:
                await scheduled_competitor_runner.close()
            await competitor_public_client.close()
            refresh_coordinator.close()
            product_thumbnails.close()
            cny_zar_rates.close()
            auth.close()
            read_engine.dispose()

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
    app.state.cny_zar_rate_service = cny_zar_rates
    app.state.anomaly_product_cache = anomaly_product_cache
    app.state.search_ranking_service = search_ranking
    app.state.search_ranking_batch_controller = search_ranking_batch
    app.state.read_engine = read_engine
    app.state.read_projection_cache = read_projection_cache

    @app.middleware("http")
    async def cache_fingerprinted_frontend_assets(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        if request.method not in {"GET", "HEAD"} or response.status_code != 200:
            return response
        path = request.url.path
        if path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path in {"/", "/index.html"}:
            response.headers["Cache-Control"] = "no-cache"
        return response

    def _control_search_ranking_batch(
        request: Request,
        action: Literal["pause", "resume", "retry_failed", "stop"],
    ) -> dict[str, Any]:
        controller: SearchRankingBatchController = (
            request.app.state.search_ranking_batch_controller
        )
        user = request.state.erp_user
        try:
            getattr(controller, action)(
                actor_username=user.username,
                actor_is_admin=user.role == "admin",
            )
        except SearchRankingBatchPermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except SearchRankingBatchConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "batch": controller.status_payload(
                user.accessible_stores,
                actor_username=user.username,
                actor_is_admin=user.role == "admin",
            )
        }

    def require_competitor_batch_controller(request: Request) -> None:
        user = request.state.erp_user
        if user.username.casefold() != "kxx":
            raise HTTPException(
                status_code=403,
                detail="竞品批次的开始、继续和停止仅限 kxx 账号",
            )

    def require_full_refresh_controller(request: Request) -> None:
        user = request.state.erp_user
        if user.username.casefold() != "kxx":
            raise HTTPException(
                status_code=403,
                detail="刷新全部店铺数据仅限 kxx 账号",
            )

    def require_competitor_admin(request: Request) -> None:
        user = request.state.erp_user
        if user.role != "admin":
            raise HTTPException(
                status_code=403,
                detail=(
                    "全局竞品清单、插队、审计和失效复核仅限管理员；"
                    "当前账号仍可新增链接并管理自己的个人监控池"
                ),
            )

    @app.middleware("http")
    async def invalidate_read_cache_after_mutation(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and response.status_code < 400
        ):
            read_projection_cache.clear()
        return response

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
            "/api/internal/competitors/scheduled-trigger",
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
        requested_store_value = request.headers.get(
            STORE_CODE_HEADER
        ) or request.query_params.get("store_code")
        requested_store_code = (requested_store_value or "current").strip().casefold()
        accessible_store = next(
            (
                store
                for store in session.user.accessible_stores
                if store.code == requested_store_code
            ),
            None,
        )
        if (
            accessible_store is None
            and requested_store_value is None
            and path == "/api/erp/product-thumbnail"
        ):
            # An <img> request cannot attach the API client's X-Store-Code header.
            # Keep cached/older frontends working for accounts that are assigned only
            # non-"current" stores; the endpoint is still permission-gated and can
            # fetch only whitelisted public Takealot cover-image URLs.
            accessible_store = next(iter(session.user.accessible_stores), None)
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
        path = sqlite_database_path(database_url)
        if path is not None and not path.exists():
            return {"last_collection_at": None, "latest_metric_date": None}
        try:
            with Session(read_engine) as session:
                last_collection = session.scalar(
                    select(func.max(CollectionRun.finished_at)).where(
                        CollectionRun.status == "success",
                        CollectionRun.run_type.in_(("offers", "sales")),
                    )
                )
                latest_metric = session.scalar(select(func.max(DailyProductMetric.metric_date)))
        except SQLAlchemyError:
            return {"last_collection_at": None, "latest_metric_date": None}
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
        start_date: date | None = Query(default=None),
    ) -> dict[str, Any]:
        if start_date is not None and start_date > as_of:
            raise HTTPException(status_code=422, detail="开始日期不能晚于截止日期")
        settings = DashboardSettings.from_env(root)
        store = request.state.erp_store
        cache_key = (
            "store-summary-v2",
            store.code,
            as_of.isoformat(),
            start_date.isoformat() if start_date else None,
        )

        def load_projection() -> dict[str, Any]:
            dataset = load_erp_dataset(settings, as_of, engine=read_engine)
            payload = (
                build_summary_payload(dataset, as_of, start_date=start_date)
                if start_date is not None
                else build_summary_payload(dataset, as_of)
            )
            try:
                payload["traffic_series"] = (
                    period_end_traffic_series(
                        read_engine,
                        as_of=as_of,
                        days=(as_of - start_date).days + 1,
                    )
                    if start_date is not None
                    else period_end_traffic_series(read_engine, as_of=as_of)
                )
            except SQLAlchemyError:
                payload["traffic_series"] = []
            try:
                payload["operators"] = _responsible_users_by_store(
                    read_engine,
                    (store,),
                ).get(store.code, [])
            except SQLAlchemyError:
                payload["operators"] = []
            with Session(read_engine) as session:
                payload["top_products"] = enrich_product_master_records(
                    session,
                    payload.get("top_products", []),
                    as_of_date=as_of,
                )
            return payload

        return read_projection_cache.get_or_load(cache_key, load_projection)

    @app.get("/api/erp/summary/stores")
    def store_summaries(
        request: Request,
        as_of: date = Query(default_factory=date.today),
        start_date: date | None = Query(default=None),
        selected_store_scope: Literal["all", "operating"] = Query(
            default="all",
            alias="store_scope",
        ),
    ) -> dict[str, Any]:
        """Return a compact comparison for the authorized multi-store scope."""
        if start_date is not None and start_date > as_of:
            raise HTTPException(status_code=422, detail="开始日期不能晚于截止日期")
        stores = _multi_store_identities_for_request(request, selected_store_scope)
        store_codes = tuple(store.code for store in stores)
        cache_key = (
            "store-summaries-v2",
            store_codes,
            as_of.isoformat(),
            start_date.isoformat() if start_date is not None else None,
        )

        def load_projection() -> dict[str, Any]:
            completed_sales_through = min(
                as_of,
                sast_date(datetime.now(UTC)) - timedelta(days=1),
            )
            try:
                operators_by_store = _responsible_users_by_store(read_engine, stores)
            except SQLAlchemyError:
                operators_by_store = {store.code: [] for store in stores}
            try:
                metrics_by_store = load_store_metric_projections(
                    read_engine,
                    store_codes,
                    as_of=as_of,
                    start_date=start_date,
                )
            except SQLAlchemyError:
                metrics_by_store = {
                    code: _empty_store_metric_projection() for code in store_codes
                }
            try:
                traffic_by_store = load_store_traffic_series(
                    read_engine,
                    store_codes,
                    as_of=as_of,
                    days=(as_of - start_date).days + 1 if start_date else 30,
                )
            except SQLAlchemyError:
                traffic_by_store = {code: [] for code in store_codes}
            try:
                inventory_by_store = load_store_inventory_projections(
                    read_engine,
                    store_codes,
                )
            except SQLAlchemyError:
                inventory_by_store = {
                    code: _empty_store_inventory() for code in store_codes
                }
            try:
                revenue_states_by_store = load_store_sales_metric_states(
                    read_engine,
                    store_codes,
                    as_of=as_of,
                    start_date=start_date,
                )
            except SQLAlchemyError:
                revenue_states_by_store = {code: {} for code in store_codes}
            try:
                sales_reconciliation_by_store = load_store_sales_reconciliations(
                    read_engine,
                    {store.code: store.display_name for store in stores},
                    as_of=as_of,
                )
            except SQLAlchemyError:
                sales_reconciliation_by_store = {
                    store.code: _empty_store_sales_reconciliation(store)
                    for store in stores
                }

            items: list[dict[str, Any]] = []
            revenue_series_by_store: dict[str, Sequence[Mapping[str, Any]]] = {}
            for store in stores:
                payload = metrics_by_store.get(
                    store.code,
                    _empty_store_metric_projection(),
                )
                traffic_series = traffic_by_store.get(store.code, [])
                inventory = inventory_by_store.get(
                    store.code,
                    _empty_store_inventory(),
                )
                sales_reconciliation = sales_reconciliation_by_store.get(
                    store.code,
                    _empty_store_sales_reconciliation(store),
                )
                revenue_series_by_store[store.code] = payload.get("sales_series", [])
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
                    "sales_reconciliation": sales_reconciliation,
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
                overseas_inventory = load_shared_overseas_inventory(
                    read_engine,
                    store_codes,
                )
            except SQLAlchemyError:
                overseas_inventory = _empty_overseas_inventory()
            return {
                "as_of": as_of.isoformat(),
                "range_start": (
                    start_date.isoformat()
                    if start_date is not None
                    else (as_of - timedelta(days=29)).isoformat()
                ),
                "range_end": as_of.isoformat(),
                "store_count": len(items),
                "health_summary": _health_rollup(items),
                "sales_revenue_series": _aggregate_store_revenue_series(
                    revenue_series_by_store,
                    store_states=revenue_states_by_store,
                    reconciliation_by_store=sales_reconciliation_by_store,
                    start_date=start_date,
                    completed_through=completed_sales_through,
                    limit=None if start_date is not None else 30,
                ),
                "sales_revenue_completed_through": completed_sales_through.isoformat(),
                "sales_reconciliation": _sales_reconciliation_summary(
                    tuple(sales_reconciliation_by_store.values())
                ),
                "logistics": {
                    "overseas_warehouse": overseas_inventory,
                    "platform_warehouse": _aggregate_platform_inventory(items),
                },
                "stores": items,
            }

        return read_projection_cache.get_or_load(cache_key, load_projection)

    @app.get("/api/erp/summary/stores/sales-revisions")
    def store_sales_revisions(
        request: Request,
        start_date: date | None = Query(default=None),
        end_date: date | None = Query(default=None),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        selected_store_scope: Literal["all", "operating"] = Query(
            default="all",
            alias="store_scope",
        ),
    ) -> dict[str, Any]:
        """Return immutable post-close revenue revisions for the chosen scope."""
        if start_date is not None and end_date is not None and start_date > end_date:
            raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
        stores = _multi_store_identities_for_request(request, selected_store_scope)
        cache_key = (
            "sales-revisions-v2",
            tuple(store.code for store in stores),
            start_date.isoformat() if start_date else None,
            end_date.isoformat() if end_date else None,
        )
        rows = read_projection_cache.get_or_load(
            cache_key,
            lambda: _sales_revenue_revision_rows(
                read_engine,
                stores=stores,
                start_date=start_date,
                end_date=end_date,
            ),
        )
        offset = (page - 1) * page_size
        return {
            "items": rows[offset : offset + page_size],
            "total": len(rows),
            "page": page,
            "page_size": page_size,
            "start_date": start_date.isoformat() if start_date is not None else None,
            "end_date": end_date.isoformat() if end_date is not None else None,
            "source_policy": {
                "before": "南非业务日结束后最近一次成功 Sales API 验证的正式基线",
                "after": "正式基线建立后再次改变该业务日金额或件数的成功 Sales API 批次",
                "immutable": True,
            },
        }

    @app.get("/api/erp/products")
    def products(
        request: Request,
        as_of: date = Query(default_factory=date.today),
        selected_store_scope: Literal["current", "all", "operating"] = Query(
            default="current",
            alias="store_scope",
        ),
    ) -> dict[str, Any]:
        settings = DashboardSettings.from_env(root)
        stores = _read_store_identities_for_request(request, selected_store_scope)
        cache_key = (
            "products-v2",
            tuple(store.code for store in stores),
            as_of.isoformat(),
        )

        def load_projection() -> dict[str, Any]:
            items: list[dict[str, Any]] = []
            metric_dates: dict[str, str | None] = {}
            for store in stores:
                with store_scope(store.code):
                    payload = build_products_payload(
                        load_product_list_dataset(
                            settings,
                            as_of,
                            engine=read_engine,
                        ),
                        as_of,
                    )
                metric_dates[store.code] = payload.get("latest_metric_date")
                items.extend(_tag_store_records(payload.get("items", []), store))
            items = _product_master_records(
                root,
                items,
                as_of_date=as_of,
                engine=read_engine,
            )
            items.sort(
                key=lambda item: (
                    -float(item.get("ordered_units") or 0),
                    -float(item.get("page_views_30_days") or 0),
                    str(item.get("store_name") or "").casefold(),
                    str(item.get("title") or "").casefold(),
                )
            )
            latest_dates = [value for value in metric_dates.values() if value]
            return {
                "latest_metric_date": max(latest_dates) if latest_dates else None,
                "store_scope": selected_store_scope,
                "store_count": len(stores),
                "store_metric_dates": metric_dates,
                "items": items,
            }

        return read_projection_cache.get_or_load(cache_key, load_projection)

    @app.get("/api/erp/products/{offer_id}")
    def product_detail(
        offer_id: str,
        request: Request,
        as_of: date = Query(default_factory=date.today),
    ) -> dict[str, Any]:
        settings = DashboardSettings.from_env(root)
        store = request.state.erp_store
        cache_key = (
            "product-detail-v2",
            store.code,
            offer_id,
            as_of.isoformat(),
        )

        def load_projection() -> dict[str, Any]:
            payload = build_product_detail_payload(
                load_product_detail_dataset(
                    settings,
                    as_of,
                    offer_id,
                    engine=read_engine,
                ),
                as_of,
                offer_id,
            )
            identity = payload.get("identity")
            enriched = _product_master_records(
                root,
                [identity] if isinstance(identity, Mapping) else [],
                as_of_date=as_of,
                engine=read_engine,
            )
            payload["identity"] = (
                _tag_store_record(enriched[0], store) if enriched else {}
            )
            enriched_identity = payload["identity"]
            payload["cost_conversion"] = product_cost_conversion_payload(
                enriched_identity.get("cost_rmb")
                if isinstance(enriched_identity, Mapping)
                else None,
                request.app.state.cny_zar_rate_service,
            )
            payload["history"] = _tag_store_records(
                payload.get("history", []),
                store,
            )
            return payload

        return read_projection_cache.get_or_load(cache_key, load_projection)

    @app.get("/api/erp/returns")
    def seller_returns(
        request: Request,
        start_date: date | None = Query(default=None),
        end_date: date | None = Query(default=None),
        selected_store_scope: Literal["current", "all", "operating"] = Query(
            default="current",
            alias="store_scope",
        ),
        query: str | None = Query(default=None, max_length=200),
        reason: str | None = Query(default=None, max_length=100),
        outcome: str | None = Query(default=None, max_length=100),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        """Return authorized, filterable seller-return detail across store scopes."""
        selected_end = end_date or datetime.now(ZoneInfo("Africa/Johannesburg")).date()
        selected_start = start_date or selected_end - timedelta(days=29)
        if selected_start > selected_end:
            raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
        stores = _read_store_identities_for_request(request, selected_store_scope)
        cache_key = (
            "seller-returns-v4-full-removal-orders",
            tuple(store.code for store in stores),
            selected_start.isoformat(),
            selected_end.isoformat(),
        )

        def load_projection() -> dict[str, Any]:
            all_rows: list[dict[str, Any]] = []
            statuses: list[dict[str, Any]] = []
            counters: list[dict[str, Any]] = []
            removal_statuses: list[dict[str, Any]] = []
            all_removal_orders: list[dict[str, Any]] = []
            removal_warnings: list[str] = []
            with store_scope("current"):
                w8_snapshot = load_provider_snapshot(read_engine, "w8")
            raw_w8_payload = (
                w8_snapshot.get("payload")
                if isinstance(w8_snapshot, Mapping)
                else None
            )
            w8_payload: Mapping[str, Any] = (
                raw_w8_payload if isinstance(raw_w8_payload, Mapping) else {}
            )
            w8_return_orders = (
                w8_payload.get("return_orders", [])
                if isinstance(w8_payload, Mapping)
                else []
            )
            if not isinstance(w8_return_orders, list):
                w8_return_orders = []
            for store in stores:
                with store_scope(store.code):
                    removal_snapshot = load_provider_snapshot(
                        read_engine,
                        REMOVAL_SNAPSHOT_PROVIDER,
                    )
                    with Session(read_engine) as session:
                        store_rows = enrich_product_master_records(
                            session,
                            load_store_return_rows(
                                session,
                                start_date=selected_start,
                                end_date=selected_end,
                            ),
                            as_of_date=selected_end,
                        )
                        status = load_return_collection_status(
                            session,
                            start_date=selected_start,
                            end_date=selected_end,
                        )
                        counter = load_offer_returned_30_day_counter(session)
                removal_payload = (
                    removal_snapshot.get("payload")
                    if isinstance(removal_snapshot, Mapping)
                    and isinstance(removal_snapshot.get("payload"), Mapping)
                    else None
                )
                store_rows = attach_removal_lifecycles(
                    store_rows,
                    removal_snapshot=removal_payload,
                    w8_return_orders=w8_return_orders,
                    today=datetime.now(ZoneInfo("Africa/Johannesburg")).date(),
                )
                all_removal_orders.extend(
                    project_removal_orders(
                        store.code,
                        store.display_name,
                        removal_snapshot,
                        w8_return_orders=w8_return_orders,
                        today=datetime.now(ZoneInfo("Africa/Johannesburg")).date(),
                    )
                )
                removal_warnings.extend(removal_snapshot_warnings(removal_snapshot))
                all_rows.extend(
                    _tag_store_records(
                        store_rows,
                        store,
                        identity_fields=("seller_return_id",),
                    )
                )
                statuses.append({**status, "store_code": store.code, "store_name": store.display_name})
                counters.append({**counter, "store_code": store.code, "store_name": store.display_name})
                removal_statuses.append(
                    removal_tracking_status(
                        store.code,
                        store.display_name,
                        removal_snapshot,
                    )
                )
            return {
                "rows": all_rows,
                "statuses": statuses,
                "counters": counters,
                "removal_statuses": removal_statuses,
                "removal_orders": all_removal_orders,
                "removal_warnings": list(dict.fromkeys(removal_warnings)),
                "w8_tracking": {
                    "data_status": (
                        "synced"
                        if isinstance(w8_snapshot, Mapping)
                        and "return_orders" in w8_payload
                        else "uncollected"
                    ),
                    "synced_at": (
                        w8_snapshot.get("fetched_at")
                        if isinstance(w8_snapshot, Mapping)
                        else None
                    ),
                    "return_order_count": len(w8_return_orders),
                    "message": (
                        "已读取本地长睿退货快照"
                        if isinstance(w8_snapshot, Mapping)
                        and "return_orders" in w8_payload
                        else "现有长睿快照尚未包含退货处置明细，请在物流同步后再核对"
                    ),
                },
            }

        projection = read_projection_cache.get_or_load(cache_key, load_projection)
        all_rows = projection["rows"]
        statuses = projection["statuses"]
        counters = projection["counters"]
        removal_statuses = projection["removal_statuses"]
        removal_orders = projection["removal_orders"]
        removal_data_status = (
            "synced"
            if removal_statuses
            and all(item.get("data_status") == "synced" for item in removal_statuses)
            else "partial"
            if any(item.get("data_status") == "synced" for item in removal_statuses)
            else "uncollected"
        )

        options = return_filter_options(all_rows)
        filtered = filter_return_rows(
            all_rows,
            query=query,
            reason=reason,
            outcome=outcome,
        )
        offset = (page - 1) * page_size
        summary = summarize_return_rows(filtered)
        summary["removal_lifecycle"] = summarize_removal_lifecycles(filtered)
        return {
            "range_start": selected_start.isoformat(),
            "range_end": selected_end.isoformat(),
            "date_basis": "Africa/Johannesburg",
            "store_scope": selected_store_scope,
            "store_count": len(stores),
            "data_status": _combined_return_data_status(statuses),
            "store_statuses": statuses,
            "offer_returned_30_days": _aggregate_offer_return_counter(counters),
            "summary": summary,
            "removal_order_tracking": {
                "data_status": removal_data_status,
                "store_statuses": removal_statuses,
                "w8": projection["w8_tracking"],
            },
            "removal_orders": {
                "data_status": removal_data_status,
                "counts": {
                    "total": len(removal_orders),
                    **{
                        stage: sum(
                            1 for item in removal_orders if item.get("stage") == stage
                        )
                        for stage in ("submitted", "pickup_ready", "closed")
                    },
                },
                "items": removal_orders,
                "warnings": projection["removal_warnings"],
                "source_notice": (
                    "Submitted、Ready For Pickup、Closed 及其商品明细来自本地 "
                    "Seller Portal Manage Removal Orders 快照；列表不受退货日期和筛选影响。"
                    "长睿结果只在 PO reference + SKU 双重一致时显示；长睿已上架不是 "
                    "Takealot 重新上架。"
                ),
            },
            "filters": options,
            "items": filtered[offset : offset + page_size],
            "total": len(filtered),
            "page": page,
            "page_size": page_size,
            "source_notice": (
                "退货原因、客户备注、处理结果与交易来自 Seller API /returns；"
                "完整 PO 模块、到期、预约与提货数量来自本地 Seller Portal 移除单快照；"
                "到仓、上架与报损只在长睿 PO + SKU 双重一致时展示。"
                "Offer 的 quantity_returned_30_days 是独立滚动30天计数，不替代退货明细。"
            ),
        }

    @app.post("/api/erp/returns/removal-orders/sync")
    def sync_seller_return_removal_orders(request: Request) -> dict[str, Any]:
        """Explicitly refresh read-only Seller Portal removal-order snapshots."""
        _require_platform_warehouse_loopback(request)
        require_full_refresh_controller(request)
        try:
            return platform_warehouse.sync_return_removal_orders()
        except PortalAuthenticationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (PortalDisabledError, PortalError) as exc:
            _raise_platform_warehouse_portal_error(exc)

    @app.post("/api/erp/returns/removal-orders/verify-otp")
    def verify_seller_return_removal_order_otp(
        payload: PlatformWarehousePortalOtpRequest,
        request: Request,
    ) -> dict[str, Any]:
        """Verify pending OTP and continue the same read-only removal-order sync."""
        _require_platform_warehouse_loopback(request)
        require_full_refresh_controller(request)
        try:
            return platform_warehouse.verify_otp_and_sync_return_removal_orders(payload.otp)
        except PortalAuthenticationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (PortalDisabledError, PortalError) as exc:
            _raise_platform_warehouse_portal_error(exc)

    @app.get("/api/erp/keyword-traffic")
    def keyword_traffic_products(
        request: Request,
        as_of: date = Query(default_factory=date.today),
        selected_store_scope: Literal["current", "all", "operating"] = Query(
            default="current",
            alias="store_scope",
        ),
    ) -> dict[str, Any]:
        stores = _read_store_identities_for_request(request, selected_store_scope)
        cache_key = (
            "keyword-traffic-list-v2",
            tuple(store.code for store in stores),
            as_of.isoformat(),
        )

        def load_projection() -> dict[str, Any]:
            items: list[dict[str, Any]] = []
            summary = {
                "product_count": 0,
                "with_traffic_count": 0,
                "archived_product_count": 0,
                "keyword_change_count": 0,
            }
            for store in stores:
                with store_scope(store.code), Session(read_engine) as session:
                    payload = build_keyword_product_list(session, as_of=as_of)
                    store_items = enrich_product_master_records(
                        session,
                        payload.get("items", []),
                        as_of_date=as_of,
                    )
                items.extend(_tag_store_records(store_items, store))
                payload_summary = payload.get("summary", {})
                for key in summary:
                    summary[key] += int(payload_summary.get(key) or 0)
            return {
                "as_of": as_of.isoformat(),
                "store_scope": selected_store_scope,
                "store_count": len(stores),
                "items": items,
                "summary": summary,
            }

        return read_projection_cache.get_or_load(cache_key, load_projection)

    @app.get("/api/erp/keyword-traffic/{offer_id}")
    def keyword_traffic_product_detail(
        offer_id: str,
        request: Request,
        as_of: date = Query(default_factory=date.today),
        history_days: int = Query(90, ge=30, le=365),
        comparison_days: int = Query(7, ge=3, le=30),
    ) -> dict[str, Any]:
        store = request.state.erp_store
        cache_key = (
            "keyword-traffic-detail-v2",
            store.code,
            offer_id,
            as_of.isoformat(),
            history_days,
            comparison_days,
        )

        def load_projection() -> dict[str, Any] | None:
            with Session(read_engine) as session:
                payload = build_keyword_product_detail(
                    session,
                    offer_id=offer_id,
                    as_of=as_of,
                    history_days=history_days,
                    comparison_days=comparison_days,
                )
                if payload is not None:
                    product = payload.get("product")
                    enriched = enrich_product_master_records(
                        session,
                        [product] if isinstance(product, Mapping) else [],
                        as_of_date=as_of,
                    )
                    payload["product"] = (
                        _tag_store_record(enriched[0], store)
                        if enriched
                        else {}
                    )
            return payload

        payload = read_projection_cache.get_or_load(cache_key, load_projection)
        if payload is None:
            raise HTTPException(status_code=404, detail="没有找到对应的店铺商品")
        return payload

    @app.get("/api/erp/search-ranking")
    def search_ranking_products(
        request: Request,
        selected_store_scope: Literal["current", "all", "operating"] = Query(
            default="current",
            alias="store_scope",
        ),
    ) -> dict[str, Any]:
        service: SearchRankingService = request.app.state.search_ranking_service
        stores = _read_store_identities_for_request(request, selected_store_scope)
        status: Mapping[str, Any] | None = None
        items: list[dict[str, Any]] = []
        excluded_reasons: defaultdict[str, int] = defaultdict(int)
        eligibility: dict[str, Any] = {
            "source": "authenticated_store_seller_offers",
            "rule": "current_offer_and_buyable_and_positive_available_stock_and_fresh",
            "current_offer_count": 0,
            "eligible_count": 0,
            "excluded_count": 0,
            "excluded_reasons": excluded_reasons,
            "latest_capture_at": None,
            "max_age_hours": 0,
        }
        for store in stores:
            with store_scope(store.code):
                payload = service.list_payload()
            if status is None:
                status = payload.get("status", {})
            store_eligibility = payload.get("eligibility", {})
            for key in ("current_offer_count", "eligible_count", "excluded_count"):
                eligibility[key] += int(store_eligibility.get(key) or 0)
            eligibility["max_age_hours"] = max(
                float(eligibility["max_age_hours"] or 0),
                float(store_eligibility.get("max_age_hours") or 0),
            )
            captured_at = store_eligibility.get("latest_capture_at")
            if captured_at and (
                eligibility["latest_capture_at"] is None
                or str(captured_at) > str(eligibility["latest_capture_at"])
            ):
                eligibility["latest_capture_at"] = captured_at
            for reason, count in dict(
                store_eligibility.get("excluded_reasons") or {}
            ).items():
                excluded_reasons[str(reason)] += int(count or 0)
            items.extend(_tag_store_records(payload.get("items", []), store))
        if status is None:
            status = service.list_payload().get("status", {})
        eligibility["excluded_reasons"] = dict(excluded_reasons)
        return {
            "status": status,
            "eligibility": eligibility,
            "store_scope": selected_store_scope,
            "store_count": len(stores),
            "items": _product_master_records(root, items),
        }

    @app.get("/api/erp/search-ranking/root-expansion-library")
    def search_ranking_root_expansion_library(
        request: Request,
        search: str = Query(default="", max_length=100),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> dict[str, Any]:
        service: SearchRankingService = request.app.state.search_ranking_service
        return service.root_expansion_library_payload(search=search, limit=limit)

    @app.get("/api/erp/search-ranking/autocomplete-library")
    def search_ranking_autocomplete_library_compatibility(
        request: Request,
        search: str = Query(default="", max_length=100),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> dict[str, Any]:
        service: SearchRankingService = request.app.state.search_ranking_service
        return service.root_expansion_library_payload(search=search, limit=limit)

    @app.get("/api/erp/search-ranking/batch")
    def search_ranking_batch_preview(request: Request) -> dict[str, Any]:
        controller: SearchRankingBatchController = (
            request.app.state.search_ranking_batch_controller
        )
        user = request.state.erp_user
        try:
            return controller.preview_payload(
                user.accessible_stores,
                actor_username=user.username,
                actor_is_admin=user.role == "admin",
            )
        except SearchRankingBatchInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/erp/search-ranking/batch/status")
    def search_ranking_batch_status(request: Request) -> dict[str, Any]:
        controller: SearchRankingBatchController = (
            request.app.state.search_ranking_batch_controller
        )
        user = request.state.erp_user
        return {
            "batch": controller.status_payload(
                user.accessible_stores,
                actor_username=user.username,
                actor_is_admin=user.role == "admin",
            )
        }

    @app.post("/api/erp/search-ranking/batch/start")
    async def start_search_ranking_batch(
        payload: SearchRankingBatchStartRequest,
        request: Request,
    ) -> dict[str, Any]:
        controller: SearchRankingBatchController = (
            request.app.state.search_ranking_batch_controller
        )
        user = request.state.erp_user
        try:
            batch = controller.start(
                user.accessible_stores,
                actor_username=user.username,
                actor_display_name=user.display_name or user.username,
                actor_is_admin=user.role == "admin",
                snapshot_id=payload.snapshot_id,
            )
        except SearchRankingBatchInputError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SearchRankingBatchConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"batch": batch}

    @app.post("/api/erp/search-ranking/batch/pause")
    async def pause_search_ranking_batch(request: Request) -> dict[str, Any]:
        return _control_search_ranking_batch(request, "pause")

    @app.post("/api/erp/search-ranking/batch/resume")
    async def resume_search_ranking_batch(request: Request) -> dict[str, Any]:
        return _control_search_ranking_batch(request, "resume")

    @app.post("/api/erp/search-ranking/batch/retry-failed")
    async def retry_failed_search_ranking_batch(request: Request) -> dict[str, Any]:
        return _control_search_ranking_batch(request, "retry_failed")

    @app.post("/api/erp/search-ranking/batch/stop")
    async def stop_search_ranking_batch(request: Request) -> dict[str, Any]:
        return _control_search_ranking_batch(request, "stop")

    @app.post("/api/erp/search-ranking/batch/restart")
    async def restart_search_ranking_batch(
        payload: SearchRankingBatchStartRequest,
        request: Request,
    ) -> dict[str, Any]:
        controller: SearchRankingBatchController = (
            request.app.state.search_ranking_batch_controller
        )
        user = request.state.erp_user
        try:
            batch = controller.restart(
                user.accessible_stores,
                actor_username=user.username,
                actor_display_name=user.display_name or user.username,
                actor_is_admin=user.role == "admin",
                snapshot_id=payload.snapshot_id,
            )
        except SearchRankingBatchPermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (SearchRankingBatchInputError, SearchRankingBatchConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"batch": batch}

    @app.get("/api/erp/search-ranking/{offer_id}")
    def search_ranking_product_detail(
        offer_id: str,
        request: Request,
    ) -> dict[str, Any]:
        service: SearchRankingService = request.app.state.search_ranking_service
        payload = service.detail_payload(offer_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="没有找到对应的店铺商品")
        return _decorate_search_ranking_detail(
            root,
            payload,
            request.state.erp_store,
        )

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
                payload = await service.analyze_offer(offer_id)
                return _decorate_search_ranking_detail(
                    root,
                    payload,
                    request.state.erp_store,
                )
        except SearchRankingInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SearchRankingConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (SearchRankingProviderError, CompetitorNetworkError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/erp/search-ranking/{offer_id}/product-facts/confirm")
    async def confirm_search_ranking_product_facts(
        offer_id: str,
        payload: ProductFactConfirmRequest,
        request: Request,
    ) -> dict[str, Any]:
        if search_ranking_lock.locked():
            raise HTTPException(
                status_code=409,
                detail="另一个搜索定位任务正在运行；商品事实尚未写入，请稍后重试",
            )
        service: SearchRankingService = request.app.state.search_ranking_service
        user = request.state.erp_user
        confirmation = ProductFactConfirmation(
            source_analysis_id=payload.source_analysis_id,
            reason_code=payload.reason_code,
            actor_username=user.username,
            actor_display_name=user.display_name or user.username,
            facts=tuple(
                ProductFactInput(
                    fact_type=item.fact_type,
                    fact_term=item.fact_term,
                    statement=item.statement,
                )
                for item in payload.facts
            ),
        )
        try:
            async with search_ranking_lock:
                result = await service.confirm_product_facts(
                    offer_id,
                    confirmation,
                )
                return _decorate_search_ranking_detail(
                    root,
                    result,
                    request.state.erp_store,
                )
        except SearchRankingInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SearchRankingConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (SearchRankingProviderError, CompetitorNetworkError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/erp/search-ranking/{offer_id}/decision-parameters/confirm")
    async def confirm_search_ranking_decision_parameters(
        offer_id: str,
        payload: DecisionParameterConfirmRequest,
        request: Request,
    ) -> dict[str, Any]:
        if search_ranking_lock.locked():
            raise HTTPException(
                status_code=409,
                detail="搜索定位任务正在运行；决策参数尚未写入，请稍后再确认",
            )
        service: SearchRankingService = request.app.state.search_ranking_service
        user = request.state.erp_user
        try:
            async with search_ranking_lock:
                result = service.confirm_decision_parameters(
                    offer_id,
                    DecisionParameterConfirmation(
                        actor_username=user.username,
                        actor_display_name=user.display_name or user.username,
                        choices=tuple(
                            DecisionParameterChoice(
                                parameter_key=item.parameter_key,
                                is_decision_parameter=item.is_decision_parameter,
                            )
                            for item in payload.choices
                        ),
                    ),
                )
                return _decorate_search_ranking_detail(
                    root,
                    result,
                    request.state.erp_store,
                )
        except SearchRankingInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/erp/search-ranking/{offer_id}/product-facts/{fact_id}/revoke")
    def revoke_search_ranking_product_fact(
        offer_id: str,
        fact_id: int,
        payload: ProductFactRevokeRequest,
        request: Request,
    ) -> dict[str, Any]:
        service: SearchRankingService = request.app.state.search_ranking_service
        user = request.state.erp_user
        try:
            result = service.revoke_product_fact(
                offer_id,
                fact_id,
                ProductFactRevocation(
                    actor_username=user.username,
                    actor_display_name=user.display_name or user.username,
                    reason=payload.reason,
                ),
            )
            return _decorate_search_ranking_detail(
                root,
                result,
                request.state.erp_store,
            )
        except SearchRankingInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/erp/quadrants")
    def quadrants(
        request: Request,
        as_of: date = Query(default_factory=date.today),
        percentile: int = Query(50),
        selected_store_scope: Literal["current", "all", "operating"] = Query(
            default="current",
            alias="store_scope",
        ),
    ) -> dict[str, Any]:
        if percentile not in {25, 50, 75}:
            raise HTTPException(status_code=422, detail="分位数只能是25、50或75")
        settings = DashboardSettings.from_env(root)
        stores = _read_store_identities_for_request(request, selected_store_scope)
        cache_key = (
            "quadrants-v2",
            selected_store_scope,
            tuple(store.code for store in stores),
            as_of.isoformat(),
            percentile,
        )

        def load_projection() -> dict[str, Any]:
            dataset, offer_scope = _combined_store_dataset(
                settings,
                as_of,
                stores,
                dataset_loader=lambda configured_settings, requested_as_of: (
                    load_quadrant_dataset(
                        configured_settings,
                        requested_as_of,
                        engine=read_engine,
                    )
                ),
            )
            payload = build_quadrant_payload(dataset, as_of, percentile)
            scoped_items: list[dict[str, Any]] = []
            for item in payload.get("items", []):
                record = dict(item)
                synthetic_offer_id = str(record.get("offer_id") or "")
                identity = offer_scope.get(synthetic_offer_id)
                if identity is not None:
                    store, original_offer_id = identity
                    record["offer_id"] = original_offer_id
                    record = _tag_store_record(record, store)
                scoped_items.append(record)
            payload["items"] = _product_master_records(
                root,
                scoped_items,
                as_of_date=as_of,
                engine=read_engine,
            )
            payload["store_scope"] = selected_store_scope
            payload["store_count"] = len(stores)
            return payload

        return read_projection_cache.get_or_load(cache_key, load_projection)

    @app.get("/api/erp/container-selection")
    def container_selection(
        request: Request,
        as_of: date = Query(default_factory=date.today),
    ) -> dict[str, Any]:
        """Return the all-authorized-store container-fill decision workbench."""
        store_codes = _own_store_codes_for_request(request, "all")
        cache_key = (
            "container-selection-v1",
            tuple(sorted(store_codes)),
            as_of.isoformat(),
        )
        return read_projection_cache.get_or_load(
            cache_key,
            lambda: load_container_selection_payload(
                root,
                read_engine,
                store_codes=store_codes,
                as_of=as_of,
            ),
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

    @app.get("/api/erp/anomaly-products")
    def anomaly_products(
        request: Request,
        as_of: date = Query(default_factory=date.today),
        selected_store_scope: Literal["current", "all", "operating"] = Query(
            default="current",
            alias="store_scope",
        ),
    ) -> dict[str, Any]:
        """Return the new separated anomaly workspace from local evidence only."""

        stores = _read_store_identities_for_request(request, selected_store_scope)
        completed_through = min(
            as_of,
            sast_date(datetime.now(UTC)) - timedelta(days=1),
        )
        store_payloads: list[tuple[StoreIdentity, dict[str, Any]]] = []
        for store in stores:
            with store_scope(store.code), Session(read_engine) as session:
                store_payload = load_cached_anomaly_product_payload(
                    session,
                    cache=anomaly_product_cache,
                    store_code=store.code,
                    requested_as_of=as_of,
                    completed_through=completed_through,
                )
            store_payloads.append(
                (
                    store,
                    store_payload,
                )
            )
        return _aggregate_anomaly_payloads(
            root,
            store_payloads,
            requested_as_of=as_of,
            completed_through=completed_through,
            selected_store_scope=selected_store_scope,
        )

    @app.get("/api/erp/logistics")
    def logistics(
        request: Request,
        refresh: bool = Query(False),
        selected_store_scope: Literal["current", "all", "operating"] = Query(
            default="current",
            alias="store_scope",
        ),
    ) -> dict[str, Any]:
        """Return a cached, sanitized, read-only W8 and Takealot shipment overview."""
        stores = _read_store_identities_for_request(request, selected_store_scope)
        if refresh and selected_store_scope != "current":
            raise HTTPException(
                status_code=422,
                detail="全部店铺查看只读取各店本地快照；请切换到明确单店后再手动同步",
            )
        payloads: list[tuple[StoreIdentity, dict[str, Any]]] = []
        for store in stores:
            with store_scope(store.code):
                payloads.append((store, logistics_overview.load(force=refresh)))
        return _aggregate_logistics_payloads(payloads, selected_store_scope)

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
    def platform_warehouse_overview(
        request: Request,
        selected_store_scope: Literal["current", "all", "operating"] = Query(
            default="current",
            alias="store_scope",
        ),
    ) -> dict[str, Any]:
        """Return guarded drafts plus the latest read-only Takealot shipment snapshot."""
        stores = _read_store_identities_for_request(request, selected_store_scope)
        payloads: list[tuple[StoreIdentity, dict[str, Any]]] = []
        for store in stores:
            with store_scope(store.code):
                payload = platform_warehouse.load()
            payload["offers"] = _product_master_records(
                root,
                payload.get("offers", []),
            )
            payloads.append((store, payload))
        return _aggregate_platform_warehouse_payloads(
            payloads,
            selected_store_scope,
        )

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
        require_full_refresh_controller(request)
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
            result = run_dashboard_refresh(root, all_stores=True)
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
        return daily_report_payload(
            read_engine,
            business_date,
            capture_start=capture_start,
            capture_end=capture_end,
        )

    @app.get("/api/erp/daily-report/events")
    def operations_daily_report_events(request: Request) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            async for event in daily_report_event_stream(
                read_engine,
                is_disconnected=request.is_disconnected,
                business_date=_default_operations_business_date,
            ):
                yield event

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
        return reminder_payload(read_engine)

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
        unresolved = unresolved_locations(read_engine, through)
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
        own_store_scope: Literal["current", "all", "operating"] = Query(default="current"),
        include_own_store: bool = Query(default=True),
    ) -> dict[str, object]:
        own_store_codes = _own_store_codes_for_request(request, own_store_scope)
        cache_key = (
            "competitors-list-v8",
            tuple(sorted(own_store_codes)),
            start_date.isoformat() if start_date else None,
            end_date.isoformat() if end_date else None,
            include_own_store,
        )

        def load_projection() -> dict[str, object]:
            dataset = _load_competitor_dataset(
                root,
                start_date=start_date,
                end_date=end_date,
                own_store_codes=own_store_codes,
                include_detail_frames=False,
                include_store_projection=include_own_store,
                engine=read_engine,
            )
            return {
                "items": _competitor_card_category_records(
                    frame_records(dataset.current),
                    dataset.category_paths,
                ),
                "store_items": _competitor_card_category_records(
                    _own_store_sales_comparison_records(
                        root,
                        _product_master_competitor_store_records(
                            root,
                            frame_records(dataset.store_current),
                            engine=read_engine,
                        ),
                        own_store_codes=own_store_codes,
                        through=datetime.now(ZoneInfo("Asia/Shanghai")).date(),
                        engine=read_engine,
                    ),
                    dataset.category_paths,
                ),
                "own_follower_events": dataset.own_follower_events,
                "date_range": dataset.date_range_payload(),
            }

        return read_projection_cache.get_or_load(cache_key, load_projection)

    @app.get("/api/competitors/own-store")
    def own_store_competitors(
        request: Request,
        start_date: date | None = Query(default=None),
        end_date: date | None = Query(default=None),
        plid: str | None = Query(default=None, min_length=1, max_length=30),
        own_store_scope: Literal["current", "all", "operating"] = Query(
            default="current"
        ),
    ) -> dict[str, object]:
        """Return only the scope-dependent private-store radar partition."""
        own_store_codes = _own_store_codes_for_request(request, own_store_scope)
        cache_key = (
            "competitors-own-store-v6",
            tuple(sorted(own_store_codes)),
            start_date.isoformat() if start_date else None,
            end_date.isoformat() if end_date else None,
            plid,
        )

        def load_projection() -> dict[str, object]:
            dataset = _load_competitor_dataset(
                root,
                start_date=start_date,
                end_date=end_date,
                own_store_codes=own_store_codes,
                plids={plid} if plid else None,
                include_detail_frames=False,
                own_store_only=True,
                engine=read_engine,
            )
            return {
                "store_items": _competitor_card_category_records(
                    _own_store_sales_comparison_records(
                        root,
                        _product_master_competitor_store_records(
                            root,
                            frame_records(dataset.store_current),
                            engine=read_engine,
                        ),
                        own_store_codes=own_store_codes,
                        through=datetime.now(ZoneInfo("Asia/Shanghai")).date(),
                        engine=read_engine,
                    ),
                    dataset.category_paths,
                ),
                "date_range": dataset.date_range_payload(),
            }

        return read_projection_cache.get_or_load(cache_key, load_projection)

    @app.get("/api/competitors/link-health")
    def competitor_link_health(
        request: Request,
    ) -> dict[str, list[dict[str, Any]]]:
        require_competitor_admin(request)
        return {"items": load_competitor_link_health(read_engine)}

    def _competitor_batch_status_payload(
        *,
        include_details: bool = False,
        result_page: int = 1,
        error_page: int = 1,
        terminal_error_page: int = 1,
        page_size: int = 50,
    ) -> dict[str, object]:
        status = collection_registry.status(
            include_details=include_details,
            result_offset=(result_page - 1) * page_size,
            error_offset=(error_page - 1) * page_size,
            terminal_error_offset=(terminal_error_page - 1) * page_size,
            detail_limit=page_size if include_details else None,
        )
        runner_status = (
            scheduled_competitor_runner.status()
            if scheduled_competitor_runner is not None
            else {}
        )
        stopped_checkpoint = (
            scheduled_competitor_runner.stopped_checkpoint_status(
                include_details=include_details,
                result_offset=(result_page - 1) * page_size,
                error_offset=(error_page - 1) * page_size,
                terminal_error_offset=(terminal_error_page - 1) * page_size,
                detail_limit=page_size if include_details else None,
            )
            if scheduled_competitor_runner is not None
            else None
        )
        if not status.get("active") and stopped_checkpoint is not None:
            status.update(stopped_checkpoint)
        resume_available = bool(
            not status.get("active")
            and status.get("source") == "scheduled"
            and status.get("event") in {"manual_stop", "completed"}
            and status.get("batch_id") == runner_status.get("batch_id")
            and runner_status.get("resume_available")
        )
        resumable_pending = runner_status.get("resumable_pending")
        status["scheduled_resume_available"] = resume_available
        status["scheduled_resume_pending"] = (
            resumable_pending
            if resume_available and isinstance(resumable_pending, int)
            else 0
        )
        network_resume_available = bool(
            status.get("active")
            and status.get("source") == "scheduled"
            and status.get("event") == "scheduled_pause"
            and status.get("batch_id") == runner_status.get("batch_id")
            and runner_status.get("wait_kind") == "network"
            and runner_status.get("network_resume_available")
        )
        network_resume_pending = runner_status.get("network_resume_pending")
        status["scheduled_network_resume_available"] = network_resume_available
        status["scheduled_network_resume_pending"] = (
            network_resume_pending
            if network_resume_available and isinstance(network_resume_pending, int)
            else 0
        )
        status["scheduled_wait_kind"] = (
            runner_status.get("wait_kind")
            if status.get("active") and status.get("source") == "scheduled"
            else None
        )
        status["scheduled_auto_resume_at"] = (
            runner_status.get("resume_after")
            if status.get("active") and status.get("source") == "scheduled"
            else None
        )
        status["scheduled_retry_round"] = runner_status.get(
            "pending_retry_round",
            0,
        )
        status["scheduled_retry_round_limit"] = runner_status.get(
            "pending_retry_round_limit",
            0,
        )
        status["result_page"] = result_page
        status["error_page"] = error_page
        status["terminal_error_page"] = terminal_error_page
        status["detail_page_size"] = page_size
        return status

    @app.get("/api/competitors/batch-status")
    def competitor_batch_status(
        include_details: bool = Query(default=False),
        result_page: int = Query(default=1, ge=1),
        error_page: int = Query(default=1, ge=1),
        terminal_error_page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=10, le=100),
    ) -> dict[str, object]:
        """Return lightweight progress by default and bounded task detail on demand."""
        return _competitor_batch_status_payload(
            include_details=include_details,
            result_page=result_page,
            error_page=error_page,
            terminal_error_page=terminal_error_page,
            page_size=page_size,
        )

    @app.get("/api/competitors/collection-logs")
    def competitor_collection_logs(
        request: Request,
        batch_id: str | None = Query(default=None, min_length=1, max_length=128),
    ) -> dict[str, object]:
        """Return one structured read-only summary per competitor collection round."""
        require_competitor_admin(request)
        status = _competitor_batch_status_payload()
        current_batch_id = status.get("batch_id")
        try:
            return read_collection_round_summaries(
                root,
                current_batch_id=(
                    current_batch_id if isinstance(current_batch_id, str) else None
                ),
                selected_batch_id=batch_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/competitors/batch-options")
    def update_competitor_batch_options(
        payload: CompetitorBatchOptionsRequest,
        request: Request,
    ) -> dict[str, object]:
        require_competitor_batch_controller(request)
        user = request.state.erp_user
        try:
            collection_registry.update_options(
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
        return {"ok": True, "status": _competitor_batch_status_payload()}

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
        return {
            "ok": True,
            "ready": ready,
            "status": _competitor_batch_status_payload(),
        }

    @app.get("/api/competitors/targets")
    def competitor_targets() -> dict[str, list[dict[str, object]]]:
        with Session(read_engine) as session:
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
                statement.order_by(
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

    @app.get("/api/competitors/personal-watchlist")
    def competitor_personal_watchlist(
        request: Request,
    ) -> dict[str, object]:
        """Return personal memberships plus owned and explicitly shared libraries."""
        user = request.state.erp_user
        accessible_store_codes = tuple(
            sorted(
                store.code
                for store in user.accessible_stores
                if store.active and store.data_connected
            )
        )
        with Session(read_engine) as session:
            return _personal_watchlist_payload(
                session,
                user_id=user.id,
                accessible_store_codes=set(accessible_store_codes),
            )

    @app.get("/api/competitors/personal-watchlist/overview")
    def competitor_personal_watchlist_overview(
        request: Request,
        start_date: date | None = Query(default=None),
        end_date: date | None = Query(default=None),
    ) -> dict[str, object]:
        """Hydrate personal-pool cards across every store authorized to the account."""

        user = request.state.erp_user
        own_store_codes = _own_store_codes_for_request(request, "all")
        cache_key = (
            "competitor-personal-overview-v7",
            user.id,
            tuple(sorted(own_store_codes)),
            start_date.isoformat() if start_date else None,
            end_date.isoformat() if end_date else None,
        )

        def load_projection() -> dict[str, object]:
            with Session(read_engine) as session:
                plids = _personal_watchlist_projection_plids(
                    session,
                    user_id=user.id,
                )
            dataset = _load_competitor_dataset(
                root,
                start_date=start_date,
                end_date=end_date,
                own_store_codes=own_store_codes,
                plids=plids,
                include_detail_frames=False,
                engine=read_engine,
            )
            return {
                "items": _competitor_card_category_records(
                    frame_records(dataset.current),
                    dataset.category_paths,
                ),
                "store_items": _competitor_card_category_records(
                    _own_store_sales_comparison_records(
                        root,
                        _product_master_competitor_store_records(
                            root,
                            frame_records(dataset.store_current),
                            engine=read_engine,
                        ),
                        own_store_codes=own_store_codes,
                        through=datetime.now(ZoneInfo("Asia/Shanghai")).date(),
                        engine=read_engine,
                    ),
                    dataset.category_paths,
                ),
                "own_follower_events": [],
                "date_range": dataset.date_range_payload(),
            }

        return read_projection_cache.get_or_load(cache_key, load_projection)

    @app.get("/api/competitors/personal-watchlist/share-users")
    def personal_watchlist_share_users(
        request: Request,
    ) -> dict[str, object]:
        """Return the minimal account directory needed to choose share recipients."""
        user = request.state.erp_user
        with Session(read_engine) as session:
            accounts = session.scalars(
                select(ErpUser)
                .where(ErpUser.id != user.id)
                .order_by(
                    ErpUser.active.desc(),
                    ErpUser.display_name.asc(),
                    ErpUser.username.asc(),
                    ErpUser.id.asc(),
                )
            ).all()
        return {
            "items": [
                {
                    "id": account.id,
                    "username": account.username,
                    "display_name": account.display_name,
                    "active": account.active,
                }
                for account in accounts
            ]
        }

    @app.post("/api/competitors/personal-watchlist/libraries")
    def create_personal_watchlist_library(
        payload: PersonalWatchlistLibraryRequest,
        request: Request,
    ) -> dict[str, object]:
        user = request.state.erp_user
        name = _validated_personal_watchlist_library_name(payload.name)
        settings = DashboardSettings.from_env(root)
        engine = create_engine_for_settings(settings)
        try:
            create_schema(engine)
            with Session(engine) as session:
                existing_names = session.scalars(
                    select(PersonalWatchlistLibrary.name).where(
                        PersonalWatchlistLibrary.user_id == user.id
                    )
                ).all()
                if any(existing.casefold() == name.casefold() for existing in existing_names):
                    raise HTTPException(status_code=409, detail=f"类型库“{name}”已存在")
                now = datetime.now(UTC)
                library = PersonalWatchlistLibrary(
                    user_id=user.id,
                    name=name,
                    created_at=now,
                    updated_at=now,
                )
                session.add(library)
                session.flush()
                result = _single_personal_watchlist_library_payload(
                    session,
                    library=library,
                    viewer_user_id=user.id,
                )
                session.commit()
        finally:
            engine.dispose()
        competitor_logger.info(
            "personal_watchlist_library action=create id=%s user=%s",
            result["id"],
            user.username,
        )
        return {"library": result}

    @app.patch("/api/competitors/personal-watchlist/libraries/{library_id}")
    def rename_personal_watchlist_library(
        library_id: int,
        payload: PersonalWatchlistLibraryRequest,
        request: Request,
    ) -> dict[str, object]:
        user = request.state.erp_user
        name = _validated_personal_watchlist_library_name(payload.name)
        settings = DashboardSettings.from_env(root)
        engine = create_engine_for_settings(settings)
        try:
            create_schema(engine)
            with Session(engine) as session:
                library = session.get(PersonalWatchlistLibrary, library_id)
                if library is None or library.user_id != user.id:
                    raise HTTPException(status_code=404, detail="未找到当前账号的类型库")
                siblings = session.scalars(
                    select(PersonalWatchlistLibrary).where(
                        PersonalWatchlistLibrary.user_id == user.id,
                        PersonalWatchlistLibrary.id != library_id,
                    )
                ).all()
                if any(item.name.casefold() == name.casefold() for item in siblings):
                    raise HTTPException(status_code=409, detail=f"类型库“{name}”已存在")
                library.name = name
                library.updated_at = datetime.now(UTC)
                session.flush()
                result = _single_personal_watchlist_library_payload(
                    session,
                    library=library,
                    viewer_user_id=user.id,
                )
                session.commit()
        finally:
            engine.dispose()
        competitor_logger.info(
            "personal_watchlist_library action=rename id=%s user=%s",
            library_id,
            user.username,
        )
        return {"library": result}

    @app.put("/api/competitors/personal-watchlist/libraries/{library_id}/shares")
    def update_personal_watchlist_library_shares(
        library_id: int,
        payload: PersonalWatchlistLibrarySharesRequest,
        request: Request,
    ) -> dict[str, object]:
        """Replace per-user share grants; only the library creator may do this."""
        user = request.state.erp_user
        recipient_ids = [share.user_id for share in payload.shares]
        if len(recipient_ids) != len(set(recipient_ids)):
            raise HTTPException(status_code=422, detail="同一系统用户不能重复指定分享权限")
        if user.id in recipient_ids:
            raise HTTPException(status_code=422, detail="创建者无需把类型库分享给自己")
        settings = DashboardSettings.from_env(root)
        engine = create_engine_for_settings(settings)
        try:
            create_schema(engine)
            with Session(engine) as session:
                library = session.get(PersonalWatchlistLibrary, library_id)
                if library is None or library.user_id != user.id:
                    raise HTTPException(status_code=404, detail="未找到当前账号创建的类型库")
                recipients = (
                    session.scalars(
                        select(ErpUser).where(ErpUser.id.in_(recipient_ids))
                    ).all()
                    if recipient_ids
                    else []
                )
                found_recipient_ids = {recipient.id for recipient in recipients}
                missing_recipient_ids = sorted(set(recipient_ids) - found_recipient_ids)
                if missing_recipient_ids:
                    missing = "、".join(str(item) for item in missing_recipient_ids)
                    raise HTTPException(
                        status_code=404,
                        detail=f"系统用户不存在：{missing}",
                    )
                existing_rows = session.scalars(
                    select(PersonalWatchlistLibraryShare).where(
                        PersonalWatchlistLibraryShare.library_id == library.id
                    )
                ).all()
                existing_by_user_id = {row.user_id: row for row in existing_rows}
                selected_by_user_id = {share.user_id: share for share in payload.shares}
                lost_default_editor_ids = {
                    recipient_id
                    for recipient_id, existing_row in existing_by_user_id.items()
                    if existing_row.permission == "edit"
                    and (
                        recipient_id not in selected_by_user_id
                        or selected_by_user_id[recipient_id].permission != "edit"
                    )
                }
                for recipient_id, existing_row in existing_by_user_id.items():
                    if recipient_id not in selected_by_user_id:
                        session.delete(existing_row)
                now = datetime.now(UTC)
                for recipient_id, requested_share in selected_by_user_id.items():
                    share_row = existing_by_user_id.get(recipient_id)
                    if share_row is None:
                        session.add(
                            PersonalWatchlistLibraryShare(
                                library_id=library.id,
                                user_id=recipient_id,
                                permission=requested_share.permission,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                    else:
                        share_row.permission = requested_share.permission
                        share_row.updated_at = now
                if lost_default_editor_ids:
                    invalid_preferences = session.scalars(
                        select(PersonalWatchlistPreference).where(
                            PersonalWatchlistPreference.user_id.in_(
                                lost_default_editor_ids
                            ),
                            PersonalWatchlistPreference.default_library_id == library.id,
                        )
                    ).all()
                    for preference in invalid_preferences:
                        preference.default_library_id = None
                        preference.default_configured = False
                        preference.updated_at = now
                library.updated_at = now
                session.flush()
                result = _single_personal_watchlist_library_payload(
                    session,
                    library=library,
                    viewer_user_id=user.id,
                )
                session.commit()
        finally:
            engine.dispose()
        competitor_logger.info(
            "personal_watchlist_library action=share id=%s user=%s recipients=%s",
            library_id,
            user.username,
            len(recipient_ids),
        )
        return {"library": result}

    @app.delete(
        "/api/competitors/personal-watchlist/libraries/{library_id}/items/{plid}"
    )
    def delete_personal_watchlist_library_item(
        library_id: int,
        plid: str,
        request: Request,
    ) -> dict[str, object]:
        """Remove one card from a library for its owner or an edit recipient."""
        normalized_plid = _validated_competitor_plid(plid)
        user = request.state.erp_user
        settings = DashboardSettings.from_env(root)
        engine = create_engine_for_settings(settings)
        removed = False
        try:
            create_schema(engine)
            with Session(engine) as session:
                library = session.get(PersonalWatchlistLibrary, library_id)
                if library is None:
                    raise HTTPException(status_code=404, detail="类型库不存在")
                access = _personal_watchlist_library_access(
                    session,
                    library=library,
                    user_id=user.id,
                )
                if access is None:
                    raise HTTPException(status_code=404, detail="当前账号无权访问该类型库")
                if access == "read":
                    raise HTTPException(status_code=403, detail="该类型库为只读分享，不能移除商品")
                assignment = session.get(
                    PersonalWatchlistLibraryItem,
                    (library.id, normalized_plid),
                )
                if assignment is not None:
                    session.delete(assignment)
                    library.updated_at = datetime.now(UTC)
                    removed = True
                    session.flush()
                result = _single_personal_watchlist_library_payload(
                    session,
                    library=library,
                    viewer_user_id=user.id,
                )
                session.commit()
        finally:
            engine.dispose()
        competitor_logger.info(
            "personal_watchlist_library action=remove_item id=%s plid=%s user=%s removed=%s",
            library_id,
            normalized_plid,
            user.username,
            removed,
        )
        return {"ok": True, "removed": removed, "library": result}

    @app.delete("/api/competitors/personal-watchlist/libraries/{library_id}")
    def delete_personal_watchlist_library(
        library_id: int,
        request: Request,
    ) -> dict[str, object]:
        user = request.state.erp_user
        settings = DashboardSettings.from_env(root)
        engine = create_engine_for_settings(settings)
        try:
            create_schema(engine)
            with Session(engine) as session:
                library = session.get(PersonalWatchlistLibrary, library_id)
                if library is None or library.user_id != user.id:
                    raise HTTPException(status_code=404, detail="未找到当前账号的类型库")
                affected_preferences = session.scalars(
                    select(PersonalWatchlistPreference).where(
                        PersonalWatchlistPreference.default_library_id == library_id
                    )
                ).all()
                owner_default_was_deleted = any(
                    item.user_id == user.id for item in affected_preferences
                )
                preference = next(
                    (item for item in affected_preferences if item.user_id == user.id),
                    session.get(PersonalWatchlistPreference, user.id),
                )
                default_library_configured = bool(
                    preference is not None and preference.default_configured
                )
                default_library_id = (
                    preference.default_library_id if preference is not None else None
                )
                if affected_preferences:
                    now = datetime.now(UTC)
                    for affected_preference in affected_preferences:
                        affected_preference.default_library_id = None
                        affected_preference.default_configured = False
                        affected_preference.updated_at = now
                if owner_default_was_deleted:
                    default_library_configured = False
                    default_library_id = None
                session.delete(library)
                session.commit()
        finally:
            engine.dispose()
        competitor_logger.info(
            "personal_watchlist_library action=delete id=%s user=%s",
            library_id,
            user.username,
        )
        return {
            "ok": True,
            "default_library_configured": default_library_configured,
            "default_library_id": default_library_id,
        }

    @app.put("/api/competitors/personal-watchlist/settings")
    def update_personal_watchlist_settings(
        payload: PersonalWatchlistSettingsRequest,
        request: Request,
    ) -> dict[str, object]:
        user = request.state.erp_user
        settings = DashboardSettings.from_env(root)
        engine = create_engine_for_settings(settings)
        try:
            create_schema(engine)
            with Session(engine) as session:
                if payload.default_library_id is not None:
                    library = session.get(
                        PersonalWatchlistLibrary,
                        payload.default_library_id,
                    )
                    if library is None:
                        raise HTTPException(
                            status_code=404,
                            detail="未找到当前账号可使用的默认类型库",
                        )
                    access = _personal_watchlist_library_access(
                        session,
                        library=library,
                        user_id=user.id,
                    )
                    if access is None:
                        raise HTTPException(
                            status_code=404,
                            detail="未找到当前账号可使用的默认类型库",
                        )
                    if access == "read":
                        raise HTTPException(
                            status_code=403,
                            detail="只读共享类型库不能设为新增链接默认归类",
                        )
                preference = session.get(PersonalWatchlistPreference, user.id)
                now = datetime.now(UTC)
                if preference is None:
                    preference = PersonalWatchlistPreference(
                        user_id=user.id,
                        default_configured=True,
                        default_library_id=payload.default_library_id,
                        updated_at=now,
                    )
                    session.add(preference)
                else:
                    preference.default_configured = True
                    preference.default_library_id = payload.default_library_id
                    preference.updated_at = now
                session.commit()
        finally:
            engine.dispose()
        competitor_logger.info(
            "personal_watchlist_settings action=default user=%s library_id=%s",
            user.username,
            payload.default_library_id,
        )
        return {
            "default_library_configured": True,
            "default_library_id": payload.default_library_id,
        }

    @app.put("/api/competitors/personal-watchlist/{plid}/libraries")
    def update_personal_watchlist_item_libraries(
        plid: str,
        payload: PersonalWatchlistLibraryAssignmentsRequest,
        request: Request,
    ) -> dict[str, object]:
        normalized_plid = _validated_competitor_plid(plid)
        user = request.state.erp_user
        selected_library_ids = sorted(set(payload.library_ids))
        settings = DashboardSettings.from_env(root)
        engine = create_engine_for_settings(settings)
        try:
            create_schema(engine)
            with Session(engine) as session:
                if not _personal_watchlist_membership_exists(
                    session,
                    user_id=user.id,
                    plid=normalized_plid,
                ):
                    raise HTTPException(status_code=404, detail="该商品不在你的个人监控池")
                owned_library_ids = set(
                    session.scalars(
                        select(PersonalWatchlistLibrary.id).where(
                            PersonalWatchlistLibrary.user_id == user.id
                        )
                    ).all()
                )
                share_rows = session.scalars(
                    select(PersonalWatchlistLibraryShare).where(
                        PersonalWatchlistLibraryShare.user_id == user.id
                    )
                ).all()
                access_by_library_id: dict[
                    int,
                    Literal["owner", "read", "edit"],
                ] = {library_id: "owner" for library_id in owned_library_ids}
                access_by_library_id.update(
                    {
                        share.library_id: (
                            "edit" if share.permission == "edit" else "read"
                        )
                        for share in share_rows
                        if share.library_id not in owned_library_ids
                    }
                )
                selected_ids = set(selected_library_ids)
                if not selected_ids.issubset(access_by_library_id):
                    raise HTTPException(status_code=404, detail="包含当前账号无权访问的类型库")
                accessible_ids = set(access_by_library_id)
                current_ids = set(
                    session.scalars(
                        select(PersonalWatchlistLibraryItem.library_id).where(
                            PersonalWatchlistLibraryItem.plid == normalized_plid,
                            PersonalWatchlistLibraryItem.library_id.in_(accessible_ids),
                        )
                    ).all()
                    if accessible_ids
                    else []
                )
                read_only_ids = {
                    library_id
                    for library_id, access in access_by_library_id.items()
                    if access == "read"
                }
                if (selected_ids & read_only_ids) - current_ids:
                    raise HTTPException(status_code=403, detail="只读类型库不能加入商品")
                modifiable_ids = {
                    library_id
                    for library_id, access in access_by_library_id.items()
                    if access in {"owner", "edit"}
                }
                selected_modifiable_ids = selected_ids & modifiable_ids
                for library_id in (current_ids & modifiable_ids) - selected_modifiable_ids:
                    assignment = session.get(
                        PersonalWatchlistLibraryItem,
                        (library_id, normalized_plid),
                    )
                    if assignment is not None:
                        session.delete(assignment)
                now = datetime.now(UTC)
                for library_id in selected_modifiable_ids - current_ids:
                    session.add(
                        PersonalWatchlistLibraryItem(
                            library_id=library_id,
                            plid=normalized_plid,
                            added_at=now,
                        )
                    )
                effective_library_ids = sorted(
                    selected_modifiable_ids | (current_ids & read_only_ids)
                )
                session.commit()
        finally:
            engine.dispose()
        competitor_logger.info(
            "personal_watchlist_library action=assign plid=%s user=%s library_ids=%s",
            normalized_plid,
            user.username,
            effective_library_ids,
        )
        return {"plid": normalized_plid, "library_ids": effective_library_ids}

    @app.put("/api/competitors/personal-watchlist/{plid}")
    def add_competitor_personal_watchlist_item(
        plid: str,
        request: Request,
    ) -> dict[str, object]:
        """Idempotently save one true competitor or connected-store product."""
        normalized_plid = _validated_competitor_plid(plid)
        user = request.state.erp_user
        settings = DashboardSettings.from_env(root)
        engine = create_engine_for_settings(settings)
        try:
            create_schema(engine)
            with Session(engine) as session:
                target = session.get(CompetitorTarget, normalized_plid)
                private_store_rows = _authorized_own_store_rows_for_personal_watchlist(
                    load_connected_store_offers(
                        session,
                        plids={normalized_plid},
                    ),
                    user=user,
                )
                now = datetime.now(UTC)
                item: CompetitorPersonalWatchlist | OwnStorePersonalWatchlist
                if private_store_rows:
                    item, created = _ensure_own_store_personal_watchlist_item(
                        session,
                        user_id=user.id,
                        plid=normalized_plid,
                        added_at=now,
                    )
                    source: Literal["competitor", "own_store"] = "own_store"
                elif target is not None:
                    item, created = _ensure_competitor_personal_watchlist_item(
                        session,
                        user_id=user.id,
                        plid=normalized_plid,
                        added_at=now,
                    )
                    source = "competitor"
                else:
                    raise HTTPException(
                        status_code=404,
                        detail=f"PLID{normalized_plid} 不在真正竞品或自有店铺清单中",
                    )
                _assign_default_personal_watchlist_library(
                    session,
                    user_id=user.id,
                    plid=normalized_plid,
                    added_at=now,
                    membership_created=created,
                )
                session.flush()
                result = _personal_watchlist_item_payload(
                    item,
                    source=source,
                    library_ids=_personal_watchlist_library_ids_for_plid(
                        session,
                        user_id=user.id,
                        plid=normalized_plid,
                    ),
                )
                session.commit()
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
                own_store_item = session.get(
                    OwnStorePersonalWatchlist,
                    (user.id, normalized_plid),
                )
                if own_store_item is not None:
                    session.delete(own_store_item)
                    removed = True
                _remove_personal_watchlist_library_assignments(
                    session,
                    user_id=user.id,
                    plid=normalized_plid,
                )
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

    @app.post("/api/competitors/listing-preview")
    async def preview_competitor_listing_source(
        payload: CompetitorListingPreviewRequest,
        request: Request,
    ) -> dict[str, object]:
        user = request.state.erp_user
        try:
            source = parse_competitor_listing_source(
                payload.url,
                expected_type=payload.source_type,
            )
            selected_sorts = payload.sorts or [source.default_sort]
            for sort in selected_sorts:
                build_competitor_listing_url(
                    source,
                    sort=sort,
                    price_min=payload.price_min,
                    price_max=payload.price_max,
                )
        except CompetitorListingInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        try:
            async with competitor_public_client.lease() as public_client_lease:
                await public_client_lease.client.start()
                try:
                    result = await preview_competitor_listing(
                        public_client_lease.client,
                        source_url=payload.url,
                        source_type=payload.source_type,
                        price_min=payload.price_min,
                        price_max=payload.price_max,
                        sorts=selected_sorts,
                        product_limit=payload.product_limit,
                    )
                except (CompetitorNetworkError, CompetitorListingProviderError):
                    public_client_lease.invalidate()
                    raise
        except CompetitorListingInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except CompetitorNetworkError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except CompetitorListingProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        preview_token = None
        if result["candidate_queue_frozen"]:
            preview_token = listing_preview_registry.issue(
                user_id=user.id,
                payload=result,
            )
        result_sorts = result.get("sorts")
        sort_count = len(result_sorts) if isinstance(result_sorts, list) else 0
        competitor_logger.info(
            "listing_preview source=%s total=%s selected=%s sorts=%s user=%s committable=%s",
            result["source_type"],
            result["source_total"],
            result["selected_count"],
            sort_count,
            user.username,
            result["can_commit"],
        )
        return {**result, "preview_token": preview_token}

    @app.post("/api/competitors/listing-targets")
    def create_competitor_listing_targets(
        payload: CompetitorListingCommitRequest,
        request: Request,
    ) -> dict[str, object]:
        user = request.state.erp_user
        try:
            preview = listing_preview_registry.resolve(
                token=payload.preview_token,
                user_id=user.id,
            )
            preview = finalize_competitor_listing_preview(
                preview,
                product_limit=payload.product_limit,
            )
        except CompetitorListingPreviewExpiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CompetitorListingInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raw_products = preview.get("products")
        if not isinstance(raw_products, list) or not raw_products:
            raise HTTPException(status_code=409, detail="筛选预览没有可加入的商品")

        settings = DashboardSettings.from_env(root)
        engine = create_engine_for_settings(settings)
        try:
            create_schema(engine)
            result = _persist_competitor_listing_targets(
                engine,
                preview=preview,
                user=user,
                personal_library_id=payload.library_id,
            )
        except CompetitorListingInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            engine.dispose()

        queued_count = sum(
            collection_registry.enqueue_target(plid=plid, url=url)
            for plid, url in result.new_targets
        )
        listing_preview_registry.discard(payload.preview_token)
        competitor_logger.info(
            "listing_commit source=%s selected=%s added=%s existing=%s own_store=%s "
            "library_id=%s queued=%s user=%s",
            preview.get("source_type"),
            result.selected_count,
            result.added_target_count,
            result.existing_target_count,
            result.own_store_count,
            result.personal_library_id,
            queued_count,
            user.username,
        )
        return {
            "source_type": preview.get("source_type"),
            "source_url": preview.get("source_url"),
            "operation_id": result.operation_id,
            "personal_library_id": result.personal_library_id,
            "personal_library_name": result.personal_library_name,
            "selected_count": result.selected_count,
            "added_target_count": result.added_target_count,
            "reactivated_target_count": result.reactivated_target_count,
            "existing_target_count": result.existing_target_count,
            "own_store_count": result.own_store_count,
            "personal_watchlist_added_count": result.personal_watchlist_added_count,
            "queued_to_active_batch_count": queued_count,
        }

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
                personal_item: CompetitorPersonalWatchlist | OwnStorePersonalWatchlist
                private_store_rows = _authorized_own_store_rows_for_personal_watchlist(
                    load_connected_store_offers(
                        session,
                        plids={plid},
                    ),
                    user=user,
                )
                if private_store_rows:
                    personal_item, personal_created = _ensure_own_store_personal_watchlist_item(
                        session,
                        user_id=user.id,
                        plid=plid,
                        added_at=now,
                    )
                    _assign_default_personal_watchlist_library(
                        session,
                        user_id=user.id,
                        plid=plid,
                        added_at=now,
                        membership_created=personal_created,
                    )
                    session.flush()
                    personal_payload = _personal_watchlist_item_payload(
                        personal_item,
                        source="own_store",
                        library_ids=_personal_watchlist_library_ids_for_plid(
                            session,
                            user_id=user.id,
                            plid=plid,
                        ),
                    )
                    session.commit()
                    competitor_logger.info(
                        "personal_watchlist action=auto_add_own_store plid=%s user=%s created=%s",
                        plid,
                        user.username,
                        personal_created,
                    )
                    return {
                        "item": None,
                        "queued_to_active_batch": False,
                        "automatic_store_target": True,
                        "store_names": sorted({row.store_name for row in private_store_rows}),
                        "personal_watchlist_member": True,
                        "personal_watchlist_item": personal_payload,
                    }
                target = session.get(CompetitorTarget, plid)
                if target is not None and target.active:
                    personal_item, personal_created = _ensure_competitor_personal_watchlist_item(
                        session,
                        user_id=user.id,
                        plid=plid,
                        added_at=now,
                    )
                    _assign_default_personal_watchlist_library(
                        session,
                        user_id=user.id,
                        plid=plid,
                        added_at=now,
                        membership_created=personal_created,
                    )
                    session.commit()
                    competitor_logger.info(
                        "personal_watchlist action=auto_add_existing plid=%s user=%s created=%s",
                        plid,
                        user.username,
                        personal_created,
                    )
                    if personal_created:
                        read_projection_cache.clear()
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
                personal_item, personal_created = _ensure_competitor_personal_watchlist_item(
                    session,
                    user_id=user.id,
                    plid=plid,
                    added_at=now,
                )
                _assign_default_personal_watchlist_library(
                    session,
                    user_id=user.id,
                    plid=plid,
                    added_at=now,
                    membership_created=personal_created,
                )
                session.flush()
                personal_payload = _personal_watchlist_item_payload(
                    personal_item,
                    source="competitor",
                    library_ids=_personal_watchlist_library_ids_for_plid(
                        session,
                        user_id=user.id,
                        plid=plid,
                    ),
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
            "personal_watchlist_item": personal_payload,
        }

    @app.patch("/api/competitors/targets/{plid}")
    def update_competitor_target(
        plid: str,
        payload: CompetitorTargetRequest,
        request: Request,
    ) -> dict[str, object]:
        require_competitor_admin(request)
        normalized_plid, url = _validated_competitor_target_url(payload.url)
        if normalized_plid != plid:
            raise HTTPException(
                status_code=422,
                detail="修改链接不能改变 PLID；请直接新增另一条监控链接",
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

    @app.post("/api/competitors/targets/{plid}/prioritize")
    def prioritize_competitor_target(
        plid: str,
        request: Request,
        payload: CompetitorTargetPriorityRequest | None = None,
    ) -> dict[str, object]:
        require_competitor_admin(request)
        user = request.state.erp_user
        source = payload.source if payload is not None else "manual"
        settings = DashboardSettings.from_env(root)
        is_true_competitor = False
        with Session(read_engine) as session:
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
        own_store_scope: Literal["current", "all", "operating"] = Query(default="current"),
    ) -> dict[str, object]:
        """Return private PLIDs for the selected store or authorized all-store view."""
        accessible_codes = _own_store_codes_for_request(request, "all")
        selected_codes = _own_store_codes_for_request(request, own_store_scope)
        cache_key = (
            "competitor-store-targets-v2",
            own_store_scope,
            tuple(sorted(accessible_codes)),
            tuple(sorted(selected_codes)),
        )

        def load_projection() -> dict[str, object]:
            with Session(read_engine) as session:
                rows = load_connected_store_offers(session)
            accessible_rows = [row for row in rows if row.store_code in accessible_codes]
            selected_rows = [
                row for row in accessible_rows if row.store_code in selected_codes
            ]
            grouped: dict[str, list[ConnectedStoreOffer]] = defaultdict(list)
            for row in selected_rows:
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
                for row in selected_rows
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

        return read_projection_cache.get_or_load(cache_key, load_projection)

    @app.get("/api/competitors/target-audits")
    def competitor_target_audits(
        request: Request,
        start_date: date | None = Query(default=None),
        end_date: date | None = Query(default=None),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, object]:
        require_competitor_admin(request)
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

    @app.get("/api/competitors/listing-operations")
    def competitor_listing_operations(
        request: Request,
        source_type: Literal["seller", "category"] | None = Query(default=None),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=10, ge=1, le=50),
    ) -> dict[str, object]:
        require_competitor_admin(request)
        settings = DashboardSettings.from_env(root)
        engine = create_read_only_erp_engine(settings.database_url)
        try:
            with Session(engine) as session:
                statement = select(CompetitorListingOperation)
                count_statement = select(func.count()).select_from(
                    CompetitorListingOperation
                )
                if source_type is not None:
                    condition = CompetitorListingOperation.source_type == source_type
                    statement = statement.where(condition)
                    count_statement = count_statement.where(condition)
                total = int(session.scalar(count_statement) or 0)
                operations = session.scalars(
                    statement.order_by(
                        CompetitorListingOperation.committed_at.desc(),
                        CompetitorListingOperation.id.desc(),
                    )
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                ).all()
        finally:
            engine.dispose()
        return {
            "items": [
                _competitor_listing_operation_payload(operation)
                for operation in operations
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
            "source_type": source_type,
        }

    @app.get("/api/competitors/listing-operations/{operation_id}/items")
    def competitor_listing_operation_items(
        operation_id: int,
        request: Request,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, object]:
        require_competitor_admin(request)
        settings = DashboardSettings.from_env(root)
        engine = create_read_only_erp_engine(settings.database_url)
        try:
            with Session(engine) as session:
                if session.get(CompetitorListingOperation, operation_id) is None:
                    raise HTTPException(status_code=404, detail="店铺/类目操作记录不存在")
                condition = (
                    CompetitorListingOperationItem.operation_id == operation_id
                )
                total = int(
                    session.scalar(
                        select(func.count())
                        .select_from(CompetitorListingOperationItem)
                        .where(condition)
                    )
                    or 0
                )
                items = session.scalars(
                    select(CompetitorListingOperationItem)
                    .where(condition)
                    .order_by(CompetitorListingOperationItem.position.asc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                ).all()
        finally:
            engine.dispose()
        return {
            "items": [
                _competitor_listing_operation_item_payload(item)
                for item in items
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
            "operation_id": operation_id,
        }

    @app.get("/api/competitors/{plid}")
    def competitor_detail(
        plid: str,
        request: Request,
        start_date: date | None = Query(default=None),
        end_date: date | None = Query(default=None),
        own_store_scope: Literal["current", "all", "operating"] = Query(default="current"),
    ) -> dict[str, Any]:
        own_store_codes = _own_store_codes_for_request(request, own_store_scope)
        inventory_store_codes = _own_store_codes_for_request(request, "all")
        profit_fee_window_end = (
            datetime.now(ZoneInfo("Africa/Johannesburg")).date() - timedelta(days=1)
        )
        dataset = _load_competitor_dataset(
            root,
            start_date=start_date,
            end_date=end_date,
            own_store_codes=own_store_codes,
            plids={plid},
            engine=read_engine,
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
        current_frame = dataset.store_current if store_item is not None else dataset.current
        if not current_frame.empty:
            current_frame = current_frame.loc[
                current_frame["plid"].astype(str) == plid
            ].head(1)
        current_records = frame_records(current_frame)
        with Session(read_engine) as session:
            target = session.get(CompetitorTarget, plid)
            monitoring_target = (
                _competitor_target_payload(
                    target,
                    has_history=session.scalar(
                        select(CompetitorSnapshot.id)
                        .where(CompetitorSnapshot.plid == plid)
                        .limit(1)
                    )
                    is not None,
                )
                if target is not None and target.active
                else None
            )
            competitor_personal_item = session.get(
                CompetitorPersonalWatchlist,
                (request.state.erp_user.id, plid),
            )
            own_personal_item = (
                session.get(
                    OwnStorePersonalWatchlist,
                    (request.state.erp_user.id, plid),
                )
                if competitor_personal_item is None
                else None
            )
            personal_item: CompetitorPersonalWatchlist | OwnStorePersonalWatchlist | None
            personal_item = competitor_personal_item or own_personal_item
            personal_source: Literal["competitor", "own_store"] = (
                "competitor" if competitor_personal_item is not None else "own_store"
            )
            personal_watchlist_item = (
                _personal_watchlist_item_payload(
                    personal_item,
                    source=personal_source,
                    library_ids=[],
                )
                if personal_item is not None
                else None
            )
        own_store_sales_detail = (
            _load_own_store_sales_detail(
                root,
                plid=plid,
                own_store_codes=own_store_codes,
                through=datetime.now(ZoneInfo("Asia/Shanghai")).date(),
                engine=read_engine,
            )
            if store_item is not None
            else {"link_series": [], "variant_series": []}
        )
        own_store_sales_scope = aggregate_own_store_sales_series(
            own_store_sales_detail["link_series"]
        )
        own_store_traffic = (
            _load_own_store_traffic(
                root,
                plid=plid,
                own_store_codes=own_store_codes,
                start_date=start_date,
                end_date=end_date,
                engine=read_engine,
            )
            if store_item is not None
            else []
        )
        own_store_returns = (
            _load_own_store_returns(
                root,
                plid=plid,
                own_store_codes=own_store_codes,
                engine=read_engine,
            )
            if store_item is not None
            else _empty_return_payload(
                start_date=start_date,
                end_date=end_date,
                message="该链接不是当前授权范围内的自有链接。",
            )
        )
        return {
            "current_item": current_records[0] if current_records else None,
            "monitoring_target": monitoring_target,
            "personal_watchlist_item": personal_watchlist_item,
            "category_path": dataset.category_paths.get(plid, []),
            "history": frame_records(history),
            "reviews": frame_records(reviews),
            "variants": frame_records(variants),
            "own_store_sales": own_store_sales_detail["link_series"],
            "own_store_sales_scope": own_store_sales_scope,
            "own_store_variant_sales": own_store_sales_detail["variant_series"],
            "own_store_traffic": own_store_traffic,
            "own_store_returns": own_store_returns,
            "own_store_profitability": (
                _load_own_store_profitability(
                    root,
                    plid=plid,
                    own_store_codes=own_store_codes,
                    rate_service=request.app.state.cny_zar_rate_service,
                    cost_as_of=datetime.now(ZoneInfo("Asia/Shanghai")).date(),
                    fee_window_end=profit_fee_window_end,
                    engine=read_engine,
                )
                if store_item is not None
                else empty_own_store_profitability(
                    store_codes=own_store_codes,
                    fee_window_end=profit_fee_window_end,
                    message="该链接不是当前授权范围内的自有链接。",
                )
            ),
            "company_inventory": (
                _load_company_inventory(
                    root,
                    plid=plid,
                    own_store_codes=inventory_store_codes,
                    engine=read_engine,
                )
                if store_item is not None
                else {
                    "items": [],
                    "company_sku_count": 0,
                    "store_codes": sorted(inventory_store_codes),
                    "w8_shared_once": True,
                    "stage_totals_are_additive": False,
                    "message": "该链接不是当前授权范围内的自有链接。",
                }
            ),
        }

    async def run_competitor_collection(
        payload: CollectCompetitorRequest,
        user: UserIdentity,
    ) -> CompetitorCollectionResult:
        """Run one link through the shared registry/coordinator for page or schedule."""
        try:
            plid = extract_plid(payload.url)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
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
            raise exc
        effective_with_stock_probe, effective_visible_browser = (
            collection_registry.collection_options(
                batch_id=payload.batch_id,
                fallback_with_stock_probe=payload.with_stock_probe,
                fallback_visible_browser=payload.visible_browser,
            )
        )
        request_started_at = time.perf_counter()
        collection_source = (
            "scheduled"
            if user.username == SCHEDULED_OWNER_USERNAME
            else "manual"
        )

        def request_elapsed_ms() -> int:
            return max(0, int(round((time.perf_counter() - request_started_at) * 1000)))

        async def execute_collection() -> CompetitorCollectionResult:
            registry_reason = ""

            def report_stage(stage: str) -> None:
                collection_registry.update_link_stage(
                    batch_id=payload.batch_id,
                    request_id=payload.request_id,
                    stage=stage,
                )
                competitor_logger.info(
                    "link_stage batch=%s request=%s item=%s/%s plid=%s "
                    "elapsed_ms=%s stage=%s",
                    payload.batch_id or "-",
                    payload.request_id or "-",
                    _display_item_number(payload.item_index),
                    payload.total_items or "-",
                    plid,
                    request_elapsed_ms(),
                    _single_line(stage),
                )

            competitor_logger.info(
                "link_start batch=%s request=%s item=%s/%s plid=%s source=%s "
                "user=%s retry_kind=%s retry_attempt=%s stock_probe=%s "
                "visible_browser=%s",
                payload.batch_id or "-",
                payload.request_id or "-",
                _display_item_number(payload.item_index),
                payload.total_items or "-",
                plid,
                collection_source,
                user.username,
                payload.retry_kind or "-",
                payload.retry_attempt if payload.retry_attempt is not None else "-",
                effective_with_stock_probe,
                effective_visible_browser,
            )
            settings = DashboardSettings.from_env(root)
            engine = create_engine_for_settings(settings)
            try:
                create_schema(engine)
                with Session(engine) as session:
                    followers_only = is_connected_store_plid(session, plid)
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
                                "offer_targets_discovered batch=%s request=%s "
                                "origin_plid=%s added=%s queued=%s user=%s",
                                payload.batch_id or "-",
                                payload.request_id or "-",
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
                        "plid=%s kind=network duration_ms=%s reason=%s",
                        payload.batch_id or "-",
                        payload.request_id or "-",
                        _display_item_number(payload.item_index),
                        payload.total_items or "-",
                        plid,
                        request_elapsed_ms(),
                        _single_line(str(exc)),
                    )
                    raise
                registry_reason = _single_line(result.message)
            except BaseException as exc:
                registry_reason = registry_reason or _single_line(str(exc))
                if not isinstance(exc, CompetitorNetworkError):
                    competitor_logger.error(
                        "link_exception batch=%s request=%s item=%s/%s plid=%s "
                        "type=%s duration_ms=%s reason=%s",
                        payload.batch_id or "-",
                        payload.request_id or "-",
                        _display_item_number(payload.item_index),
                        payload.total_items or "-",
                        plid,
                        type(exc).__name__,
                        request_elapsed_ms(),
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
                "succeeded=%s kind=%s retryable=%s added_targets=%s "
                "duration_ms=%s reason=%s",
                payload.batch_id or "-",
                payload.request_id or "-",
                _display_item_number(payload.item_index),
                payload.total_items or "-",
                result.plid,
                result.succeeded,
                result.failure_kind or "-",
                result.retryable,
                result.added_target_count,
                request_elapsed_ms(),
                _single_line(result.message),
            )
            return result

        try:
            result, reused = await collection_coordinator.run(
                payload.request_id,
                execute_collection,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            collection_registry.record_outcome(
                batch_id=payload.batch_id,
                plid=plid,
                url=payload.url,
                title=None,
                message=_single_line(str(exc)) or type(exc).__name__,
                succeeded=False,
                failure_kind="other",
            )
            raise
        if reused:
            competitor_logger.info(
                "link_reused batch=%s request=%s item=%s/%s plid=%s "
                "duration_ms=%s",
                payload.batch_id or "-",
                payload.request_id or "-",
                _display_item_number(payload.item_index),
                payload.total_items or "-",
                result.plid,
                request_elapsed_ms(),
            )
            collection_registry.finish_link(
                batch_id=payload.batch_id,
                request_id=payload.request_id,
                reason=_single_line(result.message),
            )
        collection_registry.record_outcome(
            batch_id=payload.batch_id,
            plid=result.plid,
            url=payload.url,
            title=result.title,
            message=result.message,
            succeeded=result.succeeded,
            failure_kind=result.failure_kind,
        )
        return result

    async def load_scheduled_competitor_targets() -> list[ScheduledCollectionTarget]:
        def load() -> list[ScheduledCollectionTarget]:
            settings = DashboardSettings.from_env(root)
            engine = create_read_only_erp_engine(settings.database_url)
            try:
                with Session(engine) as session:
                    own_plids = connected_store_plids(session)
                    statement = select(CompetitorTarget).where(
                        CompetitorTarget.active.is_(True)
                    )
                    if own_plids:
                        statement = statement.where(CompetitorTarget.plid.not_in(own_plids))
                    true_targets = session.scalars(
                        statement.order_by(
                            CompetitorTarget.created_at.asc(),
                            CompetitorTarget.plid.asc(),
                        )
                    ).all()
                    own_targets = sorted(own_plids)
                return [
                    *[
                        ScheduledCollectionTarget(plid=target.plid, url=target.url)
                        for target in true_targets
                    ],
                    *[
                        ScheduledCollectionTarget(
                            plid=plid,
                            url=f"https://www.takealot.com/p/PLID{plid}",
                        )
                        for plid in own_targets
                    ],
                ]
            finally:
                engine.dispose()

        return await run_in_threadpool(load)

    scheduled_user = UserIdentity(
        id=0,
        username=SCHEDULED_OWNER_USERNAME,
        display_name=SCHEDULED_OWNER_DISPLAY_NAME,
        role="system",
        permissions=(COMPETITORS_COLLECT,),
        permissions_customized=False,
        all_stores=True,
        assigned_store_ids=(),
        accessible_stores=(),
    )

    async def collect_scheduled_target(
        url: str,
        batch_id: str,
        request_id: str,
        item_index: int,
        total_items: int,
        retry_kind: str | None,
        retry_attempt: int | None,
    ) -> ScheduledCollectionAttempt:
        payload = CollectCompetitorRequest(
            url=url,
            with_stock_probe=True,
            visible_browser=False,
            batch_id=batch_id,
            client_id=SCHEDULED_CLIENT_ID,
            request_id=request_id,
            item_index=item_index,
            total_items=total_items,
            retry_kind=retry_kind,
            retry_attempt=retry_attempt,
        )
        try:
            result = await run_competitor_collection(payload, scheduled_user)
        except asyncio.CancelledError:
            raise
        except CompetitorNetworkError as exc:
            return ScheduledCollectionAttempt(
                plid=extract_plid(url),
                title=None,
                message=_single_line(str(exc)),
                succeeded=False,
                failure_kind="network",
                retryable=True,
            )
        except Exception as exc:
            competitor_logger.exception(
                "scheduled_link_exception batch=%s request=%s item=%s/%s url=%s",
                batch_id,
                request_id,
                item_index + 1,
                total_items,
                url,
            )
            return ScheduledCollectionAttempt(
                plid=extract_plid(url),
                title=None,
                message=_single_line(str(exc)) or type(exc).__name__,
                succeeded=False,
                failure_kind="other",
                retryable=True,
            )
        return ScheduledCollectionAttempt(
            plid=result.plid,
            title=result.title,
            message=result.message,
            succeeded=result.succeeded,
            failure_kind=result.failure_kind,
            retryable=result.retryable,
            added_target_count=result.added_target_count,
        )

    scheduled_competitor_runner = ScheduledCompetitorBatchRunner(
        registry=collection_registry,
        journal_path=(
            None
            if database_url.startswith("sqlite")
            else root / "logs" / "competitor-scheduled-batch.json"
        ),
        trigger_dir=root / "logs" / "competitor-scheduled-triggers",
        load_targets=load_scheduled_competitor_targets,
        collect_target=collect_scheduled_target,
        logger=competitor_logger,
        continuous_rounds=True,
    )
    app.state.scheduled_competitor_runner = scheduled_competitor_runner

    @app.post("/api/internal/competitors/scheduled-trigger")
    async def trigger_scheduled_competitor_batch(
        request: Request,
        payload: ScheduledCompetitorTriggerRequest | None = None,
    ) -> dict[str, object]:
        if not _is_loopback_request(request):
            raise HTTPException(
                status_code=403,
                detail="竞品自动批次只允许 Windows 计划任务从服务器本机触发",
            )
        try:
            return await scheduled_competitor_runner.trigger(
                payload.requested_for if payload is not None else None
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/competitors/batch-resume")
    async def resume_scheduled_competitor_batch(
        payload: CompetitorBatchResumeRequest,
        request: Request,
    ) -> dict[str, object]:
        require_competitor_batch_controller(request)
        try:
            runner_status = scheduled_competitor_runner.status()
            if runner_status.get("run_status") == "paused":
                await scheduled_competitor_runner.resume_network_pause(
                    payload.batch_id,
                    resumed_by=request.state.erp_user.username,
                )
            else:
                await scheduled_competitor_runner.resume_stopped(
                    payload.batch_id,
                    resumed_by=request.state.erp_user.username,
                )
        except (CollectionBatchBusyError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "status": _competitor_batch_status_payload()}

    @app.post("/api/competitors/collect")
    async def collect_competitor(
        payload: CollectCompetitorRequest,
        request: Request,
    ) -> dict[str, object]:
        require_competitor_batch_controller(request)
        try:
            result = await run_competitor_collection(payload, request.state.erp_user)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except CollectionBatchBusyError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc
        except asyncio.CancelledError as exc:
            raise HTTPException(
                status_code=409,
                detail="采集已停止，当前浏览器探测已中断并关闭",
            ) from exc
        except CompetitorNetworkError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
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

    async def stop_competitor_batch(
        *,
        batch_id: str,
        reason: str,
        stopped_by: str,
    ) -> dict[str, object]:
        async with collection_stop_lock:
            try:
                status = collection_registry.stop(batch_id=batch_id, reason=reason)
            except CollectionBatchBusyError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if not status.get("active") and status.get("event") == "manual_stop":
                return status
            cancelled_request_id = str(status.get("current_request_id") or "")
            cancelled = False
            scheduled_stop_recorded = False
            try:
                scheduled_stop_recorded = await scheduled_competitor_runner.mark_stopped(
                    batch_id,
                    stopped_by=stopped_by,
                    reason=reason,
                )
            finally:
                try:
                    cancelled = await collection_coordinator.cancel(
                        cancelled_request_id
                    )
                finally:
                    try:
                        await competitor_public_client.close()
                    finally:
                        status = collection_registry.complete_stop(batch_id=batch_id)
            competitor_logger.info(
                "batch_cancel batch=%s request=%s cancelled=%s user=%s source=%s",
                batch_id,
                cancelled_request_id or "-",
                cancelled,
                stopped_by,
                status.get("source") or "manual",
            )
            if not scheduled_stop_recorded:
                competitor_logger.info(
                    "batch_event batch=%s event=manual_stop completed=%s total=%s "
                    "pending=%s succeeded=%s failed=%s terminal=%s user=%s source=%s "
                    "wall_elapsed_seconds=%.3f reason=%s",
                    batch_id,
                    status.get("completed") or 0,
                    status.get("total") or 0,
                    status.get("pending") or 0,
                    status.get("succeeded") or 0,
                    status.get("failed") or 0,
                    status.get("terminal") or 0,
                    stopped_by,
                    status.get("source") or "manual",
                    _wall_elapsed_seconds(status.get("started_at")),
                    _single_line(reason),
                )
            return status

    @app.post("/api/competitors/batch-stop")
    async def stop_active_competitor_batch(
        payload: CompetitorBatchStopRequest,
        request: Request,
    ) -> dict[str, object]:
        require_competitor_batch_controller(request)
        await stop_competitor_batch(
            batch_id=payload.batch_id,
            reason=payload.reason,
            stopped_by=request.state.erp_user.username,
        )
        return {"ok": True, "status": _competitor_batch_status_payload()}

    @app.post("/api/competitors/batch-events")
    async def competitor_batch_event(
        payload: CompetitorBatchEventRequest,
        request: Request,
    ) -> dict[str, object]:
        require_competitor_batch_controller(request)
        user = request.state.erp_user
        if payload.event == "manual_stop":
            await stop_competitor_batch(
                batch_id=payload.batch_id,
                reason=payload.reason,
                stopped_by=user.username,
            )
            return {"ok": True, "status": _competitor_batch_status_payload()}
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
                include_details=False,
            )
        except CollectionBatchBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        effective_event = str(status["event"])
        wall_elapsed_seconds = _wall_elapsed_seconds(status.get("started_at"))
        if effective_event != payload.event:
            competitor_logger.info(
                "batch_event batch=%s event=%s submitted_event=%s completed=%s "
                "total=%s pending=%s succeeded=%s failed=%s terminal=%s user=%s "
                "source=%s result_count=%s error_count=%s stock_probe=%s "
                "visible_browser=%s wall_elapsed_seconds=%.3f reason=%s",
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
                status.get("source") or "manual",
                status.get("result_count") or 0,
                status.get("error_count") or 0,
                status.get("with_stock_probe"),
                status.get("visible_browser"),
                wall_elapsed_seconds,
                _single_line(payload.reason),
            )
        else:
            competitor_logger.info(
                "batch_event batch=%s event=%s completed=%s total=%s pending=%s "
                "succeeded=%s failed=%s terminal=%s user=%s source=%s "
                "result_count=%s error_count=%s stock_probe=%s visible_browser=%s "
                "wall_elapsed_seconds=%.3f reason=%s",
                payload.batch_id,
                payload.event,
                payload.completed,
                payload.total,
                payload.pending,
                payload.succeeded,
                payload.failed,
                payload.terminal,
                user.username,
                status.get("source") or "manual",
                status.get("result_count") or 0,
                status.get("error_count") or 0,
                status.get("with_stock_probe"),
                status.get("visible_browser"),
                wall_elapsed_seconds,
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


@dataclass(frozen=True)
class _PersistedListingTargets:
    operation_id: int
    personal_library_id: int
    personal_library_name: str
    selected_count: int
    added_target_count: int
    reactivated_target_count: int
    existing_target_count: int
    own_store_count: int
    personal_watchlist_added_count: int
    new_targets: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _NormalizedListingProduct:
    plid: str
    url: str
    title: str
    sort_ranks: dict[str, int]


def _persist_competitor_listing_targets(
    engine: Engine,
    *,
    preview: Mapping[str, object],
    user: UserIdentity,
    personal_library_id: int,
) -> _PersistedListingTargets:
    """Persist one frozen listing preview with the single-link ownership semantics."""

    raw_products = preview.get("products")
    if not isinstance(raw_products, list):
        raise CompetitorListingInputError("筛选预览中的商品格式无效")
    raw_source_type = str(preview.get("source_type") or "").strip()
    if raw_source_type not in {"seller", "category"}:
        raise CompetitorListingInputError("筛选预览中的来源类型无效")
    source_type: Literal["seller", "category"] = (
        "seller" if raw_source_type == "seller" else "category"
    )
    source = parse_competitor_listing_source(
        str(preview.get("source_url") or ""),
        expected_type=source_type,
    )
    price_min = _listing_preview_optional_int(preview.get("price_min"), "最低价格")
    price_max = _listing_preview_optional_int(preview.get("price_max"), "最高价格")
    product_limit = _listing_preview_optional_int(
        preview.get("product_limit"),
        "最终加入数量",
    )
    if product_limit is not None and not 1 <= product_limit <= MAX_LISTING_PRODUCTS:
        raise CompetitorListingInputError("筛选预览中的最终加入数量无效")
    raw_sorts = preview.get("sorts")
    if not isinstance(raw_sorts, list) or not raw_sorts:
        raise CompetitorListingInputError("筛选预览中的排序方式无效")
    selected_sorts: list[str] = []
    for raw_sort in raw_sorts:
        sort = " ".join(str(raw_sort).split())
        build_competitor_listing_url(
            source,
            sort=sort,
            price_min=price_min,
            price_max=price_max,
        )
        if sort not in selected_sorts:
            selected_sorts.append(sort)
    selection_rule = str(preview.get("selection_rule") or "").strip()
    if selection_rule != BALANCED_LISTING_SELECTION_RULE:
        raise CompetitorListingInputError("筛选预览中的合并排序规则已失效")

    normalized_products: list[_NormalizedListingProduct] = []
    seen: set[str] = set()
    for raw in raw_products:
        if not isinstance(raw, Mapping):
            raise CompetitorListingInputError("筛选预览中的商品格式无效")
        submitted_plid = str(raw.get("plid") or "").strip()
        submitted_url = str(raw.get("url") or "").strip()
        try:
            plid, url = _validated_competitor_target_url(submitted_url)
        except HTTPException as exc:
            raise CompetitorListingInputError(str(exc.detail)) from exc
        if submitted_plid != plid:
            raise CompetitorListingInputError("筛选预览中的商品 PLID 与链接不一致")
        if plid in seen:
            continue
        title = " ".join(str(raw.get("title") or "").split())[:1000]
        seen.add(plid)
        normalized_products.append(
            _NormalizedListingProduct(
                plid=plid,
                url=url,
                title=title or f"PLID{plid}",
                sort_ranks=_listing_preview_sort_ranks(
                    raw.get("sort_ranks"),
                    selected_sorts,
                ),
            )
        )
    if not normalized_products:
        raise CompetitorListingInputError("筛选预览没有可加入的商品")
    if len(normalized_products) > MAX_LISTING_PRODUCTS:
        raise CompetitorListingInputError(
            f"单次最多加入 {MAX_LISTING_PRODUCTS} 个去重商品"
        )

    added_target_count = 0
    reactivated_target_count = 0
    existing_target_count = 0
    own_store_count = 0
    personal_watchlist_added_count = 0
    new_targets: list[tuple[str, str]] = []
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        personal_library = session.get(
            PersonalWatchlistLibrary,
            personal_library_id,
        )
        if personal_library is None or personal_library.user_id != user.id:
            raise CompetitorListingInputError(
                "请选择当前账号拥有的个人监控池类型库"
            )
        personal_library_name = personal_library.name
        own_rows = _authorized_own_store_rows_for_personal_watchlist(
            load_connected_store_offers(
                session,
                plids={product.plid for product in normalized_products},
            ),
            user=user,
        )
        own_plids = {
            str(row.offer.productline_id or "").strip()
            for row in own_rows
            if str(row.offer.productline_id or "").strip()
        }
        operation = CompetitorListingOperation(
            source_type=source.source_type,
            source_url=source.source_url,
            source_label=source.source_label[:255],
            personal_library_id=personal_library.id,
            personal_library_name=personal_library.name,
            price_min=price_min,
            price_max=price_max,
            sorts=list(selected_sorts),
            selection_rule=selection_rule,
            product_limit=product_limit,
            selected_count=len(normalized_products),
            added_target_count=0,
            reactivated_target_count=0,
            existing_target_count=0,
            own_store_count=0,
            personal_watchlist_added_count=0,
            actor_user_id=user.id,
            actor_username=user.username,
            actor_display_name=user.display_name,
            committed_at=now,
        )
        session.add(operation)
        session.flush()
        operation_id = operation.id

        for position, product in enumerate(normalized_products, start=1):
            plid = product.plid
            url = product.url
            title = product.title
            personal_created = False
            if plid in own_plids:
                _, personal_created = _ensure_own_store_personal_watchlist_item(
                    session,
                    user_id=user.id,
                    plid=plid,
                    added_at=now,
                )
                _assign_personal_watchlist_library(
                    session,
                    library_id=personal_library.id,
                    plid=plid,
                    added_at=now,
                )
                own_store_count += 1
                personal_watchlist_added_count += int(personal_created)
                item_result = "own_store"
            else:
                target = session.get(CompetitorTarget, plid)
                if target is not None and target.active:
                    existing_target_count += 1
                    _, personal_created = _ensure_competitor_personal_watchlist_item(
                        session,
                        user_id=user.id,
                        plid=plid,
                        added_at=now,
                    )
                    _assign_personal_watchlist_library(
                        session,
                        library_id=personal_library.id,
                        plid=plid,
                        added_at=now,
                    )
                    personal_watchlist_added_count += int(personal_created)
                    item_result = "existing_target"
                else:
                    old_url = target.url if target is not None else None
                    item_result = "reactivated_target" if target is not None else "added_target"
                    if target is None:
                        target = CompetitorTarget(
                            plid=plid,
                            offer_group_plid=plid,
                            url=url,
                            title=title,
                            active=True,
                            created_at=now,
                            updated_at=now,
                        )
                        session.add(target)
                    else:
                        if not target.offer_group_plid:
                            target.offer_group_plid = plid
                        target.url = url
                        target.title = target.title or title
                        target.active = True
                        target.updated_at = now
                        reactivated_target_count += 1
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
                    _, personal_created = _ensure_competitor_personal_watchlist_item(
                        session,
                        user_id=user.id,
                        plid=plid,
                        added_at=now,
                    )
                    _assign_personal_watchlist_library(
                        session,
                        library_id=personal_library.id,
                        plid=plid,
                        added_at=now,
                    )
                    personal_watchlist_added_count += int(personal_created)
                    added_target_count += 1
                    new_targets.append((plid, url))
            session.add(
                CompetitorListingOperationItem(
                    operation_id=operation_id,
                    position=position,
                    plid=plid,
                    title=title,
                    url=url,
                    result=item_result,
                    personal_watchlist_added=personal_created,
                    sort_ranks=product.sort_ranks,
                )
            )
        operation.added_target_count = added_target_count
        operation.reactivated_target_count = reactivated_target_count
        operation.existing_target_count = existing_target_count
        operation.own_store_count = own_store_count
        operation.personal_watchlist_added_count = personal_watchlist_added_count

    return _PersistedListingTargets(
        operation_id=operation_id,
        personal_library_id=personal_library_id,
        personal_library_name=personal_library_name,
        selected_count=len(normalized_products),
        added_target_count=added_target_count,
        reactivated_target_count=reactivated_target_count,
        existing_target_count=existing_target_count,
        own_store_count=own_store_count,
        personal_watchlist_added_count=personal_watchlist_added_count,
        new_targets=tuple(new_targets),
    )


def _listing_preview_optional_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CompetitorListingInputError(f"筛选预览中的{label}无效")
    return value


def _listing_preview_sort_ranks(
    value: object,
    selected_sorts: Sequence[str],
) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    output: dict[str, int] = {}
    for sort in selected_sorts:
        rank = value.get(sort)
        if (
            isinstance(rank, int)
            and not isinstance(rank, bool)
            and 1 <= rank <= MAX_LISTING_PRODUCTS
        ):
            output[sort] = rank
    return output


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
        own_store_item = session.get(OwnStorePersonalWatchlist, (user_id, plid))
        if own_store_item is not None:
            session.delete(own_store_item)
        return item, False
    own_store_item = session.get(OwnStorePersonalWatchlist, (user_id, plid))
    if own_store_item is not None:
        added_at = own_store_item.added_at
        session.delete(own_store_item)
    item = CompetitorPersonalWatchlist(
        user_id=user_id,
        plid=plid,
        added_at=added_at,
    )
    session.add(item)
    return item, True


def _ensure_own_store_personal_watchlist_item(
    session: Session,
    *,
    user_id: int,
    plid: str,
    added_at: datetime,
) -> tuple[OwnStorePersonalWatchlist, bool]:
    item = session.get(OwnStorePersonalWatchlist, (user_id, plid))
    if item is not None:
        competitor_item = session.get(CompetitorPersonalWatchlist, (user_id, plid))
        if competitor_item is not None:
            session.delete(competitor_item)
        return item, False
    competitor_item = session.get(CompetitorPersonalWatchlist, (user_id, plid))
    if competitor_item is not None:
        added_at = competitor_item.added_at
        session.delete(competitor_item)
    item = OwnStorePersonalWatchlist(
        user_id=user_id,
        plid=plid,
        added_at=added_at,
    )
    session.add(item)
    return item, True


def _assign_default_personal_watchlist_library(
    session: Session,
    *,
    user_id: int,
    plid: str,
    added_at: datetime,
    membership_created: bool,
) -> None:
    if not membership_created:
        return
    preference = session.get(PersonalWatchlistPreference, user_id)
    if (
        preference is None
        or not preference.default_configured
        or preference.default_library_id is None
    ):
        return
    library = session.get(PersonalWatchlistLibrary, preference.default_library_id)
    if library is None:
        return
    access = _personal_watchlist_library_access(
        session,
        library=library,
        user_id=user_id,
    )
    if access not in {"owner", "edit"}:
        return
    _assign_personal_watchlist_library(
        session,
        library_id=library.id,
        plid=plid,
        added_at=added_at,
    )


def _assign_personal_watchlist_library(
    session: Session,
    *,
    library_id: int,
    plid: str,
    added_at: datetime,
) -> None:
    assignment = session.get(
        PersonalWatchlistLibraryItem,
        (library_id, plid),
    )
    if assignment is None:
        session.add(
            PersonalWatchlistLibraryItem(
                library_id=library_id,
                plid=plid,
                added_at=added_at,
            )
        )


def _personal_watchlist_library_ids_for_plid(
    session: Session,
    *,
    user_id: int,
    plid: str,
) -> list[int]:
    owned_ids = set(
        session.scalars(
            select(PersonalWatchlistLibrary.id).where(
                PersonalWatchlistLibrary.user_id == user_id
            )
        ).all()
    )
    shared_ids = set(
        session.scalars(
            select(PersonalWatchlistLibraryShare.library_id).where(
                PersonalWatchlistLibraryShare.user_id == user_id
            )
        ).all()
    )
    accessible_ids = owned_ids | shared_ids
    if not accessible_ids:
        return []
    return sorted(
        session.scalars(
            select(PersonalWatchlistLibraryItem.library_id)
            .where(
                PersonalWatchlistLibraryItem.library_id.in_(accessible_ids),
                PersonalWatchlistLibraryItem.plid == plid,
            )
        ).all()
    )


def _personal_watchlist_item_payload(
    item: CompetitorPersonalWatchlist | OwnStorePersonalWatchlist,
    *,
    source: Literal["competitor", "own_store"],
    library_ids: Sequence[int] = (),
) -> dict[str, object]:
    return {
        "plid": item.plid,
        "added_at": item.added_at.isoformat(),
        "source": source,
        "library_ids": list(library_ids),
    }


def _personal_watchlist_projection_plids(
    session: Session,
    *,
    user_id: int,
) -> set[str]:
    """Return only PLIDs the account can currently see in its watchlist workspace."""

    plids = {
        *session.scalars(
            select(CompetitorPersonalWatchlist.plid).where(
                CompetitorPersonalWatchlist.user_id == user_id
            )
        ).all(),
        *session.scalars(
            select(OwnStorePersonalWatchlist.plid).where(
                OwnStorePersonalWatchlist.user_id == user_id
            )
        ).all(),
    }
    owned_library_ids = set(
        session.scalars(
            select(PersonalWatchlistLibrary.id).where(
                PersonalWatchlistLibrary.user_id == user_id
            )
        ).all()
    )
    shared_library_ids = set(
        session.scalars(
            select(PersonalWatchlistLibraryShare.library_id).where(
                PersonalWatchlistLibraryShare.user_id == user_id
            )
        ).all()
    )
    accessible_library_ids = owned_library_ids | shared_library_ids
    if accessible_library_ids:
        plids.update(
            session.scalars(
                select(PersonalWatchlistLibraryItem.plid).where(
                    PersonalWatchlistLibraryItem.library_id.in_(accessible_library_ids)
                )
            ).all()
        )
    return {str(plid).strip() for plid in plids if str(plid).strip()}


def _personal_watchlist_payload(
    session: Session,
    *,
    user_id: int,
    accessible_store_codes: set[str],
) -> dict[str, object]:
    competitor_items = session.scalars(
        select(CompetitorPersonalWatchlist).where(CompetitorPersonalWatchlist.user_id == user_id)
    ).all()
    own_store_items = session.scalars(
        select(OwnStorePersonalWatchlist).where(OwnStorePersonalWatchlist.user_id == user_id)
    ).all()
    owned_libraries = session.scalars(
        select(PersonalWatchlistLibrary)
        .where(PersonalWatchlistLibrary.user_id == user_id)
        .order_by(
            PersonalWatchlistLibrary.created_at.asc(),
            PersonalWatchlistLibrary.id.asc(),
        )
    ).all()
    recipient_shares = session.scalars(
        select(PersonalWatchlistLibraryShare).where(
            PersonalWatchlistLibraryShare.user_id == user_id
        )
    ).all()
    owned_library_ids = {library.id for library in owned_libraries}
    shared_library_ids = {
        share.library_id
        for share in recipient_shares
        if share.library_id not in owned_library_ids
    }
    shared_libraries = (
        session.scalars(
            select(PersonalWatchlistLibrary)
            .where(PersonalWatchlistLibrary.id.in_(shared_library_ids))
            .order_by(
                PersonalWatchlistLibrary.created_at.asc(),
                PersonalWatchlistLibrary.id.asc(),
            )
        ).all()
        if shared_library_ids
        else []
    )
    libraries = [*owned_libraries, *shared_libraries]
    library_ids = [library.id for library in libraries]
    access_by_library_id: dict[int, Literal["owner", "read", "edit"]] = {
        library.id: "owner" for library in owned_libraries
    }
    access_by_library_id.update(
        {
            share.library_id: "edit" if share.permission == "edit" else "read"
            for share in recipient_shares
            if share.library_id in shared_library_ids
        }
    )
    assignments = (
        session.scalars(
            select(PersonalWatchlistLibraryItem).where(
                PersonalWatchlistLibraryItem.library_id.in_(library_ids)
            )
        ).all()
        if library_ids
        else []
    )
    assignments_by_plid: dict[str, list[int]] = defaultdict(list)
    assignment_added_at_by_plid: dict[str, datetime] = {}
    item_counts: dict[int, int] = defaultdict(int)
    for assignment in assignments:
        assignments_by_plid[assignment.plid].append(assignment.library_id)
        item_counts[assignment.library_id] += 1
        previous_added_at = assignment_added_at_by_plid.get(assignment.plid)
        if previous_added_at is None or assignment.added_at > previous_added_at:
            assignment_added_at_by_plid[assignment.plid] = assignment.added_at

    all_shares = (
        session.scalars(
            select(PersonalWatchlistLibraryShare).where(
                PersonalWatchlistLibraryShare.library_id.in_(library_ids)
            )
        ).all()
        if library_ids
        else []
    )
    shares_by_library_id: dict[int, list[PersonalWatchlistLibraryShare]] = defaultdict(list)
    for share in all_shares:
        shares_by_library_id[share.library_id].append(share)
    referenced_user_ids = {
        *(library.user_id for library in libraries),
        *(share.user_id for share in all_shares),
    }
    users_by_id = {
        account.id: account
        for account in (
            session.scalars(
                select(ErpUser).where(ErpUser.id.in_(referenced_user_ids))
            ).all()
            if referenced_user_ids
            else []
        )
    }

    membership_rows: dict[
        str,
        tuple[
            CompetitorPersonalWatchlist | OwnStorePersonalWatchlist,
            Literal["competitor", "own_store"],
        ],
    ] = {item.plid: (item, "competitor") for item in competitor_items}
    membership_rows.update({item.plid: (item, "own_store") for item in own_store_items})
    shared_plids = set(assignments_by_plid) - set(membership_rows)
    shared_item_context = _personal_watchlist_shared_item_context(
        session,
        plids=shared_plids,
        accessible_store_codes=accessible_store_codes,
    )
    ordered_memberships = sorted(
        membership_rows.values(),
        key=lambda row: (row[0].added_at, row[0].plid),
        reverse=True,
    )
    preference = session.get(PersonalWatchlistPreference, user_id)
    default_eligible_library_ids = owned_library_ids | {
        library_id
        for library_id, access in access_by_library_id.items()
        if access == "edit"
    }
    default_preference_valid = bool(
        preference is not None
        and preference.default_configured
        and (
            preference.default_library_id is None
            or preference.default_library_id in default_eligible_library_ids
        )
    )
    default_library_id = (
        preference.default_library_id
        if preference is not None and default_preference_valid
        else None
    )
    shared_items = [
        {
            "plid": plid,
            "added_at": assignment_added_at_by_plid[plid].isoformat(),
            "library_ids": sorted(set(assigned_library_ids)),
            "source": shared_item_context[plid][0],
            "detail_access": shared_item_context[plid][1],
        }
        for plid, assigned_library_ids in assignments_by_plid.items()
        if plid not in membership_rows
    ]
    shared_items.sort(
        key=lambda item: (str(item["added_at"]), str(item["plid"])),
        reverse=True,
    )
    return {
        "items": [
            _personal_watchlist_item_payload(
                item,
                source=source,
                library_ids=sorted(assignments_by_plid[item.plid]),
            )
            for item, source in ordered_memberships
        ],
        "count": len(ordered_memberships),
        "shared_items": shared_items,
        "libraries": [
            _personal_watchlist_library_payload(
                library,
                owner=users_by_id[library.user_id],
                access=access_by_library_id[library.id],
                item_count=item_counts[library.id],
                share_rows=shares_by_library_id[library.id],
                users_by_id=users_by_id,
            )
            for library in libraries
        ],
        "default_library_configured": bool(
            default_preference_valid
        ),
        "default_library_id": default_library_id,
    }


def _validated_personal_watchlist_library_name(value: str) -> str:
    name = " ".join(value.split())
    if not name:
        raise HTTPException(status_code=422, detail="类型库名称不能为空")
    if len(name) > 40:
        raise HTTPException(status_code=422, detail="类型库名称不能超过40个字符")
    return name


def _authorized_own_store_rows_for_personal_watchlist(
    rows: Sequence[ConnectedStoreOffer],
    *,
    user: UserIdentity,
) -> tuple[ConnectedStoreOffer, ...]:
    """Reject own-store memberships unless the account can see an owning store."""

    if not rows:
        return ()
    accessible_store_codes = {
        store.code
        for store in user.accessible_stores
        if store.active and store.data_connected
    }
    owner_codes_by_plid: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        plid = str(row.offer.productline_id or "").strip()
        if plid:
            owner_codes_by_plid[plid].add(row.store_code)
    blocked_plids = sorted(
        plid
        for plid, owner_codes in owner_codes_by_plid.items()
        if owner_codes.isdisjoint(accessible_store_codes)
    )
    if blocked_plids:
        shown = "、".join(f"PLID{plid}" for plid in blocked_plids[:5])
        if len(blocked_plids) > 5:
            shown += f" 等{len(blocked_plids)}个商品"
        logging.getLogger(__name__).warning(
            "personal_watchlist denied_unauthorized_own_store plids=%s user=%s",
            ",".join(blocked_plids),
            user.username,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "所选商品中包含当前账号无权查看店铺的自有商品"
                f"（{shown}），不能加入个人监控池；"
                "请调整选择，或联系管理员分配对应店铺权限"
            ),
        )
    return tuple(row for row in rows if row.store_code in accessible_store_codes)


def _personal_watchlist_shared_item_context(
    session: Session,
    *,
    plids: set[str],
    accessible_store_codes: set[str],
) -> dict[
    str,
    tuple[
        Literal["competitor", "own_store", "unknown"],
        Literal["public", "authorized", "store_access_denied", "unknown"],
    ],
]:
    """Classify shared PLIDs without exposing private store identities or details."""

    if not plids:
        return {}
    own_store_membership_plids = set(
        session.scalars(
            select(OwnStorePersonalWatchlist.plid).where(
                OwnStorePersonalWatchlist.plid.in_(plids)
            )
        ).all()
    )
    competitor_membership_plids = set(
        session.scalars(
            select(CompetitorPersonalWatchlist.plid).where(
                CompetitorPersonalWatchlist.plid.in_(plids)
            )
        ).all()
    )
    competitor_target_plids = set(
        session.scalars(
            select(CompetitorTarget.plid).where(CompetitorTarget.plid.in_(plids))
        ).all()
    )
    owner_codes_by_plid: dict[str, set[str]] = defaultdict(set)
    for row in load_connected_store_offers(session, plids=plids):
        plid = str(row.offer.productline_id or "").strip()
        if plid:
            owner_codes_by_plid[plid].add(row.store_code)

    result: dict[
        str,
        tuple[
            Literal["competitor", "own_store", "unknown"],
            Literal["public", "authorized", "store_access_denied", "unknown"],
        ],
    ] = {}
    for plid in plids:
        owner_codes = owner_codes_by_plid[plid]
        if plid in own_store_membership_plids or owner_codes:
            detail_access: Literal[
                "public",
                "authorized",
                "store_access_denied",
                "unknown",
            ]
            if not owner_codes:
                detail_access = "unknown"
            elif owner_codes.isdisjoint(accessible_store_codes):
                detail_access = "store_access_denied"
            else:
                detail_access = "authorized"
            result[plid] = ("own_store", detail_access)
        elif plid in competitor_membership_plids or plid in competitor_target_plids:
            result[plid] = ("competitor", "public")
        else:
            result[plid] = ("unknown", "unknown")
    return result


def _personal_watchlist_membership_exists(
    session: Session,
    *,
    user_id: int,
    plid: str,
) -> bool:
    return (
        session.get(CompetitorPersonalWatchlist, (user_id, plid)) is not None
        or session.get(OwnStorePersonalWatchlist, (user_id, plid)) is not None
    )


def _remove_personal_watchlist_library_assignments(
    session: Session,
    *,
    user_id: int,
    plid: str,
) -> None:
    library_ids = session.scalars(
        select(PersonalWatchlistLibrary.id).where(PersonalWatchlistLibrary.user_id == user_id)
    ).all()
    if not library_ids:
        return
    assignments = session.scalars(
        select(PersonalWatchlistLibraryItem).where(
            PersonalWatchlistLibraryItem.library_id.in_(library_ids),
            PersonalWatchlistLibraryItem.plid == plid,
        )
    ).all()
    for assignment in assignments:
        session.delete(assignment)


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


def _personal_watchlist_library_payload(
    library: PersonalWatchlistLibrary,
    *,
    owner: ErpUser,
    access: Literal["owner", "read", "edit"],
    item_count: int,
    share_rows: Sequence[PersonalWatchlistLibraryShare] = (),
    users_by_id: Mapping[int, ErpUser] | None = None,
) -> dict[str, object]:
    accounts = users_by_id or {}
    visible_shares = []
    if access == "owner":
        for share in sorted(share_rows, key=lambda item: item.user_id):
            recipient = accounts.get(share.user_id)
            if recipient is None:
                continue
            visible_shares.append(
                {
                    "user_id": recipient.id,
                    "username": recipient.username,
                    "display_name": recipient.display_name,
                    "active": recipient.active,
                    "permission": "edit" if share.permission == "edit" else "read",
                }
            )
    return {
        "id": library.id,
        "name": library.name,
        "created_at": library.created_at.isoformat(),
        "updated_at": library.updated_at.isoformat(),
        "item_count": item_count,
        "owner_user_id": owner.id,
        "owner_username": owner.username,
        "owner_display_name": owner.display_name,
        "access": access,
        "is_owner": access == "owner",
        "share_count": len(share_rows),
        "shares": visible_shares,
    }


def _single_personal_watchlist_library_payload(
    session: Session,
    *,
    library: PersonalWatchlistLibrary,
    viewer_user_id: int,
) -> dict[str, object]:
    access = _personal_watchlist_library_access(
        session,
        library=library,
        user_id=viewer_user_id,
    )
    if access is None:
        raise HTTPException(status_code=404, detail="未找到当前账号可访问的类型库")
    owner = session.get(ErpUser, library.user_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="类型库创建者账号不存在")
    share_rows = session.scalars(
        select(PersonalWatchlistLibraryShare).where(
            PersonalWatchlistLibraryShare.library_id == library.id
        )
    ).all()
    share_user_ids = {share.user_id for share in share_rows}
    users_by_id = {
        account.id: account
        for account in (
            session.scalars(select(ErpUser).where(ErpUser.id.in_(share_user_ids))).all()
            if share_user_ids
            else []
        )
    }
    item_count = int(
        session.scalar(
            select(func.count())
            .select_from(PersonalWatchlistLibraryItem)
            .where(PersonalWatchlistLibraryItem.library_id == library.id)
        )
        or 0
    )
    return _personal_watchlist_library_payload(
        library,
        owner=owner,
        access=access,
        item_count=item_count,
        share_rows=share_rows,
        users_by_id=users_by_id,
    )


def _personal_watchlist_library_access(
    session: Session,
    *,
    library: PersonalWatchlistLibrary,
    user_id: int,
) -> Literal["owner", "read", "edit"] | None:
    if library.user_id == user_id:
        return "owner"
    share = session.get(PersonalWatchlistLibraryShare, (library.id, user_id))
    if share is None:
        return None
    return "edit" if share.permission == "edit" else "read"


def _competitor_listing_operation_payload(
    operation: CompetitorListingOperation,
) -> dict[str, object]:
    return {
        "id": operation.id,
        "source_type": operation.source_type,
        "source_url": operation.source_url,
        "source_label": operation.source_label,
        "personal_library_id": operation.personal_library_id,
        "personal_library_name": operation.personal_library_name,
        "price_min": operation.price_min,
        "price_max": operation.price_max,
        "sorts": list(operation.sorts),
        "selection_rule": operation.selection_rule,
        "product_limit": operation.product_limit,
        "selected_count": operation.selected_count,
        "added_target_count": operation.added_target_count,
        "reactivated_target_count": operation.reactivated_target_count,
        "existing_target_count": operation.existing_target_count,
        "own_store_count": operation.own_store_count,
        "personal_watchlist_added_count": operation.personal_watchlist_added_count,
        "actor_username": operation.actor_username,
        "actor_display_name": operation.actor_display_name,
        "committed_at": operation.committed_at.isoformat(),
    }


def _competitor_listing_operation_item_payload(
    item: CompetitorListingOperationItem,
) -> dict[str, object]:
    return {
        "id": item.id,
        "operation_id": item.operation_id,
        "position": item.position,
        "plid": item.plid,
        "title": item.title,
        "url": item.url,
        "result": item.result,
        "personal_watchlist_added": item.personal_watchlist_added,
        "sort_ranks": dict(item.sort_ranks),
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
    if path.startswith("/api/erp/container-selection"):
        return STORE_VIEW, COMPETITORS_VIEW
    if path == "/api/erp/refresh":
        return REFRESH_RUN
    if path.startswith("/api/erp/returns/removal-orders"):
        return STORE_VIEW if safe_method else REFRESH_RUN
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
            "/api/erp/returns",
            "/api/erp/quadrants",
            "/api/erp/anomaly-products",
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
                "/api/erp/returns",
                "/api/erp/keyword-traffic",
                "/api/erp/search-ranking",
                "/api/erp/quadrants",
                "/api/erp/anomaly-products",
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
        return "当前账号可以查看搜索定位，但不能运行定位或保存人工确认"
    if permission in {REPORTS_GENERATE, NFT102_MANAGE}:
        return "当前账号不能执行报表生成或续写"
    return "当前账号没有访问此模块的权限"


def _refresh_coordination_role(user: UserIdentity) -> str:
    if user.username.casefold() != "kxx" or not user.can(REFRESH_RUN):
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


def _wall_elapsed_seconds(started_at: object) -> float:
    raw = str(started_at or "").strip()
    if not raw:
        return 0.0
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds())


def _load_competitor_dataset(
    project_root: Path,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    own_store_codes: set[str] | None = None,
    plids: set[str] | None = None,
    include_detail_frames: bool = True,
    own_store_only: bool = False,
    include_store_projection: bool = True,
    engine: Engine | None = None,
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
    owned_engine = engine is None
    read_engine = engine or create_read_only_erp_engine(settings.database_url)
    try:
        try:
            return load_competitor_dataset(
                read_engine,
                start_date=start_date,
                end_date=end_date,
                own_store_codes=own_store_codes,
                plids=plids,
                include_detail_frames=include_detail_frames,
                own_store_only=own_store_only,
                include_store_projection=include_store_projection,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        if owned_engine:
            read_engine.dispose()


def _product_master_records(
    project_root: Path,
    records: Sequence[Mapping[str, Any]],
    *,
    sku_field: str = "sku",
    as_of_date: date | None = None,
    engine: Engine | None = None,
) -> list[dict[str, Any]]:
    """Attach global company identity using only the local read-only database."""
    copied = [dict(record) for record in records]
    if not copied:
        return copied
    settings = DashboardSettings.from_env(project_root)
    owned_engine = engine is None
    read_engine = engine or create_read_only_erp_engine(settings.database_url)
    try:
        with Session(read_engine) as session:
            return enrich_product_master_records(
                session,
                copied,
                sku_field=sku_field,
                as_of_date=as_of_date,
            )
    except SQLAlchemyError:
        for record in copied:
            record.update(
                {
                    "company_sku": None,
                    "company_product_name": None,
                    "cost_rmb": None,
                    "cost_effective_date": None,
                }
            )
        return copied
    finally:
        if owned_engine:
            read_engine.dispose()


def _competitor_card_category_records(
    records: Sequence[Mapping[str, Any]],
    category_paths: Mapping[str, Sequence[Mapping[str, str | None]]],
) -> list[dict[str, Any]]:
    """Attach persisted broad-to-specific category paths to read-only card rows."""

    enriched: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        plid = str(item.get("plid") or "").strip()
        item["类目路径"] = [
            dict(breadcrumb)
            for breadcrumb in category_paths.get(plid, ())
        ]
        enriched.append(item)
    return enriched


def _product_master_competitor_store_records(
    project_root: Path,
    records: Sequence[Mapping[str, Any]],
    *,
    engine: Engine | None = None,
) -> list[dict[str, Any]]:
    """Enrich only the own-store identities nested in private-link cards."""
    copied = [dict(record) for record in records]
    nested_locations: list[tuple[int, str, int]] = []
    nested_records: list[dict[str, Any]] = []
    for item_index, item in enumerate(copied):
        for field in ("自有报价", "对比报价"):
            offers = item.get(field)
            if not isinstance(offers, list):
                continue
            item[field] = [dict(offer) for offer in offers if isinstance(offer, Mapping)]
            for offer_index, offer in enumerate(item[field]):
                if field == "对比报价" and offer.get("报价来源") != "seller_api":
                    continue
                nested_locations.append((item_index, field, offer_index))
                nested_records.append(offer)
    enriched = _product_master_records(
        project_root,
        nested_records,
        sku_field="SKU",
        engine=engine,
    )
    for (item_index, field, offer_index), offer in zip(
        nested_locations,
        enriched,
        strict=True,
    ):
        copied[item_index][field][offer_index] = offer
    for item in copied:
        own_offers = item.get("自有报价")
        own_offers = own_offers if isinstance(own_offers, list) else []
        company_skus = sorted(
            {
                str(offer.get("company_sku") or "").strip()
                for offer in own_offers
                if isinstance(offer, Mapping)
                and str(offer.get("company_sku") or "").strip()
            },
            key=str.casefold,
        )
        item["company_skus"] = company_skus
        item["company_sku"] = company_skus[0] if len(company_skus) == 1 else None
    return copied


def _own_store_sales_comparison_records(
    project_root: Path,
    records: Sequence[Mapping[str, Any]],
    *,
    own_store_codes: set[str],
    through: date,
    engine: Engine | None = None,
) -> list[dict[str, Any]]:
    """Attach fixed official-sales totals to all private-link cards in bulk."""

    copied = [dict(record) for record in records]
    plids = {
        str(record.get("plid") or "").strip()
        for record in copied
        if str(record.get("plid") or "").strip()
    }
    empty_windows = {"7": None, "15": None, "30": None, "60": None, "90": None}
    if not copied or not plids or not own_store_codes:
        for record in copied:
            record.update(
                {
                    "自有官方销量": dict(empty_windows),
                    "自有官方销量截至": None,
                    "自有官方销量店铺数": 0,
                    "自有官方销量Offer数": 0,
                }
            )
        return copied

    settings = DashboardSettings.from_env(project_root)
    window_start = through - timedelta(days=max(OWN_STORE_SALES_WINDOW_DAYS) - 1)
    path = sqlite_database_path(settings.database_url)
    if path is not None and not path.exists():
        series_by_plid: dict[str, list[dict[str, Any]]] = {}
    else:
        owned_engine = engine is None
        read_engine = engine or create_read_only_erp_engine(settings.database_url)
        try:
            with Session(read_engine) as session:
                series_by_plid = build_own_store_sales_series_bulk(
                    session,
                    plids=plids,
                    store_codes=own_store_codes,
                    through=through,
                    start=window_start,
                )
        except SQLAlchemyError:
            series_by_plid = {}
        finally:
            if owned_engine:
                read_engine.dispose()

    for record in copied:
        plid = str(record.get("plid") or "").strip()
        aggregate = aggregate_own_store_sales_series(
            series_by_plid.get(plid, []),
            start=window_start,
        )
        if aggregate is None:
            record.update(
                {
                    "自有官方销量": dict(empty_windows),
                    "自有官方销量截至": None,
                    "自有官方销量店铺数": 0,
                    "自有官方销量Offer数": 0,
                }
            )
            continue
        record.update(
            {
                "自有官方销量": summarize_own_store_sales_windows(aggregate),
                "自有官方销量截至": aggregate["through_date"],
                "自有官方销量店铺数": int(aggregate.get("store_count") or 0),
                "自有官方销量Offer数": len(aggregate.get("offer_ids") or []),
            }
        )
    return copied


def _load_own_store_returns(
    project_root: Path,
    *,
    plid: str,
    own_store_codes: set[str],
    engine: Engine | None = None,
) -> dict[str, Any]:
    """Load all locally collected returns for one PLID in the authorized stores."""
    today = datetime.now(ZoneInfo("Africa/Johannesburg")).date()
    if not own_store_codes:
        return _empty_own_store_return_payload(
            message="当前账号没有可读取的店铺范围。",
        )
    settings = DashboardSettings.from_env(project_root)
    path = sqlite_database_path(settings.database_url)
    if path is not None and not path.exists():
        return _empty_own_store_return_payload(
            message="本地退货数据库尚未建立。",
        )
    owned_engine = engine is None
    read_engine = engine or create_read_only_erp_engine(settings.database_url)
    try:
        with Session(read_engine) as session:
            store_names = {
                store.code: store.display_name
                for store in session.scalars(
                    select(ErpStore).where(ErpStore.code.in_(own_store_codes))
                )
            }
        rows: list[dict[str, Any]] = []
        statuses: list[dict[str, Any]] = []
        counters: list[dict[str, Any]] = []
        for store_code in sorted(own_store_codes):
            store_name = store_names.get(store_code, store_code)
            with store_scope(store_code), Session(read_engine) as session:
                store_rows = enrich_product_master_records(
                    session,
                    load_store_return_rows(session, plid=plid),
                    as_of_date=today,
                )
                counter = load_offer_returned_30_day_counter(session, plid=plid)
            for source in store_rows:
                item = dict(source)
                item["store_code"] = store_code
                item["store_name"] = store_name
                item["store_scope_key"] = f"{store_code}:{item.get('seller_return_id')}"
                rows.append(item)
            counters.append(
                {**counter, "store_code": store_code, "store_name": store_name}
            )
        filtered = filter_return_rows(rows)
        # These bounds describe coverage of the observed history, not a row filter
        # or a claim that the platform's entire lifetime has been collected.
        observed_dates = [
            date.fromisoformat(str(row["return_date"]))
            for row in filtered
            if row.get("return_date")
        ]
        selected_start = min([today, *observed_dates])
        selected_end = max([today, *observed_dates])
        undated_store_codes = {
            row["store_code"] for row in filtered if not row.get("return_date")
        }
        for store_code in sorted(own_store_codes):
            with store_scope(store_code), Session(read_engine) as session:
                status = load_return_collection_status(
                    session, start_date=selected_start, end_date=selected_end
                )
            if store_code in undated_store_codes and status["data_status"] == "collected":
                status = {**status, "data_status": "partial"}
            statuses.append(
                {
                    **status,
                    "store_code": store_code,
                    "store_name": store_names.get(store_code, store_code),
                }
            )
        return {
            "date_scope": "all_collected",
            "range_start": selected_start.isoformat(),
            "range_end": selected_end.isoformat(),
            "date_basis": "Africa/Johannesburg",
            "data_status": _combined_return_data_status(statuses),
            "store_statuses": statuses,
            "offer_returned_30_days": _aggregate_offer_return_counter(counters),
            "summary": summarize_return_rows(filtered),
            "filters": return_filter_options(filtered),
            "items": filtered,
            "total": len(filtered),
            "page": 1,
            "page_size": len(filtered),
            "message": (
                "展示该商品全部已采集历史退货明细。"
                if filtered
                else "本地尚无该商品退货明细，不代表平台全部历史无退货。"
            ),
            "source_notice": (
                "仅展示本地已采集明细，不代表平台全部历史完整；Offers 为独立滚动30天计数。"
            ),
        }
    except SQLAlchemyError:
        return _empty_own_store_return_payload(
            message="退货明细暂时不可读，请查看采集状态。",
            data_status="unavailable",
        )
    finally:
        if owned_engine:
            read_engine.dispose()


def _empty_own_store_return_payload(
    *, message: str, data_status: str = "uncollected"
) -> dict[str, Any]:
    return {
        **_empty_return_payload(
            start_date=None, end_date=None, message=message, data_status=data_status
        ),
        "date_scope": "all_collected",
        "range_start": "",
        "range_end": "",
        "page_size": 0,
    }


def _combined_return_data_status(statuses: Sequence[Mapping[str, Any]]) -> str:
    values = {str(status.get("data_status") or "uncollected") for status in statuses}
    if not values or values == {"uncollected"}:
        return "uncollected"
    if values == {"collected"}:
        return "collected"
    if values == {"failed"}:
        return "failed"
    if values == {"unavailable"}:
        return "unavailable"
    return "partial"


def _aggregate_offer_return_counter(
    counters: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    known_units = [
        int(counter["units"])
        for counter in counters
        if counter.get("units") is not None
    ]
    captures = [
        str(counter.get("captured_at"))
        for counter in counters
        if counter.get("captured_at")
    ]
    return {
        "units": sum(known_units) if known_units else None,
        "covered_offer_count": sum(
            int(counter.get("covered_offer_count") or 0) for counter in counters
        ),
        "offer_count": sum(int(counter.get("offer_count") or 0) for counter in counters),
        "covered_store_count": len(known_units),
        "store_count": len(counters),
        "captured_at": max(captures) if captures else None,
        "metric": "quantity_returned_30_days",
        "window": "rolling_30_days",
    }


def _empty_return_payload(
    *,
    start_date: date | None,
    end_date: date | None,
    message: str,
    data_status: str = "uncollected",
) -> dict[str, Any]:
    selected_end = end_date or datetime.now(ZoneInfo("Africa/Johannesburg")).date()
    selected_start = start_date or selected_end - timedelta(days=29)
    return {
        "range_start": selected_start.isoformat(),
        "range_end": selected_end.isoformat(),
        "date_basis": "Africa/Johannesburg",
        "data_status": data_status,
        "store_statuses": [],
        "offer_returned_30_days": {
            "units": None,
            "covered_offer_count": 0,
            "offer_count": 0,
            "covered_store_count": 0,
            "store_count": 0,
            "captured_at": None,
            "metric": "quantity_returned_30_days",
            "window": "rolling_30_days",
        },
        "summary": {
            **summarize_return_rows([]),
            "removal_lifecycle": summarize_removal_lifecycles([]),
        },
        "removal_order_tracking": {
            "data_status": "uncollected",
            "store_statuses": [],
            "w8": {
                "data_status": "uncollected",
                "synced_at": None,
                "return_order_count": 0,
                "message": "尚无可读取的移除单或长睿退货快照",
            },
        },
        "removal_orders": {
            "data_status": "uncollected",
            "counts": {
                "total": 0,
                "submitted": 0,
                "pickup_ready": 0,
                "closed": 0,
            },
            "items": [],
            "warnings": [],
            "source_notice": (
                "尚无本地 Seller Portal Manage Removal Orders 快照；未知不能解释为 0 单。"
            ),
        },
        "filters": return_filter_options([]),
        "items": [],
        "total": 0,
        "page": 1,
        "page_size": 20,
        "message": message,
        "source_notice": (
            "明细来自 Seller API /returns；未采集状态不能解释为零退货。"
        ),
    }


def _load_company_inventory(
    project_root: Path,
    *,
    plid: str,
    own_store_codes: set[str],
    engine: Engine | None = None,
) -> dict[str, Any]:
    settings = DashboardSettings.from_env(project_root)
    owned_engine = engine is None
    read_engine = engine or create_read_only_erp_engine(settings.database_url)
    try:
        return load_company_inventory_for_plid(
            read_engine,
            plid=plid,
            store_codes=own_store_codes,
        )
    except SQLAlchemyError:
        return {
            "items": [],
            "company_sku_count": 0,
            "store_codes": sorted(own_store_codes),
            "w8_shared_once": True,
            "stage_totals_are_additive": False,
            "message": "公司 SKU 库存快照暂时不可读。",
        }
    finally:
        if owned_engine:
            read_engine.dispose()


def _load_own_store_profitability(
    project_root: Path,
    *,
    plid: str,
    own_store_codes: set[str],
    rate_service: CnyZarRateService,
    cost_as_of: date,
    fee_window_end: date,
    engine: Engine | None = None,
) -> dict[str, Any]:
    if not own_store_codes:
        return empty_own_store_profitability(
            store_codes=set(),
            fee_window_end=fee_window_end,
            message="当前账号没有可用于利润计算的已授权店铺。",
        )
    settings = DashboardSettings.from_env(project_root)
    path = sqlite_database_path(settings.database_url)
    if path is not None and not path.exists():
        return empty_own_store_profitability(
            store_codes=own_store_codes,
            fee_window_end=fee_window_end,
            message="本地业务数据库尚不存在，暂不能读取自有 Offer 成本与利润。",
        )
    owned_engine = engine is None
    read_engine = engine or create_read_only_erp_engine(settings.database_url)
    try:
        return load_own_store_profitability_payload(
            read_engine,
            plid=plid,
            store_codes=own_store_codes,
            rate_service=rate_service,
            cost_as_of=cost_as_of,
            fee_window_end=fee_window_end,
        )
    except SQLAlchemyError:
        return empty_own_store_profitability(
            store_codes=own_store_codes,
            fee_window_end=fee_window_end,
            message="自有 Offer 的本地成本或 Seller Sales 费用数据暂时不可读。",
        )
    finally:
        if owned_engine:
            read_engine.dispose()


def _load_own_store_sales(
    project_root: Path,
    *,
    plid: str,
    own_store_codes: set[str],
    through: date,
    engine: Engine | None = None,
) -> list[dict[str, Any]]:
    if not own_store_codes:
        return []
    settings = DashboardSettings.from_env(project_root)
    path = sqlite_database_path(settings.database_url)
    if path is not None and not path.exists():
        return []
    owned_engine = engine is None
    read_engine = engine or create_read_only_erp_engine(settings.database_url)
    try:
        with Session(read_engine) as session:
            return build_own_store_sales_series(
                session,
                plid=plid,
                store_codes=own_store_codes,
                through=through,
            )
    finally:
        if owned_engine:
            read_engine.dispose()


def _load_own_store_sales_detail(
    project_root: Path,
    *,
    plid: str,
    own_store_codes: set[str],
    through: date,
    engine: Engine | None = None,
) -> dict[str, list[dict[str, Any]]]:
    if not own_store_codes:
        return {"link_series": [], "variant_series": []}
    settings = DashboardSettings.from_env(project_root)
    path = sqlite_database_path(settings.database_url)
    if path is not None and not path.exists():
        return {"link_series": [], "variant_series": []}
    owned_engine = engine is None
    read_engine = engine or create_read_only_erp_engine(settings.database_url)
    try:
        with Session(read_engine) as session:
            return build_own_store_sales_detail(
                session,
                plid=plid,
                store_codes=own_store_codes,
                through=through,
            )
    finally:
        if owned_engine:
            read_engine.dispose()


def _load_own_store_traffic(
    project_root: Path,
    *,
    plid: str,
    own_store_codes: set[str],
    start_date: date | None,
    end_date: date | None,
    engine: Engine | None = None,
) -> list[dict[str, Any]]:
    if not own_store_codes:
        return []
    settings = DashboardSettings.from_env(project_root)
    path = sqlite_database_path(settings.database_url)
    if path is not None and not path.exists():
        return []
    owned_engine = engine is None
    read_engine = engine or create_read_only_erp_engine(settings.database_url)
    try:
        with Session(read_engine) as session:
            return build_own_store_traffic_series(
                session,
                plid=plid,
                store_codes=own_store_codes,
                start_date=start_date,
                end_date=end_date,
            )
    finally:
        if owned_engine:
            read_engine.dispose()


def _read_store_identities_for_request(
    request: Request,
    selected_store_scope: Literal["current", "all", "operating"],
) -> tuple[StoreIdentity, ...]:
    """Resolve one explicit read scope without widening account authorization."""
    if selected_store_scope != "current":
        return _multi_store_identities_for_request(request, selected_store_scope)
    selected_store = getattr(request.state, "erp_store", None)
    if (
        selected_store is None
        or not selected_store.active
        or not selected_store.data_connected
    ):
        return ()
    return (selected_store,)


def _tag_store_record(
    record: Mapping[str, Any],
    store: StoreIdentity,
    *,
    identity_fields: Sequence[str] = ("offer_id",),
) -> dict[str, Any]:
    tagged = dict(record)
    tagged["store_code"] = store.code
    tagged["store_name"] = store.display_name
    identity = next(
        (
            str(tagged.get(field))
            for field in identity_fields
            if tagged.get(field) not in (None, "")
        ),
        "record",
    )
    tagged["store_scope_key"] = f"{store.code}:{identity}"
    return tagged


def _tag_store_records(
    records: Sequence[Mapping[str, Any]],
    store: StoreIdentity,
    *,
    identity_fields: Sequence[str] = ("offer_id",),
) -> list[dict[str, Any]]:
    return [
        _tag_store_record(record, store, identity_fields=identity_fields)
        for record in records
    ]


def _decorate_search_ranking_detail(
    project_root: Path,
    payload: Mapping[str, Any],
    store: StoreIdentity,
) -> dict[str, Any]:
    decorated = dict(payload)
    product = decorated.get("product")
    enriched = _product_master_records(
        project_root,
        [product] if isinstance(product, Mapping) else [],
    )
    decorated["product"] = (
        _tag_store_record(enriched[0], store) if enriched else {}
    )
    family = decorated.get("variant_family")
    if isinstance(family, Mapping):
        decorated_family = dict(family)
        variants = _product_master_records(
            project_root,
            list(decorated_family.get("variants") or []),
        )
        decorated_family["variants"] = _tag_store_records(variants, store)
        decorated["variant_family"] = decorated_family
    return decorated


def _combined_store_dataset(
    settings: DashboardSettings,
    as_of: date,
    stores: Sequence[StoreIdentity],
    *,
    dataset_loader: Callable[
        [DashboardSettings, date], DashboardDataset
    ] = load_erp_dataset,
) -> tuple[DashboardDataset, dict[str, tuple[StoreIdentity, str]]]:
    """Combine authorized store frames with collision-proof internal Offer IDs."""
    scoped_datasets: list[DashboardDataset] = []
    offer_scope: dict[str, tuple[StoreIdentity, str]] = {}
    frame_fields = (
        "store_daily",
        "product_daily",
        "offer_current",
        "anomalies",
        "quality_events",
        "offer_history",
    )
    for store in stores:
        with store_scope(store.code):
            dataset = dataset_loader(settings, as_of)
        replacements: dict[str, pd.DataFrame] = {}
        for field_name in frame_fields:
            frame = getattr(dataset, field_name).copy()
            if "offer_id" in frame.columns:
                scoped_offer_ids: list[Any] = []
                for raw_offer_id in frame["offer_id"].tolist():
                    if raw_offer_id is None or pd.isna(raw_offer_id):
                        scoped_offer_ids.append(raw_offer_id)
                        continue
                    original_offer_id = str(raw_offer_id)
                    scoped_offer_id = f"{store.code}\x1f{original_offer_id}"
                    offer_scope[scoped_offer_id] = (store, original_offer_id)
                    scoped_offer_ids.append(scoped_offer_id)
                frame["offer_id"] = scoped_offer_ids
            replacements[field_name] = frame
        scoped_datasets.append(replace(dataset, **replacements))
    if not scoped_datasets:
        empty = pd.DataFrame()
        return (
            DashboardDataset(
                store_daily=empty.copy(),
                product_daily=empty.copy(),
                offer_current=empty.copy(),
                anomalies=empty.copy(),
                quality_events=empty.copy(),
                offer_history=empty.copy(),
            ),
            offer_scope,
        )
    combined_frames = {
        field_name: pd.concat(
            [getattr(dataset, field_name) for dataset in scoped_datasets],
            ignore_index=True,
            sort=False,
        )
        for field_name in frame_fields
    }
    return replace(scoped_datasets[0], **combined_frames), offer_scope


def _aggregate_anomaly_payloads(
    project_root: Path,
    store_payloads: Sequence[tuple[StoreIdentity, Mapping[str, Any]]],
    *,
    requested_as_of: date,
    completed_through: date,
    selected_store_scope: Literal["current", "all", "operating"],
) -> dict[str, Any]:
    first_payload = store_payloads[0][1] if store_payloads else {}
    sudden: list[dict[str, Any]] = []
    slow: list[dict[str, Any]] = []
    daily_bad_reviews: list[dict[str, Any]] = []
    poor_review_quality: list[dict[str, Any]] = []
    return_product_totals: list[dict[str, Any]] = []
    return_coverages: list[dict[str, Any]] = []
    stock_groups: dict[str, list[dict[str, Any]]] = {
        "not_buyable": [],
        "disabled_by_takealot": [],
        "disabled_by_seller": [],
    }
    store_data_through: dict[str, str | None] = {}
    for store, payload in store_payloads:
        store_data_through[store.code] = payload.get("data_through")
        sudden.extend(_tag_store_records(payload.get("sudden_sales_stop", []), store))
        slow.extend(_tag_store_records(payload.get("slow_moving", []), store))
        daily_bad_reviews.extend(
            _tag_store_records(
                payload.get("daily_bad_reviews", []),
                store,
                identity_fields=("plid",),
            )
        )
        poor_review_quality.extend(
            _tag_store_records(
                payload.get("poor_review_quality", []),
                store,
                identity_fields=("plid",),
            )
        )
        return_product_totals.extend(
            _tag_store_records(
                payload.get(
                    "return_product_totals",
                    payload.get("high_returns", []),
                ),
                store,
                identity_fields=("company_sku",),
            )
        )
        coverage = dict(payload.get("return_coverage") or {})
        coverage["store_code"] = store.code
        coverage["store_name"] = store.display_name
        return_coverages.append(coverage)
        raw_groups = payload.get("stock_status_anomalies", {})
        for key in stock_groups:
            stock_groups[key].extend(
                _tag_store_records(raw_groups.get(key, []), store)
            )
    sudden = _product_master_records(project_root, sudden, as_of_date=requested_as_of)
    slow = _product_master_records(project_root, slow, as_of_date=requested_as_of)
    daily_bad_reviews = merge_review_anomaly_items(
        _product_master_records(
            project_root,
            daily_bad_reviews,
            as_of_date=requested_as_of,
        )
    )
    poor_review_quality = merge_review_anomaly_items(
        _product_master_records(
            project_root,
            poor_review_quality,
            as_of_date=requested_as_of,
        )
    )
    for key, records in stock_groups.items():
        stock_groups[key] = _product_master_records(
            project_root,
            records,
            as_of_date=requested_as_of,
        )
    rules = dict(first_payload.get("rules") or {})
    slow_options = [int(value) for value in rules.get("slow_day_options", [])]
    high_returns = merge_return_anomaly_items(
        return_product_totals,
        minimum_units=max(1, int(rules.get("high_return_min_units") or 5)),
    )
    return_coverage = merge_return_coverage(return_coverages)
    valid_data_dates = [value for value in store_data_through.values() if value]
    review_discovery_dates = [
        str(payload.get("review_discovery_through"))
        for _, payload in store_payloads
        if payload.get("review_discovery_through")
    ]
    collection_times = {
        field_name: _latest_text(
            [
                (payload.get("collection_times") or {}).get(field_name)
                for _, payload in store_payloads
                if isinstance(payload.get("collection_times"), Mapping)
            ]
        )
        for field_name in ("offers_at", "sales_at", "reviews_at", "returns_at")
    }
    collection_times["latest_at"] = _latest_text(list(collection_times.values()))
    return {
        "requested_as_of": requested_as_of.isoformat(),
        "completed_through": completed_through.isoformat(),
        "data_through": min(valid_data_dates) if valid_data_dates else None,
        "store_data_through": store_data_through,
        "store_scope": selected_store_scope,
        "store_count": len(store_payloads),
        "date_basis": first_payload.get("date_basis", "Africa/Johannesburg"),
        "collection_times": collection_times,
        "sales_zero_evidence": first_payload.get(
            "sales_zero_evidence",
            "verified_complete_business_days_only",
        ),
        "rules": rules,
        "summary": {
            "sudden_sales_stop": len(sudden),
            "not_buyable_with_stock": len(stock_groups["not_buyable"]),
            "disabled_by_takealot_with_stock": len(
                stock_groups["disabled_by_takealot"]
            ),
            "disabled_by_seller_with_stock": len(
                stock_groups["disabled_by_seller"]
            ),
            "slow_moving_by_days": {
                str(days): sum(
                    int(item.get("no_sales_days") or 0) >= days for item in slow
                )
                for days in slow_options
            },
            "daily_bad_reviews": len(daily_bad_reviews),
            "poor_review_quality": len(poor_review_quality),
            "high_returns": len(high_returns),
        },
        "sudden_sales_stop": sudden,
        "stock_status_anomalies": stock_groups,
        "slow_moving": slow,
        "daily_bad_reviews": daily_bad_reviews,
        "poor_review_quality": poor_review_quality,
        "review_discovery_through": (
            max(review_discovery_dates) if review_discovery_dates else None
        ),
        "return_coverage": return_coverage,
        "high_returns": high_returns,
    }


def _latest_text(values: Sequence[Any]) -> str | None:
    normalized = [str(value) for value in values if value not in (None, "")]
    return max(normalized) if normalized else None


def _aggregate_logistics_payloads(
    store_payloads: Sequence[tuple[StoreIdentity, Mapping[str, Any]]],
    selected_store_scope: Literal["current", "all", "operating"],
) -> dict[str, Any]:
    if not store_payloads:
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "cache_ttl_seconds": 0,
            "cache_age_seconds": 0,
            "automatic_page_refresh": False,
            "store_scope": selected_store_scope,
            "store_count": 0,
            "w8": {},
            "takealot": {},
            "matching": {},
            "boundaries": [],
        }
    first_payload = store_payloads[0][1]
    w8_candidates = [dict(payload.get("w8") or {}) for _, payload in store_payloads]
    w8 = max(
        w8_candidates,
        key=lambda item: (
            str(item.get("data_source") or "unavailable") != "unavailable",
            str(item.get("synced_at") or ""),
        ),
    )
    takealot_payloads = [dict(payload.get("takealot") or {}) for _, payload in store_payloads]
    takealot_summary: defaultdict[str, int] = defaultdict(int)
    recent_shipments: list[dict[str, Any]] = []
    takealot_warnings: list[str] = []
    candidate_fields: dict[str, list[dict[str, Any]]] = {
        "high_confidence_candidates": [],
        "medium_confidence_candidates": [],
        "low_confidence_candidates": [],
        "confirmed_links": [],
        "split_batch_groups": [],
        "items": [],
    }
    matching_warnings: list[str] = []
    boundaries: list[str] = []
    for store, payload in store_payloads:
        takealot = dict(payload.get("takealot") or {})
        for key, value in dict(takealot.get("summary") or {}).items():
            takealot_summary[str(key)] += int(value or 0)
        recent_shipments.extend(
            _tag_store_records(
                takealot.get("recent_shipments", []),
                store,
                identity_fields=("shipment_id", "reference"),
            )
        )
        takealot_warnings.extend(
            f"{store.display_name}：{warning}"
            for warning in takealot.get("warnings", [])
        )
        matching = dict(payload.get("matching") or {})
        for field_name in candidate_fields:
            identity_fields: Sequence[str]
            if field_name == "confirmed_links":
                identity_fields = ("id", "takealot_shipment_id")
            elif field_name == "split_batch_groups":
                identity_fields = ("w8_order_no",)
            else:
                identity_fields = ("takealot_shipment_id", "w8_order_no")
            candidate_fields[field_name].extend(
                _tag_store_records(
                    matching.get(field_name, []),
                    store,
                    identity_fields=identity_fields,
                )
            )
        matching_warnings.extend(
            f"{store.display_name}：{warning}"
            for warning in matching.get("warnings", [])
        )
        boundaries.extend(str(value) for value in payload.get("boundaries", []))
    recent_shipments.sort(
        key=lambda item: str(item.get("created_at") or item.get("due_date") or ""),
        reverse=True,
    )
    connected_count = sum(bool(item.get("connected")) for item in takealot_payloads)
    live_count = sum(bool(item.get("live_connected")) for item in takealot_payloads)
    sources = {str(item.get("data_source") or "unavailable") for item in takealot_payloads}
    takealot_source = (
        "live_api"
        if sources == {"live_api"}
        else "local_database"
        if sources - {"unavailable"}
        else "unavailable"
    )
    takealot = {
        "connected": connected_count > 0,
        "live_connected": live_count == len(store_payloads),
        "data_source": takealot_source,
        "synced_at": _latest_text([item.get("synced_at") for item in takealot_payloads]),
        "snapshot_saved": all(bool(item.get("snapshot_saved")) for item in takealot_payloads),
        "refresh_attempted": any(
            bool(item.get("refresh_attempted")) for item in takealot_payloads
        ),
        "message": (
            f"已合并 {len(store_payloads)} 个授权店铺的本地 Takealot 货件快照"
            if selected_store_scope != "current"
            else takealot_payloads[0].get("message")
        ),
        "summary": dict(takealot_summary),
        "recent_shipments": recent_shipments,
        "warnings": takealot_warnings,
    }
    direct_items = candidate_fields["items"]
    confirmed_links = candidate_fields["confirmed_links"]
    matched_w8 = {
        str(item.get("w8_order_no") or "")
        for item in [*direct_items, *confirmed_links]
        if item.get("w8_order_no")
    }
    matched_shipments = {
        (str(item.get("store_code") or ""), str(item.get("takealot_shipment_id") or ""))
        for item in [*direct_items, *confirmed_links]
        if item.get("takealot_shipment_id") not in (None, "")
    }
    total_w8_inbound = int(dict(w8.get("summary") or {}).get("inbound_orders") or 0)
    matching = {
        "method": (
            "按店铺分别执行明确编号、分级候选与人工确认规则；W8 共享数据只展示一次。"
            if selected_store_scope != "current"
            else str(dict(first_payload.get("matching") or {}).get("method") or "")
        ),
        "direct_match_count": len(direct_items),
        "matched_w8_inbound": len(matched_w8),
        "matched_takealot_shipments": len(matched_shipments),
        "unmatched_w8_inbound": max(0, total_w8_inbound - len(matched_w8)),
        "unmatched_takealot_shipments": max(
            0,
            int(takealot_summary.get("shipments", 0)) - len(matched_shipments),
        ),
        "confirmed_link_count": len(confirmed_links),
        "confirmed_links": confirmed_links,
        "high_confidence_candidate_count": len(
            candidate_fields["high_confidence_candidates"]
        ),
        "high_confidence_candidates": candidate_fields["high_confidence_candidates"],
        "medium_confidence_candidate_count": len(
            candidate_fields["medium_confidence_candidates"]
        ),
        "medium_confidence_candidates": candidate_fields["medium_confidence_candidates"],
        "low_confidence_candidate_count": len(
            candidate_fields["low_confidence_candidates"]
        ),
        "low_confidence_candidates": candidate_fields["low_confidence_candidates"],
        "split_batch_group_count": len(candidate_fields["split_batch_groups"]),
        "split_batch_groups": candidate_fields["split_batch_groups"],
        "warnings": matching_warnings,
        "items": direct_items,
    }
    unique_boundaries = list(dict.fromkeys(boundaries))
    if selected_store_scope != "current":
        unique_boundaries.insert(
            0,
            "全部店铺只读各店最近成功快照；W8 共享仓只展示一次，Takealot 货件按店铺保留身份。",
        )
    return {
        "generated_at": _latest_text(
            [payload.get("generated_at") for _, payload in store_payloads]
        )
        or datetime.now(UTC).isoformat(),
        "cache_ttl_seconds": max(
            int(payload.get("cache_ttl_seconds") or 0) for _, payload in store_payloads
        ),
        "cache_age_seconds": max(
            float(payload.get("cache_age_seconds") or 0) for _, payload in store_payloads
        ),
        "automatic_page_refresh": False,
        "store_scope": selected_store_scope,
        "store_count": len(store_payloads),
        "w8": w8,
        "takealot": takealot,
        "matching": matching,
        "boundaries": unique_boundaries,
    }


def _aggregate_platform_warehouse_payloads(
    store_payloads: Sequence[tuple[StoreIdentity, Mapping[str, Any]]],
    selected_store_scope: Literal["current", "all", "operating"],
) -> dict[str, Any]:
    if not store_payloads:
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "store_scope": selected_store_scope,
            "store_count": 0,
            "offers": [],
            "drafts": [],
            "platform_shipments": [],
            "platform_snapshot_synced_at": None,
        }
    first_payload = store_payloads[0][1]
    offers: list[dict[str, Any]] = []
    drafts: list[dict[str, Any]] = []
    shipments: list[dict[str, Any]] = []
    for store, payload in store_payloads:
        offers.extend(_tag_store_records(payload.get("offers", []), store))
        drafts.extend(
            _tag_store_records(
                payload.get("drafts", []),
                store,
                identity_fields=("id", "draft_number"),
            )
        )
        shipments.extend(
            _tag_store_records(
                payload.get("platform_shipments", []),
                store,
                identity_fields=("shipment_id", "reference"),
            )
        )
    capability = dict(first_payload.get("capability") or {})
    portal = dict(first_payload.get("portal") or {})
    if selected_store_scope != "current":
        capability.update(
            {
                "write_mode": "disabled_by_default",
                "official_shipment_write_supported": False,
                "message": (
                    f"当前合并查看 {len(store_payloads)} 个授权店铺，仅展示本地商品、草稿与平台货件快照；"
                    "创建、验证码和 Shipment 状态操作必须先切换到明确单店。"
                ),
            }
        )
        portal.update(
            {
                "enabled": False,
                "authenticated": False,
                "requires_otp": False,
                "otp_destination": None,
                "expires_at": None,
                "identity": None,
                "credential_configured": False,
                "credential_email": None,
                "credential_error": None,
            }
        )
    return {
        "generated_at": _latest_text(
            [payload.get("generated_at") for _, payload in store_payloads]
        )
        or datetime.now(UTC).isoformat(),
        "store_scope": selected_store_scope,
        "store_count": len(store_payloads),
        "capability": capability,
        "portal": portal,
        "offers": offers,
        "drafts": drafts,
        "platform_shipments": shipments,
        "platform_snapshot_synced_at": _latest_text(
            [
                payload.get("platform_snapshot_synced_at")
                for _, payload in store_payloads
            ]
        ),
    }


def _own_store_codes_for_request(
    request: Request,
    own_store_scope: Literal["current", "all", "operating"],
) -> set[str]:
    """Resolve connected own-store visibility without exposing unauthorized stores."""
    accessible_codes = {
        store.code
        for store in request.state.erp_user.accessible_stores
        if store.active and store.data_connected
    }
    if own_store_scope == "all":
        return accessible_codes
    if own_store_scope == "operating":
        operating_store_ids = set(request.state.erp_user.assigned_store_ids)
        return {
            store.code
            for store in request.state.erp_user.accessible_stores
            if (
                store.id in operating_store_ids
                and store.active
                and store.data_connected
            )
        }
    selected_store = getattr(request.state, "erp_store", None)
    if selected_store is None or selected_store.code not in accessible_codes:
        return set()
    return {selected_store.code}


def _multi_store_identities_for_request(
    request: Request,
    store_scope_value: Literal["all", "operating"],
) -> tuple[StoreIdentity, ...]:
    """Resolve an authorized multi-store identity set without widening access."""
    operating_store_ids = set(request.state.erp_user.assigned_store_ids)
    return tuple(
        store
        for store in request.state.erp_user.accessible_stores
        if (
            store.active
            and store.data_connected
            and (
                store_scope_value == "all"
                or store.id in operating_store_ids
            )
        )
    )


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
            detail="Seller Portal 登录、同步、预审和写入只允许从 ERP 服务器本机执行",
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
