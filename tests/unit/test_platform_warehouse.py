from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from takealot_ops.platform_warehouse import (
    PlatformWarehouseConflictError,
    PlatformWarehouseInputError,
    PlatformWarehouseService,
)
from takealot_ops.storage.migrations import (
    _add_platform_warehouse_upstream_columns,
    create_engine_for_database_url,
    create_schema,
)
from takealot_ops.storage.models import (
    OfferCurrent,
    PlatformWarehouseDraftAudit,
)


def _service(tmp_path, monkeypatch) -> tuple[PlatformWarehouseService, str]:
    database_path = tmp_path / "platform-warehouse.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("TAKEALOT_PORTAL_BFF_ENABLED", "false")
    engine = create_engine_for_database_url(database_url)
    try:
        create_schema(engine)
        with Session(engine) as session, session.begin():
            session.add(
                OfferCurrent(
                    offer_id="offer-1",
                    sku="SKU-1",
                    tsin_id="TSIN-1",
                    title="Warehouse product",
                    captured_at=datetime.now(UTC),
                    takealot_available_stock=8,
                    takealot_stock_on_way=2,
                    takealot_stock_in_receiving=1,
                )
            )
    finally:
        engine.dispose()
    return PlatformWarehouseService(tmp_path), database_url


def test_create_local_draft_freezes_offer_and_quantities(tmp_path, monkeypatch) -> None:
    service, database_url = _service(tmp_path, monkeypatch)

    draft = service.create_draft(
        [
            {
                "offer_id": "offer-1",
                "cpt_quantity": 3,
                "jhb_quantity": 4,
                "dbn_quantity": 0,
            }
        ],
        actor_user_id=None,
        actor_username="operator",
        note="local only",
    )

    assert draft["status"] == "draft"
    assert draft["quantity_totals"] == {
        "cpt_quantity": 3,
        "jhb_quantity": 4,
        "dbn_quantity": 0,
    }
    assert draft["lines"][0]["sku"] == "SKU-1"
    assert draft["audits"][0]["details"]["upstream_write"] is False

    overview = service.load()
    assert overview["capability"]["official_shipment_write_supported"] is False
    assert overview["drafts"][0]["draft_number"] == draft["draft_number"]

    engine = create_engine_for_database_url(database_url)
    try:
        with Session(engine) as session:
            actions = session.scalars(select(PlatformWarehouseDraftAudit.action)).all()
        assert actions == ["created"]
    finally:
        engine.dispose()


def test_draft_rejects_duplicate_or_empty_quantities(tmp_path, monkeypatch) -> None:
    service, _ = _service(tmp_path, monkeypatch)

    with pytest.raises(PlatformWarehouseInputError, match="至少填写一个仓库"):
        service.create_draft(
            [{"offer_id": "offer-1"}],
            actor_user_id=None,
            actor_username="operator",
        )

    with pytest.raises(PlatformWarehouseInputError, match="重复出现"):
        service.create_draft(
            [
                {"offer_id": "offer-1", "cpt_quantity": 1},
                {"offer_id": "offer-1", "jhb_quantity": 1},
            ],
            actor_user_id=None,
            actor_username="operator",
        )


def test_existing_platform_draft_table_gets_direct_request_idempotency_key(
    tmp_path,
) -> None:
    database_path = tmp_path / "platform-warehouse-migration.db"
    engine = create_engine_for_database_url(
        f"sqlite:///{database_path.as_posix()}"
    )
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE platform_warehouse_drafts ("
                "id INTEGER PRIMARY KEY, "
                "store_code VARCHAR(64) NOT NULL, "
                "draft_number VARCHAR(40) NOT NULL"
                ")"
            )
        _add_platform_warehouse_upstream_columns(engine)
        schema = inspect(engine)
        columns = {
            column["name"]
            for column in schema.get_columns("platform_warehouse_drafts")
        }
        indexes = {
            index["name"]: index
            for index in schema.get_indexes("platform_warehouse_drafts")
        }
        assert "client_request_id" in columns
        request_index = indexes["uq_platform_warehouse_draft_store_request"]
        assert request_index["unique"] == 1
        assert request_index["column_names"] == ["store_code", "client_request_id"]
    finally:
        engine.dispose()

def test_manual_lifecycle_is_ordered_and_audited(tmp_path, monkeypatch) -> None:
    service, _ = _service(tmp_path, monkeypatch)
    draft = service.create_draft(
        [{"offer_id": "offer-1", "jhb_quantity": 5}],
        actor_user_id=None,
        actor_username="operator",
    )

    with pytest.raises(PlatformWarehouseConflictError, match="不能执行确认已发货"):
        service.confirm_shipped(
            draft["id"],
            tracking_reference="TRACK-1",
            actor_user_id=None,
            actor_username="operator",
        )

    po = service.confirm_po(
        draft["id"],
        po_number="PO-100",
        platform_shipment_id=123,
        actor_user_id=None,
        actor_username="operator",
    )
    shipped = service.confirm_shipped(
        draft["id"],
        tracking_reference="TRACK-1",
        actor_user_id=None,
        actor_username="operator",
    )
    archived = service.archive(
        draft["id"],
        actor_user_id=None,
        actor_username="operator",
        note="documents checked",
    )

    assert po["status"] == "po_confirmed"
    assert shipped["status"] == "shipped"
    assert archived["status"] == "archived"
    assert [audit["action"] for audit in archived["audits"]] == [
        "archive",
        "confirm_shipped",
        "confirm_po",
        "created",
    ]
