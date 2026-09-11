"""Freeze and overlay the shared ERP application in BLUE, preserving node state.

This utility never copies .env, node launchers, secrets, databases, journals,
virtual environments or green service settings. Stop BLUE web/worker before apply.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import sys
import zipfile


BLUE_ROOT = Path("D:/TakealotBlue")
APP_FILES = {"pyproject.toml", "scripts/build_nft102_payload.py",
             "scripts/update_nft102_daily.ps1", "scripts/write_nft102_workbook.py"}
BLUE_RUNTIME_FILES = frozenset({"blue_shared_crawl.py", "blue_crawl_journal.py",
    "blue_crawl_delivery.py", "blue_resilient_worker.py", "blue_full_crawl.py"})
BLUE_PREFIX = "blue-runtime/"
ASSET_REFERENCE = re.compile(r'''["'](?:/?assets/|\./)?([^"'\s/]+\.(?:js|css))["']''')


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def allowed(relative: str) -> bool:
    path = PurePosixPath(relative)
    if (not relative or path.is_absolute() or ".." in path.parts or "\\" in relative
            or ":" in relative or str(path) != relative):
        return False
    return (relative in APP_FILES
            or (relative.startswith("src/takealot_ops/") and path.suffix == ".py")
            or (relative.startswith("config/") and len(path.parts) == 2
                and path.suffix in {".yaml", ".json", ".txt"})
            or relative == "frontend/competitor/dist/index.html"
            or (relative.startswith("frontend/competitor/dist/assets/")
                and len(path.parts) == 5 and path.suffix in {".js", ".css"}))


def target_path(app: Path, relative: str) -> Path:
    if not allowed(relative):
        raise ValueError(f"Disallowed shared application path: {relative}")
    return contained_path(app, relative)


def contained_path(app: Path, relative: str) -> Path:
    target = app / relative
    if app.is_symlink() or not target.resolve().is_relative_to(app.resolve()):
        raise ValueError("Release target escapes BLUE app")
    for parent in (target, *target.parents):
        if parent == app.parent:
            break
        try:
            reparse = bool(getattr(parent.lstat(), "st_file_attributes", 0) & 0x400)
        except FileNotFoundError:
            reparse = False
        if parent.is_symlink() or reparse:
            raise ValueError("Release target must not traverse a link")
    return target


def release_target(app: Path, relative: str) -> Path:
    if relative.startswith(BLUE_PREFIX) and relative[len(BLUE_PREFIX):] in BLUE_RUNTIME_FILES:
        return contained_path(BLUE_ROOT, relative[len(BLUE_PREFIX):])
    return target_path(app, relative)


def asset_graph(dist: Path) -> set[str]:
    pending = ["index.html"]
    found: set[str] = set()
    while pending:
        relative = pending.pop()
        if relative in found:
            continue
        data = (dist / relative).read_text(encoding="utf-8")
        found.add(relative)
        pending.extend("assets/" + name for name in ASSET_REFERENCE.findall(data)
                       if "assets/" + name not in found)
    return found


def build(project: Path, archive: Path, *, with_blue_runtime: bool = False,
          frontend_dist: Path | None = None) -> dict:
    project = project.resolve(strict=True)
    dist = (frontend_dist or project / "frontend/competitor/dist").resolve(strict=True)
    paths = set(APP_FILES)
    paths.update(p.relative_to(project).as_posix()
                 for p in (project / "src/takealot_ops").rglob("*.py"))
    paths.update(p.relative_to(project).as_posix()
                 for p in (project / "config").iterdir() if p.is_file())
    assets = asset_graph(dist)
    if not all(allowed(name) for name in paths):
        raise ValueError("Unexpected input file; review the release allowlist")
    sources = {name: project / name for name in paths}
    sources.update({"frontend/competitor/dist/" + name: dist / name for name in assets})
    if with_blue_runtime:
        sources.update({BLUE_PREFIX + name: project / "scripts/ha" / name for name in BLUE_RUNTIME_FILES})
    payload = {name: sources[name].read_bytes() for name in sorted(sources)}
    manifest = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                "source": str(project), "frontend_dist": str(dist),
                "files": {name: digest(data) for name, data in payload.items()}}
    # Detect concurrent editing instead of combining two versions silently.
    for name, data in payload.items():
        if sources[name].read_bytes() != data:
            raise RuntimeError(f"Source changed during snapshot: {name}")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest, indent=2))
        for name, data in payload.items():
            bundle.writestr(name, data)
    result = {"archive": str(archive.resolve()), "sha256": digest(archive.read_bytes()),
              "files": len(payload), "asset_files": len(assets),
              "blue_runtime_files": len(BLUE_RUNTIME_FILES) if with_blue_runtime else 0}
    archive.with_suffix(".json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def read_bundle(archive: Path, expected: str) -> tuple[dict, dict[str, bytes]]:
    raw = archive.read_bytes()
    if digest(raw) != expected:
        raise ValueError("Release SHA256 mismatch")
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate archive members")
        manifest = json.loads(bundle.read("manifest.json"))
        if manifest["version"] != 1 or set(names) != {*manifest["files"], "manifest.json"}:
            raise ValueError("Manifest does not cover the exact archive")
        runtime = {name for name in manifest["files"] if name.startswith(BLUE_PREFIX)}
        if runtime and runtime != {BLUE_PREFIX + name for name in BLUE_RUNTIME_FILES}:
            raise ValueError("BLUE runtime overlay must contain the complete reviewed five-file set")
        payload = {}
        for name, checksum in manifest["files"].items():
            if not allowed(name) and name not in runtime:
                raise ValueError("Archive includes a protected or unsafe path")
            data = bundle.read(name)
            if digest(data) != checksum:
                raise ValueError("Archive member checksum mismatch")
            payload[name] = data
    return manifest, payload


def inspect_release(archive: Path, expected: str, *, apply: bool = False) -> dict:
    sys.path.insert(0, str(BLUE_ROOT))
    import blue_node

    cfg = blue_node.config()
    if blue_node.ROOT.resolve() != BLUE_ROOT.resolve():
        raise RuntimeError("Unexpected BLUE root")
    manifest, payload = read_bundle(archive, expected)
    app = BLUE_ROOT / "app"
    if (app / ".env").exists():
        raise RuntimeError("BLUE must not inherit a project .env")
    changed = [name for name, data in payload.items()
               if not release_target(app, name).is_file()
               or release_target(app, name).read_bytes() != data]
    result = {"node": cfg["node"], "sha256": expected, "files": len(payload),
              "changed": changed, "applied": False}
    if not apply:
        return result
    with socket.socket() as probe:
        probe.settimeout(2)
        if probe.connect_ex(("127.0.0.1", 8503)) == 0:
            raise RuntimeError("BLUE web must be stopped before application overlay")
    # The deployment wrapper must stop the worker too; retain every delivery row.
    journal = BLUE_ROOT / "state/crawl-outbox/journal.sqlite3"
    if journal.exists():
        import sqlite3
        # A stopped Windows worker can leave WAL recovery pending. SQLite needs
        # a writable handle for recovery even when our inspection is read-only.
        with sqlite3.connect(journal) as db:
            db.execute("PRAGMA query_only=ON")
            active = db.execute("SELECT COUNT(*) FROM attempts WHERE state IN ('running','pending','blocked')").fetchone()[0]
        if active:
            raise RuntimeError("BLUE has in-flight or undelivered observations")
    backup = BLUE_ROOT / "staging" / ("pre-common-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    backup.mkdir(parents=True)
    prior = {}
    for name in changed:
        target = release_target(app, name)
        prior[name] = digest(target.read_bytes()) if target.exists() else None
        if target.exists():
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
    (backup / "manifest.json").write_text(json.dumps(prior, indent=2), encoding="utf-8")
    # Keep old hash assets and all runtime state. Publish the entrypoint last.
    entry = "frontend/competitor/dist/index.html"
    for name in sorted(changed, key=lambda value: value == entry):
        target = release_target(app, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".blue-sync-tmp")
        with temporary.open("xb") as stream:
            stream.write(payload[name])
        temporary.replace(target)
    for name, data in payload.items():
        if release_target(app, name).read_bytes() != data:
            raise RuntimeError(f"Installed file mismatch: {name}; backup: {backup}")
    record = {**manifest, "archive_sha256": expected, "backup": str(backup), "node": cfg["node"]}
    (BLUE_ROOT / "state/common-release.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return {**result, "applied": True, "backup": str(backup)}


def prepare_schema() -> dict:
    """Only the additive delta needed by the shared release; no data backfills."""
    sys.path.insert(0, str(BLUE_ROOT))
    import blue_node
    import blue_test_db

    blue_test_db.require_main()
    with blue_node.connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT @@hostname,@@server_id,@@port,@@datadir,@@server_uuid,"
                       "@@global.read_only,@@global.super_read_only")
        blue_test_db.validate_primary(cursor.fetchone(), writable=True)
        cursor.execute("CREATE TABLE IF NOT EXISTS takealot_ops.erp_data_revisions ("
                       "scope VARCHAR(64) NOT NULL, topic VARCHAR(32) NOT NULL,"
                       "revision VARCHAR(32) NOT NULL, PRIMARY KEY(scope,topic))")
        cursor.execute("SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema='takealot_ops' "
                       "AND table_name='daily_report_observations' "
                       "AND index_name='ix_daily_report_observations_store_run_views'")
        if not cursor.fetchone()[0]:
            cursor.execute("ALTER TABLE takealot_ops.daily_report_observations "
                           "ADD INDEX ix_daily_report_observations_store_run_views "
                           "(store_code,run_id,page_views_30_days), ALGORITHM=INPLACE, LOCK=NONE")
        # Account grants belong to the fixed primary, not replicated account metadata.
        cursor.execute("SET SESSION sql_log_bin=0")
        for user in ("blue_worker_main", "blue_worker_laptop"):
            cursor.execute(f"GRANT INSERT,UPDATE ON takealot_ops.erp_data_revisions TO '{user}'@'localhost'")
    return {"schema": "ready", "primary_server_id": 101, "green_changed": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "inspect", "apply", "schema"))
    parser.add_argument("--project", type=Path)
    parser.add_argument("--frontend-dist", type=Path,
                        help="Verified isolated build directory; never replaces the live GREEN entrypoint")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--with-blue-runtime", action="store_true",
                        help="Include the explicitly reviewed daily BLUE crawler five-file overlay")
    args = parser.parse_args()
    if args.command == "build":
        if not args.project or not args.archive:
            parser.error("build requires --project and --archive")
        result = build(args.project, args.archive, with_blue_runtime=args.with_blue_runtime,
                       frontend_dist=args.frontend_dist)
    elif args.command == "schema":
        result = prepare_schema()
    else:
        if not args.archive or not args.sha256:
            parser.error("inspect/apply require --archive and --sha256")
        result = inspect_release(args.archive, args.sha256, apply=args.command == "apply")
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
