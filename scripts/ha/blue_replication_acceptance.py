"""Bounded CREATE/INSERT/UPDATE/DELETE/DROP probe on the exact two BLUE databases."""
from __future__ import annotations

import json
import subprocess
from uuid import uuid4

import blue_node
import blue_test_db


def replica_check(gtid: str, expected: list[list[object]] | None) -> None:
    code = f'''
import sys,json
sys.path.insert(0,'D:/TakealotBlue')
import blue_node
with blue_node.connection() as conn,conn.cursor() as q:
 q.execute("SELECT WAIT_FOR_EXECUTED_GTID_SET(%s,20)",({gtid!r},))
 assert q.fetchone()[0] == 0, 'BLUE replica failed to catch up'
 if {expected!r} is None:
  q.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='takealot_ops' AND table_name='blue_acceptance_probe'")
  assert q.fetchone()[0] == 0, 'Probe table drop not replicated'
 else:
  q.execute("SELECT token,value FROM takealot_ops.blue_acceptance_probe ORDER BY token")
  assert [list(r) for r in q.fetchall()] == {expected!r}, 'Probe data mismatch'
 q.execute("SELECT @@server_id,@@global.read_only,@@global.super_read_only")
 assert q.fetchone() == (102,1,1), 'Unexpected BLUE replica role'
print('BLUE_REPLICA_VERIFIED')
'''
    result = subprocess.run([
        "ssh.exe", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "takealot-admin-laptop",
        "D:/TakealotBlue/runtime/Scripts/python.exe", "-",
    ], input=code, capture_output=True, text=True, timeout=50)
    if result.returncode or "BLUE_REPLICA_VERIFIED" not in result.stdout:
        raise RuntimeError("BLUE replica acceptance failed; inspect replica state")


def main() -> None:
    blue_test_db.require_main()
    token = uuid4().hex
    evidence = []
    with blue_test_db.primary_connection("blue_web_main", writable=True) as conn, conn.cursor() as q:
        q.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='takealot_ops' AND table_name='blue_acceptance_probe'")
        if q.fetchone()[0]:
            raise RuntimeError("Probe table already exists; refusing overwrite")
        q.execute("CREATE TABLE takealot_ops.blue_acceptance_probe (token VARCHAR(32) PRIMARY KEY,value INT NOT NULL) ENGINE=InnoDB")
        for action, statement, params, expected in (
            ("insert", "INSERT INTO takealot_ops.blue_acceptance_probe VALUES (%s,1)", (token,), [[token, 1]]),
            ("update", "UPDATE takealot_ops.blue_acceptance_probe SET value=2 WHERE token=%s", (token,), [[token, 2]]),
            ("delete", "DELETE FROM takealot_ops.blue_acceptance_probe WHERE token=%s", (token,), []),
            ("drop", "DROP TABLE takealot_ops.blue_acceptance_probe", (), None),
        ):
            q.execute(statement, params)
            q.execute("SELECT @@global.gtid_executed")
            gtid = q.fetchone()[0]
            replica_check(gtid, expected)
            evidence.append({"step": action, "replica_verified": True, "gtid": gtid})
            print(json.dumps(evidence[-1]), flush=True)
    (blue_node.ROOT / "state/replication-acceptance.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
