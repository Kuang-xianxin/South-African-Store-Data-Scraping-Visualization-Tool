"""Transactional operational ledger; never stores Seller credentials or responses."""
from __future__ import annotations

import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3

from blue_seller_authority import initial_state, transition


def initialize(path: Path) -> None:
    # Explicit provisioning only. Runtime opens with mode=rw and cannot reset it.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.executescript("""
            CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
            CREATE TABLE runs (id TEXT PRIMARY KEY, generation INTEGER NOT NULL);
            CREATE TABLE receipts (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)
        value = initial_state()
        value.pop("used_runs")
        value.pop("completed")
        conn.execute("INSERT INTO metadata VALUES (1,?)", (json.dumps(value),))


def execute(path: Path, node: str, message: dict, *, now: float, boot: str, reports: dict) -> dict:
    # Query only the referenced IDs; ledger size does not make every request O(history).
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=10)
    try:
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT payload FROM metadata WHERE id=1").fetchone()
        if not row:
            raise RuntimeError("missing-seller-metadata")
        state = json.loads(row[0])
        run_ids = {message.get("run_id")}
        if state.get("run"):
            run_ids.add(state["run"]["id"])
        state["used_runs"] = {}
        for run_id in run_ids:
            if isinstance(run_id, str):
                row = conn.execute("SELECT generation FROM runs WHERE id=?", (run_id,)).fetchone()
                if row:
                    state["used_runs"][run_id] = row[0]
        state["completed"] = {}
        request_id = message.get("request_id")
        if isinstance(request_id, str):
            row = conn.execute("SELECT payload FROM receipts WHERE id=?", (request_id,)).fetchone()
            if row:
                state["completed"][request_id] = json.loads(row[0])
        updated, reply = transition(state, node, message, now=now, boot=boot, reports=reports)
        for key, generation in updated.pop("used_runs").items():
            if key not in state["used_runs"]:
                conn.execute("INSERT INTO runs VALUES (?,?)", (key, generation))
        for key, value in updated.pop("completed").items():
            if key not in state["completed"]:
                conn.execute("INSERT INTO receipts VALUES (?,?)", (key, json.dumps(value)))
        conn.execute("UPDATE metadata SET payload=? WHERE id=1", (json.dumps(updated),))
        conn.commit()  # Never send an admission reply before this durable commit.
        return reply
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
