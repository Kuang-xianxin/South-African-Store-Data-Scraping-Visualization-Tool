import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);
const apiSource = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");
const appSource = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");

const scopeWatchSource = pageSource.slice(
  pageSource.indexOf("watch([ownStoreScope"),
  pageSource.indexOf("watch([selectedOfferKey"),
);
const scopeLoaderSource = pageSource.slice(
  pageSource.indexOf("async function loadOwnStoreScope"),
  pageSource.indexOf("async function loadSharedBatchStatus"),
);
const overviewLoaderSource = pageSource.slice(
  pageSource.indexOf("async function loadOverview"),
  pageSource.indexOf("async function loadOwnStoreScope"),
);

test("store selection changes only reload the scope-dependent partitions", () => {
  assert.match(scopeWatchSource, /\[ownStoreScope, \(\) => props\.currentStoreCode \?\? ""\]/);
  assert.match(scopeWatchSource, /void loadOwnStoreScope\(\)/);
  assert.doesNotMatch(scopeWatchSource, /loadOverview|loadTargets/);
  assert.match(scopeLoaderSource, /fetchOwnStoreCompetitors\(/);
  assert.match(scopeLoaderSource, /fetchCompetitorStoreTargets\(requestScope, controller\.signal\)/);
});

test("rapid scope changes abort and reject stale responses", () => {
  assert.match(scopeLoaderSource, /const requestId = \+\+ownStoreRequestId/);
  assert.match(scopeLoaderSource, /const targetRequestId = \+\+storeTargetRequestId/);
  assert.match(scopeLoaderSource, /ownStoreAbortController\?\.abort\(\)/);
  assert.match(scopeLoaderSource, /const controller = new AbortController\(\)/);
  assert.match(
    scopeLoaderSource,
    /requestId !== ownStoreRequestId[\s\S]*targetRequestId !== storeTargetRequestId[\s\S]*!ownStoreScopeStillCurrent\(requestScope, requestStoreCode\)/,
  );
  assert.match(
    overviewLoaderSource,
    /\+\+ownStoreRequestId[\s\S]*ownStoreAbortController\?\.abort\(\)[\s\S]*void loadOwnStoreScope\(\)/,
  );
});

test("full refresh returns the invariant true-competitor partition first", () => {
  assert.match(
    overviewLoaderSource,
    /fetchCompetitors\([\s\S]*controller\.signal,[\s\S]*false,[\s\S]*\)/,
  );
  assert.match(
    apiSource,
    /if \(!includeOwnStore\) query\.set\("include_own_store", "false"\)/,
  );
  assert.match(
    overviewLoaderSource,
    /competitors\.value = overview\.items[\s\S]*void loadOwnStoreScope\(\)/,
  );
});

test("scope responses are cached and switching shows an explicit loading state", () => {
  assert.match(pageSource, /const ownStoreOverviewCache = new Map/);
  assert.match(pageSource, /const storeTargetCache = new Map/);
  assert.match(scopeLoaderSource, /if \(cachedOverview && cachedTargets\)/);
  assert.match(scopeLoaderSource, /cacheScopeValue\(ownStoreOverviewCache/);
  assert.match(scopeLoaderSource, /cacheScopeValue\(storeTargetCache/);
  assert.match(pageSource, /v-if="ownStoreScopeLoading"[\s\S]*正在读取自有店铺数据/);
  assert.match(overviewLoaderSource, /ownStoreOverviewCache\.clear\(\)/);
  assert.match(
    pageSource,
    /async function loadTargets\(\) \{\s*storeTargetCache\.clear\(\)/,
  );
});

test("large card lists use shallow containers and memoized own-store cards", () => {
  assert.match(pageSource, /const competitors = shallowRef<CompetitorItem\[\]>/);
  assert.match(pageSource, /const storeCompetitors = shallowRef<CompetitorItem\[\]>/);
  assert.match(
    pageSource,
    /:key="`store-\$\{item\.plid\}`"[\s\S]*v-memo="\[[\s\S]*item,[\s\S]*selectedPlid === item\.plid/,
  );
});

test("competitor page remains mounted across store changes", () => {
  assert.match(appSource, /const competitorRefreshKey = ref\(0\)/);
  assert.match(
    appSource,
    /const pageComponentKey = computed\(\(\) =>[\s\S]*currentPage\.value === "competitors"[\s\S]*`competitors-\$\{competitorRefreshKey\.value\}`/,
  );
  assert.match(appSource, /currentStoreCode: selectedStore\.value\?\.code \?\? ""/);
  assert.match(appSource, /:key="pageComponentKey"/);
});

test("scope APIs accept cancellation signals and use the narrow endpoint", () => {
  assert.match(
    apiSource,
    /export function fetchOwnStoreCompetitors\([\s\S]*signal\?: AbortSignal[\s\S]*\/api\/competitors\/own-store\?\$\{query\.toString\(\)\}[\s\S]*\{ signal \}/,
  );
  assert.match(
    apiSource,
    /export async function fetchCompetitorStoreTargets\([\s\S]*signal\?: AbortSignal[\s\S]*\{ signal \}/,
  );
});
