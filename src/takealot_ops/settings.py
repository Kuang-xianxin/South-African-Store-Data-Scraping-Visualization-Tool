"""Environment-backed application settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError


DEFAULT_BASE_URL = "https://marketplace-api.takealot.com/v1"
DEFAULT_DATABASE_URL = (
    "mysql+pymysql://takealot_app@127.0.0.1:3306/takealot_ops?charset=utf8mb4"
)
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_DASHBOARD_HOST = "0.0.0.0"
DEFAULT_DASHBOARD_PORT = 8501


class SettingsError(ValueError):
    """Raised when required runtime settings are invalid or unavailable."""


@dataclass(frozen=True)
class Settings:
    project_root: Path
    api_key: str
    base_url: str
    database_url: str
    request_timeout_seconds: float
    dashboard_host: str
    dashboard_port: int
    backup_root: Path | None = None
    backup_database_url: str | None = None

    @classmethod
    def from_env(cls, project_root: Path) -> Settings:
        """Build validated settings from the current process environment."""
        resolved_root = project_root.resolve()
        load_dotenv(resolved_root / ".env", override=False)
        api_key = os.environ.get("TAKEALOT_API_KEY", "").strip()
        if not api_key:
            raise SettingsError("接口密钥不能为空")

        database_url = _resolve_database_url(
            os.environ.get("TAKEALOT_DATABASE_URL", DEFAULT_DATABASE_URL), resolved_root
        )
        _validate_database_url(database_url)
        primary_backend = make_url(database_url).get_backend_name()
        backup_database_url = (
            os.environ.get("TAKEALOT_BACKUP_DATABASE_URL", "").strip()
            if primary_backend == "mysql"
            else ""
        )
        if backup_database_url:
            _validate_database_url(backup_database_url)
            if make_url(backup_database_url).database != make_url(database_url).database:
                raise SettingsError("备份账号必须指向与正式库相同的 MySQL 数据库")
        return cls(
            project_root=resolved_root,
            api_key=api_key,
            base_url=os.environ.get("TAKEALOT_BASE_URL", DEFAULT_BASE_URL),
            database_url=database_url,
            request_timeout_seconds=float(
                os.environ.get("TAKEALOT_REQUEST_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS)
            ),
            dashboard_host=os.environ.get("TAKEALOT_DASHBOARD_HOST", DEFAULT_DASHBOARD_HOST),
            dashboard_port=int(os.environ.get("TAKEALOT_DASHBOARD_PORT", DEFAULT_DASHBOARD_PORT)),
            backup_root=_backup_root_from_env(resolved_root),
            backup_database_url=backup_database_url or None,
        )


@dataclass(frozen=True)
class DashboardSettings:
    """Database and local-server settings that never require API credentials."""

    project_root: Path
    database_url: str
    dashboard_host: str
    dashboard_port: int

    @classmethod
    def from_env(cls, project_root: Path) -> DashboardSettings:
        """Build the read-only dashboard runtime boundary from environment values."""
        resolved_root = project_root.resolve()
        load_dotenv(resolved_root / ".env", override=False)
        database_url = _resolve_database_url(
            os.environ.get("TAKEALOT_DATABASE_URL", DEFAULT_DATABASE_URL), resolved_root
        )
        _validate_database_url(database_url)
        return cls(
            project_root=resolved_root,
            database_url=database_url,
            dashboard_host=_dashboard_host_from_env(),
            dashboard_port=_dashboard_port_from_env(),
        )


def _dashboard_host_from_env() -> str:
    host = os.environ.get("TAKEALOT_DASHBOARD_HOST", DEFAULT_DASHBOARD_HOST).strip()
    if host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        raise SettingsError("看板地址只能是 0.0.0.0、127.0.0.1 或 localhost")
    return host


def _dashboard_port_from_env() -> int:
    raw_port = str(os.environ.get("TAKEALOT_DASHBOARD_PORT", DEFAULT_DASHBOARD_PORT))
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise SettingsError("看板端口必须是整数") from exc
    if not 1 <= port <= 65535:
        raise SettingsError("看板端口必须是1到65535之间的整数")
    return port


def _backup_root_from_env(project_root: Path) -> Path:
    raw_path = os.environ.get("TAKEALOT_BACKUP_ROOT", "").strip()
    path = Path(raw_path) if raw_path else project_root / "backups"
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def _validate_database_url(database_url: str) -> None:
    try:
        url = make_url(database_url)
    except SQLAlchemyError as exc:
        raise SettingsError("数据库地址格式无效") from exc
    if url.drivername in {"sqlite", "sqlite+pysqlite"}:
        return
    if url.drivername != "mysql+pymysql":
        raise SettingsError("数据库必须使用 mysql+pymysql 同步驱动")
    if url.host not in {"127.0.0.1", "localhost"}:
        raise SettingsError("MySQL 必须连接本机 127.0.0.1 或 localhost")
    if not url.database:
        raise SettingsError("MySQL 数据库名称不能为空")
    if not url.username:
        raise SettingsError("MySQL 用户名不能为空")


def _resolve_database_url(database_url: str, project_root: Path) -> str:
    """Resolve the retained SQLite test/migration URL; MySQL URLs pass through."""
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return database_url

    database_path = Path(database_url.removeprefix(prefix))
    if database_path.is_absolute():
        return database_url
    return f"{prefix}{(project_root / database_path).as_posix()}"
