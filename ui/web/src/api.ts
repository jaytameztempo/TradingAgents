// Typed client for the local FastAPI server. Every broker call here is a GET;
// the UI has no request that places, changes, cancels or closes an order.

export class ApiError extends Error {
  constructor(public status: number, public code: string | null, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!resp.ok) {
    let code: string | null = null;
    let message = `${resp.status} ${resp.statusText}`;
    try {
      const body = await resp.json();
      const detail = body?.detail;
      if (typeof detail === "string") message = detail;
      else if (detail?.message) {
        message = detail.message;
        code = detail.code ?? null;
      }
    } catch {
      /* keep the status text */
    }
    throw new ApiError(resp.status, code, message);
  }
  return resp.json() as Promise<T>;
}

const get = <T>(path: string) => request<T>(path);
const post = <T>(path: string, body: unknown) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body) });
const put = <T>(path: string, body: unknown) =>
  request<T>(path, { method: "PUT", body: JSON.stringify(body) });

// --- settings ------------------------------------------------------------------------

export interface Automation {
  enabled: boolean;
  days: string[];
  times: string[];
  run: "daily" | "scans";
}

export interface Trading {
  days: string[];
  sessions: { start: string; end: string }[];
  auto_send: { enabled: boolean; times: string[] };
}

export interface SchedulerEvent {
  at: string;
  kind: string;
  slot: string;
  outcome: "started" | "skipped" | "error";
  detail: string;
  job_id: string | null;
}

export interface SchedulerStatus {
  running: boolean;
  now: string;
  next_scan: string | null;
  next_send: string | null;
  auto_send_enabled: boolean;
  window_saved: boolean;
  events: SchedulerEvent[];
}

export interface LlmField {
  label: string;
  value: string;
  choices?: string[];
  int?: [number, number];
  pattern?: string;
}

export interface SettingsData {
  error: string | null;
  automation: Automation;
  automation_runs: Record<string, string>;
  trading: Trading;
  trading_saved: boolean;
  llm: Record<string, LlmField>;
  keys: { name: string; label: string; set: boolean }[];
  provider_keys: Record<string, string | null>;
  scheduler: SchedulerStatus;
  env_path: string;
  settings_path: string;
}

export interface Health {
  ok: boolean;
  data_home: string;
  orders_enabled: boolean;
  scheduler_running: boolean;
}

// --- broker (paper, read-only) -------------------------------------------------------

export interface Account {
  account_number: string | null;
  status: string | null;
  paper: boolean;
  equity: number | null;
  last_equity: number | null;
  buying_power: number | null;
  cash: number | null;
  daily_change: number | null;
  daily_change_pct: number | null;
}

export interface HistoryPoint {
  t: number;
  equity: number | null;
  pnl: number | null;
  pnl_pct: number | null;
}

export type HistoryRange = "1D" | "1W" | "1M" | "1Y";

export interface History {
  range: HistoryRange;
  timeframe: string;
  base_value: number | null;
  points: HistoryPoint[];
}

export interface Position {
  symbol: string;
  side: "Long" | "Short";
  qty: number | null;
  avg_entry_price: number | null;
  market_value: number | null;
  current_price: number | null;
  today_pl: number | null;
  today_pl_pct: number | null;
  total_pl: number | null;
  total_pl_pct: number | null;
}

export interface Order {
  id: string;
  symbol: string;
  position_side: "Long" | "Short" | null;
  side: string | null;
  qty: number | null;
  filled_qty: number | null;
  filled_avg_price: number | null;
  status: string;
  filled: boolean;
  type: string | null;
  submitted_at: string | null;
  filled_at: string | null;
}

// --- pipeline files ------------------------------------------------------------------

export interface Artifact<T> {
  file: string;
  path: string;
  prefix: string | null;
  as_of: string | null;
  data?: T;
  error?: string;
}

export interface BasketMember {
  ticker: string;
  score: number;
  metrics: Record<string, number | string | null>;
}

export interface Basket {
  scan_name: string;
  as_of: string;
  created_at: string;
  universe?: string[];
  parameters?: Record<string, unknown>;
  members: BasketMember[];
  rejected: { ticker: string; reason: string }[];
}

export interface ChannelCall {
  passed: boolean;
  reasons: string[];
  detail: string[];
  quality: Record<string, boolean>;
  quality_passed: number;
  metrics: Record<string, unknown>;
}

/** A sideways channel scan report (SCAN-LongSidewaysChannel / SCAN-ShortSidewaysChannel), members' calls only. */
export interface ChannelReport {
  scan_name: string;
  mode: string;
  as_of: string;
  created_at: string;
  financials_report?: string;
  policy?: Record<string, unknown>;
  stage_counts: Record<string, number>;
  reason_counts: Record<string, number>;
  quality_counts: Record<string, number>;
  members: string[];
  member_calls: Record<string, ChannelCall | null>;
  scanned: number;
  warnings?: string[];
}

export type ScanKind = "all" | "both" | "sideways" | "up" | "down" | "long_side" | "short_side";

export interface Regime {
  symbol: string;
  as_of: string;
  created_at: string;
  label: "UP" | "DOWN" | "SIDE";
  reasons: string[];
  metrics: Record<string, number | string | boolean | null>;
  parameters?: Record<string, unknown>;
}

export interface Route {
  as_of: string;
  created_at: string;
  series: string;
  regime_symbol: string;
  regime_label: "UP" | "DOWN" | "SIDE";
  up_allowed: boolean;
  down_allowed?: boolean;
  tradeable: string[];
  blocked: string[];
  up_passed?: string[];
  down_passed?: string[];
  reason: string;
}

export interface Plan {
  file: string;
  bot: string | null;
  symbol: string | null;
  as_of: string | null;
  stamp: string | null;
  title: string;
  fields: Record<string, string>;
  text: string;
}

export interface Summary {
  as_of: string | null;
  regime: Artifact<Regime> | null;
  baskets: { up?: Artifact<Basket> | null; down?: Artifact<Basket> | null };
  route: Artifact<Route> | null;
  plans: Plan[];
}

// --- research ------------------------------------------------------------------------

export interface ResearchRun {
  ticker: string;
  date: string;
  modified: string;
  rating?: string | null;
  error?: string;
}

export interface StateLog {
  company_of_interest: string;
  trade_date: string;
  market_report?: string;
  sentiment_report?: string;
  news_report?: string;
  fundamentals_report?: string;
  investment_debate_state?: Record<string, string>;
  trader_investment_plan?: string;
  risk_debate_state?: Record<string, string>;
  investment_plan?: string;
  final_trade_decision?: string;
  final_rating?: string;
  run_settings?: Record<string, unknown>;
}

export interface ReportDir {
  name: string;
  ticker: string;
  created: string;
}

// --- jobs ----------------------------------------------------------------------------

export interface Job {
  id: string;
  kind: string;
  status: "queued" | "running" | "ok" | "failed" | "blocked";
  exit_code: number | null;
  exit_meaning: string | null;
  steps: { script: string; args: string[]; exit_code: number | null }[];
  created_at: string;
  finished_at: string | null;
}

export const api = {
  account: () => get<Account>("/api/broker/account"),
  history: (range: HistoryRange) => get<History>(`/api/broker/history?range=${range}`),
  positions: () => get<Position[]>("/api/broker/positions"),
  orders: () => get<Order[]>("/api/broker/orders"),

  dates: () => get<string[]>("/api/dates"),
  summary: (asOf?: string) => get<Summary>(`/api/summary${asOf ? `?as_of=${asOf}` : ""}`),
  baskets: (kind?: "up" | "down") => get<Artifact<Basket>[]>(`/api/baskets${kind ? `?kind=${kind}` : ""}`),
  channelScans: (side: "long" | "short") => get<Artifact<ChannelReport>[]>(`/api/channel-scans?side=${side}`),
  regimes: () => get<Artifact<Regime>[]>("/api/regimes"),
  routes: () => get<Artifact<Route>[]>("/api/routes"),
  plans: () => get<Plan[]>("/api/plans"),

  researchRuns: () => get<ResearchRun[]>("/api/research/runs"),
  researchState: (ticker: string, date: string) => get<StateLog>(`/api/research/state/${ticker}/${date}`),
  reports: () => get<ReportDir[]>("/api/research/reports"),
  report: (name: string) => get<{ name: string; sections: Record<string, string> }>(`/api/research/reports/${name}`),

  jobs: () => get<Job[]>("/api/jobs"),
  health: () => get<Health>("/api/health"),

  settings: () => get<SettingsData>("/api/settings"),
  saveAutomation: (body: Automation) => put<Automation>("/api/settings/automation", body),
  saveTrading: (body: Trading) => put<Trading>("/api/settings/trading", body),
  saveLlm: (values: Record<string, string>) => put<{ ok: true }>("/api/settings/llm", { values }),
  saveKeys: (body: { set: Record<string, string>; clear: string[] }) =>
    put<{ ok: true; saved: string[]; cleared: string[] }>("/api/settings/keys", body),

  runScans: (body: { as_of: string; kind: ScanKind; tickers: string[] }) =>
    post<Job>("/api/scans/run", body),
  runPipeline: (body: { as_of: string; tickers: string[]; regime_symbol: string }) =>
    post<Job>("/api/pipeline/run", body),
  runRegime: (body: { as_of: string; symbol: string }) => post<Job>("/api/regime/run", body),
  runRouter: (body: { as_of: string }) => post<Job>("/api/router/run", body),
  runBot: (body: { as_of: string; bot: "up" | "down" | "side"; symbol: string }) =>
    post<Job>("/api/bots/run", body),
  runResearch: (body: { as_of: string; ticker: string; analysts: string[]; confirm: true }) =>
    post<Job>("/api/research/run", body),
};

/** Last completed US trading-ish day as YYYY-MM-DD in New York (skips weekends only). */
export function defaultAsOf(): string {
  const ny = new Date(new Date().toLocaleString("en-US", { timeZone: "America/New_York" }));
  ny.setDate(ny.getDate() - 1);
  while (ny.getDay() === 0 || ny.getDay() === 6) ny.setDate(ny.getDate() - 1);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${ny.getFullYear()}-${pad(ny.getMonth() + 1)}-${pad(ny.getDate())}`;
}

export function parseTickers(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((t) => t.trim().toUpperCase())
    .filter(Boolean);
}
