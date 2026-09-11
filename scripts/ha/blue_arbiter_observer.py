"""Windows blue-only observer: report database facts, never grant/enable writes."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import blue_node


ROOT = Path("D:/TakealotBlue")
CLUSTER = "takealot-blue-3307-v1"


def database_facts() -> dict:
    cfg = blue_node.config()
    # connection() checks hostname, server_id, port AND datadir before returning.
    with blue_node.connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT @@hostname,@@server_id,@@port,@@datadir,@@server_uuid,"
                       "@@gtid_mode,@@gtid_executed,@@read_only,@@super_read_only")
        names = ("computer", "server_id", "port", "datadir", "server_uuid", "gtid_mode",
                 "gtid_executed", "read_only", "super_read_only")
        facts = dict(zip(names, cursor.fetchone(), strict=True))
        cursor.execute("SELECT seed_sha256 FROM takealot_ops.blue_environment_marker WHERE id=1")
        facts["seed_sha256"] = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM information_schema.innodb_trx")
        facts["active_transactions"] = cursor.fetchone()[0]
        cursor.execute("SHOW REPLICA STATUS")
        row = cursor.fetchone()
        replication = dict(zip([item[0] for item in cursor.description], row, strict=True)) if row else {}
        for destination, source in (
            ("replica_io", "Replica_IO_Running"), ("replica_sql", "Replica_SQL_Running"),
            ("replica_lag", "Seconds_Behind_Source"),
            ("replica_io_errno", "Last_IO_Errno"), ("replica_sql_errno", "Last_SQL_Errno"),
        ):
            facts[destination] = replication.get(source)
    if cfg["auto_failover"] or cfg["formal_crawler_enabled"]:
        raise RuntimeError("Observer is for the non-promoting stage only")
    return facts


def observer_config() -> dict:
    local = blue_node.config()
    cfg = json.loads((ROOT / "arbiter.json").read_text(encoding="utf-8-sig"))
    if (cfg.get("cluster") != CLUSTER or cfg.get("node") != local["node"]
            or cfg.get("mode") != "observe"
            or cfg.get("host") not in {"119.91.117.232", "100.72.100.10"}):
        raise RuntimeError("Unexpected arbiter endpoint or node")
    for name in ("arbiter_ed25519", "arbiter_known_hosts"):
        if not (ROOT / "secrets" / name).is_file():
            raise RuntimeError("Observer SSH prerequisite missing")
    return cfg


def ssh_command(cfg: dict) -> list[str]:
    return ["ssh.exe", "-F", "NUL", "-T", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
            "-o", "LogLevel=ERROR", "-o", "IdentitiesOnly=yes",
            "-o", "HostKeyAlias=119.91.117.232",
            "-o", f"UserKnownHostsFile={ROOT / 'secrets/arbiter_known_hosts'}",
            "-i", str(ROOT / "secrets/arbiter_ed25519"),
            f"takealot-blue-arbiter@{cfg['host']}"]


def write_status(payload: dict) -> None:
    path = ROOT / "state/arbiter-observer.json"
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"observed_at": datetime.now(timezone.utc).isoformat(),
                                     **payload}, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


async def send(process, request: dict) -> dict:
    process.stdin.write((json.dumps(request, separators=(",", ":")) + "\n").encode())
    await asyncio.wait_for(process.stdin.drain(), timeout=10)
    line = await asyncio.wait_for(process.stdout.readline(), timeout=15)
    result = json.loads(line)
    if (result.get("cluster") != CLUSTER or result.get("mode") != "observe"
            or result.get("writer_permitted") is not False
            or result.get("status") != "observed"):
        raise RuntimeError("Unexpected arbiter response; no write permission accepted")
    return result


async def observe(once: bool = False) -> None:
    cfg = observer_config()
    while True:
        process = None
        phase = "connect"
        try:
            process = await asyncio.create_subprocess_exec(
                *ssh_command(cfg), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, creationflags=0x08000000, limit=131072)
            write_status({"state": "connecting", "node": cfg["node"], "writer_permitted": False})
            greeting = json.loads(await asyncio.wait_for(process.stdout.readline(), timeout=25))
            if (greeting.get("cluster") != CLUSTER or greeting.get("status") != "hello"
                    or greeting.get("protocol") != 1 or greeting.get("writer_permitted") is not False):
                raise RuntimeError("Unexpected blue arbiter handshake")
            while True:
                phase = "database-facts"
                facts = await asyncio.to_thread(database_facts)
                phase = "report"
                response = await send(process, {"cluster": CLUSTER, "action": "report",
                                                 "report": facts})
                status = {"state": "observing", "node": cfg["node"], "arbiter": response}
                write_status(status)
                if once:
                    print(json.dumps(status))
                    return
                await asyncio.sleep(5)
        except Exception as exc:
            ssh_error = ""
            if process is not None:
                try:
                    raw = await asyncio.wait_for(process.stderr.read(4096), timeout=1)
                    # Fixed, credential-free argv with LogLevel=ERROR. Store categories only.
                    lower = raw.decode(errors="replace").lower()
                    for pattern, category in (
                        ("permissions", "key-permissions"), ("bad owner", "key-permissions"),
                        ("permission denied", "authentication"), ("host key", "host-key"),
                        ("no such file", "missing-file"), ("not accessible", "missing-file"),
                        ("connection", "transport"), ("timed out", "transport-timeout"),
                    ):
                        if pattern in lower:
                            ssh_error = category
                            break
                    if raw and not ssh_error:
                        ssh_error = "other-ssh-error"
                except TimeoutError:
                    ssh_error = "ssh-still-running"
            write_status({"state": "disconnected", "node": cfg["node"],
                          "writer_permitted": False, "phase": phase,
                          "error_type": type(exc).__name__, "ssh_error": ssh_error})
            if once:
                raise RuntimeError("Blue observer connection failed; inspect scoped status") from None
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
        await asyncio.sleep(10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("facts", "once", "run"))
    args = parser.parse_args()
    if args.action == "facts":
        print(json.dumps(database_facts()))
        return
    if args.action == "once":
        asyncio.run(observe(once=True))
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex = kernel.CreateMutexW(None, False, "Global\\TakealotBlueArbiterObserver")
    if not mutex:
        raise ctypes.WinError()
    try:
        if ctypes.get_last_error() == 183:
            return
        asyncio.run(observe())
    finally:
        kernel.CloseHandle(mutex)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}), file=sys.stderr)
        sys.exit(1)
