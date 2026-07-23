from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from takealot_ops.erp.web import create_app


PROJECT_ROOT = Path(__file__).parents[2]


def test_erp_health_and_empty_read_routes_do_not_create_missing_database(
    tmp_path: Path,
    monkeypatch,
) -> None:
    missing_database = tmp_path / "missing.db"
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{missing_database.as_posix()}",
    )
    client = TestClient(create_app(PROJECT_ROOT))

    assert client.get("/api/health").json() == {
        "status": "ok",
        "application": "takealot-erp",
    }
    summary = client.get("/api/erp/summary?as_of=2026-07-20")
    assert summary.status_code == 200
    assert summary.json()["latest_metric_date"] is None
    assert not missing_database.exists()


def test_erp_rejects_unsupported_quadrant_percentile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "TAKEALOT_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'missing.db').as_posix()}",
    )
    client = TestClient(create_app(PROJECT_ROOT))

    response = client.get("/api/erp/quadrants?as_of=2026-07-20&percentile=40")

    assert response.status_code == 422
    assert response.json()["detail"] == "分位数只能是25、50或75"
