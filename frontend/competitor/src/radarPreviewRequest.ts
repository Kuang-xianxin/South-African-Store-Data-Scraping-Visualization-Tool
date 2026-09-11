export interface RadarReadResult<T> {
  value: T;
  refreshing: boolean;
  generatedAt: string;
}

/** Show a complete previous read, then join the server's one fresh read. */
export async function readRadarWithPreview<T>(
  load: (preferCached: boolean) => Promise<RadarReadResult<T>>,
  onPreview: ((value: T, generatedAt: string) => void) | undefined,
  isCurrent: () => boolean,
): Promise<T> {
  const first = await load(Boolean(onPreview));
  if (!isCurrent()) throw new DOMException("Session changed", "AbortError");
  if (!first.refreshing || !onPreview) return first.value;
  onPreview(first.value, first.generatedAt);
  const fresh = await load(false);
  if (!isCurrent()) throw new DOMException("Session changed", "AbortError");
  return fresh.value;
}
