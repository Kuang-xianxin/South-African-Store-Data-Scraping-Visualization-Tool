from copy import deepcopy
from datetime import UTC, date, datetime
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from starlette.requests import Request

from takealot_ops.search_ranking import title_optimization as subject


def _analysis() -> dict[str, Any]:
    return {
        "profile": {"product_type_terms": ["cat storage box"], "distinctive_terms": ["Foldable"]},
        "keywords": [{
            "keyword": "cat storage box", "organic_rank": 15,
            "observed_at": "2026-09-09T02:00:00+00:00",
            "validation_evidence": {"first_page_result_classifications": [
                {"plid": str(i), "title": f"Cat Storage Box Foldable {i} cm",
                 "classification": "direct_same_product", "organic_position": i}
                for i in range(1, 16)
            ]},
        }],
    }


def test_deduplicates_plids_and_preserves_real_query_title_time() -> None:
    analysis = _analysis()
    other_query = deepcopy(analysis["keywords"][0])
    other_query["keyword"] = "foldable cat box"
    other_query["validation_evidence"]["first_page_result_classifications"][0]["title"] = (
        "Cat Storage Box New Title"
    )
    analysis["keywords"].append(other_query)
    before = deepcopy(analysis)
    result = subject.build_title_benchmarks(
        analysis, target_plid="15", current_title="NEXOHOGAR Cat Storage Box Foldable",
    )
    assert analysis == before
    assert len(result["items"]) == 10
    assert len({row["plid"] for row in result["items"]}) == 10
    assert result["candidate_count"] == 14
    first = result["items"][0]
    assert first["title"] == "Cat Storage Box Foldable 1 cm"
    assert first["search_evidence"][1]["title"] == "Cat Storage Box New Title"
    assert first["search_evidence"][0]["captured_at"] == "2026-09-09T02:00:00+00:00"
    assert first["monitored_evidence"] is None
    assert "1 cm" in first["requires_confirmation"]
    assert "1 cm" not in first["borrowable_phrases"]
    assert "Foldable" in first["borrowable_phrases"]


def test_excludes_target_unrelated_conflicting_and_invalid_samples() -> None:
    analysis = _analysis()
    rows = analysis["keywords"][0]["validation_evidence"]["first_page_result_classifications"]
    rows[0]["classification"] = "unrelated"
    rows[1]["matched_exclusion_terms"] = ["box cover"]
    rows[2]["plid"] = "not-a-plid"
    rows[3]["is_target"] = True
    rows[4]["organic_position"] = False
    rows[5]["title"] = ""
    result = subject.build_title_benchmarks(analysis, target_plid="15", current_title="Cat Storage Box")
    assert {row["plid"] for row in result["items"]} == set(map(str, range(7, 15)))


def test_references_use_the_saved_full_url_instead_of_a_bare_plid() -> None:
    analysis = _analysis()
    row = analysis["keywords"][0]["validation_evidence"]["first_page_result_classifications"][0]
    row.update(plid="97221731", title="Cute Cozy Cat Beds for Indoor Cats - 2 in 1 Cat Cave Bed with Plush Ball")
    url = "https://www.takealot.com/cute-cozy-cat-beds-for-indoor-cats-2-in-1-cat-cave-bed-with-plus/PLID97221731"
    row["url"] = url
    before = deepcopy(analysis)
    result = subject.build_title_benchmarks(analysis, target_plid="15", current_title="Cat Box")
    assert next(item for item in result["items"] if item["plid"] == "97221731")["url"] == url
    assert analysis == before


def test_url_can_be_recovered_from_another_query_for_the_same_plid() -> None:
    analysis = _analysis()
    later = deepcopy(analysis["keywords"][0])
    later["keyword"] = "another cat query"
    row = later["validation_evidence"]["first_page_result_classifications"][0]
    row.update(classification="unrelated", url="/cat-storage-box/PLID1")
    later["validation_evidence"]["first_page_result_classifications"] = [row]
    analysis["keywords"].append(later)
    result = subject.build_title_benchmarks(analysis, target_plid="15", current_title="Cat Box")
    items = [item for item in result["items"] if item["plid"] == "1"]
    assert len(items) == 1
    assert items[0]["url"] == "https://www.takealot.com/cat-storage-box/PLID1"


@pytest.mark.parametrize("url", [
    None, "", "https://www.takealot.com/PLID1", "https://www.takealot.com/cat/PLID2",
    "https://example.com/cat/PLID1", "javascript:alert(1)",
    "https://www.takealot.com@evil.test/cat/PLID1", "//evil.test/cat/PLID1",
    "https://www.takealot.com/ca\nt/PLID1", "https://www.takealot.com/cat\\other/PLID1",
])
def test_missing_or_invalid_urls_do_not_become_fabricated_product_links(url: Any) -> None:
    analysis = _analysis()
    analysis["keywords"][0]["validation_evidence"]["first_page_result_classifications"][0]["url"] = url
    result = subject.build_title_benchmarks(analysis, target_plid="15", current_title="Cat Box")
    assert next(item for item in result["items"] if item["plid"] == "1")["url"] == ""


def test_url_repair_preserves_cached_title_reading_and_query_evidence() -> None:
    analysis, reading = _tree_reading()
    args = dict(target_plid="15", current_title="Cat Box")
    before = subject.build_title_benchmarks(analysis, **args)
    reviews = {before["review_fingerprint"]: {"status": "complete", "items": [reading]}}
    analysis["keywords"][0]["validation_evidence"]["first_page_result_classifications"][0]["url"] = (
        "https://www.takealot.com/cat-tree-house/PLID93262285"
    )
    result = subject.build_title_benchmarks(analysis, **args, reviews=reviews)
    assert result["review_fingerprint"] == before["review_fingerprint"]
    assert result["review_status"] == "complete"
    assert result["items"][0]["search_evidence"] == before["items"][0]["search_evidence"]
    assert result["items"][0]["url"].endswith("/cat-tree-house/PLID93262285")


def test_rank_changes_cannot_change_textual_assessment_or_invent_performance() -> None:
    analysis = _analysis()
    before = subject.build_title_benchmarks(analysis, target_plid="15", current_title="Cat Storage Box")
    analysis["keywords"][0]["organic_rank"] = 1
    after = subject.build_title_benchmarks(analysis, target_plid="15", current_title="Cat Storage Box")
    for old, new in zip(before["items"], after["items"], strict=True):
        assert old["title_assessment"] == new["title_assessment"]
        assert old["comparison_points"] == new["comparison_points"]
        assert new["monitored_evidence"] is None


def test_specifications_preserve_actual_dimensions_hyphens_and_units() -> None:
    assert subject._specifications('100-inch Screen 265x150cm 16:9 120" IP66') == [
        '100-inch', '265x150cm', '16:9', '120"', 'IP66',
    ]


@pytest.mark.parametrize("conflict_first", [True, False])
def test_plid_conflict_in_any_query_excludes_it_everywhere(conflict_first: bool) -> None:
    analysis = _analysis()
    conflict = deepcopy(analysis["keywords"][0])
    row = conflict["validation_evidence"]["first_page_result_classifications"][0]
    row.update(category_conflicts_target=True, classification="unrelated")
    analysis["keywords"].insert(0 if conflict_first else 1, conflict)
    result = subject.build_title_benchmarks(analysis, target_plid="15", current_title="Cat Box")
    assert "1" not in {item["plid"] for item in result["items"]}


def test_category_is_merged_from_another_query_even_when_not_selected_there() -> None:
    analysis = _analysis()
    extra = deepcopy(analysis["keywords"][0])
    row = extra["validation_evidence"]["first_page_result_classifications"][0]
    row.update(classification="unrelated", category_path=[{"id": "17", "name": "Pets"}])
    analysis["keywords"].append(extra)
    result = subject.build_title_benchmarks(analysis, target_plid="15", current_title="Cat Box")
    assert result["items"][0]["category_path"] == row["category_path"]


def _tree_reading() -> tuple[dict[str, Any], dict[str, Any]]:
    analysis = _analysis()
    title = "Cat Tree House Tower & Climbing Frame Sisal Post - 138cm Dark Grey"
    analysis["keywords"][0]["validation_evidence"]["first_page_result_classifications"] = [{
        "plid": "93262285", "title": title, "classification": "same_demand_competitor",
        "organic_position": 10,
    }]
    reading = {"plid": "93262285", "source_title": title,
               "comparability": "same_demand_competitor", "comparison_reason": "可比较猫活动空间的表达，结构不同。",
               "core_phrases": ["Cat Tree House Tower"],
               "detail_phrases": ["Climbing Frame", "Sisal Post", "Dark Grey"],
               "specifications": ["138cm"], "structure_note": "品名在前，随后列出结构、材料、高度和颜色。"}
    return analysis, reading


def test_semantic_reading_extracts_competitor_words_independently_without_transferring_facts() -> None:
    analysis, reading = _tree_reading()
    args = dict(target_plid="15", current_title="Cat Storage Box Foldable Blue")
    before = subject.build_title_benchmarks(analysis, **args)
    reviews = {before["review_fingerprint"]: {"status": "complete", "items": [reading]}}
    result = subject.build_title_benchmarks(analysis, **args, reviews=reviews)
    item = result["items"][0]
    assert item["title_analysis_status"] == "complete"
    assert item["core_phrases"] == ["Cat Tree House Tower"]
    assert "Sisal Post" in item["detail_phrases"]
    assert "Sisal Post" in item["requires_confirmation"]
    assert "138cm" in item["requires_confirmation"]
    assert item["borrowable_phrases"] == []
    changed = subject.build_title_benchmarks(analysis, target_plid="15", current_title="Different variant", reviews=reviews)
    assert changed["review_status"] == "pending"
    assert changed["items"][0]["title_analysis_status"] == "pending"
    reading["comparability"] = "not_comparable"
    assert subject.build_title_benchmarks(analysis, **args, reviews=reviews)["items"] == []


@pytest.mark.parametrize("mutation", ["invented", "wrong_title", "duplicate", "missing"])
def test_semantic_output_must_be_grounded_in_exact_titles(mutation: str) -> None:
    analysis, reading = _tree_reading()
    items = subject.build_title_benchmarks(analysis, target_plid="15", current_title="Cat Box")["items"]
    raw = {"items": [reading]}
    if mutation == "invented":
        reading["detail_phrases"].append("Waterproof")
    elif mutation == "wrong_title":
        reading["source_title"] = "Another product"
    elif mutation == "duplicate":
        raw["items"].append(deepcopy(reading))
    else:
        raw["items"] = []
    with pytest.raises(ValueError):
        subject.validate_title_readings(raw, items)


@pytest.mark.asyncio
async def test_review_saved_evidence_persists_usage_reuses_cache_and_rejects_changed_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from takealot_ops.storage.models import SearchRankingAnalysis
    from takealot_ops.search_ranking import service as service_module
    from takealot_ops.storage.migrations import create_schema
    from takealot_ops.storage.store_context import store_scope

    database_url = f"sqlite:///{tmp_path / 'review.db'}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    cli = tmp_path / "codex.exe"
    cli.touch()
    monkeypatch.setenv("TAKEALOT_SEARCH_CODEX_CLI_PATH", str(cli))
    engine = create_engine(database_url)
    create_schema(engine)
    analysis, reading = _tree_reading()
    title = ["Cat Storage Box Blue"]
    with store_scope("test"):
        with Session(engine) as session, session.begin():
            record = SearchRankingAnalysis(
                offer_id="own", productline_id="15", source_title=title[0], source_image_url="image",
                cache_key="a" * 64, provider="codex_cli", model="gpt-5.6-sol",
                prompt_version="test", status="completed", created_at=datetime.now(UTC),
                vision_payload={"usage": {"total_tokens": 10}},
            )
            session.add(record)
            session.flush()
            analysis_id = record.id
        service = service_module.SearchRankingService(tmp_path)
        def detail(_: str) -> dict[str, Any]:
            with Session(engine) as session:
                row = session.get(SearchRankingAnalysis, analysis_id)
                return {"product": {"title": title[0]}, "analysis": {"id": analysis_id,
                    "title_benchmarks": subject.build_title_benchmarks(
                        analysis, target_plid="15", current_title=title[0],
                        reviews=(row.vision_payload or {}).get("competitor_title_reviews"),
                    )}}
        monkeypatch.setattr(service, "detail_payload", detail)
        calls = []
        async def request(**kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"status": "complete", "items": [reading], "usage": {"total_tokens": 13}}
        monkeypatch.setattr(service_module, "request_title_readings", request)
        first = await service.review_title_benchmarks("own")
        assert first["analysis"]["title_benchmarks"]["review_status"] == "complete"
        await service.review_title_benchmarks("own")
        assert len(calls) == 1
        with Session(engine) as session:
            row = session.get(SearchRankingAnalysis, analysis_id)
            assert row.status == "completed"
            assert row.vision_payload["usage"]["total_tokens"] == 23
        title[0] = "Cat Storage Box Red"
        async def stale(**kwargs: Any) -> dict[str, Any]:
            title[0] = "Cat Storage Box Yellow"
            return await request(**kwargs)
        monkeypatch.setattr(service_module, "request_title_readings", stale)
        with pytest.raises(service_module.SearchRankingInputError, match="已更新"):
            await service.review_title_benchmarks("own")
        assert detail("own")["analysis"]["title_benchmarks"]["review_status"] == "pending"
        with Session(engine) as session:
            row = session.get(SearchRankingAnalysis, analysis_id)
            assert row.vision_payload["usage"]["total_tokens"] == 36
            assert row.status == "completed"
    engine.dispose()


def test_title_review_requires_model_run_permission() -> None:
    from takealot_ops.erp.web import _required_permission, SEARCH_RANKING_RUN
    assert _required_permission("/api/erp/search-ranking/own/title-review", "POST") == SEARCH_RANKING_RUN


def test_monitoring_enrichment_preserves_missing_zero_and_snapshot_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    benchmarks = subject.build_title_benchmarks(_analysis(), target_plid="15", current_title="Cat Storage Box")
    calls = []
    def load(engine: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(current=pd.DataFrame([
            {"来源": "competitor", "plid": "1", "商品": "Monitored title", "价格": 0.0,
             "采集时间": datetime(2026, 9, 8, tzinfo=UTC), "近期观察售出": {"30": 0},
             "近期观察售出截至": date(2026, 9, 8)},
            {"来源": "competitor", "plid": "2", "价格": float("nan"),
             "近期观察售出": {"30": None}},
        ]))
    monkeypatch.setattr(subject, "load_competitor_dataset", load)
    subject.enrich_monitored_benchmarks(None, benchmarks)  # type: ignore[arg-type]
    assert calls[0]["plids"] == set(map(str, range(1, 11)))
    assert calls[0]["include_store_projection"] is False
    assert calls[0]["own_store_codes"] == set()
    first, second, third = benchmarks["items"][:3]
    assert first["monitored_evidence"]["observed_sales_30"] == 0
    assert first["monitored_evidence"]["price"] == 0
    assert first["monitored_evidence"]["observed_sales_through"] == "2026-09-08"
    assert second["monitored_evidence"]["price"] is None
    assert second["monitored_evidence"]["observed_sales_30"] is None
    assert third["monitoring_status"] == "not_monitored"
    assert third["title"] and third["search_evidence"]


def test_detail_endpoint_gates_monitoring_without_hiding_public_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from takealot_ops.erp import web
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", f"sqlite:///{tmp_path / 'titles.db'}")
    app = web.create_app(tmp_path)
    benchmarks = subject.build_title_benchmarks(
        _analysis(), target_plid="15", current_title="Cat Storage Box",
    )
    monkeypatch.setattr(app.state.search_ranking_service, "detail_payload", lambda _: {
        "analysis": {"title_benchmarks": deepcopy(benchmarks)},
    })
    monkeypatch.setattr(web, "_decorate_search_ranking_detail", lambda root, payload, store: payload)
    calls = []
    monkeypatch.setattr(web, "enrich_monitored_benchmarks", lambda engine, refs: calls.append(refs))
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == (
        "/api/erp/search-ranking/{offer_id}"
    ))
    def request(allowed: bool) -> Request:
        return Request({"type": "http", "app": app, "state": {
            "erp_user": SimpleNamespace(can=lambda permission: allowed),
            "erp_store": SimpleNamespace(code="current"),
        }})
    denied = endpoint("own-offer", request(False))
    assert not calls
    assert len(denied["analysis"]["title_benchmarks"]["items"]) == 10
    assert all(row["monitoring_status"] == "no_access" for row in (
        denied["analysis"]["title_benchmarks"]["items"]
    ))
    endpoint("own-offer", request(True))
    assert len(calls) == 1
    def fail(engine: Any, refs: Any) -> None:
        raise ValueError("read unavailable")
    monkeypatch.setattr(web, "enrich_monitored_benchmarks", fail)
    failed = endpoint("own-offer", request(True))
    assert all(row["monitoring_status"] == "unavailable" for row in (
        failed["analysis"]["title_benchmarks"]["items"]
    ))
    app.state.read_engine.dispose()
