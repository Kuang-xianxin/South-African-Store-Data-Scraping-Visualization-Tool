"""Long-running witness guard for one manually promoted Windows HA node."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from takealot_ops.ha.witness import WitnessLeaseConfig, acquire_witness_lease


@dataclass(frozen=True, slots=True)
class GuardConfig:
    node_id: str
    witness_host: str
    identity_file: Path
    known_hosts_file: Path
    status_file: Path
    fail_close_script: Path
    stop_file: Path | None = None
    witness_port: int = 22


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _write_status(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _fail_close(config: GuardConfig, reason: str) -> int:
    completed = subprocess.run(
        (
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(config.fail_close_script),
            "-Reason",
            reason,
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return completed.returncode


async def run_guard(config: GuardConfig) -> int:
    lease = None
    exit_reason = "witness guard stopped"
    try:
        lease = await acquire_witness_lease(
            WitnessLeaseConfig(
                host=config.witness_host,
                port=config.witness_port,
                node_id=config.node_id,
                identity_file=config.identity_file,
                known_hosts_file=config.known_hosts_file,
                connect_timeout_seconds=20.0,
            )
        )
        _write_status(
            config.status_file,
            {
                "state": "holding",
                "node": config.node_id,
                "epoch": lease.epoch,
                "heartbeat_at": _utc_iso(),
            },
        )
        while True:
            if config.stop_file is not None and config.stop_file.is_file():
                exit_reason = "witness guard stop requested"
                return 0
            await asyncio.sleep(lease.config.heartbeat_interval_seconds)
            heartbeat = await lease.ping()
            _write_status(
                config.status_file,
                {
                    "state": "holding",
                    "node": config.node_id,
                    "epoch": heartbeat.epoch,
                    "heartbeat_at": _utc_iso(),
                    "server_unix_ms": heartbeat.server_unix_ms,
                },
            )
    except asyncio.CancelledError:
        exit_reason = "witness guard cancelled"
        raise
    except BaseException as exc:
        exit_reason = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        if lease is not None:
            await lease.close()
        fail_close_code = await asyncio.to_thread(_fail_close, config, exit_reason)
        _write_status(
            config.status_file,
            {
                "state": "released",
                "node": config.node_id,
                "heartbeat_at": _utc_iso(),
                "reason": exit_reason,
                "fail_close_exit_code": fail_close_code,
            },
        )
        if config.stop_file is not None:
            config.stop_file.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--witness-host", required=True)
    parser.add_argument("--witness-port", type=int, default=22)
    parser.add_argument("--identity-file", type=Path, required=True)
    parser.add_argument("--known-hosts-file", type=Path, required=True)
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--fail-close-script", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = GuardConfig(
        node_id=args.node_id,
        witness_host=args.witness_host,
        witness_port=args.witness_port,
        identity_file=args.identity_file,
        known_hosts_file=args.known_hosts_file,
        status_file=args.status_file,
        fail_close_script=args.fail_close_script,
        stop_file=args.stop_file,
    )
    return asyncio.run(run_guard(config))


if __name__ == "__main__":
    sys.exit(main())
