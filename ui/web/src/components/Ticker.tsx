import { createContext, KeyboardEvent, MouseEvent, ReactNode, useCallback, useContext, useState } from "react";
import { TradingViewChart } from "./TradingViewChart";
import { Dialog } from "./ui";

const OpenChart = createContext<(symbol: string) => void>(() => {});

/** One TradingView chart dialog for the whole app; any <Ticker> opens it. */
export function ChartProvider({ children }: { children: ReactNode }) {
  const [symbol, setSymbol] = useState<string | null>(null);
  const open = useCallback((s: string) => setSymbol(s.trim().toUpperCase()), []);
  return (
    <OpenChart.Provider value={open}>
      {children}
      <Dialog open={symbol != null} onClose={() => setSymbol(null)} wide closeLabel="Close"
              title={<>{symbol} <span className="muted">· chart by TradingView</span></>}>
        {symbol && <TradingViewChart key={symbol} symbol={symbol} />}
      </Dialog>
    </OpenChart.Provider>
  );
}

/**
 * A ticker symbol that opens its TradingView chart.
 *
 * Rendered as a span with role="button" rather than a <button>, so it can sit
 * inside the clickable list rows on Research and Trade; it stops the click so
 * the row underneath is not selected as well.
 */
export function Ticker({ symbol, chip = false, children }: { symbol: string; chip?: boolean; children?: ReactNode }) {
  // A combined label such as "SPY+QQQ" is two tickers: link each one on its own.
  const parts = children == null ? symbol.split("+").map((s) => s.trim()).filter(Boolean) : [symbol];
  if (parts.length > 1) {
    return <>{parts.map((s, i) => <span key={s}>{i > 0 && "+"}<Ticker symbol={s} chip={chip} /></span>)}</>;
  }
  return <SingleTicker symbol={symbol} chip={chip}>{children}</SingleTicker>;
}

function SingleTicker({ symbol, chip, children }: { symbol: string; chip: boolean; children?: ReactNode }) {
  const open = useContext(OpenChart);
  const activate = (e: MouseEvent | KeyboardEvent) => {
    e.stopPropagation();
    e.preventDefault();
    open(symbol);
  };
  return (
    <span
      role="button"
      tabIndex={0}
      className={`symbol-link ${chip ? "chip" : ""}`}
      title={`Open ${symbol} chart`}
      onClick={activate}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && activate(e)}
    >
      {children ?? symbol}
    </span>
  );
}

/** A row of ticker chips, or "none". */
export function TickerChips({ symbols }: { symbols?: string[] | null }) {
  if (!symbols?.length) return <span className="muted">none</span>;
  return <>{symbols.map((t) => <Ticker key={t} symbol={t} chip />)}</>;
}
