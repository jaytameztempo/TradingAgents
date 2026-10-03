import { Fragment, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, Artifact, defaultAsOf, Regime, Route } from "../api";
import { Card, Empty, ErrorBox, JobLog, RegimeBadge } from "../components/ui";
import { dateTime, num } from "../format";
import { useJob } from "../useJob";

type Bot = "up" | "down" | "side";
const BOT_FOR_LABEL: Record<string, Bot> = { UP: "up", DOWN: "down", SIDE: "side" };
const BOT_NAME: Record<Bot, string> = { up: "UPBot", down: "DOWNBot", side: "SIDEBot" };

export default function Strategy() {
  return (
    <>
      <div className="page-head"><h1>Strategy</h1></div>
      <div className="grid grid-2">
        <RegimeCard />
        <RouteCard />
      </div>
      <BotCard />
    </>
  );
}

function Picker<T>({ items, value, onChange, label }: {
  items: Artifact<T>[]; value: Artifact<T> | undefined; onChange: (file: string) => void; label: string;
}) {
  return (
    <select aria-label={label} value={value?.file ?? ""} onChange={(e) => onChange(e.target.value)}>
      {items.map((i) => <option key={i.file} value={i.file}>{i.as_of} · {i.file.split("_").slice(-1)[0].replace(".json", "")}</option>)}
    </select>
  );
}

function RegimeCard() {
  const regimes = useQuery({ queryKey: ["regimes"], queryFn: api.regimes });
  const [file, setFile] = useState<string | null>(null);
  const list = regimes.data ?? [];
  const current = list.find((r) => r.file === file) ?? list[0];
  const [asOf, setAsOf] = useState(defaultAsOf());
  const [symbol, setSymbol] = useState("SPY");
  const runner = useJob();
  const r: Regime | undefined = current?.data;

  return (
    <Card title="Regime (RegimeBot)" actions={list.length > 0 && <Picker items={list} value={current} onChange={setFile} label="Regime file" />}>
      <ErrorBox error={regimes.error ?? current?.error} />
      {r ? (
        <>
          <div className="row" style={{ marginBottom: 8 }}>
            <RegimeBadge label={r.label} /><strong>{r.symbol}</strong><span className="muted">{r.as_of} · {dateTime(r.created_at)}</span>
          </div>
          <ul>{r.reasons.map((x) => <li key={x}>{x}</li>)}</ul>
          <dl className="kv">
            {Object.entries(r.metrics).map(([k, v]) => (
              <Fragment key={k}><dt>{k}</dt><dd>{typeof v === "boolean" ? (v ? "yes" : "no") : num(v)}</dd></Fragment>
            ))}
          </dl>
        </>
      ) : <Empty>No regime labels yet.</Empty>}
      <hr style={{ border: 0, borderTop: "1px solid var(--border)", margin: "16px 0" }} />
      <div className="form">
        <label className="field">Date<input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} /></label>
        <label className="field">Symbol<input value={symbol} size={6} onChange={(e) => setSymbol(e.target.value.toUpperCase())} /></label>
        <button className="btn btn-primary" disabled={runner.running}
                onClick={() => runner.start(() => api.runRegime({ as_of: asOf, symbol }))}>Run RegimeBot</button>
      </div>
      <ErrorBox error={runner.error} />
      <JobLog job={runner.job} onDone={runner.onDone} />
    </Card>
  );
}

function RouteCard() {
  const routes = useQuery({ queryKey: ["routes"], queryFn: api.routes });
  const [file, setFile] = useState<string | null>(null);
  const list = routes.data ?? [];
  const current = list.find((r) => r.file === file) ?? list[0];
  const [asOf, setAsOf] = useState(defaultAsOf());
  const runner = useJob();
  const r: Route | undefined = current?.data;
  const chips = (xs?: string[]) => xs?.length ? xs.map((t) => <span key={t} className="chip">{t}</span>) : <span className="muted">none</span>;

  return (
    <Card title="Route (strategy router)" actions={list.length > 0 && <Picker items={list} value={current} onChange={setFile} label="Route file" />}>
      <ErrorBox error={routes.error ?? current?.error} />
      {r ? (
        <>
          <div className="row" style={{ marginBottom: 8 }}>
            <RegimeBadge label={r.regime_label} /><span className="muted">{r.regime_symbol} · {r.as_of} · series {r.series}</span>
          </div>
          <p>{r.reason}</p>
          <dl className="kv">
            <dt>UPBot</dt><dd>{r.up_allowed ? "✓ allowed" : "✕ blocked"}</dd>
            <dt>DOWNBot</dt><dd>{r.down_allowed ? "✓ allowed" : "✕ blocked"}</dd>
            <dt>Tradeable</dt><dd className="row">{chips(r.tradeable)}</dd>
            <dt>Blocked</dt><dd className="row">{chips(r.blocked)}</dd>
            <dt>Up scan passed</dt><dd className="row">{chips(r.up_passed)}</dd>
            <dt>Down scan passed</dt><dd className="row">{chips(r.down_passed)}</dd>
          </dl>
        </>
      ) : <Empty>No route decisions yet.</Empty>}
      <hr style={{ border: 0, borderTop: "1px solid var(--border)", margin: "16px 0" }} />
      <div className="form">
        <label className="field">Date<input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} /></label>
        <button className="btn btn-primary" disabled={runner.running}
                onClick={() => runner.start(() => api.runRouter({ as_of: asOf }))}>Run router</button>
      </div>
      <p className="muted">Reads the newest baskets and regime label for the date; needs both scans and RegimeBot first.</p>
      <ErrorBox error={runner.error} />
      <JobLog job={runner.job} onDone={runner.onDone} />
    </Card>
  );
}

function BotCard() {
  const dates = useQuery({ queryKey: ["dates"], queryFn: api.dates });
  const [asOf, setAsOf] = useState("");
  const date = asOf || dates.data?.[0] || "";
  const summary = useQuery({ queryKey: ["summary", date], queryFn: () => api.summary(date), enabled: !!date });
  const route = summary.data?.route?.data;
  const routed = route ? [...route.tradeable, ...route.blocked] : [];
  const [symbol, setSymbol] = useState("");
  const chosen = routed.includes(symbol) ? symbol : routed[0] ?? "";
  const [botChoice, setBotChoice] = useState<Bot | "">("");
  const bot: Bot = botChoice || (route ? BOT_FOR_LABEL[route.regime_label] : "side") || "side";
  const runner = useJob();
  const plans = useQuery({ queryKey: ["plans"], queryFn: api.plans });
  const latestPlan = plans.data?.find((p) => p.as_of === date && p.symbol === chosen && p.bot === BOT_NAME[bot].toLowerCase());

  return (
    <Card title="Playbooks (UPBot / DOWNBot / SIDEBot)">
      <div className="form">
        <label className="field">Route date
          <select value={date} onChange={(e) => setAsOf(e.target.value)}>
            {(dates.data ?? []).map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </label>
        <label className="field">Bot
          <select value={bot} onChange={(e) => setBotChoice(e.target.value as Bot)}>
            {(["up", "down", "side"] as Bot[]).map((b) => (
              <option key={b} value={b}>{BOT_NAME[b]}{route && BOT_FOR_LABEL[route.regime_label] === b ? " (matches regime)" : ""}</option>
            ))}
          </select>
        </label>
        <label className="field">Symbol
          <select value={chosen} onChange={(e) => setSymbol(e.target.value)} disabled={!routed.length}>
            {routed.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
        <button className="btn btn-primary" disabled={runner.running || !chosen}
                onClick={() => runner.start(() => api.runBot({ as_of: date, bot, symbol: chosen }))}>
          Run {BOT_NAME[bot]}
        </button>
        {route && <span className="row">Regime: <RegimeBadge label={route.regime_label} /></span>}
      </div>
      <ErrorBox error={runner.error} />
      {runner.result?.status === "blocked" && (
        <div className="notice notice-warn" style={{ marginTop: 12 }}>
          ◆ Regime mismatch: {BOT_NAME[bot]} is blocked because the route's regime is {route?.regime_label}. No plan was written.
        </div>
      )}
      <JobLog job={runner.job} onDone={runner.onDone} />
      {latestPlan && (
        <div style={{ marginTop: 12 }}>
          <h2>Latest {BOT_NAME[bot]} plan for {chosen}</h2>
          <pre className="plain">{latestPlan.text}</pre>
        </div>
      )}
    </Card>
  );
}
