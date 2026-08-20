import type { OwnStoreScope } from "./types";

export const ERP_MODULE_KEYS = [
  "overview",
  "products",
  "keyword-traffic",
  "search-ranking",
  "quadrants",
  "anomaly-products",
  "returns",
  "logistics",
  "competitors",
  "users",
] as const;

export type ErpModuleKey = (typeof ERP_MODULE_KEYS)[number];

const ERP_MODULE_KEY_SET = new Set<string>(ERP_MODULE_KEYS);

export function isErpModuleKey(value: string | null | undefined): value is ErpModuleKey {
  return Boolean(value && ERP_MODULE_KEY_SET.has(value));
}

export function modulePageHref(moduleKey: ErpModuleKey): string {
  return `#module=${encodeURIComponent(moduleKey)}`;
}

export function modulePageFromHash(hash: string): ErpModuleKey | null {
  const normalizedHash = hash.trim().replace(/^#/, "");
  if (!normalizedHash) return null;
  const requestedModule = new URLSearchParams(normalizedHash).get("module");
  return isErpModuleKey(requestedModule) ? requestedModule : null;
}

export function competitorDetailPageHref(plid: string): string {
  const params = new URLSearchParams({
    module: "competitors",
    detail_plid: plid.trim(),
  });
  return `#${params.toString()}`;
}

export function competitorDetailPlidFromHash(hash: string): string | null {
  const normalizedHash = hash.trim().replace(/^#/, "");
  if (!normalizedHash) return null;
  const params = new URLSearchParams(normalizedHash);
  if (params.get("module") !== "competitors") return null;
  const plid = params.get("detail_plid")?.trim() ?? "";
  return /^\d{1,20}$/.test(plid) ? plid : null;
}

export interface OwnStoreDetailPageRequest {
  plid: string;
  scope: OwnStoreScope;
  storeCode?: string;
  startDate?: string;
  endDate?: string;
}

const OWN_STORE_SCOPES = new Set<OwnStoreScope>(["current", "operating", "all"]);
const ISO_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const STORE_CODE_PATTERN = /^[a-z0-9._-]{1,100}$/i;

export function ownStoreDetailPageHref(request: OwnStoreDetailPageRequest): string {
  const params = new URLSearchParams({
    module: "competitors",
    own_detail_plid: request.plid.trim(),
    own_store_scope: request.scope,
  });
  const storeCode = request.storeCode?.trim() ?? "";
  const startDate = request.startDate?.trim() ?? "";
  const endDate = request.endDate?.trim() ?? "";
  if (request.scope === "current" && storeCode) params.set("store_code", storeCode);
  if (ISO_DATE_PATTERN.test(startDate)) params.set("start_date", startDate);
  if (ISO_DATE_PATTERN.test(endDate)) params.set("end_date", endDate);
  return `#${params.toString()}`;
}

export function ownStoreDetailRequestFromHash(
  hash: string,
): OwnStoreDetailPageRequest | null {
  const normalizedHash = hash.trim().replace(/^#/, "");
  if (!normalizedHash) return null;
  const params = new URLSearchParams(normalizedHash);
  if (params.get("module") !== "competitors") return null;
  const plid = params.get("own_detail_plid")?.trim() ?? "";
  const scope = params.get("own_store_scope")?.trim() as OwnStoreScope | undefined;
  const storeCode = params.get("store_code")?.trim() ?? "";
  const startDate = params.get("start_date")?.trim() ?? "";
  const endDate = params.get("end_date")?.trim() ?? "";
  if (!/^\d{1,20}$/.test(plid) || !scope || !OWN_STORE_SCOPES.has(scope)) return null;
  if (storeCode && !STORE_CODE_PATTERN.test(storeCode)) return null;
  if (startDate && !ISO_DATE_PATTERN.test(startDate)) return null;
  if (endDate && !ISO_DATE_PATTERN.test(endDate)) return null;
  if (startDate && endDate && startDate > endDate) return null;
  return {
    plid,
    scope,
    ...(scope === "current" && storeCode ? { storeCode } : {}),
    ...(startDate ? { startDate } : {}),
    ...(endDate ? { endDate } : {}),
  };
}

export function openOwnStoreDetailTab(
  request: OwnStoreDetailPageRequest,
): Window | null {
  const href = `${window.location.pathname}${window.location.search}${ownStoreDetailPageHref(request)}`;
  const opened = window.open(href, "_blank");
  if (opened) opened.opener = null;
  return opened;
}

export interface ModuleNavigationClick {
  altKey: boolean;
  button: number;
  ctrlKey: boolean;
  defaultPrevented: boolean;
  metaKey: boolean;
  shiftKey: boolean;
}

export function shouldHandleModulePageClick(event: ModuleNavigationClick): boolean {
  return (
    !event.defaultPrevented
    && event.button === 0
    && !event.altKey
    && !event.ctrlKey
    && !event.metaKey
    && !event.shiftKey
  );
}
