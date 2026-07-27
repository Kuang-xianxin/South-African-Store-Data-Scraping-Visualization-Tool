from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from takealot_ops.quality import verify_quality
from takealot_ops.reporting import ReportPaths
from takealot_ops.scheduler import run_daily
from takealot_ops.settings import Settings
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import DataQualityEvent
from takealot_ops.storage.repository import Repository


FIXTURES = Path(__file__).parents[1] / "fixtures"


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value


class RecordingClient:
    def __init__(self, *, fail_after_first_offer: bool = False) -> None:
        self.fail_after_first_offer = fail_after_first_offer
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def iter_items(
        self, path: str, params: Mapping[str, Any]
    ) -> Iterator[dict[str, Any]]:
        self.calls.append((path, dict(params)))
        if path == "/offers" and self.fail_after_first_offer:
            payload = json.loads((FIXTURES / "offers_page_1.json").read_text(encoding="utf-8"))
            yield dict(payload["items"][0])
            raise RuntimeError("simulated second page failure")

    def close(self) -> None:
        return None


class FixtureSaleClient(RecordingClient):
    def iter_items(
        self, path: str, params: Mapping[str, Any]
    ) -> Iterator[dict[str, Any]]:
        self.calls.append((path, dict(params)))
        fixture = "offers_page_1.json" if path == "/offers" else "sales_page.json"
        payload = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
        yield dict(payload["items"][0])


def _settings(tmp_path: Path) -> Settings:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "anomaly_rules.yaml").write_text(
        """sales_drop:
  baseline_days: 7
  minimum_baseline_daily_quantity: 2
  drop_percentage: 50
sales_spike:
  baseline_days: 7
  minimum_daily_quantity: 4
  increase_percentage: 50
traffic_conversion:
  high_page_views_percentile: 75
  low_conversion_percentile: 25
  low_page_views_percentile: 25
  high_conversion_percentile: 75
stale_offer_snapshot_hours: 24
""",
        encoding="utf-8",
    )
    (config_dir / "sale_status_rules.yaml").write_text(
        "included: []\nexcluded: []\nunknown: []\n", encoding="utf-8"
    )
    return Settings(
        project_root=tmp_path,
        api_key="fixture-key",
        base_url="https://example.invalid/v1",
        database_url=f"sqlite:///{(tmp_path / 'data' / 'takealot.db').as_posix()}",
        request_timeout_seconds=1.0,
        dashboard_host="127.0.0.1",
        dashboard_port=8501,
    )


def _fake_reports(tmp_path: Path, report_date: date) -> ReportPaths:
    partition = tmp_path / "exports" / report_date.isoformat()
    partition.mkdir(parents=True, exist_ok=True)
    html = partition / "report.html"
    excel = partition / "report.xlsx"
    png = partition / "report.png"
    html.write_text("report", encoding="utf-8")
    excel.write_bytes(b"xlsx")
    png.write_bytes(b"png")
    return ReportPaths(html=html, excel=excel, png=png)


def test_daily_run_refreshes_seven_sast_days(tmp_path: Path, monkeypatch) -> None:
    client = RecordingClient()
    monkeypatch.setattr("takealot_ops.scheduler.TakealotClient", lambda _: client)
    monkeypatch.setattr(
        "takealot_ops.scheduler.generate_daily_reports",
        lambda _dataset, _root, report_date: _fake_reports(tmp_path, report_date),
    )

    result = run_daily(
        _settings(tmp_path), FixedClock(datetime(2026, 7, 20, 22, 30, tzinfo=UTC))
    )

    assert result.start_date == date(2026, 7, 15)
    assert result.end_date == date(2026, 7, 21)
    assert client.calls[1] == (
        "/sales",
        {
            "order_date__gte": "2026-07-15",
            "order_date__lte": "2026-07-21",
            "limit": 100,
        },
    )
    assert result.status == "success"
    assert result.report_paths is not None


def test_daily_run_can_export_the_previous_completed_business_day(
    tmp_path: Path, monkeypatch
) -> None:
    client = RecordingClient()
    published: list[date] = []
    monkeypatch.setattr("takealot_ops.scheduler.TakealotClient", lambda _: client)
    monkeypatch.setattr(
        "takealot_ops.scheduler.generate_daily_reports",
        lambda _dataset, _root, report_date: (
            published.append(report_date) or _fake_reports(tmp_path, report_date)
        ),
    )

    result = run_daily(
        _settings(tmp_path),
        FixedClock(datetime(2026, 7, 25, 2, 5, tzinfo=UTC)),
        report_date=date(2026, 7, 24),
    )

    assert result.end_date == date(2026, 7, 25)
    assert published == [date(2026, 7, 24)]
    assert result.report_paths is not None
    assert result.report_paths.html.parent.name == "2026-07-24"


def test_daily_run_does_not_publish_reports_after_incomplete_pagination(
    tmp_path: Path, monkeypatch
) -> None:
    client = RecordingClient(fail_after_first_offer=True)
    published: list[date] = []
    monkeypatch.setattr("takealot_ops.scheduler.TakealotClient", lambda _: client)
    monkeypatch.setattr(
        "takealot_ops.scheduler.generate_daily_reports",
        lambda _dataset, _root, report_date: published.append(report_date),
    )

    result = run_daily(
        _settings(tmp_path), FixedClock(datetime(2026, 7, 20, 8, tzinfo=UTC))
    )

    assert result.status == "collection_failed"
    assert result.report_paths is None
    assert published == []


def test_daily_run_checks_quality_across_the_full_refresh_window(
    tmp_path: Path, monkeypatch
) -> None:
    client = FixtureSaleClient()
    monkeypatch.setattr("takealot_ops.scheduler.TakealotClient", lambda _: client)
    monkeypatch.setattr(
        "takealot_ops.scheduler.generate_daily_reports",
        lambda _dataset, _root, report_date: _fake_reports(tmp_path, report_date),
    )

    result = run_daily(
        _settings(tmp_path), FixedClock(datetime(2026, 7, 20, 22, 30, tzinfo=UTC))
    )

    assert result.status == "quality_failed", result.error
    assert result.quality is not None
    assert result.quality.issue_count == 1
    assert result.quality.unknown_sales_status_count == 1


def test_verify_reports_unknown_sales_statuses() -> None:
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        with session.begin():
            session.add(
                DataQualityEvent(
                    event_id="unknown-status",
                    event_date=date(2026, 7, 20),
                    event_type="unknown_sale_status",
                    severity="warning",
                    offer_id="offer-1",
                    details={"sale_statuses": ["new-status"]},
                    created_at=datetime(2026, 7, 20, 8, tzinfo=UTC),
                )
            )
        result = verify_quality(Repository(session), date(2026, 7, 20))

    assert result.issue_count == 1
    assert result.unknown_sales_status_count == 1
    assert not result.passed
