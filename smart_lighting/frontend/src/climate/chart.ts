// Phase A7.0: a room's temperature history as inline SVG (no library). Lines: the room's own sensors, each
// thermostat's own reading and its setpoint (a step line), the outdoor temperature. Lanes below: when a device
// *reported* heating, when a window or door was open, when the room was occupied. An unusable reading is a gap,
// never zero; a device that does not report its activity gets a lane that says so.
import { html, nothing, svg, type TemplateResult } from "lit";

import { toDisplay, type Band, type History, type Series, type Unit, type ValidationChart } from "./api";

const CLS: Record<Series["kind"], string> = {
  room: "cl-line-room", secondary: "cl-line-secondary", thermostat: "cl-line-thermostat", setpoint: "cl-line-setpoint", outdoor: "cl-line-outdoor",
  model: "cl-line-model", persistence: "cl-line-persistence",
};
const BAND_CLS: Record<Band["kind"], string> = { heating: "cl-band-heating", open: "cl-band-open", occupied: "cl-band-occupied", excluded: "cl-band-excluded" };

/** The line's class: its kind, and for a heating device's own reading and setpoint the device's colour. */
function seriesClass(h: History, s: Series): string {
  if (s.kind !== "thermostat" && s.kind !== "setpoint") return CLS[s.kind];
  const devices = [...new Set(h.series.filter((x) => x.kind === "setpoint").map((x) => x.entity_id))];
  return `${CLS[s.kind]} cl-dev-${Math.max(0, devices.indexOf(s.entity_id)) % 4}`;
}

function formatter(timeZone: string | null, span: number): Intl.DateTimeFormat {
  const opts: Intl.DateTimeFormatOptions = span > 2 * 86400
    ? { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }
    : { hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
  try {
    return new Intl.DateTimeFormat(undefined, { ...opts, timeZone: timeZone ?? undefined });
  } catch {
    return new Intl.DateTimeFormat(undefined, opts);
  }
}

function path(points: Array<[number, number | null]>, x: (t: number) => number, y: (v: number) => number, step: boolean, end: number): string {
  let d = "";
  let pen = false;
  let lastY = 0;
  for (let i = 0; i < points.length; i++) {
    const [t, v] = points[i];
    if (v === null) { pen = false; continue; }
    const px = x(t), py = y(v);
    if (!pen) d += `M${px.toFixed(1)},${py.toFixed(1)}`;
    else if (step) d += `L${px.toFixed(1)},${lastY.toFixed(1)}L${px.toFixed(1)},${py.toFixed(1)}`;
    else d += `L${px.toFixed(1)},${py.toFixed(1)}`;
    pen = true;
    lastY = py;
    if (step && (i === points.length - 1 || points[i + 1][1] === null)) {
      const next = i === points.length - 1 ? end : points[i + 1][0];
      d += `L${x(next).toFixed(1)},${py.toFixed(1)}`;
    }
  }
  return d;
}

export function historyChart(h: History, unit: Unit, hidden: Set<string>): TemplateResult {
  const W = 760, L = 48, R = 12, T = 12, plotH = 220;
  const lanes = h.bands;
  const laneH = 14, laneGap = 4;
  const lanesTop = T + plotH + 30;
  const H = lanesTop + lanes.length * (laneH + laneGap) + (lanes.length ? 6 : 0);
  const pw = W - L - R;
  const span = Math.max(1, h.end - h.start);
  const x = (t: number) => L + ((t - h.start) / span) * pw;
  const visible = h.series.filter((s) => !hidden.has(s.key));
  const values = visible.flatMap((s) => s.points.map((p) => toDisplay(p[1], unit)).filter((v): v is number => v !== null));
  let lo = values.length ? Math.min(...values) : unit === "°F" ? 60 : 15;
  let hi = values.length ? Math.max(...values) : unit === "°F" ? 75 : 24;
  if (hi - lo < 4) { const mid = (hi + lo) / 2; lo = mid - 2; hi = mid + 2; }
  lo = Math.floor(lo - 0.5);
  hi = Math.ceil(hi + 0.5);
  const y = (v: number) => T + (1 - (v - lo) / (hi - lo)) * plotH;
  const stepV = (hi - lo) > 30 ? 10 : (hi - lo) > 12 ? 5 : (hi - lo) > 6 ? 2 : 1;
  const ticksY: number[] = [];
  for (let v = Math.ceil(lo / stepV) * stepV; v <= hi; v += stepV) ticksY.push(v);
  const fmt = formatter(h.time_zone, span);
  const ticksX = Array.from({ length: 7 }, (_, i) => h.start + (span * i) / 6);
  const observedFrom = h.observed_from && h.observed_from > h.start ? h.observed_from : null;
  return html`<figure class="cl-chart" data-testid="climate-history-chart">
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Temperature history">
      ${ticksY.map((v) => svg`<line class="cl-grid" x1=${L} x2=${W - R} y1=${y(v)} y2=${y(v)}></line>
        <text class="cl-axis" x=${L - 6} y=${y(v) + 4} text-anchor="end">${v}°</text>`)}
      ${Object.values(h.gaps).flat().filter(([a, z]) => z > h.start && a < h.end).map(([a, z]) => svg`<rect class="cl-unobserved" x=${x(Math.max(a, h.start))} y=${T} width=${Math.max(1, x(Math.min(z, h.end)) - x(Math.max(a, h.start)))} height=${plotH}><title>Not observed (the App or Home Assistant was not running, or the Recorder no longer had it)</title></rect>`)}
      ${observedFrom ? svg`<rect class="cl-unobserved" x=${L} y=${T} width=${Math.max(0, x(observedFrom) - L)} height=${plotH}><title>Not observed yet</title></rect>` : nothing}
      ${ticksX.map((t, i) => svg`<text class="cl-axis" x=${x(t)} y=${T + plotH + 16} text-anchor=${i === 0 ? "start" : i === 6 ? "end" : "middle"}>${fmt.format(new Date(t * 1000))}</text>`)}
      ${visible.map((s) => {
        const pts = s.points.map((p) => [p[0], toDisplay(p[1], unit)] as [number, number | null]);
        return svg`<path class="cl-line ${seriesClass(h, s)}" d=${path(pts, x, y, s.kind === "setpoint", h.end)}><title>${s.label}</title></path>`;
      })}
      ${lanes.map((b, i) => {
        const top = lanesTop + i * (laneH + laneGap);
        return svg`<text class="cl-lane-label" x=${L - 6} y=${top + laneH - 3} text-anchor="end">${b.kind === "heating" ? "heat" : b.kind === "open" ? (b.opening === "door" ? "door" : b.opening === "window" ? "window" : "open") : b.kind === "excluded" ? "excl." : "occ."}</text>
          <rect class="cl-lane" x=${L} y=${top} width=${pw} height=${laneH} rx="3"><title>${b.label}${b.kind === "heating" && !b.reported ? " (this device does not report its activity)" : ""}</title></rect>
          ${b.kind === "heating" && !b.reported ? svg`<text class="cl-lane-note" x=${L + 6} y=${top + laneH - 3}>activity not reported by this device</text>` : nothing}
          ${b.intervals.map(([a, z]) => svg`<rect class=${BAND_CLS[b.kind]} x=${x(a)} y=${top} width=${Math.max(1, x(z) - x(a))} height=${laneH} rx="2"><title>${b.label}: ${fmt.format(new Date(a * 1000))}–${fmt.format(new Date(z * 1000))}</title></rect>`)}`;
      })}
    </svg>
  </figure>`;
}

export function legend(h: History, hidden: Set<string>, toggle: (key: string) => void): TemplateResult {
  return html`<ul class="cl-legend" aria-label="Series">
    ${h.series.map((s) => html`<li><button type="button" class="cl-legend-btn ${hidden.has(s.key) ? "off" : ""}" aria-pressed=${hidden.has(s.key) ? "false" : "true"} @click=${() => toggle(s.key)}>
      <svg width="26" height="10" viewBox="0 0 26 10" aria-hidden="true"><path class="cl-line ${seriesClass(h, s)}" d="M1,5 L25,5"></path></svg><span>${s.label}</span></button></li>`)}
    ${h.bands.length ? html`<li class="cl-legend-band"><span class="cl-swatch cl-band-heating"></span>reported heating</li>
      <li class="cl-legend-band"><span class="cl-swatch cl-band-open"></span>window / door open</li>
      <li class="cl-legend-band"><span class="cl-swatch cl-band-occupied"></span>occupied</li>` : nothing}
  </ul>`;
}

// ---------------------------------------------------- thermal learning (A7.1)
/** The held-out reconstruction: measured temperature (solid) and the model's paths (dashed), each restarted from
 * the measured temperature; persistence dotted. A historical reconstruction on data the model never saw, not a
 * forecast. Lines break at nulls, so nothing crosses an excluded or unobserved period (shaded). */
export function validationChart(c: ValidationChart, unit: Unit, timeZone: string | null): TemplateResult {
  const h: History = {
    area_id: "", window: c.to - c.from, windows: [], start: c.from, end: c.to, unit, observed_from: null, gaps: {}, records: 1,
    backfill: { state: "done", done_at: null, error: null, windows: 0, records: 0, entities: 0 }, source: "", time_zone: timeZone,
    series: [
      { key: "actual", label: "Measured room temperature", kind: "room", entity_id: "", points: c.actual },
      { key: "model", label: `Model reconstruction (restarted every ${c.restart_hours} h)`, kind: "model", entity_id: "", points: c.model },
      { key: "persistence", label: "Persistence baseline (no change)", kind: "persistence", entity_id: "", points: c.persistence },
    ],
    bands: [{ key: "excluded", label: "Excluded from learning (window open, unknown state, sensor problem, not observed)", kind: "excluded", entity_id: "", intervals: c.excluded }],
  };
  return historyChart(h, unit, new Set());
}

/** Prediction error over the same held-out period (model and persistence), symmetric around zero. */
export function errorChart(c: ValidationChart, unit: Unit, timeZone: string | null): TemplateResult {
  const W = 760, L = 48, R = 12, T = 10, plotH = 140;
  const span = Math.max(1, c.to - c.from);
  const x = (t: number) => L + ((t - c.from) / span) * (W - L - R);
  const scale = unit === "°F" ? 1.8 : 1;
  const all = [...c.error_model, ...c.error_persistence].map((p) => p[1]).filter((v): v is number => v !== null).map((v) => Math.abs(v * scale));
  const lim = Math.max(0.5, Math.ceil((all.length ? Math.max(...all) : 1) * 2) / 2);
  const y = (v: number) => T + (1 - (v + lim) / (2 * lim)) * plotH;
  const fmt = formatter(timeZone, span);
  const ticksX = Array.from({ length: 5 }, (_, i) => c.from + (span * i) / 4);
  const pts = (p: Array<[number, number | null]>) => p.map(([t, v]) => [t, v === null ? null : v * scale] as [number, number | null]);
  return html`<figure class="cl-chart" data-testid="climate-error-chart">
    <svg viewBox="0 0 ${W} ${T + plotH + 24}" role="img" aria-label="Prediction error">
      ${[-lim, 0, lim].map((v) => svg`<line class=${v === 0 ? "cl-zero" : "cl-grid"} x1=${L} x2=${W - R} y1=${y(v)} y2=${y(v)}></line>
        <text class="cl-axis" x=${L - 6} y=${y(v) + 4} text-anchor="end">${v > 0 ? "+" : ""}${v}°</text>`)}
      ${c.excluded.filter(([a, z]) => z > c.from && a < c.to).map(([a, z]) => svg`<rect class="cl-unobserved" x=${x(Math.max(a, c.from))} y=${T} width=${Math.max(1, x(Math.min(z, c.to)) - x(Math.max(a, c.from)))} height=${plotH}></rect>`)}
      ${ticksX.map((t, i) => svg`<text class="cl-axis" x=${x(t)} y=${T + plotH + 16} text-anchor=${i === 0 ? "start" : i === 4 ? "end" : "middle"}>${fmt.format(new Date(t * 1000))}</text>`)}
      <path class="cl-line cl-line-persistence" d=${path(pts(c.error_persistence), x, y, false, c.to)}><title>Persistence error</title></path>
      <path class="cl-line cl-line-model" d=${path(pts(c.error_model), x, y, false, c.to)}><title>Model error</title></path>
    </svg>
    <figcaption class="cl-legend"><span><svg width="26" height="10" aria-hidden="true"><path class="cl-line cl-line-model" d="M1,5 L25,5"></path></svg>model − measured</span>
      <span><svg width="26" height="10" aria-hidden="true"><path class="cl-line cl-line-persistence" d="M1,5 L25,5"></path></svg>persistence − measured</span></figcaption>
  </figure>`;
}
