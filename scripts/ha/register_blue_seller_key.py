"""Register a public, node-bound, Seller-only forced-command key on Tencent."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import sys
import uuid


def authorized_line(record: dict) -> str:
    if (record.get("version") != 1 or record.get("cluster") != "takealot-blue-3307-v1"
            or record.get("node") not in {"main", "laptop"} or record.get("scope") != "seller-only"
            or not re.fullmatch(r"S-1-[0-9-]+", record.get("sid", ""))):
        raise ValueError("invalid-public-identity-record")
    fields = record["public_key"].split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519" or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", fields[1]):
        raise ValueError("unsupported-public-key")
    payload = base64.b64decode(fields[1], validate=True)
    if len(payload) != 51 or not payload.startswith(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"):
        raise ValueError("invalid-ed25519-key-payload")
    node = record["node"]
    return (f'restrict,command="/usr/bin/python3 /usr/local/libexec/takealot-blue-arbiter {node} --seller-only" '
            f'ssh-ed25519 {fields[1]} blue-seller-{node}-{record["sid"]}')


def register(record: dict) -> dict:
    import fcntl
    import pwd

    if os.geteuid() != 0:
        raise RuntimeError("root-required")
    line = authorized_line(record)
    account = pwd.getpwnam("takealot-blue-arbiter")
    directory = Path(account.pw_dir) / ".ssh"
    target = directory / "authorized_keys"
    if directory.is_symlink() or target.is_symlink():
        raise RuntimeError("linked-authorized-keys-refused")
    with (directory / "seller-key-registration.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = target.read_text()
        token = record["public_key"].split()[1]
        existing = [item for item in original.splitlines() if token in item.split()]
        if existing and existing != [line]:
            raise RuntimeError("existing-key-has-different-scope")
        if not existing:
            temporary = target.with_name("authorized_keys." + uuid.uuid4().hex + ".stage")
            with temporary.open("x") as stream:
                stream.write(original.rstrip("\n") + "\n" + line + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.chown(temporary, account.pw_uid, account.pw_gid)
            os.replace(temporary, target)
    return {"registered": True, "node": record["node"], "sid": record["sid"], "scope": "seller-only"}


if __name__ == "__main__":
    try:
        print(json.dumps(register(json.loads(Path(sys.argv[1]).read_text()))))
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
