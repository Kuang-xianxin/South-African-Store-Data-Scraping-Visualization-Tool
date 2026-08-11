import assert from "node:assert/strict";
import test from "node:test";

import {
  buildPersonalWatchlistWorkspaceCards,
  personalWatchlistPageForPlid,
  recountPersonalWatchlistLibraries,
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
      },
      {
        plid: "11",
        added_at: "2026-08-11T03:00:00Z",
        library_ids: [8],
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
