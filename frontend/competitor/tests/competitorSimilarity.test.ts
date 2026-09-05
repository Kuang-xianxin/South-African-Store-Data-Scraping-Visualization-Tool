import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  rankCompetitorMatches,
  type CompetitorMatchSource,
} from "../src/competitorSimilarity.ts";
import type {
  CompetitorCategoryBreadcrumb,
  CompetitorItem,
} from "../src/types.ts";

const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);
const radarCardSource = readFileSync(
  new URL("../src/components/CompetitorRadarProductCard.vue", import.meta.url),
  "utf8",
);
const stylesSource = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

function category(name: string, id: string): CompetitorCategoryBreadcrumb {
  return { name, id, slug: null, type: "productline" };
}

const office = category("Office & Stationery", "office");
const projectors = category("Projectors & Accessories", "projectors");
const screens = category("Screens", "screens");
const projectorMounts = category("Projector Mounts", "mounts");

function item(
  plid: string,
  title: string,
  path: CompetitorCategoryBreadcrumb[],
  source: CompetitorItem["来源"] = "competitor",
  collectedAt = "2026-09-05T10:00:00Z",
): CompetitorItem {
  return {
    来源: source,
    plid,
    商品: title,
    类目路径: path,
    采集时间: collectedAt,
    跟卖报价: [],
    近期观察售出: { "30": 0 },
    周期销售额: 0,
    价格: 100,
    评论数: 0,
  } as CompetitorItem;
}

test("merges almost-identical and same-demand products in relevance order", () => {
  const source: CompetitorMatchSource = {
    plid: "source",
    商品: "100 Inch Retractable Projector Screen with Stand",
    类目路径: [office, projectors, screens],
  };
  const matches = rankCompetitorMatches(source, [
    item("same", "100 Inch Retractable Projector Screen with Tripod Stand", [office, projectors, screens]),
    item("need", "Portable Foldable Projector Screen for Home Cinema", [office, projectors, screens]),
  ]);

  assert.deepEqual(matches.map((match) => [match.item.plid, match.kind]), [
    ["same", "near_identical"],
    ["need", "same_need"],
  ]);
  assert.ok(matches[0]!.score > matches[1]!.score);
  assert.match(matches[0]!.reasons.join(" "), /同一精确类目/);
});

test("allows adjacent-category same-demand matches only with strong title evidence", () => {
  const source: CompetitorMatchSource = {
    plid: "source",
    商品: "Ceiling Projector Mount Adjustable Bracket",
    类目路径: [office, projectors, projectorMounts],
  };
  const matches = rankCompetitorMatches(source, [
    item("adjacent", "Universal Adjustable Projector Ceiling Bracket Mount", [office, projectors, screens]),
    item("unrelated", "Portable Projection Screen with Carry Bag", [office, projectors, screens]),
  ]);

  assert.deepEqual(matches.map((match) => match.item.plid), ["adjacent"]);
  assert.equal(matches[0]!.kind, "same_need");
});

test("excludes the source PLID, own-store rows, duplicates, and accessory-main conflicts", () => {
  const source: CompetitorMatchSource = {
    plid: "source",
    商品: "10 Inch Android Tablet 128GB",
    类目路径: [category("Computers", "computers"), category("Tablets", "tablets")],
  };
  const path = source.类目路径 ?? [];
  const matches = rankCompetitorMatches(source, [
    item("source", "10 Inch Android Tablet 128GB", path),
    item("own", "10 Inch Android Tablet 128GB", path, "own_store"),
    item("case", "Protective Case for 10 Inch Android Tablet 128GB", path),
    item("kept", "10 Inch Android Tablet 128GB WiFi", path, "competitor", "2026-09-05T09:00:00Z"),
    item("kept", "10 Inch Android Tablet 128GB WiFi Latest", path, "competitor", "2026-09-05T11:00:00Z"),
  ]);

  assert.deepEqual(matches.map((match) => match.item.plid), ["kept"]);
  assert.equal(matches[0]!.item.采集时间, "2026-09-05T11:00:00Z");
});

test("keeps a high-confidence title match clickable when category evidence is missing", () => {
  const matches = rankCompetitorMatches(
    { plid: "source", 商品: "Kada SMD Rework Station KD903D", 类目路径: [] },
    [item("same", "Kada SMD Rework Station KD903D", [])],
  );

  assert.equal(matches.length, 1);
  assert.equal(matches[0]!.kind, "near_identical");
  assert.equal(matches[0]!.score, 100);
});

test("every radar product-card surface exposes competitor query and the modal reuses the outer card", () => {
  assert.match(radarCardSource, />\s*竞品查询\s*<\/button>/);
  assert.match(pageSource, /openPersonalWatchlistCompetitorMatches\(card, \$event\)/);
  assert.ok(
    (pageSource.match(/openCompetitorMatchModal\(item, \$event\)/g)?.length ?? 0) >= 2,
  );
  assert.equal(pageSource.match(/<CompetitorRadarProductCard/g)?.length, 2);
  assert.match(pageSource, /COMPETITOR MATCHING/);
  assert.match(pageSource, /几乎同款/);
  assert.match(pageSource, /相同需求/);
  assert.doesNotMatch(pageSource, /competitorMatchKindFilter|competitor-match-evidence/);
  assert.match(radarCardSource, /<footer class="competitor-card-query-actions">[\s\S]*竞品查询/);
  assert.equal(pageSource.match(/<footer class="competitor-card-query-actions">/g)?.length, 3);
  assert.match(stylesSource, /\.competitor-match-result-list \{[\s\S]*grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(stylesSource, /\.competitor-product-detail-backdrop \{[\s\S]*z-index: 100/);
  assert.match(stylesSource, /\.competitor-match-backdrop \{[\s\S]*z-index: 94/);
});
