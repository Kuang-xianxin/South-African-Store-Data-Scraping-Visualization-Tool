from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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
    SearchRankingRuntimeSettings,
    SearchRankingService,
    VisionCallResult,
    VisionProfile,
    _build_title_suggestion,
    _collect_keyword_observation,
    _cross_check_image_profile,
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
from takealot_ops.storage.models import OfferCurrent


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
                for index in range(8)
            )
            products.extend(
                (str(75_000_000 + index), f"Laptop Sleeve Style {index}")
                for index in range(27)
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
                title="Silent Rechargeable Wireless Mouse",
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

    accepted, second_accepted, opportunity, rejected = first["analysis"]["keywords"]
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
    assert opportunity["validation_evidence"]["matched_first_page_results"] == 9
    assert opportunity["validation_evidence"]["evaluated_first_page_results"] == 36
    assert opportunity["found"] is True
    assert rejected["relevance_status"] == "rejected_irrelevant"
    assert rejected["pages_scanned"] == 1
    assert rejected["found"] is False
    assert first["analysis"]["usage"]["total_tokens"] == 200
    assert first["analysis"]["provider"] == "qwen"
    assert first["analysis"]["estimated_cost_cny"] == 0.00088
    assert first["analysis"]["title_suggestion"] == (
        "Wireless Mouse Silent Rechargeable"
    )
    assert first["analysis"]["profile"]["title_suggestion"] == (
        "Wireless Mouse Silent Rechargeable"
    )
    assert first["analysis"]["opportunity_title_suggestion"] == (
        "Mouse For Laptop Wireless Silent Rechargeable"
    )
    assert first["analysis"]["recognition"]["model_received_source_title"] is False
    assert first["analysis"]["recognition"]["title_reference_terms"] == [
        "mouse",
        "wireless mouse",
        "rechargeable",
        "silent",
    ]
    assert "第一核心词" in first["analysis"]["title_reason"]
    assert second["analysis"]["vision_reused"] is True
    assert second["analysis"]["usage"]["total_tokens"] == 0
    assert second["analysis"]["title_validation"]["status"] == "pending_title_change"
    assert FakeVisionClient.calls == 1
    assert all(client.next_calls == 2 for client in clients)


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
        },
    )
