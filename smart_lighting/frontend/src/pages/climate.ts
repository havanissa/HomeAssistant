// Phase A7.0: Climate (Smart Comfort). Observation only: room temperatures, thermostats and what they *report*,
// windows and doors, thermal zones, configuration completeness and the observation history. Routes:
// #/climate (Overview) · #/climate/rooms (list) · #/climate/rooms/<area_id> (one room) · #/climate/settings/<section>.
import "../climate/climate.css";

import { html, nothing, type PropertyValues, type TemplateResult } from "lit";

import {
  age, ApiError, delta, getHistory, getOverview, getRoom, LEARNING_TONE, openingLabel, QUALITY, rate, rebuild, temp, WINDOW_LABELS,
  type History, type LearningSummary, type OpeningView, type Overview, type Reading, type RoomView, type ThermostatView, type Unit,
} from "../climate/api";
import { historyChart, legend } from "../climate/chart";
import { icon } from "../icons";
import { haTime } from "../presence/components";
import { store } from "../store";
import { chip, pageHeader, StoreElement } from "../ui";

export const CLIMATE_TABS = [
  { key: "overview", title: "Overview", icon: "view-dashboard", href: "#/climate" },
  { key: "rooms", title: "Rooms", icon: "home-thermometer", href: "#/climate/rooms" },
  { key: "settings", title: "Settings", icon: "cog", href: "#/climate/settings" },
] as const;

export function climateTabs(active: string): TemplateResult {
  return html`<nav class="tabs" aria-label="Climate">
    ${CLIMATE_TABS.map((t) => html`<a href=${t.href} class=${t.key === active ? "active" : ""} aria-current=${t.key === active ? "page" : "false"} data-tab=${t.key}>${icon(t.icon, 20)}${t.title}</a>`)}
  </nav>`;
}

export function observingNotice(): TemplateResult {
  return html`<p class="notice notice-info cl-mode" data-testid="climate-observing">${icon("observe", 20)}<span><strong>Observing.</strong>
    Smart Comfort watches temperatures and heating devices and learns how each room warms and cools from them.
    It does not change any thermostat: automatic heating comes in a later version, only after simulation and your explicit activation.</span></p>`;
}

export function problemText(err: unknown): TemplateResult {
  const message = err instanceof ApiError ? err.message : String(err);
  return html`<p class="notice notice-error" role="alert">${icon("error", 20)}<span>${message}</span></p>`;
}

export function qualityChip(r: Reading): TemplateResult {
  const q = QUALITY[r.quality] ?? { label: r.quality, tone: "neutral" as const };
  return chip(q.tone, q.label);
}

export function heatingChip(t: ThermostatView): TemplateResult {
  if (t.heating.basis === "unavailable") return chip("error", "Unavailable");
  if (t.heating.basis === "not_reported") return chip("neutral", "Activity not reported");
  if (t.heating.state === "heating") return html`<span class="chip chip-warning">${icon("fire", 14)}<span class="chip-label">Heating (reported)</span></span>`;
  return chip("ok", `${t.hvac_action ?? "Idle"} (reported)`);
}

export function setpoint(t: ThermostatView, unit: Unit): string {
  if (t.target_c !== null) return temp(t.target_c, unit);
  if (t.target_low_c !== null || t.target_high_c !== null) return `${temp(t.target_low_c, unit)} – ${temp(t.target_high_c, unit)}`;
  return "—";
}

/** Monitoring | Thermal learning, inside one climate room. */
export function roomTabs(areaId: string, active: "monitoring" | "learning"): TemplateResult {
  const base = `#/climate/rooms/${encodeURIComponent(areaId)}`;
  return html`<nav class="cl-subtabs" aria-label="Room views">
    <a href=${base} class=${active === "monitoring" ? "active" : ""} aria-current=${active === "monitoring" ? "page" : "false"} data-subtab="monitoring">${icon("home-thermometer", 18)}Monitoring</a>
    <a href="${base}/learning" class=${active === "learning" ? "active" : ""} aria-current=${active === "learning" ? "page" : "false"} data-subtab="learning">${icon("chart", 18)}Thermal learning</a>
  </nav>`;
}

/** "Window open: Stova window" — what it is (set by a person or Home Assistant's device class), and which. */
export function openingChip(o: OpeningView): TemplateResult {
  const name = o.name ?? o.entity_id;
  const what = openingLabel(o.kind);
  const kindIcon = o.kind === "door" ? "door-open" : "window-open";
  return html`<span class="chip chip-info" data-testid="climate-open-chip" data-entity=${o.entity_id} title=${o.kind ? `${what} (${o.kind_source === "configured" ? "set in Climate settings" : "Home Assistant device class"})` : "Set whether this is a window or a door in Climate → Settings → Rooms and devices"}>${icon(kindIcon, 14)}<span class="chip-label">${what} open: ${name}</span></span>`;
}

export function learningChip(l: LearningSummary): TemplateResult {
  return chip(LEARNING_TONE[l.status] ?? "neutral", l.validity.state === "invalidated" ? "Model invalidated" : l.status_text);
}

/** The thermal-learning summary on a room card (Overview). */
export function learningSummary(l: LearningSummary, unit: Unit, areaId: string): TemplateResult {
  const progress = l.usable_days ?? 0;
  return html`<div class="cl-learn" data-testid="climate-room-learning" data-status=${l.status}>
    <div class="cl-row">${learningChip(l)}${l.running ? html`<span class="small muted"><span class="spinner"></span> training…</span>` : l.queued ? html`<span class="small muted">queued</span>` : nothing}
      ${l.validity.state === "outdated" ? chip("warning", "Outdated") : nothing}</div>
    <div class="cl-learn-row"><span>Usable history <strong>${l.usable_days !== null ? `${progress.toFixed(1)} of ${l.min_history_days} days` : `${l.observed_days.toFixed(1)} days observed`}</strong></span>
      ${l.confidence ? html`<span>Confidence <strong>${l.confidence.level} (${l.confidence.score})</strong></span>` : nothing}</div>
    ${l.heating || l.cooling ? html`<div class="cl-learn-row">
      ${l.heating ? html`<span>Heating <strong>${l.heating.value !== null ? `${rate(l.heating.value, unit)}${l.heating.basis === "inferred" ? " (inferred)" : ""}` : "unavailable"}</strong></span>` : nothing}
      ${l.cooling?.time_constant_h ? html`<span>Time constant <strong>${l.cooling.time_constant_h.toFixed(0)} h</strong></span>` : nothing}
      ${l.accuracy ? html`<span>Accuracy (${l.accuracy.horizon_min / 60} h) <strong>${delta(l.accuracy.mae, unit).replace("+", "")}</strong> vs. ${delta(l.accuracy.persistence_mae, unit).replace("+", "")} persistence</span>` : nothing}
    </div>` : nothing}
    ${l.trained_at ? html`<span class="small muted">Last trained ${haTime(false)(l.trained_at)}</span>` : nothing}
    ${l.validity.state === "invalidated" ? html`<span class="small tone-warning">${l.validity.reasons[0]}</span>` : nothing}
    ${l.prerequisites.filter((p) => p.blocking).map((p) => html`<span class="small tone-warning">${p.text}</span>`)}
    <a class="text-link small" href="#/climate/rooms/${encodeURIComponent(areaId)}/learning" data-testid="climate-learning-link">Thermal learning details</a>
  </div>`;
}

/** Refetch on live climate changes, at most once a second. */
abstract class ClimateElement extends StoreElement {
  private seenVersion = -1;
  private timer: number | undefined;

  protected willUpdate(changed: PropertyValues): void {
    super.willUpdate(changed);
    if (store.climateVersion !== this.seenVersion) {
      const first = this.seenVersion === -1;
      this.seenVersion = store.climateVersion;
      window.clearTimeout(this.timer);
      if (first) void this.reload();
      else this.timer = window.setTimeout(() => void this.reload(), 1000);
    }
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    window.clearTimeout(this.timer);
  }

  abstract reload(): Promise<void>;
}

// ------------------------------------------------------------------ overview
export class SlClimate extends ClimateElement {
  static properties = { tab: { type: String }, data: { state: true }, error: { state: true }, busy: { state: true }, actionError: { state: true } };
  declare tab: string;
  declare data: Overview | null;
  declare error: unknown;
  declare busy: boolean;
  declare actionError: unknown;

  constructor() {
    super();
    this.tab = "overview";
    this.data = null;
    this.error = null;
    this.busy = false;
    this.actionError = null;
  }

  private async rebuildAll(): Promise<void> {
    this.busy = true;
    try {
      await rebuild(null);
      this.actionError = null;
      await this.reload();
    } catch (err) {
      this.actionError = err;
    } finally {
      this.busy = false;
    }
  }

  async reload(): Promise<void> {
    try {
      this.data = await getOverview();
      this.error = null;
    } catch (err) {
      this.error = err;
    }
  }

  private roomCard(r: RoomView, unit: Unit): TemplateResult {
    const t = r.temperature;
    const devices = r.thermostats;
    return html`<article class="cl-room-card" data-testid="climate-room-card" data-area=${r.area_id}>
      <h3><a href="#/climate/rooms/${encodeURIComponent(r.area_id)}">${r.name}</a>
        ${r.completeness.ready ? nothing : chip("warning", "Incomplete")}</h3>
      <div class="cl-figures">
        <div class="cl-figure"><span class="cl-temp" data-testid="climate-room-temp">${temp(t.value_c, unit)}</span>
          <span class="stat-label">${t.source === "primary" ? "room sensor" : t.source === "secondary" ? "secondary sensors (primary not usable)" : "room temperature not measured"}</span></div>
        ${devices.length ? html`<div class="cl-figure"><span class="cl-temp-small">${devices.map((d) => setpoint(d, unit)).join(" · ")}</span><span class="stat-label">${devices.length === 1 ? "setpoint" : "setpoints"}</span></div>` : nothing}
        ${r.outdoor_difference_c !== null ? html`<div class="cl-figure"><span class="cl-temp-small">${delta(r.outdoor_difference_c, unit)}</span><span class="stat-label">vs. outdoor</span></div>` : nothing}
      </div>
      <div class="cl-row">
        ${devices.length ? devices.map((d) => heatingChip(d)) : chip("neutral", "No heating device: observed only")}
        ${r.openings.filter((o) => o.status === "open").map((o) => openingChip(o))}
        ${r.occupancy ? chip(r.occupancy.occupied ? "ok" : "neutral", r.occupancy.occupied ? "Occupied" : r.occupancy.occupied === false ? "Vacant" : "Occupancy unknown") : nothing}
        ${r.zone ? chip("info", `Zone: ${r.zone.name}`) : nothing}
      </div>
      ${r.demand ? html`<p class="small muted">${icon("info", 14)} ${r.demand.text}</p>` : nothing}
      ${r.learning ? learningSummary(r.learning, unit, r.area_id) : nothing}
    </article>`;
  }

  private overviewTab(d: Overview): TemplateResult {
    const unit = d.unit;
    if (!d.rooms.length) {
      return html`<div class="card empty-state" data-testid="climate-empty">
        <p><strong>No climate rooms yet.</strong></p>
        <p class="muted">Climate works with or without heating devices: a room can be just a temperature sensor. Smart Comfort discovers thermostats, radiator valves, temperature sensors, windows and doors in Home Assistant and suggests them; nothing is assigned until you review and save.</p>
        <p><a class="btn btn-primary" href="#/climate/settings/rooms" data-testid="climate-setup">${icon("plus", 18)}Set up climate rooms</a></p>
      </div>`;
    }
    const heating = d.rooms.reduce((n, r) => n + r.heating.reported, 0);
    const unreported = d.rooms.reduce((n, r) => n + r.heating.not_reported, 0);
    return html`
      <div class="stat-tiles cl-tiles" data-testid="climate-tiles">
        <div class="stat"><span class="stat-value">${d.rooms.length}</span><span class="stat-label">rooms</span></div>
        <div class="stat"><span class="stat-value ${d.completeness.ready < d.rooms.length ? "tone-warning" : "tone-ok"}">${d.completeness.ready}/${d.rooms.length}</span><span class="stat-label">rooms complete</span></div>
        <div class="stat"><span class="stat-value" data-testid="climate-outdoor">${temp(d.outdoor.value_c, unit)}</span><span class="stat-label">outdoor${d.outdoor.entity_id ? "" : " (no source)"}</span></div>
        <div class="stat"><span class="stat-value">${heating}</span><span class="stat-label">devices report heating${unreported ? ` · ${unreported} do not report` : ""}</span></div>
        <div class="stat"><span class="stat-value ${d.problems.length ? "tone-warning" : ""}">${d.problems.length}</span><span class="stat-label">sensor problems</span></div>
      </div>
      <div class="cl-grid-rooms" style="margin-top:1rem">${d.rooms.map((r) => this.roomCard(r, unit))}</div>
      <div class="cl-two" style="margin-top:1rem">
        <section class="card pad" data-testid="climate-problems"><h3>Missing and unavailable</h3>
          ${d.problems.length
            ? html`<ul class="cl-problem-list">${d.problems.map((p) => html`<li>${chip(QUALITY[p.quality]?.tone ?? "warning", p.text)}<span>${p.room ? html`<strong>${p.room}</strong> · ` : nothing}${p.role}</span><span class="mono small muted">${p.entity_id}</span></li>`)}</ul>`
            : html`<p class="muted">Every assigned sensor reports a current, plausible value.</p>`}
        </section>
        <section class="card pad" data-testid="climate-learning"><h3>Thermal learning</h3>
          <p>${d.learning.history_days} of ${d.learning.min_history_days} days observed for the shortest room history.</p>
          <div class="progress"><span style="width:${Math.min(100, Math.round((100 * d.learning.history_days) / Math.max(1, d.learning.min_history_days)))}%"></span></div>
          ${d.learning.counts ? html`<div class="cl-row" style="margin-top:0.5rem" data-testid="climate-learning-counts">${Object.entries(d.learning.counts).map(([k, n]) => chip(LEARNING_TONE[k] ?? "neutral", `${n} · ${d.learning.statuses?.[k] ?? k}`))}</div>` : nothing}
          <p class="small muted">${d.learning.text} ${d.status.observation.tracked} entities are recorded; ${(d.status.observation.bytes / 1048576).toFixed(1)} MB stored${d.status.observation.backfill.state === "running" ? "; reading the Recorder's history…" : ""}.
            Rooms are trained automatically about once a day when new observations exist.</p>
          ${d.learning.running ? html`<p class="small" role="status"><span class="spinner"></span> Training ${d.rooms.find((r) => r.area_id === d.learning.running)?.name ?? d.learning.running}…</p>` : nothing}
          <div class="cl-actions"><button type="button" class="btn" data-testid="climate-rebuild-all" ?disabled=${this.busy} @click=${() => void this.rebuildAll()}>${icon("refresh", 18)}Rebuild all models</button></div>
          ${this.actionError ? problemText(this.actionError) : nothing}
          ${(d.learning.notices ?? []).map((n) => html`<p class="notice notice-warning">${icon("warning", 18)}<span>${n.message}</span></p>`)}
          ${d.status.observation.error ? html`<p class="notice notice-warning">${icon("warning", 18)}<span>${d.status.observation.error}</span></p>` : nothing}
        </section>
      </div>
      ${d.zones.length ? html`<section class="card pad" style="margin-top:1rem" data-testid="climate-zones"><h3>Thermal zones</h3>
        <ul class="plain-list">${d.zones.map((z) => html`<li><strong>${z.name}</strong> <span class="muted">· ${z.names?.join(", ")}${z.shared_thermostats.length ? ` · shared: ${z.shared_thermostats.join(", ")}` : " · devices controlled per room"}</span></li>`)}</ul></section>` : nothing}`;
  }

  private roomsTab(d: Overview): TemplateResult {
    const unit = d.unit;
    if (!d.rooms.length) return this.overviewTab(d);
    return html`<div class="table-wrap card" data-testid="climate-rooms-table"><table class="plain-table">
      <thead><tr><th>Room</th><th>Room temperature</th><th>Thermostat reports</th><th>Setpoint</th><th>Mode / activity</th><th>Occupancy</th><th>Windows / doors</th><th>Sensors</th><th>Learning</th></tr></thead>
      <tbody>${d.rooms.map((r) => html`<tr>
        <td><a class="text-link" href="#/climate/rooms/${encodeURIComponent(r.area_id)}">${r.name}</a></td>
        <td>${temp(r.temperature.value_c, unit)}${r.temperature.source === "secondary" ? html` <span class="small muted">(secondary)</span>` : nothing}</td>
        <td>${r.thermostats.length ? r.thermostats.map((t) => temp(t.measured.value_c, unit)).join(" · ") : "—"}</td>
        <td>${r.thermostats.length ? r.thermostats.map((t) => setpoint(t, unit)).join(" · ") : "—"}</td>
        <td>${r.thermostats.length ? html`<span class="cl-row">${r.thermostats.map((t) => html`<span class="small">${t.hvac_mode ?? "—"}</span>${heatingChip(t)}`)}</span>` : html`<span class="muted">no device</span>`}</td>
        <td>${r.occupancy ? (r.occupancy.occupied ? "Occupied" : r.occupancy.occupied === false ? "Vacant" : "Unknown") : "—"}</td>
        <td>${r.openings.length ? (r.openings.some((o) => o.status === "open") ? r.openings.filter((o) => o.status === "open").map((o) => `${openingLabel(o.kind)} open: ${o.name ?? o.entity_id}`).join(" · ") : `${r.openings.length} closed`) : "—"}</td>
        <td>${qualityChip(r.primary)} <span class="small muted">${age(r.primary.age_s)}</span></td>
        <td>${r.learning ? html`<a class="text-link" href="#/climate/rooms/${encodeURIComponent(r.area_id)}/learning">${learningChip(r.learning)}</a>` : "—"}</td>
      </tr>`)}</tbody></table></div>`;
  }

  protected render(): unknown {
    const tab = this.tab === "rooms" ? "rooms" : "overview";
    const d = this.data;
    return html`${pageHeader("Climate", "Room temperatures, heating devices, windows and thermal zones of the home.",
      html`<div class="page-actions"><a class="btn" href="#/climate/settings" data-testid="climate-settings-button">${icon("cog", 18)}Settings</a></div>`)}
      ${observingNotice()}
      ${climateTabs(tab)}
      ${this.error ? problemText(this.error) : nothing}
      ${d?.status.notices.map((n) => html`<p class="notice notice-warning">${icon("warning", 20)}<span>${n.message}</span></p>`)}
      ${!d ? (this.error ? nothing : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`)
        : tab === "rooms" ? this.roomsTab(d) : this.overviewTab(d)}`;
  }
}
customElements.define("sl-climate", SlClimate);

// --------------------------------------------------------------------- room
export class SlClimateRoom extends ClimateElement {
  static properties = { areaId: { type: String }, room: { state: true }, history: { state: true }, window: { state: true },
    error: { state: true }, historyError: { state: true }, muted: { state: true } };
  declare areaId: string;
  declare room: RoomView | null;
  declare history: History | null;
  declare window: number;
  declare error: unknown;
  declare historyError: unknown;
  declare muted: Set<string>;
  private lastHistory = 0;

  constructor() {
    super();
    this.areaId = "";
    this.room = null;
    this.history = null;
    this.window = 86400;
    this.error = null;
    this.historyError = null;
    this.muted = new Set();
  }

  protected willUpdate(changed: PropertyValues): void {
    if (changed.has("areaId") && changed.get("areaId") !== undefined) {
      this.room = null;
      this.history = null;
      void this.reload(true);
    }
    super.willUpdate(changed);
  }

  async reload(forceHistory = false): Promise<void> {
    const area = this.areaId;
    if (!area) return;
    try {
      const room = await getRoom(area);
      if (area !== this.areaId) return;
      this.room = room;
      this.error = null;
    } catch (err) {
      this.error = err;
    }
    // The history comes from the App's store: re-read it at most every 30 s on live changes.
    if (forceHistory || !this.history || Date.now() - this.lastHistory > 30_000) await this.loadHistory();
  }

  private async loadHistory(): Promise<void> {
    const area = this.areaId;
    this.lastHistory = Date.now();
    try {
      const h = await getHistory(area, this.window);
      if (area === this.areaId) {
        this.history = h;
        this.historyError = null;
      }
    } catch (err) {
      this.historyError = err;
    }
  }

  private setWindow(seconds: number): void {
    this.window = seconds;
    void this.loadHistory();
  }

  private reading(label: string, r: Reading, unit: Unit, note?: string): TemplateResult {
    return html`<li class="cl-device" data-entity=${r.entity_id ?? ""}>
      <span class="cl-device-name">${label}: ${r.name ?? r.entity_id ?? "—"}</span>
      <span>${qualityChip(r)}</span>
      <div class="cl-figures"><span class="cl-temp-small">${temp(r.value_c, unit)}</span>
        <span class="small muted">reported ${r.raw ?? "—"}${r.unit ? ` ${r.unit}` : ""} · ${age(r.age_s)}</span></div>
      <span class="cl-device-meta mono">${r.entity_id ?? ""}${note ? html` · <span class="muted">${note}</span>` : nothing}</span>
    </li>`;
  }

  private thermostat(t: ThermostatView, unit: Unit): TemplateResult {
    const caps = t.capabilities;
    return html`<li class="cl-device" data-testid="climate-thermostat" data-entity=${t.entity_id}>
      <span class="cl-device-name">${icon("radiator", 18)} ${t.name ?? t.entity_id}</span>
      <span>${heatingChip(t)}</span>
      <div class="cl-figures">
        <div class="cl-figure"><span class="cl-temp-small">${setpoint(t, unit)}</span><span class="stat-label">requested setpoint</span></div>
        <div class="cl-figure"><span class="cl-temp-small">${temp(t.measured.value_c, unit)}</span><span class="stat-label">its own reading (not the room's)</span></div>
        <div class="cl-figure"><span class="cl-temp-small">${t.hvac_mode ?? t.state}</span><span class="stat-label">mode${t.preset ? ` · preset ${t.preset}` : ""}</span></div>
      </div>
      <span class="cl-device-meta">${t.kind_label}${t.shared ? " · shared by its thermal zone" : ""} · <span class="mono">${t.entity_id}</span></span>
      ${caps ? html`<span class="cl-device-meta">Reports: ${[caps.reports_action ? "activity" : null, caps.reports_current ? "its temperature" : null].filter(Boolean).join(", ") || "neither activity nor temperature"}
        · setpoint range ${temp(caps.min_temp_c, unit)}–${temp(caps.max_temp_c, unit)}${caps.step ? ` (step ${caps.step})` : ""}
        · ${caps.external_input ? html`has an input for an external sensor (<span class="mono">${caps.external_input}</span>); Smart Comfort does not use it` : "no native external-sensor input found"}</span>` : nothing}
      ${t.capabilities_changed ? html`<span class="cl-device-meta tone-warning">${icon("warning", 14)} Its capabilities changed since the configuration was saved.</span>` : nothing}
    </li>`;
  }

  protected render(): unknown {
    const r = this.room;
    const unit: Unit = this.history?.unit ?? (store.overview?.home_assistant.info?.unit_system.temperature === "°F" ? "°F" : "°C");
    const when = haTime(false);
    const back = html`<div class="page-actions"><a class="btn" href="#/climate/rooms">${icon("arrow-left", 18)}All rooms</a>
      <a class="btn" href="#/climate/settings/rooms?room=${encodeURIComponent(this.areaId)}" data-testid="climate-room-configure">${icon("cog", 18)}Configure</a></div>`;
    if (!r) {
      return html`${pageHeader("Climate", "", back)}${climateTabs("rooms")}${this.error ? problemText(this.error) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`}`;
    }
    const h = this.history;
    return html`${pageHeader(r.name, "Climate of this room: what its sensors measure and what its heating devices report.", back)}
      ${observingNotice()}
      ${climateTabs("rooms")}
      ${roomTabs(this.areaId, "monitoring")}
      ${this.error ? problemText(this.error) : nothing}
      ${!r.area_exists ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>This area no longer exists in Home Assistant.</span></p>` : nothing}
      <div class="stat-tiles cl-tiles" data-testid="climate-room-tiles">
        <div class="stat"><span class="stat-value" data-testid="climate-room-temp">${temp(r.temperature.value_c, unit)}</span><span class="stat-label">${r.temperature.source === "primary" ? "room (primary sensor)" : r.temperature.source === "secondary" ? "room (secondary sensors)" : "room not measured"}</span></div>
        <div class="stat"><span class="stat-value">${r.outdoor_difference_c !== null ? delta(r.outdoor_difference_c, unit) : "—"}</span><span class="stat-label">indoor − outdoor</span></div>
        <div class="stat"><span class="stat-value">${r.occupancy ? (r.occupancy.occupied ? "Occupied" : r.occupancy.occupied === false ? "Vacant" : "—") : "—"}</span><span class="stat-label">occupancy</span></div>
        <div class="stat"><span class="stat-value ${r.completeness.ready ? "tone-ok" : "tone-warning"}">${r.completeness.score} %</span><span class="stat-label">configuration complete</span></div>
      </div>
      ${r.demand ? html`<p class="notice notice-info">${icon("info", 20)}<span>${r.demand.text}</span></p>` : nothing}
      <section class="card pad" style="margin-top:1rem" data-testid="climate-history">
        <div class="cl-row" style="justify-content:space-between"><h3 style="margin:0">History</h3>
          <div class="cl-windows" role="group" aria-label="Time window">${(h?.windows ?? [21600, 86400, 259200, 604800, 2592000]).map((w) => html`<button type="button" class="btn btn-small ${w === this.window ? "btn-primary" : ""}" aria-pressed=${w === this.window ? "true" : "false"} data-window=${w} @click=${() => this.setWindow(w)}>${WINDOW_LABELS[w] ?? `${w} s`}</button>`)}</div></div>
        ${this.historyError ? problemText(this.historyError) : nothing}
        ${h ? html`${h.records ? historyChart(h, unit, this.muted) : html`<p class="muted" data-testid="climate-history-empty">Nothing observed in this window yet${h.backfill.state === "running" ? " (reading the Recorder's history…)" : ""}.</p>`}
            ${h.records ? legend(h, this.muted, (key) => { const next = new Set(this.muted); if (next.has(key)) next.delete(key); else next.add(key); this.muted = next; }) : nothing}
            <p class="small muted">${h.source}.${h.observed_from ? ` Observed since ${when(new Date(h.observed_from * 1000).toISOString())}.` : ""}${Object.keys(h.gaps).length ? ` Some periods were not observed (the App or Home Assistant was not running, or the Recorder no longer had them).` : ""}</p>`
          : html`<div class="loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`}
      </section>
      <div class="cl-two" style="margin-top:1rem">
        <section class="card pad cl-section"><h3>Heating devices</h3>
          ${r.thermostats.length ? html`<ul class="cl-device-list">${r.thermostats.map((t) => this.thermostat(t, unit))}</ul>`
            : html`<p class="muted">No heating device is assigned: this room is observed only.</p>`}
        </section>
        <section class="card pad cl-section"><h3>Temperature sensors</h3>
          <ul class="cl-device-list">
            ${r.primary.entity_id ? this.reading("Primary", r.primary, unit) : html`<li class="muted">No primary sensor: the room temperature is not measured.</li>`}
            ${r.secondary.map((s) => this.reading("Secondary", s, unit))}
          </ul>
        </section>
        <section class="card pad cl-section"><h3>Windows and doors</h3>
          ${r.openings.length ? html`<ul class="cl-problem-list" data-testid="climate-openings-list">${r.openings.map((o) => html`<li data-entity=${o.entity_id}>${icon(o.status === "open" ? (o.kind === "door" ? "door-open" : "window-open") : o.kind === "door" ? "door" : "window", 18)}<span>${openingLabel(o.kind)}:</span><strong>${o.name ?? o.entity_id}</strong>
              ${o.kind ? nothing : html`<a class="text-link small" href="#/climate/settings/rooms?room=${encodeURIComponent(r.area_id)}">set the type</a>`}
              ${chip(o.status === "open" ? "info" : o.status === "closed" ? "ok" : "error", o.status === "open" ? "Open" : o.status === "closed" ? "Closed" : o.status === "missing" ? "Entity not found" : o.status === "unknown" ? "No value" : "Unavailable")}
              ${o.since ? html`<span class="small muted">since ${when(o.since)}</span>` : nothing}</li>`)}</ul>`
            : html`<p class="muted">None assigned.</p>`}
        </section>
        <section class="card pad cl-section"><h3>Configuration</h3>
          <ul class="cl-check-list">${r.completeness.items.map((i) => html`<li>${icon(i.ok ? "ok" : i.required ? "warning" : "info", 18)}<span class=${i.ok ? "" : i.required ? "tone-warning" : "muted"}>${i.text}</span></li>`)}</ul>
          <p class="small muted">Comfort ${temp(r.targets.comfort_c, unit)} · setback ${temp(r.targets.setback_c, unit)} · allowed ${temp(r.targets.min_target_c, unit)}–${temp(r.targets.max_target_c, unit)} · a sensor counts as current for ${r.targets.stale_minutes} min${r.zone ? ` · thermal zone ${r.zone.name}` : ""}.
            These preferences are stored for later phases; nothing is controlled now.</p>
        </section>
      </div>`;
  }
}
customElements.define("sl-climate-room", SlClimateRoom);
