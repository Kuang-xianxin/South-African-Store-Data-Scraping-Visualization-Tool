<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { fetchReturns } from "../api";
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
watch(
  [() => props.rangeStart, () => props.rangeEnd, () => props.storeScope],
  () => {
    if (companySkuDetailOpen.value) closeCompanySkuDetail(false);
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
    </header>

    <p v-if="error" class="error-banner">{{ error }}</p>
    <div v-if="loading && !payload" class="returns-loading">正在读取退货明细……</div>

    <template v-if="payload">
      <section class="returns-status-banner" :class="`status-${payload.data_status}`">
        <div>
          <strong>{{ statusLabel(payload.data_status) }}</strong>
          <span>{{ statusMessage(payload.data_status) }}</span>
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
.returns-loading, .returns-inline-loading, .returns-empty { padding: 22px; border: 1px dashed var(--line); border-radius: 14px; color: var(--muted); text-align: center; }
.returns-status-banner { display: grid; gap: 8px; padding: 16px 18px; border: 1px solid #dfc99e; border-radius: 14px; background: #fff8e6; }
.returns-status-banner > div { display: flex; gap: 12px; align-items: baseline; }
.returns-status-banner span, .returns-status-banner small { color: #7c6034; font-size: .72rem; line-height: 1.5; }
.returns-status-banner.status-collected { border-color: #b9dac7; background: #eef8f2; }
.returns-status-banner.status-collected span, .returns-status-banner.status-collected small { color: #35674d; }
.returns-status-banner.status-failed, .returns-status-banner.status-unavailable { border-color: #dfb7b2; background: #fff0ee; }
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
.returns-table { width: 100%; min-width: 1220px; border-collapse: collapse; font-size: .72rem; }
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
@media (max-width: 1180px) { .returns-summary-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } .returns-filters { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 720px) { .returns-hero { align-items: flex-start; flex-direction: column; } .returns-summary-grid, .returns-filters, .company-sku-summary, .company-sku-return-card dl { grid-template-columns: 1fr; } .returns-status-banner > div, .returns-pagination, .company-sku-own-links > div:first-child, .company-sku-modal-footer { align-items: flex-start; flex-direction: column; } .company-sku-modal-backdrop { padding: 0; } .company-sku-modal { width: 100%; max-height: 100vh; border-radius: 0; } .company-sku-modal-header, .company-sku-modal-body, .company-sku-modal-footer { padding-left: 15px; padding-right: 15px; } .company-sku-own-link { grid-template-columns: 60px minmax(0, 1fr); } .company-sku-own-link-media { width: 60px; height: 60px; } }
</style>
