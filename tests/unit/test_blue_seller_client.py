import importlib
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/ha"))
authority = importlib.import_module("blue_seller_authority")
client = importlib.import_module("blue_seller_client")


class Journal:
    def __init__(self):
        self.entries = {}
    def prepare(self, entry):
        self.entries[entry["request_id"]] = dict(entry)
    def finish(self, request_id, http_status):
        self.entries[request_id]["http_status"] = http_status
    def ack(self, request_id):
        self.entries.pop(request_id)
    def completed(self):
        return [v for v in self.entries.values() if "http_status" in v]


class Rpc:
    def __init__(self):
        self.state = authority.initial_state()
        self.now = 1
        self.boot = "boot"
        self.lose = None
        self.calls = []
    def __call__(self, message):
        self.calls.append(message["action"])
        self.state, reply = authority.transition(self.state, "main", message, now=self.now,
                                                 boot=self.boot, reports={"main": {"boot": self.boot, "seen": self.now}})
        if message["action"] == self.lose:
            self.lose = None
            raise OSError("lost wire reply")
        return reply


def response(status=200):
    return httpx.Response(status, json={"items": []}, request=httpx.Request("GET", "https://example.test/offers"))


def test_every_page_and_retry_gets_its_own_admission_and_full_completion():
    rpc, journal = Rpc(), Journal()
    run = client.SellerRun(rpc, journal, heartbeat_seconds=None)
    sent = []
    for code in (429, 200, 200):
        def send():
            assert rpc.state["request"] is not None
            sent.append(code)
            return response(code)
        assert run.perform(send).status_code == code
        assert rpc.state["request"] is None
    assert sent == [429, 200, 200]
    assert rpc.calls.count("seller.begin_request") == 3
    assert rpc.calls.count("seller.complete") == 3
    assert not journal.entries
    run.close()


def test_lost_admission_reply_sends_no_http_and_preserves_server_fence():
    rpc, journal = Rpc(), Journal()
    rpc.lose = "seller.begin_request"
    run = client.SellerRun(rpc, journal, heartbeat_seconds=None)
    def must_not_send():
        pytest.fail("HTTP sent without confirmed admission")
    with pytest.raises(OSError):
        run.perform(must_not_send)
    assert rpc.state["request"]
    with pytest.raises(client.AdmissionError):
        run.perform(must_not_send)
    run.close()
    assert rpc.state["request"]


def test_client_pause_after_admission_does_not_allow_laptop_send():
    rpc, journal = Rpc(), Journal()
    run = client.SellerRun(rpc, journal, heartbeat_seconds=None)
    def paused_send():
        rpc.now = 500
        _, reply = authority.transition(rpc.state, "laptop", {
            "cluster": authority.CLUSTER, "action": "seller.begin_run", "run_id": "a" * 32},
            now=500, boot="boot", reports={"main": {"boot": "boot", "seen": 1},
                                          "laptop": {"boot": "boot", "seen": 500}})
        assert reply["reason"] == "previous-request-unconfirmed"
        return response()
    run.perform(paused_send)
    run.close()


def test_http_timeout_does_not_release_or_retry_on_another_node():
    rpc, journal = Rpc(), Journal()
    run = client.SellerRun(rpc, journal, heartbeat_seconds=None)
    sent = []
    def timeout():
        sent.append(1)
        raise httpx.ReadTimeout("sensitive-request-details")
    with pytest.raises(client.AdmissionError, match="unconfirmed") as error:
        run.perform(timeout)
    assert "sensitive" not in str(error.value)
    assert rpc.state["request"]["status"] == "uncertain"
    with pytest.raises(client.AdmissionError):
        run.perform(timeout)
    assert sent == [1]
    assert not journal.completed()
    run.close()


def test_complete_ack_loss_recovery_after_run_closed_never_repeats_http():
    rpc, journal = Rpc(), Journal()
    run = client.SellerRun(rpc, journal, heartbeat_seconds=None)
    rpc.lose = "seller.complete"
    with pytest.raises(client.AdmissionError, match="acknowledgement pending"):
        run.perform(response)
    assert len(journal.completed()) == 1
    run.close()
    assert rpc.state["run"] is None
    next_run = client.SellerRun(rpc, journal, heartbeat_seconds=None)
    next_run.start()
    assert not journal.entries
    assert rpc.calls.count("seller.begin_request") == 1
    next_run.close()


def test_missing_journal_durability_prevents_http_and_admission():
    rpc, journal = Rpc(), Journal()
    def fail(_):
        raise OSError("disk full")
    journal.prepare = fail
    run = client.SellerRun(rpc, journal, heartbeat_seconds=None)
    with pytest.raises(OSError):
        run.perform(lambda: pytest.fail("sent despite failed journal"))
    assert "seller.begin_request" not in rpc.calls
    run.close()


def test_denied_renewal_stops_next_request_without_erasing_old_operation():
    rpc, journal = Rpc(), Journal()
    run = client.SellerRun(rpc, journal, heartbeat_seconds=None)
    run.perform(response)
    rpc.now = 100
    with pytest.raises(client.AdmissionError):
        run.perform(lambda: pytest.fail("expired lease sent HTTP"))
    assert rpc.state["request"] is None
    run.close()
