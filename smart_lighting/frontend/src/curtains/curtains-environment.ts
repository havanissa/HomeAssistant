// Curtains → Environment (Phase A5): the sensor-quality summary (every candidate per role and its validity now),
// the environment and temperature settings for all windows (``auto/settings`` at the global scope, generated from
// ``rules.schema``), the sun now with its calibration check (``calibration/reset``) and the sun sources
// (``settings``). Doubtful readings never open a window; nothing here moves anything.
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { chip, kv, StoreElement } from "../ui";
import { cmd, curtains, type CurtainsModel, type Obj, type Reading, type Rule, type SimulationWindow } from "./api";
import { closeDialog, deg, ENV_GROUPS, fmt, needsSimulation, num1, openDialog, planRows, problemNotice, READING_TONE, ruleField, ruleValue, type CurtainsHost } from "./components";

const ROLE_RULE: Record<string, string> = { outdoor_lux: "lux_entity", outdoor_temp: "outdoor_temp_entity", weather: "weather_entity", cloud: "cloud_entity" };

interface Sim {
  key: string;
  label: string;
  values: Obj;
  result: { windows: SimulationWindow[] } | null;
  error: string | null;
  busy: boolean;
}

export class SlCurtainsEnvironment extends StoreElement {
  static properties = { model: { attribute: false }, host: { attribute: false }, openRoles: { state: true }, sim: { state: true } };
  declare model: CurtainsModel;
  declare host: CurtainsHost;
  declare openRoles: Set<string>;
  declare sim: Sim | null;

  constructor() {
    super();
    this.openRoles = new Set();
    this.sim = null;
  }

  private save(values: Obj, done = "Saved for all windows"): void {
    void this.host.run(() => curtains(cmd("auto/settings"), { scope: "global", values }), done);
  }

  /** A confirm-flagged rule: show what every window would decide before it is saved. */
  private async simulate(rule: Rule, value: unknown): Promise<void> {
    const values = { [rule.key]: value };
    const s: Sim = { key: rule.key, label: rule.label, values, result: null, error: null, busy: true };
    this.sim = s;
    await openDialog(this, "dialog.cu-sim");
    try {
      s.result = await curtains<{ windows: SimulationWindow[] }>(cmd("auto/simulate"), { scope: "global", values });
    } catch (err) {
      s.error = err instanceof Error ? err.message : String(err);
    }
    s.busy = false;
    this.requestUpdate();
  }

  private async simApply(): Promise<void> {
    const s = this.sim;
    if (!s || !s.result) return;
    const ok = await this.host.run(() => curtains(cmd("auto/settings"), { scope: "global", values: s.values }), `${s.label} switched on for all windows`);
    if (ok) { closeDialog(this, "dialog.cu-sim"); this.sim = null; }
  }

  private change(rule: Rule, value: unknown): void {
    if (needsSimulation(rule, value)) void this.simulate(rule, value);
    else this.save({ [rule.key]: value });
  }

  private candidate(role: string, x: Reading): TemplateResult {
    const key = ROLE_RULE[role];
    const val = x.value === null || x.value === undefined ? "" : x.unit === "lx" ? `${Math.round(x.value)} lx` : `${num1(x.value)} ${x.unit}`;
    const isGlobal = !!key && this.model.environment.global[key] === x.entity_id;
    return html`<li class="cu-reading" data-entity=${x.entity_id ?? ""}>
      <span class="cu-reading-main"><strong>${x.name ?? x.entity_id}</strong>
        <span class="small muted"><code>${x.entity_id}</code>${val ? ` · ${val}` : ""}${x.since ? ` · since ${fmt(x.since)}` : ""}${x.reported ? ` · reported ${fmt(x.reported)}` : ""}</span>
        ${x.status !== "ok" && x.detail ? html`<span class="block small">${x.detail}</span>` : nothing}
        ${key && this.host.canEdit && !isGlobal && x.status === "ok" ? html`<span><button type="button" class="lp-link" ?disabled=${this.host.busy} @click=${() => this.save({ [key]: x.entity_id }, `${x.name ?? x.entity_id} used for all windows`)}>Use for all windows</button></span>` : nothing}</span>
      <span class="cu-chips">${x.in_use ? chip("info", "in use") : nothing}${isGlobal ? chip("ok", "all windows") : nothing}${chip(READING_TONE[x.status] ?? "neutral", x.status_word)}</span></li>`;
  }

  private roles(): TemplateResult {
    const env = this.model.environment;
    return html`${env.roles.map((r) => {
      const open = this.openRoles.has(r.role);
      const valid = r.candidates.filter((x) => x.status === "ok").length;
      return html`<section class="card" data-testid="curtains-env-role" data-role=${r.role}><header class="card-head">${icon(r.role.includes("lux") ? "brightness" : r.role.includes("temp") || r.role === "thermostat" ? "thermometer" : "weather-sunny")}<h3>${r.word}</h3>
          <span class="small muted">${valid} valid of ${r.candidates.length}${r.chosen.length ? ` · ${r.chosen.length} in use` : ""}</span></header>
        ${r.chosen.length ? html`<p class="small">In use: ${r.chosen.map((e) => html`<code>${e}</code> `)}</p>` : html`<p class="small muted">${ROLE_RULE[r.role] ? "Not chosen for all windows yet." : "Chosen per room or window (its Environment tab)."}</p>`}
        ${r.candidates.length ? html`<ul class="cu-readings">${r.candidates.slice(0, open ? 60 : 4).map((x) => this.candidate(r.role, x))}</ul>
          ${r.candidates.length > 4 ? html`<button type="button" class="lp-link" @click=${() => { const s = new Set(this.openRoles); if (open) s.delete(r.role); else s.add(r.role); this.openRoles = s; }}>${open ? "Show fewer" : `Show all ${r.candidates.length}`}</button>` : nothing}`
          : html`<p class="muted small">None found.</p>`}
      </section>`;
    })}`;
  }

  private settings(): TemplateResult {
    const m = this.model;
    const can = this.host.canEdit;
    const groups = m.rules.groups.filter(([g]) => ENV_GROUPS.has(g));
    return html`${groups.map(([g, title, desc]) => {
      const rules = m.rules.schema.filter((r) => r.group === g && r.scopes.includes("global"));
      return html`<section class="card" data-testid=${`curtains-env-${g}`}><header class="card-head">${icon(g === "thermal" ? "sun-thermometer" : "tune")}<h3>${title}</h3></header>
        <p class="small muted">${desc} Defaults for every window, unless a room or window sets its own (a window's Environment tab).</p>
        <div class="lp-form">${rules.map((rule) => {
          const { value, source } = ruleValue(m, null, "global", rule.key);
          return ruleField(rule, { value, source, scope: "global", disabled: !can || this.host.busy, entities: m.entities, weekdays: m.rules.weekdays,
            onChange: (v) => this.change(rule, v), onReset: () => this.save({ [rule.key]: null }, `${rule.label}: default again`) });
        })}</div></section>`;
    })}`;
  }

  private sun(): TemplateResult {
    const m = this.model;
    const s = m.settings;
    const sun = m.sun;
    const can = this.host.canEdit;
    const setting = (key: string, value: unknown, done: string) => void this.host.run(() => curtains(cmd("settings"), { [key]: value }), done);
    return html`<section class="card" data-testid="curtains-sun"><header class="card-head">${icon("weather-sunny")}<h3>The sun now</h3></header>
      ${kv([
        ["Azimuth", deg(sun.azimuth)], ["Elevation", deg(sun.elevation)], ["Sun angle", sun.sun_angle === null ? "not configured" : deg(sun.sun_angle)],
        ["Sun angle check", chip(sun.calibration.status === "validated" ? "ok" : sun.calibration.status === "inconsistent" ? "warning" : "neutral", sun.calibration.status)],
      ])}
      <p class="small muted">${sun.calibration.text}</p>
      <p class="small muted">sun.sun gives the standard azimuth and elevation. The sun angle (0° = the sun faces the south-facing windows) is shown and checked against the azimuth; it is not used to move anything.</p>
      ${can ? html`<button type="button" class="btn btn-small" data-testid="curtains-calibration-reset" ?disabled=${this.host.busy} @click=${() => void this.host.run(() => curtains(cmd("calibration/reset")), "Sun angle check restarted")}>${icon("refresh", 16)}Reset calibration</button>` : nothing}
      <h4 class="lp-h">Sun sources and settings for every window</h4>
      <div class="lp-form">
        <label class="lp-field"><span class="lp-label">Sun azimuth entity</span><input type="text" .value=${s.azimuth_entity} ?disabled=${!can} @change=${(e: Event) => setting("azimuth_entity", (e.target as HTMLInputElement).value.trim() || null, "Sun source saved")} /></label>
        <label class="lp-field"><span class="lp-label">Sun elevation entity</span><input type="text" .value=${s.elevation_entity} ?disabled=${!can} @change=${(e: Event) => setting("elevation_entity", (e.target as HTMLInputElement).value.trim() || null, "Sun source saved")} /></label>
        <label class="lp-field"><span class="lp-label">Sun angle entity</span><input type="text" .value=${s.sun_angle_entity ?? ""} placeholder="none" ?disabled=${!can} @change=${(e: Event) => setting("sun_angle_entity", (e.target as HTMLInputElement).value.trim() || null, "Sun source saved")} />
          ${sun.sources.sun_angle && !sun.sources.sun_angle_exists ? html`<span class="small tone-warning">not found in Home Assistant</span>` : nothing}</label>
        <label class="lp-field"><span class="lp-label">Position tolerance</span><span class="lp-input-row"><input type="number" class="lp-num" min=${m.limits.tolerance[0]} max=${m.limits.tolerance[1]} .value=${String(s.tolerance)} ?disabled=${!can} @change=${(e: Event) => setting("tolerance", Math.round(Number((e.target as HTMLInputElement).value)), "Tolerance saved")} /><span class="muted">points</span></span><span class="small muted">A cover this close to a target is at it and gets no command.</span></label>
        <label class="lp-field"><span class="lp-label">Verify for</span><span class="lp-input-row"><input type="number" class="lp-num" min=${m.limits.verify_timeout[0]} max=${m.limits.verify_timeout[1]} .value=${String(s.verify_timeout)} ?disabled=${!can} @change=${(e: Event) => setting("verify_timeout", Math.round(Number((e.target as HTMLInputElement).value)), "Verify time saved")} /><span class="muted">s</span></span><span class="small muted">How long to wait for the reported position.</span></label>
      </div>
    </section>`;
  }

  private simDialog(): TemplateResult | typeof nothing {
    const s = this.sim;
    if (!s) return nothing;
    return html`<dialog class="sheet sheet-full review-dialog cu-sim" aria-labelledby="cu-sim-title" data-testid="curtains-simulate" @close=${() => (this.sim = null)}>
      <div class="sheet-inner"><header class="sheet-head"><h2 id="cu-sim-title">Before you switch on ${s.label}</h2><button type="button" class="icon-btn" aria-label="Close" @click=${() => { closeDialog(this, "dialog.cu-sim"); this.sim = null; }}>${icon("close")}</button></header>
        <div class="sheet-body">
          ${s.error ? html`<p class="notice notice-error" role="alert">${icon("error", 20)}<span>${s.error}</span></p>` : nothing}
          ${this.host.error ? problemNotice(this.host.error) : nothing}
          ${s.busy ? html`<div class="loading" role="status"><span class="spinner"></span><span>Working out what would happen…</span></div>` : nothing}
          ${s.result ? simulationList(s.result.windows) : nothing}
          <p class="small muted">Predicted from the current measurements; nothing is saved or moved yet.</p>
        </div>
        <footer class="sheet-foot"><button type="button" class="btn" @click=${() => { closeDialog(this, "dialog.cu-sim"); this.sim = null; }}>Cancel</button>
          <button type="button" class="btn btn-primary" data-testid="curtains-simulate-apply" ?disabled=${!s.result || this.host.busy} @click=${() => void this.simApply()}>Switch on</button></footer></div></dialog>`;
  }

  protected render(): unknown {
    const env = this.model.environment;
    return html`<div data-testid="curtains-environment">
      <p class="muted small">Measurements for daylight, sunshine and temperature, found by type and unit. Nothing is used until you choose it. Missing, stale, stuck or implausible readings never open a window, and a forecast never overrides a working light sensor.</p>
      ${this.host.error && !this.sim ? problemNotice(this.host.error) : nothing}
      <div class="cu-chips lp-gap">${Object.entries(env.band_words).map(([k, w]) => chip(k === "cold" ? "info" : k === "comfortable" ? "ok" : k === "warm" ? "warning" : "error", w))}<span class="small muted">temperature bands</span>
        ${Object.entries(env.status_words).map(([k, w]) => chip(READING_TONE[k] ?? "neutral", w))}<span class="small muted">reading validity</span></div>
      <div class="grid engine-grid">
        ${this.sun()}
        ${this.settings()}
        ${this.roles()}
      </div>
      ${this.simDialog()}
    </div>`;
  }
}

/** The predicted result of a settings change (``auto/simulate``), per window. */
export function simulationList(windows: SimulationWindow[]): TemplateResult {
  if (!windows.length) return html`<p class="muted">No window is affected.</p>`;
  return html`<ul class="cu-rows">${windows.map((x) => {
    if (x.mode === "off" || !x.predicted) return html`<li class="cu-row"><span class="cu-row-main"><strong>${x.name}</strong><span class="small muted">Off: not evaluated</span></span></li>`;
    const cur = x.current ? `${x.current.condition_word}${x.current_preset_name ? ` → ${x.current_preset_name}` : ""}` : "—";
    const what = !x.changes ? "No change now" : x.mode === "automatic" ? (x.moves ? `${x.moves} cover${x.moves === 1 ? "" : "s"} would move at once` : "No cover needs to move") : "Observing: nothing moves";
    return html`<li class="cu-row" data-window=${x.window_id}><span class="cu-row-main"><strong>${x.name}</strong><span class="small muted">now ${cur}</span><span>→ ${x.predicted.condition_word}${x.preset_name ? ` → ${x.preset_name}` : ""}</span><span class="small muted">${x.predicted.text}</span>
      ${x.items?.length && x.moves ? planRows(x.items.filter((i) => i.decision === "move"), { reasons: false }) : nothing}</span>
      ${chip(x.changes ? (x.mode === "automatic" && x.moves ? "warning" : "info") : "ok", what)}</li>`;
  })}</ul>`;
}

customElements.define("sl-curtains-environment", SlCurtainsEnvironment);
