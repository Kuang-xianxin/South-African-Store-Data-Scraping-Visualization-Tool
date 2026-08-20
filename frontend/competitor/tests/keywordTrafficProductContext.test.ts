import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const pageSource = readFileSync(
  new URL("../src/pages/KeywordTrafficPage.vue", import.meta.url),
  "utf8",
);
const typeSource = readFileSync(new URL("../src/types.ts", import.meta.url), "utf8");

test("keyword traffic keeps listing and restock time in one fixed product context area", () => {
  assert.match(pageSource, /class="product-lifecycle" aria-label="商品上架与补货时间"/);
  assert.match(pageSource, /首次上架时间 · 南非时间/);
  assert.match(pageSource, /首次上架时间 · 本库最早记录/);
  assert.match(pageSource, /最近补货时间 · 北京时间/);
  assert.match(pageSource, /当前没有可用的首次上架或本库历史记录/);
  assert.match(pageSource, /尚未观察到平台库存增加/);
  assert.match(pageSource, /latest_restock_increase/);
  assert.match(pageSource, /\.product-lifecycle \{[^}]*grid-template-columns: repeat\(2,/s);
});

test("keyword traffic detail type carries the shared lifecycle evidence", () => {
  assert.match(typeSource, /interface ProductLifecycleContext/);
  assert.match(typeSource, /first_listed_at: string \| null/);
  assert.match(typeSource, /first_listed_source: "platform" \| "first_observed"/);
  assert.match(typeSource, /latest_restock_date: string \| null/);
  assert.match(typeSource, /latest_restock_increase: number \| null/);
  assert.match(typeSource, /interface KeywordTrafficProduct extends ProductLifecycleContext/);
});

test("keyword traffic follow tooltip shows the title from the same daily snapshot", () => {
  assert.match(pageSource, /<span>当时主标题<\/span>/);
  assert.match(pageSource, /activePoint\.source_title \|\| "—"/);
  assert.match(pageSource, /point\.source_title \|\| '无标题快照'/);
  assert.match(
    typeSource,
    /interface KeywordTrafficHistoryPoint \{[^}]*source_title: string \| null;/s,
  );
});

test("keyword traffic omits the selected-event impact summary while keeping node controls", () => {
  assert.doesNotMatch(pageSource, /已选变化节点|已选基线节点/);
  assert.doesNotMatch(pageSource, /30天浏览量上升 \/ 下降|上升 \/ 下降趋势变化/);
  assert.doesNotMatch(pageSource, /class="impact-section"|class="impact-card/);
  assert.match(pageSource, /v-model\.number="comparisonDays"/);
  assert.match(pageSource, /class="event-marker"/);
  assert.match(pageSource, /class="event-timeline"/);
  assert.match(pageSource, /@click="setSelectedEvent\(event\)"/);
});
