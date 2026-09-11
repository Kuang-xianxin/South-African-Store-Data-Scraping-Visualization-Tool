from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from takealot_ops.ha.witness import (
    WitnessBusyError,
    WitnessLeaseConfig,
    WitnessLeaseLostError,
    WitnessProtocolError,
    acquire_witness_lease,
    build_ssh_command,
    parse_witness_message,
)


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, payload: bytes) -> None:
        if self.closed:
            raise BrokenPipeError
        self.writes.append(payload)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _FakeProcess:
    def __init__(self, *stdout_lines: bytes) -> None:
        self.stdin = _FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        for line in stdout_lines:
            self.stdout.feed_data(line)
        self.stderr.feed_eof()
        self.returncode: int | None = None

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _config(tmp_path: Path) -> WitnessLeaseConfig:
    identity = tmp_path / "node-key"
    identity.write_text("fixture key path only", encoding="utf-8")
    return WitnessLeaseConfig(
        host="119.91.117.232",
        node_id="desktop-ntrmang",
        identity_file=identity,
    )


def _message(status: str, **values: object) -> bytes:
    import json

    return (json.dumps({"protocol": 1, "status": status, **values}) + "\n").encode()


def test_build_ssh_command_is_noninteractive_and_checks_host_key(tmp_path: Path) -> None:
    identity = tmp_path / "node-key"
    identity.write_text("fixture key path only", encoding="utf-8")
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("fixture host key path only", encoding="utf-8")
    config = WitnessLeaseConfig(
        host="119.91.117.232",
        node_id="desktop-ntrmang",
        identity_file=identity,
        known_hosts_file=known_hosts,
    )

    command = build_ssh_command(config)

    rendered = " ".join(command)
    assert "BatchMode=yes" in rendered
    assert "StrictHostKeyChecking=yes" in rendered
    assert f"UserKnownHostsFile={known_hosts}" in rendered
    assert "ServerAliveCountMax=2" in rendered
    assert "takealot-witness@119.91.117.232" in command
    assert str(config.identity_file) in command


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json\n",
        b"[]\n",
        b'{"protocol":2,"status":"granted","node":"main","epoch":1}\n',
        b'{"protocol":1,"status":"granted","node":"main"}\n',
        b'{"protocol":1,"status":"busy"}\n',
    ],
)
def test_parse_witness_message_rejects_untrusted_payload(payload: bytes) -> None:
    with pytest.raises(WitnessProtocolError):
        parse_witness_message(payload)


async def test_acquired_lease_pings_only_matching_epoch(tmp_path: Path) -> None:
    process = _FakeProcess(
        _message("granted", node="desktop-ntrmang", epoch=7, lease_timeout_seconds=8),
        _message("alive", node="desktop-ntrmang", epoch=7, lease_timeout_seconds=8),
    )

    async def process_factory(*_args, **_kwargs):
        return process

    lease = await acquire_witness_lease(_config(tmp_path), process_factory=process_factory)
    heartbeat = await lease.ping()
    await lease.close()

    assert lease.epoch == 7
    assert heartbeat.status == "alive"
    assert process.stdin.writes == [b"PING\n"]
    assert process.stdin.closed


async def test_acquire_reports_current_leader_when_busy(tmp_path: Path) -> None:
    process = _FakeProcess(_message("busy", leader="laptop-2t5mn8eu", epoch=4))

    async def process_factory(*_args, **_kwargs):
        return process

    with pytest.raises(WitnessBusyError, match="laptop-2t5mn8eu"):
        await acquire_witness_lease(_config(tmp_path), process_factory=process_factory)
    assert process.stdin.closed


async def test_heartbeat_fails_closed_on_epoch_change(tmp_path: Path) -> None:
    process = _FakeProcess(
        _message("granted", node="desktop-ntrmang", epoch=7),
        _message("alive", node="desktop-ntrmang", epoch=8),
    )

    async def process_factory(*_args, **_kwargs):
        return process

    lease = await acquire_witness_lease(_config(tmp_path), process_factory=process_factory)
    with pytest.raises(WitnessLeaseLostError, match="did not confirm"):
        await lease.ping()
    await lease.close()
