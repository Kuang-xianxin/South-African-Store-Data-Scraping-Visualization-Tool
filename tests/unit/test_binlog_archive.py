from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from takealot_ops.binlog_archive import (
    BinlogArchiveSettings,
    RemoteBinlog,
    build_binlog_archive_plan,
    inspect_binlog_archive,
    maintain_binlog_archive,
    run_continuous_binlog_archive,
)


def _settings(tmp_path: Path) -> BinlogArchiveSettings:
    return BinlogArchiveSettings(
        project_root=tmp_path,
        backup_root=tmp_path / "backups",
        database_url=(
            "mysql+pymysql://takealot_backup:fixture-secret@127.0.0.1:3306/"
            "takealot_ops?charset=utf8mb4"
        ),
    )


def _remote_logs() -> list[RemoteBinlog]:
    return [
        RemoteBinlog("server-bin.000001", 1024, False),
        RemoteBinlog("server-bin.000002", 2048, False),
        RemoteBinlog("server-bin.000003", 4096, False),
    ]


def test_archive_plan_resumes_from_latest_local_remote_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.archive_dir.mkdir(parents=True)
    (settings.archive_dir / "server-bin.000001").write_bytes(b"\xfebin")
    (settings.archive_dir / "server-bin.000002").write_bytes(b"\xfebin")

    plan = build_binlog_archive_plan(
        settings,
        _remote_logs(),
        executable=Path("C:/mysql/mysqlbinlog.exe"),
    )

    assert plan.start_file == "server-bin.000002"
    assert "--stop-never" in plan.command
    assert "--raw" in plan.command
    assert "fixture-secret" not in " ".join(plan.command)
    assert str(settings.archive_dir) in " ".join(plan.command)


def test_continuous_archive_passes_password_only_in_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        "takealot_ops.binlog_archive.list_remote_binlogs",
        lambda _settings: _remote_logs(),
    )
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["password"] = kwargs["env"].get("MYSQL_PWD")
        return subprocess.CompletedProcess(command, 0)

    result = run_continuous_binlog_archive(
        settings,
        runner=fake_run,
        executable=Path("C:/mysql/mysqlbinlog.exe"),
    )

    assert result == 0
    assert captured["password"] == "fixture-secret"
    assert "fixture-secret" not in " ".join(captured["command"])
    assert (settings.archive_dir / "archive-status.json").is_file()


def test_maintenance_verifies_closed_files_and_deletes_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    settings.archive_dir.mkdir(parents=True)
    old_archive = settings.archive_dir / "server-bin.000001"
    closed_archive = settings.archive_dir / "server-bin.000002"
    current_archive = settings.archive_dir / "server-bin.000003"
    for archive in (old_archive, closed_archive, current_archive):
        archive.write_bytes(b"\xfebin")
    now = datetime(2026, 7, 29, tzinfo=UTC)
    old_time = (now - timedelta(days=36)).timestamp()
    os.utime(old_archive, (old_time, old_time))
    monkeypatch.setattr(
        "takealot_ops.binlog_archive.list_remote_binlogs",
        lambda _settings: _remote_logs(),
    )

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0)

    result = maintain_binlog_archive(
        settings,
        now=now,
        runner=fake_run,
        executable=Path("C:/mysql/mysqlbinlog.exe"),
    )

    assert result == {"verified": 2, "deleted": 1}
    assert not old_archive.exists()
    assert Path(f"{closed_archive}.sha256").is_file()
    assert not Path(f"{current_archive}.sha256").exists()


def test_status_reports_missing_files_and_current_lag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    settings.archive_dir.mkdir(parents=True)
    current_archive = settings.archive_dir / "server-bin.000003"
    current_archive.write_bytes(b"\xfebin" + b"x" * 100)
    monkeypatch.setattr(
        "takealot_ops.binlog_archive.list_remote_binlogs",
        lambda _settings: _remote_logs(),
    )

    status = inspect_binlog_archive(settings)

    assert not status.healthy
    assert status.local_current_size == 104
    assert status.lag_bytes == 3992
    assert status.missing_remote_files == (
        "server-bin.000001",
        "server-bin.000002",
    )
