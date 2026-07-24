"""Versioned operations daily report with human reconciliation and audit history."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from openpyxl import Workbook  # type: ignore[import-untyped]
from openpyxl.styles import Alignment, Font, PatternFill  # type: ignore[import-untyped]
from openpyxl.utils import get_column_letter  # type: ignore[import-untyped]
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from takealot_ops.storage.models import (
    DailyReportAudit,
    DailyReportDeadlineSnapshot,
    DailyReportObservation,
    DailyReportResolution,
    DailyReportRun,
    OfferCurrent,
    SaleItem,
)


SLOTS = frozenset({"morning", "evening"})
SOURCES = frozenset({"morning", "evening", "manual"})
MANUAL_REASONS = frozenset({"platform_delay", "stock_adjustment", "other"})
OPEN_STATUSES = frozenset({"awaiting_evening", "ready", "needs_review"})


@dataclass(frozen=True)
class ReportCaptureResult:
    run_id: str
    business_date: date
    slot: str
    product_count: int
    reopened_count: int


class DailyReportInputError(ValueError):
    """Safe validation failure for an operator action."""


class DailyReportConflictError(RuntimeError):
    """Safe state conflict for an operator action."""


def capture_daily_report(
    engine: Engine,
    *,
    business_date: date,
    slot: str,
    captured_at: datetime,
) -> ReportCaptureResult:
    """Freeze current order/stock values without overwriting earlier captures."""
    if slot not in SLOTS:
        raise DailyReportInputError("采集时段只能是 morning 或 evening")
    now = _naive_utc(captured_at)
    run_id = str(uuid4())
    reopened = 0
    with Session(engine) as session, session.begin():
        offers = list(
            session.scalars(select(OfferCurrent).order_by(OfferCurrent.offer_id))
        )
        sales: dict[str, int] = {}
        sale_rows = session.execute(
            select(
                SaleItem.offer_id,
                func.coalesce(func.sum(SaleItem.quantity), 0),
            )
            .where(
                SaleItem.sales_day == business_date,
                SaleItem.offer_id.is_not(None),
            )
            .group_by(SaleItem.offer_id)
        ).all()
        for sale_offer_id, quantity in sale_rows:
            if sale_offer_id is not None:
                sales[sale_offer_id] = int(quantity)
        run = DailyReportRun(
            run_id=run_id,
            business_date=business_date,
            slot=slot,
            captured_at=now,
            status="success",
            counts={"products": len(offers)},
            created_at=now,
        )
        session.add(run)
        session.flush()
        for offer in offers:
            platform_stock, stock_source = _platform_stock(offer)
            observation = DailyReportObservation(
                run_id=run_id,
                offer_id=offer.offer_id,
                sku=offer.sku,
                title=offer.title,
                page_views_30_days=offer.page_views_30_days,
                ordered_units=int(sales.get(offer.offer_id, 0) or 0),
                platform_stock=platform_stock,
                stock_source=stock_source,
            )
            session.add(observation)
            resolution = session.scalar(
                select(DailyReportResolution).where(
                    DailyReportResolution.business_date == business_date,
                    DailyReportResolution.offer_id == offer.offer_id,
                )
            )
            if resolution is None:
                resolution = DailyReportResolution(
                    business_date=business_date,
                    offer_id=offer.offer_id,
                    status="awaiting_evening",
                    stock_alert_dismissed=False,
                    created_at=now,
                    updated_at=now,
                )
                session.add(resolution)
            previous_status = resolution.status
            resolution.status = _status_after_capture(
                session,
                resolution=resolution,
                slot=slot,
                incoming=_value_dict(observation),
            )
            if previous_status == "confirmed" and resolution.status != "confirmed":
                reopened += 1
                session.add(
                    DailyReportAudit(
                        business_date=business_date,
                        offer_id=offer.offer_id,
                        action="system_reopened",
                        payload={"slot": slot, "run_id": run_id},
                        note="确认后平台数据发生变化，已重新进入待确认。",
                        user_id=None,
                        created_at=now,
                    )
                )
            resolution.updated_at = now
    return ReportCaptureResult(
        run_id=run_id,
        business_date=business_date,
        slot=slot,
        product_count=len(offers),
        reopened_count=reopened,
    )


def daily_report_payload(engine: Engine, business_date: date) -> dict[str, Any]:
    """Build one date's comparison table and all persistent prior reminders."""
    with Session(engine) as session:
        runs = list(
            session.scalars(
                select(DailyReportRun)
                .where(DailyReportRun.business_date == business_date)
                .order_by(DailyReportRun.captured_at)
            )
        )
        observations = _latest_observations(session, business_date)
        resolutions = {
            row.offer_id: row
            for row in session.scalars(
                select(DailyReportResolution).where(
                    DailyReportResolution.business_date == business_date
                )
            )
        }
        previous = _previous_values(session, business_date)
        offer_ids = sorted(set(observations["morning"]) | set(observations["evening"]))
        items = [
            _item_payload(
                offer_id,
                observations["morning"].get(offer_id),
                observations["evening"].get(offer_id),
                resolutions.get(offer_id),
                previous.get(offer_id),
            )
            for offer_id in offer_ids
        ]
        reminders = _reminders(session, before=business_date)
        deadline = session.get(DailyReportDeadlineSnapshot, business_date)
    counts = {
        "products": len(items),
        "with_sales": sum(
            1
            for item in items
            if int(item["current"]["ordered_units"] or 0) > 0
        ),
        "awaiting_evening": sum(item["status"] == "awaiting_evening" for item in items),
        "ready": sum(item["status"] == "ready" for item in items),
        "needs_review": sum(item["status"] == "needs_review" for item in items),
        "confirmed": sum(item["status"] == "confirmed" for item in items),
        "stock_alerts": sum(
            bool(item["stock_check"]["mismatch"])
            and not bool(item["stock_check"]["dismissed"])
            for item in items
        ),
    }
    return {
        "business_date": business_date.isoformat(),
        "runs": [
            {
                "run_id": run.run_id,
                "slot": run.slot,
                "captured_at": run.captured_at.isoformat(),
                "status": run.status,
                "counts": run.counts or {},
            }
            for run in runs
        ],
        "counts": counts,
        "items": items,
        "prior_reminders": reminders,
        "deadline_snapshot": (
            {
                "snapped_at": deadline.snapped_at.isoformat(),
                "unresolved_count": deadline.unresolved_count,
                "resolved_at": (
                    deadline.resolved_at.isoformat()
                    if deadline.resolved_at is not None
                    else None
                ),
            }
            if deadline is not None
            else None
        ),
    }


def reminder_payload(
    engine: Engine,
    current_business_date: date | None = None,
) -> dict[str, Any]:
    """Return unresolved past dates for the global start-of-work reminder."""
    today = current_business_date or datetime.now(
        ZoneInfo("Africa/Johannesburg")
    ).date()
    with Session(engine) as session:
        rows = _reminders(session, before=today)
        active_deadlines = list(
            session.scalars(
                select(DailyReportDeadlineSnapshot.business_date).where(
                    DailyReportDeadlineSnapshot.resolved_at.is_(None),
                    DailyReportDeadlineSnapshot.business_date >= today,
                )
            )
        )
        for deadline_date in active_deadlines:
            rows.extend(_reminders_for_date(session, deadline_date))
    rows = list({str(row["business_date"]): row for row in rows}.values())
    rows.sort(key=lambda row: str(row["business_date"]))
    return {
        "count": sum(int(row["unresolved_count"]) for row in rows),
        "dates": rows,
    }


def save_manual_candidate(
    engine: Engine,
    *,
    business_date: date,
    offer_id: str,
    values: Mapping[str, int | None],
    reason: str,
    note: str,
    user_id: int,
) -> None:
    if reason not in MANUAL_REASONS:
        raise DailyReportInputError("人工修改原因无效")
    clean_note = _required_note(note, "人工修改必须填写备注")
    supplied = {key: values.get(key) for key in _VALUE_KEYS if key in values}
    if not supplied:
        raise DailyReportInputError("至少填写一个人工修改值")
    for key, value in supplied.items():
        if value is not None and int(value) < 0:
            raise DailyReportInputError(f"{key} 不能小于 0")
    now = _utc_now()
    with Session(engine) as session, session.begin():
        resolution = _resolution_or_error(session, business_date, offer_id)
        before = _manual_values(resolution)
        if "page_views_30_days" in supplied:
            resolution.manual_page_views_30_days = supplied["page_views_30_days"]
        if "ordered_units" in supplied:
            resolution.manual_ordered_units = supplied["ordered_units"]
        if "platform_stock" in supplied:
            resolution.manual_platform_stock = supplied["platform_stock"]
        resolution.manual_reason = reason
        resolution.manual_note = clean_note
        resolution.manual_by = user_id
        resolution.manual_at = now
        if resolution.status == "confirmed":
            resolution.status = "needs_review"
        elif _latest_observation(session, business_date, "evening", offer_id) is None:
            resolution.status = "awaiting_evening"
        else:
            resolution.status = "needs_review"
        resolution.updated_at = now
        _audit(
            session,
            business_date,
            offer_id,
            "manual_candidate",
            {"before": before, "after": _manual_values(resolution), "reason": reason},
            clean_note,
            user_id,
            now,
        )


def save_operator_note(
    engine: Engine,
    *,
    business_date: date,
    offer_id: str,
    note: str,
    user_id: int,
) -> None:
    clean_note = _required_note(note, "备注不能为空")
    now = _utc_now()
    with Session(engine) as session, session.begin():
        resolution = _resolution_or_error(session, business_date, offer_id)
        resolution.operator_note = clean_note
        resolution.updated_at = now
        _audit(
            session,
            business_date,
            offer_id,
            "operator_note",
            {"note": clean_note},
            clean_note,
            user_id,
            now,
        )


def confirm_entry(
    engine: Engine,
    *,
    business_date: date,
    offer_id: str,
    source: str,
    note: str,
    user_id: int,
) -> None:
    if source not in SOURCES:
        raise DailyReportInputError("最终值来源只能选择早间、晚间或人工值")
    clean_note = _required_note(note, "确认合并必须填写备注")
    now = _utc_now()
    with Session(engine) as session, session.begin():
        resolution = _resolution_or_error(session, business_date, offer_id)
        values = _source_values(session, resolution, source)
        if values is None:
            raise DailyReportConflictError("所选来源没有可用数据")
        _apply_final(resolution, values, source, clean_note, user_id, now)
        _audit(
            session,
            business_date,
            offer_id,
            "confirm",
            {"source": source, "values": values},
            clean_note,
            user_id,
            now,
        )
        _resolve_deadline_if_complete(session, business_date, now)


def confirm_ready_entries(
    engine: Engine,
    *,
    business_date: date,
    note: str,
    user_id: int,
) -> int:
    clean_note = _required_note(note, "批量确认必须填写备注")
    now = _utc_now()
    confirmed = 0
    with Session(engine) as session, session.begin():
        rows = list(
            session.scalars(
                select(DailyReportResolution).where(
                    DailyReportResolution.business_date == business_date,
                    DailyReportResolution.status == "ready",
                )
            )
        )
        for resolution in rows:
            values = _source_values(session, resolution, "evening")
            if values is None:
                continue
            _apply_final(resolution, values, "evening", clean_note, user_id, now)
            _audit(
                session,
                business_date,
                resolution.offer_id,
                "bulk_confirm",
                {"source": "evening", "values": values},
                clean_note,
                user_id,
                now,
            )
            confirmed += 1
        _resolve_deadline_if_complete(session, business_date, now)
    return confirmed


def dismiss_stock_alert(
    engine: Engine,
    *,
    business_date: date,
    offer_id: str,
    note: str,
    user_id: int,
) -> None:
    clean_note = _required_note(note, "取消库存红色标记必须填写原因")
    now = _utc_now()
    with Session(engine) as session, session.begin():
        resolution = _resolution_or_error(session, business_date, offer_id)
        resolution.stock_alert_dismissed = True
        resolution.stock_alert_note = clean_note
        resolution.stock_alert_dismissed_by = user_id
        resolution.stock_alert_dismissed_at = now
        resolution.updated_at = now
        _audit(
            session,
            business_date,
            offer_id,
            "dismiss_stock_alert",
            None,
            clean_note,
            user_id,
            now,
        )


def create_deadline_snapshot(
    engine: Engine,
    *,
    business_date: date,
    snapped_at: datetime,
) -> int:
    now = _naive_utc(snapped_at)
    with Session(engine) as session, session.begin():
        unresolved = list(
            session.scalars(
                select(DailyReportResolution).where(
                    DailyReportResolution.business_date == business_date,
                    DailyReportResolution.status != "confirmed",
                )
            )
        )
        details = [
            {"offer_id": row.offer_id, "status": row.status}
            for row in unresolved
        ]
        snapshot = session.get(DailyReportDeadlineSnapshot, business_date)
        if snapshot is None:
            snapshot = DailyReportDeadlineSnapshot(
                business_date=business_date,
                snapped_at=now,
                unresolved_count=len(unresolved),
                details=details,
                resolved_at=now if not unresolved else None,
            )
            session.add(snapshot)
        else:
            snapshot.snapped_at = now
            snapshot.unresolved_count = len(unresolved)
            snapshot.details = details
            snapshot.resolved_at = now if not unresolved else None
        _audit(
            session,
            business_date,
            None,
            "deadline_snapshot",
            {"unresolved_count": len(unresolved), "details": details},
            None,
            None,
            now,
        )
    return len(unresolved)


def export_operations_workbook(
    engine: Engine,
    *,
    business_date: date,
    destination: Path,
) -> Path:
    """Export confirmed history in the reference workbook's product-column layout."""
    with Session(engine) as session:
        unresolved = list(
            session.scalars(
                select(DailyReportResolution).where(
                    DailyReportResolution.business_date <= business_date,
                    DailyReportResolution.status != "confirmed",
                )
            )
        )
        if unresolved:
            locations = ", ".join(
                f"{row.business_date.isoformat()} / {row.offer_id}"
                for row in unresolved[:8]
            )
            suffix = " 等" if len(unresolved) > 8 else ""
            raise DailyReportConflictError(
                f"仍有 {len(unresolved)} 个数据未合并：{locations}{suffix}"
            )
        dates = list(
            session.scalars(
                select(DailyReportResolution.business_date)
                .where(
                    DailyReportResolution.business_date <= business_date,
                    DailyReportResolution.status == "confirmed",
                )
                .distinct()
                .order_by(DailyReportResolution.business_date)
            )
        )
        if not dates:
            raise DailyReportConflictError("尚无已确认的运营日报数据可导出")
        resolutions = list(
            session.scalars(
                select(DailyReportResolution)
                .where(
                    DailyReportResolution.business_date.in_(dates),
                    DailyReportResolution.status == "confirmed",
                )
                .order_by(
                    DailyReportResolution.business_date,
                    DailyReportResolution.offer_id,
                )
            )
        )
        identities = _identity_map(session, resolutions)
        previous = _all_previous_confirmed_stock(resolutions)
    destination.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "运营日报"
    offer_ids = sorted({row.offer_id for row in resolutions})
    by_key = {(row.business_date, row.offer_id): row for row in resolutions}
    sheet.cell(1, 1, "日期")
    sheet.cell(1, 2, "指标")
    for column, offer_id in enumerate(offer_ids, start=3):
        identity = identities.get(offer_id, {})
        title = str(identity.get("title") or offer_id)
        sku = str(identity.get("sku") or offer_id)
        sheet.cell(1, column, f"{title}\n{sku}")
    header_fill = PatternFill("solid", fgColor="FFF2CC")
    date_fill = PatternFill("solid", fgColor="DDEBF7")
    sales_fill = PatternFill("solid", fgColor="FCE4D6")
    alert_fill = PatternFill("solid", fgColor="FFC7CE")
    row_number = 2
    for report_date in dates:
        labels = (
            "平台近30天浏览量",
            "当天订单数",
            "平台仓可售库存",
            "运营备注",
        )
        for offset, label in enumerate(labels):
            sheet.cell(row_number + offset, 1, report_date)
            sheet.cell(row_number + offset, 2, label)
            sheet.cell(row_number + offset, 1).fill = date_fill
        for column, offer_id in enumerate(offer_ids, start=3):
            resolution = by_key.get((report_date, offer_id))
            if resolution is None:
                continue
            sheet.cell(row_number, column, resolution.final_page_views_30_days)
            orders = resolution.final_ordered_units
            stock = resolution.final_platform_stock
            sheet.cell(row_number + 1, column, orders)
            sheet.cell(row_number + 2, column, stock)
            note = "；".join(
                part
                for part in (
                    resolution.operator_note,
                    resolution.manual_note,
                    resolution.confirm_note,
                    resolution.stock_alert_note,
                )
                if part
            )
            sheet.cell(row_number + 3, column, note or None)
            if int(orders or 0) > 0:
                sheet.cell(row_number + 1, column).fill = sales_fill
            previous_stock = previous.get((report_date, offer_id))
            if (
                previous_stock is not None
                and stock is not None
                and previous_stock - int(orders or 0) != stock
                and not resolution.stock_alert_dismissed
            ):
                sheet.cell(row_number + 2, column).fill = alert_fill
        row_number += 4
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for row in sheet.iter_rows():
        for cell in row:
            if cell.value is not None:
                cell.alignment = Alignment(
                    horizontal="center", vertical="center", wrap_text=True
                )
    sheet.freeze_panes = "C2"
    sheet.column_dimensions["A"].width = 13
    sheet.column_dimensions["B"].width = 20
    for column in range(3, len(offer_ids) + 3):
        sheet.column_dimensions[get_column_letter(column)].width = 22
    sheet.row_dimensions[1].height = 54
    workbook.save(destination)
    workbook.close()
    return destination


def unresolved_locations(engine: Engine, through: date) -> list[dict[str, Any]]:
    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(DailyReportResolution)
                .where(
                    DailyReportResolution.business_date <= through,
                    DailyReportResolution.status != "confirmed",
                )
                .order_by(
                    DailyReportResolution.business_date,
                    DailyReportResolution.offer_id,
                )
            )
        )
        identities = _identity_map(session, rows)
    return [
        {
            "business_date": row.business_date.isoformat(),
            "offer_id": row.offer_id,
            "sku": identities.get(row.offer_id, {}).get("sku"),
            "title": identities.get(row.offer_id, {}).get("title"),
            "status": row.status,
        }
        for row in rows
    ]


_VALUE_KEYS = ("page_views_30_days", "ordered_units", "platform_stock")


def _platform_stock(offer: OfferCurrent) -> tuple[int | None, str | None]:
    if offer.takealot_available_stock is not None:
        return int(offer.takealot_available_stock), "takealot_available_stock"
    if offer.total_stock is not None:
        return int(offer.total_stock), "total_stock_fallback"
    return None, None


def _status_after_capture(
    session: Session,
    *,
    resolution: DailyReportResolution,
    slot: str,
    incoming: dict[str, int | None],
) -> str:
    if slot == "morning":
        return "awaiting_evening" if resolution.status != "confirmed" else "confirmed"
    if resolution.status == "confirmed":
        final = _final_values(resolution)
        if final == incoming:
            return "confirmed"
    morning = _latest_observation(
        session,
        resolution.business_date,
        "morning",
        resolution.offer_id,
    )
    if morning is None:
        return "needs_review"
    candidates = [_value_dict(morning), incoming]
    manual = _manual_values(resolution)
    if any(value is not None for value in manual.values()):
        candidates.append(manual)
    return "ready" if all(candidate == candidates[0] for candidate in candidates[1:]) else "needs_review"


def _latest_observations(
    session: Session, business_date: date
) -> dict[str, dict[str, DailyReportObservation]]:
    result: dict[str, dict[str, DailyReportObservation]] = {
        "morning": {},
        "evening": {},
    }
    for slot in SLOTS:
        run = session.scalar(
            select(DailyReportRun)
            .where(
                DailyReportRun.business_date == business_date,
                DailyReportRun.slot == slot,
                DailyReportRun.status == "success",
            )
            .order_by(DailyReportRun.captured_at.desc())
        )
        if run is None:
            continue
        result[slot] = {
            row.offer_id: row
            for row in session.scalars(
                select(DailyReportObservation).where(
                    DailyReportObservation.run_id == run.run_id
                )
            )
        }
    return result


def _latest_observation(
    session: Session,
    business_date: date,
    slot: str,
    offer_id: str,
) -> DailyReportObservation | None:
    return session.scalar(
        select(DailyReportObservation)
        .join(DailyReportRun, DailyReportRun.run_id == DailyReportObservation.run_id)
        .where(
            DailyReportRun.business_date == business_date,
            DailyReportRun.slot == slot,
            DailyReportRun.status == "success",
            DailyReportObservation.offer_id == offer_id,
        )
        .order_by(DailyReportRun.captured_at.desc())
    )


def _value_dict(
    row: DailyReportObservation,
) -> dict[str, int | None]:
    return {
        "page_views_30_days": row.page_views_30_days,
        "ordered_units": row.ordered_units,
        "platform_stock": row.platform_stock,
    }


def _manual_values(
    row: DailyReportResolution,
) -> dict[str, int | None]:
    return {
        "page_views_30_days": row.manual_page_views_30_days,
        "ordered_units": row.manual_ordered_units,
        "platform_stock": row.manual_platform_stock,
    }


def _final_values(
    row: DailyReportResolution,
) -> dict[str, int | None]:
    return {
        "page_views_30_days": row.final_page_views_30_days,
        "ordered_units": row.final_ordered_units,
        "platform_stock": row.final_platform_stock,
    }


def _source_values(
    session: Session,
    resolution: DailyReportResolution,
    source: str,
) -> dict[str, int | None] | None:
    if source == "manual":
        manual = _manual_values(resolution)
        if not any(value is not None for value in manual.values()):
            return None
        base_observation = _latest_observation(
            session,
            resolution.business_date,
            "evening",
            resolution.offer_id,
        ) or _latest_observation(
            session,
            resolution.business_date,
            "morning",
            resolution.offer_id,
        )
        base = _value_dict(base_observation) if base_observation is not None else {}
        return {
            key: manual[key] if manual[key] is not None else base.get(key)
            for key in _VALUE_KEYS
        }
    observation = _latest_observation(
        session,
        resolution.business_date,
        source,
        resolution.offer_id,
    )
    return _value_dict(observation) if observation is not None else None


def _apply_final(
    resolution: DailyReportResolution,
    values: Mapping[str, int | None],
    source: str,
    note: str,
    user_id: int,
    now: datetime,
) -> None:
    resolution.selected_source = source
    resolution.final_page_views_30_days = values["page_views_30_days"]
    resolution.final_ordered_units = values["ordered_units"]
    resolution.final_platform_stock = values["platform_stock"]
    resolution.confirm_note = note
    resolution.confirmed_by = user_id
    resolution.confirmed_at = now
    resolution.status = "confirmed"
    resolution.updated_at = now


def _item_payload(
    offer_id: str,
    morning: DailyReportObservation | None,
    evening: DailyReportObservation | None,
    resolution: DailyReportResolution | None,
    previous_stock: int | None,
) -> dict[str, Any]:
    identity = evening or morning
    morning_values = _value_dict(morning) if morning is not None else None
    evening_values = _value_dict(evening) if evening is not None else None
    manual_values = _manual_values(resolution) if resolution is not None else None
    final_values = _final_values(resolution) if resolution is not None else None
    if resolution is not None and resolution.status == "confirmed":
        current = final_values or {}
    elif evening_values is not None:
        current = evening_values
    elif manual_values is not None and any(value is not None for value in manual_values.values()):
        base = morning_values or {}
        current = {
            key: manual_values[key] if manual_values[key] is not None else base.get(key)
            for key in _VALUE_KEYS
        }
    else:
        current = morning_values or {key: None for key in _VALUE_KEYS}
    differences = [
        key
        for key in _VALUE_KEYS
        if _candidate_values(morning_values, evening_values, manual_values, key)
    ]
    orders = int(current.get("ordered_units") or 0)
    current_stock = current.get("platform_stock")
    expected_stock = previous_stock - orders if previous_stock is not None else None
    mismatch = (
        expected_stock is not None
        and current_stock is not None
        and expected_stock != current_stock
    )
    return {
        "offer_id": offer_id,
        "sku": identity.sku if identity is not None else None,
        "title": identity.title if identity is not None else offer_id,
        "status": resolution.status if resolution is not None else "awaiting_evening",
        "morning": morning_values,
        "evening": evening_values,
        "manual": manual_values,
        "manual_reason": resolution.manual_reason if resolution is not None else None,
        "manual_note": resolution.manual_note if resolution is not None else None,
        "final": final_values if resolution is not None and resolution.status == "confirmed" else None,
        "selected_source": resolution.selected_source if resolution is not None else None,
        "confirm_note": resolution.confirm_note if resolution is not None else None,
        "operator_note": resolution.operator_note if resolution is not None else None,
        "differences": differences,
        "current": current,
        "stock_check": {
            "previous_stock": previous_stock,
            "expected_stock": expected_stock,
            "actual_stock": current_stock,
            "mismatch": mismatch,
            "dismissed": (
                resolution.stock_alert_dismissed if resolution is not None else False
            ),
            "note": resolution.stock_alert_note if resolution is not None else None,
        },
    }


def _candidate_values(
    morning: dict[str, int | None] | None,
    evening: dict[str, int | None] | None,
    manual: dict[str, int | None] | None,
    key: str,
) -> bool:
    values: list[int | None] = []
    if morning is not None:
        values.append(morning[key])
    if evening is not None:
        values.append(evening[key])
    if manual is not None and manual[key] is not None:
        values.append(manual[key])
    return len(values) > 1 and any(value != values[0] for value in values[1:])


def _previous_values(
    session: Session, business_date: date
) -> dict[str, int | None]:
    previous_date = session.scalar(
        select(func.max(DailyReportResolution.business_date)).where(
            DailyReportResolution.business_date < business_date
        )
    )
    if previous_date is None:
        return {}
    rows = list(
        session.scalars(
            select(DailyReportResolution).where(
                DailyReportResolution.business_date == previous_date
            )
        )
    )
    result: dict[str, int | None] = {}
    for row in rows:
        if row.status == "confirmed":
            result[row.offer_id] = row.final_platform_stock
            continue
        evening = _latest_observation(
            session, previous_date, "evening", row.offer_id
        )
        morning = _latest_observation(
            session, previous_date, "morning", row.offer_id
        )
        latest = evening or morning
        result[row.offer_id] = latest.platform_stock if latest is not None else None
    return result


def _reminders(
    session: Session, *, before: date | None
) -> list[dict[str, Any]]:
    statement = (
        select(
            DailyReportResolution.business_date,
            func.count(DailyReportResolution.id),
        )
        .where(DailyReportResolution.status != "confirmed")
        .group_by(DailyReportResolution.business_date)
        .order_by(DailyReportResolution.business_date)
    )
    if before is not None:
        statement = statement.where(DailyReportResolution.business_date < before)
    return [
        {
            "business_date": business_date.isoformat(),
            "unresolved_count": int(count),
        }
        for business_date, count in session.execute(statement).all()
    ]


def _reminders_for_date(
    session: Session, business_date: date
) -> list[dict[str, Any]]:
    count = int(
        session.scalar(
            select(func.count(DailyReportResolution.id)).where(
                DailyReportResolution.business_date == business_date,
                DailyReportResolution.status != "confirmed",
            )
        )
        or 0
    )
    return (
        [{"business_date": business_date.isoformat(), "unresolved_count": count}]
        if count
        else []
    )


def _resolution_or_error(
    session: Session, business_date: date, offer_id: str
) -> DailyReportResolution:
    row = session.scalar(
        select(DailyReportResolution).where(
            DailyReportResolution.business_date == business_date,
            DailyReportResolution.offer_id == offer_id,
        )
    )
    if row is None:
        raise DailyReportConflictError("该商品当天还没有日报采集数据")
    return row


def _resolve_deadline_if_complete(
    session: Session, business_date: date, now: datetime
) -> None:
    remaining = int(
        session.scalar(
            select(func.count(DailyReportResolution.id)).where(
                DailyReportResolution.business_date == business_date,
                DailyReportResolution.status != "confirmed",
            )
        )
        or 0
    )
    snapshot = session.get(DailyReportDeadlineSnapshot, business_date)
    if snapshot is not None and remaining == 0:
        snapshot.resolved_at = now


def _audit(
    session: Session,
    business_date: date,
    offer_id: str | None,
    action: str,
    payload: dict[str, Any] | None,
    note: str | None,
    user_id: int | None,
    now: datetime,
) -> None:
    session.add(
        DailyReportAudit(
            business_date=business_date,
            offer_id=offer_id,
            action=action,
            payload=payload,
            note=note,
            user_id=user_id,
            created_at=now,
        )
    )


def _required_note(note: str, message: str) -> str:
    clean = note.strip()
    if not clean:
        raise DailyReportInputError(message)
    if len(clean) > 2000:
        raise DailyReportInputError("备注不能超过 2000 个字符")
    return clean


def _identity_map(
    session: Session,
    rows: list[DailyReportResolution],
) -> dict[str, dict[str, str | None]]:
    offer_ids = sorted({row.offer_id for row in rows})
    if not offer_ids:
        return {}
    current = {
        row.offer_id: {"sku": row.sku, "title": row.title}
        for row in session.scalars(
            select(OfferCurrent).where(OfferCurrent.offer_id.in_(offer_ids))
        )
    }
    missing = set(offer_ids) - set(current)
    if missing:
        observations = session.scalars(
            select(DailyReportObservation)
            .where(DailyReportObservation.offer_id.in_(missing))
            .order_by(DailyReportObservation.id.desc())
        )
        for row in observations:
            current.setdefault(
                row.offer_id,
                {"sku": row.sku, "title": row.title},
            )
    return current


def _all_previous_confirmed_stock(
    rows: list[DailyReportResolution],
) -> dict[tuple[date, str], int | None]:
    by_offer: dict[str, list[DailyReportResolution]] = {}
    for row in rows:
        by_offer.setdefault(row.offer_id, []).append(row)
    result: dict[tuple[date, str], int | None] = {}
    for offer_id, values in by_offer.items():
        previous_stock: int | None = None
        for row in sorted(values, key=lambda item: item.business_date):
            result[(row.business_date, offer_id)] = previous_stock
            previous_stock = row.final_platform_stock
    return result


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
