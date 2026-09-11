import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/ha"))
rehearsal = importlib.import_module("blue_branch_rehearsal")


@pytest.fixture
def replica(tmp_path, monkeypatch):
    state = SimpleNamespace(running=True, readonly=True, lag=0, commands=[], stop_error=False, channel=rehearsal.CHANNEL,
                            cfg={"node": "laptop", "auto_failover": False})

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql):
            self.sql = sql
            state.commands.append(sql)
            if sql == rehearsal.STOP_REPLICA:
                state.running = False
                if state.stop_error:
                    raise OSError("Synthetic lost STOP reply")
            elif sql == rehearsal.START_REPLICA:
                state.running = True
            self.description = [(name,) for name in ("Source_UUID", "Channel_Name", "Replica_IO_Running",
                "Replica_SQL_Running", "Seconds_Behind_Source", "Last_IO_Errno", "Last_SQL_Errno")]

        def fetchone(self):
            if self.sql.startswith("SELECT @@server_uuid"):
                return (rehearsal.snapshots.UUIDS["laptop"], int(state.readonly), int(state.readonly))
            return (100,)

        def fetchall(self):
            return [(rehearsal.snapshots.UUIDS["main"], state.channel, "Yes" if state.running else "No",
                "Yes" if state.running else "No", state.lag, 0, 0)]

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(rehearsal.blue_node, "config", lambda: state.cfg)
    monkeypatch.setattr(rehearsal.blue_node, "connection", Connection)
    monkeypatch.setattr(rehearsal.snapshots, "archive_path", lambda path: path)
    monkeypatch.setattr(rehearsal.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3))
    monkeypatch.setattr(rehearsal.journal, "boundary", lambda _: {"logs": []})
    monkeypatch.setattr(rehearsal, "protect_directory", lambda _: None)
    monkeypatch.setattr(rehearsal.snapshots, "capture", lambda *args, **kwargs: {"snapshot_seal": "a" * 64})
    monkeypatch.setattr(rehearsal.journal, "capture", lambda *args: {"journal_seal": "b" * 64})
    monkeypatch.setattr(rehearsal.journal, "verify_journal", lambda _: ({"identity": {"snapshot_seal": "a" * 64}}, "b" * 64))
    return state, tmp_path / "rehearsal"


def test_success_restores_original_replica_and_preserves_receipt(replica):
    state, directory = replica
    result = rehearsal.run(directory, epoch=1)
    assert result["replica_restored"] and state.running
    assert result["promoted"] is False and result["seller_http_calls"] == 0
    assert [sql for sql in state.commands if not sql.startswith(("SELECT", "SHOW"))] == [rehearsal.STOP_REPLICA, rehearsal.START_REPLICA]
    assert json.loads((directory / "rehearsal.json").read_text())["phase"] == "evidence-verified"


@pytest.mark.parametrize("phase", ["stop-response", "snapshot", "journal", "verification"])
def test_every_capture_failure_restores_replica(replica, monkeypatch, phase):
    state, directory = replica

    def fail(*args, **kwargs):
        raise OSError("Synthetic evidence failure")

    if phase == "stop-response":
        state.stop_error = True
    elif phase == "snapshot":
        monkeypatch.setattr(rehearsal.snapshots, "capture", fail)
    elif phase == "journal":
        monkeypatch.setattr(rehearsal.journal, "capture", fail)
    else:
        monkeypatch.setattr(rehearsal.journal, "verify_journal", fail)
    with pytest.raises(OSError):
        rehearsal.run(directory, epoch=1)
    assert state.running and state.commands.count(rehearsal.START_REPLICA) == 1
    report = json.loads((directory / "rehearsal.json").read_text())
    assert report["phase"] == "capture-failed" and report["replica_restored"]


def test_role_change_refuses_restoration_instead_of_overwriting_new_primary(replica, monkeypatch):
    state, directory = replica

    def promote_elsewhere(*args, **kwargs):
        state.readonly = False
        raise OSError("Synthetic concurrent role change")

    monkeypatch.setattr(rehearsal.snapshots, "capture", promote_elsewhere)
    with pytest.raises(RuntimeError, match="read-only-laptop"):
        rehearsal.run(directory, epoch=1)
    assert rehearsal.START_REPLICA not in state.commands
    report = json.loads((directory / "rehearsal.json").read_text())
    assert report["replica_restored"] is False and report["restore_error_type"] == "RuntimeError"


@pytest.mark.parametrize("kind", ["wrong-node", "lagging", "automatic-mode", "wrong-channel"])
def test_invalid_start_state_never_stops_replication(replica, kind):
    state, directory = replica
    if kind == "wrong-node":
        state.cfg["node"] = "main"
    elif kind == "lagging":
        state.lag = 5
    elif kind == "automatic-mode":
        state.cfg["auto_failover"] = True
    else:
        state.channel = "another_channel"
    with pytest.raises(RuntimeError):
        rehearsal.run(directory, epoch=1)
    assert rehearsal.STOP_REPLICA not in state.commands and not directory.exists()
