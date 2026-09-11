import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createContext, runInContext } from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/pages/CompetitorsPage.vue", import.meta.url), "utf8");
const script = source.match(/<script\b[^>]*>([\s\S]*?)<\/script>/)![1]!;
const parsed = ts.createSourceFile("page.ts", script, ts.ScriptTarget.ESNext, true);
const actions = ["loadMatchingCatalog", "loadMatchingCards"].map((name) => {
  const fn = parsed.statements.find((n) => ts.isFunctionDeclaration(n) && n.name?.text === name);
  assert.ok(fn);
  return fn.getText(parsed);
}).join("\n");
const compiled = ts.transpileModule(actions, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;

function harness() {
  const requests: any[] = [];
  const ref = (value: unknown) => ({ value });
  const context = createContext({
    AbortController, Map,
    competitorMatchModalOpen: ref(true), matchingCatalog: ref({ revision: "one", items: [] }),
    matchingCatalogLoading: ref(false), matchingCatalogError: ref(""), matchingPreviewAt: ref(""),
    matchingCardsLoading: ref(false), matchingCardsError: ref(""), matchingCardPreviewAt: ref(""),
    matchingUnavailable: ref([]), matchingCards: ref(new Map()), matchingCatalogController: null,
    matchingCardsController: null, competitorMatchPage: ref(1), competitorMatchPageSize: 20,
    filteredCompetitorMatchResults: ref(Array.from({ length: 45 }, (_, i) => ({ item: { plid: String(i + 1) } }))),
    appliedStartDate: ref("2026-09-01"), appliedEndDate: ref("2026-09-11"),
    fetchCompetitorMatchCards(plids, start, end, signal, preview) {
      return new Promise((resolve, reject) => requests.push({ plids, start, end, signal, preview, resolve, reject }));
    },
    fetchCompetitorMatchCatalog(signal, preview) {
      return new Promise((resolve, reject) => requests.push({ signal, preview, resolve, reject }));
    },
  });
  runInContext(compiled, context);
  return { context, requests };
}

test("only the current page is hydrated and a delayed previous page cannot replace it", async () => {
  const { context: state, requests } = harness();
  const first = state.loadMatchingCards();
  assert.deepEqual(Array.from(requests[0].plids), Array.from({ length: 20 }, (_, i) => String(i + 1)));
  state.competitorMatchPage.value = 3;
  const third = state.loadMatchingCards();
  assert.ok(requests[0].signal.aborted);
  assert.deepEqual(Array.from(requests[1].plids), ["41", "42", "43", "44", "45"]);
  requests[1].resolve({ items: [{ plid: "41", 商品: "Third page" }], unavailable_plids: [] });
  await third;
  requests[0].resolve({ items: [{ plid: "1", 商品: "Old page" }], unavailable_plids: [] });
  await first;
  assert.deepEqual([...state.matchingCards.value.keys()], ["41"]);
});

test("matching waits for the small directory, not an incomplete seed list", async () => {
  const { context: state, requests } = harness();
  state.matchingCatalog.value = null;
  await state.loadMatchingCards();
  assert.equal(requests.length, 0);
});

test("failed refresh preserves a complete preview and does not invent zeros", async () => {
  const { context: state, requests } = harness();
  const read = state.loadMatchingCards();
  requests[0].preview({ items: [{ plid: "1", 价格: null }], unavailable_plids: [] }, "2026-09-11T02:00:00Z");
  requests[0].reject(new Error("offline"));
  await read;
  assert.equal(state.matchingCards.value.get("1").价格, null);
  assert.equal(state.matchingCardPreviewAt.value, "2026-09-11T02:00:00Z");
  assert.ok(state.matchingCardsError.value);
});

test("unchanged matching identity keeps its indexed array while changed titles replace it", async () => {
  const { context: state, requests } = harness();
  const original = state.matchingCatalog.value;
  const first = state.loadMatchingCatalog();
  requests[0].resolve({ revision: "one", items: [] });
  await first;
  assert.equal(state.matchingCatalog.value, original);
  const second = state.loadMatchingCatalog();
  requests[1].resolve({ revision: "two", items: [{ plid: "2", 商品: "New title" }] });
  await second;
  assert.notEqual(state.matchingCatalog.value, original);
  assert.equal(state.matchingCatalog.value.items[0].商品, "New title");
});
