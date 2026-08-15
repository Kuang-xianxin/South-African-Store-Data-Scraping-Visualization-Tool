import assert from "node:assert/strict";
import test from "node:test";

import { groupSearchRankingProducts } from "../src/searchRankingFamilies.ts";
import type { SearchRankingProduct } from "../src/types.ts";

function product(
  offerId: string,
  productlineId: string | null,
  options: { analysedAt?: string; stock?: number; title?: string } = {},
): SearchRankingProduct {
  return {
    offer_id: offerId,
    productline_id: productlineId,
    sku: `SKU-${offerId}`,
    title: options.title ?? `Product ${offerId}`,
    image_url: null,
    offer_status: "buyable",
    available_stock: options.stock ?? 1,
    takealot_available_stock: options.stock ?? 1,
    seller_available_stock: 0,
    captured_at: "2026-08-12T00:00:00",
    snapshot_age_hours: 1,
    ownership_source: "authenticated_store_seller_offers",
    analyzable: true,
    latest_analysis: options.analysedAt
      ? {
          id: Number(offerId.replace(/\D/g, "")) || 1,
          status: "completed",
          source_title: options.title ?? `Product ${offerId}`,
          provider: "doubao",
          model: "test",
          confidence: 0.9,
          vision_reused: false,
          created_at: options.analysedAt,
          completed_at: options.analysedAt,
          error: null,
          vision_stage_completed: true,
          usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 },
          estimated_cost_cny: 0.01,
          title_validation_status: null,
          title_score_value: 80,
          title_score_band: "solid",
          title_score_evidence_coverage: 100,
          title_score_current_title_match: true,
          identity_difference_level: "aligned",
          identity_large_difference: false,
          manual_fact_required: false,
          manual_fact_reason: null,
        }
      : null,
  };
}

test("groups variants with the same PLID into one operator card", () => {
  const families = groupSearchRankingProducts([
    product("offer-2", "PLID-1", { stock: 3 }),
    product("offer-1", "PLID-1", { stock: 5 }),
    product("offer-3", "PLID-2", { stock: 2 }),
  ]);

  assert.equal(families.length, 2);
  const first = families.find((item) => item.productline_id === "PLID-1");
  assert.ok(first);
  assert.equal(first.variant_count, 2);
  assert.equal(first.total_available_stock, 8);
  assert.deepEqual(first.variants.map((item) => item.offer_id), ["offer-1", "offer-2"]);
});

test("uses the most recently analysed variant as the stable family representative", () => {
  const [family] = groupSearchRankingProducts([
    product("offer-1", "PLID-1", { analysedAt: "2026-08-11T10:00:00" }),
    product("offer-2", "PLID-1", { analysedAt: "2026-08-12T10:00:00" }),
  ]);

  assert.equal(family.representative.offer_id, "offer-2");
  assert.equal(family.latest_analysis?.created_at, "2026-08-12T10:00:00");
});

test("keeps offers without a PLID as separate families", () => {
  const families = groupSearchRankingProducts([
    product("offer-1", null),
    product("offer-2", null),
  ]);

  assert.equal(families.length, 2);
});

test("keeps Double King and King XL as per-offer parameters on one shared family", () => {
  const [family] = groupSearchRankingProducts([
    product("double", "102695333", { title: "2 Inch 7 Zone Memory Foam Double" }),
    product("king", "102695333", { title: "2 Inch 7 Zone Memory Foam King" }),
    product("king-xl", "102695333", { title: "2 Inch 7 Zone Memory Foam King XL" }),
  ]);

  assert.equal(family.shared_title, "2 Inch 7 Zone Memory Foam");
  assert.deepEqual(family.variant_parameter_values, ["Double", "King", "King XL"]);
  assert.deepEqual(
    Object.fromEntries(Object.entries(family.variant_parameters_by_offer).map(
      ([offerId, parameters]) => [offerId, parameters.map((item) => item.value)],
    )),
    {
      double: ["Double"],
      king: ["King"],
      "king-xl": ["King XL"],
    },
  );
  assert.equal(
    family.variant_parameters_by_offer["king-xl"][0].visually_verified,
    false,
  );
});
