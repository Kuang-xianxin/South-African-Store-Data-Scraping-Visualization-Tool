"""Permission-scoped homepage projections. GET reads local SQL and cached FX only."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, case, func, select
from sqlalchemy.orm import Session

from takealot_ops.container_selection import _load_local_rate
from takealot_ops.domain import sast_date
from takealot_ops.product_master import load_product_master_links, normalize_product_sku
from takealot_ops.storage.models import (
    CollectionRun, DailySalesMetricState, OfferCurrent, OfferSnapshot, SaleItem, SellerHomeSnapshot,
)


def _month(day: date, offset: int) -> date:
    ordinal = day.year * 12 + day.month - 1 + offset
    return date(ordinal // 12, ordinal % 12 + 1, 1)


def _sum(values: list[Any]) -> float | None:
    known = [Decimal(str(v)) for v in values if v is not None]
    return float(sum(known)) if known else None


def _verified_day(row: Mapping[str, Any], today: date) -> bool:
    verified_at = row["verified_at"]
    if row["source_kind"] != "takealot_sales_api" or not isinstance(verified_at, datetime):
        return False
    if row["metric_date"] == today:
        return True  # visibly labelled in-progress in the cards
    closed = datetime.combine(row["metric_date"] + timedelta(days=1), time.min,
                              ZoneInfo("Africa/Johannesburg"))
    verified_utc = verified_at.replace(tzinfo=UTC) if verified_at.tzinfo is None else verified_at.astimezone(UTC)
    return verified_utc >= closed.astimezone(UTC)


def build_home_dashboard(
    engine: Engine, stores: Mapping[str, str], root: Path, *,
    today: date | None = None, start_date: date | None = None, end_date: date | None = None,
) -> dict[str, Any]:
    today = today or sast_date(datetime.now(UTC))
    end_date = min(end_date or today, today)
    start_date = start_date or _month(end_date, 0)
    if start_date > end_date or (end_date - start_date).days > 730:
        raise ValueError("invalid dashboard date range")
    codes = tuple(stores)
    sale = SaleItem.__table__
    state = DailySalesMetricState.__table__
    offer = OfferCurrent.__table__
    snapshot = OfferSnapshot.__table__
    home = SellerHomeSnapshot.__table__
    runs = CollectionRun.__table__
    _, rate = _load_local_rate(root)
    if rate["fetched_at"]:
        rate["stale"] = datetime.now(UTC) - datetime.fromisoformat(rate["fetched_at"]) > timedelta(hours=1)
    fx = Decimal(str(rate["rate"])) if rate["rate"] else None
    with engine.connect() as db:
        states = list(db.execute(select(
            state.c.store_code, state.c.metric_date, state.c.source_kind, state.c.verified_at,
        ).where(state.c.store_code.in_(codes))).mappings())
        first = db.scalar(select(func.min(sale.c.sales_day)).where(sale.c.store_code.in_(codes)))
        history_start = min([r["metric_date"] for r in states] + ([first] if first else []) or [today])
        known = {(r["store_code"], r["metric_date"]) for r in states if _verified_day(dict(r), today)}
        current = [dict(row) for row in db.execute(select(
            offer.c.store_code, offer.c.offer_id, offer.c.sku, offer.c.selling_price,
            offer.c.takealot_available_stock, offer.c.captured_at,
        ).where(offer.c.store_code.in_(codes))).mappings()]
        snapshots = {(r["store_code"], r["kind"]): dict(r) for r in db.execute(
            select(home).where(home.c.store_code.in_(codes))
        ).mappings()}
        # A bootstrap capture may be newer than OfferCurrent. Price, units and DC
        # totals must then all come from that same official response.
        for code in codes:
            dc = snapshots.get((code, "warehouse"))
            latest_offer = max((r["captured_at"] for r in current if r["store_code"] == code), default=datetime.min)
            if dc and "offers" in dc["payload"] and dc["captured_at"] >= latest_offer:
                current = [r for r in current if r["store_code"] != code] + [
                    {**row, "store_code": code, "captured_at": dc["captured_at"],
                     "selling_price": Decimal(str(row["selling_price"])) if row["selling_price"] is not None else None}
                    for row in dc["payload"]["offers"]
                ]
        finance_runs = list(db.execute(select(
            runs.c.store_code, runs.c.status, runs.c.started_at,
        ).where(runs.c.store_code.in_(codes), runs.c.run_type == "home_finance")
            .order_by(runs.c.started_at.desc())).mappings())
        latest_finance_status: dict[str, str] = {}
        for finance_row in finance_runs:
            latest_finance_status.setdefault(finance_row["store_code"], finance_row["status"])

        def coverage(start: date, end: date) -> dict[str, Any]:
            expected = max(0, (end - start).days + 1)
            counts = {code: sum((code, start + timedelta(days=i)) in known for i in range(expected)) for code in codes}
            return {"known_store_days": sum(counts.values()), "expected_store_days": expected * len(codes),
                    "complete_stores": sum(n == expected and expected > 0 for n in counts.values()),
                    "store_count": len(codes)}

        month_key = func.substr(sale.c.sales_day, 1, 7)
        monthly_rows = list(db.execute(select(
            sale.c.store_code, month_key.label("month"), func.count().label("lines"),
            func.count(func.distinct(sale.c.order_id)).label("orders"),
            func.sum(case((sale.c.order_id.is_(None), 1), else_=0)).label("missing_order_ids"),
            func.sum(case((sale.c.selling_price.is_(None), 1), else_=0)).label("missing_price_lines"),
            func.sum(sale.c.quantity).label("units"), func.sum(sale.c.selling_price).label("revenue"),
        ).where(sale.c.store_code.in_(codes),
                sale.c.sales_day.between(min(_month(end_date, -11), _month(today, -1)), today))
            .group_by(sale.c.store_code, month_key)).mappings())

        def aggregate(start: date, end: date) -> dict[str, Any]:
            whole_month = start.day == 1 and end == min(_month(start, 1) - timedelta(days=1), today)
            rows = ([r for r in monthly_rows if r["month"] == start.strftime("%Y-%m")] if whole_month else list(db.execute(select(
                sale.c.store_code, func.count().label("lines"),
                func.count(func.distinct(sale.c.order_id)).label("orders"),
                func.sum(case((sale.c.order_id.is_(None), 1), else_=0)).label("missing_order_ids"),
                func.sum(case((sale.c.selling_price.is_(None), 1), else_=0)).label("missing_price_lines"),
                func.sum(sale.c.quantity).label("units"), func.sum(sale.c.selling_price).label("revenue"),
            ).where(sale.c.store_code.in_(codes), sale.c.sales_day.between(start, end))
                .group_by(sale.c.store_code)).mappings()))
            cov = coverage(start, end)
            has_data = bool(rows) or cov["known_store_days"] > 0
            order_count = sum(int(r["orders"]) for r in rows)
            return {
                "start": start.isoformat(), "end": end.isoformat(), **cov,
                "orders": order_count if has_data and (not rows or order_count > 0) else None,
                "units": sum(int(r["units"] or 0) for r in rows) if has_data else None,
                "revenue": _sum([r["revenue"] for r in rows]) if rows else (0 if has_data else None),
                "missing_order_ids": sum(int(r["missing_order_ids"] or 0) for r in rows),
                "missing_price_lines": sum(int(r["missing_price_lines"] or 0) for r in rows),
            }

        periods = []
        for key, label, begin, end in (
            ("total", "已采集累计", history_start, today),
            ("today", "今日", today, today),
            ("yesterday", "昨日", today - timedelta(days=1), today - timedelta(days=1)),
            ("month", "本月", _month(today, 0), today),
            ("previous_month", "上月", _month(today, -1), _month(today, 0) - timedelta(days=1)),
        ):
            result = aggregate(begin, end)
            sold_rows = db.execute(select(sale.c.store_code, sale.c.sku).where(
                sale.c.store_code.in_(codes), sale.c.sales_day.between(begin, end), sale.c.quantity > 0,
            ).distinct()).all()
            sold = {(code, normalize_product_sku(sku)) for code, sku in sold_rows if sku}
            complete_sales = result["known_store_days"] == result["expected_store_days"] > 0
            complete_sales = complete_sales and not any(not sku for _, sku in sold_rows)
            # A historical denominator must use that period's closing snapshot.
            closing = current if end == today else ([dict(row) for row in db.execute(select(
                snapshot.c.store_code, snapshot.c.sku, snapshot.c.takealot_available_stock,
            ).where(snapshot.c.store_code.in_(codes), snapshot.c.snapshot_date == end)).mappings()] if complete_sales else [])
            closing_skus = {(r["store_code"], normalize_product_sku(r["sku"])) for r in closing
                            if r["sku"] and (r["takealot_available_stock"] or 0) > 0}
            stock = _sum([r["takealot_available_stock"] for r in closing])
            closing_complete = bool(codes) and {r["store_code"] for r in closing} == set(codes) and all(
                r["takealot_available_stock"] is not None for r in closing
            )
            if end == today:
                closing_complete = closing_complete and all(
                    sast_date(r["captured_at"].replace(tzinfo=UTC)) == today for r in current
                )
            denominator = len(closing_skus | sold)
            units = result["units"]
            result.update(key=key, label=label, sold_skus=len(sold) if result["orders"] is not None else None,
                sku_denominator=denominator, closing_stock=stock,
                sku_selling_rate=round(len(sold) / denominator * 100, 2) if denominator and closing_complete and complete_sales else None,
                inventory_sell_through=round(units / (units + stock) * 100, 2)
                    if units is not None and stock is not None and units + stock > 0 and closing_complete and complete_sales else None,
                in_progress=end == today)
            periods.append(result)

        # One grouped read for the selected daily viewport. Missing days stay gaps.
        daily_rows = [dict(row) for row in db.execute(select(
            sale.c.store_code, sale.c.sales_day,
            func.count(func.distinct(sale.c.order_id)).label("orders"),
            func.sum(case((sale.c.order_id.is_(None), 1), else_=0)).label("missing_order_ids"),
            func.sum(case((sale.c.selling_price.is_(None), 1), else_=0)).label("missing_price_lines"),
            func.sum(sale.c.selling_price).label("revenue"),
        ).where(sale.c.store_code.in_(codes), sale.c.sales_day.between(start_date, end_date))
            .group_by(sale.c.store_code, sale.c.sales_day)).mappings()]
        daily = []
        # Keep the in-progress business day in cards, not completed-day trend lines.
        for i in range(max(0, (min(end_date, today - timedelta(days=1)) - start_date).days + 1)):
            day = start_date + timedelta(days=i)
            rows = [r for r in daily_rows if r["sales_day"] == day]
            cov = coverage(day, day)
            available = bool(rows) or cov["known_store_days"] > 0
            order_count = sum(r["orders"] for r in rows)
            daily.append({"date": day.isoformat(), "orders": order_count if available and (not rows or order_count > 0) else None,
                "revenue": _sum([r["revenue"] for r in rows]) if rows else (0 if available else None),
                "missing_order_ids": sum(r["missing_order_ids"] for r in rows),
                "missing_price_lines": sum(r["missing_price_lines"] for r in rows), **cov})
        monthly = []
        for offset in range(-11, 1):
            begin = _month(end_date, offset)
            end = min(_month(begin, 1) - timedelta(days=1), today)
            monthly.append({"date": begin.strftime("%Y-%m"), **aggregate(begin, end), "in_progress": end == today})

    with Session(engine) as session:
        links = load_product_master_links(session, platform_skus=[r["sku"] for r in current if r["sku"]], as_of_date=today)
    warehouse_stores = []
    for code, name in stores.items():
        rows = [r for r in current if r["store_code"] == code]
        cost_total = Decimal(0)
        gross_total = Decimal(0)
        cost_skus: set[str] = set()
        profit_skus: set[str] = set()
        missing_cost: set[str] = set()
        for row in rows:
            stock = row["takealot_available_stock"]
            if stock is None or stock <= 0:
                continue
            sku = normalize_product_sku(row["sku"] or "")
            identity = sku or f"offer:{row['offer_id']}"
            link = links.get(sku)
            cost = link.cost_rmb if link else None
            if cost is None or cost <= 0:
                missing_cost.add(identity)
                continue
            cost_skus.add(identity)
            cost_total += cost * stock
            if fx and row["selling_price"] is not None:
                profit_skus.add(identity)
                gross_total += (row["selling_price"] - cost * fx) * stock
        dc = snapshots.get((code, "warehouse"))
        warehouse_stores.append({
            "store_code": code, "store_name": name,
            "stock": _sum([r["takealot_available_stock"] for r in rows]),
            "stock_missing_offers": sum(r["takealot_available_stock"] is None for r in rows),
            "cost_rmb": float(cost_total) if cost_skus else None,
            "gross_zar": float(gross_total) if profit_skus else None,
            "cost_skus": len(cost_skus), "profit_skus": len(profit_skus), "missing_cost_skus": len(missing_cost),
            "regions": dc["payload"]["regions"] if dc else {},
            "regions_complete": bool(dc and dc["payload"]["missing_count"] == 0),
            "captured_at": max((r["captured_at"] for r in rows), default=None),
            "regions_captured_at": dc["captured_at"] if dc else None,
        })
    finance_stores = []
    for code, name in stores.items():
        balance = snapshots.get((code, "balances"))
        payout = snapshots.get((code, "payouts"))
        finance_stores.append({"store_code": code, "store_name": name,
            **{k: float(balance["payload"][k]) if balance else None for k in ("current", "available", "held_back")},
            "paid_out": float(payout["payload"]["total"]) if payout else None,
            "payout_start": sast_date(datetime.fromisoformat(payout["payload"]["start"])).isoformat() if payout else None,
            "payout_end": sast_date(datetime.fromisoformat(payout["payload"]["end"])).isoformat() if payout else None,
            "captured_at": balance["captured_at"] if balance else None,
            "payout_captured_at": payout["captured_at"] if payout else None,
            "latest_status": latest_finance_status.get(code, "pending"),
        })
    return {
        "today": today.isoformat(), "store_count": len(codes), "history_start": history_start.isoformat(),
        "rate": rate, "periods": periods, "daily": daily, "monthly": monthly,
        "finance": {"stores": finance_stores, "totals": {k: _sum([r[k] for r in finance_stores]) for k in ("current", "available", "held_back", "paid_out")},
            "balance_coverage": sum(r["current"] is not None for r in finance_stores),
            "payout_coverage": sum(r["paid_out"] is not None for r in finance_stores)},
        "warehouse": {"stores": warehouse_stores, "totals": {k: _sum([r[k] for r in warehouse_stores]) for k in ("stock", "cost_rmb", "gross_zar")},
            "profit_skus": sum(r["profit_skus"] for r in warehouse_stores),
            "missing_cost_skus": sum(r["missing_cost_skus"] for r in warehouse_stores)},
    }
