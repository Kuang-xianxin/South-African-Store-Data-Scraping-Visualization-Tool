import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const apiSource = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");
const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);
const viewerSource = readFileSync(
  new URL("../src/components/CompetitorCollectionLogViewer.vue", import.meta.url),
  "utf8",
);

test("competitor radar exposes the admin collection log viewer", () => {
  assert.match(pageSource, /CompetitorCollectionLogViewer/);
  assert.match(pageSource, />\s*查看轮次日志\s*</);
  assert.match(pageSource, /:current-batch-id="sharedBatchStatus\.batch_id"/);
  assert.match(pageSource, /@close="closeCollectionLogs"/);
});

test("log viewer renders one aggregate summary per collection round", () => {
  assert.match(apiSource, /\/api\/competitors\/collection-logs/);
  assert.match(apiSource, /selected_round: CompetitorCollectionLogRound \| null/);
  assert.match(viewerSource, /window\.setInterval/);
  assert.match(viewerSource, /3_000/);
  assert.match(viewerSource, /竞品雷达轮次详情/);
  assert.match(viewerSource, /本轮目标/);
  assert.match(viewerSource, /未解决失败/);
  assert.match(viewerSource, /最近轮次事件/);
  assert.match(viewerSource, /这里只显示轮次汇总，不展示逐商品爬取行/);
  assert.doesNotMatch(viewerSource, /<pre/);
  assert.doesNotMatch(viewerSource, /payload\?\.content/);
  assert.doesNotMatch(apiSource, /max_lines/);
  assert.doesNotMatch(viewerSource, /v-html/);
});
