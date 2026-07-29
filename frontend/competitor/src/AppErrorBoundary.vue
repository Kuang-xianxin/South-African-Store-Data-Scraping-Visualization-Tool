<script setup lang="ts">
import { onErrorCaptured, ref } from "vue";

import App from "./App.vue";

const failure = ref("");

onErrorCaptured((error) => {
  failure.value =
    error instanceof Error
      ? error.message
      : "页面脚本发生未知错误";
  return false;
});

function reloadPage() {
  window.location.reload();
}
</script>

<template>
  <App v-if="!failure" />
  <main v-else class="fatal-page" role="alert">
    <section class="fatal-card">
      <p>PAGE ERROR</p>
      <h1>页面加载失败，没有影响后台任务</h1>
      <span>{{ failure }}</span>
      <small>
        请重新加载页面。若问题持续出现，请联系管理员检查 ERP 前后端版本。
      </small>
      <button type="button" @click="reloadPage">重新加载页面</button>
    </section>
  </main>
</template>

<style scoped>
.fatal-page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
  background: #edf2ec;
}

.fatal-card {
  width: min(560px, 100%);
  padding: 30px;
  border: 1px solid #dcc8a5;
  border-radius: 20px;
  background: #fffdf7;
  box-shadow: 0 18px 50px rgb(39 64 54 / 10%);
}

.fatal-card p {
  margin: 0 0 8px;
  color: #9b6525;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.12em;
}

.fatal-card h1 {
  margin: 0 0 14px;
  color: #243c33;
}

.fatal-card span,
.fatal-card small {
  display: block;
  line-height: 1.7;
}

.fatal-card span {
  color: #684d2d;
}

.fatal-card small {
  margin-top: 8px;
  color: #6d7a74;
}

.fatal-card button {
  margin-top: 22px;
  border: 0;
  border-radius: 12px;
  padding: 11px 18px;
  color: white;
  background: #315f50;
  font-weight: 700;
  cursor: pointer;
}
</style>
