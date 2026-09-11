"""Install dormant Seller admission beside the existing observe-only arbiter.

Run as root on Tencent with a reviewed package. Does not enable Seller admission,
edit the arbiter configuration, reset a ledger, or touch either business database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sys
import uuid


BASE = Path("/var/lib/takealot-blue-arbiter")
LIB = Path("/usr/local/libexec")
CONFIG = Path("/etc/takealot-blue-arbiter.json")
FILES = {"blue_seller_authority.py": "blue_seller_authority.py",
         "blue_seller_ledger.py": "blue_seller_ledger.py", "blue_arbiter.py": "takealot-blue-arbiter"}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(package: Path) -> dict:
    if os.geteuid() != 0:
        raise RuntimeError("root-required-for-reviewed-arbiter-install")
    cfg = json.loads(CONFIG.read_text())
    if (cfg.get("cluster") != "takealot-blue-3307-v1" or cfg.get("mode") != "observe"
            or cfg.get("seller_api", {}).get("enabled")):
        raise RuntimeError("only-disabled-observe-stage-supported")
    manifest = json.loads((package / "manifest.json").read_text())
    if manifest.get("kind") != "blue-seller-arbiter-disabled" or set(manifest.get("files", {})) != set(FILES):
        raise RuntimeError("unreviewed-file-inventory")
    for source, target in FILES.items():
        if sha(package / source) != manifest["files"][source]:
            raise RuntimeError("package-hash-mismatch")
        destination = LIB / target
        if destination.is_symlink():
            raise RuntimeError("linked-runtime-refused")
        if (source != "blue_arbiter.py" and destination.exists()
                and sha(destination) not in {manifest["files"][source], manifest.get("previous_files", {}).get(source)}):
            raise RuntimeError("existing-helper-needs-review")
    if sha(LIB / "takealot-blue-arbiter") != manifest["previous_arbiter_sha256"]:
        raise RuntimeError("arbiter-changed-since-review")
    archive = BASE / "backups" / ("seller-authority-" + uuid.uuid4().hex)
    archive.mkdir(parents=True, mode=0o700)
    shutil.copy2(LIB / "takealot-blue-arbiter", archive / "arbiter-before.py")
    shutil.copy2(package / "manifest.json", archive / "install-manifest.json")
    for source, target in FILES.items():
        destination = LIB / target
        temporary = LIB / (target + "." + uuid.uuid4().hex + ".stage")
        temporary.write_bytes((package / source).read_bytes())
        os.chown(temporary, 0, 0)
        os.chmod(temporary, 0o755 if source == "blue_arbiter.py" else 0o644)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    descriptor = os.open(LIB, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    sys.path.insert(0, str(LIB))
    from blue_seller_ledger import initialize

    ledger = BASE / "state/seller-api.sqlite3"
    if not ledger.exists():
        initialize(ledger)
        account = pwd.getpwnam("takealot-blue-arbiter")
        os.chown(ledger, account.pw_uid, account.pw_gid)
        os.chmod(ledger, 0o600)
    # Missing state is provisioned exactly once; an existing ledger is NEVER
    # replaced from the backup directory, even if a future upgrade fails.
    return {"files": len(FILES), "seller_enabled": False, "database_mode": cfg["mode"],
            "backup": str(archive), "configuration_changed": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.package.resolve(strict=True))))
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
