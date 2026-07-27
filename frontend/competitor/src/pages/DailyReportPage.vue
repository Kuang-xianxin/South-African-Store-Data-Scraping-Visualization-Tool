<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";

import {
  confirmDailyReportEntry,
  deleteDailyReportNote,
  dismissDailyReportStockAlert,
  fetchDailyReport,
  fetchDailyReportExport,
  generateDailyReportExport,
  revertDailyReportConfirmation,
  saveDailyReportManual,
  saveDailyReportNote,
  updateDailyReportNote,
} from "../api";
import { formatChinaDateTime } from "../time";
import type {
  DailyReportExport,
  DailyReportItem,
  DailyReportPendingAction,
  DailyReportPayload,
} from "../types";

type MatrixDailyReportItem =
  DailyReportPayload["comparison_history"][number]["items"][number];
type EditableDailyReportItem =
  | DailyReportPendingAction
  | (MatrixDailyReportItem & { business_date: string });
type DailyReportNote = DailyReportItem["operator_notes"][number];

const props = defineProps<{ asOf: string; canOperate?: boolean }>();
const report = ref<DailyReportPayload | null>(null);
const exportState = ref<DailyReportExport | null>(null);
const loading = ref(false);
const saving = ref(false);
const message = ref("");
const search = ref("");
const filter = ref<"review" | "all" | "sales" | "stock" | "missing">("all");
const page = ref(1);
const pageSize = 24;
const slots = ["morning", "evening"] as const;
const matrixScroll = ref<HTMLElement | null>(null);
const editor = ref<{
  mode:
    | "manual"
    | "confirm"
    | "revert"
    | "note"
    | "edit_note"
    | "dismiss"
    | null;
  item: EditableDailyReportItem | null;
  note: DailyReportNote | null;
}>({ mode: null, item: null, note: null });
const noteManager = ref<EditableDailyReportItem | null>(null);
const form = ref({
  page_views_30_days: "",
  ordered_units: "",
  platform_stock: "",
  reason: "platform_delay",
  source: "latest" as "morning" | "evening" | "latest" | "manual",
  note_issue: "general" as
    | "general"
    | "capture_difference"
    | "stock_continuity",
  note: "",
});

watch(() => props.asOf, load, { immediate: true });

const comparisonHistory = computed(() => report.value?.comparison_history ?? []);
const manualRuns = computed(
  () => report.value?.runs.filter((run) => run.slot === "manual") ?? [],
);
const successfulManualRuns = computed(
  () => manualRuns.value.filter((run) => run.status === "success"),
);
const latestManualRun = computed(
  () => manualRuns.value[manualRuns.value.length - 1] ?? null,
);
const matrixBaseItems = computed(() => {
  const itemsByOffer = new Map<
    string,
    DailyReportPayload["comparison_history"][number]["items"][number]
  >();
  for (const day of comparisonHistory.value) {
    for (const item of day.items) itemsByOffer.set(item.offer_id, item);
  }
  for (const item of report.value?.items ?? []) {
    itemsByOffer.set(item.offer_id, item);
  }
  return [...itemsByOffer.values()];
});
const comparisonLookup = computed(() => {
  const result = new Map<
    string,
    Map<string, DailyReportPayload["comparison_history"][number]["items"][number]>
  >();
  for (const day of comparisonHistory.value) {
    result.set(
      day.business_date,
      new Map(day.items.map((item) => [item.offer_id, item])),
    );
  }
  return result;
});
const filteredItems = computed(() => {
  const term = search.value.trim().toLowerCase();
  return matrixBaseItems.value.filter((item) => {
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
    if (filter.value === "missing") return item.missing_capture;
    if (filter.value === "review") {
      return item.status === "needs_review";
    }
    return true;
  });
});
const pageCount = computed(() =>
  Math.max(1, Math.ceil(filteredItems.value.length / pageSize)),
);
const visibleItems = computed(() =>
  filteredItems.value.slice((page.value - 1) * pageSize, page.value * pageSize),
);
const actionItems = computed(() => report.value?.pending_actions ?? []);
const missingPageViewsCount = computed(
  () =>
    report.value?.items.filter((item) =>
      item.missing_fields.includes("page_views_30_days"),
    ).length ?? 0,
);

watch([search, filter], () => {
  page.value = 1;
});
watch(pageCount, (count) => {
  if (page.value > count) page.value = count;
});

async function load() {
  loading.value = true;
  message.value = "";
  try {
    [report.value, exportState.value] = await Promise.all([
      fetchDailyReport(props.asOf),
      fetchDailyReportExport(props.asOf),
    ]);
    await nextTick();
    if (matrixScroll.value) {
      matrixScroll.value.scrollTop = matrixScroll.value.scrollHeight;
    }
  } catch (error) {
    message.value = error instanceof Error ? error.message : "运营日报读取失败";
  } finally {
    loading.value = false;
  }
}

function openEditor(
  mode: "manual" | "confirm" | "revert" | "note" | "edit_note" | "dismiss",
  item: EditableDailyReportItem | null = null,
  note: DailyReportNote | null = null,
) {
  editor.value = { mode, item, note };
  const fullItem = item && "manual" in item ? item : null;
  const current = mode === "manual" && fullItem?.manual_at && fullItem.manual
    ? fullItem.manual
    : item?.current;
  const pendingItem = item && "review_issues" in item ? item : null;
  form.value = {
    page_views_30_days: toInput(current?.page_views_30_days),
    ordered_units: toInput(current?.ordered_units),
    platform_stock: toInput(current?.platform_stock),
    reason: mode === "manual" && fullItem?.manual_reason
      ? fullItem.manual_reason
      : "platform_delay",
    source: pendingItem?.manual_note
      ? "manual"
      : pendingItem?.capture_versions.length
        ? "latest"
        : pendingItem?.evening
          ? "evening"
          : "morning",
    note_issue: note?.issue_type ?? (
      pendingItem && hasReviewIssue(pendingItem, "capture_difference")
        ? "capture_difference"
        : pendingItem && hasReviewIssue(pendingItem, "stock_continuity")
          ? "stock_continuity"
          : "general"
    ),
    note: note?.note ?? (
      mode === "manual" && fullItem?.manual_note ? fullItem.manual_note : ""
    ),
  };
  noteManager.value = null;
}

function closeEditor() {
  editor.value = { mode: null, item: null, note: null };
}

async function submitEditor() {
  if (!props.canOperate || !editor.value.mode) return;
  const mode = editor.value.mode;
  const item = editor.value.item;
  saving.value = true;
  message.value = "";
  try {
    if (mode === "manual" && item) {
      await saveDailyReportManual(item.business_date, item.offer_id, {
        page_views_30_days: parseInput(form.value.page_views_30_days),
        ordered_units: parseInput(form.value.ordered_units),
        platform_stock: parseInput(form.value.platform_stock),
        reason: form.value.reason,
        note: form.value.note,
      });
      message.value = "人工候选值已更新并标记，仍需最终确认；历次修改均已留痕。";
    } else if (mode === "confirm" && item) {
      const result = await confirmDailyReportEntry(
        item.business_date,
        item.offer_id,
        form.value.source,
        form.value.note,
      );
      message.value = result.exported
        ? "数据已确认；当天全部完成，Excel 已自动导出。"
        : "该商品已确认合并。";
    } else if (mode === "revert" && item) {
      await revertDailyReportConfirmation(
        item.business_date,
        item.offer_id,
        form.value.note,
      );
      message.value = "已撤销确认并恢复待核对；原确认和撤销原因均已留痕。";
    } else if (mode === "note" && item) {
      await saveDailyReportNote(
        item.business_date,
        item.offer_id,
        form.value.note,
        form.value.note_issue,
      );
      message.value = "备注已新增，不会改变待办状态。";
    } else if (mode === "edit_note" && item && editor.value.note) {
      await updateDailyReportNote(
        item.business_date,
        item.offer_id,
        editor.value.note.id,
        form.value.note,
        form.value.note_issue,
      );
      message.value = "备注已修改，原内容与修改记录已保留在审计中。";
    } else if (mode === "dismiss" && item) {
      await dismissDailyReportStockAlert(
        item.business_date,
        item.offer_id,
        form.value.note,
      );
      message.value = "库存连续性差异已人工确认，原因已留痕。";
    }
    closeEditor();
    await load();
  } catch (error) {
    message.value = error instanceof Error ? error.message : "保存失败";
  } finally {
    saving.value = false;
  }
}

function openNoteManager(item: EditableDailyReportItem) {
  noteManager.value = item;
}

function closeNoteManager() {
  noteManager.value = null;
}

async function removeNote(item: EditableDailyReportItem, note: DailyReportNote) {
  if (!props.canOperate || saving.value) return;
  if (!window.confirm(`确定删除备注“${note.note}”吗？删除操作会保留审计记录。`)) {
    return;
  }
  saving.value = true;
  message.value = "";
  try {
    await deleteDailyReportNote(item.business_date, item.offer_id, note.id);
    message.value = "备注已删除，删除操作已留痕。";
    closeNoteManager();
    await load();
  } catch (error) {
    message.value = error instanceof Error ? error.message : "删除备注失败";
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

function reviewStatusLabel(item: DailyReportItem) {
  const captureIssue = item.review_issues.find(
    (issue) => issue.type === "capture_difference",
  );
  const hasStockContinuity = item.review_issues.some(
    (issue) => issue.type === "stock_continuity",
  );
  const labels: string[] = [];
  if (captureIssue) {
    const prefix = item.confirmation_baseline ? "确认后" : "";
    labels.push(`${prefix}销量版本有差异`);
  }
  if (hasStockContinuity) {
    labels.push(
      item.confirmation_trigger
        ? "人工确认后库存不平"
        : "前后日报库存不平",
    );
  }
  if (hasReviewIssue(item, "confirmation_reverted")) {
    labels.push("已撤销确认待重核");
  }
  return labels.join("；") || "待人工核对";
}

function statusLabel(item: DailyReportItem) {
  if (item.status === "needs_review") return reviewStatusLabel(item);
  return {
    awaiting_evening: "等待下一次拉取",
    ready: "无差异已自动采用",
    missing_capture: "漏爬已自动补缺",
    confirmed: "已确认",
  }[item.status];
}

function reviewStatusClass(item: DailyReportItem) {
  if (item.status !== "needs_review") return "";
  const hasCapture = hasReviewIssue(item, "capture_difference");
  const hasStock = hasReviewIssue(item, "stock_continuity");
  const hasRevert = hasReviewIssue(item, "confirmation_reverted");
  if (hasCapture && hasStock) return "mixed-review";
  if (hasStock) return "stock-review";
  return hasRevert ? "revert-review" : "version-review";
}

function captureStatusLabel(slot: "morning" | "evening") {
  const state = report.value?.capture_status[slot];
  if (!state) return "未记录";
  return {
    success: `已采集 ${state.product_count} 个商品`,
    failed: "采集失败",
    missing: "未生成记录",
    pending: "等待计划时间",
  }[state.status];
}

function slotLabel(slot: "morning" | "evening") {
  return slot === "morning" ? "10:05早间" : "18:00晚间";
}

function productLabel(item: { sku: string | null; offer_id: string }) {
  return item.sku || item.offer_id;
}

function notesForIssue(
  item: { operator_notes: DailyReportNote[] },
  issueType: "general" | "capture_difference" | "stock_continuity",
) {
  return item.operator_notes.filter((note) => note.issue_type === issueType);
}

function hasReviewIssue(
  item: DailyReportItem,
  issueType:
    | "capture_difference"
    | "stock_continuity"
    | "confirmation_reverted",
) {
  return item.review_issues.some((issue) => issue.type === issueType);
}

function comparisonItem(businessDate: string, offerId: string) {
  return comparisonLookup.value.get(businessDate)?.get(offerId);
}

function editableComparisonItem(
  businessDate: string,
  offerId: string,
): EditableDailyReportItem | null {
  const item = comparisonItem(businessDate, offerId);
  return item ? { ...item, business_date: businessDate } : null;
}

function noteIssueLabel(issueType: DailyReportNote["issue_type"]) {
  return {
    general: "通用",
    capture_difference: "版本",
    stock_continuity: "库存",
  }[issueType];
}

function matrixNoteText(item: MatrixDailyReportItem | undefined) {
  if (!item) return "";
  const notes: string[] = [];
  const confirmationNote = item.confirmation_baseline?.confirm_note?.trim();
  if (confirmationNote) notes.push(`（确认：${confirmationNote}）`);
  if (item.operator_notes.length) {
    notes.push(
      ...item.operator_notes
      .map((note) => `（${noteIssueLabel(note.issue_type)}：${note.note}）`)
    );
  } else if (item.operator_note) {
    notes.push(`（${item.operator_note}）`);
  }
  return notes.join(" ");
}

function fieldLabel(key: string) {
  return {
    page_views_30_days: "近30天浏览量",
    ordered_units: "订单",
    platform_stock: "库存",
  }[key] ?? key;
}

function stockContinuityText(item: DailyReportItem) {
  const check = item.stock_check;
  if (!check.mismatch || check.dismissed) return "";
  return `库存连续性：${value(check.previous_stock)} − ${value(item.current.ordered_units)} = ${value(check.expected_stock)}，实际 ${value(check.actual_stock)}`;
}

function comparisonBeforeLabel(
  state: "matched" | "mismatch" | "unavailable" | undefined,
) {
  if (!state) return "历史记录未保存当时状态";
  return {
    matched: "当时相符",
    mismatch: "当时按临时值也不相符",
    unavailable: "当时因版本未定暂未计算",
  }[state];
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
        <h2>每日10:00至次日10:00，自动核对全部定时与手动拉取</h2>
      </div>
      <div class="daily-run-times">
        <span>10:00 周期开始</span><i>→</i><span>10:05 / 18:00 定时</span><i>→</i><span>期间每次手动刷新</span>
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
      <article class="sales"><small>日报日有销量</small><strong>{{ report?.counts.with_sales ?? 0 }}</strong></article>
      <article class="review"><small>人工核对待办</small><strong>{{ actionItems.length }}</strong></article>
      <article class="missing">
        <small>暂缺近30天浏览量</small>
        <strong>{{ missingPageViewsCount }}<span> 个商品</span></strong>
      </article>
      <article class="danger"><small>库存不平</small><strong>{{ report?.counts.stock_alerts ?? 0 }}</strong></article>
      <article class="confirmed"><small>已确认</small><strong>{{ report?.counts.confirmed ?? 0 }}</strong></article>
    </section>

    <section v-if="report?.capture_issues.length" class="capture-notice">
      <div class="capture-notice-title">
        <strong>数据完整性说明：{{ report.capture_issues.length }} 条</strong>
        <span>漏爬不算冲突，系统按本周期最新非空版本自动补缺</span>
      </div>
      <details>
        <summary>查看具体漏爬原因</summary>
        <ul>
          <li v-for="(issue, index) in report.capture_issues.slice(0, 8)" :key="`${issue.offer_id || issue.slot}-${index}`">
            <b>{{ issue.slot === "morning" ? "早间" : issue.slot === "evening" ? "晚间" : issue.slot === "manual" ? "手动" : "字段" }}</b>
            <span v-if="issue.sku">{{ issue.sku }}：</span>
            {{ issue.reason }}
          </li>
        </ul>
        <small v-if="report.capture_issues.length > 8">
          另有 {{ report.capture_issues.length - 8 }} 条商品级说明，完整内容已写入 Excel 的“漏爬说明”工作表。
        </small>
      </details>
    </section>

    <section class="erp-panel daily-workspace">
      <div class="daily-toolbar">
        <div class="daily-run-state">
          <span
            v-for="slot in slots"
            :key="slot"
            :class="[slot, report?.capture_status[slot].status]"
            :title="report?.capture_status[slot].reason || ''"
          >
            {{ slot === "morning" ? "10:05 早间" : "18:00 晚间" }}：
            {{ captureStatusLabel(slot) }}
            <small v-if="report?.capture_status[slot].captured_at">
              {{ formatChinaDateTime(report.capture_status[slot].captured_at, "—") }}
            </small>
            <em v-if="report?.capture_status[slot].recovered">
              第{{ report.capture_status[slot].attempt_count }}次恢复
            </em>
          </span>
          <span
            class="manual"
            :class="{ failed: manualRuns.length > successfulManualRuns.length }"
            :title="latestManualRun?.status === 'failed' ? String(latestManualRun.counts.missing_reason || '') : ''"
          >
            当前周期手动刷新：已核对 {{ successfulManualRuns.length }} 次
            <small v-if="latestManualRun">
              最近 {{ formatChinaDateTime(latestManualRun.captured_at, "—") }}
            </small>
            <em v-if="manualRuns.length > successfulManualRuns.length">
              {{ manualRuns.length - successfulManualRuns.length }} 次失败已留日志
            </em>
          </span>
        </div>
        <div class="daily-actions">
          <button
            class="action-button"
            :disabled="!props.canOperate || saving || !matrixBaseItems.length"
            @click="runExport"
          >
            {{ exportState?.blocked ? `尚有 ${exportState.unresolved.length} 处未合并` : "导出 Excel" }}
          </button>
          <a v-if="exportState?.download_url" :href="exportState.download_url">下载已导出版本</a>
        </div>
      </div>

      <details
        v-if="slots.some((slot) => report?.capture_status[slot].attempts.length)"
        class="capture-attempt-log"
      >
        <summary>查看采集保护与重试记录</summary>
        <div v-for="slot in slots" :key="slot">
          <template v-if="report?.capture_status[slot].attempts.length">
            <strong>{{ slotLabel(slot) }}</strong>
            <span
              v-for="attempt in report.capture_status[slot].attempts"
              :key="`${slot}-${attempt.attempt}`"
              :class="attempt.status"
            >
              第{{ attempt.attempt }}次 · {{ attempt.strategy }} ·
              {{ attempt.status === "success" ? "成功" : "失败" }} ·
              {{ attempt.reason }}
            </span>
          </template>
        </div>
      </details>

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
        <button :class="{ active: filter === 'missing' }" @click="filter = 'missing'">漏爬说明</button>
        <button :class="{ active: filter === 'sales' }" @click="filter = 'sales'">有销量</button>
        <button :class="{ active: filter === 'stock' }" @click="filter = 'stock'">库存不平</button>
        <button :class="{ active: filter === 'all' }" @click="filter = 'all'">全部</button>
      </div>

      <div class="matrix-meta">
        <div>
          <strong>最近 {{ comparisonHistory.length }} 个有数据业务日</strong>
          <span>一次完整显示5日；上下滑动查看更多日期，打开时定位到最新日期。</span>
          <div class="matrix-legend">
            <i class="date"></i>日期起始
            <i class="sales"></i>当天有订单
            <i class="stock"></i>库存公式不平
            <i class="missing"></i>接口未提供
          </div>
        </div>
        <div class="matrix-pages">
          <button :disabled="page <= 1" @click="page -= 1">上一组</button>
          <span>{{ page }} / {{ pageCount }}（共 {{ filteredItems.length }} 个商品）</span>
          <button :disabled="page >= pageCount" @click="page += 1">下一组</button>
        </div>
      </div>
      <div ref="matrixScroll" class="daily-matrix-wrap">
        <table class="daily-matrix">
          <thead>
            <tr>
              <th class="metric-head">指标</th>
              <th class="date-head">日期</th>
              <th
                v-for="item in visibleItems"
                :key="item.offer_id"
                class="matrix-product"
                :class="{
                  'has-conflict': item.status === 'needs_review',
                  'has-missing': item.missing_capture,
                }"
                :title="`${item.title}\n${productLabel(item)}${item.missing_reason ? `\n${item.missing_reason}` : ''}`"
              >
                <strong>{{ item.title }}</strong>
                <code>{{ productLabel(item) }}</code>
              </th>
            </tr>
          </thead>
          <tbody>
            <template v-for="day in comparisonHistory" :key="day.business_date">
              <tr class="visitor-total date-start">
                <th>近30天浏览量</th>
                <td class="matrix-date">{{ day.business_date }}</td>
                <td
                  v-for="item in visibleItems"
                  :key="item.offer_id"
                  :class="{ 'missing-value': comparisonItem(day.business_date, item.offer_id)?.current.page_views_30_days == null }"
                  :title="comparisonItem(day.business_date, item.offer_id)?.missing_reason || ''"
                >
                  {{ value(comparisonItem(day.business_date, item.offer_id)?.current.page_views_30_days) }}
                </td>
              </tr>
              <tr>
                <th>当天订单数</th>
                <td></td>
                <td
                  v-for="item in visibleItems"
                  :key="item.offer_id"
                  :class="{ 'sales-hit': Number(comparisonItem(day.business_date, item.offer_id)?.current.ordered_units || 0) > 0 }"
                >
                  {{ value(comparisonItem(day.business_date, item.offer_id)?.current.ordered_units) }}
                </td>
              </tr>
              <tr>
                <th>平台库存数量（当日10:05）</th>
                <td></td>
                <td
                  v-for="item in visibleItems"
                  :key="item.offer_id"
                  :class="{
                    'stock-value-mismatch':
                      comparisonItem(day.business_date, item.offer_id)?.stock_check.mismatch &&
                      !comparisonItem(day.business_date, item.offer_id)?.stock_check.dismissed,
                    'missing-value': comparisonItem(day.business_date, item.offer_id)?.current.platform_stock == null,
                  }"
                  :title="
                    comparisonItem(day.business_date, item.offer_id)?.missing_reason ||
                    comparisonItem(day.business_date, item.offer_id)?.stock_check.note ||
                    ''
                  "
                >
                  {{ value(comparisonItem(day.business_date, item.offer_id)?.current.platform_stock) }}
                </td>
              </tr>
              <tr class="date-end matrix-note-row">
                <th>备注</th>
                <td></td>
                <td
                  v-for="item in visibleItems"
                  :key="item.offer_id"
                  class="matrix-note-cell"
                  :title="matrixNoteText(comparisonItem(day.business_date, item.offer_id))"
                >
                  <button
                    v-if="
                      props.canOperate &&
                      editableComparisonItem(day.business_date, item.offer_id)
                    "
                    type="button"
                    class="matrix-note-manager"
                    :class="{
                      'has-confirmation': comparisonItem(
                        day.business_date,
                        item.offer_id,
                      )?.confirmation_baseline,
                    }"
                    @click="
                      openNoteManager(
                        editableComparisonItem(day.business_date, item.offer_id)!,
                      )
                    "
                  >
                    <span v-if="matrixNoteText(comparisonItem(day.business_date, item.offer_id))">
                      {{ matrixNoteText(comparisonItem(day.business_date, item.offer_id)) }}
                    </span>
                    <span
                      v-else-if="
                        comparisonItem(day.business_date, item.offer_id)
                          ?.confirmation_baseline
                      "
                    >
                      已确认 · 管理或撤销
                    </span>
                    <span v-else class="empty-note">＋ 添加备注</span>
                  </button>
                  <span v-else-if="matrixNoteText(comparisonItem(day.business_date, item.offer_id))">
                    {{ matrixNoteText(comparisonItem(day.business_date, item.offer_id)) }}
                  </span>
                  <span v-else>—</span>
                </td>
              </tr>
            </template>
            <tr v-if="!loading && !visibleItems.length">
              <td colspan="3" class="empty-row">当前筛选没有商品。</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="erp-panel review-panel">
      <div class="review-title">
        <div>
          <p class="section-kicker">全部未处理核对</p>
          <h3>人工核对待办</h3>
        </div>
        <span>
          包含同一日报日内多次拉取差异和前后日报日库存连续性差异；每张卡只显示该日期所属周期的版本，顶部手动次数只统计当前周期
        </span>
      </div>
      <div v-if="actionItems.length" class="review-list">
        <article
          v-for="item in actionItems"
          :key="`${item.business_date}-${item.offer_id}`"
        >
          <div class="review-product">
            <strong>{{ item.title }}</strong>
            <code>{{ productLabel(item) }}</code>
            <small>{{ item.business_date }}</small>
            <span
              class="status-badge"
              :class="[item.status, reviewStatusClass(item)]"
            >
              {{ statusLabel(item) }}
            </span>
          </div>
          <div class="review-issues">
            <section
              v-if="hasReviewIssue(item, 'capture_difference')"
              class="review-issue capture-issue"
            >
              <header>
                <strong>同周期销量版本差异</strong>
                <span>只比较早间、晚间和全部手动拉取的当天销量，库存固定采用业务日10:05快照</span>
              </header>
              <div
                v-for="field in item.differences"
                :key="field"
                class="issue-field-values"
              >
                <b>{{ fieldLabel(field) }}</b>
                <span
                  v-for="(version, versionIndex) in item.review_versions"
                  :key="`${field}-${version.kind}-${version.run_id || versionIndex}`"
                  :class="{ 'confirmed-version': version.kind === 'confirmed' }"
                >
                  {{ version.label }}：{{ value(version.values[field]) }}
                  <small v-if="version.kind === 'confirmed'">
                    {{ version.source_label }} · {{ version.user_name }} · 北京时间
                    {{ formatChinaDateTime(version.captured_at, "—") }}
                  </small>
                </span>
              </div>
              <div v-if="item.manual_note" class="issue-note">
                人工修改备注：{{ item.manual_note }}
              </div>
              <div
                v-for="note in notesForIssue(item, 'capture_difference')"
                :key="note.id"
                class="issue-note"
              >
                <span>
                  {{ note.user_name }} · 北京时间
                  {{ formatChinaDateTime(note.created_at, "—") }}：
                  {{ note.note }}
                  <small v-if="note.updated_at">
                    （{{ note.updated_by || "未知操作人" }} 于北京时间
                    {{ formatChinaDateTime(note.updated_at, "—") }}修改）
                  </small>
                </span>
                <span v-if="props.canOperate" class="note-actions">
                  <button type="button" @click="openEditor('edit_note', item, note)">修改</button>
                  <button type="button" class="danger-link" @click="removeNote(item, note)">删除</button>
                </span>
              </div>
            </section>

            <section
              v-if="hasReviewIssue(item, 'stock_continuity')"
              class="review-issue stock-issue"
            >
              <header>
                <strong>前后日报日库存连续性</strong>
                <span>这里只比较前一日报日与当前日报日</span>
              </header>
              <div class="stock-formula-data">
                <span>
                  前一日报日：{{ item.stock_context?.business_date || "—" }}，
                  库存 {{ value(item.stock_check.previous_stock) }}
                </span>
                <small>
                  {{ item.stock_context?.source_label || "没有可用的前一日报日库存" }}
                  <template v-if="item.stock_context?.capture_label">
                    · {{ item.stock_context.capture_label }}
                  </template>
                </small>
                <small v-if="item.stock_context?.confirmed_by">
                  前一日报日确认：{{ item.stock_context.confirmed_by }} · 北京时间
                  {{ formatChinaDateTime(item.stock_context.confirmed_at, "—") }}
                  <template v-if="item.stock_context.confirm_note">
                    · {{ item.stock_context.confirm_note }}
                  </template>
                </small>
                <span>
                  当前日报日：{{ item.business_date }}，订单
                  {{ value(item.current.ordered_units) }}，应有库存
                  {{ value(item.stock_check.expected_stock) }}，实际库存
                  {{ value(item.stock_check.actual_stock) }}
                </span>
                <strong>{{ stockContinuityText(item) }}</strong>
              </div>
              <div v-if="item.confirmation_trigger" class="propagated-conflict">
                <strong>{{ item.confirmation_trigger.message }}</strong>
                <span>
                  前一日确认：{{ item.confirmation_trigger.confirmation_source_label }} /
                  {{ item.confirmation_trigger.confirmed_by || "未知操作人" }} /
                  {{ formatChinaDateTime(item.confirmation_trigger.confirmed_at, "—") }}
                </span>
                <span>确认备注：{{ item.confirmation_trigger.confirmation_note }}</span>
                <span>
                  确认前：{{ value(item.confirmation_trigger.previous_stock_before_confirmation) }}
                  − {{ value(item.confirmation_trigger.current_ordered_units) }}
                  = {{ value(item.confirmation_trigger.expected_stock_before_confirmation) }}，
                  实际 {{ value(item.confirmation_trigger.actual_stock) }}（{{
                    comparisonBeforeLabel(item.confirmation_trigger.comparison_before_state)
                  }}）
                </span>
                <span>
                  确认后：{{ value(item.confirmation_trigger.confirmed_previous_stock) }}
                  − {{ value(item.confirmation_trigger.current_ordered_units) }}
                  = {{ value(item.confirmation_trigger.expected_stock_after_confirmation) }}，
                  实际 {{ value(item.confirmation_trigger.actual_stock) }}（产生冲突）
                </span>
                <span v-if="item.confirmation_trigger.affected_previous_final">
                  本待办此前确认值：订单
                  {{ value(item.confirmation_trigger.affected_previous_final.ordered_units) }} /
                  库存 {{ value(item.confirmation_trigger.affected_previous_final.platform_stock) }}；
                  {{ item.confirmation_trigger.affected_previous_confirmed_by || "未知操作人" }}
                  于
                  {{ formatChinaDateTime(item.confirmation_trigger.affected_previous_confirmed_at, "—") }}
                  确认，备注：{{ item.confirmation_trigger.affected_previous_confirm_note || "—" }}
                </span>
              </div>
              <div
                v-for="note in notesForIssue(item, 'stock_continuity')"
                :key="note.id"
                class="issue-note"
              >
                <span>
                  {{ note.user_name }} · 北京时间
                  {{ formatChinaDateTime(note.created_at, "—") }}：
                  {{ note.note }}
                  <small v-if="note.updated_at">
                    （{{ note.updated_by || "未知操作人" }} 于北京时间
                    {{ formatChinaDateTime(note.updated_at, "—") }}修改）
                  </small>
                </span>
                <span v-if="props.canOperate" class="note-actions">
                  <button type="button" @click="openEditor('edit_note', item, note)">修改</button>
                  <button type="button" class="danger-link" @click="removeNote(item, note)">删除</button>
                </span>
              </div>
            </section>

            <section
              v-if="hasReviewIssue(item, 'confirmation_reverted') && item.confirmation_revert"
              class="review-issue revert-issue"
            >
              <header>
                <strong>确认合并已撤销，等待重新核对</strong>
                <span>原确认没有删除，撤销原因和操作人均已留痕</span>
              </header>
              <div class="revert-context">
                <span>
                  原确认：{{ item.confirmation_revert.previous_confirmation.source_label }}，
                  订单 {{ value(item.confirmation_revert.previous_confirmation.values.ordered_units) }} /
                  库存 {{ value(item.confirmation_revert.previous_confirmation.values.platform_stock) }}
                </span>
                <small>
                  {{ item.confirmation_revert.previous_confirmation.confirmed_by || "未知操作人" }}
                  · 北京时间
                  {{ formatChinaDateTime(item.confirmation_revert.previous_confirmation.confirmed_at, "—") }}
                  · {{ item.confirmation_revert.previous_confirmation.confirm_note || "无确认备注" }}
                </small>
                <strong>
                  {{ item.confirmation_revert.reverted_by || "未知操作人" }} 于北京时间
                  {{ formatChinaDateTime(item.confirmation_revert.reverted_at, "—") }}撤销：
                  {{ item.confirmation_revert.revert_note }}
                </strong>
              </div>
            </section>

            <section
              v-if="
                notesForIssue(item, 'general').length ||
                (item.operator_note && !item.operator_notes.length)
              "
              class="review-issue note-issue"
            >
              <header><strong>独立备注记录</strong></header>
              <div
                v-for="note in notesForIssue(item, 'general')"
                :key="note.id"
                class="issue-note"
              >
                <span>
                  {{ note.user_name }} · 北京时间
                  {{ formatChinaDateTime(note.created_at, "—") }}：
                  {{ note.note }}
                  <small v-if="note.updated_at">
                    （{{ note.updated_by || "未知操作人" }} 于北京时间
                    {{ formatChinaDateTime(note.updated_at, "—") }}修改）
                  </small>
                </span>
                <span v-if="props.canOperate" class="note-actions">
                  <button type="button" @click="openEditor('edit_note', item, note)">修改</button>
                  <button type="button" class="danger-link" @click="removeNote(item, note)">删除</button>
                </span>
              </div>
              <div v-if="item.operator_note && !item.operator_notes.length" class="issue-note">
                历史备注：{{ item.operator_note }}
              </div>
            </section>
          </div>
          <div class="row-actions">
            <button v-if="props.canOperate" @click="openEditor('manual', item)">人工修改</button>
            <button
              v-if="
                props.canOperate &&
                (
                  hasReviewIssue(item, 'capture_difference') ||
                  hasReviewIssue(item, 'confirmation_reverted')
                )
              "
              @click="openEditor('confirm', item)"
            >
              确认合并
            </button>
            <button v-if="props.canOperate" @click="openEditor('note', item)">单独加备注</button>
            <button
              v-if="props.canOperate && hasReviewIssue(item, 'stock_continuity')"
              class="danger-link"
              @click="openEditor('dismiss', item)"
            >
              确认库存差异
            </button>
            <button
              v-if="props.canOperate && item.confirmation_baseline"
              class="danger-link"
              @click="openEditor('revert', item)"
            >
              撤销上次确认
            </button>
          </div>
        </article>
      </div>
      <div v-else class="review-empty">
        截至 {{ props.asOf }} 没有未处理的采集版本差异或库存连续性差异。
      </div>
    </section>

    <div
      v-if="noteManager"
      class="daily-modal-backdrop"
      @click.self="closeNoteManager"
    >
      <section class="daily-modal note-manager-modal">
        <p class="section-kicker">DATE NOTE</p>
        <h3>{{ noteManager.business_date }} 备注与确认记录</h3>
        <p class="modal-product">
          {{ noteManager.title }} · {{ noteManager.sku || noteManager.offer_id }}
        </p>
        <section
          v-if="noteManager.confirmation_baseline"
          class="confirmation-manager"
        >
          <div>
            <strong>当前人工确认</strong>
            <p>
              {{ noteManager.confirmation_baseline.source_label }} ·
              订单 {{ value(noteManager.confirmation_baseline.values.ordered_units) }} /
              库存 {{ value(noteManager.confirmation_baseline.values.platform_stock) }}
            </p>
            <small>
              {{ noteManager.confirmation_baseline.confirmed_by }} · 北京时间
              {{ formatChinaDateTime(noteManager.confirmation_baseline.confirmed_at, "—") }}
              · {{ noteManager.confirmation_baseline.confirm_note || "无确认备注" }}
            </small>
          </div>
          <button
            v-if="props.canOperate"
            type="button"
            class="danger-link"
            @click="openEditor('revert', noteManager)"
          >
            撤销确认合并
          </button>
        </section>
        <section
          v-else-if="noteManager.confirmation_revert"
          class="confirmation-manager reverted"
        >
          <div>
            <strong>最近一次确认已撤销</strong>
            <p>撤销原因：{{ noteManager.confirmation_revert.revert_note }}</p>
            <small>
              {{ noteManager.confirmation_revert.reverted_by || "未知操作人" }}
              · 北京时间
              {{ formatChinaDateTime(noteManager.confirmation_revert.reverted_at, "—") }}
            </small>
          </div>
        </section>
        <div v-if="noteManager.operator_notes.length" class="note-manager-list">
          <article v-for="note in noteManager.operator_notes" :key="note.id">
            <div>
              <strong>{{ noteIssueLabel(note.issue_type) }}备注</strong>
              <p>（{{ note.note }}）</p>
              <small>
                {{ note.user_name }} · 北京时间
                {{ formatChinaDateTime(note.created_at, "—") }}
                <template v-if="note.updated_at">
                  · {{ note.updated_by || "未知操作人" }} 于
                  {{ formatChinaDateTime(note.updated_at, "—") }}修改
                </template>
              </small>
            </div>
            <div v-if="props.canOperate" class="note-manager-actions">
              <button type="button" @click="openEditor('edit_note', noteManager, note)">
                修改
              </button>
              <button
                type="button"
                class="danger-link"
                @click="removeNote(noteManager, note)"
              >
                删除
              </button>
            </div>
          </article>
        </div>
        <p v-else class="review-empty">这个日期和商品还没有单独备注。</p>
        <div class="modal-actions">
          <button type="button" @click="closeNoteManager">关闭</button>
          <button
            v-if="props.canOperate"
            type="button"
            class="action-button"
            @click="openEditor('note', noteManager)"
          >
            新增备注
          </button>
        </div>
      </section>
    </div>

    <div v-if="editor.mode" class="daily-modal-backdrop" @click.self="closeEditor">
      <form class="daily-modal" @submit.prevent="submitEditor">
        <p class="section-kicker">OPERATOR ACTION</p>
        <h3>
          {{
            editor.mode === "manual"
              ? "修改人工候选值"
              : editor.mode === "confirm"
                ? "确认最终采用值"
                : editor.mode === "revert"
                  ? "撤销确认合并"
                : editor.mode === "dismiss"
                  ? "确认库存连续性差异"
                  : editor.mode === "edit_note"
                    ? "修改备注"
                    : "新增备注"
          }}
        </h3>
        <p v-if="editor.item" class="modal-product">
          {{ editor.item.business_date }} · {{ editor.item.title }} ·
          {{ editor.item.sku || editor.item.offer_id }}
        </p>
        <p v-if="editor.mode === 'revert'" class="revert-warning">
          撤销后会恢复为待核对；原确认记录不会删除，相邻日报日的库存连续性会在重新确认后重新计算。
        </p>
        <div v-if="editor.mode === 'manual'" class="manual-grid">
          <p class="manual-baseline-tip">
            输入框已带入当前数据；如已有人工候选，则带入上次修改值。下方基准值始终保留用于对照。
          </p>
          <label>
            <span>近30天浏览量<small>当前基准：{{ value(editor.item?.current.page_views_30_days) }}</small></span>
            <input v-model="form.page_views_30_days" type="number" min="0" />
          </label>
          <label>
            <span>当天订单数<small>当前基准：{{ value(editor.item?.current.ordered_units) }}</small></span>
            <input v-model="form.ordered_units" type="number" min="0" />
          </label>
          <label>
            <span>平台仓库存<small>当前基准：{{ value(editor.item?.current.platform_stock) }}</small></span>
            <input v-model="form.platform_stock" type="number" min="0" />
          </label>
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
            <option value="latest">本周期最新拉取值</option>
            <option value="morning">10:05 早间值</option>
            <option value="evening">18:00 晚间值</option>
            <option value="manual">人工候选值</option>
          </select>
        </label>
        <label v-if="editor.mode === 'note' || editor.mode === 'edit_note'">
          备注关联问题
          <select v-model="form.note_issue">
            <option value="general">整条待办的通用备注</option>
            <option value="capture_difference">同周期版本差异</option>
            <option value="stock_continuity">前后日报日库存连续性</option>
          </select>
        </label>
        <label>
          {{
            editor.mode === "confirm"
              ? "合并备注（必填）"
              : editor.mode === "revert"
                ? "撤销原因（必填）"
              : editor.mode === "note"
                ? "新增备注内容（必填）"
                : editor.mode === "edit_note"
                  ? "修改后的备注内容（必填）"
                  : "操作备注（必填）"
          }}
          <textarea v-model="form.note" required maxlength="2000"></textarea>
        </label>
        <div class="modal-actions">
          <button type="button" @click="closeEditor">取消</button>
          <button class="action-button" :disabled="saving || !form.note.trim()">
            {{
              saving
                ? "正在保存…"
                : editor.mode === "revert"
                  ? "确认撤销"
                  : "确认保存"
            }}
          </button>
        </div>
      </form>
    </div>
  </div>
</template>

<style scoped>
.daily-report-page {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  width: 100%;
  min-width: 0;
  gap: 18px;
}
.daily-report-page > * { width: 100%; min-width: 0; max-width: 100%; }
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
.daily-kpis .missing strong { color: #a06613; }
.daily-kpis .missing strong span { font-size: 11px; font-weight: 500; }
.daily-kpis .confirmed strong { color: #1e7954; }
.capture-notice { padding: 12px 16px; border: 1px solid #e0c27b; border-left: 5px solid #d6a42b; border-radius: 12px; background: #fff9e8; color: #6e5317; }
.capture-notice-title { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px 18px; }
.capture-notice-title span, .capture-notice small { color: #856f39; font-size: 11px; }
.capture-notice details { margin-top: 7px; }
.capture-notice summary { width: fit-content; cursor: pointer; color: #765716; font-size: 11px; font-weight: 700; }
.capture-notice ul { display: grid; gap: 5px; margin: 9px 0 4px; padding-left: 19px; font-size: 12px; line-height: 1.55; }
.capture-notice li b { display: inline-block; min-width: 38px; }
.daily-workspace { overflow: hidden; }
.daily-toolbar { display: flex; justify-content: space-between; gap: 16px; padding: 18px 20px; border-bottom: 1px solid #e0e5e1; }
.daily-run-state, .daily-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 9px; }
.daily-run-state span { padding: 6px 9px; border-radius: 7px; background: #edf2ee; color: #52635a; font-size: 11px; }
.daily-run-state span.morning { border-left: 3px solid #3c8abb; }
.daily-run-state span.evening { border-left: 3px solid #d46b32; }
.daily-run-state span.manual { border-left: 3px solid #7457a8; }
.daily-run-state span.failed, .daily-run-state span.missing { background: #fff3d8; color: #835d0f; }
.daily-run-state span.pending { background: #eef3f7; color: #597081; }
.daily-run-state small { display: block; margin-top: 2px; opacity: .72; }
.daily-actions button, .daily-actions a, .row-actions button, .daily-filters button { border: 1px solid #d4ddd6; border-radius: 7px; background: #fff; color: #315245; padding: 7px 10px; cursor: pointer; font-size: 11px; text-decoration: none; }
.daily-actions button:disabled { cursor: not-allowed; opacity: .48; }
.export-blocker { display: flex; gap: 12px; padding: 11px 20px; background: #fff1ee; color: #9c3d2d; font-size: 12px; }
.daily-filters { display: flex; gap: 7px; padding: 12px 20px; border-bottom: 1px solid #e0e5e1; }
.daily-filters input { flex: 1; min-width: 220px; padding: 9px 11px; border: 1px solid #d4ddd6; border-radius: 8px; background: #f5f8f5; }
.daily-filters button.active { border-color: #1e5d43; background: #1e5d43; color: white; }
.matrix-meta { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 13px 20px; border-bottom: 1px solid #d9dedb; background: #fbfcfa; }
.matrix-meta strong, .matrix-meta span { display: block; }
.matrix-meta strong { color: #263f35; }
.matrix-meta span { margin-top: 3px; color: #718077; font-size: 11px; }
.matrix-legend { display: flex; flex-wrap: wrap; gap: 5px 13px; margin-top: 7px; color: #65746b; font-size: 10px; }
.matrix-legend i { width: 11px; height: 11px; margin-right: -8px; border: 1px solid rgba(39, 57, 48, .14); border-radius: 2px; }
.matrix-legend .date { background: #d9e5f5; }
.matrix-legend .sales { background: #fce4d6; }
.matrix-legend .stock { background: #ffc7ce; }
.matrix-legend .missing { background: #f5f1e8; }
.matrix-pages { display: flex; align-items: center; gap: 9px; white-space: nowrap; }
.matrix-pages button { padding: 6px 9px; border: 1px solid #cfd8d2; border-radius: 6px; background: white; color: #315245; cursor: pointer; }
.matrix-pages button:disabled { cursor: not-allowed; opacity: .4; }
.daily-matrix-wrap {
  --daily-data-row-height: 38px;
  --daily-note-row-height: 52px;
  --daily-header-height: 64px;
  overflow: auto;
  max-height: calc(
    var(--daily-header-height) +
    15 * var(--daily-data-row-height) +
    5 * var(--daily-note-row-height)
  );
  background: white;
}
.daily-matrix { width: max-content; min-width: 100%; border-collapse: separate; border-spacing: 0; font-family: "SimSun", "宋体", serif; font-size: 13px; table-layout: fixed; }
.daily-matrix th, .daily-matrix td { box-sizing: border-box; min-width: 158px; max-width: 158px; height: var(--daily-data-row-height); padding: 5px 9px; border-right: 1px solid #d7dad8; border-bottom: 1px solid #d7dad8; text-align: center; vertical-align: middle; }
.daily-matrix thead th { position: sticky; top: 0; z-index: 5; height: var(--daily-header-height); border-color: #474747; background: #ffff00; color: #202020; font-weight: 400; }
.daily-matrix .metric-head, .daily-matrix tbody th { position: sticky; left: 0; z-index: 7; min-width: 170px; max-width: 170px; }
.daily-matrix .date-head, .daily-matrix tbody td:first-of-type { position: sticky; left: 188px; z-index: 6; min-width: 138px; max-width: 138px; }
.daily-matrix tbody th, .daily-matrix tbody td:first-of-type { background: #fff; font-weight: 400; }
.daily-matrix .visitor-total th, .daily-matrix .visitor-total td { background: #d9e5f5; }
.daily-matrix tbody .visitor-total td:first-of-type { background: #d9e5f5; }
.daily-matrix .date-start > * { border-top: 3px solid #7898b6; }
.daily-matrix .date-end > * { border-bottom: 2px solid #a7b3ac; }
.daily-matrix .matrix-date { color: #294d70; font-weight: 700; }
.daily-matrix tbody tr:hover td { outline: 1px solid rgba(33, 93, 67, .22); outline-offset: -1px; }
.matrix-product { font-family: "Microsoft YaHei UI", "微软雅黑", sans-serif; }
.matrix-product strong { display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-height: 1.22; font-weight: 500; }
.matrix-product code { display: block; overflow: hidden; margin-top: 4px; font-family: inherit; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.matrix-product.has-conflict { box-shadow: inset 0 5px #c94d3d; }
.matrix-product.has-missing:not(.has-conflict) { box-shadow: inset 0 5px #d6a42b; }
.daily-matrix td.sales-hit { background: #fce4d6; color: #6a391d; }
.daily-matrix td.stock-value-mismatch { background: #ffc7ce; color: #8d1f28; font-weight: 700; }
.daily-matrix td.missing-value { background: #f5f1e8; color: #8b7952; }
.daily-matrix .matrix-note-row > * { height: var(--daily-note-row-height); background: #f8faf8; }
.daily-matrix .matrix-note-row th { color: #53685d; font-weight: 700; }
.matrix-note-cell { color: #596a61; font-family: "Microsoft YaHei UI", "微软雅黑", sans-serif; font-size: 10px; line-height: 1.35; }
.matrix-note-manager { width: 100%; max-width: 100%; padding: 4px 5px; border: 1px solid #d8e0da; border-radius: 6px; background: #fff; color: #596a61; cursor: pointer; font: inherit; line-height: inherit; }
.matrix-note-manager.has-confirmation { border-color: #7db398; background: #eef8f2; color: #236446; font-weight: 700; }
.matrix-note-manager > span:not(.empty-note) { display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow-wrap: anywhere; }
.matrix-note-manager .empty-note { color: #6f8277; }
.review-panel { overflow: hidden; padding: 18px 20px; }
.review-title { display: flex; flex-wrap: wrap; align-items: end; justify-content: space-between; gap: 12px; }
.review-title h3 { margin: 0; }
.review-title > span { color: #7c8981; font-size: 11px; }
.review-list { display: grid; grid-template-columns: minmax(0, 1fr); gap: 9px; margin-top: 14px; }
.review-list article { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 2fr) minmax(160px, 190px); align-items: center; gap: 16px; min-width: 0; padding: 12px 14px; border: 1px solid #dfe5e0; border-radius: 10px; background: #fbfcfa; }
.review-product, .review-issues { min-width: 0; }
.review-product strong, .review-product code, .review-product small { display: block; }
.review-product strong { overflow-wrap: anywhere; }
.review-product code { margin: 3px 0 5px; color: #1e5d43; font-size: 11px; }
.review-product small { margin-bottom: 5px; color: #7b8780; }
.review-issues { display: grid; gap: 9px; }
.review-issue { min-width: 0; padding: 10px 11px; border-radius: 8px; background: #f0f4f1; }
.review-issue header { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 5px 12px; }
.review-issue header strong { color: #315245; font-size: 12px; }
.review-issue header span { color: #748178; font-size: 9px; }
.review-issue.capture-issue { border-left: 4px solid #7d62a8; background: #f5f1fa; }
.review-issue.stock-issue { border-left: 4px solid #c94d3d; background: #fff0ed; }
.review-issue.revert-issue { border-left: 4px solid #d18a21; background: #fff8e8; }
.review-issue.note-issue { border-left: 4px solid #768a7e; }
.issue-field-values { display: grid; grid-template-columns: minmax(100px, .5fr) repeat(auto-fit, minmax(160px, 1fr)); gap: 4px 9px; margin-top: 7px; color: #596a61; font-size: 11px; line-height: 1.45; }
.issue-field-values b { color: #4b3966; }
.issue-field-values span { min-width: 0; padding: 4px 6px; border-radius: 5px; }
.issue-field-values span small { display: block; margin-top: 2px; color: #786d80; font-size: 9px; overflow-wrap: anywhere; }
.issue-field-values span.confirmed-version { background: #e9e1f2; color: #4b3966; font-weight: 700; }
.stock-formula-data { display: grid; gap: 4px; margin-top: 7px; font-size: 11px; line-height: 1.5; }
.stock-formula-data span, .stock-formula-data small, .stock-formula-data strong { display: block; }
.stock-formula-data small { color: #7a6d67; }
.stock-formula-data strong { color: #9c3d2d; }
.propagated-conflict { margin-top: 8px; padding: 8px 9px; border: 1px solid #e8ad7d; border-left: 4px solid #d46b32; border-radius: 7px; background: #fff6ed; color: #714126; font-size: 10px; line-height: 1.5; }
.propagated-conflict > strong, .propagated-conflict span { display: block; }
.propagated-conflict > strong { margin-bottom: 5px; color: #a23f1d; }
.revert-context { display: grid; gap: 4px; margin-top: 7px; color: #765716; font-size: 11px; line-height: 1.5; }
.revert-context small { color: #8a7442; }
.revert-context strong { color: #9b5e12; }
.issue-note { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin-top: 7px; padding: 6px 8px; border-radius: 6px; background: rgba(255, 255, 255, .72); color: #596a61; font-size: 10px; line-height: 1.5; overflow-wrap: anywhere; }
.issue-note > span:first-child { min-width: 0; }
.issue-note small { color: #7a867f; }
.note-actions { display: flex; flex: 0 0 auto; gap: 4px; }
.note-actions button, .note-manager-actions button { padding: 3px 6px; border: 1px solid #cfd9d2; border-radius: 5px; background: #fff; color: #315245; cursor: pointer; font-size: 9px; }
.note-actions button.danger-link, .note-manager-actions button.danger-link { border-color: #e3a294; color: #ad442f; }
.review-empty { margin-top: 14px; padding: 20px; border: 1px dashed #ccd8d0; border-radius: 10px; color: #718077; text-align: center; }
.row-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; width: auto; min-width: 0; }
.row-actions button { margin: 3px; padding: 5px 7px; }
.row-actions button.danger-link { border-color: #e3a294; color: #ad442f; }
.status-badge { display: block; width: fit-content; margin: 0 auto 5px; padding: 4px 7px; border-radius: 999px; background: #e8ece9; color: #536159; font-size: 10px; }
.status-badge.needs_review.version-review { background: #eee7f7; color: #604982; }
.status-badge.needs_review.stock-review { background: #ffe2dc; color: #a63d2d; }
.status-badge.needs_review.revert-review { background: #fff0c9; color: #8e5a0d; }
.status-badge.needs_review.mixed-review { background: #ffe8cf; color: #9a4d18; }
.status-badge.ready { background: #fff1d6; color: #8a5a0d; }
.status-badge.missing_capture { background: #fff1c9; color: #775611; }
.status-badge.confirmed { background: #dff2e7; color: #236446; }
.empty-row { padding: 50px !important; color: #7a8880; }
.daily-modal-backdrop { position: fixed; inset: 0; z-index: 50; display: grid; place-items: center; padding: 20px; background: rgba(13, 35, 26, .48); backdrop-filter: blur(3px); }
.daily-modal { width: min(590px, 100%); max-height: calc(100vh - 40px); overflow: auto; padding: 24px; border-radius: 17px; background: #f9fbf8; box-shadow: 0 30px 80px rgba(7, 31, 20, .25); }
.daily-modal h3 { margin: 0 0 7px; }
.modal-product { color: #6e7c74; font-size: 12px; }
.daily-modal label { display: grid; gap: 6px; margin-top: 13px; color: #4e5f56; font-size: 11px; }
.daily-modal input, .daily-modal select, .daily-modal textarea { width: 100%; padding: 9px 10px; border: 1px solid #d1dad4; border-radius: 8px; background: white; }
.daily-modal textarea { min-height: 100px; font-family: inherit; line-height: 1.5; }
.note-manager-modal { width: min(680px, 100%); }
.note-manager-list { display: grid; gap: 8px; margin-top: 15px; }
.note-manager-list article { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 10px 11px; border: 1px solid #dce3de; border-radius: 8px; background: white; }
.note-manager-list article > div:first-child { min-width: 0; }
.note-manager-list strong, .note-manager-list small { display: block; }
.note-manager-list p { margin: 5px 0; color: #4f6258; overflow-wrap: anywhere; }
.note-manager-list small { color: #7a877f; font-size: 9px; }
.note-manager-actions { display: flex; flex: 0 0 auto; gap: 5px; }
.confirmation-manager { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; margin-top: 14px; padding: 11px 12px; border: 1px solid #aad0bb; border-left: 4px solid #3f8b65; border-radius: 9px; background: #eff8f2; }
.confirmation-manager.reverted { border-color: #e2c17e; border-left-color: #d18a21; background: #fff8e8; }
.confirmation-manager p { margin: 5px 0; color: #365c49; font-size: 11px; }
.confirmation-manager small { color: #6d8176; font-size: 9px; }
.confirmation-manager button { flex: 0 0 auto; padding: 6px 8px; border: 1px solid #e3a294; border-radius: 6px; background: #fff; color: #ad442f; cursor: pointer; font-size: 10px; }
.revert-warning { padding: 9px 10px; border-left: 4px solid #d18a21; border-radius: 7px; background: #fff4d9; color: #805314; font-size: 11px; line-height: 1.5; }
.capture-attempt-log { margin: 0 0 13px; padding: 11px 14px; border: 1px solid #d7e0da; border-radius: 10px; background: #f7faf8; }
.capture-attempt-log summary { cursor: pointer; color: #385346; font-size: 12px; font-weight: 700; }
.capture-attempt-log > div { display: grid; grid-template-columns: 120px minmax(0, 1fr); gap: 6px 12px; margin-top: 10px; }
.capture-attempt-log strong { color: #466055; font-size: 11px; }
.capture-attempt-log span { overflow-wrap: anywhere; color: #64746b; font-size: 11px; }
.capture-attempt-log span.failed { color: #9b4039; }
.daily-run-state em { display: block; color: #9a6420; font-size: 10px; font-style: normal; font-weight: 700; }
.manual-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0 12px; }
.manual-baseline-tip { grid-column: 1 / -1; margin: 10px 0 0; padding: 8px 10px; border-radius: 7px; background: #edf5f0; color: #506a5c; font-size: 10px; line-height: 1.5; }
.manual-grid label > span { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.manual-grid label small { color: #77867e; font-size: 9px; font-weight: 400; }
.modal-actions { display: flex; justify-content: flex-end; gap: 9px; margin-top: 18px; }
.modal-actions button { padding: 9px 15px; border: 1px solid #d1dad4; border-radius: 8px; background: white; color: #315245; cursor: pointer; }
.modal-actions button.action-button { border-color: var(--green); background: var(--green); color: white; }
@media (max-width: 1000px) {
  .daily-kpis { grid-template-columns: repeat(3, 1fr); }
  .daily-toolbar, .daily-run-times { align-items: flex-start; flex-direction: column; }
  .review-list article { grid-template-columns: 1fr; }
}
@media (max-width: 640px) {
  .daily-kpis { grid-template-columns: repeat(2, 1fr); }
  .daily-filters { flex-wrap: wrap; }
  .daily-filters input { flex-basis: 100%; }
  .manual-grid { grid-template-columns: 1fr; }
}
</style>
