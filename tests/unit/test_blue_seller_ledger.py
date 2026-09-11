from concurrent.futures import ThreadPoolExecutor
import importlib
import json
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/ha"))
ledger = importlib.import_module("blue_seller_ledger")
CLUSTER = ledger.initial_state()["cluster"]
RUN, REQUEST, TOKEN = (str(n) * 32 for n in (1, 2, 3))


def call(path, action, **fields):
    return ledger.execute(path, "main", {"cluster": CLUSTER, "action": "seller." + action, **fields},
                          now=1, boot="boot", reports={"main": {"boot": "boot", "seen": 1}})


def test_admission_survives_reopen_and_duplicate_cannot_send(tmp_path):
    path = tmp_path / "gate.sqlite3"
    ledger.initialize(path)
    call(path, "begin_run", run_id=RUN)
    first = call(path, "begin_request", run_id=RUN, generation=1, request_id=REQUEST, token=TOKEN)
    second = call(path, "begin_request", run_id=RUN, generation=1, request_id=REQUEST, token=TOKEN)
    assert first["request_permitted"] and not second["request_permitted"]
    assert second["blocked_by_unconfirmed_request"]


def test_concurrent_process_connections_serialize_admission(tmp_path):
    path = tmp_path / "gate.sqlite3"
    ledger.initialize(path)
    call(path, "begin_run", run_id=RUN)
    def attempt(n):
        return call(path, "begin_request", run_id=RUN, generation=1, request_id=f"{n:032x}", token=TOKEN)
    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(attempt, range(10, 26)))
    assert sum(r["request_permitted"] for r in replies) == 1


def test_missing_state_is_not_recreated(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        call(path, "status")
    assert not path.exists()


def test_initialization_refuses_to_overwrite_pending_request(tmp_path):
    path = tmp_path / "gate.sqlite3"
    ledger.initialize(path)
    call(path, "begin_run", run_id=RUN)
    call(path, "begin_request", run_id=RUN, generation=1, request_id=REQUEST, token=TOKEN)
    with pytest.raises(FileExistsError):
        ledger.initialize(path)
    assert call(path, "status")["request_id"] == REQUEST


def test_receipt_and_release_commit_together_and_history_cannot_be_reused(tmp_path):
    path = tmp_path / "gate.sqlite3"
    ledger.initialize(path)
    call(path, "begin_run", run_id=RUN)
    call(path, "begin_request", run_id=RUN, generation=1, request_id=REQUEST, token=TOKEN)
    call(path, "complete", run_id=RUN, generation=1, request_id=REQUEST, token=TOKEN, http_status=429)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
        assert json.loads(db.execute("SELECT payload FROM metadata").fetchone()[0])["request"] is None
    assert call(path, "begin_request", run_id=RUN, generation=1, request_id=REQUEST, token=TOKEN)["reason"] == "request-id-already-used"


def test_failed_commit_never_returns_permission(tmp_path, monkeypatch):
    path = tmp_path / "gate.sqlite3"
    ledger.initialize(path)
    call(path, "begin_run", run_id=RUN)
    connect = sqlite3.connect
    class BrokenCommit(sqlite3.Connection):
        def commit(self):
            raise OSError("disk failure")
    monkeypatch.setattr(ledger.sqlite3, "connect", lambda *a, **kw: connect(*a, factory=BrokenCommit, **kw))
    with pytest.raises(OSError):
        call(path, "begin_request", run_id=RUN, generation=1, request_id=REQUEST, token=TOKEN)
    monkeypatch.undo()
    assert call(path, "status")["request_id"] is None
