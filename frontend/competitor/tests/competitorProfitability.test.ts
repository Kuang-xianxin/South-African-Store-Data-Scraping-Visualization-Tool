import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);
const typeSource = readFileSync(new URL("../src/types.ts", import.meta.url), "utf8");

test("self-owned profitability follows the exact selected Seller API Offer", () => {
  assert.match(
    pageSource,
    /selected\.value\?\.来源 === "own_store"[\s\S]*selectedOffer\.value\?\.报价来源 === "seller_api"/,
  );
  assert.match(
    pageSource,
    /item\.offer_key === selectedOffer\.value\?\.报价键/,
  );
  assert.match(pageSource, /v-if="showOwnProfitabilityPanel"/);
});

test("the detail card keeps only current-price RMB gross profit and cost", () => {
  assert.match(pageSource, /成本与利润（人民币）/);
  assert.match(pageSource, /人民币单件成本/);
  assert.match(pageSource, /当前售价毛利润/);
  assert.doesNotMatch(pageSource, /当前售价平台扣费后利润（估算）/);
  assert.doesNotMatch(pageSource, /原价毛利润/);
  assert.doesNotMatch(pageSource, /扣费样本/);
  assert.match(pageSource, /销售利润率/);
  assert.match(pageSource, /成本加价率/);
  assert.match(pageSource, /currency: "CNY"/);
  assert.match(pageSource, /所有利润金额均为人民币/);
  assert.match(pageSource, /未扣平台及履约费用/);
  assert.match(pageSource, /头程、税费、广告或退货损失/);
  assert.match(pageSource, /Math\.floor\(Date\.now\(\) \/ \(60 \* 60 \* 1_000\)\)/);
  assert.match(pageSource, /获取于/);
});

test("profitability response keeps scenario and exchange-rate evidence typed", () => {
  assert.match(typeSource, /export interface OwnStoreProfitabilityPayload/);
  assert.match(typeSource, /current_gross: OwnStoreProfitScenario \| null/);
  assert.match(typeSource, /current_fee_adjusted: OwnStoreProfitScenario \| null/);
  assert.match(typeSource, /rrp_gross: OwnStoreProfitScenario \| null/);
  assert.match(typeSource, /fee_rate_percentage: number \| null/);
  assert.match(typeSource, /profit_margin_percentage: number/);
  assert.match(typeSource, /cost_markup_percentage: number/);
});
