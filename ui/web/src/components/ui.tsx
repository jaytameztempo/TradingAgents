import { ReactNode, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { ApiError, Job } from "../api";
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

export function Dialog({ open, title, children, onClose, actions, tone }: {
  open: boolean;
  title: ReactNode;
  children: ReactNode;
  onClose: () => void;
  actions?: ReactNode;
  tone?: "danger";
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) el.showModal();
    if (!open && el.open) el.close();
  }, [open]);
  return (
    <dialog ref={ref} className={`dialog ${tone === "danger" ? "dialog-danger" : ""}`} onClose={onClose}>
      <h3>{title}</h3>
      <div className="dialog-body">{children}</div>
      <div className="dialog-actions">
        <button className="btn" onClick={onClose}>Cancel</button>
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

/** Streams a job's output over Server-Sent Events and calls onDone once with the final summary. */
export function JobLog({ job, onDone }: { job: Job | null; onDone?: (job: Job) => void }) {
  const [lines, setLines] = useState<string[]>([]);
  const [final, setFinal] = useState<Job | null>(null);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;
  const boxRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    setLines([]);
    setFinal(null);
    if (!job) return;
    const source = new EventSource(`/api/jobs/${job.id}/stream`);
    source.addEventListener("line", (e) => setLines((prev) => [...prev, JSON.parse((e as MessageEvent).data)]));
    source.addEventListener("done", (e) => {
      const summary = JSON.parse((e as MessageEvent).data) as Job;
      setFinal(summary);
      source.close();
      doneRef.current?.(summary);
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
      <pre ref={boxRef} className="joblog-lines">{lines.join("\n") || "Starting…"}</pre>
    </div>
  );
}
