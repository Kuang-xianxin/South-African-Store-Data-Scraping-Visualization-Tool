"""Guarded five-file BLUE web overlay; never opens crawler journals."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path("D:/TakealotBlue")
ALLOWED = {"blue_web.py", "blue_runtime.py", "blue_read_replica.py",
           "blue_web_capacity.py", "app/src/takealot_ops/erp/service.py"}
MODE = {"version": 1, "web_routing": True, "replica_reads": True, "crawler_policy_changed": False}

def digest(value):
    return hashlib.sha256(value).hexdigest()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    import blue_node
    with blue_node.connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT @@global.read_only,@@global.super_read_only")
        expected = (0, 0) if blue_node.config()["node"] == "main" else (1, 1)
        if cursor.fetchone() != expected:
            raise RuntimeError("BLUE database role changed")
    if not (ROOT / "secrets/web-routing.key").is_file():
        raise RuntimeError("Missing protected web routing credential")
    archive = ROOT / "staging/blue-web-routing-20260909.zip"
    if digest(archive.read_bytes()) != "7fc8f2b247556e85810eb29e9f6ca98ca94e58dc9ffabd1468aa61db1f47cb26":
        raise RuntimeError("Unexpected routing archive digest")
    with zipfile.ZipFile(archive) as zipped:
        if set(zipped.namelist()) != ALLOWED | {"manifest.json"}:
            raise RuntimeError("Unexpected routing archive scope")
        manifest = json.loads(zipped.read("manifest.json"))
        if set(manifest) != ALLOWED:
            raise RuntimeError("Unexpected routing manifest")
        for name, hashes in manifest.items():
            target = ROOT / name
            if target.is_symlink() or not target.resolve().is_relative_to(ROOT.resolve()):
                raise RuntimeError("Unsafe overlay path")
            actual = digest(target.read_bytes()) if target.exists() else None
            if actual not in {hashes["before"], hashes["after"]}:
                raise RuntimeError("Changed deployment baseline: " + name)
            if digest(zipped.read(name)) != hashes["after"]:
                raise RuntimeError("Corrupt candidate: " + name)
        if args.apply:
            backup = ROOT / "staging" / ("pre-web-routing-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
            backup.mkdir()
            changed = []
            marker = ROOT / "state/web-routing-enabled.json"
            previous_marker = marker.read_bytes() if marker.exists() else None
            try:
                for name in sorted(ALLOWED):
                    target = ROOT / name
                    saved = backup / name
                    if target.exists():
                        saved.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(target, saved)
                    changed.append(name)
                    temporary = target.with_name(target.name + ".routing-next")
                    temporary.write_bytes(zipped.read(name))
                    temporary.replace(target)
                marker.write_text(json.dumps(MODE), encoding="ascii")
            except BaseException:
                for name in reversed(changed):
                    saved, target = backup / name, ROOT / name
                    if saved.exists():
                        shutil.copy2(saved, target)
                    elif target.exists():
                        target.unlink()
                if previous_marker is not None:
                    marker.write_bytes(previous_marker)
                elif marker.exists():
                    marker.unlink()
                raise
            print(json.dumps({"applied": True, "backup": str(backup), "files": len(ALLOWED)}))
        else:
            print(json.dumps({"ready": True, "node": blue_node.config()["node"], "files": len(ALLOWED)}))

if __name__ == "__main__":
    main()
