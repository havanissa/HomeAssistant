// Display of lengths and areas. Stored values are always metres; the
// display follows Home Assistant's unit system (metric or imperial).
export type Units = "metric" | "imperial";

export function formatLength(m: number, units: Units = "metric"): string {
  if (units === "imperial") {
    const inches = Math.round(m / 0.0254);
    const feet = Math.floor(inches / 12);
    return `${feet}′ ${inches % 12}″`;
  }
  return m < 10 ? `${m.toFixed(2)} m` : `${m.toFixed(1)} m`;
}

export function formatArea(m2: number, units: Units = "metric"): string {
  if (units === "imperial") return `${Math.round(m2 * 10.7639)} sq ft`;
  return `${m2 < 10 ? m2.toFixed(1) : Math.round(m2)} m²`;
}

/** Parse a length typed by the user in the display unit ("3.2", "3,2 m", "10'6"") into metres. */
export function parseLength(text: string, units: Units = "metric"): number | null {
  const t = text.trim().replace(",", ".");
  if (units === "imperial") {
    const m = /^(\d+(?:\.\d+)?)\s*(?:′|'|ft)?\s*(?:(\d+(?:\.\d+)?)\s*(?:″|"|in)?)?$/.exec(t);
    if (!m) return null;
    return (Number(m[1]) * 12 + Number(m[2] ?? 0)) * 0.0254;
  }
  const m = /^(\d+(?:\.\d+)?)\s*(m|cm|mm)?$/.exec(t);
  if (!m) return null;
  const value = Number(m[1]);
  return m[2] === "cm" ? value / 100 : m[2] === "mm" ? value / 1000 : value;
}
