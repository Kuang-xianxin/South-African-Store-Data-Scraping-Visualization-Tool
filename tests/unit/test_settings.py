from __future__ import annotations

from pathlib import Path

import pytest

from takealot_ops.settings import Settings, SettingsError


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


def test_settings_uses_defaults_and_resolves_relative_sqlite_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "TAKEALOT_BASE_URL",
        "TAKEALOT_DATABASE_URL",
        "TAKEALOT_REQUEST_TIMEOUT_SECONDS",
        "TAKEALOT_DASHBOARD_HOST",
        "TAKEALOT_DASHBOARD_PORT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TAKEALOT_API_KEY", "test-api-key")

    settings = Settings.from_env(tmp_path)

    assert settings.project_root == tmp_path
    assert settings.api_key == "test-api-key"
    assert settings.base_url == "https://marketplace-api.takealot.com/v1"
    assert settings.database_url == f"sqlite:///{(tmp_path / 'data' / 'takealot.db').as_posix()}"
    assert settings.request_timeout_seconds == 30.0
    assert settings.dashboard_host == "127.0.0.1"
    assert settings.dashboard_port == 8501


def test_settings_rejects_blank_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAKEALOT_API_KEY", "   ")

    with pytest.raises(SettingsError, match="接口密钥"):
        Settings.from_env(tmp_path)
