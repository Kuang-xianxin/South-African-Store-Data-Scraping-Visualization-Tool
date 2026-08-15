export const ERP_MODULE_KEYS = [
  "overview",
  "products",
  "keyword-traffic",
  "search-ranking",
  "quadrants",
  "anomaly-products",
  "logistics",
  "platform-warehouse",
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
