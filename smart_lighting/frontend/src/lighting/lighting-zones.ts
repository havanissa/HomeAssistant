// Zones (0.6.0 / 0.6.8; App 0.9.0 split by owner). One editor, two explicit modes:
//
// * ``presence`` (Presence → a room → Zones): the zone itself - name and stable id, enabled, detection (room
//   sensors or a radar distance band), timing, availability policy, holding the room occupied - through the
//   Presence-owned commands ``smart_occupancy/zones/set|remove|radar_test`` (detection keys only). A zone exists
//   whether or not a light responds to it; each zone shows what responds to it, with a link to Lighting.
// * ``lighting`` (Lighting → a room → Zone lighting rules): which lights respond to each zone and how (targets,
//   priority between zones, manual changes, daylight, release) through ``zone/set`` with lighting keys only, which
//   rule controls each light now, and the room's shared space. Zones are created and their detection is edited in
//   Presence (linked). Saving sends no command in either mode.
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { chip, kv, StoreElement } from "../ui";
import { cmd, lighting } from "./api";
import { inspectButton } from "./light-inspector";
import {
  advancedToggle,
  card,
  fmtSecs,
  numberField,
  selectField,
  segments,
  stateText,
  toggleField,
  type EditorModel,
  type LightingHost,
  type Obj,
  type RoomModel,
} from "./lighting-model";

const TIMING: Array<[string, string]> = [["correlation_window", "Confirmation window"], ["inactivity_timeout", "Hold after the last detection"],
  ["exit_confirmation", "Exit confirmation"], ["dropout_grace", "Sensor dropout grace"]];
const FAILSAFE: Record<string, string> = { hold: "keeps its state", vacant: "treated as vacant", occupied: "treated as occupied" };

function radarDefaults(r: Obj | null | undefined): Obj {
  const n = (v: unknown, d: number | null) => (v === null || v === undefined || v === "" ? d : Number(v));
  return { presence: r?.presence ?? "", distance: r?.distance ?? "", min_m: n(r?.min_m, 1), max_m: n(r?.max_m, 3), attribute: r?.attribute || null, unit: r?.unit || null,
    entry_delay: n(r?.entry_delay, 2), exit_delay: n(r?.exit_delay, 15), hysteresis: n(r?.hysteresis, 0.2), max_age: r?.max_age ? Number(r.max_age) : null };
}

const DETECTION = ["id", "name", "enabled", "detection", "radar", "sensors", "activation", "timing", "unavailable", "holds_room"];

export class SlLpZones extends StoreElement {
  static properties = {
    room: { attribute: false }, model: { attribute: false }, host: { attribute: false }, busy: { type: Boolean }, canEdit: { type: Boolean },
    mode: { type: String },
    draft: { state: true }, more: { state: true }, test: { state: true }, askDelete: { state: true }, space: { state: true },
  };
  /** ``presence``: detection (Presence → Zones); ``lighting``: the zones' lighting rules (Lighting → Zone lighting rules). */
  declare mode: "presence" | "lighting";
  declare room: RoomModel;
  declare model: EditorModel | null;
  declare host: LightingHost;
  declare busy: boolean;
  declare canEdit: boolean;
  declare draft: Obj | null;
  declare more: boolean;
  declare test: { distance: number | null; presence: boolean; result: Obj | null; error: string | null };
  declare askDelete: boolean;
  declare space: Obj | null;

  constructor() {
    super();
    this.draft = null;
    this.more = false;
    this.test = { distance: null, presence: true, result: null, error: null };
    this.askDelete = false;
    this.space = null;
    this.mode = "lighting";
  }

  private get presence(): boolean {
    return this.mode === "presence";
  }

  private zoneCmd(name: "set" | "remove" | "radar_test"): string {
    return this.presence ? `smart_occupancy/zones/${name}` : cmd(`zone/${name}`);
  }

  private presenceLink(zoneId?: string): string {
    return `#/presence/${encodeURIComponent(this.room.area_id)}?tab=zones${zoneId ? `&zone=${encodeURIComponent(zoneId)}` : ""}`;
  }

  private get z(): Obj {
    return this.room.zones ?? {};
  }

  private send(name: string, params: Obj, done = "Saved"): Promise<boolean> {
    return this.host.run(() => lighting(name.startsWith("smart_occupancy/") ? name : cmd(name), params), done);
  }

  /** Open a zone for editing (``?zone=`` from a link). */
  openZone(zoneId: string): void {
    const zone = (this.z.zones ?? []).find((x: Obj) => x.id === zoneId);
    if (zone && this.canEdit) this.edit(zone);
  }

  private enrolled(): Set<string> {
    return new Set((this.room.brightness?.lights ?? []).filter((l: Obj) => l.matrix).map((l: Obj) => l.entity_id));
  }

  private edit(zone: Obj | null, radar = false): void {
    this.askDelete = false;
    this.more = false;
    this.test = { distance: null, presence: true, result: null, error: null };
    this.draft = zone ? JSON.parse(JSON.stringify({ ...zone, status: undefined })) : {
      name: `Zone ${(this.z.zones?.length ?? 0) + 1}`, enabled: true, detection: radar ? "radar" : "sensors", radar: radar ? radarDefaults(null) : null,
      sensors: [], activation: "possible", priority: 50, targets: [], timing: {}, unavailable: "hold", holds_room: true, manual: "respect", daylight: "room", on_below: null, release: "restore",
    };
  }

  private patch(p: Obj): void {
    this.draft = { ...this.draft, ...p };
  }

  private patchRadar(p: Obj): void {
    this.draft = { ...this.draft, radar: { ...radarDefaults(this.draft?.radar), ...p } };
    this.test = { ...this.test, result: null };
  }

  private async save(): Promise<void> {
    const d = this.draft!;
    let zone: Obj;
    if (this.presence) {
      // Presence owns the zone's identity and detection: nothing else is sent.
      zone = { name: String(d.name ?? "").trim() || "Zone", enabled: !!d.enabled, detection: d.detection ?? "sensors", unavailable: d.unavailable, holds_room: !!d.holds_room };
      if (zone.detection === "radar") zone.radar = radarDefaults(d.radar);
      else {
        zone.sensors = d.sensors ?? [];
        zone.activation = d.activation;
        zone.timing = Object.fromEntries(TIMING.map(([k]) => [k, d.timing?.[k] ?? null]));
      }
      for (const k of Object.keys(zone)) if (!DETECTION.includes(k)) delete zone[k];
    } else {
      // Lighting owns which lights respond and how.
      zone = {
        priority: Number(d.priority) || 1,
        targets: (d.targets ?? []).map((t: Obj) => ({ entity_id: t.entity_id, action: t.action ?? "on", brightness_pct: t.brightness_pct ?? null, color_temp_kelvin: t.color_temp_kelvin ?? null, transition: t.transition ?? null })),
        manual: d.manual, daylight: d.daylight, on_below: d.daylight === "below" ? d.on_below ?? null : null, release: d.release,
      };
    }
    if (d.id) zone.id = d.id;
    const ok = await this.send(this.zoneCmd("set"), { area_id: this.room.area_id, zone }, d.id ? "Zone saved · nothing was sent" : "Zone added · nothing was sent");
    if (ok) this.draft = null;
  }

  private async runTest(): Promise<void> {
    const radar = radarDefaults(this.draft?.radar);
    try {
      const res = await lighting<Obj>(this.zoneCmd("radar_test"), { area_id: this.room.area_id, radar, distance_m: this.test.distance, presence: this.test.presence, active: false, unavailable: this.draft?.unavailable ?? "hold" });
      const active = await lighting<Obj>(this.zoneCmd("radar_test"), { area_id: this.room.area_id, radar, distance_m: this.test.distance, presence: this.test.presence, active: true, unavailable: this.draft?.unavailable ?? "hold" });
      this.test = { ...this.test, result: { fresh: res, held: active }, error: null };
    } catch (err) {
      this.test = { ...this.test, result: null, error: err instanceof Error ? err.message : String(err) };
    }
  }

  // ------------------------------------------------------------------ list
  private zoneStatus(zone: Obj): [string, "ok" | "warning" | "neutral"] {
    const st = zone.status;
    if (!zone.enabled) return ["Off", "neutral"];
    if (!st) return ["—", "neutral"];
    if (st.failsafe) return [`Sensor unavailable (${FAILSAFE[st.failsafe] ?? st.failsafe})`, "warning"];
    return st.active ? ["Occupied", "ok"] : ["Vacant", "neutral"];
  }

  private zoneCard(zone: Obj): TemplateResult {
    const z = this.z;
    const [word, tone] = this.zoneStatus(zone);
    const rs = zone.status?.radar;
    const enrolled = this.enrolled();
    const sensors = zone.detection === "radar"
      ? (rs?.configured ? `Radar ${rs.band} · ${rs.presence_name}: ${rs.presence === true ? "detected" : rs.presence === false ? "clear" : "unavailable"} · distance ${rs.distance_text ?? "—"}` : "Radar distance zone: radar entities not chosen")
      : (zone.sensors ?? []).map((e: string) => z.sensors?.find((s: Obj) => s.entity_id === e)?.name ?? e).join(", ") || "no sensor";
    const lights = this.presence
      ? ((zone.lighting?.lights ?? []).length ? `Lights responding: ${zone.lighting.lights.map((l: Obj) => l.name).join(", ")}` : "No light responds to it (an occupancy signal only)")
      : (zone.targets ?? []).map((t: Obj) => `${z.targets?.find((x: Obj) => x.entity_id === t.entity_id)?.name ?? t.entity_id} ${enrolled.has(t.entity_id) ? "(brightness matrix)" : t.action === "off" ? "off" : t.action === "unchanged" ? "unchanged" : stateText({ state: "on", ...t })}`).join(" · ") || "no light";
    const selected = this.draft?.id === zone.id;
    return html`<li><button type="button" class="lp-zone ${selected ? "selected" : ""}" aria-pressed=${selected ? "true" : "false"} data-zone=${zone.id} ?disabled=${!this.canEdit} title=${this.canEdit ? (this.presence ? "Edit this zone" : "Edit its lighting rules") : "Read only"} @click=${() => this.edit(zone)}>
      <span class="lp-zone-icon">${icon(zone.detection === "radar" ? "radar" : "layers", 22)}</span>
      <span class="lp-pick-main"><span class="lp-row-line"><strong>${zone.name}</strong>${chip(tone, word)}${this.presence ? (zone.holds_room ? html`<span class="small muted">holds the room</span>` : nothing) : html`<span class="small muted">priority ${zone.priority}</span>`}</span>
        <span class="small muted">${sensors}</span><span class="small">${lights}</span></span>${this.canEdit ? icon("pencil", 20) : nothing}</button></li>`;
  }

  // ------------------------------------------------------------------ form
  private radarBand(cfg: Obj, status: Obj | null): TemplateResult {
    if (!(cfg.max_m > cfg.min_m)) return html`<p class="small tone-warning">The maximum distance must be larger than the minimum.</p>`;
    const dist = (this.z.radar?.distance ?? []).find((x: Obj) => x.entity_id === cfg.distance);
    const now = dist?.valid ? Number(dist.metres) : null;
    const t = this.test.distance;
    const h = Number(cfg.hysteresis) || 0;
    const scale = Math.max(cfg.max_m + h + 1, now !== null ? now + 0.5 : 0, t !== null ? t + 0.5 : 0, 4);
    const pc = (m: number) => `${Math.max(0, Math.min(100, (m / scale) * 100)).toFixed(2)}%`;
    return html`<div class="lp-band" role="img" aria-label=${`Radar band ${cfg.min_m} to ${cfg.max_m} m${now !== null ? `; target ${now} m` : ""}`}>
      <div class="lp-band-bar">
        <span class="lp-band-margin" style=${`left:${pc(cfg.min_m - h)};width:calc(${pc(cfg.max_m + h)} - ${pc(cfg.min_m - h)})`}></span>
        <span class="lp-band-active" style=${`left:${pc(cfg.min_m)};width:calc(${pc(cfg.max_m)} - ${pc(cfg.min_m)})`}></span>
        ${now !== null ? html`<span class="lp-band-now" style=${`left:${pc(now)}`} title="Distance now"><b>${now.toFixed(2)} m</b></span>` : nothing}
        ${t !== null ? html`<span class="lp-band-test" style=${`left:${pc(t)}`} title="Test distance"><b>test ${t} m</b></span>` : nothing}
      </div>
      <div class="lp-band-scale small muted"><span>0 m (radar)</span><span>${scale.toFixed(1)} m</span></div></div>
      ${kv([["Zone", status ? chip(status.state === "active" ? "ok" : status.state === "unavailable" ? "warning" : "neutral", status.state === "active" ? "Active" : status.state === "unavailable" ? "Unavailable" : "Inactive") : "not saved yet"],
        ["Distance now", dist ? (dist.valid ? `${dist.metres} m (${dist.state} ${dist.unit ?? ""})` : dist.problem ?? dist.state) : "—"],
        ["Band", `${cfg.min_m}–${cfg.max_m} m, margin ±${h} m`]])}
      <p class="small muted">One reported distance is a band around the radar (every direction it sees), and it cannot tell several people apart.</p>`;
  }

  private radarForm(d: Obj): TemplateResult {
    const z = this.z;
    const lim = z.limits?.radar ?? { distance: [0, 50], delay: [0, 600], hysteresis: [0, 2], max_age: [1, 3600], units: [] };
    const c = radarDefaults(d.radar);
    const presence: Obj[] = z.radar?.presence ?? [];
    const paired: string[] = (presence.find((p) => p.entity_id === c.presence)?.distances ?? []).map((x: Obj) => x.entity_id);
    const dists: Obj[] = [...(z.radar?.distance ?? [])].sort((a, b) => Number(paired.includes(b.entity_id)) - Number(paired.includes(a.entity_id)));
    const status = (z.zones ?? []).find((x: Obj) => x.id === d.id)?.status?.radar ?? null;
    const t = this.test;
    const num = (k: string, label: string, min: number, max: number, step: number, unit: string, help?: string) =>
      numberField({ label, value: c[k], min, max, step, unit, help, onChange: (v) => this.patchRadar({ [k]: v }) });
    return html`<div class="lp-form lp-form-1">
      ${selectField({ label: "Radar presence entity", value: c.presence, options: [["", "Choose…"], ...presence.map((p) => [p.entity_id, `${p.name} · ${p.state ?? "—"}${p.distances.length ? " · has a distance sensor" : ""}`] as [string, string])],
        onChange: (v) => { const pd = presence.find((p) => p.entity_id === v)?.distances ?? []; this.patchRadar({ presence: v, ...(pd.length === 1 && !c.distance ? { distance: pd[0].entity_id } : {}) }); } })}
      ${selectField({ label: "Distance sensor", value: c.distance, options: [["", "Choose…"], ...dists.map((x) => [x.entity_id, `${x.name} · ${x.valid ? `${x.metres} m` : x.problem ?? x.state}${paired.includes(x.entity_id) ? " · same device" : ""}`] as [string, string])],
        help: c.presence && !paired.length ? "No distance sensor of the same device was found: choose one yourself." : undefined, onChange: (v) => this.patchRadar({ distance: v }) })}
      <div class="lp-form">${num("min_m", "Active from", lim.distance[0], lim.distance[1], 0.05, "m")}${num("max_m", "to", lim.distance[0], lim.distance[1], 0.05, "m")}</div>
    </div>
    ${this.radarBand(c, status)}
    <div class="lp-subcard" data-testid="lighting-radar-test"><h4 class="lp-h">Try a distance (nothing is sent)</h4>
      <div class="lp-inline">${numberField({ label: "Test distance", value: t.distance, min: 0, max: lim.distance[1], step: 0.05, unit: "m", onChange: (v) => (this.test = { ...t, distance: v, result: null }) })}
        ${toggleField({ label: "Radar detects presence", checked: t.presence, onChange: (v) => (this.test = { ...t, presence: v, result: null }) })}
        <button type="button" class="btn btn-small" ?disabled=${!c.presence || !c.distance} @click=${() => void this.runTest()}>${icon("radar", 16)}Test</button></div>
      ${t.error ? html`<p class="small tone-error">${t.error}</p>` : nothing}
      ${t.result ? html`<p class="small"><strong>${t.result.fresh.where}</strong>: an inactive zone ${t.result.fresh.settled.active ? "becomes active" : "stays inactive"} (${t.result.fresh.settled.reason});
        an active zone ${t.result.held.settled.active ? "stays active" : "is released"} (${t.result.held.settled.reason}). Nothing was sent.</p>` : nothing}
    </div>
    ${advancedToggle(this.more, () => (this.more = !this.more), "Delays, hysteresis and units")}
    ${this.more ? html`<div class="lp-form">
      ${num("entry_delay", "Enter after", 0, lim.delay[1], 0.5, "s")}${num("exit_delay", "Release after", 0, lim.delay[1], 1, "s")}
      ${num("hysteresis", "Hysteresis", 0, lim.hysteresis[1], 0.05, "m", "An active zone stays active this far outside the band.")}
      ${numberField({ label: "Ignore a distance older than", value: c.max_age, min: lim.max_age[0], max: lim.max_age[1], step: 10, unit: "s", placeholder: "no limit", onChange: (v) => this.patchRadar({ max_age: v }) })}
      <label class="lp-field"><span class="lp-label">Distance attribute</span><input type="text" maxlength="64" .value=${c.attribute ?? ""} placeholder="(the sensor's state)" @change=${(e: Event) => this.patchRadar({ attribute: (e.target as HTMLInputElement).value.trim() || null })} /></label>
      ${selectField({ label: "Unit", value: c.unit ?? "", options: [["", "The sensor's unit"], ...(lim.units ?? []).map((u: Obj) => [u.id, u.name] as [string, string])], onChange: (v) => this.patchRadar({ unit: v || null }) })}
    </div>` : nothing}`;
  }

  private targetsForm(d: Obj): TemplateResult {
    const z = this.z;
    const lim = z.limits ?? {};
    const enrolled = this.enrolled();
    const targets: Obj[] = d.targets ?? [];
    const setT = (entityId: string, p: Obj | null) => this.patch({
      targets: p === null ? targets.filter((t) => t.entity_id !== entityId) : targets.some((t) => t.entity_id === entityId)
        ? targets.map((t) => (t.entity_id === entityId ? { ...t, ...p } : t)) : [...targets, { entity_id: entityId, action: "on", ...p }],
    });
    return html`<ul class="plain-list lp-zone-targets">${(z.targets ?? []).map((t: Obj) => {
      const cur = targets.find((x) => x.entity_id === t.entity_id);
      const caps = t.capabilities ?? {};
      const relay = t.type === "relay";
      return html`<li class="lp-subcard"><label class="lp-check"><input type="checkbox" .checked=${!!cur} @change=${(e: Event) => setT(t.entity_id, (e.target as HTMLInputElement).checked ? {} : null)} />
          <span><strong>${t.name}</strong> ${relay ? html`<span class="small muted">relay · on/off only</span>` : caps.known === false ? html`<span class="small muted">capabilities unknown</span>` : nothing}</span></label>${inspectButton(t.entity_id, this.room.area_id, t.name)}
        ${cur && enrolled.has(t.entity_id) ? html`<p class="small muted">On/off and brightness come from its brightness matrix (this zone becomes a column there).</p>`
        : cur ? html`<div class="lp-form">
          ${selectField({ label: "Action", value: cur.action ?? "on", options: [["on", "On"], ["off", "Off"], ["unchanged", "Unchanged (claimed, left as it is)"]], onChange: (v) => setT(t.entity_id, { action: v }) })}
          ${cur.action === "on" && caps.brightness && !relay ? numberField({ label: "Brightness", value: cur.brightness_pct, placeholder: "on", unit: "%", min: 1, max: 100, onChange: (v) => setT(t.entity_id, { brightness_pct: v }) }) : nothing}
          ${cur.action === "on" && caps.color_temp && !relay ? numberField({ label: "Colour temperature", value: cur.color_temp_kelvin, placeholder: "unchanged", unit: "K", step: 50, min: caps.min_kelvin ?? lim.kelvin?.[0], max: caps.max_kelvin ?? lim.kelvin?.[1], onChange: (v) => setT(t.entity_id, { color_temp_kelvin: v }) }) : nothing}
          ${cur.action !== "unchanged" && caps.transition && !relay ? numberField({ label: "Fade", value: cur.transition, unit: "s", step: 0.5, min: 0, max: lim.transition?.[1] ?? 300, onChange: (v) => setT(t.entity_id, { transition: v }) }) : nothing}
        </div>` : nothing}</li>`;
    })}${!(z.targets ?? []).length ? html`<li class="muted small">The room has no controlled light.</li>` : nothing}</ul>
    <p class="small muted">Only what each light supports is offered (a relay switches on and off). Unchanged: the zone keeps the light as it is, and lower rules do not change it.</p>`;
  }

  private form(d: Obj): TemplateResult {
    const z = this.z;
    const radarOk = (z.radar?.distance ?? []).some((x: Obj) => x.valid || x.problem === "unavailable");
    const radar = d.detection === "radar";
    return html`<aside class="inspector lp-inspector lp-zone-form" aria-label=${d.id ? `Zone ${d.name}` : "New zone"} data-testid="lighting-zone-form">
      <header class="inspector-head"><span class="inspector-icon">${icon(radar ? "radar" : "layers", 22)}</span>
        <div><h3>${d.id ? d.name : "New zone"}</h3><span class="muted small">${d.id ? `id ${d.id}` : "not saved yet"}</span></div>
        <button type="button" class="icon-btn" aria-label="Close" @click=${() => (this.draft = null)}>${icon("close")}</button></header>
      ${this.presence ? this.detectionForm(d, radar, radarOk) : this.rulesForm(d)}
    </aside>`;
  }

  /** Lighting → Zone lighting rules: the lights that respond to the zone and how. */
  private rulesForm(d: Obj): TemplateResult {
    const lim = this.z.limits ?? {};
    return html`<section class="inspector-section"><p class="small muted">Detection (sensors, radar band, timing) is set in <a href=${this.presenceLink(d.id)}>Presence → ${this.room.name} → Zones</a>.</p>
        <div class="lp-form lp-form-1">${numberField({ label: "Priority", value: d.priority, min: lim.priority?.[0] ?? 1, max: lim.priority?.[1] ?? 100, help: "Higher wins between zones for a light both list.", onChange: (v) => this.patch({ priority: v ?? 1 }) })}</div></section>
      <section class="inspector-section"><h4>Lights that respond to this zone</h4>${this.targetsForm(d)}</section>
      <section class="inspector-section"><h4>Manual changes, daylight and release</h4><div class="lp-form lp-form-1">
          ${selectField({ label: "Manual changes", value: d.manual, options: [["respect", "Respected"], ["override", "Zone wins over earlier ones"]], onChange: (v) => this.patch({ manual: v }) })}
          ${selectField({ label: "Switch on in daylight", value: d.daylight, options: [["room", "Only when the room needs light"], ["always", "Always (ignore light level)"], ["below", "Below its own light level"]], onChange: (v) => this.patch({ daylight: v }) })}
          ${d.daylight === "below" ? numberField({ label: "Its light level", value: d.on_below, unit: "lx", min: lim.lux?.[0] ?? 1, max: lim.lux?.[1] ?? 5000, onChange: (v) => this.patch({ on_below: v }) }) : nothing}
          ${selectField({ label: "When the zone ends", value: d.release, options: [["restore", "Room rules take over"], ["leave", "Lights stay as they are"]], onChange: (v) => this.patch({ release: v }) })}
        </div></section>
      <section class="inspector-section"><div class="row-actions lp-row">
        <button type="button" class="btn" @click=${() => (this.draft = null)}>Cancel</button>
        <button type="button" class="btn btn-primary" data-testid="lighting-zone-save" ?disabled=${this.busy} @click=${() => void this.save()}>${icon("save", 18)}Save lighting rules</button></div>
        <p class="small muted">Saving sends no command: the lights follow at the zone's next change or the next lighting trigger.</p></section>`;
  }

  /** Presence → Zones: the zone's identity and detection. */
  private detectionForm(d: Obj, radar: boolean, radarOk: boolean): TemplateResult {
    const z = this.z;
    const lim = z.limits ?? {};
    const uses = d.id ? (z.zones ?? []).find((x: Obj) => x.id === d.id)?.lighting : null;
    const hasUses = !!uses && ((uses.lights ?? []).length > 0 || (uses.brightness ?? []).length > 0);
    return html`
      <section class="inspector-section"><div class="lp-form lp-form-1">
        <label class="lp-field"><span class="lp-label">Name</span><input type="text" maxlength="60" .value=${d.name ?? ""} @input=${(e: Event) => this.patch({ name: (e.target as HTMLInputElement).value })} /></label>
        ${toggleField({ label: "Enabled", checked: !!d.enabled, help: d.id ? "Switched off, the zone ends: if it is occupied, its lights follow the remaining rules at once." : undefined, onChange: (v) => this.patch({ enabled: v }) })}
        <div class="lp-field"><span class="lp-label">Detection</span>${segments<string>(d.detection ?? "sensors", [["sensors", "Room sensors", "motion-sensor"], ...(radarOk || radar ? [["radar", "Radar distance", "radar"] as [string, string, string]] : [])],
          (v) => this.patch({ detection: v, radar: v === "radar" ? radarDefaults(d.radar) : d.radar }), { label: "Detection" })}
          ${radarOk ? nothing : html`<span class="small muted">Radar distance needs a distance sensor in metres, centimetres or millimetres.</span>`}</div>
      </div></section>
      <section class="inspector-section"><h4>${radar ? "Radar" : "Presence sensors of this zone"}</h4>
        ${radar ? this.radarForm(d) : html`<ul class="plain-list">${(z.sensors ?? []).map((s: Obj) => html`<li><label class="lp-check"><input type="checkbox" .checked=${(d.sensors ?? []).includes(s.entity_id)}
            @change=${(e: Event) => this.patch({ sensors: (e.target as HTMLInputElement).checked ? [...(d.sensors ?? []), s.entity_id] : (d.sensors ?? []).filter((x: string) => x !== s.entity_id) })} />
          <span>${s.name} <span class="small muted">${s.kind_name}${s.used ? "" : " · not used for occupancy"}</span></span></label></li>`)}
          ${!(z.sensors ?? []).length ? html`<li class="muted small">The room has no sensors.</li>` : nothing}</ul>
          ${selectField({ label: "Occupied", value: d.activation, options: [["possible", "At the first detection"], ["confirmed", "When presence is confirmed"]], onChange: (v) => this.patch({ activation: v }) })}`}
      </section>
      <section class="inspector-section"><h4>Timing and availability</h4><div class="lp-form lp-form-1">
          ${toggleField({ label: "Keeps the room occupied while this zone is occupied", checked: !!d.holds_room, onChange: (v) => this.patch({ holds_room: v }) })}
          ${selectField({ label: radar ? "If the distance or radar is unavailable" : "If its sensors are unavailable", value: d.unavailable,
            options: radar ? [["hold", "Keep its state while presence is detected"], ["vacant", "Release it (after the exit delay)"], ["occupied", "Active while presence is detected"]]
              : [["hold", "Keep its last state"], ["vacant", "Treat as vacant"], ["occupied", "Occupied while the room is"]], onChange: (v) => this.patch({ unavailable: v }) })}
          ${radar ? html`<p class="small muted">Entry and exit delays and hysteresis of the radar band are under “Delays, hysteresis and units” above.</p>`
            : advancedToggle(!!d._more, () => this.patch({ _more: !d._more }), "Occupancy timing of this zone")}
          ${!radar && d._more ? TIMING.map(([k, label]) => numberField({ label, value: d.timing?.[k], placeholder: z.room_timing?.[k], unit: "s", min: lim.timing?.[k]?.[0], max: lim.timing?.[k]?.[1],
            tag: html`<span class="inherit-tag ${d.timing?.[k] !== null && d.timing?.[k] !== undefined ? "override" : ""}">${d.timing?.[k] !== null && d.timing?.[k] !== undefined ? "zone value" : `room (${fmtSecs(z.room_timing?.[k])})`}</span>`,
            onChange: (v) => this.patch({ timing: { ...(d.timing ?? {}), [k]: v } }) })) : nothing}
        </div></section>
      ${d.id ? html`<section class="inspector-section" data-testid="zone-lighting-uses"><h4>Lighting</h4>
        ${hasUses ? html`<p class="small">${(uses.lights ?? []).length ? `Lights responding: ${uses.lights.map((l: Obj) => l.name).join(", ")}.` : ""}
            ${(uses.brightness ?? []).length ? ` Brightness matrix cells: ${uses.brightness.map((b: Obj) => `${b.name} (${b.cells})`).join(", ")}.` : ""}</p>`
          : html`<p class="small muted">No light responds to this zone: it is an occupancy signal of ${this.room.name} only.</p>`}
        <a class="text-link" href=${`#/lighting/${encodeURIComponent(this.room.area_id)}/zones`}>Zone lighting rules ${icon("chevron-right", 16)}</a></section>` : nothing}
      <section class="inspector-section"><div class="row-actions lp-row">
        <button type="button" class="btn" @click=${() => (this.draft = null)}>Cancel</button>
        <button type="button" class="btn btn-primary" data-testid="presence-zone-save" ?disabled=${this.busy || (radar && (!d.radar?.presence || !d.radar?.distance))} @click=${() => void this.save()}>${icon("save", 18)}${d.id ? "Save zone" : "Add zone"}</button></div>
        ${d.id ? this.askDelete ? html`<p class="lp-confirm" data-testid="zone-delete-confirm">Delete ${d.name}?${hasUses ? html` <strong>Lights respond to it:</strong> ${[...(uses.lights ?? []).map((l: Obj) => l.name), ...(uses.brightness ?? []).map((b: Obj) => `${b.name}'s matrix column`)].join(", ")} ${(uses.brightness ?? []).length ? "(those brightness values are deleted with it)" : ""}.` : ""} If it is active, its lights follow the remaining rules at once.
              <button type="button" class="btn btn-danger btn-small" data-testid="zone-delete" @click=${async () => { if (await this.send(this.zoneCmd("remove"), { area_id: this.room.area_id, zone_id: d.id, ...(this.presence ? { confirm: true } : {}) }, "Zone deleted")) this.draft = null; }}>Delete zone</button>
              <button type="button" class="btn btn-small" @click=${() => (this.askDelete = false)}>Cancel</button></p>`
          : html`<button type="button" class="btn btn-danger btn-small lp-gap" @click=${() => (this.askDelete = true)}>${icon("delete", 16)}Delete zone…</button>` : nothing}
        <p class="small muted">Saving sends no command: the zone's new state is used from now on, and its lights follow at the zone's next change.</p></section>`;
  }

  // ------------------------------------------------------------------ space
  private spaceCard(): TemplateResult {
    const z = this.z;
    const sp = z.space;
    const s = this.space;
    const dis = !this.canEdit || this.busy;
    const moods = (this.model?.moods ?? []).map((m) => m.name);
    const rooms: Obj[] = z.space_rooms ?? [];
    const others: Obj[] = (z.spaces ?? []).filter((x: Obj) => !sp || x.id !== sp.id);
    const body = s ? html`<div class="lp-form lp-form-1">
        <label class="lp-field"><span class="lp-label">Name</span><input type="text" maxlength="60" .value=${s.name ?? ""} @input=${(e: Event) => (this.space = { ...s, name: (e.target as HTMLInputElement).value })} /></label>
        <div class="lp-field"><span class="lp-label">Rooms</span><ul class="plain-list">${rooms.map((x) => html`<li><label class="lp-check"><input type="checkbox" .checked=${(s.rooms ?? []).includes(x.area_id)}
            @change=${(e: Event) => (this.space = { ...s, rooms: (e.target as HTMLInputElement).checked ? [...(s.rooms ?? []), x.area_id] : (s.rooms ?? []).filter((a: string) => a !== x.area_id) })} />
          <span>${x.name}${x.adjacent ? html` <span class="small muted">adjacent</span>` : nothing}</span></label></li>`)}</ul></div>
        ${selectField({ label: "Mood", value: s.mood_override ?? "", options: [["", "Each room's own mood"], ...moods.map((m) => [m, m] as [string, string])], onChange: (v) => (this.space = { ...s, mood_override: v || null }) })}
        ${selectField({ label: "When a room is vacant", value: s.vacancy ?? "independent", options: [["independent", "It runs its own vacancy action"], ["together", "It waits until the whole space is vacant"]], onChange: (v) => (this.space = { ...s, vacancy: v }) })}
      </div>
      <div class="row-actions lp-row"><button type="button" class="btn" @click=${() => (this.space = null)}>Cancel</button>
        <button type="button" class="btn btn-primary" ?disabled=${dis || !String(s.name ?? "").trim()} @click=${async () => {
          if (await this.send("space/set", { space: { ...(s.id ? { id: s.id } : {}), name: String(s.name).trim(), rooms: s.rooms, mood_override: s.mood_override ?? null, vacancy: s.vacancy ?? "independent" } }, "Shared space saved")) this.space = null; }}>${icon("save", 18)}Save shared space</button></div>`
      : sp ? html`<p><strong>${sp.name}</strong>: ${(sp.rooms ?? []).map((a: string) => sp.room_names?.[a] ?? a).join(", ")}</p>
          <p class="small muted">Mood: ${sp.mood_override ?? "each room's own"} · ${sp.vacancy === "together" ? "vacant together" : "each room vacates on its own"}</p>
          ${dis ? nothing : html`<div class="row-actions lp-row"><button type="button" class="btn btn-small" @click=${() => (this.space = { ...sp })}>${icon("pencil", 16)}Edit</button>
            <button type="button" class="btn btn-small" @click=${() => void this.send("space/set", { space: { id: sp.id, rooms: (sp.rooms ?? []).filter((a: string) => a !== this.room.area_id) } }, `${this.room.name} left the shared space`)}>${icon("link-off", 16)}Remove ${this.room.name} from it</button></div>`}`
      : html`<p class="muted">${this.room.name} is not part of a shared space.</p>
          ${dis ? nothing : html`<div class="row-actions lp-row">
            <button type="button" class="btn btn-small" @click=${() => (this.space = { name: "Shared space", rooms: [this.room.area_id], mood_override: null, vacancy: "independent" })}>${icon("plus", 16)}Create a shared space</button>
            ${others.map((o) => html`<button type="button" class="btn btn-small" @click=${() => void this.send("space/set", { space: { id: o.id, rooms: [...(o.rooms ?? []), this.room.area_id] } }, `${this.room.name} joined ${o.name}`)}>${icon("link", 16)}Join ${o.name}</button>`)}</div>`}`;
    return card("Shared space", "open-plan", html`<p class="small muted">Rooms that form one open-plan space. Each keeps its own occupancy, zones and lights; a light both rooms control is never switched off by one while the other is occupied. All shared spaces: <a href="#/lighting/settings/spaces">Lighting settings</a>.</p>${body}`, { testid: "lighting-space" });
  }

  protected render(): unknown {
    const z = this.z;
    if (!this.room.zones) return html`<p class="muted">Zones are not available for this room${this.presence ? " (it is not enabled, or lighting is not running)" : ""}.</p>`;
    const dis = !this.canEdit || this.busy;
    const max = z.limits?.max_zones ?? 8;
    const radarOk = (z.radar?.distance ?? []).some((x: Obj) => x.valid || x.problem === "unavailable");
    const rules: Obj[] = z.rules ?? [];
    const list = (z.zones ?? []).length ? html`<ul class="lp-zone-list">${z.zones.map((x: Obj) => this.zoneCard(x))}</ul>` : html`<p class="muted">No zones in this room.</p>`;
    if (this.presence) {
      const hist: Obj[] = z.history ?? [];
      return html`<div class="lp-split ${this.draft ? "lp-has-inspector" : ""}">
        <div class="lp-main">
          ${card("Zones", "layers", html`
            <p class="small muted">Parts of ${this.room.name} where presence is detected on its own (a desk, a sink, a reading chair): by some of the room's sensors, or by a radar's distance band. A zone is an occupancy signal of the room. Lighting can respond to it (Lighting → ${this.room.name} → Zone lighting), and it can keep the room occupied. Home Assistant's areas are not changed.</p>
            ${list}
            ${dis || (z.zones ?? []).length >= max ? nothing : html`<div class="row-actions lp-row">
              <button type="button" class="btn btn-small" data-testid="presence-zone-add" @click=${() => this.edit(null)}>${icon("plus", 16)}Add zone</button>
              ${radarOk ? html`<button type="button" class="btn btn-small" data-testid="presence-radar-zone-add" @click=${() => this.edit(null, true)}>${icon("radar", 16)}Add radar distance zone</button>` : nothing}</div>`}`,
            { wide: true, testid: "presence-zones-editor" })}
          <div class="lp-two">
            ${card("Recent zone changes", "history", hist.length ? html`<ul class="lp-dlist">${[...hist].reverse().map((h: Obj) => html`<li><span class="mono small">${String(h.at ?? "").slice(11, 19)}</span> <strong>${h.name}</strong> ${h.event}${h.distance !== undefined ? ` · ${h.distance} m` : ""}${h.reason ? html` <span class="small muted">${h.reason}</span>` : nothing}</li>`)}</ul>`
              : html`<p class="muted small">No zone change recorded yet.</p>`, { testid: "presence-zone-history" })}
            ${card("Shared space", "open-plan", z.space ? html`<p><strong>${z.space.name}</strong>: ${(z.space.rooms ?? []).map((a: string) => z.space.room_names?.[a] ?? a).join(", ")}</p>
                <p class="small muted">Each room keeps its own occupancy and zones. How the space's lights behave is set in <a href="#/lighting/settings/spaces">Lighting settings</a>.</p>`
              : html`<p class="muted small">${this.room.name} is not part of a shared space (<a href="#/lighting/settings/spaces">Lighting settings</a>).</p>`, { testid: "presence-zone-space" })}
          </div>
        </div>
        ${this.draft ? this.form(this.draft) : nothing}
      </div>`;
    }
    return html`<div class="lp-split ${this.draft ? "lp-has-inspector" : ""}">
      <div class="lp-main">
        ${card("Zone lighting rules", "layers", html`
          <p class="small muted">Which lights respond while a zone of ${this.room.name} is occupied, and how. A zone's lighting wins over the mood while it is occupied; the higher priority wins between zones. Select a zone to change its lighting rules.</p>
          ${list}
          <p class="lp-zone-links small"><a class="btn btn-small" data-testid="lighting-zones-presence-link" href=${this.presenceLink()}>${icon("motion-sensor", 16)}Add zones or change their detection in Presence</a></p>`,
          { wide: true, testid: "lighting-zones" })}
        <div class="lp-two">
          ${card("Which rule controls each light now", "list", rules.length ? html`<div class="table-wrap"><table class="plain-table lp-table"><thead><tr><th scope="col">Light</th><th scope="col">Rule</th><th scope="col">Requests</th><th scope="col">Why</th></tr></thead>
            <tbody>${rules.map((x) => html`<tr><td><span class="cell-main">${x.name}${inspectButton(x.entity_id, this.room.area_id, x.name)}</span></td><td>${x.rule ?? "—"}${x.superseded?.length ? html`<span class="block small muted">over ${x.superseded.join(", ")}</span>` : nothing}</td>
              <td>${stateText(x.requested)}</td><td class="small muted">${x.reason}</td></tr>`)}</tbody></table></div>` : html`<p class="muted small">No rule is active.</p>`)}
          ${this.spaceCard()}
        </div>
      </div>
      ${this.draft ? this.form(this.draft) : nothing}
    </div>`;
  }
}

customElements.define("sl-lp-zones", SlLpZones);
