interface QueueOptions {
  refresh: () => Promise<boolean | void>;
  available: () => boolean;
  busy: () => boolean;
  editing: () => boolean;
  status: (value: string) => void;
  minimumInterval?: number;
  now?: () => number;
  setTimer?: typeof setTimeout;
  clearTimer?: typeof clearTimeout;
}

/** Coalesced refreshes: one in flight, latest revision retained, bounded retries. */
export class LiveUpdateQueue {
  private current = "";
  private applied = "";
  private context = "";
  private appliedByContext = new Map<string, string>();
  private running = false;
  private disposed = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private lastStarted = -Infinity;
  private failures = 0;

  private readonly options: QueueOptions;

  constructor(options: QueueOptions) { this.options = options; }

  observe(context: string, revision: string, baseline = false): void {
    if (this.disposed) return;
    if (this.context !== context) {
      this.context = context;
      this.applied = this.appliedByContext.get(context) ?? (baseline ? revision : "");
      this.cancelTimer();
    }
    this.current = revision;
    if (baseline) { this.applied = revision; this.rememberApplied(context, revision); }
    this.wake();
  }

  wake(): void {
    if (this.disposed || this.running || this.current === this.applied || !this.current) return;
    if (!this.options.available()) { this.cancelTimer(); return; }
    if (this.options.editing()) this.options.status("有新数据，完成编辑后自动更新");
    if (this.timer !== null) return;
    const delay = Math.max(1_500,
      this.lastStarted + (this.options.minimumInterval ?? 15_000) - this.now());
    this.timer = (this.options.setTimer ?? setTimeout)(() => {
      this.timer = null;
      void this.flush();
    }, delay);
  }

  private now(): number { return (this.options.now ?? Date.now)(); }

  private async flush(): Promise<void> {
    if (this.disposed || !this.options.available()) return;
    if (this.options.busy() || this.options.editing()) { this.wake(); return; }
    const revision = this.current;
    const context = this.context;
    this.running = true;
    this.lastStarted = this.now();
    this.options.status("");
    try {
      if (await this.options.refresh() === false) throw new Error("refresh unavailable");
      if (context === this.context && !this.options.editing()) {
        this.applied = revision;
        this.rememberApplied(context, revision);
      }
      this.failures = 0;
      if (!this.disposed) this.options.status("");
    } catch {
      this.failures += 1;
      this.lastStarted = this.now() + Math.min(120_000, 15_000 * 2 ** this.failures);
      if (!this.disposed) this.options.status("暂未更新，稍后重试");
    } finally {
      this.running = false;
      this.wake();
    }
  }

  private cancelTimer(): void {
    if (this.timer !== null) (this.options.clearTimer ?? clearTimeout)(this.timer);
    this.timer = null;
  }

  private rememberApplied(context: string, revision: string): void {
    this.appliedByContext.delete(context);
    this.appliedByContext.set(context, revision);
    if (this.appliedByContext.size > 32) {
      this.appliedByContext.delete(this.appliedByContext.keys().next().value!);
    }
  }

  dispose(): void { this.disposed = true; this.cancelTimer(); this.options.status(""); }
}
