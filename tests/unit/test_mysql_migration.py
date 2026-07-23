from __future__ import annotations

from pathlib import Path

import pytest

from takealot_ops.storage.mysql_migration import migrate_sqlite_to_mysql


def test_mysql_migration_requires_existing_sqlite_source(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        migrate_sqlite_to_mysql(
            tmp_path / "missing.db",
            "mysql+pymysql://user:secret@127.0.0.1/takealot_ops",
        )


def test_mysql_migration_rejects_non_mysql_target(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    source.touch()

    with pytest.raises(ValueError, match="target must use MySQL"):
        migrate_sqlite_to_mysql(
            source,
            f"sqlite:///{(tmp_path / 'target.db').as_posix()}",
        )
