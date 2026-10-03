import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, defaultAsOf, HistoryRange, Job, Order, Position } from "../api";
import {
  Card, Column, DataTable, Delta, Dialog, ErrorBox, isMissingKeys, JobLog, RegimeBadge, Stat, Tabs,
} from "../components/ui";
import { dateTime, money, num, qty, signedMoney, signedPct } from "../format";

const REFRESH_MS = 15_000;
const RANGES: HistoryRange[] = ["1D", "1W", "1M", "1Y"];

export default function Dashboard() {
  const account = useQuery({ queryKey: ["account"], queryFn: api.account, refetchInterval: REFRESH_MS });
  const keysMissing = isMissingKeys(account.error);

  return (
    <>
      <div className="page-head">
        <h1>Dashboard</h1>
        <span className="muted">Paper account · refreshes every 15s</span>
      </div>
      {keysMissing ? <ConnectKeys message={(account.error as Error).message} /> : (
        <>
          <AccountCard />
          <PositionsOrders />
        </>
      )}
      <PipelineStrip />
    </>
  );
}

function ConnectKeys({ message }: { message: string }) {
  return (
    <Card title="Connect paper keys">
      <p>Add your Alpaca <strong>paper</strong> keys to <code>.env</code> in the repo root, then restart the UI:</p>
      <pre className="plain">ALPACA_API_KEY=PK...{"\n"}ALPACA_SECRET_KEY=...</pre>
      <p className="muted">{message}</p>
    </Card>
  );
}

function AccountCard() {
  const [range, setRange] = useState<HistoryRange>("1M");
  const account = useQuery({ queryKey: ["account"], queryFn: api.account, refetchInterval: REFRESH_MS });
  const history = useQuery({ queryKey: ["history", range], queryFn: () => api.history(range), refetchInterval: REFRESH_MS * 4 });
  const a = account.data;
  const points = (history.data?.points ?? []).filter((p) => p.equity != null && p.equity > 0);
  const first = points[0]?.equity ?? null;
  const last = points[points.length - 1]?.equity ?? null;
  const rangeChange = first != null && last != null ? last - first : null;
  const rangePct = rangeChange != null && first ? (rangeChange / first) * 100 : null;
  const intraday = range === "1D" || range === "1W";

  return (
    <Card
      title={<>Portfolio {a?.account_number && <span className="muted">· {a.account_number}</span>}</>}
      actions={
        <div className="seg" role="group" aria-label="Chart range">
          {RANGES.map((r) => (
            <button key={r} className={r === range ? "active" : ""} onClick={() => setRange(r)}>{r}</button>
          ))}
        </div>
      }
    >
      <ErrorBox error={account.error} />
      <div className="stats">
        <div className="stat-hero">
          <Stat
            label="Equity"
            value={money(a?.equity)}
            sub={<Delta value={a?.daily_change}>{signedMoney(a?.daily_change)} ({signedPct(a?.daily_change_pct)}) today</Delta>}
          />
        </div>
        <Stat label="Buying power" value={money(a?.buying_power)} />
        <Stat label="Cash" value={money(a?.cash)} />
        <Stat label={`${range} change`} value={<Delta value={rangeChange}>{signedMoney(rangeChange)}</Delta>}
              sub={<Delta value={rangePct}>{signedPct(rangePct)}</Delta>} />
      </div>
      <ErrorBox error={history.error} />
      <div className="chart" aria-label={`Portfolio equity, ${range}`}>
        {points.length > 1 ? (
          <ResponsiveContainer>
            <AreaChart data={points} margin={{ top: 16, right: 8, bottom: 0, left: 8 }}>
              <defs>
                <linearGradient id="eq-fill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="var(--series-1)" stopOpacity={0.18} />
                  <stop offset="100%" stopColor="var(--series-1)" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid vertical={false} stroke="var(--grid)" />
              <XAxis
                dataKey="t" type="number" domain={["dataMin", "dataMax"]} scale="time"
                tickFormatter={(t: number) => new Date(t * 1000).toLocaleString(undefined, intraday
                  ? { month: "short", day: "numeric", hour: "numeric" } : { month: "short", day: "numeric" })}
                stroke="var(--muted)" tick={{ fontSize: 11 }} tickLine={false} axisLine={false} minTickGap={40}
              />
              <YAxis
                domain={["auto", "auto"]} width={80} stroke="var(--muted)" tick={{ fontSize: 11 }}
                tickLine={false} axisLine={false}
                tickFormatter={(v: number) => `$${Math.round(v).toLocaleString()}`}
              />
              <Tooltip
                cursor={{ stroke: "var(--muted)", strokeDasharray: "3 3" }}
                content={({ active, payload }) => {
                  const p = active && payload?.[0]?.payload;
                  if (!p) return null;
                  return (
                    <div className="chart-tip">
                      <div className="muted">{new Date(p.t * 1000).toLocaleString()}</div>
                      <div><strong>{money(p.equity)}</strong></div>
                      <Delta value={p.pnl}>{signedMoney(p.pnl)} ({signedPct(p.pnl_pct)})</Delta>
                    </div>
                  );
                }}
              />
              <Area type="monotone" dataKey="equity" stroke="var(--series-1)" strokeWidth={2}
                    fill="url(#eq-fill)" dot={false} activeDot={{ r: 4 }} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        ) : (
          <div className="empty">{history.isLoading ? "Loading chart…" : "No portfolio history for this range."}</div>
        )}
      </div>
    </Card>
  );
}

const DISABLED_REASON = "Order placement is disabled in this build";

function PositionsOrders() {
  const [tab, setTab] = useState<"positions" | "orders">("positions");
  const [closeAllOpen, setCloseAllOpen] = useState(false);
  const positions = useQuery({ queryKey: ["positions"], queryFn: api.positions, refetchInterval: REFRESH_MS });
  const orders = useQuery({ queryKey: ["orders"], queryFn: api.orders, refetchInterval: REFRESH_MS });

  const positionCols: Column<Position>[] = [
    { key: "symbol", label: "Symbol", render: (p) => <strong>{p.symbol}</strong>, sortValue: (p) => p.symbol },
    { key: "side", label: "Long/Short", render: (p) => p.side, sortValue: (p) => p.side },
    { key: "qty", label: "Qty", align: "right", render: (p) => qty(p.qty), sortValue: (p) => p.qty },
    { key: "avg", label: "Avg Entry", align: "right", render: (p) => money(p.avg_entry_price), sortValue: (p) => p.avg_entry_price },
    { key: "mv", label: "Market Value", align: "right", render: (p) => money(p.market_value), sortValue: (p) => p.market_value },
    { key: "tpp", label: "Today's P/L %", align: "right", render: (p) => <Delta value={p.today_pl_pct}>{signedPct(p.today_pl_pct)}</Delta>, sortValue: (p) => p.today_pl_pct },
    { key: "tpd", label: "Today's P/L $", align: "right", render: (p) => <Delta value={p.today_pl}>{signedMoney(p.today_pl)}</Delta>, sortValue: (p) => p.today_pl },
    { key: "pp", label: "Total P/L %", align: "right", render: (p) => <Delta value={p.total_pl_pct}>{signedPct(p.total_pl_pct)}</Delta>, sortValue: (p) => p.total_pl_pct },
    { key: "pd", label: "Total P/L $", align: "right", render: (p) => <Delta value={p.total_pl}>{signedMoney(p.total_pl)}</Delta>, sortValue: (p) => p.total_pl },
    {
      key: "action", label: "Action", align: "center",
      render: (p) => (
        <span title={DISABLED_REASON}>
          <button className="btn btn-sm" disabled aria-label={`Liquidate ${p.symbol} (disabled)`}>Liquidate</button>
        </span>
      ),
    },
  ];

  const orderCols: Column<Order>[] = [
    { key: "symbol", label: "Symbol", render: (o) => <strong>{o.symbol}</strong>, sortValue: (o) => o.symbol },
    { key: "ls", label: "Long/Short", render: (o) => o.position_side ?? "—", sortValue: (o) => o.position_side },
    { key: "side", label: "Buy/Sell", render: (o) => o.side ?? "—", sortValue: (o) => o.side },
    { key: "qty", label: "Qty", align: "right", render: (o) => qty(o.qty), sortValue: (o) => o.qty },
    { key: "fq", label: "Filled Qty", align: "right", render: (o) => qty(o.filled_qty), sortValue: (o) => o.filled_qty },
    { key: "fp", label: "Avg Fill Price", align: "right", render: (o) => money(o.filled_avg_price), sortValue: (o) => o.filled_avg_price },
    {
      key: "status", label: "Status",
      render: (o) => (
        <span className={`badge ${o.filled ? "regime-up" : ""}`} title={`Alpaca status: ${o.status}`}>
          {o.filled ? "✓ Filled" : "○ Not Filled"}
        </span>
      ),
      sortValue: (o) => (o.filled ? 1 : 0),
    },
    { key: "sub", label: "Submitted", render: (o) => dateTime(o.submitted_at), sortValue: (o) => o.submitted_at },
    { key: "fill", label: "Filled", render: (o) => dateTime(o.filled_at), sortValue: (o) => o.filled_at },
  ];

  const openCount = positions.data?.length ?? 0;
  return (
    <Card
      actions={
        <button className="btn btn-danger" onClick={() => setCloseAllOpen(true)} disabled={!openCount}>
          ⚠ Close all Positions
        </button>
      }
      title={
        <Tabs
          value={tab}
          onChange={setTab}
          tabs={[
            { id: "positions", label: `Positions (${positions.data?.length ?? "…"})` },
            { id: "orders", label: `Orders (${orders.data?.length ?? "…"})` },
          ]}
        />
      }
    >
      {tab === "positions" ? (
        <>
          <ErrorBox error={positions.error} />
          <DataTable columns={positionCols} rows={positions.data ?? []} rowKey={(p) => p.symbol}
                     empty={positions.isLoading ? "Loading positions…" : "No open positions."}
                     initialSort={{ key: "mv", dir: "desc" }} />
        </>
      ) : (
        <>
          <ErrorBox error={orders.error} />
          <DataTable columns={orderCols} rows={orders.data ?? []} rowKey={(o) => o.id}
                     empty={orders.isLoading ? "Loading orders…" : "No orders."}
                     initialSort={{ key: "sub", dir: "desc" }} />
        </>
      )}
      <Dialog
        open={closeAllOpen}
        onClose={() => setCloseAllOpen(false)}
        tone="danger"
        title="⚠ Close all Positions"
        actions={
          <span title={DISABLED_REASON}>
            <button className="btn btn-danger" disabled>Close {openCount} position{openCount === 1 ? "" : "s"}</button>
          </span>
        }
      >
        <p>
          This would send market orders to close <strong>all {openCount} open position{openCount === 1 ? "" : "s"}</strong>{" "}
          on the paper account. <strong>This cannot be undone.</strong>
        </p>
        <div className="notice notice-warn">🔒 {DISABLED_REASON}. No request will be sent to Alpaca.</div>
      </Dialog>
    </Card>
  );
}

function PipelineStrip() {
  const qc = useQueryClient();
  const summary = useQuery({ queryKey: ["summary"], queryFn: () => api.summary() });
  const research = useQuery({ queryKey: ["researchRuns"], queryFn: api.researchRuns });
  const [asOf, setAsOf] = useState(defaultAsOf());
  const [job, setJob] = useState<Job | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const s = summary.data;
  const regime = s?.regime?.data;
  const route = s?.route?.data;
  const latestRun = research.data?.[0];

  async function run() {
    setError(null);
    try {
      setJob(await api.runPipeline({ as_of: asOf, tickers: [], regime_symbol: "SPY" }));
      setRunning(true);
      setRunning(true);
    } catch (e) {
      setError(e);
    }
  }

  return (
    <Card
      title={<>Pipeline {s?.as_of && <span className="muted">· {s.as_of}</span>}</>}
      actions={
        <>
          <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} aria-label="Pipeline date" />
          <button className="btn btn-primary" onClick={run} disabled={running}>
            Run full pipeline
          </button>
        </>
      }
    >
      <ErrorBox error={summary.error ?? error} />
      <div className="grid grid-3">
        <div>
          <div className="stat-label">Regime</div>
          <div className="row">
            <RegimeBadge label={regime?.label} />
            {regime && <span className="muted">{regime.symbol} · ADX {num(regime.metrics?.adx_14, 1)}</span>}
          </div>
          {regime?.reasons?.length ? <div className="muted">{regime.reasons.join("; ")}</div> : null}
        </div>
        <div>
          <div className="stat-label">Route</div>
          {route ? (
            <>
              <div className="row">Tradeable: {route.tradeable.length ? route.tradeable.map((t) => <span key={t} className="chip">{t}</span>) : <span className="muted">none</span>}</div>
              <div className="row">Blocked: {route.blocked.length ? route.blocked.map((t) => <span key={t} className="chip">{t}</span>) : <span className="muted">none</span>}</div>
            </>
          ) : <span className="muted">No route yet</span>}
        </div>
        <div>
          <div className="stat-label">Plans & research</div>
          <div>{s?.plans.length ?? 0} plan{s?.plans.length === 1 ? "" : "s"} for this date</div>
          {latestRun && <div className="muted">Latest research: {latestRun.ticker} {latestRun.date} → <strong>{latestRun.rating ?? "—"}</strong></div>}
        </div>
      </div>
      {route?.reason && <p className="muted">{route.reason}</p>}
      <JobLog job={job} onDone={() => { setRunning(false); qc.invalidateQueries(); }} />
    </Card>
  );
}
