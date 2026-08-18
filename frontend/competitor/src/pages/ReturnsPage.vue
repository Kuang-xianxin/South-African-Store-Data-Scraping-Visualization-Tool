<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";

import { fetchReturns } from "../api";
import { PRODUCT_IMAGE_SIZE, productThumbnailUrl } from "../productImages";
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
let requestRevision = 0;
let activeController: AbortController | null = null;

const pageCount = computed(() => Math.max(1, Math.ceil((payload.value?.total ?? 0) / pageSize.value)));
const scopeLabel = computed(() =>
  (props.storeScope ?? "current") === "current"
    ? "当前店铺"
    : props.multiStoreLabel || ((props.storeScope ?? "current") === "operating" ? "我的运营店铺" : "全部店铺"),
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

onBeforeUnmount(() => {
  requestRevision += 1;
  activeController?.abort();
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
  if (status === "collected") return "所选店铺的最近成功 /returns 批次完整覆盖当前日期区间；当前筛选为 0 时可解释为该已采区间没有匹配明细。";
  if (status === "partial") return "部分店铺或部分日期未被成功批次完整覆盖；表格保留已有明细，不能把未覆盖部分当作零退货。";
  if (status === "failed") return "所选店铺的退货明细采集失败；Offers 的滚动30天计数仍会分开展示。";
  if (status === "unavailable") return "本地退货明细暂不可读，请稍后刷新或检查服务状态。";
  return "还没有成功采集 /returns；此时表格为空代表未采集，不代表没有退货。";
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
  return productThumbnailUrl(item.image_url, PRODUCT_IMAGE_SIZE.list);
}
</script>

<template>
  <section class="returns-page">
    <header class="returns-hero">
      <div>
        <p class="section-kicker">SELLER RETURNS</p>
        <h1>退货管理</h1>
        <p>
          汇总 {{ scopeLabel }} 在 {{ props.rangeStart }} 至 {{ props.rangeEnd }} 的退货原因、客户备注、处理结果与交易展开。
          退货日期按南非业务日解释。
        </p>
      </div>
      <div class="returns-source-chip">GET /v1/returns · outcomes · transactions</div>
    </header>

    <p v-if="error" class="error-banner">{{ error }}</p>
    <div v-if="loading && !payload" class="returns-loading">正在读取退货明细……</div>

    <template v-if="payload">
      <section class="returns-status-banner" :class="`status-${payload.data_status}`">
        <div>
          <strong>{{ statusLabel(payload.data_status) }}</strong>
          <span>{{ statusMessage(payload.data_status) }}</span>
        </div>
        <small>{{ payload.source_notice }}</small>
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
            <tr v-for="item in payload.items" :key="item.store_scope_key">
              <td>
                <strong>{{ item.return_date || "—" }}</strong>
                <small>RRN {{ item.return_reference_number || "—" }}</small>
                <small>Seller Return {{ item.seller_return_id }}</small>
                <small>订单 {{ item.order_id || "—" }}</small>
              </td>
              <td>
                <div class="returns-product-cell">
                  <img v-if="itemImage(item)" :src="itemImage(item)" :alt="item.product_title || item.sku || '退货商品'" />
                  <div>
                    <strong>{{ item.product_title || item.company_product_name || item.sku || "未匹配商品名称" }}</strong>
                    <small>公司 SKU {{ item.company_sku || "—" }}</small>
                    <small>平台 SKU {{ item.sku || "—" }} · Offer {{ item.offer_id || "—" }}</small>
                  </div>
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
</template>

<style scoped>
.returns-page { display: grid; gap: 18px; }
.returns-hero { display: flex; justify-content: space-between; gap: 24px; align-items: flex-end; padding: 24px; border: 1px solid #e3d5bf; border-radius: 20px; background: linear-gradient(135deg, #fffdf9, #f6ecdd); }
.returns-hero h1, .returns-hero p { margin: 0; }
.returns-hero > div:first-child { display: grid; gap: 7px; }
.returns-hero > div:first-child > p:last-child { max-width: 850px; color: var(--muted); font-size: .82rem; line-height: 1.65; }
.returns-source-chip { padding: 8px 11px; border: 1px solid #d9c4a2; border-radius: 999px; background: rgba(255,255,255,.82); color: #77522c; font: 700 .68rem/1.2 monospace; white-space: nowrap; }
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
.returns-product-cell { display: flex; gap: 10px; min-width: 270px; }
.returns-product-cell img { width: 54px; height: 54px; flex: 0 0 auto; border: 1px solid var(--line); border-radius: 9px; object-fit: contain; background: #fff; }
.returns-product-cell > div { display: grid; gap: 3px; }
.returns-comment { min-width: 180px; max-width: 300px; overflow-wrap: anywhere; line-height: 1.5; }
.returns-empty { display: grid; gap: 6px; }
.returns-pagination { justify-content: flex-end; color: var(--muted); font-size: .7rem; }
.returns-pagination label { display: flex; align-items: center; gap: 5px; }
.returns-pagination select { min-height: 32px; }
@media (max-width: 1180px) { .returns-summary-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } .returns-filters { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 720px) { .returns-hero { align-items: flex-start; flex-direction: column; } .returns-source-chip { white-space: normal; } .returns-summary-grid, .returns-filters { grid-template-columns: 1fr; } .returns-status-banner > div, .returns-pagination { align-items: flex-start; flex-direction: column; } }
</style>
