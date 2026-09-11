<script setup lang="ts">
withDefaults(defineProps<{
  label?: string;
  compact?: boolean;
  inline?: boolean;
}>(), {
  label: "加载中…",
  compact: false,
  inline: false,
});
</script>

<template>
  <span
    class="loading-state"
    :class="{ 'is-compact': compact, 'is-inline': inline }"
    role="status"
  >
    <span class="loading-state-dots" aria-hidden="true">
      <i></i><i></i><i></i>
    </span>
    <span>{{ label }}</span>
  </span>
</template>

<style scoped>
.loading-state {
  display: flex;
  min-height: 180px;
  padding: 24px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  color: var(--muted);
  font-size: 0.82rem;
  text-align: center;
}

.loading-state-dots {
  display: flex;
  height: 28px;
  align-items: center;
  gap: 7px;
}

.loading-state-dots i {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--green);
  animation: loading-dot-bounce 1.2s ease-in-out infinite;
}

.loading-state-dots i:nth-child(2) { animation-delay: 0.15s; }
.loading-state-dots i:nth-child(3) { animation-delay: 0.3s; }

.is-compact { min-height: 96px; padding: 16px; }

.is-inline {
  display: inline-flex;
  min-height: 0;
  padding: 0;
  flex-direction: row;
  gap: 8px;
  color: inherit;
  font: inherit;
  vertical-align: middle;
}

.is-inline .loading-state-dots { height: 16px; gap: 3px; }
.is-inline .loading-state-dots i { width: 4px; height: 4px; }

@keyframes loading-dot-bounce {
  0%, 70%, 100% { opacity: 0.35; transform: translateY(0); }
  35% { opacity: 1; transform: translateY(-5px); }
}

@media (prefers-reduced-motion: reduce) {
  .loading-state-dots i { animation: none; opacity: 0.7; }
}
</style>
