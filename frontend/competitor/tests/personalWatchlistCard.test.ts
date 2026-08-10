import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);
const styleSource = readFileSync(
  new URL("../src/styles.css", import.meta.url),
  "utf8",
);

test("the personal pool is a standalone top workspace with direct card location", () => {
  const workspaceIndex = pageSource.indexOf("personal-operator-workspace");
  const sharedManagementIndex = pageSource.indexOf("shared-management-panel");
  assert.ok(workspaceIndex >= 0);
  assert.ok(sharedManagementIndex > workspaceIndex);
  assert.match(pageSource, /当前账号专属工作区/);
  assert.match(pageSource, /props\.currentUsername \|\| "当前账号"/);
  assert.match(pageSource, /个人监控池商品/);
  assert.match(pageSource, /personal-watchlist-product-grid/);
  assert.match(pageSource, /`personal-watchlist-card-\$\{card\.plid\}`/);
  assert.match(pageSource, /加入监控队列和我的监控池/);
  assert.match(pageSource, /定位到我的监控池/);
  assert.match(pageSource, /focusPersonalWatchlistCard\(plid\)/);
  assert.match(pageSource, /personalWatchlistPageForPlid/);
  assert.match(pageSource, /personalWatchlistHighlightPlid === card\.plid/);
  assert.match(pageSource, /从个人池移除/);
  assert.match(pageSource, /监控队列操作/);
  assert.doesNotMatch(pageSource, /togglePersonalWatchlistWorkspace/);
  assert.doesNotMatch(pageSource, /duplicateTargetHighlightPlid/);
  assert.match(pageSource, /全局链接与批次/);
  assert.match(pageSource, /管理员核心工作区/);
  assert.match(
    pageSource,
    /class="competitor-module"\s+:class="\{ 'admin-priority-layout': props\.isAdmin \}"/,
  );
  assert.match(
    pageSource,
    /:class="\{ 'operator-primary': !props\.isAdmin \}"/,
  );
  assert.match(
    pageSource,
    /v-if="props\.isAdmin"\s+class="collector panel shared-management-panel"/,
  );
  assert.match(pageSource, /v-if="props\.isAdmin && linkHealth\.length"/);
  assert.match(
    pageSource,
    /v-if="props\.isAdmin && selected\.来源 === 'competitor'"/,
  );
  assert.match(
    pageSource,
    /if \(props\.isAdmin\) initialRequests\.push\(loadSharedBatchStatus\(\)\)/,
  );
  assert.match(styleSource, /\.personal-watchlist-summary-card\s*\{/);
  assert.match(styleSource, /\.personal-operator-workspace\.operator-primary/);
  assert.match(styleSource, /\.personal-watchlist-product-card\.is-highlighted/);
  assert.match(styleSource, /@keyframes personal-watchlist-card-pulse/);
  assert.match(
    styleSource,
    /\.competitor-module > \.overview\s*\{\s*order: 3;/,
  );
  assert.match(
    styleSource,
    /\.competitor-module > \.collector\s*\{\s*order: 5;/,
  );
  assert.match(
    styleSource,
    /\.competitor-module\.admin-priority-layout > \.personal-operator-workspace\s*\{\s*order: 2;/,
  );
  assert.match(
    styleSource,
    /\.competitor-module\.admin-priority-layout > \.collector\s*\{\s*order: 3;/,
  );
  assert.match(
    styleSource,
    /\.competitor-module\.admin-priority-layout > \.metrics\s*\{\s*order: 4;/,
  );
  assert.match(
    styleSource,
    /\.competitor-module\.admin-priority-layout > \.link-health-panel\s*\{\s*order: 5;/,
  );
  assert.match(
    styleSource,
    /\.competitor-module\.admin-priority-layout > \.overview\s*\{\s*order: 6;/,
  );
  assert.match(
    styleSource,
    /\.competitor-module\.admin-priority-layout > \.shared-management-panel/,
  );
});
