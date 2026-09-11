"""Fail-closed SSH lease client for the lightweight cloud witness.

The witness grants one long-running SSH session at a time.  The local process
must keep sending application-level ``PING`` messages; losing the session means
losing permission to act as the writable node.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence


PROTOCOL_VERSION = 1
_NODE_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MESSAGE_STATUSES = {"granted", "alive", "busy", "error"}


class WitnessProtocolError(RuntimeError):
    """Raised when the witness sends malformed or contradictory data."""


class WitnessLeaseLostError(RuntimeError):
    """Raised when a granted lease can no longer be proven alive."""


class WitnessBusyError(RuntimeError):
    """Raised when another node currently owns the witness lease."""

    def __init__(self, message: "WitnessMessage") -> None:
        self.message = message
        owner = message.leader or "another node"
        super().__init__(f"witness lease is held by {owner}")


@dataclass(frozen=True, slots=True)
class WitnessMessage:
    """One validated line from the witness protocol."""

    status: Literal["granted", "alive", "busy", "error"]
    node: str | None = None
    leader: str | None = None
    epoch: int | None = None
    server_unix_ms: int | None = None
    lease_timeout_seconds: int | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class WitnessLeaseConfig:
    """Connection details for one node-specific restricted SSH key."""

    host: str
    node_id: str
    identity_file: Path
    known_hosts_file: Path | None = None
    user: str = "takealot-witness"
    port: int = 22
    ssh_executable: str | Path = "ssh"
    connect_timeout_seconds: float = 8.0
    heartbeat_interval_seconds: float = 2.0
    heartbeat_timeout_seconds: float = 6.0

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("witness host cannot be empty")
        if not _NODE_PATTERN.fullmatch(self.node_id):
            raise ValueError("node_id must be a lowercase DNS-style label")
        if not self.user.strip():
            raise ValueError("witness user cannot be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("witness SSH port must be between 1 and 65535")
        if self.connect_timeout_seconds <= 0:
            raise ValueError("connect timeout must be positive")
        if self.heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        if self.heartbeat_timeout_seconds <= self.heartbeat_interval_seconds:
            raise ValueError("heartbeat timeout must exceed heartbeat interval")


def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise WitnessProtocolError(f"witness field {key!r} must be non-empty text")
    return value.strip()


def _optional_positive_int(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise WitnessProtocolError(f"witness field {key!r} must be a positive integer")
    return value


def parse_witness_message(raw_line: bytes | str) -> WitnessMessage:
    """Parse and validate a single newline-delimited JSON witness message."""

    try:
        decoded = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
    except UnicodeDecodeError as exc:
        raise WitnessProtocolError("witness response is not UTF-8") from exc
    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise WitnessProtocolError("witness response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise WitnessProtocolError("witness response must be a JSON object")
    if payload.get("protocol") != PROTOCOL_VERSION:
        raise WitnessProtocolError("unsupported witness protocol version")
    status = payload.get("status")
    if status not in _MESSAGE_STATUSES:
        raise WitnessProtocolError("unknown witness status")

    node = _optional_text(payload, "node")
    leader = _optional_text(payload, "leader")
    epoch = _optional_positive_int(payload, "epoch")
    server_unix_ms = _optional_positive_int(payload, "server_unix_ms")
    lease_timeout_seconds = _optional_positive_int(payload, "lease_timeout_seconds")
    detail = _optional_text(payload, "detail")

    if status in {"granted", "alive"} and (node is None or epoch is None):
        raise WitnessProtocolError(f"{status} response requires node and epoch")
    if status == "busy" and leader is None:
        raise WitnessProtocolError("busy response requires leader")
    return WitnessMessage(
        status=status,
        node=node,
        leader=leader,
        epoch=epoch,
        server_unix_ms=server_unix_ms,
        lease_timeout_seconds=lease_timeout_seconds,
        detail=detail,
    )


def build_ssh_command(config: WitnessLeaseConfig) -> tuple[str, ...]:
    """Build a non-interactive, host-key-verified SSH command."""

    connect_timeout = max(1, math.ceil(config.connect_timeout_seconds))
    server_alive_interval = max(1, math.floor(config.heartbeat_interval_seconds))
    known_hosts_options = (
        (
            "-o",
            f"UserKnownHostsFile={config.known_hosts_file}",
        )
        if config.known_hosts_file is not None
        else ()
    )
    return (
        str(config.ssh_executable),
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        f"ServerAliveInterval={server_alive_interval}",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "LogLevel=ERROR",
        *known_hosts_options,
        "-i",
        str(config.identity_file),
        "-p",
        str(config.port),
        f"{config.user}@{config.host}",
    )


class WitnessLease:
    """A granted lease whose validity must be checked continuously."""

    def __init__(
        self,
        *,
        config: WitnessLeaseConfig,
        process: asyncio.subprocess.Process,
        granted: WitnessMessage,
    ) -> None:
        if process.stdin is None or process.stdout is None:
            raise ValueError("witness process must expose stdin and stdout")
        self.config = config
        self.process = process
        self.granted = granted
        self.epoch = int(granted.epoch or 0)
        self._closed = False

    async def ping(self) -> WitnessMessage:
        """Renew the application-level lease once or fail closed."""

        if self._closed or self.process.returncode is not None:
            raise WitnessLeaseLostError("witness SSH process is not running")
        stdin = self.process.stdin
        stdout = self.process.stdout
        assert stdin is not None
        assert stdout is not None
        try:
            stdin.write(b"PING\n")
            await stdin.drain()
            raw_line = await asyncio.wait_for(
                stdout.readline(),
                timeout=self.config.heartbeat_timeout_seconds,
            )
        except (BrokenPipeError, ConnectionError, asyncio.TimeoutError) as exc:
            raise WitnessLeaseLostError("witness heartbeat failed") from exc
        if not raw_line:
            raise WitnessLeaseLostError("witness closed the SSH session")
        message = parse_witness_message(raw_line)
        if (
            message.status != "alive"
            or message.node != self.config.node_id
            or message.epoch != self.epoch
        ):
            raise WitnessLeaseLostError("witness heartbeat did not confirm this lease")
        return message

    async def maintain(
        self,
        on_heartbeat: Callable[[WitnessMessage], Awaitable[None] | None] | None = None,
    ) -> None:
        """Keep renewing until cancelled or until ownership can no longer be proven."""

        while not self._closed:
            await asyncio.sleep(self.config.heartbeat_interval_seconds)
            message = await self.ping()
            if on_heartbeat is not None:
                callback_result = on_heartbeat(message)
                if callback_result is not None:
                    await callback_result

    async def close(self) -> None:
        """Release the lease and reap the SSH child process."""

        if self._closed:
            return
        self._closed = True
        stdin = self.process.stdin
        if stdin is not None:
            stdin.close()
            wait_closed = getattr(stdin, "wait_closed", None)
            if wait_closed is not None:
                try:
                    await wait_closed()
                except (BrokenPipeError, ConnectionError):
                    pass
        try:
            await asyncio.wait_for(self.process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

    async def __aenter__(self) -> "WitnessLease":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()


async def _read_stderr(process: asyncio.subprocess.Process) -> str:
    if process.stderr is None:
        return ""
    try:
        payload = await asyncio.wait_for(process.stderr.read(4096), timeout=1.0)
    except asyncio.TimeoutError:
        return ""
    return payload.decode("utf-8", errors="replace").strip()


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.stdin is not None:
        process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        process.terminate()
        await process.wait()


async def acquire_witness_lease(
    config: WitnessLeaseConfig,
    *,
    process_factory: Callable[..., Awaitable[asyncio.subprocess.Process]] | None = None,
) -> WitnessLease:
    """Open the restricted SSH session and acquire the exclusive witness lease."""

    if not config.identity_file.is_file():
        raise FileNotFoundError(config.identity_file)
    factory = process_factory or asyncio.create_subprocess_exec
    process = await factory(
        *build_ssh_command(config),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if process.stdout is None:
        await _stop_process(process)
        raise WitnessProtocolError("witness SSH process has no stdout")
    try:
        raw_line = await asyncio.wait_for(
            process.stdout.readline(),
            timeout=config.connect_timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        await _stop_process(process)
        raise WitnessLeaseLostError("witness did not answer before connect timeout") from exc
    if not raw_line:
        detail = await _read_stderr(process)
        await _stop_process(process)
        suffix = f": {detail}" if detail else ""
        raise WitnessLeaseLostError(f"witness SSH session closed before grant{suffix}")
    try:
        message = parse_witness_message(raw_line)
    except WitnessProtocolError:
        await _stop_process(process)
        raise
    if message.status == "busy":
        await _stop_process(process)
        raise WitnessBusyError(message)
    if message.status != "granted":
        await _stop_process(process)
        raise WitnessProtocolError(message.detail or "witness denied the lease")
    if message.node != config.node_id:
        await _stop_process(process)
        raise WitnessProtocolError("witness granted the lease to a different node")
    return WitnessLease(config=config, process=process, granted=message)


def redact_ssh_command(command: Sequence[str]) -> str:
    """Render a diagnostic command without ever reading key contents."""

    return " ".join(command)
