"""Freshness-checked BLUE laptop business reads; auth and all writes stay primary."""
from __future__ import annotations

import json
import threading


_lock = threading.Lock()
_counts = {"replica_connections": 0, "primary_fallbacks": 0, "last_fallback": None}


def read_status() -> dict:
    with _lock:
        return dict(_counts)


def record(replica: bool, reason: str = "") -> None:
    with _lock:
        _counts["replica_connections" if replica else "primary_fallbacks"] += 1
        if not replica:
            _counts["last_fallback"] = reason


def validate_replica(conn) -> None:
    import blue_node
    if blue_node.config()["node"] != "laptop":
        raise RuntimeError("Replica reads are laptop-only")
    with conn.cursor() as cursor:
        cursor.execute("SELECT @@hostname,@@server_id,@@port,@@datadir,"
                       "@@global.read_only,@@global.super_read_only")
        row = cursor.fetchone()
        blue_node.validate_database_identity(row[:4], "LAPTOP-2T5MN8EU")
        if tuple(row[4:]) != (1, 1):
            raise RuntimeError("BLUE replica must remain read-only")
        cursor.execute("SELECT environment,seed_sha256 FROM takealot_ops.blue_environment_marker WHERE id=1")
        seed = json.loads((blue_node.ROOT / "state/seed.json").read_text(encoding="utf-8"))
        if cursor.fetchone() != ("blue", seed["seed_sha256"]):
            raise RuntimeError("BLUE replica seed mismatch")


def choose_connection(primary_factory, replica_factory, validator=validate_replica):
    # Failure to verify the primary is not permission to serve an old backup.
    source = primary_factory()
    replica = None
    try:
        with source.cursor() as cursor:
            cursor.execute("SELECT @@GLOBAL.gtid_executed")
            boundary = cursor.fetchone()[0]
        try:
            if not boundary:
                raise RuntimeError("Missing primary GTID boundary")
            replica = replica_factory()
            validator(replica)
            with replica.cursor() as cursor:
                cursor.execute("SELECT GTID_SUBSET(%s, @@GLOBAL.gtid_executed)", (boundary,))
                if cursor.fetchone()[0] != 1:
                    raise RuntimeError("Replica has not reached the primary boundary")
            chosen, role = replica, "replica"
        except Exception as exc:
            if replica is not None:
                replica.close()
            replica = None
            chosen, role = source, "primary"
            record(False, type(exc).__name__)
        chosen.select_db("takealot_ops")
        chosen.rollback()
        with chosen.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
        chosen.autocommit(False)
        chosen._blue_read_role = role
        if role == "replica":
            source.close()
            record(True)
        return chosen
    except BaseException:
        if replica is not None:
            replica.close()
        source.close()
        raise


def create_read_engine(database_url: str):
    import blue_node
    import blue_runtime
    import blue_test_db
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool
    if blue_node.config()["node"] != "laptop" or database_url != blue_runtime.database_url():
        raise RuntimeError("Unapproved BLUE read routing origin")
    def connect():
        return choose_connection(
            lambda: blue_test_db.primary_connection("blue_web_laptop", writable=True),
            lambda: blue_node.connection("blue_app"))
    # A new verified transaction per checkout prevents a pooled replica connection
    # from serving stale data after replication stops. No retry of business writes.
    return create_engine("mysql+pymysql://", creator=connect, poolclass=NullPool)
