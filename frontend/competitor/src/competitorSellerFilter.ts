export interface CompetitorSellerFilterOffer {
  卖家ID?: string | number | null;
  卖家?: string | null;
}

export interface CompetitorSellerFilterItem {
  plid: string;
  跟卖报价?: readonly CompetitorSellerFilterOffer[];
  对比报价?: readonly CompetitorSellerFilterOffer[];
}

export interface CompetitorSellerOption {
  key: string;
  sellerId: string | null;
  sellerName: string;
  inputValue: string;
  productCount: number;
}

interface CompetitorSellerOptionDraft {
  key: string;
  sellerId: string | null;
  sellerNames: Map<string, { value: string; count: number }>;
  productPlids: Set<string>;
}

const UNKNOWN_SELLER_NAMES = new Set([
  "未知卖家",
  "unknown",
  "unknown seller",
]);

function cleanSellerValue(value: string | number | null | undefined): string {
  return String(value ?? "").trim();
}

function normalizedSearchValue(value: string | number | null | undefined): string {
  return cleanSellerValue(value).toLocaleLowerCase();
}

function usableSellerName(value: string | null | undefined): string {
  const sellerName = cleanSellerValue(value);
  return UNKNOWN_SELLER_NAMES.has(sellerName.toLocaleLowerCase()) ? "" : sellerName;
}

export function normalizeCompetitorSellerId(
  value: string | number | null | undefined,
): string {
  const sellerId = cleanSellerValue(value);
  return /^m\d+$/i.test(sellerId) ? sellerId.slice(1) : sellerId;
}

function comparisonOffers(
  item: CompetitorSellerFilterItem,
): readonly CompetitorSellerFilterOffer[] {
  return item.对比报价 ?? item.跟卖报价 ?? [];
}

function sellerIdentityKey(offer: CompetitorSellerFilterOffer): string {
  const sellerId = normalizeCompetitorSellerId(offer.卖家ID);
  if (sellerId) return `id:${sellerId.toLocaleLowerCase()}`;
  const sellerName = usableSellerName(offer.卖家);
  return sellerName ? `name:${sellerName.toLocaleLowerCase()}` : "";
}

function preferredSellerName(
  sellerNames: Map<string, { value: string; count: number }>,
): string {
  return [...sellerNames.values()]
    .sort((left, right) => (
      right.count - left.count
      || left.value.localeCompare(right.value, "zh-CN", { sensitivity: "base" })
    ))[0]?.value ?? "";
}

export function competitorSellerInputValue(
  sellerName: string,
  sellerId: string | null,
): string {
  if (sellerName && sellerId) return `${sellerName} · sellers ${sellerId}`;
  if (sellerId) return `sellers ${sellerId}`;
  return sellerName;
}

export function buildCompetitorSellerOptions(
  items: readonly CompetitorSellerFilterItem[],
): CompetitorSellerOption[] {
  const drafts = new Map<string, CompetitorSellerOptionDraft>();

  for (const item of items) {
    for (const offer of comparisonOffers(item)) {
      const key = sellerIdentityKey(offer);
      if (!key) continue;
      const sellerId = normalizeCompetitorSellerId(offer.卖家ID) || null;
      const sellerName = usableSellerName(offer.卖家);
      const draft = drafts.get(key) ?? {
        key,
        sellerId,
        sellerNames: new Map<string, { value: string; count: number }>(),
        productPlids: new Set<string>(),
      };
      draft.productPlids.add(item.plid);
      if (sellerName) {
        const normalizedName = sellerName.toLocaleLowerCase();
        const existing = draft.sellerNames.get(normalizedName);
        draft.sellerNames.set(normalizedName, {
          value: existing?.value ?? sellerName,
          count: (existing?.count ?? 0) + 1,
        });
      }
      drafts.set(key, draft);
    }
  }

  return [...drafts.values()]
    .map((draft) => {
      const sellerName = preferredSellerName(draft.sellerNames);
      return {
        key: draft.key,
        sellerId: draft.sellerId,
        sellerName,
        inputValue: competitorSellerInputValue(sellerName, draft.sellerId),
        productCount: draft.productPlids.size,
      };
    })
    .sort((left, right) => (
      (left.sellerName || left.sellerId || "").localeCompare(
        right.sellerName || right.sellerId || "",
        "zh-CN",
        { sensitivity: "base" },
      )
      || (left.sellerId ?? "").localeCompare(right.sellerId ?? "")
    ));
}

function offerMatchesSellerQuery(
  offer: CompetitorSellerFilterOffer,
  normalizedQuery: string,
): boolean {
  const rawSellerId = cleanSellerValue(offer.卖家ID);
  const sellerId = normalizeCompetitorSellerId(rawSellerId);
  const sellerName = usableSellerName(offer.卖家);
  const values = [sellerName, rawSellerId, sellerId];
  if (sellerId) {
    values.push(`M${sellerId}`, `sellers ${sellerId}`, `sellers=${sellerId}`);
  }
  if (sellerName || sellerId) {
    values.push(competitorSellerInputValue(sellerName, sellerId || null));
  }
  return values.some((value) => normalizedSearchValue(value).includes(normalizedQuery));
}

export function matchesCompetitorSellerFilter(
  item: CompetitorSellerFilterItem,
  query: string | number | null | undefined,
): boolean {
  const normalizedQuery = normalizedSearchValue(query);
  if (!normalizedQuery) return true;
  return comparisonOffers(item).some((offer) => (
    offerMatchesSellerQuery(offer, normalizedQuery)
  ));
}
