from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from takealot_ops.competitors.domain import (
    CompetitorOffer,
    CompetitorProduct,
    CompetitorReviewRecord,
    CompetitorVariant,
    OfferStockObservation,
    ReviewSummary,
    StockProbeResult,
    VariantStockObservation,
    analyze_sales_signal,
    estimate_lifetime_sales,
    summarize_reviews,
)
from takealot_ops.competitors.repository import CompetitorRepository
from takealot_ops.competitors.own_store import load_connected_store_offer_points
from takealot_ops.competitors.service import (
    _variant_row,
    load_competitor_dataset,
    load_competitor_link_health,
)
from takealot_ops.competitors.stock import skipped_stock_probe
from takealot_ops.competitors.web import create_app
from takealot_ops.storage.migrations import create_schema
from takealot_ops.storage.models import (
    CompetitorTarget,
    ErpStore,
    OfferCurrent,
    StoreOfferBaseline,
    StoreOfferObservation,
)
from takealot_ops.storage.store_context import store_scope


def test_store_offer_points_filter_scope_and_skip_observation_duplicates(
    tmp_path: Path,
) -> None:
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'store-offer-points.db').as_posix()}"
    )
    create_schema(engine)
    captured_at = datetime(2026, 8, 14, 1, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add(
            ErpStore(
                code="store-02",
                display_name="Beta Store",
                active=True,
                data_connected=True,
                created_at=captured_at,
                updated_at=captured_at,
            )
        )

    def point(
        model: type[StoreOfferBaseline] | type[StoreOfferObservation],
        offer_id: str,
        plid: str,
        captured: datetime,
    ) -> StoreOfferBaseline | StoreOfferObservation:
        return model(
            display_date=date(2026, 8, 14),
            offer_id=offer_id,
            productline_id=plid,
            sku=f"SKU-{offer_id}",
            title=offer_id,
            image_url=None,
            selling_price=100,
            status="buyable",
            total_stock=5,
            captured_at=captured,
        )

    with store_scope("current"), Session(engine) as session, session.begin():
        session.add_all(
            [
                point(StoreOfferObservation, "duplicate", "11111111", captured_at),
                point(StoreOfferBaseline, "duplicate", "11111111", captured_at),
                point(
                    StoreOfferBaseline,
                    "legacy-only",
                    "11111111",
                    datetime(2026, 8, 14, 2, tzinfo=UTC),
                ),
            ]
        )
    with store_scope("store-02"), Session(engine) as session, session.begin():
        session.add(
            point(StoreOfferObservation, "other-store", "11111111", captured_at)
        )

    with Session(engine) as session:
        points = load_connected_store_offer_points(
            session,
            plids={"11111111"},
            store_codes={"current"},
        )
    engine.dispose()

    assert [point.offer_id for point in points] == ["duplicate", "legacy-only"]
    assert isinstance(points[0], StoreOfferObservation)
    assert isinstance(points[1], StoreOfferBaseline)
    assert {point.store_code for point in points} == {"current"}


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


def test_own_store_variant_family_keeps_one_plid_card_and_exact_variant_images(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'own-store-variant-family.db').as_posix()}")
    create_schema(engine)
    plid = "102722716"
    url = f"https://www.takealot.com/p/PLID{plid}"
    captured_at = datetime(2026, 8, 10, 2, tzinfo=UTC)
    black_image = "https://example.invalid/blanket-black.jpg"
    grey_image = "https://example.invalid/blanket-grey.jpg"
    variants = (
        CompetitorVariant(
            key="colour=black",
            label="Black",
            url=f"{url}?colour=black",
            title="Blanket Family - Black",
            sku="SKU-BLACK",
            seller_id="own-seller",
            seller_name="Current Store",
            price=1320,
            stock_status="In stock",
            is_leadtime=False,
            is_add_to_cart_available=True,
            image_url=black_image,
        ),
        CompetitorVariant(
            key="colour=grey",
            label="Grey",
            url=f"{url}?colour=grey",
            title="Blanket Family - Grey",
            sku="SKU-GREY",
            seller_id="own-seller",
            seller_name="Current Store",
            price=1360,
            stock_status="In stock",
            is_leadtime=False,
            is_add_to_cart_available=True,
            image_url=grey_image,
        ),
    )
    product = CompetitorProduct(
        plid=plid,
        url=url,
        title="Infrared Sauna Blanket Family",
        image_url=black_image,
        sku="SKU-BLACK",
        seller_id="own-seller",
        seller_name="Current Store",
        price=1320,
        stock_status="In stock",
        is_leadtime=False,
        review_count=0,
        rating=0,
        offers=(
            CompetitorOffer(
                selected=True,
                sku="SKU-BLACK",
                seller_id="own-seller",
                seller_name="Current Store",
                price=1320,
                stock_status="In stock",
                is_buybox=True,
                plid=plid,
                url=f"{url}?colour=black",
                offer_id="offer-black",
                variant_key="colour=black",
                variant_label="Black",
            ),
            CompetitorOffer(
                selected=False,
                sku="SKU-GREY",
                seller_id="own-seller",
                seller_name="Current Store",
                price=1360,
                stock_status="In stock",
                is_buybox=True,
                plid=plid,
                url=f"{url}?colour=grey",
                offer_id="offer-grey",
                variant_key="colour=grey",
                variant_label="Grey",
            ),
        ),
        variants=variants,
    )
    skipped = skipped_stock_probe()
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                OfferCurrent(
                    offer_id="offer-black",
                    productline_id=plid,
                    tsin_id="TSIN-BLACK",
                    sku="SKU-BLACK",
                    title="Infrared Sauna Blanket Family - Black",
                    image_url=black_image,
                    selling_price=1320,
                    total_stock=37,
                    captured_at=captured_at,
                ),
                OfferCurrent(
                    offer_id="offer-grey",
                    productline_id=plid,
                    tsin_id="TSIN-GREY",
                    sku="SKU-GREY",
                    title="Infrared Sauna Blanket Family - Grey",
                    image_url=grey_image,
                    selling_price=1360,
                    total_stock=36,
                    captured_at=captured_at,
                ),
                StoreOfferBaseline(
                    display_date=date(2026, 8, 10),
                    offer_id="offer-black",
                    productline_id=plid,
                    sku="SKU-BLACK",
                    title="Infrared Sauna Blanket Family - Black",
                    image_url=black_image,
                    selling_price=1320,
                    status="buyable",
                    total_stock=37,
                    captured_at=captured_at,
                ),
                StoreOfferBaseline(
                    display_date=date(2026, 8, 10),
                    offer_id="offer-grey",
                    productline_id=plid,
                    sku="SKU-GREY",
                    title="Infrared Sauna Blanket Family - Grey",
                    image_url=grey_image,
                    selling_price=1360,
                    status="buyable",
                    total_stock=36,
                    captured_at=captured_at,
                ),
            ]
        )
        CompetitorRepository(session).save_observation(
            product=product,
            reviews=[],
            review_summary=summarize_reviews([]),
            stock=skipped,
            variant_stocks=[
                VariantStockObservation(variant=variant, stock=skipped) for variant in variants
            ],
            offer_stocks=[],
            lifetime_sales=estimate_lifetime_sales(0),
            signal=analyze_sales_signal(
                None,
                current_stock_quantity=None,
                current_stock_exact=False,
                current_review_count=0,
            ),
            collected_at=captured_at,
            register_target=False,
        )

    dataset = load_competitor_dataset(engine)
    engine.dispose()

    assert len(dataset.store_current) == 1
    item = dataset.store_current.iloc[0]
    assert item["plid"] == plid
    assert item["商品"] == "Infrared Sauna Blanket Family"
    assert item["图片"] == black_image
    seller_api_offers = [offer for offer in item["对比报价"] if offer["报价来源"] == "seller_api"]
    assert len(seller_api_offers) == 2
    assert {offer["TSIN"]: offer["图片"] for offer in seller_api_offers} == {
        "TSIN-BLACK": black_image,
        "TSIN-GREY": grey_image,
    }
    assert {row["变体"]: row["图片"] for row in dataset.variants.to_dict(orient="records")} == {
        "Black": black_image,
        "Grey": grey_image,
    }


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
                is_buybox=True,
                is_add_to_cart_available=True,
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


def test_targeted_competitor_dataset_filters_detail_queries_by_plid(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'targeted-detail.db').as_posix()}")
    create_schema(engine)
    collected_at = datetime(2026, 8, 10, 8, tzinfo=UTC)

    for index, plid in enumerate(("101163999", "99999999"), start=1):
        variant = CompetitorVariant(
            key="default",
            label="默认款",
            url=f"https://www.takealot.com/example/PLID{plid}",
            title=f"Product {index}",
            sku=f"SKU-{index}",
            seller_id=f"seller-{index}",
            seller_name=f"Seller {index}",
            price=float(100 + index),
            stock_status="In stock",
            is_leadtime=False,
            is_add_to_cart_available=True,
        )
        product = CompetitorProduct(
            plid=plid,
            url=variant.url,
            title=variant.title,
            image_url=None,
            sku=variant.sku,
            seller_id=variant.seller_id,
            seller_name=variant.seller_name,
            price=variant.price,
            stock_status=variant.stock_status,
            is_leadtime=False,
            review_count=1,
            rating=5.0,
            offers=(),
            variants=(variant,),
        )
        review = CompetitorReviewRecord(
            review_id=f"review-{index}",
            rating=5,
            title="Good",
            body="Works",
            customer_name="Buyer",
            review_date="2026-08-10",
        )
        stock = StockProbeResult(
            quantity=index,
            exact=True,
            method="test",
            note="test",
        )
        with Session(engine) as session, session.begin():
            CompetitorRepository(session).save_observation(
                product=product,
                reviews=[review],
                review_summary=summarize_reviews([review]),
                stock=stock,
                variant_stocks=[VariantStockObservation(variant=variant, stock=stock)],
                lifetime_sales=estimate_lifetime_sales(product.review_count),
                signal=analyze_sales_signal(
                    None,
                    current_stock_quantity=stock.quantity,
                    current_stock_exact=True,
                    current_review_count=product.review_count,
                ),
                collected_at=collected_at,
            )

    statements: list[str] = []

    def capture_statement(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement.casefold())

    event.listen(engine, "before_cursor_execute", capture_statement)
    dataset = load_competitor_dataset(engine, plids={"101163999"})
    event.remove(engine, "before_cursor_execute", capture_statement)
    engine.dispose()

    for frame in (dataset.current, dataset.history, dataset.reviews, dataset.variants):
        assert set(frame["plid"].astype(str)) == {"101163999"}
    for table in (
        "competitor_snapshots",
        "competitor_reviews",
        "competitor_variant_snapshots",
    ):
        matching_queries = [
            statement
            for statement in statements
            if f"from {table}" in statement and statement.lstrip().startswith("select")
        ]
        assert matching_queries
        assert all(".plid in (" in statement for statement in matching_queries)


def test_latest_snapshot_classifies_only_explicit_follow_selling_opportunities(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'follow-opportunities.db').as_posix()}")
    create_schema(engine)
    collected_at = datetime(2026, 8, 7, 2, tzinfo=UTC)
    cases = (
        ("10000001", ()),
        (
            "10000002",
            (
                CompetitorOffer(
                    selected=True,
                    sku="SOLD-1",
                    seller_id="seller-1",
                    seller_name="Sold Out One",
                    price=100.0,
                    stock_status="Out of stock",
                    is_buybox=True,
                    is_add_to_cart_available=False,
                ),
                CompetitorOffer(
                    selected=False,
                    sku="SOLD-2",
                    seller_id="seller-2",
                    seller_name="Sold Out Two",
                    price=110.0,
                    stock_status="Sold out",
                    is_buybox=False,
                    is_add_to_cart_available=False,
                ),
            ),
        ),
        (
            "10000003",
            (
                CompetitorOffer(
                    selected=True,
                    sku="UNKNOWN-1",
                    seller_id="seller-3",
                    seller_name="Unknown Stock",
                    price=120.0,
                    stock_status="Status pending",
                    is_buybox=True,
                    is_add_to_cart_available=None,
                ),
            ),
        ),
    )
    skipped = skipped_stock_probe()
    for plid, offers in cases:
        product = CompetitorProduct(
            plid=plid,
            url=f"https://www.takealot.com/p/PLID{plid}",
            title=f"Opportunity {plid}",
            image_url=None,
            sku=f"SKU-{plid}",
            seller_id="",
            seller_name="",
            price=100.0,
            stock_status="Status pending",
            is_leadtime=False,
            review_count=0,
            rating=0.0,
            offers=offers,
            variants=(),
        )
        with Session(engine) as session, session.begin():
            CompetitorRepository(session).save_observation(
                product=product,
                reviews=[],
                review_summary=summarize_reviews([]),
                stock=skipped,
                variant_stocks=[],
                offer_stocks=[],
                lifetime_sales=estimate_lifetime_sales(0),
                signal=analyze_sales_signal(
                    None,
                    current_stock_quantity=None,
                    current_stock_exact=False,
                    current_review_count=0,
                ),
                collected_at=collected_at,
            )

    dataset = load_competitor_dataset(engine)
    engine.dispose()
    items = {row["plid"]: row for _, row in dataset.current.iterrows()}

    assert bool(items["10000001"]["跟卖机会"])
    assert items["10000001"]["跟卖机会类型"] == "暂无卖家报价"
    assert items["10000001"]["公开报价数"] == 0
    assert bool(items["10000002"]["跟卖机会"])
    assert items["10000002"]["跟卖机会类型"] == "全部报价售罄"
    assert items["10000002"]["公开报价数"] == 2
    assert not bool(items["10000003"]["跟卖机会"])
    unavailable_type = items["10000003"]["跟卖机会类型"]
    assert unavailable_type is None or unavailable_type != unavailable_type
    assert items["10000003"]["公开报价数"] == 1


def test_green_and_red_follower_stock_is_bound_to_its_exact_seller_offer(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'offer-stock.db').as_posix()}")
    create_schema(engine)
    url = "https://www.takealot.com/example/PLID12345678"
    variant = CompetitorVariant(
        key="default",
        label="默认款",
        url=url,
        title="Offer Stock",
        sku="SKU-MAIN",
        seller_id="seller-main",
        seller_name="Main Seller",
        price=100.0,
        stock_status="In stock",
        is_leadtime=False,
        is_add_to_cart_available=True,
    )
    main_offer = CompetitorOffer(
        selected=True,
        sku="SKU-MAIN",
        seller_id="seller-main",
        seller_name="Main Seller",
        price=100.0,
        stock_status="In stock",
        is_buybox=True,
        is_add_to_cart_available=True,
        plid="12345678",
        url=url,
        offer_id="main-offer",
    )
    follower_one = CompetitorOffer(
        selected=False,
        sku="SKU-ONE",
        seller_id="seller-one",
        seller_name="Seller One",
        price=105.0,
        stock_status="In stock",
        is_buybox=True,
        is_add_to_cart_available=True,
        plid="12345678",
        url=url,
        offer_id="other-buying-option-SKU-ONE",
        identity_key="offer:other-buying-option-sku-one",
        buybox_rank=1,
        is_follower_offer=True,
    )
    follower_two = replace(
        follower_one,
        sku="SKU-TWO",
        seller_id="seller-two",
        seller_name="Seller Two",
        price=110.0,
        offer_id="other-buying-option-SKU-TWO",
        identity_key="offer:other-buying-option-sku-two",
    )
    product = CompetitorProduct(
        plid="12345678",
        url=url,
        title="Offer Stock",
        image_url=None,
        sku=variant.sku,
        seller_id=variant.seller_id,
        seller_name=variant.seller_name,
        price=variant.price,
        stock_status=variant.stock_status,
        is_leadtime=False,
        review_count=0,
        rating=0.0,
        offers=(main_offer, follower_one, follower_two),
        variants=(variant,),
    )
    main_stock = StockProbeResult(7, True, "anonymous-cart-limit", "主报价库存")
    follower_one_stock = StockProbeResult(
        4,
        True,
        "anonymous-cart-limit",
        "卖家一库存",
    )
    follower_two_stock = StockProbeResult(
        2,
        True,
        "anonymous-cart-limit",
        "卖家二库存",
    )

    with Session(engine) as session, session.begin():
        snapshot = CompetitorRepository(session).save_observation(
            product=product,
            reviews=[],
            review_summary=summarize_reviews([]),
            stock=main_stock,
            variant_stocks=[VariantStockObservation(variant, main_stock)],
            offer_stocks=[
                OfferStockObservation(follower_one, follower_one_stock),
                OfferStockObservation(follower_two, follower_two_stock),
            ],
            lifetime_sales=estimate_lifetime_sales(0),
            signal=analyze_sales_signal(
                None,
                current_stock_quantity=main_stock.quantity,
                current_stock_exact=main_stock.exact,
                current_review_count=0,
            ),
            collected_at=datetime(2026, 8, 1, 8, tzinfo=UTC),
        )
        offers = {offer["offer_id"]: offer for offer in snapshot.offers or []}

    engine.dispose()
    assert offers["main-offer"]["stock_quantity"] == 7
    assert offers["other-buying-option-SKU-ONE"]["stock_quantity"] == 4
    assert offers["other-buying-option-SKU-TWO"]["stock_quantity"] == 2
    assert offers["other-buying-option-SKU-ONE"]["stock_note"] == "卖家一库存"
    assert offers["other-buying-option-SKU-TWO"]["stock_note"] == "卖家二库存"


def test_anonymous_buybox_history_uses_variant_sku_without_crossing_sellers(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'variant-history.db').as_posix()}")
    create_schema(engine)
    plid = "96909926"
    base_url = f"https://www.takealot.com/example/PLID{plid}"

    def variant(
        key: str,
        sku: str,
        price: float,
        *,
        seller_id: str = "",
        seller_name: str = "未知卖家",
        stock_status: str = "Supplier out of stock",
    ) -> CompetitorVariant:
        return CompetitorVariant(
            key=key,
            label=key,
            url=f"{base_url}?{key}",
            title="Knee Brace",
            sku=sku,
            seller_id=seller_id,
            seller_name=seller_name,
            price=price,
            stock_status=stock_status,
            is_leadtime=False,
            is_add_to_cart_available=stock_status == "In stock",
        )

    def offer(
        item: CompetitorVariant,
        *,
        selected: bool,
        include_identity: bool,
    ) -> CompetitorOffer:
        return CompetitorOffer(
            selected=selected,
            sku=item.sku if include_identity else "",
            seller_id=item.seller_id if include_identity else "",
            seller_name=item.seller_name if include_identity else "未知卖家",
            price=item.price,
            stock_status=item.stock_status,
            is_buybox=True,
            is_add_to_cart_available=item.is_add_to_cart_available,
            plid=plid,
            url=item.url,
            variant_key=item.key,
            variant_label=item.label,
        )

    right = variant("size=Right+Hand", "98109848", 450.0)
    left = variant("size=Left+Hand", "98109849", 529.0)
    other_seller_left = variant(
        "size=Left+Hand",
        "231601538",
        701.0,
        seller_id="29887827",
        seller_name="T C STORE",
        stock_status="In stock",
    )
    out_of_stock = StockProbeResult(0, True, "out-of-stock", "平台没货")
    seller_stock = StockProbeResult(4, True, "anonymous-cart-limit", "精确库存")

    observations = (
        (
            datetime(2026, 7, 29, 6, tzinfo=UTC),
            CompetitorProduct(
                plid=plid,
                url=base_url,
                title="Knee Brace",
                image_url=None,
                sku=right.sku,
                seller_id=right.seller_id,
                seller_name=right.seller_name,
                price=right.price,
                stock_status=right.stock_status,
                is_leadtime=False,
                review_count=0,
                rating=0.0,
                offers=(offer(right, selected=True, include_identity=False),),
                variants=(right, left),
            ),
            (
                VariantStockObservation(right, out_of_stock),
                VariantStockObservation(left, out_of_stock),
            ),
            out_of_stock,
        ),
        (
            datetime(2026, 8, 2, 5, tzinfo=UTC),
            CompetitorProduct(
                plid=plid,
                url=base_url,
                title="Knee Brace - Left Hand",
                image_url=None,
                sku=other_seller_left.sku,
                seller_id=other_seller_left.seller_id,
                seller_name=other_seller_left.seller_name,
                price=other_seller_left.price,
                stock_status=other_seller_left.stock_status,
                is_leadtime=False,
                review_count=0,
                rating=0.0,
                offers=(
                    offer(other_seller_left, selected=True, include_identity=True),
                ),
                variants=(other_seller_left,),
            ),
            (VariantStockObservation(other_seller_left, seller_stock),),
            seller_stock,
        ),
        (
            datetime(2026, 8, 3, 2, tzinfo=UTC),
            CompetitorProduct(
                plid=plid,
                url=base_url,
                title="Knee Brace",
                image_url=None,
                sku=left.sku,
                seller_id=left.seller_id,
                seller_name=left.seller_name,
                price=left.price,
                stock_status=left.stock_status,
                is_leadtime=False,
                review_count=0,
                rating=0.0,
                offers=(
                    offer(right, selected=False, include_identity=False),
                    offer(left, selected=True, include_identity=False),
                ),
                variants=(right, left),
            ),
            (
                VariantStockObservation(right, out_of_stock),
                VariantStockObservation(left, out_of_stock),
            ),
            out_of_stock,
        ),
    )
    for collected_at, product, variant_stocks, stock in observations:
        with Session(engine) as session, session.begin():
            CompetitorRepository(session).save_observation(
                product=product,
                reviews=[],
                review_summary=summarize_reviews([]),
                stock=stock,
                variant_stocks=list(variant_stocks),
                lifetime_sales=estimate_lifetime_sales(0),
                signal=analyze_sales_signal(
                    None,
                    current_stock_quantity=stock.quantity,
                    current_stock_exact=stock.exact,
                    current_review_count=0,
                ),
                collected_at=collected_at,
            )

    dataset = load_competitor_dataset(engine)
    engine.dispose()

    left_key = "variant-buybox:98109849|size=left+hand"
    current_offers = {
        item["报价键"]: item for item in dataset.current.iloc[0]["跟卖报价"]
    }
    assert current_offers[left_key]["SKU"] == "98109849"
    assert current_offers[left_key]["价格"] == 529.0
    assert current_offers[left_key]["库存数量"] == 0
    assert bool(current_offers[left_key]["库存精确"])
    assert current_offers[left_key]["区间起始价格"] == 529.0
    assert current_offers[left_key]["库存数量变化"] == 0

    history = {
        item["采集时间"].date(): {offer["报价键"]: offer for offer in item["跟卖报价"]}
        for _, item in dataset.history.iterrows()
    }
    assert history[date(2026, 7, 29)][left_key]["库存数量"] == 0
    assert left_key not in history[date(2026, 8, 2)]
    assert history[date(2026, 8, 2)][
        "fallback:29887827|231601538|size=left+hand|"
    ]["库存数量"] == 4
    assert history[date(2026, 8, 3)][left_key]["库存数量"] == 0


def test_own_store_product_is_separated_and_only_exposes_follower_offers(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'store-radar.db').as_posix()}")
    create_schema(engine)
    plid = "12345678"
    url = f"https://www.takealot.com/p/PLID{plid}"
    captured_at = datetime(2026, 8, 2, 1, tzinfo=UTC)
    buybox = CompetitorOffer(
        selected=True,
        sku="OWN-SKU",
        seller_id="seller-main",
        seller_name="Main Seller",
        price=120.0,
        stock_status="In stock",
        is_buybox=True,
        plid=plid,
        url=url,
        offer_id="own-offer",
    )
    follower = CompetitorOffer(
        selected=False,
        sku="FOLLOWER",
        seller_id="seller-follower",
        seller_name="Follower Seller",
        price=110.0,
        stock_status="In stock",
        is_buybox=False,
        plid=plid,
        url=url,
        offer_id="follower-offer",
        identity_key="offer:follower-offer",
        is_follower_offer=True,
    )
    product = CompetitorProduct(
        plid=plid,
        url=url,
        title="Own Store Product",
        image_url="https://example.invalid/own.jpg",
        sku="OWN-SKU",
        seller_id="seller-main",
        seller_name="Main Seller",
        price=120.0,
        stock_status="In stock",
        is_leadtime=False,
        review_count=8,
        rating=4.0,
        offers=(buybox, follower),
        variants=(),
    )
    stock = skipped_stock_probe()
    with Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="own-offer",
                productline_id=plid,
                title="Own Store Product",
                selling_price=100,
                status="disabled_by_seller",
                total_stock=7,
                captured_at=captured_at,
            )
        )
        session.add(
            StoreOfferBaseline(
                display_date=date(2026, 8, 2),
                offer_id="own-offer",
                productline_id=plid,
                sku="OWN-SKU",
                title="Own Store Product",
                image_url="https://example.invalid/own.jpg",
                selling_price=100,
                status="buyable",
                total_stock=7,
                captured_at=captured_at,
            )
        )
        session.add(
            StoreOfferObservation(
                display_date=date(2026, 8, 2),
                offer_id="own-offer",
                productline_id=plid,
                sku="OWN-SKU",
                title="Own Store Product",
                image_url="https://example.invalid/own.jpg",
                selling_price=95,
                status="buyable",
                total_stock=6,
                captured_at=datetime(2026, 8, 2, 2, tzinfo=UTC),
            )
        )
        session.add(
            StoreOfferBaseline(
                display_date=date(2026, 8, 2),
                offer_id="removed-own-offer",
                productline_id="99999999",
                sku="REMOVED-SKU",
                title="Removed Own Product",
                selling_price=80,
                status="disabled",
                total_stock=0,
                captured_at=captured_at,
            )
        )
        session.add(
            CompetitorTarget(
                plid=plid,
                offer_group_plid=plid,
                url=url,
                title="Legacy mixed target",
                active=True,
                created_at=captured_at,
                updated_at=captured_at,
            )
        )
        CompetitorRepository(session).save_observation(
            product=product,
            reviews=[],
            review_summary=summarize_reviews([]),
            stock=stock,
            variant_stocks=[],
            offer_stocks=[OfferStockObservation(follower, stock)],
            lifetime_sales=estimate_lifetime_sales(product.review_count),
            signal=analyze_sales_signal(
                None,
                current_stock_quantity=None,
                current_stock_exact=False,
                current_review_count=product.review_count,
            ),
            collected_at=captured_at,
            register_target=False,
        )

    dataset = load_competitor_dataset(engine)
    ranged_dataset = load_competitor_dataset(
        engine,
        start_date=date(2026, 8, 2),
        end_date=date(2026, 8, 2),
    )
    list_only_dataset = load_competitor_dataset(
        engine,
        include_detail_frames=False,
        own_store_only=True,
    )
    engine.dispose()

    assert dataset.current.empty
    assert len(dataset.store_current) == 1
    item = dataset.store_current.iloc[0]
    assert item["来源"] == "own_store"
    assert item["价格"] == 95.0
    assert item["区间起始价格"] == 100.0
    assert item["价格变化"] == -5.0
    assert item["价格信号"] == "降价"
    assert item["库存数量"] == 6
    assert item["库存净变化"] == -1
    assert item["库存净流出"] == 1
    assert item["周期销售件数"] == 1
    assert item["周期销售额"] == 95.0
    assert item["周期补货量"] == 0
    assert item["周期补货货值"] == 0.0
    assert item["周期库存周转金额"] == 95.0
    assert item["最新Offer状态"] == ["disabled_by_seller"]
    assert item["最新Offer状态更新时间"].date() == captured_at.date()
    assert item["自有报价"][0]["状态"] == "buyable"
    assert ranged_dataset.store_current.iloc[0]["最新Offer状态"] == [
        "disabled_by_seller"
    ]
    assert list_only_dataset.current.empty
    assert len(list_only_dataset.store_current) == 1
    assert list_only_dataset.history.empty
    assert list_only_dataset.reviews.empty
    assert list_only_dataset.variants.empty
    assert list_only_dataset.store_history.empty
    assert bool(item["库存可比"])
    assert item["跟卖发现日期"] == ["2026-08-02"]
    assert item["新增跟卖卖家数"] == 1
    assert item["新增跟卖卖家"] == ["Follower Seller"]
    assert item["跟卖卖家明细"][0]["首次发现日期"] == "2026-08-02"
    assert dataset.own_follower_events[0]["plid"] == plid
    assert dataset.own_follower_events[0]["跟卖发现日期"] == ["2026-08-02"]
    assert [offer["offer_id"] for offer in item["跟卖报价"]] == [
        "follower-offer"
    ]
    assert [offer["报价来源"] for offer in item["对比报价"]] == [
        "seller_api",
        "public_offer",
    ]
    assert item["对比报价"][0]["卖家"] == "当前店铺"
    assert item["对比报价"][0]["库存数量"] == 6
    assert item["对比报价"][0]["库存原始状态"] == "buyable"
    assert item["对比报价"][0]["最新Offer状态"] == "disabled_by_seller"
    assert item["对比报价"][0]["最新Offer状态更新时间"].date() == captured_at.date()
    assert item["对比报价"][0]["最新Offer库存数量"] == 7
    assert item["对比报价"][0]["最新Offer库存状态"] == "有货"
    assert (
        ranged_dataset.store_current.iloc[0]["对比报价"][0]["最新Offer状态"]
        == "disabled_by_seller"
    )
    assert (
        ranged_dataset.store_current.iloc[0]["对比报价"][0]["最新Offer库存状态"]
        == "有货"
    )
    seller_api_history = dataset.store_history.loc[
        dataset.store_history["评论数可用"].eq(False)
    ]
    assert len(seller_api_history) == 2


def test_own_store_product_without_public_snapshot_waits_for_first_check(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'store-waiting.db').as_posix()}")
    create_schema(engine)
    captured_at = datetime(2026, 8, 4, 1, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="own-waiting",
                productline_id="87654321",
                title="Waiting Product",
                selling_price=100,
                total_stock=5,
                captured_at=captured_at,
            )
        )
        session.add(
            StoreOfferBaseline(
                display_date=date(2026, 8, 4),
                offer_id="own-waiting",
                productline_id="87654321",
                sku="WAITING-SKU",
                title="Waiting Product",
                selling_price=100,
                status="buyable",
                total_stock=5,
                captured_at=captured_at,
            )
        )

    dataset = load_competitor_dataset(engine)
    engine.dispose()

    item = dataset.store_current.iloc[0]
    assert item["趋势判断"] == "等待首次检查"
    assert "等待后台轮巡首次检查" in item["判断说明"]


def test_own_store_follower_history_keeps_dates_after_seller_disappears(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'store-follower-history.db').as_posix()}")
    create_schema(engine)
    plid = "33334444"
    url = f"https://www.takealot.com/p/PLID{plid}"
    own_offer = CompetitorOffer(
        selected=True,
        sku="OWN-HISTORY-SKU",
        seller_id="own-history",
        seller_name="Own History Store",
        price=200.0,
        stock_status="In stock",
        is_buybox=True,
        plid=plid,
        url=url,
        offer_id="own-history-offer",
    )
    follower_offer = CompetitorOffer(
        selected=False,
        sku="FOLLOWER-HISTORY-SKU",
        seller_id="follower-history",
        seller_name="History Follower",
        price=190.0,
        stock_status="In stock",
        is_buybox=False,
        plid=plid,
        url=url,
        offer_id="follower-history-offer",
        identity_key="offer:follower-history-offer",
        is_follower_offer=True,
    )
    captured_at = datetime(2026, 8, 1, 1, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="own-history-offer",
                productline_id=plid,
                title="Follower History Product",
                selling_price=200,
                total_stock=9,
                captured_at=captured_at,
            )
        )
        session.add(
            StoreOfferBaseline(
                display_date=date(2026, 8, 1),
                offer_id="own-history-offer",
                productline_id=plid,
                sku="OWN-HISTORY-SKU",
                title="Follower History Product",
                selling_price=200,
                status="buyable",
                total_stock=9,
                captured_at=captured_at,
            )
        )

    for day, offers in (
        (1, (own_offer,)),
        (2, (own_offer, follower_offer)),
        (3, (own_offer,)),
    ):
        collected_at = datetime(2026, 8, day, 1, tzinfo=UTC)
        product = CompetitorProduct(
            plid=plid,
            url=url,
            title="Follower History Product",
            image_url=None,
            sku="OWN-HISTORY-SKU",
            seller_id="own-history",
            seller_name="Own History Store",
            price=200.0,
            stock_status="In stock",
            is_leadtime=False,
            review_count=0,
            rating=0.0,
            offers=offers,
            variants=(),
        )
        with Session(engine) as session, session.begin():
            CompetitorRepository(session).save_observation(
                product=product,
                reviews=[],
                review_summary=summarize_reviews([]),
                stock=skipped_stock_probe(),
                variant_stocks=[],
                offer_stocks=(
                    [OfferStockObservation(follower_offer, skipped_stock_probe())]
                    if day == 2
                    else []
                ),
                lifetime_sales=estimate_lifetime_sales(0),
                signal=analyze_sales_signal(
                    None,
                    current_stock_quantity=None,
                    current_stock_exact=False,
                    current_review_count=0,
                ),
                collected_at=collected_at,
                register_target=False,
            )

    full_range = load_competitor_dataset(
        engine,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 3),
    )
    disappeared_range = load_competitor_dataset(
        engine,
        start_date=date(2026, 8, 3),
        end_date=date(2026, 8, 3),
    )
    engine.dispose()

    full_item = full_range.store_current.iloc[0]
    assert full_item["跟卖报价"] == []
    assert full_item["跟卖发现日期"] == ["2026-08-02"]
    assert full_item["新增跟卖卖家"] == ["History Follower"]
    assert full_range.own_follower_events[0]["跟卖发现日期"] == ["2026-08-02"]
    assert full_range.own_follower_events[0]["跟卖卖家明细"][0]["是否区间新增"] is True
    assert disappeared_range.own_follower_events == []


def test_own_store_without_followers_still_exposes_reviews_and_variants(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'store-public-detail.db').as_posix()}")
    create_schema(engine)
    plid = "96909926"
    url = f"https://www.takealot.com/p/PLID{plid}"
    captured_at = datetime(2026, 8, 4, 4, tzinfo=UTC)
    variant = CompetitorVariant(
        key="side=right",
        label="Side：Right Hand",
        url=url,
        title="Knee Brace - Right Hand",
        image_url=None,
        sku="PUBLIC-RIGHT",
        seller_id="seller-main",
        seller_name="Main Seller",
        price=699,
        stock_status="In stock",
        is_leadtime=False,
        is_add_to_cart_available=True,
    )
    product = CompetitorProduct(
        plid=plid,
        url=url,
        title="Knee Brace - Right Hand",
        image_url=None,
        sku="PUBLIC-RIGHT",
        seller_id="seller-main",
        seller_name="Main Seller",
        price=699,
        stock_status="In stock",
        is_leadtime=False,
        review_count=2,
        rating=4.5,
        offers=(),
        variants=(variant,),
    )
    reviews = [
        CompetitorReviewRecord("review-1", 5, "Great", "Good", "A", "2026-08-01"),
        CompetitorReviewRecord("review-2", 4, "Useful", "Works", "B", "2026-08-02"),
    ]
    skipped = skipped_stock_probe()
    with Session(engine) as session, session.begin():
        session.add(
            OfferCurrent(
                offer_id="own-right",
                productline_id=plid,
                title=product.title,
                selling_price=699,
                total_stock=0,
                captured_at=captured_at,
            )
        )
        session.add(
            StoreOfferBaseline(
                display_date=date(2026, 8, 4),
                offer_id="own-right",
                productline_id=plid,
                sku="OWN-RIGHT",
                title=product.title,
                selling_price=699,
                status="not_buyable",
                total_stock=0,
                captured_at=captured_at,
            )
        )
        CompetitorRepository(session).save_observation(
            product=product,
            reviews=reviews,
            review_summary=summarize_reviews(reviews),
            stock=skipped,
            variant_stocks=[VariantStockObservation(variant, skipped)],
            lifetime_sales=estimate_lifetime_sales(product.review_count),
            signal=analyze_sales_signal(
                None,
                current_stock_quantity=None,
                current_stock_exact=False,
                current_review_count=product.review_count,
            ),
            collected_at=captured_at,
            register_target=False,
        )

    dataset = load_competitor_dataset(engine)
    engine.dispose()

    item = dataset.store_current.iloc[0]
    assert item["趋势判断"] == "暂未发现跟卖"
    assert item["评论数"] == 2
    assert item["评分"] == 4.5
    assert item["好评"] == 2
    assert "首次检查或评论数变化" in item["共享评论说明"]
    assert len(dataset.reviews) == 2
    assert len(dataset.variants) == 1
    assert "未执行购物车库存探测" in dataset.variants.iloc[0]["库存说明"]
    assert len(dataset.store_history) == 2
    baseline_history = dataset.store_history.loc[
        dataset.store_history["评论数可用"].eq(False)
    ].iloc[0]
    assert baseline_history["对比报价"][0]["报价来源"] == "seller_api"


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
                is_buybox=True,
                is_add_to_cart_available=True,
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
                stock_status="Out of stock",
                plid="12345678",
                url="https://www.takealot.com/example/PLID12345678",
                offer_id="offer-up",
                is_follower_offer=True,
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
        (
            datetime(2026, 7, 22, 8, tzinfo=UTC),
            10,
            10,
            6,
            2,
            2,
            220.0,
            180.0,
            "Out of stock",
        ),
        (
            datetime(2026, 7, 23, 8, tzinfo=UTC),
            8,
            11,
            7,
            2,
            2,
            210.0,
            190.0,
            "Out of stock",
        ),
        (
            datetime(2026, 7, 24, 8, tzinfo=UTC),
            4,
            13,
            8,
            2,
            3,
            200.0,
            210.0,
            "In stock",
        ),
    )
    for (
        collected_at,
        quantity,
        review_count,
        positive_reviews,
        neutral_reviews,
        negative_reviews,
        price,
        other_price,
        other_stock_status,
    ) in observations:
        product = replace(
            base_product,
            review_count=review_count,
            price=price,
            offers=(
                replace(base_product.offers[0], price=price),
                replace(
                    base_product.offers[1],
                    price=other_price,
                    stock_status=other_stock_status,
                ),
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
                review_summary=ReviewSummary(
                    total=review_count,
                    positive=positive_reviews,
                    neutral=neutral_reviews,
                    negative=negative_reviews,
                ),
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
    assert all_signal["周期销售件数"] == 6
    assert all_signal["周期销售额"] == 1220.0
    assert all_signal["周期补货量"] == 0
    assert all_signal["周期补货货值"] == 0.0
    assert all_signal["周期库存周转金额"] == 1220.0
    assert all_signal["新增评论"] == 3
    assert all_signal["新增好评"] == 2
    assert all_signal["新增差评"] == 1
    assert all_signal["趋势判断"] == "两个独立正向信号"
    assert all_signal["观察期销量信号"] == "60–150"
    assert all_signal["区间快照数"] == 3
    assert all_signal["区间起始价格"] == 220.0
    assert all_signal["价格变化"] == -20.0
    assert all_signal["价格信号"] == "降价"
    assert all_signal["跟卖发现日期"] == [
        "2026-07-22",
        "2026-07-23",
        "2026-07-24",
    ]
    assert all_signal["新增跟卖卖家"] == ["Other Seller"]
    all_offers = {offer["offer_id"]: offer for offer in all_signal["跟卖报价"]}
    assert all_offers["offer-down"]["价格变化"] == -20.0
    assert all_offers["offer-down"]["价格信号"] == "降价"
    assert all_offers["offer-down"]["库存数量"] == 4
    assert all_offers["offer-down"]["区间起始库存数量"] == 10
    assert all_offers["offer-down"]["库存数量变化"] == -6
    assert all_offers["offer-down"]["库存信号"] == "库存减少"
    assert bool(all_offers["offer-down"]["库存精确"])
    assert all_offers["offer-up"]["区间起始价格"] == 180.0
    assert all_offers["offer-up"]["价格变化"] == 30.0
    assert all_offers["offer-up"]["价格信号"] == "涨价"
    assert all_offers["offer-up"]["库存状态"] == "有货"
    assert all_offers["offer-up"]["区间起始库存状态"] == "没货"
    assert all_offers["offer-up"]["库存数量"] is None
    assert all_offers["offer-up"]["区间起始库存数量"] is None
    assert all_offers["offer-up"]["库存信号"] == "恢复有货"
    assert not bool(all_offers["offer-up"]["库存精确"])
    assert "不补 0" in str(all_offers["offer-up"]["库存说明"])
    assert bool(all_signal["库存可比"])
    assert all_range.available_start_date == date(2026, 7, 22)
    assert all_range.available_end_date == date(2026, 7, 24)
    assert set(all_range.history["趋势判断"]) == {"原始快照"}
    assert all_range.history["库存净流出"].isna().all()

    recent_signal = recent_range.current.iloc[0]
    assert recent_signal["库存净变化"] == -4
    assert recent_signal["库存净流出"] == 4
    assert recent_signal["周期销售件数"] == 4
    assert recent_signal["周期销售额"] == 800.0
    assert recent_signal["周期补货量"] == 0
    assert recent_signal["周期补货货值"] == 0.0
    assert recent_signal["周期库存周转金额"] == 800.0
    assert recent_signal["新增评论"] == 2
    assert recent_signal["新增好评"] == 1
    assert recent_signal["新增差评"] == 1
    assert recent_signal["观察期销量信号"] == "40–100"
    assert recent_signal["区间快照数"] == 2
    assert recent_signal["区间起始价格"] == 210.0
    assert recent_signal["价格变化"] == -10.0
    assert recent_signal["价格信号"] == "降价"
    assert recent_signal["新增跟卖卖家数"] == 0
    assert recent_signal["跟卖卖家明细"][0]["是否区间新增"] is False
    recent_offers = {offer["offer_id"]: offer for offer in recent_signal["跟卖报价"]}
    assert recent_offers["offer-down"]["价格变化"] == -10.0
    assert recent_offers["offer-down"]["库存数量变化"] == -4
    assert recent_offers["offer-up"]["价格变化"] == 20.0
    assert recent_offers["offer-up"]["库存信号"] == "恢复有货"
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
