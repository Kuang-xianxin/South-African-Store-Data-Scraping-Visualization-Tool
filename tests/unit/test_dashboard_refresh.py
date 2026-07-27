from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from takealot_ops.dashboard.refresh import run_dashboard_refresh


def _project_with_python(tmp_path: Path) -> Path:
    python = tmp_path / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"test")
    return tmp_path


def test_refresh_runs_complete_daily_workflow_with_project_python(tmp_path: Path) -> None:
    project_root = _project_with_python(tmp_path)
    captured: dict[str, Any] = {}

    def runner(command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = list(command)
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, "完成", "")

    result = run_dashboard_refresh(project_root, runner=runner)

    assert result.succeeded is True
    assert result.message == "数据刷新完成，本次手动数据已纳入当前10:00核对周期。"
    assert captured["command"] == [
        str(project_root / ".venv" / "Scripts" / "python.exe"),
        "-m",
        "takealot_ops.cli",
        "daily-report-run",
        "--slot",
        "manual",
    ]
    assert captured["kwargs"]["cwd"] == project_root.resolve()
    assert captured["kwargs"]["timeout"] == 600


def test_refresh_failure_never_exposes_subprocess_output(tmp_path: Path) -> None:
    project_root = _project_with_python(tmp_path)

    def runner(command: Sequence[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 4, "secret-output", "raw-error")

    result = run_dashboard_refresh(project_root, runner=runner)

    assert result.succeeded is False
    assert "secret-output" not in result.message
    assert "raw-error" not in result.message
    assert "本地日志" in result.message


def test_refresh_handles_timeout_and_missing_environment(tmp_path: Path) -> None:
    missing = run_dashboard_refresh(tmp_path)
    assert missing.succeeded is False
    assert "未找到项目运行环境" in missing.message

    project_root = _project_with_python(tmp_path)

    def runner(command: Sequence[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 600)

    timed_out = run_dashboard_refresh(project_root, runner=runner)
    assert timed_out.succeeded is False
    assert "超过10分钟" in timed_out.message
