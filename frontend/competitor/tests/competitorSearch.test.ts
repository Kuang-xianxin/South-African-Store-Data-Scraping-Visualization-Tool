import assert from "node:assert/strict";
import test from "node:test";

import {
  competitorSearchTerm,
  matchesCompetitorSearch,
  matchesCompetitorSearchValues,
} from "../src/competitorSearch.ts";
import type { CompetitorItem } from "../src/types.ts";

function ownStoreItem(plid: string, title: string): CompetitorItem {
  return {
    来源: "own_store",
    plid,
    商品: title,
    当前卖家: "自有店铺（Seller API）",
    库存上限: "12",
    趋势判断: "等待首次检查",
    价格信号: "Seller API刷新",
    自有报价: [],
    跟卖报价: [],
  } as CompetitorItem;
}

test("extracts a PLID from a full Takealot link", () => {
  assert.equal(
    competitorSearchTerm("https://www.takealot.com/example/PLID12345678"),
    "12345678",
  );
});

test("filters own-store rows with missing competitor-only fields without throwing", () => {
  const rows = [
    ownStoreItem("11111111", "First product"),
    ownStoreItem("22222222", "Second product"),
  ];

  assert.deepEqual(
    rows
      .filter((item) =>
        matchesCompetitorSearch(
          item,
          "https://www.takealot.com/second-product/PLID22222222",
        ),
      )
      .map((item) => item.plid),
    ["22222222"],
  );
});

test("normalizes null, undefined, and numeric values before matching", () => {
  assert.equal(
    matchesCompetitorSearchValues([undefined, null, 12345, "Seller Name"], "12345"),
    true,
  );
  assert.equal(
    matchesCompetitorSearchValues([undefined, null, 12345, "Seller Name"], "seller"),
    true,
  );
  assert.equal(
    matchesCompetitorSearchValues([undefined, null, 12345], "missing"),
    false,
  );
});
