<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

import { fetchFreshness, refreshStoreData } from "./api";
import CompetitorsPage from "./pages/CompetitorsPage.vue";
import OverviewPage from "./pages/OverviewPage.vue";
import ProductsPage from "./pages/ProductsPage.vue";
import QuadrantsPage from "./pages/QuadrantsPage.vue";
import ReportsPage from "./pages/ReportsPage.vue";
import RisksPage from "./pages/RisksPage.vue";
import { formatChinaDateTime } from "./time";
import type { FreshnessPayload } from "./types";

type PageKey =
  | "overview"
  | "products"
  | "quadrants"
  | "risks"
  | "competitors"
  | "reports";

const pages = [
  { key: "overview", label: "经营总览", hint: "今日经营脉搏", mark: "01" },
  { key: "products", label: "商品中心", hint: "单品销售与流量", mark: "02" },
  { key: "quadrants", label: "经营四象限", hint: "商品组合定位", mark: "03" },
  { key: "risks", label: "风险与质量", hint: "异常和数据质量", mark: "04" },
  { key: "competitors", label: "竞品雷达", hint: "库存评论与销量", mark: "05" },
  { key: "reports", label: "报表工作台", hint: "导出与 NFT102", mark: "06" },
] as const;

const currentPage = ref<PageKey>("overview");
const asOf = ref(localDate());
const freshness = ref<FreshnessPayload>({
  last_collection_at: null,
  latest_metric_date: null,
});
const refreshKey = ref(0);
const refreshing = ref(false);
const refreshMessage = ref("");
const mobileNavOpen = ref(false);

const activePage = computed(
  () => pages.find((page) => page.key === currentPage.value) ?? pages[0],
);
const pageComponent = computed(() => {
  const components = {
    overview: OverviewPage,
    products: ProductsPage,
    quadrants: QuadrantsPage,
    risks: RisksPage,
    competitors: CompetitorsPage,
    reports: ReportsPage,
  };
  return components[currentPage.value];
});

onMounted(loadFreshness);

async function loadFreshness() {
  freshness.value = await fetchFreshness().catch(() => ({
    last_collection_at: null,
    latest_metric_date: null,
  }));
}

async function runRefresh() {
  refreshing.value = true;
  refreshMessage.value = "";
  try {
    const result = await refreshStoreData();
    refreshMessage.value = result.message;
    if (result.succeeded) {
      refreshKey.value += 1;
      await loadFreshness();
    }
  } catch (error) {
    refreshMessage.value =
      error instanceof Error ? error.message : "刷新失败，请检查本地日志。";
  } finally {
    refreshing.value = false;
  }
}

function switchPage(page: PageKey) {
  currentPage.value = page;
  mobileNavOpen.value = false;
}

function localDate() {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
}
</script>

<template>
  <div class="erp-shell">
    <aside class="erp-sidebar" :class="{ open: mobileNavOpen }">
      <div class="brand">
        <span class="brand-mark">T</span>
        <div>
          <strong>南非运营 ERP</strong>
          <small>TAKEALOT OPERATIONS</small>
        </div>
      </div>

      <nav aria-label="ERP 主导航">
        <button
          v-for="page in pages"
          :key="page.key"
          :class="{ active: currentPage === page.key }"
          @click="switchPage(page.key)"
        >
          <span>{{ page.mark }}</span>
          <div>
            <strong>{{ page.label }}</strong>
            <small>{{ page.hint }}</small>
          </div>
        </button>
      </nav>

      <div class="sidebar-status">
        <p>数据状态</p>
        <span><i></i> 本机 MySQL 已连接</span>
        <small>
          最近采集 {{ formatChinaDateTime(freshness.last_collection_at, "暂无") }}
          · 北京时间
        </small>
        <small>最新指标 {{ freshness.latest_metric_date || "暂无" }}</small>
      </div>
    </aside>

    <div class="erp-main">
      <header class="erp-topbar">
        <button
          class="mobile-menu"
          aria-label="打开导航"
          @click="mobileNavOpen = !mobileNavOpen"
        >
          菜单
        </button>
        <div class="page-identity">
          <p>{{ activePage.mark }} / OPERATIONS</p>
          <h1>{{ activePage.label }}</h1>
        </div>
        <div class="topbar-actions">
          <label>
            <span>数据截止日期</span>
            <input v-model="asOf" type="date" />
          </label>
          <button class="refresh-button" :disabled="refreshing" @click="runRefresh">
            {{ refreshing ? "正在刷新…" : "刷新全部数据" }}
          </button>
        </div>
      </header>

      <p v-if="refreshMessage" class="global-notice">{{ refreshMessage }}</p>

      <section class="erp-content">
        <component
          :is="pageComponent"
          :key="`${currentPage}-${refreshKey}`"
          :as-of="asOf"
        />
      </section>
    </div>
    <button
      v-if="mobileNavOpen"
      class="nav-backdrop"
      aria-label="关闭导航"
      @click="mobileNavOpen = false"
    ></button>
  </div>
</template>
