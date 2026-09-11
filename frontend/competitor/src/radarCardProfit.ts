import type { OwnStoreProfitabilityItem } from "./types";

export function radarCardPlatformUrl(item: { plid: string; 链接?: string | null }): string | null {
  try {
    const url = new URL(item.链接 ?? "");
    if (url.protocol !== "https:" || !["www.takealot.com", "takealot.com"].includes(url.hostname.toLowerCase())
      || url.username || url.password || (url.port && url.port !== "443")) return null;
    const match = url.pathname.match(/^\/[^/]+\/PLID(\d+)\/?$/i);
    return match?.[1] === item.plid ? url.href : null;
  } catch { return null; }
}

export function radarCardProfitSummary(items: readonly OwnStoreProfitabilityItem[], plid: string) {
  const offers = items.filter((item) => item.plid === plid);
  const available = offers.filter((item) => {
    const result = item.workbook_profit;
    return result?.status === "available" && result.calculation
      && Number.isFinite(result.calculation.profit_zar)
      && Number.isFinite(result.calculation.margin_percentage);
  });
  const range = (values: number[]): [number, number] | null => values.length
    ? [Math.min(...values), Math.max(...values)] : null;
  const reasons = [...new Set(offers.filter((item) => !available.includes(item))
    .map((item) => item.workbook_profit?.message || "当前服务尚未提供运营表利润模型"))];
  return {
    total: offers.length,
    available: available.length,
    profit: range(available.map((item) => item.workbook_profit!.calculation!.profit_zar)),
    margin: range(available.map((item) => item.workbook_profit!.calculation!.margin_percentage)),
    price: range(available.map((item) => item.selling_price_zar).filter((value): value is number => typeof value === "number" && Number.isFinite(value) && value > 0)),
    reasons,
  };
}
