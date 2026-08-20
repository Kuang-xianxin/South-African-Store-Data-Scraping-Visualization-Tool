export type CompetitorOfferTrendPanelCount = 3 | 4;
export type CompetitorOfferTrendDensity = "standard" | "standalone-compact";

export interface CompetitorOfferTrendLayout {
  chartHeight: number;
  cursorBottom: number;
  dividerOffset: number;
  panelStride: number;
  panelTop: number;
  plotHeight: number;
  surfaceHeight: number;
  surfaceTopOffset: number;
  xAxisLabelY: number;
}

export const COMPETITOR_OFFER_TREND_HORIZONTAL_LAYOUT = {
  axisLabelX: 158,
  panelTextDividerX: 108,
  panelTextX: 12,
  plotLeft: 170,
  plotRight: 936,
} as const;

export function buildCompetitorOfferTrendLayout(
  panelCount: CompetitorOfferTrendPanelCount,
  density: CompetitorOfferTrendDensity = "standard",
): CompetitorOfferTrendLayout {
  const standaloneCompact = density === "standalone-compact";
  const panelStride = standaloneCompact
    ? (panelCount === 4 ? 66 : 78)
    : (panelCount === 4 ? 88 : 102);
  const plotHeight = standaloneCompact
    ? (panelCount === 4 ? 42 : 52)
    : (panelCount === 4 ? 58 : 70);
  const chartHeight = 40 + panelCount * panelStride;

  return {
    chartHeight,
    cursorBottom: chartHeight - 40,
    dividerOffset: 14,
    panelStride,
    panelTop: 18,
    plotHeight,
    surfaceHeight: panelStride - 8,
    surfaceTopOffset: 10,
    xAxisLabelY: chartHeight - 14,
  };
}
