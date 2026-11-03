import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, TradebotItem, TradebotStatus } from "../api";
import { Card, Column, DataTable, Dialog, ErrorBox, JobLog } from "./ui";
import { Ticker } from "./Ticker";
import { dateTime, money } from "../format";
import { useJob } from "../useJob";

// The Tradebot queue: names the router allowed wait here until their entry triggers fire. Every button starts
// the same command the scheduler runs, as a job; only a confirmed Send starts paper_orders.sender --send.

const TONE: Record<TradebotStatus, string> = {
  waiting: "", armed: "tone-info", triggered: "tone-warn", ticketed: "tone-warn", sent: "tone-good",
  expired: "", blocked: "tone-bad", removed: "",
};
const ACTIVE: TradebotStatus[] = ["waiting", "armed", "triggered", "ticketed", "sent"];

function StatusChip({ status }: { status: TradebotStatus }) {
  return <span className={`badge ${TONE[status]}`}>{status}</span>;
}

function GuardChip({ guard }: { guard: TradebotItem["last_guard"] }) {
  if (!guard) return <span className="muted">—</span>;
  const tone = guard.decision === "allowed" ? "tone-good" : "tone-bad";
  return (
    <span className={`badge ${tone}`} title={`${guard.reason ?? "allowed"} (${dateTime(guard.at)})`}>
      {guard.decision === "allowed" ? "allowed" : `${guard.decision}: ${guard.reason ?? ""}`.slice(0, 60)}
    </span>
  );
}

function Levels({ t }: { t: TradebotItem["ticket"] }) {
  if (!t) return <span className="muted">—</span>;
  return (
    <span className="chip" title={t.client_order_id}>
      {t.quantity ?? "?"} @ {t.entry?.toFixed(2) ?? "?"} · stop {t.stop?.toFixed(2) ?? "?"} · target {t.target?.toFixed(2) ?? "?"}
      {" · risk "}{money(t.risk_dollars)}{t.exit_done ? " · exit on" : ""}
    </span>
  );
}

export function TradebotQueue() {
  const queue = useQuery({ queryKey: ["tradebot-queue"], queryFn: api.tradebotQueue, refetchInterval: 30_000 });
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const job = useJob();
  const [showDone, setShowDone] = useState(false);
  const [open, setOpen] = useState<TradebotItem | null>(null);
  const [confirmSend, setConfirmSend] = useState(false);
  const rows = (queue.data ?? []).filter((i) => showDone || ACTIVE.includes(i.status));
  const count = (s: TradebotStatus) => (queue.data ?? []).filter((i) => i.status === s).length;
  const ticketed = count("ticketed");
  const mode = settings.data?.tradebot?.mode ?? "manual";

  const columns: Column<TradebotItem>[] = [
    { key: "ticker", label: "Ticker", render: (i) => <Ticker symbol={i.ticker} />, sortValue: (i) => i.ticker },
    { key: "bot", label: "Bot / side", render: (i) => `${i.bot} · ${i.side}`, sortValue: (i) => i.bot },
    { key: "status", label: "Status", render: (i) => <StatusChip status={i.status} />, sortValue: (i) => i.status },
    { key: "left", label: "Sessions left", align: "right", render: (i) => i.sessions_left ?? "—",
      sortValue: (i) => i.sessions_left },
    { key: "why", label: "Last check", render: (i) => (
        <button className="link" onClick={() => setOpen(i)} title="Show every check">
          {(i.status_reason ?? i.last_check?.reason ?? "not checked yet").slice(0, 80)}
        </button>) },
    { key: "ticket", label: "Ticket", render: (i) => <Levels t={i.ticket} /> },
    { key: "guard", label: "Guard", render: (i) => <GuardChip guard={i.last_guard} /> },
    { key: "act", label: "", render: (i) => (
        <span className="row">
          <button className="btn btn-sm" disabled={job.running || !["waiting", "armed"].includes(i.status)}
                  onClick={() => job.start(() => api.tradebotCheck({ stage: "eod", item: i.id }))}>Check</button>
          <button className="btn btn-sm" disabled={job.running || ACTIVE.indexOf(i.status) < 0}
                  onClick={async () => { await api.removeQueued("trade", i.id); queue.refetch(); }}>Remove</button>
        </span>) },
  ];

  return (
    <Card title="Tradebot queue" actions={
      <label className="muted"><input type="checkbox" checked={showDone} onChange={(e) => setShowDone(e.target.checked)} /> show expired / blocked</label>}>
      <p className="muted">
        Mode <strong>{mode}</strong>{mode !== "manual" && settings.data?.scheduler.next_intraday
          ? ` · next intraday check ${dateTime(settings.data.scheduler.next_intraday)}` : ""}.
        {" "}waiting {count("waiting")} · armed {count("armed")} · triggered {count("triggered")} · ticketed {ticketed} · sent {count("sent")}
      </p>
      <div className="row" style={{ marginBottom: 12, flexWrap: "wrap" }}>
        <button className="btn" disabled={job.running} onClick={() => job.start(() => api.tradebotCheck({ stage: "eod" }))}>Check EOD</button>
        <button className="btn" disabled={job.running} onClick={() => job.start(() => api.tradebotCheck({ stage: "intraday" }))}>Check intraday</button>
        <button className="btn" disabled={job.running} onClick={() => job.start(() => api.tradebotPreview({}))}
                title="Levels and size for triggered names, and the risk guard on ticketed ones. Nothing is written or sent.">Preview</button>
        <button className="btn" disabled={job.running || count("triggered") === 0}
                onClick={() => job.start(() => api.tradebotTicket({}))}>Ticket triggered</button>
        <button className="btn btn-danger" disabled={job.running || (ticketed === 0 && count("sent") === 0)}
                onClick={() => setConfirmSend(true)}>Send…</button>
      </div>
      <ErrorBox error={queue.error ?? job.error} />
      <JobLog job={job.job} onDone={(j) => { job.onDone(j); queue.refetch(); }} />
      <DataTable columns={columns} rows={rows} rowKey={(i) => i.id}
                 empty="Nothing queued. run_daily (or Send to → Trade on the Scans page) fills the queue." />
      <Dialog open={confirmSend} onClose={() => setConfirmSend(false)} tone="danger" title="⚠ Send the ticketed entries?"
              actions={<button className="btn btn-danger" onClick={() => { setConfirmSend(false);
                                                                          job.start(() => api.tradebotSend({ send: true, confirm: true })); }}>Send to paper</button>}>
        <p>This sends {ticketed} ticketed entr{ticketed === 1 ? "y" : "ies"} to the Alpaca <strong>paper</strong> account
          as limit DAY orders, and places OCO exits for any that already filled.</p>
        <p>The risk guard checks each entry first (kill switch, daily loss, drawdown, positions, exposure, stop-risk);
          anything it holds back stays ticketed with the reason in the Guard column.</p>
      </Dialog>
      <Dialog open={open !== null} onClose={() => setOpen(null)} title={open ? `${open.ticker}: checks` : ""} wide closeLabel="Close">
        {open && (
          <>
            <p>{open.scan} · queued {open.as_of} ({open.source}) · last session {open.expires_after ?? "—"}</p>
            {[...open.checks].reverse().map((c, n) => (
              <details key={n} open={n === 0}>
                <summary>{dateTime(c.at)} · {c.stage} · {c.status ?? "unchanged"} · {c.reason}</summary>
                <pre className="plain">{JSON.stringify({ gates: c.gates, daily: c.daily, squeeze: c.squeeze, filters: c.filters }, null, 2)}</pre>
              </details>
            ))}
            {!open.checks.length && <p className="muted">Not checked yet.</p>}
          </>
        )}
      </Dialog>
    </Card>
  );
}
