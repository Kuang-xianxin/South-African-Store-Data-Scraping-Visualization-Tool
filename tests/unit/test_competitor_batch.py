from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from takealot_ops.competitors.batch import (
    CollectionBatchBusyError,
    CollectionBatchRegistry,
    CollectionRequestCoordinator,
    configure_collection_logger,
)


@pytest.mark.asyncio
async def test_collection_request_coordinator_reuses_inflight_and_completed() -> None:
    coordinator = CollectionRequestCoordinator[str]()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "collected"

    first = asyncio.create_task(coordinator.run("request-1", operation))
    await started.wait()
    second = asyncio.create_task(coordinator.run("request-1", operation))
    await asyncio.sleep(0)
    release.set()

    assert await first == ("collected", False)
    assert await second == ("collected", True)
    assert await coordinator.run("request-1", operation) == ("collected", True)
    assert calls == 1


@pytest.mark.asyncio
async def test_collection_request_survives_cancelled_browser_waiter() -> None:
    coordinator = CollectionRequestCoordinator[str]()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "collected"

    abandoned_waiter = asyncio.create_task(coordinator.run("request-2", operation))
    await started.wait()
    abandoned_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned_waiter

    resumed_waiter = asyncio.create_task(coordinator.run("request-2", operation))
    await asyncio.sleep(0)
    release.set()

    assert await resumed_waiter == ("collected", True)
    assert calls == 1


@pytest.mark.asyncio
async def test_collection_request_coordinator_explicitly_cancels_inflight_request() -> None:
    coordinator = CollectionRequestCoordinator[str]()
    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def operation() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    waiter = asyncio.create_task(coordinator.run("request-stop", operation))
    await started.wait()

    assert await coordinator.cancel("request-stop") is True
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert cleaned_up.is_set()
    assert await coordinator.cancel("request-stop") is False


def test_collection_logger_writes_rotating_project_log(tmp_path: Path) -> None:
    logger = configure_collection_logger(tmp_path)

    logger.info("batch_event batch=batch-1 event=start")
    for handler in logger.handlers:
        handler.flush()

    log_text = (tmp_path / "logs" / "competitor-collection.log").read_text(encoding="utf-8")
    assert "batch=batch-1 event=start" in log_text


def test_collection_batch_registry_blocks_other_users_and_syncs_progress() -> None:
    registry = CollectionBatchRegistry()
    status = registry.event(
        batch_id="batch-1",
        client_id="client-1",
        event="start",
        username="operator.one",
        display_name="Operator One",
        completed=0,
        total=20,
        pending=20,
        succeeded=0,
        failed=0,
        terminal=0,
        reason="",
    )
    assert status["active"] is True
    assert status["owner_display_name"] == "Operator One"

    with pytest.raises(CollectionBatchBusyError, match="Operator One"):
        registry.event(
            batch_id="batch-2",
            client_id="client-2",
            event="start",
            username="operator.two",
            display_name="Operator Two",
            completed=0,
            total=5,
            pending=5,
            succeeded=0,
            failed=0,
            terminal=0,
            reason="",
        )
    with pytest.raises(CollectionBatchBusyError, match="Operator One"):
        registry.event(
            batch_id="batch-1",
            client_id="another-tab",
            event="resume",
            username="operator.one",
            display_name="Operator One",
            completed=0,
            total=20,
            pending=20,
            succeeded=0,
            failed=0,
            terminal=0,
            reason="",
        )

    status = registry.event(
        batch_id="batch-1",
        client_id="client-1",
        event="progress",
        username="operator.one",
        display_name="Operator One",
        completed=7,
        total=20,
        pending=13,
        succeeded=6,
        failed=1,
        terminal=0,
        reason="",
    )
    assert status["completed"] == 7
    assert status["succeeded"] == 6
    assert registry.status()["pending"] == 13


def test_collection_batch_registry_waits_for_active_link_before_release() -> None:
    registry = CollectionBatchRegistry()
    registry.event(
        batch_id="batch-1",
        client_id="client-1",
        event="start",
        username="operator.one",
        display_name="Operator One",
        completed=0,
        total=2,
        pending=2,
        succeeded=0,
        failed=0,
        terminal=0,
        reason="",
    )
    registry.start_link(
        batch_id="batch-1",
        client_id="client-1",
        request_id="request-1",
        username="operator.one",
        display_name="Operator One",
        item_index=0,
        total_items=2,
        plid="12345678",
    )

    status = registry.event(
        batch_id="batch-1",
        client_id="client-1",
        event="manual_stop",
        username="operator.one",
        display_name="Operator One",
        completed=0,
        total=2,
        pending=2,
        succeeded=0,
        failed=0,
        terminal=0,
        reason="manual stop",
    )
    assert status["active"] is True
    assert status["current_plid"] == "12345678"

    registry.finish_link(
        batch_id="batch-1",
        request_id="request-1",
        reason="browser closed",
    )
    assert registry.status()["active"] is False


@pytest.mark.parametrize(
    "reason",
    [
        "监控清单新增了 1 个链接，已加入当前批次队头；当前商品结束后优先探测。",
        "PLID93033479 库存仍未探测，已安排在本轮其他链接之后第 1 次复探。",
    ],
)
def test_collection_batch_registry_downgrades_legacy_nonblocking_pause(
    reason: str,
) -> None:
    registry = CollectionBatchRegistry()
    registry.event(
        batch_id="batch-1",
        client_id="client-1",
        event="start",
        username="kxx",
        display_name="KXX",
        completed=0,
        total=3,
        pending=3,
        succeeded=0,
        failed=0,
        terminal=0,
        reason="",
    )

    status = registry.event(
        batch_id="batch-1",
        client_id="client-1",
        event="paused",
        username="kxx",
        display_name="KXX",
        completed=1,
        total=3,
        pending=2,
        succeeded=1,
        failed=0,
        terminal=0,
        reason=reason,
    )

    assert status["active"] is True
    assert status["event"] == "progress"
    assert status["reason"] == reason


def test_collection_batch_registry_blocks_parallel_links_but_allows_rejoin() -> None:
    registry = CollectionBatchRegistry()
    registry.event(
        batch_id="batch-1",
        client_id="client-1",
        event="start",
        username="operator.one",
        display_name="Operator One",
        completed=0,
        total=2,
        pending=2,
        succeeded=0,
        failed=0,
        terminal=0,
        reason="",
    )
    registry.start_link(
        batch_id="batch-1",
        client_id="client-1",
        request_id="request-1",
        username="operator.one",
        display_name="Operator One",
        item_index=0,
        total_items=2,
        plid="12345678",
    )
    registry.update_link_stage(
        batch_id="batch-1",
        request_id="request-1",
        stage="正在读取全部评论",
    )
    assert registry.status()["current_stage"] == "正在读取全部评论"

    registry.start_link(
        batch_id="batch-1",
        client_id="client-1",
        request_id="request-1",
        username="operator.one",
        display_name="Operator One",
        item_index=0,
        total_items=2,
        plid="12345678",
    )
    with pytest.raises(CollectionBatchBusyError, match="阻止另一页面并发"):
        registry.start_link(
            batch_id="batch-1",
            client_id="client-1",
            request_id="request-2",
            username="operator.one",
            display_name="Operator One",
            item_index=1,
            total_items=2,
            plid="87654321",
        )

    registry.finish_link(
        batch_id="batch-1",
        request_id="request-1",
        reason="done",
    )
    status = registry.status()
    assert status["current_request_id"] is None
    assert status["current_stage"] is None


def test_collection_batch_registry_transfers_same_account_control_at_link_boundary() -> None:
    registry = CollectionBatchRegistry()
    registry.event(
        batch_id="batch-1",
        client_id="client-old",
        event="start",
        username="kxx",
        display_name="管理员",
        completed=279,
        total=316,
        pending=37,
        succeeded=279,
        failed=37,
        terminal=0,
        reason="",
        visible_browser=False,
    )
    registry.start_link(
        batch_id="batch-1",
        client_id="client-old",
        request_id="request-1",
        username="kxx",
        display_name="管理员",
        item_index=280,
        total_items=316,
        plid="12345678",
        retry_kind="stock",
        retry_attempt=1,
    )

    pending, ready = registry.request_takeover(
        batch_id="batch-1",
        client_id="client-new",
        username="kxx",
    )
    assert ready is False
    assert pending["takeover_pending"] is True
    assert pending["current_retry_kind"] == "stock"
    assert pending["current_retry_attempt"] == 1

    registry.finish_link(
        batch_id="batch-1",
        request_id="request-1",
        reason="库存仍未探测",
    )
    transferred, ready = registry.request_takeover(
        batch_id="batch-1",
        client_id="client-new",
        username="kxx",
    )
    assert ready is True
    assert transferred["event"] == "takeover_ready"
    assert transferred["takeover_pending"] is False
    with pytest.raises(CollectionBatchBusyError, match="管理员"):
        registry.event(
            batch_id="batch-1",
            client_id="client-old",
            event="progress",
            username="kxx",
            display_name="管理员",
            completed=280,
            total=316,
            pending=36,
            succeeded=280,
            failed=36,
            terminal=0,
            reason="",
        )
    registry.start_link(
        batch_id="batch-1",
        client_id="client-new",
        request_id="request-2",
        username="kxx",
        display_name="管理员",
        item_index=281,
        total_items=316,
        plid="87654321",
    )
    assert registry.status()["current_request_id"] == "request-2"


def test_collection_batch_registry_syncs_visible_browser_for_next_link() -> None:
    registry = CollectionBatchRegistry()
    registry.event(
        batch_id="batch-1",
        client_id="client-1",
        event="start",
        username="kxx",
        display_name="管理员",
        completed=0,
        total=2,
        pending=2,
        succeeded=0,
        failed=0,
        terminal=0,
        reason="",
        visible_browser=False,
    )

    status = registry.update_options(
        batch_id="batch-1",
        username="kxx",
        visible_browser=True,
    )
    assert status["visible_browser"] is True
    assert registry.collection_options(
        batch_id="batch-1",
        fallback_with_stock_probe=False,
        fallback_visible_browser=False,
    ) == (True, True)
    with pytest.raises(CollectionBatchBusyError, match="管理员"):
        registry.update_options(
            batch_id="batch-1",
            username="another.user",
            visible_browser=False,
        )


def test_collection_batch_registry_appends_new_targets_to_active_tail() -> None:
    registry = CollectionBatchRegistry()
    registry.event(
        batch_id="batch-1",
        client_id="client-1",
        event="start",
        username="operator.one",
        display_name="Operator One",
        completed=1,
        total=2,
        pending=1,
        succeeded=1,
        failed=0,
        terminal=0,
        reason="",
    )

    assert registry.enqueue_target(
        plid="12345678",
        url="https://www.takealot.com/example/PLID12345678",
    )
    assert not registry.enqueue_target(
        plid="12345678",
        url="https://www.takealot.com/example/PLID12345678",
    )

    status = registry.event(
        batch_id="batch-1",
        client_id="client-1",
        event="progress",
        username="operator.one",
        display_name="Operator One",
        completed=1,
        total=2,
        pending=1,
        succeeded=1,
        failed=0,
        terminal=0,
        reason="",
    )
    assert status["total"] == 3
    assert status["pending"] == 2
    assert status["queued_targets"] == [
        {
            "plid": "12345678",
            "url": "https://www.takealot.com/example/PLID12345678",
            "queued_at": status["queued_targets"][0]["queued_at"],
        }
    ]

    registry.event(
        batch_id="batch-1",
        client_id="client-1",
        event="completed",
        username="operator.one",
        display_name="Operator One",
        completed=3,
        total=3,
        pending=0,
        succeeded=3,
        failed=0,
        terminal=0,
        reason="",
    )
    assert not registry.enqueue_target(
        plid="87654321",
        url="https://www.takealot.com/example/PLID87654321",
    )

    reset = registry.event(
        batch_id="batch-2",
        client_id="client-1",
        event="start",
        username="operator.one",
        display_name="Operator One",
        completed=0,
        total=1,
        pending=1,
        succeeded=0,
        failed=0,
        terminal=0,
        reason="",
    )
    assert reset["queued_targets"] == []


def test_collection_batch_registry_prioritizes_target_until_it_starts() -> None:
    registry = CollectionBatchRegistry()
    registry.event(
        batch_id="batch-1",
        client_id="client-1",
        event="start",
        username="operator.one",
        display_name="Operator One",
        completed=1,
        total=3,
        pending=2,
        succeeded=1,
        failed=0,
        terminal=0,
        reason="",
    )

    prioritized, accepted = registry.prioritize_target(
        plid="12345678",
        url="https://www.takealot.com/example/PLID12345678",
        requested_by="Supervisor",
    )

    assert accepted is True
    assert prioritized["priority_targets"] == [
        {
            "plid": "12345678",
            "url": "https://www.takealot.com/example/PLID12345678",
            "requested_at": prioritized["priority_targets"][0]["requested_at"],
            "requested_by": "Supervisor",
            "source": "manual",
        }
    ]
    assert prioritized["prioritized_targets"] == [
        {
            "plid": "12345678",
            "url": "https://www.takealot.com/example/PLID12345678",
            "requested_at": prioritized["priority_targets"][0]["requested_at"],
            "requested_by": "Supervisor",
            "source": "manual",
        }
    ]
    duplicate, duplicate_accepted = registry.prioritize_target(
        plid="12345678",
        url="https://www.takealot.com/example/PLID12345678",
        requested_by="Supervisor",
    )
    assert duplicate_accepted is False
    assert duplicate["priority_targets"] == prioritized["priority_targets"]
    assert duplicate["prioritized_targets"] == prioritized["prioritized_targets"]
    registry.start_link(
        batch_id="batch-1",
        client_id="client-1",
        request_id="request-priority",
        username="operator.one",
        display_name="Operator One",
        item_index=2,
        total_items=3,
        plid="12345678",
    )
    status = registry.status()
    assert status["current_plid"] == "12345678"
    assert status["priority_targets"] == []


def test_collection_batch_registry_allows_a_consumed_target_to_be_manually_retried() -> None:
    registry = CollectionBatchRegistry()
    registry.event(
        batch_id="batch-1",
        client_id="client-1",
        event="start",
        username="operator.one",
        display_name="Operator One",
        completed=1,
        total=3,
        pending=2,
        succeeded=0,
        failed=1,
        terminal=0,
        reason="",
    )
    _, first_accepted = registry.prioritize_target(
        plid="12345678",
        url="https://www.takealot.com/example/PLID12345678",
        requested_by="Supervisor",
    )
    assert first_accepted is True
    registry.start_link(
        batch_id="batch-1",
        client_id="client-1",
        request_id="request-priority",
        username="operator.one",
        display_name="Operator One",
        item_index=2,
        total_items=3,
        plid="12345678",
    )
    registry.finish_link(
        batch_id="batch-1",
        request_id="request-priority",
        reason="priority attempt failed",
    )

    retried, retry_accepted = registry.prioritize_target(
        plid="12345678",
        url="https://www.takealot.com/example/PLID12345678",
        requested_by="Supervisor",
        source="manual_retry",
    )
    duplicate, duplicate_accepted = registry.prioritize_target(
        plid="12345678",
        url="https://www.takealot.com/example/PLID12345678",
        requested_by="Supervisor",
        source="manual_retry",
    )

    assert retry_accepted is True
    assert duplicate_accepted is False
    assert retried["priority_targets"][0]["source"] == "manual_retry"
    assert duplicate["priority_targets"] == retried["priority_targets"]
    assert [
        item["source"] for item in retried["prioritized_targets"]
    ] == ["manual_retry", "manual"]


def test_collection_batch_registry_preserves_new_target_order_at_tail() -> None:
    registry = CollectionBatchRegistry()
    registry.event(
        batch_id="batch-1",
        client_id="client-1",
        event="start",
        username="operator.one",
        display_name="Operator One",
        completed=0,
        total=2,
        pending=2,
        succeeded=0,
        failed=0,
        terminal=0,
        reason="",
    )

    assert registry.enqueue_target(
        plid="12345678",
        url="https://www.takealot.com/first/PLID12345678",
    )
    assert registry.enqueue_target(
        plid="87654321",
        url="https://www.takealot.com/newest/PLID87654321",
    )
    assert [
        item["plid"] for item in registry.status()["queued_targets"]
    ] == ["12345678", "87654321"]
    assert registry.status()["prioritized_targets"] == []


def test_collection_batch_registry_restores_pending_queue_after_restart(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "competitor-batch-queue.json"
    registry = CollectionBatchRegistry(journal_path)
    registry.event(
        batch_id="batch-1",
        client_id="client-1",
        event="start",
        username="operator.one",
        display_name="Operator One",
        completed=0,
        total=2,
        pending=2,
        succeeded=0,
        failed=0,
        terminal=0,
        reason="",
    )
    registry.enqueue_target(
        plid="12345678",
        url="https://www.takealot.com/new/PLID12345678",
    )
    registry.prioritize_target(
        plid="87654321",
        url="https://www.takealot.com/existing/PLID87654321",
        requested_by="Supervisor",
    )

    restarted = CollectionBatchRegistry(journal_path)
    restored = restarted.event(
        batch_id="batch-1",
        client_id="client-1",
        event="resume",
        username="operator.one",
        display_name="Operator One",
        completed=0,
        total=3,
        pending=3,
        succeeded=0,
        failed=0,
        terminal=0,
        reason="",
    )

    assert [item["plid"] for item in restored["queued_targets"]] == ["12345678"]
    assert [item["plid"] for item in restored["priority_targets"]] == ["87654321"]
    assert {
        (item["plid"], item["source"])
        for item in restored["prioritized_targets"]
    } == {("87654321", "manual")}

    restarted.start_link(
        batch_id="batch-1",
        client_id="client-1",
        request_id="request-auto",
        username="operator.one",
        display_name="Operator One",
        item_index=2,
        total_items=3,
        plid="12345678",
    )
    after_start = CollectionBatchRegistry(journal_path)
    resumed_again = after_start.event(
        batch_id="batch-1",
        client_id="client-1",
        event="resume",
        username="operator.one",
        display_name="Operator One",
        completed=1,
        total=3,
        pending=2,
        succeeded=1,
        failed=0,
        terminal=0,
        reason="",
    )
    assert resumed_again["queued_targets"] == []
    assert [item["plid"] for item in resumed_again["priority_targets"]] == [
        "87654321"
    ]
    assert len(resumed_again["prioritized_targets"]) == 1


def test_collection_batch_registry_rejects_priority_without_active_batch() -> None:
    registry = CollectionBatchRegistry()

    with pytest.raises(CollectionBatchBusyError, match="没有可插队"):
        registry.prioritize_target(
            plid="12345678",
            url="https://www.takealot.com/example/PLID12345678",
            requested_by="Supervisor",
        )
