import type {
  AnomalyProductPayload,
  CollectResult,
  CompetitorDetail,
  CompetitorLinkHealthItem,
  CompetitorListingCommitResult,
  CompetitorListingOperationItemPayload,
  CompetitorListingOperationPayload,
  CompetitorListingPreview,
  CompetitorOverview,
  CompetitorPersonalWatchlistItem,
  CompetitorPersonalWatchlistPayload,
  PersonalWatchlistLibrary,
  PersonalWatchlistLibrarySharePermission,
  PersonalWatchlistShareUser,
  CompetitorStoreTargetPayload,
  CompetitorTargetAuditPayload,
  CompetitorTargetItem,
  FreshnessPayload,
  LogisticsOverviewPayload,
  KeywordTrafficDetailPayload,
  KeywordTrafficListPayload,
  AuthSession,
  AuthStatus,
  ManagedStore,
  ManagedUser,
  PermissionKey,
  UserRole,
  ProductDetailPayload,
  ProductsPayload,
  QuadrantPayload,
  SalesRevenueRevisionPayload,
  StoreOverviewPayload,
  SummaryPayload,
  OwnStoreScope,
  OwnStoreCompetitorOverview,
  PlatformWarehouseDraft,
  PlatformWarehousePayload,
} from "./types";
import type {
  SearchRootExpansionLibraryPayload,
  SearchRankingBatchPreviewPayload,
  SearchRankingBatchStatusPayload,
  SearchRankingDetailPayload,
  SearchRankingListPayload,
  SearchRankingProductFactType,
} from "./types";
import { templatePermissions } from "./permissions";
import { AuthSessionRevision } from "./authSessionRevision";
import {
  getActiveStoreCode,
  setActiveStoreCode,
  withStoreContext,
} from "./storeContext";

let csrfToken = "";
const authSessionRevision = new AuthSessionRevision();

export { setActiveStoreCode, withStoreContext };

export const AUTH_SESSION_ENDING_EVENT = "erp-auth-session-ending";

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
  authSessionRevision.advance();
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
  const requestAuthSessionRevision = authSessionRevision.snapshot();
  const headers = new Headers(init?.headers);
  const method = (init?.method ?? "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  headers.set("X-Store-Code", getActiveStoreCode());
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
      && authSessionRevision.isCurrent(requestAuthSessionRevision)
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
  signal?: AbortSignal,
): Promise<CompetitorOverview> {
  const query = new URLSearchParams();
  if (startDate) query.set("start_date", startDate);
  if (endDate) query.set("end_date", endDate);
  query.set("own_store_scope", ownStoreScope);
  const suffix = query.size ? `?${query.toString()}` : "";
  return request<CompetitorOverview>(`/api/competitors${suffix}`, { signal });
}

export function fetchOwnStoreCompetitors(
  startDate?: string,
  endDate?: string,
  ownStoreScope: OwnStoreScope = "current",
  signal?: AbortSignal,
  plid?: string,
): Promise<OwnStoreCompetitorOverview> {
  const query = new URLSearchParams();
  if (startDate) query.set("start_date", startDate);
  if (endDate) query.set("end_date", endDate);
  query.set("own_store_scope", ownStoreScope);
  if (plid) query.set("plid", plid);
  return request<OwnStoreCompetitorOverview>(
    `/api/competitors/own-store?${query.toString()}`,
    { signal },
  );
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

export function fetchCompetitorPersonalWatchlist(): Promise<CompetitorPersonalWatchlistPayload> {
  return request<CompetitorPersonalWatchlistPayload>(
    "/api/competitors/personal-watchlist",
  );
}

export function fetchCompetitorPersonalWatchlistOverview(
  startDate?: string,
  endDate?: string,
): Promise<CompetitorOverview> {
  const query = new URLSearchParams();
  if (startDate) query.set("start_date", startDate);
  if (endDate) query.set("end_date", endDate);
  const suffix = query.size ? `?${query.toString()}` : "";
  return request<CompetitorOverview>(`/api/competitors/personal-watchlist/overview${suffix}`);
}

export async function fetchPersonalWatchlistShareUsers(): Promise<PersonalWatchlistShareUser[]> {
  const result = await request<{ items: PersonalWatchlistShareUser[] }>(
    "/api/competitors/personal-watchlist/share-users",
  );
  return result.items;
}

export function addCompetitorPersonalWatchlistItem(
  plid: string,
): Promise<{ item: CompetitorPersonalWatchlistItem; created: boolean }> {
  return request(`/api/competitors/personal-watchlist/${encodeURIComponent(plid)}`, {
    method: "PUT",
  });
}

export function deleteCompetitorPersonalWatchlistItem(
  plid: string,
): Promise<{ ok: boolean; removed: boolean }> {
  return request(`/api/competitors/personal-watchlist/${encodeURIComponent(plid)}`, {
    method: "DELETE",
  });
}

export function createPersonalWatchlistLibrary(
  name: string,
): Promise<{ library: PersonalWatchlistLibrary }> {
  return request("/api/competitors/personal-watchlist/libraries", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}

export function renamePersonalWatchlistLibrary(
  libraryId: number,
  name: string,
): Promise<{ library: PersonalWatchlistLibrary }> {
  return request(
    `/api/competitors/personal-watchlist/libraries/${encodeURIComponent(libraryId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    },
  );
}

export function deletePersonalWatchlistLibrary(
  libraryId: number,
): Promise<{
  ok: boolean;
  default_library_configured: boolean;
  default_library_id: number | null;
}> {
  return request(
    `/api/competitors/personal-watchlist/libraries/${encodeURIComponent(libraryId)}`,
    { method: "DELETE" },
  );
}

export function updatePersonalWatchlistLibraryShares(
  libraryId: number,
  shares: Array<{
    user_id: number;
    permission: PersonalWatchlistLibrarySharePermission;
  }>,
): Promise<{ library: PersonalWatchlistLibrary }> {
  return request(
    `/api/competitors/personal-watchlist/libraries/${encodeURIComponent(libraryId)}/shares`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ shares }),
    },
  );
}

export function deletePersonalWatchlistLibraryItem(
  libraryId: number,
  plid: string,
): Promise<{ ok: boolean; removed: boolean; library: PersonalWatchlistLibrary }> {
  return request(
    `/api/competitors/personal-watchlist/libraries/${encodeURIComponent(libraryId)}`
      + `/items/${encodeURIComponent(plid)}`,
    { method: "DELETE" },
  );
}

export function updatePersonalWatchlistSettings(
  defaultLibraryId: number | null,
): Promise<{
  default_library_configured: boolean;
  default_library_id: number | null;
}> {
  return request("/api/competitors/personal-watchlist/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ default_library_id: defaultLibraryId }),
  });
}

export function updatePersonalWatchlistItemLibraries(
  plid: string,
  libraryIds: number[],
): Promise<{ plid: string; library_ids: number[] }> {
  return request(
    `/api/competitors/personal-watchlist/${encodeURIComponent(plid)}/libraries`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ library_ids: libraryIds }),
    },
  );
}

export async function fetchCompetitorStoreTargets(
  ownStoreScope: OwnStoreScope = "current",
  signal?: AbortSignal,
): Promise<CompetitorStoreTargetPayload> {
  const query = new URLSearchParams({ own_store_scope: ownStoreScope });
  return request<CompetitorStoreTargetPayload>(
    `/api/competitors/store-targets?${query.toString()}`,
    { signal },
  );
}

export async function createCompetitorTarget(
  url: string,
): Promise<{
  item: CompetitorTargetItem | null;
  queued_to_active_batch: boolean;
  automatic_store_target: boolean;
  store_names: string[];
  personal_watchlist_member: boolean;
  personal_watchlist_item: CompetitorPersonalWatchlistItem;
}> {
  return request("/api/competitors/targets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
}

export function previewCompetitorListing(input: {
  source_type: "seller" | "category";
  url: string;
  price_min?: number;
  price_max?: number;
  sorts: string[];
  product_limit?: number;
}): Promise<CompetitorListingPreview> {
  return request("/api/competitors/listing-preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function commitCompetitorListing(
  previewToken: string,
  libraryId: number,
  productLimit?: number,
): Promise<CompetitorListingCommitResult> {
  return request("/api/competitors/listing-targets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      preview_token: previewToken,
      library_id: libraryId,
      product_limit: productLimit,
    }),
  });
}

export function fetchCompetitorListingOperations(
  sourceType: "seller" | "category",
  page = 1,
  pageSize = 10,
): Promise<CompetitorListingOperationPayload> {
  const query = new URLSearchParams({
    source_type: sourceType,
    page: String(page),
    page_size: String(pageSize),
  });
  return request<CompetitorListingOperationPayload>(
    `/api/competitors/listing-operations?${query.toString()}`,
  );
}

export function fetchCompetitorListingOperationItems(
  operationId: number,
  page = 1,
  pageSize = 20,
): Promise<CompetitorListingOperationItemPayload> {
  const query = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  return request<CompetitorListingOperationItemPayload>(
    `/api/competitors/listing-operations/${encodeURIComponent(operationId)}/items?${query.toString()}`,
  );
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
  source: "manual" | "scheduled";
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
  results: Array<CollectResult & { url: string }>;
  errors: Array<{
    plid: string;
    url: string;
    message: string;
  }>;
  scheduled_resume_available?: boolean;
  scheduled_resume_pending?: number;
  scheduled_wait_kind?: "network" | "pending_retry" | null;
  scheduled_auto_resume_at?: string | null;
  scheduled_retry_round?: number;
  scheduled_retry_round_limit?: number;
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

export async function stopCompetitorBatch(
  batchId: string,
  reason: string,
): Promise<CompetitorBatchStatus> {
  const result = await request<{ ok: boolean; status: CompetitorBatchStatus }>(
    "/api/competitors/batch-stop",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch_id: batchId, reason }),
    },
  );
  return result.status;
}

export async function resumeStoppedScheduledCompetitorBatch(
  batchId: string,
): Promise<CompetitorBatchStatus> {
  const result = await request<{ ok: boolean; status: CompetitorBatchStatus }>(
    "/api/competitors/batch-resume",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch_id: batchId }),
    },
  );
  return result.status;
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

export async function fetchSummaryRange(
  startDate: string,
  endDate: string,
): Promise<SummaryPayload> {
  const params = new URLSearchParams({ start_date: startDate, as_of: endDate });
  return request<SummaryPayload>(`/api/erp/summary?${params.toString()}`);
}

export async function fetchStoreOverview(
  startDate: string,
  endDate: string,
  storeScope: Exclude<OwnStoreScope, "current"> = "all",
): Promise<StoreOverviewPayload> {
  const params = new URLSearchParams({
    start_date: startDate,
    as_of: endDate,
    store_scope: storeScope,
  });
  return request<StoreOverviewPayload>(
    `/api/erp/summary/stores?${params.toString()}`,
  );
}

export async function fetchSalesRevenueRevisions(options: {
  startDate?: string;
  endDate?: string;
  page?: number;
  pageSize?: number;
  storeScope?: Exclude<OwnStoreScope, "current">;
} = {}): Promise<SalesRevenueRevisionPayload> {
  const params = new URLSearchParams();
  if (options.startDate) params.set("start_date", options.startDate);
  if (options.endDate) params.set("end_date", options.endDate);
  if (options.storeScope) params.set("store_scope", options.storeScope);
  params.set("page", String(options.page ?? 1));
  params.set("page_size", String(options.pageSize ?? 20));
  return request<SalesRevenueRevisionPayload>(
    `/api/erp/summary/stores/sales-revisions?${params.toString()}`,
  );
}

export async function fetchProducts(asOf: string): Promise<ProductsPayload> {
  return request<ProductsPayload>(`/api/erp/products?${query(asOf)}`);
}

export async function fetchAnomalyProducts(
  asOf: string,
): Promise<AnomalyProductPayload> {
  return request<AnomalyProductPayload>(
    `/api/erp/anomaly-products?${query(asOf)}`,
  );
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

export function fetchSearchRankingProducts(): Promise<SearchRankingListPayload> {
  return request<SearchRankingListPayload>("/api/erp/search-ranking");
}

export function fetchSearchRankingRootExpansionLibrary(
  search = "",
): Promise<SearchRootExpansionLibraryPayload> {
  const params = new URLSearchParams();
  if (search.trim()) params.set("search", search.trim());
  params.set("limit", "100");
  return request<SearchRootExpansionLibraryPayload>(
    `/api/erp/search-ranking/root-expansion-library?${params.toString()}`,
  );
}

export function fetchSearchRankingBatchPreview(): Promise<SearchRankingBatchPreviewPayload> {
  return request<SearchRankingBatchPreviewPayload>("/api/erp/search-ranking/batch");
}

export function fetchSearchRankingBatchStatus(): Promise<SearchRankingBatchStatusPayload> {
  return request<SearchRankingBatchStatusPayload>("/api/erp/search-ranking/batch/status");
}

export function startSearchRankingBatch(
  snapshotId: string,
): Promise<SearchRankingBatchStatusPayload> {
  return request<SearchRankingBatchStatusPayload>("/api/erp/search-ranking/batch/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      snapshot_id: snapshotId,
      confirmed_paid_model_calls: true,
      confirmed_public_takealot_requests: true,
      confirmed_strict_serial_no_retry: true,
    }),
  });
}

export function controlSearchRankingBatch(
  action: "pause" | "resume" | "stop",
): Promise<SearchRankingBatchStatusPayload> {
  return request<SearchRankingBatchStatusPayload>(
    `/api/erp/search-ranking/batch/${action}`,
    { method: "POST" },
  );
}

export function restartSearchRankingBatch(
  snapshotId: string,
): Promise<SearchRankingBatchStatusPayload> {
  return request<SearchRankingBatchStatusPayload>("/api/erp/search-ranking/batch/restart", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      snapshot_id: snapshotId,
      confirmed_paid_model_calls: true,
      confirmed_public_takealot_requests: true,
      confirmed_strict_serial_no_retry: true,
    }),
  });
}

export function fetchSearchRankingDetail(
  offerId: string,
): Promise<SearchRankingDetailPayload> {
  return request<SearchRankingDetailPayload>(
    `/api/erp/search-ranking/${encodeURIComponent(offerId)}`,
  );
}

export function analyzeSearchRanking(
  offerId: string,
): Promise<SearchRankingDetailPayload> {
  return request<SearchRankingDetailPayload>(
    `/api/erp/search-ranking/${encodeURIComponent(offerId)}/analyze`,
    { method: "POST" },
  );
}

export function confirmSearchRankingDecisionParameters(
  offerId: string,
  choices: Array<{
    parameter_key: string;
    is_decision_parameter: boolean;
  }>,
): Promise<SearchRankingDetailPayload> {
  return request<SearchRankingDetailPayload>(
    `/api/erp/search-ranking/${encodeURIComponent(offerId)}/decision-parameters/confirm`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        choices,
        confirmed_current_title: true,
        acknowledged_search_validation: true,
        acknowledged_no_ranking_guarantee: true,
      }),
    },
  );
}

export function confirmSearchRankingProductFacts(
  offerId: string,
  payload: {
    source_analysis_id: number;
    reason_code: string;
    facts: Array<{
      fact_type: SearchRankingProductFactType;
      fact_term: string;
      statement: string;
    }>;
    confirmed: true;
    acknowledged_fact_accuracy: true;
    acknowledged_ranking_revalidation: true;
  },
): Promise<SearchRankingDetailPayload> {
  return request<SearchRankingDetailPayload>(
    `/api/erp/search-ranking/${encodeURIComponent(offerId)}/product-facts/confirm`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function revokeSearchRankingProductFact(
  offerId: string,
  factId: number,
  reason: string,
): Promise<SearchRankingDetailPayload> {
  return request<SearchRankingDetailPayload>(
    `/api/erp/search-ranking/${encodeURIComponent(offerId)}/product-facts/${factId}/revoke`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    },
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
