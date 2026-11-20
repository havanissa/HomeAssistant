// Phase A7.1: Climate → room → Thermal learning. What the room's thermal model learned from its observations, how
// accurate it is on held-out data, and the evidence behind it. Observation only: nothing here changes a thermostat.
// Route: #/climate/rooms/<area_id>/learning.
import "../climate/climate.css";

import { html, nothing, type PropertyValues, type TemplateResult } from "lit";

import {
  delta, getHistory, getRoom, getRoomLearning, learningExportUrl, LEARNING_TONE, rate, rebuild, resetModel, temp, WINDOW_LABELS,
  type Evidence, type History, type LearningRoom, type ModelParam, type RoomView, type ThermalModelDoc, type Unit,
} from "../climate/api";
import { errorChart, historyChart, legend, validationChart } from "../climate/chart";
import { icon } from "../icons";
import { haTime } from "../presence/components";
import { download } from "../rooms/model";
import { store } from "../store";
import { chip, pageHeader, StoreElement } from "../ui";
import { climateTabs, learningChip, observingNotice, problemText, roomTabs } from "./climate";

const CONF_TONE: Record<string, "ok" | "warning" | "neutral" | "info"> = { high: "ok", moderate: "info", low: "warning", none: "neutral" };
const BASIS: Record<string, string> = {
  observed: "measured", reported: "reported heating", inferred: "inferred from setpoints", derived: "derived",
  geometry: "window geometry", "geometry+illuminance": "window geometry and light sensors",
};
const TERMS: Record<string, string> = { heating: "Heating", solar: "Direct sun", night: "Night baseline" };

function paramLabel(name: string, doc: ThermalModelDoc, names: (a: string) => string): string {
  if (name.startsWith("coupling:")) return `Exchange with ${names(name.slice(9))}`;
  return {
    k_per_h: doc.structure.reference === "outdoor" ? "Outdoor influence (loss coefficient)" : "Relaxation rate (no outdoor source)",
    baseline_c_per_h: "Baseline gain",
    heating_c_per_h: "Heating response",
    heating_inferred_c_per_h: "Heating response (inferred)",
    heating_delay_min: "Heating delay",
    residual_warming_c: "Residual warming after heating stops",
    time_constant_h: "Thermal inertia (time constant)",
    balance_offset_c: "Unheated balance above outdoor",
    solar_c_per_h: "Direct sun (solar gain)",
    night_c_per_h: "Night baseline difference",
  }[name] ?? name;
}

function paramValue(p: ModelParam, unit: Unit): string {
  if (!p.available || p.value === null) return "Unavailable";
  if (p.unit === "°C/h") return rate(p.value, unit);
  if (p.unit === "1/h") return `${(p.value * 100).toFixed(1)} % of the difference per hour`;
  if (p.unit === "h") return `${p.value.toFixed(p.value < 10 ? 1 : 0)} h`;
  if (p.unit === "min") return `${p.value.toFixed(0)} min`;
  if (p.unit === "°C") return delta(p.value, unit);
  return `${p.value}`;
}

function seValue(p: ModelParam, unit: Unit): string {
  if (p.se === null || !p.available) return "";
  if (p.unit === "°C/h") return ` ± ${rate(p.se, unit).replace("+", "")}`;
  if (p.unit === "1/h") return ` ± ${(p.se * 100).toFixed(1)} %`;
  return "";
}

const ORDER = ["heating_c_per_h", "heating_inferred_c_per_h", "heating_delay_min", "residual_warming_c", "k_per_h", "time_constant_h",
  "balance_offset_c", "baseline_c_per_h", "night_c_per_h", "solar_c_per_h"];

export class SlClimateLearning extends StoreElement {
  static properties = {
    areaId: { type: String }, data: { state: true }, room: { state: true }, history: { state: true }, window: { state: true },
    error: { state: true }, actionError: { state: true }, busy: { state: true }, showDetails: { state: true }, muted: { state: true },
  };
  declare areaId: string;
  declare data: LearningRoom | null;
  declare room: RoomView | null;
  declare history: History | null;
  declare window: number;
  declare error: unknown;
  declare actionError: unknown;
  declare busy: boolean;
  declare showDetails: boolean;
  declare muted: Set<string>;
  private seen = -1;
  private timer: number | undefined;
  private lastHistory = 0;

  constructor() {
    super();
    this.areaId = "";
    this.data = null;
    this.room = null;
    this.history = null;
    this.window = 604800;
    this.error = null;
    this.actionError = null;
    this.busy = false;
    this.showDetails = false;
    this.muted = new Set();
  }

  protected willUpdate(changed: PropertyValues): void {
    super.willUpdate(changed);
    if (changed.has("areaId") && changed.get("areaId") !== undefined) {
      this.data = null;
      this.room = null;
      this.history = null;
      void this.reload(true);
    }
    if (store.climateVersion !== this.seen) {
      const first = this.seen === -1;
      this.seen = store.climateVersion;
      window.clearTimeout(this.timer);
      if (first) void this.reload(true);
      else this.timer = window.setTimeout(() => void this.reload(), 1500);
    }
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    window.clearTimeout(this.timer);
  }

  async reload(forceHistory = false): Promise<void> {
    const area = this.areaId;
    if (!area) return;
    try {
      const [data, room] = await Promise.all([getRoomLearning(area), getRoom(area)]);
      if (area !== this.areaId) return;
      this.data = data;
      this.room = room;
      this.error = null;
    } catch (err) {
      this.error = err;
    }
    if (forceHistory || !this.history || Date.now() - this.lastHistory > 60_000) await this.loadHistory();
  }

  private async loadHistory(): Promise<void> {
    const area = this.areaId;
    this.lastHistory = Date.now();
    try {
      const h = await getHistory(area, this.window);
      if (area === this.areaId) this.history = h;
    } catch {
      // The measurements chart is optional; the learning view still works without it.
    }
  }

  private async act(what: "rebuild" | "reset"): Promise<void> {
    if (what === "reset") {
      const ok = window.confirm("Reset the learned model of this room?\n\nThe model is archived and the room starts learning again. "
        + "The observations, the thermostats and the climate configuration are not changed.");
      if (!ok) return;
    }
    this.busy = true;
    try {
      if (what === "rebuild") await rebuild(this.areaId);
      else await resetModel(this.areaId);
      this.actionError = null;
      await this.reload();
    } catch (err) {
      this.actionError = err;
    } finally {
      this.busy = false;
    }
  }

  private names = (area: string): string => this.data?.names?.[area] ?? area;

  private header(d: LearningRoom): TemplateResult {
    const s = d.summary;
    const m = d.model;
    const attempt = s.last_attempt;
    return html`<section class="card pad cl-section" data-testid="learning-status">
      <div class="cl-row" style="justify-content:space-between">
        <div class="cl-row">${learningChip(s)}${s.running ? html`<span class="small" role="status"><span class="spinner"></span> Training…</span>` : s.queued ? html`<span class="small muted">Queued</span>` : nothing}
          ${m?.preliminary ? chip("warning", "Preliminary") : nothing}${s.validity.state === "outdated" ? chip("warning", "Outdated") : nothing}</div>
        <div class="cl-actions">
          <button type="button" class="btn btn-primary" data-testid="learning-rebuild" ?disabled=${this.busy || s.running} @click=${() => void this.act("rebuild")}>${icon("refresh", 18)}Rebuild model</button>
          <button type="button" class="btn" data-testid="learning-diagnostics" @click=${() => this.querySelector("#learning-evidence")?.scrollIntoView({ behavior: "smooth" })}>${icon("stethoscope", 18)}Training diagnostics</button>
          <button type="button" class="btn" data-testid="learning-details" ?disabled=${!m} @click=${() => (this.showDetails = !this.showDetails)} aria-expanded=${this.showDetails ? "true" : "false"}>${icon("json", 18)}Model details</button>
          <button type="button" class="btn" data-testid="learning-export" @click=${() => download(learningExportUrl(this.areaId))}>${icon("export", 18)}Export</button>
          <button type="button" class="btn" data-testid="learning-reset" ?disabled=${this.busy || !m} @click=${() => void this.act("reset")}>${icon("delete", 18)}Reset learned model</button>
        </div>
      </div>
      ${this.actionError ? problemText(this.actionError) : nothing}
      <p style="margin:0">${s.usable_days !== null
        ? html`<strong>${s.usable_days.toFixed(1)} of ${s.min_history_days} days</strong> of usable history (${s.observed_days.toFixed(1)} days observed).`
        : html`${s.observed_days.toFixed(1)} days observed so far; learning starts after a day of observations.`}</p>
      <div class="progress"><span style="width:${Math.min(100, Math.round((100 * (s.usable_days ?? 0)) / Math.max(1, s.min_history_days)))}%"></span></div>
      ${s.validity.state === "invalidated" ? html`<p class="notice notice-warning" data-testid="learning-invalidated">${icon("warning", 20)}<span><strong>This model no longer applies.</strong> ${s.validity.reasons.join("; ")}. It is not used; rebuild it to learn with the current configuration.</span></p>` : nothing}
      ${s.validity.state === "outdated" ? html`<p class="notice notice-info">${icon("info", 20)}<span>${s.validity.reasons.join("; ")}. The model is still used; rebuild it to include the change.</span></p>` : nothing}
      ${s.problem ? html`<p class="notice notice-error">${icon("error", 20)}<span>${s.problem.message}</span></p>` : nothing}
      ${attempt?.result && !["low", "moderate", "high"].includes(attempt.result) ? html`<p class="small muted" data-testid="learning-attempt">Last attempt ${attempt.finished_at ? haTime(false)(attempt.finished_at) : ""}: ${d.summary.status === "learning" ? "running" : attempt.result}${attempt.error ? ` — ${attempt.error}` : ""}${d.attempt.diagnostic?.reason ? ` — ${d.attempt.diagnostic.reason}` : ""}${attempt.kept ? ` — ${attempt.kept}` : ""}</p>` : nothing}
      ${s.prerequisites.length ? html`<ul class="cl-check-list" data-testid="learning-prerequisites">${s.prerequisites.map((p) => html`<li>${icon(p.blocking ? "warning" : "info", 18)}<span class=${p.blocking ? "tone-warning" : "muted"}>${p.text}</span></li>`)}</ul>` : nothing}
      ${this.showDetails && m ? html`<pre class="cl-pre" data-testid="learning-json">${JSON.stringify({ ...m, validation: m.validation ? { ...m.validation, chart: "(chart data omitted)" } : null }, null, 1)}</pre>` : nothing}
    </section>`;
  }

  private explanations(m: ThermalModelDoc): TemplateResult {
    return html`<section class="card pad cl-section" data-testid="learning-explanations"><h3>What Smart Comfort learned</h3>
      <ul class="cl-explain">${m.explanations.map((e) => html`<li>${e}</li>`)}</ul>
      ${m.confidence.capped ? html`<p class="small muted">${m.confidence.capped}.</p>` : nothing}
    </section>`;
  }

  private measurements(): TemplateResult {
    const h = this.history;
    const unit = this.data?.unit ?? "°C";
    return html`<section class="card pad cl-section" data-testid="learning-measurements">
      <div class="cl-row" style="justify-content:space-between"><h3>Measurements</h3>
        <div class="cl-windows" role="group" aria-label="Time window">${[86400, 259200, 604800, 2592000].map((w) => html`<button type="button" class="btn btn-small ${w === this.window ? "btn-primary" : ""}" aria-pressed=${w === this.window ? "true" : "false"} @click=${() => { this.window = w; void this.loadHistory(); }}>${WINDOW_LABELS[w]}</button>`)}</div></div>
      <p class="small muted">What the sensors measured (observed values only): room temperature, outdoor temperature, setpoints, reported heating, windows and doors, occupancy. Shaded: not observed.</p>
      ${h ? (h.records ? html`${historyChart(h, unit, this.muted)}${legend(h, this.muted, (k) => { const n = new Set(this.muted); if (n.has(k)) n.delete(k); else n.add(k); this.muted = n; })}` : html`<p class="muted">Nothing observed in this window yet.</p>`)
        : html`<div class="loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`}
      ${this.room?.covers.length ? html`<p class="small muted">Curtain positions and the sun are recorded for this room (${this.room.covers.length} cover(s)); they feed the direct-sun estimate.</p>` : nothing}
    </section>`;
  }

  private behaviour(m: ThermalModelDoc, unit: Unit): TemplateResult {
    const names = Object.keys(m.parameters).sort((a, b) => {
      const ia = ORDER.indexOf(a), ib = ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
    });
    return html`<section class="card pad cl-section" data-testid="learning-parameters"><h3>Learned behaviour</h3>
      <div class="table-wrap"><table class="plain-table cl-params">
        <thead><tr><th>Behaviour</th><th>Estimate</th><th>Confidence</th><th>Basis / why</th></tr></thead>
        <tbody>${names.map((n) => {
          const p = m.parameters[n];
          return html`<tr data-param=${n}><td>${paramLabel(n, m, this.names)}</td>
            <td>${paramValue(p, unit)}<span class="small muted">${seValue(p, unit)}</span></td>
            <td>${p.available ? chip(CONF_TONE[p.confidence] ?? "neutral", p.confidence) : chip("neutral", "—")}</td>
            <td><span class="cl-why">${p.available ? `${p.text ?? ""}${p.basis ? ` (${BASIS[p.basis] ?? p.basis})` : ""}` : p.reason ?? "Not identified"}</span></td></tr>`;
        })}</tbody></table></div>
      <p class="small muted">Model: one thermal mass per room. Rates are per hour, in addition to the losses; a term the data could not identify is left out rather than guessed.</p>
    </section>`;
  }

  private accuracy(m: ThermalModelDoc, unit: Unit): TemplateResult {
    const v = m.validation;
    if (!v) return html`<section class="card pad"><h3>Accuracy</h3><p class="muted">No held-out validation.</p></section>`;
    const tz = this.history?.time_zone ?? null;
    const when = (t: number) => haTime(false)(new Date(t * 1000).toISOString());
    const d = (x: number | undefined) => (x === undefined ? "—" : delta(x, unit).replace("+", ""));
    return html`<section class="card pad cl-section" data-testid="learning-accuracy"><h3>Accuracy on held-out data</h3>
      <p class="small muted">Measured on ${v.usable_hours.toFixed(0)} usable hours from ${when(v.from)} to ${when(v.to)} (${Math.round(v.coverage * 100)} % of that period usable). The model was fitted only on earlier data and never saw this period. Each prediction is simulated with the outdoor temperature, heating and sun that actually occurred (a perfect forecast of them), on periods with windows closed and the heating state known${m.structure.coupling.length ? "; the other rooms of the zone are held at their temperature at the start" : ""}.</p>
      <div class="table-wrap"><table class="plain-table" data-testid="learning-metrics">
        <thead><tr><th>Ahead</th><th>Model MAE</th><th>Model RMSE</th><th>Persistence MAE</th><th>Passive model MAE</th><th>Periods</th></tr></thead>
        <tbody>${Object.entries(v.horizons).map(([hz, r]) => html`<tr><td>${Number(hz) / 60} h</td>
          ${r.available ? html`<td><strong>${d(r.model?.mae)}</strong></td><td>${d(r.model?.rmse)}</td><td>${d(r.persistence?.mae)}</td><td>${r.passive ? d(r.passive.mae) : "—"}</td><td>${r.origins}</td>`
            : html`<td colspan="5" class="muted">${r.reason ?? "Unavailable"} (${r.origins})</td>`}</tr>`)}</tbody></table></div>
      ${Object.keys(v.conditions[v.headline ?? ""] ?? {}).length ? html`<p class="small" style="margin:0">By condition (${Number(v.headline) / 60} h ahead):
        ${Object.entries(v.conditions[v.headline ?? ""]).map(([k, c]) => html`<span class="chip chip-neutral"><span class="chip-label">${k}: ${d(c.model_mae)} vs ${d(c.persistence_mae)} (${c.origins})</span></span> `)}</p>` : nothing}
      <h4>Measured vs. model (historical reconstruction, not a forecast)</h4>
      ${validationChart(v.chart, unit, tz)}
      <ul class="cl-legend"><li><svg width="26" height="10" aria-hidden="true"><path class="cl-line cl-line-room" d="M1,5 L25,5"></path></svg>measured</li>
        <li><svg width="26" height="10" aria-hidden="true"><path class="cl-line cl-line-model" d="M1,5 L25,5"></path></svg>model, restarted from the measurement every ${v.chart.restart_hours} h</li>
        <li><svg width="26" height="10" aria-hidden="true"><path class="cl-line cl-line-persistence" d="M1,5 L25,5"></path></svg>persistence</li>
        <li><span class="cl-swatch cl-band-excluded"></span>excluded</li></ul>
      <h4>Error</h4>
      ${errorChart(v.chart, unit, tz)}
      ${Object.keys(m.uncertainty).length ? html`<p class="small muted">80 % of the held-out ${Object.keys(m.uncertainty).map((k) => `${Number(k) / 60} h`).join(" and ")} predictions were within ${Object.values(m.uncertainty).map((q) => d(q.q80)).join(" and ")}, with the actual conditions known. Real predictions also depend on how good the weather and heating forecasts are, so expect a wider error.</p>` : nothing}
    </section>`;
  }

  private evidence(d: LearningRoom, ev: Evidence | undefined, m: ThermalModelDoc | null, unit: Unit): TemplateResult {
    if (!ev) return html`<section class="card pad" id="learning-evidence"><h3>Evidence and diagnostics</h3><p class="muted">No training has run yet.</p></section>`;
    const when = (t: number | null | undefined) => (t ? haTime(false)(new Date(t * 1000).toISOString()) : "—");
    return html`<section class="card pad cl-section" id="learning-evidence" data-testid="learning-evidence"><h3>Evidence and diagnostics</h3>
      <div class="stat-tiles cl-tiles">
        <div class="stat"><span class="stat-value">${ev.usable_days.toFixed(1)}</span><span class="stat-label">usable days (${ev.usable_hours.toFixed(0)} h)</span></div>
        <div class="stat"><span class="stat-value">${ev.calendar_days.toFixed(1)}</span><span class="stat-label">calendar days observed</span></div>
        <div class="stat"><span class="stat-value">${ev.episodes.heating.count}</span><span class="stat-label">confirmed heating episodes (${ev.episodes.heating.hours.toFixed(0)} h)</span></div>
        <div class="stat"><span class="stat-value">${ev.episodes.passive.count + ev.episodes.solar.count}</span><span class="stat-label">passive cooling episodes (${(ev.episodes.passive.hours + ev.episodes.solar.hours).toFixed(0)} h)</span></div>
        <div class="stat"><span class="stat-value">${ev.episodes.solar.count}</span><span class="stat-label">sunny passive episodes</span></div>
        <div class="stat"><span class="stat-value">${ev.open_windows.count}</span><span class="stat-label">open window/door periods excluded (${ev.open_windows.hours.toFixed(1)} h)</span></div>
        <div class="stat"><span class="stat-value">${d.recorder_gap_hours.toFixed(1)} h</span><span class="stat-label">recorded gaps (not observed)</span></div>
        <div class="stat"><span class="stat-value">${ev.discontinuities}</span><span class="stat-label">sensor jumps</span></div>
      </div>
      <div class="cl-two">
        <div><h4>Time used and excluded</h4><table class="plain-table" data-testid="learning-exclusions"><tbody>
          ${Object.entries(ev.excluded).map(([k, h]) => html`<tr><td>${ev.excluded_text[k] ?? k}</td><td>${h.toFixed(1)} h</td></tr>`)}</tbody></table>
          <p class="small muted">Heating activity: ${ev.heat_basis === "reported" ? "reported by every device" : ev.heat_basis === "inferred" ? "not reported (inferred from setpoints, kept apart)" : ev.heat_basis === "mixed" ? "reported by some devices only (time with an unreporting device on is excluded)" : "no heating device"}.
            ${ev.secondary_hours ? `Secondary sensors replaced the primary for ${ev.secondary_hours.toFixed(0)} usable hours.` : ""}
            Direct sun: ${ev.solar_basis === "none" ? "no window geometry" : ev.solar_basis}.</p></div>
        ${m ? html`<div><h4>Model</h4><table class="plain-table"><tbody>
          <tr><td>Algorithm</td><td class="mono">${m.algorithm} · schema ${m.schema}</td></tr>
          <tr><td>Last successful fit</td><td>${haTime(false)(m.trained_at)}${m.training_seconds !== undefined ? ` (${m.training_seconds} s)` : ""}</td></tr>
          <tr><td>Data</td><td>${when(m.data.from)} – ${when(m.data.to)}</td></tr>
          <tr><td>Trained on</td><td>${when(m.data.train?.from)} – ${when(m.data.train?.to)} (${m.data.train?.blocks ?? 0} half-hour blocks)</td></tr>
          <tr><td>Validated on</td><td>${when(m.data.test?.from)} – ${when(m.data.test?.to)}</td></tr>
          <tr><td>Confidence</td><td>${m.confidence.level} · score ${m.confidence.score}</td></tr>
          ${m.fit ? html`<tr><td>Fit residual</td><td>${rate(m.fit.rmse_c_per_h, unit)} RMS per block (autocorrelation ${m.fit.residual_autocorrelation})</td></tr>` : nothing}
        </tbody></table></div>` : nothing}
      </div>
      ${m ? html`<details><summary>Confidence factors</summary><table class="plain-table"><tbody>${m.confidence.factors.map((f) => html`<tr><td>${f.key}</td><td>${Math.round(f.value * 100)} %</td><td class="small muted">${f.text}</td></tr>`)}</tbody></table>
          <p class="small muted">${m.confidence.note ?? ""}</p></details>
        <details><summary>Which terms were tested</summary><table class="plain-table" data-testid="learning-selection"><tbody>${(m.selection ?? []).map((x) => html`<tr><td>${TERMS[x.term] ?? (x.term.startsWith("cpl:") ? `Exchange with ${this.names(x.term.slice(4))}` : x.term)}</td>
          <td>${x.accepted ? chip("ok", "kept") : chip("neutral", x.removed ? "removed" : "not kept")}</td><td class="small muted">${x.gain !== undefined ? `held-out error ${x.gain >= 0 ? "−" : "+"}${Math.abs(x.gain * 100).toFixed(1)} %` : ""} ${x.reason ?? ""}</td></tr>`)}</tbody></table></details>
        ${(ev.heating_rates ?? []).length || (ev.cooling_rates ?? []).length ? html`<details><summary>Observed rates by condition</summary><table class="plain-table"><tbody>
          ${(ev.heating_rates ?? []).map((b) => html`<tr><td>Heating, ${b.bin}</td><td>${rate(b.median_rate_c_per_h, unit)}</td><td class="small muted">${b.episodes} episodes</td></tr>`)}
          ${(ev.cooling_rates ?? []).map((b) => html`<tr><td>Passive, ${b.bin}</td><td>${rate(b.median_rate_c_per_h, unit)}</td><td class="small muted">${b.episodes} episodes</td></tr>`)}
          ${ev.after_heating ? html`<tr><td>30 min after heating stopped</td><td>${delta(ev.after_heating.median_change_c, unit)}</td><td class="small muted">median of ${ev.after_heating.stops} stops</td></tr>` : nothing}
        </tbody></table></details>` : nothing}` : nothing}
      ${Object.keys(d.recorder_gaps).length ? html`<details><summary>Recorded gaps per entity</summary><ul class="plain-list">${Object.entries(d.recorder_gaps).map(([e, g]) => html`<li><span class="mono">${e}</span>: ${g.length} gap(s), ${(g.reduce((n, [a, b]) => n + b - a, 0) / 3600).toFixed(1)} h</li>`)}</ul></details>` : nothing}
    </section>`;
  }

  protected render(): unknown {
    const d = this.data;
    const back = html`<div class="page-actions"><a class="btn" href="#/climate/rooms">${icon("arrow-left", 18)}All rooms</a>
      <a class="btn" href="#/climate/settings/rooms?room=${encodeURIComponent(this.areaId)}">${icon("cog", 18)}Configure</a></div>`;
    const name = this.room?.name ?? this.areaId;
    if (!d) {
      return html`${pageHeader(name, "", back)}${climateTabs("rooms")}${roomTabs(this.areaId, "learning")}
        ${this.error ? problemText(this.error) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`}`;
    }
    const unit = d.unit;
    const m = d.model;
    const ev = m?.evidence ?? d.attempt.diagnostic?.evidence;
    return html`${pageHeader(name, "Thermal learning: how this room warms and cools, learned from its own observations.", back)}
      ${observingNotice()}
      ${climateTabs("rooms")}
      ${roomTabs(this.areaId, "learning")}
      ${this.error ? problemText(this.error) : nothing}
      <div class="stat-tiles cl-tiles" style="margin-bottom:1rem">
        <div class="stat"><span class="stat-value">${temp(this.room?.temperature.value_c, unit)}</span><span class="stat-label">room now</span></div>
        <div class="stat"><span class="stat-value tone-${LEARNING_TONE[d.summary.status] === "ok" ? "ok" : "warning"}">${d.summary.confidence ? d.summary.confidence.level : "—"}</span><span class="stat-label">model confidence</span></div>
        <div class="stat"><span class="stat-value">${d.summary.heating?.value !== undefined && d.summary.heating?.value !== null ? rate(d.summary.heating.value, unit) : "—"}</span><span class="stat-label">heating response${d.summary.heating?.basis === "inferred" ? " (inferred)" : ""}</span></div>
        <div class="stat"><span class="stat-value">${d.summary.accuracy ? delta(d.summary.accuracy.mae, unit).replace("+", "") : "—"}</span><span class="stat-label">${d.summary.accuracy ? `${d.summary.accuracy.horizon_min / 60} h error (persistence ${delta(d.summary.accuracy.persistence_mae, unit).replace("+", "")})` : "validation error"}</span></div>
      </div>
      ${this.header(d)}
      ${m && m.explanations.length ? html`<div style="margin-top:1rem">${this.explanations(m)}</div>` : nothing}
      <div style="margin-top:1rem">${this.measurements()}</div>
      ${m && Object.keys(m.parameters).length ? html`<div style="margin-top:1rem">${this.behaviour(m, unit)}</div>` : nothing}
      ${m && m.validation ? html`<div style="margin-top:1rem">${this.accuracy(m, unit)}</div>` : nothing}
      <div style="margin-top:1rem">${this.evidence(d, ev, m, unit)}</div>`;
  }
}
customElements.define("sl-climate-learning", SlClimateLearning);
