import type {
  OwnStoreSalesPoint,
  OwnStoreSalesSeries,
  OwnStoreVariantSalesSeries,
} from "./types";

export const OWN_STORE_SALES_SUMMARY_DAYS = [7, 15, 30, 60, 90] as const;

export interface OwnStoreSalesWindowSummary {
  days: typeof OWN_STORE_SALES_SUMMARY_DAYS[number];
  expectedDays: number;
  verifiedDays: number;
  partialDays: number;
  missingDays: number;
  orderedUnits: number | null;
}

const DAY_MS = 86_400_000;
const ISO_DATE_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;

export function selectOwnStoreSalesSeries(
  series: OwnStoreSalesSeries[],
  preferredStoreCode?: string | null,
): OwnStoreSalesSeries | null {
  const preferred = String(preferredStoreCode ?? "").trim().toLocaleLowerCase();
  return (
    series.find((item) => item.store_code.toLocaleLowerCase() === preferred)
    ?? series[0]
    ?? null
  );
}

export function selectOwnStoreVariantSalesSeries(
  series: OwnStoreVariantSalesSeries[],
  offerId: string | null | undefined,
  preferredStoreCode?: string | null,
): OwnStoreVariantSalesSeries | null {
  const exactOfferId = String(offerId ?? "").trim();
  if (!exactOfferId) return null;
  const exactSeries = series.filter((item) => item.offer_id === exactOfferId);
  const preferredStore = String(preferredStoreCode ?? "").trim().toLocaleLowerCase();
  return (
    exactSeries.find(
      (item) => item.store_code.toLocaleLowerCase() === preferredStore,
    )
    ?? exactSeries[0]
    ?? null
  );
}

export function summarizeOwnStoreSalesWindows(
  series: OwnStoreSalesSeries,
): OwnStoreSalesWindowSummary[] {
  return OWN_STORE_SALES_SUMMARY_DAYS.map((days) =>
    summarizeOwnStoreSalesWindow(series, days));
}

export function summarizeOwnStoreSalesWindow(
  series: OwnStoreSalesSeries,
  days: typeof OWN_STORE_SALES_SUMMARY_DAYS[number],
): OwnStoreSalesWindowSummary {
  const pointDates = series.points
    .map((point) => normalizedIsoDate(point.date))
    .filter((value): value is string => value !== null)
    .sort((left, right) => left.localeCompare(right));
  const listingDate = normalizedIsoDate(series.listing_date) ?? pointDates[0] ?? null;
  const throughDate = normalizedIsoDate(series.through_date) ?? pointDates.at(-1) ?? null;
  if (!listingDate || !throughDate || listingDate > throughDate) {
    return {
      days,
      expectedDays: 0,
      verifiedDays: 0,
      partialDays: 0,
      missingDays: 0,
      orderedUnits: null,
    };
  }

  const nominalStart = shiftIsoDate(throughDate, -(days - 1));
  const startDate = nominalStart && nominalStart > listingDate
    ? nominalStart
    : listingDate;
  const expectedDays = inclusiveDayCount(startDate, throughDate);
  const pointsByDate = new Map<string, OwnStoreSalesPoint>();
  for (const point of series.points) {
    const pointDate = normalizedIsoDate(point.date);
    if (!pointDate || pointDate < startDate || pointDate > throughDate) continue;
    pointsByDate.set(pointDate, point);
  }
  const points = [...pointsByDate.values()];
  const verifiedDays = points.filter((point) => point.data_status === "verified").length;
  const partialDays = points.filter((point) => point.data_status === "partial").length;
  const explicitMissingDays = points.filter((point) => point.data_status === "missing").length;
  const missingDays = explicitMissingDays + Math.max(0, expectedDays - points.length);
  const knownPoints = points.filter(
    (point): point is OwnStoreSalesPoint & { ordered_units: number } =>
      point.ordered_units !== null,
  );

  return {
    days,
    expectedDays,
    verifiedDays,
    partialDays,
    missingDays,
    orderedUnits: knownPoints.length
      ? knownPoints.reduce((total, point) => total + point.ordered_units, 0)
      : null,
  };
}

function normalizedIsoDate(value: string | null | undefined): string | null {
  const match = ISO_DATE_PATTERN.exec(String(value ?? ""));
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const timestamp = Date.UTC(year, month - 1, day);
  const normalized = new Date(timestamp).toISOString().slice(0, 10);
  return normalized === value ? normalized : null;
}

function shiftIsoDate(value: string, dayOffset: number): string | null {
  const timestamp = isoDateTimestamp(value);
  if (timestamp === null) return null;
  return new Date(timestamp + dayOffset * DAY_MS).toISOString().slice(0, 10);
}

function inclusiveDayCount(startDate: string, endDate: string): number {
  const start = isoDateTimestamp(startDate);
  const end = isoDateTimestamp(endDate);
  if (start === null || end === null || start > end) return 0;
  return Math.floor((end - start) / DAY_MS) + 1;
}

function isoDateTimestamp(value: string): number | null {
  const normalized = normalizedIsoDate(value);
  if (!normalized) return null;
  const [year, month, day] = normalized.split("-").map(Number);
  return Date.UTC(year, (month ?? 1) - 1, day ?? 1);
}
