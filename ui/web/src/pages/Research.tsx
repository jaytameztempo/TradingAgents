import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, ResearchRun, StateLog } from "../api";
import { Card, Dialog, Empty, ErrorBox, JobLog, Markdown, Tabs } from "../components/ui";
import { dateTime } from "../format";
import { useJob } from "../useJob";

const ANALYSTS = ["market", "social", "news", "fundamentals"] as const;

export default function Research() {
  return (
    <>
      <div className="page-head"><h1>Research</h1></div>
      <RunCard />
      <RunsBrowser />
      <ReportsBrowser />
    </>
  );
}

function RunCard() {
  const dates = useQuery({ queryKey: ["dates"], queryFn: api.dates });
  const [asOf, setAsOf] = useState<string>("");
  const date = asOf || dates.data?.[0] || "";
  const summary = useQuery({ queryKey: ["summary", date], queryFn: () => api.summary(date), enabled: !!date });
  const route = summary.data?.route?.data;
  const routed = route ? [...route.tradeable.map((t) => ({ t, s: "ALLOWED" })), ...route.blocked.map((t) => ({ t, s: "BLOCKED" }))] : [];
  const [ticker, setTicker] = useState("");
  const chosen = routed.some((r) => r.t === ticker) ? ticker : routed[0]?.t ?? "";
  const [analysts, setAnalysts] = useState<string[]>(["market"]);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const runner = useJob();

  const toggle = (a: string) =>
    setAnalysts((prev) => (prev.includes(a) ? prev.filter((x) => x !== a) : [...prev, a]));

  return (
    <Card title="Run TradingAgents research">
      <div className="form">
        <label className="field">Route date
          <select value={date} onChange={(e) => setAsOf(e.target.value)}>
            {(dates.data ?? []).map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </label>
        <label className="field">Ticker (from the route)
          <select value={chosen} onChange={(e) => setTicker(e.target.value)} disabled={!routed.length}>
            {routed.map((r) => <option key={r.t} value={r.t}>{r.t} · {r.s}</option>)}
          </select>
        </label>
        <fieldset className="field" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend>Analysts</legend>
          <div className="checks">
            {ANALYSTS.map((a) => (
              <label key={a}><input type="checkbox" checked={analysts.includes(a)} onChange={() => toggle(a)} /> {a}</label>
            ))}
          </div>
        </fieldset>
        <button className="btn btn-primary" disabled={runner.running || !chosen || !analysts.length}
                onClick={() => setConfirmOpen(true)}>
          {runner.running ? "Running…" : "Run research…"}
        </button>
      </div>
      {!routed.length && date && !summary.isLoading && (
        <p className="muted">No routed tickers for {date}. Run the pipeline (Dashboard) or the router (Strategy) first.</p>
      )}
      <p className="muted">Runs the LangGraph agents unchanged via run_research. Research only; no order is placed.</p>
      <ErrorBox error={runner.error} />
      <JobLog job={runner.job} onDone={runner.onDone} />
      <Dialog
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title="Run TradingAgents?"
        actions={
          <button className="btn btn-primary" onClick={() => {
            setConfirmOpen(false);
            runner.start(() => api.runResearch({ as_of: date, ticker: chosen, analysts, confirm: true }));
          }}>Run</button>
        }
      >
        <p><strong>{chosen}</strong> as of <strong>{date}</strong> with {analysts.join(", ")}.</p>
        <p>This calls your configured LLM provider several times and can take several minutes. It costs API credits.</p>
      </Dialog>
    </Card>
  );
}

type Section = "analysts" | "debate" | "trader" | "risk" | "decision";

function RunsBrowser() {
  const runs = useQuery({ queryKey: ["researchRuns"], queryFn: api.researchRuns });
  const [selected, setSelected] = useState<ResearchRun | null>(null);
  const current = selected ?? runs.data?.[0] ?? null;
  const state = useQuery({
    queryKey: ["state", current?.ticker, current?.date],
    queryFn: () => api.researchState(current!.ticker, current!.date),
    enabled: !!current,
  });

  return (
    <Card title="Research runs">
      <ErrorBox error={runs.error} />
      {!runs.data?.length ? <Empty>No research runs yet.</Empty> : (
        <div className="split">
          <div className="list">
            {runs.data.map((r) => (
              <button key={`${r.ticker}-${r.date}`}
                      className={`list-item ${current && r.ticker === current.ticker && r.date === current.date ? "active" : ""}`}
                      onClick={() => setSelected(r)}>
                <span>{r.ticker} · {r.date}</span>
                <span className="badge">{r.rating ?? "—"}</span>
              </button>
            ))}
          </div>
          <div>
            <ErrorBox error={state.error} />
            {state.data && <StateView state={state.data} />}
          </div>
        </div>
      )}
    </Card>
  );
}

function StateView({ state }: { state: StateLog }) {
  const [tab, setTab] = useState<Section>("decision");
  const debate = state.investment_debate_state ?? {};
  const risk = state.risk_debate_state ?? {};
  return (
    <>
      <div className="row" style={{ marginBottom: 8 }}>
        <h2>{state.company_of_interest} · {state.trade_date}</h2>
        <span className="badge">Rating: {state.final_rating ?? "—"}</span>
        {state.run_settings?.deep_think_llm != null && (
          <span className="muted">{String(state.run_settings.llm_provider)} · {String(state.run_settings.deep_think_llm)}</span>
        )}
      </div>
      <Tabs value={tab} onChange={setTab} tabs={[
        { id: "decision", label: "Final decision" },
        { id: "analysts", label: "Analysts" },
        { id: "debate", label: "Bull / Bear" },
        { id: "trader", label: "Trader" },
        { id: "risk", label: "Risk" },
      ]} />
      {tab === "decision" && <Markdown text={state.final_trade_decision} />}
      {tab === "analysts" && (
        <>
          {([["Market", state.market_report], ["Sentiment", state.sentiment_report], ["News", state.news_report],
             ["Fundamentals", state.fundamentals_report]] as const)
            .filter(([, text]) => text)
            .map(([label, text]) => <details key={label} open><summary><strong>{label}</strong></summary><Markdown text={text} /></details>)}
        </>
      )}
      {tab === "debate" && (
        <>
          <details open><summary><strong>Bull</strong></summary><Markdown text={debate.bull_history} /></details>
          <details><summary><strong>Bear</strong></summary><Markdown text={debate.bear_history} /></details>
          <details><summary><strong>Research manager</strong></summary><Markdown text={state.investment_plan} /></details>
        </>
      )}
      {tab === "trader" && <Markdown text={state.trader_investment_plan} />}
      {tab === "risk" && (
        <>
          <details open><summary><strong>Aggressive</strong></summary><Markdown text={risk.aggressive_history} /></details>
          <details><summary><strong>Conservative</strong></summary><Markdown text={risk.conservative_history} /></details>
          <details><summary><strong>Neutral</strong></summary><Markdown text={risk.neutral_history} /></details>
        </>
      )}
    </>
  );
}

function ReportsBrowser() {
  const reports = useQuery({ queryKey: ["reports"], queryFn: api.reports });
  const [name, setName] = useState<string | null>(null);
  const current = name ?? reports.data?.[0]?.name ?? null;
  const report = useQuery({ queryKey: ["report", current], queryFn: () => api.report(current!), enabled: !!current });
  const [section, setSection] = useState("complete_report.md");
  const sections = report.data?.sections ?? {};
  const keys = Object.keys(sections);
  const shown = sections[section] != null ? section : keys[0];

  return (
    <Card title="Saved report folders">
      <ErrorBox error={reports.error ?? report.error} />
      {!reports.data?.length ? <Empty>No saved reports.</Empty> : (
        <div className="split">
          <div className="list">
            {reports.data.map((r) => (
              <button key={r.name} className={`list-item ${r.name === current ? "active" : ""}`} onClick={() => setName(r.name)}>
                <span>{r.ticker}</span><span className="muted">{dateTime(r.created)}</span>
              </button>
            ))}
          </div>
          <div>
            <div className="form" style={{ marginBottom: 8 }}>
              <select value={shown} onChange={(e) => setSection(e.target.value)} aria-label="Report section">
                {keys.map((k) => <option key={k} value={k}>{k}</option>)}
              </select>
            </div>
            <Markdown text={shown ? sections[shown] : null} />
          </div>
        </div>
      )}
    </Card>
  );
}
