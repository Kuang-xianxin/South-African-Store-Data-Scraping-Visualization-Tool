import assert from "node:assert/strict";
import test from "node:test";
import { rankCompetitorMatches, type CompetitorMatchCandidate } from "../src/competitorSimilarity.ts";

function item(plid: string, title: string, leaf = ""): CompetitorMatchCandidate {
  return { plid, 商品: title, 来源: "competitor", 类目路径: leaf
    ? [{ name: leaf, id: leaf, type: "category", slug: null }] : [] };
}
function reciprocal(products: CompetitorMatchCandidate[], negatives: CompetitorMatchCandidate[] = []) {
  const catalog = [...products, ...negatives];
  for (const source of products) {
    const result = rankCompetitorMatches(source, catalog);
    assert.deepEqual(new Set(result.map((m) => m.item.plid)), new Set(products.filter((x) => x !== source).map((x) => x.plid)), source.商品);
    assert.deepEqual(rankCompetitorMatches(source, catalog), result, "warm index must preserve all results");
  }
  for (const source of negatives) assert.deepEqual(rankCompetitorMatches(source, products), [], source.商品);
}

test("persisted commercial ice makers match in both directions across different leaves and exclude juice makers", () => {
  reciprocal([
    item("98875564", "Goldair 30Kg/24Hrs Matt Black Plumbed-In Ice Maker Square Cubes", "Commercial Ice Makers"),
    item("100204903", "Mix Box 60Kg/24Hrs Ice Maker Machine for Bussiness", "Large Capacity Ice Makers"),
    item("compound", "Compact ICEMAKER 12kg", "Small Appliances"),
    item("hyphen", "Portable Ice-Makers for Home", "Cooling Equipment"),
  ], [item("95276787", "Juice Maker 2 Speed - 500W Fruit & Vegetable Juicer", "Juicers")]);
});

// These are constructed regression cases, not claims about collected inventory.
for (const [name, first, second, leaf, otherLeaf] of [
  ["air fryers", "BrandOne Digital Air Fryer 8L Black", "BrandTwo Compact Airfryer for Home", "Air Fryers", "Countertop Appliances"],
  ["vacuum cleaners", "BrandOne 2000W Vacuum Cleaner", "BrandTwo Portable VacuumCleaner 1200W", "Vacuum Cleaners", "Floor Care"],
  ["coffee grinders", "BrandOne Electric Coffee Grinder 200W", "BrandTwo Stainless Steel Coffee-Grinders", "Coffee Grinders", "Kitchen Utensils"],
  ["water purifiers", "BrandOne 5L Water Purifier", "BrandTwo WaterPurifiers for Home", "Water Purifiers", "Drinking Equipment"],
  ["camping chairs", "BrandOne Folding Camping Chair Red", "BrandTwo CampingChairs with Carry Bag", "Camping Chairs", "Garden Furniture"],
  ["soldering stations", "BrandOne Digital Soldering Station 90W", "BrandTwo SolderingStations for Professional Use", "Soldering Stations", "Electrical Tools"],
  ["label printers", "BrandOne Wireless Label Printer", "BrandTwo LabelPrinters for Business", "Label Printers", "Office Machines"],
] as const) {
  test(`general product-name evidence handles ${name} without a family-specific rule`, () => {
    reciprocal([item("a", first, leaf), item("b", second, otherLeaf)]);
    reciprocal([item("a", first), item("b", second)]);
  });
}

test("different category IDs, catalog order and ownership do not change reciprocal membership", () => {
  const a = item("a", "BrandOne Ice Maker 30kg", "Commercial Ice Makers");
  const b = { ...item("b", "BrandTwo Icemaker 60kg", "Large Capacity Ice Makers"), 来源: "own_store" as const };
  for (const products of [[a, b], [b, a]]) reciprocal(products);
  b.类目路径 = [];
  reciprocal([a, b]);
});

test("compatibility targets, consumables and included accessories do not identify the product sold", () => {
  const source = item("source", "Countertop Ice Maker 12kg", "Ice Makers");
  reciprocal([source, item("bundled", "Portable Ice Maker with Cover", "Other Appliances")], [
    item("case", "Protective Case for Countertop Ice Maker 12kg", "Ice Makers"),
    item("cleaner", "Countertop Ice Maker Cleaner 12kg", "Ice Makers"),
    item("filter", "Water Filter Compatible with Icemaker", "Ice Makers"),
    item("tray", "Ice Maker Tray", "Ice Makers"),
    item("spare", "Ice-Maker Replacement Pump", "Ice Makers"),
  ]);
});

test("shared material, weak head words and broad department alone are insufficient", () => {
  const pairs = [
    ["Stainless Steel Ice Maker", "Stainless Steel Juice Maker"],
    ["Electric Coffee Grinder", "Electric Meat Grinder"],
    ["Portable Ice Maker", "Portable Ice Cream Maker"],
    ["Cat Storage Box With Scratching Pad", "Hot Stone Set with Electrical Heating Storage Box"],
    ["Cat Tree Tower Play House", "Children Garden Play House"],
  ];
  for (const [left, right] of pairs) {
    const a = item("a", left!, "Home & Kitchen"), b = item("b", right!, "Home & Kitchen");
    assert.deepEqual(rankCompetitorMatches(a, [b]), [], `${left} / ${right}`);
    assert.deepEqual(rankCompetitorMatches(b, [a]), [], `${right} / ${left}`);
  }
});

test("an incorrect leaf that is absent from the title supplies no product evidence", () => {
  const a = item("a", "Commercial Ice Maker 30kg", "Commercial Juicers");
  const b = item("b", "Manual Citrus Juicer", "Commercial Juicers");
  assert.deepEqual(rankCompetitorMatches(a, [b]), []);
  assert.deepEqual(rankCompetitorMatches(b, [a]), []);
});
