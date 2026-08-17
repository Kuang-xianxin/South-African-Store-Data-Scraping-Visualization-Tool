import type { OwnStoreScope } from "./types";

export function defaultMultiStoreScope(
  accessibleConnectedStoreCount: number,
  operatingConnectedStoreCount: number,
): OwnStoreScope {
  if (accessibleConnectedStoreCount <= 1) return "current";
  return operatingConnectedStoreCount === accessibleConnectedStoreCount
    ? "operating"
    : "all";
}
