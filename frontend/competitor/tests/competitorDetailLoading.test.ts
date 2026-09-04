import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);
const apiSource = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");
const styleSource = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

test("does not request the first product detail while its modal is closed", () => {
  assert.match(
    pageSource,
    /if \(!modalOpen\) \{\s+detailLoading\.value = false;/,
  );
  assert.ok(pageSource.indexOf("if (!modalOpen)") < pageSource.indexOf("fetchCompetitorDetail("));
});

test("reuses a bounded detail cache for repeated card opens", () => {
  assert.match(pageSource, /const competitorDetailCacheLimit = 24;/);
  assert.match(pageSource, /const cached = cachedCompetitorDetail\(cacheKey\);/);
  assert.match(pageSource, /cacheCompetitorDetail\(cacheKey, result\);/);
});

test("competitor images retry transient proxy failures before showing a placeholder", () => {
  assert.match(pageSource, /const competitorImageRetryDelaysMs = \[500, 1_500\] as const;/);
  assert.match(pageSource, /function retryCompetitorImage\(/);
  assert.match(pageSource, /image\.dataset\.imageRetryAttempt/);
  assert.match(pageSource, /image\.src = competitorImageUrl\(url, nextAttempt\);/);
  assert.match(pageSource, /failedCompetitorImages\.value = new Set\(\);/);
  const proxiedImageCount = [...pageSource.matchAll(/:src="competitorImageUrl\(/g)].length;
  const retryingImageCount = [
    ...pageSource.matchAll(/@error="retryCompetitorImage\(\$event, /g),
  ].length;
  assert.ok(proxiedImageCount > 0);
  assert.equal(retryingImageCount, proxiedImageCount);
  assert.doesNotMatch(pageSource, /@error="markCompetitorImageFailed\(/);
});

test("the shared product detail modal always exposes the persisted category path", () => {
  assert.match(pageSource, /class="competitor-category-path"/);
  assert.match(pageSource, /商品具体类目/);
  assert.match(pageSource, /selectedCategoryPathText/);
  assert.match(pageSource, /末级类目 ID/);
  assert.match(pageSource, /成功完成一次公开商品采集后自动补齐/);
});

test("monitoring-link actions are the first card below the product detail header", () => {
  const detailModalIndex = pageSource.indexOf(
    'class="competitor-modal competitor-product-detail-modal"',
  );
  const monitoringActionsIndex = pageSource.indexOf(
    'v-if="!props.detailOnly && props.isAdmin && selected.来源 === \'competitor\'"',
    detailModalIndex,
  );
  const personalWatchlistIndex = pageSource.indexOf(
    'class="personal-watchlist-banner"',
    detailModalIndex,
  );
  const detailMetricsIndex = pageSource.indexOf(
    'class="competitor-modal-metrics"',
    detailModalIndex,
  );

  assert.ok(detailModalIndex >= 0);
  assert.ok(monitoringActionsIndex > detailModalIndex);
  assert.ok(personalWatchlistIndex > monitoringActionsIndex);
  assert.ok(detailMetricsIndex > personalWatchlistIndex);
});

test("only own-store cards leave the current page", () => {
  const dispatcher = pageSource.slice(
    pageSource.indexOf("function openProductDetail"),
    pageSource.indexOf("let handledRequestedDetailRevision"),
  );
  assert.match(
    dispatcher,
    /if \(item\.来源 !== "own_store"\) \{\s+openProductModal\(item, context\);\s+return;/,
  );
  assert.match(dispatcher, /openOwnStoreDetailTab\(\{/);
  assert.match(pageSource, /class="competitor-status-card own-store-card"[\s\S]*新标签页/);
  assert.match(pageSource, /class="competitor-status-card"[\s\S]*aria-haspopup="dialog"/);
});

test("standalone own-link detail loads its full local evidence concurrently", () => {
  assert.match(
    pageSource,
    /const request = Promise\.all\(\[/,
  );
  assert.match(pageSource, /fetchOwnStoreCompetitors\([\s\S]*fetchCompetitorDetail\(/);
  assert.match(pageSource, /fetchCompetitorPersonalWatchlist\(\)/);
  assert.match(pageSource, /requestedOwnStoreDetailRequests\.get\(key\)/);
  assert.match(pageSource, /requestedOwnStoreDetailCacheTtlMs = 15_000/);
  assert.match(pageSource, /requestedDetailStartDate\?: string/);
  assert.match(pageSource, /requestedDetailEndDate\?: string/);
  assert.match(pageSource, /cacheCompetitorDetail\(detailCacheKey, prefetchedDetail\)/);
  assert.match(pageSource, /applyPersonalWatchlistPayload\(personalWatchlist\)/);
  assert.match(pageSource, /<Teleport to="body" :disabled="props\.detailOnly">/);
  assert.match(apiSource, /ownStoreScope: OwnStoreScope = "current",\s+signal\?: AbortSignal/);
  assert.match(apiSource, /\/api\/competitors\/\$\{plid\}\$\{suffix\}`?, \{ signal \}/);
});

test("embedded radar detail reuses the full modal without loading or navigating the radar page", () => {
  assert.match(pageSource, /embeddedDetailOnly\?: boolean/);
  assert.match(pageSource, /payload\.current_item\?\.plid === plid/);
  assert.match(pageSource, /async function openRequestedEmbeddedDetail/);
  assert.match(pageSource, /loadRequestedEmbeddedDetail\(plid, startDate, endDate, scope\)/);
  assert.match(pageSource, /embeddedDetailItem\.value = item;\s+openProductModal\(item\);/);
  assert.match(pageSource, /targets\.value = result\.monitoring_target/);
  assert.match(pageSource, /personalWatchlistItems\.value = result\.personal_watchlist_item/);
  const embeddedMount = pageSource.slice(
    pageSource.indexOf("if (props.embeddedDetailOnly)"),
    pageSource.indexOf("const initialRequests"),
  );
  assert.doesNotMatch(embeddedMount, /loadTargets\(/);
  assert.match(pageSource, /v-if="!props\.detailOnly && !props\.embeddedDetailOnly"/);
  assert.match(pageSource, /class="competitor-modal competitor-product-detail-modal embedded-detail-loading-modal"/);
  assert.match(pageSource, /emit\("detail-closed"\)/);
  assert.doesNotMatch(
    pageSource.slice(
      pageSource.indexOf("async function openRequestedEmbeddedDetail"),
      pageSource.indexOf("function closeProductModal"),
    ),
    /window\.location|location\.hash|competitorDetailPageHref/,
  );
});

test("embedded radar detail prefetch reuses a bounded short-lived request cache", () => {
  assert.match(pageSource, /requestedDetailPrefetchPlid\?: string/);
  assert.match(pageSource, /requestedDetailPrefetchRevision\?: number/);
  assert.match(pageSource, /const requestedEmbeddedDetailCacheLimit = 12/);
  assert.match(pageSource, /const requestedEmbeddedDetailCacheTtlMs = 15_000/);
  assert.match(pageSource, /requestedEmbeddedDetailCache\.get\(key\)/);
  assert.match(pageSource, /requestedEmbeddedDetailRequests\.get\(key\)/);
  assert.match(pageSource, /async function prefetchRequestedEmbeddedDetail/);
  assert.match(pageSource, /await loadRequestedEmbeddedDetail\(/);
  assert.match(pageSource, /\(\) => props\.requestedDetailPrefetchRevision \?\? 0/);
  assert.match(pageSource, /void prefetchRequestedEmbeddedDetail\(revision, plid\)/);
});

test("standalone own-link detail groups its modules behind an accessible tab bar", () => {
  const detailModalIndex = pageSource.indexOf(
    'class="competitor-modal competitor-product-detail-modal"',
  );
  const metricsIndex = pageSource.indexOf('class="competitor-modal-metrics"', detailModalIndex);
  const tabsIndex = pageSource.indexOf('class="standalone-own-detail-tabs-shell"', detailModalIndex);
  const contentIndex = pageSource.indexOf('class="competitor-modal-content"', detailModalIndex);

  assert.ok(detailModalIndex >= 0);
  assert.ok(metricsIndex > detailModalIndex);
  assert.ok(tabsIndex > metricsIndex);
  assert.ok(contentIndex > tabsIndex);
  assert.match(
    pageSource,
    /v-if="props\.detailOnly && selected\.来源 === 'own_store'"\s+class="standalone-own-detail-tabs-shell"/,
  );
  assert.match(
    pageSource,
    /class="standalone-own-detail-tabs-heading"[\s\S]*详情标签页[\s\S]*点击下方标签切换不同内容[\s\S]*当前查看：/,
  );
  assert.match(pageSource, /class="standalone-own-detail-tabs" role="tablist"/);
  assert.match(pageSource, /role="tab"[\s\S]*:aria-selected=/);
  assert.match(pageSource, /:aria-controls="tab\.panelId"/);
  assert.match(pageSource, /:role="props\.detailOnly \? 'tabpanel' : undefined"/);
  assert.match(pageSource, /:aria-labelledby="props\.detailOnly \? activeStandaloneOwnDetailTabMeta\.tabId/);

  for (const label of ["报价与销量", "成本利润", "库存", "退货", "评论"]) {
    assert.match(pageSource, new RegExp(`label: "${label}"`));
  }
  for (const key of ["ArrowRight", "ArrowLeft", "Home", "End"]) {
    assert.match(pageSource, new RegExp(`event\\.key === "${key}"`));
  }

  assert.match(
    pageSource,
    /return !props\.detailOnly \|\| activeStandaloneOwnDetailTab\.value === tabId;/,
  );
  assert.match(
    pageSource,
    /if \(props\.detailOnly && item\.来源 === "own_store"\) \{\s+activeStandaloneOwnDetailTab\.value = "offers";/,
  );

  const moduleBindings = [
    ["profit", "own-profitability-panel"],
    ["inventory", "company-inventory-panel"],
    ["returns", "own-return-panel"],
    ["offers", "competitor-offer-workbench"],
    ["inventory", "variant-panel"],
    ["reviews", "reviews-panel"],
  ] as const;
  for (const [tabId, className] of moduleBindings) {
    const classIndex = pageSource.indexOf(`class="panel ${className}"`, contentIndex);
    const bindingIndex = pageSource.lastIndexOf(
      `v-show="showStandaloneOwnDetailModule('${tabId}')"`,
      classIndex,
    );
    assert.ok(classIndex > contentIndex, `${className} remains in the detail content`);
    assert.ok(
      bindingIndex > contentIndex && classIndex - bindingIndex < 180,
      `${className} is assigned to the ${tabId} tab`,
    );
  }

  assert.match(
    styleSource,
    /\.standalone-own-detail-tabs \{[\s\S]*grid-template-columns: repeat\(5, minmax\(0, 1fr\)\);/,
  );
  assert.match(
    styleSource,
    /@media \(max-width: 900px\) \{[\s\S]*\.standalone-own-detail-tabs \{[\s\S]*overflow-x: auto;/,
  );
  assert.match(
    styleSource,
    /\.standalone-own-detail-tabs-shell \{[\s\S]*border: 2px solid[\s\S]*box-shadow:/,
  );
  assert.match(
    styleSource,
    /\.standalone-own-detail-tabs button\.active \{[\s\S]*background: linear-gradient[\s\S]*color: #fff;/,
  );
  assert.match(
    styleSource,
    /\.standalone-own-detail-tabs button \{[\s\S]*justify-items: center;[\s\S]*text-align: center;/,
  );
});

test("shared seller offer cards label each readable variant name", () => {
  assert.match(
    pageSource,
    /<strong>变体：\{\{ offer\.变体 \|\| "默认款" \}\}<\/strong>/,
  );
  assert.match(pageSource, /· SKU \{\{ offer\.SKU \|\| "未返回" \}\}/);
});

test("standalone own-link detail keeps its enlarged actions fixed at the viewport bottom", () => {
  const actionsIndex = pageSource.lastIndexOf('<div class="competitor-modal-actions">');
  const actionsSource = pageSource.slice(actionsIndex, actionsIndex + 520);

  assert.ok(actionsIndex >= 0);
  assert.match(actionsSource, /打开当前卖家报价页/);
  assert.match(actionsSource, /关闭标签页/);
  assert.match(
    styleSource,
    /\.competitor-standalone-detail-page \.competitor-modal-actions \{[\s\S]*position: fixed;[\s\S]*bottom: 0;[\s\S]*width: min\(1680px, 100vw\);/,
  );
  assert.match(
    styleSource,
    /\.competitor-standalone-detail-page \.competitor-modal-actions a,[\s\S]*min-height: 48px;[\s\S]*font-size: 0\.84rem;/,
  );
  assert.match(
    styleSource,
    /\.competitor-standalone-detail-page \.competitor-modal-actions a,[\s\S]*display: inline-flex;[\s\S]*align-items: center;[\s\S]*justify-content: center;[\s\S]*text-align: center;/,
  );
  assert.match(
    styleSource,
    /@media \(min-width: 901px\) \{[\s\S]*\.competitor-standalone-detail-page \.competitor-modal-content \{[\s\S]*padding: 10px 16px 96px;/,
  );
});

test("standalone own-link detail omits the redundant green seller signal card", () => {
  const guardedDecisionCard = pageSource.indexOf(
    '<section v-if="!props.detailOnly" class="detail-grid modal-detail-grid">',
  );
  const sellerSignal = pageSource.indexOf("SELLER OFFER SIGNAL", guardedDecisionCard);
  const fallbackSignal = pageSource.indexOf("OPERATING SIGNAL", sellerSignal);

  assert.ok(guardedDecisionCard >= 0);
  assert.ok(sellerSignal > guardedDecisionCard);
  assert.ok(fallbackSignal > sellerSignal);
  assert.ok(fallbackSignal - guardedDecisionCard < 5_000);
  assert.match(
    pageSource,
    /v-show="showStandaloneOwnDetailModule\('reviews'\)"\s+class="detail-grid modal-detail-grid"/,
  );
  assert.match(
    pageSource,
    /class="competitor-modal-actions"[\s\S]*:href="selectedOfferLink"[\s\S]*打开当前卖家报价页/,
  );
});

test("large detail payloads stay shallow and paginate review nodes", () => {
  assert.match(pageSource, /const detail = shallowRef<CompetitorDetail>/);
  assert.match(pageSource, /const reviewPageSize = 20/);
  assert.match(pageSource, /filteredReviews\.value\.slice\(start, start \+ reviewPageSize\)/);
  assert.match(pageSource, /v-for="\(review, reviewIndex\) in visibleReviews"/);
  assert.match(pageSource, /class="compact-pagination detail-review-pagination"/);
});
