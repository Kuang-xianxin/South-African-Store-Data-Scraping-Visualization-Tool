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
  assert.match(pageSource, /const showOwnProfitabilityShell = computed/);
  assert.match(pageSource, /v-if="showOwnProfitabilityShell"/);
  assert.match(pageSource, /v-if="selectedOwnProfitability"/);
});

test("follower selection keeps the profitability shell without borrowing own-store figures", () => {
  assert.match(pageSource, /公开跟卖报价；[\s\S]*自有成本与利润区域仍保留/);
  assert.match(pageSource, /平台未披露该跟卖卖家的采购成本/);
  assert.doesNotMatch(pageSource, /不会把自有成本套用到其他卖家的公开报价/);
  assert.doesNotMatch(pageSource, /不会把自有店铺费率当作跟卖卖家的费用依据/);
  assert.match(pageSource, /@click="selectOwnProfitabilityOffer"/);
  assert.match(pageSource, /返回 \{\{ preferredOwnProfitabilityOffer\.卖家/);
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
  assert.match(pageSource, /未扣平台及履约费用/);
  assert.match(pageSource, /未含仓储、广告、月租、头程、税费和退货损失；不等同净利润/);
  assert.match(pageSource, /Math\.floor\(Date\.now\(\) \/ \(60 \* 60 \* 1_000\)\)/);
  assert.match(pageSource, /最近成功于/);
  assert.match(pageSource, /最新请求失败，已回退最近成功汇率/);
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
