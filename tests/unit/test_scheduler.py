from __future__ import annotations

import gzip
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from takealot_ops.scheduler import (
    LocalBackupRetention,
    _apply_local_backup_retention,
    backup_database,
    verify_local_backup,
)
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
        kwargs["stdout"].write(
            b"-- MySQL dump 10.13\nCREATE TABLE example (id int);\n"
            b"-- Dump completed on 2026-07-29 10:00:00\n"
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("takealot_ops.scheduler.subprocess.run", fake_run)

    backup = backup_database(settings)

    assert backup.name.endswith(".sql.gz")
    assert gzip.decompress(backup.read_bytes()).startswith(b"-- MySQL dump")
    assert captured["password"] == "fixture-secret"
    assert "fixture-secret" not in " ".join(captured["command"])
    result = verify_local_backup(backup)
    manifest = json.loads(Path(f"{backup}.json").read_text(encoding="utf-8"))
    assert result.raw_bytes == manifest["raw_bytes"]
    assert result.compressed_bytes == manifest["compressed_bytes"]
    assert result.sha256 == manifest["sha256"]
    assert Path(f"{backup}.sha256").read_text(encoding="ascii").startswith(
        result.sha256
    )


def test_mysql_backup_removes_partial_files_after_dump_failure(
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

    def fake_run(command, **kwargs):
        kwargs["stdout"].write(b"incomplete")
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr("takealot_ops.scheduler.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="MySQL backup failed"):
        backup_database(settings)

    assert not list((tmp_path / "backups").glob("takealot-*"))


def test_mysql_backup_records_binlog_coordinates_with_dedicated_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        project_root=tmp_path,
        api_key="fixture-key",
        base_url="https://example.invalid/v1",
        database_url=(
            "mysql+pymysql://takealot_app:app-secret@127.0.0.1:3306/"
            "takealot_ops?charset=utf8mb4"
        ),
        request_timeout_seconds=1.0,
        dashboard_host="127.0.0.1",
        dashboard_port=8501,
        backup_root=tmp_path / "d-drive-backups",
        backup_database_url=(
            "mysql+pymysql://takealot_backup:backup-secret@127.0.0.1:3306/"
            "takealot_ops?charset=utf8mb4"
        ),
    )
    monkeypatch.setattr(
        "takealot_ops.scheduler._find_mysql_program",
        lambda _: Path("C:/mysql/bin/mysqldump.exe"),
    )
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["password"] = kwargs["env"].get("MYSQL_PWD")
        kwargs["stdout"].write(
            b"-- MySQL dump 10.13\n"
            b"-- CHANGE MASTER TO MASTER_LOG_FILE='server-bin.000123', "
            b"MASTER_LOG_POS=4567;\n"
            b"-- Dump completed on 2026-07-29 10:00:00\n"
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("takealot_ops.scheduler.subprocess.run", fake_run)

    backup = backup_database(settings)
    manifest = json.loads(Path(f"{backup}.json").read_text(encoding="utf-8"))

    assert "--source-data=2" in captured["command"]
    assert captured["password"] == "backup-secret"
    assert manifest["binlog"] == {
        "file": "server-bin.000123",
        "position": 4567,
    }
    assert backup.parent == tmp_path / "d-drive-backups"


def test_local_backup_retention_keeps_tiered_restore_points(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    names = (
        "takealot-20260728-120000-000000.sql.gz",
        "takealot-20260728-100000-000000.sql.gz",
        "takealot-20260620-120000-000000.sql.gz",
        "takealot-20260620-100000-000000.sql.gz",
        "takealot-20260310-120000-000000.sql.gz",
        "takealot-20260309-100000-000000.sql.gz",
        "takealot-20250310-120000-000000.sql.gz",
        "takealot-20250309-100000-000000.sql.gz",
        "takealot-20180101-100000-000000.sql.gz",
    )
    for name in names:
        archive = backup_dir / name
        archive.write_bytes(b"archive")
        Path(f"{archive}.json").write_text("{}", encoding="utf-8")
        Path(f"{archive}.sha256").write_text("checksum", encoding="ascii")

    _apply_local_backup_retention(
        backup_dir,
        now=datetime(2026, 7, 29, tzinfo=UTC),
        policy=LocalBackupRetention(),
    )

    retained = {path.name for path in backup_dir.glob("*.sql.gz")}
    assert "takealot-20260728-120000-000000.sql.gz" in retained
    assert "takealot-20260728-100000-000000.sql.gz" in retained
    assert "takealot-20260620-120000-000000.sql.gz" in retained
    assert "takealot-20260620-100000-000000.sql.gz" not in retained
    assert "takealot-20260310-120000-000000.sql.gz" in retained
    assert "takealot-20260309-100000-000000.sql.gz" not in retained
    assert "takealot-20250310-120000-000000.sql.gz" in retained
    assert "takealot-20250309-100000-000000.sql.gz" not in retained
    assert "takealot-20180101-100000-000000.sql.gz" not in retained
    assert not (backup_dir / f"{names[-1]}.json").exists()


def test_scheduler_script_installs_daily_jobs_and_current_user_erp_startup() -> None:
    script = (PROJECT_ROOT / "scripts" / "install_scheduled_task.ps1").read_text(
        encoding="utf-8"
    )
    assert "daily-report-run --slot morning" in script
    assert "daily-report-run --slot evening" in script
    assert "daily-report-run --slot pre_close" in script
    assert "daily-report-deadline" in script
    assert "trigger-competitor-collection" in script
    assert "Unregister-ScheduledTask -TaskName $ObsoleteTaskName" in script
    assert "-MultipleInstances IgnoreNew" in script
    assert "-RestartCount 3" in script
    assert "-RestartInterval (New-TimeSpan -Minutes 5)" in script
    assert "exit `$LASTEXITCODE" in script
    assert "scripts\\ensure_erp_started.ps1" in script
    assert "restart_erp.ps1" not in script
    assert "New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser" in script
    assert "New-ScheduledTaskTrigger -AtStartup" not in script
    assert "$StartupTrigger.Delay = 'PT30S'" in script
    assert "-LogonType Interactive" in script
    assert "-LogonType S4U" not in script
    assert "-UserId 'SYSTEM'" not in script
    assert "-RunLevel Limited" in script
    assert "-MultipleInstances IgnoreNew" in script
    assert "-WorkingDirectory $ResolvedProjectPath" in script
    assert "-RestartCount 12" in script
    assert "-RestartInterval (New-TimeSpan -Minutes 1)" in script
    assert "-AllowStartIfOnBatteries" in script
    assert "-DontStopIfGoingOnBatteries" in script


def test_erp_startup_guard_is_idempotent_and_uses_formal_restart_chain() -> None:
    script = (PROJECT_ROOT / "scripts" / "ensure_erp_started.ps1").read_text(
        encoding="utf-8"
    )

    assert "Get-Service -Name 'MySQL80'" in script
    assert "Invoke-RestMethod -Uri $HealthUrl" in script
    assert "$HealthResponse.status -eq 'ok'" in script
    assert "$HealthResponse.application -eq 'takealot-erp'" in script
    assert "return" in script
    assert "scripts\\restart_erp.ps1" in script
    assert "& $RestartScriptPath -HealthTimeoutSeconds $HealthTimeoutSeconds" in script
    assert "erp-startup.log" in script
    assert "System.Threading.Mutex" in script
    assert "Local\\TakealotErpStartup" in script
    assert "$StartupMutex.WaitOne" in script

    restart_script = (PROJECT_ROOT / "scripts" / "restart_erp.ps1").read_text(
        encoding="utf-8"
    )
    assert "Local\\TakealotErpStartup" in restart_script
    assert "$restartMutex.WaitOne" in restart_script
    assert "$restartMutex.ReleaseMutex()" in restart_script
    assert "Push-Location -LiteralPath $projectRoot" in restart_script
    assert 'Import-UserEnvironmentVariableIfMissing -Name "DASHSCOPE_API_KEY"' in (
        restart_script
    )
    assert 'Import-UserEnvironmentVariableIfMissing -Name "ARK_API_KEY"' in (
        restart_script
    )
    assert "codex-cli 0.147.0" not in restart_script
    assert "login status" not in restart_script


def test_binlog_scheduler_requires_d_drive_and_continuous_restart() -> None:
    script = (PROJECT_ROOT / "scripts" / "install_binlog_archive_task.ps1").read_text(
        encoding="utf-8"
    )
    grants = (
        PROJECT_ROOT / "scripts" / "provision_binlog_backup_user.sql.example"
    ).read_text(encoding="utf-8")
    archive_loop = (
        PROJECT_ROOT / "scripts" / "run_binlog_archive_loop.ps1"
    ).read_text(encoding="utf-8")
    maintenance_loop = (
        PROJECT_ROOT / "scripts" / "run_binlog_maintenance_loop.ps1"
    ).read_text(encoding="utf-8")

    assert "$ProjectDrive -ne 'D'" in script
    assert "binlog-archive-status --preflight" in script
    assert "TakealotMySQLBinlogArchive" in script
    assert "TakealotMySQLBinlogMaintenance" in script
    assert "-AtStartup" in script
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in script
    assert "binlog-archive" in archive_loop
    assert "Start-Sleep -Seconds 60" in archive_loop
    assert "binlog-archive-maintain" in maintenance_loop
    assert "AddDays(1)" in maintenance_loop
    assert "REPLICATION CLIENT" in grants
    assert "REPLICATION SLAVE" in grants
    assert "takealot_backup" in grants

    configurator = (
        PROJECT_ROOT / "scripts" / "configure_binlog_backup.ps1"
    ).read_text(encoding="utf-8")
    assert "Read-Host" in configurator
    assert "-AsSecureString" in configurator
    assert "TAKEALOT_BACKUP_ROOT" in configurator
    assert "D'" in configurator
    assert "--password" not in configurator

