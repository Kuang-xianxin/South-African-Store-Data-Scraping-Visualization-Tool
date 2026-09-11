"""Additive homepage evidence; failures never replace the last successful snapshot."""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from takealot_ops.api.client import TakealotClient
from takealot_ops.collectors.offers import CollectionResult, _persist_run_failure, _persist_run_start
from takealot_ops.storage.models import SaleItem, SellerHomeSnapshot
from takealot_ops.storage.repository import Repository
from takealot_ops.storage.store_context import current_store_code

PAYOUT_TYPES = (
    "disbursement-disbursement", "disbursement-manual", "reversal-disbursement",
)


def save_snapshot(session: Session, kind: str, payload: dict[str, Any], at: datetime) -> None:
    code = current_store_code()
    row = session.get(SellerHomeSnapshot, (code, kind))
    if row is None:
        row = SellerHomeSnapshot(store_code=code, kind=kind)
        session.add(row)
    row.payload = payload
    row.captured_at = at.astimezone(UTC).replace(tzinfo=None)


def warehouse_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep DC evidence from the exact successful offers response, without a second pull."""
    regions: dict[str, int] = {}
    missing = 0
    offers = []
    for item in items:
        stocks = item.get("takealot_warehouse_stock")
        stock_rows = stocks if isinstance(stocks, list) else []
        valid = isinstance(stocks, list) and all(
            isinstance(s.get("quantity_available"), int) and s["quantity_available"] >= 0
            for s in stock_rows
        )
        offers.append({"offer_id": str(item["offer_id"]) if "offer_id" in item else None,
            "sku": item.get("sku"), "selling_price": item.get("selling_price"),
            "takealot_available_stock": sum(s["quantity_available"] for s in stock_rows) if valid else None})
        if not isinstance(stocks, list):
            missing += 1
            continue
        for stock in stocks:
            name = stock.get("region")
            qty = stock.get("quantity_available")
            if not isinstance(name, str) or not isinstance(qty, int) or qty < 0:
                missing += 1
                continue
            regions[name] = regions.get(name, 0) + qty
    return {"regions": regions, "offers": offers, "offer_count": len(items), "missing_count": missing}


def collect_home_finance(
    client: TakealotClient, repository: Repository, captured_at: datetime,
) -> CollectionResult:
    """Publish balances and paginated payouts independently in the normal refresh workflow."""
    run_id = _persist_run_start(repository, "home_finance")
    session = repository._session
    try:
        balances = client.get_balances().get("balances")
        if not isinstance(balances, dict):
            raise ValueError("invalid balances")
        values = {key: Decimal(str(balances.get(key))) for key in ("current", "available", "held_back")}
        if any(not value.is_finite() for value in values.values()):
            raise ValueError("invalid balance amount")
        with repository.transaction():
            save_snapshot(session, "balances", {k: str(v) for k, v in values.items()}, captured_at)
        # Seed from the first locally known sales day. This is an explicit coverage
        # boundary, never a claim of complete account lifetime history.
        with repository.transaction():
            previous = session.get(SellerHomeSnapshot, (current_store_code(), "payouts"))
            old = dict(previous.payload) if previous else {}
            first_day = session.scalar(select(func.min(SaleItem.sales_day)))
        if first_day is None:
            raise ValueError("payout history start unavailable")
        end = captured_at.astimezone(UTC)
        origin = datetime.combine(first_day, time.min, ZoneInfo("Africa/Johannesburg")).astimezone(UTC)
        origin = min(origin, datetime.fromisoformat(old["start"])) if old else origin
        start = max(origin, datetime.fromisoformat(old["end"]) - timedelta(days=30)) if old else origin
        transactions = dict(old.get("transactions", {}))
        # Reconcile the overlap as a full replacement, including deleted/revised rows.
        transactions = {k: v for k, v in transactions.items() if datetime.fromisoformat(v["at"]) < start}
        while start < end:
            stop = min(end, start + timedelta(days=179))
            for item in client.iter_items("/transactions", {
                "limit": 100, "transaction_type__in": list(PAYOUT_TYPES),
                "created_at__gte": start.isoformat(), "created_at__lte": stop.isoformat(),
            }):
                if item.get("transaction_type") not in PAYOUT_TYPES:
                    raise ValueError("unexpected payout transaction type")
                amount = Decimal(str(item["amount_incl_vat"]))
                at = datetime.fromisoformat(item["created_at"])
                if not amount.is_finite() or at.tzinfo is None:
                    raise ValueError("invalid payout transaction")
                transactions[str(item["transaction_id"])] = {
                    "amount": str(amount), "at": at.isoformat(), "type": item["transaction_type"],
                }
            start = stop
        # Official disbursements debit the seller ledger; reversals credit it.
        total = -sum((Decimal(row["amount"]) for row in transactions.values()), Decimal(0))
        with repository.transaction():
            save_snapshot(session, "payouts", {
                "total": str(total), "start": origin.isoformat(), "end": end.isoformat(),
                "transactions": transactions,
            }, captured_at)
            repository.finish_run(run_id, "success", {"records": len(transactions)}, None)
        return CollectionResult(run_id, "success", {"records": len(transactions)})
    except Exception as error:
        session.rollback()
        return _persist_run_failure(repository, run_id, error)
