"""Environment-backed application settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from takealot_ops.storage.store_context import (
    DEFAULT_STORE_CODE,
    current_store_code,
    normalize_store_code,
)


DEFAULT_BASE_URL = "https://marketplace-api.takealot.com/v1"
DEFAULT_DATABASE_URL = (
    "mysql+pymysql://takealot_app@127.0.0.1:3306/takealot_ops?charset=utf8mb4"
)
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_DASHBOARD_HOST = "0.0.0.0"
DEFAULT_DASHBOARD_PORT = 8501
DEFAULT_W8_BASE_URL = "https://crgyl.w8soft.net/prod-api/w8"
DEFAULT_W8_REQUEST_TIMEOUT_SECONDS = 30.0
TAKEALOT_SELLER_BFF_URL = "https://seller-api.takealot.com"
DEFAULT_PORTAL_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_PORTAL_TASK_TIMEOUT_SECONDS = 45.0
DEFAULT_PORTAL_MAX_TOTAL_QUANTITY = 500


class SettingsError(ValueError):
    """Raised when required runtime settings are invalid or unavailable."""


@dataclass(frozen=True)
class StoreConfiguration:
    """One configured seller account without exposing its credential."""

    code: str
    display_name: str
    api_key_env: str
    api_key: str


@dataclass(frozen=True)
class Settings:
    project_root: Path
    api_key: str
    base_url: str
    database_url: str
    request_timeout_seconds: float
    dashboard_host: str
    dashboard_port: int
    store_code: str = DEFAULT_STORE_CODE
    backup_root: Path | None = None
    backup_database_url: str | None = None

    @classmethod
    def from_env(
        cls,
        project_root: Path,
        store_code: str | None = None,
    ) -> Settings:
        """Build validated settings from the current process environment."""
        resolved_root = project_root.resolve()
        load_dotenv(resolved_root / ".env", override=False)
        stores = configured_stores(resolved_root)
        selected_code = normalize_store_code(store_code or current_store_code())
        selected = next((store for store in stores if store.code == selected_code), None)
        if selected is None:
            raise SettingsError(f"店铺 {selected_code} 未配置 API 凭据")

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
            api_key=selected.api_key,
            base_url=os.environ.get("TAKEALOT_BASE_URL", DEFAULT_BASE_URL),
            database_url=database_url,
            request_timeout_seconds=float(
                os.environ.get("TAKEALOT_REQUEST_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS)
            ),
            dashboard_host=os.environ.get("TAKEALOT_DASHBOARD_HOST", DEFAULT_DASHBOARD_HOST),
            dashboard_port=int(os.environ.get("TAKEALOT_DASHBOARD_PORT", DEFAULT_DASHBOARD_PORT)),
            store_code=selected.code,
            backup_root=_backup_root_from_env(resolved_root),
            backup_database_url=backup_database_url or None,
        )


def configured_stores(project_root: Path) -> tuple[StoreConfiguration, ...]:
    """Load the deduplicated store credential registry from the ignored environment."""
    resolved_root = project_root.resolve()
    load_dotenv(resolved_root / ".env", override=False)
    raw_registry = os.environ.get("TAKEALOT_STORES", "").strip()
    entries = (
        [entry.strip() for entry in raw_registry.split(";") if entry.strip()]
        if raw_registry
        else [f"{DEFAULT_STORE_CODE}|当前店铺|TAKEALOT_API_KEY"]
    )
    stores: list[StoreConfiguration] = []
    seen_codes: set[str] = set()
    seen_keys: set[str] = set()
    for entry in entries:
        parts = [part.strip() for part in entry.split("|")]
        if len(parts) != 3:
            raise SettingsError("TAKEALOT_STORES 每项必须是 店铺代码|显示名称|密钥变量名")
        code = normalize_store_code(parts[0])
        display_name = parts[1]
        api_key_env = parts[2]
        if not display_name or not api_key_env:
            raise SettingsError("店铺显示名称和密钥变量名不能为空")
        if code in seen_codes:
            raise SettingsError(f"店铺代码重复：{code}")
        api_key = os.environ.get(api_key_env, "").strip()
        if not api_key:
            raise SettingsError(f"接口密钥未配置：店铺 {code}")
        if api_key in seen_keys:
            raise SettingsError(f"店铺 {code} 使用了重复的 API 凭据")
        seen_codes.add(code)
        seen_keys.add(api_key)
        stores.append(
            StoreConfiguration(
                code=code,
                display_name=display_name,
                api_key_env=api_key_env,
                api_key=api_key,
            )
        )
    return tuple(stores)


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


@dataclass(frozen=True)
class W8Settings:
    """Optional Long Reach W8 read-only integration settings."""

    project_root: Path
    token: str
    base_url: str
    request_timeout_seconds: float

    @property
    def configured(self) -> bool:
        return bool(self.token)

    @classmethod
    def from_env(cls, project_root: Path) -> W8Settings:
        resolved_root = project_root.resolve()
        load_dotenv(resolved_root / ".env", override=False)
        base_url = os.environ.get("W8_BASE_URL", DEFAULT_W8_BASE_URL).strip().rstrip("/")
        parsed = urlsplit(base_url)
        hostname = (parsed.hostname or "").casefold()
        if (
            parsed.scheme != "https"
            or not hostname
            or not (hostname == "w8soft.net" or hostname.endswith(".w8soft.net"))
            or parsed.path.rstrip("/") != "/prod-api/w8"
            or parsed.query
            or parsed.fragment
        ):
            raise SettingsError(
                "长睿 W8 地址必须是 w8soft.net 下的 HTTPS /prod-api/w8 地址"
            )
        try:
            timeout = float(
                os.environ.get(
                    "W8_REQUEST_TIMEOUT_SECONDS",
                    DEFAULT_W8_REQUEST_TIMEOUT_SECONDS,
                )
            )
        except ValueError as exc:
            raise SettingsError("长睿 W8 请求超时必须是数字") from exc
        if not 1 <= timeout <= 120:
            raise SettingsError("长睿 W8 请求超时必须在1到120秒之间")
        return cls(
            project_root=resolved_root,
            token=os.environ.get("W8_API_TOKEN", "").strip(),
            base_url=base_url,
            request_timeout_seconds=timeout,
        )


@dataclass(frozen=True)
class TakealotPortalSettings:
    """Safety controls for the private Seller Portal BFF integration.

    The upstream host is deliberately fixed in code. Credentials and bearer tokens are
    never part of these settings: per-store credentials live in the server account's
    Windows Credential Manager, while bearer tokens remain process-memory only.
    """

    enabled: bool
    base_url: str
    request_timeout_seconds: float
    task_timeout_seconds: float
    max_total_quantity: int
    enabled_store_codes: frozenset[str]

    def is_store_enabled(self, store_code: str | None = None) -> bool:
        """Return whether Seller Portal writes are enabled for one explicit store."""
        code = normalize_store_code(store_code or current_store_code())
        return self.enabled and code in self.enabled_store_codes

    @classmethod
    def from_env(cls, project_root: Path) -> TakealotPortalSettings:
        resolved_root = project_root.resolve()
        load_dotenv(resolved_root / ".env", override=False)
        request_timeout = _bounded_float_from_env(
            "TAKEALOT_PORTAL_REQUEST_TIMEOUT_SECONDS",
            DEFAULT_PORTAL_REQUEST_TIMEOUT_SECONDS,
            minimum=1,
            maximum=60,
        )
        task_timeout = _bounded_float_from_env(
            "TAKEALOT_PORTAL_TASK_TIMEOUT_SECONDS",
            DEFAULT_PORTAL_TASK_TIMEOUT_SECONDS,
            minimum=5,
            maximum=120,
        )
        raw_maximum = os.environ.get(
            "TAKEALOT_PORTAL_MAX_TOTAL_QUANTITY",
            str(DEFAULT_PORTAL_MAX_TOTAL_QUANTITY),
        )
        try:
            maximum = int(raw_maximum)
        except ValueError as exc:
            raise SettingsError("平台仓单次总数量上限必须是整数") from exc
        if not 1 <= maximum <= 10_000:
            raise SettingsError("平台仓单次总数量上限必须在 1 到 10000 之间")
        enabled_store_codes: set[str] = set()
        raw_enabled_stores = os.environ.get(
            "TAKEALOT_PORTAL_ENABLED_STORES", ""
        ).strip()
        for raw_code in raw_enabled_stores.replace(";", ",").split(","):
            if not raw_code.strip():
                continue
            try:
                enabled_store_codes.add(normalize_store_code(raw_code))
            except ValueError as exc:
                raise SettingsError(
                    "TAKEALOT_PORTAL_ENABLED_STORES 包含无效店铺代码"
                ) from exc
        return cls(
            enabled=_bool_from_env("TAKEALOT_PORTAL_BFF_ENABLED", default=False),
            base_url=TAKEALOT_SELLER_BFF_URL,
            request_timeout_seconds=request_timeout,
            task_timeout_seconds=task_timeout,
            max_total_quantity=maximum,
            enabled_store_codes=frozenset(enabled_store_codes),
        )


def _bool_from_env(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SettingsError(f"{name} 必须是 true/false、1/0、yes/no 或 on/off")


def _bounded_float_from_env(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(os.environ.get(name, default))
    except ValueError as exc:
        raise SettingsError(f"{name} 必须是数字") from exc
    if not minimum <= value <= maximum:
        raise SettingsError(f"{name} 必须在 {minimum:g} 到 {maximum:g} 之间")
    return value


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
