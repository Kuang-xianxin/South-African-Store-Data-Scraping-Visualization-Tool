export type DateViewportMode = "month" | "custom";

export interface DateViewport {
  startDate: string;
  endDate: string;
  mode: DateViewportMode;
}

type ChangedBoundary = "start" | "end";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function validIsoDate(value: string) {
  if (!ISO_DATE.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1
    && parsed.getUTCDate() === day;
}

function requireIsoDate(value: string, label: string) {
  if (!validIsoDate(value)) throw new Error(`${label}必须是有效的 YYYY-MM-DD 日期`);
}

function monthKey(value: string) {
  return value.slice(0, 7);
}

function formatUtcDate(value: Date) {
  return value.toISOString().slice(0, 10);
}

function monthEnd(month: string) {
  const [year, monthNumber] = month.split("-").map(Number);
  return formatUtcDate(new Date(Date.UTC(year, monthNumber, 0)));
}

export function calendarMonthViewport(
  anchorDate: string,
  today: string,
): DateViewport {
  requireIsoDate(anchorDate, "锚点日期");
  requireIsoDate(today, "今天");
  const currentMonth = monthKey(today);
  const selectedMonth = monthKey(anchorDate) > currentMonth
    ? currentMonth
    : monthKey(anchorDate);
  return {
    startDate: `${selectedMonth}-01`,
    endDate: selectedMonth === currentMonth ? today : monthEnd(selectedMonth),
    mode: "month",
  };
}

export function shiftMonthViewport(
  anchorDate: string,
  offset: number,
  today: string,
): DateViewport {
  requireIsoDate(anchorDate, "锚点日期");
  requireIsoDate(today, "今天");
  const [year, month] = monthKey(anchorDate).split("-").map(Number);
  const shifted = new Date(Date.UTC(year, month - 1 + offset, 1));
  return calendarMonthViewport(formatUtcDate(shifted), today);
}

export function normalizeCustomViewport(
  startDate: string,
  endDate: string,
  today: string,
  changedBoundary: ChangedBoundary,
): DateViewport {
  requireIsoDate(today, "今天");
  let nextStart = validIsoDate(startDate) ? startDate : today;
  let nextEnd = validIsoDate(endDate) ? endDate : today;
  nextStart = nextStart > today ? today : nextStart;
  nextEnd = nextEnd > today ? today : nextEnd;
  if (nextStart > nextEnd) {
    if (changedBoundary === "start") nextEnd = nextStart;
    else nextStart = nextEnd;
  }
  return {
    startDate: nextStart,
    endDate: nextEnd,
    mode: "custom",
  };
}

export function canMoveToNextMonth(startDate: string, today: string) {
  requireIsoDate(startDate, "开始日期");
  requireIsoDate(today, "今天");
  return monthKey(startDate) < monthKey(today);
}
