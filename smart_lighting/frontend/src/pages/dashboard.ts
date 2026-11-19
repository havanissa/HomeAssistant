// The main dashboard (Phase A6): the whole house at a glance from the App's own runtimes - occupancy, lighting,
// the mood, room modes and overrides, curtains, predictions, offline devices and health - with the owner of
// every subsystem and whether its data is fresh. Nothing on this page commands a device: actions live in the
// modules (and, for everyday use, in the companion's Lovelace cards).
import { html, nothing, type TemplateResult } from "lit";

import { formatDuration, formatTime, plural } from "../format";
import { icon } from "../icons";
import { navigate, navigatePath } from "../router";
import { getJson, store } from "../store";
import type { Check } from "../types";
import { chip, haConnectionInfo, kv, linkInfo, loading, pageHeader, StoreElement, type Tone } from "../ui";
import { companionText, DOMAIN_TITLES, ownerText, type CompanionBrief } from "../system/api";

const REFRESH_MS = 10_000;

export interface DashboardRoom {
  area_id: string;
  name: string;
  occupancy: { state: string | null; occupied: boolean | null; reason: string | null; since: string | null; fresh: boolean };
  lighting: { mode: "active" | "paused" | "observe" | null; active: boolean; overrides: number; paused: boolean; mood: string | null };
  daylight: string | null;
  windows: number;
  forecast: { probability: number | null; predicted: boolean | null; horizon: string | null } | null;
  sensors: number;
  lights: number;
}

export interface Dashboard {
  generated_at: string;
  house: { rooms: number; occupied: number; vacant: number; unknown: number; occupied_rooms: string[] };
  rooms: DashboardRoom[];
  mood: { current: string | null; manual: boolean; source: string | null; detail: string | null; options: string[]; main_mood?: string | null };
  curtains: { windows: Array<{ window_id: string; name: string; area_id: string | null; room: string; mode: string; moving: boolean; covers: string[] }>; automatic: number; moving: number; fresh: boolean } | null;
  forecast: { learning: string; days: number; next_room: { name: string; probability: number } | null; fresh: boolean } | null;
  devices: { unavailable: Array<{ entity_id: string; name: string | null; rooms: string[] | null; title: string | null }>; problems: number; status: string | null };
  health: { status: string; checks: Check[] };
  engines: Record<string, { owner: string; fresh: boolean }>;
  config_authority: "app" | "staged" | "integration" | string;
  companion: CompanionBrief | null;
}

const HEALTH: Record<string, { tone: Tone; label: string }> = {
  ok: { tone: "ok", label: "Healthy" },
  warning: { tone: "warning", label: "Degraded" },
  error: { tone: "error", label: "Problem" },
};

const OCCUPANCY: Record<string, { tone: Tone; label: string }> = {
  occupied: { tone: "ok", label: "Occupied" },
  vacant: { tone: "neutral", label: "Vacant" },
  vacancy_pending: { tone: "info", label: "Vacancy pending" },
  possible: { tone: "info", label: "Possible occupancy" },
  unknown: { tone: "pending", label: "Unknown" },
};

export function occupancyText(state: string | null | undefined, fresh: boolean): { tone: Tone; label: string } {
  const base = OCCUPANCY[state ?? "unknown"] ?? { tone: "pending" as Tone, label: state ?? "Unknown" };
  return fresh ? base : { tone: "pending", label: `${base.label} (not fresh)` };
}

export function lightingText(l: DashboardRoom["lighting"]): { tone: Tone; label: string } {
  if (l.mode === "paused") return { tone: "warning", label: "Automatic · paused (manual mode)" };
  if (l.mode === "active") return { tone: "ok", label: l.overrides ? `Automatic · ${plural(l.overrides, "override", "overrides")}` : "Automatic" };
  return { tone: "neutral", label: "Observing" };
}

export class SlDashboard extends StoreElement {
  static properties = { data: { state: true }, error: { state: true } };
  declare data: Dashboard | null;
  declare error: string | null;
  private timer: number | undefined;
  private seen = -1;

  constructor() {
    super();
    this.data = null;
    this.error = null;
  }

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
    this.timer = window.setInterval(() => void this.load(), REFRESH_MS);
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    window.clearInterval(this.timer);
  }

  protected willUpdate(): void {
    const version = store.systemVersion + store.engineVersion + store.lightingVersion + store.curtainsVersion + store.predictionsVersion + store.roomsVersion;
    if (version !== this.seen) {
      const first = this.seen === -1;
      this.seen = version;
      if (!first) void this.load();
    }
  }

  async load(): Promise<void> {
    try {
      this.data = await getJson<Dashboard>("api/dashboard");
      this.error = null;
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
    }
  }

  private house(d: Dashboard): TemplateResult {
    const h = d.house;
    const any = h.rooms > 0;
    const fresh = d.engines.occupancy?.fresh ?? false;
    return html`<section class="card wide engine-hero ${h.occupied ? "engine-app" : ""}" data-testid="card-house">
      <div class="engine-hero-main">
        <span class="engine-hero-icon">${icon("home", 36)}</span>
        <div>
          <p class="muted small">House</p>
          <h3 class="engine-owner">${!any ? "No rooms configured yet" : h.occupied ? `${plural(h.occupied, "room", "rooms")} occupied` : h.unknown === h.rooms ? "Occupancy unknown" : "Nobody detected"}</h3>
          <p class="small">
            ${any ? chip("ok", `${h.occupied} occupied`) : nothing}
            ${any ? chip("neutral", `${h.vacant} vacant`) : nothing}
            ${h.unknown ? chip("pending", `${h.unknown} unknown`) : nothing}
            ${any && !fresh ? chip("warning", "Occupancy data not fresh: the App does not run occupancy") : nothing}
          </p>
          ${h.occupied_rooms.length ? html`<p class="small muted">${h.occupied_rooms.join(", ")}</p>` : nothing}
        </div>
      </div>
      <div class="engine-hero-actions">
        ${!any ? html`<a class="btn btn-primary" href="#/settings/setup" @click=${(e: Event) => (e.preventDefault(), navigatePath("settings", "setup"))}>${icon("plus", 18)}Set up the home</a>` : nothing}
        <a class="btn" href="#/presence" @click=${(e: Event) => (e.preventDefault(), navigate("presence"))}>${icon("motion-sensor", 18)}Presence</a>
      </div>
    </section>`;
  }

  private rooms(d: Dashboard): TemplateResult | typeof nothing {
    if (!d.rooms.length) return nothing;
    return html`<section class="card wide" data-testid="card-rooms">
      <header class="card-head">${icon("floor-plan")}<h3>Rooms</h3>${chip("neutral", `${d.rooms.length}`)}</header>
      <div class="table-wrap"><table class="plain-table room-table"><thead><tr>
        <th scope="col">Room</th><th scope="col">Occupancy</th><th scope="col">Lighting</th><th scope="col">Mood</th><th scope="col">Daylight</th><th scope="col">Prediction</th></tr></thead>
        <tbody>${d.rooms.map((r) => {
          const occ = occupancyText(r.occupancy.state, r.occupancy.fresh);
          const lt = lightingText(r.lighting);
          return html`<tr data-room=${r.area_id}>
            <th scope="row"><a href="#/rooms/${r.area_id}" @click=${(e: Event) => (e.preventDefault(), navigatePath("rooms", r.area_id))}>${r.name}</a>
              <span class="block small muted">${plural(r.sensors, "sensor", "sensors")} · ${plural(r.lights, "light", "lights")}${r.windows ? ` · ${plural(r.windows, "window", "windows")}` : ""}</span></th>
            <td>${chip(occ.tone, occ.label)}${r.occupancy.since ? html`<span class="block small muted">since ${formatTime(r.occupancy.since)}</span>` : nothing}</td>
            <td>${r.lights ? chip(lt.tone, lt.label) : html`<span class="muted">—</span>`}</td>
            <td>${r.lighting.mood ?? html`<span class="muted">—</span>`}</td>
            <td>${r.daylight ?? html`<span class="muted">—</span>`}</td>
            <td>${r.forecast && r.forecast.probability != null ? html`${r.forecast.probability}%${r.forecast.horizon ? html` <span class="muted small">(${r.forecast.horizon})</span>` : nothing}` : html`<span class="muted">—</span>`}</td>
          </tr>`;
        })}</tbody></table></div>
    </section>`;
  }

  private mood(d: Dashboard): TemplateResult {
    const m = d.mood;
    return html`<section class="card" data-testid="card-mood">
      <header class="card-head">${icon("palette")}<h3>Mood</h3>${m.manual ? chip("warning", "Manual mode") : m.current ? chip("ok", m.current) : chip("neutral", "None")}</header>
      ${kv([
        ["Current", m.current ?? "—"],
        ["Source", m.source === "builtin" ? "Built-in selector" : m.source ?? "Not configured"],
        ["Detail", m.detail],
        ["Moods", m.options.length ? m.options.join(", ") : null],
      ])}
      <a class="text-link" href="#/moods" @click=${(e: Event) => (e.preventDefault(), navigate("moods"))}>Open Moods ${icon("chevron-right", 18)}</a>
    </section>`;
  }

  private curtains(d: Dashboard): TemplateResult | typeof nothing {
    const c = d.curtains;
    if (!c || !c.windows.length) return nothing;
    return html`<section class="card" data-testid="card-curtains">
      <header class="card-head">${icon("curtains")}<h3>Curtains</h3>${chip(c.moving ? "info" : "neutral", c.moving ? `${plural(c.moving, "window", "windows")} moving` : `${c.automatic} of ${c.windows.length} automatic`)}</header>
      ${!c.fresh ? html`<p class="small tone-warning">The App does not run the curtains: positions are not commanded.</p>` : nothing}
      <ul class="plain-list small">${c.windows.slice(0, 8).map((w) => html`<li>${chip(w.mode === "automatic" ? "ok" : "neutral", w.mode)} ${w.name} <span class="muted">· ${w.room}</span>${w.moving ? html` ${chip("info", "moving")}` : nothing}</li>`)}</ul>
      <a class="text-link" href="#/curtains" @click=${(e: Event) => (e.preventDefault(), navigate("curtains"))}>Open Curtains ${icon("chevron-right", 18)}</a>
    </section>`;
  }

  private forecast(d: Dashboard): TemplateResult | typeof nothing {
    const f = d.forecast;
    if (!f) return nothing;
    return html`<section class="card" data-testid="card-forecast">
      <header class="card-head">${icon("crystal-ball")}<h3>Predictions</h3>${chip(f.learning === "ready" ? "ok" : "info", f.learning)}</header>
      ${kv([
        ["Learned days", `${f.days}`],
        ["Next room", f.next_room ? `${f.next_room.name} (${f.next_room.probability}%)` : "—"],
        ["Published", f.fresh ? "By the App" : "Not by the App"],
      ])}
      <a class="text-link" href="#/predictions" @click=${(e: Event) => (e.preventDefault(), navigate("predictions"))}>Open Predictions ${icon("chevron-right", 18)}</a>
    </section>`;
  }

  private devices(d: Dashboard): TemplateResult {
    const u = d.devices.unavailable;
    return html`<section class="card" data-testid="card-devices">
      <header class="card-head">${icon("offline")}<h3>Devices</h3>${chip(u.length ? "warning" : "ok", u.length ? `${plural(u.length, "device", "devices")} unavailable` : "All available")}</header>
      ${u.length ? html`<ul class="plain-list small">${u.slice(0, 10).map((x) => html`<li><strong>${x.name ?? x.entity_id}</strong> <span class="muted">${x.title ?? ""}${x.rooms?.length ? ` · ${x.rooms.join(", ")}` : ""}</span></li>`)}</ul>` : html`<p class="muted small">Every configured sensor, light and cover reports a state.</p>`}
      ${d.devices.problems ? html`<p class="small">${plural(d.devices.problems, "diagnostic problem", "diagnostic problems")} · <a href="#/diagnostics" @click=${(e: Event) => (e.preventDefault(), navigate("diagnostics"))}>Diagnostics</a></p>` : nothing}
    </section>`;
  }

  private health(d: Dashboard): TemplateResult {
    const o = store.overview;
    const health = HEALTH[d.health.status] ?? HEALTH.warning;
    const problems = d.health.checks.filter((c) => c.status === "error" || c.status === "warning");
    const conn = o?.home_assistant.connection;
    const ha = conn ? haConnectionInfo(conn) : linkInfo(store.link);
    return html`<section class="card" data-testid="card-app">
      <header class="card-head">${icon("lightbulb-auto")}<h3>App</h3>${chip(health.tone, health.label)}</header>
      ${kv([
        ["Version", o?.app.version],
        ["Running for", o ? formatDuration(o.app.uptime_s) : null],
        ["Home Assistant", `${ha.label}${o?.home_assistant.info?.version ? ` · ${o.home_assistant.info.version}` : ""}`],
        ["Health checks", problems.length ? plural(problems.length, "needs attention", "need attention") : "All passed"],
      ])}
      ${problems.length ? html`<ul class="plain-list">${problems.map((c) => html`<li>${chip(c.status, c.title)} <span class="muted">${c.detail}</span></li>`)}</ul>` : nothing}
      <a class="text-link" href="#/diagnostics" @click=${(e: Event) => (e.preventDefault(), navigate("diagnostics"))}>Open Diagnostics ${icon("chevron-right", 18)}</a>
    </section>`;
  }

  private engines(d: Dashboard): TemplateResult {
    const c = companionText(d.companion);
    const authority = d.config_authority;
    return html`<section class="card" data-testid="card-engines">
      <header class="card-head">${icon("shield-check")}<h3>Who runs what</h3>${chip(c.tone, `Companion: ${c.label}`)}</header>
      <ul class="plain-list">${Object.entries(d.engines).map(([k, e]) => {
        const o = ownerText(e.owner);
        return html`<li data-engine=${k}>${chip(o.tone, o.label)} ${DOMAIN_TITLES[k] ?? k}${!e.fresh && e.owner !== "none" ? html` <span class="muted small">(not run by the App)</span>` : nothing}</li>`;
      })}</ul>
      <p class="small muted">Configuration: ${authority === "app" ? "the App" : authority === "staged" ? "a staged copy (migration in progress)" : "the integration (until migrated)"}.</p>
      <a class="text-link" href="#/settings/system" @click=${(e: Event) => (e.preventDefault(), navigatePath("settings", "system"))}>Migration &amp; System ${icon("chevron-right", 18)}</a>
    </section>`;
  }

  protected render(): unknown {
    const d = this.data;
    return html`
      ${pageHeader("Dashboard", "The house now: occupancy, lighting, mood, curtains, predictions, devices and health.")}
      ${this.error ? html`<p class="notice notice-error" data-testid="dashboard-error">${icon("error", 20)}<span>${this.error}</span></p>` : nothing}
      ${d
        ? html`<div class="grid">
            ${this.house(d)} ${this.rooms(d)} ${this.mood(d)} ${this.curtains(d)} ${this.forecast(d)} ${this.devices(d)} ${this.health(d)} ${this.engines(d)}
          </div>`
        : loading()}
    `;
  }
}

customElements.define("sl-dashboard", SlDashboard);
