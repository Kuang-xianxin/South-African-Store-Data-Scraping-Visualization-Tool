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
const homeChartSource = readFileSync(new URL("../src/components/HomeMetricChart.vue", import.meta.url), "utf8");

test("overview traffic and merged sales charts retain pointer and keyboard details", () => {
  assert.equal(overviewSource.match(/class="trend-hover-card"/g)?.length, 1);
  assert.match(overviewSource, /@pointermove="handleTrafficPointer"/);
  assert.match(overviewSource, /@pointerleave="clearTrafficPointer"/);
  assert.match(overviewSource, /@pointerenter="setTrafficPoint\(index, \$event\)"/);
  assert.match(overviewSource, /@focus="setTrafficPoint\(index, \$event\)"/);
  assert.match(overviewSource, /floatingChartTooltipStyle\(trafficTooltipPosition, 310\)/);
  assert.match(overviewSource, /class="trend-crosshair"/);
  assert.match(overviewSource, /class="trend-missing-mark"/);
  assert.match(overviewSource, /trafficChart\.missingBridgeSegments/);
  assert.match(homeChartSource, /@pointermove="move"/);
  assert.match(homeChartSource, /@pointerleave="active = null"/);
  assert.match(homeChartSource, /@keydown="keyboard"/);
  assert.match(homeChartSource, /homeCoverageLabel\(current\)/);
  assert.match(overviewSource, /部分合计 · 缺/);
  assert.match(overviewSource, /橙色虚线为参考值；缺失商品不补 0/);
  assert.match(overviewSource, /周期末失败 · \{\{ trafficSlotLabel/);
});

test("merged overview keeps store alerts separate from the dated source and revision audit", () => {
  assert.match(overviewSource, /storeData\.sales_reconciliation\.pending_store_count/);
  assert.match(overviewSource, /v-for="point in storeData\.sales_revenue_series"/);
  assert.match(overviewSource, /point\.data_status === 'pending' \? revenuePendingStatus\(point\)/);
  assert.match(overviewSource, /point\.missing_store_count/);
  assert.match(overviewSource, /point\.latest_revision_at/);
  assert.match(overviewSource, /销售额日终后历史修订记录/);
  assert.match(overviewSource, /仅记录日终基线后的变化/);
  assert.match(overviewSource, /更新前来源/);
  assert.match(overviewSource, /更新后来源/);
  assert.match(overviewSource, /fetchSalesRevenueRevisions/);
  assert.match(overviewSource, /salesSourceLabel\(revision\.before_source\)/);
  assert.match(overviewSource, /salesSourceLabel\(revision\.after_source\)/);
  assert.match(overviewSource, /storeData\.sales_revenue_completed_through/);
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
  assert.match(keywordTrafficSource, /滚动30天值/);
});

test("competitor three-panel line chart keeps a fixed detail panel with pointer and keyboard selection", () => {
  assert.match(competitorsSource, /@pointermove="handleOfferTrendPointer"/);
  assert.doesNotMatch(competitorsSource, /@pointerleave="clearOfferTrendPointer"/);
  assert.match(competitorsSource, /@keydown\.left\.prevent="stepOfferTrendPoint\(-1\)"/);
  assert.match(competitorsSource, /class="competitor-offer-trend-tooltip"/);
  assert.doesNotMatch(competitorsSource, /offerTrendTooltipPosition/);
  assert.doesNotMatch(competitorsSource, /floatingChartTooltipStyle/);
  assert.match(competitorsSource, /if \(hoveredOfferTrendIndex\.value === null\) return selectedOfferTrend\.value\.length - 1/);
  assert.match(competitorsSource, /近30天浏览量为滚动值；缺失点不补 0/);
  assert.match(competitorsSource, /offerStockEvidenceLabel\(activeOfferTrendPoint\.offer\)/);
  assert.match(competitorsSource, /class="competitor-offer-period-metric"/);
  assert.match(competitorsSource, /区间内售出件数/);
  assert.match(competitorsSource, /区间内补货件数/);
  assert.match(competitorsSource, /offerIntervalSalesUnits\(filteredOfferTrendHistory\.value, selectedOffer\.value\)/);
  assert.match(competitorsSource, /offerIntervalReplenishmentUnits\(filteredOfferTrendHistory\.value, selectedOffer\.value\)/);
  assert.doesNotMatch(competitorsSource, /class="offer-trend-line halo"/);
  assert.match(competitorsSource, /panel\.missingBridgeSegments/);
  assert.match(competitorsSource, /offer-trend-line missing-bridge/);
  assert.doesNotMatch(sharedStyles, /\.offer-trend-line\.halo/);
  assert.match(sharedStyles, /\.offer-trend-line\.missing-bridge/);
});

test("offer charts share a Beijing range selector while official sales keep their own dates", () => {
  assert.match(competitorsSource, /v-for="days in \[7, 15, 30, 60, 90\]"/);
  assert.match(competitorsSource, /@click="setRecentOfferTrendRange\(days\)"/);
  assert.match(competitorsSource, /@click="resetOfferTrendDateRange"/);
  assert.match(competitorsSource, /@input="updateOfferTrendRangeStart"/);
  assert.match(competitorsSource, /@input="updateOfferTrendRangeEnd"/);
  assert.match(competitorsSource, /buildOfferObservationTrend\(filteredOfferTrendHistory\.value, selectedOffer\.value\)/);
  assert.match(competitorsSource, /<CompetitorHistoryCoverage :points="selectedOfferTrend"/);
  assert.match(competitorsSource, /alignOwnStoreTrafficTrendToOfferTrend\(\s*selectedOwnTrafficTrend\.value,\s*selectedOfferTrend\.value/);
  assert.match(competitorsSource, /watch\(\[selectedOfferKey, filteredOfferTrendHistory\]/);
  const dateHandlers = competitorsSource.slice(
    competitorsSource.indexOf("function clampOfferTrendDate("),
    competitorsSource.indexOf("function offerTrendXAtTime("),
  );
  assert.doesNotMatch(dateHandlers, /appliedStartDate|appliedEndDate|fetchCompetitorDetail|detail\.value\s*=/);
  assert.match(competitorsSource, /<OwnStoreSalesChart[\s\S]*?:series="selectedOwnScopeSales"/);
  assert.doesNotMatch(ownStoreSalesSource, /offerTrendRange/);
});

test("restricted detail ranges load full chart history with scope guards without replacing other detail data", () => {
  const fullHistoryLoader = competitorsSource.slice(
    competitorsSource.indexOf("async ([modalOpen, needsFullHistory, detailKey, scopeKey]"),
    competitorsSource.indexOf("[detailModalOpen, offerTrendScopeKey, offerTrendAvailableStart, offerTrendAvailableEnd]"),
  );
  assert.match(fullHistoryLoader, /if \(!modalOpen \|\| !needsFullHistory \|\| !selectedPlid\.value\) return;/);
  assert.match(fullHistoryLoader, /cachedCompetitorDetail\(cacheKey\)/);
  assert.match(fullHistoryLoader, /fetchCompetitorDetail\(\s*selectedPlid\.value,\s*undefined,\s*undefined,\s*detailOwnStoreScope\.value,\s*controller\.signal/);
  assert.match(fullHistoryLoader, /onCleanup\(\(\) => \{\s*cancelled = true;\s*controller\.abort\(\)/);
  assert.match(fullHistoryLoader, /if \(cancelled\) return;\s*offerTrendFullDetail\.value = result;/);
  assert.doesNotMatch(fullHistoryLoader, /\bdetail\.value\s*=|appliedStartDate\.value\s*=|appliedEndDate\.value\s*=/);
});

test("own-store official sales bars sit below comments and preserve zero versus missing evidence", () => {
  assert.match(competitorsSource, /<OwnStoreSalesChart/);
  assert.ok(
    competitorsSource.indexOf('class="competitor-offer-trend-tooltip"') <
      competitorsSource.indexOf("<OwnStoreSalesChart"),
  );
  assert.match(ownStoreSalesSource, /国内自然日（北京时间）/);
  assert.match(ownStoreSalesSource, /@pointermove="handlePointer"/);
  assert.match(ownStoreSalesSource, /@keydown\.left\.prevent="stepPoint\(-1\)"/);
  assert.match(ownStoreSalesSource, /缺失，不补 0/);
  assert.match(ownStoreSalesSource, /完整 0 件基线/);
  assert.match(ownStoreSalesSource, /按日展示/);
  assert.match(ownStoreSalesSource, /截至采集/);
  assert.match(ownStoreSalesSource, /class="own-sales-bar"/);
  assert.doesNotMatch(ownStoreSalesSource, /class="own-sales-line"/);
  assert.doesNotMatch(ownStoreSalesSource, /overflow-x\s*:\s*(?:auto|scroll)/);
});

test("overview and keyword details float while the competitor detail panel stays fixed", () => {
  assert.equal(overviewSource.match(/class="trend-hover-card"/g)?.length, 1);
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
