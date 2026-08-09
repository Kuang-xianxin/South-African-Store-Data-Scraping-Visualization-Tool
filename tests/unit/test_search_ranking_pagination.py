from __future__ import annotations

from typing import Any

import pytest

from takealot_ops.search_ranking.service import (
    KeywordCandidate,
    SearchKeywordCandidate,
    VisionProfile,
    _collect_keyword_observation,
)


def _product(plid: str, title: str) -> dict[str, Any]:
    return {
        "type": "product_views",
        "product_views": {
            "core": {
                "id": int(plid),
                "title": title,
                "slug": title.casefold().replace(" ", "-"),
            }
        },
    }


def _payload(results: list[dict[str, Any]], *, after: str) -> dict[str, Any]:
    return {
        "sections": {
            "products": {
                "results": results,
                "paging": {"next_is_after": after, "total_num_found": 72},
            }
        }
    }


class CursorPageWithSponsoredClient:
    async def fetch_search_first_page(
        self,
        keyword: str,
    ) -> tuple[str, dict[str, Any]]:
        assert keyword == "wireless mouse"
        natural = [
            _product(str(81_000_000 + index), f"Wireless Mouse Model {index}")
            for index in range(35)
        ]
        sponsored = {
            **_product("89999999", "Wireless Mouse Sponsored"),
            "is_sponsored": True,
        }
        return (
            "https://api.takealot.com/rest/v-1-18-0/"
            "searches/products,filters?qsearch=wireless+mouse",
            _payload([sponsored, *natural], after="page-two"),
        )

    async def fetch_search_next_page(
        self,
        request_url: str,
        after: str,
    ) -> dict[str, Any]:
        assert "/rest/v-1-18-0/searches/products," in request_url
        assert after == "page-two"
        return _payload(
            [_product("12345678", "Rechargeable Wireless Mouse")],
            after="",
        )


@pytest.mark.asyncio
async def test_cursor_page_coordinates_and_rank_use_only_included_natural_products() -> None:
    profile = VisionProfile(
        product_name="Rechargeable wireless mouse",
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
            KeywordCandidate(phrase="mouse for laptop", rationale="Adjacent need")
        ],
        exclusions=["keyboard"],
        confidence=0.9,
        title_suggestion="Wireless Mouse Rechargeable",
        title_reason="Image-only hypothesis",
    )
    candidate = SearchKeywordCandidate(
        phrase="wireless mouse",
        rationale="Takealot autocomplete",
        candidate_source="takealot_autocomplete",
        intended_strategy="core",
        seed="wireless",
        seed_source="image_shopper_root",
        autocomplete_rank=1,
    )

    observation = await _collect_keyword_observation(
        CursorPageWithSponsoredClient(),  # type: ignore[arg-type]
        candidate=candidate,
        candidate_order=1,
        target_plid="12345678",
        profile=profile,
        max_pages=2,
        relevance_threshold=0.60,
        page_delay_seconds=0,
        source_title="Rechargeable Wireless Mouse",
    )

    assert observation.relevance_status == "accepted"
    assert observation.page_number == 2
    assert observation.page_rank == 1
    assert observation.row_number == 1
    assert observation.column_number == 1
    assert observation.organic_rank == 36
    assert observation.validation_evidence["evaluated_first_page_results"] == 35
