<script setup lang="ts">
import { computed, ref } from "vue";

import { createUser, fetchUsers, updateUser } from "../api";
import {
  permissionGroups,
  permissionLabels,
  templateLabels,
  templatePermissions,
  togglePermission,
} from "../permissions";
import type {
  ManagedUser,
  PermissionKey,
  UserRole,
} from "../types";

const users = ref<ManagedUser[]>([]);
const loading = ref(true);
const saving = ref(false);
const busyUserId = ref<number | null>(null);
const notice = ref("");
const error = ref("");
const username = ref("");
const displayName = ref("");
const password = ref("");
const role = ref<UserRole>("viewer");

const roleDescriptions: Record<UserRole, string> = {
  viewer: "查看店铺、竞品、运营日报和已有报表，不执行采集、刷新或人工处理。",
  operator: "承担日常运营工作，可采集、刷新、处理日报待办并生成报表。",
  selection: "可查看和采集竞品雷达，也可查看运营日报，但不能处理任何日报待办。",
  admin: "拥有全部业务能力，并可创建账号、套用模板和调整每个账号的独立权限。",
};

const templateCards = (Object.keys(templateLabels) as UserRole[]).map((key) => ({
  role: key,
  title: templateLabels[key],
  description: roleDescriptions[key],
  permissions: templatePermissions[key].map((permission) => permissionLabels[permission]),
}));

const selectedRoleDescription = computed(() => roleDescriptions[role.value]);

void load();

async function load() {
  loading.value = true;
  error.value = "";
  try {
    users.value = await fetchUsers();
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
  try {
    const created = await createUser({
      username: username.value,
      display_name: displayName.value,
      password: password.value,
      role: role.value,
    });
    users.value = [...users.value, created];
    username.value = "";
    displayName.value = "";
    password.value = "";
    role.value = "viewer";
    notice.value = "账号已创建，并已套用所选权限模板。";
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "账号创建失败";
  } finally {
    saving.value = false;
  }
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
    active?: boolean;
    password?: string;
  },
  successMessage: string,
) {
  if (busyUserId.value !== null) return;
  busyUserId.value = user.id;
  notice.value = "";
  error.value = "";
  try {
    const updated = await updateUser(user.id, change);
    users.value = users.value.map((item) =>
      item.id === updated.id ? updated : item,
    );
    notice.value = successMessage;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "账号更新失败";
  } finally {
    busyUserId.value = null;
  }
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
        <span>模板只用于快速套用，现有账号的权限可继续单独增删</span>
      </div>

      <div class="template-grid">
        <article v-for="card in templateCards" :key="card.role">
          <header>
            <strong>{{ card.title }}</strong>
            <small>{{ card.role }}</small>
          </header>
          <p>{{ card.description }}</p>
          <ul>
            <li v-for="item in card.permissions" :key="item">{{ item }}</li>
          </ul>
        </article>
      </div>
    </section>

    <section class="erp-panel create-panel">
      <div class="section-title">
        <div>
          <p class="section-kicker">CREATE ACCOUNT</p>
          <h2>新建账号</h2>
        </div>
        <span>新账号先套用模板，创建后可在下方逐项调整权限</span>
      </div>
      <form @submit.prevent="submit">
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
          <select v-model="role" aria-label="新账号权限模板">
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
        <p class="selected-template-help" aria-live="polite">
          <strong>{{ templateLabels[role] }}：</strong>{{ selectedRoleDescription }}
        </p>
      </form>
    </section>

    <p v-if="notice" class="user-notice success">{{ notice }}</p>
    <p v-if="error" class="user-notice error">{{ error }}</p>

    <section class="erp-panel accounts-panel">
      <div class="section-title">
        <div>
          <p class="section-kicker">EXISTING ACCOUNTS</p>
          <h2>现有账号</h2>
        </div>
        <span>共 {{ users.length }} 个账号 · 权限变更后账号需重新登录</span>
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
              <span>套用其他模板会替换当前自定义权限</span>
            </div>
            <div class="account-actions">
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

          <div class="account-permissions">
            <div class="permission-heading">
              <div>
                <strong>账号独立权限</strong>
                <span>勾选后立即保存；所需的查看权限会自动一并开启</span>
              </div>
              <small>{{ user.permissions.length }} 项已开启</small>
            </div>
            <div class="permission-group-grid">
              <fieldset v-for="group in permissionGroups" :key="group.title">
                <legend>{{ group.title }}</legend>
                <p>{{ group.description }}</p>
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
form {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr)) auto;
  gap: 14px;
  align-items: end;
}
form label,
.account-toolbar > label {
  min-width: 0;
  display: grid;
  gap: 7px;
  color: #53655d;
  font-size: 11px;
  font-weight: 700;
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
.account-permissions {
  padding: 18px 20px 20px;
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
  form {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  form > button {
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
  form,
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
  .account-permissions {
    padding-right: 16px;
    padding-left: 16px;
  }
}
</style>
