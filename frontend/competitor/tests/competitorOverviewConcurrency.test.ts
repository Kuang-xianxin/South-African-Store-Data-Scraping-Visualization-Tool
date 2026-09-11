import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { setImmediate } from "node:timers/promises";
import { createContext, runInContext } from "node:vm";
import test from "node:test";
import ts from "typescript";
import { effectiveCompetitorSortMetric } from "../src/competitorListSort.ts";

const source = readFileSync(new URL("../src/pages/CompetitorsPage.vue", import.meta.url), "utf8");
const functions = source.slice(source.indexOf("function mainListQuery("), source.indexOf("function applyTrueCompetitorPage("))
  + source.slice(source.indexOf("async function loadOverview()"), source.indexOf("async function loadSharedBatchStatus("));
const compiled = ts.transpileModule(functions, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
const range = { available_start: "2026-07-01", available_end: "2026-09-05", selected_start: "2026-07-01", selected_end: "2026-09-05" };

function deferred() {
  let resolve!: (value: any) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<any>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function harness() {
  const dates = deferred();
  const own = deferred();
  const main = deferred();
  const calls: string[] = [];
  const context: Record<string, any> = {
    AbortController, Map, Set, effectiveCompetitorSortMetric,
    categoryCatalogRequestId: 0, categoryCatalogAbortController: null,
    overviewRequestId: 0, ownStoreRequestId: 0, storeTargetRequestId: 0,
    overviewAbortController: null, ownStoreAbortController: null, listPagesReady: false,
    props: { isAdmin: false, currentStoreCode: "current" },
    ownStoreOverviewCache: new Map(), storeTargetCache: new Map(),
    ownStoreScopeCacheKey: () => "scope", storeTargetScopeCacheKey: () => "scope",
    ownStoreScopeStillCurrent: () => true,
    cacheScopeValue: (cache: Map<any, any>, key: string, value: any) => cache.set(key, value),
    loadPersonalWatchlistOverview: () => undefined,
    closeCategoryModal: () => undefined, closeCompetitorMatchModal: () => undefined,
    isAbortError: () => false,
    mergedCompetitorDateRange: () => range,
    fetchCompetitorDateRange: () => { calls.push("dates"); return dates.promise; },
    fetchOwnStoreCompetitors: (start: string, end: string, ...args: any[]) => {
      calls.push("own"); assert.equal(start, range.selected_start); assert.equal(end, range.selected_end);
      assert.equal(args[4].page, 2); assert.equal(args[4].page_size, 20);
      assert.equal(args[4].sort, context.competitorListSortMetric.value);
      return own.promise;
    },
    fetchCompetitorStoreTargets: async () => ({ items: [] }),
    fetchCompetitors: (...args: any[]) => {
      calls.push("main"); assert.equal(args[6].page, 1); assert.equal(args[6].page_size, 20);
      assert.equal(args[6].sort, effectiveCompetitorSortMetric(context.competitorListSortMetric.value, false));
      context.previewMain = args[5];
      return main.promise;
    },
    applyOwnStoreOverview: (value: any) => { context.storeCompetitors.value = value.store_items; },
    applyStoreTargetPayload: () => { calls.push("apply-targets"); },
  };
  for (const [, name] of functions.matchAll(/\b(\w+)\.value\b/g)) context[name!] ??= { value: "" };
  context.ownStoreScope.value = "all";
  context.competitorPage.value = 1;
  context.storeCompetitorPage.value = 2;
  context.competitorPageSize.value = 20;
  context.competitorListSortMetric.value = "sales_30";
  context.competitorSourceView.value = "own_store";
  context.personalWatchlistCompetitorItems.value = [];
  context.storeCompetitors.value = [{ plid: "previous" }];
  createContext(context);
  runInContext(compiled, context);
  return { context, dates, own, main, calls };
}

test("the visible own-store partition starts with resolved dates and renders before the true list", async () => {
  const { context, dates, own, main, calls } = harness();
  const pending = context.loadOverview();
  assert.deepEqual(calls, ["dates"]);
  dates.resolve(range);
  await setImmediate();
  assert.deepEqual(calls, ["dates", "own", "main"]);
  assert.equal(context.storeCompetitors.value[0].plid, "previous");
  // A concurrent target-list read must not discard the independently valid cards.
  context.storeTargetRequestId++;
  own.resolve({ store_items: [{ plid: "own" }], date_range: range });
  await setImmediate();
  assert.equal(context.storeCompetitors.value[0].plid, "own");
  assert.deepEqual(calls, ["dates", "own", "main"]);
  assert.ok(!calls.includes("apply-targets"));
  assert.equal(context.storeTargetCache.size, 0);
  main.resolve({ items: [{ plid: "true" }], date_range: range });
  await pending;
  assert.equal(context.competitors.value[0].plid, "true");
});

test("list requests carry the selected period, direction and signal independently for each source", () => {
  const { context } = harness();
  context.competitorListSortMetric.value = "follower_sales_90";
  context.competitorListSortDirection.value = "asc";
  context.competitorSignalFilter.value = "库存减少";
  const own = context.mainListQuery(true);
  assert.equal(own.sort, "follower_sales_90");
  assert.equal(own.direction, "asc");
  assert.equal(own.signal, "库存减少");
  assert.equal(context.mainListQuery(false).sort, "sales_90");
});

test("a slow visible own-store read does not block the true competitor cards", async () => {
  const { context, dates, own, main, calls } = harness();
  const pending = context.loadOverview();
  dates.resolve(range);
  await setImmediate();
  assert.deepEqual(calls, ["dates", "own", "main"]);
  main.resolve({ items: [{ plid: "true" }], date_range: range });
  await pending;
  assert.equal(context.competitors.value[0].plid, "true");
  assert.equal(context.ownStoreScopeLoading.value, true);
  own.resolve({ store_items: [{ plid: "own" }], date_range: range });
  await setImmediate();
  assert.equal(context.storeCompetitors.value[0].plid, "own");
});

test("a slow true partition does not delay the independent own-store read", async () => {
  const { context, dates, own, main, calls } = harness();
  context.competitorSourceView.value = "competitor";
  const pending = context.loadOverview();
  assert.deepEqual(calls, ["dates"]);
  dates.resolve(range);
  await setImmediate();
  assert.deepEqual(calls, ["dates", "main", "own"]);
  assert.equal(context.ownStoreScopeLoading.value, true);
  own.resolve({ store_items: [{ plid: "own" }], date_range: range });
  await setImmediate();
  assert.equal(context.storeCompetitors.value[0].plid, "own");
  assert.equal(context.loading.value, true);
  main.resolve({ items: [{ plid: "true" }], date_range: range });
  await pending;
  assert.deepEqual(calls, ["dates", "main", "own", "apply-targets"]);
  assert.equal(context.competitors.value[0].plid, "true");
});

test("a complete cached preview unlocks the secondary read exactly once while refresh is pending", async () => {
  const { context, dates, own, main, calls } = harness();
  context.competitorSourceView.value = "competitor";
  const pending = context.loadOverview();
  dates.resolve(range);
  await setImmediate();
  context.previewMain({ items: [{ plid: "cached" }], date_range: range }, "123");
  context.previewMain({ items: [{ plid: "cached" }], date_range: range }, "123");
  assert.deepEqual(calls, ["dates", "main", "own"]);
  assert.equal(context.competitors.value[0].plid, "cached");
  own.resolve({ store_items: [{ plid: "own" }], date_range: range });
  main.resolve({ items: [{ plid: "true" }], date_range: range });
  await pending;
  assert.equal(calls.filter(call => call === "own").length, 1);
});

test("a failed visible read still starts the secondary partition after dates are resolved", async () => {
  const { context, dates, own, main, calls } = harness();
  context.competitorSourceView.value = "competitor";
  const pending = context.loadOverview();
  dates.resolve(range);
  await setImmediate();
  main.reject(new Error("visible read failed"));
  await pending;
  assert.deepEqual(calls, ["dates", "main", "own"]);
  own.resolve({ store_items: [{ plid: "own" }], date_range: range });
  await setImmediate();
  assert.equal(context.storeCompetitors.value[0].plid, "own");
});

test("a superseded date request cannot start heavy reads or overwrite the newer dates", async () => {
  const { context, dates, calls } = harness();
  const pending = context.loadOverview();
  context.overviewRequestId++;
  dates.resolve(range);
  await pending;
  assert.deepEqual(calls, ["dates"]);
  assert.equal(context.appliedStartDate.value, "");
});

test("a superseded own-store response cannot replace the selected store's cards", async () => {
  const { context, own } = harness();
  context.appliedStartDate.value = range.selected_start;
  context.appliedEndDate.value = range.selected_end;
  const pending = context.loadOwnStoreScope();
  context.ownStoreRequestId++;
  own.resolve({ store_items: [{ plid: "stale" }], date_range: range });
  await pending;
  assert.equal(context.storeCompetitors.value.length, 0);
  assert.equal(context.ownStoreOverviewCache.size, 0);
});
