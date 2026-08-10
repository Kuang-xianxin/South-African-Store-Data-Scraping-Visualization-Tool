import type {
  CompetitorItem,
  CompetitorPersonalWatchlistItem,
  CompetitorTargetItem,
} from "./types";

export interface PersonalWatchlistWorkspaceCard {
  plid: string;
  addedAt: string;
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
