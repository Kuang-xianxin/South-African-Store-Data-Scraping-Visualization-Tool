"""Official loopback-only process launcher for the Streamlit dashboard."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from takealot_ops.settings import DashboardSettings, SettingsError


Runner = Callable[..., subprocess.CompletedProcess[Any]]


def build_dashboard_command(
    settings: DashboardSettings, *, python_executable: str = sys.executable
) -> list[str]:
    """Build the Streamlit command with CLI options that override local config."""
    _validate_launch_settings(settings)
    app_path = settings.project_root / "src" / "takealot_ops" / "dashboard" / "app.py"
    if not app_path.is_file():
        raise SettingsError(f"dashboard app was not found: {app_path}")
    return [
        python_executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.address=127.0.0.1",
        f"--server.port={settings.dashboard_port}",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
    ]


def launch_dashboard(
    settings: DashboardSettings,
    *,
    runner: Runner = subprocess.run,
) -> int:
    """Run the dashboard as a hidden, blocking local Streamlit subprocess."""
    command = build_dashboard_command(settings)
    completed = runner(
        command,
        cwd=settings.project_root,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return int(completed.returncode)


def main() -> int:
    """Validate environment-backed settings and launch the official local server."""
    project_root = Path(os.environ.get("TAKEALOT_PROJECT_ROOT", Path.cwd())).resolve()
    try:
        settings = DashboardSettings.from_env(project_root)
        return launch_dashboard(settings)
    except SettingsError as exc:
        print(f"Dashboard configuration error: {exc}", file=sys.stderr)
        return 2


def _validate_launch_settings(settings: DashboardSettings) -> None:
    if settings.dashboard_host not in {"127.0.0.1", "localhost"}:
        raise SettingsError("dashboard launcher requires a loopback host")
    if not 1 <= settings.dashboard_port <= 65535:
        raise SettingsError("dashboard launcher port must be between 1 and 65535")


if __name__ == "__main__":
    raise SystemExit(main())
