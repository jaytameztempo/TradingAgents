import { ReactNode, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, QueueItem, QueueName } from "../api";
import { dateTime, num } from "../format";
import { useJob } from "../useJob";
import { OrderEntryDialog } from "./OrderEntry";
import { Ticker } from "./Ticker";
import { Card, Column, DataTable, Dialog, ErrorBox, JobLog, RegimeBadge } from "./ui";

const ANALYSTS = ["market", "social", "news", "fundamentals"] as const;

function useQueue(name: QueueName) {
  const q = useQuery({ queryKey: ["queues"], queryFn: api.queues, refetchInterval: 30_000 });
  return { ...q, items: q.data?.[name] ?? [] };
}

function RemoveButton({ queue, id }: { queue: QueueName; id: string }) {
  const qc = useQueryClient();
  return (
    <button className="btn btn-sm" onClick={async () => {
      await api.removeQueued(queue, id);
      qc.invalidateQueries({ queryKey: ["queues"] });
    }}>Remove</button>
  );
}

const baseCols = (queue: QueueName): Column<QueueItem>[] => [
  { key: "ticker", label: "Ticker", render: (i) => <Ticker symbol={i.ticker} />, sortValue: (i) => i.ticker },
  { key: "side", label: "Side", render: (i) => (i.side === "long" ? "Long" : "Short"), sortValue: (i) => i.side },
  { key: "scan", label: "From scan", render: (i) => <span title={i.scan_file}>{i.scan}</span>, sortValue: (i) => i.scan },
  { key: "as_of", label: "Scan date", render: (i) => i.as_of ?? "—", sortValue: (i) => i.as_of },
  { key: "added", label: "Added", render: (i) => dateTime(i.added_at), sortValue: (i) => i.added_at },
  { key: "remove", label: "", align: "right", render: (i) => <RemoveButton queue={queue} id={i.id} /> },
];

function QueueCard({ title, queue, columns, hint, empty }: {
  title: string; queue: QueueName; columns: Column<QueueItem>[]; hint?: ReactNode; empty: string;
}) {
  const q = useQueue(queue);
  return (
    <Card title={`${title} (${q.items.length})`}>
      {hint && <p className="muted" style={{ marginTop: 0 }}>{hint}</p>}
      <ErrorBox error={q.error} />
      <DataTable columns={columns} rows={q.items} rowKey={(i) => i.id} empty={empty}
                 initialSort={{ key: "added", dir: "desc" }} />
    </Card>
  );
}

// --- Research page ---------------------------------------------------------------------

const ROUTE_TEXT: Record<string, string> = {
  ALLOWED: "✓ Routed: allowed", BLOCKED: "◆ Routed: blocked", NOT_ROUTED: "○ Not in route", NO_ROUTE: "○ No route for date",
};

const isRouted = (i: QueueItem) => i.route_status === "ALLOWED" || i.route_status === "BLOCKED";

export function ResearchQueue() {
  const [item, setItem] = useState<QueueItem | null>(null);
  const [analysts, setAnalysts] = useState<string[]>(["market"]);
  const runner = useJob();
  const cols = baseCols("research");
  cols.splice(4, 0,
    {
      key: "route", label: "Route", sortValue: (i) => i.route_status,
      render: (i) => <span className={i.route_status === "ALLOWED" ? "status-ok" : i.route_status === "BLOCKED" ? "status-blocked" : "muted"}>
        {ROUTE_TEXT[i.route_status ?? "NO_ROUTE"]}</span>,
    },
    {
      key: "run", label: "", render: (i) => (
        <span title={!i.as_of ? "This row has no scan date"
                     : isRouted(i) ? "Run TradingAgents on this ticker"
                     : "Not in a route: TradingAgents runs on it as a passer of its scan file"}>
          <button className="btn btn-sm btn-primary" disabled={!i.as_of || runner.running} onClick={() => setItem(i)}>Research…</button>
        </span>
      ),
    });
  return (
    <>
      <QueueCard title="Research queue" queue="research" columns={cols} empty="Nothing queued. Use Send to → Research on the Scans page."
                 hint="Sent from the Scans page. Nothing runs until you click Research… (each run costs LLM credits)." />
      {runner.job && <Card title="Queued research run"><ErrorBox error={runner.error} /><JobLog job={runner.job} onDone={runner.onDone} /></Card>}
      <Dialog open={item != null} onClose={() => setItem(null)} title="Run TradingAgents?"
              actions={<button className="btn btn-primary" disabled={!analysts.length} onClick={() => {
                const i = item!;
                setItem(null);
                const scan = isRouted(i) ? {} : { scan_kind: i.kind, scan_file: i.scan_file };
                runner.start(() => api.runResearch({ as_of: i.as_of!, ticker: i.ticker, analysts, confirm: true, ...scan }));
              }}>Run</button>}>
        {item && (isRouted(item)
          ? <p><strong>{item.ticker}</strong> as of <strong>{item.as_of}</strong> ({ROUTE_TEXT[item.route_status!]}).</p>
          : <p><strong>{item.ticker}</strong> as of <strong>{item.as_of}</strong>, a passer of {item.scan}. It is not in a
              route for that date, so the run uses its scan file instead.</p>)}
        <div className="checks">
          {ANALYSTS.map((a) => (
            <label key={a}><input type="checkbox" checked={analysts.includes(a)}
                                  onChange={() => setAnalysts(analysts.includes(a) ? analysts.filter((x) => x !== a) : [...analysts, a])} /> {a}</label>
          ))}
        </div>
        <p>This calls your LLM provider several times and can take several minutes. It costs API credits.</p>
      </Dialog>
    </>
  );
}

// --- Strategy page ---------------------------------------------------------------------

const VERDICT_TEXT: Record<string, string> = { ALLOWED: "✓ Allowed", BLOCKED: "✕ Blocked", NO_REGIME: "○ No regime yet" };

export function StrategyQueue() {
  const cols = baseCols("strategy");
  cols.splice(4, 0,
    { key: "needs", label: "Needs", render: (i) => <RegimeBadge label={i.needs_regime} />, sortValue: (i) => i.needs_regime },
    { key: "regime", label: "Regime", render: (i) => <span title={i.regime_source ?? ""}><RegimeBadge label={i.regime_label} /></span>, sortValue: (i) => i.regime_label },
    {
      key: "verdict", label: "Regime filter", sortValue: (i) => i.verdict,
      render: (i) => <span title={i.reason} className={i.verdict === "ALLOWED" ? "status-ok" : i.verdict === "BLOCKED" ? "status-failed" : "muted"}>
        {VERDICT_TEXT[i.verdict ?? "NO_REGIME"]}</span>,
    });
  return (
    <QueueCard title="Regime filter" queue="strategy" columns={cols} empty="Nothing queued. Use Send to → Strategy on the Scans page."
               hint="Each ticker is checked against the regime for its scan date (newest route, else the newest RegimeBot label): long trend names need UP, short trend names need DOWN, sideways channel names need SIDE." />
  );
}

// --- Trade page ------------------------------------------------------------------------

export function TradeQueue() {
  const [item, setItem] = useState<QueueItem | null>(null);
  const cols = baseCols("trade");
  const m = (i: QueueItem, k: string) => i.metrics?.[k] ?? null;
  const price = (v: number | null) => (v == null ? "—" : v.toFixed(2));
  cols.splice(4, 0,
    { key: "close", label: "Close", align: "right", render: (i) => price(m(i, "close")), sortValue: (i) => m(i, "close") },
    {
      key: "level", label: "Support / Resistance", align: "right",
      render: (i) => (m(i, "support") != null ? `${price(m(i, "support"))} / ${price(m(i, "resistance"))}` : "—"),
    },
    {
      key: "trend", label: "SMA 50 / 200", align: "right",
      render: (i) => (m(i, "sma_50") != null ? `${num(m(i, "sma_50"))} / ${num(m(i, "sma_200"))}` : "—"),
    },
    {
      key: "order", label: "", render: (i) => (
        <span title={i.as_of ? "Write a paper ticket for this ticker" : "This row has no scan date"}>
          <button className="btn btn-sm btn-primary" disabled={!i.as_of} onClick={() => setItem(i)}>Order…</button>
        </span>
      ),
    });
  return (
    <>
      <QueueCard title="To trade" queue="trade" columns={cols} empty="Nothing queued. Use Send to → Trade on the Scans page."
                 hint="Sent from the Scans page. Order… writes a paper ticket below; nothing is sent until you send that ticket." />
      <OrderEntryDialog item={item} onClose={() => setItem(null)} />
    </>
  );
}
