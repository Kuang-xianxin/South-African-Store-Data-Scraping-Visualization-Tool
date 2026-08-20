from __future__ import annotations

from takealot_ops.competitors.api import _is_matching_listing_response_url


def test_listing_response_url_requires_takealot_search_endpoint() -> None:
    assert _is_matching_listing_response_url(
        "https://api.takealot.com/rest/v-1-19-0/"
        "searches/products,filters?qsearch=cat+storage+box"
    )
    assert not _is_matching_listing_response_url(
        "https://example.com/rest/v-1-19-0/"
        "searches/products,filters?qsearch=cat+storage+box"
    )
    assert not _is_matching_listing_response_url(
        "https://api.takealot.com/rest/v-1-19-0/searches/search_suggestions"
        "?qsearch=cat+storage+box"
    )


def test_listing_response_url_must_match_current_search_query() -> None:
    matching_url = (
        "https://api.takealot.com/rest/v-1-19-0/"
        "searches/products,filters?qsearch=Cat%20%20Storage+Box&client_id=test"
    )
    stale_url = (
        "https://api.takealot.com/rest/v-1-19-0/"
        "searches/products,filters?qsearch=cat+tree&client_id=test"
    )

    assert _is_matching_listing_response_url(
        matching_url,
        expected_qsearch="cat storage box",
    )
    assert not _is_matching_listing_response_url(
        stale_url,
        expected_qsearch="cat storage box",
    )
