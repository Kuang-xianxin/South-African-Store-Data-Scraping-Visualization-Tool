"""Seal original MySQL transactions for a frozen BLUE branch; never replay them.

Raw binlogs remain on their originating machine in the protected branch folder.
The GTID index records exact transaction byte boundaries, including multi-row and
multi-table changes. It is not a replacement for business conflict decisions.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
from itertools import zip_longest
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import uuid

from blue_branch_capture import ROOT, SEED, archive_path, facts
from blue_branch_merge import CLUSTER, canonical, exclusive_database, readonly, verify_snapshot


class JournalRejected(RuntimeError):
    pass


class Ranges:
    def __init__(self):
        self.values = {}

    def add(self, sid, number):
        intervals = self.values.setdefault(sid, [])
        start = end = number
        combined = []
        for low, high in intervals:
            if high < start - 1:
                combined.append((low, high))
            elif end < low - 1:
                combined.append((start, end))
                start, end = low, high
            else:
                start, end = min(start, low), max(end, high)
        combined.append((start, end))
        self.values[sid] = combined

    def text(self):
        return ",".join(sid + ":" + ":".join(str(low) if low == high else f"{low}-{high}"
            for low, high in intervals) for sid, intervals in sorted(self.values.items()))


def _packed_length(data, offset):
    if offset >= len(data):
        raise JournalRejected("missing-transaction-length")
    marker = data[offset]
    if marker < 251:
        return marker
    width = {252: 2, 253: 3, 254: 8}.get(marker)
    if width is None or offset + 1 + width > len(data):
        raise JournalRejected("invalid-transaction-length")
    return int.from_bytes(data[offset + 1:offset + 1 + width], "little")


def transaction_regions(path: Path):
    """Read MySQL 8.0 GTID headers only, never materialize row/SQL payloads.

    The official mysqlbinlog verifier separately checks event checksums. The GTID
    transaction_length prevents a checksum-valid prefix ending mid-transaction
    from being mistaken for complete evidence.
    """
    size = path.stat().st_size
    pending = None
    with path.open("rb") as stream:
        if stream.read(4) != b"\xfebin":
            raise JournalRejected("unsupported-or-encrypted-binlog")
        offset = 4
        while offset < size:
            header = stream.read(19)
            if len(header) != 19:
                raise JournalRejected("truncated-event-header")
            _, kind, _, length, _, _ = struct.unpack("<IBIIIH", header)
            if length < 23 or offset + length > size:
                raise JournalRejected("truncated-event-body")
            body = stream.read(min(length - 23, 128))  # Exclude CRC32; bounded metadata read.
            if offset == 4:
                if kind != 15 or len(body) < 57 or body[:2] != b"\x04\x00" or body[56] != 19:
                    raise JournalRejected("unsupported-binlog-format")
                stream.seek(offset + length - 5)
                if stream.read(1) != b"\x01":
                    raise JournalRejected("crc32-binlog-required")
            elif kind == 15:
                raise JournalRejected("unexpected-format-event")
            if kind == 34:
                raise JournalRejected("anonymous-transaction-not-supported")
            if kind == 33:
                if pending or len(body) < 50 or body[25] != 2:
                    raise JournalRejected("overlapping-or-unsupported-gtid")
                sid = str(uuid.UUID(bytes=body[1:17]))
                number = struct.unpack("<q", body[17:25])[0]
                if not 1 <= number < 2**63 - 1:
                    raise JournalRejected("invalid-gtid-sequence")
                commit_timestamp = int.from_bytes(body[42:49], "little")
                length_offset = 56 if commit_timestamp & (1 << 55) else 49
                transaction_length = _packed_length(body, length_offset)
                if transaction_length < length or offset + transaction_length > size:
                    raise JournalRejected("incomplete-transaction")
                pending = (sid, number, offset, transaction_length)
            offset += length
            stream.seek(offset)
            if pending:
                end = pending[2] + pending[3]
                if offset > end:
                    raise JournalRejected("transaction-boundary-cuts-event")
                if offset == end:
                    yield pending
                    pending = None
        if pending:
            raise JournalRejected("incomplete-transaction")


def verify_binlog(path: Path, executable: Path):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run([str(executable), "--no-defaults", "--verify-binlog-checksum",
        "--base64-output=DECODE-ROWS", str(path)], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, creationflags=flags)
    if result.returncode:
        raise JournalRejected("mysqlbinlog-validation-failed")


def prefix_digest(path: Path, size: int, destination: Path | None = None):
    digest = hashlib.sha256()
    output = None
    if destination is not None:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        output = os.fdopen(fd, "wb")
    try:
        with path.open("rb") as source:
            remaining = size
            while remaining:
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    raise JournalRejected("source-log-shorter-than-boundary")
                digest.update(block)
                if output:
                    output.write(block)
                remaining -= len(block)
        if output:
            output.flush()
            os.fsync(output.fileno())
        return digest.hexdigest()
    finally:
        if output:
            output.close()


def boundary(conn):
    with conn.cursor() as cursor:
        cursor.execute("SELECT @@global.log_bin,@@global.gtid_mode,@@global.enforce_gtid_consistency,"
            "@@global.binlog_format,@@global.binlog_row_image,@@global.binlog_checksum,"
            "@@global.log_replica_updates,@@global.sync_binlog,@@global.innodb_flush_log_at_trx_commit,"
            "@@global.binlog_expire_logs_seconds,@@global.log_bin_basename")
        keys = ("log_bin", "gtid_mode", "enforce_gtid_consistency", "binlog_format", "binlog_row_image",
                "binlog_checksum", "log_replica_updates", "sync_binlog", "flush_at_commit", "expire_seconds", "basename")
        config = dict(zip(keys, cursor.fetchone(), strict=True))
        cursor.execute("SHOW BINARY LOGS")
        logs = []
        for row in cursor.fetchall():
            encrypted = str(row[2]).casefold() if len(row) > 2 else "no"
            if encrypted not in {"no", "off", "0", "yes", "on", "1"}:
                raise JournalRejected("unknown-binlog-encryption-state")
            logs.append((row[0], row[1], encrypted in {"yes", "on", "1"}))
        cursor.execute("SHOW MASTER STATUS")
        row = cursor.fetchone()
        if not row:
            raise JournalRejected("binary-log-boundary-missing")
        filename, position, do_db, ignore_db, gtid = row
    if do_db or ignore_db:
        raise JournalRejected("filtered-binlog-not-complete-evidence")
    expected = {"log_bin": 1, "gtid_mode": "ON", "enforce_gtid_consistency": "ON", "binlog_format": "ROW",
        "binlog_row_image": "FULL", "binlog_checksum": "CRC32", "log_replica_updates": 1,
        "sync_binlog": 1, "flush_at_commit": 1}
    if any(config[key] != value for key, value in expected.items()):
        raise JournalRejected("full-durable-gtid-binlog-required")
    if not logs or any(encrypted for _, _, encrypted in logs) or filename not in [name for name, _, _ in logs]:
        raise JournalRejected("binlog-inventory-changed-or-encrypted")
    selected = []
    for name, length, _ in logs:
        if not re.fullmatch(r"blue-bin\.\d{6}", name) or length < 4:
            raise JournalRejected("unexpected-binlog-filename-or-size")
        selected.append({"name": name, "size": position if name == filename else length})
        if name == filename:
            break
    return {"config": config, "file": filename, "position": position, "gtid": gtid, "logs": selected}


def archive_files(directory: Path, files: list[tuple[Path, int]], executable: Path, identity: dict,
                  check_coverage, final_check):
    """An incomplete directory/index is deliberately retained without a seal."""
    if identity.get("cluster") != CLUSTER or not files:
        raise JournalRejected("journal-identity-or-files-missing")
    if directory.exists():
        raise FileExistsError(directory)
    required = sum(size for _, size in files)
    if shutil.disk_usage(directory.parent).free < required + 1024**3:
        raise JournalRejected("insufficient-journal-disk-space")
    directory.mkdir()
    ranges, objects, count = Ranges(), [], 0
    with closing(exclusive_database(directory / "transactions.sqlite3")) as db:
        db.executescript("""
            CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK(id=1),header TEXT,seal TEXT);
            INSERT INTO metadata VALUES (1,NULL,NULL);
            CREATE TABLE transactions (sid TEXT,gno INTEGER,file TEXT,offset INTEGER,length INTEGER,
                PRIMARY KEY(sid,gno)) WITHOUT ROWID;
        """)
        for source, size in files:
            if source.name in {entry["name"] for entry in objects}:
                raise JournalRejected("duplicate-journal-file")
            target = directory / source.name
            before = prefix_digest(source, size, target)
            verify_binlog(target, executable)
            for sid, number, offset, length in transaction_regions(target):
                db.execute("INSERT INTO transactions VALUES (?,?,?,?,?)", (sid, number, source.name, offset, length))
                ranges.add(sid, number)
                count += 1
                if count % 1000 == 0:
                    db.commit()  # No final seal yet.
            if prefix_digest(source, size) != before:
                raise JournalRejected("source-binlog-changed-during-copy")
            objects.append({"name": source.name, "size": size, "sha256": before})
        covered = ranges.text()
        if not check_coverage(covered):
            raise JournalRejected("journal-missing-required-gtids")
        final_check()
        header = {"version": 1, "kind": "blue-branch-transaction-journal", "identity": identity,
                  "files": objects, "gtids": covered, "transactions": count}
        seal = hashlib.sha256(canonical(header).encode())
        for row in db.execute("SELECT * FROM transactions ORDER BY sid,gno"):
            seal.update(canonical(row).encode() + b"\n")
        db.execute("UPDATE metadata SET header=?,seal=? WHERE id=1", (canonical(header), seal.hexdigest()))
        db.commit()
    return {"journal_seal": seal.hexdigest(), "transactions": count, "files": len(files),
            "mysql_writes": 0, "production_replay_available": False}


def verify_journal(directory: Path):
    with closing(readonly(directory / "transactions.sqlite3")) as db:
        db.execute("BEGIN")
        record = db.execute("SELECT header,seal FROM metadata WHERE id=1").fetchone()
        if not record or not record[0] or not record[1]:
            raise JournalRejected("journal-incomplete")
        header = json.loads(record[0])
        if (header.get("version") != 1 or header.get("kind") != "blue-branch-transaction-journal"
                or header.get("identity", {}).get("cluster") != CLUSTER):
            raise JournalRejected("journal-format-invalid")
        seen, ranges, count = set(), Ranges(), 0
        for entry in header["files"]:
            name, size = entry["name"], entry["size"]
            if not re.fullmatch(r"blue-bin\.\d{6}", name) or name in seen:
                raise JournalRejected("journal-file-inventory-invalid")
            seen.add(name)
            path = directory / name
            if (path.is_symlink() or getattr(path.stat(), "st_file_attributes", 0) & 0x400
                    or path.stat().st_size != size or prefix_digest(path, size) != entry["sha256"]):
                raise JournalRejected("journal-file-integrity-failed")
            actual = transaction_regions(path)
            indexed = db.execute("SELECT sid,gno,offset,length FROM transactions WHERE file=? ORDER BY offset", (name,))
            for region, stored in zip_longest(actual, indexed):
                if region != stored:
                    raise JournalRejected("journal-transaction-index-mismatch")
                ranges.add(region[0], region[1])
                count += 1
            if prefix_digest(path, size) != entry["sha256"]:
                raise JournalRejected("journal-file-changed-during-verification")
        if count != header["transactions"] or ranges.text() != header["gtids"]:
            raise JournalRejected("journal-gtid-index-mismatch")
        seal = hashlib.sha256(canonical(header).encode())
        indexed_count = 0
        for row in db.execute("SELECT * FROM transactions ORDER BY sid,gno"):
            indexed_count += 1
            seal.update(canonical(row).encode() + b"\n")
        if indexed_count != count or seal.hexdigest() != record[1]:
            raise JournalRejected("journal-seal-mismatch")
        return header, record[1]


def capture(snapshot_path: Path, directory: Path, baseline_path: Path | None = None):
    import blue_node

    snapshot, snapshot_seal = verify_snapshot(archive_path(snapshot_path))
    cfg = blue_node.config()
    if snapshot["identity"].get("node") != cfg["node"]:
        raise JournalRejected("snapshot-node-mismatch")
    if baseline_path:
        baseline, baseline_seal = verify_snapshot(archive_path(baseline_path))
        if (snapshot["identity"].get("role") != "branch" or baseline["identity"].get("role") != "baseline"
                or snapshot["identity"].get("baseline_seal") != baseline_seal
                or snapshot["identity"].get("epoch") != baseline["identity"].get("epoch")
                or baseline["identity"].get("cluster") != CLUSTER
                or baseline["identity"].get("seed_sha256") != SEED):
            raise JournalRejected("journal-baseline-mismatch")
    elif snapshot["identity"].get("role") == "baseline":
        baseline, baseline_seal = snapshot, snapshot_seal
    else:
        raise JournalRejected("branch-journal-requires-baseline")
    # Reuse the protected archive boundary and reparse-point checks for every
    # ancestor. The existing directory must never be reused or overwritten.
    index_path = archive_path(directory / "transactions.sqlite3", create_parent=False)
    directory = index_path.parent
    if not directory.parent.is_dir():
        raise JournalRejected("protected-journal-parent-missing")
    with closing(blue_node.connection()) as conn:
        before = facts(conn, cfg["node"])
        bound = boundary(conn)
        if (snapshot["identity"].get("cluster") != CLUSTER or snapshot["identity"].get("seed_sha256") != SEED
                or snapshot["identity"].get("server_uuid") != before["server_uuid"]
                or type(snapshot["identity"].get("epoch")) is not int or snapshot["identity"]["epoch"] <= 0):
            raise JournalRejected("journal-snapshot-identity-mismatch")
        if not before["capture_ready"] or before["gtid"] != snapshot["identity"]["gtid"] or bound["gtid"] != before["gtid"]:
            raise JournalRejected("branch-not-frozen-at-snapshot")
        log_root = ROOT / "mysql/logs"
        if Path(bound["config"]["basename"]).resolve() != (log_root / "blue-bin").resolve():
            raise JournalRejected("unexpected-blue-binlog-directory")
        files = []
        for item in bound["logs"]:
            source = log_root / item["name"]
            if source.is_symlink() or getattr(source.stat(), "st_file_attributes", 0) & 0x400:
                raise JournalRejected("linked-binlog-refused")
            files.append((source, item["size"]))

        def covers(gtids):
            with conn.cursor() as cursor:
                cursor.execute("SELECT GTID_SUBSET(%s,%s),GTID_SUBSET(GTID_SUBTRACT(%s,%s),%s)",
                    (baseline["identity"]["gtid"], bound["gtid"], bound["gtid"], baseline["identity"]["gtid"], gtids))
                return cursor.fetchone() == (1, 1)

        def unchanged():
            if facts(conn, cfg["node"]) != before or boundary(conn) != bound:
                raise JournalRejected("branch-changed-during-journal-capture")
            if verify_snapshot(snapshot_path)[1] != snapshot_seal or (baseline_path and verify_snapshot(baseline_path)[1] != baseline_seal):
                raise JournalRejected("snapshot-changed-during-journal-capture")

        identity = {"cluster": CLUSTER, "node": cfg["node"], "server_uuid": before["server_uuid"],
            "epoch": snapshot["identity"]["epoch"], "snapshot_seal": snapshot_seal, "baseline_seal": baseline_seal,
            "baseline_gtid": baseline["identity"]["gtid"], "gtid": bound["gtid"],
            "file": bound["file"], "position": bound["position"], "binlog_config": bound["config"]}
        return archive_files(directory, files, Path(cfg["mysql_bin"]) / "mysqlbinlog.exe", identity, covers, unchanged)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    capture_parser = commands.add_parser("capture")
    capture_parser.add_argument("--snapshot", type=Path, required=True)
    capture_parser.add_argument("--baseline", type=Path)
    capture_parser.add_argument("--output-directory", type=Path, required=True)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            import blue_node
            cfg = blue_node.config()
            with closing(blue_node.connection()) as conn:
                frozen, bound = facts(conn, cfg["node"]), boundary(conn)
            print(canonical({"node": cfg["node"], "frozen_for_snapshot": frozen["capture_ready"],
                "freeze_reasons": frozen["reasons"], "durable_full_row_binlog": True,
                "binlog_files": len(bound["logs"]), "binlog_bytes": sum(row["size"] for row in bound["logs"]),
                "retention_seconds": bound["config"]["expire_seconds"],
                "retention_pinned": bound["config"]["expire_seconds"] == 0,
                "free_bytes": shutil.disk_usage(ROOT).free, "mysql_writes": 0,
                "automatic_failover": cfg["auto_failover"]}))
        elif args.command == "verify":
            directory = archive_path(args.directory / "transactions.sqlite3").parent
            header, seal = verify_journal(directory)
            print(canonical({"journal_seal": seal, "transactions": header["transactions"], "mysql_writes": 0}))
        else:
            print(canonical(capture(args.snapshot, args.output_directory, args.baseline)))
    except Exception as exc:
        print(canonical({"status": "rejected", "error_type": type(exc).__name__, "mysql_writes": 0}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
