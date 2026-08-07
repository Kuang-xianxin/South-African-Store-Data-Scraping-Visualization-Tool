from __future__ import annotations

from sqlalchemy import inspect

from takealot_ops.erp.permissions import (
    COMPETITORS_COLLECT,
    COMPETITORS_VIEW,
    DAILY_REPORT_MANAGE,
    DAILY_REPORT_VIEW,
    KEYWORD_TRAFFIC_MANAGE,
    LOGISTICS_MANAGE,
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
    assert LOGISTICS_MANAGE not in selection
    assert KEYWORD_TRAFFIC_MANAGE not in selection


def test_logistics_management_is_operator_admin_only_and_requires_store_view() -> None:
    assert LOGISTICS_MANAGE not in ROLE_PERMISSIONS["viewer"]
    assert LOGISTICS_MANAGE in ROLE_PERMISSIONS["operator"]
    assert LOGISTICS_MANAGE in ROLE_PERMISSIONS["admin"]
    assert normalize_permissions("viewer", [LOGISTICS_MANAGE]) == frozenset(
        {"store.view", LOGISTICS_MANAGE}
    )


def test_legacy_manual_keyword_permission_is_not_in_active_templates() -> None:
    assert KEYWORD_TRAFFIC_MANAGE not in ROLE_PERMISSIONS["viewer"]
    assert KEYWORD_TRAFFIC_MANAGE not in ROLE_PERMISSIONS["selection"]
    assert KEYWORD_TRAFFIC_MANAGE not in ROLE_PERMISSIONS["operator"]
    assert KEYWORD_TRAFFIC_MANAGE not in ROLE_PERMISSIONS["admin"]
    assert normalize_permissions("viewer", [KEYWORD_TRAFFIC_MANAGE]) == frozenset(
        {KEYWORD_TRAFFIC_MANAGE}
    )


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


def test_create_schema_adds_permissions_and_store_scope_to_legacy_users(
    tmp_path,
) -> None:
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
            connection.exec_driver_sql(
                "INSERT INTO erp_users (id, role) VALUES (1, 'admin')"
            )

        create_schema(engine)

        columns = {
            str(column["name"])
            for column in inspect(engine).get_columns("erp_users")
        }
        assert "permissions_json" in columns
        assert "store_access_all" in columns
        assert inspect(engine).has_table("erp_stores")
        assert inspect(engine).has_table("erp_user_stores")
        assert inspect(engine).has_table("logistics_shipment_links")
        assert inspect(engine).has_table("logistics_shipment_link_audits")
        assert inspect(engine).has_table("logistics_provider_snapshots")
        assert inspect(engine).has_table("platform_warehouse_drafts")
        assert inspect(engine).has_table("platform_warehouse_draft_lines")
        assert inspect(engine).has_table("platform_warehouse_draft_audits")
        assert inspect(engine).has_table("platform_warehouse_shipments")
        draft_columns = {
            str(column["name"])
            for column in inspect(engine).get_columns("platform_warehouse_drafts")
        }
        assert {
            "upstream_mode",
            "review_payload_hash",
            "review_approval_hash",
            "create_task_id",
            "last_error",
        } <= draft_columns
        assert not inspect(engine).has_table("product_keyword_snapshots")
        with engine.connect() as connection:
            default_store = connection.exec_driver_sql(
                "SELECT code, display_name, active, data_connected "
                "FROM erp_stores"
            ).one()
            legacy_scope = connection.exec_driver_sql(
                "SELECT store_access_all FROM erp_users WHERE id = 1"
            ).scalar_one()
        assert tuple(default_store) == ("current", "当前店铺", 1, 1)
        assert legacy_scope == 1
    finally:
        engine.dispose()
