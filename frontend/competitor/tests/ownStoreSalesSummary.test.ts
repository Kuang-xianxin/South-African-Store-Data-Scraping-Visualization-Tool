import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  selectOwnStoreSalesSeries,
  selectOwnStoreVariantSalesSeries,
  summarizeOwnStoreSalesWindows,
} from "../src/ownStoreSalesSummary.ts";
import type {
  OwnStoreSalesPoint,
  OwnStoreSalesSeries,
  OwnStoreVariantSalesSeries,
} from "../src/types.ts";

const componentSource = readFileSync(
  new URL("../src/components/OwnStoreSalesSummary.vue", import.meta.url),
  "utf8",
);
const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);
const styleSource = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

function dailyPoints(start: string, end: string): OwnStoreSalesPoint[] {
  const output: OwnStoreSalesPoint[] = [];
  let cursor = Date.parse(`${start}T00:00:00Z`);
  const final = Date.parse(`${end}T00:00:00Z`);
  while (cursor <= final) {
    output.push({
      date: new Date(cursor).toISOString().slice(0, 10),
      ordered_units: 1,
      data_status: "verified",
      revision_count: 0,
    });
    cursor += 86_400_000;
  }
  return output;
}

function salesSeries(overrides: Partial<OwnStoreSalesSeries> = {}): OwnStoreSalesSeries {
  return {
    store_code: "store-01",
    store_name: "Store One",
    plid: "123",
    offer_ids: ["offer-1"],
    image_url: null,
    skus: ["SKU-1"],
    listing_date: "2026-06-01",
    listing_date_source: "platform",
    listing_at: "2026-06-01T09:30:00+08:00",
    through_date: "2026-09-03",
    date_basis: "Asia/Shanghai",
    source_date_basis: "Africa/Johannesburg",
    total_ordered_units: 95,
    covered_days: 95,
    partial_days: 0,
    missing_days: 0,
    coverage_start: "2026-06-01",
    coverage_end: "2026-09-03",
    points: dailyPoints("2026-06-01", "2026-09-03"),
    ...overrides,
  };
}

function variantSalesSeries(
  overrides: Partial<OwnStoreVariantSalesSeries> = {},
): OwnStoreVariantSalesSeries {
  return {
    ...salesSeries(),
    offer_id: "offer-1",
    sku: "SKU-1",
    ...overrides,
  };
}

test("official sales windows use the full listing-to-through series", () => {
  const summaries = summarizeOwnStoreSalesWindows(salesSeries());

  assert.deepEqual(
    summaries.map((summary) => summary.orderedUnits),
    [7, 15, 30, 60, 90],
  );
  assert.deepEqual(
    summaries.map((summary) => summary.expectedDays),
    [7, 15, 30, 60, 90],
  );
});

test("official sales windows keep missing and partial days visible instead of filling zero", () => {
  const series = salesSeries();
  series.points.at(-1)!.ordered_units = null;
  series.points.at(-1)!.data_status = "missing";
  series.points.at(-2)!.data_status = "partial";

  const sevenDays = summarizeOwnStoreSalesWindows(series)[0]!;
  assert.equal(sevenDays.orderedUnits, 6);
  assert.equal(sevenDays.verifiedDays, 5);
  assert.equal(sevenDays.partialDays, 1);
  assert.equal(sevenDays.missingDays, 1);
});

test("preferred store selection never silently merges multiple stores", () => {
  const first = salesSeries();
  const second = salesSeries({ store_code: "store-02", store_name: "Store Two" });

  assert.equal(selectOwnStoreSalesSeries([first, second], "STORE-02")?.store_code, "store-02");
  assert.equal(selectOwnStoreSalesSeries([first, second], null)?.store_code, "store-01");
});

test("variant selection requires an exact offer id before applying store preference", () => {
  const red = variantSalesSeries({ offer_id: "offer-red", sku: "SKU-RED" });
  const blueStoreOne = variantSalesSeries({ offer_id: "offer-blue", sku: "SKU-BLUE" });
  const blueStoreTwo = variantSalesSeries({
    offer_id: "offer-blue",
    sku: "SKU-BLUE-2",
    store_code: "store-02",
  });

  assert.equal(
    selectOwnStoreVariantSalesSeries(
      [red, blueStoreOne, blueStoreTwo],
      "offer-blue",
      "STORE-02",
    )?.sku,
    "SKU-BLUE-2",
  );
  assert.equal(
    selectOwnStoreVariantSalesSeries([red, blueStoreOne], "missing-offer", "store-01"),
    null,
  );
  assert.equal(selectOwnStoreVariantSalesSeries([red], "", "store-01"), null);
});

test("own-link detail compares scope-wide official sales with exact variant and followers", () => {
  assert.match(pageSource, /import OwnStoreSalesSummary/);
  assert.match(
    pageSource,
    /<template v-if="selected\.来源 === 'own_store'">[\s\S]{0,2400}?:series="selectedOwnScopeSales"[\s\S]{0,2400}?:series="selectedOwnVariantSales"[\s\S]{0,2400}?:values="detail\.current_item\?\.跟卖近期观察售出/,
  );
  assert.match(pageSource, /detail\.value\.own_store_sales_scope/);
  assert.match(pageSource, /title="当前范围全部自有链接官方销量（件）"/);
  assert.match(pageSource, /当前范围全部自有店铺与 Offer/);
  assert.match(pageSource, /selectOwnStoreVariantSalesSeries\([\s\S]{0,260}?selectedOffer\.value\.offer_id/);
  assert.match(pageSource, /title="当前变体官方销量（件）"/);
  assert.match(pageSource, /listing-label="变体上架时间"/);
  assert.match(pageSource, /仅统计当前 Offer ID/);
  assert.match(pageSource, /title="全部跟卖报价库存观察售出（件）"/);
  assert.match(pageSource, /:series="selectedOwnScopeSales"/);
  assert.doesNotMatch(
    pageSource,
    /<OwnStoreSalesChart[\s\S]{0,180}?:preferred-store-code="selectedOwnSalesStoreCode"/,
  );
  assert.match(
    pageSource,
    /<template v-else>[\s\S]{0,1800}?:values="selectedOffer\?\.卖家近期观察售出"[\s\S]{0,1800}?:values="selectedOffer\?\.变体近期观察售出"/,
  );
  assert.match(componentSource, /近\{\{ summary\.days \}\}天/);
  assert.match(componentSource, /总销量/);
  assert.match(componentSource, /selectedSeries\.total_ordered_units/);
  assert.match(componentSource, /整条链接官方销量（件）/);
  assert.match(componentSource, /listingLabel/);
  assert.match(componentSource, /series\.listing_at/);
  assert.match(componentSource, /整条链接全部 Offer/);
  assert.match(componentSource, /不受列表区间影响/);
  assert.match(componentSource, /已完整 \$\{summary\.verifiedDays\}天 · 尚未完整 \$\{summary\.partialDays\}天/);
  assert.doesNotMatch(componentSource, /日内/);
  assert.doesNotMatch(componentSource, /appliedStartDate|appliedEndDate|activeRangeLabel/);
  assert.match(
    styleSource,
    /\.own-store-sales-overview-list \{[\s\S]*grid-template-columns: repeat\(5, minmax\(78px, 1fr\)\) minmax\(104px, 1\.15fr\) minmax\(168px, 1\.75fr\)/,
  );
});
