import assert from "node:assert/strict";
import test from "node:test";

import {
  buildPersonalWatchlistWorkspaceCards,
  filterPersonalWatchlistWorkspaceCards,
  personalWatchlistPageForPlid,
  personalWatchlistUnavailableReason,
  recountPersonalWatchlistLibraries,
  sortPersonalWatchlistWorkspaceCards,
} from "../src/personalWatchlistWorkspace.ts";
import type {
  CompetitorItem,
  CompetitorTargetItem,
} from "../src/types.ts";

test("personal workspace keeps every membership even before first capture", () => {
  const target = {
    plid: "22",
    title: null,
  } as CompetitorTargetItem;
  const competitor = {
    plid: "11",
    商品: "Captured product",
  } as CompetitorItem;
  const ownStoreProduct = {
    plid: "44",
    来源: "own_store",
    商品: "Own store product",
  } as CompetitorItem;
  const cards = buildPersonalWatchlistWorkspaceCards(
    [
      {
        plid: "11",
        added_at: "2026-08-10T01:00:00Z",
        source: "competitor",
        library_ids: [],
      },
      {
        plid: "22",
        added_at: "2026-08-10T02:00:00Z",
        source: "competitor",
        library_ids: [3],
      },
      {
        plid: "33",
        added_at: "2026-08-10T03:00:00Z",
        source: "competitor",
        library_ids: [],
      },
      {
        plid: "44",
        added_at: "2026-08-10T04:00:00Z",
        source: "own_store",
        library_ids: [3, 7],
      },
    ],
    [target],
    [competitor, ownStoreProduct],
  );

  assert.deepEqual(cards.map((item) => item.plid), ["11", "22", "33", "44"]);
  assert.equal(cards[0]?.competitor, competitor);
  assert.equal(cards[0]?.personalMember, true);
  assert.equal(cards[0]?.target, null);
  assert.equal(cards[1]?.target, target);
  assert.deepEqual(cards[1]?.libraryIds, [3]);
  assert.equal(cards[1]?.competitor, null);
  assert.equal(cards[2]?.target, null);
  assert.equal(cards[2]?.competitor, null);
  assert.equal(cards[3]?.source, "own_store");
  assert.deepEqual(cards[3]?.libraryIds, [3, 7]);
  assert.equal(cards[3]?.competitor, ownStoreProduct);
});

test("shared library cards stay separate from personal membership", () => {
  const sharedCompetitor = {
    plid: "55",
    来源: "competitor",
    商品: "Shared captured product",
  } as CompetitorItem;
  const cards = buildPersonalWatchlistWorkspaceCards(
    [
      {
        plid: "11",
        added_at: "2026-08-11T01:00:00Z",
        source: "competitor",
        library_ids: [],
      },
    ],
    [],
    [sharedCompetitor],
    [
      {
        plid: "55",
        added_at: "2026-08-11T02:00:00Z",
        library_ids: [8],
        source: "competitor",
        detail_access: "public",
      },
      {
        plid: "11",
        added_at: "2026-08-11T03:00:00Z",
        library_ids: [8],
        source: "competitor",
        detail_access: "public",
      },
    ],
  );

  assert.deepEqual(cards.map((item) => item.plid), ["11", "55"]);
  assert.equal(cards[0]?.personalMember, true);
  assert.equal(cards[1]?.personalMember, false);
  assert.equal(cards[1]?.source, "competitor");
  assert.deepEqual(cards[1]?.libraryIds, [8]);
  assert.equal(cards[1]?.competitor, sharedCompetitor);
});

test("an own-store card without account-authorized details is not called first capture", () => {
  const [ownStoreCard] = buildPersonalWatchlistWorkspaceCards(
    [
      {
        plid: "102831637",
        added_at: "2026-08-12T02:44:00Z",
        source: "own_store",
        library_ids: [],
      },
    ],
    [],
    [],
  );
  assert.ok(ownStoreCard);
  assert.equal(
    personalWatchlistUnavailableReason(ownStoreCard),
    "authorized_store_data_unavailable",
  );

  const [capturedOwnStoreCard] = buildPersonalWatchlistWorkspaceCards(
    [
      {
        plid: "102831637",
        added_at: "2026-08-12T02:44:00Z",
        source: "own_store",
        library_ids: [],
      },
    ],
    [],
    [{ plid: "102831637", 来源: "own_store" } as CompetitorItem],
  );
  assert.ok(capturedOwnStoreCard);
  assert.equal(personalWatchlistUnavailableReason(capturedOwnStoreCard), null);
});

test("a shared private card reports account store denial explicitly", () => {
  const [deniedCard, unknownCard] = buildPersonalWatchlistWorkspaceCards(
    [],
    [],
    [],
    [
      {
        plid: "102576284",
        added_at: "2026-08-12T06:33:23Z",
        library_ids: [5],
        source: "own_store",
        detail_access: "store_access_denied",
      },
      {
        plid: "102576285",
        added_at: "2026-08-12T06:34:23Z",
        library_ids: [5],
        source: "unknown",
        detail_access: "unknown",
      },
    ],
  );

  assert.ok(deniedCard);
  assert.equal(deniedCard.source, "own_store");
  assert.equal(personalWatchlistUnavailableReason(deniedCard), "store_access_denied");
  assert.ok(unknownCard);
  assert.equal(
    personalWatchlistUnavailableReason(unknownCard),
    "shared_details_unavailable",
  );
});

test("a located membership switches to the page containing its card", () => {
  const cards = Array.from({ length: 14 }, (_, index) => ({
    plid: String(index + 1),
    addedAt: "",
    source: "competitor" as const,
    personalMember: true,
    libraryIds: [],
    competitor: null,
    target: null,
  }));

  assert.equal(personalWatchlistPageForPlid(cards, "1", 6), 1);
  assert.equal(personalWatchlistPageForPlid(cards, "7", 6), 2);
  assert.equal(personalWatchlistPageForPlid(cards, "14", 6), 3);
  assert.equal(personalWatchlistPageForPlid(cards, "99", 6), null);
});

test("library counts immediately follow local membership deletion", () => {
  const libraries = [
    {
      id: 3,
      name: "红光库",
      created_at: "2026-08-10T01:00:00Z",
      updated_at: "2026-08-10T01:00:00Z",
      item_count: 1,
    },
    {
      id: 7,
      name: "空白库",
      created_at: "2026-08-10T02:00:00Z",
      updated_at: "2026-08-10T02:00:00Z",
      item_count: 0,
    },
  ];
  const membership = {
    plid: "12345678",
    added_at: "2026-08-10T03:00:00Z",
    source: "competitor" as const,
    library_ids: [3],
  };

  assert.deepEqual(
    recountPersonalWatchlistLibraries(libraries, [membership]).map(
      (library) => [library.name, library.item_count],
    ),
    [["红光库", 1], ["空白库", 0]],
  );
  assert.deepEqual(
    recountPersonalWatchlistLibraries(libraries, []).map(
      (library) => [library.name, library.item_count],
    ),
    [["红光库", 0], ["空白库", 0]],
  );
  assert.equal(libraries[0]?.item_count, 1);
});

test("personal workspace filters both true competitors and own-store followers", () => {
  const trueCompetitor = {
    来源: "competitor",
    plid: "501",
    商品: "Blue kettle",
    当前卖家: "Seller One",
    库存参考过期: false,
    库存数量: 8,
    库存上限: "8 件",
    趋势判断: "库存减少",
    价格信号: "价格不变",
    库存净流出: 2,
    库存净变化: -2,
    库存可比: true,
    新增评论: 3,
    新增好评: 2,
    新增差评: 0,
    新增跟卖卖家数: 1,
    跟卖发现日期: ["2026-08-11"],
    自有报价: [],
    跟卖报价: [{ 卖家ID: "123", 卖家: "Seller One", 库存信号: "库存减少" }],
  } as unknown as CompetitorItem;
  const ownStore = {
    来源: "own_store",
    plid: "601",
    商品: "Own red kettle",
    当前卖家: "YeboShop",
    库存参考过期: false,
    库存数量: 0,
    库存上限: "没货",
    趋势判断: "稳定",
    价格信号: "价格不变",
    库存净流出: 0,
    库存净变化: 0,
    库存可比: true,
    新增评论: 0,
    新增好评: 0,
    新增差评: 0,
    新增跟卖卖家数: 0,
    跟卖发现日期: [],
    自有报价: [],
    跟卖报价: [],
  } as unknown as CompetitorItem;
  const cards = buildPersonalWatchlistWorkspaceCards(
    [
      { plid: "501", added_at: "", source: "competitor", library_ids: [9] },
      { plid: "601", added_at: "", source: "own_store", library_ids: [9] },
    ],
    [],
    [trueCompetitor, ownStore],
  );

  const competitorResult = filterPersonalWatchlistWorkspaceCards(cards, {
    source: "competitor",
    query: "blue",
    sellerQuery: "sellers 123",
    stock: "有货",
    follower: "现在被跟卖",
    signal: "评论增加",
  });
  const ownResult = filterPersonalWatchlistWorkspaceCards(cards, {
    source: "own_store",
    query: "PLID601",
    sellerQuery: "ignored seller",
    stock: "没货",
    follower: "未发现跟卖",
    signal: "库存数量不变",
  });

  assert.deepEqual(competitorResult.map((card) => card.plid), ["501"]);
  assert.deepEqual(ownResult.map((card) => card.plid), ["601"]);
  assert.deepEqual(
    sortPersonalWatchlistWorkspaceCards(
      [...competitorResult, ...ownResult],
      "评论增加",
      "desc",
    ).map((card) => card.plid),
    ["501", "601"],
  );
});

test("follower status keeps current, historical-only, and never-seen products separate", () => {
  const currentFollower = {
    来源: "own_store",
    plid: "701",
    跟卖发现日期: ["2026-08-12"],
    跟卖报价: [{ 卖家ID: "current", 卖家: "Current Seller" }],
  } as unknown as CompetitorItem;
  const historicalFollower = {
    来源: "own_store",
    plid: "702",
    跟卖发现日期: ["2026-08-08"],
    跟卖报价: [],
  } as unknown as CompetitorItem;
  const neverFollowed = {
    来源: "own_store",
    plid: "703",
    跟卖发现日期: [],
    跟卖报价: [],
  } as unknown as CompetitorItem;
  const cards = buildPersonalWatchlistWorkspaceCards(
    [
      { plid: "701", added_at: "", source: "own_store", library_ids: [] },
      { plid: "702", added_at: "", source: "own_store", library_ids: [] },
      { plid: "703", added_at: "", source: "own_store", library_ids: [] },
    ],
    [],
    [currentFollower, historicalFollower, neverFollowed],
  );
  const filterByFollower = (
    follower: "现在被跟卖" | "曾经被跟卖" | "未发现跟卖",
  ) => filterPersonalWatchlistWorkspaceCards(cards, {
    source: "own_store",
    query: "",
    sellerQuery: "",
    stock: "全部",
    follower,
    signal: "全部",
  }).map((card) => card.plid);

  assert.deepEqual(filterByFollower("现在被跟卖"), ["701"]);
  assert.deepEqual(filterByFollower("曾经被跟卖"), ["702"]);
  assert.deepEqual(filterByFollower("未发现跟卖"), ["703"]);
});
