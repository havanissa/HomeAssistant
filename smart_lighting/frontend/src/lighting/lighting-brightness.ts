// Room → Brightness (0.6.7/0.6.8): the room's ambient light levels and each light's brightness matrix
// (categories × Room occupied / zones). A draft matrix is previewed by the runtime (``brightness/preview``) before it
// is saved; enrolling a light needs that preview and ``confirm``. Saving never sends a command.
import "./lighting-levels";
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { chip, kv, StoreElement } from "../ui";
import { cmd, lighting } from "./api";
import { inspectButton } from "./light-inspector";
import {
  AMB_BASIS,
  advancedToggle,
  card,
  fmt,
  inheritTag,
  numberField,
  OCC,
  selectField,
  stateText,
  summary,
  toggleField,
  type LightingHost,
  type Obj,
  type RoomModel,
} from "./lighting-model";

type Cells = Record<string, number | null>;
/** App 0.9.0: per-cell delays in seconds, column ("room" or a zone id) → category → {on, off} (absent = 0 = immediate). */
type Delays = Record<string, Record<string, { on: number; off: number }>>;
interface MatrixDraft { entity_id: string; enabled: boolean; room: Cells; zones: Record<string, Cells>; delays: Delays; template: string | null; dirty: boolean; enrolled: boolean }
const DELAY_MAX = 3600;

function cleanDelays(d: Delays, values: (col: string, level: string) => number | null | undefined): Delays {
  const out: Delays = {};
  for (const [col, levels] of Object.entries(d ?? {})) {
    for (const [level, p] of Object.entries(levels ?? {})) {
      const on = Math.max(0, Math.min(DELAY_MAX, Math.round(Number(p?.on) || 0)));
      const off = Math.max(0, Math.min(DELAY_MAX, Math.round(Number(p?.off) || 0)));
      if ((on || off) && (Number(values(col, level)) || 0) > 0) (out[col] ??= {})[level] = { on, off };
    }
  }
  return out;
}

/** The delays to send: every cell of the draft, null where none (so a removed delay is removed). */
export function delaysPayload(d: { room: Cells; zones: Record<string, Cells>; delays: Delays }): Record<string, Record<string, { on: number; off: number } | null>> {
  const out: Record<string, Record<string, { on: number; off: number } | null>> = {};
  const cols: Array<[string, Cells]> = [["room", d.room], ...Object.entries(d.zones)];
  for (const [col, cells] of cols) {
    out[col] = {};
    for (const level of Object.keys(cells)) {
      const p = d.delays?.[col]?.[level];
      out[col][level] = p && (p.on || p.off) && (cells[level] ?? 0) > 0 ? { on: p.on, off: p.off } : null;
    }
  }
  return out;
}

export function delayText(p: { on?: number; off?: number } | null | undefined): string {
  if (!p || (!p.on && !p.off)) return "";
  return [p.on ? `Delay On ${p.on} s` : null, p.off ? `Delay Off ${p.off} s` : null].filter(Boolean).join(" · ");
}

export class SlLpBrightness extends StoreElement {
  static properties = {
    room: { attribute: false }, host: { attribute: false }, busy: { type: Boolean }, canEdit: { type: Boolean },
    sel: { state: true }, draft: { state: true }, cell: { state: true }, preview: { state: true }, previewError: { state: true },
    adv: { state: true }, askRemove: { state: true }, templateName: { state: true },
  };
  declare room: RoomModel;
  declare host: LightingHost;
  declare busy: boolean;
  declare canEdit: boolean;
  declare sel: string | null;
  declare draft: MatrixDraft | null;
  declare cell: { col: string; level: string } | null;
  declare preview: Obj | null;
  declare previewError: string | null;
  declare adv: boolean;
  declare askRemove: boolean;
  declare templateName: string | null;
  private timer: number | undefined;

  constructor() {
    super();
    this.sel = null;
    this.draft = null;
    this.cell = null;
    this.preview = null;
    this.previewError = null;
    this.adv = false;
    this.askRemove = false;
    this.templateName = null;
  }

  private get b(): Obj {
    return this.room.brightness ?? {};
  }

  private send(name: string, params: Obj, done = "Saved"): Promise<boolean> {
    return this.host.run(() => lighting(cmd(name), { area_id: this.room.area_id, ...params }), done);
  }

  private emptyCells(): Cells {
    return Object.fromEntries((this.b.levels ?? []).map((l: Obj) => [l.key, null]));
  }

  private select(entityId: string | null): void {
    this.sel = entityId;
    this.cell = null;
    this.askRemove = false;
    this.templateName = null;
    this.preview = null;
    this.previewError = null;
    this.draft = null;
    if (!entityId) return;
    const l = (this.b.lights ?? []).find((x: Obj) => x.entity_id === entityId);
    if (!l) return;
    const zones: string[] = l.columns.filter((c: Obj) => c.id !== "room").map((c: Obj) => c.id);
    let m = l.matrix;
    if (!m) {
      const t = (this.b.templates ?? []).find((x: Obj) => x.id === this.b.default_template) ?? this.b.templates?.[0] ?? { room: {}, zone: {} };
      m = { enabled: true, room: { ...this.emptyCells(), ...t.room }, zones: Object.fromEntries(zones.map((z) => [z, { ...this.emptyCells(), ...t.zone }])), template: t.id ?? null,
        delays: this.templateDelays(t, zones) };
    }
    this.draft = {
      entity_id: entityId, enabled: m.enabled !== false, room: { ...this.emptyCells(), ...m.room }, template: m.template ?? null,
      zones: Object.fromEntries(zones.map((z) => [z, { ...this.emptyCells(), ...((m.zones ?? {})[z] ?? {}) }])), dirty: !l.matrix, enrolled: !!l.matrix,
      delays: JSON.parse(JSON.stringify(m.delays ?? {})),
    };
    this.loadPreview();
  }

  protected willUpdate(changed: Map<PropertyKey, unknown>): void {
    // After a save the room model is new: a clean draft follows the stored matrix again.
    if (changed.has("room") && this.sel && this.draft && !this.draft.dirty) {
      const keep = this.cell;
      this.select(this.sel);
      this.cell = keep;
    }
    if (changed.has("room") && this.sel && !(this.b.lights ?? []).some((x: Obj) => x.entity_id === this.sel)) this.select(null);
  }

  private templateDelays(t: Obj, zones: string[]): Delays {
    const td = t?.delays ?? {};
    const out: Delays = {};
    if (td.room && Object.keys(td.room).length) out.room = JSON.parse(JSON.stringify(td.room));
    if (td.zone && Object.keys(td.zone).length) for (const z of zones) out[z] = JSON.parse(JSON.stringify(td.zone));
    return out;
  }

  private update_(patch: Partial<MatrixDraft>): void {
    if (!this.draft) return;
    const next = { ...this.draft, ...patch, dirty: true };
    // A delay belongs to an On cell: a cell set to Off or Not configured loses its delays.
    next.delays = cleanDelays(next.delays, (col, level) => (col === "room" ? next.room[level] : next.zones[col]?.[level]));
    this.draft = next;
    this.loadPreview();
  }

  private setDelay(kind: "on" | "off", seconds: number | null): void {
    const d = this.draft;
    const c = this.cell;
    if (!d || !c) return;
    const cur = d.delays?.[c.col]?.[c.level] ?? { on: 0, off: 0 };
    const delays: Delays = JSON.parse(JSON.stringify(d.delays ?? {}));
    (delays[c.col] ??= {})[c.level] = { ...cur, [kind]: Math.max(0, Math.min(DELAY_MAX, Math.round(seconds ?? 0))) };
    this.update_({ delays });
  }

  private loadPreview(): void {
    window.clearTimeout(this.timer);
    const d = this.draft;
    if (!d) return;
    this.timer = window.setTimeout(async () => {
      try {
        const res = await lighting<Obj>(cmd("brightness/preview"), { area_id: this.room.area_id, entity_id: d.entity_id, matrix: { enabled: true, room: d.room, zones: d.zones, delays: delaysPayload(d) } });
        if (this.draft?.entity_id === d.entity_id) { this.preview = res; this.previewError = null; }
      } catch (err) {
        this.preview = null;
        this.previewError = err instanceof Error ? err.message : String(err);
      }
    }, 250);
  }

  private setCell(value: number | null): void {
    const d = this.draft;
    const c = this.cell;
    if (!d || !c) return;
    if (c.col === "room") this.update_({ room: { ...d.room, [c.level]: value } });
    else this.update_({ zones: { ...d.zones, [c.col]: { ...(d.zones[c.col] ?? {}), [c.level]: value } } });
  }

  private applyTemplate(id: string): void {
    const t = (this.b.templates ?? []).find((x: Obj) => x.id === id);
    const d = this.draft;
    if (!t || !d) return;
    this.update_({ room: { ...this.emptyCells(), ...t.room }, zones: Object.fromEntries(Object.keys(d.zones).map((z) => [z, { ...this.emptyCells(), ...t.zone }])), template: t.id,
      delays: this.templateDelays(t, Object.keys(d.zones)) });
  }

  private async save(enrol: boolean): Promise<void> {
    const d = this.draft;
    if (!d) return;
    const ok = await this.send("brightness/set", { entity_id: d.entity_id, matrix: { enabled: d.enabled, room: d.room, zones: d.zones, template: d.template, delays: delaysPayload(d) }, ...(enrol ? { confirm: true } : {}) },
      enrol ? "Enrolled · nothing was sent" : "Saved · nothing was sent");
    if (ok && this.draft) this.draft = { ...this.draft, dirty: false, enrolled: true };
  }

  // ---------------------------------------------------------------- ambient
  private ambientCard(): TemplateResult {
    const a = this.b.ambient ?? {};
    const st = a.status ?? {};
    const own = a.settings ?? {};
    const glob = a.global ?? {};
    const eff = a.effective ?? {};
    const lim = this.b.limits ?? {};
    const dis = !this.canEdit || this.busy;
    const set = (change: Obj, done = "Saved") => void this.send("ambient/set", change, done);
    const isOwn = (k: string) => own[k] !== null && own[k] !== undefined;
    const tun = (k: string, label: string, unit: string, max: number, step: number, help: string) => numberField({
      label, value: eff[k], unit, min: 0, max, step, disabled: dis, help,
      tag: inheritTag(isOwn(k), `${glob[k]} ${unit}`, dis ? null : () => set({ [k]: null }, "Uses the global default")), onChange: (v) => set({ [k]: v }) });
    const basis = AMB_BASIS[st.basis] ?? [st.basis ?? "—", "neutral"];
    const levels: Obj[] = st.levels ?? [];
    return card("Ambient light levels", "brightness", html`
      <p class="small muted">The natural light in ${this.room.name}, in categories, from the room's light sensors and estimates. Brightness matrices use the category to choose each enrolled light's brightness; a reading lit by the room's own lamps never raises it.</p>
      ${toggleField({ label: `Ambient brightness control in ${this.room.name}`, checked: !!a.enabled, disabled: dis, testid: "lighting-ambient-enabled",
        help: a.enabled ? undefined : "Off: enrolled lights keep their mood brightness. The category is shown for reference.", onChange: (v) => set({ enabled: v }) })}
      <div class="lp-levels-strip" role="list" aria-label=${`Ambient light level now: ${st.name ?? "unknown"}`}>
        ${levels.map((l) => html`<div role="listitem" class="lp-level ${l.key === st.level ? "on" : ""}" aria-current=${l.key === st.level ? "true" : "false"}>
          <strong>${l.name}</strong><span class="small muted">${l.below === null || l.below === undefined ? `${l.from} lx and above` : l.from ? `${l.from}–${l.below} lx` : `below ${l.below} lx`}</span>
          ${l.key === st.level ? chip("ok", "now") : nothing}</div>`)}
      </div>
      ${kv([["Ambient now", html`${st.label ?? "—"} ${chip(basis[1], basis[0])}`],
        ["Measured indoors", st.measured_lux !== null && st.measured_lux !== undefined ? `${Math.floor(st.measured_lux)} lx${st.sensor_name ? ` · ${st.sensor_name}` : ""}` : "no indoor reading"],
        ["Natural light", String(st.text ?? "").replace(/^Natural light: /, "")],
        ["Confidence", st.confidence ?? "—"], ["Category since", fmt(st.since)],
        st.contaminated && st.contamination ? ["Not used as natural light", st.contamination] : null,
        st.candidate ? ["Changing to", `${levels.find((l) => l.key === st.candidate)?.name ?? st.candidate}${st.candidate_due ? ` at ${fmt(st.candidate_due)}` : ""} if the level holds`] : null])}
      ${st.note ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>${st.note}</span></p>` : nothing}
      <p class="small muted">Light sensors: the same as daylight switch-on (Automatic tab).</p>
      ${advancedToggle(this.adv, () => (this.adv = !this.adv), "Stability, fallback and fades")}
      ${this.adv ? html`<div class="lp-form">
        ${tun("hysteresis", "Hysteresis", "lx", lim.hysteresis?.[1] ?? 200, 0.5, "A category is left downwards only this far below its threshold.")}
        ${tun("stable_seconds", "Stable for", "s", lim.stable?.[1] ?? 3600, 10, "A new category counts after it held this long.")}
        ${selectField({ label: "When uncertain", value: eff.fallback, disabled: dis, options: [["hold", "Keep the last reliable category"], ["level", "Use the fallback category"]],
          tag: inheritTag(isOwn("fallback"), glob.fallback === "hold" ? "keep" : "fallback", dis ? null : () => void this.host.run(async () => {
            await lighting(cmd("ambient/set"), { area_id: this.room.area_id, fallback: null, fallback_level: null }); }, "Uses the global default")),
          onChange: (v) => set({ fallback: v }) })}
        ${selectField({ label: "Fallback category", value: eff.fallback_level, disabled: dis, options: (this.b.levels ?? []).map((l: Obj) => [l.key, l.name] as [string, string]), onChange: (v) => set({ fallback_level: v }) })}
        ${tun("transition", "Brightness fade", "s", lim.transition?.[1] ?? 60, 0.5, "Uses the light's own transition when it supports one.")}
        ${tun("dead_band", "Dead band", "%", lim.dead_band?.[1] ?? 25, 1, "Smaller brightness differences are not sent.")}
        ${tun("min_interval", "Between changes", "s", lim.min_interval?.[1] ?? 3600, 30, "At least this long between two changes caused by the light level (0: no limit).")}
      </div><p class="small muted">Global defaults: <a href="#/lighting/settings/ambient">Lighting settings</a>.</p>` : nothing}
      ${(a.history ?? []).length ? html`<h4 class="lp-h">Recent category changes</h4><ul class="lp-dlist">${[...a.history].reverse().map((x: Obj) => html`<li>${fmt(x.at)} ${levels.find((l) => l.key === x.from)?.name ?? x.from} → <strong>${levels.find((l) => l.key === x.to)?.name ?? x.to}</strong> <span class="small muted">${x.lux ?? ""}</span></li>`)}</ul>` : nothing}`,
      { testid: "lighting-ambient" });
  }

  // ----------------------------------------------------------------- matrix
  private cellText(v: number | null | undefined, onoff: boolean): string {
    if (v === null || v === undefined) return "—";
    if (v === 0) return "Off";
    return onoff ? "On" : `${v} %`;
  }

  private matrixPanel(l: Obj, d: MatrixDraft): TemplateResult {
    const b = this.b;
    const st = b.ambient?.status ?? {};
    const levels: Obj[] = b.levels ?? [];
    const dis = !this.canEdit || this.busy;
    const onoff = !!l.onoff;
    const nb = l.now?.brightness;
    const val = (col: string, level: string) => (col === "room" ? d.room[level] : d.zones[col]?.[level]);
    const zones: Obj[] = l.columns.filter((c: Obj) => c.id !== "room");
    const others: Obj[] = (b.lights ?? []).filter((x: Obj) => x.matrix && x.entity_id !== l.entity_id);
    const c = this.cell;
    const edCol = c ? l.columns.find((x: Obj) => x.id === c.col) : null;
    const edVal = c ? val(c.col, c.level) : null;
    const p = this.preview;
    const after = p?.after;
    const ab = after?.rule?.brightness;
    const resolved = (item: Obj | null | undefined) => (item ? (item.service === "turn_off" ? "Off" : item.service === "turn_on" ? summary(item.resolved) : "No command") : "—");
    return html`<div class="lp-matrix-panel" data-testid="lighting-matrix">
      <div class="lp-row-line"><h3 class="lp-h2">${l.name}</h3>${inspectButton(l.entity_id, this.room.area_id, l.name)}${d.enrolled ? (d.enabled ? chip("ok", "Enrolled") : chip("warning", "Paused")) : chip("neutral", "Not enrolled")}
        ${d.dirty && d.enrolled ? chip("info", "Unsaved changes") : nothing}<span class="spacer"></span>
        <button type="button" class="icon-btn" aria-label="Close" @click=${() => this.select(null)}>${icon("close")}</button></div>
      ${onoff ? html`<p class="notice notice-info">${icon("info", 20)}<span>${l.name}${l.fixture?.kind === "dumb" ? " is a Dumb Light: it" : ""} switches on and off only: each value is On or Off (no dimming, colour or fade is sent).</span></p>` : nothing}
      ${l.override ? html`<p class="notice notice-info">${icon("tap", 20)}<span>Manual change (${l.override.source ?? "manual"}) respected ${l.override.until ? `until ${fmt(l.override.until)}` : "until the room is vacant"}.
        ${dis ? nothing : html`<button type="button" class="lp-link" @click=${() => void this.send("override/clear", { entity_id: l.entity_id }, "Automatic lighting resumed")}>Resume automatic lighting</button>`}</span></p>` : nothing}
      ${toggleField({ label: "Use this brightness matrix", checked: d.enabled, disabled: dis, onChange: (v) => this.update_({ enabled: v }) })}
      ${dis ? nothing : html`<div class="lp-tools">
        <select aria-label="Apply a template" @change=${(e: Event) => { const v = (e.target as HTMLSelectElement).value; (e.target as HTMLSelectElement).value = ""; if (v) this.applyTemplate(v); }}>
          <option value="">Apply a template…</option>${(b.templates ?? []).map((t: Obj) => html`<option value=${t.id}>${t.name}${t.builtin ? "" : " (saved)"}</option>`)}</select>
        ${others.length ? html`<select aria-label="Copy from another light" @change=${(e: Event) => { const v = (e.target as HTMLSelectElement).value; (e.target as HTMLSelectElement).value = "";
            const o = others.find((x) => x.entity_id === v); if (o) this.update_({ room: { ...this.emptyCells(), ...o.matrix.room }, zones: Object.fromEntries(Object.keys(d.zones).map((z) => [z, { ...this.emptyCells(), ...(o.matrix.zones?.[z] ?? d.zones[z]) }])),
              delays: Object.fromEntries(Object.entries(o.matrix.delays ?? {}).filter(([col]) => col === "room" || col in d.zones).map(([col, v]) => [col, JSON.parse(JSON.stringify(v))])) }); }}>
          <option value="">Copy from light…</option>${others.map((x) => html`<option value=${x.entity_id}>${x.name}</option>`)}</select>` : nothing}
        ${zones.length ? html`<select aria-label="Copy the Room occupied column to a zone" @change=${(e: Event) => { const v = (e.target as HTMLSelectElement).value; (e.target as HTMLSelectElement).value = "";
            if (v) this.update_({ zones: { ...d.zones, [v]: { ...d.room } }, delays: { ...d.delays, [v]: JSON.parse(JSON.stringify(d.delays.room ?? {})) } }); }}>
          <option value="">Copy Room column to…</option>${zones.map((z) => html`<option value=${z.id}>${z.name} zone</option>`)}</select>` : nothing}
        <button type="button" class="btn btn-small" @click=${() => (this.templateName = this.templateName === null ? `${l.name} light` : null)}>${icon("save", 16)}Save as template</button>
      </div>
      ${this.templateName !== null ? html`<div class="lp-inline lp-subcard"><label class="lp-field"><span class="lp-label">Template name (for every room)</span>
          <input type="text" maxlength="60" .value=${this.templateName} @input=${(e: Event) => (this.templateName = (e.target as HTMLInputElement).value)} /></label>
        <button type="button" class="btn btn-small btn-primary" ?disabled=${!this.templateName.trim()} @click=${async () => {
          const firstZone = Object.keys(d.zones)[0];
          const zone = firstZone ? d.zones[firstZone] : this.emptyCells();
          const delays = { room: d.delays.room ?? {}, zone: firstZone ? d.delays[firstZone] ?? {} : {} };
          if (await this.send("brightness/template", { op: "save", name: this.templateName!.trim(), room: d.room, zone, delays }, "Template saved")) this.templateName = null; }}>Save template</button></div>` : nothing}`}
      <p class="small muted">Rows: ambient light. Columns: who is in the room. ${onoff ? "On or Off" : "0 % = Off"}, — = not configured. Select a value to change it.</p>
      <div class="table-wrap"><table class="lp-matrix">
        <thead><tr><th scope="col">Ambient light</th>${l.columns.map((col: Obj) => html`<th scope="col">${col.id === "room" ? "Room occupied" : `${col.name} zone`}
          ${col.id === "room" ? nothing : html`<span class="block small muted">priority ${col.priority}${col.active ? " · active" : ""}</span>`}</th>`)}</tr></thead>
        <tbody>${levels.map((lv) => html`<tr class=${lv.key === st.level ? "lp-now-row" : ""}><th scope="row">${lv.name}${lv.key === st.level ? html` ${chip("ok", "now")}` : nothing}<span class="block small muted">${lv.range}</span></th>
          ${l.columns.map((col: Obj) => {
            const v = val(col.id, lv.key);
            const wins = d.enrolled && !d.dirty && nb && nb.column === col.id && nb.level === lv.key;
            const editing = c && c.col === col.id && c.level === lv.key;
            const shade = v === null || v === undefined ? 0 : v === 0 ? 0 : onoff ? 0.55 : 0.12 + (v / 100) * 0.6;
            const dly = delayText(d.delays?.[col.id]?.[lv.key]);
            return html`<td><button type="button" class="lp-cell ${v === null || v === undefined ? "unset" : v === 0 ? "off" : ""} ${wins ? "wins" : ""} ${editing ? "editing" : ""}"
              style=${`--lp-shade:${shade}`} ?disabled=${dis} aria-pressed=${editing ? "true" : "false"} title=${dly || nothing} data-delay=${dly ? "1" : nothing}
              aria-label=${`${col.id === "room" ? "Room occupied" : `${col.name} zone`}, ${lv.name}: ${v === null || v === undefined ? "not configured" : this.cellText(v, onoff)}${dly ? `, ${dly}` : ""}`}
              @click=${() => (this.cell = editing ? null : { col: col.id, level: lv.key })}>${this.cellText(v, onoff)}${dly ? html`<span class="lp-cell-delay" aria-hidden="true">${icon("timer-outline", 13)}</span>` : nothing}${wins ? html`<span class="lp-cell-now">in use</span>` : nothing}</button></td>`;
          })}</tr>`)}</tbody></table></div>
      ${c && edCol ? html`<div class="lp-subcard lp-cell-editor" role="group" aria-label="Edit a brightness value" data-testid="lighting-cell-editor">
        <strong>${edCol.id === "room" ? "Room occupied" : `Zone: ${edCol.name}`} · ${levels.find((x) => x.key === c.level)?.name ?? c.level}</strong>
        ${onoff ? html`<div class="lp-seg" role="group" aria-label="On or Off">
            <button type="button" class=${edVal !== null && edVal !== undefined && edVal > 0 ? "on" : ""} @click=${() => this.setCell(100)}>On</button>
            <button type="button" class=${edVal === 0 ? "on" : ""} @click=${() => this.setCell(0)}>Off</button></div>`
          : html`<div class="lp-inline"><input type="range" min="0" max="100" step="1" .value=${String(edVal ?? 0)} aria-label="Brightness (0 = Off)"
              @change=${(e: Event) => this.setCell(Number((e.target as HTMLInputElement).value))} />
            <span class="lp-input-row"><input type="number" min="0" max="100" class="lp-num" .value=${edVal === null || edVal === undefined ? "" : String(edVal)} aria-label="Brightness in percent"
              @change=${(e: Event) => { const raw = (e.target as HTMLInputElement).value.trim(); this.setCell(raw === "" ? null : Math.max(0, Math.min(100, Number(raw)))); }} /> %</span>
            <button type="button" class="btn btn-small" @click=${() => this.setCell(0)}>Off</button></div>`}
        ${(edVal ?? 0) > 0 ? html`<div class="lp-form lp-delays" data-testid="lighting-cell-delays">
            ${numberField({ label: "Delay On", value: d.delays?.[c.col]?.[c.level]?.on ?? 0, unit: "s", min: 0, max: DELAY_MAX, step: 1,
              help: "Wait this long before switching on (0 = at once).", onChange: (v) => this.setDelay("on", v) })}
            ${numberField({ label: "Delay Off", value: d.delays?.[c.col]?.[c.level]?.off ?? 0, unit: "s", min: 0, max: DELAY_MAX, step: 1,
              help: "Keep it on this long after this situation ends (0 = at once).", onChange: (v) => this.setDelay("off", v) })}
          </div>
          <p class="small muted">Delays apply only to switching on and off: a change from one brightness to another is never delayed. A manual change, a wall switch, a Priority Input or vacancy always acts at once.</p>` : nothing}
        <div class="row-actions lp-row"><button type="button" class="btn btn-small" @click=${() => this.setCell(null)}>Not configured</button>
          <button type="button" class="btn btn-small" @click=${() => (this.cell = null)}>${icon("check", 16)}Done</button></div>
        <p class="small muted">${edCol.id === "room" ? "Not configured: the light keeps its mood brightness in this situation." : "Not configured: this zone does not claim the brightness (another active zone, else Room occupied decides)."}</p>
      </div>` : nothing}
      ${!zones.length ? html`<p class="small muted">No zone lists ${l.name}: add it to a zone's lights (Zones tab) to get a zone column.</p>` : nothing}
      <div class="lp-preview" data-testid="lighting-matrix-preview"><h4 class="lp-h">${d.enrolled ? "Preview" : "Before enrolling"}</h4>
        ${this.previewError ? html`<p class="small tone-error">${this.previewError}</p>` : !p ? html`<p class="small muted">Loading the preview…</p>` : html`
        ${kv([["Now", `${p.occupancy ? OCC[p.occupancy]?.[0] ?? p.occupancy : "—"}${p.zones?.length ? ` · ${p.zones.map((z: Obj) => `${z.name} zone`).join(", ")} active` : ""} · ${p.ambient?.label ?? ""}`],
          ["Mood", p.mood ?? "no mood"],
          ["Without matrix", html`${resolved(p.before)} <span class="small muted">${p.before?.rule?.name ?? ""}</span>`],
          ["With this matrix", html`<strong>${ab ? `${ab.column_name} · ${ab.level_name} → ${ab.text}` : resolved(after)}</strong>${ab?.appearance ? html` <span class="small muted">${ab.appearance}</span>` : nothing}`],
          ab && (ab.delay_on || ab.delay_off) ? ["Delays of this cell", delayText({ on: ab.delay_on, off: ab.delay_off })] : null,
          ["Command", after ? (after.decision === "change" ? (p.would_send ? "sent at the next trigger" : "would be sent (room Observing)") : after.decision === "unchanged" ? "none needed (already like this)" : after.reason) : "—"]])}
        ${ab?.explanation && (ab.superseded ?? []).length > 1 ? html`<p class="small muted">${ab.explanation}</p>` : nothing}
        ${(p.conflicts ?? []).length ? html`<div class="notice notice-warning">${icon("warning", 20)}<span><strong>What changes for this light</strong><ul class="lp-dlist">${p.conflicts.map((x: string) => html`<li>${x}</li>`)}</ul></span></div>` : nothing}`}
        <p class="small muted">${d.enrolled ? "Saving never sends a command: the next trigger (occupancy, a zone, the light level, the mood) applies it." : "Enrolling sends no command: the light follows its matrix from the next trigger."}</p>
      </div>
      ${dis ? nothing : html`<div class="row-actions lp-row lp-matrix-actions">
        ${d.enrolled ? html`
          ${d.dirty ? html`<button type="button" class="btn" @click=${() => this.select(l.entity_id)}>Discard changes</button>` : nothing}
          <button type="button" class="btn btn-primary" data-testid="lighting-matrix-save" ?disabled=${!d.dirty || !p} @click=${() => void this.save(false)}>${icon("save", 18)}Save matrix</button>
          ${this.askRemove ? html`<span class="lp-confirm">${l.name} leaves the brightness rules and follows its mood and zone settings again; its matrix is deleted.
              <button type="button" class="btn btn-danger btn-small" @click=${async () => { if (await this.send("brightness/remove", { entity_id: l.entity_id }, "Left the brightness rules")) this.select(null); }}>Leave brightness rules</button>
              <button type="button" class="btn btn-small" @click=${() => (this.askRemove = false)}>Cancel</button></span>`
            : html`<button type="button" class="btn btn-danger" @click=${() => (this.askRemove = true)}>${icon("delete", 18)}Leave brightness rules…</button>`}`
        : html`<button type="button" class="btn btn-primary" data-testid="lighting-matrix-enrol" ?disabled=${!p} @click=${() => void this.save(true)}>${icon("check", 18)}Enrol after this preview</button>`}
      </div>`}
    </div>`;
  }

  protected render(): unknown {
    const b = this.b;
    if (!this.room.brightness) return html`<p class="muted">Brightness settings are not available for this room.</p>`;
    const lights: Obj[] = b.lights ?? [];
    const l = this.sel ? lights.find((x) => x.entity_id === this.sel) : null;
    return html`<div class="lp-stack"><div class="lp-two">
      ${this.ambientCard()}
      ${card("Categories", "layers", html`<sl-lp-levels .areaId=${this.room.area_id} .roomName=${this.room.name} .ambient=${b.ambient} .levels=${b.levels ?? []}
          .limits=${b.limits} .current=${b.ambient?.status?.level ?? null} .host=${this.host} .busy=${this.busy} ?canEdit=${this.canEdit}></sl-lp-levels>`, { testid: "lighting-categories" })}
      </div>
      ${card("Brightness matrices", "grid", html`
        <p class="small muted">An enrolled light gets its on/off and brightness from occupancy, its zones and the ambient level; its mood keeps choosing colour and colour temperature. Lights that are not enrolled keep every mood and zone setting.</p>
        ${b.ambient?.enabled ? nothing : html`<p class="notice notice-warning">${icon("warning", 20)}<span>Ambient brightness control is off in ${this.room.name}: enrolled lights follow their mood until it is switched on.</span></p>`}
        <div class="lp-matrix-layout">
          <ul class="lp-pick-list" aria-label="Lights">${lights.map((x) => {
            const m = x.matrix;
            const [word, tone] = !m ? ["Not enrolled", "neutral"] : !m.enabled ? ["Paused", "warning"] : !b.ambient?.enabled ? ["Enrolled · control off", "warning"] : ["Enrolled", "ok"];
            const nb = x.now?.brightness;
            return html`<li><button type="button" class="lp-pick ${this.sel === x.entity_id ? "selected" : ""}" aria-pressed=${this.sel === x.entity_id ? "true" : "false"} @click=${() => this.select(this.sel === x.entity_id ? null : x.entity_id)}>
              ${icon(x.type === "relay" ? "electric-switch" : "lightbulb", 20)}<span class="lp-pick-main"><strong>${x.name}</strong>
                <span class="small muted">${nb ? `${nb.column_name} · ${nb.level_name} → ${nb.text}` : x.now ? `${x.now.rule ?? ""} · ${stateText(x.now.requested)}` : ""}</span>
                <span>${chip(tone as never, word)}</span></span></button>${inspectButton(x.entity_id, this.room.area_id, x.name)}</li>`;
          })}${!lights.length ? html`<li class="muted">No light is managed in this room.</li>` : nothing}</ul>
          <div class="lp-matrix-main">${l && this.draft ? this.matrixPanel(l, this.draft) : html`<div class="lp-placeholder">${icon("grid", 32)}<p class="muted">Select a light to see or edit its brightness matrix.</p></div>`}</div>
        </div>`, { wide: true, testid: "lighting-matrices" })}</div>`;
  }
}

customElements.define("sl-lp-brightness", SlLpBrightness);
