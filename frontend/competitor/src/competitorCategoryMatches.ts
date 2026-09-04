import type { CompetitorCategoryBreadcrumb } from "./types";

export interface CompetitorCategoryCatalogItem {
  来源: "competitor" | "own_store";
  plid: string;
  商品: string;
  类目路径?: CompetitorCategoryBreadcrumb[];
}

function normalizedCategoryValue(value: string | null | undefined): string {
  return String(value ?? "").trim().toLocaleLowerCase();
}

export function competitorCategoryIdentity(
  category: CompetitorCategoryBreadcrumb,
): string {
  const id = normalizedCategoryValue(category.id);
  if (id) return `id:${id}`;
  const slug = normalizedCategoryValue(category.slug);
  if (slug) return `slug:${slug}`;
  return [
    "type-name",
    normalizedCategoryValue(category.type),
    normalizedCategoryValue(category.name),
  ].join(":");
}

export function competitorItemMatchesCategory(
  item: CompetitorCategoryCatalogItem,
  category: CompetitorCategoryBreadcrumb,
): boolean {
  const selectedIdentity = competitorCategoryIdentity(category);
  return (item.类目路径 ?? []).some(
    (candidate) => competitorCategoryIdentity(candidate) === selectedIdentity,
  );
}

export function mergeCompetitorCategoryCatalog<T extends CompetitorCategoryCatalogItem>(
  ...groups: ReadonlyArray<readonly T[]>
): T[] {
  const byPlid = new Map<string, T>();
  for (const group of groups) {
    for (const item of group) {
      const key = normalizedCategoryValue(item.plid);
      if (!key) continue;
      const existing = byPlid.get(key);
      if (!existing || (existing.来源 !== "own_store" && item.来源 === "own_store")) {
        byPlid.set(key, item);
      }
    }
  }
  return [...byPlid.values()].sort((left, right) => {
    if (left.来源 !== right.来源) return left.来源 === "own_store" ? -1 : 1;
    return left.商品.localeCompare(right.商品, "en", { sensitivity: "base" });
  });
}
