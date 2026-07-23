"""One-time, audited migration from the retired SQLite runtime to local MySQL."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, func, select

from takealot_ops.storage.migrations import (
    create_engine_for_database_url,
    create_schema,
)
from takealot_ops.storage.models import Base


@dataclass(frozen=True)
class MySQLMigrationReport:
    """Verified source and target row counts for every application table."""

    source_path: Path
    table_counts: dict[str, int]

    @property
    def total_rows(self) -> int:
        return sum(self.table_counts.values())


def migrate_sqlite_to_mysql(
    source_path: Path,
    mysql_database_url: str,
    *,
    batch_size: int = 500,
) -> MySQLMigrationReport:
    """Copy every SQLAlchemy table into an empty MySQL schema and verify counts."""
    source = source_path.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source}")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    source_engine = create_engine_for_database_url(
        f"sqlite:///{source.as_posix()}"
    )
    target_engine = create_engine_for_database_url(mysql_database_url)
    if target_engine.dialect.name != "mysql":
        source_engine.dispose()
        target_engine.dispose()
        raise ValueError("migration target must use MySQL")

    try:
        create_schema(target_engine)
        source_counts = _table_counts(source_engine)
        target_counts = _table_counts(target_engine)
        nonempty = {
            table_name: count
            for table_name, count in target_counts.items()
            if count > 0
        }
        if nonempty:
            rendered = ", ".join(
                f"{table_name}={count}"
                for table_name, count in sorted(nonempty.items())
            )
            raise ValueError(f"MySQL target is not empty: {rendered}")

        with source_engine.connect() as source_connection:
            with target_engine.begin() as target_connection:
                for table in Base.metadata.sorted_tables:
                    result = source_connection.execute(select(table)).mappings()
                    batch: list[dict[str, object]] = []
                    for row in result:
                        batch.append(dict(row))
                        if len(batch) >= batch_size:
                            target_connection.execute(table.insert(), batch)
                            batch.clear()
                    if batch:
                        target_connection.execute(table.insert(), batch)

        migrated_counts = _table_counts(target_engine)
        if migrated_counts != source_counts:
            raise RuntimeError(
                "MySQL row-count verification failed after migration"
            )
        return MySQLMigrationReport(
            source_path=source,
            table_counts=source_counts,
        )
    finally:
        source_engine.dispose()
        target_engine.dispose()


def _table_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table.name: int(
                connection.execute(
                    select(func.count()).select_from(table)
                ).scalar_one()
            )
            for table in Base.metadata.sorted_tables
        }
