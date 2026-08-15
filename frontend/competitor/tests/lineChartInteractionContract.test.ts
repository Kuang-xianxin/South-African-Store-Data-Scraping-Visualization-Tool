import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { floatingChartTooltipStyle } from "../src/floatingChartTooltip.ts";

const overviewSource = readFileSync(
  new URL("../src/pages/OverviewPage.vue", import.meta.url),
  "utf8",
);
const keywordTrafficSource = readFileSync(
  new URL("../src/pages/KeywordTrafficPage.vue", import.meta.url),
  "utf8",
);
const competitorsSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);
const ownStoreSalesSource = readFileSync(
  new URL("../src/components/OwnStoreSalesChart.vue", import.meta.url),
  "utf8",
);
const floatingTooltipSource = readFileSync(
  new URL("../src/floatingChartTooltip.ts", import.meta.url),
  "utf8",
);
const sharedStyles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

test("overview revenue and traffic details follow the pointer and clear on chart leave", () => {
  assert.equal(overviewSource.match(/class="trend-hover-card"/g)?.length, 2);
  assert.equal(overviewSource.match(/@pointermove="handle(?:Revenue|Traffic)Pointer"/g)?.length, 2);
  assert.equal(overviewSource.match(/@pointerleave="clear(?:Revenue|Traffic)Pointer"/g)?.length, 2);
  assert.equal(overviewSource.match(/@pointerenter="set(?:Revenue|Traffic)Point\(index, \$event\)"/g)?.length, 2);
  assert.equal(overviewSource.match(/@focus="set(?:Revenue|Traffic)Point\(index, \$event\)"/g)?.length, 2);
  assert.equal(overviewSource.match(/floatingChartTooltipStyle\((?:revenue|traffic)TooltipPosition, 310\)/g)?.length, 2);
  assert.equal(overviewSource.match(/class="trend-crosshair"/g)?.length, 2);
  assert.equal(overviewSource.match(/class="trend-missing-mark"/g)?.length, 2);
  assert.equal(overviewSource.match(/missingBridgeSegments/g)?.length >= 6, true);
  assert.match(overviewSource, /当前金额仅合计已有店铺，缺失店铺未按 0 补齐/);
  assert.match(overviewSource, /只要至少一家店返回该业务日金额，就绘制已有店铺合计并标注覆盖数/);
  assert.match(overviewSource, /周期末失败 · \{\{ trafficSlotLabel/);
});

test("overview revenue line discloses pending reconciliation and immutable source audit", () => {
  assert.match(overviewSource, /storeData\.sales_reconciliation\.pending_store_count/);
  assert.match(overviewSource, /class="revenue-line reconciliation-pending"/);
  assert.match(overviewSource, /class="revenue-line revised"/);
  assert.match(overviewSource, /销售额历史修订记录/);
  assert.match(overviewSource, /更新前来源/);
  assert.match(overviewSource, /更新后来源/);
  assert.match(overviewSource, /fetchSalesRevenueRevisions/);
  assert.match(overviewSource, /salesSourceLabel\(revision\.before_source\)/);
  assert.match(overviewSource, /salesSourceLabel\(revision\.after_source\)/);
  assert.match(overviewSource, /storeData\.sales_revenue_completed_through/);
  assert.match(overviewSource, /当前仍在进行的 SAST 业务日不进入折线/);
});

test("keyword traffic line keeps hover, keyboard, missing-point and rolling-window details", () => {
  assert.match(keywordTrafficSource, /@pointermove="handleChartPointer"/);
  assert.match(keywordTrafficSource, /@pointerleave="clearChartPointer"/);
  assert.match(keywordTrafficSource, /@pointerenter="setActivePoint\(point\.index, \$event\)"/);
  assert.match(keywordTrafficSource, /@keydown\.left\.prevent="stepActivePoint\(point\.index, -1, \$event\)"/);
  assert.match(keywordTrafficSource, /floatingChartTooltipStyle\(chartTooltipPosition, 330\)/);
  assert.match(keywordTrafficSource, /class="point-cursor"/);
  assert.match(keywordTrafficSource, /class="point-missing"/);
  assert.match(keywordTrafficSource, /const chartBridgeSegments = computed/);
  assert.match(keywordTrafficSource, /traffic-line missing-bridge/);
  assert.match(keywordTrafficSource, /这是该日看到的滚动30天值，不是单日浏览量/);
});

test("competitor three-panel line chart keeps a fixed detail panel with pointer and keyboard selection", () => {
  assert.match(competitorsSource, /@pointermove="handleOfferTrendPointer"/);
  assert.doesNotMatch(competitorsSource, /@pointerleave="clearOfferTrendPointer"/);
  assert.match(competitorsSource, /@keydown\.left\.prevent="stepOfferTrendPoint\(-1\)"/);
  assert.match(competitorsSource, /class="competitor-offer-trend-tooltip"/);
  assert.doesNotMatch(competitorsSource, /offerTrendTooltipPosition/);
  assert.doesNotMatch(competitorsSource, /floatingChartTooltipStyle/);
  assert.match(competitorsSource, /if \(hoveredOfferTrendIndex\.value === null\) return selectedOfferTrend\.value\.length - 1/);
  assert.match(competitorsSource, /图表上方固定显示当前时间点/);
  assert.match(competitorsSource, /offerStockEvidenceLabel\(activeOfferTrendPoint\.offer\)/);
  assert.match(competitorsSource, /class="competitor-offer-period-metric"/);
  assert.match(competitorsSource, /区间内售出件数/);
  assert.match(competitorsSource, /区间内补货件数/);
  assert.match(competitorsSource, /offerIntervalSalesUnits\(detail\.value\.history, selectedOffer\.value\)/);
  assert.match(competitorsSource, /offerIntervalReplenishmentUnits\(detail\.value\.history, selectedOffer\.value\)/);
  assert.doesNotMatch(competitorsSource, /class="offer-trend-line halo"/);
  assert.match(competitorsSource, /panel\.missingBridgeSegments/);
  assert.match(competitorsSource, /offer-trend-line missing-bridge/);
  assert.doesNotMatch(sharedStyles, /\.offer-trend-line\.halo/);
  assert.match(sharedStyles, /\.offer-trend-line\.missing-bridge/);
});

test("own-store official sales sits below comments and preserves zero versus missing evidence", () => {
  assert.match(competitorsSource, /<OwnStoreSalesChart/);
  assert.ok(
    competitorsSource.indexOf("图表上方固定显示当前时间点") <
      competitorsSource.indexOf("<OwnStoreSalesChart"),
  );
  assert.match(ownStoreSalesSource, /国内自然日（北京时间）/);
  assert.match(ownStoreSalesSource, /@pointermove="handlePointer"/);
  assert.match(ownStoreSalesSource, /@keydown\.left\.prevent="stepPoint\(-1\)"/);
  assert.match(ownStoreSalesSource, /完整的 0 件只在该国内日结束后/);
  assert.match(ownStoreSalesSource, /今天等未结束日期标为“截至采集”/);
  assert.match(ownStoreSalesSource, /缺失日期会断线，不按 0 补齐/);
  assert.doesNotMatch(ownStoreSalesSource, /overflow-x\s*:\s*(?:auto|scroll)/);
});

test("overview and keyword details float while the competitor detail panel stays fixed", () => {
  assert.equal(overviewSource.match(/class="trend-hover-card"/g)?.length, 2);
  assert.equal(keywordTrafficSource.match(/class="point-readout"/g)?.length, 1);
  assert.equal(competitorsSource.match(/class="competitor-offer-trend-tooltip"/g)?.length, 1);
  assert.match(floatingTooltipSource, /alignLeft: x > viewport\.width \/ 2/);
  assert.match(floatingTooltipSource, /alignAbove: y > viewport\.height \/ 2/);
  assert.match(overviewSource, /\.trend-hover-card \{[\s\S]*?position: fixed;/);
  assert.match(keywordTrafficSource, /\.point-readout \{[^\n]*position: fixed;/);
  const competitorTooltipStyles = sharedStyles.slice(
    sharedStyles.indexOf(".competitor-offer-trend-tooltip {"),
    sharedStyles.indexOf(".competitor-offer-trend-tooltip > div"),
  );
  assert.doesNotMatch(competitorTooltipStyles, /position:\s*fixed/);
  assert.match(competitorTooltipStyles, /margin-bottom:\s*8px/);
  assert.doesNotMatch(sharedStyles, /\.competitor-offer-trend-tooltip\.tooltip-align/);
  assert.deepEqual(
    floatingChartTooltipStyle(
      {
        x: 180,
        y: 120,
        viewportWidth: 375,
        alignLeft: false,
        alignAbove: false,
      },
      330,
    ),
    { left: "33px", top: "120px" },
  );
});

test("all line chart containers remain responsive without horizontal scrolling", () => {
  const overviewChartStyles = overviewSource.slice(
    overviewSource.indexOf(".traffic-chart-scroll"),
    overviewSource.indexOf(".traffic-grid line"),
  );
  const keywordChartStyles = keywordTrafficSource.slice(
    keywordTrafficSource.indexOf(".chart-wrap {"),
    keywordTrafficSource.indexOf(".chart-background"),
  );
  const competitorChartStyles = sharedStyles.slice(
    sharedStyles.indexOf(".competitor-offer-trend-chart {"),
    sharedStyles.indexOf(".competitor-offer-trend-chart > p"),
  );

  for (const styles of [overviewChartStyles, keywordChartStyles, competitorChartStyles]) {
    assert.doesNotMatch(styles, /overflow-x\s*:\s*(?:auto|scroll)/);
    assert.doesNotMatch(styles, /min-width\s*:\s*(?:680|760|820)px/);
  }
  assert.doesNotMatch(competitorChartStyles, /box-shadow/);
});
