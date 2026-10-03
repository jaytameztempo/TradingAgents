const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 });
const qtyFmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 });

export const DASH = "—";

export function money(v: number | null | undefined): string {
  return v == null ? DASH : usd.format(v);
}

/** Signed money: "+$12.34" / "−$12.34". */
export function signedMoney(v: number | null | undefined): string {
  if (v == null) return DASH;
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return sign + usd.format(Math.abs(v));
}

/** Value already in percent units: 1.5 -> "+1.50%". */
export function signedPct(v: number | null | undefined): string {
  if (v == null) return DASH;
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}${Math.abs(v).toFixed(2)}%`;
}

export function qty(v: number | null | undefined): string {
  return v == null ? DASH : qtyFmt.format(v);
}

export function num(v: unknown, digits = 2): string {
  return typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(digits)) : v == null ? DASH : String(v);
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/** "up" / "down" / "flat" — paired with a sign and arrow, never colour alone. */
export function direction(v: number | null | undefined): "up" | "down" | "flat" {
  if (v == null || v === 0) return "flat";
  return v > 0 ? "up" : "down";
}
