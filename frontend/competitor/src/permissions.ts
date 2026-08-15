import type { AuthUser, PermissionKey, UserRole } from "./types";

export const templateLabels: Record<UserRole, string> = {
  viewer: "查看",
  operator: "运营",
  selection: "选品",
  admin: "管理员",
};

export const permissionLabels: Record<PermissionKey, string> = {
  "store.view": "查看店铺经营数据",
  "logistics.manage": "管理物流关联与平台仓草稿",
  "keyword_traffic.manage": "旧版关键词手工记录（已停用）",
  "search_ranking.run": "调用多模态模型并采集搜索定位",
  "competitors.view": "查看竞品雷达",
  "competitors.collect": "采集竞品",
  "refresh.run": "刷新全部数据",
  "users.manage": "管理账号与权限",
};

export const templatePermissions: Record<UserRole, PermissionKey[]> = {
  viewer: [
    "store.view",
    "competitors.view",
  ],
  operator: [
    "store.view",
    "logistics.manage",
    "search_ranking.run",
    "competitors.view",
    "competitors.collect",
    "refresh.run",
  ],
  selection: [
    "competitors.view",
    "competitors.collect",
  ],
  admin: [
    "store.view",
    "logistics.manage",
    "search_ranking.run",
    "competitors.view",
    "competitors.collect",
    "refresh.run",
    "users.manage",
  ],
};

export const permissionGroups: Array<{
  title: string;
  description: string;
  permissions: PermissionKey[];
}> = [
  {
    title: "店铺经营",
    description: "经营总览、关键词流量、物流关联与平台仓草稿",
    permissions: [
      "store.view",
      "logistics.manage",
      "search_ranking.run",
      "refresh.run",
    ],
  },
  {
    title: "竞品雷达",
    description: "查看历史与发起公开竞品采集",
    permissions: ["competitors.view", "competitors.collect"],
  },
  {
    title: "系统管理",
    description: "创建账号、套用模板和修改账号权限",
    permissions: ["users.manage"],
  },
];

const dependencies: Partial<Record<PermissionKey, PermissionKey[]>> = {
  "competitors.collect": ["competitors.view"],
  "refresh.run": ["store.view"],
  "logistics.manage": ["store.view"],
  "search_ranking.run": ["store.view"],
};

export function userHasPermission(
  user: AuthUser | null | undefined,
  permission: PermissionKey,
): boolean {
  return Array.isArray(user?.permissions) && user.permissions.includes(permission);
}

export function togglePermission(
  current: PermissionKey[],
  permission: PermissionKey,
  enabled: boolean,
): PermissionKey[] {
  const next = new Set(current);
  if (enabled) {
    next.add(permission);
    for (const dependency of dependencies[permission] ?? []) next.add(dependency);
  } else {
    next.delete(permission);
    for (const [child, parents] of Object.entries(dependencies) as Array<
      [PermissionKey, PermissionKey[]]
    >) {
      if (parents.includes(permission)) next.delete(child);
    }
  }
  return [...next].sort();
}
