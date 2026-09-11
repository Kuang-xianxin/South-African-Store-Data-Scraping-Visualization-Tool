from datetime import datetime
import pytest

from sqlalchemy import event, insert, select, text, update
from sqlalchemy.orm import Session

from takealot_ops.erp.live_updates import DataRevisionReader, VersionedReadProjectionCache
from takealot_ops.storage.migrations import create_engine_for_database_url
from takealot_ops.storage.models import Base, CollectionRun, CompetitorTarget, ErpDataRevision, ErpStore, OfferCurrent


def test_radar_preview_boundary_ignores_logins_but_changes_with_effective_permissions_and_ownership(tmp_path):
    engine = create_engine_for_database_url(f"sqlite+pysqlite:///{tmp_path / 'permissions.db'}")
    Base.metadata.create_all(engine)
    reader = DataRevisionReader(engine, ttl_seconds=0)
    try:
        first = reader.radar_access_token(("one",), permissions="verified-user1-store1")
        with engine.begin() as connection:
            connection.execute(insert(ErpDataRevision).values(scope="*", topic="users", revision="another-login"))
        assert reader.radar_access_token(("one",), permissions="verified-user1-store1") == first
        assert reader.radar_access_token(("one",), permissions="verified-user1-store2") != first
        with engine.begin() as connection:
            connection.execute(insert(ErpDataRevision).values(scope="*", topic="own-identities", revision="new-owned-product"))
            connection.execute(insert(OfferCurrent.__table__).values(
                store_code="one", offer_id="1", productline_id="100", captured_at=datetime(2026, 9, 7),
            ))
        assert reader.radar_access_token(("one",), permissions="verified-user1-store1") != first
    finally:
        engine.dispose()


def test_radar_preview_survives_stock_updates_but_not_actual_global_ownership_changes(tmp_path):
    from takealot_ops.erp.radar_page_cache import RadarPageCache

    engine = create_engine_for_database_url(f"sqlite+pysqlite:///{tmp_path / 'ownership.db'}")
    Base.metadata.create_all(engine)
    reader = DataRevisionReader(engine, ttl_seconds=0)
    pages = RadarPageCache()
    try:
        with engine.begin() as connection:
            connection.execute(insert(OfferCurrent.__table__).values(
                store_code="other", offer_id="1", productline_id="100", sku="A", tsin_id="T1",
                total_stock=5, selling_price=100, captured_at=datetime(2026, 9, 7),
            ))
            connection.execute(insert(ErpStore.__table__).values(
                id=1, code="other", display_name="Other", active=True, data_connected=True,
                created_at=datetime(2026, 9, 7), updated_at=datetime(2026, 9, 7),
            ))
        first = reader.radar_access_token(("one",), permissions="same-user")
        version = reader.cache_token(("competitors-list-v9", (), None, None, False))
        original, _ = pages.get_or_load("list", version=version, boundary=first, loader=lambda: {"stock": 5})
        with engine.begin() as connection:
            connection.execute(update(OfferCurrent.__table__).values(total_stock=3, selling_price=120))
        assert reader.cache_token(("competitors-list-v9", (), None, None, False)) != version
        assert reader.radar_access_token(("one",), permissions="same-user") == first
        new_version = reader.cache_token(("competitors-list-v9", (), None, None, False))
        preview, refreshing = pages.get_or_load("list", version=new_version, boundary=first,
            loader=lambda: {"stock": 3}, prefer_cached=True)
        assert preview is original and refreshing
        fresh, refreshing = pages.get_or_load("list", version=new_version, boundary=first,
            loader=lambda: {"stock": 3})
        assert fresh.body == b'{"stock":3}' and not refreshing
        # Another store's PLID/variant identity must invalidate the public partition.
        for field, value in [("productline_id", "200"), ("sku", "B"), ("tsin_id", "T2")]:
            with engine.begin() as connection:
                connection.execute(update(OfferCurrent.__table__).values({field: value}))
            next_token = reader.radar_access_token(("one",), permissions="same-user")
            assert next_token != first
            first = next_token
        with engine.begin() as connection:
            connection.execute(update(ErpStore.__table__).values(active=False))
        assert reader.radar_access_token(("one",), permissions="same-user") != first
    finally:
        pages.close()
        engine.dispose()


@pytest.mark.parametrize(("prefix", "topic"), [
    ("logistics-overview-v1", "logistics"),
    ("logistics-overview-v1", "store"),
    ("anomaly-products-v1", "competitors"),
    ("search-ranking-list-v1", "search"),
])
def test_module_projection_cache_reuses_and_invalidates_only_relevant_sources(tmp_path, prefix, topic):
    engine = create_engine_for_database_url(f"sqlite+pysqlite:///{tmp_path / 'cache.db'}")
    Base.metadata.create_all(engine)
    reader = DataRevisionReader(engine, ttl_seconds=0)
    cache = VersionedReadProjectionCache(reader, ttl_seconds=180, max_entries=12)
    calls = []
    def load():
        calls.append(1)
        return len(calls)
    key = (prefix, "all", ("one",))
    assert cache.get_or_load(key, load) == 1
    assert cache.get_or_load(key, load) == 1
    with engine.begin() as connection:
        connection.execute(insert(ErpDataRevision).values(scope="two", topic=topic, revision="a"))
    assert cache.get_or_load(key, load) == 1
    with engine.begin() as connection:
        connection.execute(insert(ErpDataRevision).values(scope="one", topic=topic, revision="b"))
    assert cache.get_or_load(key, load) == 2
    assert cache.get_or_load((prefix, "current", ("one",)), load) == 3
    engine.dispose()


def test_changes_commit_atomically_and_rollbacks_do_not_publish(tmp_path):
    engine = create_engine_for_database_url(f"sqlite+pysqlite:///{tmp_path / 'data.db'}")
    Base.metadata.create_all(engine)
    with engine.connect() as writer:
        tx = writer.begin()
        writer.execute(insert(CompetitorTarget).values(plid="1", url="u", active=True, created_at=datetime.now(), updated_at=datetime.now()))
        with engine.connect() as reader:
            assert not reader.execute(select(ErpDataRevision)).all()
        tx.commit()
    revisions = DataRevisionReader(engine, ttl_seconds=0)
    first = revisions.token("competitors", [])
    with engine.connect() as writer:
        writer.execute(update(CompetitorTarget).values(url="rolled back"))
        writer.rollback()
    assert revisions.token("competitors", []) == first
    with Session(engine) as session, session.begin():
        target = session.get(CompetitorTarget, "1")
        target.url = "committed"
    assert revisions.token("competitors", []) != first
    engine.dispose()


def test_many_rows_only_emit_one_revision_upsert_per_transaction(tmp_path):
    engine = create_engine_for_database_url(f"sqlite+pysqlite:///{tmp_path / 'data.db'}")
    Base.metadata.create_all(engine)
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, u, s, p, x, m: statements.append(s))
    with engine.begin() as connection:
        for n in range(30):
            connection.execute(insert(CompetitorTarget).values(plid=str(n), url="u", active=True, created_at=datetime.now(), updated_at=datetime.now()))
    assert sum(s.startswith("INSERT INTO erp_data_revisions") for s in statements) == 1
    with engine.connect() as connection:
        assert len(connection.execute(select(ErpDataRevision)).all()) == 1
    engine.dispose()


def test_savepoint_rollback_removes_only_nested_changes(tmp_path):
    engine = create_engine_for_database_url(f"sqlite+pysqlite:///{tmp_path / 'data.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(insert(CompetitorTarget).values(plid="1", url="u", active=True, created_at=datetime.now(), updated_at=datetime.now()))
        nested = connection.begin_nested()
        connection.execute(text("INSERT INTO erp_stores (code, display_name, active, data_connected, created_at, updated_at) VALUES ('x','x',1,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        nested.rollback()
    with engine.connect() as connection:
        assert connection.execute(select(ErpDataRevision.topic)).scalars().all() == ["competitors"]
    engine.dispose()


def test_revision_reader_filters_stores_and_domains_and_shares_reads(tmp_path):
    engine = create_engine_for_database_url(f"sqlite+pysqlite:///{tmp_path / 'data.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(insert(ErpDataRevision), [
            dict(scope="one", topic="store", revision="a"),
            dict(scope="two", topic="store", revision="b"),
            dict(scope="*", topic="competitors", revision="c"),
        ])
    reader = DataRevisionReader(engine)
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, u, s, p, x, m: statements.append(s))
    first = reader.token("overview", ["one"])
    competitor = reader.token("competitors", ["one"])
    for _ in range(20):
        assert reader.token("overview", ["one"]) == first
    assert len(statements) == 1
    with engine.begin() as connection:
        connection.execute(update(ErpDataRevision).where(ErpDataRevision.scope == "two").values(revision="new"))
    reader.invalidate()
    assert reader.token("overview", ["one"]) == first
    assert reader.token("competitors", ["one"]) == competitor
    with engine.begin() as connection:
        connection.execute(update(ErpDataRevision).where(ErpDataRevision.topic == "competitors").values(revision="new"))
    reader.invalidate()
    assert reader.token("overview", ["one"]) == first
    assert reader.token("competitors", ["one"]) != competitor
    engine.dispose()


def test_longer_cache_lifetime_requires_registered_data_versions(tmp_path, monkeypatch):
    engine = create_engine_for_database_url(f"sqlite+pysqlite:///{tmp_path / 'ttl.db'}")
    Base.metadata.create_all(engine)
    cache = VersionedReadProjectionCache(DataRevisionReader(engine), ttl_seconds=180, max_entries=4)
    now = [0.0]
    monkeypatch.setattr(cache, "_clock", lambda: now[0])
    calls = []
    def load():
        calls.append(1)
        return len(calls)
    assert cache.get_or_load(("logistics-overview-v1", "one"), load) == 1
    assert cache.get_or_load(("unregistered", "one"), load) == 2
    now[0] = 21
    assert cache.get_or_load(("logistics-overview-v1", "one"), load) == 1
    assert cache.get_or_load(("unregistered", "one"), load) == 3
    now[0] = 181
    assert cache.get_or_load(("logistics-overview-v1", "one"), load) == 4
    engine.dispose()


def test_store_markers_use_written_store_and_selective_cache_versions(tmp_path):
    engine = create_engine_for_database_url(f"sqlite+pysqlite:///{tmp_path / 'data.db'}")
    Base.metadata.create_all(engine)
    reader = DataRevisionReader(engine, ttl_seconds=0)
    cache = VersionedReadProjectionCache(reader, ttl_seconds=60, max_entries=12)
    calls = []

    def load():
        calls.append(1)
        return len(calls)

    assert cache.get_or_load(("store-summary-v2", "one"), load) == 1
    assert cache.get_or_load(("store-summary-v2", "two"), load) == 2
    with engine.begin() as connection:
        connection.execute(insert(CollectionRun).values(
            store_code="two", run_id="two-run", run_type="offers", started_at=datetime.now(),
        ))
    with engine.connect() as connection:
        assert connection.execute(select(ErpDataRevision.scope, ErpDataRevision.topic)).all() == [("two", "store")]
    assert cache.get_or_load(("store-summary-v2", "one"), load) == 1
    assert cache.get_or_load(("store-summary-v2", "two"), load) == 3
    engine.dispose()
