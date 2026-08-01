"""Authenticated FastAPI application for the unified local ERP."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, Literal
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
    reminder_payload,
    revert_confirmation,
    reopen_stock_alert,
    save_manual_candidate,
    save_operator_note,
    unresolved_locations,
    update_operator_note,
)
from takealot_ops.erp.daily_report_live import daily_report_event_stream
from takealot_ops.erp.permissions import (
    COMPETITORS_COLLECT,
    COMPETITORS_VIEW,
    DAILY_REPORT_EXPORT,
    DAILY_REPORT_MANAGE,
    DAILY_REPORT_VIEW,
    NFT102_MANAGE,
    REFRESH_RUN,
    REPORTS_GENERATE,
    REPORTS_VIEW,
    STORE_VIEW,
    USERS_MANAGE,
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
from takealot_ops.nft102_portal import (
    generate_nft102_from_baseline,
    inspect_nft102_upload,
    persist_nft102_baseline,
)
from takealot_ops.reporting import generate_daily_reports
from takealot_ops.scheduler import verify_database_integrity
from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.migrations import create_engine_for_settings, create_schema
from takealot_ops.storage.models import (
    CollectionRun,
    CompetitorSnapshot,
    CompetitorTarget,
    CompetitorTargetAudit,
    DailyProductMetric,
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
    reason: str = Field(default="", max_length=500)


class CompetitorTargetRequest(BaseModel):
    """One persisted Takealot competitor product URL."""

    url: str = Field(min_length=1, max_length=2000)


class CompetitorTargetPriorityRequest(BaseModel):
    """Describe why an operator is adding one priority collection attempt."""

    source: Literal["manual", "manual_retry"] = "manual"


class ExportRequest(BaseModel):
    """One explicit report export request."""

    as_of: date


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
    app.state.auth_manager = auth
    app.state.product_thumbnail_cache = product_thumbnails

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
        if (
            _requires_connected_store_access(path)
            and not session.user.can_access_connected_store()
        ):
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
    def summary(as_of: date = Query(default_factory=date.today)) -> dict[str, Any]:
        settings = DashboardSettings.from_env(root)
        return build_summary_payload(load_erp_dataset(settings, as_of), as_of)

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
            result = run_dashboard_refresh(root)
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
        start_date: date | None = Query(default=None),
        end_date: date | None = Query(default=None),
    ) -> dict[str, object]:
        dataset = _load_competitor_dataset(
            root,
            start_date=start_date,
            end_date=end_date,
        )
        return {
            "items": frame_records(dataset.current),
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

    @app.get("/api/competitors/targets")
    def competitor_targets() -> dict[str, list[dict[str, object]]]:
        settings = DashboardSettings.from_env(root)
        engine = create_read_only_erp_engine(settings.database_url)
        try:
            with Session(engine) as session:
                has_history = (
                    select(CompetitorSnapshot.id)
                    .where(CompetitorSnapshot.plid == CompetitorTarget.plid)
                    .exists()
                )
                target_rows = session.execute(
                    select(CompetitorTarget, has_history.label("has_history"))
                    .where(CompetitorTarget.active.is_(True))
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
                target = session.get(CompetitorTarget, plid)
                if target is not None and target.active:
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
        try:
            with Session(engine) as session:
                target = session.get(CompetitorTarget, plid)
                if target is None or not target.active:
                    raise HTTPException(status_code=404, detail=f"PLID{plid} 不在监控清单中")
                url = target.url
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
        if accepted and source == "manual_retry":
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
            "target_priority plid=%s user=%s batch=%s source=%s accepted=%s",
            plid,
            user.username,
            status["batch_id"],
            source,
            accepted,
        )
        return {"ok": True, "accepted": accepted, "status": status}

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
        start_date: date | None = Query(default=None),
        end_date: date | None = Query(default=None),
    ) -> dict[str, list[dict[str, Any]]]:
        dataset = _load_competitor_dataset(
            root,
            start_date=start_date,
            end_date=end_date,
        )
        history = dataset.history
        reviews = dataset.reviews
        variants = dataset.variants
        if not history.empty:
            history = history.loc[history["plid"].astype(str) == plid]
        if not reviews.empty:
            reviews = reviews.loc[reviews["plid"].astype(str) == plid]
        if not variants.empty:
            variants = variants.loc[variants["plid"].astype(str) == plid]
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
            )
        except CollectionBatchBusyError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc

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
                                with_stock_probe=payload.with_stock_probe,
                                visible_browser=payload.visible_browser,
                            )
                        added_targets = _sync_discovered_competitor_targets(
                            engine,
                            origin_plid=plid,
                            discovered_targets=result.discovered_targets,
                            user=user,
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
    def competitor_batch_event(
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
            )
        except CollectionBatchBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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
    unique_targets = {target.plid: target for target in discovered_targets}
    origin = unique_targets.get(origin_plid)
    if origin is None:
        return ()
    now = datetime.now(UTC)
    added: list[CompetitorDiscoveredTarget] = []
    with Session(engine) as session:
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
    if path.startswith("/api/erp/daily-report/export"):
        return DAILY_REPORT_VIEW if safe_method else DAILY_REPORT_EXPORT
    if path.startswith("/api/erp/daily-report"):
        return DAILY_REPORT_VIEW if safe_method else DAILY_REPORT_MANAGE
    if path.startswith("/api/erp/exports"):
        return REPORTS_VIEW if safe_method else REPORTS_GENERATE
    if path.startswith("/api/erp/nft102"):
        return NFT102_MANAGE
    if path.startswith("/api/competitors"):
        return COMPETITORS_VIEW if safe_method else COMPETITORS_COLLECT
    if path.startswith(
        (
            "/api/erp/summary",
            "/api/erp/products",
            "/api/erp/quadrants",
            "/api/erp/risks",
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
                "/api/erp/quadrants",
                "/api/erp/risks",
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
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        engine.dispose()


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
