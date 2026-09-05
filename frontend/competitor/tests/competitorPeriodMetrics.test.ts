import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  buildPersonalWatchlistWorkspaceCards,
  filterPersonalWatchlistWorkspaceCards,
  sortPersonalWatchlistWorkspaceCards,
} from "../src/personalWatchlistWorkspace.ts";
import type {
  CompetitorItem,
  CompetitorPersonalWatchlistItem,
} from "../src/types.ts";

const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);
const apiSource = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");
const observedSalesSource = readFileSync(
  new URL("../src/components/CompetitorObservedSalesMetrics.vue", import.meta.url),
  "utf8",
);
const ownSalesComparisonSource = readFileSync(
  new URL("../src/components/OwnStoreSalesComparisonMetrics.vue", import.meta.url),
  "utf8",
);
const stylesSource = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const typesSource = readFileSync(new URL("../src/types.ts", import.meta.url), "utf8");

function competitor(
  plid: string,
  periodSalesUnits: number,
  periodSalesAmount: number,
): CompetitorItem {
  return {
    来源: "competitor",
    plid,
    库存上限: "5",
    库存数量: 5,
    库存参考过期: false,
    库存净流出: 2,
    库存净变化: -2,
    周期销售件数: periodSalesUnits,
    周期销售额: periodSalesAmount,
    周期库存周转金额: periodSalesAmount,
    价格信号: "价格不变",
    趋势判断: "库存净流出（待验证）",
    新增评论: 0,
    新增好评: 0,
    新增差评: 0,
    新增跟卖卖家数: 0,
    跟卖报价: [],
    跟卖发现日期: [],
  } as CompetitorItem;
}

const memberships: CompetitorPersonalWatchlistItem[] = [
  {
    plid: "A",
    source: "competitor",
    added_at: "2026-08-11T00:00:00Z",
    library_ids: [],
  },
  {
    plid: "B",
    source: "competitor",
    added_at: "2026-08-11T00:00:00Z",
    library_ids: [],
  },
];

function rankedPersonalPlids(items: CompetitorItem[]): string[] {
  const cards = buildPersonalWatchlistWorkspaceCards(memberships, [], items);
  const filtered = filterPersonalWatchlistWorkspaceCards(cards, {
    source: "competitor",
    query: "",
    sellerQuery: "",
    stock: "全部",
    follower: "全部",
    signal: "库存减少",
  });
  return sortPersonalWatchlistWorkspaceCards(filtered, "库存减少", "desc")
    .map((card) => card.plid);
}

test("personal watchlist reranks from refreshed interval metrics", () => {
  assert.deepEqual(
    rankedPersonalPlids([competitor("A", 1, 500), competitor("B", 3, 100)]),
    ["B", "A"],
  );
  assert.deepEqual(
    rankedPersonalPlids([competitor("A", 5, 50), competitor("B", 2, 800)]),
    ["A", "B"],
  );
});

test("radar and category cards use link totals while seller workbench separates seller and variant totals", () => {
  assert.deepEqual(
    [...observedSalesSource.matchAll(/const windowDays = \[([^\]]+)\]/g)]
      .map((match) => match[1]?.replace(/\s/g, "")),
    ["7,15,30,60,90"],
  );
  assert.match(observedSalesSource, /近期库存观察售出/);
  assert.match(observedSalesSource, /<dl class="competitor-observed-sales-list">/);
  assert.match(observedSalesSource, /<dt>\{\{ days \}\}天：<\/dt>/);
  assert.match(observedSalesSource, /<dd>\{\{ observedUnitsLabel\(days\) \}\}<\/dd>/);
  assert.match(observedSalesSource, /库存观察 · 不等同订单/);
  assert.match(observedSalesSource, /embedded\?: boolean/);
  assert.match(observedSalesSource, /title\?: string/);
  assert.match(observedSalesSource, /contextLabel\?: string \| null/);
  assert.match(stylesSource, /\.competitor-observed-sales-list > div \{[\s\S]*grid-template-columns: 44px max-content[\s\S]*justify-content: start/);
  assert.match(stylesSource, /\.competitor-observed-sales-list dd \{[\s\S]*text-align: left/);
  assert.match(stylesSource, /\.competitor-observed-sales\.embedded/);
  assert.match(
    stylesSource,
    /\.competitor-offer-workbench > \.competitor-observed-sales \{[\s\S]*width: 100%/,
  );
  assert.match(
    stylesSource,
    /\.competitor-offer-workbench > \.competitor-observed-sales \.competitor-observed-sales-list \{[\s\S]*grid-template-columns: repeat\(5, minmax\(0, 1fr\)\)/,
  );
  assert.match(
    stylesSource,
    /\.competitor-offer-workbench > \.competitor-observed-sales \.competitor-observed-sales-list > div \{[\s\S]*grid-template-columns: minmax\(0, 1fr\)[\s\S]*justify-items: center/,
  );
  assert.match(
    stylesSource,
    /\.competitor-offer-workbench > \.competitor-observed-sales\.competitor-variant-observed-sales \{[\s\S]*background: #f4f7fb/,
  );
  assert.match(
    stylesSource,
    /\.competitor-status-summary \{[\s\S]*grid-template-columns: repeat\(5, minmax\(0, 1fr\)\) minmax\(220px, 250px\)/,
  );
  assert.match(
    stylesSource,
    /\.competitor-status-summary > \.competitor-observed-sales\.competitor-status-observed-sales \{[\s\S]*width: calc\(100% - 8px\)[\s\S]*max-width: 164px/,
  );
  assert.doesNotMatch(stylesSource, /competitor-observed-sales-grid/);
  assert.equal(
    pageSource.match(/<CompetitorObservedSalesMetrics/g)?.length,
    6,
  );
  assert.match(pageSource, /:values="card\.competitor\?\.近期观察售出"/);
  assert.match(pageSource, /全部卖家 · 全部变体/);
  assert.equal(
    pageSource.match(/:values="item\.近期观察售出"/g)?.length,
    2,
  );
  assert.equal(pageSource.match(/<OwnStoreSalesComparisonMetrics/g)?.length, 3);
  assert.match(pageSource, /:own-values="item\.自有官方销量"/);
  assert.match(pageSource, /:follower-values="item\.跟卖近期观察售出"/);
  assert.match(pageSource, /:own-values="card\.competitor\.自有官方销量"/);
  assert.match(pageSource, /:follower-values="card\.competitor\.跟卖近期观察售出"/);
  assert.match(ownSalesComparisonSource, /自有官方/);
  assert.match(ownSalesComparisonSource, /跟卖观察/);
  assert.match(ownSalesComparisonSource, /Seller Sales；跟卖为库存观察，不等同订单/);
  assert.match(ownSalesComparisonSource, /const windowDays = \[7, 15, 30, 60, 90\]/);
  assert.match(pageSource, /:values="selectedOffer\?\.卖家近期观察售出"/);
  assert.match(pageSource, /:values="selectedOffer\?\.变体近期观察售出"/);
  assert.match(pageSource, /title="当前卖家全部变体库存观察售出（件）"/);
  assert.match(pageSource, /title="当前变体单独库存观察售出（件）"/);
  assert.doesNotMatch(
    pageSource,
    /:values="detail\.current_item\?\.近期观察售出 \?\? selected\.近期观察售出"/,
  );
  assert.equal(
    pageSource.match(/class="competitor-status-observed-sales"/g)?.length,
    2,
  );
  assert.equal(
    [...pageSource.matchAll(
      /<CompetitorObservedSalesMetrics[\s\S]{0,180}?class="competitor-status-observed-sales"/g,
    )].length,
    2,
  );
  assert.equal(pageSource.match(/最新评论数（PLID 共用）/g)?.length, 4);
  assert.equal(pageSource.match(/class="competitor-first-monitored-badge/g)?.length, 4);
  assert.equal(pageSource.match(/<small>首次监控<\/small>/g)?.length, 4);
  assert.doesNotMatch(
    pageSource,
    /最新评论数（PLID 共用）[\s\S]{0,180}?首次监控/,
  );
  assert.equal(
    pageSource.match(/class="competitor-card-category(?: is-compact)?"/g)?.length,
    4,
  );
  assert.equal(pageSource.match(/aria-label="商品类目层级"/g)?.length, 4);
  assert.equal(pageSource.match(/类目待采集 · 后续成功采集后补齐/g)?.length, 4);
  assert.match(pageSource, /competitorCategoryLevelLabel\(index: number, total: number\)/);
  assert.match(pageSource, /if \(total <= 1 \|\| index === total - 1\) return "精确类目"/);
  assert.match(pageSource, /if \(index === 0\) return "大类"/);
  assert.doesNotMatch(pageSource, /competitorOperatingSignals\(item\)/);
  assert.match(
    stylesSource,
    /\.competitor-first-monitored-badge \{[\s\S]*background: linear-gradient\(135deg, #edf5ff, #dcecff\)/,
  );
  assert.match(
    stylesSource,
    /\.competitor-card-category li \{[\s\S]*grid-template-columns: 18px 52px minmax\(0, 1fr\)/,
  );
  assert.match(stylesSource, /\.competitor-card-category li > small,[\s\S]*grid-column: 2/);
  assert.match(stylesSource, /\.competitor-card-category li > strong,[\s\S]*grid-column: 3/);
  assert.match(stylesSource, /\.competitor-card-category li:not\(:last-child\)::after/);
  assert.match(pageSource, /latestReviewCountLabel\(card\.competitor\)/);
  assert.equal(pageSource.match(/latestReviewCountLabel\(item\)/g)?.length, 3);
  assert.match(typesSource, /首次监控时间\?: string \| null/);
  assert.match(typesSource, /最新评论数\?: number \| null/);
  assert.match(typesSource, /最新评论获取时间\?: string \| null/);
  assert.match(typesSource, /类目路径\?: CompetitorCategoryBreadcrumb\[\]/);
  assert.match(typesSource, /卖家近期观察售出\?: CompetitorObservedSalesWindows/);
  assert.match(typesSource, /变体近期观察售出\?: CompetitorObservedSalesWindows/);
  assert.match(typesSource, /自有官方销量\?: CompetitorObservedSalesWindows/);
  assert.match(typesSource, /跟卖近期观察售出\?: CompetitorObservedSalesWindows/);
  assert.match(
    stylesSource,
    /\.competitor-status-summary > \.own-store-sales-comparison \{[\s\S]*max-width: 238px/,
  );
  assert.match(
    stylesSource,
    /\.competitor-status-summary > \.own-store-sales-comparison \{[\s\S]*grid-column: 1 \/ -1/,
  );
});

test("stock-decrease filtering includes sales that were later replenished", () => {
  const tvStand = competitor("A", 50, 59_890);
  tvStand.库存净流出 = 0;
  tvStand.库存净变化 = 23;
  tvStand.趋势判断 = "检测到补货";
  const redLight = competitor("B", 25, 42_475);
  redLight.库存净流出 = 1;
  redLight.库存净变化 = -1;

  assert.deepEqual(rankedPersonalPlids([redLight, tvStand]), ["A", "B"]);
});

test("personal watchlist does not classify replenished inventory as unchanged", () => {
  const replenished = competitor("A", 0, 0);
  replenished.周期补货量 = 3;
  replenished.库存净变化 = 3;
  replenished.库存可比 = true;
  const unchanged = competitor("B", 0, 0);
  unchanged.周期补货量 = 0;
  unchanged.库存净变化 = 0;
  unchanged.库存可比 = true;
  const cards = buildPersonalWatchlistWorkspaceCards(
    memberships,
    [],
    [replenished, unchanged],
  );
  const filters = {
    source: "competitor" as const,
    query: "",
    sellerQuery: "",
    stock: "全部" as const,
    follower: "全部" as const,
  };

  assert.deepEqual(
    filterPersonalWatchlistWorkspaceCards(cards, {
      ...filters,
      signal: "补货",
    }).map((card) => card.plid),
    ["A"],
  );
  assert.deepEqual(
    filterPersonalWatchlistWorkspaceCards(cards, {
      ...filters,
      signal: "库存数量不变",
    }).map((card) => card.plid),
    ["B"],
  );
});

test("date-range apply shows true competitors before restoring the own-store partition", () => {
  assert.doesNotMatch(pageSource, /日期按北京时间自然日筛选/);
  assert.doesNotMatch(pageSource, /上述金额只是公开库存变化的观察口径/);
  assert.match(
    apiSource,
    /if \(startDate\) query\.set\("start_date", startDate\);[\s\S]*if \(endDate\) query\.set\("end_date", endDate\);/,
  );
  assert.match(
    pageSource,
    /async function applyDateRange\(\): Promise<void> \{[\s\S]*appliedStartDate\.value = rangeStartDate\.value;[\s\S]*appliedEndDate\.value = rangeEndDate\.value;[\s\S]*await loadOverview\(\);[\s\S]*?\n\}/,
  );
  const overviewLoader = pageSource.slice(
    pageSource.indexOf("async function loadOverview"),
    pageSource.indexOf("async function loadOwnStoreScope"),
  );
  assert.match(
    overviewLoader,
    /fetchCompetitors\(\s*appliedStartDate\.value,\s*appliedEndDate\.value,\s*requestScope,\s*controller\.signal,\s*false,\s*\)/,
  );
  assert.match(overviewLoader, /competitors\.value = overview\.items/);
  assert.match(
    overviewLoader,
    /trueCompetitorDateRange\.value = overview\.date_range[\s\S]*void loadOwnStoreScope\(\)/,
  );
  assert.match(
    pageSource,
    /buildPersonalWatchlistWorkspaceCards\([\s\S]*personalWatchlistItems\.value,[\s\S]*targets\.value,[\s\S]*personalWatchlistCompetitorItems\.value,/,
  );
});

test("personal watchlist hydrates its PLIDs before the full radar projection", () => {
  assert.match(
    apiSource,
    /\/api\/competitors\/personal-watchlist\/overview\$\{suffix\}/,
  );
  const projectionStart = apiSource.indexOf(
    "export function fetchCompetitorPersonalWatchlistOverview",
  );
  const projectionEnd = apiSource.indexOf("\nexport ", projectionStart + 1);
  const projectionSource = apiSource.slice(projectionStart, projectionEnd);
  assert.doesNotMatch(projectionSource, /ownStoreScope|own_store_scope/);
  assert.match(
    pageSource,
    /async function loadOverview\(\)[\s\S]*void loadPersonalWatchlistOverview\(\);[\s\S]*competitors\.value = overview\.items;/,
  );
  assert.match(
    pageSource,
    /fetchCompetitorPersonalWatchlist\(\)\.then\([\s\S]*applyPersonalWatchlistPayload\(payload\);/,
  );
  assert.match(
    pageSource,
    /personalWatchlistCompetitorItems = computed\(\(\) => \[[\s\S]*allCompetitorItems\.value,[\s\S]*personalWatchlistOverviewItems\.value,/,
  );
  assert.match(
    pageSource,
    /fetchCompetitorPersonalWatchlistOverview\(\s*appliedStartDate\.value,\s*appliedEndDate\.value,\s*\)/,
  );
  assert.match(pageSource, /正在恢复商品详情/);
  assert.match(pageSource, /等待首次采集/);
  assert.match(pageSource, /无权查看店铺详情/);
  assert.doesNotMatch(pageSource, /个人池内店铺详情始终按当前账号全部已授权店铺读取/);
  assert.doesNotMatch(pageSource, /已加入两个清单，首次采集完成后/);
});

test("shared-library items can open detail without leaving an invisible scroll lock", () => {
  assert.match(
    pageSource,
    /const selected = computed\(\(\) => \{[\s\S]*const preferredItems =[\s\S]*const fallbackItems =[\s\S]*return preferredItems\.find\([\s\S]*\?\? fallbackItems\.find\(/,
  );
  assert.match(
    pageSource,
    /if \(!personalWatchlistCompetitorItems\.value\.some\([\s\S]*selectedPlid\.value = personalWatchlistCompetitorItems\.value\[0\]\?\.plid \?\? "";/,
  );
  assert.match(
    pageSource,
    /\[\s*detailModalOpen,\s*\(\) => selected\.value !== null,[\s\S]*personalWatchlistLibraryModalOpen,[\s\S]*detailOpen,[\s\S]*detailSelected,[\s\S]*personalLibraryDialogOpen,[\s\S]*\(!props\.detailOnly && detailOpen && detailSelected\)[\s\S]*\|\| personalLibraryDialogOpen/,
  );
  assert.doesNotMatch(
    pageSource,
    /personalWatchlistLibraryModalOpen\.value = true;\s*document\.body\.style\.overflow/,
  );
  assert.match(pageSource, /v-if="detailModalOpen && selected"/);
});
