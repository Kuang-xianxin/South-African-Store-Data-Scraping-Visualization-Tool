import type {
  CompetitorItem,
  CompetitorPersonalWatchlistItem,
  CompetitorTargetItem,
  PersonalWatchlistDetailAccess,
  PersonalWatchlistLibrary,
  PersonalWatchlistSharedItem,
} from "./types";
import { followerOffers } from "./competitorOfferHistory.ts";
import {
  matchesCompetitorOperatingSignal,
  type CompetitorOperatingSignal,
} from "./competitorOperatingSignals.ts";
import {
  sortCompetitorItems,
  type CompetitorListSortDirection,
} from "./competitorListSort.ts";
import {
  matchesCompetitorProductSearchValues,
  matchesCompetitorSearch,
} from "./competitorSearch.ts";
import { matchesCompetitorSellerFilter } from "./competitorSellerFilter.ts";

export interface PersonalWatchlistWorkspaceCard {
  plid: string;
  addedAt: string;
  source: CompetitorPersonalWatchlistItem["source"] | PersonalWatchlistSharedItem["source"];
  personalMember: boolean;
  libraryIds: number[];
  competitor: CompetitorItem | null;
  target: CompetitorTargetItem | null;
  detailAccess?: PersonalWatchlistDetailAccess | null;
}

export type PersonalWatchlistSourceView = "competitor" | "own_store";
export type PersonalWatchlistUnavailableReason =
  | "store_access_denied"
  | "authorized_store_data_unavailable"
  | "shared_details_unavailable";
export type PersonalWatchlistStockFilter = "全部" | "有货" | "没货" | "未探测";
export type PersonalWatchlistFollowerFilter =
  | "全部"
  | "现在被跟卖"
  | "曾经被跟卖"
  | "未发现跟卖";

export interface PersonalWatchlistWorkspaceFilters {
  source: PersonalWatchlistSourceView;
  query: string;
  sellerQuery: string;
  stock: PersonalWatchlistStockFilter;
  follower: PersonalWatchlistFollowerFilter;
  signal: CompetitorOperatingSignal;
}

export function buildPersonalWatchlistWorkspaceCards(
  memberships: CompetitorPersonalWatchlistItem[],
  targets: CompetitorTargetItem[],
  competitors: CompetitorItem[],
  sharedItems: PersonalWatchlistSharedItem[] = [],
): PersonalWatchlistWorkspaceCard[] {
  const targetsByPlid = new Map(targets.map((item) => [item.plid, item]));
  const competitorsByPlid = new Map(competitors.map((item) => [item.plid, item]));
  const personalCards = memberships.map((membership) => ({
    plid: membership.plid,
    addedAt: membership.added_at,
    source: membership.source,
    personalMember: true,
    libraryIds: membership.library_ids,
    competitor: competitorsByPlid.get(membership.plid) ?? null,
    target: targetsByPlid.get(membership.plid) ?? null,
    detailAccess: null,
  }));
  const personalPlids = new Set(memberships.map((membership) => membership.plid));
  const sharedCards = sharedItems
    .filter((item) => !personalPlids.has(item.plid))
    .map((item) => {
      const competitor = competitorsByPlid.get(item.plid) ?? null;
      return {
        plid: item.plid,
        addedAt: item.added_at,
        source: competitor?.来源 ?? item.source,
        personalMember: false,
        libraryIds: item.library_ids,
        competitor,
        target: targetsByPlid.get(item.plid) ?? null,
        detailAccess: item.detail_access,
      };
    });
  return [...personalCards, ...sharedCards];
}

export function personalWatchlistPageForPlid(
  cards: PersonalWatchlistWorkspaceCard[],
  plid: string,
  pageSize: number,
): number | null {
  if (pageSize <= 0) return null;
  const index = cards.findIndex((item) => item.plid === plid);
  return index < 0 ? null : Math.floor(index / pageSize) + 1;
}

export function recountPersonalWatchlistLibraries(
  libraries: PersonalWatchlistLibrary[],
  memberships: CompetitorPersonalWatchlistItem[],
  sharedItems: PersonalWatchlistSharedItem[] = [],
): PersonalWatchlistLibrary[] {
  const counts = new Map(libraries.map((library) => [library.id, 0]));
  [...memberships, ...sharedItems].forEach((membership) => {
    new Set(membership.library_ids).forEach((libraryId) => {
      if (counts.has(libraryId)) {
        counts.set(libraryId, (counts.get(libraryId) ?? 0) + 1);
      }
    });
  });
  return libraries.map((library) => ({
    ...library,
    item_count: counts.get(library.id) ?? 0,
  }));
}

export function personalWatchlistCardSource(
  card: PersonalWatchlistWorkspaceCard,
): PersonalWatchlistSourceView {
  if (card.competitor?.来源) return card.competitor.来源;
  return card.source === "own_store" ? "own_store" : "competitor";
}

export function personalWatchlistUnavailableReason(
  card: PersonalWatchlistWorkspaceCard,
): PersonalWatchlistUnavailableReason | null {
  if (card.competitor) return null;
  if (card.detailAccess === "store_access_denied") return "store_access_denied";
  if (card.source === "own_store") return "authorized_store_data_unavailable";
  if (!card.personalMember && card.source === "unknown") return "shared_details_unavailable";
  return null;
}

export function personalWatchlistCompetitorStockState(
  item: CompetitorItem,
): Exclude<PersonalWatchlistStockFilter, "全部"> {
  if (item.库存参考过期) return "未探测";
  if (item.库存数量 !== null) return item.库存数量 > 0 ? "有货" : "没货";
  const label = item.库存上限.trim();
  if (label.includes("没货") || label.includes("售罄")) return "没货";
  if (/\d/.test(label)) return "有货";
  return "未探测";
}

export function matchesFollowerPresenceFilter(
  item: CompetitorItem,
  filter: PersonalWatchlistFollowerFilter,
): boolean {
  if (filter === "全部") return true;
  const hasCurrentFollowers = followerOffers(item).length > 0;
  const hadFollowersInRange = item.跟卖发现日期.length > 0;
  if (filter === "现在被跟卖") return hasCurrentFollowers;
  if (filter === "曾经被跟卖") return hadFollowersInRange && !hasCurrentFollowers;
  return !hadFollowersInRange && !hasCurrentFollowers;
}

export function filterPersonalWatchlistWorkspaceCards(
  cards: PersonalWatchlistWorkspaceCard[],
  filters: PersonalWatchlistWorkspaceFilters,
): PersonalWatchlistWorkspaceCard[] {
  return cards.filter((card) => {
    if (personalWatchlistCardSource(card) !== filters.source) return false;
    const item = card.competitor;
    if (item) {
      if (!matchesCompetitorSearch(item, filters.query)) return false;
    } else if (!matchesCompetitorProductSearchValues(
      [card.target?.title],
      [card.plid, card.target?.url],
      filters.query,
    )) {
      return false;
    }
    if (
      filters.source === "competitor"
      && filters.sellerQuery.trim()
      && (!item || !matchesCompetitorSellerFilter(item, filters.sellerQuery))
    ) {
      return false;
    }
    if (filters.stock !== "全部") {
      const stock = item ? personalWatchlistCompetitorStockState(item) : "未探测";
      if (stock !== filters.stock) return false;
    }
    if (filters.follower !== "全部") {
      if (!item) return false;
      if (!matchesFollowerPresenceFilter(item, filters.follower)) return false;
    }
    return item
      ? matchesCompetitorOperatingSignal(item, filters.signal)
      : filters.signal === "全部";
  });
}

export function sortPersonalWatchlistWorkspaceCards(
  cards: PersonalWatchlistWorkspaceCard[],
  signal: CompetitorOperatingSignal,
  direction: CompetitorListSortDirection,
): PersonalWatchlistWorkspaceCard[] {
  if (signal === "全部") return [...cards];
  const rankedItems = sortCompetitorItems(
    cards.flatMap((card) => card.competitor ? [card.competitor] : []),
    signal,
    direction,
  );
  const rankByPlid = new Map(rankedItems.map((item, index) => [item.plid, index]));
  return [...cards].sort((left, right) => (
    (rankByPlid.get(left.plid) ?? Number.MAX_SAFE_INTEGER)
    - (rankByPlid.get(right.plid) ?? Number.MAX_SAFE_INTEGER)
  ));
}
