"""Loopback-only FastAPI application for the unified local ERP."""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
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


def create_app(project_root: Path | None = None) -> FastAPI:
    """Create the unified ERP API and attach its built Vue application."""
    root = (
        project_root
        or Path(os.environ.get("TAKEALOT_PROJECT_ROOT", Path.cwd()))
    ).resolve()
    app = FastAPI(
        title="Takealot 本地运营 ERP",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "application": "takealot-erp"}

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


app = create_app()
