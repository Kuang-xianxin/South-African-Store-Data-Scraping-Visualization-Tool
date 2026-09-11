from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts/ha"
SPEC = importlib.util.spec_from_file_location("blue_arbiter", SCRIPTS / "blue_arbiter.py")
assert SPEC and SPEC.loader
arbiter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(arbiter)


@pytest.fixture
def cfg():
    return {"cluster": arbiter.CLUSTER, "mode": "controlled", "seed_sha256": "a" * 64,
            "nodes": {"main": {"server_uuid": "11111111-1111-1111-1111-111111111111"},
                      "laptop": {"server_uuid": "22222222-2222-2222-2222-222222222222"}}}


def facts(cfg, node, gtid=""):
    computer, server_id = arbiter.NODES[node]
    return {"computer": computer, "server_id": server_id, "port": 3307,
            "datadir": "D:\\TakealotBlue\\mysql\\data\\", "gtid_mode": "ON",
            "server_uuid": cfg["nodes"][node]["server_uuid"], "seed_sha256": cfg["seed_sha256"],
            "read_only": 1, "super_read_only": 1, "gtid_executed": gtid, "active_transactions": 0}


def step(state, cfg, node="main", action="status", now=1, boot="boot1", session="s1", **fields):
    return arbiter.transition(state, cfg, node, session,
                              {"cluster": arbiter.CLUSTER, "action": action, **fields}, now, boot)


def ready(cfg):
    state = arbiter.initial_state()
    for node in arbiter.NODES:
        state, _ = step(state, cfg, node, "report", report=facts(cfg, node))
    return state


def test_deployed_observe_mode_never_grants_or_releases(cfg):
    state = ready(cfg)
    cfg["mode"] = "observe"
    for action in ("acquire", "renew", "fenced"):
        updated, reply = step(state, cfg, action=action, epoch=0, gtid_executed="")
        assert updated == state
        assert reply["status"] == "denied"
        assert not reply["writer_permitted"]


@pytest.mark.parametrize("change", [
    {"port": 3306}, {"server_id": 1}, {"computer": "OTHER"},
    {"datadir": "D:/TakealotMySQLReplica/data"}, {"server_uuid": "unapproved"},
    {"seed_sha256": "b" * 64}, {"gtid_mode": "OFF"}, {"read_only": "1"},
    {"active_transactions": -1}, {"gtid_executed": "not-a-gtid"},
])
def test_wrong_identity_or_malformed_report_is_rejected_without_state_change(cfg, change):
    state = ready(cfg)
    original = copy.deepcopy(state)
    report = {**facts(cfg, "main"), **change}
    with pytest.raises(ValueError):
        step(state, cfg, action="report", report=report)
    assert state == original


def test_report_drops_unknown_fields_and_does_not_echo_credentials(cfg):
    report = {**facts(cfg, "main"), "password": "must-not-persist"}
    state, reply = step(arbiter.initial_state(), cfg, action="report", report=report)
    assert "must-not-persist" not in repr((state, reply))


def test_two_contenders_cannot_both_acquire(cfg):
    state, first = step(ready(cfg), cfg, action="acquire")
    state, second = step(state, cfg, "laptop", "acquire")
    assert first["writer_permitted"] and first["epoch"] == 1
    assert not second["writer_permitted"]
    assert state["owner"]["node"] == "main"


@pytest.mark.parametrize("now,boot", [(10000, "boot1"), (2, "new-cloud-boot"), (-50, "boot1")])
def test_timeout_reboot_or_clock_rollback_never_releases_previous_owner(cfg, now, boot):
    state, _ = step(ready(cfg), cfg, action="acquire")
    state, reply = step(state, cfg, "laptop", "acquire", now=now, boot=boot)
    assert reply["reason"] == "previous-owner-not-fenced"
    assert state["owner"]["node"] == "main"


def test_missing_or_corrupt_ledger_is_not_an_empty_election(cfg):
    with pytest.raises(ValueError, match="refuse-reset"):
        step({}, cfg)


def test_expired_or_different_session_cannot_renew(cfg):
    state, _ = step(ready(cfg), cfg, action="acquire")
    for options in ({"now": 25}, {"session": "reconnected"}, {"boot": "boot2"}):
        _, reply = step(state, cfg, action="renew", epoch=1, **options)
        assert not reply["writer_permitted"]
        assert reply["reason"] == "lease-lost-must-fence"


def test_peer_or_old_epoch_cannot_acknowledge_owner_fence(cfg):
    state, _ = step(ready(cfg), cfg, action="acquire")
    for node, epoch in (("laptop", 1), ("main", 0)):
        updated, reply = step(state, cfg, node, "fenced", epoch=epoch, gtid_executed="",
                              report=facts(cfg, node))
        assert reply["status"] == "denied" and updated["owner"] == state["owner"]


def test_active_transaction_blocks_fence_and_fresh_gtid_boundary_is_required(cfg):
    state, _ = step(ready(cfg), cfg, action="acquire")
    report = {**facts(cfg, "main"), "active_transactions": 1}
    state, _ = step(state, cfg, action="report", report=report)
    _, reply = step(state, cfg, action="fenced", epoch=1, gtid_executed="", report=report)
    assert reply["reason"] == "fence-not-confirmed"


def test_controlled_handover_requires_exact_fenced_gtid_on_candidate(cfg):
    state, _ = step(ready(cfg), cfg, action="acquire")
    boundary = "11111111-1111-1111-1111-111111111111:1-3"
    state, _ = step(state, cfg, action="report", report=facts(cfg, "main", boundary))
    state, receipt = step(state, cfg, action="fenced", epoch=1, gtid_executed=boundary,
                          report=facts(cfg, "main", boundary))
    assert receipt["status"] == "fenced" and state["owner"] is None
    _, denied = step(state, cfg, "laptop", "acquire")
    assert denied["reason"] == "replica-not-at-fenced-boundary"
    state, _ = step(state, cfg, "laptop", "report", report=facts(cfg, "laptop", boundary))
    state, granted = step(state, cfg, "laptop", "acquire")
    assert granted["epoch"] == 2 and state["owner"]["node"] == "laptop"


def test_fence_cannot_reuse_readonly_heartbeat_from_before_promotion(cfg):
    state, _ = step(ready(cfg), cfg, action="acquire")
    updated, reply = step(state, cfg, action="fenced", epoch=1, gtid_executed="")
    assert reply["reason"] == "fence-needs-new-receipt"
    assert updated["owner"] == state["owner"]


def test_stale_or_previous_boot_observations_cannot_bootstrap(cfg):
    for options in ({"now": 25}, {"boot": "new"}):
        _, reply = step(ready(cfg), cfg, action="acquire", **options)
        assert reply["reason"] == "candidate-not-fenced"


def test_transport_and_windows_installer_cannot_enable_writes():
    server = (SCRIPTS / "blue_arbiter.py").read_text()
    observer = (SCRIPTS / "blue_arbiter_observer.py").read_text()
    installer = (SCRIPTS / "install_blue_arbiter_observer.ps1").read_text()
    assert 'raise ValueError("writer-guard-not-installed")' in server
    assert '"action": "report"' in observer
    assert "SET GLOBAL" not in observer and "ALTER USER" not in observer
    assert "StrictHostKeyChecking=yes" in observer and "IdentitiesOnly=yes" in observer
    assert "-AtStartup" in installer and "-LogonType ServiceAccount" in installer
    assert "Stop-Service" not in installer and "Restart-Service" not in installer
    assert "3306" not in installer and "8501" not in installer
