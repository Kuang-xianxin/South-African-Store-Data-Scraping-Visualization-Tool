import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createContext, runInContext } from "node:vm";
import ts from "typescript";

const page = readFileSync(new URL("../src/pages/CompetitorsPage.vue", import.meta.url), "utf8");
const script = page.match(/<script\b[^>]*>([\s\S]*?)<\/script>/)![1]!;
const parsed = ts.createSourceFile("page.ts", script, ts.ScriptTarget.ESNext, true);
const names = ["loadDetailCatalogItems", "openCategoryProductDetail", "handleWindowKeydown"];
const computedDeclarations = parsed.statements.filter((node) => ts.isVariableStatement(node)
  && node.declarationList.declarations.some((declaration) => ["catalogResultsLoading", "catalogResultsIncomplete"].includes(declaration.name.getText(parsed))))
  .map((node) => node.getText(parsed)).join("\n");
const compiled = ts.transpileModule(computedDeclarations + "\n" + names.map((name) => {
  const node = parsed.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === name);
  assert.ok(node);
  return node.getText(parsed);
}).join("\n") + "\nfunction catalogStatus() { return { loading: catalogResultsLoading.value, incomplete: catalogResultsIncomplete.value }; }",
{ compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;

function deferred() {
  let resolve!: (value: unknown) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness() {
  const requests: { args: unknown[]; task: ReturnType<typeof deferred> }[] = [];
  const calls: string[] = [];
  const request = (...args: unknown[]) => {
    const task = deferred(); requests.push({ args, task }); return task.promise;
  };
  const state = createContext({
    AbortController, HTMLElement: class {}, document: { activeElement: null },
    props: { detailOnly: true, embeddedDetailOnly: false },
    computed: (read: () => unknown) => ({ get value() { return read(); } }),
    categoryCatalogLoading: { value: false }, categoryCatalogError: { value: "" },
    loading: { value: true }, personalWatchlistOverviewLoading: { value: true },
    ownStoreScopeLoading: { value: true }, pageError: { value: "" }, personalWatchlistOverviewFailed: { value: false },
    ownStoreScopeCacheKey: () => "all|scope-a",
    appliedStartDate: { value: "2026-07-23" }, appliedEndDate: { value: "2026-09-07" },
    detailCatalogItems: { value: [] }, detailCatalogLoadedKey: { value: "" },
    detailCatalogLoading: { value: false }, detailCatalogError: { value: "" },
    detailCatalogController: null, detailCatalogRequest: null,
    fetchCompetitors: request, fetchCompetitorPersonalWatchlistOverview: request,
    catalogFromDetail: { value: true }, catalogDetailPlid: { value: "" },
    catalogDetailRevision: { value: 0 }, catalogDetailTrigger: null,
    competitorMatchModalOpen: { value: false }, categoryModalOpen: { value: true },
    detailModalOpen: { value: true }, collectionLogOpen: { value: false },
    personalWatchlistLibraryModalOpen: { value: false }, targetActionOpen: { value: false },
    targetAuditOpen: { value: false }, targetListOpen: { value: false },
    openProductDetail: (item: { 来源: string }, scope: string) => calls.push(`${item.来源}:${scope}`),
    closeCategoryModal: () => { state.categoryModalOpen.value = false; calls.push("category"); },
    closeCompetitorMatchModal: () => { state.competitorMatchModalOpen.value = false; calls.push("match"); },
    closeDetailView: () => calls.push("detail"),
  });
  runInContext(compiled, state);
  return { state, requests, calls };
}

test("standalone catalog coalesces clicks, includes radar and visible watchlist, and caches success", async () => {
  const { state, requests } = harness();
  const first = state.loadDetailCatalogItems();
  const second = state.loadDetailCatalogItems();
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[0]!.args.slice(0, 3), ["2026-07-23", "2026-09-07", "all"]);
  assert.equal(requests[0]!.args[4], false);
  requests[0]!.task.resolve({ items: [{ plid: "radar" }], store_items: [] });
  requests[1]!.task.resolve({ items: [{ plid: "saved" }], store_items: [{ plid: "own" }] });
  await Promise.all([first, second]);
  assert.equal(JSON.stringify(state.detailCatalogItems.value), '[{"plid":"radar"},{"plid":"saved"},{"plid":"own"}]');
  assert.equal(state.detailCatalogLoading.value, false);
  await state.loadDetailCatalogItems();
  assert.equal(requests.length, 2);
});

test("partial failure retains successful items, marks incompleteness, and permits retry", async () => {
  const { state, requests } = harness();
  const first = state.loadDetailCatalogItems();
  requests[0]!.task.resolve({ items: [{ plid: "radar" }], store_items: [] });
  requests[1]!.task.reject(new Error("unavailable"));
  await first;
  assert.equal(state.detailCatalogItems.value.length, 1);
  assert.ok(state.detailCatalogError.value);
  assert.equal(state.detailCatalogLoadedKey.value, "");
  const retry = state.loadDetailCatalogItems();
  assert.equal(requests.length, 4);
  requests[2]!.task.resolve({ items: [], store_items: [] });
  requests[3]!.task.resolve({ items: [], store_items: [] });
  await retry;
  assert.equal(state.detailCatalogError.value, "");
});

test("aborted or superseded detail catalog cannot publish late data", async () => {
  const { state, requests } = harness();
  const pending = state.loadDetailCatalogItems();
  state.detailCatalogController.abort();
  requests.forEach(({ task }) => task.resolve({ items: [{ plid: "stale" }], store_items: [] }));
  await pending;
  assert.equal(state.detailCatalogItems.value.length, 0);
  assert.equal(state.detailCatalogLoadedKey.value, "");
});

test("normal paginated radar loads the full catalog independently and caches it", async () => {
  const { state, requests } = harness();
  state.props.detailOnly = false;
  const pending = state.loadDetailCatalogItems();
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[0]!.args.slice(0, 3), ["2026-07-23", "2026-09-07", "all"]);
  assert.equal(requests[0]!.args[6], undefined);
  requests[0]!.task.resolve({ items: [{ plid: "outside-current-page" }], store_items: [] });
  requests[1]!.task.resolve({ items: [{ plid: "saved" }], store_items: [{ plid: "own" }] });
  await pending;
  assert.equal(JSON.stringify(state.detailCatalogItems.value),
    '[{"plid":"outside-current-page"},{"plid":"saved"},{"plid":"own"}]');
  await state.loadDetailCatalogItems();
  assert.equal(requests.length, 2);
});

test("detail queries finish loading even though the unused radar workspace was never mounted", () => {
  const { state } = harness();
  assert.equal(state.catalogStatus().loading, false);
  state.detailCatalogLoading.value = true;
  assert.equal(state.catalogStatus().loading, true);
  state.detailCatalogLoading.value = false;
  state.detailCatalogError.value = "failed";
  assert.ok(state.catalogStatus().incomplete);
  state.props.detailOnly = false;
  assert.equal(state.catalogStatus().loading, true);
});

test("Escape closes query then category without closing the standalone page", () => {
  const { state, calls } = harness();
  state.competitorMatchModalOpen.value = true;
  state.handleWindowKeydown({ key: "Escape" });
  assert.deepEqual(calls, ["match"]);
  state.handleWindowKeydown({ key: "Escape" });
  assert.deepEqual(calls, ["match", "category"]);
  state.catalogDetailPlid.value = "competitor";
  state.handleWindowKeydown({ key: "Escape" });
  assert.equal(calls.length, 2);
});

test("catalog competitor opens an overlay while own links keep the authorized all-store new-tab path", () => {
  const { state, calls } = harness();
  state.openCategoryProductDetail({ plid: "competitor", 来源: "competitor" });
  assert.equal(state.catalogDetailPlid.value, "competitor");
  assert.equal(state.catalogDetailRevision.value, 1);
  assert.equal(calls.length, 0);
  state.openCategoryProductDetail({ plid: "own", 来源: "own_store" });
  assert.deepEqual(calls, ["own_store:personal_watchlist"]);
});

for (const entry of ["matching", "category"]) {
  test(`${entry} result outside the radar page and personal pool opens complete detail`, () => {
    const { state, calls } = harness();
    state.props.detailOnly = false;
    state.catalogFromDetail.value = false;
    state.detailModalOpen.value = false;
    state.competitorMatchModalOpen.value = entry === "matching";
    state.categoryModalOpen.value = entry === "category";
    state.competitorMatchQuery = { value: "chicken" };
    state.competitorMatchPage = { value: 2 };
    // No radar page or personal-pool item exists for this independently loaded result.
    state.openCategoryProductDetail({ plid: "95895158", 来源: "competitor" });
    assert.equal(state.catalogDetailPlid.value, "95895158");
    assert.equal(state.catalogDetailRevision.value, 1);
    assert.equal(state.detailModalOpen.value, false);
    assert.equal(state.competitorMatchModalOpen.value, entry === "matching");
    assert.equal(state.categoryModalOpen.value, entry === "category");
    assert.equal(state.competitorMatchQuery.value, "chicken");
    assert.equal(state.competitorMatchPage.value, 2);
    assert.deepEqual(calls, []);
  });
}
