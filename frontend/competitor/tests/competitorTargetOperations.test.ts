import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);
const apiSource = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");

test("global competitor targets only expose add and update operations", () => {
  assert.match(pageSource, /createCompetitorTarget/);
  assert.match(pageSource, /updateCompetitorTarget/);
  assert.doesNotMatch(pageSource, /\bdeleteCompetitorTarget\b/);
  assert.doesNotMatch(pageSource, /\bremoveTarget\b/);
  assert.doesNotMatch(pageSource, />\s*删除链接\s*</);
  assert.doesNotMatch(apiSource, /function deleteCompetitorTarget/);
});

test("historical delete audit copy stays read-only", () => {
  assert.match(pageSource, /历史删除留痕/);
});

test("own-store automatic tracking card never appears in true-competitor detail", () => {
  assert.match(
    pageSource,
    /v-if="\s*!props\.detailOnly\s*&& props\.isAdmin\s*&& selected\.来源 === 'own_store'\s*"\s*class="panel competitor-target-action-card own-store-auto-target"/,
  );
});
