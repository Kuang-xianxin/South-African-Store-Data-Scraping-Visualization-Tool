import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const pageSource = readFileSync(new URL("../src/pages/ReturnsPage.vue", import.meta.url), "utf8");
const competitorSource = readFileSync(new URL("../src/pages/CompetitorsPage.vue", import.meta.url), "utf8");
const appSource = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");

test("returns module keeps uncollected distinct from verified zero", () => {
  assert.match(pageSource, /尚未采集退货明细/);
  assert.match(pageSource, /Offers 滚动30天退货件数/);
  assert.doesNotMatch(pageSource, /outcomes · transactions/);
});

test("returns product card opens an exact company SKU history with own-link detail entry", () => {
  assert.match(pageSource, /查看该公司 SKU 全部退货/);
  assert.match(pageSource, /filterReturnsForCompanySku\(candidates, companySku\)/);
  assert.match(pageSource, /const requestPageSize = 100/);
  assert.doesNotMatch(pageSource, /忽略主列表当前的关键词、退货原因和处理结果筛选/);
  assert.match(pageSource, /openOwnStoreDetailTab/);
  assert.match(pageSource, /startDate: props\.rangeStart/);
  assert.match(pageSource, /endDate: props\.rangeEnd/);
  assert.match(pageSource, /<Teleport to="body">/);
  assert.match(pageSource, /companySkuOwnLinkImage\(link\)/);
  assert.match(pageSource, /@error="markReturnImageUnavailable\(link\.imageUrl\)"/);
  assert.match(pageSource, /width="192"/);
  assert.match(pageSource, /loading="lazy"/);
  assert.match(pageSource, /暂无图片/);
});

test("returns whole record row opens the company SKU history by pointer or keyboard", () => {
  assert.match(pageSource, /class="returns-record-row"/);
  assert.match(pageSource, /:class="\{ 'is-clickable': hasCompanySku\(item\) \}"/);
  assert.match(pageSource, /:tabindex="hasCompanySku\(item\) \? 0 : undefined"/);
  assert.match(pageSource, /:role="hasCompanySku\(item\) \? 'button' : undefined"/);
  assert.match(pageSource, /@click="activateReturnRow\(item, \$event\)"/);
  assert.match(pageSource, /@keydown\.enter\.self\.prevent="activateReturnRow\(item, \$event\)"/);
  assert.match(pageSource, /@keydown\.space\.self\.prevent="activateReturnRow\(item, \$event\)"/);
  assert.match(pageSource, /<div class="returns-product-card">/);
  assert.doesNotMatch(pageSource, /<button[^>]*class="returns-product-card"/);
  assert.match(pageSource, /tr\.returns-record-row\.is-clickable:hover > td/);
});

test("own-store detail links to the consolidated returns module", () => {
  assert.match(competitorSource, /own_store_returns/);
  assert.match(competitorSource, /modulePageHref\('returns'\)/);
  assert.match(competitorSource, /退货情况/);
  assert.match(appSource, /label: "退货管理"/);
  assert.match(appSource, /returns: ReturnsPage/);
});
