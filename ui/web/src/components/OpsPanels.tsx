import { useQuery } from "@tanstack/react-query";
import { api, LedgerLine, TradebotStatus } from "../api";
import { Card, Column, DataTable, ErrorBox, JobLog, Stat } from "./ui";
import { Ticker } from "./Ticker";
import { dateTime } from "../format";
import { useJob } from "../useJob";

// Dashboard: the Tradebot queue at a glance, the scheduler's heartbeat, today's order ledger and the latest
// reconcile. All of it is read from disk; Run reconcile starts the read-only reconcile job.

const STALE_MS = 2 * 60_000;
const STATUSES: TradebotStatus[] = ["waiting", "armed", "triggered", "ticketed", "sent"];

function nyDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-CA", { timeZone: "America/New_York" });
}

export function OpsPanels() {
  const queue = useQuery({ queryKey: ["tradebot-queue"], queryFn: api.tradebotQueue, refetchInterval: 30_000 });
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings, refetchInterval: 30_000 });
  const ledger = useQuery({ queryKey: ["ledger"], queryFn: () => api.ledger(), refetchInterval: 30_000 });
  const report = useQuery({ queryKey: ["reconcile"], queryFn: api.reconcileReport, refetchInterval: 60_000 });
  const run = useJob();
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 30_000 });

  const s = settings.data?.scheduler;
  const tick = s?.last_tick ? Date.parse(s.last_tick) : null;
  const stale = !s?.running || tick === null || Date.now() - tick > STALE_MS;
  const today = nyDate(new Date().toISOString());
  const lines = (ledger.data?.lines ?? []).filter((l) => nyDate(l.at) === today);
  const r = report.data?.report;

  const cols: Column<LedgerLine>[] = [
    { key: "at", label: "Time", render: (l) => dateTime(l.at), sortValue: (l) => l.at },
    { key: "sym", label: "Ticker", render: (l) => (l.symbol ? <Ticker symbol={l.symbol} /> : "—") },
    { key: "kind", label: "Kind", render: (l) => `${l.mode} · ${l.kind}` },
    { key: "action", label: "What happened", render: (l) => l.action ?? "—" },
    { key: "status", label: "Status", render: (l) => l.status ?? "—" },
    { key: "fill", label: "Filled", align: "right",
      render: (l) => (l.filled_qty && Number(l.filled_qty) > 0 ? `${l.filled_qty} @ ${l.filled_avg_price}` : "—") },
    { key: "why", label: "Reason", render: (l) => <span className="muted">{l.reason ?? ""}</span> },
  ];

  return (
    <>
      <Card title="Tradebot and scheduler">
        <ErrorBox error={queue.error ?? settings.error} />
        {health.data?.scheduler_refused && <div className="notice notice-error" style={{ marginBottom: 12 }}>
          {health.data.scheduler_refused}</div>}
        <div className="stats">
          {STATUSES.map((st) => (
            <Stat key={st} label={st} value={(queue.data ?? []).filter((i) => i.status === st).length} />
          ))}
          <Stat label="Mode" value={s?.tradebot_mode ?? "—"}
                sub={s?.auto_send_sends ? "sends automatically" : "no automatic sends"} />
          <Stat label="Scheduler" value={<span className={stale ? "delta-down" : "delta-up"}>{stale ? "not ticking" : "ticking"}</span>}
                sub={s?.last_tick ? `last tick ${dateTime(s.last_tick)}` : "no tick yet"} />
        </div>
        {stale && <div className="notice notice-error" style={{ marginTop: 12 }}>
          The scheduler has not ticked in the last 2 minutes{s?.running === false ? " (its thread is not running)" : ""}: no
          scheduled check, send or reconcile will run until the UI server is running again.</div>}
      </Card>
      <Card title="Reconcile" actions={
        <button className="btn btn-sm" disabled={run.running} onClick={() => run.start(api.runReconcile)}>Run reconcile</button>}>
        <ErrorBox error={report.error ?? run.error} />
        <JobLog job={run.job} onDone={(j) => { run.onDone(j); report.refetch(); }} />
        {!r ? <p className="muted">No reconcile yet.</p> : r.clean ? (
          <p><span className="badge tone-good">clean</span> {dateTime(r.created_at)}: the paper account matches the tickets and the queue
            ({r.counts.positions} positions, {r.counts.open_orders} open orders).</p>
        ) : (
          <div className="notice notice-error">
            <strong>{r.problems.length} problem{r.problems.length === 1 ? "" : "s"}</strong> at {dateTime(r.created_at)}:
            <ul>{r.problems.map((p, n) => <li key={n}><code>{p.type}</code> {p.symbol ?? ""}: {p.detail}</li>)}</ul>
          </div>
        )}
      </Card>
      <Card title="Today's order ledger">
        <ErrorBox error={ledger.error} />
        <DataTable columns={cols} rows={lines} rowKey={(l) => `${l.at}-${l.kind}-${l.client_order_id ?? l.symbol}-${l.action}`}
                   empty="Nothing sent or held back today." />
      </Card>
    </>
  );
}
