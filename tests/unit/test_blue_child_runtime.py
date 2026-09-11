from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_child_startup_error_cannot_continue(tmp_path):
    (tmp_path / "sitecustomize.py").write_text(
        (ROOT / "scripts/ha/blue_child_sitecustomize.py").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "blue_runtime.py").write_text(
        "def prepare_environment():\n    raise RuntimeError('wrong database')\n", encoding="utf-8")
    result = subprocess.run([sys.executable, "-c", "print('UNSAFE_CONTINUATION')"],
                            env=dict(os.environ, PYTHONPATH=str(tmp_path), TAKEALOT_BLUE_CHILD_GUARD="1"),
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 78
    assert "UNSAFE_CONTINUATION" not in result.stdout
    assert "process refused" in result.stderr


def test_blue_backup_keeps_verified_tls_and_password_out_of_argv(monkeypatch, tmp_path):
    from takealot_ops import scheduler

    settings = SimpleNamespace(project_root=tmp_path, database_url=(
        "mysql+pymysql://blue_web_main:dummy-secret@127.0.0.1:3307/takealot_ops"
        "?ssl_ca=C%3A%2Fblue%2Fca.pem&ssl_check_hostname=true"))
    captured = []

    def run(command, **kwargs):
        captured.extend(command)
        assert kwargs["env"]["MYSQL_PWD"] == "dummy-secret"
        kwargs["stdout"].write(b"-- MySQL dump 10.13\nCREATE TABLE test (id int);\n-- Dump completed on 2026-09-05\n")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(scheduler, "_find_mysql_program", lambda _: Path("mysqldump.exe"))
    monkeypatch.setattr(scheduler.subprocess, "run", run)
    scheduler.backup_database(settings)
    assert "--ssl-mode=VERIFY_IDENTITY" in captured
    assert "--ssl-ca=C:/blue/ca.pem" in captured
    assert "--port=3307" in captured
    assert not any("dummy-secret" in argument for argument in captured)


def test_missing_explicit_mysql_binary_never_falls_back(monkeypatch, tmp_path):
    from takealot_ops.scheduler import _find_mysql_program

    monkeypatch.setenv("TAKEALOT_MYSQL_BIN", str(tmp_path))
    with pytest.raises(RuntimeError, match="missing"):
        _find_mysql_program("mysqldump.exe")
