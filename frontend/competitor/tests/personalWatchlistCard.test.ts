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

test("personal type libraries separate owner controls from read and edit sharing", () => {
  assert.match(pageSource, /openPersonalWatchlistLibrarySettings/);
  assert.match(pageSource, /openPersonalWatchlistCardLibraries\(card\)/);
  assert.match(pageSource, /togglePersonalWatchlistLibrarySelection\(library\.id\)/);
  assert.match(pageSource, /updatePersonalWatchlistItemLibraries/);
  assert.match(pageSource, /promptForPersonalWatchlistDefault\(url\)/);
  assert.match(pageSource, /!personalWatchlistDefaultConfigured\.value/);
  assert.match(pageSource, /personalWatchlistDefaultSelection\.value/);
  assert.match(pageSource, /:value="null"/);
  assert.match(pageSource, /filteredPersonalWatchlistCards/);
  assert.match(pageSource, /personalWatchlistLibraryFilter === library\.id/);
  assert.match(pageSource, /unclassifiedPersonalWatchlistCount/);
  assert.match(pageSource, /recountPersonalWatchlistLibraries\(/);
  assert.match(pageSource, /updatePersonalWatchlistLibraryShares/);
  assert.match(pageSource, /personalWatchlistSharePermissionFor/);
  assert.match(pageSource, /SHARED WITH ME/);
  assert.match(pageSource, /只读/);
  assert.match(pageSource, /可编辑/);
  assert.match(pageSource, /library\.access === "owner"/);
  assert.match(pageSource, /deletePersonalWatchlistLibraryItem/);
  assert.match(pageSource, /共享库仅传递库内 PLID/);
  assert.match(styleSource, /\.personal-watchlist-library-modal\s*\{/);
  assert.match(styleSource, /\.personal-watchlist-library-filter\s*\{/);
  assert.match(styleSource, /\.personal-watchlist-share-user-list\s*\{/);
  assert.match(styleSource, /\.shared-with-me-library-grid\s*\{/);
  assert.match(styleSource, /background:\s*#fff/);
});

test("own-store quick add avoids the full overview reload and selected offers drive the hero image", () => {
  const ownStoreBranchStart = pageSource.indexOf("if (result.automatic_store_target)");
  const ownStoreBranchEnd = pageSource.indexOf("if (!result.item)", ownStoreBranchStart);
  assert.ok(ownStoreBranchStart >= 0);
  assert.ok(ownStoreBranchEnd > ownStoreBranchStart);
  const ownStoreBranch = pageSource.slice(ownStoreBranchStart, ownStoreBranchEnd);
  assert.match(ownStoreBranch, /setPersonalWatchlistLocal/);
  assert.match(ownStoreBranch, /focusPersonalWatchlistCard/);
  assert.doesNotMatch(ownStoreBranch, /loadOverview/);
  assert.match(pageSource, /const selectedHeroImage = computed/);
  assert.match(pageSource, /selectedOffer\.value\?\./);
  assert.match(pageSource, /canShowCompetitorImage\(selectedHeroImage\)/);
  assert.match(pageSource, /ownStoreVariantCount\(item\)/);
  assert.match(pageSource, /offer\.TSIN/);
});
