import { competitorCategoryIdentity } from "./competitorCategoryMatches.ts";
import { productIdentity, sharedProductType, dependentProductReference, conflictingProductHeads } from "./competitorProductIdentity.ts";
import type { CompetitorCategoryBreadcrumb, CompetitorItem } from "./types";

export type CompetitorMatchKind = "near_identical" | "same_need";

export interface CompetitorMatchSource {
  plid: string;
  商品: string;
  类目路径?: CompetitorCategoryBreadcrumb[];
  价格?: number | null;
}

export interface CompetitorMatchCandidate extends CompetitorMatchSource {
  来源: "competitor" | "own_store";
  采集时间?: string;
  链接?: string;
  当前卖家?: string | null;
  周期销售额?: number | null;
  近期观察售出?: Partial<Record<string, number | null>>;
  最新评论数?: number | null;
  评论数?: number | null;
}

export interface CompetitorMatchResult<T extends CompetitorMatchCandidate = CompetitorItem> {
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
  ["children", "child"],
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

// Audience, packaging and marketing words cannot establish product identity alone.
const GENERIC_PRODUCT_TERMS = new Set([
  "adult", "child", "infant", "portable", "device", "kit", "set", "pack",
  "rescue", "emergency", "aid", "professional", "home", "use", "new",
  "maker", "machine", "equipment", "black", "white", "stainless", "steel",
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
  const candidateLeaf = candidatePath.at(-1) ?? null;
  const candidateParent = candidatePath.at(-2) ?? null;
  const sharedCategory = [...sourcePath]
    .reverse()
    .find((entry) => candidateIdentities.has(competitorCategoryIdentity(entry))) ?? null;
  return {
    exactLeaf: Boolean(
      sourceLeaf
      && candidateLeaf && competitorCategoryIdentity(sourceLeaf) === competitorCategoryIdentity(candidateLeaf),
    ),
    sameParent: Boolean(
      sourceParent
      && candidateParent && competitorCategoryIdentity(sourceParent) === competitorCategoryIdentity(candidateParent),
    ),
    sharedCategory,
  };
}

function commercialPriority(item: CompetitorMatchCandidate): number {
  const observedThirtyDays = item.近期观察售出?.["30"] ?? 0;
  return (item.周期销售额 ?? 0)
    + observedThirtyDays * Math.max(item.价格 ?? 0, 1)
    + (item.最新评论数 ?? item.评论数 ?? 0) * 0.01;
}

interface MatchFeatures {
  title: string;
  tokens: string[];
  bigrams: string[];
  models: string[];
  specs: string[];
  needs: string[];
}

// Explicit shared-use evidence can bridge differing marketplace categories.
// A pet/audience word alone never establishes a competing product.
function sharedUseFamilies(title: string): string[] {
  const cat = /\b(?:cat|cats|kitten|kittens|feline)\b/.test(title);
  const scratching = /\b(?:scratch|scratching|scratcher|scratchers)\b/.test(title);
  const furniture = /\b(?:tree|tower|post|house|condo|villa|furniture|bed|lounge|lounger|board|pad|mat)\b/.test(title);
  const climbingFurniture = /\b(?:cat|kitten)\s+(?:tree|tower|condo|house|villa)\b/.test(title);
  const accessory = STRONG_ACCESSORY_PATTERN.test(title)
    || /\b(?:carri(?:er|ers)|transport|waste|bag|bags|liner|liners)\b/.test(title);
  return cat && ((scratching && furniture) || climbingFurniture) && !accessory
    ? ["猫咪抓挠、攀爬与休憩家具"] : [];
}

const featureCache = new WeakMap<CompetitorMatchSource, { raw: string; features: MatchFeatures }>();
function matchFeatures(item: CompetitorMatchSource): MatchFeatures {
  const cached = featureCache.get(item);
  if (cached?.raw === item.商品) return cached.features;
  const title = normalizeTitle(item.商品);
  const tokens = titleTokens(item.商品);
  const features = { title, tokens, bigrams: titleBigrams(tokens), models: modelTokens(tokens),
    specs: titleSpecs(item.商品), needs: sharedUseFamilies(title) };
  featureCache.set(item, { raw: item.商品, features });
  return features;
}

interface MatchIndex {
  terms: Map<string, Set<CompetitorMatchCandidate>>;
  titles: Map<string, Set<CompetitorMatchCandidate>>;
  needs: Map<string, Set<CompetitorMatchCandidate>>;
  identities: Map<string, Set<CompetitorMatchCandidate>>;
}
// Catalog arrays are immutable generations. Inventory/card refreshes are separate.
const indexCache = new WeakMap<readonly CompetitorMatchCandidate[], MatchIndex>();
function indexedCandidates<T extends CompetitorMatchCandidate>(source: CompetitorMatchSource, candidates: readonly T[]): T[] {
  let index = indexCache.get(candidates);
  if (!index) {
    index = { terms: new Map(), titles: new Map(), needs: new Map(), identities: new Map() };
    const add = (map: Map<string, Set<CompetitorMatchCandidate>>, key: string, item: T) => {
      if (!map.has(key)) map.set(key, new Set());
      map.get(key)!.add(item);
    };
    for (const item of candidates) {
      const features = matchFeatures(item);
      for (const token of features.tokens) add(index.terms, token, item);
      add(index.titles, features.title, item);
      for (const need of features.needs) add(index.needs, need, item);
      for (const key of productIdentity(item).forms.keys()) add(index.identities, key, item);
    }
    indexCache.set(candidates, index);
  }
  const features = matchFeatures(source);
  const selected = new Set<CompetitorMatchCandidate>(index.titles.get(features.title));
  for (const term of features.tokens) for (const item of index.terms.get(term) ?? []) selected.add(item);
  for (const need of features.needs) for (const item of index.needs.get(need) ?? []) selected.add(item);
  for (const key of productIdentity(source).forms.keys()) for (const item of index.identities.get(key) ?? []) selected.add(item);
  return [...selected] as T[];
}

function scoreCandidate<T extends CompetitorMatchCandidate>(
  source: CompetitorMatchSource,
  candidate: T,
): CompetitorMatchResult<T> | null {
  const left = matchFeatures(source);
  const right = matchFeatures(candidate);
  const sourceTitle = left.title;
  const candidateTitle = right.title;
  if (!sourceTitle || !candidateTitle) return null;
  const sourceIdentity = productIdentity(source);
  const candidateIdentity = productIdentity(candidate);
  const accessoryMismatch = sourceIdentity.accessory !== candidateIdentity.accessory;
  if (accessoryMismatch || dependentProductReference(sourceIdentity, candidateIdentity)
    || dependentProductReference(candidateIdentity, sourceIdentity)) return null;
  const sharedNeeds = sharedValues(left.needs, right.needs);
  const knownUseConflict = (left.needs.length > 0 || right.needs.length > 0) && !sharedNeeds.length;
  const sharedType = knownUseConflict ? null : sharedProductType(sourceIdentity, candidateIdentity);
  if (knownUseConflict || (!sharedType && !sharedNeeds.length
    && conflictingProductHeads(sourceIdentity, candidateIdentity))) return null;
  const differentCatUse = (furniture: MatchFeatures, other: MatchFeatures) => furniture.needs.length > 0
    && other.needs.length === 0 && /\b(?:litter|toilet|scoop|carri(?:er|ers)|transport|food|feeding|bowl|fountain)\b/.test(other.title);
  if (differentCatUse(left, right) || differentCatUse(right, left)) return null;

  const sourceTokens = left.tokens;
  const candidateTokens = right.tokens;
  const sharedTerms = sharedValues(sourceTokens, candidateTokens)
    .sort((left, right) => tokenWeight(right) - tokenWeight(left) || left.localeCompare(right));
  const sharedCoreTerms = sharedTerms.filter((term) => !GENERIC_PRODUCT_TERMS.has(term));
  const sharedBigrams = sharedValues(left.bigrams, right.bigrams);
  const sharedCoreWeight = sharedCoreTerms.reduce((total, term) => total + tokenWeight(term), 0);
  const shorterCoreWeight = Math.min(
    ...[sourceTokens, candidateTokens].map((tokens) => (
      unique(tokens).filter((term) => !GENERIC_PRODUCT_TERMS.has(term))
        .reduce((total, term) => total + tokenWeight(term), 0)
    )),
  );
  const coreContainment = shorterCoreWeight ? sharedCoreWeight / shorterCoreWeight : 0;
  const tokenSimilarity = weightedDice(sourceTokens, candidateTokens);
  const bigramSimilarity = weightedDice(
    left.bigrams,
    right.bigrams,
  );
  const sharedModels = sharedValues(left.models, right.models);
  const sourceSpecs = left.specs;
  const candidateSpecs = right.specs;
  const sharedSpecs = sharedValues(sourceSpecs, candidateSpecs);
  const specsComparable = sourceSpecs.length > 0 && candidateSpecs.length > 0;
  const specSimilarity = specsComparable
    ? (2 * sharedSpecs.length) / (sourceSpecs.length + candidateSpecs.length)
    : 0;
  const categories = categoryRelation(source, candidate);

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
    && sharedCoreTerms.length > 0
    && (categories.exactLeaf || sharedModels.length > 0)
    && (
      (score >= 66 && tokenSimilarity >= 0.5)
      || (sharedModels.length > 0 && score >= 60 && tokenSimilarity >= 0.32)
    )
    && (!specsComparable || specSimilarity >= 0.3)
  );
  // Missing or differently assigned leaves must not veto strong title evidence.
  // Use coverage of the shorter title so long listing copy does not drown it out.
  const strongTitleEvidence = !accessoryMismatch
    && sharedCoreTerms.length >= 2
    && sharedTerms.length >= 3
    && (tokenSimilarity >= 0.78 || (
      coreContainment >= 0.85 && tokenSimilarity >= 0.35 && sharedBigrams.length > 0
    ));
  const sameNeed = Boolean(sharedType) || sharedNeeds.length > 0 || sharedCoreTerms.length > 0 && ((
    categories.exactLeaf
    && score >= 30
    && (sharedTerms.length > 0 || tokenSimilarity >= 0.14)
  ) || (
    categories.sameParent
    && score >= 45
    && sharedTerms.length >= 2
  ) || (
    strongTitleEvidence
  ));

  if (!nearIdentical && !sameNeed) return null;
  if (accessoryMismatch && score < 74) return null;

  if (!nearIdentical && (sharedNeeds.length || sharedType)) score = Math.max(score, 42);
  const reasons: string[] = [];
  if (sharedType && !nearIdentical) reasons.push(`共同品名：${sharedType}`);
  if (sharedNeeds.length && !nearIdentical) reasons.push(`同一用途：${sharedNeeds[0]}`);
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

export function rankCompetitorMatches<T extends CompetitorMatchCandidate>(
  source: CompetitorMatchSource,
  candidates: readonly T[],
): CompetitorMatchResult<T>[] {
  const sourcePlid = String(source.plid ?? "").trim().toLocaleLowerCase();
  const byPlid = new Map<string, T>();
  for (const candidate of indexedCandidates(source, candidates)) {
    const plid = String(candidate.plid ?? "").trim().toLocaleLowerCase();
    if (!plid || plid === sourcePlid) continue;
    const existing = byPlid.get(plid);
    if (
      !existing
      || (existing.来源 !== "own_store" && candidate.来源 === "own_store")
      || (existing.来源 === candidate.来源 && (candidate.采集时间 ?? "") > (existing.采集时间 ?? ""))
    ) byPlid.set(plid, candidate);
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
