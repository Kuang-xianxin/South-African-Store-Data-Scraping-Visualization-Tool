import assert from "node:assert/strict";
import test from "node:test";

import {
  matchesFollowSellingOpportunity,
  summarizeFollowSellingOpportunities,
} from "../src/competitorFollowOpportunities.ts";
import type { CompetitorItem, FollowSellingOpportunityType } from "../src/types.ts";

function item(
  plid: string,
  type: FollowSellingOpportunityType | null,
  source: CompetitorItem["来源"] = "competitor",
): CompetitorItem {
  return {
    plid,
    来源: source,
    跟卖机会: type !== null,
    跟卖机会类型: type,
    跟卖机会说明: "test",
    公开报价数: type === "暂无卖家报价" ? 0 : 1,
  } as CompetitorItem;
}

test("summarizes only true-competitor follow-selling opportunities", () => {
  const items = [
    item("1", "全部报价售罄"),
    item("2", "暂无卖家报价"),
    item("3", null),
    item("4", "暂无卖家报价", "own_store"),
  ];

  assert.deepEqual(summarizeFollowSellingOpportunities(items), {
    total: 2,
    soldOut: 1,
    noSeller: 1,
  });
});

test("matches the combined and individual opportunity selectors", () => {
  const soldOut = item("1", "全部报价售罄");
  const noSeller = item("2", "暂无卖家报价");
  const active = item("3", null);

  assert.equal(matchesFollowSellingOpportunity(soldOut, "可跟卖机会"), true);
  assert.equal(matchesFollowSellingOpportunity(noSeller, "全部报价售罄"), false);
  assert.equal(matchesFollowSellingOpportunity(noSeller, "暂无卖家报价"), true);
  assert.equal(matchesFollowSellingOpportunity(active, "可跟卖机会"), false);
  assert.equal(
    matchesFollowSellingOpportunity(item("4", null, "own_store"), "可跟卖机会"),
    true,
  );
});
