import type {
  CollectResult,
  CompetitorDetail,
  CompetitorLinkHealthItem,
  CompetitorOverview,
  CompetitorStoreTargetPayload,
  CompetitorTargetAuditPayload,
  CompetitorTargetItem,
  ExportPayload,
  FreshnessPayload,
  LogisticsOverviewPayload,
  KeywordTrafficDetailPayload,
  KeywordTrafficListPayload,
  NftGeneration,
  NftInspection,
  AuthSession,
  AuthStatus,
  ManagedStore,
  ManagedUser,
  PermissionKey,
  UserRole,
  ProductDetailPayload,
  ProductsPayload,
  QuadrantPayload,
  RiskPayload,
  SummaryPayload,
  DailyReportExport,
  DailyReportPayload,
  DailyReportReminders,
  OwnStoreScope,
  PlatformWarehouseDraft,
  PlatformWarehousePayload,
} from "./types";
import { templatePermissions } from "./permissions";

let csrfToken = "";
let activeStoreCode = "current";

export class ApiRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiRequestError";
  }
}

export function setAuthSession(session: AuthSession | null) {
  csrfToken = session?.csrf_token ?? "";
}

export function setActiveStoreCode(storeCode: string | null | undefined) {
  activeStoreCode = storeCode?.trim().toLowerCase() || "current";
}

export function withStoreContext(url: string): string {
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}store_code=${encodeURIComponent(activeStoreCode)}`;
}

function normalizeAuthSession(session: AuthSession): AuthSession {
  const role = session?.user?.role;
  if (!role || !(role in templatePermissions)) {
    throw new ApiRequestError(
      "登录信息与当前页面版本不兼容，请重新加载；若仍失败，请联系管理员重启 ERP 服务",
      500,
    );
  }
  return {
    ...session,
    user: {
      ...session.user,
      permissions: Array.isArray(session.user.permissions)
        ? session.user.permissions
        : [...templatePermissions[role]],
      permissions_customized:
        typeof session.user.permissions_customized === "boolean"
          ? session.user.permissions_customized
          : false,
      all_stores:
        typeof session.user.all_stores === "boolean"
          ? session.user.all_stores
          : true,
      assigned_store_ids: Array.isArray(session.user.assigned_store_ids)
        ? session.user.assigned_store_ids
        : [],
      accessible_stores: Array.isArray(session.user.accessible_stores)
        ? session.user.accessible_stores
        : [],
    },
  };
}

async function request<T>(url: string, init?: RequestInit & { signal?: AbortSignal }): Promise<T> {
  const headers = new Headers(init?.headers);
  const method = (init?.method ?? "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  headers.set("X-Store-Code", activeStoreCode);
  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      headers,
      credentials: "same-origin",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiRequestError(
      "无法连接本机 ERP 服务，请确认服务正在运行",
      0,
    );
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (
      response.status === 401
      && !["/api/auth/login", "/api/auth/session"].includes(url)
    ) {
      window.dispatchEvent(new CustomEvent("erp-auth-expired"));
    }
    const message =
      typeof payload.detail === "string"
        ? payload.detail
        : `本机接口返回异常（HTTP ${response.status}）`;
    throw new ApiRequestError(message, response.status);
  }
  return payload as T;
}

export function fetchAuthStatus(): Promise<AuthStatus> {
  return request<AuthStatus>("/api/auth/status");
}

export async function fetchAuthSession(): Promise<AuthSession> {
  const session = normalizeAuthSession(
    await request<AuthSession>("/api/auth/session"),
  );
  setAuthSession(session);
  return session;
}

export async function login(username: string, password: string): Promise<AuthSession> {
  const session = normalizeAuthSession(
    await request<AuthSession>("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    }),
  );
  setAuthSession(session);
  return session;
}

export async function bootstrapAdmin(
  username: string,
  displayName: string,
  password: string,
): Promise<AuthSession> {
  const session = normalizeAuthSession(
    await request<AuthSession>("/api/auth/bootstrap", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username,
        display_name: displayName,
        password,
      }),
    }),
  );
  setAuthSession(session);
  return session;
}

export async function logout(): Promise<void> {
  await request<{ ok: boolean }>("/api/auth/logout", { method: "POST" });
  setAuthSession(null);
}

export async function fetchUsers(): Promise<ManagedUser[]> {
  const result = await request<{ items: ManagedUser[] }>("/api/auth/users");
  return result.items;
}

export async function fetchStores(): Promise<ManagedStore[]> {
  const result = await request<{ items: ManagedStore[] }>("/api/auth/stores");
  return result.items;
}

export async function createStore(input: {
  code: string;
  display_name: string;
}): Promise<ManagedStore> {
  const result = await request<{ store: ManagedStore }>("/api/auth/stores", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return result.store;
}

export async function updateStore(
  id: number,
  input: {
    display_name?: string;
    active?: boolean;
  },
): Promise<ManagedStore> {
  const result = await request<{ store: ManagedStore }>(`/api/auth/stores/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return result.store;
}

export async function createUser(input: {
  username: string;
  display_name: string;
  password: string;
  role: UserRole;
  permissions?: PermissionKey[];
  all_stores?: boolean;
  store_ids?: number[];
}): Promise<ManagedUser> {
  const result = await request<{ user: ManagedUser }>("/api/auth/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return result.user;
}

export async function updateUser(
  id: number,
  input: {
    display_name?: string;
    password?: string;
    role?: UserRole;
    permissions?: PermissionKey[];
    all_stores?: boolean;
    store_ids?: number[];
    active?: boolean;
  },
): Promise<ManagedUser> {
  const result = await request<{ user: ManagedUser }>(`/api/auth/users/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return result.user;
}

export function fetchCompetitors(
  startDate?: string,
  endDate?: string,
  ownStoreScope: OwnStoreScope = "current",
): Promise<CompetitorOverview> {
  const query = new URLSearchParams();
  if (startDate) query.set("start_date", startDate);
  if (endDate) query.set("end_date", endDate);
  query.set("own_store_scope", ownStoreScope);
  const suffix = query.size ? `?${query.toString()}` : "";
  return request<CompetitorOverview>(`/api/competitors${suffix}`);
}

export async function fetchCompetitorLinkHealth(): Promise<
  CompetitorLinkHealthItem[]
> {
  const result = await request<{ items: CompetitorLinkHealthItem[] }>(
    "/api/competitors/link-health",
  );
  return result.items;
}

export async function fetchCompetitorTargets(): Promise<CompetitorTargetItem[]> {
  const result = await request<{ items: CompetitorTargetItem[] }>(
    "/api/competitors/targets",
  );
  return result.items;
}

export async function fetchCompetitorStoreTargets(
  ownStoreScope: OwnStoreScope = "current",
): Promise<CompetitorStoreTargetPayload> {
  const query = new URLSearchParams({ own_store_scope: ownStoreScope });
  return request<CompetitorStoreTargetPayload>(
    `/api/competitors/store-targets?${query.toString()}`,
  );
}

export async function createCompetitorTarget(
  url: string,
): Promise<{
  item: CompetitorTargetItem | null;
  queued_to_active_batch: boolean;
  automatic_store_target: boolean;
  store_names: string[];
}> {
  return request("/api/competitors/targets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
}

export async function updateCompetitorTarget(
  plid: string,
  url: string,
): Promise<CompetitorTargetItem> {
  const result = await request<{ item: CompetitorTargetItem }>(
    `/api/competitors/targets/${encodeURIComponent(plid)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    },
  );
  return result.item;
}

export async function deleteCompetitorTarget(plid: string): Promise<void> {
  await request(`/api/competitors/targets/${encodeURIComponent(plid)}`, {
    method: "DELETE",
  });
}

export async function prioritizeCompetitorTarget(
  plid: string,
  source: "manual" | "manual_retry" = "manual",
): Promise<{ status: CompetitorBatchStatus; accepted: boolean }> {
  const result = await request<{
    ok: boolean;
    accepted: boolean;
    status: CompetitorBatchStatus;
  }>(`/api/competitors/targets/${encodeURIComponent(plid)}/prioritize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source }),
  });
  return { status: result.status, accepted: result.accepted };
}

export function fetchCompetitorTargetAudits(
  startDate?: string,
  endDate?: string,
  page = 1,
  pageSize = 20,
): Promise<CompetitorTargetAuditPayload> {
  const query = new URLSearchParams();
  if (startDate) query.set("start_date", startDate);
  if (endDate) query.set("end_date", endDate);
  query.set("page", String(page));
  query.set("page_size", String(pageSize));
  const suffix = query.size ? `?${query.toString()}` : "";
  return request<CompetitorTargetAuditPayload>(
    `/api/competitors/target-audits${suffix}`,
  );
}

export async function fetchCompetitorDetail(
  plid: string,
  startDate?: string,
  endDate?: string,
  ownStoreScope: OwnStoreScope = "current",
): Promise<CompetitorDetail> {
  const query = new URLSearchParams();
  if (startDate) query.set("start_date", startDate);
  if (endDate) query.set("end_date", endDate);
  query.set("own_store_scope", ownStoreScope);
  const suffix = query.size ? `?${query.toString()}` : "";
  return request<CompetitorDetail>(`/api/competitors/${plid}${suffix}`);
}

export interface CompetitorCollectionContext {
  batchId: string;
  clientId: string;
  requestId: string;
  itemIndex: number;
  totalItems: number;
  retryKind?: "stock" | "automatic";
  retryAttempt?: number;
}

export interface CompetitorBatchEvent {
  batchId: string;
  clientId: string;
  event:
    | "start"
    | "resume"
    | "auto_resume"
    | "progress"
    | "heartbeat"
    | "paused"
    | "manual_stop"
    | "completed";
  completed: number;
  total: number;
  pending: number;
  succeeded: number;
  failed: number;
  terminal: number;
  withStockProbe: boolean;
  visibleBrowser: boolean;
  reason?: string;
}

export interface CompetitorBatchStatus {
  active: boolean;
  batch_id: string | null;
  owner_username: string | null;
  owner_display_name: string | null;
  event: string;
  completed: number;
  total: number;
  pending: number;
  succeeded: number;
  failed: number;
  terminal: number;
  current_index: number | null;
  current_plid: string | null;
  current_request_id: string | null;
  current_stage: string | null;
  current_retry_kind: "stock" | "automatic" | null;
  current_retry_attempt: number | null;
  with_stock_probe: boolean;
  visible_browser: boolean;
  takeover_pending: boolean;
  reason: string;
  started_at: string | null;
  updated_at: string | null;
  queued_targets: Array<{
    plid: string;
    url: string;
    queued_at: string;
  }>;
  priority_targets: Array<{
    plid: string;
    url: string;
    requested_at: string;
    requested_by: string;
    source?: "manual" | "manual_retry";
  }>;
  prioritized_targets: Array<{
    plid: string;
    url: string;
    requested_at: string;
    requested_by: string;
    source: "manual" | "manual_retry" | "automatic";
  }>;
}

export async function collectCompetitor(
  url: string,
  withStockProbe: boolean,
  visibleBrowser: boolean,
  signal?: AbortSignal,
  context?: CompetitorCollectionContext,
): Promise<CollectResult> {
  return request<CollectResult>("/api/competitors/collect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      url,
      with_stock_probe: withStockProbe,
      visible_browser: visibleBrowser,
      batch_id: context?.batchId,
      client_id: context?.clientId,
      request_id: context?.requestId,
      item_index: context?.itemIndex,
      total_items: context?.totalItems,
      retry_kind: context?.retryKind,
      retry_attempt: context?.retryAttempt,
    }),
    signal,
  });
}

export async function logCompetitorBatchEvent(
  event: CompetitorBatchEvent,
): Promise<CompetitorBatchStatus> {
  const result = await request<{
    ok: boolean;
    status: CompetitorBatchStatus;
  }>("/api/competitors/batch-events", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      batch_id: event.batchId,
      client_id: event.clientId,
      event: event.event,
      completed: event.completed,
      total: event.total,
      pending: event.pending,
      succeeded: event.succeeded,
      failed: event.failed,
      terminal: event.terminal,
      with_stock_probe: event.withStockProbe,
      visible_browser: event.visibleBrowser,
      reason: event.reason ?? "",
    }),
  });
  return result.status;
}

export function fetchCompetitorBatchStatus(): Promise<CompetitorBatchStatus> {
  return request<CompetitorBatchStatus>("/api/competitors/batch-status");
}

export async function updateCompetitorBatchOptions(
  batchId: string,
  visibleBrowser: boolean,
): Promise<CompetitorBatchStatus> {
  const result = await request<{ ok: boolean; status: CompetitorBatchStatus }>(
    "/api/competitors/batch-options",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        batch_id: batchId,
        visible_browser: visibleBrowser,
      }),
    },
  );
  return result.status;
}

export function takeoverCompetitorBatch(
  batchId: string,
  clientId: string,
): Promise<{ ok: boolean; ready: boolean; status: CompetitorBatchStatus }> {
  return request<{ ok: boolean; ready: boolean; status: CompetitorBatchStatus }>(
    "/api/competitors/batch-takeover",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch_id: batchId, client_id: clientId }),
    },
  );
}

function query(asOf: string) {
  return `as_of=${encodeURIComponent(asOf)}`;
}

export async function fetchFreshness(): Promise<FreshnessPayload> {
  return request<FreshnessPayload>("/api/erp/freshness");
}

export async function fetchSummary(asOf: string): Promise<SummaryPayload> {
  return request<SummaryPayload>(`/api/erp/summary?${query(asOf)}`);
}

export async function fetchProducts(asOf: string): Promise<ProductsPayload> {
  return request<ProductsPayload>(`/api/erp/products?${query(asOf)}`);
}

export async function fetchProductDetail(
  offerId: string,
  asOf: string,
): Promise<ProductDetailPayload> {
  return request<ProductDetailPayload>(
    `/api/erp/products/${encodeURIComponent(offerId)}?${query(asOf)}`,
  );
}

export async function fetchQuadrants(
  asOf: string,
  percentile: number,
): Promise<QuadrantPayload> {
  return request<QuadrantPayload>(
    `/api/erp/quadrants?${query(asOf)}&percentile=${percentile}`,
  );
}

export async function fetchRisks(asOf: string): Promise<RiskPayload> {
  return request<RiskPayload>(`/api/erp/risks?${query(asOf)}`);
}

export function fetchKeywordTrafficProducts(
  asOf: string,
): Promise<KeywordTrafficListPayload> {
  return request<KeywordTrafficListPayload>(
    `/api/erp/keyword-traffic?${query(asOf)}`,
  );
}

export function fetchKeywordTrafficDetail(
  offerId: string,
  asOf: string,
  historyDays: number,
  comparisonDays: number,
): Promise<KeywordTrafficDetailPayload> {
  return request<KeywordTrafficDetailPayload>(
    `/api/erp/keyword-traffic/${encodeURIComponent(offerId)}`
      + `?${query(asOf)}&history_days=${historyDays}&comparison_days=${comparisonDays}`,
  );
}

export async function fetchLogisticsOverview(
  refresh = false,
): Promise<LogisticsOverviewPayload> {
  const suffix = refresh ? "?refresh=true" : "";
  return request<LogisticsOverviewPayload>(`/api/erp/logistics${suffix}`);
}

export function confirmLogisticsLink(
  w8OrderNo: string,
  takealotShipmentId: number,
): Promise<{ link: LogisticsOverviewPayload["matching"]["confirmed_links"][number] }> {
  return request("/api/erp/logistics/links", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      w8_order_no: w8OrderNo,
      takealot_shipment_id: takealotShipmentId,
    }),
  });
}

export function revokeLogisticsLink(
  linkId: number,
  note: string,
): Promise<{ link: LogisticsOverviewPayload["matching"]["confirmed_links"][number] }> {
  return request(`/api/erp/logistics/links/${linkId}/revoke`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note }),
  });
}

export function fetchPlatformWarehouse(): Promise<PlatformWarehousePayload> {
  return request<PlatformWarehousePayload>("/api/erp/platform-warehouse");
}

export interface PlatformWarehouseDirectCreateResult {
  state: "created" | "need_2fa";
  draft: PlatformWarehouseDraft;
  portal: PlatformWarehousePayload["portal"];
  otp_destination?: string | null;
}

export function createPlatformWarehouseDirect(input: {
  client_request_id: string;
  lines: Array<{
    offer_id: string;
    cpt_quantity: number;
    jhb_quantity: number;
    dbn_quantity: number;
  }>;
  note?: string;
}): Promise<PlatformWarehouseDirectCreateResult> {
  return request("/api/erp/platform-warehouse/create-direct", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function verifyPlatformWarehouseOtpAndCreate(
  draftId: number,
  otp: string,
): Promise<PlatformWarehouseDirectCreateResult> {
  return request(`/api/erp/platform-warehouse/drafts/${draftId}/verify-otp-and-create`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ otp }),
  });
}

export function logoutPlatformWarehousePortal(): Promise<{
  portal: PlatformWarehousePayload["portal"];
}> {
  return request("/api/erp/platform-warehouse/portal/logout", { method: "POST" });
}

export type PlatformWarehouseUpstreamAction = "confirm_po" | "confirm_shipped" | "archive";

export function preparePlatformWarehouseAction(
  shipmentId: number,
  action: PlatformWarehouseUpstreamAction,
): Promise<{
  action: PlatformWarehouseUpstreamAction;
  shipment_id: number;
  approval_token: string;
  expires_at: string;
  preview: Record<string, unknown> | null;
}> {
  return request(`/api/erp/platform-warehouse/shipments/${shipmentId}/prepare-action`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
}

export function executePlatformWarehouseAction(
  shipmentId: number,
  input: {
    action: PlatformWarehouseUpstreamAction;
    approval_token: string;
    confirmation_text: string;
    tracking_reference?: string;
    my_soh_decrease_warehouse_id?: number;
  },
): Promise<{ draft: PlatformWarehouseDraft }> {
  return request(`/api/erp/platform-warehouse/shipments/${shipmentId}/execute-action`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function confirmPlatformWarehousePo(
  draftId: number,
  input: { po_number: string; platform_shipment_id?: number; note?: string },
): Promise<{ draft: PlatformWarehouseDraft }> {
  return request(`/api/erp/platform-warehouse/drafts/${draftId}/confirm-po`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function confirmPlatformWarehouseShipped(
  draftId: number,
  input: { tracking_reference: string; note?: string },
): Promise<{ draft: PlatformWarehouseDraft }> {
  return request(`/api/erp/platform-warehouse/drafts/${draftId}/confirm-shipped`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function archivePlatformWarehouseDraft(
  draftId: number,
  note: string,
): Promise<{ draft: PlatformWarehouseDraft }> {
  return request(`/api/erp/platform-warehouse/drafts/${draftId}/archive`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note }),
  });
}

export interface RefreshStatus {
  in_progress: boolean;
  in_progress_by: string | null;
  in_progress_display_name: string | null;
  started_at: string | null;
  last_success_at: string | null;
  last_success_by: string | null;
  last_success_display_name: string | null;
  cooldown_until: string | null;
  cooldown_remaining_seconds: number;
  cooldown_seconds: number;
  admin_exempt: boolean;
  can_refresh: boolean;
}

export function fetchRefreshStatus(): Promise<RefreshStatus> {
  return request<RefreshStatus>("/api/erp/refresh-status");
}

export async function refreshStoreData(): Promise<{
  succeeded: boolean;
  message: string;
  refresh_status: RefreshStatus;
}> {
  return request("/api/erp/refresh", { method: "POST" });
}

export async function fetchExports(asOf: string): Promise<ExportPayload> {
  return request<ExportPayload>(`/api/erp/exports?${query(asOf)}`);
}

export async function generateExports(asOf: string): Promise<ExportPayload> {
  return request<ExportPayload>("/api/erp/exports", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ as_of: asOf }),
  });
}

export async function inspectNft102(file: File): Promise<NftInspection> {
  const body = new FormData();
  body.append("file", file);
  return request<NftInspection>("/api/erp/nft102/inspect", {
    method: "POST",
    body,
  });
}

export async function generateNft102(
  file: File,
  reportDate: string,
): Promise<NftGeneration> {
  const body = new FormData();
  body.append("file", file);
  body.append("report_date", reportDate);
  return request<NftGeneration>("/api/erp/nft102/generate", {
    method: "POST",
    body,
  });
}

export function fetchDailyReport(
  businessDate: string,
  captureStart?: string,
  captureEnd?: string,
): Promise<DailyReportPayload> {
  const params = new URLSearchParams({ business_date: businessDate });
  if (captureStart) params.set("capture_start", captureStart);
  if (captureEnd) params.set("capture_end", captureEnd);
  return request<DailyReportPayload>(`/api/erp/daily-report?${params.toString()}`);
}

export function fetchDailyReportReminders(): Promise<DailyReportReminders> {
  return request<DailyReportReminders>("/api/erp/daily-report/reminders");
}

export function saveDailyReportManual(
  businessDate: string,
  offerId: string,
  input: {
    page_views_30_days?: number | null;
    ordered_units?: number | null;
    platform_stock?: number | null;
    reason: string;
    note?: string;
  },
): Promise<{ ok: boolean }> {
  return request(
    `/api/erp/daily-report/${encodeURIComponent(businessDate)}/${encodeURIComponent(offerId)}/manual`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    },
  );
}

export function confirmDailyReportEntry(
  businessDate: string,
  offerId: string,
  source: "morning" | "evening" | "latest" | "manual",
  note: string,
): Promise<{ ok: boolean; exported: boolean }> {
  return request(
    `/api/erp/daily-report/${encodeURIComponent(businessDate)}/${encodeURIComponent(offerId)}/confirm`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source, note }),
    },
  );
}

export function revertDailyReportConfirmation(
  businessDate: string,
  offerId: string,
  note: string,
): Promise<{ ok: boolean }> {
  return request(
    `/api/erp/daily-report/${encodeURIComponent(businessDate)}/${encodeURIComponent(offerId)}/revert-confirmation`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note }),
    },
  );
}

export function confirmReadyDailyReportEntries(
  businessDate: string,
  note: string,
): Promise<{ ok: boolean; confirmed: number; exported: boolean }> {
  return request(
    `/api/erp/daily-report/${encodeURIComponent(businessDate)}/confirm-ready`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note }),
    },
  );
}

export function dismissDailyReportStockAlert(
  businessDate: string,
  offerId: string,
  note: string,
): Promise<{ ok: boolean }> {
  return request(
    `/api/erp/daily-report/${encodeURIComponent(businessDate)}/${encodeURIComponent(offerId)}/stock-alert`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note }),
    },
  );
}

export function eliminateDailyReportStockAlert(
  businessDate: string,
  offerId: string,
  note: string,
): Promise<{ ok: boolean }> {
  return request(
    `/api/erp/daily-report/${encodeURIComponent(businessDate)}/${encodeURIComponent(offerId)}/stock-alert/eliminate`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note }),
    },
  );
}

export function reopenDailyReportStockAlert(
  businessDate: string,
  offerId: string,
  note: string,
): Promise<{ ok: boolean }> {
  return request(
    `/api/erp/daily-report/${encodeURIComponent(businessDate)}/${encodeURIComponent(offerId)}/stock-alert/reopen`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note }),
    },
  );
}

export function saveDailyReportNote(
  businessDate: string,
  offerId: string,
  note: string,
  issueType: "general" | "capture_difference" | "stock_continuity",
): Promise<{ ok: boolean }> {
  return request(
    `/api/erp/daily-report/${encodeURIComponent(businessDate)}/${encodeURIComponent(offerId)}/note`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note, issue_type: issueType }),
    },
  );
}

export function updateDailyReportNote(
  businessDate: string,
  offerId: string,
  noteId: number,
  note: string,
  issueType: "general" | "capture_difference" | "stock_continuity",
): Promise<{ ok: boolean }> {
  return request(
    `/api/erp/daily-report/${encodeURIComponent(businessDate)}/${encodeURIComponent(offerId)}/note/${noteId}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note, issue_type: issueType }),
    },
  );
}

export function deleteDailyReportNote(
  businessDate: string,
  offerId: string,
  noteId: number,
  note = "",
): Promise<{ ok: boolean }> {
  return request(
    `/api/erp/daily-report/${encodeURIComponent(businessDate)}/${encodeURIComponent(offerId)}/note/${noteId}`,
    {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note }),
    },
  );
}

export function fetchDailyReportExport(through: string): Promise<DailyReportExport> {
  return request<DailyReportExport>(
    `/api/erp/daily-report/export?through=${encodeURIComponent(through)}`,
  );
}

export function generateDailyReportExport(
  through: string,
): Promise<DailyReportExport> {
  return request<DailyReportExport>("/api/erp/daily-report/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ as_of: through }),
  });
}
