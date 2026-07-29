"""Continuous local MySQL binary-log archiving on the configured backup drive."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from takealot_ops.settings import Settings, SettingsError


BINLOG_RETENTION_DAYS = 35
BINLOG_CONNECTION_SERVER_ID = 2929001
BINLOG_LAG_WARNING_BYTES = 16 * 1024 * 1024
_BINLOG_NAME = re.compile(r"^.+\.\d{6,}$")


@dataclass(frozen=True)
class RemoteBinlog:
    name: str
    size: int
    encrypted: bool


@dataclass(frozen=True)
class BinlogArchiveSettings:
    project_root: Path
    backup_root: Path
    database_url: str
    retention_days: int = BINLOG_RETENTION_DAYS
    connection_server_id: int = BINLOG_CONNECTION_SERVER_ID

    @classmethod
    def from_settings(cls, settings: Settings) -> BinlogArchiveSettings:
        backup_root = settings.backup_root or settings.project_root / "backups"
        if os.name == "nt" and backup_root.drive.upper() != "D:":
            raise SettingsError("正式备份目录必须位于 D 盘")
        if settings.backup_database_url is None:
            raise SettingsError(
                "缺少 TAKEALOT_BACKUP_DATABASE_URL；"
                "binlog 必须使用独立的低权限备份账号"
            )
        url = make_url(settings.backup_database_url)
        if not url.username or url.password is None:
            raise SettingsError("备份数据库地址必须包含专用账号和密码")
        retention_days = int(
            os.environ.get("TAKEALOT_BINLOG_RETENTION_DAYS", BINLOG_RETENTION_DAYS)
        )
        if retention_days < 30:
            raise SettingsError("binlog 本地归档至少保留30天")
        return cls(
            project_root=settings.project_root,
            backup_root=backup_root,
            database_url=settings.backup_database_url,
            retention_days=retention_days,
        )

    @property
    def archive_dir(self) -> Path:
        return self.backup_root / "binlog"


@dataclass(frozen=True)
class BinlogArchivePlan:
    start_file: str
    remote_files: tuple[str, ...]
    missing_before_start: bool
    command: tuple[str, ...]


@dataclass(frozen=True)
class BinlogArchiveStatus:
    healthy: bool
    archive_dir: Path
    remote_current_file: str
    remote_current_size: int
    local_current_size: int | None
    lag_bytes: int | None
    missing_remote_files: tuple[str, ...]
    archive_files: int
    archive_bytes: int


Runner = Callable[..., subprocess.CompletedProcess[Any]]


def build_binlog_archive_plan(
    settings: BinlogArchiveSettings,
    remote_logs: list[RemoteBinlog],
    *,
    executable: Path | None = None,
) -> BinlogArchivePlan:
    if not remote_logs:
        raise RuntimeError("MySQL 没有可归档的 binlog 文件")
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    local_names = {path.name for path in _local_binlog_files(settings.archive_dir)}
    remote_names = [row.name for row in remote_logs]
    common_names = [name for name in remote_names if name in local_names]
    start_file = common_names[-1] if common_names else remote_names[0]
    missing_before_start = bool(local_names and not common_names)
    url = make_url(settings.database_url)
    mysqlbinlog = executable or find_mysqlbinlog()
    result_prefix = f"{settings.archive_dir}{os.sep}"
    command = (
        str(mysqlbinlog),
        "--read-from-remote-server",
        "--raw",
        "--stop-never",
        "--verify-binlog-checksum",
        f"--connection-server-id={settings.connection_server_id}",
        "--protocol=TCP",
        f"--host={url.host or '127.0.0.1'}",
        f"--port={url.port or 3306}",
        f"--user={url.username}",
        "--get-server-public-key",
        f"--result-file={result_prefix}",
        start_file,
    )
    return BinlogArchivePlan(
        start_file=start_file,
        remote_files=tuple(remote_names),
        missing_before_start=missing_before_start,
        command=command,
    )


def list_remote_binlogs(settings: BinlogArchiveSettings) -> list[RemoteBinlog]:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("SHOW BINARY LOGS")).all()
    finally:
        engine.dispose()
    return [
        RemoteBinlog(
            name=str(row[0]),
            size=int(row[1]),
            encrypted=bool(row[2]) if len(row) > 2 else False,
        )
        for row in rows
    ]


def run_continuous_binlog_archive(
    settings: BinlogArchiveSettings,
    *,
    runner: Runner = subprocess.run,
    executable: Path | None = None,
) -> int:
    remote_logs = list_remote_binlogs(settings)
    plan = build_binlog_archive_plan(
        settings,
        remote_logs,
        executable=executable,
    )
    write_binlog_status(
        settings,
        {
            "state": "starting",
            "started_at": datetime.now(UTC).isoformat(),
            "start_file": plan.start_file,
            "missing_before_start": plan.missing_before_start,
        },
    )
    url = make_url(settings.database_url)
    environment = os.environ.copy()
    environment["MYSQL_PWD"] = str(url.password)
    stderr_path = settings.archive_dir / "mysqlbinlog.stderr.log"
    try:
        with stderr_path.open("ab") as stderr:
            completed = runner(
                list(plan.command),
                cwd=settings.archive_dir,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                env=environment,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    finally:
        environment.pop("MYSQL_PWD", None)
    return_code = int(completed.returncode)
    write_binlog_status(
        settings,
        {
            "state": "stopped",
            "stopped_at": datetime.now(UTC).isoformat(),
            "return_code": return_code,
            "start_file": plan.start_file,
        },
    )
    return return_code


def maintain_binlog_archive(
    settings: BinlogArchiveSettings,
    *,
    now: datetime | None = None,
    runner: Runner = subprocess.run,
    executable: Path | None = None,
) -> dict[str, int]:
    checked_at = now or datetime.now(UTC)
    remote_logs = list_remote_binlogs(settings)
    if not remote_logs:
        raise RuntimeError("MySQL 没有可维护的 binlog 文件")
    current_name = remote_logs[-1].name
    verified = 0
    deleted = 0
    for archive in _local_binlog_files(settings.archive_dir):
        if archive.name == current_name:
            continue
        verify_binlog_archive(archive, runner=runner, executable=executable)
        checksum_path = Path(f"{archive}.sha256")
        checksum_path.write_text(
            f"{_sha256_file(archive)}  {archive.name}\n",
            encoding="ascii",
        )
        verified += 1
        modified_at = datetime.fromtimestamp(archive.stat().st_mtime, tz=UTC)
        if checked_at - modified_at > timedelta(days=settings.retention_days):
            archive.unlink()
            checksum_path.unlink(missing_ok=True)
            deleted += 1
    result = {"verified": verified, "deleted": deleted}
    write_binlog_status(
        settings,
        {
            "state": "maintenance_complete",
            "checked_at": checked_at.isoformat(),
            **result,
        },
    )
    return result


def inspect_binlog_archive(settings: BinlogArchiveSettings) -> BinlogArchiveStatus:
    remote_logs = list_remote_binlogs(settings)
    if not remote_logs:
        raise RuntimeError("MySQL 没有可检查的 binlog 文件")
    local_files = _local_binlog_files(settings.archive_dir)
    local_by_name = {path.name: path for path in local_files}
    current = remote_logs[-1]
    current_local = local_by_name.get(current.name)
    local_size = current_local.stat().st_size if current_local else None
    lag_bytes = (
        max(0, current.size - local_size) if local_size is not None else None
    )
    missing = tuple(row.name for row in remote_logs if row.name not in local_by_name)
    healthy = (
        current_local is not None
        and lag_bytes is not None
        and lag_bytes <= BINLOG_LAG_WARNING_BYTES
        and not missing
    )
    return BinlogArchiveStatus(
        healthy=healthy,
        archive_dir=settings.archive_dir,
        remote_current_file=current.name,
        remote_current_size=current.size,
        local_current_size=local_size,
        lag_bytes=lag_bytes,
        missing_remote_files=missing,
        archive_files=len(local_files),
        archive_bytes=sum(path.stat().st_size for path in local_files),
    )


def verify_binlog_archive(
    archive: Path,
    *,
    runner: Runner = subprocess.run,
    executable: Path | None = None,
) -> None:
    with archive.open("rb") as source:
        if source.read(4) != b"\xfebin":
            raise RuntimeError(f"binlog 文件头无效：{archive.name}")
    command = [
        str(executable or find_mysqlbinlog()),
        "--verify-binlog-checksum",
        str(archive),
    ]
    completed = runner(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise RuntimeError(f"binlog 校验失败：{archive.name}")


def write_binlog_status(
    settings: BinlogArchiveSettings,
    payload: dict[str, object],
) -> Path:
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    destination = settings.archive_dir / "archive-status.json"
    partial = Path(f"{destination}.part")
    document = {
        "format_version": 1,
        "archive_dir": str(settings.archive_dir),
        **payload,
    }
    partial.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    partial.replace(destination)
    return destination


def find_mysqlbinlog() -> Path:
    candidates = (
        Path("C:/Program Files/MySQL/MySQL Server 8.0/bin/mysqlbinlog.exe"),
        Path("C:/Program Files/MySQL/MySQL Server 8.4/bin/mysqlbinlog.exe"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("未找到 MySQL 工具：mysqlbinlog.exe")


def _local_binlog_files(archive_dir: Path) -> list[Path]:
    if not archive_dir.is_dir():
        return []
    return sorted(
        path
        for path in archive_dir.iterdir()
        if path.is_file() and _BINLOG_NAME.fullmatch(path.name)
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
