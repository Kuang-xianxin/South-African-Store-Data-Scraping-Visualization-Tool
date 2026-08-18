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

test("the detail card adds an evidence-bounded platform-direct-fee profit estimate", () => {
  assert.match(pageSource, /成本与利润（人民币）/);
  assert.match(pageSource, /人民币单件成本/);
  assert.match(pageSource, /当前售价毛利润/);
  assert.match(pageSource, /平台直接费用后利润（估算）/);
  assert.match(
    pageSource,
    /v-if="selectedOwnProfitability\.scenarios\.current_fee_adjusted"/,
  );
  assert.match(pageSource, /平台直接费用依据/);
  assert.match(pageSource, /fee_basis\.covered_days/);
  assert.match(pageSource, /fee_basis\.order_line_count/);
  assert.match(pageSource, /fee_basis\.fee_rate_percentage/);
  assert.match(pageSource, /fee_basis\.source/);
  assert.doesNotMatch(pageSource, /原价毛利润/);
  assert.match(pageSource, /销售利润率/);
  assert.match(pageSource, /成本加价率/);
  assert.match(pageSource, /currency: "CNY"/);
  assert.match(pageSource, /所有利润金额均为人民币/);
  assert.match(pageSource, /未扣平台及履约费用/);
  assert.match(pageSource, /sum\(total_fees\) \/ sum\(selling_price\)/);
  assert.match(pageSource, /成功费、履约费、揽收费和库存调拨费/);
  assert.match(pageSource, /仓储费、广告费、月租、头程、税费及退货损失/);
  assert.match(pageSource, /不是净利润或平台结算金额/);
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
