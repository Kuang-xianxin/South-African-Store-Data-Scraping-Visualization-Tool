import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const pageSource = readFileSync(
  new URL("../src/pages/SearchRankingPage.vue", import.meta.url),
  "utf8",
);
const typesSource = readFileSync(new URL("../src/types.ts", import.meta.url), "utf8");
const statusTypeSource = typesSource.slice(
  typesSource.indexOf("export interface SearchRankingStatus"),
  typesSource.indexOf("export interface SearchRankingAnalysisSummary"),
);
const summaryTypeSource = typesSource.slice(
  typesSource.indexOf("export interface SearchRankingAnalysisSummary"),
  typesSource.indexOf("export interface SearchRankingProduct"),
);
const analysisTypeSource = typesSource.slice(
  typesSource.indexOf("export interface SearchRankingAnalysis extends"),
  typesSource.indexOf("export interface SearchRankingListPayload"),
);

test("normalizes current and analyzed titles case-insensitively", () => {
  const comparisonBlock = pageSource.slice(
    pageSource.indexOf("const currentTitleChangedSinceAnalysis"),
    pageSource.indexOf("const acceptedKeywords"),
  );

  assert.equal(comparisonBlock.match(/\.toLocaleLowerCase\(\)/g)?.length, 2);
});

test("an explicit strategy contract never reuses legacy reasons for unavailable cards", () => {
  assert.match(
    pageSource,
    /explanation: hasStrategyContract\s+\? "本轮没有按当前核心词门槛形成可用的连续词组方案。"\s+: current\.title_reason/,
  );
  assert.match(
    pageSource,
    /旧记录中的相邻需求建议没有经过现行的实际命中与首页低竞争门槛，已安全停用/,
  );
});

test("uses the current required search-ranking response contract", () => {
  assert.doesNotMatch(statusTypeSource, /opportunity_first_page_threshold/);
  assert.match(statusTypeSource, /opportunity_max_direct_competitors: number;/);
  assert.match(statusTypeSource, /opportunity_max_organic_rank: number;/);
  assert.match(summaryTypeSource, /vision_stage_completed: boolean;/);
  assert.match(summaryTypeSource, /\n  usage: \{/);
  assert.match(summaryTypeSource, /estimated_cost_cny: number \| null;/);
  assert.doesNotMatch(
    summaryTypeSource,
    /(vision_stage_completed|usage|estimated_cost_cny)\?:/,
  );
});

test("allows per-provider usage and cost evidence on model attempts", () => {
  assert.match(
    analysisTypeSource,
    /provider_attempts\?: Array<\{[\s\S]*?usage\?: \{[\s\S]*?input_tokens\?: number;[\s\S]*?output_tokens\?: number;[\s\S]*?total_tokens\?: number;[\s\S]*?estimated_cost_cny\?: number \| null;[\s\S]*?\}>;/,
  );
});

test("separates unusable billed attempts from reusable model results", () => {
  assert.match(pageSource, /const latestAttemptHasUnusableSpend = computed/);
  assert.match(pageSource, /v-else-if="latestAttemptHasUnusableSpend"/);
  assert.match(
    pageSource,
    /模型请求已产生[\s\S]*但识别结构不可用，重试不能复用本次结果。/,
  );
});

test("attributes ranking validation to the previously adopted strategy", () => {
  assert.match(analysisTypeSource, /matched_suggestion\?: string;/);
  assert.match(pageSource, /修改后复采归属/);
  assert.match(pageSource, /上一轮实际采用：/);
  assert.match(pageSource, /不代表本轮三张候选卡已经采用/);
});
