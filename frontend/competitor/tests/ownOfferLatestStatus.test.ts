import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  OWN_OFFER_LATEST_STATUS_OPTIONS,
  matchesOwnOfferLatestFilters,
  ownOfferLatestStatusLabel,
} from "../src/ownOfferLatestStatus.ts";

const pageSource = readFileSync(
  new URL("../src/pages/CompetitorsPage.vue", import.meta.url),
  "utf8",
);

test("offers the four Seller Offers current statuses in the requested order", () => {
  assert.deepEqual(
    OWN_OFFER_LATEST_STATUS_OPTIONS.map((option) => option.value),
    [
      "not_buyable",
      "buyable",
      "disabled_by_takealot",
      "disabled_by_seller",
    ],
  );
  assert.equal(
    ownOfferLatestStatusLabel("disabled_by_takealot"),
    "平台已停用（Disabled by Takealot）",
  );
});

test("requires latest status and stock to match the same exact own Offer", () => {
  const splitAcrossStores = {
    对比报价: [
      {
        报价来源: "seller_api" as const,
        最新Offer状态: "disabled_by_seller",
        最新Offer库存状态: "没货" as const,
        库存状态: "有货",
      },
      {
        报价来源: "seller_api" as const,
        最新Offer状态: "buyable",
        最新Offer库存状态: "有货" as const,
        库存状态: "没货",
      },
      {
        报价来源: "public_offer" as const,
        最新Offer状态: "disabled_by_seller",
        最新Offer库存状态: "有货" as const,
      },
    ],
  };

  assert.equal(
    matchesOwnOfferLatestFilters(splitAcrossStores, "disabled_by_seller", "有货"),
    false,
  );
  assert.equal(
    matchesOwnOfferLatestFilters(splitAcrossStores, "disabled_by_seller", "没货"),
    true,
  );
  assert.equal(
    matchesOwnOfferLatestFilters(splitAcrossStores, "buyable", "有货"),
    true,
  );

  const sameOfferMatch = {
    对比报价: [
      {
        报价来源: "seller_api" as const,
        最新Offer状态: "disabled_by_seller",
        最新Offer库存状态: "有货" as const,
        库存状态: "没货",
      },
    ],
  };
  assert.equal(
    matchesOwnOfferLatestFilters(sameOfferMatch, "disabled_by_seller", "有货"),
    true,
  );
  assert.equal(matchesOwnOfferLatestFilters({}, "全部", "全部"), true);
});

test("wires the two own-store selectors into one exact-Offer predicate", () => {
  assert.match(pageSource, /v-model="ownOfferLatestStatusFilter"/);
  assert.match(
    pageSource,
    /matchesOwnOfferLatestFilters\(\s*item,\s*ownOfferLatestStatusFilter\.value,\s*competitorStockFilter\.value,\s*\)/,
  );
  assert.match(
    pageSource,
    /item\.来源 === "competitor"\s*&& competitorStockFilter\.value !== "全部"/,
  );
  assert.doesNotMatch(pageSource, /不随观察区间变化/);
});

test("shows the exact selected store Offer current status in the sticky detail header", () => {
  assert.match(
    pageSource,
    /selected\.来源 === 'own_store' && selectedOffer\?\.报价来源 === 'seller_api'/,
  );
  assert.match(pageSource, /selectedOffer\.最新Offer状态/);
  assert.match(
    pageSource,
    /ownOfferLatestStatusLabel\(selectedOffer\.最新Offer状态\)/,
  );
  assert.match(pageSource, /selectedOffer\.最新Offer状态更新时间/);
});
