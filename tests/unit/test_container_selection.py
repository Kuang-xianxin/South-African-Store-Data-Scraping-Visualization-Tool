from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from takealot_ops.container_selection import (
    _competitor_review_date,
    _load_radar_link_evidence,
    _own_offer_image_url,
    _own_profile_payload,
    _point_window,
    _radar_link_payload,
    import_container_selection_targets,
    load_container_selection_config,
)
from takealot_ops.erp.permissions import COMPETITORS_VIEW, STORE_VIEW
from takealot_ops.erp.web import _required_permission, _requires_connected_store_access
from takealot_ops.storage.models import (
    Base,
    CompetitorSnapshot,
    CompetitorTarget,
    CompetitorTargetAudit,
    CompetitorVariantSnapshot,
)


ROOT = Path(__file__).resolve().parents[2]


def test_versioned_profile_uses_multi_link_categories_and_one_month_recent_policy() -> None:
    payload = load_container_selection_config(ROOT)
    policy = payload["policy"]
    categories = payload["radar_categories"]
    representatives = [
        representative
        for category in categories
        for representative in category["representatives"]
    ]
    required_structure_roles = {"low_price", "high_price", "most_reviewed", "mid_market"}
    recent_roles = {"recent_stock_mover", "recent_review_grower", "recent_signal_backup"}

    assert policy["electrified_volume_limit_percent"] == 30
    assert policy["recent_sales_window_days"] == 30
    assert policy["comparison_sales_window_days"] == 30
    assert policy["replenishment_cover_days"] == 30
    assert len(categories) == 35
    assert len(representatives) == 196
    assert len(representatives) >= 4 * len(categories)
    assert len({candidate["plid"] for candidate in representatives}) == len(representatives)
    for category in categories:
        roles = {
            role
            for representative in category["representatives"]
            for role in representative["roles"]
        }
        anchor = category["economics_anchor"]
        assert len(category["representatives"]) >= policy["minimum_representatives_per_category"]
        assert required_structure_roles <= roles
        assert roles & recent_roles
        assert category["cohort_basis"]["extremes_are_sample_relative"] is True
        assert anchor["unit_cbm"] >= policy["new_min_unit_cbm"]
        assert anchor["sea_profit_rmb"] > 0
        assert anchor["electrical_evidence"]

    categories_by_id = {category["category_id"]: category for category in categories}
    expanded_category_ids = {
        "barrel-braais",
        "kids-climbing-playsets",
        "mattress-toppers",
        "executive-office-chairs",
        "cat-litter-furniture",
        "racing-wheel-stands",
        "hydraulic-floor-jacks",
        "gaming-chairs",
        "rolling-cosmetic-trolleys",
        "folding-sofa-beds",
        "printer-stands",
        "portable-camping-toilets",
    }
    assert expanded_category_ids <= categories_by_id.keys()
    assert all(
        len(categories_by_id[category_id]["representatives"]) >= 5
        for category_id in expanded_category_ids
    )
    assert policy["profit_currency_note"].startswith("sea_profit_rmb统一为人民币")
    assert categories_by_id["standard-kitchen-sinks"]["economics_anchor"][
        "sea_profit_rmb"
    ] == pytest.approx(701.151 / 2.6)
    assert categories_by_id["telescopic-ladders"]["economics_anchor"][
        "sea_profit_rmb"
    ] == pytest.approx(709.23573 / 2.6)
    assert categories_by_id["bedframes-mattresses"]["economics_anchor"][
        "sea_profit_rmb"
    ] == pytest.approx(378.93256 / 2.6)


def test_import_is_idempotent_and_retains_a_traceable_system_audit() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    changed_at = datetime(2026, 8, 25, 8, 0, tzinfo=UTC)
    try:
        first = import_container_selection_targets(
            ROOT,
            engine,
            actor_username="codex-container-selection",
            actor_display_name="Codex 配柜选品导入",
            changed_at=changed_at,
        )
        second = import_container_selection_targets(
            ROOT,
            engine,
            actor_username="codex-container-selection",
            actor_display_name="Codex 配柜选品导入",
            changed_at=changed_at,
        )

        assert first.added_count == first.configured_count
        assert first.configured_count == sum(
            len(category["representatives"])
            for category in load_container_selection_config(ROOT)["radar_categories"]
        ) + len(load_container_selection_config(ROOT)["retained_watchlist"])
        assert first.existing_count == 0
        assert second.added_count == 0
        assert second.existing_count == first.configured_count
        with Session(engine) as session:
            assert session.scalar(select(func.count(CompetitorTarget.plid))) == first.configured_count
            assert session.scalar(select(func.count(CompetitorTargetAudit.id))) == first.configured_count
            audit = session.scalars(select(CompetitorTargetAudit)).first()
            assert audit is not None
            assert audit.actor_user_id is None
            assert audit.actor_username == "codex-container-selection"
    finally:
        engine.dispose()


def test_missing_sales_points_remain_missing_in_coverage_math() -> None:
    points = [
        {"date": "2026-08-23", "ordered_units": 2, "data_status": "verified"},
        {"date": "2026-08-24", "ordered_units": None, "data_status": "missing"},
        {"date": "2026-08-25", "ordered_units": 0, "data_status": "verified"},
    ]

    window = _point_window(points, date(2026, 8, 23), date(2026, 8, 25))

    assert window == {
        "units": 2,
        "known_days": 2,
        "verified_days": 2,
        "partial_days": 0,
        "missing_days": 1,
    }


def test_own_offer_image_prefers_a_sellable_offer() -> None:
    unavailable = SimpleNamespace(
        offer_id="A",
        image_url=" https://media.takealot.com/covers_images/unavailable.jpg ",
        status="not_buyable",
        total_stock=0,
        takealot_available_stock=0,
        seller_available_stock=0,
    )
    sellable = SimpleNamespace(
        offer_id="B",
        image_url="https://media.takealot.com/covers_images/sellable.jpg",
        status="buyable",
        total_stock=2,
        takealot_available_stock=2,
        seller_available_stock=0,
    )

    assert _own_offer_image_url([unavailable, sellable]) == sellable.image_url


def test_old_sales_cannot_rescue_a_product_without_recent_velocity() -> None:
    item = _own_profile_payload(
        {
            "company_sku": "RECENT-ONLY-001",
            "product_name": "Recent-only fixture",
            "source_sheet": "工作表9",
            "source_row": 2,
            "electrical_status": "non_electric",
            "electrical_evidence": "无插头、无随货电池",
            "unit_cbm": 0.1,
            "units_per_carton": 1,
            "sea_freight_rmb": 10,
        },
        product_name="Recent-only fixture",
        plids=["12345678"],
        links=[
            {
                "store_code": "store-02",
                "image_url": "https://media.takealot.com/covers_images/recent-only.jpg",
                "ordered_units": 120,
                "monthly_velocity": 40.0,
                "recent_30_units": 0,
                "previous_30_units": 30,
                "recent_data_ready": True,
                "previous_data_ready": True,
                "recent_monthly_velocity": 0.0,
                "previous_monthly_velocity": 30.0,
                "known_days": 90,
                "inventory": {
                    "platform_available_stock": 0,
                    "seller_available_stock": 0,
                    "sellable_stock": 0,
                    "stock_in_receiving": 0,
                    "stock_on_way": 0,
                },
            }
        ],
        profit_items=[
            {
                "store_code": "STORE-A",
                "offer_id": "OFFER-A",
                "scenarios": {
                    "current_fee_adjusted": {
                        "profit_rmb": 100,
                        "price_rmb": 200,
                    }
                },
                "fee_basis": "fixture",
            }
        ],
        rate_available=True,
        as_of=date(2026, 8, 25),
        window_start=date(2026, 5, 28),
        target_cover_days=30,
        clearance_days=30,
        minimum_known_days=14,
        minimum_recent_monthly_units=3,
        strong_recent_monthly_units=6,
        maximum_recent_decline_ratio=0.2,
    )

    assert item["sales"]["ordered_units"] == 120
    assert item["image_url"] == "https://media.takealot.com/covers_images/recent-only.jpg"
    assert item["image_store_code"] == "store-02"
    assert item["sales"]["forecast_monthly_units"] == 0
    assert item["recommendation"]["status"] == "low_velocity"
    assert item["recommendation"]["recommended_units"] == 0
    assert "旧销量" in item["recommendation"]["reason"] or "历史销量" in item["recommendation"]["reason"]


def test_competitor_review_freshness_parses_public_review_dates() -> None:
    assert _competitor_review_date("24 Aug 2026") == date(2026, 8, 24)
    assert _competitor_review_date("2026-08-24") == date(2026, 8, 24)
    assert _competitor_review_date("Recently") is None


def test_radar_link_exposes_fixed_observed_stock_outflow_windows() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    plid = "12345678"
    url = f"https://www.takealot.com/example/PLID{plid}"
    observations = [
        (datetime(2026, 8, 1, 8, tzinfo=UTC), 100, "variant-a"),
        (datetime(2026, 8, 10, 8, tzinfo=UTC), 90, "variant-a"),
        (datetime(2026, 8, 15, 8, tzinfo=UTC), 20, "variant-b"),
        (datetime(2026, 8, 23, 8, tzinfo=UTC), 15, "variant-b"),
        (datetime(2026, 8, 29, 8, tzinfo=UTC), 10, "variant-b"),
    ]
    try:
        with Session(engine) as session, session.begin():
            session.add(
                CompetitorTarget(
                    plid=plid,
                    offer_group_plid=plid,
                    url=url,
                    title="Window fixture",
                    active=True,
                    created_at=observations[0][0],
                    updated_at=observations[-1][0],
                )
            )
            for collected_at, stock_quantity, variant_key in observations:
                snapshot = CompetitorSnapshot(
                    plid=plid,
                    collected_at=collected_at,
                    url=url,
                    title="Window fixture",
                    image_url=None,
                    category_path=None,
                    sku="SKU-1",
                    seller_id="seller-1",
                    seller_name="Seller 1",
                    price=Decimal("100"),
                    stock_status="In stock",
                    stock_quantity=stock_quantity,
                    stock_exact=True,
                    stock_method="test",
                    stock_note=None,
                    review_count=0,
                    fetched_review_count=0,
                    rating=None,
                    positive_reviews=0,
                    neutral_reviews=0,
                    negative_reviews=0,
                    lifetime_sales_min=0,
                    lifetime_sales_max=0,
                    previous_snapshot_id=None,
                    observed_stock_outflow=None,
                    review_delta=None,
                    period_sales_min=None,
                    period_sales_max=None,
                    trend_label="fixture",
                    trend_note="fixture",
                    offers=[],
                )
                session.add(snapshot)
                session.flush()
                session.add(
                    CompetitorVariantSnapshot(
                        snapshot_id=snapshot.id,
                        plid=plid,
                        collected_at=collected_at,
                        variant_key=variant_key,
                        variant_label=variant_key,
                        image_url=None,
                        url=url,
                        sku="SKU-1",
                        seller_id="seller-1",
                        seller_name="Seller 1",
                        price=Decimal("100"),
                        stock_status="In stock",
                        is_leadtime=False,
                        stock_quantity=stock_quantity,
                        stock_exact=True,
                        stock_method="test",
                        stock_note=None,
                        customer_purchase_limit=None,
                    )
                )

        evidence = _load_radar_link_evidence(
            engine,
            configured=[{"plid": plid}],
            policy={
                "minimum_radar_snapshots": 2,
                "minimum_radar_baseline_days": 7,
                "recent_sales_window_days": 30,
            },
            as_of=date(2026, 8, 29),
        )[plid]
        payload = _radar_link_payload(
            {"plid": plid, "url": url, "name": "Window fixture", "roles": []},
            evidence=evidence,
        )

        assert payload["monitoring"]["recent_observed_sales"] == {
            "7": 5,
            "15": 10,
            "30": 20,
            "60": 20,
            "90": 20,
        }
        assert payload["monitoring"]["recent_observed_sales_through"] == "2026-08-29"
    finally:
        engine.dispose()


def test_container_selection_route_accepts_store_or_competitor_view_without_single_store_gate() -> None:
    path = "/api/erp/container-selection"

    assert _required_permission(path, "GET") == (STORE_VIEW, COMPETITORS_VIEW)
    assert _requires_connected_store_access(path) is False
