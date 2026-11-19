// One window (Phase A5): its covers and presets, the confirmed Apply (preview → confirm with the exact targets),
// the Automatic mode with its reviewed activation, the rule settings per scope (generated from ``rules.schema``,
// simulated before a confirm-flagged rule is switched on), the sun and environment of this window, and its log.
// Every value comes from the curtain runtime's model; every change is one of its commands. A cover is never moved
// without the confirmation dialog.
import "../lighting/lighting-pages.css";
import "../curtains/curtains.css";
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { chip, kv, pageHeader } from "../ui";
import { segments } from "../lighting/lighting-model";
import { cmd, curtains, type CurtainsModel, type EnableReview, type LayerModel, type MultiSunModel, type MultiSunStep, type Obj, type Plan, type PresetModel, type Rule, type SimulationWindow, type WindowModel } from "../curtains/api";
import {
  adminNotice,
  BAND_TONE,
  closeDialog,
  COND_ICON,
  conditionChip,
  controlBanner,
  coverStateText,
  CurtainsElement,
  deg,
  DIRECT_TONE,
  DIRECT_WORD,
  ENV_GROUPS,
  fmt,
  fmtS,
  KIND_ICON,
  loadModel,
  mdi,
  modeChip,
  needsSimulation,
  num1,
  openDialog,
  OVERALL_TONE,
  pct,
  planRows,
  plural,
  positionBar,
  problemList,
  problemNotice,
  readingRow,
  RESULT_TONE,
  ruleField,
  ruleValue,
  STATUS_TONE,
  transitionList,
  windowWarnings,
} from "../curtains/components";
import { simulationList } from "../curtains/curtains-environment";

type Tab = "overview" | "covers" | "presets" | "automatic" | "environment" | "log";
const TABS: Array<{ key: Tab; title: string; icon: string }> = [
  { key: "overview", title: "Overview", icon: "view-dashboard" },
  { key: "covers", title: "Covers", icon: "blinds" },
  { key: "presets", title: "Presets", icon: "tune" },
  { key: "automatic", title: "Automatic", icon: "robot" },
  { key: "environment", title: "Environment", icon: "weather-sunny" },
  { key: "log", title: "Log", icon: "history" },
];

interface ApplyState { preset_id: string; preview: boolean; plan: Plan | null; error: string | null; busy: boolean }
interface EnableState { review: EnableReview | null; error: string | null; busy: boolean }
interface SimState { label: string; msg: Obj; result: { windows: SimulationWindow[] } | null; error: string | null; busy: boolean }
interface PresetDraft { preset_id: string | null; name: string; positions: Record<string, number | null> }
/** App 0.8.0: the read-only multi-sensor sunlight test. */
interface SunSimState { now: Record<string, string>; then: Record<string, string>; minutes: string; from: "current" | "inactive" | "active"; result: { steps: MultiSunStep[]; result: MultiSunStep } | null; error: string | null; busy: boolean }

export class SlCurtainsWindow extends CurtainsElement {
  static properties = {
    ...CurtainsElement.properties, windowId: { type: String }, tab: { type: String }, model: { state: true },
    apply: { state: true }, enable: { state: true }, sim: { state: true }, presetDraft: { state: true }, scope: { state: true }, addEntity: { state: true },
    askDelete: { state: true }, openGroups: { state: true }, sunAdd: { state: true }, sunSim: { state: true },
  };
  declare windowId: string;
  declare tab: string;
  declare model: CurtainsModel | null;
  declare apply: ApplyState | null;
  declare enable: EnableState | null;
  declare sim: SimState | null;
  declare presetDraft: PresetDraft | null;
  declare scope: "window" | "room" | "global";
  declare addEntity: string | null;
  declare askDelete: PresetModel | null;
  declare openGroups: Set<string>;
  declare sunAdd: string;
  declare sunSim: SunSimState;

  constructor() {
    super();
    this.windowId = "";
    this.tab = "overview";
    this.model = null;
    this.apply = null;
    this.enable = null;
    this.sim = null;
    this.presetDraft = null;
    this.scope = "window";
    this.addEntity = null;
    this.askDelete = null;
    this.openGroups = new Set(["sleep", "daylight"]);
    this.sunAdd = "";
    this.sunSim = { now: {}, then: {}, minutes: "15", from: "current", result: null, error: null, busy: false };
  }

  async reload(): Promise<void> {
    try {
      this.model = await loadModel();
      this.error = null;
    } catch (err) {
      this.error = err;
    }
  }

  private get w(): WindowModel | null {
    return this.model?.windows.find((x) => x.window_id === this.windowId) ?? null;
  }

  private href(tab: Tab): string {
    return `#/curtains/windows/${encodeURIComponent(this.windowId)}/${tab}`;
  }

  private saveWindow(fields: Obj, done = "Saved"): Promise<boolean> {
    return this.run(() => curtains(cmd("window/update"), { window_id: this.windowId, ...fields }), done);
  }

  // ----------------------------------------------------------------- apply (preview → confirm)
  private async openPlan(presetId: string, preview: boolean): Promise<void> {
    const a: ApplyState = { preset_id: presetId, preview, plan: null, error: null, busy: true };
    this.apply = a;
    await openDialog(this, "dialog.cu-apply");
    try {
      a.plan = await curtains<Plan>(cmd("preview"), { window_id: this.windowId, preset_id: presetId });
    } catch (err) {
      a.error = err instanceof Error ? err.message : String(err);
    }
    a.busy = false;
    this.requestUpdate();
  }

  private async applyConfirmed(): Promise<void> {
    const a = this.apply;
    if (!a?.plan) return;
    const plan = a.plan;
    a.busy = true;
    this.requestUpdate();
    const ok = await this.run(() => curtains(cmd("apply"), { window_id: this.windowId, preset_id: plan.preset_id, confirm: true, expected: plan.positions }), `${plan.preset_name}: commands sent`);
    if (ok) this.closeApply();
    else { a.busy = false; a.error = this.error instanceof Error ? this.error.message : String(this.error); this.requestUpdate(); }
  }

  private closeApply(): void {
    closeDialog(this, "dialog.cu-apply");
    this.apply = null;
  }

  private applyDialog(w: WindowModel): TemplateResult | typeof nothing {
    const a = this.apply;
    if (!a) return nothing;
    const p = a.plan;
    const moves = p?.moves ?? 0;
    return html`<dialog class="sheet sheet-full review-dialog cu-apply" aria-labelledby="cu-apply-title" data-testid="curtains-apply-dialog" @close=${() => (this.apply = null)}>
      <div class="sheet-inner"><header class="sheet-head"><h2 id="cu-apply-title">${a.preview ? "Preview — nothing moves" : `Apply ${p?.preset_name ?? "the preset"} to ${w.name}?`}</h2><button type="button" class="icon-btn" aria-label="Close" @click=${() => this.closeApply()}>${icon("close")}</button></header>
        <div class="sheet-body">
          ${a.error ? html`<p class="notice notice-error" role="alert">${icon("error", 20)}<span>${a.error}</span></p>` : nothing}
          ${a.busy && !p ? html`<div class="loading" role="status"><span class="spinner"></span><span>Checking the covers…</span></div>` : nothing}
          ${p ? html`<p class="small muted">${p.preset_name}: ${moves ? plural(moves, "cover") + " would move" : "no cover needs a command"}${p.running ? " · a preset is already being applied" : ""}.</p>
            ${planRows(p.items)}
            ${!a.preview && moves ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>This sends real commands to ${plural(moves, "cover")}${w.mode === "observing" ? " (Observing: allowed because you ask for it)" : w.mode === "automatic" ? " (Automatic: the preset is then kept like a manual move)" : ""}. The targets you confirm are the exact ones sent; nothing else moves.</span></p>` : nothing}` : nothing}
        </div>
        <footer class="sheet-foot"><button type="button" class="btn" ?disabled=${a.busy && !!p} @click=${() => this.closeApply()}>${a.preview ? "Close" : "Cancel"}</button>
          ${a.preview
            ? html`<button type="button" class="btn btn-primary" data-testid="curtains-apply" ?disabled=${!p || !this.canEdit || w.mode === "off" || w.running} @click=${() => { a.preview = false; this.requestUpdate(); }}>Apply…</button>`
            : html`<button type="button" class="btn btn-primary" data-testid="curtains-apply-confirm" ?disabled=${!p || !moves || a.busy || !this.canEdit} @click=${() => void this.applyConfirmed()}>${icon("play-circle", 18)}Move ${plural(moves, "cover")}</button>`}</footer></div></dialog>`;
  }

  // ----------------------------------------------------------------- enable Automatic (reviewed)
  private async openEnable(): Promise<void> {
    const e: EnableState = { review: null, error: null, busy: true };
    this.enable = e;
    await openDialog(this, "dialog.cu-enable");
    try {
      e.review = await curtains<EnableReview>(cmd("auto/preview"), { window_id: this.windowId });
    } catch (err) {
      e.error = err instanceof Error ? err.message : String(err);
    }
    e.busy = false;
    this.requestUpdate();
  }

  private async enableConfirmed(): Promise<void> {
    const ok = await this.run(() => curtains(cmd("auto/enable"), { window_id: this.windowId, confirm: true }), "Automatic enabled");
    if (ok) { closeDialog(this, "dialog.cu-enable"); this.enable = null; }
    else if (this.enable) { this.enable.error = this.error instanceof Error ? this.error.message : String(this.error); this.requestUpdate(); }
  }

  private enableDialog(w: WindowModel): TemplateResult | typeof nothing {
    const e = this.enable;
    if (!e) return nothing;
    const r = e.review;
    const d = r?.decision;
    return html`<dialog class="sheet sheet-full review-dialog cu-enable" aria-labelledby="cu-enable-title" data-testid="curtains-enable-dialog" @close=${() => (this.enable = null)}>
      <div class="sheet-inner"><header class="sheet-head"><h2 id="cu-enable-title">Enable Automatic for ${w.name}?</h2><button type="button" class="icon-btn" aria-label="Close" @click=${() => { closeDialog(this, "dialog.cu-enable"); this.enable = null; }}>${icon("close")}</button></header>
        <div class="sheet-body">
          ${e.error ? html`<p class="notice notice-error" role="alert">${icon("error", 20)}<span>${e.error}</span></p>` : nothing}
          ${e.busy && !r ? html`<div class="loading" role="status"><span class="spinner"></span><span>Checking the window…</span></div>` : nothing}
          ${r ? html`
            <div class="review-block"><h3>Now: ${d?.condition_word ?? "—"} → ${r.preset_name ?? "keeps the position"}</h3><p class="small muted">${d?.text ?? ""}</p>
              ${r.items.length ? html`<p class="small">${r.moves ? `${plural(r.moves, "cover")} will move at once` : "No cover needs to move now"}</p>${planRows(r.items)}` : nothing}</div>
            ${r.blockers.length ? html`<div class="review-block"><h3>Must be resolved first</h3>${problemList(r.blockers, "error")}</div>` : nothing}
            ${r.warnings.length ? html`<div class="review-block"><h3>Good to know</h3>${problemList(r.warnings)}</div>` : nothing}
            <div class="review-block"><h3>Order of precedence</h3><ol class="cu-prec small">${r.precedence.map((p) => html`<li>${p.word}</li>`)}</ol></div>
            <p class="notice notice-warning">${icon("warning", 20)}<span>From now on this window moves on its own, without asking again. Every movement is explained here and in Diagnostics. Other windows stay as they are.</span></p>` : nothing}
        </div>
        <footer class="sheet-foot"><button type="button" class="btn" @click=${() => { closeDialog(this, "dialog.cu-enable"); this.enable = null; }}>Cancel</button>
          <button type="button" class="btn btn-primary" data-testid="curtains-enable-confirm" ?disabled=${!r || r.blockers.length > 0 || this.busy || !this.canEdit} @click=${() => void this.enableConfirmed()}>${icon("robot", 18)}Enable Automatic</button></footer></div></dialog>`;
  }

  // ----------------------------------------------------------------- rule settings (per scope)
  private ruleMsg(w: WindowModel, key: string, value: unknown): Obj {
    const scope = this.scope === "room" && !w.area_id ? "window" : this.scope;
    const msg: Obj = { scope, values: { [key]: value } };
    if (scope === "window") msg.window_id = w.window_id;
    if (scope === "room") msg.area_id = w.area_id;
    return msg;
  }

  private saveRule(w: WindowModel, rule: Rule, value: unknown): void {
    if (value !== null && needsSimulation(rule, value)) { void this.openSim(w, rule, value); return; }
    void this.run(() => curtains(cmd("auto/settings"), this.ruleMsg(w, rule.key, value)), value === null ? `${rule.label}: inherited again` : `${rule.label} saved`);
  }

  private async openSim(w: WindowModel, rule: Rule, value: unknown): Promise<void> {
    const msg = this.ruleMsg(w, rule.key, value);
    const s: SimState = { label: rule.label, msg, result: null, error: null, busy: true };
    this.sim = s;
    await openDialog(this, "dialog.cu-sim");
    try {
      s.result = await curtains<{ windows: SimulationWindow[] }>(cmd("auto/simulate"), msg);
    } catch (err) {
      s.error = err instanceof Error ? err.message : String(err);
    }
    s.busy = false;
    this.requestUpdate();
  }

  private async simApply(): Promise<void> {
    const s = this.sim;
    if (!s?.result) return;
    const ok = await this.run(() => curtains(cmd("auto/settings"), s.msg), `${s.label} switched on`);
    if (ok) { closeDialog(this, "dialog.cu-sim"); this.sim = null; }
    else { s.error = this.error instanceof Error ? this.error.message : String(this.error); this.requestUpdate(); }
  }

  private simDialog(): TemplateResult | typeof nothing {
    const s = this.sim;
    if (!s) return nothing;
    return html`<dialog class="sheet sheet-full review-dialog cu-sim" aria-labelledby="cu-sim-title" data-testid="curtains-simulate" @close=${() => (this.sim = null)}>
      <div class="sheet-inner"><header class="sheet-head"><h2 id="cu-sim-title">Before you switch on ${s.label}</h2><button type="button" class="icon-btn" aria-label="Close" @click=${() => { closeDialog(this, "dialog.cu-sim"); this.sim = null; }}>${icon("close")}</button></header>
        <div class="sheet-body">${s.error ? html`<p class="notice notice-error" role="alert">${icon("error", 20)}<span>${s.error}</span></p>` : nothing}
          ${s.busy ? html`<div class="loading" role="status"><span class="spinner"></span><span>Working out what would happen…</span></div>` : nothing}
          ${s.result ? simulationList(s.result.windows) : nothing}
          <p class="small muted">Predicted from the current measurements; nothing is saved or moved yet.</p></div>
        <footer class="sheet-foot"><button type="button" class="btn" @click=${() => { closeDialog(this, "dialog.cu-sim"); this.sim = null; }}>Cancel</button>
          <button type="button" class="btn btn-primary" data-testid="curtains-simulate-apply" ?disabled=${!s.result || this.busy} @click=${() => void this.simApply()}>Switch on</button></footer></div></dialog>`;
  }

  private rulesForm(w: WindowModel, m: CurtainsModel, only: (group: string) => boolean, title: string): TemplateResult {
    const scope = this.scope === "room" && !w.area_id ? "window" : this.scope;
    const can = this.canEdit;
    const scopes: Array<["window" | "room" | "global", string]> = [["window", "This window"], ...(w.area_id ? [["room", `Room ${w.area_name ?? ""}`] as ["room", string]] : []), ["global", "All windows"]];
    const groups = m.rules.groups.filter(([g]) => only(g) && g !== "schedule");
    return html`<section class="card wide" data-testid=${`curtains-rules-${title.toLowerCase().replace(/[^a-z]+/g, "-")}`}><header class="card-head">${icon("tune")}<h3>${title}</h3></header>
      <div class="lp-row">${segments(scope, scopes, (v) => (this.scope = v), { label: "Settings for", testid: "curtains-rule-scope" })}
        <span class="small muted">${scope === "window" ? "Values set here apply to this window only; the rest come from its room or all windows." : scope === "room" ? "Values set here apply to every window in this room that does not set its own." : "Defaults for every window, unless a room or window sets its own."}</span></div>
      ${w.auto.warnings.length ? problemList(w.auto.warnings) : nothing}
      ${groups.map(([g, label, desc]) => {
        const rules = m.rules.schema.filter((r) => r.group === g && r.scopes.includes(scope));
        const open = this.openGroups.has(g);
        return html`<div class="lp-subcard" data-group=${g}><button type="button" class="collapse-toggle" aria-expanded=${open ? "true" : "false"} @click=${() => { const s = new Set(this.openGroups); if (open) s.delete(g); else s.add(g); this.openGroups = s; }}>${icon(open ? "chevron-down" : "chevron-right", 18)}<strong>${label}</strong></button>
          <p class="small muted">${desc}</p>
          ${open ? html`${g === "lamp" && scope === "window" ? html`<p class="small">Window Lamp preset: <strong>${w.effective.lamp?.text ?? "—"}</strong> · <a class="text-link" href="#/curtains/modes">Set in Global Modes → Window Lamp</a></p>` : nothing}
            ${g === "lamp" && scope !== "window" ? html`<p class="small muted">The lamp and its preset are chosen per window.</p>` : nothing}
            ${rules.length ? html`<div class="lp-form">${rules.map((rule) => {
              const { value, source } = ruleValue(m, w, scope, rule.key);
              return ruleField(rule, { value, source, scope, disabled: !can || this.busy, entities: m.entities, weekdays: m.rules.weekdays,
                onChange: (v) => this.saveRule(w, rule, v), onReset: () => this.saveRule(w, rule, null) });
            })}</div>` : html`<p class="small muted">Nothing to set at this scope.</p>`}` : nothing}
        </div>`;
      })}
    </section>`;
  }

  // ----------------------------------------------------------------- tabs
  private autoBlock(w: WindowModel, full: boolean): TemplateResult {
    const a = w.auto;
    if (w.mode === "off") return html`<div class="cu-auto">${icon("power", 20)}<span class="muted">Off: curtains are not evaluated, and presets cannot be applied to this window.</span></div>`;
    const d = a.decision;
    if (!d) return html`<div class="cu-auto">${icon("pending", 20)}<span class="muted">Evaluating…</span></div>`;
    const st = a.status;
    const canResume = this.canEdit && (a.hold || (st && ["blocked", "failed"].includes(st.action) && st.code === "gave_up"));
    const what = d.preset_name ? html`<strong>${d.preset_name}</strong> <span class="small muted">(${d.source_word})</span>` : d.condition === "manual_override" || d.condition === "waiting" ? "keeps the position" : d.target.state === "no_action" ? html`No action <span class="small muted">(${d.source_word})</span>` : "no preset assigned";
    return html`<div class="cu-auto" data-condition=${d.condition}>${icon(COND_ICON[d.condition] ?? "robot", 22)}
      <div><div class="cu-auto-head">${conditionChip(d)} · ${what}${w.mode === "observing" ? chip("info", "Observing") : nothing}${d.base !== d.condition ? html`<span class="small muted">(automatic: ${d.base_word})</span>` : nothing}</div>
        <div class="small">${d.text}</div>
        ${st && st.text ? html`<div class="small">${chip(STATUS_TONE[st.action] ?? "neutral", st.word)} ${st.text !== d.text ? st.text : html`<span class="muted">since ${fmt(st.at)}</span>`}</div>` : nothing}
        <div class="small muted">Managed in <a class="text-link" href="#/curtains/modes">Global Modes</a> and <a class="text-link" href="#/curtains/schedule">Global Schedule</a>${a.next_evaluation ? ` · next check ${fmt(a.next_evaluation)}` : ""}</div>
        ${full && d.evidence?.length ? html`<div class="small muted">${d.evidence.join(" · ")}</div>` : nothing}
        ${full && d.direct_sun?.status && d.direct_sun.status !== "off" ? html`<div class="small"><strong>Direct sun:</strong> ${d.direct_sun.text}</div>` : nothing}
        ${full && d.thermal && d.thermal.state !== "off" ? html`<div class="small"><strong>Temperature:</strong> ${d.thermal.text}</div>` : nothing}
        ${a.hold ? html`<div class="small tone-warning">${icon("hand", 16)} Manual hold · ${a.hold.policy_word} · since ${fmt(a.hold.created)}${a.hold.name ? ` · ${a.hold.name}` : ""}${a.hold.from !== undefined ? ` (${a.hold.from ?? "—"} → ${a.hold.to ?? "—"} %)` : ""}</div>` : nothing}
        ${a.defer ? chip("warning", "Waits for the next change of condition") : nothing}
        ${canResume ? html`<div class="lp-row"><button type="button" class="btn btn-small" data-testid="curtains-resume" ?disabled=${this.busy} @click=${() => void this.run(() => curtains(cmd("resume"), { window_id: w.window_id }), "Automatic control resumed")}>${icon("play-circle", 16)}Resume automatic curtains</button></div>` : nothing}
      </div></div>`;
  }

  private overviewTab(w: WindowModel, m: CurtainsModel): TemplateResult {
    const sel = w.selected_preset;
    const canApply = this.canEdit && w.mode !== "off" && !w.running && !!sel && w.layers.length > 0;
    const op = w.last;
    return html`<div class="grid engine-grid">
      <section class="card wide" data-testid="curtains-window-covers"><header class="card-head">${icon("blinds")}<h3>Covers</h3>${w.running ? chip("info", "Applying a preset") : nothing}</header>
        ${w.layers.length ? w.layers.map((l) => html`<div class="cu-cover-row" data-layer=${l.layer_id}>${icon(KIND_ICON[l.kind] ?? "blinds", 20)}
          <span><span class="cu-cover-name"><strong>${l.name}</strong> <span class="small muted">${l.kind_word}${l.caps.position ? "" : " · open / close only"}</span></span>${positionBar(l.position, l.caps, { target: l.desired !== null && l.desired !== l.position ? l.desired : null, name: l.name, moving: l.moving })}</span>
          <span class="cu-cover-val">${coverStateText(l)}${l.desired !== null && l.desired !== l.position ? html`<span class="block small muted">→ ${l.desired} %${l.matches_desired ? " (within tolerance)" : ""}</span>` : nothing}</span></div>`) : html`<p class="muted">No covers yet: add one under <a class="text-link" href=${this.href("covers")}>Covers</a>.</p>`}
        ${kv([["Recognised now", w.current.status === "preset" ? html`<strong>${w.current.text}</strong>` : w.current.text], ["Sun exposure", html`${w.exposure.text}${w.solar_protection ? "" : " · sun protection off"}`]])}
        <div class="cu-apply-row">
          <label class="visually-hidden" for="cu-sel">Desired preset</label>
          <select id="cu-sel" data-testid="curtains-preset-select" ?disabled=${!this.canEdit || w.running || this.busy} @change=${(e: Event) => void this.run(() => curtains(cmd("preset/select"), { window_id: w.window_id, preset_id: (e.target as HTMLSelectElement).value || null }), "Preset selected")}>
            <option value="" ?selected=${!sel}>Choose a preset…</option>${w.presets.map((p) => html`<option value=${p.preset_id} ?selected=${p.preset_id === sel}>${p.name}${p.matches ? " ✓" : ""}</option>`)}</select>
          <button type="button" class="btn btn-primary" data-testid="curtains-apply" ?disabled=${!canApply || this.busy} title=${w.mode === "off" ? "This window is Off" : w.running ? "A preset is being applied" : !sel ? "Choose a preset first" : "Shows what moves, then asks you to confirm"} @click=${() => sel && void this.openPlan(sel, false)}>${icon("play-circle", 18)}Apply…</button>
          ${sel ? html`<button type="button" class="btn" data-testid="curtains-preview" @click=${() => void this.openPlan(sel, true)}>${icon("eye", 18)}Preview</button>` : nothing}
        </div>
        <p class="small muted">Selecting and previewing never move anything. Apply shows every cover's target and asks you to confirm the exact targets before a command is sent.</p>
      </section>
      <section class="card" data-testid="curtains-window-auto"><header class="card-head">${icon("robot")}<h3>Automatic</h3>${modeChip(w.mode, w.mode_word)}</header>
        ${this.autoBlock(w, true)}
        ${w.auto.schedule?.next?.length ? html`<h4 class="lp-h">Next transitions</h4>${transitionList(w.auto.schedule.next, 3)}` : nothing}
      </section>
      <section class="card" data-testid="curtains-window-presets"><header class="card-head">${icon("tune")}<h3>Presets</h3><a class="btn btn-small" href=${this.href("presets")}>Edit</a></header>
        ${w.presets.length ? html`<ul class="cu-rows">${w.presets.map((p) => html`<li class="cu-row ${p.preset_id === sel ? "cu-selected" : ""}"><span class="cu-row-main"><strong>${p.name}</strong><span class="small muted">${this.positionsText(w, p)}</span></span>
          <span class="cu-chips">${p.preset_id === sel ? chip("info", "Selected") : nothing}${p.matches ? chip("ok", "Matches now") : nothing}</span></li>`)}</ul>` : html`<p class="muted small">No presets yet.</p>`}
      </section>
      <section class="card" data-testid="curtains-window-manual"><header class="card-head">${icon("hand")}<h3>Manual changes</h3></header>
        ${w.manual ? html`<p>${w.manual.name}: ${pct(w.manual.from)} → ${pct(w.manual.to)}<span class="block small muted">${fmtS(w.manual.at)}, ${w.manual.source === "person" ? "by a person in Home Assistant" : "outside Smart Occupancy"}</span></p>
          ${this.canEdit ? html`<button type="button" class="btn btn-small" data-testid="curtains-manual-clear" ?disabled=${this.busy} @click=${() => void this.run(() => curtains(cmd("manual/clear"), { window_id: w.window_id }), "Manual change forgotten")}>${icon("close-circle", 16)}Clear manual change</button>` : nothing}` : html`<p class="muted">None recorded.</p>`}
        <h4 class="lp-h">Last operation</h4>
        ${op ? html`<p>${chip(OVERALL_TONE[op.status] ?? "neutral", m.words.overall[op.status] ?? op.status)} ${op.explanation}<span class="block small muted">${op.preset_name} · ${fmtS(op.finished ?? op.started)}</span></p>` : html`<p class="muted">No preset applied yet.</p>`}
      </section>
      <section class="card" data-testid="curtains-window-problems"><header class="card-head">${icon("warning")}<h3>Checks</h3></header>${problemList(w.problems)}</section>
    </div>`;
  }

  private positionsText(w: WindowModel, p: PresetModel): string {
    return w.layers.map((l) => `${l.label ?? l.kind_word} ${p.positions[l.layer_id] === null || p.positions[l.layer_id] === undefined ? "unchanged" : pct(p.positions[l.layer_id])}`).join(" · ");
  }

  private layersPayload(w: WindowModel, change: Record<string, Obj> = {}, drop?: string, add?: Obj): Obj[] {
    const out: Obj[] = w.layers.filter((l) => l.layer_id !== drop).map((l) => ({ layer_id: l.layer_id, entity_id: l.entity_id, kind: l.kind, label: l.label, ...(change[l.layer_id] ?? {}) }));
    if (add) out.push(add);
    return out;
  }

  private coversTab(w: WindowModel, m: CurtainsModel): TemplateResult {
    const can = this.canEdit && !w.running;
    const free = m.candidates.filter((c) => !c.assigned_to).sort((a, b) => Number(b.area_id === w.area_id) - Number(a.area_id === w.area_id) || a.name.localeCompare(b.name));
    const cand = free.find((c) => c.entity_id === this.addEntity);
    return html`<div class="grid engine-grid">
      ${w.running ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>Layers can only change while no preset is being applied.</span></p>` : nothing}
      ${w.layers.map((l) => this.layerCard(w, l, m, can))}
      ${!w.layers.length ? html`<p class="muted">No covers yet.</p>` : nothing}
      ${w.layers.length === 1 ? html`<p class="small muted wide">With one cover, this window's presets (Presets tab) are the cover's presets.</p>` : nothing}
      ${this.canEdit && w.layers.length < m.limits.layers ? html`<section class="card" data-testid="curtains-add-cover"><header class="card-head">${icon("plus")}<h3>Add a cover</h3></header>
        <div class="lp-row"><select aria-label="Cover to add" ?disabled=${!can} @change=${(e: Event) => (this.addEntity = (e.target as HTMLSelectElement).value || null)}>
          <option value="" ?selected=${!this.addEntity}>Choose a cover…</option>${free.map((c) => html`<option value=${c.entity_id} ?selected=${this.addEntity === c.entity_id}>${c.name} · ${c.area_name ?? "No room"}${c.caps.position ? "" : " · open/close only"}</option>`)}</select>
          <button type="button" class="btn" ?disabled=${!can || !cand || this.busy} @click=${() => { if (cand) void this.saveWindow({ layers: this.layersPayload(w, {}, undefined, { entity_id: cand.entity_id, kind: cand.kind, label: null }) }, `${cand.name} added`).then((ok) => { if (ok) this.addEntity = null; }); }}>${icon("plus", 18)}Add</button></div>
        <p class="small muted">Covers already in another window are not offered: one cover belongs to one window. A new cover is "Leave unchanged" in the existing presets until you set it.</p></section>` : nothing}
    </div>`;
  }

  private layerCard(w: WindowModel, l: LayerModel, m: CurtainsModel, can: boolean): TemplateResult {
    const caps = (["position", "open", "close", "stop"] as const).map((k) => [k, l.caps[k]] as const);
    return html`<section class="card" data-layer=${l.layer_id} data-testid="curtains-layer"><header class="card-head">${icon(KIND_ICON[l.kind] ?? "blinds")}<h3>${l.name}</h3>${coverStateText(l)}</header>
      ${positionBar(l.position, l.caps, { name: l.name, moving: l.moving })}
      <p class="small muted lp-gap"><code>${l.entity_id}</code>${l.members.length ? ` · group of ${l.members.length}: ${l.members.join(", ")}` : ""}</p>
      <p class="small"><span class="cap-list">${caps.map(([k, ok]) => html`<span class="cap ${ok ? "" : "lp-cap-no"}" title=${ok ? "Reported by the cover" : "Not reported by the cover"}>${ok ? k : `no ${k}`}</span>`)}</span>
        ${!l.caps.position ? html`<span class="block small tone-warning">This cover can only open or close fully: presets may only set it to 0 % or 100 %.</span>` : nothing}</p>
      <div class="lp-form">
        <label class="lp-field"><span class="lp-label">Type</span><select ?disabled=${!can} @change=${(e: Event) => void this.saveWindow({ layers: this.layersPayload(w, { [l.layer_id]: { kind: (e.target as HTMLSelectElement).value } }) }, "Cover type saved")}>${m.kinds.map((k) => html`<option value=${k} ?selected=${k === l.kind}>${m.words.kinds[k]}</option>`)}</select></label>
        <label class="lp-field"><span class="lp-label">Own name</span><input type="text" maxlength="60" .value=${l.label ?? ""} placeholder=${l.entity_name} ?disabled=${!can} @change=${(e: Event) => void this.saveWindow({ layers: this.layersPayload(w, { [l.layer_id]: { label: (e.target as HTMLInputElement).value.trim() || null } }) }, "Cover name saved")} /></label>
      </div>
      ${l.conflicts.length ? html`<p class="small tone-warning">${plural(l.conflicts.filter((c) => c.enabled).length, "enabled automation")} may also move this cover: ${l.conflicts.map((c) => c.name ?? c.automation).join(", ")} · <a class="text-link" href="#/curtains/diagnostics">review in Diagnostics</a></p>` : nothing}
      ${w.layered && l.presets.length ? html`<p class="small muted">Own presets (used when this cover is assigned on its own in Global Modes): ${l.presets.map((p) => `${p.name} ${p.position} %`).join(" · ")} · <a class="text-link" href="#/curtains/modes">edit in Global Modes</a></p>` : nothing}
      ${can ? html`<button type="button" class="btn btn-small btn-danger" ?disabled=${this.busy} @click=${() => { if (confirm(`Remove ${l.name} from ${w.name}? The cover is not moved.`)) void this.saveWindow({ layers: this.layersPayload(w, {}, l.layer_id) }, `${l.name} removed`); }}>${icon("delete", 16)}Remove from window</button>` : nothing}
    </section>`;
  }

  // ----------------------------------------------------------------- presets
  private startPreset(w: WindowModel, p?: PresetModel): void {
    const positions: Record<string, number | null> = {};
    for (const l of w.layers) positions[l.layer_id] = p ? (p.positions[l.layer_id] ?? null) : (l.position ?? 50);
    this.presetDraft = { preset_id: p?.preset_id ?? null, name: p?.name ?? "", positions };
    void openDialog(this, "dialog.cu-preset");
  }

  private async savePreset(w: WindowModel): Promise<void> {
    const d = this.presetDraft;
    if (!d || !d.name.trim()) return;
    const ok = await this.run(() => curtains(cmd("preset/save"), { window_id: w.window_id, preset_id: d.preset_id, name: d.name.trim(), positions: d.positions }), "Preset saved");
    if (ok) { closeDialog(this, "dialog.cu-preset"); this.presetDraft = null; }
  }

  private presetDialog(w: WindowModel): TemplateResult | typeof nothing {
    const d = this.presetDraft;
    if (!d) return nothing;
    const set = (id: string, v: number | null) => { d.positions = { ...d.positions, [id]: v }; this.requestUpdate(); };
    return html`<dialog class="sheet sheet-full review-dialog cu-preset" aria-labelledby="cu-preset-title" data-testid="curtains-preset-dialog" @close=${() => (this.presetDraft = null)}>
      <div class="sheet-inner"><header class="sheet-head"><h2 id="cu-preset-title">${d.preset_id ? "Edit preset" : "New preset"}</h2><button type="button" class="icon-btn" aria-label="Close" @click=${() => { closeDialog(this, "dialog.cu-preset"); this.presetDraft = null; }}>${icon("close")}</button></header>
        <div class="sheet-body">${this.error ? problemNotice(this.error) : nothing}
          <label class="lp-field"><span class="lp-label">Name</span><input type="text" maxlength="60" .value=${d.name} data-testid="curtains-preset-name" @input=${(e: Event) => { d.name = (e.target as HTMLInputElement).value; this.requestUpdate(); }} /></label>
          ${w.layers.map((l) => {
            const v = d.positions[l.layer_id];
            const leave = v === null || v === undefined;
            const warn = !l.caps.position && !leave && v !== 0 && v !== 100;
            return html`<div class="cu-preset-pos" data-layer=${l.layer_id}>
              <span><strong>${icon(KIND_ICON[l.kind] ?? "blinds", 16)} ${l.name}</strong> <span class="small muted">${l.kind_word} · now ${pct(l.position)}</span>
                <input type="range" min="0" max="100" step="1" .value=${String(leave ? (l.position ?? 50) : v)} ?disabled=${leave} aria-label=${`${l.name} position`} @input=${(e: Event) => set(l.layer_id, Number((e.target as HTMLInputElement).value))} />
                <span class="cu-quick">${(l.caps.position ? [0, 25, 50, 75, 100] : [0, 100]).map((q) => html`<button type="button" class="btn btn-small ${v === q ? "lp-on" : ""}" @click=${() => set(l.layer_id, q)}>${q} %</button>`)}</span>
                ${warn ? html`<span class="block small tone-warning">This cover can only open or close fully: use 0 % or 100 %.</span>` : nothing}</span>
              <span><span class="lp-input-row"><input type="number" class="lp-num" min="0" max="100" step="1" .value=${leave ? "" : String(v)} ?disabled=${leave} aria-label=${`${l.name} position in %`} @change=${(e: Event) => { const raw = (e.target as HTMLInputElement).value.trim(); if (raw !== "") set(l.layer_id, Math.max(0, Math.min(100, Math.round(Number(raw))))); }} /><span class="muted">%</span></span>
                <label class="lp-check"><input type="checkbox" .checked=${leave} @change=${(e: Event) => set(l.layer_id, (e.target as HTMLInputElement).checked ? null : (l.position ?? 50))} /><span>Leave unchanged</span></label></span></div>`;
          })}
          <p class="small muted">0 % is closed, 100 % is fully open. Saving changes the preset only; it does not move the covers.</p></div>
        <footer class="sheet-foot"><button type="button" class="btn" @click=${() => { closeDialog(this, "dialog.cu-preset"); this.presetDraft = null; }}>Cancel</button>
          <button type="button" class="btn btn-primary" data-testid="curtains-preset-save" ?disabled=${!d.name.trim() || this.busy} @click=${() => void this.savePreset(w)}>${icon("save", 18)}Save</button></footer></div></dialog>`;
  }

  private presetsTab(w: WindowModel, m: CurtainsModel): TemplateResult {
    const can = this.canEdit;
    const sel = w.selected_preset;
    return html`<div class="grid engine-grid">
      <section class="card wide" data-testid="curtains-presets"><header class="card-head">${icon("tune")}<h3>Presets of ${w.name}</h3>
          ${can && w.layers.length && w.presets.length < m.limits.presets ? html`<button type="button" class="btn btn-small btn-primary" data-testid="curtains-preset-new" @click=${() => this.startPreset(w)}>${icon("plus", 16)}New preset</button>` : nothing}</header>
        <p class="small muted">Select the preset you want, then Apply on the Overview. Selecting, editing and previewing never move anything.</p>
        ${!w.layers.length ? html`<p class="muted">Add covers first.</p>` : nothing}
        ${w.presets.map((p) => html`<div class="cu-preset ${p.preset_id === sel ? "cu-selected" : ""}" data-preset=${p.preset_id}>
          <span><strong>${p.name}</strong> ${p.preset_id === sel ? chip("info", "Selected") : nothing}${p.matches ? chip("ok", "Matches now") : nothing}<span class="block small muted">${this.positionsText(w, p)}</span>
            ${w.usage[p.preset_id]?.length ? html`<span class="block small muted">Used by: ${w.usage[p.preset_id].join(", ")}</span>` : nothing}</span>
          <span class="cu-preset-actions">
            ${can && p.preset_id !== sel ? html`<button type="button" class="btn btn-small" ?disabled=${this.busy} @click=${() => void this.run(() => curtains(cmd("preset/select"), { window_id: w.window_id, preset_id: p.preset_id }), "Preset selected")}>Select</button>` : nothing}
            <button type="button" class="btn btn-small" @click=${() => void this.openPlan(p.preset_id, true)}>${icon("eye", 16)}Preview</button>
            ${can ? html`<button type="button" class="btn btn-small" ?disabled=${w.mode === "off" || w.running} @click=${() => void this.openPlan(p.preset_id, false)}>${icon("play-circle", 16)}Apply…</button>
              <button type="button" class="btn btn-small" @click=${() => this.startPreset(w, p)}>${icon("pencil", 16)}Edit</button>
              <button type="button" class="btn btn-small" ?disabled=${this.busy || w.presets.length >= m.limits.presets} @click=${() => void this.run(() => curtains(cmd("preset/duplicate"), { window_id: w.window_id, preset_id: p.preset_id }), "Preset duplicated")}>${icon("copy", 16)}Duplicate</button>
              <button type="button" class="btn btn-small btn-danger" aria-label=${`Delete ${p.name}`} ?disabled=${this.busy} @click=${() => { const used = w.usage[p.preset_id] ?? []; if (confirm(`Delete the preset ${p.name}?${used.length ? ` It is used by: ${used.join(", ")}. Those modes then keep the position.` : ""}`)) void this.run(() => curtains(cmd("preset/delete"), { window_id: w.window_id, preset_id: p.preset_id }), "Preset deleted"); }}>${icon("delete", 16)}</button>` : nothing}
          </span></div>`)}
      </section>
      <section class="card wide" data-testid="curtains-effective"><header class="card-head">${icon("view-grid")}<h3>Presets per mode</h3><a class="btn btn-small" href="#/curtains/modes">Global Modes</a></header>
        <p class="small muted">When each mode happens is set on the Global Schedule; which of this window's presets it uses is set on Global Modes (read-only here).</p>
        <ul class="cu-rows">${m.global_modes.modes.map((md) => html`<li class="cu-row"><span class="cu-row-main"><strong>${icon(mdi(md.icon), 16)} ${md.word}</strong><span class="small muted">${w.effective[md.key]?.text ?? "—"}</span>
          ${w.effective[md.key]?.problems?.length ? html`<span class="small tone-warning">${w.effective[md.key].problems.join(" · ")}</span>` : nothing}</span>
          ${w.auto.decision?.condition === md.key ? chip("ok", "Active now") : nothing}</li>`)}</ul>
      </section>
    </div>`;
  }

  private automaticTab(w: WindowModel, m: CurtainsModel): TemplateResult {
    const can = this.canEdit;
    const flag = (key: "sleep_protection" | "privacy" | "solar_protection", label: string, hint: string) => html`<label class="switch-row lp-switch"><input type="checkbox" role="switch" .checked=${w[key]} ?disabled=${!can || this.busy} data-flag=${key}
      @change=${(e: Event) => void this.saveWindow({ [key]: (e.target as HTMLInputElement).checked }, `${label} ${(e.target as HTMLInputElement).checked ? "on" : "off"}`)} /><span><span>${label}</span><span class="block small muted">${hint}</span></span></label>`;
    return html`<div class="grid engine-grid">
      <section class="card wide" data-testid="curtains-window-mode"><header class="card-head">${icon("robot")}<h3>Operating mode</h3>${modeChip(w.mode, w.mode_word)}</header>
        <div class="lp-row">${segments<"off" | "observing" | "automatic">(w.mode, [["off", "Off", "power"], ["observing", "Observing", "observe"], ["automatic", "Automatic…", "robot"]], (v) => {
            if (v === "automatic") void this.openEnable(); else void this.saveWindow({ mode: v }, `Mode: ${v}`);
          }, { disabled: !can || this.busy, label: "Operating mode", testid: "curtains-mode" })}
          ${w.mode !== "automatic" && can ? html`<button type="button" class="btn btn-primary" data-testid="curtains-enable" ?disabled=${this.busy} @click=${() => void this.openEnable()}>${icon("robot", 18)}Enable Automatic…</button>` : nothing}</div>
        <p class="small muted">${w.mode === "off" ? "Off: not evaluated, and presets cannot be applied to this window." : w.mode === "observing" ? "Observing: decides and explains what it would do; nothing moves on its own. You can still apply a preset yourself." : "Automatic: moves this window on its own and explains every decision. A preset you apply yourself is kept like a manual move."} Enabling Automatic is reviewed first: the intended preset, safety and competing automations.</p>
        ${this.autoBlock(w, true)}
        <h4 class="lp-h">This window handles</h4>
        ${flag("sleep_protection", "Sleep mode", "Stays closed while sleep is on (see Sleep mode below)")}
        ${flag("privacy", "Evening privacy", "Closes when it gets dark in the evening")}
        ${flag("solar_protection", "Sun protection", "Shades when the sun is in front of the window and the light is strong (direction and geometry on the Environment tab)")}
        <p class="small muted">Waiting for daylight and a manual override never move anything: the covers stay where they are. Useful solar heating keeps the Morning / Daytime preset. A mode without any assignment leaves the covers where they are.</p>
        <h4 class="lp-h">Order of precedence</h4>
        <ol class="cu-prec small">${w.auto.precedence.map((p) => html`<li>${p.word}</li>`)}</ol>
      </section>
      ${this.multiSunSection(w, m)}
      ${w.multi_sun ? this.rulesForm(w, m, (g) => g === "multisun", "Multi-sensor sunlight levels") : nothing}
      ${this.rulesForm(w, m, (g) => !ENV_GROUPS.has(g) && g !== "multisun", "Automatic settings")}
    </div>`;
  }

  // ------------------------------------------------- multi-sensor sunlight (App 0.8.0)
  private saveSunSensors(sensors: Array<{ entity_id: string; enabled: boolean }>, label: string): void {
    void this.saveWindow({ sun_sensors: sensors }, label);
  }

  private multiSunSection(w: WindowModel, m: CurtainsModel): TemplateResult {
    const can = this.canEdit;
    const ms: MultiSunModel | undefined = w.auto.environment?.multi_sun;
    const sensors = w.sun_sensors ?? [];
    const res = ms?.result ?? null;
    const shown = new Map((ms?.sensors ?? []).map((x) => [x.entity_id, x]));
    const name = (e: string) => shown.get(e)?.name ?? e;
    const lux = (m.entities?.lux ?? []).filter((x) => !sensors.some((s) => s.entity_id === x.entity_id));
    const status = !w.multi_sun ? chip("neutral", "Off") : !ms?.active_source ? chip("warning", "No sensor enabled")
      : res?.active ? chip("ok", "Sun protection requested") : chip("neutral", "No direct sunlight");
    const remaining = res?.remaining_seconds != null ? `${Math.floor(res.remaining_seconds / 60)} min ${String(res.remaining_seconds % 60).padStart(2, "0")} s` : null;
    return html`<section class="card wide" data-testid="curtains-multisun">
      <header class="card-head">${icon("weather-sunny")}<h3>Multi-sensor sunlight</h3>${status}</header>
      <p class="small muted">Optional sunlight evidence for this window. As soon as <strong>any</strong> enabled sensor reads more than “Activate above”, sun protection is requested. It ends only when <strong>every</strong> enabled sensor reads below “Release below” for the whole release delay. A sensor that is unavailable, stale, stuck or implausible is never sunshine, and while protection is on it prevents the release. Releasing re-evaluates the whole decision: sleep, night, privacy, manual holds and the movement limits still come first.</p>
      <label class="switch-row lp-switch"><input type="checkbox" role="switch" data-testid="multisun-enabled" .checked=${!!w.multi_sun} ?disabled=${!can || this.busy}
        @change=${(e: Event) => void this.saveWindow({ multi_sun: (e.target as HTMLInputElement).checked }, `Multi-sensor sunlight ${(e.target as HTMLInputElement).checked ? "on" : "off"}`)} />
        <span><span>Use this window's sunlight sensors</span><span class="block small muted">${w.solar_protection ? "Replaces the outdoor light sensor and the sun geometry as this window's sunlight evidence" : "Sun protection is off for this window: switch it on above to use the sensors"}</span></span></label>
      <ul class="cu-rows" data-testid="multisun-sensors">${sensors.length ? sensors.map((s) => {
        const r = shown.get(s.entity_id);
        const tone = !s.enabled ? "neutral" : r?.status === "ok" ? "ok" : r?.status === "none" ? "pending" : "warning";
        const reading = !s.enabled ? "Disabled" : r?.status === "ok" && r.value != null ? `${Math.round(r.value)} lx` : (r?.status_word ?? r?.status ?? "—");
        const marks = [res?.triggered_by === s.entity_id ? chip("info", "Triggered") : nothing,
          res?.above_on.includes(s.entity_id) ? chip("warning", "Above activation") : nothing,
          res?.blocking.includes(s.entity_id) ? chip("warning", "Prevents release") : nothing,
          res?.active && res.invalid.includes(s.entity_id) ? chip("error", "Release cannot be confirmed") : nothing];
        return html`<li class="cu-row" data-sensor=${s.entity_id}><span class="cu-row-main"><strong>${name(s.entity_id)}</strong><span class="small muted">${s.entity_id}${r?.detail && r.status !== "ok" ? ` · ${r.detail}` : ""}</span></span>
          ${chip(tone, reading)} ${marks}
          <span class="cu-row-actions">
            <button type="button" class="btn btn-small" ?disabled=${!can || this.busy} @click=${() => this.saveSunSensors(sensors.map((x) => x.entity_id === s.entity_id ? { ...x, enabled: !x.enabled } : x), `${name(s.entity_id)} ${s.enabled ? "disabled" : "enabled"}`)}>${s.enabled ? "Disable" : "Enable"}</button>
            <button type="button" class="btn btn-small" aria-label=${`Remove ${name(s.entity_id)}`} ?disabled=${!can || this.busy} @click=${() => this.saveSunSensors(sensors.filter((x) => x.entity_id !== s.entity_id), `${name(s.entity_id)} removed`)}>${icon("delete", 16)}</button>
          </span></li>`;
      }) : html`<li class="muted small">No sensor yet.</li>`}</ul>
      ${can ? html`<div class="lp-row">
        <input type="text" list="multisun-lux" placeholder="sensor.… (illuminance)" .value=${this.sunAdd} data-testid="multisun-add-input" @input=${(e: Event) => (this.sunAdd = (e.target as HTMLInputElement).value.trim())} />
        <datalist id="multisun-lux">${lux.map((x) => html`<option value=${x.entity_id}>${x.name}</option>`)}</datalist>
        <button type="button" class="btn" data-testid="multisun-add" ?disabled=${this.busy || !this.sunAdd.startsWith("sensor.") || sensors.length >= 8} @click=${() => { const e = this.sunAdd; this.sunAdd = ""; this.saveSunSensors([...sensors, { entity_id: e, enabled: true }], `${e} added`); }}>${icon("plus", 18)}Add sensor</button>
      </div>` : nothing}
      ${w.multi_sun && ms?.active_source ? html`<div class="cu-env-state" data-testid="multisun-state">
        ${kv([
          ["Now", res?.reason ?? "Not evaluated yet"],
          ["Triggered by", res?.active && res.triggered_by ? `${name(res.triggered_by)}${res.since ? ` at ${fmt(res.since)}` : ""}` : null],
          ["Above activation", res?.above_on.length ? res.above_on.map(name).join(", ") : res?.active ? "none" : null],
          ["Preventing release", res?.active ? ([...res.blocking, ...res.invalid].map(name).join(", ") || "none") : null],
          ["Release in", res?.active && remaining ? remaining : null],
          ["Levels", ms ? `activate above ${ms.levels.on_lux} lx · release below ${ms.levels.off_lux} lx for ${ms.levels.release_minutes} min` : null],
        ])}
      </div>` : nothing}
      ${w.multi_sun && sensors.length ? this.sunSimulation(w, sensors, name) : nothing}
    </section>`;
  }

  private sunSimulation(w: WindowModel, sensors: Array<{ entity_id: string; enabled: boolean }>, name: (e: string) => string): TemplateResult {
    const sim = this.sunSim;
    const value = (raw: string | undefined): number | null | "unavailable" => {
      const t = (raw ?? "").trim();
      if (t === "" || t.toLowerCase() === "unavailable") return t === "" ? null : "unavailable";
      const n = Number(t);
      return Number.isFinite(n) && n >= 0 ? n : null;
    };
    const run = async () => {
      this.sunSim = { ...sim, busy: true, error: null };
      try {
        const readings: Record<string, number | null | "unavailable"> = {};
        const then: Record<string, number | null | "unavailable"> = {};
        for (const s of sensors) {
          readings[s.entity_id] = value(sim.now[s.entity_id]);
          then[s.entity_id] = value(sim.then[s.entity_id] ?? sim.now[s.entity_id]);
        }
        const result = await curtains<{ steps: MultiSunStep[]; result: MultiSunStep }>(cmd("sun_sensors/simulate"), {
          window_id: w.window_id, readings, then, elapsed_minutes: Number(sim.minutes) || 0, from_state: sim.from });
        this.sunSim = { ...this.sunSim, result, busy: false };
      } catch (err) {
        this.sunSim = { ...this.sunSim, error: err instanceof Error ? err.message : String(err), busy: false };
      }
    };
    const field = (which: "now" | "then", e: string) => html`<input type="text" inputmode="decimal" class="cu-sim-input" aria-label=${`${name(e)} ${which === "now" ? "now" : "after"} (lx)`} placeholder=${which === "now" ? "lx" : "same"}
      .value=${sim[which][e] ?? ""} @input=${(ev: Event) => (this.sunSim = { ...this.sunSim, [which]: { ...this.sunSim[which], [e]: (ev.target as HTMLInputElement).value } })} />`;
    const r = sim.result?.result;
    return html`<details class="cu-sim" data-testid="multisun-simulation"><summary>Test the rules (read only)</summary>
      <p class="small muted">Enter readings in lx (empty or “unavailable” = no trustworthy reading), and what they become for how long. Nothing is saved and nothing moves.</p>
      <table class="plain-table small"><thead><tr><th scope="col">Sensor</th><th scope="col">Now</th><th scope="col">Then</th></tr></thead>
        <tbody>${sensors.map((s) => html`<tr><th scope="row">${name(s.entity_id)}${s.enabled ? nothing : html` <span class="muted">(disabled)</span>`}</th><td>${field("now", s.entity_id)}</td><td>${field("then", s.entity_id)}</td></tr>`)}</tbody></table>
      <div class="lp-row">
        <label>for <input type="number" min="0" max="1440" class="cu-sim-input" .value=${sim.minutes} @input=${(e: Event) => (this.sunSim = { ...this.sunSim, minutes: (e.target as HTMLInputElement).value })} /> min</label>
        <label>starting <select .value=${sim.from} @change=${(e: Event) => (this.sunSim = { ...this.sunSim, from: (e.target as HTMLSelectElement).value as SunSimState["from"] })}>
          <option value="current">from the window's state now</option><option value="inactive">with protection off</option><option value="active">with protection on</option></select></label>
        <button type="button" class="btn" data-testid="multisun-simulate" ?disabled=${sim.busy} @click=${() => void run()}>${icon("play-circle", 18)}Test</button>
      </div>
      ${sim.error ? html`<p class="notice notice-error">${sim.error}</p>` : nothing}
      ${r ? html`<div data-testid="multisun-sim-result">${kv([
        ["Result", r.active ? "Sun protection requested" : "No sun protection from the sensors"],
        ["Why", r.reason],
        ["Release in", r.active && r.remaining_seconds != null ? `${Math.floor(r.remaining_seconds / 60)} min ${String(r.remaining_seconds % 60).padStart(2, "0")} s` : null],
      ])}</div>` : nothing}
    </details>`;
  }

  private environmentTab(w: WindowModel, m: CurtainsModel): TemplateResult {
    const env = w.auto.environment;
    const r = env?.readings ?? {};
    const ds = env?.direct_sun;
    const th = env?.thermal;
    const c = env?.comfort;
    const can = this.canEdit;
    const sun = m.sun;
    const side = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${Math.abs(Math.round(v * 10) / 10)}° ${v >= 0 ? "right" : "left"}`);
    const solar = (fields: Obj, done: string) => void this.saveWindow({ solar: { ...w.solar, ...fields } }, done);
    return html`<div class="grid engine-grid">
      <section class="card" data-testid="curtains-window-sun"><header class="card-head">${icon(w.exposure.status === "exposed" ? "weather-sunny" : "weather-sunny-off")}<h3>Sun and direction</h3></header>
        <div class="cu-sun ${w.exposure.status === "exposed" ? "cu-exposed" : ""}">${icon(w.exposure.status === "exposed" ? "white-balance-sunny" : "weather-sunny-off", 28)}<div><strong>${w.exposure.text}</strong>
          <span class="block small muted">${w.exposure.offset !== null ? `The sun is ${Math.abs(w.exposure.offset)}° ${w.exposure.offset >= 0 ? "right" : "left"} of the window's facing direction (seen from inside).` : ""}</span></div></div>
        ${kv([
          ["Window faces", w.effective_bearing === null ? "not set" : `${deg(w.effective_bearing)} ${w.compass ?? ""}`],
          ["Sun azimuth", deg(sun.azimuth)], ["Sun elevation", deg(sun.elevation)], ["Sun angle", sun.sun_angle === null ? "not configured" : deg(sun.sun_angle)],
          env ? ["From the window", side(env.sun.offset)] : null,
          env ? ["Incidence", env.sun.incidence === null ? "—" : `${deg(env.sun.incidence)} (0° = straight on)`] : null,
          env?.sun.offset_via_sun_angle !== null && env?.sun.offset_via_sun_angle !== undefined ? ["Via sun angle", side(env.sun.offset_via_sun_angle)] : null,
        ])}
        <h4 class="lp-h">Direction</h4>
        <div class="lp-form">
          <div class="lp-field"><span class="lp-label">Compass</span>${segments(w.direction ?? "", m.compass.map((d) => [d, d] as [string, string]), (v) => void this.saveWindow({ direction: v, bearing: null }, `Direction ${v}`), { disabled: !can || this.busy, label: "Compass direction" })}</div>
          <label class="lp-field"><span class="lp-label">Exact bearing</span><span class="lp-input-row"><input type="number" class="lp-num" min="0" max="360" step="0.5" .value=${w.bearing === null ? "" : String(w.bearing)} placeholder=${w.effective_bearing === null ? "°" : String(w.effective_bearing)} ?disabled=${!can || this.busy}
            @change=${(e: Event) => { const raw = (e.target as HTMLInputElement).value.trim(); void this.saveWindow(raw === "" ? { direction: w.direction, bearing: null } : { bearing: Number(raw) }, "Bearing saved"); }} /><span class="muted">°</span></span><span class="small muted">0 = north, 90 = east, 180 = south; replaces the compass point.</span></label>
        </div>
        <h4 class="lp-h">Sun geometry</h4>
        <div class="lp-form">
          <label class="lp-field"><span class="lp-label">Exposure width</span><span class="lp-input-row">± <input type="number" class="lp-num" min=${m.limits.exposure_width[0]} max=${m.limits.exposure_width[1]} .value=${String(w.solar.exposure_width)} ?disabled=${!can} @change=${(e: Event) => solar({ exposure_width: Math.round(Number((e.target as HTMLInputElement).value)) }, "Exposure width saved")} /><span class="muted">° around the direction the window faces</span></span></label>
          <label class="lp-field"><span class="lp-label">Minimum elevation</span><span class="lp-input-row"><input type="number" class="lp-num" step="0.5" min=${m.limits.elevation[0]} max=${m.limits.elevation[1]} .value=${String(w.solar.min_elevation)} ?disabled=${!can} @change=${(e: Event) => solar({ min_elevation: Number((e.target as HTMLInputElement).value) }, "Minimum elevation saved")} /><span class="muted">° (lower sun does not reach the window)</span></span></label>
          <label class="lp-field"><span class="lp-label">Overhang</span><span class="lp-input-row"><input type="number" class="lp-num" step="1" min=${m.limits.max_elevation[0]} max=${m.limits.max_elevation[1]} .value=${w.solar.max_elevation === null ? "" : String(w.solar.max_elevation)} placeholder="none" ?disabled=${!can} @change=${(e: Event) => { const raw = (e.target as HTMLInputElement).value.trim(); solar({ max_elevation: raw === "" ? null : Number(raw) }, "Overhang saved"); }} /><span class="muted">° (eaves or a balcony; empty = none)</span></span></label>
        </div>
        <h4 class="lp-h">Obstructions</h4>
        <p class="small muted">A hill, a house or trees: between two directions the sun must be higher to reach this window.</p>
        ${w.solar.obstructions.map((o, i) => html`<div class="cu-obstruction" data-index=${i}><span class="small muted">From</span><input type="number" min="0" max="360" .value=${String(o.from_azimuth)} aria-label="From azimuth" ?disabled=${!can} @change=${(e: Event) => solar({ obstructions: w.solar.obstructions.map((x, j) => (j === i ? { ...x, from_azimuth: Number((e.target as HTMLInputElement).value) } : x)) }, "Obstruction saved")} />°
          <span class="small muted">to</span><input type="number" min="0" max="360" .value=${String(o.to_azimuth)} aria-label="To azimuth" ?disabled=${!can} @change=${(e: Event) => solar({ obstructions: w.solar.obstructions.map((x, j) => (j === i ? { ...x, to_azimuth: Number((e.target as HTMLInputElement).value) } : x)) }, "Obstruction saved")} />°
          <span class="small muted">needs above</span><input type="number" min="0" max="90" .value=${String(o.min_elevation)} aria-label="Minimum elevation" ?disabled=${!can} @change=${(e: Event) => solar({ obstructions: w.solar.obstructions.map((x, j) => (j === i ? { ...x, min_elevation: Number((e.target as HTMLInputElement).value) } : x)) }, "Obstruction saved")} />°
          ${can ? html`<button type="button" class="btn btn-small" aria-label="Remove obstruction" @click=${() => solar({ obstructions: w.solar.obstructions.filter((_, j) => j !== i) }, "Obstruction removed")}>${icon("delete", 16)}</button>` : nothing}</div>`)}
        ${!w.solar.obstructions.length ? html`<p class="muted small">None.</p>` : nothing}
        ${can && w.solar.obstructions.length < m.limits.obstructions ? html`<button type="button" class="btn btn-small" @click=${() => solar({ obstructions: [...w.solar.obstructions, { from_azimuth: Math.round(w.effective_bearing ?? 0), to_azimuth: Math.round((w.effective_bearing ?? 0) + 30) % 360, min_elevation: 10 }] }, "Obstruction added")}>${icon("plus", 16)}Add obstruction</button>` : nothing}
        <p class="small muted lp-gap">${sun.calibration.text}</p>
      </section>
      <section class="card" data-testid="curtains-window-readings"><header class="card-head">${icon("thermometer")}<h3>Measurements now</h3></header>
        ${w.mode === "off" || !env ? html`<p class="muted">This window is Off: nothing is evaluated.</p>` : html`
          <ul class="cu-readings">${readingRow("Outdoor light", r.outdoor_lux)}${r.indoor_lux ? readingRow("Indoor light", r.indoor_lux) : nothing}${readingRow("Room temperature", r.room_temp)}${readingRow("Outdoor temperature", r.outdoor_temp)}
            ${readingRow("Weather", r.weather ? { ...r.weather, unit: r.weather.value === null ? "" : "% cloud" } : null)}</ul>
          <p class="small muted">Only valid readings are used. Missing, stale, stuck or implausible readings never open a window, and a forecast never overrides a working light sensor.</p>
          ${kv([
            ["Direct sun", ds ? chip(DIRECT_TONE[ds.status] ?? "neutral", DIRECT_WORD[ds.status] ?? ds.status) : "—"],
            ["Room", html`${env.band_word ? chip(BAND_TONE[env.band ?? ""] ?? "neutral", env.band_word) : "—"}${c ? html` <span class="small muted">comfortable ${num1(c.low)}–${num1(c.high)} °C, max ${num1(c.top)} °C${c.source !== "setting" ? ` (thermostat ${c.source})` : ""}</span>` : nothing}`],
            ["Glare protection", env.glare.enabled ? `${env.glare.when === "occupied" ? "only while occupied" : "whenever there is direct sunshine"}${env.glare.active ? " · active" : ""}` : "off"],
            ["Sun evidence", env.sun_evidence], ["Decision", w.auto.decision?.condition_word ?? "—"],
          ])}
          ${ds?.text ? html`<p class="small">${ds.text}</p>` : nothing}
          ${th && th.state !== "off" ? html`<p class="small">${th.text}</p>` : nothing}
          ${!w.solar_protection ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>Sun protection is off for this window (Automatic tab): direct sunshine, temperature and glare do not move it.</span></p>` : nothing}`}
      </section>
      ${this.rulesForm(w, m, (g) => ENV_GROUPS.has(g), "Environment, temperature and glare")}
    </div>`;
  }

  private logTab(w: WindowModel, m: CurtainsModel): TemplateResult {
    const a = w.auto;
    const d = a.decision;
    return html`<div class="grid engine-grid">
      <section class="card" data-testid="curtains-window-stats"><header class="card-head">${icon("robot")}<h3>Automatic decisions</h3></header>
        ${kv([
          ["Condition", d ? html`${d.condition_word}${d.base !== d.condition ? html` <span class="small muted">(automatic: ${d.base_word})</span>` : nothing}` : "—"],
          ["Moves today", `${a.moves_today} of ${a.values.max_moves}`], ["Last move", fmtS(a.last_move_at)],
          ["Next check", a.next_evaluation ? fmtS(a.next_evaluation) : d?.next_check ? fmtS(d.next_check) : "—"],
          ["Morning since", fmtS(a.morning_since)], ["Manual hold", a.hold ? `${a.hold.policy_word} · ${fmtS(a.hold.created)}` : "None"],
          a.applied ? ["Applied by automatic control", JSON.stringify(a.applied)] : null,
        ])}
        ${d?.evidence?.length ? html`<p class="small muted">${d.evidence.join(" · ")}</p>` : nothing}
      </section>
      <section class="card wide" data-testid="curtains-window-log"><header class="card-head">${icon("history")}<h3>Decision log</h3><span class="small muted">${a.log.length}</span></header>
        ${a.log.length ? html`<ul class="cu-log">${a.log.map((x) => html`<li data-action=${x.action}><time datetime=${x.at ?? ""}>${fmtS(x.at)}</time><span>${chip(STATUS_TONE[x.action] ?? "neutral", x.word)}${x.mode && x.mode !== "automatic" ? html` <span class="small muted">${x.mode}</span>` : nothing} ${x.summary ?? x.text}${x.summary && x.summary !== x.text ? html`<span class="block small muted">${x.text}</span>` : nothing}</span></li>`)}</ul>` : html`<p class="muted">Nothing yet.</p>`}
      </section>
      <section class="card wide" data-testid="curtains-window-history"><header class="card-head">${icon("blinds")}<h3>Operations</h3><span class="small muted">${w.history.length}</span></header>
        ${w.history.length ? html`<ul class="cu-impact">${w.history.map((op) => html`<li class="cu-impact-row"><div class="cu-impact-head"><strong>${op.preset_name}</strong>${chip(OVERALL_TONE[op.status] ?? "neutral", m.words.overall[op.status] ?? op.status)}<span class="small muted">${fmtS(op.started)}${op.finished ? ` → ${fmtS(op.finished)}` : ""}${op.origin ? ` · ${op.origin}` : ""}</span></div>
          <div class="small">${op.explanation}</div>
          <div class="table-wrap"><table class="plain-table"><thead><tr><th scope="col">Cover</th><th scope="col">Target</th><th scope="col">Result</th><th scope="col">Detail</th><th scope="col">Start</th><th scope="col">Final</th></tr></thead>
            <tbody>${op.items.map((i) => html`<tr><td>${i.name}${i.service ? html`<span class="block small muted">cover.${i.service}</span>` : nothing}</td><td>${i.target === null ? "unchanged" : pct(i.target)}</td><td>${chip(RESULT_TONE[i.status] ?? "neutral", m.words.results[i.status] ?? i.status)}</td><td class="small">${i.detail}</td><td>${pct(i.start_position)}</td><td>${pct(i.final_position)}</td></tr>`)}</tbody></table></div></li>`)}</ul>` : html`<p class="muted">No preset applied yet.</p>`}
      </section>
    </div>`;
  }

  protected render(): unknown {
    const m = this.model;
    const w = this.w;
    const back = html`<div class="page-actions"><a class="btn" href="#/curtains" data-testid="curtains-back">${icon("arrow-left", 20)}Curtains</a></div>`;
    if (!m) return html`${pageHeader("Window", "", back)}${controlBanner(this.status)}${this.error ? problemNotice(this.error) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`}`;
    if (!w) return html`${pageHeader("Window not found", "This window does not exist (any more).", back)}${controlBanner(this.status)}`;
    const tab = (TABS.find((t) => t.key === this.tab)?.key ?? "overview") as Tab;
    const warnings = windowWarnings(w);
    const content = { overview: () => this.overviewTab(w, m), covers: () => this.coversTab(w, m), presets: () => this.presetsTab(w, m), automatic: () => this.automaticTab(w, m), environment: () => this.environmentTab(w, m), log: () => this.logTab(w, m) }[tab]();
    return html`${pageHeader(w.name, "", html`<div class="page-actions"><a class="btn" href="#/curtains">${icon("arrow-left", 20)}Curtains</a><a class="btn" href="#/curtains/control">${icon("swap", 20)}Control</a></div>`)}
      <div class="cu-head-sub">${modeChip(w.mode, w.mode_word)}<span class="muted">${w.area_name ?? "No room"} · ${w.compass ? `${w.compass}${w.effective_bearing !== null ? ` ${Math.round(w.effective_bearing)}°` : ""}` : "no direction"} · ${plural(w.layers.length, "cover")} · ${plural(w.presets.length, "preset")}</span>
        ${w.auto?.decision && w.mode !== "off" ? conditionChip(w.auto.decision) : nothing}${warnings.length ? chip("warning", plural(warnings.length, "warning")) : nothing}</div>
      ${controlBanner(this.status)}
      ${!this.canEdit && this.status ? adminNotice() : nothing}
      ${this.error && !this.apply && !this.enable && !this.sim && !this.presetDraft ? problemNotice(this.error) : nothing}
      ${this.notice ? html`<p class="notice" role="status">${icon("ok", 20)}<span>${this.notice}</span></p>` : nothing}
      <nav class="tabs lp-tabs" role="tablist" aria-label=${w.name}>${TABS.map((t) => html`<a role="tab" href=${this.href(t.key)} class=${tab === t.key ? "active" : ""} aria-selected=${tab === t.key ? "true" : "false"} data-tab=${t.key}>${icon(t.icon, 18)}<span>${t.title}</span></a>`)}</nav>
      <div class="tab-panel" role="tabpanel">${content}</div>
      ${this.applyDialog(w)}${this.enableDialog(w)}${this.simDialog()}${this.presetDialog(w)}`;
  }
}

customElements.define("sl-curtains-window", SlCurtainsWindow);
