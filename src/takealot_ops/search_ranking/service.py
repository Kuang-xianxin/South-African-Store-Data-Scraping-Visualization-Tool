"""Image-led keyword discovery with live Takealot relevance and rank validation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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


PROMPT_VERSION = "takealot-search-v2"
ORGANIC_PAGE_SIZE = 36
DESKTOP_COLUMNS = 4
TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
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
        return tuple(provider for provider in self.providers if provider.api_key)

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
        return "|".join(
            f"{provider.name}:{provider.model}" for provider in self.providers
        )

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
        title: str,
        sku: str | None,
    ) -> VisionCallResult: ...


class OpenAICompatibleProductVisionClient:
    """Cross-vendor multimodal chat client with forced schema tools and fallback."""

    def __init__(self, settings: SearchRankingRuntimeSettings) -> None:
        self.settings = settings

    async def identify(
        self,
        *,
        image_url: str,
        title: str,
        sku: str | None,
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
        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=False,
        ) as client:
            for provider_index, provider in enumerate(providers):
                try:
                    return await self._request_provider(
                        client,
                        provider=provider,
                        image_data_url=image_data_url,
                        title=title,
                        sku=sku,
                    )
                except (SearchRankingConfigurationError, SearchRankingProviderError) as exc:
                    last_error = exc
                    if provider_index == len(providers) - 1:
                        raise
        raise SearchRankingProviderError("千问与豆包多模态服务暂时均不可用") from last_error

    async def _request_provider(
        self,
        client: httpx.AsyncClient,
        *,
        provider: VisionProviderSettings,
        image_data_url: str,
        title: str,
        sku: str | None,
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
                                "Analyze this seller offer. The current title is: "
                                f"{title}\nSeller SKU: {sku or 'unknown'}"
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
                profile = VisionProfile.model_validate_json(_chat_profile_json(body))
            except (ValueError, TypeError, ValidationError, json.JSONDecodeError) as exc:
                raise SearchRankingProviderError("多模态模型没有返回合格的结构化商品识别结果") from exc
            usage = body.get("usage") if isinstance(body, dict) else {}
            input_tokens = int(
                (usage or {}).get("prompt_tokens")
                or (usage or {}).get("input_tokens")
                or 0
            )
            output_tokens = int(
                (usage or {}).get("completion_tokens")
                or (usage or {}).get("output_tokens")
                or 0
            )
            normalized_usage = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": int(
                    (usage or {}).get("total_tokens") or input_tokens + output_tokens
                ),
            }
            return VisionCallResult(
                profile=profile,
                provider=provider.name,
                model=provider.model,
                response_id=str(body.get("id")) if body.get("id") else None,
                usage=normalized_usage,
                estimated_cost_cny=_estimated_cost_cny(provider, normalized_usage),
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
            "position_scope": "organic_results_excluding_sponsored",
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
                latest: dict[str, SearchRankingAnalysis] = {}
                for analysis in analyses:
                    latest.setdefault(analysis.offer_id, analysis)
                evaluated = [
                    (offer, _offer_eligibility(offer, self.runtime, now=now))
                    for offer in offers
                ]
                items = [
                    _offer_summary(offer, latest.get(offer.offer_id), eligibility)
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
                latest = analyses[0] if analyses else None
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
                previous = _previous_analysis_snapshot(session, offer_id)
                cache_key = _analysis_cache_key(
                    image_url=image_url,
                    title=title,
                    provider_signature=self.runtime.provider_signature,
                )
                cached = session.scalar(
                    select(SearchRankingAnalysis)
                    .where(
                        SearchRankingAnalysis.cache_key == cache_key,
                        SearchRankingAnalysis.status == "completed",
                        SearchRankingAnalysis.vision_payload.is_not(None),
                    )
                    .order_by(SearchRankingAnalysis.id.desc())
                    .limit(1)
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
                sku = offer.sku

            if cached_payload is not None:
                profile = VisionProfile.model_validate(
                    cached_payload.get("profile", cached_payload)
                )
                source_usage = cached_payload.get("usage", {})
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
                    title=title,
                    sku=sku,
                )
                profile = call.profile
                used_provider = call.provider
                used_model = call.model
                vision_payload = {
                    "profile": profile.model_dump(mode="json"),
                    "usage": call.usage,
                    "response_id": call.response_id,
                    "estimated_cost_cny": call.estimated_cost_cny,
                    "pricing_snapshot_date": PRICING_SNAPSHOT_DATE,
                }

            candidates = _unique_candidates(profile)[: self.runtime.max_keywords]
            observations: list[KeywordObservation] = []
            if profile.confidence < self.runtime.confidence_threshold:
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
                    for order, candidate in enumerate(candidates, start=1):
                        observations.append(
                            await _collect_keyword_observation(
                                search_client,
                                keyword=candidate.phrase,
                                rationale=candidate.rationale,
                                candidate_order=order,
                                target_plid=plid,
                                profile=profile,
                                max_pages=self.runtime.max_pages,
                                relevance_threshold=self.runtime.relevance_threshold,
                                page_delay_seconds=self.runtime.page_delay_seconds,
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
                persisted_analysis.vision_payload = vision_payload
                persisted_analysis.title_suggestion = profile.title_suggestion
                persisted_analysis.title_reason = profile.title_reason
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
    keyword: str,
    rationale: str,
    candidate_order: int,
    target_plid: str,
    profile: VisionProfile,
    max_pages: int,
    relevance_threshold: float,
    page_delay_seconds: float,
) -> KeywordObservation:
    request_url, payload = await client.fetch_search_first_page(keyword)
    first_products, paging = _search_products(payload)
    validation_terms = _validation_terms(profile)
    top_titles = [item["title"] for item in first_products[:12]]
    relevant_flags = [_title_matches_terms(title, validation_terms) for title in top_titles]
    score = sum(relevant_flags) / len(relevant_flags) if relevant_flags else 0.0
    api_match = API_VERSION_PATTERN.search(request_url)
    evidence = {
        "candidate_rationale": rationale,
        "validation_terms": validation_terms,
        "top_result_titles": top_titles[:5],
        "matched_top_results": sum(relevant_flags),
        "evaluated_top_results": len(relevant_flags),
        "api_version": api_match.group(1) if api_match else None,
        "sort": "Relevance",
        "page_size": ORGANIC_PAGE_SIZE,
        "columns_per_row": DESKTOP_COLUMNS,
        "position_scope": "organic_results_excluding_sponsored",
    }
    observed_at = _utcnow()
    total = _optional_int(paging.get("total_num_found"))
    if score < relevance_threshold:
        evidence["threshold"] = relevance_threshold
        return KeywordObservation(
            keyword=keyword,
            candidate_order=candidate_order,
            relevance_status="rejected_irrelevant",
            relevance_score=score,
            validation_evidence=evidence,
            total_num_found=total,
            pages_scanned=1,
            found=False,
            page_number=None,
            page_rank=None,
            organic_rank=None,
            row_number=None,
            column_number=None,
            target_url=None,
            observed_at=observed_at,
        )

    pages_scanned = 0
    cumulative = 0
    current_products = first_products
    current_paging = paging
    for page_number in range(1, max_pages + 1):
        pages_scanned += 1
        for page_rank, product in enumerate(current_products, start=1):
            if product["plid"] != target_plid:
                continue
            organic_rank = cumulative + page_rank
            return KeywordObservation(
                keyword=keyword,
                candidate_order=candidate_order,
                relevance_status="accepted",
                relevance_score=score,
                validation_evidence=evidence,
                total_num_found=total,
                pages_scanned=pages_scanned,
                found=True,
                page_number=page_number,
                page_rank=page_rank,
                organic_rank=organic_rank,
                row_number=((page_rank - 1) // DESKTOP_COLUMNS) + 1,
                column_number=((page_rank - 1) % DESKTOP_COLUMNS) + 1,
                target_url=product["url"],
                observed_at=observed_at,
            )
        cumulative += len(current_products)
        after = str(current_paging.get("next_is_after") or "")
        if page_number >= max_pages or not after:
            break
        if page_delay_seconds:
            await asyncio.sleep(page_delay_seconds)
        next_payload = await client.fetch_search_next_page(request_url, after)
        current_products, current_paging = _search_products(next_payload)
    return KeywordObservation(
        keyword=keyword,
        candidate_order=candidate_order,
        relevance_status="accepted",
        relevance_score=score,
        validation_evidence=evidence,
        total_num_found=total,
        pages_scanned=pages_scanned,
        found=False,
        page_number=None,
        page_rank=None,
        organic_rank=None,
        row_number=None,
        column_number=None,
        target_url=None,
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


def _validation_terms(profile: VisionProfile) -> list[str]:
    terms: list[str] = []
    for raw in profile.product_type_terms:
        normalized = " ".join(raw.casefold().split())
        if normalized and normalized not in terms:
            terms.append(normalized)
    return terms


def _title_matches_terms(title: str, terms: list[str]) -> bool:
    title_tokens = set(TOKEN_PATTERN.findall(title.casefold()))
    return any(
        bool(tokens) and set(tokens).issubset(title_tokens)
        for term in terms
        if (tokens := TOKEN_PATTERN.findall(term))
    )


def _unique_candidates(profile: VisionProfile) -> list[KeywordCandidate]:
    output: list[KeywordCandidate] = []
    seen: set[str] = set()
    for candidate in profile.keywords:
        phrase = " ".join(candidate.phrase.split())
        key = phrase.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(KeywordCandidate(phrase=phrase, rationale=candidate.rationale.strip()))
    return output


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
    title: str,
    provider_signature: str,
) -> str:
    raw = "\n".join((PROMPT_VERSION, provider_signature, image_url, title))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _previous_analysis_snapshot(session: Session, offer_id: str) -> dict[str, Any] | None:
    previous = session.scalar(
        select(SearchRankingAnalysis)
        .where(
            SearchRankingAnalysis.offer_id == offer_id,
            SearchRankingAnalysis.status == "completed",
        )
        .order_by(SearchRankingAnalysis.id.desc())
        .limit(1)
    )
    if previous is None:
        return None
    results = list(
        session.scalars(
            select(SearchRankingKeywordResult).where(
                SearchRankingKeywordResult.analysis_id == previous.id
            )
        )
    )
    return {
        "source_title": previous.source_title,
        "title_suggestion": previous.title_suggestion,
        "analysis_id": previous.id,
        "ranks": {
            item.keyword.casefold(): item.organic_rank
            for item in results
            if item.relevance_status == "accepted"
        },
    }


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
    suggestion = " ".join(str(previous.get("title_suggestion") or "").split())
    old_title = " ".join(str(previous.get("source_title") or "").split())
    if current_title.casefold() == old_title.casefold():
        return {**base, "status": "pending_title_change", "comparisons": []}
    if not suggestion or current_title.casefold() != suggestion.casefold():
        return {**base, "status": "changed_to_other_title", "comparisons": []}
    previous_ranks = previous.get("ranks") or {}
    comparisons: list[dict[str, Any]] = []
    for result in current_results:
        before = previous_ranks.get(result.keyword.casefold())
        after = result.organic_rank
        if before is None or after is None:
            continue
        comparisons.append(
            {
                "keyword": result.keyword,
                "before_rank": before,
                "after_rank": after,
                "delta": before - after,
            }
        )
    if not comparisons:
        status = "insufficient_comparable_evidence"
    elif all(item["delta"] > 0 for item in comparisons):
        status = "observed_forward"
    elif any(item["delta"] > 0 for item in comparisons):
        status = "mixed_movement"
    else:
        status = "no_observed_forward"
    return {**base, "status": status, "comparisons": comparisons}


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
        "title_validation_status": (
            str((analysis.title_validation or {}).get("status") or "") or None
        ),
    }


def _analysis_payload(
    analysis: SearchRankingAnalysis,
    results: list[SearchRankingKeywordResult],
) -> dict[str, Any]:
    vision = analysis.vision_payload or {}
    profile = vision.get("profile", vision) if isinstance(vision, dict) else {}
    return {
        **_analysis_history_item(analysis),
        "product_name": analysis.product_name,
        "category": analysis.category,
        "profile": profile,
        "usage": vision.get("usage", {}) if isinstance(vision, dict) else {},
        "estimated_cost_cny": (
            _float_or_none(vision.get("estimated_cost_cny"))
            if isinstance(vision, dict)
            else None
        ),
        "title_suggestion": analysis.title_suggestion,
        "title_reason": analysis.title_reason,
        "title_validation": analysis.title_validation,
        "keywords": [_keyword_payload(item) for item in results],
    }


def _keyword_payload(item: SearchRankingKeywordResult) -> dict[str, Any]:
    return {
        "id": item.id,
        "keyword": item.keyword,
        "candidate_order": item.candidate_order,
        "relevance_status": item.relevance_status,
        "relevance_score": float(item.relevance_score),
        "validation_evidence": item.validation_evidence,
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
You identify ecommerce products from both the supplied image and current seller title.
Return 2-5 precise English search phrases that a South African shopper would type on
Takealot for this exact product type. Accuracy beats breadth. Do not invent a brand,
model, material, compatibility, capacity, size, audience, or feature that is not visible
or stated. Avoid vague department words, SEO stuffing, and near-duplicate phrases.
product_type_terms must contain short terms that should occur in genuinely relevant top
result titles; they will be used for deterministic platform validation. distinctive_terms
are optional attributes that distinguish this item. exclusions describe plausible visual
confusions. The title suggestion must be natural English, preserve verified facts only,
and put the most important accepted product wording early. It is a hypothesis for later
rank observation, never a promise that ranking will improve.
""".strip()
