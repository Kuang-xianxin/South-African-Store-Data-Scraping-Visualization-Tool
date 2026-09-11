<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref } from "vue";
import SearchRankingPage from "./SearchRankingPage.vue";
import type { OwnStoreScope } from "../types";

const KeywordTrafficPage = defineAsyncComponent(() => import("./KeywordTrafficPage.vue"));
defineProps<{ asOf: string; canOperate: boolean; storeScope?: OwnStoreScope; multiStoreLabel?: string; onPermissionDenied?: (message: string) => void }>();
const initialParams = new URLSearchParams(window.location.hash.replace(/^#/, ""));
const tab = ref<"optimize" | "history">(initialParams.get("module") === "keyword-traffic" || initialParams.get("title_tab") === "history" ? "history" : "optimize");
const historyOpened = ref(tab.value === "history");
const optimizeOpened = ref(tab.value === "optimize");
const selection = ref({ offerId: "", storeCode: "" });
const historyLabel = computed(() => selection.value.offerId ? "修改记录 · 当前商品" : "修改记录");
function readTab() {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  if (params.get("module") === "keyword-traffic" || params.get("title_tab") === "history") setTab("history", false);
}
function setTab(value: "optimize" | "history", updateUrl = true) {
  tab.value = value;
  if (value === "history") historyOpened.value = true;
  else optimizeOpened.value = true;
  if (updateUrl) {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    params.set("module", "search-ranking");
    if (value === "history") params.set("title_tab", "history"); else params.delete("title_tab");
    window.history.replaceState(window.history.state, "", `#${params}`);
  }
}
onMounted(() => { readTab(); window.addEventListener("hashchange", readTab); });
onBeforeUnmount(() => window.removeEventListener("hashchange", readTab));
</script>

<template>
  <div class="title-optimization-page">
    <header class="optimization-header">
      <div><h2>标题优化</h2><p>看清问题，参考竞品，确定修改。</p></div>
      <nav aria-label="标题优化页签">
        <button type="button" :aria-pressed="tab === 'optimize'" @click="setTab('optimize')">优化标题</button>
        <button type="button" :aria-pressed="tab === 'history'" @click="setTab('history')">{{ historyLabel }}</button>
      </nav>
    </header>
    <SearchRankingPage v-if="optimizeOpened" v-show="tab === 'optimize'" :active="tab === 'optimize'" :can-operate="canOperate" :store-scope="storeScope" :multi-store-label="multiStoreLabel" :on-permission-denied="onPermissionDenied" :requested-offer-id="selection.offerId" :requested-store-code="selection.storeCode" @select-product="selection = $event" />
    <KeywordTrafficPage v-if="historyOpened" v-show="tab === 'history'" :active="tab === 'history'" :as-of="asOf" :store-scope="storeScope" :multi-store-label="multiStoreLabel" :requested-offer-id="selection.offerId" :requested-store-code="selection.storeCode" @select-product="selection = $event" />
  </div>
</template>

<style scoped>
.optimization-header{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:20px}.optimization-header h2{margin:0;font-size:24px}.optimization-header p{margin:8px 0 0;color:#6b7c76;font-size:13px}.optimization-header nav{display:flex;gap:4px;padding:4px;background:#eaf0ed;border-radius:11px}.optimization-header button{border:0;padding:10px 16px;border-radius:8px;color:#51675d;background:transparent;cursor:pointer}.optimization-header button[aria-pressed=true]{background:white;color:#215a43;box-shadow:0 1px 4px #15372a12}.optimization-header button:focus-visible{outline:3px solid #207d68;outline-offset:2px}@media(max-width:640px){.optimization-header{align-items:stretch;flex-direction:column;gap:14px}.optimization-header nav{align-self:flex-start}.optimization-header h2{font-size:21px}}
</style>
