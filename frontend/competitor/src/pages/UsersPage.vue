<script setup lang="ts">
import { computed, onBeforeUnmount, onDeactivated, ref } from "vue";
import { useLiveUpdates } from "../liveUpdates";
import { copyUserLogin, ERP_PUBLIC_LOGIN_URL, formatUserLogin } from "../userLoginCopy";

import {
  createStore,
  createUser,
  fetchStores,
  fetchUsers,
  updateStore,
  updateUser,
} from "../api";
import {
  permissionGroups,
  permissionLabels,
  templateLabels,
  templatePermissions,
  togglePermission,
} from "../permissions";
import type {
  ManagedStore,
  ManagedUser,
  PermissionKey,
  UserRole,
} from "../types";

useLiveUpdates("users", async () => {
  await load(true); return !error.value;
}, {
  busy: () => loading.value || saving.value || savingStore.value || busyUserId.value !== null || busyStoreId.value !== null || copyingUserId.value !== null,
  editing: () => Boolean(username.value || displayName.value || password.value || storeCode.value || storeDisplayName.value || copyTarget.value),
});

const users = ref<ManagedUser[]>([]);
const stores = ref<ManagedStore[]>([]);
const loading = ref(true);
const saving = ref(false);
const savingStore = ref(false);
const busyUserId = ref<number | null>(null);
const busyStoreId = ref<number | null>(null);
const notice = ref("");
const error = ref("");
const username = ref("");
const displayName = ref("");
const password = ref("");
const role = ref<UserRole>("viewer");
const createAllStores = ref(false);
const createStoreIds = ref<number[]>([]);
const storeCode = ref("");
const storeDisplayName = ref("");
const loginPasswords = new Map<number, { password: string; updatedAt: string }>();
const copyingUserId = ref<number | null>(null);
const copiedUserId = ref<number | null>(null);
const copyDialog = ref<HTMLDialogElement | null>(null);
const copyTarget = ref<ManagedUser | null>(null);
const copyPassword = ref("");
const copyMode = ref<"current" | "reset">("current");
const copyError = ref("");
const manualCopyText = ref("");
let copiedTimer: ReturnType<typeof setTimeout> | undefined;
let copyGeneration = 0;

function clearLoginPasswords() {
  copyGeneration += 1;
  loginPasswords.clear();
  clearTimeout(copiedTimer);
  copiedUserId.value = null;
  copyDialog.value?.close();
  closeCopyDialog();
}
onBeforeUnmount(clearLoginPasswords);
onDeactivated(clearLoginPasswords);

const roleDescriptions: Record<UserRole, string> = {
  viewer: "仅查看数据与报表。",
  operator: "日常运营与采集；全店刷新仅限 kxx。",
  selection: "查看与采集竞品。",
  admin: "管理系统；全店刷新仅限 kxx。",
};

const templateCards = (Object.keys(templateLabels) as UserRole[]).map((key) => ({
  role: key,
  title: templateLabels[key],
  permissions: templatePermissions[key].map((permission) => permissionLabels[permission]),
}));

const selectedRoleDescription = computed(() => roleDescriptions[role.value]);
const activeStores = computed(() => stores.value.filter((store) => store.active));

void load();

async function load(background: unknown = false) {
  const preserve = background === true;
  loading.value = !preserve;
  error.value = "";
  try {
    const [loadedUsers, loadedStores] = await Promise.all([
      fetchUsers(),
      fetchStores(),
    ]);
    users.value = loadedUsers;
    for (const [id, saved] of loginPasswords) {
      const user = loadedUsers.find((item) => item.id === id);
      if (!user?.active || user.updated_at !== saved.updatedAt) loginPasswords.delete(id);
    }
    stores.value = loadedStores;
    if (!preserve && !createStoreIds.value.length) {
      const current = loadedStores.find(
        (store) => store.active && store.data_connected,
      );
      if (current) createStoreIds.value = [current.id];
    }
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "用户列表读取失败";
  } finally {
    loading.value = false;
  }
}

async function submit() {
  if (saving.value) return;
  saving.value = true;
  notice.value = "";
  error.value = "";
  const submittedPassword = password.value;
  const generation = copyGeneration;
  try {
    const created = await createUser({
      username: username.value,
      display_name: displayName.value,
      password: submittedPassword,
      role: role.value,
      all_stores: createAllStores.value,
      store_ids: createStoreIds.value,
    });
    users.value = [...users.value, created];
    if (generation === copyGeneration) {
      loginPasswords.set(created.id, { password: submittedPassword, updatedAt: created.updated_at });
    }
    username.value = "";
    displayName.value = "";
    password.value = "";
    role.value = "viewer";
    createAllStores.value = false;
    const current = stores.value.find(
      (store) => store.active && store.data_connected,
    );
    createStoreIds.value = current ? [current.id] : [];
    notice.value = "账号已创建，可在该账号旁一键复制登录信息。";
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "账号创建失败";
  } finally {
    saving.value = false;
  }
}

function handleCreateRoleChange() {
  if (role.value === "admin") createAllStores.value = true;
}

async function submitStore() {
  if (savingStore.value) return;
  savingStore.value = true;
  notice.value = "";
  error.value = "";
  try {
    const created = await createStore({
      code: storeCode.value,
      display_name: storeDisplayName.value,
    });
    stores.value = [...stores.value, created];
    storeCode.value = "";
    storeDisplayName.value = "";
    notice.value = `已预留店铺“${created.display_name}”；接入数据后即可按账号范围使用。`;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "店铺预留失败";
  } finally {
    savingStore.value = false;
  }
}

async function renameStore(store: ManagedStore) {
  const next = window.prompt("输入新的店铺显示名称", store.display_name);
  if (!next || next.trim() === store.display_name) return;
  await saveStore(
    store,
    { display_name: next },
    `店铺名称已更新为“${next.trim()}”。`,
  );
}

async function toggleStoreActive(store: ManagedStore) {
  await saveStore(
    store,
    { active: !store.active },
    `店铺“${store.display_name}”已${store.active ? "停用" : "启用"}。`,
  );
}

async function saveStore(
  store: ManagedStore,
  change: { display_name?: string; active?: boolean },
  successMessage: string,
) {
  if (busyStoreId.value !== null) return;
  busyStoreId.value = store.id;
  notice.value = "";
  error.value = "";
  try {
    const updated = await updateStore(store.id, change);
    stores.value = stores.value.map((item) =>
      item.id === updated.id ? updated : item,
    );
    notice.value = successMessage;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "店铺更新失败";
  } finally {
    busyStoreId.value = null;
  }
}

function storeAssignmentSummary(store: ManagedStore) {
  const operatingAccounts = users.value.filter(
    (user) =>
      user.active
      && user.assigned_store_ids.includes(store.id),
  ).length;
  const allStoreAccounts = users.value.filter(
    (user) => user.active && user.all_stores,
  ).length;
  return `${operatingAccounts} 个账号负责运营 · ${allStoreAccounts} 个全店账号可查看`;
}

async function applyTemplate(user: ManagedUser, nextRole: UserRole) {
  await saveUser(
    user,
    {
      role: nextRole,
      permissions: [...templatePermissions[nextRole]],
    },
    `已为 ${user.display_name} 套用“${templateLabels[nextRole]}”模板。`,
  );
}

async function changeAllStores(user: ManagedUser, enabled: boolean) {
  await saveUser(
    user,
    {
      all_stores: enabled,
      store_ids: [...user.assigned_store_ids],
    },
    enabled
      ? `已为 ${user.display_name} 开启全部店铺；运营店铺勾选保持不变。`
      : `已把 ${user.display_name} 改为仅查看并运营勾选店铺。`,
  );
}

async function changeUserStore(
  user: ManagedUser,
  storeId: number,
  enabled: boolean,
) {
  const next = enabled
    ? [...new Set([...user.assigned_store_ids, storeId])]
    : user.assigned_store_ids.filter((id) => id !== storeId);
  await saveUser(
    user,
    { all_stores: user.all_stores, store_ids: next },
    `已更新 ${user.display_name} 的运营店铺；该账号需重新登录后生效。`,
  );
}

async function changePermission(
  user: ManagedUser,
  permission: PermissionKey,
  enabled: boolean,
) {
  const next = togglePermission(user.permissions, permission, enabled);
  await saveUser(
    user,
    { permissions: next },
    `已更新 ${user.display_name} 的账号权限；该账号需重新登录后生效。`,
  );
}

async function resetPassword(user: ManagedUser) {
  const next = window.prompt(`为 ${user.display_name} 输入新密码（至少8个字符）`);
  if (!next) return;
  await saveUser(
    user,
    { password: next },
    `${user.display_name} 的密码已重置，原会话已失效。`,
  );
}

async function toggleActive(user: ManagedUser) {
  await saveUser(
    user,
    { active: !user.active },
    `${user.display_name} 已${user.active ? "停用" : "启用"}。`,
  );
}

async function saveUser(
  user: ManagedUser,
  change: {
    role?: UserRole;
    permissions?: PermissionKey[];
    all_stores?: boolean;
    store_ids?: number[];
    active?: boolean;
    password?: string;
  },
  successMessage: string,
) {
  if (busyUserId.value !== null) return;
  const generation = copyGeneration;
  busyUserId.value = user.id;
  notice.value = "";
  error.value = "";
  try {
    const updated = await updateUser(user.id, change);
    users.value = users.value.map((item) =>
      item.id === updated.id ? updated : item,
    );
    const savedPassword = change.password ?? loginPasswords.get(user.id)?.password;
    if (savedPassword && updated.active && generation === copyGeneration) {
      loginPasswords.set(updated.id, { password: savedPassword, updatedAt: updated.updated_at });
    } else {
      loginPasswords.delete(user.id);
    }
    notice.value = successMessage;
    return updated;
  } catch (reason) {
    // An uncertain update must never leave an older password ready to forward.
    loginPasswords.delete(user.id);
    error.value = reason instanceof Error ? reason.message : "账号更新失败";
  } finally {
    busyUserId.value = null;
  }
}

function openCopyDialog(user: ManagedUser, knownPassword = "") {
  copyTarget.value = user;
  copyPassword.value = knownPassword;
  copyMode.value = "current";
  copyError.value = "";
  manualCopyText.value = "";
  copyDialog.value?.showModal();
}

function closeCopyDialog() {
  copyTarget.value = null;
  copyPassword.value = "";
  copyError.value = "";
  manualCopyText.value = "";
}

async function copyLogin(user: ManagedUser, suppliedPassword?: string) {
  if (!user.active || copyingUserId.value !== null || busyUserId.value !== null) return;
  const saved = loginPasswords.get(user.id);
  const knownPassword = suppliedPassword ?? (saved?.updatedAt === user.updated_at ? saved.password : "");
  if (!knownPassword) {
    openCopyDialog(user);
    return;
  }
  copyingUserId.value = user.id;
  const generation = copyGeneration;
  copiedUserId.value = null;
  copyError.value = "";
  const text = formatUserLogin(user.username, knownPassword);
  try {
    await copyUserLogin(text);
    if (generation !== copyGeneration) return;
    loginPasswords.set(user.id, { password: knownPassword, updatedAt: user.updated_at });
    copyDialog.value?.close();
    closeCopyDialog();
    copiedUserId.value = user.id;
    notice.value = `${user.display_name} 的账号、密码和公网地址已复制。`;
    clearTimeout(copiedTimer);
    copiedTimer = setTimeout(() => { copiedUserId.value = null; }, 3000);
  } catch {
    if (generation !== copyGeneration) return;
    if (!copyDialog.value?.open) openCopyDialog(user, knownPassword);
    manualCopyText.value = text;
    copyError.value = "复制失败，请重试或手动复制下方内容。";
  } finally {
    copyingUserId.value = null;
  }
}

async function submitLoginCopy() {
  let user = copyTarget.value;
  const value = copyPassword.value;
  const generation = copyGeneration;
  if (!user || !value || copyingUserId.value !== null || busyUserId.value !== null) return;
  copyError.value = "";
  if (copyMode.value === "reset") {
    const updated = await saveUser(user, { password: value }, `${user.display_name} 的密码已重置，原会话已失效。`);
    if (generation !== copyGeneration) return;
    if (!updated) {
      copyError.value = error.value || "密码重置未完成";
      return;
    }
    user = updated;
    copyTarget.value = updated;
    // Retrying a denied clipboard write must never submit another password reset.
    copyMode.value = "current";
  }
  await copyLogin(user, value);
}

function formatDate(value: string | null) {
  if (!value) return "从未登录";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}
</script>

<template>
  <div class="erp-page users-page">
    <section class="erp-panel permission-overview">
      <div class="section-title">
        <div>
          <p class="section-kicker">PERMISSION TEMPLATES</p>
          <h2>权限模板一览</h2>
        </div>
      </div>

      <div class="template-grid">
        <article v-for="card in templateCards" :key="card.role">
          <header>
            <strong>{{ card.title }}</strong>
            <small>{{ card.role }}</small>
          </header>
          <ul>
            <li v-for="item in card.permissions" :key="item">{{ item }}</li>
          </ul>
        </article>
      </div>
    </section>

    <section class="erp-panel store-panel">
      <div class="section-title">
        <div>
          <p class="section-kicker">STORE ACCESS DIRECTORY</p>
          <h2>店铺权限目录</h2>
        </div>
      </div>

      <form class="store-create-form" @submit.prevent="submitStore">
        <label>
          店铺代码
          <input
            v-model="storeCode"
            required
            maxlength="64"
            placeholder="例如 shop-02"
          />
        </label>
        <label>
          店铺名称
          <input
            v-model="storeDisplayName"
            required
            maxlength="100"
            placeholder="运营可识别的名称"
          />
        </label>
        <button class="action-button" :disabled="savingStore">
          {{ savingStore ? "正在添加…" : "预留店铺" }}
        </button>
      </form>

      <div class="store-registry-grid">
        <article
          v-for="store in stores"
          :key="store.id"
          class="store-registry-card"
          :class="{ inactive: !store.active }"
        >
          <header>
            <div>
              <strong>{{ store.display_name }}</strong>
              <code>{{ store.code }}</code>
            </div>
            <span
              :class="{
                connected: store.data_connected,
                inactive: !store.active,
              }"
            >
              {{
                !store.active
                  ? "已停用"
                  : store.data_connected
                    ? "已接入当前数据"
                    : "已预留 · 待接入"
              }}
            </span>
          </header>
          <p>{{ storeAssignmentSummary(store) }}</p>
          <div class="store-card-actions">
            <button
              type="button"
              :disabled="busyStoreId !== null"
              @click="renameStore(store)"
            >
              修改名称
            </button>
            <button
              v-if="!store.data_connected"
              type="button"
              :class="{ danger: store.active }"
              :disabled="busyStoreId !== null"
              @click="toggleStoreActive(store)"
            >
              {{ store.active ? "停用预留" : "重新启用" }}
            </button>
          </div>
        </article>
      </div>
    </section>

    <section class="erp-panel create-panel">
      <div class="section-title">
        <div>
          <p class="section-kicker">CREATE ACCOUNT</p>
          <h2>新建账号</h2>
        </div>
      </div>
      <form class="create-account-form" @submit.prevent="submit">
        <label>
          用户名
          <input v-model="username" required minlength="3" maxlength="64" />
        </label>
        <label>
          显示名称
          <input v-model="displayName" maxlength="100" />
        </label>
        <label>
          初始密码
          <input v-model="password" required minlength="8" maxlength="128" type="password" />
        </label>
        <label>
          权限模板
          <select
            v-model="role"
            aria-label="新账号权限模板"
            @change="handleCreateRoleChange"
          >
            <option
              v-for="(label, key) in templateLabels"
              :key="key"
              :value="key"
            >
              {{ label }}
            </option>
          </select>
        </label>
        <button class="action-button" :disabled="saving">
          {{ saving ? "正在创建…" : "创建账号" }}
        </button>
        <fieldset class="create-store-scope">
          <legend>店铺查看与运营范围</legend>
          <label class="store-all-option">
            <input v-model="createAllStores" type="checkbox" />
            <span>
              <strong>全部店铺（含未来新增）</strong>
            </span>
          </label>
          <div class="permission-heading create-operating-heading">
            <div>
              <strong>运营店铺授权（可多选）</strong>
            </div>
            <small>已选 {{ createStoreIds.length }} 个</small>
          </div>
          <div class="store-checkbox-grid">
            <label
              v-for="store in activeStores"
              :key="store.id"
              class="store-option"
            >
              <input
                v-model="createStoreIds"
                type="checkbox"
                :value="store.id"
              />
              <span>
                {{ store.display_name }}
                <small>{{ store.data_connected ? "已接入" : "待接入" }}</small>
              </span>
            </label>
            <p v-if="!activeStores.length">尚无可分配店铺，请先在上方预留。</p>
          </div>
        </fieldset>
        <p class="selected-template-help" aria-live="polite">
          <strong>{{ templateLabels[role] }}：</strong>{{ selectedRoleDescription }}
        </p>
      </form>
    </section>

    <p v-if="notice" class="user-notice success" role="status">{{ notice }}</p>
    <p v-if="error" class="user-notice error" role="alert">{{ error }}</p>

    <section class="erp-panel accounts-panel">
      <div class="section-title">
        <div>
          <p class="section-kicker">EXISTING ACCOUNTS</p>
          <h2>现有账号</h2>
        </div>
        <span>{{ users.length }} 个账号 · 修改后重新登录生效</span>
      </div>

      <div v-if="loading" class="state-card">正在读取用户列表…</div>
      <div v-else class="account-list">
        <article
          v-for="user in users"
          :key="user.id"
          class="account-card"
          :class="{ inactive: !user.active }"
        >
          <header class="account-header">
            <div class="account-identity">
              <strong>{{ user.display_name }}</strong>
              <span>@{{ user.username }}</span>
              <small>
                最近登录：{{ formatDate(user.last_login_at) }}
              </small>
            </div>
            <span class="account-status" :class="{ off: !user.active }">
              {{ user.active ? "启用中" : "已停用" }}
            </span>
          </header>

          <div class="account-toolbar">
            <label>
              权限模板
              <select
                :value="user.role"
                :disabled="busyUserId !== null"
                :aria-label="`${user.display_name}的权限模板`"
                @change="
                  applyTemplate(
                    user,
                    ($event.target as HTMLSelectElement).value as UserRole,
                  )
                "
              >
                <option
                  v-for="(label, key) in templateLabels"
                  :key="key"
                  :value="key"
                >
                  {{ label }}
                </option>
              </select>
            </label>
            <div class="template-state">
              <small>当前状态</small>
              <strong>
                {{ user.permissions_customized ? "已单独调整" : "跟随模板" }}
              </strong>
              <span>套用会覆盖自定义权限</span>
            </div>
            <div class="account-actions">
              <button
                type="button"
                class="copy-login-button"
                :disabled="busyUserId !== null || copyingUserId !== null || !user.active"
                :aria-label="`一键复制 ${user.display_name} 的登录信息`"
                @click="copyLogin(user)"
              >
                {{ copyingUserId === user.id ? "正在复制…" : copiedUserId === user.id ? "已复制" : "一键复制" }}
              </button>
              <button
                type="button"
                :disabled="busyUserId !== null"
                @click="resetPassword(user)"
              >
                重置密码
              </button>
              <button
                type="button"
                :class="{ danger: user.active }"
                :disabled="busyUserId !== null"
                @click="toggleActive(user)"
              >
                {{ user.active ? "停用账号" : "启用账号" }}
              </button>
            </div>
          </div>

          <div class="account-store-access">
            <div class="permission-heading">
              <div>
                <strong>账号店铺查看范围</strong>
              </div>
              <small>
                {{
                  user.all_stores
                    ? `可查看全部 ${user.accessible_stores.length} 个启用店铺`
                    : `可查看 ${user.assigned_store_ids.length} 个运营店铺`
                }}
              </small>
            </div>
            <label class="store-all-option">
              <input
                type="checkbox"
                :checked="user.all_stores"
                :disabled="busyUserId !== null || !user.active"
                @change="
                  changeAllStores(
                    user,
                    ($event.target as HTMLInputElement).checked,
                  )
                "
              />
              <span>
                <strong>全部店铺（含未来新增）</strong>
                <small>仅扩大查看范围</small>
              </span>
            </label>
            <div class="permission-heading operating-store-heading">
              <div>
                <strong>运营店铺授权（可多选）</strong>
              </div>
              <small>负责 {{ user.assigned_store_ids.length }} 个店铺</small>
            </div>
            <div class="store-checkbox-grid">
              <label
                v-for="store in stores"
                :key="store.id"
                class="store-option"
                :class="{ inactive: !store.active }"
              >
                <input
                  type="checkbox"
                  :checked="user.assigned_store_ids.includes(store.id)"
                  :disabled="
                    busyUserId !== null
                    || !user.active
                    || !store.active
                  "
                  @change="
                    changeUserStore(
                      user,
                      store.id,
                      ($event.target as HTMLInputElement).checked,
                    )
                  "
                />
                <span>
                  {{ store.display_name }}
                  <small>
                    {{
                      !store.active
                        ? "已停用"
                        : store.data_connected
                          ? "已接入"
                          : "待接入"
                    }}
                  </small>
                </span>
              </label>
              <p v-if="!stores.length">尚无店铺；该账号暂时不能分配运营店铺。</p>
            </div>
          </div>

          <div class="account-permissions">
            <div class="permission-heading">
              <div>
                <strong>账号独立权限</strong>
                <span>勾选即保存，并开启所需查看权限</span>
              </div>
              <small>{{ user.permissions.length }} 项已开启</small>
            </div>
            <div class="permission-group-grid">
              <fieldset v-for="group in permissionGroups" :key="group.title">
                <legend>{{ group.title }}</legend>
                <label
                  v-for="permission in group.permissions"
                  :key="permission"
                  class="permission-option"
                >
                  <input
                    type="checkbox"
                    :checked="user.permissions.includes(permission)"
                    :disabled="busyUserId !== null || !user.active"
                    @change="
                      changePermission(
                        user,
                        permission,
                        ($event.target as HTMLInputElement).checked,
                      )
                    "
                  />
                  <span>{{ permissionLabels[permission] }}</span>
                </label>
              </fieldset>
            </div>
          </div>
        </article>
      </div>
    </section>
    <dialog ref="copyDialog" class="login-copy-dialog" aria-labelledby="login-copy-title" @close="closeCopyDialog" @cancel="copyingUserId !== null || busyUserId !== null ? $event.preventDefault() : undefined">
      <form @submit.prevent="submitLoginCopy">
        <h2 id="login-copy-title">复制登录信息</h2>
        <p>{{ copyTarget?.display_name }} · {{ copyTarget?.username }}</p>
        <p class="login-copy-url">系统地址：{{ ERP_PUBLIC_LOGIN_URL }}</p>
        <label>
          密码来源
          <select v-model="copyMode" :disabled="busyUserId !== null || copyingUserId !== null" @change="copyPassword = ''; copyError = ''; manualCopyText = ''">
            <option value="current">填写当前密码</option>
            <option value="reset">设置新密码</option>
          </select>
        </label>
        <p v-if="copyMode === 'current'" class="login-copy-hint">原密码无法读取，请填写当前密码；新建或重置后可直接复制。</p>
        <p v-else class="login-copy-warning">确认后旧密码失效，该账号需重新登录。</p>
        <label>
          {{ copyMode === 'reset' ? "新密码" : "当前密码" }}
          <input v-model="copyPassword" type="password" autocomplete="new-password" required :minlength="copyMode === 'reset' ? 8 : undefined" maxlength="128" :disabled="busyUserId !== null || copyingUserId !== null" @input="manualCopyText = ''; copyError = ''" />
        </label>
        <p v-if="copyError" class="login-copy-warning" role="alert">{{ copyError }}</p>
        <textarea v-if="manualCopyText" :value="manualCopyText" readonly rows="5" aria-label="待复制的登录信息" @focus="($event.target as HTMLTextAreaElement).select()" />
        <div class="login-copy-actions">
          <button type="button" :disabled="busyUserId !== null || copyingUserId !== null" @click="copyDialog?.close()">取消</button>
          <button type="submit" class="primary-button" :disabled="!copyPassword || busyUserId !== null || copyingUserId !== null">
            {{ busyUserId !== null ? "正在重置…" : copyingUserId !== null ? "正在复制…" : copyMode === "reset" ? "重置并复制" : "复制登录信息" }}
          </button>
        </div>
      </form>
    </dialog>
  </div>
</template>

<style scoped>
.users-page {
  min-width: 0;
  display: grid;
  gap: 22px;
}
.erp-panel {
  min-width: 0;
  padding: 26px;
  overflow: hidden;
}
.section-title {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 20px;
}
.section-title > div {
  min-width: 0;
}
.section-title h2 {
  margin: 2px 0 0;
  color: #173f31;
  font-size: 22px;
}
.section-title > span {
  max-width: 520px;
  color: #76867e;
  font-size: 11px;
  line-height: 1.6;
  text-align: right;
}
.template-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}
.template-grid article {
  min-width: 0;
  padding: 18px;
  border: 1px solid #dce5e0;
  border-radius: 12px;
  background: #fbfcfb;
}
.template-grid header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}
.template-grid header strong {
  color: #173f31;
  font-size: 17px;
}
.template-grid header small {
  color: #8c9892;
  font-size: 9px;
}
.template-grid article > p {
  min-height: 54px;
  margin: 12px 0;
  color: #5d7067;
  font-size: 11px;
  line-height: 1.6;
}
.template-grid ul {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.template-grid li {
  padding: 4px 7px;
  border-radius: 6px;
  color: #52675d;
  background: #eef3f0;
  font-size: 9px;
}
.store-scope-guidance {
  display: grid;
  gap: 4px;
  margin-bottom: 16px;
  padding: 12px 14px;
  border-left: 3px solid #2c7654;
  border-radius: 8px;
  color: #567067;
  background: #f1f6f3;
  font-size: 11px;
  line-height: 1.55;
}
.store-scope-guidance strong {
  color: #245740;
  font-size: 12px;
}
.store-create-form {
  display: grid;
  grid-template-columns: minmax(160px, 0.7fr) minmax(240px, 1.3fr) auto;
  gap: 14px;
  align-items: end;
}
.store-registry-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 280px), 1fr));
  gap: 12px;
  margin-top: 18px;
}
.store-registry-card {
  padding: 15px;
  border: 1px solid #dbe5df;
  border-radius: 11px;
  background: #fbfcfb;
}
.store-registry-card.inactive {
  opacity: 0.68;
}
.store-registry-card header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.store-registry-card header > div {
  min-width: 0;
  display: grid;
  gap: 3px;
}
.store-registry-card strong {
  color: #214d39;
  font-size: 13px;
}
.store-registry-card code {
  overflow-wrap: anywhere;
  color: #788980;
  font-size: 9px;
}
.store-registry-card header > span {
  flex: 0 0 auto;
  padding: 4px 7px;
  border-radius: 999px;
  color: #765f32;
  background: #f4eddc;
  font-size: 9px;
  font-weight: 700;
}
.store-registry-card header > span.connected {
  color: #256744;
  background: #e4f2e9;
}
.store-registry-card header > span.inactive {
  color: #87564e;
  background: #f3e7e4;
}
.store-registry-card > p {
  margin: 12px 0;
  color: #75867d;
  font-size: 9px;
  line-height: 1.5;
}
.store-card-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}
.store-card-actions button {
  padding: 6px 9px;
  border: 1px solid #ccd9d2;
  border-radius: 7px;
  color: #365b49;
  background: white;
  font-size: 10px;
}
.store-card-actions button.danger {
  color: #a8483f;
  border-color: #e6bbb6;
}
.create-account-form {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr)) auto;
  gap: 14px;
  align-items: end;
}
.create-account-form > label,
.store-create-form > label,
.account-toolbar > label {
  min-width: 0;
  display: grid;
  gap: 7px;
  color: #53655d;
  font-size: 11px;
  font-weight: 700;
}
.create-store-scope {
  grid-column: 1 / -1;
}
.store-all-option {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  color: #405f51;
  cursor: pointer;
}
.store-all-option input,
.store-option input {
  flex: 0 0 auto;
  width: 16px;
  min-height: 16px;
  margin: 1px 0 0;
  padding: 0;
  accent-color: #24704e;
}
.store-all-option > span,
.store-option > span {
  min-width: 0;
  display: grid;
  gap: 2px;
}
.store-all-option strong {
  color: #285640;
  font-size: 11px;
}
.store-all-option small,
.store-option small {
  color: #849189;
  font-size: 9px;
  font-weight: 400;
}
.store-checkbox-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 8px;
  margin-top: 12px;
}
.store-option {
  min-width: 0;
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 9px 10px;
  border: 1px solid #dee7e2;
  border-radius: 8px;
  color: #486457;
  background: #fbfcfb;
  font-size: 10px;
  line-height: 1.4;
  cursor: pointer;
}
.store-option.inactive {
  opacity: 0.55;
}
.store-checkbox-grid > p {
  grid-column: 1 / -1;
  margin: 0;
  color: #8b6c5f;
  font-size: 10px;
}
input,
select {
  width: 100%;
  min-width: 0;
  min-height: 42px;
  padding: 9px 11px;
  border: 1px solid #d1dbd6;
  border-radius: 8px;
  color: #234b3a;
  background: white;
}
button {
  cursor: pointer;
}
button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
.selected-template-help {
  grid-column: 1 / -1;
  margin: -2px 0 0;
  padding: 9px 11px;
  border-radius: 8px;
  color: #5e7067;
  background: #f1f5f2;
  font-size: 11px;
  line-height: 1.5;
}
.selected-template-help strong {
  color: #255841;
}
.user-notice {
  margin: 0;
  padding: 12px 16px;
  border-radius: 10px;
  font-size: 12px;
}
.user-notice.success {
  color: #206642;
  background: #e8f5ec;
}
.user-notice.error {
  color: #a43f35;
  background: #fff0ed;
}
.account-list {
  display: grid;
  gap: 16px;
}
.account-card {
  overflow: hidden;
  border: 1px solid #dce5e0;
  border-radius: 14px;
  background: #fff;
}
.account-card.inactive {
  background: #f7f9f8;
}
.account-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  padding: 18px 20px;
  border-bottom: 1px solid #e2e8e4;
}
.account-identity {
  display: grid;
  gap: 3px;
}
.account-identity strong {
  color: #173f31;
  font-size: 15px;
}
.account-identity span {
  color: #65766e;
  font-size: 11px;
}
.account-identity small {
  color: #8a9690;
  font-size: 10px;
}
.account-status {
  flex: 0 0 auto;
  padding: 5px 8px;
  border-radius: 999px;
  color: #24704e;
  background: #e4f3ea;
  font-size: 10px;
  font-weight: 700;
}
.account-status.off {
  color: #8a5147;
  background: #f6e8e5;
}
.account-toolbar {
  display: grid;
  grid-template-columns: minmax(180px, 240px) minmax(220px, 1fr) auto;
  gap: 18px;
  align-items: end;
  padding: 16px 20px;
  border-bottom: 1px solid #e2e8e4;
  background: #fbfcfb;
}
.template-state {
  display: grid;
  gap: 3px;
}
.template-state small,
.template-state span {
  color: #86938d;
  font-size: 9px;
}
.template-state strong {
  color: #3b5f4f;
  font-size: 12px;
}
.account-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}
.account-actions button {
  min-height: 36px;
  padding: 7px 10px;
  border: 1px solid #ced9d3;
  border-radius: 7px;
  color: #36584b;
  background: white;
}
.account-actions button.danger {
  color: #a8483f;
  border-color: #e6bbb6;
}
.account-actions .copy-login-button {
  color: white;
  border-color: #24704e;
  background: #24704e;
}
.login-copy-dialog {
  width: min(460px, calc(100vw - 32px));
  max-height: calc(100dvh - 32px);
  padding: 24px;
  border: 1px solid #dce5e0;
  border-radius: 14px;
  color: #234b3a;
  overflow: auto;
}
.login-copy-dialog::backdrop { background: rgb(15 35 26 / 45%); }
.login-copy-dialog form, .login-copy-dialog label { display: grid; gap: 10px; }
.login-copy-dialog h2, .login-copy-dialog p { margin: 0; }
.login-copy-dialog h2 { font-size: 20px; }
.login-copy-dialog p { font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
.login-copy-dialog label { margin-top: 8px; font-size: 13px; }
.login-copy-url, .login-copy-hint { color: #687b70; }
.login-copy-warning { color: #a43f35; }
.login-copy-dialog textarea { width: 100%; padding: 10px; resize: vertical; box-sizing: border-box; }
.login-copy-actions { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 10px; margin-top: 10px; }
.login-copy-actions button { min-height: 44px; padding: 9px 16px; border: 1px solid #ced9d3; border-radius: 8px; }
.account-permissions {
  padding: 18px 20px 20px;
}
.account-store-access {
  padding: 18px 20px;
  border-bottom: 1px solid #e2e8e4;
  background: #f8fbf9;
}
.permission-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}
.permission-heading > div {
  display: grid;
  gap: 3px;
}
.permission-heading strong {
  color: #244e3c;
  font-size: 13px;
}
.permission-heading span,
.permission-heading small {
  color: #7e8d85;
  font-size: 10px;
}
.create-operating-heading,
.operating-store-heading {
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid #e2e8e4;
}
.permission-group-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 10px;
}
fieldset {
  min-width: 0;
  margin: 0;
  padding: 12px;
  border: 1px solid #e0e7e3;
  border-radius: 10px;
}
legend {
  padding: 0 5px;
  color: #2f5c48;
  font-size: 11px;
  font-weight: 800;
}
fieldset > p {
  min-height: 30px;
  margin: 0 0 9px;
  color: #8a9690;
  font-size: 9px;
  line-height: 1.45;
}
.permission-option {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  margin-top: 7px;
  color: #53665d;
  font-size: 10px;
  line-height: 1.4;
  cursor: pointer;
}
.permission-option input {
  flex: 0 0 auto;
  width: 15px;
  min-height: 15px;
  margin: 0;
  padding: 0;
  accent-color: #24704e;
}
.inactive .account-permissions {
  opacity: 0.6;
}
@media (max-width: 1200px) {
  .template-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .permission-group-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 1000px) {
  .create-account-form {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .create-account-form > button {
    grid-column: 1 / -1;
  }
  .account-toolbar {
    grid-template-columns: 1fr 1fr;
  }
  .account-actions {
    grid-column: 1 / -1;
    justify-content: flex-start;
  }
}
@media (max-width: 680px) {
  .erp-panel {
    padding: 20px;
  }
  .section-title {
    display: grid;
    gap: 8px;
  }
  .section-title > span {
    text-align: left;
  }
  .template-grid,
  .create-account-form,
  .store-create-form,
  .account-toolbar,
  .permission-group-grid {
    grid-template-columns: 1fr;
  }
  .template-grid article > p {
    min-height: 0;
  }
  .account-header,
  .permission-heading {
    display: grid;
  }
  .account-toolbar,
  .account-header,
  .account-store-access,
  .account-permissions {
    padding-right: 16px;
    padding-left: 16px;
  }
}

/* Mobile layout: retain every field and existing action. */

@media (max-width: 760px) {
  .store-option-grid, .permission-group-grid { grid-template-columns: minmax(0, 1fr); }
  .account-actions, .permission-actions { flex-wrap: wrap; }
  .account-actions > button { flex: 1 1 100px; }
  .permission-item, .store-access-option { min-height: 48px; align-items: start; }
  .account-toolbar input, .store-create-form input { width: 100%; }
  .account-card, .account-header > div, .store-card { min-width: 0; }
}

</style>
