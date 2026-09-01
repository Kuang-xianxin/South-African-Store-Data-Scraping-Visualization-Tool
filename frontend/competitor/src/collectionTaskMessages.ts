const LEGACY_STOCK_SUMMARY =
  /^商品与评论快照已保存，但\s*(\d+)\/(\d+)\s*个变体\/卖家报价库存仍未探测；/;

export function formatCollectionTaskMessage(message: string) {
  const legacyStockSummary = message.match(LEGACY_STOCK_SUMMARY);
  if (!legacyStockSummary) return message;
  return message.replace(
    legacyStockSummary[0],
    `商品与评论快照已保存，有${legacyStockSummary[1]}个变体/`
      + `${legacyStockSummary[2]}个卖家报价，`
      + `其中${legacyStockSummary[1]}个报价库存仍未探测；`,
  );
}
