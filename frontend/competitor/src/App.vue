<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

import {
  fetchAuthSession,
  fetchAuthStatus,
  fetchFreshness,
  fetchDailyReportReminders,
  fetchRefreshStatus,
  logout,
  refreshStoreData,
  setAuthSession,
  type RefreshStatus,
} from "./api";
import CompetitorsPage from "./pages/CompetitorsPage.vue";
import DailyReportPage from "./pages/DailyReportPage.vue";
import LoginPage from "./pages/LoginPage.vue";
import OverviewPage from "./pages/OverviewPage.vue";
import ProductsPage from "./pages/ProductsPage.vue";
import QuadrantsPage from "./pages/QuadrantsPage.vue";
import ReportsPage from "./pages/ReportsPage.vue";
import RisksPage from "./pages/RisksPage.vue";
import UsersPage from "./pages/UsersPage.vue";
import { formatChinaDateTime } from "./time";
import type {
  AuthSession,
  AuthStatus,
  DailyReportReminders,
  FreshnessPayload,
} from "./types";

type PageKey =
  | "overview"
  | "products"
  | "quadrants"
  | "risks"
  | "competitors"
  | "daily-report"
  | "reports"
  | "users";

const pageStorageKey = "takealot-erp-active-page-v1";
const competitorCheckpointKey = "takealot-competitor-collection-v1";

const basePages = [
  { key: "overview", label: "经营总览", hint: "今日经营脉搏", mark: "01" },
  { key: "products", label: "商品中心", hint: "单品销售与流量", mark: "02" },
  { key: "quadrants", label: "经营四象限", hint: "商品组合定位", mark: "03" },
  { key: "risks", label: "风险与质量", hint: "异常和数据质量", mark: "04" },
  { key: "competitors", label: "竞品雷达", hint: "库存评论与销量", mark: "05" },
  { key: "daily-report", label: "运营日报", hint: "全周期核对与合并", mark: "06" },
  { key: "reports", label: "报表工作台", hint: "导出与 NFT102", mark: "07" },
] as const;
const adminPage = {
  key: "users",
  label: "用户权限",
  hint: "账号与角色管理",
  mark: "08",
} as const;

const authReady = ref(false);
const authStatus = ref<AuthStatus>({ setup_required: false, bootstrap_allowed: false });
const session = ref<AuthSession | null>(null);
const currentPage = ref<PageKey>(initialPage());
const asOf = ref(localDate());
const dailyReportAsOf = ref(currentOperationsBusinessDate());
const freshness = ref<FreshnessPayload>({
  last_collection_at: null,
  latest_metric_date: null,
});
const dailyReportReminders = ref<DailyReportReminders>({ count: 0, dates: [] });
const refreshStatus = ref<RefreshStatus>({
  in_progress: false,
  in_progress_by: null,
  in_progress_display_name: null,
  started_at: null,
  last_success_at: null,
  last_success_by: null,
  last_success_display_name: null,
  cooldown_until: null,
  cooldown_remaining_seconds: 0,
  cooldown_seconds: 3600,
  admin_exempt: false,
  can_refresh: false,
});
const refreshClock = ref(Date.now());
const refreshKey = ref(0);
const refreshing = ref(false);
const refreshMessage = ref("");
const mobileNavOpen = ref(false);
let dailyReportEvents: EventSource | null = null;

const isAdmin = computed(() => session.value?.user.role === "admin");
const canOperate = computed(() =>
  ["operator", "admin"].includes(session.value?.user.role ?? ""),
);
const refreshCooldownRemaining = computed(() => {
  void refreshClock.value;
  if (!refreshStatus.value.cooldown_until) return 0;
  return Math.max(
    0,
    Math.ceil(
      (new Date(refreshStatus.value.cooldown_until).getTime() - Date.now()) / 1000,
    ),
  );
});
const refreshButtonLabel = computed(() => {
  if (refreshing.value) return "正在刷新…";
  if (refreshStatus.value.in_progress) {
    const owner =
      refreshStatus.value.in_progress_display_name
      || refreshStatus.value.in_progress_by
      || "其他用户";
    return `${owner} 正在刷新`;
  }
  if (!isAdmin.value && refreshCooldownRemaining.value > 0) {
    return `刷新冷却 ${formatCooldown(refreshCooldownRemaining.value)}`;
  }
  return "刷新全部数据";
});
const refreshStatusNotice = computed(() => {
  if (refreshStatus.value.in_progress) {
    const owner =
      refreshStatus.value.in_progress_display_name
      || refreshStatus.value.in_progress_by
      || "其他用户";
    return `${owner} 正在刷新全部数据，所有用户将在完成后同步看到最新状态。`;
  }
  if (
    refreshStatus.value.last_success_at
    && refreshCooldownRemaining.value > 0
  ) {
    const owner =
      refreshStatus.value.last_success_display_name
      || refreshStatus.value.last_success_by
      || "其他用户";
    const suffix = isAdmin.value
      ? "；管理员可在必要时再次刷新"
      : `；普通账号还需等待 ${formatCooldown(refreshCooldownRemaining.value)}`;
    return `${owner} 已刷新全部数据${suffix}。`;
  }
  return "";
});
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
    "daily-report": DailyReportPage,
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
  asOf: currentPage.value === "daily-report" ? dailyReportAsOf.value : asOf.value,
  ...(["competitors", "daily-report", "reports"].includes(currentPage.value)
    ? { canOperate: canOperate.value }
    : {}),
}));

onMounted(async () => {
  window.addEventListener("erp-auth-expired", handleExpired);
  await restoreSession();
  refreshStatusTimer = window.setInterval(() => void loadRefreshStatus(), 2_000);
  refreshClockTimer = window.setInterval(() => {
    refreshClock.value = Date.now();
  }, 1_000);
});
onBeforeUnmount(() => {
  window.removeEventListener("erp-auth-expired", handleExpired);
  disconnectDailyReportEvents();
  if (refreshStatusTimer !== null) window.clearInterval(refreshStatusTimer);
  if (refreshClockTimer !== null) window.clearInterval(refreshClockTimer);
});

let refreshStatusTimer: number | null = null;
let refreshClockTimer: number | null = null;

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
  if (currentPage.value === "users" && next.user.role !== "admin") {
    switchPage("overview");
  }
  authReady.value = true;
  void loadFreshness();
  void loadDailyReportReminders();
  void loadRefreshStatus();
  connectDailyReportEvents();
}

function handleExpired() {
  disconnectDailyReportEvents();
  session.value = null;
  setAuthSession(null);
  currentPage.value = "overview";
  refreshStatus.value.can_refresh = false;
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

async function loadDailyReportReminders() {
  dailyReportReminders.value = await fetchDailyReportReminders().catch(() => ({
    count: 0,
    dates: [],
  }));
}

function connectDailyReportEvents() {
  disconnectDailyReportEvents();
  if (!session.value) return;
  const source = new EventSource("/api/erp/daily-report/events");
  const publishUpdate = (event: Event) => {
    const payload = JSON.parse((event as MessageEvent<string>).data) as {
      business_date?: string;
    };
    if (payload.business_date) dailyReportAsOf.value = payload.business_date;
    void loadDailyReportReminders();
    window.dispatchEvent(
      new CustomEvent("erp-daily-report-updated", { detail: payload }),
    );
  };
  source.addEventListener("ready", publishUpdate);
  source.addEventListener("daily-report-updated", publishUpdate);
  dailyReportEvents = source;
}

function disconnectDailyReportEvents() {
  dailyReportEvents?.close();
  dailyReportEvents = null;
}

async function loadRefreshStatus() {
  if (!session.value) return;
  try {
    refreshStatus.value = await fetchRefreshStatus();
  } catch {
    // Keep the last shared status during a short local-service interruption.
  }
}

async function runRefresh() {
  if (!canOperate.value || !refreshStatus.value.can_refresh) return;
  refreshing.value = true;
  refreshMessage.value = "";
  try {
    const result = await refreshStoreData();
    refreshStatus.value = result.refresh_status;
    refreshMessage.value = result.message;
    if (result.succeeded) {
      dailyReportAsOf.value = currentOperationsBusinessDate();
      refreshKey.value += 1;
      await loadFreshness();
      await loadDailyReportReminders();
    }
  } catch (error) {
    refreshMessage.value =
      error instanceof Error ? error.message : "刷新失败，请检查本地日志。";
    await loadRefreshStatus();
  } finally {
    refreshing.value = false;
  }
}

function formatCooldown(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function switchPage(page: PageKey) {
  currentPage.value = page;
  try {
    localStorage.setItem(pageStorageKey, page);
  } catch {
    // Navigation still works when browser storage is unavailable.
  }
  mobileNavOpen.value = false;
}

function initialPage(): PageKey {
  try {
    const checkpoint = JSON.parse(
      localStorage.getItem(competitorCheckpointKey) ?? "null",
    ) as { version?: number; running?: boolean; stopReason?: string } | null;
    if (
      checkpoint?.version === 4
      && checkpoint.running === true
      && !checkpoint.stopReason
    ) {
      return "competitors";
    }
    const stored = localStorage.getItem(pageStorageKey);
    const allowed: PageKey[] = [
      "overview",
      "products",
      "quadrants",
      "risks",
      "competitors",
      "daily-report",
      "reports",
      "users",
    ];
    if (stored && allowed.includes(stored as PageKey)) return stored as PageKey;
  } catch {
    // Fall back to the overview if saved browser state is unavailable or invalid.
  }
  return "overview";
}

function localDate() {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
}

function currentOperationsBusinessDate() {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  const cycleDate = new Date(
    Date.UTC(Number(values.year), Number(values.month) - 1, Number(values.day)),
  );
  if (Number(values.hour) < 10) cycleDate.setUTCDate(cycleDate.getUTCDate() - 1);
  cycleDate.setUTCDate(cycleDate.getUTCDate() - 1);
  return cycleDate.toISOString().slice(0, 10);
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
          <label v-if="currentPage !== 'daily-report'">
            <span>数据截止日期</span>
            <input v-model="asOf" type="date" />
          </label>
          <button
            v-if="canOperate"
            class="refresh-button"
            :disabled="refreshing || !refreshStatus.can_refresh"
            @click="runRefresh"
          >
            {{ refreshButtonLabel }}
          </button>
          <div class="account-menu">
            <span>{{ session.user.display_name }}</span>
            <small>{{ roleLabel }}</small>
            <button type="button" @click="signOut">退出</button>
          </div>
        </div>
      </header>

      <p v-if="refreshMessage" class="global-notice">{{ refreshMessage }}</p>
      <p
        v-else-if="refreshStatusNotice"
        class="global-notice refresh-cooldown-notice"
        aria-live="polite"
      >
        {{ refreshStatusNotice }}
      </p>
      <button
        v-if="dailyReportReminders.count && currentPage !== 'daily-report'"
        class="pending-daily-banner"
        @click="switchPage('daily-report')"
      >
        <strong>有 {{ dailyReportReminders.count }} 个日报数据尚未人工合并</strong>
        <span>
          涉及
          {{ dailyReportReminders.dates.map((row) => row.business_date).join("、") }}
          · 请先处理后再开始今日工作
        </span>
      </button>

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
.pending-daily-banner {
  display: flex;
  align-items: center;
  gap: 14px;
  width: calc(100% - 48px);
  margin: 12px 24px 0;
  padding: 12px 15px;
  border: 1px solid #e1aa82;
  border-left: 5px solid #c95830;
  border-radius: 10px;
  background: #fff5ec;
  color: #873c1d;
  text-align: left;
  cursor: pointer;
}
.pending-daily-banner span {
  color: #9c6447;
  font-size: 12px;
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
