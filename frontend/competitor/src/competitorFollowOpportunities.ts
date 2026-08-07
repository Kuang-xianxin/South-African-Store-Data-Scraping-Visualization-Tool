import type { CompetitorItem, FollowSellingOpportunityType } from "./types";

export type FollowSellingOpportunityFilter =
  | "全部"
  | "可跟卖机会"
  | FollowSellingOpportunityType;

export interface FollowSellingOpportunitySummary {
  total: number;
  soldOut: number;
  noSeller: number;
}

export function matchesFollowSellingOpportunity(
  item: CompetitorItem,
  filter: FollowSellingOpportunityFilter,
): boolean {
  if (item.来源 !== "competitor" || filter === "全部") return true;
  if (filter === "可跟卖机会") return item.跟卖机会 === true;
  return item.跟卖机会类型 === filter;
}

export function summarizeFollowSellingOpportunities(
  items: CompetitorItem[],
): FollowSellingOpportunitySummary {
  const competitorItems = items.filter((item) => item.来源 === "competitor");
  return {
    total: competitorItems.filter((item) => item.跟卖机会 === true).length,
    soldOut: competitorItems.filter((item) => item.跟卖机会类型 === "全部报价售罄").length,
    noSeller: competitorItems.filter((item) => item.跟卖机会类型 === "暂无卖家报价").length,
  };
}
