// Phase A7.0: Climate → Settings. One draft of the whole climate configuration; nothing is stored until the
// changes are reviewed (validation, the list of changes, warnings) and saved with the revision they were based on.
// Suggestions from Home Assistant's areas are applied only when a person chooses them. Route:
// #/climate/settings/<general|rooms|zones|outdoor|backups>[?room=<area_id>].
import "../climate/climate.css";

import { html, nothing, type PropertyValues, type TemplateResult } from "lit";

import {
  ApiError, clone, emptyRoom, exportUrl, fromDisplay, getBackups, getDiscovery, getDocument, importApply, importReview,
  restore, restoreReview, review, save, slugify, temp, toDisplay,
  type BackupInfo, type Change, type ClimateConfig, type ClimateDocument, type Discovery, type Review, type RoomConfig,
  type Targets, type Unit, type Zone,
} from "../climate/api";
import { icon } from "../icons";
import { haTime } from "../presence/components";
import { hashQuery, setLeaveGuard } from "../router";
import { download } from "../rooms/model";
import { store } from "../store";
import { chip, pageHeader, StoreElement } from "../ui";
import { climateTabs, observingNotice, problemText } from "./climate";

const SECTIONS = [
  { key: "general", title: "General", icon: "tune" },
  { key: "rooms", title: "Rooms and devices", icon: "home-thermometer" },
  { key: "zones", title: "Thermal zones", icon: "open-plan" },
  { key: "outdoor", title: "Outdoor temperature", icon: "weather-sunny" },
  { key: "backups", title: "Backups", icon: "restore" },
] as const;
type Section = (typeof SECTIONS)[number]["key"];

type Dialog =
  | { kind: "save"; review: Review | null; error?: unknown }
  | { kind: "restore"; backup: string; review: Review | null; error?: unknown }
  | { kind: "import"; file: unknown; review: Review | null; error?: unknown }
  | null;

const LABEL: Record<string, string> = {
  comfort_c: "comfort temperature", setback_c: "setback temperature", min_target_c: "lowest allowed target",
  max_target_c: "highest allowed target", stale_minutes: "sensor freshness (minutes)", thermostats: "heating devices",
  primary_sensor: "primary sensor", "primary_sensor.entity_id": "primary sensor", secondary_sensors: "secondary sensors",
  openings: "windows and doors", retention_days: "observation retention (days)", backfill_days: "Recorder history to read (days)",
  report_minutes: "unchanged values re-recorded every (minutes)", enabled: "observation", min_history_days: "days needed for learning",
  display: "display unit", "outdoor.entity_id": "outdoor source", name: "name", rooms: "rooms", layout: "layout",
  shared_thermostats: "shared heating devices", heating_circuit: "heating circuit",
};

export class SlClimateSettings extends StoreElement {
  static properties = {
    section: { type: String }, doc: { state: true }, discovery: { state: true }, draft: { state: true },
    room: { state: true }, dialog: { state: true }, busy: { state: true }, error: { state: true }, notice: { state: true },
    backups: { state: true }, addArea: { state: true },
  };
  declare section: string;
  declare doc: ClimateDocument | null;
  declare discovery: Discovery | null;
  declare draft: ClimateConfig | null;
  declare room: string;
  declare dialog: Dialog;
  declare busy: boolean;
  declare error: unknown;
  declare notice: string | null;
  declare backups: BackupInfo[] | null;
  declare addArea: string;
  private seenConfig = -1;
  /** The revision the draft was based on (sent with review and save, so a change made meanwhile is detected). */
  private base = "";

  constructor() {
    super();
    this.section = "general";
    this.doc = null;
    this.discovery = null;
    this.draft = null;
    this.room = hashQuery().get("room") ?? "";
    this.dialog = null;
    this.busy = false;
    this.error = null;
    this.notice = null;
    this.backups = null;
    this.addArea = "";
  }

  connectedCallback(): void {
    super.connectedCallback();
    setLeaveGuard(() => (this.dirty() ? "The climate settings have unsaved changes. Leave without saving them?" : null));
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    setLeaveGuard(null);
  }

  protected willUpdate(changed: PropertyValues): void {
    super.willUpdate(changed);
    if (store.climateConfigVersion !== this.seenConfig) {
      this.seenConfig = store.climateConfigVersion;
      void this.load();
    }
    if (changed.has("section")) {
      const q = hashQuery().get("room");
      if (q) this.room = q;
      if (this.currentSection === "backups") void this.loadBackups();
    }
  }

  private get currentSection(): Section {
    return (SECTIONS.find((s) => s.key === this.section)?.key ?? "general") as Section;
  }

  private get unit(): Unit {
    const d = this.draft?.units.display ?? "auto";
    if (d === "celsius") return "°C";
    if (d === "fahrenheit") return "°F";
    return (this.doc?.ha_unit === "°F" ? "°F" : "°C");
  }

  private async load(): Promise<void> {
    try {
      const [doc, discovery] = await Promise.all([getDocument(), getDiscovery()]);
      const keep = this.dirty();
      this.doc = doc;
      this.discovery = discovery;
      if (!keep || !this.draft) {
        this.draft = clone(doc.config);
        this.base = doc.revision;
      } else this.notice = "The climate configuration was changed in another session. Your unsaved changes are kept; the review will show that they are based on an older version.";
      if (!this.room || !(this.room in (this.draft?.rooms ?? {}))) this.room = Object.keys(this.draft?.rooms ?? {}).sort((a, b) => this.areaName(a).localeCompare(this.areaName(b)))[0] ?? this.room;
      this.error = null;
    } catch (err) {
      this.error = err;
    }
  }

  private async loadBackups(): Promise<void> {
    try {
      this.backups = await getBackups();
    } catch (err) {
      this.error = err;
    }
  }

  private dirty(): boolean {
    return Boolean(this.doc && this.draft && JSON.stringify(this.draft) !== JSON.stringify(this.doc.config));
  }

  private change(mutate: (c: ClimateConfig) => void): void {
    if (!this.draft) return;
    const next = clone(this.draft);
    mutate(next);
    // A removed room or heating device leaves no dangling reference in a thermal zone.
    next.zones = next.zones
      .map((z) => {
        const rooms = z.rooms.filter((r) => r in next.rooms);
        const devices = new Set(rooms.flatMap((r) => next.rooms[r].thermostats.map((t) => t.entity_id)));
        return { ...z, rooms, shared_thermostats: z.shared_thermostats.filter((t) => devices.has(t)) };
      })
      .filter((z) => z.rooms.length);
    this.draft = next;
    this.notice = null;
  }

  private areaName(areaId: string): string {
    return this.discovery?.areas.find((a) => a.id === areaId)?.name ?? areaId;
  }

  private entityName(entityId: string): string {
    const d = this.discovery;
    return d?.thermostats.find((t) => t.entity_id === entityId)?.name ?? d?.sensors.find((s) => s.entity_id === entityId)?.name
      ?? d?.openings.find((o) => o.entity_id === entityId)?.name ?? d?.outdoor.find((o) => o.entity_id === entityId)?.name ?? entityId;
  }

  // --------------------------------------------------------------- inputs
  private tempField(label: string, value: number | null, set: (c: number | null) => void, opts: { inherit?: number | null; help?: string; testid?: string } = {}): TemplateResult {
    const unit = this.unit;
    const shown = toDisplay(value, unit);
    return html`<label class="cl-field"><span class="field-label">${label} (${unit})</span>
      <input type="number" step="0.5" inputmode="decimal" data-testid=${opts.testid ?? nothing} .value=${shown === null ? "" : String(Math.round(shown * 10) / 10)}
        placeholder=${opts.inherit !== undefined && opts.inherit !== null ? `${(toDisplay(opts.inherit, unit) ?? 0).toFixed(1)} (default)` : ""}
        @change=${(e: Event) => { const raw = (e.target as HTMLInputElement).value.trim(); set(raw === "" ? null : fromDisplay(Number(raw), unit)); }} />
      ${opts.help ? html`<span class="cl-help">${opts.help}</span>` : nothing}</label>`;
  }

  private numberField(label: string, value: number | null, set: (v: number | null) => void, opts: { min: number; max: number; placeholder?: string; help?: string; testid?: string }): TemplateResult {
    return html`<label class="cl-field"><span class="field-label">${label}</span>
      <input type="number" min=${opts.min} max=${opts.max} step="1" data-testid=${opts.testid ?? nothing} .value=${value === null ? "" : String(value)} placeholder=${opts.placeholder ?? ""}
        @change=${(e: Event) => { const raw = (e.target as HTMLInputElement).value.trim(); set(raw === "" ? null : Math.round(Number(raw))); }} />
      ${opts.help ? html`<span class="cl-help">${opts.help}</span>` : nothing}</label>`;
  }

  // -------------------------------------------------------------- general
  private general(c: ClimateConfig): TemplateResult {
    const setD = (k: keyof Targets) => (v: number | null) => this.change((n) => { (n.defaults as unknown as Record<string, number | null>)[k] = v; });
    return html`<section class="card pad cl-section" data-testid="climate-general">
      <h3>Mode</h3>
      <p>${chip("info", "Observing")} <span class="muted">The only mode in this version. Smart Comfort never sends a thermostat command; automatic heating needs later phases and your explicit activation.</span></p>
      <h3>Units</h3>
      <div class="cl-form"><label class="cl-field"><span class="field-label">Show temperatures in</span>
        <select data-testid="climate-unit" @change=${(e: Event) => this.change((n) => { n.units.display = (e.target as HTMLSelectElement).value as ClimateConfig["units"]["display"]; })}>
          ${(["auto", "celsius", "fahrenheit"] as const).map((u) => html`<option value=${u} ?selected=${c.units.display === u}>${u === "auto" ? `Home Assistant's unit (${this.doc?.ha_unit ?? "°C"})` : u === "celsius" ? "Celsius (°C)" : "Fahrenheit (°F)"}</option>`)}
        </select><span class="cl-help">Stored in °C either way; sensors in °F or K are converted.</span></label></div>
      <h3>Comfort preferences (defaults for every room)</h3>
      <p class="small muted">Stored now and used by later phases (prediction and, after your activation, control). A room can override each value.</p>
      <div class="cl-form">
        ${this.tempField("Comfort", c.defaults.comfort_c, setD("comfort_c"), { help: "When the room is in use", testid: "climate-comfort" })}
        ${this.tempField("Setback", c.defaults.setback_c, setD("setback_c"), { help: "When the room is not in use" })}
        ${this.tempField("Lowest allowed target", c.defaults.min_target_c, setD("min_target_c"), { help: "Frost and damp protection; never below 5 °C" })}
        ${this.tempField("Highest allowed target", c.defaults.max_target_c, setD("max_target_c"), { help: "Never above 30 °C" })}
      </div>
      <h3>Data quality and observation</h3>
      <div class="cl-form">
        ${this.numberField("A sensor is current for (minutes)", c.defaults.stale_minutes, (v) => this.change((n) => { n.defaults.stale_minutes = v as number; }), { min: 5, max: 1440, help: "Without a report for longer, a reading is \"not updated recently\" and not used" })}
        <label class="cl-field"><span class="field-label">Observation</span>
          <select @change=${(e: Event) => this.change((n) => { n.observation.enabled = (e.target as HTMLSelectElement).value === "on"; })}>
            <option value="on" ?selected=${c.observation.enabled}>Record observations</option>
            <option value="off" ?selected=${!c.observation.enabled}>Do not record</option></select>
          <span class="cl-help">State changes of the configured entities, stored in the App</span></label>
        ${this.numberField("Keep observations for (days)", c.observation.retention_days, (v) => this.change((n) => { n.observation.retention_days = v as number; }), { min: 7, max: 730 })}
        ${this.numberField("Read Recorder history for (days)", c.observation.backfill_days, (v) => this.change((n) => { n.observation.backfill_days = v as number; }), { min: 0, max: 30, help: "Once per new entity and after an outage, in bounded windows" })}
        ${this.numberField("Re-record an unchanged value every (minutes)", c.observation.report_minutes, (v) => this.change((n) => { n.observation.report_minutes = v as number; }), { min: 5, max: 120, help: "Tells a steady sensor from a silent one" })}
        ${this.numberField("Days of history before learning", c.prediction.min_history_days, (v) => this.change((n) => { n.prediction.min_history_days = v as number; }), { min: 3, max: 90, help: "Used by thermal learning (a later phase)" })}
      </div>
    </section>`;
  }

  // ---------------------------------------------------------------- rooms
  private useSuggestion(areaId: string): void {
    const s = this.discovery?.suggestions.rooms[areaId];
    if (!s) return;
    this.change((n) => {
      const room = n.rooms[areaId] ?? emptyRoom();
      const taken = new Set(Object.entries(n.rooms).filter(([a]) => a !== areaId).flatMap(([, r]) => r.thermostats.map((t) => t.entity_id)));
      if (!room.thermostats.length) room.thermostats = s.thermostats.filter((t) => !taken.has(t.entity_id)).map((t) => ({ entity_id: t.entity_id, kind: t.kind }));
      if (!room.primary_sensor && s.primary_sensor) room.primary_sensor = { entity_id: s.primary_sensor };
      if (!room.secondary_sensors.length) room.secondary_sensors = s.secondary_sensors.map((e) => ({ entity_id: e }));
      if (!room.openings.length) room.openings = s.openings.map((e) => ({ entity_id: e }));
      n.rooms[areaId] = room;
    });
  }

  private addRoom(areaId: string): void {
    if (!areaId || this.draft?.rooms[areaId]) return;
    this.change((n) => { n.rooms[areaId] = emptyRoom(); });
    this.room = areaId;
    this.addArea = "";
  }

  private removeRoom(areaId: string): void {
    this.change((n) => {
      delete n.rooms[areaId];
      n.zones = n.zones.map((z) => ({ ...z, rooms: z.rooms.filter((r) => r !== areaId) })).filter((z) => z.rooms.length);
    });
    this.room = Object.keys(this.draft?.rooms ?? {})[0] ?? "";
  }

  private toggle<T extends { entity_id: string }>(list: T[], entityId: string, on: boolean, make: () => T): T[] {
    return on ? (list.some((x) => x.entity_id === entityId) ? list : [...list, make()]) : list.filter((x) => x.entity_id !== entityId);
  }

  private pickList(items: Array<{ entity_id: string; name: string; area_id: string | null; area: string | null; note?: string; disabled?: string }>,
    areaId: string, checked: (id: string) => boolean, set: (id: string, on: boolean) => void, testid: string): TemplateResult {
    const here = items.filter((i) => i.area_id === areaId);
    const other = items.filter((i) => i.area_id !== areaId);
    const row = (i: (typeof items)[number]) => html`<li><label>
      <input type="checkbox" data-entity=${i.entity_id} .checked=${checked(i.entity_id)} ?disabled=${Boolean(i.disabled) && !checked(i.entity_id)} @change=${(e: Event) => set(i.entity_id, (e.target as HTMLInputElement).checked)} />
      <span class="cl-pick-body"><span>${i.name}</span><span class="small muted"><span class="mono">${i.entity_id}</span>${i.area ? ` · ${i.area}` : " · no area"}${i.note ? ` · ${i.note}` : ""}${i.disabled ? ` · ${i.disabled}` : ""}</span></span></label></li>`;
    return html`<div data-testid=${testid}>
      ${here.length ? html`<ul class="cl-pick">${here.map(row)}</ul>` : html`<p class="small muted">None found in this area.</p>`}
      ${other.length ? html`<details><summary class="small">Other areas (${other.length})</summary><ul class="cl-pick">${other.map(row)}</ul></details>` : nothing}
    </div>`;
  }

  private roomEditor(c: ClimateConfig, areaId: string): TemplateResult {
    const d = this.discovery!;
    const room = c.rooms[areaId];
    const unit = this.unit;
    const owner = new Map(Object.entries(c.rooms).flatMap(([a, r]) => r.thermostats.map((t) => [t.entity_id, a] as [string, string])));
    const set = (mutate: (r: RoomConfig) => void) => this.change((n) => mutate(n.rooms[areaId]));
    const suggestion = d.suggestions.rooms[areaId];
    const primary = room.primary_sensor?.entity_id ?? "";
    const sensors = d.sensors;
    const sensorNote = (s: (typeof sensors)[number]) => [s.value_c !== null ? temp(s.value_c, unit) : s.quality, s.on_thermostat ? "belongs to a heating device (measures at the radiator)" : null, s.outdoor ? "looks like an outdoor sensor" : null, s.not_room ? "probably not a room temperature" : null].filter(Boolean).join(" · ");
    const setO = (k: keyof Targets) => (v: number | null) => set((r) => { (r.overrides as unknown as Record<string, number | null>)[k] = v; });
    return html`<section class="card pad cl-section" data-testid="climate-room-editor" data-area=${areaId}>
      <div class="cl-row" style="justify-content:space-between"><h3>${this.areaName(areaId)}</h3>
        <span class="cl-row">${suggestion ? html`<button type="button" class="btn" data-testid="climate-use-suggestion" @click=${() => this.useSuggestion(areaId)}>${icon("plus", 18)}Fill empty fields from Home Assistant's area</button>` : nothing}
          <button type="button" class="btn btn-danger" data-testid="climate-remove-room" @click=${() => this.removeRoom(areaId)}>${icon("delete", 18)}Remove room</button></span></div>
      ${suggestion ? html`<p class="small muted">${suggestion.reason}. Suggested: ${[suggestion.thermostats.length ? `${suggestion.thermostats.length} heating device(s)` : null, suggestion.primary_sensor ? this.entityName(suggestion.primary_sensor) : null, suggestion.openings.length ? `${suggestion.openings.length} window/door sensor(s)` : null].filter(Boolean).join(", ")}.</p>` : nothing}
      <h4>Heating devices</h4>
      <p class="small muted">Radiator valves, thermostats and heaters (Home Assistant <span class="mono">climate</span> entities). Without one, the room is observed only. Each device belongs to one room; a device that heats several rooms is marked as shared in a thermal zone.</p>
      ${this.pickList(d.thermostats.map((t) => ({ entity_id: t.entity_id, name: t.name, area_id: t.area_id, area: t.area,
          note: [t.model, t.capabilities?.reports_action ? "reports its activity" : "does not report its activity", t.capabilities?.external_input ? "has an external-sensor input" : null].filter(Boolean).join(" · "),
          disabled: owner.has(t.entity_id) && owner.get(t.entity_id) !== areaId ? `assigned to ${this.areaName(owner.get(t.entity_id)!)}` : undefined })),
        areaId, (id) => room.thermostats.some((t) => t.entity_id === id),
        (id, on) => set((r) => { r.thermostats = this.toggle(r.thermostats, id, on, () => ({ entity_id: id, kind: d.thermostats.find((t) => t.entity_id === id)?.kind ?? "other" })); }), "climate-thermostats")}
      ${room.thermostats.length ? html`<div class="cl-form">${room.thermostats.map((t, i) => html`<label class="cl-field"><span class="field-label">Kind of ${this.entityName(t.entity_id)}</span>
          <select data-testid="climate-kind" @change=${(e: Event) => set((r) => { r.thermostats[i].kind = (e.target as HTMLSelectElement).value; })}>
            ${Object.entries(d.kinds).map(([k, label]) => html`<option value=${k} ?selected=${t.kind === k}>${label}</option>`)}</select></label>`)}</div>` : nothing}
      <h4>Room temperature</h4>
      <p class="small muted">The primary sensor is the room's temperature. It may be in another Home Assistant area. A thermostat's own reading is always kept separately and never used as the room temperature; Smart Comfort does not reconfigure a radiator valve to use this sensor.</p>
      <label class="cl-field"><span class="field-label">Primary sensor</span>
        <select data-testid="climate-primary" @change=${(e: Event) => { const v = (e.target as HTMLSelectElement).value; set((r) => { r.primary_sensor = v ? { entity_id: v } : null; r.secondary_sensors = r.secondary_sensors.filter((s) => s.entity_id !== v); }); }}>
          <option value="" ?selected=${!primary}>None (not measured)</option>
          <optgroup label="In this area">${sensors.filter((s) => s.area_id === areaId).map((s) => html`<option value=${s.entity_id} ?selected=${s.entity_id === primary}>${s.name} · ${sensorNote(s)}</option>`)}</optgroup>
          <optgroup label="Other areas">${sensors.filter((s) => s.area_id !== areaId).map((s) => html`<option value=${s.entity_id} ?selected=${s.entity_id === primary}>${s.name}${s.area ? ` (${s.area})` : ""} · ${sensorNote(s)}</option>`)}</optgroup>
        </select></label>
      <details ?open=${room.secondary_sensors.length > 0}><summary>Secondary sensors (${room.secondary_sensors.length})</summary>
        <p class="small muted">Used when the primary sensor is unavailable (their mean), and recorded for learning.</p>
        ${this.pickList(sensors.filter((s) => s.entity_id !== primary).map((s) => ({ entity_id: s.entity_id, name: s.name, area_id: s.area_id, area: s.area, note: sensorNote(s) })),
          areaId, (id) => room.secondary_sensors.some((s) => s.entity_id === id), (id, on) => set((r) => { r.secondary_sensors = this.toggle(r.secondary_sensors, id, on, () => ({ entity_id: id })); }), "climate-secondary")}
      </details>
      <h4>Windows and doors</h4>
      ${this.pickList(d.openings.map((o) => ({ entity_id: o.entity_id, name: o.name, area_id: o.area_id, area: o.area, note: `${o.kind} · ${o.status}` })),
        areaId, (id) => room.openings.some((o) => o.entity_id === id), (id, on) => set((r) => { r.openings = this.toggle(r.openings, id, on, () => ({ entity_id: id })); }), "climate-openings")}
      <details ?open=${Object.values(room.overrides).some((v) => v !== null)}><summary>Room preferences (override the defaults)</summary>
        <div class="cl-form">
          ${this.tempField("Comfort", room.overrides.comfort_c, setO("comfort_c"), { inherit: c.defaults.comfort_c })}
          ${this.tempField("Setback", room.overrides.setback_c, setO("setback_c"), { inherit: c.defaults.setback_c })}
          ${this.tempField("Lowest allowed target", room.overrides.min_target_c, setO("min_target_c"), { inherit: c.defaults.min_target_c })}
          ${this.tempField("Highest allowed target", room.overrides.max_target_c, setO("max_target_c"), { inherit: c.defaults.max_target_c })}
          ${this.numberField("Sensor current for (minutes)", room.overrides.stale_minutes, setO("stale_minutes"), { min: 5, max: 1440, placeholder: `${c.defaults.stale_minutes} (default)` })}
        </div><p class="small muted">Leave a field empty to use the default.</p></details>
    </section>`;
  }

  private rooms(c: ClimateConfig): TemplateResult {
    const d = this.discovery;
    if (!d) return html`<div class="card loading" role="status"><span class="spinner"></span><span>Discovering…</span></div>`;
    const configured = Object.keys(c.rooms).sort((a, b) => this.areaName(a).localeCompare(this.areaName(b)));
    const free = d.areas.filter((a) => !c.rooms[a.id]);
    const suggested = free.filter((a) => d.suggestions.rooms[a.id]);
    const selected = configured.includes(this.room) ? this.room : configured[0];
    return html`<div class="cl-section">
      <section class="card pad cl-section" data-testid="climate-room-list">
        <div class="cl-inline"><label class="cl-field"><span class="field-label">Add a room (a Home Assistant area)</span>
          <select data-testid="climate-add-area" @change=${(e: Event) => (this.addArea = (e.target as HTMLSelectElement).value)}>
            <option value="" ?selected=${!this.addArea}>Choose an area…</option>
            ${free.map((a) => html`<option value=${a.id} ?selected=${this.addArea === a.id}>${a.name}${d.suggestions.rooms[a.id] ? " · devices found" : ""}</option>`)}</select></label>
          <button type="button" class="btn btn-primary" data-testid="climate-add-room" ?disabled=${!this.addArea} @click=${() => this.addRoom(this.addArea)}>${icon("plus", 18)}Add</button></div>
        ${suggested.length ? html`<p class="small muted">Areas with heating devices or temperature sensors: ${suggested.map((a, i) => html`${i ? ", " : ""}<button type="button" class="link-btn" data-testid="climate-add-suggested" data-area=${a.id} @click=${() => { this.addRoom(a.id); this.useSuggestion(a.id); }}>${a.name}</button>`)} (adds the room with its suggested devices; review before saving).</p>` : nothing}
        ${configured.length ? html`<div class="cl-room-rows">${configured.map((a) => {
          const r = c.rooms[a];
          return html`<div class="cl-room-row ${a === selected ? "active" : ""}"><span><strong>${this.areaName(a)}</strong>
            <span class="small muted"> · ${r.thermostats.length} heating device(s) · ${r.primary_sensor ? this.entityName(r.primary_sensor.entity_id) : "no primary sensor"} · ${r.openings.length} window/door</span></span>
            <button type="button" class="btn btn-small" data-testid="climate-edit-room" data-area=${a} @click=${() => (this.room = a)}>${icon("pencil", 16)}Edit</button></div>`;
        })}</div>` : html`<p class="muted">No climate rooms yet.</p>`}
      </section>
      ${selected ? this.roomEditor(c, selected) : nothing}
    </div>`;
  }

  // ---------------------------------------------------------------- zones
  private zones(c: ClimateConfig): TemplateResult {
    const d = this.discovery;
    const roomIds = Object.keys(c.rooms);
    const zoneOf = new Map(c.zones.flatMap((z) => z.rooms.map((r) => [r, z.id] as [string, string])));
    const setZ = (i: number, mutate: (z: Zone) => void) => this.change((n) => mutate(n.zones[i]));
    const addZone = (rooms: string[], name: string | null, layout: string, origin: string) => this.change((n) => {
      const id = slugify(name || rooms.map((r) => this.areaName(r)).join(" "), new Set(n.zones.map((z) => z.id)));
      n.zones.push({ id, name: name || rooms.map((r) => this.areaName(r)).join(" + "), rooms, layout, shared_thermostats: [], heating_circuit: null, origin });
    });
    return html`<div class="cl-section" data-testid="climate-zones-editor">
      <p class="notice notice-info">${icon("info", 20)}<span><strong>Thermal zones are not presence zones.</strong> A presence zone is part of a room where people are detected. A thermal zone is a room or a group of rooms whose heat is related: an open-plan space, connected rooms, or rooms heated by one device or circuit. Each room keeps its own temperature readings.</span></p>
      ${(d?.suggestions.zones ?? []).filter((s) => !s.grouped).length ? html`<section class="card pad cl-section"><h3>Suggestions</h3>
        ${d!.suggestions.zones.filter((s) => !s.grouped).map((s) => html`<div class="cl-suggestion" data-testid="climate-zone-suggestion"><span><strong>${s.name ?? s.rooms.map((r) => this.areaName(r)).join(" + ")}</strong>
          <span class="small muted block">${s.reason}</span></span>
          ${s.rooms.every((r) => roomIds.includes(r) && !zoneOf.has(r))
            ? html`<button type="button" class="btn" @click=${() => addZone(s.rooms, s.name, s.layout, s.origin)}>${icon("plus", 18)}Group as a thermal zone</button>`
            : html`<span class="small muted">Add ${s.rooms.filter((r) => !roomIds.includes(r)).map((r) => this.areaName(r)).join(", ") || "the rooms"} as climate rooms first</span>`}</div>`)}
      </section>` : nothing}
      ${c.zones.map((z, i) => {
        const thermostats = z.rooms.flatMap((r) => c.rooms[r]?.thermostats ?? []);
        return html`<section class="cl-zone" data-testid="climate-zone" data-zone=${z.id}>
          <div class="cl-form">
            <label class="cl-field"><span class="field-label">Name</span><input type="text" maxlength="60" .value=${z.name} @change=${(e: Event) => setZ(i, (x) => { x.name = (e.target as HTMLInputElement).value; })} /></label>
            <label class="cl-field"><span class="field-label">Layout</span><select @change=${(e: Event) => setZ(i, (x) => { x.layout = (e.target as HTMLSelectElement).value; })}>
              ${Object.entries(d?.layouts ?? {}).map(([k, label]) => html`<option value=${k} ?selected=${z.layout === k}>${label}</option>`)}</select></label>
            <label class="cl-field"><span class="field-label">Heating circuit (optional)</span><input type="text" maxlength="60" .value=${z.heating_circuit ?? ""} placeholder="e.g. Floor heating ground floor" @change=${(e: Event) => setZ(i, (x) => { x.heating_circuit = (e.target as HTMLInputElement).value.trim() || null; })} /></label>
          </div>
          <fieldset class="cl-section" style="border:0;padding:0;margin:0"><legend class="field-label">Rooms</legend>
            <ul class="cl-pick">${roomIds.map((r) => html`<li><label><input type="checkbox" .checked=${z.rooms.includes(r)} ?disabled=${zoneOf.has(r) && zoneOf.get(r) !== z.id}
              @change=${(e: Event) => setZ(i, (x) => { x.rooms = (e.target as HTMLInputElement).checked ? [...x.rooms, r] : x.rooms.filter((y) => y !== r); x.shared_thermostats = x.shared_thermostats.filter((t) => x.rooms.some((y) => c.rooms[y]?.thermostats.some((th) => th.entity_id === t))); })} />
              <span class="cl-pick-body">${this.areaName(r)}${zoneOf.has(r) && zoneOf.get(r) !== z.id ? html`<span class="small muted">in another zone</span>` : nothing}</span></label></li>`)}</ul></fieldset>
          ${thermostats.length ? html`<fieldset class="cl-section" style="border:0;padding:0;margin:0"><legend class="field-label">Heating devices shared by the whole zone</legend>
            <ul class="cl-pick">${thermostats.map((t) => html`<li><label><input type="checkbox" .checked=${z.shared_thermostats.includes(t.entity_id)}
              @change=${(e: Event) => setZ(i, (x) => { x.shared_thermostats = (e.target as HTMLInputElement).checked ? [...x.shared_thermostats, t.entity_id] : x.shared_thermostats.filter((y) => y !== t.entity_id); })} />
              <span class="cl-pick-body">${this.entityName(t.entity_id)}<span class="small muted mono">${t.entity_id}</span></span></label></li>`)}</ul>
            <span class="cl-help">Leave all unchecked when every room's devices are controlled individually.</span></fieldset>` : nothing}
          <div><button type="button" class="btn btn-danger btn-small" @click=${() => this.change((n) => { n.zones.splice(i, 1); })}>${icon("delete", 16)}Remove zone</button></div>
        </section>`;
      })}
      <div><button type="button" class="btn" data-testid="climate-add-zone" ?disabled=${!roomIds.some((r) => !zoneOf.has(r))} @click=${() => { const r = roomIds.find((x) => !zoneOf.has(x)); if (r) addZone([r], null, "single", "manual"); }}>${icon("plus", 18)}New thermal zone</button>
        ${roomIds.length ? nothing : html`<span class="small muted"> Add climate rooms first.</span>`}</div>
    </div>`;
  }

  // -------------------------------------------------------------- outdoor
  private outdoor(c: ClimateConfig): TemplateResult {
    const d = this.discovery;
    const current = c.outdoor?.entity_id ?? "";
    const unit = this.unit;
    const others = (d?.sensors ?? []).filter((s) => !s.outdoor);
    return html`<section class="card pad cl-section" data-testid="climate-outdoor-editor">
      <p class="small muted">Used for indoor/outdoor differences now and for learning the outdoor influence later. A sensor is better than a weather service (it measures your site).</p>
      <label class="cl-field"><span class="field-label">Outdoor temperature source</span>
        <select data-testid="climate-outdoor" @change=${(e: Event) => { const v = (e.target as HTMLSelectElement).value; this.change((n) => { n.outdoor = v ? { entity_id: v } : null; }); }}>
          <option value="" ?selected=${!current}>None</option>
          <optgroup label="Outdoor sensors and weather">${(d?.outdoor ?? []).map((o) => html`<option value=${o.entity_id} ?selected=${o.entity_id === current}>${o.name} (${o.source === "weather" ? "weather" : "sensor"}) · ${o.value_c !== null ? temp(o.value_c, unit) : o.quality}</option>`)}</optgroup>
          <optgroup label="Other temperature sensors">${others.map((s) => html`<option value=${s.entity_id} ?selected=${s.entity_id === current}>${s.name}${s.area ? ` (${s.area})` : ""}</option>`)}</optgroup>
        </select></label>
      ${d?.suggestions.outdoor && !current ? html`<p><button type="button" class="btn" @click=${() => this.change((n) => { n.outdoor = { entity_id: d.suggestions.outdoor! }; })}>${icon("plus", 18)}Use ${this.entityName(d.suggestions.outdoor)}</button></p>` : nothing}
    </section>`;
  }

  // -------------------------------------------------------------- backups
  private backupsSection(): TemplateResult {
    const when = haTime(false);
    return html`<section class="card pad cl-section" data-testid="climate-backups">
      <div class="cl-row"><button type="button" class="btn" data-testid="climate-export" @click=${() => download(exportUrl)}>${icon("export", 18)}Export</button>
        <label class="btn file-btn">${icon("import", 18)}Import…<input type="file" accept="application/json,.json" data-testid="climate-import-file" @change=${(e: Event) => void this.pickImport(e)} /></label></div>
      <p class="small muted">A backup of the previous version is kept before every save (at most one every 10 minutes while editing), restore and import. The observations are not part of the configuration backups.</p>
      ${this.backups === null ? html`<p class="muted">Loading…</p>` : this.backups.length
        ? html`<ul class="plain-list">${this.backups.map((b) => html`<li class="cl-room-row"><span><strong>${b.created_at ? when(b.created_at) : b.id}</strong>
            <span class="small muted"> · ${b.reason ?? ""} · ${b.summary ? `${b.summary.rooms} rooms, ${b.summary.thermostats} heating devices, ${b.summary.zones} zones` : ""}</span></span>
            <button type="button" class="btn btn-small" data-testid="climate-restore" @click=${() => void this.openRestore(b.id)}>${icon("restore", 16)}Restore…</button></li>`)}</ul>`
        : html`<p class="muted">No backups yet.</p>`}
    </section>`;
  }

  private async pickImport(e: Event): Promise<void> {
    const input = e.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = "";
    if (!file) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(await file.text());
    } catch {
      this.error = new ApiError("The file is not valid JSON", "invalid_json", 400);
      return;
    }
    this.dialog = { kind: "import", file: parsed, review: null };
    await this.showDialog();
    try {
      const r = await importReview(parsed);
      if (this.dialog?.kind === "import") this.dialog = { ...this.dialog, review: r };
    } catch (err) {
      if (this.dialog?.kind === "import") this.dialog = { ...this.dialog, error: err };
    }
  }

  private async openRestore(backup: string): Promise<void> {
    this.dialog = { kind: "restore", backup, review: null };
    await this.showDialog();
    try {
      const r = await restoreReview(backup);
      if (this.dialog?.kind === "restore") this.dialog = { ...this.dialog, review: r };
    } catch (err) {
      if (this.dialog?.kind === "restore") this.dialog = { ...this.dialog, error: err };
    }
  }

  // --------------------------------------------------------------- review
  private async openReview(): Promise<void> {
    if (!this.draft || !this.doc) return;
    this.dialog = { kind: "save", review: null };
    await this.showDialog();
    try {
      const r = await review(this.draft, this.base);
      if (this.dialog?.kind === "save") this.dialog = { kind: "save", review: r };
    } catch (err) {
      if (this.dialog?.kind === "save") this.dialog = { kind: "save", review: null, error: err };
    }
  }

  private async showDialog(): Promise<void> {
    await this.updateComplete;
    const dialog = this.querySelector<HTMLDialogElement>("dialog.cl-review");
    if (dialog && !dialog.open) dialog.showModal();
  }

  private closeDialog(): void {
    this.querySelector<HTMLDialogElement>("dialog.cl-review")?.close();
    this.dialog = null;
  }

  private async confirm(): Promise<void> {
    const d = this.dialog;
    if (!d || !this.doc || !this.draft) return;
    this.busy = true;
    try {
      let doc: ClimateDocument;
      if (d.kind === "save") {
        const reply = await save(this.draft, this.base);
        doc = reply.document;
        this.notice = reply.result.saved ? `Saved${reply.result.backup ? "; the previous version is kept as a backup" : ""}.` : "Nothing changed.";
      } else if (d.kind === "restore") {
        doc = (await restore(d.backup, this.doc.revision)).document;
        this.notice = "The backup was restored; the version before it is kept as a backup.";
      } else {
        doc = (await importApply(d.file, this.doc.revision)).document;
        this.notice = "The file was imported; the version before it is kept as a backup.";
      }
      this.doc = doc;
      this.draft = clone(doc.config);
      this.base = doc.revision;
      this.closeDialog();
      if (this.currentSection === "backups") void this.loadBackups();
    } catch (err) {
      if (this.dialog) this.dialog = { ...this.dialog, error: err } as Dialog;
    } finally {
      this.busy = false;
    }
  }

  private describe(ch: Change): string {
    const parts = ch.path.split(".");
    const unit = this.unit;
    const value = (v: unknown, key: string): string => {
      if (v === null || v === undefined || (Array.isArray(v) && !v.length)) return key.startsWith("overrides") || parts.includes("overrides") ? "default" : "none";
      if (Array.isArray(v)) return v.map((x) => this.entityName(String(x).split(" (")[0]) + (String(x).includes(" (") ? ` (${String(x).split(" (")[1]}` : "")).join(", ");
      if (typeof v === "number" && key.endsWith("_c")) return temp(v, unit);
      if (typeof v === "string" && v.includes(".")) return this.entityName(v);
      return String(v);
    };
    if (ch.kind === "room_added") return `Climate room added: ${this.areaName(String(ch.after))}`;
    if (ch.kind === "room_removed") return `Climate room removed: ${this.areaName(String(ch.before))}`;
    if (ch.kind === "zone_added") return `Thermal zone added: ${String(ch.after)}`;
    if (ch.kind === "zone_removed") return `Thermal zone removed: ${String(ch.before)}`;
    let where = "";
    let key = parts.slice(1).join(".");
    if (parts[0] === "rooms") { where = `${this.areaName(parts[1])}: `; key = parts.slice(2).join("."); }
    else if (parts[0] === "zones") { where = `Zone ${this.draft?.zones.find((z) => z.id === parts[1])?.name ?? parts[1]}: `; key = parts.slice(2).join("."); }
    else if (parts[0] === "defaults") where = "Default ";
    else if (parts[0] === "outdoor") key = "outdoor.entity_id";
    const last = key.replace(/^overrides\./, "");
    const label = LABEL[key] ?? LABEL[last] ?? key;
    return `${where}${key.startsWith("overrides.") ? `${label} (room override)` : label}: ${value(ch.before, last)} → ${value(ch.after, last)}`;
  }

  private dialogTemplate(): TemplateResult | typeof nothing {
    const d = this.dialog;
    if (!d) return nothing;
    const r = d.review;
    const title = d.kind === "save" ? "Review changes" : d.kind === "restore" ? "Restore a backup" : "Import a file";
    const ready = Boolean(r && r.valid && r.writable && !(d.kind === "save" && r.stale) && !r.identical && store.link !== "offline");
    return html`<dialog class="sheet sheet-full review-dialog cl-review" aria-labelledby="cl-review-title" data-testid="climate-review" @close=${() => (this.dialog = null)}>
      <div class="sheet-inner">
        <header class="sheet-head"><h2 id="cl-review-title">${title}</h2>
          <button type="button" class="icon-btn" aria-label="Close" @click=${() => this.closeDialog()}>${icon("close")}</button></header>
        <div class="sheet-body">
          ${d.error ? problemText(d.error) : nothing}
          ${!r && !d.error ? html`<p class="muted" role="status"><span class="spinner" aria-hidden="true"></span> Checking…</p>` : nothing}
          ${r ? html`
            ${r.stale && d.kind === "save" ? html`<p class="notice notice-warning" data-testid="climate-stale">${icon("warning", 20)}<span>The configuration was changed in another session after you started these changes, so they cannot be saved. Use <strong>Discard</strong> to start from the current version, then make your changes again.</span></p>` : nothing}
            ${!r.writable ? html`<p class="notice notice-error">${icon("error", 20)}<span>The climate configuration cannot be written: ${this.doc?.status.read_only}</span></p>` : nothing}
            ${r.errors.length ? html`<section class="review-block" data-group="errors"><h3 class="tone-error">Cannot be saved</h3><ul class="change-list">${r.errors.map((e) => html`<li>${icon("error", 18)}<span>${e.message}<span class="cl-review-path block">${e.path}</span></span></li>`)}</ul></section>` : nothing}
            ${r.valid && r.identical ? html`<p class="muted">Nothing changes compared with the stored configuration.</p>` : nothing}
            ${r.changes.length ? html`<section class="review-block" data-group="changes"><h3>Changes</h3><ul class="change-list">${r.changes.map((c) => html`<li>${icon(c.kind.endsWith("added") ? "plus" : c.kind.endsWith("removed") ? "minus" : "pencil", 18)}<span>${this.describe(c)}</span></li>`)}</ul></section>` : nothing}
            ${r.warnings.length ? html`<section class="review-block" data-group="warnings"><h3>Please check</h3><ul class="change-list">${r.warnings.map((w) => html`<li>${icon("warning", 18)}<span>${w.message}</span></li>`)}</ul></section>` : nothing}
            <p class="muted small">Saving stores the configuration in the App and starts recording the assigned entities. No thermostat or other device is changed.</p>` : nothing}
        </div>
        <footer class="sheet-foot">
          <button type="button" class="btn" @click=${() => this.closeDialog()}>Cancel</button>
          <button type="button" class="btn btn-primary" data-testid="climate-confirm" ?disabled=${!ready || this.busy} @click=${() => void this.confirm()}>${icon("save", 18)}${this.busy ? "Saving…" : d.kind === "save" ? "Save" : d.kind === "restore" ? "Restore" : "Import"}</button>
        </footer>
      </div>
    </dialog>`;
  }

  // --------------------------------------------------------------- render
  protected render(): unknown {
    const c = this.draft;
    const section = this.currentSection;
    const dirty = this.dirty();
    const header = pageHeader("Climate settings", "Rooms, heating devices, sensors, thermal zones and preferences. Changes are saved after a review.");
    if (!c || !this.doc) {
      return html`${header}${climateTabs("settings")}${this.error ? problemText(this.error) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`}`;
    }
    return html`${header}
      ${observingNotice()}
      ${climateTabs("settings")}
      ${this.error ? problemText(this.error) : nothing}
      ${this.doc.status.read_only ? html`<p class="notice notice-error">${icon("error", 20)}<span>${this.doc.status.read_only}</span></p>` : nothing}
      ${this.notice ? html`<p class="notice" role="status" data-testid="climate-notice">${icon("ok", 20)}<span>${this.notice}</span></p>` : nothing}
      <div class="cl-settings">
        <nav class="cl-side" aria-label="Climate settings">${SECTIONS.map((s) => html`<a href="#/climate/settings/${s.key}" class=${s.key === section ? "active" : ""} aria-current=${s.key === section ? "page" : "false"} data-section=${s.key}>${icon(s.icon, 20)}${s.title}</a>`)}</nav>
        <div>${section === "general" ? this.general(c) : section === "rooms" ? this.rooms(c) : section === "zones" ? this.zones(c) : section === "outdoor" ? this.outdoor(c) : this.backupsSection()}</div>
      </div>
      ${dirty ? html`<div class="unsaved-bar visible" role="region" aria-label="Unsaved changes" data-testid="climate-unsaved">
        <span class="unsaved-text">${icon("pencil", 20)}<span><strong>Unsaved changes</strong><span class="muted"> · saved only after you review them</span></span></span>
        <span class="row-actions"><button type="button" class="btn" data-testid="climate-discard" @click=${() => { this.draft = clone(this.doc!.config); this.base = this.doc!.revision; this.notice = null; }}>${icon("undo", 20)}Discard</button>
          <button type="button" class="btn btn-primary" data-testid="climate-review-button" ?disabled=${store.link === "offline"} @click=${() => void this.openReview()}>${icon("eye", 20)}Review changes</button></span>
      </div>` : nothing}
      ${this.dialogTemplate()}`;
  }
}
customElements.define("sl-climate-settings", SlClimateSettings);
