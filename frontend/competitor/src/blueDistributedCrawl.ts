export interface BlueCrawlWorker {
  worker_id: string;
  node_name: string;
  state: string;
  current_plid: string | null;
  online: boolean | number;
  last_error: string | null;
  last_seen_at: string;
}

export interface BlueCrawlBatch {
  batch_id: string;
  total: number;
  pending: number;
  running: number;
  retry: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  updated_at: string;
}

export interface BlueCrawlStatus {
  workers: BlueCrawlWorker[];
  batches: BlueCrawlBatch[];
  items: Array<{
    batch_id: string; plid: string; status: string; lease_owner: string | null;
    attempt_count: number; title: string | null; message: string | null; followers_only?: boolean;
  }>;
  durable_delivery: boolean;
  durable_cluster_ready: boolean;
  full_crawl?: boolean;
  max_targets: number;
}

export interface BlueCrawlPreview { total: number; competitors: number; own: number }
export interface BlueCrawlPending { request_id: string; mode: "full" | "selected"; plids: string[] }

export function isBlueDeployment(label: string | null | undefined): boolean {
  return /^blue-stage-(main|laptop)$/.test(label ?? "");
}

export function blueBatchActive(batch: BlueCrawlBatch): boolean {
  return Number(batch.pending) + Number(batch.running) + Number(batch.retry) > 0;
}

export function blueBatchLabel(batch: BlueCrawlBatch): string {
  if (blueBatchActive(batch)) return Number(batch.running) > 0 ? "采集中" : Number(batch.retry) > 0 ? "等待重试" : "等待领取";
  if (Number(batch.cancelled) > 0) return "已停止";
  return Number(batch.failed) > 0 ? "已结束，含终止链接" : "已完成";
}

export function blueWorkerLabel(worker: BlueCrawlWorker | undefined): string {
  if (!worker) return "尚未连接";
  if (!worker.online) return "连接已过期";
  return ({ collecting: "采集中", idle: "空闲", "offline-waiting": "等待连接",
    "result-buffered": "结果待补交", "delivery-needs-attention": "补交需处理",
    "disk-space-low": "磁盘余量不足", "needs-attention": "需要处理", "legacy-busy": "原单机批次运行中" } as Record<string, string>)[worker.state]
    ?? "等待同步";
}

export function blueWorkerBacklog(worker: BlueCrawlWorker | undefined): string {
  const pending = Number(worker?.last_error?.match(/(?:^|\s)pending_uploads=(\d+)(?:\s|$)/)?.[1] ?? 0);
  const blocked = Number(worker?.last_error?.match(/(?:^|\s)blocked_uploads=(\d+)(?:\s|$)/)?.[1] ?? 0);
  return [pending ? `${pending}条结果待补交` : "", blocked ? `${blocked}条结果需核对` : ""].filter(Boolean).join(" · ");
}

export function bluePendingBatchId(pending: BlueCrawlPending): string {
  return `blue-${pending.mode === "full" ? "full" : "web"}-${pending.request_id}`;
}

export function parseBlueTestPlids(text: string): string[] {
  const tokens = text.trim().split(/[\s,，;；]+/).filter(Boolean);
  const plids = tokens.map(token => /^\d{1,30}$/.test(token) ? token
    : token.match(/(?:^PLID|\/PLID)(\d{1,30})(?:[?#].*)?$/i)?.[1] ?? "");
  const unique = [...new Set(plids)];
  if (!unique.length || unique.length > 20 || unique.includes("")) {
    throw new Error("请输入1–20条已监控真正竞品的链接或PLID");
  }
  return unique;
}

export function readBluePending(value: string | null): BlueCrawlPending | null {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as BlueCrawlPending;
    if (!/^[a-f0-9]{32}$/.test(parsed.request_id) || !["full", "selected"].includes(parsed.mode)
      || !Array.isArray(parsed.plids)) return null;
    if (parsed.mode === "full" && parsed.plids.length) return null;
    if (parsed.mode === "selected") parseBlueTestPlids(parsed.plids.join("\n"));
    return parsed;
  } catch { return null; }
}
