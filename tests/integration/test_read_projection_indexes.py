from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect

from takealot_ops.storage.migrations import create_schema


def test_read_projection_indexes_exist_and_schema_upgrade_is_idempotent(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "projection-indexes.db"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")

    create_schema(engine)
    create_schema(engine)

    schema = inspect(engine)
    expected = {
        "collection_runs": ("store_code", "run_type", "status", "scope_date"),
        "offer_current": ("store_code", "productline_id"),
        "store_offer_observations": (
            "store_code",
            "productline_id",
            "captured_at",
        ),
        "return_items": ("store_code", "return_date"),
        "daily_product_metrics": ("store_code", "offer_id", "metric_date"),
    }
    for table_name, columns in expected.items():
        actual = {
            tuple(index.get("column_names") or ())
            for index in schema.get_indexes(table_name)
        }
        assert columns in actual

    engine.dispose()
