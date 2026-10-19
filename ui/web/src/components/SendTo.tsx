import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, QueueName, ScanSource } from "../api";
import { Dialog } from "./ui";

const TARGETS: { value: QueueName | "trash"; label: string }[] = [
  { value: "research", label: "Research" },
  { value: "strategy", label: "Strategy" },
  { value: "trade", label: "Trade" },
  { value: "trash", label: "Trash" },
];

/**
 * "Send to:" menu for a whole scan file (no ticker) or one ticker in it.
 * Research / Strategy / Trade add to that page's queue; Trash permanently deletes after a confirm.
 */
export function SendTo({ kind, file, ticker, count, onTrashed }: {
  kind: ScanSource;
  file: string;
  ticker?: string;
  count?: number; // members in the scan, for the whole-basket wording
  onTrashed?: () => void;
}) {
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const [note, setNote] = useState<{ text: string; error?: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const what = ticker ?? `${count ?? "all"} ticker${count === 1 ? "" : "s"}`;

  const flash = (text: string, error = false) => {
    setNote({ text, error });
    window.setTimeout(() => setNote(null), 4000);
  };

  const send = async (to: QueueName) => {
    setBusy(true);
    try {
      const r = await api.sendTo(to, { kind, file, tickers: ticker ? [ticker] : [] });
      qc.invalidateQueries({ queryKey: ["queues"] });
      const label = TARGETS.find((t) => t.value === to)!.label;
      flash(r.added.length ? `✓ ${r.added.length} → ${label}` : `Already in ${label}`);
    } catch (e) {
      flash(e instanceof Error ? e.message : String(e), true);
    } finally {
      setBusy(false);
    }
  };

  const trash = async () => {
    setConfirm(false);
    setBusy(true);
    try {
      await api.trashScan({ kind, file, ticker });
      qc.invalidateQueries({ queryKey: ["baskets"] });
      qc.invalidateQueries({ queryKey: ["channelScans"] });
      qc.invalidateQueries({ queryKey: ["summary"] });
      onTrashed?.();
    } catch (e) {
      flash(e instanceof Error ? e.message : String(e), true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className="send-to" onClick={(e) => e.stopPropagation()}>
      <select
        className="send-to-select"
        aria-label={`Send ${ticker ?? file} to`}
        value=""
        disabled={busy}
        onChange={(e) => {
          const v = e.target.value as QueueName | "trash";
          if (v === "trash") setConfirm(true);
          else if (v) send(v);
        }}
      >
        <option value="" disabled>Send to:</option>
        {TARGETS.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
      </select>
      {note && <span className={`send-to-note ${note.error ? "status-failed" : "status-ok"}`}>{note.text}</span>}
      <Dialog open={confirm} onClose={() => setConfirm(false)} tone="danger"
              title={ticker ? `Remove ${ticker} from this scan?` : "Delete this scan file?"}
              actions={<button className="btn btn-danger" onClick={trash}>{ticker ? "Remove permanently" : "Delete permanently"}</button>}>
        {ticker ? (
          <p><strong>{ticker}</strong> is removed from <code>{file}</code> itself, so the router, handoffs and ticket builder no longer see it. <strong>This cannot be undone.</strong></p>
        ) : (
          <p><code>{file}</code> ({what}) is deleted from disk. If it was the newest scan for its date, the router uses the next newest one, or none. <strong>This cannot be undone.</strong></p>
        )}
      </Dialog>
    </span>
  );
}
