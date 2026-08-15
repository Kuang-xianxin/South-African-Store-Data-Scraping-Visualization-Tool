import type {
  SearchRankingAnalysisSummary,
  SearchRankingProduct,
  SearchRankingVariantParameter,
  SearchRankingVariantParameterType,
} from "./types";

export interface SearchRankingProductFamily {
  key: string;
  productline_id: string | null;
  representative: SearchRankingProduct;
  variants: SearchRankingProduct[];
  variant_count: number;
  total_available_stock: number;
  latest_analysis: SearchRankingAnalysisSummary | null;
  shared_title: string;
  variant_parameter_values: string[];
  variant_parameters_by_offer: Record<string, SearchRankingVariantParameter[]>;
}

export function searchRankingFamilyKey(product: SearchRankingProduct): string {
  const productlineId = String(product.productline_id ?? "").trim().toLocaleLowerCase();
  return productlineId
    ? `plid:${productlineId}`
    : `offer:${String(product.offer_id).trim().toLocaleLowerCase()}`;
}

function representativeFor(variants: SearchRankingProduct[]): SearchRankingProduct {
  const sorted = [...variants].sort((left, right) => left.offer_id.localeCompare(right.offer_id));
  const declaredRepresentative = sorted.find((item) =>
    item.family_representative_offer_id === item.offer_id,
  );
  if (declaredRepresentative) return declaredRepresentative;
  const analysisSourceOfferId = sorted
    .map((item) => item.latest_analysis?.source_offer_id)
    .find((value) => value);
  const analysisRepresentative = sorted.find((item) => item.offer_id === analysisSourceOfferId);
  if (analysisRepresentative) return analysisRepresentative;
  const analysed = sorted.filter((item) => item.latest_analysis != null);
  if (!analysed.length) return sorted[0];
  const newest = analysed.reduce((latest, item) => {
    const timestamp = item.latest_analysis?.created_at ?? "";
    return timestamp > latest ? timestamp : latest;
  }, "");
  return analysed.find((item) => item.latest_analysis?.created_at === newest) ?? analysed[0];
}

function titleTokens(value: string | null | undefined): string[] {
  return String(value ?? "").match(/[\p{L}\p{N}]+/gu) ?? [];
}

function longestCommonSubsequence(left: string[], right: string[]): string[] {
  const rows = Array.from({ length: left.length + 1 }, () =>
    Array.from({ length: right.length + 1 }, () => 0),
  );
  for (let leftIndex = left.length - 1; leftIndex >= 0; leftIndex -= 1) {
    for (let rightIndex = right.length - 1; rightIndex >= 0; rightIndex -= 1) {
      rows[leftIndex][rightIndex] = left[leftIndex].toLocaleLowerCase()
        === right[rightIndex].toLocaleLowerCase()
        ? rows[leftIndex + 1][rightIndex + 1] + 1
        : Math.max(rows[leftIndex + 1][rightIndex], rows[leftIndex][rightIndex + 1]);
    }
  }
  const output: string[] = [];
  let leftIndex = 0;
  let rightIndex = 0;
  while (leftIndex < left.length && rightIndex < right.length) {
    if (left[leftIndex].toLocaleLowerCase() === right[rightIndex].toLocaleLowerCase()) {
      output.push(left[leftIndex]);
      leftIndex += 1;
      rightIndex += 1;
    } else if (rows[leftIndex + 1][rightIndex] >= rows[leftIndex][rightIndex + 1]) {
      leftIndex += 1;
    } else {
      rightIndex += 1;
    }
  }
  return output;
}

function unmatchedPhrases(tokens: string[], sharedTokens: string[]): string[] {
  const shared = sharedTokens.map((token) => token.toLocaleLowerCase());
  const groups: string[][] = [];
  let current: string[] = [];
  let sharedIndex = 0;
  for (const token of tokens) {
    if (sharedIndex < shared.length && token.toLocaleLowerCase() === shared[sharedIndex]) {
      if (current.length) groups.push(current);
      current = [];
      sharedIndex += 1;
    } else {
      current.push(token);
    }
  }
  if (current.length) groups.push(current);
  return groups.map((group) => group.join(" ")).filter(Boolean);
}

const colourTokens = new Set([
  "beige", "black", "blue", "bronze", "brown", "charcoal", "clear", "copper",
  "cream", "gold", "gray", "green", "grey", "ivory", "navy", "orange", "pink",
  "purple", "red", "silver", "tan", "transparent", "white", "yellow",
]);
const sizeTokens = new Set([
  "single", "double", "queen", "king", "super", "xl", "xs", "small", "medium",
  "large", "extra", "quarter", "three", "of",
]);

function fallbackParameterType(value: string): SearchRankingVariantParameterType {
  const tokens = titleTokens(value).map((token) => token.toLocaleLowerCase());
  if (tokens.length && tokens.every((token) => colourTokens.has(token))) return "colour";
  if (tokens.length && tokens.every((token) => sizeTokens.has(token))) return "size";
  if (tokens.some((token) => /^\d+(?:\.\d+)?(?:gb|tb|mb|mah|ah|wh|l|ml)$/i.test(token))) {
    return "capacity";
  }
  if (tokens.some((token) => /^\d+(?:\.\d+)?(?:cm|mm|m|ft|inch|inches)$/i.test(token))) {
    return "size";
  }
  return "variant_value";
}

function familyMetadata(
  variants: SearchRankingProduct[],
  representative: SearchRankingProduct,
) {
  let sharedTokens = titleTokens(representative.title);
  for (const variant of variants) {
    if (variant.offer_id === representative.offer_id) continue;
    sharedTokens = longestCommonSubsequence(sharedTokens, titleTokens(variant.title));
    if (!sharedTokens.length) break;
  }
  const declaredSharedTitle = variants
    .map((item) => item.shared_family_title?.trim())
    .find((value) => value);
  const sharedTitle = declaredSharedTitle
    ?? (sharedTokens.length ? sharedTokens.join(" ") : String(representative.title ?? ""));
  const effectiveSharedTokens = titleTokens(sharedTitle);
  const parametersByOffer = Object.fromEntries(variants.map((variant) => {
    const supplied = variant.variant_parameters ?? [];
    const parameters = supplied.length || variants.length === 1
      ? supplied
      : unmatchedPhrases(titleTokens(variant.title), effectiveSharedTokens).map((value) => ({
          value,
          parameter_type: fallbackParameterType(value),
          source: "seller_offer_title_difference" as const,
          visually_verified: false as const,
        }));
    return [variant.offer_id, parameters];
  }));
  const parameterValues = [...new Set(
    Object.values(parametersByOffer).flat().map((item) => item.value).filter(Boolean),
  )];
  return {
    sharedTitle,
    parametersByOffer,
    parameterValues,
  };
}

export function groupSearchRankingProducts(
  products: SearchRankingProduct[],
): SearchRankingProductFamily[] {
  const grouped = new Map<string, SearchRankingProduct[]>();
  for (const product of products) {
    const key = searchRankingFamilyKey(product);
    grouped.set(key, [...(grouped.get(key) ?? []), product]);
  }

  return [...grouped.entries()]
    .map(([key, rawVariants]) => {
      const variants = [...rawVariants].sort((left, right) =>
        left.offer_id.localeCompare(right.offer_id),
      );
      const representative = representativeFor(variants);
      const metadata = familyMetadata(variants, representative);
      return {
        key,
        productline_id: representative.productline_id,
        representative,
        variants,
        variant_count: variants.length,
        total_available_stock: variants.reduce(
          (total, item) => total + Math.max(0, item.available_stock ?? 0),
          0,
        ),
        latest_analysis: representative.latest_analysis,
        shared_title: metadata.sharedTitle,
        variant_parameter_values: metadata.parameterValues,
        variant_parameters_by_offer: metadata.parametersByOffer,
      };
    })
    .sort((left, right) => {
      const titleOrder = String(left.representative.title ?? "").localeCompare(
        String(right.representative.title ?? ""),
      );
      return titleOrder || left.key.localeCompare(right.key);
    });
}
