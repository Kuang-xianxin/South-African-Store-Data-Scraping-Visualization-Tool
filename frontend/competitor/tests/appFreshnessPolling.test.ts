import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8").replace(/\r\n/g, "\n");

test("sidebar freshness polls while the page is visible and cleans up its lifecycle", () => {
  assert.match(appSource, /const freshnessPollIntervalMs = 15_000/);
  assert.match(
    appSource,
    /freshnessTimer = window\.setTimeout\(\(\) => void loadFreshness\(\), freshnessPollIntervalMs\)/,
  );
  assert.match(
    appSource,
    /document\.addEventListener\("visibilitychange", handleFreshnessVisibilityChange\)/,
  );
  assert.match(
    appSource,
    /document\.removeEventListener\("visibilitychange", handleFreshnessVisibilityChange\)/,
  );
  assert.match(
    appSource,
    /if \(freshnessTimer !== null\) window\.clearTimeout\(freshnessTimer\)/,
  );
});

test("freshness updates immediately when the page becomes visible again", () => {
  assert.match(
    appSource,
    /function handleFreshnessVisibilityChange\(\) \{\s+if \(document\.visibilityState === "visible"\) \{\s+void loadFreshness\(\);/,
  );
});

test("refresh status polls quickly only while a refresh is active", () => {
  assert.match(appSource, /const refreshStatusActivePollIntervalMs = 2_000/);
  assert.match(appSource, /const refreshStatusIdlePollIntervalMs = 15_000/);
  assert.match(
    appSource,
    /const delay = refreshStatus\.value\.in_progress\s+\? refreshStatusActivePollIntervalMs\s+: refreshStatusIdlePollIntervalMs/,
  );
  assert.match(
    appSource,
    /refreshStatusTimer = window\.setTimeout\(\(\) => \{\s+refreshStatusTimer = null;\s+void loadRefreshStatus\(\);\s+\}, delay\)/,
  );
  assert.match(
    appSource,
    /if \(refreshStatusTimer !== null\) window\.clearTimeout\(refreshStatusTimer\)/,
  );
  assert.doesNotMatch(
    appSource,
    /setInterval\(\(\) => void loadRefreshStatus\(\), 2_000\)/,
  );
});

test("a transient freshness request failure preserves the last known timestamps", () => {
  const loadFreshnessBlock = appSource.slice(
    appSource.indexOf("async function loadFreshness"),
    appSource.indexOf("async function loadRefreshStatus"),
  );
  assert.match(loadFreshnessBlock, /const requestRevision = \+\+freshnessRequestRevision/);
  assert.match(loadFreshnessBlock, /const update = await fetchDataUpdates\(selectedStoreScope.value, controller.signal\)/);
  assert.match(
    loadFreshnessBlock,
    /if \(requestRevision === freshnessRequestRevision && !controller.signal.aborted\) \{[\s\S]*freshness\.value = update.freshness;/,
  );
  assert.match(
    loadFreshnessBlock,
    /catch \{\s+\/\/ Keep the last known timestamps during a short local-service interruption\.\s+\}/,
  );
  assert.doesNotMatch(loadFreshnessBlock, /catch\(\(\) => \(\{/);
});

test("switching stores clears the previous store state and rejects stale responses", () => {
  const storeWatcherBlock = appSource.slice(
    appSource.indexOf("watch(\n  () => selectedStore.value?.code"),
    appSource.indexOf("const activePageProps"),
  );
  assert.match(
    storeWatcherBlock,
    /freshness\.value = \{\s+last_collection_at: null,\s+latest_metric_date: null,\s+\};\s+refreshKey\.value \+= 1;\s+void loadFreshness\(\)/,
  );
  assert.match(appSource, /let freshnessRequestRevision = 0/);
});
