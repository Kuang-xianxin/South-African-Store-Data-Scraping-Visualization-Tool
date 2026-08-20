import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const readPage = (name: string) => readFileSync(
  new URL(`../src/pages/${name}.vue`, import.meta.url),
  "utf8",
);

const appSource = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
const competitorSource = readPage("CompetitorsPage");
const searchRankingSource = readPage("SearchRankingPage");
const overviewSource = readPage("OverviewPage");
const returnsSource = readPage("ReturnsPage");
const keywordTrafficSource = readPage("KeywordTrafficPage");
const visibleSource = [
  appSource,
  competitorSource,
  searchRankingSource,
  overviewSource,
  returnsSource,
  keywordTrafficSource,
].join("\n");

test("keeps internal collection and calculation manuals out of default page copy", () => {
  const removedCopy = [
    "仅当前账号可见，用于个人筛选和类型库归类",
    "按北京时间自然日 · 以系统保存的公开页快照为准",
    "每次点击开始都会重新读取最新真正竞品清单",
    "已在个人监控池，且该 PLID 仍在全局采集队列",
    "主图独立识别，标题参与融合",
    "当前商品族只走一次链路",
    "这是近30天浏览量滚动窗口",
  ];

  for (const copy of removedCopy) assert.doesNotMatch(visibleSource, new RegExp(copy));
  assert.doesNotMatch(overviewSource, /class="revenue-definition"/);
  assert.doesNotMatch(overviewSource, /class="multi-store-definition"/);
  assert.doesNotMatch(returnsSource, /returns-source-chip/);
  assert.doesNotMatch(keywordTrafficSource, /class="metric-boundary"/);
  assert.doesNotMatch(searchRankingSource, /class="method-details"/);
});

test("keeps short decisions, state and evidence boundaries visible", () => {
  assert.match(competitorSource, /等待首次采集/);
  assert.match(competitorSource, /未含仓储、广告、月租、头程、税费和退货损失；不等同净利润/);
  assert.match(searchRankingSource, /商品类型优先，规格参数默认后置/);
  assert.match(returnsSource, /已有明细可查看；未覆盖部分不按 0 计算/);
  assert.match(appSource, /完成该店铺数据接入后即可查看/);
});
