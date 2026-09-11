// Formatting objects are expensive to construct inside a card/chart render.
// Keep a small shared pool while retaining each caller's locale and precision.
const formatters = new Map<string, Intl.NumberFormat>();
const maxFormatters = 32;

export function cachedNumberFormatter(
  locale: string,
  options?: Intl.NumberFormatOptions,
): Intl.NumberFormat {
  const key = JSON.stringify([locale, options]);
  const existing = formatters.get(key);
  if (existing) return existing;
  const formatter = new Intl.NumberFormat(locale, options);
  if (formatters.size >= maxFormatters) {
    formatters.delete(formatters.keys().next().value!);
  }
  formatters.set(key, formatter);
  return formatter;
}
