import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ManualTicket, QueueItem } from "../api";
import { dateTime, money } from "../format";
import { useJob } from "../useJob";
import { Ticker } from "./Ticker";
import { Card, Column, DataTable, Dialog, ErrorBox, JobLog } from "./ui";

// Order entry for the "To trade" queue. Saving writes a manual paper ticket and sends nothing; a confirmed
// Send starts paper_orders.sender on that one ticket: a limit DAY entry, then an OCO exit from the fill.

interface Draft { entry: string; stop: string; target: string; quantity: string }

const cents = (v: number | null | undefined) => (v == null ? "" : v.toFixed(2));
const parse = (s: string) => (s.trim() === "" ? null : Number(s));

function prefill(item: QueueItem): Draft {
  const m = item.metrics ?? {};
  const support = m.support ?? null, resistance = m.resistance ?? null;
  if (item.kind === "long" || item.kind === "short") {
    // Channel names: enter at the channel line, target the midline (as the daily tickets do).
    const entry = item.side === "long" ? support : resistance;
    const mid = support != null && resistance != null ? (support + resistance) / 2 : null;
    return { entry: cents(entry), stop: "", target: cents(mid), quantity: "" };
  }
  return { entry: cents(m.close), stop: "", target: "", quantity: "" };
}

/** The same rules the server and the sender apply; null when the ticket can be saved. */
function problem(side: "long" | "short", d: Draft): string | null {
  const [entry, stop, target, qty] = [parse(d.entry), parse(d.stop), parse(d.target), parse(d.quantity)];
  if ([entry, stop, target].some((v) => v == null || !Number.isFinite(v) || v <= 0)) return "Enter entry, stop and target prices.";
  if (qty == null || !Number.isInteger(qty) || qty < 1) return "Quantity must be a whole number of at least 1.";
  const sign = side === "long" ? 1 : -1;
  if (!(sign * (entry! - stop!) > 0)) return `A ${side} stop must be ${side === "long" ? "below" : "above"} the entry.`;
  if (!(sign * (target! - entry!) > 0)) return `A ${side} target must be ${side === "long" ? "above" : "below"} the entry.`;
  return null;
}

export function OrderEntryDialog({ item, onClose }: { item: QueueItem | null; onClose: () => void }) {
  const qc = useQueryClient();
  const account = useQuery({ queryKey: ["account"], queryFn: api.account, enabled: item != null });
  const [draft, setDraft] = useState<Draft>({ entry: "", stop: "", target: "", quantity: "" });
  const [error, setError] = useState<unknown>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (item) setDraft(prefill(item));
    setError(null);
  }, [item?.id]);

  const why = item ? problem(item.side, draft) : null;
  const entry = parse(draft.entry), stop = parse(draft.stop), target = parse(draft.target), qty = parse(draft.quantity);
  const risk = entry != null && stop != null ? Math.abs(entry - stop) : null;
  const reward = entry != null && target != null ? Math.abs(target - entry) : null;
  const notional = entry != null && qty != null ? entry * qty : null;
  const buyingPower = account.data?.buying_power ?? null;

  const save = async () => {
    if (!item || why) return;
    setSaving(true);
    setError(null);
    try {
      await api.createManualTicket({ item_id: item.id, entry: entry!, stop: stop!, target: target!, quantity: qty! });
      qc.invalidateQueries({ queryKey: ["manualTickets"] });
      onClose();
    } catch (e) {
      setError(e);
    } finally {
      setSaving(false);
    }
  };

  const field = (key: keyof Draft, label: string, step: string) => (
    <label className="field">{label}
      <input type="number" min="0" step={step} value={draft[key]} style={{ width: 110 }}
             onChange={(e) => setDraft({ ...draft, [key]: e.target.value })} />
    </label>
  );

  return (
    <Dialog open={item != null} onClose={onClose} title="New paper ticket"
            actions={<button className="btn btn-primary" disabled={!!why || saving} onClick={save}>Save ticket</button>}>
      {item && (
        <>
          <p>
            <strong><Ticker symbol={item.ticker} /></strong> · {item.side === "long" ? "Long (buy)" : "Short (sell)"} ·
            from {item.scan} {item.as_of}
          </p>
          <div className="form">
            {field("entry", "Limit entry", "0.01")}
            {field("stop", "Stop", "0.01")}
            {field("target", "Target", "0.01")}
            {field("quantity", "Quantity", "1")}
          </div>
          <dl className="kv" style={{ marginTop: 12 }}>
            <dt>Risk / share</dt><dd>{money(risk)}</dd>
            <dt>Reward : risk</dt><dd>{risk && reward != null ? (reward / risk).toFixed(2) : "—"}</dd>
            <dt>Risk at stop</dt><dd>{risk != null && qty != null ? money(risk * qty) : "—"}</dd>
            <dt>Notional</dt>
            <dd>{money(notional)}{notional != null && buyingPower ? <span className="muted"> · {(notional / buyingPower * 100).toFixed(1)}% of buying power</span> : null}</dd>
          </dl>
          {why && <p className="muted">{why}</p>}
          <ErrorBox error={error} />
          <p className="muted" style={{ marginBottom: 0 }}>
            Saving writes a ticket only; nothing is sent. When sent, the entry is a limit DAY order. Once it fills,
            an OCO exit takes profit at the target and stops the same distance from the actual fill.
          </p>
        </>
      )}
    </Dialog>
  );
}

export function ManualTickets() {
  const qc = useQueryClient();
  const tickets = useQuery({ queryKey: ["manualTickets"], queryFn: api.manualTickets, refetchInterval: 30_000 });
  const runner = useJob();
  const [confirm, setConfirm] = useState<ManualTicket | null>(null);
  const [error, setError] = useState<unknown>(null);

  const remove = async (t: ManualTicket) => {
    setError(null);
    try {
      await api.deleteManualTicket(t.name);
      qc.invalidateQueries({ queryKey: ["manualTickets"] });
    } catch (e) {
      setError(e);
    }
  };

  const cols: Column<ManualTicket>[] = [
    { key: "symbol", label: "Ticker", render: (t) => <Ticker symbol={t.symbol} />, sortValue: (t) => t.symbol },
    { key: "side", label: "Side", render: (t) => (t.side === "long" ? "Long" : "Short"), sortValue: (t) => t.side },
    { key: "qty", label: "Qty", align: "right", render: (t) => t.quantity, sortValue: (t) => t.quantity },
    { key: "entry", label: "Entry", align: "right", render: (t) => t.entry?.toFixed(2) },
    { key: "stop", label: "Stop", align: "right", render: (t) => t.stop?.toFixed(2) },
    { key: "target", label: "Target", align: "right", render: (t) => t.target?.toFixed(2) },
    { key: "rr", label: "R:R", align: "right", render: (t) => t.reward_risk?.toFixed(2), sortValue: (t) => t.reward_risk },
    { key: "risk", label: "Risk", align: "right", render: (t) => money(t.risk_dollars), sortValue: (t) => t.risk_dollars },
    { key: "notional", label: "Notional", align: "right", render: (t) => money(t.notional), sortValue: (t) => t.notional },
    { key: "created", label: "Created", render: (t) => dateTime(t.created_at), sortValue: (t) => t.created_at },
    {
      key: "status", label: "Status", sortValue: (t) => t.sends.length,
      render: (t) => t.error ? <span className="status-failed" title={t.error}>Unreadable</span>
        : t.sends.length ? <span className="status-ok" title={t.sends.map((s) => dateTime(s.at)).join(", ")}>Sent ×{t.sends.length}</span>
        : <span className="muted">Not sent</span>,
    },
    {
      key: "actions", label: "", align: "right", render: (t) => (
        <span className="row" style={{ justifyContent: "flex-end" }}>
          <button className="btn btn-sm" disabled={!!t.error || runner.running}
                  onClick={() => runner.start(() => api.sendManualTicket(t.name, { send: false }))}>Preview</button>
          <button className="btn btn-sm btn-primary" disabled={!!t.error || runner.running} onClick={() => setConfirm(t)}
                  title={t.sends.length ? "Run the sender again: places the OCO exit once the entry has filled" : "Send the entry to Alpaca paper"}>
            {t.sends.length ? "Place exits…" : "Send…"}
          </button>
          <button className="btn btn-sm" disabled={t.sends.length > 0} onClick={() => remove(t)}
                  title={t.sends.length ? "A sent ticket stays as the record of its orders" : "Delete this ticket"}>Delete</button>
        </span>
      ),
    },
  ];

  return (
    <>
      <Card title={`Paper tickets (${tickets.data?.length ?? 0})`}>
        <p className="muted" style={{ marginTop: 0 }}>
          Written with Order… above. Preview prints the orders and sends nothing. Send runs paper_orders.sender on
          one ticket: it refuses live keys and sends only inside your trading window (Settings), and never sends the same order twice.
        </p>
        <ErrorBox error={tickets.error ?? error} />
        <DataTable columns={cols} rows={tickets.data ?? []} rowKey={(t) => t.name}
                   empty="No tickets yet. Click Order… on a row of the trade queue." initialSort={{ key: "created", dir: "desc" }} />
      </Card>
      {(runner.job || runner.error != null) && (
        <Card title="Paper sender"><ErrorBox error={runner.error} /><JobLog job={runner.job} onDone={runner.onDone} /></Card>
      )}
      <Dialog open={confirm != null} onClose={() => setConfirm(null)} tone="danger"
              title={confirm?.sends.length ? "Run the paper sender again?" : "⚠ Send this paper order?"}
              actions={<button className="btn btn-primary" onClick={() => {
                const t = confirm!;
                setConfirm(null);
                runner.start(() => api.sendManualTicket(t.name, { send: true, confirm: true }));
              }}>{confirm?.sends.length ? "Run sender" : "Send to paper"}</button>}>
        {confirm && (
          <>
            <p>
              <strong>{confirm.order_side === "buy" ? "Buy" : "Sell short"} {confirm.quantity} {confirm.symbol}</strong> at a
              limit of <strong>{confirm.entry.toFixed(2)}</strong> (DAY). Once it fills: take profit at {confirm.target.toFixed(2)},
              stop {confirm.stop_offset.toFixed(2)} from the fill ({confirm.stop.toFixed(2)} at the entry price).
            </p>
            {confirm.sends.length > 0 && <p>This ticket was already sent. The entry is not sent again; this places the exit if the entry has filled.</p>}
            <p>Paper account only (key IDs starting with PK). Refused outside your trading window.</p>
          </>
        )}
      </Dialog>
    </>
  );
}
