"""Verified Google Search autocomplete evidence for the South-African region."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from takealot_ops.settings import DashboardSettings
from takealot_ops.storage.migrations import (
    create_engine_for_database_url,
    create_read_only_engine,
    create_schema,
)
from takealot_ops.storage.models import (
    GoogleSearchAutocompleteCapture,
    GoogleSearchAutocompleteCurrent,
)


GOOGLE_AUTOCOMPLETE_PRIMARY_ENDPOINT = "https://www.google.com/complete/search"
GOOGLE_AUTOCOMPLETE_MIRROR_ENDPOINT = "https://www.google.co.za/complete/search"
GOOGLE_AUTOCOMPLETE_REGION_CODE = "ZA"
GOOGLE_AUTOCOMPLETE_LANGUAGE_CODE = "en"
GOOGLE_AUTOCOMPLETE_CLIENT_NAME = "chrome"
GOOGLE_AUTOCOMPLETE_CONTRACT_VERSION = "google-chrome-za-dual-consensus-v1"
GOOGLE_AUTOCOMPLETE_VERIFIED_STATUS = "verified_dual_endpoint_consensus"
GOOGLE_AUTOCOMPLETE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _normalized_input(value: str) -> str:
    return " ".join(value.split())


def _input_key(value: str) -> str:
    return _normalized_input(value).casefold()


@dataclass(frozen=True)
class GoogleAutocompleteEndpointEvidence:
    """Validated response from one anonymous Google completion endpoint."""

    endpoint: str
    request_url: str
    response_query: str
    suggestions: tuple[str, ...]
    raw_payload: list[Any]
    status_code: int
    sent_cookie: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "request_url": self.request_url,
            "request_parameters": {
                "client": GOOGLE_AUTOCOMPLETE_CLIENT_NAME,
                "hl": GOOGLE_AUTOCOMPLETE_LANGUAGE_CODE,
                "gl": GOOGLE_AUTOCOMPLETE_REGION_CODE.lower(),
            },
            "response_query": self.response_query,
            "suggestions": list(self.suggestions),
            "raw_payload": self.raw_payload,
            "status_code": self.status_code,
            "sent_cookie": self.sent_cookie,
        }


@dataclass(frozen=True)
class GoogleAutocompleteCaptureResult:
    """Outcome of one dual-endpoint capture and persistence attempt."""

    capture_id: int
    input_text: str
    verification_status: str
    suggestions: tuple[str, ...]
    current_updated: bool
    captured_at: datetime
    error: str | None

    @property
    def verified(self) -> bool:
        return self.verification_status == GOOGLE_AUTOCOMPLETE_VERIFIED_STATUS

    def as_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "input_text": self.input_text,
            "verification_status": self.verification_status,
            "verified": self.verified,
            "suggestions": list(self.suggestions),
            "current_updated": self.current_updated,
            "captured_at": self.captured_at.isoformat(),
            "error": self.error,
        }


class GoogleAutocompleteFetcher(Protocol):
    """Fetch one endpoint without account cookies or browser history."""

    def fetch(
        self,
        endpoint: str,
        input_text: str,
    ) -> GoogleAutocompleteEndpointEvidence: ...


class AnonymousGoogleAutocompleteFetcher:
    """Use a fresh no-cookie HTTP session for each Google autocomplete endpoint."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Google 补全请求超时必须大于0秒")
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def fetch(
        self,
        endpoint: str,
        input_text: str,
    ) -> GoogleAutocompleteEndpointEvidence:
        params = {
            "client": GOOGLE_AUTOCOMPLETE_CLIENT_NAME,
            "hl": GOOGLE_AUTOCOMPLETE_LANGUAGE_CODE,
            "gl": GOOGLE_AUTOCOMPLETE_REGION_CODE.lower(),
            "q": input_text,
        }
        headers = {
            "Accept": "application/json,text/plain,*/*",
            "User-Agent": GOOGLE_AUTOCOMPLETE_USER_AGENT,
        }
        # A new client per endpoint deliberately prevents Google account cookies,
        # prior searches, or cookies set by the first hostname from entering evidence.
        with httpx.Client(
            headers=headers,
            follow_redirects=True,
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            response = client.get(endpoint, params=params)
        response.raise_for_status()
        if "cookie" in response.request.headers:
            raise ValueError("Google 补全请求意外携带Cookie，拒绝作为公共南非证据")
        raw_payload = cast(Any, response.json())
        response_query, suggestions = _validate_google_payload(raw_payload, input_text)
        return GoogleAutocompleteEndpointEvidence(
            endpoint=endpoint,
            request_url=str(response.url),
            response_query=response_query,
            suggestions=suggestions,
            raw_payload=cast(list[Any], raw_payload),
            status_code=response.status_code,
            sent_cookie=False,
        )


def _validate_google_payload(
    raw_payload: Any,
    expected_input: str,
) -> tuple[str, tuple[str, ...]]:
    if not isinstance(raw_payload, list) or len(raw_payload) < 2:
        raise ValueError("Google 补全响应不是预期的列表结构")
    response_query = raw_payload[0]
    raw_suggestions = raw_payload[1]
    if not isinstance(response_query, str):
        raise ValueError("Google 补全响应缺少原始输入词")
    if _input_key(response_query) != _input_key(expected_input):
        raise ValueError("Google 补全响应原始输入词与请求不一致")
    if not isinstance(raw_suggestions, list):
        raise ValueError("Google 补全响应缺少有序建议列表")
    suggestions: list[str] = []
    seen: set[str] = set()
    for raw_suggestion in raw_suggestions:
        if not isinstance(raw_suggestion, str):
            raise ValueError("Google 补全响应包含非文本建议")
        suggestion = _normalized_input(raw_suggestion)
        if not suggestion:
            raise ValueError("Google 补全响应包含空建议")
        key = suggestion.casefold()
        if key in seen:
            raise ValueError("Google 补全响应包含重复建议")
        seen.add(key)
        suggestions.append(suggestion)
    return _normalized_input(response_query), tuple(suggestions)


class GoogleSearchAutocompleteService:
    """Capture and read verified Google Chrome autocomplete evidence for ZA."""

    def __init__(
        self,
        project_root: Path,
        *,
        database_url: str | None = None,
        fetcher: GoogleAutocompleteFetcher | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.project_root = project_root.resolve()
        self.database_url = database_url or DashboardSettings.from_env(
            self.project_root
        ).database_url
        self.fetcher = fetcher or AnonymousGoogleAutocompleteFetcher()
        self.clock = clock

    def capture(self, input_text: str) -> GoogleAutocompleteCaptureResult:
        normalized_input = _normalized_input(input_text)
        if not normalized_input:
            raise ValueError("Google 补全词根不能为空")
        if len(normalized_input) > 100:
            raise ValueError("Google 补全词根不能超过100个字符")

        evidence: dict[str, GoogleAutocompleteEndpointEvidence] = {}
        errors: dict[str, str] = {}
        for label, endpoint in (
            ("primary", GOOGLE_AUTOCOMPLETE_PRIMARY_ENDPOINT),
            ("mirror", GOOGLE_AUTOCOMPLETE_MIRROR_ENDPOINT),
        ):
            try:
                evidence[label] = self.fetcher.fetch(endpoint, normalized_input)
            except Exception as exc:  # capture the failed attempt without stale overwrite
                errors[label] = f"{type(exc).__name__}: {exc}"

        primary = evidence.get("primary")
        mirror = evidence.get("mirror")
        if primary is None or mirror is None:
            verification_status = "source_request_failed"
            verified_suggestions: tuple[str, ...] = ()
            error = "; ".join(
                f"{label}={message}" for label, message in sorted(errors.items())
            )
        elif primary.suggestions != mirror.suggestions:
            verification_status = "endpoint_disagreement"
            verified_suggestions = ()
            error = "google.com与google.co.za的有序补全结果不一致"
        else:
            verification_status = GOOGLE_AUTOCOMPLETE_VERIFIED_STATUS
            verified_suggestions = primary.suggestions
            error = None

        captured_at = self.clock()
        engine = create_engine_for_database_url(self.database_url)
        try:
            create_schema(engine)
            with Session(engine) as session, session.begin():
                capture_row = GoogleSearchAutocompleteCapture(
                    region_code=GOOGLE_AUTOCOMPLETE_REGION_CODE,
                    language_code=GOOGLE_AUTOCOMPLETE_LANGUAGE_CODE,
                    client_name=GOOGLE_AUTOCOMPLETE_CLIENT_NAME,
                    input_key=_input_key(normalized_input),
                    input_text=normalized_input,
                    verification_status=verification_status,
                    suggestions=(
                        list(verified_suggestions)
                        if verification_status == GOOGLE_AUTOCOMPLETE_VERIFIED_STATUS
                        else None
                    ),
                    primary_endpoint=GOOGLE_AUTOCOMPLETE_PRIMARY_ENDPOINT,
                    mirror_endpoint=GOOGLE_AUTOCOMPLETE_MIRROR_ENDPOINT,
                    primary_evidence=primary.as_dict() if primary is not None else None,
                    mirror_evidence=mirror.as_dict() if mirror is not None else None,
                    source_contract_version=GOOGLE_AUTOCOMPLETE_CONTRACT_VERSION,
                    error=error,
                    captured_at=captured_at,
                )
                session.add(capture_row)
                session.flush()
                current_updated = False
                if verification_status == GOOGLE_AUTOCOMPLETE_VERIFIED_STATUS:
                    current_updated = True
                    current_row = session.scalar(
                        select(GoogleSearchAutocompleteCurrent).where(
                            GoogleSearchAutocompleteCurrent.region_code
                            == GOOGLE_AUTOCOMPLETE_REGION_CODE,
                            GoogleSearchAutocompleteCurrent.language_code
                            == GOOGLE_AUTOCOMPLETE_LANGUAGE_CODE,
                            GoogleSearchAutocompleteCurrent.client_name
                            == GOOGLE_AUTOCOMPLETE_CLIENT_NAME,
                            GoogleSearchAutocompleteCurrent.input_key
                            == _input_key(normalized_input),
                        )
                    )
                    if current_row is None:
                        session.add(
                            GoogleSearchAutocompleteCurrent(
                                capture_id=capture_row.id,
                                region_code=GOOGLE_AUTOCOMPLETE_REGION_CODE,
                                language_code=GOOGLE_AUTOCOMPLETE_LANGUAGE_CODE,
                                client_name=GOOGLE_AUTOCOMPLETE_CLIENT_NAME,
                                input_key=_input_key(normalized_input),
                                input_text=normalized_input,
                                suggestions=list(verified_suggestions),
                                verification_status=verification_status,
                                source_contract_version=(
                                    GOOGLE_AUTOCOMPLETE_CONTRACT_VERSION
                                ),
                                captured_at=captured_at,
                                refresh_count=1,
                                created_at=captured_at,
                                updated_at=captured_at,
                            )
                        )
                    else:
                        current_row.capture_id = capture_row.id
                        current_row.input_text = normalized_input
                        current_row.suggestions = list(verified_suggestions)
                        current_row.verification_status = verification_status
                        current_row.source_contract_version = (
                            GOOGLE_AUTOCOMPLETE_CONTRACT_VERSION
                        )
                        current_row.captured_at = captured_at
                        current_row.refresh_count += 1
                        current_row.updated_at = captured_at
                capture_id = capture_row.id
        finally:
            engine.dispose()

        return GoogleAutocompleteCaptureResult(
            capture_id=capture_id,
            input_text=normalized_input,
            verification_status=verification_status,
            suggestions=verified_suggestions,
            current_updated=current_updated,
            captured_at=captured_at,
            error=error,
        )

    def library_payload(
        self,
        *,
        search: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read verified current evidence without contacting Google."""

        normalized_search = _normalized_input(search).casefold()
        bounded_limit = max(1, min(limit, 500))
        engine = create_read_only_engine(self.database_url)
        try:
            with Session(engine) as session:
                rows = list(
                    session.scalars(
                        select(GoogleSearchAutocompleteCurrent).order_by(
                            GoogleSearchAutocompleteCurrent.captured_at.desc()
                        )
                    )
                )
        finally:
            engine.dispose()
        filtered_rows = [
            row
            for row in rows
            if not normalized_search
            or normalized_search in row.input_text.casefold()
            or any(
                normalized_search in suggestion.casefold()
                for suggestion in row.suggestions
            )
        ]
        return {
            "policy": {
                "region_code": GOOGLE_AUTOCOMPLETE_REGION_CODE,
                "language_code": GOOGLE_AUTOCOMPLETE_LANGUAGE_CODE,
                "client_name": GOOGLE_AUTOCOMPLETE_CLIENT_NAME,
                "source_contract_version": GOOGLE_AUTOCOMPLETE_CONTRACT_VERSION,
                "anonymous_no_cookie": True,
                "dual_endpoint_consensus_required": True,
                "autocomplete_rank_is_search_volume": False,
                "passive_read_triggers_external_request": False,
                "note": (
                    "每个词根下的顺序仅是该次Google Chrome补全顺序，不是搜索量；"
                    "仅google.com与google.co.za的ZA英文无Cookie响应完全一致时进入当前库。"
                ),
            },
            "summary": {
                "input_count": len(rows),
                "matching_input_count": len(filtered_rows),
            },
            "items": [
                {
                    "capture_id": row.capture_id,
                    "input_text": row.input_text,
                    "suggestions": [
                        {"phrase": phrase, "rank": rank}
                        for rank, phrase in enumerate(row.suggestions, start=1)
                    ],
                    "verification_status": row.verification_status,
                    "captured_at": row.captured_at.isoformat(),
                    "refresh_count": row.refresh_count,
                }
                for row in filtered_rows[:bounded_limit]
            ],
        }


def collect_google_autocomplete_inputs(
    service: GoogleSearchAutocompleteService,
    inputs: Sequence[str],
    *,
    delay_seconds: float = 2.0,
    sleeper: Callable[[float], None] | None = None,
) -> list[GoogleAutocompleteCaptureResult]:
    """Capture a bounded, caller-ordered set of roots without inventing expansion inputs."""

    if delay_seconds < 0:
        raise ValueError("Google补全采集间隔不能为负数")
    normalized_inputs: list[str] = []
    seen: set[str] = set()
    for value in inputs:
        normalized = _normalized_input(value)
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        normalized_inputs.append(normalized)
    if not normalized_inputs:
        raise ValueError("至少需要一个非空Google补全词根")
    if len(normalized_inputs) > 50:
        raise ValueError("单次最多采集50个Google补全词根")
    pause = sleeper
    if pause is None:
        import time

        pause = time.sleep
    results: list[GoogleAutocompleteCaptureResult] = []
    for index, value in enumerate(normalized_inputs):
        if index and delay_seconds:
            pause(delay_seconds)
        results.append(service.capture(value))
    return results
