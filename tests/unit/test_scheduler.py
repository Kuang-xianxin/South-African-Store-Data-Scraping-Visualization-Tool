from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from takealot_ops.scheduler import backup_database
from takealot_ops.settings import Settings


PROJECT_ROOT = Path(__file__).parents[2]


def _settings(project_root: Path, database_path: Path) -> Settings:
    return Settings(
        project_root=project_root,
        api_key="fixture-key",
        base_url="https://example.invalid/v1",
        database_url=f"sqlite:///{database_path.as_posix()}",
        request_timeout_seconds=1.0,
        dashboard_host="127.0.0.1",
        dashboard_port=8501,
    )


def test_database_backup_keeps_only_eight_newest_files(tmp_path: Path) -> None:
    database_path = tmp_path / "data" / "takealot.db"
    database_path.parent.mkdir(parents=True)
    database_path.write_bytes(b"")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for index in range(8):
        (backup_dir / f"takealot-202607{index + 1:02d}-000000-000000.db").write_bytes(b"old")

    newest = backup_database(_settings(tmp_path, database_path), keep=8)

    backups = sorted(backup_dir.glob("takealot-*.db"))
    assert newest in backups
    assert len(backups) == 8
    assert not (backup_dir / "takealot-20260701-000000-000000.db").exists()


def test_mysql_backup_uses_password_environment_not_command_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        project_root=tmp_path,
        api_key="fixture-key",
        base_url="https://example.invalid/v1",
        database_url=(
            "mysql+pymysql://takealot_app:fixture-secret@127.0.0.1:3306/"
            "takealot_ops?charset=utf8mb4"
        ),
        request_timeout_seconds=1.0,
        dashboard_host="127.0.0.1",
        dashboard_port=8501,
    )
    monkeypatch.setattr(
        "takealot_ops.scheduler._find_mysql_program",
        lambda _: Path("C:/mysql/bin/mysqldump.exe"),
    )
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["password"] = kwargs["env"].get("MYSQL_PWD")
        kwargs["stdout"].write(b"mysql dump")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("takealot_ops.scheduler.subprocess.run", fake_run)

    backup = backup_database(settings)

    assert backup.suffix == ".sql"
    assert backup.read_bytes() == b"mysql dump"
    assert captured["password"] == "fixture-secret"
    assert "fixture-secret" not in " ".join(captured["command"])


def test_scheduler_script_installs_two_captures_and_one_deadline() -> None:
    script = (PROJECT_ROOT / "scripts" / "install_scheduled_task.ps1").read_text(
        encoding="utf-8"
    )
    assert "daily-report-run --slot morning" in script
    assert "daily-report-run --slot evening" in script
    assert "daily-report-deadline" in script
    assert "-MultipleInstances IgnoreNew" in script

