import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from "vue";

/** Measure the actual chart container; desktop coordinates remain unchanged. */
export function useResponsiveChart(desktopWidth: number) {
  const chartElement = shallowRef<HTMLElement | null>(null);
  const measuredWidth = ref(desktopWidth);
  const media = window.matchMedia("(max-width: 760px)");
  const compact = ref(media.matches);
  let observer: ResizeObserver | null = null;
  const chartWidth = computed(() => compact.value ? Math.max(220, measuredWidth.value) : desktopWidth);

  function measure() {
    const element = chartElement.value;
    if (!element) return;
    const style = getComputedStyle(element);
    const width = element.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    // KeepAlive temporarily detaches hidden pages; zero must not replace a valid width.
    if (width > 0) measuredWidth.value = Math.min(desktopWidth, Math.floor(width));
  }
  function handleMedia(event: MediaQueryListEvent) {
    compact.value = event.matches;
    measure();
  }
  watch(chartElement, (element) => {
    observer?.disconnect();
    if (!element) return;
    observer = new ResizeObserver(measure);
    observer.observe(element);
    measure();
  }, { flush: "post" });
  onMounted(() => media.addEventListener("change", handleMedia));
  onBeforeUnmount(() => {
    observer?.disconnect();
    media.removeEventListener("change", handleMedia);
  });
  return { chartElement, chartWidth, compact };
}
