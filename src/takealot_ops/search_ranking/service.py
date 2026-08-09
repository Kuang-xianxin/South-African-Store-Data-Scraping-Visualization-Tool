"""Image-led keyword discovery with live Takealot relevance and rank validation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from takealot_ops.competitors.api import CompetitorPublicClient
from takealot_ops.erp.product_images import (
    ProductImageInputError,
    ProductImageUnavailableError,
    ProductThumbnailCache,
    trusted_product_image_url,
)
from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.migrations import (
    create_engine_for_database_url,
    create_read_only_engine,
)
from takealot_ops.storage.models import (
    OfferCurrent,
    SearchRankingAnalysis,
    SearchRankingKeywordResult,
)


PROMPT_VERSION = "takealot-v4-image-narrow"
ORGANIC_PAGE_SIZE = 36
DESKTOP_COLUMNS = 4
TITLE_MAX_LENGTH = 160
ORGANIC_RESULT_TYPE = "product_views"
AUTOCOMPLETE_RESULT_LIMIT = 5
AUTOCOMPLETE_SEED_LIMIT = 6
CORE_MAJORITY_FLOOR = 0.51
OPPORTUNITY_MAX_DIRECT_COMPETITORS = 2
OPPORTUNITY_MAX_ORGANIC_RANK = 72
IDENTITY_TITLE_SIMILARITY_FLOOR = 0.40
HIGH_RISK_CLAIM_TOKENS = {
    "app",
    "battery",
    "bluetooth",
    "compatible",
    "control",
    "dimmable",
    "foldable",
    "heated",
    "memory",
    "portable",
    "power",
    "rechargeable",
    "remote",
    "smart",
    "solar",
    "touch",
    "usb",
    "waterproof",
    "wifi",
    "wireless",
}
FACT_ATTRIBUTE_CLAIM_TOKENS = {
    # Materials and colours.
    "aluminium",
    "aluminum",
    "bamboo",
    "beige",
    "black",
    "blue",
    "bronze",
    "brown",
    "ceramic",
    "chrome",
    "clear",
    "copper",
    "cotton",
    "denim",
    "fabric",
    "foam",
    "glass",
    "gold",
    "gray",
    "green",
    "grey",
    "leather",
    "linen",
    "marble",
    "metal",
    "mesh",
    "nylon",
    "oak",
    "orange",
    "pink",
    "plastic",
    "polyester",
    "purple",
    "red",
    "rubber",
    "silver",
    "silicone",
    "steel",
    "suede",
    "transparent",
    "velvet",
    "walnut",
    "white",
    "wood",
    "wooden",
    "wool",
    "yellow",
    # Audience and capacity claims.
    "adult",
    "baby",
    "boy",
    "child",
    "children",
    "girl",
    "infant",
    "kid",
    "kids",
    "king",
    "large",
    "male",
    "medium",
    "men",
    "mini",
    "queen",
    "seater",
    "senior",
    "single",
    "small",
    "teen",
    "teenager",
    "toddler",
    "twin",
    "unisex",
    "women",
    # Compatibility/brand and efficacy claims.
    "android",
    "apple",
    "ergonomic",
    "healing",
    "iphone",
    "macbook",
    "medical",
    "orthopedic",
    "pain",
    "playstation",
    "ps4",
    "ps5",
    "relief",
    "samsung",
    "therapeutic",
    "xbox",
}
MEASUREMENT_CLAIM_TOKENS = {
    "cm",
    "ft",
    "g",
    "gb",
    "inch",
    "inches",
    "kg",
    "l",
    "litre",
    "litres",
    "m",
    "mah",
    "ml",
    "mm",
    "ounce",
    "ounces",
    "tb",
    "v",
    "volt",
    "w",
    "watt",
}
OPPORTUNITY_COMPATIBILITY_CONTEXT_TOKENS = {
    "ambient",
    "bedroom",
    "bedside",
    "camping",
    "car",
    "computer",
    "desk",
    "dorm",
    "gaming",
    "guest",
    "home",
    "indoor",
    "kitchen",
    "laptop",
    "lazy",
    "living",
    "lounge",
    "mood",
    "office",
    "outdoor",
    "party",
    "reading",
    "relaxation",
    "room",
    "school",
    "study",
    "travel",
    "tv",
    "work",
}
TITLE_CONNECTOR_TOKENS = {"a", "an", "and", "for", "of", "the", "to", "with"}
TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
COMBINED_MEASUREMENT_PATTERN = re.compile(
    r"^\d+(?:\.\d+)?(?:cm|ft|g|gb|inch|inches|kg|l|litre|litres|m|mah|ml|mm|tb|v|volt|w|watt)$"
)
API_VERSION_PATTERN = re.compile(r"/rest/(v-[^/]+)/")
QWEN_INPUT_PRICE_CNY_PER_MILLION = 2.0
QWEN_OUTPUT_PRICE_CNY_PER_MILLION = 8.0
DOUBAO_INPUT_PRICE_CNY_PER_MILLION = 0.6
DOUBAO_OUTPUT_PRICE_CNY_PER_MILLION = 3.6
PRICING_SNAPSHOT_DATE = "2026-08-07"
ELIGIBILITY_REASON_LABELS = {
    "not_buyable": "状态不是 buyable",
    "no_available_stock": "没有明确正数可售库存",
    "stale_snapshot": "Seller Offers 快照已过期",
    "missing_title": "缺少主标题",
    "invalid_plid": "缺少有效 PLID",
    "untrusted_image": "缺少可信 Takealot 官方主图",
}


class SearchRankingInputError(ValueError):
    """The selected store offer cannot be safely analyzed."""


class SearchRankingConfigurationError(RuntimeError):
    """The ranking module is not configured for a model request."""


class SearchRankingProviderError(RuntimeError):
    """The multimodal provider failed without exposing credentials or raw bodies."""


class _CountedVisionProviderError(SearchRankingProviderError):
    """A model response consumed known tokens but its profile was unusable."""

    def __init__(
        self,
        message: str,
        *,
        usage: dict[str, int],
        estimated_cost_cny: float,
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.estimated_cost_cny = estimated_cost_cny


class _VisionAttemptsExhaustedError(SearchRankingProviderError):
    """Every usable profile failed, while some provider usage was measurable."""

    def __init__(
        self,
        message: str,
        *,
        usage: dict[str, int],
        estimated_cost_cny: float,
        provider_attempts: tuple[dict[str, Any], ...],
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.estimated_cost_cny = estimated_cost_cny
        self.provider_attempts = provider_attempts


class KeywordCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phrase: str = Field(min_length=2, max_length=100)
    rationale: str = Field(min_length=2, max_length=300)


class VisionProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_name: str = Field(min_length=2, max_length=160)
    category: str = Field(min_length=2, max_length=160)
    product_type_terms: list[str] = Field(min_length=1, max_length=5)
    distinctive_terms: list[str] = Field(min_length=0, max_length=8)
    keywords: list[KeywordCandidate] = Field(min_length=2, max_length=5)
    autocomplete_seeds: list[KeywordCandidate] = Field(min_length=2, max_length=5)
    opportunity_seeds: list[KeywordCandidate] = Field(min_length=1, max_length=3)
    exclusions: list[str] = Field(min_length=0, max_length=8)
    confidence: float = Field(ge=0, le=1)
    title_suggestion: str = Field(min_length=2, max_length=160)
    title_reason: str = Field(min_length=2, max_length=500)


@dataclass(frozen=True)
class VisionCallResult:
    profile: VisionProfile
    provider: str
    model: str
    response_id: str | None
    usage: dict[str, int]
    estimated_cost_cny: float
    provider_attempts: tuple[dict[str, Any], ...] = ()
    cache_profile: VisionProfile | None = None


@dataclass(frozen=True)
class VisionProviderSettings:
    name: str
    display_name: str
    api_key: str
    base_url: str
    model: str
    input_price_cny_per_million: float
    output_price_cny_per_million: float

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


@dataclass(frozen=True)
class SearchRankingRuntimeSettings:
    project_root: Path
    providers: tuple[VisionProviderSettings, ...]
    max_pages: int
    max_keywords: int
    confidence_threshold: float
    relevance_threshold: float
    request_timeout_seconds: float
    page_delay_seconds: float
    offer_max_age_hours: float
    image_max_dimension: int

    @property
    def configured_providers(self) -> tuple[VisionProviderSettings, ...]:
        configured = [provider for provider in self.providers if provider.api_key]
        priority = {"doubao": 0, "qwen": 1}
        return tuple(sorted(configured, key=lambda item: priority.get(item.name, 99)))

    @property
    def primary_provider(self) -> VisionProviderSettings:
        configured = self.configured_providers
        return configured[0] if configured else self.providers[0]

    @property
    def fallback_provider(self) -> VisionProviderSettings | None:
        configured = self.configured_providers
        if len(configured) >= 2:
            return configured[1]
        return None

    @property
    def provider_signature(self) -> str:
        configured = self.configured_providers
        if configured:
            return "|".join(
                f"{provider.name}:{provider.model}" for provider in configured
            )
        models = "|".join(
            f"{provider.name}:{provider.model}" for provider in self.providers
        )
        return f"unconfigured|{models}"

    @classmethod
    def from_env(cls, project_root: Path) -> SearchRankingRuntimeSettings:
        load_dotenv(project_root / ".env", override=False)
        qwen = VisionProviderSettings(
            name="qwen",
            display_name="阿里云百炼千问",
            api_key=os.environ.get("DASHSCOPE_API_KEY", "").strip(),
            base_url=_https_base_url(
                "TAKEALOT_SEARCH_QWEN_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            model=os.environ.get(
                "TAKEALOT_SEARCH_QWEN_MODEL", "qwen3.7-plus"
            ).strip(),
            input_price_cny_per_million=_bounded_float(
                "TAKEALOT_SEARCH_QWEN_INPUT_PRICE_CNY_PER_MILLION",
                QWEN_INPUT_PRICE_CNY_PER_MILLION,
                0.0,
                100.0,
            ),
            output_price_cny_per_million=_bounded_float(
                "TAKEALOT_SEARCH_QWEN_OUTPUT_PRICE_CNY_PER_MILLION",
                QWEN_OUTPUT_PRICE_CNY_PER_MILLION,
                0.0,
                100.0,
            ),
        )
        doubao = VisionProviderSettings(
            name="doubao",
            display_name="火山方舟豆包",
            api_key=os.environ.get("ARK_API_KEY", "").strip(),
            base_url=_https_base_url(
                "TAKEALOT_SEARCH_DOUBAO_BASE_URL",
                "https://ark.cn-beijing.volces.com/api/v3",
            ),
            model=os.environ.get(
                "TAKEALOT_SEARCH_DOUBAO_MODEL",
                "doubao-seed-2-0-lite-260215",
            ).strip(),
            input_price_cny_per_million=_bounded_float(
                "TAKEALOT_SEARCH_DOUBAO_INPUT_PRICE_CNY_PER_MILLION",
                DOUBAO_INPUT_PRICE_CNY_PER_MILLION,
                0.0,
                100.0,
            ),
            output_price_cny_per_million=_bounded_float(
                "TAKEALOT_SEARCH_DOUBAO_OUTPUT_PRICE_CNY_PER_MILLION",
                DOUBAO_OUTPUT_PRICE_CNY_PER_MILLION,
                0.0,
                100.0,
            ),
        )
        if not qwen.model or not doubao.model:
            raise SearchRankingConfigurationError("搜索定位模型名称不能为空")
        return cls(
            project_root=project_root.resolve(),
            providers=(qwen, doubao),
            max_pages=_bounded_int("TAKEALOT_SEARCH_MAX_PAGES", 5, 1, 10),
            max_keywords=_bounded_int("TAKEALOT_SEARCH_MAX_KEYWORDS", 4, 2, 5),
            confidence_threshold=_bounded_float(
                "TAKEALOT_SEARCH_CONFIDENCE_THRESHOLD", 0.68, 0.5, 0.95
            ),
            relevance_threshold=_bounded_float(
                "TAKEALOT_SEARCH_RELEVANCE_THRESHOLD", 0.60, 0.4, 0.95
            ),
            request_timeout_seconds=_bounded_float(
                "TAKEALOT_SEARCH_MODEL_TIMEOUT_SECONDS", 60.0, 10.0, 180.0
            ),
            page_delay_seconds=_bounded_float(
                "TAKEALOT_SEARCH_PAGE_DELAY_SECONDS", 1.5, 0.0, 10.0
            ),
            offer_max_age_hours=_bounded_float(
                "TAKEALOT_SEARCH_OFFER_MAX_AGE_HOURS", 36.0, 1.0, 168.0
            ),
            image_max_dimension=_bounded_choice_int(
                "TAKEALOT_SEARCH_IMAGE_MAX_DIMENSION", 640, {192, 384, 640}
            ),
        )


class VisionClient(Protocol):
    async def identify(
        self,
        *,
        image_url: str,
        reference_title: str,
    ) -> VisionCallResult: ...


class OpenAICompatibleProductVisionClient:
    """Cross-vendor multimodal chat client with forced schema tools and fallback."""

    def __init__(self, settings: SearchRankingRuntimeSettings) -> None:
        self.settings = settings

    async def identify(
        self,
        *,
        image_url: str,
        reference_title: str,
    ) -> VisionCallResult:
        providers = self.settings.configured_providers
        if not providers:
            raise SearchRankingConfigurationError(
                "未配置 DASHSCOPE_API_KEY 或 ARK_API_KEY；搜索定位不会调用模型"
            )
        image_data_url = await asyncio.to_thread(
            _thumbnail_data_url,
            self.settings,
            image_url,
        )
        last_error: Exception | None = None
        conflicting_results: list[tuple[float, VisionCallResult]] = []
        provider_attempts: list[dict[str, Any]] = []
        aggregate_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
        aggregate_cost_cny = 0.0

        def record_usage(
            usage: Mapping[str, Any],
            estimated_cost_cny: float,
        ) -> None:
            nonlocal aggregate_cost_cny
            for key in aggregate_usage:
                aggregate_usage[key] += _optional_int(usage.get(key)) or 0
            aggregate_cost_cny += estimated_cost_cny

        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=False,
        ) as client:
            for provider_index, provider in enumerate(providers):
                try:
                    result = await self._request_provider(
                        client,
                        provider=provider,
                        image_data_url=image_data_url,
                    )
                except (SearchRankingConfigurationError, SearchRankingProviderError) as exc:
                    last_error = exc
                    attempt_evidence: dict[str, Any] = {
                        "provider": provider.name,
                        "status": "request_or_schema_failed",
                        "reason": type(exc).__name__,
                    }
                    if isinstance(exc, _CountedVisionProviderError):
                        record_usage(exc.usage, exc.estimated_cost_cny)
                        attempt_evidence["usage"] = dict(exc.usage)
                        attempt_evidence["estimated_cost_cny"] = (
                            exc.estimated_cost_cny
                        )
                    provider_attempts.append(attempt_evidence)
                    if provider_index == len(providers) - 1:
                        break
                    continue
                record_usage(result.usage, result.estimated_cost_cny)
                _, cross_check = _cross_check_image_profile(
                    result.profile,
                    reference_title,
                )
                similarity = float(cross_check["source_title_similarity"])
                if similarity >= IDENTITY_TITLE_SIMILARITY_FLOOR:
                    provider_attempts.append(
                        {
                            "provider": provider.name,
                            "status": "accepted",
                            "source_title_similarity": similarity,
                            "usage": dict(result.usage),
                            "estimated_cost_cny": result.estimated_cost_cny,
                        }
                    )
                    return replace(
                        result,
                        usage=dict(aggregate_usage),
                        estimated_cost_cny=round(aggregate_cost_cny, 10),
                        provider_attempts=tuple(provider_attempts),
                    )
                provider_attempts.append(
                    {
                        "provider": provider.name,
                        "status": "identity_conflict",
                        "source_title_similarity": similarity,
                        "usage": dict(result.usage),
                        "estimated_cost_cny": result.estimated_cost_cny,
                    }
                )
                conflicting_results.append((similarity, result))
        if conflicting_results:
            _, best = max(conflicting_results, key=lambda item: item[0])
            low_confidence_profile = best.profile.model_copy(
                update={"confidence": min(best.profile.confidence, 0.49)}
            )
            return replace(
                best,
                profile=low_confidence_profile,
                usage=dict(aggregate_usage),
                estimated_cost_cny=round(aggregate_cost_cny, 10),
                provider_attempts=tuple(provider_attempts),
                cache_profile=best.profile,
            )
        if any("usage" in item for item in provider_attempts):
            raise _VisionAttemptsExhaustedError(
                "千问与豆包均未返回可用商品识别结果",
                usage=dict(aggregate_usage),
                estimated_cost_cny=round(aggregate_cost_cny, 10),
                provider_attempts=tuple(provider_attempts),
            ) from last_error
        raise SearchRankingProviderError("千问与豆包多模态服务暂时均不可用") from last_error

    async def _request_provider(
        self,
        client: httpx.AsyncClient,
        *,
        provider: VisionProviderSettings,
        image_data_url: str,
    ) -> VisionCallResult:
        payload = {
            "model": provider.model,
            "messages": [
                {
                    "role": "system",
                    "content": _SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Identify the physical product shown in the image. "
                                "No seller title or SKU is supplied at this stage; "
                                "base the identity and shopper wording only on visible evidence."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": image_data_url},
                        },
                    ],
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "submit_takealot_product_profile",
                        "description": "Return the validated product identity and Takealot keyword candidates.",
                        "parameters": VisionProfile.model_json_schema(),
                    },
                }
            ],
            "tool_choice": {
                "type": "function",
                "function": {"name": "submit_takealot_product_profile"},
            },
            "max_tokens": 1200,
        }
        if provider.name == "qwen":
            payload["enable_thinking"] = False
        elif provider.name == "doubao":
            payload["thinking"] = {"type": "disabled"}
        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = await client.post(
                    provider.chat_completions_url,
                    headers=headers,
                    json=payload,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt < 1:
                    await asyncio.sleep(1.5 * (2**attempt))
                    continue
                raise SearchRankingProviderError("多模态模型网络请求连续失败") from exc
            if response.status_code in {401, 403}:
                raise SearchRankingConfigurationError(
                    f"{provider.display_name}密钥无效或无权使用所选模型"
                )
            if response.status_code == 400:
                raise SearchRankingConfigurationError(
                    f"{provider.display_name}模型 {provider.model} 或结构化图片输入配置不受当前账号支持"
                )
            if response.status_code == 429 or response.status_code >= 500:
                last_error = SearchRankingProviderError(
                    f"多模态模型临时返回 HTTP {response.status_code}"
                )
                if attempt < 1:
                    await asyncio.sleep(1.5 * (2**attempt))
                    continue
                raise last_error
            if response.status_code != 200:
                raise SearchRankingProviderError(
                    f"多模态模型返回 HTTP {response.status_code}"
                )
            try:
                body = response.json()
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise SearchRankingProviderError(
                    "多模态模型没有返回合格的结构化商品识别结果"
                ) from exc
            normalized_usage = _normalized_vision_usage(body)
            estimated_cost_cny = _estimated_cost_cny(
                provider,
                normalized_usage,
            )
            try:
                profile = _validated_chat_profile(body)
            except (ValueError, TypeError, ValidationError) as exc:
                raise _CountedVisionProviderError(
                    "多模态模型没有返回合格的结构化商品识别结果",
                    usage=normalized_usage,
                    estimated_cost_cny=estimated_cost_cny,
                ) from exc
            return VisionCallResult(
                profile=profile,
                provider=provider.name,
                model=provider.model,
                response_id=str(body.get("id")) if body.get("id") else None,
                usage=normalized_usage,
                estimated_cost_cny=estimated_cost_cny,
            )
        raise SearchRankingProviderError("多模态模型暂时不可用") from last_error


@dataclass(frozen=True)
class OfferEligibility:
    eligible: bool
    reasons: tuple[str, ...]
    trusted_image_url: str | None
    available_stock: int
    captured_at: datetime
    age_hours: float


@dataclass(frozen=True)
class KeywordObservation:
    keyword: str
    candidate_order: int
    relevance_status: str
    relevance_score: float
    validation_evidence: dict[str, Any]
    total_num_found: int | None
    pages_scanned: int
    found: bool
    page_number: int | None
    page_rank: int | None
    organic_rank: int | None
    row_number: int | None
    column_number: int | None
    target_url: str | None
    observed_at: datetime


@dataclass(frozen=True)
class SearchKeywordCandidate:
    phrase: str
    rationale: str
    candidate_source: str
    intended_strategy: str
    seed: str | None = None
    seed_source: str | None = None
    autocomplete_rank: int | None = None
    candidate_provenance: tuple[dict[str, Any], ...] = ()
    comparison_baseline_rank: int | None = None
    comparison_role: str | None = None
    comparison_strategy: str | None = None


class SearchRankingService:
    """Persisted store-scoped analysis; ordinary GET requests remain local-only."""

    def __init__(
        self,
        project_root: Path,
        *,
        vision_client_factory: Callable[[SearchRankingRuntimeSettings], VisionClient]
        | None = None,
        search_client_factory: Callable[[], CompetitorPublicClient] | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.runtime = SearchRankingRuntimeSettings.from_env(self.project_root)
        self.database_url = DashboardSettings.from_env(self.project_root).database_url
        self._vision_client_factory = (
            vision_client_factory or OpenAICompatibleProductVisionClient
        )
        self._search_client_factory = search_client_factory or (
            lambda: CompetitorPublicClient(timeout_seconds=45.0)
        )

    def status_payload(self) -> dict[str, Any]:
        primary = self.runtime.primary_provider
        fallback = self.runtime.fallback_provider
        return {
            "configured": bool(self.runtime.configured_providers),
            "provider": primary.name,
            "provider_label": primary.display_name,
            "primary_model": primary.model,
            "fallback_provider": fallback.name if fallback else None,
            "fallback_provider_label": fallback.display_name if fallback else None,
            "fallback_model": fallback.model if fallback else None,
            "configured_provider_count": len(self.runtime.configured_providers),
            "pricing_snapshot_date": PRICING_SNAPSHOT_DATE,
            "max_pages": self.runtime.max_pages,
            "max_keywords": self.runtime.max_keywords,
            "offer_max_age_hours": self.runtime.offer_max_age_hours,
            "image_max_dimension": self.runtime.image_max_dimension,
            "organic_page_size": ORGANIC_PAGE_SIZE,
            "columns_per_row": DESKTOP_COLUMNS,
            "core_first_page_threshold": max(
                self.runtime.relevance_threshold,
                CORE_MAJORITY_FLOOR,
            ),
            "opportunity_max_direct_competitors": (
                OPPORTUNITY_MAX_DIRECT_COMPETITORS
            ),
            "opportunity_max_organic_rank": OPPORTUNITY_MAX_ORGANIC_RANK,
            "position_scope": "organic_results_excluding_sponsored",
            "ranking_source": "sections.products.results:type=product_views",
            "passive_reads_are_local_only": True,
        }

    def list_payload(self) -> dict[str, Any]:
        engine = create_read_only_engine(self.database_url)
        try:
            with Session(engine) as session:
                now = _utcnow()
                offers = list(
                    session.scalars(
                        select(OfferCurrent).order_by(OfferCurrent.title, OfferCurrent.offer_id)
                    )
                )
                analyses = list(
                    session.scalars(
                        select(SearchRankingAnalysis).order_by(SearchRankingAnalysis.id.desc())
                    )
                )
                latest_completed: dict[str, SearchRankingAnalysis] = {}
                for analysis in analyses:
                    if analysis.status == "completed":
                        latest_completed.setdefault(analysis.offer_id, analysis)
                evaluated = [
                    (offer, _offer_eligibility(offer, self.runtime, now=now))
                    for offer in offers
                ]
                items = [
                    _offer_summary(
                        offer,
                        latest_completed.get(offer.offer_id),
                        eligibility,
                    )
                    for offer, eligibility in evaluated
                    if eligibility.eligible
                ]
                excluded_reasons: dict[str, int] = {}
                for _, eligibility in evaluated:
                    if eligibility.eligible:
                        continue
                    for reason in eligibility.reasons:
                        excluded_reasons[reason] = excluded_reasons.get(reason, 0) + 1
                latest_capture = max(
                    (eligibility.captured_at for _, eligibility in evaluated),
                    default=None,
                )
        finally:
            engine.dispose()
        return {
            "status": self.status_payload(),
            "eligibility": {
                "source": "authenticated_store_seller_offers",
                "rule": "current_offer_and_buyable_and_positive_available_stock_and_fresh",
                "current_offer_count": len(offers),
                "eligible_count": len(items),
                "excluded_count": len(offers) - len(items),
                "excluded_reasons": excluded_reasons,
                "latest_capture_at": latest_capture.isoformat() if latest_capture else None,
                "max_age_hours": self.runtime.offer_max_age_hours,
            },
            "items": items,
        }

    def detail_payload(self, offer_id: str) -> dict[str, Any] | None:
        engine = create_read_only_engine(self.database_url)
        try:
            with Session(engine) as session:
                offer = session.scalar(select(OfferCurrent).where(OfferCurrent.offer_id == offer_id))
                if offer is None:
                    return None
                eligibility = _offer_eligibility(offer, self.runtime)
                if not eligibility.eligible:
                    return None
                analyses = list(
                    session.scalars(
                        select(SearchRankingAnalysis)
                        .where(SearchRankingAnalysis.offer_id == offer_id)
                        .order_by(SearchRankingAnalysis.id.desc())
                        .limit(12)
                    )
                )
                latest_attempt = analyses[0] if analyses else None
                latest = session.scalar(
                    select(SearchRankingAnalysis)
                    .where(
                        SearchRankingAnalysis.offer_id == offer_id,
                        SearchRankingAnalysis.status == "completed",
                    )
                    .order_by(SearchRankingAnalysis.id.desc())
                    .limit(1)
                )
                results = (
                    list(
                        session.scalars(
                            select(SearchRankingKeywordResult)
                            .where(SearchRankingKeywordResult.analysis_id == latest.id)
                            .order_by(SearchRankingKeywordResult.candidate_order)
                        )
                    )
                    if latest is not None
                    else []
                )
                payload = {
                    "status": self.status_payload(),
                    "product": _offer_summary(offer, latest, eligibility),
                    "analysis": _analysis_payload(latest, results) if latest else None,
                    "latest_attempt": (
                        _analysis_history_item(latest_attempt)
                        if latest_attempt is not None
                        and (latest is None or latest_attempt.id != latest.id)
                        else None
                    ),
                    "history": [_analysis_history_item(item) for item in analyses],
                }
        finally:
            engine.dispose()
        return payload

    async def analyze_offer(self, offer_id: str) -> dict[str, Any]:
        engine = create_engine_for_database_url(self.database_url)
        analysis_id: int | None = None
        try:
            with Session(engine) as session, session.begin():
                offer = session.scalar(select(OfferCurrent).where(OfferCurrent.offer_id == offer_id))
                if offer is None:
                    raise SearchRankingInputError("没有找到对应的店铺商品")
                eligibility = _offer_eligibility(offer, self.runtime)
                _raise_if_ineligible(eligibility)
                title = " ".join(str(offer.title or "").split())
                image_url = str(eligibility.trusted_image_url or "")
                plid = str(offer.productline_id or "").strip()
                previous = _previous_analysis_snapshot(
                    session,
                    offer_id,
                    current_title=title,
                )
                cache_key = _analysis_cache_key(
                    image_url=image_url,
                    provider_signature=self.runtime.provider_signature,
                )
                cached_candidates = list(
                    session.scalars(
                        select(SearchRankingAnalysis)
                        .where(
                            SearchRankingAnalysis.cache_key == cache_key,
                            SearchRankingAnalysis.vision_payload.is_not(None),
                        )
                        .order_by(SearchRankingAnalysis.id.desc())
                        .limit(12)
                    )
                )
                cached = next(
                    (
                        item
                        for item in cached_candidates
                        if item.status == "completed"
                        or (
                            isinstance(item.vision_payload, Mapping)
                            and item.vision_payload.get("vision_stage_completed")
                            is True
                        )
                    ),
                    None,
                )
                now = _utcnow()
                primary = self.runtime.primary_provider
                analysis = SearchRankingAnalysis(
                    offer_id=offer.offer_id,
                    productline_id=plid,
                    sku=offer.sku,
                    source_title=title,
                    source_image_url=image_url,
                    cache_key=cache_key,
                    provider=primary.name,
                    model=primary.model,
                    prompt_version=PROMPT_VERSION,
                    status="running",
                    vision_reused=cached is not None,
                    created_at=now,
                )
                session.add(analysis)
                session.flush()
                analysis_id = analysis.id
                cached_payload = dict(cached.vision_payload or {}) if cached else None
                cached_model = cached.model if cached else None
                cached_provider = cached.provider if cached else None

            if cached_payload is not None:
                model_profile = VisionProfile.model_validate(
                    cached_payload.get(
                        "model_profile",
                        cached_payload.get("profile", cached_payload),
                    )
                )
                live_identity_confidence_cap: float | None = None
                source_usage = cached_payload.get(
                    "source_usage",
                    cached_payload.get("usage", {}),
                )
                vision_payload = {
                    **cached_payload,
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                    },
                    "source_usage": source_usage,
                    "estimated_cost_cny": 0.0,
                    "pricing_snapshot_date": PRICING_SNAPSHOT_DATE,
                }
                used_provider = cached_provider or primary.name
                used_model = cached_model or primary.model
            else:
                call = await self._vision_client_factory(self.runtime).identify(
                    image_url=image_url,
                    reference_title=title,
                )
                model_profile = call.cache_profile or call.profile
                live_identity_confidence_cap = (
                    call.profile.confidence
                    if call.cache_profile is not None
                    else None
                )
                used_provider = call.provider
                used_model = call.model
                vision_payload = {
                    "model_profile": model_profile.model_dump(mode="json"),
                    "usage": call.usage,
                    "response_id": call.response_id,
                    "estimated_cost_cny": call.estimated_cost_cny,
                    "provider_attempts": list(call.provider_attempts),
                    "pricing_snapshot_date": PRICING_SNAPSHOT_DATE,
                }

            vision_payload["vision_stage_completed"] = True
            with Session(engine) as session, session.begin():
                staged_analysis = session.get(SearchRankingAnalysis, analysis_id)
                if staged_analysis is None:
                    raise RuntimeError("搜索定位分析记录意外丢失")
                staged_analysis.provider = used_provider
                staged_analysis.model = used_model
                staged_analysis.product_name = model_profile.product_name
                staged_analysis.category = model_profile.category
                staged_analysis.confidence = Decimal(
                    str(model_profile.confidence)
                )
                staged_analysis.vision_payload = dict(vision_payload)

            profile, recognition = _cross_check_image_profile(model_profile, title)
            if live_identity_confidence_cap is not None:
                profile = profile.model_copy(
                    update={
                        "confidence": min(
                            profile.confidence,
                            live_identity_confidence_cap,
                        )
                    }
                )
                recognition["live_identity_conflict"] = True
                recognition["live_identity_action"] = (
                    "public_search_skipped_but_raw_profile_cached"
                )
            cached_identity_conflict = bool(
                cached_payload is not None
                and float(recognition["source_title_similarity"])
                < IDENTITY_TITLE_SIMILARITY_FLOOR
            )
            if cached_identity_conflict:
                profile = profile.model_copy(
                    update={"confidence": min(profile.confidence, 0.49)}
                )
                recognition["cached_identity_conflict"] = True
                recognition["cached_identity_action"] = (
                    "public_search_skipped_require_identity_review"
                )
                recognition["identity_similarity_floor"] = (
                    IDENTITY_TITLE_SIMILARITY_FLOOR
                )
                provider_attempts = [
                    dict(item)
                    for item in vision_payload.get("provider_attempts", [])
                    if isinstance(item, Mapping)
                    and item.get("status") != "cached_identity_conflict"
                ]
                provider_attempts.append(
                    {
                        "provider": used_provider,
                        "status": "cached_identity_conflict",
                        "source_title_similarity": recognition[
                            "source_title_similarity"
                        ],
                    }
                )
                vision_payload["provider_attempts"] = provider_attempts
            observations: list[KeywordObservation] = []
            autocomplete_checks: list[dict[str, Any]] = []
            if profile.confidence < self.runtime.confidence_threshold:
                candidates = _precise_candidates(
                    profile,
                    source_title=title,
                )[: self.runtime.max_keywords]
                for order, candidate in enumerate(candidates, start=1):
                    observations.append(
                        _low_confidence_observation(
                            candidate.phrase,
                            order,
                            profile.confidence,
                            self.runtime.confidence_threshold,
                        )
                    )
            else:
                self._assert_offer_still_eligible(
                    offer_id=offer_id,
                    expected_plid=plid,
                    expected_title=title,
                    expected_image_url=image_url,
                )
                async with self._search_client_factory() as search_client:
                    candidates, autocomplete_checks = await _discover_keyword_candidates(
                        search_client,
                        profile=profile,
                        source_title=title,
                        title_reference_terms=recognition["title_reference_terms"],
                        max_keywords=self.runtime.max_keywords,
                    )
                    candidates = _inject_comparison_resample_candidates(
                        candidates,
                        previous=previous,
                        current_title=title,
                        max_keywords=self.runtime.max_keywords,
                    )
                    for order, candidate in enumerate(candidates, start=1):
                        observations.append(
                            await _collect_keyword_observation(
                                search_client,
                                candidate=candidate,
                                candidate_order=order,
                                target_plid=plid,
                                profile=profile,
                                max_pages=self.runtime.max_pages,
                                relevance_threshold=self.runtime.relevance_threshold,
                                page_delay_seconds=self.runtime.page_delay_seconds,
                                source_title=title,
                            )
                        )
                        if order < len(candidates) and self.runtime.page_delay_seconds:
                            await asyncio.sleep(self.runtime.page_delay_seconds)

            with Session(engine) as session, session.begin():
                persisted_analysis = session.get(SearchRankingAnalysis, analysis_id)
                if persisted_analysis is None:
                    raise RuntimeError("搜索定位分析记录意外丢失")
                persisted_analysis.provider = used_provider
                persisted_analysis.model = used_model
                persisted_analysis.product_name = profile.product_name
                persisted_analysis.category = profile.category
                persisted_analysis.confidence = Decimal(str(profile.confidence))
                (
                    accepted_title_keywords,
                    hot_term_title_keywords,
                    opportunity_title_keywords,
                ) = _title_strategy_keywords(observations, title)
                title_suggestion = _build_title_suggestion(
                    title,
                    accepted_title_keywords,
                )
                title_reason = _title_suggestion_reason(accepted_title_keywords)
                title_strategies = _build_title_strategies(
                    source_title=title,
                    accepted_keywords=accepted_title_keywords,
                    hot_term_keywords=hot_term_title_keywords,
                    opportunity_keywords=opportunity_title_keywords,
                )
                opportunity_title_suggestion = title_strategies[2]["title"]
                profile_payload = profile.model_dump(mode="json")
                profile_payload["title_suggestion"] = title_suggestion
                profile_payload["title_reason"] = title_reason
                profile_payload["title_strategies"] = title_strategies
                profile_payload["opportunity_title_suggestion"] = (
                    opportunity_title_suggestion
                )
                profile_payload["opportunity_title_reason"] = (
                    _opportunity_title_reason(opportunity_title_keywords)
                    if opportunity_title_suggestion
                    else None
                )
                vision_payload["model_profile"] = model_profile.model_dump(mode="json")
                vision_payload["profile"] = profile_payload
                vision_payload["recognition"] = recognition
                vision_payload["autocomplete_checks"] = autocomplete_checks
                persisted_analysis.vision_payload = vision_payload
                persisted_analysis.title_suggestion = title_suggestion
                persisted_analysis.title_reason = title_reason
                for item in observations:
                    session.add(_keyword_result_model(persisted_analysis.id, item))
                persisted_analysis.title_validation = _title_validation(
                    previous=previous,
                    current_title=title,
                    current_results=observations,
                )
                persisted_analysis.status = "completed"
                persisted_analysis.completed_at = _utcnow()
            detail = self.detail_payload(offer_id)
            if detail is None:
                raise RuntimeError("搜索定位结果保存后无法读取")
            return detail
        except Exception as exc:
            if analysis_id is not None:
                with Session(engine) as session, session.begin():
                    failed_analysis = session.get(SearchRankingAnalysis, analysis_id)
                    if failed_analysis is not None:
                        if isinstance(exc, _VisionAttemptsExhaustedError):
                            failed_analysis.vision_payload = {
                                "vision_stage_completed": False,
                                "usage": dict(exc.usage),
                                "estimated_cost_cny": exc.estimated_cost_cny,
                                "provider_attempts": [
                                    dict(item) for item in exc.provider_attempts
                                ],
                                "pricing_snapshot_date": PRICING_SNAPSHOT_DATE,
                            }
                        failed_analysis.status = "failed"
                        failed_analysis.error = _safe_error(exc)
                        failed_analysis.completed_at = _utcnow()
            raise
        finally:
            engine.dispose()

    def _assert_offer_still_eligible(
        self,
        *,
        offer_id: str,
        expected_plid: str,
        expected_title: str,
        expected_image_url: str,
    ) -> None:
        engine = create_read_only_engine(self.database_url)
        try:
            with Session(engine) as session:
                offer = session.scalar(
                    select(OfferCurrent).where(OfferCurrent.offer_id == offer_id)
                )
                if offer is None:
                    raise SearchRankingInputError(
                        "该商品已不在当前店铺 Seller Offers 中，已停止搜索"
                    )
                eligibility = _offer_eligibility(offer, self.runtime)
                _raise_if_ineligible(eligibility)
                current_title = " ".join(str(offer.title or "").split())
                current_plid = str(offer.productline_id or "").strip()
                if (
                    current_plid != expected_plid
                    or current_title != expected_title
                    or eligibility.trusted_image_url != expected_image_url
                ):
                    raise SearchRankingInputError(
                        "商品资料在识别期间发生变化，已停止搜索，请重新运行"
                    )
        finally:
            engine.dispose()


async def _collect_keyword_observation(
    client: CompetitorPublicClient,
    *,
    candidate: SearchKeywordCandidate,
    candidate_order: int,
    target_plid: str,
    profile: VisionProfile,
    max_pages: int,
    relevance_threshold: float,
    page_delay_seconds: float,
    source_title: str = "",
) -> KeywordObservation:
    keyword = candidate.phrase
    request_url, payload = await client.fetch_search_first_page(keyword)
    first_products, paging = _search_products(payload)
    first_products = first_products[:ORGANIC_PAGE_SIZE]
    validation_terms = _validation_terms(profile)
    first_page_titles = [item["title"] for item in first_products]
    relevant_flags = [
        _title_matches_terms(title, validation_terms) for title in first_page_titles
    ]
    score = sum(relevant_flags) / len(relevant_flags) if relevant_flags else 0.0
    matched_count = sum(relevant_flags)
    evaluated_count = len(relevant_flags)
    core_threshold = max(relevance_threshold, CORE_MAJORITY_FLOOR)
    provenance = _candidate_provenance(candidate)
    comparison_resample = (
        candidate.candidate_source == "comparison_resample"
        and candidate.intended_strategy == "comparison"
    )
    comparison_required = candidate.comparison_role in {"primary", "secondary"}
    accepted_as_core = score >= core_threshold and not comparison_resample
    opportunity_candidate = _provenance_has_strategy(
        provenance,
        candidate_source="takealot_autocomplete",
        intended_strategy="opportunity",
        require_autocomplete_rank=True,
    )
    autocomplete_ranks = [
        rank
        for item in provenance
        if str(item.get("candidate_source") or "") == "takealot_autocomplete"
        and (rank := _optional_int(item.get("autocomplete_rank"))) is not None
    ]
    observed_autocomplete_rank = (
        min(autocomplete_ranks)
        if autocomplete_ranks
        else candidate.autocomplete_rank
    )
    opportunity_seeds = list(
        dict.fromkeys(
            str(item.get("seed") or "").strip()
            for item in provenance
            if str(item.get("candidate_source") or "")
            == "takealot_autocomplete"
            and str(item.get("intended_strategy") or "") == "opportunity"
            and str(item.get("seed") or "").strip()
        )
    )
    opportunity_safety = _opportunity_phrase_safety(
        keyword=keyword,
        source_title=source_title,
        opportunity_seeds=opportunity_seeds,
        distinctive_terms=profile.distinctive_terms,
    )
    target_first_page_index = next(
        (
            index
            for index, product in enumerate(first_products)
            if product["plid"] == target_plid
        ),
        None,
    )
    target_on_first_page = target_first_page_index is not None
    target_counted_as_direct_competitor = bool(
        target_first_page_index is not None
        and relevant_flags[target_first_page_index]
    )
    direct_competitors_excluding_target = max(
        0,
        matched_count - int(target_counted_as_direct_competitor),
    )
    opportunity_claims_safe = bool(
        opportunity_safety["opportunity_claims_safe"]
    )
    opportunity_precheck_reasons: list[str] = []
    if not comparison_resample:
        if not opportunity_candidate:
            opportunity_precheck_reasons.append("not_opportunity_autocomplete")
        opportunity_precheck_reasons.extend(
            _opportunity_safety_rejection_reasons(opportunity_safety)
        )
        if direct_competitors_excluding_target > OPPORTUNITY_MAX_DIRECT_COMPETITORS:
            opportunity_precheck_reasons.append("too_many_direct_competitors")
    api_match = API_VERSION_PATTERN.search(request_url)
    evidence = {
        "candidate_rationale": candidate.rationale,
        "candidate_source": candidate.candidate_source,
        "intended_strategy": candidate.intended_strategy,
        "candidate_provenance": [dict(item) for item in provenance],
        "intended_strategies": list(
            dict.fromkeys(
                str(item.get("intended_strategy") or "")
                for item in provenance
                if str(item.get("intended_strategy") or "")
            )
        ),
        "effective_strategy": (
            "comparison_resample"
            if comparison_resample
            else "core" if accepted_as_core else "pending_validation"
        ),
        "comparison_baseline_rank": candidate.comparison_baseline_rank,
        "comparison_role": candidate.comparison_role,
        "comparison_strategy": candidate.comparison_strategy,
        "autocomplete_seed": candidate.seed,
        "autocomplete_seed_source": candidate.seed_source,
        "autocomplete_rank": observed_autocomplete_rank,
        "autocomplete_endpoint": (
            "searches/search_suggestions"
            if observed_autocomplete_rank is not None
            else None
        ),
        "autocomplete_is_search_volume": False,
        "demand_signal_note": (
            "该词来自上一轮建议标题的有排名基线，仅用于同词公开搜索复采。"
            if comparison_resample
            else (
                "Takealot 搜索框补全及其顺序是平台直接意图信号，但不是公开搜索量。"
                if observed_autocomplete_rank is not None
                else "该词来自图片精准识别，不把模型判断当作平台搜索量。"
            )
        ),
        "validation_terms": validation_terms,
        "profile_distinctive_terms": list(profile.distinctive_terms),
        "top_result_titles": first_page_titles[:5],
        "matched_top_results": matched_count,
        "evaluated_top_results": evaluated_count,
        "matched_first_page_results": matched_count,
        "evaluated_first_page_results": evaluated_count,
        "first_page_same_type_ratio": score,
        "first_page_majority": score >= CORE_MAJORITY_FLOOR,
        "core_threshold": core_threshold,
        "direct_competitor_count_first_page": matched_count,
        "direct_competitor_detection": "narrow_physical_form_term_in_result_title",
        "direct_competitor_detection_note": (
            "直接同类按图片识别的首个窄物理形态词匹配自然结果标题，不把宽泛用途词算作同类。"
        ),
        "direct_competitor_count_excluding_target_first_page": (
            direct_competitors_excluding_target
        ),
        "target_on_first_page": target_on_first_page,
        "target_counted_as_direct_competitor": (
            target_counted_as_direct_competitor
        ),
        "opportunity_candidate": opportunity_candidate,
        **opportunity_safety,
        "opportunity_max_direct_competitors": OPPORTUNITY_MAX_DIRECT_COMPETITORS,
        "opportunity_max_organic_rank": OPPORTUNITY_MAX_ORGANIC_RANK,
        "opportunity_qualified": False,
        "opportunity_rejection_reasons": opportunity_precheck_reasons,
        "api_version": api_match.group(1) if api_match else None,
        "sort": "Relevance",
        "page_size": ORGANIC_PAGE_SIZE,
        "columns_per_row": DESKTOP_COLUMNS,
        "position_scope": "organic_results_excluding_sponsored",
        "ranking_source": "sections.products.results:type=product_views",
        "sponsored_exclusion": "section_type_and_explicit_flags",
    }
    observed_at = _utcnow()
    total = _optional_int(paging.get("total_num_found"))
    should_scan_opportunity = (
        opportunity_candidate
        and opportunity_claims_safe
        and direct_competitors_excluding_target <= OPPORTUNITY_MAX_DIRECT_COMPETITORS
    )
    if not accepted_as_core and not should_scan_opportunity and not comparison_required:
        evidence["threshold"] = core_threshold
        evidence["effective_strategy"] = "rejected_irrelevant"
        if target_first_page_index is not None:
            first_page_rank = target_first_page_index + 1
            found = True
            page_number = 1
            page_rank = first_page_rank
            organic_rank = first_page_rank
            row_number = ((first_page_rank - 1) // DESKTOP_COLUMNS) + 1
            column_number = ((first_page_rank - 1) % DESKTOP_COLUMNS) + 1
            target_url = first_products[target_first_page_index]["url"]
        else:
            found = False
            page_number = None
            page_rank = None
            organic_rank = None
            row_number = None
            column_number = None
            target_url = None
        return KeywordObservation(
            keyword=keyword,
            candidate_order=candidate_order,
            relevance_status="rejected_irrelevant",
            relevance_score=score,
            validation_evidence=evidence,
            total_num_found=total,
            pages_scanned=1,
            found=found,
            page_number=page_number,
            page_rank=page_rank,
            organic_rank=organic_rank,
            row_number=row_number,
            column_number=column_number,
            target_url=target_url,
            observed_at=observed_at,
        )

    pages_scanned = 0
    cumulative = 0
    current_products = first_products
    current_paging = paging
    found_page_number: int | None = None
    found_page_rank: int | None = None
    found_organic_rank: int | None = None
    found_row_number: int | None = None
    found_column_number: int | None = None
    found_target_url: str | None = None
    scan_page_limit = max_pages
    opportunity_window_only = not accepted_as_core and not comparison_required
    for page_number in range(1, scan_page_limit + 1):
        pages_scanned += 1
        page_products = current_products[:ORGANIC_PAGE_SIZE]
        if opportunity_window_only:
            remaining_organic_slots = OPPORTUNITY_MAX_ORGANIC_RANK - cumulative
            if remaining_organic_slots <= 0:
                break
            page_products = page_products[:remaining_organic_slots]
        for page_rank, product in enumerate(page_products, start=1):
            if product["plid"] != target_plid:
                continue
            organic_rank = cumulative + page_rank
            found_page_number = page_number
            found_page_rank = page_rank
            found_organic_rank = organic_rank
            found_row_number = ((page_rank - 1) // DESKTOP_COLUMNS) + 1
            found_column_number = ((page_rank - 1) % DESKTOP_COLUMNS) + 1
            found_target_url = product["url"]
            break
        if found_page_number is not None:
            break
        cumulative += len(page_products)
        after = str(current_paging.get("next_is_after") or "")
        if (
            page_number >= scan_page_limit
            or not after
            or (
                opportunity_window_only
                and cumulative >= OPPORTUNITY_MAX_ORGANIC_RANK
            )
        ):
            break
        if page_delay_seconds:
            await asyncio.sleep(page_delay_seconds)
        next_payload = await client.fetch_search_next_page(request_url, after)
        current_products, current_paging = _search_products(next_payload)

    found = found_page_number is not None
    if comparison_resample:
        relevance_status = "comparison_resample"
        evidence["effective_strategy"] = "comparison_resample"
    elif accepted_as_core:
        relevance_status = "accepted"
        evidence["effective_strategy"] = "core"
    elif opportunity_candidate:
        rejection_reasons = _opportunity_safety_rejection_reasons(
            opportunity_safety
        )
        if not found:
            rejection_reasons.append("target_not_found_within_72")
        elif (
            found_organic_rank is None
            or found_organic_rank > OPPORTUNITY_MAX_ORGANIC_RANK
        ):
            rejection_reasons.append("target_beyond_organic_rank_72")
        if direct_competitors_excluding_target > OPPORTUNITY_MAX_DIRECT_COMPETITORS:
            rejection_reasons.append("too_many_direct_competitors")
        qualified = not rejection_reasons
        relevance_status = "opportunity" if qualified else "rejected_irrelevant"
        evidence["opportunity_qualified"] = qualified
        evidence["opportunity_rejection_reasons"] = rejection_reasons
        evidence["effective_strategy"] = relevance_status
    else:
        relevance_status = "rejected_irrelevant"
        evidence["effective_strategy"] = relevance_status
    return KeywordObservation(
        keyword=keyword,
        candidate_order=candidate_order,
        relevance_status=relevance_status,
        relevance_score=score,
        validation_evidence=evidence,
        total_num_found=total,
        pages_scanned=pages_scanned,
        found=found,
        page_number=found_page_number,
        page_rank=found_page_rank,
        organic_rank=found_organic_rank,
        row_number=found_row_number,
        column_number=found_column_number,
        target_url=found_target_url,
        observed_at=observed_at,
    )


def _search_products(payload: Mapping[str, Any]) -> tuple[list[dict[str, str]], Mapping[str, Any]]:
    sections = payload.get("sections")
    products_section = sections.get("products") if isinstance(sections, Mapping) else None
    if not isinstance(products_section, Mapping):
        raise SearchRankingProviderError("Takealot 搜索响应缺少自然商品区")
    raw_results = products_section.get("results")
    if not isinstance(raw_results, list):
        raise SearchRankingProviderError("Takealot 搜索响应缺少自然商品列表")
    products: list[dict[str, str]] = []
    for raw in raw_results:
        if not isinstance(raw, Mapping):
            continue
        if raw.get("type") != ORGANIC_RESULT_TYPE or _is_sponsored_search_result(raw):
            continue
        view = raw.get("product_views")
        core = view.get("core") if isinstance(view, Mapping) else None
        if not isinstance(core, Mapping):
            continue
        plid = str(core.get("id") or "")
        title = " ".join(str(core.get("title") or "").split())
        slug = str(core.get("slug") or "").strip("/")
        if not plid or not title:
            continue
        products.append(
            {
                "plid": plid,
                "title": title,
                "url": f"https://www.takealot.com/{slug}/PLID{plid}" if slug else "",
            }
        )
    paging = products_section.get("paging")
    return products, paging if isinstance(paging, Mapping) else {}


def _is_sponsored_search_result(raw: Mapping[str, Any]) -> bool:
    containers: list[Mapping[str, Any]] = [raw]
    view = raw.get("product_views")
    if isinstance(view, Mapping):
        containers.append(view)
        core = view.get("core")
        if isinstance(core, Mapping):
            containers.append(core)
    for container in containers:
        for key in ("is_sponsored", "sponsored", "is_ad", "is_promoted"):
            value = container.get(key)
            if value is True or str(value).strip().casefold() in {"1", "true", "yes"}:
                return True
        for key in ("listing_type", "placement_type", "result_type"):
            kind = str(container.get(key) or "").strip().casefold()
            if kind in {"ad", "advertisement", "promoted", "sponsored"}:
                return True
    return False


def _validation_terms(profile: VisionProfile) -> list[str]:
    # The first model term is contractually the narrow physical form. Broader use,
    # colour, or category terms must not make a mixed first page look same-type.
    primary = " ".join(profile.product_type_terms[0].casefold().split())
    return [primary] if primary else []


def _title_matches_terms(title: str, terms: list[str]) -> bool:
    title_tokens = _canonical_tokens(title)
    return any(
        bool(tokens) and set(tokens).issubset(title_tokens)
        for term in terms
        if (tokens := _canonical_tokens(term))
    )


def _canonical_tokens(value: str) -> set[str]:
    return {_canonical_token(token) for token in TOKEN_PATTERN.findall(value.casefold())}


def _canonical_token(token: str) -> str:
    irregular = {
        "lighting": "light",
        "backlighting": "backlight",
        "controlled": "control",
        "mice": "mouse",
        "powered": "power",
    }
    if token in irregular:
        return irregular[token]
    if token.endswith("ies") and len(token) > 4:
        return f"{token[:-3]}y"
    if token.endswith(("ches", "shes", "xes", "zes")) and len(token) > 4:
        return token[:-2]
    if (
        token.endswith("s")
        and len(token) > 3
        and not token.endswith(("ss", "us", "is", "ous", "less"))
    ):
        return token[:-1]
    return token


def _keyword_claims_supported(keyword: str, source_title: str) -> bool:
    return not _unsupported_fact_claim_tokens(keyword, source_title)


def _unsupported_fact_claim_tokens(keyword: str, source_title: str) -> set[str]:
    keyword_tokens = _canonical_tokens(keyword)
    source_tokens = _canonical_tokens(source_title)
    unsupported = (keyword_tokens & HIGH_RISK_CLAIM_TOKENS) - source_tokens
    if "control" in unsupported and {"remote", "control"} & source_tokens:
        unsupported.remove("control")
    unsupported.update(
        (keyword_tokens & FACT_ATTRIBUTE_CLAIM_TOKENS) - source_tokens
    )
    unsupported.update(
        (keyword_tokens & MEASUREMENT_CLAIM_TOKENS) - source_tokens
    )
    unsupported.update(
        token
        for token in keyword_tokens - source_tokens
        if token.isdigit() or COMBINED_MEASUREMENT_PATTERN.fullmatch(token)
    )
    raw_keyword_tokens = [
        _canonical_token(token)
        for token in TOKEN_PATTERN.findall(keyword.casefold())
    ]
    for index, token in enumerate(raw_keyword_tokens[:-1]):
        if token not in {"for", "with"}:
            continue
        compatibility_term = raw_keyword_tokens[index + 1]
        if (
            compatibility_term not in source_tokens
            and compatibility_term
            not in OPPORTUNITY_COMPATIBILITY_CONTEXT_TOKENS
        ):
            unsupported.add(compatibility_term)
    return unsupported


def _opportunity_phrase_safety(
    *,
    keyword: str,
    source_title: str,
    opportunity_seeds: list[str],
    distinctive_terms: list[str],
) -> dict[str, Any]:
    keyword_tokens = _canonical_tokens(keyword) - TITLE_CONNECTOR_TOKENS
    source_tokens = _canonical_tokens(source_title) - TITLE_CONNECTOR_TOKENS
    new_tokens = keyword_tokens - source_tokens
    seed_token_sets = [
        _canonical_tokens(seed) - TITLE_CONNECTOR_TOKENS
        for seed in opportunity_seeds
        if " ".join(seed.split())
    ]
    seed_covers_new_terms = not new_tokens or (
        bool(seed_token_sets)
        and any(new_tokens.issubset(seed_tokens) for seed_tokens in seed_token_sets)
    )
    seed_union = set().union(*seed_token_sets) if seed_token_sets else set()
    unsupported_autocomplete_terms = sorted(new_tokens - seed_union)
    unsupported_fact_terms = sorted(
        _unsupported_fact_claim_tokens(keyword, source_title)
    )
    distinctive_tokens = _canonical_tokens(" ".join(distinctive_terms))
    unsupported_distinctive_terms = sorted(
        (new_tokens & distinctive_tokens) - source_tokens
    )
    safe = (
        seed_covers_new_terms
        and not unsupported_autocomplete_terms
        and not unsupported_fact_terms
        and not unsupported_distinctive_terms
    )
    return {
        "opportunity_claims_safe": safe,
        "opportunity_seed_covers_new_terms": seed_covers_new_terms,
        "opportunity_seed_terms": sorted(seed_union),
        "opportunity_new_terms": sorted(new_tokens),
        "opportunity_unsupported_autocomplete_terms": (
            unsupported_autocomplete_terms
        ),
        "opportunity_unsupported_fact_terms": unsupported_fact_terms,
        "opportunity_unsupported_distinctive_terms": (
            unsupported_distinctive_terms
        ),
    }


def _opportunity_safety_rejection_reasons(
    safety: Mapping[str, Any],
) -> list[str]:
    if bool(safety.get("opportunity_claims_safe")):
        return []
    reasons = ["unsupported_high_risk_claim"]
    if not bool(safety.get("opportunity_seed_covers_new_terms")):
        reasons.append("opportunity_seed_does_not_cover_new_terms")
    if safety.get("opportunity_unsupported_autocomplete_terms"):
        reasons.append("autocomplete_added_unsupported_terms")
    if safety.get("opportunity_unsupported_fact_terms"):
        reasons.append("unsupported_fact_claim")
    if safety.get("opportunity_unsupported_distinctive_terms"):
        reasons.append("unsupported_distinctive_claim")
    return reasons


def _title_supported_keywords(
    keywords: list[str],
    source_title: str,
) -> list[str]:
    source_tokens = _canonical_tokens(source_title)
    output: list[str] = []
    for keyword in keywords:
        meaningful = _canonical_tokens(keyword) - TITLE_CONNECTOR_TOKENS
        if meaningful and meaningful.issubset(source_tokens):
            output.append(keyword)
    return output


def _opportunity_gate_from_result(
    *,
    keyword: str,
    source_title: str,
    found: bool,
    page_number: int | None,
    organic_rank: int | None,
    validation_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Re-evaluate stored and new adjacent-demand results under the strict gate."""
    evidence = validation_evidence or {}
    provenance = _evidence_candidate_provenance(evidence)
    opportunity_candidate = _provenance_has_strategy(
        provenance,
        candidate_source="takealot_autocomplete",
        intended_strategy="opportunity",
        require_autocomplete_rank=True,
    )
    target_on_first_page = bool(
        evidence.get("target_on_first_page")
        if "target_on_first_page" in evidence
        else found and page_number == 1
    )
    validation_terms = evidence.get("validation_terms")
    normalized_validation_terms = (
        [str(term) for term in validation_terms if str(term).strip()]
        if isinstance(validation_terms, list)
        else []
    )
    target_counted_as_direct_competitor = bool(
        evidence.get("target_counted_as_direct_competitor")
        if "target_counted_as_direct_competitor" in evidence
        else target_on_first_page
        and _title_matches_terms(source_title, normalized_validation_terms)
    )
    direct_count = _optional_int(
        evidence.get("direct_competitor_count_excluding_target_first_page")
    )
    if direct_count is None:
        stored_direct_count = _optional_int(
            evidence.get("direct_competitor_count_first_page")
        )
        if stored_direct_count is not None:
            direct_count = max(
                0,
                stored_direct_count - int(target_counted_as_direct_competitor),
            )
    opportunity_seeds = list(
        dict.fromkeys(
            str(item.get("seed") or "").strip()
            for item in provenance
            if str(item.get("candidate_source") or "")
            == "takealot_autocomplete"
            and str(item.get("intended_strategy") or "") == "opportunity"
            and str(item.get("seed") or "").strip()
        )
    )
    raw_distinctive_terms = evidence.get("profile_distinctive_terms")
    distinctive_terms = (
        [str(term) for term in raw_distinctive_terms if str(term).strip()]
        if isinstance(raw_distinctive_terms, list)
        else []
    )
    recomputed_safety = _opportunity_phrase_safety(
        keyword=keyword,
        source_title=source_title,
        opportunity_seeds=opportunity_seeds,
        distinctive_terms=distinctive_terms,
    )
    safety = dict(recomputed_safety)
    for key in (
        "opportunity_seed_terms",
        "opportunity_new_terms",
        "opportunity_unsupported_autocomplete_terms",
        "opportunity_unsupported_fact_terms",
        "opportunity_unsupported_distinctive_terms",
    ):
        stored_terms = evidence.get(key)
        if isinstance(stored_terms, list):
            safety[key] = sorted(
                {
                    *[str(term) for term in safety.get(key, [])],
                    *[str(term) for term in stored_terms],
                }
            )
    if "opportunity_seed_covers_new_terms" in evidence:
        safety["opportunity_seed_covers_new_terms"] = bool(
            safety["opportunity_seed_covers_new_terms"]
            and evidence.get("opportunity_seed_covers_new_terms")
        )
    safety["opportunity_claims_safe"] = bool(
        recomputed_safety["opportunity_claims_safe"]
        and (
            evidence.get("opportunity_claims_safe")
            if "opportunity_claims_safe" in evidence
            else True
        )
        and not safety["opportunity_unsupported_autocomplete_terms"]
        and not safety["opportunity_unsupported_fact_terms"]
        and not safety["opportunity_unsupported_distinctive_terms"]
        and safety["opportunity_seed_covers_new_terms"]
    )
    reasons: list[str] = []
    if not opportunity_candidate:
        reasons.append("not_opportunity_autocomplete")
    reasons.extend(_opportunity_safety_rejection_reasons(safety))
    if direct_count is None:
        reasons.append("missing_direct_competitor_evidence")
    elif direct_count > OPPORTUNITY_MAX_DIRECT_COMPETITORS:
        reasons.append("too_many_direct_competitors")
    if not found:
        reasons.append("target_not_found_within_72")
    elif organic_rank is None or organic_rank > OPPORTUNITY_MAX_ORGANIC_RANK:
        reasons.append("target_beyond_organic_rank_72")
    return {
        "opportunity_candidate": opportunity_candidate,
        **safety,
        "target_on_first_page": target_on_first_page,
        "target_counted_as_direct_competitor": target_counted_as_direct_competitor,
        "direct_competitor_count_excluding_target_first_page": direct_count,
        "opportunity_max_direct_competitors": OPPORTUNITY_MAX_DIRECT_COMPETITORS,
        "opportunity_max_organic_rank": OPPORTUNITY_MAX_ORGANIC_RANK,
        "opportunity_qualified": not reasons,
        "opportunity_rejection_reasons": reasons,
    }


def _evidence_candidate_provenance(
    evidence: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw_provenance = evidence.get("candidate_provenance")
    if isinstance(raw_provenance, list):
        output = [dict(item) for item in raw_provenance if isinstance(item, Mapping)]
        if output:
            return output
    return [
        {
            "candidate_source": evidence.get("candidate_source"),
            "intended_strategy": evidence.get("intended_strategy"),
            "seed": evidence.get("autocomplete_seed"),
            "seed_source": evidence.get("autocomplete_seed_source"),
            "autocomplete_rank": evidence.get("autocomplete_rank"),
        }
    ]


def _title_strategy_keywords(
    results: list[KeywordObservation] | list[SearchRankingKeywordResult],
    source_title: str,
    *,
    profile_distinctive_terms: list[str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    accepted_title_keywords = _title_supported_keywords(
        [item.keyword for item in results if item.relevance_status == "accepted"],
        source_title,
    )
    accepted_keys = {keyword.casefold() for keyword in accepted_title_keywords}
    autocomplete_rows: list[tuple[int, int, str]] = []
    opportunity_title_keywords: list[str] = []
    for item in results:
        evidence = (
            item.validation_evidence
            if isinstance(item.validation_evidence, Mapping)
            else {}
        )
        provenance = _evidence_candidate_provenance(evidence)
        autocomplete_rank = min(
            (
                rank
                for source in provenance
                if str(source.get("candidate_source") or "")
                == "takealot_autocomplete"
                and (rank := _optional_int(source.get("autocomplete_rank")))
                is not None
            ),
            default=None,
        )
        if (
            item.relevance_status == "accepted"
            and item.keyword.casefold() in accepted_keys
            and any(
                str(source.get("candidate_source") or "")
                == "takealot_autocomplete"
                for source in provenance
            )
            and autocomplete_rank is not None
        ):
            autocomplete_rows.append(
                (autocomplete_rank, item.candidate_order, item.keyword)
            )
        if item.relevance_status != "opportunity":
            continue
        gate_evidence = dict(evidence)
        if (
            profile_distinctive_terms is not None
            and "profile_distinctive_terms" not in gate_evidence
        ):
            gate_evidence["profile_distinctive_terms"] = list(
                profile_distinctive_terms
            )
        gate = _opportunity_gate_from_result(
            keyword=item.keyword,
            source_title=source_title,
            found=bool(item.found),
            page_number=item.page_number,
            organic_rank=item.organic_rank,
            validation_evidence=gate_evidence,
        )
        if gate["opportunity_qualified"]:
            opportunity_title_keywords.append(item.keyword)
    hot_term_keywords = [
        keyword
        for _, _, keyword in sorted(
            autocomplete_rows,
            key=lambda item: (item[0], item[1], item[2].casefold()),
        )
    ]
    return (
        accepted_title_keywords,
        hot_term_keywords,
        opportunity_title_keywords,
    )


def _cross_check_image_profile(
    profile: VisionProfile,
    source_title: str,
) -> tuple[VisionProfile, dict[str, Any]]:
    """Keep model identity image-only, then compare it with the local seller title."""
    source_tokens = _canonical_tokens(source_title)
    name_tokens = _canonical_tokens(profile.product_name)
    similarity = (
        len(source_tokens & name_tokens) / len(name_tokens) if name_tokens else 0.0
    )
    source_word_count = len(TOKEN_PATTERN.findall(source_title.casefold()))
    model_word_count = len(TOKEN_PATTERN.findall(profile.product_name.casefold()))
    copied_or_verbose = model_word_count > 7 or (
        source_word_count >= 8 and model_word_count >= 8 and similarity >= 0.9
    )
    candidate_product_name = (
        _concise_visual_product_name(profile) if copied_or_verbose else profile.product_name
    )
    product_name, removed_identity_terms = _remove_unconfirmed_identity_claims(
        candidate_product_name,
        source_title,
    )
    title_reference_terms: list[str] = []
    for raw in (
        *profile.product_type_terms,
        *profile.distinctive_terms,
        *(candidate.phrase for candidate in profile.keywords),
    ):
        phrase = " ".join(raw.split())
        tokens = _canonical_tokens(phrase)
        if not tokens or not tokens.issubset(source_tokens):
            continue
        if phrase.casefold() in {item.casefold() for item in title_reference_terms}:
            continue
        title_reference_terms.append(phrase)
        if len(title_reference_terms) >= 8:
            break
    normalized = profile.model_copy(update={"product_name": product_name})
    return normalized, {
        "basis": "image_only_then_title_cross_check",
        "model_received_source_title": False,
        "model_received_sku": False,
        "original_model_product_name": profile.product_name,
        "product_name_adjusted": copied_or_verbose or bool(removed_identity_terms),
        "removed_unconfirmed_identity_terms": removed_identity_terms,
        "source_title_similarity": round(similarity, 4),
        "title_reference_terms": title_reference_terms,
        "title_reference_role": "post_recognition_cross_check_only",
    }


def _remove_unconfirmed_identity_claims(
    product_name: str,
    source_title: str,
) -> tuple[str, list[str]]:
    source_tokens = _canonical_tokens(source_title)
    supported_claims = set(source_tokens)
    if "remote" in source_tokens:
        supported_claims.add("control")
    kept: list[str] = []
    removed: list[str] = []
    for token in _title_tokens(product_name):
        normalized = _canonical_token(token.casefold())
        if normalized in HIGH_RISK_CLAIM_TOKENS and normalized not in supported_claims:
            removed.append(token)
            continue
        kept.append(token)
    if len(kept) >= 2:
        return " ".join(kept), removed
    return product_name, []


def _concise_visual_product_name(profile: VisionProfile) -> str:
    base = max(
        profile.product_type_terms,
        key=lambda item: len(TOKEN_PATTERN.findall(item.casefold())),
    )
    output = _title_tokens(base)
    seen = {token.casefold() for token in output}
    for term in profile.distinctive_terms:
        for token in _title_tokens(term):
            key = token.casefold()
            if key in seen:
                continue
            if len(output) >= 7:
                break
            output.append(token)
            seen.add(key)
        if len(output) >= 7:
            break
    return " ".join(output[:7]) or "Product"


def _precise_candidates(
    profile: VisionProfile,
    *,
    source_title: str | None = None,
) -> list[SearchKeywordCandidate]:
    output: list[SearchKeywordCandidate] = []
    seen: set[str] = set()
    for candidate in profile.keywords:
        phrase = " ".join(candidate.phrase.split())
        if source_title and not _keyword_claims_supported(phrase, source_title):
            continue
        key = phrase.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(
            SearchKeywordCandidate(
                phrase=phrase,
                rationale=candidate.rationale.strip(),
                candidate_source="image_precise",
                intended_strategy="core",
                seed_source="image_only_model",
                candidate_provenance=(
                    {
                        "candidate_source": "image_precise",
                        "intended_strategy": "core",
                        "seed_source": "image_only_model",
                    },
                ),
            )
        )
    return output


async def _discover_keyword_candidates(
    client: CompetitorPublicClient,
    *,
    profile: VisionProfile,
    source_title: str,
    title_reference_terms: list[str],
    max_keywords: int,
) -> tuple[list[SearchKeywordCandidate], list[dict[str, Any]]]:
    precise = _precise_candidates(profile, source_title=source_title)
    core_seeds: list[tuple[KeywordCandidate, str, str]] = [
        (candidate, "image_shopper_root", "core")
        for candidate in profile.autocomplete_seeds
    ]
    core_seeds.extend(
        (
            KeywordCandidate(
                phrase=term,
                rationale="主标题中与图片识别一致的短语，仅在识别后用于补全交叉核对",
            ),
            "title_cross_check",
            "core",
        )
        for term in title_reference_terms
        if 2 <= len(term) <= 100
        and len(TOKEN_PATTERN.findall(term.casefold())) <= 4
    )
    opportunity_seeds = [
        (candidate, "image_need_state", "opportunity")
        for candidate in profile.opportunity_seeds
    ]
    seed_specs = _group_seed_specs(
        [
            *_unique_seed_specs(core_seeds)[:4],
            *_unique_seed_specs(opportunity_seeds)[:2],
        ]
    )[:AUTOCOMPLETE_SEED_LIMIT]

    autocomplete: list[tuple[float, SearchKeywordCandidate]] = []
    checks: list[dict[str, Any]] = []
    for seed_order, (seed, seed_intents) in enumerate(seed_specs):
        normalized_seed = " ".join(seed.phrase.split())
        primary_seed_source, primary_strategy = seed_intents[0]
        try:
            suggestions = (
                await client.fetch_search_suggestions(normalized_seed)
            )[:AUTOCOMPLETE_RESULT_LIMIT]
        except Exception as exc:
            checks.append(
                {
                    "seed": normalized_seed,
                    "seed_source": primary_seed_source,
                    "seed_sources": list(
                        dict.fromkeys(item[0] for item in seed_intents)
                    ),
                    "intended_strategies": list(
                        dict.fromkeys(item[1] for item in seed_intents)
                    ),
                    "status": "unavailable",
                    "error_type": type(exc).__name__,
                }
            )
            continue
        checks.append(
            {
                "seed": normalized_seed,
                "seed_source": primary_seed_source,
                "seed_sources": list(
                    dict.fromkeys(item[0] for item in seed_intents)
                ),
                "intended_strategies": list(
                    dict.fromkeys(item[1] for item in seed_intents)
                ),
                "status": "observed",
                "suggestions": suggestions,
            }
        )
        for rank, phrase in enumerate(suggestions, start=1):
            fit = _autocomplete_fit_score(phrase, profile, source_title=source_title)
            if fit <= 0:
                continue
            autocomplete.append(
                (
                    fit - (rank * 0.01) - (seed_order * 0.001),
                    SearchKeywordCandidate(
                        phrase=phrase,
                        rationale=seed.rationale.strip(),
                        candidate_source="takealot_autocomplete",
                        intended_strategy=primary_strategy,
                        seed=normalized_seed,
                        seed_source=primary_seed_source,
                        autocomplete_rank=rank,
                        candidate_provenance=tuple(
                            {
                                "candidate_source": "takealot_autocomplete",
                                "intended_strategy": intended_strategy,
                                "seed": normalized_seed,
                                "seed_source": seed_source,
                                "autocomplete_rank": rank,
                            }
                            for seed_source, intended_strategy in seed_intents
                        ),
                    ),
                )
            )

    ranked = [item for _, item in sorted(autocomplete, key=lambda row: row[0], reverse=True)]
    core_autocomplete = [
        item
        for item in ranked
        if _provenance_has_strategy(
            _candidate_provenance(item),
            candidate_source="takealot_autocomplete",
            intended_strategy="core",
        )
    ]
    narrow_core_autocomplete = [
        item
        for item in core_autocomplete
        if _candidate_has_primary_shape(item.phrase, profile)
    ]
    broad_core_autocomplete = [
        item
        for item in core_autocomplete
        if not _candidate_has_primary_shape(item.phrase, profile)
    ]
    opportunity_autocomplete = [
        item
        for item in ranked
        if _provenance_has_strategy(
            _candidate_provenance(item),
            candidate_source="takealot_autocomplete",
            intended_strategy="opportunity",
        )
    ]
    selected: list[SearchKeywordCandidate] = []
    primary_core_pool = narrow_core_autocomplete or core_autocomplete
    if primary_core_pool:
        _append_unique_candidate(selected, primary_core_pool[0], max_keywords)
    if precise:
        _append_unique_candidate(selected, precise[0], max_keywords)
    if opportunity_autocomplete and max_keywords >= 3:
        # Reserve one measured adjacent-demand query so the third strategy can be
        # evaluated instead of letting similar core variants consume every slot.
        _append_unique_candidate(selected, opportunity_autocomplete[0], max_keywords)
    if len(primary_core_pool) > 1 and max_keywords >= 3:
        _append_unique_candidate(selected, primary_core_pool[1], max_keywords)
    if max_keywords >= 4:
        used_seeds = {item.seed.casefold() for item in selected if item.seed}
        diverse_broad = next(
            (
                item
                for item in broad_core_autocomplete
                if item.seed and item.seed.casefold() not in used_seeds
            ),
            None,
        )
        if diverse_broad is not None:
            _append_unique_candidate(selected, diverse_broad, max_keywords)
        elif opportunity_autocomplete:
            _append_unique_candidate(selected, opportunity_autocomplete[0], max_keywords)
    for candidate in (
        *narrow_core_autocomplete,
        *broad_core_autocomplete,
        *precise,
        *opportunity_autocomplete,
    ):
        _append_unique_candidate(selected, candidate, max_keywords)
    return selected, checks


def _unique_seed_specs(
    seeds: list[tuple[KeywordCandidate, str, str]],
) -> list[tuple[KeywordCandidate, str, str]]:
    output: list[tuple[KeywordCandidate, str, str]] = []
    seen: set[str] = set()
    for item in seeds:
        phrase = " ".join(item[0].phrase.split()).casefold()
        if not phrase or phrase in seen:
            continue
        seen.add(phrase)
        output.append(item)
    return output


def _group_seed_specs(
    seeds: list[tuple[KeywordCandidate, str, str]],
) -> list[tuple[KeywordCandidate, tuple[tuple[str, str], ...]]]:
    output: list[tuple[KeywordCandidate, list[tuple[str, str]]]] = []
    indexes: dict[str, int] = {}
    for candidate, seed_source, intended_strategy in seeds:
        normalized = " ".join(candidate.phrase.split()).casefold()
        if not normalized:
            continue
        index = indexes.get(normalized)
        if index is None:
            indexes[normalized] = len(output)
            output.append((candidate, [(seed_source, intended_strategy)]))
            continue
        intents = output[index][1]
        intent = (seed_source, intended_strategy)
        if intent not in intents:
            intents.append(intent)
    return [(candidate, tuple(intents)) for candidate, intents in output]


def _autocomplete_fit_score(
    phrase: str,
    profile: VisionProfile,
    *,
    source_title: str,
) -> float:
    phrase_tokens = _canonical_tokens(phrase)
    if not phrase_tokens:
        return 0.0
    primary_tokens = _canonical_tokens(profile.product_type_terms[0])
    for exclusion in profile.exclusions:
        exclusion_tokens = _canonical_tokens(exclusion)
        if exclusion_tokens and exclusion_tokens.issubset(phrase_tokens):
            return 0.0
        exclusion_words = TOKEN_PATTERN.findall(exclusion.casefold())
        exclusion_head = (
            _canonical_token(exclusion_words[-1]) if exclusion_words else ""
        )
        if (
            exclusion_head
            and exclusion_head not in primary_tokens
            and exclusion_head in phrase_tokens
        ):
            return 0.0
    type_tokens = _canonical_tokens(" ".join(profile.product_type_terms))
    distinctive_tokens = _canonical_tokens(" ".join(profile.distinctive_terms))
    source_tokens = _canonical_tokens(source_title)
    type_overlap = phrase_tokens & type_tokens
    distinctive_overlap = phrase_tokens & distinctive_tokens
    source_overlap = phrase_tokens & source_tokens
    if not type_overlap and not distinctive_overlap and not source_overlap:
        return 0.0
    score = (
        (3 * len(type_overlap)) + len(distinctive_overlap) + len(source_overlap)
    ) / max(2, 2 * len(phrase_tokens))
    if primary_tokens and primary_tokens.issubset(phrase_tokens):
        score += 5.0
    if len(phrase_tokens) == 1:
        score *= 0.25
    return score


def _candidate_has_primary_shape(phrase: str, profile: VisionProfile) -> bool:
    primary_tokens = _canonical_tokens(profile.product_type_terms[0])
    return bool(primary_tokens) and primary_tokens.issubset(_canonical_tokens(phrase))


def _append_unique_candidate(
    output: list[SearchKeywordCandidate],
    candidate: SearchKeywordCandidate,
    limit: int,
) -> None:
    normalized = " ".join(candidate.phrase.split()).casefold()
    if not normalized:
        return
    duplicate_index = next(
        (
            index
            for index, item in enumerate(output)
            if " ".join(item.phrase.split()).casefold() == normalized
        ),
        None,
    )
    if duplicate_index is not None:
        existing = output[duplicate_index]
        base = (
            candidate
            if existing.candidate_source == "comparison_resample"
            and candidate.candidate_source != "comparison_resample"
            else existing
        )
        comparison_role = (
            "primary"
            if "primary" in {existing.comparison_role, candidate.comparison_role}
            else existing.comparison_role or candidate.comparison_role
        )
        output[duplicate_index] = replace(
            base,
            candidate_provenance=_merged_candidate_provenance(existing, candidate),
            comparison_baseline_rank=(
                existing.comparison_baseline_rank
                if existing.comparison_baseline_rank is not None
                else candidate.comparison_baseline_rank
            ),
            comparison_role=comparison_role,
            comparison_strategy=(
                existing.comparison_strategy or candidate.comparison_strategy
            ),
        )
        return
    if len(output) >= limit:
        return
    output.append(candidate)


def _candidate_provenance(candidate: SearchKeywordCandidate) -> tuple[dict[str, Any], ...]:
    if candidate.candidate_provenance:
        return candidate.candidate_provenance
    return (
        {
            "candidate_source": candidate.candidate_source,
            "intended_strategy": candidate.intended_strategy,
            "seed": candidate.seed,
            "seed_source": candidate.seed_source,
            "autocomplete_rank": candidate.autocomplete_rank,
        },
    )


def _merged_candidate_provenance(
    first: SearchKeywordCandidate,
    second: SearchKeywordCandidate,
) -> tuple[dict[str, Any], ...]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, int | None]] = set()
    for raw in (*_candidate_provenance(first), *_candidate_provenance(second)):
        item = dict(raw)
        key = (
            str(item.get("candidate_source") or ""),
            str(item.get("intended_strategy") or ""),
            str(item.get("seed") or ""),
            str(item.get("seed_source") or ""),
            _optional_int(item.get("autocomplete_rank")),
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return tuple(output)


def _provenance_has_strategy(
    provenance: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    *,
    candidate_source: str,
    intended_strategy: str,
    require_autocomplete_rank: bool = False,
) -> bool:
    return any(
        str(item.get("candidate_source") or "") == candidate_source
        and str(item.get("intended_strategy") or "") == intended_strategy
        and (
            not require_autocomplete_rank
            or _optional_int(item.get("autocomplete_rank")) is not None
        )
        for item in provenance
        if isinstance(item, Mapping)
    )


def _low_confidence_observation(
    keyword: str,
    candidate_order: int,
    confidence: float,
    threshold: float,
) -> KeywordObservation:
    return KeywordObservation(
        keyword=keyword,
        candidate_order=candidate_order,
        relevance_status="model_low_confidence",
        relevance_score=0,
        validation_evidence={
            "model_confidence": confidence,
            "confidence_threshold": threshold,
            "reason": "图片识别置信度未达门槛，未向 Takealot 发起可能无意义的搜索",
        },
        total_num_found=None,
        pages_scanned=0,
        found=False,
        page_number=None,
        page_rank=None,
        organic_rank=None,
        row_number=None,
        column_number=None,
        target_url=None,
        observed_at=_utcnow(),
    )


def _offer_eligibility(
    offer: OfferCurrent,
    runtime: SearchRankingRuntimeSettings,
    *,
    now: datetime | None = None,
) -> OfferEligibility:
    current_time = now or _utcnow()
    captured_at = _naive_utc(offer.captured_at)
    age_hours = max(0.0, (current_time - captured_at).total_seconds() / 3600)
    takealot_stock = _optional_int(offer.takealot_available_stock)
    seller_stock = _optional_int(offer.seller_available_stock)
    if takealot_stock is None and seller_stock is None:
        available_stock = 0
    else:
        available_stock = max(takealot_stock or 0, 0) + max(seller_stock or 0, 0)

    reasons: list[str] = []
    if str(offer.status or "").strip().casefold() != "buyable":
        reasons.append("not_buyable")
    if available_stock <= 0:
        reasons.append("no_available_stock")
    if captured_at < current_time - timedelta(hours=runtime.offer_max_age_hours):
        reasons.append("stale_snapshot")
    if not " ".join(str(offer.title or "").split()):
        reasons.append("missing_title")
    if not str(offer.productline_id or "").strip().isdigit():
        reasons.append("invalid_plid")
    try:
        trusted_image = trusted_product_image_url(str(offer.image_url or ""))
    except ProductImageInputError:
        trusted_image = None
        reasons.append("untrusted_image")
    return OfferEligibility(
        eligible=not reasons,
        reasons=tuple(reasons),
        trusted_image_url=trusted_image,
        available_stock=available_stock,
        captured_at=captured_at,
        age_hours=age_hours,
    )


def _raise_if_ineligible(eligibility: OfferEligibility) -> None:
    if eligibility.eligible:
        return
    labels = [ELIGIBILITY_REASON_LABELS.get(reason, reason) for reason in eligibility.reasons]
    raise SearchRankingInputError(
        "该链接不再满足‘当前店铺自有且在售’条件，未调用模型：" + "、".join(labels)
    )


def _analysis_cache_key(
    *,
    image_url: str,
    provider_signature: str,
) -> str:
    # The model sees only the image. Title changes therefore reuse the paid vision
    # result while title cross-checking and rank collection are recomputed locally.
    raw = "\n".join((PROMPT_VERSION, provider_signature, image_url))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _previous_analysis_snapshot(
    session: Session,
    offer_id: str,
    *,
    current_title: str | None = None,
) -> dict[str, Any] | None:
    completed = list(
        session.scalars(
            select(SearchRankingAnalysis)
            .where(
                SearchRankingAnalysis.offer_id == offer_id,
                SearchRankingAnalysis.status == "completed",
            )
            .order_by(SearchRankingAnalysis.id.desc())
            .limit(24)
        )
    )
    if not completed:
        return None
    results_by_analysis: dict[int, list[SearchRankingKeywordResult]] = {}

    def results_for(
        analysis: SearchRankingAnalysis,
    ) -> list[SearchRankingKeywordResult]:
        cached_results = results_by_analysis.get(analysis.id)
        if cached_results is not None:
            return cached_results
        loaded = list(
            session.scalars(
                select(SearchRankingKeywordResult)
                .where(SearchRankingKeywordResult.analysis_id == analysis.id)
                .order_by(SearchRankingKeywordResult.candidate_order)
            )
        )
        results_by_analysis[analysis.id] = loaded
        return loaded

    previous = completed[0]
    normalized_current_title = " ".join(str(current_title or "").split()).casefold()
    if (
        normalized_current_title
        and " ".join(previous.source_title.split()).casefold()
        != normalized_current_title
    ):
        for candidate_analysis in completed:
            candidate_results = results_for(candidate_analysis)
            candidate_accepted, _, _ = _title_strategy_keywords(
                candidate_results,
                candidate_analysis.source_title,
            )
            candidate_issued = _issued_title_strategies(
                candidate_analysis,
                accepted_title_keywords=candidate_accepted,
                opportunity_title_keywords=[
                    item.keyword
                    for item in candidate_results
                    if item.relevance_status == "opportunity"
                ],
            )
            if any(
                " ".join(str(item.get("title") or "").split()).casefold()
                == normalized_current_title
                for item in candidate_issued
            ):
                previous = candidate_analysis
                break
    results = results_for(previous)
    previous_vision = previous.vision_payload or {}
    previous_raw_profile = (
        previous_vision.get("profile", previous_vision)
        if isinstance(previous_vision, Mapping)
        else {}
    )
    previous_profile = (
        previous_raw_profile if isinstance(previous_raw_profile, Mapping) else {}
    )
    raw_distinctive_terms = previous_profile.get("distinctive_terms")
    previous_distinctive_terms = (
        [str(term) for term in raw_distinctive_terms if str(term).strip()]
        if isinstance(raw_distinctive_terms, list)
        else []
    )
    (
        accepted_title_keywords,
        hot_term_title_keywords,
        opportunity_title_keywords,
    ) = _title_strategy_keywords(
        results,
        previous.source_title,
        profile_distinctive_terms=previous_distinctive_terms,
    )
    core_suggestion = _build_title_suggestion(
        previous.source_title,
        accepted_title_keywords,
    )
    title_strategies = _build_title_strategies(
        source_title=previous.source_title,
        accepted_keywords=accepted_title_keywords,
        hot_term_keywords=hot_term_title_keywords,
        opportunity_keywords=opportunity_title_keywords,
    )
    issued_strategies = _issued_title_strategies(
        previous,
        accepted_title_keywords=accepted_title_keywords,
        opportunity_title_keywords=[
            item.keyword for item in results if item.relevance_status == "opportunity"
        ],
    )
    issued_keyword_keys = {
        " ".join(str(keyword).split()).casefold()
        for strategy in issued_strategies
        for keyword in strategy.get("evidence_keywords", [])
        if " ".join(str(keyword).split())
    }
    baseline_ranks = {
        item.keyword.casefold(): int(item.organic_rank)
        for item in results
        if item.organic_rank is not None
        and (
            item.relevance_status in {"accepted", "comparison_resample"}
            or (
                isinstance(item.validation_evidence, Mapping)
                and _optional_int(
                    item.validation_evidence.get("comparison_baseline_rank")
                )
                is not None
            )
            or item.keyword.casefold() in issued_keyword_keys
            or (
                item.relevance_status == "opportunity"
                and _opportunity_gate_from_result(
                    keyword=item.keyword,
                    source_title=previous.source_title,
                    found=bool(item.found),
                    page_number=item.page_number,
                    organic_rank=item.organic_rank,
                    validation_evidence={
                        **(
                            dict(item.validation_evidence)
                            if isinstance(item.validation_evidence, Mapping)
                            else {}
                        ),
                        "profile_distinctive_terms": (
                            (
                                item.validation_evidence.get(
                                    "profile_distinctive_terms"
                                )
                                if isinstance(
                                    item.validation_evidence,
                                    Mapping,
                                )
                                and "profile_distinctive_terms"
                                in item.validation_evidence
                                else previous_distinctive_terms
                            )
                        ),
                    },
                )["opportunity_qualified"]
            )
        )
    }
    return {
        "source_title": previous.source_title,
        "title_suggestion": previous.title_suggestion or core_suggestion,
        # Matching uses the titles actually issued by the historical analysis.
        # Recomputed strategies above are only for current display semantics.
        "title_suggestions": [item["title"] for item in issued_strategies],
        "issued_strategies": issued_strategies,
        "display_title_strategies": title_strategies,
        "analysis_id": previous.id,
        "ranks": baseline_ranks,
    }


def _issued_title_strategies(
    analysis: SearchRankingAnalysis,
    *,
    accepted_title_keywords: list[str],
    opportunity_title_keywords: list[str],
) -> list[dict[str, Any]]:
    vision = analysis.vision_payload or {}
    raw_profile = vision.get("profile", vision) if isinstance(vision, Mapping) else {}
    profile = raw_profile if isinstance(raw_profile, Mapping) else {}
    stored_strategies = profile.get("title_strategies")
    output: list[dict[str, Any]] = []

    def append_strategy(
        strategy: str,
        title: Any,
        keywords: Any,
        *,
        policy_status: str,
    ) -> None:
        normalized_title = " ".join(str(title or "").split())
        if not normalized_title:
            return
        normalized_keywords = (
            [
                " ".join(str(keyword).split())
                for keyword in keywords
                if " ".join(str(keyword).split())
            ]
            if isinstance(keywords, list)
            else []
        )
        duplicate = next(
            (
                item
                for item in output
                if str(item["title"]).casefold() == normalized_title.casefold()
            ),
            None,
        )
        if duplicate is not None:
            duplicate["evidence_keywords"] = list(
                dict.fromkeys(
                    [*duplicate["evidence_keywords"], *normalized_keywords]
                )
            )
            return
        output.append(
            {
                "strategy": strategy,
                "title": normalized_title,
                "evidence_keywords": list(dict.fromkeys(normalized_keywords)),
                "policy_status": policy_status,
            }
        )

    if isinstance(stored_strategies, list):
        for raw in stored_strategies:
            if not isinstance(raw, Mapping):
                continue
            append_strategy(
                str(raw.get("strategy") or "historical"),
                raw.get("title"),
                raw.get("evidence_keywords"),
                policy_status="stored_issued",
            )
        return output

    # Legacy analyses predate title_strategies, but the persisted issued title
    # remains an audit fact even when today's stricter display gate would hide it.
    append_strategy(
        "contiguous_core",
        analysis.title_suggestion or profile.get("title_suggestion"),
        accepted_title_keywords[:1],
        policy_status="legacy_issued_deprecated",
    )
    append_strategy(
        "adjacent_opportunity",
        profile.get("opportunity_title_suggestion"),
        opportunity_title_keywords[:1],
        policy_status="legacy_issued_deprecated",
    )
    return output


def _previous_issued_strategies(previous: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_issued = previous.get("issued_strategies")
    if isinstance(raw_issued, list):
        return [dict(item) for item in raw_issued if isinstance(item, Mapping)]
    if "title_suggestions" in previous:
        raw_suggestions = previous.get("title_suggestions")
        if not isinstance(raw_suggestions, list):
            return []
        return [
            {
                "strategy": "historical",
                "title": " ".join(str(title).split()),
            }
            for title in raw_suggestions
            if " ".join(str(title).split())
        ]
    fallback = " ".join(str(previous.get("title_suggestion") or "").split())
    return (
        [{"strategy": "historical", "title": fallback}]
        if fallback
        else []
    )


def _matched_previous_strategy(
    previous: Mapping[str, Any] | None,
    current_title: str,
) -> dict[str, Any] | None:
    if previous is None:
        return None
    normalized_title = " ".join(current_title.split()).casefold()
    return next(
        (
            item
            for item in _previous_issued_strategies(previous)
            if " ".join(str(item.get("title") or "").split()).casefold()
            == normalized_title
        ),
        None,
    )


def _baseline_ranks(previous: Mapping[str, Any]) -> dict[str, int]:
    raw_ranks = previous.get("ranks")
    if not isinstance(raw_ranks, Mapping):
        return {}
    output: dict[str, int] = {}
    for keyword, raw_rank in raw_ranks.items():
        rank = _optional_int(raw_rank)
        normalized_keyword = " ".join(str(keyword).split()).casefold()
        if normalized_keyword and rank is not None:
            output[normalized_keyword] = rank
    return output


def _strategy_baseline_keywords(
    matched_strategy: Mapping[str, Any],
    baseline_ranks: Mapping[str, int],
) -> list[str]:
    return [
        keyword
        for keyword in _strategy_required_keywords(
            matched_strategy,
            fallback_keywords=list(baseline_ranks),
        )
        if keyword in baseline_ranks
    ]


def _strategy_required_keywords(
    matched_strategy: Mapping[str, Any],
    *,
    fallback_keywords: list[str],
) -> list[str]:
    if "evidence_keywords" not in matched_strategy:
        return list(dict.fromkeys(fallback_keywords))
    raw_keywords = matched_strategy.get("evidence_keywords")
    if not isinstance(raw_keywords, list):
        return []
    output: list[str] = []
    for keyword in raw_keywords:
        normalized = " ".join(str(keyword).split()).casefold()
        if normalized and normalized not in output:
            output.append(normalized)
    return output


def _inject_comparison_resample_candidates(
    candidates: list[SearchKeywordCandidate],
    *,
    previous: Mapping[str, Any] | None,
    current_title: str,
    max_keywords: int,
) -> list[SearchKeywordCandidate]:
    matched_strategy = _matched_previous_strategy(previous, current_title)
    if matched_strategy is None or previous is None:
        return candidates[:max_keywords]
    ranks = _baseline_ranks(previous)
    primary_keywords = _strategy_required_keywords(
        matched_strategy,
        fallback_keywords=list(ranks),
    )
    primary_keys = set(primary_keywords)
    ordered_keywords = [
        *primary_keywords,
        *(keyword for keyword in ranks if keyword not in primary_keys),
    ]
    fresh_by_keyword = {
        " ".join(candidate.phrase.split()).casefold(): candidate
        for candidate in candidates
    }
    output: list[SearchKeywordCandidate] = []
    used: set[str] = set()
    strategy = str(matched_strategy.get("strategy") or "historical")
    for keyword in ordered_keywords:
        if len(output) >= max_keywords:
            break
        role = "primary" if keyword in primary_keys else "secondary"
        fresh = fresh_by_keyword.get(keyword)
        if fresh is not None:
            candidate = replace(
                fresh,
                comparison_baseline_rank=ranks.get(keyword),
                comparison_role=role,
                comparison_strategy=strategy,
            )
        else:
            candidate = SearchKeywordCandidate(
                phrase=keyword,
                rationale="上一轮建议标题对应搜索词的同词排名复采",
                candidate_source="comparison_resample",
                intended_strategy="comparison",
                seed_source="previous_analysis_baseline",
                candidate_provenance=(
                    {
                        "candidate_source": "comparison_resample",
                        "intended_strategy": "comparison",
                        "seed_source": "previous_analysis_baseline",
                    },
                ),
                comparison_baseline_rank=ranks.get(keyword),
                comparison_role=role,
                comparison_strategy=strategy,
            )
        output.append(candidate)
        used.add(keyword)
    for candidate in candidates:
        if len(output) >= max_keywords:
            break
        normalized = " ".join(candidate.phrase.split()).casefold()
        if normalized in used:
            continue
        output.append(candidate)
        used.add(normalized)
    return output


def _title_validation(
    *,
    previous: dict[str, Any] | None,
    current_title: str,
    current_results: list[KeywordObservation],
) -> dict[str, Any]:
    base = {
        "causality": "observational_only",
        "guarantee": False,
        "note": "排名会受时间、个性化、库存、价格和平台策略影响；单次前移不能证明由标题导致。",
    }
    if previous is None:
        return {**base, "status": "baseline_created", "comparisons": []}
    old_title = " ".join(str(previous.get("source_title") or "").split())
    if current_title.casefold() == old_title.casefold():
        return {**base, "status": "pending_title_change", "comparisons": []}
    matched_strategy = _matched_previous_strategy(previous, current_title)
    if matched_strategy is None:
        return {**base, "status": "changed_to_other_title", "comparisons": []}
    matched_suggestion = " ".join(str(matched_strategy.get("title") or "").split())
    previous_ranks = _baseline_ranks(previous)
    required_keywords = _strategy_required_keywords(
        matched_strategy,
        fallback_keywords=list(previous_ranks),
    )
    required_keys = set(required_keywords)
    missing_baseline_keywords = [
        keyword for keyword in required_keywords if keyword not in previous_ranks
    ]
    current_by_keyword: dict[str, KeywordObservation] = {}
    for result in current_results:
        key = " ".join(result.keyword.split()).casefold()
        existing = current_by_keyword.get(key)
        if existing is None or (
            existing.organic_rank is None and result.organic_rank is not None
        ):
            current_by_keyword[key] = result
    comparisons: list[dict[str, Any]] = []
    missing_keywords: list[str] = []
    for keyword in required_keywords:
        if keyword not in previous_ranks:
            continue
        current_result = current_by_keyword.get(keyword)
        if current_result is None or current_result.organic_rank is None:
            missing_keywords.append(keyword)
            continue
        before = previous_ranks[keyword]
        after = current_result.organic_rank
        comparisons.append(
            {
                "keyword": keyword,
                "before_rank": before,
                "after_rank": after,
                "delta": before - after,
            }
        )
    secondary_comparisons: list[dict[str, Any]] = []
    for keyword, before in previous_ranks.items():
        if keyword in required_keys:
            continue
        secondary_result = current_by_keyword.get(keyword)
        if secondary_result is None or secondary_result.organic_rank is None:
            continue
        secondary_comparisons.append(
            {
                "keyword": keyword,
                "before_rank": before,
                "after_rank": secondary_result.organic_rank,
                "delta": before - secondary_result.organic_rank,
            }
        )
    if (
        not required_keywords
        or missing_baseline_keywords
        or missing_keywords
    ):
        status = "insufficient_comparable_evidence"
    elif all(item["delta"] > 0 for item in comparisons):
        status = "observed_forward"
    elif any(item["delta"] > 0 for item in comparisons):
        status = "mixed_movement"
    else:
        status = "no_observed_forward"
    return {
        **base,
        "status": status,
        "matched_suggestion": matched_suggestion,
        "matched_strategy": str(matched_strategy.get("strategy") or "historical"),
        "required_keywords": required_keywords,
        "missing_baseline_keywords": missing_baseline_keywords,
        "missing_keywords": missing_keywords,
        "comparisons": comparisons,
        "secondary_comparisons": secondary_comparisons,
    }


def _build_title_suggestion(
    raw_suggestion: str,
    accepted_keywords: list[str],
) -> str:
    """Place validated search wording first and return punctuation-free title text."""
    priority_tokens = (
        _title_tokens(accepted_keywords[0]) if accepted_keywords else []
    )
    return _build_title_from_priority_tokens(raw_suggestion, priority_tokens)


def _build_hot_term_title_suggestion(
    raw_suggestion: str,
    hot_term_keywords: list[str],
) -> str | None:
    """Naturally merge overlapping autocomplete phrases before the source title."""
    merged, _ = _merge_hot_term_keywords(hot_term_keywords)
    if not merged:
        return None
    return _build_title_from_priority_tokens(raw_suggestion, merged)


def _build_title_strategies(
    *,
    source_title: str,
    accepted_keywords: list[str],
    hot_term_keywords: list[str],
    opportunity_keywords: list[str],
) -> list[dict[str, Any]]:
    """Return three evidence-bounded title tactics with stable API keys."""
    safe_accepted_keywords = _title_supported_keywords(
        accepted_keywords,
        source_title,
    )
    core_title = _build_title_suggestion(source_title, safe_accepted_keywords)
    safe_hot_keywords = _title_supported_keywords(hot_term_keywords, source_title)
    distinct_hot_keywords: list[str] = []
    seen_hot_phrases: set[tuple[str, ...]] = set()
    for keyword in safe_hot_keywords:
        canonical_phrase = tuple(
            _canonical_token(token)
            for token in TOKEN_PATTERN.findall(keyword.casefold())
        )
        if not canonical_phrase or canonical_phrase in seen_hot_phrases:
            continue
        seen_hot_phrases.add(canonical_phrase)
        distinct_hot_keywords.append(keyword)
    _, mergeable_hot_keywords = _merge_hot_term_keywords(distinct_hot_keywords)
    hot_title = _build_hot_term_title_suggestion(source_title, mergeable_hot_keywords)
    safe_opportunity_keywords = [
        keyword
        for keyword in opportunity_keywords
        if _keyword_claims_supported(keyword, source_title)
    ]
    opportunity_title = (
        _build_title_suggestion(core_title, safe_opportunity_keywords[:1])
        if safe_opportunity_keywords
        else None
    )
    core_available = bool(safe_accepted_keywords)
    hot_available = bool(
        len(mergeable_hot_keywords) >= 2
        and hot_title
        and hot_title.casefold() != core_title.casefold()
    )
    opportunity_available = bool(
        safe_opportunity_keywords
        and opportunity_title
        and opportunity_title.casefold() != core_title.casefold()
        and (
            not hot_available
            or hot_title is None
            or opportunity_title.casefold() != hot_title.casefold()
        )
    )
    return [
        {
            "strategy": "contiguous_core",
            "label": "完整连续词组版",
            "title": core_title if core_available else None,
            "available": core_available,
            "explanation": (
                "把首个通过首页同类验证的完整词组连续前置，卖点和参数后置；"
                "修改后仍需按相同词复采，不能保证排名前移。"
                if core_available
                else "本轮没有通过首页同类验证的核心词，因此不生成连续词组版。"
            ),
            "evidence_keywords": safe_accepted_keywords[:1],
        },
        {
            "strategy": "hot_term_coverage",
            "label": "类目热词覆盖版",
            "title": hot_title if hot_available else None,
            "available": hot_available,
            "explanation": (
                "只合并已通过相关性验证且真实出现在 Takealot 搜索框补全中的词；"
                "补全顺序不是搜索量，修改后仍需复采。"
                if hot_available
                else "本轮没有形成与完整连续词组版不同，且同时满足标题支持、相关性通过和真实补全证据的类目词版本。"
            ),
            "evidence_keywords": mergeable_hot_keywords[:3] if hot_available else [],
        },
        {
            "strategy": "adjacent_opportunity",
            "label": "相邻需求蓝海版",
            "title": opportunity_title if opportunity_available else None,
            "available": opportunity_available,
            "explanation": (
                "仅使用能找到本商品、自然位不超过72，且首页扣除本商品后直接同类不超过2个"
                "的相邻需求补全词；直接同类按窄物理形态词匹配结果标题，"
                "这是待复采打法，不保证排名前移。"
                if opportunity_available
                else (
                    "相邻需求词已通过证据门槛，但没有形成与前两种打法不同的标题。"
                    if safe_opportunity_keywords
                    else "本轮没有相邻需求词同时通过真实补全、目标命中、前72位和低同类竞争门槛。"
                )
            ),
            "evidence_keywords": safe_opportunity_keywords[:1],
        },
    ]


def _build_title_from_priority_tokens(
    raw_suggestion: str,
    priority_tokens: list[str],
) -> str:
    suggestion_tokens = _title_tokens(raw_suggestion)
    preferred_case: dict[str, str] = {}
    for token in suggestion_tokens:
        preferred_case.setdefault(token.casefold(), token)

    output: list[str] = []
    output_keys: set[str] = set()

    for token in priority_tokens:
        key = _title_dedup_key(token)
        if key in output_keys:
            continue
        output.append(_title_token_case(token, preferred_case))
        output_keys.add(key)

    for token in suggestion_tokens:
        key = _title_dedup_key(token)
        if key in output_keys:
            continue
        output.append(token)
        output_keys.add(key)

    if not output:
        output = ["Product"]
    while output and len(" ".join(output)) > TITLE_MAX_LENGTH:
        output.pop()
    return " ".join(output) or "Product"


def _longest_title_phrase_overlap(
    existing: list[str],
    incoming: list[str],
) -> tuple[int, int, int] | None:
    best: tuple[int, int, int] | None = None
    for existing_start in range(len(existing)):
        for incoming_start in range(len(incoming)):
            length = 0
            while (
                existing_start + length < len(existing)
                and incoming_start + length < len(incoming)
                and _title_dedup_key(existing[existing_start + length])
                == _title_dedup_key(incoming[incoming_start + length])
            ):
                length += 1
            if length and (best is None or length > best[2]):
                best = (existing_start, incoming_start, length)
    return best


def _merge_hot_term_keywords(
    hot_term_keywords: list[str],
) -> tuple[list[str], list[str]]:
    merged: list[str] = []
    used_keywords: list[str] = []
    for keyword in hot_term_keywords[:3]:
        phrase = _title_tokens(keyword)
        if not phrase:
            continue
        if not merged:
            merged = list(phrase)
            used_keywords.append(keyword)
            continue
        overlap = _longest_title_phrase_overlap(merged, phrase)
        if overlap is None:
            continue
        merged_start, phrase_start, overlap_length = overlap
        prefix = phrase[:phrase_start]
        suffix = phrase[phrase_start + overlap_length :]
        merged = (
            merged[:merged_start]
            + prefix
            + merged[merged_start : merged_start + overlap_length]
            + suffix
            + merged[merged_start + overlap_length :]
        )
        merged = _deduplicate_title_tokens(merged)
        used_keywords.append(keyword)
    return merged, used_keywords


def _deduplicate_title_tokens(tokens: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        key = _title_dedup_key(token)
        if key in seen:
            continue
        output.append(token)
        seen.add(key)
    return output


def _title_tokens(value: str) -> list[str]:
    without_punctuation = "".join(
        character if character.isalnum() else " " for character in str(value)
    )
    return without_punctuation.split()


def _title_dedup_key(token: str) -> str:
    normalized = token.casefold()
    if normalized in {"lighting", "backlighting"}:
        return normalized
    return _canonical_token(normalized)


def _title_token_case(token: str, preferred_case: dict[str, str]) -> str:
    known = preferred_case.get(token.casefold())
    if known is not None:
        return known
    if token.isalpha() and token.islower():
        return token.capitalize()
    return token


def _title_suggestion_reason(accepted_keywords: list[str]) -> str:
    if accepted_keywords:
        return (
            "建议标题已由服务器按固定规则整理：首个通过验证且获当前标题支持的核心词"
            "完整前置，其他卖点和参数保持后置，标题只保留字母、数字和空格。修改后仍需使用"
            "相同搜索词复采排名，不能保证前移。"
        )
    return (
        "当前没有候选搜索词通过 Takealot 相关性验证；建议标题只执行无标点清洗，"
        "不能据此判断排名会前移。"
    )


def _opportunity_title_reason(opportunity_keywords: list[str]) -> str:
    return (
        f"相邻需求蓝海版把真实补全词“{opportunity_keywords[0]}”前置："
        "本轮已找到目标商品 自然位不超过72 且首页扣除本商品后直接同类不超过2个。"
        "补全顺序不是搜索量，修改后仍需复采且不能保证前移。"
    )


def _keyword_result_model(
    analysis_id: int,
    item: KeywordObservation,
) -> SearchRankingKeywordResult:
    return SearchRankingKeywordResult(
        analysis_id=analysis_id,
        keyword=item.keyword,
        candidate_order=item.candidate_order,
        relevance_status=item.relevance_status,
        relevance_score=Decimal(str(item.relevance_score)),
        validation_evidence=item.validation_evidence,
        total_num_found=item.total_num_found,
        pages_scanned=item.pages_scanned,
        found=item.found,
        page_number=item.page_number,
        page_rank=item.page_rank,
        organic_rank=item.organic_rank,
        row_number=item.row_number,
        column_number=item.column_number,
        columns_per_row=DESKTOP_COLUMNS,
        target_url=item.target_url,
        observed_at=item.observed_at,
    )


def _offer_summary(
    offer: OfferCurrent,
    analysis: SearchRankingAnalysis | None,
    eligibility: OfferEligibility,
) -> dict[str, Any]:
    return {
        "offer_id": offer.offer_id,
        "productline_id": offer.productline_id,
        "sku": offer.sku,
        "title": offer.title,
        "image_url": eligibility.trusted_image_url,
        "offer_status": offer.status,
        "available_stock": eligibility.available_stock,
        "takealot_available_stock": offer.takealot_available_stock,
        "seller_available_stock": offer.seller_available_stock,
        "captured_at": eligibility.captured_at.isoformat(),
        "snapshot_age_hours": round(eligibility.age_hours, 2),
        "ownership_source": "authenticated_store_seller_offers",
        "analyzable": eligibility.eligible,
        "latest_analysis": _analysis_history_item(analysis) if analysis else None,
    }


def _analysis_history_item(analysis: SearchRankingAnalysis) -> dict[str, Any]:
    vision = analysis.vision_payload or {}
    usage = vision.get("usage", {}) if isinstance(vision, Mapping) else {}
    return {
        "id": analysis.id,
        "status": analysis.status,
        "source_title": analysis.source_title,
        "provider": analysis.provider,
        "model": analysis.model,
        "confidence": _float_or_none(analysis.confidence),
        "vision_reused": analysis.vision_reused,
        "created_at": analysis.created_at.isoformat(),
        "completed_at": analysis.completed_at.isoformat() if analysis.completed_at else None,
        "error": analysis.error,
        "vision_stage_completed": bool(
            isinstance(vision, Mapping)
            and vision.get("vision_stage_completed") is True
        ),
        "usage": dict(usage) if isinstance(usage, Mapping) else {},
        "estimated_cost_cny": (
            _float_or_none(vision.get("estimated_cost_cny"))
            if isinstance(vision, Mapping)
            else None
        ),
        "title_validation_status": (
            str((analysis.title_validation or {}).get("status") or "") or None
        ),
    }


def _analysis_payload(
    analysis: SearchRankingAnalysis,
    results: list[SearchRankingKeywordResult],
) -> dict[str, Any]:
    vision = analysis.vision_payload or {}
    raw_profile = vision.get("profile", vision) if isinstance(vision, dict) else {}
    profile = dict(raw_profile) if isinstance(raw_profile, Mapping) else {}
    raw_distinctive_terms = profile.get("distinctive_terms")
    profile_distinctive_terms = (
        [str(term) for term in raw_distinctive_terms if str(term).strip()]
        if isinstance(raw_distinctive_terms, list)
        else []
    )
    (
        accepted_title_keywords,
        hot_term_title_keywords,
        opportunity_title_keywords,
    ) = _title_strategy_keywords(
        results,
        analysis.source_title,
        profile_distinctive_terms=profile_distinctive_terms,
    )
    title_suggestion = _build_title_suggestion(
        analysis.source_title,
        accepted_title_keywords,
    )
    title_reason = _title_suggestion_reason(accepted_title_keywords)
    title_strategies = _build_title_strategies(
        source_title=analysis.source_title,
        accepted_keywords=accepted_title_keywords,
        hot_term_keywords=hot_term_title_keywords,
        opportunity_keywords=opportunity_title_keywords,
    )
    opportunity_title_suggestion = title_strategies[2]["title"]
    profile["title_suggestion"] = title_suggestion
    profile["title_reason"] = title_reason
    profile["title_strategies"] = title_strategies
    profile["opportunity_title_suggestion"] = opportunity_title_suggestion
    profile["opportunity_title_reason"] = (
        _opportunity_title_reason(opportunity_title_keywords)
        if opportunity_title_suggestion
        else None
    )
    return {
        **_analysis_history_item(analysis),
        "product_name": analysis.product_name,
        "category": analysis.category,
        "profile": profile,
        "recognition": (
            vision.get("recognition", {}) if isinstance(vision, dict) else {}
        ),
        "autocomplete_checks": (
            vision.get("autocomplete_checks", []) if isinstance(vision, dict) else []
        ),
        "provider_attempts": (
            vision.get("provider_attempts", []) if isinstance(vision, dict) else []
        ),
        "usage": vision.get("usage", {}) if isinstance(vision, dict) else {},
        "estimated_cost_cny": (
            _float_or_none(vision.get("estimated_cost_cny"))
            if isinstance(vision, dict)
            else None
        ),
        "title_suggestion": title_suggestion,
        "title_reason": title_reason,
        "title_strategies": title_strategies,
        "opportunity_title_suggestion": opportunity_title_suggestion,
        "opportunity_title_reason": (
            _opportunity_title_reason(opportunity_title_keywords)
            if opportunity_title_suggestion
            else None
        ),
        "title_validation": analysis.title_validation,
        "keywords": [
            _keyword_payload(
                item,
                source_title=analysis.source_title,
                profile_distinctive_terms=profile_distinctive_terms,
            )
            for item in results
        ],
    }


def _keyword_payload(
    item: SearchRankingKeywordResult,
    *,
    source_title: str | None = None,
    profile_distinctive_terms: list[str] | None = None,
) -> dict[str, Any]:
    evidence = (
        dict(item.validation_evidence)
        if isinstance(item.validation_evidence, Mapping)
        else {}
    )
    relevance_status = item.relevance_status
    if (
        profile_distinctive_terms is not None
        and "profile_distinctive_terms" not in evidence
    ):
        evidence["profile_distinctive_terms"] = list(profile_distinctive_terms)
    if (
        source_title is not None
        and relevance_status != "accepted"
        and (
            relevance_status == "opportunity"
            or evidence.get("intended_strategy") == "opportunity"
        )
    ):
        opportunity_gate = _opportunity_gate_from_result(
            keyword=item.keyword,
            source_title=source_title,
            found=bool(item.found),
            page_number=item.page_number,
            organic_rank=item.organic_rank,
            validation_evidence=evidence,
        )
        evidence.update(opportunity_gate)
        evidence["stored_relevance_status"] = item.relevance_status
        evidence["effective_relevance_status"] = (
            "opportunity"
            if opportunity_gate["opportunity_qualified"]
            else "rejected_irrelevant"
        )
        if relevance_status == "opportunity" and not opportunity_gate[
            "opportunity_qualified"
        ]:
            relevance_status = "rejected_irrelevant"
    return {
        "id": item.id,
        "keyword": item.keyword,
        "candidate_order": item.candidate_order,
        "relevance_status": relevance_status,
        "relevance_score": float(item.relevance_score),
        "validation_evidence": evidence,
        "total_num_found": item.total_num_found,
        "pages_scanned": item.pages_scanned,
        "found": item.found,
        "page_number": item.page_number,
        "page_rank": item.page_rank,
        "organic_rank": item.organic_rank,
        "row_number": item.row_number,
        "column_number": item.column_number,
        "columns_per_row": item.columns_per_row,
        "target_url": item.target_url,
        "search_url": f"https://www.takealot.com/all?qsearch={quote(item.keyword)}",
        "observed_at": item.observed_at.isoformat(),
    }


def _chat_profile_json(body: Mapping[str, Any]) -> str:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("missing choices")
    first = choices[0]
    message = first.get("message") if isinstance(first, Mapping) else None
    if not isinstance(message, Mapping):
        raise ValueError("missing message")
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tool_call in tool_calls:
            function = (
                tool_call.get("function") if isinstance(tool_call, Mapping) else None
            )
            if not isinstance(function, Mapping):
                continue
            if function.get("name") != "submit_takealot_product_profile":
                continue
            arguments = function.get("arguments")
            if isinstance(arguments, str) and arguments.strip():
                return arguments
            if isinstance(arguments, Mapping):
                return json.dumps(arguments)
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    raise ValueError("missing function arguments")


def _validated_chat_profile(body: Mapping[str, Any]) -> VisionProfile:
    """Normalize minor provider schema variance before strict business validation."""
    raw = json.loads(_chat_profile_json(body))
    if not isinstance(raw, dict):
        raise ValueError("product profile must be an object")
    rationales = {
        "keywords": "图片模型给出的精准商品搜索表达",
        "autocomplete_seeds": "图片模型给出的自然搜索词根",
        "opportunity_seeds": "图片模型给出的相邻需求词根",
    }
    for field, fallback_rationale in rationales.items():
        candidates = raw.get(field)
        if not isinstance(candidates, list):
            continue
        normalized: list[Any] = []
        for candidate in candidates:
            if isinstance(candidate, str):
                normalized.append(
                    {
                        "phrase": " ".join(candidate.split()),
                        "rationale": fallback_rationale,
                    }
                )
            elif isinstance(candidate, Mapping):
                item = dict(candidate)
                if not " ".join(str(item.get("rationale") or "").split()):
                    item["rationale"] = fallback_rationale
                normalized.append(item)
            else:
                normalized.append(candidate)
        raw[field] = normalized
    return VisionProfile.model_validate(raw)


def _safe_error(exc: Exception) -> str:
    if isinstance(
        exc,
        (
            SearchRankingInputError,
            SearchRankingConfigurationError,
            SearchRankingProviderError,
        ),
    ):
        return str(exc)[:1000]
    return "搜索定位运行失败；请查看服务日志后重试"


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    return float(value) if value is not None else None


def _naive_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _thumbnail_data_url(
    settings: SearchRankingRuntimeSettings,
    image_url: str,
) -> str:
    cache = ProductThumbnailCache(settings.project_root)
    try:
        path = cache.thumbnail_path(image_url, settings.image_max_dimension)
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except (ProductImageInputError, ProductImageUnavailableError, OSError) as exc:
        raise SearchRankingProviderError(
            "商品主图暂时无法读取，未调用多模态模型"
        ) from exc
    finally:
        cache.close()
    return f"data:image/jpeg;base64,{encoded}"


def _normalized_vision_usage(body: Any) -> dict[str, int]:
    usage = body.get("usage") if isinstance(body, Mapping) else {}
    normalized = usage if isinstance(usage, Mapping) else {}
    input_tokens = _optional_int(
        normalized.get("prompt_tokens") or normalized.get("input_tokens")
    ) or 0
    output_tokens = _optional_int(
        normalized.get("completion_tokens") or normalized.get("output_tokens")
    ) or 0
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": _optional_int(normalized.get("total_tokens"))
        or input_tokens + output_tokens,
    }


def _estimated_cost_cny(
    provider: VisionProviderSettings,
    usage: Mapping[str, int],
) -> float:
    amount = (
        int(usage.get("input_tokens") or 0)
        * provider.input_price_cny_per_million
        + int(usage.get("output_tokens") or 0)
        * provider.output_price_cny_per_million
    ) / 1_000_000
    return round(amount, 6)


def _https_base_url(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip().rstrip("/")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise SearchRankingConfigurationError(f"{name} 不是有效地址") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SearchRankingConfigurationError(f"{name} 必须是无凭据、无参数的 HTTPS 地址")
    return value


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError as exc:
        raise SearchRankingConfigurationError(f"{name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise SearchRankingConfigurationError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _bounded_choice_int(name: str, default: int, choices: set[int]) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError as exc:
        raise SearchRankingConfigurationError(f"{name} 必须是整数") from exc
    if value not in choices:
        allowed = "、".join(str(item) for item in sorted(choices))
        raise SearchRankingConfigurationError(f"{name} 只支持 {allowed}")
    return value


def _bounded_float(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(os.environ.get(name, default))
    except ValueError as exc:
        raise SearchRankingConfigurationError(f"{name} 必须是数字") from exc
    if not minimum <= value <= maximum:
        raise SearchRankingConfigurationError(
            f"{name} 必须在 {minimum:g} 到 {maximum:g} 之间"
        )
    return value


_SYSTEM_PROMPT = """
You are the image-only first stage of a Takealot search-intent pipeline. No seller title,
SKU, listing metadata, or historical keyword is available to you. Identify the physical
product from visible pixels instead of transcribing packaging or image text. product_name
must be a concise 2-7 word identity, never an SEO title.

Return three deliberately different groups of South African English shopper wording:
1. keywords: 2-5 precise 2-6 word long-tail queries for shoppers who already know the
   target product type.
2. autocomplete_seeds: 2-5 short 1-3 word roots a less certain shopper would naturally
   type before choosing a Takealot search-box completion, such as a product noun, use,
   room, device, or locally natural term. These are roots, not invented completions.
3. opportunity_seeds: 1-3 relevant adjacent need-state roots where this product could
   satisfy the same buyer even if the direct product type may be less crowded. Relevance
   is mandatory; do not force an unrelated traffic lane.

Accuracy beats breadth. Do not invent a brand, model, material, compatibility, capacity,
size, audience, or feature not visible in the image. Avoid department-only words, SEO
stuffing, and near duplicates. product_type_terms must be short noun phrases likely to
occur in genuinely same-type result titles; they drive deterministic first-page checks.
The first product_type_terms item is mandatory and must be the narrow physical form or
shape (for example light bars rather than RGB lights or ambient lighting), because only
that first item is allowed to decide whether the full first page contains the same type.
distinctive_terms describe visible differentiators. exclusions list plausible visual
confusions and adjacent products that should be filtered out. The image-only title
suggestion must be punctuation-free natural English with product wording first and
visible selling points later. It is only source material for later platform validation,
never a promise that ranking will improve.
""".strip()
