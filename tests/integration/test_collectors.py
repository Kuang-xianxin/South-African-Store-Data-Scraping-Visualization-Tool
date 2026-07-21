from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from takealot_ops.collectors import CollectionResult, collect_offers, collect_sales
from takealot_ops.domain import OfferRecord
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import CollectionRun, OfferCurrent, OfferSnapshot, SaleItem
from takealot_ops.storage.repository import Repository


FIXTURES = Path(__file__).parents[1] / "fixtures"


class FakeClient:
    def __init__(self, items: list[dict[str, Any]], failure_after: int | None = None) -> None:
        self.items = items
        self.failure_after = failure_after
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def iter_items(
        self, path: str, params: Mapping[str, Any]
    ) -> Iterator[dict[str, Any]]:
        self.calls.append((path, dict(params)))
        for index, item in enumerate(self.items):
            if self.failure_after == index:
                raise RuntimeError("page failed with top-secret-token")
            yield item


class FailingOfferRepository(Repository):
    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self.upserts = 0

    def upsert_offer_snapshot(self, record: OfferRecord, snapshot_date: date) -> None:
        super().upsert_offer_snapshot(record, snapshot_date)
        self.upserts += 1
        if self.upserts == 2:
            raise RuntimeError("write failed with top-secret-token")


def _engine() -> Engine:
    engine = create_engine("sqlite://")
    create_schema(engine)
    return engine


def _items(fixture_name: str) -> list[dict[str, Any]]:
    payload = json.loads((FIXTURES / fixture_name).read_text(encoding="utf-8"))
    return payload["items"]


def test_collect_offers_persists_complete_snapshot_and_successful_run() -> None:
    engine = _engine()
    captured_at = datetime(2026, 7, 20, 8, tzinfo=UTC)
    items = _items("offers_page_1.json") + _items("offers_page_2.json")
    items[0]["total_stock"] = 0
    client = FakeClient(items)
    with Session(engine) as session:
        result = collect_offers(client, Repository(session), captured_at)

    with Session(engine) as session:
        current = session.scalars(select(OfferCurrent).order_by(OfferCurrent.offer_id)).all()
        snapshots = session.scalars(
            select(OfferSnapshot).order_by(OfferSnapshot.offer_id)
        ).all()
        run = session.get(CollectionRun, result.run_id)

    assert isinstance(result, CollectionResult)
    assert result.status == "success"
    assert result.counts == {"records": 2}
    assert client.calls == [("/offers", {"limit": 100})]
    assert [row.offer_id for row in current] == ["100001", "100002"]
    assert [row.snapshot_date for row in snapshots] == [date(2026, 7, 20)] * 2
    assert snapshots[0].total_stock == 0
    assert snapshots[1].total_stock is None
    assert run is not None
    assert run.status == "success"
    assert run.counts == {"records": 2}
    assert run.scope_date == date(2026, 7, 20)


def test_collect_offers_replaces_same_day_snapshot_and_current_offer_set() -> None:
    engine = _engine()
    captured_at = datetime(2026, 7, 20, 8, tzinfo=UTC)
    both_items = _items("offers_page_1.json") + _items("offers_page_2.json")
    with Session(engine) as session:
        collect_offers(FakeClient(both_items), Repository(session), captured_at)
        collect_offers(FakeClient([both_items[0]]), Repository(session), captured_at)

    with Session(engine) as session:
        current_ids = session.scalars(select(OfferCurrent.offer_id)).all()
        snapshot_ids = session.scalars(select(OfferSnapshot.offer_id)).all()

    assert current_ids == ["100001"]
    assert snapshot_ids == ["100001"]


def test_collect_offers_rolls_back_partial_pages_and_persists_sanitized_failure() -> None:
    engine = _engine()
    client = FakeClient(
        _items("offers_page_1.json") + _items("offers_page_2.json"), failure_after=1
    )
    with Session(engine) as session:
        result = collect_offers(
            client, Repository(session), datetime(2026, 7, 20, 8, tzinfo=UTC)
        )

    with Session(engine) as session:
        offers = session.scalars(select(OfferCurrent)).all()
        run = session.get(CollectionRun, result.run_id)

    assert result.status == "failed"
    assert offers == []
    assert run is not None
    assert run.status == "failed"
    assert run.counts == {"records": 0}
    assert run.error is not None
    assert "top-secret-token" not in run.error
    assert "top-secret-token" not in (result.error or "")


def test_collect_offers_rolls_back_mutations_before_finishing_failed_run() -> None:
    engine = _engine()
    client = FakeClient(_items("offers_page_1.json") + _items("offers_page_2.json"))
    with Session(engine) as session:
        result = collect_offers(
            client,
            FailingOfferRepository(session),
            datetime(2026, 7, 20, 8, tzinfo=UTC),
        )

    with Session(engine) as session:
        current = session.scalars(select(OfferCurrent)).all()
        snapshots = session.scalars(select(OfferSnapshot)).all()
        run = session.get(CollectionRun, result.run_id)

    assert current == []
    assert snapshots == []
    assert run is not None
    assert run.status == "failed"
    assert run.counts == {"records": 0}
    assert "top-secret-token" not in (run.error or "")


def test_collect_sales_uses_exact_inclusive_params_and_preserves_raw_payload() -> None:
    engine = _engine()
    raw_item = _items("sales_page.json")[0]
    client = FakeClient([raw_item])
    start = date(2026, 7, 1)
    end = date(2026, 7, 20)
    with Session(engine) as session:
        result = collect_sales(client, Repository(session), start, end)

    with Session(engine) as session:
        sale = session.get(SaleItem, "12345678")
        run = session.get(CollectionRun, result.run_id)

    assert client.calls == [
        (
            "/sales",
            {
                "order_date__gte": "2026-07-01",
                "order_date__lte": "2026-07-20",
                "limit": 100,
            },
        )
    ]
    assert sale is not None
    assert sale.raw_payload == raw_item
    assert run is not None
    assert run.status == "success"
    assert run.counts == {"records": 1}


def test_collect_sales_converts_all_items_before_atomic_mutation() -> None:
    engine = _engine()
    valid = _items("sales_page.json")[0]
    invalid = dict(valid, order_item_id=999, quantity="top-secret-token")
    client = FakeClient([valid, invalid])
    with Session(engine) as session:
        result = collect_sales(
            client, Repository(session), date(2026, 7, 1), date(2026, 7, 20)
        )

    with Session(engine) as session:
        sales = session.scalars(select(SaleItem)).all()
        run = session.get(CollectionRun, result.run_id)

    assert result.status == "failed"
    assert sales == []
    assert run is not None
    assert run.status == "failed"
    assert "top-secret-token" not in (run.error or "")


def test_repository_public_transaction_boundary_commits_and_rolls_back_runs() -> None:
    engine = _engine()
    with Session(engine) as session:
        repository = Repository(session)
        with repository.transaction():
            committed_run = repository.begin_run("committed")

        try:
            with repository.transaction():
                rolled_back_run = repository.begin_run("rolled-back")
                raise RuntimeError("stop")
        except RuntimeError:
            pass

    with Session(engine) as session:
        assert session.get(CollectionRun, committed_run) is not None
        assert session.get(CollectionRun, rolled_back_run) is None
