"""Image-led keyword discovery with live Takealot relevance and rank validation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import parse_qsl, quote, urlsplit

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from takealot_ops.competitors.api import CompetitorPublicClient
from takealot_ops.erp.product_images import (
    ProductImageInputError,
    ProductImageUnavailableError,
    ProductThumbnailCache,
    trusted_product_image_url,
)
from takealot_ops.search_ranking.codex_cli import (
    CODEX_TERRA_MODEL,
    CodexAppServerClient,
    CodexCliConfigurationError,
    CodexCliProviderError,
    CodexCliQuotaExceededError,
    CodexWeeklyQuotaGuard,
)
from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.migrations import (
    create_engine_for_database_url,
    create_read_only_engine,
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


PROMPT_VERSION = "takealot-v17-demand-coverage"
MODEL_MARKET_CONTEXT: Literal["South Africa"] = "South Africa"
MODEL_LANGUAGE_VARIANT: Literal["South African English"] = "South African English"
MODEL_SHOPPER_CONTEXT: Literal["South African local customer habits"] = (
    "South African local customer habits"
)
MODEL_LOCALIZATION_POLICY_VERSION = "za-shopper-v1"
ORGANIC_PAGE_SIZE = 36
DESKTOP_COLUMNS = 4
TITLE_MAX_LENGTH = 160
ORGANIC_RESULT_TYPE = "product_views"
AUTOCOMPLETE_RESULT_LIMIT = 5
AUTOCOMPLETE_CACHE_TTL_HOURS = 24
ROOT_EXPANSION_INPUT_LIMIT = 20
ROOT_EXPANSION_CORE_ROOT_LIMIT = 14
ROOT_EXPANSION_OPPORTUNITY_ROOT_LIMIT = 1
ROOT_EXPANSION_FOLLOWUP_ROOT_LIMIT = 4
ROOT_EXPANSION_PHRASE_MAX_WORDS = 5
TITLE_ROOT_EXPANSION_LIMIT = 8
ROOT_SOURCE_PRIORITY = (
    "human_confirmed_product_fact",
    "image_title_same_product_lexicon",
    "image_title_first_instinct",
    "title_word_root",
    "result_page_learning",
    "image_title_need_state",
    "title_cross_check",
)
PLATFORM_ROOT_EXPANSION_SOURCES = {
    "takealot_root_expansion",
    "takealot_autocomplete",  # Historical persisted analyses.
}
SHOPPER_JOURNEY_CANDIDATE_POOL_LIMIT = 18
MODEL_DIRECT_QUERY_TARGET = 6
MODEL_DIRECT_QUERY_MIN_WORDS = 2
MODEL_DIRECT_QUERY_MAX_WORDS = 4
MODEL_DIRECT_QUERY_PREFERRED_MAX_WORDS = 3
MODEL_DIRECT_QUERY_MIN_PREFERRED_COUNT = 4
SAME_PRODUCT_LEXICON_POLICY_VERSION = "same-product-lexicon-v2"
SAME_PRODUCT_LEXICON_ROOT_LIMIT = 4
ROOT_EXPANSION_CORE_QUERY_TARGET = 6
SELLER_TITLE_COMPLETE_PHRASE_QUERY_MAX = 1
ROOT_EXPANSION_OPPORTUNITY_QUERY_TARGET = 1
ADAPTIVE_RECOVERY_QUERY_TARGET = 1
ADAPTIVE_BASE_QUERY_TARGET = (
    MODEL_DIRECT_QUERY_TARGET
    + ROOT_EXPANSION_CORE_QUERY_TARGET
    + ROOT_EXPANSION_OPPORTUNITY_QUERY_TARGET
)
ADAPTIVE_VALID_PLATFORM_ROOT_TARGET = 3
RESULT_PAGE_LEARNING_MIN_MATCHES = 2
DECISION_PARAMETER_POLICY_VERSION = "manual-title-v1"
DECISION_PARAMETER_MAX_CANDIDATES = 12
DECISION_PARAMETER_MAX_POSITIVE = 3
CORE_MAJORITY_FLOOR = 0.51
CORE_DEMAND_COMPETITOR_RATIO_FLOOR = 0.30
CORE_DEMAND_COMPETITOR_MIN_RESULTS = 8
CORE_MIN_PLATFORM_RESULTS = ORGANIC_PAGE_SIZE
SEMANTIC_ADJACENT_RATIO_FLOOR = 0.50
SEMANTIC_SUPPORTED_RATIO_FLOOR = 0.70
SEMANTIC_ADJACENT_MIN_RESULTS = 3
OPPORTUNITY_MAX_DIRECT_COMPETITORS = 2
OPPORTUNITY_MAX_ORGANIC_RANK = 72
IDENTITY_TITLE_SIMILARITY_FLOOR = 0.40
HIGH_RISK_CLAIM_TOKENS = {
    "app",
    "battery",
    "bluetooth",
    "compressed",
    "compression",
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
    "vacuum",
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
TITLE_ROOT_EXPANSION_NOISE_TOKENS = {
    "functional",
    "function",
    "home",
    "multi",
    "multifunction",
    "multifunctional",
    "new",
    "plid",
    "style",
}
TITLE_PRESERVED_BRAND_PHRASES = (
    ("nexohogar",),
    ("nexohogarheight",),
    ("nexohogart",),
    ("soly", "sombra"),
    ("wovibo",),
)
IDENTITY_TOKEN_ALIASES = {
    "cellphone": "phone",
    "couch": "sofa",
    "loveseat": "sofa",
    "refrigerator": "fridge",
    "settee": "sofa",
    "smartphone": "phone",
    "television": "tv",
}
CANONICAL_COMPOUND_TOKEN_PARTS = {
    # Marketplace titles freely alternate between closed and open compounds.
    # Keep this list deliberately small and product-shaped: generic substring
    # splitting would make unrelated titles look semantically identical.
    "backlight": ("back", "light"),
    "floodlight": ("flood", "light"),
    "lightbar": ("light", "bar"),
    "soundbar": ("sound", "bar"),
}
GENERIC_IDENTITY_HEAD_TOKENS = {
    "accessory",
    "bar",
    "board",
    "box",
    "case",
    "cover",
    "device",
    "holder",
    "kit",
    "light",
    "machine",
    "mat",
    "pad",
    "rack",
    "screen",
    "set",
    "stand",
    "system",
    "unit",
}
LEXICON_AMBIGUOUS_IDENTITY_HEAD_TOKENS = {
    "accessory",
    "box",
    "device",
    "furniture",
    "system",
    "unit",
}
SEMANTIC_RETARGETING_HEAD_TOKENS = GENERIC_IDENTITY_HEAD_TOKENS | {
    "adapter",
    "attachment",
    "bag",
    "base",
    "cable",
    "charger",
    "cleaner",
    "cushion",
    "liner",
    "mattress",
    "part",
    "pillow",
    "protector",
    "replacement",
    "sleeve",
    "table",
    "topper",
}
TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
COMBINED_MEASUREMENT_PATTERN = re.compile(
    r"^\d+(?:\.\d+)?(?:cm|ft|g|gb|inch|inches|kg|l|litre|litres|m|mah|ml|mm|tb|v|volt|w|watt)$"
)
TITLE_PARAMETER_UNIT_TOKENS = MEASUREMENT_CLAIM_TOKENS | {
    "a",
    "ah",
    "amps",
    "amp",
    "bar",
    "cl",
    "db",
    "dpi",
    "gallon",
    "gallons",
    "hz",
    "khz",
    "km",
    "kwh",
    "kw",
    "lb",
    "lbs",
    "liter",
    "liters",
    "lm",
    "lumen",
    "lumens",
    "ma",
    "mb",
    "mg",
    "mhz",
    "mp",
    "mw",
    "oz",
    "percent",
    "psi",
    "rpm",
    "volts",
    "watts",
    "wh",
}
TITLE_PARAMETER_COUNT_TOKENS = {
    "blade",
    "blades",
    "channel",
    "channels",
    "pack",
    "packs",
    "pair",
    "pairs",
    "pc",
    "pcs",
    "piece",
    "pieces",
    "port",
    "ports",
    "seater",
    "seaters",
    "set",
    "sets",
    "tier",
    "tiers",
}
TITLE_COMBINED_PARAMETER_PATTERN = re.compile(
    r"^\d+(?:\.\d+)?(?:a|ah|amp|amps|bar|cl|cm|db|dpi|ft|g|gal|gallon|gb|hz|"
    r"inch|inches|kg|khz|km|kw|kwh|l|lb|lbs|liter|liters|litre|litres|lm|lumen|"
    r"lumens|ma|mah|mb|mg|mhz|ml|mm|mp|mw|oz|percent|psi|rpm|tb|v|volt|volts|w|"
    r"watt|watts|wh)$"
)
TITLE_DIMENSION_PARAMETER_PATTERN = re.compile(
    r"^\d+(?:\.\d+)?(?:x\d+(?:\.\d+)?){1,2}(?:cm|ft|inch|inches|m|mm)?$"
)
TITLE_PROTECTION_RATING_PATTERN = re.compile(r"^(?:ipx?\d{1,2}[a-z]?|ik\d{2})$")
TITLE_RESOLUTION_PARAMETER_PATTERN = re.compile(r"^\d+(?:p|k)$")
TITLE_CONNECTIVITY_GENERATION_PATTERN = re.compile(r"^[3-6]g$")
INCH_PARAMETER_PATTERN = re.compile(
    r'(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*(?:-\s*)?(?:inch(?:es)?\b|["″])',
    re.IGNORECASE,
)
PROJECTION_SCREEN_HEAD_TOKENS = {"projection", "projector"}
PROJECTION_SCREEN_FORM_TOKENS = {"screen", "cloth", "curtain"}
TITLE_DECISION_PARAMETER_RULES: tuple[dict[str, Any], ...] = (
    {
        "rule": "projection_screen_diagonal",
        "identity_token_sets": tuple(
            frozenset({head, form})
            for head in sorted(PROJECTION_SCREEN_HEAD_TOKENS)
            for form in sorted(PROJECTION_SCREEN_FORM_TOKENS)
        ),
        "value_pattern": INCH_PARAMETER_PATTERN,
        "display_unit": "Inch",
        "query_shape": "projection screen",
    },
)
API_VERSION_PATTERN = re.compile(r"/rest/(v-[^/]+)/")
QWEN_INPUT_PRICE_CNY_PER_MILLION = 2.0
QWEN_OUTPUT_PRICE_CNY_PER_MILLION = 8.0
DOUBAO_INPUT_PRICE_CNY_PER_MILLION = 0.6
DOUBAO_OUTPUT_PRICE_CNY_PER_MILLION = 3.6
PRICING_SNAPSHOT_DATE = "2026-08-19"
PRODUCT_FACT_TYPES = {
    "product_type",
    "construction",
    "material",
    "function",
    "packaging",
    "usage",
}
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


class SearchRankingQuotaExceededError(SearchRankingProviderError):
    """The exact persisted Codex weekly budget no longer permits another turn."""

    def __init__(
        self,
        message: str,
        *,
        usage: dict[str, int],
        quota: Mapping[str, Any],
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.estimated_cost_cny = 0.0
        self.quota = dict(quota)
        self.provider_attempts = (
            {
                "provider": "codex_cli",
                "status": "weekly_quota_stopped",
                "usage": dict(usage),
                "quota": dict(quota),
            },
        )


class _CountedVisionProviderError(SearchRankingProviderError):
    """A model response consumed known tokens but its profile was unusable."""

    def __init__(
        self,
        message: str,
        *,
        usage: dict[str, int],
        estimated_cost_cny: float,
        provider_attempts: Sequence[Mapping[str, Any]] = (),
        failure_audit: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.estimated_cost_cny = estimated_cost_cny
        self.provider_attempts = tuple(dict(item) for item in provider_attempts)
        self.failure_audit = dict(failure_audit or {})


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
    buyer_job: str = Field(default="", max_length=200)
    alternative_product_terms: list[str] = Field(default_factory=list, max_length=8)
    excluded_product_terms: list[str] = Field(default_factory=list, max_length=8)


class AdjacentDemandCandidate(KeywordCandidate):
    """A model hypothesis that can be checked against actual result titles."""

    buyer_job: str = Field(min_length=2, max_length=200)
    alternative_product_terms: list[str] = Field(min_length=1, max_length=8)


class VisionProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Defaults keep historical cached payloads readable. New provider calls use
    # LocalizedVisionProfile, where the same literal fields are required.
    market_context: Literal["South Africa"] = MODEL_MARKET_CONTEXT
    language_variant: Literal["South African English"] = MODEL_LANGUAGE_VARIANT
    shopper_context: Literal["South African local customer habits"] = (
        MODEL_SHOPPER_CONTEXT
    )
    product_name: str = Field(min_length=2, max_length=160)
    category: str = Field(min_length=2, max_length=160)
    product_type_terms: list[str] = Field(min_length=1, max_length=6)
    same_product_aliases: list[str] = Field(default_factory=list, max_length=8)
    same_demand_product_terms: list[str] = Field(default_factory=list, max_length=12)
    distinctive_terms: list[str] = Field(min_length=0, max_length=10)
    keywords: list[KeywordCandidate] = Field(min_length=2, max_length=10)
    autocomplete_seeds: list[KeywordCandidate] = Field(min_length=2, max_length=10)
    opportunity_seeds: Sequence[KeywordCandidate] = Field(min_length=1, max_length=4)
    exclusions: list[str] = Field(min_length=0, max_length=10)
    confidence: float = Field(ge=0, le=1)
    title_suggestion: str = Field(min_length=2, max_length=160)
    title_reason: str = Field(min_length=2, max_length=500)
    requires_human_fact_confirmation: bool = False
    manual_fact_reason: str = Field(default="", max_length=500)
    missing_facts: list[str] = Field(default_factory=list, max_length=5)


class LocalizedVisionProfile(VisionProfile):
    """Provider contract requiring an explicit South African shopper context."""

    market_context: Literal["South Africa"] = Field()
    language_variant: Literal["South African English"] = Field()
    shopper_context: Literal["South African local customer habits"] = Field()


class FusionVisionProfile(LocalizedVisionProfile):
    """Strict output contract for the image-title generation stage."""

    keywords: list[KeywordCandidate] = Field(min_length=6, max_length=10)
    autocomplete_seeds: list[KeywordCandidate] = Field(min_length=6, max_length=10)
    same_product_aliases: list[str] = Field(min_length=2, max_length=8)
    same_demand_product_terms: list[str] = Field(default_factory=list, max_length=12)
    opportunity_seeds: Sequence[AdjacentDemandCandidate] = Field(min_length=1, max_length=4)

    @field_validator("keywords")
    @classmethod
    def _validate_concise_direct_queries(
        cls,
        values: list[KeywordCandidate],
    ) -> list[KeywordCandidate]:
        word_counts = [
            len(TOKEN_PATTERN.findall(item.phrase.casefold())) for item in values
        ]
        if any(
            count < MODEL_DIRECT_QUERY_MIN_WORDS
            or count > MODEL_DIRECT_QUERY_MAX_WORDS
            for count in word_counts
        ):
            raise ValueError("keywords must each contain 2 to 4 words")
        if (
            sum(count <= MODEL_DIRECT_QUERY_PREFERRED_MAX_WORDS for count in word_counts)
            < MODEL_DIRECT_QUERY_MIN_PREFERRED_COUNT
        ):
            raise ValueError("at least four keywords must contain no more than 3 words")
        return values

    @field_validator("same_product_aliases")
    @classmethod
    def _reject_generic_single_word_aliases(cls, values: list[str]) -> list[str]:
        for value in values:
            tokens = _identity_term_tokens(value)
            if len(tokens) == 1 and tokens[0] in GENERIC_IDENTITY_HEAD_TOKENS:
                raise ValueError(
                    "same_product_aliases cannot use a generic one-word product head"
                )
        return values


def _fusion_validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Return bounded, JSON-safe Pydantic evidence without echoing arbitrary inputs."""

    output: list[dict[str, Any]] = []
    for item in exc.errors(include_url=False)[:20]:
        location = ".".join(str(part) for part in item.get("loc", ())) or "profile"
        output.append(
            {
                "path": location,
                "type": str(item.get("type") or "validation_error"),
                "message": str(item.get("msg") or "invalid value")[:500],
            }
        )
    return output


def _fusion_validation_summary(exc: ValidationError) -> str:
    rows = _fusion_validation_errors(exc)
    return "；".join(
        f"{item['path']}：{item['message']}" for item in rows[:3]
    ) or "结构化字段未通过本地业务校验"


def _fusion_identity_supplements(payload: Mapping[str, Any]) -> list[str]:
    values: list[Any] = []
    raw_product_types = payload.get("product_type_terms")
    if isinstance(raw_product_types, list):
        values.extend(raw_product_types)
    raw_aliases = payload.get("same_product_aliases")
    if isinstance(raw_aliases, list):
        values.extend(raw_aliases)
    values.append(payload.get("product_name"))

    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        phrase = " ".join(str(value or "").split())
        key = phrase.casefold()
        if not phrase or key in seen:
            continue
        seen.add(key)
        output.append(phrase)
    return output


def _normalize_fusion_payload(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Safely repair only query length and generic-alias contract violations.

    Invalid model phrases are never shortened by dropping arbitrary words. They are
    removed and may only be replaced by an already returned exact product identity.
    Pydantic remains the final authority when the payload cannot be repaired safely.
    """

    normalized = json.loads(json.dumps(dict(payload), ensure_ascii=False))
    audit: dict[str, Any] = {
        "policy": "safe_identity_filter_v1",
        "applied": False,
        "keywords_removed": [],
        "keywords_added": [],
        "same_product_aliases_removed": [],
        "same_product_aliases_added": [],
        "same_demand_product_terms_removed": [],
        "same_demand_product_terms_added": [],
    }

    raw_aliases = normalized.get("same_product_aliases")
    aliases: list[str] = []
    alias_keys: set[str] = set()
    if isinstance(raw_aliases, list):
        for raw_alias in raw_aliases:
            alias = " ".join(str(raw_alias or "").split())
            key = alias.casefold()
            tokens = _identity_term_tokens(alias)
            if not alias or key in alias_keys:
                if alias:
                    audit["same_product_aliases_removed"].append(
                        {"phrase": alias, "reason": "duplicate"}
                    )
                continue
            if len(tokens) == 1 and tokens[0] in GENERIC_IDENTITY_HEAD_TOKENS:
                audit["same_product_aliases_removed"].append(
                    {"phrase": alias, "reason": "generic_one_word_identity"}
                )
                continue
            alias_keys.add(key)
            aliases.append(alias)

    identity_supplements = _fusion_identity_supplements(normalized)
    for phrase in identity_supplements:
        if len(aliases) >= 2:
            break
        key = phrase.casefold()
        tokens = _identity_term_tokens(phrase)
        if (
            key in alias_keys
            or not tokens
            or (len(tokens) == 1 and tokens[0] in GENERIC_IDENTITY_HEAD_TOKENS)
        ):
            continue
        aliases.append(phrase)
        alias_keys.add(key)
        audit["same_product_aliases_added"].append(phrase)
    normalized["same_product_aliases"] = aliases[:8]

    exact_identity_keys = {
        " ".join(str(value or "").casefold().split())
        for value in (
            *(normalized.get("product_type_terms") or []),
            *normalized["same_product_aliases"],
        )
        if str(value or "").strip()
    }
    raw_same_demand = normalized.get("same_demand_product_terms")
    same_demand_values = list(raw_same_demand) if isinstance(raw_same_demand, list) else []
    raw_opportunity = normalized.get("opportunity_seeds")
    if isinstance(raw_opportunity, list):
        for raw_intent in raw_opportunity:
            if not isinstance(raw_intent, Mapping):
                continue
            raw_alternatives = raw_intent.get("alternative_product_terms")
            if isinstance(raw_alternatives, list):
                same_demand_values.extend(raw_alternatives)

    same_demand_terms: list[str] = []
    same_demand_keys: set[str] = set()
    for raw_term in same_demand_values:
        term = " ".join(str(raw_term or "").split())
        key = term.casefold()
        tokens = _identity_term_tokens(term)
        if not term or key in same_demand_keys or key in exact_identity_keys:
            if term and key in exact_identity_keys:
                audit["same_demand_product_terms_removed"].append(
                    {"phrase": term, "reason": "exact_same_product_identity"}
                )
            continue
        if len(tokens) == 1 and tokens[0] in GENERIC_IDENTITY_HEAD_TOKENS:
            audit["same_demand_product_terms_removed"].append(
                {"phrase": term, "reason": "generic_one_word_identity"}
            )
            continue
        same_demand_keys.add(key)
        same_demand_terms.append(term)
    explicit_same_demand_keys = {
        " ".join(str(value or "").casefold().split())
        for value in (raw_same_demand or [])
        if str(value or "").strip()
    } if isinstance(raw_same_demand, list) else set()
    audit["same_demand_product_terms_added"] = [
        term for term in same_demand_terms if term.casefold() not in explicit_same_demand_keys
    ]
    normalized["same_demand_product_terms"] = same_demand_terms[:12]

    raw_keywords = normalized.get("keywords")
    keywords: list[dict[str, Any]] = []
    keyword_keys: set[str] = set()
    if isinstance(raw_keywords, list):
        for raw_keyword in raw_keywords:
            if not isinstance(raw_keyword, Mapping):
                audit["keywords_removed"].append(
                    {"phrase": "", "reason": "not_an_object"}
                )
                continue
            keyword = dict(raw_keyword)
            phrase = " ".join(str(keyword.get("phrase") or "").split())
            word_count = len(TOKEN_PATTERN.findall(phrase.casefold()))
            key = phrase.casefold()
            if not phrase:
                audit["keywords_removed"].append(
                    {"phrase": "", "reason": "empty_phrase"}
                )
                continue
            if key in keyword_keys:
                audit["keywords_removed"].append(
                    {"phrase": phrase, "reason": "duplicate"}
                )
                continue
            if not MODEL_DIRECT_QUERY_MIN_WORDS <= word_count <= MODEL_DIRECT_QUERY_MAX_WORDS:
                audit["keywords_removed"].append(
                    {
                        "phrase": phrase,
                        "reason": "outside_2_to_4_words",
                        "word_count": word_count,
                    }
                )
                continue
            keyword["phrase"] = phrase
            keywords.append(keyword)
            keyword_keys.add(key)

    def short_keyword_count() -> int:
        return sum(
            len(TOKEN_PATTERN.findall(str(item.get("phrase") or "").casefold()))
            <= MODEL_DIRECT_QUERY_PREFERRED_MAX_WORDS
            for item in keywords
        )

    supplement_rows: list[tuple[int, str]] = []
    for phrase in identity_supplements:
        word_count = len(TOKEN_PATTERN.findall(phrase.casefold()))
        if MODEL_DIRECT_QUERY_MIN_WORDS <= word_count <= MODEL_DIRECT_QUERY_MAX_WORDS:
            supplement_rows.append((word_count, phrase))
    supplement_rows.sort(key=lambda item: (item[0] > MODEL_DIRECT_QUERY_PREFERRED_MAX_WORDS, item[0]))

    for word_count, phrase in supplement_rows:
        if (
            len(keywords) >= 6
            and short_keyword_count() >= MODEL_DIRECT_QUERY_MIN_PREFERRED_COUNT
        ):
            break
        key = phrase.casefold()
        if key in keyword_keys:
            continue
        if len(keywords) >= 10:
            replace_index = next(
                (
                    index
                    for index in range(len(keywords) - 1, -1, -1)
                    if len(
                        TOKEN_PATTERN.findall(
                            str(keywords[index].get("phrase") or "").casefold()
                        )
                    )
                    > MODEL_DIRECT_QUERY_PREFERRED_MAX_WORDS
                ),
                None,
            )
            if replace_index is None or word_count > MODEL_DIRECT_QUERY_PREFERRED_MAX_WORDS:
                continue
            replaced = keywords.pop(replace_index)
            replaced_phrase = str(replaced.get("phrase") or "")
            keyword_keys.discard(replaced_phrase.casefold())
            audit["keywords_removed"].append(
                {
                    "phrase": replaced_phrase,
                    "reason": "replaced_to_meet_short_query_mix",
                }
            )
        keywords.append(
            {
                "phrase": phrase,
                "rationale": "Exact product identity already present in the fused profile.",
                "buyer_job": "",
                "alternative_product_terms": [],
                "excluded_product_terms": [],
            }
        )
        keyword_keys.add(key)
        audit["keywords_added"].append(phrase)
    normalized["keywords"] = keywords[:10]

    audit["applied"] = any(
        audit[key]
        for key in (
            "keywords_removed",
            "keywords_added",
            "same_product_aliases_removed",
            "same_product_aliases_added",
            "same_demand_product_terms_removed",
            "same_demand_product_terms_added",
        )
    )
    return normalized, audit


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
    visual_profile: VisionProfile | None = None
    fusion_profile: VisionProfile | None = None
    cross_validation: dict[str, Any] | None = None
    visual_response_id: str | None = None


@dataclass(frozen=True)
class ProductFactInput:
    fact_type: str
    fact_term: str
    statement: str = ""


@dataclass(frozen=True)
class ProductFactConfirmation:
    source_analysis_id: int
    reason_code: str
    actor_username: str
    actor_display_name: str
    facts: tuple[ProductFactInput, ...]


@dataclass(frozen=True)
class ProductFactRevocation:
    actor_username: str
    actor_display_name: str
    reason: str


@dataclass(frozen=True)
class DecisionParameterChoice:
    parameter_key: str
    is_decision_parameter: bool


@dataclass(frozen=True)
class DecisionParameterConfirmation:
    actor_username: str
    actor_display_name: str
    choices: tuple[DecisionParameterChoice, ...]


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

    @property
    def responses_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/responses"


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
    page_delay_jitter_seconds: float = 2.0
    codex_cli_path: Path | None = None
    codex_quota_state_path: Path | None = None

    @property
    def configured_providers(self) -> tuple[VisionProviderSettings, ...]:
        configured = [
            provider
            for provider in self.providers
            if provider.name in {"doubao", "qwen"} and bool(provider.api_key)
        ]
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
            return "|".join(f"{provider.name}:{provider.model}" for provider in configured)
        models = "|".join(f"{provider.name}:{provider.model}" for provider in self.providers)
        return f"unconfigured|{models}"

    @property
    def codex_quota_path(self) -> Path:
        return self.codex_quota_state_path or (
            self.project_root / "logs" / "search-ranking-codex-quota.json"
        )

    @classmethod
    def from_env(cls, project_root: Path) -> SearchRankingRuntimeSettings:
        load_dotenv(project_root / ".env", override=False)
        resolved_root = project_root.resolve()
        qwen = VisionProviderSettings(
            name="qwen",
            display_name="阿里云百炼千问",
            api_key=os.environ.get("DASHSCOPE_API_KEY", "").strip(),
            base_url=_https_base_url(
                "TAKEALOT_SEARCH_QWEN_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            model=os.environ.get("TAKEALOT_SEARCH_QWEN_MODEL", "qwen3.7-plus").strip(),
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
            project_root=resolved_root,
            providers=(doubao, qwen),
            max_pages=_bounded_int("TAKEALOT_SEARCH_MAX_PAGES", 5, 1, 10),
            max_keywords=_bounded_int("TAKEALOT_SEARCH_MAX_KEYWORDS", 14, 6, 16),
            confidence_threshold=_bounded_float(
                "TAKEALOT_SEARCH_CONFIDENCE_THRESHOLD", 0.68, 0.5, 0.95
            ),
            relevance_threshold=_bounded_float(
                "TAKEALOT_SEARCH_RELEVANCE_THRESHOLD", 0.60, 0.4, 0.95
            ),
            request_timeout_seconds=_bounded_float(
                "TAKEALOT_SEARCH_MODEL_TIMEOUT_SECONDS", 60.0, 10.0, 180.0
            ),
            page_delay_seconds=_bounded_float("TAKEALOT_SEARCH_PAGE_DELAY_SECONDS", 3.0, 0.0, 10.0),
            offer_max_age_hours=_bounded_float(
                "TAKEALOT_SEARCH_OFFER_MAX_AGE_HOURS", 36.0, 1.0, 168.0
            ),
            image_max_dimension=_bounded_choice_int(
                "TAKEALOT_SEARCH_IMAGE_MAX_DIMENSION", 640, {192, 384, 640}
            ),
            page_delay_jitter_seconds=_bounded_float(
                "TAKEALOT_SEARCH_PAGE_DELAY_JITTER_SECONDS", 2.0, 0.0, 5.0
            ),
            codex_cli_path=None,
            codex_quota_state_path=None,
        )


class VisionClient(Protocol):
    async def identify(
        self,
        *,
        image_url: str,
        reference_title: str,
        variant_context: Mapping[str, Any] | None = None,
    ) -> VisionCallResult: ...


class SearchPublicClient(Protocol):
    async def fetch_search_suggestions(self, keyword: str) -> list[str]: ...

    async def fetch_search_first_page(
        self,
        keyword: str,
    ) -> tuple[str, dict[str, Any]]: ...

    async def fetch_search_next_page(
        self,
        request_url: str,
        after: str,
    ) -> dict[str, Any]: ...


class _PublicRequestThrottle:
    """Serialize public requests across concurrent manual analyses in this ERP."""

    def __init__(
        self,
        *,
        minimum_interval_seconds: float,
        jitter_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._minimum_interval_seconds = max(0.0, minimum_interval_seconds)
        self._jitter_seconds = (
            max(0.0, jitter_seconds) if self._minimum_interval_seconds > 0 else 0.0
        )
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._last_request_started_at: float | None = None
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = self._clock()
            if self._last_request_started_at is not None:
                target_interval = self._minimum_interval_seconds + self._jitter(
                    0.0,
                    self._jitter_seconds,
                )
                remaining = target_interval - (now - self._last_request_started_at)
                if remaining > 0:
                    await self._sleep(remaining)
                    now = self._clock()
            self._last_request_started_at = now


class _SharedAutocompleteCache:
    """Persist ordered completions globally and refresh only on a stale cache hit."""

    def __init__(
        self,
        database_url: str,
        *,
        ttl: timedelta = timedelta(hours=AUTOCOMPLETE_CACHE_TTL_HOURS),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database_url = database_url
        self._ttl = ttl
        self._clock = clock or (lambda: datetime.now(UTC).replace(tzinfo=None))
        self._locks: dict[str, asyncio.Lock] = {}

    async def resolve(
        self,
        input_text: str,
        fetch_live: Callable[[], Any],
    ) -> tuple[list[str], dict[str, Any]]:
        normalized_input = " ".join(input_text.split())
        input_key = normalized_input.casefold()
        if not input_key:
            raise ValueError("补全输入不能为空")
        lock = self._locks.setdefault(input_key, asyncio.Lock())
        async with lock:
            now = self._clock()
            cached = self._load(input_key)
            if cached is not None:
                captured_at = _naive_utc(cached["captured_at"])
                age = max(timedelta(0), now - captured_at)
                if age <= self._ttl:
                    hit_count = self._record_hit(
                        input_key=input_key,
                        now=now,
                        refresh_status=None,
                        error=None,
                    )
                    return list(cached["suggestions"]), self._evidence(
                        cache_status="fresh_hit",
                        captured_at=captured_at,
                        now=now,
                        hit_count=hit_count,
                        refresh_count=int(cached["refresh_count"]),
                    )
            try:
                live = await fetch_live()
            except Exception as exc:
                if cached is not None:
                    self._record_hit(
                        input_key=input_key,
                        now=now,
                        refresh_status="failed",
                        error=_safe_error(exc),
                    )
                raise
            suggestions = _normalized_autocomplete_suggestions(live)
            cache_status = "stale_refreshed" if cached is not None else "miss_refreshed"
            saved = self._save_success(
                input_key=input_key,
                input_text=normalized_input,
                suggestions=suggestions,
                now=now,
            )
            return suggestions, self._evidence(
                cache_status=cache_status,
                captured_at=now,
                now=now,
                hit_count=int(saved["hit_count"]),
                refresh_count=int(saved["refresh_count"]),
            )

    def _load(self, input_key: str) -> dict[str, Any] | None:
        engine = create_read_only_engine(self._database_url)
        try:
            with Session(engine) as session:
                row = session.scalar(
                    select(SearchAutocompleteCache).where(
                        SearchAutocompleteCache.input_key == input_key
                    )
                )
                if row is None:
                    return None
                return {
                    "suggestions": _normalized_autocomplete_suggestions(row.suggestions),
                    "captured_at": row.captured_at,
                    "hit_count": row.hit_count,
                    "refresh_count": row.refresh_count,
                }
        finally:
            engine.dispose()

    def _record_hit(
        self,
        *,
        input_key: str,
        now: datetime,
        refresh_status: str | None,
        error: str | None,
    ) -> int:
        engine = create_engine_for_database_url(self._database_url)
        try:
            with Session(engine) as session, session.begin():
                row = session.scalar(
                    select(SearchAutocompleteCache).where(
                        SearchAutocompleteCache.input_key == input_key
                    )
                )
                if row is None:
                    return 0
                row.hit_count += 1
                row.last_hit_at = now
                row.updated_at = now
                if refresh_status is not None:
                    row.last_refresh_status = refresh_status
                    row.last_error = error
                session.flush()
                return row.hit_count
        finally:
            engine.dispose()

    def _save_success(
        self,
        *,
        input_key: str,
        input_text: str,
        suggestions: list[str],
        now: datetime,
    ) -> dict[str, int]:
        engine = create_engine_for_database_url(self._database_url)
        try:
            with Session(engine) as session, session.begin():
                row = session.scalar(
                    select(SearchAutocompleteCache).where(
                        SearchAutocompleteCache.input_key == input_key
                    )
                )
                if row is None:
                    row = SearchAutocompleteCache(
                        input_key=input_key,
                        input_text=input_text,
                        suggestions=suggestions,
                        captured_at=now,
                        last_hit_at=now,
                        hit_count=1,
                        refresh_count=1,
                        last_refresh_status="success",
                        last_error=None,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                    session.flush()
                else:
                    row.input_text = input_text
                    row.suggestions = suggestions
                    row.captured_at = now
                    row.last_hit_at = now
                    row.hit_count += 1
                    row.refresh_count += 1
                    row.last_refresh_status = "success"
                    row.last_error = None
                    row.updated_at = now
                    session.flush()
                session.add(
                    SearchAutocompleteSnapshot(
                        cache_id=row.id,
                        input_key=input_key,
                        input_text=input_text,
                        suggestions=suggestions,
                        captured_at=now,
                    )
                )
                return {
                    "hit_count": row.hit_count,
                    "refresh_count": row.refresh_count,
                }
        finally:
            engine.dispose()

    def _evidence(
        self,
        *,
        cache_status: str,
        captured_at: datetime,
        now: datetime,
        hit_count: int,
        refresh_count: int,
    ) -> dict[str, Any]:
        return {
            "cache_status": cache_status,
            "captured_at": captured_at.isoformat(),
            "age_hours": round(
                max(0.0, (now - captured_at).total_seconds() / 3600),
                4,
            ),
            "ttl_hours": int(self._ttl.total_seconds() / 3600),
            "refresh_policy": "refresh_on_first_hit_after_ttl",
            "shared_across_stores": True,
            "input_hit_count": hit_count,
            "refresh_count": refresh_count,
        }


class _PacedSearchClient:
    """Route every public Takealot call through a shared minimum-interval gate."""

    def __init__(
        self,
        client: SearchPublicClient,
        *,
        minimum_interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
        throttle: _PublicRequestThrottle | None = None,
        autocomplete_cache: _SharedAutocompleteCache | None = None,
    ) -> None:
        self._client = client
        self._throttle = throttle or _PublicRequestThrottle(
            minimum_interval_seconds=minimum_interval_seconds,
            clock=clock,
            sleep=sleep,
        )
        self._autocomplete_cache = autocomplete_cache
        self._autocomplete_evidence: dict[str, dict[str, Any]] = {}
        self.request_count = 0

    async def _pace(self) -> None:
        await self._throttle.wait()
        self.request_count += 1

    async def fetch_search_suggestions(self, keyword: str) -> list[str]:
        normalized = " ".join(keyword.split())

        async def fetch_live() -> list[str]:
            await self._pace()
            return await self._client.fetch_search_suggestions(normalized)

        if self._autocomplete_cache is None:
            suggestions = _normalized_autocomplete_suggestions(await fetch_live())
            self._autocomplete_evidence[normalized.casefold()] = {
                "cache_status": "not_configured",
                "shared_across_stores": False,
            }
            return suggestions
        suggestions, evidence = await self._autocomplete_cache.resolve(
            normalized,
            fetch_live,
        )
        self._autocomplete_evidence[normalized.casefold()] = evidence
        return suggestions

    def autocomplete_evidence(self, keyword: str) -> dict[str, Any]:
        return dict(self._autocomplete_evidence.get(" ".join(keyword.split()).casefold(), {}))

    async def fetch_search_first_page(
        self,
        keyword: str,
    ) -> tuple[str, dict[str, Any]]:
        await self._pace()
        return await self._client.fetch_search_first_page(keyword)

    async def fetch_search_next_page(
        self,
        request_url: str,
        after: str,
    ) -> dict[str, Any]:
        await self._pace()
        return await self._client.fetch_search_next_page(request_url, after)


class OpenAICompatibleProductVisionClient:
    """Cross-vendor multimodal chat client with forced schema tools and fallback."""

    def __init__(self, settings: SearchRankingRuntimeSettings) -> None:
        self.settings = settings
        self._reference_title: ContextVar[str] = ContextVar(
            "search_ranking_reference_title",
            default="",
        )
        self._variant_context: ContextVar[Mapping[str, Any] | None] = ContextVar(
            "search_ranking_variant_context",
            default=None,
        )

    async def identify(
        self,
        *,
        image_url: str,
        reference_title: str,
        variant_context: Mapping[str, Any] | None = None,
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

        title_token = self._reference_title.set(" ".join(reference_title.split()))
        variant_token = self._variant_context.set(
            dict(variant_context) if isinstance(variant_context, Mapping) else None
        )
        try:
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
                            attempt_evidence["estimated_cost_cny"] = exc.estimated_cost_cny
                        provider_attempts.append(attempt_evidence)
                        if provider_index == len(providers) - 1:
                            break
                        continue
                    record_usage(result.usage, result.estimated_cost_cny)
                    visual_profile = result.visual_profile or result.profile
                    cross_check = result.cross_validation
                    if not isinstance(cross_check, Mapping):
                        _, cross_check = _cross_check_image_profile(
                            visual_profile,
                            reference_title,
                        )
                    provider_attempts.append(
                        {
                            "provider": provider.name,
                            "status": "accepted",
                            "stages": [
                                "isolated_image_observation",
                                "image_title_fusion",
                            ],
                            "source_title_similarity": float(
                                cross_check["source_title_similarity"]
                            ),
                            "title_identity_support": bool(
                                cross_check.get("title_identity_support")
                            ),
                            "title_identity_supported_terms": list(
                                cross_check.get("title_identity_supported_terms") or []
                            ),
                            "identity_difference_level": _identity_difference_level(
                                cross_check
                            ),
                            "usage": dict(result.usage),
                            "estimated_cost_cny": result.estimated_cost_cny,
                        }
                    )
                    return replace(
                        result,
                        visual_profile=visual_profile,
                        fusion_profile=result.fusion_profile or result.profile,
                        cross_validation=dict(cross_check),
                        usage=dict(aggregate_usage),
                        estimated_cost_cny=round(aggregate_cost_cny, 10),
                        provider_attempts=tuple(provider_attempts),
                    )
        finally:
            self._reference_title.reset(title_token)
            self._variant_context.reset(variant_token)
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
        visual_result = await self._request_structured_profile(
            client,
            provider=provider,
            image_data_url=image_data_url,
            system_prompt=_VISUAL_SYSTEM_PROMPT,
            user_text=(
                "Identify the physical product shown in the image. No seller title or SKU "
                "is supplied at this stage; base the identity only on visible evidence. "
                "Express every generated term for South African local customers, using "
                "South African English and locally plausible shopping vocabulary."
            ),
            function_name="submit_takealot_visual_observation",
            function_description=(
                "Return the isolated visual product observation without seller-title evidence, "
                "with every generated term localized for South African shoppers."
            ),
            max_tokens=1100,
            profile_type=LocalizedVisionProfile,
        )
        reference_title = self._reference_title.get()
        variant_context = self._variant_context.get()
        _, cross_check = _cross_check_image_profile(
            visual_result.profile,
            reference_title,
        )
        fusion_context = json.dumps(
            {
                "source_title": reference_title,
                "variant_family": dict(variant_context or {}),
                "variant_evidence_policy": {
                    "shared_subject_uses_image_and_titles": True,
                    "variant_parameters_come_from_seller_titles": True,
                    "representative_image_does_not_verify_sibling_variant_values": True,
                    "do_not_apply_one_variant_value_to_the_whole_family": True,
                },
                "isolated_visual_observation": visual_result.profile.model_dump(mode="json"),
                "independent_cross_validation": {
                    "source_title_similarity": cross_check["source_title_similarity"],
                    "title_identity_support": cross_check.get("title_identity_support"),
                    "title_identity_supported_terms": cross_check.get(
                        "title_identity_supported_terms"
                    ),
                    "difference_level": _identity_difference_level(cross_check),
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            fusion_result = await self._request_structured_profile(
                client,
                provider=provider,
                image_data_url=image_data_url,
                system_prompt=_FUSION_SYSTEM_PROMPT,
                user_text=(
                    "Generate one shared product-family search intent from the representative "
                    "image, shared subject title, representative title, and the explicitly "
                    "separated Seller-title variant parameters. Preserve variant distinctions "
                    "but do not turn one variant value into a family-wide fact. "
                    "Every generated term and search phrase must predict how South African "
                    "local customers shop, in South African English; do not default to generic "
                    "US or UK marketplace wording. "
                    "The isolated observation and cross-validation are evidence, not permission "
                    "to silently overwrite either source. Context JSON: "
                    + fusion_context
                ),
                function_name="submit_takealot_fused_search_profile",
                function_description=(
                    "Return image-title fused South African shopper roots and concise 2-4 word "
                    "search queries."
                ),
                max_tokens=1800,
                profile_type=FusionVisionProfile,
            )
        except (SearchRankingConfigurationError, SearchRankingProviderError) as exc:
            fusion_usage = (
                exc.usage if isinstance(exc, _CountedVisionProviderError) else {}
            )
            fusion_cost = (
                exc.estimated_cost_cny
                if isinstance(exc, _CountedVisionProviderError)
                else 0.0
            )
            raise _CountedVisionProviderError(
                str(exc),
                usage=_sum_usage(visual_result.usage, fusion_usage),
                estimated_cost_cny=round(
                    visual_result.estimated_cost_cny + fusion_cost,
                    10,
                ),
            ) from exc
        return replace(
            fusion_result,
            usage=_sum_usage(visual_result.usage, fusion_result.usage),
            estimated_cost_cny=round(
                visual_result.estimated_cost_cny + fusion_result.estimated_cost_cny,
                10,
            ),
            visual_profile=visual_result.profile,
            fusion_profile=fusion_result.profile,
            cross_validation=dict(cross_check),
            visual_response_id=visual_result.response_id,
        )

    async def _request_structured_profile(
        self,
        client: httpx.AsyncClient,
        *,
        provider: VisionProviderSettings,
        image_data_url: str,
        system_prompt: str,
        user_text: str,
        function_name: str,
        function_description: str,
        max_tokens: int,
        profile_type: type[VisionProfile] = VisionProfile,
    ) -> VisionCallResult:
        payload = {
            "model": provider.model,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": user_text,
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
                        "name": function_name,
                        "description": function_description,
                        "parameters": profile_type.model_json_schema(),
                    },
                }
            ],
            "tool_choice": {
                "type": "function",
                "function": {"name": function_name},
            },
            "max_tokens": max_tokens,
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
                raise SearchRankingProviderError(f"多模态模型返回 HTTP {response.status_code}")
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
                profile = _validated_chat_profile(
                    body,
                    function_name=function_name,
                    profile_type=profile_type,
                )
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


class CodexCliProductVisionClient:
    """Two-stage product vision through a Terra-only local Codex App Server."""

    def __init__(self, settings: SearchRankingRuntimeSettings) -> None:
        self.settings = settings
        self.quota_guard = CodexWeeklyQuotaGuard(settings.codex_quota_path)

    async def preflight_quota(self) -> dict[str, Any]:
        executable = self._executable()
        try:
            async with CodexAppServerClient(
                executable,
                project_root=self.settings.project_root,
                quota_guard=self.quota_guard,
                timeout_seconds=self.settings.request_timeout_seconds,
            ) as client:
                return await client.preflight_quota()
        except CodexCliQuotaExceededError as exc:
            raise SearchRankingQuotaExceededError(
                str(exc),
                usage=dict(exc.usage),
                quota=dict(exc.quota),
            ) from exc
        except CodexCliConfigurationError as exc:
            raise SearchRankingConfigurationError(str(exc)) from exc
        except CodexCliProviderError as exc:
            raise SearchRankingProviderError(str(exc)) from exc

    async def identify(
        self,
        *,
        image_url: str,
        reference_title: str,
        variant_context: Mapping[str, Any] | None = None,
    ) -> VisionCallResult:
        executable = self._executable()
        image_path = await asyncio.to_thread(
            _thumbnail_path,
            self.settings,
            image_url,
        )
        aggregate_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
        visual_result: Any = None
        fusion_result: Any = None
        fusion_normalization: dict[str, Any] = {"applied": False}
        try:
            async with CodexAppServerClient(
                executable,
                project_root=self.settings.project_root,
                quota_guard=self.quota_guard,
                timeout_seconds=self.settings.request_timeout_seconds,
            ) as client:
                visual_result = await client.run_structured_turn(
                    stage="isolated_image_observation",
                    system_prompt=_VISUAL_SYSTEM_PROMPT,
                    user_text=(
                        "Identify the physical product shown in the image. No seller title or SKU "
                        "is supplied at this stage; base the identity only on visible evidence. "
                        "Express every generated term for South African local customers, using "
                        "South African English and locally plausible shopping vocabulary."
                    ),
                    image_path=image_path,
                    output_schema=LocalizedVisionProfile.model_json_schema(),
                )
                aggregate_usage = _sum_usage(aggregate_usage, visual_result.usage)
                try:
                    visual_profile = LocalizedVisionProfile.model_validate(visual_result.payload)
                except ValidationError as exc:
                    validation_errors = _fusion_validation_errors(exc)
                    validation_summary = _fusion_validation_summary(exc)
                    failure_audit = {
                        "stage": "isolated_image_observation",
                        "summary": validation_summary,
                        "validation_errors": validation_errors,
                        "raw_payload": dict(visual_result.payload),
                    }
                    raise _CountedVisionProviderError(
                        "Codex Terra 隔离图片识别结果校验失败："
                        + validation_summary,
                        usage=dict(aggregate_usage),
                        estimated_cost_cny=0.0,
                        provider_attempts=(
                            {
                                "provider": "codex_cli",
                                "model": CODEX_TERRA_MODEL,
                                "status": "local_validation_failed",
                                "stage": "isolated_image_observation",
                                "usage": dict(aggregate_usage),
                                "weekly_quota": dict(visual_result.quota),
                                "validation_errors": validation_errors,
                            },
                        ),
                        failure_audit=failure_audit,
                    ) from exc

                normalized_title = " ".join(reference_title.split())
                _, cross_check = _cross_check_image_profile(
                    visual_profile,
                    normalized_title,
                )
                fusion_context = json.dumps(
                    {
                        "source_title": normalized_title,
                        "variant_family": dict(variant_context or {}),
                        "variant_evidence_policy": {
                            "shared_subject_uses_image_and_titles": True,
                            "variant_parameters_come_from_seller_titles": True,
                            "representative_image_does_not_verify_sibling_variant_values": True,
                            "do_not_apply_one_variant_value_to_the_whole_family": True,
                        },
                        "isolated_visual_observation": visual_profile.model_dump(mode="json"),
                        "independent_cross_validation": {
                            "source_title_similarity": cross_check["source_title_similarity"],
                            "title_identity_support": cross_check.get("title_identity_support"),
                            "title_identity_supported_terms": cross_check.get(
                                "title_identity_supported_terms"
                            ),
                            "difference_level": _identity_difference_level(cross_check),
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                fusion_result = await client.run_structured_turn(
                    stage="image_title_fusion",
                    system_prompt=_FUSION_SYSTEM_PROMPT,
                    user_text=(
                        "Generate one shared product-family search intent from the representative "
                        "image, shared subject title, representative title, and the explicitly "
                        "separated Seller-title variant parameters. Preserve variant distinctions "
                        "but do not turn one variant value into a family-wide fact. Every generated "
                        "term and search phrase must predict how South African local customers shop, "
                        "in South African English. The isolated observation and cross-validation "
                        "are evidence, not permission to silently overwrite either source. "
                        "Return concise natural shopper queries, normally 2-4 words, and place the "
                        "product identity before colour or other variant parameters. Context JSON: "
                        + fusion_context
                    ),
                    image_path=image_path,
                    output_schema=FusionVisionProfile.model_json_schema(),
                )
                aggregate_usage = _sum_usage(aggregate_usage, fusion_result.usage)
                normalized_fusion_payload, fusion_normalization = (
                    _normalize_fusion_payload(fusion_result.payload)
                )
                try:
                    fusion_profile = FusionVisionProfile.model_validate(
                        normalized_fusion_payload
                    )
                except ValidationError as exc:
                    validation_errors = _fusion_validation_errors(exc)
                    validation_summary = _fusion_validation_summary(exc)
                    failure_audit = {
                        "stage": "image_title_fusion",
                        "summary": validation_summary,
                        "validation_errors": validation_errors,
                        "normalization": dict(fusion_normalization),
                        "raw_payload": dict(fusion_result.payload),
                        "normalized_payload": normalized_fusion_payload,
                    }
                    raise _CountedVisionProviderError(
                        "Codex Terra 图片标题融合结果无法安全校正："
                        + validation_summary,
                        usage=dict(aggregate_usage),
                        estimated_cost_cny=0.0,
                        provider_attempts=(
                            {
                                "provider": "codex_cli",
                                "model": CODEX_TERRA_MODEL,
                                "status": "local_validation_failed",
                                "stage": "image_title_fusion",
                                "usage": dict(aggregate_usage),
                                "weekly_quota": dict(fusion_result.quota),
                                "validation_errors": validation_errors,
                                "normalization": dict(fusion_normalization),
                            },
                        ),
                        failure_audit=failure_audit,
                    ) from exc
        except SearchRankingProviderError:
            raise
        except CodexCliQuotaExceededError as exc:
            usage = _sum_usage(aggregate_usage, exc.usage)
            raise SearchRankingQuotaExceededError(
                str(exc),
                usage=usage,
                quota=dict(exc.quota),
            ) from exc
        except CodexCliConfigurationError as exc:
            raise SearchRankingConfigurationError(str(exc)) from exc
        except CodexCliProviderError as exc:
            usage = _sum_usage(aggregate_usage, exc.usage)
            if usage["total_tokens"]:
                failed_stage = (
                    "image_title_fusion"
                    if visual_result is not None
                    else "isolated_image_observation"
                )
                raise _CountedVisionProviderError(
                    str(exc),
                    usage=usage,
                    estimated_cost_cny=0.0,
                    provider_attempts=(
                        {
                            "provider": "codex_cli",
                            "model": CODEX_TERRA_MODEL,
                            "status": "request_or_schema_failed",
                            "stage": failed_stage,
                            "reason": type(exc).__name__,
                            "usage": dict(usage),
                            "weekly_quota": dict(exc.quota),
                        },
                    ),
                ) from exc
            raise SearchRankingProviderError(str(exc)) from exc

        if visual_result is None or fusion_result is None:
            raise SearchRankingProviderError("Codex Terra 识别流程未完整结束")
        return VisionCallResult(
            profile=fusion_profile,
            provider="codex_cli",
            model=CODEX_TERRA_MODEL,
            response_id=fusion_result.turn_id,
            usage=dict(aggregate_usage),
            estimated_cost_cny=0.0,
            provider_attempts=(
                {
                    "provider": "codex_cli",
                    "status": "accepted",
                    "stages": [
                        "isolated_image_observation",
                        "image_title_fusion",
                    ],
                    "model": CODEX_TERRA_MODEL,
                    "model_fallback_allowed": False,
                    "weekly_quota": dict(fusion_result.quota),
                    "usage": dict(aggregate_usage),
                    "estimated_cost_cny": 0.0,
                    "normalization": dict(fusion_normalization),
                },
            ),
            cache_profile=visual_profile,
            visual_profile=visual_profile,
            fusion_profile=fusion_profile,
            cross_validation=dict(cross_check),
            visual_response_id=visual_result.turn_id,
        )

    def _executable(self) -> Path:
        executable = self.settings.codex_cli_path
        if executable is None or not executable.is_file():
            raise SearchRankingConfigurationError(
                "未安装项目锁定的 Codex CLI；搜索定位不会回退到其他模型"
            )
        provider = self.settings.primary_provider
        if provider.name != "codex_cli" or provider.model != CODEX_TERRA_MODEL:
            raise SearchRankingConfigurationError(
                f"搜索定位只允许模型 {CODEX_TERRA_MODEL}"
            )
        return executable


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
    journey_type: str | None = None
    journey_root: str | None = None
    journey_path: tuple[str, ...] = ()
    journey_depth: int = 0
    journey_parent_query: str | None = None
    adaptive_recovery_source: str | None = None


def _eligible_family_offers(
    session: Session,
    selected_offer: OfferCurrent,
    runtime: SearchRankingRuntimeSettings,
) -> list[tuple[OfferCurrent, OfferEligibility]]:
    """Load only currently eligible siblings from the selected store-scoped database."""

    productline_id = str(selected_offer.productline_id or "").strip()
    if productline_id:
        candidates = list(
            session.scalars(
                select(OfferCurrent)
                .where(OfferCurrent.productline_id == productline_id)
                .order_by(OfferCurrent.offer_id)
            )
        )
    else:
        candidates = [selected_offer]
    output: list[tuple[OfferCurrent, OfferEligibility]] = []
    now = _utcnow()
    for candidate in candidates:
        eligibility = _offer_eligibility(candidate, runtime, now=now)
        if eligibility.eligible:
            output.append((candidate, eligibility))
    return output


def _family_representative_offer(
    session: Session,
    family: Sequence[tuple[OfferCurrent, OfferEligibility]],
) -> OfferCurrent:
    """Reuse the newest completed family source when possible, otherwise lowest Offer ID."""

    if not family:
        raise ValueError("eligible family requires at least one Offer")
    offers_by_id = {str(offer.offer_id): offer for offer, _ in family}
    productline_id = str(family[0][0].productline_id or "").strip()
    if productline_id:
        analyses = session.scalars(
            select(SearchRankingAnalysis)
            .where(
                SearchRankingAnalysis.productline_id == productline_id,
                SearchRankingAnalysis.status == "completed",
            )
            .order_by(SearchRankingAnalysis.id.desc())
            .limit(24)
        )
        for analysis in analyses:
            representative = offers_by_id.get(str(analysis.offer_id))
            if representative is not None:
                return representative
    return min((offer for offer, _ in family), key=lambda item: str(item.offer_id))


def _family_profile_from_offers(
    family: Sequence[tuple[OfferCurrent, OfferEligibility]],
    *,
    representative_offer_id: str,
) -> dict[str, Any]:
    eligibility_by_offer = {
        str(offer.offer_id): eligibility for offer, eligibility in family
    }
    return _variant_family_profile(
        [
            {
                "offer_id": offer.offer_id,
                "productline_id": offer.productline_id,
                "sku": offer.sku,
                "title": offer.title,
                "image_url": eligibility_by_offer[str(offer.offer_id)].trusted_image_url,
                "available_stock": eligibility_by_offer[str(offer.offer_id)].available_stock,
            }
            for offer, _ in family
        ],
        representative_offer_id=representative_offer_id,
    )


class SearchRankingService:
    """Persisted store-scoped analysis; ordinary GET requests remain local-only."""

    def __init__(
        self,
        project_root: Path,
        *,
        vision_client_factory: Callable[[SearchRankingRuntimeSettings], VisionClient] | None = None,
        search_client_factory: Callable[[], CompetitorPublicClient] | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.runtime = SearchRankingRuntimeSettings.from_env(self.project_root)
        self.database_url = DashboardSettings.from_env(self.project_root).database_url
        self._vision_client_factory = vision_client_factory or OpenAICompatibleProductVisionClient
        self._search_client_factory = search_client_factory or (
            lambda: CompetitorPublicClient(
                timeout_seconds=45.0,
                search_endpoint_retries=0,
            )
        )
        self._public_request_throttle = _PublicRequestThrottle(
            minimum_interval_seconds=self.runtime.page_delay_seconds,
            jitter_seconds=self.runtime.page_delay_jitter_seconds,
        )
        self._autocomplete_cache = _SharedAutocompleteCache(self.database_url)

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
            "pricing_mode": "api_unit_price",
            "model_policy": {
                "transport": "openai_compatible_https",
                "model_fallback_allowed": fallback is not None,
                "codex_cli_integration_retained": True,
                "codex_cli_execution_enabled": False,
            },
            "max_pages": self.runtime.max_pages,
            "max_keywords": self.runtime.max_keywords,
            "root_expansion_input_limit": ROOT_EXPANSION_INPUT_LIMIT,
            "root_expansion_followup_root_limit": ROOT_EXPANSION_FOLLOWUP_ROOT_LIMIT,
            "root_expansion_phrase_roots_enabled": True,
            "root_expansion_selection_policy": (
                "same_product_identity_or_structured_adjacent_product_family"
            ),
            "root_expansion_raw_suggestions_are_selected": False,
            "root_source_priority": list(ROOT_SOURCE_PRIORITY),
            "model_market_context": MODEL_MARKET_CONTEXT,
            "model_language_variant": MODEL_LANGUAGE_VARIANT,
            "model_shopper_context": MODEL_SHOPPER_CONTEXT,
            "model_localization_policy_version": MODEL_LOCALIZATION_POLICY_VERSION,
            "model_localization_scope": "all_model_generated_text_fields",
            "model_localization_is_measured_demand": False,
            "search_query_attempt_limit": self.runtime.max_keywords,
            "public_request_min_interval_seconds": self.runtime.page_delay_seconds,
            "public_request_jitter_seconds": self.runtime.page_delay_jitter_seconds,
            "public_request_retry_policy": "no_automatic_retry_for_search_endpoints",
            "model_direct_query_policy": {
                "min_words": MODEL_DIRECT_QUERY_MIN_WORDS,
                "max_words": MODEL_DIRECT_QUERY_MAX_WORDS,
                "preferred_max_words": MODEL_DIRECT_QUERY_PREFERRED_MAX_WORDS,
                "min_preferred_count": MODEL_DIRECT_QUERY_MIN_PREFERRED_COUNT,
                "source_priority": ["same_product_lexicon", "fusion_keywords"],
            },
            "query_source_targets": {
                "model_south_african_direct": MODEL_DIRECT_QUERY_TARGET,
                "same_product_lexicon_first": True,
                "takealot_root_expansion": ROOT_EXPANSION_CORE_QUERY_TARGET,
                "seller_title_complete_phrase_max": (
                    SELLER_TITLE_COMPLETE_PHRASE_QUERY_MAX
                ),
                "root_related_core_total": ROOT_EXPANSION_CORE_QUERY_TARGET,
                "adjacent_opportunity": ROOT_EXPANSION_OPPORTUNITY_QUERY_TARGET,
                "adaptive_recovery": ADAPTIVE_RECOVERY_QUERY_TARGET,
            },
            "operation_scope": "manual_single_offer_or_confirmed_serial_batch",
            "offer_max_age_hours": self.runtime.offer_max_age_hours,
            "image_max_dimension": self.runtime.image_max_dimension,
            "organic_page_size": ORGANIC_PAGE_SIZE,
            "columns_per_row": DESKTOP_COLUMNS,
            "core_first_page_threshold": CORE_DEMAND_COMPETITOR_RATIO_FLOOR,
            "core_same_demand_competitor_ratio_floor": (
                CORE_DEMAND_COMPETITOR_RATIO_FLOOR
            ),
            "core_same_demand_competitor_min_results": (
                CORE_DEMAND_COMPETITOR_MIN_RESULTS
            ),
            "core_min_platform_results": CORE_MIN_PLATFORM_RESULTS,
            "platform_result_count_is_search_volume": False,
            "platform_result_count_role": "core_keyword_supply_breadth_gate",
            "semantic_relation_grades": ["S", "A", "C/I"],
            "semantic_relation_source_priority_decides_grade": False,
            "semantic_adjacent_ratio_floor": SEMANTIC_ADJACENT_RATIO_FLOOR,
            "semantic_supported_ratio_floor": SEMANTIC_SUPPORTED_RATIO_FLOOR,
            "semantic_adjacent_min_results": SEMANTIC_ADJACENT_MIN_RESULTS,
            "opportunity_max_direct_competitors": (OPPORTUNITY_MAX_DIRECT_COMPETITORS),
            "opportunity_max_organic_rank": OPPORTUNITY_MAX_ORGANIC_RANK,
            "position_scope": "organic_results_excluding_sponsored",
            "ranking_source": "sections.products.results:type=product_views",
            "passive_reads_are_local_only": True,
            "product_fact_manual_confirmation_available": True,
            "product_fact_profile_requires_current_image": True,
            "autocomplete_cache_shared_across_stores": True,
            "autocomplete_cache_ttl_hours": AUTOCOMPLETE_CACHE_TTL_HOURS,
            "autocomplete_cache_refresh_mode": "refresh_on_first_hit_after_ttl",
            "root_expansion_rank_is_search_volume": False,
            "product_fact_confirmation_mode": "manual_only",
            "decision_parameter_confirmation_mode": "manual_per_title",
            "decision_parameter_max_candidates": DECISION_PARAMETER_MAX_CANDIDATES,
            "decision_parameter_max_positive": DECISION_PARAMETER_MAX_POSITIVE,
            "title_score_version": TITLE_SCORE_VERSION,
            "title_score_scope": (
                "current_title_text_against_frozen_product_and_query_evidence"
            ),
            "title_score_excludes_search_performance": True,
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
                        family_key = (
                            f"plid:{str(analysis.productline_id).strip()}"
                            if str(analysis.productline_id or "").strip()
                            else f"offer:{analysis.offer_id}"
                        )
                        latest_completed.setdefault(family_key, analysis)
                evaluated = [
                    (offer, _offer_eligibility(offer, self.runtime, now=now)) for offer in offers
                ]
                eligible_families: dict[
                    str,
                    list[tuple[OfferCurrent, OfferEligibility]],
                ] = {}
                for offer, eligibility in evaluated:
                    if not eligibility.eligible:
                        continue
                    family_key = (
                        f"plid:{str(offer.productline_id).strip()}"
                        if str(offer.productline_id or "").strip()
                        else f"offer:{offer.offer_id}"
                    )
                    eligible_families.setdefault(family_key, []).append(
                        (offer, eligibility)
                    )
                family_profiles: dict[str, dict[str, Any]] = {}
                for family_key, family in eligible_families.items():
                    latest = latest_completed.get(family_key)
                    representative_offer_id = (
                        str(latest.offer_id)
                        if latest is not None
                        and any(str(offer.offer_id) == str(latest.offer_id) for offer, _ in family)
                        else min(str(offer.offer_id) for offer, _ in family)
                    )
                    family_profiles[family_key] = _family_profile_from_offers(
                        family,
                        representative_offer_id=representative_offer_id,
                    )
                items = []
                for family_key, family in eligible_families.items():
                    family_profile = family_profiles[family_key]
                    latest = latest_completed.get(family_key)
                    for offer, eligibility in family:
                        items.append(
                            _offer_summary(
                                offer,
                                latest,
                                eligibility,
                                family_profile=family_profile,
                            )
                        )
                items.sort(
                    key=lambda item: (
                        str(item.get("shared_family_title") or item.get("title") or "").casefold(),
                        str(item.get("offer_id") or ""),
                    )
                )
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

    def root_expansion_library_payload(
        self,
        *,
        search: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read complete-root expansions without causing a Takealot request."""

        normalized_search = " ".join(search.split()).casefold()
        bounded_limit = max(1, min(limit, 200))
        now = _utcnow()
        engine = create_read_only_engine(self.database_url)
        try:
            with Session(engine) as session:
                rows = list(
                    session.scalars(
                        select(SearchAutocompleteCache)
                        .order_by(SearchAutocompleteCache.captured_at.desc())
                        .limit(1000)
                    )
                )
        finally:
            engine.dispose()

        root_rows: list[dict[str, Any]] = []
        matching_expansions: set[str] = set()
        stale_root_count = 0
        complete_root_count = 0
        for row in rows:
            if not _is_complete_root_expansion_input(row.input_text):
                continue
            complete_root_count += 1
            suggestions = _normalized_autocomplete_suggestions(row.suggestions)
            captured_at = _naive_utc(row.captured_at)
            age_hours = max(0.0, (now - captured_at).total_seconds() / 3600)
            stale = age_hours > AUTOCOMPLETE_CACHE_TTL_HOURS
            stale_root_count += int(stale)
            ranked_suggestions = [
                {"phrase": phrase, "rank": rank} for rank, phrase in enumerate(suggestions, start=1)
            ]
            input_matches = not normalized_search or normalized_search in row.input_text.casefold()
            suggestion_matches = any(
                normalized_search in str(item["phrase"]).casefold() for item in ranked_suggestions
            )
            if not (input_matches or suggestion_matches):
                continue
            root_rows.append(
                {
                    "root": row.input_text,
                    "expansions": ranked_suggestions,
                    "captured_at": captured_at.isoformat(),
                    "age_hours": round(age_hours, 2),
                    "stale": stale,
                    "last_hit_at": _naive_utc(row.last_hit_at).isoformat(),
                    "system_input_hit_count": row.hit_count,
                    "refresh_count": row.refresh_count,
                    "last_refresh_status": row.last_refresh_status,
                    "last_error": row.last_error,
                }
            )
            matching_expansions.update(
                str(item["phrase"]).casefold() for item in ranked_suggestions
            )
        return {
            "policy": {
                "scope": "shared_across_all_store_analyses",
                "ttl_hours": AUTOCOMPLETE_CACHE_TTL_HOURS,
                "refresh_mode": "refresh_on_first_hit_after_ttl",
                "scheduled_refresh": False,
                "passive_read_triggers_external_request": False,
                "root_expansion_rank_is_search_volume": False,
                "legacy_partial_input_states_hidden": True,
                "phrase_roots_supported": True,
                "raw_expansions_require_product_context_selection": True,
                "note": (
                    "每个完整词根或词组下的第1至5项是平台原始扩展，不代表系统已为某件商品选中；"
                    "分析时还会按同品身份或结构化相邻商品族逐项筛选，相关扩展可继续作为词组词根扩展。"
                    "不同词根不合并排名，它不是公开搜索量，系统命中次数也不是买家搜索次数。"
                ),
            },
            "summary": {
                "root_count": complete_root_count,
                "stale_root_count": stale_root_count,
                "matching_root_count": len(root_rows),
                "matching_expansion_count": len(matching_expansions),
                "legacy_partial_input_state_count": len(rows) - complete_root_count,
            },
            "roots": root_rows[:bounded_limit],
        }

    def autocomplete_library_payload(
        self,
        *,
        search: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Backward-compatible alias for clients using the former route name."""

        return self.root_expansion_library_payload(search=search, limit=limit)

    def detail_payload(self, offer_id: str) -> dict[str, Any] | None:
        engine = create_read_only_engine(self.database_url)
        try:
            with Session(engine) as session:
                offer = session.scalar(
                    select(OfferCurrent).where(OfferCurrent.offer_id == offer_id)
                )
                if offer is None:
                    return None
                eligibility = _offer_eligibility(offer, self.runtime)
                if not eligibility.eligible:
                    return None
                family = _eligible_family_offers(session, offer, self.runtime)
                if not family:
                    return None
                representative = _family_representative_offer(session, family)
                family_profile = _family_profile_from_offers(
                    family,
                    representative_offer_id=str(representative.offer_id),
                )
                productline_id = str(offer.productline_id or "").strip()
                product_facts = list(
                    session.scalars(
                        select(SearchRankingProductFact)
                        .where(
                            SearchRankingProductFact.productline_id
                            == productline_id,
                            SearchRankingProductFact.source_type == "manual_confirmation",
                        )
                        .order_by(SearchRankingProductFact.id.desc())
                        .limit(100)
                    )
                )
                decision_parameter_confirmations = list(
                    session.scalars(
                        select(SearchRankingDecisionParameterConfirmation)
                        .where(
                            SearchRankingDecisionParameterConfirmation.productline_id
                            == productline_id
                        )
                        .order_by(SearchRankingDecisionParameterConfirmation.id.desc())
                        .limit(20)
                    )
                )
                analysis_scope = (
                    SearchRankingAnalysis.productline_id == productline_id
                    if productline_id
                    else SearchRankingAnalysis.offer_id == offer_id
                )
                analyses = list(
                    session.scalars(
                        select(SearchRankingAnalysis)
                        .where(analysis_scope)
                        .order_by(SearchRankingAnalysis.id.desc())
                        .limit(12)
                    )
                )
                latest_attempt = analyses[0] if analyses else None
                latest = session.scalar(
                    select(SearchRankingAnalysis)
                    .where(
                        analysis_scope,
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
                current_title = " ".join(str(offer.title or "").split())
                decision_parameter_profile = _decision_parameter_profile_payload(
                    decision_parameter_confirmations,
                    current_title=current_title,
                )
                payload = {
                    "status": self.status_payload(),
                    "product": _offer_summary(
                        offer,
                        latest,
                        eligibility,
                        family_profile=family_profile,
                    ),
                    "variant_family": family_profile,
                    "product_fact_profile": _product_fact_profile_payload(
                        product_facts,
                        current_image_url=str(eligibility.trusted_image_url or ""),
                    ),
                    "decision_parameter_profile": decision_parameter_profile,
                    "analysis": (
                        _analysis_payload(
                            latest,
                            results,
                            current_offer_id=str(offer.offer_id),
                            current_title=current_title,
                            current_image_url=str(eligibility.trusted_image_url or ""),
                            current_decision_parameter_profile=(
                                decision_parameter_profile
                            ),
                            current_variant_family=family_profile,
                        )
                        if latest
                        else None
                    ),
                    "latest_attempt": (
                        _analysis_history_item(
                            latest_attempt,
                            current_offer_id=str(offer.offer_id),
                            current_title=current_title,
                        )
                        if latest_attempt is not None
                        and (latest is None or latest_attempt.id != latest.id)
                        else None
                    ),
                    "history": [
                        _analysis_history_item(
                            item,
                            current_offer_id=str(offer.offer_id),
                            current_title=current_title,
                        )
                        for item in analyses
                    ],
                }
        finally:
            engine.dispose()
        return payload

    def confirm_decision_parameters(
        self,
        offer_id: str,
        confirmation: DecisionParameterConfirmation,
    ) -> dict[str, Any]:
        """Append one complete human classification for the current Seller title."""

        engine = create_engine_for_database_url(self.database_url)
        try:
            with Session(engine) as session, session.begin():
                offer = session.scalar(
                    select(OfferCurrent).where(OfferCurrent.offer_id == offer_id)
                )
                if offer is None:
                    raise SearchRankingInputError("没有找到对应的店铺商品")
                eligibility = _offer_eligibility(offer, self.runtime)
                _raise_if_ineligible(eligibility)
                title = " ".join(str(offer.title or "").split())
                candidates = _title_parameter_candidates(title)
                expected_keys = {str(item["parameter_key"]) for item in candidates}
                choices_by_key: dict[str, bool] = {}
                for choice in confirmation.choices:
                    key = " ".join(choice.parameter_key.split()).casefold()
                    if not key or key in choices_by_key:
                        raise SearchRankingInputError("决策参数确认中存在空值或重复参数")
                    choices_by_key[key] = bool(choice.is_decision_parameter)
                if set(choices_by_key) != expected_keys:
                    raise SearchRankingInputError(
                        "当前标题参数已经变化，请刷新页面后逐项重新确认"
                    )
                positive_count = sum(choices_by_key.values())
                if positive_count > DECISION_PARAMETER_MAX_POSITIVE:
                    raise SearchRankingInputError(
                        f"一个标题最多确认{DECISION_PARAMETER_MAX_POSITIVE}项决策参数，"
                        "否则关键词前置会失去主次"
                    )
                latest_completed = session.scalar(
                    select(SearchRankingAnalysis)
                    .where(
                        SearchRankingAnalysis.productline_id
                        == str(offer.productline_id or "").strip(),
                        SearchRankingAnalysis.status == "completed",
                    )
                    .order_by(SearchRankingAnalysis.id.desc())
                    .limit(1)
                )
                decisions = [
                    {
                        **item,
                        "is_decision_parameter": choices_by_key[str(item["parameter_key"])],
                    }
                    for item in candidates
                ]
                session.add(
                    SearchRankingDecisionParameterConfirmation(
                        productline_id=str(offer.productline_id or "").strip(),
                        source_offer_id=offer_id,
                        source_analysis_id=(latest_completed.id if latest_completed else None),
                        source_title=title,
                        decisions=decisions,
                        policy_version=DECISION_PARAMETER_POLICY_VERSION,
                        confirmed_by_username=confirmation.actor_username,
                        confirmed_by_display_name=confirmation.actor_display_name,
                        confirmed_at=_utcnow(),
                    )
                )
        finally:
            engine.dispose()
        detail = self.detail_payload(offer_id)
        if detail is None:
            raise RuntimeError("决策参数保存后无法读取当前商品")
        return detail

    async def confirm_product_facts(
        self,
        offer_id: str,
        confirmation: ProductFactConfirmation,
    ) -> dict[str, Any]:
        """Persist operator-confirmed facts, then re-run the bounded ranking workflow."""

        normalized_facts = _validated_product_fact_inputs(confirmation.facts)
        source_offer_id = offer_id
        engine = create_engine_for_database_url(self.database_url)
        try:
            with Session(engine) as session, session.begin():
                offer = session.scalar(
                    select(OfferCurrent).where(OfferCurrent.offer_id == offer_id)
                )
                if offer is None:
                    raise SearchRankingInputError("没有找到对应的店铺商品")
                eligibility = _offer_eligibility(offer, self.runtime)
                _raise_if_ineligible(eligibility)
                plid = str(offer.productline_id or "").strip()
                source_analysis = session.get(
                    SearchRankingAnalysis,
                    confirmation.source_analysis_id,
                )
                latest_completed = session.scalar(
                    select(SearchRankingAnalysis)
                    .where(
                        SearchRankingAnalysis.productline_id == plid,
                        SearchRankingAnalysis.status == "completed",
                    )
                    .order_by(SearchRankingAnalysis.id.desc())
                    .limit(1)
                )
                if (
                    source_analysis is None
                    or str(source_analysis.productline_id or "").strip() != plid
                    or source_analysis.status != "completed"
                    or latest_completed is None
                    or latest_completed.id != source_analysis.id
                ):
                    raise SearchRankingInputError(
                        "商品事实确认对应的分析已不是最新结果，请刷新后重新确认"
                    )
                source_offer = session.scalar(
                    select(OfferCurrent).where(
                        OfferCurrent.offer_id == source_analysis.offer_id
                    )
                )
                if source_offer is None:
                    raise SearchRankingInputError(
                        "商品族代表 Offer 已不在当前 Seller Offers 中，请先重新分析"
                    )
                source_eligibility = _offer_eligibility(source_offer, self.runtime)
                _raise_if_ineligible(source_eligibility)
                source_offer_id = str(source_offer.offer_id)
                title = " ".join(str(source_offer.title or "").split())
                image_url = str(source_eligibility.trusted_image_url or "")
                if (
                    " ".join(source_analysis.source_title.split()) != title
                    or source_analysis.source_image_url != image_url
                ):
                    raise SearchRankingInputError(
                        "商品标题或主图已变化，请先重新识别定位后再确认商品事实"
                    )
                source_results = list(
                    session.scalars(
                        select(SearchRankingKeywordResult)
                        .where(SearchRankingKeywordResult.analysis_id == source_analysis.id)
                        .order_by(SearchRankingKeywordResult.candidate_order)
                    )
                )
                recommendation = _product_fact_recommendation_from_analysis(
                    source_analysis,
                    source_results,
                )
                if confirmation.reason_code != recommendation["reason_code"]:
                    raise SearchRankingInputError("商品事实补证原因已变化，请刷新页面后重新确认")
                # The recommendation is advisory. An operator may add or update
                # auditable facts even when the model did not identify a gap,
                # provided the latest analysis, title, image and acknowledgements
                # still match this request.
                confirmation_basis = (
                    "system_recommended_gap"
                    if recommendation["recommended"]
                    else "operator_initiated_optional_confirmation"
                )
                now = _utcnow()
                for fact in normalized_facts:
                    existing = list(
                        session.scalars(
                            select(SearchRankingProductFact).where(
                                SearchRankingProductFact.productline_id == plid,
                                SearchRankingProductFact.normalized_term
                                == fact.fact_term.casefold(),
                                SearchRankingProductFact.status == "active",
                            )
                        )
                    )
                    if any(
                        item.source_type == "manual_confirmation"
                        and item.source_analysis_id == source_analysis.id
                        and item.source_image_url == image_url
                        for item in existing
                    ):
                        continue
                    for item in existing:
                        item.status = "superseded"
                        item.revoked_by_username = confirmation.actor_username
                        item.revoked_by_display_name = confirmation.actor_display_name
                        item.revoked_at = now
                        item.revoke_reason = "由新的人工确认商品事实版本替代"
                    session.add(
                        SearchRankingProductFact(
                            productline_id=plid,
                            source_offer_id=source_offer_id,
                            fact_type=fact.fact_type,
                            fact_term=fact.fact_term,
                            normalized_term=fact.fact_term.casefold(),
                            statement=fact.statement or fact.fact_term,
                            status="active",
                            source_type="manual_confirmation",
                            source_analysis_id=source_analysis.id,
                            source_title=title,
                            source_image_url=image_url,
                            evidence={
                                "reason_code": confirmation.reason_code,
                                "reason": recommendation["reason"],
                                "operator_assertion": True,
                                "confirmation_basis": confirmation_basis,
                            },
                            confirmed_by_username=confirmation.actor_username,
                            confirmed_by_display_name=(confirmation.actor_display_name),
                            confirmed_at=now,
                        )
                    )
        finally:
            engine.dispose()
        await self.analyze_offer(source_offer_id)
        detail = self.detail_payload(offer_id)
        if detail is None:
            raise RuntimeError("商品事实保存后无法读取当前变体")
        return detail

    def revoke_product_fact(
        self,
        offer_id: str,
        fact_id: int,
        revocation: ProductFactRevocation,
    ) -> dict[str, Any]:
        """Stop applying one fact while retaining the complete confirmation record."""

        reason = " ".join(revocation.reason.split())
        if len(reason) < 2 or len(reason) > 500:
            raise SearchRankingInputError("停用原因必须为2到500个字符")
        engine = create_engine_for_database_url(self.database_url)
        try:
            with Session(engine) as session, session.begin():
                offer = session.scalar(
                    select(OfferCurrent).where(OfferCurrent.offer_id == offer_id)
                )
                if offer is None:
                    raise SearchRankingInputError("没有找到对应的店铺商品")
                fact = session.scalar(
                    select(SearchRankingProductFact).where(SearchRankingProductFact.id == fact_id)
                )
                if fact is None or fact.productline_id != str(offer.productline_id or "").strip():
                    raise SearchRankingInputError("没有找到对应的商品事实记录")
                if fact.source_type != "manual_confirmation":
                    raise SearchRankingInputError("历史非人工事实已停用，不能从当前人工路径操作")
                if fact.status != "active":
                    raise SearchRankingInputError("该商品事实已经不是启用状态")
                fact.status = "revoked"
                fact.revoked_by_username = revocation.actor_username
                fact.revoked_by_display_name = revocation.actor_display_name
                fact.revoked_at = _utcnow()
                fact.revoke_reason = reason
        finally:
            engine.dispose()
        detail = self.detail_payload(offer_id)
        if detail is None:
            raise SearchRankingInputError("商品事实已停用，但当前商品不再满足分析条件")
        return detail

    async def analyze_offer(self, offer_id: str) -> dict[str, Any]:
        requested_offer_id = offer_id
        engine = create_engine_for_database_url(self.database_url)
        analysis_id: int | None = None
        product_fact_records: list[dict[str, Any]] = []
        applied_product_fact_terms: list[str] = []
        applied_identity_fact_terms: list[str] = []
        decision_parameter_profile: dict[str, Any] = {}
        applied_decision_parameter_values: list[str] = []
        family_profile: dict[str, Any] = {}
        family_cache_material = ""
        variant_contexts: list[dict[str, Any]] = []
        try:
            with Session(engine) as session, session.begin():
                offer = session.scalar(
                    select(OfferCurrent).where(OfferCurrent.offer_id == offer_id)
                )
                if offer is None:
                    raise SearchRankingInputError("没有找到对应的店铺商品")
                eligibility = _offer_eligibility(offer, self.runtime)
                _raise_if_ineligible(eligibility)
                family = _eligible_family_offers(session, offer, self.runtime)
                if not family:
                    raise SearchRankingInputError("当前商品族没有可分析的有效 Offer")
                offer = _family_representative_offer(session, family)
                offer_id = str(offer.offer_id)
                eligibility = next(
                    item_eligibility
                    for item_offer, item_eligibility in family
                    if str(item_offer.offer_id) == offer_id
                )
                family_profile = _family_profile_from_offers(
                    family,
                    representative_offer_id=offer_id,
                )
                family_cache_material = _variant_family_cache_material(family_profile)
                title = " ".join(str(offer.title or "").split())
                image_url = str(eligibility.trusted_image_url or "")
                plid = str(offer.productline_id or "").strip()
                product_fact_models = list(
                    session.scalars(
                        select(SearchRankingProductFact)
                        .where(
                            SearchRankingProductFact.productline_id == plid,
                            SearchRankingProductFact.source_type == "manual_confirmation",
                        )
                        .order_by(SearchRankingProductFact.id.desc())
                        .limit(100)
                    )
                )
                product_fact_records = [
                    _product_fact_record_payload(
                        item,
                        current_image_url=image_url,
                    )
                    for item in product_fact_models
                ]
                applied_product_fact_terms = list(
                    dict.fromkeys(
                        str(item["fact_term"])
                        for item in product_fact_records
                        if item["applied_to_current_image"]
                    )
                )
                applied_identity_fact_terms = list(
                    dict.fromkeys(
                        str(item["fact_term"])
                        for item in product_fact_records
                        if item["applied_to_current_image"] and item["fact_type"] == "product_type"
                    )
                )
                decision_parameter_models = list(
                    session.scalars(
                        select(SearchRankingDecisionParameterConfirmation)
                        .where(
                            SearchRankingDecisionParameterConfirmation.productline_id == plid
                        )
                        .order_by(SearchRankingDecisionParameterConfirmation.id.desc())
                        .limit(20)
                    )
                )
                decision_parameter_profile = _decision_parameter_profile_payload(
                    decision_parameter_models,
                    current_title=title,
                )
                applied_decision_parameter_values = list(
                    decision_parameter_profile["applied_decision_values"]
                )
                for family_offer, family_eligibility in family:
                    family_image_url = str(
                        family_eligibility.trusted_image_url or ""
                    )
                    family_fact_records = [
                        _product_fact_record_payload(
                            item,
                            current_image_url=family_image_url,
                        )
                        for item in product_fact_models
                    ]
                    family_fact_terms = list(
                        dict.fromkeys(
                            str(item["fact_term"])
                            for item in family_fact_records
                            if item["applied_to_current_image"]
                        )
                    )
                    family_identity_fact_terms = list(
                        dict.fromkeys(
                            str(item["fact_term"])
                            for item in family_fact_records
                            if item["applied_to_current_image"]
                            and item["fact_type"] == "product_type"
                        )
                    )
                    family_title = " ".join(str(family_offer.title or "").split())
                    variant_contexts.append(
                        {
                            "offer_id": str(family_offer.offer_id),
                            "title": family_title,
                            "image_url": family_image_url,
                            "applied_product_fact_records": [
                                item
                                for item in family_fact_records
                                if item["applied_to_current_image"]
                            ],
                            "applied_product_fact_terms": family_fact_terms,
                            "applied_identity_fact_terms": family_identity_fact_terms,
                            "decision_parameter_profile": (
                                _decision_parameter_profile_payload(
                                    decision_parameter_models,
                                    current_title=family_title,
                                )
                            ),
                        }
                    )
                previous = _previous_analysis_snapshot(
                    session,
                    offer_id,
                    current_title=title,
                )
                cache_key = _analysis_cache_key(
                    image_url=image_url,
                    provider_signature=self.runtime.provider_signature,
                    source_title=family_cache_material,
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
                            and item.vision_payload.get("vision_stage_completed") is True
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
                        "fusion_profile",
                        cached_payload.get(
                            "model_profile",
                            cached_payload.get("profile", cached_payload),
                        ),
                    )
                )
                visual_profile = VisionProfile.model_validate(
                    cached_payload.get(
                        "visual_profile",
                        cached_payload.get("profile", cached_payload),
                    )
                )
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
                # Historical payloads may contain fields from retired features;
                # new analyses expose and apply manual product facts only.
                vision_payload.pop("reverse_image_search", None)
                used_provider = cached_provider or primary.name
                used_model = cached_model or primary.model
            else:
                call = await self._vision_client_factory(self.runtime).identify(
                    image_url=image_url,
                    reference_title=title,
                    variant_context=family_profile,
                )
                model_profile = call.fusion_profile or call.profile
                visual_profile = call.visual_profile or call.cache_profile or call.profile
                used_provider = call.provider
                used_model = call.model
                vision_payload = {
                    "model_profile": model_profile.model_dump(mode="json"),
                    "visual_profile": visual_profile.model_dump(mode="json"),
                    "fusion_profile": model_profile.model_dump(mode="json"),
                    "usage": call.usage,
                    "response_id": call.response_id,
                    "visual_response_id": call.visual_response_id,
                    "estimated_cost_cny": call.estimated_cost_cny,
                    "provider_attempts": list(call.provider_attempts),
                    "pricing_snapshot_date": PRICING_SNAPSHOT_DATE,
                }

            vision_payload["variant_family"] = family_profile
            vision_payload["model_localization"] = {
                "market_context": MODEL_MARKET_CONTEXT,
                "language_variant": MODEL_LANGUAGE_VARIANT,
                "shopper_context": MODEL_SHOPPER_CONTEXT,
                "policy_version": MODEL_LOCALIZATION_POLICY_VERSION,
                "scope": "all_model_generated_text_fields",
                "is_measured_demand": False,
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
                staged_analysis.confidence = Decimal(str(model_profile.confidence))
                staged_analysis.vision_payload = dict(vision_payload)

            applied_product_fact_records = [
                item for item in product_fact_records if item["applied_to_current_image"]
            ]
            profile = _enrich_profile_with_confirmed_facts(
                model_profile,
                applied_product_fact_records,
            )
            same_product_lexicon = _same_product_lexicon(
                profile,
                model_profile=model_profile,
                confirmed_fact_records=applied_product_fact_records,
                source_title=title,
            )
            vision_payload["product_fact_profile"] = {
                "applied_terms": list(applied_product_fact_terms),
                "facts": product_fact_records,
                "requires_current_image_match": True,
                "source_policy": "manual_confirmation_only",
            }
            vision_payload["same_product_lexicon"] = same_product_lexicon
            vision_payload["decision_parameter_profile"] = decision_parameter_profile

            _, recognition = _cross_check_image_profile(
                visual_profile,
                title,
                confirmed_fact_terms=applied_product_fact_terms,
            )
            identity_fact_check = _confirmed_identity_fact_cross_check(
                visual_profile,
                applied_identity_fact_terms,
            )
            recognition.update(identity_fact_check)
            difference_level = _identity_difference_level(recognition)
            title_identity_conflict = bool(
                float(recognition["source_title_similarity"]) < IDENTITY_TITLE_SIMILARITY_FLOOR
                and not recognition.get("title_identity_support")
            )
            fact_resolved_title_conflict = bool(
                title_identity_conflict and identity_fact_check["confirmed_identity_fact_support"]
            )
            recognition["title_identity_conflict"] = title_identity_conflict
            recognition["confirmed_fact_resolved_title_conflict"] = fact_resolved_title_conflict
            recognition["provider_identity_reference_included_confirmed_facts"] = bool(
                applied_identity_fact_terms
            )
            recognition["cross_validation_isolated"] = True
            recognition["cross_validation_completed_before_fusion_generation"] = True
            recognition["image_evidence_scope"] = "representative_offer_only"
            recognition["variant_parameter_source"] = "current_seller_offer_titles"
            recognition["variant_parameters_visually_verified"] = False
            recognition["family_variant_count"] = int(
                family_profile.get("variant_count") or 1
            )
            recognition["identity_difference_level"] = difference_level
            recognition["identity_large_difference"] = difference_level == "high"
            recognition["identity_difference_warning"] = (
                "独立图片观察与当前主标题差异较大；本轮仍按图文融合生成，建议运营抽查商品事实。"
                if difference_level == "high"
                else (
                    "独立图片观察与当前主标题存在部分差异；已保留两份证据供复核。"
                    if difference_level == "moderate"
                    else None
                )
            )
            recognition["identity_deviation_branch"] = (
                "confirmed_fact_support_continue"
                if fact_resolved_title_conflict
                else (
                    "large_difference_warning"
                    if difference_level == "high"
                    else (
                        "moderate_difference_warning"
                        if difference_level == "moderate"
                        else "title_consistent"
                    )
                )
            )
            manual_fact_requirement = _manual_fact_requirement(
                profile,
                confirmed_fact_terms=applied_product_fact_terms,
            )
            recognition.update(manual_fact_requirement)
            if manual_fact_requirement["manual_fact_resolved_by_confirmation"]:
                profile = profile.model_copy(
                    update={
                        "requires_human_fact_confirmation": False,
                        "manual_fact_reason": "",
                        "missing_facts": [],
                    }
                )
            evidence_source_title = _title_evidence_source(
                title,
                applied_product_fact_terms,
            )
            recognition["product_fact_profile_applied"] = bool(applied_product_fact_terms)
            recognition["product_fact_supported_terms"] = list(applied_product_fact_terms)
            observations: list[KeywordObservation] = []
            autocomplete_checks: list[dict[str, Any]] = []
            shopper_journey: dict[str, Any] = {
                "mode": "manual_single_offer_one_click",
                "target_scope": "one_shared_chain_per_store_productline_id",
                "variant_count": int(family_profile.get("variant_count") or 1),
                "variant_parameter_source": "current_seller_offer_titles",
                "root_expansion_input_limit": ROOT_EXPANSION_INPUT_LIMIT,
                "root_expansion_followup_root_limit": ROOT_EXPANSION_FOLLOWUP_ROOT_LIMIT,
                "root_expansion_phrase_roots_enabled": True,
                "root_expansion_selection_policy": (
                    "same_product_identity_or_structured_adjacent_product_family"
                ),
                "root_expansion_raw_suggestions_are_selected": False,
                "root_source_priority": list(ROOT_SOURCE_PRIORITY),
                "model_localization": {
                    "market_context": MODEL_MARKET_CONTEXT,
                    "language_variant": MODEL_LANGUAGE_VARIANT,
                    "shopper_context": MODEL_SHOPPER_CONTEXT,
                    "policy_version": MODEL_LOCALIZATION_POLICY_VERSION,
                    "scope": "all_model_generated_text_fields",
                    "is_measured_demand": False,
                },
                "search_query_attempt_limit": self.runtime.max_keywords,
                "public_request_min_interval_seconds": self.runtime.page_delay_seconds,
                "public_request_jitter_seconds": (self.runtime.page_delay_jitter_seconds),
                "model_direct_query_policy": {
                    "min_words": MODEL_DIRECT_QUERY_MIN_WORDS,
                    "max_words": MODEL_DIRECT_QUERY_MAX_WORDS,
                    "preferred_max_words": MODEL_DIRECT_QUERY_PREFERRED_MAX_WORDS,
                    "min_preferred_count": MODEL_DIRECT_QUERY_MIN_PREFERRED_COUNT,
                    "source_priority": ["same_product_lexicon", "fusion_keywords"],
                },
                "query_source_targets": {
                    "model_south_african_direct": MODEL_DIRECT_QUERY_TARGET,
                    "same_product_lexicon_first": True,
                    "takealot_root_expansion": ROOT_EXPANSION_CORE_QUERY_TARGET,
                    "seller_title_complete_phrase_max": (
                        SELLER_TITLE_COMPLETE_PHRASE_QUERY_MAX
                    ),
                    "root_related_core_total": ROOT_EXPANSION_CORE_QUERY_TARGET,
                    "adjacent_opportunity": ROOT_EXPANSION_OPPORTUNITY_QUERY_TARGET,
                    "adaptive_recovery": ADAPTIVE_RECOVERY_QUERY_TARGET,
                },
                "same_product_lexicon": {
                    "policy_version": SAME_PRODUCT_LEXICON_POLICY_VERSION,
                    "entry_count": len(same_product_lexicon["entries"]),
                    "direct_query_priority": True,
                    "complete_root_expansion_enabled": True,
                    "complete_root_expansion_limit": SAME_PRODUCT_LEXICON_ROOT_LIMIT,
                },
                "adaptive_policy": {
                    "base_query_target": ADAPTIVE_BASE_QUERY_TARGET,
                    "recovery_query_target": ADAPTIVE_RECOVERY_QUERY_TARGET,
                    "valid_platform_root_target": (ADAPTIVE_VALID_PLATFORM_ROOT_TARGET),
                    "recovery_priority": [
                        "result_page_learning",
                        "second_best_root_expansion",
                    ],
                },
                "steps": [],
            }
            if recognition.get("manual_fact_required"):
                shopper_journey.update(
                    {
                        "valid_platform_root_target": ADAPTIVE_VALID_PLATFORM_ROOT_TARGET,
                        "valid_platform_root_count": 0,
                        "valid_platform_roots": [],
                        "adaptive_recovery_used": False,
                        "adaptive_recovery_query": None,
                        "adaptive_recovery_source": None,
                        "adaptive_recovery_skipped_reason": "manual_fact_confirmation_required",
                        "public_request_count": 0,
                        "skipped_for_manual_fact": True,
                        "manual_fact_reason": recognition.get("manual_fact_reason"),
                        "missing_facts": list(recognition.get("missing_facts") or []),
                    }
                )
            elif profile.confidence < self.runtime.confidence_threshold:
                shopper_journey.update(
                    {
                        "valid_platform_root_target": (ADAPTIVE_VALID_PLATFORM_ROOT_TARGET),
                        "valid_platform_root_count": 0,
                        "valid_platform_roots": [],
                        "adaptive_recovery_used": False,
                        "adaptive_recovery_query": None,
                        "adaptive_recovery_source": None,
                        "adaptive_recovery_skipped_reason": (
                            "model_confidence_below_search_threshold"
                        ),
                    }
                )
                candidates = _precise_candidates(
                    profile,
                    source_title=evidence_source_title,
                    same_product_lexicon=same_product_lexicon,
                )[: self.runtime.max_keywords]
                for order, candidate in enumerate(candidates, start=1):
                    observations.append(
                        _low_confidence_observation(
                            candidate,
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
                    expected_family_cache_material=family_cache_material,
                )
                async with self._search_client_factory() as search_client:
                    paced_search_client = _PacedSearchClient(
                        search_client,
                        minimum_interval_seconds=self.runtime.page_delay_seconds,
                        throttle=self._public_request_throttle,
                        autocomplete_cache=self._autocomplete_cache,
                    )
                    candidates, autocomplete_checks = await _discover_keyword_candidates(
                        paced_search_client,
                        profile=profile,
                        source_title=evidence_source_title,
                        official_title=title,
                        title_reference_terms=recognition["title_reference_terms"],
                        confirmed_fact_records=[
                            item
                            for item in product_fact_records
                            if item["applied_to_current_image"]
                        ],
                        same_product_lexicon=same_product_lexicon,
                        model_autocomplete_seeds=model_profile.autocomplete_seeds,
                        model_opportunity_seeds=model_profile.opportunity_seeds,
                        decision_parameter_values=applied_decision_parameter_values,
                        max_keywords=self.runtime.max_keywords,
                    )
                    candidates = _inject_comparison_resample_candidates(
                        candidates,
                        previous=previous,
                        current_title=title,
                        max_keywords=self.runtime.max_keywords,
                    )
                    (
                        observations,
                        journey_steps,
                        adaptive_summary,
                    ) = await _collect_shopper_journey(
                        paced_search_client,
                        candidates=candidates,
                        autocomplete_checks=autocomplete_checks,
                        target_plid=plid,
                        profile=profile,
                        max_pages=self.runtime.max_pages,
                        max_keywords=self.runtime.max_keywords,
                        relevance_threshold=self.runtime.relevance_threshold,
                        source_title=evidence_source_title,
                    )
                    shopper_journey["steps"] = journey_steps
                    shopper_journey.update(adaptive_summary)
                    shopper_journey["public_request_count"] = paced_search_client.request_count

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
                ) = _title_strategy_keywords(
                    observations,
                    evidence_source_title,
                )
                title_reason = _title_suggestion_reason(
                    accepted_title_keywords,
                    validated_keyword_count=sum(
                        item.relevance_status == "accepted" for item in observations
                    ),
                )
                title_strategies = _build_title_strategies(
                    source_title=title,
                    evidence_source_title=evidence_source_title,
                    accepted_keywords=accepted_title_keywords,
                    hot_term_keywords=hot_term_title_keywords,
                    opportunity_keywords=opportunity_title_keywords,
                    validated_core_keywords=accepted_title_keywords,
                    decision_parameter_values=applied_decision_parameter_values,
                    keyword_journey_evidence=_title_keyword_journey_evidence(
                        observations,
                        source_title=evidence_source_title,
                    ),
                )
                title_suggestion = str(
                    title_strategies[0]["title"]
                    or _build_title_suggestion(title, accepted_title_keywords)
                )
                opportunity_title_suggestion = title_strategies[2]["title"]
                profile_payload = profile.model_dump(mode="json")
                profile_payload["confirmed_product_fact_terms"] = list(applied_product_fact_terms)
                profile_payload["same_product_lexicon"] = same_product_lexicon
                profile_payload["title_suggestion"] = title_suggestion
                profile_payload["title_reason"] = title_reason
                profile_payload["title_strategies"] = title_strategies
                profile_payload["opportunity_title_suggestion"] = opportunity_title_suggestion
                profile_payload["opportunity_title_reason"] = (
                    _opportunity_title_reason(opportunity_title_keywords)
                    if opportunity_title_suggestion
                    else None
                )
                vision_payload["model_profile"] = model_profile.model_dump(mode="json")
                vision_payload["visual_profile"] = visual_profile.model_dump(mode="json")
                vision_payload["fusion_profile"] = model_profile.model_dump(mode="json")
                vision_payload["profile"] = profile_payload
                vision_payload["recognition"] = recognition
                vision_payload["autocomplete_checks"] = autocomplete_checks
                vision_payload["root_expansion_checks"] = autocomplete_checks
                vision_payload["shopper_journey"] = shopper_journey
                vision_payload["title_score"] = _title_score_payload(
                    source_title=title,
                    profile=profile,
                    recognition=recognition,
                    observations=observations,
                    confirmed_fact_terms=applied_product_fact_terms,
                )
                vision_payload["variant_reviews"] = _variant_reviews_payload(
                    family_profile=family_profile,
                    variant_contexts=variant_contexts,
                    representative_image_url=image_url,
                    model_profile=model_profile,
                    visual_profile=visual_profile,
                    observations=observations,
                )
                vision_payload["product_fact_recommendation"] = (
                    _product_fact_recommendation(
                        source_analysis_id=persisted_analysis.id,
                        profile_confidence=profile.confidence,
                        recognition=recognition,
                        observations=observations,
                    )
                )
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
            detail = self.detail_payload(requested_offer_id)
            if detail is None:
                raise RuntimeError("搜索定位结果保存后无法读取")
            return detail
        except Exception as exc:
            if analysis_id is not None:
                with Session(engine) as session, session.begin():
                    failed_analysis = session.get(SearchRankingAnalysis, analysis_id)
                    if failed_analysis is not None:
                        if isinstance(
                            exc,
                            (
                                _CountedVisionProviderError,
                                _VisionAttemptsExhaustedError,
                                SearchRankingQuotaExceededError,
                            ),
                        ):
                            failed_analysis.vision_payload = {
                                "vision_stage_completed": False,
                                "usage": dict(exc.usage),
                                "estimated_cost_cny": exc.estimated_cost_cny,
                                "provider_attempts": [dict(item) for item in exc.provider_attempts],
                                "pricing_snapshot_date": PRICING_SNAPSHOT_DATE,
                            }
                            if (
                                isinstance(exc, _CountedVisionProviderError)
                                and exc.failure_audit
                            ):
                                failed_analysis.vision_payload["failure_audit"] = dict(
                                    exc.failure_audit
                                )
                            if isinstance(exc, SearchRankingQuotaExceededError):
                                failed_analysis.vision_payload["weekly_quota"] = dict(exc.quota)
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
        expected_family_cache_material: str = "",
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
                if expected_family_cache_material:
                    family = _eligible_family_offers(session, offer, self.runtime)
                    current_family_profile = _family_profile_from_offers(
                        family,
                        representative_offer_id=offer_id,
                    )
                    if (
                        _variant_family_cache_material(current_family_profile)
                        != expected_family_cache_material
                    ):
                        raise SearchRankingInputError(
                            "商品族的变体标题、参数、主图或有效成员在识别期间发生变化，已停止搜索，请重新运行"
                        )
        finally:
            engine.dispose()


async def _collect_keyword_observation(
    client: SearchPublicClient,
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
    request_parts = urlsplit(request_url)
    captured_request_qsearch = dict(
        parse_qsl(request_parts.query, keep_blank_values=True)
    ).get("qsearch", "")
    captured_request_matches_keyword = " ".join(
        captured_request_qsearch.casefold().split()
    ) == " ".join(keyword.casefold().split())
    first_products, paging = _search_products(payload)
    first_products = first_products[:ORGANIC_PAGE_SIZE]
    first_page_titles = [item["title"] for item in first_products]
    total = _optional_int(paging.get("total_num_found"))
    core_threshold = max(relevance_threshold, CORE_MAJORITY_FLOOR)
    semantic_relation = _semantic_relation_evidence(
        keyword=keyword,
        first_page_titles=first_page_titles,
        first_page_products=first_products,
        profile=profile,
        candidate=candidate,
        core_threshold=core_threshold,
        source_title=source_title,
        target_plid=target_plid,
        total_num_found=total,
    )
    validation_terms = [
        str(term)
        for term in semantic_relation["semantic_relation_same_product_terms"]
        if str(term).strip()
    ]
    controlled_validation_aliases = list(
        dict.fromkeys(
            " ".join(
                sorted(
                    tokens,
                    key=lambda token: (token in PROJECTION_SCREEN_FORM_TOKENS, token),
                )
            )
            for term in validation_terms
            for tokens in _validation_token_sets(term)
            if len(_validation_token_sets(term)) > 1
        )
    )
    validation_term_source = "semantic_verified_same_product_terms"
    raw_result_classifications = semantic_relation.get("first_page_result_classifications")
    result_classifications = (
        raw_result_classifications
        if isinstance(raw_result_classifications, list)
        else []
    )
    relevant_flags = [
        bool(item.get("is_core_competitor")) if isinstance(item, Mapping) else False
        for item in result_classifications
    ]
    direct_flags = [
        bool(item.get("is_direct_competitor")) if isinstance(item, Mapping) else False
        for item in result_classifications
    ]
    same_demand_flags = [
        bool(item.get("is_same_demand_competitor"))
        if isinstance(item, Mapping)
        else False
        for item in result_classifications
    ]
    score = sum(relevant_flags) / len(relevant_flags) if relevant_flags else 0.0
    matched_count = sum(relevant_flags)
    direct_count = sum(direct_flags)
    same_demand_count = sum(same_demand_flags)
    matched_result_titles = [
        title
        for title, relevant in zip(first_page_titles, relevant_flags, strict=True)
        if relevant
    ]
    evaluated_count = len(relevant_flags)
    semantic_grade = str(semantic_relation["semantic_relation_grade"])
    provenance = _candidate_provenance(candidate)
    comparison_resample = (
        candidate.candidate_source == "comparison_resample"
        and candidate.intended_strategy == "comparison"
    )
    comparison_required = candidate.comparison_role in {"primary", "secondary"}
    accepted_as_core = bool(
        semantic_grade == "S"
        and semantic_relation.get("semantic_relation_core_page_qualified")
        and not comparison_resample
    )
    autocomplete_sources = [
        item
        for item in provenance
        if _is_platform_root_expansion_source(item.get("candidate_source"))
    ]
    autocomplete_ranks = [
        rank
        for item in autocomplete_sources
        if (rank := _optional_int(item.get("autocomplete_rank"))) is not None
    ]
    observed_autocomplete_rank = (
        min(autocomplete_ranks) if autocomplete_ranks else candidate.autocomplete_rank
    )
    platform_expansion_observed = bool(autocomplete_ranks)
    opportunity_candidate = bool(
        platform_expansion_observed and semantic_grade in {"S", "A"}
    )
    primary_autocomplete_source = min(
        autocomplete_sources,
        key=lambda item: _optional_int(item.get("autocomplete_rank")) or 10_000,
        default={},
    )
    opportunity_seeds = list(
        dict.fromkeys(
            str(item.get("seed") or "").strip()
            for item in provenance
            if _is_platform_root_expansion_source(item.get("candidate_source"))
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
        (index for index, product in enumerate(first_products) if product["plid"] == target_plid),
        None,
    )
    target_on_first_page = target_first_page_index is not None
    target_counted_as_direct_competitor = bool(
        target_first_page_index is not None and direct_flags[target_first_page_index]
    )
    direct_competitors_excluding_target = max(
        0,
        direct_count - int(target_counted_as_direct_competitor),
    )
    core_competitors_excluding_target = max(
        0,
        matched_count - int(target_counted_as_direct_competitor),
    )
    opportunity_claims_safe = bool(opportunity_safety["opportunity_claims_safe"])
    opportunity_precheck_reasons: list[str] = []
    if not comparison_resample:
        if not platform_expansion_observed:
            opportunity_precheck_reasons.append("missing_platform_root_expansion")
        if semantic_grade not in {"S", "A"}:
            opportunity_precheck_reasons.append("semantic_relation_not_s_or_a")
        opportunity_precheck_reasons.extend(
            _opportunity_safety_rejection_reasons(opportunity_safety)
        )
        if direct_competitors_excluding_target > OPPORTUNITY_MAX_DIRECT_COMPETITORS:
            opportunity_precheck_reasons.append("too_many_direct_competitors")
    api_match = API_VERSION_PATTERN.search(request_url)
    evidence = {
        "candidate_rationale": candidate.rationale,
        "candidate_source": candidate.candidate_source,
        "query_source_channel": _query_source_channel(candidate),
        "query_source_channels": _query_source_channels(candidate),
        "intended_strategy": candidate.intended_strategy,
        "journey_type": candidate.journey_type,
        "journey_root": candidate.journey_root,
        "journey_path": list(candidate.journey_path),
        "journey_depth": candidate.journey_depth,
        "journey_parent_query": candidate.journey_parent_query,
        "adaptive_recovery": candidate.adaptive_recovery_source is not None,
        "adaptive_recovery_source": candidate.adaptive_recovery_source,
        "journey_types": list(
            dict.fromkeys(
                str(item.get("journey_type") or "")
                for item in provenance
                if str(item.get("journey_type") or "")
            )
        ),
        "journey_roots": list(
            dict.fromkeys(
                str(item.get("journey_root") or "")
                for item in provenance
                if str(item.get("journey_root") or "")
            )
        ),
        "journey_paths": [
            list(raw_path)
            for item in provenance
            if isinstance((raw_path := item.get("journey_path")), list) and raw_path
        ],
        "candidate_provenance": [dict(item) for item in provenance],
        "root_expansion_sources": list(
            dict.fromkeys(
                str(item.get("root_source") or item.get("seed_source") or "")
                for item in provenance
                if _is_platform_root_expansion_source(item.get("candidate_source"))
                and str(item.get("root_source") or item.get("seed_source") or "")
            )
        ),
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
            else "core"
            if accepted_as_core
            else "pending_validation"
        ),
        "comparison_baseline_rank": candidate.comparison_baseline_rank,
        "comparison_role": candidate.comparison_role,
        "comparison_strategy": candidate.comparison_strategy,
        "autocomplete_seed": candidate.seed,
        "autocomplete_seed_source": candidate.seed_source,
        "autocomplete_rank": observed_autocomplete_rank,
        "root_expansion_root": candidate.seed,
        "root_expansion_source": candidate.seed_source,
        "root_expansion_rank": observed_autocomplete_rank,
        "root_expansion_origin_phrase": primary_autocomplete_source.get(
            "root_expansion_origin_phrase"
        ),
        "autocomplete_endpoint": (
            "searches/search_suggestions" if observed_autocomplete_rank is not None else None
        ),
        "autocomplete_is_search_volume": False,
        "root_expansion_rank_is_search_volume": False,
        "autocomplete_cache_status": primary_autocomplete_source.get("autocomplete_cache_status"),
        "autocomplete_observed_at": primary_autocomplete_source.get("autocomplete_observed_at"),
        "autocomplete_cache_age_hours": primary_autocomplete_source.get(
            "autocomplete_cache_age_hours"
        ),
        "autocomplete_cache_ttl_hours": primary_autocomplete_source.get(
            "autocomplete_cache_ttl_hours"
        ),
        "autocomplete_shared_across_stores": primary_autocomplete_source.get(
            "autocomplete_shared_across_stores"
        ),
        "demand_signal_note": (
            "该词来自上一轮建议标题的有排名基线，仅用于同词公开搜索复采。"
            if comparison_resample
            else (
                "Takealot 完整根词的扩展及其顺序是平台直接意图信号，但不是公开搜索量。"
                if observed_autocomplete_rank is not None
                else (
                    "该词来自当前主标题完整词组直验；平台未返回可采用补全词，"
                    "因此不能把它表述为平台热词。"
                    if "seller_title_complete_phrase"
                    in _query_source_channels(candidate)
                    else "该词来自图片精准识别，不把模型判断当作平台搜索量。"
                )
            )
        ),
        "validation_terms": validation_terms,
        "profile_distinctive_terms": list(profile.distinctive_terms),
        "top_result_titles": first_page_titles[:5],
        "matched_result_titles": matched_result_titles[:8],
        "matched_top_results": matched_count,
        "evaluated_top_results": evaluated_count,
        "matched_first_page_results": matched_count,
        "evaluated_first_page_results": evaluated_count,
        "first_page_same_type_ratio": score,
        **semantic_relation,
        "page_validation_status": "completed",
        "same_type_validation_method": (
            "exact_identity_and_same_demand_family_page_audit"
        ),
        "same_type_validation_controlled_aliases": controlled_validation_aliases,
        "same_type_validation_term_source": validation_term_source,
        "same_type_validation_uses_multimodal_per_result": False,
        "same_type_validation_requires_contiguous_phrase": True,
        "same_type_validation_limitations": (
            "首页36个自然商品分开核验完全同款、同需求替代品和无关商品；"
            "平台图片链接随判定保存供人工复核，但自动统计不会为每个结果再次调用视觉模型。"
        ),
        "first_page_majority": (
            direct_count / evaluated_count >= CORE_MAJORITY_FLOOR
            if evaluated_count
            else False
        ),
        "first_page_core_competitor_density_qualified": bool(
            semantic_relation.get("semantic_relation_core_page_qualified")
        ),
        "core_threshold": core_threshold,
        "core_demand_ratio_floor": CORE_DEMAND_COMPETITOR_RATIO_FLOOR,
        "core_demand_min_results": CORE_DEMAND_COMPETITOR_MIN_RESULTS,
        "core_min_platform_results": CORE_MIN_PLATFORM_RESULTS,
        "platform_result_count_is_search_volume": False,
        "platform_result_count_role": "core_keyword_supply_breadth_gate",
        "direct_competitor_count_first_page": direct_count,
        "same_demand_competitor_count_first_page": same_demand_count,
        "core_competitor_count_first_page": matched_count,
        "core_competitor_count_excluding_target_first_page": (
            core_competitors_excluding_target
        ),
        "direct_competitor_detection": (
            "ordered_same_product_identity_exclusions_and_title_signatures"
        ),
        "direct_competitor_detection_note": (
            "直接同品按完整有序商品名、直接别名、目标PLID或至少3词同款标题签名逐条判定；"
            "不同形态但满足同一核心需求的商品另计为同需求竞品，不再混入无关商品。"
        ),
        "direct_competitor_count_excluding_target_first_page": (
            direct_competitors_excluding_target
        ),
        "target_on_first_page": target_on_first_page,
        "target_counted_as_direct_competitor": (target_counted_as_direct_competitor),
        "opportunity_candidate": opportunity_candidate,
        "blue_ocean_candidate": opportunity_candidate,
        "blue_ocean_platform_expansion_observed": platform_expansion_observed,
        **opportunity_safety,
        "opportunity_max_direct_competitors": OPPORTUNITY_MAX_DIRECT_COMPETITORS,
        "opportunity_max_organic_rank": OPPORTUNITY_MAX_ORGANIC_RANK,
        "opportunity_qualified": False,
        "blue_ocean_qualified": False,
        "opportunity_rejection_reasons": opportunity_precheck_reasons,
        "captured_request_endpoint": (
            f"{request_parts.scheme}://{request_parts.netloc}{request_parts.path}"
        ),
        "captured_request_qsearch": captured_request_qsearch,
        "captured_request_matches_keyword": captured_request_matches_keyword,
        "api_version": api_match.group(1) if api_match else None,
        "sort": "Relevance",
        "page_size": ORGANIC_PAGE_SIZE,
        "columns_per_row": DESKTOP_COLUMNS,
        "position_scope": "organic_results_excluding_sponsored",
        "ranking_source": "sections.products.results:type=product_views",
        "sponsored_exclusion": "section_type_and_explicit_flags",
    }
    observed_at = _utcnow()
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
        evidence.update(
            _opportunity_gate_from_result(
                keyword=keyword,
                source_title=source_title,
                found=found,
                page_number=page_number,
                organic_rank=organic_rank,
                validation_evidence=evidence,
            )
        )
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
            or (opportunity_window_only and cumulative >= OPPORTUNITY_MAX_ORGANIC_RANK)
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
    else:
        gate = _opportunity_gate_from_result(
            keyword=keyword,
            source_title=source_title,
            found=found,
            page_number=found_page_number,
            organic_rank=found_organic_rank,
            validation_evidence=evidence,
        )
        evidence.update(gate)
        if accepted_as_core:
            relevance_status = "accepted"
            evidence["effective_strategy"] = "core"
        elif gate["blue_ocean_qualified"]:
            relevance_status = "opportunity"
            evidence["effective_strategy"] = "blue_ocean"
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


async def _collect_shopper_journey(
    client: SearchPublicClient,
    *,
    candidates: list[SearchKeywordCandidate],
    autocomplete_checks: list[dict[str, Any]],
    target_plid: str,
    profile: VisionProfile,
    max_pages: int,
    max_keywords: int,
    relevance_threshold: float,
    source_title: str,
) -> tuple[list[KeywordObservation], list[dict[str, Any]], dict[str, Any]]:
    """Validate a bounded shopper path while preserving one-click operator UX."""

    queue = list(candidates)
    observations: list[KeywordObservation] = []
    journey_steps: list[dict[str, Any]] = []
    evaluated_pairs: list[tuple[SearchKeywordCandidate, KeywordObservation]] = []
    attempted_queries: set[str] = set()
    comparison_mode = any(
        candidate.comparison_role in {"primary", "secondary"}
        or candidate.candidate_source == "comparison_resample"
        for candidate in queue
    )

    async def collect_candidate(candidate: SearchKeywordCandidate) -> None:
        normalized = " ".join(candidate.phrase.split()).casefold()
        if not normalized or normalized in attempted_queries:
            return
        attempted_queries.add(normalized)
        observation = await _collect_keyword_observation(
            client,
            candidate=candidate,
            candidate_order=len(observations) + 1,
            target_plid=target_plid,
            profile=profile,
            max_pages=max_pages,
            relevance_threshold=relevance_threshold,
            # The shared wrapper already spaces autocomplete, first-page, and
            # cursor requests. Do not add a second delay between cursor pages.
            page_delay_seconds=0,
            source_title=source_title,
        )
        observations.append(observation)
        evaluated_pairs.append((candidate, observation))
        journey_steps.append(
            {
                "query": observation.keyword,
                "query_source_channel": _query_source_channel(candidate),
                "journey_type": candidate.journey_type,
                "shopper_root": candidate.journey_root,
                "path": list(candidate.journey_path),
                "parent_query": candidate.journey_parent_query,
                "result": observation.relevance_status,
                "first_page_same_type_ratio": observation.relevance_score,
                "target_found": observation.found,
                "pages_scanned": observation.pages_scanned,
                "adaptive_recovery": candidate.adaptive_recovery_source is not None,
                "adaptive_recovery_source": candidate.adaptive_recovery_source,
            }
        )

    if comparison_mode:
        for candidate in queue:
            if len(observations) >= max_keywords:
                break
            await collect_candidate(candidate)
        return (
            observations,
            journey_steps,
            {
                "valid_platform_root_target": ADAPTIVE_VALID_PLATFORM_ROOT_TARGET,
                "valid_platform_root_count": 0,
                "valid_platform_roots": [],
                "adaptive_recovery_used": False,
                "adaptive_recovery_query": None,
                "adaptive_recovery_source": None,
                "adaptive_recovery_skipped_reason": "comparison_resample_priority",
            },
        )

    base_queue = [item for item in queue if item.adaptive_recovery_source is None]
    recovery_queue = [item for item in queue if item.adaptive_recovery_source]
    for candidate in base_queue:
        if len(observations) >= max_keywords:
            break
        await collect_candidate(candidate)

    valid_platform_roots = _valid_platform_core_roots(evaluated_pairs)
    recovery_candidate: SearchKeywordCandidate | None = None
    skipped_reason: str | None = None
    if len(valid_platform_roots) >= ADAPTIVE_VALID_PLATFORM_ROOT_TARGET:
        skipped_reason = "valid_platform_root_target_met"
    elif len(observations) >= max_keywords:
        skipped_reason = "query_budget_exhausted"
    else:
        # Result-page learning is source priority 4, but it has a runtime
        # dependency: at least one real rejected Takealot result page must
        # already exist. Once that evidence exists, prefer it over a generic
        # second-best expansion fallback.
        for parent_candidate, observation in evaluated_pairs:
            if (
                observation.relevance_status != "rejected_irrelevant"
                or not _candidate_has_intended_strategy(parent_candidate, "core")
                or "takealot_root_expansion"
                not in _query_source_channels(parent_candidate)
            ):
                continue
            learned_seed = _result_page_learning_seed(
                observation,
                profile=profile,
                source_title=source_title,
            )
            if not learned_seed:
                continue
            learned_seed_key = " ".join(learned_seed.split()).casefold()
            learned_seed_already_checked = any(
                " ".join(str(item.get("seed") or "").split()).casefold()
                == learned_seed_key
                for item in autocomplete_checks
            )
            if (
                len(autocomplete_checks) >= ROOT_EXPANSION_INPUT_LIMIT
                and not learned_seed_already_checked
            ):
                continue
            learned_candidates = await _result_page_learning_candidates(
                client,
                seed=learned_seed,
                parent_candidate=parent_candidate,
                profile=profile,
                source_title=source_title,
                autocomplete_checks=autocomplete_checks,
            )
            recovery_candidate = next(
                (
                    replace(
                        item,
                        adaptive_recovery_source="result_page_learning",
                    )
                    for item in learned_candidates
                    if " ".join(item.phrase.split()).casefold()
                    not in attempted_queries
                ),
                None,
            )
            if recovery_candidate is not None:
                break
        if recovery_candidate is None:
            recovery_candidate = next(
                (
                    item
                    for item in recovery_queue
                    if " ".join(item.phrase.split()).casefold() not in attempted_queries
                ),
                None,
            )
        if recovery_candidate is None:
            skipped_reason = "no_supported_recovery_candidate"
        else:
            await collect_candidate(recovery_candidate)

    valid_platform_roots = _valid_platform_core_roots(evaluated_pairs)
    recovery_observation = next(
        (
            observation
            for candidate, observation in evaluated_pairs
            if candidate.adaptive_recovery_source is not None
        ),
        None,
    )
    return (
        observations,
        journey_steps,
        {
            "valid_platform_root_target": ADAPTIVE_VALID_PLATFORM_ROOT_TARGET,
            "valid_platform_root_count": len(valid_platform_roots),
            "valid_platform_roots": sorted(valid_platform_roots),
            "adaptive_recovery_used": recovery_observation is not None,
            "adaptive_recovery_query": (
                recovery_observation.keyword if recovery_observation is not None else None
            ),
            "adaptive_recovery_source": (
                recovery_candidate.adaptive_recovery_source
                if recovery_observation is not None and recovery_candidate is not None
                else None
            ),
            "adaptive_recovery_skipped_reason": (
                None if recovery_observation is not None else skipped_reason
            ),
        },
    )


def _valid_platform_core_roots(
    evaluated_pairs: list[tuple[SearchKeywordCandidate, KeywordObservation]],
) -> set[str]:
    roots: set[str] = set()
    for candidate, observation in evaluated_pairs:
        if observation.relevance_status != "accepted":
            continue
        for source in _candidate_provenance(candidate):
            if (
                not _is_platform_root_expansion_source(source.get("candidate_source"))
                or str(source.get("intended_strategy") or "") != "core"
            ):
                continue
            root = " ".join(str(source.get("journey_root") or "").split())
            if root:
                roots.add(root.casefold())
    return roots


def _candidate_has_intended_strategy(
    candidate: SearchKeywordCandidate,
    intended_strategy: str,
) -> bool:
    return any(
        str(item.get("intended_strategy") or "") == intended_strategy
        for item in _candidate_provenance(candidate)
    )


def _result_page_learning_seed(
    observation: KeywordObservation,
    *,
    profile: VisionProfile,
    source_title: str,
) -> str | None:
    evidence = observation.validation_evidence
    raw_titles = evidence.get("matched_result_titles")
    if not isinstance(raw_titles, list) or len(raw_titles) < RESULT_PAGE_LEARNING_MIN_MATCHES:
        return None
    primary_tokens = [
        _canonical_token(token)
        for token in TOKEN_PATTERN.findall(profile.product_type_terms[0].casefold())
    ]
    if not primary_tokens:
        return None
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for raw_title in raw_titles:
        title_tokens = TOKEN_PATTERN.findall(str(raw_title).casefold())
        canonical_title_tokens = [_canonical_token(token) for token in title_tokens]
        for start in range(len(title_tokens) - len(primary_tokens) + 1):
            if canonical_title_tokens[start : start + len(primary_tokens)] != primary_tokens:
                continue
            phrase_tokens = title_tokens[start : start + len(primary_tokens)]
            if len(primary_tokens) == 1:
                if start == 0:
                    continue
                prefix = canonical_title_tokens[start - 1]
                if not prefix or prefix in TITLE_CONNECTOR_TOKENS or prefix.isdigit():
                    continue
                phrase_tokens = title_tokens[start - 1 : start + 1]
            phrase = " ".join(phrase_tokens)
            normalized = " ".join(phrase.casefold().split())
            meaningful_phrase_tokens = _canonical_tokens(phrase) - TITLE_CONNECTOR_TOKENS
            if (
                not meaningful_phrase_tokens
                or not meaningful_phrase_tokens.issubset(_canonical_tokens(source_title))
                or not _keyword_claims_supported(phrase, source_title)
            ):
                continue
            phrase_canonical_tokens = _canonical_tokens(phrase)
            if any(
                (excluded := _canonical_tokens(exclusion))
                and excluded.issubset(phrase_canonical_tokens)
                for exclusion in profile.exclusions
            ):
                continue
            counts[normalized] = counts.get(normalized, 0) + 1
            display.setdefault(normalized, phrase)
            break
    qualified = [
        (count, len(TOKEN_PATTERN.findall(key)), key)
        for key, count in counts.items()
        if count >= RESULT_PAGE_LEARNING_MIN_MATCHES and key != observation.keyword.casefold()
    ]
    if not qualified:
        return None
    _, _, selected = max(qualified, key=lambda item: (item[0], item[1], item[2]))
    return display[selected]


async def _result_page_learning_candidates(
    client: SearchPublicClient,
    *,
    seed: str,
    parent_candidate: SearchKeywordCandidate,
    profile: VisionProfile,
    source_title: str,
    autocomplete_checks: list[dict[str, Any]],
) -> list[SearchKeywordCandidate]:
    normalized_seed = " ".join(seed.split())
    existing_check = next(
        (
            item
            for item in autocomplete_checks
            if " ".join(str(item.get("seed") or "").split()).casefold()
            == normalized_seed.casefold()
            and item.get("status") == "observed"
        ),
        None,
    )
    try:
        suggestions = (
            [str(item) for item in existing_check.get("suggestions", [])]
            if isinstance(existing_check, Mapping)
            else (await client.fetch_search_suggestions(normalized_seed))[
                :AUTOCOMPLETE_RESULT_LIMIT
            ]
        )
    except Exception as exc:
        autocomplete_checks.append(
            {
                "seed": normalized_seed,
                "root": normalized_seed,
                "input_kind": "complete_root_expansion",
                "seed_source": "result_page_learning",
                "root_source": "result_page_learning",
                "shopper_root": normalized_seed,
                "input_state": normalized_seed,
                "journey_path": [*parent_candidate.journey_path, normalized_seed],
                "journey_type": "result_page_root_expansion",
                "status": "unavailable",
                "error_type": type(exc).__name__,
            }
        )
        return []
    expansion_rows: list[dict[str, Any]] = []
    ranked: list[tuple[float, int, str]] = []
    for rank, phrase in enumerate(suggestions, start=1):
        normalized_phrase = " ".join(phrase.split())
        decision = _root_expansion_relevance_decision(
            normalized_phrase,
            profile,
            source_title=source_title,
        )
        has_primary_shape = _candidate_has_primary_shape(normalized_phrase, profile)
        if decision["accepted"] and not has_primary_shape:
            decision = {
                "accepted": False,
                "relation": "irrelevant",
                "reason": "result_page_followup_missing_primary_product_shape",
                "matched_terms": list(decision["matched_terms"]),
            }
        query_word_count = len(TOKEN_PATTERN.findall(normalized_phrase.casefold()))
        query_length_status = (
            "eligible"
            if query_word_count <= MODEL_DIRECT_QUERY_MAX_WORDS
            else "rejected_too_long"
        )
        expansion_rows.append(
            {
                "phrase": normalized_phrase,
                "rank": rank,
                "relevance_status": (
                    "eligible" if decision["accepted"] else "rejected_irrelevant"
                ),
                "relation": decision["relation"],
                "reason": decision["reason"],
                "matched_terms": list(decision["matched_terms"]),
                "query_word_count": query_word_count,
                "query_length_status": query_length_status,
                "used_as_followup_root": False,
            }
        )
        fit = _autocomplete_fit_score(
            normalized_phrase,
            profile,
            source_title=source_title,
        )
        if decision["accepted"] and fit > 0 and query_length_status == "eligible":
            ranked.append((fit, rank, normalized_phrase))
    if existing_check is None:
        autocomplete_checks.append(
            {
                "seed": normalized_seed,
                "root": normalized_seed,
                "input_kind": "complete_root_expansion",
                "seed_source": "result_page_learning",
                "root_source": "result_page_learning",
                "seed_sources": ["result_page_learning"],
                "intended_strategies": ["core"],
                "shopper_root": normalized_seed,
                "input_state": normalized_seed,
                "journey_path": [*parent_candidate.journey_path, normalized_seed],
                "journey_type": "result_page_root_expansion",
                "journey_depth": parent_candidate.journey_depth + 1,
                "status": "observed",
                "suggestions": suggestions,
                "expansions": expansion_rows,
                "eligible_expansion_count": sum(
                    item["relevance_status"] == "eligible"
                    and item["query_length_status"] == "eligible"
                    for item in expansion_rows
                ),
                "rejected_expansion_count": sum(
                    item["relevance_status"] == "rejected_irrelevant"
                    or item["query_length_status"] == "rejected_too_long"
                    for item in expansion_rows
                ),
                "related_but_too_long_count": sum(
                    item["relevance_status"] == "eligible"
                    and item["query_length_status"] == "rejected_too_long"
                    for item in expansion_rows
                ),
                "raw_suggestions_are_selected": False,
                "selection_policy": (
                    "same_product_identity_or_structured_adjacent_product_family"
                ),
                "parent_query": parent_candidate.phrase,
                **_autocomplete_cache_evidence(client, normalized_seed),
            }
        )
    output: list[SearchKeywordCandidate] = []
    for _, rank, phrase in sorted(ranked, key=lambda item: (-item[0], item[1]))[:2]:
        path = tuple(dict.fromkeys((*parent_candidate.journey_path, normalized_seed, phrase)))
        output.append(
            SearchKeywordCandidate(
                phrase=phrase,
                rationale=(
                    "上一条搜索页出现少量同形态商品，系统从这些结果标题提炼词根，"
                    "再使用 Takealot 当时的真实根词扩展验证"
                ),
                candidate_source="takealot_root_expansion",
                intended_strategy="core",
                seed=normalized_seed,
                seed_source="result_page_learning",
                autocomplete_rank=rank,
                journey_type="result_page_root_expansion",
                journey_root=normalized_seed,
                journey_path=path,
                journey_depth=parent_candidate.journey_depth + 1,
                journey_parent_query=parent_candidate.phrase,
                candidate_provenance=(
                    {
                        "candidate_source": "takealot_root_expansion",
                        "intended_strategy": "core",
                        "seed": normalized_seed,
                        "seed_source": "result_page_learning",
                        "autocomplete_rank": rank,
                        "root": normalized_seed,
                        "root_expansion_rank": rank,
                        "journey_type": "result_page_root_expansion",
                        "journey_root": normalized_seed,
                        "journey_path": list(path),
                        "journey_depth": parent_candidate.journey_depth + 1,
                        "journey_parent_query": parent_candidate.phrase,
                    },
                ),
            )
        )
    return output


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
        subtitle = " ".join(str(core.get("subtitle") or "").split())
        slug = str(core.get("slug") or "").strip("/")
        if not plid or not title:
            continue
        gallery = view.get("gallery") if isinstance(view, Mapping) else None
        raw_images = gallery.get("images") if isinstance(gallery, Mapping) else None
        image_url = ""
        if isinstance(raw_images, list):
            image_url = next(
                (
                    str(value).replace("{size}", "pdpxl")
                    for value in raw_images
                    if str(value or "").strip()
                ),
                "",
            )
        products.append(
            {
                "plid": plid,
                "title": title,
                "subtitle": subtitle,
                "image_url": image_url,
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


def _validation_terms(profile: VisionProfile, *, keyword: str) -> list[str]:
    del keyword
    return _same_product_relation_terms(profile)


def _title_matches_terms(title: str, terms: list[str]) -> bool:
    title_tokens = _canonical_tokens(title)
    return any(
        bool(tokens) and tokens.issubset(title_tokens)
        for term in terms
        for tokens in _validation_token_sets(term)
    )


def _same_product_relation_terms(profile: VisionProfile) -> list[str]:
    """Return model/fact-backed product names without using keyword provenance."""

    return list(
        dict.fromkeys(
            normalized
            for value in (*profile.product_type_terms, *profile.same_product_aliases)
            if (normalized := " ".join(str(value or "").casefold().split()))
            and not (
                len(tokens := _identity_term_tokens(normalized)) == 1
                and tokens[0] in GENERIC_IDENTITY_HEAD_TOKENS
            )
        )
    )


def _same_demand_relation_terms(profile: VisionProfile) -> list[str]:
    """Return explicit substitute families for the target's primary buyer need."""

    exact_terms = {
        term.casefold() for term in _same_product_relation_terms(profile)
    }
    return list(
        dict.fromkeys(
            normalized
            for value in (
                *profile.same_demand_product_terms,
                *(
                    term
                    for intent in profile.opportunity_seeds
                    for term in intent.alternative_product_terms
                ),
            )
            if (normalized := " ".join(str(value or "").casefold().split()))
            and normalized not in exact_terms
            and not (
                len(tokens := _identity_term_tokens(normalized)) == 1
                and tokens[0] in GENERIC_IDENTITY_HEAD_TOKENS
            )
        )
    )


def _semantic_token_sets(value: str) -> list[set[str]]:
    return [
        {IDENTITY_TOKEN_ALIASES.get(token, token) for token in tokens}
        for tokens in _validation_token_sets(value)
        if tokens
    ]


def _semantic_text_tokens(value: str) -> tuple[str, ...]:
    return tuple(_identity_term_tokens(value))


def _semantic_retargets_product(value: str, matched_tokens: set[str]) -> bool:
    ordered = [
        token
        for raw_token in TOKEN_PATTERN.findall(value.casefold())
        for canonical_part in _canonical_token_parts(raw_token)
        if (token := IDENTITY_TOKEN_ALIASES.get(canonical_part, canonical_part))
    ]
    if not ordered:
        return False
    matched_positions = [
        index for index, token in enumerate(ordered) if token in matched_tokens
    ]
    if not matched_positions:
        return False
    first_match = min(matched_positions)
    last_match = max(matched_positions)
    for index, token in enumerate(ordered):
        if token in matched_tokens or token not in SEMANTIC_RETARGETING_HEAD_TOKENS:
            continue
        # Reject an accessory head before the product, a retargeting noun that
        # splits the matched product phrase, or an accessory noun immediately
        # following it. A later "with cover/cushion" bundle is not rejected by
        # this structural check merely because the accessory is also included.
        if index < first_match or first_match < index < last_match or index == last_match + 1:
            return True
    return False


def _semantic_matching_product_terms(value: str, terms: Sequence[str]) -> list[str]:
    text_tokens = set(_semantic_text_tokens(value))
    output: list[str] = []
    for raw_term in terms:
        term = " ".join(str(raw_term or "").casefold().split())
        if not term:
            continue
        if any(
            token_set.issubset(text_tokens)
            and not _semantic_retargets_product(value, token_set)
            for token_set in _semantic_token_sets(term)
        ):
            output.append(term)
    return list(dict.fromkeys(output))


def _semantic_title_matches_product_terms(title: str, terms: Sequence[str]) -> bool:
    return bool(_semantic_matching_product_terms(title, terms))


def _current_title_direct_product_alias(
    *,
    keyword: str,
    source_title: str,
    same_product_terms: Sequence[str],
) -> str | None:
    """Verify an exact title phrase as a product alias without model guesswork."""

    query_tokens = _semantic_text_tokens(keyword)
    title_tokens = _semantic_text_tokens(source_title)
    if not query_tokens or not _contains_token_sequence(title_tokens, query_tokens):
        return None
    matched_identity_tokens: set[str] = set()
    for term in same_product_terms:
        term_tokens = _semantic_text_tokens(term)
        if not term_tokens:
            continue
        identity_matches, _ = _identity_type_tokens_match(query_tokens, term_tokens)
        if identity_matches:
            matched_identity_tokens.update(set(query_tokens) & set(term_tokens))
    if not matched_identity_tokens:
        return None
    if _semantic_retargets_product(keyword, matched_identity_tokens):
        return None
    return " ".join(keyword.casefold().split())


def _matching_adjacent_demand_intents(
    *,
    profile: VisionProfile,
    candidate: SearchKeywordCandidate,
    keyword: str,
) -> list[KeywordCandidate]:
    """Match a query to structured buyer-job hypotheses, never to source priority."""

    keyword_tokens = set(_semantic_text_tokens(keyword))
    provenance = _candidate_provenance(candidate)
    provenance_roots = {
        " ".join(str(value or "").casefold().split())
        for item in provenance
        for value in (
            item.get("seed"),
            item.get("root"),
            item.get("journey_root"),
        )
        if " ".join(str(value or "").split())
    }
    output: list[KeywordCandidate] = []
    for intent in profile.opportunity_seeds:
        root = " ".join(intent.phrase.casefold().split())
        root_tokens = set(_semantic_text_tokens(root))
        alternatives = [
            " ".join(str(term or "").split())
            for term in intent.alternative_product_terms
            if " ".join(str(term or "").split())
        ]
        if not root_tokens or not intent.buyer_job.strip() or not alternatives:
            continue
        if root_tokens.issubset(keyword_tokens) or root in provenance_roots:
            output.append(intent)
    return output


def _strict_identity_sequences(value: str) -> list[tuple[str, ...]]:
    """Return ordered product-name forms suitable for direct-competitor proof."""

    tokens = _identity_term_tokens(value)
    if not tokens:
        return []
    token_set = set(tokens)
    if not (
        token_set & PROJECTION_SCREEN_HEAD_TOKENS
        and token_set & PROJECTION_SCREEN_FORM_TOKENS
    ):
        return [tokens]
    return [
        (head, form)
        for head in sorted(PROJECTION_SCREEN_HEAD_TOKENS)
        for form in sorted(PROJECTION_SCREEN_FORM_TOKENS)
    ]


def _strict_matching_product_terms(value: str, terms: Sequence[str]) -> list[str]:
    """Match a complete ordered identity phrase, never scattered title tokens."""

    title_tokens = _identity_term_tokens(value)
    output: list[str] = []
    for raw_term in terms:
        term = " ".join(str(raw_term or "").casefold().split())
        if not term:
            continue
        matched = False
        for sequence in _strict_identity_sequences(term):
            if not _contains_token_sequence(title_tokens, sequence):
                continue
            if _semantic_retargets_product(value, set(sequence)):
                continue
            matched = True
            break
        if matched:
            output.append(term)
    return list(dict.fromkeys(output))


def _source_title_signature_tokens(value: str) -> tuple[str, ...]:
    output: list[str] = []
    for raw_token in TOKEN_PATTERN.findall(value.casefold()):
        for token in _canonical_token_parts(raw_token):
            if (
                not token
                or token in TITLE_CONNECTOR_TOKENS
                or token in TITLE_ROOT_EXPANSION_NOISE_TOKENS
                or token in _VARIANT_COLOUR_TOKENS
                or token in TITLE_PARAMETER_UNIT_TOKENS
                or token.isdigit()
                or COMBINED_MEASUREMENT_PATTERN.fullmatch(token)
                or not any(character.isalpha() for character in token)
            ):
                continue
            output.append(IDENTITY_TOKEN_ALIASES.get(token, token))
    return tuple(output)


def _source_title_identity_signatures(
    source_title: str,
    profile: VisionProfile,
) -> list[str]:
    """Extract repeated three/four-word title signatures for clone-family evidence."""

    title_tokens = _source_title_signature_tokens(source_title)
    anchor_tokens = {
        token
        for term in (*profile.product_type_terms, *profile.same_product_aliases)
        for token in _identity_term_tokens(term)
        if token not in GENERIC_IDENTITY_HEAD_TOKENS
    }
    if len(title_tokens) < 3 or not anchor_tokens:
        return []
    output: list[str] = []
    for width in (4, 3):
        if len(title_tokens) < width:
            continue
        for index in range(len(title_tokens) - width + 1):
            window = title_tokens[index : index + width]
            if not set(window) & anchor_tokens:
                continue
            if len(set(window) - GENERIC_IDENTITY_HEAD_TOKENS) < 2:
                continue
            phrase = " ".join(window)
            if phrase not in output:
                output.append(phrase)
    return output


def _seller_title_identity_query_terms(
    source_title: str,
    profile: VisionProfile,
) -> list[str]:
    """Keep one concise title noun phrase as a page-validated search hypothesis."""

    candidates: list[tuple[int, int, str]] = []
    for order, phrase in enumerate(_source_title_identity_signatures(source_title, profile)):
        tokens = _identity_term_tokens(phrase)
        if not 2 <= len(tokens) <= MODEL_DIRECT_QUERY_MAX_WORDS:
            continue
        if tokens[-1] not in GENERIC_IDENTITY_HEAD_TOKENS:
            continue
        candidates.append((len(tokens), order, phrase))
    return [
        phrase
        for _, _, phrase in sorted(candidates, key=lambda item: (item[0], item[1]))[:1]
    ]


def _same_product_exclusion_rows(
    profile: VisionProfile,
    same_product_terms: Sequence[str],
) -> list[dict[str, str]]:
    raw_terms = list(
        dict.fromkeys(
            normalized
            for value in (
                *profile.exclusions,
                *(term for intent in profile.opportunity_seeds for term in intent.excluded_product_terms),
            )
            if (normalized := " ".join(str(value or "").casefold().split()))
        )
    )
    rows: list[dict[str, str]] = [
        {"term": term, "source": "profile_exact_exclusion"} for term in raw_terms
    ]
    seen = {term.casefold() for term in raw_terms}
    same_sequences = [
        sequence
        for term in same_product_terms
        for sequence in _strict_identity_sequences(term)
    ]

    def add_core(sequence: tuple[str, ...]) -> None:
        if len(sequence) != 2 or sequence[0] in GENERIC_IDENTITY_HEAD_TOKENS:
            return
        if any(_contains_token_sequence(same_sequence, sequence) for same_sequence in same_sequences):
            return
        term = " ".join(sequence)
        if term in seen:
            return
        seen.add(term)
        rows.append({"term": term, "source": "derived_excluded_product_family"})

    for term in raw_terms:
        tokens = _identity_term_tokens(term)
        if len(tokens) >= 3:
            add_core(tokens[-2:])
        for index in range(len(tokens) - 1):
            pair = tokens[index : index + 2]
            if pair[-1] in GENERIC_IDENTITY_HEAD_TOKENS:
                add_core(pair)
    return rows


def _matched_exclusion_terms(
    value: str,
    rows: Sequence[Mapping[str, str]],
) -> list[str]:
    output: list[str] = []
    for row in rows:
        term = str(row.get("term") or "")
        source = str(row.get("source") or "")
        matched = bool(
            _semantic_matching_product_terms(value, [term])
            if source == "profile_exact_exclusion"
            else _strict_matching_product_terms(value, [term])
        )
        if matched:
            output.append(term)
    return list(dict.fromkeys(output))


def _first_page_result_classification(
    product: Mapping[str, str],
    *,
    same_product_terms: Sequence[str],
    same_demand_terms: Sequence[str],
    exclusion_rows: Sequence[Mapping[str, str]],
    source_title_signatures: Sequence[str],
    target_plid: str,
    position: int,
) -> dict[str, Any]:
    """Classify one organic result with a complete, inspectable reason trail."""

    plid = str(product.get("plid") or "")
    title = " ".join(str(product.get("title") or "").split())
    subtitle = " ".join(str(product.get("subtitle") or "").split())
    title_identity_terms = _strict_matching_product_terms(title, same_product_terms)
    loose_identity_terms = _semantic_matching_product_terms(title, same_product_terms)
    title_exclusions = _matched_exclusion_terms(title, exclusion_rows)
    subtitle_exclusions = _matched_exclusion_terms(subtitle, exclusion_rows) if subtitle else []
    signature_matches = _strict_matching_product_terms(title, source_title_signatures)
    title_demand_terms = _semantic_matching_product_terms(title, same_demand_terms)
    subtitle_demand_terms = (
        _semantic_matching_product_terms(subtitle, same_demand_terms) if subtitle else []
    )
    is_target = bool(target_plid and plid == target_plid)

    if is_target:
        classification = "direct_same_product"
        reason = "target_product"
    elif title_exclusions and (title_identity_terms or title_demand_terms):
        classification = "same_demand_competitor"
        reason = (
            "same_product_identity_with_different_form"
            if title_identity_terms
            else "same_demand_product_family"
        )
    elif title_exclusions:
        classification = "unrelated"
        reason = "conflicting_product_family_in_title"
    elif signature_matches:
        classification = "direct_same_product"
        reason = "source_title_identity_signature"
    elif title_identity_terms:
        classification = "direct_same_product"
        reason = "ordered_same_product_name_or_alias"
    elif title_demand_terms:
        classification = "same_demand_competitor"
        reason = "same_demand_product_family"
    elif loose_identity_terms:
        classification = "unrelated"
        reason = "identity_tokens_scattered_not_direct_proof"
    elif subtitle_exclusions and subtitle_demand_terms:
        classification = "same_demand_competitor"
        reason = "same_demand_product_family_in_subtitle"
    elif subtitle_exclusions:
        classification = "unrelated"
        reason = "conflicting_product_family_in_subtitle"
    elif subtitle_demand_terms:
        classification = "same_demand_competitor"
        reason = "same_demand_product_family_in_subtitle"
    else:
        classification = "unrelated"
        reason = "no_complete_same_product_identity"

    return {
        "organic_position": position,
        "plid": plid,
        "title": title,
        "subtitle": subtitle,
        "url": str(product.get("url") or ""),
        "image_url": str(product.get("image_url") or ""),
        "classification": classification,
        "is_direct_competitor": classification == "direct_same_product",
        "is_same_demand_competitor": classification == "same_demand_competitor",
        "is_core_competitor": classification
        in {"direct_same_product", "same_demand_competitor"},
        "is_target": is_target,
        "reason": reason,
        "matched_identity_terms": title_identity_terms,
        "matched_loose_identity_terms": loose_identity_terms,
        "matched_exclusion_terms": title_exclusions,
        "matched_subtitle_exclusion_terms": subtitle_exclusions,
        "matched_source_title_signatures": signature_matches,
        "matched_same_demand_terms": title_demand_terms,
        "matched_subtitle_same_demand_terms": subtitle_demand_terms,
    }


def _semantic_relation_evidence(
    *,
    keyword: str,
    first_page_titles: Sequence[str],
    first_page_products: Sequence[Mapping[str, str]] = (),
    profile: VisionProfile,
    candidate: SearchKeywordCandidate,
    core_threshold: float,
    source_title: str = "",
    target_plid: str = "",
    total_num_found: int | None = None,
) -> dict[str, Any]:
    """Classify query-to-product relation as S, A, or the merged C/I rejection grade."""

    same_product_terms = _same_product_relation_terms(profile)
    same_demand_terms = _same_demand_relation_terms(profile)
    source_title_signatures = _source_title_identity_signatures(source_title, profile)
    seller_title_identity_terms = _seller_title_identity_query_terms(
        source_title,
        profile,
    )
    deterministic_title_alias = _current_title_direct_product_alias(
        keyword=keyword,
        source_title=source_title,
        same_product_terms=same_product_terms,
    )
    normalized_keyword = " ".join(keyword.casefold().split())
    if not deterministic_title_alias and normalized_keyword in {
        term.casefold() for term in seller_title_identity_terms
    }:
        deterministic_title_alias = normalized_keyword
    if deterministic_title_alias:
        same_product_terms = list(
            dict.fromkeys((*same_product_terms, deterministic_title_alias))
        )
    same_query_terms = _semantic_matching_product_terms(keyword, same_product_terms)
    strict_query_same_terms = _strict_matching_product_terms(
        keyword,
        same_product_terms,
    )
    candidate_strategies = {
        str(item.get("intended_strategy") or "").strip().casefold()
        for item in _candidate_provenance(candidate)
    }
    core_only_candidate = bool(
        candidate.intended_strategy == "core"
        and "opportunity" not in candidate_strategies
    )
    explicit_opportunity_phrases = {
        " ".join(intent.phrase.casefold().split())
        for intent in profile.opportunity_seeds
        if intent.phrase.strip()
    }
    strict_core_query_terms = [
        term
        for term in strict_query_same_terms
        if len(_identity_term_tokens(term)) >= 2
        or normalized_keyword == " ".join(term.casefold().split())
        or (
            core_only_candidate
            and normalized_keyword not in explicit_opportunity_phrases
        )
    ]
    intents = _matching_adjacent_demand_intents(
        profile=profile,
        candidate=candidate,
        keyword=keyword,
    )
    alternative_terms = list(
        dict.fromkeys(
            normalized
            for intent in intents
            for term in intent.alternative_product_terms
            if (normalized := " ".join(str(term or "").casefold().split()))
        )
    )
    products = (
        [dict(item) for item in first_page_products]
        if first_page_products
        else [
            {
                "plid": "",
                "title": title,
                "subtitle": "",
                "url": "",
                "image_url": "",
            }
            for title in first_page_titles
        ]
    )
    first_page_titles = [str(item.get("title") or "") for item in products]
    exclusion_rows = _same_product_exclusion_rows(profile, same_product_terms)
    result_classifications = [
        _first_page_result_classification(
            product,
            same_product_terms=same_product_terms,
            same_demand_terms=same_demand_terms,
            exclusion_rows=exclusion_rows,
            source_title_signatures=source_title_signatures,
            target_plid=target_plid,
            position=index,
        )
        for index, product in enumerate(products, start=1)
    ]
    excluded_terms = [str(item["term"]) for item in exclusion_rows]
    same_flags = [bool(item["is_direct_competitor"]) for item in result_classifications]
    adjacent_flags = [
        bool(item["is_same_demand_competitor"]) for item in result_classifications
    ]

    evaluated_count = len(first_page_titles)
    same_count = sum(same_flags)
    adjacent_count = sum(adjacent_flags)
    rejected_count = max(0, evaluated_count - same_count - adjacent_count)
    same_ratio = same_count / evaluated_count if evaluated_count else 0.0
    adjacent_ratio = adjacent_count / evaluated_count if evaluated_count else 0.0
    supported_ratio = (
        (same_count + adjacent_count) / evaluated_count if evaluated_count else 0.0
    )
    # A core query must name the product as one complete, ordered identity phrase.
    # Loose token overlap remains useful audit evidence, but it must not upgrade an
    # adjacent-demand query such as ``mouse for laptop`` into an S-grade core term.
    query_identity_supported = bool(
        deterministic_title_alias or strict_core_query_terms
    )
    platform_supply_evidence_available = total_num_found is not None
    platform_supply_qualified = bool(
        total_num_found is None or total_num_found >= CORE_MIN_PLATFORM_RESULTS
    )
    competitor_density_qualified = bool(
        (
            total_num_found is None
            and evaluated_count > 0
            and same_ratio >= core_threshold
        )
        or (
            same_count + adjacent_count >= CORE_DEMAND_COMPETITOR_MIN_RESULTS
            and supported_ratio >= CORE_DEMAND_COMPETITOR_RATIO_FLOOR
        )
    )
    core_page_qualified = bool(
        query_identity_supported
        and platform_supply_qualified
        and competitor_density_qualified
    )
    adjacent_page_qualified = bool(
        evaluated_count >= SEMANTIC_ADJACENT_MIN_RESULTS
        and adjacent_count >= SEMANTIC_ADJACENT_MIN_RESULTS
        and adjacent_ratio >= SEMANTIC_ADJACENT_RATIO_FLOOR
        and supported_ratio >= SEMANTIC_SUPPORTED_RATIO_FLOOR
    )
    if core_page_qualified:
        grade = "S"
        decision = "first_page_same_demand_competitor_density"
    elif query_identity_supported:
        grade = "C/I"
        decision = (
            "insufficient_platform_supply_for_core_keyword"
            if not platform_supply_qualified
            else "same_demand_competitor_density_below_core_threshold"
        )
    elif intents and adjacent_page_qualified:
        grade = "A"
        decision = "buyer_job_and_alternative_product_page_cohere"
    else:
        grade = "C/I"
        if intents:
            decision = "adjacent_hypothesis_not_supported_by_first_page"
        else:
            decision = "no_verified_same_product_or_adjacent_buyer_job"
    return {
        "semantic_relation_grade": grade,
        "semantic_relation_label": {
            "S": "core_query_with_same_demand_competitor_density",
            "A": "adjacent_demand_alternative",
            "C/I": "complementary_or_irrelevant_rejected",
        }[grade],
        "semantic_relation_decision": decision,
        "semantic_relation_source_priority_decides_grade": False,
        "semantic_relation_requires_page_majority_for_s": False,
        "semantic_relation_requires_demand_competitor_density_for_s": True,
        "semantic_relation_current_title_alias": deterministic_title_alias,
        "semantic_relation_query_identity_supported": query_identity_supported,
        "semantic_relation_query_same_product_terms": same_query_terms,
        "semantic_relation_query_strict_same_product_terms": (
            strict_query_same_terms
        ),
        "semantic_relation_query_core_identity_terms": strict_core_query_terms,
        "semantic_relation_query_core_only_candidate": core_only_candidate,
        "semantic_relation_query_matches_explicit_opportunity_phrase": (
            normalized_keyword in explicit_opportunity_phrases
        ),
        "semantic_relation_same_product_terms": same_product_terms,
        "semantic_relation_same_demand_product_terms": same_demand_terms,
        "semantic_relation_buyer_jobs": list(
            dict.fromkeys(intent.buyer_job.strip() for intent in intents)
        ),
        "semantic_relation_adjacent_roots": list(
            dict.fromkeys(intent.phrase.strip() for intent in intents)
        ),
        "semantic_relation_alternative_product_terms": same_demand_terms,
        "semantic_relation_matched_intent_alternative_terms": alternative_terms,
        "semantic_relation_excluded_product_terms": excluded_terms,
        "semantic_relation_same_product_result_count": same_count,
        "semantic_relation_same_demand_result_count": adjacent_count,
        "semantic_relation_adjacent_result_count": adjacent_count,
        "semantic_relation_rejected_result_count": rejected_count,
        "semantic_relation_evaluated_result_count": evaluated_count,
        "semantic_relation_same_product_ratio": round(same_ratio, 4),
        "semantic_relation_same_demand_ratio": round(adjacent_ratio, 4),
        "semantic_relation_adjacent_ratio": round(adjacent_ratio, 4),
        "semantic_relation_supported_ratio": round(supported_ratio, 4),
        "semantic_relation_core_competitor_result_count": same_count + adjacent_count,
        "semantic_relation_core_competitor_ratio": round(supported_ratio, 4),
        "semantic_relation_core_density_qualified": competitor_density_qualified,
        "semantic_relation_core_page_qualified": core_page_qualified,
        "semantic_relation_core_demand_ratio_floor": (
            CORE_DEMAND_COMPETITOR_RATIO_FLOOR
        ),
        "semantic_relation_core_demand_min_results": (
            CORE_DEMAND_COMPETITOR_MIN_RESULTS
        ),
        "semantic_relation_platform_supply_evidence_available": (
            platform_supply_evidence_available
        ),
        "semantic_relation_platform_supply_qualified": platform_supply_qualified,
        "semantic_relation_platform_total_num_found": total_num_found,
        "semantic_relation_core_min_platform_results": CORE_MIN_PLATFORM_RESULTS,
        "semantic_relation_adjacent_page_qualified": adjacent_page_qualified,
        "semantic_relation_adjacent_ratio_floor": SEMANTIC_ADJACENT_RATIO_FLOOR,
        "semantic_relation_supported_ratio_floor": SEMANTIC_SUPPORTED_RATIO_FLOOR,
        "semantic_relation_min_adjacent_results": SEMANTIC_ADJACENT_MIN_RESULTS,
        "semantic_relation_same_product_result_titles": [
            str(item["title"])
            for item in result_classifications
            if item["is_direct_competitor"]
        ][:8],
        "semantic_relation_adjacent_result_titles": [
            title
            for title, matched in zip(first_page_titles, adjacent_flags, strict=True)
            if matched
        ][:8],
        "semantic_relation_same_demand_result_titles": [
            title
            for title, matched in zip(first_page_titles, adjacent_flags, strict=True)
            if matched
        ][:8],
        "semantic_relation_evidence_scope": (
            "first_page_organic_result_title_subtitle_and_product_metadata"
        ),
        "first_page_result_classifications": result_classifications,
        "source_title_identity_signatures": source_title_signatures,
        "semantic_relation_uses_per_result_image_or_category": False,
        "semantic_relation_limitations": (
            "S级核心词必须同时命中当前商品身份、达到首页同款加同需求竞品的最低数量与占比，"
            "并通过平台供给规模门槛；只有少量结果的过度组合词不能成为S级。"
            "完全同款与同需求替代品分开计数，配件、运输、食品储存及无关商品不计入。"
            "系统保存平台图片供人工核验，但当前自动判定不对每条结果再次调用多模态模型。"
        ),
    }


def _validation_token_sets(value: str) -> list[set[str]]:
    """Return conservative title-token forms for one physical product shape.

    Takealot projection-screen listings commonly alternate ``projection`` with
    ``projector`` and ``screen`` with ``cloth`` or ``curtain``. Treat those
    two-word forms as the same narrow physical class, but never match a bare
    projector device or a generic screen/cloth word on its own.
    """

    tokens = _canonical_tokens(value)
    if not tokens:
        return []
    if not (
        tokens & PROJECTION_SCREEN_HEAD_TOKENS
        and tokens & PROJECTION_SCREEN_FORM_TOKENS
    ):
        return [tokens]
    return [
        {head, form}
        for head in sorted(PROJECTION_SCREEN_HEAD_TOKENS)
        for form in sorted(PROJECTION_SCREEN_FORM_TOKENS)
    ]


def _canonical_tokens(value: str) -> set[str]:
    return {
        part
        for token in TOKEN_PATTERN.findall(value.casefold())
        for part in _canonical_token_parts(token)
    }


def _title_parameter_candidates(source_title: str) -> list[dict[str, Any]]:
    """Extract auditable title parameter phrases without classifying their importance."""

    tokens = _title_tokens(source_title)
    normalized = [token.casefold() for token in tokens]
    groups: list[tuple[int, int]] = []
    index = 0
    while index < len(tokens):
        token = normalized[index]
        if (
            TITLE_PROTECTION_RATING_PATTERN.fullmatch(token)
            or TITLE_RESOLUTION_PARAMETER_PATTERN.fullmatch(token)
            or (
                not TITLE_CONNECTIVITY_GENERATION_PATTERN.fullmatch(token)
                and (
                    TITLE_COMBINED_PARAMETER_PATTERN.fullmatch(token)
                    or TITLE_DIMENSION_PARAMETER_PATTERN.fullmatch(token)
                )
            )
        ):
            groups.append((index, index + 1))
            index += 1
            continue
        if (
            token in TITLE_PARAMETER_COUNT_TOKENS
            and index + 2 < len(tokens)
            and normalized[index + 1] == "of"
            and _is_title_number(normalized[index + 2])
        ):
            groups.append((index, index + 3))
            index += 3
            continue
        if _is_title_number(token) and index + 1 < len(tokens):
            if normalized[index + 1] == "x" and index + 2 < len(tokens):
                end = index + 2
                if not _is_title_number(normalized[end]):
                    index += 1
                    continue
                while (
                    end + 2 < len(tokens)
                    and normalized[end + 1] == "x"
                    and _is_title_number(normalized[end + 2])
                ):
                    end += 2
                if end + 1 < len(tokens) and normalized[end + 1] in TITLE_PARAMETER_UNIT_TOKENS:
                    end += 1
                groups.append((index, end + 1))
                index = end + 1
                continue
            if normalized[index + 1] in TITLE_PARAMETER_UNIT_TOKENS | TITLE_PARAMETER_COUNT_TOKENS:
                groups.append((index, index + 2))
                index += 2
                continue
        index += 1

    registry_evidence = _title_decision_parameter_evidence(source_title)
    registry_value_key = (
        " ".join(_title_tokens(str(registry_evidence["value"]))).casefold()
        if registry_evidence is not None
        else ""
    )
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for order, (start, end) in enumerate(groups, start=1):
        value = " ".join(tokens[start:end])
        key = value.casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        registry_recommended = bool(registry_value_key and key == registry_value_key)
        output.append(
            {
                "parameter_key": key,
                "parameter_value": value,
                "parameter_type": _title_parameter_type(tokens[start:end]),
                "title_order": order,
                "system_recommendation": (
                    "decision_parameter" if registry_recommended else "ordinary_specification"
                ),
                "system_reason": (
                    "该商品族已登记此选购维度，但仍须人工确认并通过同商品族搜索验证"
                    if registry_recommended
                    else "系统只识别到明确规格，默认后置；是否属于购买决策由运营人工确认"
                ),
            }
        )
        if len(output) >= DECISION_PARAMETER_MAX_CANDIDATES:
            break
    return output


def _title_parameter_type(tokens: list[str]) -> str:
    normalized = [token.casefold() for token in tokens]
    compact = "".join(normalized)
    if any(TITLE_PROTECTION_RATING_PATTERN.fullmatch(token) for token in normalized):
        return "protection_rating"
    if any(TITLE_RESOLUTION_PARAMETER_PATTERN.fullmatch(token) for token in normalized):
        return "resolution"
    if any(token in TITLE_PARAMETER_COUNT_TOKENS for token in normalized):
        return "quantity"
    if "x" in normalized or "x" in compact:
        return "dimensions"
    unit_match = re.search(r"([a-z]+)$", compact)
    unit = unit_match.group(1) if unit_match else ""
    if unit in {"w", "watt", "watts", "kw", "mw"}:
        return "power"
    if unit in {"v", "volt", "volts"}:
        return "voltage"
    if unit in {"a", "amp", "amps", "ma"}:
        return "current"
    if unit in {"ah", "mah", "wh", "kwh", "gb", "tb", "mb", "l", "ml", "cl"}:
        return "capacity"
    if unit in {"inch", "inches", "cm", "mm", "m", "ft", "km"}:
        return "size"
    if unit in {"g", "kg", "mg", "lb", "lbs", "oz"}:
        return "weight"
    return "specification"


def _title_decision_parameter_evidence(
    source_title: str,
    *,
    product_type_terms: list[str] | None = None,
) -> dict[str, str] | None:
    """Resolve an explicit parameter under a controlled product-family rule.

    The model may identify the product family, but it never supplies the value.
    A value is eligible only when it is written in the current evidence title.
    """

    source_tokens = _canonical_tokens(source_title)
    type_token_sets = [
        _canonical_tokens(term)
        for term in (product_type_terms or [])
        if " ".join(str(term).split())
    ]
    for rule in TITLE_DECISION_PARAMETER_RULES:
        identity_token_sets = [set(item) for item in rule["identity_token_sets"]]
        if not any(tokens.issubset(source_tokens) for tokens in identity_token_sets):
            continue
        if type_token_sets and not any(
            identity_tokens.issubset(type_tokens)
            for type_tokens in type_token_sets
            for identity_tokens in identity_token_sets
        ):
            continue
        pattern = rule["value_pattern"]
        match = pattern.search(source_title)
        if match is None:
            continue
        raw_value = match.group(1)
        normalized_value = raw_value.rstrip("0").rstrip(".") if "." in raw_value else raw_value
        display_value = f'{normalized_value} {rule["display_unit"]}'
        return {
            "rule": str(rule["rule"]),
            "value": display_value,
            "query_shape": str(rule["query_shape"]),
        }
    return None


def _confirmed_decision_parameter_candidates(
    profile: VisionProfile,
    *,
    source_title: str,
    decision_parameter_values: list[str],
) -> list[SearchKeywordCandidate]:
    """Build bounded live-search probes only from operator-confirmed title values."""

    registry_evidence = _title_decision_parameter_evidence(
        source_title,
        product_type_terms=profile.product_type_terms,
    )
    primary_shape = " ".join(str(profile.product_type_terms[0] or "").split())
    output: list[SearchKeywordCandidate] = []
    seen: set[str] = set()
    for value in decision_parameter_values[:DECISION_PARAMETER_MAX_POSITIVE]:
        normalized_value = " ".join(value.split())
        if not normalized_value:
            continue
        query_shape = primary_shape
        rule = "human_confirmed_product_parameter"
        if (
            registry_evidence is not None
            and str(registry_evidence["value"]).casefold() == normalized_value.casefold()
        ):
            query_shape = str(registry_evidence["query_shape"])
            rule = str(registry_evidence["rule"])
        phrase = " ".join((normalized_value, query_shape)).strip().casefold()
        if (
            not query_shape
            or phrase in seen
            or len(TOKEN_PATTERN.findall(phrase)) > MODEL_DIRECT_QUERY_MAX_WORDS
        ):
            continue
        seen.add(phrase)
        output.append(
            SearchKeywordCandidate(
                phrase=phrase,
                rationale=(
                    "运营已确认该标题参数会影响购买选择；参数值来自当前Seller标题，"
                    "仍须通过同商品族完整搜索页验证后才允许前置"
                ),
                candidate_source="human_confirmed_decision_parameter",
                intended_strategy="core",
                seed_source="human_confirmed_decision_parameter",
                journey_type="human_confirmed_decision_parameter",
                journey_root=phrase,
                journey_path=(phrase,),
                journey_depth=0,
                candidate_provenance=(
                    {
                        "candidate_source": "human_confirmed_decision_parameter",
                        "intended_strategy": "core",
                        "seed_source": "human_confirmed_decision_parameter",
                        "journey_type": "human_confirmed_decision_parameter",
                        "journey_root": phrase,
                        "journey_path": [phrase],
                        "journey_depth": 0,
                        "decision_parameter_rule": rule,
                        "decision_parameter_value": normalized_value,
                        "operator_confirmed": True,
                    },
                ),
            )
        )
    return output


def _canonical_token_parts(token: str) -> tuple[str, ...]:
    canonical = _canonical_token(token)
    return CANONICAL_COMPOUND_TOKEN_PARTS.get(canonical, (canonical,))


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
    unsupported.update((keyword_tokens & FACT_ATTRIBUTE_CLAIM_TOKENS) - source_tokens)
    unsupported.update((keyword_tokens & MEASUREMENT_CLAIM_TOKENS) - source_tokens)
    unsupported.update(
        token
        for token in keyword_tokens - source_tokens
        if token.isdigit() or COMBINED_MEASUREMENT_PATTERN.fullmatch(token)
    )
    raw_keyword_tokens = [
        _canonical_token(token) for token in TOKEN_PATTERN.findall(keyword.casefold())
    ]
    for index, token in enumerate(raw_keyword_tokens[:-1]):
        if token not in {"for", "with"}:
            continue
        compatibility_term = raw_keyword_tokens[index + 1]
        if (
            compatibility_term not in source_tokens
            and compatibility_term not in OPPORTUNITY_COMPATIBILITY_CONTEXT_TOKENS
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
    unsupported_fact_terms = sorted(_unsupported_fact_claim_tokens(keyword, source_title))
    distinctive_tokens = _canonical_tokens(" ".join(distinctive_terms))
    unsupported_distinctive_terms = sorted((new_tokens & distinctive_tokens) - source_tokens)
    safe = (
        not unsupported_fact_terms
        and not unsupported_distinctive_terms
    )
    return {
        "opportunity_claims_safe": safe,
        "opportunity_seed_covers_new_terms": seed_covers_new_terms,
        "opportunity_seed_terms": sorted(seed_union),
        "opportunity_new_terms": sorted(new_tokens),
        "opportunity_unsupported_autocomplete_terms": (unsupported_autocomplete_terms),
        "opportunity_unsupported_fact_terms": unsupported_fact_terms,
        "opportunity_unsupported_distinctive_terms": (unsupported_distinctive_terms),
    }


def _opportunity_safety_rejection_reasons(
    safety: Mapping[str, Any],
) -> list[str]:
    if bool(safety.get("opportunity_claims_safe")):
        return []
    reasons = ["unsupported_high_risk_claim"]
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


def _validated_identity_title_phrase(
    item: KeywordObservation | SearchRankingKeywordResult,
    source_title: str,
) -> str | None:
    """Project a page-verified S query onto a safe product-identity phrase.

    A current title can be wrong about the product noun, so requiring every
    accepted query token to already exist in that title creates a deadlock. We
    only relax that rule when the stored evidence names a verified same-product
    term and the complete first page passes the same-demand competitor density
    plus supply-breadth gate. Unsupported
    feature, material, colour, audience, brand, or specification claims remain
    blocked.
    """

    evidence = (
        item.validation_evidence
        if isinstance(item.validation_evidence, Mapping)
        else {}
    )
    if (
        item.relevance_status != "accepted"
        or str(evidence.get("semantic_relation_grade") or "") != "S"
        or str(evidence.get("page_validation_status") or "") != "completed"
    ):
        return None
    evaluated = (
        _optional_int(evidence.get("semantic_relation_evaluated_result_count"))
        or _optional_int(evidence.get("evaluated_first_page_results"))
        or 0
    )
    same_count = (
        _optional_int(evidence.get("semantic_relation_same_product_result_count"))
        or _optional_int(evidence.get("matched_first_page_results"))
        or 0
    )
    threshold = float(evidence.get("core_threshold") or CORE_MAJORITY_FLOOR)
    core_page_qualified = evidence.get("semantic_relation_core_page_qualified")
    if core_page_qualified is None:
        core_page_qualified = bool(
            evaluated > 0
            and (
                bool(evidence.get("first_page_majority"))
                or (same_count / evaluated) >= max(CORE_MAJORITY_FLOOR, threshold)
            )
        )
    if evaluated <= 0 or not core_page_qualified:
        return None
    raw_terms = evidence.get("semantic_relation_query_same_product_terms")
    same_product_terms = (
        [str(value) for value in raw_terms if str(value).strip()]
        if isinstance(raw_terms, list)
        else []
    )
    if not same_product_terms:
        return None

    keyword_tokens = _title_tokens(item.keyword)
    keyword_parts: list[tuple[str, int]] = []
    for raw_index, raw_token in enumerate(keyword_tokens):
        for canonical_part in _canonical_token_parts(raw_token.casefold()):
            keyword_parts.append(
                (IDENTITY_TOKEN_ALIASES.get(canonical_part, canonical_part), raw_index)
            )
    source_tokens = _canonical_tokens(source_title)
    blocked_new_claims = (
        HIGH_RISK_CLAIM_TOKENS
        | FACT_ATTRIBUTE_CLAIM_TOKENS
        | MEASUREMENT_CLAIM_TOKENS
        | TITLE_PARAMETER_UNIT_TOKENS
    )
    candidates: list[tuple[int, int, int, str]] = []
    for same_product_term in same_product_terms:
        term_tokens = _identity_term_tokens(same_product_term)
        if len(term_tokens) < 2:
            continue
        matched_raw_indexes: list[int] = []
        search_index = 0
        for term_token in term_tokens:
            match = next(
                (
                    (part_index, raw_index)
                    for part_index, (part, raw_index) in enumerate(
                        keyword_parts[search_index:],
                        start=search_index,
                    )
                    if part == term_token
                ),
                None,
            )
            if match is None:
                matched_raw_indexes = []
                break
            search_index = match[0] + 1
            matched_raw_indexes.append(match[1])
        if not matched_raw_indexes:
            continue
        start = min(matched_raw_indexes)
        end = max(matched_raw_indexes)
        phrase_tokens = keyword_tokens[start : end + 1]
        phrase = " ".join(phrase_tokens)
        phrase_canonical = _canonical_tokens(phrase)
        term_canonical = set(term_tokens)
        inserted_context = phrase_canonical - term_canonical
        new_identity_claims = term_canonical - source_tokens
        if inserted_context - source_tokens:
            continue
        if new_identity_claims & blocked_new_claims:
            continue
        if any(
            token.isdigit()
            or COMBINED_MEASUREMENT_PATTERN.fullmatch(token.casefold())
            for token in phrase_tokens
        ):
            continue
        candidates.append((-len(term_tokens), len(phrase_tokens), start, phrase))
    if not candidates:
        return None
    return min(candidates)[3]


def _opportunity_gate_from_result(
    *,
    keyword: str,
    source_title: str,
    found: bool,
    page_number: int | None,
    organic_rank: int | None,
    validation_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Re-evaluate a stored/new S-or-A query under the independent blue-ocean gate."""
    evidence = validation_evidence or {}
    provenance = _evidence_candidate_provenance(evidence)
    platform_expansion_observed = any(
        _is_platform_root_expansion_source(item.get("candidate_source"))
        and _optional_int(item.get("autocomplete_rank")) is not None
        for item in provenance
    )
    semantic_grade = str(evidence.get("semantic_relation_grade") or "")
    semantic_relation_qualified = semantic_grade in {"S", "A"}
    adjacent_page_qualified = bool(
        evidence.get("semantic_relation_adjacent_page_qualified")
    )
    opportunity_candidate = bool(
        platform_expansion_observed and semantic_relation_qualified
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
        stored_direct_count = _optional_int(evidence.get("direct_competitor_count_first_page"))
        if stored_direct_count is not None:
            direct_count = max(
                0,
                stored_direct_count - int(target_counted_as_direct_competitor),
            )
    opportunity_seeds = list(
        dict.fromkeys(
            str(item.get("seed") or "").strip()
            for item in provenance
            if _is_platform_root_expansion_source(item.get("candidate_source"))
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
        and not safety["opportunity_unsupported_fact_terms"]
        and not safety["opportunity_unsupported_distinctive_terms"]
    )
    reasons: list[str] = []
    if not platform_expansion_observed:
        reasons.append("missing_platform_root_expansion")
    if not semantic_relation_qualified:
        reasons.append("semantic_relation_not_s_or_a")
    elif semantic_grade == "A" and not adjacent_page_qualified:
        reasons.append("adjacent_page_coherence_not_qualified")
    reasons.extend(_opportunity_safety_rejection_reasons(safety))
    if direct_count is None:
        reasons.append("missing_direct_competitor_evidence")
    elif direct_count > OPPORTUNITY_MAX_DIRECT_COMPETITORS:
        reasons.append("too_many_direct_competitors")
    if not found:
        reasons.append("target_not_found_within_72")
    elif organic_rank is None or organic_rank > OPPORTUNITY_MAX_ORGANIC_RANK:
        reasons.append("target_beyond_organic_rank_72")
    reasons = list(dict.fromkeys(reasons))
    qualified = not reasons
    return {
        "opportunity_candidate": opportunity_candidate,
        "blue_ocean_candidate": opportunity_candidate,
        "blue_ocean_platform_expansion_observed": platform_expansion_observed,
        "blue_ocean_semantic_relation_grade": semantic_grade or None,
        **safety,
        "target_on_first_page": target_on_first_page,
        "target_counted_as_direct_competitor": target_counted_as_direct_competitor,
        "direct_competitor_count_excluding_target_first_page": direct_count,
        "opportunity_max_direct_competitors": OPPORTUNITY_MAX_DIRECT_COMPETITORS,
        "opportunity_max_organic_rank": OPPORTUNITY_MAX_ORGANIC_RANK,
        "opportunity_qualified": qualified,
        "blue_ocean_qualified": qualified,
        "opportunity_rejection_reasons": reasons,
        "blue_ocean_rejection_reasons": reasons,
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
            "seed": evidence.get("root_expansion_root") or evidence.get("autocomplete_seed"),
            "seed_source": evidence.get("root_expansion_source")
            or evidence.get("autocomplete_seed_source"),
            "autocomplete_rank": evidence.get("root_expansion_rank")
            or evidence.get("autocomplete_rank"),
        }
    ]


def _title_strategy_keywords(
    results: list[KeywordObservation] | list[SearchRankingKeywordResult],
    source_title: str,
    *,
    profile_distinctive_terms: list[str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    accepted_rows = [item for item in results if item.relevance_status == "accepted"]
    accepted_rows.sort(
        key=lambda item: (
            _title_journey_priority(item.validation_evidence),
            item.candidate_order,
            item.keyword.casefold(),
        )
    )
    accepted_title_keywords: list[str] = []
    accepted_title_phrase_by_source_key: dict[str, str] = {}
    for item in accepted_rows:
        supported = _title_supported_keywords([item.keyword], source_title)
        projected_keyword: str | None
        if supported:
            projected_keyword = supported[0]
        else:
            projected_keyword = _validated_identity_title_phrase(item, source_title)
        if not projected_keyword:
            continue
        accepted_title_phrase_by_source_key[item.keyword.casefold()] = projected_keyword
        if projected_keyword.casefold() in {
            value.casefold() for value in accepted_title_keywords
        }:
            continue
        accepted_title_keywords.append(projected_keyword)
    autocomplete_rows: list[tuple[int, int, str, tuple[str, ...]]] = []
    opportunity_title_keywords: list[str] = []
    for item in results:
        evidence = item.validation_evidence if isinstance(item.validation_evidence, Mapping) else {}
        provenance = _evidence_candidate_provenance(evidence)
        autocomplete_rank = min(
            (
                rank
                for source in provenance
                if _is_platform_root_expansion_source(source.get("candidate_source"))
                and (rank := _optional_int(source.get("autocomplete_rank"))) is not None
            ),
            default=None,
        )
        if (
            item.relevance_status == "accepted"
            and item.keyword.casefold() in accepted_title_phrase_by_source_key
            and any(
                _is_platform_root_expansion_source(source.get("candidate_source"))
                for source in provenance
            )
            and autocomplete_rank is not None
        ):
            autocomplete_rows.append(
                (
                    autocomplete_rank,
                    item.candidate_order,
                    accepted_title_phrase_by_source_key[item.keyword.casefold()],
                    _journey_roots_from_evidence(evidence),
                )
            )
        if item.relevance_status != "opportunity":
            continue
        gate_evidence = dict(evidence)
        if (
            profile_distinctive_terms is not None
            and "profile_distinctive_terms" not in gate_evidence
        ):
            gate_evidence["profile_distinctive_terms"] = list(profile_distinctive_terms)
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
    ranked_autocomplete_rows = sorted(
        autocomplete_rows,
        key=lambda item: (item[0], item[1], item[2].casefold()),
    )
    hot_term_keywords: list[str] = []
    used_roots: set[str] = set()
    deferred_rows: list[tuple[int, int, str, tuple[str, ...]]] = []
    for row in ranked_autocomplete_rows:
        roots = set(row[3])
        if roots and not roots.issubset(used_roots):
            hot_term_keywords.append(row[2])
            used_roots.update(roots)
        else:
            deferred_rows.append(row)
    hot_term_keywords.extend(row[2] for row in deferred_rows)
    deduplicated_hot_terms: list[str] = []
    seen_hot_terms: set[str] = set()
    for keyword in hot_term_keywords:
        key = keyword.casefold()
        if key in seen_hot_terms:
            continue
        seen_hot_terms.add(key)
        deduplicated_hot_terms.append(keyword)
    hot_term_keywords = deduplicated_hot_terms
    return (
        accepted_title_keywords,
        hot_term_keywords,
        opportunity_title_keywords,
    )


def _title_journey_priority(evidence: Mapping[str, Any] | Any) -> int:
    if not isinstance(evidence, Mapping):
        return 4
    journey_types = set(_journey_types_from_evidence(evidence))
    if journey_types & {
        "same_product_lexicon_direct",
        "concise_direct",
        "known_long_tail",
    }:
        return 0
    if journey_types & {
        "platform_root_expansion",
        "title_root_expansion",
        "first_instinct_autocomplete",
        "switched_instinct_root",
        "autocomplete_backtrack",
    }:
        return 1
    if journey_types & {"result_page_root_expansion", "result_page_learning"}:
        return 2
    return 3


def _journey_types_from_evidence(evidence: Mapping[str, Any]) -> tuple[str, ...]:
    output: list[str] = []
    top_level = str(evidence.get("journey_type") or "")
    if top_level:
        output.append(top_level)
    for source in _evidence_candidate_provenance(evidence):
        journey_type = str(source.get("journey_type") or "")
        if journey_type and journey_type not in output:
            output.append(journey_type)
    return tuple(output)


def _journey_roots_from_evidence(evidence: Mapping[str, Any]) -> tuple[str, ...]:
    output: list[str] = []
    top_level = " ".join(str(evidence.get("journey_root") or "").split())
    if top_level:
        output.append(top_level)
    for source in _evidence_candidate_provenance(evidence):
        root = " ".join(str(source.get("journey_root") or "").split())
        if root and root not in output:
            output.append(root)
    return tuple(output)


def _title_keyword_journey_evidence(
    results: list[KeywordObservation] | list[SearchRankingKeywordResult],
    *,
    source_title: str = "",
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}

    def merge_row(key: str, row: dict[str, Any]) -> None:
        existing = output.get(key)
        if existing is None:
            output[key] = row
            return
        for field in ("journey_types", "shopper_roots", "paths"):
            merged = list(existing.get(field, []))
            for value in row.get(field, []):
                if value not in merged:
                    merged.append(value)
            existing[field] = merged

    for item in results:
        evidence = item.validation_evidence if isinstance(item.validation_evidence, Mapping) else {}
        paths: list[list[str]] = []
        raw_path = evidence.get("journey_path")
        if isinstance(raw_path, list) and raw_path:
            paths.append([str(value) for value in raw_path if str(value).strip()])
        for source in _evidence_candidate_provenance(evidence):
            raw_source_path = source.get("journey_path")
            if not isinstance(raw_source_path, list):
                continue
            path = [str(value) for value in raw_source_path if str(value).strip()]
            if path and path not in paths:
                paths.append(path)
        row = {
            "journey_types": list(_journey_types_from_evidence(evidence)),
            "shopper_roots": list(_journey_roots_from_evidence(evidence)),
            "paths": paths,
        }
        merge_row(item.keyword.casefold(), row)
        if source_title and (
            projected_keyword := _validated_identity_title_phrase(item, source_title)
        ):
            merge_row(projected_keyword.casefold(), row)
    return output


def _title_root_expansions(
    source_title: str,
    *,
    identity_terms: Sequence[str] = (),
    limit: int = TITLE_ROOT_EXPANSION_LIMIT,
) -> list[str]:
    """Extract auditable word and phrase roots from the current seller title.

    Product-shaped phrases are preferred over isolated modifiers. A few single
    words remain as broad discovery entries, but ``lazy sofa`` and ``sofa chair``
    are no longer discarded merely because they contain more than one word.
    """

    bounded_limit = max(1, limit)
    roots: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        normalized = _complete_root_expansion_input(value)
        key = normalized.casefold()
        if not normalized or key in seen or len(roots) >= bounded_limit:
            return
        seen.add(key)
        roots.append(normalized)

    identity_rows = [
        row
        for value in identity_terms
        if (row := _identity_term_tokens(str(value or "")))
    ]
    title_tokens = _identity_term_tokens(source_title)
    phrase_budget = max(1, bounded_limit - min(3, bounded_limit))

    # Preserve exact multi-word product names and aliases that already occur in
    # the title before generating any surrounding phrase window.
    for value in identity_terms:
        term = _complete_root_expansion_input(str(value or ""))
        term_tokens = _identity_term_tokens(term)
        if len(term_tokens) < 2 or not _contains_token_sequence(title_tokens, term_tokens):
            continue
        add(term)
        if len(roots) >= phrase_budget:
            break

    identity_heads = {
        row[-1]
        for row in identity_rows
        if row and row[-1] not in GENERIC_IDENTITY_HEAD_TOKENS
    }
    generic_identity_rows = [
        row for row in identity_rows if row and row[-1] in GENERIC_IDENTITY_HEAD_TOKENS
    ]
    identity_context_tokens = set().union(*(set(row) for row in identity_rows)) - (
        GENERIC_IDENTITY_HEAD_TOKENS | TITLE_CONNECTOR_TOKENS
    )
    phrase_candidates: list[tuple[int, int, int, str]] = []
    segments = re.split(r"\s+(?:[-–—|:/])\s+|[,;()]", source_title.casefold())
    for segment_index, segment in enumerate(segments):
        segment_tokens = [
            token
            for token in TOKEN_PATTERN.findall(segment)
            if len(token) >= 2
            and token not in TITLE_CONNECTOR_TOKENS
            and token not in TITLE_ROOT_EXPANSION_NOISE_TOKENS
            and token not in _VARIANT_COLOUR_TOKENS
            and token not in TITLE_PARAMETER_UNIT_TOKENS
            and not token.isdigit()
            and not COMBINED_MEASUREMENT_PATTERN.fullmatch(token)
            and any(character.isalpha() for character in token)
        ]
        for phrase_length in range(2, min(4, len(segment_tokens)) + 1):
            for start in range(len(segment_tokens) - phrase_length + 1):
                window = segment_tokens[start : start + phrase_length]
                phrase = " ".join(window)
                phrase_tokens = set(_identity_term_tokens(phrase))
                full_identity_match = any(set(row).issubset(phrase_tokens) for row in identity_rows)
                matched_heads = phrase_tokens & identity_heads
                shared_identity_context = phrase_tokens & identity_context_tokens
                contextual_generic_matches = [
                    row
                    for row in generic_identity_rows
                    if row[-1] in phrase_tokens
                    and bool(
                        ((set(row[:-1]) & phrase_tokens) - GENERIC_IDENTITY_HEAD_TOKENS)
                        or shared_identity_context
                    )
                ]
                if not full_identity_match and not matched_heads and not contextual_generic_matches:
                    continue
                matched_identity = set().union(
                    *(set(row) for row in identity_rows if set(row).issubset(phrase_tokens)),
                    matched_heads,
                    *(
                        ({row[-1]} | (set(row[:-1]) & phrase_tokens))
                        for row in contextual_generic_matches
                    ),
                    shared_identity_context,
                )
                if matched_identity and _semantic_retargets_product(phrase, matched_identity):
                    continue
                score = (
                    (20 if full_identity_match else 0)
                    + (5 * len(matched_heads))
                    + (12 if contextual_generic_matches else 0)
                    + sum(
                        len(set(row[:-1]) & phrase_tokens)
                        for row in contextual_generic_matches
                    )
                    + (3 * len(shared_identity_context))
                    + (6 - phrase_length)
                    + (
                        2
                        if _canonical_token(window[-1])
                        in (
                            matched_heads
                            | {row[-1] for row in contextual_generic_matches}
                        )
                        else 0
                    )
                )
                phrase_candidates.append((score, segment_index, start, phrase))

    for _, _, _, phrase in sorted(
        phrase_candidates,
        key=lambda item: (-item[0], len(item[3].split()), item[1], item[2]),
    ):
        if len(roots) >= phrase_budget:
            break
        add(phrase)

    # Retain a small auditable single-word fallback for discovery. These words
    # are still subject to the product-relevance gate before an expansion can
    # consume a real search-page slot.
    for raw_token in TOKEN_PATTERN.findall(source_title.casefold()):
        token = raw_token.strip()
        if (
            len(token) < 2
            or token in TITLE_CONNECTOR_TOKENS
            or token in TITLE_ROOT_EXPANSION_NOISE_TOKENS
            or token in _VARIANT_COLOUR_TOKENS
            or token in TITLE_PARAMETER_UNIT_TOKENS
            or token.isdigit()
            or COMBINED_MEASUREMENT_PATTERN.fullmatch(token)
            or not any(character.isalpha() for character in token)
        ):
            continue
        add(token)
        if len(roots) >= bounded_limit:
            break
    return roots


def _cross_check_image_profile(
    profile: VisionProfile,
    source_title: str,
    *,
    confirmed_fact_terms: list[str] | tuple[str, ...] = (),
) -> tuple[VisionProfile, dict[str, Any]]:
    """Keep model identity image-only, then compare it with the local seller title."""
    source_tokens = _canonical_tokens(source_title)
    confirmed_fact_source = _title_evidence_source("", list(confirmed_fact_terms))
    confirmed_fact_tokens = _canonical_tokens(confirmed_fact_source)
    supported_identity_source = _title_evidence_source(
        source_title,
        list(confirmed_fact_terms),
    )
    name_tokens = _canonical_tokens(profile.product_name)
    similarity = len(source_tokens & name_tokens) / len(name_tokens) if name_tokens else 0.0
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
        supported_identity_source,
    )
    title_reference_terms: list[str] = []
    confirmed_fact_reference_terms: list[str] = []
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
    for raw in (
        *profile.product_type_terms,
        *profile.distinctive_terms,
        *(candidate.phrase for candidate in profile.keywords),
    ):
        phrase = " ".join(raw.split())
        tokens = _canonical_tokens(phrase)
        if not tokens or not tokens.issubset(confirmed_fact_tokens):
            continue
        if phrase.casefold() in {item.casefold() for item in confirmed_fact_reference_terms}:
            continue
        confirmed_fact_reference_terms.append(phrase)
        if len(confirmed_fact_reference_terms) >= 8:
            break
    normalized = profile.model_copy(update={"product_name": product_name})
    title_identity_check = _title_identity_cross_check(profile, source_title)
    return normalized, {
        "basis": "isolated_image_then_title_cross_check_then_image_title_fusion",
        "visual_stage_received_source_title": False,
        "fusion_stage_received_source_title": True,
        "model_received_source_title": True,
        "model_received_sku": False,
        "original_model_product_name": profile.product_name,
        "product_name_adjusted": copied_or_verbose or bool(removed_identity_terms),
        "removed_unconfirmed_identity_terms": removed_identity_terms,
        "source_title_similarity": round(similarity, 4),
        "title_reference_terms": title_reference_terms,
        "title_root_expansions": _title_root_expansions(
            source_title,
            identity_terms=(*profile.product_type_terms, *profile.same_product_aliases),
        ),
        "confirmed_fact_reference_terms": confirmed_fact_reference_terms,
        "title_reference_role": "post_recognition_cross_check_only",
        **title_identity_check,
    }


def _identity_difference_level(cross_check: Mapping[str, Any]) -> str:
    """Classify the isolated image/title comparison without blocking fusion."""

    similarity = _float_or_none(cross_check.get("source_title_similarity")) or 0.0
    if bool(cross_check.get("title_identity_support")) or similarity >= 0.60:
        return "aligned"
    if similarity >= 0.20:
        return "moderate"
    return "high"


def _manual_fact_requirement(
    profile: VisionProfile,
    *,
    confirmed_fact_terms: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Resolve only explicitly named fusion gaps with current-image confirmations."""

    requested = bool(profile.requires_human_fact_confirmation)
    missing_facts = list(
        dict.fromkeys(
            normalized
            for raw in profile.missing_facts
            if (normalized := " ".join(str(raw).split()))
        )
    )
    confirmed_tokens = _canonical_tokens(" ".join(confirmed_fact_terms))
    unresolved = [
        fact
        for fact in missing_facts
        if not _canonical_tokens(fact)
        or not _canonical_tokens(fact).issubset(confirmed_tokens)
    ]
    resolved = bool(requested and missing_facts and not unresolved)
    required = bool(requested and not resolved)
    reason = " ".join(profile.manual_fact_reason.split())
    if required and not reason:
        reason = "图文融合仍缺少决定安全搜索意图的关键商品事实"
    return {
        "manual_fact_requested_by_fusion_model": requested,
        "manual_fact_required": required,
        "manual_fact_resolved_by_confirmation": resolved,
        "manual_fact_reason": reason if required else None,
        "missing_facts": unresolved if required else [],
        "manual_fact_confirmation_optional": True,
        "batch_action": "skip_without_retry" if required else "continue",
    }


TITLE_SCORE_VERSION = "evidence-title-v2"
TITLE_SCORE_MIN_EVIDENCE_COVERAGE = 70
TITLE_SCORE_COMPONENT_WEIGHTS = {
    "image_title_alignment": 25,
    "product_type_expression": 20,
    "validated_search_term_coverage": 25,
    "evidence_backed_detail_quality": 20,
    "title_readability": 10,
}


def _title_score_non_scoring_signals() -> list[dict[str, str]]:
    return [
        {
            "key": "organic_search_visibility",
            "label": "自然搜索位置",
            "reason": "受价格、库存、广告、排序算法和时间影响，只作为搜索表现旁证",
        },
        {
            "key": "first_page_same_type_relevance",
            "label": "搜索首页同类占比与竞争数",
            "reason": "描述查询和市场竞争，不评价当前主标题文字本身",
        },
        {
            "key": "root_expansion_rank",
            "label": "平台根词扩展顺序",
            "reason": "不是公开搜索量，不参与主标题质量分",
        },
    ]


def _title_score_component_payload(
    *,
    key: str,
    label: str,
    available: bool,
    score: float | None,
    summary: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    weight = TITLE_SCORE_COMPONENT_WEIGHTS[key]
    return {
        "key": key,
        "label": label,
        "weight": weight,
        "available": available,
        "score": (
            round(max(0.0, min(float(score or 0), weight)), 1)
            if available
            else None
        ),
        "max_points": weight if available else 0,
        "summary": summary,
        "evidence": evidence,
    }


def _finalize_title_score_payload(
    *,
    source_title: str,
    components: list[dict[str, Any]],
    extra_limitations: Sequence[str] = (),
) -> dict[str, Any]:
    available_points = sum(
        int(component["weight"])
        for component in components
        if component["available"]
    )
    earned_points = sum(
        float(component["score"] or 0)
        for component in components
        if component["available"]
    )
    score = round((earned_points / available_points) * 100) if available_points else 0
    evidence_coverage = round(available_points)
    if evidence_coverage < TITLE_SCORE_MIN_EVIDENCE_COVERAGE:
        band = "insufficient_evidence"
        label = "证据不足"
    elif score >= 85:
        band = "strong"
        label = "强"
    elif score >= 70:
        band = "solid"
        label = "稳健"
    elif score >= 55:
        band = "needs_improvement"
        label = "待优化"
    else:
        band = "weak"
        label = "弱"
    limitations = [
        "在本轮证据固定的前提下，本分数只评价当前主标题文字与商品身份、已验证搜索表达、事实词和可读性的匹配。",
        "自然排名、搜索首页同类占比、竞争商品数、价格、库存、广告位和平台根词扩展顺序均不参与标题分。",
        (
            "缺失证据不按零分处理，而是从分母中排除并降低证据覆盖率；"
            f"覆盖不足 {TITLE_SCORE_MIN_EVIDENCE_COVERAGE}% 时只显示证据不足档。"
        ),
    ]
    limitations.extend(
        limitation
        for limitation in extra_limitations
        if limitation and limitation not in limitations
    )
    return {
        "score": score,
        "band": band,
        "label": label,
        "evidence_coverage": evidence_coverage,
        "available_points": available_points,
        "earned_points": round(earned_points, 1),
        "current_title": source_title,
        "current_title_match": True,
        "components": components,
        "limitations": limitations,
        "scoring_version": TITLE_SCORE_VERSION,
        "score_scope": "current_title_text_against_frozen_product_and_query_evidence",
        "title_quality_only": True,
        "non_scoring_signals": _title_score_non_scoring_signals(),
    }


def _product_type_title_match(
    ordered_title_tokens: Sequence[str],
    product_type_terms: Sequence[str],
) -> dict[str, Any] | None:
    title_token_set = set(ordered_title_tokens)
    matches: list[dict[str, Any]] = []
    for raw_term in product_type_terms:
        normalized_term = " ".join(str(raw_term).split())
        term_tokens = tuple(dict.fromkeys(_identity_term_tokens(normalized_term)))
        if not normalized_term or not term_tokens:
            continue
        covered_tokens = [token for token in term_tokens if token in title_token_set]
        coverage_ratio = len(covered_tokens) / len(term_tokens)
        start_index = min(
            (
                index
                for index, token in enumerate(ordered_title_tokens)
                if token in covered_tokens
            ),
            default=None,
        )
        contiguous = any(
            tuple(ordered_title_tokens[index : index + len(term_tokens)]) == term_tokens
            for index in range(
                max(0, len(ordered_title_tokens) - len(term_tokens) + 1)
            )
        )
        matches.append(
            {
                "product_type": normalized_term,
                "product_type_tokens": list(term_tokens),
                "covered_tokens": covered_tokens,
                "coverage_ratio": coverage_ratio,
                "start_index": start_index,
                "contiguous": contiguous,
            }
        )
    if not matches:
        return None
    return max(
        matches,
        key=lambda item: (
            float(item["coverage_ratio"]),
            len(item["product_type_tokens"]),
            -(int(item["start_index"]) if item["start_index"] is not None else 10_000),
        ),
    )


def _title_score_payload(
    *,
    source_title: str,
    profile: VisionProfile,
    recognition: Mapping[str, Any],
    observations: list[KeywordObservation] | list[SearchRankingKeywordResult],
    confirmed_fact_terms: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Score only the current title text against frozen, auditable evidence.

    Search position, result-page competition, price, stock, ads, and expansion order
    are deliberately excluded. Holding the evidence fixed, changing those signals
    cannot change this score; only changing the title text can.
    """

    components: list[dict[str, Any]] = []
    difference_level = (
        str(recognition.get("identity_difference_level") or "")
        or (_identity_difference_level(recognition) if recognition else "unknown")
    )
    identity_points_by_level = {"aligned": 25.0, "moderate": 15.0, "high": 5.0}
    identity_available = difference_level in identity_points_by_level
    components.append(
        _title_score_component_payload(
            key="image_title_alignment",
            label="图片与标题商品身份一致性",
            available=identity_available,
            score=identity_points_by_level.get(difference_level),
            summary={
                "aligned": "独立图片观察支持当前标题的商品身份",
                "moderate": "图片与标题存在部分差异，标题身份表达不够稳定",
                "high": "图片与标题差异较大，当前标题存在商品身份偏差风险",
            }.get(difference_level, "缺少可核对的图题身份结论，暂不计分"),
            evidence=[
                {
                    "type": "isolated_cross_validation",
                    "difference_level": difference_level,
                    "source_title_similarity": _float_or_none(
                        recognition.get("source_title_similarity")
                    ),
                    "title_identity_support": bool(
                        recognition.get("title_identity_support")
                    ),
                    "supported_terms": list(
                        recognition.get("title_identity_supported_terms") or []
                    ),
                }
            ]
            if identity_available
            else [],
        )
    )

    ordered_title_tokens = [
        canonical
        for token in TOKEN_PATTERN.findall(source_title.casefold())
        if (canonical := _canonical_token(token))
    ]
    title_tokens = set(ordered_title_tokens)
    product_type_match = _product_type_title_match(
        ordered_title_tokens,
        profile.product_type_terms,
    )
    product_type_available = product_type_match is not None
    type_ratio = (
        float(product_type_match["coverage_ratio"])
        if product_type_match is not None
        else 0.0
    )
    type_start_index = (
        product_type_match.get("start_index")
        if product_type_match is not None
        else None
    )
    type_is_early = type_start_index is not None and int(type_start_index) <= 4
    if type_ratio == 1.0:
        product_type_points = 20.0 if type_is_early else 12.0
    elif type_ratio >= 2 / 3:
        product_type_points = 8.0 if type_is_early else 5.0
    elif type_ratio >= 0.5:
        product_type_points = 3.0
    else:
        product_type_points = 0.0
    components.append(
        _title_score_component_payload(
            key="product_type_expression",
            label="商品类型表达与前置",
            available=product_type_available,
            score=product_type_points,
            summary=(
                f"标题覆盖有依据商品类型“{product_type_match['product_type']}”的 {type_ratio:.0%}，"
                + (
                    f"首个类型词位于第 {int(type_start_index) + 1} 个有效词元"
                    if type_start_index is not None
                    else "未出现可定位的商品类型词"
                )
                if product_type_match is not None
                else "没有可用于核对的商品类型证据，暂不计分"
            ),
            evidence=[
                {
                    "type": "supported_product_type_expression",
                    **product_type_match,
                }
            ]
            if product_type_match is not None
            else [],
        )
    )

    validated = [
        item
        for item in observations
        if item.relevance_status in {"accepted", "opportunity"}
        and getattr(item, "pages_scanned", 0) > 0
    ]
    validated_tokens = _canonical_tokens(
        " ".join(str(item.keyword) for item in validated)
    )
    coverage = (
        len(title_tokens & validated_tokens) / len(validated_tokens)
        if validated_tokens
        else 0.0
    )
    components.append(
        _title_score_component_payload(
            key="validated_search_term_coverage",
            label="平台验证搜索表达覆盖",
            available=bool(validated_tokens),
            score=25 * coverage,
            summary=(
                f"当前标题覆盖 {coverage:.0%} 的已验证搜索词有效词元"
                if validated_tokens
                else "本轮没有可用于标题文字核对的平台验证搜索词"
            ),
            evidence=[
                {
                    "type": "platform_validated_queries",
                    "queries": [str(item.keyword) for item in validated[:8]],
                    "covered_tokens": sorted(title_tokens & validated_tokens),
                    "missing_tokens": sorted(validated_tokens - title_tokens),
                    "coverage_ratio": round(coverage, 4),
                    "rank_and_page_relevance_excluded": True,
                }
            ]
            if validated_tokens
            else [],
        )
    )

    detail_terms = list(
        dict.fromkeys(
            normalized
            for raw in (*profile.distinctive_terms, *confirmed_fact_terms)
            if (normalized := " ".join(str(raw).split()))
        )
    )
    supported_details = [
        term for term in detail_terms if _canonical_tokens(term).issubset(title_tokens)
    ]
    detail_ratio = len(supported_details) / len(detail_terms) if detail_terms else 0.0
    components.append(
        _title_score_component_payload(
            key="evidence_backed_detail_quality",
            label="有依据的卖点与事实表达",
            available=bool(detail_terms),
            score=20 * detail_ratio,
            summary=(
                f"当前标题覆盖 {len(supported_details)}/{len(detail_terms)} 个图文或人工事实词"
                if detail_terms
                else "没有足够的可核对差异化事实，暂不计分"
            ),
            evidence=[
                {
                    "type": "visual_or_confirmed_fact_terms",
                    "available_terms": detail_terms,
                    "covered_terms": supported_details,
                    "coverage_ratio": round(detail_ratio, 4),
                }
            ]
            if detail_terms
            else [],
        )
    )

    word_count = len(ordered_title_tokens)
    if 5 <= word_count <= 18:
        length_points = 6.0
    elif 3 <= word_count <= 24:
        length_points = 4.0
    else:
        length_points = 1.0
    max_repetition = max(
        (ordered_title_tokens.count(token) for token in set(ordered_title_tokens)),
        default=0,
    )
    repetition_points = (
        4.0
        if ordered_title_tokens and max_repetition <= 2
        else 2.0
        if max_repetition == 3
        else 0.0
    )
    components.append(
        _title_score_component_payload(
            key="title_readability",
            label="标题长度、可读性与重复",
            available=bool(ordered_title_tokens),
            score=length_points + repetition_points,
            summary="只按标题有效词元数量和严重重复确定性计分",
            evidence=[
                {
                    "type": "deterministic_title_readability",
                    "word_count": word_count,
                    "maximum_token_repetition": max_repetition,
                    "subscores": {
                        "length": length_points,
                        "repetition": repetition_points,
                    },
                }
            ]
            if ordered_title_tokens
            else [],
        )
    )
    return _finalize_title_score_payload(
        source_title=source_title,
        components=components,
    )


def _project_legacy_title_score(
    raw_score: Mapping[str, Any],
    *,
    source_title: str | None = None,
) -> dict[str, Any] | None:
    """Project stored v1 title scores to v2 without external calls or DB writes."""

    raw_components = raw_score.get("components")
    if not isinstance(raw_components, list):
        return None
    by_key = {
        str(item.get("key") or ""): item
        for item in raw_components
        if isinstance(item, Mapping)
    }
    required_keys = {
        "image_title_alignment",
        "validated_search_term_coverage",
        "title_structure_readability",
        "evidence_backed_detail_quality",
    }
    if not required_keys.issubset(by_key):
        return None

    def scaled_component(
        legacy_key: str,
        *,
        new_key: str,
        label: str,
    ) -> dict[str, Any]:
        legacy = by_key[legacy_key]
        available = bool(legacy.get("available"))
        legacy_weight = _float_or_none(legacy.get("weight")) or 0.0
        legacy_score = _float_or_none(legacy.get("score")) or 0.0
        weight = TITLE_SCORE_COMPONENT_WEIGHTS[new_key]
        return _title_score_component_payload(
            key=new_key,
            label=label,
            available=available and legacy_weight > 0,
            score=(weight * legacy_score / legacy_weight) if legacy_weight else None,
            summary=str(legacy.get("summary") or ""),
            evidence=[
                dict(item)
                for item in legacy.get("evidence", [])
                if isinstance(item, Mapping)
            ],
        )

    structure = by_key["title_structure_readability"]
    structure_evidence = [
        dict(item)
        for item in structure.get("evidence", [])
        if isinstance(item, Mapping)
    ]
    subscores = (
        structure_evidence[0].get("subscores", {})
        if structure_evidence
        else {}
    )
    if not isinstance(subscores, Mapping):
        subscores = {}
    legacy_type = _float_or_none(subscores.get("product_type_position"))
    legacy_length = _float_or_none(subscores.get("length"))
    legacy_repetition = _float_or_none(subscores.get("repetition"))
    type_points = {4.0: 20.0, 2.0: 12.0, 0.0: 0.0}.get(legacy_type or 0.0)
    length_points = {4.0: 6.0, 3.0: 4.0, 1.0: 1.0}.get(legacy_length or 0.0)
    repetition_points = {2.0: 4.0, 1.0: 2.0, 0.0: 0.0}.get(
        legacy_repetition or 0.0
    )
    structure_available = bool(structure.get("available"))
    components = [
        scaled_component(
            "image_title_alignment",
            new_key="image_title_alignment",
            label="图片与标题商品身份一致性",
        ),
        _title_score_component_payload(
            key="product_type_expression",
            label="商品类型表达与前置",
            available=structure_available and type_points is not None,
            score=type_points,
            summary="按旧记录中商品类型是否出现及前置位置本地换算",
            evidence=structure_evidence,
        ),
        scaled_component(
            "validated_search_term_coverage",
            new_key="validated_search_term_coverage",
            label="平台验证搜索表达覆盖",
        ),
        scaled_component(
            "evidence_backed_detail_quality",
            new_key="evidence_backed_detail_quality",
            label="有依据的卖点与事实表达",
        ),
        _title_score_component_payload(
            key="title_readability",
            label="标题长度、可读性与重复",
            available=(
                structure_available
                and length_points is not None
                and repetition_points is not None
            ),
            score=(
                length_points + repetition_points
                if length_points is not None and repetition_points is not None
                else None
            ),
            summary="按旧记录中的标题长度与严重重复子项本地换算",
            evidence=structure_evidence,
        ),
    ]
    legacy_limitations = raw_score.get("limitations")
    extra_limitations = [
        str(item)
        for item in legacy_limitations
        if isinstance(item, str)
        and "代表图" in item
    ] if isinstance(legacy_limitations, list) else []
    projected = _finalize_title_score_payload(
        source_title=(
            " ".join(str(source_title).split())
            if source_title is not None
            else " ".join(str(raw_score.get("current_title") or "").split())
        ),
        components=components,
        extra_limitations=extra_limitations,
    )
    projected["compatibility_projection"] = {
        "source_version": str(raw_score.get("scoring_version") or "evidence-title-v1"),
        "persisted_payload_changed": False,
    }
    return projected


def _normalize_title_score_payload(
    raw_score: Mapping[str, Any] | None,
    *,
    source_title: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(raw_score, Mapping):
        return None
    version = str(raw_score.get("scoring_version") or "")
    if version == TITLE_SCORE_VERSION:
        normalized = dict(raw_score)
        if source_title is not None:
            normalized["current_title"] = " ".join(str(source_title).split())
        normalized.setdefault(
            "score_scope",
            "current_title_text_against_frozen_product_and_query_evidence",
        )
        normalized.setdefault("title_quality_only", True)
        normalized.setdefault(
            "non_scoring_signals",
            _title_score_non_scoring_signals(),
        )
        return normalized
    if version in {"", "evidence-title-v1"}:
        return _project_legacy_title_score(
            raw_score,
            source_title=source_title,
        )
    return None


def _variant_reviews_payload(
    *,
    family_profile: Mapping[str, Any],
    variant_contexts: Sequence[Mapping[str, Any]],
    representative_image_url: str,
    model_profile: VisionProfile,
    visual_profile: VisionProfile,
    observations: list[KeywordObservation] | list[SearchRankingKeywordResult],
) -> list[dict[str, Any]]:
    """Project one shared search chain onto every current Offer title without new I/O."""

    family_variants = family_profile.get("variants")
    variant_by_offer = (
        {
            str(item.get("offer_id") or ""): item
            for item in family_variants
            if isinstance(item, Mapping)
        }
        if isinstance(family_variants, list)
        else {}
    )
    output: list[dict[str, Any]] = []
    for context in variant_contexts:
        offer_id = str(context.get("offer_id") or "").strip()
        title = " ".join(str(context.get("title") or "").split())
        image_url = str(context.get("image_url") or "").strip()
        fact_terms = [
            " ".join(str(value).split())
            for value in context.get("applied_product_fact_terms", [])
            if " ".join(str(value).split())
        ]
        identity_fact_terms = [
            " ".join(str(value).split())
            for value in context.get("applied_identity_fact_terms", [])
            if " ".join(str(value).split())
        ]
        decision_profile = context.get("decision_parameter_profile")
        if not isinstance(decision_profile, Mapping):
            decision_profile = {}
        decision_values = [
            " ".join(str(value).split())
            for value in decision_profile.get("applied_decision_values", [])
            if " ".join(str(value).split())
        ]
        raw_fact_records = context.get("applied_product_fact_records")
        variant_profile = _enrich_profile_with_confirmed_facts(
            model_profile,
            [
                dict(item)
                for item in raw_fact_records
                if isinstance(item, Mapping)
            ]
            if isinstance(raw_fact_records, list)
            else [],
        )
        _, recognition = _cross_check_image_profile(
            visual_profile,
            title,
            confirmed_fact_terms=fact_terms,
        )
        identity_fact_check = _confirmed_identity_fact_cross_check(
            visual_profile,
            identity_fact_terms,
        )
        recognition.update(identity_fact_check)
        difference_level = _identity_difference_level(recognition)
        title_identity_conflict = bool(
            float(recognition["source_title_similarity"]) < IDENTITY_TITLE_SIMILARITY_FLOOR
            and not recognition.get("title_identity_support")
        )
        recognition.update(
            {
                "title_identity_conflict": title_identity_conflict,
                "confirmed_fact_resolved_title_conflict": bool(
                    title_identity_conflict
                    and identity_fact_check["confirmed_identity_fact_support"]
                ),
                "provider_identity_reference_included_confirmed_facts": bool(
                    identity_fact_terms
                ),
                "cross_validation_isolated": True,
                "cross_validation_completed_before_fusion_generation": True,
                "identity_difference_level": difference_level,
                "identity_large_difference": difference_level == "high",
                "identity_difference_warning": (
                    "代表主图与该变体标题差异较大；变体参数仅来自当前 Seller 标题，建议运营抽查。"
                    if difference_level == "high"
                    else (
                        "代表主图与该变体标题存在部分差异；变体参数仍仅作为标题证据。"
                        if difference_level == "moderate"
                        else None
                    )
                ),
                "identity_deviation_branch": (
                    "confirmed_fact_support_continue"
                    if title_identity_conflict
                    and identity_fact_check["confirmed_identity_fact_support"]
                    else (
                        "large_difference_warning"
                        if difference_level == "high"
                        else (
                            "moderate_difference_warning"
                            if difference_level == "moderate"
                            else "title_consistent"
                        )
                    )
                ),
                "image_evidence_scope": "representative_offer_only",
                "current_image_matches_representative": bool(
                    image_url and image_url == representative_image_url
                ),
                "variant_parameters_visually_verified": False,
                "variant_parameter_source": "current_seller_offer_titles",
                "product_fact_profile_applied": bool(fact_terms),
                "product_fact_supported_terms": list(fact_terms),
            }
        )
        recognition.update(
            _manual_fact_requirement(
                variant_profile,
                confirmed_fact_terms=fact_terms,
            )
        )
        evidence_source_title = _title_evidence_source(title, fact_terms)
        accepted, hot_terms, opportunity = _title_strategy_keywords(
            observations,
            evidence_source_title,
            profile_distinctive_terms=list(variant_profile.distinctive_terms),
        )
        title_strategies = _build_title_strategies(
            source_title=title,
            evidence_source_title=evidence_source_title,
            accepted_keywords=accepted,
            hot_term_keywords=hot_terms,
            opportunity_keywords=opportunity,
            validated_core_keywords=accepted,
            decision_parameter_values=decision_values,
            keyword_journey_evidence=_title_keyword_journey_evidence(
                observations,
                source_title=evidence_source_title,
            ),
        )
        title_suggestion = str(
            title_strategies[0]["title"]
            or _build_title_suggestion(title, accepted)
        )
        title_score = _title_score_payload(
            source_title=title,
            profile=variant_profile,
            recognition=recognition,
            observations=observations,
            confirmed_fact_terms=fact_terms,
        )
        if image_url != representative_image_url:
            title_score.setdefault("limitations", []).append(
                "该分数复用商品族代表图和同一组搜索页证据；代表图不验证此 Offer 的颜色、尺寸、容量等变体值。"
            )
        family_variant = variant_by_offer.get(offer_id)
        variant_parameters = (
            list(family_variant.get("parameters") or [])
            if isinstance(family_variant, Mapping)
            else []
        )
        output.append(
            {
                "offer_id": offer_id,
                "title": title,
                "image_url": image_url,
                "variant_parameters": variant_parameters,
                "applied_product_fact_terms": fact_terms,
                "decision_parameter_profile": dict(decision_profile),
                "recognition": recognition,
                "title_score": title_score,
                "title_suggestion": title_suggestion,
                "title_reason": _title_suggestion_reason(
                    accepted,
                    validated_keyword_count=sum(
                        item.relevance_status == "accepted" for item in observations
                    ),
                ),
                "title_strategies": title_strategies,
                "opportunity_title_suggestion": title_strategies[2]["title"],
                "opportunity_title_reason": (
                    _opportunity_title_reason(opportunity)
                    if title_strategies[2]["title"]
                    else None
                ),
            }
        )
    return output


def _title_identity_cross_check(
    profile: VisionProfile,
    source_title: str,
) -> dict[str, Any]:
    """Confirm that the local title contains the image product subject.

    Non-generic product heads such as ``sofa`` or ``mouse`` establish the same
    broad product family. Generic heads such as ``light`` or ``bar`` require the
    model's two-token physical-form tail, so attributes like RGB or outdoor
    cannot resolve an unrelated-product conflict by themselves.
    """

    title_ordered_tokens = _identity_term_tokens(source_title)
    model_name_ordered_tokens = _identity_term_tokens(profile.product_name)
    rows: list[dict[str, Any]] = []
    for raw_term in profile.product_type_terms:
        term = " ".join(str(raw_term).split())
        model_ordered_tokens = _identity_term_tokens(term)
        if not term or not model_ordered_tokens:
            continue
        model_head = model_ordered_tokens[-1]
        model_name_supported, _ = _identity_type_tokens_match(
            model_ordered_tokens,
            model_name_ordered_tokens,
        )
        title_supported, title_match_rule = _identity_type_tokens_match(
            model_ordered_tokens,
            title_ordered_tokens,
        )
        supported = bool(model_name_supported and title_supported)
        if not model_name_supported:
            match_rule = "model_type_term_conflicts_with_product_name"
        else:
            match_rule = title_match_rule
        rows.append(
            {
                "term": term,
                "identity_supported": supported,
                "matched_identity_anchor": model_head if supported else None,
                "identity_match_rule": match_rule,
                "model_product_name_supported": model_name_supported,
            }
        )
    supported_terms = [str(item["term"]) for item in rows if item["identity_supported"]]
    return {
        "title_identity_support": bool(supported_terms),
        "title_identity_supported_terms": supported_terms,
        "title_identity_matches": rows,
        "title_identity_decision_rule": (
            "product_subject_or_controlled_generic_form_not_modifier_overlap"
        ),
    }


def _identity_type_tokens_match(
    product_type_tokens: tuple[str, ...],
    reference_tokens: tuple[str, ...],
) -> tuple[bool, str]:
    product_head = product_type_tokens[-1]
    exact_type_sequence = _contains_token_sequence(
        reference_tokens,
        product_type_tokens,
    )
    generic_tail_sequence = bool(
        product_head in GENERIC_IDENTITY_HEAD_TOKENS
        and len(product_type_tokens) >= 2
        and _contains_token_sequence(
            reference_tokens,
            product_type_tokens[-2:],
        )
    )
    product_subject_match = bool(
        product_head not in GENERIC_IDENTITY_HEAD_TOKENS and product_head in set(reference_tokens)
    )
    if exact_type_sequence:
        return True, "exact_product_type_sequence"
    if generic_tail_sequence:
        return True, "matching_two_token_tail_for_generic_head"
    if product_subject_match:
        return True, "product_subject_or_alias_match"
    if product_head in set(reference_tokens):
        return False, "generic_head_without_matching_tail_rejected"
    return False, "no_product_subject_overlap"


def _contains_token_sequence(
    haystack: tuple[str, ...],
    needle: tuple[str, ...],
) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    return any(
        haystack[index : index + len(needle)] == needle
        for index in range(len(haystack) - len(needle) + 1)
    )


def _confirmed_identity_fact_cross_check(
    profile: VisionProfile,
    fact_terms: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Match product-family subjects; modifiers alone never resolve identity."""

    model_type_rows = [
        _identity_term_tokens(term)
        for term in profile.product_type_terms
        if _identity_term_tokens(term)
    ]
    model_tokens = set().union(*map(set, model_type_rows)) if model_type_rows else set()
    rows: list[dict[str, Any]] = []
    for raw_term in fact_terms:
        term = " ".join(str(raw_term).split())
        fact_ordered_tokens = _identity_term_tokens(term)
        fact_tokens = set(fact_ordered_tokens)
        if not term or not fact_tokens:
            continue
        overlap = fact_tokens & model_tokens
        score = len(overlap) / len(fact_tokens)
        fact_head = fact_ordered_tokens[-1]
        matched_anchors: set[str] = set()
        generic_head_phrase_match = False
        generic_head_overlap = False
        for model_ordered_tokens in model_type_rows:
            if not model_ordered_tokens:
                continue
            model_head = model_ordered_tokens[-1]
            for anchor in {fact_head, model_head}:
                if anchor not in fact_tokens or anchor not in set(model_ordered_tokens):
                    continue
                if anchor in GENERIC_IDENTITY_HEAD_TOKENS:
                    generic_head_overlap = True
                    if (
                        len(fact_ordered_tokens) >= 2
                        and len(model_ordered_tokens) >= 2
                        and fact_ordered_tokens[-2:] == model_ordered_tokens[-2:]
                    ):
                        matched_anchors.add(anchor)
                        generic_head_phrase_match = True
                    continue
                matched_anchors.add(anchor)
        supported = bool(matched_anchors)
        if supported:
            match_rule = (
                "matching_two_token_tail_for_generic_head"
                if generic_head_phrase_match
                else "product_subject_or_alias_match"
            )
        elif generic_head_overlap:
            match_rule = "generic_head_without_matching_tail_rejected"
        elif overlap:
            match_rule = "modifier_only_overlap_rejected"
        else:
            match_rule = "no_product_subject_overlap"
        rows.append(
            {
                "term": term,
                "similarity": round(score, 4),
                "matched_tokens": sorted(overlap),
                "matched_identity_anchors": sorted(matched_anchors),
                "rejected_modifier_overlap": (sorted(overlap) if overlap and not supported else []),
                "identity_supported": supported,
                "identity_match_rule": match_rule,
            }
        )
    best_score = max(
        (float(item["similarity"]) for item in rows),
        default=0.0,
    )
    supported_terms = [str(item["term"]) for item in rows if item["identity_supported"]]
    return {
        "confirmed_identity_fact_terms": [item["term"] for item in rows],
        "confirmed_identity_fact_matches": rows,
        "confirmed_identity_fact_similarity": round(best_score, 4),
        "confirmed_identity_fact_similarity_decides_support": False,
        "confirmed_identity_fact_support": bool(supported_terms),
        "confirmed_identity_fact_supported_terms": supported_terms,
        "confirmed_identity_fact_decision_rule": ("product_subject_or_alias_not_modifier_overlap"),
    }


def _identity_term_tokens(value: str) -> tuple[str, ...]:
    output: list[str] = []
    for raw_token in TOKEN_PATTERN.findall(value.casefold()):
        for canonical_part in _canonical_token_parts(raw_token):
            token = IDENTITY_TOKEN_ALIASES.get(canonical_part, canonical_part)
            if not token or token in TITLE_CONNECTOR_TOKENS:
                continue
            output.append(token)
    return tuple(output)


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


def _same_product_lexicon(
    profile: VisionProfile,
    *,
    model_profile: VisionProfile | None = None,
    confirmed_fact_records: Sequence[Mapping[str, Any]] = (),
    source_title: str = "",
) -> dict[str, Any]:
    """Build one auditable, query-ready same-product lexicon.

    Only concise product identities are admitted.  Operator facts are included
    when they explicitly confirm the product type, or when the existing fact
    enrichment gate has already accepted the exact phrase as a same-product
    alias.  Materials, uses, and free-standing feature phrases remain outside
    this identity lexicon.
    """

    raw_model_profile = model_profile or profile
    primary_type_tokens = _identity_term_tokens(raw_model_profile.product_type_terms[0])
    source_title_tokens = _identity_term_tokens(source_title)
    enriched_alias_keys = {
        " ".join(str(value or "").split()).casefold()
        for value in profile.same_product_aliases
        if " ".join(str(value or "").split())
    }
    entries: list[dict[str, Any]] = []
    indexes: dict[str, int] = {}
    excluded: list[dict[str, Any]] = []
    excluded_keys: set[tuple[str, str, str]] = set()

    def add(raw_term: Any, source: str) -> None:
        term = " ".join(str(raw_term or "").split())
        if not term:
            return
        word_count = len(TOKEN_PATTERN.findall(term.casefold()))
        if not MODEL_DIRECT_QUERY_MIN_WORDS <= word_count <= MODEL_DIRECT_QUERY_MAX_WORDS:
            reason = "outside_2_to_4_words"
            excluded_key = (term.casefold(), source, reason)
            if excluded_key not in excluded_keys:
                excluded_keys.add(excluded_key)
                excluded.append(
                    {
                        "term": term,
                        "source": source,
                        "word_count": word_count,
                        "reason": reason,
                    }
                )
            return
        term_tokens = _identity_term_tokens(term)
        broad_head = bool(
            term_tokens
            and term_tokens[-1] in LEXICON_AMBIGUOUS_IDENTITY_HEAD_TOKENS
        )
        broad_identity_supported = bool(
            term_tokens
            and (
                term_tokens == primary_type_tokens
                or _contains_token_sequence(source_title_tokens, term_tokens)
                or (
                    len(term_tokens) >= 2
                    and len(primary_type_tokens) >= 2
                    and term_tokens[-2:] == primary_type_tokens[-2:]
                )
            )
        )
        if source.startswith("fusion_") and broad_head and not broad_identity_supported:
            reason = "broad_identity_head_without_title_or_primary_shape"
            excluded_key = (term.casefold(), source, reason)
            if excluded_key not in excluded_keys:
                excluded_keys.add(excluded_key)
                excluded.append(
                    {
                        "term": term,
                        "source": source,
                        "word_count": word_count,
                        "reason": reason,
                    }
                )
            return
        key = term.casefold()
        index = indexes.get(key)
        if index is None:
            indexes[key] = len(entries)
            entries.append(
                {
                    "term": term,
                    "sources": [source],
                    "word_count": word_count,
                    "direct_query_eligible": True,
                }
            )
            return
        sources = entries[index]["sources"]
        if source not in sources:
            sources.append(source)

    for record in confirmed_fact_records:
        fact_type = str(record.get("fact_type") or "product_type")
        term = " ".join(str(record.get("fact_term") or "").split())
        key = term.casefold()
        if fact_type == "product_type" or (
            fact_type in {"construction", "function", "packaging"}
            and key in enriched_alias_keys
        ):
            add(term, "human_confirmed_product_fact")
    for term in _seller_title_identity_query_terms(source_title, raw_model_profile):
        add(term, "seller_title_identity_phrase")
    for term in raw_model_profile.product_type_terms:
        add(term, "fusion_product_type_terms")
    for term in raw_model_profile.same_product_aliases:
        add(term, "fusion_same_product_aliases")

    return {
        "policy_version": SAME_PRODUCT_LEXICON_POLICY_VERSION,
        "selection_policy": (
            "manual_identity_then_title_phrase_then_fusion_types_then_aliases"
        ),
        "search_use": "priority_direct_query_and_complete_root_expansion",
        "direct_query_limit": MODEL_DIRECT_QUERY_TARGET,
        "complete_root_expansion_limit": SAME_PRODUCT_LEXICON_ROOT_LIMIT,
        "entries": entries,
        "excluded": excluded,
    }


def _same_product_lexicon_root_source(entry: Mapping[str, Any]) -> str:
    sources = entry.get("sources")
    if isinstance(sources, list) and "human_confirmed_product_fact" in sources:
        return "human_confirmed_product_fact"
    return "image_title_same_product_lexicon"


def _precise_candidates(
    profile: VisionProfile,
    *,
    source_title: str | None = None,
    same_product_lexicon: Mapping[str, Any] | None = None,
) -> list[SearchKeywordCandidate]:
    # Image-model queries are hypotheses for live page validation, not title
    # claims. Do not discard them merely because words such as stand/tripod are
    # absent from the current title; the stricter title-suggestion gate remains
    # in _title_supported_keywords.
    _ = source_title
    lexicon = dict(same_product_lexicon or _same_product_lexicon(profile))
    candidate_rows: list[tuple[str, str, int, tuple[str, ...]]] = []
    raw_lexicon_entries = lexicon.get("entries")
    if isinstance(raw_lexicon_entries, list):
        for entry in raw_lexicon_entries:
            if not isinstance(entry, Mapping):
                continue
            raw_sources = entry.get("sources")
            sources = tuple(
                dict.fromkeys(
                    str(value)
                    for value in raw_sources
                    if str(value).strip()
                )
            ) if isinstance(raw_sources, list) else ()
            candidate_rows.append(
                (
                    str(entry.get("term") or ""),
                    "同品词库中的精准商品名称，优先用于直接搜索页验证",
                    0,
                    sources,
                )
            )
    for candidate in profile.keywords:
        candidate_rows.append(
            (
                candidate.phrase,
                candidate.rationale,
                1,
                (),
            )
        )

    concise_rows: list[tuple[int, int, str, str, tuple[str, ...]]] = []
    seen: set[str] = set()
    for raw_phrase, rationale, fallback_order, lexicon_sources in candidate_rows:
        phrase = " ".join(raw_phrase.split())
        word_count = len(TOKEN_PATTERN.findall(phrase.casefold()))
        key = phrase.casefold()
        if (
            key in seen
            or word_count < MODEL_DIRECT_QUERY_MIN_WORDS
            or word_count > MODEL_DIRECT_QUERY_MAX_WORDS
        ):
            continue
        seen.add(key)
        concise_rows.append(
            (
                fallback_order,
                0 if word_count <= MODEL_DIRECT_QUERY_PREFERRED_MAX_WORDS else 1,
                phrase,
                rationale.strip(),
                lexicon_sources,
            )
        )

    output: list[SearchKeywordCandidate] = []
    for _, _, phrase, rationale, lexicon_sources in sorted(
        concise_rows,
        key=lambda row: row[0],
    ):
        from_lexicon = bool(lexicon_sources)
        candidate_source = (
            "same_product_lexicon" if from_lexicon else "image_title_fused_precise"
        )
        seed_source = (
            "human_confirmed_product_fact"
            if "human_confirmed_product_fact" in lexicon_sources
            else (
                "image_title_same_product_lexicon"
                if from_lexicon
                else "image_title_fusion_model"
            )
        )
        journey_type = "same_product_lexicon_direct" if from_lexicon else "concise_direct"
        output.append(
            SearchKeywordCandidate(
                phrase=phrase,
                rationale=rationale,
                candidate_source=candidate_source,
                intended_strategy="core",
                seed=phrase if from_lexicon else None,
                seed_source=seed_source,
                journey_type=journey_type,
                journey_root=phrase,
                journey_path=(phrase,),
                journey_depth=0,
                candidate_provenance=(
                    {
                        "candidate_source": candidate_source,
                        "intended_strategy": "core",
                        "seed": phrase if from_lexicon else None,
                        "seed_source": seed_source,
                        "journey_type": journey_type,
                        "journey_root": phrase,
                        "journey_path": [phrase],
                        "journey_depth": 0,
                        "same_product_lexicon_sources": list(lexicon_sources),
                        "same_product_lexicon_policy_version": (
                            SAME_PRODUCT_LEXICON_POLICY_VERSION if from_lexicon else None
                        ),
                    },
                ),
            )
        )
    return output


def _normalized_adjacent_demand_candidates(
    values: Sequence[KeywordCandidate],
) -> list[KeywordCandidate]:
    output: list[KeywordCandidate] = []
    for candidate in values:
        phrase = " ".join(candidate.phrase.split())
        buyer_job = " ".join(candidate.buyer_job.split())
        alternatives = list(
            dict.fromkeys(
                normalized
                for value in candidate.alternative_product_terms
                if (normalized := " ".join(str(value or "").split()))
            )
        )
        if not phrase:
            continue
        output.append(
            candidate.model_copy(
                update={
                    "phrase": phrase,
                    "buyer_job": buyer_job,
                    "alternative_product_terms": alternatives,
                    "excluded_product_terms": list(
                        dict.fromkeys(
                            normalized
                            for value in candidate.excluded_product_terms
                            if (normalized := " ".join(str(value or "").split()))
                        )
                    ),
                }
            )
        )
    return output


async def _discover_keyword_candidates(
    client: SearchPublicClient,
    *,
    profile: VisionProfile,
    source_title: str,
    title_reference_terms: list[str],
    decision_parameter_values: list[str] | None = None,
    max_keywords: int,
    official_title: str | None = None,
    confirmed_fact_records: Sequence[Mapping[str, Any]] = (),
    same_product_lexicon: Mapping[str, Any] | None = None,
    model_autocomplete_seeds: Sequence[KeywordCandidate] | None = None,
    model_opportunity_seeds: Sequence[KeywordCandidate] | None = None,
) -> tuple[list[SearchKeywordCandidate], list[dict[str, Any]]]:
    lexicon = dict(same_product_lexicon or _same_product_lexicon(profile))
    precise = _precise_candidates(
        profile,
        source_title=source_title,
        same_product_lexicon=lexicon,
    )
    title_roots = _title_root_expansions(
        official_title or source_title,
        identity_terms=(*profile.product_type_terms, *profile.same_product_aliases),
    )
    manual_fact_seeds = _confirmed_fact_root_seed_specs(confirmed_fact_records)
    lexicon_core_seeds = _same_product_lexicon_root_seed_specs(lexicon)
    title_reference_seeds = _complete_root_seed_specs(
        [
            KeywordCandidate(
                phrase=term,
                rationale="主标题中与图片识别一致的词根，仅在识别后用于平台扩展交叉核对",
            )
            for term in title_reference_terms
            if 2 <= len(term) <= 100
        ],
        seed_source="title_cross_check",
        intended_strategy="core",
    )
    image_core_seeds = _complete_root_seed_specs(
        list(model_autocomplete_seeds or profile.autocomplete_seeds),
        seed_source="image_title_first_instinct",
        intended_strategy="core",
    )
    title_core_seeds = [
        (
            KeywordCandidate(
                phrase=root,
                rationale="当前主标题中的完整词根或产品短语，直接用于 Takealot 平台扩展验证",
            ),
            "title_word_root",
            "core",
        )
        for root in title_roots
    ]
    decision_parameter_candidates = _confirmed_decision_parameter_candidates(
        profile,
        source_title=source_title,
        decision_parameter_values=decision_parameter_values or [],
    )
    manual_core_seeds = [item for item in manual_fact_seeds if item[2] == "core"]
    manual_opportunity_seeds = [
        item for item in manual_fact_seeds if item[2] == "opportunity"
    ]
    prioritized_core_seeds = [
        *manual_core_seeds,
        *lexicon_core_seeds,
        *image_core_seeds,
        *title_core_seeds,
        *title_reference_seeds,
    ]
    bounded_core_seeds = _bounded_core_seed_specs_with_title_coverage(
        prioritized_core_seeds,
        title_core_seeds,
        limit=ROOT_EXPANSION_CORE_ROOT_LIMIT,
    )
    model_opportunity_candidates = _normalized_adjacent_demand_candidates(
        list(model_opportunity_seeds or profile.opportunity_seeds)
    )
    model_opportunity_roots = _complete_root_seed_specs(
        model_opportunity_candidates,
        seed_source="image_title_need_state",
        intended_strategy="opportunity",
    )
    opportunity_seeds = [*manual_opportunity_seeds, *model_opportunity_roots]
    selected_opportunity_roots = {
        " ".join(candidate.phrase.split()).casefold()
        for candidate, _ in _group_seed_specs(opportunity_seeds)[
            :ROOT_EXPANSION_OPPORTUNITY_ROOT_LIMIT
        ]
    }
    prioritized_seed_specs = [
        *manual_fact_seeds,
        *lexicon_core_seeds,
        *image_core_seeds,
        *title_core_seeds,
        *model_opportunity_roots,
        *title_reference_seeds,
    ]
    selected_core_roots = {
        " ".join(item[0].phrase.split()).casefold() for item in bounded_core_seeds
    }
    selected_root_specs = [
        item
        for item in prioritized_seed_specs
        if (
            item[2] == "core"
            and " ".join(item[0].phrase.split()).casefold() in selected_core_roots
        )
        or (
            item[2] == "opportunity"
            and " ".join(item[0].phrase.split()).casefold()
            in selected_opportunity_roots
        )
    ]
    root_specs = _group_seed_specs(
        selected_root_specs
    )[:ROOT_EXPANSION_INPUT_LIMIT]
    root_origin_phrases = _root_seed_origin_phrases(
        confirmed_fact_records=confirmed_fact_records,
        same_product_lexicon=lexicon,
        title_reference_terms=title_reference_terms,
        model_autocomplete_seeds=list(model_autocomplete_seeds or profile.autocomplete_seeds),
        title_roots=title_roots,
        model_opportunity_seeds=model_opportunity_candidates,
    )

    root_expansions: list[tuple[int, float, SearchKeywordCandidate]] = []
    checks: list[dict[str, Any]] = []
    observed_root_keys: set[str] = set()
    followup_roots: list[dict[str, Any]] = []

    async def observe_root(
        *,
        root_order: int,
        seed: KeywordCandidate,
        seed_intents: tuple[tuple[str, str], ...],
        journey_path: tuple[str, ...],
        journey_depth: int,
        origin_phrases: list[str],
        parent_root: str | None = None,
        allow_followup: bool,
    ) -> None:
        normalized_seed = " ".join(seed.phrase.split())
        root_key = normalized_seed.casefold()
        if not normalized_seed or root_key in observed_root_keys:
            return
        observed_root_keys.add(root_key)
        primary_seed_source, primary_strategy = seed_intents[0]
        primary_journey_type = (
            "platform_expansion_followup"
            if journey_depth > 0
            else _root_expansion_journey_type(
                seed_source=primary_seed_source,
                intended_strategy=primary_strategy,
            )
        )
        check_base = {
            "seed": normalized_seed,
            "root": normalized_seed,
            "input_kind": "complete_root_expansion",
            "seed_source": primary_seed_source,
            "root_source": primary_seed_source,
            "shopper_root": normalized_seed,
            "input_state": normalized_seed,
            "journey_path": list(journey_path),
            "journey_type": primary_journey_type,
            "journey_depth": journey_depth,
            "parent_root": parent_root,
            "seed_sources": list(dict.fromkeys(item[0] for item in seed_intents)),
            "origin_phrases": origin_phrases,
            "intended_strategies": list(dict.fromkeys(item[1] for item in seed_intents)),
            "raw_suggestions_are_selected": False,
            "selection_policy": (
                "same_product_identity_or_structured_adjacent_product_family"
            ),
        }
        try:
            suggestions = (await client.fetch_search_suggestions(normalized_seed))[
                :AUTOCOMPLETE_RESULT_LIMIT
            ]
        except Exception as exc:
            checks.append(
                {
                    **check_base,
                    "status": "unavailable",
                    "error_type": type(exc).__name__,
                }
            )
            return

        cache_evidence = _autocomplete_cache_evidence(client, normalized_seed)
        expansion_rows: list[dict[str, Any]] = []
        for rank, phrase in enumerate(suggestions, start=1):
            normalized_phrase = " ".join(phrase.split())
            decision = _root_expansion_relevance_decision(
                normalized_phrase,
                profile,
                source_title=source_title,
            )
            if decision["relation"] == "adjacent_demand" and not any(
                intended_strategy == "opportunity"
                for _, intended_strategy in seed_intents
            ):
                decision = {
                    "accepted": False,
                    "relation": "irrelevant",
                    "reason": "adjacent_family_requires_structured_opportunity_root",
                    "matched_terms": list(decision["matched_terms"]),
                }
            fit = _autocomplete_fit_score(
                normalized_phrase,
                profile,
                source_title=source_title,
            )
            query_word_count = len(TOKEN_PATTERN.findall(normalized_phrase.casefold()))
            query_length_status = (
                "eligible"
                if query_word_count <= MODEL_DIRECT_QUERY_MAX_WORDS
                else "rejected_too_long"
            )
            expansion_evidence = {
                "phrase": normalized_phrase,
                "rank": rank,
                "relevance_status": (
                    "eligible" if decision["accepted"] else "rejected_irrelevant"
                ),
                "relation": decision["relation"],
                "reason": decision["reason"],
                "matched_terms": list(decision["matched_terms"]),
                "query_word_count": query_word_count,
                "query_length_status": query_length_status,
                "used_as_followup_root": False,
            }
            expansion_rows.append(expansion_evidence)
            if (
                not decision["accepted"]
                or fit <= 0
                or query_length_status != "eligible"
            ):
                continue

            candidate_journey_path = tuple(
                dict.fromkeys((*journey_path, normalized_phrase))
            )
            root_expansions.append(
                (
                    root_order,
                    fit - (rank * 0.01) - (root_order * 0.001),
                    SearchKeywordCandidate(
                        phrase=normalized_phrase,
                        rationale=seed.rationale.strip(),
                        candidate_source="takealot_root_expansion",
                        intended_strategy=(
                            "opportunity"
                            if decision["relation"] == "adjacent_demand"
                            else primary_strategy
                        ),
                        seed=normalized_seed,
                        seed_source=primary_seed_source,
                        autocomplete_rank=rank,
                        journey_type=primary_journey_type,
                        journey_root=normalized_seed,
                        journey_path=candidate_journey_path,
                        journey_depth=journey_depth,
                        candidate_provenance=tuple(
                            {
                                "candidate_source": "takealot_root_expansion",
                                "intended_strategy": intended_strategy,
                                "seed": origin_phrases[index]
                                if index < len(origin_phrases)
                                else normalized_seed,
                                "root": normalized_seed,
                                "root_expansion_origin_phrase": origin_phrases[index]
                                if index < len(origin_phrases)
                                else normalized_seed,
                                "seed_source": seed_source,
                                "root_source": seed_source,
                                "autocomplete_rank": rank,
                                "root_expansion_rank": rank,
                                "root_expansion_relation": decision["relation"],
                                "root_expansion_selection_reason": decision["reason"],
                                "journey_type": primary_journey_type,
                                "journey_root": normalized_seed,
                                "journey_path": list(candidate_journey_path),
                                "journey_depth": journey_depth,
                                "journey_parent_root": parent_root,
                                "autocomplete_cache_status": cache_evidence.get("cache_status"),
                                "autocomplete_observed_at": cache_evidence.get("captured_at"),
                                "autocomplete_cache_age_hours": cache_evidence.get("age_hours"),
                                "autocomplete_cache_ttl_hours": cache_evidence.get("ttl_hours"),
                                "autocomplete_shared_across_stores": cache_evidence.get(
                                    "shared_across_stores"
                                ),
                            }
                            for index, (seed_source, intended_strategy) in enumerate(seed_intents)
                        ),
                    ),
                )
            )
            if (
                allow_followup
                and len(TOKEN_PATTERN.findall(normalized_phrase.casefold())) >= 2
                and normalized_phrase.casefold() != root_key
            ):
                followup_roots.append(
                    {
                        "priority": (
                            root_order,
                            0 if decision["relation"] == "same_product" else 1,
                            rank,
                            -fit,
                        ),
                        "seed": KeywordCandidate(
                            phrase=normalized_phrase,
                            rationale=(
                                "与目标商品相关的平台扩展词，继续作为完整词组词根观察下一层扩展"
                            ),
                        ),
                        "seed_intents": seed_intents,
                        "journey_path": candidate_journey_path,
                        "origin_phrases": origin_phrases,
                        "parent_root": normalized_seed,
                        "expansion_evidence": expansion_evidence,
                    }
                )

        checks.append(
            {
                **check_base,
                "status": "observed",
                "suggestions": suggestions,
                "expansions": expansion_rows,
                "eligible_expansion_count": sum(
                    item["relevance_status"] == "eligible"
                    and item["query_length_status"] == "eligible"
                    for item in expansion_rows
                ),
                "rejected_expansion_count": sum(
                    item["relevance_status"] == "rejected_irrelevant"
                    or item["query_length_status"] == "rejected_too_long"
                    for item in expansion_rows
                ),
                "related_but_too_long_count": sum(
                    item["relevance_status"] == "eligible"
                    and item["query_length_status"] == "rejected_too_long"
                    for item in expansion_rows
                ),
                **cache_evidence,
            }
        )

    for root_order, (seed, seed_intents) in enumerate(root_specs):
        normalized_seed = " ".join(seed.phrase.split())
        origin_phrases = list(
            dict.fromkeys(
                root_origin_phrases.get(
                    (normalized_seed.casefold(), seed_source, intended_strategy),
                    normalized_seed,
                )
                for seed_source, intended_strategy in seed_intents
            )
        )
        await observe_root(
            root_order=root_order,
            seed=seed,
            seed_intents=seed_intents,
            journey_path=(normalized_seed,),
            journey_depth=0,
            origin_phrases=origin_phrases,
            allow_followup=True,
        )

    followup_count = 0
    for followup in sorted(followup_roots, key=lambda item: item["priority"]):
        if (
            followup_count >= ROOT_EXPANSION_FOLLOWUP_ROOT_LIMIT
            or len(checks) >= ROOT_EXPANSION_INPUT_LIMIT
        ):
            break
        followup_seed = followup["seed"]
        if followup_seed.phrase.casefold() in observed_root_keys:
            continue
        followup["expansion_evidence"]["used_as_followup_root"] = True
        await observe_root(
            root_order=len(root_specs) + followup_count,
            seed=followup_seed,
            seed_intents=followup["seed_intents"],
            journey_path=followup["journey_path"],
            journey_depth=1,
            origin_phrases=followup["origin_phrases"],
            parent_root=followup["parent_root"],
            allow_followup=False,
        )
        followup_count += 1

    seller_title_targets: list[SearchKeywordCandidate] = []
    for root in title_roots:
        normalized_root = " ".join(root.split())
        if (
            len(_identity_term_tokens(normalized_root)) < 2
            or len(TOKEN_PATTERN.findall(normalized_root.casefold()))
            > MODEL_DIRECT_QUERY_MAX_WORDS
        ):
            continue
        root_check = next(
            (
                check
                for check in checks
                if " ".join(str(check.get("root") or "").split()).casefold()
                == normalized_root.casefold()
            ),
            None,
        )
        if (
            root_check is None
            or root_check.get("status") != "observed"
            or int(root_check.get("eligible_expansion_count") or 0) > 0
        ):
            continue
        suggestions = root_check.get("suggestions")
        suggestion_count = len(suggestions) if isinstance(suggestions, list) else 0
        fallback_reason = (
            "platform_returned_no_suggestions"
            if suggestion_count == 0
            else (
                "platform_returned_no_concise_relevant_suggestions"
                if int(root_check.get("related_but_too_long_count") or 0) > 0
                else "platform_returned_no_relevant_suggestions"
            )
        )
        root_check["direct_query_fallback_selected"] = True
        root_check["direct_query_fallback_reason"] = fallback_reason
        seller_title_targets.append(
            SearchKeywordCandidate(
                phrase=normalized_root,
                rationale=(
                    "当前主标题中的完整商品词组；平台未返回可采用补全词，"
                    "因此占用一个既有核心查询位直接验证完整搜索结果页"
                ),
                candidate_source="seller_title_complete_phrase",
                intended_strategy="core",
                seed=normalized_root,
                seed_source="title_word_root",
                journey_type="title_complete_phrase_direct",
                journey_root=normalized_root,
                journey_path=(normalized_root,),
                candidate_provenance=(
                    {
                        "candidate_source": "seller_title_complete_phrase",
                        "intended_strategy": "core",
                        "seed": normalized_root,
                        "root": normalized_root,
                        "seed_source": "title_word_root",
                        "root_source": "title_word_root",
                        "journey_type": "title_complete_phrase_direct",
                        "journey_root": normalized_root,
                        "journey_path": [normalized_root],
                        "journey_depth": 0,
                        "title_phrase_direct_reason": fallback_reason,
                        "platform_suggestion_count": suggestion_count,
                    },
                ),
            )
        )
        if len(seller_title_targets) >= SELLER_TITLE_COMPLETE_PHRASE_QUERY_MAX:
            break

    ranked = [
        item
        for _, _, item in sorted(
            root_expansions,
            key=lambda row: (row[0], -row[1]),
        )
    ]
    core_root_expansions = [
        item
        for item in ranked
        if _provenance_has_strategy(
            _candidate_provenance(item),
            candidate_source="takealot_root_expansion",
            intended_strategy="core",
        )
    ]
    narrow_core_expansions = [
        item for item in core_root_expansions if _candidate_has_primary_shape(item.phrase, profile)
    ]
    broad_core_expansions = [
        item
        for item in core_root_expansions
        if not _candidate_has_primary_shape(item.phrase, profile)
    ]
    opportunity_root_expansions = [
        item
        for item in ranked
        if _provenance_has_strategy(
            _candidate_provenance(item),
            candidate_source="takealot_root_expansion",
            intended_strategy="opportunity",
        )
    ]
    selected: list[SearchKeywordCandidate] = []
    pool_limit = min(
        SHOPPER_JOURNEY_CANDIDATE_POOL_LIMIT,
        max(max_keywords + 4, max_keywords),
    )
    ranked_core_pool = [*narrow_core_expansions, *broad_core_expansions]
    diverse_core_pool: list[SearchKeywordCandidate] = []
    deferred_core_pool: list[SearchKeywordCandidate] = []
    used_roots: set[str] = set()
    for candidate in ranked_core_pool:
        root = " ".join(str(candidate.journey_root or candidate.seed or "").split()).casefold()
        if root and root not in used_roots:
            diverse_core_pool.append(candidate)
            used_roots.add(root)
        else:
            deferred_core_pool.append(candidate)
    diverse_core_pool.extend(deferred_core_pool)
    platform_query_target = max(
        0,
        ROOT_EXPANSION_CORE_QUERY_TARGET
        - len(decision_parameter_candidates)
        - len(seller_title_targets),
    )
    platform_targets = diverse_core_pool[:platform_query_target]
    direct_targets = precise[:MODEL_DIRECT_QUERY_TARGET]
    opportunity_targets = opportunity_root_expansions[
        :ROOT_EXPANSION_OPPORTUNITY_QUERY_TARGET
    ]
    # The normal phase is at most thirteen queries: six core slots shared by
    # platform-root-expansion winners, at most one seller-title complete phrase,
    # and operator-confirmed parameter probes; plus six concise direct queries that
    # take same-product lexicon entries first and use fusion keywords only as filler,
    # plus one adjacent-demand probe. The fourteenth search-query slot stays
    # outside this list for adaptive recovery.
    priority_candidates: list[SearchKeywordCandidate] = []
    channel_span = max(
        len(platform_targets),
        len(direct_targets),
        len(decision_parameter_candidates),
        len(seller_title_targets),
    )
    for channel_index in range(channel_span):
        if channel_index < len(seller_title_targets):
            priority_candidates.append(seller_title_targets[channel_index])
        if channel_index < len(platform_targets):
            priority_candidates.append(platform_targets[channel_index])
        if channel_index < len(direct_targets):
            priority_candidates.append(direct_targets[channel_index])
        if channel_index < len(decision_parameter_candidates):
            priority_candidates.append(decision_parameter_candidates[channel_index])
        if channel_index == 0:
            priority_candidates.extend(opportunity_targets[:1])
    base_limit = min(ADAPTIVE_BASE_QUERY_TARGET, max_keywords)
    for candidate in priority_candidates:
        _append_unique_candidate(selected, candidate, base_limit)
    for candidate in (
        *seller_title_targets,
        *diverse_core_pool,
        *precise,
        *decision_parameter_candidates,
        *opportunity_root_expansions,
    ):
        _append_unique_candidate(selected, candidate, base_limit)
    for candidate in diverse_core_pool:
        _append_unique_candidate(
            selected,
            replace(
                candidate,
                adaptive_recovery_source="second_best_root_expansion",
            ),
            pool_limit,
        )
    return selected, checks


def _normalized_autocomplete_suggestions(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for value in raw:
        normalized = " ".join(str(value).split())[:200]
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        output.append(normalized)
        if len(output) >= AUTOCOMPLETE_RESULT_LIMIT:
            break
    return output


def _autocomplete_cache_evidence(
    client: SearchPublicClient,
    input_state: str,
) -> dict[str, Any]:
    resolver = getattr(client, "autocomplete_evidence", None)
    if not callable(resolver):
        return {
            "cache_status": "not_recorded",
            "shared_across_stores": False,
        }
    raw = resolver(input_state)
    return dict(raw) if isinstance(raw, Mapping) else {}


def _complete_root_expansion_input(phrase: str) -> str:
    """Normalize one complete root word or phrase, never a typed prefix.

    A shopper root is a semantic entry point, not the first token of a phrase.
    Keeping ``lazy sofa`` or ``floor chair`` intact prevents a useful product
    phrase from collapsing into ambiguous inputs such as ``lazy`` or ``floor``.
    """

    raw_tokens = [str(token) for token in TOKEN_PATTERN.findall(phrase.casefold())]
    shaped_initial = bool(
        len(raw_tokens) >= 2
        and raw_tokens[0] in {"l", "u"}
        and raw_tokens[1] == "shaped"
    )
    tokens = [
        token
        for index, token in enumerate(raw_tokens)
        if token not in _VARIANT_COLOUR_TOKENS
        and (
            token not in TITLE_PARAMETER_UNIT_TOKENS
            or (shaped_initial and index == 0)
        )
        and not token.isdigit()
        and not COMBINED_MEASUREMENT_PATTERN.fullmatch(token)
        and any(character.isalpha() for character in token)
    ]
    while tokens and tokens[0] in TITLE_CONNECTOR_TOKENS:
        tokens.pop(0)
    while tokens and tokens[-1] in TITLE_CONNECTOR_TOKENS:
        tokens.pop()
    if not tokens:
        return ""
    if len(tokens[0]) == 1 and not (
        len(tokens) >= 2 and tokens[0] in {"l", "u"} and tokens[1] == "shaped"
    ):
        return ""
    if any(len(token) == 1 for token in tokens[1:]):
        return ""
    return " ".join(tokens[:ROOT_EXPANSION_PHRASE_MAX_WORDS])


def _complete_root_seed_specs(
    candidates: Sequence[KeywordCandidate],
    *,
    seed_source: str,
    intended_strategy: str,
) -> list[tuple[KeywordCandidate, str, str]]:
    output: list[tuple[KeywordCandidate, str, str]] = []
    for candidate in candidates:
        root = _complete_root_expansion_input(candidate.phrase)
        if not root:
            continue
        output.append(
            (
                KeywordCandidate(phrase=root, rationale=candidate.rationale),
                seed_source,
                intended_strategy,
            )
        )
    return output


def _same_product_lexicon_root_seed_specs(
    lexicon: Mapping[str, Any],
) -> list[tuple[KeywordCandidate, str, str]]:
    output: list[tuple[KeywordCandidate, str, str]] = []
    raw_entries = lexicon.get("entries")
    if not isinstance(raw_entries, list):
        return output
    entry_count = 0
    for entry in raw_entries:
        if entry_count >= SAME_PRODUCT_LEXICON_ROOT_LIMIT:
            break
        if not isinstance(entry, Mapping):
            continue
        phrase = " ".join(str(entry.get("term") or "").split())
        root = _complete_root_expansion_input(phrase)
        if not root:
            continue
        entry_count += 1
        raw_sources = entry.get("sources")
        sources = raw_sources if isinstance(raw_sources, list) else []
        root_sources: list[str] = []
        if "human_confirmed_product_fact" in sources:
            root_sources.append("human_confirmed_product_fact")
        if any(str(source).startswith("fusion_") for source in sources):
            root_sources.append("image_title_same_product_lexicon")
        if not root_sources:
            root_sources.append(_same_product_lexicon_root_source(entry))
        for root_source in root_sources:
            output.append(
                (
                    KeywordCandidate(
                        phrase=root,
                        rationale=(
                            "同品词库中的精准商品名称，作为完整词组读取Takealot平台扩展"
                        ),
                    ),
                    root_source,
                    "core",
                )
            )
    return output


def _confirmed_fact_root_seed_specs(
    confirmed_fact_records: Sequence[Mapping[str, Any]],
) -> list[tuple[KeywordCandidate, str, str]]:
    """Keep operator-confirmed facts separate from model-produced root seeds."""

    output: list[tuple[KeywordCandidate, str, str]] = []
    for record in confirmed_fact_records:
        phrase = " ".join(str(record.get("fact_term") or "").split())
        root = _complete_root_expansion_input(phrase)
        if not root:
            continue
        fact_type = str(record.get("fact_type") or "product_type")
        intended_strategy = "opportunity" if fact_type == "usage" else "core"
        output.append(
            (
                KeywordCandidate(
                    phrase=root,
                    rationale="运营人工确认的商品事实词根，优先用于平台扩展验证",
                ),
                "human_confirmed_product_fact",
                intended_strategy,
            )
        )
    return output


def _bounded_core_seed_specs_with_title_coverage(
    prioritized_seeds: list[tuple[KeywordCandidate, str, str]],
    required_title_seeds: list[tuple[KeywordCandidate, str, str]],
    *,
    limit: int,
) -> list[tuple[KeywordCandidate, str, str]]:
    """Apply source priority while retaining every bounded official-title root."""

    title_root_keys = [
        " ".join(candidate.phrase.split()).casefold()
        for candidate, _ in _group_seed_specs(required_title_seeds)
    ][:limit]
    selected_roots = set(title_root_keys)
    for candidate, _ in _group_seed_specs(prioritized_seeds):
        if len(selected_roots) >= limit:
            break
        selected_roots.add(" ".join(candidate.phrase.split()).casefold())
    return [
        item
        for item in prioritized_seeds
        if " ".join(item[0].phrase.split()).casefold() in selected_roots
    ]


def _root_seed_origin_phrases(
    *,
    confirmed_fact_records: Sequence[Mapping[str, Any]],
    same_product_lexicon: Mapping[str, Any],
    title_reference_terms: Sequence[str],
    model_autocomplete_seeds: Sequence[KeywordCandidate],
    title_roots: Sequence[str],
    model_opportunity_seeds: Sequence[KeywordCandidate],
) -> dict[tuple[str, str, str], str]:
    origins: dict[tuple[str, str, str], str] = {}

    def remember(phrase: str, source: str, strategy: str) -> None:
        normalized_phrase = " ".join(phrase.split())
        root = _complete_root_expansion_input(normalized_phrase)
        if not root:
            return
        origins.setdefault(
            (root.casefold(), source, strategy),
            normalized_phrase,
        )

    for record in confirmed_fact_records:
        fact_type = str(record.get("fact_type") or "product_type")
        remember(
            str(record.get("fact_term") or ""),
            "human_confirmed_product_fact",
            "opportunity" if fact_type == "usage" else "core",
        )
    raw_lexicon_entries = same_product_lexicon.get("entries")
    if isinstance(raw_lexicon_entries, list):
        for entry in raw_lexicon_entries:
            if not isinstance(entry, Mapping):
                continue
            phrase = str(entry.get("term") or "")
            raw_sources = entry.get("sources")
            sources = raw_sources if isinstance(raw_sources, list) else []
            if "human_confirmed_product_fact" in sources:
                remember(phrase, "human_confirmed_product_fact", "core")
            if any(str(source).startswith("fusion_") for source in sources):
                remember(phrase, "image_title_same_product_lexicon", "core")
    for term in title_reference_terms:
        remember(term, "title_cross_check", "core")
    for candidate in model_autocomplete_seeds:
        remember(candidate.phrase, "image_title_first_instinct", "core")
    for root in title_roots:
        remember(root, "title_word_root", "core")
    for candidate in model_opportunity_seeds:
        remember(candidate.phrase, "image_title_need_state", "opportunity")
    return origins


def _is_complete_root_expansion_input(value: str) -> bool:
    """Hide historical character-by-character states such as ``rgb l``."""

    tokens = TOKEN_PATTERN.findall(value.casefold())
    return bool(tokens and len(tokens[-1]) >= 2)


def _root_expansion_journey_type(
    *,
    seed_source: str,
    intended_strategy: str,
) -> str:
    if intended_strategy == "opportunity":
        return "adjacent_opportunity"
    if seed_source == "human_confirmed_product_fact":
        return "human_confirmed_fact_root_expansion"
    if seed_source == "image_title_same_product_lexicon":
        return "same_product_lexicon_root_expansion"
    if seed_source == "title_cross_check":
        return "title_cross_check_root_expansion"
    if seed_source == "image_title_first_instinct":
        return "model_fusion_root_expansion"
    if seed_source == "title_decision_parameter":
        return "title_decision_parameter"
    if seed_source == "title_word_root":
        return "title_root_expansion"
    if seed_source == "result_page_learning":
        return "result_page_root_expansion"
    return "platform_root_expansion"


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


def _root_expansion_relevance_decision(
    phrase: str,
    profile: VisionProfile,
    *,
    source_title: str,
) -> dict[str, Any]:
    """Select a platform expansion only when its product relation is explainable.

    Platform order is raw evidence, not permission to search every returned
    phrase. Core candidates must retain the target product identity. A different
    product family is eligible only when a structured adjacent-demand hypothesis
    explicitly names it as an alternative for the same buyer job.
    """

    normalized = " ".join(phrase.split())
    phrase_tokens = set(_semantic_text_tokens(normalized))
    if not normalized or not phrase_tokens:
        return {
            "accepted": False,
            "relation": "irrelevant",
            "reason": "empty_or_unusable_phrase",
            "matched_terms": [],
        }
    ordered_phrase_tokens = _identity_term_tokens(normalized)
    if (
        len(ordered_phrase_tokens) == 1
        and ordered_phrase_tokens[0] in GENERIC_IDENTITY_HEAD_TOKENS
    ):
        return {
            "accepted": False,
            "relation": "irrelevant",
            "reason": "generic_single_word_without_product_context",
            "matched_terms": [],
        }

    structured_exclusions = list(
        dict.fromkeys(
            value
            for value in (
                *profile.exclusions,
                *(
                    term
                    for intent in profile.opportunity_seeds
                    for term in intent.excluded_product_terms
                ),
            )
            if " ".join(str(value or "").split())
        )
    )
    matched_exclusions = _semantic_matching_product_terms(
        normalized,
        structured_exclusions,
    )
    if matched_exclusions:
        return {
            "accepted": False,
            "relation": "irrelevant",
            "reason": "matched_excluded_product_term",
            "matched_terms": matched_exclusions,
        }

    same_product_terms = _same_product_relation_terms(profile)
    same_matches = _semantic_matching_product_terms(normalized, same_product_terms)
    deterministic_title_alias = _current_title_direct_product_alias(
        keyword=normalized,
        source_title=source_title,
        same_product_terms=same_product_terms,
    )
    if same_matches or deterministic_title_alias:
        return {
            "accepted": True,
            "relation": "same_product",
            "reason": (
                "same_product_term_match"
                if same_matches
                else "current_title_direct_product_phrase"
            ),
            "matched_terms": same_matches or [str(deterministic_title_alias)],
        }

    identity_rows = [
        row
        for value in same_product_terms
        if (row := _identity_term_tokens(value))
    ]
    identity_heads = {
        row[-1]
        for row in identity_rows
        if row[-1] not in GENERIC_IDENTITY_HEAD_TOKENS
    }
    matched_heads = phrase_tokens & identity_heads
    if matched_heads and not _semantic_retargets_product(normalized, matched_heads):
        supported_context = (
            _canonical_tokens(source_title)
            | _canonical_tokens(" ".join(profile.distinctive_terms))
            | set().union(*(set(row) for row in identity_rows))
        )
        supporting_tokens = (phrase_tokens & supported_context) - matched_heads
        if len(phrase_tokens) == 1 or supporting_tokens:
            return {
                "accepted": True,
                "relation": "same_product",
                "reason": "product_identity_anchor_with_supported_context",
                "matched_terms": sorted(matched_heads | supporting_tokens),
            }

    adjacent_matches: list[str] = []
    for intent in profile.opportunity_seeds:
        if not intent.buyer_job.strip() or not intent.alternative_product_terms:
            continue
        matched_alternatives = _semantic_matching_product_terms(
            normalized,
            intent.alternative_product_terms,
        )
        adjacent_matches.extend(matched_alternatives)
    if adjacent_matches:
        return {
            "accepted": True,
            "relation": "adjacent_demand",
            "reason": "structured_adjacent_product_family_match",
            "matched_terms": list(dict.fromkeys(adjacent_matches)),
        }

    return {
        "accepted": False,
        "relation": "irrelevant",
        "reason": "no_product_identity_or_structured_adjacent_match",
        "matched_terms": [],
    }


def _autocomplete_fit_score(
    phrase: str,
    profile: VisionProfile,
    *,
    source_title: str,
) -> float:
    decision = _root_expansion_relevance_decision(
        phrase,
        profile,
        source_title=source_title,
    )
    if not decision["accepted"]:
        return 0.0
    phrase_tokens = _canonical_tokens(phrase)
    if not phrase_tokens:
        return 0.0
    supported_identity_tokens = _canonical_tokens(
        " ".join(_same_product_relation_terms(profile))
    )
    for exclusion in profile.exclusions:
        exclusion_tokens = _canonical_tokens(exclusion)
        if exclusion_tokens and exclusion_tokens.issubset(phrase_tokens):
            return 0.0
        exclusion_words = TOKEN_PATTERN.findall(exclusion.casefold())
        exclusion_head = _canonical_token(exclusion_words[-1]) if exclusion_words else ""
        if (
            exclusion_head
            # An exclusion such as ``rigid frame sofa`` must reject that full
            # product shape, not every ``sofa`` expansion when another model-
            # supported product type or direct alias explicitly uses sofa.
            and exclusion_head not in supported_identity_tokens
            and exclusion_head in phrase_tokens
        ):
            return 0.0
    type_tokens = _canonical_tokens(" ".join(profile.product_type_terms))
    distinctive_tokens = _canonical_tokens(" ".join(profile.distinctive_terms))
    source_tokens = _canonical_tokens(source_title)
    type_overlap = phrase_tokens & type_tokens
    distinctive_overlap = phrase_tokens & distinctive_tokens
    source_overlap = phrase_tokens & source_tokens
    score = ((3 * len(type_overlap)) + len(distinctive_overlap) + len(source_overlap)) / max(
        2, 2 * len(phrase_tokens)
    )
    score += 8.0 if decision["relation"] == "same_product" else 4.0
    if any(
        tokens.issubset(phrase_tokens)
        for tokens in _validation_token_sets(profile.product_type_terms[0])
    ):
        score += 5.0
    if len(phrase_tokens) == 1:
        score *= 0.25
    return score


def _candidate_has_primary_shape(phrase: str, profile: VisionProfile) -> bool:
    phrase_tokens = _canonical_tokens(phrase)
    return any(
        tokens.issubset(phrase_tokens)
        for tokens in _validation_token_sets(profile.product_type_terms[0])
    )


def _is_platform_root_expansion_source(value: Any) -> bool:
    return str(value or "") in PLATFORM_ROOT_EXPANSION_SOURCES


def _query_source_channels(candidate: SearchKeywordCandidate) -> list[str]:
    channels: list[str] = []
    for source in _candidate_provenance(candidate):
        candidate_source = str(source.get("candidate_source") or "")
        if _is_platform_root_expansion_source(candidate_source):
            channel = "takealot_root_expansion"
        elif candidate_source == "same_product_lexicon":
            channel = "same_product_lexicon_direct"
        elif candidate_source in {"image_precise", "image_title_fused_precise"}:
            channel = "model_south_african_direct"
        elif candidate_source == "seller_title_complete_phrase":
            channel = "seller_title_complete_phrase"
        elif candidate_source == "comparison_resample":
            channel = "comparison_resample"
        elif candidate_source == "title_verified_parameter":
            channel = "title_verified_parameter"
        elif candidate_source == "human_confirmed_decision_parameter":
            channel = "human_confirmed_decision_parameter"
        else:
            continue
        if channel not in channels:
            channels.append(channel)
    return channels


def _query_source_channel(candidate: SearchKeywordCandidate) -> str:
    channels = _query_source_channels(candidate)
    if candidate.candidate_source == "comparison_resample":
        return "comparison_resample"
    if "takealot_root_expansion" in channels:
        return "takealot_root_expansion"
    if "seller_title_complete_phrase" in channels:
        return "seller_title_complete_phrase"
    if "same_product_lexicon_direct" in channels:
        return "same_product_lexicon_direct"
    if "model_south_african_direct" in channels:
        return "model_south_african_direct"
    if "title_verified_parameter" in channels:
        return "title_verified_parameter"
    if "human_confirmed_decision_parameter" in channels:
        return "human_confirmed_decision_parameter"
    return "unknown"


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
        base = _preferred_duplicate_candidate(existing, candidate)
        secondary = candidate if base is existing else existing
        comparison_role = (
            "primary"
            if "primary" in {existing.comparison_role, candidate.comparison_role}
            else existing.comparison_role or candidate.comparison_role
        )
        output[duplicate_index] = replace(
            base,
            candidate_provenance=_merged_candidate_provenance(base, secondary),
            comparison_baseline_rank=(
                existing.comparison_baseline_rank
                if existing.comparison_baseline_rank is not None
                else candidate.comparison_baseline_rank
            ),
            comparison_role=comparison_role,
            comparison_strategy=(existing.comparison_strategy or candidate.comparison_strategy),
        )
        return
    if len(output) >= limit:
        return
    output.append(candidate)


def _preferred_duplicate_candidate(
    existing: SearchKeywordCandidate,
    candidate: SearchKeywordCandidate,
) -> SearchKeywordCandidate:
    if existing.candidate_source == "comparison_resample":
        return candidate if candidate.candidate_source != "comparison_resample" else existing
    if candidate.candidate_source == "comparison_resample":
        return existing
    existing_is_platform = _is_platform_root_expansion_source(existing.candidate_source)
    candidate_is_platform = _is_platform_root_expansion_source(candidate.candidate_source)
    if existing_is_platform != candidate_is_platform:
        return existing if existing_is_platform else candidate
    if existing_is_platform and candidate_is_platform:
        existing_priority = _root_source_priority_index(existing.seed_source)
        candidate_priority = _root_source_priority_index(candidate.seed_source)
        return candidate if candidate_priority < existing_priority else existing
    return existing


def _root_source_priority_index(source: str | None) -> int:
    try:
        return ROOT_SOURCE_PRIORITY.index(str(source or ""))
    except ValueError:
        return len(ROOT_SOURCE_PRIORITY)


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
            "journey_type": candidate.journey_type,
            "journey_root": candidate.journey_root,
            "journey_path": list(candidate.journey_path),
            "journey_depth": candidate.journey_depth,
            "journey_parent_query": candidate.journey_parent_query,
        },
    )


def _merged_candidate_provenance(
    first: SearchKeywordCandidate,
    second: SearchKeywordCandidate,
) -> tuple[dict[str, Any], ...]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, int | None, str, str]] = set()
    for raw in (*_candidate_provenance(first), *_candidate_provenance(second)):
        item = dict(raw)
        key = (
            str(item.get("candidate_source") or ""),
            str(item.get("intended_strategy") or ""),
            str(item.get("seed") or ""),
            str(item.get("seed_source") or ""),
            _optional_int(item.get("autocomplete_rank")),
            str(item.get("journey_type") or ""),
            str(item.get("journey_root") or ""),
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
        (
            _is_platform_root_expansion_source(item.get("candidate_source"))
            if candidate_source == "takealot_root_expansion"
            else str(item.get("candidate_source") or "") == candidate_source
        )
        and str(item.get("intended_strategy") or "") == intended_strategy
        and (
            not require_autocomplete_rank
            or _optional_int(item.get("autocomplete_rank")) is not None
        )
        for item in provenance
        if isinstance(item, Mapping)
    )


def _low_confidence_observation(
    candidate: SearchKeywordCandidate,
    candidate_order: int,
    confidence: float,
    threshold: float,
) -> KeywordObservation:
    return KeywordObservation(
        keyword=candidate.phrase,
        candidate_order=candidate_order,
        relevance_status="model_low_confidence",
        relevance_score=0,
        validation_evidence={
            "candidate_rationale": candidate.rationale,
            "candidate_source": candidate.candidate_source,
            "query_source_channel": _query_source_channel(candidate),
            "query_source_channels": _query_source_channels(candidate),
            "model_confidence": confidence,
            "confidence_threshold": threshold,
            "page_validation_status": "not_run",
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
    source_title: str = "",
) -> str:
    # Fusion output is title-dependent. A title edit must never reuse phrases generated
    # for an older title even when the product image is unchanged.
    normalized_title = " ".join(source_title.split()).casefold()
    raw = "\n".join((PROMPT_VERSION, provider_signature, image_url, normalized_title))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_VARIANT_COLOUR_TOKENS = {
    "beige",
    "black",
    "blue",
    "bronze",
    "brown",
    "charcoal",
    "clear",
    "copper",
    "cream",
    "gold",
    "gray",
    "green",
    "grey",
    "ivory",
    "navy",
    "orange",
    "pink",
    "purple",
    "red",
    "silver",
    "tan",
    "transparent",
    "white",
    "yellow",
}
_VARIANT_SIZE_TOKENS = {
    "single",
    "double",
    "queen",
    "king",
    "super",
    "xl",
    "xs",
    "small",
    "medium",
    "large",
    "extra",
    "quarter",
}


def _longest_common_title_subsequence(
    left: Sequence[str],
    right: Sequence[str],
) -> list[str]:
    """Return a case-insensitive LCS while preserving the left title's spelling."""

    left_values = list(left)
    right_values = list(right)
    left_keys = [value.casefold() for value in left_values]
    right_keys = [value.casefold() for value in right_values]
    rows = len(left_values) + 1
    columns = len(right_values) + 1
    lengths = [[0] * columns for _ in range(rows)]
    for left_index in range(len(left_values) - 1, -1, -1):
        for right_index in range(len(right_values) - 1, -1, -1):
            if left_keys[left_index] == right_keys[right_index]:
                lengths[left_index][right_index] = (
                    lengths[left_index + 1][right_index + 1] + 1
                )
            else:
                lengths[left_index][right_index] = max(
                    lengths[left_index + 1][right_index],
                    lengths[left_index][right_index + 1],
                )
    output: list[str] = []
    left_index = 0
    right_index = 0
    while left_index < len(left_values) and right_index < len(right_values):
        if left_keys[left_index] == right_keys[right_index]:
            output.append(left_values[left_index])
            left_index += 1
            right_index += 1
        elif lengths[left_index + 1][right_index] >= lengths[left_index][right_index + 1]:
            left_index += 1
        else:
            right_index += 1
    return output


def _unmatched_title_phrases(
    title_tokens: Sequence[str],
    shared_tokens: Sequence[str],
) -> list[str]:
    """Keep contiguous Seller-title text that is not part of the shared family subject."""

    tokens = list(title_tokens)
    shared_keys = [token.casefold() for token in shared_tokens]
    shared_index = 0
    unmatched: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if shared_index < len(shared_keys) and token.casefold() == shared_keys[shared_index]:
            if current:
                unmatched.append(current)
                current = []
            shared_index += 1
        else:
            current.append(token)
    if current:
        unmatched.append(current)
    return [" ".join(group) for group in unmatched if group]


def _variant_parameter_type(value: str) -> str:
    tokens = _title_tokens(value)
    normalized = {token.casefold() for token in tokens}
    structured = _title_parameter_candidates(value)
    if structured:
        candidate_types = {
            str(item.get("parameter_type") or "specification") for item in structured
        }
        if len(candidate_types) == 1:
            return candidate_types.pop()
        return "specification"
    if normalized and normalized.issubset(_VARIANT_COLOUR_TOKENS):
        return "colour"
    if normalized and normalized.issubset(_VARIANT_SIZE_TOKENS | {"of", "three"}):
        return "size"
    return "variant_value"


def _variant_family_profile(
    items: Sequence[Mapping[str, Any]],
    *,
    representative_offer_id: str | None = None,
) -> dict[str, Any]:
    """Separate a PLID's shared subject from each Offer's title-backed parameters."""

    normalized_items = [
        {
            "offer_id": str(item.get("offer_id") or "").strip(),
            "productline_id": str(item.get("productline_id") or "").strip() or None,
            "sku": str(item.get("sku") or "").strip() or None,
            "title": " ".join(str(item.get("title") or "").split()),
            "image_url": str(item.get("image_url") or "").strip() or None,
            "available_stock": max(0, _optional_int(item.get("available_stock")) or 0),
        }
        for item in items
        if str(item.get("offer_id") or "").strip()
    ]
    if not normalized_items:
        raise ValueError("variant family requires at least one Offer")
    normalized_items.sort(key=lambda item: str(item["offer_id"]))
    representative = next(
        (
            item
            for item in normalized_items
            if item["offer_id"] == str(representative_offer_id or "").strip()
        ),
        normalized_items[0],
    )
    ordered_for_lcs = [
        representative,
        *(item for item in normalized_items if item is not representative),
    ]
    shared_tokens = _title_tokens(str(representative["title"]))
    for item in ordered_for_lcs[1:]:
        shared_tokens = _longest_common_title_subsequence(
            shared_tokens,
            _title_tokens(str(item["title"])),
        )
        if not shared_tokens:
            break
    shared_title_source = "all_variant_titles"
    if not shared_tokens:
        shared_tokens = _title_tokens(str(representative["title"]))
        shared_title_source = "representative_fallback_no_common_sequence"
    shared_title = " ".join(shared_tokens)

    variants: list[dict[str, Any]] = []
    for item in normalized_items:
        phrases = (
            _unmatched_title_phrases(_title_tokens(str(item["title"])), shared_tokens)
            if len(normalized_items) > 1
            else []
        )
        parameters = [
            {
                "value": phrase,
                "parameter_type": _variant_parameter_type(phrase),
                "source": "seller_offer_title_difference",
                "visually_verified": False,
            }
            for phrase in phrases
            if phrase
        ]
        variants.append({**item, "parameters": parameters})

    image_urls = {
        str(item["image_url"])
        for item in normalized_items
        if str(item.get("image_url") or "").strip()
    }
    return {
        "productline_id": representative["productline_id"],
        "representative_offer_id": representative["offer_id"],
        "representative_title": representative["title"],
        "shared_title": shared_title,
        "shared_title_source": shared_title_source,
        "variant_count": len(variants),
        "distinct_image_count": len(image_urls),
        "image_evidence_scope": "representative_offer_only",
        "variant_parameter_source": "current_seller_offer_titles",
        "variant_parameters_visually_verified": False,
        "variants": variants,
    }


def _variant_family_cache_material(profile: Mapping[str, Any]) -> str:
    """Build stable cache material so changed membership, titles, images, or values invalidate."""

    variants = profile.get("variants")
    compact_variants = []
    if isinstance(variants, list):
        for item in variants:
            if not isinstance(item, Mapping):
                continue
            compact_variants.append(
                {
                    "offer_id": item.get("offer_id"),
                    "title": item.get("title"),
                    "image_url": item.get("image_url"),
                    "parameters": item.get("parameters", []),
                }
            )
    return json.dumps(
        {
            "productline_id": profile.get("productline_id"),
            "representative_offer_id": profile.get("representative_offer_id"),
            "representative_title": profile.get("representative_title"),
            "shared_title": profile.get("shared_title"),
            "variants": compact_variants,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _product_fact_terms_from_vision(
    vision_payload: Mapping[str, Any] | None,
) -> list[str]:
    if not isinstance(vision_payload, Mapping):
        return []
    profile = vision_payload.get("product_fact_profile")
    raw_facts = profile.get("facts") if isinstance(profile, Mapping) else None
    if not isinstance(raw_facts, list):
        return []
    return list(
        dict.fromkeys(
            normalized
            for item in raw_facts
            if isinstance(item, Mapping)
            and item.get("source_type") == "manual_confirmation"
            and item.get("applied_to_current_image") is True
            if (normalized := " ".join(str(item.get("fact_term") or "").split()))
        )
    )


def _title_evidence_source(
    source_title: str,
    supported_fact_terms: list[str] | tuple[str, ...],
) -> str:
    return " ".join(
        dict.fromkeys(
            value
            for value in (
                " ".join(source_title.split()),
                *(" ".join(term.split()) for term in supported_fact_terms),
            )
            if value
        )
    )


def _sum_usage(*items: Mapping[str, Any] | Any) -> dict[str, int]:
    output = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        for key in output:
            output[key] += _optional_int(item.get(key)) or 0
    return output


def _product_fact_recommendation(
    *,
    source_analysis_id: int,
    profile_confidence: float,
    recognition: Mapping[str, Any],
    observations: list[KeywordObservation] | list[SearchRankingKeywordResult],
) -> dict[str, Any]:
    validated_count = sum(
        item.relevance_status in {"accepted", "opportunity"} for item in observations
    )
    title_reference_terms = recognition.get("title_reference_terms")
    confirmed_title_phrase_count = (
        len(title_reference_terms) if isinstance(title_reference_terms, list) else 0
    )
    codes: list[str] = []
    reason_parts: list[str] = []
    if validated_count == 0:
        codes.append("no_platform_validated_query")
        reason_parts.append("本轮没有搜索词通过核心词或相邻需求蓝海门槛")
    if confirmed_title_phrase_count == 0:
        codes.append("no_title_cross_check_phrase")
        reason_parts.append("图片独立识别与当前标题没有可确认短语")
    if profile_confidence < 0.68:
        codes.append("low_image_identity_confidence")
        reason_parts.append("图片身份置信度不足，平台搜索已被保守限制")
    if recognition.get("manual_fact_required"):
        codes.append("fusion_requires_manual_fact")
        reason_parts.append(
            str(recognition.get("manual_fact_reason") or "图文融合缺少关键商品事实")
        )
    elif recognition.get("identity_large_difference"):
        codes.append("image_title_identity_conflict")
        reason_parts.append("独立图片观察与当前标题差异较大")
    recommended = bool(
        "no_platform_validated_query" in codes
        or "low_image_identity_confidence" in codes
        or "image_title_identity_conflict" in codes
        or "fusion_requires_manual_fact" in codes
    )
    if recommended:
        reason = (
            "；".join(reason_parts)
            + "。如运营能从供应商资料或实物确认图片看不出的商品类型 结构或包装事实，"
            "可人工录入；确认后仍须重新验证平台搜索页。"
        )
    elif "no_title_cross_check_phrase" in codes:
        reason = (
            "模型本轮未主动要求补证，但图片与标题没有形成可确认的完整商品短语。"
            "已有平台验证词不代表商品事实完整；如运营掌握供应商资料或实物依据，"
            "仍可继续人工确认，确认后重新验证平台搜索页。"
        )
    else:
        reason = (
            "模型本轮未主动标记商品事实缺口。已有平台验证词不代表商品事实完整；"
            "如运营掌握供应商资料或实物依据，仍可继续新增或更新人工事实，"
            "确认后重新验证平台搜索页。"
        )
    return {
        "recommended": recommended,
        "reason_code": "+".join(codes) if codes else "evidence_currently_sufficient",
        "reason": reason,
        "source_analysis_id": source_analysis_id,
        "requires_human_confirmation": True,
        "external_lookup_available": False,
        "evidence_use": "operator_confirmed_terms_only",
    }


def _product_fact_recommendation_from_analysis(
    analysis: SearchRankingAnalysis,
    results: list[SearchRankingKeywordResult],
) -> dict[str, Any]:
    vision = analysis.vision_payload or {}
    recognition = vision.get("recognition", {}) if isinstance(vision, Mapping) else {}
    if not isinstance(recognition, Mapping):
        recognition = {}
    return _product_fact_recommendation(
        source_analysis_id=analysis.id,
        profile_confidence=float(analysis.confidence or 0),
        recognition=recognition,
        observations=results,
    )


def _normalized_confirmed_fact_term(raw: Any) -> str:
    normalized = " ".join(str(raw or "").split())
    if (
        len(normalized) < 2
        or len(normalized) > 100
        or normalized.isdigit()
        or re.fullmatch(r"[A-Za-z0-9]+(?: [A-Za-z0-9]+){0,5}", normalized) is None
    ):
        raise SearchRankingInputError(
            "商品事实词必须为2到100个字符的英文或数字短语，最多6个词且不能含标点"
        )
    return normalized


def _validated_product_fact_inputs(
    facts: tuple[ProductFactInput, ...],
) -> list[ProductFactInput]:
    if not 1 <= len(facts) <= 6:
        raise SearchRankingInputError("每次必须确认1到6条商品事实")
    output: list[ProductFactInput] = []
    seen: set[str] = set()
    for fact in facts:
        if fact.fact_type not in PRODUCT_FACT_TYPES:
            raise SearchRankingInputError("商品事实类型不在允许范围内")
        fact_term = _normalized_confirmed_fact_term(fact.fact_term)
        key = fact_term.casefold()
        if key in seen:
            raise SearchRankingInputError("同一次确认中不能重复商品事实词")
        seen.add(key)
        statement = " ".join(str(fact.statement or "").split())
        if len(statement) > 500:
            raise SearchRankingInputError("商品事实说明不能超过500个字符")
        output.append(
            ProductFactInput(
                fact_type=fact.fact_type,
                fact_term=fact_term,
                statement=statement,
            )
        )
    return output


def _product_fact_record_payload(
    fact: SearchRankingProductFact,
    *,
    current_image_url: str,
) -> dict[str, Any]:
    image_matches = fact.source_image_url == current_image_url
    applied = (
        fact.status == "active"
        and image_matches
        and fact.source_type == "manual_confirmation"
    )
    return {
        "id": fact.id,
        "productline_id": fact.productline_id,
        "source_offer_id": fact.source_offer_id,
        "fact_type": fact.fact_type,
        "fact_term": fact.fact_term,
        "statement": fact.statement,
        "status": fact.status,
        "source_type": fact.source_type,
        "source_analysis_id": fact.source_analysis_id,
        "source_title": fact.source_title,
        "source_image_url": fact.source_image_url,
        "current_image_matches": image_matches,
        "applied_to_current_image": applied,
        "needs_image_reconfirmation": fact.status == "active" and not image_matches,
        "evidence": dict(fact.evidence or {}),
        "confirmed_by_username": fact.confirmed_by_username,
        "confirmed_by_display_name": fact.confirmed_by_display_name,
        "confirmed_at": _naive_utc(fact.confirmed_at).isoformat(),
        "revoked_by_username": fact.revoked_by_username,
        "revoked_by_display_name": fact.revoked_by_display_name,
        "revoked_at": (_naive_utc(fact.revoked_at).isoformat() if fact.revoked_at else None),
        "revoke_reason": fact.revoke_reason,
    }


def _product_fact_profile_payload(
    facts: list[SearchRankingProductFact],
    *,
    current_image_url: str,
) -> dict[str, Any]:
    records = [
        _product_fact_record_payload(item, current_image_url=current_image_url) for item in facts
    ]
    return {
        "applied_terms": list(
            dict.fromkeys(
                str(item["fact_term"]) for item in records if item["applied_to_current_image"]
            )
        ),
        "active_count": sum(item["status"] == "active" for item in records),
        "applied_count": sum(item["applied_to_current_image"] for item in records),
        "needs_image_reconfirmation_count": sum(
            item["needs_image_reconfirmation"] for item in records
        ),
        "archive_count": len(records),
        "requires_current_image_match": True,
        "source_policy": "manual_confirmation_only",
        "facts": records,
    }


def _decision_parameter_confirmation_payload(
    confirmation: SearchRankingDecisionParameterConfirmation,
    *,
    current_title: str,
) -> dict[str, Any]:
    normalized_current_title = " ".join(current_title.split()).casefold()
    normalized_source_title = " ".join(confirmation.source_title.split()).casefold()
    decisions = [
        {
            "parameter_key": " ".join(str(item.get("parameter_key") or "").split()).casefold(),
            "parameter_value": " ".join(str(item.get("parameter_value") or "").split()),
            "parameter_type": str(item.get("parameter_type") or "specification"),
            "title_order": _optional_int(item.get("title_order")) or 0,
            "system_recommendation": str(
                item.get("system_recommendation") or "ordinary_specification"
            ),
            "system_reason": str(item.get("system_reason") or ""),
            "is_decision_parameter": bool(item.get("is_decision_parameter")),
        }
        for item in confirmation.decisions
        if isinstance(item, Mapping) and str(item.get("parameter_key") or "").strip()
    ]
    return {
        "id": confirmation.id,
        "productline_id": confirmation.productline_id,
        "source_offer_id": confirmation.source_offer_id,
        "source_analysis_id": confirmation.source_analysis_id,
        "source_title": confirmation.source_title,
        "current_title_matches": normalized_source_title == normalized_current_title,
        "decisions": decisions,
        "policy_version": confirmation.policy_version,
        "confirmed_by_username": confirmation.confirmed_by_username,
        "confirmed_by_display_name": confirmation.confirmed_by_display_name,
        "confirmed_at": _naive_utc(confirmation.confirmed_at).isoformat(),
    }


def _decision_parameter_profile_payload(
    confirmations: list[SearchRankingDecisionParameterConfirmation],
    *,
    current_title: str,
) -> dict[str, Any]:
    candidates = _title_parameter_candidates(current_title)
    archive = [
        _decision_parameter_confirmation_payload(item, current_title=current_title)
        for item in confirmations
    ]
    latest = archive[0] if archive else None
    latest_decisions = {
        str(item["parameter_key"]): bool(item["is_decision_parameter"])
        for item in (latest.get("decisions", []) if isinstance(latest, Mapping) else [])
        if isinstance(item, Mapping)
    }
    candidate_keys = {str(item["parameter_key"]) for item in candidates}
    current_title_confirmed = bool(
        latest
        and latest.get("current_title_matches") is True
        and latest.get("policy_version") == DECISION_PARAMETER_POLICY_VERSION
        and set(latest_decisions) == candidate_keys
    )
    current_candidates = [
        {
            **item,
            "manual_decision": (
                latest_decisions.get(str(item["parameter_key"]))
                if current_title_confirmed
                else None
            ),
        }
        for item in candidates
    ]
    applied = [
        item for item in current_candidates if item.get("manual_decision") is True
    ]
    return {
        "policy_version": DECISION_PARAMETER_POLICY_VERSION,
        "source_policy": "current_seller_title_human_confirmation",
        "fronting_requires_search_validation": True,
        "max_positive_decisions": DECISION_PARAMETER_MAX_POSITIVE,
        "current_title": current_title,
        "current_title_confirmed": current_title_confirmed,
        "requires_confirmation": not current_title_confirmed,
        "candidate_count": len(current_candidates),
        "decision_parameter_count": len(applied),
        "ordinary_parameter_count": sum(
            item.get("manual_decision") is False for item in current_candidates
        ),
        "unconfirmed_count": sum(
            item.get("manual_decision") is None for item in current_candidates
        ),
        "candidates": current_candidates,
        "applied_decision_parameters": applied,
        "applied_decision_values": [str(item["parameter_value"]) for item in applied],
        "latest_confirmation": latest,
        "archive": archive,
    }


def _decision_parameter_values_from_vision(
    vision_payload: Mapping[str, Any] | None,
) -> list[str]:
    if not isinstance(vision_payload, Mapping):
        return []
    profile = vision_payload.get("decision_parameter_profile")
    raw_values = profile.get("applied_decision_values") if isinstance(profile, Mapping) else None
    if not isinstance(raw_values, list):
        return []
    return list(
        dict.fromkeys(
            normalized
            for value in raw_values
            if (normalized := " ".join(str(value or "").split()))
        )
    )


def _enrich_profile_with_confirmed_facts(
    base: VisionProfile,
    facts: list[dict[str, Any]],
) -> VisionProfile:
    normalized_facts = [
        (
            str(item.get("fact_type") or "product_type"),
            " ".join(str(item.get("fact_term") or "").split()),
            str(item.get("source_type") or "manual_confirmation"),
        )
        for item in facts
        if " ".join(str(item.get("fact_term") or "").split())
    ]
    if not normalized_facts:
        return base

    def merged_terms(preferred: list[str], fallback: list[str], limit: int) -> list[str]:
        return list(
            dict.fromkeys(term for term in (*preferred, *fallback) if " ".join(str(term).split()))
        )[:limit]

    def merged_candidates(
        preferred: Sequence[KeywordCandidate],
        fallback: Sequence[KeywordCandidate],
        limit: int,
    ) -> list[KeywordCandidate]:
        output: list[KeywordCandidate] = []
        seen: set[str] = set()
        for candidate in (*preferred, *fallback):
            key = " ".join(candidate.phrase.split()).casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            output.append(candidate)
            if len(output) >= limit:
                break
        return output

    # Only an explicitly confirmed product type may extend the product-family
    # terms, and the image model's first visible physical form remains primary for
    # first-page same-type validation. Construction/packaging facts such as
    # "compressed" or "vacuum packed" remain searchable evidence, but they are
    # hidden attributes rather than something the image visibly proved.
    product_type_terms = [
        term for fact_type, term, _ in normalized_facts if fact_type == "product_type"
    ]
    identity_tokens = {
        token
        for value in (*base.product_type_terms, *base.same_product_aliases)
        for token in _identity_term_tokens(value)
    }
    confirmed_same_product_aliases = [
        term
        for fact_type, term, _ in normalized_facts
        if fact_type in {"product_type", "construction", "function", "packaging"}
        and (
            fact_type == "product_type"
            or bool(set(_identity_term_tokens(term)) & identity_tokens)
        )
    ]
    searchable = [
        KeywordCandidate(
            phrase=term,
            rationale="运营人工确认的商品事实档案",
        )
        for fact_type, term, _ in normalized_facts
        if fact_type in {"product_type", "construction", "function", "packaging"}
    ]
    opportunity = [
        KeywordCandidate(
            phrase=term,
            rationale="运营人工确认的使用场景事实，仅作为相邻需求入口验证",
        )
        for fact_type, term, _ in normalized_facts
        if fact_type == "usage"
    ]
    return base.model_copy(
        update={
            "product_type_terms": merged_terms(
                base.product_type_terms,
                product_type_terms,
                5,
            ),
            "same_product_aliases": merged_terms(
                confirmed_same_product_aliases,
                base.same_product_aliases,
                8,
            ),
            "distinctive_terms": merged_terms(
                [term for _, term, _ in normalized_facts],
                base.distinctive_terms,
                8,
            ),
            "keywords": merged_candidates(searchable, base.keywords, 5),
            "autocomplete_seeds": merged_candidates(
                searchable,
                base.autocomplete_seeds,
                5,
            ),
            "opportunity_seeds": merged_candidates(
                opportunity,
                base.opportunity_seeds,
                3,
            ),
        }
    )


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
        and " ".join(previous.source_title.split()).casefold() != normalized_current_title
    ):
        for candidate_analysis in completed:
            candidate_results = results_for(candidate_analysis)
            candidate_evidence_title = _title_evidence_source(
                candidate_analysis.source_title,
                _product_fact_terms_from_vision(candidate_analysis.vision_payload),
            )
            candidate_accepted, _, _ = _title_strategy_keywords(
                candidate_results,
                candidate_evidence_title,
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
    previous_profile = previous_raw_profile if isinstance(previous_raw_profile, Mapping) else {}
    raw_distinctive_terms = previous_profile.get("distinctive_terms")
    previous_distinctive_terms = (
        [str(term) for term in raw_distinctive_terms if str(term).strip()]
        if isinstance(raw_distinctive_terms, list)
        else []
    )
    previous_evidence_title = _title_evidence_source(
        previous.source_title,
        _product_fact_terms_from_vision(previous_vision),
    )
    (
        accepted_title_keywords,
        hot_term_title_keywords,
        opportunity_title_keywords,
    ) = _title_strategy_keywords(
        results,
        previous_evidence_title,
        profile_distinctive_terms=previous_distinctive_terms,
    )
    title_strategies = _build_title_strategies(
        source_title=previous.source_title,
        evidence_source_title=previous_evidence_title,
        accepted_keywords=accepted_title_keywords,
        hot_term_keywords=hot_term_title_keywords,
        opportunity_keywords=opportunity_title_keywords,
        validated_core_keywords=accepted_title_keywords,
        decision_parameter_values=_decision_parameter_values_from_vision(previous_vision),
    )
    core_suggestion = str(
        title_strategies[0]["title"]
        or _build_title_suggestion(previous.source_title, accepted_title_keywords)
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
                and _optional_int(item.validation_evidence.get("comparison_baseline_rank"))
                is not None
            )
            or item.keyword.casefold() in issued_keyword_keys
            or (
                item.relevance_status == "opportunity"
                and _opportunity_gate_from_result(
                    keyword=item.keyword,
                    source_title=previous_evidence_title,
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
                            item.validation_evidence.get("profile_distinctive_terms")
                            if isinstance(
                                item.validation_evidence,
                                Mapping,
                            )
                            and "profile_distinctive_terms" in item.validation_evidence
                            else previous_distinctive_terms
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
                dict.fromkeys([*duplicate["evidence_keywords"], *normalized_keywords])
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
    return [{"strategy": "historical", "title": fallback}] if fallback else []


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
            if " ".join(str(item.get("title") or "").split()).casefold() == normalized_title
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
        return candidates
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
        " ".join(candidate.phrase.split()).casefold(): candidate for candidate in candidates
    }
    output: list[SearchKeywordCandidate] = []
    used: set[str] = set()
    strategy = str(matched_strategy.get("strategy") or "historical")
    for keyword in ordered_keywords:
        if len(output) >= max_keywords:
            break
        if (
            len(TOKEN_PATTERN.findall(keyword.casefold()))
            > MODEL_DIRECT_QUERY_MAX_WORDS
        ):
            continue
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
        if existing is None or (existing.organic_rank is None and result.organic_rank is not None):
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
    if not required_keywords or missing_baseline_keywords or missing_keywords:
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
    *,
    decision_parameter_values: list[str] | None = None,
) -> str:
    """Place validated search wording first and return punctuation-free title text."""
    priority_tokens = _title_tokens(accepted_keywords[0]) if accepted_keywords else []
    return _build_title_from_priority_tokens(
        raw_suggestion,
        priority_tokens,
        leading_tokens=_source_title_brand_tokens(
            raw_suggestion,
            " ".join(priority_tokens),
        ),
        front_parameter_tokens=_validated_title_decision_parameter_tokens(
            raw_suggestion,
            accepted_keywords,
            decision_parameter_values=decision_parameter_values,
        ),
    )


def _build_hot_term_title_suggestion(
    raw_suggestion: str,
    hot_term_keywords: list[str],
    *,
    decision_parameter_values: list[str] | None = None,
) -> str | None:
    """Naturally merge overlapping autocomplete phrases before the source title."""
    merged, _ = _merge_hot_term_keywords(hot_term_keywords)
    if not merged:
        return None
    return _build_title_from_priority_tokens(
        raw_suggestion,
        merged,
        leading_tokens=_source_title_brand_tokens(
            raw_suggestion,
            " ".join(merged),
        ),
        front_parameter_tokens=_validated_title_decision_parameter_tokens(
            raw_suggestion,
            hot_term_keywords,
            decision_parameter_values=decision_parameter_values,
        ),
    )


def _build_validated_identity_title_suggestion(
    source_title: str,
    validated_identity_keyword: str,
    *,
    decision_parameter_values: list[str] | None = None,
) -> str:
    """Replace a contradicted product noun while preserving title-backed facts."""

    source_tokens = _title_tokens(source_title)
    source_brand_tokens = _source_title_brand_tokens(
        source_title,
        validated_identity_keyword,
    )
    source_parameter_indexes = _title_parameter_indexes(source_tokens)
    suffix_start = next(
        (
            index
            for index, token in enumerate(source_tokens)
            if _canonical_token(token.casefold()) == "with"
        ),
        None,
    )
    retained: list[str] = []
    if suffix_start is not None:
        retained.extend(source_tokens[suffix_start:])
    suffix_indexes = (
        set(range(suffix_start, len(source_tokens)))
        if suffix_start is not None
        else set()
    )
    for index, token in enumerate(source_tokens):
        canonical = _canonical_token(token.casefold())
        if index in suffix_indexes:
            continue
        if (
            index in source_parameter_indexes
            or canonical in HIGH_RISK_CLAIM_TOKENS
            or canonical in FACT_ATTRIBUTE_CLAIM_TOKENS
            or canonical in _VARIANT_COLOUR_TOKENS
        ):
            retained.append(token)
    return _build_title_from_priority_tokens(
        " ".join(retained),
        _title_tokens(validated_identity_keyword),
        leading_tokens=source_brand_tokens,
        front_parameter_tokens=_validated_title_decision_parameter_tokens(
            source_title,
            [validated_identity_keyword],
            decision_parameter_values=decision_parameter_values,
        ),
    )


def _source_title_brand_tokens(
    source_title: str,
    _validated_identity_keyword: str,
) -> list[str]:
    """Return only controlled brand phrases already present in the source title."""

    source_tokens = _title_tokens(source_title)
    if not source_tokens:
        return []
    canonical_tokens = [_canonical_token(token.casefold()) for token in source_tokens]
    brand_tokens: list[str] = []
    for phrase in TITLE_PRESERVED_BRAND_PHRASES:
        phrase_length = len(phrase)
        start = next(
            (
                index
                for index in range(len(canonical_tokens) - phrase_length + 1)
                if tuple(canonical_tokens[index : index + phrase_length]) == phrase
            ),
            None,
        )
        if start is None:
            continue
        brand_tokens.extend(source_tokens[start : start + phrase_length])

    output: list[str] = []
    seen: set[str] = set()
    for token in brand_tokens:
        key = _title_dedup_key(token)
        if key in seen:
            continue
        seen.add(key)
        output.append(token)
    return output


def _title_has_redundant_identity_prefix(source_title: str, keyword: str) -> bool:
    """Detect a duplicated product identity before the validated title phrase."""

    source_tokens = _identity_term_tokens(source_title)
    keyword_tokens = _identity_term_tokens(keyword)
    if len(keyword_tokens) < 2 or len(source_tokens) <= len(keyword_tokens):
        return False
    for start in range(len(source_tokens) - len(keyword_tokens) + 1):
        if source_tokens[start : start + len(keyword_tokens)] != keyword_tokens:
            continue
        return bool(set(source_tokens[:start]) & set(keyword_tokens))
    return False


def _build_title_strategies(
    *,
    source_title: str,
    evidence_source_title: str | None = None,
    accepted_keywords: list[str],
    hot_term_keywords: list[str],
    opportunity_keywords: list[str],
    validated_core_keywords: list[str] | None = None,
    decision_parameter_values: list[str] | None = None,
    keyword_journey_evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return three evidence-bounded title tactics with stable API keys."""
    supported_claim_source = evidence_source_title or source_title
    source_supported_accepted_keywords = _title_supported_keywords(
        accepted_keywords,
        supported_claim_source,
    )
    safe_accepted_keywords = list(source_supported_accepted_keywords)
    validated_core_keys = {
        keyword.casefold() for keyword in (validated_core_keywords or [])
    }
    for keyword in accepted_keywords:
        if (
            keyword.casefold() in validated_core_keys
            and keyword.casefold()
            not in {value.casefold() for value in safe_accepted_keywords}
        ):
            safe_accepted_keywords.append(keyword)
    validated_but_unsupported = bool(accepted_keywords and not safe_accepted_keywords)
    identity_rewrite_keyword = (
        None
        if source_supported_accepted_keywords
        else next(
            (
                keyword
                for keyword in safe_accepted_keywords
                if not _title_supported_keywords([keyword], supported_claim_source)
            ),
            None,
        )
    )
    redundant_supported_identity_keyword = next(
        (
            keyword
            for keyword in source_supported_accepted_keywords
            if _title_has_redundant_identity_prefix(source_title, keyword)
        ),
        None,
    )
    core_identity_keyword = (
        identity_rewrite_keyword or redundant_supported_identity_keyword
    )
    core_title = (
        _build_validated_identity_title_suggestion(
            source_title,
            core_identity_keyword,
            decision_parameter_values=decision_parameter_values,
        )
        if core_identity_keyword
        else _build_title_suggestion(
            source_title,
            safe_accepted_keywords,
            decision_parameter_values=decision_parameter_values,
        )
    )
    safe_hot_keywords = _title_supported_keywords(
        hot_term_keywords,
        supported_claim_source,
    )
    safe_hot_keys = {keyword.casefold() for keyword in safe_hot_keywords}
    for keyword in hot_term_keywords:
        key = keyword.casefold()
        if key in validated_core_keys and key not in safe_hot_keys:
            safe_hot_keywords.append(keyword)
            safe_hot_keys.add(key)
    distinct_hot_keywords: list[str] = []
    seen_hot_phrases: set[tuple[str, ...]] = set()
    for keyword in safe_hot_keywords:
        canonical_phrase = tuple(
            _canonical_token(token) for token in TOKEN_PATTERN.findall(keyword.casefold())
        )
        if not canonical_phrase or canonical_phrase in seen_hot_phrases:
            continue
        seen_hot_phrases.add(canonical_phrase)
        distinct_hot_keywords.append(keyword)
    merged_hot_tokens, mergeable_hot_keywords = _merge_hot_term_keywords(
        distinct_hot_keywords
    )
    merged_hot_phrase = " ".join(merged_hot_tokens)
    hot_title = (
        _build_validated_identity_title_suggestion(
            source_title,
            merged_hot_phrase,
            decision_parameter_values=decision_parameter_values,
        )
        if merged_hot_phrase
        and not _title_supported_keywords([merged_hot_phrase], supported_claim_source)
        else _build_hot_term_title_suggestion(
            source_title,
            mergeable_hot_keywords,
            decision_parameter_values=decision_parameter_values,
        )
    )
    safe_opportunity_keywords = [
        keyword
        for keyword in opportunity_keywords
        if _keyword_claims_supported(keyword, supported_claim_source)
    ]
    opportunity_title = (
        _build_title_suggestion(
            core_title,
            safe_opportunity_keywords[:1],
            decision_parameter_values=decision_parameter_values,
        )
        if safe_opportunity_keywords
        else None
    )
    core_available = bool(safe_accepted_keywords)
    hot_available = bool(
        mergeable_hot_keywords
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
    journey_evidence = keyword_journey_evidence or {}

    def strategy_evidence(keywords: list[str]) -> dict[str, Any]:
        keyword_rows = [
            dict(journey_evidence[keyword.casefold()])
            for keyword in keywords
            if keyword.casefold() in journey_evidence
        ]
        journey_types = list(
            dict.fromkeys(
                str(journey_type)
                for row in keyword_rows
                for journey_type in row.get("journey_types", [])
                if str(journey_type)
            )
        )
        shopper_roots = list(
            dict.fromkeys(
                str(root)
                for row in keyword_rows
                for root in row.get("shopper_roots", [])
                if str(root)
            )
        )
        paths = [
            path
            for row in keyword_rows
            for path in row.get("paths", [])
            if isinstance(path, list) and path
        ]
        return {
            "journey_types": journey_types,
            "shopper_roots": shopper_roots,
            "paths": paths,
        }

    return [
        {
            "strategy": "contiguous_core",
            "label": "完整连续词组版",
            "title": core_title if core_available else None,
            "available": core_available,
            "explanation": (
                "优先把已知长尾直接验证，或证据最明确且通过完整首页同类验证的词组连续前置；"
                "证据词已获当前标题支持时，成稿必须原样包含该词，不得被另一模型身份词替换；"
                "当前标题商品名与图文识别冲突时，仅允许S级同品词及首页同需求竞品密度、"
                "平台供给规模共同支持的商品身份词替换原商品名；"
                "商品类型在前、功能卖点居中；运营逐项确认为决策参数且通过同商品族"
                "搜索页验证的规格可以前置，"
                "未确认或确认为非决策参数的功率、尺寸和防护等级等规格统一放在末尾；"
                "修改后仍需按相同词复采，不能保证排名前移。"
                if core_available
                else (
                    "本轮有搜索词通过首页同类验证，但其中包含当前标题或已确认事实无法支持、"
                    "或与现有参数冲突的词，因此为避免错误改题，不生成连续词组版。"
                    if validated_but_unsupported
                    else "本轮没有通过首页同类验证的核心词，因此不生成连续词组版。"
                )
            ),
            "evidence_keywords": safe_accepted_keywords[:1],
            "evidence": strategy_evidence(safe_accepted_keywords[:1]),
        },
        {
            "strategy": "hot_term_coverage",
            "label": "类目热词覆盖版",
            "title": hot_title if hot_available else None,
            "available": hot_available,
            "explanation": (
                "采用已通过相关性验证且真实出现在 Takealot 完整根词扩展中的类目表达；"
                "一个与连续词组版不同的安全表达即可形成覆盖版，多个表达再优先跨根词自然合并；"
                "根词内扩展顺序不是搜索量，修改后仍需复采。"
                if hot_available
                else (
                    "本轮虽有类目查询通过，但没有可安全写入标题、来自真实平台根词扩展且"
                    "与连续词组版不同的类目表达，因此不生成热词覆盖版。"
                    if accepted_keywords
                    else "本轮没有形成与完整连续词组版不同，且同时满足标题支持、相关性通过和平台根词扩展证据的类目词版本。"
                )
            ),
            "evidence_keywords": mergeable_hot_keywords[:3] if hot_available else [],
            "evidence": strategy_evidence(mergeable_hot_keywords[:3] if hot_available else []),
        },
        {
            "strategy": "adjacent_opportunity",
            "label": "S/A蓝海命名版",
            "title": opportunity_title if opportunity_available else None,
            "available": opportunity_available,
            "explanation": (
                "仅使用实际结果已判为S级同品直称或A级相邻替代需求、能找到本商品、"
                "自然位不超过72，且首页扣除本商品后直接同类不超过2个的平台根词扩展词；"
                "词根来源优先级不参与S/A判级，"
                "这是待复采打法，不保证排名前移。"
                if opportunity_available
                else (
                    "S/A级蓝海词已通过证据门槛，但没有形成与前两种打法不同的标题。"
                    if safe_opportunity_keywords
                    else "本轮没有搜索词同时通过S/A语义关系、平台根词扩展、目标命中、前72位和低同类竞争门槛。"
                )
            ),
            "evidence_keywords": safe_opportunity_keywords[:1],
            "evidence": strategy_evidence(safe_opportunity_keywords[:1]),
        },
    ]


def _build_title_from_priority_tokens(
    raw_suggestion: str,
    priority_tokens: list[str],
    *,
    leading_tokens: list[str] | None = None,
    front_parameter_tokens: list[str] | None = None,
) -> str:
    suggestion_tokens = _title_tokens(raw_suggestion)
    suggestion_parameter_indexes = _title_parameter_indexes(suggestion_tokens)
    priority_parameter_indexes = _title_parameter_indexes(priority_tokens)
    suggestion_colour_indexes = {
        index
        for index, token in enumerate(suggestion_tokens)
        if _canonical_token(token.casefold()) in _VARIANT_COLOUR_TOKENS
    }
    priority_colour_indexes = {
        index
        for index, token in enumerate(priority_tokens)
        if _canonical_token(token.casefold()) in _VARIANT_COLOUR_TOKENS
    }
    front_parameters = front_parameter_tokens or []
    front_parameter_keys = {_title_dedup_key(token) for token in front_parameters}
    front_parameter_compact = "".join(token.casefold() for token in front_parameters)
    preferred_case: dict[str, str] = {}
    for token in suggestion_tokens:
        preferred_case.setdefault(token.casefold(), token)

    output: list[str] = []
    output_keys: set[str] = set()

    for token in leading_tokens or []:
        key = _title_dedup_key(token)
        if key in output_keys:
            continue
        output.append(_title_token_case(token, preferred_case))
        output_keys.add(key)

    for token in front_parameters:
        key = _title_dedup_key(token)
        if key in output_keys:
            continue
        output.append(_title_token_case(token, preferred_case))
        output_keys.add(key)

    for index, token in enumerate(priority_tokens):
        if index in priority_colour_indexes:
            continue
        if index in priority_parameter_indexes and _title_dedup_key(token) not in front_parameter_keys:
            continue
        key = _title_dedup_key(token)
        if key in output_keys:
            continue
        output.append(_title_token_case(token, preferred_case))
        output_keys.add(key)

    for index, token in enumerate(suggestion_tokens):
        if index in suggestion_parameter_indexes or index in suggestion_colour_indexes:
            continue
        key = _title_dedup_key(token)
        if key in output_keys:
            continue
        output.append(token)
        output_keys.add(key)

    if not output:
        output = ["Product"]
    tail_colours = [
        token
        for index, token in enumerate(suggestion_tokens)
        if index in suggestion_colour_indexes
    ] or [
        _title_token_case(token, preferred_case)
        for index, token in enumerate(priority_tokens)
        if index in priority_colour_indexes
    ]
    for token in tail_colours:
        key = _title_dedup_key(token)
        if key in output_keys:
            continue
        output.append(token)
        output_keys.add(key)
    source_parameters = [
        token
        for index, token in enumerate(suggestion_tokens)
        if index in suggestion_parameter_indexes
        and _title_dedup_key(token) not in front_parameter_keys
        and token.casefold() != front_parameter_compact
    ]
    priority_parameters = [
        _title_token_case(token, preferred_case)
        for index, token in enumerate(priority_tokens)
        if index in priority_parameter_indexes
        and _title_dedup_key(token) not in front_parameter_keys
        and token.casefold() != front_parameter_compact
    ]
    output.extend(source_parameters or priority_parameters)
    while output and len(" ".join(output)) > TITLE_MAX_LENGTH:
        output.pop()
    return " ".join(output) or "Product"


def _validated_title_decision_parameter_tokens(
    source_title: str,
    validated_keywords: list[str],
    *,
    decision_parameter_values: list[str] | None = None,
) -> list[str]:
    confirmed_keys = {
        " ".join(_title_tokens(value)).casefold()
        for value in (decision_parameter_values or [])
        if " ".join(_title_tokens(value))
    }
    if not confirmed_keys:
        return []
    source_candidates = _title_parameter_candidates(source_title)
    output: list[str] = []
    for candidate in source_candidates:
        key = str(candidate["parameter_key"])
        if key not in confirmed_keys:
            continue
        value_tokens = _canonical_tokens(str(candidate["parameter_value"]))
        if not any(
            value_tokens and value_tokens.issubset(_canonical_tokens(keyword))
            for keyword in validated_keywords
        ):
            continue
        output.extend(_title_tokens(str(candidate["parameter_value"])))
    return output


def _title_parameter_indexes(tokens: list[str]) -> set[int]:
    """Identify explicit units, protection ratings, dimensions and pack quantities."""
    normalized = [token.casefold() for token in tokens]
    parameter_indexes: set[int] = set()

    for index, token in enumerate(normalized):
        if TITLE_PROTECTION_RATING_PATTERN.fullmatch(token):
            parameter_indexes.add(index)
            continue
        if (
            not TITLE_CONNECTIVITY_GENERATION_PATTERN.fullmatch(token)
            and (
                TITLE_COMBINED_PARAMETER_PATTERN.fullmatch(token)
                or TITLE_DIMENSION_PARAMETER_PATTERN.fullmatch(token)
                or TITLE_RESOLUTION_PARAMETER_PATTERN.fullmatch(token)
            )
        ):
            parameter_indexes.add(index)

    for index in range(len(normalized) - 1):
        if not _is_title_number(normalized[index]):
            continue
        next_token = normalized[index + 1]
        if next_token in TITLE_PARAMETER_UNIT_TOKENS | TITLE_PARAMETER_COUNT_TOKENS:
            parameter_indexes.update({index, index + 1})

    for index in range(len(normalized) - 2):
        if (
            normalized[index] in TITLE_PARAMETER_COUNT_TOKENS
            and normalized[index + 1] == "of"
            and _is_title_number(normalized[index + 2])
        ):
            parameter_indexes.update({index, index + 1, index + 2})

    index = 0
    while index < len(normalized) - 2:
        if not _is_title_number(normalized[index]) or normalized[index + 1] != "x":
            index += 1
            continue
        end = index + 2
        if not _is_title_number(normalized[end]):
            index += 1
            continue
        while (
            end + 2 < len(normalized)
            and normalized[end + 1] == "x"
            and _is_title_number(normalized[end + 2])
        ):
            end += 2
        if end + 1 < len(normalized) and normalized[end + 1] in TITLE_PARAMETER_UNIT_TOKENS:
            end += 1
        parameter_indexes.update(range(index, end + 1))
        index = end + 1

    for index in range(len(normalized) - 1):
        if not _is_title_number(normalized[index]):
            continue
        if index + 1 in parameter_indexes:
            parameter_indexes.add(index)

    return parameter_indexes


def _is_title_number(token: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", token))


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


def _title_suggestion_reason(
    accepted_keywords: list[str],
    *,
    validated_keyword_count: int = 0,
) -> str:
    if accepted_keywords:
        return (
            "建议标题已由服务器按固定规则整理：首个通过验证且获当前标题支持的核心词"
            "完整前置，商品类型在前、功能卖点居中，功率、电压、容量、尺寸、重量、数量及"
            "防护等级等明确规格参数稳定后置；标题只保留字母、数字和空格。修改后仍需使用"
            "相同搜索词复采排名，不能保证前移。"
        )
    return (
        "已有搜索词通过 Takealot 相关性验证，但其词面包含当前标题或已确认事实无法支持、"
        "或与现有参数冲突的内容，因此不生成可执行标题建议。"
        if validated_keyword_count
        else "当前没有候选搜索词通过 Takealot 相关性验证；建议标题只执行无标点清洗，"
        "不能据此判断排名会前移。"
    )


def _opportunity_title_reason(opportunity_keywords: list[str]) -> str:
    return (
        f"蓝海命名版把平台根词扩展词“{opportunity_keywords[0]}”前置："
        "该词先经搜索结果判为S级同品直称或A级相邻替代需求，且本轮已找到目标商品 "
        "自然位不超过72 首页扣除本商品后直接同类不超过2个。"
        "同一根词内的扩展顺序不是搜索量，修改后仍需复采且不能保证前移。"
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


def _variant_review_from_vision(
    vision_payload: Mapping[str, Any],
    *,
    current_offer_id: str | None,
    current_title: str | None,
) -> dict[str, Any] | None:
    reviews = vision_payload.get("variant_reviews")
    if not isinstance(reviews, list):
        return None
    normalized_offer_id = str(current_offer_id or "").strip()
    normalized_title = " ".join(str(current_title or "").split()).casefold()
    for raw_review in reviews:
        if not isinstance(raw_review, Mapping):
            continue
        if str(raw_review.get("offer_id") or "").strip() != normalized_offer_id:
            continue
        if " ".join(str(raw_review.get("title") or "").split()).casefold() != normalized_title:
            continue
        return dict(raw_review)
    return None


def _family_variant_from_profile(
    family_profile: Mapping[str, Any] | None,
    offer_id: str,
) -> dict[str, Any] | None:
    variants = family_profile.get("variants") if isinstance(family_profile, Mapping) else None
    if not isinstance(variants, list):
        return None
    return next(
        (
            dict(item)
            for item in variants
            if isinstance(item, Mapping)
            and str(item.get("offer_id") or "").strip() == str(offer_id).strip()
        ),
        None,
    )


def _offer_summary(
    offer: OfferCurrent,
    analysis: SearchRankingAnalysis | None,
    eligibility: OfferEligibility,
    *,
    family_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    variant = _family_variant_from_profile(family_profile, str(offer.offer_id))
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
        "shared_family_title": (
            str(family_profile.get("shared_title") or "") or None
            if isinstance(family_profile, Mapping)
            else None
        ),
        "family_representative_offer_id": (
            str(family_profile.get("representative_offer_id") or "") or None
            if isinstance(family_profile, Mapping)
            else str(offer.offer_id)
        ),
        "variant_count": (
            _optional_int(family_profile.get("variant_count")) or 1
            if isinstance(family_profile, Mapping)
            else 1
        ),
        "variant_parameters": (
            list(variant.get("parameters") or []) if isinstance(variant, Mapping) else []
        ),
        "variant_parameter_source": (
            str(family_profile.get("variant_parameter_source") or "") or None
            if isinstance(family_profile, Mapping)
            else None
        ),
        "variant_parameters_visually_verified": False,
        "latest_analysis": (
            _analysis_history_item(
                analysis,
                current_offer_id=str(offer.offer_id),
                current_title=str(offer.title or ""),
            )
            if analysis
            else None
        ),
    }


def _analysis_history_item(
    analysis: SearchRankingAnalysis,
    *,
    current_offer_id: str | None = None,
    current_title: str | None = None,
) -> dict[str, Any]:
    vision = analysis.vision_payload or {}
    usage = vision.get("usage", {}) if isinstance(vision, Mapping) else {}
    variant_review = (
        _variant_review_from_vision(
            vision,
            current_offer_id=current_offer_id,
            current_title=current_title,
        )
        if isinstance(vision, Mapping)
        else None
    )
    recognition = (
        variant_review.get("recognition", {})
        if isinstance(variant_review, Mapping)
        else vision.get("recognition", {}) if isinstance(vision, Mapping) else {}
    )
    if not isinstance(recognition, Mapping):
        recognition = {}
    raw_failure_audit = (
        vision.get("failure_audit", {}) if isinstance(vision, Mapping) else {}
    )
    failure_audit = (
        {
            key: raw_failure_audit[key]
            for key in (
                "stage",
                "summary",
                "validation_errors",
                "normalization",
            )
            if key in raw_failure_audit
        }
        if isinstance(raw_failure_audit, Mapping)
        else {}
    )
    raw_title_score = (
        variant_review.get("title_score", {})
        if isinstance(variant_review, Mapping)
        else vision.get("title_score", {}) if isinstance(vision, Mapping) else {}
    )
    score_title = (
        str(variant_review.get("title") or "")
        if isinstance(variant_review, Mapping)
        else str(analysis.source_title or "")
    )
    title_score = (
        _normalize_title_score_payload(
            raw_title_score,
            source_title=score_title,
        )
        if isinstance(raw_title_score, Mapping)
        else None
    ) or {}
    title_is_current = bool(variant_review) or (
        " ".join(current_title.split()).casefold()
        == " ".join(str(analysis.source_title or "").split()).casefold()
        if current_title is not None
        else True
    )
    return {
        "id": analysis.id,
        "status": analysis.status,
        "source_offer_id": analysis.offer_id,
        "source_title": analysis.source_title,
        "provider": analysis.provider,
        "model": analysis.model,
        "confidence": _float_or_none(analysis.confidence),
        "vision_reused": analysis.vision_reused,
        "created_at": analysis.created_at.isoformat(),
        "completed_at": analysis.completed_at.isoformat() if analysis.completed_at else None,
        "error": analysis.error,
        "failure_audit": failure_audit or None,
        "vision_stage_completed": bool(
            isinstance(vision, Mapping) and vision.get("vision_stage_completed") is True
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
        "title_score_value": (
            _optional_int(title_score.get("score")) if title_is_current else None
        ),
        "title_score_band": (
            str(title_score.get("band") or "") or None if title_is_current else None
        ),
        "title_score_evidence_coverage": (
            _optional_int(title_score.get("evidence_coverage"))
            if title_is_current
            else None
        ),
        "title_score_current_title_match": title_is_current,
        "identity_difference_level": (
            str(recognition.get("identity_difference_level") or "") or None
            if title_is_current
            else None
        ),
        "identity_large_difference": bool(
            title_is_current and recognition.get("identity_large_difference")
        ),
        "manual_fact_required": bool(
            title_is_current and recognition.get("manual_fact_required")
        ),
        "manual_fact_reason": (
            str(recognition.get("manual_fact_reason") or "") or None
            if title_is_current
            else None
        ),
        "variant_projection_applied": bool(
            variant_review
            and str(current_offer_id or "").strip() != str(analysis.offer_id).strip()
        ),
        "variant_parameters": (
            list(variant_review.get("variant_parameters") or [])
            if isinstance(variant_review, Mapping)
            else []
        ),
    }


def _analysis_payload(
    analysis: SearchRankingAnalysis,
    results: list[SearchRankingKeywordResult],
    *,
    current_offer_id: str | None = None,
    current_title: str | None = None,
    current_image_url: str | None = None,
    current_decision_parameter_profile: Mapping[str, Any] | None = None,
    current_variant_family: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    vision = analysis.vision_payload or {}
    variant_review = (
        _variant_review_from_vision(
            vision,
            current_offer_id=current_offer_id,
            current_title=current_title,
        )
        if isinstance(vision, Mapping)
        else None
    )
    raw_profile = vision.get("profile", vision) if isinstance(vision, dict) else {}
    profile = dict(raw_profile) if isinstance(raw_profile, Mapping) else {}
    raw_distinctive_terms = profile.get("distinctive_terms")
    profile_distinctive_terms = (
        [str(term) for term in raw_distinctive_terms if str(term).strip()]
        if isinstance(raw_distinctive_terms, list)
        else []
    )
    product_fact_terms = (
        [
            " ".join(str(value).split())
            for value in variant_review.get("applied_product_fact_terms", [])
            if " ".join(str(value).split())
        ]
        if isinstance(variant_review, Mapping)
        else _product_fact_terms_from_vision(vision)
    )
    effective_title = " ".join(
        str(
            variant_review.get("title")
            if isinstance(variant_review, Mapping)
            else current_title or analysis.source_title
        ).split()
    )
    evidence_source_title = _title_evidence_source(
        effective_title,
        product_fact_terms,
    )
    (
        accepted_title_keywords,
        hot_term_title_keywords,
        opportunity_title_keywords,
    ) = _title_strategy_keywords(
        results,
        evidence_source_title,
        profile_distinctive_terms=profile_distinctive_terms,
    )
    stored_decision_profile = (
        variant_review.get("decision_parameter_profile", {})
        if isinstance(variant_review, Mapping)
        else vision.get("decision_parameter_profile", {})
        if isinstance(vision, Mapping)
        else {}
    )
    if not isinstance(stored_decision_profile, Mapping):
        stored_decision_profile = {}
    decision_parameter_values = [
        " ".join(str(value).split())
        for value in stored_decision_profile.get("applied_decision_values", [])
        if " ".join(str(value).split())
    ]
    title_reason = _title_suggestion_reason(
        accepted_title_keywords,
        validated_keyword_count=sum(item.relevance_status == "accepted" for item in results),
    )
    title_strategies = _build_title_strategies(
        source_title=effective_title,
        evidence_source_title=evidence_source_title,
        accepted_keywords=accepted_title_keywords,
        hot_term_keywords=hot_term_title_keywords,
        opportunity_keywords=opportunity_title_keywords,
        validated_core_keywords=accepted_title_keywords,
        decision_parameter_values=decision_parameter_values,
        keyword_journey_evidence=_title_keyword_journey_evidence(
            results,
            source_title=evidence_source_title,
        ),
    )
    title_suggestion = str(
        title_strategies[0]["title"]
        or _build_title_suggestion(effective_title, accepted_title_keywords)
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
    raw_title_score = (
        variant_review.get("title_score")
        if isinstance(variant_review, Mapping)
        and isinstance(variant_review.get("title_score"), Mapping)
        else vision.get("title_score")
        if isinstance(vision, dict) and isinstance(vision.get("title_score"), Mapping)
        else None
    )
    title_matches_analysis = bool(variant_review) or (
        " ".join(str(current_title or "").split()).casefold()
        == " ".join(str(analysis.source_title or "").split()).casefold()
    )
    score_source_title = (
        effective_title if title_matches_analysis else str(analysis.source_title or "")
    )
    title_score = (
        _normalize_title_score_payload(
            raw_title_score,
            source_title=score_source_title,
        )
        if isinstance(raw_title_score, Mapping)
        else None
    )

    recognition = (
        dict(variant_review.get("recognition") or {})
        if isinstance(variant_review, Mapping)
        else dict(vision.get("recognition") or {})
        if isinstance(vision, Mapping) and isinstance(vision.get("recognition"), Mapping)
        else {}
    )
    raw_score_profile: Any = None
    if isinstance(vision, Mapping):
        raw_score_profile = vision.get("fusion_profile")
        if not isinstance(raw_score_profile, Mapping):
            raw_score_profile = vision.get("model_profile")
    if not isinstance(raw_score_profile, Mapping):
        raw_score_profile = raw_profile
    try:
        score_profile = (
            VisionProfile.model_validate(raw_score_profile)
            if isinstance(raw_score_profile, Mapping)
            else None
        )
    except ValidationError:
        score_profile = None
    if title_matches_analysis and score_profile is not None:
        title_score = _title_score_payload(
            source_title=effective_title,
            profile=score_profile,
            recognition=recognition,
            observations=results,
            confirmed_fact_terms=product_fact_terms,
        )
        if (
            current_image_url
            and str(current_image_url).strip()
            != str(analysis.source_image_url or "").strip()
        ):
            title_score.setdefault("limitations", []).append(
                "该分数复用商品族代表图和同一组搜索页证据；代表图不验证此 Offer 的颜色、尺寸、容量等变体值。"
            )
    if title_score is not None:
        title_score["current_title_match"] = title_matches_analysis
    stored_family = (
        dict(vision.get("variant_family") or {})
        if isinstance(vision, Mapping) and isinstance(vision.get("variant_family"), Mapping)
        else {}
    )
    live_variant = _family_variant_from_profile(
        current_variant_family,
        str(current_offer_id or analysis.offer_id),
    )
    variant_parameters = (
        list(variant_review.get("variant_parameters") or [])
        if isinstance(variant_review, Mapping)
        else list(live_variant.get("parameters") or [])
        if isinstance(live_variant, Mapping)
        else []
    )
    family_snapshot_current = bool(
        stored_family
        and isinstance(current_variant_family, Mapping)
        and _variant_family_cache_material(stored_family)
        == _variant_family_cache_material(current_variant_family)
    )

    def latest_confirmation_id(profile_value: Mapping[str, Any] | None) -> int | None:
        latest = profile_value.get("latest_confirmation") if profile_value else None
        return _optional_int(latest.get("id")) if isinstance(latest, Mapping) else None

    decision_confirmation_current = (
        latest_confirmation_id(stored_decision_profile)
        == latest_confirmation_id(current_decision_parameter_profile)
    )
    variant_projection = {
        "family_analysis_shared": True,
        "applied": bool(
            variant_review
            and str(current_offer_id or "").strip() != str(analysis.offer_id).strip()
        ),
        "source_offer_id": analysis.offer_id,
        "current_offer_id": str(current_offer_id or analysis.offer_id),
        "current_title": effective_title,
        "title_review_available": bool(variant_review),
        "family_snapshot_current": family_snapshot_current,
        "decision_parameter_confirmation_current": decision_confirmation_current,
        "variant_parameters": variant_parameters,
        "variant_parameter_source": "current_seller_offer_titles",
        "variant_parameters_visually_verified": False,
        "image_evidence_scope": "representative_offer_only",
        "current_image_matches_representative": bool(
            current_image_url
            and str(current_image_url).strip() == str(analysis.source_image_url).strip()
        ),
    }
    return {
        **_analysis_history_item(
            analysis,
            current_offer_id=current_offer_id,
            current_title=current_title,
        ),
        "product_name": analysis.product_name,
        "category": analysis.category,
        "profile": profile,
        "recognition": recognition,
        "title_score": title_score,
        "visual_profile": (
            vision.get("visual_profile", {}) if isinstance(vision, dict) else {}
        ),
        "fusion_profile": (
            vision.get("fusion_profile", {}) if isinstance(vision, dict) else {}
        ),
        "autocomplete_checks": (
            vision.get("autocomplete_checks", []) if isinstance(vision, dict) else []
        ),
        "root_expansion_checks": (
            vision.get("root_expansion_checks", vision.get("autocomplete_checks", []))
            if isinstance(vision, dict)
            else []
        ),
        "shopper_journey": (vision.get("shopper_journey", {}) if isinstance(vision, dict) else {}),
        "provider_attempts": (
            vision.get("provider_attempts", []) if isinstance(vision, dict) else []
        ),
        "product_fact_profile": (
            vision.get("product_fact_profile", {}) if isinstance(vision, dict) else {}
        ),
        "decision_parameter_profile": (
            dict(stored_decision_profile)
        ),
        "variant_family": stored_family,
        "variant_projection": variant_projection,
        "product_fact_recommendation": (
            vision.get("product_fact_recommendation")
            if isinstance(vision, dict)
            and isinstance(vision.get("product_fact_recommendation"), Mapping)
            else _product_fact_recommendation_from_analysis(
                analysis,
                results,
            )
        ),
        "usage": vision.get("usage", {}) if isinstance(vision, dict) else {},
        "estimated_cost_cny": (
            _float_or_none(vision.get("estimated_cost_cny")) if isinstance(vision, dict) else None
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
                source_title=evidence_source_title,
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
        dict(item.validation_evidence) if isinstance(item.validation_evidence, Mapping) else {}
    )
    relevance_status = item.relevance_status
    if profile_distinctive_terms is not None and "profile_distinctive_terms" not in evidence:
        evidence["profile_distinctive_terms"] = list(profile_distinctive_terms)
    if (
        source_title is not None
        and relevance_status != "accepted"
        and (
            relevance_status == "opportunity" or evidence.get("intended_strategy") == "opportunity"
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
            "opportunity" if opportunity_gate["opportunity_qualified"] else "rejected_irrelevant"
        )
        if relevance_status == "opportunity" and not opportunity_gate["opportunity_qualified"]:
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


def _chat_profile_json(
    body: Mapping[str, Any],
    *,
    function_name: str = "submit_takealot_product_profile",
) -> str:
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
            function = tool_call.get("function") if isinstance(tool_call, Mapping) else None
            if not isinstance(function, Mapping):
                continue
            if function.get("name") not in {
                function_name,
                # Read historical/test fixtures and providers that ignore the
                # requested tool name but still return the required schema.
                "submit_takealot_product_profile",
            }:
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


def _validated_chat_profile(
    body: Mapping[str, Any],
    *,
    function_name: str = "submit_takealot_product_profile",
    profile_type: type[VisionProfile] = VisionProfile,
) -> VisionProfile:
    """Normalize minor provider schema variance before strict business validation."""
    raw = json.loads(_chat_profile_json(body, function_name=function_name))
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
    raw.setdefault("same_product_aliases", [])
    if profile_type is FusionVisionProfile:
        raw, _ = _normalize_fusion_payload(raw)
    return profile_type.model_validate(raw)


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
    path = _thumbnail_path(settings, image_url)
    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as exc:
        raise SearchRankingProviderError("商品主图暂时无法读取，未调用多模态模型") from exc
    return f"data:image/jpeg;base64,{encoded}"


def _thumbnail_path(
    settings: SearchRankingRuntimeSettings,
    image_url: str,
) -> Path:
    cache = ProductThumbnailCache(settings.project_root)
    try:
        return cache.thumbnail_path(image_url, settings.image_max_dimension).resolve()
    except (ProductImageInputError, ProductImageUnavailableError, OSError) as exc:
        raise SearchRankingProviderError("商品主图暂时无法读取，未调用多模态模型") from exc
    finally:
        cache.close()


def _normalized_vision_usage(body: Any) -> dict[str, int]:
    usage = body.get("usage") if isinstance(body, Mapping) else {}
    normalized = usage if isinstance(usage, Mapping) else {}
    input_tokens = (
        _optional_int(normalized.get("prompt_tokens") or normalized.get("input_tokens")) or 0
    )
    output_tokens = (
        _optional_int(normalized.get("completion_tokens") or normalized.get("output_tokens")) or 0
    )
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
        int(usage.get("input_tokens") or 0) * provider.input_price_cny_per_million
        + int(usage.get("output_tokens") or 0) * provider.output_price_cny_per_million
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
        raise SearchRankingConfigurationError(f"{name} 必须在 {minimum:g} 到 {maximum:g} 之间")
    return value


_VISUAL_SYSTEM_PROMPT = """
You are the isolated visual-observation stage of a Takealot search-intent pipeline. You do
not receive the seller title, SKU, listing metadata, or historical keyword. Identify the
physical product from visible pixels instead of trusting packaging text. product_name must
be a concise 2-7 word identity, never an SEO title. Keep keywords and root-expansion seeds
to 2-5 cautious visual hypotheses in this stage; a root may be one complete word or a
meaningful 2-5 word product phrase and must never be reduced to only its first modifier.
They are evidence for a later fusion call, not final search recommendations.

The target market is always South Africa. Every word or phrase you generate in every text
field must be predicted for South African local customer habits and written in South African
English. Explicitly return market_context="South Africa", language_variant="South African
English", and shopper_context="South African local customer habits". This market context
governs wording only: it never permits you to invent a physical fact that is not visible, and
it is a prediction rather than measured search demand.

Accuracy beats breadth. Do not invent a brand, model, material, compatibility, capacity,
size, audience, or feature that is not visible. product_type_terms must be short noun
phrases likely to occur in genuinely same-type result titles. Its first item must be the
narrow physical form or shape. distinctive_terms describe visible differentiators and
exclusions list plausible visual confusions. Set requires_human_fact_confirmation false in
this observation stage; the later image-title fusion stage owns that decision. The title
suggestion is only visual evidence and never a promise that ranking will improve.
""".strip()


_FUSION_SYSTEM_PROMPT = """
You are the image-title fusion stage of a Takealot search-intent pipeline. The user message
contains one representative seller main title, its product image, a shared family subject,
separately listed Offer titles and title-derived variant parameters, an isolated image-only
observation, and a separately computed cross-validation summary. Use both image and title
evidence. Never silently treat either one as ground truth when they differ. Generate one
shared product-family search path. A size, colour, capacity, quantity, model, or other value
belonging to one Offer must not be generalized to sibling Offers. Variant values are Seller
title evidence only; the representative image does not visually verify sibling values.

The target market is always South Africa. Every model-generated text field, including the
product identity, category, aliases, roots, buyer jobs, alternatives, exclusions, complete
search phrases, title suggestion, and reasons, must be predicted for South African local
customer habits and written in South African English. Do not silently fall back to generic US
or UK marketplace vocabulary. Explicitly return market_context="South Africa",
language_variant="South African English", and shopper_context="South African local customer
habits". This is a mandatory localization context, not measured platform search demand.

Return the localized shopper wording in six deliberately different groups:
1. product_type_terms and same_product_aliases together form the same-product lexicon.
   product_type_terms must name the exact physical product type; same_product_aliases must be
   other concise names for that exact same physical product, not uses, accessories,
   complements, or merely similar products. Prefer natural 2-4 word buyer queries such as
   "cat storage box", "enclosed litter box", and "covered cat tray". The server searches
   eligible lexicon phrases before ordinary keywords and also submits the first four as
   complete Takealot expansion roots. Never use a generic one-word identity such as "box", "case",
   "bar", "light", "stand", or "device".
2. keywords: 6-10 additional concise, natural search queries a South African shopper would realistically
   type. Every query must contain 2-4 meaningful words, at least four queries must contain no
   more than 3 words, and no query may exceed 4 words. Lead with the common exact product type,
   then cover distinct high-confidence synonyms or essential use intent. Prefer compact forms
   such as "wireless mouse", "cat litter box", "hooded litter tray", or "projection screen".
   Do not write SEO-title fragments, descriptive sentences, colours, specifications, or chained
   "with"/"and" feature clauses unless that word is essential to identify the product. Avoid
   near duplicates.
3. autocomplete_seeds: 6-10 distinct complete shopper roots of 1-5 meaningful words. A root
   may be a single word or a natural product phrase such as "lazy sofa", "floor chair", or
   "l shaped desk". Prefer the shortest phrase that preserves the intended product identity;
   never collapse a known product phrase to only an ambiguous modifier such as "lazy",
   "floor", or "foldable". Never emit a character prefix or next-word initial. Mix product
   names, uses, room/context, connected items, local everyday wording, and other plausible
   entry instincts. The platform's raw ordered expansions are not automatically selected:
   the server retains only same-product identity or a structured adjacent product family,
   and may use a related platform expansion as one additional phrase root.
4. distinctive_terms: visible differentiators only. These are not automatically same-product
   names or direct search queries.
5. same_demand_product_terms: 4-12 concise product-family names for real substitutes a
   shopper could compare because they solve the same primary buyer need, even when their
   construction, material, or exact physical form differs. This group is deliberately broader
   than the exact same-product lexicon. Include the important substitute families likely to
   appear on a marketplace result page; do not include accessories, food or consumable storage,
   transport-only products, decorative lookalikes, or items that merely share one generic noun.
   A family must not be repeated from product_type_terms or same_product_aliases. Do not fill
   this group with feature or size variants of the exact product (for example "large litter
   tray", "top-entry litter box", or "self-cleaning litter box" when the exact product is
   already a litter box). Name genuinely different comparison families instead: for an enclosed
   cat toilet/house these may include "litter box enclosure", "cat house with litter box", or
   "cat cage with litter box" when they solve the same toileting/privacy job. The server counts
   this group separately from exact same-product results and never treats the terms themselves as
   measured search demand.
6. opportunity_seeds: 1-4 closely adjacent complete roots under the same 1-5 word rule.
   Every item must also contain buyer_job (the shared outcome in plain English), one or more
   alternative_product_terms naming different product families that can fulfil that job, and
   optional excluded_product_terms only for complements or genuinely irrelevant branches. Do
   not exclude a different product family merely because it has another material or form when
   it still solves the same primary buyer job; put that family in same_demand_product_terms.
   Do not put
   an exact same-product alias into this group. The server will still validate the actual first
   page and can reject this hypothesis; source priority never decides S/A relation grade.

Combine title facts with visible evidence, but do not invent unsupported claims. If the
image-title difference is large, keep it as a warning and still generate safe wording when
the product search intent can be resolved. Set requires_human_fact_confirmation true only
when a specific missing or contradictory fact makes safe search-intent generation
impossible. In that case provide a concise manual_fact_reason and 1-5 concrete missing_facts;
do not use this flag merely because confidence is imperfect. Otherwise set it false, use an
empty reason and empty missing_facts. title_suggestion must be punctuation-free natural
English with product type first. Nothing in this output promises a ranking improvement.
""".strip()

# Historical import compatibility for tests and internal callers. New requests use the
# explicit visual and fusion prompts above.
_SYSTEM_PROMPT = _VISUAL_SYSTEM_PROMPT
