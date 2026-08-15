import type { CompetitorItem, CompetitorOfferItem } from "./types";

export const OWN_OFFER_LATEST_STATUS_OPTIONS = [
  { value: "not_buyable", label: "不可购买（Not Buyable）" },
  { value: "buyable", label: "可购买（Buyable）" },
  { value: "disabled_by_takealot", label: "平台已停用（Disabled by Takealot）" },
  { value: "disabled_by_seller", label: "卖家已停用（Disabled by Seller）" },
] as const;

export type OwnOfferLatestStatus =
  (typeof OWN_OFFER_LATEST_STATUS_OPTIONS)[number]["value"];
export type OwnOfferLatestStatusFilter = "全部" | OwnOfferLatestStatus;
export type OwnOfferStockFilter = "全部" | "有货" | "没货" | "未探测";

type FilterableOwnOffer = Pick<
  CompetitorOfferItem,
  "报价来源" | "最新Offer状态" | "最新Offer库存状态"
>;

const OWN_OFFER_LATEST_STATUS_LABELS = new Map<string, string>(
  OWN_OFFER_LATEST_STATUS_OPTIONS.map((option) => [option.value, option.label]),
);

function ownOfferStockState(
  offer: FilterableOwnOffer,
): Exclude<OwnOfferStockFilter, "全部"> {
  if (
    offer.最新Offer库存状态 === "有货"
    || offer.最新Offer库存状态 === "没货"
  ) {
    return offer.最新Offer库存状态;
  }
  return "未探测";
}

export function matchesOwnOfferLatestFilters(
  item: Pick<CompetitorItem, "对比报价">,
  selectedStatus: OwnOfferLatestStatusFilter,
  selectedStock: OwnOfferStockFilter,
): boolean {
  if (selectedStatus === "全部" && selectedStock === "全部") return true;
  return (item.对比报价 ?? []).some((offer) => (
    offer.报价来源 === "seller_api"
    && (
      selectedStatus === "全部"
      || offer.最新Offer状态 === selectedStatus
    )
    && (
      selectedStock === "全部"
      || ownOfferStockState(offer) === selectedStock
    )
  ));
}

export function ownOfferLatestStatusLabel(status: string): string {
  return OWN_OFFER_LATEST_STATUS_LABELS.get(status) ?? status;
}
