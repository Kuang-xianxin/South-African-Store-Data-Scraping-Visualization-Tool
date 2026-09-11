"""Consistent SQLite copies for isolated preparation and stopped-main install."""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path


def copy_radar_cache(source: Path, destination: Path, namespace: str) -> list[str]:
    if not re.fullmatch(r"[a-f0-9]{64}", namespace):
        raise ValueError("Invalid radar code namespace")
    source, destination = source.resolve(), destination.resolve()
    if source == destination:
        raise ValueError("Preparation and live cache directories must be different")
    destination.mkdir(parents=True, exist_ok=True)
    copied = []
    for partition in ("true", "own"):
        name = f"radar-{partition}-{namespace}.sqlite3"
        original, target = source / name, destination / name
        if not original.is_file():
            continue
        temporary = target.with_suffix(".release-copy")
        with closing(sqlite3.connect(original.as_uri() + "?mode=ro", uri=True, timeout=30)) as reader:
            with closing(sqlite3.connect(temporary)) as writer:
                reader.backup(writer)
                if writer.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise RuntimeError("Invalid prepared cache copy")
                writer.execute("PRAGMA journal_mode=DELETE")
                writer.commit()
        # Caller owns an idle destination. The formal restart installs only
        # after the old main process exits; never replace its live WAL files.
        for suffix in ("-wal", "-shm"):
            target.with_name(target.name + suffix).unlink(missing_ok=True)
        temporary.replace(target)
        copied.append(name)
    return copied
