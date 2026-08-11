import type {
  CompetitorItem,
  CompetitorPersonalWatchlistItem,
  CompetitorTargetItem,
  PersonalWatchlistLibrary,
  PersonalWatchlistSharedItem,
} from "./types";

export interface PersonalWatchlistWorkspaceCard {
  plid: string;
  addedAt: string;
  source: CompetitorPersonalWatchlistItem["source"] | "shared";
  personalMember: boolean;
  libraryIds: number[];
  competitor: CompetitorItem | null;
  target: CompetitorTargetItem | null;
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
  }));
  const personalPlids = new Set(memberships.map((membership) => membership.plid));
  const sharedCards = sharedItems
    .filter((item) => !personalPlids.has(item.plid))
    .map((item) => {
      const competitor = competitorsByPlid.get(item.plid) ?? null;
      return {
        plid: item.plid,
        addedAt: item.added_at,
        source: competitor?.来源 ?? "shared" as const,
        personalMember: false,
        libraryIds: item.library_ids,
        competitor,
        target: targetsByPlid.get(item.plid) ?? null,
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
