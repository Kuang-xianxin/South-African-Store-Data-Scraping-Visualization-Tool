"""Read/recover exact completed Seller receipts; never replay HTTP or clear uncertainty."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from blue_seller_authority import CLUSTER
from blue_seller_runtime import Journal, SSH, seller_ssh_command


ROOT = Path("D:/TakealotBlue")


def inspect(*, recover=False):
    import blue_node
    from blue_arbiter_observer import observer_config

    rpc = SSH(seller_ssh_command(observer_config()))
    try:
        recovered = 0
        path = ROOT / "state/seller-api/receipts.sqlite3"
        if recover and path.exists():
            journal = Journal(path, blue_node.protect_secret, lambda blob: blue_node.protect_secret(blob, decrypt=True))
            for entry in journal.completed():
                reply = rpc({"cluster": CLUSTER, "action": "seller.complete", **entry})
                if (reply.get("cluster") != CLUSTER or reply.get("protocol") != "seller-v1"
                        or reply.get("status") not in {"completed", "already-completed"}):
                    raise RuntimeError("completed-receipt-recovery-not-acknowledged")
                journal.ack(entry["request_id"])
                recovered += 1
        result = rpc({"cluster": CLUSTER, "action": "seller.status"})
        allowed = ("status", "reason", "owner", "generation", "run_live", "request_id",
                   "request_state", "blocked_by_unconfirmed_request")
        return {key: result[key] for key in allowed if key in result} | {"recovered_receipts": recovered, "http_calls": 0}
    finally:
        rpc.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recover-completed", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(inspect(recover=args.recover_completed)))
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "http_calls": 0}))
        raise SystemExit(1) from None
