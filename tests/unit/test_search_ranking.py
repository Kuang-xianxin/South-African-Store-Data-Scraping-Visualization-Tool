from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from takealot_ops.competitors.api import CompetitorPublicClient
from takealot_ops.search_ranking.service import (
    AdjacentDemandCandidate,
    DecisionParameterChoice,
    DecisionParameterConfirmation,
    FusionVisionProfile,
    KeywordCandidate,
    KeywordObservation,
    LocalizedVisionProfile,
    OpenAICompatibleProductVisionClient,
    PROMPT_VERSION,
    ProductFactConfirmation,
    ProductFactInput,
    ProductFactRevocation,
    SearchKeywordCandidate,
    SearchRankingInputError,
    SearchRankingProviderError,
    SearchRankingRuntimeSettings,
    SearchRankingService,
    VisionCallResult,
    VisionProfile,
    _PacedSearchClient,
    _PublicRequestThrottle,
    _SharedAutocompleteCache,
    _append_unique_candidate,
    _analysis_payload,
    _analysis_cache_key,
    _autocomplete_fit_score,
    _complete_root_expansion_input,
    _build_hot_term_title_suggestion,
    _build_title_suggestion,
    _build_title_strategies,
    _collect_keyword_observation,
    _collect_shopper_journey,
    _confirmed_identity_fact_cross_check,
    _cross_check_image_profile,
    _discover_keyword_candidates,
    _enrich_profile_with_confirmed_facts,
    _inject_comparison_resample_candidates,
    _opportunity_gate_from_result,
    _opportunity_phrase_safety,
    _normalize_title_score_payload,
    _previous_analysis_snapshot,
    _result_page_learning_seed,
    _root_expansion_relevance_decision,
    _search_products,
    _semantic_relation_evidence,
    _title_validation,
    _title_strategy_keywords,
    _title_matches_terms,
    _title_parameter_candidates,
    _title_root_expansions,
    _title_score_payload,
    _validated_chat_profile,
    _variant_family_cache_material,
    _variant_family_profile,
)
from takealot_ops.search_ranking import service as search_ranking_service
from takealot_ops.erp.web import create_app
from takealot_ops.storage.migrations import (
    create_engine_for_database_url,
    create_schema,
)
from takealot_ops.storage.models import (
    OfferCurrent,
    SearchAutocompleteCache,
    SearchAutocompleteSnapshot,
    SearchRankingAnalysis,
    SearchRankingDecisionParameterConfirmation,
    SearchRankingKeywordResult,
    SearchRankingProductFact,
)
from takealot_ops.storage.store_context import store_scope


class FakeVisionClient:
    calls = 0

    def __init__(self, _: SearchRankingRuntimeSettings) -> None:
        pass

    async def identify(
        self,
        *,
        image_url: str,
        reference_title: str,
        variant_context: Mapping[str, Any] | None = None,
    ) -> VisionCallResult:
        del image_url, reference_title, variant_context
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


def test_variant_family_profile_keeps_each_offer_parameter_separate() -> None:
    profile = _variant_family_profile(
        [
            {
                "offer_id": "double",
                "productline_id": "102695333",
                "sku": "FOAM-DOUBLE",
                "title": "2 Inch 7 Zone Memory Foam Double",
                "image_url": "https://media.takealot.com/double.file",
                "available_stock": 2,
            },
            {
                "offer_id": "king",
                "productline_id": "102695333",
                "sku": "FOAM-KING",
                "title": "2 Inch 7 Zone Memory Foam King",
                "image_url": "https://media.takealot.com/king.file",
                "available_stock": 3,
            },
            {
                "offer_id": "king-xl",
                "productline_id": "102695333",
                "sku": "FOAM-KING-XL",
                "title": "2 Inch 7 Zone Memory Foam King XL",
                "image_url": "https://media.takealot.com/king-xl.file",
                "available_stock": 4,
            },
        ],
        representative_offer_id="double",
    )

    assert profile["shared_title"] == "2 Inch 7 Zone Memory Foam"
    assert profile["variant_count"] == 3
    assert profile["image_evidence_scope"] == "representative_offer_only"
    assert profile["variant_parameters_visually_verified"] is False
    assert {
        item["offer_id"]: [parameter["value"] for parameter in item["parameters"]]
        for item in profile["variants"]
    } == {
        "double": ["Double"],
        "king": ["King"],
        "king-xl": ["King XL"],
    }
    assert all(
        parameter["parameter_type"] == "size"
        for item in profile["variants"]
        for parameter in item["parameters"]
    )


def test_title_score_is_title_quality_only_and_ignores_search_performance() -> None:
    profile = VisionProfile(
        product_name="Rechargeable wireless gaming mouse",
        category="Computer mice",
        product_type_terms=["wireless mouse"],
        distinctive_terms=["rechargeable", "gaming"],
        keywords=[
            KeywordCandidate(phrase="wireless gaming mouse", rationale="Exact intent"),
            KeywordCandidate(phrase="rechargeable mouse", rationale="Feature intent"),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="wireless mouse", rationale="Product instinct"),
            KeywordCandidate(phrase="gaming mouse", rationale="Use instinct"),
        ],
        opportunity_seeds=[
            KeywordCandidate(phrase="mouse for laptop", rationale="Adjacent need")
        ],
        exclusions=["keyboard"],
        confidence=0.92,
        title_suggestion="Wireless Gaming Mouse Rechargeable",
        title_reason="Product type first",
    )
    observation = KeywordObservation(
        keyword="wireless gaming mouse",
        candidate_order=1,
        relevance_status="accepted",
        relevance_score=0.8,
        validation_evidence={},
        total_num_found=420,
        pages_scanned=1,
        found=True,
        page_number=1,
        page_rank=20,
        organic_rank=20,
        row_number=5,
        column_number=4,
        target_url="https://www.takealot.com/example/PLID12345678",
        observed_at=datetime.now(UTC).replace(tzinfo=None),
    )
    scored = _title_score_payload(
        source_title="Wireless Gaming Mouse Rechargeable",
        profile=profile,
        recognition={
            "identity_difference_level": "aligned",
            "source_title_similarity": 1.0,
            "title_identity_support": True,
            "title_identity_supported_terms": ["wireless mouse"],
        },
        observations=[observation],
    )
    changed_search_performance = _title_score_payload(
        source_title="Wireless Gaming Mouse Rechargeable",
        profile=profile,
        recognition={
            "identity_difference_level": "aligned",
            "source_title_similarity": 1.0,
            "title_identity_support": True,
            "title_identity_supported_terms": ["wireless mouse"],
        },
        observations=[
            replace(
                observation,
                relevance_score=0.05,
                found=False,
                page_number=None,
                page_rank=None,
                organic_rank=None,
                row_number=None,
                column_number=None,
            )
        ],
    )

    assert scored["score"] == changed_search_performance["score"]
    assert scored["components"] == changed_search_performance["components"]
    assert scored["scoring_version"] == "evidence-title-v2"
    assert scored["title_quality_only"] is True
    assert [item["key"] for item in scored["components"]] == [
        "image_title_alignment",
        "product_type_expression",
        "validated_search_term_coverage",
        "evidence_backed_detail_quality",
        "title_readability",
    ]
    assert sum(item["weight"] for item in scored["components"]) == 100
    assert scored["evidence_coverage"] == 100
    assert any("均不参与标题分" in item for item in scored["limitations"])
    assert {item["key"] for item in scored["non_scoring_signals"]} == {
        "organic_search_visibility",
        "first_page_same_type_relevance",
        "root_expansion_rank",
    }

    weaker_title = _title_score_payload(
        source_title="Wireless Mouse",
        profile=profile,
        recognition={"identity_difference_level": "aligned"},
        observations=[observation],
    )
    assert weaker_title["score"] < scored["score"]

    without_pages = _title_score_payload(
        source_title="Wireless Gaming Mouse Rechargeable",
        profile=profile.model_copy(update={"distinctive_terms": []}),
        recognition={"identity_difference_level": "aligned"},
        observations=[],
    )
    query_without_pages = next(
        item
        for item in without_pages["components"]
        if item["key"] == "validated_search_term_coverage"
    )
    assert query_without_pages["available"] is False
    assert query_without_pages["score"] is None
    assert without_pages["evidence_coverage"] == 55
    assert without_pages["band"] == "insufficient_evidence"


def test_legacy_title_score_is_locally_projected_without_ranking_components() -> None:
    legacy = {
        "score": 49,
        "scoring_version": "evidence-title-v1",
        "current_title": "Corduroy Lazy Sofa Chair Foldable Multi Functional Seat Blue",
        "components": [
            {
                "key": "image_title_alignment",
                "weight": 20,
                "available": True,
                "score": 20,
                "summary": "图题身份一致",
                "evidence": [],
            },
            {
                "key": "validated_search_term_coverage",
                "weight": 20,
                "available": True,
                "score": 10,
                "summary": "覆盖一半验证词",
                "evidence": [],
            },
            {
                "key": "organic_search_visibility",
                "weight": 25,
                "available": True,
                "score": 2,
                "summary": "未定位",
                "evidence": [],
            },
            {
                "key": "first_page_same_type_relevance",
                "weight": 15,
                "available": True,
                "score": 2.1,
                "summary": "首页同类占比14%",
                "evidence": [],
            },
            {
                "key": "title_structure_readability",
                "weight": 10,
                "available": True,
                "score": 10,
                "summary": "结构完整",
                "evidence": [
                    {
                        "type": "deterministic_title_structure",
                        "subscores": {
                            "length": 4,
                            "product_type_position": 4,
                            "repetition": 2,
                        },
                    }
                ],
            },
            {
                "key": "evidence_backed_detail_quality",
                "weight": 10,
                "available": True,
                "score": 5,
                "summary": "覆盖3/6事实词",
                "evidence": [],
            },
        ],
        "limitations": [],
    }

    projected = _normalize_title_score_payload(legacy)

    assert projected is not None
    assert projected["score"] == 78
    assert projected["band"] == "solid"
    assert projected["scoring_version"] == "evidence-title-v2"
    assert projected["compatibility_projection"] == {
        "source_version": "evidence-title-v1",
        "persisted_payload_changed": False,
    }
    assert "organic_search_visibility" not in {
        item["key"] for item in projected["components"]
    }
    assert "first_page_same_type_relevance" not in {
        item["key"] for item in projected["components"]
    }


def test_complete_root_expansion_never_creates_typed_prefixes() -> None:
    assert _complete_root_expansion_input("rgb light") == "rgb light"
    assert _complete_root_expansion_input("tv light") == "tv light"
    assert _complete_root_expansion_input("L shaped desk") == "l shaped desk"
    assert _complete_root_expansion_input("compressed sofa") == "compressed sofa"
    assert _complete_root_expansion_input("lazy s") == ""


def test_title_root_expansions_include_lazy_and_remove_variant_noise() -> None:
    roots = _title_root_expansions(
        "Corduroy Lazy Sofa Chair - Foldable Multi-Functional Seat for Home - Navy Blue",
        identity_terms=["floor chair", "sofa chair", "lazy sofa", "floor sofa"],
    )

    assert "lazy sofa" in roots
    assert "sofa chair" in roots
    assert "corduroy" in roots
    assert "navy" not in roots
    assert "blue" not in roots


def test_root_expansion_relevance_gate_keeps_s_and_a_but_rejects_blind_branches() -> None:
    profile = VisionProfile(
        product_name="Corduroy floor sofa chair",
        category="Living room seating",
        product_type_terms=["floor chair", "sofa chair"],
        same_product_aliases=["lazy sofa", "floor sofa"],
        distinctive_terms=["corduroy", "foldable"],
        keywords=[
            KeywordCandidate(phrase="corduroy floor chair", rationale="Visible form"),
            KeywordCandidate(phrase="foldable lazy sofa", rationale="Direct alias"),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="lazy sofa", rationale="Product phrase"),
            KeywordCandidate(phrase="floor chair", rationale="Product phrase"),
        ],
        opportunity_seeds=[
            AdjacentDemandCandidate(
                phrase="guest seating",
                rationale="Same compact seating job",
                buyer_job="provide compact occasional seating for a guest",
                alternative_product_terms=["bean bag", "floor cushion"],
                excluded_product_terms=["sofa cover"],
            )
        ],
        exclusions=["sofa cover", "chair cover"],
        confidence=0.95,
        title_suggestion="Corduroy Floor Chair Foldable Lazy Sofa",
        title_reason="Image-title fused identity",
    )
    title = "Corduroy Lazy Sofa Chair Foldable Multi Functional Seat for Home Blue"

    same = _root_expansion_relevance_decision("lazy sofa", profile, source_title=title)
    adjacent = _root_expansion_relevance_decision(
        "bean bag chair", profile, source_title=title
    )

    assert same["accepted"] is True
    assert same["relation"] == "same_product"
    assert adjacent["accepted"] is True
    assert adjacent["relation"] == "adjacent_demand"
    for phrase in (
        "corduroy jacket",
        "lazy susan",
        "foldable table",
        "modular psu",
        "floor lamp",
        "sofa cover",
    ):
        decision = _root_expansion_relevance_decision(phrase, profile, source_title=title)
        assert decision["accepted"] is False, phrase
        assert decision["relation"] == "irrelevant", phrase


def test_sofa_exclusion_does_not_zero_supported_sofa_identity() -> None:
    profile = VisionProfile(
        product_name="Corduroy foldable floor chair",
        category="Living room floor seating",
        product_type_terms=[
            "floor chair",
            "folding sofa bed",
            "tatami mat chair",
            "lounge chair",
            "convertible floor seat",
        ],
        same_product_aliases=["foldable floor seat", "corduroy lounge chair"],
        distinctive_terms=["corduroy fabric", "segmented folding design"],
        keywords=[
            KeywordCandidate(phrase="corduroy floor chair", rationale="Visible form"),
            KeywordCandidate(phrase="folding sofa bed", rationale="Supported product type"),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="corduroy lazy sofa", rationale="Title-supported root"),
            KeywordCandidate(phrase="sofa chair", rationale="Title-supported root"),
        ],
        opportunity_seeds=[
            KeywordCandidate(phrase="compact guest seating", rationale="Adjacent use")
        ],
        exclusions=["rigid frame sofa", "sofa cover", "office chair"],
        confidence=0.92,
        title_suggestion="Corduroy Foldable Floor Chair",
        title_reason="Image-title fused identity",
    )
    title = "Corduroy Lazy Sofa Chair Foldable Multi Functional Seat for Home Blue"

    assert _autocomplete_fit_score("sofas", profile, source_title=title) > 0
    assert _autocomplete_fit_score("sofa chairs", profile, source_title=title) > 0
    assert _autocomplete_fit_score("sofa bed", profile, source_title=title) > 0
    assert _autocomplete_fit_score("rigid frame sofa", profile, source_title=title) == 0
    assert _autocomplete_fit_score("sofa cover", profile, source_title=title) == 0


@pytest.mark.asyncio
async def test_public_search_pacer_spaces_autocomplete_and_both_page_requests() -> None:
    class UnderlyingClient:
        async def fetch_search_suggestions(self, keyword: str) -> list[str]:
            return [keyword]

        async def fetch_search_first_page(
            self,
            keyword: str,
        ) -> tuple[str, dict[str, Any]]:
            return _search_url(keyword), {}

        async def fetch_search_next_page(
            self,
            request_url: str,
            after: str,
        ) -> dict[str, Any]:
            del request_url, after
            return {}

    clock_values = iter([0.0, 0.2, 1.0, 1.1, 2.0])
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = _PacedSearchClient(
        UnderlyingClient(),  # type: ignore[arg-type]
        minimum_interval_seconds=1.0,
        clock=lambda: next(clock_values),
        sleep=fake_sleep,
    )

    await client.fetch_search_suggestions("rgb")
    await client.fetch_search_first_page("rgb light bar")
    await client.fetch_search_next_page(_search_url("rgb light bar"), "next")

    assert sleeps == pytest.approx([0.8, 0.9])
    assert client.request_count == 3


@pytest.mark.asyncio
async def test_public_request_throttle_adds_bounded_random_jitter() -> None:
    clock_values = iter([0.0, 0.2, 1.4])
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    throttle = _PublicRequestThrottle(
        minimum_interval_seconds=1.0,
        jitter_seconds=0.5,
        jitter=lambda _minimum, _maximum: 0.4,
        clock=lambda: next(clock_values),
        sleep=fake_sleep,
    )

    await throttle.wait()
    await throttle.wait()

    assert sleeps == pytest.approx([1.2])


@pytest.mark.asyncio
async def test_ranking_public_client_disables_search_endpoint_retries() -> None:
    retries_seen: list[int] = []
    client = CompetitorPublicClient(search_endpoint_retries=0)

    async def fake_get_json(
        _url: str,
        *,
        retries: int = 3,
    ) -> dict[str, Any]:
        retries_seen.append(retries)
        return {"sections": {"search_suggestions": {"results": []}}}

    client._get_json = fake_get_json  # type: ignore[method-assign]

    assert await client.fetch_search_suggestions("rgb") == []
    assert retries_seen == [0]


@pytest.mark.asyncio
async def test_concurrent_analysis_clients_share_one_public_request_throttle() -> None:
    class UnderlyingClient:
        async def fetch_search_suggestions(self, keyword: str) -> list[str]:
            return [keyword]

    clock_values = iter([0.0, 0.25, 1.0])
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    throttle = _PublicRequestThrottle(
        minimum_interval_seconds=1.0,
        clock=lambda: next(clock_values),
        sleep=fake_sleep,
    )
    first = _PacedSearchClient(
        UnderlyingClient(),  # type: ignore[arg-type]
        minimum_interval_seconds=1.0,
        throttle=throttle,
    )
    second = _PacedSearchClient(
        UnderlyingClient(),  # type: ignore[arg-type]
        minimum_interval_seconds=1.0,
        throttle=throttle,
    )

    await first.fetch_search_suggestions("rgb")
    await second.fetch_search_suggestions("ambient")

    assert sleeps == pytest.approx([0.75])
    assert first.request_count == 1
    assert second.request_count == 1


@pytest.mark.asyncio
async def test_shared_autocomplete_cache_refreshes_only_on_first_hit_after_24_hours(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'autocomplete-cache.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    engine.dispose()
    now = [datetime(2026, 8, 11, 1, 0, 0)]
    live_calls: list[str] = []

    class UnderlyingClient:
        async def fetch_search_suggestions(self, keyword: str) -> list[str]:
            live_calls.append(keyword)
            return (
                ["rgb light bar", "rgb lights"]
                if len(live_calls) == 1
                else ["rgb lights", "rgb light bar"]
            )

    cache = _SharedAutocompleteCache(
        database_url,
        clock=lambda: now[0],
    )
    client = _PacedSearchClient(
        UnderlyingClient(),  # type: ignore[arg-type]
        minimum_interval_seconds=0,
        autocomplete_cache=cache,
    )

    first = await client.fetch_search_suggestions("rgb")
    now[0] += timedelta(hours=23, minutes=59)
    cached = await client.fetch_search_suggestions("RGB")
    now[0] += timedelta(minutes=2)
    refreshed = await client.fetch_search_suggestions("rgb")

    assert first == ["rgb light bar", "rgb lights"]
    assert cached == first
    assert refreshed == ["rgb lights", "rgb light bar"]
    assert live_calls == ["rgb", "rgb"]
    assert client.request_count == 2
    assert client.autocomplete_evidence("rgb")["cache_status"] == ("stale_refreshed")
    engine = create_engine_for_database_url(database_url)
    with Session(engine) as session:
        row = session.query(SearchAutocompleteCache).one()
        snapshots = session.query(SearchAutocompleteSnapshot).all()
        assert row.hit_count == 3
        assert row.refresh_count == 2
        assert row.suggestions == ["rgb lights", "rgb light bar"]
        assert len(snapshots) == 2
    engine.dispose()
    library = SearchRankingService(tmp_path).root_expansion_library_payload()
    assert library["policy"]["refresh_mode"] == "refresh_on_first_hit_after_ttl"
    assert library["policy"]["scheduled_refresh"] is False
    assert library["policy"]["legacy_partial_input_states_hidden"] is True
    assert library["roots"][0]["root"] == "rgb"
    assert library["roots"][0]["expansions"][0] == {
        "phrase": "rgb lights",
        "rank": 1,
    }


@pytest.mark.asyncio
async def test_stale_autocomplete_is_not_used_when_required_refresh_fails(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'autocomplete-stale.db').as_posix()}"
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    engine.dispose()
    now = [datetime(2026, 8, 11, 1, 0, 0)]
    should_fail = False

    class UnderlyingClient:
        async def fetch_search_suggestions(self, keyword: str) -> list[str]:
            del keyword
            if should_fail:
                raise RuntimeError("temporary upstream failure")
            return ["compressed sofa", "vacuum packed sofa"]

    client = _PacedSearchClient(
        UnderlyingClient(),  # type: ignore[arg-type]
        minimum_interval_seconds=0,
        autocomplete_cache=_SharedAutocompleteCache(
            database_url,
            clock=lambda: now[0],
        ),
    )
    await client.fetch_search_suggestions("compressed s")
    now[0] += timedelta(hours=24, seconds=1)
    should_fail = True

    with pytest.raises(RuntimeError, match="temporary upstream failure"):
        await client.fetch_search_suggestions("compressed s")

    engine = create_engine_for_database_url(database_url)
    with Session(engine) as session:
        row = session.query(SearchAutocompleteCache).one()
        assert row.suggestions == ["compressed sofa", "vacuum packed sofa"]
        assert row.last_refresh_status == "failed"
        assert row.refresh_count == 1
    engine.dispose()


def test_existing_analysis_autocomplete_evidence_is_backfilled_once_globally(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'autocomplete-backfill.db').as_posix()}"
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    observations = (
        (
            "current",
            datetime(2026, 8, 10, 1, 0, 0),
            ["rgb light bar", "rgb lights"],
            [{"input_state": "ambient", "suggestions": ["ambient lights"]}],
        ),
        (
            "store-02",
            datetime(2026, 8, 10, 2, 0, 0),
            ["rgb lights", "rgb light bar"],
            [],
        ),
    )
    for store_code, observed_at, suggestions, extra_checks in observations:
        with store_scope(store_code), Session(engine) as session, session.begin():
            session.add(
                SearchRankingAnalysis(
                    offer_id=f"offer-{store_code}",
                    productline_id="12345678",
                    sku=None,
                    source_title="RGB Light Bar",
                    source_image_url="https://media.takealot.com/covers_images/test.jpg",
                    cache_key=f"cache-{store_code}",
                    provider="qwen",
                    model="test-model",
                    prompt_version=PROMPT_VERSION,
                    status="completed",
                    product_name="RGB light bar",
                    category="Lighting",
                    confidence=Decimal("0.95"),
                    vision_payload={
                        "autocomplete_checks": [
                            {
                                "input_state": "RGB L",
                                "suggestions": suggestions,
                            },
                            *extra_checks,
                        ]
                    },
                    vision_reused=False,
                    created_at=observed_at,
                    completed_at=observed_at,
                )
            )

    create_schema(engine)
    create_schema(engine)

    with Session(engine) as session:
        caches = (
            session.query(SearchAutocompleteCache).order_by(SearchAutocompleteCache.input_key).all()
        )
        snapshots = session.query(SearchAutocompleteSnapshot).all()
        rgb = next(row for row in caches if row.input_key == "rgb l")
        assert len(caches) == 2
        assert rgb.suggestions == ["rgb lights", "rgb light bar"]
        assert rgb.captured_at == datetime(2026, 8, 10, 2, 0, 0)
        assert rgb.hit_count == 2
        assert rgb.refresh_count == 2
        assert len(snapshots) == 3
    engine.dispose()


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


def test_fusion_profile_rejects_unstructured_adjacent_demand_candidate() -> None:
    arguments = {
        "product_name": "Corduroy floor chair",
        "category": "Living room seating",
        "product_type_terms": ["floor chair"],
        "same_product_aliases": ["lazy sofa", "sofa chair"],
        "distinctive_terms": ["corduroy"],
        "keywords": [
            {"phrase": f"floor chair {index}", "rationale": "Direct product intent"}
            for index in range(6)
        ],
        "autocomplete_seeds": [
            {"phrase": f"chair root {index}", "rationale": "Shopper root"}
            for index in range(6)
        ],
        "opportunity_seeds": [
            {"phrase": "guest seating", "rationale": "Adjacent demand hypothesis"}
        ],
        "exclusions": ["chair cover"],
        "confidence": 0.92,
        "title_suggestion": "Corduroy Floor Chair Lazy Sofa",
        "title_reason": "Image-title fused identity",
    }
    body = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "submit_takealot_fused_search_profile",
                                "arguments": json.dumps(arguments),
                            }
                        }
                    ]
                }
            }
        ]
    }

    with pytest.raises(ValueError) as exc_info:
        _validated_chat_profile(
            body,
            function_name="submit_takealot_fused_search_profile",
            profile_type=FusionVisionProfile,
        )

    assert "buyer_job" in str(exc_info.value)
    assert "alternative_product_terms" in str(exc_info.value)


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
        if keyword == "mouse":
            return ["mouse for laptop"]
        return []

    async def fetch_search_first_page(
        self,
        keyword: str,
    ) -> tuple[str, dict[str, Any]]:
        if keyword in {"wireless mouse", "wireless gaming mouse"}:
            return _search_url(keyword), _payload(
                [(str(90_000_000 + index), f"Wireless Mouse Model {index}") for index in range(36)],
                after="page-two",
                total=120,
            )
        if keyword == "mouse for laptop":
            products = [("12345678", "Rechargeable Wireless Mouse")]
            products.extend(
                (str(70_000_000 + index), f"Wireless Mouse Laptop {index}") for index in range(2)
            )
            products.extend(
                (str(75_000_000 + index), f"Laptop Sleeve Style {index}") for index in range(33)
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
            (str(91_000_000 + index), f"Wireless Mouse Page Two {index}") for index in range(4)
        ]
        products.append(("12345678", "Rechargeable Wireless Gaming Mouse Silent Dual Mode"))
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
            (str(70_000_000 + index), f"Wireless Mouse Laptop {index}") for index in range(2)
        ]
        products.extend(
            (str(75_000_000 + index), f"Laptop Sleeve Style {index}") for index in range(34)
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
            (str(91_000_000 + index), f"Wireless Mouse Page Two {index}") for index in range(36)
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
            after=(f"page-{page_number + 1}" if page_number < len(self.pages) else ""),
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

    keyword_rows = {
        item["keyword"]: item for item in first["analysis"]["keywords"]
    }
    accepted = keyword_rows["wireless mouse"]
    opportunity = keyword_rows["mouse for laptop"]
    second_accepted = keyword_rows["wireless gaming mouse"]
    rejected = keyword_rows["computer accessory"]
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
        "takealot_root_expansion"
    )
    assert accepted["validation_evidence"]["query_source_channel"] == (
        "takealot_root_expansion"
    )
    assert "model_south_african_direct" in accepted["validation_evidence"]["query_source_channels"]
    assert accepted["validation_evidence"]["autocomplete_rank"] == 1
    assert accepted["validation_evidence"]["evaluated_first_page_results"] == 36
    assert accepted["validation_evidence"]["matched_first_page_results"] == 36
    assert second_accepted["relevance_status"] == "accepted"
    assert opportunity["relevance_status"] == "opportunity"
    assert opportunity["validation_evidence"]["autocomplete_rank"] == 1
    assert opportunity["validation_evidence"]["matched_first_page_results"] == 3
    assert opportunity["validation_evidence"]["evaluated_first_page_results"] == 36
    assert (
        opportunity["validation_evidence"]["direct_competitor_count_excluding_target_first_page"]
        == 2
    )
    assert opportunity["validation_evidence"]["opportunity_qualified"] is True
    assert opportunity["found"] is True
    assert rejected["relevance_status"] == "rejected_irrelevant"
    assert rejected["pages_scanned"] == 1
    assert rejected["found"] is False
    assert first["analysis"]["usage"]["total_tokens"] == 200
    assert first["analysis"]["provider"] == "qwen"
    assert first["analysis"]["estimated_cost_cny"] == 0.00088
    assert first["status"]["operation_scope"] == (
        "manual_single_offer_or_confirmed_serial_batch"
    )
    assert first["status"]["root_expansion_input_limit"] == 20
    assert first["status"]["root_expansion_followup_root_limit"] == 4
    assert first["status"]["root_expansion_phrase_roots_enabled"] is True
    assert first["status"]["root_expansion_raw_suggestions_are_selected"] is False
    assert first["status"]["root_source_priority"] == [
        "human_confirmed_product_fact",
        "image_title_first_instinct",
        "title_word_root",
        "result_page_learning",
        "image_title_need_state",
        "title_cross_check",
    ]
    assert first["status"]["model_market_context"] == "South Africa"
    assert first["status"]["model_language_variant"] == "South African English"
    assert first["status"]["model_shopper_context"] == (
        "South African local customer habits"
    )
    assert first["status"]["model_localization_scope"] == (
        "all_model_generated_text_fields"
    )
    assert first["status"]["model_localization_is_measured_demand"] is False
    assert first["status"]["search_query_attempt_limit"] == 14
    assert first["status"]["query_source_targets"] == {
        "model_south_african_direct": 6,
        "takealot_root_expansion": 6,
        "adjacent_opportunity": 1,
        "adaptive_recovery": 1,
    }
    assert first["analysis"]["shopper_journey"]["mode"] == ("manual_single_offer_one_click")
    assert first["analysis"]["shopper_journey"]["public_request_count"] > 0
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
    assert all("journey_types" in item["evidence"] for item in title_strategies)
    assert title_strategies[0]["title"].startswith("Wireless Mouse")
    assert title_strategies[1]["title"].startswith("Wireless Gaming Mouse")
    assert title_strategies[1]["title"] != title_strategies[0]["title"]
    assert title_strategies[2]["title"].startswith("Mouse For Laptop")
    for strategy in title_strategies:
        assert all(character.isalnum() or character == " " for character in strategy["title"])
    assert first["analysis"]["title_suggestion"] == title_strategies[0]["title"]
    assert first["analysis"]["profile"]["title_suggestion"] == title_strategies[0]["title"]
    assert first["analysis"]["opportunity_title_suggestion"] == title_strategies[2]["title"]
    assert first["analysis"]["recognition"]["model_received_source_title"] is True
    assert (
        first["analysis"]["recognition"]["visual_stage_received_source_title"]
        is False
    )
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
async def test_family_analysis_runs_once_and_projects_variant_title_parameters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'variant-family.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setenv("TAKEALOT_SEARCH_PAGE_DELAY_SECONDS", "0")
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    captured_at = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                OfferCurrent(
                    offer_id="offer-family-black",
                    productline_id="12345678",
                    sku="MOUSE-BLACK",
                    title="Silent Rechargeable Wireless Gaming Mouse Black",
                    image_url="http://media.takealot.com/covers_images/test/black.file",
                    status="buyable",
                    takealot_available_stock=2,
                    captured_at=captured_at,
                ),
                OfferCurrent(
                    offer_id="offer-family-white",
                    productline_id="12345678",
                    sku="MOUSE-WHITE",
                    title="Silent Rechargeable Wireless Gaming Mouse White",
                    image_url="http://media.takealot.com/covers_images/test/white.file",
                    status="buyable",
                    takealot_available_stock=3,
                    captured_at=captured_at,
                ),
            ]
        )
    engine.dispose()

    class FamilyVisionClient(FakeVisionClient):
        calls = 0
        contexts: list[Mapping[str, Any]] = []

        async def identify(
            self,
            *,
            image_url: str,
            reference_title: str,
            variant_context: Mapping[str, Any] | None = None,
        ) -> VisionCallResult:
            type(self).contexts.append(dict(variant_context or {}))
            return await super().identify(
                image_url=image_url,
                reference_title=reference_title,
                variant_context=variant_context,
            )

    search_clients: list[FakeSearchClient] = []

    def search_factory() -> FakeSearchClient:
        client = FakeSearchClient()
        search_clients.append(client)
        return client

    service = SearchRankingService(
        tmp_path,
        vision_client_factory=FamilyVisionClient,
        search_client_factory=search_factory,  # type: ignore[arg-type]
    )
    white_detail = await service.analyze_offer("offer-family-white")

    assert FamilyVisionClient.calls == 1
    assert len(search_clients) == 1
    assert FamilyVisionClient.contexts[0]["shared_title"] == (
        "Silent Rechargeable Wireless Gaming Mouse"
    )
    assert FamilyVisionClient.contexts[0]["representative_offer_id"] == (
        "offer-family-black"
    )
    assert white_detail["product"]["offer_id"] == "offer-family-white"
    assert white_detail["analysis"]["source_offer_id"] == "offer-family-black"
    assert white_detail["analysis"]["variant_projection"]["applied"] is True
    assert white_detail["analysis"]["variant_projection"]["title_review_available"] is True
    assert white_detail["analysis"]["variant_projection"][
        "variant_parameters_visually_verified"
    ] is False
    assert [
        item["value"]
        for item in white_detail["analysis"]["variant_projection"]["variant_parameters"]
    ] == ["White"]
    assert white_detail["analysis"]["title_score"]["current_title_match"] is True
    assert white_detail["analysis"]["title_score"]["current_title"].endswith("White")

    black_detail = service.detail_payload("offer-family-black")
    assert black_detail is not None
    assert black_detail["analysis"]["id"] == white_detail["analysis"]["id"]
    assert black_detail["analysis"]["variant_projection"]["applied"] is False
    assert FamilyVisionClient.calls == 1
    listing = service.list_payload()
    assert {item["latest_analysis"]["source_offer_id"] for item in listing["items"]} == {
        "offer-family-black"
    }


@pytest.mark.asyncio
async def test_fusion_manual_fact_gap_persists_result_and_makes_zero_public_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'manual-gap.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setenv("TAKEALOT_SEARCH_PAGE_DELAY_SECONDS", "0")
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    with Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="offer-manual-gap",
                productline_id="12345679",
                sku="GAP-01",
                title="Portable Power Product",
                image_url="http://media.takealot.com/covers_images/test/gap.file",
                status="buyable",
                takealot_available_stock=3,
                captured_at=datetime.now(UTC),
            )
        )
    engine.dispose()

    class ManualGapVisionClient:
        def __init__(self, _: SearchRankingRuntimeSettings) -> None:
            pass

        async def identify(
            self,
            *,
            image_url: str,
            reference_title: str,
            variant_context: Mapping[str, Any] | None = None,
        ) -> VisionCallResult:
            del image_url, reference_title, variant_context
            visual = _opportunity_profile()
            fusion = visual.model_copy(
                update={
                    "requires_human_fact_confirmation": True,
                    "manual_fact_reason": "Cannot distinguish battery type safely",
                    "missing_facts": ["battery type"],
                }
            )
            return VisionCallResult(
                profile=fusion,
                visual_profile=visual,
                fusion_profile=fusion,
                provider="qwen",
                model="qwen3.7-plus",
                response_id="fusion-gap",
                usage={"input_tokens": 600, "output_tokens": 200, "total_tokens": 800},
                estimated_cost_cny=0.002,
            )

    def forbidden_search_factory() -> FakeSearchClient:
        raise AssertionError("manual fact gaps must not open a Takealot search client")

    service = SearchRankingService(
        tmp_path,
        vision_client_factory=ManualGapVisionClient,
        search_client_factory=forbidden_search_factory,  # type: ignore[arg-type]
    )
    detail = await service.analyze_offer("offer-manual-gap")

    analysis = detail["analysis"]
    assert analysis["status"] == "completed"
    assert analysis["recognition"]["manual_fact_required"] is True
    assert analysis["recognition"]["missing_facts"] == ["battery type"]
    assert analysis["recognition"]["batch_action"] == "skip_without_retry"
    assert analysis["shopper_journey"]["skipped_for_manual_fact"] is True
    assert analysis["shopper_journey"]["public_request_count"] == 0
    assert analysis["keywords"] == []
    assert analysis["title_score"]["components"]
    assert analysis["product_fact_recommendation"]["recommended"] is True


@pytest.mark.asyncio
async def test_optional_manual_product_fact_profile_drives_compressed_sofa_validation_and_title(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'manual-facts.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setenv("TAKEALOT_SEARCH_PAGE_DELAY_SECONDS", "0")
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    title = "Corduroy Lazy Sofa Chair Foldable Multi Functional Seat Blue"
    raw_image_url = "http://media.takealot.com/covers_images/test/sofa.file"
    with Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="offer-sofa-fact",
                productline_id="102110267",
                sku="SOFA-FACT-01",
                title=title,
                image_url=raw_image_url,
                status="buyable",
                takealot_available_stock=4,
                captured_at=datetime.now(UTC),
            )
        )
    engine.dispose()

    class SofaSearchClient:
        async def __aenter__(self) -> SofaSearchClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def fetch_search_suggestions(self, keyword: str) -> list[str]:
            return ["compressed sofa", "vacuum packed sofa"] if "compressed" in keyword else []

        async def fetch_search_first_page(
            self,
            keyword: str,
        ) -> tuple[str, dict[str, Any]]:
            if keyword == "compressed sofa":
                products = [
                    (str(88_000_000 + index), f"Compressed Sofa Model {index}")
                    for index in range(36)
                ]
                products[3] = (
                    "102110267",
                    "Corduroy Compressed Sofa Chair Blue",
                )
                return _search_url(keyword), _payload(products, after="", total=91)
            return _search_url(keyword), _payload(
                [(str(89_000_000 + index), f"Rocking Chair Model {index}") for index in range(36)],
                after="",
                total=400,
            )

        async def fetch_search_next_page(
            self,
            request_url: str,
            after: str,
        ) -> dict[str, Any]:
            del request_url, after
            return _payload([], after="", total=0)

    profile = VisionProfile(
        product_name="Navy corduroy floor sofa chair",
        category="Living room chairs and seating",
        product_type_terms=["floor sofa", "sofa chair"],
        distinctive_terms=["corduroy", "foldable", "blue"],
        keywords=[
            KeywordCandidate(
                phrase="floor sofa",
                rationale="Visible low floor seating shape",
            ),
            KeywordCandidate(
                phrase="sofa chair",
                rationale="Visible chair-sized sofa form",
            ),
        ],
        autocomplete_seeds=[
            KeywordCandidate(
                phrase="floor sofa",
                rationale="Likely shopper wording from the image",
            ),
            KeywordCandidate(
                phrase="sofa chair",
                rationale="Alternative shopper wording",
            ),
        ],
        opportunity_seeds=[
            KeywordCandidate(
                phrase="lazy sofa",
                rationale="Adjacent relaxation demand",
            )
        ],
        exclusions=["rocking chair"],
        confidence=0.95,
        title_suggestion="Floor Sofa Chair Corduroy Blue",
        title_reason="Image-only suggestion",
    )
    service = SearchRankingService(
        tmp_path,
        vision_client_factory=FakeVisionClient,
        search_client_factory=SofaSearchClient,  # type: ignore[arg-type]
    )
    trusted_image_url = service.list_payload()["items"][0]["image_url"]
    engine = create_engine_for_database_url(database_url)
    with Session(engine) as session, session.begin():
        source = SearchRankingAnalysis(
            offer_id="offer-sofa-fact",
            productline_id="102110267",
            sku="SOFA-FACT-01",
            source_title=title,
            source_image_url=trusted_image_url,
            cache_key=_analysis_cache_key(
                image_url=trusted_image_url,
                provider_signature=service.runtime.provider_signature,
                source_title=_variant_family_cache_material(
                    _variant_family_profile(
                        [
                            {
                                "offer_id": "offer-sofa-fact",
                                "productline_id": "102110267",
                                "sku": "SOFA-FACT-01",
                                "title": title,
                                "image_url": trusted_image_url,
                                "available_stock": 4,
                            }
                        ]
                    )
                ),
            ),
            provider="qwen",
            model="qwen3.7-plus",
            prompt_version=PROMPT_VERSION,
            status="completed",
            product_name=profile.product_name,
            category=profile.category,
            confidence=Decimal("0.95"),
            vision_payload={
                "vision_stage_completed": True,
                "model_profile": profile.model_dump(mode="json"),
                "profile": profile.model_dump(mode="json"),
                "recognition": {"title_reference_terms": []},
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                },
            },
            vision_reused=False,
            created_at=datetime.now(UTC).replace(tzinfo=None),
            completed_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(source)
        session.flush()
        source_analysis_id = source.id
        session.add(
            SearchRankingKeywordResult(
                analysis_id=source_analysis_id,
                keyword="chairs",
                candidate_order=1,
                relevance_status="accepted",
                relevance_score=Decimal("0.9000"),
                validation_evidence={"semantic_relation_grade": "S"},
                total_num_found=5000,
                pages_scanned=5,
                found=False,
                page_number=None,
                page_rank=None,
                organic_rank=None,
                row_number=None,
                column_number=None,
                columns_per_row=4,
                target_url=None,
                observed_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        session.add(
            SearchRankingProductFact(
                productline_id="102110267",
                source_offer_id="offer-sofa-fact",
                fact_type="product_type",
                fact_term="foldable floor chair",
                normalized_term="foldable floor chair",
                statement="Operator already confirmed the foldable floor chair identity",
                status="active",
                source_type="manual_confirmation",
                source_analysis_id=source_analysis_id,
                source_title=title,
                source_image_url=trusted_image_url,
                evidence={"operator_assertion": True},
                confirmed_by_username="operator",
                confirmed_by_display_name="Sofa Operator",
                confirmed_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        session.add(
            SearchRankingProductFact(
                productline_id="102110267",
                source_offer_id="offer-sofa-fact",
                fact_type="material",
                fact_term="leather sofa",
                normalized_term="leather sofa",
                statement="Legacy external evidence retained only for historical audit",
                status="active",
                source_type="reverse_corroborated",
                source_analysis_id=source_analysis_id,
                source_title=title,
                source_image_url=trusted_image_url,
                evidence={"legacy": True},
                confirmed_by_username="legacy-operator",
                confirmed_by_display_name="Legacy Operator",
                confirmed_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
    engine.dispose()

    before_confirmation = service.detail_payload("offer-sofa-fact")
    assert before_confirmation is not None
    before_recommendation = before_confirmation["analysis"]["product_fact_recommendation"]
    assert before_recommendation["recommended"] is False
    assert before_recommendation["reason_code"] == "no_title_cross_check_phrase"

    detail = await service.confirm_product_facts(
        "offer-sofa-fact",
        ProductFactConfirmation(
            source_analysis_id=source_analysis_id,
            reason_code="no_title_cross_check_phrase",
            actor_username="operator",
            actor_display_name="Sofa Operator",
            facts=(
                ProductFactInput(
                    fact_type="product_type",
                    fact_term="compressed sofa",
                    statement="Supplier confirms vacuum compressed sofa construction",
                ),
            ),
        ),
    )

    fact_profile = detail["product_fact_profile"]
    assert fact_profile["applied_terms"] == [
        "compressed sofa",
        "foldable floor chair",
    ]
    assert len(fact_profile["facts"]) == 2
    compressed_fact = next(
        item for item in fact_profile["facts"] if item["fact_term"] == "compressed sofa"
    )
    assert compressed_fact["source_type"] == "manual_confirmation"
    assert compressed_fact["evidence"]["confirmation_basis"] == (
        "operator_initiated_optional_confirmation"
    )
    assert all(item["keyword"] != "leather sofa" for item in detail["analysis"]["keywords"])
    compressed = next(
        item for item in detail["analysis"]["keywords"] if item["keyword"] == "compressed sofa"
    )
    assert compressed["relevance_status"] == "accepted"
    assert compressed["validation_evidence"]["validation_terms"] == [
        "floor sofa",
        "sofa chair",
        "compressed sofa",
        "foldable floor chair",
    ]
    assert (
        compressed["validation_evidence"]["same_type_validation_term_source"]
        == "semantic_verified_same_product_terms"
    )
    assert compressed["validation_evidence"]["semantic_relation_grade"] == "S"
    assert compressed["organic_rank"] == 4
    assert compressed["validation_evidence"]["autocomplete_cache_status"] == ("miss_refreshed")
    assert compressed["validation_evidence"]["autocomplete_observed_at"]
    assert compressed["validation_evidence"]["autocomplete_shared_across_stores"] is True
    assert detail["analysis"]["title_strategies"][0]["title"].startswith("Compressed Sofa")
    assert detail["analysis"]["usage"]["total_tokens"] == 0

    active_fact = fact_profile["facts"][0]
    revoked = service.revoke_product_fact(
        "offer-sofa-fact",
        active_fact["id"],
        ProductFactRevocation(
            actor_username="operator",
            actor_display_name="Sofa Operator",
            reason="Supplier corrected the construction",
        ),
    )
    assert revoked["product_fact_profile"]["applied_count"] == 1
    assert revoked["product_fact_profile"]["facts"][0]["status"] == "revoked"
    assert revoked["product_fact_profile"]["applied_terms"] == ["foldable floor chair"]


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
        item for item in second["analysis"]["keywords"] if item["keyword"] == "mouse for laptop"
    )

    assert second["analysis"]["vision_reused"] is False
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
    assert all(item["delta"] < 0 for item in validation["secondary_comparisons"])
    assert primary["validation_evidence"]["comparison_role"] == "primary"
    assert primary["validation_evidence"]["comparison_strategy"] == ("adjacent_opportunity")
    assert FakeVisionClient.calls == 2


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
async def test_title_change_invalidates_fusion_cache_and_large_difference_only_warns(
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

    assert second["analysis"]["vision_reused"] is False
    assert second["analysis"]["usage"]["total_tokens"] == 200
    assert second["analysis"]["recognition"]["identity_large_difference"] is True
    assert second["analysis"]["recognition"]["manual_fact_required"] is False
    assert any(item["pages_scanned"] > 0 for item in second["analysis"]["keywords"])
    assert FakeVisionClient.calls == 2
    assert len(search_clients) == 2


@pytest.mark.asyncio
async def test_current_image_product_fact_is_preserved_on_large_difference_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'identity-fact-override.db').as_posix()}"
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
                offer_id="offer-identity-fact",
                productline_id="12345678",
                sku="IDENTITY-FACT-01",
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
    first = await service.analyze_offer("offer-identity-fact")
    current_image_url = service.list_payload()["items"][0]["image_url"]
    engine = create_engine_for_database_url(database_url)
    with Session(engine) as session, session.begin():
        offer = session.get(OfferCurrent, "offer-identity-fact")
        assert offer is not None
        offer.title = "Leather Dining Chair"
        offer.captured_at = datetime.now(UTC)
        session.add(
            SearchRankingProductFact(
                productline_id="12345678",
                source_offer_id="offer-identity-fact",
                fact_type="product_type",
                fact_term="wireless mouse",
                normalized_term="wireless mouse",
                statement="Operator confirmed the current image is a wireless mouse",
                status="active",
                source_type="manual_confirmation",
                source_analysis_id=first["analysis"]["id"],
                source_title="Silent Rechargeable Wireless Gaming Mouse",
                source_image_url=current_image_url,
                evidence={"reason": "supplier confirmation"},
                confirmed_by_username="operator",
                confirmed_by_display_name="Operator",
                confirmed_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
    engine.dispose()

    second = await service.analyze_offer("offer-identity-fact")
    recognition = second["analysis"]["recognition"]

    assert second["analysis"]["vision_reused"] is False
    assert second["analysis"]["usage"]["total_tokens"] == 200
    assert second["analysis"]["confidence"] == pytest.approx(0.91)
    assert recognition["title_identity_conflict"] is True
    assert recognition["confirmed_identity_fact_support"] is True
    assert recognition["confirmed_fact_resolved_title_conflict"] is True
    assert recognition["identity_deviation_branch"] == ("confirmed_fact_support_continue")
    assert any(item["pages_scanned"] > 0 for item in second["analysis"]["keywords"])
    assert FakeVisionClient.calls == 2
    assert len(search_clients) == 2


def test_compressed_sofa_fact_bridges_subject_without_replacing_visible_shape() -> None:
    profile = VisionProfile(
        product_name="Navy Blue Corduroy Floor Sofa Chair",
        category="Living room seating",
        product_type_terms=["floor sofa"],
        distinctive_terms=["corduroy", "blue"],
        keywords=[
            KeywordCandidate(
                phrase="floor sofa chair",
                rationale="Visible physical form",
            ),
            KeywordCandidate(
                phrase="corduroy floor sofa",
                rationale="Visible material and physical form",
            ),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="floor sofa", rationale="Visible shopper root"),
            KeywordCandidate(phrase="sofa chair", rationale="Everyday shopper root"),
        ],
        opportunity_seeds=[KeywordCandidate(phrase="lazy sofa", rationale="Adjacent need state")],
        exclusions=["rocking chair"],
        confidence=0.95,
        title_suggestion="Floor Sofa Chair Corduroy Blue",
        title_reason="Image-only suggestion",
    )

    identity_check = _confirmed_identity_fact_cross_check(
        profile,
        ["compressed sofa"],
    )
    enriched = _enrich_profile_with_confirmed_facts(
        profile,
        [
            {
                "fact_type": "product_type",
                "fact_term": "compressed sofa",
                "source_type": "manual_confirmation",
            }
        ],
    )

    assert identity_check["confirmed_identity_fact_support"] is True
    assert identity_check["confirmed_identity_fact_similarity"] == pytest.approx(0.5)
    assert identity_check["confirmed_identity_fact_similarity_decides_support"] is False
    assert identity_check["confirmed_identity_fact_matches"] == [
        {
            "term": "compressed sofa",
            "similarity": 0.5,
            "matched_tokens": ["sofa"],
            "matched_identity_anchors": ["sofa"],
            "rejected_modifier_overlap": [],
            "identity_supported": True,
            "identity_match_rule": "product_subject_or_alias_match",
        }
    ]
    assert enriched.product_type_terms[:2] == ["floor sofa", "compressed sofa"]
    assert enriched.keywords[0].phrase == "compressed sofa"


def test_identity_fact_rejects_modifier_only_overlap_and_accepts_subject_alias() -> None:
    profile = VisionProfile(
        product_name="Navy Blue Corduroy Floor Sofa Chair",
        category="Living room seating",
        product_type_terms=["floor sofa", "sofa chair"],
        distinctive_terms=["corduroy", "blue"],
        keywords=[
            KeywordCandidate(phrase="floor sofa", rationale="Visible form"),
            KeywordCandidate(phrase="sofa chair", rationale="Visible form alias"),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="floor sofa", rationale="Visible root"),
            KeywordCandidate(phrase="sofa chair", rationale="Second visible root"),
        ],
        opportunity_seeds=[KeywordCandidate(phrase="lazy sofa", rationale="Adjacent need")],
        exclusions=[],
        confidence=0.95,
        title_suggestion="Floor Sofa Chair",
        title_reason="Image-only suggestion",
    )

    check = _confirmed_identity_fact_cross_check(
        profile,
        ["floor lamp", "vacuum packed couch"],
    )

    assert check["confirmed_identity_fact_supported_terms"] == ["vacuum packed couch"]
    assert check["confirmed_identity_fact_matches"][0] == {
        "term": "floor lamp",
        "similarity": 0.5,
        "matched_tokens": ["floor"],
        "matched_identity_anchors": [],
        "rejected_modifier_overlap": ["floor"],
        "identity_supported": False,
        "identity_match_rule": "modifier_only_overlap_rejected",
    }
    assert check["confirmed_identity_fact_matches"][1]["matched_identity_anchors"] == ["sofa"]

    generic_check = _confirmed_identity_fact_cross_check(
        profile.model_copy(update={"product_type_terms": ["sound bar"]}),
        ["light bar"],
    )
    assert generic_check["confirmed_identity_fact_support"] is False
    assert (
        generic_check["confirmed_identity_fact_matches"][0]["identity_match_rule"]
        == "generic_head_without_matching_tail_rejected"
    )


def test_construction_fact_is_searchable_but_not_a_physical_shape_term() -> None:
    profile = VisionProfile(
        product_name="Navy Blue Corduroy Floor Sofa Chair",
        category="Living room seating",
        product_type_terms=["floor sofa"],
        distinctive_terms=["corduroy", "blue"],
        keywords=[
            KeywordCandidate(phrase="floor sofa", rationale="Visible form"),
            KeywordCandidate(phrase="sofa chair", rationale="Visible form alias"),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="floor sofa", rationale="Visible root"),
            KeywordCandidate(phrase="sofa chair", rationale="Second visible root"),
        ],
        opportunity_seeds=[KeywordCandidate(phrase="lazy sofa", rationale="Adjacent need")],
        exclusions=[],
        confidence=0.95,
        title_suggestion="Floor Sofa Chair",
        title_reason="Image-only suggestion",
    )

    enriched = _enrich_profile_with_confirmed_facts(
        profile,
        [
            {
                "fact_type": "construction",
                "fact_term": "vacuum compressed",
                "source_type": "manual_confirmation",
            }
        ],
    )

    assert enriched.product_type_terms == ["floor sofa"]
    assert enriched.keywords[0].phrase == "vacuum compressed"
    assert enriched.autocomplete_seeds[0].phrase == "vacuum compressed"


@pytest.mark.asyncio
async def test_large_difference_does_not_block_and_corrected_title_gets_new_fusion(
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
            variant_context: Mapping[str, Any] | None = None,
        ) -> VisionCallResult:
            del image_url, reference_title, variant_context
            type(self).calls += 1
            raw = _opportunity_profile()
            return VisionCallResult(
                profile=raw,
                visual_profile=raw,
                fusion_profile=raw,
                provider="qwen",
                model="qwen3.7-plus",
                response_id="both-conflict",
                usage={
                    "input_tokens": 300,
                    "output_tokens": 100,
                    "total_tokens": 400,
                },
                estimated_cost_cny=0.0014,
                provider_attempts=({"provider": "qwen", "status": "accepted"},),
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
    assert first["analysis"]["confidence"] == pytest.approx(0.9)
    assert first["analysis"]["recognition"]["identity_large_difference"] is True
    assert first["analysis"]["recognition"]["manual_fact_required"] is False
    assert len(search_clients) == 1

    engine = create_engine_for_database_url(database_url)
    with Session(engine) as session, session.begin():
        offer = session.get(OfferCurrent, "offer-raw-profile")
        assert offer is not None
        offer.title = "Silent Rechargeable Wireless Gaming Mouse"
        offer.captured_at = datetime.now(UTC)
    engine.dispose()
    second = await service.analyze_offer("offer-raw-profile")

    assert second["analysis"]["vision_reused"] is False
    assert second["analysis"]["usage"]["total_tokens"] == 400
    assert second["analysis"]["confidence"] == pytest.approx(0.9)
    assert second["analysis"]["recognition"]["identity_large_difference"] is False
    assert any(item["relevance_status"] == "accepted" for item in second["analysis"]["keywords"])
    assert ConflictVisionClient.calls == 2
    assert len(search_clients) == 2


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
        "Portable Projection Screen High Brightness Outdoor Movie Foldable 100 Inch"
    )
    assert suggestion.startswith("Portable Projection Screen")
    assert suggestion.endswith("100 Inch")
    assert all(character.isalnum() or character == " " for character in suggestion)


@pytest.mark.parametrize(
    "listing_title",
    [
        "Portable Projection Screen With Stand",
        "Foldable Projector Screen 100 Inch",
        "White Projection Cloth For Home Cinema",
        "Retractable Projector Curtain",
    ],
)
def test_projection_screen_same_type_uses_controlled_marketplace_aliases(
    listing_title: str,
) -> None:
    assert _title_matches_terms(listing_title, ["projection screen"]) is True


@pytest.mark.parametrize(
    "listing_title",
    [
        "4K Smart Projector Device",
        "Laptop Privacy Screen",
        "Cotton Table Cloth",
    ],
)
def test_projection_screen_aliases_do_not_match_generic_devices_or_materials(
    listing_title: str,
) -> None:
    assert _title_matches_terms(listing_title, ["projection screen"]) is False


@pytest.mark.asyncio
async def test_projection_screen_cloth_with_many_alias_competitors_is_core_not_blue_ocean() -> None:
    products = [
        ("12345678", "100 Inch Portable Projection Screen"),
        *[
            (str(64_000_000 + index), f"Projection Screen Model {index}")
            for index in range(18)
        ],
        ("65000001", "Projection Cloth For Home Cinema"),
        ("65000002", "Foldable Projector Screen"),
        ("65000003", "Retractable Projector Curtain"),
        *[
            (str(66_000_000 + index), f"4K Projector Device {index}")
            for index in range(14)
        ],
    ]
    candidate = SearchKeywordCandidate(
        phrase="projection screen cloth",
        rationale="Takealot completion",
        candidate_source="takealot_autocomplete",
        intended_strategy="opportunity",
        seed="projection screen",
        seed_source="image_need_state",
        autocomplete_rank=1,
        candidate_provenance=(
            {
                "candidate_source": "takealot_autocomplete",
                "intended_strategy": "opportunity",
                "seed": "projection screen",
                "seed_source": "image_need_state",
                "autocomplete_rank": 1,
            },
        ),
    )
    profile = VisionProfile(
        product_name="Portable projection screen",
        category="Home Theatre & Projectors",
        product_type_terms=["projection screen"],
        distinctive_terms=["portable"],
        keywords=[
            KeywordCandidate(phrase="portable projection screen", rationale="Exact type"),
            KeywordCandidate(phrase="projector screen for home", rationale="Use wording"),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="projection screen", rationale="Product instinct"),
            KeywordCandidate(phrase="movie screen", rationale="Use instinct"),
        ],
        opportunity_seeds=[
            KeywordCandidate(phrase="projection screen", rationale="Need state")
        ],
        exclusions=["projector device"],
        confidence=0.95,
        title_suggestion="Portable Projection Screen",
        title_reason="Image-only suggestion",
    )

    observation = await _collect_keyword_observation(
        OpportunityGateSearchClient([products]),  # type: ignore[arg-type]
        candidate=candidate,
        candidate_order=1,
        target_plid="12345678",
        profile=profile,
        max_pages=3,
        relevance_threshold=0.6,
        page_delay_seconds=0,
        source_title="100-Inch Portable Projection Screen",
    )

    assert observation.relevance_status == "accepted"
    assert observation.relevance_score == pytest.approx(22 / 36)
    assert observation.validation_evidence[
        "direct_competitor_count_excluding_target_first_page"
    ] == 21
    assert observation.validation_evidence["opportunity_qualified"] is False


def test_title_suggestion_moves_floodlight_specs_behind_features() -> None:
    suggestion = _build_title_suggestion(
        "300W Outdoor RGB LED Floodlight Smart App Control IP66 Waterproof",
        ["flood lights"],
    )

    assert suggestion == (
        "Flood Lights Outdoor RGB LED Floodlight Smart App Control Waterproof 300W IP66"
    )


def test_title_parameter_candidates_extract_specs_without_deciding_for_operator() -> None:
    floodlight = _title_parameter_candidates(
        "300W Outdoor RGB LED Floodlight Smart App Control IP66 Waterproof"
    )
    projection_screen = _title_parameter_candidates(
        "100-Inch Portable High-Brightness Retractable Projection Screen"
    )
    phone = _title_parameter_candidates(
        "256GB Samsung Galaxy S24 5G Smartphone 4K Video"
    )

    assert [
        (item["parameter_value"], item["parameter_type"], item["system_recommendation"])
        for item in floodlight
    ] == [
        ("300W", "power", "ordinary_specification"),
        ("IP66", "protection_rating", "ordinary_specification"),
    ]
    assert [item["parameter_value"] for item in projection_screen] == ["100 Inch"]
    assert projection_screen[0]["system_recommendation"] == "decision_parameter"
    assert [item["parameter_value"] for item in phone] == ["256GB", "4K"]
    assert all("manual_decision" not in item for item in [*floodlight, *projection_screen])


def test_manual_decision_parameter_confirmation_is_audited_and_title_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'decision-parameters.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                OfferCurrent(
                    offer_id="offer-decision-parameter",
                    productline_id="10101010",
                    sku="LIGHT-300W",
                    title="300W Outdoor RGB LED Floodlight IP66 Waterproof",
                    image_url="http://media.takealot.com/covers_images/test/light.file",
                    status="buyable",
                    takealot_available_stock=4,
                    captured_at=now,
                ),
                OfferCurrent(
                    offer_id="offer-no-parameter",
                    productline_id="20202020",
                    sku="SOFA-NO-SPEC",
                    title="Corduroy Lazy Sofa Chair Blue",
                    image_url="http://media.takealot.com/covers_images/test/sofa.file",
                    status="buyable",
                    takealot_available_stock=2,
                    captured_at=now,
                ),
            ]
        )
    engine.dispose()
    service = SearchRankingService(tmp_path)

    before = service.detail_payload("offer-decision-parameter")
    assert before is not None
    assert before["analysis"] is None
    assert before["decision_parameter_profile"]["current_title_confirmed"] is False
    assert [
        item["parameter_value"]
        for item in before["decision_parameter_profile"]["candidates"]
    ] == ["300W", "IP66"]

    confirmed = service.confirm_decision_parameters(
        "offer-decision-parameter",
        DecisionParameterConfirmation(
            actor_username="operator.one",
            actor_display_name="Operator One",
            choices=(
                DecisionParameterChoice("300w", True),
                DecisionParameterChoice("ip66", False),
            ),
        ),
    )
    profile = confirmed["decision_parameter_profile"]
    assert profile["current_title_confirmed"] is True
    assert profile["applied_decision_values"] == ["300W"]
    assert profile["ordinary_parameter_count"] == 1
    assert profile["latest_confirmation"]["confirmed_by_username"] == "operator.one"

    no_parameter = service.confirm_decision_parameters(
        "offer-no-parameter",
        DecisionParameterConfirmation(
            actor_username="operator.one",
            actor_display_name="Operator One",
            choices=(),
        ),
    )
    assert no_parameter["decision_parameter_profile"]["candidate_count"] == 0
    assert no_parameter["decision_parameter_profile"]["current_title_confirmed"] is True

    engine = create_engine_for_database_url(database_url)
    with Session(engine) as session, session.begin():
        confirmations = list(
            session.query(SearchRankingDecisionParameterConfirmation).order_by(
                SearchRankingDecisionParameterConfirmation.id
            )
        )
        assert len(confirmations) == 2
        offer = session.get(OfferCurrent, "offer-decision-parameter")
        assert offer is not None
        offer.title = "300W Outdoor RGB LED Floodlight Remote IP66 Waterproof"
        offer.captured_at = datetime.now(UTC)
    engine.dispose()

    stale = service.detail_payload("offer-decision-parameter")
    assert stale is not None
    stale_profile = stale["decision_parameter_profile"]
    assert stale_profile["current_title_confirmed"] is False
    assert stale_profile["requires_confirmation"] is True
    assert stale_profile["applied_decision_values"] == []
    assert stale_profile["latest_confirmation"]["current_title_matches"] is False


def test_parameter_never_leads_without_human_confirmation_even_when_query_is_accepted() -> None:
    strategies = _build_title_strategies(
        source_title="300W Outdoor RGB LED Floodlight Smart App Control IP66 Waterproof",
        accepted_keywords=["300w rgb led floodlight"],
        hot_term_keywords=[],
        opportunity_keywords=[],
    )

    assert strategies[0]["available"] is True
    assert str(strategies[0]["title"]).startswith("RGB LED Floodlight")
    assert str(strategies[0]["title"]).endswith("300W IP66")


def test_human_confirmed_parameter_leads_only_after_same_family_query_is_accepted() -> None:
    strategies = _build_title_strategies(
        source_title="300W Outdoor RGB LED Floodlight Smart App Control IP66 Waterproof",
        accepted_keywords=["300w rgb led floodlight"],
        hot_term_keywords=[],
        opportunity_keywords=[],
        decision_parameter_values=["300W"],
    )

    assert strategies[0]["available"] is True
    assert str(strategies[0]["title"]).startswith("300W RGB LED Floodlight")
    assert str(strategies[0]["title"]).endswith("IP66")


def test_projection_screen_validated_size_can_lead_while_other_specs_stay_trailing() -> None:
    strategies = _build_title_strategies(
        source_title="100-Inch Portable High-Brightness Retractable Projection Screen",
        accepted_keywords=["portable projection screen", "100 inch projection screen"],
        hot_term_keywords=[],
        opportunity_keywords=[],
        decision_parameter_values=["100 Inch"],
    )

    contiguous = strategies[0]
    assert contiguous["available"] is True
    assert str(contiguous["title"]).startswith("100 Inch Portable Projection Screen")
    assert all(character.isalnum() or character == " " for character in contiguous["title"])


def test_conflicting_projection_screen_size_is_not_written_into_title() -> None:
    strategies = _build_title_strategies(
        source_title="100-Inch Portable High-Brightness Retractable Projection Screen",
        accepted_keywords=["projection screen 120 inch"],
        hot_term_keywords=["projection screen 120 inch"],
        opportunity_keywords=[],
    )

    assert strategies[0]["available"] is False
    assert strategies[0]["title"] is None
    assert "参数冲突" in strategies[0]["explanation"]
    assert strategies[1]["available"] is False


@pytest.mark.parametrize(
    "source_title",
    [
        "2 Pack Cordless Vacuum Cleaner 220V 1500W 2L 35CM 4KG IPX4",
        "Cordless Vacuum Cleaner 2 Pack 220 V 1500 W 2 L 35 CM 4 KG IPX4",
    ],
)
def test_title_suggestion_moves_common_parameter_families_to_tail(
    source_title: str,
) -> None:
    suggestion = _build_title_suggestion(source_title, ["cordless vacuum cleaner"])

    assert suggestion.startswith("Cordless Vacuum Cleaner")
    assert suggestion.endswith("2 Pack 220V 1500W 2L 35CM 4KG IPX4") or suggestion.endswith(
        "2 Pack 220 V 1500 W 2 L 35 CM 4 KG IPX4"
    )


def test_title_parameter_sort_keeps_model_identity_and_connectivity_in_front() -> None:
    suggestion = _build_title_suggestion(
        "256GB Samsung Galaxy S24 5G Smartphone 4K Video",
        ["samsung galaxy s24 5g smartphone"],
    )

    assert suggestion == "Samsung Galaxy S24 5G Smartphone Video 256GB 4K"


def test_all_three_title_playbooks_put_specs_last() -> None:
    strategies = _build_title_strategies(
        source_title="300W Outdoor RGB LED Floodlight Smart App Control IP66 Waterproof",
        accepted_keywords=["rgb led floodlight"],
        hot_term_keywords=["rgb led floodlight", "outdoor floodlight"],
        opportunity_keywords=["garden floodlight"],
    )

    assert all(strategy["available"] is True for strategy in strategies)
    assert all(
        str(strategy["title"]).endswith("300W IP66") for strategy in strategies
    )


def test_hot_term_strategy_merges_overlapping_phrases_without_keyword_stutter() -> None:
    suggestion = _build_hot_term_title_suggestion(
        "Silent Rechargeable Wireless Gaming Mouse",
        ["wireless mouse", "wireless gaming mouse"],
    )

    assert suggestion == "Wireless Gaming Mouse Silent Rechargeable"
    assert all(character.isalnum() or character == " " for character in suggestion)


def test_title_keywords_prioritize_known_long_tail_and_diverse_instinct_roots() -> None:
    rows = [
        _observation(
            "rgb light bars",
            5,
            validation_evidence={
                "journey_type": "first_instinct_autocomplete",
                "journey_root": "rgb light",
                "candidate_provenance": [
                    {
                        "candidate_source": "takealot_autocomplete",
                        "autocomplete_rank": 1,
                        "journey_type": "first_instinct_autocomplete",
                        "journey_root": "rgb light",
                    },
                    {
                        "candidate_source": "image_precise",
                        "journey_type": "known_long_tail",
                        "journey_root": "rgb light bars",
                    },
                ],
            },
        ),
        _observation(
            "rgb led light bars",
            6,
            validation_evidence={
                "candidate_provenance": [
                    {
                        "candidate_source": "takealot_autocomplete",
                        "autocomplete_rank": 2,
                        "journey_type": "autocomplete_backtrack",
                        "journey_root": "rgb light",
                    }
                ]
            },
        ),
        _observation(
            "gaming light bars",
            7,
            validation_evidence={
                "candidate_provenance": [
                    {
                        "candidate_source": "takealot_autocomplete",
                        "autocomplete_rank": 3,
                        "journey_type": "switched_instinct_root",
                        "journey_root": "gaming light",
                    }
                ]
            },
        ),
    ]

    accepted, hot_terms, opportunity = _title_strategy_keywords(
        rows,
        "RGB LED Gaming Light Bars",
    )

    assert accepted[0] == "rgb light bars"
    assert hot_terms[:2] == ["rgb light bars", "gaming light bars"]
    assert hot_terms[2] == "rgb led light bars"
    assert opportunity == []


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
    assert autocomplete_added["opportunity_unsupported_autocomplete_terms"] == ["leather"]
    assert unsupported_distinctive["opportunity_claims_safe"] is False
    assert unsupported_distinctive["opportunity_unsupported_distinctive_terms"] == ["tufted"]
    assert safe_need_state["opportunity_claims_safe"] is True


def test_compressed_sofa_requires_title_or_manual_fact_support_for_naming() -> None:
    unconfirmed = _opportunity_phrase_safety(
        keyword="compressed sofa",
        source_title="Corduroy Lazy Sofa Chair",
        opportunity_seeds=[],
        distinctive_terms=[],
    )
    manually_confirmed = _opportunity_phrase_safety(
        keyword="compressed sofa",
        source_title="Corduroy Lazy Sofa Chair compressed sofa",
        opportunity_seeds=[],
        distinctive_terms=["compressed sofa"],
    )

    assert unconfirmed["opportunity_claims_safe"] is False
    assert unconfirmed["opportunity_unsupported_fact_terms"] == ["compressed"]
    assert manually_confirmed["opportunity_claims_safe"] is True


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
    assert "too_many_direct_competitors" in evidence["opportunity_rejection_reasons"]
    assert "target_beyond_organic_rank_72" in evidence["opportunity_rejection_reasons"]
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
    assert "semantic_relation_not_s_or_a" in evidence["opportunity_rejection_reasons"]


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
                    "opportunity_title_suggestion": ("Mouse For Laptop Gaming Mouse Silent")
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
        item for item in snapshot["issued_strategies"] if item["strategy"] == "adjacent_opportunity"
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
    source_title = "2 RGB LED Light Bars TV Backlight with Remote Ambient Lighting Gaming Desk"
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
        opportunity_seeds=[KeywordCandidate(phrase="gaming lights", rationale="Adjacent room use")],
        exclusions=["light bulb", "led strip"],
        confidence=0.95,
        title_suggestion="RGB Light Bars TV Backlight Ambient Lighting",
        title_reason="Image-only hypothesis",
    )

    normalized, recognition = _cross_check_image_profile(profile, source_title)

    assert normalized.product_name != source_title
    assert len(normalized.product_name.split()) <= 7
    assert normalized.product_name == "RGB light bars ambient lighting remote control"
    assert recognition["model_received_source_title"] is True
    assert recognition["visual_stage_received_source_title"] is False
    assert recognition["product_name_adjusted"] is True
    assert "RGB light bars" in recognition["title_reference_terms"]

    claim_checked, claim_evidence = _cross_check_image_profile(
        profile.model_copy(update={"product_name": "Smart RGB light bars"}),
        source_title,
    )
    assert claim_checked.product_name == "RGB light bars"
    assert claim_evidence["removed_unconfirmed_identity_terms"] == ["Smart"]


def test_floodlight_title_semantically_cross_certifies_flood_light_image() -> None:
    profile = VisionProfile(
        product_name="RGB LED Flood Light with Remote",
        category="Outdoor Lighting",
        product_type_terms=[
            "flood light",
            "outdoor led light",
            "security light",
            "rgb spotlight",
        ],
        distinctive_terms=["remote control", "colour changing"],
        keywords=[
            KeywordCandidate(
                phrase="colour changing outdoor led floodlight",
                rationale="South African shopper long tail",
            ),
            KeywordCandidate(
                phrase="rgb led security flood light",
                rationale="Known long tail",
            ),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="flood light", rationale="Shopper root"),
            KeywordCandidate(phrase="rgb light", rationale="Colour root"),
        ],
        opportunity_seeds=[
            KeywordCandidate(phrase="garden colour light", rationale="Adjacent use")
        ],
        exclusions=["solar flood light"],
        confidence=0.95,
        title_suggestion="RGB LED Flood Light Outdoor Colour Changing",
        title_reason="Image-only suggestion",
    )

    normalized, recognition = _cross_check_image_profile(
        profile,
        "300W Outdoor RGB LED Floodlight Smart App Control IP66 Waterproof",
    )

    # The historical run scored this at 0.3333 because ``floodlight`` and
    # ``flood light`` were treated as different identities.
    assert recognition["source_title_similarity"] >= 0.40
    assert recognition["title_identity_support"] is True
    assert recognition["title_identity_supported_terms"] == ["flood light"]
    assert "flood light" in recognition["title_reference_terms"]
    assert normalized.product_name == "RGB LED Flood Light with"


def test_generic_light_modifier_overlap_does_not_cross_certify_wrong_type() -> None:
    profile = _opportunity_profile().model_copy(
        update={
            "product_name": "RGB LED Flood Light",
            "product_type_terms": ["flood light"],
        }
    )

    _, recognition = _cross_check_image_profile(
        profile,
        "RGB Outdoor Table Light Smart App Control",
    )

    assert recognition["title_identity_support"] is False
    assert recognition["title_identity_supported_terms"] == []
    assert recognition["title_identity_matches"][0]["identity_match_rule"] == (
        "generic_head_without_matching_tail_rejected"
    )


@pytest.mark.asyncio
async def test_core_validation_uses_the_complete_first_page_majority() -> None:
    class FirstPageClient:
        async def fetch_search_first_page(
            self,
            keyword: str,
        ) -> tuple[str, dict[str, Any]]:
            products = [(str(60_000_000 + index), f"Wireless Mouse {index}") for index in range(20)]
            products.extend(
                (str(61_000_000 + index), f"Laptop Sleeve {index}") for index in range(16)
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


@pytest.mark.asyncio
async def test_same_type_validation_treats_floodlight_as_flood_light() -> None:
    class FloodlightFirstPageClient:
        async def fetch_search_first_page(
            self,
            keyword: str,
        ) -> tuple[str, dict[str, Any]]:
            products = [
                (
                    str(62_000_000 + index),
                    f"Outdoor RGB LED Floodlight IP66 Model {index}",
                )
                for index in range(36)
            ]
            return _search_url(keyword), _payload(products, after="", total=900)

    profile = VisionProfile(
        product_name="RGB LED Flood Light",
        category="Outdoor Lighting",
        product_type_terms=["flood light"],
        distinctive_terms=["colour changing"],
        keywords=[
            KeywordCandidate(
                phrase="colour changing outdoor led floodlight",
                rationale="South African shopper long tail",
            ),
            KeywordCandidate(
                phrase="rgb led flood light",
                rationale="Known long tail",
            ),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="flood light", rationale="Shopper root"),
            KeywordCandidate(phrase="rgb light", rationale="Colour root"),
        ],
        opportunity_seeds=[KeywordCandidate(phrase="garden light", rationale="Adjacent use")],
        exclusions=["solar light"],
        confidence=0.95,
        title_suggestion="RGB LED Flood Light Outdoor",
        title_reason="Image-only suggestion",
    )

    observation = await _collect_keyword_observation(
        FloodlightFirstPageClient(),  # type: ignore[arg-type]
        candidate=SearchKeywordCandidate(
            phrase="colour changing outdoor led floodlight",
            rationale="South African shopper long tail",
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

    assert observation.relevance_status == "accepted"
    assert observation.relevance_score == 1.0
    assert observation.validation_evidence["matched_first_page_results"] == 36
    assert observation.validation_evidence["page_validation_status"] == "completed"


def _opportunity_profile() -> VisionProfile:
    return VisionProfile(
        product_name="Rechargeable wireless gaming mouse",
        category="Computer mice",
        product_type_terms=["mouse", "wireless mouse"],
        same_product_aliases=["computer mouse", "gaming mouse"],
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
            KeywordCandidate(
                phrase="mouse for laptop",
                rationale="Laptop navigation demand",
                buyer_job="control a laptop pointer",
                alternative_product_terms=["trackpad", "trackball"],
            )
        ],
        exclusions=["keyboard"],
        confidence=0.9,
        title_suggestion="Rechargeable Wireless Gaming Mouse",
        title_reason="Image-only hypothesis",
    )


def _fusion_ready_profile(base: VisionProfile | None = None) -> VisionProfile:
    profile = base or _opportunity_profile()
    return profile.model_copy(
        update={
            "keywords": [
                KeywordCandidate(phrase="wireless gaming mouse", rationale="Exact intent"),
                KeywordCandidate(phrase="rechargeable wireless mouse", rationale="Power intent"),
                KeywordCandidate(phrase="silent computer mouse", rationale="Noise intent"),
                KeywordCandidate(phrase="ergonomic mouse for laptop", rationale="Use intent"),
                KeywordCandidate(phrase="wireless office mouse", rationale="Work intent"),
                KeywordCandidate(phrase="portable optical mouse", rationale="Form intent"),
            ],
            "autocomplete_seeds": [
                KeywordCandidate(phrase="mouse", rationale="Product instinct"),
                KeywordCandidate(phrase="wireless mouse", rationale="Connection instinct"),
                KeywordCandidate(phrase="gaming mouse", rationale="Use instinct"),
                KeywordCandidate(phrase="laptop mouse", rationale="Device instinct"),
                KeywordCandidate(phrase="office mouse", rationale="Work instinct"),
                KeywordCandidate(phrase="silent mouse", rationale="Feature instinct"),
            ],
            "same_product_aliases": ["computer mouse", "gaming mouse"],
            "opportunity_seeds": [
                AdjacentDemandCandidate(
                    phrase="laptop",
                    rationale="Alternative laptop navigation need",
                    buyer_job="control a laptop pointer",
                    alternative_product_terms=["trackpad", "trackball"],
                    excluded_product_terms=["mouse pad", "laptop sleeve"],
                )
            ],
        }
    )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("market_context", "United States"),
        ("language_variant", "American English"),
        ("shopper_context", "generic global shoppers"),
    ],
)
def test_fusion_profile_rejects_non_south_african_localization_context(
    field_name: str,
    invalid_value: str,
) -> None:
    payload = _fusion_ready_profile().model_dump(mode="json")
    payload[field_name] = invalid_value

    with pytest.raises(ValueError) as exc_info:
        FusionVisionProfile.model_validate(payload)

    assert field_name in str(exc_info.value)


@pytest.mark.parametrize("profile_type", [LocalizedVisionProfile, FusionVisionProfile])
@pytest.mark.parametrize(
    "field_name",
    ["market_context", "language_variant", "shopper_context"],
)
def test_new_provider_profiles_require_explicit_south_african_localization_context(
    profile_type: type[VisionProfile],
    field_name: str,
) -> None:
    payload = _fusion_ready_profile().model_dump(mode="json")
    payload.pop(field_name)

    with pytest.raises(ValueError) as exc_info:
        profile_type.model_validate(payload)

    assert field_name in str(exc_info.value)


def _sofa_semantic_profile() -> VisionProfile:
    return VisionProfile(
        product_name="Corduroy floor chair",
        category="Living room seating",
        product_type_terms=["floor chair", "sofa chair"],
        same_product_aliases=["lazy sofa", "floor sofa"],
        distinctive_terms=["corduroy", "foldable"],
        keywords=[
            KeywordCandidate(phrase="corduroy floor chair", rationale="Visible product form"),
            KeywordCandidate(phrase="foldable lazy sofa", rationale="Direct product alias"),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="chair", rationale="Product root"),
            KeywordCandidate(phrase="sofa", rationale="Marketplace alias root"),
        ],
        opportunity_seeds=[
            KeywordCandidate(
                phrase="guest",
                rationale="Compact occasional guest seating demand",
                buyer_job="provide compact occasional seating for a guest",
                alternative_product_terms=[
                    "bean bag",
                    "folding ottoman",
                    "floor cushion",
                    "sleeper chair",
                ],
                excluded_product_terms=["chair cover", "sofa cover"],
            )
        ],
        exclusions=["chair cover", "sofa cover"],
        confidence=0.95,
        title_suggestion="Corduroy Floor Chair Foldable Lazy Sofa",
        title_reason="Image-title fused product identity",
    )


def _semantic_candidate(
    phrase: str,
    *,
    root: str | None = None,
    platform_expansion: bool = False,
) -> SearchKeywordCandidate:
    source = "takealot_root_expansion" if platform_expansion else "image_title_fused_precise"
    rank = 1 if platform_expansion else None
    provenance = (
        {
            "candidate_source": source,
            "intended_strategy": "opportunity",
            "seed": root,
            "root": root,
            "seed_source": "image_title_need_state",
            "autocomplete_rank": rank,
            "journey_root": root,
        },
    )
    return SearchKeywordCandidate(
        phrase=phrase,
        rationale="Semantic relation test",
        candidate_source=source,
        intended_strategy="opportunity",
        seed=root,
        seed_source="image_title_need_state",
        autocomplete_rank=rank,
        journey_root=root,
        candidate_provenance=provenance,
    )


@pytest.mark.parametrize(
    ("keyword", "profile"),
    [
        ("lazy sofa", _sofa_semantic_profile()),
        ("floor chair", _sofa_semantic_profile()),
        (
            "compressed sofa",
            _sofa_semantic_profile().model_copy(
                update={"product_type_terms": ["floor chair", "compressed sofa"]}
            ),
        ),
    ],
    ids=["direct-marketplace-alias", "primary-product-name", "confirmed-product-fact"],
)
def test_semantic_relation_recognizes_multiple_s_grade_paths(
    keyword: str,
    profile: VisionProfile,
) -> None:
    evidence = _semantic_relation_evidence(
        keyword=keyword,
        first_page_titles=[
            "Corduroy Foldable Lazy Sofa Chair",
            "Navy Floor Chair For Living Room",
            "Compressed Sofa Lounge Seat",
            "Unrelated Home Decor",
        ],
        profile=profile,
        candidate=_semantic_candidate(keyword),
        core_threshold=0.60,
    )

    assert evidence["semantic_relation_grade"] == "S"
    assert evidence["semantic_relation_query_same_product_terms"]
    assert evidence["semantic_relation_source_priority_decides_grade"] is False


def test_semantic_relation_recognizes_s_from_first_page_same_product_majority() -> None:
    titles = [
        *(f"Foldable Lazy Sofa Chair Model {index}" for index in range(24)),
        *(f"Compact Living Room Decor {index}" for index in range(12)),
    ]
    evidence = _semantic_relation_evidence(
        keyword="compact lounge seating",
        first_page_titles=titles,
        profile=_sofa_semantic_profile(),
        candidate=_semantic_candidate("compact lounge seating"),
        core_threshold=0.60,
    )

    assert evidence["semantic_relation_grade"] == "S"
    assert evidence["semantic_relation_decision"] == "first_page_same_product_majority"
    assert evidence["semantic_relation_query_same_product_terms"] == []
    assert evidence["semantic_relation_same_product_result_count"] == 24


@pytest.mark.parametrize(
    "keyword",
    ["lazy susan", "lazy sofa cover", "floor chair cushion", "sofa cleaning machine"],
)
def test_semantic_relation_merges_complementary_and_irrelevant_into_c_i(
    keyword: str,
) -> None:
    evidence = _semantic_relation_evidence(
        keyword=keyword,
        first_page_titles=[
            "Lazy Susan Turntable",
            "Stretch Sofa Cover",
            "Floor Chair Cushion Replacement",
            "Upholstery Cleaning Machine",
        ],
        profile=_sofa_semantic_profile(),
        candidate=_semantic_candidate(keyword),
        core_threshold=0.60,
    )

    assert evidence["semantic_relation_grade"] == "C/I"
    assert evidence["semantic_relation_label"] == "complementary_or_irrelevant_rejected"


def _coherent_guest_seating_titles() -> list[str]:
    titles = [
        *(f"Compact Bean Bag Chair Model {index}" for index in range(8)),
        *(f"Folding Ottoman Guest Seat {index}" for index in range(8)),
        *(f"Floor Cushion Seating Model {index}" for index in range(8)),
        "Corduroy Floor Chair For Home",
        "Lazy Sofa Chair Foldable",
    ]
    titles.extend(f"Decorative Guest Room Item {index}" for index in range(10))
    return titles


def test_semantic_relation_recognizes_a_only_from_buyer_job_and_coherent_alternatives() -> None:
    evidence = _semantic_relation_evidence(
        keyword="compact guest seating",
        first_page_titles=_coherent_guest_seating_titles(),
        profile=_sofa_semantic_profile(),
        candidate=_semantic_candidate("compact guest seating", root="guest"),
        core_threshold=0.60,
    )

    assert evidence["semantic_relation_grade"] == "A"
    assert evidence["semantic_relation_adjacent_result_count"] == 24
    assert evidence["semantic_relation_same_product_result_count"] == 2
    assert evidence["semantic_relation_supported_ratio"] == pytest.approx(26 / 36, abs=0.0001)
    assert evidence["semantic_relation_buyer_jobs"] == [
        "provide compact occasional seating for a guest"
    ]
    assert evidence["semantic_relation_source_priority_decides_grade"] is False


def test_semantic_relation_rejects_an_a_hypothesis_when_page_is_fragmented() -> None:
    titles = [
        *(f"Bean Bag Chair Model {index}" for index in range(6)),
        *(f"Folding Ottoman Model {index}" for index in range(4)),
        "Corduroy Floor Chair",
        "Lazy Sofa Chair",
        *(f"Guest Room Decoration {index}" for index in range(24)),
    ]
    evidence = _semantic_relation_evidence(
        keyword="compact guest seating",
        first_page_titles=titles,
        profile=_sofa_semantic_profile(),
        candidate=_semantic_candidate("compact guest seating", root="guest"),
        core_threshold=0.60,
    )

    assert evidence["semantic_relation_grade"] == "C/I"
    assert evidence["semantic_relation_decision"] == (
        "adjacent_hypothesis_not_supported_by_first_page"
    )
    assert evidence["semantic_relation_adjacent_page_qualified"] is False


def test_semantic_relation_does_not_count_alternative_accessories_as_a() -> None:
    titles = [
        *(f"Bean Bag Cover Replacement {index}" for index in range(10)),
        *(f"Floor Cushion Cover Set {index}" for index in range(10)),
        *(f"Compact Bean Bag Chair {index}" for index in range(4)),
        *(f"Guest Room Decoration {index}" for index in range(12)),
    ]
    evidence = _semantic_relation_evidence(
        keyword="compact guest seating",
        first_page_titles=titles,
        profile=_sofa_semantic_profile(),
        candidate=_semantic_candidate("compact guest seating", root="guest"),
        core_threshold=0.60,
    )

    assert evidence["semantic_relation_grade"] == "C/I"
    assert evidence["semantic_relation_adjacent_result_count"] == 4
    assert evidence["semantic_relation_rejected_result_count"] == 32


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform_expansion", "expected_status", "expected_reason"),
    [
        (True, "opportunity", None),
        (False, "rejected_irrelevant", "missing_platform_root_expansion"),
    ],
)
async def test_a_grade_is_independent_from_platform_blue_ocean_gate(
    platform_expansion: bool,
    expected_status: str,
    expected_reason: str | None,
) -> None:
    products = [
        (str(82_000_000 + index), title)
        for index, title in enumerate(_coherent_guest_seating_titles())
    ]
    products[4] = ("12345678", "Corduroy Floor Chair For Home")
    observation = await _collect_keyword_observation(
        OpportunityGateSearchClient([products]),  # type: ignore[arg-type]
        candidate=_semantic_candidate(
            "compact guest seating",
            root="guest",
            platform_expansion=platform_expansion,
        ),
        candidate_order=1,
        target_plid="12345678",
        profile=_sofa_semantic_profile(),
        max_pages=2,
        relevance_threshold=0.60,
        page_delay_seconds=0,
        source_title="Corduroy Lazy Sofa Chair Foldable",
    )

    assert observation.validation_evidence["semantic_relation_grade"] == "A"
    assert observation.relevance_status == expected_status
    assert observation.validation_evidence["blue_ocean_qualified"] is platform_expansion
    if expected_reason:
        assert expected_reason in observation.validation_evidence[
            "blue_ocean_rejection_reasons"
        ]


@pytest.mark.asyncio
async def test_title_lazy_sofa_is_s_blue_ocean_when_model_alias_omits_it() -> None:
    products = [
        (str(84_000_000 + index), f"Lazy Susan Kitchen Turntable {index}")
        for index in range(36)
    ]
    products[3] = ("84001001", "Foldable Lazy Sofa Lounge Seat")
    products[19] = ("12345678", "Corduroy Floor Chair Lazy Sofa")
    observation = await _collect_keyword_observation(
        OpportunityGateSearchClient([products]),  # type: ignore[arg-type]
        candidate=_semantic_candidate(
            "lazy sofa",
            root="lazy",
            platform_expansion=True,
        ),
        candidate_order=1,
        target_plid="12345678",
        profile=_sofa_semantic_profile().model_copy(
            update={"same_product_aliases": ["floor sofa"]}
        ),
        max_pages=2,
        relevance_threshold=0.60,
        page_delay_seconds=0,
        source_title="Corduroy Lazy Sofa Chair Foldable",
    )

    assert observation.validation_evidence["semantic_relation_grade"] == "S"
    assert observation.validation_evidence["semantic_relation_decision"] == (
        "query_is_current_title_product_phrase"
    )
    assert observation.validation_evidence["semantic_relation_current_title_alias"] == (
        "lazy sofa"
    )
    assert "lazy sofa" in observation.validation_evidence[
        "semantic_relation_same_product_terms"
    ]
    assert observation.validation_evidence["semantic_relation_same_product_result_count"] == 2
    assert observation.relevance_score == pytest.approx(2 / 36)
    assert observation.relevance_status == "opportunity"
    assert observation.validation_evidence["blue_ocean_qualified"] is True


def _opportunity_candidate(
    *,
    autocomplete: bool = True,
) -> SearchKeywordCandidate:
    return SearchKeywordCandidate(
        phrase="mouse for laptop",
        rationale="Adjacent laptop-use demand",
        candidate_source=("takealot_autocomplete" if autocomplete else "image_precise"),
        intended_strategy="opportunity",
        seed="mouse for laptop" if autocomplete else None,
        seed_source="image_need_state" if autocomplete else "image_only_model",
        autocomplete_rank=1 if autocomplete else None,
    )


@pytest.mark.asyncio
async def test_same_seed_core_and_opportunity_is_requested_once_with_both_provenances() -> None:
    profile = _opportunity_profile().model_copy(
        update={
            "autocomplete_seeds": [KeywordCandidate(phrase="mouse", rationale="Core root")],
            "opportunity_seeds": [KeywordCandidate(phrase="mouse", rationale="Need-state root")],
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

    assert client.calls.count("mouse") == 1
    mouse_check = next(item for item in checks if item["root"] == "mouse")
    assert mouse_check["intended_strategies"] == ["core", "opportunity"]
    assert {str(item["intended_strategy"]) for item in candidate.candidate_provenance} == {
        "core",
        "opportunity",
    }


@pytest.mark.asyncio
async def test_discovery_reads_complete_roots_without_typing_paths() -> None:
    profile = VisionProfile(
        product_name="RGB light bars",
        category="Lighting",
        product_type_terms=["light bars"],
        distinctive_terms=["rgb", "ambient"],
        keywords=[
            KeywordCandidate(phrase="rgb light bars", rationale="Known long tail"),
            KeywordCandidate(phrase="ambient light bars", rationale="Known use"),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="rgb light", rationale="First colour instinct"),
            KeywordCandidate(phrase="ambient light", rationale="First use instinct"),
        ],
        opportunity_seeds=[
            KeywordCandidate(phrase="gaming lights", rationale="Adjacent gaming use")
        ],
        exclusions=["light bulb", "led strip"],
        confidence=0.95,
        title_suggestion="RGB Light Bars Ambient Lighting",
        title_reason="Image-only suggestion",
    )

    class PathSuggestionClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def fetch_search_suggestions(self, keyword: str) -> list[str]:
            self.calls.append(keyword)
            suggestions = {
                "rgb light": ["rgb light bar"],
                "ambient light": ["ambient lights for room"],
                "gaming lights": ["gaming light bars"],
            }
            return suggestions.get(keyword, [])

    client = PathSuggestionClient()
    candidates, checks = await _discover_keyword_candidates(
        client,  # type: ignore[arg-type]
        profile=profile,
        source_title="RGB Ambient Light Bars",
        title_reference_terms=[],
        max_keywords=4,
    )
    rgb_candidate = next(item for item in candidates if item.phrase == "rgb light bar")

    assert client.calls[:2] == ["rgb light", "ambient light"]
    assert "light bars" in client.calls
    assert "gaming lights" in client.calls
    assert len(checks) <= 20
    assert rgb_candidate.candidate_source == "takealot_root_expansion"
    assert rgb_candidate.journey_root == "rgb light"
    assert rgb_candidate.journey_path == (
        "rgb light",
        "rgb light bar",
    )
    assert any(
        item["root"] == "rgb light"
        and item["input_kind"] == "complete_root_expansion"
        and item["expansions"][0]["phrase"] == "rgb light bar"
        and item["expansions"][0]["relevance_status"] == "eligible"
        for item in checks
    )
    assert all(not item["input_state"].endswith(" l") for item in checks)


@pytest.mark.asyncio
async def test_discovery_queries_lazy_phrase_and_rejects_unrelated_raw_expansions() -> None:
    profile = VisionProfile(
        product_name="Floor chair",
        category="Living room seating",
        product_type_terms=["chair", "sofa"],
        distinctive_terms=["corduroy", "foldable"],
        keywords=[
            KeywordCandidate(phrase="corduroy floor chair", rationale="Visible form"),
            KeywordCandidate(phrase="foldable lounge seat", rationale="Visible use"),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="floor chair", rationale="Model root"),
            KeywordCandidate(phrase="lounge seat", rationale="Model root"),
        ],
        opportunity_seeds=[
            KeywordCandidate(phrase="reading chair", rationale="Adjacent use")
        ],
        exclusions=["office chair"],
        confidence=0.9,
        title_suggestion="Corduroy Floor Chair Foldable Seat",
        title_reason="Visible product form",
    )

    class LazyRootClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def fetch_search_suggestions(self, keyword: str) -> list[str]:
            self.calls.append(keyword)
            if keyword == "lazy":
                return [
                    "lazy susan",
                    "lazy boy recliner",
                    "lazy makoti cookbook",
                    "lazy boy",
                    "lazy makoti",
                ]
            return []

    client = LazyRootClient()
    candidates, checks = await _discover_keyword_candidates(
        client,  # type: ignore[arg-type]
        profile=profile,
        source_title=(
            "Corduroy Lazy Sofa Chair - Foldable Multi Functional Seat for Home - Navy Blue"
        ),
        title_reference_terms=[],
        max_keywords=6,
    )

    assert "lazy" in client.calls
    assert "lazy sofa" in client.calls
    lazy_check = next(item for item in checks if item["root"] == "lazy")
    assert lazy_check["expansions"] == [
        {
            "phrase": "lazy susan",
            "rank": 1,
            "relevance_status": "rejected_irrelevant",
            "relation": "irrelevant",
            "reason": "no_product_identity_or_structured_adjacent_match",
            "matched_terms": [],
            "used_as_followup_root": False,
        },
        {
            "phrase": "lazy boy recliner",
            "rank": 2,
            "relevance_status": "rejected_irrelevant",
            "relation": "irrelevant",
            "reason": "no_product_identity_or_structured_adjacent_match",
            "matched_terms": [],
            "used_as_followup_root": False,
        },
        {
            "phrase": "lazy makoti cookbook",
            "rank": 3,
            "relevance_status": "rejected_irrelevant",
            "relation": "irrelevant",
            "reason": "no_product_identity_or_structured_adjacent_match",
            "matched_terms": [],
            "used_as_followup_root": False,
        },
        {
            "phrase": "lazy boy",
            "rank": 4,
            "relevance_status": "rejected_irrelevant",
            "relation": "irrelevant",
            "reason": "no_product_identity_or_structured_adjacent_match",
            "matched_terms": [],
            "used_as_followup_root": False,
        },
        {
            "phrase": "lazy makoti",
            "rank": 5,
            "relevance_status": "rejected_irrelevant",
            "relation": "irrelevant",
            "reason": "no_product_identity_or_structured_adjacent_match",
            "matched_terms": [],
            "used_as_followup_root": False,
        },
    ]
    assert lazy_check["eligible_expansion_count"] == 0
    assert not {
        "lazy susan",
        "lazy boy recliner",
        "lazy makoti cookbook",
        "lazy boy",
        "lazy makoti",
    } & {item.phrase for item in candidates}


@pytest.mark.asyncio
async def test_related_platform_expansion_becomes_a_phrase_root_for_one_followup() -> None:
    profile = VisionProfile(
        product_name="Corduroy floor sofa chair",
        category="Living room seating",
        product_type_terms=["floor chair", "sofa chair"],
        same_product_aliases=["lazy sofa", "floor sofa"],
        distinctive_terms=["corduroy", "foldable"],
        keywords=[
            KeywordCandidate(phrase="corduroy floor chair", rationale="Visible form"),
            KeywordCandidate(phrase="foldable lazy sofa", rationale="Direct alias"),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="corduroy", rationale="Material entry"),
            KeywordCandidate(phrase="lazy sofa", rationale="Product phrase entry"),
        ],
        opportunity_seeds=[
            AdjacentDemandCandidate(
                phrase="guest seating",
                rationale="Compact guest seating",
                buyer_job="provide compact occasional seating for a guest",
                alternative_product_terms=["bean bag", "floor cushion"],
            )
        ],
        exclusions=["sofa cover", "chair cover"],
        confidence=0.95,
        title_suggestion="Corduroy Floor Chair Foldable Lazy Sofa",
        title_reason="Image-title fused identity",
    )

    class PhraseFollowupClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def fetch_search_suggestions(self, keyword: str) -> list[str]:
            self.calls.append(keyword)
            return {
                "corduroy": ["corduroy jacket", "corduroy floor sofa"],
                "corduroy floor sofa": [
                    "corduroy floor sofa chair",
                    "corduroy sofa cover",
                ],
            }.get(keyword, [])

    client = PhraseFollowupClient()
    candidates, checks = await _discover_keyword_candidates(
        client,  # type: ignore[arg-type]
        profile=profile,
        source_title=(
            "Corduroy Lazy Sofa Chair - Foldable Multi Functional Seat for Home - Blue"
        ),
        title_reference_terms=[],
        max_keywords=10,
    )

    assert "corduroy floor sofa" in client.calls
    corduroy_check = next(item for item in checks if item["root"] == "corduroy")
    assert corduroy_check["expansions"][0]["phrase"] == "corduroy jacket"
    assert corduroy_check["expansions"][0]["relevance_status"] == "rejected_irrelevant"
    assert corduroy_check["expansions"][1]["relevance_status"] == "eligible"
    assert corduroy_check["expansions"][1]["used_as_followup_root"] is True
    followup_check = next(item for item in checks if item["root"] == "corduroy floor sofa")
    assert followup_check["journey_depth"] == 1
    assert followup_check["parent_root"] == "corduroy"
    assert followup_check["journey_path"] == ["corduroy", "corduroy floor sofa"]
    assert followup_check["expansions"][1]["relevance_status"] == "rejected_irrelevant"
    assert "corduroy floor sofa chair" in {item.phrase for item in candidates}
    assert "corduroy jacket" not in {item.phrase for item in candidates}
    assert "corduroy sofa cover" not in {item.phrase for item in candidates}


@pytest.mark.asyncio
async def test_structured_adjacent_root_keeps_a_related_alternative_product_family() -> None:
    profile = _sofa_semantic_profile()

    class AdjacentSuggestionClient:
        async def fetch_search_suggestions(self, keyword: str) -> list[str]:
            if keyword == "guest":
                return ["lazy susan", "bean bag chair"]
            return []

    candidates, checks = await _discover_keyword_candidates(
        AdjacentSuggestionClient(),  # type: ignore[arg-type]
        profile=profile,
        source_title="Corduroy Lazy Sofa Chair - Foldable Seat for Home - Blue",
        title_reference_terms=[],
        max_keywords=10,
    )

    guest_check = next(item for item in checks if item["root"] == "guest")
    assert guest_check["expansions"][0]["relevance_status"] == "rejected_irrelevant"
    assert guest_check["expansions"][1]["relevance_status"] == "eligible"
    assert guest_check["expansions"][1]["relation"] == "adjacent_demand"
    bean_bag = next(item for item in candidates if item.phrase == "bean bag chair")
    assert bean_bag.intended_strategy == "opportunity"
    assert any(
        item["intended_strategy"] == "opportunity"
        and item["seed_source"] == "image_title_need_state"
        for item in bean_bag.candidate_provenance
    )


@pytest.mark.asyncio
async def test_root_sources_follow_operator_priority_and_keep_all_provenance() -> None:
    profile = VisionProfile(
        product_name="Corduroy floor sofa chair",
        category="Living room seating",
        product_type_terms=["sofa chair"],
        distinctive_terms=["corduroy", "foldable"],
        keywords=[
            KeywordCandidate(phrase="corduroy sofa chair", rationale="Visible form"),
            KeywordCandidate(phrase="foldable floor sofa", rationale="Visible use"),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="sofa", rationale="Fusion noun"),
            KeywordCandidate(phrase="lounger", rationale="Fusion wording"),
        ],
        opportunity_seeds=[
            KeywordCandidate(phrase="reading", rationale="Adjacent reading use")
        ],
        exclusions=[],
        confidence=0.95,
        title_suggestion="Corduroy Floor Sofa Chair",
        title_reason="Visible product form",
    )

    class PrioritySuggestionClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def fetch_search_suggestions(self, keyword: str) -> list[str]:
            self.calls.append(keyword)
            return ["lazy sofa"] if keyword == "lazy" else []

    client = PrioritySuggestionClient()
    _, checks = await _discover_keyword_candidates(
        client,  # type: ignore[arg-type]
        profile=profile,
        source_title="Corduroy Lazy Sofa Chair compressed sofa",
        official_title="Corduroy Lazy Sofa Chair",
        title_reference_terms=["sofa chair"],
        confirmed_fact_records=[
            {
                "fact_type": "construction",
                "fact_term": "compressed sofa",
                "source_type": "manual_confirmation",
            }
        ],
        model_autocomplete_seeds=profile.autocomplete_seeds,
        model_opportunity_seeds=profile.opportunity_seeds,
        max_keywords=8,
    )

    assert client.calls == [
        "compressed sofa",
        "sofa",
        "lounger",
        "sofa chair",
        "lazy sofa chair",
        "corduroy lazy sofa chair",
        "corduroy",
        "lazy",
        "chair",
        "reading",
        "lazy sofa",
    ]
    assert checks[0]["root_source"] == "human_confirmed_product_fact"
    assert checks[0]["origin_phrases"] == ["compressed sofa"]
    assert checks[0]["root"] == "compressed sofa"
    sofa_chair_check = next(item for item in checks if item["root"] == "sofa chair")
    assert sofa_chair_check["seed_sources"] == ["title_word_root", "title_cross_check"]
    lazy_check = next(item for item in checks if item["root"] == "lazy")
    assert lazy_check["expansions"][0]["phrase"] == "lazy sofa"
    assert lazy_check["expansions"][0]["used_as_followup_root"] is True
    lazy_sofa_check = next(item for item in checks if item["root"] == "lazy sofa")
    assert lazy_sofa_check["journey_depth"] == 1


@pytest.mark.asyncio
async def test_supported_sofa_roots_reach_platform_query_candidates() -> None:
    profile = VisionProfile(
        product_name="Corduroy foldable floor chair",
        category="Living room floor seating",
        product_type_terms=[
            "floor chair",
            "folding sofa bed",
            "tatami mat chair",
            "lounge chair",
            "convertible floor seat",
        ],
        same_product_aliases=["foldable floor seat", "corduroy lounge chair"],
        distinctive_terms=["corduroy fabric", "segmented folding design"],
        keywords=[
            KeywordCandidate(
                phrase="corduroy foldable floor chair",
                rationale="South African direct product wording",
            ),
            KeywordCandidate(
                phrase="folding sofa bed for home",
                rationale="South African alternative product wording",
            ),
        ],
        autocomplete_seeds=[
            KeywordCandidate(
                phrase="corduroy lazy sofa",
                rationale="Image-title fused shopper root",
            ),
            KeywordCandidate(
                phrase="adjustable backrest chair",
                rationale="Image-title fused shopper root",
            ),
        ],
        opportunity_seeds=[
            KeywordCandidate(phrase="compact guest seating", rationale="Adjacent use")
        ],
        exclusions=["rigid frame sofa", "sofa cover", "office chair"],
        confidence=0.92,
        title_suggestion="Corduroy Foldable Floor Chair",
        title_reason="Image-title fused identity",
    )

    class SofaSuggestionClient:
        async def fetch_search_suggestions(self, keyword: str) -> list[str]:
            return {
                "corduroy lazy sofa": [
                    "corduroy lazy sofa chair",
                    "rigid frame sofa",
                ],
                "sofa chair": ["sofa chairs", "sofa cover"],
                "sofa": ["sofas", "sofa bed", "sofa covers"],
                "chair": ["chairs", "chair covers"],
            }.get(keyword, [])

    candidates, checks = await _discover_keyword_candidates(
        SofaSuggestionClient(),  # type: ignore[arg-type]
        profile=profile,
        source_title=(
            "Corduroy Lazy Sofa Chair Foldable Multi Functional Seat for Home Blue"
        ),
        official_title=(
            "Corduroy Lazy Sofa Chair - Foldable Multi-Functional Seat for Home - Blue"
        ),
        title_reference_terms=["floor chair", "lounge chair"],
        max_keywords=14,
    )

    platform_candidates = [
        item
        for item in candidates
        if item.candidate_source == "takealot_root_expansion"
        and item.adaptive_recovery_source is None
    ]
    platform_phrases = {item.phrase for item in platform_candidates}
    platform_roots = {item.journey_root for item in platform_candidates}

    assert "corduroy lazy sofa chair" in platform_phrases
    assert "sofa chairs" in platform_phrases
    assert {"corduroy lazy sofa", "sofa chair", "sofa"}.issubset(platform_roots)
    assert not {"rigid frame sofa", "sofa cover", "sofa covers"} & platform_phrases
    sofa_check = next(item for item in checks if item["root"] == "sofa")
    assert [item["relevance_status"] for item in sofa_check["expansions"]] == [
        "eligible",
        "eligible",
        "rejected_irrelevant",
    ]


@pytest.mark.asyncio
async def test_title_roots_keep_minimum_coverage_after_higher_priority_sources() -> None:
    model_roots = [f"modelroot{index}" for index in range(10)]
    cross_roots = [f"crossroot{index}" for index in range(8)]
    title_roots = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "theta", "iota"]
    profile = VisionProfile(
        product_name="Alpha seating",
        category="Living room seating",
        product_type_terms=["seating"],
        distinctive_terms=[],
        keywords=[
            KeywordCandidate(phrase="alpha seating", rationale="Visible form"),
            KeywordCandidate(phrase="floor seating", rationale="Visible use"),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase=root, rationale="Fusion root") for root in model_roots
        ],
        opportunity_seeds=[KeywordCandidate(phrase="reading", rationale="Adjacent use")],
        exclusions=[],
        confidence=0.95,
        title_suggestion="Alpha Floor Seating",
        title_reason="Visible product form",
    )

    class CoverageSuggestionClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def fetch_search_suggestions(self, keyword: str) -> list[str]:
            self.calls.append(keyword)
            return []

    client = CoverageSuggestionClient()
    await _discover_keyword_candidates(
        client,  # type: ignore[arg-type]
        profile=profile,
        source_title=" ".join(title_roots),
        official_title=" ".join(title_roots),
        title_reference_terms=cross_roots,
        model_autocomplete_seeds=profile.autocomplete_seeds,
        max_keywords=8,
    )

    assert len(client.calls) == 15
    assert client.calls[:6] == model_roots[:6]
    assert client.calls[6:14] == title_roots
    assert client.calls[-1] == "reading"
    assert not set(cross_roots) & set(client.calls)


@pytest.mark.asyncio
async def test_adaptive_ten_query_base_keeps_both_source_channels_and_five_roots() -> None:
    profile = VisionProfile(
        product_name="Wireless computer mouse",
        category="Computer mice",
        product_type_terms=["mouse"],
        distinctive_terms=["wireless", "gaming", "silent", "rechargeable"],
        keywords=[
            KeywordCandidate(
                phrase="rechargeable computer mouse",
                rationale="South African known-type wording",
            ),
            KeywordCandidate(
                phrase="silent optical mouse",
                rationale="South African feature-led wording",
            ),
            KeywordCandidate(
                phrase="ergonomic wireless mouse",
                rationale="South African use-led wording",
            ),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="mouse", rationale="Visible noun"),
            KeywordCandidate(phrase="gaming", rationale="Use instinct"),
            KeywordCandidate(phrase="laptop", rationale="Connected device instinct"),
            KeywordCandidate(phrase="silent", rationale="Feature instinct"),
            KeywordCandidate(phrase="office", rationale="Use-place instinct"),
        ],
        opportunity_seeds=[
            KeywordCandidate(phrase="work", rationale="Adjacent work need")
        ],
        exclusions=["keyboard"],
        confidence=0.95,
        title_suggestion="Wireless Computer Mouse Rechargeable Silent",
        title_reason="Image-only suggestion",
    )

    class MultiRootSuggestionClient:
        async def fetch_search_suggestions(self, keyword: str) -> list[str]:
            return {
                "mouse": ["wireless mouse"],
                "gaming": ["gaming mouse"],
                "laptop": ["laptop mouse"],
                "silent": ["silent mouse"],
                "office": ["office mouse"],
                "work": ["mouse for work"],
            }.get(keyword, [])

    candidates, _ = await _discover_keyword_candidates(
        MultiRootSuggestionClient(),  # type: ignore[arg-type]
        profile=profile,
        source_title=(
            "Ergonomic Wireless Rechargeable Silent Optical Gaming Laptop Computer Mouse For Work"
        ),
        title_reference_terms=[],
        max_keywords=10,
    )
    selected = [item for item in candidates if item.adaptive_recovery_source is None]
    platform = [
        item for item in selected if item.candidate_source == "takealot_root_expansion"
    ]
    model_direct = [
        item for item in selected if item.candidate_source == "image_title_fused_precise"
    ]
    platform_core_roots = {
        item.journey_root
        for item in platform
        if item.intended_strategy == "core" and item.journey_root
    }

    assert len(selected) == 9
    assert len(platform) == 6
    assert len(model_direct) == 3
    assert len(platform_core_roots) == 5
    assert sum(item.intended_strategy == "opportunity" for item in platform) == 1


@pytest.mark.asyncio
async def test_human_confirmed_projection_size_uses_one_bounded_query_slot() -> None:
    profile = VisionProfile(
        product_name="Portable projection screen with stand",
        category="Home Theatre & Projectors",
        product_type_terms=["projection screen", "outdoor movie screen"],
        distinctive_terms=["portable", "retractable"],
        keywords=[
            KeywordCandidate(
                phrase="portable projection screen with stand",
                rationale="Known long tail",
            ),
            KeywordCandidate(
                phrase="outdoor movie screen with stand",
                rationale="Local use wording",
            ),
            KeywordCandidate(
                phrase="collapsible projector screen with tripod",
                rationale="Feature-led wording",
            ),
            KeywordCandidate(
                phrase="large portable projection screen",
                rationale="Alternative wording",
            ),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="projector screen", rationale="Product instinct"),
            KeywordCandidate(phrase="outdoor screen", rationale="Use instinct"),
            KeywordCandidate(phrase="movie screen", rationale="Use instinct"),
            KeywordCandidate(phrase="portable screen", rationale="Feature instinct"),
        ],
        opportunity_seeds=[
            KeywordCandidate(phrase="backyard movie night", rationale="Adjacent need")
        ],
        exclusions=["projector device"],
        confidence=0.95,
        title_suggestion="Portable Projection Screen",
        title_reason="Image-only suggestion",
    )

    class ProjectionSuggestionClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def fetch_search_suggestions(self, keyword: str) -> list[str]:
            self.calls.append(keyword)
            return {
                "projector": ["projector screen"],
                "outdoor": ["outdoor projector screen"],
                "movie": ["movie projection screen"],
                "projection screen": ["projection screen cloth"],
                "projection screen 100 inch": ["projection screen 100 inch"],
                "backyard movie night": ["backyard movie screen"],
            }.get(keyword, [])

    client = ProjectionSuggestionClient()
    candidates, _ = await _discover_keyword_candidates(
        client,  # type: ignore[arg-type]
        profile=profile,
        source_title="100-Inch Portable High-Brightness Retractable Projection Screen",
        title_reference_terms=["projection screen"],
        decision_parameter_values=["100 Inch"],
        max_keywords=10,
    )
    selected = [item for item in candidates if item.adaptive_recovery_source is None]
    model_direct = [
        item for item in selected if item.candidate_source == "image_title_fused_precise"
    ]
    parameter = [
        item
        for item in selected
        if item.candidate_source == "human_confirmed_decision_parameter"
    ]

    assert len(selected) <= 10
    assert [item.phrase for item in model_direct] == [
        "portable projection screen with stand",
        "outdoor movie screen with stand",
        "collapsible projector screen with tripod",
        "large portable projection screen",
    ]
    assert [item.phrase for item in parameter] == ["100 inch projection screen"]
    assert parameter[0].candidate_provenance[0]["operator_confirmed"] is True
    assert "projection screen 100 inch" not in client.calls
    assert len(client.calls) <= 16


@pytest.mark.asyncio
async def test_adaptive_recovery_runs_only_when_three_platform_roots_do_not_pass() -> None:
    profile = _opportunity_profile()

    def platform_candidate(
        phrase: str,
        root: str,
        *,
        recovery: bool = False,
    ) -> SearchKeywordCandidate:
        return SearchKeywordCandidate(
            phrase=phrase,
            rationale="Takealot completion path",
            candidate_source="takealot_autocomplete",
            intended_strategy="core",
            seed=root,
            seed_source="image_shopper_root",
            autocomplete_rank=1,
            journey_type="first_instinct_autocomplete",
            journey_root=root,
            journey_path=(root, phrase),
            adaptive_recovery_source=("second_best_autocomplete" if recovery else None),
        )

    class AcceptedMouseSearchClient:
        def __init__(self) -> None:
            self.search_calls: list[str] = []

        async def fetch_search_suggestions(self, keyword: str) -> list[str]:
            raise AssertionError(keyword)

        async def fetch_search_first_page(
            self,
            keyword: str,
        ) -> tuple[str, dict[str, Any]]:
            self.search_calls.append(keyword)
            products = [
                ("12345678", "Wireless Gaming Mouse"),
                *(
                    (str(91_000_000 + index), f"Wireless Mouse Model {index}")
                    for index in range(35)
                ),
            ]
            return _search_url(keyword), _payload(products, after="", total=80)

        async def fetch_search_next_page(
            self,
            request_url: str,
            after: str,
        ) -> dict[str, Any]:
            raise AssertionError((request_url, after))

    three_root_client = AcceptedMouseSearchClient()
    base = [
        platform_candidate("wireless mouse", "mouse"),
        platform_candidate("gaming mouse", "gaming"),
        platform_candidate("laptop mouse", "laptop"),
    ]
    fallback = platform_candidate("office mouse", "office", recovery=True)
    observations, _, adaptive = await _collect_shopper_journey(
        three_root_client,  # type: ignore[arg-type]
        candidates=[*base, fallback],
        autocomplete_checks=[],
        target_plid="12345678",
        profile=profile,
        max_pages=2,
        max_keywords=10,
        relevance_threshold=0.60,
        source_title="Wireless Gaming Laptop Office Mouse",
    )

    assert len(observations) == 3
    assert three_root_client.search_calls == [
        "wireless mouse",
        "gaming mouse",
        "laptop mouse",
    ]
    assert adaptive["valid_platform_root_count"] == 3
    assert adaptive["adaptive_recovery_used"] is False
    assert adaptive["adaptive_recovery_skipped_reason"] == ("valid_platform_root_target_met")

    two_root_client = AcceptedMouseSearchClient()
    observations, steps, adaptive = await _collect_shopper_journey(
        two_root_client,  # type: ignore[arg-type]
        candidates=[*base[:2], fallback],
        autocomplete_checks=[],
        target_plid="12345678",
        profile=profile,
        max_pages=2,
        max_keywords=10,
        relevance_threshold=0.60,
        source_title="Wireless Gaming Laptop Office Mouse",
    )

    assert len(observations) == 3
    assert two_root_client.search_calls[-1] == "office mouse"
    assert adaptive["valid_platform_root_count"] == 3
    assert adaptive["adaptive_recovery_used"] is True
    assert adaptive["adaptive_recovery_source"] == "second_best_autocomplete"
    assert steps[-1]["adaptive_recovery"] is True


@pytest.mark.asyncio
async def test_rejected_page_can_learn_one_supported_seed_then_validate_live_completion() -> None:
    profile = VisionProfile(
        product_name="RGB light bars",
        category="Lighting",
        product_type_terms=["light bars"],
        distinctive_terms=["rgb", "ambient"],
        keywords=[
            KeywordCandidate(phrase="rgb light bars", rationale="Known long tail"),
            KeywordCandidate(phrase="tv light bars", rationale="Known TV use"),
        ],
        autocomplete_seeds=[
            KeywordCandidate(phrase="ambient", rationale="First use instinct"),
            KeywordCandidate(phrase="rgb", rationale="First colour instinct"),
        ],
        opportunity_seeds=[KeywordCandidate(phrase="gaming lights", rationale="Adjacent use")],
        exclusions=["light bulb", "led strip"],
        confidence=0.95,
        title_suggestion="RGB Light Bars Ambient Lighting",
        title_reason="Image-only suggestion",
    )

    class ResultLearningClient:
        def __init__(self) -> None:
            self.suggestion_calls: list[str] = []
            self.search_calls: list[str] = []

        async def fetch_search_suggestions(self, keyword: str) -> list[str]:
            self.suggestion_calls.append(keyword)
            return ["rgb light bars"] if keyword.casefold() == "light bars" else []

        async def fetch_search_first_page(
            self,
            keyword: str,
        ) -> tuple[str, dict[str, Any]]:
            self.search_calls.append(keyword)
            if keyword == "ambient lighting":
                products = [
                    ("71000001", "RGB Light Bars for Desk"),
                    ("71000002", "LED Light Bars for TV"),
                ]
                products.extend(
                    (str(72_000_000 + index), f"Ambient Light Bulb {index}") for index in range(34)
                )
                return _search_url(keyword), _payload(products, after="", total=90)
            products = [("12345678", "RGB LED Light Bars TV Backlight")]
            products.extend(
                (str(73_000_000 + index), f"RGB Light Bars Model {index}") for index in range(35)
            )
            return _search_url(keyword), _payload(products, after="", total=120)

        async def fetch_search_next_page(
            self,
            request_url: str,
            after: str,
        ) -> dict[str, Any]:
            raise AssertionError((request_url, after))

    client = ResultLearningClient()
    autocomplete_checks: list[dict[str, Any]] = []
    observations, steps, adaptive = await _collect_shopper_journey(
        client,  # type: ignore[arg-type]
        candidates=[
            SearchKeywordCandidate(
                phrase="ambient lighting",
                rationale="Broad first completion",
                candidate_source="takealot_autocomplete",
                intended_strategy="core",
                seed="ambient",
                seed_source="image_shopper_root",
                autocomplete_rank=1,
                journey_type="first_instinct_autocomplete",
                journey_root="ambient",
                journey_path=("ambient", "ambient lighting"),
            ),
            SearchKeywordCandidate(
                phrase="fallback light bars",
                rationale="Available second-best platform expansion",
                candidate_source="takealot_autocomplete",
                intended_strategy="core",
                seed="fallback",
                seed_source="title_cross_check",
                autocomplete_rank=2,
                journey_type="title_cross_check_autocomplete",
                journey_root="fallback",
                journey_path=("fallback", "fallback light bars"),
                adaptive_recovery_source="second_best_root_expansion",
            ),
        ],
        autocomplete_checks=autocomplete_checks,
        target_plid="12345678",
        profile=profile,
        max_pages=2,
        max_keywords=3,
        relevance_threshold=0.60,
        source_title="RGB LED Light Bars TV Backlight Ambient Lighting",
    )

    assert [item.relevance_status for item in observations] == [
        "rejected_irrelevant",
        "accepted",
    ]
    assert client.suggestion_calls == ["light bars"]
    assert client.search_calls == ["ambient lighting", "rgb light bars"]
    assert "fallback light bars" not in client.search_calls
    assert observations[1].validation_evidence["journey_type"] == (
        "result_page_root_expansion"
    )
    assert observations[1].validation_evidence["journey_parent_query"] == ("ambient lighting")
    assert steps[1]["path"][-2:] == ["light bars", "rgb light bars"]
    assert autocomplete_checks[0]["seed_source"] == "result_page_learning"
    assert adaptive["adaptive_recovery_used"] is True
    assert adaptive["adaptive_recovery_source"] == "result_page_learning"

    capped_client = ResultLearningClient()
    capped_checks = [
        {"seed": f"already-checked-{index}", "status": "observed"} for index in range(20)
    ]
    capped_observations, _, capped_adaptive = await _collect_shopper_journey(
        capped_client,  # type: ignore[arg-type]
        candidates=[
            SearchKeywordCandidate(
                phrase="ambient lighting",
                rationale="Broad first completion",
                candidate_source="takealot_autocomplete",
                intended_strategy="core",
                journey_type="first_instinct_autocomplete",
                journey_root="ambient",
                journey_path=("ambient", "ambient lighting"),
            )
        ],
        autocomplete_checks=capped_checks,
        target_plid="12345678",
        profile=profile,
        max_pages=2,
        max_keywords=3,
        relevance_threshold=0.60,
        source_title="RGB LED Light Bars TV Backlight Ambient Lighting",
    )
    assert len(capped_observations) == 1
    assert capped_client.suggestion_calls == []
    assert len(capped_checks) == 20
    assert capped_adaptive["adaptive_recovery_used"] is False


@pytest.mark.asyncio
async def test_blue_ocean_candidate_requires_low_competition_and_early_target() -> None:
    observation = await _collect_keyword_observation(
        OpportunityGateSearchClient([_opportunity_page(direct_competitors=2, target_rank=3)]),  # type: ignore[arg-type]
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
    assert (
        observation.validation_evidence["direct_competitor_count_excluding_target_first_page"] == 2
    )
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
            "missing_platform_root_expansion",
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
    assert expected_reason in observation.validation_evidence["opportunity_rejection_reasons"]


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
    assert validation["missing_baseline_keywords"] == ["wireless gaming mouse"]
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
    assert [item["keyword"] for item in validation["comparisons"]] == [primary_keyword]
    assert [item["keyword"] for item in validation["secondary_comparisons"]] == [secondary_keyword]


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
        OpportunityGateSearchClient([_opportunity_page(direct_competitors=2, target_rank=3)]),  # type: ignore[arg-type]
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


def test_result_page_learning_never_adopts_an_unsupported_competitor_brand() -> None:
    profile = _opportunity_profile()
    branded = _observation(
        "computer accessory",
        None,
        relevance_status="rejected_irrelevant",
        validation_evidence={"matched_result_titles": ["Acme Mouse", "Acme Mouse Pro"]},
    )
    supported = _observation(
        "computer accessory",
        None,
        relevance_status="rejected_irrelevant",
        validation_evidence={"matched_result_titles": ["Wireless Mouse", "Wireless Mouse Silent"]},
    )

    assert (
        _result_page_learning_seed(
            branded,
            profile=profile,
            source_title="Wireless Gaming Mouse",
        )
        is None
    )
    assert (
        _result_page_learning_seed(
            supported,
            profile=profile,
            source_title="Wireless Gaming Mouse",
        )
        == "wireless mouse"
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
                    title="300W Outdoor RGB LED Floodlight IP66 Waterproof",
                    image_url="http://media.takealot.com/covers_images/test/s.file",
                    status="buyable",
                    takealot_available_stock=1,
                    captured_at=datetime.now(UTC),
                )
            )
        engine.dispose()

        listing = client.get("/api/erp/search-ranking")
        detail_before_confirmation = client.get(
            "/api/erp/search-ranking/offer-web"
        )
        root_expansion_library = client.get(
            "/api/erp/search-ranking/root-expansion-library"
        )
        batch_preview = client.get("/api/erp/search-ranking/batch")
        batch_without_acknowledgements = client.post(
            "/api/erp/search-ranking/batch/start",
            headers={"X-CSRF-Token": issued["csrf_token"]},
            json={"snapshot_id": "a" * 64},
        )
        batch_without_provider = client.post(
            "/api/erp/search-ranking/batch/start",
            headers={"X-CSRF-Token": issued["csrf_token"]},
            json={
                "snapshot_id": batch_preview.json()["preview"]["snapshot_id"],
                "confirmed_paid_model_calls": True,
                "confirmed_public_takealot_requests": True,
                "confirmed_strict_serial_no_retry": True,
            },
        )
        run = client.post(
            "/api/erp/search-ranking/offer-web/analyze",
            headers={"X-CSRF-Token": issued["csrf_token"]},
        )
        reverse_without_acknowledgements = client.post(
            "/api/erp/search-ranking/offer-web/reverse-image-search",
            headers={"X-CSRF-Token": issued["csrf_token"]},
            json={
                "source_analysis_id": 1,
                "reason_code": "no_platform_validated_query",
                "confirmed": True,
            },
        )
        reverse_with_false_confirmation = client.post(
            "/api/erp/search-ranking/offer-web/reverse-image-search",
            headers={"X-CSRF-Token": issued["csrf_token"]},
            json={
                "source_analysis_id": 1,
                "reason_code": "no_platform_validated_query",
                "confirmed": False,
                "acknowledged_external_call": True,
                "acknowledged_estimated_cost": True,
            },
        )
        manual_fact_without_acknowledgements = client.post(
            "/api/erp/search-ranking/offer-web/product-facts/confirm",
            headers={"X-CSRF-Token": issued["csrf_token"]},
            json={
                "source_analysis_id": 1,
                "reason_code": "no_platform_validated_query",
                "facts": [
                    {
                        "fact_type": "product_type",
                        "fact_term": "compressed sofa",
                        "statement": "Supplier confirmed",
                    }
                ],
                "confirmed": True,
            },
        )
        decision_parameter_without_acknowledgements = client.post(
            "/api/erp/search-ranking/offer-web/decision-parameters/confirm",
            headers={"X-CSRF-Token": issued["csrf_token"]},
            json={
                "choices": [
                    {"parameter_key": "300w", "is_decision_parameter": True},
                    {"parameter_key": "ip66", "is_decision_parameter": False},
                ],
                "confirmed_current_title": True,
            },
        )
        decision_parameter_confirmation = client.post(
            "/api/erp/search-ranking/offer-web/decision-parameters/confirm",
            headers={"X-CSRF-Token": issued["csrf_token"]},
            json={
                "choices": [
                    {"parameter_key": "300w", "is_decision_parameter": True},
                    {"parameter_key": "ip66", "is_decision_parameter": False},
                ],
                "confirmed_current_title": True,
                "acknowledged_search_validation": True,
                "acknowledged_no_ranking_guarantee": True,
            },
        )

    assert listing.status_code == 200
    assert listing.json()["status"]["configured"] is False
    assert listing.json()["status"]["passive_reads_are_local_only"] is True
    assert listing.json()["status"]["product_fact_confirmation_mode"] == "manual_only"
    assert listing.json()["status"]["decision_parameter_confirmation_mode"] == (
        "manual_per_title"
    )
    assert listing.json()["status"]["autocomplete_cache_ttl_hours"] == 24
    assert root_expansion_library.status_code == 200
    assert root_expansion_library.json()["policy"]["scheduled_refresh"] is False
    assert root_expansion_library.json()["roots"] == []
    assert listing.json()["eligibility"]["source"] == ("authenticated_store_seller_offers")
    assert listing.json()["items"][0]["offer_id"] == "offer-web"
    assert detail_before_confirmation.status_code == 200
    assert [
        item["parameter_value"]
        for item in detail_before_confirmation.json()["decision_parameter_profile"][
            "candidates"
        ]
    ] == ["300W", "IP66"]
    assert batch_preview.status_code == 200
    assert batch_preview.json()["policy"] == {
        "scope": "all_accessible_active_connected_stores",
        "target_scope": "one_representative_offer_per_store_productline_id",
        "strict_serial": True,
        "max_concurrency": 1,
        "automatic_retry": False,
        "pause_after_provider_or_network_error": True,
        "reverse_image_search": False,
        "requires_snapshot_confirmation": True,
        "public_request_min_interval_seconds": 3.0,
        "public_request_max_interval_seconds": 5.0,
    }
    assert batch_preview.json()["preview"]["eligible_count"] == 1
    assert batch_without_acknowledgements.status_code == 422
    assert batch_without_provider.status_code == 409
    assert run.status_code == 503
    assert "DASHSCOPE_API_KEY" in run.json()["detail"]
    assert reverse_without_acknowledgements.status_code == 404
    assert reverse_with_false_confirmation.status_code == 404
    assert manual_fact_without_acknowledgements.status_code == 422
    assert decision_parameter_without_acknowledgements.status_code == 422
    assert decision_parameter_confirmation.status_code == 200
    assert decision_parameter_confirmation.json()["analysis"] is None
    assert decision_parameter_confirmation.json()["decision_parameter_profile"][
        "applied_decision_values"
    ] == ["300W"]


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
            arguments_profile = VisionProfile(
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
                    KeywordCandidate(phrase="wireless mouse", rationale="Exact shopper root"),
                ],
                opportunity_seeds=[
                    KeywordCandidate(phrase="mouse for laptop", rationale="Adjacent use case")
                ],
                exclusions=["keyboard combo"],
                confidence=0.9,
                title_suggestion="Rechargeable Wireless Mouse",
                title_reason="Lead with the exact product type.",
            )
            if json["tool_choice"]["function"]["name"] == (
                "submit_takealot_fused_search_profile"
            ):
                arguments_profile = _fusion_ready_profile(arguments_profile)
            arguments = arguments_profile.model_dump_json()
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

    monkeypatch.setattr(
        search_ranking_service, "_thumbnail_data_url", lambda *_: "data:image/jpeg;base64,AA=="
    )
    monkeypatch.setattr(search_ranking_service.httpx, "AsyncClient", FakeAsyncClient)

    result = await OpenAICompatibleProductVisionClient(runtime).identify(
        image_url="https://media.takealot.com/covers_images/test.jpg",
        reference_title="Rechargeable Wireless Mouse",
    )

    assert result.provider == "qwen"
    assert result.model == "qwen3.7-plus"
    assert result.estimated_cost_cny == 0.012
    assert [url for url, _, _ in requests] == [
        "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    ]
    assert requests[0][2]["thinking"] == {"type": "disabled"}
    assert requests[1][2]["enable_thinking"] is False
    assert requests[1][2]["tool_choice"]["function"]["name"] == (
        "submit_takealot_visual_observation"
    )
    assert requests[2][2]["tool_choice"]["function"]["name"] == (
        "submit_takealot_fused_search_profile"
    )
    assert "Rechargeable Wireless Mouse" not in str(requests[1][2])
    assert "Rechargeable Wireless Mouse" in str(requests[2][2])
    fusion_request = str(requests[2][2])
    assert "lazy sofa" in fusion_request
    assert "1-5 meaningful words" in fusion_request
    assert "exactly one meaningful complete word" not in fusion_request
    for _, _, request_payload in requests[1:]:
        serialized_request = str(request_payload)
        assert "South Africa" in serialized_request
        assert "South African English" in serialized_request
        required_fields = request_payload["tools"][0]["function"]["parameters"][
            "required"
        ]
        assert "market_context" in required_fields
        assert "language_variant" in required_fields
        assert "shopper_context" in required_fields
    assert result.profile.market_context == "South Africa"
    assert result.profile.language_variant == "South African English"
    assert result.profile.shopper_context == "South African local customer habits"
    assert result.provider_attempts[0] == {
        "provider": "doubao",
        "status": "request_or_schema_failed",
        "reason": "SearchRankingConfigurationError",
    }
    assert result.provider_attempts[1]["status"] == "accepted"
    assert result.provider_attempts[1]["stages"] == [
        "isolated_image_observation",
        "image_title_fusion",
    ]
    assert result.provider_attempts[1]["usage"]["total_tokens"] == 3000
    assert result.provider_attempts[1]["estimated_cost_cny"] == 0.012


@pytest.mark.asyncio
async def test_semantic_title_identity_avoids_unnecessary_provider_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "qwen-test-key")
    monkeypatch.setenv("ARK_API_KEY", "doubao-test-key")
    runtime = SearchRankingRuntimeSettings.from_env(tmp_path)
    client = OpenAICompatibleProductVisionClient(runtime)
    profile = _opportunity_profile().model_copy(
        update={
            "product_name": (
                "RGB LED Flood Light with Remote Colour Changing Adjustable Bracket Fixture Lamp"
            ),
            "product_type_terms": ["flood light"],
        }
    )
    requested_providers: list[str] = []

    async def fake_request(
        _: httpx.AsyncClient,
        *,
        provider: Any,
        image_data_url: str,
    ) -> VisionCallResult:
        del image_data_url
        requested_providers.append(provider.name)
        return VisionCallResult(
            profile=profile,
            provider=provider.name,
            model=provider.model,
            response_id=f"{provider.name}-semantic-identity",
            usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            },
            estimated_cost_cny=0.001,
        )

    monkeypatch.setattr(
        search_ranking_service,
        "_thumbnail_data_url",
        lambda *_: "data:image/jpeg;base64,AA==",
    )
    monkeypatch.setattr(client, "_request_provider", fake_request)

    result = await client.identify(
        image_url="https://media.takealot.com/covers_images/floodlight.jpg",
        reference_title=("300W Outdoor RGB LED Floodlight Smart App Control IP66 Waterproof"),
    )

    assert requested_providers == ["doubao"]
    assert result.provider == "doubao"
    assert result.profile.confidence == profile.confidence
    assert result.cache_profile is None
    assert result.provider_attempts[0]["source_title_similarity"] < 0.40
    assert result.provider_attempts[0]["title_identity_support"] is True
    assert result.provider_attempts[0]["title_identity_supported_terms"] == ["flood light"]


@pytest.mark.asyncio
async def test_large_identity_difference_does_not_trigger_provider_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "qwen-test-key")
    monkeypatch.setenv("ARK_API_KEY", "doubao-test-key")
    runtime = SearchRankingRuntimeSettings.from_env(tmp_path)
    mouse_profile = _opportunity_profile()
    chair_profile = mouse_profile.model_copy(update={"product_name": "Dining Chair"})
    lamp_profile = mouse_profile.model_copy(update={"product_name": "Floor Lamp"})
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

    assert accepted.provider == "doubao"
    assert accepted.usage == {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
    }
    assert accepted.estimated_cost_cny == 0.001
    assert [item["status"] for item in accepted.provider_attempts] == ["accepted"]
    assert accepted.provider_attempts[0]["identity_difference_level"] == "high"
    assert conflicted.usage == accepted.usage
    assert conflicted.estimated_cost_cny == 0.001
    assert conflicted.profile.confidence == chair_profile.confidence
    assert conflicted.cache_profile is None
    assert [item["status"] for item in conflicted.provider_attempts] == ["accepted"]


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
            del headers
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
                                                "arguments": (
                                                    _fusion_ready_profile().model_dump_json()
                                                    if json["tool_choice"]["function"]["name"]
                                                    == "submit_takealot_fused_search_profile"
                                                    else _opportunity_profile().model_dump_json()
                                                ),
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
        "input_tokens": 500,
        "output_tokens": 250,
        "total_tokens": 750,
    }
    assert result.estimated_cost_cny == pytest.approx(0.00264)
    assert result.provider_attempts[0]["status"] == "request_or_schema_failed"
    assert result.provider_attempts[0]["usage"]["total_tokens"] == 150
    assert result.provider_attempts[0]["estimated_cost_cny"] == pytest.approx(0.00024)


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
        assert detail["latest_attempt"]["estimated_cost_cny"] == pytest.approx(0.00084)
        assert FakeAsyncClient.posts == expected_posts


@pytest.mark.asyncio
async def test_reverse_search_path_is_absent_and_manual_fact_gap_remains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'reverse-confirmation.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "qwen-reverse-test-key")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setenv("TAKEALOT_SEARCH_PAGE_DELAY_SECONDS", "0")
    FakeVisionClient.calls = 0
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    with Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="offer-reverse-confirm",
                productline_id="12345678",
                sku="REVERSE-01",
                title="Silent Rechargeable Wireless Gaming Mouse",
                image_url="http://media.takealot.com/covers_images/test/s.file",
                status="buyable",
                takealot_available_stock=2,
                captured_at=datetime.now(UTC),
            )
        )
    engine.dispose()

    class RejectAllSearchClient(FakeSearchClient):
        async def fetch_search_first_page(
            self,
            keyword: str,
        ) -> tuple[str, dict[str, Any]]:
            return _search_url(keyword), _payload(
                [(str(81_000_000 + index), f"Winter Jacket Style {index}") for index in range(36)],
                after="",
                total=36,
            )

    service = SearchRankingService(
        tmp_path,
        vision_client_factory=FakeVisionClient,
        search_client_factory=RejectAllSearchClient,  # type: ignore[arg-type]
    )
    ordinary = await service.analyze_offer("offer-reverse-confirm")
    recommendation = ordinary["analysis"]["product_fact_recommendation"]

    assert recommendation["recommended"] is True
    assert recommendation["reason_code"] == "no_platform_validated_query"
    assert recommendation["requires_human_confirmation"] is True
    assert recommendation["external_lookup_available"] is False
    assert "reverse_image_search" not in ordinary["analysis"]
