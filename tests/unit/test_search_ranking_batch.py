from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from takealot_ops.search_ranking.batch import (
    SearchRankingBatchController,
    SearchRankingBatchInputError,
)
from takealot_ops.search_ranking.service import (
    PROMPT_VERSION,
    SearchRankingProviderError,
    SearchRankingService,
    _analysis_cache_key,
    _variant_family_cache_material,
    _variant_family_profile,
)
from takealot_ops.storage.migrations import (
    create_engine_for_database_url,
    create_schema,
)
from takealot_ops.storage.models import OfferCurrent, SearchRankingAnalysis
from takealot_ops.storage.store_context import current_store_code, store_scope


def _stores() -> list[dict[str, Any]]:
    return [
        {
            "code": "current",
            "display_name": "Store One",
            "active": True,
            "data_connected": True,
        },
        {
            "code": "store-02",
            "display_name": "Store Two",
            "active": True,
            "data_connected": True,
        },
    ]


def _target(index: int, store_code: str) -> dict[str, Any]:
    return {
        "store_code": store_code,
        "store_name": "Store One" if store_code == "current" else "Store Two",
        "offer_id": f"offer-{index}",
        "productline_id": str(10_000_000 + index),
        "sku": f"SKU-{index}",
        "title": f"Product {index}",
        "image_url": f"https://media.takealot.com/covers_images/test/{index}.file",
        "captured_at": datetime.now(UTC).isoformat(),
        "preview_cache_state": "fresh_model",
    }


def _preview(targets: list[dict[str, Any]], snapshot_id: str = "a" * 64) -> dict[str, Any]:
    store_rows = []
    for store in _stores():
        count = sum(1 for target in targets if target["store_code"] == store["code"])
        if count:
            store_rows.append(
                {
                    "code": store["code"],
                    "display_name": store["display_name"],
                    "current_offer_count": count,
                    "eligible_count": count,
                    "existing_vision_cache_hit_count": 0,
                    "same_batch_vision_reuse_count": 0,
                    "fresh_vision_count": count,
                }
            )
    return {
        "snapshot_id": snapshot_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "store_count": len(store_rows),
        "stores": store_rows,
        "current_offer_count": len(targets),
        "eligible_count": len(targets),
        "existing_vision_cache_hit_count": 0,
        "same_batch_vision_reuse_count": 0,
        "fresh_vision_count": len(targets),
        "maximum_fresh_vision_count": len(targets),
        "estimated_usage": {},
        "estimated_cost": {},
        "estimated_duration": {},
        "_targets": targets,
    }


class _FakeRuntime:
    def __init__(self) -> None:
        self.primary_provider = SimpleNamespace(
            name="doubao",
            model="doubao-test",
            input_price_cny_per_million=0.6,
            output_price_cny_per_million=3.6,
        )
        self.fallback_provider = None
        self.configured_providers = (self.primary_provider,)
        self.provider_signature = "doubao:doubao-test"
        self.page_delay_seconds = 3.0
        self.page_delay_jitter_seconds = 2.0


class _FakeService:
    def __init__(self) -> None:
        self.runtime = _FakeRuntime()
        self.database_url = "sqlite:///:memory:"
        self.calls: list[tuple[str, str]] = []
        self.concurrent = 0
        self.max_concurrent = 0

    async def analyze_offer(self, offer_id: str) -> dict[str, Any]:
        self.calls.append((current_store_code(), offer_id))
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        await asyncio.sleep(0.01)
        self.concurrent -= 1
        index = len(self.calls)
        return {
            "analysis": {
                "id": index,
                "vision_reused": False,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                },
                "estimated_cost_cny": 0.001,
            }
        }

    def detail_payload(self, offer_id: str) -> dict[str, Any] | None:
        del offer_id
        return None


def test_preview_counts_existing_and_same_batch_vision_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'preview.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("ARK_API_KEY", "test-only-key")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    captured_at = datetime.now(UTC)
    shared_image = "http://media.takealot.com/covers_images/test/shared.file"
    with store_scope("current"):
        with Session(engine) as session, session.begin():
            session.add_all(
                [
                    OfferCurrent(
                        offer_id="offer-1",
                        productline_id="10000001",
                        sku="SKU-1",
                        title="Product One",
                        image_url=shared_image,
                        status="buyable",
                        takealot_available_stock=1,
                        seller_available_stock=0,
                        captured_at=captured_at,
                    ),
                    OfferCurrent(
                        offer_id="offer-2",
                        productline_id="10000002",
                        sku="SKU-2",
                        title="Product Two",
                        image_url=shared_image,
                        status="buyable",
                        takealot_available_stock=1,
                        seller_available_stock=0,
                        captured_at=captured_at,
                    ),
                    OfferCurrent(
                        offer_id="offer-3",
                        productline_id="10000003",
                        sku="SKU-3",
                        title="Product Three",
                        image_url="http://media.takealot.com/covers_images/test/cached.file",
                        status="buyable",
                        takealot_available_stock=1,
                        seller_available_stock=0,
                        captured_at=captured_at,
                    ),
                ]
            )
    with store_scope("store-02"):
        with Session(engine) as session, session.begin():
            session.add(
                OfferCurrent(
                    offer_id="offer-4",
                    productline_id="10000004",
                    sku="SKU-4",
                    title="Product Four",
                    image_url=shared_image,
                    status="buyable",
                    takealot_available_stock=1,
                    seller_available_stock=0,
                    captured_at=captured_at,
                )
            )
    engine.dispose()

    service = SearchRankingService(tmp_path)
    cached_image = "https://media.takealot.com/covers_images/test/cached.file"
    cache_key = _analysis_cache_key(
        image_url=cached_image,
        provider_signature=service.runtime.provider_signature,
        source_title=_variant_family_cache_material(
            _variant_family_profile(
                [
                    {
                        "offer_id": "offer-3",
                        "productline_id": "10000003",
                        "sku": "SKU-3",
                        "title": "Product Three",
                        "image_url": cached_image,
                        "available_stock": 1,
                    }
                ]
            )
        ),
    )
    engine = create_engine_for_database_url(database_url)
    with store_scope("current"):
        with Session(engine) as session, session.begin():
            session.add(
                SearchRankingAnalysis(
                    offer_id="historical",
                    productline_id="99999999",
                    sku="OLD",
                    source_title="Product Three",
                    source_image_url=cached_image,
                    cache_key=cache_key,
                    provider="doubao",
                    model=service.runtime.primary_provider.model,
                    prompt_version=PROMPT_VERSION,
                    status="completed",
                    product_name="Historical product",
                    category="Test",
                    confidence=0.9,
                    vision_payload={
                        "vision_stage_completed": True,
                        "usage": {
                            "input_tokens": 2_800,
                            "output_tokens": 600,
                            "total_tokens": 3_400,
                        },
                        "estimated_cost_cny": 0.00384,
                    },
                    vision_reused=False,
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                    completed_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
    engine.dispose()

    controller = SearchRankingBatchController(
        tmp_path,
        service=service,
        analysis_lock=asyncio.Lock(),
        state_path=tmp_path / "batch.json",
    )
    payload = controller.preview_payload(
        _stores(),
        actor_username="tester",
        actor_is_admin=True,
    )

    assert payload["policy"]["public_request_min_interval_seconds"] == 3.0
    assert payload["policy"]["public_request_max_interval_seconds"] == 5.0
    assert payload["policy"]["max_concurrency"] == 1
    assert payload["policy"]["automatic_retry"] is False
    assert (
        payload["policy"]["target_scope"]
        == "one_representative_offer_per_store_productline_id"
    )
    assert payload["preview"]["store_count"] == 2
    assert payload["preview"]["eligible_count"] == 4
    assert payload["preview"]["existing_vision_cache_hit_count"] == 1
    assert payload["preview"]["same_batch_vision_reuse_count"] == 0
    assert payload["preview"]["fresh_vision_count"] == 3
    assert payload["preview"]["maximum_fresh_vision_count"] == 3


def test_preview_groups_same_store_variants_but_keeps_other_store_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'variant-preview.db').as_posix()}"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    monkeypatch.setenv("ARK_API_KEY", "test-only-key")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    engine = create_engine_for_database_url(database_url)
    create_schema(engine)
    captured_at = datetime.now(UTC)
    with store_scope("current"):
        with Session(engine) as session, session.begin():
            session.add_all(
                [
                    OfferCurrent(
                        offer_id="variant-double",
                        productline_id="102695333",
                        sku="DOUBLE",
                        title="Memory Foam Double",
                        image_url="http://media.takealot.com/covers_images/test/double.file",
                        status="buyable",
                        takealot_available_stock=2,
                        seller_available_stock=0,
                        captured_at=captured_at,
                    ),
                    OfferCurrent(
                        offer_id="variant-king",
                        productline_id="102695333",
                        sku="KING",
                        title="Memory Foam King",
                        image_url="http://media.takealot.com/covers_images/test/king.file",
                        status="buyable",
                        takealot_available_stock=3,
                        seller_available_stock=0,
                        captured_at=captured_at,
                    ),
                ]
            )
    with store_scope("store-02"):
        with Session(engine) as session, session.begin():
            session.add(
                OfferCurrent(
                    offer_id="other-store-king",
                    productline_id="102695333",
                    sku="KING-OTHER",
                    title="Memory Foam King",
                    image_url="http://media.takealot.com/covers_images/test/king.file",
                    status="buyable",
                    takealot_available_stock=4,
                    seller_available_stock=0,
                    captured_at=captured_at,
                )
            )
    engine.dispose()

    service = SearchRankingService(tmp_path)
    controller = SearchRankingBatchController(
        tmp_path,
        service=service,
        analysis_lock=asyncio.Lock(),
        state_path=tmp_path / "batch.json",
    )

    preview = controller._build_preview(_stores())

    assert preview["current_offer_count"] == 3
    assert preview["eligible_offer_count"] == 3
    assert preview["eligible_count"] == 2
    assert preview["variant_family_count"] == 1
    assert len(preview["_targets"]) == 2
    current_target = next(
        item for item in preview["_targets"] if item["store_code"] == "current"
    )
    assert current_target["variant_count"] == 2
    assert set(current_target["variant_offer_ids"]) == {
        "variant-double",
        "variant-king",
    }
    assert current_target["shared_family_title"] == "Memory Foam"
    assert {
        item["offer_id"]: [parameter["value"] for parameter in item["parameters"]]
        for item in current_target["variant_parameters"]
    } == {
        "variant-double": ["Double"],
        "variant-king": ["King"],
    }


@pytest.mark.asyncio
async def test_batch_runs_every_target_strictly_serial_and_checkpoints(
    tmp_path: Path,
) -> None:
    service = _FakeService()
    controller = SearchRankingBatchController(
        tmp_path,
        service=service,  # type: ignore[arg-type]
        analysis_lock=asyncio.Lock(),
        state_path=tmp_path / "batch.json",
    )
    targets = [_target(1, "current"), _target(2, "store-02"), _target(3, "current")]
    controller._build_preview = lambda stores: _preview(targets)  # type: ignore[method-assign]

    controller.start(
        _stores(),
        actor_username="tester",
        actor_display_name="Tester",
        actor_is_admin=False,
        snapshot_id="a" * 64,
    )
    task = controller._task
    assert task is not None
    await task

    status = controller.status_payload(
        _stores(),
        actor_username="tester",
        actor_is_admin=False,
    )
    assert status is not None
    assert status["status"] == "completed"
    assert status["completed_count"] == 3
    assert status["processed_count"] == 3
    assert status["usage"]["total_tokens"] == 450
    assert status["usage"]["estimated_cost_cny"] == 0.003
    assert service.calls == [
        ("current", "offer-1"),
        ("store-02", "offer-2"),
        ("current", "offer-3"),
    ]
    assert service.max_concurrent == 1
    assert (tmp_path / "batch.json").exists()


@pytest.mark.asyncio
async def test_batch_does_not_invoke_retained_codex_quota_hooks(tmp_path: Path) -> None:
    class ApiServiceWithForbiddenCodexHooks(_FakeService):
        async def prepare_model_quota(self) -> dict[str, Any]:
            raise AssertionError("retained Codex quota preflight must be unreachable")

        def model_quota_status(self) -> dict[str, Any]:
            raise AssertionError("retained Codex quota status must be unreachable")

    service = ApiServiceWithForbiddenCodexHooks()
    controller = SearchRankingBatchController(
        tmp_path,
        service=service,  # type: ignore[arg-type]
        analysis_lock=asyncio.Lock(),
        state_path=tmp_path / "batch.json",
    )
    controller._build_preview = lambda stores: _preview(  # type: ignore[method-assign]
        [_target(1, "current")]
    )

    controller.start(
        _stores(),
        actor_username="tester",
        actor_display_name="Tester",
        actor_is_admin=False,
        snapshot_id="a" * 64,
    )
    task = controller._task
    assert task is not None
    await task

    status = controller.status_payload(
        _stores(), actor_username="tester", actor_is_admin=False
    )
    assert status is not None
    assert status["status"] == "completed"
    assert status["next_index"] == 1
    assert status["processed_count"] == 1
    assert "weekly_quota" not in status
    assert service.calls == [("current", "offer-1")]


@pytest.mark.asyncio
async def test_manual_fact_gap_is_skipped_without_pausing_later_targets(
    tmp_path: Path,
) -> None:
    class ManualGapService(_FakeService):
        async def analyze_offer(self, offer_id: str) -> dict[str, Any]:
            detail = await super().analyze_offer(offer_id)
            if offer_id == "offer-1":
                detail["analysis"]["recognition"] = {
                    "manual_fact_required": True,
                    "manual_fact_reason": "Confirm battery type",
                }
            return detail

    service = ManualGapService()
    controller = SearchRankingBatchController(
        tmp_path,
        service=service,  # type: ignore[arg-type]
        analysis_lock=asyncio.Lock(),
        state_path=tmp_path / "batch.json",
    )
    targets = [_target(1, "current"), _target(2, "store-02")]
    controller._build_preview = lambda stores: _preview(targets)  # type: ignore[method-assign]
    controller.start(
        _stores(),
        actor_username="tester",
        actor_display_name="Tester",
        actor_is_admin=False,
        snapshot_id="a" * 64,
    )
    task = controller._task
    assert task is not None
    await task

    status = controller.status_payload(
        _stores(), actor_username="tester", actor_is_admin=False
    )
    assert status is not None
    assert status["status"] == "completed"
    assert status["skipped_count"] == 1
    assert status["completed_count"] == 1
    assert status["failed_count"] == 0
    assert status["recent_results"][0]["message"] == "Confirm battery type"
    assert service.calls == [("current", "offer-1"), ("store-02", "offer-2")]


@pytest.mark.asyncio
async def test_completed_batch_can_restart_from_index_zero_with_new_batch_id(
    tmp_path: Path,
) -> None:
    service = _FakeService()
    controller = SearchRankingBatchController(
        tmp_path,
        service=service,  # type: ignore[arg-type]
        analysis_lock=asyncio.Lock(),
        state_path=tmp_path / "batch.json",
    )
    targets = [_target(1, "current"), _target(2, "store-02")]
    controller._build_preview = lambda stores: _preview(targets)  # type: ignore[method-assign]
    first = controller.start(
        _stores(),
        actor_username="tester",
        actor_display_name="Tester",
        actor_is_admin=False,
        snapshot_id="a" * 64,
    )
    first_task = controller._task
    assert first_task is not None
    await first_task
    completed = controller.status_payload(
        _stores(), actor_username="tester", actor_is_admin=False
    )
    assert completed is not None and completed["can_restart"] is True

    restarted = controller.restart(
        _stores(),
        actor_username="tester",
        actor_display_name="Tester",
        actor_is_admin=False,
        snapshot_id="a" * 64,
    )
    assert restarted["batch_id"] != first["batch_id"]
    assert restarted["next_index"] == 0
    restarted_task = controller._task
    assert restarted_task is not None
    await restarted_task
    assert service.calls == [
        ("current", "offer-1"),
        ("store-02", "offer-2"),
        ("current", "offer-1"),
        ("store-02", "offer-2"),
    ]


@pytest.mark.asyncio
async def test_provider_error_pauses_without_retry_and_resume_skips_failed_target(
    tmp_path: Path,
) -> None:
    class FailingOnceService(_FakeService):
        async def analyze_offer(self, offer_id: str) -> dict[str, Any]:
            if offer_id == "offer-1":
                self.calls.append((current_store_code(), offer_id))
                raise SearchRankingProviderError("provider unavailable")
            return await super().analyze_offer(offer_id)

        def detail_payload(self, offer_id: str) -> dict[str, Any] | None:
            if offer_id != "offer-1":
                return None
            return {
                "latest_attempt": {
                    "id": 91,
                    "vision_reused": False,
                    "usage": {
                        "input_tokens": 80,
                        "output_tokens": 20,
                        "total_tokens": 100,
                    },
                    "estimated_cost_cny": 0.0005,
                }
            }

    service = FailingOnceService()
    controller = SearchRankingBatchController(
        tmp_path,
        service=service,  # type: ignore[arg-type]
        analysis_lock=asyncio.Lock(),
        state_path=tmp_path / "batch.json",
    )
    targets = [_target(1, "current"), _target(2, "store-02")]
    controller._build_preview = lambda stores: _preview(targets)  # type: ignore[method-assign]
    controller.start(
        _stores(),
        actor_username="tester",
        actor_display_name="Tester",
        actor_is_admin=False,
        snapshot_id="a" * 64,
    )
    first_task = controller._task
    assert first_task is not None
    await first_task

    paused = controller.status_payload(
        _stores(), actor_username="tester", actor_is_admin=False
    )
    assert paused is not None
    assert paused["status"] == "paused_after_error"
    assert paused["failed_count"] == 1
    assert paused["next_index"] == 1
    assert paused["can_resume"] is True
    assert paused["can_retry_failed_target"] is True
    assert paused["retry_failed_target"]["offer_id"] == "offer-1"
    assert paused["retry_remaining_count"] == 2
    assert service.calls == [("current", "offer-1")]

    controller.stop(actor_username="tester", actor_is_admin=False)
    stopped = controller.status_payload(
        _stores(), actor_username="tester", actor_is_admin=False
    )
    assert stopped is not None
    assert stopped["status"] == "stopped"
    assert stopped["can_resume"] is True
    assert stopped["can_retry_failed_target"] is True
    assert stopped["can_stop"] is False

    controller.resume(actor_username="tester", actor_is_admin=False)
    resumed_task = controller._task
    assert resumed_task is not None
    await resumed_task
    completed = controller.status_payload(
        _stores(), actor_username="tester", actor_is_admin=False
    )
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["failed_count"] == 1
    assert completed["completed_count"] == 1
    assert service.calls.count(("current", "offer-1")) == 1
    assert service.calls.count(("store-02", "offer-2")) == 1


@pytest.mark.asyncio
async def test_explicit_failed_target_retry_rewinds_only_that_target_and_keeps_spend(
    tmp_path: Path,
) -> None:
    class FailingFirstAttemptService(_FakeService):
        def __init__(self) -> None:
            super().__init__()
            self.offer_one_attempts = 0

        async def analyze_offer(self, offer_id: str) -> dict[str, Any]:
            if offer_id == "offer-1":
                self.offer_one_attempts += 1
                if self.offer_one_attempts == 1:
                    self.calls.append((current_store_code(), offer_id))
                    raise SearchRankingProviderError("provider output invalid")
            return await super().analyze_offer(offer_id)

        def detail_payload(self, offer_id: str) -> dict[str, Any] | None:
            if offer_id != "offer-1" or self.offer_one_attempts != 1:
                return None
            return {
                "latest_attempt": {
                    "id": 92,
                    "vision_reused": False,
                    "usage": {
                        "input_tokens": 80,
                        "output_tokens": 20,
                        "total_tokens": 100,
                    },
                    "estimated_cost_cny": 0.0,
                }
            }

    service = FailingFirstAttemptService()
    controller = SearchRankingBatchController(
        tmp_path,
        service=service,  # type: ignore[arg-type]
        analysis_lock=asyncio.Lock(),
        state_path=tmp_path / "batch.json",
    )
    targets = [_target(1, "current"), _target(2, "store-02")]
    controller._build_preview = lambda stores: _preview(targets)  # type: ignore[method-assign]
    controller.start(
        _stores(),
        actor_username="tester",
        actor_display_name="Tester",
        actor_is_admin=False,
        snapshot_id="a" * 64,
    )
    first_task = controller._task
    assert first_task is not None
    await first_task

    paused = controller.status_payload(
        _stores(), actor_username="tester", actor_is_admin=False
    )
    assert paused is not None
    assert paused["status"] == "paused_after_error"
    assert paused["usage"]["total_tokens"] == 100

    controller.retry_failed(actor_username="tester", actor_is_admin=False)
    retry_task = controller._task
    assert retry_task is not None
    await retry_task

    completed = controller.status_payload(
        _stores(), actor_username="tester", actor_is_admin=False
    )
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["completed_count"] == 2
    assert completed["failed_count"] == 0
    assert completed["processed_count"] == 2
    assert completed["usage"]["total_tokens"] == 400
    assert service.calls == [
        ("current", "offer-1"),
        ("current", "offer-1"),
        ("store-02", "offer-2"),
    ]
    assert len(controller._state["retry_history"]) == 1
    assert controller._state["retry_history"][0]["offer_id"] == "offer-1"


def test_v1_checkpoint_compacts_only_unfinished_variant_targets(
    tmp_path: Path,
) -> None:
    service = _FakeService()
    checkpoint = tmp_path / "batch.json"
    processed = _target(1, "current")
    processed["productline_id"] = "family-1"
    processed_variant = _target(2, "current")
    processed_variant["productline_id"] = "family-1"
    pending = _target(3, "current")
    pending["productline_id"] = "family-2"
    pending_variant = _target(4, "current")
    pending_variant["productline_id"] = "family-2"
    checkpoint.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "batch_id": "legacy-batch",
                "owner_username": "tester",
                "owner_display_name": "Tester",
                "status": "stopped",
                "stores": [{"code": "current", "display_name": "Store One"}],
                "targets": [processed, processed_variant, pending, pending_variant],
                "next_index": 1,
                "completed_count": 1,
                "skipped_count": 0,
                "failed_count": 0,
                "current_target": None,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                    "estimated_cost_cny": 0.001,
                    "cost_accounting_complete": True,
                },
                "store_progress": {
                    "current": {
                        "code": "current",
                        "display_name": "Store One",
                        "target_count": 4,
                        "completed_count": 1,
                        "skipped_count": 0,
                        "failed_count": 0,
                    }
                },
                "results": [],
                "last_error": None,
                "pause_requested": False,
                "stop_requested": False,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    controller = SearchRankingBatchController(
        tmp_path,
        service=service,  # type: ignore[arg-type]
        analysis_lock=asyncio.Lock(),
        state_path=checkpoint,
    )
    status = controller.status_payload(
        _stores(), actor_username="tester", actor_is_admin=False
    )

    assert status is not None
    assert status["target_count"] == 2
    assert status["next_index"] == 1
    assert status["remaining_count"] == 1
    assert status["deduplicated_pending_variant_count"] == 2
    assert status["can_resume"] is True
    persisted = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 4
    assert [item["offer_id"] for item in persisted["targets"]] == [
        "offer-1",
        "offer-3",
    ]
    assert persisted["targets"][1]["variant_count"] == 2
    assert persisted["targets"][1]["shared_family_title"] == "Product"
    assert {
        item["offer_id"]: [parameter["value"] for parameter in item["parameters"]]
        for item in persisted["targets"][1]["variant_parameters"]
    } == {"offer-3": ["3"], "offer-4": ["4"]}


def test_v2_checkpoint_backfills_variant_parameters_without_resetting_progress(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "batch-v2.json"
    target = _target(1, "current")
    target.update(
        {
            "productline_id": "102695333",
            "variant_count": 3,
            "variant_offer_ids": ["double", "king", "king-xl"],
            "variant_titles": [
                "2 Inch 7 Zone Memory Foam Double",
                "2 Inch 7 Zone Memory Foam King",
                "2 Inch 7 Zone Memory Foam King XL",
            ],
        }
    )
    checkpoint.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "batch_id": "v2-batch",
                "owner_username": "tester",
                "status": "stopped",
                "stores": [{"code": "current", "display_name": "Store One"}],
                "targets": [target],
                "next_index": 0,
                "completed_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
                "usage": {},
                "store_progress": {},
                "results": [],
                "pause_requested": False,
                "stop_requested": False,
            }
        ),
        encoding="utf-8",
    )

    SearchRankingBatchController(
        tmp_path,
        service=_FakeService(),  # type: ignore[arg-type]
        analysis_lock=asyncio.Lock(),
        state_path=checkpoint,
    )

    persisted = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 4
    assert persisted["next_index"] == 0
    assert persisted["targets"][0]["shared_family_title"] == (
        "2 Inch 7 Zone Memory Foam"
    )
    assert {
        item["offer_id"]: [parameter["value"] for parameter in item["parameters"]]
        for item in persisted["targets"][0]["variant_parameters"]
    } == {
        "double": ["Double"],
        "king": ["King"],
        "king-xl": ["King XL"],
    }


def test_v3_checkpoint_repairs_missing_variant_metadata(tmp_path: Path) -> None:
    checkpoint = tmp_path / "batch-v3.json"
    target = _target(1, "current")
    target.update(
        {
            "productline_id": "102719093",
            "variant_count": 4,
            "variant_offer_ids": ["grey", "purple", "black", "pink"],
            "variant_titles": [
                "2-Zone Dual Zipper Far Infrared Sauna Blanket - Grey",
                "2-Zone Dual Zipper Far Infrared Sauna Blanket - Purple",
                "2-Zone Dual Zipper Far Infrared Sauna Blanket - Black",
                "2-Zone Dual Zipper Far Infrared Sauna Blanket - Pink",
            ],
        }
    )
    checkpoint.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "batch_id": "v3-batch",
                "owner_username": "tester",
                "status": "stopped",
                "stores": [{"code": "current", "display_name": "Store One"}],
                "targets": [target],
                "next_index": 0,
                "completed_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
                "usage": {},
                "store_progress": {},
                "results": [],
                "pause_requested": False,
                "stop_requested": False,
            }
        ),
        encoding="utf-8",
    )

    SearchRankingBatchController(
        tmp_path,
        service=_FakeService(),  # type: ignore[arg-type]
        analysis_lock=asyncio.Lock(),
        state_path=checkpoint,
    )

    persisted = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert persisted["targets"][0]["shared_family_title"] == (
        "2 Zone Dual Zipper Far Infrared Sauna Blanket"
    )
    assert {
        item["offer_id"]: [parameter["value"] for parameter in item["parameters"]]
        for item in persisted["targets"][0]["variant_parameters"]
    } == {
        "grey": ["Grey"],
        "purple": ["Purple"],
        "black": ["Black"],
        "pink": ["Pink"],
    }


def test_stale_preview_does_not_start_a_batch(tmp_path: Path) -> None:
    service = _FakeService()
    controller = SearchRankingBatchController(
        tmp_path,
        service=service,  # type: ignore[arg-type]
        analysis_lock=asyncio.Lock(),
        state_path=tmp_path / "batch.json",
    )
    controller._build_preview = lambda stores: _preview([_target(1, "current")])  # type: ignore[method-assign]

    with pytest.raises(SearchRankingBatchInputError, match="已经变化"):
        controller.start(
            _stores(),
            actor_username="tester",
            actor_display_name="Tester",
            actor_is_admin=False,
            snapshot_id="b" * 64,
        )
    assert controller._task is None
    assert not (tmp_path / "batch.json").exists()
