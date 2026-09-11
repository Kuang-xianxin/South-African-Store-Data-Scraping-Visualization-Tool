"""Blue-only SSH arbitration ledger. Deployed in observe mode, never promotes MySQL.

The ledger is sticky: disconnects, clock changes and host reboots do NOT release
an owner. A controlled handover needs an authenticated, current-epoch fence
receipt and an exact GTID boundary. This is not an independent power fence.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid


CLUSTER = "takealot-blue-3307-v1"
BASE = Path("/var/lib/takealot-blue-arbiter")
CONFIG = Path("/etc/takealot-blue-arbiter.json")
MAX_MESSAGE = 65536
MAX_AGE = 20
NODES = {"main": ("DESKTOP-NTRMANG", 101), "laptop": ("LAPTOP-2T5MN8EU", 102)}
REPORT_FIELDS = ("computer", "server_id", "port", "datadir", "server_uuid", "seed_sha256",
                 "gtid_mode", "gtid_executed", "read_only", "super_read_only",
                 "active_transactions", "replica_io", "replica_sql", "replica_lag",
                 "replica_io_errno", "replica_sql_errno")


def initial_state() -> dict:
    return {"cluster": CLUSTER, "version": 1, "epoch": 0, "owner": None,
            "boundary": None, "reports": {}}


def validate_report(node: str, report: dict, cfg: dict) -> None:
    computer, server_id = NODES[node]
    expected = cfg["nodes"][node]
    identity = (report.get("computer"), report.get("server_id"), report.get("port"),
                str(report.get("datadir", "")).replace("\\", "/").rstrip("/").lower())
    if identity != (computer, server_id, 3307, "d:/takealotblue/mysql/data"):
        raise ValueError("blue-identity-mismatch")
    if report.get("server_uuid") != expected["server_uuid"]:
        raise ValueError("blue-uuid-mismatch")
    if report.get("seed_sha256") != cfg["seed_sha256"] or report.get("gtid_mode") != "ON":
        raise ValueError("blue-seed-or-gtid-mismatch")
    for name in ("read_only", "super_read_only"):
        if type(report.get(name)) is not int or report[name] not in (0, 1):
            raise ValueError("invalid-readonly-state")
    if type(report.get("active_transactions")) is not int or report["active_transactions"] < 0:
        raise ValueError("invalid-transaction-count")
    if not isinstance(report.get("gtid_executed"), str):
        raise ValueError("invalid-gtid-set")
    # MySQL 8.0 GTIDs; tagged 8.4 GTIDs are intentionally not accepted.
    if not re.fullmatch(r"[0-9a-fA-F:,\-\s]*", report["gtid_executed"]):
        raise ValueError("invalid-gtid-set")


def fenced(report: dict) -> bool:
    return (report.get("read_only") == 1 and report.get("super_read_only") == 1
            and report.get("active_transactions") == 0)


def recent(entry: dict | None, now: float, boot: str) -> bool:
    return bool(entry and entry["boot"] == boot and 0 <= now - entry["seen"] <= MAX_AGE)


def transition(state: dict, cfg: dict, node: str, session: str, request: dict,
               now: float, boot: str) -> tuple[dict, dict]:
    """Pure state transition, called under a Linux flock; no SQL or service actions."""
    if state.get("cluster") != CLUSTER or state.get("version") != 1:
        raise ValueError("ledger-invalid-refuse-reset")
    if cfg.get("cluster") != CLUSTER or cfg.get("mode") not in {"observe", "controlled"}:
        raise ValueError("configuration-invalid")
    if node not in NODES or request.get("cluster") != CLUSTER:
        raise ValueError("request-identity-invalid")
    result = copy.deepcopy(state)
    action = request.get("action")
    reply = {"cluster": CLUSTER, "mode": cfg["mode"], "epoch": state["epoch"],
             "writer_permitted": False}
    if action == "report":
        report = request["report"]
        validate_report(node, report, cfg)
        # Only whitelist operational facts. Never retain arbitrary keys or credentials.
        facts = {key: report[key] for key in REPORT_FIELDS if key in report}
        result["reports"][node] = {"facts": facts, "boot": boot, "seen": now}
        reply["status"] = "observed"
    elif action == "status":
        reply["status"] = "ok"
    elif action in {"acquire", "renew", "fenced"}:
        if cfg["mode"] != "controlled":
            reply.update(status="denied", reason="observe-only-no-write-permission")
        else:
            if action == "fenced":
                receipt = request.get("report")
                if not isinstance(receipt, dict):
                    reply.update(status="denied", reason="fence-needs-new-receipt")
                    return result, reply
                validate_report(node, receipt, cfg)
                # A heartbeat cached before promotion cannot stand in for a fresh fence.
                facts = {key: receipt[key] for key in REPORT_FIELDS if key in receipt}
                result["reports"][node] = {"facts": facts, "boot": boot, "seen": now}
            _ownership_transition(result, reply, node, session, request, now, boot)
    else:
        raise ValueError("unsupported-action")
    owner = result["owner"]
    reply["owner"] = None if owner is None else {"node": owner["node"], "epoch": owner["epoch"]}
    reply["nodes"] = {
        key: {"fresh": recent(entry, now, boot), "facts": entry["facts"]}
        for key, entry in result["reports"].items()
    }
    reply["epoch"] = result["epoch"]
    return result, reply


def _ownership_transition(state: dict, reply: dict, node: str, session: str,
                          request: dict, now: float, boot: str) -> None:
    """Controlled protocol only; enabling requires a separately reviewed node writer guard."""
    owner = state["owner"]
    action = request["action"]
    entry = state["reports"].get(node)
    if action == "acquire":
        if owner is not None:
            reply.update(status="denied", reason="previous-owner-not-fenced")
            return
        if not recent(entry, now, boot) or not fenced(entry["facts"]):
            reply.update(status="denied", reason="candidate-not-fenced")
            return
        if state["boundary"] is None:
            entries = [state["reports"].get(key) for key in NODES]
            if not all(recent(item, now, boot) and fenced(item["facts"]) for item in entries):
                reply.update(status="denied", reason="bootstrap-needs-both-fenced")
                return
            boundary = entries[0]["facts"]["gtid_executed"]
            if any(item["facts"]["gtid_executed"] != boundary for item in entries):
                reply.update(status="denied", reason="bootstrap-boundary-mismatch")
                return
            state["boundary"] = boundary
        # Exact equality deliberately rejects both missing and errant transactions.
        # False negatives for differently formatted sets are safe; normalize only after review.
        if entry["facts"]["gtid_executed"] != state["boundary"]:
            reply.update(status="denied", reason="replica-not-at-fenced-boundary")
            return
        state["epoch"] += 1
        state["owner"] = {"node": node, "session": session, "epoch": state["epoch"],
                          "boot": boot, "deadline": now + MAX_AGE}
        reply.update(status="granted", writer_permitted=True)
    elif action == "renew":
        if (not owner or owner["node"] != node or owner["session"] != session
                or owner["epoch"] != request.get("epoch") or owner["boot"] != boot
                or now >= owner["deadline"]):
            reply.update(status="denied", reason="lease-lost-must-fence")
            return
        owner["deadline"] = now + MAX_AGE
        reply.update(status="renewed", writer_permitted=True)
    else:
        # An authenticated old owner may reconnect to report a completed fence;
        # a peer, a stale epoch or a mere timeout may never do so on its behalf.
        if not owner or owner["node"] != node or request.get("epoch") != owner["epoch"]:
            reply.update(status="denied", reason="fence-owner-or-epoch-mismatch")
            return
        if not recent(entry, now, boot) or not fenced(entry["facts"]):
            reply.update(status="denied", reason="fence-not-confirmed")
            return
        if request.get("gtid_executed") != entry["facts"]["gtid_executed"]:
            reply.update(status="denied", reason="fence-boundary-mismatch")
            return
        state["boundary"] = entry["facts"]["gtid_executed"]
        state["owner"] = None
        reply["status"] = "fenced"


def save_state(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(state, stream, separators=(",", ":"), sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def validate_scope(request: dict, *, seller_only: bool) -> None:
    if seller_only and not str(request.get("action", "")).startswith("seller."):
        raise ValueError("seller-key-cannot-access-database-arbitration")


def serve(node: str, *, seller_only: bool = False) -> None:
    import fcntl
    import signal

    if node not in NODES:
        raise ValueError("invalid-forced-node")
    os.umask(0o077)
    session = uuid.uuid4().hex
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    print(json.dumps({"cluster": CLUSTER, "status": "hello", "protocol": 1,
                      "writer_permitted": False}), flush=True)
    while True:
        signal.alarm(60)  # No idle SSH child can live forever.
        line = sys.stdin.readline(MAX_MESSAGE + 1)
        signal.alarm(0)
        if not line:
            return  # Intentionally DO NOT clear owner on EOF / process death.
        if len(line) > MAX_MESSAGE or not line.endswith("\n"):
            raise ValueError("message-too-large-or-incomplete")
        request = json.loads(line)
        validate_scope(request, seller_only=seller_only)
        cfg = json.loads(CONFIG.read_text())
        # The controlled transition model is tested below the transport layer,
        # but MUST NOT be activated until an independent node writer/fence guard
        # has been implemented and fault-tested. A config edit cannot bypass it.
        if cfg.get("mode") != "observe":
            raise ValueError("writer-guard-not-installed")
        with (BASE / "state/ledger.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = BASE / "state/ledger.json"
            # Missing/corrupt state is an error, never an implicit new election.
            state = json.loads(path.read_text())
            if str(request.get("action", "")).startswith("seller."):
                # Seller authority is independent of MySQL's observe-only ledger.
                # Provisioning must explicitly attest coverage of same-account
                # GREEN and BLUE consumers before any platform call is admitted.
                from blue_seller_ledger import execute

                if cfg.get("seller_api") != {"version": 1, "enabled": True,
                                             "same_account_coverage_verified": True}:
                    reply = {"cluster": CLUSTER, "protocol": "seller-v1", "status": "denied",
                             "reason": "seller-coverage-not-enabled", "request_permitted": False}
                else:
                    reply = execute(BASE / "state/seller-api.sqlite3", node, request,
                                    now=time.monotonic(), boot=boot, reports=state["reports"])
            else:
                updated, reply = transition(state, cfg, node, session, request, time.monotonic(), boot)
                if updated != state:
                    save_state(path, updated)
        print(json.dumps(reply, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    try:
        if len(sys.argv) not in {2, 3} or (len(sys.argv) == 3 and sys.argv[2] != "--seller-only"):
            raise ValueError("invalid-forced-command")
        serve(sys.argv[1], seller_only=len(sys.argv) == 3)
    except Exception as exc:
        # Never echo a request, environment, SSH command or unexpected exception text.
        print(json.dumps({"status": "error", "writer_permitted": False,
                          "error_type": type(exc).__name__}), flush=True)
        sys.exit(1)
