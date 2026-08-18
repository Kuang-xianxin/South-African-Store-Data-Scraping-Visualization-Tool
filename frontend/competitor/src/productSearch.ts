export interface ProductSearchFields {
  productNames?: readonly unknown[];
  otherValues?: readonly unknown[];
}

const combiningMarks = /\p{M}+/gu;
const separators = /[^\p{L}\p{N}]+/gu;
const letters = /\p{L}/u;

export function normalizeProductSearchText(value: unknown): string {
  return String(value ?? "")
    .normalize("NFKD")
    .replace(combiningMarks, "")
    .toLocaleLowerCase()
    .replace(separators, " ")
    .trim()
    .replace(/\s+/g, " ");
}

export function matchesProductName(value: unknown, rawQuery: unknown): boolean {
  const name = normalizeProductSearchText(value);
  const query = normalizeProductSearchText(rawQuery);
  if (!query) return true;
  if (!name) return false;
  if (name.includes(query)) return true;

  const compactName = name.replace(/\s/g, "");
  const compactQuery = query.replace(/\s/g, "");
  if (compactName.includes(compactQuery)) return true;

  const nameTokens = name.split(" ").filter(Boolean);
  const queryTokens = query.split(" ").filter(Boolean);
  return queryTokens.every((queryToken) =>
    nameTokens.some((nameToken) => fuzzyTokenMatches(nameToken, queryToken)),
  );
}

export function matchesProductSearch(
  fields: ProductSearchFields,
  rawQuery: unknown,
): boolean {
  const exactQuery = String(rawQuery ?? "").trim().toLocaleLowerCase();
  if (!exactQuery) return true;

  if ((fields.otherValues ?? []).some((value) =>
    String(value ?? "").toLocaleLowerCase().includes(exactQuery),
  )) {
    return true;
  }

  return (fields.productNames ?? []).some((value) =>
    matchesProductName(value, rawQuery),
  );
}

function fuzzyTokenMatches(nameToken: string, queryToken: string): boolean {
  if (nameToken.includes(queryToken)) return true;
  if (!letters.test(queryToken) || !letters.test(nameToken)) return false;

  // Keep character order: this is bounded typo tolerance, never an anagram match.
  const maximumDistance = queryToken.length >= 9
    ? 2
    : queryToken.length >= 5
      ? 1
      : 0;
  if (!maximumDistance) return false;
  if (Math.abs(nameToken.length - queryToken.length) > maximumDistance) return false;
  return editDistanceWithin(nameToken, queryToken, maximumDistance);
}

function editDistanceWithin(left: string, right: string, limit: number): boolean {
  const leftCharacters = Array.from(left);
  const rightCharacters = Array.from(right);
  if (Math.abs(leftCharacters.length - rightCharacters.length) > limit) return false;

  const matrix = Array.from(
    { length: leftCharacters.length + 1 },
    () => new Array<number>(rightCharacters.length + 1).fill(0),
  );
  for (let index = 0; index <= leftCharacters.length; index += 1) matrix[index][0] = index;
  for (let index = 0; index <= rightCharacters.length; index += 1) matrix[0][index] = index;

  for (let leftIndex = 1; leftIndex <= leftCharacters.length; leftIndex += 1) {
    for (let rightIndex = 1; rightIndex <= rightCharacters.length; rightIndex += 1) {
      const substitutionCost = leftCharacters[leftIndex - 1] === rightCharacters[rightIndex - 1]
        ? 0
        : 1;
      matrix[leftIndex][rightIndex] = Math.min(
        matrix[leftIndex - 1][rightIndex] + 1,
        matrix[leftIndex][rightIndex - 1] + 1,
        matrix[leftIndex - 1][rightIndex - 1] + substitutionCost,
      );
      if (
        leftIndex > 1
        && rightIndex > 1
        && leftCharacters[leftIndex - 1] === rightCharacters[rightIndex - 2]
        && leftCharacters[leftIndex - 2] === rightCharacters[rightIndex - 1]
      ) {
        // One adjacent swap is a typing error, not arbitrary letter reordering.
        matrix[leftIndex][rightIndex] = Math.min(
          matrix[leftIndex][rightIndex],
          matrix[leftIndex - 2][rightIndex - 2] + 1,
        );
      }
    }
  }

  return matrix[leftCharacters.length][rightCharacters.length] <= limit;
}
