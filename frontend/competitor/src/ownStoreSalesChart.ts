import type { OwnStoreSalesPoint } from "./types";

export const OWN_STORE_SALES_CHART = {
  width: 960,
  height: 260,
  plotLeft: 58,
  plotRight: 940,
  plotTop: 28,
  plotBottom: 208,
} as const;

export interface OwnStoreSalesChartPoint {
  index: number;
  date: string;
  units: number | null;
  status: OwnStoreSalesPoint["data_status"];
  revisionCount: number;
  x: number;
  y: number | null;
  barX: number;
  barY: number | null;
  barWidth: number;
  barHeight: number | null;
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

export function buildOwnStoreSalesChart(
  source: OwnStoreSalesPoint[],
): OwnStoreSalesChartGeometry {
  const { plotLeft, plotRight, plotTop, plotBottom } = OWN_STORE_SALES_CHART;
  const yMaximum = niceSalesMaximum(
    source.reduce(
      (maximum, point) => Math.max(maximum, point.ordered_units ?? 0),
      0,
    ),
  );
  const slotWidth = (plotRight - plotLeft) / Math.max(1, source.length);
  const barWidth = round(Math.max(1.25, Math.min(18, slotWidth * 0.72)));
  const points = source.map((point, index) => {
    const x = plotLeft + slotWidth * (index + 0.5);
    const y =
      point.ordered_units === null
        ? null
        : plotBottom -
          ((plotBottom - plotTop) * point.ordered_units) / yMaximum;
    const barHeight =
      y === null ? null : round(Math.max(2, plotBottom - y));
    return {
      index,
      date: point.date,
      units: point.ordered_units,
      status: point.data_status,
      revisionCount: point.revision_count,
      x,
      y,
      barX: round(x - barWidth / 2),
      barY: barHeight === null ? null : round(plotBottom - barHeight),
      barWidth,
      barHeight,
    };
  });

  const yValues = [yMaximum, yMaximum / 2, 0];
  const yTicks = yValues.map((value) => ({
    value,
    label: numberLabel(value),
    y: plotBottom - ((plotBottom - plotTop) * value) / yMaximum,
  }));
  const xIndexes = uniqueIndexes(source.length);
  const xTicks = xIndexes.map((index, position) => ({
    label: shortDate(source[index]?.date ?? ""),
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
  const magnitude = 10 ** Math.floor(Math.log10(maximum));
  const normalized = maximum / magnitude;
  const ceiling = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return ceiling * magnitude;
}

function uniqueIndexes(length: number): number[] {
  if (length <= 0) return [];
  return [...new Set([0, Math.floor((length - 1) / 2), length - 1])];
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
