import type {
  CollectResult,
  CompetitorDetail,
  CompetitorItem,
  CompetitorLinkHealthItem,
  ExportPayload,
  FreshnessPayload,
  NftGeneration,
  NftInspection,
  AuthSession,
  AuthStatus,
  ManagedUser,
  UserRole,
  ProductDetailPayload,
  ProductsPayload,
  QuadrantPayload,
  RiskPayload,
  SummaryPayload,
  DailyReportExport,
  DailyReportPayload,
  DailyReportReminders,
} from "./types";

let csrfToken = "";

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

async function request<T>(url: string, init?: RequestInit & { signal?: AbortSignal }): Promise<T> {
  const headers = new Headers(init?.headers);
  const method = (init?.method ?? "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
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
  const session = await request<AuthSession>("/api/auth/session");
  setAuthSession(session);
  return session;
}

export async function login(username: string, password: string): Promise<AuthSession> {
  const session = await request<AuthSession>("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  setAuthSession(session);
  return session;
}

export async function bootstrapAdmin(
  username: string,
  displayName: string,
  password: string,
): Promise<AuthSession> {
  const session = await request<AuthSession>("/api/auth/bootstrap", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username,
      display_name: displayName,
      password,
    }),
  });
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

export async function createUser(input: {
  username: string;
  display_name: string;
  password: string;
  role: UserRole;
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

export async function fetchCompetitors(): Promise<CompetitorItem[]> {
  const result = await request<{ items: CompetitorItem[] }>("/api/competitors");
  return result.items;
}

export async function fetchCompetitorLinkHealth(): Promise<
  CompetitorLinkHealthItem[]
> {
  const result = await request<{ items: CompetitorLinkHealthItem[] }>(
    "/api/competitors/link-health",
  );
  return result.items;
}

export async function fetchCompetitorDetail(
  plid: string,
): Promise<CompetitorDetail> {
  return request<CompetitorDetail>(`/api/competitors/${plid}`);
}

export interface CompetitorCollectionContext {
  batchId: string;
  clientId: string;
  requestId: string;
  itemIndex: number;
  totalItems: number;
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
  reason: string;
  started_at: string | null;
  updated_at: string | null;
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
      reason: event.reason ?? "",
    }),
  });
  return result.status;
}

export function fetchCompetitorBatchStatus(): Promise<CompetitorBatchStatus> {
  return request<CompetitorBatchStatus>("/api/competitors/batch-status");
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

export function fetchDailyReport(businessDate: string): Promise<DailyReportPayload> {
  return request<DailyReportPayload>(
    `/api/erp/daily-report?business_date=${encodeURIComponent(businessDate)}`,
  );
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
    note: string;
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
): Promise<{ ok: boolean }> {
  return request(
    `/api/erp/daily-report/${encodeURIComponent(businessDate)}/${encodeURIComponent(offerId)}/note/${noteId}`,
    { method: "DELETE" },
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
