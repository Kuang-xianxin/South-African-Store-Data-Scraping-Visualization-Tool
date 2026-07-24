"""Official process launcher for the authenticated local ERP."""

from __future__ import annotations

import os
import subprocess
import sys
import ctypes
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any

from takealot_ops.settings import DashboardSettings, SettingsError


Runner = Callable[..., subprocess.CompletedProcess[Any]]

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WINDOWS_JOB_HANDLE: int | None = None


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _ensure_windows_kill_on_close_job() -> None:
    """Put this launcher and all future children in a kill-on-close Windows job."""
    global _WINDOWS_JOB_HANDLE
    if sys.platform != "win32" or _WINDOWS_JOB_HANDLE is not None:
        return

    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job_handle = kernel32.CreateJobObjectW(None, None)
    if not job_handle:
        raise ctypes.WinError(ctypes.get_last_error())

    information = _JobObjectExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    try:
        configured = kernel32.SetInformationJobObject(
            job_handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        if not configured:
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.AssignProcessToJobObject(job_handle, kernel32.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel32.CloseHandle(job_handle)
        raise

    # Keep the handle open for the launcher's lifetime. If Windows terminates the
    # launcher, the OS closes it and atomically terminates every descendant in the job.
    _WINDOWS_JOB_HANDLE = int(job_handle)


def _run_dashboard_process(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    _ensure_windows_kill_on_close_job()
    return subprocess.run(command, **kwargs)


def build_dashboard_command(
    settings: DashboardSettings, *, python_executable: str = sys.executable
) -> list[str]:
    """Build the unified authenticated Vue ERP command."""
    _validate_launch_settings(settings)
    return [
        python_executable,
        "-m",
        "uvicorn",
        "takealot_ops.erp.web:app",
        "--host",
        settings.dashboard_host,
        "--port",
        str(settings.dashboard_port),
        "--no-access-log",
    ]


def build_legacy_dashboard_command(
    settings: DashboardSettings, *, python_executable: str = sys.executable
) -> list[str]:
    """Build the retained Streamlit fallback command."""
    _validate_launch_settings(settings)
    app_path = settings.project_root / "src" / "takealot_ops" / "dashboard" / "app.py"
    if not app_path.is_file():
        raise SettingsError(f"未找到旧版看板程序：{app_path}")
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
        "--server.maxUploadSize=100",
    ]


def launch_dashboard(
    settings: DashboardSettings,
    *,
    runner: Runner = _run_dashboard_process,
) -> int:
    """Run the unified Vue ERP on the configured host and port."""
    command = build_dashboard_command(settings)
    completed = runner(
        command,
        cwd=settings.project_root,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return int(completed.returncode)


def launch_legacy_dashboard(
    settings: DashboardSettings,
    *,
    runner: Runner = _run_dashboard_process,
) -> int:
    """Run the previous Streamlit dashboard as a compatibility fallback."""
    completed = runner(
        build_legacy_dashboard_command(settings),
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
        print(f"看板配置错误：{exc}", file=sys.stderr)
        return 2


def _validate_launch_settings(settings: DashboardSettings) -> None:
    if settings.dashboard_host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        raise SettingsError("看板启动地址只能是 0.0.0.0、127.0.0.1 或 localhost")
    if not 1 <= settings.dashboard_port <= 65535:
        raise SettingsError("看板端口必须是1到65535之间的整数")


if __name__ == "__main__":
    raise SystemExit(main())
