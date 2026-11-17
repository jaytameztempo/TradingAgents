import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Area, CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  api, BacktestBot, BacktestCoverageDay, BacktestExportFormat, BacktestOrigin, BacktestOrigins, BacktestParams,
  BacktestResult, BacktestRunMeta, BacktestSavedRun, BacktestSizing, BacktestSkip, BacktestStrategy,
  BacktestSummaryRow, BacktestTrade, defaultAsOf,
} from "../api";
import { Card, Column, DataTable, Delta, Empty, ErrorBox, HoverTip, JobLog, RegimeBadge } from "../components/ui";
import { Ticker } from "../components/Ticker";
import { DASH, dateTime, money, num, signedMoney } from "../format";
import { useJob } from "../useJob";

const STRATEGIES: { id: BacktestStrategy; label: string }[] = [
  { id: "all", label: "All" },
  { id: "up", label: "Long / UPBot" },
  { id: "down", label: "Short / DOWNBot" },
  { id: "side", label: "Sideways / SIDEBot" },
];
const BOT_LABEL: Record<BacktestBot, string> = { up: "UPBot", down: "DOWNBot", side: "SIDEBot" };
const ORIGIN_LABEL: Record<BacktestOrigin, string> = { replay: "Replay", reconstructed: "Reconstructed" };
const SIZING_LABEL: Record<BacktestSizing, string> = { live: "Live (Tradebot basket)", fixed: "Fixed risk %" };
const LIMIT_DEFAULTS: Record<BacktestSizing, { positions: string; gross: string; risk: string }> = {
  live: { positions: "15", gross: "60", risk: "8" },
  fixed: { positions: "none", gross: "none", risk: "none" },
};
const STATUS_LABEL: Record<BacktestCoverageDay["status"], string> = {
  saved: "Saved", reconstructed: "Reconstructed", holiday: "Holiday", missing: "Missing",
};

function blankToNull(v: string): number | null {
  return v.trim() === "" ? null : Number(v);
}

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
  const [capital, setCapital] = useState(100000);
  const [sizingMode, setSizingMode] = useState<BacktestSizing>("live");
  const [origins, setOrigins] = useState<BacktestOrigins>("both");
  const [maxPositions, setMaxPositions] = useState("");
  const [maxGross, setMaxGross] = useState("");
  const [maxOpenRisk, setMaxOpenRisk] = useState("");
  const [slippage, setSlippage] = useState(0);
  const [commission, setCommission] = useState(0);
  const [prepare, setPrepare] = useState(true);
  const [fetchSec, setFetchSec] = useState(false);
  const [params, setParams] = useState<BacktestParams | null>(null);
  const [opened, setOpened] = useState<BacktestSavedRun | null>(null);
  const runner = useJob();

  const last = single ? start : end;
  const yesterday = daysBefore(today, 1);
  const rebuildEnd = last > yesterday ? yesterday : last;
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

  const rebuild = (opts: { rebuild?: boolean; all_dates?: boolean; trade_bars?: boolean } = {}) =>
    runner.start(() => api.reconstruct({ start, end: rebuildEnd, fetch_fundamentals: fetchSec, ...opts }));

  const run = async () => {
    const p: BacktestParams = {
      start, end: last, strategy, timeframe, max_sessions: maxSessions, risk_pct: riskPct,
      include_reconstructed: origins !== "replay", hold_to_exit: holdToExit, capital, origins,
      sizing: sizingMode, max_positions: blankToNull(maxPositions), max_gross_pct: blankToNull(maxGross),
      max_open_risk_pct: blankToNull(maxOpenRisk), slippage_bps: slippage, commission_per_share: commission,
    };
    // Rebuild dates with no files (cheap when there are none) and fetch the bars the trades need.
    if (prepare && start <= rebuildEnd) await rebuild({ trade_bars: true });
    setOpened(null);
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
          <label className="field">Origins
            <select value={origins} onChange={(e) => setOrigins(e.target.value as BacktestOrigins)}>
              <option value="both">Both</option>
              <option value="replay">Replay only</option>
              <option value="reconstructed">Reconstructed only</option>
            </select>
          </label>
          <HoverTip as="label" className="field"
                    text={"Starting capital stands in for equity and buying power on every day, with no "
                          + "compounding. Dollars are whole shares x the price move, less fees."}>
            Starting capital ($)
            <input type="number" min={1000} step={1000} value={capital}
                   onChange={(e) => setCapital(Number(e.target.value))} style={{ width: 120 }} />
          </HoverTip>
          <HoverTip as="label" className="field"
                    text={"Live: as the Tradebot sizes tickets: 23% of capital (x 0.7 when SPY and QQQ ADX are both "
                          + "under 15) split equally over the day's names, at most 15% a name, whole shares, the "
                          + "book's stop-risk held to 8%; then the guard's limits in rank order.\nFixed risk %: "
                          + "Risk per trade (%) of capital at the stop, held to a position value of at most the "
                          + "capital."}>
            Sizing
            <select value={sizingMode} onChange={(e) => setSizingMode(e.target.value as BacktestSizing)}>
              <option value="live">{SIZING_LABEL.live}</option>
              <option value="fixed">{SIZING_LABEL.fixed}</option>
            </select>
          </HoverTip>
          <label className="field">Risk per trade (%)
            <input type="number" min={0.1} max={5} step={0.1} value={riskPct} disabled={sizingMode !== "fixed"}
                   onChange={(e) => setRiskPct(Number(e.target.value))} style={{ width: 90 }} />
          </label>
          <HoverTip as="label" className="field" text="Most positions open at once (0 = no limit; blank = default)">
            Max open positions
            <input type="number" min={0} max={200} value={maxPositions} placeholder={LIMIT_DEFAULTS[sizingMode].positions}
                   onChange={(e) => setMaxPositions(e.target.value)} style={{ width: 80 }} />
          </HoverTip>
          <HoverTip as="label" className="field"
                    text="Most gross exposure, % of capital (0 = no limit; blank = default)">
            Max gross (%)
            <input type="number" min={0} max={400} value={maxGross} placeholder={LIMIT_DEFAULTS[sizingMode].gross}
                   onChange={(e) => setMaxGross(e.target.value)} style={{ width: 80 }} />
          </HoverTip>
          <HoverTip as="label" className="field"
                    text="Most stop-risk open at once, % of capital (0 = no limit; blank = default)">
            Max open risk (%)
            <input type="number" min={0} max={100} step={0.5} value={maxOpenRisk}
                   placeholder={LIMIT_DEFAULTS[sizingMode].risk}
                   onChange={(e) => setMaxOpenRisk(e.target.value)} style={{ width: 80 }} />
          </HoverTip>
          <HoverTip as="label" className="field"
                    text="Against the trade on fills at the open and on stop, gap, time and mark exits; not on limit or target fills">
            Slippage (bps)
            <input type="number" min={0} max={500} step={1} value={slippage}
                   onChange={(e) => setSlippage(Number(e.target.value))} style={{ width: 70 }} />
          </HoverTip>
          <label className="field">Commission ($/share)
            <input type="number" min={0} max={1} step={0.001} value={commission}
                   onChange={(e) => setCommission(Number(e.target.value))} style={{ width: 80 }} />
          </label>
          <HoverTip as="label" className="field"
                    text={"Before running: rebuild dates with no saved files, and fetch the daily and 4-hour bars "
                          + "of the names the backtest would trade, up to yesterday (only what the cache lacks)."}>
            Rebuild missing dates and fetch missing bars first
            <input type="checkbox" checked={prepare} onChange={(e) => setPrepare(e.target.checked)} />
          </HoverTip>
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

      <SavedRuns openedId={opened?.id ?? null} onOpen={setOpened} />

      {opened ? (
        <>
          <div className="notice">
            Showing saved run <strong>{opened.name}</strong>, saved {dateTime(opened.saved_at)}. It is the result as
            it was then; later rebuilds or new bars do not change it.{" "}
            <button className="btn btn-sm" onClick={() => setOpened(null)}>Close</button>
          </div>
          <Results data={opened.result} />
        </>
      ) : (
        <>
          <ErrorBox error={result.error} />
          {!params && <Empty>Pick dates and a strategy, then Run.</Empty>}
          {result.data && params && <SaveRunBar params={params} />}
          {result.data && <Results data={result.data} />}
        </>
      )}
    </>
  );
}

function SaveRunBar({ params }: { params: BacktestParams }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<BacktestRunMeta | null>(null);
  const [error, setError] = useState<unknown>(null);
  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      setSaved(await api.saveBacktestRun({ ...params, name }));
      await qc.invalidateQueries({ queryKey: ["backtest-runs"] });
    } catch (e) {
      setError(e);
    } finally {
      setSaving(false);
    }
  };
  return (
    <Card>
      <div className="form">
        <label className="field">Run name
          <input value={name} maxLength={120} placeholder="optional" onChange={(e) => setName(e.target.value)}
                 style={{ width: 260 }} />
        </label>
        <HoverTip text={"Save this run (its settings and every result) under ~/.tradingagents/backtest/runs. "
                        + "The saved copy does not change when dates are rebuilt or new bars are cached."}>
          <button className="btn btn-primary" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save run"}
          </button>
        </HoverTip>
        {saved && <span className="muted">Saved as “{saved.name}”.</span>}
      </div>
      <ErrorBox error={error} />
    </Card>
  );
}

const EXPORTS: { format: BacktestExportFormat; label: string; tip: string }[] = [
  { format: "json", label: "JSON", tip: "Everything: settings, summary, equity curve, trades, skipped names, days" },
  { format: "trades_csv", label: "Trades CSV", tip: "One row per simulated trade, with R and P/L ($)" },
  { format: "summary_csv", label: "Summary CSV", tip: "One row per origin and strategy" },
];

function SavedRuns({ openedId, onOpen }: {
  openedId: string | null;
  onOpen: (run: BacktestSavedRun | null) => void;
}) {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["backtest-runs"], queryFn: api.backtestRuns });
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const open = async (id: string) => {
    setBusy(id);
    setError(null);
    try {
      onOpen(await api.backtestRun(id));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  const rename = async (run: BacktestRunMeta) => {
    const name = window.prompt("New name for this saved run", run.name);
    if (!name || !name.trim() || name === run.name) return;
    setError(null);
    try {
      await api.renameBacktestRun(run.id, name.trim());
      await qc.invalidateQueries({ queryKey: ["backtest-runs"] });
    } catch (e) {
      setError(e);
    }
  };
  const remove = async (run: BacktestRunMeta) => {
    if (!window.confirm(`Delete saved run “${run.name}”? This cannot be undone.`)) return;
    setError(null);
    try {
      await api.deleteBacktestRun(run.id);
      if (openedId === run.id) onOpen(null);
      await qc.invalidateQueries({ queryKey: ["backtest-runs"] });
    } catch (e) {
      setError(e);
    }
  };
  const total = (run: BacktestRunMeta, origin: BacktestOrigin) => {
    const t = run.totals.find((x) => x.origin === origin);
    if (!t || !t.trades) return <span className="muted">{DASH}</span>;
    return (
      <HoverTip text={`${t.trades} closed${t.open ? `, ${t.open} open at end` : ""} · win rate ${pct(t.win_rate)}`
                      + ` · avg ${num(t.avg_r, 2)}R · ${pct(t.total_return_pct)}`}>
        <Delta value={t.total_return_usd}>{signedMoney(t.total_return_usd)}</Delta>{" "}
        <span className="muted">({num(t.total_r, 2)}R)</span>
      </HoverTip>
    );
  };
  const columns: Column<BacktestRunMeta>[] = [
    { key: "name", label: "Name", sortValue: (x) => x.name,
      render: (x) => (x.id === openedId ? <strong>{x.name} (open)</strong> : x.name) },
    { key: "saved", label: "Saved", sortValue: (x) => x.saved_at, render: (x) => dateTime(x.saved_at) },
    { key: "range", label: "Dates", sortValue: (x) => x.params.start,
      render: (x) => (x.params.start === x.params.end ? x.params.start : `${x.params.start} to ${x.params.end}`) },
    { key: "settings", label: "Settings",
      render: (x) => (
        <span className="muted">
          {STRATEGIES.find((s) => s.id === x.params.strategy)?.label ?? x.params.strategy} · {x.params.timeframe}
          {" · "}{x.params.hold_to_exit ? "hold to stop/target" : `max ${x.params.max_sessions} sessions`}
          {" · "}{(x.params.sizing ?? "fixed") === "live" ? `live sizing on ${money(x.params.capital)}`
            : `${x.params.risk_pct}% of ${money(x.params.capital)}`}
        </span>
      ) },
    { key: "replay", label: "Replay", align: "right", render: (x) => total(x, "replay") },
    { key: "recon", label: "Reconstructed", align: "right", render: (x) => total(x, "reconstructed") },
    { key: "actions", label: "",
      render: (x) => (
        <div className="row" style={{ gap: 4, flexWrap: "wrap" }}>
          <button className="btn btn-sm" onClick={() => open(x.id)} disabled={busy === x.id}>
            {busy === x.id ? "Opening…" : "Open"}
          </button>
          {EXPORTS.map((e) => (
            <HoverTip key={e.format} text={e.tip}>
              <a className="btn btn-sm" href={api.backtestRunExportUrl(x.id, e.format)} download>{e.label}</a>
            </HoverTip>
          ))}
          <button className="btn btn-sm" onClick={() => rename(x)}>Rename</button>
          <button className="btn btn-sm btn-danger" onClick={() => remove(x)}>Delete</button>
        </div>
      ) },
  ];
  return (
    <Card title="Saved runs">
      <ErrorBox error={list.error ?? error} />
      <DataTable columns={columns} rows={list.data ?? []} rowKey={(x) => x.id}
                 empty="No saved runs yet. Run a backtest, then Save run."
                 initialSort={{ key: "saved", dir: "desc" }} />
    </Card>
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
  const live = (data.params.sizing ?? "fixed") === "live";
  return (
    <>
      <DataWarnings data={data} />
      <Card title="Summary">
        <SummaryTable rows={data.summary} />
        <p className="muted">
          {live
            ? <>Live sizing on {money(data.params.capital)}: each day's basket gets 23% of it (x 0.7 in a calm
                market) split equally, at most 15% a name, in whole shares, with the book's stop-risk held to{" "}
                {data.params.max_open_risk_pct ?? 8}%.</>
            : <>Each closed trade risks {data.params.risk_pct}% of {money(data.params.capital)} at its stop, in whole
                shares, held to a position value of at most the capital.</>}{" "}
          Starting capital stands in for equity and buying power; no compounding. Replay and reconstructed results
          are never added together. Trades still open when the bars run out, or whose bars ended early, are listed,
          marked to their last close, and left out of these figures.
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
          {(Object.keys(data.caveats) as BacktestOrigin[]).filter((o) => data.caveats[o]).map((o) => (
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
    { key: "bars_end", label: "Bars ended", align: "right", render: (x) => x.bars_end ?? DASH },
    { key: "win_rate", label: "Win rate", align: "right", render: (x) => pct(x.win_rate) },
    { key: "avg_r", label: "Avg R", align: "right", render: (x) => r(x.avg_r) },
    { key: "total_r", label: "Total R", align: "right", render: (x) => r(x.total_r) },
    { key: "ret", label: "Total return", align: "right",
      render: (x) => <Delta value={x.total_return_pct}>{pct(x.total_return_pct)}</Delta> },
    { key: "usd", label: "Total return ($)", align: "right",
      render: (x) => <Delta value={x.total_return_usd}>{signedMoney(x.total_return_usd)}</Delta> },
    { key: "ex", label: "Without best trade", align: "right",
      render: (x) => (x.total_return_usd_ex_best == null || !x.trades ? DASH : (
        <HoverTip text={`Total R without the single best trade: ${num(x.total_r_ex_best ?? 0, 2)}R`
                        + (x.outliers ? ` · ${x.outliers} trade(s) over 5R` : "")}>
          <Delta value={x.total_return_usd_ex_best}>{signedMoney(x.total_return_usd_ex_best)}</Delta>
        </HoverTip>
      )) },
    { key: "dd", label: "Max drawdown", align: "right", render: (x) => pct(x.max_drawdown_pct) },
    { key: "peak", label: "Peak open", align: "right",
      render: (x) => (x.peak_positions == null ? DASH
        : `${x.peak_positions} · ${num(x.peak_open_risk_pct ?? 0, 2)}% risk`) },
  ];
  return <DataTable columns={columns} rows={rows} rowKey={(x) => `${x.origin}-${x.strategy}`} />;
}

function TradesTable({ rows }: { rows: BacktestTrade[] }) {
  const columns: Column<BacktestTrade>[] = [
    { key: "origin", label: "Origin", render: (t) => ORIGIN_LABEL[t.origin], sortValue: (t) => t.origin },
    { key: "asof", label: "As-of", render: (t) => <HoverTip text={t.source_file}>{t.report_as_of}</HoverTip>,
      sortValue: (t) => t.report_as_of },
    { key: "bot", label: "Bot", render: (t) => BOT_LABEL[t.bot], sortValue: (t) => t.bot },
    { key: "sym", label: "Symbol", render: (t) => <strong><Ticker symbol={t.symbol} /></strong>,
      sortValue: (t) => t.symbol },
    { key: "side", label: "Side", render: (t) => t.side },
    { key: "qty", label: "Qty", align: "right", render: (t) => t.qty ?? DASH, sortValue: (t) => t.qty ?? 0 },
    { key: "lv", label: "Levels", render: (t) => <HoverTip text={t.detail}>{t.levels_source}</HoverTip> },
    { key: "ed", label: "Entry date", render: (t) => t.entry_date, sortValue: (t) => t.entry_date },
    { key: "entry", label: "Entry", align: "right", render: (t) => num(t.entry) },
    { key: "stop", label: "Stop", align: "right", render: (t) => num(t.stop) },
    { key: "target", label: "Target", align: "right", render: (t) => num(t.target) },
    { key: "xd", label: "Exit date", render: (t) => t.exit_date, sortValue: (t) => t.exit_date },
    { key: "exit", label: "Exit", align: "right", render: (t) => num(t.exit) },
    { key: "why", label: "Exit reason", render: (t) => t.exit_reason.replace(/_/g, " "),
      sortValue: (t) => t.exit_reason },
    { key: "r", label: "R", align: "right", render: (t) => r(t.r), sortValue: (t) => t.r },
    { key: "notional", label: "Notional", align: "right", render: (t) => (t.notional == null ? DASH : money(t.notional)),
      sortValue: (t) => t.notional ?? 0 },
    { key: "fees", label: "Fees", align: "right", render: (t) => (t.fees ? money(t.fees) : DASH) },
    { key: "flags", label: "Flags", render: (t) => <TradeFlags t={t} /> },
    { key: "usd", label: "P/L ($)", align: "right", sortValue: (t) => t.pnl_usd,
      render: (t) => {
        const v = <Delta value={t.pnl_usd}>{signedMoney(t.pnl_usd)}</Delta>;
        return t.exit_reason === "open_at_end" || t.exit_reason === "bars_end"
          ? <HoverTip text="Unrealised: marked at the last close; not counted in the summary">{v}*</HoverTip> : v;
      } },
    { key: "held", label: "Sessions", align: "right", render: (t) => t.sessions_held,
      sortValue: (t) => t.sessions_held },
  ];
  return <DataTable columns={columns} rows={rows} empty="No simulated trades."
                    rowKey={(t) => `${t.origin}-${t.bot}-${t.symbol}-${t.run_date}-${t.source_file}`}
                    initialSort={{ key: "ed", dir: "asc" }} />;
}

function TradeFlags({ t }: { t: BacktestTrade }) {
  const flags: { label: string; tip: string }[] = [];
  if (t.outlier) flags.push({ label: ">5R", tip: "Outlier: over 5R. Check the stop distance and the size." });
  if (t.capped) flags.push({ label: "capped", tip: "Fixed risk wanted more shares than the capital buys; held to the capital." });
  if (t.calm_scale_fallback) flags.push({ label: "calm 1.0",
    tip: "SPY or QQQ daily bars were missing on the run date, so the calm scale was taken as 1.0 (live would not ticket)." });
  if (t.entry_type === "limit") flags.push({ label: "limit", tip: "Filled by the DAY limit at the channel line." });
  if (!flags.length) return <span className="muted">{DASH}</span>;
  return (
    <span className="row" style={{ gap: 4 }}>
      {flags.map((f) => (
        <HoverTip key={f.label} text={f.tip}>
          <span className="badge" style={f.label === "limit" ? undefined
            : { color: "var(--warning-text)", borderColor: "var(--warning)" }}>{f.label}</span>
        </HoverTip>
      ))}
    </span>
  );
}

function DataWarnings({ data }: { data: BacktestResult }) {
  const rebuilt = data.coverage.filter((d) => d.status === "reconstructed");
  const daysWith = (needle: string) => rebuilt.filter((d) => d.warnings.some((w) => w.includes(needle))).length;
  const missingFundamentals = rebuilt.reduce((n, d) => {
    if (d.fundamentals) return n + d.fundamentals.missing;
    const w = d.warnings.find((x) => x.includes("no cached SEC file"));
    const m = w?.match(/^(\d+) of/);
    return n + (m ? Number(m[1]) : 0);
  }, 0);
  const noBars = data.skipped.filter((s) => s.reason.startsWith("no cached")).length;
  const count = (f: (t: BacktestTrade) => boolean) => data.trades.filter(f).length;
  const calmDays = data.calm_scale_fallback_dates ?? [];
  const lines: string[] = [];
  if (rebuilt.length) {
    const unranked = daysWith("ranked side handoff refused");
    if (unranked) lines.push(`${unranked} of ${rebuilt.length} rebuilt day(s) used the unranked side handoff (sectors missing from sector_map.py)`);
    const spread = daysWith("spread gate not applied");
    if (spread) lines.push(`${spread} rebuilt day(s) skipped the spread gate`);
    const assets = daysWith("asset list was fetched");
    if (assets) lines.push(`${assets} rebuilt day(s) used an asset list fetched after the as-of date (survivorship)`);
    if (missingFundamentals) lines.push(`${missingFundamentals} name-days had no SEC fundamentals and failed the financial gates`);
  }
  if (noBars) lines.push(`${noBars} name(s) skipped for lack of cached bars after the run date`);
  const ended = count((t) => t.exit_reason === "bars_end");
  if (ended) lines.push(`${ended} trade(s) whose bars stopped early (bars ended): left out of the figures`);
  const open = count((t) => t.exit_reason === "open_at_end");
  if (open) lines.push(`${open} trade(s) still open at the data end: left out of the figures`);
  if (calmDays.length) lines.push(`calm scale taken as 1.0 on ${calmDays.length} day(s) with no SPY / QQQ bar: ${calmDays.join(", ")}`);
  const capped = count((t) => !!t.capped);
  if (capped) lines.push(`${capped} fixed-risk trade(s) capped at the capital`);
  const outliers = count((t) => !!t.outlier);
  if (outliers) lines.push(`${outliers} trade(s) over 5R; see "Without best trade" in the summary`);
  return (
    <Card title="Data warnings">
      {lines.length ? <ul>{lines.map((l) => <li key={l}>{l}</li>)}</ul> : <p className="muted">None.</p>}
      <ul className="muted">
        {(data.assumptions ?? ["Starting capital stands in for equity and buying power; there is no compounding."])
          .map((a) => <li key={a}>{a}</li>)}
      </ul>
    </Card>
  );
}

function SkippedTable({ rows }: { rows: BacktestSkip[] }) {
  const columns: Column<BacktestSkip>[] = [
    { key: "origin", label: "Origin", render: (s) => ORIGIN_LABEL[s.origin], sortValue: (s) => s.origin },
    { key: "asof", label: "As-of", render: (s) => s.report_as_of, sortValue: (s) => s.report_as_of },
    { key: "bot", label: "Bot", render: (s) => BOT_LABEL[s.bot], sortValue: (s) => s.bot },
    { key: "sym", label: "Symbol", render: (s) => <Ticker symbol={s.symbol} />, sortValue: (s) => s.symbol },
    { key: "src", label: "Source", render: (s) => <span className="muted">{s.source_file}</span> },
    { key: "why", label: "Reason", render: (s) => s.reason, sortValue: (s) => s.reason },
  ];
  return <DataTable columns={columns} rows={rows} empty="Nothing skipped."
                    rowKey={(s) => `${s.origin}-${s.bot}-${s.symbol}-${s.run_date}-${s.source_file}`} />;
}
