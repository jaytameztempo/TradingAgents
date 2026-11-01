import { ReactNode, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { ApiError, Job, JobStep } from "../api";
import { direction } from "../format";

export function Card({ title, actions, children, className = "" }: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card-head">
          {title && <h2>{title}</h2>}
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Stat({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

/** A signed number with an arrow and sign, so gain/loss never rides on colour alone. */
export function Delta({ value, children }: { value: number | null | undefined; children: ReactNode }) {
  const dir = direction(value);
  const arrow = dir === "up" ? "▲" : dir === "down" ? "▼" : "";
  return (
    <span className={`delta delta-${dir}`}>
      {arrow && <span aria-hidden="true">{arrow} </span>}
      {children}
    </span>
  );
}

export function RegimeBadge({ label }: { label?: string | null }) {
  if (!label) return <span className="badge">—</span>;
  const icon = label === "UP" ? "▲" : label === "DOWN" ? "▼" : "◆";
  return <span className={`badge regime-${label.toLowerCase()}`}>{icon} {label}</span>;
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const message = error instanceof Error ? error.message : String(error);
  return <div className="notice notice-error" role="alert">{message}</div>;
}

export function isMissingKeys(error: unknown): boolean {
  return error instanceof ApiError && error.code === "missing_keys";
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Tabs<T extends string>({ tabs, value, onChange }: {
  tabs: { id: T; label: ReactNode }[];
  value: T;
  onChange: (id: T) => void;
}) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={value === t.id}
          className={`tab ${value === t.id ? "active" : ""}`}
          onClick={() => onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Markdown({ text }: { text?: string | null }) {
  if (!text) return <Empty>Nothing in this section.</Empty>;
  return (
    <div className="markdown">
      <ReactMarkdown>{text}</ReactMarkdown>
    </div>
  );
}

// --- sortable table ------------------------------------------------------------------

export interface Column<R> {
  key: string;
  label: ReactNode;
  render: (row: R) => ReactNode;
  sortValue?: (row: R) => number | string | null | undefined;
  align?: "left" | "right" | "center";
}

export function DataTable<R>({ columns, rows, rowKey, empty, initialSort }: {
  columns: Column<R>[];
  rows: R[];
  rowKey: (row: R) => string;
  empty?: ReactNode;
  initialSort?: { key: string; dir: "asc" | "desc" };
}) {
  const [sort, setSort] = useState(initialSort ?? null);
  const sorted = useMemo(() => {
    const col = sort && columns.find((c) => c.key === sort.key);
    if (!col?.sortValue) return rows;
    const val = col.sortValue;
    return [...rows].sort((a, b) => {
      const x = val(a), y = val(b);
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      const cmp = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y));
      return sort!.dir === "asc" ? cmp : -cmp;
    });
  }, [rows, sort, columns]);

  if (!rows.length) return <Empty>{empty ?? "Nothing to show."}</Empty>;
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            {columns.map((c) => {
              const active = sort?.key === c.key;
              return (
                <th key={c.key} className={`align-${c.align ?? "left"}`}
                    aria-sort={active ? (sort!.dir === "asc" ? "ascending" : "descending") : undefined}>
                  {c.sortValue ? (
                    <button className="th-sort" onClick={() =>
                      setSort({ key: c.key, dir: active && sort!.dir === "desc" ? "asc" : "desc" })}>
                      {c.label}
                      <span className="sort-ind" aria-hidden="true">{active ? (sort!.dir === "asc" ? "↑" : "↓") : "↕"}</span>
                    </button>
                  ) : c.label}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => (
            <tr key={rowKey(r)}>
              {columns.map((c) => <td key={c.key} className={`align-${c.align ?? "left"}`}>{c.render(r)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --- dialog --------------------------------------------------------------------------

export function Dialog({ open, title, children, onClose, actions, tone, wide, closeLabel = "Cancel" }: {
  open: boolean;
  title: ReactNode;
  children: ReactNode;
  onClose: () => void;
  actions?: ReactNode;
  tone?: "danger";
  wide?: boolean;
  closeLabel?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) el.showModal();
    if (!open && el.open) el.close();
  }, [open]);
  return (
    <dialog ref={ref} className={`dialog ${tone === "danger" ? "dialog-danger" : ""} ${wide ? "dialog-wide" : ""}`}
            onClose={onClose}
            onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <h3>{title}</h3>
      <div className="dialog-body">{open && children}</div>
      <div className="dialog-actions">
        <button className="btn" onClick={onClose}>{closeLabel}</button>
        {actions}
      </div>
    </dialog>
  );
}

// --- job log (SSE) -------------------------------------------------------------------

const STATUS_TEXT: Record<Job["status"], string> = {
  queued: "Queued",
  running: "Running…",
  ok: "Finished",
  failed: "Failed",
  blocked: "Blocked: regime mismatch",
};

const CATEGORY_SCAN_LABEL: Record<string, string> = { up: "upward", down: "downward", side: "sideways (long + short)" };

/** What each script is, in words, and how long it usually takes (from runs on this machine; omitted when unknown). */
function stepInfo(step: JobStep): { label: string; typical?: string } {
  switch (step.script) {
    case "run_scanbot_universe":
      return { label: "Universe and liquidity (Alpaca bars and quotes)", typical: "usually about 30 min" };
    case "run_scanbot_financials":
      return { label: "Financials (SEC filings)", typical: "usually 5–15 min" };
    case "run_category_scan": {
      const scan = step.args[step.args.indexOf("--scan") + 1];
      const category = step.args[step.args.indexOf("--category") + 1];
      return { label: `Category ${category} scan: ${CATEGORY_SCAN_LABEL[scan] ?? scan}`, typical: "usually under a minute" };
    }
    case "run_scanbot_long_channel":
      return { label: "Long sideways channel scan" };
    case "run_scanbot_short_channel":
      return { label: "Short sideways channel scan" };
    case "run_regime_bot":
      return { label: "RegimeBot", typical: "usually seconds" };
    case "run_router":
      return { label: "Router", typical: "usually seconds" };
    case "run_daily":
      return { label: "Daily SIDE pipeline" };
    default:
      return { label: step.script };
  }
}

/** "1:05", "12:40", "1:02:03". */
function duration(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

const since = (iso: string | null | undefined, end: number) => (iso ? end - Date.parse(iso) : 0);

// Scripts report progress as a line "  <what> <done>/<total>" (fundamentals 600/1234, bars 4000/8763, ...).
const PROGRESS_LINE = /^\s*([A-Za-z][A-Za-z ]*?)\s+(\d+)\/(\d+)\s*$/;

/** The newest progress line printed since the current step's command line, if any. */
function latestProgress(lines: string[]): { what: string; done: number; total: number } | null {
  for (let i = lines.length - 1; i >= 0 && !lines[i].startsWith("$ python -m"); i--) {
    const m = PROGRESS_LINE.exec(lines[i]);
    if (m && Number(m[3]) > 0) return { what: m[1], done: Number(m[2]), total: Number(m[3]) };
  }
  return null;
}

/** Step N of M, the step's name, timers, a usual-duration hint, a progress bar when the step reports one, and a
 *  checklist of every step. */
function JobProgress({ summary, lines, running }: { summary: Job; lines: string[]; running: boolean }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);

  const steps = summary.steps;
  const current = summary.current_step ?? null;
  const end = running ? now : Date.parse(summary.finished_at ?? "") || now;
  const total = since(steps[0]?.started_at ?? summary.created_at, end);
  const step = current != null ? steps[current] : null;
  const info = step ? stepInfo(step) : null;
  const progress = running && step ? latestProgress(lines) : null;
  const pct = progress ? Math.min(100, Math.round((100 * progress.done) / progress.total)) : 0;

  return (
    <div className="joblog-progress">
      {running && step && info ? (
        <div>
          <strong>Step {current! + 1} of {steps.length}</strong> · {info.label}
          <span className="muted"> · {duration(since(step.started_at, now))} on this step
            {info.typical ? ` (${info.typical})` : ""} · {duration(total)} in total</span>
        </div>
      ) : (
        <div className="muted">
          {running ? "Starting…" : `${steps.filter((s) => s.finished_at).length} of ${steps.length} steps ran · ${duration(total)} in total`}
        </div>
      )}
      {progress && (
        <div className="progress-row">
          <div className="progress" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}
               aria-label={`${progress.what} progress`}>
            <div className="progress-fill" style={{ width: `${pct}%` }} />
          </div>
          <span className="muted">{progress.what} {progress.done.toLocaleString()}/{progress.total.toLocaleString()} ({pct}%)</span>
        </div>
      )}
      {steps.length > 1 && (
        <ol className="joblog-steps">
          {steps.map((s, i) => {
            const state = s.finished_at ? (s.exit_code === 0 ? "done" : "failed") : i === current ? "current" : "pending";
            const mark = { done: "✓", failed: "✕", current: "●", pending: "○" }[state];
            return (
              <li key={i} className={`step-${state}`}>
                {mark} {stepInfo(s).label}
                {s.finished_at && s.started_at ? <span className="muted"> {duration(since(s.started_at, Date.parse(s.finished_at)))}</span> : null}
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

/** Streams a job's output over Server-Sent Events and calls onDone once with the final summary. */
export function JobLog({ job, onDone }: { job: Job | null; onDone?: (job: Job) => void }) {
  const [lines, setLines] = useState<string[]>([]);
  const [summary, setSummary] = useState<Job | null>(null);
  const [final, setFinal] = useState<Job | null>(null);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;
  const boxRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    setLines([]);
    setSummary(null);
    setFinal(null);
    if (!job) return;
    const source = new EventSource(`/api/jobs/${job.id}/stream`);
    source.addEventListener("line", (e) => setLines((prev) => [...prev, JSON.parse((e as MessageEvent).data)]));
    source.addEventListener("steps", (e) => setSummary(JSON.parse((e as MessageEvent).data) as Job));
    source.addEventListener("done", (e) => {
      const done = JSON.parse((e as MessageEvent).data) as Job;
      setSummary(done);
      setFinal(done);
      source.close();
      doneRef.current?.(done);
    });
    source.onerror = () => source.close();
    return () => source.close();
  }, [job?.id]);

  useEffect(() => {
    if (boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
  }, [lines]);

  if (!job) return null;
  const status = final?.status ?? "running";
  return (
    <div className="joblog">
      <div className={`joblog-status status-${status}`}>
        <span>{STATUS_TEXT[status]}</span>
        <span className="muted">{job.kind}{final?.exit_code != null ? ` · exit ${final.exit_code}` : ""}</span>
      </div>
      <JobProgress summary={summary ?? job} lines={lines} running={!final} />
      <pre ref={boxRef} className="joblog-lines">{lines.join("\n") || "Starting…"}</pre>
    </div>
  );
}
