from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from ctypes import Structure, byref, c_long, c_size_t, sizeof
from ctypes import wintypes
from pathlib import Path
from typing import Any

import pytest

from takealot_ops.dashboard.launcher import (
    build_dashboard_command,
    build_legacy_dashboard_command,
    launch_dashboard,
)
from takealot_ops.settings import DashboardSettings, SettingsError


PROJECT_ROOT = Path(__file__).parents[2]


def test_launcher_builds_exact_lan_erp_command_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAKEALOT_API_KEY", raising=False)
    monkeypatch.setenv("TAKEALOT_DASHBOARD_HOST", "0.0.0.0")
    monkeypatch.setenv("TAKEALOT_DASHBOARD_PORT", "8765")
    settings = DashboardSettings.from_env(PROJECT_ROOT)

    command = build_dashboard_command(settings, python_executable=sys.executable)

    assert command == [
        sys.executable,
        "-m",
        "uvicorn",
        "takealot_ops.erp.web:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8765",
        "--no-access-log",
    ]
    assert build_legacy_dashboard_command(
        settings,
        python_executable=sys.executable,
    )[2:5] == [
        "streamlit",
        "run",
        str(PROJECT_ROOT / "src" / "takealot_ops" / "dashboard" / "app.py"),
    ]


def test_daily_schedule_defaults_to_china_time_after_platform_rollover() -> None:
    script = (PROJECT_ROOT / "scripts" / "install_scheduled_task.ps1").read_text(
        encoding="utf-8"
    )

    assert "[string]$DailyAt = '10:10'" in script
    assert '$TaskName = "Takealot $ChineseTaskSuffix"' in script
    assert "0x5E97, 0x94FA, 0x6570, 0x636E" in script


def test_launcher_rejects_arbitrary_bind_address_before_subprocess() -> None:
    settings = DashboardSettings(
        project_root=PROJECT_ROOT,
        database_url=f"sqlite:///{(PROJECT_ROOT / 'data' / 'takealot.db').as_posix()}",
        dashboard_host="192.168.1.20",
        dashboard_port=8501,
    )
    calls: list[list[str]] = []

    def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(SettingsError, match="0.0.0.0"):
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
    assert "--host" in seen["command"]
    assert "127.0.0.1" in seen["command"]
    assert "--port" in seen["command"]
    assert "8642" in seen["command"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows uv-wrapper regression")
def test_terminating_official_launcher_releases_descendants_and_port(
    tmp_path: Path,
) -> None:
    port = _free_port()
    environment = os.environ.copy()
    environment.pop("TAKEALOT_API_KEY", None)
    environment.update(
        {
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "TAKEALOT_PROJECT_ROOT": str(PROJECT_ROOT),
            "TAKEALOT_DATABASE_URL": f"sqlite:///{(tmp_path / 'missing.db').as_posix()}",
            "TAKEALOT_DASHBOARD_HOST": "127.0.0.1",
            "TAKEALOT_DASHBOARD_PORT": str(port),
        }
    )
    launcher = subprocess.Popen(
        [str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"), "-m",
         "takealot_ops.dashboard.launcher"],
        cwd=PROJECT_ROOT,
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    descendants: set[int] = set()
    try:
        assert _wait_until(lambda: _http_ready(port), timeout=15)
        descendants = _descendant_pids(launcher.pid)
        assert descendants

        launcher.terminate()
        launcher.wait(timeout=5)

        assert _wait_until(
            lambda: all(not _process_alive(pid) for pid in descendants)
            and _port_is_released(port),
            timeout=8,
        ), f"survivors={sorted(pid for pid in descendants if _process_alive(pid))}"
    finally:
        if launcher.poll() is None:
            launcher.terminate()
            launcher.wait(timeout=5)
        for pid in sorted(descendants, reverse=True):
            _terminate_process(pid)
        assert _wait_until(lambda: _port_is_released(port), timeout=5)


class _ProcessEntry(Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _descendant_pids(root_pid: int) -> set[int]:
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise OSError("CreateToolhelp32Snapshot failed")
    pairs: list[tuple[int, int]] = []
    entry = _ProcessEntry()
    entry.dwSize = sizeof(entry)
    try:
        has_entry = bool(kernel32.Process32FirstW(snapshot, byref(entry)))
        while has_entry:
            pairs.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
            has_entry = bool(kernel32.Process32NextW(snapshot, byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)
    descendants: set[int] = set()
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        children = [pid for pid, parent_pid in pairs if parent_pid == parent]
        for child in children:
            if child not in descendants:
                descendants.add(child)
                pending.append(child)
    return descendants


def _process_alive(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel32.CloseHandle(handle)


def _terminate_process(pid: int) -> None:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x0001 | 0x00100000, False, pid)
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 1)
        kernel32.WaitForSingleObject(handle, 3000)
    finally:
        kernel32.CloseHandle(handle)


def _http_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}", timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def _port_is_released(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _free_port() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


def _wait_until(predicate: Callable[[], bool], *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()
