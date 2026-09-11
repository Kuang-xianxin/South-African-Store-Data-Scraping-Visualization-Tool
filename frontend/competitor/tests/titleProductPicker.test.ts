import assert from "node:assert/strict";
import test from "node:test";
import { groupSearchRankingProducts } from "../src/searchRankingFamilies.ts";
import {
  defaultPickerFilters, filterPickerFamilies, pickerQueries, pickerScore,
  pickerStatus, pickerTarget, pickerViewMatches, pickerFilterError,
} from "../src/titleProductPicker.ts";
import type { SearchRankingAnalysisSummary, SearchRankingProduct } from "../src/types.ts";

function product(id: string, overrides: Partial<SearchRankingProduct> = {}): SearchRankingProduct {
  return {
    offer_id: id, productline_id: id, title: `Product ${id}`, sku: `SKU-${id}`,
    image_url: null, available_stock: 1, takealot_available_stock: 1, seller_available_stock: 0,
    offer_status: "buyable", captured_at: "2026-09-10T00:00:00", snapshot_age_hours: 1,
    ownership_source: "authenticated_store_seller_offers", analyzable: true, latest_analysis: null,
    store_code: "store-a", store_name: "Store A", ...overrides,
  };
}
function analysis(overrides: Partial<SearchRankingAnalysisSummary> = {}): SearchRankingAnalysisSummary {
  return {
    id: 1, source_offer_id: "1", source_title: "Product 1", status: "completed", provider: "test",
    model: "test", confidence: 1, vision_reused: false, created_at: "2026-09-10T01:00:00",
    completed_at: "2026-09-10T01:01:00", error: null, vision_stage_completed: true,
    usage: {}, estimated_cost_cny: null, title_validation_status: null,
    title_score_current_title_match: true, title_score_value: 80, title_score_band: "solid", ...overrides,
  };
}

test("matches full Takealot URLs and prefixed PLIDs, without turning other hosts into PLID matches", () => {
  const families = groupSearchRankingProducts([product("12345")]);
  for (const search of ["PLID12345", "plid 12345", "https://www.takealot.com/original-title/PLID12345?ref=search"]) {
    assert.equal(filterPickerFamilies(families, { ...defaultPickerFilters(), search }).length, 1);
  }
  assert.equal(filterPickerFamilies(families, { ...defaultPickerFilters(), search: "https://takealot.com.evil.test/name/PLID12345" }).length, 0);
});

test("batch lookup uses OR, deduplicates inputs and does not duplicate matching families", () => {
  assert.deepEqual(pickerQueries(" SKU-1\nSKU-2, SKU-1；PLID12345 "), ["SKU-1", "SKU-2", "12345"]);
  const families = groupSearchRankingProducts([product("1"), product("2"), product("3")]);
  assert.deepEqual(filterPickerFamilies(families, { ...defaultPickerFilters(), search: "SKU-1\nSKU-2" }).map((item) => item.key), [families[0].key, families[1].key]);
});

test("SKU search selects the matching nonrepresentative variant and preserves store identity", () => {
  const families = groupSearchRankingProducts([
    product("1", { productline_id: "100", company_sku: "RED", family_representative_offer_id: "1" }),
    product("2", { productline_id: "100", company_sku: "BLUE" }),
    product("2", { productline_id: "100", company_sku: "BLUE", store_code: "store-b" }),
  ]);
  const filtered = filterPickerFamilies(families, { ...defaultPickerFilters(), store: "store-a", search: "BLUE" });
  assert.equal(filtered.length, 1);
  assert.equal(filtered[0].variant_count, 2);
  assert.equal(pickerTarget(filtered[0], ["BLUE"]).offer_id, "2");
  assert.equal(pickerTarget(filtered[0], ["BLUE"]).store_code, "store-a");
  assert.equal(pickerTarget(filtered[0], []).offer_id, "1");
  assert.equal(pickerTarget(filtered[0], ["100"]).offer_id, "1", "PLID search retains the representative");
});

test("search combines company name and store tokens within one authorized variant", () => {
  const families = groupSearchRankingProducts([product("1", { company_product_name: "电动搅拌机", store_name: "VoltTech" }), product("2")]);
  assert.equal(filterPickerFamilies(families, { ...defaultPickerFilters(), search: "VoltTech 搅拌机" }).length, 1);
});

test("current score filters exclude stale, unknown, failed and insufficient evidence scores", () => {
  const families = groupSearchRankingProducts([
    product("1", { latest_analysis: analysis({ title_score_value: 50 }) }),
    product("2", { latest_analysis: analysis({ title_score_value: 20, title_score_current_title_match: false }) }),
    product("3", { latest_analysis: analysis({ title_score_value: 10, title_score_current_title_match: undefined }) }),
    product("4", { latest_analysis: analysis({ title_score_value: 5, title_score_band: "insufficient_evidence" }) }),
    product("5", { latest_analysis: analysis({ title_score_value: 0, status: "failed" }) }),
  ]);
  assert.deepEqual(filterPickerFamilies(families, { ...defaultPickerFilters(), view: "low_score" }).map((item) => item.representative.offer_id), ["1"]);
  assert.equal(pickerStatus(families[1]), "标题已变化");
  assert.equal(filterPickerFamilies(families, { ...defaultPickerFilters(), score: "unscored" }).length, 4);
});

test("score sorting retains known zero and keeps missing values last in both directions", () => {
  const families = groupSearchRankingProducts([
    product("1"), product("2", { latest_analysis: analysis({ title_score_value: 0 }) }),
    product("3", { latest_analysis: analysis({ title_score_value: 85 }) }),
  ]);
  assert.equal(pickerScore(families[1]), 0);
  for (const sort of ["score_asc", "score_desc"] as const) {
    const sorted = filterPickerFamilies(families, { ...defaultPickerFilters(), sort });
    assert.equal(sorted[2].representative.offer_id, "1");
    assert.equal(sorted[0].representative.offer_id, sort === "score_asc" ? "2" : "3");
  }
});

test("score boundaries 55, 70, 85 and 100 belong to the correct bands", () => {
  const families = groupSearchRankingProducts([54, 55, 69, 70, 84, 85, 100].map((score) => product(String(score), { latest_analysis: analysis({ title_score_value: score }) })));
  for (const [score, count] of [["below_55", 1], ["55_69", 2], ["70_84", 2], ["85_plus", 2]] as const) {
    assert.equal(filterPickerFamilies(families, { ...defaultPickerFilters(), score }).length, count);
  }
});

test("stock sorting uses the complete family and filters the complete set before pagination", () => {
  const families = groupSearchRankingProducts([
    product("1", { available_stock: 2 }), product("2", { available_stock: 10, productline_id: "99" }),
    product("3", { available_stock: 12, productline_id: "99" }), product("4", { available_stock: 20 }),
  ]);
  const result = filterPickerFamilies(families, { ...defaultPickerFilters(), sort: "stock_desc" });
  assert.equal(result.slice(0, 1)[0].productline_id, "99");
  assert.equal(result[0].total_available_stock, 22);
  assert.equal(families[0].representative.offer_id, "1", "does not mutate input order");
});

test("manual review includes explicit high differences without inventing failed or unanalysed status", () => {
  const [family] = groupSearchRankingProducts([product("1", { latest_analysis: analysis({ identity_difference_level: "high" }) })]);
  assert.equal(pickerViewMatches(family, "manual"), true);
  assert.equal(pickerViewMatches(family, "unanalysed"), false);
  assert.equal(pickerStatus(family), "图题差异大");
});

test("combined filters include missing SKU on any variant and keep same PLID stores separate", () => {
  const families = groupSearchRankingProducts([
    product("1", { productline_id: "100", company_sku: "RED", latest_analysis: analysis({ identity_difference_level: "high" }) }),
    product("2", { productline_id: "100", company_sku: "" }),
    product("3", { productline_id: "100", company_sku: "BLUE", store_code: "store-b" }),
  ]);
  assert.equal(filterPickerFamilies(families, { ...defaultPickerFilters(), store: "store-a", sku: "missing", variants: "multiple", identity: "high" }).length, 1);
  assert.equal(filterPickerFamilies(families, { ...defaultPickerFilters(), store: "store-b", variants: "multiple" }).length, 0);
});

test("analysis dates normalize UTC offsets and missing dates stay last", () => {
  const families = groupSearchRankingProducts([
    product("1", { latest_analysis: analysis({ created_at: "2026-09-10T02:00:00" }) }),
    product("2", { latest_analysis: analysis({ created_at: "2026-09-10T09:00:00+08:00" }) }),
    product("3"),
  ]);
  assert.deepEqual(filterPickerFamilies(families, { ...defaultPickerFilters(), sort: "newest" }).map((item) => item.representative.offer_id), ["1", "2", "3"]);
  assert.deepEqual(filterPickerFamilies(families, { ...defaultPickerFilters(), sort: "oldest" }).map((item) => item.representative.offer_id), ["2", "1", "3"]);
});

test("field-specific exact search selects only the matching variant and keeps PLID normalization", () => {
  const families = groupSearchRankingProducts([
    product("1", { productline_id: "100", title: "BLUE sofa", company_sku: "RED", family_representative_offer_id: "1" }),
    product("2", { productline_id: "100", company_sku: "BLUE" }),
    product("3", { title: "BLUE", company_sku: "BLUE-XL" }),
  ]);
  const filters = { ...defaultPickerFilters(), search: "blue", searchField: "company_sku" as const, searchMatch: "exact" as const };
  const result = filterPickerFamilies(families, filters);
  assert.equal(result.length, 1);
  assert.equal(pickerTarget(result[0], pickerQueries(filters.search), filters).offer_id, "2");
  assert.equal(filterPickerFamilies(families, { ...filters, searchField: "title" }).length, 1);
  assert.equal(filterPickerFamilies(families, { ...filters, search: "PLID100", searchField: "productline_id" }).length, 1);
  assert.equal(filterPickerFamilies(families, { ...filters, search: "SKU-2", searchField: "sku" }).length, 1);
});

test("exclusions apply to any variant in a family without changing another store's family", () => {
  const families = groupSearchRankingProducts([
    product("1", { productline_id: "100", title: "Chair white" }),
    product("2", { productline_id: "100", title: "Chair pink" }),
    product("1", { productline_id: "100", title: "Chair white", store_code: "store-b" }),
  ]);
  const result = filterPickerFamilies(families, { ...defaultPickerFilters(), search: "chair", exclude: "pink,cover" });
  assert.equal(result.length, 1);
  assert.equal(result[0].representative.store_code, "store-b");
});

test("stock ranges are inclusive and operate on complete family stock, including zero", () => {
  const families = groupSearchRankingProducts([
    product("1", { available_stock: 2, productline_id: "100" }),
    product("2", { available_stock: 3, productline_id: "100" }),
    product("3", { available_stock: 6 }), product("4", { available_stock: 0 }),
  ]);
  assert.equal(filterPickerFamilies(families, { ...defaultPickerFilters(), stockMin: "5", stockMax: "5" })[0].productline_id, "100");
  assert.equal(filterPickerFamilies(families, { ...defaultPickerFilters(), stockMax: "0" })[0].representative.offer_id, "4");
  assert.equal(filterPickerFamilies(families, { ...defaultPickerFilters(), stockMin: "6" }).length, 1);
});

test("custom score ranges exclude stale scores and retain known zero", () => {
  const families = groupSearchRankingProducts([
    product("1", { latest_analysis: analysis({ title_score_value: 0 }) }),
    product("2", { latest_analysis: analysis({ title_score_value: 40, title_score_current_title_match: false }) }),
    product("3"), product("4", { latest_analysis: analysis({ title_score_value: 50 }) }),
  ]);
  assert.deepEqual(filterPickerFamilies(families, { ...defaultPickerFilters(), scoreMin: "0", scoreMax: "50" }).map(f => f.representative.offer_id), ["1", "4"]);
  assert.equal(filterPickerFamilies(families, { ...defaultPickerFilters(), score: "85_plus", scoreMax: "50" }).length, 0);
});

test("invalid numeric ranges report a specific error instead of silently ignoring the filter", () => {
  const families = groupSearchRankingProducts([product("1")]);
  for (const values of [{ stockMin: "5", stockMax: "1" }, { stockMin: "-1" }, { stockMax: "abc" }, { scoreMax: "101" }, { stockMin: "Infinity" }]) {
    const filters = { ...defaultPickerFilters(), ...values };
    assert.ok(pickerFilterError(filters));
    assert.equal(filterPickerFamilies(families, filters).length, 0);
  }
  assert.equal(pickerFilterError({ ...defaultPickerFilters(), stockMin: "  ", scoreMin: "0", scoreMax: "100" }), "");
});

test("image and company SKU completeness require every variant to have a value", () => {
  const families = groupSearchRankingProducts([
    product("1", { productline_id: "100", company_sku: "A", image_url: "https://example.test/1.png" }),
    product("2", { productline_id: "100", company_sku: " ", image_url: null }),
    product("3", { company_sku: "B", image_url: "https://example.test/3.png" }),
  ]);
  assert.equal(filterPickerFamilies(families, { ...defaultPickerFilters(), sku: "linked", image: "complete" })[0].representative.offer_id, "3");
  assert.equal(filterPickerFamilies(families, { ...defaultPickerFilters(), sku: "missing", image: "missing" })[0].variant_count, 2);
});

test("new sort metrics use full-family variant counts and keep absent snapshot dates last", () => {
  const families = groupSearchRankingProducts([
    product("1", { title: "A", productline_id: "100", captured_at: "2026-09-11T01:00:00Z" }),
    product("2", { title: "A", productline_id: "100" }),
    product("3", { title: "B", captured_at: "2026-09-11T02:00:00Z" }),
    product("4", { title: "C", captured_at: "" }),
  ]);
  assert.equal(filterPickerFamilies(families, { ...defaultPickerFilters(), sort: "variants_desc" })[0].productline_id, "100");
  assert.equal(filterPickerFamilies(families, { ...defaultPickerFilters(), sort: "title_desc" })[0].shared_title, "C");
  for (const sort of ["captured_asc", "captured_desc"] as const) {
    const result = filterPickerFamilies(families, { ...defaultPickerFilters(), sort });
    assert.equal(result[2].representative.offer_id, "4");
    assert.equal(result[0].representative.offer_id, sort === "captured_asc" ? "1" : "3");
  }
});
