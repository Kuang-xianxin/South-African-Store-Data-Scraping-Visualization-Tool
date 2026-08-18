import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const pageSource = readFileSync(new URL("../src/pages/ReturnsPage.vue", import.meta.url), "utf8");
const competitorSource = readFileSync(new URL("../src/pages/CompetitorsPage.vue", import.meta.url), "utf8");
const appSource = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");

test("returns module keeps uncollected distinct from verified zero", () => {
  assert.match(pageSource, /此时表格为空代表未采集，不代表没有退货/);
  assert.match(pageSource, /Offers 滚动30天退货件数/);
  assert.match(pageSource, /outcomes · transactions/);
});

test("own-store detail links to the consolidated returns module", () => {
  assert.match(competitorSource, /own_store_returns/);
  assert.match(competitorSource, /modulePageHref\('returns'\)/);
  assert.match(competitorSource, /退货情况/);
  assert.match(appSource, /label: "退货管理"/);
  assert.match(appSource, /returns: ReturnsPage/);
});
