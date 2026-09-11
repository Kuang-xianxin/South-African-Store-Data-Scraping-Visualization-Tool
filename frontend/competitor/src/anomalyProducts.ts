import type { AnomalyProductItem, AnomalyProductPayload } from "./types";

export const ANOMALY_PRODUCT_VIEWS = [
  "sudden_sales_stop",
  "daily_bad_reviews",
  "poor_review_quality",
  "high_returns",
  "not_buyable",
  "disabled_by_takealot",
  "disabled_by_seller",
  "slow_moving",
] as const;

export type AnomalyProductView = (typeof ANOMALY_PRODUCT_VIEWS)[number];
export type SlowMovingSortDirection = "asc" | "desc";

export function slowMovingDaysLabel(item: AnomalyProductItem): string {
  return `${item.no_sales_days_exact ? "" : "至少 "}${item.no_sales_days} 天`;
}

export function slowMovingPeriodLabel(item: AnomalyProductItem): string {
  const start = item.slow_moving_started_on;
  const end = item.data_through;
  if (!start || !end) return "有货未动销区间待确认";
  return `${item.no_sales_days_exact ? "有货未动销" : "已确认区间"} ${start} 至 ${end}`;
}

export function slowMovingBoundaryLabel(item: AnomalyProductItem): string {
  if (!item.no_sales_days_exact) return "更早记录不足，实际起点未确认";
  if (item.slow_moving_boundary_reason === "out_of_stock") return "缺货后重新有货，从本轮起算";
  if (item.slow_moving_boundary_reason === "sale") return "从上次动销次日起算";
  return "按已确认起点计算";
}

export function lastRecordedSaleLabel(item: AnomalyProductItem): string {
  if (!item.last_sale_on) return "已采集销量中暂无动销记录";
  const days = item.days_since_last_sale;
  const interval = typeof days === "number" && Number.isInteger(days) && days >= 0
    ? `（距截止日 ${days} 天）`
    : "";
  return `最近已记录动销 ${item.last_sale_on}${interval}`;
}

export const ANOMALY_VIEW_LABELS: Record<AnomalyProductView, string> = {
  sudden_sales_stop: "动销突然中断",
  daily_bad_reviews: "当日新增差评",
  poor_review_quality: "累计差评偏高",
  high_returns: "公司SKU高退货",
  not_buyable: "不可购买有库存",
  disabled_by_takealot: "平台禁售有库存",
  disabled_by_seller: "卖家禁售有库存",
  slow_moving: "滞销产品",
};

export function itemsForAnomalyView(
  payload: AnomalyProductPayload | null,
  view: AnomalyProductView,
  slowDays: number,
  slowSortDirection: SlowMovingSortDirection = "desc",
): AnomalyProductItem[] {
  if (!payload) return [];
  if (view === "sudden_sales_stop") return payload.sudden_sales_stop;
  if (view === "daily_bad_reviews") return payload.daily_bad_reviews;
  if (view === "poor_review_quality") return payload.poor_review_quality;
  if (view === "high_returns") return payload.high_returns;
  if (view === "slow_moving") {
    const direction = slowSortDirection === "asc" ? 1 : -1;
    return payload.slow_moving
      .filter((item) => item.no_sales_days >= slowDays)
      .sort((first, second) => (
        (first.no_sales_days - second.no_sales_days) * direction
        || second.available_stock - first.available_stock
        || first.title.localeCompare(second.title, "zh-CN")
        || `${first.store_code ?? ""}:${first.offer_id}`.localeCompare(
          `${second.store_code ?? ""}:${second.offer_id}`,
        )
      ));
  }
  return payload.stock_status_anomalies[view];
}

export function countForAnomalyView(
  payload: AnomalyProductPayload | null,
  view: AnomalyProductView,
  slowDays: number,
): number {
  if (!payload) return 0;
  if (view === "sudden_sales_stop") return payload.summary.sudden_sales_stop;
  if (view === "daily_bad_reviews") return payload.summary.daily_bad_reviews;
  if (view === "poor_review_quality") return payload.summary.poor_review_quality;
  if (view === "high_returns") return payload.summary.high_returns;
  if (view === "slow_moving") {
    return payload.summary.slow_moving_by_days[String(slowDays)] ?? 0;
  }
  return payload.stock_status_anomalies[view].length;
}
