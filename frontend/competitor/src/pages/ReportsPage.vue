<script setup lang="ts">
import { ref } from "vue";

import { generateNft102, inspectNft102 } from "../api";
import type { NftGeneration, NftInspection } from "../types";

const props = defineProps<{
  canUseNft102?: boolean;
  onPermissionDenied?: () => void;
}>();
const message = ref("");
const nftFile = ref<File | null>(null);
const inspection = ref<NftInspection | null>(null);
const reportDate = ref("");
const inspecting = ref(false);
const generating = ref(false);
const generation = ref<NftGeneration | null>(null);

async function chooseFile(event: Event) {
  if (!props.canUseNft102) {
    props.onPermissionDenied?.();
    (event.target as HTMLInputElement).value = "";
    return;
  }
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
  if (!props.canUseNft102) {
    props.onPermissionDenied?.();
    return;
  }
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

function guardFilePicker(event: MouseEvent) {
  if (props.canUseNft102) return;
  event.preventDefault();
  props.onPermissionDenied?.();
}
</script>

<template>
  <div class="erp-page reports-page">
    <div class="page-intro">
      <div>
        <p class="section-kicker">REPORT WORKSPACE</p>
        <h2>NFT102 日报续写</h2>
      </div>
    </div>
    <p v-if="message" class="global-notice">{{ message }}</p>

    <section class="nft-workspace">
      <article class="erp-panel upload-panel">
        <p class="section-kicker">OPERATOR BASELINE</p>
        <h3>上传运营最终版</h3>
        <p>系统以本次上传文件为唯一基准；原文件不会被覆盖。</p>
        <label class="file-drop">
          <input
            type="file"
            accept=".xlsx"
            @click="guardFilePicker"
            @change="chooseFile"
          />
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
          :disabled="Boolean(props.canUseNft102) && (!inspection || generating)"
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
