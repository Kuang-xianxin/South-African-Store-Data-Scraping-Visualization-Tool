import { computed, ref, shallowRef, watch, type CSSProperties, type WatchSource } from "vue";

/** Keep a date reload's placeholders from shortening the document and clamping scroll. */
export function useStablePageHeight(source: WatchSource, pending: () => boolean) {
  const pageElement = shallowRef<HTMLElement | null>(null);
  const reservedHeight = ref<number | undefined>();

  watch(source, () => {
    // Pre-flush runs before either the page or its children replace their content.
    const height = pageElement.value?.getBoundingClientRect().height;
    if (height) reservedHeight.value = Math.max(reservedHeight.value ?? 0, height);
  }, { flush: "pre" });

  watch(pending, (busy) => {
    // All independent requests must settle and render before releasing the space.
    if (!busy) reservedHeight.value = undefined;
  }, { flush: "post" });

  const pageStyle = computed<CSSProperties>(() => ({
    minHeight: reservedHeight.value === undefined ? undefined : `${reservedHeight.value}px`,
    alignContent: "start",
    // Date reloads must not anchor to temporary loading nodes or replacement charts.
    overflowAnchor: "none",
  }));
  return { pageElement, pageStyle };
}
