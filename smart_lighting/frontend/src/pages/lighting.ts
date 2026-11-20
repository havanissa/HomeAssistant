// Lighting (Phase A4): every enabled room's automatic lighting at a glance - mode, occupancy, mood and profile,
// lights on, daylight / ambient level, the last decision, warnings, offline lights and manual overrides. Every
// value comes from the lighting runtime's editor model (``smart_occupancy/lighting/editor``); a room opens its
// workspace.
import "../lighting/lighting-pages.css";
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { chip, pageHeader } from "../ui";
import { controlBanner, LightingElement, problemNotice } from "../lighting/components";
import {
  AMB_BASIS,
  DAYLIGHT,
  decisionChip,
  fmt,
  loadEditor,
  modeChip,
  occChip,
  plural,
  problemLevel,
  roomMode,
  type EditorModel,
  type RoomModel,
} from "../lighting/lighting-model";

type Filter = "all" | "automatic" | "observing" | "attention";

export class SlLighting extends LightingElement {
  static properties = {
    ...LightingElement.properties, areaId: { type: String }, tab: { type: String },
    model: { state: true }, rooms: { state: true }, filter: { state: true }, failed: { state: true },
  };
  declare areaId: string;
  declare tab: string;
  declare model: EditorModel | null;
  declare rooms: Map<string, RoomModel>;
  declare filter: Filter;
  declare failed: Map<string, string>;
  private loading = false;

  constructor() {
    super();
    this.model = null;
    this.rooms = new Map();
    this.failed = new Map();
    this.filter = "all";
  }

  async reload(): Promise<void> {
    if (this.loading) return;
    this.loading = true;
    try {
      const model = await loadEditor();
      this.model = model;
      this.error = null;
      const rooms = new Map<string, RoomModel>();
      const failed = new Map<string, string>();
      await Promise.all(model.rooms.map(async (r) => {
        try {
          const m = await loadEditor(r.area_id);
          if (m.room) rooms.set(r.area_id, m.room);
        } catch (err) {
          failed.set(r.area_id, err instanceof Error ? err.message : String(err));
        }
      }));
      this.rooms = rooms;
      this.failed = failed;
    } catch (err) {
      this.error = err;
    } finally {
      this.loading = false;
    }
  }

  private attention(r: RoomModel): number {
    const d = r.diagnostics ?? {};
    const problems = (r.problems ?? []).filter((p) => problemLevel(p) !== "info").length;
    return problems + (d.offline_lights?.length ?? 0) + (r.group_issues ?? []).filter((g) => !g.dismissed).length
      + (d.blocking_automations?.length ?? 0);
  }

  private shown(r: RoomModel): boolean {
    const on = !!r.settings?.control_enabled;
    if (this.filter === "automatic") return on;
    if (this.filter === "observing") return !on;
    if (this.filter === "attention") return this.attention(r) > 0;
    return true;
  }

  private totals(): TemplateResult {
    const rooms = [...this.rooms.values()];
    const auto = rooms.filter((r) => r.settings?.control_enabled).length;
    const lights = rooms.reduce((n, r) => n + (r.diagnostics?.managed_lights ?? 0), 0);
    const on = rooms.reduce((n, r) => n + r.lights.filter((l) => !l.excluded && !l.missing && l.state === "on").length, 0);
    const overrides = rooms.reduce((n, r) => n + (r.diagnostics?.manual_overrides?.length ?? 0), 0);
    const offline = rooms.reduce((n, r) => n + (r.diagnostics?.offline_lights?.length ?? 0), 0);
    const attention = rooms.filter((r) => this.attention(r) > 0).length;
    const m = this.model;
    return html`<section class="card wide lp-summary" data-testid="lighting-summary">
      <div class="lp-summary-main">
        <div class="lp-summary-mood">${icon("palette", 28)}<div><span class="muted small">Mood now (global)</span>
          <strong class="lp-big">${m?.mood?.manual_mode ? "Manual" : m?.mood?.mood ?? "No mood"}</strong>
          <span class="block small muted">${m?.mood?.detail ?? ""}</span></div></div>
        <div class="stat-tiles lp-tiles">
          <div class="stat"><span class="stat-value">${auto} / ${rooms.length}</span><span class="stat-label">rooms Automatic</span></div>
          <div class="stat"><span class="stat-value">${on} / ${lights}</span><span class="stat-label">managed lights on</span></div>
          <div class="stat"><span class="stat-value ${overrides ? "tone-info" : ""}">${overrides}</span><span class="stat-label">manual overrides</span></div>
          <div class="stat"><span class="stat-value ${offline ? "tone-warning" : ""}">${offline}</span><span class="stat-label">lights offline</span></div>
          <div class="stat"><span class="stat-value ${attention ? "tone-warning" : ""}">${attention}</span><span class="stat-label">rooms need attention</span></div>
        </div>
      </div>
      ${m?.mood?.manual_mode ? html`<p class="notice notice-warning">${icon("warning", 20)}<span><strong>Manual control — automatic lighting paused.</strong> Main Mood is Manual: nothing is switched or adjusted automatically; occupancy and diagnostics continue.</span></p>` : nothing}
      ${m?.adaptive && m.adaptive.status !== "not_configured" ? html`<p class="small muted">Global adjustment: ${this.globalNow(m)} · ${m.adaptive.status}</p>` : nothing}
    </section>`;
  }

  private globalNow(m: EditorModel): string {
    const v = m.adaptive?.values ?? {};
    const parts: string[] = [];
    if (v.brightness?.status !== "not_configured") parts.push(v.brightness?.value !== null && v.brightness?.value !== undefined ? `${Math.round(v.brightness.value)} %` : "— %");
    if (v.color_temp?.status !== "not_configured") parts.push(v.color_temp?.value !== null && v.color_temp?.value !== undefined ? `${Math.round(v.color_temp.value)} K` : "— K");
    return parts.join(" · ") || "—";
  }

  private roomCard(areaId: string, name: string): TemplateResult {
    const r = this.rooms.get(areaId);
    const href = `#/lighting/${encodeURIComponent(areaId)}`;
    if (!r) {
      const err = this.failed.get(areaId);
      return html`<a class="card lp-room-card" href=${href} data-area=${areaId}>
        <header class="lp-room-head"><h3>${name}</h3>${err ? chip("error", "Not loaded") : chip("pending", "Loading…")}</header>
        ${err ? html`<p class="small tone-error">${err}</p>` : nothing}</a>`;
    }
    const d = r.diagnostics ?? {};
    const mode = roomMode(!!r.settings?.control_enabled, d.control_mode);
    const managed = r.lights.filter((l) => !l.excluded && !l.missing);
    const on = managed.filter((l) => l.state === "on").length;
    const offline: string[] = d.offline_lights ?? [];
    const overrides: unknown[] = d.manual_overrides ?? [];
    const problems = (r.problems ?? []).filter((p) => problemLevel(p) !== "info");
    const errors = problems.filter((p) => problemLevel(p) === "error").length;
    const blocking: string[] = d.blocking_automations ?? [];
    const groupIssues = (r.group_issues ?? []).filter((g) => !g.dismissed).length;
    const dl = r.daylight?.status;
    const amb = r.brightness?.ambient;
    const latest = r.decisions?.latest;
    const nameOf = (e: string) => r.lights.find((l) => l.entity_id === e)?.name ?? e;
    const pct = managed.length ? Math.round((on / managed.length) * 100) : 0;
    return html`<a class="card lp-room-card lp-mode-${mode.key}" href=${href} data-area=${areaId} data-testid="lighting-room-card">
      <header class="lp-room-head">
        <span class="lp-room-icon">${icon(mode.icon, 22)}</span>
        <h3>${r.name}</h3>
        ${modeChip(!!r.settings?.control_enabled, d.control_mode)}
      </header>
      ${mode.tone === "warning" ? html`<p class="small tone-warning lp-reason">${d.control_mode_reason}</p>` : nothing}
      <dl class="lp-facts">
        <div><dt>Occupancy</dt><dd>${occChip(r.occupancy?.state)}</dd></div>
        <div><dt>Mood</dt><dd>${d.manual_mode ? "Manual (paused)" : r.now_mood ?? "No mood"}<span class="small muted"> · ${d.follows_global ? "global" : "room mood"}</span></dd></div>
        <div><dt>Profile now</dt><dd>${r.now_profile === "vacant" ? "Vacant" : "Occupied"}</dd></div>
        <div><dt>Lights on</dt><dd><span class="lp-meter" aria-hidden="true"><span style="width:${pct}%"></span></span> ${on} / ${managed.length}</dd></div>
        <div><dt>Daylight</dt><dd>${dl ? chip(DAYLIGHT[dl.decision]?.[1] ?? "neutral", DAYLIGHT[dl.decision]?.[0] ?? dl.decision) : "—"}</dd></div>
        <div><dt>Ambient</dt><dd>${amb ? html`${amb.status?.name ?? amb.status?.label ?? "—"}${amb.status?.basis ? html`<span class="small muted"> · ${AMB_BASIS[amb.status.basis]?.[0] ?? amb.status.basis}</span>` : nothing}${amb.enabled ? nothing : html`<span class="small muted"> · control off</span>`}` : "—"}</dd></div>
      </dl>
      <div class="lp-room-last">
        ${latest ? html`<span class="small muted">Last decision · ${fmt(latest.at)}</span>
          <span class="lp-last-line"><strong>${latest.action}</strong> ${decisionChip(latest)}</span>
          <span class="small muted lp-clamp">${latest.explanation}</span>` : html`<span class="small muted">No decision recorded yet.</span>`}
      </div>
      <footer class="lp-room-foot">
        ${errors ? chip("error", plural(errors, "error")) : nothing}
        ${problems.length - errors ? chip("warning", plural(problems.length - errors, "warning")) : nothing}
        ${blocking.length ? chip("warning", plural(blocking.length, "conflicting automation")) : nothing}
        ${groupIssues ? chip("warning", plural(groupIssues, "group question")) : nothing}
        ${offline.length ? html`<span class="chip chip-warning" title=${offline.map(nameOf).join(", ")}>${icon("offline", 14)}<span class="chip-label">${plural(offline.length, "light")} offline</span></span>` : nothing}
        ${overrides.length ? chip("info", plural(overrides.length, "manual override")) : nothing}
        ${!errors && !problems.length && !blocking.length && !groupIssues && !offline.length && !overrides.length ? chip("ok", "No warnings") : nothing}
        <span class="spacer"></span>${icon("chevron-right", 20)}
      </footer>
    </a>`;
  }

  protected render(): unknown {
    const header = pageHeader("Lighting", "Automatic lighting of every room, live decisions and warnings.",
      html`<div class="page-actions">
        <a class="btn" href="#/lighting/settings" data-testid="lighting-settings-link">${icon("cog", 20)}Settings</a>
        <a class="btn" href="#/lighting/control">${icon("swap", 20)}Control</a>
        <button type="button" class="icon-btn" aria-label="Refresh" title="Refresh" ?disabled=${this.loading} @click=${() => void this.refreshAll()}>${icon("refresh")}</button>
      </div>`);
    const m = this.model;
    if (!m) return html`${header}${controlBanner(this.status)}${this.error ? problemNotice(this.error) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`}`;
    const rooms = m.rooms.filter((r) => { const x = this.rooms.get(r.area_id); return !x || this.shown(x); });
    const general = (m.problems ?? []).filter((p) => !p.area_id);
    const filters: Array<[Filter, string]> = [["all", "All rooms"], ["automatic", "Automatic"], ["observing", "Observing"], ["attention", "Needs attention"]];
    return html`${header}${controlBanner(this.status)}
      ${this.error ? problemNotice(this.error) : nothing}
      ${general.length ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>${plural(general.length, "lighting problem")} that concern no single room: <a class="btn btn-small" href="#/lighting/settings">Lighting settings</a>.</span></p>` : nothing}
      <div class="grid lp-grid">${this.totals()}</div>
      ${m.rooms.length ? html`
        <div class="toolbar lp-toolbar" role="group" aria-label="Show rooms">
          ${filters.map(([k, l]) => html`<button type="button" class="btn btn-small ${this.filter === k ? "lp-on" : ""}" aria-pressed=${this.filter === k ? "true" : "false"} @click=${() => (this.filter = k)}>${l}</button>`)}
        </div>
        <div class="lp-room-grid" data-testid="lighting-rooms">${rooms.map((r) => this.roomCard(r.area_id, r.name))}
          ${!rooms.length ? html`<p class="muted">No room matches this filter.</p>` : nothing}</div>`
        : html`<div class="card empty-state"><p><strong>No room uses Smart Comfort lighting yet.</strong></p>
            <p class="muted">Enable a room in the Rooms editor; its lights then appear here in Observing mode.</p>
            <a class="btn" href="#/rooms">${icon("floor-plan", 20)}Rooms</a></div>`}`;
  }
}

customElements.define("sl-lighting", SlLighting);
