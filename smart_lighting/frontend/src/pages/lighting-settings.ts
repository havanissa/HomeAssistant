// Lighting → Settings (Phase A4): what every room follows unless it sets its own value - manual changes, the
// global daylight sensor, ambient light categories and their tuning, possible-occupancy defaults, the Room
// Supervisor's defaults, the global brightness / colour temperature sources, shared spaces and brightness
// templates. Every change is one of the runtime's commands; room values are never changed by a global default.
import "../lighting/lighting-pages.css";
import "../lighting/lighting-levels";
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { chip, pageHeader } from "../ui";
import { cmd, lighting } from "../lighting/api";
import { controlBanner, LightingElement, problemNotice } from "../lighting/components";
import {
  advancedToggle,
  card,
  fmt,
  loadEditor,
  numberField,
  plural,
  problemItem,
  segments,
  selectField,
  SUP_LIMITS,
  toggleField,
  type EditorModel,
  type Obj,
  type RoomModel,
} from "../lighting/lighting-model";

type Tab = "general" | "daylight" | "ambient" | "possible" | "supervisor" | "adaptive" | "spaces" | "templates";
const TABS: Array<{ key: Tab; title: string; icon: string; sub: string }> = [
  { key: "general", title: "General", icon: "cog", sub: "Manual changes, exclusions, moods" },
  { key: "daylight", title: "Daylight", icon: "compass", sub: "Outdoor light sensor" },
  { key: "ambient", title: "Ambient levels", icon: "brightness", sub: "Categories and stability" },
  { key: "possible", title: "Possible occupancy", icon: "motion-sensor", sub: "Dim light defaults" },
  { key: "supervisor", title: "Room Supervisor", icon: "shield-check", sub: "Checks and corrections" },
  { key: "adaptive", title: "Global sources", icon: "sync", sub: "Brightness and colour temperature" },
  { key: "spaces", title: "Shared spaces", icon: "open-plan", sub: "Open-plan rooms" },
  { key: "templates", title: "Brightness templates", icon: "grid", sub: "Reusable matrices" },
];

export class SlLightingSettings extends LightingElement {
  static properties = {
    ...LightingElement.properties, areaId: { type: String }, tab: { type: String },
    model: { state: true }, ref: { state: true }, adv: { state: true }, space: { state: true }, ask: { state: true }, rename: { state: true },
  };
  declare areaId: string;
  declare tab: string;
  declare model: EditorModel | null;
  /** A room's model: the global defaults that the integration reports only with a room (ambient, supervisor …). */
  declare ref: RoomModel | null;
  declare adv: Set<string>;
  declare space: Obj | null;
  declare ask: string | null;
  declare rename: { id: string; name: string } | null;

  constructor() {
    super();
    this.tab = "general";
    this.model = null;
    this.ref = null;
    this.adv = new Set();
    this.space = null;
    this.ask = null;
    this.rename = null;
  }

  async reload(): Promise<void> {
    try {
      const model = await loadEditor();
      this.model = model;
      const first = model.rooms[0]?.area_id;
      this.ref = first ? (await loadEditor(first)).room ?? null : null;
    } catch (err) {
      this.error = err;
    }
  }

  private send(name: string, params: Obj, done = "Saved"): Promise<boolean> {
    return this.run(() => lighting(cmd(name), params), done);
  }

  private toggleAdv(key: string): void {
    const n = new Set(this.adv);
    if (n.has(key)) n.delete(key);
    else n.add(key);
    this.adv = n;
  }

  private get dis(): boolean {
    return !this.canEdit || this.busy;
  }

  private needsRoom(): TemplateResult {
    return html`<p class="notice notice-info">${icon("info", 20)}<span>These defaults are shown once a room uses Smart Comfort lighting: enable a room in the <a href="#/rooms">Rooms editor</a>.</span></p>`;
  }

  // ---------------------------------------------------------------- general
  private renderGeneral(m: EditorModel): TemplateResult {
    const s = m.settings ?? {};
    const set = (change: Obj, done = "Saved") => void this.send("settings/set", change, done);
    const moods = m.moods ?? [];
    const handled = moods.filter((x: Obj) => x.handling);
    const open = this.adv.has("general");
    return html`<div class="tab-grid lp-tab-grid">
      ${card("Manual changes", "tap", html`
        ${numberField({ label: "Respect a manual change for", value: s.manual_override_minutes, unit: "min", min: 0, max: 1440, disabled: this.dis,
          help: "A light changed by a person or another automation is left alone this long (0: until the room is vacant). Rooms can set their own value.",
          onChange: (v) => v !== null && set({ manual_override_minutes: v }) })}
        ${selectField({ label: "Changes without a known source", value: s.unattributed, disabled: this.dis,
          options: [["ignore", "Ignore (not a manual change)"], ["manual", "Treat as a manual change"]],
          help: "A light change that Home Assistant cannot attribute to a person or an automation.", onChange: (v) => set({ unattributed: v }) })}`, { testid: "lighting-settings-manual" })}
      ${card("Lights and moods", "palette", html`
        ${selectField({ label: "Never manage lights with the label", value: s.exclude_label ?? "", disabled: this.dis,
          options: [["", "No exclusion label"], ...(m.labels ?? []).map((l) => [l.label_id, l.name] as [string, string])],
          help: "Home Assistant labels; a light with this label is never managed in any room.", onChange: (v) => set({ exclude_label: v || null }) })}
        ${selectField({ label: "Fallback mood", value: s.fallback_mood ?? "", disabled: this.dis,
          options: [["", "None"], ...moods.map((x) => [x.name, x.name] as [string, string])],
          help: "Used when the mood source reports no mood.", onChange: (v) => set({ fallback_mood: v || null }) })}
        ${advancedToggle(open, () => this.toggleAdv("general"))}
        ${open ? html`
          <label class="lp-field"><span class="lp-label">Manual option (pauses automatic lighting)</span>
            <input type="text" maxlength="100" .value=${s.manual_option ?? ""} ?disabled=${this.dis} @change=${(e: Event) => set({ manual_option: (e.target as HTMLInputElement).value })} />
            <span class="small muted">The Main Mood option that means "Manual control".</span></label>
          <h4 class="lp-h">Moods that are not Main Mood options</h4>
          ${handled.length ? handled.map((x: Obj) => {
            const h = x.handling ?? { mode: "follow_main" };
            return html`<div class="lp-form">${selectField({ label: x.name, value: h.mode, disabled: this.dis,
                options: [["follow_main", "Keep following Main Mood"], ["no_mood", "No mood (lighting changes nothing)"], ["use_mood", "Use another mood"]],
                onChange: (v) => { if (v !== "use_mood") set({ unrepresentable: { [x.name]: { mode: v } } }); else if (!this.adv.has(`um-${x.name}`)) this.toggleAdv(`um-${x.name}`); } })}
              ${h.mode === "use_mood" || this.adv.has(`um-${x.name}`) ? selectField({ label: "Use the mood", value: h.mood ?? "", disabled: this.dis,
                options: [["", "Choose…"], ...moods.filter((y) => y.name !== x.name).map((y) => [y.name, y.name] as [string, string])],
                onChange: (v) => v && set({ unrepresentable: { [x.name]: { mode: "use_mood", mood: v } } }) }) : nothing}</div>`;
          }) : html`<p class="small muted">Every mood is a Main Mood option.</p>`}` : nothing}`, { testid: "lighting-settings-moods" })}
      ${(m.problems ?? []).length ? card("Lighting problems", "warning", html`<ul class="lp-problems">${m.problems.map(problemItem)}</ul>`, { wide: true }) : nothing}
    </div>`;
  }

  // --------------------------------------------------------------- daylight
  private renderDaylight(m: EditorModel): TemplateResult {
    const g = m.daylight ?? {};
    const cands: Obj[] = g.candidates ?? [];
    const set = (change: Obj) => void this.send("daylight/set", change);
    return html`<div class="tab-grid lp-tab-grid">${card("Outdoor light", "compass", html`
      <p class="small muted">An optional outdoor light sensor, shared by every room: it estimates the daylight when a room has no usable indoor reading, and calibrates indoor sensors. Read only: it is never changed.</p>
      ${selectField({ label: "Outdoor light sensor", value: g.outdoor_entity ?? "", disabled: this.dis,
        options: [["", "None — sun position only"], ...(g.outdoor_entity && !cands.some((c) => c.entity_id === g.outdoor_entity) ? [[g.outdoor_entity, `${g.outdoor_entity} (not found)`] as [string, string]] : []),
          ...cands.map((c) => [c.entity_id, `${c.name}${c.value !== null && c.value !== undefined ? ` · ${c.value} lx` : ""}${c.outdoor ? " · outdoor" : c.area ? ` (${c.area})` : ""}`] as [string, string])],
        onChange: (v) => set({ outdoor_entity: v || null }) })}
      ${g.outdoor_exists === false ? html`<p class="small tone-warning">The chosen sensor does not exist.</p>` : nothing}
      ${numberField({ label: "Dark below", value: g.outdoor_dark_lux, unit: "lx", min: 0, max: 5000, disabled: this.dis, help: "Outdoor light below this counts as dark.", onChange: (v) => v !== null && set({ outdoor_dark_lux: v }) })}
      ${numberField({ label: "Smoothing window", value: g.outdoor_window, unit: "s", min: 0, max: 3600, disabled: this.dis, help: "Short clouds and shadows are averaged over this time.", onChange: (v) => v !== null && set({ outdoor_window: v }) })}
      <p class="small muted">Each room's indoor sensors and darkness threshold are set in its Automatic tab.</p>`, { testid: "lighting-settings-daylight" })}</div>`;
  }

  // ---------------------------------------------------------------- ambient
  private renderAmbient(): TemplateResult {
    const b = this.ref?.brightness;
    if (!b) return this.needsRoom();
    const a = b.ambient ?? {};
    const g = a.global ?? {};
    const lim = b.limits ?? {};
    const set = (change: Obj) => void this.send("ambient/set", change);
    const num = (k: string, label: string, unit: string, max: number, step: number, help: string) =>
      numberField({ label, value: g[k], unit, min: 0, max, step, disabled: this.dis, help, onChange: (v) => v !== null && set({ [k]: v }) });
    const levels: Obj[] = (a.global_levels ?? []).map((l: Obj) => ({ key: l.id, ...l }));
    return html`<div class="tab-grid lp-tab-grid">
      ${card("Global categories", "layers", html`
        <p class="small muted">Used by every room without its own categories${(a.global_rooms ?? []).length ? ` (now ${a.global_rooms.join(", ")})` : ""}.</p>
        <sl-lp-levels .areaId=${null} .ambient=${a} .levels=${levels} .limits=${lim} .host=${this} .busy=${this.busy} ?canEdit=${this.canEdit}></sl-lp-levels>
        ${numberField({ label: "Most categories a room may have", value: a.max_levels, min: lim.levels?.[0] ?? 2, max: lim.max_levels ?? 24, disabled: this.dis,
          onChange: (v) => v !== null && void this.send("ambient/levels", { max_levels: v }) })}`, { testid: "lighting-settings-levels" })}
      ${card("Stability, fallback and fades", "tune", html`
        <p class="small muted">Global defaults: every room that does not set its own value follows them (Brightness tab of a room).</p>
        <div class="lp-form">
          ${num("hysteresis", "Hysteresis", "lx", lim.hysteresis?.[1] ?? 200, 0.5, "A category is left downwards only this far below its threshold.")}
          ${num("stable_seconds", "Stable for", "s", lim.stable?.[1] ?? 3600, 10, "A new category counts after it held this long.")}
          ${selectField({ label: "When uncertain", value: g.fallback, disabled: this.dis, options: [["hold", "Keep the last reliable category"], ["level", "Use the fallback category"]], onChange: (v) => set({ fallback: v }) })}
          ${selectField({ label: "Fallback category", value: g.fallback_level, disabled: this.dis, options: levels.map((l) => [l.id, l.name] as [string, string]), onChange: (v) => set({ fallback_level: v }) })}
          ${num("transition", "Brightness fade", "s", lim.transition?.[1] ?? 60, 0.5, "Uses the light's own transition when it supports one.")}
          ${num("dead_band", "Dead band", "%", lim.dead_band?.[1] ?? 25, 1, "Smaller brightness differences are not sent.")}
          ${num("min_interval", "Between changes", "s", lim.min_interval?.[1] ?? 3600, 30, "At least this long between two changes caused by the light level.")}
        </div>`, { testid: "lighting-settings-ambient" })}
    </div>`;
  }

  // --------------------------------------------------------------- possible
  private renderPossible(): TemplateResult {
    const p = this.ref?.preliminary;
    if (!p) return this.needsRoom();
    const g = p.global ?? {};
    const lim = p.limits ?? {};
    const set = (change: Obj) => void this.send("preliminary/set", change);
    return html`<div class="tab-grid lp-tab-grid">${card("Possible occupancy defaults", "motion-sensor", html`
      <p class="small muted">A dim light at the first sign of someone while occupancy is being confirmed. Every room that does not set a value follows these; room values are not changed. A room changes nothing until it has a preliminary light (its Automatic tab).</p>
      ${toggleField({ label: "Enabled by default", checked: !!g.enabled, disabled: this.dis, onChange: (v) => set({ enabled: v }) })}
      <div class="lp-form">
        ${numberField({ label: "Brightness", value: g.brightness_pct, unit: "%", min: 1, max: 100, disabled: this.dis, onChange: (v) => v !== null && set({ brightness_pct: v }) })}
        ${numberField({ label: "Fade", value: g.transition, unit: "s", min: 0, max: lim.transition?.[1] ?? 300, step: 0.1, disabled: this.dis, onChange: (v) => v !== null && set({ transition: v }) })}
        ${numberField({ label: "Timeout", value: g.timeout, unit: "s", min: lim.timeout?.[0] ?? 5, max: lim.timeout?.[1] ?? 600, disabled: this.dis, help: "Switched off this long after it started unless confirmed.", onChange: (v) => v !== null && set({ timeout: v }) })}
        ${numberField({ label: "On below", value: g.on_below, placeholder: "same as normal", unit: "lx", min: lim.lux?.[0] ?? 1, max: lim.lux?.[1] ?? 5000, disabled: this.dis, help: "Empty: each room's normal darkness threshold.", onChange: (v) => set({ on_below: v }) })}
      </div>
      <div class="lp-field"><span class="lp-label">When occupancy is confirmed</span>${segments<string>(g.on_confirm ?? "handover", [["handover", "Hand over", "swap"], ["turn_off", "Turn off", "power"]], (v) => set({ on_confirm: v }), { disabled: this.dis, label: "Default when occupancy is confirmed" })}</div>`,
      { testid: "lighting-settings-possible" })}</div>`;
  }

  // ------------------------------------------------------------- supervisor
  private renderSupervisor(): TemplateResult {
    const s = this.ref?.supervisor;
    if (!s) return this.needsRoom();
    const g = s.global_settings ?? {};
    const set = (change: Obj) => void this.send("supervisor/set", change);
    return html`<div class="tab-grid lp-tab-grid">${card("Room Supervisor defaults", "shield-check", html`
      <p class="small muted">Checks that every light does what its winning rule requests. Observing reports differences only; Automatic also corrects a device that did not follow a command. A person's change is never corrected. Rooms can set their own values (Supervisor tab).</p>
      ${segments<string>(g.mode, [["observing", "Observing", "eye"], ["automatic", "Automatic", "sync"], ["off", "Off", "power"]], (v) => set({ mode: v }), { disabled: this.dis, label: "Global Room Supervisor mode", testid: "lighting-settings-sup-mode" })}
      ${selectField({ label: "Changes by automations", value: String(!!g.correct_external), disabled: this.dis, options: [["false", "Report only"], ["true", "Correct once, then report a conflict"]], onChange: (v) => set({ correct_external: v === "true" }) })}
      ${advancedToggle(this.adv.has("sup"), () => this.toggleAdv("sup"), "Timing and tolerances")}
      ${this.adv.has("sup") ? html`<div class="lp-form">${SUP_LIMITS.map(([k, label, unit]) => numberField({ label, value: g[k], unit, disabled: this.dis, onChange: (v) => v !== null && set({ [k]: v }) }))}</div>` : nothing}`,
      { testid: "lighting-settings-supervisor" })}</div>`;
  }

  // --------------------------------------------------------------- adaptive
  private renderAdaptive(m: EditorModel): TemplateResult {
    const a = m.adaptive ?? {};
    const c = a.config ?? {};
    const v = a.values ?? {};
    const cands = a.candidates ?? { brightness: [], color_temp: [] };
    const set = (change: Obj) => void this.send("adaptive/set", change);
    const source = (kind: "brightness" | "color_temp", label: string, scales: Array<[string, string]>) => {
      const entity = c[`${kind}_entity`];
      const list: Obj[] = cands[kind] ?? [];
      const val = v[kind] ?? {};
      return html`<div class="lp-subcard"><h4 class="lp-h">${label}</h4>
        ${selectField({ label: "Source entity", value: entity ?? "", disabled: this.dis,
          options: [["", "Not used"], ...(entity && !list.some((x) => x.entity_id === entity) ? [[entity, entity] as [string, string]] : []),
            ...list.map((x) => [x.entity_id, `${x.name} · ${x.value} ${x.unit ?? ""}${x.likely ? "" : " (less likely)"}`] as [string, string])],
          onChange: (e) => set({ [`${kind}_entity`]: e || null }) })}
        ${entity ? html`<p class="small">${chip(val.status === "connected" ? "ok" : val.status === "not_configured" ? "neutral" : "warning", val.status ?? "—")}
          ${val.value !== null && val.value !== undefined ? html` Now <strong>${Math.round(val.value)} ${kind === "brightness" ? "%" : "K"}</strong>` : nothing}
          <span class="muted">${val.detail ?? ""}${val.updated_at ? ` · ${fmt(val.updated_at)}` : ""}</span></p>
          ${this.adv.has("adaptive") ? html`<div class="lp-form">
            <label class="lp-field"><span class="lp-label">Attribute</span><input type="text" maxlength="64" .value=${c[`${kind}_attribute`] ?? ""} placeholder="(the state)" ?disabled=${this.dis}
              @change=${(e: Event) => set({ [`${kind}_attribute`]: (e.target as HTMLInputElement).value.trim() || null })} /></label>
            ${selectField({ label: "Unit", value: c[`${kind}_scale`], disabled: this.dis, options: scales, onChange: (s) => set({ [`${kind}_scale`]: s }) })}</div>` : nothing}` : nothing}
      </div>`;
    };
    return html`<div class="tab-grid lp-tab-grid">${card("Global brightness and colour temperature", "sync", html`
      <p class="small muted">Existing outputs (for example Adaptive Lighting or a helper) that profiles can follow ("Follow global"). They are only read, never changed.</p>
      ${source("brightness", "Brightness", [["auto", "Automatic"], ["percent", "Percent (0–100)"], ["byte", "0–255"]])}
      ${source("color_temp", "Colour temperature", [["auto", "Automatic"], ["kelvin", "Kelvin"], ["mired", "Mired"]])}
      ${advancedToggle(this.adv.has("adaptive"), () => this.toggleAdv("adaptive"), "Attributes, units and update steps")}
      ${this.adv.has("adaptive") ? html`<div class="lp-form">
        ${numberField({ label: "At most every", value: c.min_interval, unit: "s", min: 0, max: 3600, disabled: this.dis, help: "Between two adjustments of a light that follows a global value.", onChange: (x) => x !== null && set({ min_interval: x }) })}
        ${numberField({ label: "Brightness step", value: c.brightness_step, unit: "%", min: 1, max: 50, disabled: this.dis, help: "Smaller changes are not sent.", onChange: (x) => x !== null && set({ brightness_step: x }) })}
        ${numberField({ label: "Colour temperature step", value: c.kelvin_step, unit: "K", min: 10, max: 2000, disabled: this.dis, onChange: (x) => x !== null && set({ kelvin_step: x }) })}
        ${numberField({ label: "Fade", value: c.transition, unit: "s", min: 0, max: 300, step: 0.5, disabled: this.dis, onChange: (x) => x !== null && set({ transition: x }) })}
      </div>` : nothing}
      ${(a.controllers ?? []).length ? html`<h4 class="lp-h">Detected controllers</h4><ul class="plain-list">${a.controllers.map((x: Obj) => html`<li class="small">${x.name ?? x.entity_id ?? JSON.stringify(x)}</li>`)}</ul>` : nothing}`,
      { testid: "lighting-settings-adaptive" })}</div>`;
  }

  // ----------------------------------------------------------------- spaces
  private renderSpaces(m: EditorModel): TemplateResult {
    const z = this.ref?.zones;
    if (!z) return this.needsRoom();
    const spaces: Obj[] = z.spaces ?? [];
    const names = Object.fromEntries((m.rooms ?? []).map((r) => [r.area_id, r.name]));
    const s = this.space;
    const moods = (m.moods ?? []).map((x) => x.name);
    const editor = s ? html`<div class="lp-subcard" data-testid="lighting-space-editor"><h4 class="lp-h">${s.id ? `Edit ${s.name}` : "New shared space"}</h4>
      <div class="lp-form lp-form-1">
        <label class="lp-field"><span class="lp-label">Name</span><input type="text" maxlength="60" .value=${s.name ?? ""} @input=${(e: Event) => (this.space = { ...s, name: (e.target as HTMLInputElement).value })} /></label>
        <div class="lp-field"><span class="lp-label">Rooms (a room belongs to at most one space)</span><ul class="plain-list">${(m.rooms ?? []).map((r) => {
          const other = spaces.find((x) => x.id !== s.id && (x.rooms ?? []).includes(r.area_id));
          return html`<li><label class="lp-check"><input type="checkbox" ?disabled=${!!other} .checked=${(s.rooms ?? []).includes(r.area_id)}
              @change=${(e: Event) => (this.space = { ...s, rooms: (e.target as HTMLInputElement).checked ? [...(s.rooms ?? []), r.area_id] : (s.rooms ?? []).filter((a: string) => a !== r.area_id) })} />
            <span>${r.name}${other ? html` <span class="small muted">in ${other.name}</span>` : nothing}</span></label></li>`;
        })}</ul></div>
        ${selectField({ label: "Mood", value: s.mood_override ?? "", options: [["", "Each room's own mood"], ...moods.map((x) => [x, x] as [string, string])], onChange: (v) => (this.space = { ...s, mood_override: v || null }) })}
        ${selectField({ label: "When a room is vacant", value: s.vacancy ?? "independent", options: [["independent", "It runs its own vacancy action"], ["together", "It waits until the whole space is vacant"]], onChange: (v) => (this.space = { ...s, vacancy: v }) })}
      </div>
      <div class="row-actions lp-row"><button type="button" class="btn" @click=${() => (this.space = null)}>Cancel</button>
        <button type="button" class="btn btn-primary" ?disabled=${this.dis || !String(s.name ?? "").trim()} @click=${async () => {
          if (await this.send("space/set", { space: { ...(s.id ? { id: s.id } : {}), name: String(s.name).trim(), rooms: s.rooms ?? [], mood_override: s.mood_override ?? null, vacancy: s.vacancy ?? "independent" } }, "Shared space saved")) this.space = null; }}>${icon("save", 18)}Save</button></div></div>` : nothing;
    return html`<div class="tab-grid lp-tab-grid">${card("Shared spaces", "open-plan", html`
      <p class="small muted">Rooms that form one open-plan space (for example a kitchen and a living room). Each room keeps its own occupancy, zones and lights; a light both rooms control is never switched off by one while the other is occupied.</p>
      ${spaces.length ? html`<ul class="plain-list">${spaces.map((x) => html`<li class="lp-subcard"><div class="lp-row-line"><strong>${x.name}</strong><span class="small muted">${(x.rooms ?? []).map((a: string) => names[a] ?? a).join(", ") || "no rooms"}</span>
          <span class="spacer"></span>${this.canEdit ? html`<button type="button" class="btn btn-small" @click=${() => (this.space = { ...x })}>${icon("pencil", 16)}Edit</button>
          ${this.ask === `space:${x.id}` ? html`<button type="button" class="btn btn-small btn-danger" @click=${async () => { if (await this.send("space/remove", { space_id: x.id }, "Shared space removed")) this.ask = null; }}>Remove ${x.name}</button>
              <button type="button" class="btn btn-small" @click=${() => (this.ask = null)}>Cancel</button>`
            : html`<button type="button" class="btn btn-small" @click=${() => (this.ask = `space:${x.id}`)}>${icon("delete", 16)}Remove…</button>`}` : nothing}</div>
          <span class="small muted">Mood: ${x.mood_override ?? "each room's own"} · ${x.vacancy === "together" ? "vacant together" : "each room vacates on its own"}</span></li>`)}</ul>`
        : html`<p class="muted">No shared space.</p>`}
      ${editor}
      ${!s && this.canEdit ? html`<button type="button" class="btn btn-small" data-testid="lighting-space-add" @click=${() => (this.space = { name: "Shared space", rooms: [], vacancy: "independent" })}>${icon("plus", 16)}Add a shared space</button>` : nothing}`,
      { testid: "lighting-settings-spaces" })}</div>`;
  }

  // -------------------------------------------------------------- templates
  private renderTemplates(): TemplateResult {
    const b = this.ref?.brightness;
    if (!b) return this.needsRoom();
    const templates: Obj[] = b.templates ?? [];
    const pct = (v: unknown) => (v === null || v === undefined ? "—" : v === 0 ? "Off" : `${v} %`);
    return html`<div class="tab-grid lp-tab-grid">${card("Brightness templates", "grid", html`
      <p class="small muted">Starting points for a light's brightness matrix: a Room occupied column and a zone column per ambient category. Built-in templates cannot change; save your own from a light's matrix (room → Brightness → Save as template). At most ${b.limits?.max_templates ?? 24}.</p>
      <div class="lp-template-grid">${templates.map((t) => {
        const levels: Obj[] = t.levels ?? [];
        return html`<div class="lp-subcard" data-template=${t.id}>
          <div class="lp-row-line"><strong>${t.name}</strong>${t.builtin ? chip("neutral", "built-in") : chip("info", "saved")}${t.id === b.default_template ? chip("ok", "default") : nothing}</div>
          <table class="plain-table lp-table lp-mini"><thead><tr><th scope="col">Category</th><th scope="col">Room</th><th scope="col">Zone</th></tr></thead>
            <tbody>${levels.map((l) => html`<tr><th scope="row">${l.name}</th><td>${pct(t.room?.[l.id])}</td><td>${pct(t.zone?.[l.id])}</td></tr>`)}</tbody></table>
          ${!t.builtin && this.canEdit ? html`<div class="row-actions lp-row">
            ${this.rename?.id === t.id ? html`<input type="text" maxlength="60" .value=${this.rename!.name} aria-label="Template name" @input=${(e: Event) => (this.rename = { id: t.id, name: (e.target as HTMLInputElement).value })} />
                <button type="button" class="btn btn-small btn-primary" ?disabled=${this.dis || !this.rename!.name.trim()} @click=${async () => { if (await this.send("brightness/template", { op: "save", template_id: t.id, name: this.rename!.name.trim() }, "Template renamed")) this.rename = null; }}>Save</button>
                <button type="button" class="btn btn-small" @click=${() => (this.rename = null)}>Cancel</button>`
              : html`<button type="button" class="btn btn-small" @click=${() => (this.rename = { id: t.id, name: t.name })}>${icon("pencil", 16)}Rename</button>`}
            ${this.ask === `tpl:${t.id}` ? html`<button type="button" class="btn btn-small btn-danger" @click=${async () => { if (await this.send("brightness/template", { op: "delete", template_id: t.id }, "Template deleted")) this.ask = null; }}>Delete</button>
                <button type="button" class="btn btn-small" @click=${() => (this.ask = null)}>Cancel</button>`
              : html`<button type="button" class="btn btn-small" @click=${() => (this.ask = `tpl:${t.id}`)}>${icon("delete", 16)}Delete…</button>`}</div>` : nothing}
        </div>`;
      })}</div>`, { wide: true, testid: "lighting-settings-templates" })}</div>`;
  }

  protected render(): unknown {
    const header = pageHeader("Lighting settings", "What every room follows unless it sets its own value.",
      html`<div class="page-actions"><a class="btn" href="#/lighting">${icon("arrow-left", 20)}Lighting</a></div>`);
    const m = this.model;
    if (!m) return html`${header}${controlBanner(this.status)}${this.error ? problemNotice(this.error) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`}`;
    const tab = (TABS.some((t) => t.key === this.tab) ? this.tab : "general") as Tab;
    let content: TemplateResult;
    switch (tab) {
      case "daylight": content = this.renderDaylight(m); break;
      case "ambient": content = this.renderAmbient(); break;
      case "possible": content = this.renderPossible(); break;
      case "supervisor": content = this.renderSupervisor(); break;
      case "adaptive": content = this.renderAdaptive(m); break;
      case "spaces": content = this.renderSpaces(m); break;
      case "templates": content = this.renderTemplates(); break;
      default: content = this.renderGeneral(m);
    }
    const current = TABS.find((t) => t.key === tab)!;
    return html`${header}${controlBanner(this.status)}
      ${!this.canEdit ? html`<p class="notice" role="status">${icon("info", 20)}<span>Read only: only Home Assistant administrators can change lighting settings.</span></p>` : nothing}
      ${this.error ? problemNotice(this.error) : nothing}
      ${this.notice ? html`<p class="notice notice-info" role="status" data-testid="lighting-saved">${icon("ok", 20)}<span>${this.notice}</span></p>` : nothing}
      <div class="lp-settings">
        <nav class="lp-settings-nav" aria-label="Lighting settings">${TABS.map((t) => html`<a href=${`#/lighting/settings/${t.key}`} class=${t.key === tab ? "active" : ""} aria-current=${t.key === tab ? "page" : "false"} data-tab=${t.key}>
          ${icon(t.icon, 20)}<span><span class="lp-nav-title">${t.title}</span><span class="block small muted">${t.sub}</span></span></a>`)}</nav>
        <section class="lp-settings-main" aria-label=${current.title}>
          <h3 class="lp-settings-title">${current.title}</h3>
          ${content}
          ${this.ref ? nothing : html`<p class="small muted">${plural(m.rooms.length, "room")} use Smart Comfort lighting.</p>`}
        </section>
      </div>`;
  }
}

customElements.define("sl-lighting-settings", SlLightingSettings);
