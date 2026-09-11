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

test("includes own-store alternatives but excludes the source, duplicates, and accessory-main conflicts", () => {
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

  assert.deepEqual(matches.map((match) => match.item.plid), ["own", "kept"]);
  assert.equal(matches[1]!.item.采集时间, "2026-09-05T11:00:00Z");
});

const health = category("Health", "health");
const healthCare = category("Health Care", "health-care");
const firstAidPath = [health, healthCare, category("First Aid", "first-aid"), category("First Aid Supplies", "first-aid-supplies"), category("Equipment", "equipment")];
const chokingPath = [health, healthCare, category("Health Supplies & Equipment", "health-supplies"), category("Health Equipment", "health-equipment"), category("Anti-Choking Devices", "anti-choking")];
const chokingProducts = [
  item("100472483", "Anti Choking Device Rescue Kit Portable for Adult Child Emergency Aid", [], "own_store"),
  item("100146502", "Anti Choking Device Kit Adult Child Infant Pocket Resuscitator", firstAidPath, "own_store"),
  item("100409965", "Anti Choking Device Portable Rescue Kit for Adult Child and Infant", firstAidPath, "own_store"),
  item("100472178", "Anti-Choking Device Portable Rescue Kit for Adult, Child and Infant", firstAidPath, "own_store"),
  item("93447568", "Anti Choking Kit (Adult & Child)", chokingPath),
];

test("each anti-choking link finds the other own links and the monitored link across missing or different leaves", () => {
  for (const source of chokingProducts) {
    const matches = rankCompetitorMatches(source, chokingProducts);
    assert.deepEqual(
      new Set(matches.map((match) => match.item.plid)),
      new Set(chokingProducts.filter((item) => item.plid !== source.plid).map((item) => item.plid)),
      `Missing alternatives for ${source.plid}`,
    );
  }
});

test("retains own identity even when a newer public snapshot has the same PLID", () => {
  const owned = chokingProducts[1]!;
  const publicCopy = { ...owned, 来源: "competitor" as const, 采集时间: "2026-09-07T12:00:00Z" };
  for (const candidates of [[owned, publicCopy], [publicCopy, owned]]) {
    const matches = rankCompetitorMatches(chokingProducts[0]!, candidates);
    assert.equal(matches.length, 1);
    assert.equal(matches[0]!.item, owned);
  }
});

test("title evidence tolerates missing categories without matching generic emergency products or replacement accessories", () => {
  const unrelated = [
    item("first-aid", "Portable First Aid Emergency Rescue Kit for Adult Child", firstAidPath),
    item("mask", "Replacement Mask for Anti Choking Device Adult Child Kit", chokingPath),
    item("bag", "Carry Case for Anti Choking Device Kit Adult Child", chokingPath),
  ];
  for (const source of chokingProducts) {
    assert.deepEqual(rankCompetitorMatches(source, unrelated), [], `False match for ${source.plid}`);
  }
  const matches = rankCompetitorMatches(
    { plid: "source", 商品: "Portable Retractable Projector Screen with Stand", 类目路径: [] },
    [item("screen", "Retractable Projector Screen with Tripod Stand", [])],
  );
  assert.equal(matches.length, 1);
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

test("cat houses and climbing towers match reciprocally by explicit use across marketplace categories", () => {
  const pets = [category("Pets", "17"), category("Equipment & Accessories", "26669")];
  const cats = [
    item("102111538", "Cat Foldable Villa Cat Storage Box With Scratching Pad Yellow", [...pets, category("Litter & Accessories", "26711"), category("Litter Boxes", "26714")], "own_store"),
    item("103316517", "Cat Tree Tower Cat Scratching Post Tower Climb Play House", [...pets, category("Toys & Scratchers", "26715"), category("Scratchers", "26721")], "own_store"),
    item("103316653", "Cat Tree Tower Cat Scratching Post Tower Climb Play House", [], "own_store"),
  ];
  const unrelated = [
    item("1", "Automatic Cat Litter Box", cats[0]!.类目路径!),
    item("2", "Enclosed Cat Toilet Box with Drawer and Litter Scoop", cats[0]!.类目路径!),
    item("3", "Replacement Scratching Pad for Cat Tree Tower", cats[1]!.类目路径!),
    item("4", "Cat Carrier Transport House Bag", cats[1]!.类目路径!),
    item("5", "Cat Food Storage Box Yellow", cats[0]!.类目路径!),
  ];
  const candidates = [...cats, ...unrelated];
  for (const source of cats) {
    const matches = rankCompetitorMatches(source, candidates);
    assert.deepEqual(new Set(matches.map((m) => m.item.plid)), new Set(cats.filter((x) => x !== source).map((x) => x.plid)));
    if (source === cats[0]) assert.ok(matches.every((m) => m.kind === "same_need"));
    assert.deepEqual(rankCompetitorMatches(source, candidates), matches, "warm inverted index must preserve results");
  }
  for (const source of unrelated) assert.deepEqual(rankCompetitorMatches(source, cats), []);
});

test("every radar product-card surface exposes competitor query and the modal reuses the outer card", () => {
  assert.match(radarCardSource, />\s*竞品查询\s*<\/button>/);
  assert.match(pageSource, /openPersonalWatchlistCompetitorMatches\(card, \$event\)/);
  assert.ok(
    (pageSource.match(/openCompetitorMatchModal\(item, \$event\)/g)?.length ?? 0) >= 2,
  );
  assert.equal(pageSource.match(/<CompetitorRadarProductCard/g)?.length, 2);
  assert.match(pageSource, /COMPETITOR MATCHING/);
  assert.match(pageSource, /系统已有商品（含自有链接） · 按相关度排序/);
  assert.match(pageSource, /rankCompetitorMatches\(competitorMatchSource\.value, competitorMatchCandidates\.value\)/);
  assert.doesNotMatch(pageSource, /competitorMatchKindFilter|competitor-match-evidence/);
  assert.match(radarCardSource, /<footer class="competitor-card-query-actions">[\s\S]*竞品查询/);
  assert.equal(pageSource.match(/<footer class="competitor-card-query-actions">/g)?.length, 3);
  assert.match(stylesSource, /\.competitor-match-result-list \{[\s\S]*grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(stylesSource, /\.competitor-product-detail-backdrop \{[\s\S]*z-index: 100/);
  assert.match(stylesSource, /\.competitor-match-backdrop \{[\s\S]*z-index: 94/);
});
