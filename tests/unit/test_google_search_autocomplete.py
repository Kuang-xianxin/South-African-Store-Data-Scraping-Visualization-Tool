from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from takealot_ops.google_search_autocomplete import (
    GOOGLE_AUTOCOMPLETE_MIRROR_ENDPOINT,
    GOOGLE_AUTOCOMPLETE_PRIMARY_ENDPOINT,
    GOOGLE_AUTOCOMPLETE_VERIFIED_STATUS,
    AnonymousGoogleAutocompleteFetcher,
    GoogleAutocompleteEndpointEvidence,
    GoogleSearchAutocompleteService,
    collect_google_autocomplete_inputs,
)
from takealot_ops.storage.migrations import create_engine_for_database_url
from takealot_ops.storage.models import (
    GoogleSearchAutocompleteCapture,
    GoogleSearchAutocompleteCurrent,
)


class FakeGoogleAutocompleteFetcher:
    def __init__(
        self,
        responses: dict[str, tuple[str, ...] | Exception],
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def fetch(
        self,
        endpoint: str,
        input_text: str,
    ) -> GoogleAutocompleteEndpointEvidence:
        self.calls.append((endpoint, input_text))
        response = self.responses[endpoint]
        if isinstance(response, Exception):
            raise response
        raw_payload: list[object] = [input_text, list(response), [], [], {}]
        return GoogleAutocompleteEndpointEvidence(
            endpoint=endpoint,
            request_url=(
                f"{endpoint}?client=chrome&hl=en&gl=za&q={input_text.replace(' ', '+')}"
            ),
            response_query=input_text,
            suggestions=response,
            raw_payload=raw_payload,
            status_code=200,
            sent_cookie=False,
        )


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'google-autocomplete.db').as_posix()}"


def _service(
    tmp_path: Path,
    fetcher: FakeGoogleAutocompleteFetcher,
    captured_at: datetime,
) -> GoogleSearchAutocompleteService:
    return GoogleSearchAutocompleteService(
        tmp_path,
        database_url=_database_url(tmp_path),
        fetcher=fetcher,
        clock=lambda: captured_at,
    )


def test_anonymous_fetcher_uses_chrome_za_contract_without_cookie() -> None:
    observed_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_requests.append(request)
        return httpx.Response(
            200,
            json=[
                "camping tent",
                ["camping tents", "camping tents for sale south africa"],
                ["", ""],
                [],
                {"google:suggestrelevance": [1251, 1250]},
            ],
        )

    fetcher = AnonymousGoogleAutocompleteFetcher(
        transport=httpx.MockTransport(handler),
    )

    evidence = fetcher.fetch(
        GOOGLE_AUTOCOMPLETE_PRIMARY_ENDPOINT,
        "camping tent",
    )

    assert evidence.suggestions == (
        "camping tents",
        "camping tents for sale south africa",
    )
    assert len(observed_requests) == 1
    request = observed_requests[0]
    assert request.url.params["client"] == "chrome"
    assert request.url.params["hl"] == "en"
    assert request.url.params["gl"] == "za"
    assert request.url.params["q"] == "camping tent"
    assert "cookie" not in request.headers


def test_consensus_capture_persists_immutable_and_current_rows(tmp_path: Path) -> None:
    suggestions = (
        "camping tents",
        "camping tents for sale",
        "camping tents at makro",
    )
    fetcher = FakeGoogleAutocompleteFetcher(
        {
            GOOGLE_AUTOCOMPLETE_PRIMARY_ENDPOINT: suggestions,
            GOOGLE_AUTOCOMPLETE_MIRROR_ENDPOINT: suggestions,
        }
    )
    captured_at = datetime(2026, 8, 12, 3, 0, 0)

    result = _service(tmp_path, fetcher, captured_at).capture("  Camping   Tent ")

    assert result.verified is True
    assert result.verification_status == GOOGLE_AUTOCOMPLETE_VERIFIED_STATUS
    assert result.input_text == "Camping Tent"
    assert result.suggestions == suggestions
    assert result.current_updated is True
    assert fetcher.calls == [
        (GOOGLE_AUTOCOMPLETE_PRIMARY_ENDPOINT, "Camping Tent"),
        (GOOGLE_AUTOCOMPLETE_MIRROR_ENDPOINT, "Camping Tent"),
    ]

    engine = create_engine_for_database_url(_database_url(tmp_path))
    try:
        with Session(engine) as session:
            capture = session.scalar(select(GoogleSearchAutocompleteCapture))
            current = session.scalar(select(GoogleSearchAutocompleteCurrent))
            assert capture is not None
            assert current is not None
            assert capture.id == result.capture_id
            assert capture.input_key == "camping tent"
            assert capture.suggestions == list(suggestions)
            assert capture.primary_evidence is not None
            assert capture.primary_evidence["sent_cookie"] is False
            assert current.capture_id == capture.id
            assert current.suggestions == list(suggestions)
            assert current.refresh_count == 1
    finally:
        engine.dispose()


def test_disagreement_is_audited_without_overwriting_verified_current(
    tmp_path: Path,
) -> None:
    original = ("camping tents", "camping tents for sale")
    first_fetcher = FakeGoogleAutocompleteFetcher(
        {
            GOOGLE_AUTOCOMPLETE_PRIMARY_ENDPOINT: original,
            GOOGLE_AUTOCOMPLETE_MIRROR_ENDPOINT: original,
        }
    )
    first = _service(
        tmp_path,
        first_fetcher,
        datetime(2026, 8, 12, 3, 0, 0),
    ).capture("camping tent")
    disagreement_fetcher = FakeGoogleAutocompleteFetcher(
        {
            GOOGLE_AUTOCOMPLETE_PRIMARY_ENDPOINT: (
                "camping tents",
                "camping tent price",
            ),
            GOOGLE_AUTOCOMPLETE_MIRROR_ENDPOINT: (
                "camping tents",
                "camping tents at makro",
            ),
        }
    )

    failed = _service(
        tmp_path,
        disagreement_fetcher,
        datetime(2026, 8, 12, 4, 0, 0),
    ).capture("camping tent")

    assert failed.verified is False
    assert failed.verification_status == "endpoint_disagreement"
    assert failed.suggestions == ()
    assert failed.current_updated is False

    engine = create_engine_for_database_url(_database_url(tmp_path))
    try:
        with Session(engine) as session:
            capture_count = session.scalar(
                select(func.count()).select_from(GoogleSearchAutocompleteCapture)
            )
            current = session.scalar(select(GoogleSearchAutocompleteCurrent))
            failed_capture = session.get(
                GoogleSearchAutocompleteCapture,
                failed.capture_id,
            )
            assert capture_count == 2
            assert current is not None
            assert current.capture_id == first.capture_id
            assert current.suggestions == list(original)
            assert current.refresh_count == 1
            assert failed_capture is not None
            assert failed_capture.suggestions is None
            assert failed_capture.primary_evidence is not None
            assert failed_capture.mirror_evidence is not None
    finally:
        engine.dispose()


def test_source_failure_is_audited_without_creating_current_row(tmp_path: Path) -> None:
    fetcher = FakeGoogleAutocompleteFetcher(
        {
            GOOGLE_AUTOCOMPLETE_PRIMARY_ENDPOINT: RuntimeError("blocked"),
            GOOGLE_AUTOCOMPLETE_MIRROR_ENDPOINT: ("camping tents",),
        }
    )

    result = _service(
        tmp_path,
        fetcher,
        datetime(2026, 8, 12, 3, 0, 0),
    ).capture("camping tent")

    assert result.verification_status == "source_request_failed"
    assert result.error is not None
    assert "primary=RuntimeError: blocked" in result.error
    engine = create_engine_for_database_url(_database_url(tmp_path))
    try:
        with Session(engine) as session:
            assert session.scalar(select(GoogleSearchAutocompleteCurrent)) is None
            capture = session.get(GoogleSearchAutocompleteCapture, result.capture_id)
            assert capture is not None
            assert capture.primary_evidence is None
            assert capture.mirror_evidence is not None
    finally:
        engine.dispose()


def test_library_is_local_only_and_preserves_per_root_order(tmp_path: Path) -> None:
    suggestions = ("camping tents", "camping tents south africa")
    fetcher = FakeGoogleAutocompleteFetcher(
        {
            GOOGLE_AUTOCOMPLETE_PRIMARY_ENDPOINT: suggestions,
            GOOGLE_AUTOCOMPLETE_MIRROR_ENDPOINT: suggestions,
        }
    )
    service = _service(tmp_path, fetcher, datetime(2026, 8, 12, 3, 0, 0))
    service.capture("camping tent")
    calls_after_capture = list(fetcher.calls)

    payload = service.library_payload(search="south africa")

    assert fetcher.calls == calls_after_capture
    assert payload["policy"]["passive_read_triggers_external_request"] is False
    assert payload["policy"]["autocomplete_rank_is_search_volume"] is False
    assert payload["summary"] == {
        "input_count": 1,
        "matching_input_count": 1,
    }
    assert payload["items"][0]["suggestions"] == [
        {"phrase": "camping tents", "rank": 1},
        {"phrase": "camping tents south africa", "rank": 2},
    ]


def test_bounded_batch_deduplicates_roots_and_paces_between_inputs(
    tmp_path: Path,
) -> None:
    fetcher = FakeGoogleAutocompleteFetcher(
        {
            GOOGLE_AUTOCOMPLETE_PRIMARY_ENDPOINT: ("camping tents",),
            GOOGLE_AUTOCOMPLETE_MIRROR_ENDPOINT: ("camping tents",),
        }
    )
    service = _service(tmp_path, fetcher, datetime(2026, 8, 12, 3, 0, 0))
    pauses: list[float] = []

    results = collect_google_autocomplete_inputs(
        service,
        ["camping tent", " Camping Tent ", "outdoor tent"],
        delay_seconds=1.5,
        sleeper=pauses.append,
    )

    assert [result.input_text for result in results] == [
        "camping tent",
        "outdoor tent",
    ]
    assert pauses == [1.5]


@pytest.mark.parametrize("value", ["", "   ", "x" * 101])
def test_capture_rejects_invalid_inputs(tmp_path: Path, value: str) -> None:
    fetcher = FakeGoogleAutocompleteFetcher(
        {
            GOOGLE_AUTOCOMPLETE_PRIMARY_ENDPOINT: (),
            GOOGLE_AUTOCOMPLETE_MIRROR_ENDPOINT: (),
        }
    )

    with pytest.raises(ValueError):
        _service(tmp_path, fetcher, datetime(2026, 8, 12, 3, 0, 0)).capture(value)

    assert fetcher.calls == []
