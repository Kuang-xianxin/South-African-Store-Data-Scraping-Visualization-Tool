import type { CompetitorMatchSource } from "./competitorSimilarity.ts";

// Descriptors may rank alternatives but cannot establish a product type alone.
// Product names come from the titles/leaf labels, not a list of supported families.
const DESCRIPTORS = new Set((
  "a an and or the of in on to for with without by from & "
  + "black white red blue green yellow grey gray pink purple brown silver gold matt matte "
  + "stainless steel metal plastic wooden wood glass silicone cotton premium deluxe quality "
  + "large small mini compact capacity commercial industrial professional electric electrical "
  + "portable foldable folding adjustable automatic digital wireless rechargeable universal "
  + "home kitchen outdoor indoor business bussiness travel adult child infant new use "
  + "heavy duty high low power performance energy saving efficient durable lightweight "
  + "waterproof resistant fast quick easy clean best latest modern style design suitable "
  + "machine device equipment appliance accessory supply product tool kit set pack piece "
  + "kg g litre liter ml cm mm inch metre meter watt volt speed hour hr w v l"
).split(/\s+/));

const ACCESSORY = /\b(?:accessor(?:y|ies)|attachment|bracket|case|cover|holder|parts?|protector|refill|replacement|sleeve|spare)\b/i;
const DEPENDENT_HEAD = /^(?:cleaner|cleaning|descaler|detergent|solution|filter|tray|mould|mold|bag|brush|cable|adapter|mount|stand|screen|case|cover|protector|holder|blade|basket|liner|pad|part|replacement)\b/;

function words(value: string): string[] {
  return value.normalize("NFKC").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ").trim().split(/\s+/).filter(Boolean)
    .map((word) => {
      if (word.length > 4 && word.endsWith("ies")) return word.slice(0, -3) + "y";
      if (/(?:sses|ches|shes|xes|zes)$/.test(word)) return word.slice(0, -2);
      if (word.length > 3 && word.endsWith("s") && !/(?:ss|us|is)$/.test(word)) return word.slice(0, -1);
      return word;
    });
}

function significant(word: string): boolean {
  return word.length > 1 && !/\d/.test(word) && !DESCRIPTORS.has(word);
}

interface Phrase { key: string; label: string; start: number; end: number }
function phrases(tokens: readonly string[]): Phrase[] {
  const result: Phrase[] = [];
  for (let start = 0; start < tokens.length; start += 1) {
    for (let width = 1; width <= 4 && start + width <= tokens.length; width += 1) {
      const part = tokens.slice(start, start + width);
      if (!part.every(significant)) continue;
      result.push({ key: part.join(""), label: part.join(" "), start, end: start + width });
    }
  }
  return result;
}

export interface ProductIdentity {
  forms: Map<string, Phrase[]>;
  types: Map<string, string>;
  categoryTypes: Map<string, string>;
  accessory: boolean;
  tokens: string[];
}

const cache = new WeakMap<CompetitorMatchSource, { signature: string; value: ProductIdentity }>();
export function productIdentity(item: CompetitorMatchSource): ProductIdentity {
  const leaf = item.类目路径?.at(-1)?.name ?? "";
  const signature = item.商品 + "\0" + leaf;
  const hit = cache.get(item);
  if (hit?.signature === signature) return hit.value;
  const tokens = words(item.商品);
  const forms = new Map<string, Phrase[]>();
  for (const phrase of phrases(tokens)) {
    const entries = forms.get(phrase.key) ?? [];
    entries.push(phrase); forms.set(phrase.key, entries);
  }
  const types = new Map<string, string>();
  // A leaf is supporting vocabulary only when the title actually contains it.
  for (const part of leaf.split(/\s*(?:&|\/|\band\b|\bor\b)\s*/i)) {
    const categoryTokens = words(part);
    for (const phrase of phrases(categoryTokens)) {
      if (phrase.end === categoryTokens.length && forms.has(phrase.key)
        && !(phrase.end - phrase.start === 1 && (phrase.key.length < 6
          || ["maker", "storage", "holder"].includes(phrase.key)))) {
        types.set(phrase.key, phrase.label);
      }
    }
  }
  const categoryTypes = new Map(types);
  // A title's core ending also works with missing or incorrectly assigned leaves.
  // Specifications/colour after the name do not dilute that name's evidence.
  const qualifier = tokens.findIndex((word) => ["for", "with", "including", "includes"].includes(word));
  const main = tokens.slice(0, qualifier < 0 ? tokens.length : qualifier);
  while (main.length && !significant(main.at(-1)!)) main.pop();
  for (const phrase of phrases(main)) {
    if (phrase.end === main.length && phrase.end - phrase.start >= 2) types.set(phrase.key, phrase.label);
  }
  // An included carrying case is different from a case sold for an appliance.
  const bundled = item.商品.search(/\b(?:with|including|includes)\b/i);
  const accessory = ACCESSORY.test(bundled < 0 ? item.商品 : item.商品.slice(0, bundled));
  const value = { forms, types, categoryTypes, accessory, tokens };
  cache.set(item, { signature, value });
  return value;
}

function mainOccurrence(identity: ProductIdentity, key: string): boolean {
  return (identity.forms.get(key) ?? []).some((phrase) => {
    const before = identity.tokens.slice(0, phrase.start);
    const after = identity.tokens.slice(phrase.end).join(" ");
    // Compatibility targets and a product name followed by a consumable/part
    // identify what an accessory fits, not what is being sold.
    return !before.some((word) => ["for", "with", "including", "includes", "compatible", "fits", "replacement"].includes(word))
      && !DEPENDENT_HEAD.test(after);
  });
}

export function sharedProductType(left: ProductIdentity, right: ProductIdentity): string | null {
  if (left.accessory !== right.accessory) return null;
  const options = [...left.types, ...right.types].filter(([key]) =>
    mainOccurrence(left, key) && mainOccurrence(right, key));
  // Do not fall back to a shared generic head (e.g. coffee grinder / meat grinder)
  // when both titles provide a more specific name ending with that head.
  const supported = options.filter(([key]) => [left, right].every((identity) =>
    ![...identity.categoryTypes.keys()].some((specific) => specific !== key && specific.endsWith(key)
      && !options.some(([other]) => other === specific))));
  return supported.sort((a, b) => b[0].length - a[0].length || a[0].localeCompare(b[0]))[0]?.[1] ?? null;
}

export function dependentProductReference(left: ProductIdentity, right: ProductIdentity): boolean {
  return [...left.types.keys()].some((key) => mainOccurrence(left, key)
    && right.forms.has(key) && !mainOccurrence(right, key));
}

export function conflictingProductHeads(left: ProductIdentity, right: ProductIdentity): boolean {
  const endings = (identity: ProductIdentity) => [...identity.types.values()]
    .map((label) => label.split(" ")).filter((parts) => parts.length === 2);
  return endings(left).some((a) => endings(right).some((b) => a[1] === b[1] && a[0] !== b[0]
    && (!right.tokens.includes(a[0]!) || !left.tokens.includes(b[0]!))));
}
