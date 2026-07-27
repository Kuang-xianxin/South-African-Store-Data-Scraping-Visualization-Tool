from __future__ import annotations

from pathlib import Path

import pytest

from takealot_ops.erp.coordination import RefreshBusyError, RefreshCoordinator


def test_refresh_cooldown_is_global_and_survives_coordinator_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    coordinator = RefreshCoordinator(tmp_path)

    started = coordinator.begin(
        username="operator.one",
        display_name="Operator One",
        role="operator",
    )
    assert started["in_progress"] is True
    with pytest.raises(RefreshBusyError, match="Operator One"):
        coordinator.begin(
            username="admin",
            display_name="Admin",
            role="admin",
        )
    finished = coordinator.finish(
        username="operator.one",
        display_name="Operator One",
        succeeded=True,
        role="operator",
    )
    assert finished["can_refresh"] is False
    assert 3_590 <= int(finished["cooldown_remaining_seconds"]) <= 3_600
    coordinator.close()

    restored = RefreshCoordinator(tmp_path)
    operator_status = restored.status(role="operator")
    admin_status = restored.status(role="admin")
    assert operator_status["last_success_by"] == "operator.one"
    assert operator_status["can_refresh"] is False
    assert admin_status["admin_exempt"] is True
    assert admin_status["can_refresh"] is True
    with pytest.raises(RefreshBusyError, match="全员冷却"):
        restored.begin(
            username="operator.two",
            display_name="Operator Two",
            role="operator",
        )
    restored.close()


def test_failed_refresh_does_not_start_cooldown(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "erp.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{database_path.as_posix()}",
    )
    coordinator = RefreshCoordinator(tmp_path)
    coordinator.begin(
        username="operator.one",
        display_name="Operator One",
        role="operator",
    )
    status = coordinator.finish(
        username="operator.one",
        display_name="Operator One",
        succeeded=False,
        role="operator",
    )
    assert status["cooldown_remaining_seconds"] == 0
    assert status["can_refresh"] is True
    coordinator.close()
