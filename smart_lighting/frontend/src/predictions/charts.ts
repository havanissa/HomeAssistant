// Phase A5: the Predictions charts, as inline SVG (no library). Every chart scales with its container
// (viewBox + width 100 %), takes its colours from the theme tokens through classes in predictions.css, labels
// its axes, carries a legend when it has more than one series and a <title> on every mark for hovering.
// Missing data is drawn as "not observed", never as zero.
import { html, nothing, svg, type SVGTemplateResult, type TemplateResult } from "lit";

import type { AccuracyStats, Patterns, RoomForecast } from "./api";

export const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const HOUR_LABELS = [0, 3, 6, 9, 12, 15, 18, 21];

function hourRange(h: number): string {
  return `${String(h).padStart(2, "0")}:00–${String((h + 1) % 24).padStart(2, "0")}:00`;
}

function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? "not observed" : `${Math.round(v * 100)} %`;
}

type Series = { key: string; label: string; cls: string; values: Array<number | null> | null | undefined };

function path(points: Array<[number, number] | null>): string {
  let d = "";
  let pen = false;
  for (const p of points) {
    if (!p) { pen = false; continue; }
    d += `${pen ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`;
    pen = true;
  }
  return d;
}

/** A legend below a chart: swatches drawn with the same classes as the marks. */
export function legend(items: Array<{ cls: string; label: string; kind?: "line" | "bar" | "cell" | "dot" }>): TemplateResult {
  return html`<ul class="pr-legend" aria-label="Legend">${items.map((i) => html`<li>
    <svg class="pr-swatch" width="28" height="12" viewBox="0 0 28 12" aria-hidden="true">
      ${i.kind === "bar" || i.kind === "cell" ? svg`<rect class=${i.cls} x="2" y="1" width="24" height="10" rx="2"></rect>`
        : i.kind === "dot" ? svg`<circle class=${i.cls} cx="14" cy="6" r="5"></circle>`
        : svg`<path class=${i.cls} d="M1,6 L27,6"></path>`}
    </svg><span>${i.label}</span></li>`)}</ul>`;
}

/**
 * Hourly occupancy: the observed share of each hour (bars, real data), the learned curve the model uses and
 * its workday / day-off variants (lines). Values are fractions 0..1; null = not observed.
 */
export function hourlyChart(p: Patterns): TemplateResult {
  const W = 640, H = 230, L = 40, R = 12, T = 14, B = 40;
  const pw = W - L - R, ph = H - T - B;
  const x = (h: number) => L + (h / 24) * pw;
  const y = (v: number) => T + (1 - Math.min(1, Math.max(0, v))) * ph;
  const slot = pw / 24;
  const series: Series[] = [
    { key: "learned", label: "Learned (used for forecasts)", cls: "pr-line-learned", values: p.hourly_learned },
    { key: "workday", label: "Workdays", cls: "pr-line-workday", values: p.hourly_workday },
    { key: "day_off", label: "Days off", cls: "pr-line-dayoff", values: p.hourly_day_off },
  ].filter((s) => s.values?.some((v) => v !== null));
  const hours = Array.from({ length: 24 }, (_, h) => h);
  const tip = (h: number) => {
    const parts = [`${hourRange(h)}: observed ${pct(p.hourly_observed[h])}`];
    for (const s of series) parts.push(`${s.label.split(" (")[0]} ${pct(s.values?.[h])}`);
    return parts.join(" · ");
  };
  return html`<figure class="pr-chart">
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Occupancy by hour of the day" data-testid="predictions-hourly-chart">
      ${[0, 0.5, 1].map((v) => svg`<line class="pr-grid" x1=${L} x2=${W - R} y1=${y(v)} y2=${y(v)}></line>
        <text class="pr-axis" x=${L - 6} y=${y(v) + 4} text-anchor="end">${Math.round(v * 100)} %</text>`)}
      ${hours.map((h) => p.hourly_observed[h] === null
        ? svg`<rect class="pr-missing" x=${x(h) + 2} y=${T} width=${slot - 4} height=${ph}><title>${hourRange(h)}: not observed</title></rect>`
        : svg`<rect class="pr-bar-observed" x=${x(h) + 2} y=${y(p.hourly_observed[h] ?? 0)} width=${slot - 4} height=${Math.max(0, ph - (y(p.hourly_observed[h] ?? 0) - T))} rx="2"></rect>`)}
      ${series.map((s) => svg`<path class="pr-line ${s.cls}" d=${path(hours.map((h) => (s.values?.[h] === null || s.values?.[h] === undefined ? null : [x(h) + slot / 2, y(s.values[h] as number)])))}></path>`)}
      ${HOUR_LABELS.map((h) => svg`<text class="pr-axis" x=${x(h) + slot / 2} y=${H - B + 16} text-anchor="middle">${String(h).padStart(2, "0")}:00</text>`)}
      <text class="pr-axis pr-axis-title" x=${L + pw / 2} y=${H - 6} text-anchor="middle">Hour of the day</text>
      ${hours.map((h) => svg`<rect class="pr-hit" x=${x(h)} y=${T} width=${slot} height=${ph}><title>${tip(h)}</title></rect>`)}
    </svg>
    ${legend([{ cls: "pr-bar-observed", label: `Observed share of the hour (${p.days} day${p.days === 1 ? "" : "s"})`, kind: "bar" },
      ...series.map((s) => ({ cls: s.cls, label: s.label })),
      { cls: "pr-missing", label: "Not observed", kind: "bar" as const }])}
  </figure>`;
}

/** Simple bars per hour (arrivals per day). */
export function hourlyBars(values: Array<number | null>, label: string, unit: string): TemplateResult {
  const W = 640, H = 170, L = 40, R = 12, T = 14, B = 40;
  const pw = W - L - R, ph = H - T - B;
  const max = Math.max(0.1, ...values.map((v) => v ?? 0));
  const nice = max <= 0.5 ? 0.5 : max <= 1 ? 1 : Math.ceil(max);
  const x = (h: number) => L + (h / 24) * pw;
  const y = (v: number) => T + (1 - v / nice) * ph;
  const slot = pw / 24;
  return html`<figure class="pr-chart">
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label=${label} data-testid="predictions-arrivals-chart">
      ${[0, 0.5, 1].map((f) => svg`<line class="pr-grid" x1=${L} x2=${W - R} y1=${y(f * nice)} y2=${y(f * nice)}></line>
        <text class="pr-axis" x=${L - 6} y=${y(f * nice) + 4} text-anchor="end">${(f * nice).toFixed(nice < 1 ? 2 : nice < 5 ? 1 : 0)}</text>`)}
      ${values.map((v, h) => v === null
        ? svg`<rect class="pr-missing" x=${x(h) + 2} y=${T} width=${slot - 4} height=${ph}><title>${hourRange(h)}: not observed</title></rect>`
        : svg`<rect class="pr-bar-primary" x=${x(h) + 2} y=${y(v)} width=${slot - 4} height=${Math.max(0, ph - (y(v) - T))} rx="2"><title>${hourRange(h)}: ${v.toFixed(2)} ${unit}</title></rect>`)}
      ${HOUR_LABELS.map((h) => svg`<text class="pr-axis" x=${x(h) + slot / 2} y=${H - B + 16} text-anchor="middle">${String(h).padStart(2, "0")}:00</text>`)}
      <text class="pr-axis pr-axis-title" x=${L + pw / 2} y=${H - 6} text-anchor="middle">Hour of the day · ${unit}</text>
    </svg>
  </figure>`;
}

/** The week as a 7 × 24 heatmap of the observed occupancy share; grey where the hour was not observed. */
export function weeklyHeatmap(p: Patterns): TemplateResult {
  const W = 640, L = 40, R = 12, T = 22, cell = 22, gap = 2;
  const pw = W - L - R;
  const cw = pw / 24;
  const H = T + 7 * (cell + gap) + 30;
  const tone = (v: number) => 0.12 + 0.88 * Math.min(1, Math.max(0, v));
  return html`<figure class="pr-chart">
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Occupancy by weekday and hour" data-testid="predictions-weekly-heatmap">
      ${HOUR_LABELS.map((h) => svg`<text class="pr-axis" x=${L + h * cw + cw / 2} y="12" text-anchor="middle">${String(h).padStart(2, "0")}</text>`)}
      ${p.weekly.map((row, d) => svg`<text class="pr-axis" x=${L - 6} y=${T + d * (cell + gap) + cell / 2 + 4} text-anchor="end">${DOW[d]}</text>
        ${row.map((v, h) => {
          const obs = p.weekly_observed_hours[d]?.[h] ?? 0;
          const title = `${DOW[d]} ${hourRange(h)}: ${v === null ? `not observed (${obs} h of data)` : `${Math.round(v * 100)} % occupied (${obs} h observed)`}`;
          return v === null
            ? svg`<rect class="pr-cell-missing" x=${L + h * cw + gap / 2} y=${T + d * (cell + gap)} width=${cw - gap} height=${cell} rx="3"><title>${title}</title></rect>`
            : svg`<rect class="pr-cell" style="fill-opacity:${tone(v).toFixed(2)}" x=${L + h * cw + gap / 2} y=${T + d * (cell + gap)} width=${cw - gap} height=${cell} rx="3"><title>${title}</title></rect>`;
        })}`)}
      <text class="pr-axis pr-axis-title" x=${L + pw / 2} y=${H - 8} text-anchor="middle">Hour of the day (start)</text>
    </svg>
    <ul class="pr-legend" aria-label="Legend">
      <li><svg class="pr-swatch pr-ramp" width="96" height="12" viewBox="0 0 96 12" aria-hidden="true">${[0, 0.25, 0.5, 0.75, 1].map((v, i) => svg`<rect class="pr-cell" style="fill-opacity:${tone(v).toFixed(2)}" x=${i * 19} y="1" width="18" height="10" rx="2"></rect>`)}</svg><span>0 % → 100 % of the hour occupied</span></li>
      <li><svg class="pr-swatch" width="28" height="12" viewBox="0 0 28 12" aria-hidden="true"><rect class="pr-cell-missing" x="2" y="1" width="24" height="10" rx="2"></rect></svg><span>Not observed (less than 30 min of data)</span></li>
    </ul>
  </figure>`;
}

/**
 * The forecast at each horizon as horizontal bars (0..100 %): the probability of occupancy, the schedule
 * baseline as a tick, the entry probability (vacant rooms) as an outlined bar, the threshold as a dashed line.
 */
export function horizonBars(f: RoomForecast): TemplateResult {
  const horizons = Object.keys(f.probabilities ?? {}).map(Number).sort((a, b) => a - b);
  const W = 640, L = 84, R = 60, T = 10, row = 46;
  const pw = W - L - R;
  const H = T + horizons.length * row + 26;
  const x = (v: number) => L + (Math.min(100, Math.max(0, v)) / 100) * pw;
  const hasEntry = !!f.entry && Object.keys(f.entry).length > 0;
  return html`<figure class="pr-chart">
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Probability of occupancy by horizon" data-testid="predictions-horizon-chart">
      ${[0, 25, 50, 75, 100].map((v) => svg`<line class="pr-grid" x1=${x(v)} x2=${x(v)} y1=${T} y2=${T + horizons.length * row}></line>
        <text class="pr-axis" x=${x(v)} y=${T + horizons.length * row + 16} text-anchor="middle">${v} %</text>`)}
      <line class="pr-threshold" x1=${x(f.threshold)} x2=${x(f.threshold)} y1=${T - 4} y2=${T + horizons.length * row}></line>
      <text class="pr-axis pr-threshold-label" x=${x(f.threshold)} y=${T + horizons.length * row + 16} text-anchor=${f.threshold > 85 ? "end" : "start"} dx=${f.threshold > 85 ? -4 : 4}> threshold ${f.threshold} %</text>
      ${horizons.map((h, i) => {
        const y0 = T + i * row;
        const p = f.probabilities?.[String(h)] ?? 0;
        const s = f.schedule?.[String(h)];
        const e = f.entry?.[String(h)];
        const bh = hasEntry ? 14 : 20;
        return svg`<text class="pr-axis pr-row-label" x=${L - 8} y=${y0 + (hasEntry ? 22 : 16)} text-anchor="end">${h === f.horizon ? "▸ " : ""}in ${h} min</text>
          <rect class="pr-track" x=${L} y=${y0 + 6} width=${pw} height=${bh} rx="3"></rect>
          <rect class="pr-bar-primary ${h === f.horizon ? "pr-bar-current" : ""}" x=${L} y=${y0 + 6} width=${x(p) - L} height=${bh} rx="3"><title>In ${h} min: ${Math.round(p)} % likely occupied${s !== undefined ? `; schedule alone ${Math.round(s)} %` : ""}</title></rect>
          <text class="pr-value" x=${x(p) + 6} y=${y0 + 6 + bh / 2 + 4}>${Math.round(p)} %</text>
          ${s !== undefined && s !== null ? svg`<line class="pr-schedule-tick" x1=${x(s)} x2=${x(s)} y1=${y0 + 2} y2=${y0 + 10 + bh}><title>Schedule alone: ${Math.round(s)} % (time-of-week average)</title></line>` : nothing}
          ${hasEntry && e !== undefined && e !== null ? svg`<rect class="pr-bar-entry" x=${L} y=${y0 + 24} width=${Math.max(0, x(e) - L)} height="10" rx="3"><title>Someone enters within ${h} min: ${Math.round(e)} %</title></rect>
            <text class="pr-value pr-value-small" x=${x(e) + 6} y=${y0 + 33}>${Math.round(e)} % entry</text>` : nothing}`;
      })}
    </svg>
    ${legend([{ cls: "pr-bar-primary", label: "Probability the room is occupied", kind: "bar" },
      { cls: "pr-schedule-tick", label: "Schedule alone (time-of-week average)" },
      ...(hasEntry ? [{ cls: "pr-bar-entry", label: "Probability someone enters by then", kind: "bar" as const }] : []),
      { cls: "pr-threshold", label: `Threshold for "predicted occupied" (${f.threshold} %)` }])}
  </figure>`;
}

/** Calibration: for each forecast bin, the mean forecast against the share that was really occupied. */
export function calibrationPlot(a: AccuracyStats): TemplateResult {
  const S = 300, L = 44, R = 12, T = 12, B = 44;
  const pw = S - L - R, ph = S - T - B;
  const x = (v: number) => L + v * pw;
  const y = (v: number) => T + (1 - v) * ph;
  const bins = a.calibration.filter((b) => b.mean_forecast !== null && b.observed !== null);
  const maxN = Math.max(1, ...bins.map((b) => b.forecasts));
  const r = (n: number) => 4 + 10 * Math.sqrt(n / maxN);
  return html`<figure class="pr-chart pr-chart-square">
    <svg viewBox="0 0 ${S} ${S}" role="img" aria-label="Calibration: forecast against observed" data-testid="predictions-calibration-chart">
      ${[0, 0.25, 0.5, 0.75, 1].map((v) => svg`<line class="pr-grid" x1=${L} x2=${S - R} y1=${y(v)} y2=${y(v)}></line>
        <line class="pr-grid" y1=${T} y2=${S - B} x1=${x(v)} x2=${x(v)}></line>
        <text class="pr-axis" x=${L - 6} y=${y(v) + 4} text-anchor="end">${Math.round(v * 100)}</text>
        <text class="pr-axis" x=${x(v)} y=${S - B + 16} text-anchor="middle">${Math.round(v * 100)}</text>`)}
      <line class="pr-diagonal" x1=${x(0)} y1=${y(0)} x2=${x(1)} y2=${y(1)}><title>Perfect calibration</title></line>
      ${bins.map((b) => svg`<circle class="pr-dot" cx=${x(b.mean_forecast as number)} cy=${y(b.observed as number)} r=${r(b.forecasts).toFixed(1)}>
        <title>Forecasts of ${b.bin}: mean forecast ${Math.round((b.mean_forecast as number) * 100)} %, really occupied ${Math.round((b.observed as number) * 100)} % of ${b.forecasts} times</title></circle>`)}
      <text class="pr-axis pr-axis-title" x=${L + pw / 2} y=${S - 6} text-anchor="middle">Forecast probability (%)</text>
      <text class="pr-axis pr-axis-title" transform="rotate(-90)" x=${-(T + ph / 2)} y="12" text-anchor="middle">Really occupied (%)</text>
    </svg>
    ${bins.length ? legend([{ cls: "pr-dot", label: "One forecast bin (size: how many forecasts)", kind: "dot" }, { cls: "pr-diagonal", label: "Perfect calibration" }])
      : html`<p class="small muted">No evaluated forecasts yet.</p>`}
  </figure>`;
}

/** Daily Brier score of the forecasts against the two baselines (lower is better). */
export function brierLine(daily: Array<{ date: string; forecasts: number; brier: number | null; brier_schedule: number | null; brier_persistence: number | null }>): TemplateResult {
  const W = 640, H = 220, L = 44, R = 12, T = 14, B = 44;
  const pw = W - L - R, ph = H - T - B;
  const n = daily.length;
  const all = daily.flatMap((d) => [d.brier, d.brier_schedule, d.brier_persistence]).filter((v): v is number => v !== null);
  const top = Math.max(0.25, Math.min(1, Math.ceil(Math.max(0, ...all) * 20) / 20));
  const x = (i: number) => (n <= 1 ? L + pw / 2 : L + (i / (n - 1)) * pw);
  const y = (v: number) => T + (1 - Math.min(top, v) / top) * ph;
  const series: Array<{ key: "brier" | "brier_schedule" | "brier_persistence"; label: string; cls: string }> = [
    { key: "brier", label: "Forecasts", cls: "pr-line-learned" },
    { key: "brier_schedule", label: "Schedule baseline", cls: "pr-line-workday" },
    { key: "brier_persistence", label: "Persistence baseline (stays as it is)", cls: "pr-line-dayoff" },
  ];
  const labelEvery = Math.max(1, Math.ceil(n / 6));
  const short = (d: string) => d.slice(5);
  return html`<figure class="pr-chart">
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Daily Brier score" data-testid="predictions-brier-chart">
      ${[0, 0.5, 1].map((f) => svg`<line class="pr-grid" x1=${L} x2=${W - R} y1=${y(f * top)} y2=${y(f * top)}></line>
        <text class="pr-axis" x=${L - 6} y=${y(f * top) + 4} text-anchor="end">${(f * top).toFixed(2)}</text>`)}
      ${series.map((s) => svg`<path class="pr-line ${s.cls}" d=${path(daily.map((d, i) => (d[s.key] === null ? null : [x(i), y(d[s.key] as number)])))}></path>`)}
      ${daily.map((d, i) => svg`${d.brier !== null ? svg`<circle class="pr-marker pr-line-learned" cx=${x(i)} cy=${y(d.brier)} r="3"></circle>` : nothing}
        <rect class="pr-hit" x=${x(i) - (n > 1 ? pw / (n - 1) / 2 : pw / 2)} y=${T} width=${n > 1 ? pw / (n - 1) : pw} height=${ph}>
          <title>${d.date}: forecasts ${d.brier?.toFixed(3) ?? "—"} · schedule ${d.brier_schedule?.toFixed(3) ?? "—"} · persistence ${d.brier_persistence?.toFixed(3) ?? "—"} (${d.forecasts} forecasts)</title></rect>
        ${i % labelEvery === 0 || i === n - 1 ? svg`<text class="pr-axis" x=${x(i)} y=${H - B + 16} text-anchor="middle">${short(d.date)}</text>` : nothing}`)}
      <text class="pr-axis pr-axis-title" x=${L + pw / 2} y=${H - 6} text-anchor="middle">Day · Brier score (0 = perfect, lower is better)</text>
    </svg>
    ${legend(series.map((s) => ({ cls: s.cls, label: s.label })))}
  </figure>`;
}

/** A small inline probability bar (0..100) for tables. */
export function miniBar(p: number | null | undefined, threshold?: number): SVGTemplateResult | typeof nothing {
  if (p === null || p === undefined) return nothing;
  return svg`<svg class="pr-mini" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true"><rect class="pr-track" x="0" y="0" width="100" height="8" rx="2"></rect>
    <rect class="pr-bar-primary" x="0" y="0" width=${Math.min(100, Math.max(0, p))} height="8" rx="2"></rect>
    ${threshold !== undefined ? svg`<line class="pr-threshold" x1=${threshold} x2=${threshold} y1="-1" y2="9"></line>` : nothing}</svg>`;
}
