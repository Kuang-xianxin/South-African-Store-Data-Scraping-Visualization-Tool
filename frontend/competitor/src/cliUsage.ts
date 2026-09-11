export type CliUsagePeriod = "today" | "7d" | "30d" | "all";
export interface CliUsageTotals {
  attempts: number;
  requests: number;
  completed: number;
  failed: number;
  blocked: number;
  unfinished: number;
  unknown_usage_requests: number;
  input_tokens: number | null;
  cached_input_tokens: number | null;
  output_tokens: number | null;
  reasoning_output_tokens: number | null;
  total_tokens: number | null;
}
export interface CliUsagePayload {
  period: CliUsagePeriod;
  timezone: string;
  start: string | null;
  as_of: string;
  first_recorded_at: string | null;
  history_scope: "recorded_cli_requests_only";
  total: CliUsageTotals;
  by_user: Array<CliUsageTotals & { user_id: number | null; username: string | null; display_name: string }>;
  by_store: Array<CliUsageTotals & { store_code: string; display_name: string }>;
  recent: Array<{
    id: string; actor_user_id: number | null; actor_username: string | null;
    store_code: string; model: string; stage: string; status: string;
    created_at: string; dispatched_at: string | null;
    input_tokens: number | null; output_tokens: number | null; total_tokens: number | null;
  }>;
}
