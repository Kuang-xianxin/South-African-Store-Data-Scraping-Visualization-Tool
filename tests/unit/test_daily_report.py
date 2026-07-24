from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from takealot_ops.erp.daily_report import (
    DailyReportConflictError,
    capture_daily_report,
    confirm_entry,
    confirm_ready_entries,
    create_deadline_snapshot,
    daily_report_payload,
    dismiss_stock_alert,
    export_operations_workbook,
    reminder_payload,
    save_manual_candidate,
)
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import ErpUser, OfferCurrent, SaleItem


REPORT_DATE = date(2026, 7, 24)


def _seed(engine) -> None:
    with Session(engine) as session, session.begin():
        session.add(
            ErpUser(
                id=1,
                username="operator",
                display_name="Operator",
                password_hash="unused",
                role="operator",
                active=True,
                created_at=datetime(2026, 7, 24, 1),
                updated_at=datetime(2026, 7, 24, 1),
            )
        )
        session.add_all(
            [
                OfferCurrent(
                    offer_id="offer-a",
                    sku="9900000000001",
                    title="Product A",
                    captured_at=datetime(2026, 7, 24, 2, tzinfo=UTC),
                    page_views_30_days=50,
                    takealot_available_stock=9,
                ),
                OfferCurrent(
                    offer_id="offer-b",
                    sku="9900000000002",
                    title="Product B",
                    captured_at=datetime(2026, 7, 24, 2, tzinfo=UTC),
                    page_views_30_days=20,
                    takealot_available_stock=4,
                ),
            ]
        )
        session.add(
            SaleItem(
                order_item_id="sale-a",
                order_date=datetime(2026, 7, 24, 1, tzinfo=UTC),
                sales_day=REPORT_DATE,
                offer_id="offer-a",
                sku="9900000000001",
                quantity=1,
                raw_payload={},
            )
        )


def _engine():
    engine = create_engine("sqlite://")
    create_schema(engine)
    _seed(engine)
    return engine


def test_capture_keeps_morning_and_evening_versions_and_requires_review() -> None:
    engine = _engine()
    capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="morning",
        captured_at=datetime(2026, 7, 24, 2, 5, tzinfo=UTC),
    )
    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").takealot_available_stock = 7
        session.get(SaleItem, "sale-a").quantity = 2
    capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="evening",
        captured_at=datetime(2026, 7, 24, 10, tzinfo=UTC),
    )

    payload = daily_report_payload(engine, REPORT_DATE)
    product = next(row for row in payload["items"] if row["offer_id"] == "offer-a")
    assert product["morning"]["ordered_units"] == 1
    assert product["morning"]["platform_stock"] == 9
    assert product["evening"]["ordered_units"] == 2
    assert product["evening"]["platform_stock"] == 7
    assert product["status"] == "needs_review"
    assert set(product["differences"]) == {"ordered_units", "platform_stock"}
    unchanged = next(row for row in payload["items"] if row["offer_id"] == "offer-b")
    assert unchanged["status"] == "ready"


def test_manual_candidate_and_confirm_are_separate_audited_states() -> None:
    engine = _engine()
    for slot, hour in (("morning", 2), ("evening", 10)):
        capture_daily_report(
            engine,
            business_date=REPORT_DATE,
            slot=slot,
            captured_at=datetime(2026, 7, 24, hour, tzinfo=UTC),
        )
    save_manual_candidate(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        values={"ordered_units": 3},
        reason="platform_delay",
        note="平台订单延迟，人工核对为3件",
        user_id=1,
    )
    before = daily_report_payload(engine, REPORT_DATE)
    product = next(row for row in before["items"] if row["offer_id"] == "offer-a")
    assert product["status"] == "needs_review"
    assert product["manual"]["ordered_units"] == 3
    assert product["current"]["ordered_units"] == 1

    confirm_entry(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        source="manual",
        note="采用人工核对订单，库存沿用晚间值",
        user_id=1,
    )
    after = daily_report_payload(engine, REPORT_DATE)
    product = next(row for row in after["items"] if row["offer_id"] == "offer-a")
    assert product["status"] == "confirmed"
    assert product["final"]["ordered_units"] == 3
    assert product["final"]["platform_stock"] == 9


def test_export_is_blocked_until_every_entry_is_confirmed(tmp_path: Path) -> None:
    engine = _engine()
    for slot, hour in (("morning", 2), ("evening", 10)):
        capture_daily_report(
            engine,
            business_date=REPORT_DATE,
            slot=slot,
            captured_at=datetime(2026, 7, 24, hour, tzinfo=UTC),
        )
    output = tmp_path / "daily.xlsx"
    with pytest.raises(DailyReportConflictError, match="未合并"):
        export_operations_workbook(
            engine,
            business_date=REPORT_DATE,
            destination=output,
        )

    assert (
        confirm_ready_entries(
            engine,
            business_date=REPORT_DATE,
            note="早晚数据一致，批量确认",
            user_id=1,
        )
        == 2
    )
    export_operations_workbook(
        engine,
        business_date=REPORT_DATE,
        destination=output,
    )
    workbook = load_workbook(output)
    try:
        assert workbook.sheetnames == ["运营日报"]
        assert workbook["运营日报"]["B3"].value == "当天订单数"
        assert workbook["运营日报"]["C3"].fill.fgColor.rgb == "00FCE4D6"
    finally:
        workbook.close()


def test_export_does_not_create_an_empty_workbook(tmp_path: Path) -> None:
    engine = create_engine("sqlite://")
    create_schema(engine)
    with pytest.raises(DailyReportConflictError, match="尚无已确认"):
        export_operations_workbook(
            engine,
            business_date=REPORT_DATE,
            destination=tmp_path / "empty.xlsx",
        )


def test_deadline_snapshot_and_stock_alert_remain_persistent() -> None:
    engine = _engine()
    capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="morning",
        captured_at=datetime(2026, 7, 24, 2, 5, tzinfo=UTC),
    )
    assert reminder_payload(engine, REPORT_DATE)["count"] == 0
    assert (
        create_deadline_snapshot(
            engine,
            business_date=REPORT_DATE,
            snapped_at=datetime(2026, 7, 24, 10, 30, tzinfo=UTC),
        )
        == 2
    )
    reminders = reminder_payload(engine, REPORT_DATE)
    assert reminders["count"] == 2
    assert reminders["dates"] == [
        {"business_date": "2026-07-24", "unresolved_count": 2}
    ]

    dismiss_stock_alert(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        note="今日补货2件，库存差异合理",
        user_id=1,
    )
    payload = daily_report_payload(engine, REPORT_DATE)
    product = next(row for row in payload["items"] if row["offer_id"] == "offer-a")
    assert product["stock_check"]["dismissed"] is True
