"""Capture a real read-only laptop baseline, restoring the original replica state.

This is an explicit operator rehearsal, not an arbiter promotion command. Never
change read_only, credentials, application routes, Seller gates or business rows.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import blue_node
import blue_branch_capture as snapshots
import blue_branch_journal as journal
from blue_branch_merge import canonical
from blue_seller_runtime import current_sid


CHANNEL = "blue_test_main"
STOP_REPLICA = f"STOP REPLICA FOR CHANNEL '{CHANNEL}'"
START_REPLICA = f"START REPLICA FOR CHANNEL '{CHANNEL}'"


def replica_status(conn):
    with conn.cursor() as cursor:
        cursor.execute("SELECT @@server_uuid,@@global.read_only,@@global.super_read_only")
        identity = cursor.fetchone()
        cursor.execute("SHOW REPLICA STATUS")
        names = [column[0] for column in cursor.description]
        rows = cursor.fetchall()
    if len(rows) != 1 or identity != (snapshots.UUIDS["laptop"], 1, 1):
        raise RuntimeError("rehearsal-requires-read-only-laptop-replica")
    replica = dict(zip(names, rows[0], strict=True))
    if replica.get("Source_UUID") != snapshots.UUIDS["main"] or replica.get("Channel_Name") != CHANNEL:
        raise RuntimeError("replica-source-or-channel-changed")
    return {name: replica.get(name) for name in ("Replica_IO_Running", "Replica_SQL_Running",
        "Seconds_Behind_Source", "Last_IO_Errno", "Last_SQL_Errno")}


def healthy(status):
    return status == {"Replica_IO_Running": "Yes", "Replica_SQL_Running": "Yes",
        "Seconds_Behind_Source": 0, "Last_IO_Errno": 0, "Last_SQL_Errno": 0}


def protect_directory(path):
    # Directory is empty when ACL inheritance is removed. No credential or row
    # evidence is created until all explicit grants have succeeded.
    sid = current_sid()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run(["icacls.exe", str(path), "/inheritance:r", "/grant:r",
        "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", f"*{sid}:(OI)(CI)F"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, timeout=15)
    if result.returncode:
        raise RuntimeError("recovery-directory-acl-failed")


def save(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def restore_replica(*, timeout=60):
    cfg = blue_node.config()
    if cfg["node"] != "laptop" or cfg["auto_failover"]:
        raise RuntimeError("restore-refused-after-role-config-change")
    with closing(blue_node.connection()) as conn:
        replica_status(conn)  # Never start replication on a promoted/wrong server.
        with conn.cursor() as cursor:
            cursor.execute(START_REPLICA)
        deadline = time.monotonic() + timeout
        while True:
            status = replica_status(conn)
            if healthy(status):
                return status
            if status["Last_IO_Errno"] or status["Last_SQL_Errno"] or time.monotonic() >= deadline:
                raise RuntimeError("replica-restoration-not-healthy")
            time.sleep(1)


def run(directory: Path, *, epoch: int):
    cfg = blue_node.config()
    if cfg["node"] != "laptop" or cfg["auto_failover"] or type(epoch) is not int or epoch <= 0:
        raise RuntimeError("non-promoting-laptop-rehearsal-required")
    snapshot_path = snapshots.archive_path(directory / "baseline.sqlite3")
    directory = snapshot_path.parent
    if directory.exists():
        raise FileExistsError(directory)
    with closing(blue_node.connection()) as conn:
        initial = replica_status(conn)
        if not healthy(initial):
            raise RuntimeError("healthy-replica-required-before-rehearsal")
        logs = journal.boundary(conn)
        with conn.cursor() as cursor:
            cursor.execute("SELECT COALESCE(SUM(DATA_LENGTH+INDEX_LENGTH),0) FROM information_schema.TABLES WHERE TABLE_SCHEMA='takealot_ops'")
            data_size = int(cursor.fetchone()[0])
        required = 4 * data_size + sum(row["size"] for row in logs["logs"]) + 2 * 1024**3
        if shutil.disk_usage(snapshots.ROOT).free < required:
            raise RuntimeError("insufficient-space-for-real-recovery-evidence")
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.mkdir()
        protect_directory(directory)
        receipt_path = directory / "rehearsal.json"
        receipt = {"kind": "blue-branch-capture-rehearsal", "node": "laptop", "epoch": epoch,
            "automatic_failover": False, "promoted": False, "seller_http_calls": 0,
            "replica_restored": False, "initial_replica": initial, "phase": "prepared"}
        save(receipt_path, receipt)
        try:
            # Set the recovery intent BEFORE sending STOP, whose response may be
            # lost. finally restores the original running replica in either case.
            receipt["phase"] = "stop-requested"
            save(receipt_path, receipt)
            with conn.cursor() as cursor:
                cursor.execute(STOP_REPLICA)
            print(canonical({"phase": "replica-stopped"}), flush=True)
            receipt["snapshot"] = snapshots.capture(snapshot_path, epoch=epoch)
            receipt["phase"] = "snapshot-sealed"
            save(receipt_path, receipt)
            print(canonical({"phase": "snapshot-sealed", "snapshot": receipt["snapshot"]}), flush=True)
            receipt["journal"] = journal.capture(snapshot_path, directory / "journal")
            verified, seal = journal.verify_journal(directory / "journal")
            if seal != receipt["journal"]["journal_seal"] or verified["identity"]["snapshot_seal"] != receipt["snapshot"]["snapshot_seal"]:
                raise RuntimeError("rehearsal-evidence-binding-failed")
            receipt["phase"] = "evidence-verified"
            save(receipt_path, receipt)
        except BaseException as exc:
            receipt.update(phase="capture-failed", error_type=type(exc).__name__)
            raise
        finally:
            try:
                receipt["final_replica"] = restore_replica()
                receipt["replica_restored"] = True
            except BaseException as exc:
                receipt["restore_error_type"] = type(exc).__name__
                raise
            finally:
                save(receipt_path, receipt)
                print(canonical({"phase": receipt["phase"], "replica_restored": receipt["replica_restored"]}), flush=True)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--epoch", type=int, required=True)
    args = parser.parse_args()
    try:
        print(canonical(run(args.output_directory, epoch=args.epoch)))
    except Exception as exc:
        print(json.dumps({"status": "rejected", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
