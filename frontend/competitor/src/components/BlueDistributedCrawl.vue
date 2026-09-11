<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef } from "vue";
import { ApiRequestError, controlBlueCrawl, fetchBlueCrawlPreview, fetchBlueCrawlStatus,
  fetchBlueDeployment, startBlueCrawl } from "../api";
import { blueBatchActive, blueBatchLabel, bluePendingBatchId, blueWorkerLabel, blueWorkerBacklog, parseBlueTestPlids,
  readBluePending, type BlueCrawlPending, type BlueCrawlPreview, type BlueCrawlStatus } from "../blueDistributedCrawl";
import { formatChinaDateTime } from "../time";

const props = defineProps<{ legacyActive: boolean; username: string }>();
const emit = defineEmits<{ available: [value: boolean]; active: [value: boolean]; stopLegacy: [] }>();
const available = ref(false);
const expanded = ref(false);
const status = shallowRef<BlueCrawlStatus | null>(null);
const preview = shallowRef<BlueCrawlPreview | null>(null);
const loading = ref(false);
const busy = ref(false);
const fresh = ref(false);
const notice = ref("");
const error = ref("");
const testPlids = ref("");
const selectedBatchId = ref("");
const stopConfirmId = ref("");
const pending = ref<BlueCrawlPending | null>(null);
const storageKey = `takealot-blue-crawl-pending-v2:${props.username}`;
let timer: ReturnType<typeof setTimeout> | undefined;
let disposed = false;
const readController = new AbortController();

const activeBatch = computed(() => status.value?.batches.find(blueBatchActive));
const batch = computed(() => status.value?.batches.find(row => row.batch_id === selectedBatchId.value)
  ?? activeBatch.value ?? status.value?.batches[0]);
// History selection must never redirect the main stop button away from the active batch.
const controlBatch = computed(() => activeBatch.value ?? batch.value);
const resumableBatch = computed(() => !activeBatch.value && Number(controlBatch.value?.cancelled) > 0
  ? controlBatch.value : undefined);
const items = computed(() => status.value?.items.filter(row => row.batch_id === batch.value?.batch_id) ?? []);
const nodes = computed(() => ["blue-main", "blue-laptop"].map(id => ({
  id, name: id === "blue-main" ? "主机" : "笔记本",
  worker: status.value?.workers.find(worker => worker.worker_id === id),
})));
const canStart = computed(() => fresh.value && !busy.value && !loading.value && !props.legacyActive && !activeBatch.value
  && status.value?.full_crawl === true && Boolean(preview.value?.total)
  && status.value.workers.some(worker => Boolean(worker.online)));
const blockedReason = computed(() => {
  if (props.legacyActive) return "上次采集尚未结束，请先停止上次采集；已有结果和断点会保留。";
  if (!fresh.value) return "正在核对双机状态，暂不提交新任务。";
  if (!status.value?.full_crawl) return "两台爬虫尚未全部加载全量版本，请等待蓝版更新完成。";
  if (!status.value.workers.some(worker => Boolean(worker.online))) return "两台电脑均未在线，等待连接恢复。";
  return "";
});

function persistPending(value: BlueCrawlPending | null) {
  // Persist BEFORE POST; if local storage is unavailable, do not risk dispatch
  // without a recoverable idempotency key.
  if (value) sessionStorage.setItem(storageKey, JSON.stringify(value));
  else sessionStorage.removeItem(storageKey);
  pending.value = value;
}

async function refresh(withPreview = false) {
  if (loading.value || disposed || !available.value) return;
  loading.value = true;
  try {
    const next = await fetchBlueCrawlStatus(readController.signal);
    const nextPreview = withPreview || !preview.value
      ? await fetchBlueCrawlPreview(readController.signal) : preview.value;
    if (disposed) return;
    status.value = next;
    preview.value = nextPreview;
    fresh.value = true;
    error.value = "";
    emit("active", next.batches.some(blueBatchActive));
    if (pending.value) {
      const acceptedId = bluePendingBatchId(pending.value);
      if (next.batches.some(row => row.batch_id === acceptedId)) {
        selectedBatchId.value = acceptedId;
        persistPending(null);
        notice.value = "已确认上次提交成功，继续查看原批次。";
      }
    }
  } catch (caught) {
    if (disposed) return;
    fresh.value = false;
    error.value = caught instanceof Error ? caught.message : "双机状态读取失败";
  } finally { loading.value = false; }
}

function scheduleRefresh() {
  timer = setTimeout(async () => {
    if (disposed) return;
    if (!document.hidden) await refresh();
    scheduleRefresh();
  }, 10000);
}

async function toggleDetails() {
  expanded.value = !expanded.value;
  if (expanded.value) await refresh(true);
}

function requestId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, "0")).join("");
}

async function start(mode: "full" | "selected" = "full") {
  if (busy.value || loading.value) return;
  error.value = "";
  notice.value = "";
  expanded.value = true;
  try {
    if (pending.value && pending.value.mode !== mode) throw new Error("上次提交尚未确认，请先核对原请求。");
    await refresh(true);
    if (!canStart.value) return;
    busy.value = true;
    const plids = mode === "selected" ? parseBlueTestPlids(testPlids.value) : [];
    if (pending.value && JSON.stringify(pending.value.plids) !== JSON.stringify(plids)) {
      throw new Error("上次提交尚未确认，请保留原商品清单并核对原请求。");
    }
    const dispatch = pending.value ?? { request_id: requestId(), mode, plids };
    persistPending(dispatch);
    const result = await startBlueCrawl(dispatch);
    persistPending(null);
    selectedBatchId.value = result.batch_id;
    notice.value = `已提交 ${result.total} 条链接。两台电脑从同一队列领取，关闭页面也会继续。`;
    await refresh();
  } catch (caught) {
    if (caught instanceof ApiRequestError && caught.status >= 400 && caught.status < 500) persistPending(null);
    error.value = caught instanceof Error ? caught.message : "提交失败，请先核对状态";
  } finally { busy.value = false; }
}

async function control(action: "stop" | "resume", batchId: string) {
  if (busy.value || !fresh.value || (action === "resume" && (props.legacyActive || activeBatch.value))) return;
  busy.value = true;
  error.value = "";
  try {
    const result = await controlBlueCrawl(action, batchId);
    notice.value = result.message;
    stopConfirmId.value = "";
    await refresh();
  } catch (caught) { error.value = caught instanceof Error ? caught.message : "操作失败，请刷新状态"; }
  finally { busy.value = false; }
}

const itemLabels: Record<string, string> = {
  pending: "待领取", leased: "采集中", retry: "待重试", succeeded: "成功", terminal: "已终止", cancelled: "已停止",
};

onMounted(async () => {
  try {
    available.value = await fetchBlueDeployment(readController.signal);
    if (disposed || !available.value) return;
    emit("available", true);
    pending.value = readBluePending(sessionStorage.getItem(storageKey));
    if (pending.value?.mode === "selected") testPlids.value = pending.value.plids.join("\n");
    expanded.value = Boolean(pending.value);
    await refresh(true);
    scheduleRefresh();
  } catch (caught) {
    if (available.value && !disposed) error.value = caught instanceof Error ? caught.message : "无法读取双机状态";
  }
});
onBeforeUnmount(() => { disposed = true; clearTimeout(timer); readController.abort(); });
</script>

<template>
  <section v-if="available" class="blue-crawl-panel" aria-labelledby="blue-crawl-title">
    <div class="collector-run-heading">
      <div>
        <p class="section-kicker">管理员批次控制</p>
        <h3 id="blue-crawl-title">批量采集真正竞品 + 自有链接</h3>
      </div>
      <span v-if="preview">真正竞品 {{ preview.competitors }} 个 · 自有链接 {{ preview.own }} 个 · 共 {{ preview.total }} 个</span>
      <span v-else>正在核对全量清单…</span>
    </div>
    <div class="collector-actions blue-crawl-actions">
      <button v-if="activeBatch" type="button" class="primary-button stop-button"
        :disabled="busy || !fresh" @click="stopConfirmId = activeBatch.batch_id">停止采集</button>
      <button v-else-if="resumableBatch" type="button" class="primary-button resume-button"
        :disabled="busy || !fresh || legacyActive" @click="control('resume', resumableBatch.batch_id)">
        {{ busy ? "正在处理…" : `继续未完成（${resumableBatch.cancelled}）` }}
      </button>
      <button v-if="!activeBatch" type="button" :class="resumableBatch ? 'secondary-button blue-crawl-new-round' : 'primary-button'"
        :disabled="!canStart || Boolean(pending)" @click="start('full')">
        {{ busy ? "正在处理…" : resumableBatch ? "开始新一轮采集" : `开始采集（${preview?.total ?? '…'}）` }}
      </button>
      <button v-if="legacyActive" type="button" class="secondary-button" @click="emit('stopLegacy')">停止上次采集</button>
      <button type="button" class="secondary-button" :aria-expanded="expanded" aria-controls="blue-crawl-details" @click="toggleDetails">
        {{ expanded ? "收起任务详情" : "查看任务详情" }}
      </button>
      <button type="button" class="secondary-button" :disabled="loading || busy" @click="refresh(true)">{{ loading ? "正在核对…" : "刷新状态" }}</button>
    </div>
    <p class="blue-crawl-note">主机与笔记本共同采集，每台同时处理一条；关闭页面后继续在后台运行。</p>
    <div v-if="stopConfirmId && activeBatch?.batch_id === stopConfirmId" class="blue-crawl-pending" role="status">
      当前商品完成并保存后停止；断线电脑收到停止通知前可能继续采集。
      <div class="blue-crawl-actions">
        <button type="button" class="primary-button stop-button" :disabled="busy" @click="control('stop', stopConfirmId)">{{ busy ? "正在停止…" : "确认停止" }}</button>
        <button type="button" class="secondary-button" :disabled="busy" @click="stopConfirmId = ''">取消</button>
      </div>
    </div>
    <div v-if="controlBatch" class="blue-crawl-batch" aria-live="polite">
      <div class="blue-crawl-heading">
        <strong>{{ blueBatchLabel(controlBatch) }} · {{ controlBatch.total }} 条</strong>
        <small>{{ formatChinaDateTime(controlBatch.updated_at) }}</small>
      </div>
      <progress :value="Number(controlBatch.succeeded) + Number(controlBatch.failed)" :max="controlBatch.total"
        :aria-label="`已完成${Number(controlBatch.succeeded) + Number(controlBatch.failed)}条，共${controlBatch.total}条`" />
      <p>成功 {{ controlBatch.succeeded }} · 已终止 {{ controlBatch.failed }} · 采集中 {{ controlBatch.running }} · 待领取 {{ controlBatch.pending }} · 待重试 {{ controlBatch.retry }} · 待继续 {{ controlBatch.cancelled }}</p>
    </div>
    <div class="blue-crawl-workers">
      <div v-for="node in nodes" :key="node.id" class="blue-crawl-worker">
        <strong>{{ node.name }}</strong>
        <span :class="{ 'blue-crawl-online': node.worker?.online }">{{ blueWorkerLabel(node.worker) }}</span>
        <span v-if="node.worker?.current_plid">PLID{{ node.worker.current_plid }}</span>
        <span v-if="blueWorkerBacklog(node.worker)" class="blue-crawl-error">{{ blueWorkerBacklog(node.worker) }}</span>
      </div>
    </div>
    <p v-if="blockedReason" class="blue-crawl-note">{{ blockedReason }}</p>
    <p v-if="error" class="blue-crawl-error" role="alert">{{ error }}</p>
    <p v-if="notice" class="blue-crawl-notice" role="status">{{ notice }}</p>
    <div v-if="pending" class="blue-crawl-pending" role="status">
      上次提交结果尚未确认。再次核对会沿用原请求，不会另建批次。
      <button type="button" class="secondary-button" :disabled="busy || loading" @click="start(pending.mode)">核对原请求</button>
    </div>
    <div v-if="expanded" id="blue-crawl-details" class="blue-crawl-details">
      <div class="blue-crawl-actions">
        <label v-if="status?.batches.length">查看批次
          <select v-model="selectedBatchId" aria-label="采集批次">
            <option value="">{{ activeBatch ? "当前运行批次" : "最近批次" }}</option>
            <option v-for="row in status.batches" :key="row.batch_id" :value="row.batch_id">
              {{ formatChinaDateTime(row.updated_at) }} · {{ row.total }}条 · {{ blueBatchLabel(row) }}
            </option>
          </select>
        </label>
      </div>
      <div v-if="batch" class="blue-crawl-batch">
        <strong>{{ blueBatchLabel(batch) }} · {{ batch.total }} 条</strong>
        <p>成功 {{ batch.succeeded }} · 已终止 {{ batch.failed }} · 待继续 {{ batch.cancelled }}</p>
        <div v-if="items.length" class="blue-crawl-table-wrap">
          <table><thead><tr><th>商品</th><th>范围</th><th>状态</th><th>结果</th></tr></thead>
            <tbody><tr v-for="item in items" :key="`${item.batch_id}:${item.plid}`">
              <td><strong>PLID{{ item.plid }}</strong><small>{{ item.title }}</small></td>
              <td>{{ item.followers_only ? '自有跟卖' : '真正竞品' }}</td>
              <td>{{ itemLabels[item.status] ?? '待核对' }}<small v-if="item.lease_owner">{{ item.lease_owner === 'blue-main' ? '主机' : '笔记本' }}</small></td>
              <td>{{ item.message || '等待领取' }}</td>
            </tr></tbody>
          </table>
        </div>
        <p class="blue-crawl-note">汇总包含整批任务，明细显示最近100条更新。断线时结果先保存在本机，连接恢复后补交。</p>
      </div>
      <p v-else class="blue-crawl-note">尚无采集批次。</p>
    </div>
  </section>
</template>

<style scoped>
.blue-crawl-panel{min-width:0}
.blue-crawl-heading,.blue-crawl-actions,.blue-crawl-workers,.blue-crawl-worker{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.blue-crawl-heading{justify-content:space-between}.blue-crawl-heading p{margin:0}
.blue-crawl-workers{margin:14px 0;gap:12px}.blue-crawl-worker{flex:1;min-width:180px;border:1px solid var(--line,#dbe3ed);border-radius:10px;padding:12px}
.blue-crawl-worker span{font-size:13px}.blue-crawl-online{color:#167046}.blue-crawl-note,small{color:var(--muted,#64748b);font-size:12px;line-height:1.6}
.blue-crawl-note{margin:10px 0}.blue-crawl-error{color:#a62e20}.blue-crawl-notice{color:#166534}.blue-crawl-error,.blue-crawl-notice{font-size:13px}
.blue-crawl-details{border-top:1px solid var(--line,#dbe3ed);padding-top:16px;margin-top:14px}.blue-crawl-batch{margin-top:16px}
.blue-crawl-batch progress{width:100%;height:10px;margin-top:12px;accent-color:#2563eb}.blue-crawl-batch p{font-size:13px}
.blue-crawl-actions label{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.blue-crawl-actions select{max-width:100%;min-height:40px;border:1px solid #cbd5e1;border-radius:8px;padding:6px}
.blue-crawl-pending{border:1px solid #e5c475;border-radius:8px;background:#fff9eb;padding:12px;margin:12px 0;font-size:13px}.blue-crawl-pending .blue-crawl-actions{margin-top:10px}
.blue-crawl-table-wrap{overflow-x:auto;margin-top:14px}.blue-crawl-table-wrap table{width:100%;border-collapse:collapse;text-align:left;font-size:12px}
.blue-crawl-table-wrap td,.blue-crawl-table-wrap th{padding:10px 8px;border-bottom:1px solid var(--line,#dbe3ed);vertical-align:top}.blue-crawl-table-wrap td:first-child{min-width:130px}.blue-crawl-table-wrap td:last-child{min-width:170px}.blue-crawl-table-wrap small{display:block}
@media(max-width:600px){.collector-actions.blue-crawl-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));width:100%;align-items:stretch}.collector-actions.blue-crawl-actions button{box-sizing:border-box;width:100%;min-width:0;min-height:44px;white-space:normal}.collector-actions.blue-crawl-actions .primary-button,.collector-actions.blue-crawl-actions .blue-crawl-new-round{grid-column:1/-1}.blue-crawl-worker{min-width:0;flex-basis:100%}.blue-crawl-actions{gap:8px}.blue-crawl-actions select{width:100%}}
</style>
