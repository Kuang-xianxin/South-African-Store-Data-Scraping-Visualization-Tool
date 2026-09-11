"""Shared release must not overwrite BLUE identity, runtime or checkpoints."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location(
    "blue_sync_release", Path(__file__).resolve().parents[2] / "scripts/ha/blue_sync_release.py")
assert spec and spec.loader
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


@pytest.mark.parametrize("path", ["../.env", "/src/takealot_ops/a.py", "C:/bad.py",
    "src\\takealot_ops\\a.py", "src/takealot_ops/../../.env", "config/.env",
    "state/crawl-outbox/journal.sqlite3", "logs/competitor-batch-queue.json",
    "blue_runtime.py", "scripts/ha/blue_web.py", ".venv/pyvenv.cfg", "secrets/mysql.dpapi"])
def test_protected_paths_are_rejected(path):
    assert not sync.allowed(path)


def test_recursive_asset_graph_includes_lazy_chunks(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text('<script src="/assets/index-a.js"></script>')
    (tmp_path / "assets/index-a.js").write_text('import("./page-b.js");const css="assets/page-c.css"')
    (tmp_path / "assets/page-b.js").write_text('import "./index-a.js";')
    (tmp_path / "assets/page-c.css").write_text("body{}")
    assert sync.asset_graph(tmp_path) == {"index.html", "assets/index-a.js", "assets/page-b.js", "assets/page-c.css"}


def test_isolated_build_does_not_replace_green_entrypoint(tmp_path):
    project = tmp_path / "project"
    for name in (*sync.APP_FILES, "src/takealot_ops/__init__.py", "config/settings.yaml",
                 "frontend/competitor/dist/index.html"):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"original")
    dist = tmp_path / "isolated-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<script src="/assets/new.js"></script>')
    (dist / "assets/new.js").write_text('console.log("blue");')
    archive = tmp_path / "release.zip"
    sync.build(project, archive, frontend_dist=dist)
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.read("frontend/competitor/dist/index.html") == (dist / "index.html").read_bytes()
        assert bundle.read("frontend/competitor/dist/assets/new.js") == (dist / "assets/new.js").read_bytes()
    assert (project / "frontend/competitor/dist/index.html").read_bytes() == b"original"


def test_archive_rejects_protected_path_even_with_valid_digest(tmp_path):
    archive = tmp_path / "release.zip"
    content = b"do not overwrite"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps({"version": 1, "files": {"blue_runtime.py": sync.digest(content)}}))
        bundle.writestr("blue_runtime.py", content)
    with pytest.raises(ValueError, match="protected"):
        sync.read_bundle(archive, sync.digest(archive.read_bytes()))


def test_incomplete_blue_runtime_overlay_is_rejected(tmp_path):
    archive = tmp_path / "release.zip"
    name = "blue-runtime/blue_shared_crawl.py"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps({"version": 1, "files": {name: sync.digest(b"partial")}}))
        bundle.writestr(name, b"partial")
    with pytest.raises(ValueError, match="complete reviewed"):
        sync.read_bundle(archive, sync.digest(archive.read_bytes()))


def test_target_rejects_parent_symlink(tmp_path):
    app = tmp_path / "app"
    outside = tmp_path / "outside"
    app.mkdir()
    outside.mkdir()
    try:
        (app / "src").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable")
    with pytest.raises(ValueError, match="escapes|link"):
        sync.target_path(app, "src/takealot_ops/a.py")


@pytest.mark.parametrize("journal_state", ["acked", "pending", "blocked", "running"])
def test_overlay_preserves_checkpoint_runtime_and_previous_assets(tmp_path, monkeypatch, journal_state):
    root = tmp_path / "blue"
    app = root / "app"
    keep = {
        "app/logs/competitor-batch-queue.json": b'{"results":[{"plid":"123"}]}',
        "app/.venv/pyvenv.cfg": b"test-protected-runtime",
        "blue_runtime.py": b"fixed-blue-primary",
        "state/crawl-outbox/retained-envelope.json": b"unacknowledged-evidence",
        "app/frontend/competitor/dist/assets/old.js": b"old-tab-still-open",
    }
    for name, data in keep.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    name = "src/takealot_ops/erp/service.py"
    target = app / name
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous-source")
    archive = tmp_path / "release.zip"
    payload = {name: b"new-source", "frontend/competitor/dist/index.html": b"new-entry"}
    payload.update({sync.BLUE_PREFIX + name: b"reviewed-blue-code" for name in sync.BLUE_RUNTIME_FILES})
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps({"version": 1, "files": {
            key: sync.digest(value) for key, value in payload.items()}}))
        for key, value in payload.items():
            bundle.writestr(key, value)
    monkeypatch.setattr(sync, "BLUE_ROOT", root)
    monkeypatch.setitem(sync.sys.modules, "blue_node", SimpleNamespace(
        ROOT=root, config=lambda: {"node": "main"}))

    class StoppedSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, seconds):
            pass

        def connect_ex(self, address):
            return 1

    monkeypatch.setattr(sync.socket, "socket", StoppedSocket)
    journal = root / "state/crawl-outbox/journal.sqlite3"
    # Abrupt worker exit leaves committed WAL pages for the deploy reader to recover.
    subprocess.run([sys.executable, "-c", """
import os,sqlite3,sys
db=sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=WAL')
db.execute('CREATE TABLE attempts (id INTEGER PRIMARY KEY,state TEXT,payload TEXT)')
db.execute('INSERT INTO attempts VALUES(1,?,?)',(sys.argv[2],'retained-observation'))
db.commit()
os._exit(0)
""", str(journal), journal_state], check=True)
    if journal_state != "acked":
        with pytest.raises(RuntimeError, match="in-flight|undelivered"):
            sync.inspect_release(archive, sync.digest(archive.read_bytes()), apply=True)
        assert target.read_bytes() == b"previous-source"
        with sqlite3.connect(journal) as db:
            assert db.execute("SELECT state,payload FROM attempts").fetchall() == [
                (journal_state, "retained-observation")]
        return
    result = sync.inspect_release(archive, sync.digest(archive.read_bytes()), apply=True)
    assert result["applied"]
    assert target.read_bytes() == b"new-source"
    assert (Path(result["backup"]) / name).read_bytes() == b"previous-source"
    with sqlite3.connect(journal) as db:
        assert db.execute("SELECT state,payload FROM attempts").fetchall() == [
            ("acked", "retained-observation")]
    for filename in sync.BLUE_RUNTIME_FILES:
        assert (root / filename).read_bytes() == b"reviewed-blue-code"
    for key, value in keep.items():
        assert (root / key).read_bytes() == value
