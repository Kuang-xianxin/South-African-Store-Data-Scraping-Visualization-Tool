from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from takealot_ops.competitors.auto_tracking import (
    record_automatic_follower_attempt,
    select_automatic_follower_targets,
)
from takealot_ops.competitors.own_store import (
    connected_store_plids,
    is_connected_store_plid,
)
from takealot_ops.competitors.service import CompetitorCollectionResult
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import (
    ErpStore,
    OfferCurrent,
    OwnStoreFollowerTracking,
)
from takealot_ops.storage.store_context import store_scope


def test_automatic_targets_cover_connected_stores_dedupe_and_rotate() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    now = datetime(2026, 8, 4, 8, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                ErpStore(
                    code="alpha",
                    display_name="Alpha",
                    active=True,
                    data_connected=True,
                    created_at=now,
                    updated_at=now,
                ),
                ErpStore(
                    code="beta",
                    display_name="Beta",
                    active=True,
                    data_connected=True,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
    with store_scope("alpha"):
        with Session(engine) as session, session.begin():
            session.add_all(
                [
                    OfferCurrent(
                        offer_id="a-1",
                        productline_id="100",
                        status="disabled_by_seller",
                        captured_at=now,
                    ),
                    OfferCurrent(
                        offer_id="a-2",
                        productline_id="200",
                        status="buyable",
                        captured_at=now,
                    ),
                ]
            )
    with store_scope("beta"):
        with Session(engine) as session, session.begin():
            session.add_all(
                [
                    OfferCurrent(
                        offer_id="b-1",
                        productline_id="200",
                        status="disabled_by_takealot",
                        captured_at=now,
                    ),
                    OfferCurrent(
                        offer_id="b-2",
                        productline_id="300",
                        status="not_buyable",
                        captured_at=now,
                    ),
                ]
            )
    with Session(engine) as session, session.begin():
        session.add(
            OwnStoreFollowerTracking(
                plid="200",
                last_attempted_at=now - timedelta(days=1),
                last_succeeded_at=now - timedelta(days=1),
                last_status="success",
                consecutive_failures=0,
                last_message="ok",
            )
        )

    available, targets = select_automatic_follower_targets(engine, max_targets=3)
    all_available, all_targets = select_automatic_follower_targets(
        engine,
        max_targets=None,
    )
    engine.dispose()

    assert available == 3
    assert all_available == 3
    assert len(all_targets) == 3
    assert [target.plid for target in targets] == ["100", "300", "200"]
    # Disabled own Offers still identify PLIDs that must remain in follower monitoring.
    assert {target.plid for target in all_targets} == {"100", "200", "300"}
    assert targets[2].store_codes == ("alpha", "beta")


def test_connected_store_plid_queries_keep_exact_store_membership() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    now = datetime(2026, 8, 20, 8, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                ErpStore(
                    code="alpha",
                    display_name="Alpha",
                    active=True,
                    data_connected=True,
                    created_at=now,
                    updated_at=now,
                ),
                ErpStore(
                    code="beta",
                    display_name="Beta",
                    active=True,
                    data_connected=True,
                    created_at=now,
                    updated_at=now,
                ),
                ErpStore(
                    code="offline",
                    display_name="Offline",
                    active=True,
                    data_connected=False,
                    created_at=now,
                    updated_at=now,
                ),
                ErpStore(
                    code="inactive",
                    display_name="Inactive",
                    active=False,
                    data_connected=True,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
    for store_code, offer_id, plid in (
        ("alpha", "alpha-1", "100"),
        ("beta", "beta-1", " 200 "),
        ("offline", "offline-1", "300"),
        ("inactive", "inactive-1", "400"),
    ):
        with store_scope(store_code), Session(engine) as session, session.begin():
            session.add(
                OfferCurrent(
                    offer_id=offer_id,
                    productline_id=plid,
                    captured_at=now,
                )
            )

    with Session(engine) as session:
        assert connected_store_plids(session) == {"100", "200"}
        assert is_connected_store_plid(session, "100") is True
        assert is_connected_store_plid(session, " 200 ") is True
        assert is_connected_store_plid(session, "300") is False
        assert is_connected_store_plid(session, "400") is False
        assert is_connected_store_plid(session, "") is False
    engine.dispose()


def test_automatic_attempt_state_keeps_partial_snapshot_success() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    attempted_at = datetime(2026, 8, 4, 8, tzinfo=UTC)
    result = CompetitorCollectionResult(
        plid="123",
        title="Example",
        succeeded=False,
        message="公开报价已保存，但库存未探测",
        retryable=True,
        failure_kind="stock-unprobed",
    )

    status = record_automatic_follower_attempt(
        engine,
        plid="123",
        attempted_at=attempted_at,
        result=result,
    )
    with Session(engine) as session:
        state = session.scalar(
            select(OwnStoreFollowerTracking).where(
                OwnStoreFollowerTracking.plid == "123"
            )
        )
    engine.dispose()

    assert status == "partial"
    assert state is not None
    assert state.last_succeeded_at == attempted_at.replace(tzinfo=None)
    assert state.consecutive_failures == 0
