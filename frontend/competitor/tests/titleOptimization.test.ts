import assert from "node:assert/strict";
import test from "node:test";
import { keywordRankingContext, keywordRankingRows, primaryTitle, titleDiagnosis, titleWordChanges } from "../src/titleOptimization.ts";
import type { SearchRankingAnalysis, SearchRankingKeywordResult, SearchRankingTitleStrategy } from "../src/types.ts";

const keyword = (overrides: Partial<SearchRankingKeywordResult> = {}) => ({
  id: 1, keyword: "cat house", candidate_order: 0, relevance_status: "accepted", validation_evidence: {},
  pages_scanned: 1, found: true, organic_rank: 6, page_number: 1, observed_at: "2026-09-09T06:23:50",
  ...overrides,
}) as SearchRankingKeywordResult;

test("unadopted queries retain observed ranks and all queries remain available", () => {
  const input = [keyword({ id: 1, relevance_status: "rejected_irrelevant", organic_rank: 1 }),
    keyword({ id: 2, candidate_order: 1, organic_rank: 6 }),
    keyword({ id: 3, candidate_order: 2, found: false, organic_rank: null, pages_scanned: 3 })];
  const original = structuredClone(input);
  const rows = keywordRankingRows(input);
  assert.deepEqual(rows.map((row) => row.item.id), [2, 3, 1]);
  assert.equal(rows[2].rank, "#1");
  assert.equal(rows[2].relation, "未纳入推荐");
  assert.equal(rows[1].rank, "扫描范围内未找到");
  assert.equal(rows[1].scope, "已扫描 3 页");
  assert.deepEqual(input, original);
});

test("unsearched candidates do not turn record creation times into collection times", () => {
  const [row] = keywordRankingRows([keyword({ pages_scanned: 0, found: false, organic_rank: null, relevance_status: "model_low_confidence" })]);
  assert.equal(row.rank, "未采集");
  assert.equal(row.observedAt, null);
  assert.match(row.scope, /未发起搜索/);
});

test("homepage-only filtering never implies a full search or a rank beyond the window", () => {
  const [row] = keywordRankingRows([keyword({ relevance_status: "rejected_irrelevant", found: false, organic_rank: null })]);
  assert.equal(row.rank, "扫描范围内未找到");
  assert.equal(row.scope, "仅检查首页，未继续翻页");
  assert.equal(row.observedAt, "2026-09-09T06:23:50");
});

test("incomplete and contradictory rank records never display fabricated positions", () => {
  for (const organic_rank of [null, 0, -1, 1.5, Number.NaN]) {
    const [row] = keywordRankingRows([keyword({ organic_rank })]);
    assert.equal(row.rank, "待核实");
    assert.equal(row.located, false);
  }
  assert.equal(keywordRankingRows([keyword({ found: false, organic_rank: 8 })])[0].located, false);
  assert.equal(keywordRankingRows([keyword({ pages_scanned: 0 })])[0].rank, "未采集");
});

test("another family PLID is explicitly a reference while same-PLID offers share rank", () => {
  const data = analysis({ source_offer_id: "one", source_title: "Cat House", variant_family: { variants: [{ offer_id: "one", productline_id: "100" }] } });
  assert.equal(keywordRankingContext(data, "Cat House", "one", "100").note, "");
  assert.equal(keywordRankingContext(data, "Cat House", "two", "100").sameLink, true);
  assert.equal(keywordRankingContext(data, "Cat House", "two", "200").sameLink, false);
  assert.match(keywordRankingContext(data, "Cat House", "two", "200").note, /并非当前链接的实测结果/);
  assert.equal(keywordRankingContext(data, "Cat House", "two", null).sameLink, false);
});

test("title changes retain historical positions with a resampling notice", () => {
  const data = analysis({ source_offer_id: "one", source_title: "Cat House" });
  assert.equal(keywordRankingContext(data, " cat  HOUSE ", "one").note, "");
  assert.match(keywordRankingContext(data, "Foldable Cat House", "one").note, /当前标题已变化/);
});

test("historical analyses with no stored family variants retain ranks without claiming another link", () => {
  for (const variant_family of [undefined, null, {}, { variants: [] }]) {
    const data = analysis({ source_offer_id: "224260871", source_title: "Cat House", variant_family });
    const original = structuredClone(data);
    assert.deepEqual(keywordRankingContext(data, "Cat House", "224260871", "100"), { sameLink: true, note: "" });
    assert.match(keywordRankingContext(data, "Foldable Cat House", "224260871", "100").note, /当前标题已变化/);
    const other = keywordRankingContext(data, "Cat House", "another-offer", "100");
    assert.equal(other.sameLink, false);
    assert.match(other.note, /并非当前链接的实测结果/);
    assert.deepEqual(data, original);
  }
});

const analysis = (overrides: Record<string, unknown> = {}) => ({
  title_score: { band: "strong", current_title_match: true, components: [{ available: true, score: 20, max_points: 20, summary: "身份明确" }] },
  ...overrides,
}) as unknown as SearchRankingAnalysis;

test("good titles may remain unchanged and stale evidence cannot authorize adoption", () => {
  assert.equal(titleDiagnosis(analysis(), "Brand Cat Storage Box").keep, true);
  assert.equal(titleDiagnosis(analysis({ variant_projection: { family_snapshot_current: false } }), "New title").usable, false);
  assert.equal(titleDiagnosis(analysis({ variant_projection: { decision_parameter_confirmation_current: false } }), "New title").usable, false);
  assert.equal(titleDiagnosis(analysis({ title_score: { current_title_match: false } }), "New title").usable, false);
  assert.equal(titleDiagnosis(null, "Title").label, "待分析");
});

test("missing evidence stays undecided and primary title prefers a valid core strategy", () => {
  assert.equal(titleDiagnosis(analysis({ title_score: { band: "insufficient_evidence" } }), "Title").usable, false);
  const strategies = [
    { strategy: "adjacent_opportunity", available: true, title: "Alternative" },
    { strategy: "contiguous_core", available: true, title: "Core" },
  ] as SearchRankingTitleStrategy[];
  assert.equal(primaryTitle(strategies)?.title, "Core");
  assert.equal(primaryTitle([{ ...strategies[1], available: false }]), null);
});

test("title comparison distinguishes added facts from reordering existing words", () => {
  assert.deepEqual(titleWordChanges("Brand Blue Cat Box", "Brand Cat Box Blue"), { added: [], removed: [], reordered: true });
  assert.deepEqual(titleWordChanges("Brand Cat Box", "Brand Cat Storage Box"), { added: ["Storage"], removed: [], reordered: false });
  assert.deepEqual(titleWordChanges("Brand Cat BOX", "Brand Cat Box"), { added: [], removed: [], reordered: false });
});
