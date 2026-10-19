import { ReactNode, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, Artifact, Basket, BasketMember, ChannelCall, ChannelReport, defaultAsOf, parseTickers, ScanKind } from "../api";
import { Card, Column, DataTable, Empty, ErrorBox, JobLog, Tabs } from "../components/ui";
import { dateTime, num } from "../format";
import { useJob } from "../useJob";
import { Ticker } from "../components/Ticker";
import { usePrefs } from "../prefs";

type Kind = "up" | "down" | "long" | "short";
const KIND_LABEL: Record<Kind, string> = {
  up: "Upward trend momentum",
  down: "Breakdown short candidates",
  long: "Long sideways channel",
  short: "Short sideways channel",
};

const SCAN_OPTIONS: { value: ScanKind; label: string }[] = [
  { value: "all", label: "All four scans" },
  { value: "both", label: "Both trend scans" },
  { value: "sideways", label: "Both sideways scans" },
  { value: "up", label: KIND_LABEL.up },
  { value: "down", label: KIND_LABEL.down },
  { value: "long_side", label: KIND_LABEL.long },
  { value: "short_side", label: KIND_LABEL.short },
];
// Kinds that run a trend scan, the only scans that take a ticker list.
const USES_TICKERS: ScanKind[] = ["all", "both", "up", "down"];
const USES_FINANCIALS: ScanKind[] = ["all", "sideways", "long_side", "short_side"];

export default function Scans() {
  const [asOf, setAsOf] = useState(defaultAsOf());
  const { prefs } = usePrefs();
  const [which, setWhich] = useState<ScanKind>(prefs.defaultScan);
  const [tickerText, setTickerText] = useState(prefs.defaultTickers);
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
            <select value={which} onChange={(e) => setWhich(e.target.value as ScanKind)}>
              {SCAN_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </label>
          <label className="field" style={{ flex: 1, minWidth: 220 }}>
            {USES_TICKERS.includes(which)
              ? `Tickers for the trend scans (blank = default universe)${which === "all" ? "; sideways scans use the financials survivors" : ""}`
              : "Tickers (sideways scans use the financials survivors)"}
            <input value={tickerText} placeholder="SPY QQQ NVDA AAPL MSFT" disabled={!USES_TICKERS.includes(which)}
                   onChange={(e) => setTickerText(e.target.value)} />
          </label>
          <button className="btn btn-primary" disabled={runner.running}
                  onClick={() => runner.start(() => api.runScans({
                    as_of: asOf, kind: which, tickers: USES_TICKERS.includes(which) ? parseTickers(tickerText) : [],
                  }))}>
            {runner.running ? "Running…" : "Run"}
          </button>
        </div>
        <p className="muted">
          Fetches daily bars from Alpaca (paper keys, read-only). Trend scans write a basket to ~/.tradingagents/baskets.
          {USES_FINANCIALS.includes(which) && (
            <> Sideways scans scan the STRONG (long) or WEAK (short) survivors of the newest financials report for the date,
              so run <code>run_scanbot_financials</code> for that date first; they write to ~/.tradingagents/scanbot.</>
          )}
        </p>
        <ErrorBox error={runner.error} />
        <JobLog job={runner.job} onDone={runner.onDone} />
      </Card>
      <BasketBrowser />
    </>
  );
}

const TABS: { id: Kind; label: string }[] = (["up", "down", "long", "short"] as Kind[]).map((id) => ({ id, label: KIND_LABEL[id] }));

function BasketBrowser() {
  const [kind, setKind] = useState<Kind>("up");
  return (
    <Card title={<Tabs value={kind} onChange={setKind} tabs={TABS} />}>
      {kind === "up" || kind === "down"
        ? <TrendBaskets key={kind} kind={kind} />
        : <ChannelReports key={kind} side={kind} />}
    </Card>
  );
}

/** A list of reports on the left, the selected one on the right. */
function ReportList<T>({ items, render, count, empty }: {
  items: Artifact<T>[];
  render: (item: Artifact<T>) => ReactNode;
  count: (data: T) => number;
  empty: ReactNode;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const current = items.find((b) => b.file === selected) ?? items[0];
  if (!items.length) return <Empty>{empty}</Empty>;
  return (
    <div className="split">
      <div className="list">
        {items.map((b) => (
          <button key={b.file} className={`list-item ${b === current ? "active" : ""}`} onClick={() => setSelected(b.file)}>
            <span>{b.as_of}</span>
            <span className="muted">{b.error || !b.data ? "unreadable" : `${count(b.data)} passed`}</span>
          </button>
        ))}
      </div>
      {current && render(current)}
    </div>
  );
}

function TrendBaskets({ kind }: { kind: "up" | "down" }) {
  const baskets = useQuery({ queryKey: ["baskets", kind], queryFn: () => api.baskets(kind) });
  return (
    <>
      <ErrorBox error={baskets.error} />
      <ReportList items={baskets.data ?? []} count={(b) => b.members.length}
                  render={(b) => <BasketDetail basket={b} />} empty="No baskets yet. Run a scan above." />
    </>
  );
}

function ChannelReports({ side }: { side: "long" | "short" }) {
  const reports = useQuery({ queryKey: ["channelScans", side], queryFn: () => api.channelScans(side) });
  return (
    <>
      <ErrorBox error={reports.error} />
      <ReportList items={reports.data ?? []} count={(r) => r.members.length}
                  render={(r) => <ChannelDetail report={r} side={side} />}
                  empty="No sideways channel reports yet. Run the financials step, then this scan." />
    </>
  );
}

type MemberRow = { ticker: string; call: ChannelCall | null };
const metric = (row: MemberRow, key: string) => {
  const v = row.call?.metrics[key];
  return typeof v === "number" ? v : null;
};
const price = (v: number | null) => (v == null ? "—" : v.toFixed(2));

function ChannelDetail({ report, side }: { report: Artifact<ChannelReport>; side: "long" | "short" }) {
  if (report.error || !report.data) return <ErrorBox error={report.error ?? "unreadable report"} />;
  const r = report.data;
  const rows: MemberRow[] = r.members.map((ticker) => ({ ticker, call: r.member_calls[ticker] ?? null }));
  const level = side === "long" ? "support" : "resistance";
  const distanceKey = side === "long" ? "distance_to_support_atr" : "distance_to_resistance_atr";
  const qualityTotal = Object.keys(r.quality_counts).length;
  const memberCols: Column<MemberRow>[] = [
    { key: "ticker", label: "Ticker", render: (m) => <Ticker symbol={m.ticker} />, sortValue: (m) => m.ticker },
    { key: "close", label: "Close", align: "right", render: (m) => price(metric(m, "close")), sortValue: (m) => metric(m, "close") },
    { key: "support", label: "Support", align: "right", render: (m) => price(metric(m, "support")), sortValue: (m) => metric(m, "support") },
    { key: "resistance", label: "Resistance", align: "right", render: (m) => price(metric(m, "resistance")), sortValue: (m) => metric(m, "resistance") },
    { key: "height", label: "Height %", align: "right", render: (m) => num(metric(m, "height_pct")), sortValue: (m) => metric(m, "height_pct") },
    { key: "dist", label: `ATR to ${level}`, align: "right", render: (m) => num(metric(m, distanceKey)), sortValue: (m) => metric(m, distanceKey) },
    { key: "adx", label: "ADX 14", align: "right", render: (m) => num(metric(m, "adx_14"), 1), sortValue: (m) => metric(m, "adx_14") },
    { key: "atr", label: "ATR %", align: "right", render: (m) => num(metric(m, "atr_pct")), sortValue: (m) => metric(m, "atr_pct") },
    {
      key: "quality", label: "Quality", align: "right",
      render: (m) => (
        <span title={Object.entries(m.call?.quality ?? {}).map(([k, v]) => `${v ? "✓" : "✕"} ${k}`).join("\n")}>
          {m.call?.quality_passed ?? "—"} / {qualityTotal}
        </span>
      ),
      sortValue: (m) => m.call?.quality_passed,
    },
  ];
  const stages = Object.entries(r.stage_counts);
  const reasons = Object.entries(r.reason_counts).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
  return (
    <div className="grid">
      <div className="muted">
        {report.file} · created {dateTime(r.created_at)} · {r.scanned} scanned
        {r.policy?.financial_gate ? ` · ${String(r.policy.financial_gate)} financials` : ""}
      </div>
      {r.warnings?.map((w) => <div key={w} className="notice notice-warn">⚠ {w}</div>)}
      <h2>Passed ({r.members.length})</h2>
      <DataTable columns={memberCols} rows={rows} rowKey={(m) => m.ticker} empty="No tickers passed this scan."
                 initialSort={{ key: "dist", dir: "asc" }} />
      <div className="grid grid-2">
        <div>
          <h2>Funnel</h2>
          <DataTable
            columns={[
              { key: "stage", label: "Stage", render: (s: [string, number]) => s[0] },
              { key: "n", label: "Survivors", align: "right", render: (s: [string, number]) => s[1] },
            ]}
            rows={stages} rowKey={(s) => s[0]}
          />
        </div>
        <div>
          <h2>Fail reasons</h2>
          <DataTable
            columns={[
              { key: "reason", label: "Reason", render: (s: [string, number]) => s[0] },
              { key: "n", label: "Names", align: "right", render: (s: [string, number]) => s[1] },
            ]}
            rows={reasons} rowKey={(s) => s[0]} empty="No failures recorded."
          />
          <p className="muted">A name can fail for several reasons.</p>
        </div>
      </div>
    </div>
  );
}

function BasketDetail({ basket }: { basket: Artifact<Basket> }) {
  if (basket.error || !basket.data) return <ErrorBox error={basket.error ?? "unreadable basket"} />;
  const b = basket.data;
  const memberCols: Column<BasketMember>[] = [
    { key: "ticker", label: "Ticker", render: (m) => <Ticker symbol={m.ticker} />, sortValue: (m) => m.ticker },
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
          { key: "ticker", label: "Ticker", render: (r) => <Ticker symbol={r.ticker} />, sortValue: (r) => r.ticker },
          { key: "reason", label: "Reason", render: (r) => <span style={{ whiteSpace: "normal" }}>{r.reason}</span> },
        ]}
        rows={b.rejected} rowKey={(r) => r.ticker} empty="Nothing rejected."
      />
    </div>
  );
}
