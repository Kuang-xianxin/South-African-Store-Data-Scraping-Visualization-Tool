import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  COMPETITOR_OFFER_TREND_HORIZONTAL_LAYOUT,
  buildCompetitorOfferTrendLayout,
} from "../src/competitorTrendLayout.ts";

const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);
const styleSource = readFileSync(
  new URL("../src/styles.css", import.meta.url),
  "utf8",
);

test("own-store Seller API offers add rolling traffic to the aligned trend panels", () => {
  assert.match(pageSource, /const showOwnTrafficPanel = computed/);
  assert.match(pageSource, /key: "traffic"/);
  assert.match(pageSource, /label: "近30天浏览量"/);
  assert.match(pageSource, /offerTrendXAtTime/);
  assert.match(pageSource, /alignOwnStoreTrafficTrendToOfferTrend/);
  assert.match(pageSource, /offerTrendPanelCount\.value \? 4 : 3|showOwnTrafficPanel\.value \? 4 : 3/);
  assert.match(pageSource, /按最近报价节点对齐/);
  assert.match(pageSource, /真实流量快照时间/);
  assert.match(pageSource, /不是当天流量或访客数/);
  assert.match(pageSource, /'with-own-traffic': showOwnTrafficPanel/);
  assert.match(styleSource, /\.competitor-offer-workbench-grid\.with-own-traffic\s*\{[\s\S]*grid-template-columns: minmax\(0, 1fr\)/);
});

test("title-change traffic nodes use a stronger marker without filling missing traffic", () => {
  assert.match(pageSource, /'title-change': point\.titleChanged/);
  assert.match(pageSource, /offer-trend-title-change-missing/);
  assert.match(styleSource, /\.offer-trend-point\.title-change\s*\{/);
  assert.match(styleSource, /stroke-width: 4;/);
});

test("three and four-panel competitor trends use compact non-overlapping geometry", () => {
  const fourPanel = buildCompetitorOfferTrendLayout(4);
  const threePanel = buildCompetitorOfferTrendLayout(3);

  assert.equal(fourPanel.chartHeight, 392);
  assert.equal(threePanel.chartHeight, 346);
  assert.ok(fourPanel.chartHeight <= 400);
  assert.ok(fourPanel.plotHeight < threePanel.plotHeight);

  for (const [panelCount, layout] of [[4, fourPanel], [3, threePanel]] as const) {
    const lastPanelTop = layout.panelTop + (panelCount - 1) * layout.panelStride;
    const lastSurfaceBottom = lastPanelTop - layout.surfaceTopOffset + layout.surfaceHeight;
    assert.equal(lastSurfaceBottom, layout.cursorBottom);
    assert.ok(lastPanelTop + layout.plotHeight < layout.xAxisLabelY);
  }
});

test("trend metadata, y-axis labels, and plot use dedicated horizontal columns", () => {
  const horizontal = COMPETITOR_OFFER_TREND_HORIZONTAL_LAYOUT;

  assert.ok(horizontal.panelTextDividerX - horizontal.panelTextX >= 96);
  assert.ok(horizontal.axisLabelX - horizontal.panelTextDividerX >= 48);
  assert.ok(horizontal.plotLeft - horizontal.axisLabelX >= 12);
  assert.ok(horizontal.plotRight - horizontal.plotLeft >= 760);
  assert.match(pageSource, /class="offer-trend-panel-text-divider"/);
  assert.match(
    pageSource,
    /COMPETITOR_OFFER_TREND_HORIZONTAL_LAYOUT\.axisLabelX/,
  );
  assert.match(styleSource, /\.offer-trend-panel-text-divider\s*\{/);
});

test("short desktop competitor details compact sticky chrome around the fixed readout", () => {
  assert.match(
    pageSource,
    /class="competitor-modal-backdrop competitor-product-detail-backdrop"/,
  );
  assert.match(
    pageSource,
    /class="competitor-modal competitor-product-detail-modal"/,
  );
  assert.match(styleSource, /@media \(min-width: 901px\) and \(max-height: 900px\)/);
  assert.match(
    styleSource,
    /\.competitor-product-detail-modal \.competitor-modal-header\s*\{[\s\S]*?padding: 12px 18px 10px;/,
  );
  assert.match(
    styleSource,
    /\.competitor-product-detail-modal \.competitor-modal-actions\s*\{[\s\S]*?padding: 10px 18px;/,
  );
});
