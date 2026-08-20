from __future__ import annotations

import asyncio
import json
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
    pending_retry_delay_seconds: float = 600,
    pending_retry_round_limit: int = 3,
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
        pending_retry_delay_seconds=pending_retry_delay_seconds,
        pending_retry_round_limit=pending_retry_round_limit,
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


async def test_runner_skips_today_when_manual_batch_is_active(tmp_path: Path) -> None:
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
        triggered = await runner.trigger()
        await asyncio.sleep(0.03)
        assert triggered["accepted"] is False
        assert triggered["state"] == "skipped_busy"
        assert runner.status()["pending"] is False
        assert runner.status()["last_skipped_on"] == "2026-08-10"
        assert "手动批次" in str(runner.status()["last_skip_reason"])
        assert "当天不排队" in str(runner.status()["last_skip_reason"])
        assert load_calls == 0

        journal = json.loads((tmp_path / "scheduled.json").read_text(encoding="utf-8"))
        assert journal["handled_trigger_dates"] == ["2026-08-10"]

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
        await asyncio.sleep(0.03)
        assert load_calls == 0
        assert registry.status()["source"] == "manual"

        repeated = await runner.trigger()
        assert repeated["accepted"] is False
        assert repeated["state"] == "already_handled"
    finally:
        await runner.close()


async def test_runner_skips_if_manual_batch_wins_slot_during_target_load(
    tmp_path: Path,
) -> None:
    registry = CollectionBatchRegistry()
    load_started = asyncio.Event()
    release_load = asyncio.Event()
    collected: list[str] = []

    async def load_targets() -> list[ScheduledCollectionTarget]:
        load_started.set()
        await release_load.wait()
        return [
            ScheduledCollectionTarget(
                "33333334",
                "https://takealot.com/p/PLID33333334",
            )
        ]

    async def collect_target(
        url: str,
        *_args,
    ) -> ScheduledCollectionAttempt:
        collected.append(url)
        return ScheduledCollectionAttempt("33333334", "Product", "采集成功", True)

    runner = _runner(
        tmp_path,
        registry=registry,
        load_targets=load_targets,
        collect_target=collect_target,
    )
    try:
        triggered = await runner.trigger()
        assert triggered["accepted"] is True
        await _wait_for(load_started.is_set)
        registry.event(
            batch_id="manual-race-winner",
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
            reason="manual start during target load",
        )
        release_load.set()

        await _wait_for(
            lambda: runner.status()["last_skipped_on"] == "2026-08-10"
        )
        assert runner.status()["pending"] is False
        assert collected == []
        assert registry.status()["batch_id"] == "manual-race-winner"
        assert "manual-race-winner" in str(runner.status()["last_skip_reason"])
    finally:
        release_load.set()
        await runner.close()


async def test_runner_skips_offline_trigger_while_durable_batch_resumes(
    tmp_path: Path,
) -> None:
    previous_batch_id = "scheduled-20260810-resuming"
    previous_target = ScheduledCollectionTarget(
        "33333335",
        "https://takealot.com/p/PLID33333335",
    )
    journal = {
        "version": 1,
        "pending": False,
        "pending_for": None,
        "requested_at": FIXED_NOW.isoformat(),
        "run_status": "running",
        "batch_id": previous_batch_id,
        "last_started_on": "2026-08-10",
        "last_started_at": FIXED_NOW.isoformat(),
        "last_completed_at": None,
        "last_error": "",
        "targets": [
            {"index": 0, "plid": previous_target.plid, "url": previous_target.url}
        ],
        "queue": [{"index": 0, "url": previous_target.url}],
        "attempted_indexes": [],
        "failed_indexes": [],
        "terminal_indexes": [],
        "stock_unprobed_indexes": [],
        "results": [],
        "errors": [],
        "accepted_priority_keys": [],
        "last_target_refresh_completed": 0,
        "resume_after": None,
        "network_failures": [],
        "handled_trigger_dates": ["2026-08-10"],
        "active_item": None,
    }
    (tmp_path / "scheduled.json").write_text(
        json.dumps(journal, ensure_ascii=False),
        encoding="utf-8",
    )
    next_day = FIXED_NOW + timedelta(days=1)
    register_scheduled_trigger(tmp_path, now=next_day)
    trigger_dir = tmp_path / "logs" / "competitor-scheduled-triggers"
    load_calls = 0
    collected: list[str] = []

    async def load_targets() -> list[ScheduledCollectionTarget]:
        nonlocal load_calls
        load_calls += 1
        return [previous_target]

    async def collect_target(
        url: str,
        *_args,
    ) -> ScheduledCollectionAttempt:
        collected.append(url.rsplit("PLID", 1)[1])
        return ScheduledCollectionAttempt(
            previous_target.plid,
            "Previous batch product",
            "采集成功",
            True,
        )

    runner = _runner(
        tmp_path,
        registry=CollectionBatchRegistry(),
        load_targets=load_targets,
        collect_target=collect_target,
        trigger_dir=trigger_dir,
        clock=lambda: next_day,
    )
    try:
        runner.start()
        await _wait_for(lambda: runner.status()["run_status"] == "completed")

        assert collected == [previous_target.plid]
        assert load_calls >= 1
        assert runner.status()["last_skipped_on"] == "2026-08-11"
        assert previous_batch_id in str(runner.status()["last_skip_reason"])
        persisted = json.loads(
            (tmp_path / "scheduled.json").read_text(encoding="utf-8")
        )
        assert persisted["handled_trigger_dates"] == ["2026-08-10", "2026-08-11"]
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


async def test_kxx_explicitly_resumes_stopped_batch_in_frozen_target_order(
    tmp_path: Path,
) -> None:
    batch_id = "scheduled-20260810-resume"
    targets = [
        ScheduledCollectionTarget(plid, f"https://takealot.com/p/PLID{plid}")
        for plid in ("91000001", "91000002", "91000003", "91000004")
    ]
    initial_result = {
        "plid": "91000001",
        "url": targets[0].url,
        "title": "Product 91000001",
        "message": "采集成功",
        "added_target_count": 0,
    }
    initial_errors = [
        {"plid": "91000002", "url": targets[1].url, "message": "临时失败"},
        {"plid": "91000003", "url": targets[2].url, "message": "确认失效"},
    ]
    # A new ERP process starts with an empty in-memory registry. The scheduled
    # journal must remain sufficient to project and resume the stopped batch.
    registry = CollectionBatchRegistry()

    journal = {
        "version": 1,
        "pending": False,
        "pending_for": None,
        "requested_at": FIXED_NOW.isoformat(),
        "run_status": "stopped",
        "batch_id": batch_id,
        "last_started_on": "2026-08-10",
        "last_started_at": FIXED_NOW.isoformat(),
        "last_completed_at": None,
        "last_error": "",
        "targets": [
            {"index": index, "plid": target.plid, "url": target.url}
            for index, target in enumerate(targets)
        ],
        # The durable queue only contains the untouched tail. Explicit resume
        # must restore the earlier failed item ahead of it without replaying
        # the successful or confirmed-invalid items.
        "queue": [{"index": 3, "url": targets[3].url}],
        "attempted_indexes": [0, 1, 2],
        "failed_indexes": [1],
        "terminal_indexes": [2],
        "stock_unprobed_indexes": [],
        "results": [initial_result],
        "errors": initial_errors,
        "accepted_priority_keys": [],
        "last_target_refresh_completed": 3,
        "resume_after": None,
        "network_failures": [],
        "handled_trigger_dates": ["2026-08-10"],
        "active_item": None,
        "stopped_at": FIXED_NOW.isoformat(),
        "stopped_by": "kxx",
    }
    (tmp_path / "scheduled.json").write_text(
        json.dumps(journal, ensure_ascii=False),
        encoding="utf-8",
    )
    collected: list[str] = []

    async def load_targets() -> list[ScheduledCollectionTarget]:
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
    )
    try:
        assert runner.status()["resume_available"] is True
        assert runner.status()["resumable_pending"] == 2
        projected = runner.stopped_checkpoint_status()
        assert projected is not None
        assert projected["batch_id"] == batch_id
        assert projected["completed"] == 3
        assert projected["pending"] == 2
        assert [row["plid"] for row in projected["results"]] == ["91000001"]

        resumed = await runner.resume_stopped(batch_id, resumed_by="kxx")
        assert resumed["active"] is True
        assert resumed["batch_id"] == batch_id
        assert resumed["event"] == "resume"
        assert resumed["pending"] == 2

        await _wait_for(lambda: runner.status()["run_status"] == "completed")
        assert collected == ["91000002", "91000004"]
        final_status = registry.status()
        assert final_status["batch_id"] == batch_id
        assert final_status["completed"] == 4
        assert final_status["total"] == 4
        assert final_status["pending"] == 0
        assert final_status["succeeded"] == 3
        assert final_status["terminal"] == 1

        repeated_trigger = await runner.trigger()
        assert repeated_trigger["accepted"] is False
        assert repeated_trigger["state"] == "already_handled"
    finally:
        await runner.close()


async def test_tail_failure_waits_and_retries_until_confirmed_invalid(
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
    attempt_count = 0

    async def sleeper(seconds: float) -> None:
        observed_now[0] += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    async def load_targets() -> list[ScheduledCollectionTarget]:
        return [
            ScheduledCollectionTarget(
                "92000001",
                "https://takealot.com/p/PLID92000001",
            )
        ]

    async def collect_target(
        *_args,
    ) -> ScheduledCollectionAttempt:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count < 3:
            return ScheduledCollectionAttempt(
                "92000001",
                None,
                f"疑似失效复核 {attempt_count}/3",
                False,
                failure_kind="suspected-invalid",
                retryable=True,
            )
        return ScheduledCollectionAttempt(
            "92000001",
            None,
            "确认失效",
            False,
            failure_kind="confirmed-invalid",
            retryable=False,
        )

    runner = _runner(
        tmp_path,
        registry=registry,
        load_targets=load_targets,
        collect_target=collect_target,
        clock=lambda: observed_now[0],
        sleeper=sleeper,
        pending_retry_delay_seconds=0.02,
    )
    try:
        await runner.trigger()
        await _wait_for(lambda: runner.status()["run_status"] == "completed")

        assert attempt_count == 3
        assert registry.events.count("scheduled_pause") >= 2
        assert registry.events.count("auto_resume") == 2
        status = registry.status()
        assert status["pending"] == 0
        assert status["failed"] == 0
        assert status["terminal"] == 1
    finally:
        await runner.close()


async def test_suspected_invalid_skips_inline_retry_until_delayed_wave(
    tmp_path: Path,
) -> None:
    registry = CollectionBatchRegistry()
    observed_now = [FIXED_NOW]
    calls: list[str] = []
    sleeps: list[float] = []
    suspect_attempts = 0

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        observed_now[0] += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    async def load_targets() -> list[ScheduledCollectionTarget]:
        return [
            ScheduledCollectionTarget(
                plid,
                f"https://takealot.com/p/PLID{plid}",
            )
            for plid in ("92500001", "92500002", "92500003")
        ]

    async def collect_target(
        url: str,
        *_args,
    ) -> ScheduledCollectionAttempt:
        nonlocal suspect_attempts
        plid = url.rsplit("PLID", 1)[-1]
        calls.append(plid)
        if plid == "92500001":
            suspect_attempts += 1
            if suspect_attempts == 1:
                return ScheduledCollectionAttempt(
                    plid,
                    None,
                    "疑似失效，等待间隔复核",
                    False,
                    failure_kind="suspected-invalid",
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
        pending_retry_delay_seconds=0.02,
    )
    try:
        await runner.trigger()
        await _wait_for(lambda: runner.status()["run_status"] == "completed")

        assert calls == ["92500001", "92500002", "92500003", "92500001"]
        assert round(sum(sleeps), 6) == 0.02
        status = registry.status()
        assert status["succeeded"] == 3
        assert status["failed"] == 0
    finally:
        await runner.close()


async def test_exhausted_delayed_retries_can_continue_same_server_batch(
    tmp_path: Path,
) -> None:
    registry = CollectionBatchRegistry()
    observed_now = [FIXED_NOW]
    attempt_count = 0

    async def sleeper(seconds: float) -> None:
        observed_now[0] += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    async def load_targets() -> list[ScheduledCollectionTarget]:
        return [
            ScheduledCollectionTarget(
                "93000001",
                "https://takealot.com/p/PLID93000001",
            )
        ]

    async def collect_target(
        *_args,
    ) -> ScheduledCollectionAttempt:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count <= 2:
            return ScheduledCollectionAttempt(
                "93000001",
                None,
                "页面复核仍不确定",
                False,
                failure_kind="validation-uncertain",
                retryable=True,
            )
        return ScheduledCollectionAttempt(
            "93000001",
            "Recovered Product",
            "采集成功",
            True,
        )

    runner = _runner(
        tmp_path,
        registry=registry,
        load_targets=load_targets,
        collect_target=collect_target,
        clock=lambda: observed_now[0],
        sleeper=sleeper,
        pending_retry_delay_seconds=0.02,
        pending_retry_round_limit=1,
    )
    try:
        await runner.trigger()
        await _wait_for(
            lambda: runner.status()["run_status"] == "completed_with_pending"
        )
        batch_id = str(runner.status()["batch_id"])
        assert attempt_count == 2
        assert runner.status()["resume_available"] is True
        assert runner.status()["resumable_pending"] == 1
        projected = runner.stopped_checkpoint_status()
        assert projected is not None
        assert projected["event"] == "completed"
        assert projected["batch_id"] == batch_id

        resumed = await runner.resume_stopped(batch_id, resumed_by="kxx")
        assert resumed["active"] is True
        assert resumed["batch_id"] == batch_id
        await _wait_for(lambda: runner.status()["run_status"] == "completed")
        assert attempt_count == 3
        assert registry.status()["succeeded"] == 1
    finally:
        await runner.close()


async def test_start_upgrades_legacy_completed_pending_checkpoint(
    tmp_path: Path,
) -> None:
    batch_id = "scheduled-20260810-legacy-pending"
    target = ScheduledCollectionTarget(
        "94000001",
        "https://takealot.com/p/PLID94000001",
    )
    journal = {
        "version": 1,
        "pending": False,
        "pending_for": None,
        "requested_at": FIXED_NOW.isoformat(),
        "run_status": "completed_with_pending",
        "batch_id": batch_id,
        "last_started_on": "2026-08-10",
        "last_started_at": (FIXED_NOW - timedelta(hours=1)).isoformat(),
        "last_completed_at": (FIXED_NOW - timedelta(minutes=11)).isoformat(),
        "last_error": "",
        "targets": [{"index": 0, "plid": target.plid, "url": target.url}],
        "queue": [],
        "attempted_indexes": [0],
        "failed_indexes": [0],
        "terminal_indexes": [],
        "stock_unprobed_indexes": [],
        "results": [],
        "errors": [
            {"plid": target.plid, "url": target.url, "message": "待重试"}
        ],
        "accepted_priority_keys": [],
        "last_target_refresh_completed": 1,
        "resume_after": None,
        "network_failures": [],
        "handled_trigger_dates": ["2026-08-10"],
        "active_item": None,
    }
    (tmp_path / "scheduled.json").write_text(
        json.dumps(journal, ensure_ascii=False),
        encoding="utf-8",
    )
    registry = CollectionBatchRegistry()
    collected: list[str] = []

    async def load_targets() -> list[ScheduledCollectionTarget]:
        return [target]

    async def collect_target(
        url: str,
        *_args,
    ) -> ScheduledCollectionAttempt:
        collected.append(url.rsplit("PLID", 1)[1])
        return ScheduledCollectionAttempt(
            target.plid,
            "Recovered Product",
            "采集成功",
            True,
        )

    runner = _runner(
        tmp_path,
        registry=registry,
        load_targets=load_targets,
        collect_target=collect_target,
    )
    try:
        assert runner.status()["resume_available"] is True
        runner.start()
        await _wait_for(lambda: runner.status()["run_status"] == "completed")
        assert collected == [target.plid]
        assert registry.status()["batch_id"] == batch_id
        assert registry.status()["succeeded"] == 1
    finally:
        await runner.close()
