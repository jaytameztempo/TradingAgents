import { ReactNode, useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Automation, ScanKind, SettingsData, Trading } from "../api";
import { Card, Dialog, ErrorBox } from "../components/ui";
import { dateTime } from "../format";
import { DEFAULT_PREFS, Prefs, usePrefs } from "../prefs";

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
const DAY_LABEL: Record<string, string> = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };

export default function Settings() {
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings, refetchInterval: 30_000 });
  return (
    <>
      <div className="page-head"><h1>Settings</h1></div>
      <ErrorBox error={settings.error ?? settings.data?.error} />
      <AppPrefs />
      {settings.data && (
        <>
          <AutomationCard data={settings.data} />
          <TradingCard data={settings.data} />
          <LlmCard data={settings.data} />
          <KeysCard data={settings.data} />
        </>
      )}
    </>
  );
}

// --- shared bits ---------------------------------------------------------------------

function useSave() {
  const qc = useQueryClient();
  const [state, setState] = useState<{ busy: boolean; error: unknown; saved: boolean }>({ busy: false, error: null, saved: false });
  const run = async (fn: () => Promise<unknown>) => {
    setState({ busy: true, error: null, saved: false });
    try {
      await fn();
      setState({ busy: false, error: null, saved: true });
      qc.invalidateQueries({ queryKey: ["settings"] });
      qc.invalidateQueries({ queryKey: ["health"] });
    } catch (e) {
      setState({ busy: false, error: e, saved: false });
    }
  };
  return { ...state, run };
}

function SaveRow({ save, onSave, label = "Save", note }: {
  save: ReturnType<typeof useSave>; onSave: () => void; label?: string; note?: ReactNode;
}) {
  return (
    <div className="settings-save">
      <button className="btn btn-primary" disabled={save.busy} onClick={onSave}>{save.busy ? "Saving…" : label}</button>
      {save.saved && <span className="status-ok">✓ Saved</span>}
      {note && <span className="muted">{note}</span>}
      <ErrorBox error={save.error} />
    </div>
  );
}

function DayPicker({ value, onChange }: { value: string[]; onChange: (days: string[]) => void }) {
  return (
    <div className="seg" role="group" aria-label="Days">
      {DAYS.map((d) => (
        <button key={d} className={value.includes(d) ? "active" : ""} aria-pressed={value.includes(d)}
                onClick={() => onChange(value.includes(d) ? value.filter((x) => x !== d) : DAYS.filter((x) => x === d || value.includes(x)))}>
          {DAY_LABEL[d]}
        </button>
      ))}
    </div>
  );
}

function TimeList({ value, onChange, label }: { value: string[]; onChange: (t: string[]) => void; label: string }) {
  const [draft, setDraft] = useState("");
  const add = () => {
    if (/^\d{2}:\d{2}$/.test(draft) && !value.includes(draft)) onChange([...value, draft].sort());
    setDraft("");
  };
  return (
    <div className="row">
      {value.map((t) => (
        <span key={t} className="chip time-chip">
          {t}
          <button className="chip-x" aria-label={`Remove ${t}`} onClick={() => onChange(value.filter((x) => x !== t))}>×</button>
        </span>
      ))}
      <input type="time" value={draft} onChange={(e) => setDraft(e.target.value)} aria-label={`Add ${label}`} />
      <button className="btn btn-sm" onClick={add} disabled={!draft}>Add</button>
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="settings-field">
      <div className="settings-label">{label}</div>
      <div className="settings-control">
        {children}
        {hint && <div className="muted settings-hint">{hint}</div>}
      </div>
    </div>
  );
}

function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <label className="toggle">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="toggle-track" aria-hidden="true"><span className="toggle-thumb" /></span>
      <span>{label}</span>
    </label>
  );
}

const nyTime = (iso: string | null) =>
  iso ? `${dateTime(iso)} (${new Date(iso).toLocaleString("en-US", { timeZone: "America/New_York", weekday: "short", hour: "2-digit", minute: "2-digit" })} NY)` : "—";

// --- 1. app preferences (this browser only) ----------------------------------------------

const INTERVALS: [string, string][] = [["1", "1 minute"], ["5", "5 minutes"], ["15", "15 minutes"], ["30", "30 minutes"],
  ["60", "1 hour"], ["240", "4 hours"], ["D", "1 day"], ["W", "1 week"]];
const STYLES: [string, string][] = [["1", "Candles"], ["0", "Bars (OHLC)"], ["9", "Hollow candles"], ["8", "Heikin Ashi"],
  ["2", "Line"], ["3", "Area"]];
const SCANS: [ScanKind, string][] = [["all", "All four scans"], ["both", "Both trend scans"], ["sideways", "Both sideways scans"],
  ["up", "Upward trend momentum"], ["down", "Breakdown short candidates"], ["long_side", "Long sideways channel"],
  ["short_side", "Short sideways channel"]];

function AppPrefs() {
  const { prefs, setPrefs, reset } = usePrefs();
  const set = <K extends keyof Prefs>(k: K) => (v: Prefs[K]) => setPrefs({ [k]: v } as Partial<Prefs>);
  return (
    <Card title="App preferences" actions={<button className="btn btn-sm" onClick={reset}>Reset to defaults</button>}>
      <p className="muted">Saved in this browser only and applied right away.</p>
      <Field label="Theme">
        <div className="seg" role="group" aria-label="Theme">
          {(["system", "light", "dark"] as const).map((t) => (
            <button key={t} className={prefs.theme === t ? "active" : ""} onClick={() => set("theme")(t)}>
              {t[0].toUpperCase() + t.slice(1)}
            </button>
          ))}
        </div>
      </Field>
      <Field label="Dashboard refresh" hint="How often account, positions and orders are re-read.">
        <select value={prefs.refreshSeconds} onChange={(e) => set("refreshSeconds")(Number(e.target.value))}>
          {[5, 10, 15, 30, 60, 120].map((s) => <option key={s} value={s}>Every {s} seconds</option>)}
        </select>
      </Field>
      <Field label="Chart interval" hint="The interval a TradingView chart opens on.">
        <select value={prefs.chartInterval} onChange={(e) => set("chartInterval")(e.target.value)}>
          {INTERVALS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
      </Field>
      <Field label="Chart style">
        <select value={prefs.chartStyle} onChange={(e) => set("chartStyle")(e.target.value)}>
          {STYLES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
      </Field>
      <Field label="Default scan" hint="Pre-selected on the Scans page.">
        <select value={prefs.defaultScan} onChange={(e) => set("defaultScan")(e.target.value as ScanKind)}>
          {SCANS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
      </Field>
      <Field label="Default trend-scan tickers" hint="Blank uses each scan's default universe.">
        <input value={prefs.defaultTickers} placeholder="SPY QQQ NVDA AAPL MSFT" style={{ width: "100%", maxWidth: 420 }}
               onChange={(e) => set("defaultTickers")(e.target.value.toUpperCase())} />
      </Field>
      <Field label="Regime symbol" hint={`Used by Run full pipeline and Run RegimeBot. Default ${DEFAULT_PREFS.regimeSymbol}.`}>
        <input value={prefs.regimeSymbol} size={8} onChange={(e) => set("regimeSymbol")(e.target.value.toUpperCase().trim())} />
      </Field>
    </Card>
  );
}

// --- 2. automation ---------------------------------------------------------------------

function AutomationCard({ data }: { data: SettingsData }) {
  const [a, setA] = useState<Automation>(data.automation);
  useEffect(() => setA(data.automation), [data.automation]);
  const save = useSave();
  const s = data.scheduler;
  return (
    <Card title="Automation: recurring scans">
      <SchedulerNote running={s.running} />
      <Field label="Recurring scans">
        <Toggle checked={a.enabled} onChange={(enabled) => setA({ ...a, enabled })} label={a.enabled ? "On" : "Off"} />
      </Field>
      <Field label="What runs">
        <select value={a.run} onChange={(e) => setA({ ...a, run: e.target.value as Automation["run"] })}>
          {Object.entries(data.automation_runs).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
      </Field>
      <Field label="Days"><DayPicker value={a.days} onChange={(days) => setA({ ...a, days })} /></Field>
      <Field label="Times (New York)" hint="After 4:15 PM on a weekday a run uses that day's bars; earlier, the previous weekday's.">
        <TimeList value={a.times} onChange={(times) => setA({ ...a, times })} label="scan time" />
      </Field>
      <Field label="Next run">{a.enabled ? nyTime(s.next_scan) : <span className="muted">Off</span>}</Field>
      <SaveRow save={save} onSave={() => save.run(() => api.saveAutomation(a))} />
      <EventLog events={s.events.filter((e) => e.kind !== "send")} />
    </Card>
  );
}

function SchedulerNote({ running }: { running: boolean }) {
  return (
    <div className={`notice ${running ? "" : "notice-warn"}`} style={{ marginBottom: 12 }}>
      {running
        ? "⏱ The scheduler runs inside BATS. It only runs while the BATS server is running; a time missed while it was closed is skipped, not made up."
        : "⚠ The scheduler is not running in this server (BATS_SCHEDULER=0?). Saved schedules will not fire until it is."}
    </div>
  );
}

function EventLog({ events }: { events: SettingsData["scheduler"]["events"] }) {
  if (!events.length) return null;
  return (
    <details style={{ marginTop: 12 }}>
      <summary>Recent scheduler activity ({events.length})</summary>
      <ul className="event-log">
        {events.slice(0, 15).map((e, i) => (
          <li key={i}>
            <span className={`status-${e.outcome === "started" ? "ok" : e.outcome === "error" ? "failed" : "blocked"}`}>
              {e.outcome === "started" ? "✓" : e.outcome === "error" ? "✕" : "○"} {e.outcome}
            </span>{" "}
            <span className="muted">{dateTime(e.at)} · {e.kind} {e.slot}</span> — {e.detail}
          </li>
        ))}
      </ul>
    </details>
  );
}

// --- 3. trade settings ---------------------------------------------------------------------

function TradingCard({ data }: { data: SettingsData }) {
  const [t, setT] = useState<Trading>(data.trading);
  useEffect(() => setT(data.trading), [data.trading]);
  const [confirm, setConfirm] = useState(false);
  const save = useSave();
  const s = data.scheduler;
  const turningOn = t.auto_send.enabled && !data.trading.auto_send.enabled;
  const doSave = () => save.run(() => api.saveTrading(t));

  const setSession = (i: number, key: "start" | "end", v: string) =>
    setT({ ...t, sessions: t.sessions.map((x, j) => (j === i ? { ...x, [key]: v } : x)) });

  return (
    <Card title="Trade settings: days, sessions and paper auto-send">
      {!data.trading_saved && (
        <div className="notice notice-warn" style={{ marginBottom: 12 }}>
          No trading window is saved yet, so the paper sender is not gated. Saving these settings turns the gate on.
        </div>
      )}
      <Field label="Trading days"><DayPicker value={t.days} onChange={(days) => setT({ ...t, days })} /></Field>
      <Field label="Sessions (New York)" hint="paper_orders.sender refuses a --send outside these windows. Dry runs are always allowed.">
        <div className="grid" style={{ gap: 8 }}>
          {t.sessions.map((x, i) => (
            <div key={i} className="row">
              <input type="time" value={x.start} onChange={(e) => setSession(i, "start", e.target.value)} aria-label="Session start" />
              <span>to</span>
              <input type="time" value={x.end} onChange={(e) => setSession(i, "end", e.target.value)} aria-label="Session end" />
              <button className="btn btn-sm" onClick={() => setT({ ...t, sessions: t.sessions.filter((_, j) => j !== i) })}>Remove</button>
            </div>
          ))}
          <div><button className="btn btn-sm" onClick={() => setT({ ...t, sessions: [...t.sessions, { start: "13:00", end: "15:00" }] })}>Add session</button></div>
        </div>
      </Field>
      <hr className="settings-rule" />
      <Field label="Paper auto-send">
        <Toggle checked={t.auto_send.enabled} label={t.auto_send.enabled ? "On — sends paper orders on schedule" : "Off"}
                onChange={(enabled) => setT({ ...t, auto_send: { ...t.auto_send, enabled } })} />
      </Field>
      <Field label="Send times (New York)" hint="Each must be inside a session. At each time the newest ticket file dated before today (at most 5 days old) is sent with paper_orders.sender --send. A later run only places exits for entries that filled since; nothing is sent twice.">
        <TimeList value={t.auto_send.times} onChange={(times) => setT({ ...t, auto_send: { ...t.auto_send, times } })} label="send time" />
      </Field>
      <Field label="Next send">{s.auto_send_enabled ? nyTime(s.next_send) : <span className="muted">Off</span>}</Field>
      <SaveRow save={save} onSave={() => (turningOn ? setConfirm(true) : doSave())} />
      <EventLog events={s.events.filter((e) => e.kind === "send")} />
      <Dialog open={confirm} onClose={() => setConfirm(false)} tone="danger" title="⚠ Turn on paper auto-send?"
              actions={<button className="btn btn-danger" onClick={() => { setConfirm(false); doSave(); }}>Turn on and save</button>}>
        <p>While BATS is running, it will send <strong>paper</strong> orders to Alpaca on its own at {t.auto_send.times.join(", ") || "your send times"} New York
          time on {t.days.map((d) => DAY_LABEL[d]).join(", ")}, without asking each time.</p>
        <p>Paper keys only (key IDs starting with PK); the sender refuses live keys and the live URL.</p>
      </Dialog>
    </Card>
  );
}

// --- 4. research (LLM) -------------------------------------------------------------------

function LlmCard({ data }: { data: SettingsData }) {
  const initial = Object.fromEntries(Object.entries(data.llm).map(([k, f]) => [k, f.value]));
  const [values, setValues] = useState<Record<string, string>>(initial);
  useEffect(() => setValues(Object.fromEntries(Object.entries(data.llm).map(([k, f]) => [k, f.value]))), [data.llm]);
  const save = useSave();
  const provider = values.TRADINGAGENTS_LLM_PROVIDER;
  const keyName = data.provider_keys[provider];
  const keySet = keyName ? data.keys.find((k) => k.name === keyName)?.set : true;
  const hidden = (name: string) =>
    (name === "TRADINGAGENTS_GOOGLE_THINKING_LEVEL" && provider !== "google")
    || (name === "TRADINGAGENTS_OPENAI_REASONING_EFFORT" && provider !== "openai")
    || (name === "TRADINGAGENTS_ANTHROPIC_EFFORT" && provider !== "anthropic");

  return (
    <Card title="Research (LLM) settings">
      <p className="muted">Written to <code>{data.env_path}</code>; the next research run uses them. Blank removes the setting so TradingAgents' default applies.</p>
      {Object.entries(data.llm).filter(([name]) => !hidden(name)).map(([name, f]) => (
        <Field key={name} label={f.label} hint={<code>{name}</code>}>
          {f.choices ? (
            <select value={values[name] ?? ""} onChange={(e) => setValues({ ...values, [name]: e.target.value })}>
              {!f.choices.includes("") && <option value="">(default)</option>}
              {f.choices.map((c) => <option key={c} value={c}>{c || "(default)"}</option>)}
            </select>
          ) : f.int ? (
            <input type="number" min={f.int[0]} max={f.int[1]} value={values[name] ?? ""}
                   onChange={(e) => setValues({ ...values, [name]: e.target.value })} style={{ width: 90 }} />
          ) : (
            <input value={values[name] ?? ""} onChange={(e) => setValues({ ...values, [name]: e.target.value })}
                   style={{ width: "100%", maxWidth: 360 }} />
          )}
        </Field>
      ))}
      {keyName && !keySet && <div className="notice notice-warn">⚠ {provider} needs <code>{keyName}</code>, which is not set. Add it under API keys.</div>}
      <SaveRow save={save} onSave={() => save.run(() => api.saveLlm(values))} />
    </Card>
  );
}

// --- 5. API keys -------------------------------------------------------------------------

function KeysCard({ data }: { data: SettingsData }) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [clear, setClear] = useState<string[]>([]);
  const [showAll, setShowAll] = useState(false);
  const save = useSave();
  const provider = data.llm.TRADINGAGENTS_LLM_PROVIDER?.value;
  const important = new Set(["ALPACA_API_KEY", "ALPACA_SECRET_KEY", data.provider_keys[provider ?? ""] ?? ""]);
  const rows = data.keys.filter((k) => showAll || important.has(k.name) || k.set);
  const changed = Object.values(drafts).some((v) => v.trim()) || clear.length > 0;

  return (
    <Card title="API keys" actions={<button className="btn btn-sm" onClick={() => setShowAll(!showAll)}>{showAll ? "Show fewer" : `Show all ${data.keys.length}`}</button>}>
      <p className="muted">
        Write-only: saved values are never shown or sent back to this page. Leave a box blank to keep the current key.
        The Alpaca key ID must be a paper key (starts with PK).
      </p>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Key</th><th>Status</th><th>New value</th><th /></tr></thead>
          <tbody>
            {rows.map((k) => (
              <tr key={k.name}>
                <td><div>{k.label}</div><code className="muted">{k.name}</code></td>
                <td>{clear.includes(k.name) ? <span className="status-failed">✕ will be removed</span>
                  : k.set ? <span className="status-ok">✓ Set</span> : <span className="muted">○ Not set</span>}</td>
                <td>
                  <input type="password" autoComplete="off" spellCheck={false} placeholder={k.set ? "•••••••• (unchanged)" : "paste key"}
                         value={drafts[k.name] ?? ""} onChange={(e) => setDrafts({ ...drafts, [k.name]: e.target.value })}
                         style={{ width: 260 }} aria-label={`New value for ${k.name}`} />
                </td>
                <td>
                  {k.set && (
                    <button className="btn btn-sm" onClick={() => setClear(clear.includes(k.name) ? clear.filter((x) => x !== k.name) : [...clear, k.name])}>
                      {clear.includes(k.name) ? "Keep" : "Remove"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <SaveRow save={save} label="Save keys" note={changed ? undefined : "No changes"}
               onSave={() => save.run(async () => {
                 const set = Object.fromEntries(Object.entries(drafts).filter(([, v]) => v.trim()));
                 await api.saveKeys({ set, clear });
                 setDrafts({});
                 setClear([]);
               })} />
    </Card>
  );
}
