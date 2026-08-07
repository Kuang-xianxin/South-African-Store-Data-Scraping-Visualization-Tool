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
  "daily_report.view": "查看运营日报",
  "daily_report.manage": "处理运营日报待办",
  "daily_report.export": "生成运营日报 Excel",
  "reports.view": "查看与下载报表",
  "reports.generate": "生成全部报表",
  "nft102.manage": "NFT102 上传与续写",
  "refresh.run": "刷新全部数据",
  "users.manage": "管理账号与权限",
};

export const templatePermissions: Record<UserRole, PermissionKey[]> = {
  viewer: [
    "store.view",
    "competitors.view",
    "daily_report.view",
    "reports.view",
  ],
  operator: [
    "store.view",
    "logistics.manage",
    "search_ranking.run",
    "competitors.view",
    "competitors.collect",
    "daily_report.view",
    "daily_report.manage",
    "daily_report.export",
    "reports.view",
    "reports.generate",
    "nft102.manage",
    "refresh.run",
  ],
  selection: [
    "competitors.view",
    "competitors.collect",
    "daily_report.view",
  ],
  admin: [
    "store.view",
    "logistics.manage",
    "search_ranking.run",
    "competitors.view",
    "competitors.collect",
    "daily_report.view",
    "daily_report.manage",
    "daily_report.export",
    "reports.view",
    "reports.generate",
    "nft102.manage",
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
    description: "经营总览、关键词流量、风险质量、物流关联与平台仓草稿",
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
    title: "运营日报",
    description: "查看日报、处理待办及生成日报 Excel",
    permissions: [
      "daily_report.view",
      "daily_report.manage",
      "daily_report.export",
    ],
  },
  {
    title: "报表工作台",
    description: "下载已有报表、生成报表和续写 NFT102",
    permissions: ["reports.view", "reports.generate", "nft102.manage"],
  },
  {
    title: "系统管理",
    description: "创建账号、套用模板和修改账号权限",
    permissions: ["users.manage"],
  },
];

const dependencies: Partial<Record<PermissionKey, PermissionKey[]>> = {
  "competitors.collect": ["competitors.view"],
  "daily_report.manage": ["daily_report.view"],
  "daily_report.export": ["daily_report.view"],
  "reports.generate": ["reports.view"],
  "nft102.manage": ["reports.view"],
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
