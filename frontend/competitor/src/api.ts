import type {
  CollectResult,
  CompetitorDetail,
  CompetitorItem,
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

export function setAuthSession(session: AuthSession | null) {
  csrfToken = session?.csrf_token ?? "";
}

async function request<T>(url: string, init?: RequestInit & { signal?: AbortSignal }): Promise<T> {
  const headers = new Headers(init?.headers);
  const method = (init?.method ?? "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(url, {
    ...init,
    headers,
    credentials: "same-origin",
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (
      response.status === 401
      && !["/api/auth/login", "/api/auth/session"].includes(url)
    ) {
      window.dispatchEvent(new CustomEvent("erp-auth-expired"));
    }
    const message =
      typeof payload.detail === "string" ? payload.detail : "本机接口请求失败";
    throw new Error(message);
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

export async function fetchCompetitorDetail(
  plid: string,
): Promise<CompetitorDetail> {
  return request<CompetitorDetail>(`/api/competitors/${plid}`);
}

export async function collectCompetitor(
  url: string,
  withStockProbe: boolean,
  visibleBrowser: boolean,
  signal?: AbortSignal,
): Promise<CollectResult> {
  return request<CollectResult>("/api/competitors/collect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      url,
      with_stock_probe: withStockProbe,
      visible_browser: visibleBrowser,
    }),
    signal,
  });
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

export async function refreshStoreData(): Promise<{
  succeeded: boolean;
  message: string;
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
  source: "morning" | "evening" | "manual",
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
): Promise<{ ok: boolean }> {
  return request(
    `/api/erp/daily-report/${encodeURIComponent(businessDate)}/${encodeURIComponent(offerId)}/note`,
    {
      method: "POST",
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
