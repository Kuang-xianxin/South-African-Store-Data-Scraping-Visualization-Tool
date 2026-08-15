export type CompetitorOfferTrendPanelCount = 3 | 4;

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

export function buildCompetitorOfferTrendLayout(
  panelCount: CompetitorOfferTrendPanelCount,
): CompetitorOfferTrendLayout {
  const panelStride = panelCount === 4 ? 88 : 102;
  const plotHeight = panelCount === 4 ? 58 : 70;
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
