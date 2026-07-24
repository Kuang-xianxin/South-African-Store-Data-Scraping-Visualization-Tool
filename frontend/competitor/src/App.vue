<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

import {
  fetchAuthSession,
  fetchAuthStatus,
  fetchFreshness,
  logout,
  refreshStoreData,
  setAuthSession,
} from "./api";
import CompetitorsPage from "./pages/CompetitorsPage.vue";
import LoginPage from "./pages/LoginPage.vue";
import OverviewPage from "./pages/OverviewPage.vue";
import ProductsPage from "./pages/ProductsPage.vue";
import QuadrantsPage from "./pages/QuadrantsPage.vue";
import ReportsPage from "./pages/ReportsPage.vue";
import RisksPage from "./pages/RisksPage.vue";
import UsersPage from "./pages/UsersPage.vue";
import { formatChinaDateTime } from "./time";
import type { AuthSession, AuthStatus, FreshnessPayload } from "./types";

type PageKey =
  | "overview"
  | "products"
  | "quadrants"
  | "risks"
  | "competitors"
  | "reports"
  | "users";

const basePages = [
  { key: "overview", label: "经营总览", hint: "今日经营脉搏", mark: "01" },
  { key: "products", label: "商品中心", hint: "单品销售与流量", mark: "02" },
  { key: "quadrants", label: "经营四象限", hint: "商品组合定位", mark: "03" },
  { key: "risks", label: "风险与质量", hint: "异常和数据质量", mark: "04" },
  { key: "competitors", label: "竞品雷达", hint: "库存评论与销量", mark: "05" },
  { key: "reports", label: "报表工作台", hint: "导出与 NFT102", mark: "06" },
] as const;
const adminPage = {
  key: "users",
  label: "用户权限",
  hint: "账号与角色管理",
  mark: "07",
} as const;

const authReady = ref(false);
const authStatus = ref<AuthStatus>({ setup_required: false, bootstrap_allowed: false });
const session = ref<AuthSession | null>(null);
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

const isAdmin = computed(() => session.value?.user.role === "admin");
const canOperate = computed(() =>
  ["operator", "admin"].includes(session.value?.user.role ?? ""),
);
const pages = computed(() => (isAdmin.value ? [...basePages, adminPage] : basePages));
const activePage = computed(
  () => pages.value.find((page) => page.key === currentPage.value) ?? pages.value[0],
);
const pageComponent = computed(() => {
  const components = {
    overview: OverviewPage,
    products: ProductsPage,
    quadrants: QuadrantsPage,
    risks: RisksPage,
    competitors: CompetitorsPage,
    reports: ReportsPage,
    users: UsersPage,
  };
  return components[currentPage.value];
});
const roleLabel = computed(() => {
  const labels = { viewer: "查看员", operator: "运营员", admin: "管理员" };
  return labels[session.value?.user.role ?? "viewer"];
});
const activePageProps = computed(() => ({
  asOf: asOf.value,
  ...(["competitors", "reports"].includes(currentPage.value)
    ? { canOperate: canOperate.value }
    : {}),
}));

onMounted(async () => {
  window.addEventListener("erp-auth-expired", handleExpired);
  await restoreSession();
});
onBeforeUnmount(() => window.removeEventListener("erp-auth-expired", handleExpired));

async function restoreSession() {
  try {
    acceptSession(await fetchAuthSession());
  } catch {
    setAuthSession(null);
    session.value = null;
    authStatus.value = await fetchAuthStatus().catch(() => ({
      setup_required: false,
      bootstrap_allowed: false,
    }));
  } finally {
    authReady.value = true;
  }
}

function acceptSession(next: AuthSession) {
  session.value = next;
  setAuthSession(next);
  authReady.value = true;
  void loadFreshness();
}

function handleExpired() {
  session.value = null;
  setAuthSession(null);
  currentPage.value = "overview";
  void fetchAuthStatus().then((status) => {
    authStatus.value = status;
  });
}

async function signOut() {
  try {
    await logout();
  } finally {
    handleExpired();
  }
}

async function loadFreshness() {
  freshness.value = await fetchFreshness().catch(() => ({
    last_collection_at: null,
    latest_metric_date: null,
  }));
}

async function runRefresh() {
  if (!canOperate.value) return;
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
  <div v-if="!authReady" class="auth-loading">正在连接经营系统…</div>
  <LoginPage
    v-else-if="!session"
    :status="authStatus"
    @authenticated="acceptSession"
  />
  <div v-else class="erp-shell">
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
          <button
            v-if="canOperate"
            class="refresh-button"
            :disabled="refreshing"
            @click="runRefresh"
          >
            {{ refreshing ? "正在刷新…" : "刷新全部数据" }}
          </button>
          <div class="account-menu">
            <span>{{ session.user.display_name }}</span>
            <small>{{ roleLabel }}</small>
            <button type="button" @click="signOut">退出</button>
          </div>
        </div>
      </header>

      <p v-if="refreshMessage" class="global-notice">{{ refreshMessage }}</p>

      <section class="erp-content">
        <KeepAlive include="CompetitorsPage">
          <component
            :is="pageComponent"
            :key="`${currentPage}-${refreshKey}`"
            v-bind="activePageProps"
          />
        </KeepAlive>
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

<style scoped>
.auth-loading {
  min-height: 100vh;
  display: grid;
  place-items: center;
  color: #315245;
  background: #edf2ec;
}
.account-menu {
  display: grid;
  grid-template-columns: auto auto;
  column-gap: 9px;
  align-items: center;
  padding-left: 16px;
  border-left: 1px solid #d8e0dc;
}
.account-menu span {
  color: #173f31;
  font-size: 13px;
  font-weight: 700;
}
.account-menu small {
  color: #7b8982;
  font-size: 11px;
}
.account-menu button {
  grid-column: 2;
  grid-row: 1 / span 2;
  padding: 7px 10px;
  border: 0;
  border-radius: 7px;
  color: #36584b;
  background: #e9f0ec;
  cursor: pointer;
}
@media (max-width: 760px) {
  .account-menu {
    padding-left: 0;
    border-left: 0;
  }
  .account-menu span,
  .account-menu small {
    display: none;
  }
}
</style>
