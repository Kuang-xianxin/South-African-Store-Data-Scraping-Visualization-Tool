from __future__ import annotations

from pathlib import Path

import pytest

from takealot_ops.settings import Settings, SettingsError, W8Settings


def test_settings_requires_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAKEALOT_API_KEY", raising=False)

    with pytest.raises(SettingsError, match="接口密钥"):
        Settings.from_env(tmp_path)


def test_settings_loads_api_key_from_project_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TAKEALOT_API_KEY", raising=False)
    (tmp_path / ".env").write_text("TAKEALOT_API_KEY=local-file-key\n", encoding="utf-8")

    settings = Settings.from_env(tmp_path)

    assert settings.api_key == "local-file-key"


def test_settings_uses_mysql_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "TAKEALOT_BASE_URL",
        "TAKEALOT_DATABASE_URL",
        "TAKEALOT_REQUEST_TIMEOUT_SECONDS",
        "TAKEALOT_DASHBOARD_HOST",
        "TAKEALOT_DASHBOARD_PORT",
        "TAKEALOT_BACKUP_ROOT",
        "TAKEALOT_BACKUP_DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TAKEALOT_API_KEY", "test-api-key")

    settings = Settings.from_env(tmp_path)

    assert settings.project_root == tmp_path
    assert settings.api_key == "test-api-key"
    assert settings.base_url == "https://marketplace-api.takealot.com/v1"
    assert settings.database_url == (
        "mysql+pymysql://takealot_app@127.0.0.1:3306/"
        "takealot_ops?charset=utf8mb4"
    )
    assert settings.request_timeout_seconds == 30.0
    assert settings.dashboard_host == "0.0.0.0"
    assert settings.dashboard_port == 8501
    assert settings.backup_root == tmp_path / "backups"
    assert settings.backup_database_url is None


def test_settings_loads_dedicated_backup_location_and_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAKEALOT_API_KEY", "test-api-key")
    monkeypatch.setenv("TAKEALOT_BACKUP_ROOT", "D:/takealot-backups")
    monkeypatch.setenv(
        "TAKEALOT_BACKUP_DATABASE_URL",
        "mysql+pymysql://takealot_backup:secret@127.0.0.1:3306/"
        "takealot_ops?charset=utf8mb4",
    )

    settings = Settings.from_env(tmp_path)

    assert settings.backup_root == Path("D:/takealot-backups")
    assert settings.backup_database_url is not None


def test_settings_retains_relative_sqlite_resolution_for_migration_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAKEALOT_API_KEY", "test-api-key")
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", "sqlite:///data/source.db")

    settings = Settings.from_env(tmp_path)

    assert settings.database_url == (
        f"sqlite:///{(tmp_path / 'data' / 'source.db').as_posix()}"
    )


def test_settings_rejects_remote_mysql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAKEALOT_API_KEY", "test-api-key")
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        "mysql+pymysql://user:secret@db.example.com/takealot_ops",
    )

    with pytest.raises(SettingsError, match="本机"):
        Settings.from_env(tmp_path)


def test_settings_rejects_blank_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAKEALOT_API_KEY", "   ")

    with pytest.raises(SettingsError, match="接口密钥"):
        Settings.from_env(tmp_path)


def test_w8_settings_loads_optional_token_and_formal_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("W8_API_TOKEN", "fixture-w8-token")
    monkeypatch.delenv("W8_BASE_URL", raising=False)
    monkeypatch.delenv("W8_REQUEST_TIMEOUT_SECONDS", raising=False)

    settings = W8Settings.from_env(tmp_path)

    assert settings.configured is True
    assert settings.token == "fixture-w8-token"
    assert settings.base_url == "https://crgyl.w8soft.net/prod-api/w8"
    assert settings.request_timeout_seconds == 30.0


def test_w8_settings_allows_unconfigured_optional_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("W8_API_TOKEN", raising=False)

    settings = W8Settings.from_env(tmp_path)

    assert settings.configured is False


@pytest.mark.parametrize(
    "url",
    [
        "http://crgyl.w8soft.net/prod-api/w8",
        "https://w8soft.net.example.com/prod-api/w8",
        "https://example.com/prod-api/w8",
        "https://crgyl.w8soft.net/not-the-api",
    ],
)
def test_w8_settings_rejects_untrusted_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("W8_BASE_URL", url)

    with pytest.raises(SettingsError, match="w8soft.net"):
        W8Settings.from_env(tmp_path)
