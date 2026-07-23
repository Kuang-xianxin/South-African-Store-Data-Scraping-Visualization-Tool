<script setup lang="ts">
import { ref, watch } from "vue";

import {
  fetchExports,
  generateExports,
  generateNft102,
  inspectNft102,
} from "../api";
import type {
  ExportPayload,
  NftGeneration,
  NftInspection,
} from "../types";

const props = defineProps<{ asOf: string }>();
const tab = ref<"exports" | "nft102">("exports");
const exports = ref<ExportPayload | null>(null);
const exporting = ref(false);
const message = ref("");
const nftFile = ref<File | null>(null);
const inspection = ref<NftInspection | null>(null);
const reportDate = ref("");
const inspecting = ref(false);
const generating = ref(false);
const generation = ref<NftGeneration | null>(null);

watch(() => props.asOf, loadExports, { immediate: true });

async function loadExports() {
  exports.value = await fetchExports(props.asOf);
}

async function runExport() {
  exporting.value = true;
  message.value = "";
  try {
    exports.value = await generateExports(props.asOf);
    message.value = exports.value.png_error
      ? "网页和电子表格已生成，图片暂未生成。"
      : "三种日报均已生成。";
  } catch (error) {
    message.value = error instanceof Error ? error.message : "报表生成失败";
  } finally {
    exporting.value = false;
  }
}

async function chooseFile(event: Event) {
  const input = event.target as HTMLInputElement;
  nftFile.value = input.files?.[0] ?? null;
  inspection.value = null;
  generation.value = null;
  if (!nftFile.value) return;
  inspecting.value = true;
  try {
    inspection.value = await inspectNft102(nftFile.value);
    reportDate.value = inspection.value.suggested_report_date;
  } catch (error) {
    message.value = error instanceof Error ? error.message : "文件校验失败";
  } finally {
    inspecting.value = false;
  }
}

async function runNftGeneration() {
  if (!nftFile.value || !reportDate.value) return;
  generating.value = true;
  message.value = "";
  try {
    generation.value = await generateNft102(nftFile.value, reportDate.value);
    message.value = `${reportDate.value} 的 NFT102 新表格已生成。`;
  } catch (error) {
    message.value = error instanceof Error ? error.message : "NFT102 生成失败";
  } finally {
    generating.value = false;
  }
}

function megabytes(value: number) {
  return `${(value / 1024 / 1024).toFixed(2)} MB`;
}
</script>

<template>
  <div class="erp-page reports-page">
    <div class="page-intro">
      <div>
        <p class="section-kicker">REPORT WORKSPACE</p>
        <h2>导出与日报续写集中处理</h2>
      </div>
      <div class="page-tabs">
        <button :class="{ active: tab === 'exports' }" @click="tab = 'exports'">运营日报</button>
        <button :class="{ active: tab === 'nft102' }" @click="tab = 'nft102'">NFT102 续写</button>
      </div>
    </div>
    <p v-if="message" class="global-notice">{{ message }}</p>

    <section v-if="tab === 'exports'" class="erp-panel export-panel">
      <div class="panel-heading">
        <div>
          <p class="section-kicker">DAILY REPORTS</p>
          <h3>{{ asOf }} 运营日报</h3>
        </div>
        <button class="action-button" :disabled="exporting" @click="runExport">
          {{ exporting ? "正在生成…" : "生成全部报表" }}
        </button>
      </div>
      <p class="method-note">只读取当前 SQLite，不调用平台接口；同时尝试 HTML、Excel 和 PNG。</p>
      <div class="export-cards">
        <article v-for="file in exports?.files ?? []" :key="file.kind">
          <span>{{ file.label }}</span>
          <strong>{{ file.exists ? "已生成" : "未生成" }}</strong>
          <small>{{ file.name }}</small>
          <a v-if="file.download_url" :href="file.download_url">下载{{ file.label }}</a>
          <button v-else disabled>等待生成</button>
        </article>
      </div>
    </section>

    <section v-else class="nft-workspace">
      <article class="erp-panel upload-panel">
        <p class="section-kicker">OPERATOR BASELINE</p>
        <h3>上传运营最终版</h3>
        <p>系统以本次上传文件为唯一基准；原文件不会被覆盖。</p>
        <label class="file-drop">
          <input type="file" accept=".xlsx" @change="chooseFile" />
          <strong>{{ nftFile?.name || "选择电子表格" }}</strong>
          <span>{{ inspecting ? "正在校验…" : "仅支持 .xlsx，最大100 MB" }}</span>
        </label>
      </article>

      <article class="erp-panel" :class="{ muted: !inspection }">
        <p class="section-kicker">VALIDATION & GENERATION</p>
        <h3>核对日期并生成</h3>
        <div v-if="inspection" class="inspection-grid">
          <span><small>文件大小</small><strong>{{ megabytes(inspection.size_bytes) }}</strong></span>
          <span><small>商品列</small><strong>{{ inspection.product_columns }}</strong></span>
          <span><small>表内最新日期</small><strong>{{ inspection.latest_report_date }}</strong></span>
          <label><small>本次新增日期</small><input v-model="reportDate" type="date" /></label>
        </div>
        <div v-else class="state-card slim">先上传并通过校验，才能生成下一日表格。</div>
        <button
          class="action-button full"
          :disabled="!inspection || generating"
          @click="runNftGeneration"
        >
          {{ generating ? "正在生成，请勿关闭…" : "保存基准并生成下一日表格" }}
        </button>
        <div v-if="generation" class="download-row">
          <a :href="generation.workbook_url">下载新表格</a>
          <a :href="generation.audit_url">下载运营核对说明</a>
        </div>
      </article>
    </section>
  </div>
</template>
