<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

import {
  AUTH_SESSION_ENDING_EVENT,
  ApiRequestError,
  fetchAuthSession,
  fetchAuthStatus,
  fetchFreshness,
  fetchDailyReportReminders,
  fetchRefreshStatus,
  logout,
  refreshStoreData,
  setActiveStoreCode,
  setAuthSession,
  withStoreContext,
  type RefreshStatus,
} from "./api";
import CompetitorsPage from "./pages/CompetitorsPage.vue";
import DailyReportPage from "./pages/DailyReportPage.vue";
import LoginPage from "./pages/LoginPage.vue";
import LogisticsPage from "./pages/LogisticsPage.vue";
import PlatformWarehousePage from "./pages/PlatformWarehousePage.vue";
import KeywordTrafficPage from "./pages/KeywordTrafficPage.vue";
import SearchRankingPage from "./pages/SearchRankingPage.vue";
import OverviewPage from "./pages/OverviewPage.vue";
import ProductsPage from "./pages/ProductsPage.vue";
import QuadrantsPage from "./pages/QuadrantsPage.vue";
import UsersPage from "./pages/UsersPage.vue";
import {
  templateLabels,
  userHasPermission,
} from "./permissions";
import { formatChinaDateTime } from "./time";
import type {
  AuthSession,
  AuthStatus,
  DailyReportReminders,
  FreshnessPayload,
  OwnStoreScope,
  StoreAccessItem,
} from "./types";
import type { PermissionKey } from "./types";

type PageKey =
  | "overview"
  | "products"
  | "keyword-traffic"
  | "search-ranking"
  | "quadrants"
  | "logistics"
  | "platform-warehouse"
  | "competitors"
  | "daily-report"
  | "users";

const storeScopedPages = new Set<PageKey>([
  "overview",
  "products",
  "keyword-traffic",
  "search-ranking",
  "quadrants",
  "logistics",
  "platform-warehouse",
  "daily-report",
]);
const pageStorageKey = "takealot-erp-active-page-v1";
const competitorCheckpointKey = "takealot-competitor-collection-v1";
const competitorClientKey = "takealot-competitor-client-v1";
const competitorCheckpointVersion = 9;
const allStoresSelectorValue = "all-connected-stores";

const basePages = [
  { key: "overview", label: "经营总览", hint: "今日经营脉搏", mark: "01", permission: "store.view" },
  { key: "products", label: "商品中心", hint: "单品销售与流量", mark: "02", permission: "store.view" },
  { key: "keyword-traffic", label: "关键词流量", hint: "变更节点与趋势对比", mark: "03", permission: "store.view" },
  { key: "search-ranking", label: "搜索定位", hint: "图片热词与自然排名", mark: "04", permission: "store.view" },
  { key: "quadrants", label: "经营坐标", hint: "流量与下单分布", mark: "05", permission: "store.view" },
  { key: "logistics", label: "物流管理", hint: "长睿与平台货件", mark: "06", permission: "store.view" },
  { key: "platform-warehouse", label: "约平台仓", hint: "补货草稿与 PO", mark: "07", permission: "store.view" },
  { key: "competitors", label: "竞品雷达", hint: "库存评论与销量", mark: "08", permission: "competitors.view" },
  { key: "daily-report", label: "运营日报", hint: "全周期核对与合并", mark: "09", permission: "daily_report.view" },
] as const;
const adminPage = {
  key: "users",
  label: "用户权限",
  hint: "账号与权限管理",
  mark: "10",
  permission: "users.manage",
} as const;

const authReady = ref(false);
const authStatus = ref<AuthStatus>({ setup_required: false, bootstrap_allowed: false });
const session = ref<AuthSession | null>(null);
const selectedStoreId = ref<number | null>(null);
const overviewStoreScope = ref<OwnStoreScope>("current");
const competitorOwnStoreScope = ref<OwnStoreScope>("current");
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
const permissionNotice = ref("");
const mobileNavOpen = ref(false);
const authError = ref("");
let dailyReportEvents: EventSource | null = null;

const hasPermission = (permission: PermissionKey) =>
  userHasPermission(session.value?.user, permission);
const canManageUsers = computed(() => hasPermission("users.manage"));
const canManageLogistics = computed(() => hasPermission("logistics.manage"));
const canRunSearchRanking = computed(() => hasPermission("search_ranking.run"));
const canRefresh = computed(() => hasPermission("refresh.run"));
const canCollectCompetitors = computed(() => hasPermission("competitors.collect"));
const canControlCompetitorCollection = computed(
  () =>
    canCollectCompetitors.value
    && session.value?.user.username.toLowerCase() === "kxx",
);
const canManageDailyReport = computed(() => hasPermission("daily_report.manage"));
const canGenerateDailyReport = computed(() => hasPermission("daily_report.export"));
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
  if (!refreshStatus.value.admin_exempt && refreshCooldownRemaining.value > 0) {
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
    const suffix = refreshStatus.value.admin_exempt
      ? "；管理员可在必要时再次刷新"
      : `；普通账号还需等待 ${formatCooldown(refreshCooldownRemaining.value)}`;
    return `${owner} 已刷新全部数据${suffix}。`;
  }
  return "";
});
const pages = computed(() =>
  [...basePages, adminPage].filter((page) =>
    hasPermission(page.permission as PermissionKey),
  ),
);
const allPages = [...basePages, adminPage];
const activePage = computed(
  () =>
    pages.value.find((page) => page.key === currentPage.value)
    ?? pages.value[0]
    ?? { key: "overview", label: "暂无可用模块", hint: "", mark: "--" },
);
const pageComponent = computed(() => {
  const components = {
    overview: OverviewPage,
    products: ProductsPage,
    "keyword-traffic": KeywordTrafficPage,
    "search-ranking": SearchRankingPage,
    quadrants: QuadrantsPage,
    logistics: LogisticsPage,
    "platform-warehouse": PlatformWarehousePage,
    competitors: CompetitorsPage,
    "daily-report": DailyReportPage,
    users: UsersPage,
  };
  return components[activePage.value.key as PageKey];
});
const roleLabel = computed(() => {
  const user = session.value?.user;
  if (!user) return "";
  const customized = user.permissions_customized ? " · 自定义权限" : "";
  return `${templateLabels[user.role]}模板${customized}`;
});
const storeScopeLabel = computed(() => {
  const user = session.value?.user;
  if (!user) return "";
  const prefix = user.all_stores ? "全部店铺" : "已分配店铺";
  return `${prefix} · ${user.accessible_stores.length} 个`;
});
const canAccessConnectedStore = computed(() =>
  (session.value?.user.accessible_stores ?? []).some(
    (store) => store.data_connected,
  ),
);
const accessibleConnectedStoreCount = computed(
  () => (session.value?.user.accessible_stores ?? []).filter(
    (store) => store.active && store.data_connected,
  ).length,
);
const selectedStore = computed<StoreAccessItem | null>(() => {
  const stores = session.value?.user.accessible_stores ?? [];
  return (
    stores.find((store) => store.id === selectedStoreId.value)
    ?? stores.find((store) => store.code === "current" && store.data_connected)
    ?? stores[0]
    ?? null
  );
});
const competitorAllStoresSelected = computed(
  () => currentPage.value === "competitors" && competitorOwnStoreScope.value === "all",
);
const overviewAllStoresSelected = computed(
  () =>
    currentPage.value === "overview"
    && overviewStoreScope.value === "all"
    && accessibleConnectedStoreCount.value > 1,
);
const selectedStoreChoice = computed({
  get: () =>
    competitorAllStoresSelected.value || overviewAllStoresSelected.value
      ? allStoresSelectorValue
      : selectedStoreId.value === null
        ? ""
        : String(selectedStoreId.value),
  set: (value: string) => {
    if (value === allStoresSelectorValue) {
      if (currentPage.value === "overview") {
        overviewStoreScope.value = "all";
      } else if (currentPage.value === "competitors") {
        competitorOwnStoreScope.value = "all";
      }
      return;
    }
    if (currentPage.value === "overview") {
      overviewStoreScope.value = "current";
    } else if (currentPage.value === "competitors") {
      competitorOwnStoreScope.value = "current";
    }
    const storeId = Number(value);
    selectedStoreId.value = Number.isFinite(storeId) ? storeId : null;
  },
});

function selectStoreFromOverview(storeCode: string) {
  const store = (session.value?.user.accessible_stores ?? []).find(
    (candidate) => candidate.code === storeCode && candidate.active && candidate.data_connected,
  );
  if (!store) return;
  overviewStoreScope.value = "current";
  selectedStoreId.value = store.id;
}

const selectedStorePending = computed(
  () =>
    storeScopedPages.has(activePage.value.key as PageKey)
    && (
      selectedStore.value === null
      || !selectedStore.value.data_connected
    ),
);

watch(
  () => selectedStore.value?.code,
  (storeCode, previousStoreCode) => {
    setActiveStoreCode(storeCode);
    if (!storeCode || storeCode === previousStoreCode) return;
    refreshKey.value += 1;
    void loadFreshness();
    void loadDailyReportReminders();
    void loadRefreshStatus();
    connectDailyReportEvents();
  },
);
const activePageProps = computed(() => {
  const key = activePage.value.key;
  const common = {
    asOf: key === "daily-report" ? dailyReportAsOf.value : asOf.value,
  };
  if (key === "overview") {
    return {
      ...common,
      currentStoreName: selectedStore.value?.display_name ?? "当前店铺",
      allStoresSelected: overviewAllStoresSelected.value,
    };
  }
  if (key === "competitors") {
    return {
      ...common,
      canOperate: canCollectCompetitors.value,
      canControlCollection: canControlCompetitorCollection.value,
      currentUsername: session.value?.user.username ?? "",
      currentStoreName: selectedStore.value?.display_name ?? "当前店铺",
      accessibleConnectedStoreCount: accessibleConnectedStoreCount.value,
      ownStoreScope: competitorOwnStoreScope.value,
      onPermissionDenied: showPermissionDenied,
    };
  }
  if (key === "logistics" || key === "platform-warehouse") {
    return {
      ...common,
      canManage: canManageLogistics.value,
      onPermissionDenied: showPermissionDenied,
    };
  }
  if (key === "search-ranking") {
    return {
      canOperate: canRunSearchRanking.value,
      onPermissionDenied: showPermissionDenied,
    };
  }
  if (key === "daily-report") {
    return {
      ...common,
      canOperate: canManageDailyReport.value,
      canExport: canGenerateDailyReport.value,
      onPermissionDenied: showPermissionDenied,
    };
  }
  return common;
});

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
  if (permissionNoticeTimer !== null) window.clearTimeout(permissionNoticeTimer);
});

let refreshStatusTimer: number | null = null;
let refreshClockTimer: number | null = null;
let permissionNoticeTimer: number | null = null;

function showPermissionDenied() {
  permissionNotice.value = "当前账号该功能权限未开放";
  if (permissionNoticeTimer !== null) window.clearTimeout(permissionNoticeTimer);
  permissionNoticeTimer = window.setTimeout(() => {
    permissionNotice.value = "";
    permissionNoticeTimer = null;
  }, 4_000);
}

function openPage(page: (typeof allPages)[number]) {
  if (!hasPermission(page.permission as PermissionKey)) {
    showPermissionDenied();
    return;
  }
  switchPage(page.key);
}

async function restoreSession() {
  authError.value = "";
  try {
    acceptSession(await fetchAuthSession());
  } catch (error) {
    setAuthSession(null);
    session.value = null;
    if (!(error instanceof ApiRequestError) || error.status !== 401) {
      authError.value =
        error instanceof Error
          ? error.message
          : "登录信息加载失败，请重新连接经营系统";
    }
    authStatus.value = await fetchAuthStatus().catch(() => ({
      setup_required: false,
      bootstrap_allowed: false,
    }));
  } finally {
    authReady.value = true;
  }
}

async function retryAuthentication() {
  authReady.value = false;
  await restoreSession();
}

function acceptSession(next: AuthSession) {
  session.value = next;
  setAuthSession(next);
  overviewStoreScope.value = "current";
  competitorOwnStoreScope.value = "current";
  const currentSelection = next.user.accessible_stores.find(
    (store) => store.id === selectedStoreId.value,
  );
  const nextStore = (
    currentSelection
    ?? next.user.accessible_stores.find(
      (store) => store.code === "current" && store.data_connected,
    )
    ?? next.user.accessible_stores[0]
    ?? null
  );
  selectedStoreId.value = nextStore?.id ?? null;
  setActiveStoreCode(nextStore?.code);
  const allowedPage = pages.value.find((page) => page.key === currentPage.value);
  const allowedPageNeedsStore = (
    allowedPage
    && storeScopedPages.has(allowedPage.key as PageKey)
  );
  if (
    (!allowedPage || (!canAccessConnectedStore.value && allowedPageNeedsStore))
    && pages.value[0]
  ) {
    const firstUsablePage = pages.value.find(
      (page) =>
        canAccessConnectedStore.value
        || !storeScopedPages.has(page.key as PageKey),
    );
    switchPage((firstUsablePage ?? pages.value[0]).key);
  }
  authReady.value = true;
  void loadFreshness();
  void loadDailyReportReminders();
  void loadRefreshStatus();
  connectDailyReportEvents();
}

function handleExpired() {
  window.dispatchEvent(new CustomEvent(AUTH_SESSION_ENDING_EVENT));
  disconnectDailyReportEvents();
  session.value = null;
  selectedStoreId.value = null;
  overviewStoreScope.value = "current";
  competitorOwnStoreScope.value = "current";
  setAuthSession(null);
  currentPage.value = "overview";
  refreshStatus.value.can_refresh = false;
  void fetchAuthStatus().then((status) => {
    authStatus.value = status;
  });
}

async function signOut() {
  window.dispatchEvent(new CustomEvent(AUTH_SESSION_ENDING_EVENT));
  try {
    await logout();
  } finally {
    handleExpired();
  }
}

async function loadFreshness() {
  if (!canAccessConnectedStore.value) {
    freshness.value = {
      last_collection_at: null,
      latest_metric_date: null,
    };
    return;
  }
  freshness.value = await fetchFreshness().catch(() => ({
    last_collection_at: null,
    latest_metric_date: null,
  }));
}

async function loadDailyReportReminders() {
  if (!hasPermission("daily_report.view") || !canAccessConnectedStore.value) {
    dailyReportReminders.value = { count: 0, dates: [] };
    return;
  }
  dailyReportReminders.value = await fetchDailyReportReminders().catch(() => ({
    count: 0,
    dates: [],
  }));
}

function connectDailyReportEvents() {
  disconnectDailyReportEvents();
  if (
    !session.value
    || !hasPermission("daily_report.view")
    || !canAccessConnectedStore.value
  ) return;
  const source = new EventSource(withStoreContext("/api/erp/daily-report/events"));
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
  if (!session.value || !canAccessConnectedStore.value) return;
  try {
    refreshStatus.value = await fetchRefreshStatus();
  } catch {
    // Keep the last shared status during a short local-service interruption.
  }
}

async function runRefresh() {
  if (!canRefresh.value) {
    showPermissionDenied();
    return;
  }
  if (!refreshStatus.value.can_refresh) return;
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
    let checkpoint = JSON.parse(
      localStorage.getItem(competitorCheckpointKey) ?? "null",
    ) as {
      version?: number;
      running?: boolean;
      stopReason?: string;
      clientId?: string;
    } | null;
    if (checkpoint && checkpoint.version !== competitorCheckpointVersion) {
      localStorage.removeItem(competitorCheckpointKey);
      checkpoint = null;
    }
    const currentClientId = sessionStorage.getItem(competitorClientKey);
    const checkpointBelongsToThisPage =
      (checkpoint?.version ?? 0) >= competitorCheckpointVersion
      && (
        Boolean(currentClientId)
        && checkpoint?.clientId === currentClientId
      );
    if (
      (checkpoint?.version ?? 0) >= 4
      && checkpoint?.running === true
      && !checkpoint?.stopReason
      && checkpointBelongsToThisPage
    ) {
      return "competitors";
    }
    const stored = localStorage.getItem(pageStorageKey);
    const allowed: PageKey[] = [
      "overview",
      "products",
      "quadrants",
      "logistics",
      "competitors",
      "daily-report",
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
  <section v-else-if="authError" class="auth-recovery" role="alert">
    <div class="auth-recovery-card">
      <p>CONNECTION ERROR</p>
      <h1>页面暂时无法加载</h1>
      <span>{{ authError }}</span>
      <small>后台采集和已有数据不会因此中断。</small>
      <button type="button" @click="retryAuthentication">重新连接</button>
    </div>
  </section>
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
          v-for="page in allPages"
          :key="page.key"
          :class="{
            active: currentPage === page.key,
            locked: !hasPermission(page.permission as PermissionKey),
          }"
          @click="openPage(page)"
        >
          <span>{{ page.mark }}</span>
          <div>
            <strong>{{ page.label }}</strong>
            <small>
              {{ page.hint }}
              <em v-if="!hasPermission(page.permission as PermissionKey)">未开放</em>
            </small>
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
          <label v-if="session.user.accessible_stores.length" class="store-context">
            <span>当前查看店铺</span>
            <select v-model="selectedStoreChoice" aria-label="切换当前查看店铺">
              <option
                v-if="['overview', 'competitors'].includes(currentPage) && accessibleConnectedStoreCount > 1"
                :value="allStoresSelectorValue"
              >
                {{
                  currentPage === "overview"
                    ? `全部 ${accessibleConnectedStoreCount} 个店铺`
                    : `全部店铺 · ${accessibleConnectedStoreCount} 店合并`
                }}
              </option>
              <option
                v-for="store in session.user.accessible_stores"
                :key="store.id"
                :value="String(store.id)"
              >
                {{
                  `${store.display_name} · ${
                    store.data_connected
                      ? "已接入"
                      : "待接入"
                  }`
                }}
              </option>
            </select>
          </label>
          <label
            v-if="
              !selectedStorePending
              && !['search-ranking', 'logistics', 'daily-report', 'users'].includes(currentPage)
            "
          >
            <span>数据截止日期</span>
            <input v-model="asOf" type="date" />
          </label>
          <button
            v-if="
              !['logistics', 'users'].includes(currentPage)
              && !selectedStorePending
              && canAccessConnectedStore
              && !competitorAllStoresSelected
              && !overviewAllStoresSelected
            "
            class="refresh-button"
            :disabled="refreshing || (canRefresh && !refreshStatus.can_refresh)"
            @click="runRefresh"
          >
            {{ refreshButtonLabel }}
          </button>
          <div class="account-menu">
            <span>{{ session.user.display_name }}</span>
            <small>{{ roleLabel }}</small>
            <small>{{ storeScopeLabel }}</small>
            <button type="button" @click="signOut">退出</button>
          </div>
        </div>
      </header>

      <p
        v-if="permissionNotice"
        class="global-notice permission-notice"
        role="alert"
        aria-live="assertive"
      >
        {{ permissionNotice }}
      </p>
      <p v-else-if="refreshMessage" class="global-notice">{{ refreshMessage }}</p>
      <p
        v-else-if="refreshStatusNotice"
        class="global-notice refresh-cooldown-notice"
        aria-live="polite"
      >
        {{ refreshStatusNotice }}
      </p>
      <button
        v-if="
          canManageDailyReport
          && dailyReportReminders.count
          && currentPage !== 'daily-report'
          && !selectedStorePending
        "
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
        <div
          v-if="selectedStorePending && currentPage !== 'users'"
          class="store-context-pending"
        >
          <p>{{ selectedStore ? "STORE NOT CONNECTED" : "STORE ACCESS REQUIRED" }}</p>
          <h2>
            {{
              selectedStore
                ? `${selectedStore.display_name} 数据尚未接入`
                : "当前账号尚未获授权访问已接入店铺"
            }}
          </h2>
          <span>
            {{
              selectedStore
                ? "当前页面不会复用其他店铺的数据；管理员完成该店铺凭据和采集任务配置后才会开放。"
                : "经营总览、商品、经营坐标、风险与运营日报属于店铺数据模块；竞品雷达等公共模块仍可按账号已开放的功能权限正常使用。"
            }}
          </span>
          <button
            v-if="canManageUsers"
            type="button"
            @click="switchPage('users')"
          >
            前往用户权限配置
          </button>
        </div>
        <KeepAlive v-else include="CompetitorsPage">
          <component
            v-if="pages.length"
            :is="pageComponent"
            :key="`${currentPage}-${refreshKey}`"
            v-bind="activePageProps"
            @select-store="selectStoreFromOverview"
          />
        </KeepAlive>
        <div v-if="!pages.length" class="state-card">
          当前账号尚未分配任何模块权限，请联系管理员配置。
        </div>
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

.auth-recovery {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
  background: #edf2ec;
}

.auth-recovery-card {
  width: min(520px, 100%);
  padding: 30px;
  border: 1px solid #d8c9a7;
  border-radius: 20px;
  background: #fffdf7;
  box-shadow: 0 18px 50px rgb(39 64 54 / 10%);
}

.auth-recovery-card p {
  margin: 0 0 8px;
  color: #9b6525;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.12em;
}

.auth-recovery-card h1 {
  margin: 0 0 14px;
  color: #243c33;
}

.auth-recovery-card span,
.auth-recovery-card small {
  display: block;
  line-height: 1.7;
}

.auth-recovery-card span {
  color: #684d2d;
}

.auth-recovery-card small {
  margin-top: 8px;
  color: #6d7a74;
}

.auth-recovery-card button {
  margin-top: 22px;
  border: 0;
  border-radius: 12px;
  padding: 11px 18px;
  color: white;
  background: #315f50;
  font-weight: 700;
  cursor: pointer;
}

.erp-sidebar nav button.locked {
  opacity: 0.62;
}

.erp-sidebar nav button.locked:hover {
  opacity: 0.82;
}

.erp-sidebar nav button small em {
  margin-left: 6px;
  color: #d9b26f;
  font-style: normal;
  font-weight: 700;
}

.permission-notice {
  border-color: #d9b26f;
  color: #6b481c;
  background: #fff6dc;
}
.store-context {
  display: grid;
  gap: 4px;
  min-width: 190px;
}
.store-context span {
  color: #6f7f77;
  font-size: 11px;
  font-weight: 700;
}
.store-context select {
  min-height: 38px;
  padding: 0 34px 0 11px;
  border: 1px solid #ccd9d2;
  border-radius: 9px;
  color: #21483a;
  background: #fff;
  font: inherit;
}
.store-context-pending {
  max-width: 720px;
  margin: 48px auto;
  padding: 34px;
  border: 1px solid #d8c9a7;
  border-radius: 20px;
  background: #fffdf7;
  box-shadow: 0 18px 50px rgb(39 64 54 / 8%);
}
.store-context-pending p {
  margin: 0 0 8px;
  color: #9b6525;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.12em;
}
.store-context-pending h2 {
  margin: 0 0 14px;
  color: #243c33;
}
.store-context-pending span {
  display: block;
  color: #684d2d;
  line-height: 1.75;
}
.store-context-pending button {
  margin-top: 22px;
  border: 0;
  border-radius: 10px;
  padding: 10px 16px;
  color: white;
  background: #315f50;
  font-weight: 700;
  cursor: pointer;
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
  .store-context {
    width: 100%;
  }
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
