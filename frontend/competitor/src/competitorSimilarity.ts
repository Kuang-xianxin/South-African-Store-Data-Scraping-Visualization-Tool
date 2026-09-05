import { competitorCategoryIdentity } from "./competitorCategoryMatches.ts";
import type { CompetitorCategoryBreadcrumb, CompetitorItem } from "./types";

export type CompetitorMatchKind = "near_identical" | "same_need";

export interface CompetitorMatchSource {
  plid: string;
  商品: string;
  类目路径?: CompetitorCategoryBreadcrumb[];
  价格?: number | null;
}

export interface CompetitorMatchResult<T extends CompetitorItem = CompetitorItem> {
  item: T;
  kind: CompetitorMatchKind;
  score: number;
  reasons: string[];
  sharedTerms: string[];
}

const STOP_WORDS = new Set([
  "a",
  "an",
  "and",
  "at",
  "by",
  "for",
  "from",
  "in",
  "into",
  "of",
  "on",
  "or",
  "the",
  "to",
  "with",
  "without",
  "your",
]);

const TOKEN_ALIASES = new Map<string, string>([
  ["centimeter", "cm"],
  ["centimeters", "cm"],
  ["centimetre", "cm"],
  ["centimetres", "cm"],
  ["inches", "inch"],
  ["liters", "litre"],
  ["litres", "litre"],
  ["meters", "metre"],
  ["metres", "metre"],
  ["millimeter", "mm"],
  ["millimeters", "mm"],
  ["millimetre", "mm"],
  ["millimetres", "mm"],
  ["pc", "piece"],
  ["pcs", "piece"],
  ["pieces", "piece"],
  ["television", "tv"],
]);

const STRONG_ACCESSORY_PATTERN = /\b(?:accessor(?:y|ies)|attachment|bracket|case|cover|holder|parts?|protector|refill|replacement|sleeve|spare)\b/i;
const SPEC_PATTERN = /(\d+(?:\.\d+)?)\s*(?:-|\s)?(ah|cm|g|gb|inch|inches|kg|l|liters?|litres?|m|mah|mb|ml|mm|piece|pieces|pcs|v|volt|volts|w|watt|watts)\b/gi;

function normalizeTitle(value: string): string {
  return value
    .normalize("NFKC")
    .toLocaleLowerCase()
    .replace(/&/g, " and ")
    .replace(/[^\p{L}\p{N}.]+/gu, " ")
    .trim();
}

function singularize(token: string): string {
  if (token.length > 5 && token.endsWith("ies")) return `${token.slice(0, -3)}y`;
  if (token.length > 4 && token.endsWith("ses")) return token.slice(0, -2);
  if (token.length > 4 && token.endsWith("s") && !token.endsWith("ss")) {
    return token.slice(0, -1);
  }
  return token;
}

function titleTokens(value: string): string[] {
  return normalizeTitle(value)
    .split(/\s+/)
    .map((token) => TOKEN_ALIASES.get(token) ?? singularize(token))
    .filter((token) => token.length > 1 && !STOP_WORDS.has(token));
}

function unique<T>(values: readonly T[]): T[] {
  return [...new Set(values)];
}

function tokenWeight(token: string): number {
  if (/\d/.test(token)) return 1.8;
  if (token.length >= 9) return 1.45;
  if (token.length >= 6) return 1.2;
  return 1;
}

function weightedDice(left: readonly string[], right: readonly string[]): number {
  const leftSet = new Set(left);
  const rightSet = new Set(right);
  const leftWeight = [...leftSet].reduce((total, token) => total + tokenWeight(token), 0);
  const rightWeight = [...rightSet].reduce((total, token) => total + tokenWeight(token), 0);
  if (!leftWeight || !rightWeight) return 0;
  const sharedWeight = [...leftSet]
    .filter((token) => rightSet.has(token))
    .reduce((total, token) => total + tokenWeight(token), 0);
  return (2 * sharedWeight) / (leftWeight + rightWeight);
}

function titleBigrams(tokens: readonly string[]): string[] {
  const bigrams: string[] = [];
  for (let index = 0; index < tokens.length - 1; index += 1) {
    bigrams.push(`${tokens[index]} ${tokens[index + 1]}`);
  }
  return unique(bigrams);
}

function sharedValues(left: readonly string[], right: readonly string[]): string[] {
  const rightSet = new Set(right);
  return unique(left.filter((value) => rightSet.has(value)));
}

function modelTokens(tokens: readonly string[]): string[] {
  return unique(tokens.filter((token) => /[a-z]/i.test(token) && /\d/.test(token)));
}

function titleSpecs(value: string): string[] {
  const normalized = normalizeTitle(value);
  const specs: string[] = [];
  for (const match of normalized.matchAll(SPEC_PATTERN)) {
    const amount = Number.parseFloat(match[1] ?? "");
    const unit = TOKEN_ALIASES.get(match[2]?.toLocaleLowerCase() ?? "")
      ?? singularize(match[2]?.toLocaleLowerCase() ?? "");
    if (Number.isFinite(amount) && unit) specs.push(`${amount}:${unit}`);
  }
  return unique(specs);
}

function categoryPath(item: CompetitorMatchSource): CompetitorCategoryBreadcrumb[] {
  return (item.类目路径 ?? []).filter((entry) => entry.name.trim());
}

interface CategoryRelation {
  exactLeaf: boolean;
  sameParent: boolean;
  sharedCategory: CompetitorCategoryBreadcrumb | null;
}

function categoryRelation(
  source: CompetitorMatchSource,
  candidate: CompetitorMatchSource,
): CategoryRelation {
  const sourcePath = categoryPath(source);
  const candidatePath = categoryPath(candidate);
  const candidateIdentities = new Set(candidatePath.map(competitorCategoryIdentity));
  const sourceLeaf = sourcePath.at(-1) ?? null;
  const sourceParent = sourcePath.at(-2) ?? null;
  const sharedCategory = [...sourcePath]
    .reverse()
    .find((entry) => candidateIdentities.has(competitorCategoryIdentity(entry))) ?? null;
  return {
    exactLeaf: Boolean(
      sourceLeaf
      && candidateIdentities.has(competitorCategoryIdentity(sourceLeaf)),
    ),
    sameParent: Boolean(
      sourceParent
      && candidateIdentities.has(competitorCategoryIdentity(sourceParent)),
    ),
    sharedCategory,
  };
}

function commercialPriority(item: CompetitorItem): number {
  const observedThirtyDays = item.近期观察售出?.["30"] ?? 0;
  return (item.周期销售额 ?? 0)
    + observedThirtyDays * Math.max(item.价格 ?? 0, 1)
    + (item.最新评论数 ?? item.评论数 ?? 0) * 0.01;
}

function scoreCandidate<T extends CompetitorItem>(
  source: CompetitorMatchSource,
  candidate: T,
): CompetitorMatchResult<T> | null {
  const sourceTitle = normalizeTitle(source.商品);
  const candidateTitle = normalizeTitle(candidate.商品);
  if (!sourceTitle || !candidateTitle) return null;

  const sourceTokens = titleTokens(source.商品);
  const candidateTokens = titleTokens(candidate.商品);
  const sharedTerms = sharedValues(sourceTokens, candidateTokens)
    .sort((left, right) => tokenWeight(right) - tokenWeight(left) || left.localeCompare(right));
  const tokenSimilarity = weightedDice(sourceTokens, candidateTokens);
  const bigramSimilarity = weightedDice(
    titleBigrams(sourceTokens),
    titleBigrams(candidateTokens),
  );
  const sharedModels = sharedValues(modelTokens(sourceTokens), modelTokens(candidateTokens));
  const sourceSpecs = titleSpecs(source.商品);
  const candidateSpecs = titleSpecs(candidate.商品);
  const sharedSpecs = sharedValues(sourceSpecs, candidateSpecs);
  const specsComparable = sourceSpecs.length > 0 && candidateSpecs.length > 0;
  const specSimilarity = specsComparable
    ? (2 * sharedSpecs.length) / (sourceSpecs.length + candidateSpecs.length)
    : 0;
  const categories = categoryRelation(source, candidate);
  const accessoryMismatch = STRONG_ACCESSORY_PATTERN.test(sourceTitle)
    !== STRONG_ACCESSORY_PATTERN.test(candidateTitle);

  let score = tokenSimilarity * 52 + bigramSimilarity * 14;
  if (categories.exactLeaf) score += 22;
  else if (categories.sameParent) score += 12;
  else if (categories.sharedCategory) score += 5;
  if (sharedModels.length) score += 8;
  if (specsComparable) score += specSimilarity * 8;
  if (sourceTitle === candidateTitle) score = 100;
  if (accessoryMismatch) score -= 24;
  score = Math.max(0, Math.min(100, Math.round(score)));

  const exactTitle = sourceTitle === candidateTitle;
  const nearIdentical = exactTitle || (
    !accessoryMismatch
    && (categories.exactLeaf || sharedModels.length > 0)
    && (
      (score >= 66 && tokenSimilarity >= 0.5)
      || (sharedModels.length > 0 && score >= 60 && tokenSimilarity >= 0.32)
    )
    && (!specsComparable || specSimilarity >= 0.3)
  );
  const sameNeed = (
    categories.exactLeaf
    && score >= 30
    && (sharedTerms.length > 0 || tokenSimilarity >= 0.14)
  ) || (
    categories.sameParent
    && score >= 45
    && sharedTerms.length >= 2
  ) || (
    !categories.sharedCategory
    && score >= 72
    && tokenSimilarity >= 0.65
  );

  if (!nearIdentical && !sameNeed) return null;
  if (accessoryMismatch && score < 74) return null;

  const reasons: string[] = [];
  if (exactTitle) reasons.push("商品标题完全一致");
  if (categories.exactLeaf) {
    reasons.push(`同一精确类目：${categoryPath(source).at(-1)?.name ?? "已采集类目"}`);
  } else if (categories.sameParent && categories.sharedCategory) {
    reasons.push(`同一相邻类目范围：${categories.sharedCategory.name}`);
  } else if (categories.sharedCategory) {
    reasons.push(`共享类目：${categories.sharedCategory.name}`);
  }
  if (sharedModels.length) reasons.push(`型号相符：${sharedModels.slice(0, 2).join("、")}`);
  if (sharedSpecs.length) {
    reasons.push(`规格相符：${sharedSpecs.slice(0, 2).map((value) => value.replace(":", " ")).join("、")}`);
  }
  if (sharedTerms.length) reasons.push(`共同核心词：${sharedTerms.slice(0, 4).join("、")}`);
  if (!reasons.length) reasons.push("标题结构高度接近");

  return {
    item: candidate,
    kind: nearIdentical ? "near_identical" : "same_need",
    score,
    reasons: reasons.slice(0, 3),
    sharedTerms: sharedTerms.slice(0, 8),
  };
}

export function rankCompetitorMatches<T extends CompetitorItem>(
  source: CompetitorMatchSource,
  candidates: readonly T[],
): CompetitorMatchResult<T>[] {
  const sourcePlid = String(source.plid ?? "").trim().toLocaleLowerCase();
  const byPlid = new Map<string, T>();
  for (const candidate of candidates) {
    if (candidate.来源 !== "competitor") continue;
    const plid = String(candidate.plid ?? "").trim().toLocaleLowerCase();
    if (!plid || plid === sourcePlid) continue;
    const existing = byPlid.get(plid);
    if (!existing || candidate.采集时间 > existing.采集时间) byPlid.set(plid, candidate);
  }

  return [...byPlid.values()]
    .map((candidate) => scoreCandidate(source, candidate))
    .filter((match): match is CompetitorMatchResult<T> => match !== null)
    .sort((left, right) => (
      right.score - left.score
      || commercialPriority(right.item) - commercialPriority(left.item)
      || left.item.商品.localeCompare(right.item.商品, "en", { sensitivity: "base" })
    ));
}
