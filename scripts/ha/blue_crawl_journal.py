"""BLUE-only durable delivery journal, NOT an ERP database fallback.

The task roster and immutable result envelopes survive process restarts. SQLite
is used directly (not through the ERP engine); FULL commits precede upload/ACK.
No lease expiry discards an observation and no receipt is ever auto-deleted.
"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit
from uuid import uuid4


VERSION = 1
OFFLINE_TTL = 1800
MAX_PENDING_BYTES = 256 * 1024 * 1024
MODE = {"version": 1, "delivery": "at-least-once", "offline_seconds": 1800,
        "database": "fixed-primary-blue-101", "automatic_failover": False}


def enabled(root: Path) -> bool:
    marker = root / "state/durable-crawl.json"
    if not marker.exists():
        return False
    if json.loads(marker.read_text(encoding="utf-8")) != MODE:
        raise RuntimeError("Unapproved durable crawler mode")
    return True


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def task_spec(row: dict) -> dict:
    spec = {key: row[key] for key in
            ("job_id", "batch_id", "plid", "url", "with_stock_probe", "followers_only")}
    if (type(spec["job_id"]) is not int or spec["job_id"] < 1
            or not re.fullmatch(r"blue-(?:web|full)-[a-f0-9]{32}", spec["batch_id"])
            or not re.fullmatch(r"[0-9]{1,30}", spec["plid"])
            or type(spec["with_stock_probe"]) is not bool
            or type(spec["followers_only"]) is not bool):
        raise ValueError("Unsupported BLUE durable task")
    url = urlsplit(spec["url"])
    if (url.scheme != "https" or url.hostname not in {"www.takealot.com", "takealot.com"}
            or url.username or url.password or url.port not in {None, 443}
            or not url.path.endswith("/PLID" + spec["plid"])):
        raise ValueError("Task URL does not match its PLID")
    # Context may refresh between attempts. The six-field task identity below
    # stays immutable; an already saved attempt retains its original context.
    for key in ("own_offer_scopes", "already_invalid", "validation_controls"):
        if key in row:
            spec[key] = row[key]
    if spec["followers_only"] and spec.get("own_offer_scopes"):
        from blue_full_crawl import valid_own_scopes
        valid_own_scopes(spec)
    return spec


def task_key(spec: dict) -> str:
    identity = task_spec(spec)
    for key in ("own_offer_scopes", "already_invalid", "validation_controls"):
        identity.pop(key, None)
    return sha256(canonical(identity).encode()).hexdigest()


class Journal:
    def __init__(self, path: Path, node: str, *, clock=time.time):
        if node not in {"main", "laptop"}:
            raise ValueError("Unknown BLUE worker")
        self.path, self.node, self.clock = path, node, clock
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks(
                    task_key TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL,
                    synced_at REAL NOT NULL, lease_owner TEXT, lease_until REAL NOT NULL,
                    next_try REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS attempts(
                    attempt_id TEXT PRIMARY KEY, task_key TEXT NOT NULL, started_at REAL NOT NULL,
                    state TEXT NOT NULL, body TEXT, digest TEXT, acked_at REAL);
                CREATE INDEX IF NOT EXISTS attempt_task ON attempts(task_key,state);
            """)
            expected = canonical({"node": node, "version": VERSION, "kind": "blue-crawl-outbox"})
            db.execute("INSERT OR IGNORE INTO meta VALUES('identity',?)", (expected,))
            if db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0] != expected:
                raise ValueError("Journal identity mismatch; refusing reset")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Journal integrity check failed; refusing reset")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def sync(self, rows: list[dict]) -> None:
        now = self.clock()
        with self.connect() as db:
            existing = {}
            for old in db.execute("SELECT task_key,spec FROM tasks"):
                prior = json.loads(old["spec"])
                existing[(prior["batch_id"], prior["plid"])] = old["task_key"]
            for row in rows:
                spec = task_spec(row)
                if row["status"] not in {"pending", "leased", "retry", "succeeded", "terminal", "cancelled"}:
                    raise ValueError("Unknown central task status")
                # Reject changed instructions under an existing logical batch/PLID.
                logical = (spec["batch_id"], spec["plid"])
                if logical in existing and existing[logical] != task_key(spec):
                    raise ValueError("Task changed under an existing identity")
                existing[logical] = task_key(spec)
                db.execute("""INSERT INTO tasks(task_key,spec,status,synced_at,lease_owner,lease_until,next_try)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_key) DO UPDATE SET
                    spec=excluded.spec,status=excluded.status,synced_at=excluded.synced_at,
                    lease_owner=excluded.lease_owner,lease_until=excluded.lease_until,
                    next_try=CASE WHEN tasks.status='cancelled' AND excluded.status='pending'
                        THEN excluded.next_try ELSE MAX(tasks.next_try,excluded.next_try) END""",
                    (task_key(spec), canonical(spec), row["status"], now,
                     row.get("lease_owner"), float(row.get("lease_until") or 0),
                     now + max(0, float(row.get("available_after") or 0))))

    def recover(self) -> int:
        """Only call under the worker's exclusive OS lock on startup."""
        with self.connect() as db:
            return db.execute("UPDATE attempts SET state='interrupted' WHERE state='running'").rowcount

    def candidate(self) -> dict | None:
        now = self.clock()
        with self.connect() as db:
            size = db.execute("SELECT COALESCE(SUM(LENGTH(CAST(body AS BLOB))),0) FROM attempts WHERE state IN ('pending','blocked')").fetchone()[0]
            if size >= MAX_PENDING_BYTES:
                return None
            row = db.execute("""SELECT * FROM tasks t WHERE status IN ('pending','retry','leased')
                AND synced_at <= ? AND synced_at >= ? AND next_try <= ?
                AND (status != 'leased' OR lease_until <= ?)
                AND NOT EXISTS(SELECT 1 FROM attempts a WHERE a.task_key=t.task_key
                    AND a.state IN ('running','pending','blocked'))
                    ORDER BY CAST(json_extract(spec,'$.job_id') AS INTEGER),task_key LIMIT 1""",
                (now, now - OFFLINE_TTL, now, now)).fetchone()
            return json.loads(row["spec"]) if row else None

    def begin(self, spec: dict) -> str:
        key, identity = task_key(spec), uuid4().hex
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM tasks WHERE task_key=?", (key,)).fetchone():
                raise ValueError("Task must be durably cached before collection")
            if db.execute("SELECT 1 FROM attempts WHERE task_key=? AND state IN ('running','pending','blocked')", (key,)).fetchone():
                raise ValueError("Local attempt is already pending")
            db.execute("INSERT INTO attempts(attempt_id,task_key,started_at,state) VALUES(?,?,?,'running')",
                       (identity, key, self.clock()))
        return identity

    def save(self, envelope: dict) -> str:
        body = canonical(envelope)
        # Oversized results still stay on disk; ingestion may require attention.
        digest = sha256(body.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute("SELECT * FROM attempts WHERE attempt_id=?", (envelope["attempt_id"],)).fetchone()
            if (not row or row["task_key"] != task_key(envelope["task"])
                    or envelope["node"] != self.node):
                raise ValueError("Result identity mismatch")
            if row["body"] is not None:
                if row["digest"] != digest:
                    raise ValueError("Attempt result is immutable")
                return digest
            db.execute("UPDATE attempts SET state='pending',body=?,digest=? WHERE attempt_id=?",
                       (body, digest, envelope["attempt_id"]))
        return digest

    def pending(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT attempt_id,body,digest FROM attempts WHERE state='pending' ORDER BY started_at LIMIT 20")]

    def block(self, identity: str, digest: str) -> None:
        """Preserve invalid/oversized evidence for inspection without starving others."""
        with self.connect() as db:
            result = db.execute("UPDATE attempts SET state='blocked' WHERE attempt_id=? AND digest=? AND state='pending'",
                                (identity, digest))
            if result.rowcount != 1:
                raise ValueError("Cannot quarantine an unknown result")

    def acknowledge(self, identity: str, digest: str, *, succeeded: bool) -> None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM attempts WHERE attempt_id=? AND digest=?", (identity, digest)).fetchone()
            if not row or row["state"] not in {"pending", "acked"}:
                raise ValueError("ACK does not match an immutable local result")
            if row["state"] == "acked":
                return
            db.execute("UPDATE attempts SET state='acked',acked_at=? WHERE attempt_id=?", (self.clock(), identity))
            if succeeded:
                db.execute("UPDATE tasks SET status='succeeded' WHERE task_key=?", (row["task_key"],))
            else:
                failures = db.execute("SELECT failures FROM tasks WHERE task_key=?", (row["task_key"],)).fetchone()[0] + 1
                delay = min(1800, 60 * 2 ** min(failures - 1, 5))
                db.execute("UPDATE tasks SET failures=?,next_try=? WHERE task_key=?",
                           (failures, self.clock() + delay, row["task_key"]))

    def counts(self) -> dict:
        with self.connect() as db:
            return {row[0]: row[1] for row in db.execute("SELECT state,COUNT(*) FROM attempts GROUP BY state")}
