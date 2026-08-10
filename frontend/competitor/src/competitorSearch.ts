import type { CompetitorItem } from "./types";

export function competitorSearchTerm(value: string): string {
  const trimmed = value.trim();
  return (trimmed.match(/PLID(\d+)/i)?.[1] ?? trimmed).toLocaleLowerCase();
}

export function matchesCompetitorSearchValues(
  values: readonly unknown[],
  rawQuery: string,
): boolean {
  const query = competitorSearchTerm(rawQuery);
  if (!query) return true;
  return values.some((value) =>
    String(value ?? "").toLocaleLowerCase().includes(query),
  );
}

export function matchesCompetitorSearch(
  item: CompetitorItem,
  rawQuery: string,
): boolean {
  return matchesCompetitorSearchValues(
    [
      item.商品,
      item.plid,
      item.当前卖家,
      item.库存上限,
      item.趋势判断,
      item.价格信号,
      ...(item.自有报价 ?? []).flatMap((offer) => [
        offer.offer_id,
        offer.店铺,
        offer.SKU,
        offer.状态,
      ]),
      ...(item.跟卖报价 ?? []).flatMap((offer) => [
        offer.offer_id,
        offer.卖家ID,
        offer.卖家,
        offer.SKU,
        offer.变体,
        offer.库存状态,
        offer.价格信号,
        offer.库存信号,
      ]),
    ],
    rawQuery,
  );
}
