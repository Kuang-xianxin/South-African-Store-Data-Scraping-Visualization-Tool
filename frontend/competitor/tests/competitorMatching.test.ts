import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { findCompetitorMatches, matchCompetitor } from "../src/competitorMatching.ts";
import type { CompetitorItem, CompetitorCategoryBreadcrumb } from "../src/types.ts";

const cat = (name: string, id = name): CompetitorCategoryBreadcrumb => ({ name, id, slug: null, type: "productline" });
const path = [cat("Electronics"), cat("Projection"), cat("Projector Screens")];
function item(plid: string, title: string, categories = path): CompetitorItem {
  return { plid, 商品: title, 来源: "competitor", 类目路径: categories,
    最新评论数: null, 周期销售件数: null } as CompetitorItem;
}
const source = item("1", "Alpha Portable Projector Screen 100inch");
test("same product phrase and size qualify as near identical, independent of brand", () => {
  const match = matchCompetitor(source, item("2", "Beta Portable Projection Screen 100 inch"))!;
  assert.equal(match.kind, "near_identical");
  assert.ok(match.reasons.some((r) => r.includes("数字规格")));
  assert.equal(match.score, matchCompetitor({ ...source, 商品: source.商品.replace("Alpha", "Gamma") }, match.item)?.score);
});
test("different size or model remains a substitute rather than near identical", () => {
  assert.equal(matchCompetitor(source, item("2", "Portable Projector Screen 120inch"))?.kind, "same_demand");
  assert.equal(matchCompetitor(item("1", "Projector Screen Model AB100"), item("2", "Projector Screen Model AB200"))?.kind, "same_demand");
});
test("different physical form in exact category is an explicit same-demand candidate", () => {
  const p = [cat("Home"), cat("Seating")];
  assert.equal(matchCompetitor(item("1", "Foldable Floor Chair", p), item("2", "Bean Bag", p))?.kind, "same_demand");
});
test("unknown titles require exact persisted category and do not invent one", () => {
  assert.equal(matchCompetitor(item("1", "Unique furnishing"), item("2", "Alternate furnishing"))?.kind, "same_demand");
  assert.equal(matchCompetitor(item("1", "Unique furnishing", []), item("2", "Unique furnishing", [])), null);
});
test("neighbours require product evidence, and broad shared roots never qualify", () => {
  const neighbour = [cat("Electronics"), cat("Projection"), cat("Tripod Screens")];
  assert.ok(matchCompetitor(source, item("2", "Portable Projector Screen 100inch", neighbour)));
  assert.equal(matchCompetitor(source, item("2", "Alpha Portable Table 100inch", neighbour)), null);
  assert.equal(matchCompetitor(source, item("2", "Portable Projector Screen 100inch", [cat("Electronics"), cat("Other Products")])), null);
});
test("adjacent known alternative families require a shared persisted parent", () => {
  const p = [cat("Home"), cat("Living Room"), cat("Sofas")];
  const q = [cat("Home"), cat("Living Room"), cat("Bean Bags")];
  assert.equal(matchCompetitor(item("1", "Lazy Sofa", p), item("2", "Bean Bag Chair", q))?.kind, "same_demand");
});
test("main products reject covers, replacement parts, refill and carrying bags", () => {
  for (const title of ["Projector Screen Cover", "Replacement Projector Screen", "Projector Screen Carrying Bag", "Projector Screen Refill"]) {
    assert.equal(matchCompetitor(source, item("2", title)), null, title);
  }
});
test("bundled case does not change a main product into an accessory", () => {
  assert.equal(matchCompetitor(source, item("2", "Portable Projector Screen 100inch with Carrying Bag"))?.kind, "near_identical");
});
test("known conflicting identities override an incorrectly shared leaf", () => {
  assert.equal(matchCompetitor(source, item("2", "Portable Projector 100inch")), null);
  assert.equal(matchCompetitor(item("1", "Litter Box"), item("2", "Pet Carrier")), null);
});
test("accessory compatibility conflicts are rejected", () => {
  const a = item("1", "Phone Case for iPhone 15");
  assert.equal(matchCompetitor(a, item("2", "Phone Case for iPhone 16")), null);
});
test("missing category needs a product identity AND numeric/model corroboration", () => {
  assert.ok(matchCompetitor({ ...source, 类目路径: [] }, item("2", "Portable Projector Screen 100inch", [])));
  assert.equal(matchCompetitor(item("1", "Portable Projector Screen", []), item("2", "Portable Projector Screen", [])), null);
});
test("normalizes unit equivalents", () => {
  assert.equal(matchCompetitor(item("1", "Projector Screen 100cm"), item("2", "Projector Screen 1000mm"))?.kind, "near_identical");
});
test("seller, stock, sales and reviews cannot qualify or change semantic score", () => {
  const candidate = item("2", "Portable Projector Screen 100inch");
  const altered = { ...candidate, 当前卖家: "Alpha", 库存数量: 99999, 周期销售件数: 99999, 最新评论数: 99999 };
  assert.equal(matchCompetitor(source, candidate)?.score, matchCompetitor(source, altered)?.score);
  assert.equal(matchCompetitor(source, { ...altered, 商品: "Portable Heater" }), null);
});
test("deduplicates PLID, excludes self, and preserves authorized own row", () => {
  const candidate = item("2", "Portable Projector Screen 100inch");
  const own = { ...candidate, 来源: "own_store" as const };
  const result = findCompetitorMatches(source, [source, { ...source, plid: "PLID1" }, candidate, own]);
  assert.equal(result.length, 1);
  assert.equal(result[0]!.item, own);
});
test("commercial evidence only breaks ties after match type and score", () => {
  const best = item("2", "Portable Projector Screen 100inch");
  const substitute = { ...item("3", "Projector Screen 120inch"), 最新评论数: 99999 };
  const tied = { ...best, plid: "4", 最新评论数: 100 };
  assert.deepEqual(findCompetitorMatches(source, [best, substitute, tied]).map((m) => m.item.plid), ["4", "2", "3"]);
});
test("wired and wireless configurations cannot be nearly identical", () => {
  assert.equal(matchCompetitor(item("1", "Wired Bluetooth Headphones"), item("2", "Wireless Bluetooth Headphones"))?.kind, "same_demand");
});
test("pure matching does not mutate source or candidate data", () => {
  const candidates = [item("2", "Portable Projector Screen 100inch")];
  const before = JSON.stringify([source, candidates]);
  findCompetitorMatches(source, candidates);
  assert.equal(JSON.stringify([source, candidates]), before);
});
test("separated model numbers and different toothbrush shapes prevent near-identical claims", () => {
  const toothbrush = item("1", "Janqi Z1 Electric Toothbrush");
  assert.equal(matchCompetitor(toothbrush, item("2", "Beurer Electric Toothbrush TB 15"))?.kind, "same_demand");
  assert.equal(matchCompetitor(toothbrush, item("2", "U-Shape Electric Toothbrush"))?.kind, "same_demand");
  assert.equal(matchCompetitor(toothbrush, item("2", "Generic Electric Toothbrush"))?.kind, "same_demand");
});
test("specifications after with remain evidence for the product", () => {
  assert.equal(matchCompetitor(item("1", "Portable Power Station with 500Wh Battery"), item("2", "Portable Power Station with 1000Wh Battery"))?.kind, "same_demand");
});
test("same desk width cannot override different construction or a frame-only listing", () => {
  const desk = item("1", "Computer Desk 120cm");
  assert.equal(matchCompetitor(desk, item("2", "Electric Height Adjustable Sit-Stand Desk 120cm"))?.kind, "same_demand");
  assert.equal(matchCompetitor(desk, item("2", "Computer Desk Frame 120cm")), null);
});
test("inflatable and rigid products are different configurations even with shared adjectives", () => {
  assert.equal(matchCompetitor(item("1", "Portable Waterproof Inflatable Camping Tent"), item("2", "Portable Waterproof Camping Tent"))?.kind, "same_demand");
});
test("outer radar and query render the exact same complete card component", () => {
  const page = readFileSync(new URL("../src/pages/CompetitorsPage.vue", import.meta.url), "utf8");
  const card = readFileSync(new URL("../src/components/CompetitorRadarCard.vue", import.meta.url), "utf8");
  assert.equal(page.match(/<CompetitorRadarCard\b/g)?.length, 2);
  for (const field of ["报价区间 / 主报价", "主报价库存", "周期内销售额", "商品类目", "最新评论数", "首次监控", "我的监控池", "CompetitorObservedSalesMetrics"]) assert.ok(card.includes(field), field);
  assert.match(card, /@keydown\.enter\.self/);
  assert.match(page, /openQueryProductDetail[\s\S]*openProductModal\(item, "personal_watchlist"\)/);
  assert.match(page, /if \(competitorQuerySource.value\) \{ closeCompetitorQuery\(\); return; \}/);
});
