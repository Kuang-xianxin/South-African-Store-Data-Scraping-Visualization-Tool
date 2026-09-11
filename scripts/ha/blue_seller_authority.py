"""Durable Seller API admission, separate from MySQL leadership.

A run lease may expire BETWEEN requests. An admitted request never expires:
an unconfirmed HTTP attempt fences every other caller until it is resolved.
This distinction also protects against a client pausing after admission.
Only an authenticated transport may supply ``node`` and the node reports.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import math
import re


CLUSTER = "takealot-blue-3307-v1"
VERSION = 1
RUN_TTL = 30
REPORT_TTL = 20
NODES = frozenset({"main", "laptop"})
ID = re.compile(r"[0-9a-f]{32}\Z")


def initial_state() -> dict:
    return {"cluster": CLUSTER, "version": VERSION, "generation": 0,
            "run": None, "request": None, "used_runs": {}, "completed": {}}


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("invalid-identifier")
    return value


def _digest(token: str) -> str:
    _identifier(token)
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _active(run: dict | None, now: float, boot: str) -> bool:
    return bool(run and run["boot"] == boot and run["started"] <= now < run["deadline"])


def _fresh(reports: dict, node: str, now: float, boot: str) -> bool:
    item = reports.get(node)
    return bool(item and item.get("boot") == boot and
                0 <= now - item.get("seen", -math.inf) <= REPORT_TTL)


def validate_state(state: dict) -> None:
    if (state.get("cluster") != CLUSTER or state.get("version") != VERSION or
            type(state.get("generation")) is not int or state["generation"] < 0 or
            not isinstance(state.get("used_runs"), dict) or
            not isinstance(state.get("completed"), dict) or
            "run" not in state or "request" not in state):
        raise ValueError("seller-ledger-invalid-refuse-reset")
    run, pending = state["run"], state["request"]
    if run is not None:
        _identifier(run["id"])
        if run["node"] not in NODES or run["generation"] != state["generation"]:
            raise ValueError("seller-run-invalid")
        for key in ("deadline", "started"):
            if not isinstance(run[key], (float, int)) or not math.isfinite(run[key]):
                raise ValueError("seller-run-time-invalid")
        if state["used_runs"].get(run["id"]) != run["generation"]:
            raise ValueError("seller-run-history-invalid")
    if pending is not None:
        if (not run or pending["run_id"] != run["id"] or
                pending["node"] != run["node"] or pending["generation"] != run["generation"] or
                pending["status"] not in {"admitted", "uncertain"} or
                not re.fullmatch(r"[a-f0-9]{64}", pending["token_hash"])):
            raise ValueError("seller-request-invalid")
        _identifier(pending["id"])


def public_status(state: dict, now: float, boot: str) -> dict:
    run, pending = state["run"], state["request"]
    return {"version": VERSION, "generation": state["generation"],
            "owner": run["node"] if run else None,
            "run_id": run["id"] if run else None,
            "run_live": _active(run, now, boot),
            "request_id": pending["id"] if pending else None,
            "request_state": pending["status"] if pending else None,
            "blocked_by_unconfirmed_request": pending is not None}


def transition(state: dict, node: str, message: dict, *, now: float,
               boot: str, reports: dict) -> tuple[dict, dict]:
    """Pure transition. Persist the returned state BEFORE replying or executing HTTP."""
    validate_state(state)
    if node not in NODES or not math.isfinite(now) or not boot:
        raise ValueError("unverified-seller-caller")
    if message.get("cluster") != CLUSTER:
        raise ValueError("wrong-seller-cluster")
    updated = copy.deepcopy(state)
    action = message.get("action")
    response = {"cluster": CLUSTER, "protocol": "seller-v1", "status": "denied",
                "reason": "unsupported-action", "request_permitted": False}

    def deny(reason: str):
        response.update(status="denied", reason=reason)

    if action == "seller.status":
        response.update(status="ok", reason=None)
    elif action == "seller.begin_run":
        run_id = _identifier(message.get("run_id"))
        if run_id in state["used_runs"]:
            deny("run-id-already-used")
        elif state["request"]:
            deny("previous-request-unconfirmed")
        elif _active(state["run"], now, boot):
            deny("another-run-active")
        elif not _fresh(reports, node, now, boot):
            deny("caller-health-unverified")
        elif node == "laptop" and _fresh(reports, "main", now, boot):
            deny("main-preferred")
        elif node == "laptop" and "main" not in reports:
            deny("main-never-observed")
        else:
            updated["generation"] += 1
            updated["run"] = {"id": run_id, "node": node,
                              "generation": updated["generation"], "boot": boot,
                              "started": now, "deadline": now + RUN_TTL}
            updated["used_runs"][run_id] = updated["generation"]
            response.update(status="run-granted", reason=None, ttl=RUN_TTL)
    elif action in {"seller.renew", "seller.end_run", "seller.begin_request",
                    "seller.complete", "seller.uncertain"}:
        run = state["run"]
        run_id = _identifier(message.get("run_id"))
        generation = message.get("generation")
        receipt = state["completed"].get(message.get("request_id"))
        if (action == "seller.complete" and receipt and receipt["node"] == node and
                receipt["run_id"] == run_id and receipt["generation"] == generation and
                hmac.compare_digest(receipt["token_hash"], _digest(message.get("token")))):
            # A lost completion acknowledgement remains recoverable after the run
            # closes or a different node becomes owner. This grants no new HTTP.
            response.update(status="already-completed", reason=None)
            response.update(public_status(updated, now, boot))
            return updated, response
        # Completion may arrive after lease expiry/cloud reboot. It releases ONLY
        # that exact old in-flight operation, never grants a new operation.
        if (not run or run["id"] != run_id or run["node"] != node or
                type(generation) is not int or run["generation"] != generation):
            deny("run-owner-or-generation-mismatch")
        elif action in {"seller.complete", "seller.uncertain"}:
            request_id = _identifier(message.get("request_id"))
            token_hash = _digest(message.get("token"))
            pending = state["request"]
            receipt = state["completed"].get(request_id)
            if (receipt and receipt["run_id"] == run_id and receipt["node"] == node and
                    hmac.compare_digest(receipt["token_hash"], token_hash)):
                response.update(status="already-completed", reason=None)
            elif (not pending or pending["id"] != request_id or
                  not hmac.compare_digest(pending["token_hash"], token_hash)):
                deny("request-or-token-mismatch")
            elif action == "seller.uncertain":
                updated["request"]["status"] = "uncertain"
                response.update(status="uncertain-retained", reason=None)
            elif type(message.get("http_status")) is not int or not 100 <= message["http_status"] <= 599:
                deny("complete-http-response-required")
            else:
                # No payload, API key, URL, response headers or error body in this ledger.
                updated["completed"][request_id] = {
                    "run_id": run_id, "node": node, "token_hash": token_hash,
                    "http_status": message["http_status"], "generation": generation}
                updated["request"] = None
                response.update(status="completed", reason=None)
        elif action == "seller.end_run":
            if state["request"]:
                deny("previous-request-unconfirmed")
            else:
                updated["run"] = None
                response.update(status="run-ended", reason=None)
        elif not _active(run, now, boot):
            deny("run-expired-or-arbiter-restarted")
        elif action == "seller.renew":
            updated["run"]["deadline"] = now + RUN_TTL
            response.update(status="renewed", reason=None, ttl=RUN_TTL)
        elif action == "seller.begin_request":
            request_id = _identifier(message.get("request_id"))
            token_hash = _digest(message.get("token"))
            if state["request"]:
                # Even replay of an identical begin must not authorize sending twice.
                deny("previous-request-unconfirmed")
            elif request_id in state["completed"]:
                deny("request-id-already-used")
            else:
                updated["request"] = {"id": request_id, "run_id": run_id,
                                      "node": node, "generation": generation,
                                      "token_hash": token_hash, "status": "admitted"}
                updated["run"]["deadline"] = now + RUN_TTL
                response.update(status="request-granted", reason=None, request_permitted=True)
    else:
        raise ValueError("unsupported-seller-action")
    validate_state(updated)
    response.update(public_status(updated, now, boot))
    return updated, response
