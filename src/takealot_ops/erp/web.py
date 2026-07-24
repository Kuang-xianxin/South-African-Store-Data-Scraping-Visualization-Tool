"""Authenticated FastAPI application for the unified local ERP."""

from __future__ import annotations

import os
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Annotated, Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from takealot_ops.competitors.api import extract_plid
from takealot_ops.competitors.service import (
    CompetitorCollector,
    CompetitorDataset,
    load_competitor_dataset,
)
from takealot_ops.dashboard.refresh import run_dashboard_refresh
from takealot_ops.erp.auth import (
    OPERATOR_ROLES,
    SESSION_COOKIE,
    AuthConflictError,
    AuthInputError,
    AuthManager,
    IssuedSession,
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
from takealot_ops.storage.models import CollectionRun, DailyProductMetric


class CollectCompetitorRequest(BaseModel):
    """One explicit competitor collection request."""

    url: str = Field(min_length=1)
    with_stock_probe: bool = True
    visible_browser: bool = False


class ExportRequest(BaseModel):
    """One explicit report export request."""

    as_of: date


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class BootstrapRequest(LoginRequest):
    display_name: str = Field(default="", max_length=100)


class UserCreateRequest(BootstrapRequest):
    role: str


class UserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    password: str | None = Field(default=None, max_length=128)
    role: str | None = None
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


def create_app(project_root: Path | None = None) -> FastAPI:
    """Create the unified ERP API and attach its built Vue application."""
    root = (
        project_root
        or Path(os.environ.get("TAKEALOT_PROJECT_ROOT", Path.cwd()))
    ).resolve()
    auth = AuthManager(root)
    limiter = _LoginLimiter()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        auth.close()

    app = FastAPI(
        title="Takealot 本地运营 ERP",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.auth_manager = auth

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

        session = await run_in_threadpool(
            auth.resolve_session,
            request.cookies.get(SESSION_COOKIE),
        )
        if session is None:
            return JSONResponse(status_code=401, content={"detail": "请先登录"})
        request.state.erp_user = session.user
        request.state.erp_session = session

        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf_token = request.headers.get("X-CSRF-Token", "")
            if not csrf_token or csrf_token != session.csrf_token:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "请求校验失败，请刷新页面后重试"},
                )
            if path.startswith("/api/auth/users"):
                if session.user.role != "admin":
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "只有管理员可以管理用户"},
                    )
            elif path != "/api/auth/logout" and session.user.role not in OPERATOR_ROLES:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "当前账号只有查看权限"},
                )
        elif path.startswith("/api/auth/users") and session.user.role != "admin":
            return JSONResponse(
                status_code=403,
                content={"detail": "只有管理员可以管理用户"},
            )
        return await call_next(request)

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
    def auth_session(request: Request) -> dict[str, Any]:
        resolved = auth.resolve_session(request.cookies.get(SESSION_COOKIE))
        if resolved is None:
            raise HTTPException(status_code=401, detail="请先登录")
        return {
            "user": resolved.user.as_dict(),
            "csrf_token": resolved.csrf_token,
            "expires_at": resolved.expires_at.isoformat(),
        }

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

    @app.post("/api/auth/users")
    def auth_create_user(payload: UserCreateRequest) -> dict[str, Any]:
        try:
            user = auth.create_user(
                username=payload.username,
                display_name=payload.display_name,
                password=payload.password,
                role=payload.role,
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
                latest_metric = session.scalar(
                    select(func.max(DailyProductMetric.metric_date))
                )
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

    @app.get("/api/erp/risks")
    def risks(as_of: date = Query(default_factory=date.today)) -> dict[str, Any]:
        settings = DashboardSettings.from_env(root)
        return build_risk_payload(load_erp_dataset(settings, as_of), as_of)

    @app.post("/api/erp/refresh")
    def refresh() -> dict[str, object]:
        result = run_dashboard_refresh(root)
        return {"succeeded": result.succeeded, "message": result.message}

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
    def competitors() -> dict[str, list[dict[str, Any]]]:
        dataset = _load_competitor_dataset(root)
        return {"items": frame_records(dataset.current)}

    @app.get("/api/competitors/{plid}")
    def competitor_detail(plid: str) -> dict[str, list[dict[str, Any]]]:
        dataset = _load_competitor_dataset(root)
        history = dataset.history
        reviews = dataset.reviews
        variants = dataset.variants
        if not history.empty:
            history = history.loc[history["plid"].astype(str) == plid]
        if not reviews.empty:
            reviews = reviews.loc[reviews["plid"].astype(str) == plid]
        if not variants.empty:
            variants = variants.loc[variants["plid"].astype(str) == plid]
            latest_snapshot_id = variants["快照ID"].max()
            variants = variants.loc[variants["快照ID"] == latest_snapshot_id]
        return {
            "history": frame_records(history),
            "reviews": frame_records(reviews),
            "variants": frame_records(variants),
        }

    @app.post("/api/competitors/collect")
    def collect_competitor(request: CollectCompetitorRequest) -> dict[str, object]:
        try:
            extract_plid(request.url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        settings = DashboardSettings.from_env(root)
        engine = create_engine_for_settings(settings)
        try:
            create_schema(engine)
            with CompetitorCollector(engine=engine, project_root=root) as collector:
                result = collector.collect(
                    request.url,
                    with_stock_probe=request.with_stock_probe,
                    visible_browser=request.visible_browser,
                )
        finally:
            engine.dispose()
        if not result.succeeded:
            raise HTTPException(status_code=422, detail=result.message)
        return {
            "plid": result.plid,
            "title": result.title,
            "message": result.message,
        }

    frontend_dist = root / "frontend" / "competitor" / "dist"
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
    else:
        @app.get("/", response_class=HTMLResponse)
        def missing_frontend() -> str:
            return (
                "<h1>ERP 前端尚未构建</h1>"
                "<p>请在 frontend/competitor 目录执行 npm run build。</p>"
            )
    return app


def _load_competitor_dataset(project_root: Path) -> CompetitorDataset:
    settings = DashboardSettings.from_env(project_root)
    path = sqlite_database_path(settings.database_url)
    if path is not None and not path.exists():
        return CompetitorDataset(
            pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
        )
    engine = create_read_only_erp_engine(settings.database_url)
    try:
        return load_competitor_dataset(engine)
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
    return (
        "/api/erp/nft102/download?"
        f"report_date={report_date.isoformat()}&name={quote(name)}"
    )


def _is_loopback_request(request: Request) -> bool:
    return bool(
        request.client
        and request.client.host in {"127.0.0.1", "::1", "localhost"}
    )


def _session_response(request: Request, issued: IssuedSession) -> Response:
    response = JSONResponse(
        {
            "user": issued.user.as_dict(),
            "csrf_token": issued.csrf_token,
            "expires_at": issued.expires_at.isoformat(),
        }
    )
    response.set_cookie(
        SESSION_COOKIE,
        issued.token,
        max_age=12 * 60 * 60,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    return response


app = create_app()
