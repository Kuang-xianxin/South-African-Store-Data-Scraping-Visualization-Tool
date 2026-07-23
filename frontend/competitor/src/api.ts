import type {
  CollectResult,
  CompetitorDetail,
  CompetitorItem,
  ExportPayload,
  FreshnessPayload,
  NftGeneration,
  NftInspection,
  ProductDetailPayload,
  ProductsPayload,
  QuadrantPayload,
  RiskPayload,
  SummaryPayload,
} from "./types";

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message =
      typeof payload.detail === "string" ? payload.detail : "本机接口请求失败";
    throw new Error(message);
  }
  return payload as T;
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
): Promise<CollectResult> {
  return request<CollectResult>("/api/competitors/collect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      url,
      with_stock_probe: withStockProbe,
      visible_browser: visibleBrowser,
    }),
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
