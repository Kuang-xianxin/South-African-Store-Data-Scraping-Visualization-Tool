export type RootExpansionCheckIdentity = {
  root?: unknown;
  seed?: unknown;
  input_state?: unknown;
  shopper_root?: unknown;
};

export function rootExpansionCheckValue(check: RootExpansionCheckIdentity): string {
  for (const value of [check.root, check.seed, check.input_state, check.shopper_root]) {
    if (typeof value !== "string" && typeof value !== "number") continue;
    const normalized = String(value).trim();
    if (normalized) return normalized;
  }
  return "";
}

export function rootExpansionCheckLabel(check: RootExpansionCheckIdentity): string {
  return rootExpansionCheckValue(check) || "历史词根（原记录未保存）";
}

export function rootExpansionCheckIsPhrase(check: RootExpansionCheckIdentity): boolean {
  const value = rootExpansionCheckValue(check);
  return Boolean(value && value.split(/\s+/).length > 1);
}
