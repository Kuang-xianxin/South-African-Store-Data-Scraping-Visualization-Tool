import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  ANOMALY_PRODUCT_VIEWS,
  countForAnomalyView,
  itemsForAnomalyView,
} from "../src/anomalyProducts.ts";
import type { AnomalyProductItem, AnomalyProductPayload } from "../src/types.ts";

function item(
  offerId: string,
  anomalyType: AnomalyProductItem["anomaly_type"],
  noSalesDays = 0,
): AnomalyProductItem {
  return {
    anomaly_type: anomalyType,
    anomaly_label: anomalyType,
    offer_id: offerId,
    plid: `100${offerId}`,
    tsin_id: null,
    sku: null,
    title: offerId,
    image_url: null,
    selling_price: null,
    page_views_30_days: null,
    conversion_percentage_30_days: null,
    offer_status: "buyable",
    offer_status_label: "Buyable",
    available_stock: 5,
    takealot_available_stock: 5,
    seller_available_stock: 0,
    receiving_stock: 0,
    on_way_stock: 0,
    inventory_units: 5,
    data_through: "2026-08-14",
    latest_ordered_units: 0,
    no_sales_days: noSalesDays,
    no_sales_days_exact: true,
    last_sale_on: "2026-08-02",
    slow_moving_started_on: "2026-08-03",
  };
}

const sudden = item("sudden", "sudden_sales_stop", 3);
const dailyReview = item("daily-review", "daily_bad_review");
const poorReview = item("poor-review", "poor_review_quality");
const highReturn = item("high-return", "high_return_volume");
const notBuyable = item("not", "not_buyable_with_stock");
const platformDisabled = item("platform", "disabled_by_takealot_with_stock");
const sellerDisabled = item("seller", "disabled_by_seller_with_stock");
const slow7 = item("slow7", "slow_moving", 7);
const slow20 = item("slow20", "slow_moving", 20);

const payload: AnomalyProductPayload = {
  requested_as_of: "2026-08-15",
  completed_through: "2026-08-14",
  data_through: "2026-08-14",
  date_basis: "Africa/Johannesburg",
  collection_times: {
    offers_at: "2026-08-15T01:01:00+00:00",
    sales_at: "2026-08-15T01:02:00+00:00",
    reviews_at: "2026-08-15T01:03:00+00:00",
    returns_at: "2026-08-15T01:04:00+00:00",
    latest_at: "2026-08-15T01:04:00+00:00",
  },
  sales_zero_evidence: "verified_complete_business_days_only",
  rules: {
    sales_stop_zero_days: 3,
    sales_stop_baseline_days: 7,
    sales_stop_min_selling_days: 5,
    sales_stop_min_baseline_units: 7,
    slow_day_options: [4, 7, 10, 15, 20, 30],
    slow_moving_requires_status: "buyable",
    slow_moving_requires_available_stock: true,
    slow_moving_day_basis: "verified_zero_sales_and_positive_stock_days",
    stock_status_requires_available_stock: true,
    stock_status_excluded_inventory: ["receiving", "on_way"],
    bad_review_rating_below: 5,
    daily_bad_review_basis: "first_seen_after_plid_review_baseline",
    poor_review_min_bad_count: 5,
    poor_review_min_bad_rate_percentage: 20,
    poor_review_identity: "plid",
    return_window_days: 30,
    high_return_min_units: 5,
    high_return_identity: "company_sku",
    high_return_source: "seller_returns_detail",
    uncollected_returns_are_zero: false,
  },
  summary: {
    sudden_sales_stop: 1,
    not_buyable_with_stock: 1,
    disabled_by_takealot_with_stock: 1,
    disabled_by_seller_with_stock: 1,
    slow_moving_by_days: { "4": 2, "7": 2, "10": 1, "15": 1, "20": 1, "30": 0 },
    daily_bad_reviews: 1,
    poor_review_quality: 1,
    high_returns: 1,
  },
  sudden_sales_stop: [sudden],
  stock_status_anomalies: {
    not_buyable: [notBuyable],
    disabled_by_takealot: [platformDisabled],
    disabled_by_seller: [sellerDisabled],
  },
  slow_moving: [slow20, slow7],
  daily_bad_reviews: [dailyReview],
  poor_review_quality: [poorReview],
  review_discovery_through: "2026-08-14",
  return_coverage: {
    data_status: "collected",
    window_start: "2026-07-16",
    window_end: "2026-08-14",
    window_days: 30,
    source: "seller_returns_detail",
    uncollected_is_zero: false,
  },
  high_returns: [highReturn],
};

test("all anomaly views remain separate", () => {
  assert.deepEqual(ANOMALY_PRODUCT_VIEWS, [
    "sudden_sales_stop",
    "daily_bad_reviews",
    "poor_review_quality",
    "high_returns",
    "not_buyable",
    "disabled_by_takealot",
    "disabled_by_seller",
    "slow_moving",
  ]);
  assert.deepEqual(itemsForAnomalyView(payload, "sudden_sales_stop", 7), [sudden]);
  assert.deepEqual(itemsForAnomalyView(payload, "daily_bad_reviews", 7), [dailyReview]);
  assert.deepEqual(itemsForAnomalyView(payload, "poor_review_quality", 7), [poorReview]);
  assert.deepEqual(itemsForAnomalyView(payload, "high_returns", 7), [highReturn]);
  assert.equal(countForAnomalyView(payload, "daily_bad_reviews", 7), 1);
  assert.equal(countForAnomalyView(payload, "poor_review_quality", 7), 1);
  assert.equal(countForAnomalyView(payload, "high_returns", 7), 1);
  assert.deepEqual(itemsForAnomalyView(payload, "not_buyable", 7), [notBuyable]);
  assert.deepEqual(
    itemsForAnomalyView(payload, "disabled_by_takealot", 7),
    [platformDisabled],
  );
  assert.deepEqual(
    itemsForAnomalyView(payload, "disabled_by_seller", 7),
    [sellerDisabled],
  );
});

test("review and return anomalies show their evidence and missing-data boundaries", () => {
  const pageSource = readFileSync(
    new URL("../src/pages/AnomalyProductsPage.vue", import.meta.url),
    "utf8",
  );

  assert.match(pageSource, /低于 5 星 · 按所选日期首次发现/);
  assert.match(pageSource, /低于五星 · 首次抓取基线不计入/);
  assert.match(pageSource, /review\.first_seen_on/);
  assert.match(pageSource, /item\.new_bad_reviews \|\| \[\]/);
  assert.match(pageSource, /review\.body \|\| "买家未留下文字内容"/);
  assert.match(pageSource, /累计低于 5 星至少/);
  assert.match(pageSource, /产品力重点核查门槛/);
  assert.match(pageSource, /同一公司 SKU 的 Seller Returns 明细合计至少/);
  assert.match(pageSource, /returnCoverageLabel\(\)/);
  assert.match(pageSource, /item\.return_reason_counts \|\| \[\]/);
  assert.match(pageSource, /退货明细覆盖不完整/);
});

test("all collection timestamps are explicitly rendered in Beijing time", () => {
  const pageSource = readFileSync(
    new URL("../src/pages/AnomalyProductsPage.vue", import.meta.url),
    "utf8",
  );

  assert.match(pageSource, /import \{ formatChinaDateTime \} from "\.\.\/time"/);
  assert.match(pageSource, /<span>最近一次拉取<\/span>/);
  assert.match(pageSource, /库存 \{\{ formatChinaDateTime\(payload\?\.collection_times\.offers_at/);
  assert.match(pageSource, /均为北京时间 · 完整销量证据至/);
  assert.match(pageSource, /sourceCollectionLabel\("销量"/);
  assert.match(pageSource, /sourceCollectionLabel\("库存"/);
  assert.match(pageSource, /sourceCollectionLabel\("评论"/);
  assert.match(pageSource, /sourceCollectionLabel\("退货"/);
  assert.match(pageSource, /北京时间首见/);
  assert.doesNotMatch(pageSource, /<span v-else>数据截至/);
});

test("slow-moving selector includes items exactly on the selected threshold", () => {
  assert.deepEqual(itemsForAnomalyView(payload, "slow_moving", 7), [slow20, slow7]);
  assert.deepEqual(itemsForAnomalyView(payload, "slow_moving", 15), [slow20]);
  assert.deepEqual(itemsForAnomalyView(payload, "slow_moving", 20), [slow20]);
  assert.deepEqual(itemsForAnomalyView(payload, "slow_moving", 30), []);
  assert.equal(countForAnomalyView(payload, "slow_moving", 7), 2);
  assert.equal(countForAnomalyView(payload, "slow_moving", 20), 1);
});

test("slow-moving control keeps the inclusive threshold visible", () => {
  const pageSource = readFileSync(
    new URL("../src/pages/AnomalyProductsPage.vue", import.meta.url),
    "utf8",
  );

  assert.match(pageSource, /\{\{ days \}\} 天及以上未动销/);
  assert.match(pageSource, /连续 \$\{slowDays\.value\} 天及以上未动销/);
  assert.doesNotMatch(pageSource, /有库存 \{\{ days \}\} 天没动销/);
  assert.match(pageSource, /滞销起算 \{\{ item\.slow_moving_started_on/);
});

test("cards open the shared full own-link detail as a standalone browser page", () => {
  const pageSource = readFileSync(
    new URL("../src/pages/AnomalyProductsPage.vue", import.meta.url),
    "utf8",
  );
  const appSource = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const competitorSource = readFileSync(
    new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
    "utf8",
  );

  assert.match(pageSource, /import \{ openOwnStoreDetailTab \} from "\.\.\/moduleNavigation"/);
  assert.match(pageSource, /<button[\s\S]*class="anomaly-card"/);
  assert.match(pageSource, /在新标签页查看 \$\{item\.title\} 的完整自有链接详情/);
  assert.match(pageSource, /openOwnStoreDetailTab\(\{/);
  assert.match(pageSource, /scope === "current" && currentStoreCode/);
  assert.match(pageSource, /新标签页查看完整商品详情/);
  assert.doesNotMatch(pageSource, /<CompetitorsPage|detailHost|detailRequest|prefetchRequestedOwnStoreDetail/);
  assert.match(appSource, /if \(key === "anomaly-products"\)/);
  assert.match(appSource, /canViewCompetitors: hasPermission\("competitors\.view"\)/);
  assert.match(appSource, /requestedDetailPlid: competitorDetailRequest\.value\.plid/);
  assert.match(appSource, /ownStoreDetailRequestFromHash\(window\.location\.hash\)/);
  assert.match(appSource, /class="standalone-own-detail-shell"/);
  assert.match(appSource, /<CompetitorsPage[\s\S]*detail-only/);
  assert.match(appSource, /:own-store-scope="standaloneOwnStoreDetailRequest\.scope"/);
  assert.match(competitorSource, /detailOnly\?: boolean/);
  assert.match(competitorSource, /if \(props\.detailOnly\)/);
  assert.match(competitorSource, /async function openRequestedOwnStoreDetail/);
  assert.match(
    competitorSource,
    /fetchOwnStoreCompetitors\([\s\S]*startDate \|\| undefined[\s\S]*endDate \|\| undefined[\s\S]*scope[\s\S]*plid/,
  );
  assert.match(competitorSource, /fetchCompetitorPersonalWatchlist\(\)/);
  assert.match(competitorSource, /applyPersonalWatchlistPayload\(personalWatchlist\)/);
  assert.doesNotMatch(
    competitorSource.slice(
      competitorSource.indexOf("if (props.detailOnly)"),
      competitorSource.indexOf("let checkpoint"),
    ),
    /loadOwnStoreScope|loadPersonalWatchlist/,
  );
  assert.match(
    competitorSource,
    /<template v-if="!props\.detailOnly && !props\.embeddedDetailOnly">/,
  );
  assert.match(competitorSource, /const ownItem = overview\.store_items\.find/);
  assert.match(competitorSource, /openProductModal\(ownItem\)/);
  assert.match(
    competitorSource,
    /<Teleport to="body" :disabled="props\.detailOnly">[\s\S]*competitor-standalone-detail-page/,
  );
  assert.match(competitorSource, /:role="props\.detailOnly \? 'main' : 'dialog'"/);
  assert.doesNotMatch(competitorSource, /class="module-footer"/);
});

test("standalone own-link detail uses a compact full-page flow with fixed actions", () => {
  const styleSource = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  const competitorSource = readFileSync(
    new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
    "utf8",
  );
  const standaloneStyles = styleSource.slice(
    styleSource.indexOf("/* Standalone own-store detail tab */"),
  );

  assert.match(
    standaloneStyles,
    /\.standalone-own-detail-shell \.competitor-module\s*\{[\s\S]*?width: min\(1680px, 100%\);/,
  );
  assert.match(
    standaloneStyles,
    /\.competitor-standalone-detail-page \.competitor-modal-header\s*\{[\s\S]*?position: static;/,
  );
  assert.match(
    standaloneStyles,
    /\.competitor-standalone-detail-page \.competitor-modal-actions\s*\{[\s\S]*?position: fixed;[\s\S]*?bottom: 0;/,
  );
  assert.match(
    standaloneStyles,
    /\.competitor-standalone-detail-page \.competitor-modal-header\s*\{[\s\S]*?padding: 11px 16px 9px;/,
  );
  assert.match(
    standaloneStyles,
    /\.competitor-standalone-detail-page \.company-inventory-panel,[\s\S]*?padding: 13px;/,
  );
  assert.match(
    standaloneStyles,
    /\.competitor-standalone-detail-page \.platform-inventory-offers\s*\{\s*grid-template-columns: repeat\(3, minmax\(0, 1fr\)\);/,
  );
  assert.match(
    competitorSource,
    /props\.detailOnly \? "standalone-compact" : "standard"/,
  );
  assert.doesNotMatch(standaloneStyles, /\bzoom\s*:|transform:\s*scale\(/);
});

test("stock-status cards count only sellable units", () => {
  const pageSource = readFileSync(
    new URL("../src/pages/AnomalyProductsPage.vue", import.meta.url),
    "utf8",
  );

  assert.match(pageSource, /只统计可售库存/);
  assert.match(pageSource, /收货中 .*（不计入）/);
  assert.match(pageSource, /在途 .*（不计入）/);
  assert.match(pageSource, /当前可售库存/);
  assert.match(pageSource, /<strong>\{\{ number\(item\.inventory_units\) \}\} 件<\/strong>/);
});

test("large anomaly groups render only one bounded card page", () => {
  const pageSource = readFileSync(
    new URL("../src/pages/AnomalyProductsPage.vue", import.meta.url),
    "utf8",
  );

  assert.match(pageSource, /const payload = shallowRef<AnomalyProductPayload \| null>/);
  assert.match(pageSource, /const anomalyPageSize = 30/);
  assert.match(pageSource, /filteredItems\.value\.slice\(start, start \+ anomalyPageSize\)/);
  assert.match(pageSource, /v-for="item in visibleItems"/);
  assert.match(pageSource, /class="anomaly-pagination"/);
  assert.match(pageSource, /const integerFormatter = new Intl\.NumberFormat/);
  assert.match(pageSource, /content-visibility: auto/);
});
