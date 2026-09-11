import importlib
from pathlib import Path
import sys

import httpx
import pytest

from takealot_ops.api.client import TakealotClient
from takealot_ops.settings import Settings

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/ha"))
runtime = importlib.import_module("blue_seller_runtime")
authority = importlib.import_module("blue_seller_authority")
ledger = importlib.import_module("blue_seller_ledger")


def guarded(tmp_path):
    path = tmp_path / "remote.sqlite3"
    ledger.initialize(path)
    calls = []
    def rpc(message):
        calls.append(message["action"])
        return ledger.execute(path, "main", message, now=1, boot="boot",
                              reports={"main": {"boot": "boot", "seen": 1}})
    journal = runtime.Journal(tmp_path / "local.sqlite3", lambda b: b[::-1], lambda b: b[::-1])
    class Client(TakealotClient):
        pass
    runtime.install(Client, run_factory=lambda: runtime.SellerRun(rpc, journal, heartbeat_seconds=None),
                    process_scope=False)
    return Client, calls, path


def settings(tmp_path):
    return Settings(project_root=tmp_path, api_key="fixture-not-a-real-key",
                    base_url="https://example.test", database_url="sqlite://",
                    request_timeout_seconds=1, dashboard_host="127.0.0.1", dashboard_port=0)


def test_real_client_pagination_and_429_retry_all_pass_authority(tmp_path):
    Client, calls, _ = guarded(tmp_path)
    attempts = []
    def upstream(request):
        attempts.append(str(request.url))
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        if len(attempts) == 2:
            return httpx.Response(200, json={"items": [{"id": "first"}], "continuation_token": "next"})
        return httpx.Response(200, json={"items": [{"id": "last"}]})
    instance = Client(settings(tmp_path), transport=httpx.MockTransport(upstream), sleep=lambda _: None)
    try:
        assert list(instance.iter_items("/offers", {})) == [{"id": "first"}, {"id": "last"}]
    finally:
        instance.close()
    assert len(attempts) == calls.count("seller.begin_request") == calls.count("seller.complete") == 3
    assert "continuation_token=next" in attempts[-1]
    assert calls[-1] == "seller.end_run"


def test_real_client_transport_retry_cannot_bypass_unconfirmed_outcome(tmp_path):
    Client, calls, path = guarded(tmp_path)
    attempts = []
    def upstream(request):
        attempts.append(1)
        raise httpx.ReadTimeout("network error", request=request)
    instance = Client(settings(tmp_path), transport=httpx.MockTransport(upstream), sleep=lambda _: None)
    with pytest.raises(runtime.AdmissionError, match="unconfirmed"):
        list(instance.iter_items("/offers", {}))
    instance.close()
    assert attempts == [1]
    status = ledger.execute(path, "main", {"cluster": authority.CLUSTER, "action": "seller.status"},
                            now=500, boot="reboot", reports={})
    assert status["blocked_by_unconfirmed_request"]


def test_local_journal_reopens_complete_proof_and_retains_acknowledged_records(tmp_path):
    path = tmp_path / "local.sqlite3"
    def protect(b):
        return b[::-1]
    journal = runtime.Journal(path, protect, protect)
    entry = {"run_id": "1" * 32, "generation": 1, "request_id": "2" * 32, "token": "3" * 32}
    journal.prepare(entry)
    assert not journal.completed()
    journal.finish(entry["request_id"], 200)
    reopened = runtime.Journal(path, protect, protect)
    assert reopened.completed() == [{**entry, "http_status": 200}]
    reopened.ack(entry["request_id"])
    assert not reopened.completed()
    with reopened._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1


def test_hook_does_not_affect_unpatched_client_class(tmp_path):
    original = TakealotClient._send_get
    Client, _, _ = guarded(tmp_path)
    assert Client._send_get is not original
    assert TakealotClient._send_get is original
