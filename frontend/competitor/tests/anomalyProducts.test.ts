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
  },
  summary: {
    sudden_sales_stop: 1,
    not_buyable_with_stock: 1,
    disabled_by_takealot_with_stock: 1,
    disabled_by_seller_with_stock: 1,
    slow_moving_by_days: { "4": 2, "7": 2, "10": 1, "15": 1, "20": 1, "30": 0 },
  },
  sudden_sales_stop: [sudden],
  stock_status_anomalies: {
    not_buyable: [notBuyable],
    disabled_by_takealot: [platformDisabled],
    disabled_by_seller: [sellerDisabled],
  },
  slow_moving: [slow20, slow7],
};

test("all anomaly views remain separate", () => {
  assert.deepEqual(ANOMALY_PRODUCT_VIEWS, [
    "sudden_sales_stop",
    "not_buyable",
    "disabled_by_takealot",
    "disabled_by_seller",
    "slow_moving",
  ]);
  assert.deepEqual(itemsForAnomalyView(payload, "sudden_sales_stop", 7), [sudden]);
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

test("slow-moving selector includes items exactly on the selected threshold", () => {
  assert.deepEqual(itemsForAnomalyView(payload, "slow_moving", 7), [slow20, slow7]);
  assert.deepEqual(itemsForAnomalyView(payload, "slow_moving", 15), [slow20]);
  assert.deepEqual(itemsForAnomalyView(payload, "slow_moving", 20), [slow20]);
  assert.deepEqual(itemsForAnomalyView(payload, "slow_moving", 30), []);
  assert.equal(countForAnomalyView(payload, "slow_moving", 7), 2);
  assert.equal(countForAnomalyView(payload, "slow_moving", 20), 1);
});

test("slow-moving copy states an inclusive threshold and stocked-day basis", () => {
  const pageSource = readFileSync(
    new URL("../src/pages/AnomalyProductsPage.vue", import.meta.url),
    "utf8",
  );

  assert.match(pageSource, /\{\{ days \}\} 天及以上未动销/);
  assert.match(pageSource, /实际连续未动销天数达到或超过所选天数/);
  assert.match(pageSource, /连续 \$\{slowDays\.value\} 天及以上未动销/);
  assert.doesNotMatch(pageSource, /有库存 \{\{ days \}\} 天没动销/);
  assert.match(pageSource, /库存归零后，重新有货时重新起算/);
  assert.match(pageSource, /滞销起算 \{\{ item\.slow_moving_started_on/);
});

test("cards open the existing full own-link detail modal in the anomaly page", () => {
  const pageSource = readFileSync(
    new URL("../src/pages/AnomalyProductsPage.vue", import.meta.url),
    "utf8",
  );
  const appSource = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const competitorSource = readFileSync(
    new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
    "utf8",
  );

  assert.match(pageSource, /import CompetitorsPage from "\.\/CompetitorsPage\.vue"/);
  assert.match(pageSource, /const detailRequest = ref\(\{ plid: "", revision: 0 \}\)/);
  assert.match(pageSource, /<button[\s\S]*class="anomaly-card"/);
  assert.match(pageSource, /在当前页面查看 \$\{item\.title\} 的自有链接详情/);
  assert.match(pageSource, /<CompetitorsPage[\s\S]*detail-only/);
  assert.match(pageSource, /:own-store-scope="props\.storeScope \?\? 'current'"/);
  assert.match(pageSource, /:requested-detail-plid="detailRequest\.plid"/);
  assert.doesNotMatch(pageSource, /competitorDetailPageHref/);
  assert.doesNotMatch(pageSource, /open-own-link-detail/);
  assert.match(appSource, /if \(key === "anomaly-products"\)/);
  assert.match(appSource, /canViewCompetitors: hasPermission\("competitors\.view"\)/);
  assert.doesNotMatch(appSource, /@open-own-link-detail=/);
  assert.match(appSource, /requestedDetailPlid: competitorDetailRequest\.value\.plid/);
  assert.match(competitorSource, /detailOnly\?: boolean/);
  assert.match(competitorSource, /if \(props\.detailOnly\)/);
  assert.match(competitorSource, /async function openRequestedOwnStoreDetail/);
  assert.match(
    competitorSource,
    /fetchOwnStoreCompetitors\(undefined, undefined, scope, undefined, plid\)/,
  );
  assert.match(pageSource, /ref="detailHost"/);
  assert.match(pageSource, /@pointerenter="scheduleOwnLinkDetailPrefetch\(item\)"/);
  assert.match(competitorSource, /defineExpose\(\{ prefetchRequestedOwnStoreDetail \}\)/);
  assert.doesNotMatch(
    competitorSource.slice(
      competitorSource.indexOf("if (props.detailOnly)"),
      competitorSource.indexOf("let checkpoint"),
    ),
    /loadOwnStoreScope|loadPersonalWatchlist/,
  );
  assert.match(competitorSource, /<template v-if="!props\.detailOnly">/);
  assert.match(competitorSource, /const ownItem = overview\.store_items\.find/);
  assert.match(competitorSource, /openProductModal\(ownItem\)/);
  assert.match(
    competitorSource,
    /<\/section>\s*<\/template>\s*<Teleport to="body">\s*<div\s*v-if="detailModalOpen && selected"/,
  );
  assert.match(
    competitorSource,
    /<footer v-if="!props\.detailOnly" class="module-footer">/,
  );
});

test("stock-status cards count only sellable units", () => {
  const pageSource = readFileSync(
    new URL("../src/pages/AnomalyProductsPage.vue", import.meta.url),
    "utf8",
  );

  assert.match(pageSource, /只统计可售库存/);
  assert.match(pageSource, /收货中和在途均展示，但都不计入异常/);
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
