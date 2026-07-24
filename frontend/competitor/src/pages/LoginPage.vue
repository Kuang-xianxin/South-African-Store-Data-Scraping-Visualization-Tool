<script setup lang="ts">
import { computed, ref } from "vue";

import { bootstrapAdmin, login } from "../api";
import type { AuthSession, AuthStatus } from "../types";

const props = defineProps<{ status: AuthStatus }>();
const emit = defineEmits<{ authenticated: [session: AuthSession] }>();

const username = ref("");
const displayName = ref("");
const password = ref("");
const confirmPassword = ref("");
const busy = ref(false);
const error = ref("");

const isSetup = computed(() => props.status.setup_required);

async function submit() {
  error.value = "";
  if (isSetup.value && password.value !== confirmPassword.value) {
    error.value = "两次输入的密码不一致";
    return;
  }
  busy.value = true;
  try {
    const session = isSetup.value
      ? await bootstrapAdmin(username.value, displayName.value, password.value)
      : await login(username.value, password.value);
    emit("authenticated", session);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "登录失败";
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <main class="login-shell">
    <section class="login-card">
      <div class="login-brand">
        <span>T</span>
        <div>
          <strong>南非运营 ERP</strong>
          <small>TAKEALOT OPERATIONS</small>
        </div>
      </div>

      <template v-if="isSetup && !status.bootstrap_allowed">
        <p class="eyebrow">WAITING FOR ADMIN</p>
        <h1>系统等待本机初始化</h1>
        <p class="login-copy">
          请管理员在服务器电脑上打开
          <code>http://127.0.0.1:8501</code>，创建首个管理员账号后再登录。
        </p>
      </template>

      <form v-else @submit.prevent="submit">
        <p class="eyebrow">{{ isSetup ? "FIRST TIME SETUP" : "SECURE SIGN IN" }}</p>
        <h1>{{ isSetup ? "创建首个管理员" : "登录经营系统" }}</h1>
        <p class="login-copy">
          {{ isSetup ? "账号只保存在本机 MySQL，不会写入浏览器或代码。" : "请输入公司分配的 ERP 账号。" }}
        </p>

        <label>
          <span>用户名</span>
          <input
            v-model.trim="username"
            autocomplete="username"
            minlength="3"
            maxlength="64"
            pattern="[a-z0-9][a-z0-9._-]{2,63}"
            placeholder="例如 mayn"
            required
          />
        </label>
        <label v-if="isSetup">
          <span>显示名称</span>
          <input
            v-model.trim="displayName"
            autocomplete="name"
            maxlength="100"
            placeholder="例如 管理员"
          />
        </label>
        <label>
          <span>密码</span>
          <input
            v-model="password"
            type="password"
            :autocomplete="isSetup ? 'new-password' : 'current-password'"
            :minlength="isSetup ? 8 : 1"
            maxlength="128"
            required
          />
          <small v-if="isSetup">至少 8 个字符</small>
        </label>
        <label v-if="isSetup">
          <span>确认密码</span>
          <input
            v-model="confirmPassword"
            type="password"
            autocomplete="new-password"
            minlength="8"
            maxlength="128"
            required
          />
        </label>
        <p v-if="error" class="login-error">{{ error }}</p>
        <button type="submit" :disabled="busy">
          {{ busy ? "请稍候…" : isSetup ? "创建并进入系统" : "登录" }}
        </button>
      </form>
    </section>
  </main>
</template>

<style scoped>
.login-shell {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 28px;
  background:
    radial-gradient(circle at 20% 10%, rgba(202, 240, 94, 0.18), transparent 32%),
    #edf2ec;
}
.login-card {
  width: min(440px, 100%);
  padding: 38px;
  border: 1px solid rgba(22, 77, 56, 0.12);
  border-radius: 24px;
  background: rgba(255, 255, 255, 0.94);
  box-shadow: 0 24px 70px rgba(19, 55, 42, 0.14);
}
.login-brand {
  display: flex;
  gap: 14px;
  align-items: center;
  margin-bottom: 42px;
}
.login-brand > span {
  display: grid;
  place-items: center;
  width: 48px;
  height: 48px;
  border-radius: 13px;
  color: #173f31;
  background: #d4f36d;
  font-size: 22px;
  font-weight: 800;
}
.login-brand div {
  display: grid;
}
.login-brand strong {
  color: #173f31;
  font-size: 18px;
}
.login-brand small,
.eyebrow {
  color: #517064;
  font-size: 11px;
  letter-spacing: 0.13em;
}
h1 {
  margin: 8px 0;
  color: #132d24;
  font-size: 30px;
}
.login-copy {
  margin: 0 0 28px;
  color: #66766f;
  line-height: 1.7;
}
label {
  display: grid;
  gap: 8px;
  margin-top: 18px;
  color: #30483f;
  font-size: 13px;
}
input {
  width: 100%;
  padding: 13px 14px;
  border: 1px solid #cad6d0;
  border-radius: 10px;
  outline: none;
  font: inherit;
}
input:focus {
  border-color: #24684f;
  box-shadow: 0 0 0 3px rgba(36, 104, 79, 0.1);
}
label small {
  color: #718078;
}
.login-error {
  margin: 18px 0 0;
  color: #b43e31;
  font-size: 13px;
}
button {
  width: 100%;
  margin-top: 24px;
  padding: 14px;
  border: 0;
  border-radius: 10px;
  color: white;
  background: #175c43;
  font-weight: 700;
  cursor: pointer;
}
button:disabled {
  opacity: 0.6;
  cursor: wait;
}
code {
  padding: 2px 5px;
  color: #175c43;
  background: #edf4f0;
  border-radius: 4px;
}
</style>
