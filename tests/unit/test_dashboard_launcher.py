from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from takealot_ops.dashboard.launcher import build_dashboard_command, launch_dashboard
from takealot_ops.settings import DashboardSettings, SettingsError


PROJECT_ROOT = Path(__file__).parents[2]


def test_launcher_builds_exact_loopback_streamlit_command_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAKEALOT_API_KEY", raising=False)
    monkeypatch.setenv("TAKEALOT_DASHBOARD_HOST", "localhost")
    monkeypatch.setenv("TAKEALOT_DASHBOARD_PORT", "8765")
    settings = DashboardSettings.from_env(PROJECT_ROOT)

    command = build_dashboard_command(settings, python_executable=sys.executable)

    assert command == [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(PROJECT_ROOT / "src" / "takealot_ops" / "dashboard" / "app.py"),
        "--server.address=127.0.0.1",
        "--server.port=8765",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
    ]


def test_launcher_rejects_non_loopback_settings_before_subprocess() -> None:
    settings = DashboardSettings(
        project_root=PROJECT_ROOT,
        database_url=f"sqlite:///{(PROJECT_ROOT / 'data' / 'takealot.db').as_posix()}",
        dashboard_host="0.0.0.0",
        dashboard_port=8501,
    )
    calls: list[list[str]] = []

    def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(SettingsError, match="loopback"):
        launch_dashboard(settings, runner=runner)

    assert calls == []


def test_launcher_invokes_hidden_subprocess_from_project_root() -> None:
    settings = DashboardSettings(
        project_root=PROJECT_ROOT,
        database_url=f"sqlite:///{(PROJECT_ROOT / 'data' / 'takealot.db').as_posix()}",
        dashboard_host="127.0.0.1",
        dashboard_port=8642,
    )
    seen: dict[str, object] = {}

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["command"] = command
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 7)

    result = launch_dashboard(settings, runner=runner)

    assert result == 7
    assert seen["cwd"] == settings.project_root
    assert seen["check"] is False
    assert seen["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert "--server.address=127.0.0.1" in seen["command"]
    assert "--server.port=8642" in seen["command"]
