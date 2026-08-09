from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from takealot_ops.search_ranking.service import (
    KeywordCandidate,
    OpenAICompatibleProductVisionClient,
    PROMPT_VERSION,
    SearchKeywordCandidate,
    SearchRankingInputError,
    SearchRankingProviderError,
    SearchRankingRuntimeSettings,
    SearchRankingService,
    VisionCallResult,
    VisionProfile,
    _append_unique_candidate,
    _analysis_payload,
    _build_hot_term_title_suggestion,
    _build_title_suggestion,
    _build_title_strategies,
    _collect_keyword_observation,
    _cross_check_image_profile,
    _discover_keyword_candidates,
    _inject_comparison_resample_candidates,
    _opportunity_gate_from_result,
    _opportunity_phrase_safety,
    _previous_analysis_snapshot,
    _search_products,
    _title_validation,
    _validated_chat_profile,
)
from takealot_ops.search_ranking import service as search_ranking_service
from takealot_ops.erp.web import create_app
from takealot_ops.storage.migrations import (
    create_engine_for_database_url,
    create_schema,
)
from takealot_ops.storage.models import (
    OfferCurrent,
    SearchRankingAnalysis,
    SearchRankingKeywordResult,
)


class FakeVisionClient:
    calls = 0

    def __init__(self, _: SearchRankingRuntimeSettings) -> None:
        pass

    async def identify(
        self,
        *,
        image_url: str,
        reference_title: str,
    ) -> VisionCallResult:
        del image_url, reference_title
        type(self).calls += 1
        return VisionCallResult(
            profile=VisionProfile(
                product_name="Rechargeable wireless gaming mouse",
                category="Computer mice",
                product_type_terms=["mouse", "wireless mouse"],
                distinctive_terms=["rechargeable", "silent", "gaming"],
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
                autocomplete_seeds=[
                    KeywordCandidate(
                        phrase="wireless",
                        rationale="A shopper starts with the connection type",
                    ),
                    KeywordCandidate(
                        phrase="wireless mouse",
                        rationale="The visible product type",
                    ),
                ],
                opportunity_seeds=[
                    KeywordCandidate(
                        phrase="mouse for laptop",
                        rationale="Adjacent laptop-use demand",
                    )
                ],
                exclusions=["keyboard combo"],
                confidence=0.91,
                title_suggestion="Rechargeable Wireless Mouse - Silent Dual Mode",
                title_reason="Lead with the verified product type and differentiators.",
            ),
            provider="qwen",
            model="qwen3.7-plus",
            response_id="resp_test",
            usage={"input_tokens": 120, "output_tokens": 80, "total_tokens": 200},
            estimated_cost_cny=0.00088,
        )


def test_prompt_version_fits_persisted_column() -> None:
    assert len(PROMPT_VERSION) <= 30


def test_qwen_string_candidate_arrays_are_normalized_before_validation() -> None:
    arguments = {
        "product_name": "RGB light bars",
        "category": "Lighting",
        "product_type_terms": ["light bars"],
        "distinctive_terms": ["remote control"],
        "keywords": ["rgb light bars", "remote light bars"],
        "autocomplete_seeds": ["rgb light", "ambient light"],
        "opportunity_seeds": ["gaming lights"],
        "exclusions": ["light bulb"],
        "confidence": 0.92,
        "title_suggestion": "RGB Light Bars Remote Ambient Lighting",
        "title_reason": "Image-only product wording",
    }
    body = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "submit_takealot_product_profile",
                                "arguments": json.dumps(arguments),
                            }
                        }
                    ]
                }
            }
        ]
    }

    profile = _validated_chat_profile(body)

    assert profile.keywords[0].phrase == "rgb light bars"
    assert profile.autocomplete_seeds[0].rationale == "图片模型给出的自然搜索词根"
    assert profile.opportunity_seeds[0].phrase == "gaming lights"


class FakeSearchClient:
    def __init__(self) -> None:
        self.next_calls = 0

    async def __aenter__(self) -> FakeSearchClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def fetch_search_suggestions(self, keyword: str) -> list[str]:
        if keyword == "wireless":
            return ["wireless mouse", "wireless gaming mouse"]
        if keyword == "mouse for laptop":
            return ["mouse for laptop"]
        return []

    async def fetch_search_first_page(
        self,
        keyword: str,
    ) -> tuple[str, dict[str, Any]]:
        if keyword in {"wireless mouse", "wireless gaming mouse"}:
            return _search_url(keyword), _payload(
                [
                    (str(90_000_000 + index), f"Wireless Mouse Model {index}")
                    for index in range(36)
                ],
                after="page-two",
                total=120,
            )
        if keyword == "mouse for laptop":
            products = [("12345678", "Rechargeable Wireless Mouse")]
            products.extend(
                (str(70_000_000 + index), f"Wireless Mouse Laptop {index}")
                for index in range(2)
            )
            products.extend(
                (str(75_000_000 + index), f"Laptop Sleeve Style {index}")
                for index in range(33)
            )
            return _search_url(keyword), _payload(products, after="", total=640)
        return _search_url(keyword), _payload(
            [(str(80_000_000 + index), f"Winter Jacket Style {index}") for index in range(36)],
            after="",
            total=36,
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
        products.append(
            ("12345678", "Rechargeable Wireless Gaming Mouse Silent Dual Mode")
        )
        return _payload(products, after="", total=120)


class RankingScenarioSearchClient(FakeSearchClient):
    def __init__(self, *, opportunity_rank: int, core_page_rank: int) -> None:
        super().__init__()
        self.opportunity_rank = opportunity_rank
        self.core_page_rank = core_page_rank

    async def fetch_search_first_page(
        self,
        keyword: str,
    ) -> tuple[str, dict[str, Any]]:
        if keyword != "mouse for laptop":
            return await super().fetch_search_first_page(keyword)
        products = [
            (str(70_000_000 + index), f"Wireless Mouse Laptop {index}")
            for index in range(2)
        ]
        products.extend(
            (str(75_000_000 + index), f"Laptop Sleeve Style {index}")
            for index in range(34)
        )
        products[self.opportunity_rank - 1] = (
            "12345678",
            "Rechargeable Wireless Mouse",
        )
        return _search_url(keyword), _payload(products, after="", total=640)

    async def fetch_search_next_page(
        self,
        request_url: str,
        after: str,
    ) -> dict[str, Any]:
        assert after == "page-two"
        self.next_calls += 1
        products = [
            (str(91_000_000 + index), f"Wireless Mouse Page Two {index}")
            for index in range(36)
        ]
        products[self.core_page_rank - 1] = (
            "12345678",
            "Rechargeable Wireless Gaming Mouse Silent Dual Mode",
        )
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


class OpportunityGateSearchClient:
    def __init__(self, pages: list[list[tuple[str, str]]]) -> None:
        self.pages = pages

    async def fetch_search_first_page(
        self,
        keyword: str,
    ) -> tuple[str, dict[str, Any]]:
        return _search_url(keyword), _payload(
            self.pages[0],
            after="page-2" if len(self.pages) > 1 else "",
            total=sum(len(page) for page in self.pages),
        )

    async def fetch_search_next_page(
        self,
        request_url: str,
        after: str,
    ) -> dict[str, Any]:
        assert "/rest/v-1-18-0/searches/products," in request_url
        page_number = int(after.rsplit("-", 1)[-1])
        page_index = page_number - 1
        return _payload(
            self.pages[page_index],
            after=(
                f"page-{page_number + 1}"
                if page_number < len(self.pages)
                else ""
            ),
            total=sum(len(page) for page in self.pages),
        )


def _opportunity_page(
    *,
    direct_competitors: int,
    target_rank: int | None = None,
    page_size: int = 36,
) -> list[tuple[str, str]]:
    products = [
        (str(62_000_000 + index), f"Wireless Mouse Competitor {index}")
        for index in range(direct_competitors)
    ]
    while len(products) < page_size:
        index = len(products)
        products.append((str(63_000_000 + index), f"Laptop Sleeve Style {index}"))
    if target_rank is not None:
        products[target_rank - 1] = (
            "12345678",
            "Rechargeable Wireless Gaming Mouse",
        )
    return products


@pytest.mark.asyncio
async def test_service_validates_keywords_locates_cursor_page_and_reuses_vision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'ranking.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
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
                title="Silent Rechargeable Wireless Gaming Mouse",
                image_url="http://media.takealot.com/covers_images/test/s.file",
                status="buyable",
                takealot_available_stock=2,
                seller_available_stock=1,
                captured_at=datetime.now(UTC),
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

    accepted, opportunity, second_accepted, rejected = first["analysis"]["keywords"]
    assert accepted["relevance_status"] == "accepted"
    assert accepted["page_number"] == 2
    assert accepted["page_rank"] == 5
    assert accepted["organic_rank"] == 41
    assert accepted["row_number"] == 2
    assert accepted["column_number"] == 1
    assert accepted["validation_evidence"]["position_scope"] == (
        "organic_results_excluding_sponsored"
    )
    assert accepted["validation_evidence"]["candidate_source"] == (
        "takealot_autocomplete"
    )
    assert accepted["validation_evidence"]["autocomplete_rank"] == 1
    assert accepted["validation_evidence"]["evaluated_first_page_results"] == 36
    assert accepted["validation_evidence"]["matched_first_page_results"] == 36
    assert second_accepted["relevance_status"] == "accepted"
    assert opportunity["relevance_status"] == "opportunity"
    assert opportunity["validation_evidence"]["autocomplete_rank"] == 1
    assert opportunity["validation_evidence"]["matched_first_page_results"] == 3
    assert opportunity["validation_evidence"]["evaluated_first_page_results"] == 36
    assert opportunity["validation_evidence"][
        "direct_competitor_count_excluding_target_first_page"
    ] == 2
    assert opportunity["validation_evidence"]["opportunity_qualified"] is True
    assert opportunity["found"] is True
    assert rejected["relevance_status"] == "rejected_irrelevant"
    assert rejected["pages_scanned"] == 1
    assert rejected["found"] is False
    assert first["analysis"]["usage"]["total_tokens"] == 200
    assert first["analysis"]["provider"] == "qwen"
    assert first["analysis"]["estimated_cost_cny"] == 0.00088
    title_strategies = first["analysis"]["title_strategies"]
    assert [item["strategy"] for item in title_strategies] == [
        "contiguous_core",
        "hot_term_coverage",
        "adjacent_opportunity",
    ]
    assert all(item["available"] is True for item in title_strategies)
    assert all(item["title"] for item in title_strategies)
    assert all(item["explanation"] for item in title_strategies)
    assert len({item["explanation"] for item in title_strategies}) == 3
    assert all(item["evidence_keywords"] for item in title_strategies)
    assert title_strategies[0]["title"].startswith("Wireless Mouse")
    assert title_strategies[1]["title"].startswith("Wireless Gaming Mouse")
    assert title_strategies[1]["title"] != title_strategies[0]["title"]
    assert title_strategies[2]["title"].startswith("Mouse For Laptop")
    for strategy in title_strategies:
        assert all(
            character.isalnum() or character == " "
            for character in strategy["title"]
        )
    assert first["analysis"]["title_suggestion"] == title_strategies[0]["title"]
    assert (
        first["analysis"]["profile"]["title_suggestion"]
        == title_strategies[0]["title"]
    )
    assert (
        first["analysis"]["opportunity_title_suggestion"]
        == title_strategies[2]["title"]
    )
    assert first["analysis"]["recognition"]["model_received_source_title"] is False
    assert first["analysis"]["recognition"]["title_reference_terms"] == [
        "mouse",
        "wireless mouse",
        "rechargeable",
        "silent",
        "gaming",
    ]
    assert first["analysis"]["title_reason"]
    assert second["analysis"]["vision_reused"] is True
    assert second["analysis"]["usage"]["total_tokens"] == 0
    assert second["analysis"]["title_validation"]["status"] == "pending_title_change"
    assert FakeVisionClient.calls == 1
    assert all(client.next_calls == 2 for client in clients)


@pytest.mark.asyncio
async def test_changed_adjacent_title_resamples_only_its_primary_evidence_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'strategy-resample.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setenv("TAKEALOT_SEARCH_PAGE_DELAY_SECONDS", "0")
    FakeVisionClient.calls = 0
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    with Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="offer-resample",
                productline_id="12345678",
                sku="RESAMPLE-01",
                title="Silent Rechargeable Wireless Gaming Mouse",
                image_url="http://media.takealot.com/covers_images/test/s.file",
                status="buyable",
                takealot_available_stock=2,
                captured_at=datetime.now(UTC),
            )
        )
    engine.dispose()
    scenarios = iter(
        [
            RankingScenarioSearchClient(
                opportunity_rank=20,
                core_page_rank=5,
            ),
            RankingScenarioSearchClient(
                opportunity_rank=5,
                core_page_rank=10,
            ),
        ]
    )

    service = SearchRankingService(
        tmp_path,
        vision_client_factory=FakeVisionClient,
        search_client_factory=lambda: next(scenarios),  # type: ignore[arg-type]
    )
    first = await service.analyze_offer("offer-resample")
    adjacent_title = first["analysis"]["title_strategies"][2]["title"]
    assert adjacent_title
    engine = create_engine_for_database_url(database_url)
    with Session(engine) as session, session.begin():
        offer = session.get(OfferCurrent, "offer-resample")
        assert offer is not None
        offer.title = adjacent_title
        offer.captured_at = datetime.now(UTC)
    engine.dispose()

    second = await service.analyze_offer("offer-resample")
    validation = second["analysis"]["title_validation"]
    primary = next(
        item
        for item in second["analysis"]["keywords"]
        if item["keyword"] == "mouse for laptop"
    )

    assert second["analysis"]["vision_reused"] is True
    assert validation["matched_strategy"] == "adjacent_opportunity"
    assert validation["status"] == "observed_forward"
    assert validation["required_keywords"] == ["mouse for laptop"]
    assert validation["comparisons"] == [
        {
            "keyword": "mouse for laptop",
            "before_rank": 20,
            "after_rank": 5,
            "delta": 15,
        }
    ]
    assert all(
        item["delta"] < 0 for item in validation["secondary_comparisons"]
    )
    assert primary["validation_evidence"]["comparison_role"] == "primary"
    assert primary["validation_evidence"]["comparison_strategy"] == (
        "adjacent_opportunity"
    )
    assert FakeVisionClient.calls == 1


def test_provider_signature_tracks_the_configured_fallback_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "qwen-secret")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    qwen_only = SearchRankingRuntimeSettings.from_env(tmp_path).provider_signature

    monkeypatch.setenv("ARK_API_KEY", "doubao-secret")
    both = SearchRankingRuntimeSettings.from_env(tmp_path).provider_signature

    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    doubao_only = SearchRankingRuntimeSettings.from_env(tmp_path).provider_signature

    assert qwen_only.startswith("qwen:")
    assert both.startswith("doubao:")
    assert "|qwen:" in both
    assert doubao_only.startswith("doubao:")
    assert len({qwen_only, both, doubao_only}) == 3
    assert "secret" not in f"{qwen_only}{both}{doubao_only}"


@pytest.mark.asyncio
async def test_cached_identity_conflict_skips_search_without_another_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'identity-conflict.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setenv("TAKEALOT_SEARCH_PAGE_DELAY_SECONDS", "0")
    FakeVisionClient.calls = 0
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    with Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="offer-identity",
                productline_id="12345678",
                sku="IDENTITY-01",
                title="Silent Rechargeable Wireless Gaming Mouse",
                image_url="http://media.takealot.com/covers_images/test/s.file",
                status="buyable",
                takealot_available_stock=2,
                captured_at=datetime.now(UTC),
            )
        )
    engine.dispose()
    search_clients: list[FakeSearchClient] = []

    def search_factory() -> FakeSearchClient:
        client = FakeSearchClient()
        search_clients.append(client)
        return client

    service = SearchRankingService(
        tmp_path,
        vision_client_factory=FakeVisionClient,
        search_client_factory=search_factory,  # type: ignore[arg-type]
    )
    await service.analyze_offer("offer-identity")
    engine = create_engine_for_database_url(database_url)
    with Session(engine) as session, session.begin():
        offer = session.get(OfferCurrent, "offer-identity")
        assert offer is not None
        offer.title = "Leather Dining Chair"
        offer.captured_at = datetime.now(UTC)
    engine.dispose()

    second = await service.analyze_offer("offer-identity")

    assert second["analysis"]["vision_reused"] is True
    assert second["analysis"]["usage"]["total_tokens"] == 0
    assert second["analysis"]["recognition"]["cached_identity_conflict"] is True
    assert all(
        item["relevance_status"] == "model_low_confidence"
        and item["pages_scanned"] == 0
        for item in second["analysis"]["keywords"]
    )
    assert FakeVisionClient.calls == 1
    assert len(search_clients) == 1


@pytest.mark.asyncio
async def test_live_identity_conflict_caches_raw_profile_for_a_corrected_title(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'raw-profile-cache.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setenv("TAKEALOT_SEARCH_PAGE_DELAY_SECONDS", "0")
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    with Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="offer-raw-profile",
                productline_id="12345678",
                sku="RAW-01",
                title="Leather Dining Chair",
                image_url="http://media.takealot.com/covers_images/test/s.file",
                status="buyable",
                takealot_available_stock=2,
                captured_at=datetime.now(UTC),
            )
        )
    engine.dispose()

    class ConflictVisionClient:
        calls = 0

        def __init__(self, _: SearchRankingRuntimeSettings) -> None:
            pass

        async def identify(
            self,
            *,
            image_url: str,
            reference_title: str,
        ) -> VisionCallResult:
            del image_url, reference_title
            type(self).calls += 1
            raw = _opportunity_profile()
            return VisionCallResult(
                profile=raw.model_copy(update={"confidence": 0.49}),
                cache_profile=raw,
                provider="qwen",
                model="qwen3.7-plus",
                response_id="both-conflict",
                usage={
                    "input_tokens": 300,
                    "output_tokens": 100,
                    "total_tokens": 400,
                },
                estimated_cost_cny=0.0014,
                provider_attempts=(
                    {"provider": "qwen", "status": "identity_conflict"},
                ),
            )

    search_clients: list[FakeSearchClient] = []

    def search_factory() -> FakeSearchClient:
        client = FakeSearchClient()
        search_clients.append(client)
        return client

    service = SearchRankingService(
        tmp_path,
        vision_client_factory=ConflictVisionClient,
        search_client_factory=search_factory,  # type: ignore[arg-type]
    )
    first = await service.analyze_offer("offer-raw-profile")
    assert first["analysis"]["confidence"] == pytest.approx(0.49)
    assert first["analysis"]["recognition"]["live_identity_conflict"] is True
    assert search_clients == []

    engine = create_engine_for_database_url(database_url)
    with Session(engine) as session, session.begin():
        offer = session.get(OfferCurrent, "offer-raw-profile")
        assert offer is not None
        offer.title = "Silent Rechargeable Wireless Gaming Mouse"
        offer.captured_at = datetime.now(UTC)
    engine.dispose()
    second = await service.analyze_offer("offer-raw-profile")

    assert second["analysis"]["vision_reused"] is True
    assert second["analysis"]["usage"]["total_tokens"] == 0
    assert second["analysis"]["confidence"] == pytest.approx(0.9)
    assert second["analysis"]["recognition"].get("cached_identity_conflict") is None
    assert any(
        item["relevance_status"] == "accepted"
        for item in second["analysis"]["keywords"]
    )
    assert ConflictVisionClient.calls == 1
    assert len(search_clients) == 1


@pytest.mark.asyncio
async def test_failed_search_reuses_the_already_paid_vision_stage_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'vision-stage-cache.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setenv("TAKEALOT_SEARCH_PAGE_DELAY_SECONDS", "0")
    FakeVisionClient.calls = 0
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    with Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="offer-stage-cache",
                productline_id="12345678",
                sku="CACHE-01",
                title="Silent Rechargeable Wireless Gaming Mouse",
                image_url="http://media.takealot.com/covers_images/test/s.file",
                status="buyable",
                takealot_available_stock=2,
                captured_at=datetime.now(UTC),
            )
        )
    engine.dispose()

    class FailingSearchClient(FakeSearchClient):
        async def fetch_search_first_page(
            self,
            keyword: str,
        ) -> tuple[str, dict[str, Any]]:
            raise RuntimeError(f"search failed for {keyword}")

    search_attempts = 0

    def search_factory() -> FakeSearchClient:
        nonlocal search_attempts
        search_attempts += 1
        return FailingSearchClient() if search_attempts == 1 else FakeSearchClient()

    service = SearchRankingService(
        tmp_path,
        vision_client_factory=FakeVisionClient,
        search_client_factory=search_factory,  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="search failed"):
        await service.analyze_offer("offer-stage-cache")

    failed_detail = service.detail_payload("offer-stage-cache")
    assert failed_detail is not None
    assert failed_detail["analysis"] is None
    assert failed_detail["latest_attempt"]["status"] == "failed"
    assert failed_detail["latest_attempt"]["vision_stage_completed"] is True
    assert failed_detail["latest_attempt"]["usage"]["total_tokens"] == 200

    recovered = await service.analyze_offer("offer-stage-cache")

    assert recovered["analysis"]["vision_reused"] is True
    assert recovered["analysis"]["usage"]["total_tokens"] == 0
    assert FakeVisionClient.calls == 1
    assert search_attempts == 2


def test_title_suggestion_puts_validated_terms_first_and_removes_punctuation() -> None:
    suggestion = _build_title_suggestion(
        "High-Brightness Outdoor Movie Screen (100 Inch) Foldable & Portable Projection Screen",
        ["portable projection screen", "outdoor movie screen"],
    )

    assert suggestion == (
        "Portable Projection Screen High Brightness Outdoor Movie 100 Inch Foldable"
    )
    assert suggestion.startswith("Portable Projection Screen")
    assert all(character.isalnum() or character == " " for character in suggestion)


def test_hot_term_strategy_merges_overlapping_phrases_without_keyword_stutter() -> None:
    suggestion = _build_hot_term_title_suggestion(
        "Silent Rechargeable Wireless Gaming Mouse",
        ["wireless mouse", "wireless gaming mouse"],
    )

    assert suggestion == "Wireless Gaming Mouse Silent Rechargeable"
    assert all(character.isalnum() or character == " " for character in suggestion)


@pytest.mark.parametrize(
    ("source_title", "accepted_keywords", "hot_term_keywords"),
    [
        (
            "Silent Rechargeable Wireless Gaming Mouse",
            ["wireless mouse"],
            ["wireless gaming mouse"],
        ),
        (
            "Silent Wireless Mouse",
            ["wireless mouse"],
            ["wireless mouse", "wireless mice"],
        ),
    ],
    ids=["only-one-hot-term", "merged-title-duplicates-core"],
)
def test_hot_term_strategy_is_unavailable_without_a_distinct_multi_term_tactic(
    source_title: str,
    accepted_keywords: list[str],
    hot_term_keywords: list[str],
) -> None:
    strategies = _build_title_strategies(
        source_title=source_title,
        accepted_keywords=accepted_keywords,
        hot_term_keywords=hot_term_keywords,
        opportunity_keywords=[],
    )

    hot = strategies[1]
    assert hot["strategy"] == "hot_term_coverage"
    assert hot["available"] is False
    assert hot["title"] is None


def test_unsupported_high_risk_adjacent_term_never_generates_third_title() -> None:
    strategies = _build_title_strategies(
        source_title="Silent Gaming Mouse",
        accepted_keywords=["gaming mouse"],
        hot_term_keywords=["gaming mouse"],
        opportunity_keywords=["smart wireless mouse for laptop"],
    )

    adjacent = strategies[2]
    assert adjacent["strategy"] == "adjacent_opportunity"
    assert adjacent["available"] is False
    assert adjacent["title"] is None
    assert adjacent["evidence_keywords"] == []
    assert all(
        "smart" not in str(strategy["title"] or "").casefold()
        and "wireless" not in str(strategy["title"] or "").casefold()
        for strategy in strategies
    )


@pytest.mark.parametrize(
    ("source_title", "opportunity_keyword"),
    [
        ("Compressed Sofa", "leather lazy sofa"),
        ("Compressed Sofa", "2 seater sofa"),
        ("Dining Chair", "kids chair"),
        ("Memory Pillow", "orthopedic pillow"),
    ],
    ids=["material", "capacity", "audience", "efficacy"],
)
def test_unsupported_adjacent_fact_claim_never_enters_a_title(
    source_title: str,
    opportunity_keyword: str,
) -> None:
    strategies = _build_title_strategies(
        source_title=source_title,
        accepted_keywords=[source_title],
        hot_term_keywords=[],
        opportunity_keywords=[opportunity_keyword],
    )

    assert strategies[2]["available"] is False
    assert strategies[2]["title"] is None


def test_pure_need_state_adjacent_seed_can_generate_a_distinct_title() -> None:
    strategies = _build_title_strategies(
        source_title="Compressed Sofa",
        accepted_keywords=["compressed sofa"],
        hot_term_keywords=[],
        opportunity_keywords=["lazy sofa"],
    )

    assert strategies[2]["available"] is True
    assert str(strategies[2]["title"]).startswith("Lazy Sofa")


def test_opportunity_phrase_safety_blocks_autocomplete_and_distinctive_claims() -> None:
    autocomplete_added = _opportunity_phrase_safety(
        keyword="leather lazy sofa",
        source_title="Compressed Sofa",
        opportunity_seeds=["lazy sofa"],
        distinctive_terms=[],
    )
    unsupported_distinctive = _opportunity_phrase_safety(
        keyword="tufted lazy sofa",
        source_title="Compressed Sofa",
        opportunity_seeds=["tufted lazy sofa"],
        distinctive_terms=["tufted"],
    )
    safe_need_state = _opportunity_phrase_safety(
        keyword="lazy sofa",
        source_title="Compressed Sofa",
        opportunity_seeds=["lazy sofa"],
        distinctive_terms=[],
    )

    assert autocomplete_added["opportunity_claims_safe"] is False
    assert autocomplete_added["opportunity_unsupported_autocomplete_terms"] == [
        "leather"
    ]
    assert unsupported_distinctive["opportunity_claims_safe"] is False
    assert unsupported_distinctive["opportunity_unsupported_distinctive_terms"] == [
        "tufted"
    ]
    assert safe_need_state["opportunity_claims_safe"] is True


def test_stored_safe_flag_cannot_override_current_fact_claim_rejection() -> None:
    observed_at = datetime(2026, 8, 9, 8, tzinfo=UTC)
    evidence = {
        "candidate_source": "takealot_autocomplete",
        "intended_strategy": "opportunity",
        "autocomplete_seed": "leather lazy sofa",
        "autocomplete_rank": 1,
        "direct_competitor_count_excluding_target_first_page": 1,
        "opportunity_claims_safe": True,
    }
    gate = _opportunity_gate_from_result(
        keyword="leather lazy sofa",
        source_title="Compressed Sofa",
        found=True,
        page_number=1,
        organic_rank=12,
        validation_evidence=evidence,
    )
    analysis = SearchRankingAnalysis(
        id=95,
        offer_id="offer-leather-history",
        productline_id="12345678",
        sku="SOFA-HISTORY",
        source_title="Compressed Sofa",
        source_image_url="https://media.takealot.com/covers_images/sofa.jpg",
        cache_key="f" * 64,
        provider="qwen",
        model="qwen3.7-plus",
        prompt_version=PROMPT_VERSION,
        status="completed",
        product_name="Compressed sofa",
        category="Sofas",
        confidence=Decimal("0.9000"),
        vision_payload={"profile": {"distinctive_terms": []}},
        vision_reused=False,
        title_suggestion="Compressed Sofa",
        title_reason="Historical suggestion",
        title_validation={"status": "baseline_created"},
        created_at=observed_at,
        completed_at=observed_at,
    )
    result = SearchRankingKeywordResult(
        id=96,
        analysis_id=95,
        keyword="leather lazy sofa",
        candidate_order=1,
        relevance_status="opportunity",
        relevance_score=Decimal("0.0500"),
        validation_evidence=evidence,
        total_num_found=80,
        pages_scanned=1,
        found=True,
        page_number=1,
        page_rank=12,
        organic_rank=12,
        row_number=3,
        column_number=4,
        columns_per_row=4,
        target_url="https://www.takealot.com/example/PLID12345678",
        observed_at=observed_at,
    )
    payload = _analysis_payload(analysis, [result])

    assert gate["opportunity_qualified"] is False
    assert gate["opportunity_claims_safe"] is False
    assert gate["opportunity_unsupported_fact_terms"] == ["leather"]
    assert "unsupported_fact_claim" in gate["opportunity_rejection_reasons"]
    assert payload["keywords"][0]["relevance_status"] == "rejected_irrelevant"
    assert payload["title_strategies"][2]["available"] is False


def test_adjacent_strategy_is_unavailable_when_it_duplicates_the_core_title() -> None:
    strategies = _build_title_strategies(
        source_title="Wireless Mouse",
        accepted_keywords=["wireless mouse"],
        hot_term_keywords=[],
        opportunity_keywords=["wireless mouse"],
    )

    adjacent = strategies[2]
    assert adjacent["strategy"] == "adjacent_opportunity"
    assert adjacent["available"] is False
    assert adjacent["title"] is None


def test_historical_opportunity_is_projected_through_current_strict_gate() -> None:
    observed_at = datetime(2026, 8, 7, 10, tzinfo=UTC)
    analysis = SearchRankingAnalysis(
        id=91,
        offer_id="offer-history",
        productline_id="12345678",
        sku="MOUSE-HISTORY",
        source_title="Silent Gaming Mouse",
        source_image_url="https://media.takealot.com/covers_images/history.jpg",
        cache_key="a" * 64,
        provider="qwen",
        model="qwen3.7-plus",
        prompt_version=PROMPT_VERSION,
        status="completed",
        product_name="Gaming mouse",
        category="Computer mice",
        confidence=Decimal("0.9000"),
        vision_payload={"profile": {}},
        vision_reused=False,
        title_suggestion="Gaming Mouse Silent",
        title_reason="Historical suggestion",
        title_validation={"status": "baseline_created"},
        created_at=observed_at,
        completed_at=observed_at,
    )
    historical_result = SearchRankingKeywordResult(
        id=92,
        analysis_id=91,
        keyword="mouse for laptop",
        candidate_order=1,
        relevance_status="opportunity",
        relevance_score=Decimal("0.1389"),
        validation_evidence={
            "candidate_source": "takealot_autocomplete",
            "intended_strategy": "opportunity",
            "autocomplete_rank": 1,
            "direct_competitor_count_first_page": 5,
            "validation_terms": ["mouse"],
        },
        total_num_found=640,
        pages_scanned=3,
        found=True,
        page_number=3,
        page_rank=23,
        organic_rank=95,
        row_number=6,
        column_number=3,
        columns_per_row=4,
        target_url="https://www.takealot.com/example/PLID12345678",
        observed_at=observed_at,
    )

    payload = _analysis_payload(analysis, [historical_result])

    adjacent = payload["title_strategies"][2]
    keyword = payload["keywords"][0]
    evidence = keyword["validation_evidence"]
    assert adjacent["strategy"] == "adjacent_opportunity"
    assert adjacent["available"] is False
    assert adjacent["title"] is None
    assert keyword["relevance_status"] == "rejected_irrelevant"
    assert evidence["stored_relevance_status"] == "opportunity"
    assert evidence["effective_relevance_status"] == "rejected_irrelevant"
    assert evidence["opportunity_qualified"] is False
    assert "too_many_direct_competitors" in evidence[
        "opportunity_rejection_reasons"
    ]
    assert "target_beyond_organic_rank_72" in evidence[
        "opportunity_rejection_reasons"
    ]
    assert historical_result.relevance_status == "opportunity"


def test_historical_opportunity_without_seed_safety_is_conservatively_hidden() -> None:
    observed_at = datetime(2026, 8, 7, 10, tzinfo=UTC)
    analysis = SearchRankingAnalysis(
        id=93,
        offer_id="offer-history-safe",
        productline_id="12345678",
        sku="MOUSE-HISTORY-SAFE",
        source_title="Silent Gaming Mouse",
        source_image_url="https://media.takealot.com/covers_images/history.jpg",
        cache_key="e" * 64,
        provider="qwen",
        model="qwen3.7-plus",
        prompt_version=PROMPT_VERSION,
        status="completed",
        product_name="Gaming mouse",
        category="Computer mice",
        confidence=Decimal("0.9000"),
        vision_payload={"profile": {"distinctive_terms": ["gaming"]}},
        vision_reused=False,
        title_suggestion="Gaming Mouse Silent",
        title_reason="Historical suggestion",
        title_validation={"status": "baseline_created"},
        created_at=observed_at,
        completed_at=observed_at,
    )
    historical_result = SearchRankingKeywordResult(
        id=94,
        analysis_id=93,
        keyword="mouse for laptop",
        candidate_order=1,
        relevance_status="opportunity",
        relevance_score=Decimal("0.1000"),
        validation_evidence={
            "candidate_source": "takealot_autocomplete",
            "intended_strategy": "opportunity",
            "autocomplete_rank": 1,
            "direct_competitor_count_excluding_target_first_page": 1,
            "validation_terms": ["mouse"],
        },
        total_num_found=100,
        pages_scanned=1,
        found=True,
        page_number=1,
        page_rank=20,
        organic_rank=20,
        row_number=5,
        column_number=4,
        columns_per_row=4,
        target_url="https://www.takealot.com/example/PLID12345678",
        observed_at=observed_at,
    )

    payload = _analysis_payload(analysis, [historical_result])
    evidence = payload["keywords"][0]["validation_evidence"]

    assert payload["title_strategies"][2]["available"] is False
    assert payload["keywords"][0]["relevance_status"] == "rejected_irrelevant"
    assert "opportunity_seed_does_not_cover_new_terms" in evidence[
        "opportunity_rejection_reasons"
    ]


def test_snapshot_preserves_legacy_issued_title_and_rank_for_audit(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'ranking-history.db').as_posix()}"
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    observed_at = datetime(2026, 8, 7, 10, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        analysis = SearchRankingAnalysis(
            offer_id="offer-history",
            productline_id="12345678",
            sku="MOUSE-HISTORY",
            source_title="Silent Gaming Mouse",
            source_image_url="https://media.takealot.com/covers_images/history.jpg",
            cache_key="b" * 64,
            provider="qwen",
            model="qwen3.7-plus",
            prompt_version=PROMPT_VERSION,
            status="completed",
            product_name="Gaming mouse",
            category="Computer mice",
            confidence=Decimal("0.9000"),
            vision_payload={
                "profile": {
                    "opportunity_title_suggestion": (
                        "Mouse For Laptop Gaming Mouse Silent"
                    )
                }
            },
            vision_reused=False,
            title_suggestion="Gaming Mouse Silent",
            title_reason="Historical suggestion",
            title_validation={"status": "baseline_created"},
            created_at=observed_at,
            completed_at=observed_at,
        )
        session.add(analysis)
        session.flush()
        session.add(
            SearchRankingKeywordResult(
                analysis_id=analysis.id,
                keyword="mouse for laptop",
                candidate_order=1,
                relevance_status="opportunity",
                relevance_score=Decimal("0.1389"),
                validation_evidence={
                    "candidate_source": "takealot_autocomplete",
                    "intended_strategy": "opportunity",
                    "autocomplete_rank": 1,
                    "direct_competitor_count_first_page": 5,
                    "validation_terms": ["mouse"],
                },
                total_num_found=640,
                pages_scanned=3,
                found=True,
                page_number=3,
                page_rank=23,
                organic_rank=95,
                row_number=6,
                column_number=3,
                columns_per_row=4,
                target_url="https://www.takealot.com/example/PLID12345678",
                observed_at=observed_at,
            )
        )

    with Session(engine) as session:
        snapshot = _previous_analysis_snapshot(session, "offer-history")
    engine.dispose()

    assert snapshot is not None
    assert "Mouse For Laptop Gaming Mouse Silent" in snapshot["title_suggestions"]
    adjacent = next(
        item
        for item in snapshot["issued_strategies"]
        if item["strategy"] == "adjacent_opportunity"
    )
    assert adjacent["policy_status"] == "legacy_issued_deprecated"
    assert adjacent["evidence_keywords"] == ["mouse for laptop"]
    assert snapshot["ranks"]["mouse for laptop"] == 95


def test_snapshot_uses_one_older_analysis_when_its_issued_title_is_adopted_late(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'delayed-title.db').as_posix()}"
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    observed_at = datetime(2026, 8, 7, 10, tzinfo=UTC)
    adopted_title = "Wireless Mouse Silent Rechargeable"
    with Session(engine) as session, session.begin():
        earlier = SearchRankingAnalysis(
            offer_id="offer-delayed",
            productline_id="12345678",
            sku="DELAYED-01",
            source_title="Old Mouse Title",
            source_image_url="https://media.takealot.com/covers_images/mouse.jpg",
            cache_key="1" * 64,
            provider="qwen",
            model="qwen3.7-plus",
            prompt_version=PROMPT_VERSION,
            status="completed",
            product_name="Wireless mouse",
            category="Computer mice",
            confidence=Decimal("0.9000"),
            vision_payload={
                "profile": {
                    "distinctive_terms": [],
                    "title_strategies": [
                        {
                            "strategy": "contiguous_core",
                            "title": adopted_title,
                            "available": True,
                            "evidence_keywords": ["wireless mouse"],
                        }
                    ],
                }
            },
            vision_reused=False,
            title_suggestion=adopted_title,
            title_reason="Earlier issued title",
            title_validation={"status": "baseline_created"},
            created_at=observed_at,
            completed_at=observed_at,
        )
        session.add(earlier)
        session.flush()
        earlier_id = earlier.id
        session.add(
            SearchRankingKeywordResult(
                analysis_id=earlier.id,
                keyword="wireless mouse",
                candidate_order=1,
                relevance_status="accepted",
                relevance_score=Decimal("0.9000"),
                validation_evidence={},
                total_num_found=100,
                pages_scanned=1,
                found=True,
                page_number=1,
                page_rank=10,
                organic_rank=10,
                row_number=3,
                column_number=2,
                columns_per_row=4,
                target_url=None,
                observed_at=observed_at,
            )
        )
        latest = SearchRankingAnalysis(
            offer_id="offer-delayed",
            productline_id="12345678",
            sku="DELAYED-01",
            source_title="Old Mouse Title",
            source_image_url="https://media.takealot.com/covers_images/mouse.jpg",
            cache_key="2" * 64,
            provider="qwen",
            model="qwen3.7-plus",
            prompt_version=PROMPT_VERSION,
            status="completed",
            product_name="Gaming mouse",
            category="Computer mice",
            confidence=Decimal("0.9000"),
            vision_payload={
                "profile": {
                    "distinctive_terms": [],
                    "title_strategies": [
                        {
                            "strategy": "contiguous_core",
                            "title": "Gaming Mouse Compact",
                            "available": True,
                            "evidence_keywords": ["gaming mouse"],
                        }
                    ],
                }
            },
            vision_reused=True,
            title_suggestion="Gaming Mouse Compact",
            title_reason="Latest issued title",
            title_validation={"status": "pending_title_change"},
            created_at=observed_at + timedelta(minutes=5),
            completed_at=observed_at + timedelta(minutes=5),
        )
        session.add(latest)
        session.flush()
        latest_id = latest.id
        session.add(
            SearchRankingKeywordResult(
                analysis_id=latest.id,
                keyword="gaming mouse",
                candidate_order=1,
                relevance_status="accepted",
                relevance_score=Decimal("0.9000"),
                validation_evidence={},
                total_num_found=100,
                pages_scanned=1,
                found=True,
                page_number=1,
                page_rank=30,
                organic_rank=30,
                row_number=8,
                column_number=2,
                columns_per_row=4,
                target_url=None,
                observed_at=observed_at + timedelta(minutes=5),
            )
        )

    with Session(engine) as session:
        delayed = _previous_analysis_snapshot(
            session,
            "offer-delayed",
            current_title=adopted_title,
        )
        fallback = _previous_analysis_snapshot(
            session,
            "offer-delayed",
            current_title="Unrelated New Title",
        )
    engine.dispose()

    assert delayed is not None
    assert delayed["analysis_id"] == earlier_id
    assert delayed["ranks"] == {"wireless mouse": 10}
    assert fallback is not None
    assert fallback["analysis_id"] == latest_id
    assert fallback["ranks"] == {"gaming mouse": 30}


def test_image_identity_cannot_remain_a_long_copy_of_source_title() -> None:
    source_title = (
        "2 RGB LED Light Bars TV Backlight with Remote Ambient Lighting Gaming Desk"
    )
    profile = VisionProfile(
        product_name=source_title,
        category="Lighting",
        product_type_terms=["RGB light bars", "TV backlight"],
        distinctive_terms=["ambient lighting", "remote control"],
        keywords=[
            KeywordCandidate(phrase="rgb light bars", rationale="Exact product type"),
            KeywordCandidate(phrase="tv backlight", rationale="Visible TV use"),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="rgb light", rationale="Natural shopper root"),
            KeywordCandidate(phrase="ambient light", rationale="Natural use root"),
        ],
        opportunity_seeds=[
            KeywordCandidate(phrase="gaming lights", rationale="Adjacent room use")
        ],
        exclusions=["light bulb", "led strip"],
        confidence=0.95,
        title_suggestion="RGB Light Bars TV Backlight Ambient Lighting",
        title_reason="Image-only hypothesis",
    )

    normalized, recognition = _cross_check_image_profile(profile, source_title)

    assert normalized.product_name != source_title
    assert len(normalized.product_name.split()) <= 7
    assert normalized.product_name == "RGB light bars ambient lighting remote control"
    assert recognition["model_received_source_title"] is False
    assert recognition["product_name_adjusted"] is True
    assert "RGB light bars" in recognition["title_reference_terms"]

    claim_checked, claim_evidence = _cross_check_image_profile(
        profile.model_copy(update={"product_name": "Smart RGB light bars"}),
        source_title,
    )
    assert claim_checked.product_name == "RGB light bars"
    assert claim_evidence["removed_unconfirmed_identity_terms"] == ["Smart"]


@pytest.mark.asyncio
async def test_core_validation_uses_the_complete_first_page_majority() -> None:
    class FirstPageClient:
        async def fetch_search_first_page(
            self,
            keyword: str,
        ) -> tuple[str, dict[str, Any]]:
            products = [
                (str(60_000_000 + index), f"Wireless Mouse {index}")
                for index in range(20)
            ]
            products.extend(
                (str(61_000_000 + index), f"Laptop Sleeve {index}")
                for index in range(16)
            )
            return _search_url(keyword), _payload(products, after="", total=800)

    profile = VisionProfile(
        product_name="Wireless mouse",
        category="Computer mice",
        product_type_terms=["wireless mouse"],
        distinctive_terms=["rechargeable"],
        keywords=[
            KeywordCandidate(phrase="wireless mouse", rationale="Exact type"),
            KeywordCandidate(phrase="rechargeable mouse", rationale="Visible feature"),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="wireless", rationale="Shopper root"),
            KeywordCandidate(phrase="mouse", rationale="Product root"),
        ],
        opportunity_seeds=[
            KeywordCandidate(phrase="laptop accessory", rationale="Adjacent demand")
        ],
        exclusions=["keyboard"],
        confidence=0.9,
        title_suggestion="Wireless Mouse Rechargeable",
        title_reason="Image-only hypothesis",
    )

    observation = await _collect_keyword_observation(
        FirstPageClient(),  # type: ignore[arg-type]
        candidate=SearchKeywordCandidate(
            phrase="wireless mouse",
            rationale="Precise image query",
            candidate_source="image_precise",
            intended_strategy="core",
        ),
        candidate_order=1,
        target_plid="12345678",
        profile=profile,
        max_pages=1,
        relevance_threshold=0.60,
        page_delay_seconds=0,
    )

    assert observation.relevance_status == "rejected_irrelevant"
    assert observation.validation_evidence["evaluated_first_page_results"] == 36
    assert observation.validation_evidence["matched_first_page_results"] == 20
    assert observation.relevance_score == pytest.approx(20 / 36)


def _opportunity_profile() -> VisionProfile:
    return VisionProfile(
        product_name="Rechargeable wireless gaming mouse",
        category="Computer mice",
        product_type_terms=["mouse", "wireless mouse"],
        distinctive_terms=["rechargeable", "gaming"],
        keywords=[
            KeywordCandidate(phrase="wireless mouse", rationale="Exact type"),
            KeywordCandidate(phrase="gaming mouse", rationale="Visible use"),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="wireless", rationale="Shopper root"),
            KeywordCandidate(phrase="mouse", rationale="Product root"),
        ],
        opportunity_seeds=[
            KeywordCandidate(phrase="mouse for laptop", rationale="Adjacent demand")
        ],
        exclusions=["keyboard"],
        confidence=0.9,
        title_suggestion="Rechargeable Wireless Gaming Mouse",
        title_reason="Image-only hypothesis",
    )


def _opportunity_candidate(
    *,
    autocomplete: bool = True,
) -> SearchKeywordCandidate:
    return SearchKeywordCandidate(
        phrase="mouse for laptop",
        rationale="Adjacent laptop-use demand",
        candidate_source=(
            "takealot_autocomplete" if autocomplete else "image_precise"
        ),
        intended_strategy="opportunity",
        seed="mouse for laptop" if autocomplete else None,
        seed_source="image_need_state" if autocomplete else "image_only_model",
        autocomplete_rank=1 if autocomplete else None,
    )


@pytest.mark.asyncio
async def test_same_seed_core_and_opportunity_is_requested_once_with_both_provenances() -> None:
    profile = _opportunity_profile().model_copy(
        update={
            "autocomplete_seeds": [
                KeywordCandidate(phrase="mouse", rationale="Core root")
            ],
            "opportunity_seeds": [
                KeywordCandidate(phrase="mouse", rationale="Need-state root")
            ],
        }
    )

    class SameSeedSuggestionClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def fetch_search_suggestions(self, keyword: str) -> list[str]:
            self.calls.append(keyword)
            return ["mouse for laptop"]

    client = SameSeedSuggestionClient()
    candidates, checks = await _discover_keyword_candidates(
        client,  # type: ignore[arg-type]
        profile=profile,
        source_title="Wireless Gaming Mouse",
        title_reference_terms=[],
        max_keywords=4,
    )
    candidate = next(item for item in candidates if item.phrase == "mouse for laptop")

    assert client.calls == ["mouse"]
    assert checks[0]["intended_strategies"] == ["core", "opportunity"]
    assert {
        str(item["intended_strategy"])
        for item in candidate.candidate_provenance
    } == {"core", "opportunity"}


@pytest.mark.asyncio
async def test_blue_ocean_candidate_requires_low_competition_and_early_target() -> None:
    observation = await _collect_keyword_observation(
        OpportunityGateSearchClient(
            [_opportunity_page(direct_competitors=2, target_rank=3)]
        ),  # type: ignore[arg-type]
        candidate=_opportunity_candidate(),
        candidate_order=1,
        target_plid="12345678",
        profile=_opportunity_profile(),
        max_pages=3,
        relevance_threshold=0.60,
        page_delay_seconds=0,
    )

    assert observation.relevance_status == "opportunity"
    assert observation.found is True
    assert observation.organic_rank == 3
    assert observation.validation_evidence[
        "direct_competitor_count_excluding_target_first_page"
    ] == 2
    assert observation.validation_evidence["target_on_first_page"] is True
    assert observation.validation_evidence["opportunity_max_direct_competitors"] == 2
    assert observation.validation_evidence["opportunity_max_organic_rank"] == 72
    assert observation.validation_evidence["opportunity_qualified"] is True
    assert observation.validation_evidence["opportunity_rejection_reasons"] == []


@pytest.mark.asyncio
async def test_blue_ocean_scans_third_cursor_page_when_rank_71_is_still_in_window() -> None:
    observation = await _collect_keyword_observation(
        OpportunityGateSearchClient(
            [
                _opportunity_page(direct_competitors=2, page_size=35),
                _opportunity_page(direct_competitors=0, page_size=35),
                _opportunity_page(
                    direct_competitors=0,
                    target_rank=1,
                    page_size=35,
                ),
            ]
        ),  # type: ignore[arg-type]
        candidate=_opportunity_candidate(),
        candidate_order=1,
        target_plid="12345678",
        profile=_opportunity_profile(),
        max_pages=3,
        relevance_threshold=0.60,
        page_delay_seconds=0,
    )

    assert observation.relevance_status == "opportunity"
    assert observation.found is True
    assert observation.organic_rank == 71
    assert observation.page_number == 3
    assert observation.page_rank == 1
    assert observation.pages_scanned == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "pages",
        "max_pages",
        "autocomplete",
        "expected_found",
        "expected_rank",
        "expected_reason",
    ),
    [
        (
            [_opportunity_page(direct_competitors=3, target_rank=4)],
            1,
            True,
            True,
            4,
            "too_many_direct_competitors",
        ),
        (
            [_opportunity_page(direct_competitors=2)],
            1,
            True,
            False,
            None,
            "target_not_found_within_72",
        ),
        (
            [
                _opportunity_page(direct_competitors=2),
                _opportunity_page(direct_competitors=0),
                _opportunity_page(direct_competitors=0, target_rank=1),
            ],
            3,
            True,
            False,
            None,
            "target_not_found_within_72",
        ),
        (
            [_opportunity_page(direct_competitors=2, target_rank=3)],
            1,
            False,
            True,
            3,
            "not_opportunity_autocomplete",
        ),
    ],
    ids=[
        "too-many-direct-competitors",
        "target-not-found",
        "target-after-organic-rank-72",
        "not-from-takealot-autocomplete",
    ],
)
async def test_blue_ocean_rejects_candidates_without_every_required_signal(
    pages: list[list[tuple[str, str]]],
    max_pages: int,
    autocomplete: bool,
    expected_found: bool,
    expected_rank: int | None,
    expected_reason: str,
) -> None:
    observation = await _collect_keyword_observation(
        OpportunityGateSearchClient(pages),  # type: ignore[arg-type]
        candidate=_opportunity_candidate(autocomplete=autocomplete),
        candidate_order=1,
        target_plid="12345678",
        profile=_opportunity_profile(),
        max_pages=max_pages,
        relevance_threshold=0.60,
        page_delay_seconds=0,
    )

    assert observation.relevance_status == "rejected_irrelevant"
    assert observation.found is expected_found
    assert observation.organic_rank == expected_rank
    assert observation.validation_evidence["opportunity_qualified"] is False
    assert expected_reason in observation.validation_evidence[
        "opportunity_rejection_reasons"
    ]


def test_search_products_accepts_only_unflagged_product_results() -> None:
    payload = _payload([("12345678", "Organic Projection Screen")], after="", total=1)
    organic = payload["sections"]["products"]["results"][0]
    different_type = {
        **organic,
        "type": "sponsored_product_views",
    }
    explicitly_sponsored = {
        **organic,
        "is_sponsored": True,
    }
    payload["sections"]["products"]["results"] = [
        different_type,
        explicitly_sponsored,
        organic,
    ]
    payload["sections"]["sponsored"] = {"results": [organic]}

    products, _ = _search_products(payload)

    assert [item["plid"] for item in products] == ["12345678"]


@pytest.mark.parametrize(
    "current_title",
    [
        "Wireless Mouse Silent Rechargeable Gaming",
        "Wireless Gaming Mouse Silent Rechargeable",
        "Mouse For Laptop Wireless Gaming Mouse Silent Rechargeable",
    ],
    ids=["contiguous-core", "hot-term-coverage", "adjacent-opportunity"],
)
def test_each_title_strategy_is_only_marked_forward_after_comparable_observation(
    current_title: str,
) -> None:
    title_suggestions = [
        "Wireless Mouse Silent Rechargeable Gaming",
        "Wireless Gaming Mouse Silent Rechargeable",
        "Mouse For Laptop Wireless Gaming Mouse Silent Rechargeable",
    ]
    validation = _title_validation(
        previous={
            "source_title": "Old Mouse Title",
            "title_suggestion": title_suggestions[0],
            "title_suggestions": title_suggestions,
            "analysis_id": 1,
            "ranks": {"wireless mouse": 41, "rechargeable mouse": 28},
        },
        current_title=current_title,
        current_results=[
            _observation("wireless mouse", 33),
            _observation("rechargeable mouse", 20),
        ],
    )

    assert validation["status"] == "observed_forward"
    assert validation["guarantee"] is False
    assert validation["causality"] == "observational_only"
    assert validation["matched_suggestion"] == current_title
    assert validation["comparisons"][0]["delta"] == 8


def test_title_validation_requires_every_strategy_baseline_keyword() -> None:
    title = "Wireless Gaming Mouse Silent Rechargeable"
    validation = _title_validation(
        previous={
            "source_title": "Old Mouse Title",
            "title_suggestions": [title],
            "issued_strategies": [
                {
                    "strategy": "hot_term_coverage",
                    "title": title,
                    "evidence_keywords": [
                        "wireless mouse",
                        "wireless gaming mouse",
                    ],
                }
            ],
            "ranks": {
                "wireless mouse": 41,
                "wireless gaming mouse": 35,
                "mouse for laptop": None,
            },
        },
        current_title=title,
        current_results=[
            _observation("wireless mouse", 30),
            _observation("wireless gaming mouse", None),
        ],
    )

    assert validation["status"] == "insufficient_comparable_evidence"
    assert validation["required_keywords"] == [
        "wireless mouse",
        "wireless gaming mouse",
    ]
    assert validation["missing_keywords"] == ["wireless gaming mouse"]
    assert validation["comparisons"][0]["delta"] == 11


def test_title_validation_never_drops_an_issued_target_without_a_baseline() -> None:
    title = "Wireless Gaming Mouse Silent Rechargeable"
    previous = {
        "source_title": "Old Mouse Title",
        "title_suggestions": [title],
        "issued_strategies": [
            {
                "strategy": "hot_term_coverage",
                "title": title,
                "evidence_keywords": [
                    "wireless mouse",
                    "wireless gaming mouse",
                ],
            }
        ],
        "ranks": {"wireless mouse": 10},
    }
    validation = _title_validation(
        previous=previous,
        current_title=title,
        current_results=[
            _observation("wireless mouse", 5),
            _observation("wireless gaming mouse", 4),
        ],
    )
    candidates = _inject_comparison_resample_candidates(
        [],
        previous=previous,
        current_title=title,
        max_keywords=4,
    )

    assert validation["status"] == "insufficient_comparable_evidence"
    assert validation["required_keywords"] == [
        "wireless mouse",
        "wireless gaming mouse",
    ]
    assert validation["missing_baseline_keywords"] == [
        "wireless gaming mouse"
    ]
    assert [item.phrase for item in candidates] == [
        "wireless mouse",
        "wireless gaming mouse",
    ]
    assert candidates[1].comparison_role == "primary"
    assert candidates[1].comparison_baseline_rank is None


@pytest.mark.parametrize(
    ("current_title", "expected_status", "primary_keyword", "secondary_keyword"),
    [
        (
            "Mouse For Laptop Wireless Gaming Mouse",
            "observed_forward",
            "mouse for laptop",
            "wireless mouse",
        ),
        (
            "Wireless Mouse Silent Rechargeable",
            "no_observed_forward",
            "wireless mouse",
            "mouse for laptop",
        ),
    ],
    ids=["adjacent-uses-only-adjacent-target", "core-uses-only-core-target"],
)
def test_title_validation_uses_only_the_matched_strategy_target_queries(
    current_title: str,
    expected_status: str,
    primary_keyword: str,
    secondary_keyword: str,
) -> None:
    validation = _title_validation(
        previous={
            "source_title": "Old Mouse Title",
            "title_suggestions": [
                "Wireless Mouse Silent Rechargeable",
                "Mouse For Laptop Wireless Gaming Mouse",
            ],
            "issued_strategies": [
                {
                    "strategy": "contiguous_core",
                    "title": "Wireless Mouse Silent Rechargeable",
                    "evidence_keywords": ["wireless mouse"],
                },
                {
                    "strategy": "adjacent_opportunity",
                    "title": "Mouse For Laptop Wireless Gaming Mouse",
                    "evidence_keywords": ["mouse for laptop"],
                },
            ],
            "ranks": {"wireless mouse": 10, "mouse for laptop": 10},
        },
        current_title=current_title,
        current_results=[
            _observation("wireless mouse", 20),
            _observation("mouse for laptop", 5),
        ],
    )

    assert validation["status"] == expected_status
    assert [item["keyword"] for item in validation["comparisons"]] == [
        primary_keyword
    ]
    assert [item["keyword"] for item in validation["secondary_comparisons"]] == [
        secondary_keyword
    ]


def test_explicit_empty_issued_titles_never_fall_back_to_cleaned_source() -> None:
    cleaned = "Wireless Mouse Silent"
    previous = {
        "source_title": "Old Mouse Title",
        "title_suggestion": cleaned,
        "title_suggestions": [],
        "ranks": {"wireless mouse": 12},
    }
    validation = _title_validation(
        previous=previous,
        current_title=cleaned,
        current_results=[_observation("wireless mouse", 8)],
    )
    candidates = _inject_comparison_resample_candidates(
        [
            SearchKeywordCandidate(
                phrase="gaming mouse",
                rationale="Fresh candidate",
                candidate_source="image_precise",
                intended_strategy="core",
            )
        ],
        previous=previous,
        current_title=cleaned,
        max_keywords=4,
    )

    assert validation["status"] == "changed_to_other_title"
    assert len(candidates) == 1
    assert candidates[0].comparison_baseline_rank is None


def test_comparison_injection_prioritizes_targets_and_preserves_fresh_classification() -> None:
    current_title = "Mouse For Laptop Wireless Gaming Mouse"
    fresh = [
        SearchKeywordCandidate(
            phrase="wireless mouse",
            rationale="Fresh autocomplete",
            candidate_source="takealot_autocomplete",
            intended_strategy="core",
            autocomplete_rank=2,
        ),
        SearchKeywordCandidate(
            phrase="gaming mouse",
            rationale="Fresh image term",
            candidate_source="image_precise",
            intended_strategy="core",
        ),
    ]
    candidates = _inject_comparison_resample_candidates(
        fresh,
        previous={
            "source_title": "Old Mouse Title",
            "title_suggestions": [current_title],
            "issued_strategies": [
                {
                    "strategy": "adjacent_opportunity",
                    "title": current_title,
                    "evidence_keywords": ["mouse for laptop"],
                }
            ],
            "ranks": {"mouse for laptop": 15, "wireless mouse": 41},
        },
        current_title=current_title,
        max_keywords=3,
    )

    assert [item.phrase for item in candidates] == [
        "mouse for laptop",
        "wireless mouse",
        "gaming mouse",
    ]
    assert candidates[0].candidate_source == "comparison_resample"
    assert candidates[0].comparison_role == "primary"
    assert candidates[1].candidate_source == "takealot_autocomplete"
    assert candidates[1].intended_strategy == "core"
    assert candidates[1].comparison_baseline_rank == 41
    assert candidates[1].comparison_role == "secondary"


@pytest.mark.asyncio
async def test_comparison_resample_forces_full_scan_without_entering_title_strategies() -> None:
    pages = [
        _opportunity_page(direct_competitors=0),
        _opportunity_page(direct_competitors=0, target_rank=1),
    ]
    observation = await _collect_keyword_observation(
        OpportunityGateSearchClient(pages),  # type: ignore[arg-type]
        candidate=SearchKeywordCandidate(
            phrase="legacy mouse query",
            rationale="Baseline resample",
            candidate_source="comparison_resample",
            intended_strategy="comparison",
            comparison_baseline_rank=55,
            comparison_role="primary",
            comparison_strategy="contiguous_core",
        ),
        candidate_order=1,
        target_plid="12345678",
        profile=_opportunity_profile(),
        max_pages=2,
        relevance_threshold=0.60,
        page_delay_seconds=0,
    )

    assert observation.relevance_status == "comparison_resample"
    assert observation.found is True
    assert observation.organic_rank == 37
    assert observation.pages_scanned == 2
    assert observation.validation_evidence["comparison_baseline_rank"] == 55
    assert observation.validation_evidence["comparison_role"] == "primary"


@pytest.mark.asyncio
async def test_comparison_resample_without_old_rank_still_forces_public_search() -> None:
    observation = await _collect_keyword_observation(
        OpportunityGateSearchClient(
            [
                _opportunity_page(direct_competitors=0),
                _opportunity_page(direct_competitors=0, target_rank=1),
            ]
        ),  # type: ignore[arg-type]
        candidate=SearchKeywordCandidate(
            phrase="wireless gaming mouse",
            rationale="Issued target without baseline",
            candidate_source="comparison_resample",
            intended_strategy="comparison",
            comparison_role="primary",
            comparison_strategy="hot_term_coverage",
        ),
        candidate_order=1,
        target_plid="12345678",
        profile=_opportunity_profile(),
        max_pages=2,
        relevance_threshold=0.60,
        page_delay_seconds=0,
    )

    assert observation.relevance_status == "comparison_resample"
    assert observation.found is True
    assert observation.organic_rank == 37
    assert observation.pages_scanned == 2
    assert observation.validation_evidence["comparison_baseline_rank"] is None


@pytest.mark.asyncio
async def test_duplicate_autocomplete_keeps_core_and_opportunity_provenance() -> None:
    selected: list[SearchKeywordCandidate] = []
    core = SearchKeywordCandidate(
        phrase="mouse for laptop",
        rationale="Core seed",
        candidate_source="takealot_autocomplete",
        intended_strategy="core",
        seed="mouse for laptop",
        seed_source="image_shopper_root",
        autocomplete_rank=5,
    )
    opportunity = SearchKeywordCandidate(
        phrase="mouse for laptop",
        rationale="Adjacent seed",
        candidate_source="takealot_autocomplete",
        intended_strategy="opportunity",
        seed="mouse for laptop",
        seed_source="image_need_state",
        autocomplete_rank=1,
    )
    _append_unique_candidate(selected, core, 4)
    _append_unique_candidate(selected, opportunity, 4)

    observation = await _collect_keyword_observation(
        OpportunityGateSearchClient(
            [_opportunity_page(direct_competitors=2, target_rank=3)]
        ),  # type: ignore[arg-type]
        candidate=selected[0],
        candidate_order=1,
        target_plid="12345678",
        profile=_opportunity_profile(),
        max_pages=2,
        relevance_threshold=0.60,
        page_delay_seconds=0,
    )

    assert len(selected) == 1
    assert observation.relevance_status == "opportunity"
    assert observation.validation_evidence["autocomplete_rank"] == 1
    assert observation.validation_evidence["intended_strategies"] == [
        "core",
        "opportunity",
    ]


def _observation(
    keyword: str,
    rank: int | None,
    *,
    relevance_status: str = "accepted",
    validation_evidence: dict[str, Any] | None = None,
) -> Any:
    from takealot_ops.search_ranking.service import KeywordObservation

    return KeywordObservation(
        keyword=keyword,
        candidate_order=1,
        relevance_status=relevance_status,
        relevance_score=1.0,
        validation_evidence=validation_evidence or {},
        total_num_found=100,
        pages_scanned=1,
        found=rank is not None,
        page_number=1 if rank is not None else None,
        page_rank=rank,
        organic_rank=rank,
        row_number=1 if rank is not None else None,
        column_number=1 if rank is not None else None,
        target_url=None,
        observed_at=datetime(2026, 8, 7),
    )


def test_web_reads_local_status_and_missing_key_never_starts_external_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'ranking-web.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
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
                    image_url="http://media.takealot.com/covers_images/test/s.file",
                    status="buyable",
                    takealot_available_stock=1,
                    captured_at=datetime.now(UTC),
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
    assert listing.json()["eligibility"]["source"] == (
        "authenticated_store_seller_offers"
    )
    assert listing.json()["items"][0]["offer_id"] == "offer-web"
    assert run.status_code == 503
    assert "DASHSCOPE_API_KEY" in run.json()["detail"]


@pytest.mark.asyncio
async def test_only_fresh_buyable_positive_stock_offers_enter_list_or_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'eligibility.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    now = datetime.now(UTC)
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    with Session(engine) as session, session.begin():
        for offer_id, status, stock, captured_at in (
            ("eligible", "buyable", 1, now),
            ("disabled", "disabled_by_seller", 8, now),
            ("zero-stock", "buyable", 0, now),
            ("stale", "buyable", 2, now - timedelta(hours=40)),
        ):
            session.add(
                OfferCurrent(
                    offer_id=offer_id,
                    productline_id="12345678",
                    sku=offer_id,
                    title="Wireless Mouse",
                    image_url="http://media.takealot.com/covers_images/test/s.file",
                    status=status,
                    takealot_available_stock=stock,
                    seller_available_stock=0,
                    captured_at=captured_at,
                )
            )
    engine.dispose()
    FakeVisionClient.calls = 0
    service = SearchRankingService(
        tmp_path,
        vision_client_factory=FakeVisionClient,
        search_client_factory=FakeSearchClient,  # type: ignore[arg-type]
    )

    listing = service.list_payload()

    assert [item["offer_id"] for item in listing["items"]] == ["eligible"]
    assert listing["items"][0]["image_url"].startswith("https://")
    assert listing["eligibility"]["current_offer_count"] == 4
    assert listing["eligibility"]["eligible_count"] == 1
    assert listing["eligibility"]["excluded_count"] == 3
    assert listing["eligibility"]["excluded_reasons"] == {
        "not_buyable": 1,
        "no_available_stock": 1,
        "stale_snapshot": 1,
    }

    for offer_id in ("disabled", "zero-stock", "stale"):
        with pytest.raises(SearchRankingInputError, match="未调用模型"):
            await service.analyze_offer(offer_id)
    assert FakeVisionClient.calls == 0


def test_latest_failed_attempt_does_not_replace_last_completed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'stable-detail.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    observed_at = datetime.now(UTC)
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    with Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="offer-stable",
                productline_id="12345678",
                sku="STABLE-01",
                title="Silent Wireless Gaming Mouse",
                image_url="http://media.takealot.com/covers_images/test/s.file",
                status="buyable",
                takealot_available_stock=1,
                captured_at=observed_at,
            )
        )
        completed = SearchRankingAnalysis(
            offer_id="offer-stable",
            productline_id="12345678",
            sku="STABLE-01",
            source_title="Silent Wireless Gaming Mouse",
            source_image_url="https://media.takealot.com/covers_images/test/s.file",
            cache_key="c" * 64,
            provider="qwen",
            model="qwen3.7-plus",
            prompt_version=PROMPT_VERSION,
            status="completed",
            product_name="Gaming mouse",
            category="Computer mice",
            confidence=Decimal("0.9000"),
            vision_payload={
                "profile": {"distinctive_terms": ["gaming"]},
                "vision_stage_completed": True,
                "usage": {"total_tokens": 200},
            },
            vision_reused=False,
            title_suggestion="Wireless Gaming Mouse Silent",
            title_reason="Validated core phrase",
            title_validation={"status": "baseline_created"},
            created_at=observed_at,
            completed_at=observed_at,
        )
        session.add(completed)
        session.flush()
        completed_id = completed.id
        session.add(
            SearchRankingKeywordResult(
                analysis_id=completed.id,
                keyword="wireless gaming mouse",
                candidate_order=1,
                relevance_status="accepted",
                relevance_score=Decimal("0.9000"),
                validation_evidence={
                    "candidate_source": "takealot_autocomplete",
                    "intended_strategy": "core",
                    "autocomplete_rank": 1,
                },
                total_num_found=100,
                pages_scanned=1,
                found=True,
                page_number=1,
                page_rank=8,
                organic_rank=8,
                row_number=2,
                column_number=4,
                columns_per_row=4,
                target_url="https://www.takealot.com/example/PLID12345678",
                observed_at=observed_at,
            )
        )
        failed = SearchRankingAnalysis(
            offer_id="offer-stable",
            productline_id="12345678",
            sku="STABLE-01",
            source_title="Silent Wireless Gaming Mouse",
            source_image_url="https://media.takealot.com/covers_images/test/s.file",
            cache_key="d" * 64,
            provider="doubao",
            model="doubao-seed-2-0-lite-260215",
            prompt_version=PROMPT_VERSION,
            status="failed",
            vision_reused=False,
            error="provider unavailable",
            created_at=observed_at + timedelta(minutes=1),
            completed_at=observed_at + timedelta(minutes=1),
        )
        session.add(failed)
        session.flush()
        failed_id = failed.id
    engine.dispose()

    service = SearchRankingService(tmp_path)
    detail = service.detail_payload("offer-stable")
    listing = service.list_payload()

    assert detail is not None
    assert detail["analysis"]["id"] == completed_id
    assert len(detail["analysis"]["title_strategies"]) == 3
    assert detail["latest_attempt"]["id"] == failed_id
    assert detail["latest_attempt"]["status"] == "failed"
    assert listing["items"][0]["latest_analysis"]["id"] == completed_id
    assert listing["items"][0]["latest_analysis"]["status"] == "completed"


@pytest.mark.asyncio
async def test_doubao_failure_falls_back_to_qwen_with_forced_schema_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "qwen-test-key")
    monkeypatch.setenv("ARK_API_KEY", "doubao-test-key")
    runtime = SearchRankingRuntimeSettings.from_env(tmp_path)
    requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    class FakeAsyncClient:
        def __init__(self, **_: Any) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            requests.append((url, headers, json))
            if "volces" in url:
                return httpx.Response(400, json={"error": "unsupported test request"})
            arguments = VisionProfile(
                product_name="Rechargeable wireless mouse",
                category="Computer mice",
                product_type_terms=["wireless mouse"],
                distinctive_terms=["rechargeable"],
                keywords=[
                    KeywordCandidate(phrase="wireless mouse", rationale="Exact type"),
                    KeywordCandidate(
                        phrase="rechargeable mouse", rationale="Visible charging port"
                    ),
                ],
                autocomplete_seeds=[
                    KeywordCandidate(phrase="wireless", rationale="Shopper root"),
                    KeywordCandidate(
                        phrase="wireless mouse", rationale="Exact shopper root"
                    ),
                ],
                opportunity_seeds=[
                    KeywordCandidate(
                        phrase="mouse for laptop", rationale="Adjacent use case"
                    )
                ],
                exclusions=["keyboard combo"],
                confidence=0.9,
                title_suggestion="Rechargeable Wireless Mouse",
                title_reason="Lead with the exact product type.",
            ).model_dump_json()
            return httpx.Response(
                200,
                json={
                    "id": "doubao-response",
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "type": "function",
                                        "function": {
                                            "name": "submit_takealot_product_profile",
                                            "arguments": arguments,
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1000,
                        "completion_tokens": 500,
                        "total_tokens": 1500,
                    },
                },
            )

    monkeypatch.setattr(search_ranking_service, "_thumbnail_data_url", lambda *_: "data:image/jpeg;base64,AA==")
    monkeypatch.setattr(search_ranking_service.httpx, "AsyncClient", FakeAsyncClient)

    result = await OpenAICompatibleProductVisionClient(runtime).identify(
        image_url="https://media.takealot.com/covers_images/test.jpg",
        reference_title="Rechargeable Wireless Mouse",
    )

    assert result.provider == "qwen"
    assert result.model == "qwen3.7-plus"
    assert result.estimated_cost_cny == 0.006
    assert [url for url, _, _ in requests] == [
        "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    ]
    assert requests[0][2]["thinking"] == {"type": "disabled"}
    assert requests[1][2]["enable_thinking"] is False
    assert requests[1][2]["tool_choice"]["function"]["name"] == (
        "submit_takealot_product_profile"
    )
    serialized_request = str(requests[1][2])
    assert "Rechargeable Wireless Mouse" not in serialized_request
    assert result.provider_attempts == (
        {
            "provider": "doubao",
            "status": "request_or_schema_failed",
            "reason": "SearchRankingConfigurationError",
        },
        {
            "provider": "qwen",
            "status": "accepted",
            "source_title_similarity": 1.0,
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "total_tokens": 1500,
            },
            "estimated_cost_cny": 0.006,
        },
    )


@pytest.mark.asyncio
async def test_identity_fallback_reports_all_successful_provider_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "qwen-test-key")
    monkeypatch.setenv("ARK_API_KEY", "doubao-test-key")
    runtime = SearchRankingRuntimeSettings.from_env(tmp_path)
    mouse_profile = _opportunity_profile()
    chair_profile = mouse_profile.model_copy(
        update={"product_name": "Dining Chair"}
    )
    lamp_profile = mouse_profile.model_copy(
        update={"product_name": "Floor Lamp"}
    )
    client = OpenAICompatibleProductVisionClient(runtime)
    mode = "fallback_accepted"

    async def fake_request(
        _: httpx.AsyncClient,
        *,
        provider: Any,
        image_data_url: str,
    ) -> VisionCallResult:
        del image_data_url
        if provider.name == "doubao":
            return VisionCallResult(
                profile=chair_profile,
                provider="doubao",
                model=provider.model,
                response_id="doubao-conflict",
                usage={
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                },
                estimated_cost_cny=0.001,
            )
        return VisionCallResult(
            profile=mouse_profile if mode == "fallback_accepted" else lamp_profile,
            provider="qwen",
            model=provider.model,
            response_id="qwen-result",
            usage={
                "input_tokens": 200,
                "output_tokens": 100,
                "total_tokens": 300,
            },
            estimated_cost_cny=0.002,
        )

    monkeypatch.setattr(
        search_ranking_service,
        "_thumbnail_data_url",
        lambda *_: "data:image/jpeg;base64,AA==",
    )
    monkeypatch.setattr(client, "_request_provider", fake_request)

    accepted = await client.identify(
        image_url="https://media.takealot.com/covers_images/test.jpg",
        reference_title="Wireless Mouse",
    )
    mode = "both_conflict"
    conflicted = await client.identify(
        image_url="https://media.takealot.com/covers_images/test.jpg",
        reference_title="Wireless Mouse",
    )

    assert accepted.provider == "qwen"
    assert accepted.usage == {
        "input_tokens": 300,
        "output_tokens": 150,
        "total_tokens": 450,
    }
    assert accepted.estimated_cost_cny == 0.003
    assert [item["status"] for item in accepted.provider_attempts] == [
        "identity_conflict",
        "accepted",
    ]
    assert accepted.provider_attempts[0]["usage"]["total_tokens"] == 150
    assert accepted.provider_attempts[1]["usage"]["total_tokens"] == 300
    assert conflicted.usage == accepted.usage
    assert conflicted.estimated_cost_cny == 0.003
    assert conflicted.profile.confidence == 0.49
    assert conflicted.cache_profile is not None
    assert conflicted.cache_profile.confidence == mouse_profile.confidence
    assert [item["status"] for item in conflicted.provider_attempts] == [
        "identity_conflict",
        "identity_conflict",
    ]


@pytest.mark.asyncio
async def test_schema_invalid_200_response_usage_is_included_in_fallback_total(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "qwen-test-key")
    monkeypatch.setenv("ARK_API_KEY", "doubao-test-key")
    runtime = SearchRankingRuntimeSettings.from_env(tmp_path)

    class FakeAsyncClient:
        def __init__(self, **_: Any) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            del headers, json
            if "volces" in url:
                return httpx.Response(
                    200,
                    json={
                        "id": "invalid-profile",
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 50,
                            "total_tokens": 150,
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": "accepted-profile",
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "name": "submit_takealot_product_profile",
                                            "arguments": _opportunity_profile().model_dump_json(),
                                        }
                                    }
                                ]
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 200,
                        "completion_tokens": 100,
                        "total_tokens": 300,
                    },
                },
            )

    monkeypatch.setattr(
        search_ranking_service,
        "_thumbnail_data_url",
        lambda *_: "data:image/jpeg;base64,AA==",
    )
    monkeypatch.setattr(search_ranking_service.httpx, "AsyncClient", FakeAsyncClient)

    result = await OpenAICompatibleProductVisionClient(runtime).identify(
        image_url="https://media.takealot.com/covers_images/test.jpg",
        reference_title="Wireless Gaming Mouse",
    )

    assert result.provider == "qwen"
    assert result.usage == {
        "input_tokens": 300,
        "output_tokens": 150,
        "total_tokens": 450,
    }
    assert result.estimated_cost_cny == pytest.approx(0.00144)
    assert result.provider_attempts[0]["status"] == "request_or_schema_failed"
    assert result.provider_attempts[0]["usage"]["total_tokens"] == 150
    assert result.provider_attempts[0]["estimated_cost_cny"] == pytest.approx(
        0.00024
    )


@pytest.mark.asyncio
async def test_all_schema_failures_persist_known_cost_without_creating_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'all-schema-failed.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "qwen-test-key")
    monkeypatch.setenv("ARK_API_KEY", "doubao-test-key")
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    with Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="offer-schema-failed",
                productline_id="12345678",
                sku="SCHEMA-FAIL-01",
                title="Wireless Gaming Mouse",
                image_url="http://media.takealot.com/covers_images/test/s.file",
                status="buyable",
                takealot_available_stock=1,
                captured_at=datetime.now(UTC),
            )
        )
    engine.dispose()

    class FakeAsyncClient:
        posts = 0

        def __init__(self, **_: Any) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            del url, headers, json
            type(self).posts += 1
            return httpx.Response(
                200,
                json={
                    "id": f"invalid-{type(self).posts}",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "total_tokens": 150,
                    },
                },
            )

    monkeypatch.setattr(
        search_ranking_service,
        "_thumbnail_data_url",
        lambda *_: "data:image/jpeg;base64,AA==",
    )
    monkeypatch.setattr(search_ranking_service.httpx, "AsyncClient", FakeAsyncClient)
    service = SearchRankingService(tmp_path)

    for expected_posts in (2, 4):
        with pytest.raises(SearchRankingProviderError, match="均未返回可用"):
            await service.analyze_offer("offer-schema-failed")
        detail = service.detail_payload("offer-schema-failed")
        assert detail is not None
        assert detail["analysis"] is None
        assert detail["latest_attempt"]["vision_stage_completed"] is False
        assert detail["latest_attempt"]["vision_reused"] is False
        assert detail["latest_attempt"]["usage"] == {
            "input_tokens": 200,
            "output_tokens": 100,
            "total_tokens": 300,
        }
        assert detail["latest_attempt"]["estimated_cost_cny"] == pytest.approx(
            0.00084
        )
        assert FakeAsyncClient.posts == expected_posts
