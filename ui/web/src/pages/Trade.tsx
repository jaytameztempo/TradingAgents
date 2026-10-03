import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, Plan } from "../api";
import { Card, Empty, ErrorBox, RegimeBadge } from "../components/ui";

// Read-only plan review. Nothing on this page talks to the broker.
const CHECKLIST = [
  "Regime label still matches the bot",
  "Ticker is tradeable (not blocked) in the latest route",
  "Research rating supports the direction",
  "Entry, stop and target written down",
  "Position size fits risk per trade",
];

interface Review { checks: boolean[]; notes: string }

function loadReview(file: string): Review {
  try {
    const raw = localStorage.getItem(`review:${file}`);
    if (raw) return JSON.parse(raw) as Review;
  } catch { /* storage unavailable: start blank */ }
  return { checks: CHECKLIST.map(() => false), notes: "" };
}

function saveReview(file: string, review: Review) {
  try { localStorage.setItem(`review:${file}`, JSON.stringify(review)); } catch { /* not persisted */ }
}

export default function Trade() {
  const plans = useQuery({ queryKey: ["plans"], queryFn: api.plans });
  const [file, setFile] = useState<string | null>(null);
  const current = plans.data?.find((p) => p.file === file) ?? plans.data?.[0];

  return (
    <>
      <div className="page-head"><h1>Trade</h1></div>
      <div className="banner" role="note">
        <span className="badge badge-paper">PAPER</span>
        <span>🔒 Paper keys only — order placement is disabled in this build. This page reviews plans; it sends nothing to Alpaca.</span>
      </div>
      <Card title="Plans">
        <ErrorBox error={plans.error} />
        {!plans.data?.length ? <Empty>No plans yet. Run a playbook on the Strategy page.</Empty> : (
          <div className="split">
            <div className="list">
              {plans.data.map((p) => (
                <button key={p.file} className={`list-item ${p === current ? "active" : ""}`} onClick={() => setFile(p.file)}>
                  <span>{p.symbol} · {p.as_of}</span><span className="muted">{p.bot}</span>
                </button>
              ))}
            </div>
            {current && <PlanReview plan={current} />}
          </div>
        )}
      </Card>
    </>
  );
}

function PlanReview({ plan }: { plan: Plan }) {
  const summary = useQuery({ queryKey: ["summary", plan.as_of], queryFn: () => api.summary(plan.as_of ?? undefined), enabled: !!plan.as_of });
  const runs = useQuery({ queryKey: ["researchRuns"], queryFn: api.researchRuns });
  const route = summary.data?.route?.data;
  const status = !route || !plan.symbol ? null
    : route.tradeable.includes(plan.symbol) ? "Tradeable" : route.blocked.includes(plan.symbol) ? "Blocked" : "Not in route";
  const rating = runs.data?.find((r) => r.ticker === plan.symbol && r.date === plan.as_of);
  const regimeLabel = plan.fields.regime?.split(" ")[0];

  const [review, setReview] = useState<Review>(() => loadReview(plan.file));
  useEffect(() => setReview(loadReview(plan.file)), [plan.file]);
  const update = (next: Review) => { setReview(next); saveReview(plan.file, next); };

  return (
    <div className="grid">
      <div className="row">
        <h2>{plan.title}</h2>
        <RegimeBadge label={regimeLabel} />
      </div>
      <div className="grid grid-3">
        <div className="card">
          <div className="stat-label">Plan</div>
          <dl className="kv">
            <dt>Symbol</dt><dd><strong>{plan.symbol}</strong></dd>
            <dt>Date</dt><dd>{plan.as_of}</dd>
            <dt>Regime</dt><dd>{plan.fields.regime ?? "—"}</dd>
            <dt>Entry / stop / target</dt><dd className="muted">not in this plan</dd>
          </dl>
        </div>
        <div className="card">
          <div className="stat-label">Latest route ({plan.as_of})</div>
          {route ? (
            <dl className="kv">
              <dt>Regime</dt><dd><RegimeBadge label={route.regime_label} /></dd>
              <dt>{plan.symbol}</dt><dd>{status}</dd>
            </dl>
          ) : <span className="muted">No route for this date</span>}
          {route && <p className="muted" style={{ marginBottom: 0 }}>{route.reason}</p>}
        </div>
        <div className="card">
          <div className="stat-label">Research rating</div>
          {rating ? <div className="stat-value">{rating.rating ?? "—"}</div> : <span className="muted">No research run for {plan.symbol} on {plan.as_of}</span>}
        </div>
      </div>
      {plan.fields["mean-reversion note"] || plan.fields["trend-following note"] ? (
        <div className="notice">{plan.fields["mean-reversion note"] ?? plan.fields["trend-following note"]}</div>
      ) : null}
      <div className="grid grid-2">
        <div>
          <h2>Checklist</h2>
          <div className="grid" style={{ gap: 6, marginTop: 8 }}>
            {CHECKLIST.map((item, i) => (
              <label key={item}>
                <input type="checkbox" checked={review.checks[i] ?? false}
                       onChange={(e) => update({ ...review, checks: review.checks.map((c, j) => (j === i ? e.target.checked : c)) })} />{" "}
                {item}
              </label>
            ))}
          </div>
        </div>
        <div>
          <h2>Notes</h2>
          <textarea value={review.notes} placeholder="Your notes (saved in this browser only)"
                    onChange={(e) => update({ ...review, notes: e.target.value })} style={{ marginTop: 8 }} />
        </div>
      </div>
      <details>
        <summary>Plan file · {plan.file}</summary>
        <pre className="plain">{plan.text}</pre>
      </details>
    </div>
  );
}
