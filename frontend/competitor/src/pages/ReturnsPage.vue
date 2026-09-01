<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import {
  fetchReturns,
  refreshReturnRemovalOrders,
  verifyReturnRemovalOrderOtp,
} from "../api";
import { openOwnStoreDetailTab } from "../moduleNavigation";
import { PRODUCT_IMAGE_SIZE, productThumbnailUrl } from "../productImages";
import {
  companySkuOwnLinks,
  filterReturnsForCompanySku,
  summarizeCompanySkuReturns,
  type CompanySkuOwnLink,
} from "../returnCompanySku";
import { formatChinaDateTime } from "../time";
import type {
  OwnStoreScope,
  ReturnCollectionStoreStatus,
  ReturnRemovalLifecycle,
  ReturnRemovalLifecycleSummary,
  ReturnRemovalOrder,
  ReturnRemovalOrderItem,
  ReturnRemovalW8Lifecycle,
  ReturnsPayload,
  SellerReturnItem,
} from "../types";

defineOptions({ name: "ReturnsPage" });

const props = defineProps<{
  asOf: string;
  rangeStart: string;
  rangeEnd: string;
  storeScope?: OwnStoreScope;
  multiStoreLabel?: string;
  canSyncRemovalOrders?: boolean;
}>();

const payload = ref<ReturnsPayload | null>(null);
const loading = ref(true);
const error = ref("");
const queryDraft = ref("");
const appliedQuery = ref("");
const reason = ref("");
const outcome = ref("");
const page = ref(1);
const pageSize = ref(50);
const companySkuDetailOpen = ref(false);
const companySkuDetailLoading = ref(false);
const companySkuDetailError = ref("");
const companySkuDetailProgress = ref("");
const companySkuDetailActionMessage = ref("");
const companySkuDetailItem = ref<SellerReturnItem | null>(null);
const companySkuDetailItems = ref<SellerReturnItem[]>([]);
const companySkuDetailPayload = ref<ReturnsPayload | null>(null);
const companySkuDetailPage = ref(1);
const companySkuDetailPageSize = 20;
const failedReturnImageUrls = ref<Set<string>>(new Set());
const removalSyncing = ref(false);
const removalSyncMessage = ref("");
const removalSyncError = ref("");
const removalOtpRequired = ref(false);
const removalOtp = ref("");
const removalOrderStage = ref<ReturnRemovalOrder["stage"]>("submitted");
const removalOrderQuery = ref("");
const removalOrderType = ref("");
const removalOrderPage = ref(1);
const removalOrderPageSize = 20;
const expandedRemovalOrderKey = ref<string | null>(null);
let requestRevision = 0;
let activeController: AbortController | null = null;
let companySkuRequestRevision = 0;
let companySkuController: AbortController | null = null;
let companySkuReturnFocusElement: HTMLElement | null = null;

const pageCount = computed(() => Math.max(1, Math.ceil((payload.value?.total ?? 0) / pageSize.value)));
const scopeLabel = computed(() =>
  (props.storeScope ?? "current") === "current"
    ? "当前店铺"
    : props.multiStoreLabel || ((props.storeScope ?? "current") === "operating" ? "我的运营店铺" : "全部店铺"),
);
const companySkuDetailPageCount = computed(() => Math.max(
  1,
  Math.ceil(companySkuDetailItems.value.length / companySkuDetailPageSize),
));
const companySkuDetailVisibleItems = computed(() => {
  const offset = (companySkuDetailPage.value - 1) * companySkuDetailPageSize;
  return companySkuDetailItems.value.slice(offset, offset + companySkuDetailPageSize);
});
const companySkuDetailSummary = computed(() =>
  summarizeCompanySkuReturns(companySkuDetailItems.value),
);
const companySkuDetailOwnLinks = computed(() =>
  companySkuOwnLinks([
    ...(companySkuDetailItem.value ? [companySkuDetailItem.value] : []),
    ...companySkuDetailItems.value,
  ]),
);
const removalSummary = computed<ReturnRemovalLifecycleSummary>(() =>
  payload.value?.summary.removal_lifecycle ?? {
    relevant_count: 0,
    pending_creation_count: 0,
    linked_po_count: 0,
    ready_count: 0,
    collectable_count: 0,
    expired_count: 0,
    expiring_count: 0,
    booked_count: 0,
    fully_collected_count: 0,
    w8_received_count: 0,
    w8_pending_shelf_units: 0,
    w8_shelved_units: 0,
    w8_defective_units: 0,
    unknown_after_pickup_count: 0,
  },
);
const canRunRemovalSync = computed(() =>
  Boolean(props.canSyncRemovalOrders) && (props.storeScope ?? "current") === "current",
);
const portalTrackingStatus = computed(() =>
  payload.value?.removal_order_tracking?.data_status ?? "uncollected",
);
const w8TrackingAvailable = computed(() =>
  payload.value?.removal_order_tracking?.w8.data_status === "synced",
);
const allRemovalOrders = computed(() => payload.value?.removal_orders?.items ?? []);
const removalOrderTypes = computed(() => Array.from(new Set(
  allRemovalOrders.value
    .map((item) => item.order_type)
    .filter((value): value is string => Boolean(value)),
)).sort((left, right) => left.localeCompare(right, "en")));
const filteredRemovalOrders = computed(() => {
  const query = removalOrderQuery.value.trim().toLocaleLowerCase();
  return allRemovalOrders.value.filter((order) => {
    if (order.stage !== removalOrderStage.value) return false;
    if (removalOrderType.value && order.order_type !== removalOrderType.value) return false;
    if (!query) return true;
    const searchable = [
      order.removal_order_id,
      order.reference,
      order.instruction_id,
      order.status,
      order.order_type,
      order.store_name,
      order.warehouse_id,
      order.returns_facility_code,
      ...order.items.flatMap((item) => [
        item.product_title,
        item.sku,
        item.offer_id,
        item.tsin_id,
        ...item.return_reference_numbers,
        ...item.seller_return_ids,
      ]),
    ].filter(Boolean).join(" ").toLocaleLowerCase();
    return searchable.includes(query);
  });
});
const removalOrderPageCount = computed(() => Math.max(
  1,
  Math.ceil(filteredRemovalOrders.value.length / removalOrderPageSize),
));
const visibleRemovalOrders = computed(() => {
  const offset = (removalOrderPage.value - 1) * removalOrderPageSize;
  return filteredRemovalOrders.value.slice(offset, offset + removalOrderPageSize);
});

watch(
  [
    () => props.rangeStart,
    () => props.rangeEnd,
    () => props.storeScope,
    appliedQuery,
    reason,
    outcome,
    page,
    pageSize,
  ],
  loadReturns,
  { immediate: true },
);
watch(companySkuDetailOpen, (open) => {
  document.body.style.overflow = open ? "hidden" : "";
});
watch([removalOrderStage, removalOrderQuery, removalOrderType], () => {
  removalOrderPage.value = 1;
  expandedRemovalOrderKey.value = null;
});
watch(removalOrderPageCount, (count) => {
  if (removalOrderPage.value > count) removalOrderPage.value = count;
});
watch(
  [() => props.rangeStart, () => props.rangeEnd, () => props.storeScope],
  () => {
    if (companySkuDetailOpen.value) closeCompanySkuDetail(false);
    removalOtpRequired.value = false;
    removalOtp.value = "";
    removalSyncMessage.value = "";
    removalSyncError.value = "";
  },
);

onMounted(() => {
  window.addEventListener("keydown", handleWindowKeydown);
});

onBeforeUnmount(() => {
  requestRevision += 1;
  activeController?.abort();
  companySkuRequestRevision += 1;
  companySkuController?.abort();
  window.removeEventListener("keydown", handleWindowKeydown);
  document.body.style.overflow = "";
});

async function loadReturns() {
  const revision = ++requestRevision;
  activeController?.abort();
  const controller = new AbortController();
  activeController = controller;
  loading.value = true;
  error.value = "";
  try {
    const result = await fetchReturns(
      props.rangeStart,
      props.rangeEnd,
      props.storeScope ?? "current",
      {
        query: appliedQuery.value,
        reason: reason.value,
        outcome: outcome.value,
        page: page.value,
        pageSize: pageSize.value,
      },
      controller.signal,
    );
    if (revision !== requestRevision) return;
    payload.value = result;
    if (page.value > Math.max(1, Math.ceil(result.total / pageSize.value))) {
      page.value = Math.max(1, Math.ceil(result.total / pageSize.value));
    }
  } catch (reasonValue) {
    if (controller.signal.aborted || revision !== requestRevision) return;
    error.value = reasonValue instanceof Error ? reasonValue.message : "退货数据读取失败";
  } finally {
    if (revision === requestRevision) loading.value = false;
  }
}

async function runRemovalSync() {
  if (!canRunRemovalSync.value || removalSyncing.value) return;
  removalSyncing.value = true;
  removalSyncMessage.value = "";
  removalSyncError.value = "";
  try {
    const result = await refreshReturnRemovalOrders();
    if (result.state === "otp_required") {
      removalOtpRequired.value = true;
      removalSyncMessage.value = result.portal.otp_destination
        ? `Takealot 已发送验证码至 ${result.portal.otp_destination}`
        : "Takealot 要求输入登录验证码";
      return;
    }
    removalOtpRequired.value = false;
    removalOtp.value = "";
    removalSyncMessage.value = `PO 状态已刷新：${result.order_count ?? 0} 单`;
    await loadReturns();
  } catch (reasonValue) {
    removalSyncError.value = reasonValue instanceof Error
      ? reasonValue.message
      : "PO 状态刷新失败";
  } finally {
    removalSyncing.value = false;
  }
}

async function verifyRemovalOtp() {
  const otp = removalOtp.value.trim();
  if (!otp || removalSyncing.value) return;
  removalSyncing.value = true;
  removalSyncError.value = "";
  try {
    const result = await verifyReturnRemovalOrderOtp(otp);
    removalOtpRequired.value = false;
    removalOtp.value = "";
    removalSyncMessage.value = `验证码通过，PO 状态已刷新：${result.order_count ?? 0} 单`;
    await loadReturns();
  } catch (reasonValue) {
    removalSyncError.value = reasonValue instanceof Error
      ? reasonValue.message
      : "验证码校验或 PO 状态刷新失败";
  } finally {
    removalSyncing.value = false;
  }
}

function applySearch() {
  page.value = 1;
  appliedQuery.value = queryDraft.value.trim();
}

function clearFilters() {
  queryDraft.value = "";
  appliedQuery.value = "";
  reason.value = "";
  outcome.value = "";
  page.value = 1;
}

function resetPage() {
  page.value = 1;
}

function changePage(nextPage: number) {
  page.value = Math.min(pageCount.value, Math.max(1, nextPage));
}

function selectRemovalOrderStage(stage: ReturnRemovalOrder["stage"]): void {
  removalOrderStage.value = stage;
}

function changeRemovalOrderPage(nextPage: number): void {
  removalOrderPage.value = Math.min(
    removalOrderPageCount.value,
    Math.max(1, nextPage),
  );
  expandedRemovalOrderKey.value = null;
}

function removalOrderKey(order: ReturnRemovalOrder): string {
  return `${order.store_code}:${order.stage}:${order.removal_order_id}`;
}

function activateRemovalOrder(order: ReturnRemovalOrder, event: MouseEvent | KeyboardEvent): void {
  if (event instanceof MouseEvent) {
    const target = event.target;
    if (
      target instanceof Element
      && target.closest("a, button, input, select, textarea, [contenteditable='true']")
    ) return;
    if (window.getSelection()?.toString().trim()) return;
  }
  const key = removalOrderKey(order);
  expandedRemovalOrderKey.value = expandedRemovalOrderKey.value === key ? null : key;
}

function removalOrderDate(order: ReturnRemovalOrder): string {
  if (order.stage === "submitted") return order.date_submitted || "提交日期未提供";
  if (order.stage === "pickup_ready") {
    return formatPortalDateTime(order.pickup_date_start) || "提货日期未提供";
  }
  return order.date_closed || "关闭日期未提供";
}

function removalOrderFacility(order: ReturnRemovalOrder): string {
  return order.returns_facility_code
    || order.warehouse_id
    || order.returns_region_id
    || "—";
}

function removalOrderExpiry(order: ReturnRemovalOrder): string {
  if (order.expiry_status === "expired") {
    return `已过期${order.disposal_date ? ` · ${order.disposal_date}` : ""}`;
  }
  if (order.expiry_status === "expiring") {
    return `临期${order.days_until_expiry !== null ? ` · 剩 ${order.days_until_expiry} 天` : ""}`;
  }
  if (order.expiry_status === "active") {
    return `未过期${order.disposal_date ? ` · ${order.disposal_date} 处置` : ""}`;
  }
  return "过期时间未提供";
}

function removalOrderCollectability(order: ReturnRemovalOrder): string {
  if (order.can_collect === true) return "当前可提货";
  if (order.can_collect === false) {
    if (order.stage === "submitted") return "尚未备好，不可提";
    if (order.expiry_status === "expired") return "已过期，不可提";
    return "当前不可提货";
  }
  return "能否提货未知";
}

function removalOrderBooking(order: ReturnRemovalOrder): string {
  if (order.has_booking === true) {
    const window = [order.pickup_date_start, order.pickup_date_end]
      .map(formatPortalDateTime)
      .filter(Boolean)
      .join(" → ");
    return window ? `已预约 · ${window}` : "已预约 · 窗口未提供";
  }
  if (order.has_booking === false) return "尚未预约";
  return "预约状态未提供";
}

function removalOrderCollectionStatus(
  status: ReturnRemovalOrder["collection_status"] | ReturnRemovalOrderItem["collection_status"],
): string {
  return {
    fully_collected: "已全部取到",
    partly_collected: "只取到部分",
    not_collected: "尚未取到",
    unknown: "是否取到未知",
  }[status];
}

function removalOrderW8Label(w8: ReturnRemovalW8Lifecycle): string {
  if (w8.match_status !== "linked") return w8.message || "长睿未精确关联";
  return {
    awaiting_receipt: "仅有预报，尚无实际到仓证据",
    pending_shelf: `已到长睿 · 待上架 ${w8.pending_shelf_quantity}`,
    shelved: `长睿仓已上架 ${w8.shelved_quantity}`,
    defective: `已报损 ${w8.defective_quantity}`,
    mixed: `长睿仓已上架 ${w8.shelved_quantity} · 报损 ${w8.defective_quantity}`,
    received_unresolved: "已到长睿 · 后续处置未展开",
    unknown: "长睿处置未知",
  }[w8.disposition];
}

function removalOrderRelations(item: ReturnRemovalOrderItem): string {
  const rrns = item.return_reference_numbers.length
    ? `RRN ${item.return_reference_numbers.join("、")}`
    : "RRN —";
  const sellerReturns = item.seller_return_ids.length
    ? `Seller Return ${item.seller_return_ids.join("、")}`
    : "Seller Return —";
  return `${rrns} · ${sellerReturns}`;
}

function formatRemovalWeight(value: number | null): string {
  if (value === null) return "—";
  return `${value.toLocaleString("en-ZA")} g · ${(value / 1000).toFixed(3)} kg`;
}

function formatRemovalFee(cents: number | null): string {
  return cents === null ? "—" : formatZar(cents / 100);
}

function formatPortalDateTime(value: string | null): string {
  const text = String(value ?? "").trim();
  if (!text) return "";
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text;
  const parsed = new Date(text);
  if (Number.isNaN(parsed.getTime())) return text;
  return new Intl.DateTimeFormat("en-ZA", {
    timeZone: "Africa/Johannesburg",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}

function removalOrderItemImage(item: ReturnRemovalOrderItem, order: ReturnRemovalOrder): string {
  const source = String(item.image_url ?? "").trim();
  return source && !failedReturnImageUrls.value.has(source)
    ? productThumbnailUrl(source, PRODUCT_IMAGE_SIZE.list, order.store_code)
    : "";
}

function hasCompanySku(item: SellerReturnItem): boolean {
  return Boolean(String(item.company_sku ?? "").trim());
}

function activateReturnRow(item: SellerReturnItem, event: MouseEvent | KeyboardEvent): void {
  if (!hasCompanySku(item)) return;
  if (event instanceof MouseEvent) {
    const target = event.target;
    if (
      target instanceof Element
      && target.closest("a, button, input, select, textarea, [contenteditable='true']")
    ) return;
    if (window.getSelection()?.toString().trim()) return;
  }
  void openCompanySkuDetail(item, event);
}

async function openCompanySkuDetail(item: SellerReturnItem, event?: Event) {
  const companySku = String(item.company_sku ?? "").trim();
  if (!companySku) return;
  companySkuReturnFocusElement =
    event?.currentTarget instanceof HTMLElement ? event.currentTarget : null;
  companySkuDetailItem.value = item;
  companySkuDetailItems.value = [];
  companySkuDetailPayload.value = null;
  companySkuDetailPage.value = 1;
  companySkuDetailError.value = "";
  companySkuDetailProgress.value = "";
  companySkuDetailActionMessage.value = "";
  companySkuDetailOpen.value = true;

  const revision = ++companySkuRequestRevision;
  companySkuController?.abort();
  const controller = new AbortController();
  companySkuController = controller;
  companySkuDetailLoading.value = true;
  try {
    const candidates: SellerReturnItem[] = [];
    const requestPageSize = 100;
    let requestPage = 1;
    let expectedTotal = 0;
    do {
      const result = await fetchReturns(
        props.rangeStart,
        props.rangeEnd,
        props.storeScope ?? "current",
        {
          query: companySku,
          page: requestPage,
          pageSize: requestPageSize,
        },
        controller.signal,
      );
      if (controller.signal.aborted || revision !== companySkuRequestRevision) return;
      if (requestPage === 1) companySkuDetailPayload.value = result;
      expectedTotal = result.total;
      candidates.push(...result.items);
      companySkuDetailProgress.value = expectedTotal
        ? `正在读取候选明细 ${Math.min(candidates.length, expectedTotal)} / ${expectedTotal}`
        : "当前查询没有候选明细";
      if (!result.items.length || candidates.length >= expectedTotal) break;
      requestPage += 1;
    } while (true);
    companySkuDetailItems.value = filterReturnsForCompanySku(candidates, companySku);
  } catch (reasonValue) {
    if (controller.signal.aborted || revision !== companySkuRequestRevision) return;
    companySkuDetailError.value = reasonValue instanceof Error
      ? reasonValue.message
      : "公司 SKU 退货明细读取失败";
  } finally {
    if (revision === companySkuRequestRevision) {
      companySkuDetailLoading.value = false;
      companySkuDetailProgress.value = "";
    }
  }
}

function closeCompanySkuDetail(restoreFocus = true) {
  companySkuRequestRevision += 1;
  companySkuController?.abort();
  companySkuController = null;
  companySkuDetailLoading.value = false;
  companySkuDetailOpen.value = false;
  const target = companySkuReturnFocusElement;
  companySkuReturnFocusElement = null;
  if (restoreFocus && target) void nextTick(() => target.focus());
}

function changeCompanySkuDetailPage(nextPage: number) {
  companySkuDetailPage.value = Math.min(
    companySkuDetailPageCount.value,
    Math.max(1, nextPage),
  );
}

function openCompanySkuOwnLink(link: CompanySkuOwnLink) {
  companySkuDetailActionMessage.value = "";
  const scope = props.storeScope ?? "current";
  const opened = openOwnStoreDetailTab({
    plid: link.plid,
    scope,
    ...(scope === "current" && link.storeCode ? { storeCode: link.storeCode } : {}),
    startDate: props.rangeStart,
    endDate: props.rangeEnd,
  });
  if (!opened) {
    companySkuDetailActionMessage.value = "浏览器拦截了新标签页，请允许此 ERP 打开新标签页后重试。";
  }
}

function handleWindowKeydown(event: KeyboardEvent) {
  if (event.key === "Escape" && companySkuDetailOpen.value) closeCompanySkuDetail();
}

function statusLabel(status: ReturnsPayload["data_status"] | ReturnCollectionStoreStatus["data_status"]): string {
  return {
    collected: "明细已采集",
    partial: "部分店铺异常",
    stale: "沿用最近成功明细",
    failed: "明细采集失败",
    uncollected: "明细尚未采集",
    unavailable: "明细暂不可读",
  }[status];
}

function statusMessage(status: ReturnsPayload["data_status"]): string {
  if (status === "collected") return "已完整覆盖当前日期区间。";
  if (status === "partial") return "已有明细可查看；未覆盖部分不按 0 计算。";
  if (status === "failed") return "退货明细采集失败。";
  if (status === "unavailable") return "本地退货明细暂不可读。";
  return "尚未采集退货明细。";
}

function outcomeText(item: SellerReturnItem): string {
  return item.outcome_labels.length ? item.outcome_labels.join("、") : "待平台处理 / 未展开";
}

function transactionText(item: SellerReturnItem): string {
  const values = item.transactions.map((transaction) => {
    const kind = String(transaction.transaction_type ?? "交易");
    const amount = Number(transaction.amount_incl_vat ?? transaction.amount);
    return Number.isFinite(amount) ? `${kind} ${formatZar(amount)}` : kind;
  });
  return values.length ? values.join("；") : "无展开交易";
}

function removalStageLabel(lifecycle?: ReturnRemovalLifecycle): string {
  if (!lifecycle) return "PO 状态未读取";
  return {
    not_applicable: "不涉及移除 PO",
    pending_creation: "待 Takealot 创建 PO",
    unlinked: "已进入移除单 · PO 未关联",
    submitted: "移除单已提交",
    pickup_ready: "已备好，可安排提货",
    closed: "移除单已关闭",
  }[lifecycle.stage];
}

function removalExpiryLabel(lifecycle?: ReturnRemovalLifecycle): string {
  if (!lifecycle || lifecycle.expiry_status === "unknown") return "过期日未提供";
  if (lifecycle.expiry_status === "expired") {
    return `已过期${lifecycle.disposal_date ? ` · ${lifecycle.disposal_date}` : ""}`;
  }
  if (lifecycle.expiry_status === "expiring") {
    return `临期${lifecycle.days_until_expiry !== null ? ` · 剩 ${lifecycle.days_until_expiry} 天` : ""}`;
  }
  return `未过期${lifecycle.disposal_date ? ` · ${lifecycle.disposal_date} 处置` : ""}`;
}

function removalCollectLabel(lifecycle?: ReturnRemovalLifecycle): string {
  if (!lifecycle) return "能否提货未知";
  if (lifecycle.can_collect === true) return "当前仍可提货";
  if (lifecycle.can_collect === false) {
    return lifecycle.stage === "submitted" ? "尚未备好，不可提" : "当前不可提货";
  }
  return "能否提货未知";
}

function removalBookingLabel(lifecycle?: ReturnRemovalLifecycle): string {
  if (!lifecycle) return "预约未知";
  if (lifecycle.has_booking === true) {
    const window = [lifecycle.pickup_date_start, lifecycle.pickup_date_end]
      .filter(Boolean)
      .join(" → ");
    return window ? `已预约 · ${window}` : "已预约 · 窗口未提供";
  }
  if (lifecycle.has_booking === false) return "尚未预约";
  return "预约状态未知";
}

function removalCollectionLabel(lifecycle?: ReturnRemovalLifecycle): string {
  if (!lifecycle) return "提货数量未知";
  const hasItemQuantities = [
    lifecycle.quantity_requested,
    lifecycle.quantity_prepared,
    lifecycle.quantity_collected,
  ].some((value) => value !== null);
  const hasOrderQuantities = [
    lifecycle.order_quantity_requested,
    lifecycle.order_quantity_prepared,
    lifecycle.order_quantity_collected,
  ].some((value) => value !== null);
  if (!hasItemQuantities && !hasOrderQuantities) return "提货数量未知";
  const quantities = hasItemQuantities
    ? [lifecycle.quantity_requested, lifecycle.quantity_prepared, lifecycle.quantity_collected]
    : [
      lifecycle.order_quantity_requested,
      lifecycle.order_quantity_prepared,
      lifecycle.order_quantity_collected,
    ];
  const status = hasItemQuantities
    ? lifecycle.collection_status
    : lifecycle.order_collection_status;
  const label = {
    fully_collected: "已全部取到",
    partly_collected: "只取到部分",
    not_collected: "尚无已取数量",
    unknown: "是否取到未知",
  }[status];
  return `${label} · ${hasItemQuantities ? "该商品" : "PO合计"} 要求/备好/已取 ${quantities.map((value) => value ?? "—").join(" / ")}`;
}

function removalW8Label(lifecycle?: ReturnRemovalLifecycle): string {
  const w8 = lifecycle?.w8;
  if (!w8 || w8.match_status !== "linked") return w8?.message || "长睿未关联";
  const label = {
    awaiting_receipt: "长睿仅有预报，尚无实际到仓证据",
    pending_shelf: `长睿已到仓 · 待上架 ${w8.pending_shelf_quantity}`,
    shelved: `长睿已上架 ${w8.shelved_quantity}（仓库库存）`,
    defective: `长睿已报损 ${w8.defective_quantity}`,
    mixed: `长睿已上架 ${w8.shelved_quantity} · 报损 ${w8.defective_quantity}`,
    received_unresolved: "长睿已到仓 · 后续处置未展开",
    unknown: "长睿处置未知",
  }[w8.disposition];
  return `${label}${w8.order_no ? ` · ${w8.order_no}` : ""}`;
}

function portalMetric(value: number): string | number {
  if (portalTrackingStatus.value === "uncollected") return "—";
  return portalTrackingStatus.value === "partial" ? `${value}+` : value;
}

function portalMetricPair(first: number, second: number): string {
  if (portalTrackingStatus.value === "uncollected") return "— / —";
  const suffix = portalTrackingStatus.value === "partial" ? "+" : "";
  return `${first}${suffix} / ${second}${suffix}`;
}

function w8Metric(value: number): string | number {
  return w8TrackingAvailable.value ? value : "—";
}

function w8MetricPair(first: number, second: number): string {
  return w8TrackingAvailable.value ? `${first} / ${second}` : "— / —";
}

function formatZar(value: number | null): string {
  if (value === null) return "—";
  return new Intl.NumberFormat("en-ZA", {
    style: "currency",
    currency: "ZAR",
    maximumFractionDigits: 2,
  }).format(value);
}

function itemImage(item: SellerReturnItem): string {
  const source = String(item.image_url ?? "").trim();
  return source && !failedReturnImageUrls.value.has(source)
    ? productThumbnailUrl(source, PRODUCT_IMAGE_SIZE.list, item.store_code)
    : "";
}

function companySkuOwnLinkImage(link: CompanySkuOwnLink): string {
  const source = String(link.imageUrl ?? "").trim();
  return source && !failedReturnImageUrls.value.has(source)
    ? productThumbnailUrl(source, PRODUCT_IMAGE_SIZE.list, link.storeCode)
    : "";
}

function markReturnImageUnavailable(source: string | null | undefined): void {
  const normalized = String(source ?? "").trim();
  if (!normalized) return;
  failedReturnImageUrls.value = new Set([...failedReturnImageUrls.value, normalized]);
}
</script>

<template>
  <section class="returns-page">
    <header class="returns-hero">
      <div>
        <p class="section-kicker">SELLER RETURNS</p>
        <h1>退货管理</h1>
        <p>{{ scopeLabel }} · {{ props.rangeStart }} 至 {{ props.rangeEnd }}</p>
      </div>
      <div class="returns-removal-sync">
        <button
          v-if="props.canSyncRemovalOrders"
          type="button"
          class="primary-button"
          :disabled="!canRunRemovalSync || removalSyncing"
          @click="runRemovalSync"
        >
          {{ removalSyncing ? "正在刷新 PO 状态…" : "刷新 PO 状态" }}
        </button>
        <small v-if="props.canSyncRemovalOrders && !canRunRemovalSync">
          请切到单个店铺后刷新
        </small>
        <small v-else-if="props.canSyncRemovalOrders">
          普通页面读取本地快照；刷新只读 Seller Portal，不创建或变更 PO
        </small>
      </div>
    </header>

    <p v-if="error" class="error-banner">{{ error }}</p>
    <form
      v-if="removalOtpRequired"
      class="returns-removal-otp"
      @submit.prevent="verifyRemovalOtp"
    >
      <div>
        <strong>Takealot 登录需要验证码</strong>
        <span>{{ removalSyncMessage }}</span>
      </div>
      <input
        v-model="removalOtp"
        type="text"
        inputmode="numeric"
        autocomplete="one-time-code"
        maxlength="12"
        aria-label="Takealot Seller Portal 验证码"
        placeholder="输入验证码"
      />
      <button class="primary-button" type="submit" :disabled="removalSyncing || !removalOtp.trim()">
        验证并继续刷新
      </button>
    </form>
    <p v-if="removalSyncError" class="error-banner">{{ removalSyncError }}</p>
    <p v-else-if="removalSyncMessage && !removalOtpRequired" class="returns-sync-message">
      {{ removalSyncMessage }}
    </p>
    <div v-if="loading && !payload" class="returns-loading">正在读取退货明细……</div>

    <template v-if="payload">
      <section class="returns-status-banner" :class="`status-${payload.data_status}`">
        <div>
          <strong>{{ statusLabel(payload.data_status) }}</strong>
          <span>{{ statusMessage(payload.data_status) }}</span>
        </div>
      </section>

      <section class="removal-orders-module" aria-labelledby="removal-orders-title">
        <header class="removal-orders-header">
          <div>
            <p class="section-kicker">TAKEALOT MANAGE REMOVAL ORDERS</p>
            <h2 id="removal-orders-title">Manage Removal Orders · PO 全部信息</h2>
            <p>
              PO 是本模块的主记录；普通 Removal Order、Takealot Removal Order、Returns Removal Order
              都会保留，不要求先关联到退货明细。
            </p>
          </div>
          <span :class="`status-${payload.removal_orders?.data_status ?? 'uncollected'}`">
            {{ payload.removal_orders?.data_status === "synced" ? "本地快照完整" : payload.removal_orders?.data_status === "partial" ? "部分店铺已有快照" : "尚无 PO 快照" }}
          </span>
        </header>

        <nav class="removal-order-tabs" aria-label="PO 状态">
          <button
            type="button"
            :class="{ active: removalOrderStage === 'submitted' }"
            @click="selectRemovalOrderStage('submitted')"
          >
            Submitted ({{ payload.removal_orders?.counts.submitted ?? 0 }})
          </button>
          <button
            type="button"
            :class="{ active: removalOrderStage === 'pickup_ready' }"
            @click="selectRemovalOrderStage('pickup_ready')"
          >
            Ready For Pickup ({{ payload.removal_orders?.counts.pickup_ready ?? 0 }})
          </button>
          <button
            type="button"
            :class="{ active: removalOrderStage === 'closed' }"
            @click="selectRemovalOrderStage('closed')"
          >
            Closed ({{ payload.removal_orders?.counts.closed ?? 0 }})
          </button>
        </nav>

        <div class="removal-order-filters">
          <label>
            <span>搜索 PO / ID / SKU / RRN / 商品</span>
            <input
              v-model="removalOrderQuery"
              type="search"
              placeholder="例如 RO-…、Removal Order ID、平台 SKU 或 RRN"
            />
          </label>
          <label>
            <span>Order Type</span>
            <select v-model="removalOrderType">
              <option value="">全部类型</option>
              <option v-for="value in removalOrderTypes" :key="value" :value="value">
                {{ value }}
              </option>
            </select>
          </label>
        </div>

        <p class="removal-orders-source">{{ payload.removal_orders?.source_notice }}</p>
        <p
          v-for="warning in payload.removal_orders?.warnings ?? []"
          :key="warning"
          class="removal-orders-warning"
        >
          {{ warning }}
        </p>

        <div v-if="visibleRemovalOrders.length" class="removal-orders-table-wrap">
          <table class="removal-orders-table">
            <thead>
              <tr>
                <th>{{ removalOrderStage === "submitted" ? "Date Submitted" : removalOrderStage === "pickup_ready" ? "Pickup Date" : "Closed Date" }}</th>
                <th>Status</th>
                <th>ID / Order Name (PO)</th>
                <th>Order Type</th>
                <th>店铺 / DC</th>
                <th>Total Weight / Boxes</th>
                <th>Qty Requested / Prepared / Collected</th>
                <th>Fees (Incl VAT)</th>
                <th>预约 / 到期 / 能否提货</th>
              </tr>
            </thead>
            <tbody>
              <template v-for="order in visibleRemovalOrders" :key="removalOrderKey(order)">
                <tr
                  class="removal-order-row"
                  tabindex="0"
                  role="button"
                  :aria-expanded="expandedRemovalOrderKey === removalOrderKey(order)"
                  @click="activateRemovalOrder(order, $event)"
                  @keydown.enter.self.prevent="activateRemovalOrder(order, $event)"
                  @keydown.space.self.prevent="activateRemovalOrder(order, $event)"
                >
                  <td>
                    <strong>{{ removalOrderDate(order) }}</strong>
                    <small v-if="order.ship_by_date">Ship by {{ order.ship_by_date }}</small>
                  </td>
                  <td>
                    <strong>{{ order.status || "平台未返回" }}</strong>
                    <small>Status ID {{ order.status_id ?? "—" }}</small>
                    <small v-if="order.failure_reason" class="removal-order-failure">{{ order.failure_reason }}</small>
                  </td>
                  <td>
                    <strong>{{ order.removal_order_id }}</strong>
                    <span>{{ order.reference || "Order Name 未提供" }}</span>
                    <small>Instruction {{ order.instruction_id || "—" }}</small>
                  </td>
                  <td>
                    <strong>{{ order.order_type || "平台未返回" }}</strong>
                    <small>Type ID {{ order.order_type_id ?? "—" }}</small>
                  </td>
                  <td>
                    <strong>{{ order.store_name }}</strong>
                    <span>{{ removalOrderFacility(order) }}</span>
                  </td>
                  <td>
                    <strong>{{ formatRemovalWeight(order.total_weight_grams) }}</strong>
                    <span>{{ order.number_of_boxes ?? "—" }} 箱</span>
                  </td>
                  <td>
                    <strong>{{ order.quantity_requested ?? "—" }} / {{ order.quantity_prepared ?? "—" }} / {{ order.quantity_collected ?? "—" }}</strong>
                    <span>{{ removalOrderCollectionStatus(order.collection_status) }}</span>
                  </td>
                  <td>
                    <strong>{{ formatRemovalFee(order.total_handling_fee_cents) }}</strong>
                    <small>{{ order.total_offers ?? order.items.length }} 个 SKU</small>
                  </td>
                  <td>
                    <strong>{{ removalOrderCollectability(order) }}</strong>
                    <span>{{ removalOrderBooking(order) }}</span>
                    <small>{{ removalOrderExpiry(order) }}</small>
                  </td>
                </tr>
                <tr
                  v-if="expandedRemovalOrderKey === removalOrderKey(order)"
                  class="removal-order-detail-row"
                >
                  <td colspan="9">
                    <section class="removal-order-detail">
                      <header>
                        <div>
                          <strong>PO {{ order.reference || order.removal_order_id }}</strong>
                          <span>{{ order.stage_label }} · {{ order.status || "状态未提供" }}</span>
                        </div>
                        <span>快照 {{ order.synced_at ? formatChinaDateTime(order.synced_at) : "时间未提供" }}</span>
                      </header>
                      <dl>
                        <div><dt>Removal reason</dt><dd>{{ order.removal_reason || "平台未返回" }}</dd></div>
                        <div><dt>DC / Region / Facility</dt><dd>{{ order.warehouse_id || "—" }} / {{ order.returns_region_id || "—" }} / {{ order.returns_facility_code || "—" }}</dd></div>
                        <div><dt>提交 / 关闭</dt><dd>{{ order.date_submitted || "—" }} / {{ order.date_closed || "—" }}</dd></div>
                        <div><dt>预约窗口</dt><dd>{{ removalOrderBooking(order) }}</dd></div>
                        <div><dt>过期处置日</dt><dd>{{ order.disposal_date || "—" }} · {{ removalOrderExpiry(order) }}</dd></div>
                        <div><dt>Returns lead time</dt><dd>{{ order.returns_leadtime_days ?? "—" }} 天</dd></div>
                        <div><dt>数量状态</dt><dd>要求 / 备好 / 已取 {{ order.quantity_requested ?? "—" }} / {{ order.quantity_prepared ?? "—" }} / {{ order.quantity_collected ?? "—" }}</dd></div>
                        <div><dt>长睿精确关联</dt><dd>{{ order.w8_summary.matched_item_count }}/{{ order.w8_summary.item_count }} 个商品；到仓 {{ order.w8_summary.received_item_count }}，待上架 {{ order.w8_summary.pending_shelf_units }}，长睿仓已上架 {{ order.w8_summary.shelved_units }}，报损 {{ order.w8_summary.defective_units }}</dd></div>
                      </dl>
                      <p class="removal-order-w8-note">
                        “长睿仓已上架”只表示进入长睿仓库存，不代表已经寄回 Takealot 或在 Takealot 重新上架；没有精确证据时保持未知。
                      </p>
                      <div v-if="order.items.length" class="removal-order-items-wrap">
                        <table class="removal-order-items-table">
                          <thead>
                            <tr>
                              <th>Product Title</th>
                              <th>SKU / Offer / TSIN</th>
                              <th>RRN / Seller Return</th>
                              <th>Qty Requested</th>
                              <th>Qty Prepared</th>
                              <th>Qty Collected</th>
                              <th>Fee Incl VAT</th>
                              <th>长睿到仓与处置</th>
                            </tr>
                          </thead>
                          <tbody>
                            <tr v-for="item in order.items" :key="item.removal_order_item_id || `${item.sku}:${item.offer_id}`">
                              <td>
                                <div class="removal-order-product">
                                  <span class="removal-order-product-image">
                                    <img
                                      v-if="removalOrderItemImage(item, order)"
                                      :src="removalOrderItemImage(item, order)"
                                      :alt="item.product_title || item.sku || 'PO 商品'"
                                      width="96"
                                      height="96"
                                      loading="lazy"
                                      @error="markReturnImageUnavailable(item.image_url)"
                                    />
                                    <small v-else>暂无图片</small>
                                  </span>
                                  <span>
                                    <strong>{{ item.product_title || "商品名称未提供" }}</strong>
                                    <small v-if="item.has_item_mismatch" class="removal-order-failure">Item mismatch</small>
                                  </span>
                                </div>
                              </td>
                              <td>
                                <strong>{{ item.sku || "—" }}</strong>
                                <small>Offer {{ item.offer_id || "—" }} · TSIN {{ item.tsin_id || "—" }}</small>
                                <small>{{ item.offer_status || "Offer 状态未提供" }}</small>
                              </td>
                              <td>
                                <strong>{{ removalOrderRelations(item) }}</strong>
                                <small v-for="info in item.return_informations" :key="`${info.id}:${info.rrn}:${info.seller_return_id}`">
                                  {{ info.created_at ? formatPortalDateTime(info.created_at) : "关联时间未提供" }}{{ info.has_item_mismatch ? " · mismatch" : "" }}
                                </small>
                              </td>
                              <td>{{ item.quantity_requested ?? "—" }}</td>
                              <td>{{ item.quantity_prepared ?? "—" }}</td>
                              <td>
                                <strong>{{ item.quantity_collected ?? "—" }}</strong>
                                <small>{{ removalOrderCollectionStatus(item.collection_status) }}</small>
                              </td>
                              <td>{{ formatRemovalFee(item.handling_fee_cents) }}</td>
                              <td>
                                <strong>{{ removalOrderW8Label(item.w8) }}</strong>
                                <small>{{ item.w8.match_status === "linked" ? `长睿单 ${item.w8.order_no || "—"} · 实到 ${item.w8.inbound_quantity ?? "—"}` : "未把未知数量补成 0" }}</small>
                              </td>
                            </tr>
                          </tbody>
                        </table>
                      </div>
                      <p v-else class="returns-empty">该 PO 快照没有返回商品明细。</p>
                    </section>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>
        <div v-else class="returns-empty">
          <strong>{{ payload.removal_orders?.data_status === "uncollected" ? "尚未刷新 PO 快照" : `当前 ${removalOrderStage === "submitted" ? "Submitted" : removalOrderStage === "pickup_ready" ? "Ready For Pickup" : "Closed"} 没有匹配记录` }}</strong>
          <span>未采集或未返回都不会按 0 单解释。</span>
        </div>
        <footer v-if="filteredRemovalOrders.length" class="removal-order-pagination">
          <span>共 {{ filteredRemovalOrders.length }} 单 · 第 {{ removalOrderPage }} / {{ removalOrderPageCount }} 页</span>
          <div>
            <button type="button" class="quiet-button" :disabled="removalOrderPage <= 1" @click="changeRemovalOrderPage(removalOrderPage - 1)">上一页</button>
            <button type="button" class="quiet-button" :disabled="removalOrderPage >= removalOrderPageCount" @click="changeRemovalOrderPage(removalOrderPage + 1)">下一页</button>
          </div>
        </footer>
      </section>

      <section class="removal-lifecycle-board" aria-labelledby="removal-lifecycle-title">
        <header>
          <div>
            <p class="section-kicker">RETURN & W8 LINKAGE</p>
            <h2 id="removal-lifecycle-title">退货与长睿关联概览</h2>
          </div>
          <span>
            Seller Portal {{ payload.removal_order_tracking?.data_status === "synced" ? "快照已同步" : "快照未完整" }}
            · 长睿 {{ payload.removal_order_tracking?.w8.data_status === "synced" ? "退货明细已同步" : "退货明细待同步" }}
            <template v-if="portalTrackingStatus === 'partial'"> · “+”表示已同步店铺的下限</template>
          </span>
        </header>
        <div class="removal-lifecycle-grid">
          <article>
            <span>待创建 PO</span>
            <strong>{{ removalSummary.pending_creation_count }}</strong>
            <small>Takealot outcomes 明确为待创建</small>
          </article>
          <article>
            <span>已关联 PO</span>
            <strong>{{ portalMetric(removalSummary.linked_po_count) }}</strong>
            <small>{{ removalSummary.relevant_count }} 条移除相关退货</small>
          </article>
          <article>
            <span>现在可提</span>
            <strong>{{ portalMetric(removalSummary.collectable_count) }}</strong>
            <small>已备好且未过期</small>
          </article>
          <article class="is-warning">
            <span>临期 / 已过期</span>
            <strong>{{ portalMetricPair(removalSummary.expiring_count, removalSummary.expired_count) }}</strong>
            <small>临期阈值为剩余 3 天</small>
          </article>
          <article>
            <span>已预约 / 已全部提走</span>
            <strong>{{ portalMetricPair(removalSummary.booked_count, removalSummary.fully_collected_count) }}</strong>
            <small>以 Portal 数量字段为准</small>
          </article>
          <article>
            <span>长睿实际到仓</span>
            <strong>{{ w8Metric(removalSummary.w8_received_count) }}</strong>
            <small>不把预报数量当到仓</small>
          </article>
          <article>
            <span>长睿待上架 / 已上架</span>
            <strong>{{ w8MetricPair(removalSummary.w8_pending_shelf_units, removalSummary.w8_shelved_units) }}</strong>
            <small>上架指长睿仓库库存</small>
          </article>
          <article :class="{ 'is-danger': removalSummary.w8_defective_units > 0 }">
            <span>长睿报损</span>
            <strong>{{ w8Metric(removalSummary.w8_defective_units) }}</strong>
            <small>已提走但去向未知 {{ portalMetric(removalSummary.unknown_after_pickup_count) }} 条</small>
          </article>
        </div>
        <div class="removal-source-statuses">
          <span
            v-for="status in payload.removal_order_tracking?.store_statuses ?? []"
            :key="status.store_code"
          >
            <strong>{{ status.store_name }}</strong>
            {{ status.data_status === "synced" ? `${status.order_count} 单` : "尚无 PO 快照" }}
            <small v-if="status.synced_at">{{ formatChinaDateTime(status.synced_at) }}</small>
          </span>
          <span>
            <strong>长睿</strong>
            {{ payload.removal_order_tracking?.w8.message || "尚无退货快照" }}
            <small v-if="payload.removal_order_tracking?.w8.synced_at">
              {{ formatChinaDateTime(payload.removal_order_tracking.w8.synced_at) }}
            </small>
          </span>
        </div>
      </section>

      <div class="returns-summary-grid">
        <article>
          <span>Offers 滚动30天退货件数</span>
          <strong>{{ payload.offer_returned_30_days.units ?? "—" }}</strong>
          <small>
            {{ payload.offer_returned_30_days.covered_offer_count }}/{{ payload.offer_returned_30_days.offer_count }} 个 Offer 有该字段
          </small>
        </article>
        <article>
          <span>筛选区间退货件数</span>
          <strong>{{ payload.summary.return_units }}</strong>
          <small>{{ payload.summary.return_count }} 条详细退货记录</small>
        </article>
        <article>
          <span>涉及商品</span>
          <strong>{{ payload.summary.affected_product_count }}</strong>
          <small>按店铺 + Offer/SKU 去重</small>
        </article>
        <article>
          <span>质量 / 错发相关件数</span>
          <strong>{{ payload.summary.quality_related_units }}</strong>
          <small>缺陷、损坏或与下单不符</small>
        </article>
        <article>
          <span>处理结果</span>
          <strong>{{ payload.summary.sellable_stock_units }} / {{ payload.summary.removal_order_units }}</strong>
          <small>转可售 / 移除单相关</small>
        </article>
        <article>
          <span>展开交易金额（含 VAT）</span>
          <strong>{{ formatZar(payload.summary.transaction_total_incl_vat) }}</strong>
          <small>完全沿用 API 正负号，不等同最终损失</small>
        </article>
      </div>

      <section class="returns-store-statuses" aria-label="各店铺退货采集状态">
        <article
          v-for="status in payload.store_statuses"
          :key="status.store_code"
          :class="`status-${status.data_status}`"
        >
          <div>
            <strong>{{ status.store_name }}</strong>
            <span>{{ statusLabel(status.data_status) }}</span>
          </div>
          <small v-if="status.last_success_at">
            最近成功 {{ formatChinaDateTime(status.last_success_at) }} · {{ status.record_count ?? 0 }} 条
          </small>
          <small v-else>尚无成功明细批次</small>
          <small v-if="status.requested_from && status.requested_through">
            成功批次范围 {{ status.requested_from }} 至 {{ status.requested_through }}
          </small>
          <small v-if="status.latest_error" class="returns-store-error">{{ status.latest_error }}</small>
        </article>
      </section>

      <form class="returns-filters" @submit.prevent="applySearch">
        <label class="returns-search-field">
          <span>商品 / 单号 / 备注</span>
          <input
            v-model="queryDraft"
            type="search"
            placeholder="商品名称支持模糊搜索，也可输入公司SKU、平台SKU、RRN、订单号"
          />
        </label>
        <label>
          <span>退货原因</span>
          <select v-model="reason" @change="resetPage">
            <option value="">全部原因</option>
            <option v-for="item in payload.filters.reasons" :key="item.value" :value="item.value">
              {{ item.label }}（{{ item.count }}）
            </option>
          </select>
        </label>
        <label>
          <span>处理结果</span>
          <select v-model="outcome" @change="resetPage">
            <option value="">全部结果</option>
            <option v-for="item in payload.filters.outcomes" :key="item.value" :value="item.value">
              {{ item.label }}（{{ item.count }}）
            </option>
          </select>
        </label>
        <div class="returns-filter-actions">
          <button class="primary-button" type="submit">查询</button>
          <button class="quiet-button" type="button" @click="clearFilters">清空</button>
        </div>
      </form>

      <div v-if="loading" class="returns-inline-loading">正在更新筛选结果……</div>
      <div v-else-if="payload.items.length" class="returns-table-wrap">
        <table class="returns-table">
          <thead>
            <tr>
              <th>退货日期 / 编号</th>
              <th>商品</th>
              <th>店铺 / 数量</th>
              <th>退货原因</th>
              <th>处理结果</th>
              <th>移除 PO 生命周期</th>
              <th>客户备注</th>
              <th>交易展开</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in payload.items"
              :key="item.store_scope_key"
              class="returns-record-row"
              :class="{ 'is-clickable': hasCompanySku(item) }"
              :tabindex="hasCompanySku(item) ? 0 : undefined"
              :role="hasCompanySku(item) ? 'button' : undefined"
              :aria-label="hasCompanySku(item)
                ? `查看公司 SKU ${item.company_sku} 的全部退货情况`
                : undefined"
              @click="activateReturnRow(item, $event)"
              @keydown.enter.self.prevent="activateReturnRow(item, $event)"
              @keydown.space.self.prevent="activateReturnRow(item, $event)"
            >
              <td>
                <strong>{{ item.return_date || "—" }}</strong>
                <small>RRN {{ item.return_reference_number || "—" }}</small>
                <small>Seller Return {{ item.seller_return_id }}</small>
                <small>订单 {{ item.order_id || "—" }}</small>
              </td>
              <td>
                <div class="returns-product-card">
                  <span class="returns-product-media">
                    <img
                      v-if="itemImage(item)"
                      :src="itemImage(item)"
                      :alt="item.product_title || item.sku || '退货商品'"
                      width="192"
                      height="192"
                      loading="lazy"
                      decoding="async"
                      referrerpolicy="no-referrer"
                      @error="markReturnImageUnavailable(item.image_url)"
                    />
                    <span v-else>暂无图片</span>
                  </span>
                  <span class="returns-product-copy">
                    <strong>{{ item.product_title || item.company_product_name || item.sku || "未匹配商品名称" }}</strong>
                    <small>公司 SKU {{ item.company_sku || "—" }}</small>
                    <small>平台 SKU {{ item.sku || "—" }} · Offer {{ item.offer_id || "—" }}</small>
                    <span class="returns-product-action">
                      {{ hasCompanySku(item) ? "点击整行查看该公司 SKU 全部退货" : "未关联公司 SKU" }}
                    </span>
                  </span>
                </div>
              </td>
              <td>
                <strong>{{ item.store_name }}</strong>
                <span>{{ item.quantity }} 件</span>
                <small>{{ item.return_region || "地区未提供" }}</small>
              </td>
              <td><strong>{{ item.return_reason_label }}</strong></td>
              <td>
                <strong>{{ outcomeText(item) }}</strong>
                <small v-if="!item.outcome_statuses.length">当前 expansion 没有结果</small>
              </td>
              <td class="removal-lifecycle-cell">
                <strong>
                  {{ item.removal_lifecycle?.po_reference
                    ? `PO ${item.removal_lifecycle.po_reference}`
                    : item.removal_lifecycle?.message || removalStageLabel(item.removal_lifecycle) }}
                </strong>
                <template v-if="item.removal_lifecycle?.stage !== 'not_applicable'">
                  <span
                    class="removal-stage-pill"
                    :class="[
                      `stage-${item.removal_lifecycle?.stage || 'unknown'}`,
                      `expiry-${item.removal_lifecycle?.expiry_status || 'unknown'}`,
                    ]"
                  >
                    {{ removalStageLabel(item.removal_lifecycle) }}
                  </span>
                  <small>{{ removalExpiryLabel(item.removal_lifecycle) }}</small>
                  <small>{{ removalCollectLabel(item.removal_lifecycle) }}</small>
                  <small>{{ removalBookingLabel(item.removal_lifecycle) }}</small>
                  <small>{{ removalCollectionLabel(item.removal_lifecycle) }}</small>
                  <small class="removal-w8-result">{{ removalW8Label(item.removal_lifecycle) }}</small>
                </template>
              </td>
              <td class="returns-comment">{{ item.customer_comment || "未提供" }}</td>
              <td>
                <strong>{{ transactionText(item) }}</strong>
                <small>合计 {{ formatZar(item.transaction_total_incl_vat) }}</small>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else class="returns-empty">
        <strong>{{ payload.data_status === "collected" ? "当前筛选没有匹配退货明细" : "当前没有可确认的退货明细" }}</strong>
        <span>{{ statusMessage(payload.data_status) }}</span>
      </div>

      <footer class="returns-pagination">
        <span>共 {{ payload.total }} 条 · 第 {{ payload.page }} / {{ pageCount }} 页</span>
        <label>
          每页
          <select v-model.number="pageSize" @change="resetPage">
            <option :value="20">20</option>
            <option :value="50">50</option>
            <option :value="100">100</option>
          </select>
        </label>
        <button class="quiet-button" type="button" :disabled="page <= 1" @click="changePage(page - 1)">上一页</button>
        <button class="quiet-button" type="button" :disabled="page >= pageCount" @click="changePage(page + 1)">下一页</button>
      </footer>
    </template>
  </section>

  <Teleport to="body">
    <div
      v-if="companySkuDetailOpen && companySkuDetailItem"
      class="company-sku-modal-backdrop"
      @click.self="closeCompanySkuDetail()"
    >
      <section
        class="company-sku-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="company-sku-modal-title"
      >
        <header class="company-sku-modal-header">
          <div>
            <p class="section-kicker">COMPANY SKU RETURNS</p>
            <h2 id="company-sku-modal-title">
              {{ companySkuDetailItem.company_sku }} 的全部退货情况
            </h2>
            <p>
              {{ companySkuDetailItem.company_product_name || companySkuDetailItem.product_title || "未匹配商品名称" }}
              · {{ scopeLabel }} · {{ props.rangeStart }} 至 {{ props.rangeEnd }}
            </p>
          </div>
          <button
            type="button"
            class="company-sku-modal-close"
            aria-label="关闭公司 SKU 退货明细"
            @click="closeCompanySkuDetail()"
          >
            ×
          </button>
        </header>

        <div class="company-sku-modal-body">
          <section class="company-sku-summary" aria-label="公司 SKU 退货汇总">
            <article>
              <span>退货记录</span>
              <strong>{{ companySkuDetailSummary.recordCount }}</strong>
            </article>
            <article>
              <span>退货件数</span>
              <strong>{{ companySkuDetailSummary.returnUnits }}</strong>
            </article>
            <article>
              <span>涉及店铺</span>
              <strong>{{ companySkuDetailSummary.storeCount }}</strong>
            </article>
            <article>
              <span>自有链接</span>
              <strong>{{ companySkuDetailOwnLinks.length }}</strong>
            </article>
          </section>

          <section class="company-sku-own-links" aria-labelledby="company-sku-own-links-title">
            <div>
              <h3 id="company-sku-own-links-title">商品自有链接详情</h3>
            </div>
            <div v-if="companySkuDetailOwnLinks.length" class="company-sku-own-link-list">
              <button
                v-for="link in companySkuDetailOwnLinks"
                :key="link.plid"
                type="button"
                class="company-sku-own-link"
                @click="openCompanySkuOwnLink(link)"
              >
                <span class="company-sku-own-link-media">
                  <img
                    v-if="companySkuOwnLinkImage(link)"
                    :src="companySkuOwnLinkImage(link)"
                    :alt="link.productTitle"
                    width="192"
                    height="192"
                    loading="lazy"
                    decoding="async"
                    referrerpolicy="no-referrer"
                    @error="markReturnImageUnavailable(link.imageUrl)"
                  />
                  <span v-else>暂无图片</span>
                </span>
                <span class="company-sku-own-link-copy">
                  <span>{{ link.productTitle }}</span>
                  <small>PLID {{ link.plid }} · {{ link.storeName }}</small>
                  <strong>打开自有详情 ↗</strong>
                </span>
              </button>
            </div>
            <p v-else class="company-sku-own-link-empty">当前退货记录尚未关联可打开的 PLID。</p>
            <p v-if="companySkuDetailActionMessage" class="company-sku-action-message" role="status">
              {{ companySkuDetailActionMessage }}
            </p>
          </section>

          <section
            v-if="companySkuDetailPayload"
            class="company-sku-source-status"
            :class="`status-${companySkuDetailPayload.data_status}`"
          >
            <strong>{{ statusLabel(companySkuDetailPayload.data_status) }}</strong>
            <span>{{ companySkuDetailPayload.source_notice }}</span>
          </section>

          <div v-if="companySkuDetailLoading" class="company-sku-detail-state" role="status">
            <strong>正在读取该公司 SKU 的全部候选退货明细……</strong>
            <span v-if="companySkuDetailProgress">{{ companySkuDetailProgress }}</span>
          </div>
          <div v-else-if="companySkuDetailError" class="company-sku-detail-state is-error" role="alert">
            <strong>明细读取失败</strong>
            <span>{{ companySkuDetailError }}</span>
          </div>
          <div v-else-if="!companySkuDetailItems.length" class="company-sku-detail-state">
            <strong>当前范围没有精确匹配的退货记录</strong>
          </div>
          <div v-else class="company-sku-return-list">
            <article
              v-for="item in companySkuDetailVisibleItems"
              :key="item.store_scope_key"
              class="company-sku-return-card"
            >
              <header>
                <div>
                  <strong>{{ item.return_date || "日期未提供" }}</strong>
                  <span>{{ item.store_name }} · {{ item.quantity }} 件</span>
                </div>
                <span>{{ item.return_reason_label }}</span>
              </header>
              <dl>
                <div>
                  <dt>处理结果</dt>
                  <dd>{{ outcomeText(item) }}</dd>
                </div>
                <div>
                  <dt>移除 PO</dt>
                  <dd>
                    {{ item.removal_lifecycle?.po_reference ? `PO ${item.removal_lifecycle.po_reference}` : removalStageLabel(item.removal_lifecycle) }}
                    · {{ removalExpiryLabel(item.removal_lifecycle) }}
                    · {{ removalCollectLabel(item.removal_lifecycle) }}
                    · {{ removalBookingLabel(item.removal_lifecycle) }}
                  </dd>
                </div>
                <div>
                  <dt>提货与长睿处置</dt>
                  <dd>{{ removalCollectionLabel(item.removal_lifecycle) }} · {{ removalW8Label(item.removal_lifecycle) }}</dd>
                </div>
                <div>
                  <dt>客户备注</dt>
                  <dd>{{ item.customer_comment || "未提供" }}</dd>
                </div>
                <div>
                  <dt>交易展开</dt>
                  <dd>{{ transactionText(item) }} · 合计 {{ formatZar(item.transaction_total_incl_vat) }}</dd>
                </div>
                <div>
                  <dt>商品关联</dt>
                  <dd>
                    平台 SKU {{ item.sku || "—" }} · Offer {{ item.offer_id || "—" }} · PLID {{ item.productline_id || "—" }}
                  </dd>
                </div>
                <div>
                  <dt>退货编号</dt>
                  <dd>
                    RRN {{ item.return_reference_number || "—" }} · Seller Return {{ item.seller_return_id }} · 订单 {{ item.order_id || "—" }}
                  </dd>
                </div>
                <div>
                  <dt>退货地区</dt>
                  <dd>{{ item.return_region || "未提供" }}</dd>
                </div>
              </dl>
            </article>
          </div>
        </div>

        <footer class="company-sku-modal-footer">
          <span>
            共 {{ companySkuDetailItems.length }} 条<span v-if="companySkuDetailItems.length"> · 第 {{ companySkuDetailPage }} / {{ companySkuDetailPageCount }} 页</span>
          </span>
          <div>
            <button
              v-if="companySkuDetailPageCount > 1"
              type="button"
              class="quiet-button"
              :disabled="companySkuDetailPage <= 1"
              @click="changeCompanySkuDetailPage(companySkuDetailPage - 1)"
            >
              上一页
            </button>
            <button
              v-if="companySkuDetailPageCount > 1"
              type="button"
              class="quiet-button"
              :disabled="companySkuDetailPage >= companySkuDetailPageCount"
              @click="changeCompanySkuDetailPage(companySkuDetailPage + 1)"
            >
              下一页
            </button>
            <button type="button" class="primary-button" @click="closeCompanySkuDetail()">关闭</button>
          </div>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.returns-page { display: grid; gap: 18px; }
.returns-hero { display: flex; justify-content: space-between; gap: 24px; align-items: flex-end; padding: 24px; border: 1px solid #e3d5bf; border-radius: 20px; background: linear-gradient(135deg, #fffdf9, #f6ecdd); }
.returns-hero h1, .returns-hero p { margin: 0; }
.returns-hero > div:first-child { display: grid; gap: 7px; }
.returns-hero > div:first-child > p:last-child { max-width: 850px; color: var(--muted); font-size: .82rem; line-height: 1.65; }
.returns-removal-sync { display: grid; justify-items: end; gap: 6px; }
.returns-removal-sync small { color: var(--muted); font-size: .65rem; }
.returns-removal-otp { display: grid; grid-template-columns: minmax(240px, 1fr) minmax(150px, 220px) auto; gap: 12px; align-items: end; padding: 16px 18px; border: 1px solid #d5b66c; border-radius: 14px; background: #fff8e4; }
.returns-removal-otp > div { display: grid; gap: 4px; }
.returns-removal-otp span { color: #775d30; font-size: .7rem; }
.returns-removal-otp input { min-height: 38px; border: 1px solid #d5b66c; border-radius: 9px; padding: 7px 10px; background: #fff; }
.returns-sync-message { margin: 0; padding: 11px 14px; border: 1px solid #b9dac7; border-radius: 11px; background: #eef8f2; color: #35674d; font-size: .72rem; }
.returns-loading, .returns-inline-loading, .returns-empty { padding: 22px; border: 1px dashed var(--line); border-radius: 14px; color: var(--muted); text-align: center; }
.returns-status-banner { display: grid; gap: 8px; padding: 16px 18px; border: 1px solid #dfc99e; border-radius: 14px; background: #fff8e6; }
.returns-status-banner > div { display: flex; gap: 12px; align-items: baseline; }
.returns-status-banner span, .returns-status-banner small { color: #7c6034; font-size: .72rem; line-height: 1.5; }
.returns-status-banner.status-collected { border-color: #b9dac7; background: #eef8f2; }
.returns-status-banner.status-collected span, .returns-status-banner.status-collected small { color: #35674d; }
.returns-status-banner.status-failed, .returns-status-banner.status-unavailable { border-color: #dfb7b2; background: #fff0ee; }
.removal-orders-module { display: grid; gap: 14px; padding: 19px; border: 1px solid #bfcfc7; border-radius: 18px; background: linear-gradient(145deg, #fbfefc, #edf5f1); }
.removal-orders-header { display: flex; justify-content: space-between; gap: 20px; align-items: flex-start; }
.removal-orders-header > div { display: grid; gap: 5px; }
.removal-orders-header h2, .removal-orders-header p { margin: 0; }
.removal-orders-header > div > p:last-child { max-width: 900px; color: var(--muted); font-size: .7rem; line-height: 1.55; }
.removal-orders-header > span { padding: 6px 10px; border: 1px solid #d6caa9; border-radius: 999px; background: #fff8e6; color: #775d30; font-size: .65rem; font-weight: 800; white-space: nowrap; }
.removal-orders-header > span.status-synced { border-color: #acd2bd; background: #eaf7ef; color: #2e6848; }
.removal-order-tabs { display: flex; gap: 4px; overflow-x: auto; border-bottom: 1px solid #c8d8d0; }
.removal-order-tabs button { min-height: 40px; padding: 9px 15px; border: 0; border-bottom: 3px solid transparent; background: transparent; color: #64736b; font-size: .7rem; font-weight: 750; white-space: nowrap; cursor: pointer; }
.removal-order-tabs button.active { border-bottom-color: #2376a8; color: #195c83; background: rgb(255 255 255 / 72%); }
.removal-order-tabs button:focus-visible { outline: 2px solid #2376a8; outline-offset: -3px; }
.removal-order-filters { display: grid; grid-template-columns: minmax(300px, 1fr) minmax(220px, 320px); gap: 10px; }
.removal-order-filters label { display: grid; gap: 5px; color: var(--muted); font-size: .66rem; }
.removal-order-filters input, .removal-order-filters select { min-height: 38px; padding: 7px 10px; border: 1px solid #c8d8d0; border-radius: 9px; background: #fff; color: var(--ink); }
.removal-orders-source, .removal-orders-warning { margin: 0; padding: 10px 12px; border-radius: 10px; color: #52645b; font-size: .66rem; line-height: 1.5; }
.removal-orders-source { border: 1px solid #c8d8d0; background: rgb(255 255 255 / 72%); }
.removal-orders-warning { border: 1px solid #dfc99e; background: #fff8e6; color: #7c6034; }
.removal-orders-table-wrap { overflow-x: auto; border: 1px solid #c8d8d0; border-radius: 13px; background: #fff; }
.removal-orders-table { width: 100%; min-width: 1680px; border-collapse: collapse; font-size: .69rem; }
.removal-orders-table th, .removal-orders-table td { padding: 11px; border-bottom: 1px solid #dce6e1; text-align: left; vertical-align: top; }
.removal-orders-table th { background: #edf5f1; color: #466055; font-size: .64rem; }
.removal-orders-table td > strong, .removal-orders-table td > span, .removal-orders-table td > small { display: block; margin-bottom: 3px; }
.removal-orders-table td span, .removal-orders-table td small { color: var(--muted); font-size: .62rem; line-height: 1.45; }
.removal-order-row { cursor: pointer; }
.removal-order-row:hover > td, .removal-order-row:focus-visible > td { background: #f7fbf9; }
.removal-order-row:focus-visible { outline: 2px solid #327758; outline-offset: -2px; }
.removal-order-failure { color: #a23f37 !important; font-weight: 750; }
.removal-order-detail-row > td { padding: 0; background: #f6faf8; }
.removal-order-detail { display: grid; gap: 13px; padding: 17px; border-top: 2px solid #8eb5a1; }
.removal-order-detail > header { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }
.removal-order-detail > header > div { display: grid; gap: 4px; }
.removal-order-detail > header strong { color: #29483a; font-size: .82rem; }
.removal-order-detail > header span { color: var(--muted); font-size: .64rem; }
.removal-order-detail dl { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 9px; margin: 0; }
.removal-order-detail dl > div { display: grid; gap: 3px; min-width: 0; padding: 9px 10px; border: 1px solid #d5e2dc; border-radius: 9px; background: #fff; }
.removal-order-detail dt { color: var(--muted); font-size: .59rem; font-weight: 700; }
.removal-order-detail dd { margin: 0; color: #36483f; font-size: .66rem; line-height: 1.45; overflow-wrap: anywhere; }
.removal-order-w8-note { margin: 0; padding: 9px 11px; border: 1px solid #d5c697; border-radius: 9px; background: #fff9e9; color: #6f5a31; font-size: .64rem; line-height: 1.5; }
.removal-order-items-wrap { overflow-x: auto; border: 1px solid #d5e2dc; border-radius: 10px; background: #fff; }
.removal-order-items-table { width: 100%; min-width: 1420px; border-collapse: collapse; font-size: .65rem; }
.removal-order-items-table th, .removal-order-items-table td { padding: 9px; border-bottom: 1px solid #e2eae6; text-align: left; vertical-align: top; }
.removal-order-items-table th { background: #edf5f1; color: #466055; font-size: .6rem; }
.removal-order-items-table td > strong, .removal-order-items-table td > small { display: block; margin-bottom: 3px; }
.removal-order-product { display: grid; grid-template-columns: 48px minmax(180px, 1fr); gap: 9px; align-items: center; }
.removal-order-product-image { display: grid; width: 48px; height: 48px; place-items: center; overflow: hidden; border: 1px solid #d5e2dc; border-radius: 8px; background: #f7f9f8; }
.removal-order-product-image img { width: 100%; height: 100%; object-fit: contain; background: #fff; }
.removal-order-pagination { display: flex; justify-content: space-between; gap: 10px; align-items: center; color: var(--muted); font-size: .67rem; }
.removal-order-pagination > div { display: flex; gap: 8px; }
.removal-lifecycle-board { display: grid; gap: 12px; padding: 18px; border: 1px solid #d7c49a; border-radius: 18px; background: linear-gradient(145deg, #fffdf7, #f7f1e5); }
.removal-lifecycle-board > header { display: flex; justify-content: space-between; gap: 18px; align-items: end; }
.removal-lifecycle-board h2, .removal-lifecycle-board p { margin: 0; }
.removal-lifecycle-board > header > div { display: grid; gap: 4px; }
.removal-lifecycle-board > header > span { color: #735b33; font-size: .68rem; text-align: right; }
.removal-lifecycle-grid { display: grid; grid-template-columns: repeat(8, minmax(0, 1fr)); gap: 8px; }
.removal-lifecycle-grid article { display: grid; gap: 5px; min-width: 0; padding: 12px; border: 1px solid #e2d7bf; border-radius: 12px; background: rgb(255 255 255 / 82%); }
.removal-lifecycle-grid article.is-warning { border-color: #ddbc78; background: #fff7df; }
.removal-lifecycle-grid article.is-danger { border-color: #d8a7a0; background: #fff0ee; }
.removal-lifecycle-grid span, .removal-lifecycle-grid small { color: var(--muted); font-size: .62rem; line-height: 1.4; }
.removal-lifecycle-grid strong { color: #2f4439; font-size: 1.02rem; }
.removal-source-statuses { display: flex; flex-wrap: wrap; gap: 7px; }
.removal-source-statuses > span { display: flex; gap: 6px; align-items: baseline; padding: 6px 9px; border: 1px solid #e0d5be; border-radius: 999px; background: #fff; color: var(--muted); font-size: .62rem; }
.removal-source-statuses small { color: #876b3e; }
.returns-summary-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; }
.returns-summary-grid article { display: grid; gap: 7px; min-width: 0; padding: 15px; border: 1px solid var(--line); border-radius: 14px; background: var(--panel); }
.returns-summary-grid span, .returns-summary-grid small { color: var(--muted); font-size: .67rem; line-height: 1.45; }
.returns-summary-grid strong { color: #2e3b34; font-size: 1.1rem; }
.returns-store-statuses { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 9px; }
.returns-store-statuses article { display: grid; gap: 6px; padding: 12px 14px; border: 1px solid var(--line); border-radius: 12px; background: #fff; }
.returns-store-statuses article > div { display: flex; justify-content: space-between; gap: 8px; }
.returns-store-statuses span, .returns-store-statuses small { color: var(--muted); font-size: .66rem; }
.returns-store-statuses .status-collected { border-color: #b9dac7; }
.returns-store-statuses .status-failed, .returns-store-statuses .status-unavailable { border-color: #dfb7b2; }
.returns-store-error { color: #9b3b34 !important; }
.returns-filters { display: grid; grid-template-columns: minmax(260px, 1.4fr) minmax(170px, .7fr) minmax(170px, .7fr) auto; gap: 10px; align-items: end; padding: 15px; border: 1px solid var(--line); border-radius: 14px; background: var(--panel); }
.returns-filters label { display: grid; gap: 5px; color: var(--muted); font-size: .68rem; }
.returns-filters input, .returns-filters select, .returns-pagination select { min-height: 38px; border: 1px solid var(--line); border-radius: 9px; background: #fff; padding: 7px 9px; color: var(--ink); }
.returns-filter-actions, .returns-pagination { display: flex; gap: 8px; align-items: center; }
.returns-table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 14px; background: var(--panel); }
.returns-table { width: 100%; min-width: 1540px; border-collapse: collapse; font-size: .72rem; }
.returns-table th, .returns-table td { padding: 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
.returns-table th { background: #f8f5ef; color: #705b3f; font-size: .67rem; }
.returns-table td > strong, .returns-table td > span, .returns-table td > small { display: block; margin-bottom: 3px; }
.returns-table small, .returns-table span { color: var(--muted); font-size: .65rem; line-height: 1.45; }
.returns-table tbody tr.returns-record-row > td { transition: background-color .15s ease; }
.returns-table tbody tr.returns-record-row.is-clickable { cursor: pointer; }
.returns-table tbody tr.returns-record-row.is-clickable:hover > td,
.returns-table tbody tr.returns-record-row.is-clickable:focus-visible > td { background: #fffaf0; }
.returns-table tbody tr.returns-record-row.is-clickable:focus-visible { outline: 2px solid #9e6f2f; outline-offset: -2px; }
.returns-product-card { display: flex; gap: 10px; min-width: 270px; width: 100%; padding: 8px; border: 1px solid transparent; border-radius: 11px; background: transparent; color: inherit; text-align: left; cursor: inherit; }
.returns-product-media { display: grid; width: 54px; height: 54px; flex: 0 0 auto; place-items: center; overflow: hidden; border: 1px solid var(--line); border-radius: 9px; background: #fff; color: var(--muted); font-size: .58rem; }
.returns-product-media img { width: 100%; height: 100%; object-fit: contain; }
.returns-product-copy { display: grid; gap: 3px; min-width: 0; }
.returns-product-copy strong, .returns-product-copy small { display: block; }
.returns-product-copy small { color: var(--muted); font-size: .65rem; line-height: 1.45; }
.returns-product-action { width: fit-content; margin-top: 3px; color: #8a5c22; font-size: .64rem; font-weight: 800; }
.returns-comment { min-width: 180px; max-width: 300px; overflow-wrap: anywhere; line-height: 1.5; }
.removal-lifecycle-cell { min-width: 260px; max-width: 340px; }
.removal-lifecycle-cell > strong, .removal-lifecycle-cell > small { display: block; }
.removal-stage-pill { display: inline-flex !important; width: fit-content; margin: 3px 0 6px; padding: 3px 7px; border-radius: 999px; background: #edf1ee; color: #52645b !important; font-weight: 800; }
.removal-stage-pill.stage-pickup_ready { background: #e5f5eb; color: #276142 !important; }
.removal-stage-pill.expiry-expiring { background: #fff0c8; color: #8b5a09 !important; }
.removal-stage-pill.expiry-expired { background: #fde3df; color: #9b3b34 !important; }
.removal-w8-result { margin-top: 5px; padding-top: 5px; border-top: 1px dashed #d8ccb7; color: #5b4930 !important; font-weight: 700; }
.returns-empty { display: grid; gap: 6px; }
.returns-pagination { justify-content: flex-end; color: var(--muted); font-size: .7rem; }
.returns-pagination label { display: flex; align-items: center; gap: 5px; }
.returns-pagination select { min-height: 32px; }
.company-sku-modal-backdrop { position: fixed; inset: 0; z-index: 2200; display: grid; place-items: center; padding: 24px; background: rgba(25, 29, 26, .56); backdrop-filter: blur(3px); }
.company-sku-modal { display: grid; grid-template-rows: auto minmax(0, 1fr) auto; width: min(1120px, 100%); max-height: min(900px, calc(100vh - 48px)); overflow: hidden; border: 1px solid #d8c7aa; border-radius: 20px; background: #fdfbf7; box-shadow: 0 28px 80px rgba(30, 27, 21, .28); }
.company-sku-modal-header { display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; padding: 20px 22px 16px; border-bottom: 1px solid var(--line); background: linear-gradient(135deg, #fffdf9, #f5e9d7); }
.company-sku-modal-header > div { display: grid; gap: 5px; }
.company-sku-modal-header h2, .company-sku-modal-header p { margin: 0; }
.company-sku-modal-header h2 { color: #2e3b34; font-size: 1.25rem; }
.company-sku-modal-header > div > p:last-child { color: var(--muted); font-size: .72rem; line-height: 1.5; }
.company-sku-modal-close { width: 36px; height: 36px; flex: 0 0 auto; border: 1px solid #d7c6aa; border-radius: 50%; background: rgba(255,255,255,.86); color: #6d5636; font-size: 1.35rem; line-height: 1; cursor: pointer; }
.company-sku-modal-close:focus-visible { outline: 3px solid rgba(158,111,47,.22); }
.company-sku-modal-body { display: grid; gap: 14px; overflow-y: auto; padding: 18px 22px 22px; }
.company-sku-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 9px; }
.company-sku-summary article { display: grid; gap: 5px; padding: 12px 14px; border: 1px solid var(--line); border-radius: 12px; background: #fff; }
.company-sku-summary span { color: var(--muted); font-size: .66rem; }
.company-sku-summary strong { color: #2e3b34; font-size: 1.05rem; }
.company-sku-own-links { display: grid; gap: 10px; padding: 14px; border: 1px solid #d8c7aa; border-radius: 14px; background: #fffaf1; }
.company-sku-own-links > div:first-child { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }
.company-sku-own-links h3, .company-sku-own-links p { margin: 0; }
.company-sku-own-links h3 { color: #4d3b25; font-size: .82rem; }
.company-sku-own-links p { color: var(--muted); font-size: .66rem; line-height: 1.5; }
.company-sku-own-link-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 8px; }
.company-sku-own-link { display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 11px; align-items: center; padding: 10px; border: 1px solid #dac7a5; border-radius: 11px; background: #fff; color: #3a3329; text-align: left; cursor: pointer; }
.company-sku-own-link:hover { border-color: #a97936; background: #fffdf8; }
.company-sku-own-link:focus-visible { outline: 3px solid rgba(158,111,47,.2); }
.company-sku-own-link-media { display: grid; width: 72px; height: 72px; place-items: center; overflow: hidden; border: 1px solid var(--line); border-radius: 9px; background: #f7f5f1; color: var(--muted); font-size: .58rem; }
.company-sku-own-link-media img { width: 100%; height: 100%; object-fit: contain; background: #fff; }
.company-sku-own-link-copy { display: grid; min-width: 0; gap: 4px; }
.company-sku-own-link-copy > span { overflow: hidden; font-size: .72rem; font-weight: 750; text-overflow: ellipsis; white-space: nowrap; }
.company-sku-own-link small { color: var(--muted); font-size: .62rem; }
.company-sku-own-link strong { color: #8a5c22; font-size: .65rem; }
.company-sku-own-link-empty, .company-sku-action-message { padding: 8px 10px; border-radius: 9px; background: #f2eee7; }
.company-sku-action-message { color: #9b3b34 !important; background: #fff0ee; }
.company-sku-source-status { display: flex; gap: 10px; align-items: baseline; padding: 10px 12px; border: 1px solid #dfc99e; border-radius: 10px; background: #fff8e6; }
.company-sku-source-status strong { flex: 0 0 auto; color: #755834; font-size: .68rem; }
.company-sku-source-status span { color: var(--muted); font-size: .65rem; line-height: 1.5; }
.company-sku-source-status.status-collected { border-color: #b9dac7; background: #eef8f2; }
.company-sku-detail-state { display: grid; gap: 5px; padding: 24px; border: 1px dashed var(--line); border-radius: 12px; color: var(--muted); text-align: center; }
.company-sku-detail-state strong { color: #4e554f; }
.company-sku-detail-state span { font-size: .7rem; }
.company-sku-detail-state.is-error { border-color: #dfb7b2; background: #fff0ee; color: #9b3b34; }
.company-sku-return-list { display: grid; gap: 10px; }
.company-sku-return-card { display: grid; gap: 11px; padding: 14px; border: 1px solid var(--line); border-radius: 13px; background: #fff; }
.company-sku-return-card > header { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
.company-sku-return-card > header > div { display: grid; gap: 3px; }
.company-sku-return-card > header strong { color: #2f3f36; font-size: .78rem; }
.company-sku-return-card > header span { color: #805c29; font-size: .68rem; font-weight: 700; }
.company-sku-return-card dl { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px 16px; margin: 0; }
.company-sku-return-card dl > div { display: grid; gap: 3px; min-width: 0; }
.company-sku-return-card dt { color: var(--muted); font-size: .61rem; font-weight: 700; }
.company-sku-return-card dd { margin: 0; color: #454a46; font-size: .69rem; line-height: 1.5; overflow-wrap: anywhere; }
.company-sku-modal-footer { display: flex; justify-content: space-between; gap: 12px; align-items: center; padding: 13px 22px; border-top: 1px solid var(--line); background: #fff; color: var(--muted); font-size: .68rem; }
.company-sku-modal-footer > div { display: flex; gap: 8px; }
@media (max-width: 1180px) { .removal-order-detail dl { grid-template-columns: repeat(2, minmax(0, 1fr)); } .removal-lifecycle-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); } .returns-summary-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } .returns-filters { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 720px) { .returns-hero, .removal-orders-header, .removal-order-detail > header, .removal-lifecycle-board > header { align-items: flex-start; flex-direction: column; } .returns-removal-sync { justify-items: start; } .returns-removal-otp, .removal-order-filters, .removal-order-detail dl, .removal-lifecycle-grid, .returns-summary-grid, .returns-filters, .company-sku-summary, .company-sku-return-card dl { grid-template-columns: 1fr; } .removal-lifecycle-board > header > span { text-align: left; } .returns-status-banner > div, .removal-order-pagination, .returns-pagination, .company-sku-own-links > div:first-child, .company-sku-modal-footer { align-items: flex-start; flex-direction: column; } .company-sku-modal-backdrop { padding: 0; } .company-sku-modal { width: 100%; max-height: 100vh; border-radius: 0; } .company-sku-modal-header, .company-sku-modal-body, .company-sku-modal-footer { padding-left: 15px; padding-right: 15px; } .company-sku-own-link { grid-template-columns: 60px minmax(0, 1fr); } .company-sku-own-link-media { width: 60px; height: 60px; } }
</style>
