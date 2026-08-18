from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from takealot_ops.erp.returns import (
    filter_return_rows,
    load_offer_returned_30_day_counter,
    load_return_collection_status,
    load_store_return_rows,
    summarize_return_rows,
)
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import CollectionRun, OfferCurrent, ReturnItem


def test_return_projection_keeps_detail_and_offer_counter_separate() -> None:
    engine = create_engine("sqlite://")
    create_schema(engine)
    captured_at = datetime(2026, 8, 17, 8, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="offer-1",
                tsin_id="tsin-1",
                sku="SKU-1",
                title="Return Test Product",
                image_url="https://example.invalid/product.jpg",
                productline_id="12345678",
                quantity_returned_30_days=7,
                captured_at=captured_at,
            )
        )
        session.add(
            ReturnItem(
                seller_return_id="return-1",
                order_id="order-1",
                offer_id="offer-1",
                tsin_id="tsin-1",
                sku="SKU-1",
                return_reference_number="RRN-1",
                quantity=2,
                return_date=datetime(2026, 8, 10, 0),
                return_status="sellable_stock",
                return_region="CPT",
                return_reason="defective_or_damaged",
                customer_comment="Damaged on arrival",
                outcomes=[{"outcome_id": "outcome-1", "status": "sellable_stock"}],
                transactions=[
                    {"transaction_type": "refund", "amount_incl_vat": "-199.98"}
                ],
                captured_at=captured_at,
                raw_payload={"seller_return_id": "return-1"},
            )
        )
        session.add(
            CollectionRun(
                run_id="returns-run-1",
                run_type="returns",
                scope_date=date(2026, 8, 17),
                started_at=captured_at,
                finished_at=captured_at,
                status="success",
                counts={
                    "records": 1,
                    "requested_start_ordinal": date(2025, 8, 18).toordinal(),
                    "requested_end_ordinal": date(2026, 8, 17).toordinal(),
                },
                error=None,
            )
        )

    with Session(engine) as session:
        rows = load_store_return_rows(
            session,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 17),
            plid="12345678",
        )
        status = load_return_collection_status(
            session,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 17),
        )
        counter = load_offer_returned_30_day_counter(session, plid="12345678")

    assert len(rows) == 1
    assert rows[0]["product_title"] == "Return Test Product"
    assert rows[0]["return_reason_label"] == "商品有缺陷或损坏"
    assert rows[0]["outcome_statuses"] == ["sellable_stock"]
    assert rows[0]["transaction_total_incl_vat"] == -199.98
    assert status["data_status"] == "collected"
    assert status["requested_from"] == "2025-08-18"
    assert status["requested_through"] == "2026-08-17"
    assert status["record_count"] == 1
    assert counter["units"] == 7
    summary = summarize_return_rows(rows)
    assert summary["return_units"] == 2
    assert summary["quality_related_units"] == 2
    assert summary["sellable_stock_units"] == 2
    assert summary["transaction_total_incl_vat"] == -199.98
    assert filter_return_rows(rows, query="rrn-1", reason="defective_or_damaged") == rows
    assert filter_return_rows(rows, query="test retrun") == rows
    assert filter_return_rows(rows, query="rrn-2") == []
    rows[0]["company_product_name"] = "Portable Wireless Speaker"
    assert filter_return_rows(rows, query="speaker wirless") == rows
    assert filter_return_rows(rows, query="rekaeps sseleriw") == []


def test_return_status_never_treats_uncollected_as_zero() -> None:
    engine = create_engine("sqlite://")
    create_schema(engine)
    with Session(engine) as session:
        status = load_return_collection_status(
            session,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 17),
        )
        rows = load_store_return_rows(
            session,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 17),
        )

    assert rows == []
    assert status["data_status"] == "uncollected"
    assert status["record_count"] is None


def test_return_status_does_not_claim_dates_outside_successful_range() -> None:
    engine = create_engine("sqlite://")
    create_schema(engine)
    captured_at = datetime(2026, 8, 17, 8, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add(
            CollectionRun(
                run_id="returns-range-run",
                run_type="returns",
                scope_date=date(2026, 8, 17),
                started_at=captured_at,
                finished_at=captured_at,
                status="success",
                counts={
                    "records": 3,
                    "requested_start_ordinal": date(2026, 8, 10).toordinal(),
                    "requested_end_ordinal": date(2026, 8, 17).toordinal(),
                },
                error=None,
            )
        )

    with Session(engine) as session:
        partial = load_return_collection_status(
            session,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 17),
        )
        outside = load_return_collection_status(
            session,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
        )

    assert partial["data_status"] == "partial"
    assert partial["requested_from"] == "2026-08-10"
    assert outside["data_status"] == "uncollected"
    assert outside["record_count"] is None


def test_return_status_marks_covered_data_stale_after_overlapping_failure() -> None:
    engine = create_engine("sqlite://")
    create_schema(engine)
    successful_at = datetime(2026, 8, 17, 8, tzinfo=UTC)
    failed_at = datetime(2026, 8, 17, 9, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                CollectionRun(
                    run_id="returns-full-success",
                    run_type="returns",
                    scope_date=date(2026, 8, 17),
                    started_at=successful_at,
                    finished_at=successful_at,
                    status="success",
                    counts={
                        "records": 10,
                        "requested_start_ordinal": date(2025, 8, 18).toordinal(),
                        "requested_end_ordinal": date(2026, 8, 17).toordinal(),
                    },
                    error=None,
                ),
                CollectionRun(
                    run_id="returns-overlap-failed",
                    run_type="returns",
                    scope_date=date(2026, 8, 17),
                    started_at=failed_at,
                    finished_at=failed_at,
                    status="failed",
                    counts={
                        "records": 0,
                        "requested_start_ordinal": date(2026, 8, 10).toordinal(),
                        "requested_end_ordinal": date(2026, 8, 17).toordinal(),
                    },
                    error="ApiTransportError: network failure",
                ),
            ]
        )

    with Session(engine) as session:
        status = load_return_collection_status(
            session,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 17),
        )

    assert status["data_status"] == "stale"
    assert status["record_count"] == 10
    assert status["latest_error"] == "ApiTransportError: network failure"


def test_schema_upgrades_the_reserved_return_table_in_place() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE return_items (
                    seller_return_id VARCHAR(100) PRIMARY KEY,
                    order_item_id VARCHAR(100),
                    offer_id VARCHAR(100),
                    return_date DATETIME,
                    return_status VARCHAR(100),
                    raw_payload JSON NOT NULL,
                    store_code VARCHAR(64) NOT NULL DEFAULT 'current'
                )
                """
            )
        )

    create_schema(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("return_items")}
    assert {
        "order_id",
        "tsin_id",
        "sku",
        "return_reference_number",
        "quantity",
        "return_region",
        "return_reason",
        "customer_comment",
        "outcomes",
        "transactions",
        "captured_at",
    } <= columns
