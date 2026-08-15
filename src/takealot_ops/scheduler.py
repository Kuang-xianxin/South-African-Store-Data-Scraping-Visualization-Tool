"""Daily collection, reporting, integrity checking, and database backups."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import inspect, text
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
class LocalBackupRetention:
    """Retention windows for verified local MySQL backup archives."""

    all_days: int = 14
    daily_days: int = 90
    weekly_days: int = 365
    monthly_days: int = 365 * 7


@dataclass(frozen=True)
class BackupVerificationResult:
    archive: Path
    sha256: str
    raw_bytes: int
    compressed_bytes: int


LOCAL_BACKUP_RETENTION = LocalBackupRetention()
_BINLOG_COORDINATES_PATTERN = re.compile(
    rb"-- CHANGE (?:MASTER|REPLICATION SOURCE) TO .*?"
    rb"(?:MASTER|SOURCE)_LOG_FILE='([^']+)'.*?"
    rb"(?:MASTER|SOURCE)_LOG_POS=(\d+);"
)


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


def run_daily(
    settings: Settings,
    clock: Clock,
    *,
    report_date: date | None = None,
    trust_env: bool = True,
) -> DailyRunResult:
    """Run the complete daily workflow in the approved publication order."""
    captured_at = clock.now()
    end_date = sast_date(captured_at)
    # Re-pull the complete 30-day window shown by the cross-store revenue chart.
    # Takealot can add or amend historical order lines after an earlier capture.
    start_date = end_date - timedelta(days=29)
    export_date = report_date or end_date
    engine = create_engine_for_settings(settings)
    client: TakealotClient | None = None
    try:
        create_schema(engine)
        client = (
            TakealotClient(settings)
            if trust_env
            else TakealotClient(settings, trust_env=False)
        )
        with Session(engine) as session:
            repository = Repository(session)
            offer_result = collect_offers(client, repository, captured_at)
            # Sales history is independently authoritative for revenue. Even when
            # the Offer capture fails (and the formal daily-report run must remain
            # failed), still try the Sales endpoint and publish auditable corrections.
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
            sales_verified_at = clock.now()

            service = MetricService(
                repository,
                anomaly_rules_path=settings.project_root / "config" / "anomaly_rules.yaml",
                sale_status_rules_path=(
                    settings.project_root / "config" / "sale_status_rules.yaml"
                ),
                now=lambda: captured_at,
            )
            try:
                metric_rows = service.rebuild(
                    start_date,
                    end_date,
                    sales_source={
                        "kind": "takealot_sales_api",
                        "label": "Takealot Seller Sales API /sales 成功批次",
                        "endpoint": "/sales",
                        "run_id": sales_result.run_id,
                        "requested_start": start_date,
                        "requested_end": end_date,
                        "record_count": int(sales_result.counts.get("records", 0)),
                        "collected_at": sales_verified_at,
                    },
                )
            except Exception as exc:
                return DailyRunResult(
                    "processing_failed",
                    start_date,
                    end_date,
                    offer_result=offer_result,
                    sales_result=sales_result,
                    error=_safe_error("processing", exc),
                )

            if not offer_result.succeeded:
                return DailyRunResult(
                    "collection_failed",
                    start_date,
                    end_date,
                    offer_result=offer_result,
                    sales_result=sales_result,
                    metric_rows=metric_rows,
                    error=offer_result.error,
                )

            try:
                quality = verify_quality(repository, end_date, start_date=start_date)
                dataset = service.dashboard_dataset(export_date)
            except Exception as exc:
                return DailyRunResult(
                    "processing_failed",
                    start_date,
                    end_date,
                    offer_result=offer_result,
                    sales_result=sales_result,
                    metric_rows=metric_rows,
                    error=_safe_error("processing", exc),
                )

            try:
                reports = generate_daily_reports(
                    dataset, settings.project_root / "exports", export_date
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


def backup_database(
    settings: LocalDatabaseSettings,
    keep: int | None = None,
) -> Path:
    """Create a consistent local backup and apply backend-specific retention."""
    if keep is not None and keep < 1:
        raise ValueError("keep must be at least 1")
    backend = make_url(settings.database_url).get_backend_name()
    if backend == "mysql":
        return _backup_mysql_database(settings, keep)
    if backend != "sqlite":
        raise ValueError(f"unsupported backup backend: {backend}")
    source_path = _sqlite_database_path(settings)
    if not source_path.is_file():
        raise FileNotFoundError(f"database does not exist: {source_path}")
    backup_dir = _backup_root(settings)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    destination = backup_dir / f"takealot-{timestamp}.db"

    with sqlite3.connect(source_path) as source, sqlite3.connect(destination) as target:
        source.backup(target)

    backups = sorted(backup_dir.glob("takealot-*.db"), reverse=True)
    for obsolete in backups[(keep or 8) :]:
        obsolete.unlink()
    return destination


def verify_database_integrity(settings: LocalDatabaseSettings) -> None:
    """Fail unless the configured database and all application tables are healthy."""
    backend = make_url(settings.database_url).get_backend_name()
    if backend == "mysql":
        _verify_mysql_integrity(settings)
        return
    if backend != "sqlite":
        raise ValueError(f"unsupported integrity backend: {backend}")
    database_path = _sqlite_database_path(settings)
    if not database_path.is_file():
        raise FileNotFoundError(f"database does not exist: {database_path}")
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("PRAGMA quick_check").fetchone()
    if row != ("ok",):
        raise RuntimeError("SQLite integrity check failed")


def _backup_mysql_database(
    settings: LocalDatabaseSettings,
    keep: int | None,
) -> Path:
    dedicated_url = getattr(settings, "backup_database_url", None)
    url = make_url(dedicated_url or settings.database_url)
    if not url.database or not url.username:
        raise ValueError("MySQL backup requires a database and username")
    executable = _find_mysql_program("mysqldump.exe")
    backup_dir = _backup_root(settings)
    backup_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(UTC)
    timestamp = created_at.strftime("%Y%m%d-%H%M%S-%f")
    raw_dump = backup_dir / f"takealot-{timestamp}.sql.part"
    destination = backup_dir / f"takealot-{timestamp}.sql.gz"
    archive_part = Path(f"{destination}.part")
    manifest_path = Path(f"{destination}.json")
    checksum_path = Path(f"{destination}.sha256")
    command = [
        str(executable),
        "--protocol=TCP",
        f"--host={url.host or '127.0.0.1'}",
        f"--port={url.port or 3306}",
        f"--user={url.username}",
        "--single-transaction",
        "--quick",
        "--set-gtid-purged=OFF",
        "--no-tablespaces",
        "--default-character-set=utf8mb4",
        url.database,
    ]
    if dedicated_url:
        command.insert(-1, "--source-data=2")
    environment = os.environ.copy()
    if url.password is not None:
        environment["MYSQL_PWD"] = url.password
    try:
        with raw_dump.open("wb") as output:
            completed = subprocess.run(
                command,
                stdout=output,
                stderr=subprocess.PIPE,
                env=environment,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        if completed.returncode != 0:
            raise RuntimeError("MySQL backup failed")
        _validate_mysql_dump(raw_dump)
        binlog_coordinates = (
            _mysql_dump_binlog_coordinates(raw_dump) if dedicated_url else None
        )
        with raw_dump.open("rb") as source, gzip.open(
            archive_part,
            "wb",
            compresslevel=6,
        ) as compressed:
            shutil.copyfileobj(source, compressed, length=1024 * 1024)
        archive_part.replace(destination)

        digest = _sha256_file(destination)
        manifest: dict[str, Any] = {
            "format_version": 1,
            "backend": "mysql",
            "database": url.database,
            "created_at": created_at.isoformat(),
            "archive": destination.name,
            "sha256": digest,
            "raw_bytes": raw_dump.stat().st_size,
            "compressed_bytes": destination.stat().st_size,
            "retention": {
                "all_days": LOCAL_BACKUP_RETENTION.all_days,
                "daily_days": LOCAL_BACKUP_RETENTION.daily_days,
                "weekly_days": LOCAL_BACKUP_RETENTION.weekly_days,
                "monthly_days": LOCAL_BACKUP_RETENTION.monthly_days,
            },
        }
        if binlog_coordinates is not None:
            manifest["binlog"] = {
                "file": binlog_coordinates[0],
                "position": binlog_coordinates[1],
            }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        checksum_path.write_text(
            f"{digest}  {destination.name}\n",
            encoding="ascii",
        )
        verify_local_backup(destination)
    except Exception:
        destination.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        checksum_path.unlink(missing_ok=True)
        raise
    finally:
        environment.pop("MYSQL_PWD", None)
        raw_dump.unlink(missing_ok=True)
        archive_part.unlink(missing_ok=True)

    if keep is None:
        _apply_local_backup_retention(backup_dir, now=created_at)
    else:
        backups = sorted(backup_dir.glob("takealot-*.sql.gz"), reverse=True)
        for obsolete in backups[keep:]:
            _remove_backup_group(obsolete)
    return destination


def verify_local_backup(archive: Path) -> BackupVerificationResult:
    """Verify a compressed local backup against its manifest and checksum."""
    archive = archive.resolve()
    if not archive.is_file() or not archive.name.endswith(".sql.gz"):
        raise ValueError("backup archive must be an existing .sql.gz file")
    manifest_path = Path(f"{archive}.json")
    checksum_path = Path(f"{archive}.sha256")
    if not manifest_path.is_file() or not checksum_path.is_file():
        raise RuntimeError("backup manifest or checksum file is missing")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("backup manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
        raise RuntimeError("unsupported backup manifest")
    if manifest.get("archive") != archive.name:
        raise RuntimeError("backup manifest archive name does not match")

    digest = _sha256_file(archive)
    expected_digest = manifest.get("sha256")
    checksum_parts = checksum_path.read_text(encoding="ascii").strip().split()
    if (
        not isinstance(expected_digest, str)
        or digest != expected_digest
        or len(checksum_parts) != 2
        or checksum_parts[0] != digest
        or checksum_parts[1] != archive.name
    ):
        raise RuntimeError("backup checksum verification failed")

    raw_bytes = 0
    head = b""
    tail = b""
    try:
        with gzip.open(archive, "rb") as source:
            while chunk := source.read(1024 * 1024):
                raw_bytes += len(chunk)
                if len(head) < 512:
                    head = (head + chunk)[:512]
                tail = (tail + chunk)[-8192:]
    except (OSError, EOFError) as exc:
        raise RuntimeError("backup gzip verification failed") from exc
    _validate_mysql_dump_markers(head, tail)

    compressed_bytes = archive.stat().st_size
    if manifest.get("raw_bytes") != raw_bytes:
        raise RuntimeError("backup uncompressed size does not match manifest")
    if manifest.get("compressed_bytes") != compressed_bytes:
        raise RuntimeError("backup compressed size does not match manifest")
    return BackupVerificationResult(
        archive=archive,
        sha256=digest,
        raw_bytes=raw_bytes,
        compressed_bytes=compressed_bytes,
    )


def _validate_mysql_dump(path: Path) -> None:
    size = path.stat().st_size
    if size == 0:
        raise RuntimeError("MySQL backup is empty")
    with path.open("rb") as dump:
        head = dump.read(512)
        dump.seek(max(0, size - 8192))
        tail = dump.read()
    _validate_mysql_dump_markers(head, tail)


def _validate_mysql_dump_markers(head: bytes, tail: bytes) -> None:
    if b"MySQL dump" not in head:
        raise RuntimeError("MySQL backup header is missing")
    if b"Dump completed on" not in tail:
        raise RuntimeError("MySQL backup completion marker is missing")


def _mysql_dump_binlog_coordinates(path: Path) -> tuple[str, int]:
    with path.open("rb") as source:
        for line in source:
            match = _BINLOG_COORDINATES_PATTERN.search(line)
            if match is not None:
                return match.group(1).decode("ascii"), int(match.group(2))
    raise RuntimeError("MySQL backup binlog coordinates are missing")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _apply_local_backup_retention(
    backup_dir: Path,
    *,
    now: datetime,
    policy: LocalBackupRetention = LOCAL_BACKUP_RETENTION,
) -> None:
    retained_buckets: set[tuple[object, ...]] = set()
    for archive in sorted(backup_dir.glob("takealot-*.sql.gz"), reverse=True):
        captured_at = _backup_timestamp(archive)
        if captured_at is None:
            continue
        age = now - captured_at
        bucket: tuple[object, ...] | None
        if age <= timedelta(days=policy.all_days):
            continue
        if age <= timedelta(days=policy.daily_days):
            bucket = ("day", captured_at.date())
        elif age <= timedelta(days=policy.weekly_days):
            iso_year, iso_week, _ = captured_at.isocalendar()
            bucket = ("week", iso_year, iso_week)
        elif age <= timedelta(days=policy.monthly_days):
            bucket = ("month", captured_at.year, captured_at.month)
        else:
            bucket = None
        if bucket is not None and bucket not in retained_buckets:
            retained_buckets.add(bucket)
            continue
        _remove_backup_group(archive)


def _backup_timestamp(archive: Path) -> datetime | None:
    try:
        timestamp = archive.name.removeprefix("takealot-").removesuffix(".sql.gz")
        return datetime.strptime(timestamp, "%Y%m%d-%H%M%S-%f").replace(tzinfo=UTC)
    except ValueError:
        return None


def _remove_backup_group(archive: Path) -> None:
    archive.unlink(missing_ok=True)
    Path(f"{archive}.json").unlink(missing_ok=True)
    Path(f"{archive}.sha256").unlink(missing_ok=True)


def _backup_root(settings: LocalDatabaseSettings) -> Path:
    configured = getattr(settings, "backup_root", None)
    return Path(configured) if configured is not None else settings.project_root / "backups"


def _verify_mysql_integrity(settings: LocalDatabaseSettings) -> None:
    engine = create_engine_for_settings(settings)
    try:
        with engine.connect() as connection:
            tables = inspect(connection).get_table_names()
            if not tables:
                raise RuntimeError("MySQL database has no application tables")
            for table_name in tables:
                escaped = table_name.replace("`", "``")
                rows = connection.execute(
                    text(f"CHECK TABLE `{escaped}` QUICK")
                ).mappings()
                for row in rows:
                    if str(row.get("Msg_type", "")).casefold() == "error":
                        raise RuntimeError(f"MySQL table check failed: {table_name}")
    finally:
        engine.dispose()


def _find_mysql_program(name: str) -> Path:
    candidates = (
        Path("C:/Program Files/MySQL/MySQL Server 8.0/bin") / name,
        Path("C:/Program Files/MySQL/MySQL Server 8.4/bin") / name,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"未找到 MySQL 工具：{name}")


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
