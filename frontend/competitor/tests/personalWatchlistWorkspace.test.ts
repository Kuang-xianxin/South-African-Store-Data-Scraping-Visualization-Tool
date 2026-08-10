import assert from "node:assert/strict";
import test from "node:test";

import {
  buildPersonalWatchlistWorkspaceCards,
  personalWatchlistPageForPlid,
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

test("a located membership switches to the page containing its card", () => {
  const cards = Array.from({ length: 14 }, (_, index) => ({
    plid: String(index + 1),
    addedAt: "",
    source: "competitor" as const,
    libraryIds: [],
    competitor: null,
    target: null,
  }));

  assert.equal(personalWatchlistPageForPlid(cards, "1", 6), 1);
  assert.equal(personalWatchlistPageForPlid(cards, "7", 6), 2);
  assert.equal(personalWatchlistPageForPlid(cards, "14", 6), 3);
  assert.equal(personalWatchlistPageForPlid(cards, "99", 6), null);
});
