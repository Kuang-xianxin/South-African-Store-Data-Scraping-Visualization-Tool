export interface HomeCoverage {
  known_store_days: number; expected_store_days: number; complete_stores: number; store_count: number;
}
export interface HomePoint extends HomeCoverage {
  date: string; orders: number | null; revenue: number | null; in_progress?: boolean;
  missing_order_ids?: number; missing_price_lines?: number;
}
export interface HomePeriod extends HomeCoverage {
  key: string; label: string; start: string; end: string; orders: number | null;
  units: number | null; revenue: number | null; sold_skus: number | null;
  sku_selling_rate: number | null; inventory_sell_through: number | null;
  sku_denominator: number; closing_stock: number | null; missing_order_ids: number; in_progress: boolean;
  missing_price_lines: number;
}
export interface HomeFinanceStore {
  store_code: string; store_name: string; current: number | null; available: number | null;
  held_back: number | null; paid_out: number | null; captured_at: string | null;
  payout_start: string | null; payout_end: string | null; payout_captured_at: string | null; latest_status: string;
}
export interface HomeWarehouseStore {
  store_code: string; store_name: string; stock: number | null; cost_rmb: number | null;
  gross_zar: number | null; cost_skus: number; profit_skus: number; missing_cost_skus: number;
  stock_missing_offers: number; regions: Record<string, number>; regions_complete: boolean;
  captured_at: string | null; regions_captured_at: string | null;
}
export interface HomeDashboard {
  today: string; store_count: number; history_start: string;
  rate: { rate: number | null; rate_date: string | null; fetched_at: string | null; stale?: boolean };
  periods: HomePeriod[]; daily: HomePoint[]; monthly: HomePoint[];
  finance: { stores: HomeFinanceStore[]; totals: Pick<HomeFinanceStore, "current" | "available" | "held_back" | "paid_out">; balance_coverage: number; payout_coverage: number };
  warehouse: { stores: HomeWarehouseStore[]; totals: Pick<HomeWarehouseStore, "stock" | "cost_rmb" | "gross_zar">; profit_skus: number; missing_cost_skus: number };
}

export function homeCoverageLabel(point: HomeCoverage): string {
  if (point.known_store_days === point.expected_store_days && point.expected_store_days > 0) return `${point.store_count} 店覆盖`;
  return `已核验 ${point.known_store_days}/${point.expected_store_days} 店日 · 部分数据`;
}

/** Missing points split the path; zero remains a real point on the baseline. */
export function homeChartPath(points: Array<{ x: number; y: number | null }>): string {
  let connected = false;
  return points.map(point => {
    if (point.y === null) { connected = false; return ""; }
    const command = connected ? "L" : "M";
    connected = true;
    return `${command}${point.x.toFixed(2)},${point.y.toFixed(2)}`;
  }).join(" ");
}
