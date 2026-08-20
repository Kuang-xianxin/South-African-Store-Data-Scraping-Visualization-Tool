import type { OwnStoreSalesPoint } from "./types";

export const OWN_STORE_SALES_CHART = {
  width: 1200,
  height: 240,
  plotLeft: 64,
  plotRight: 1180,
  plotTop: 26,
  plotBottom: 188,
} as const;

export type OwnStoreSalesGranularity = "day" | "week" | "month";
export type OwnStoreSalesGranularityRequest = OwnStoreSalesGranularity | "auto";

export interface OwnStoreSalesBucket {
  endDate: string;
  granularity: OwnStoreSalesGranularity;
  missingDays: number;
  partialDays: number;
  revisionCount: number;
  salesDays: number;
  startDate: string;
  status: OwnStoreSalesPoint["data_status"];
  totalDays: number;
  units: number | null;
  verifiedDays: number;
}

export interface OwnStoreSalesAggregation {
  buckets: OwnStoreSalesBucket[];
  granularity: OwnStoreSalesGranularity;
}

export interface OwnStoreSalesChartPoint {
  endDate: string;
  index: number;
  granularity: OwnStoreSalesGranularity;
  missingDays: number;
  partialDays: number;
  salesDays: number;
  startDate: string;
  totalDays: number;
  units: number | null;
  status: OwnStoreSalesPoint["data_status"];
  revisionCount: number;
  verifiedDays: number;
  x: number;
  y: number | null;
  barX: number;
  barY: number | null;
  barWidth: number;
  barHeight: number | null;
  focusX: number;
  focusWidth: number;
}

export interface OwnStoreSalesChartTick {
  value?: number;
  label: string;
  x?: number;
  y?: number;
  anchor?: "start" | "middle" | "end";
}

export interface OwnStoreSalesChartGeometry {
  points: OwnStoreSalesChartPoint[];
  yTicks: OwnStoreSalesChartTick[];
  xTicks: OwnStoreSalesChartTick[];
  yMaximum: number;
}

export interface OwnStoreSalesDateBounds {
  start: string;
  end: string;
}

export function getOwnStoreSalesDateBounds(
  source: OwnStoreSalesPoint[],
): OwnStoreSalesDateBounds | null {
  if (!source.length) return null;
  const dates = source
    .map((point) => point.date)
    .filter(Boolean)
    .sort((left, right) => left.localeCompare(right));
  if (!dates.length) return null;
  return {
    start: dates[0] ?? "",
    end: dates[dates.length - 1] ?? "",
  };
}

export function filterOwnStoreSalesPoints(
  source: OwnStoreSalesPoint[],
  startDate: string,
  endDate: string,
): OwnStoreSalesPoint[] {
  const lowerBound = startDate <= endDate ? startDate : endDate;
  const upperBound = startDate <= endDate ? endDate : startDate;
  return source.filter(
    (point) => point.date >= lowerBound && point.date <= upperBound,
  );
}

export function aggregateOwnStoreSalesPoints(
  source: OwnStoreSalesPoint[],
  requestedGranularity: OwnStoreSalesGranularityRequest = "auto",
): OwnStoreSalesAggregation {
  const points = [...source].sort((left, right) => left.date.localeCompare(right.date));
  const granularity = requestedGranularity === "auto"
    ? automaticGranularity(points.length)
    : requestedGranularity;
  const groups = new Map<string, OwnStoreSalesPoint[]>();
  for (const point of points) {
    const key = bucketKey(point.date, granularity);
    const group = groups.get(key) ?? [];
    group.push(point);
    groups.set(key, group);
  }
  const buckets = [...groups.values()].map((group): OwnStoreSalesBucket => {
    const knownPoints = group.filter(
      (point): point is OwnStoreSalesPoint & { ordered_units: number } =>
        point.ordered_units !== null,
    );
    const verifiedDays = group.filter((point) => point.data_status === "verified").length;
    const partialDays = group.filter((point) => point.data_status === "partial").length;
    const missingDays = group.filter((point) => point.data_status === "missing").length;
    const units = knownPoints.length
      ? knownPoints.reduce((total, point) => total + point.ordered_units, 0)
      : null;
    const status: OwnStoreSalesPoint["data_status"] = units === null
      ? "missing"
      : verifiedDays === group.length
        ? "verified"
        : "partial";
    return {
      endDate: group.at(-1)?.date ?? "",
      granularity,
      missingDays,
      partialDays,
      revisionCount: group.reduce((total, point) => total + point.revision_count, 0),
      salesDays: knownPoints.filter((point) => point.ordered_units > 0).length,
      startDate: group[0]?.date ?? "",
      status,
      totalDays: group.length,
      units,
      verifiedDays,
    };
  });
  return { buckets, granularity };
}

export function getOwnStoreSalesRecentRange(
  bounds: OwnStoreSalesDateBounds,
  dayCount: number,
): OwnStoreSalesDateBounds {
  if (!Number.isInteger(dayCount) || dayCount <= 0) return bounds;
  const recentStart = shiftIsoDate(bounds.end, -(dayCount - 1));
  return {
    start: recentStart && recentStart > bounds.start ? recentStart : bounds.start,
    end: bounds.end,
  };
}

export function buildOwnStoreSalesChart(
  source: OwnStoreSalesBucket[],
): OwnStoreSalesChartGeometry {
  const { plotLeft, plotRight, plotTop, plotBottom } = OWN_STORE_SALES_CHART;
  const sourceMaximum = source.reduce(
    (maximum, point) => Math.max(maximum, point.units ?? 0),
    0,
  );
  const yMaximum = niceSalesMaximum(sourceMaximum);
  const slotWidth = (plotRight - plotLeft) / Math.max(1, source.length);
  const barWidth = round(Math.max(1.5, Math.min(42, slotWidth * 0.72)));
  const focusWidth = round(Math.min(58, Math.max(barWidth + 10, slotWidth * 0.84)));
  const points = source.map((point, index) => {
    const x = plotLeft + slotWidth * (index + 0.5);
    const y =
      point.units === null
        ? null
        : plotBottom -
          ((plotBottom - plotTop) * point.units) / yMaximum;
    const barHeight = y === null
      ? null
      : round(Math.max(point.units === 0 ? 4 : 3, plotBottom - y));
    return {
      endDate: point.endDate,
      index,
      granularity: point.granularity,
      missingDays: point.missingDays,
      partialDays: point.partialDays,
      revisionCount: point.revisionCount,
      salesDays: point.salesDays,
      startDate: point.startDate,
      status: point.status,
      totalDays: point.totalDays,
      units: point.units,
      verifiedDays: point.verifiedDays,
      x,
      y,
      barX: round(x - barWidth / 2),
      barY: barHeight === null ? null : round(plotBottom - barHeight),
      barWidth,
      barHeight,
      focusX: round(x - focusWidth / 2),
      focusWidth,
    };
  });

  const yValues = sourceMaximum > 0 ? integerTickValues(yMaximum) : [0];
  const yTicks = yValues.map((value) => ({
    value,
    label: numberLabel(value),
    y: plotBottom - ((plotBottom - plotTop) * value) / yMaximum,
  }));
  const xIndexes = uniqueIndexes(source.length);
  const xTicks = xIndexes.map((index, position) => ({
    label: bucketAxisLabel(source[index]),
    x: points[index]?.x ?? plotLeft,
    anchor:
      position === 0
        ? ("start" as const)
        : position === xIndexes.length - 1
          ? ("end" as const)
          : ("middle" as const),
  }));
  return { points, yTicks, xTicks, yMaximum };
}

export function nearestOwnStoreSalesPointIndex(
  localPointerX: number,
  renderedWidth: number,
  pointCount: number,
): number {
  if (pointCount <= 1 || renderedWidth <= 0) return 0;
  const { width, plotLeft, plotRight } = OWN_STORE_SALES_CHART;
  const viewBoxX = (localPointerX / renderedWidth) * width;
  const ratio = Math.max(
    0,
    Math.min(1, (viewBoxX - plotLeft) / (plotRight - plotLeft)),
  );
  return Math.min(pointCount - 1, Math.floor(ratio * pointCount));
}

function niceSalesMaximum(maximum: number): number {
  if (!Number.isFinite(maximum) || maximum <= 0) return 1;
  const step = niceSalesTickStep(maximum / 5);
  return Math.ceil(maximum / step) * step;
}

function integerTickValues(maximum: number): number[] {
  const step = niceSalesTickStep(maximum / 5);
  const values: number[] = [];
  for (let value = maximum; value > 0; value -= step) {
    values.push(value);
  }
  values.push(0);
  return values;
}

function niceSalesTickStep(roughStep: number): number {
  if (!Number.isFinite(roughStep) || roughStep <= 1) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(roughStep));
  const normalized = roughStep / magnitude;
  const step = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return Math.max(1, step * magnitude);
}

function uniqueIndexes(length: number): number[] {
  if (length <= 0) return [];
  const maximumTickCount = 7;
  if (length <= maximumTickCount) {
    return Array.from({ length }, (_, index) => index);
  }
  return [...new Set(
    Array.from(
      { length: maximumTickCount },
      (_, index) => Math.round((index * (length - 1)) / (maximumTickCount - 1)),
    ),
  )];
}

function automaticGranularity(pointCount: number): OwnStoreSalesGranularity {
  if (pointCount <= 45) return "day";
  if (pointCount <= 420) return "week";
  return "month";
}

function bucketKey(date: string, granularity: OwnStoreSalesGranularity): string {
  if (granularity === "day") return date;
  if (granularity === "month") return date.slice(0, 7);
  return isoWeekStart(date) || date;
}

function bucketAxisLabel(bucket: OwnStoreSalesBucket | undefined): string {
  if (!bucket) return "";
  if (bucket.granularity === "month") return bucket.startDate.slice(0, 7).replace("-", "/");
  return shortDate(bucket.startDate);
}

function isoWeekStart(value: string): string {
  const parsed = parseIsoDate(value);
  if (!parsed) return "";
  const mondayOffset = (parsed.getUTCDay() + 6) % 7;
  parsed.setUTCDate(parsed.getUTCDate() - mondayOffset);
  return parsed.toISOString().slice(0, 10);
}

function shiftIsoDate(value: string, dayDelta: number): string {
  const parsed = parseIsoDate(value);
  if (!parsed) return "";
  parsed.setUTCDate(parsed.getUTCDate() + dayDelta);
  return parsed.toISOString().slice(0, 10);
}

function parseIsoDate(value: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function shortDate(value: string): string {
  return value.slice(5).replace("-", "/");
}

function numberLabel(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

function round(value: number): number {
  return Math.round(value * 100) / 100;
}
