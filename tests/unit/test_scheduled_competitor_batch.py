from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from takealot_ops.competitors.batch import CollectionBatchRegistry
from takealot_ops.competitors.scheduled import (
    ScheduledCollectionAttempt,
    ScheduledCollectionTarget,
    ScheduledCompetitorBatchRunner,
    register_scheduled_trigger,
)


FIXED_NOW = datetime(2026, 8, 10, 1, 0, tzinfo=UTC)


async def _wait_for(predicate, *, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def _runner(
    tmp_path: Path,
    *,
    registry: CollectionBatchRegistry,
    load_targets,
    collect_target,
    trigger_dir: Path | None = None,
    clock=None,
    sleeper=asyncio.sleep,
    network_pause_seconds: float = 600,
) -> ScheduledCompetitorBatchRunner:
    import logging

    return ScheduledCompetitorBatchRunner(
        registry=registry,
        journal_path=tmp_path / "scheduled.json",
        trigger_dir=trigger_dir,
        load_targets=load_targets,
        collect_target=collect_target,
        logger=logging.getLogger("tests.scheduled-competitor"),
        clock=clock or (lambda: FIXED_NOW),
        sleeper=sleeper,
        busy_poll_seconds=0.005,
        inter_target_seconds=0,
        network_pause_seconds=network_pause_seconds,
    )


def test_trigger_file_is_idempotent_per_beijing_day(tmp_path: Path) -> None:
    first = register_scheduled_trigger(tmp_path, now=FIXED_NOW)
    second = register_scheduled_trigger(tmp_path, now=FIXED_NOW)

    assert first == ("2026-08-10", True)
    assert second == ("2026-08-10", False)
    trigger = tmp_path / "logs" / "competitor-scheduled-triggers" / "2026-08-10.json"
    assert trigger.is_file()
    assert '"requested_for": "2026-08-10"' in trigger.read_text(encoding="utf-8")


async def test_runner_publishes_visible_results_and_only_starts_once(
    tmp_path: Path,
) -> None:
    registry = CollectionBatchRegistry()

    async def load_targets() -> list[ScheduledCollectionTarget]:
        return [
            ScheduledCollectionTarget("11111111", "https://takealot.com/p/PLID11111111"),
            ScheduledCollectionTarget("22222222", "https://takealot.com/p/PLID22222222"),
        ]

    async def collect_target(
        url: str,
        _batch_id: str,
        _request_id: str,
        _item_index: int,
        _total_items: int,
        _retry_kind: str | None,
        _retry_attempt: int | None,
    ) -> ScheduledCollectionAttempt:
        plid = url.rsplit("PLID", 1)[1]
        return ScheduledCollectionAttempt(
            plid=plid,
            title=f"Product {plid}",
            message="采集成功",
            succeeded=True,
        )

    runner = _runner(
        tmp_path,
        registry=registry,
        load_targets=load_targets,
        collect_target=collect_target,
    )
    try:
        triggered = await runner.trigger()
        assert triggered["accepted"] is True
        await _wait_for(lambda: runner.status()["run_status"] == "completed")

        status = registry.status()
        assert status["active"] is False
        assert status["source"] == "scheduled"
        assert status["owner_display_name"] == "每日 09:00 自动任务"
        assert status["completed"] == 2
        assert status["total"] == 2
        assert status["succeeded"] == 2
        assert [row["plid"] for row in status["results"]] == ["11111111", "22222222"]
        repeated = await runner.trigger()
        assert repeated["accepted"] is False
        assert repeated["state"] == "already_handled"
    finally:
        await runner.close()


async def test_runner_defers_while_manual_batch_is_active(tmp_path: Path) -> None:
    registry = CollectionBatchRegistry()
    registry.event(
        batch_id="manual-batch",
        client_id="manual-client",
        event="start",
        username="kxx",
        display_name="KXX",
        completed=0,
        total=1,
        pending=1,
        succeeded=0,
        failed=0,
        terminal=0,
        reason="",
    )
    load_calls = 0

    async def load_targets() -> list[ScheduledCollectionTarget]:
        nonlocal load_calls
        load_calls += 1
        return [ScheduledCollectionTarget("33333333", "https://takealot.com/p/PLID33333333")]

    async def collect_target(*_args) -> ScheduledCollectionAttempt:
        return ScheduledCollectionAttempt("33333333", "Product", "采集成功", True)

    runner = _runner(
        tmp_path,
        registry=registry,
        load_targets=load_targets,
        collect_target=collect_target,
    )
    try:
        await runner.trigger()
        await asyncio.sleep(0.03)
        assert runner.status()["pending"] is True
        assert load_calls == 0

        registry.event(
            batch_id="manual-batch",
            client_id="manual-client",
            event="completed",
            username="kxx",
            display_name="KXX",
            completed=1,
            total=1,
            pending=0,
            succeeded=1,
            failed=0,
            terminal=0,
            reason="done",
        )
        await _wait_for(lambda: runner.status()["run_status"] == "completed")
        assert load_calls >= 1
        assert registry.status()["source"] == "scheduled"
    finally:
        await runner.close()


async def test_runner_imports_offline_trigger_and_appends_new_store_plid(
    tmp_path: Path,
) -> None:
    register_scheduled_trigger(tmp_path, now=FIXED_NOW)
    registry = CollectionBatchRegistry()
    loads = 0
    collected: list[str] = []

    async def load_targets() -> list[ScheduledCollectionTarget]:
        nonlocal loads
        loads += 1
        targets = [
            ScheduledCollectionTarget("44444444", "https://takealot.com/p/PLID44444444")
        ]
        if loads >= 2:
            targets.append(
                ScheduledCollectionTarget("55555555", "https://takealot.com/p/PLID55555555")
            )
        return targets

    async def collect_target(
        url: str,
        *_args,
    ) -> ScheduledCollectionAttempt:
        plid = url.rsplit("PLID", 1)[1]
        collected.append(plid)
        return ScheduledCollectionAttempt(plid, f"Product {plid}", "采集成功", True)

    runner = _runner(
        tmp_path,
        registry=registry,
        load_targets=load_targets,
        collect_target=collect_target,
        trigger_dir=tmp_path / "logs" / "competitor-scheduled-triggers",
    )
    try:
        runner.start()
        await _wait_for(lambda: runner.status()["run_status"] == "completed")
        assert collected == ["44444444", "55555555"]
        assert registry.status()["total"] == 2
    finally:
        await runner.close()


async def test_backlogged_trigger_does_not_swallow_current_day_trigger(
    tmp_path: Path,
) -> None:
    register_scheduled_trigger(
        tmp_path,
        now=datetime(2026, 8, 9, 1, 0, tzinfo=UTC),
    )
    register_scheduled_trigger(tmp_path, now=FIXED_NOW)
    registry = CollectionBatchRegistry()
    collected: list[str] = []

    async def load_targets() -> list[ScheduledCollectionTarget]:
        return [
            ScheduledCollectionTarget(
                "77777777",
                "https://takealot.com/p/PLID77777777",
            )
        ]

    async def collect_target(
        url: str,
        *_args,
    ) -> ScheduledCollectionAttempt:
        collected.append(url.rsplit("PLID", 1)[1])
        return ScheduledCollectionAttempt("77777777", "Product", "采集成功", True)

    runner = _runner(
        tmp_path,
        registry=registry,
        load_targets=load_targets,
        collect_target=collect_target,
        trigger_dir=tmp_path / "logs" / "competitor-scheduled-triggers",
    )
    try:
        runner.start()
        await _wait_for(lambda: len(collected) == 2)
        await _wait_for(lambda: runner.status()["run_status"] == "completed")
        assert runner.status()["last_started_on"] == "2026-08-10"
    finally:
        await runner.close()


async def test_two_consecutive_retryable_server_failures_pause_and_auto_resume(
    tmp_path: Path,
) -> None:
    class RecordingRegistry(CollectionBatchRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[str] = []

        def event(self, **kwargs):
            self.events.append(str(kwargs.get("event") or ""))
            return super().event(**kwargs)

    registry = RecordingRegistry()
    observed_now = [FIXED_NOW]
    attempts: dict[str, int] = {}

    async def sleeper(seconds: float) -> None:
        observed_now[0] += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    async def load_targets() -> list[ScheduledCollectionTarget]:
        return [
            ScheduledCollectionTarget(plid, f"https://takealot.com/p/PLID{plid}")
            for plid in ("81000001", "81000002", "81000003")
        ]

    async def collect_target(
        url: str,
        *_args,
    ) -> ScheduledCollectionAttempt:
        plid = url.rsplit("PLID", 1)[1]
        attempts[plid] = attempts.get(plid, 0) + 1
        if plid in {"81000001", "81000002"} and attempts[plid] == 1:
            return ScheduledCollectionAttempt(
                plid,
                None,
                "temporary network failure",
                False,
                failure_kind="other",
                retryable=True,
            )
        return ScheduledCollectionAttempt(plid, f"Product {plid}", "采集成功", True)

    runner = _runner(
        tmp_path,
        registry=registry,
        load_targets=load_targets,
        collect_target=collect_target,
        clock=lambda: observed_now[0],
        sleeper=sleeper,
        network_pause_seconds=0.02,
    )
    try:
        await runner.trigger()
        await _wait_for(lambda: runner.status()["run_status"] == "completed")
        assert "scheduled_pause" in registry.events
        assert "auto_resume" in registry.events
        assert registry.status()["succeeded"] == 3
    finally:
        await runner.close()


async def test_network_pause_remains_visible_and_can_be_stopped(
    tmp_path: Path,
) -> None:
    registry = CollectionBatchRegistry()

    async def load_targets() -> list[ScheduledCollectionTarget]:
        return [
            ScheduledCollectionTarget(plid, f"https://takealot.com/p/PLID{plid}")
            for plid in ("82000001", "82000002", "82000003")
        ]

    async def collect_target(
        url: str,
        *_args,
    ) -> ScheduledCollectionAttempt:
        plid = url.rsplit("PLID", 1)[1]
        return ScheduledCollectionAttempt(
            plid,
            None,
            "temporary network failure",
            False,
            failure_kind="network",
            retryable=True,
        )

    runner = _runner(
        tmp_path,
        registry=registry,
        load_targets=load_targets,
        collect_target=collect_target,
    )
    try:
        await runner.trigger()
        await _wait_for(lambda: runner.status()["run_status"] == "paused")
        status = registry.status()
        assert status["active"] is True
        assert status["event"] == "scheduled_pause"
        assert "连续2条网络连接失败" in str(status["reason"])

        batch_id = str(status["batch_id"])
        stopped = registry.stop(batch_id=batch_id, reason="kxx stopped pause")
        assert stopped["active"] is True
        assert await runner.mark_stopped(batch_id, stopped_by="kxx") is True
        released = registry.complete_stop(batch_id=batch_id)
        assert released["active"] is False
        assert runner.status()["run_status"] == "stopped"
    finally:
        await runner.close()


async def test_stop_during_target_refresh_cannot_enqueue_into_manual_batch(
    tmp_path: Path,
) -> None:
    registry = CollectionBatchRegistry()
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    load_count = 0

    async def load_targets() -> list[ScheduledCollectionTarget]:
        nonlocal load_count
        load_count += 1
        original = ScheduledCollectionTarget(
            "83000001",
            "https://takealot.com/p/PLID83000001",
        )
        if load_count == 1:
            return [original]
        refresh_started.set()
        await release_refresh.wait()
        return [
            original,
            ScheduledCollectionTarget(
                "83000002",
                "https://takealot.com/p/PLID83000002",
            ),
        ]

    async def collect_target(
        url: str,
        *_args,
    ) -> ScheduledCollectionAttempt:
        plid = url.rsplit("PLID", 1)[1]
        return ScheduledCollectionAttempt(plid, f"Product {plid}", "采集成功", True)

    runner = _runner(
        tmp_path,
        registry=registry,
        load_targets=load_targets,
        collect_target=collect_target,
    )
    try:
        await runner.trigger()
        await _wait_for(refresh_started.is_set)
        scheduled_batch_id = str(registry.status()["batch_id"])
        registry.stop(batch_id=scheduled_batch_id, reason="stop during refresh")
        assert await runner.mark_stopped(
            scheduled_batch_id,
            stopped_by="kxx",
        ) is True
        registry.complete_stop(batch_id=scheduled_batch_id)

        registry.event(
            batch_id="manual-after-stop",
            client_id="manual-client",
            event="start",
            username="kxx",
            display_name="KXX",
            completed=0,
            total=1,
            pending=1,
            succeeded=0,
            failed=0,
            terminal=0,
            reason="manual start",
        )
        release_refresh.set()
        await asyncio.sleep(0.03)

        status = registry.status()
        assert status["batch_id"] == "manual-after-stop"
        assert status["queued_targets"] == []
        assert runner.status()["run_status"] == "stopped"
    finally:
        release_refresh.set()
        await runner.close()


async def test_stopped_scheduled_batch_does_not_restart_same_day(tmp_path: Path) -> None:
    registry = CollectionBatchRegistry()
    collection_started = asyncio.Event()
    release_collection = asyncio.Event()

    async def load_targets() -> list[ScheduledCollectionTarget]:
        return [ScheduledCollectionTarget("66666666", "https://takealot.com/p/PLID66666666")]

    async def collect_target(
        *_args,
    ) -> ScheduledCollectionAttempt:
        collection_started.set()
        await release_collection.wait()
        return ScheduledCollectionAttempt("66666666", "Product", "采集成功", True)

    runner = _runner(
        tmp_path,
        registry=registry,
        load_targets=load_targets,
        collect_target=collect_target,
    )
    try:
        await runner.trigger()
        await _wait_for(collection_started.is_set)
        batch_id = str(registry.status()["batch_id"])
        registry.stop(batch_id=batch_id, reason="kxx stopped")
        assert await runner.mark_stopped(batch_id, stopped_by="kxx") is True
        registry.complete_stop(batch_id=batch_id)
        release_collection.set()
        await _wait_for(lambda: runner.status()["run_status"] == "stopped")

        repeated = await runner.trigger()
        assert repeated["accepted"] is False
        assert repeated["state"] == "already_handled"
        assert registry.status()["active"] is False
    finally:
        release_collection.set()
        await runner.close()
