from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from takealot_ops.search_ranking.service import (
    KeywordCandidate,
    SearchRankingRuntimeSettings,
    SearchRankingService,
    VisionCallResult,
    VisionProfile,
    _title_validation,
)
from takealot_ops.erp.web import create_app
from takealot_ops.storage.migrations import (
    create_engine_for_database_url,
    create_schema,
)
from takealot_ops.storage.models import OfferCurrent


class FakeVisionClient:
    calls = 0

    def __init__(self, _: SearchRankingRuntimeSettings) -> None:
        pass

    async def identify(
        self,
        *,
        image_url: str,
        title: str,
        sku: str | None,
    ) -> VisionCallResult:
        del image_url, title, sku
        type(self).calls += 1
        return VisionCallResult(
            profile=VisionProfile(
                product_name="Rechargeable wireless mouse",
                category="Computer mice",
                product_type_terms=["mouse", "wireless mouse"],
                distinctive_terms=["rechargeable", "silent"],
                keywords=[
                    KeywordCandidate(
                        phrase="wireless mouse",
                        rationale="Exact product type visible in the image",
                    ),
                    KeywordCandidate(
                        phrase="computer accessory",
                        rationale="A deliberately broad candidate for validation",
                    ),
                ],
                exclusions=["keyboard combo"],
                confidence=0.91,
                title_suggestion="Rechargeable Wireless Mouse - Silent Dual Mode",
                title_reason="Lead with the verified product type and differentiators.",
            ),
            model="gpt-5.6-terra",
            response_id="resp_test",
            usage={"input_tokens": 120, "output_tokens": 80, "total_tokens": 200},
        )


class FakeSearchClient:
    def __init__(self) -> None:
        self.next_calls = 0

    async def __aenter__(self) -> FakeSearchClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def fetch_search_first_page(
        self,
        keyword: str,
    ) -> tuple[str, dict[str, Any]]:
        if keyword == "wireless mouse":
            return _search_url(keyword), _payload(
                [
                    (str(90_000_000 + index), f"Wireless Mouse Model {index}")
                    for index in range(36)
                ],
                after="page-two",
                total=120,
            )
        return _search_url(keyword), _payload(
            [(str(80_000_000 + index), f"Winter Jacket Style {index}") for index in range(12)],
            after="",
            total=12,
        )

    async def fetch_search_next_page(
        self,
        request_url: str,
        after: str,
    ) -> dict[str, Any]:
        assert "/rest/v-1-18-0/searches/products," in request_url
        assert after == "page-two"
        self.next_calls += 1
        products = [
            (str(91_000_000 + index), f"Wireless Mouse Page Two {index}")
            for index in range(4)
        ]
        products.append(("12345678", "Rechargeable Wireless Mouse - Silent Dual Mode"))
        return _payload(products, after="", total=120)


def _search_url(keyword: str) -> str:
    return (
        "https://api.takealot.com/rest/v-1-18-0/"
        f"searches/products,filters?qsearch={keyword.replace(' ', '+')}"
    )


def _payload(
    products: list[tuple[str, str]],
    *,
    after: str,
    total: int,
) -> dict[str, Any]:
    return {
        "sections": {
            "products": {
                "results": [
                    {
                        "type": "product_views",
                        "product_views": {
                            "core": {
                                "id": int(plid),
                                "title": title,
                                "slug": title.casefold().replace(" ", "-"),
                            }
                        },
                    }
                    for plid, title in products
                ],
                "paging": {
                    "next_is_after": after,
                    "total_num_found": total,
                },
            }
        }
    }


@pytest.mark.asyncio
async def test_service_validates_keywords_locates_cursor_page_and_reuses_vision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'ranking.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    monkeypatch.setenv("TAKEALOT_SEARCH_PAGE_DELAY_SECONDS", "0")
    FakeVisionClient.calls = 0
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    with Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="offer-1",
                productline_id="12345678",
                sku="MOUSE-01",
                title="Silent Rechargeable Mouse",
                image_url="https://media.takealot.com/test/s.file",
                captured_at=datetime(2026, 8, 7, 1, tzinfo=UTC),
            )
        )
    engine.dispose()

    clients: list[FakeSearchClient] = []

    def search_factory() -> FakeSearchClient:
        client = FakeSearchClient()
        clients.append(client)
        return client

    service = SearchRankingService(
        tmp_path,
        vision_client_factory=FakeVisionClient,
        search_client_factory=search_factory,  # type: ignore[arg-type]
    )
    first = await service.analyze_offer("offer-1")
    second = await service.analyze_offer("offer-1")

    accepted, rejected = first["analysis"]["keywords"]
    assert accepted["relevance_status"] == "accepted"
    assert accepted["page_number"] == 2
    assert accepted["page_rank"] == 5
    assert accepted["organic_rank"] == 41
    assert accepted["row_number"] == 2
    assert accepted["column_number"] == 1
    assert accepted["validation_evidence"]["position_scope"] == (
        "organic_results_excluding_sponsored"
    )
    assert rejected["relevance_status"] == "rejected_irrelevant"
    assert rejected["pages_scanned"] == 1
    assert rejected["found"] is False
    assert first["analysis"]["usage"]["total_tokens"] == 200
    assert second["analysis"]["vision_reused"] is True
    assert second["analysis"]["title_validation"]["status"] == "pending_title_change"
    assert FakeVisionClient.calls == 1
    assert all(client.next_calls == 1 for client in clients)


def test_title_suggestion_is_only_marked_forward_after_comparable_observation() -> None:
    validation = _title_validation(
        previous={
            "source_title": "Old Mouse Title",
            "title_suggestion": "Better Wireless Mouse",
            "analysis_id": 1,
            "ranks": {"wireless mouse": 41, "rechargeable mouse": 28},
        },
        current_title="Better Wireless Mouse",
        current_results=[
            _observation("wireless mouse", 33),
            _observation("rechargeable mouse", 20),
        ],
    )

    assert validation["status"] == "observed_forward"
    assert validation["guarantee"] is False
    assert validation["causality"] == "observational_only"
    assert validation["comparisons"][0]["delta"] == 8


def _observation(keyword: str, rank: int) -> Any:
    from takealot_ops.search_ranking.service import KeywordObservation

    return KeywordObservation(
        keyword=keyword,
        candidate_order=1,
        relevance_status="accepted",
        relevance_score=1.0,
        validation_evidence={},
        total_num_found=100,
        pages_scanned=1,
        found=True,
        page_number=1,
        page_rank=rank,
        organic_rank=rank,
        row_number=1,
        column_number=1,
        target_url=None,
        observed_at=datetime(2026, 8, 7),
    )


def test_web_reads_local_status_and_missing_key_never_starts_external_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'ranking-web.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = create_app(tmp_path)

    with TestClient(app, client=("127.0.0.1", 50100)) as client:
        issued = client.post(
            "/api/auth/bootstrap",
            json={
                "username": "ranking.admin",
                "display_name": "Ranking Admin",
                "password": "ranking-password-123",
            },
        ).json()
        engine = create_engine_for_database_url(database_url)
        with Session(engine) as session, session.begin():
            session.add(
                OfferCurrent(
                    offer_id="offer-web",
                    productline_id="12345678",
                    sku="WEB-01",
                    title="Wireless Mouse",
                    image_url="https://media.takealot.com/test/s.file",
                    captured_at=datetime(2026, 8, 7),
                )
            )
        engine.dispose()

        listing = client.get("/api/erp/search-ranking")
        run = client.post(
            "/api/erp/search-ranking/offer-web/analyze",
            headers={"X-CSRF-Token": issued["csrf_token"]},
        )

    assert listing.status_code == 200
    assert listing.json()["status"]["configured"] is False
    assert listing.json()["status"]["passive_reads_are_local_only"] is True
    assert listing.json()["items"][0]["offer_id"] == "offer-web"
    assert run.status_code == 503
    assert "OPENAI_API_KEY" in run.json()["detail"]
