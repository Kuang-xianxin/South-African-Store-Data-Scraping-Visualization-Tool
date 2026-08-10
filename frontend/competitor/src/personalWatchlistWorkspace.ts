import type {
  CompetitorItem,
  CompetitorPersonalWatchlistItem,
  CompetitorTargetItem,
  PersonalWatchlistLibrary,
} from "./types";

export interface PersonalWatchlistWorkspaceCard {
  plid: string;
  addedAt: string;
  source: CompetitorPersonalWatchlistItem["source"];
  libraryIds: number[];
  competitor: CompetitorItem | null;
  target: CompetitorTargetItem | null;
}

export function buildPersonalWatchlistWorkspaceCards(
  memberships: CompetitorPersonalWatchlistItem[],
  targets: CompetitorTargetItem[],
  competitors: CompetitorItem[],
): PersonalWatchlistWorkspaceCard[] {
  const targetsByPlid = new Map(targets.map((item) => [item.plid, item]));
  const competitorsByPlid = new Map(competitors.map((item) => [item.plid, item]));
  return memberships.map((membership) => ({
    plid: membership.plid,
    addedAt: membership.added_at,
    source: membership.source,
    libraryIds: membership.library_ids,
    competitor: competitorsByPlid.get(membership.plid) ?? null,
    target: targetsByPlid.get(membership.plid) ?? null,
  }));
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
): PersonalWatchlistLibrary[] {
  const counts = new Map(libraries.map((library) => [library.id, 0]));
  memberships.forEach((membership) => {
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
