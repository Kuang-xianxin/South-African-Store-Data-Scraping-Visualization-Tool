from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from takealot_ops.competitors.batch import (
    CollectionBatchBusyError,
    CollectionBatchRegistry,
    CollectionRequestCoordinator,
    configure_collection_logger,
    read_collection_round_summaries,
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


def test_collection_logger_writes_aggregate_and_per_round_logs(tmp_path: Path) -> None:
    logger = configure_collection_logger(tmp_path)

    logger.info("batch_event batch=batch-1 event=start")
    logger.info("link_result batch=batch-1 request=request-1 succeeded=True")
    logger.info("batch_event batch=batch-2 event=start")
    logger.info("scheduled_trigger date=2026-08-22 state=accepted")
    for handler in logger.handlers:
        handler.flush()

    log_text = (tmp_path / "logs" / "competitor-collection.log").read_text(encoding="utf-8")
    assert "batch=batch-1 event=start" in log_text
    round_one = (
        tmp_path / "logs" / "competitor-rounds" / "batch-1.log"
    ).read_text(encoding="utf-8")
    round_two = (
        tmp_path / "logs" / "competitor-rounds" / "batch-2.log"
    ).read_text(encoding="utf-8")
    assert "batch=batch-1 event=start" in round_one
    assert "link_result batch=batch-1 request=request-1 succeeded=True" in round_one
    assert "batch=batch-2" not in round_one
    assert "scheduled_trigger" not in round_one
    assert "batch=batch-2 event=start" in round_two


def test_collection_round_log_reader_returns_structured_round_summaries(
    tmp_path: Path,
) -> None:
    round_dir = tmp_path / "logs" / "competitor-rounds"
    round_dir.mkdir(parents=True)
    (round_dir / "batch-current.log").write_text(
        "\n".join(
            [
                "2026-08-22 10:00:00,000 INFO round_event batch=batch-current "
                "source=scheduled round=3 revision=7 event=start "
                "trigger_date=2026-08-22 item=-/6 plid=- retry_kind=- "
                "retry_attempt=- retry_round=0/3 succeeded=- failure_kind=- "
                "completed=0 total=6 pending=6 succeeded_total=0 failed=0 "
                "terminal=0 wall_elapsed_seconds=0 reason=windows_scheduled_trigger",
                "2026-08-22 10:02:00,000 INFO link_result batch=batch-current "
                "request=request-4 item=4/6 plid=123 succeeded=True duration_ms=2000",
                "2026-08-22 10:02:00,100 INFO round_event batch=batch-current "
                "source=scheduled round=3 revision=7 event=progress "
                "trigger_date=2026-08-22 item=4/6 plid=123 retry_kind=- "
                "retry_attempt=- retry_round=0/3 succeeded=True failure_kind=- "
                "completed=4 total=6 pending=2 succeeded_total=3 failed=0 "
                "terminal=1 wall_elapsed_seconds=120.1 reason=单个商品采集成功",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (round_dir / "batch-previous.log").write_text(
        "2026-08-22 09:05:00,000 INFO batch_event batch=batch-previous "
        "event=completed completed=5 total=5 pending=0 succeeded=4 failed=0 "
        "terminal=1 user=kxx source=manual result_count=4 error_count=1 "
        "stock_probe=True visible_browser=False wall_elapsed_seconds=300 "
        "reason=本轮完成\n",
        encoding="utf-8",
    )

    payload = read_collection_round_summaries(
        tmp_path,
        current_batch_id="batch-current",
        selected_batch_id="batch-current",
    )

    assert payload["selected_batch_id"] == "batch-current"
    assert "content" not in payload
    selected = payload["selected_round"]
    assert isinstance(selected, dict)
    assert selected["status"] == "running"
    assert selected["source"] == "scheduled"
    assert selected["round_number"] == 3
    assert selected["revision"] == 7
    assert selected["completed"] == 4
    assert selected["total"] == 6
    assert selected["succeeded"] == 3
    assert selected["terminal"] == 1
    assert selected["pending"] == 2
    assert selected["retry_round"] == 0
    assert selected["retry_round_limit"] == 3
    assert selected["started_at"] == "2026-08-22T10:00:00+08:00"
    assert selected["reason"] == ""
    rounds = payload["rounds"]
    assert isinstance(rounds, list)
    assert any(
        item["batch_id"] == "batch-current" and item["current"] is True
        for item in rounds
    )
    previous = next(item for item in rounds if item["batch_id"] == "batch-previous")
    assert previous["status"] == "completed"
    assert previous["completed_at"] == "2026-08-22T09:05:00+08:00"
    assert previous["reason"] == "本轮完成"

    with pytest.raises(ValueError, match="批次编号格式无效"):
        read_collection_round_summaries(tmp_path, selected_batch_id="../outside")
    with pytest.raises(FileNotFoundError, match="未找到批次"):
        read_collection_round_summaries(tmp_path, selected_batch_id="batch-missing")


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
    stopping = registry.status()
    assert stopping["active"] is True
    assert stopping["event"] == "manual_stop"
    assert stopping["reason"] == "manual stop"

    with pytest.raises(CollectionBatchBusyError):
        registry.event(
            batch_id="batch-2",
            client_id="client-2",
            event="start",
            username="operator.two",
            display_name="Operator Two",
            completed=0,
            total=1,
            pending=1,
            succeeded=0,
            failed=0,
            terminal=0,
            reason="",
        )

    released = registry.complete_stop(batch_id="batch-1")
    assert released["active"] is False


def test_collection_batch_stop_between_links_holds_slot_until_cleanup() -> None:
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
        reason="between links",
    )

    stopping = registry.stop(batch_id="batch-1", reason="manual stop")
    assert stopping["active"] is True
    assert stopping["current_request_id"] is None
    with pytest.raises(CollectionBatchBusyError):
        registry.event(
            batch_id="batch-2",
            client_id="client-2",
            event="start",
            username="operator.two",
            display_name="Operator Two",
            completed=0,
            total=1,
            pending=1,
            succeeded=0,
            failed=0,
            terminal=0,
            reason="",
        )

    assert registry.complete_stop(batch_id="batch-1")["active"] is False


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


def test_kxx_updates_and_restores_scheduled_batch_visible_browser(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "competitor-batch-queue.json"
    registry = CollectionBatchRegistry(journal_path)
    registry.event(
        batch_id="scheduled-20260810-test",
        client_id="scheduled-runner",
        event="start",
        username="scheduled-task",
        display_name="每日 09:00 自动任务",
        completed=0,
        total=2,
        pending=2,
        succeeded=0,
        failed=0,
        terminal=0,
        reason="",
        visible_browser=False,
        source="scheduled",
    )
    current_link_options = registry.collection_options(
        batch_id="scheduled-20260810-test",
        fallback_with_stock_probe=True,
        fallback_visible_browser=False,
    )

    status = registry.update_options(
        batch_id="scheduled-20260810-test",
        username="kxx",
        visible_browser=True,
    )

    assert current_link_options == (True, False)
    assert status["visible_browser"] is True
    assert registry.collection_options(
        batch_id="scheduled-20260810-test",
        fallback_with_stock_probe=True,
        fallback_visible_browser=False,
    ) == (True, True)
    with pytest.raises(CollectionBatchBusyError, match="每日 09:00 自动任务"):
        registry.update_options(
            batch_id="scheduled-20260810-test",
            username="another.admin",
            visible_browser=False,
        )

    restored_registry = CollectionBatchRegistry(journal_path)
    restored = restored_registry.event(
        batch_id="scheduled-20260810-test",
        client_id="scheduled-runner",
        event="resume",
        username="scheduled-task",
        display_name="每日 09:00 自动任务",
        completed=0,
        total=2,
        pending=2,
        succeeded=0,
        failed=0,
        terminal=0,
        reason="ERP 重启后恢复",
        visible_browser=False,
        source="scheduled",
    )
    assert restored["visible_browser"] is True


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


def test_collection_batch_status_separates_and_pages_retry_and_terminal_details() -> None:
    registry = CollectionBatchRegistry()
    registry.event(
        batch_id="batch-large",
        client_id="client-1",
        event="start",
        username="operator.one",
        display_name="Operator One",
        completed=0,
        total=129,
        pending=129,
        succeeded=0,
        failed=0,
        terminal=0,
        reason="",
    )
    for index in range(120):
        plid = str(10_000_000 + index)
        registry.record_outcome(
            batch_id="batch-large",
            plid=plid,
            url=f"https://www.takealot.com/p/PLID{plid}",
            title=f"Product {index}",
            message="collected",
            succeeded=True,
        )
    for index in range(5):
        plid = str(20_000_000 + index)
        registry.record_outcome(
            batch_id="batch-large",
            plid=plid,
            url=f"https://www.takealot.com/p/PLID{plid}",
            title=None,
            message="retry later",
            succeeded=False,
        )
    for index in range(4):
        plid = str(30_000_000 + index)
        registry.record_outcome(
            batch_id="batch-large",
            plid=plid,
            url=f"https://www.takealot.com/p/PLID{plid}",
            title=None,
            message="confirmed invalid",
            succeeded=False,
            failure_kind="confirmed-invalid",
        )

    lightweight = registry.status(include_details=False)
    page = registry.status(
        include_details=True,
        result_offset=50,
        error_offset=2,
        terminal_error_offset=1,
        detail_limit=25,
    )

    assert lightweight["result_count"] == 120
    assert lightweight["error_count"] == 5
    assert lightweight["terminal_error_count"] == 4
    assert lightweight["results"] == []
    assert lightweight["errors"] == []
    assert lightweight["terminal_errors"] == []
    assert len(page["results"]) == 25
    assert page["results"][0]["plid"] == "10000050"
    assert len(page["errors"]) == 3
    assert page["errors"][0]["plid"] == "20000002"
    assert len(page["terminal_errors"]) == 3
    assert page["terminal_errors"][0]["plid"] == "30000001"
