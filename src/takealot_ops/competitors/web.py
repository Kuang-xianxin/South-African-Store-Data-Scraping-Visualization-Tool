"""Loopback-only FastAPI application serving the Vue competitor module."""

from __future__ import annotations

import math
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, event

from takealot_ops.competitors.api import extract_plid
from takealot_ops.competitors.service import (
    CompetitorCollector,
    CompetitorDataset,
    load_competitor_dataset,
)
from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.migrations import create_engine_for_settings, create_schema


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
    def competitors() -> dict[str, list[dict[str, Any]]]:
        dataset = _load_dataset(root)
        return {"items": _frame_records(dataset.current)}

    @app.get("/api/competitors/{plid}")
    def competitor_detail(plid: str) -> dict[str, list[dict[str, Any]]]:
        dataset = _load_dataset(root)
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
    def collect(request: CollectRequest) -> dict[str, object]:
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
                "<h1>竞品前端尚未构建</h1>"
                "<p>请在 frontend/competitor 目录执行 npm run build。</p>"
            )
    return app


def _load_dataset(project_root: Path) -> CompetitorDataset:
    settings = DashboardSettings.from_env(project_root)
    database_path = _sqlite_database_path(settings.database_url)
    if database_path is not None and not database_path.exists():
        return CompetitorDataset(
            pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
        )
    engine = create_engine(settings.database_url)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _set_query_only(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA query_only=ON")
            cursor.close()
    try:
        return load_competitor_dataset(engine)
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
