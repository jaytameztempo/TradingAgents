import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from "react";
import type { ScanKind } from "./api";

/** App preferences, kept in this browser only (localStorage); nothing here reaches the server. */
export interface Prefs {
  theme: "system" | "light" | "dark";
  refreshSeconds: number;
  chartInterval: string; // TradingView interval: 1, 5, 15, 30, 60, 240, D, W
  chartStyle: string; // TradingView style: 0 bars, 1 candles, 9 hollow candles, 8 Heikin Ashi, 2 line, 3 area
  defaultScan: ScanKind;
  defaultTickers: string;
  regimeSymbol: string;
}

export const DEFAULT_PREFS: Prefs = {
  theme: "system",
  refreshSeconds: 15,
  chartInterval: "D",
  chartStyle: "1",
  defaultScan: "all",
  defaultTickers: "",
  regimeSymbol: "SPY",
};

const STORAGE_KEY = "bats.prefs.v1";

function load(): Prefs {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? { ...DEFAULT_PREFS, ...JSON.parse(raw) } : DEFAULT_PREFS;
  } catch {
    return DEFAULT_PREFS;
  }
}

function applyTheme(theme: Prefs["theme"]) {
  const root = document.documentElement;
  if (theme === "system") delete root.dataset.theme;
  else root.dataset.theme = theme;
}

const Ctx = createContext<{ prefs: Prefs; setPrefs: (p: Partial<Prefs>) => void; reset: () => void }>({
  prefs: DEFAULT_PREFS,
  setPrefs: () => {},
  reset: () => {},
});

export function PrefsProvider({ children }: { children: ReactNode }) {
  const [prefs, setState] = useState<Prefs>(load);
  useEffect(() => applyTheme(prefs.theme), [prefs.theme]);
  const save = useCallback((next: Prefs) => {
    setState(next);
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    } catch {
      /* private window or blocked storage: the change still applies until reload */
    }
  }, []);
  const setPrefs = useCallback((p: Partial<Prefs>) => save({ ...prefs, ...p }), [prefs, save]);
  const reset = useCallback(() => save(DEFAULT_PREFS), [save]);
  return <Ctx.Provider value={{ prefs, setPrefs, reset }}>{children}</Ctx.Provider>;
}

export function usePrefs() {
  return useContext(Ctx);
}
