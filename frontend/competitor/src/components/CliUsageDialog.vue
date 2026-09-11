<script setup lang="ts">
import { computed, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from "vue";
import { fetchCliUsage } from "../api";
import type { CliUsagePayload, CliUsagePeriod } from "../cliUsage";

const props = defineProps<{ userId?: number; allUsers?: boolean; title?: string }>();
const emit = defineEmits<{ close: [] }>();
const dialog = ref<HTMLDialogElement | null>(null);
const period = ref<CliUsagePeriod>("30d");
const data = ref<CliUsagePayload | null>(null);
const loading = ref(false);
const error = ref("");
let requestVersion = 0;
const stages: Record<string, string> = {
  isolated_image_observation: "独立识图", image_title_fusion: "图题融合", competitor_title_reading: "竞品标题复核",
};
const statuses: Record<string, string> = { completed: "CLI完成", failed: "调用失败", blocked: "调用前拦截", running: "尚未确认结束" };
const formatter = new Intl.NumberFormat("zh-CN");
function number(value: number | null) { return value === null ? "未返回" : formatter.format(value); }
function time(value: string) { return new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }); }
const storeNames = computed(() => new Map(data.value?.by_store.map(row => [row.store_code, row.display_name])));
async function load() {
  const version = ++requestVersion;
  loading.value = true;
  error.value = "";
  data.value = null;
  try {
    const result = await fetchCliUsage(period.value, props.userId, props.allUsers);
    if (version === requestVersion) data.value = result;
  } catch (reason) {
    if (version === requestVersion) error.value = reason instanceof Error ? reason.message : "CLI用量读取失败";
  } finally {
    if (version === requestVersion) loading.value = false;
  }
}
watch(() => [period.value, props.userId, props.allUsers], load, { immediate: true });
onMounted(() => dialog.value?.showModal());
onBeforeUnmount(() => { requestVersion += 1; dialog.value?.close(); });
onDeactivated(() => { requestVersion += 1; data.value = null; dialog.value?.close(); emit("close"); });
</script>

<template>
  <dialog ref="dialog" class="cli-usage-dialog" aria-label="CLI用量" @cancel.prevent="emit('close')">
    <header>
      <div><h2>{{ title || (allUsers ? '全部账号 CLI 用量' : '我的 CLI 用量') }}</h2><p>按实际模型请求记账 · 北京时间</p></div>
      <button type="button" class="close-button" aria-label="关闭CLI用量" @click="emit('close')">关闭</button>
    </header>
    <div class="usage-controls">
      <label>统计区间 <select v-model="period"><option value="today">今天</option><option value="7d">近7天</option><option value="30d">近30天</option><option value="all">全部已记账记录</option></select></label>
      <button type="button" :disabled="loading" @click="load">刷新用量</button>
    </div>
    <p v-if="loading" role="status">正在读取用量…</p>
    <p v-else-if="error" role="alert" class="usage-error">{{ error }}</p>
    <template v-else-if="data">
      <div class="usage-totals">
        <div><span>已记录 Token</span><strong>{{ number(data.total.total_tokens) }}</strong></div>
        <div><span>输入 / 输出 Token</span><strong>{{ number(data.total.input_tokens) }} / {{ number(data.total.output_tokens) }}</strong></div>
        <div><span>模型请求</span><strong>{{ number(data.total.requests) }} 次</strong></div>
      </div>
      <p class="usage-note">{{ data.total.completed }} 次CLI完成 · {{ data.total.failed }} 次失败 · {{ data.total.blocked }} 次调用前拦截 · {{ data.total.unfinished }} 次尚未确认结束</p>
      <p v-if="data.total.unknown_usage_requests" class="usage-warning">{{ data.total.unknown_usage_requests }} 次请求未完整返回Token，用量合计尚不完整。</p>
      <p class="usage-note">仅统计独立记账后的请求；旧记录未分摊到个人。缓存复用不新增消耗，输入Token已包含模型缓存读取部分。共享Codex额度百分比不分摊。</p>
      <div v-if="allUsers" class="usage-table-wrap">
        <table><caption>按ERP账号</caption><thead><tr><th>账号</th><th>请求</th><th>已记录Token</th><th>用量未返回</th></tr></thead>
          <tbody><tr v-for="row in data.by_user" :key="row.user_id ?? 'unknown'"><td>{{ row.display_name }}<small v-if="row.username">@{{ row.username }}</small></td><td>{{ number(row.requests) }}</td><td>{{ number(row.total_tokens) }}</td><td>{{ number(row.unknown_usage_requests) }}</td></tr></tbody>
        </table>
      </div>
      <div v-if="data.by_store.length" class="usage-table-wrap">
        <table><caption>按店铺</caption><thead><tr><th>店铺</th><th>请求</th><th>输入Token</th><th>输出Token</th><th>已记录Token</th></tr></thead>
          <tbody><tr v-for="row in data.by_store" :key="row.store_code"><td>{{ row.display_name }}</td><td>{{ number(row.requests) }}</td><td>{{ number(row.input_tokens) }}</td><td>{{ number(row.output_tokens) }}</td><td>{{ number(row.total_tokens) }}</td></tr></tbody>
        </table>
      </div>
      <p v-else>这个区间尚无独立记账记录。</p>
      <details v-if="data.recent.length" class="usage-history"><summary>最近 {{ data.recent.length }} 条尝试</summary>
        <div class="usage-table-wrap"><table><thead><tr><th>时间</th><th v-if="allUsers">账号</th><th>店铺 / 阶段</th><th>状态</th><th>Token</th></tr></thead>
          <tbody><tr v-for="row in data.recent" :key="row.id"><td>{{ time(row.created_at) }}</td><td v-if="allUsers">{{ row.actor_username || '未归属' }}</td><td>{{ storeNames.get(row.store_code) || row.store_code }}<small>{{ stages[row.stage] || row.stage }}</small></td><td>{{ statuses[row.status] || row.status }}</td><td>{{ row.dispatched_at ? number(row.total_tokens) : '未发出请求' }}</td></tr></tbody>
        </table></div>
      </details>
    </template>
  </dialog>
</template>

<style scoped>
.cli-usage-dialog { width: min(920px, 94vw); max-height: 88vh; overflow: auto; padding: 24px; border: 1px solid #cbded2; border-radius: 16px; color: #223b2e; background: #fbfdfb; }
.cli-usage-dialog::backdrop { background: rgb(17 38 27 / 40%); }
header, .usage-controls { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
h2 { margin: 0; font-size: 21px; } header p, .usage-note { color: #65766b; font-size: 12px; line-height: 1.65; }
button, select { border: 1px solid #c7d9cc; border-radius: 7px; background: white; padding: 7px 11px; color: inherit; } button { cursor: pointer; } button:disabled { opacity: .6; cursor: wait; }
.usage-controls { margin: 14px 0; } .usage-totals { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.usage-totals > div { padding: 14px; background: #eef5ef; border-radius: 9px; } .usage-totals span { display: block; font-size: 12px; color: #546c5c; } .usage-totals strong { display: block; margin-top: 7px; font-size: 21px; }
.usage-warning, .usage-error { padding: 10px; background: #fff1dc; border-radius: 7px; color: #7a4713; }
.usage-table-wrap { overflow-x: auto; margin: 16px 0; } table { border-collapse: collapse; width: 100%; font-size: 13px; } caption { text-align: left; font-size: 15px; font-weight: 700; margin-bottom: 8px; }
th, td { padding: 10px 9px; text-align: right; border-bottom: 1px solid #dce7df; white-space: nowrap; } th:first-child, td:first-child { text-align: left; } th { color: #617367; font-size: 12px; } td small { display: block; margin-top: 4px; color: #76877b; font-size: 11px; }
.usage-history { margin-top: 18px; } summary { cursor: pointer; font-size: 13px; }
@media(max-width: 650px) { .cli-usage-dialog { padding: 15px; } .usage-totals { grid-template-columns: 1fr; gap: 6px; } .usage-totals > div { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 10px; } .usage-totals strong { margin: 0; font-size: 17px; } }
</style>
