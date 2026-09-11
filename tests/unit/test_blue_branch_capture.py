from contextlib import closing
import importlib
from pathlib import Path
import sys
import types

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/ha"))
capture = importlib.import_module("blue_branch_capture")
merge = importlib.import_module("blue_branch_merge")


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.result = None
        self.description = []
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def execute(self, sql, args=None):
        self.conn.commands.append(sql)
        if sql.startswith("SELECT @@server_uuid"):
            self.result = (self.conn.uuid, "fixture", *self.conn.read_only)
        elif sql.startswith("SELECT seed_sha256"):
            self.result = (capture.SEED,)
        elif sql.startswith("SELECT COUNT"):
            self.result = (self.conn.transactions,)
        elif sql == "SHOW REPLICA STATUS":
            self.result = self.conn.replication
            self.description = [("Replica_IO_Running",), ("Replica_SQL_Running",)]
        elif sql.startswith("SELECT GTID_SUBSET"):
            self.result = (1,)
        else:
            assert sql.startswith(("SELECT `", "START TRANSACTION", "SET TRANSACTION"))
    def fetchone(self):
        return self.result
    def fetchmany(self, count):
        rows, self.conn.rows = self.conn.rows[:count], self.conn.rows[count:]
        return rows


class Connection:
    def __init__(self):
        self.uuid = capture.UUIDS["laptop"]
        self.read_only = (1, 1)
        self.transactions = 0
        self.replication = ("No", "No")
        self.commands = []
        self.rows = [(1, "before")]
        self.closed = False
    def cursor(self, *_):
        return Cursor(self)
    def rollback(self):
        self.commands.append("ROLLBACK")
    def close(self):
        self.closed = True


@pytest.mark.parametrize("attribute,value,reason", [
    ("read_only", (0, 0), "both-read-only-switches-required"),
    ("replication", ("No", "Yes"), "replication-must-already-be-stopped"),
    ("replication", ("Connecting", "Yes"), "replication-must-already-be-stopped"),
    ("transactions", 1, "other-transactions-not-drained")])
def test_capture_preflight_never_fences_or_stops_a_live_node(attribute, value, reason):
    conn = Connection()
    setattr(conn, attribute, value)
    result = capture.facts(conn, "laptop")
    assert result["capture_ready"] is False
    assert reason in result["reasons"]
    assert all(sql.startswith(("SELECT", "SHOW")) for sql in conn.commands)


def test_wrong_database_uuid_rejected():
    conn = Connection()
    conn.uuid = "wrong"
    with pytest.raises(ValueError, match="uuid"):
        capture.facts(conn, "laptop")


def setup_capture(monkeypatch):
    conn = Connection()
    monkeypatch.setitem(sys.modules, "blue_node", types.SimpleNamespace(
        config=lambda: {"node": "laptop"}, connection=lambda: conn))
    monkeypatch.setattr(capture, "archive_path", lambda path, **_: path)
    monkeypatch.setattr(capture, "schemas", lambda _: {"sale_items": {
        "columns": ["id", "value"], "primary_key": ["id"], "foreign_keys": []}})
    return conn


def test_complete_frozen_baseline_is_sealed_and_read_only(tmp_path, monkeypatch):
    conn = setup_capture(monkeypatch)
    result = capture.capture(tmp_path / "base.sqlite3", epoch=1)
    header, seal = merge.verify_snapshot(tmp_path / "base.sqlite3")
    assert result["snapshot_seal"] == seal and result["mysql_writes"] == 0
    assert header["identity"]["role"] == "baseline"
    assert any(sql == "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY" for sql in conn.commands)
    assert conn.commands[-1] == "ROLLBACK" and conn.closed


def test_mid_capture_role_change_leaves_unusable_archive(tmp_path, monkeypatch):
    conn = setup_capture(monkeypatch)
    original = capture.facts
    count = 0
    def facts(connection, node):
        nonlocal count
        count += 1
        if count == 2:
            conn.read_only = (0, 0)
        return original(connection, node)
    monkeypatch.setattr(capture, "facts", facts)
    with pytest.raises(ValueError, match="changed-during"):
        capture.capture(tmp_path / "base.sqlite3", epoch=1)
    with pytest.raises(ValueError, match="incomplete"):
        merge.verify_snapshot(tmp_path / "base.sqlite3")
    assert conn.closed


def test_archive_rejects_external_path(tmp_path):
    with pytest.raises(ValueError, match="protected"):
        capture.archive_path(tmp_path / "data.sqlite3", create_parent=True)


def test_mysql_identifiers_cannot_execute_archive_supplied_sql():
    with pytest.raises(ValueError):
        capture.identifier("sale_items`; DROP TABLE erp_users; --")


def test_immutable_snapshot_attaches_read_only(tmp_path):
    path = tmp_path / "snapshot.sqlite3"
    with closing(merge.exclusive_database(path)) as db, db:
        db.execute("CREATE TABLE evidence (id INTEGER)")
    with closing(merge.exclusive_database(tmp_path / "plan.sqlite3")) as db:
        db.execute("ATTACH DATABASE ? AS baseline", (path.resolve().as_uri() + "?mode=ro",))
        import sqlite3
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("INSERT INTO baseline.evidence VALUES (1)")
