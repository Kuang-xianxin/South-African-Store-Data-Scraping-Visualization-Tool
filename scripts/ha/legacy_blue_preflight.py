"""Read-only identity guard for retiring ONLY the laptop's legacy blue database."""

from __future__ import annotations

import json
import os
from pathlib import Path

import blue_node


COMPUTER = "LAPTOP-2T5MN8EU"
LEGACY_ROOT = Path("D:/TakealotMySQLReplica")


def validate_legacy_identity(row: tuple[object, ...], computer: str) -> None:
    expected = (COMPUTER, 2, 3306, "d:/takealotmysqlreplica/data", 1, 1)
    actual = (*row[:3], str(row[3]).replace("\\", "/").rstrip("/").casefold(), *row[4:])
    if computer != COMPUTER or actual != expected:
        raise RuntimeError("Legacy retirement refused: laptop database identity mismatch")


def audit() -> dict[str, object]:
    computer = os.environ.get("COMPUTERNAME", "")
    if computer != COMPUTER:
        raise RuntimeError("Legacy retirement is laptop-only; main must never be targeted")
    import pymysql

    with blue_node.connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT @@hostname,@@server_id,@@port,@@datadir,"
                       "@@global.read_only,@@global.super_read_only")
        new_identity = cursor.fetchone()
        if tuple(new_identity[4:]) != (1, 1):
            raise RuntimeError("New blue is not the expected read-only test stage")
        cursor.execute("SELECT environment FROM takealot_ops.blue_environment_marker WHERE id=1")
        if cursor.fetchone() != ("blue",):
            raise RuntimeError("New blue seed marker missing")
    payload = blue_node.protect_secret(
        (LEGACY_ROOT / "secrets/mysql-dba.dpapi").read_bytes(), decrypt=True
    ).decode("utf-8")
    username, password = payload.split("\n", 1)
    with pymysql.connect(host="127.0.0.1", port=3306, user=username.strip(),
                         password=password.strip(), connect_timeout=5, read_timeout=10) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT @@hostname,@@server_id,@@port,@@datadir,"
                           "@@global.read_only,@@global.super_read_only")
            old_identity = cursor.fetchone()
            validate_legacy_identity(old_identity, computer)
            cursor.execute("SELECT user,host,command FROM information_schema.processlist")
            clients = cursor.fetchall()
            for user, host, command in clients:
                if user in {"system user", "event_scheduler"}:
                    continue
                if user not in {username.strip(), "takealot_app"} or not host.startswith("127.0.0.1:"):
                    raise RuntimeError("Unexpected legacy database client; inspect before retirement")
                if user == "takealot_app" and command != "Sleep":
                    raise RuntimeError("Legacy application is busy; retry retirement when idle")
    return {"computer": computer, "old_identity": old_identity, "new_identity": new_identity,
            "status": "safe-to-retire-legacy-only"}


if __name__ == "__main__":
    try:
        print(json.dumps(audit()))
    except Exception as exc:
        # Connection errors must not print credentials or DSNs.
        raise SystemExit(f"Legacy preflight failed ({type(exc).__name__})") from None
