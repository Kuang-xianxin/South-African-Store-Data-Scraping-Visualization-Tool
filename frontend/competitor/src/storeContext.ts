let activeStoreCode = "current";

export function getActiveStoreCode(): string {
  return activeStoreCode;
}

export function setActiveStoreCode(storeCode: string | null | undefined) {
  activeStoreCode = storeCode?.trim().toLowerCase() || "current";
}

export function withStoreContext(url: string): string {
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}store_code=${encodeURIComponent(activeStoreCode)}`;
}
