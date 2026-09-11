"""Install a frozen BLUE ingress policy without reloading either ERP node."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import tempfile
import time
from urllib.request import ProxyHandler, build_opener


TARGET = Path("/opt/takealot-blue-routing")
FILES = ("blue_seller_refresh_policy.py", "blue_web_router.py")
SERVICE = "takealot-blue-routing.service"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def status():
    with build_opener(ProxyHandler({})).open("http://127.0.0.1:18504/status", timeout=4) as response:
        return json.load(response)


def verified_idle(state):
    assert state.get("routing_version") == 2, "Unknown routing state"
    samples = state["samples"]
    assert set(samples) == {"main", "laptop"}, "Both inventories are required for installation"
    assert len({s["frontend_sha256"] for s in samples.values()}) == 1, "Release mismatch"
    for node, sample in samples.items():
        assert sample["age_seconds"] < 12, f"Stale {node} inventory"
        assert sample["routing_version"] == 2, f"Unknown {node} inventory"
        assert sample["workflows"]["refresh"]["busy"] is False, "Preserve active refresh"
    assert state["owners"].get("refresh", {}).get("until", 0) <= time.time(), "Preserve reserved start"


def replace(path, content):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".seller-policy-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o644)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    stage = args.stage.resolve(strict=True)
    assert os.geteuid() == 0, "Use the existing server administrator"
    assert TARGET.is_dir() and not TARGET.is_symlink(), "Unexpected routing installation"
    manifest = json.loads((stage / "manifest.json").read_text())
    assert set(manifest) == set(FILES), "Unexpected release scope"
    for name in FILES:
        source, target = stage / name, TARGET / name
        assert not source.is_symlink() and not target.is_symlink(), "Symlink in release"
        assert digest(source) == manifest[name]["after"], f"Candidate hash mismatch: {name}"
        assert digest(target) == manifest[name]["before"], f"Deployed baseline changed: {name}"
        py_compile.compile(str(source), doraise=True)
    before = status()
    verified_idle(before)
    nginx_before = {str(p): digest(p) for p in Path("/etc/nginx/conf.d").glob("*.conf")}
    if not args.apply:
        print(json.dumps({"ready": True, "files": list(FILES), "node_restart_required": False}))
        return
    backup = Path(tempfile.mkdtemp(prefix="takealot-blue-seller-", dir="/var/backups"))
    backup.chmod(0o700)
    (backup / "before-status.json").write_text(json.dumps(before))
    for name in FILES:
        target = TARGET / name
        if target.exists():
            shutil.copy2(target, backup / name)
    ownership = Path("/var/lib/takealot-blue-routing/ownership.json")
    shutil.copy2(ownership, backup / "ownership-before.json")
    verified_idle(status())
    try:
        for name in FILES:
            replace(TARGET / name, (stage / name).read_bytes())
        subprocess.run(["systemctl", "restart", SERVICE], check=True)
        deadline = time.monotonic() + 35
        while True:
            try:
                after = status()
                verified_idle(after)
                assert after.get("seller_refresh_policy") == "main-first-v1"
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)
        assert nginx_before == {
            str(p): digest(p) for p in Path("/etc/nginx/conf.d").glob("*.conf")
        }, "Ingress configuration unexpectedly changed"
        assert all(digest(TARGET / name) == manifest[name]["after"] for name in FILES)
        for node in ("main", "laptop"):
            assert before["samples"][node]["boot"] == after["samples"][node]["boot"], "ERP process changed"
        (backup / "after-status.json").write_text(json.dumps(after))
        print(json.dumps({"installed": True, "at": datetime.now(timezone.utc).isoformat(),
                          "backup": str(backup), "policy": "main-first-v1",
                          "files": {name: digest(TARGET / name) for name in FILES},
                          "erp_restarted": False, "nginx_changed": False}))
    except BaseException:
        # State remains version 2. Preserve any new ownership decisions; restoring
        # an older ownership snapshot could misroute a request already dispatched.
        for name in reversed(FILES):
            saved = backup / name
            if saved.exists():
                replace(TARGET / name, saved.read_bytes())
            elif manifest[name]["before"] is None and digest(TARGET / name) == manifest[name]["after"]:
                (TARGET / name).unlink()
        subprocess.run(["systemctl", "restart", SERVICE], check=True)
        raise


if __name__ == "__main__":
    main()
