export interface CompetitorItem {
  plid: string;
  商品: string;
  采集时间: string;
  当前卖家: string | null;
  价格: number | null;
  库存上限: string;
  库存数量: number | null;
  库存精确: boolean;
  评论数: number;
  评分: number | null;
  好评: number;
  中评: number;
  差评: number;
  累计销量估算: string;
  观察期销量信号: string;
  观察期估算下限: number | null;
  观察期估算上限: number | null;
  库存净流出: number | null;
  新增评论: number | null;
  趋势判断: string;
  判断说明: string;
  链接: string;
}

export interface ReviewItem {
  plid: string;
  评论日期: string | null;
  星级: number;
  标题: string | null;
  评论内容: string | null;
  评论人: string | null;
}

export interface CompetitorVariantItem {
  plid: string;
  快照ID: number;
  采集时间: string;
  变体键: string;
  变体: string;
  SKU: string | null;
  卖家: string | null;
  价格: number | null;
  库存: string;
  库存数量: number | null;
  库存精确: boolean;
  库存方式: string;
  库存说明: string | null;
  非平台仓: boolean;
  链接: string;
}

export interface CompetitorDetail {
  history: CompetitorItem[];
  reviews: ReviewItem[];
  variants: CompetitorVariantItem[];
}

export interface CollectResult {
  plid: string;
  title: string;
  message: string;
}

export interface StoreKpis {
  latest_ordered_units: number | null;
  latest_ordered_revenue: number | null;
  seven_day_ordered_units: number | null;
  latest_anomaly_products: number;
  page_views_30_days: number | null;
  median_conversion: number | null;
  selling_products: number;
  stockout_products: number;
}

export interface SalesPoint {
  metric_date: string;
  ordered_units: number | null;
  effective_units: number | null;
  ordered_revenue: number | null;
}

export interface ProductItem {
  metric_date: string;
  offer_id: string;
  sku: string | null;
  ordered_units: number | null;
  effective_units: number | null;
  ordered_revenue: number | null;
  page_views_30_days: number | null;
  page_views_30_day_average: number | null;
  page_views_window_net_change: number | null;
  conversion_percentage_30_days: number | null;
  conversion_percentage_previous_30_days: number | null;
  conversion_change_points: number | null;
  total_stock: number | null;
  offer_status: string | null;
  title?: string | null;
  tsin_id?: string | null;
  barcode?: string | null;
  selling_price?: number | null;
  rrp?: number | null;
  status?: string | null;
  status_label?: string | null;
  image_url?: string | null;
}

export interface SummaryPayload {
  as_of: string;
  latest_metric_date: string | null;
  kpis: StoreKpis;
  sales_series: SalesPoint[];
  top_products: ProductItem[];
}

export interface ProductsPayload {
  latest_metric_date: string | null;
  items: ProductItem[];
}

export interface ProductDetailPayload {
  identity: Record<string, unknown>;
  kpis: Record<string, number | string | null>;
  history: ProductItem[];
}

export type QuadrantKey =
  | "star"
  | "conversion_issue"
  | "potential"
  | "optimize"
  | "unclassified";

export interface QuadrantItem extends ProductItem {
  ordered_units: number | null;
  page_views_rank: number | null;
  ordered_units_rank: number | null;
  quadrant: QuadrantKey;
}

export interface QuadrantPayload {
  window_start: string | null;
  window_end: string | null;
  percentile: number;
  boundaries: {
    page_views: number | null;
    ordered_units: number | null;
    page_views_rank: number | null;
    ordered_units_rank: number | null;
  };
  counts: Record<QuadrantKey, number>;
  items: QuadrantItem[];
}

export interface AnomalyItem {
  event_date: string;
  offer_id: string;
  anomaly_type: string;
  anomaly_label: string;
  severity: string | null;
  severity_label: string;
  explanation: string;
}

export interface QualityItem {
  event_id: string;
  event_date: string;
  event_type: string;
  event_label: string;
  severity: string | null;
  severity_label: string;
  offer_id: string | null;
  details_text: string;
}

export interface RiskPayload {
  latest_metric_date: string | null;
  latest_anomalies: AnomalyItem[];
  anomalies: AnomalyItem[];
  quality_events: QualityItem[];
  summary: {
    latest_anomaly_products: number;
    latest_anomaly_records: number;
    quality_events: number;
    unknown_sale_status: number;
  };
}

export interface FreshnessPayload {
  last_collection_at: string | null;
  latest_metric_date: string | null;
}

export interface ExportFile {
  kind: "html" | "excel" | "png";
  label: string;
  exists: boolean;
  name: string;
  download_url: string | null;
}

export interface ExportPayload {
  as_of: string;
  files: ExportFile[];
  png_error?: string | null;
}

export interface NftInspection {
  filename: string;
  size_bytes: number;
  sha256: string;
  latest_report_date: string;
  suggested_report_date: string;
  product_columns: number;
}

export interface NftGeneration {
  report_date: string;
  workbook_name: string;
  audit_name: string;
  workbook_url: string;
  audit_url: string;
}
