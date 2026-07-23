const OFFSET_SUFFIX = /(Z|[+-]\d{2}:\d{2})$/i;

export function formatChinaDateTime(
  value: string | null,
  fallback = "—",
): string {
  if (!value) return fallback;
  const utcValue = OFFSET_SUFFIX.test(value) ? value : `${value}Z`;
  const parsed = new Date(utcValue);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(parsed);
}
