import { matchesProductSearch } from "./productSearch.ts";
import { parseUtcDateTime } from "./time.ts";
import type { SearchRankingProductFamily } from "./searchRankingFamilies";
import type { SearchRankingProduct } from "./types";

export type PickerView = "all" | "unanalysed" | "low_score" | "manual" | "failed" | "completed";
export type PickerSort = "priority" | "title" | "title_desc" | "score_asc" | "score_desc" | "stock_desc" | "stock_asc" | "newest" | "oldest" | "variants_asc" | "variants_desc" | "captured_asc" | "captured_desc";
export type PickerSearchField = "all" | "title" | "company_sku" | "sku" | "offer_id" | "productline_id";
export interface PickerFilters {
  search: string;
  searchField: PickerSearchField;
  searchMatch: "contains" | "exact";
  exclude: string;
  view: PickerView;
  store: string;
  identity: string;
  score: string;
  variants: string;
  sku: string;
  image: string;
  stockMin: string;
  stockMax: string;
  scoreMin: string;
  scoreMax: string;
  sort: PickerSort;
}

export function defaultPickerFilters(): PickerFilters {
  return { search: "", searchField: "all", searchMatch: "contains", exclude: "", view: "all", store: "", identity: "all", score: "all", variants: "all", sku: "all", image: "all", stockMin: "", stockMax: "", scoreMin: "", scoreMax: "", sort: "priority" };
}

export function pickerQueries(search: string): string[] {
  return [...new Set(search.split(/[\n,，;；]+/).map((part) => {
    const value = part.trim();
    if (/^https?:\/\//i.test(value)) {
      try {
        const url = new URL(value);
        if (["takealot.com", "www.takealot.com"].includes(url.hostname.toLowerCase())) {
          return url.pathname.match(/\/PLID(\d+)\/?$/i)?.[1] ?? value;
        }
      } catch { /* Invalid URLs remain literal searches. */ }
    }
    return value.replace(/^PLID\s*(\d+)$/i, "$1");
  }).filter(Boolean))];
}

function matchesQuery(product: SearchRankingProduct, query: string, options: Pick<PickerFilters, "searchField" | "searchMatch"> = defaultPickerFilters()): boolean {
  const fields = options.searchField === "all" ? {
    productNames: [product.title, product.company_product_name],
    otherValues: [product.sku, product.company_sku, product.offer_id, product.productline_id, product.store_name, product.store_code],
  } : options.searchField === "title" ? {
    productNames: [product.title, product.company_product_name], otherValues: [],
  } : { productNames: [], otherValues: [product[options.searchField]] };
  if (options.searchMatch === "exact") {
    return [...fields.productNames, ...fields.otherValues].some((value) =>
      value?.trim().toLocaleLowerCase() === query.trim().toLocaleLowerCase(),
    );
  }
  return matchesProductSearch(fields, query)
    || query.split(/\s+/).every((token) => matchesProductSearch(fields, token));
}

export function pickerTarget(family: SearchRankingProductFamily, queries: string[], options: Pick<PickerFilters, "searchField" | "searchMatch"> = defaultPickerFilters()): SearchRankingProduct {
  if (!queries.length) return family.representative;
  const candidates = family.variants.filter((product) => queries.some((query) => matchesQuery(product, query, options)));
  // An exact SKU/Offer search should open that variant, not an unrelated representative.
  return candidates.find((product) => queries.some((query) =>
    [product.offer_id, product.sku, product.company_sku].some((value) =>
      value?.toLocaleLowerCase() === query.toLocaleLowerCase(),
    ),
  )) ?? candidates.find((product) => product.offer_id === family.representative.offer_id)
    ?? candidates[0] ?? family.representative;
}

export function pickerFilterError(filters: PickerFilters): string {
  for (const [label, min, max, limit] of [
    ["可售库存", filters.stockMin, filters.stockMax, Infinity],
    ["标题评分", filters.scoreMin, filters.scoreMax, 100],
  ] as const) {
    for (const value of [min, max]) {
      if (value.trim() && (!Number.isFinite(Number(value)) || Number(value) < 0 || Number(value) > limit)) return `${label}请输入${limit === 100 ? "0–100之间的" : "非负"}数值`;
    }
    if (min.trim() && max.trim() && Number(min) > Number(max)) return `${label}下限不能大于上限`;
  }
  return "";
}

function withinRange(value: number | null, min: string, max: string): boolean {
  if (!min.trim() && !max.trim()) return true;
  return value !== null && Number.isFinite(value)
    && (!min.trim() || value >= Number(min)) && (!max.trim() || value <= Number(max));
}

export function pickerScore(family: SearchRankingProductFamily): number | null {
  const analysis = family.latest_analysis;
  const score = analysis?.title_score_value;
  return analysis?.status === "completed"
    && analysis.title_score_current_title_match === true
    && analysis.title_score_band !== "insufficient_evidence"
    && typeof score === "number" && Number.isFinite(score)
    ? score : null;
}

export function pickerViewMatches(family: SearchRankingProductFamily, view: PickerView): boolean {
  const analysis = family.latest_analysis;
  switch (view) {
    case "unanalysed": return !analysis;
    case "low_score": return pickerScore(family) !== null && pickerScore(family)! < 70;
    case "manual": return Boolean(analysis?.manual_fact_required || analysis?.identity_large_difference || analysis?.identity_difference_level === "high");
    case "failed": return analysis?.status === "failed";
    case "completed": return analysis?.status === "completed";
    default: return true;
  }
}

export function pickerStatus(family: SearchRankingProductFamily): string {
  const analysis = family.latest_analysis;
  if (!analysis) return "未分析";
  if (analysis.status === "running") return "分析中";
  if (analysis.status === "failed") return "上次失败";
  if (analysis.manual_fact_required) return "待人工事实";
  if (analysis.identity_large_difference || analysis.identity_difference_level === "high") return "图题差异大";
  if (analysis.title_score_current_title_match === false) return "标题已变化";
  return "已分析";
}

function priority(family: SearchRankingProductFamily): number {
  if (pickerViewMatches(family, "manual")) return 0;
  if (pickerViewMatches(family, "failed")) return 1;
  if (pickerViewMatches(family, "low_score")) return 2;
  if (family.latest_analysis?.title_score_current_title_match === false) return 3;
  if (!family.latest_analysis) return 4;
  return 5;
}

function compareNullable(left: number | null, right: number | null, direction: number): number {
  if (left === null) return right === null ? 0 : 1;
  if (right === null) return -1;
  return (left - right) * direction;
}

function analysisTime(family: SearchRankingProductFamily): number | null {
  const value = parseUtcDateTime(family.latest_analysis?.created_at ?? null);
  return Number.isFinite(value) ? value : null;
}

export function filterPickerFamilies(families: SearchRankingProductFamily[], filters: PickerFilters): SearchRankingProductFamily[] {
  if (pickerFilterError(filters)) return [];
  const queries = pickerQueries(filters.search);
  const exclusions = pickerQueries(filters.exclude);
  return families.filter((family) => {
    const analysis = family.latest_analysis;
    if (filters.store && String(family.representative.store_code ?? "") !== filters.store) return false;
    if (queries.length && !family.variants.some((product) => queries.some((query) => matchesQuery(product, query, filters)))) return false;
    if (exclusions.length && family.variants.some((product) => exclusions.some((query) => matchesQuery(product, query)))) return false;
    if (!pickerViewMatches(family, filters.view)) return false;
    if (filters.identity !== "all" && analysis?.identity_difference_level !== filters.identity) return false;
    if (filters.variants === "multiple" && family.variant_count < 2) return false;
    if (filters.variants === "single" && family.variant_count !== 1) return false;
    if (filters.sku === "missing" && family.variants.every((product) => product.company_sku?.trim())) return false;
    if (filters.sku === "linked" && family.variants.some((product) => !product.company_sku?.trim())) return false;
    if (filters.image === "missing" && family.variants.every((product) => product.image_url?.trim())) return false;
    if (filters.image === "complete" && family.variants.some((product) => !product.image_url?.trim())) return false;
    if (!withinRange(family.total_available_stock, filters.stockMin, filters.stockMax)) return false;
    const score = pickerScore(family);
    if (!withinRange(score, filters.scoreMin, filters.scoreMax)) return false;
    if (filters.score === "unscored" && score !== null) return false;
    if (filters.score === "insufficient" && analysis?.title_score_band !== "insufficient_evidence") return false;
    if (filters.score === "stale" && analysis?.title_score_current_title_match !== false) return false;
    const ranges: Record<string, [number, number]> = { "85_plus": [85, 101], "70_84": [70, 85], "55_69": [55, 70], "below_55": [0, 55] };
    const range = ranges[filters.score];
    return !range || (score !== null && score >= range[0] && score < range[1]);
  }).sort((left, right) => {
    let result = 0;
    switch (filters.sort) {
      case "priority": result = priority(left) - priority(right); break;
      case "score_asc": result = compareNullable(pickerScore(left), pickerScore(right), 1); break;
      case "score_desc": result = compareNullable(pickerScore(left), pickerScore(right), -1); break;
      case "stock_desc": result = right.total_available_stock - left.total_available_stock; break;
      case "stock_asc": result = left.total_available_stock - right.total_available_stock; break;
      case "newest": result = compareNullable(analysisTime(left), analysisTime(right), -1); break;
      case "oldest": result = compareNullable(analysisTime(left), analysisTime(right), 1); break;
      case "title_desc": result = String(right.shared_title).localeCompare(String(left.shared_title), "zh-CN", { numeric: true }); break;
      case "variants_asc": result = left.variant_count - right.variant_count; break;
      case "variants_desc": result = right.variant_count - left.variant_count; break;
      case "captured_asc":
      case "captured_desc": {
        const captured = (family: SearchRankingProductFamily) => {
          const time = parseUtcDateTime(family.representative.captured_at);
          return Number.isFinite(time) ? time : null;
        };
        result = compareNullable(captured(left), captured(right), filters.sort === "captured_asc" ? 1 : -1);
        break;
      }
    }
    return result || String(left.shared_title || left.representative.title || "").localeCompare(
      String(right.shared_title || right.representative.title || ""), "zh-CN", { numeric: true },
    ) || left.key.localeCompare(right.key);
  });
}
