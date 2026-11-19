// Predictions → one room (Phase A5): its forecast now, learned patterns, accuracy, its own settings and its log.
// Route: #/predictions/rooms/<area_id>/<tab>.
import "../predictions/predictions.css";

import { html, nothing, type PropertyValues, type TemplateResult } from "lit";

import { icon } from "../icons";
import { chip, kv, pageHeader } from "../ui";
import { haTime } from "../presence/components";
import { predictionsRoom, type RoomDetail } from "../predictions/api";
import { adminNotice, controlBanner, dayWord, learningChip, observationNotice, PredictionsElement, problemNotice, statusChip } from "../predictions/components";
import { forecastPanel, logPanel, patternsPanel, roomAccuracyPanel } from "../predictions/panels";
import { connectionsEditor, roomOverridesForm } from "../predictions/editors";

const TABS = [
  { key: "forecast", title: "Forecast", icon: "crystal-ball" },
  { key: "patterns", title: "Patterns", icon: "history" },
  { key: "accuracy", title: "Accuracy", icon: "shield-check" },
  { key: "settings", title: "Settings", icon: "cog" },
  { key: "log", title: "Log", icon: "list" },
] as const;

export class SlPredictionsRoom extends PredictionsElement {
  static properties = { ...PredictionsElement.properties, areaId: { type: String }, tab: { type: String }, detail: { state: true }, detailError: { state: true } };
  declare areaId: string;
  declare tab: string;
  declare detail: RoomDetail | null;
  declare detailError: unknown;

  constructor() {
    super();
    this.areaId = "";
    this.tab = "forecast";
    this.detail = null;
    this.detailError = null;
  }

  protected willUpdate(changed: PropertyValues): void {
    super.willUpdate();
    if (changed.has("areaId") && changed.get("areaId") !== undefined) {
      this.detail = null;
      void this.reload();
    }
  }

  async reload(): Promise<void> {
    if (!this.areaId) return;
    const areaId = this.areaId;
    try {
      const detail = await predictionsRoom(areaId);
      if (this.areaId === areaId) {
        this.detail = detail;
        this.detailError = null;
      }
    } catch (err) {
      if (this.areaId === areaId) this.detailError = err;
    }
  }

  private learningCard(d: RoomDetail): TemplateResult {
    const l = d.learning;
    const fmt = haTime(false);
    return html`<section class="card" data-testid="predictions-room-learning"><header class="card-head">${icon("sync")}<h3>Data behind this room</h3>${learningChip(l.state)}</header>
      ${kv([
        ["Days observed", `${dayWord(l.room_days)} (${l.room_weekdays} of 7 weekdays)`],
        ["Period", l.room_first_date ? `${l.room_first_date} → ${l.room_last_date ?? "—"}` : "—"],
        ["Observed", `${l.room_observed_hours} h · ${l.room_entries} visits`],
        ["House", `${dayWord(l.days_learned)} learned of a ${l.window_days}-day window${l.today_included ? ", today included" : ""}`],
        ["Last learned", fmt(l.last_update)],
        l.last_error ? ["Learning error", html`<span class="tone-error">${l.last_error}</span>`] : null,
      ])}</section>`;
  }

  private settingsTab(d: RoomDetail): TemplateResult {
    const s = d.settings;
    return html`<div data-testid="predictions-room-settings">
      ${!this.canEdit ? adminNotice() : nothing}
      <div class="grid engine-grid">
        <section class="card wide"><header class="card-head">${icon("tune")}<h3>${d.name}: own values</h3>${s.overridden.length ? chip("info", `${s.overridden.length} own value${s.overridden.length === 1 ? "" : "s"}`) : chip("neutral", "Follows the global defaults")}</header>
          <p class="muted small">Effective now: forecasts ${s.effective.enabled ? "on" : "off"}, horizon ${s.effective.horizon_minutes} min, threshold ${s.effective.probability_threshold} %, confidence needed ${s.effective.confidence_threshold}. Changes are saved at once. <a href="#/predictions/settings">Global defaults</a>.</p>
          ${roomOverridesForm(d.area_id, s.room, s.global, this, { horizons: s.horizons, confidenceOptions: s.confidence_options })}
        </section>
        <section class="card wide"><header class="card-head">${icon("link")}<h3>Connections of ${d.name}</h3></header>
          ${connectionsEditor(d.area_id, d.connections, this)}
        </section>
        <section class="card"><header class="card-head">${icon("tune")}<h3>Contexts in use</h3></header>
          <p class="muted small">${s.automatic_contexts.length && s.global.contexts === null ? "Chosen automatically." : "Chosen by hand in the global settings."} Workday source: ${s.workday_name ?? (s.workday_entity ?? "none")}${s.workday_automatic ? " (automatic)" : ""}.</p>
          ${s.contexts.length ? html`<ul class="plain-list small">${s.contexts.map((c) => html`<li>${c.name} <span class="muted">${c.entity_id}</span></li>`)}</ul>` : html`<p class="muted small">No context entity.</p>`}
        </section>
      </div>
    </div>`;
  }

  protected render(): unknown {
    const tab = TABS.some((t) => t.key === this.tab) ? this.tab : "forecast";
    const d = this.detail;
    const name = d?.name ?? this.roomName(this.areaId);
    const header = pageHeader(name, "This room's occupancy forecast: what is likely, how reliable it is and what it learned from.",
      html`<div class="page-actions">${d?.now ? statusChip(d.now.status) : nothing}<a class="btn" href="#/predictions">${icon("arrow-left", 20)}Predictions</a></div>`);
    const body = this.detailError ? problemNotice(this.detailError)
      : !d ? html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`
      : tab === "patterns" ? patternsPanel(d)
      : tab === "accuracy" ? roomAccuracyPanel(d.accuracy, d.settings.effective.probability_threshold)
      : tab === "settings" ? this.settingsTab(d)
      : tab === "log" ? logPanel(d, (id) => this.roomName(id))
      : html`<div class="grid engine-grid" data-testid="predictions-room-forecast">${forecastPanel(d.now, { shadow: d.shadow_now, source: d.source, comparison: d.comparison })}${this.learningCard(d)}</div>`;
    return html`${header}
      ${controlBanner(this.status)}
      ${observationNotice()}
      <nav class="tabs" aria-label=${name}>
        ${TABS.map((t) => html`<a href="#/predictions/rooms/${this.areaId}/${t.key}" class=${t.key === tab ? "active" : ""} aria-current=${t.key === tab ? "page" : "false"} data-tab=${t.key}>${icon(t.icon, 20)}${t.title}</a>`)}
      </nav>
      ${this.error ? problemNotice(this.error) : nothing}
      ${this.notice ? html`<p class="notice" role="status">${icon("ok", 20)}<span>${this.notice}</span></p>` : nothing}
      ${body}`;
  }
}

customElements.define("sl-predictions-room", SlPredictionsRoom);
