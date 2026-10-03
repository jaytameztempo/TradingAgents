import { useEffect, useRef } from "react";

// TradingView's free embeddable Advanced Chart. The script and market data come
// from TradingView; the only thing sent to them is the symbol being charted.
const WIDGET_SRC = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";

// Width:height of the chart, close to Alpaca's own chart panel.
const ASPECT = 1.9;
const MIN_HEIGHT = 360;

function prefersDark(): boolean {
  const forced = document.documentElement.dataset.theme;
  if (forced) return forced === "dark";
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}

export function TradingViewChart({ symbol, interval = "D" }: { symbol: string; interval?: string }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const host = ref.current;
    if (!host) return;
    let built = false;

    // The chart sizes itself once, from its box. Inside a dialog that box is 0px
    // wide until the dialog opens, so wait for a real width before building it.
    const build = (width: number) => {
      built = true;
      const height = Math.round(Math.max(MIN_HEIGHT, Math.min(width / ASPECT, window.innerHeight * 0.75)));
      host.style.height = `${height}px`;
      host.innerHTML = '<div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div>';
      const script = document.createElement("script");
      script.src = WIDGET_SRC;
      script.type = "text/javascript";
      script.async = true;
      script.innerHTML = JSON.stringify({
        // Pixel sizes: with "100%" or autosize, a script added after page load
        // leaves the chart iframe at its 150px default height.
        width: Math.floor(width),
        height,
        symbol,
        interval,
        timezone: "America/New_York",
        theme: prefersDark() ? "dark" : "light",
        style: "1",
        locale: "en",
        withdateranges: true,
        allow_symbol_change: true,
        details: false,
        calendar: false,
        support_host: "https://www.tradingview.com",
      });
      host.appendChild(script);
    };

    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? 0;
      if (!built && width > 0) build(width);
    });
    observer.observe(host);
    return () => {
      observer.disconnect();
      host.innerHTML = "";
    };
  }, [symbol, interval]);

  return <div ref={ref} className="tradingview-widget-container tv-chart" />;
}
