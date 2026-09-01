"""Evidence-bounded container-fill selection for non-electrified bulky products."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from sqlalchemy import or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from takealot_ops.competitors.own_store_sales import (
    build_own_store_sales_series_bulk,
)
from takealot_ops.competitors.service import _snapshot_recent_observed_sales_units
from takealot_ops.exchange_rates import ExchangeRateQuote
from takealot_ops.product_master import normalize_product_sku
from takealot_ops.profitability import load_own_store_profitability_bulk
from takealot_ops.storage.models import (
    CompanyProduct,
    CompetitorReview,
    CompetitorSnapshot,
    CompetitorTarget,
    CompetitorTargetAudit,
    CompetitorVariantSnapshot,
    ErpStore,
    OfferCurrent,
    PlatformSkuMapping,
    StoreOfferObservation,
)
from takealot_ops.storage.store_context import normalize_store_code, store_scope


CONFIG_RELATIVE_PATH = Path("config") / "container_selection.json"
RATE_CACHE_RELATIVE_PATH = (
    Path("data") / "runtime-cache" / "exchange-rates" / "cny-zar.json"
)

RADAR_ROLE_LABELS = {
    "low_price": "候选池低价代表",
    "high_price": "候选池高价代表",
    "most_reviewed": "候选池累计评论最多（仅市场结构）",
    "mid_market": "候选池中位价格代表",
    "recent_stock_mover": "近30天库存流出代表",
    "recent_review_grower": "近30天新评论代表",
    "recent_signal_backup": "近30天动销补充样本",
    "workbook_economics_anchor": "工作簿利润与体积锚点",
    "workbook_secondary_anchor": "工作簿补充监控链接",
}


class ContainerSelectionConfigError(ValueError):
    """Raised when the versioned selection profile is missing or malformed."""


@dataclass(frozen=True)
class ContainerSelectionImportResult:
    """Summary of one idempotent competitor-target import."""

    batch_id: str
    configured_count: int
    added_count: int
    reactivated_count: int
    existing_count: int
    new_targets: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _CachedRateProvider:
    quote: ExchangeRateQuote

    def latest(self) -> ExchangeRateQuote:
        return self.quote


def load_container_selection_config(project_root: Path) -> dict[str, Any]:
    """Load and validate the versioned category-portfolio selection profile."""
    path = project_root / CONFIG_RELATIVE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContainerSelectionConfigError("配柜选品配置暂不可用") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        raise ContainerSelectionConfigError("配柜选品配置版本不受支持")
    batch_id = str(payload.get("selection_batch_id") or "").strip()
    policy = payload.get("policy")
    own_profiles = payload.get("own_profiles")
    radar_categories = payload.get("radar_categories")
    retained_watchlist = payload.get("retained_watchlist")
    if (
        not batch_id
        or not isinstance(policy, dict)
        or not isinstance(own_profiles, list)
        or not isinstance(radar_categories, list)
        or not isinstance(retained_watchlist, list)
    ):
        raise ContainerSelectionConfigError("配柜选品配置缺少必要字段")
    minimum_representatives = max(
        4,
        int(policy.get("minimum_representatives_per_category") or 4),
    )
    minimum_unit_cbm = float(policy.get("new_min_unit_cbm") or 0)
    seen_categories: set[str] = set()
    seen_plids: set[str] = set()
    for category in radar_categories:
        if not isinstance(category, dict):
            raise ContainerSelectionConfigError("新品类目组合格式无效")
        category_id = str(category.get("category_id") or "").strip()
        representatives = category.get("representatives")
        anchor = category.get("economics_anchor")
        cohort_basis = category.get("cohort_basis")
        if (
            not category_id
            or category_id in seen_categories
            or not isinstance(representatives, list)
            or len(representatives) < minimum_representatives
            or not isinstance(anchor, dict)
            or not isinstance(cohort_basis, dict)
            or cohort_basis.get("extremes_are_sample_relative") is not True
        ):
            raise ContainerSelectionConfigError("新品类目组合缺少必要字段或代表链接不足")
        seen_categories.add(category_id)
        anchor_cbm = _number(anchor.get("unit_cbm")) or 0
        anchor_profit = _number(anchor.get("sea_profit_rmb")) or 0
        if (
            anchor_cbm < minimum_unit_cbm
            or anchor_profit <= 0
            or not str(anchor.get("electrical_evidence") or "").strip()
        ):
            raise ContainerSelectionConfigError("新品类目利润、体积或非带电证据无效")
        for representative in representatives:
            if not isinstance(representative, dict):
                raise ContainerSelectionConfigError("新品代表链接格式无效")
            roles = representative.get("roles")
            if (
                not isinstance(roles, list)
                or not roles
                or any(str(role) not in RADAR_ROLE_LABELS for role in roles)
            ):
                raise ContainerSelectionConfigError("新品代表链接角色无效")
            plid = _normalized_plid(representative.get("plid"))
            url = str(representative.get("url") or "").strip()
            if (
                not plid
                or plid in seen_plids
                or not _is_matching_takealot_url(url, plid)
            ):
                raise ContainerSelectionConfigError("新品代表链接 PLID 或链接无效")
            seen_plids.add(plid)
    for candidate in retained_watchlist:
        if not isinstance(candidate, dict):
            raise ContainerSelectionConfigError("留观链接格式无效")
        plid = _normalized_plid(candidate.get("plid"))
        url = str(candidate.get("url") or "").strip()
        if not plid or plid in seen_plids or not _is_matching_takealot_url(url, plid):
            raise ContainerSelectionConfigError("留观链接 PLID 或链接无效")
        seen_plids.add(plid)
    return payload


def load_container_selection_payload(
    project_root: Path,
    engine: Engine,
    *,
    store_codes: set[str],
    as_of: date,
) -> dict[str, Any]:
    """Build the complete read-only workbench from local MySQL and versioned inputs."""
    config = load_container_selection_config(project_root)
    normalized_codes = {
        normalize_store_code(value)
        for value in store_codes
        if str(value or "").strip()
    }
    own_items, rate_payload = _build_own_replenishment_items(
        project_root,
        engine,
        profiles=config["own_profiles"],
        policy=config["policy"],
        store_codes=normalized_codes,
        as_of=as_of,
    )
    configured_radar_links = [
        representative
        for category in config["radar_categories"]
        for representative in category["representatives"]
    ]
    configured_retained_links = [
        {**candidate, "roles": []}
        for candidate in config["retained_watchlist"]
    ]
    radar_evidence = _load_radar_link_evidence(
        engine,
        configured=[*configured_radar_links, *configured_retained_links],
        policy=config["policy"],
        as_of=as_of,
    )
    radar_categories = _build_radar_categories(
        engine,
        categories=config["radar_categories"],
        policy=config["policy"],
        as_of=as_of,
        evidence=radar_evidence,
    )
    retained_watchlist = _build_retained_watchlist(
        engine,
        candidates=config["retained_watchlist"],
        policy=config["policy"],
        as_of=as_of,
        evidence=radar_evidence,
    )
    recommended = [
        item
        for item in own_items
        if item["recommendation"]["status"] == "replenish"
    ]
    radar_links = [
        representative
        for category in radar_categories
        for representative in category["representatives"]
    ]
    recent_hot = [
        category
        for category in radar_categories
        if category["monitoring"]["status"] == "recent_hot"
    ]
    opening_review = [
        category
        for category in radar_categories
        if category["decision"]["status"] == "opening_review"
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of": as_of.isoformat(),
        "date_basis": "Asia/Shanghai",
        "selection_batch_id": config["selection_batch_id"],
        "scope": {
            "store_codes": sorted(normalized_codes),
            "store_count": len(normalized_codes),
            "label": "全部授权且已接入店铺",
        },
        "policy": config["policy"],
        "exchange_rate": rate_payload,
        "summary": {
            "own_profile_count": len(own_items),
            "replenishment_count": len(recommended),
            "recommended_units": sum(
                int(item["recommendation"]["recommended_units"] or 0)
                for item in recommended
            ),
            "recommended_cbm": round(
                sum(float(item["recommendation"]["recommended_cbm"] or 0) for item in recommended),
                3,
            ),
            "radar_category_count": len(radar_categories),
            "radar_link_count": len(radar_links),
            "radar_active_link_count": sum(
                bool(item["monitoring"]["active"])
                for item in radar_links
            ),
            "radar_recent_hot_category_count": len(recent_hot),
            "radar_opening_review_count": len(opening_review),
            "radar_waiting_category_count": len(radar_categories) - len(recent_hot),
            "retained_watchlist_count": len(retained_watchlist),
        },
        "replenishment_items": own_items,
        "radar_categories": radar_categories,
        "retained_watchlist": retained_watchlist,
        "evidence_notes": [
            "自有销量来自 Seller Sales /sales；缺失日期不补 0，先按店铺、PLID、Offer 链路计算再汇总。",
            "补货只用近30天有效覆盖决定动销和数量；前30天只作趋势对比，90天销量只作背景，不能把近期疲软托成热销。",
            "平台可售、卖家可售、收货中和在途库存分开保留；补货量只把可售与明确在途/收货中扣除。",
            "新品累计评论数只用于价格带和市场层级；动销资格只看近30天有明确日期的评论、近30天库存流出及其多链接覆盖。",
            "每个新品类目至少监控低价、高价、累计评论最多、中位价格和近期动销代表；所有极值都只表示该候选池，不冒充全平台极值。",
            "工作簿评论文字不参与近期热销判定；工作簿缓存利润与箱规只用于利润、体积初筛，正式采购前必须更新报价。",
            "公开库存下降与评论增量只作为动销信号，不等同于订单；未取得精确库存时不推断数量。",
            "非带电判定遵循无插头、无随货电池；一次性干电池方案必须明确不随货并在装柜前复核包装清单。",
        ],
    }


def import_container_selection_targets(
    project_root: Path,
    engine: Engine,
    *,
    actor_username: str,
    actor_display_name: str,
    changed_at: datetime | None = None,
) -> ContainerSelectionImportResult:
    """Idempotently add the configured new-product candidates to the global radar."""
    config = load_container_selection_config(project_root)
    username = actor_username.strip()
    display_name = actor_display_name.strip()
    if not username or len(username) > 64 or not display_name or len(display_name) > 100:
        raise ContainerSelectionConfigError("导入审计身份无效")
    now = changed_at or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        now = now.replace(tzinfo=UTC)
    else:
        now = now.astimezone(UTC)

    added_count = 0
    reactivated_count = 0
    existing_count = 0
    new_targets: list[tuple[str, str]] = []
    configured_targets = _configured_import_targets(config)
    with Session(engine) as session, session.begin():
        for candidate in configured_targets:
            plid = _normalized_plid(candidate.get("plid"))
            url = str(candidate.get("url") or "").strip()
            target = session.get(CompetitorTarget, plid)
            if target is not None and target.active:
                existing_count += 1
                continue
            old_url = target.url if target is not None else None
            if target is None:
                target = CompetitorTarget(
                    plid=plid,
                    offer_group_plid=plid,
                    url=url,
                    title=str(candidate.get("name") or f"PLID{plid}"),
                    active=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(target)
                added_count += 1
            else:
                target.offer_group_plid = target.offer_group_plid or plid
                target.url = url
                target.title = target.title or str(candidate.get("name") or f"PLID{plid}")
                target.active = True
                target.updated_at = now
                reactivated_count += 1
            session.add(
                CompetitorTargetAudit(
                    plid=plid,
                    action="add",
                    old_url=old_url,
                    new_url=url,
                    actor_user_id=None,
                    actor_username=username,
                    actor_display_name=display_name,
                    changed_at=now,
                )
            )
            new_targets.append((plid, url))
    return ContainerSelectionImportResult(
        batch_id=str(config["selection_batch_id"]),
        configured_count=len(configured_targets),
        added_count=added_count,
        reactivated_count=reactivated_count,
        existing_count=existing_count,
        new_targets=tuple(new_targets),
    )


def _configured_import_targets(config: Mapping[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for category in config["radar_categories"]:
        anchor = category["economics_anchor"]
        anchor_plid = _normalized_plid(anchor.get("plid"))
        for representative in category["representatives"]:
            plid = _normalized_plid(representative.get("plid"))
            name = (
                str(anchor.get("name") or "").strip()
                if plid == anchor_plid
                else f"{category['category_name']} · PLID{plid}"
            )
            result.append(
                {
                    "plid": plid,
                    "url": str(representative.get("url") or "").strip(),
                    "name": name or f"PLID{plid}",
                }
            )
    for candidate in config["retained_watchlist"]:
        result.append(
            {
                "plid": _normalized_plid(candidate.get("plid")),
                "url": str(candidate.get("url") or "").strip(),
                "name": str(candidate.get("name") or "").strip()
                or f"PLID{_normalized_plid(candidate.get('plid'))}",
            }
        )
    return result


def _build_own_replenishment_items(
    project_root: Path,
    engine: Engine,
    *,
    profiles: list[dict[str, Any]],
    policy: Mapping[str, Any],
    store_codes: set[str],
    as_of: date,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rate_provider, rate_payload = _load_local_rate(project_root)
    window_days = max(30, int(policy.get("sales_window_days") or 90))
    window_start = as_of - timedelta(days=window_days - 1)
    minimum_known_days = max(1, int(policy.get("minimum_known_days_per_link") or 14))
    recent_window_days = max(7, int(policy.get("recent_sales_window_days") or 30))
    comparison_window_days = max(
        7,
        int(policy.get("comparison_sales_window_days") or 30),
    )
    target_cover_days = max(1, int(policy.get("replenishment_cover_days") or 30))
    clearance_days = max(1, int(policy.get("clearance_window_days") or 30))

    if not store_codes:
        return (
            [
                _empty_own_item(
                    profile,
                    as_of=as_of,
                    window_start=window_start,
                    reason="当前账号没有可用于补货分析的已接入店铺。",
                )
                for profile in profiles
            ],
            rate_payload,
        )

    with Session(engine) as session:
        store_names = {
            str(store.code): str(store.display_name)
            for store in session.scalars(
                select(ErpStore).where(ErpStore.code.in_(sorted(store_codes)))
            )
        }
        products = list(session.scalars(select(CompanyProduct)))
        product_by_sku = {
            product.normalized_company_sku: product
            for product in products
        }
        prepared: list[dict[str, Any]] = []
        all_platform_skus: set[str] = set()
        all_plids: set[str] = set()
        for profile in profiles:
            company_sku = str(profile.get("company_sku") or "").strip()
            product = product_by_sku.get(normalize_product_sku(company_sku))
            mappings = (
                list(
                    session.scalars(
                        select(PlatformSkuMapping)
                        .where(PlatformSkuMapping.company_product_id == product.id)
                        .order_by(PlatformSkuMapping.platform_sku)
                    )
                )
                if product is not None
                else []
            )
            platform_skus = {
                str(mapping.platform_sku).strip()
                for mapping in mappings
                if str(mapping.platform_sku or "").strip()
            }
            plids = {
                str(mapping.resolved_productline_id).strip()
                for mapping in mappings
                if str(mapping.resolved_productline_id or "").strip()
            }
            all_platform_skus.update(platform_skus)
            all_plids.update(plids)
            prepared.append(
                {
                    "profile": profile,
                    "product": product,
                    "platform_skus": platform_skus,
                    "plids": plids,
                }
            )

        offers_by_store: dict[str, list[OfferCurrent]] = {}
        observations_by_link: dict[tuple[str, str], dict[date, bool]] = defaultdict(dict)
        for store_code in sorted(store_codes):
            with store_scope(store_code):
                offer_predicates = []
                if all_plids:
                    offer_predicates.append(OfferCurrent.productline_id.in_(sorted(all_plids)))
                if all_platform_skus:
                    offer_predicates.append(OfferCurrent.sku.in_(sorted(all_platform_skus)))
                offers = (
                    list(session.scalars(select(OfferCurrent).where(or_(*offer_predicates))))
                    if offer_predicates
                    else []
                )
                offers_by_store[store_code] = offers
                observed_plids = {
                    str(offer.productline_id).strip()
                    for offer in offers
                    if str(offer.productline_id or "").strip()
                } | all_plids
                observations = (
                    list(
                        session.scalars(
                            select(StoreOfferObservation).where(
                                StoreOfferObservation.productline_id.in_(sorted(observed_plids)),
                                StoreOfferObservation.display_date >= window_start,
                                StoreOfferObservation.display_date <= as_of,
                            )
                        )
                    )
                    if observed_plids
                    else []
                )
            for offer in offers:
                if offer.productline_id:
                    all_plids.add(str(offer.productline_id).strip())
            for observation in observations:
                plid = str(observation.productline_id or "").strip()
                if not plid:
                    continue
                key = (store_code, plid)
                is_buyable = _offer_is_buyable(
                    observation.status,
                    observation.total_stock,
                    observation.takealot_available_stock,
                    observation.seller_available_stock,
                )
                observations_by_link[key][observation.display_date] = (
                    observations_by_link[key].get(observation.display_date, False)
                    or is_buyable
                )

        sales_series_by_plid = build_own_store_sales_series_bulk(
            session,
            plids=all_plids,
            store_codes=store_codes,
            through=as_of,
        )
        profitability_by_plid = (
            load_own_store_profitability_bulk(
                engine,
                plids=all_plids,
                store_codes=store_codes,
                rate_service=rate_provider,
                cost_as_of=as_of,
                fee_window_end=as_of,
            )
            if rate_provider is not None
            else {}
        )
        items: list[dict[str, Any]] = []
        for entry in prepared:
            profile = entry["profile"]
            product = entry["product"]
            platform_skus = entry["platform_skus"]
            plids = set(entry["plids"])
            for offers in offers_by_store.values():
                plids.update(
                    str(offer.productline_id).strip()
                    for offer in offers
                    if offer.sku in platform_skus and str(offer.productline_id or "").strip()
                )
            links: list[dict[str, Any]] = []
            profit_items: list[dict[str, Any]] = []
            for plid in sorted(plids):
                series_rows = sales_series_by_plid.get(plid, [])
                for series in series_rows:
                    links.append(
                        _own_link_payload(
                            series,
                            store_name=store_names.get(series["store_code"], series["store_code"]),
                            current_offers=[
                                offer
                                for offer in offers_by_store.get(series["store_code"], [])
                                if str(offer.productline_id or "").strip() == plid
                            ],
                            exposure=observations_by_link.get((series["store_code"], plid), {}),
                            as_of=as_of,
                            window_start=window_start,
                            minimum_known_days=minimum_known_days,
                            recent_window_days=recent_window_days,
                            comparison_window_days=comparison_window_days,
                        )
                    )
                if rate_provider is not None:
                    profit_items.extend(
                        profitability_by_plid.get(plid, {}).get("items", [])
                    )
            items.append(
                _own_profile_payload(
                    profile,
                    product_name=(str(product.product_name) if product is not None else None),
                    plids=sorted(plids),
                    links=links,
                    profit_items=profit_items,
                    rate_available=rate_provider is not None,
                    as_of=as_of,
                    window_start=window_start,
                    target_cover_days=target_cover_days,
                    clearance_days=clearance_days,
                    minimum_known_days=minimum_known_days,
                    minimum_recent_monthly_units=float(
                        policy.get("minimum_recent_monthly_units") or 3
                    ),
                    strong_recent_monthly_units=float(
                        policy.get("strong_recent_monthly_units") or 6
                    ),
                    maximum_recent_decline_ratio=float(
                        policy.get("maximum_recent_decline_ratio") or 0.2
                    ),
                )
            )

    items.sort(
        key=lambda item: (
            item["recommendation"]["status"] == "replenish",
            float(item["priority_score"]),
            float(item["sales"]["forecast_monthly_units"] or 0),
        ),
        reverse=True,
    )
    for rank, item in enumerate(items, start=1):
        item["rank"] = rank
    return items, rate_payload


def _own_offer_image_url(current_offers: list[OfferCurrent]) -> str | None:
    ordered_offers = sorted(
        current_offers,
        key=lambda offer: (
            not _offer_is_buyable(
                offer.status,
                offer.total_stock,
                offer.takealot_available_stock,
                offer.seller_available_stock,
            ),
            str(offer.offer_id or ""),
        ),
    )
    for offer in ordered_offers:
        image_url = str(offer.image_url or "").strip()
        if image_url:
            return image_url
    return None


def _own_link_payload(
    series: Mapping[str, Any],
    *,
    store_name: str,
    current_offers: list[OfferCurrent],
    exposure: Mapping[date, bool],
    as_of: date,
    window_start: date,
    minimum_known_days: int,
    recent_window_days: int,
    comparison_window_days: int,
) -> dict[str, Any]:
    points = [
        point
        for point in series.get("points", [])
        if window_start.isoformat() <= str(point.get("date") or "") <= as_of.isoformat()
    ]
    recent_start = as_of - timedelta(days=recent_window_days - 1)
    previous_end = recent_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=comparison_window_days - 1)
    window = _point_window(points, window_start, as_of)
    recent = _point_window(points, recent_start, as_of)
    previous = _point_window(points, previous_start, previous_end)
    raw_velocity = _monthly_rate(window["units"], window["known_days"])
    recent_velocity = _monthly_rate(recent["units"], recent["known_days"])
    previous_velocity = _monthly_rate(previous["units"], previous["known_days"])
    observed_days = len(exposure)
    buyable_days = sum(bool(value) for value in exposure.values())
    exposure_ratio = buyable_days / observed_days if observed_days >= minimum_known_days else None
    adjusted_velocity = raw_velocity
    if exposure_ratio is not None and 0 < exposure_ratio < 1:
        adjusted_velocity = min(raw_velocity / max(exposure_ratio, 0.2), raw_velocity * 3)
    recent_data_ready = recent["known_days"] >= minimum_known_days
    previous_data_ready = previous["known_days"] >= minimum_known_days
    forecast = recent_velocity if recent_data_ready else 0.0

    inventory = _link_inventory(current_offers)
    listing_date = _date_or_none(series.get("listing_date"))
    listing_age_days = (as_of - listing_date).days + 1 if listing_date else None
    if window["known_days"] < minimum_known_days or (listing_age_days is not None and listing_age_days < 30):
        lifecycle = "cold_start"
        lifecycle_label = "冷启动/覆盖不足"
    elif not inventory["sellable_now"]:
        lifecycle = "unavailable"
        lifecycle_label = "当前不可售"
    elif previous_data_ready and previous_velocity > 0 and recent_velocity >= previous_velocity * 1.25 and recent["units"] >= 2:
        lifecycle = "growth"
        lifecycle_label = "增长"
    elif recent_data_ready and recent_velocity < 1:
        lifecycle = "weak"
        lifecycle_label = "近期弱动销"
    elif not recent_data_ready:
        lifecycle = "recent_coverage_insufficient"
        lifecycle_label = "近30天覆盖不足"
    else:
        lifecycle = "stable"
        lifecycle_label = "稳定/培育"
    return {
        "store_code": series["store_code"],
        "store_name": store_name,
        "plid": series["plid"],
        "offer_ids": list(series.get("offer_ids") or []),
        "skus": list(series.get("skus") or []),
        "image_url": _own_offer_image_url(current_offers),
        "listing_date": series.get("listing_date"),
        "listing_date_source": series.get("listing_date_source"),
        "listing_age_days": listing_age_days,
        "lifecycle": lifecycle,
        "lifecycle_label": lifecycle_label,
        "ordered_units": window["units"],
        "known_days": window["known_days"],
        "verified_days": window["verified_days"],
        "partial_days": window["partial_days"],
        "missing_days": window["missing_days"],
        "monthly_velocity": round(raw_velocity, 2),
        "exposure_adjusted_monthly_velocity": round(adjusted_velocity, 2),
        "forecast_monthly_units": round(max(0, forecast), 2),
        "recent_30_units": recent["units"],
        "recent_30_known_days": recent["known_days"],
        "recent_data_ready": recent_data_ready,
        "recent_monthly_velocity": round(recent_velocity, 2),
        "previous_30_units": previous["units"],
        "previous_30_known_days": previous["known_days"],
        "previous_data_ready": previous_data_ready,
        "previous_monthly_velocity": round(previous_velocity, 2),
        "recent_vs_previous_change_percentage": round(
            (recent_velocity - previous_velocity) / previous_velocity * 100,
            2,
        ) if previous_data_ready and previous_velocity > 0 else None,
        "observed_inventory_days": observed_days,
        "buyable_observed_days": buyable_days,
        "buyable_observation_ratio": round(exposure_ratio, 4) if exposure_ratio is not None else None,
        "inventory": inventory,
    }


def _own_profile_payload(
    profile: Mapping[str, Any],
    *,
    product_name: str | None,
    plids: list[str],
    links: list[dict[str, Any]],
    profit_items: list[dict[str, Any]],
    rate_available: bool,
    as_of: date,
    window_start: date,
    target_cover_days: int,
    clearance_days: int,
    minimum_known_days: int,
    minimum_recent_monthly_units: float,
    strong_recent_monthly_units: float,
    maximum_recent_decline_ratio: float,
) -> dict[str, Any]:
    ordered_units = sum(int(link["ordered_units"]) for link in links)
    monthly_velocity = sum(float(link["monthly_velocity"]) for link in links)
    recent_units = sum(int(link["recent_30_units"]) for link in links)
    previous_units = sum(int(link["previous_30_units"]) for link in links)
    recent_known_links = sum(bool(link["recent_data_ready"]) for link in links)
    previous_known_links = sum(bool(link["previous_data_ready"]) for link in links)
    recent_monthly_velocity = sum(
        float(link["recent_monthly_velocity"])
        for link in links
        if link["recent_data_ready"]
    )
    previous_monthly_velocity = sum(
        float(link["previous_monthly_velocity"])
        for link in links
        if link["previous_data_ready"]
    )
    forecast_monthly = recent_monthly_velocity
    recent_change_percentage = (
        (recent_monthly_velocity - previous_monthly_velocity)
        / previous_monthly_velocity
        * 100
        if previous_known_links and previous_monthly_velocity > 0
        else None
    )
    recent_decline_ratio = (
        max(0, previous_monthly_velocity - recent_monthly_velocity)
        / previous_monthly_velocity
        if previous_known_links and previous_monthly_velocity > 0
        else 0
    )
    recent_velocity_bright = (
        recent_monthly_velocity >= minimum_recent_monthly_units
        and (
            not previous_known_links
            or recent_decline_ratio <= maximum_recent_decline_ratio
            or recent_monthly_velocity >= strong_recent_monthly_units
        )
    )
    inventory = {
        key: sum(int(link["inventory"][key]) for link in links)
        for key in (
            "platform_available_stock",
            "seller_available_stock",
            "sellable_stock",
            "stock_in_receiving",
            "stock_on_way",
        )
    }
    inventory["sellable_now"] = inventory["sellable_stock"] > 0
    stock_cover_days = (
        inventory["sellable_stock"] / forecast_monthly * 30
        if forecast_monthly > 0
        else None
    )
    sea_freight = _number(profile.get("sea_freight_rmb")) or 0
    profit_rows: list[dict[str, Any]] = []
    for item in profit_items:
        scenario = (item.get("scenarios") or {}).get("current_fee_adjusted")
        if not isinstance(scenario, dict):
            continue
        profit_after_sea = float(scenario["profit_rmb"]) - sea_freight
        price_rmb = float(scenario["price_rmb"])
        profit_rows.append(
            {
                "store_code": item.get("store_code"),
                "offer_id": item.get("offer_id"),
                "profit_rmb_after_direct_fees_and_sea": round(profit_after_sea, 2),
                "profit_margin_percentage": round(
                    profit_after_sea / price_rmb * 100,
                    2,
                ) if price_rmb > 0 else None,
                "fee_basis": item.get("fee_basis"),
            }
        )
    profit_values = [row["profit_rmb_after_direct_fees_and_sea"] for row in profit_rows]
    margin_values = [
        row["profit_margin_percentage"]
        for row in profit_rows
        if row["profit_margin_percentage"] is not None
    ]
    profit_status = (
        "available"
        if profit_rows
        else "rate_unavailable"
        if not rate_available
        else "fee_unverified"
    )
    profit_positive = bool(profit_values and min(profit_values) > 0)

    target_units = math.ceil(forecast_monthly * target_cover_days / 30)
    clearance_units = math.ceil(forecast_monthly * clearance_days / 30)
    net_need = max(
        0,
        target_units
        - inventory["sellable_stock"]
        - inventory["stock_in_receiving"]
        - inventory["stock_on_way"],
    )
    carton_size = max(1, int(_number(profile.get("units_per_carton")) or 1))
    recommended_units = math.ceil(net_need / carton_size) * carton_size if net_need else 0
    unit_cbm = float(_number(profile.get("unit_cbm")) or 0)
    electrical_status = str(profile.get("electrical_status") or "unknown")
    electrical_qualified = electrical_status == "non_electric"
    if not product_name or not plids or not links:
        recommendation_status = "mapping_missing"
        label = "先补主档映射"
        reason = "工作簿 SKU 尚未匹配到当前公司商品主档或自有 Offer。"
        recommended_units = 0
    elif not electrical_qualified:
        recommendation_status = "electrical_condition_pending"
        label = "先核包装清单"
        reason = "一次性干电池必须确认不随货，未确认前不计入非带电配柜量。"
        recommended_units = 0
    elif recent_known_links == 0:
        recommendation_status = "coverage_insufficient"
        label = "先补数据覆盖"
        reason = "没有达到最低覆盖天数的近30天可见销售链路；旧销量不参与备货。"
        recommended_units = 0
    elif recent_monthly_velocity < minimum_recent_monthly_units:
        recommendation_status = "low_velocity"
        label = "近期低动销，不建议加急"
        reason = (
            f"近30天可信月化动销低于 {minimum_recent_monthly_units:g} 件；"
            "90天历史销量不能替代近期表现。"
        )
        recommended_units = 0
    elif not recent_velocity_bright:
        recommendation_status = "recent_momentum_weak"
        label = "近期转弱，暂缓"
        reason = "近30天较前30天明显转弱且未达到强动销豁免线，只保留观察。"
        recommended_units = 0
    elif not profit_positive:
        recommendation_status = "profit_unverified"
        label = "先核利润"
        reason = "缺少完整费用样本，或至少一个可计算 Offer 扣直接费用与海运后非正利润。"
        recommended_units = 0
    elif recommended_units <= 0:
        recommendation_status = "hold"
        label = "暂缓补货"
        reason = f"当前可售、收货中和在途库存已覆盖 {target_cover_days} 天近期需求。"
    else:
        recommendation_status = "replenish"
        label = "建议加急补货"
        reason = (
            "非带电、大体积、近30天动销与利润均通过，"
            f"当前库存未覆盖 {target_cover_days} 天近期需求。"
        )

    profit_margin_floor = min(margin_values) if margin_values else 0
    cover_bonus = 30 if stock_cover_days is not None and stock_cover_days < 45 else 0
    priority_score = (
        min(recent_monthly_velocity, 60) * 2
        + min(unit_cbm * 1000, 100)
        + max(min(profit_margin_floor, 30), -30)
        + cover_bonus
    )
    image_links = [
        link
        for link in links
        if str(link.get("image_url") or "").strip()
    ]
    image_link = next(
        (
            link
            for link in image_links
            if bool((link.get("inventory") or {}).get("sellable_now"))
        ),
        image_links[0] if image_links else None,
    )
    return {
        "rank": 0,
        "company_sku": str(profile.get("company_sku") or ""),
        "product_name": product_name or str(profile.get("product_name") or ""),
        "plids": plids,
        "image_url": image_link["image_url"] if image_link is not None else None,
        "image_store_code": (
            image_link.get("store_code") if image_link is not None else None
        ),
        "source": {
            "workbook": "takealot选品表.xlsx",
            "sheet": profile.get("source_sheet"),
            "row": profile.get("source_row"),
            "measured": bool(profile.get("measured")),
        },
        "electrical": {
            "status": electrical_status,
            "qualified": electrical_qualified,
            "evidence": profile.get("electrical_evidence"),
        },
        "logistics": {
            "length_cm": profile.get("length_cm"),
            "width_cm": profile.get("width_cm"),
            "height_cm": profile.get("height_cm"),
            "carton_weight_kg": profile.get("carton_weight_kg"),
            "units_per_carton": carton_size,
            "unit_cbm": unit_cbm,
            "cbm_per_100_units": round(unit_cbm * 100, 3),
            "sea_freight_rmb": round(sea_freight, 2),
        },
        "sales": {
            "window_start": window_start.isoformat(),
            "window_end": as_of.isoformat(),
            "ordered_units": ordered_units,
            "monthly_velocity": round(monthly_velocity, 2),
            "forecast_monthly_units": round(forecast_monthly, 2),
            "known_link_count": sum(
                int(link["known_days"]) >= minimum_known_days for link in links
            ),
            "link_count": len(links),
            "recent_30_units": recent_units,
            "recent_known_link_count": recent_known_links,
            "recent_monthly_velocity": round(recent_monthly_velocity, 2),
            "previous_30_units": previous_units,
            "previous_known_link_count": previous_known_links,
            "previous_monthly_velocity": round(previous_monthly_velocity, 2),
            "recent_vs_previous_change_percentage": round(
                recent_change_percentage,
                2,
            ) if recent_change_percentage is not None else None,
            "recent_velocity_bright": recent_velocity_bright,
            "decision_window_note": "近30天定量；前30天仅作趋势；90天仅作背景。",
            "links": links,
        },
        "inventory": {
            **inventory,
            "stock_cover_days": round(stock_cover_days, 1) if stock_cover_days is not None else None,
        },
        "profit": {
            "status": profit_status,
            "calculated_offer_count": len(profit_rows),
            "profit_positive": profit_positive,
            "minimum_profit_rmb": round(min(profit_values), 2) if profit_values else None,
            "maximum_profit_rmb": round(max(profit_values), 2) if profit_values else None,
            "minimum_margin_percentage": round(min(margin_values), 2) if margin_values else None,
            "items": profit_rows,
            "note": "扣平台直接费用与工作表9单件海运头程；未扣广告、仓储、税费、退货损失和月租。",
        },
        "recommendation": {
            "status": recommendation_status,
            "label": label,
            "reason": reason,
            "target_cover_days": target_cover_days,
            "target_demand_units": target_units,
            "clearance_window_days": clearance_days,
            "clearance_units": clearance_units,
            "recommended_units": recommended_units,
            "recommended_cartons": math.ceil(recommended_units / carton_size) if recommended_units else 0,
            "recommended_cbm": round(recommended_units * unit_cbm, 3),
        },
        "risk_tags": list(profile.get("risk_tags") or []),
        "priority_score": round(priority_score, 2),
    }


def _build_radar_categories(
    engine: Engine,
    *,
    categories: list[dict[str, Any]],
    policy: Mapping[str, Any],
    as_of: date,
    evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    configured = [
        representative
        for category in categories
        for representative in category["representatives"]
    ]
    if evidence is None:
        evidence = _load_radar_link_evidence(
            engine,
            configured=configured,
            policy=policy,
            as_of=as_of,
        )
    minimum_representatives = max(
        4,
        int(policy.get("minimum_representatives_per_category") or 4),
    )
    minimum_signal_links = max(
        2,
        int(policy.get("minimum_recent_signal_links") or 2),
    )
    minimum_signal_score = max(
        1,
        int(policy.get("minimum_recent_signal_score") or 4),
    )
    minimum_unit_cbm = float(policy.get("new_min_unit_cbm") or 0)

    result: list[dict[str, Any]] = []
    for category in categories:
        representatives = [
            _radar_link_payload(
                representative,
                evidence=evidence[_normalized_plid(representative.get("plid"))],
            )
            for representative in category["representatives"]
        ]
        active_count = sum(bool(item["monitoring"]["active"]) for item in representatives)
        baseline_ready_count = sum(
            bool(item["monitoring"]["baseline_ready"])
            for item in representatives
        )
        recent_signal_count = sum(
            bool(item["monitoring"]["recent_signal"])
            for item in representatives
        )
        qualified_recent_signal_count = sum(
            bool(item["monitoring"]["qualified_recent_signal"])
            for item in representatives
        )
        signal_score = sum(
            int(item["monitoring"]["recent_signal_score"])
            for item in representatives
            if item["monitoring"]["qualified_recent_signal"]
        )
        recent_dated_reviews = sum(
            int(item["monitoring"]["recent_dated_review_count"])
            for item in representatives
        )
        recent_review_link_count = sum(
            int(item["monitoring"]["recent_dated_review_count"]) > 0
            for item in representatives
        )
        recent_stock_outflow = sum(
            int(item["monitoring"]["recent_stock_outflow"])
            for item in representatives
        )
        latest_review_dates = [
            str(item["monitoring"]["latest_review_date"])
            for item in representatives
            if item["monitoring"]["latest_review_date"]
        ]
        if active_count < minimum_representatives:
            monitoring_status = "monitoring_incomplete"
            monitoring_label = "代表链接尚未全部进入雷达"
        elif baseline_ready_count < minimum_signal_links:
            monitoring_status = "building_baseline"
            monitoring_label = "多链接基线仍在建立"
        elif (
            qualified_recent_signal_count >= minimum_signal_links
            and signal_score >= minimum_signal_score
        ):
            monitoring_status = "recent_hot"
            monitoring_label = "近期多链接动销亮眼"
        elif recent_signal_count:
            monitoring_status = "recent_mixed"
            monitoring_label = "近期有信号，证据未达标"
        else:
            monitoring_status = "recent_cold"
            monitoring_label = "近30天未见动销信号"

        anchor = category["economics_anchor"]
        anchor_cbm = float(_number(anchor.get("unit_cbm")) or 0)
        anchor_profit = float(_number(anchor.get("sea_profit_rmb")) or 0)
        economics_pass = anchor_cbm >= minimum_unit_cbm and anchor_profit > 0
        if monitoring_status == "recent_hot" and economics_pass:
            decision_status = "opening_review"
            decision_label = "建议进入新品BOM/报价复核"
            decision_note = (
                "近30天至少两条代表链接形成合格动销信号，且工作簿利润与体积锚点通过；"
                "下单前仍需更新采购价、BOM、非带电和包装箱规。"
            )
        else:
            decision_status = "monitor"
            decision_label = "继续监控，暂不上新"
            decision_note = (
                "只有近期多链接信号、评论日期新鲜度、基线、利润、体积和非带电证据同时通过，"
                "才进入新品复核。"
            )
        result.append(
            {
                "category_id": category["category_id"],
                "category_name": category["category_name"],
                "market_leaf_id": category["market_leaf_id"],
                "market_leaf_name": category["market_leaf_name"],
                "cohort_basis": category["cohort_basis"],
                "economics_anchor": {
                    "plid": _normalized_plid(anchor.get("plid")),
                    "url": anchor.get("url"),
                    "name": anchor.get("name"),
                    "workbook_observation": anchor.get("workbook_observation"),
                    "workbook_observation_is_recent_demand": False,
                    "purchase_rmb": anchor.get("purchase_rmb"),
                    "selling_price_zar": anchor.get("selling_price_zar"),
                    "sea_profit_rmb": anchor.get("sea_profit_rmb"),
                    "sea_margin_percentage": round(
                        float(anchor.get("sea_margin") or 0) * 100,
                        2,
                    ),
                    "formula_version": policy.get("formula_version"),
                    "source_sheet": anchor.get("source_sheet"),
                    "source_row": anchor.get("source_row"),
                    "unit_cbm": anchor_cbm,
                    "cbm_per_100_units": round(anchor_cbm * 100, 3),
                    "length_cm": anchor.get("length_cm"),
                    "width_cm": anchor.get("width_cm"),
                    "height_cm": anchor.get("height_cm"),
                    "electrical_status": anchor.get("electrical_status"),
                    "electrical_evidence": anchor.get("electrical_evidence"),
                    "risk_tags": list(anchor.get("risk_tags") or []),
                },
                "monitoring": {
                    "status": monitoring_status,
                    "label": monitoring_label,
                    "representative_count": len(representatives),
                    "active_link_count": active_count,
                    "baseline_ready_link_count": baseline_ready_count,
                    "recent_signal_link_count": recent_signal_count,
                    "qualified_recent_signal_link_count": qualified_recent_signal_count,
                    "recent_signal_score": signal_score,
                    "recent_dated_review_count": recent_dated_reviews,
                    "recent_review_link_count": recent_review_link_count,
                    "latest_review_date": max(latest_review_dates) if latest_review_dates else None,
                    "recent_stock_outflow": recent_stock_outflow,
                    "window_start": (
                        as_of
                        - timedelta(
                            days=max(
                                7,
                                int(policy.get("recent_sales_window_days") or 30),
                            )
                            - 1
                        )
                    ).isoformat(),
                    "window_end": as_of.isoformat(),
                    "signal_note": (
                        "近期评论只按公开评论日期计数；库存流出是信号，不等同订单。"
                    ),
                },
                "decision": {
                    "status": decision_status,
                    "label": decision_label,
                    "note": decision_note,
                },
                "representatives": representatives,
            }
        )
    result.sort(
        key=lambda item: (
            item["decision"]["status"] == "opening_review",
            int(item["monitoring"]["recent_signal_score"]),
            int(item["monitoring"]["recent_dated_review_count"]),
            float(item["economics_anchor"]["unit_cbm"]),
        ),
        reverse=True,
    )
    return result


def _build_retained_watchlist(
    engine: Engine,
    *,
    candidates: list[dict[str, Any]],
    policy: Mapping[str, Any],
    as_of: date,
    evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    configured = [
        {**candidate, "roles": []}
        for candidate in candidates
    ]
    if evidence is None:
        evidence = _load_radar_link_evidence(
            engine,
            configured=configured,
            policy=policy,
            as_of=as_of,
        )
    result = []
    for candidate in configured:
        plid = _normalized_plid(candidate.get("plid"))
        item = _radar_link_payload(candidate, evidence=evidence[plid])
        item["retention_reason"] = candidate.get("retention_reason")
        item["decision"] = {
            "status": "retained_monitor",
            "label": "旧批次留观，不计入近期上新",
            "note": "保留采集以免丢失基线；未进入当前近期多链接类目组合。",
        }
        result.append(item)
    result.sort(
        key=lambda item: (
            item["monitoring"]["qualified_recent_signal"],
            item["monitoring"]["recent_signal_score"],
        ),
        reverse=True,
    )
    return result


def _load_radar_link_evidence(
    engine: Engine,
    *,
    configured: list[dict[str, Any]],
    policy: Mapping[str, Any],
    as_of: date,
) -> dict[str, dict[str, Any]]:
    plids = {_normalized_plid(item.get("plid")) for item in configured}
    minimum_snapshots = max(2, int(policy.get("minimum_radar_snapshots") or 2))
    minimum_days = max(1, int(policy.get("minimum_radar_baseline_days") or 7))
    recent_days = max(7, int(policy.get("recent_sales_window_days") or 30))
    recent_start = as_of - timedelta(days=recent_days - 1)
    with Session(engine) as session:
        targets = {
            target.plid: target
            for target in session.scalars(
                select(CompetitorTarget).where(CompetitorTarget.plid.in_(sorted(plids)))
            )
        }
        audits_by_plid: dict[str, list[CompetitorTargetAudit]] = defaultdict(list)
        for audit in session.scalars(
            select(CompetitorTargetAudit)
            .where(
                CompetitorTargetAudit.plid.in_(sorted(plids)),
                CompetitorTargetAudit.action == "add",
            )
            .order_by(CompetitorTargetAudit.changed_at)
        ):
            audits_by_plid[audit.plid].append(audit)
        snapshots_by_plid: dict[str, list[CompetitorSnapshot]] = defaultdict(list)
        for snapshot in session.scalars(
            select(CompetitorSnapshot)
            .where(CompetitorSnapshot.plid.in_(sorted(plids)))
            .order_by(CompetitorSnapshot.plid, CompetitorSnapshot.collected_at)
        ):
            if snapshot.collected_at.date() <= as_of:
                snapshots_by_plid[snapshot.plid].append(snapshot)
        snapshot_ids = {
            snapshot.id
            for snapshots in snapshots_by_plid.values()
            for snapshot in snapshots
        }
        variant_signatures: dict[int, frozenset[tuple[str, str, str]]] = {}
        if snapshot_ids:
            for variant in session.scalars(
                select(CompetitorVariantSnapshot).where(
                    CompetitorVariantSnapshot.snapshot_id.in_(sorted(snapshot_ids))
                )
            ):
                signature = variant_signatures.setdefault(variant.snapshot_id, frozenset())
                variant_signatures[variant.snapshot_id] = signature | {
                    (
                        variant.variant_key,
                        variant.sku or "",
                        variant.seller_id or "",
                    )
                }
        review_dates_by_plid: dict[str, list[date]] = defaultdict(list)
        for review in session.scalars(
            select(CompetitorReview).where(CompetitorReview.plid.in_(sorted(plids)))
        ):
            parsed = _competitor_review_date(review.review_date)
            if parsed is not None and parsed <= as_of:
                review_dates_by_plid[review.plid].append(parsed)

    result: dict[str, dict[str, Any]] = {}
    for plid in plids:
        snapshots = snapshots_by_plid.get(plid, [])
        first = snapshots[0] if snapshots else None
        latest = snapshots[-1] if snapshots else None
        recent_snapshots = [
            snapshot
            for snapshot in snapshots
            if recent_start <= snapshot.collected_at.date() <= as_of
        ]
        baseline_days = (
            max(0, (latest.collected_at - first.collected_at).days)
            if latest is not None and first is not None
            else 0
        )
        baseline_ready = (
            len(snapshots) >= minimum_snapshots
            and baseline_days >= minimum_days
        )
        recent_stock_outflow = sum(
            max(0, int(snapshot.observed_stock_outflow or 0))
            for snapshot in recent_snapshots
        )
        review_dates = review_dates_by_plid.get(plid, [])
        recent_review_dates = [
            review_date
            for review_date in review_dates
            if recent_start <= review_date <= as_of
        ]
        recent_review_delta = None
        if recent_snapshots:
            before_window = next(
                (
                    snapshot
                    for snapshot in reversed(snapshots)
                    if snapshot.collected_at.date() < recent_start
                ),
                recent_snapshots[0],
            )
            if recent_snapshots[-1].id != before_window.id:
                recent_review_delta = max(
                    0,
                    int(recent_snapshots[-1].review_count)
                    - int(before_window.review_count),
                )
        recent_signal_score = recent_stock_outflow + len(recent_review_dates)
        recent_signal = recent_signal_score > 0
        recent_observed_sales, recent_observed_sales_through = (
            _snapshot_recent_observed_sales_units(
                snapshots,
                variant_signatures=variant_signatures,
            )
        )
        target = targets.get(plid)
        selected_audit = audits_by_plid.get(plid, [])[-1] if audits_by_plid.get(plid) else None
        result[plid] = {
            "target": target,
            "selected_audit": selected_audit,
            "first": first,
            "latest": latest,
            "snapshot_count": len(snapshots),
            "recent_snapshot_count": len(recent_snapshots),
            "baseline_days": baseline_days,
            "baseline_ready": baseline_ready,
            "recent_stock_outflow": recent_stock_outflow,
            "recent_observed_sales": recent_observed_sales,
            "recent_observed_sales_through": (
                recent_observed_sales_through.isoformat()
                if recent_observed_sales_through is not None
                else None
            ),
            "recent_dated_review_count": len(recent_review_dates),
            "latest_review_date": max(review_dates).isoformat() if review_dates else None,
            "recent_review_delta": recent_review_delta,
            "recent_signal_score": recent_signal_score,
            "recent_signal": recent_signal,
            "qualified_recent_signal": baseline_ready and recent_signal,
        }
    return result


def _radar_link_payload(
    configured: Mapping[str, Any],
    *,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    plid = _normalized_plid(configured.get("plid"))
    target = evidence["target"]
    latest = evidence["latest"]
    first = evidence["first"]
    selected_audit = evidence["selected_audit"]
    active = bool(target and target.active)
    if not active:
        status = "not_added"
        label = "尚未加入雷达"
    elif latest is None:
        status = "awaiting_first_snapshot"
        label = "等待首轮采集"
    elif not evidence["baseline_ready"]:
        status = "building_baseline"
        label = "建立监控基线"
    elif evidence["recent_signal"]:
        status = "recent_signal"
        label = "近30天有动销信号"
    else:
        status = "recent_no_motion"
        label = "近30天未见动销"
    roles = [str(role) for role in configured.get("roles") or []]
    return {
        "plid": plid,
        "url": str(configured.get("url") or ""),
        "name": (
            latest.title
            if latest is not None
            else target.title
            if target is not None and target.title
            else str(configured.get("name") or f"PLID{plid}")
        ),
        "roles": roles,
        "role_labels": [RADAR_ROLE_LABELS[role] for role in roles],
        "current": {
            "title": latest.title if latest is not None else None,
            "image_url": latest.image_url if latest is not None else None,
            "price_zar": (
                float(latest.price)
                if latest is not None and latest.price is not None
                else None
            ),
            "stock_status": latest.stock_status if latest is not None else None,
            "stock_quantity": (
                latest.stock_quantity
                if latest is not None and latest.stock_exact
                else None
            ),
            "stock_exact": bool(latest and latest.stock_exact),
            "review_count_total": int(latest.review_count) if latest is not None else None,
            "rating": (
                float(latest.rating)
                if latest is not None and latest.rating is not None
                else None
            ),
        },
        "monitoring": {
            "active": active,
            "status": status,
            "label": label,
            "added_at": _iso_datetime(target.created_at) if target is not None else None,
            "added_by": selected_audit.actor_display_name if selected_audit is not None else None,
            "snapshot_count": evidence["snapshot_count"],
            "recent_snapshot_count": evidence["recent_snapshot_count"],
            "first_snapshot_at": _iso_datetime(first.collected_at) if first is not None else None,
            "latest_snapshot_at": _iso_datetime(latest.collected_at) if latest is not None else None,
            "baseline_days": evidence["baseline_days"],
            "baseline_ready": evidence["baseline_ready"],
            "recent_stock_outflow": evidence["recent_stock_outflow"],
            "recent_observed_sales": evidence["recent_observed_sales"],
            "recent_observed_sales_through": evidence[
                "recent_observed_sales_through"
            ],
            "recent_dated_review_count": evidence["recent_dated_review_count"],
            "latest_review_date": evidence["latest_review_date"],
            "recent_review_delta_from_snapshots": evidence["recent_review_delta"],
            "recent_signal_score": evidence["recent_signal_score"],
            "recent_signal": evidence["recent_signal"],
            "qualified_recent_signal": evidence["qualified_recent_signal"],
            "signal_note": "累计评论不判热销；近30天评论按评论日期计数，库存流出仍只是信号。",
        },
    }


def _load_local_rate(project_root: Path) -> tuple[_CachedRateProvider | None, dict[str, Any]]:
    path = project_root / RATE_CACHE_RELATIVE_PATH
    base_payload: dict[str, Any] = {
        "status": "unavailable",
        "base": "CNY",
        "quote": "ZAR",
        "rate": None,
        "rate_date": None,
        "fetched_at": None,
        "source": "本地持久化汇率缓存",
    }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rate = Decimal(str(payload.get("rate")))
        rate_date = date.fromisoformat(str(payload.get("date")))
        fetched_at = datetime.fromisoformat(str(payload.get("fetched_at")).replace("Z", "+00:00"))
        if (
            payload.get("base") != "CNY"
            or payload.get("quote") != "ZAR"
            or not rate.is_finite()
            or rate <= 0
        ):
            raise ValueError
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise ValueError
    except (OSError, UnicodeError, json.JSONDecodeError, InvalidOperation, TypeError, ValueError):
        return None, base_payload
    quote = ExchangeRateQuote(
        rate=rate,
        rate_date=rate_date,
        fetched_at=fetched_at.astimezone(UTC),
        stale=False,
    )
    return (
        _CachedRateProvider(quote),
        {
            **base_payload,
            "status": "cached",
            "rate": float(rate),
            "rate_date": rate_date.isoformat(),
            "fetched_at": fetched_at.astimezone(UTC).isoformat(),
        },
    )


def _empty_own_item(
    profile: Mapping[str, Any],
    *,
    as_of: date,
    window_start: date,
    reason: str,
) -> dict[str, Any]:
    unit_cbm = float(_number(profile.get("unit_cbm")) or 0)
    return {
        "rank": 0,
        "company_sku": str(profile.get("company_sku") or ""),
        "product_name": str(profile.get("product_name") or ""),
        "plids": [],
        "image_url": None,
        "image_store_code": None,
        "source": {
            "workbook": "takealot选品表.xlsx",
            "sheet": profile.get("source_sheet"),
            "row": profile.get("source_row"),
            "measured": bool(profile.get("measured")),
        },
        "electrical": {
            "status": profile.get("electrical_status"),
            "qualified": profile.get("electrical_status") == "non_electric",
            "evidence": profile.get("electrical_evidence"),
        },
        "logistics": {
            "length_cm": profile.get("length_cm"),
            "width_cm": profile.get("width_cm"),
            "height_cm": profile.get("height_cm"),
            "carton_weight_kg": profile.get("carton_weight_kg"),
            "units_per_carton": profile.get("units_per_carton"),
            "unit_cbm": unit_cbm,
            "cbm_per_100_units": round(unit_cbm * 100, 3),
            "sea_freight_rmb": profile.get("sea_freight_rmb"),
        },
        "sales": {
            "window_start": window_start.isoformat(),
            "window_end": as_of.isoformat(),
            "ordered_units": 0,
            "monthly_velocity": None,
            "forecast_monthly_units": None,
            "known_link_count": 0,
            "link_count": 0,
            "recent_30_units": 0,
            "recent_known_link_count": 0,
            "recent_monthly_velocity": None,
            "previous_30_units": 0,
            "previous_known_link_count": 0,
            "previous_monthly_velocity": None,
            "recent_vs_previous_change_percentage": None,
            "recent_velocity_bright": False,
            "decision_window_note": "近30天定量；前30天仅作趋势；90天仅作背景。",
            "links": [],
        },
        "inventory": {
            "platform_available_stock": 0,
            "seller_available_stock": 0,
            "sellable_stock": 0,
            "stock_in_receiving": 0,
            "stock_on_way": 0,
            "sellable_now": False,
            "stock_cover_days": None,
        },
        "profit": {
            "status": "unavailable",
            "calculated_offer_count": 0,
            "profit_positive": False,
            "minimum_profit_rmb": None,
            "maximum_profit_rmb": None,
            "minimum_margin_percentage": None,
            "items": [],
            "note": reason,
        },
        "recommendation": {
            "status": "scope_unavailable",
            "label": "暂无授权数据",
            "reason": reason,
            "target_cover_days": None,
            "target_demand_units": None,
            "clearance_window_days": None,
            "clearance_units": None,
            "recommended_units": 0,
            "recommended_cartons": 0,
            "recommended_cbm": 0,
        },
        "risk_tags": list(profile.get("risk_tags") or []),
        "priority_score": 0,
    }


def _point_window(
    points: list[Mapping[str, Any]],
    start: date,
    end: date,
) -> dict[str, int]:
    selected = [
        point
        for point in points
        if start.isoformat() <= str(point.get("date") or "") <= end.isoformat()
    ]
    known = [point for point in selected if point.get("ordered_units") is not None]
    return {
        "units": sum(int(point.get("ordered_units") or 0) for point in known),
        "known_days": len(known),
        "verified_days": sum(point.get("data_status") == "verified" for point in selected),
        "partial_days": sum(point.get("data_status") == "partial" for point in selected),
        "missing_days": sum(point.get("data_status") == "missing" for point in selected),
    }


def _link_inventory(offers: list[OfferCurrent]) -> dict[str, Any]:
    platform = sum(_non_negative_int(offer.takealot_available_stock) for offer in offers)
    seller = sum(_non_negative_int(offer.seller_available_stock) for offer in offers)
    total = sum(_non_negative_int(offer.total_stock) for offer in offers)
    sellable = max(total, platform + seller)
    receiving = sum(_non_negative_int(offer.takealot_stock_in_receiving) for offer in offers)
    on_way = sum(_non_negative_int(offer.takealot_stock_on_way) for offer in offers)
    return {
        "platform_available_stock": platform,
        "seller_available_stock": seller,
        "sellable_stock": sellable,
        "stock_in_receiving": receiving,
        "stock_on_way": on_way,
        "sellable_now": any(
            _offer_is_buyable(
                offer.status,
                offer.total_stock,
                offer.takealot_available_stock,
                offer.seller_available_stock,
            )
            for offer in offers
        ),
    }


def _offer_is_buyable(
    status: object,
    total_stock: object,
    takealot_available_stock: object,
    seller_available_stock: object,
) -> bool:
    stock = max(
        _non_negative_int(total_stock),
        _non_negative_int(takealot_available_stock)
        + _non_negative_int(seller_available_stock),
    )
    normalized_status = str(status or "").strip().casefold()
    return stock > 0 or normalized_status in {"buyable", "active", "live"}


def _monthly_rate(units: int, known_days: int) -> float:
    return units / known_days * 30 if known_days > 0 else 0.0


def _normalized_plid(value: object) -> str:
    text = str(value or "").strip().upper()
    return text.removeprefix("PLID") if text.removeprefix("PLID").isdigit() else ""


def _is_matching_takealot_url(url: str, plid: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() in {"takealot.com", "www.takealot.com"}
        and f"PLID{plid}" in parsed.path.upper()
    )


def _number(value: object) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(str(value or 0)))
    except (TypeError, ValueError):
        return 0


def _date_or_none(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _competitor_review_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _iso_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value
    if normalized.tzinfo is None or normalized.utcoffset() is None:
        normalized = normalized.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat()
