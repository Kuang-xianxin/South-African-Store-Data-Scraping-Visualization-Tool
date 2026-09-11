"""Radar component caches retain fresh, permission-scoped source data."""
from datetime import date

import pytest
from sqlalchemy import insert

from takealot_ops.erp.live_updates import DataRevisionReader, VersionedReadProjectionCache
from takealot_ops.erp import service
from takealot_ops.storage.migrations import create_engine_for_database_url
from takealot_ops.storage.models import Base, ErpDataRevision


@pytest.mark.parametrize("prefix", ["radar-store-history-v1", "radar-official-sales-v1"])
def test_components_ignore_competitor_updates_but_rebuild_for_own_store_changes(tmp_path, prefix):
    engine = create_engine_for_database_url(f"sqlite:///{tmp_path / 'cache.db'}")
    Base.metadata.create_all(engine)
    reader = DataRevisionReader(engine, ttl_seconds=0)
    cache = VersionedReadProjectionCache(reader, ttl_seconds=600, max_entries=4)
    calls = []
    def load():
        calls.append(1)
        return len(calls)
    key = (prefix, ("store-a",), ("plid-1",), date(2026, 9, 7))
    assert cache.get_or_load(key, load) == 1
    with engine.begin() as c:
        c.execute(insert(ErpDataRevision), [
            dict(scope="*", topic="competitors", revision="new"),
            dict(scope="store-b", topic="store", revision="new"),
        ])
    assert cache.get_or_load(key, load) == 1
    with engine.begin() as c:
        c.execute(insert(ErpDataRevision).values(scope="store-a", topic="store", revision="changed"))
    assert cache.get_or_load(key, load) == 2
    assert cache.get_or_load((prefix, ("store-b",), ("plid-1",), date(2026, 9, 7)), load) == 3
    assert cache.get_or_load((prefix, ("store-a",), ("plid-2",), date(2026, 9, 7)), load) == 4
    assert cache.get_or_load((prefix, ("store-a",), ("plid-1",), date(2026, 9, 8)), load) == 5
    with engine.begin() as c:
        c.execute(insert(ErpDataRevision).values(scope="*", topic="users", revision="revoked"))
    assert cache.get_or_load(key, load) == 6
    engine.dispose()


def test_true_partition_reuses_cache_on_sales_change_but_tracks_membership(tmp_path):
    engine = create_engine_for_database_url(f"sqlite:///{tmp_path / 'cache.db'}")
    Base.metadata.create_all(engine)
    reader = DataRevisionReader(engine, ttl_seconds=0)
    key = ("competitors-list-v9", (), None, None, False)
    before = reader.cache_token(key)
    with engine.begin() as c:
        c.execute(insert(ErpDataRevision).values(scope="store-a", topic="store", revision="new"))
    assert reader.cache_token(key) == before
    with engine.begin() as c:
        c.execute(insert(ErpDataRevision).values(scope="*", topic="own-identities", revision="new"))
    assert reader.cache_token(key) != before
    engine.dispose()


@pytest.mark.parametrize("driver,scheme", [("pymysql", "mysql+pymysql"), ("mysqlclient", "mysql+mysqldb")])
def test_fast_driver_changes_only_read_engine_and_preserves_connection_options(monkeypatch, driver, scheme):
    seen = []
    monkeypatch.setenv("TAKEALOT_ERP_READ_DRIVER", driver)
    monkeypatch.setattr(service, "create_read_only_engine", lambda url: seen.append(url))
    url = "mysql+pymysql://fixture:fixture%40pass@127.0.0.1:3306/fixture?charset=utf8mb4"
    service.create_read_only_erp_engine(url)
    assert seen == [url.replace("mysql+pymysql", scheme)]
    seen.clear()
    service.create_read_only_erp_engine("sqlite:///:memory:")
    assert seen == ["sqlite:///:memory:"]


def test_sales_metadata_does_not_retain_card_fields_or_share_mutable_metrics(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from takealot_ops.erp import web
    from takealot_ops.erp.read_cache import ReadProjectionCache
    monkeypatch.setattr(web.DashboardSettings, "from_env", lambda _: SimpleNamespace(
        database_url=f"sqlite:///{tmp_path / 'missing.db'}",
    ))
    cache = ReadProjectionCache(ttl_seconds=20, max_entries=4)
    kwargs = dict(own_store_codes={"one"}, through=date(2026, 9, 7), metadata_cache=cache)
    first = web._own_store_sales_comparison_records(tmp_path, [{"plid": "1", "title": "old"}, {}], **kwargs)
    assert first[1]["自有官方销量店铺数"] == 0
    first[0]["自有官方销量"]["7"] = 999
    second = web._own_store_sales_comparison_records(tmp_path, [{"plid": "1", "title": "new"}], **kwargs)
    assert second[0]["title"] == "new"
    assert second[0]["自有官方销量"]["7"] is None
