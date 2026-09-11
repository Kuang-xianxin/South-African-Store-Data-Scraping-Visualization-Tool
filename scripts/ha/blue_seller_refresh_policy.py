"""Keep BLUE store refreshes on the main node, with bounded idle failover."""
from __future__ import annotations


POLICY_VERSION = "main-first-v1"
RESERVATION_SECONDS = 120


def verified_refresh(sample):
    workflow = sample.get("workflows", {}).get("refresh", {})
    return sample.get("routing_version") == 2 and type(workflow.get("busy")) is bool


def observe_refresh(ownership, node, sample):
    """Persist idle evidence only after all previously dispatched starts expire."""
    row = ownership.owners.get("refresh")
    if not row or row["node"] != node or not verified_refresh(sample):
        return
    if sample["workflows"]["refresh"]["busy"]:
        settled = False
    elif ownership.wall() >= row.get("until", 0):
        settled = True
    else:
        return
    if row.get("settled") is not settled:
        row["settled"] = settled
        ownership.save()


def choose_refresh(ownership, new, live, samples, *, main_unavailable):
    """Never transfer an active or unacknowledged refresh to a different exit."""
    eligible = {node for node, sample in live.items() if verified_refresh(sample)}
    row = ownership.owners.get("refresh")
    busy = [node for node, sample in samples.items()
            if verified_refresh(sample) and sample["workflows"]["refresh"]["busy"]]
    if len(busy) > 1:
        return None
    if busy:
        node = busy[0]
        if row and row["node"] != node and not row.get("settled", False):
            return None
    elif row and not row.get("settled", False):
        # A timed-out ingress request is not proof that its child process stopped.
        # Only a later authenticated idle inventory can release its ownership.
        node = row["node"]
    else:
        if not row and (set(samples) != {"main", "laptop"}
                        or not all(verified_refresh(s) for s in samples.values())):
            return None
        if "main" in eligible:
            node = "main"
        elif main_unavailable and "laptop" in eligible:
            node = "laptop"
        else:
            return None
    if node not in eligible:
        return None
    if new:
        # Renew for EVERY forwarded start, including repeated requests that the
        # node may reject. This also covers delayed connections to that node.
        ownership.owners["refresh"] = {
            "node": node, "until": ownership.wall() + RESERVATION_SECONDS,
            "settled": False,
        }
        ownership.save()
    elif not row or row["node"] != node:
        ownership.owners["refresh"] = {
            "node": node, "until": 0, "settled": not bool(busy),
        }
        ownership.save()
    return node
