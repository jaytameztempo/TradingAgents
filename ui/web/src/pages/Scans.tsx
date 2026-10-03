import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, Artifact, Basket, BasketMember, defaultAsOf, parseTickers } from "../api";
import { Card, Column, DataTable, Empty, ErrorBox, JobLog, Tabs } from "../components/ui";
import { dateTime, num } from "../format";
import { useJob } from "../useJob";

type Kind = "up" | "down";
const KIND_LABEL: Record<Kind, string> = { up: "Upward trend momentum", down: "Breakdown short candidates" };

export default function Scans() {
  const [asOf, setAsOf] = useState(defaultAsOf());
  const [which, setWhich] = useState<"both" | Kind>("both");
  const [tickerText, setTickerText] = useState("");
  const runner = useJob();

  return (
    <>
      <div className="page-head"><h1>Scans</h1></div>
      <Card title="Run scans">
        <div className="form">
          <label className="field">Date
            <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} />
          </label>
          <label className="field">Scan
            <select value={which} onChange={(e) => setWhich(e.target.value as typeof which)}>
              <option value="both">Both</option>
              <option value="up">{KIND_LABEL.up}</option>
              <option value="down">{KIND_LABEL.down}</option>
            </select>
          </label>
          <label className="field" style={{ flex: 1, minWidth: 220 }}>Tickers (blank = each scan's default universe)
            <input value={tickerText} placeholder="SPY QQQ NVDA AAPL MSFT" onChange={(e) => setTickerText(e.target.value)} />
          </label>
          <button className="btn btn-primary" disabled={runner.running}
                  onClick={() => runner.start(() => api.runScans({ as_of: asOf, kind: which, tickers: parseTickers(tickerText) }))}>
            {runner.running ? "Running…" : "Run"}
          </button>
        </div>
        <p className="muted">Fetches daily bars from Alpaca (paper keys, read-only) and writes a basket JSON to ~/.tradingagents/baskets.</p>
        <ErrorBox error={runner.error} />
        <JobLog job={runner.job} onDone={runner.onDone} />
      </Card>
      <BasketBrowser />
    </>
  );
}

function BasketBrowser() {
  const [kind, setKind] = useState<Kind>("up");
  const baskets = useQuery({ queryKey: ["baskets", kind], queryFn: () => api.baskets(kind) });
  const [selected, setSelected] = useState<string | null>(null);
  const list = baskets.data ?? [];
  useEffect(() => setSelected(null), [kind]);
  const current = list.find((b) => b.file === selected) ?? list[0];

  return (
    <Card title={<Tabs value={kind} onChange={setKind} tabs={[{ id: "up", label: KIND_LABEL.up }, { id: "down", label: KIND_LABEL.down }]} />}>
      <ErrorBox error={baskets.error} />
      {!list.length ? <Empty>No baskets yet. Run a scan above.</Empty> : (
        <div className="split">
          <div className="list">
            {list.map((b) => (
              <button key={b.file} className={`list-item ${b === current ? "active" : ""}`} onClick={() => setSelected(b.file)}>
                <span>{b.as_of}</span>
                <span className="muted">{b.error ? "unreadable" : `${b.data?.members.length ?? 0} passed`}</span>
              </button>
            ))}
          </div>
          {current && <BasketDetail basket={current} />}
        </div>
      )}
    </Card>
  );
}

function BasketDetail({ basket }: { basket: Artifact<Basket> }) {
  if (basket.error || !basket.data) return <ErrorBox error={basket.error ?? "unreadable basket"} />;
  const b = basket.data;
  const memberCols: Column<BasketMember>[] = [
    { key: "ticker", label: "Ticker", render: (m) => <strong>{m.ticker}</strong>, sortValue: (m) => m.ticker },
    { key: "score", label: "Score", align: "right", render: (m) => num(m.score, 3), sortValue: (m) => m.score },
    { key: "close", label: "Close", align: "right", render: (m) => num(m.metrics.close), sortValue: (m) => m.metrics.close as number },
    { key: "sma50", label: "SMA 50", align: "right", render: (m) => num(m.metrics.sma_50), sortValue: (m) => m.metrics.sma_50 as number },
    { key: "sma200", label: "SMA 200", align: "right", render: (m) => num(m.metrics.sma_200), sortValue: (m) => m.metrics.sma_200 as number },
    { key: "bars", label: "Bars", align: "right", render: (m) => num(m.metrics.bars, 0) },
    { key: "last", label: "Last bar", render: (m) => String(m.metrics.last_bar_date ?? "—") },
  ];
  return (
    <div className="grid">
      <div className="muted">{basket.file} · created {dateTime(b.created_at)}{b.universe ? ` · universe: ${b.universe.join(" ")}` : ""}</div>
      <h2>Passed ({b.members.length})</h2>
      <DataTable columns={memberCols} rows={b.members} rowKey={(m) => m.ticker} empty="No tickers passed this scan."
                 initialSort={{ key: "score", dir: "desc" }} />
      <h2>Rejected ({b.rejected.length})</h2>
      <DataTable
        columns={[
          { key: "ticker", label: "Ticker", render: (r) => <strong>{r.ticker}</strong>, sortValue: (r) => r.ticker },
          { key: "reason", label: "Reason", render: (r) => <span style={{ whiteSpace: "normal" }}>{r.reason}</span> },
        ]}
        rows={b.rejected} rowKey={(r) => r.ticker} empty="Nothing rejected."
      />
    </div>
  );
}
