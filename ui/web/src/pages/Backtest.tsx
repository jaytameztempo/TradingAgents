import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Area, CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  api, BacktestBot, BacktestCoverageDay, BacktestOrigin, BacktestParams, BacktestResult, BacktestSkip,
  BacktestStrategy, BacktestSummaryRow, BacktestTrade, defaultAsOf,
} from "../api";
import { Card, Column, DataTable, Delta, Empty, ErrorBox, HoverTip, JobLog, RegimeBadge } from "../components/ui";
import { DASH, num } from "../format";
import { useJob } from "../useJob";

const STRATEGIES: { id: BacktestStrategy; label: string }[] = [
  { id: "all", label: "All" },
  { id: "up", label: "Long / UPBot" },
  { id: "down", label: "Short / DOWNBot" },
  { id: "side", label: "Sideways / SIDEBot" },
];
const BOT_LABEL: Record<BacktestBot, string> = { up: "UPBot", down: "DOWNBot", side: "SIDEBot" };
const ORIGIN_LABEL: Record<BacktestOrigin, string> = { replay: "Replay", reconstructed: "Reconstructed" };
const STATUS_LABEL: Record<BacktestCoverageDay["status"], string> = {
  saved: "Saved", reconstructed: "Reconstructed", holiday: "Holiday", missing: "Missing",
};

function daysBefore(iso: string, days: number): string {
  const d = new Date(`${iso}T12:00:00`);
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

export default function Backtest() {
  const today = defaultAsOf();
  const [start, setStart] = useState(daysBefore(today, 30));
  const [end, setEnd] = useState(today);
  const [single, setSingle] = useState(false);
  const [strategy, setStrategy] = useState<BacktestStrategy>("all");
  const [timeframe, setTimeframe] = useState<"daily" | "4h">("daily");
  const [maxSessions, setMaxSessions] = useState(10);
  const [holdToExit, setHoldToExit] = useState(false);
  const [riskPct, setRiskPct] = useState(1);
  const [rebuildMissing, setRebuildMissing] = useState(true);
  const [fetchSec, setFetchSec] = useState(false);
  const [params, setParams] = useState<BacktestParams | null>(null);
  const runner = useJob();

  const last = single ? start : end;
  const rebuildEnd = last > today ? today : last;
  const cover = useQuery({
    queryKey: ["backtest-coverage", start, last],
    queryFn: () => api.backtestCoverage(start, last),
    enabled: !!start && !!last && last >= start,
  });
  const result = useQuery({
    queryKey: ["backtest", params],
    queryFn: () => api.backtest(params!),
    enabled: params !== null && !runner.running,
    staleTime: Infinity,
  });

  const rebuild = (opts: { rebuild?: boolean; all_dates?: boolean } = {}) =>
    runner.start(() => api.reconstruct({ start, end: rebuildEnd, fetch_fundamentals: fetchSec, ...opts }));

  const run = async () => {
    const p: BacktestParams = {
      start, end: last, strategy, timeframe, max_sessions: maxSessions, risk_pct: riskPct,
      include_reconstructed: true, hold_to_exit: holdToExit,
    };
    if (rebuildMissing) {
      const days = await api.backtestCoverage(start, last).catch(() => [] as BacktestCoverageDay[]);
      if (days.some((d) => d.status === "missing" && d.date <= rebuildEnd)) await rebuild();
    }
    setParams(p);
  };

  return (
    <>
      <div className="page-head"><h1>Backtest</h1></div>
      <div className="notice">
        Dates with saved pipeline files are <strong>replayed</strong> from those files. Dates without them can be
        <strong> reconstructed</strong>: the pipeline is re-run on bars up to that date (missing daily bars are
        fetched and cached). The two are reported separately. No order is placed.
      </div>

      <Card title="Backtest">
        <div className="form">
          <label className="field">{single ? "Date" : "Start"}
            <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
          </label>
          {!single && (
            <label className="field">End
              <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
            </label>
          )}
          <label className="field">Single date
            <input type="checkbox" checked={single} onChange={(e) => setSingle(e.target.checked)} />
          </label>
          <label className="field">Strategy
            <select value={strategy} onChange={(e) => setStrategy(e.target.value as BacktestStrategy)}>
              {STRATEGIES.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
            </select>
          </label>
          <label className="field">Bars
            <select value={timeframe} onChange={(e) => setTimeframe(e.target.value as "daily" | "4h")}>
              <option value="daily">Daily</option>
              <option value="4h">4-hour</option>
            </select>
          </label>
          <label className="field">Max sessions held
            <input type="number" min={1} max={60} value={maxSessions} disabled={holdToExit}
                   onChange={(e) => setMaxSessions(Number(e.target.value))} style={{ width: 90 }} />
          </label>
          <HoverTip as="label" className="field"
                    text={"No time exit: a trade closes only at its stop or its target. One that has reached "
                          + "neither when the bars run out is listed as open at end and left out of the figures."}>
            Hold until stop or target
            <input type="checkbox" checked={holdToExit} onChange={(e) => setHoldToExit(e.target.checked)} />
          </HoverTip>
          <label className="field">Risk per trade (%)
            <input type="number" min={0.1} max={5} step={0.1} value={riskPct}
                   onChange={(e) => setRiskPct(Number(e.target.value))} style={{ width: 90 }} />
          </label>
          <label className="field">Rebuild dates with no saved files
            <input type="checkbox" checked={rebuildMissing} onChange={(e) => setRebuildMissing(e.target.checked)} />
          </label>
          <HoverTip as="label" className="field"
                    text={"Rebuilt dates: download SEC fundamentals missing from the cache, as the live "
                          + "financials step does (slower; needs SEC_EDGAR_USER_AGENT in .env)"}>
            Download missing SEC fundamentals
            <input type="checkbox" checked={fetchSec} onChange={(e) => setFetchSec(e.target.checked)} />
          </HoverTip>
          <button className="btn btn-primary" onClick={run} disabled={runner.running || result.isFetching}>
            {runner.running ? "Rebuilding…" : result.isFetching ? "Running…" : "Run"}
          </button>
          <HoverTip text="Rebuild every date in the range that has no saved files, replacing earlier rebuilds">
            <button className="btn" onClick={() => rebuild({ rebuild: true })} disabled={runner.running}>
              Rebuild range
            </button>
          </HoverTip>
          <HoverTip text="Also rebuild dates that have saved files, to compare a rebuild with what was saved">
            <button className="btn" onClick={() => rebuild({ rebuild: true, all_dates: true })}
                    disabled={runner.running}>
              Rebuild incl. saved dates
            </button>
          </HoverTip>
        </div>
        <CoverageStrip days={cover.data ?? []} />
        <ErrorBox error={runner.error} />
        <JobLog job={runner.job} onDone={runner.onDone} />
      </Card>

      <ErrorBox error={result.error} />
      {!params && <Empty>Pick dates and a strategy, then Run.</Empty>}
      {result.data && <Results data={result.data} />}
    </>
  );
}

function CoverageStrip({ days }: { days: BacktestCoverageDay[] }) {
  if (!days.length) return null;
  const counts = days.reduce<Record<string, number>>((acc, d) => ({ ...acc, [d.status]: (acc[d.status] ?? 0) + 1 }), {});
  return (
    <div style={{ marginTop: 12 }}>
      <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
        {(Object.keys(STATUS_LABEL) as BacktestCoverageDay["status"][])
          .filter((s) => counts[s]).map((s) => `${STATUS_LABEL[s]} ${counts[s]}`).join(" · ")}
      </div>
      <div className="row" style={{ gap: 4 }}>
        {days.map((d) => (
          <HoverTip key={d.date}
                    text={`${d.date}: ${STATUS_LABEL[d.status]}${d.label ? ` (${d.label})` : ""}${
                      d.fundamentals ? `\nfundamentals: ${d.fundamentals.cached} cached, ${d.fundamentals.fetched} `
                        + `downloaded, ${d.fundamentals.missing} missing (SEC download ${d.fundamentals.fetch ? "on" : "off"})`
                        : ""}${d.warnings.length ? `\n${d.warnings.join("\n")}` : ""}`}>
          <span className="badge"
                style={{
                  fontSize: 11,
                  fontWeight: d.status === "saved" ? 700 : 400,
                  borderStyle: d.status === "reconstructed" ? "dashed" : "solid",
                  opacity: d.status === "holiday" ? 0.5 : 1,
                  color: d.status === "missing" ? "var(--warning-text)" : undefined,
                  borderColor: d.status === "missing" ? "var(--warning)" : undefined,
                }}>
            {d.date.slice(5)} {d.status === "saved" ? "S" : d.status === "reconstructed" ? "R"
              : d.status === "holiday" ? "H" : "?"}
          </span>
          </HoverTip>
        ))}
      </div>
      <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
        S saved (replayed) · R reconstructed · H holiday · ? missing
      </div>
    </div>
  );
}

function Results({ data }: { data: BacktestResult }) {
  const origins = Array.from(new Set(data.summary.map((r) => r.origin)));
  return (
    <>
      <Card title="Summary">
        <SummaryTable rows={data.summary} />
        <p className="muted">
          Each closed trade risks {data.params.risk_pct}% of starting equity (no compounding). Replay and reconstructed
          results are never added together. Trades still open when the bars run out are listed, marked to the last
          close, and left out of these figures.
        </p>
      </Card>

      <Card title="Equity curve (start = 100)">
        {data.curve.length > 1 ? (
          <div className="chart" aria-label="Backtest equity curve">
            <ResponsiveContainer>
              <ComposedChart data={data.curve} margin={{ top: 16, right: 8, bottom: 0, left: 8 }}>
                <defs>
                  <linearGradient id="bt-fill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--series-1)" stopOpacity={0.18} />
                    <stop offset="100%" stopColor="var(--series-1)" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid vertical={false} stroke="var(--grid)" />
                <XAxis dataKey="date" stroke="var(--muted)" tick={{ fontSize: 11 }} tickLine={false}
                       axisLine={false} minTickGap={40} />
                <YAxis domain={["auto", "auto"]} width={60} stroke="var(--muted)" tick={{ fontSize: 11 }}
                       tickLine={false} axisLine={false} tickFormatter={(v: number) => v.toFixed(1)} />
                <Tooltip reverseDirection={{ y: true }}  // above the cursor, where the pointer does not cover it
                         contentStyle={{ background: "var(--surface)", border: "1px solid var(--border)",
                                         fontSize: 12 }}
                         formatter={(v: number) => v.toFixed(2)} />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                {origins.includes("replay") && (
                  <Area type="stepAfter" dataKey="replay" name="Replay" stroke="var(--series-1)" strokeWidth={2}
                        fill="url(#bt-fill)" dot={false} isAnimationActive={false} />
                )}
                {origins.includes("reconstructed") && (
                  <Line type="stepAfter" dataKey="reconstructed" name="Reconstructed" stroke="var(--text-2)"
                        strokeDasharray="6 3" strokeWidth={2} dot={false} isAnimationActive={false} />
                )}
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        ) : <Empty>No closed trades in this range, so there is no curve.</Empty>}
      </Card>

      <Card title={`Trades (${data.trades.length})`}>
        <TradesTable rows={data.trades} />
      </Card>

      <Card title={`Skipped (${data.skipped.length})`}>
        <SkippedTable rows={data.skipped} />
      </Card>

      <Card title="Replay vs reconstructed">
        <ul>
          {(Object.keys(data.caveats) as BacktestOrigin[]).map((o) => (
            <li key={o}><strong>{ORIGIN_LABEL[o]}:</strong> {data.caveats[o]}</li>
          ))}
        </ul>
        {data.coverage.filter((d) => d.status === "reconstructed" && d.warnings.length).slice(0, 1).map((d) => (
          <div key={d.date} className="notice notice-warn">
            Rebuild warnings (e.g. {d.date}): {d.warnings.join(" · ")}
          </div>
        ))}
      </Card>

      <Card title="Files acted on">
        {data.days.length ? (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>As-of</th><th>Origin</th><th>Regime</th><th>Route</th><th>Sources</th></tr></thead>
              <tbody>
                {data.days.map((d) => (
                  <tr key={`${d.origin}-${d.date}`}>
                    <td>{d.date}</td>
                    <td>{ORIGIN_LABEL[d.origin]}</td>
                    <td><RegimeBadge label={d.regime_label} /></td>
                    <td className="muted">{d.route_file ?? DASH}</td>
                    <td>{d.sources.map((s) => `${BOT_LABEL[s.bot]} ${s.side}: ${s.file} (written ${s.known_on})`)
                      .join(" · ") || DASH}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <Empty>No saved or rebuilt files in this range.</Empty>}
        {data.warnings.map((w) => <div key={w} className="notice notice-warn">{w}</div>)}
      </Card>

      <Card title="Rules used">
        <ul>
          {Object.entries(data.rules).map(([k, v]) => <li key={k}><strong>{k.replace(/_/g, " ")}:</strong> {v}</li>)}
          <li><strong>files per date:</strong> for each as-of date, the newest file of each kind written within{" "}
            {data.params.max_report_age_days} days of it; much later re-runs are ignored.</li>
        </ul>
      </Card>
    </>
  );
}

const pct = (v: number | null) => (v == null ? DASH : `${num(v, 2)}%`);
const r = (v: number | null) => (v == null ? DASH : <Delta value={v}>{num(v, 2)}R</Delta>);

function SummaryTable({ rows }: { rows: BacktestSummaryRow[] }) {
  const columns: Column<BacktestSummaryRow>[] = [
    { key: "origin", label: "Origin", render: (x) => ORIGIN_LABEL[x.origin] },
    { key: "label", label: "Strategy", render: (x) => (x.strategy === "all" ? <strong>{x.label}</strong> : x.label) },
    { key: "trades", label: "Trades", align: "right", render: (x) => x.trades },
    { key: "open", label: "Open at end", align: "right", render: (x) => x.open },
    { key: "win_rate", label: "Win rate", align: "right", render: (x) => pct(x.win_rate) },
    { key: "avg_r", label: "Avg R", align: "right", render: (x) => r(x.avg_r) },
    { key: "total_r", label: "Total R", align: "right", render: (x) => r(x.total_r) },
    { key: "ret", label: "Total return", align: "right",
      render: (x) => <Delta value={x.total_return_pct}>{pct(x.total_return_pct)}</Delta> },
    { key: "dd", label: "Max drawdown", align: "right", render: (x) => pct(x.max_drawdown_pct) },
  ];
  return <DataTable columns={columns} rows={rows} rowKey={(x) => `${x.origin}-${x.strategy}`} />;
}

function TradesTable({ rows }: { rows: BacktestTrade[] }) {
  const columns: Column<BacktestTrade>[] = [
    { key: "origin", label: "Origin", render: (t) => ORIGIN_LABEL[t.origin], sortValue: (t) => t.origin },
    { key: "asof", label: "As-of", render: (t) => <HoverTip text={t.source_file}>{t.report_as_of}</HoverTip>,
      sortValue: (t) => t.report_as_of },
    { key: "bot", label: "Bot", render: (t) => BOT_LABEL[t.bot], sortValue: (t) => t.bot },
    { key: "sym", label: "Symbol", render: (t) => <strong>{t.symbol}</strong>, sortValue: (t) => t.symbol },
    { key: "side", label: "Side", render: (t) => t.side },
    { key: "lv", label: "Levels", render: (t) => <HoverTip text={t.detail}>{t.levels_source}</HoverTip> },
    { key: "ref", label: "Recorded entry", align: "right", render: (t) => num(t.reference_entry) },
    { key: "ed", label: "Entry date", render: (t) => t.entry_date, sortValue: (t) => t.entry_date },
    { key: "entry", label: "Entry", align: "right", render: (t) => num(t.entry) },
    { key: "stop", label: "Stop", align: "right", render: (t) => num(t.stop) },
    { key: "target", label: "Target", align: "right", render: (t) => num(t.target) },
    { key: "xd", label: "Exit date", render: (t) => t.exit_date, sortValue: (t) => t.exit_date },
    { key: "exit", label: "Exit", align: "right", render: (t) => num(t.exit) },
    { key: "why", label: "Exit reason", render: (t) => t.exit_reason.replace(/_/g, " "),
      sortValue: (t) => t.exit_reason },
    { key: "r", label: "R", align: "right", render: (t) => r(t.r), sortValue: (t) => t.r },
    { key: "held", label: "Sessions", align: "right", render: (t) => t.sessions_held,
      sortValue: (t) => t.sessions_held },
  ];
  return <DataTable columns={columns} rows={rows} empty="No simulated trades."
                    rowKey={(t) => `${t.origin}-${t.bot}-${t.symbol}-${t.run_date}-${t.source_file}`}
                    initialSort={{ key: "ed", dir: "asc" }} />;
}

function SkippedTable({ rows }: { rows: BacktestSkip[] }) {
  const columns: Column<BacktestSkip>[] = [
    { key: "origin", label: "Origin", render: (s) => ORIGIN_LABEL[s.origin], sortValue: (s) => s.origin },
    { key: "asof", label: "As-of", render: (s) => s.report_as_of, sortValue: (s) => s.report_as_of },
    { key: "bot", label: "Bot", render: (s) => BOT_LABEL[s.bot], sortValue: (s) => s.bot },
    { key: "sym", label: "Symbol", render: (s) => s.symbol, sortValue: (s) => s.symbol },
    { key: "src", label: "Source", render: (s) => <span className="muted">{s.source_file}</span> },
    { key: "why", label: "Reason", render: (s) => s.reason, sortValue: (s) => s.reason },
  ];
  return <DataTable columns={columns} rows={rows} empty="Nothing skipped."
                    rowKey={(s) => `${s.origin}-${s.bot}-${s.symbol}-${s.run_date}-${s.source_file}`} />;
}
