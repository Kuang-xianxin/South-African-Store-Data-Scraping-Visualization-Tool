export interface StoreScopedRecord {
  store_code?: string | null;
  store_name?: string | null;
  store_scope_key?: string | null;
}

export interface ProductMasterIdentity extends StoreScopedRecord {
  company_sku?: string | null;
  company_product_name?: string | null;
  cost_rmb?: number | null;
  cost_effective_date?: string | null;
}

export interface CompetitorOfferItem extends ProductMasterIdentity {
  报价键: string;
  报价来源?: "seller_api" | "public_offer";
  offer_id: string | null;
  卖家ID: string | null;
  卖家: string;
  SKU: string | null;
  TSIN?: string | null;
  图片?: string | null;
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
  /** Latest current status for this exact connected-store Offer identity. */
  最新Offer状态?: string | null;
  最新Offer状态更新时间?: string | null;
  /** Current total-stock projection for the same exact Offer identity. */
  最新Offer库存数量?: number | null;
  最新Offer库存状态?: "有货" | "没货" | "未探测" | null;
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
  周期销售件数: number | null;
  周期销售额: number | null;
  周期补货量: number | null;
  周期补货货值: number | null;
  周期库存周转金额: number | null;
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
  跟卖机会?: boolean;
  跟卖机会类型?: FollowSellingOpportunityType | null;
  跟卖机会说明?: string;
  公开报价数?: number | null;
  跟卖报价: CompetitorOfferItem[];
  对比报价?: CompetitorOfferItem[];
  /** Current Seller Offers status projection; independent of the observation date range. */
  最新Offer状态?: string[];
  最新Offer状态更新时间?: string | null;
  自有报价: OwnStoreOfferItem[];
  company_skus?: string[];
  company_sku?: string | null;
  共享评论说明: string | null;
  跟卖发现日期: string[];
  新增跟卖卖家数: number;
  新增跟卖卖家: string[];
  跟卖卖家明细: OwnFollowerSellerEvent[];
}

export type FollowSellingOpportunityType = "全部报价售罄" | "暂无卖家报价";

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

export interface OwnStoreOfferItem extends ProductMasterIdentity {
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

export interface OwnStoreCompetitorOverview {
  store_items: CompetitorItem[];
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

export type OwnStoreScope = "current" | "all" | "operating";

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

export type CompetitorListingSourceType = "seller" | "category";

export interface CompetitorListingProduct {
  plid: string;
  title: string;
  url: string;
  sort_ranks: Record<string, number>;
}

export interface CompetitorListingPreview {
  source_type: CompetitorListingSourceType;
  source_url: string;
  source_label: string;
  price_min: number | null;
  price_max: number | null;
  sorts: string[];
  sort_options: Array<{ value: string; label: string }>;
  source_total: number | null;
  requires_limit: boolean;
  product_limit: number | null;
  can_commit: boolean;
  scanned_candidate_count: number;
  deduplicated_candidate_count: number;
  candidate_capacity: number;
  candidate_queue_frozen: boolean;
  duplicate_count: number;
  selected_count: number;
  selection_rule: "balanced_rank_fusion_then_plid_deduplicate";
  products: CompetitorListingProduct[];
  candidate_products: CompetitorListingProduct[];
  preview_token: string | null;
}

export interface CompetitorListingCommitResult {
  source_type: CompetitorListingSourceType;
  source_url: string;
  operation_id: number;
  personal_library_id: number;
  personal_library_name: string;
  selected_count: number;
  added_target_count: number;
  reactivated_target_count: number;
  existing_target_count: number;
  own_store_count: number;
  personal_watchlist_added_count: number;
  queued_to_active_batch_count: number;
}

export type CompetitorListingOperationItemResult =
  | "added_target"
  | "reactivated_target"
  | "existing_target"
  | "own_store";

export interface CompetitorListingOperationItem {
  id: number;
  operation_id: number;
  position: number;
  plid: string;
  title: string;
  url: string;
  result: CompetitorListingOperationItemResult;
  personal_watchlist_added: boolean;
  sort_ranks: Record<string, number>;
}

export interface CompetitorListingOperation {
  id: number;
  source_type: CompetitorListingSourceType;
  source_url: string;
  source_label: string;
  personal_library_id: number | null;
  personal_library_name: string | null;
  price_min: number | null;
  price_max: number | null;
  sorts: string[];
  selection_rule: "balanced_rank_fusion_then_plid_deduplicate";
  product_limit: number | null;
  selected_count: number;
  added_target_count: number;
  reactivated_target_count: number;
  existing_target_count: number;
  own_store_count: number;
  personal_watchlist_added_count: number;
  actor_username: string;
  actor_display_name: string;
  committed_at: string;
}

export interface CompetitorListingOperationPayload {
  items: CompetitorListingOperation[];
  total: number;
  page: number;
  page_size: number;
  source_type: CompetitorListingSourceType | null;
}

export interface CompetitorListingOperationItemPayload {
  items: CompetitorListingOperationItem[];
  total: number;
  page: number;
  page_size: number;
  operation_id: number;
}

export interface CompetitorPersonalWatchlistItem {
  plid: string;
  added_at: string;
  source: "competitor" | "own_store";
  library_ids: number[];
}

export type PersonalWatchlistDetailAccess =
  | "public"
  | "authorized"
  | "store_access_denied"
  | "unknown";

export interface PersonalWatchlistSharedItem {
  plid: string;
  added_at: string;
  library_ids: number[];
  source: "competitor" | "own_store" | "unknown";
  detail_access: PersonalWatchlistDetailAccess;
}

export type PersonalWatchlistLibraryAccess = "owner" | "read" | "edit";
export type PersonalWatchlistLibrarySharePermission = "read" | "edit";

export interface PersonalWatchlistLibraryShare {
  user_id: number;
  username: string;
  display_name: string;
  active: boolean;
  permission: PersonalWatchlistLibrarySharePermission;
}

export interface PersonalWatchlistShareUser {
  id: number;
  username: string;
  display_name: string;
  active: boolean;
}

export interface PersonalWatchlistLibrary {
  id: number;
  name: string;
  created_at: string;
  updated_at: string;
  item_count: number;
  owner_user_id: number;
  owner_username: string;
  owner_display_name: string;
  access: PersonalWatchlistLibraryAccess;
  is_owner: boolean;
  share_count: number;
  shares: PersonalWatchlistLibraryShare[];
}

export interface CompetitorPersonalWatchlistPayload {
  items: CompetitorPersonalWatchlistItem[];
  count: number;
  shared_items: PersonalWatchlistSharedItem[];
  libraries: PersonalWatchlistLibrary[];
  default_library_configured: boolean;
  default_library_id: number | null;
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

export interface CompetitorCategoryBreadcrumb {
  name: string;
  id: string | null;
  type: string | null;
  slug: string | null;
}

export interface OwnStoreSalesPoint {
  date: string;
  ordered_units: number | null;
  data_status: "verified" | "partial" | "missing";
  revision_count: number;
}

export interface OwnStoreSalesSeries {
  store_code: string;
  store_name: string;
  plid: string;
  offer_ids: string[];
  skus: string[];
  listing_date: string;
  listing_date_source: "platform" | "first_observed";
  through_date: string;
  date_basis: "Asia/Shanghai";
  source_date_basis: "Africa/Johannesburg";
  total_ordered_units: number | null;
  covered_days: number;
  partial_days: number;
  missing_days: number;
  coverage_start: string | null;
  coverage_end: string | null;
  points: OwnStoreSalesPoint[];
}

export interface OwnStoreTrafficPoint {
  date: string;
  captured_at: string | null;
  page_views_30_days: number | null;
  title: string | null;
  title_changed: boolean;
  previous_title: string | null;
  data_status: "observed" | "missing";
}

export interface OwnStoreTrafficSeries {
  store_code: string;
  store_name: string;
  plid: string;
  offer_id: string;
  sku: string | null;
  range_start: string | null;
  range_end: string | null;
  observed_count: number;
  traffic_count: number;
  missing_count: number;
  points: OwnStoreTrafficPoint[];
  metric_notice: string;
}

export interface ReturnFilterOption {
  value: string;
  label: string;
  count: number;
}

export interface SellerReturnItem {
  seller_return_id: string;
  order_id: string | null;
  order_item_id: string | null;
  offer_id: string | null;
  tsin_id: string | null;
  sku: string | null;
  return_reference_number: string | null;
  quantity: number;
  return_date: string | null;
  return_region: string | null;
  return_reason: string | null;
  return_reason_label: string;
  customer_comment: string | null;
  outcome_statuses: string[];
  outcome_labels: string[];
  outcomes: Array<Record<string, unknown>>;
  transactions: Array<Record<string, unknown>>;
  transaction_total_incl_vat: number;
  captured_at: string | null;
  productline_id: string | null;
  product_title: string | null;
  image_url: string | null;
  offer_quantity_returned_30_days: number | null;
  company_sku?: string | null;
  company_product_name?: string | null;
  store_code: string;
  store_name: string;
  store_scope_key: string;
}

export interface ReturnSummary {
  return_count: number;
  return_units: number;
  affected_product_count: number;
  quality_related_units: number;
  sellable_stock_units: number;
  removal_order_units: number;
  transaction_total_incl_vat: number;
}

export interface ReturnCollectionStoreStatus {
  store_code: string;
  store_name: string;
  data_status: "collected" | "partial" | "stale" | "failed" | "uncollected" | "unavailable";
  last_attempt_at: string | null;
  last_success_at: string | null;
  requested_from: string | null;
  requested_through: string | null;
  record_count: number | null;
  latest_error: string | null;
}

export interface OfferReturnedCounter {
  units: number | null;
  covered_offer_count: number;
  offer_count: number;
  covered_store_count: number;
  store_count: number;
  captured_at: string | null;
  metric: "quantity_returned_30_days";
  window: "rolling_30_days";
}

export interface ReturnsPayload {
  range_start: string;
  range_end: string;
  date_basis: "Africa/Johannesburg";
  store_scope?: OwnStoreScope;
  store_count?: number;
  data_status: "collected" | "partial" | "failed" | "uncollected" | "unavailable";
  store_statuses: ReturnCollectionStoreStatus[];
  offer_returned_30_days: OfferReturnedCounter;
  summary: ReturnSummary;
  filters: {
    reasons: ReturnFilterOption[];
    outcomes: ReturnFilterOption[];
  };
  items: SellerReturnItem[];
  total: number;
  page: number;
  page_size: number;
  message?: string;
  source_notice: string;
}

export interface OwnStoreProfitScenario {
  key: "current_gross" | "current_fee_adjusted" | "rrp_gross";
  label: string;
  price_zar: number;
  price_rmb: number;
  estimated_fees_zar: number;
  estimated_fees_rmb: number;
  profit_rmb: number;
  profit_margin_percentage: number;
  cost_markup_percentage: number;
  note: string;
}

export interface OwnStoreProfitFeeBasis {
  status: "available" | "unverified" | "no_sales" | "incomplete";
  window_days: number;
  covered_days: number;
  sales_days: number;
  order_line_count: number;
  ordered_units: number;
  sales_revenue_zar: number | null;
  total_fees_zar: number | null;
  fee_rate_percentage: number | null;
  invalid_line_count?: number;
  source: string;
  message: string;
}

export interface OwnStoreProfitabilityItem extends ProductMasterIdentity {
  offer_key: string;
  store_code: string;
  store_name: string;
  offer_id: string;
  plid: string;
  sku: string | null;
  cost_rmb: number | null;
  cost_effective_date: string | null;
  cost_zar: number | null;
  selling_price_zar: number | null;
  rrp_zar: number | null;
  fee_basis: OwnStoreProfitFeeBasis;
  scenarios: {
    current_gross: OwnStoreProfitScenario | null;
    current_fee_adjusted: OwnStoreProfitScenario | null;
    rrp_gross: OwnStoreProfitScenario | null;
  };
  message: string;
}

export interface OwnStoreProfitabilityPayload {
  items: OwnStoreProfitabilityItem[];
  store_codes: string[];
  fee_window: {
    start: string;
    end: string;
    days: number;
    date_basis: "Africa/Johannesburg";
  };
  exchange_rate: {
    base_currency: "CNY";
    quote_currency: "ZAR";
    rate: number | null;
    rate_date: string | null;
    fetched_at: string | null;
    source: string;
    status: "converted" | "stale" | "unavailable" | "not_required";
    message: string;
  };
  message: string;
}

export interface CompetitorDetail {
  category_path: CompetitorCategoryBreadcrumb[];
  history: CompetitorItem[];
  reviews: ReviewItem[];
  variants: CompetitorVariantItem[];
  own_store_sales: OwnStoreSalesSeries[];
  own_store_traffic: OwnStoreTrafficSeries[];
  own_store_returns: ReturnsPayload;
  own_store_profitability: OwnStoreProfitabilityPayload;
  company_inventory?: CompanyInventoryPayload;
}

export interface InventoryStageValue {
  value: number;
  coverage: number;
}

export interface CompanyOverseasInventory {
  available: boolean;
  matched: boolean;
  snapshot_at: string | null;
  warehouse: { id?: number; code?: string; name?: string; country?: string } | null;
  record_count: number;
  message: string;
  stages: Partial<Record<
    | "stock_total"
    | "usable_stock"
    | "locked_stock"
    | "outbound_allocated"
    | "transit_stock"
    | "defective_stock",
    InventoryStageValue
  >>;
}

export interface CompanyPlatformInventoryOffer {
  store_code: string;
  store_name: string;
  offer_id: string;
  plid: string | null;
  platform_sku: string;
  company_sku: string;
  status: string | null;
  platform_available_stock: number | null;
  platform_stock_on_way: number | null;
  platform_stock_in_receiving: number | null;
  captured_at: string | null;
}

export interface CompanyInventoryItem extends ProductMasterIdentity {
  company_sku: string;
  company_product_name: string;
  mapped_platform_skus: string[];
  overseas_warehouse: CompanyOverseasInventory;
  platform_warehouse: {
    offer_count: number;
    stages: {
      available: InventoryStageValue;
      on_way: InventoryStageValue;
      in_receiving: InventoryStageValue;
    };
    offers: CompanyPlatformInventoryOffer[];
    latest_captured_at: string | null;
  };
}

export interface CompanyInventoryPayload {
  items: CompanyInventoryItem[];
  store_codes: string[];
  company_sku_count: number;
  w8_shared_once: true;
  stage_totals_are_additive: false;
  message: string;
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

export interface ProductItem extends ProductMasterIdentity {
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
  productline_id?: string | null;
}

export interface SummaryPayload {
  as_of: string;
  range_start: string;
  range_end: string;
  latest_metric_date: string | null;
  kpis: StoreKpis;
  sales_series: SalesPoint[];
  traffic_series: StoreTrafficPoint[];
  top_products: ProductItem[];
  operators: StoreOperator[];
}

export interface StoreTrafficReference {
  source_slot: string;
  captured_at: string;
  page_views_30_days_total: number;
  product_count: number;
  missing_product_count: number;
}

export interface StoreTrafficPoint {
  business_date: string;
  captured_at: string;
  status: "success" | "failed";
  page_views_30_days_total: number | null;
  product_count: number;
  missing_product_count: number;
  reference: StoreTrafficReference | null;
}

export interface StoreOperator {
  user_id: number;
  display_name: string;
  role: UserRole;
}

export interface StoreInventorySummary {
  captured_at: string | null;
  offer_count: number;
  platform_available_stock: number | null;
  platform_available_coverage: number;
  platform_stock_on_way: number | null;
  platform_stock_on_way_coverage: number;
  platform_stock_in_receiving: number | null;
  platform_stock_in_receiving_coverage: number;
}

export interface StoreHealth {
  state: "attention" | "data_gap" | "healthy";
  label: string;
  priority: number;
  business_reasons: string[];
  data_reasons: string[];
}

export interface SalesRevenueSource {
  kind: string;
  label: string;
  endpoint?: string;
  table?: string;
  run_id?: string | null;
  requested_start?: string;
  requested_end?: string;
  record_count?: number;
  collected_at?: string;
  verified_at?: string | null;
  recorded_at?: string;
  metric_date?: string;
}

export interface StoreSalesReconciliation {
  store_code: string;
  store_name: string;
  status: "verified" | "recovered" | "pending" | "unverified";
  period_end_business_date: string | null;
  period_end_status: "success" | "failed" | null;
  period_end_captured_at: string | null;
  period_end_failure_reason: string | null;
  latest_sales_verified_at: string | null;
  revision_count: number;
  latest_revision_at: string | null;
}

export interface StoreOverviewItem {
  store_code: string;
  store_name: string;
  latest_metric_date: string | null;
  kpis: StoreKpis;
  latest_traffic_point: StoreTrafficPoint | null;
  operators: StoreOperator[];
  inventory: StoreInventorySummary;
  sales_reconciliation: StoreSalesReconciliation;
  health: StoreHealth;
}

export interface OverseasWarehouseSummary {
  snapshot_at: string | null;
  warehouse_name: string | null;
  stock_total: number | null;
  usable_stock: number | null;
  locked_stock: number | null;
  outbound_allocated: number | null;
  transit_stock: number | null;
  defective_stock: number | null;
  shared_across_stores: boolean;
}

export interface PlatformWarehouseSummary extends StoreInventorySummary {
  store_count: number;
  store_count_with_offers: number;
}

export interface MultiStoreRevenuePoint {
  metric_date: string;
  total_ordered_revenue: number | null;
  covered_store_count: number;
  store_count: number;
  missing_store_count: number;
  data_status: "verified" | "revised" | "pending";
  source_verified_store_count: number;
  pending_reconciliation_store_count: number;
  unverified_source_store_count: number;
  revised_store_count: number;
  revision_count: number;
  latest_sales_verified_at: string | null;
  latest_revision_at: string | null;
}

export interface SalesReconciliationSummary {
  period_end_business_date: string | null;
  failed_store_count: number;
  pending_store_count: number;
  recovered_store_count: number;
  verified_store_count: number;
  unverified_store_count: number;
  revision_count: number;
  latest_sales_verified_at: string | null;
  latest_revision_at: string | null;
  stores: StoreSalesReconciliation[];
}

export interface SalesRevenueRevision {
  id: number;
  store_code: string;
  store_name: string;
  metric_date: string;
  change_type: "corrected" | "backfilled";
  before_ordered_units: number | null;
  after_ordered_units: number | null;
  before_ordered_revenue: number | null;
  after_ordered_revenue: number | null;
  revenue_delta: number | null;
  units_delta: number | null;
  before_source: SalesRevenueSource;
  after_source: SalesRevenueSource;
  source_run_id: string | null;
  detected_at: string;
}

export interface SalesRevenueRevisionPayload {
  items: SalesRevenueRevision[];
  total: number;
  page: number;
  page_size: number;
  start_date: string | null;
  end_date: string | null;
  source_policy: {
    before: string;
    after: string;
    immutable: true;
  };
}

export interface StoreOverviewPayload {
  as_of: string;
  range_start: string;
  range_end: string;
  store_count: number;
  health_summary: {
    attention: number;
    data_gap: number;
    healthy: number;
  };
  logistics: {
    overseas_warehouse: OverseasWarehouseSummary;
    platform_warehouse: PlatformWarehouseSummary;
  };
  sales_revenue_series: MultiStoreRevenuePoint[];
  sales_revenue_completed_through: string;
  sales_reconciliation: SalesReconciliationSummary;
  stores: StoreOverviewItem[];
}

export interface ProductsPayload {
  latest_metric_date: string | null;
  store_scope?: OwnStoreScope;
  store_count?: number;
  store_metric_dates?: Record<string, string | null>;
  items: ProductItem[];
}

export interface ProductCostConversion {
  base_currency: "CNY";
  quote_currency: "ZAR";
  cost_rmb: number | null;
  cost_zar: number | null;
  rate: number | null;
  rate_date: string | null;
  fetched_at: string | null;
  source: string;
  status: "converted" | "stale" | "missing_cost" | "unavailable";
  message: string;
}

export interface ProductDetailPayload {
  identity: ProductMasterIdentity & Record<string, unknown>;
  kpis: Record<string, number | string | null>;
  history: ProductItem[];
  cost_conversion: ProductCostConversion;
}

export type AnomalyProductType =
  | "sudden_sales_stop"
  | "not_buyable_with_stock"
  | "disabled_by_takealot_with_stock"
  | "disabled_by_seller_with_stock"
  | "slow_moving"
  | "daily_bad_review"
  | "poor_review_quality"
  | "high_return_volume";

export interface AnomalyReviewRecord {
  review_id: string;
  rating: number | null;
  title: string | null;
  body: string | null;
  customer_name: string | null;
  review_date: string | null;
  first_seen_at: string | null;
  first_seen_on: string | null;
}

export interface AnomalyReturnReasonCount {
  reason: string;
  label: string;
  units: number;
  records: number;
}

export interface AnomalyReturnRecord {
  seller_return_id: string;
  return_date: string | null;
  quantity: number;
  return_reason: string | null;
  return_reason_label: string;
  customer_comment: string | null;
  sku: string | null;
  plid: string | null;
  store_code?: string | null;
  store_name?: string | null;
}

export type AnomalyReturnDataStatus =
  | "collected"
  | "partial"
  | "stale"
  | "failed"
  | "uncollected";

export interface AnomalyReturnCoverage {
  data_status: AnomalyReturnDataStatus;
  window_start: string | null;
  window_end: string | null;
  window_days: number;
  source: "seller_returns_detail";
  uncollected_is_zero: false;
  covered_store_count?: number;
  store_count?: number;
  stores?: Array<{
    store_code: string;
    store_name: string;
    data_status: AnomalyReturnDataStatus;
    requested_from?: string | null;
    requested_through?: string | null;
    last_success_at?: string | null;
    latest_error?: string | null;
  }>;
}

export interface AnomalyProductItem extends ProductMasterIdentity {
  anomaly_type: AnomalyProductType;
  anomaly_label: string;
  offer_id: string;
  plid: string;
  tsin_id: string | null;
  sku: string | null;
  title: string;
  image_url: string | null;
  selling_price: number | null;
  page_views_30_days: number | null;
  conversion_percentage_30_days: number | null;
  offer_status: string;
  offer_status_label: string;
  available_stock: number;
  takealot_available_stock: number;
  seller_available_stock: number;
  receiving_stock: number;
  on_way_stock: number;
  inventory_units: number;
  data_through: string | null;
  offer_collected_at?: string | null;
  sales_collected_at?: string | null;
  review_collected_at?: string | null;
  return_collected_at?: string | null;
  latest_ordered_units: number | null;
  no_sales_days: number;
  no_sales_days_exact: boolean;
  last_sale_on: string | null;
  slow_moving_started_on?: string | null;
  stop_started_on?: string;
  zero_sales_dates?: string[];
  baseline_start_on?: string;
  baseline_end_on?: string;
  baseline_total_units?: number;
  baseline_selling_days?: number;
  baseline_daily_average?: number;
  store_codes?: string[];
  store_names?: string[];
  platform_skus?: string[];
  plids?: string[];
  offer_ids?: string[];
  company_skus?: string[];
  review_count?: number;
  bad_review_count?: number;
  bad_review_rate_percentage?: number;
  bad_review_rating_counts?: Record<string, number>;
  review_baseline_first_seen_at?: string | null;
  review_discovered_on?: string;
  new_bad_review_count?: number;
  new_bad_reviews?: AnomalyReviewRecord[];
  recent_bad_reviews?: AnomalyReviewRecord[];
  return_units_30_days?: number;
  return_record_count?: number;
  affected_platform_sku_count?: number;
  return_reason_counts?: AnomalyReturnReasonCount[];
  recent_returns?: AnomalyReturnRecord[];
  return_window_start?: string | null;
  return_window_end?: string | null;
  return_data_status?: AnomalyReturnDataStatus;
}

export interface AnomalyProductPayload {
  requested_as_of: string;
  completed_through: string;
  data_through: string | null;
  date_basis: "Africa/Johannesburg";
  collection_times: {
    offers_at: string | null;
    sales_at: string | null;
    reviews_at: string | null;
    returns_at: string | null;
    latest_at: string | null;
  };
  sales_zero_evidence: "verified_complete_business_days_only";
  rules: {
    sales_stop_zero_days: number;
    sales_stop_baseline_days: number;
    sales_stop_min_selling_days: number;
    sales_stop_min_baseline_units: number;
    slow_day_options: number[];
    slow_moving_requires_status: "buyable";
    slow_moving_requires_available_stock: boolean;
    slow_moving_day_basis: "verified_zero_sales_and_positive_stock_days";
    stock_status_requires_available_stock: boolean;
    stock_status_excluded_inventory: Array<"receiving" | "on_way">;
    bad_review_rating_below: number;
    daily_bad_review_basis: "first_seen_after_plid_review_baseline";
    poor_review_min_bad_count: number;
    poor_review_min_bad_rate_percentage: number;
    poor_review_identity: "plid";
    return_window_days: number;
    high_return_min_units: number;
    high_return_identity: "company_sku";
    high_return_source: "seller_returns_detail";
    uncollected_returns_are_zero: false;
  };
  summary: {
    sudden_sales_stop: number;
    not_buyable_with_stock: number;
    disabled_by_takealot_with_stock: number;
    disabled_by_seller_with_stock: number;
    slow_moving_by_days: Record<string, number>;
    daily_bad_reviews: number;
    poor_review_quality: number;
    high_returns: number;
  };
  sudden_sales_stop: AnomalyProductItem[];
  stock_status_anomalies: {
    not_buyable: AnomalyProductItem[];
    disabled_by_takealot: AnomalyProductItem[];
    disabled_by_seller: AnomalyProductItem[];
  };
  slow_moving: AnomalyProductItem[];
  daily_bad_reviews: AnomalyProductItem[];
  poor_review_quality: AnomalyProductItem[];
  review_discovery_through: string | null;
  return_coverage: AnomalyReturnCoverage;
  high_returns: AnomalyProductItem[];
}

export type QuadrantKey =
  | "star"
  | "conversion_issue"
  | "potential"
  | "optimize"
  | "unclassified";

export interface ProductLifecycleContext {
  first_listed_at: string | null;
  first_listed_source: "platform" | "first_observed";
  latest_restock_date: string | null;
  latest_restock_increase: number | null;
}

export interface QuadrantItem extends ProductItem, ProductLifecycleContext {
  ordered_units: number | null;
  page_views_rank: number | null;
  ordered_units_rank: number | null;
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

export interface KeywordTrafficProductSummary extends ProductMasterIdentity {
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
  store_scope?: OwnStoreScope;
  store_count?: number;
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
  source_title: string | null;
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

export interface KeywordTrafficProduct extends ProductLifecycleContext, ProductMasterIdentity {
  offer_id: string;
  sku: string | null;
  title: string | null;
  image_url: string | null;
  current_keywords: string[];
}

export interface KeywordTrafficDetailPayload {
  as_of: string;
  history_days: number;
  comparison_days: number;
  product: KeywordTrafficProduct;
  history: KeywordTrafficHistoryPoint[];
  events: KeywordTrafficEvent[];
  metric_notice: string;
}

export interface SearchRankingProductFactRecommendation {
  recommended: boolean;
  reason_code: string;
  reason: string;
  source_analysis_id: number;
  requires_human_confirmation: true;
  external_lookup_available: false;
  evidence_use: "operator_confirmed_terms_only";
}

export type SearchRankingProductFactType =
  | "product_type"
  | "construction"
  | "material"
  | "function"
  | "packaging"
  | "usage";

export interface SearchRankingProductFactRecord {
  id: number;
  productline_id: string;
  source_offer_id?: string;
  fact_type: SearchRankingProductFactType;
  fact_term: string;
  statement: string | null;
  status: "active" | "revoked" | "superseded";
  source_type: "manual_confirmation";
  source_analysis_id: number | null;
  source_title: string;
  source_image_url: string;
  current_image_matches: boolean;
  applied_to_current_image: boolean;
  needs_image_reconfirmation: boolean;
  evidence: Record<string, unknown>;
  confirmed_by_username: string;
  confirmed_by_display_name: string;
  confirmed_at: string;
  revoked_by_username: string | null;
  revoked_by_display_name: string | null;
  revoked_at: string | null;
  revoke_reason: string | null;
}

export interface SearchRankingProductFactProfile {
  applied_terms: string[];
  active_count: number;
  applied_count: number;
  needs_image_reconfirmation_count: number;
  archive_count: number;
  requires_current_image_match: true;
  source_policy: "manual_confirmation_only";
  facts: SearchRankingProductFactRecord[];
}

export type SearchRankingDecisionParameterType =
  | "power"
  | "voltage"
  | "current"
  | "capacity"
  | "size"
  | "dimensions"
  | "weight"
  | "quantity"
  | "resolution"
  | "protection_rating"
  | "specification";

export interface SearchRankingDecisionParameterCandidate {
  parameter_key: string;
  parameter_value: string;
  parameter_type: SearchRankingDecisionParameterType;
  title_order: number;
  system_recommendation: "decision_parameter" | "ordinary_specification";
  system_reason: string;
  manual_decision: boolean | null;
}

export interface SearchRankingDecisionParameterConfirmationRecord {
  id: number;
  productline_id: string;
  source_offer_id: string;
  source_analysis_id: number | null;
  source_title: string;
  current_title_matches: boolean;
  decisions: Array<Omit<SearchRankingDecisionParameterCandidate, "manual_decision"> & {
    is_decision_parameter: boolean;
  }>;
  policy_version: string;
  confirmed_by_username: string;
  confirmed_by_display_name: string;
  confirmed_at: string;
}

export interface SearchRankingDecisionParameterProfile {
  policy_version: string;
  source_policy: "current_seller_title_human_confirmation";
  fronting_requires_search_validation: true;
  max_positive_decisions: number;
  current_title: string;
  current_title_confirmed: boolean;
  requires_confirmation: boolean;
  candidate_count: number;
  decision_parameter_count: number;
  ordinary_parameter_count: number;
  unconfirmed_count: number;
  candidates: SearchRankingDecisionParameterCandidate[];
  applied_decision_parameters: SearchRankingDecisionParameterCandidate[];
  applied_decision_values: string[];
  latest_confirmation: SearchRankingDecisionParameterConfirmationRecord | null;
  archive: SearchRankingDecisionParameterConfirmationRecord[];
}

export interface SearchRootExpansionLibraryPayload {
  policy: {
    scope: "shared_across_all_store_analyses";
    ttl_hours: number;
    refresh_mode: "refresh_on_first_hit_after_ttl";
    scheduled_refresh: false;
    passive_read_triggers_external_request: false;
    root_expansion_rank_is_search_volume: false;
    legacy_partial_input_states_hidden: true;
    phrase_roots_supported: true;
    raw_expansions_require_product_context_selection: true;
    note: string;
  };
  summary: {
    root_count: number;
    stale_root_count: number;
    matching_root_count: number;
    matching_expansion_count: number;
    legacy_partial_input_state_count: number;
  };
  roots: Array<{
    root: string;
    expansions: Array<{ phrase: string; rank: number }>;
    captured_at: string;
    age_hours: number;
    stale: boolean;
    last_hit_at: string;
    system_input_hit_count: number;
    refresh_count: number;
    last_refresh_status: "success" | "failed";
    last_error: string | null;
  }>;
}

export type SearchAutocompleteLibraryPayload = SearchRootExpansionLibraryPayload;

export type SearchRankingRootSource =
  | "human_confirmed_product_fact"
  | "image_title_same_product_lexicon"
  | "image_title_first_instinct"
  | "title_word_root"
  | "result_page_learning"
  | "image_title_need_state"
  | "title_cross_check";

export interface SearchRankingStatus {
  configured: boolean;
  provider: string;
  provider_label: string;
  primary_model: string;
  fallback_provider: string | null;
  fallback_provider_label: string | null;
  fallback_model: string | null;
  configured_provider_count: number;
  pricing_snapshot_date: string;
  pricing_mode: "api_unit_price";
  model_policy: {
    transport: "openai_compatible_https";
    model_fallback_allowed: boolean;
    codex_cli_integration_retained: true;
    codex_cli_execution_enabled: false;
  };
  max_pages: number;
  max_keywords: number;
  root_expansion_input_limit: number;
  root_expansion_followup_root_limit: number;
  root_expansion_phrase_roots_enabled: true;
  root_expansion_selection_policy:
    "same_product_identity_or_structured_adjacent_product_family";
  root_expansion_raw_suggestions_are_selected: false;
  root_source_priority: SearchRankingRootSource[];
  model_market_context: "South Africa";
  model_language_variant: "South African English";
  model_shopper_context: "South African local customer habits";
  model_localization_policy_version: string;
  model_localization_scope: "all_model_generated_text_fields";
  model_localization_is_measured_demand: false;
  search_query_attempt_limit: number;
  public_request_min_interval_seconds: number;
  public_request_jitter_seconds: number;
  public_request_retry_policy: "no_automatic_retry_for_search_endpoints";
  model_direct_query_policy: {
    min_words: 2;
    max_words: 4;
    preferred_max_words: 3;
    min_preferred_count: 4;
    source_priority: ["same_product_lexicon", "fusion_keywords"];
  };
  query_source_targets: {
    model_south_african_direct: number;
    same_product_lexicon_first: true;
    takealot_root_expansion: number;
    seller_title_complete_phrase_max: number;
    root_related_core_total: number;
    adjacent_opportunity: number;
    adaptive_recovery: number;
  };
  operation_scope: "manual_single_offer_or_confirmed_serial_batch";
  offer_max_age_hours: number;
  image_max_dimension: number;
  organic_page_size: number;
  columns_per_row: number;
  core_first_page_threshold: number;
  core_same_demand_competitor_ratio_floor: number;
  core_same_demand_competitor_min_results: number;
  core_min_platform_results: number;
  platform_result_count_is_search_volume: false;
  platform_result_count_role: "core_keyword_supply_breadth_gate";
  semantic_relation_grades: ["S", "A", "C/I"];
  semantic_relation_source_priority_decides_grade: false;
  semantic_adjacent_ratio_floor: number;
  semantic_supported_ratio_floor: number;
  semantic_adjacent_min_results: number;
  opportunity_max_direct_competitors: number;
  opportunity_max_organic_rank: number;
  position_scope: "organic_results_excluding_sponsored";
  passive_reads_are_local_only: boolean;
  product_fact_manual_confirmation_available: true;
  product_fact_profile_requires_current_image: true;
  autocomplete_cache_shared_across_stores: true;
  autocomplete_cache_ttl_hours: number;
  autocomplete_cache_refresh_mode: "refresh_on_first_hit_after_ttl";
  root_expansion_rank_is_search_volume: false;
  product_fact_confirmation_mode: "manual_only";
  decision_parameter_confirmation_mode: "manual_per_title";
  decision_parameter_max_candidates: number;
  decision_parameter_max_positive: number;
  title_score_version: "evidence-title-v2";
  title_score_scope: "current_title_text_against_frozen_product_and_query_evidence";
  title_score_excludes_search_performance: true;
}

export interface SearchRankingAnalysisSummary {
  id: number;
  status: "running" | "completed" | "failed";
  source_offer_id: string;
  source_title: string;
  provider: string;
  model: string;
  confidence: number | null;
  vision_reused: boolean;
  created_at: string;
  completed_at: string | null;
  error: string | null;
  failure_audit?: {
    stage?: string;
    summary?: string;
    validation_errors?: Array<{
      path: string;
      type: string;
      message: string;
    }>;
    normalization?: Record<string, unknown>;
  } | null;
  vision_stage_completed: boolean;
  usage: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  };
  estimated_cost_cny: number | null;
  title_validation_status: string | null;
  title_score_value?: number | null;
  title_score_band?: SearchRankingTitleScoreBand | null;
  title_score_evidence_coverage?: number | null;
  title_score_current_title_match?: boolean;
  identity_difference_level?: "aligned" | "moderate" | "high" | null;
  identity_large_difference?: boolean;
  manual_fact_required?: boolean;
  manual_fact_reason?: string | null;
  variant_projection_applied?: boolean;
  variant_parameters?: SearchRankingVariantParameter[];
}

export type SearchRankingVariantParameterType =
  | SearchRankingDecisionParameterType
  | "colour"
  | "variant_value";

export interface SearchRankingVariantParameter {
  value: string;
  parameter_type: SearchRankingVariantParameterType;
  source: "seller_offer_title_difference";
  visually_verified: false;
}

export interface SearchRankingVariantFamilyVariant extends ProductMasterIdentity {
  offer_id: string;
  productline_id: string | null;
  sku: string | null;
  title: string;
  image_url: string | null;
  available_stock: number;
  parameters: SearchRankingVariantParameter[];
}

export interface SearchRankingVariantFamily {
  productline_id: string | null;
  representative_offer_id: string;
  representative_title: string;
  shared_title: string;
  shared_title_source:
    | "all_variant_titles"
    | "representative_fallback_no_common_sequence";
  variant_count: number;
  distinct_image_count: number;
  image_evidence_scope: "representative_offer_only";
  variant_parameter_source: "current_seller_offer_titles";
  variant_parameters_visually_verified: false;
  variants: SearchRankingVariantFamilyVariant[];
}

export interface SearchRankingProduct extends ProductMasterIdentity {
  offer_id: string;
  productline_id: string | null;
  sku: string | null;
  title: string | null;
  image_url: string | null;
  offer_status: string | null;
  available_stock: number;
  takealot_available_stock: number | null;
  seller_available_stock: number | null;
  captured_at: string;
  snapshot_age_hours: number;
  ownership_source: "authenticated_store_seller_offers";
  analyzable: boolean;
  shared_family_title?: string | null;
  family_representative_offer_id?: string | null;
  variant_count?: number;
  variant_parameters?: SearchRankingVariantParameter[];
  variant_parameter_source?: "current_seller_offer_titles" | null;
  variant_parameters_visually_verified?: false;
  latest_analysis: SearchRankingAnalysisSummary | null;
}

export interface SearchRankingFirstPageResultClassification {
  organic_position: number;
  plid: string;
  title: string;
  subtitle: string;
  url: string;
  image_url: string;
  classification:
    | "direct_same_product"
    | "same_demand_competitor"
    | "adjacent_or_ambiguous"
    | "unrelated";
  is_direct_competitor: boolean;
  is_same_demand_competitor?: boolean;
  is_core_competitor?: boolean;
  is_target: boolean;
  reason:
    | "target_product"
    | "source_title_identity_signature"
    | "same_product_identity_with_different_form"
    | "same_demand_product_family"
    | "same_demand_product_family_in_subtitle"
    | "conflicting_product_family_in_title"
    | "ordered_same_product_name_or_alias"
    | "identity_tokens_scattered_not_direct_proof"
    | "conflicting_product_family_in_subtitle"
    | "no_complete_same_product_identity";
  matched_identity_terms: string[];
  matched_loose_identity_terms: string[];
  matched_exclusion_terms: string[];
  matched_subtitle_exclusion_terms: string[];
  matched_source_title_signatures: string[];
  matched_same_demand_terms?: string[];
  matched_subtitle_same_demand_terms?: string[];
}

export interface SearchRankingKeywordResult {
  id: number;
  keyword: string;
  candidate_order: number;
  relevance_status:
    | "accepted"
    | "opportunity"
    | "comparison_resample"
    | "rejected_irrelevant"
    | "model_low_confidence";
  relevance_score: number;
  validation_evidence: {
    candidate_rationale?: string;
    validation_terms?: string[];
    top_result_titles?: string[];
    matched_result_titles?: string[];
    matched_top_results?: number;
    evaluated_top_results?: number;
    matched_first_page_results?: number;
    evaluated_first_page_results?: number;
    first_page_same_type_ratio?: number;
    semantic_relation_grade?: "S" | "A" | "C/I";
    semantic_relation_label?:
      | "same_product_or_direct_alias"
      | "core_query_with_same_demand_competitor_density"
      | "adjacent_demand_alternative"
      | "complementary_or_irrelevant_rejected";
    semantic_relation_decision?: string;
    semantic_relation_source_priority_decides_grade?: false;
    semantic_relation_requires_page_majority_for_s?: boolean;
    semantic_relation_requires_demand_competitor_density_for_s?: boolean;
    semantic_relation_current_title_alias?: string | null;
    semantic_relation_query_identity_supported?: boolean;
    semantic_relation_query_same_product_terms?: string[];
    semantic_relation_query_strict_same_product_terms?: string[];
    semantic_relation_query_core_identity_terms?: string[];
    semantic_relation_query_core_only_candidate?: boolean;
    semantic_relation_query_matches_explicit_opportunity_phrase?: boolean;
    semantic_relation_same_product_terms?: string[];
    semantic_relation_same_demand_product_terms?: string[];
    semantic_relation_buyer_jobs?: string[];
    semantic_relation_adjacent_roots?: string[];
    semantic_relation_alternative_product_terms?: string[];
    semantic_relation_excluded_product_terms?: string[];
    semantic_relation_same_product_result_count?: number;
    semantic_relation_same_demand_result_count?: number;
    semantic_relation_adjacent_result_count?: number;
    semantic_relation_rejected_result_count?: number;
    semantic_relation_evaluated_result_count?: number;
    semantic_relation_same_product_ratio?: number;
    semantic_relation_same_demand_ratio?: number;
    semantic_relation_adjacent_ratio?: number;
    semantic_relation_supported_ratio?: number;
    semantic_relation_core_competitor_result_count?: number;
    semantic_relation_core_competitor_ratio?: number;
    semantic_relation_core_density_qualified?: boolean;
    semantic_relation_core_page_qualified?: boolean;
    semantic_relation_core_demand_ratio_floor?: number;
    semantic_relation_core_demand_min_results?: number;
    semantic_relation_platform_supply_evidence_available?: boolean;
    semantic_relation_platform_supply_qualified?: boolean;
    semantic_relation_platform_total_num_found?: number | null;
    semantic_relation_core_min_platform_results?: number;
    semantic_relation_adjacent_page_qualified?: boolean;
    semantic_relation_adjacent_ratio_floor?: number;
    semantic_relation_supported_ratio_floor?: number;
    semantic_relation_min_adjacent_results?: number;
    semantic_relation_same_product_result_titles?: string[];
    semantic_relation_adjacent_result_titles?: string[];
    semantic_relation_evidence_scope?:
      | "first_page_organic_result_titles"
      | "first_page_organic_result_title_subtitle_and_product_metadata";
    first_page_result_classifications?: SearchRankingFirstPageResultClassification[];
    source_title_identity_signatures?: string[];
    semantic_relation_uses_per_result_image_or_category?: false;
    semantic_relation_limitations?: string;
    page_validation_status?: "completed" | "not_run";
    first_page_majority?: boolean;
    first_page_core_competitor_density_qualified?: boolean;
    direct_competitor_count_first_page?: number;
    same_demand_competitor_count_first_page?: number;
    core_competitor_count_first_page?: number;
    core_competitor_count_excluding_target_first_page?: number;
    direct_competitor_count_excluding_target_first_page?: number;
    target_on_first_page?: boolean;
    target_counted_as_direct_competitor?: boolean;
    core_threshold?: number;
    core_demand_ratio_floor?: number;
    core_demand_min_results?: number;
    core_min_platform_results?: number;
    platform_result_count_is_search_volume?: false;
    platform_result_count_role?: "core_keyword_supply_breadth_gate";
    opportunity_threshold?: number;
    opportunity_max_direct_competitors?: number;
    opportunity_max_organic_rank?: number;
    opportunity_candidate?: boolean;
    blue_ocean_candidate?: boolean;
    blue_ocean_platform_expansion_observed?: boolean;
    blue_ocean_semantic_relation_grade?: "S" | "A" | "C/I" | null;
    blue_ocean_qualified?: boolean;
    blue_ocean_rejection_reasons?: string[];
    opportunity_claims_safe?: boolean;
    opportunity_qualified?: boolean;
    opportunity_rejection_reasons?: string[];
    stored_relevance_status?: SearchRankingKeywordResult["relevance_status"];
    effective_relevance_status?: SearchRankingKeywordResult["relevance_status"];
    candidate_source?:
      | "image_precise"
      | "image_title_fused_precise"
      | "same_product_lexicon"
      | "takealot_root_expansion"
      | "takealot_autocomplete"
      | "seller_title_complete_phrase"
      | "comparison_resample"
      | "title_verified_parameter";
    query_source_channel?:
      | "same_product_lexicon_direct"
      | "model_south_african_direct"
      | "takealot_root_expansion"
      | "takealot_autocomplete_path"
      | "seller_title_complete_phrase"
      | "comparison_resample"
      | "title_verified_parameter"
      | "human_confirmed_decision_parameter"
      | "unknown";
    query_source_channels?: Array<
      | "same_product_lexicon_direct"
      | "model_south_african_direct"
      | "takealot_root_expansion"
      | "takealot_autocomplete_path"
      | "seller_title_complete_phrase"
      | "comparison_resample"
      | "title_verified_parameter"
      | "human_confirmed_decision_parameter"
    >;
    intended_strategy?: "core" | "opportunity" | "comparison";
    intended_strategies?: Array<"core" | "opportunity" | "comparison">;
    effective_strategy?:
      | "core"
      | "opportunity"
      | "comparison_resample"
      | "rejected_irrelevant";
    comparison_baseline_rank?: number | null;
    comparison_role?: string | null;
    comparison_strategy?: SearchRankingTitleStrategyKey | string | null;
    autocomplete_seed?: string | null;
    autocomplete_seed_source?:
      | "image_shopper_root"
      | "image_need_state"
      | "image_only_model"
      | "image_title_first_instinct"
      | "image_title_same_product_lexicon"
      | "image_title_need_state"
      | "image_title_fusion_model"
      | "human_confirmed_product_fact"
      | "title_word_root"
      | "title_cross_check"
      | "title_decision_parameter"
      | "human_confirmed_decision_parameter"
      | "result_page_learning"
      | "previous_analysis_baseline"
      | null;
    autocomplete_rank?: number | null;
    root_expansion_root?: string | null;
    root_expansion_source?: string | null;
    root_expansion_sources?: SearchRankingRootSource[];
    root_expansion_rank?: number | null;
    root_expansion_origin_phrase?: string | null;
    root_expansion_rank_is_search_volume?: false;
    autocomplete_endpoint?: string | null;
    autocomplete_is_search_volume?: boolean;
    autocomplete_cache_status?:
      | "fresh_hit"
      | "miss_refreshed"
      | "stale_refreshed"
      | "not_configured"
      | "not_recorded"
      | null;
    autocomplete_observed_at?: string | null;
    autocomplete_cache_age_hours?: number | null;
    autocomplete_cache_ttl_hours?: number | null;
    autocomplete_shared_across_stores?: boolean | null;
    demand_signal_note?: string;
    same_type_validation_method?:
      | "canonicalized_title_token_subset"
      | "canonicalized_title_token_subset_with_controlled_product_aliases"
      | "semantic_alias_token_subset_with_retarget_rejection"
      | "ordered_identity_phrase_with_exclusion_and_title_signature_audit"
      | "exact_identity_and_same_demand_family_page_audit";
    same_type_validation_controlled_aliases?: string[];
    same_type_validation_term_source?:
      | "image_primary_physical_form"
      | "query_matched_confirmed_product_type"
      | "image_title_fused_same_product_terms"
      | "semantic_verified_same_product_terms";
    same_type_validation_uses_multimodal_per_result?: false;
    same_type_validation_requires_contiguous_phrase?: boolean;
    same_type_validation_limitations?: string;
    journey_type?:
      | "same_product_lexicon_direct"
      | "concise_direct"
      | "known_long_tail"
      | "platform_root_expansion"
      | "human_confirmed_fact_root_expansion"
      | "same_product_lexicon_root_expansion"
      | "title_cross_check_root_expansion"
      | "model_fusion_root_expansion"
      | "title_root_expansion"
      | "title_complete_phrase_direct"
      | "result_page_root_expansion"
      | "first_instinct_autocomplete"
      | "autocomplete_backtrack"
      | "switched_instinct_root"
      | "result_page_learning"
      | "title_cross_check"
      | "title_decision_parameter"
      | "human_confirmed_decision_parameter"
      | "adjacent_opportunity"
      | null;
    journey_root?: string | null;
    journey_path?: string[];
    journey_types?: string[];
    journey_roots?: string[];
    journey_paths?: string[][];
    journey_depth?: number;
    journey_parent_query?: string | null;
    adaptive_recovery?: boolean;
    adaptive_recovery_source?:
      | "result_page_learning"
      | "second_best_root_expansion"
      | "second_best_autocomplete"
      | null;
    captured_request_endpoint?: string;
    captured_request_qsearch?: string;
    captured_request_matches_keyword?: boolean;
    api_version?: string | null;
    reason?: string;
  };
  total_num_found: number | null;
  pages_scanned: number;
  found: boolean;
  page_number: number | null;
  page_rank: number | null;
  organic_rank: number | null;
  row_number: number | null;
  column_number: number | null;
  columns_per_row: number;
  target_url: string | null;
  search_url: string;
  observed_at: string;
}

export type SearchRankingTitleStrategyKey =
  | "contiguous_core"
  | "hot_term_coverage"
  | "adjacent_opportunity";

export interface SearchRankingTitleStrategy {
  strategy: SearchRankingTitleStrategyKey;
  label: string;
  title: string | null;
  available: boolean;
  explanation: string;
  evidence_keywords: string[];
  evidence?: Record<string, unknown>;
}

export type SearchRankingTitleScoreBand =
  | "strong"
  | "solid"
  | "needs_improvement"
  | "weak"
  | "insufficient_evidence";

export interface SearchRankingTitleScoreComponent {
  key: string;
  label: string;
  weight: number;
  available: boolean;
  score: number | null;
  max_points: number;
  summary: string;
  evidence: Array<Record<string, unknown>>;
}

export interface SearchRankingTitleScore {
  score: number;
  band: SearchRankingTitleScoreBand;
  label: string;
  evidence_coverage: number;
  available_points: number;
  earned_points: number;
  current_title: string;
  current_title_match: boolean;
  components: SearchRankingTitleScoreComponent[];
  limitations: string[];
  scoring_version: "evidence-title-v2";
  score_scope: "current_title_text_against_frozen_product_and_query_evidence";
  title_quality_only: true;
  non_scoring_signals: Array<{
    key: string;
    label: string;
    reason: string;
  }>;
  compatibility_projection?: {
    source_version: string;
    persisted_payload_changed: false;
  };
}

export type SearchRankingSameProductLexiconSource =
  | "human_confirmed_product_fact"
  | "seller_title_identity_phrase"
  | "fusion_product_type_terms"
  | "fusion_same_product_aliases"
  | "historical_profile_term";

export interface SearchRankingSameProductLexicon {
  policy_version:
    | "same-product-lexicon-v1"
    | "same-product-lexicon-v2"
    | "historical-profile-projection";
  selection_policy: string;
  search_use: "priority_direct_query_and_complete_root_expansion";
  direct_query_limit: number;
  complete_root_expansion_limit?: number;
  entries: Array<{
    term: string;
    sources: SearchRankingSameProductLexiconSource[];
    word_count: number;
    direct_query_eligible: true;
  }>;
  excluded: Array<{
    term: string;
    source: SearchRankingSameProductLexiconSource | string;
    word_count: number;
    reason: "outside_2_to_4_words" | string;
  }>;
}

export interface SearchRankingAnalysis extends SearchRankingAnalysisSummary {
  product_name: string | null;
  category: string | null;
  profile: {
    product_type_terms?: string[];
    same_product_aliases?: string[];
    same_demand_product_terms?: string[];
    same_product_lexicon?: SearchRankingSameProductLexicon;
    distinctive_terms?: string[];
    exclusions?: string[];
    title_strategies?: SearchRankingTitleStrategy[];
    opportunity_title_suggestion?: string | null;
    opportunity_title_reason?: string | null;
  };
  recognition?: {
    basis?:
      | "image_only_then_title_cross_check"
      | "isolated_image_then_title_cross_check_then_image_title_fusion";
    visual_stage_received_source_title?: false;
    fusion_stage_received_source_title?: true;
    model_received_source_title?: boolean;
    model_received_sku?: boolean;
    original_model_product_name?: string;
    product_name_adjusted?: boolean;
    removed_unconfirmed_identity_terms?: string[];
    source_title_similarity?: number;
    title_reference_terms?: string[];
    title_root_expansions?: string[];
    title_identity_support?: boolean;
    title_identity_supported_terms?: string[];
    title_identity_matches?: Array<{
      term: string;
      identity_supported: boolean;
      matched_identity_anchor: string | null;
      identity_match_rule: string;
      model_product_name_supported: boolean;
    }>;
    title_identity_decision_rule?:
      "product_subject_or_controlled_generic_form_not_modifier_overlap";
    confirmed_fact_reference_terms?: string[];
    confirmed_identity_fact_terms?: string[];
    confirmed_identity_fact_supported_terms?: string[];
    confirmed_identity_fact_matches?: Array<{
      term: string;
      similarity: number;
      matched_tokens: string[];
      matched_identity_anchors: string[];
      rejected_modifier_overlap: string[];
      identity_supported: boolean;
      identity_match_rule: string;
    }>;
    confirmed_identity_fact_similarity?: number;
    confirmed_identity_fact_similarity_decides_support?: false;
    confirmed_identity_fact_similarity_floor?: number;
    confirmed_identity_fact_decision_rule?:
      "product_subject_or_alias_not_modifier_overlap";
    confirmed_identity_fact_support?: boolean;
    confirmed_fact_resolved_title_conflict?: boolean;
    title_identity_conflict?: boolean;
    provider_identity_reference_included_confirmed_facts?: boolean;
    identity_deviation_branch?:
      | "title_consistent"
      | "confirmed_fact_support_continue"
      | "unresolved_conflict_stop"
      | "moderate_difference_warning"
      | "large_difference_warning";
    title_reference_role?: "post_recognition_cross_check_only";
    cross_validation_isolated?: true;
    cross_validation_completed_before_fusion_generation?: true;
    identity_difference_level?: "aligned" | "moderate" | "high";
    identity_large_difference?: boolean;
    identity_difference_warning?: string | null;
    manual_fact_requested_by_fusion_model?: boolean;
    manual_fact_required?: boolean;
    manual_fact_resolved_by_confirmation?: boolean;
    manual_fact_reason?: string | null;
    missing_facts?: string[];
    manual_fact_confirmation_optional?: true;
    batch_action?: "skip_without_retry" | "continue";
    image_evidence_scope?: "representative_offer_only";
    current_image_matches_representative?: boolean;
    variant_parameter_source?: "current_seller_offer_titles";
    variant_parameters_visually_verified?: false;
    family_variant_count?: number;
  };
  autocomplete_checks?: Array<{
    seed: string;
    seed_source?: string;
    shopper_root?: string;
    input_state?: string;
    journey_path?: string[];
    journey_type?: string;
    journey_depth?: number;
    status: "observed" | "unavailable" | "reused_observed";
    suggestions?: string[];
    parent_query?: string;
    cache_status?:
      | "fresh_hit"
      | "miss_refreshed"
      | "stale_refreshed"
      | "not_configured"
      | "not_recorded";
    captured_at?: string;
    age_hours?: number;
    ttl_hours?: number;
    refresh_policy?: "refresh_on_first_hit_after_ttl";
    shared_across_stores?: boolean;
    input_hit_count?: number;
    refresh_count?: number;
  }>;
  root_expansion_checks?: Array<{
    root?: string;
    seed?: string;
    input_state?: string;
    shopper_root?: string;
    root_source?: string;
    seed_sources?: SearchRankingRootSource[];
    origin_phrases?: string[];
    input_kind: "complete_root_expansion";
    status: "observed" | "unavailable" | "reused_observed";
    journey_path?: string[];
    journey_type?: string;
    journey_depth?: number;
    parent_root?: string | null;
    raw_suggestions_are_selected?: false;
    selection_policy?: "same_product_identity_or_structured_adjacent_product_family";
    eligible_expansion_count?: number;
    rejected_expansion_count?: number;
    related_but_too_long_count?: number;
    direct_query_fallback_selected?: boolean;
    direct_query_fallback_reason?:
      | "platform_returned_no_suggestions"
      | "platform_returned_no_relevant_suggestions"
      | "platform_returned_no_concise_relevant_suggestions";
    expansions?: Array<{
      phrase: string;
      rank: number;
      relevance_status?: "eligible" | "rejected_irrelevant";
      relation?: "same_product" | "adjacent_demand" | "irrelevant";
      reason?: string;
      matched_terms?: string[];
      query_word_count?: number;
      query_length_status?: "eligible" | "rejected_too_long";
      used_as_followup_root?: boolean;
    }>;
    cache_status?:
      | "fresh_hit"
      | "miss_refreshed"
      | "stale_refreshed"
      | "not_configured"
      | "not_recorded";
    captured_at?: string;
    age_hours?: number;
    ttl_hours?: number;
  }>;
  shopper_journey?: {
    mode?: "manual_single_offer_one_click";
    root_expansion_input_limit?: number;
    root_expansion_followup_root_limit?: number;
    root_expansion_phrase_roots_enabled?: true;
    root_expansion_selection_policy?:
      "same_product_identity_or_structured_adjacent_product_family";
    root_expansion_raw_suggestions_are_selected?: false;
    root_source_priority?: SearchRankingRootSource[];
    model_localization?: {
      market_context: "South Africa";
      language_variant: "South African English";
      shopper_context: "South African local customer habits";
      policy_version: string;
      scope: "all_model_generated_text_fields";
      is_measured_demand: false;
    };
    search_query_attempt_limit?: number;
    public_request_min_interval_seconds?: number;
    public_request_jitter_seconds?: number;
    model_direct_query_policy?: {
      min_words: 2;
      max_words: 4;
      preferred_max_words: 3;
      min_preferred_count: 4;
      source_priority: ["same_product_lexicon", "fusion_keywords"];
    };
    query_source_targets?: {
      model_south_african_direct: number;
      same_product_lexicon_first: true;
      takealot_root_expansion: number;
      seller_title_complete_phrase_max: number;
      root_related_core_total: number;
      adjacent_opportunity: number;
      adaptive_recovery: number;
    };
    same_product_lexicon?: {
      policy_version: "same-product-lexicon-v1";
      entry_count: number;
      direct_query_priority: true;
      complete_root_expansion_enabled: true;
      complete_root_expansion_limit: number;
    };
    adaptive_policy?: {
      base_query_target: number;
      recovery_query_target: number;
      valid_platform_root_target: number;
      recovery_priority: Array<
        "result_page_learning" | "second_best_root_expansion"
      >;
    };
    valid_platform_root_target?: number;
    valid_platform_root_count?: number;
    valid_platform_roots?: string[];
    adaptive_recovery_used?: boolean;
    adaptive_recovery_query?: string | null;
    adaptive_recovery_source?:
      | "result_page_learning"
      | "second_best_root_expansion"
      | "second_best_autocomplete"
      | null;
    adaptive_recovery_skipped_reason?: string | null;
    public_request_count?: number;
    skipped_for_manual_fact?: boolean;
    manual_fact_reason?: string | null;
    missing_facts?: string[];
    steps?: Array<{
      query: string;
      query_source_channel?: string;
      journey_type?: string | null;
      shopper_root?: string | null;
      path?: string[];
      parent_query?: string | null;
      result?: SearchRankingKeywordResult["relevance_status"];
      first_page_same_type_ratio?: number;
      target_found?: boolean;
      pages_scanned?: number;
      adaptive_recovery?: boolean;
      adaptive_recovery_source?:
        | "result_page_learning"
        | "second_best_root_expansion"
        | "second_best_autocomplete"
        | null;
    }>;
  };
  provider_attempts?: Array<{
    provider: string;
    status:
      | "accepted"
      | "request_or_schema_failed"
      | "identity_conflict"
      | "cached_identity_conflict"
      | "weekly_quota_stopped";
    reason?: string;
    source_title_similarity?: number;
    title_identity_support?: boolean;
    title_identity_supported_terms?: string[];
    usage?: {
      input_tokens?: number;
      output_tokens?: number;
      total_tokens?: number;
    };
    estimated_cost_cny?: number | null;
  }>;
  title_score?: SearchRankingTitleScore | null;
  visual_profile?: {
    market_context?: "South Africa";
    language_variant?: "South African English";
    shopper_context?: "South African local customer habits";
    product_name?: string;
    category?: string;
    product_type_terms?: string[];
    distinctive_terms?: string[];
  };
  fusion_profile?: {
    market_context?: "South Africa";
    language_variant?: "South African English";
    shopper_context?: "South African local customer habits";
    product_name?: string;
    category?: string;
    product_type_terms?: string[];
    same_product_aliases?: string[];
    distinctive_terms?: string[];
    keywords?: Array<{ phrase: string; rationale: string }>;
    autocomplete_seeds?: Array<{ phrase: string; rationale: string }>;
    opportunity_seeds?: Array<{
      phrase: string;
      rationale: string;
      buyer_job?: string;
      alternative_product_terms?: string[];
      excluded_product_terms?: string[];
    }>;
  };
  product_fact_profile?: {
    applied_terms?: string[];
    facts?: SearchRankingProductFactRecord[];
    requires_current_image_match?: boolean;
    source_policy?: string;
  };
  product_fact_recommendation: SearchRankingProductFactRecommendation;
  variant_family?: SearchRankingVariantFamily;
  variant_projection?: {
    family_analysis_shared: true;
    applied: boolean;
    source_offer_id: string;
    current_offer_id: string;
    current_title: string;
    title_review_available: boolean;
    family_snapshot_current: boolean;
    decision_parameter_confirmation_current: boolean;
    variant_parameters: SearchRankingVariantParameter[];
    variant_parameter_source: "current_seller_offer_titles";
    variant_parameters_visually_verified: false;
    image_evidence_scope: "representative_offer_only";
    current_image_matches_representative: boolean;
  };
  usage: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  };
  estimated_cost_cny: number | null;
  title_suggestion: string | null;
  title_reason: string | null;
  opportunity_title_suggestion: string | null;
  opportunity_title_reason: string | null;
  title_strategies?: SearchRankingTitleStrategy[];
  title_validation: {
    status?: string;
    causality?: "observational_only";
    guarantee?: boolean;
    note?: string;
    matched_strategy?: SearchRankingTitleStrategyKey | string;
    matched_suggestion?: string;
    required_keywords?: string[];
    missing_baseline_keywords?: string[];
    missing_keywords?: string[];
    comparisons?: Array<{
      keyword: string;
      before_rank: number;
      after_rank: number;
      delta: number;
    }>;
    secondary_comparisons?: Array<{
      keyword: string;
      before_rank: number;
      after_rank: number;
      delta: number;
    }>;
  } | null;
  keywords: SearchRankingKeywordResult[];
}

export interface SearchRankingListPayload {
  status: SearchRankingStatus;
  store_scope?: OwnStoreScope;
  store_count?: number;
  eligibility: {
    source: "authenticated_store_seller_offers";
    rule: "current_offer_and_buyable_and_positive_available_stock_and_fresh";
    current_offer_count: number;
    eligible_count: number;
    excluded_count: number;
    excluded_reasons: Record<string, number>;
    latest_capture_at: string | null;
    max_age_hours: number;
  };
  items: SearchRankingProduct[];
}

export type SearchRankingBatchStatusValue =
  | "queued"
  | "running"
  | "pausing"
  | "paused"
  | "paused_after_error"
  | "stopping"
  | "stopped"
  | "stopped_quota_limit"
  | "interrupted"
  | "completed";

export interface SearchRankingBatchState {
  batch_id: string | null;
  status: SearchRankingBatchStatusValue | null;
  owned_by_current_user: boolean;
  details_available: boolean;
  message?: string;
  owner_username?: string;
  owner_display_name?: string;
  snapshot_id?: string;
  created_at?: string;
  started_at?: string | null;
  updated_at?: string;
  finished_at?: string | null;
  store_count?: number;
  target_count?: number;
  next_index?: number;
  processed_count?: number;
  completed_count?: number;
  skipped_count?: number;
  failed_count?: number;
  remaining_count?: number;
  current_target?: {
    index: number;
    store_code: string;
    store_name: string;
    offer_id: string;
    productline_id: string | null;
    title: string | null;
    variant_count: number;
    shared_family_title?: string | null;
    variant_parameters?: Array<{
      offer_id: string;
      parameters: SearchRankingVariantParameter[];
    }>;
  } | null;
  usage?: {
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    estimated_cost_cny: number;
    cost_accounting_complete: boolean;
  };
  store_progress?: Array<{
    code: string;
    display_name: string;
    target_count: number;
    completed_count: number;
    skipped_count: number;
    failed_count: number;
  }>;
  last_error?: string | null;
  deduplicated_pending_variant_count?: number;
  recent_results?: Array<{
    index: number;
    store_code: string;
    store_name: string;
    offer_id: string;
    productline_id: string | null;
    title: string | null;
    variant_count?: number;
    outcome: "completed" | "skipped" | "failed" | "quota_stopped";
    message: string | null;
    analysis_id: number | null;
    vision_reused: boolean;
    usage: {
      input_tokens: number;
      output_tokens: number;
      total_tokens: number;
    };
    estimated_cost_cny: number;
    cost_accounting_complete: boolean;
    finished_at: string;
  }>;
  strict_serial?: true;
  max_concurrency?: 1;
  automatic_retry?: false;
  can_pause?: boolean;
  can_resume?: boolean;
  can_retry_failed_target?: boolean;
  retry_failed_target?: {
    index: number;
    store_code: string;
    store_name: string;
    offer_id: string;
    productline_id: string | null;
    title: string | null;
    variant_count?: number;
    outcome: "failed";
    message: string | null;
  } | null;
  retry_remaining_count?: number | null;
  can_stop?: boolean;
  can_restart?: boolean;
}

export interface SearchRankingBatchPreviewPayload {
  policy: {
    scope: "all_accessible_active_connected_stores";
    target_scope: "one_representative_offer_per_store_productline_id";
    strict_serial: true;
    max_concurrency: 1;
    automatic_retry: false;
    pause_after_provider_or_network_error: true;
    reverse_image_search: false;
    requires_snapshot_confirmation: true;
    primary_provider: string;
    primary_model: string;
    fallback_provider: string | null;
    fallback_model: string | null;
    model_fallback_allowed: boolean;
    codex_cli_integration_retained: true;
    codex_cli_execution_enabled: false;
    public_request_min_interval_seconds: number;
    public_request_max_interval_seconds: number;
  };
  preview: {
    snapshot_id: string;
    generated_at: string;
    store_count: number;
    stores: Array<{
      code: string;
      display_name: string;
      current_offer_count: number;
      eligible_offer_count: number;
      eligible_count: number;
      variant_family_count: number;
      existing_vision_cache_hit_count: number;
      same_batch_vision_reuse_count: number;
      fresh_vision_count: number;
    }>;
    current_offer_count: number;
    eligible_offer_count: number;
    eligible_count: number;
    variant_family_count: number;
    existing_vision_cache_hit_count: number;
    same_batch_vision_reuse_count: number;
    fresh_vision_count: number;
    maximum_fresh_vision_count: number;
    estimated_usage: {
      historical_sample_count: number;
      input_tokens_per_fresh_image: number;
      output_tokens_per_fresh_image: number;
      total_tokens_per_fresh_image: number;
      input_tokens_total: number;
      output_tokens_total: number;
      total_tokens: number;
    };
    estimated_cost: {
      currency: "CNY";
      pricing_mode: "api_unit_price";
      cost_estimate_applicable: true;
      base_cny: number;
      typical_low_cny: number;
      typical_high_cny: number;
      conservative_upper_cny: number;
      primary_provider: string;
      primary_model: string;
      pricing_snapshot_date: string;
      input_price_cny_per_million: number;
      output_price_cny_per_million: number;
      fallback_may_add_cost: boolean;
    };
    estimated_duration: {
      historical_request_sample_count: number;
      public_requests_per_offer_median: number;
      average_interval_seconds: number;
      pacing_floor_hours: number;
      likely_min_hours: number;
      likely_max_hours: number;
      note: string;
    };
  };
  batch: SearchRankingBatchState | null;
}

export interface SearchRankingBatchStatusPayload {
  batch: SearchRankingBatchState | null;
}

export interface SearchRankingDetailPayload {
  status: SearchRankingStatus;
  product: SearchRankingProduct;
  variant_family?: SearchRankingVariantFamily;
  product_fact_profile: SearchRankingProductFactProfile;
  decision_parameter_profile: SearchRankingDecisionParameterProfile;
  analysis: SearchRankingAnalysis | null;
  latest_attempt: SearchRankingAnalysisSummary | null;
  history: SearchRankingAnalysisSummary[];
}

export type UserRole = "viewer" | "operator" | "selection" | "admin";

export type PermissionKey =
  | "store.view"
  | "logistics.manage"
  | "keyword_traffic.manage"
  | "search_ranking.run"
  | "competitors.view"
  | "competitors.collect"
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
  store_scope?: OwnStoreScope;
  store_count?: number;
  items: QuadrantItem[];
}

export interface FreshnessPayload {
  last_collection_at: string | null;
  latest_metric_date: string | null;
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

export interface TakealotShipmentItem extends StoreScopedRecord {
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

export interface LogisticsHighConfidenceCandidate extends StoreScopedRecord {
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

export interface LogisticsConfirmedLink extends StoreScopedRecord {
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
  store_scope?: OwnStoreScope;
  store_count?: number;
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
