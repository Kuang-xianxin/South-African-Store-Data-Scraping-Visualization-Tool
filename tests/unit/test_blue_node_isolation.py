from __future__ import annotations

import importlib.util
from pathlib import Path
import zipfile

import pytest


SPEC = importlib.util.spec_from_file_location(
    "blue_node", Path(__file__).resolve().parents[2] / "scripts/ha/blue_node.py"
)
assert SPEC and SPEC.loader
blue = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(blue)


@pytest.mark.parametrize("computer,server_id", [
    ("DESKTOP-NTRMANG", 101), ("LAPTOP-2T5MN8EU", 102),
])
def test_blue_database_requires_exact_node_port_and_data_directory(computer, server_id):
    blue.validate_database_identity((computer, server_id, 3307, "D:/TakealotBlue/mysql/data/"), computer)
    for row in [
        (computer, 1, 3306, "C:/ProgramData/MySQL/MySQL Server 8.0/Data"),
        (computer, server_id, 3306, "D:/TakealotBlue/mysql/data"),
        (computer, server_id, 3307, "D:/TakealotMySQLReplica/data"),
        ("UNKNOWN", server_id, 3307, "D:/TakealotBlue/mysql/data"),
    ]:
        with pytest.raises(RuntimeError, match="identity mismatch"):
            blue.validate_database_identity(row, computer)


def test_blue_mysql_configuration_is_separate_and_has_durable_gtids():
    rendered = blue.mysql_configuration("DESKTOP-NTRMANG", Path("C:/Program Files/MySQL"))
    for setting in ("port=3307", "bind-address=127.0.0.1", "server-id=101", "gtid-mode=ON",
                    "sync-binlog=1", "innodb-flush-log-at-trx-commit=1", "mysqlx=OFF",
                    "read-only=ON", "super-read-only=ON"):
        assert setting in rendered.splitlines()
    assert "3306" not in rendered
    assert "TakealotMySQLReplica" not in rendered


def test_unrecognized_computer_cannot_prepare_a_blue_node():
    with pytest.raises(RuntimeError, match="approved blue node"):
        blue.node_identity("UNRELATED-SERVER")


def test_restore_rejects_wrong_checksum_before_database_connection(tmp_path, monkeypatch):
    seed = tmp_path / "test.sql"
    seed.write_bytes(b"fixture")
    monkeypatch.setattr(blue, "config", lambda: {})
    monkeypatch.setattr(blue, "connection", lambda: pytest.fail("must not access MySQL"))
    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        blue.restore(seed, "0" * 64)


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows DPAPI")
def test_machine_dpapi_round_trip():
    payload = b"non-secret-test-fixture"
    protected = blue.protect_secret(payload)
    assert payload not in protected
    assert blue.protect_secret(protected, decrypt=True) == payload


def test_release_checksum_rejected_before_directory_or_database_changes(tmp_path, monkeypatch):
    archive = tmp_path / "release.zip"
    archive.write_bytes(b"not-a-release")
    monkeypatch.setattr(blue, "config", lambda: {})
    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        blue.install_release(archive, "0" * 64)


@pytest.mark.parametrize("name", ["../outside.txt", "D:/outside.txt", "src/../../outside.txt", ".env"])
def test_release_rejects_unsafe_paths_and_preserves_existing_app(tmp_path, monkeypatch, name):
    class FreePort:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def connect_ex(self, *_):
            return 1

    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(name, "test")
    app = tmp_path / "app"
    app.mkdir()
    preserved = app / "preserved.txt"
    preserved.write_text("original")
    monkeypatch.setattr(blue, "ROOT", tmp_path)
    monkeypatch.setattr(blue, "config", lambda: {})
    monkeypatch.setattr(blue.socket, "socket", FreePort)
    with pytest.raises(RuntimeError, match="Unsafe release path"):
        blue.install_release(archive, blue.digest(archive))
    assert preserved.read_text() == "original"


def test_service_installer_is_scoped_and_waits_for_process_exit():
    script = (Path(__file__).resolve().parents[2] /
              "scripts/ha/install_blue_stage_services.ps1").read_text(encoding="utf-8")
    assert "TakealotBlueMySQL" in script
    assert "$PendingBlueProcesses.Count -eq 0" in script
    assert "blue_node.connection()" in script
    assert "-AtStartup" in script
    assert "-LogonType ServiceAccount" in script
    assert "-LocalPort 3306" not in script
    assert "-LocalPort 8501" not in script
