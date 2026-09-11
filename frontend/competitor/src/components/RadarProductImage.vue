<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from "vue";
const props = defineProps<{ src: string; title: string; show: boolean }>();
const emit = defineEmits<{ (event: "image-error", value: Event): void }>();
const anchor = ref<HTMLButtonElement | null>(null);
const preview = ref<HTMLElement | null>(null);
const visible = ref(false);
const position = ref({ left: "0px", top: "0px", width: "280px", height: "280px" });
async function open() {
  if (!props.show || !props.src || !anchor.value) return;
  const rect = anchor.value.getBoundingClientRect();
  const size = Math.min(280, window.innerWidth - 24, window.innerHeight - 24);
  const left = rect.right + size + 12 < window.innerWidth ? rect.right + 8 : Math.max(12, rect.left - size - 8);
  position.value = { left: `${left}px`, top: `${Math.max(12, Math.min(rect.top, window.innerHeight - size - 12))}px`, width: `${size}px`, height: `${size}px` };
  visible.value = true;
  await nextTick();
  if (visible.value) preview.value?.showPopover?.();
}
function close() { visible.value = false; preview.value?.hidePopover?.(); }
watch(() => [props.src, props.show], close);
if (typeof window !== "undefined") {
  window.addEventListener("scroll", close, true);
  window.addEventListener("resize", close);
}
onBeforeUnmount(() => {
  close();
  if (typeof window !== "undefined") { window.removeEventListener("scroll", close, true); window.removeEventListener("resize", close); }
});
</script>
<template>
  <button ref="anchor" type="button" class="radar-image-trigger competitor-product-image competitor-status-image"
    :disabled="!show" :aria-label="`${title}，悬浮或点击放大图片`" :aria-expanded="visible"
    @mouseenter="open" @mouseleave="close" @focus="open" @blur="close" @keydown.esc.stop="close" @click.stop="open">
    <img v-if="show" :src="src" :alt="`${title} 商品图片`" width="192" height="192" loading="lazy" decoding="async" @error="close(); emit('image-error', $event)" />
    <span v-else>暂无图片</span>
  </button>
  <Teleport to="body">
    <div v-if="visible" ref="preview" popover="manual" class="radar-image-preview" :style="position" aria-hidden="true">
      <img :src="src" :alt="title" />
    </div>
  </Teleport>
</template>
