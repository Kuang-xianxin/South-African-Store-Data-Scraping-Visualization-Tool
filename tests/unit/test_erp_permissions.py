from __future__ import annotations

from sqlalchemy import inspect

from takealot_ops.erp.permissions import (
    COMPETITORS_COLLECT,
    COMPETITORS_VIEW,
    DAILY_REPORT_MANAGE,
    DAILY_REPORT_VIEW,
    ROLE_PERMISSIONS,
    normalize_permissions,
    permissions_from_storage,
    permissions_to_storage,
)
from takealot_ops.storage.migrations import (
    create_engine_for_database_url,
    create_schema,
)


def test_selection_template_can_collect_competitors_but_not_manage_daily_report() -> None:
    selection = ROLE_PERMISSIONS["selection"]

    assert COMPETITORS_VIEW in selection
    assert COMPETITORS_COLLECT in selection
    assert DAILY_REPORT_VIEW in selection
    assert DAILY_REPORT_MANAGE not in selection


def test_custom_permissions_add_dependencies_and_only_store_differences() -> None:
    customized = normalize_permissions("selection", [DAILY_REPORT_MANAGE])

    assert customized == frozenset({DAILY_REPORT_VIEW, DAILY_REPORT_MANAGE})
    encoded = permissions_to_storage("selection", customized)
    assert encoded is not None
    assert permissions_from_storage("selection", encoded) == customized
    assert permissions_to_storage(
        "selection",
        ROLE_PERMISSIONS["selection"],
    ) is None


def test_create_schema_adds_permissions_to_legacy_user_table(tmp_path) -> None:
    database_path = tmp_path / "legacy-permissions.db"
    engine = create_engine_for_database_url(
        f"sqlite:///{database_path.as_posix()}"
    )
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE erp_users ("
                "id INTEGER PRIMARY KEY, "
                "role VARCHAR(20) NOT NULL"
                ")"
            )

        create_schema(engine)

        columns = {
            str(column["name"])
            for column in inspect(engine).get_columns("erp_users")
        }
        assert "permissions_json" in columns
    finally:
        engine.dispose()
