import type { CompetitorCategoryBreadcrumb, CompetitorItem } from "./types";

export type MatchKind = "near_identical" | "same_demand";
export type MatchSource = Pick<CompetitorItem, "plid" | "商品"> & Partial<CompetitorItem>;
export interface CompetitorMatch {
  item: CompetitorItem;
  kind: MatchKind;
  score: number;
  reasons: string[];
}

// Closed, reviewable product identities. Unknown title words (including brands)
// never qualify a candidate. Add identities with positive and negative examples.
const FAMILIES: ReadonlyArray<readonly [string, string, RegExp]> = [
  ["projector screen", "projection", /\b(?:projector|projection) screens?\b/],
  ["projector", "projector", /\bprojectors?\b/],
  ["carplay adapter", "car integration", /\b(?:carplay|android auto)\b.*\b(?:adapters?|dongles?)\b/],
  ["carplay display", "car integration", /\b(?:carplay|android auto)\b.*\b(?:screens?|displays?|stereos?)\b/],
  ["power bank", "portable power", /\bpower ?banks?\b/],
  ["power station", "portable power", /\bpower stations?\b/],
  ["phone holder", "phone support", /\b(?:phone|mobile|cellphone) (?:holders?|stands?|mounts?)\b/],
  ["laptop stand", "laptop support", /\b(?:laptop|notebook) (?:stands?|risers?)\b/],
  ["tablet stand", "tablet support", /\btablet (?:stands?|holders?)\b/],
  ["phone case", "phone protection", /\b(?:phone|iphone|samsung|galaxy)(?: [a-z0-9]+){0,3} (?:cases?|covers?)\b/],
  ["u shape toothbrush", "tooth cleaning", /\bu shap(?:e|ed) (?:electric )?toothbrush(?:es)?\b/],
  ["sonic toothbrush", "tooth cleaning", /\bsonic toothbrush(?:es)?\b/],
  ["electric toothbrush", "tooth cleaning", /\belectric toothbrush(?:es)?\b/],
  ["toothbrush", "tooth cleaning", /\btoothbrush(?:es)?\b/],
  ["water flosser", "tooth cleaning", /\b(?:water flossers?|oral irrigators?)\b/],
  ["sofa bed", "seating", /\bsofa beds?\b/],
  ["sofa", "seating", /\b(?:sofas?|couches|couch)\b/],
  ["bean bag", "seating", /\bbean ?bags?\b/],
  ["floor chair", "seating", /\bfloor chairs?\b/],
  ["office chair", "desk seating", /\b(?:office|desk|gaming) chairs?\b/],
  ["litter box", "cat toilet", /\b(?:cat litter|litter|cat storage) (?:box(?:es)?|trays?)\b/],
  ["pet carrier", "pet transport", /\b(?:pet|cat|dog) (?:carriers?|transport (?:box|bag))\b/],
  ["drip stand", "iv support", /\b(?:drip stands?|iv poles?|infusion stands?)\b/],
  ["floor drain", "drainage", /\b(?:floor|shower) drains?\b/],
  ["vacuum cleaner", "floor cleaning", /\b(?:vacuum cleaners?|robot vacuums?)\b/],
  ["mop", "floor cleaning", /\b(?:steam |spray |spin )?mops?\b/],
  ["air fryer", "cooking", /\bair fryers?\b/],
  ["oven", "cooking", /\b(?:electric |mini |convection )?ovens?\b/],
  ["kettle", "water boiling", /\bkettles?\b/],
  ["blender", "food blending", /\bblenders?\b/],
  ["food processor", "food blending", /\bfood processors?\b/],
  ["hearing aid", "hearing assistance", /\bhearing (?:aids?|amplifiers?)\b/],
  ["earbuds", "personal audio", /\b(?:earbuds?|earphones?)\b/],
  ["headphones", "personal audio", /\bheadphones?\b/],
  ["speaker", "room audio", /\b(?:bluetooth |wireless )?speakers?\b/],
  ["microphone", "audio recording", /\bmicrophones?\b/],
  ["dash camera", "driving recording", /\b(?:dash ?cams?|dash cameras?)\b/],
  ["security camera", "surveillance", /\b(?:security|cctv|ip|surveillance) cameras?\b/],
  ["floodlight", "outdoor lighting", /\bflood ?lights?\b/],
  ["solar light", "outdoor lighting", /\bsolar (?:street |garden |wall )?lights?\b/],
  ["desk lamp", "desk lighting", /\b(?:desk|table|reading) lamps?\b/],
  ["floor lamp", "room lighting", /\bfloor lamps?\b/],
  ["nail clipper", "nail trimming", /\bnail clippers?\b/],
  ["hair clipper", "hair trimming", /\b(?:hair|beard) (?:clippers?|trimmers?)\b/],
  ["shaver", "shaving", /\b(?:electric )?(?:shavers?|razors?)\b/],
  ["hair dryer", "hair drying", /\bhair ?dryers?\b/],
  ["air purifier", "air cleaning", /\bair purifiers?\b/],
  ["humidifier", "humidifying", /\bhumidifiers?\b/],
  ["dehumidifier", "dehumidifying", /\bdehumidifiers?\b/],
  ["shoe rack", "shoe storage", /\bshoe (?:racks?|organizers?|cabinets?)\b/],
  ["storage box", "storage", /\bstorage (?:box(?:es)?|bins?)\b/],
  ["water bottle", "drinking container", /\b(?:water|sports) bottles?\b/],
  ["tumbler", "drinking container", /\b(?:travel mugs?|tumblers?)\b/],
  ["massage gun", "muscle massage", /\bmassage guns?\b/],
  ["fan", "air cooling", /\b(?:desk |floor |standing |ceiling |portable )?fans?\b/],
  ["heater", "room heating", /\b(?:electric |fan |oil )?heaters?\b/],
  ["mattress topper", "mattress comfort", /\bmattress (?:toppers?|pads?)\b/],
  ["inflatable boat", "boating", /\b(?:inflatable boats?|dingh(?:y|ies))\b/],
  ["kayak", "boating", /\bkayaks?\b/],
  ["inflatable tent", "camping shelter", /\binflatable (?:[a-z]+ )?tents?\b/],
  ["camping tent", "camping shelter", /\b(?:camping |family |cabin )?tents?\b/],
  ["bookshelf", "book storage", /\b(?:bookshel(?:f|ves)|bookcases?)\b/],
  ["desk converter", "desk workspace", /\bdesk converters?\b/],
  ["standing desk", "desk workspace", /\b(?:standing|sit stand) desks?\b/],
  ["desk", "desk workspace", /\b(?:computer |writing |office |folding )?desks?\b/],
  ["christmas tree", "christmas decoration", /\bchristmas trees?\b/],
];
const ATTRIBUTES = new Set("wireless wired bluetooth usb rechargeable portable foldable folding adjustable electric manual sonic automatic robotic solar waterproof indoor outdoor magnetic wall mounted ceiling standing tripod fixed motorised motorized inflatable handheld stainless steel silicone wooden plastic led rgb digital analog universal".split(" "));
const BROAD_CATEGORY = /^(?:all|shop|home|home and kitchen|electronics|accessories|other|miscellaneous|computers|appliances|furniture|health and beauty)$/;

function normalize(value: unknown): string {
  return String(value ?? "").normalize("NFKC").toLowerCase()
    .replace(/&/g, " and ").replace(/[‐‑–—-]/g, " ").replace(/\s+/g, " ").trim();
}
function categoryKey(c: CompetitorCategoryBreadcrumb): string {
  if (normalize(c.id)) return `id:${normalize(c.id)}`;
  if (normalize(c.slug)) return `slug:${normalize(c.slug)}`;
  return normalize(c.name) ? `name:${normalize(c.type)}:${normalize(c.name)}` : "";
}
function intersection<T>(a: Set<T>, b: Set<T>): T[] { return [...a].filter((v) => b.has(v)); }

function titleEvidence(value: string) {
  const title = normalize(value);
  // Included extras do not turn the main product into an accessory.
  const subject = title.split(/\b(?:with|includes?|including|plus|and free)\b/)[0]!;
  const role = /\b(?:replacement|spare)\b|\b(?:brush heads?|replacement heads?|refills?|cartridges?|desk frames?|filters? for)\b/.test(subject)
    ? "replacement"
    : /\b(?:protective|protector|cases?|covers?|sleeves?|accessor(?:y|ies)|mounting brackets?|carrying bags?|carry bags?|(?:wall|ceiling|projector|tv) mounts?)\b/.test(subject)
      ? "accessory" : "main";
  // Match the longest named product before shorter contained names, e.g. screen
  // before projector and electric toothbrush before toothbrush.
  const family = FAMILIES.find(([, , pattern]) => pattern.test(subject));
  const attrs = new Set((subject.match(/[a-z]+/g) ?? []).filter((w) => ATTRIBUTES.has(w)));
  const specs = new Map<string, Set<string>>();
  for (const m of title.matchAll(/\b(\d+(?:\.\d+)?)\s*(mah|wh|kw|w|kg|g|ml|l|mm|cm|inch|inches|hz|gb|tb|v|pack|pcs)\b/g)) {
    let number = Number(m[1]);
    let unit = m[2]!;
    if (unit === "cm") { number *= 10; unit = "mm"; }
    if (unit === "kg") { number *= 1000; unit = "g"; }
    if (unit === "l") { number *= 1000; unit = "ml"; }
    if (unit === "kw") { number *= 1000; unit = "w"; }
    if (unit === "inches") unit = "inch";
    const values = specs.get(unit) ?? new Set<string>();
    values.add(String(number)); specs.set(unit, values);
  }
  const withoutSpecs = subject.replace(/\b\d+(?:\.\d+)?\s*(mah|wh|kw|w|kg|g|ml|l|mm|cm|inch|inches|hz|gb|tb|v|pack|pcs)\b/g, " ");
  const models = new Set(withoutSpecs.match(/\b[a-z]+\d+[a-z0-9]*\b/g) ?? []);
  for (const m of withoutSpecs.matchAll(/\b(?:model|series|iphone|galaxy)\s+([a-z0-9]+)\b/g)) models.add(m[1]!);
  // Original uppercase short model prefixes: TB 15, Z 1, HD 200.
  for (const m of value.matchAll(/\b([A-Z]{1,3})\s+(\d{1,5}[A-Z]?)\b/g)) models.add(normalize(m[1]! + m[2]!));
  return { title, role, family, attrs, specs, models };
}

export function matchCompetitor(source: MatchSource, item: CompetitorItem): CompetitorMatch | null {
  if (normalize(source.plid).replace(/^plid/, "") === normalize(item.plid).replace(/^plid/, "")) return null;
  if (!source.商品?.trim() || !item.商品?.trim()) return null;
  const a = titleEvidence(source.商品), b = titleEvidence(item.商品);
  if (a.role !== b.role) return null;
  const ap = (source.类目路径 ?? []).filter((c) => categoryKey(c));
  const bp = (item.类目路径 ?? []).filter((c) => categoryKey(c));
  const leafA = ap.at(-1), leafB = bp.at(-1);
  const exact = Boolean(leafA && leafB && categoryKey(leafA) === categoryKey(leafB)
    && !BROAD_CATEGORY.test(normalize(leafA.name)));
  const adjacent = ap.length > 1 && bp.length > 1
    && ap.slice(1, -1).some((c) => bp.slice(1, -1).some((d) => categoryKey(c) === categoryKey(d)));
  const sameFamily = Boolean(a.family && b.family && a.family[0] === b.family[0]);
  const sameDemand = Boolean(a.family && b.family && a.family[1] === b.family[1]);
  // A known conflicting product identity overrides a noisy/shared category.
  if (a.family && b.family && !sameDemand) return null;
  const sharedAttrs = intersection(a.attrs, b.attrs);
  const sharedModels = intersection(a.models, b.models);
  const modelConflict = a.models.size > 0 && b.models.size > 0
    && (sharedModels.length !== a.models.size || sharedModels.length !== b.models.size);
  let sharedSpecCount = 0, specConflict = false;
  for (const [unit, values] of a.specs) {
    const other = b.specs.get(unit);
    if (!other) continue;
    if (intersection(values, other).length) sharedSpecCount++;
    else specConflict = true;
  }
  const identityWords = new Set(a.family?.[0].split(" ") ?? []);
  const extraAttrs = sharedAttrs.filter((word) => !identityWords.has(word));
  const strongTitle = sameFamily && (extraAttrs.length > 0 || sharedSpecCount > 0 || sharedModels.length > 0);
  // No category or neighbouring categories need corroborating product evidence.
  // A broad shared root never admits candidates on its own.
  if (!exact && !(adjacent && sameDemand && (strongTitle || !sameFamily))
    && !((!leafA || !leafB) && strongTitle && (sharedModels.length > 0 || sharedSpecCount > 0))) return null;
  // Accessories/refills require the same narrow identity even in one leaf.
  if (a.role !== "main" && (!sameFamily || modelConflict || specConflict)) return null;
  const differentForm = ["inflatable", "electric", "motorised", "motorized", "robotic", "solar", "sonic"]
    .some((word) => a.attrs.has(word) !== b.attrs.has(word));
  const attributeConflict = differentForm || [["wired", "wireless"], ["manual", "electric"], ["fixed", "motorised"], ["fixed", "motorized"]]
    .some(([left, right]) => (a.attrs.has(left!) && b.attrs.has(right!)) || (b.attrs.has(left!) && a.attrs.has(right!)));
  const near = sameFamily && !modelConflict && !specConflict && !attributeConflict
    && (sharedSpecCount > 0 || sharedModels.length > 0 || extraAttrs.length >= 2);
  const kind: MatchKind = near ? "near_identical" : "same_demand";
  const score = Math.min(near ? 98 : 84,
    (near ? 82 : 58) + (exact ? 8 : adjacent ? 3 : 0)
    + (sameFamily ? 4 : sameDemand ? 2 : 0) + Math.min(4, sharedAttrs.length)
    + Math.min(4, sharedSpecCount * 2) + (sharedModels.length ? 4 : 0));
  const reasons = [
    exact ? `相同精确类目：${leafA!.name}` : adjacent ? "相邻类目，并有明确商品主体证据" : "类目缺失，按商品主体与型号/规格交叉核对",
    sameFamily ? `相同商品主体：${a.family![0]}` : sameDemand ? `同一用途：${a.family![1]}` : "同类目候选，商品形态待核对",
    sharedModels.length ? `型号相同：${sharedModels.join("、")}` : "",
    sharedSpecCount ? `${sharedSpecCount} 项数字规格一致` : "",
    sharedAttrs.length ? `共有特征：${sharedAttrs.slice(0, 3).join("、")}` : "",
    modelConflict || specConflict || attributeConflict ? "型号、规格或工作方式不同，归入替代竞品" : "",
  ].filter(Boolean);
  return { item, kind, score, reasons };
}

export function findCompetitorMatches(source: MatchSource, candidates: readonly CompetitorItem[]): CompetitorMatch[] {
  const byPlid = new Map<string, CompetitorItem>();
  for (const item of candidates) {
    const key = normalize(item.plid).replace(/^plid/, "");
    if (!key) continue;
    const existing = byPlid.get(key);
    if (!existing || (item.来源 === "own_store" && existing.来源 !== "own_store")) byPlid.set(key, item);
  }
  return [...byPlid.values()].map((item) => matchCompetitor(source, item))
    .filter((match): match is CompetitorMatch => Boolean(match))
    .sort((a, b) => a.kind.localeCompare(b.kind) || b.score - a.score
      // Commercial evidence only breaks ties AFTER type and semantic score.
      || (b.item.最新评论数 ?? -1) - (a.item.最新评论数 ?? -1)
      || (b.item.周期销售件数 ?? -1) - (a.item.周期销售件数 ?? -1)
      || a.item.plid.localeCompare(b.item.plid));
}
