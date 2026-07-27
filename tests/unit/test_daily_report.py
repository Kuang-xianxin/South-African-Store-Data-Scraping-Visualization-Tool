from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from openpyxl import load_workbook
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from takealot_ops.erp.daily_report import (
    DailyReportConflictError,
    backfill_stock_continuity_reviews,
    capture_daily_report,
    confirm_entry,
    create_deadline_snapshot,
    daily_report_payload,
    delete_operator_note,
    dismiss_stock_alert,
    export_operations_workbook,
    operations_business_date,
    record_daily_report_failure,
    reminder_payload,
    revert_confirmation,
    save_manual_candidate,
    save_operator_note,
    update_operator_note,
)
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import (
    DailyReportAudit,
    DailyReportResolution,
    ErpUser,
    OfferCurrent,
    SaleItem,
)


REPORT_DATE = date(2026, 7, 24)


def test_capture_business_date_uses_beijing_ten_to_ten_cycle() -> None:
    assert operations_business_date(
        datetime(2026, 7, 25, 2, 5, tzinfo=UTC)
    ) == date(2026, 7, 24)
    assert operations_business_date(
        datetime(2026, 7, 25, 10, 0, tzinfo=UTC)
    ) == date(2026, 7, 24)
    assert operations_business_date(
        datetime(2026, 7, 26, 1, 59, tzinfo=UTC)
    ) == date(2026, 7, 24)
    assert operations_business_date(
        datetime(2026, 7, 26, 2, 0, tzinfo=UTC)
    ) == date(2026, 7, 25)


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
    assert product["review_issues"] == [
        {
            "type": "capture_difference",
            "fields": product["differences"],
        }
    ]
    assert product["stock_context"] is None
    unchanged = next(row for row in payload["items"] if row["offer_id"] == "offer-b")
    assert unchanged["status"] == "ready"


def test_every_manual_refresh_in_the_ten_to_ten_cycle_is_compared() -> None:
    engine = _engine()
    capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="morning",
        captured_at=datetime(2026, 7, 25, 2, 5, tzinfo=UTC),
    )
    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").takealot_available_stock = 7
        session.get(SaleItem, "sale-a").quantity = 2
    capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="manual",
        captured_at=datetime(2026, 7, 25, 6, 0, tzinfo=UTC),
    )
    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").takealot_available_stock = 9
        session.get(SaleItem, "sale-a").quantity = 1
    capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="manual",
        captured_at=datetime(2026, 7, 25, 7, 0, tzinfo=UTC),
    )

    payload = daily_report_payload(engine, REPORT_DATE)
    product = next(row for row in payload["items"] if row["offer_id"] == "offer-a")

    assert product["status"] == "needs_review"
    assert set(product["differences"]) == {"ordered_units", "platform_stock"}
    assert product["current"]["ordered_units"] == 1
    assert product["current"]["platform_stock"] == 9
    assert [row["slot"] for row in product["capture_versions"]] == [
        "morning",
        "manual",
        "manual",
    ]
    assert [issue["type"] for issue in product["review_issues"]] == [
        "capture_difference"
    ]
    assert len([run for run in payload["runs"] if run["slot"] == "manual"]) == 2


def test_operator_notes_support_create_update_delete_and_keep_audit_history() -> None:
    engine = _engine()
    capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="morning",
        captured_at=datetime(2026, 7, 24, 2, 5, tzinfo=UTC),
    )
    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").takealot_available_stock = 7
    capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="evening",
        captured_at=datetime(2026, 7, 24, 10, tzinfo=UTC),
    )

    save_operator_note(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        note="先核对晚间库存来源",
        issue_type="capture_difference",
        user_id=1,
    )
    save_operator_note(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        note="运营补充：等待仓库盘点结果",
        issue_type="general",
        user_id=1,
    )

    initial = next(
        row
        for row in daily_report_payload(engine, REPORT_DATE)["items"]
        if row["offer_id"] == "offer-a"
    )
    first_note_id = initial["operator_notes"][0]["id"]
    second_note_id = initial["operator_notes"][1]["id"]
    update_operator_note(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        note_id=first_note_id,
        note="已核对晚间库存来源",
        issue_type="stock_continuity",
        user_id=1,
    )
    delete_operator_note(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        note_id=second_note_id,
        user_id=1,
    )

    product = next(
        row
        for row in daily_report_payload(engine, REPORT_DATE)["items"]
        if row["offer_id"] == "offer-a"
    )
    assert product["status"] == "needs_review"
    assert product["operator_note"] == "已核对晚间库存来源"
    assert [
        (
            note["id"],
            note["issue_type"],
            note["note"],
            note["user_name"],
            note["updated_by"],
        )
        for note in product["operator_notes"]
    ] == [
        (
            first_note_id,
            "stock_continuity",
            "已核对晚间库存来源",
            "Operator",
            "Operator",
        ),
    ]
    with Session(engine) as session:
        actions = list(
            session.scalars(
                select(DailyReportAudit.action)
                .where(DailyReportAudit.offer_id == "offer-a")
                .order_by(DailyReportAudit.id)
            )
        )
    assert actions[-4:] == [
        "operator_note",
        "operator_note",
        "operator_note_updated",
        "operator_note_deleted",
    ]


def test_confirmed_entry_reopens_only_for_new_order_or_stock_difference() -> None:
    engine = _engine()
    for slot, hour in (("morning", 2), ("evening", 10)):
        capture_daily_report(
            engine,
            business_date=REPORT_DATE,
            slot=slot,
            captured_at=datetime(2026, 7, 25, hour, tzinfo=UTC),
        )
    confirm_entry(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        source="latest",
        note="库存和订单一致，采用本周期最新值",
        user_id=1,
    )
    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").page_views_30_days = 99
    capture = capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="manual",
        captured_at=datetime(2026, 7, 25, 11, 0, tzinfo=UTC),
    )

    product = next(
        row
        for row in daily_report_payload(engine, REPORT_DATE)["items"]
        if row["offer_id"] == "offer-a"
    )
    assert capture.reopened_count == 0
    assert product["status"] == "confirmed"
    assert product["differences"] == []


def test_page_view_change_is_kept_but_does_not_require_merge() -> None:
    engine = _engine()
    capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="morning",
        captured_at=datetime(2026, 7, 24, 2, 5, tzinfo=UTC),
    )
    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").page_views_30_days = 58
    capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="evening",
        captured_at=datetime(2026, 7, 24, 10, tzinfo=UTC),
    )

    payload = daily_report_payload(engine, REPORT_DATE)
    product = next(row for row in payload["items"] if row["offer_id"] == "offer-a")
    assert product["morning"]["page_views_30_days"] == 50
    assert product["evening"]["page_views_30_days"] == 58
    assert product["current"]["page_views_30_days"] == 58
    assert product["status"] == "ready"
    assert product["differences"] == []
    assert reminder_payload(engine, REPORT_DATE)["count"] == 0


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

    save_manual_candidate(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        values={"ordered_units": 4, "platform_stock": 8},
        reason="stock_adjustment",
        note="第二次核对后改为订单4、库存8",
        user_id=1,
    )
    revised = daily_report_payload(engine, REPORT_DATE)
    product = next(row for row in revised["items"] if row["offer_id"] == "offer-a")
    assert product["manual"]["ordered_units"] == 4
    assert product["manual"]["platform_stock"] == 8
    assert product["manual_reason"] == "stock_adjustment"
    assert product["manual_note"] == "第二次核对后改为订单4、库存8"
    with Session(engine) as session:
        edits = list(
            session.scalars(
                select(DailyReportAudit)
                .where(
                    DailyReportAudit.offer_id == "offer-a",
                    DailyReportAudit.action == "manual_candidate",
                )
                .order_by(DailyReportAudit.id)
            )
        )
    assert len(edits) == 2
    assert edits[1].payload["before"]["ordered_units"] == 3
    assert edits[1].payload["after"]["ordered_units"] == 4
    assert edits[1].payload["after"]["platform_stock"] == 8

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
    assert product["final"]["ordered_units"] == 4
    assert product["final"]["platform_stock"] == 8


def test_confirmation_can_be_reverted_and_reconfirmed_with_full_audit() -> None:
    engine = _engine()
    for slot, hour in (("morning", 2), ("evening", 10)):
        capture_daily_report(
            engine,
            business_date=REPORT_DATE,
            slot=slot,
            captured_at=datetime(2026, 7, 24, hour, tzinfo=UTC),
        )
    confirm_entry(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        source="latest",
        note="首次采用本周期最新值",
        user_id=1,
    )

    revert_confirmation(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        note="复核后发现选错来源，需要重新确认",
        user_id=1,
    )
    reopened = next(
        item
        for item in daily_report_payload(engine, REPORT_DATE)["items"]
        if item["offer_id"] == "offer-a"
    )
    assert reopened["status"] == "needs_review"
    assert reopened["confirmation_baseline"] is None
    assert reopened["review_issues"] == [
        {"type": "confirmation_reverted", "fields": []}
    ]
    assert reopened["confirmation_revert"]["revert_note"] == (
        "复核后发现选错来源，需要重新确认"
    )
    assert reopened["confirmation_revert"]["previous_confirmation"]["source"] == (
        "latest"
    )
    assert reopened["confirmation_revert"]["previous_confirmation"]["values"][
        "platform_stock"
    ] == 9

    confirm_entry(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        source="morning",
        note="重新核对后采用早间值",
        user_id=1,
    )
    reconfirmed = next(
        item
        for item in daily_report_payload(engine, REPORT_DATE)["items"]
        if item["offer_id"] == "offer-a"
    )
    assert reconfirmed["status"] == "confirmed"
    assert reconfirmed["confirmation_revert"] is None
    with Session(engine) as session:
        actions = list(
            session.scalars(
                select(DailyReportAudit.action)
                .where(DailyReportAudit.offer_id == "offer-a")
                .order_by(DailyReportAudit.id)
            )
        )
    assert actions[-3:] == ["confirm", "confirmation_reverted", "confirm"]

    with pytest.raises(DailyReportConflictError, match="没有可撤销"):
        revert_confirmation(
            engine,
            business_date=REPORT_DATE,
            offer_id="offer-b",
            note="不存在确认记录",
            user_id=1,
        )


def test_revert_pauses_following_continuity_until_reconfirmation() -> None:
    engine = _engine()
    for slot, hour in (("morning", 2), ("evening", 10)):
        capture_daily_report(
            engine,
            business_date=REPORT_DATE,
            slot=slot,
            captured_at=datetime(2026, 7, 24, hour, tzinfo=UTC),
        )
    confirm_entry(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        source="latest",
        note="确认首日库存9",
        user_id=1,
    )

    next_date = REPORT_DATE + timedelta(days=1)
    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").takealot_available_stock = 7
        session.add(
            SaleItem(
                order_item_id="sale-a-after-revert",
                order_date=datetime(2026, 7, 25, 1, tzinfo=UTC),
                sales_day=next_date,
                offer_id="offer-a",
                sku="9900000000001",
                quantity=1,
                raw_payload={},
            )
        )
    for slot, hour in (("morning", 2), ("evening", 10)):
        capture_daily_report(
            engine,
            business_date=next_date,
            slot=slot,
            captured_at=datetime(2026, 7, 25, hour, tzinfo=UTC),
        )
    before = next(
        item
        for item in daily_report_payload(engine, next_date)["pending_actions"]
        if item["business_date"] == next_date.isoformat()
        and item["offer_id"] == "offer-a"
    )
    assert before["review_issues"][0]["type"] == "stock_continuity"

    revert_confirmation(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        note="首日确认来源需要重新核对",
        user_id=1,
    )
    paused = next(
        item
        for item in daily_report_payload(engine, next_date)["items"]
        if item["offer_id"] == "offer-a"
    )
    assert paused["review_issues"] == []
    assert paused["stock_check"]["mismatch"] is False
    assert paused["stock_check"]["deferred_reason"] == (
        "前一日报日的人工确认已撤销，重新确认正确值后再计算库存连续性"
    )
    assert not any(
        item["business_date"] == next_date.isoformat()
        and item["offer_id"] == "offer-a"
        for item in daily_report_payload(engine, next_date)["pending_actions"]
    )

    confirm_entry(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        source="latest",
        note="重新确认首日库存9",
        user_id=1,
    )
    resumed = next(
        item
        for item in daily_report_payload(engine, next_date)["pending_actions"]
        if item["business_date"] == next_date.isoformat()
        and item["offer_id"] == "offer-a"
    )
    assert resumed["review_issues"][0]["type"] == "stock_continuity"
    assert resumed["stock_check"]["expected_stock"] == 8
    assert resumed["stock_check"]["actual_stock"] == 7
    assert resumed["confirmation_trigger"]["confirmation_note"] == (
        "重新确认首日库存9"
    )


def test_export_is_blocked_until_every_entry_is_confirmed(tmp_path: Path) -> None:
    engine = _engine()
    capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="morning",
        captured_at=datetime(2026, 7, 24, 2, tzinfo=UTC),
    )
    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").takealot_available_stock = 7
    capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="evening",
        captured_at=datetime(2026, 7, 24, 10, tzinfo=UTC),
    )
    output = tmp_path / "daily.xlsx"
    with pytest.raises(DailyReportConflictError, match="未合并"):
        export_operations_workbook(
            engine,
            business_date=REPORT_DATE,
            destination=output,
        )

    confirm_entry(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        source="evening",
        note="采用晚间库存值",
        user_id=1,
    )
    save_operator_note(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        note="平台临时调仓",
        issue_type="stock_continuity",
        user_id=1,
    )
    export_operations_workbook(
        engine,
        business_date=REPORT_DATE,
        destination=output,
    )
    workbook = load_workbook(output)
    try:
        assert workbook.sheetnames == ["运营日报", "漏爬说明"]
        report_sheet = workbook["运营日报"]
        assert report_sheet["A1"].value == "指标"
        assert report_sheet["A2"].value == "近30天浏览量"
        assert report_sheet["A3"].value == "当天订单数"
        assert report_sheet["C3"].fill.fgColor.rgb == "00FCE4D6"
        assert report_sheet["A4"].value == "平台库存数量"
        assert report_sheet["A5"].value == "备注"
        assert report_sheet["C5"].value == (
            "（确认：采用晚间库存值） （库存：平台临时调仓）"
        )
        assert "当天访客数" not in {
            report_sheet.cell(row=row, column=1).value
            for row in range(1, report_sheet.max_row + 1)
        }
    finally:
        workbook.close()


def test_export_does_not_create_an_empty_workbook(tmp_path: Path) -> None:
    engine = create_engine("sqlite://")
    create_schema(engine)
    with pytest.raises(DailyReportConflictError, match="尚无可导出"):
        export_operations_workbook(
            engine,
            business_date=REPORT_DATE,
            destination=tmp_path / "empty.xlsx",
        )


def test_deadline_treats_missing_evening_capture_as_non_blocking() -> None:
    engine = _engine()
    capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="morning",
        captured_at=datetime(2026, 7, 24, 2, 5, tzinfo=UTC),
    )
    assert reminder_payload(engine, REPORT_DATE)["count"] == 0
    assert create_deadline_snapshot(
        engine,
        business_date=REPORT_DATE,
        snapped_at=datetime(2026, 7, 24, 10, 30, tzinfo=UTC),
    ) == 0
    payload = daily_report_payload(engine, REPORT_DATE)
    assert payload["counts"]["missing_capture"] == 2
    assert payload["counts"]["needs_review"] == 0
    assert backfill_stock_continuity_reviews(engine, through=REPORT_DATE) == 0
    assert all(
        item["status"] == "missing_capture"
        for item in daily_report_payload(engine, REPORT_DATE)["items"]
    )
    assert reminder_payload(engine, REPORT_DATE) == {
        "count": 0,
        "dates": [],
    }


def test_failed_capture_reason_is_reported_and_does_not_require_merge(
    tmp_path: Path,
) -> None:
    engine = _engine()
    capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="morning",
        captured_at=datetime(2026, 7, 24, 2, 5, tzinfo=UTC),
    )
    record_daily_report_failure(
        engine,
        business_date=REPORT_DATE,
        slot="evening",
        captured_at=datetime(2026, 7, 24, 10, tzinfo=UTC),
        reason="AuthenticationError: Takealot 登录状态失效",
        attempts=[
            {
                "attempt": 1,
                "strategy": "标准接口",
                "status": "failed",
                "reason": "Offers 失败：AuthenticationError HTTP 403",
            },
            {
                "attempt": 2,
                "strategy": "直连备用",
                "status": "failed",
                "reason": "Offers 失败：AuthenticationError HTTP 403",
            },
        ],
    )
    payload = daily_report_payload(engine, REPORT_DATE)
    assert payload["capture_status"]["evening"]["status"] == "failed"
    assert "登录状态失效" in payload["capture_status"]["evening"]["reason"]
    assert payload["capture_status"]["evening"]["attempt_count"] == 2
    assert payload["capture_status"]["evening"]["attempts"][1]["strategy"] == "直连备用"
    assert payload["counts"]["missing_capture"] == 2
    assert payload["counts"]["needs_review"] == 0
    assert all(item["status"] == "missing_capture" for item in payload["items"])

    output = export_operations_workbook(
        engine,
        business_date=REPORT_DATE,
        destination=tmp_path / "missing-capture.xlsx",
    )
    workbook = load_workbook(output)
    try:
        issue_sheet = workbook["漏爬说明"]
        assert issue_sheet["B2"].value == "晚间"
        assert "登录状态失效" in issue_sheet["E2"].value
    finally:
        workbook.close()


def test_null_field_is_missing_data_not_a_conflict() -> None:
    engine = _engine()
    capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="morning",
        captured_at=datetime(2026, 7, 24, 2, 5, tzinfo=UTC),
    )
    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").page_views_30_days = None
    capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="evening",
        captured_at=datetime(2026, 7, 24, 10, tzinfo=UTC),
    )
    payload = daily_report_payload(engine, REPORT_DATE)
    product = next(row for row in payload["items"] if row["offer_id"] == "offer-a")
    assert product["status"] == "missing_capture"
    assert product["differences"] == []
    assert product["missing_fields"] == ["page_views_30_days"]
    assert product["current"]["page_views_30_days"] == 50


def test_payload_includes_recent_dates_for_vertical_comparison() -> None:
    engine = _engine()
    for slot, hour in (("morning", 2), ("evening", 10)):
        capture_daily_report(
            engine,
            business_date=REPORT_DATE,
            slot=slot,
            captured_at=datetime(2026, 7, 24, hour, tzinfo=UTC),
        )
    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").page_views_30_days = 58
        session.get(OfferCurrent, "offer-a").takealot_available_stock = 8
    next_date = REPORT_DATE.replace(day=25)
    for slot, hour in (("morning", 2), ("evening", 10)):
        capture_daily_report(
            engine,
            business_date=next_date,
            slot=slot,
            captured_at=datetime(2026, 7, 25, hour, tzinfo=UTC),
        )

    payload = daily_report_payload(engine, next_date)
    history = payload["comparison_history"]
    assert [row["business_date"] for row in history] == [
        REPORT_DATE.isoformat(),
        next_date.isoformat(),
    ]
    first_day = next(
        row for row in history[0]["items"] if row["offer_id"] == "offer-a"
    )
    second_day = next(
        row for row in history[1]["items"] if row["offer_id"] == "offer-a"
    )
    assert first_day["current"]["page_views_30_days"] == 50
    assert second_day["current"]["page_views_30_days"] == 58
    assert second_day["current"]["platform_stock"] == 8


def test_vertical_comparison_keeps_latest_thirty_data_dates() -> None:
    engine = _engine()
    for offset in range(31):
        report_date = REPORT_DATE + timedelta(days=offset)
        for slot, hour in (("morning", 2), ("evening", 10)):
            capture_daily_report(
                engine,
                business_date=report_date,
                slot=slot,
                captured_at=datetime(
                    report_date.year,
                    report_date.month,
                    report_date.day,
                    hour,
                    tzinfo=UTC,
                ),
            )

    through = REPORT_DATE + timedelta(days=30)
    history = daily_report_payload(engine, through)["comparison_history"]

    assert len(history) == 30
    assert history[0]["business_date"] == (REPORT_DATE + timedelta(days=1)).isoformat()
    assert history[-1]["business_date"] == through.isoformat()


def test_stock_continuity_mismatch_is_a_cross_date_pending_action(
    tmp_path: Path,
) -> None:
    engine = _engine()
    for slot, hour in (("morning", 2), ("evening", 10)):
        capture_daily_report(
            engine,
            business_date=REPORT_DATE,
            slot=slot,
            captured_at=datetime(2026, 7, 24, hour, tzinfo=UTC),
        )
    next_date = REPORT_DATE.replace(day=25)
    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").takealot_available_stock = 8
    for slot, hour in (("morning", 2), ("evening", 10)):
        capture_daily_report(
            engine,
            business_date=next_date,
            slot=slot,
            captured_at=datetime(2026, 7, 25, hour, tzinfo=UTC),
        )

    payload = daily_report_payload(engine, REPORT_DATE.replace(day=26))
    pending = next(
        item
        for item in payload["pending_actions"]
        if item["business_date"] == next_date.isoformat()
        and item["offer_id"] == "offer-a"
    )
    assert pending["status"] == "needs_review"
    assert pending["differences"] == []
    assert pending["review_issues"] == [
        {
            "type": "stock_continuity",
            "fields": ["ordered_units", "platform_stock"],
        }
    ]
    assert pending["stock_context"] == {
        "business_date": REPORT_DATE.isoformat(),
        "stock": 9,
        "source": "latest_capture",
        "source_label": "前一日报日未确认，暂用最后一次成功拉取库存",
        "selected_source": None,
        "confirmed_by": None,
        "confirmed_at": None,
        "confirm_note": None,
        "capture_label": "18:00定时（07-24 18:00）",
        "continuity_ready": True,
        "version_differences": [],
    }
    assert pending["stock_check"] == {
        "previous_stock": 9,
        "expected_stock": 9,
        "actual_stock": 8,
        "mismatch": True,
        "dismissed": False,
        "note": None,
        "deferred_reason": None,
    }
    assert reminder_payload(engine, REPORT_DATE.replace(day=26))["count"] == 1
    with pytest.raises(DailyReportConflictError, match="未合并"):
        export_operations_workbook(
            engine,
            business_date=next_date,
            destination=tmp_path / "blocked.xlsx",
        )

    dismiss_stock_alert(
        engine,
        business_date=next_date,
        offer_id="offer-a",
        note="库存包含非订单调整，人工确认采用晚间值",
        user_id=1,
    )
    resolved = daily_report_payload(engine, REPORT_DATE.replace(day=26))
    assert not any(
        item["business_date"] == next_date.isoformat()
        and item["offer_id"] == "offer-a"
        for item in resolved["pending_actions"]
    )
    item = next(
        row
        for row in daily_report_payload(engine, next_date)["items"]
        if row["offer_id"] == "offer-a"
    )
    assert item["status"] == "ready"
    assert item["stock_check"]["dismissed"] is True
    assert item["stock_check"]["note"] == "库存包含非订单调整，人工确认采用晚间值"


def test_continuity_waits_until_previous_day_version_difference_is_confirmed() -> None:
    engine = _engine()
    capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="morning",
        captured_at=datetime(2026, 7, 24, 2, tzinfo=UTC),
    )
    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").takealot_available_stock = 10
    capture_daily_report(
        engine,
        business_date=REPORT_DATE,
        slot="evening",
        captured_at=datetime(2026, 7, 24, 10, tzinfo=UTC),
    )

    next_date = REPORT_DATE + timedelta(days=1)
    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").takealot_available_stock = 8
        session.add(
            SaleItem(
                order_item_id="sale-a-next",
                order_date=datetime(2026, 7, 25, 1, tzinfo=UTC),
                sales_day=next_date,
                offer_id="offer-a",
                sku="9900000000001",
                quantity=1,
                raw_payload={},
            )
        )
    for slot, hour in (("morning", 2), ("evening", 10)):
        capture_daily_report(
            engine,
            business_date=next_date,
            slot=slot,
            captured_at=datetime(2026, 7, 25, hour, tzinfo=UTC),
        )

    before = daily_report_payload(engine, next_date)
    first_day = next(
        item
        for item in before["pending_actions"]
        if item["business_date"] == REPORT_DATE.isoformat()
        and item["offer_id"] == "offer-a"
    )
    next_day = next(
        item for item in before["items"] if item["offer_id"] == "offer-a"
    )
    assert [issue["type"] for issue in first_day["review_issues"]] == [
        "capture_difference"
    ]
    assert next_day["review_issues"] == []
    assert next_day["stock_check"]["mismatch"] is False
    assert next_day["stock_check"]["deferred_reason"] == (
        "前一日报日仍有同周期版本差异，确认正确版本后再计算库存连续性"
    )

    confirm_entry(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        source="evening",
        note="确认前一日报日晚间库存10正确",
        user_id=1,
    )

    after = daily_report_payload(engine, next_date)
    next_pending = next(
        item
        for item in after["pending_actions"]
        if item["business_date"] == next_date.isoformat()
        and item["offer_id"] == "offer-a"
    )
    assert [issue["type"] for issue in next_pending["review_issues"]] == [
        "stock_continuity"
    ]
    assert next_pending["stock_check"]["previous_stock"] == 10
    assert next_pending["stock_check"]["expected_stock"] == 9
    assert next_pending["stock_check"]["actual_stock"] == 8


def test_confirmed_version_is_the_baseline_for_a_later_capture_difference() -> None:
    engine = _engine()
    for slot, hour in (("morning", 2), ("evening", 10)):
        capture_daily_report(
            engine,
            business_date=REPORT_DATE,
            slot=slot,
            captured_at=datetime(2026, 7, 24, hour, tzinfo=UTC),
        )
    next_date = REPORT_DATE + timedelta(days=1)
    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").takealot_available_stock = 8
        session.add(
            SaleItem(
                order_item_id="sale-a-next",
                order_date=datetime(2026, 7, 25, 1, tzinfo=UTC),
                sales_day=next_date,
                offer_id="offer-a",
                sku="9900000000001",
                quantity=1,
                raw_payload={},
            )
        )
    capture_daily_report(
        engine,
        business_date=next_date,
        slot="morning",
        captured_at=datetime(2026, 7, 25, 2, tzinfo=UTC),
    )
    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").takealot_available_stock = 7
    capture_daily_report(
        engine,
        business_date=next_date,
        slot="manual",
        captured_at=datetime(2026, 7, 25, 6, tzinfo=UTC),
    )

    version_pending = next(
        item
        for item in daily_report_payload(engine, next_date)["pending_actions"]
        if item["business_date"] == next_date.isoformat()
        and item["offer_id"] == "offer-a"
    )
    assert [issue["type"] for issue in version_pending["review_issues"]] == [
        "capture_difference"
    ]
    assert version_pending["stock_check"]["mismatch"] is False

    confirm_entry(
        engine,
        business_date=next_date,
        offer_id="offer-a",
        source="latest",
        note="确认手动刷新库存7为正确值",
        user_id=1,
    )
    continuity_pending = next(
        item
        for item in daily_report_payload(engine, next_date)["pending_actions"]
        if item["business_date"] == next_date.isoformat()
        and item["offer_id"] == "offer-a"
    )
    assert [issue["type"] for issue in continuity_pending["review_issues"]] == [
        "stock_continuity"
    ]
    assert continuity_pending["current"]["platform_stock"] == 7
    assert continuity_pending["confirmation_baseline"]["values"]["platform_stock"] == 7

    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").takealot_available_stock = 6
    late_capture_time = datetime.now(UTC) + timedelta(minutes=1)
    capture_daily_report(
        engine,
        business_date=next_date,
        slot="evening",
        captured_at=late_capture_time,
    )

    reopened = next(
        item
        for item in daily_report_payload(engine, next_date)["pending_actions"]
        if item["business_date"] == next_date.isoformat()
        and item["offer_id"] == "offer-a"
    )
    assert [issue["type"] for issue in reopened["review_issues"]] == [
        "capture_difference"
    ]
    assert reopened["stock_check"]["mismatch"] is False
    assert [
        (version["kind"], version["values"]["platform_stock"])
        for version in reopened["review_versions"]
    ] == [
        ("confirmed", 7),
        ("capture", 6),
    ]

    confirm_entry(
        engine,
        business_date=next_date,
        offer_id="offer-a",
        source="latest",
        note="确认晚间库存6为新的正确值",
        user_id=1,
    )
    final_pending = next(
        item
        for item in daily_report_payload(engine, next_date)["pending_actions"]
        if item["business_date"] == next_date.isoformat()
        and item["offer_id"] == "offer-a"
    )
    assert [issue["type"] for issue in final_pending["review_issues"]] == [
        "stock_continuity"
    ]
    assert final_pending["current"]["platform_stock"] == 6
    assert final_pending["stock_check"]["expected_stock"] == 8
    assert final_pending["stock_check"]["actual_stock"] == 6


def test_manual_confirmation_reopens_following_stock_conflict_and_keeps_context() -> None:
    engine = _engine()
    second_date = REPORT_DATE + timedelta(days=1)
    third_date = REPORT_DATE + timedelta(days=2)
    for slot, hour in (("morning", 2), ("evening", 10)):
        capture_daily_report(
            engine,
            business_date=REPORT_DATE,
            slot=slot,
            captured_at=datetime(2026, 7, 24, hour, tzinfo=UTC),
        )
    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").takealot_available_stock = 8
        session.add(
            SaleItem(
                order_item_id="sale-a-day-2",
                order_date=datetime(2026, 7, 25, 1, tzinfo=UTC),
                sales_day=second_date,
                offer_id="offer-a",
                sku="9900000000001",
                quantity=1,
                raw_payload={},
            )
        )
    for slot, hour in (("morning", 2), ("evening", 10)):
        capture_daily_report(
            engine,
            business_date=second_date,
            slot=slot,
            captured_at=datetime(2026, 7, 25, hour, tzinfo=UTC),
        )
    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").takealot_available_stock = 7
        session.add(
            SaleItem(
                order_item_id="sale-a-day-3",
                order_date=datetime(2026, 7, 26, 1, tzinfo=UTC),
                sales_day=third_date,
                offer_id="offer-a",
                sku="9900000000001",
                quantity=1,
                raw_payload={},
            )
        )
    for slot, hour in (("morning", 2), ("evening", 10)):
        capture_daily_report(
            engine,
            business_date=third_date,
            slot=slot,
            captured_at=datetime(2026, 7, 26, hour, tzinfo=UTC),
        )
    assert (
        create_deadline_snapshot(
            engine,
            business_date=second_date,
            snapped_at=datetime(2026, 7, 25, 10, 30, tzinfo=UTC),
        )
        == 0
    )

    save_manual_candidate(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        values={"platform_stock": 10},
        reason="stock_adjustment",
        note="盘点后确认首日库存为10",
        user_id=1,
    )
    confirm_entry(
        engine,
        business_date=REPORT_DATE,
        offer_id="offer-a",
        source="manual",
        note="采用人工盘点库存10",
        user_id=1,
    )

    second_pending = next(
        item
        for item in daily_report_payload(engine, third_date)["pending_actions"]
        if item["business_date"] == second_date.isoformat()
        and item["offer_id"] == "offer-a"
    )
    assert second_pending["stock_check"]["mismatch"] is True
    assert reminder_payload(engine, second_date)["count"] == 1
    assert second_pending["confirmation_trigger"] == {
        "kind": "previous_confirmation",
        "message": (
            "2026-07-24 人工确认合并后，触发 2026-07-25 库存连续性冲突"
        ),
        "trigger_business_date": "2026-07-24",
        "affected_business_date": "2026-07-25",
        "confirmation_source": "manual",
        "confirmation_source_label": "人工修改值",
        "confirmed_by": "Operator",
        "confirmed_at": second_pending["confirmation_trigger"]["confirmed_at"],
        "confirmation_note": "采用人工盘点库存10",
        "previous_stock_before_confirmation": 9,
        "confirmed_previous_stock": 10,
        "current_ordered_units": 1,
        "expected_stock_before_confirmation": 8,
        "comparison_before_state": "matched",
        "expected_stock_after_confirmation": 9,
        "actual_stock": 8,
        "affected_previous_status": "ready",
        "affected_previous_final": None,
        "affected_previous_confirmed_by": None,
        "affected_previous_confirmed_at": None,
        "affected_previous_confirm_note": None,
        "affected_current_values": {
            "page_views_30_days": 50,
            "ordered_units": 1,
            "platform_stock": 8,
        },
    }

    save_manual_candidate(
        engine,
        business_date=second_date,
        offer_id="offer-a",
        values={"platform_stock": 9},
        reason="stock_adjustment",
        note="按前一日确认值修正第二日库存",
        user_id=1,
    )
    confirm_entry(
        engine,
        business_date=second_date,
        offer_id="offer-a",
        source="manual",
        note="采用修正后的第二日库存9",
        user_id=1,
    )

    pending = daily_report_payload(engine, third_date)["pending_actions"]
    assert not any(
        item["business_date"] == second_date.isoformat()
        and item["offer_id"] == "offer-a"
        for item in pending
    )
    third_pending = next(
        item
        for item in pending
        if item["business_date"] == third_date.isoformat()
        and item["offer_id"] == "offer-a"
    )
    assert third_pending["stock_check"] == {
        "previous_stock": 9,
        "expected_stock": 8,
        "actual_stock": 7,
        "mismatch": True,
        "dismissed": False,
        "note": None,
        "deferred_reason": None,
    }
    assert third_pending["confirmation_trigger"]["trigger_business_date"] == (
        second_date.isoformat()
    )
    assert third_pending["confirmation_trigger"]["affected_business_date"] == (
        third_date.isoformat()
    )
    assert third_pending["confirmation_trigger"]["previous_stock_before_confirmation"] == 8
    assert third_pending["confirmation_trigger"]["confirmed_previous_stock"] == 9
    assert reminder_payload(engine, third_date + timedelta(days=1))["count"] == 1


def test_backfill_promotes_existing_stock_mismatch_to_review() -> None:
    engine = _engine()
    for slot, hour in (("morning", 2), ("evening", 10)):
        capture_daily_report(
            engine,
            business_date=REPORT_DATE,
            slot=slot,
            captured_at=datetime(2026, 7, 24, hour, tzinfo=UTC),
        )
    next_date = REPORT_DATE.replace(day=25)
    with Session(engine) as session, session.begin():
        session.get(OfferCurrent, "offer-a").takealot_available_stock = 8
    capture_daily_report(
        engine,
        business_date=next_date,
        slot="morning",
        captured_at=datetime(2026, 7, 25, 2, tzinfo=UTC),
    )
    with Session(engine) as session, session.begin():
        resolution = session.scalar(
            select(DailyReportResolution).where(
                DailyReportResolution.business_date == next_date,
                DailyReportResolution.offer_id == "offer-a",
            )
        )
        assert resolution is not None
        resolution.status = "ready"
    capture_daily_report(
        engine,
        business_date=next_date,
        slot="evening",
        captured_at=datetime(2026, 7, 25, 10, tzinfo=UTC),
    )
    with Session(engine) as session, session.begin():
        resolution = session.scalar(
            select(DailyReportResolution).where(
                DailyReportResolution.business_date == next_date,
                DailyReportResolution.offer_id == "offer-a",
            )
        )
        assert resolution is not None
        resolution.status = "ready"

    assert backfill_stock_continuity_reviews(engine, through=next_date) == 1
    payload = daily_report_payload(engine, next_date)
    item = next(row for row in payload["items"] if row["offer_id"] == "offer-a")
    assert item["status"] == "needs_review"


def test_future_capture_slots_are_pending_not_missing() -> None:
    engine = _engine()
    future_business_date = date.today()

    payload = daily_report_payload(engine, future_business_date)

    assert payload["capture_status"]["morning"]["status"] == "pending"
    assert payload["capture_status"]["evening"]["status"] == "pending"
    assert payload["capture_issues"] == []
    assert payload["counts"]["missing_capture"] == 0
