import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createContext, runInContext } from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/pages/CompetitorsPage.vue", import.meta.url), "utf8");
const script = source.match(/<script\b[^>]*>([\s\S]*?)<\/script>/)![1]!;
const parsed = ts.createSourceFile("page.ts", script, ts.ScriptTarget.ESNext, true);
const actions = [
  "clearCompetitorMatchBlockedNotice",
  "openCompetitorMatchModal", "openPersonalWatchlistCompetitorMatches",
  "closeCompetitorMatchModal", "openCategoryFromCompetitorMatch",
].map((name) => {
  const declaration = parsed.statements.find(
    (node) => ts.isFunctionDeclaration(node) && node.name?.text === name,
  );
  assert.ok(declaration, `Missing production action: ${name}`);
  return declaration.getText(parsed);
}).join("\n");
const compiled = ts.transpileModule(actions, {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText;

class ElementStub {
  focusCount = 0;
  focus() { this.focusCount++; }
  closest() { return null; }
}

function harness() {
  let catalogLoads = 0;
  let now = 0;
  let nextTimerId = 1;
  const timers = new Map<number, { callback: () => void; due: number }>();
  const ticks: Array<() => void> = [];
  const externalCalls: string[] = [];
  const context = createContext({
    document: { activeElement: null },
    window: {
      setTimeout(callback: () => void, delay: number) {
        const id = nextTimerId++;
        timers.set(id, { callback, due: now + delay });
        return id;
      },
      clearTimeout(id: number) { timers.delete(id); },
    },
    HTMLElement: ElementStub,
    nextTick: (callback: () => void) => { ticks.push(callback); },
    fetch: () => { externalCalls.push("fetch"); },
    openCategoryModal: () => { externalCalls.push("category"); },
    loadMatchingCatalog: () => { catalogLoads++; },
    categoryCatalogItems: { value: [] },
    matchingCards: { value: new Map() },
    matchingCatalogController: null,
    matchingCardsController: null,
    competitorMatchModalOpen: { value: false },
    categoryModalOpen: { value: false },
    catalogFromDetail: { value: false },
    competitorMatchSource: { value: null },
    competitorMatchQuery: { value: "" },
    competitorMatchPage: { value: 1 },
    competitorMatchModalTrigger: null,
    competitorMatchModalCloseButton: { value: new ElementStub() },
    competitorMatchBlockedNotice: { value: new ElementStub() },
    competitorMatchBlockedMessage: { value: "" },
    competitorMatchBlockedTimer: null,
  });
  runInContext(compiled, context);
  return {
    context, externalCalls, catalogLoads: () => catalogLoads,
    pendingTimers: () => timers.size,
    advance(milliseconds: number) {
      now += milliseconds;
      for (const [id, timer] of timers) {
        if (timer.due > now) continue;
        timers.delete(id);
        timer.callback();
      }
    },
    flush: () => { ticks.splice(0).forEach((tick) => tick()); },
  };
}

const original = { plid: "111", 商品: "Original product", 类目路径: [{ id: "1", name: "Category" }], 价格: 100 };
const nested = { plid: "222", 商品: "Matched product", 类目路径: [], 价格: 90 };

test("repeat queries preserve the original source, search, page and return focus", () => {
  const { context: state, externalCalls, catalogLoads, flush } = harness();
  const trigger = new ElementStub();
  state.openCompetitorMatchModal(original, { currentTarget: trigger });
  flush();
  const originalSource = state.competitorMatchSource.value;
  state.competitorMatchQuery.value = "tent";
  state.competitorMatchPage.value = 3;

  // Both a result-card click and a direct action call must hit the same guard.
  state.openCompetitorMatchModal(nested, { currentTarget: new ElementStub() });
  state.openCompetitorMatchModal(nested);
  flush();
  assert.equal(state.competitorMatchSource.value, originalSource);
  assert.equal(state.competitorMatchQuery.value, "tent");
  assert.equal(state.competitorMatchPage.value, 3);
  assert.equal(state.competitorMatchModalTrigger, trigger);
  assert.equal(state.competitorMatchModalOpen.value, true);
  assert.match(state.competitorMatchBlockedMessage.value, /已拦截重复查询.*请关闭当前竞品查询窗口/);
  assert.equal(state.competitorMatchBlockedNotice.value.focusCount, 2);
  assert.deepEqual(externalCalls, []);
  assert.equal(catalogLoads(), 1, "only the first query loads the authorized own-store catalog");
});

test("rapid clicks and personal-watchlist entry cannot replace an open query", () => {
  const { context: state, flush } = harness();
  state.openCompetitorMatchModal(original);
  state.openPersonalWatchlistCompetitorMatches({ competitor: nested }, {});
  flush();
  assert.equal(state.competitorMatchSource.value.plid, original.plid);
  assert.equal(state.competitorMatchModalCloseButton.value.focusCount, 1);
  assert.equal(state.competitorMatchBlockedNotice.value.focusCount, 1);
});

test("closing the query restores its original trigger and permits a fresh query", () => {
  const { context: state, flush, pendingTimers } = harness();
  const trigger = new ElementStub();
  state.openCompetitorMatchModal(original, { currentTarget: trigger });
  state.openCompetitorMatchModal(nested);
  flush();
  state.closeCompetitorMatchModal();
  assert.equal(pendingTimers(), 0);
  flush();
  assert.equal(trigger.focusCount, 1);
  assert.equal(state.competitorMatchBlockedMessage.value, "");
  assert.equal(state.competitorMatchModalOpen.value, false);
  assert.equal(state.competitorMatchSource.value, null);
  state.openCompetitorMatchModal(nested);
  assert.equal(state.competitorMatchSource.value.plid, nested.plid);
  assert.equal(state.competitorMatchBlockedMessage.value, "");
});

test("leaving query results for a category clears the block and allows its normal query", () => {
  const { context: state, externalCalls, pendingTimers } = harness();
  state.openCompetitorMatchModal(original);
  state.openCompetitorMatchModal(nested);
  state.openCategoryFromCompetitorMatch(original.类目路径[0], {});
  assert.equal(pendingTimers(), 0);
  assert.equal(state.competitorMatchModalOpen.value, false);
  assert.equal(state.competitorMatchBlockedMessage.value, "");
  assert.deepEqual(externalCalls, ["category"]);
  state.openCompetitorMatchModal(nested);
  assert.equal(state.competitorMatchSource.value.plid, nested.plid);
});

test("the notice dismisses after three seconds while the query and its state stay open", () => {
  const { context: state, advance, flush, pendingTimers, catalogLoads } = harness();
  state.openCompetitorMatchModal(original);
  state.competitorMatchQuery.value = "shower";
  state.competitorMatchPage.value = 2;
  state.openCompetitorMatchModal(nested);
  flush();
  state.document.activeElement = state.competitorMatchBlockedNotice.value;
  advance(2999);
  assert.notEqual(state.competitorMatchBlockedMessage.value, "");
  advance(1);
  assert.equal(state.competitorMatchBlockedMessage.value, "");
  assert.equal(pendingTimers(), 0);
  assert.equal(state.competitorMatchModalCloseButton.value.focusCount, 2);
  assert.equal(state.competitorMatchModalOpen.value, true);
  assert.equal(state.competitorMatchSource.value.plid, original.plid);
  assert.equal(state.competitorMatchQuery.value, "shower");
  assert.equal(state.competitorMatchPage.value, 2);
  state.openCompetitorMatchModal(nested);
  assert.notEqual(state.competitorMatchBlockedMessage.value, "");
  assert.equal(state.competitorMatchSource.value.plid, original.plid);
  assert.equal(catalogLoads(), 1);
});

test("another blocked click restarts the full delay without accumulating timers", () => {
  const { context: state, advance, pendingTimers } = harness();
  state.openCompetitorMatchModal(original);
  state.openCompetitorMatchModal(nested);
  advance(2000);
  state.openCompetitorMatchModal(nested);
  assert.equal(pendingTimers(), 1);
  advance(1000);
  assert.notEqual(state.competitorMatchBlockedMessage.value, "");
  advance(1999);
  assert.notEqual(state.competitorMatchBlockedMessage.value, "");
  advance(1);
  assert.equal(state.competitorMatchBlockedMessage.value, "");
});

test("automatic dismissal preserves focus after the user moves to another control", () => {
  const { context: state, advance, flush } = harness();
  state.openCompetitorMatchModal(original);
  state.openCompetitorMatchModal(nested);
  flush();
  const searchInput = new ElementStub();
  state.document.activeElement = searchInput;
  advance(3000);
  assert.equal(state.competitorMatchBlockedMessage.value, "");
  assert.equal(state.document.activeElement, searchInput);
  assert.equal(state.competitorMatchModalCloseButton.value.focusCount, 1);
});

test("result cards use the guarded action and the notice remains outside the scrolling results", () => {
  const start = source.indexOf('v-if="competitorMatchModalOpen && competitorMatchSource"');
  const modal = source.slice(start, source.indexOf("</Teleport>", start));
  assert.match(modal, /@query-competitors="openCompetitorMatchModal"/);
  assert.match(modal, /v-if="competitorMatchBlockedMessage"\s+ref="competitorMatchBlockedNotice"[\s\S]*?role="alert"\s+tabindex="-1"/);
  assert.ok(modal.indexOf('ref="competitorMatchBlockedNotice"') < modal.indexOf('class="competitor-category-results competitor-match-results"'));
});
