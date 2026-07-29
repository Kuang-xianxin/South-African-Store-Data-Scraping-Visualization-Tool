"""Loopback-only FastAPI application serving the Vue competitor module."""

from __future__ import annotations

import math
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from takealot_ops.competitors.api import CompetitorNetworkError, extract_plid
from takealot_ops.competitors.service import (
    CompetitorCollector,
    CompetitorDataset,
    load_competitor_dataset,
    load_competitor_link_health,
)
from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.migrations import (
    create_engine_for_settings,
    create_read_only_engine,
    create_schema,
)


class CollectRequest(BaseModel):
    """One explicit product collection request from the local Vue page."""

    url: str = Field(min_length=1)
    with_stock_probe: bool = True
    visible_browser: bool = False


def create_app(project_root: Path | None = None) -> FastAPI:
    """Create the local API and attach the built Vue application."""
    root = (
        project_root
        or Path(os.environ.get("TAKEALOT_PROJECT_ROOT", Path.cwd()))
    ).resolve()
    app = FastAPI(
        title="Takealot 竞品观察",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/competitors")
    def competitors(
        start_date: date | None = Query(default=None),
        end_date: date | None = Query(default=None),
    ) -> dict[str, object]:
        dataset = _load_dataset(root, start_date=start_date, end_date=end_date)
        return {
            "items": _frame_records(dataset.current),
            "date_range": dataset.date_range_payload(),
        }

    @app.get("/api/competitors/link-health")
    def competitor_link_health() -> dict[str, list[dict[str, Any]]]:
        settings = DashboardSettings.from_env(root)
        engine = create_read_only_engine(settings.database_url)
        try:
            return {"items": load_competitor_link_health(engine)}
        finally:
            engine.dispose()

    @app.get("/api/competitors/{plid}")
    def competitor_detail(
        plid: str,
        start_date: date | None = Query(default=None),
        end_date: date | None = Query(default=None),
    ) -> dict[str, list[dict[str, Any]]]:
        dataset = _load_dataset(root, start_date=start_date, end_date=end_date)
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
            "history": _frame_records(history),
            "reviews": _frame_records(reviews),
            "variants": _frame_records(variants),
        }

    @app.post("/api/competitors/collect")
    async def collect(request: CollectRequest) -> dict[str, object]:
        try:
            extract_plid(request.url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        settings = DashboardSettings.from_env(root)
        engine = create_engine_for_settings(settings)
        try:
            create_schema(engine)
            try:
                async with CompetitorCollector(
                    engine=engine,
                    project_root=root,
                ) as collector:
                    result = await collector.collect(
                        request.url,
                        with_stock_probe=request.with_stock_probe,
                        visible_browser=request.visible_browser,
                    )
            except CompetitorNetworkError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            engine.dispose()
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
        }

    frontend_dist = root / "frontend" / "competitor" / "dist"
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
    else:
        @app.get("/", response_class=HTMLResponse)
        def missing_frontend() -> str:
            return (
                "<h1>竞品前端尚未构建</h1>"
                "<p>请在 frontend/competitor 目录执行 npm run build。</p>"
            )
    return app


def _collection_failure_status(
    failure_kind: str | None,
    *,
    retryable: bool,
) -> int:
    if failure_kind == "network":
        return 503
    if failure_kind == "validation-uncertain":
        return 409
    if failure_kind == "suspected-invalid":
        return 404
    if failure_kind == "confirmed-invalid":
        return 410
    return 503 if retryable else 422


def _load_dataset(
    project_root: Path,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> CompetitorDataset:
    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    settings = DashboardSettings.from_env(project_root)
    database_path = _sqlite_database_path(settings.database_url)
    if database_path is not None and not database_path.exists():
        return CompetitorDataset(
            current=pd.DataFrame(),
            history=pd.DataFrame(),
            reviews=pd.DataFrame(),
            variants=pd.DataFrame(),
            selected_start_date=start_date,
            selected_end_date=end_date,
        )
    engine = create_read_only_engine(settings.database_url)
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


def _frame_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in frame.to_dict(orient="records"):
        row: dict[str, Any] = {}
        for key, value in raw.items():
            if value is None or _is_missing(value):
                row[str(key)] = None
            elif isinstance(value, (pd.Timestamp, datetime, date)):
                row[str(key)] = value.isoformat()
            else:
                row[str(key)] = value
        records.append(row)
    return records


def _is_missing(value: object) -> bool:
    if value is pd.NA or value is pd.NaT:
        return True
    return isinstance(value, float) and math.isnan(value)


def _sqlite_database_path(database_url: str) -> Path | None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return None
    return Path(database_url.removeprefix(prefix))


app = create_app()
