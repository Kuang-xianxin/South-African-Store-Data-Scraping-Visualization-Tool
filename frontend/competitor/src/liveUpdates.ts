import { onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from "vue";
import type { ErpModuleKey } from "./moduleNavigation";
import type { OwnStoreScope } from "./types";
import { LiveUpdateQueue } from "./liveUpdateQueue";

export const DATA_MUTATED_EVENT = "erp-data-mutated";
type DataVersionModuleKey = ErpModuleKey | "keyword-traffic";
export type DataVersions = Partial<Record<DataVersionModuleKey | `competitors:${OwnStoreScope}`, string>>;
export const liveVersions = ref<{ context: string; versions: DataVersions }>({
  context: "", versions: {},
});
export const liveUpdateMessages = ref<Record<string, string>>({});
let nextSubscriber = 0;

export function publishDataVersions(context: string, versions: DataVersions) {
  liveVersions.value = { context, versions };
}

function inputFocused(): boolean {
  return Boolean(document.activeElement?.matches("input, textarea, select, [contenteditable='true']"));
}

/** Each retained page has a subscription; only mounted/activated pages may read. */
export function useLiveUpdates(
  module: DataVersionModuleKey,
  refresh: () => Promise<boolean | void>,
  options: {
    busy?: () => boolean; editing?: () => boolean; enabled?: () => boolean;
    viewKey?: () => string; revisionKey?: () => keyof DataVersions;
  } = {},
) {
  const id = `${module}:${++nextSubscriber}`;
  const background = ref(false);
  let active = false;
  const available = () => active && document.visibilityState === "visible"
    && navigator.onLine !== false && (options.enabled?.() ?? true);
  const queue = new LiveUpdateQueue({
    refresh: async () => {
      background.value = true;
      try { return await refresh(); }
      finally { background.value = false; }
    },
    available,
    busy: () => options.busy?.() ?? false,
    editing: () => inputFocused() || (options.editing?.() ?? false),
    minimumInterval: module === "competitors" ? 30_000 : 15_000,
    status: (message) => {
      liveUpdateMessages.value = { ...liveUpdateMessages.value, [id]: active ? message : "" };
    },
  });
  const initial = liveVersions.value;
  const context = (value: string) => `${value}:${options.viewKey?.() ?? ""}`;
  const revisionKey = () => options.revisionKey?.() ?? module;
  if (initial.versions[revisionKey()]) queue.observe(context(initial.context), initial.versions[revisionKey()]!, true);
  watch([liveVersions, () => options.viewKey?.() ?? "", revisionKey], ([state]) => {
    if (state.versions[revisionKey()]) queue.observe(context(state.context), state.versions[revisionKey()]!);
  });
  const wake = () => queue.wake();
  const activate = () => { active = true; wake(); };
  const deactivate = () => {
    active = false;
    liveUpdateMessages.value = { ...liveUpdateMessages.value, [id]: "" };
    wake();
  };
  onMounted(() => {
    activate();
    document.addEventListener("visibilitychange", wake);
    document.addEventListener("focusout", wake);
    window.addEventListener("online", wake);
  });
  onActivated(activate);
  onDeactivated(deactivate);
  onBeforeUnmount(() => {
    active = false;
    queue.dispose();
    const { [id]: ignored, ...rest } = liveUpdateMessages.value;
    liveUpdateMessages.value = rest;
    document.removeEventListener("visibilitychange", wake);
    document.removeEventListener("focusout", wake);
    window.removeEventListener("online", wake);
  });
  return { background, isActive: () => active };
}
