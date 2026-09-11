from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from takealot_ops.search_ranking import service as service_module
from takealot_ops.search_ranking.reference_categories import enrich_reference_categories
from takealot_ops.search_ranking.title_optimization import build_title_benchmarks
from takealot_ops.storage.models import CompetitorSnapshot, SearchRankingAnalysis
from takealot_ops.storage.store_context import store_scope


def evidence() -> dict[str, Any]:
    return {"keywords": [{
        "keyword": "cat house", "found": True, "pages_scanned": 3,
        "organic_rank": 38, "page_number": 3, "page_rank": 4,
        "observed_at": "2026-09-09T06:22:38Z", "validation_evidence": {
            "first_page_result_classifications": [{
                "plid": "97221731", "title": "Cat Cave Bed",
                "classification": "same_demand_competitor", "organic_position": 27,
                "url": "https://www.takealot.com/cat-cave-bed/PLID97221731",
            }],
        },
    }]}


def build(raw: dict[str, Any], saved: dict[str, Any] | None = None) -> dict[str, Any]:
    return build_title_benchmarks(raw, target_plid="15", current_title="Cat House", supplemental_categories=saved)


def test_categories_do_not_rewrite_model_input_and_ranks_preserve_real_page_slots() -> None:
    raw = evidence()
    before = deepcopy(raw)
    first = build(raw)
    result = build(raw, {"97221731": {
        "status": "available", "path": [{"name": "Pets"}, {"name": "Beds"}],
        "source": "public_product", "captured_at": "2026-09-11T10:00:00Z",
    }})
    assert first["review_fingerprint"] == result["review_fingerprint"]
    item = result["items"][0]
    assert item["category_path"] == []
    assert len(item["category_observation"]["path"]) == 2
    row = item["search_evidence"][0]
    assert (row["page_number"], row["page_rank"]) == (1, 27)
    assert (row["target_page_number"], row["target_page_rank"]) == (3, 4)
    assert row["target_organic_position"] == 38
    assert raw == before


def test_local_category_reads_use_latest_nonempty_snapshot_for_exact_plid() -> None:
    engine = create_engine("sqlite://")
    CompetitorSnapshot.__table__.create(engine)
    common = dict(title="Cat bed", url="https://www.takealot.com/cat-cave-bed/PLID97221731",
                  stock_method="unknown", review_count=0, fetched_review_count=0,
                  positive_reviews=0, neutral_reviews=0, negative_reviews=0,
                  lifetime_sales_min=0, lifetime_sales_max=0, trend_label="unknown", trend_note="")
    with Session(engine) as session, session.begin():
        for day, path in [(8, [{"name": "Old"}]), (9, [{"name": "Pets"}, {"name": "Beds"}]), (10, [])]:
            session.add(CompetitorSnapshot(plid="97221731", collected_at=datetime(2026, 9, day), category_path=path, **common))
        session.add(CompetitorSnapshot(plid="999", collected_at=datetime(2026, 9, 11), category_path=[{"name": "Unrelated"}], **common))
    refs = build(evidence())
    fingerprint = refs["review_fingerprint"]
    enrich_reference_categories(engine, refs)
    observation = refs["items"][0]["category_observation"]
    assert observation["path"] == [{"name": "Pets"}, {"name": "Beds"}]
    assert observation["captured_at"].startswith("2026-09-09")
    assert observation["source"] == "monitored_snapshot"
    assert refs["review_fingerprint"] == fingerprint
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["available", "failed", "missing_url", "stale", "concurrent_success"])
async def test_explicit_category_supplement_is_bounded_independent_and_preserves_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    url = f"sqlite:///{tmp_path / 'categories.db'}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", url)
    engine = create_engine(url)
    SearchRankingAnalysis.__table__.create(engine)
    raw = evidence()
    if mode == "missing_url":
        raw["keywords"][0]["validation_evidence"]["first_page_result_classifications"][0]["url"] = "https://evil.example/cat/PLID97221731"
    original_vision = {"usage": {"total_tokens": 123}, "competitor_title_reviews": {"original": {"status": "complete"}}, "other_fact": "keep"}
    calls: list[str] = []
    generation = [1]

    with store_scope("test"):
        with Session(engine) as session, session.begin():
            record = SearchRankingAnalysis(offer_id="own", productline_id="15", source_title="Cat House",
                source_image_url="image", cache_key="a" * 64, provider="test", model="test",
                prompt_version="test", status="completed", created_at=datetime.now(UTC), vision_payload=original_vision)
            session.add(record)
            session.flush()
            analysis_id = record.id

        def detail(_: str) -> dict[str, Any]:
            with Session(engine) as session:
                row = session.get(SearchRankingAnalysis, analysis_id)
                return {"product": {"title": "Cat House"}, "analysis": {"id": analysis_id * generation[0],
                    "title_benchmarks": build(raw, row.vision_payload.get("competitor_category_observations"))}}

        class Client:
            async def __aenter__(self) -> "Client":
                return self

            async def __aexit__(self, *args: Any) -> None:
                pass

            async def fetch_product_category_path(self, product_url: str) -> Any:
                calls.append(product_url)
                if mode == "stale":
                    generation[0] += 1
                if mode == "concurrent_success":
                    with Session(engine) as session, session.begin():
                        row = session.get(SearchRankingAnalysis, analysis_id)
                        row.vision_payload = {**row.vision_payload, "competitor_category_observations": {
                            "97221731": {"status": "available", "source": "public_product", "path": [{"name": "Winner"}]}}}
                if mode in {"failed", "concurrent_success"}:
                    raise RuntimeError("upstream unavailable")
                return [{"name": "Pets"}, {"name": "Beds"}]

        service = service_module.SearchRankingService(tmp_path, search_client_factory=Client)
        monkeypatch.setattr(service, "detail_payload", detail)
        def forbidden_model(**kwargs: Any) -> Any:
            raise AssertionError("category supplementation must not invoke the title model")
        monkeypatch.setattr(service_module, "request_title_readings", forbidden_model)
        if mode == "stale":
            with pytest.raises(service_module.SearchRankingInputError, match="已更新"):
                await service.supplement_reference_categories("own")
        else:
            await service.supplement_reference_categories("own")
        assert len(calls) == (0 if mode == "missing_url" else 1)
        with Session(engine) as session:
            row = session.get(SearchRankingAnalysis, analysis_id)
            for key, value in original_vision.items():
                assert row.vision_payload[key] == value
            saved = row.vision_payload.get("competitor_category_observations", {}).get("97221731")
            if mode == "stale":
                assert saved is None
            elif mode == "concurrent_success":
                assert saved["path"] == [{"name": "Winner"}]
            elif mode in {"failed", "missing_url"}:
                assert saved["status"] == ("request_failed" if mode == "failed" else "missing_url")
                assert saved["path"] == []
            else:
                assert [part["name"] for part in saved["path"]] == ["Pets", "Beds"]
        if mode == "available":
            await service.supplement_reference_categories("own")
            assert len(calls) == 1
    engine.dispose()


def test_category_write_uses_existing_operation_permission() -> None:
    from takealot_ops.erp.web import _required_permission, SEARCH_RANKING_RUN
    assert _required_permission("/api/erp/search-ranking/own/reference-categories", "POST") == SEARCH_RANKING_RUN
