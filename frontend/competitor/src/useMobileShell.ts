import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

// Keep this breakpoint in sync with mobile.css. Page state stays in App/KeepAlive.
export function useMobileShell() {
  const viewport = window.matchMedia("(max-width: 1023px)");
  const isMobile = ref(viewport.matches);
  const mobileNavOpen = ref(false);
  const mobileToolsOpen = ref(false);
  const mobileNav = ref<HTMLElement | null>(null);
  const mobileMenuButton = ref<HTMLButtonElement | null>(null);
  let previousOverflow: string | null = null;

  function unlockScroll() {
    if (previousOverflow === null) return;
    document.body.style.overflow = previousOverflow;
    previousOverflow = null;
  }

  function closeMobileNav() {
    mobileNavOpen.value = false;
  }

  function updateViewport(event: MediaQueryListEvent) {
    isMobile.value = event.matches;
    closeMobileNav();
    mobileToolsOpen.value = false;
  }

  function handleNavigationKey(event: KeyboardEvent) {
    if (!isMobile.value || !mobileNavOpen.value) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeMobileNav();
      return;
    }
    if (event.key !== "Tab") return;
    const controls = Array.from(mobileNav.value?.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), [tabindex="0"]',
    ) ?? []).filter((element) => element.getClientRects().length > 0);
    const first = controls[0];
    const last = controls.at(-1);
    if (!first || !last) return;
    if (event.shiftKey && (document.activeElement === first || !mobileNav.value?.contains(document.activeElement))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || !mobileNav.value?.contains(document.activeElement))) {
      event.preventDefault();
      first.focus();
    }
  }

  watch(mobileNavOpen, async (open) => {
    if (open && isMobile.value) {
      previousOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      await nextTick();
      if (mobileNavOpen.value) mobileNav.value?.querySelector<HTMLElement>('a[aria-current="page"], button')?.focus();
    } else {
      unlockScroll();
      await nextTick();
      if (isMobile.value && !mobileNavOpen.value) mobileMenuButton.value?.focus();
    }
  });

  onMounted(() => {
    viewport.addEventListener("change", updateViewport);
    document.addEventListener("keydown", handleNavigationKey);
  });
  onBeforeUnmount(() => {
    viewport.removeEventListener("change", updateViewport);
    document.removeEventListener("keydown", handleNavigationKey);
    unlockScroll();
  });

  return { isMobile, mobileNavOpen, mobileToolsOpen, mobileNav, mobileMenuButton, closeMobileNav };
}
