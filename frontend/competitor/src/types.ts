export interface CompetitorOfferItem {
  报价键: string;
  报价来源?: "seller_api" | "public_offer";
  offer_id: string | null;
  卖家ID: string | null;
  卖家: string;
  SKU: string | null;
  价格: number | null;
  库存状态: string;
  库存原始状态: string;
  库存数量: number | null;
  库存精确: boolean;
  库存方式: string;
  库存说明: string | null;
  条件: string | null;
  变体键: string;
  变体: string;
  是否主报价: boolean;
  是否变体主报价: boolean;
  是否跟卖?: boolean;
  plid: string;
  链接: string;
  区间起始价格: number | null;
  价格变化: number | null;
  价格信号: string;
  区间起始库存状态: string | null;
  区间起始库存数量: number | null;
  库存数量变化: number | null;
  库存可比: boolean;
  库存信号: string;
  店铺?: string;
  Takealot可售库存?: number | null;
  卖家可售库存?: number | null;
}

export interface CompetitorItem {
  来源: "competitor" | "own_store";
  快照ID: number;
  plid: string;
  商品: string;
  图片: string | null;
  采集时间: string;
  当前卖家: string | null;
  价格: number | null;
  区间起始价格: number | null;
  价格变化: number | null;
  价格信号: string;
  库存上限: string;
  库存数量: number | null;
  库存精确: boolean;
  库存说明: string | null;
  库存参考过期: boolean;
  上次成功库存: string | null;
  上次成功库存数量: number | null;
  上次成功库存精确: boolean;
  上次成功库存时间: string | null;
  评论数: number;
  评论数可用?: boolean;
  评分: number | null;
  好评: number;
  中评: number;
  差评: number;
  观察期销量信号: string;
  观察期估算下限: number | null;
  观察期估算上限: number | null;
  库存净变化: number | null;
  库存净流入: number | null;
  库存净流出: number | null;
  新增评论: number | null;
  新增好评: number | null;
  新增差评: number | null;
  趋势判断: string;
  判断说明: string;
  信号区间开始: string | null;
  信号区间结束: string | null;
  区间快照数: number | null;
  库存可比: boolean | null;
  链接: string;
  跟卖报价: CompetitorOfferItem[];
  对比报价?: CompetitorOfferItem[];
  自有报价: OwnStoreOfferItem[];
  共享评论说明: string | null;
  跟卖发现日期: string[];
  新增跟卖卖家数: number;
  新增跟卖卖家: string[];
  跟卖卖家明细: OwnFollowerSellerEvent[];
}

export interface OwnFollowerSellerEvent {
  卖家ID: string | null;
  卖家: string;
  首次发现日期: string;
  区间发现日期: string[];
  区间观察次数: number;
  是否区间新增: boolean;
}

export interface OwnFollowerHistoryItem {
  plid: string;
  链接: string;
  商品: string;
  图片: string | null;
  店铺: string[];
  跟卖发现日期: string[];
  新增跟卖卖家数: number;
  新增跟卖卖家: string[];
  跟卖卖家明细: OwnFollowerSellerEvent[];
}

export interface OwnStoreOfferItem {
  offer_id: string;
  店铺: string;
  SKU: string | null;
  价格: number | null;
  库存: number | null;
  Takealot可售库存?: number | null;
  卖家可售库存?: number | null;
  状态: string | null;
  基准日: string;
  拉取时间: string;
}

export interface CompetitorDateRange {
  available_start: string | null;
  available_end: string | null;
  selected_start: string | null;
  selected_end: string | null;
}

export interface CompetitorOverview {
  items: CompetitorItem[];
  store_items: CompetitorItem[];
  own_follower_events: OwnFollowerHistoryItem[];
  date_range: CompetitorDateRange;
}

export interface CompetitorStoreTargetItem {
  plid: string;
  url: string;
  title: string;
  offer_count: number;
  store_count: number;
  store_names: string[];
  captured_at: string;
}

export type OwnStoreScope = "current" | "all";

export interface CompetitorStoreTargetPayload {
  items: CompetitorStoreTargetItem[];
  scope: OwnStoreScope;
  selected_store_count: number;
  selected_membership_count: number;
  all_store_count: number;
  all_store_unique_count: number;
  all_store_membership_count: number;
}

export interface CompetitorTargetItem {
  plid: string;
  offer_group_plid: string;
  url: string;
  title: string | null;
  created_at: string;
  updated_at: string;
  has_history: boolean;
}

export interface CompetitorTargetAuditItem {
  id: number;
  plid: string;
  action: "add" | "update" | "delete" | "manual_retry" | "auto_discover";
  old_url: string | null;
  new_url: string | null;
  actor_username: string;
  actor_display_name: string;
  changed_at: string;
}

export interface CompetitorTargetAuditPayload {
  items: CompetitorTargetAuditItem[];
  total: number;
  page: number;
  page_size: number;
  date_range: CompetitorDateRange;
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
  图片: string | null;
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
  每位客户限购: number | null;
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
  url?: string;
  added_target_count?: number;
}

export interface CompetitorLinkHealthItem {
  plid: string;
  url: string;
  商品: string | null;
  图片: string | null;
  status: "suspected_invalid" | "confirmed_invalid";
  confirmed_not_found_count: number;
  first_not_found_at: string | null;
  last_checked_at: string;
  control_plid: string | null;
  control_check_ok: boolean | null;
  last_error: string | null;
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
  traffic_series: StoreTrafficPoint[];
  top_products: ProductItem[];
}

export interface StoreTrafficPoint {
  business_date: string;
  captured_at: string;
  status: "success" | "failed";
  page_views_30_days_total: number | null;
  product_count: number;
  missing_product_count: number;
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
  first_listed_at: string | null;
  first_listed_source: "platform" | "first_observed";
  latest_restock_date: string | null;
  latest_restock_increase: number | null;
  quadrant: QuadrantKey;
}

export type KeywordTrafficDirection = "up" | "down" | "flat" | "unavailable";
export type KeywordTrendChange =
  | "reversal_up"
  | "reversal_down"
  | "improving"
  | "weakening"
  | "stable"
  | "insufficient";

export interface KeywordTrafficProductSummary {
  offer_id: string;
  sku: string | null;
  title: string | null;
  image_url: string | null;
  latest_page_views_30_days: number | null;
  latest_snapshot_date: string | null;
  keyword_event_count: number;
  keyword_change_count: number;
  last_keyword_change_date: string | null;
  current_keywords: string[];
}

export interface KeywordTrafficListPayload {
  as_of: string;
  items: KeywordTrafficProductSummary[];
  summary: {
    product_count: number;
    with_traffic_count: number;
    archived_product_count: number;
    keyword_change_count: number;
  };
}

export interface KeywordTrafficHistoryPoint {
  date: string;
  page_views_30_days: number | null;
}

export interface KeywordTrafficWindow {
  start_date: string;
  end_date: string;
  available_days: number;
  first_value: number | null;
  last_value: number | null;
  window_net_change: number | null;
  slope_per_day: number | null;
  trend_direction: KeywordTrafficDirection;
}

export interface KeywordTrafficComparison {
  status: "waiting" | "collecting" | "data_missing" | "complete";
  comparison_days: number;
  observed_after_days: number;
  before: KeywordTrafficWindow;
  after: KeywordTrafficWindow;
  traffic_direction: KeywordTrafficDirection;
  traffic_delta: number | null;
  traffic_delta_percent: number | null;
  trend_change: KeywordTrendChange;
  slope_change: number | null;
}

export interface KeywordTrafficEvent {
  id: number;
  effective_date: string;
  event_kind: "baseline" | "change";
  event_source: "offer_title";
  change_label: string;
  keywords: string[];
  previous_keywords: string[];
  added_keywords: string[];
  removed_keywords: string[];
  source_title: string;
  previous_source_title: string | null;
  detected_at: string;
  comparison: KeywordTrafficComparison;
}

export interface KeywordTrafficDetailPayload {
  as_of: string;
  history_days: number;
  comparison_days: number;
  product: {
    offer_id: string;
    sku: string | null;
    title: string | null;
    image_url: string | null;
    current_keywords: string[];
  };
  history: KeywordTrafficHistoryPoint[];
  events: KeywordTrafficEvent[];
  metric_notice: string;
}

export type UserRole = "viewer" | "operator" | "selection" | "admin";

export type PermissionKey =
  | "store.view"
  | "logistics.manage"
  | "keyword_traffic.manage"
  | "competitors.view"
  | "competitors.collect"
  | "daily_report.view"
  | "daily_report.manage"
  | "daily_report.export"
  | "reports.view"
  | "reports.generate"
  | "nft102.manage"
  | "refresh.run"
  | "users.manage";

export interface StoreAccessItem {
  id: number;
  code: string;
  display_name: string;
  active: boolean;
  data_connected: boolean;
}

export interface ManagedStore extends StoreAccessItem {
  created_at: string;
  updated_at: string;
}

export interface AuthUser {
  id: number;
  username: string;
  display_name: string;
  role: UserRole;
  permissions: PermissionKey[];
  permissions_customized: boolean;
  all_stores: boolean;
  assigned_store_ids: number[];
  accessible_stores: StoreAccessItem[];
}

export interface AuthSession {
  user: AuthUser;
  csrf_token: string;
  expires_at: string;
}

export interface AuthStatus {
  setup_required: boolean;
  bootstrap_allowed: boolean;
}

export interface ManagedUser extends AuthUser {
  active: boolean;
  created_at: string;
  updated_at: string;
  last_login_at: string | null;
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
  metric_date?: string | null;
  title?: string | null;
  sku?: string | null;
  tsin_id?: string | null;
  barcode?: string | null;
  image_url?: string | null;
  selling_price?: number | null;
  rrp?: number | null;
  status_label?: string | null;
  total_stock?: number | null;
  page_views_30_days?: number | null;
  ordered_units_7_days?: number | null;
  effective_units?: number | null;
  ordered_revenue?: number | null;
  conversion_percentage_30_days?: number | null;
  first_listed_at?: string | null;
  first_listed_source?: "platform" | "first_observed" | null;
  latest_restock_date?: string | null;
  latest_restock_increase?: number | null;
  details?: {
    short_window_days?: number;
    long_window_days?: number;
    short_window_average_units?: number;
    long_window_average_units?: number;
    page_views_30_days?: number;
    high_views_threshold?: number;
    low_views_threshold?: number;
    conversion_percentage_30_days?: number;
    low_conversion_threshold?: number;
    high_conversion_threshold?: number;
    total_stock?: number;
    offer_status?: string | null;
    recent_7_day_units?: number;
    captured_at?: string;
    stale_age_hours?: number;
    stale_hours_threshold?: number;
    sale_statuses?: string[];
    sales_daily_series?: Array<{
      date: string;
      ordered_units: number | null;
    }>;
    sales_series_covered_days?: number;
    sales_window_days?: number;
    sales_window_total_units?: number | null;
    sales_window_start?: string | null;
    sales_window_end?: string | null;
    sales_window_complete?: boolean;
  } | null;
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

export interface LogisticsStatusCount {
  status: string;
  count: number;
}

export interface W8InboundItem {
  order_no: string;
  status: string;
  created_at: string;
  forecast_date: string;
  inbound_date: string;
  shelf_date: string;
  headway_no: string;
  shipping_mark: string;
  sku_types: number;
  forecast_quantity: number;
}

export interface W8OutboundItem {
  order_no: string;
  status: string;
  created_at: string;
  outbound_date: string;
  waybill_no: string;
  logistics_type: string;
  sku_types: number;
  total_quantity: number;
  has_document: boolean;
}

export interface TakealotShipmentItem {
  shipment_id: number | null;
  reference: string;
  purchase_order_number: string;
  destination_region: string;
  purchase_order_state: string;
  shipment_type: string;
  shipped: boolean;
  cancelled: boolean;
  due_date: string;
  date_unloaded: string;
  tracking_info: string;
  sku_lines: number;
  quantity_sending: number;
  quantity_received: number;
  quantity_damaged: number;
}

export interface LogisticsHighConfidenceCandidate {
  confidence: "high" | "medium" | "low";
  method: string;
  w8_order_no: string;
  w8_headway_no: string;
  w8_shipping_mark: string;
  w8_status: string;
  w8_created_at: string;
  takealot_shipment_id: number;
  takealot_purchase_order_number: string;
  takealot_reference: string;
  takealot_state: string;
  takealot_created_at: string;
  sku_lines: number;
  w8_sku_lines: number;
  takealot_sku_lines: number;
  shared_sku_lines: number;
  overlap_ratio: number;
  quantity: number;
  w8_quantity: number;
  takealot_quantity: number;
  quantity_delta: number;
  date_gap_days: number;
  w8_candidate_count: number;
  takealot_candidate_count: number;
  ambiguous: boolean;
}

export interface LogisticsConfirmedLink {
  id: number;
  w8_order_no: string;
  takealot_shipment_id: number;
  takealot_purchase_order_number: string;
  takealot_reference: string;
  confidence: "high" | "medium" | "low";
  sku_lines: number;
  quantity: number;
  w8_quantity: number;
  takealot_quantity: number;
  quantity_delta: number;
  date_gap_days: number | null;
  confirmed_by: string;
  confirmed_at: string;
  active: boolean;
}

export interface LogisticsOverviewPayload {
  generated_at: string;
  cache_ttl_seconds: number;
  cache_age_seconds: number;
  automatic_page_refresh: boolean;
  w8: {
    connected: boolean;
    live_connected: boolean;
    data_source: "live_api" | "local_database" | "unavailable";
    synced_at: string | null;
    snapshot_saved: boolean;
    refresh_attempted: boolean;
    provider: string;
    environment: string;
    message?: string;
    warehouse: {
      id: number;
      code: string;
      name: string;
      country: string;
    } | null;
    channels: Array<{ code: string; name: string }>;
    summary: {
      products: number;
      stock_records: number;
      stock_total: number;
      usable_stock: number;
      locked_stock: number;
      outbound_allocated: number;
      transit_stock: number;
      defective_stock: number;
      inbound_orders: number;
      outbound_orders: number;
      returned_records: number;
    };
    inbound_statuses: LogisticsStatusCount[];
    outbound_statuses: LogisticsStatusCount[];
    recent_inbound: W8InboundItem[];
    recent_outbound: W8OutboundItem[];
    warnings: string[];
  };
  takealot: {
    connected: boolean;
    live_connected: boolean;
    data_source: "live_api" | "local_database" | "unavailable";
    synced_at: string | null;
    snapshot_saved: boolean;
    refresh_attempted: boolean;
    message?: string;
    summary: {
      shipments: number;
      replenishment: number;
      shipped: number;
      unloaded: number;
      cancelled: number;
      with_tracking_info: number;
      quantity_sending: number;
      quantity_received: number;
      quantity_damaged: number;
    };
    recent_shipments: TakealotShipmentItem[];
    warnings?: string[];
  };
  matching: {
    method: string;
    direct_match_count: number;
    matched_w8_inbound: number;
    matched_takealot_shipments: number;
    unmatched_w8_inbound: number;
    unmatched_takealot_shipments: number;
    confirmed_link_count: number;
    confirmed_links: LogisticsConfirmedLink[];
    high_confidence_candidate_count: number;
    high_confidence_candidates: LogisticsHighConfidenceCandidate[];
    medium_confidence_candidate_count: number;
    medium_confidence_candidates: LogisticsHighConfidenceCandidate[];
    low_confidence_candidate_count: number;
    low_confidence_candidates: LogisticsHighConfidenceCandidate[];
    split_batch_group_count: number;
    split_batch_groups: Array<{
      w8_order_no: string;
      w8_created_at: string;
      w8_quantity: number;
      sku_lines: number;
      takealot_shipment_ids: number[];
      takealot_purchase_order_numbers: string[];
      shipment_count: number;
      max_date_gap_days: number;
      method: string;
    }>;
    warnings: string[];
    items: Array<{
      w8_order_no: string;
      w8_headway_no: string;
      takealot_shipment_id: number;
      takealot_reference: string;
    }>;
  };
  boundaries: string[];
}

export interface PlatformWarehouseOffer {
  offer_id: string;
  sku: string | null;
  tsin_id: string | null;
  title: string | null;
  image_url: string | null;
  status: string | null;
  total_stock: number | null;
  takealot_available_stock: number | null;
  takealot_stock_on_way: number | null;
  takealot_stock_in_receiving: number | null;
  official_warehouse_capacity: number | null;
  capacity_reason: string;
}

export interface PlatformWarehouseDraftLine {
  id: number;
  offer_id: string;
  sku: string | null;
  tsin_id: string | null;
  title: string | null;
  image_url: string | null;
  cpt_quantity: number;
  jhb_quantity: number;
  dbn_quantity: number;
  total_quantity: number;
}

export interface PlatformWarehouseDraftAudit {
  id: number;
  action: string;
  action_label: string;
  actor_username: string;
  note: string | null;
  details: Record<string, unknown> | null;
  created_at: string;
}

export interface PlatformWarehouseDraft {
  id: number;
  draft_number: string;
  client_request_id: string | null;
  status: string;
  status_label: string;
  upstream_mode: "local_only" | "guarded_bff";
  po_number: string | null;
  platform_shipment_id: number | null;
  tracking_reference: string | null;
  review_task_id: number | null;
  reviewed_at: string | null;
  review_expires_at: string | null;
  create_task_id: number | null;
  last_error: string | null;
  note: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  po_confirmed_at: string | null;
  shipped_at: string | null;
  archived_at: string | null;
  line_count: number;
  quantity_totals: {
    cpt_quantity: number;
    jhb_quantity: number;
    dbn_quantity: number;
  };
  lines: PlatformWarehouseDraftLine[];
  shipments: PlatformWarehouseLinkedShipment[];
  audits: PlatformWarehouseDraftAudit[];
}

export interface PlatformWarehouseLinkedShipment {
  shipment_id: number;
  region: string | null;
  facility_code: string | null;
  facility_id: number | null;
  reference: string | null;
  status: string;
  status_label: string;
  po_number: string | null;
  tracking_reference: string | null;
  last_task_id: number | null;
  updated_at: string;
}

export interface PlatformWarehousePortalStatus {
  enabled: boolean;
  base_url: string;
  max_total_quantity: number;
  shipped_write_enabled: boolean;
  authenticated: boolean;
  requires_otp: boolean;
  otp_destination: string | null;
  expires_at: string | null;
  identity: Record<string, unknown> | null;
  credential_configured: boolean;
  credential_email: string | null;
  credential_error: string | null;
  credentials_persisted: boolean;
}

export interface PlatformWarehouseShipment {
  shipment_id: number | null;
  reference: string;
  purchase_order_number: string;
  destination_region: string;
  purchase_order_state: string;
  shipment_type: string;
  shipped: boolean;
  archived: boolean;
  cancelled: boolean;
  due_date: string;
  created_at: string;
  date_unloaded: string;
  tracking_info: string;
  sku_lines: number;
  quantity_sending: number;
  quantity_received: number;
}

export interface PlatformWarehousePayload {
  generated_at: string;
  capability: {
    write_mode: "disabled_by_default" | "guarded_seller_portal_bff";
    official_shipment_write_supported: boolean;
    message: string;
  };
  portal: PlatformWarehousePortalStatus;
  offers: PlatformWarehouseOffer[];
  drafts: PlatformWarehouseDraft[];
  platform_shipments: PlatformWarehouseShipment[];
  platform_snapshot_synced_at: string | null;
}

export type DailyReportStatus =
  | "awaiting_evening"
  | "ready"
  | "needs_review"
  | "missing_capture"
  | "confirmed";

export interface DailyReportValues {
  page_views_30_days: number | null;
  ordered_units: number | null;
  platform_stock: number | null;
}

export interface DailyReportConfirmationRevert {
  previous_confirmation: {
    values: DailyReportValues;
    source: "morning" | "evening" | "latest" | "manual";
    source_label: string;
    confirmed_by: string | null;
    confirmed_at: string | null;
    confirm_note: string | null;
  };
  previous_stock_alert: {
    dismissed: boolean;
    note: string | null;
    dismissed_by: string | null;
    dismissed_at: string | null;
  };
  reverted_by: string | null;
  reverted_at: string;
  revert_note: string;
}

export interface DailyReportItem {
  offer_id: string;
  sku: string | null;
  title: string;
  image_url: string | null;
  status: DailyReportStatus;
  morning: DailyReportValues | null;
  evening: DailyReportValues | null;
  capture_versions: Array<{
    run_id: string;
    slot: "morning" | "evening" | "pre_close" | "manual";
    label: string;
    captured_at: string;
    values: DailyReportValues;
  }>;
  manual: DailyReportValues | null;
  manual_reason: string | null;
  manual_note: string | null;
  manual_at: string | null;
  final: DailyReportValues | null;
  confirmation_baseline: {
    values: DailyReportValues;
    source: "morning" | "evening" | "latest" | "manual";
    source_label: string;
    confirmed_by: string;
    confirmed_at: string;
    confirm_note: string | null;
  } | null;
  confirmation_revert: DailyReportConfirmationRevert | null;
  review_versions: Array<{
    kind: "confirmed" | "capture" | "manual";
    run_id: string | null;
    slot: "morning" | "evening" | "pre_close" | "manual" | null;
    label: string;
    captured_at: string | null;
    values: DailyReportValues;
    source_label: string | null;
    user_name: string | null;
    note: string | null;
  }>;
  selected_source: "morning" | "evening" | "latest" | "manual" | null;
  confirm_note: string | null;
  operator_note: string | null;
  operator_notes: Array<{
    id: number;
    issue_type: "general" | "capture_difference" | "stock_continuity";
    note: string;
    user_id: number | null;
    user_name: string;
    created_at: string;
    updated_by: string | null;
    updated_at: string | null;
  }>;
  confirmation_trigger: {
    kind: "previous_confirmation";
    message: string;
    trigger_business_date: string;
    affected_business_date: string;
    confirmation_source: "morning" | "evening" | "latest" | "manual";
    confirmation_source_label: string;
    confirmed_by: string | null;
    confirmed_at: string;
    confirmation_note: string;
    previous_stock_before_confirmation: number | null;
    confirmed_previous_stock: number;
    current_ordered_units: number;
    expected_stock_before_confirmation: number | null;
    comparison_before_state?: "matched" | "mismatch" | "unavailable";
    expected_stock_after_confirmation: number;
    actual_stock: number;
    affected_previous_status: DailyReportStatus;
    affected_previous_final: DailyReportValues | null;
    affected_previous_confirmed_by: string | null;
    affected_previous_confirmed_at: string | null;
    affected_previous_confirm_note: string | null;
    affected_current_values: DailyReportValues;
  } | null;
  differences: Array<keyof DailyReportValues>;
  review_issues: Array<{
    type:
      | "capture_difference"
      | "stock_continuity"
      | "confirmation_reverted"
      | "confirmation_revert_impact";
    fields: Array<keyof DailyReportValues>;
  }>;
  missing_capture: boolean;
  missing_slots: Array<"morning" | "evening">;
  missing_run_ids: string[];
  missing_fields: Array<keyof DailyReportValues>;
  missing_reason: string | null;
  current: DailyReportValues;
  stock_context: {
    business_date: string;
    stock: number | null;
    source:
      | "confirmed"
      | "latest_capture"
      | "version_difference"
      | "confirmation_reverted";
    source_label: string;
    selected_source: "morning" | "evening" | "latest" | "manual" | null;
    confirmed_by: string | null;
    confirmed_at: string | null;
    confirm_note: string | null;
    capture_label: string | null;
    continuity_ready: boolean;
    version_differences: Array<keyof DailyReportValues>;
    confirmation_revert?: DailyReportConfirmationRevert | null;
  } | null;
  stock_check: {
    previous_stock: number | null;
    expected_stock: number | null;
    actual_stock: number | null;
    mismatch: boolean;
    dismissed: boolean;
    note: string | null;
    resolution_action: "eliminate" | "confirm_difference";
    deferred_reason: string | null;
  };
}

export interface DailyReportPendingAction extends DailyReportItem {
  business_date: string;
}

export interface DailyReportHandledAction {
  id: number;
  action_type:
    | "confirmation"
    | "stock_difference"
    | "stock_eliminated"
    | "manual_candidate"
    | "operator_note"
    | "operator_note_updated"
    | "operator_note_deleted"
    | "confirmation_reverted"
    | "stock_alert_reopened";
  business_date: string;
  offer_id: string;
  sku: string | null;
  title: string;
  image_url: string | null;
  handled_by: string;
  handled_at: string;
  note: string | null;
  active: boolean;
  reversal: {
    kind: string;
    handled_by: string;
    handled_at: string;
    note: string | null;
  } | null;
  current: DailyReportValues;
  detail: {
    source: "morning" | "evening" | "latest" | "manual" | null;
    source_label: string | null;
    previous_stock: number | null;
    ordered_units: number | null;
    expected_stock: number | null;
    actual_stock: number | null;
    reason: string | null;
    issue_type:
      | "general"
      | "capture_difference"
      | "stock_continuity"
      | null;
    before_note: string | null;
    after_note: string | null;
    deleted_note: string | null;
    before_values: DailyReportValues | null;
    after_values: DailyReportValues | null;
  };
}

export interface DailyReportPayload {
  business_date: string;
  runs: Array<{
    run_id: string;
    slot: "morning" | "evening" | "pre_close" | "manual";
    captured_at: string;
    status: string;
    counts: Record<string, unknown>;
  }>;
  capture_status: Record<
    "morning" | "evening" | "pre_close",
    {
      status: "success" | "failed" | "missing" | "pending" | "not_applicable";
      captured_at: string | null;
      product_count: number;
      reason: string | null;
      attempts: Array<{
        attempt: number;
        strategy: string;
        trust_env: boolean;
        started_at: string;
        finished_at: string;
        status: "success" | "failed";
        workflow_status: string;
        reason: string;
        offer_run_id: string | null;
        sales_run_id: string | null;
      }>;
      attempt_count: number;
      recovered: boolean;
      capture_method: string | null;
    }
  >;
  capture_issues: Array<{
    business_date: string;
    kind: "slot" | "product";
    slot: "morning" | "evening" | "pre_close" | "manual" | null;
    offer_id: string | null;
    sku: string | null;
    title: string | null;
    reason: string;
  }>;
  capture_issue_range: {
    available_start: string;
    available_end: string;
    selected_start: string;
    selected_end: string;
  };
  comparison_history: Array<{
    business_date: string;
    inventory_context: {
      inventory_date: string;
      captured_at: string | null;
      source_slot: "morning" | "evening" | "pre_close" | "manual" | null;
      source_label: string | null;
      delayed: boolean;
      resolved_after_missing: boolean;
      complete: boolean;
      product_count: number;
      missing_count: number;
      note: string;
      exception_note: string | null;
    };
    items: Array<
      Pick<
        DailyReportItem,
        | "offer_id"
        | "sku"
        | "title"
        | "image_url"
        | "status"
        | "missing_capture"
        | "missing_reason"
        | "current"
        | "stock_check"
        | "operator_note"
        | "operator_notes"
        | "confirmation_baseline"
        | "confirmation_revert"
      >
    >;
  }>;
  pending_actions: DailyReportPendingAction[];
  handled_actions: DailyReportHandledAction[];
  counts: {
    products: number;
    current_stock_total: number | null;
    current_stock_missing: number;
    with_sales: number;
    awaiting_evening: number;
    ready: number;
    needs_review: number;
    missing_capture: number;
    confirmed: number;
    stock_alerts: number;
  };
  items: DailyReportItem[];
  prior_reminders: DailyReportReminderDate[];
  deadline_snapshot: {
    snapped_at: string;
    unresolved_count: number;
    resolved_at: string | null;
  } | null;
}

export interface DailyReportReminderDate {
  business_date: string;
  unresolved_count: number;
}

export interface DailyReportReminders {
  count: number;
  dates: DailyReportReminderDate[];
}

export interface DailyReportExport {
  through: string;
  blocked: boolean;
  unresolved: Array<{
    business_date: string;
    offer_id: string;
    sku: string | null;
    title: string | null;
    status: DailyReportStatus;
  }>;
  exists: boolean;
  download_url: string | null;
  name?: string;
}
