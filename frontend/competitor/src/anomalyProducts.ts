import type { AnomalyProductItem, AnomalyProductPayload } from "./types";

export const ANOMALY_PRODUCT_VIEWS = [
  "sudden_sales_stop",
  "not_buyable",
  "disabled_by_takealot",
  "disabled_by_seller",
  "slow_moving",
] as const;

export type AnomalyProductView = (typeof ANOMALY_PRODUCT_VIEWS)[number];

export const ANOMALY_VIEW_LABELS: Record<AnomalyProductView, string> = {
  sudden_sales_stop: "动销突然中断",
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
  if (view === "slow_moving") {
    return payload.summary.slow_moving_by_days[String(slowDays)] ?? 0;
  }
  return payload.stock_status_anomalies[view].length;
}
