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
): AnomalyProductItem[] {
  if (!payload) return [];
  if (view === "sudden_sales_stop") return payload.sudden_sales_stop;
  if (view === "daily_bad_reviews") return payload.daily_bad_reviews;
  if (view === "poor_review_quality") return payload.poor_review_quality;
  if (view === "high_returns") return payload.high_returns;
  if (view === "slow_moving") {
    return payload.slow_moving.filter((item) => item.no_sales_days >= slowDays);
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
