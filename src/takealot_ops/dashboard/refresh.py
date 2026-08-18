"""Run the complete dashboard refresh workflow from the local web interface."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DashboardRefreshResult:
    """Operator-facing outcome of one complete refresh attempt."""

    succeeded: bool
    message: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


def run_dashboard_refresh(
    project_root: Path,
    *,
    store_code: str = "current",
    all_stores: bool = False,
    runner: Runner = subprocess.run,
    timeout_seconds: int | None = None,
) -> DashboardRefreshResult:
    """Run collection, metric rebuild, reports, integrity check, and backup."""
    root = project_root.resolve()
    python = root / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        return DashboardRefreshResult(False, "未找到项目运行环境，无法刷新数据。")

    base_command = (
        str(python),
        "-m",
        "takealot_ops.cli",
        "daily-report-run",
        "--slot",
        "manual",
    )
    command: Sequence[str]
    if all_stores:
        command = (*base_command, "--all-stores")
    elif store_code != "current":
        command = (*base_command, "--store", store_code)
    else:
        command = base_command
    if timeout_seconds is not None:
        effective_timeout_seconds = timeout_seconds
    else:
        effective_timeout_seconds = 3_600 if all_stores else 600
    try:
        completed = runner(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=effective_timeout_seconds,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return DashboardRefreshResult(
            False,
            (
                "全部店铺刷新超过60分钟仍未完成，请检查网络和本地日志。"
                if all_stores
                else "刷新超过10分钟仍未完成，请检查网络和本地日志。"
            ),
        )
    except OSError:
        return DashboardRefreshResult(False, "无法启动刷新任务，请检查本地运行环境。")

    if completed.returncode != 0:
        return DashboardRefreshResult(
            False, "刷新失败，请检查接口密钥、网络和本地日志后重试。"
        )
    return DashboardRefreshResult(
        True,
        (
            "全部已配置店铺数据刷新完成，"
            "各店本次手动数据已纳入当前10:00核对周期。"
            if all_stores
            else "数据刷新完成，本次手动数据已纳入当前10:00核对周期。"
        ),
    )
