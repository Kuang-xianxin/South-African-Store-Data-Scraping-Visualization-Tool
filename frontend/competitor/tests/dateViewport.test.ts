import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  calendarMonthViewport,
  canMoveToNextMonth,
  normalizeCustomViewport,
  shiftMonthViewport,
} from "../src/dateViewport.ts";

const TODAY = "2026-08-11";
const appSource = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
const overviewSource = readFileSync(
  new URL("../src/pages/OverviewPage.vue", import.meta.url),
  "utf8",
);
const apiSource = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");

test("default viewport is the current calendar month through today", () => {
  assert.deepEqual(calendarMonthViewport(TODAY, TODAY), {
    startDate: "2026-08-01",
    endDate: TODAY,
    mode: "month",
  });
});

test("month navigation uses complete past calendar months and clamps the future", () => {
  assert.deepEqual(shiftMonthViewport("2026-08-01", -1, TODAY), {
    startDate: "2026-07-01",
    endDate: "2026-07-31",
    mode: "month",
  });
  assert.deepEqual(shiftMonthViewport("2026-07-01", 1, TODAY), {
    startDate: "2026-08-01",
    endDate: TODAY,
    mode: "month",
  });
  assert.deepEqual(shiftMonthViewport("2026-08-01", 1, TODAY), {
    startDate: "2026-08-01",
    endDate: TODAY,
    mode: "month",
  });
  assert.equal(canMoveToNextMonth("2026-07-01", TODAY), true);
  assert.equal(canMoveToNextMonth("2026-08-01", TODAY), false);
});

test("manual viewport keeps an ordered past range", () => {
  assert.deepEqual(
    normalizeCustomViewport("2026-07-15", "2026-08-05", TODAY, "end"),
    {
      startDate: "2026-07-15",
      endDate: "2026-08-05",
      mode: "custom",
    },
  );
  assert.deepEqual(
    normalizeCustomViewport("2026-08-09", "2026-08-05", TODAY, "start"),
    {
      startDate: "2026-08-09",
      endDate: "2026-08-09",
      mode: "custom",
    },
  );
  assert.deepEqual(
    normalizeCustomViewport("2026-08-01", "2026-09-01", TODAY, "end"),
    {
      startDate: "2026-08-01",
      endDate: TODAY,
      mode: "custom",
    },
  );
});

test("the global selector and overview data requests use the same range", () => {
  assert.match(
    appSource,
    /\['search-ranking', 'logistics', 'anomaly-products', 'container-selection', 'competitors', 'users'\]\.includes\(currentPage\)/,
  );
  assert.match(appSource, /<span>数据范围<\/span>/);
  assert.match(appSource, /<span>开始日期<\/span>/);
  assert.match(appSource, /<span>截止日期<\/span>/);
  assert.match(appSource, /@click="moveDataViewportMonth\(-1\)"/);
  assert.match(appSource, /@click="moveDataViewportMonth\(1\)"/);
  assert.match(appSource, /@change="updateDataViewportBoundary\('start', \$event\)"/);
  assert.match(appSource, /@change="updateDataViewportBoundary\('end', \$event\)"/);
  assert.match(appSource, /rangeStart: dataRangeStart\.value/);
  assert.match(appSource, /const common = \{\s+asOf: asOf\.value,\s+\}/);
  assert.match(appSource, /const anomalyAsOf = ref\(initialDataViewport\.endDate\)/);
  assert.match(appSource, /currentPage === 'anomaly-products'/);
  assert.match(appSource, /aria-label="异常商品数据日期"/);
  assert.match(appSource, /<span>数据日期<\/span>/);
  assert.match(appSource, /<span>选择日期<\/span>/);
  assert.match(appSource, /:value="anomalyAsOf"/);
  assert.match(appSource, /@change="updateAnomalyAsOf"/);
  assert.match(
    appSource,
    /if \(key === "anomaly-products"\) \{[\s\S]*?asOf: anomalyAsOf\.value/,
  );
  assert.doesNotMatch(appSource, /<span>数据截止日期<\/span>/);
  assert.match(overviewSource, /fetchStoreOverview\(\s*props\.rangeStart,\s*props\.rangeEnd/);
  assert.match(overviewSource, /fetchSummaryRange\(props\.rangeStart, props\.rangeEnd\)/);
  assert.match(apiSource, /new URLSearchParams\(\{ start_date: startDate, as_of: endDate \}\)/);
});
