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


def test_collection_logger_writes_rotating_project_log(tmp_path: Path) -> None:
    logger = configure_collection_logger(tmp_path)

    logger.info("batch_event batch=batch-1 event=start")
    for handler in logger.handlers:
        handler.flush()

    log_text = (tmp_path / "logs" / "competitor-collection.log").read_text(
        encoding="utf-8"
    )
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
