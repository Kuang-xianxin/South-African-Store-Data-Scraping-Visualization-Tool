"""Multi-store credential and ORM isolation coverage."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from takealot_ops.settings import Settings, SettingsError, configured_stores
from takealot_ops.storage.migrations import create_engine_for_database_url, create_schema
from takealot_ops.storage.models import OfferCurrent, OfferSnapshot
from takealot_ops.storage.store_context import store_scope


def test_store_scoped_records_are_automatically_isolated(tmp_path: Path) -> None:
    engine = create_engine_for_database_url(f"sqlite:///{tmp_path / 'stores.db'}")
    create_schema(engine)
    captured_at = datetime(2026, 8, 4, tzinfo=UTC)
    try:
        with store_scope("current"), Session(engine) as session, session.begin():
            session.add(OfferCurrent(offer_id="current-offer", captured_at=captured_at))
            session.add(
                OfferSnapshot(
                    snapshot_date=date(2026, 8, 4),
                    offer_id="shared-offer",
                    captured_at=captured_at,
                )
            )
        with store_scope("shop-02"), Session(engine) as session, session.begin():
            session.add(OfferCurrent(offer_id="shop-02-offer", captured_at=captured_at))
            session.add(
                OfferSnapshot(
                    snapshot_date=date(2026, 8, 4),
                    offer_id="shared-offer",
                    captured_at=captured_at,
                )
            )

        with store_scope("current"), Session(engine) as session:
            assert list(session.scalars(select(OfferCurrent.offer_id))) == [
                "current-offer"
            ]
            assert len(list(session.scalars(select(OfferSnapshot)))) == 1

        with store_scope("shop-02"), Session(engine) as session, session.begin():
            assert list(session.scalars(select(OfferCurrent.offer_id))) == [
                "shop-02-offer"
            ]
            session.execute(delete(OfferSnapshot))

        with store_scope("current"), Session(engine) as session:
            assert len(list(session.scalars(select(OfferSnapshot)))) == 1
        with store_scope("shop-02"), Session(engine) as session:
            assert list(session.scalars(select(OfferSnapshot))) == []
    finally:
        engine.dispose()


def test_configured_store_registry_selects_credentials_and_rejects_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TAKEALOT_STORES",
        "current|店铺 1|STORE_KEY_1;shop-02|店铺 2|STORE_KEY_2",
    )
    monkeypatch.setenv("STORE_KEY_1", "key-one")
    monkeypatch.setenv("STORE_KEY_2", "key-two")
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", "sqlite:///:memory:")
    stores = configured_stores(tmp_path)
    assert [(store.code, store.display_name) for store in stores] == [
        ("current", "店铺 1"),
        ("shop-02", "店铺 2"),
    ]
    assert Settings.from_env(tmp_path, "shop-02").api_key == "key-two"

    monkeypatch.setenv("STORE_KEY_2", "key-one")
    with pytest.raises(SettingsError, match="重复"):
        configured_stores(tmp_path)
