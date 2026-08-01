from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from takealot_ops.competitors.domain import (
    CompetitorOffer,
    CompetitorProduct,
    CompetitorReviewRecord,
    CompetitorVariant,
    StockProbeResult,
    VariantStockObservation,
    analyze_sales_signal,
    estimate_lifetime_sales,
    summarize_reviews,
)
from takealot_ops.competitors.repository import CompetitorRepository
from takealot_ops.competitors.service import (
    _variant_row,
    load_competitor_dataset,
    load_competitor_link_health,
)
from takealot_ops.competitors.stock import skipped_stock_probe
from takealot_ops.competitors.web import create_app
from takealot_ops.storage.migrations import create_schema


def test_only_default_variant_falls_back_to_snapshot_product_image() -> None:
    base = {
        "plid": "12345678",
        "snapshot_id": 1,
        "image_url": None,
        "collected_at": datetime(2026, 7, 29, 8),
        "sku": "SKU-1",
        "seller_name": "Seller",
        "price": None,
        "stock_quantity": 5,
        "stock_exact": True,
        "stock_method": "anonymous-cart-limit",
        "stock_note": None,
        "customer_purchase_limit": None,
        "is_leadtime": False,
        "url": "https://www.takealot.com/example/PLID12345678",
    }
    default_variant = SimpleNamespace(
        **base,
        variant_key="default",
        variant_label="默认款",
    )
    colour_variant = SimpleNamespace(
        **base,
        variant_key="colour=black",
        variant_label="Colour：Black",
    )

    assert (
        _variant_row(
            default_variant,
            default_image_url="https://example.invalid/product.jpg",
        )["图片"]
        == "https://example.invalid/product.jpg"
    )
    assert (
        _variant_row(
            colour_variant,
            default_image_url="https://example.invalid/product.jpg",
        )["图片"]
        is None
    )


def test_competitor_observation_persists_snapshot_and_deduplicated_reviews(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "competitors.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url)
    create_schema(engine)
    product = CompetitorProduct(
        plid="72189176",
        url="https://www.takealot.com/example/PLID72189176",
        title="Laser Lipo",
        image_url="https://example.invalid/laser-lipo.jpg",
        sku="SKU-1",
        seller_id="seller-1",
        seller_name="Seller One",
        price=6597.0,
        stock_status="In stock",
        is_leadtime=False,
        review_count=1,
        rating=5.0,
        offers=(
            CompetitorOffer(
                selected=True,
                sku="SKU-1",
                seller_id="seller-1",
                seller_name="Seller One",
                price=6597.0,
                stock_status="In stock",
            ),
        ),
        variants=(
            CompetitorVariant(
                key="default",
                label=("Colour: {'name': 'Black', 'value': 'Black', 'type': 'colour_variant'}"),
                url="https://www.takealot.com/example/PLID72189176",
                title="Laser Lipo",
                sku="SKU-1",
                seller_id="seller-1",
                seller_name="Seller One",
                price=6597.0,
                stock_status="In stock",
                is_leadtime=False,
                is_add_to_cart_available=True,
                image_url="https://example.invalid/laser-lipo-black.jpg",
            ),
        ),
    )
    reviews = [
        CompetitorReviewRecord(
            review_id="review-1",
            rating=5,
            title="Great",
            body="Works",
            customer_name="Buyer",
            review_date="2026-07-20",
        )
    ]

    observations = (
        (
            datetime(2026, 7, 22, 8, tzinfo=UTC),
            StockProbeResult(
                quantity=9,
                exact=True,
                method="anonymous-cart-limit",
                note="精确库存",
            ),
        ),
        (
            datetime(2026, 7, 23, 8, tzinfo=UTC),
            skipped_stock_probe(),
        ),
    )
    for collected_at, stock_probe in observations:
        with Session(engine) as session, session.begin():
            repository = CompetitorRepository(session)
            previous = repository.latest_compatible_snapshot(product)
            repository.save_observation(
                product=product,
                reviews=reviews,
                review_summary=summarize_reviews(reviews),
                stock=stock_probe,
                variant_stocks=[
                    VariantStockObservation(
                        variant=product.variants[0],
                        stock=(
                            replace(stock_probe, customer_purchase_limit=10)
                            if stock_probe.quantity is not None
                            else stock_probe
                        ),
                    )
                ],
                lifetime_sales=estimate_lifetime_sales(product.review_count),
                signal=analyze_sales_signal(
                    previous,
                    current_stock_quantity=None,
                    current_stock_exact=False,
                    current_review_count=product.review_count,
                ),
                collected_at=collected_at,
            )

    with Session(engine) as session, session.begin():
        CompetitorRepository(session).record_not_found(
            plid=product.plid,
            url=product.url,
            checked_at=datetime(2026, 7, 24, 8, tzinfo=UTC),
            control_plid="99999999",
            control_check_ok=True,
        )

    dataset = load_competitor_dataset(engine)
    link_health = load_competitor_link_health(engine)
    engine.dispose()

    assert len(dataset.current) == 1
    assert len(dataset.history) == 2
    assert len(dataset.reviews) == 1
    assert len(dataset.variants) == 2
    assert set(dataset.history["快照ID"]) == set(dataset.variants["快照ID"])
    assert set(dataset.variants["变体"]) == {"Colour：Black"}
    assert dataset.current.iloc[0]["图片"] == "https://example.invalid/laser-lipo.jpg"
    assert dataset.history.iloc[0]["图片"] == "https://example.invalid/laser-lipo.jpg"
    assert dataset.variants.iloc[0]["图片"] == "https://example.invalid/laser-lipo-black.jpg"
    limited_variant = dataset.variants.loc[dataset.variants["每位客户限购"].notna()].iloc[0]
    assert limited_variant["每位客户限购"] == 10
    assert "累计销量估算" not in dataset.current.columns
    assert dataset.current.iloc[0]["趋势判断"] == "库存不可比，评论无新增"
    assert dataset.current.iloc[0]["库存上限"] == "未探测"
    assert bool(dataset.current.iloc[0]["库存参考过期"])
    assert dataset.current.iloc[0]["上次成功库存"] == "9"
    assert dataset.current.iloc[0]["上次成功库存时间"] == datetime(2026, 7, 22, 8)
    assert link_health[0]["商品"] == "Laser Lipo"
    assert link_health[0]["图片"] == "https://example.invalid/laser-lipo.jpg"

    monkeypatch.setenv("TAKEALOT_DATABASE_URL", database_url)
    with TestClient(create_app(tmp_path)) as client:
        detail = client.get("/api/competitors/72189176").json()
    assert len(detail["history"]) == 2
    assert len(detail["variants"]) == 2
    assert {variant["图片"] for variant in detail["variants"]} == {
        "https://example.invalid/laser-lipo-black.jpg"
    }


def test_competitor_signals_recompute_from_oldest_and_latest_in_date_range(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'range.db').as_posix()}")
    create_schema(engine)
    base_product = CompetitorProduct(
        plid="12345678",
        url="https://www.takealot.com/example/PLID12345678",
        title="Range Product",
        image_url=None,
        sku="SKU-RANGE",
        seller_id="seller-range",
        seller_name="Range Seller",
        price=199.0,
        stock_status="In stock",
        is_leadtime=False,
        review_count=10,
        rating=4.5,
        offers=(
            CompetitorOffer(
                selected=True,
                sku="SKU-RANGE",
                seller_id="seller-range",
                seller_name="Range Seller",
                price=199.0,
                stock_status="In stock",
                plid="12345678",
                url="https://www.takealot.com/example/PLID12345678",
                offer_id="offer-down",
            ),
            CompetitorOffer(
                selected=False,
                sku="SKU-OTHER",
                seller_id="seller-other",
                seller_name="Other Seller",
                price=180.0,
                stock_status="In stock",
                plid="12345678",
                url="https://www.takealot.com/example/PLID12345678",
                offer_id="offer-up",
            ),
        ),
        variants=(
            CompetitorVariant(
                key="default",
                label="默认款",
                url="https://www.takealot.com/example/PLID12345678",
                title="Range Product",
                sku="SKU-RANGE",
                seller_id="seller-range",
                seller_name="Range Seller",
                price=199.0,
                stock_status="In stock",
                is_leadtime=False,
                is_add_to_cart_available=True,
            ),
        ),
    )
    observations = (
        (datetime(2026, 7, 22, 8, tzinfo=UTC), 10, 10, 220.0, 180.0),
        (datetime(2026, 7, 23, 8, tzinfo=UTC), 8, 11, 210.0, 190.0),
        (datetime(2026, 7, 24, 8, tzinfo=UTC), 4, 13, 200.0, 210.0),
    )
    for collected_at, quantity, review_count, price, other_price in observations:
        product = replace(
            base_product,
            review_count=review_count,
            price=price,
            offers=(
                replace(base_product.offers[0], price=price),
                replace(base_product.offers[1], price=other_price),
            ),
            variants=(replace(base_product.variants[0], price=price),),
        )
        stock = StockProbeResult(
            quantity=quantity,
            exact=True,
            method="anonymous-cart-limit",
            note="精确库存",
        )
        with Session(engine) as session, session.begin():
            repository = CompetitorRepository(session)
            previous = repository.latest_compatible_snapshot(product)
            repository.save_observation(
                product=product,
                reviews=[],
                review_summary=summarize_reviews([]),
                stock=stock,
                variant_stocks=[
                    VariantStockObservation(
                        variant=product.variants[0],
                        stock=stock,
                    )
                ],
                lifetime_sales=estimate_lifetime_sales(product.review_count),
                signal=analyze_sales_signal(
                    previous,
                    current_stock_quantity=stock.quantity,
                    current_stock_exact=stock.exact,
                    current_review_count=product.review_count,
                ),
                collected_at=collected_at,
            )

    all_range = load_competitor_dataset(engine)
    recent_range = load_competitor_dataset(
        engine,
        start_date=date(2026, 7, 23),
        end_date=date(2026, 7, 24),
    )
    engine.dispose()

    all_signal = all_range.current.iloc[0]
    assert all_signal["库存净变化"] == -6
    assert all_signal["库存净流出"] == 6
    assert all_signal["新增评论"] == 3
    assert all_signal["趋势判断"] == "两个独立正向信号"
    assert all_signal["观察期销量信号"] == "60–150"
    assert all_signal["区间快照数"] == 3
    assert all_signal["区间起始价格"] == 220.0
    assert all_signal["价格变化"] == -20.0
    assert all_signal["价格信号"] == "降价"
    all_offers = {offer["offer_id"]: offer for offer in all_signal["跟卖报价"]}
    assert all_offers["offer-down"]["价格变化"] == -20.0
    assert all_offers["offer-down"]["价格信号"] == "降价"
    assert all_offers["offer-up"]["区间起始价格"] == 180.0
    assert all_offers["offer-up"]["价格变化"] == 30.0
    assert all_offers["offer-up"]["价格信号"] == "涨价"
    assert bool(all_signal["库存可比"])
    assert all_range.available_start_date == date(2026, 7, 22)
    assert all_range.available_end_date == date(2026, 7, 24)
    assert set(all_range.history["趋势判断"]) == {"原始快照"}
    assert all_range.history["库存净流出"].isna().all()

    recent_signal = recent_range.current.iloc[0]
    assert recent_signal["库存净变化"] == -4
    assert recent_signal["库存净流出"] == 4
    assert recent_signal["新增评论"] == 2
    assert recent_signal["观察期销量信号"] == "40–100"
    assert recent_signal["区间快照数"] == 2
    assert recent_signal["区间起始价格"] == 210.0
    assert recent_signal["价格变化"] == -10.0
    assert recent_signal["价格信号"] == "降价"
    recent_offers = {offer["offer_id"]: offer for offer in recent_signal["跟卖报价"]}
    assert recent_offers["offer-down"]["价格变化"] == -10.0
    assert recent_offers["offer-up"]["价格变化"] == 20.0
    assert recent_range.selected_start_date == date(2026, 7, 23)
    assert recent_range.selected_end_date == date(2026, 7, 24)


def test_schema_upgrade_backfills_competitor_offer_groups(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy-targets.db').as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE competitor_targets ("
            "plid VARCHAR(30) PRIMARY KEY, url TEXT NOT NULL, title TEXT NULL, "
            "active BOOLEAN NOT NULL, created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO competitor_targets "
            "(plid, url, title, active, created_at, updated_at) VALUES "
            "('123', 'https://www.takealot.com/example/PLID123', NULL, 1, "
            "'2026-08-01 00:00:00', '2026-08-01 00:00:00')"
        )

    create_schema(engine)

    with engine.connect() as connection:
        group_plid = connection.exec_driver_sql(
            "SELECT offer_group_plid FROM competitor_targets WHERE plid = '123'"
        ).scalar_one()
        indexes = connection.exec_driver_sql(
            "PRAGMA index_list('competitor_targets')"
        ).fetchall()
    engine.dispose()

    assert group_plid == "123"
    assert any(row[1] == "ix_competitor_targets_offer_group_plid" for row in indexes)


def test_competitor_api_reads_the_shared_sqlite(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "empty.db"
    monkeypatch.setenv("TAKEALOT_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    app = create_app(tmp_path)

    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        assert client.get("/api/competitors").json() == {
            "items": [],
            "date_range": {
                "available_start": None,
                "available_end": None,
                "selected_start": None,
                "selected_end": None,
            },
        }
        invalid_range = client.get("/api/competitors?start_date=2026-07-24&end_date=2026-07-23")
        assert invalid_range.status_code == 422
        assert invalid_range.json()["detail"] == "开始日期不能晚于结束日期"
        invalid = client.post(
            "/api/competitors/collect",
            json={"url": "https://www.takealot.com/not-a-product"},
        )
        assert invalid.status_code == 422
        assert "PLID" in invalid.json()["detail"]
