from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from takealot_ops.cli import _run_protected_daily_report_collection, build_parser
from takealot_ops.collectors import CollectionResult
from takealot_ops.scheduler import DailyRunResult
from takealot_ops.settings import Settings


def test_help_lists_all_commands(capsys) -> None:
    parser = build_parser()
    parser.print_help()
    help_text = capsys.readouterr().out
    for command in (
        "collect",
        "collect-competitors",
        "export",
        "daily-run",
        "backup-local",
        "backup-verify",
        "daily-report-run",
        "daily-report-capture",
        "daily-report-deadline",
        "dashboard",
        "migrate-to-mysql",
        "verify",
    ):
        assert command in help_text


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 7, 27, 2, 5, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(
        project_root=Path("."),
        api_key="fixture-key",
        base_url="https://example.invalid/v1",
        database_url="sqlite://",
        request_timeout_seconds=1,
        dashboard_host="127.0.0.1",
        dashboard_port=8501,
    )


def test_daily_report_protection_retries_and_uses_direct_fallback() -> None:
    report_date = date(2026, 7, 26)
    trusts: list[bool] = []
    sleeps: list[float] = []

    def runner(settings, clock, *, report_date, trust_env):
        del settings, clock, report_date
        trusts.append(trust_env)
        if len(trusts) < 3:
            return DailyRunResult(
                "collection_failed",
                date(2026, 7, 20),
                date(2026, 7, 26),
                offer_result=CollectionResult(
                    f"offer-{len(trusts)}",
                    "failed",
                    {"records": 0},
                    "ApiTransportError: ConnectTimeout",
                ),
                error="ApiTransportError: ConnectTimeout",
            )
        return DailyRunResult(
            "success",
            date(2026, 7, 20),
            date(2026, 7, 26),
            offer_result=CollectionResult("offer-3", "success", {"records": 404}),
            sales_result=CollectionResult("sales-3", "success", {"records": 127}),
        )

    result, attempts = _run_protected_daily_report_collection(
        _settings(),
        FixedClock(),
        business_date=report_date,
        slot="morning",
        runner=runner,
        sleeper=sleeps.append,
    )

    assert result.status == "success"
    assert trusts == [True, True, False]
    assert sleeps == [20.0, 60.0]
    assert [row["status"] for row in attempts] == ["failed", "failed", "success"]
    assert attempts[-1]["strategy"] == "直连备用"

