// Typed client for the local FastAPI server. Every broker call here is a GET;
// the UI has no request that places, changes, cancels or closes an order itself.
// sendManualTicket starts paper_orders.sender on one manual ticket; the sender does the paper checks.
// flatten starts paper_orders.flatten (the kill switch) as a job; the server turns the kill switch on first.
// tradebotSend starts paper_orders.sender --tradebot --send on a confirmed request; the sender runs the risk guard.

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

const del = <T>(path: string) => request<T>(path, { method: "DELETE" });

// --- queues (Scans page "Send to") ------------------------------------------------------

// Category baskets from run_category_scan: TickerBaskets, one kind per regime and side.
export type CategoryBasketKind = "up_cat" | "down_cat" | "side_long_cat" | "side_short_cat";
export type BasketKind = "up" | "down" | CategoryBasketKind;
export type ScanSource = BasketKind | "long" | "short";
export type Category = 1 | 2 | 3;
export const CATEGORIES: Category[] = [1, 2, 3];
export const CATEGORY_LABEL: Record<Category, string> = { 1: "1 · strictest", 2: "2 · balanced", 3: "3 · loosest" };
export type QueueName = "research" | "strategy" | "trade";

export interface QueueItem {
  id: string;
  ticker: string;
  as_of: string | null;
  kind: ScanSource;
  scan: string;
  scan_file: string;
  side: "long" | "short";
  needs_regime: string;
  metrics: Record<string, number | null>;
  added_at: string;
  route_status?: "ALLOWED" | "BLOCKED" | "NOT_ROUTED" | "NO_ROUTE";
  regime_label?: string | null;
  regime_source?: string | null;
  verdict?: "ALLOWED" | "BLOCKED" | "NO_REGIME";
  reason?: string;
}

// --- manual paper tickets (Trade page order entry) ------------------------------------

export interface ManualTicket {
  name: string;
  id: string;
  as_of: string;
  created_at: string | null;
  source: { queue_item?: string; scan?: string; scan_file?: string; kind?: ScanSource };
  symbol: string;
  side: "long" | "short";
  order_side: "buy" | "sell";
  quantity: number;
  entry: number;
  stop: number;
  stop_offset: number;
  target: number;
  risk_dollars: number;
  notional: number;
  reward_risk: number;
  sends: { job_id: string; at: string }[];
  error?: string;
}

export interface NewManualTicket {
  item_id: string;
  entry: number;
  stop: number;
  target: number;
  quantity: number;
}

// --- settings ------------------------------------------------------------------------

export interface Automation {
  enabled: boolean;
  days: string[];
  times: string[];
  run: "daily" | "category_scans";
  category: Category; // used by the category_scans run; saved on the server, so the scheduler reads it
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
  next_scan_as_of: string | null;
  next_send: string | null;
  last_tick: string | null;
  tradebot_mode: TradebotMode;
  next_intraday: string | null;
  auto_send_sends: boolean;
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

export interface Risk {
  kill_switch: boolean;
  daily_loss_pct: number;
  drawdown_pct: number;
  max_positions: number;
  max_gross_exposure_pct: number;
  kill_switch_at?: string;
  kill_switch_reason?: string;
}

export interface RiskStatus {
  risk: Risk | null;
  error: string | null;
  state: { peak_equity: number | null; peak_at: string | null } | null;
  state_error: string | null;
  kill_switch: boolean | null;
}

export interface LedgerLine {
  at: string;
  mode: "tradebot" | "daily" | "manual" | "flatten";
  kind: "entry" | "exit" | "guard" | "flatten";
  symbol: string | null;
  client_order_id: string | null;
  item_id: string | null;
  action: string | null;
  status: string | null;
  qty: string | null;
  filled_qty: string | null;
  filled_avg_price: string | null;
  stop: number | null;
  reason: string | null;
}

export interface ReconcileProblem {
  type: string;
  symbol: string | null;
  detail: string;
  client_order_id: string | null;
  item_id: string | null;
}

export interface ReconcileReport {
  created_at: string;
  after: string;
  window_days: number;
  counts: Record<string, number>;
  problems: ReconcileProblem[];
  clean: boolean;
  by_type: Record<string, number>;
}

export type TradebotStatus = "waiting" | "armed" | "triggered" | "ticketed" | "sent" | "expired" | "blocked" | "removed";
export type TradebotMode = "manual" | "semi" | "auto";
export const TRADEBOT_FILTERS = ["squeeze_release", "rsi_extreme", "light_volume", "squeeze_4h1h"] as const;

export interface TradebotCheck {
  at: string;
  stage: "eod" | "intraday" | "ticket";
  status: TradebotStatus | null;
  reason: string;
  passed: boolean;
  gates?: Record<string, unknown>;
  daily?: Record<string, unknown> | null;
  squeeze?: Record<string, unknown> | null;
  filters?: Record<string, boolean>;
}

export interface TradebotItem {
  id: string;
  ticker: string;
  as_of: string;
  kind: string;
  scan: string;
  side: "long" | "short";
  bot: "UP" | "DOWN" | "SIDE";
  source: "auto" | "manual";
  status: TradebotStatus;
  status_reason: string | null;
  expires_after: string | null;
  sessions_left: number | null;
  last_check: TradebotCheck | null;
  checks: TradebotCheck[];
  ticket: { file: string; client_order_id: string; exit_order_id: string; risk_dollars: number; entry?: number;
            stop?: number; target?: number; quantity?: number; exit_done?: boolean } | null;
  last_guard: { at: string; decision: "allowed" | "skipped" | "blocked"; reason: string | null } | null;
}

export interface TradebotSettings {
  mode: TradebotMode;
  expire_sessions: number;
  max_enqueue: number;
  filters: Record<(typeof TRADEBOT_FILTERS)[number], boolean>;
  earnings_unknown: "block" | "allow";
  earnings_cache_max_days: number;
  intraday_times: string[];
}

export interface SettingsData {
  tradebot: TradebotSettings | null;
  tradebot_modes: Record<TradebotMode, string>;
  auto_blockers: string[];
  error: string | null;
  risk: Risk | null;
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
  scheduler_refused: string | null;
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

// Scan picks the server will start. The five-name trend kinds (all, both, up, down) are retired.
export type CategoryScanKind = "cat_up" | "cat_down" | "cat_side" | "cat_all";
export type ScanKind = "sideways" | "long_side" | "short_side" | CategoryScanKind;
export const CATEGORY_SCAN_KINDS: ScanKind[] = ["cat_up", "cat_down", "cat_side", "cat_all"];
export const SCAN_KINDS: ScanKind[] = [...CATEGORY_SCAN_KINDS, "sideways", "long_side", "short_side"];

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
  steps: JobStep[];
  current_step?: number | null; // index of the step running now
  created_at: string;
  finished_at: string | null;
}

export interface JobStep {
  script: string;
  args: string[];
  exit_code: number | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export const api = {
  account: () => get<Account>("/api/broker/account"),
  history: (range: HistoryRange) => get<History>(`/api/broker/history?range=${range}`),
  positions: () => get<Position[]>("/api/broker/positions"),
  orders: () => get<Order[]>("/api/broker/orders"),

  dates: () => get<string[]>("/api/dates"),
  summary: (asOf?: string) => get<Summary>(`/api/summary${asOf ? `?as_of=${asOf}` : ""}`),
  baskets: (kind?: BasketKind, category?: Category) => {
    const q = new URLSearchParams();
    if (kind) q.set("kind", kind);
    if (category) q.set("category", String(category));
    const qs = q.toString();
    return get<Artifact<Basket>[]>(`/api/baskets${qs ? `?${qs}` : ""}`);
  },
  queues: () => get<Record<QueueName, QueueItem[]>>("/api/queues"),
  sendTo: (queue: QueueName, body: { kind: ScanSource; file: string; tickers: string[] }) =>
    post<{ queue: QueueName; added: string[]; already_queued: string[] }>(`/api/queues/${queue}`, body),
  removeQueued: (queue: QueueName, id: string) => del<{ removed: boolean }>(`/api/queues/${queue}/${id}`),
  trashScan: (body: { kind: ScanSource; file: string; ticker?: string }) => post<unknown>("/api/scans/trash", body),
  manualTickets: () => get<ManualTicket[]>("/api/trade/manual"),
  createManualTicket: (body: NewManualTicket) => post<ManualTicket>("/api/trade/manual", body),
  deleteManualTicket: (name: string) => del<{ deleted: string }>(`/api/trade/manual/${encodeURIComponent(name)}`),
  // send: false is a dry run. send: true needs confirm: true, and starts the sender with --send.
  sendManualTicket: (name: string, body: { send: false } | { send: true; confirm: true }) =>
    post<Job>(`/api/trade/manual/${encodeURIComponent(name)}/send`, body),
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
  saveRisk: (body: Partial<Risk>) => put<Risk>("/api/settings/risk", body),
  risk: () => get<RiskStatus>("/api/risk"),
  saveTradebot: (body: TradebotSettings) => put<TradebotSettings>("/api/settings/tradebot", body),
  tradebotQueue: () => get<TradebotItem[]>("/api/tradebot/queue"),
  tradebotCheck: (body: { stage: "eod" | "intraday"; item?: string }) => post<Job>("/api/tradebot/check", body),
  tradebotTicket: (body: { item?: string }) => post<Job>("/api/tradebot/ticket", body),
  // levels and size (nothing written), then the risk guard on the ticketed items (GET only); nothing is sent.
  tradebotPreview: (body: { item?: string }) => post<Job>("/api/tradebot/preview", body),
  // send: true needs confirm: true and starts paper_orders.sender --tradebot --send.
  tradebotSend: (body: { send: true; confirm: true; item?: string }) => post<Job>("/api/tradebot/send", body),
  ledger: (month?: string) => get<{ month: string | null; lines: LedgerLine[] }>(`/api/orders/ledger${month ? `?month=${month}` : ""}`),
  reconcileReport: () => get<{ file: string | null; report: ReconcileReport | null }>("/api/orders/reconcile"),
  // reconcile reads the paper account (GET only) and writes a report; it changes no order.
  runReconcile: () => post<Job>("/api/orders/reconcile/run", {}),
  setKillSwitch: (body: { on: boolean; reason: string }) => put<Risk>("/api/risk/kill-switch", body),
  // send: false lists what would be cancelled and closed. send: true needs confirm: true.
  flatten: (body: { send: false } | { send: true; confirm: true; reason: string }) => post<Job>("/api/risk/flatten", body),
  saveLlm: (values: Record<string, string>) => put<{ ok: true }>("/api/settings/llm", { values }),
  saveKeys: (body: { set: Record<string, string>; clear: string[] }) =>
    put<{ ok: true; saved: string[]; cleared: string[] }>("/api/settings/keys", body),

  // category is read for the category kinds only.
  runScans: (body: { as_of: string; kind: ScanKind; category?: Category }) =>
    post<Job>("/api/scans/run", body),
  // The upward and downward category scans at `category`, RegimeBot, then the router on those baskets.
  runPipeline: (body: { as_of: string; regime_symbol: string; category?: Category }) =>
    post<Job>("/api/pipeline/run", body),
  runRegime: (body: { as_of: string; symbol: string }) => post<Job>("/api/regime/run", body),
  runRouter: (body: { as_of: string; category?: Category }) => post<Job>("/api/router/run", body),
  runBot: (body: { as_of: string; bot: "up" | "down" | "side"; symbol: string }) =>
    post<Job>("/api/bots/run", body),
  runResearch: (body: { as_of: string; ticker: string; analysts: string[]; confirm: true;
                        scan_kind?: ScanSource; scan_file?: string }) =>
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
