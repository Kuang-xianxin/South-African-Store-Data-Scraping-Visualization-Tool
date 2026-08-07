import type { CompetitorItem } from "./types";
import type { CompetitorOperatingSignal } from "./competitorOperatingSignals";

export type CompetitorListSortDirection = "asc" | "desc";

function sortValue(
  item: CompetitorItem,
  signal: CompetitorOperatingSignal,
): number | null {
  if (["降价", "涨价", "价格不变"].includes(signal)) return item.价格变化;
  if (["补货", "库存减少", "库存数量不变"].includes(signal)) {
    return item.库存净变化;
  }
  if (signal === "评论增加") return item.新增评论;
  if (signal === "好评增加") return item.新增好评;
  if (signal === "差评增加") return item.新增差评;
  if (signal === "库存减少且评论增加") return item.库存净流出;
  if (signal === "新增跟卖卖家") return item.新增跟卖卖家数;
  return null;
}

export function sortCompetitorItems(
  items: CompetitorItem[],
  signal: CompetitorOperatingSignal,
  direction: CompetitorListSortDirection,
): CompetitorItem[] {
  if (signal === "全部") return [...items];
  const multiplier = direction === "asc" ? 1 : -1;
  return items
    .map((item, index) => ({ item, index, value: sortValue(item, signal) }))
    .sort((first, second) => {
      if (first.value === null && second.value === null) return first.index - second.index;
      if (first.value === null) return 1;
      if (second.value === null) return -1;
      return (first.value - second.value) * multiplier || first.index - second.index;
    })
    .map(({ item }) => item);
}
