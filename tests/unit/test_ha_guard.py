from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from takealot_ops.ha import guard
from takealot_ops.ha.witness import WitnessLeaseLostError


class _FailingLease:
    def __init__(self) -> None:
        self.epoch = 14
        self.config = SimpleNamespace(heartbeat_interval_seconds=0.001)
        self.closed = False

    async def ping(self):
        raise WitnessLeaseLostError("fixture heartbeat loss")

    async def close(self) -> None:
        self.closed = True


async def test_guard_fails_closed_and_releases_status_on_heartbeat_loss(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lease = _FailingLease()
    fail_close_reasons: list[str] = []

    async def fake_acquire(_config):
        return lease

    def fake_fail_close(_config, reason: str) -> int:
        fail_close_reasons.append(reason)
        return 0

    monkeypatch.setattr(guard, "acquire_witness_lease", fake_acquire)
    monkeypatch.setattr(guard, "_fail_close", fake_fail_close)
    status_file = tmp_path / "guard-status.json"
    config = guard.GuardConfig(
        node_id="laptop-2t5mn8eu",
        witness_host="100.72.100.10",
        identity_file=tmp_path / "identity",
        known_hosts_file=tmp_path / "known_hosts",
        status_file=status_file,
        fail_close_script=tmp_path / "demote.ps1",
        stop_file=tmp_path / "guard.stop",
    )

    exit_code = await guard.run_guard(config)

    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert lease.closed is True
    assert fail_close_reasons == ["WitnessLeaseLostError: fixture heartbeat loss"]
    assert payload["state"] == "released"
    assert payload["fail_close_exit_code"] == 0
    assert "fixture heartbeat loss" in payload["reason"]


async def test_guard_stop_request_releases_lease_and_demotes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lease = _FailingLease()
    stop_file = tmp_path / "guard.stop"
    stop_file.write_text("stop\n", encoding="utf-8")
    fail_close_reasons: list[str] = []

    async def fake_acquire(_config):
        return lease

    def fake_fail_close(_config, reason: str) -> int:
        fail_close_reasons.append(reason)
        return 0

    monkeypatch.setattr(guard, "acquire_witness_lease", fake_acquire)
    monkeypatch.setattr(guard, "_fail_close", fake_fail_close)
    status_file = tmp_path / "guard-status.json"
    config = guard.GuardConfig(
        node_id="laptop-2t5mn8eu",
        witness_host="100.72.100.10",
        identity_file=tmp_path / "identity",
        known_hosts_file=tmp_path / "known_hosts",
        status_file=status_file,
        fail_close_script=tmp_path / "demote.ps1",
        stop_file=stop_file,
    )

    exit_code = await guard.run_guard(config)

    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert lease.closed is True
    assert fail_close_reasons == ["witness guard stop requested"]
    assert payload["state"] == "released"
    assert stop_file.exists() is False
