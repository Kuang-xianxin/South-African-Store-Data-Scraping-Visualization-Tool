<script setup lang="ts">
import { computed, ref, watch } from "vue";

import {
  confirmDailyReportEntry,
  confirmReadyDailyReportEntries,
  dismissDailyReportStockAlert,
  fetchDailyReport,
  fetchDailyReportExport,
  generateDailyReportExport,
  saveDailyReportManual,
  saveDailyReportNote,
} from "../api";
import { formatChinaDateTime } from "../time";
import type {
  DailyReportExport,
  DailyReportItem,
  DailyReportPayload,
} from "../types";

const props = defineProps<{ asOf: string; canOperate?: boolean }>();
const report = ref<DailyReportPayload | null>(null);
const exportState = ref<DailyReportExport | null>(null);
const loading = ref(false);
const saving = ref(false);
const message = ref("");
const search = ref("");
const filter = ref<"review" | "all" | "sales" | "stock">("review");
const editor = ref<{
  mode: "manual" | "confirm" | "note" | "dismiss" | "bulk" | null;
  item: DailyReportItem | null;
}>({ mode: null, item: null });
const form = ref({
  page_views_30_days: "",
  ordered_units: "",
  platform_stock: "",
  reason: "platform_delay",
  source: "evening" as "morning" | "evening" | "manual",
  note: "",
});

watch(() => props.asOf, load, { immediate: true });

const filteredItems = computed(() => {
  const term = search.value.trim().toLowerCase();
  return (report.value?.items ?? []).filter((item) => {
    if (
      term &&
      !`${item.sku ?? ""} ${item.title} ${item.offer_id}`.toLowerCase().includes(term)
    ) {
      return false;
    }
    if (filter.value === "sales") return Number(item.current.ordered_units || 0) > 0;
    if (filter.value === "stock") {
      return item.stock_check.mismatch && !item.stock_check.dismissed;
    }
    if (filter.value === "review") return item.status !== "confirmed";
    return true;
  });
});

async function load() {
  loading.value = true;
  message.value = "";
  try {
    [report.value, exportState.value] = await Promise.all([
      fetchDailyReport(props.asOf),
      fetchDailyReportExport(props.asOf),
    ]);
  } catch (error) {
    message.value = error instanceof Error ? error.message : "运营日报读取失败";
  } finally {
    loading.value = false;
  }
}

function openEditor(
  mode: "manual" | "confirm" | "note" | "dismiss" | "bulk",
  item: DailyReportItem | null = null,
) {
  editor.value = { mode, item };
  const current = item?.current;
  form.value = {
    page_views_30_days: toInput(current?.page_views_30_days),
    ordered_units: toInput(current?.ordered_units),
    platform_stock: toInput(current?.platform_stock),
    reason: "platform_delay",
    source: item?.manual_note
      ? "manual"
      : item?.evening
        ? "evening"
        : "morning",
    note: "",
  };
}

function closeEditor() {
  editor.value = { mode: null, item: null };
}

async function submitEditor() {
  if (!props.canOperate || !editor.value.mode) return;
  const mode = editor.value.mode;
  const item = editor.value.item;
  saving.value = true;
  message.value = "";
  try {
    if (mode === "manual" && item) {
      await saveDailyReportManual(props.asOf, item.offer_id, {
        page_views_30_days: parseInput(form.value.page_views_30_days),
        ordered_units: parseInput(form.value.ordered_units),
        platform_stock: parseInput(form.value.platform_stock),
        reason: form.value.reason,
        note: form.value.note,
      });
      message.value = "人工候选值已保存并标记，仍需最终确认。";
    } else if (mode === "confirm" && item) {
      const result = await confirmDailyReportEntry(
        props.asOf,
        item.offer_id,
        form.value.source,
        form.value.note,
      );
      message.value = result.exported
        ? "数据已确认；当天全部完成，Excel 已自动导出。"
        : "该商品已确认合并。";
    } else if (mode === "note" && item) {
      await saveDailyReportNote(props.asOf, item.offer_id, form.value.note);
      message.value = "异常备注已保存。";
    } else if (mode === "dismiss" && item) {
      await dismissDailyReportStockAlert(props.asOf, item.offer_id, form.value.note);
      message.value = "库存红色标记已取消，原因已留痕。";
    } else if (mode === "bulk") {
      const result = await confirmReadyDailyReportEntries(props.asOf, form.value.note);
      message.value = result.exported
        ? `已合并 ${result.confirmed} 个无差异商品，Excel 已自动导出。`
        : `已合并 ${result.confirmed} 个无差异商品。`;
    }
    closeEditor();
    await load();
  } catch (error) {
    message.value = error instanceof Error ? error.message : "保存失败";
  } finally {
    saving.value = false;
  }
}

async function runExport() {
  if (!props.canOperate) return;
  saving.value = true;
  try {
    exportState.value = await generateDailyReportExport(props.asOf);
    message.value = "运营日报 Excel 已导出到本地并可下载。";
  } catch (error) {
    message.value = error instanceof Error ? error.message : "导出失败";
    exportState.value = await fetchDailyReportExport(props.asOf).catch(() => null);
  } finally {
    saving.value = false;
  }
}

function statusLabel(status: DailyReportItem["status"]) {
  return {
    awaiting_evening: "等待18:00",
    ready: "无差异待合并",
    needs_review: "数据有差异",
    confirmed: "已确认",
  }[status];
}

function fieldLabel(key: string) {
  return {
    page_views_30_days: "近30天浏览量",
    ordered_units: "订单",
    platform_stock: "库存",
  }[key] ?? key;
}

function manualReasonLabel(reason: string | null) {
  return {
    platform_delay: "平台延迟人工值",
    stock_adjustment: "库存核对人工值",
    other: "其他人工值",
  }[reason ?? ""] ?? "人工值";
}

function value(value: number | null | undefined) {
  return value === null || value === undefined ? "—" : String(value);
}

function toInput(value: number | null | undefined) {
  return value === null || value === undefined ? "" : String(value);
}

function parseInput(value: string): number | null {
  return value.trim() === "" ? null : Number(value);
}
</script>

<template>
  <div class="erp-page daily-report-page">
    <div class="page-intro">
      <div>
        <p class="section-kicker">DAILY RECONCILIATION</p>
        <h2>早晚两版数据，由运营确认最终值</h2>
      </div>
      <div class="daily-run-times">
        <span>10:05 早间</span><i>→</i><span>18:00 晚间</span><i>→</i><span>18:30 待办快照</span>
      </div>
    </div>

    <p v-if="message" class="global-notice">{{ message }}</p>
    <div v-if="report?.prior_reminders.length" class="daily-reminder">
      <strong>开始今日工作前，请先处理历史未合并数据</strong>
      <span v-for="row in report.prior_reminders" :key="row.business_date">
        {{ row.business_date }}：{{ row.unresolved_count }} 个
      </span>
    </div>

    <section class="daily-kpis">
      <article><small>商品</small><strong>{{ report?.counts.products ?? 0 }}</strong></article>
      <article class="sales"><small>今日有销量</small><strong>{{ report?.counts.with_sales ?? 0 }}</strong></article>
      <article class="review"><small>数据有差异</small><strong>{{ report?.counts.needs_review ?? 0 }}</strong></article>
      <article><small>无差异待合并</small><strong>{{ report?.counts.ready ?? 0 }}</strong></article>
      <article class="danger"><small>库存不平</small><strong>{{ report?.counts.stock_alerts ?? 0 }}</strong></article>
      <article class="confirmed"><small>已确认</small><strong>{{ report?.counts.confirmed ?? 0 }}</strong></article>
    </section>

    <section class="erp-panel daily-workspace">
      <div class="daily-toolbar">
        <div class="daily-run-state">
          <span
            v-for="run in report?.runs ?? []"
            :key="run.run_id"
            :class="run.slot"
          >
            {{ run.slot === "morning" ? "早间" : "晚间" }}
            {{ formatChinaDateTime(run.captured_at, "—") }}
          </span>
          <span v-if="!report?.runs.length">当天还没有日报采集版本</span>
        </div>
        <div class="daily-actions">
          <button
            :disabled="!props.canOperate || !report?.counts.ready"
            @click="openEditor('bulk')"
          >
            批量合并无差异
          </button>
          <button
            class="action-button"
            :disabled="!props.canOperate || saving || !report?.counts.products"
            @click="runExport"
          >
            {{ exportState?.blocked ? `尚有 ${exportState.unresolved.length} 处未合并` : "导出 Excel" }}
          </button>
          <a v-if="exportState?.download_url" :href="exportState.download_url">下载已导出版本</a>
        </div>
      </div>

      <div v-if="exportState?.blocked" class="export-blocker">
        <strong>现在不能导出</strong>
        <span>
          {{ exportState.unresolved.slice(0, 4).map((row) => `${row.business_date} / ${row.sku || row.offer_id}`).join("；") }}
          {{ exportState.unresolved.length > 4 ? "……" : "" }}
        </span>
      </div>

      <div class="daily-filters">
        <input v-model="search" placeholder="搜索平台 SKU、商品名或 Offer ID" />
        <button :class="{ active: filter === 'review' }" @click="filter = 'review'">待处理</button>
        <button :class="{ active: filter === 'sales' }" @click="filter = 'sales'">有销量</button>
        <button :class="{ active: filter === 'stock' }" @click="filter = 'stock'">库存不平</button>
        <button :class="{ active: filter === 'all' }" @click="filter = 'all'">全部</button>
      </div>

      <div class="daily-table-wrap">
        <table class="daily-table">
          <thead>
            <tr>
              <th>商品</th>
              <th>10:05 早间<br />订单 / 库存</th>
              <th>18:00 晚间<br />订单 / 库存</th>
              <th>人工候选<br />订单 / 库存</th>
              <th>当前采用<br />订单 / 库存</th>
              <th>库存核对</th>
              <th>状态与操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in filteredItems" :key="item.offer_id">
              <td class="product-cell">
                <strong>{{ item.title }}</strong>
                <code>{{ item.sku || item.offer_id }}</code>
                <small>近30天浏览量 {{ value(item.current.page_views_30_days) }}</small>
                <span v-if="item.operator_note">备注：{{ item.operator_note }}</span>
              </td>
              <td :class="{ 'sales-hit': Number(item.morning?.ordered_units || 0) > 0 }">
                <strong>{{ value(item.morning?.ordered_units) }} / {{ value(item.morning?.platform_stock) }}</strong>
              </td>
              <td :class="{ 'sales-hit': Number(item.evening?.ordered_units || 0) > 0 }">
                <strong>{{ value(item.evening?.ordered_units) }} / {{ value(item.evening?.platform_stock) }}</strong>
              </td>
              <td>
                <strong>{{ value(item.manual?.ordered_units) }} / {{ value(item.manual?.platform_stock) }}</strong>
                <em v-if="item.manual_note">{{ manualReasonLabel(item.manual_reason) }}</em>
                <small v-if="item.manual_note">{{ item.manual_note }}</small>
              </td>
              <td
                :class="{
                  'sales-hit': Number(item.current.ordered_units || 0) > 0,
                  'stock-value-mismatch': item.stock_check.mismatch && !item.stock_check.dismissed,
                }"
              >
                <strong>{{ value(item.current.ordered_units) }} / {{ value(item.current.platform_stock) }}</strong>
                <small v-if="item.differences.length">
                  差异：{{ item.differences.map(fieldLabel).join("、") }}
                </small>
              </td>
              <td
                class="stock-check"
                :class="{
                  mismatch: item.stock_check.mismatch && !item.stock_check.dismissed,
                  dismissed: item.stock_check.dismissed,
                }"
              >
                <template v-if="item.stock_check.previous_stock !== null">
                  <span>{{ item.stock_check.previous_stock }} - {{ item.current.ordered_units || 0 }}</span>
                  <strong>= {{ item.stock_check.expected_stock }}</strong>
                  <small>实际 {{ value(item.stock_check.actual_stock) }}</small>
                </template>
                <span v-else>缺少前一日库存</span>
                <small v-if="item.stock_check.dismissed">已人工取消红标</small>
              </td>
              <td class="row-actions">
                <span class="status-badge" :class="item.status">{{ statusLabel(item.status) }}</span>
                <button v-if="props.canOperate" @click="openEditor('manual', item)">人工修改</button>
                <button
                  v-if="props.canOperate && item.status !== 'confirmed'"
                  @click="openEditor('confirm', item)"
                >
                  确认合并
                </button>
                <button v-if="props.canOperate" @click="openEditor('note', item)">加备注</button>
                <button
                  v-if="props.canOperate && item.stock_check.mismatch && !item.stock_check.dismissed"
                  class="danger-link"
                  @click="openEditor('dismiss', item)"
                >
                  取消库存红标
                </button>
              </td>
            </tr>
            <tr v-if="!loading && !filteredItems.length">
              <td colspan="7" class="empty-row">当前筛选没有商品。</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <div v-if="editor.mode" class="daily-modal-backdrop" @click.self="closeEditor">
      <form class="daily-modal" @submit.prevent="submitEditor">
        <p class="section-kicker">OPERATOR ACTION</p>
        <h3>
          {{
            editor.mode === "manual"
              ? "记录人工候选值"
              : editor.mode === "confirm"
                ? "确认最终采用值"
                : editor.mode === "dismiss"
                  ? "取消库存红色标记"
                  : editor.mode === "bulk"
                    ? "批量合并无差异商品"
                    : "记录异常备注"
          }}
        </h3>
        <p v-if="editor.item" class="modal-product">
          {{ editor.item.title }} · {{ editor.item.sku || editor.item.offer_id }}
        </p>
        <div v-if="editor.mode === 'manual'" class="manual-grid">
          <label>近30天浏览量<input v-model="form.page_views_30_days" type="number" min="0" /></label>
          <label>当天订单数<input v-model="form.ordered_units" type="number" min="0" /></label>
          <label>平台仓库存<input v-model="form.platform_stock" type="number" min="0" /></label>
          <label>
            修改原因
            <select v-model="form.reason">
              <option value="platform_delay">平台订单延迟</option>
              <option value="stock_adjustment">库存核对调整</option>
              <option value="other">其他</option>
            </select>
          </label>
        </div>
        <label v-if="editor.mode === 'confirm'">
          最终采用
          <select v-model="form.source">
            <option value="morning">10:05 早间值</option>
            <option value="evening">18:00 晚间值</option>
            <option value="manual">人工候选值</option>
          </select>
        </label>
        <label>
          {{ editor.mode === "confirm" || editor.mode === "bulk" ? "合并备注（必填）" : "操作备注（必填）" }}
          <textarea v-model="form.note" required maxlength="2000"></textarea>
        </label>
        <div class="modal-actions">
          <button type="button" @click="closeEditor">取消</button>
          <button class="action-button" :disabled="saving || !form.note.trim()">
            {{ saving ? "正在保存…" : "确认保存" }}
          </button>
        </div>
      </form>
    </div>
  </div>
</template>

<style scoped>
.daily-report-page { display: grid; gap: 18px; }
.daily-run-times { display: flex; align-items: center; gap: 10px; color: #506158; font-size: 12px; }
.daily-run-times span { padding: 8px 11px; border: 1px solid #d9e1db; border-radius: 999px; background: #f9fbf8; }
.daily-run-times i { color: #9aa79f; font-style: normal; }
.daily-reminder { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 14px 17px; border: 1px solid #e3b28d; border-left: 5px solid #d46b32; border-radius: 12px; background: #fff7ef; color: #873f1c; }
.daily-reminder strong { margin-right: 8px; }
.daily-reminder span { padding: 4px 8px; border-radius: 6px; background: #ffe8d2; font-size: 12px; }
.daily-kpis { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; }
.daily-kpis article { padding: 15px 16px; border-radius: 13px; background: #f9fbf8; box-shadow: 0 7px 24px rgba(32, 54, 43, .05); }
.daily-kpis small, .daily-kpis strong { display: block; }
.daily-kpis small { color: #718077; font-size: 11px; }
.daily-kpis strong { margin-top: 4px; color: #28473b; font-size: 23px; }
.daily-kpis .sales strong { color: #bb622a; }
.daily-kpis .review strong, .daily-kpis .danger strong { color: #bf4e3b; }
.daily-kpis .confirmed strong { color: #1e7954; }
.daily-workspace { overflow: hidden; }
.daily-toolbar { display: flex; justify-content: space-between; gap: 16px; padding: 18px 20px; border-bottom: 1px solid #e0e5e1; }
.daily-run-state, .daily-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 9px; }
.daily-run-state span { padding: 6px 9px; border-radius: 7px; background: #edf2ee; color: #52635a; font-size: 11px; }
.daily-run-state span.morning { border-left: 3px solid #3c8abb; }
.daily-run-state span.evening { border-left: 3px solid #d46b32; }
.daily-actions button, .daily-actions a, .row-actions button, .daily-filters button { border: 1px solid #d4ddd6; border-radius: 7px; background: #fff; color: #315245; padding: 7px 10px; cursor: pointer; font-size: 11px; text-decoration: none; }
.daily-actions button:disabled { cursor: not-allowed; opacity: .48; }
.export-blocker { display: flex; gap: 12px; padding: 11px 20px; background: #fff1ee; color: #9c3d2d; font-size: 12px; }
.daily-filters { display: flex; gap: 7px; padding: 12px 20px; border-bottom: 1px solid #e0e5e1; }
.daily-filters input { flex: 1; min-width: 220px; padding: 9px 11px; border: 1px solid #d4ddd6; border-radius: 8px; background: #f5f8f5; }
.daily-filters button.active { border-color: #1e5d43; background: #1e5d43; color: white; }
.daily-table-wrap { overflow: auto; max-height: 62vh; }
.daily-table { width: 100%; min-width: 1220px; border-collapse: separate; border-spacing: 0; font-size: 12px; }
.daily-table th { position: sticky; top: 0; z-index: 3; padding: 11px 10px; border-bottom: 1px solid #cfd9d2; background: #edf2ee; color: #4b5b53; text-align: center; }
.daily-table td { padding: 11px 10px; border-bottom: 1px solid #e5e9e6; background: rgba(255,255,255,.72); text-align: center; vertical-align: middle; }
.daily-table tbody tr:hover td { background: #f4f8f4; }
.daily-table td.sales-hit { background: #fce4d6; color: #8c3d16; }
.product-cell { width: 280px; text-align: left !important; }
.product-cell strong, .product-cell code, .product-cell small, .product-cell span { display: block; }
.product-cell strong { max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.product-cell code { margin: 4px 0; color: #1e5d43; font-size: 11px; }
.product-cell small { color: #7b8780; }
.product-cell span { margin-top: 5px; color: #a14d2b; font-size: 10px; }
.daily-table td > small { display: block; margin-top: 5px; color: #8a6250; }
.daily-table td > em { display: block; width: fit-content; margin: 5px auto 0; padding: 3px 6px; border-radius: 5px; background: #ffe8ca; color: #92560e; font-size: 9px; font-style: normal; }
.daily-table td.stock-value-mismatch { background: #ffc7ce !important; color: #8d1f28; }
.stock-check span, .stock-check strong, .stock-check small { display: block; }
.stock-check.mismatch { background: #ffc7ce !important; color: #8d1f28; }
.stock-check.dismissed { background: #f2f3ef !important; color: #6f766d; text-decoration: line-through; }
.row-actions { width: 190px; }
.row-actions button { margin: 3px; padding: 5px 7px; }
.row-actions button.danger-link { border-color: #e3a294; color: #ad442f; }
.status-badge { display: block; width: fit-content; margin: 0 auto 5px; padding: 4px 7px; border-radius: 999px; background: #e8ece9; color: #536159; font-size: 10px; }
.status-badge.needs_review { background: #ffe2dc; color: #a63d2d; }
.status-badge.ready { background: #fff1d6; color: #8a5a0d; }
.status-badge.confirmed { background: #dff2e7; color: #236446; }
.empty-row { padding: 50px !important; color: #7a8880; }
.daily-modal-backdrop { position: fixed; inset: 0; z-index: 50; display: grid; place-items: center; padding: 20px; background: rgba(13, 35, 26, .48); backdrop-filter: blur(3px); }
.daily-modal { width: min(590px, 100%); max-height: calc(100vh - 40px); overflow: auto; padding: 24px; border-radius: 17px; background: #f9fbf8; box-shadow: 0 30px 80px rgba(7, 31, 20, .25); }
.daily-modal h3 { margin: 0 0 7px; }
.modal-product { color: #6e7c74; font-size: 12px; }
.daily-modal label { display: grid; gap: 6px; margin-top: 13px; color: #4e5f56; font-size: 11px; }
.daily-modal input, .daily-modal select, .daily-modal textarea { width: 100%; padding: 9px 10px; border: 1px solid #d1dad4; border-radius: 8px; background: white; }
.daily-modal textarea { min-height: 100px; font-family: inherit; line-height: 1.5; }
.manual-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0 12px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 9px; margin-top: 18px; }
.modal-actions button { padding: 9px 15px; border: 1px solid #d1dad4; border-radius: 8px; background: white; cursor: pointer; }
@media (max-width: 1000px) {
  .daily-kpis { grid-template-columns: repeat(3, 1fr); }
  .daily-toolbar, .daily-run-times { align-items: flex-start; flex-direction: column; }
}
@media (max-width: 640px) {
  .daily-kpis { grid-template-columns: repeat(2, 1fr); }
  .daily-filters { flex-wrap: wrap; }
  .daily-filters input { flex-basis: 100%; }
  .manual-grid { grid-template-columns: 1fr; }
}
</style>
