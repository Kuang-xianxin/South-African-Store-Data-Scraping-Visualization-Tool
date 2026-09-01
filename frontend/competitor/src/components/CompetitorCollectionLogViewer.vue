<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

import {
  fetchCompetitorCollectionLogs,
  type CompetitorCollectionLogPayload,
  type CompetitorCollectionLogRound,
} from "../api";
import { formatChinaDateTime } from "../time";

const props = defineProps<{
  currentBatchId: string | null;
}>();
const emit = defineEmits<{
  close: [];
}>();

const payload = ref<CompetitorCollectionLogPayload | null>(null);
const selectedBatchId = ref(props.currentBatchId ?? "");
const loading = ref(true);
const refreshing = ref(false);
const error = ref("");
const followingCurrentRound = ref(true);
let refreshTimer: number | null = null;
let requestRevision = 0;

const selectedRound = computed<CompetitorCollectionLogRound | null>(() =>
  payload.value?.selected_round
  ?? payload.value?.rounds.find(
    (round) => round.batch_id === payload.value?.selected_batch_id,
  )
  ?? null,
);

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function formatDuration(value: number | null): string {
  if (value === null) return "—";
  const seconds = Math.max(0, Math.round(value));
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  if (days) return `${days}天 ${hours}小时 ${minutes}分钟`;
  if (hours) return `${hours}小时 ${minutes}分钟`;
  if (minutes) return `${minutes}分钟 ${seconds % 60}秒`;
  return `${seconds}秒`;
}

function metricValue(value: number | null): string {
  return value === null ? "—" : String(value);
}

function sourceLabel(source: CompetitorCollectionLogRound["source"]): string {
  if (source === "scheduled") return "09:00自动持续轮巡";
  if (source === "manual") return "页面手动批次";
  return "来源未记录";
}

function statusLabel(status: CompetitorCollectionLogRound["status"]): string {
  return {
    running: "运行中",
    paused: "网络暂停",
    retry_wait: "等待重试",
    stopped: "人工停止",
    completed: "已完成",
    waiting_resume: "等待服务恢复",
    unknown: "状态未记录",
  }[status];
}

function eventLabel(event: string | null): string {
  if (!event) return "—";
  return {
    start: "轮次开始",
    progress: "进度更新",
    heartbeat: "运行心跳",
    resume: "继续轮次",
    auto_resume: "自动继续",
    resume_after_restart: "ERP重启后续跑",
    manual_resume: "人工继续",
    network_pause: "网络暂停",
    paused: "批次暂停",
    network_auto_resume: "网络恢复后续跑",
    pending_retry_wait: "等待安全重试",
    pending_retry_auto_resume: "开始安全重试",
    manual_stop: "人工停止",
    completed: "轮次完成",
    process_shutdown: "ERP进程关闭",
    restore_completed_pending: "恢复待重试断点",
  }[event] ?? event;
}

function retryLabel(round: CompetitorCollectionLogRound): string {
  if (round.retry_round === null) return "未进入延时重试";
  return round.retry_round_limit === null
    ? `第 ${round.retry_round} 轮`
    : `第 ${round.retry_round}/${round.retry_round_limit} 轮`;
}

async function loadRounds() {
  const revision = ++requestRevision;
  if (!payload.value) loading.value = true;
  refreshing.value = true;
  error.value = "";
  try {
    const result = await fetchCompetitorCollectionLogs(
      selectedBatchId.value || undefined,
    );
    if (revision !== requestRevision) return;
    payload.value = result;
    selectedBatchId.value = result.selected_batch_id ?? "";
  } catch (caught) {
    if (revision !== requestRevision) return;
    error.value = caught instanceof Error ? caught.message : "读取轮次详情失败";
  } finally {
    if (revision === requestRevision) {
      loading.value = false;
      refreshing.value = false;
    }
  }
}

function handleRoundChange() {
  followingCurrentRound.value = Boolean(
    props.currentBatchId && selectedBatchId.value === props.currentBatchId,
  );
  void loadRounds();
}

watch(
  () => props.currentBatchId,
  (batchId) => {
    if (followingCurrentRound.value) {
      selectedBatchId.value = batchId ?? "";
      void loadRounds();
    }
  },
);

onMounted(() => {
  void loadRounds();
  refreshTimer = window.setInterval(() => {
    if (!refreshing.value) void loadRounds();
  }, 3_000);
});

onBeforeUnmount(() => {
  requestRevision += 1;
  if (refreshTimer !== null) window.clearInterval(refreshTimer);
});
</script>

<template>
  <Teleport to="body">
    <div
      class="competitor-modal-backdrop collection-log-modal-backdrop"
      @click.self="emit('close')"
    >
      <section
        class="competitor-modal collection-log-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="collection-log-modal-title"
      >
        <header class="competitor-modal-header collection-log-modal-header">
          <div>
            <p class="section-kicker">COLLECTION ROUNDS</p>
            <h2 id="collection-log-modal-title">竞品雷达轮次详情</h2>
            <span>一个批次编号代表一轮；这里只显示轮次汇总，不展示逐商品爬取行</span>
          </div>
          <button
            class="competitor-modal-close"
            type="button"
            aria-label="关闭轮次详情"
            @click="emit('close')"
          >
            ×
          </button>
        </header>

        <div class="collection-log-content">
          <div class="collection-log-toolbar">
            <label>
              采集轮次
              <select
                v-model="selectedBatchId"
                :disabled="loading || !(payload?.rounds.length)"
                @change="handleRoundChange"
              >
                <option v-if="!payload?.rounds.length" value="">暂无轮次记录</option>
                <option
                  v-for="round in payload?.rounds ?? []"
                  :key="round.batch_id"
                  :value="round.batch_id"
                >
                  {{ round.current ? "当前 · " : "" }}
                  {{ round.round_number === null ? "手动轮次" : `第${round.round_number}轮` }}
                  · {{ statusLabel(round.status) }} · {{ round.batch_id }}
                </option>
              </select>
            </label>
            <button
              class="secondary-button"
              type="button"
              :disabled="refreshing"
              @click="loadRounds"
            >
              {{ refreshing ? "刷新中…" : "刷新轮次详情" }}
            </button>
            <span class="collection-round-count">
              共 {{ payload?.total_rounds ?? 0 }} 轮
            </span>
          </div>

          <p v-if="error" class="collection-log-error" role="alert">
            {{ error }}
          </p>
          <p v-if="loading" class="collection-log-state" role="status">
            正在读取轮次详情…
          </p>
          <section v-else-if="selectedRound" class="collection-round-summary">
            <header class="collection-round-summary-header">
              <div>
                <span :class="['collection-round-status', `is-${selectedRound.status}`]">
                  {{ statusLabel(selectedRound.status) }}
                </span>
                <strong>{{ selectedRound.batch_id }}</strong>
                <small>{{ sourceLabel(selectedRound.source) }}</small>
              </div>
              <span>
                日志更新 {{ formatChinaDateTime(selectedRound.modified_at) }}
                · {{ formatBytes(selectedRound.size_bytes) }}
              </span>
            </header>

            <div class="collection-round-metrics">
              <article>
                <small>本轮目标</small>
                <strong>{{ metricValue(selectedRound.total) }}</strong>
              </article>
              <article>
                <small>已检查</small>
                <strong>{{ metricValue(selectedRound.completed) }}</strong>
              </article>
              <article class="success">
                <small>成功</small>
                <strong>{{ metricValue(selectedRound.succeeded) }}</strong>
              </article>
              <article class="warning">
                <small>未解决失败</small>
                <strong>{{ metricValue(selectedRound.failed) }}</strong>
              </article>
              <article>
                <small>确认失效</small>
                <strong>{{ metricValue(selectedRound.terminal) }}</strong>
              </article>
              <article>
                <small>待处理</small>
                <strong>{{ metricValue(selectedRound.pending) }}</strong>
              </article>
            </div>

            <dl class="collection-round-facts">
              <div>
                <dt>轮次号</dt>
                <dd>{{ selectedRound.round_number ?? "手动批次" }}</dd>
              </div>
              <div>
                <dt>运行修订</dt>
                <dd>{{ selectedRound.revision ?? "—" }}</dd>
              </div>
              <div>
                <dt>触发日期</dt>
                <dd>{{ selectedRound.trigger_date ?? "—" }}</dd>
              </div>
              <div>
                <dt>开始时间</dt>
                <dd>{{ formatChinaDateTime(selectedRound.started_at) }}</dd>
              </div>
              <div>
                <dt>结束时间</dt>
                <dd>{{ formatChinaDateTime(selectedRound.completed_at) }}</dd>
              </div>
              <div>
                <dt>累计耗时</dt>
                <dd>{{ formatDuration(selectedRound.elapsed_seconds) }}</dd>
              </div>
              <div>
                <dt>最近轮次事件</dt>
                <dd>{{ eventLabel(selectedRound.latest_event) }}</dd>
              </div>
              <div>
                <dt>重试轮次</dt>
                <dd>{{ retryLabel(selectedRound) }}</dd>
              </div>
              <div>
                <dt>汇总更新时间</dt>
                <dd>{{ formatChinaDateTime(selectedRound.summary_updated_at) }}</dd>
              </div>
            </dl>

            <div v-if="selectedRound.reason" class="collection-round-reason">
              <strong>本轮状态原因</strong>
              <span>{{ selectedRound.reason }}</span>
            </div>
          </section>
          <p v-else class="collection-log-state">当前还没有可显示的轮次记录。</p>
          <p v-if="payload && payload.total_rounds > payload.rounds.length" class="collection-log-note">
            当前列出最近 {{ payload.rounds.length }} / {{ payload.total_rounds }} 个轮次。
          </p>
        </div>

        <div class="competitor-modal-actions collection-log-actions">
          <span>轮次详情仅供查看，不会改变采集任务或断点。</span>
          <button type="button" @click="emit('close')">关闭</button>
        </div>
      </section>
    </div>
  </Teleport>
</template>
