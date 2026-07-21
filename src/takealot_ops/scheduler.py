"""Daily collection, reporting, integrity checking, and SQLite backups."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from takealot_ops.api.client import TakealotClient
from takealot_ops.collectors import CollectionResult, collect_offers, collect_sales
from takealot_ops.domain import sast_date
from takealot_ops.metrics.service import MetricService
from takealot_ops.quality import QualityResult, verify_quality
from takealot_ops.reporting import ReportPaths, generate_daily_reports
from takealot_ops.settings import Settings
from takealot_ops.storage.migrations import create_engine_for_settings, create_schema
from takealot_ops.storage.repository import Repository


class Clock(Protocol):
    def now(self) -> datetime: ...


class LocalDatabaseSettings(Protocol):
    @property
    def project_root(self) -> Path: ...

    @property
    def database_url(self) -> str: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class DailyRunResult:
    status: str
    start_date: date
    end_date: date
    offer_result: CollectionResult | None = None
    sales_result: CollectionResult | None = None
    metric_rows: int = 0
    quality: QualityResult | None = None
    report_paths: ReportPaths | None = None
    backup_path: Path | None = None
    error: str | None = None

    @property
    def exit_code(self) -> int:
        if self.status == "success":
            return 0
        if self.status == "collection_failed":
            return 3
        if self.status in {"quality_failed", "integrity_failed"}:
            return 4
        return 5


def run_daily(settings: Settings, clock: Clock) -> DailyRunResult:
    """Run the complete daily workflow in the approved publication order."""
    captured_at = clock.now()
    end_date = sast_date(captured_at)
    start_date = end_date - timedelta(days=6)
    engine = create_engine_for_settings(settings)
    client: TakealotClient | None = None
    try:
        create_schema(engine)
        client = TakealotClient(settings)
        with Session(engine) as session:
            repository = Repository(session)
            offer_result = collect_offers(client, repository, captured_at)
            if not offer_result.succeeded:
                return DailyRunResult(
                    "collection_failed",
                    start_date,
                    end_date,
                    offer_result=offer_result,
                    error=offer_result.error,
                )

            sales_result = collect_sales(client, repository, start_date, end_date)
            if not sales_result.succeeded:
                return DailyRunResult(
                    "collection_failed",
                    start_date,
                    end_date,
                    offer_result=offer_result,
                    sales_result=sales_result,
                    error=sales_result.error,
                )

            service = MetricService(
                repository,
                anomaly_rules_path=settings.project_root / "config" / "anomaly_rules.yaml",
                sale_status_rules_path=(
                    settings.project_root / "config" / "sale_status_rules.yaml"
                ),
                now=lambda: captured_at,
            )
            try:
                metric_rows = service.rebuild(start_date, end_date)
                quality = verify_quality(repository, end_date)
                dataset = service.dashboard_dataset(end_date)
            except Exception as exc:
                return DailyRunResult(
                    "processing_failed",
                    start_date,
                    end_date,
                    offer_result=offer_result,
                    sales_result=sales_result,
                    error=_safe_error("processing", exc),
                )

            try:
                reports = generate_daily_reports(
                    dataset, settings.project_root / "exports", end_date
                )
            except Exception as exc:
                return DailyRunResult(
                    "export_failed",
                    start_date,
                    end_date,
                    offer_result=offer_result,
                    sales_result=sales_result,
                    metric_rows=metric_rows,
                    quality=quality,
                    error=_safe_error("export", exc),
                )

        try:
            verify_database_integrity(settings)
        except Exception as exc:
            return DailyRunResult(
                "integrity_failed",
                start_date,
                end_date,
                offer_result=offer_result,
                sales_result=sales_result,
                metric_rows=metric_rows,
                quality=quality,
                report_paths=reports,
                error=_safe_error("integrity check", exc),
            )
        try:
            backup_path = backup_database(settings)
        except Exception as exc:
            return DailyRunResult(
                "backup_failed",
                start_date,
                end_date,
                offer_result=offer_result,
                sales_result=sales_result,
                metric_rows=metric_rows,
                quality=quality,
                report_paths=reports,
                error=_safe_error("backup", exc),
            )

        return DailyRunResult(
            "success" if quality.passed else "quality_failed",
            start_date,
            end_date,
            offer_result=offer_result,
            sales_result=sales_result,
            metric_rows=metric_rows,
            quality=quality,
            report_paths=reports,
            backup_path=backup_path,
        )
    finally:
        if client is not None:
            client.close()
        engine.dispose()


def backup_database(settings: LocalDatabaseSettings, keep: int = 8) -> Path:
    """Create a consistent SQLite backup and retain only the newest files."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    source_path = _sqlite_database_path(settings)
    if not source_path.is_file():
        raise FileNotFoundError(f"database does not exist: {source_path}")
    backup_dir = settings.project_root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    destination = backup_dir / f"takealot-{timestamp}.db"

    with sqlite3.connect(source_path) as source, sqlite3.connect(destination) as target:
        source.backup(target)

    backups = sorted(backup_dir.glob("takealot-*.db"), reverse=True)
    for obsolete in backups[keep:]:
        obsolete.unlink()
    return destination


def verify_database_integrity(settings: LocalDatabaseSettings) -> None:
    """Fail unless SQLite reports a clean quick integrity check."""
    database_path = _sqlite_database_path(settings)
    if not database_path.is_file():
        raise FileNotFoundError(f"database does not exist: {database_path}")
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("PRAGMA quick_check").fetchone()
    if row != ("ok",):
        raise RuntimeError("SQLite integrity check failed")


def _sqlite_database_path(settings: LocalDatabaseSettings) -> Path:
    url = make_url(settings.database_url)
    if url.drivername not in {"sqlite", "sqlite+pysqlite"}:
        raise ValueError("automatic backup currently supports SQLite only")
    if not url.database or url.database == ":memory:":
        raise ValueError("automatic backup requires a file-backed SQLite database")
    path = Path(url.database)
    return path if path.is_absolute() else settings.project_root / path


def _safe_error(phase: str, exc: Exception) -> str:
    return f"{phase} failed ({type(exc).__name__})"
