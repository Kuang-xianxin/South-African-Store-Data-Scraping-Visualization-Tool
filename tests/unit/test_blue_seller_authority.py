from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "blue_seller_authority", Path(__file__).resolve().parents[2] / "scripts/ha/blue_seller_authority.py")
assert SPEC and SPEC.loader
a = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(a)

RUN, OTHER, REQUEST, TOKEN = (str(n) * 32 for n in range(1, 5))


def step(state, action, *, node="main", now=1, boot="boot", reports=None, **fields):
    return a.transition(state, node, {"cluster": a.CLUSTER, "action": "seller." + action, **fields},
                        now=now, boot=boot, reports=reports or {
                            "main": {"seen": now, "boot": boot},
                            "laptop": {"seen": now, "boot": boot}})


def running():
    state, reply = step(a.initial_state(), "begin_run", run_id=RUN)
    assert reply["status"] == "run-granted"
    return state


def pending():
    state, reply = step(running(), "begin_request", run_id=RUN, generation=1,
                        request_id=REQUEST, token=TOKEN)
    assert reply["request_permitted"]
    return state


def finish(state, **options):
    return step(state, "complete", run_id=RUN, generation=1,
                request_id=REQUEST, token=TOKEN, http_status=200, **options)


def test_main_preferred_and_absence_must_have_been_observed():
    for reports, reason in ((None, "main-preferred"),
                            ({"laptop": {"seen": 1, "boot": "boot"}}, "main-never-observed")):
        state, reply = step(a.initial_state(), "begin_run", node="laptop", run_id=RUN, reports=reports)
        assert reply["reason"] == reason and state["run"] is None


def test_laptop_takes_over_only_between_requests_and_old_run_is_fenced():
    state, reply = step(running(), "begin_run", node="laptop", now=50, run_id=OTHER,
                        reports={"main": {"seen": 1, "boot": "boot"},
                                 "laptop": {"seen": 50, "boot": "boot"}})
    assert reply["status"] == "run-granted" and state["generation"] == 2
    for action in ("renew", "begin_request", "end_run"):
        _, denied = step(state, action, now=51, run_id=RUN, generation=1,
                         request_id=REQUEST, token=TOKEN)
        assert denied["reason"] == "run-owner-or-generation-mismatch"


@pytest.mark.parametrize("now,boot", [(9999, "boot"), (1, "reboot"), (-100, "boot")])
def test_pending_request_never_expires_even_if_entire_old_machine_disappears(now, boot):
    state = pending()
    original = copy.deepcopy(state)
    updated, reply = step(state, "begin_run", now=now, boot=boot, node="laptop", run_id=OTHER)
    assert reply["reason"] == "previous-request-unconfirmed"
    assert updated == state == original


def test_lost_begin_reply_cannot_cause_second_send():
    state, reply = step(pending(), "begin_request", run_id=RUN, generation=1,
                        request_id=REQUEST, token=TOKEN)
    assert not reply["request_permitted"] and state["request"]


def test_response_completion_after_cloud_reboot_releases_only_the_old_request():
    state, reply = finish(pending(), boot="new", now=300)
    assert reply["status"] == "completed" and state["request"] is None
    _, denied = step(state, "begin_request", boot="new", now=301, run_id=RUN,
                     generation=1, request_id=OTHER, token=TOKEN)
    assert denied["reason"] == "run-expired-or-arbiter-restarted"


def test_lost_complete_reply_can_be_retried_without_repeating_http():
    state, _ = finish(pending())
    updated, reply = finish(state)
    assert reply["status"] == "already-completed" and updated == state
    _, denied = step(state, "begin_request", run_id=RUN, generation=1,
                     request_id=REQUEST, token=TOKEN)
    assert denied["reason"] == "request-id-already-used"


def test_uncertain_transport_failure_blocks_all_future_sends_and_run_close():
    state, reply = step(pending(), "uncertain", run_id=RUN, generation=1,
                        request_id=REQUEST, token=TOKEN)
    assert reply["status"] == "uncertain-retained"
    for action, fields in (("end_run", {}), ("begin_request", {"request_id": OTHER, "token": TOKEN})):
        _, denied = step(state, action, run_id=RUN, generation=1, **fields)
        assert denied["reason"] == "previous-request-unconfirmed"


@pytest.mark.parametrize("changes", [{"node": "laptop"}, {"generation": 2},
                                     {"token": "f" * 32}, {"http_status": None},
                                     {"http_status": True}, {"request_id": OTHER}])
def test_peer_stale_token_or_missing_response_cannot_release_request(changes):
    args = {"run_id": RUN, "generation": 1, "token": TOKEN,
            "request_id": REQUEST, "http_status": 200, **changes}
    state, reply = step(pending(), "complete", **args)
    assert reply["status"] == "denied" and state["request"]


def test_next_idle_run_returns_to_main_but_recovery_cannot_steal_busy_laptop():
    reports = {"main": {"seen": 1, "boot": "boot"}, "laptop": {"seen": 50, "boot": "boot"}}
    state, _ = step(a.initial_state(), "begin_run", node="laptop", now=50, reports=reports, run_id=RUN)
    _, denied = step(state, "begin_run", now=51, run_id=OTHER)
    assert denied["reason"] == "another-run-active"
    state, _ = step(state, "end_run", node="laptop", now=51, run_id=RUN, generation=1)
    state, reply = step(state, "begin_run", now=52, run_id=OTHER)
    assert reply["owner"] == "main" and reply["status"] == "run-granted"


def test_expired_run_identity_cannot_be_reused_as_new_run():
    _, reply = step(running(), "begin_run", now=500, run_id=RUN)
    assert reply["reason"] == "run-id-already-used"


def test_no_api_key_url_or_payload_persisted_or_echoed():
    state, reply = step(running(), "begin_request", run_id=RUN, generation=1,
                        request_id=REQUEST, token=TOKEN, api_key="secret-value", url="private-url")
    assert "secret-value" not in repr((state, reply))
    assert "private-url" not in repr((state, reply))
    assert TOKEN not in repr((state, reply))


@pytest.mark.parametrize("state", [{}, {"cluster": a.CLUSTER, "version": 1},
                                   {**a.initial_state(), "generation": True}])
def test_corrupt_or_missing_ledger_never_bootstraps(state):
    with pytest.raises(ValueError, match="refuse-reset"):
        step(state, "status")


def test_run_renewal_cannot_extend_an_expired_lease():
    _, reply = step(running(), "renew", now=31, run_id=RUN, generation=1)
    assert reply["status"] == "denied"


def test_invalid_request_is_atomic():
    state = running()
    original = copy.deepcopy(state)
    with pytest.raises(ValueError):
        step(state, "begin_request", run_id=RUN, generation=1, request_id="invalid", token=TOKEN)
    assert state == original
