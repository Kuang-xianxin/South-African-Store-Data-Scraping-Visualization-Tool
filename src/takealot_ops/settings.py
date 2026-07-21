"""Environment-backed application settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError


DEFAULT_BASE_URL = "https://marketplace-api.takealot.com/v1"
DEFAULT_DATABASE_URL = "sqlite:///data/takealot.db"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_DASHBOARD_HOST = "127.0.0.1"
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

    @classmethod
    def from_env(cls, project_root: Path) -> Settings:
        """Build validated settings from the current process environment."""
        api_key = os.environ.get("TAKEALOT_API_KEY", "").strip()
        if not api_key:
            raise SettingsError("TAKEALOT_API_KEY must be set to a non-blank value")

        resolved_root = project_root.resolve()
        database_url = _resolve_sqlite_url(
            os.environ.get("TAKEALOT_DATABASE_URL", DEFAULT_DATABASE_URL), resolved_root
        )
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
        database_url = _resolve_sqlite_url(
            os.environ.get("TAKEALOT_DATABASE_URL", DEFAULT_DATABASE_URL), resolved_root
        )
        _validate_dashboard_database_url(database_url)
        return cls(
            project_root=resolved_root,
            database_url=database_url,
            dashboard_host=_dashboard_host_from_env(),
            dashboard_port=_dashboard_port_from_env(),
        )


def _dashboard_host_from_env() -> str:
    host = os.environ.get("TAKEALOT_DASHBOARD_HOST", DEFAULT_DASHBOARD_HOST).strip()
    if host not in {"127.0.0.1", "localhost"}:
        raise SettingsError("TAKEALOT_DASHBOARD_HOST must be 127.0.0.1 or localhost")
    return host


def _dashboard_port_from_env() -> int:
    raw_port = str(os.environ.get("TAKEALOT_DASHBOARD_PORT", DEFAULT_DASHBOARD_PORT))
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise SettingsError("TAKEALOT_DASHBOARD_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise SettingsError("TAKEALOT_DASHBOARD_PORT must be between 1 and 65535")
    return port


def _validate_dashboard_database_url(database_url: str) -> None:
    try:
        driver_name = make_url(database_url).drivername
    except SQLAlchemyError as exc:
        raise SettingsError("TAKEALOT_DATABASE_URL must be a valid SQLite URL") from exc
    if driver_name not in {"sqlite", "sqlite+pysqlite"}:
        raise SettingsError("The dashboard currently supports synchronous SQLite URLs only")


def _resolve_sqlite_url(database_url: str, project_root: Path) -> str:
    """Resolve a relative SQLite URL against the project root."""
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return database_url

    database_path = Path(database_url.removeprefix(prefix))
    if database_path.is_absolute():
        return database_url
    return f"{prefix}{(project_root / database_path).as_posix()}"
