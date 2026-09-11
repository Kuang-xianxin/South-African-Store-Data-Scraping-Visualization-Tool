import type { CompetitorItem, CompetitorObservedSalesWindowKey } from "./types";
import type { CompetitorOperatingSignal } from "./competitorOperatingSignals";

export type CompetitorListSortDirection = "asc" | "desc";
export type CompetitorListSortMetric = "signal"
  | `sales_${CompetitorObservedSalesWindowKey | "total"}`
  | `follower_sales_${CompetitorObservedSalesWindowKey | "total"}`;

const salesWindows = ["7", "15", "30", "60", "90", "total"] as const;

export function competitorListSortOptions(own: boolean, signal: CompetitorOperatingSignal) {
  const options: Array<{ value: CompetitorListSortMetric; label: string }> = [
    { value: "signal", label: signal === "全部" ? "默认顺序" : competitorListSortMetricLabel(signal) },
  ];
  for (const days of salesWindows) {
    const label = days === "total" ? "总销量" : `${days}天销量`;
    options.push({ value: `sales_${days}`, label: `${own ? "自有官方" : "库存观察"} · ${label}` });
  }
  if (own) for (const days of salesWindows) {
    options.push({ value: `follower_sales_${days}`, label: `跟卖观察 · ${days === "total" ? "总销量" : `${days}天销量`}` });
  }
  return options;
}

export function effectiveCompetitorSortMetric(
  metric: CompetitorListSortMetric, own: boolean,
): CompetitorListSortMetric {
  return !own && metric.startsWith("follower_")
    ? metric.replace("follower_", "") as CompetitorListSortMetric : metric;
}

function salesValue(item: CompetitorItem, metric: CompetitorListSortMetric): number | null {
  const own = item.来源 === "own_store";
  const effective = effectiveCompetitorSortMetric(metric, own);
  const values = effective.startsWith("follower_sales_") ? item.跟卖近期观察售出
    : own ? item.自有官方销量 : item.近期观察售出;
  const key = effective.replace(/^(follower_)?sales_/, "") as CompetitorObservedSalesWindowKey | "total";
  const value = values?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function sortValue(
  item: CompetitorItem,
  signal: CompetitorOperatingSignal,
): number | null {
  if (["降价", "涨价", "价格不变"].includes(signal)) return item.价格变化;
  if (signal === "库存减少") return item.周期销售件数;
  if (["补货", "库存减少且评论增加"].includes(signal)) {
    return item.周期销售额;
  }
  if (signal === "库存数量不变") return item.库存净变化;
  if (signal === "库存变化大") return item.周期库存周转金额;
  if (signal === "评论增加") return item.新增评论;
  if (signal === "好评增加") return item.新增好评;
  if (signal === "差评增加") return item.新增差评;
  if (signal === "新增跟卖卖家") return item.新增跟卖卖家数;
  return null;
}

export function competitorListSortMetricLabel(signal: CompetitorOperatingSignal): string {
  if (signal === "库存减少") return "区间内售出件数";
  if (["补货", "库存减少且评论增加"].includes(signal)) {
    return "周期销售额";
  }
  if (signal === "库存变化大") return "库存周转金额";
  return "信号值";
}

export function sortCompetitorItems(
  items: CompetitorItem[],
  signal: CompetitorOperatingSignal,
  direction: CompetitorListSortDirection,
  metric: CompetitorListSortMetric = "signal",
): CompetitorItem[] {
  if (metric === "signal" && signal === "全部") return [...items];
  const multiplier = direction === "asc" ? 1 : -1;
  return items
    .map((item, index) => {
      const value = metric === "signal" ? sortValue(item, signal) : salesValue(item, metric);
      return { item, index, value: typeof value === "number" && Number.isFinite(value) ? value : null };
    })
    .sort((first, second) => {
      if (first.value === null && second.value === null) return first.index - second.index;
      if (first.value === null) return 1;
      if (second.value === null) return -1;
      return (first.value - second.value) * multiplier || first.index - second.index;
    })
    .map(({ item }) => item);
}
