<script setup lang="ts">
import { onMounted, ref } from "vue";

import { createUser, fetchUsers, updateUser } from "../api";
import type { ManagedUser, UserRole } from "../types";

const users = ref<ManagedUser[]>([]);
const loading = ref(false);
const saving = ref<number | null>(null);
const message = ref("");
const error = ref("");
const username = ref("");
const displayName = ref("");
const password = ref("");
const role = ref<UserRole>("viewer");

const roleLabels: Record<UserRole, string> = {
  viewer: "查看员",
  operator: "运营员",
  admin: "管理员",
};

onMounted(loadUsers);

async function loadUsers() {
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

async function addUser() {
  error.value = "";
  message.value = "";
  try {
    await createUser({
      username: username.value,
      display_name: displayName.value,
      password: password.value,
      role: role.value,
    });
    username.value = "";
    displayName.value = "";
    password.value = "";
    role.value = "viewer";
    message.value = "账号已创建";
    await loadUsers();
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "账号创建失败";
  }
}

async function changeRole(user: ManagedUser, nextRole: UserRole) {
  await saveUser(user, { role: nextRole });
}

async function toggleActive(user: ManagedUser) {
  await saveUser(user, { active: !user.active });
}

async function resetPassword(user: ManagedUser) {
  const next = window.prompt(`为 ${user.display_name} 设置新密码（至少 12 个字符）`);
  if (next === null) return;
  await saveUser(user, { password: next });
}

async function saveUser(
  user: ManagedUser,
  change: { role?: UserRole; active?: boolean; password?: string },
) {
  saving.value = user.id;
  error.value = "";
  message.value = "";
  try {
    const updated = await updateUser(user.id, change);
    users.value = users.value.map((item) => (item.id === updated.id ? updated : item));
    message.value = "用户权限已更新";
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "更新失败";
  } finally {
    saving.value = null;
  }
}
</script>

<template>
  <div class="erp-page users-page">
    <section class="page-heading">
      <div>
        <p>ACCESS CONTROL</p>
        <h2>用户与权限</h2>
        <span>账号保存在本机 MySQL，权限由后端接口强制执行。</span>
      </div>
    </section>

    <p v-if="message" class="user-notice success">{{ message }}</p>
    <p v-if="error" class="user-notice error">{{ error }}</p>

    <section class="erp-panel create-user">
      <div class="section-title">
        <p>NEW ACCOUNT</p>
        <h3>创建公司账号</h3>
      </div>
      <form @submit.prevent="addUser">
        <label>
          <span>用户名</span>
          <input
            v-model.trim="username"
            pattern="[a-z0-9][a-z0-9._-]{2,63}"
            placeholder="小写字母、数字"
            required
          />
        </label>
        <label>
          <span>显示名称</span>
          <input v-model.trim="displayName" placeholder="同事姓名" maxlength="100" />
        </label>
        <label>
          <span>初始密码</span>
          <input v-model="password" type="password" minlength="12" maxlength="128" required />
        </label>
        <label>
          <span>角色</span>
          <select v-model="role">
            <option value="viewer">查看员 · 只读和下载</option>
            <option value="operator">运营员 · 可采集和生成报表</option>
            <option value="admin">管理员 · 可管理账号</option>
          </select>
        </label>
        <button type="submit">创建账号</button>
      </form>
    </section>

    <section class="erp-panel user-list">
      <div class="section-title">
        <p>ACTIVE DIRECTORY</p>
        <h3>现有账号</h3>
      </div>
      <p v-if="loading">正在读取…</p>
      <div v-else class="user-table-wrap">
        <table>
          <thead>
            <tr>
              <th>账号</th>
              <th>角色</th>
              <th>状态</th>
              <th>最近登录</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="user in users" :key="user.id" :class="{ inactive: !user.active }">
              <td>
                <strong>{{ user.display_name }}</strong>
                <small>@{{ user.username }}</small>
              </td>
              <td>
                <select
                  :value="user.role"
                  :disabled="saving === user.id"
                  @change="changeRole(user, ($event.target as HTMLSelectElement).value as UserRole)"
                >
                  <option v-for="(label, key) in roleLabels" :key="key" :value="key">
                    {{ label }}
                  </option>
                </select>
              </td>
              <td>
                <span class="status-pill" :class="{ off: !user.active }">
                  {{ user.active ? "启用" : "停用" }}
                </span>
              </td>
              <td>{{ user.last_login_at ? new Date(user.last_login_at + "Z").toLocaleString("zh-CN") : "尚未登录" }}</td>
              <td class="row-actions">
                <button type="button" :disabled="saving === user.id" @click="resetPassword(user)">
                  重置密码
                </button>
                <button type="button" :disabled="saving === user.id" @click="toggleActive(user)">
                  {{ user.active ? "停用" : "启用" }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.users-page {
  display: grid;
  gap: 20px;
}
.page-heading,
.section-title {
  display: flex;
  justify-content: space-between;
}
.page-heading p,
.section-title p {
  margin: 0;
  color: #176047;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.15em;
}
.page-heading h2 {
  margin: 8px 0;
  font-size: clamp(28px, 4vw, 42px);
}
.page-heading span {
  color: #6b7872;
}
.erp-panel {
  padding: 28px;
  border-radius: 20px;
  background: rgba(255, 255, 255, 0.84);
  box-shadow: 0 14px 40px rgba(23, 58, 44, 0.07);
}
.section-title {
  margin-bottom: 20px;
}
.section-title h3 {
  margin: 6px 0 0;
  font-size: 22px;
}
form {
  display: grid;
  grid-template-columns: repeat(4, minmax(150px, 1fr)) auto;
  gap: 14px;
  align-items: end;
}
label {
  display: grid;
  gap: 7px;
  color: #53655d;
  font-size: 12px;
}
input,
select {
  min-height: 42px;
  padding: 9px 11px;
  border: 1px solid #d1dbd6;
  border-radius: 9px;
  background: white;
}
button {
  min-height: 42px;
  padding: 9px 15px;
  border: 0;
  border-radius: 9px;
  color: white;
  background: #175c43;
  font-weight: 700;
  cursor: pointer;
}
button:disabled {
  opacity: 0.5;
}
.user-notice {
  margin: 0;
  padding: 12px 16px;
  border-radius: 10px;
}
.user-notice.success {
  color: #175c43;
  background: #e7f5ed;
}
.user-notice.error {
  color: #a73b30;
  background: #fff0ed;
}
.user-table-wrap {
  overflow-x: auto;
}
table {
  width: 100%;
  border-collapse: collapse;
}
th,
td {
  padding: 14px 12px;
  border-bottom: 1px solid #e2e8e4;
  text-align: left;
  white-space: nowrap;
}
th {
  color: #718078;
  font-size: 11px;
}
td strong,
td small {
  display: block;
}
td small {
  margin-top: 3px;
  color: #87938d;
}
tr.inactive {
  opacity: 0.58;
}
.status-pill {
  display: inline-flex;
  padding: 4px 9px;
  border-radius: 999px;
  color: #176047;
  background: #e1f3e9;
  font-size: 12px;
}
.status-pill.off {
  color: #8a4b43;
  background: #f5e6e4;
}
.row-actions {
  display: flex;
  gap: 8px;
}
.row-actions button {
  min-height: auto;
  padding: 7px 10px;
  color: #245442;
  background: #edf4f0;
  font-size: 12px;
}
@media (max-width: 1100px) {
  form {
    grid-template-columns: 1fr 1fr;
  }
}
@media (max-width: 680px) {
  form {
    grid-template-columns: 1fr;
  }
  .erp-panel {
    padding: 20px;
  }
}
</style>
