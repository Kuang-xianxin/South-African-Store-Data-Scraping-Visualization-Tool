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
  const monthStart = /^(\d{4})-(\d{2})-01$/.exec(startDate);
  if (monthStart && startDate.slice(0, 7) === endDate.slice(0, 7)) {
    const month = Number.parseInt(monthStart[2], 10);
    return {
      total: `${month}月总销售额`,
      dailyAverage: `${month}月内日均销售额`,
    };
  }
  return {
    total: "所选区间总销售额",
    dailyAverage: "所选区间日均销售额",
  };
}
