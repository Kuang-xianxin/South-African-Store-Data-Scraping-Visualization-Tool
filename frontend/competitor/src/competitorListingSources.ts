export type CompetitorEntryType = "product" | "seller" | "category";
export type CompetitorListingSourceType = Exclude<CompetitorEntryType, "product">;

export const COMPETITOR_LISTING_SORT_OPTIONS = [
  { value: "Relevance", label: "相关度" },
  { value: "Price Descending", label: "价格：从高到低" },
  { value: "Price Ascending", label: "价格：从低到高" },
  { value: "Rating Descending", label: "评分最高" },
  { value: "ReleaseDate Descending", label: "最新上架" },
] as const;

export const DEFAULT_COMPETITOR_LISTING_SORTS = [
  "Rating Descending",
  "ReleaseDate Descending",
] as const;

const SORT_VALUES = new Set<string>(
  COMPETITOR_LISTING_SORT_OPTIONS.map((option) => option.value),
);
const PRICE_SORT_VALUES = new Set<string>([
  "Price Descending",
  "Price Ascending",
]);
const SEO_CATEGORY_PATH_PATTERN = /^\/(?:[a-z0-9]+(?:-[a-z0-9]+)*\/)*[a-z0-9]+(?:-[a-z0-9]+)*-\d{1,12}$/i;

export function classifyCompetitorEntryUrl(value: string): CompetitorEntryType | null {
  const parsed = parseTakealotUrl(value);
  if (!parsed) return null;
  const normalizedPath = parsed.pathname.replace(/\/+$/, "");
  if (/PLID\d+/i.test(value)) return "product";
  if (
    parsed.pathname.toLowerCase().startsWith("/seller/")
    && /^\d{1,30}$/.test(parsed.searchParams.get("sellers") ?? "")
  ) return "seller";
  if (
    (normalizedPath.toLowerCase() === "/all"
      && Boolean(parsed.searchParams.get("custom")?.trim()))
    || SEO_CATEGORY_PATH_PATTERN.test(normalizedPath)
  ) return "category";
  return null;
}

export function validateCompetitorEntryUrl(
  value: string,
  expectedType: CompetitorEntryType,
): string | null {
  if (!value.trim()) {
    return expectedType === "product"
      ? "请输入 Takealot 商品链接"
      : `请输入 Takealot ${expectedType === "seller" ? "店铺" : "类目"}链接`;
  }
  if (!parseTakealotUrl(value)) return "链接格式无效或不是 Takealot 链接";
  const actualType = classifyCompetitorEntryUrl(value);
  if (actualType === expectedType) return null;
  if (actualType) {
    const labels: Record<CompetitorEntryType, string> = {
      product: "商品链接",
      seller: "店铺链接",
      category: "类目链接",
    };
    return `识别为${labels[actualType]}，请切换到“${labels[actualType]}”入口`;
  }
  if (expectedType === "product") return "商品链接中未找到 Takealot PLID";
  if (expectedType === "seller") return "店铺链接应包含 /seller/ 和 sellers 店铺编号";
  return "类目链接应为末段带数字类目 ID 的 Takealot 类目路径，或 /all?custom=...";
}

export function listingSortFromUrl(value: string): string | null {
  const parsed = parseTakealotUrl(value);
  const sort = parsed?.searchParams.get("sort")?.trim() ?? "";
  return SORT_VALUES.has(sort) ? sort : null;
}

export function mergeListingSortsFromUrl(
  currentSorts: readonly string[],
  value: string,
): string[] {
  const sort = listingSortFromUrl(value);
  if (!sort || currentSorts.includes(sort)) return normalizeListingSorts(currentSorts);
  return normalizeListingSorts([...currentSorts, sort]);
}

export function toggleCompetitorListingSort(
  currentSorts: readonly string[],
  sort: string,
): string[] {
  const normalized = normalizeListingSorts(currentSorts);
  if (normalized.includes(sort)) return normalized.filter((item) => item !== sort);
  return normalizeListingSorts([...normalized, sort]);
}

export function parseOptionalListingInteger(
  value: string | number | null | undefined,
  label: string,
): number | undefined {
  const normalized = String(value ?? "").trim();
  if (!normalized) return undefined;
  if (!/^\d+$/.test(normalized)) throw new Error(`${label}必须是非负整数`);
  const parsed = Number(normalized);
  if (!Number.isSafeInteger(parsed)) throw new Error(`${label}超出可用范围`);
  return parsed;
}

function parseTakealotUrl(value: string): URL | null {
  let parsed: URL;
  try {
    parsed = new URL(value.trim());
  } catch {
    return null;
  }
  const hostname = parsed.hostname.toLowerCase();
  if (
    !["http:", "https:"].includes(parsed.protocol)
    || (hostname !== "takealot.com" && !hostname.endsWith(".takealot.com"))
  ) return null;
  return parsed;
}

function normalizeListingSorts(values: readonly string[]): string[] {
  const latestPriceSort = [...values].reverse().find((value) => PRICE_SORT_VALUES.has(value));
  const output: string[] = [];
  for (const value of values) {
    if (PRICE_SORT_VALUES.has(value) && value !== latestPriceSort) continue;
    if (!output.includes(value)) output.push(value);
  }
  return output;
}
