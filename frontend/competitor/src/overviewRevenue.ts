export interface RevenueDayInput {
  amount: number | null | undefined;
  partial?: boolean;
  pending?: boolean;
}

export interface RevenuePeriodSummary {
  total: number | null;
  dailyAverage: number | null;
  knownDayCount: number;
  missingDayCount: number;
  partialDayCount: number;
  pendingDayCount: number;
}

export interface RevenuePeriodLabels {
  total: string;
  dailyAverage: string;
  projectedTotal: string;
}

export interface RevenueMonthProjection {
  projectedTotal: number | null;
  monthDayCount: number | null;
}

export function summarizeRevenuePeriod(
  days: readonly RevenueDayInput[],
): RevenuePeriodSummary {
  const knownDays = days.filter(
    (day): day is RevenueDayInput & { amount: number } =>
      typeof day.amount === "number" && Number.isFinite(day.amount),
  );
  const total = knownDays.length
    ? knownDays.reduce((sum, day) => sum + day.amount, 0)
    : null;

  return {
    total,
    dailyAverage: total === null ? null : total / knownDays.length,
    knownDayCount: knownDays.length,
    missingDayCount: days.length - knownDays.length,
    partialDayCount: knownDays.filter((day) => day.partial).length,
    pendingDayCount: knownDays.filter((day) => day.pending).length,
  };
}

export function revenuePeriodLabels(
  startDate: string,
  endDate: string,
): RevenuePeriodLabels {
  const monthViewport = naturalMonthViewport(startDate, endDate);
  if (monthViewport) {
    return {
      total: `${monthViewport.month}月总销售额`,
      dailyAverage: `${monthViewport.month}月内日均销售额`,
      projectedTotal: `预计${monthViewport.month}月总销售额`,
    };
  }
  return {
    total: "所选区间总销售额",
    dailyAverage: "所选区间日均销售额",
    projectedTotal: "预计月总销售额",
  };
}

export function projectRevenueMonthTotal(
  dailyAverage: number | null,
  startDate: string,
  endDate: string,
): RevenueMonthProjection {
  const monthViewport = naturalMonthViewport(startDate, endDate);
  if (!monthViewport) {
    return { projectedTotal: null, monthDayCount: null };
  }
  return {
    projectedTotal: dailyAverage === null
      ? null
      : dailyAverage * monthViewport.dayCount,
    monthDayCount: monthViewport.dayCount,
  };
}

function naturalMonthViewport(
  startDate: string,
  endDate: string,
): { dayCount: number; month: number } | null {
  const monthStart = /^(\d{4})-(\d{2})-01$/.exec(startDate);
  if (!monthStart || startDate.slice(0, 7) !== endDate.slice(0, 7)) return null;
  const year = Number.parseInt(monthStart[1], 10);
  const month = Number.parseInt(monthStart[2], 10);
  if (month < 1 || month > 12) return null;
  const endDay = Number.parseInt(endDate.slice(8, 10), 10);
  const dayCount = new Date(Date.UTC(year, month, 0)).getUTCDate();
  if (!Number.isInteger(endDay) || endDay < 1 || endDay > dayCount) return null;
  return { dayCount, month };
}
