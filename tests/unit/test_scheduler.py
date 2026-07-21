from __future__ import annotations

from pathlib import Path

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


def test_scheduler_script_binds_dashboard_to_127_0_0_1() -> None:
    script = (PROJECT_ROOT / "scripts" / "install_scheduled_task.ps1").read_text(
        encoding="utf-8"
    )
    assert "127.0.0.1" in script
    assert "TAKEALOT_DASHBOARD_HOST" in script
    assert "python -m takealot_ops.cli daily-run" in script

