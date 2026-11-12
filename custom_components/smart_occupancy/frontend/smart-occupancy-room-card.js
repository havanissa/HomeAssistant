/*
 * Smart Occupancy Room Status card (read only).
 *
 * Served by the smart_occupancy integration and loaded automatically.
 *
 *   type: custom:smart-occupancy-room-card
 *   area: <area_id>                      # or:
 *   entity: sensor.smart_occupancy_<room>_state
 *
 * Data comes from the attributes of the room's "occupancy state" sensor.
 * The card never calls a service. Mood lighting is configured with the
 * Smart Lighting Profiles and Smart Room Lighting cards (smart-occupancy-lighting-cards.js).
 */

const CARD_TYPE = "smart-occupancy-room-card";
const CARD_VERSION = "0.3.9";

const STATE_LABELS = {
  vacant: "Vacant",
  possible_occupancy: "Possible occupancy",
  confirmed_occupancy: "Confirmed occupancy",
  possible_vacancy: "Possible vacancy",
  uncertain: "Uncertain",
};

const STATE_ICONS = {
  vacant: "mdi:home-outline",
  possible_occupancy: "mdi:account-question",
  confirmed_occupancy: "mdi:account",
  possible_vacancy: "mdi:account-clock",
  uncertain: "mdi:help-circle-outline",
};

const HEALTH = {
  healthy: { label: "OK", icon: "mdi:check-circle", tone: "ok" },
  warning: { label: "Warning", icon: "mdi:alert", tone: "warn" },
  unavailable: { label: "Not usable", icon: "mdi:close-circle", tone: "bad" },
  config_error: { label: "Error", icon: "mdi:close-octagon", tone: "bad" },
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function roomStates(hass) {
  if (!hass) return [];
  return Object.values(hass.states)
    .filter((s) => s.entity_id.startsWith("sensor.") && s.attributes.integration === "smart_occupancy"
      && s.attributes.area_id)
    .sort((a, b) => String(a.attributes.room).localeCompare(String(b.attributes.room)));
}

function formatCountdown(seconds) {
  if (seconds <= 0) return "any moment";
  const s = Math.ceil(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

class SmartOccupancyRoomCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-details]")) { this._details = !this._details; this._render(); }
    });
    this._timer = null;
    this._lastKey = null;
  }

  static getConfigElement() {
    return document.createElement(`${CARD_TYPE}-editor`);
  }

  static getStubConfig(hass) {
    const first = roomStates(hass)[0];
    return first ? { area: first.attributes.area_id } : { area: "" };
  }

  setConfig(config) {
    if (!config || (!config.area && !config.entity)) {
      throw new Error("Select a room (area) or a Smart Occupancy state entity");
    }
    this._config = { ...config };
    this._lastKey = null;
    if (this._hass) this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  connectedCallback() {
    this._render();
  }

  disconnectedCallback() {
    this._stopTimer();
  }

  getCardSize() {
    return 5;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  _stateObj() {
    const hass = this._hass;
    if (!hass || !this._config) return undefined;
    if (this._config.entity) return hass.states[this._config.entity];
    return roomStates(hass).find((s) => s.attributes.area_id === this._config.area);
  }

  _stopTimer() {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }

  _updateCountdown() {
    const el = this.shadowRoot.querySelector("[data-countdown]");
    if (!el) return;
    const at = Date.parse(el.dataset.countdown);
    el.textContent = formatCountdown((at - Date.now()) / 1000);
  }

  _render() {
    if (!this._config || !this._hass || !this.isConnected) return;
    const stateObj = this._stateObj();
    // Re-render only when the room data changed (hass updates on every state change in HA).
    const key = stateObj ? `${stateObj.state}|${stateObj.last_updated}|${this._details ? 1 : 0}` : "missing";
    if (key === this._lastKey) return;
    this._lastKey = key;
    this._stopTimer();

    if (!stateObj) {
      this.shadowRoot.innerHTML = `${STYLE}<ha-card><div class="body missing">
        <ha-icon icon="mdi:alert-circle-outline"></ha-icon>
        <span>Smart Occupancy room not found (${escapeHtml(this._config.entity || this._config.area)}).
        Check that the room is enabled in the integration.</span></div></ha-card>`;
      return;
    }

    const a = stateObj.attributes;
    const state = stateObj.state;
    const stateLabel = (this._hass.formatEntityState && STATE_LABELS[state])
      ? this._hass.formatEntityState(stateObj) : (STATE_LABELS[state] || state);
    const health = a.health || {};
    const confidence = Number.isFinite(a.confidence) ? a.confidence : null;
    const comparison = a.comparison;

    const chips = (items, empty) => (items && items.length)
      ? items.map((i) => `<span class="chip">${escapeHtml(i)}</span>`).join("")
      : `<span class="muted">${escapeHtml(empty)}</span>`;

    const usable = health.occupancy ? health.occupancy.usable : a.occupancy_usable;
    const pending = a.pending_vacancy && a.vacancy_confirm_at;

    let comparisonHtml = "";
    if (comparison) {
      const agree = comparison.agreement;
      const word = agree === true ? "agrees" : agree === false ? "disagrees" : "unknown";
      comparisonHtml = `<div class="row"><span class="label">Existing helper</span>
        <span class="value"><b>${word}</b><span class="muted"> · ${comparison.disagreements} disagreement(s)</span></span></div>`;
    }
    // One short line per warning; the full explanations behind "Details".
    const warnings = [];
    // Daylight sensing (0.3.3) is its own area: a light sensor problem never degrades occupancy.
    for (const [key, word] of [["occupancy", "Occupancy"], ["lighting", "Lighting"], ["configuration", "Configuration"], ["daylight", "Daylight"]]) {
      for (const r of health[key]?.reasons || []) warnings.push([word, r]);
    }
    const details = ["occupancy", "lighting", "configuration", "daylight"].flatMap((k) => health[k]?.details || health[k]?.reasons || []);
    const open = this._details;
    const chip = (title, h) => {
      const info = HEALTH[h?.status] || HEALTH.healthy;
      return `<span class="hchip ${info.tone}"><ha-icon icon="${info.icon}"></ha-icon>${escapeHtml(title)}: ${escapeHtml(info.label)}</span>`;
    };

    this.shadowRoot.innerHTML = `${STYLE}
      <ha-card>
        <div class="header">
          <ha-icon class="state-icon ${escapeHtml(state)}" icon="${STATE_ICONS[state] || "mdi:help"}"></ha-icon>
          <div class="titles">
            <div class="room">${escapeHtml(this._config.title || a.room)}</div>
            <div class="state">${escapeHtml(stateLabel)}</div>
          </div>
          ${a.observe_only ? `<span class="badge" title="No light is controlled in this room">Observe</span>`
            : a.lighting_paused ? `<span class="badge" title="Manual control — automatic lighting paused">Paused (Manual)</span>`
            : `<span class="badge active" title="Mood lighting is enabled for this room">Mood lighting</span>`}
        </div>
        <div class="body">
          <div class="row"><span class="label">Evidence quality</span>
            <span class="value conf">${confidence === null ? "—" : `
              <span class="bar"><span style="width:${Math.max(0, Math.min(100, confidence))}%"></span></span>
              <span>${confidence}%</span>`}</span></div>
          <div class="row"><span class="label">Active sensors</span>
            <span class="value chips">${chips(a.active_sensors, "none")}</span></div>
          ${(a.recent_door_movement || []).length ? `<div class="row"><span class="label">Door movement</span>
            <span class="value chips">${chips(a.recent_door_movement, "")}</span></div>` : ""}
          ${pending ? `<div class="row pending"><span class="label">Vacancy pending</span>
            <span class="value">confirmed in <b data-countdown="${escapeHtml(a.vacancy_confirm_at)}"></b>
            if nothing is detected</span></div>` : ""}
          <div class="hchips">${chip("Occupancy", health.occupancy)}${chip("Lighting", health.lighting)}${chip("Configuration", health.configuration)}${health.daylight && health.daylight.status !== "healthy" ? chip("Daylight", health.daylight) : ""}
            ${usable ? "" : `<span class="usable bad">occupancy not usable</span>`}</div>
          ${warnings.length ? `<ul class="warnings">${warnings.slice(0, open ? 99 : 4).map(([w, r]) => `<li><span class="muted">${escapeHtml(w)}:</span> ${escapeHtml(r)}</li>`).join("")}</ul>
            <button class="link" data-details>${open ? "Hide details" : warnings.length > 4 ? `Details (+${warnings.length - 4} more)` : "Details"}</button>
            ${open ? `<ul class="details">${details.map((d) => `<li>${escapeHtml(d)}</li>`).join("")}</ul>` : ""}` : ""}
          ${comparisonHtml}
          <div class="reason">${escapeHtml(a.reason || "")}</div>
        </div>
      </ha-card>`;

    if (pending) {
      this._updateCountdown();
      this._timer = setInterval(() => this._updateCountdown(), 1000);
    }
  }
}

class SmartOccupancyRoomCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._render();
  }

  _render() {
    if (!this._hass || !this._config) return;
    const rooms = roomStates(this._hass);
    const current = this._config.area || "";
    const options = rooms.map((s) => `<option value="${escapeHtml(s.attributes.area_id)}"
      ${s.attributes.area_id === current ? "selected" : ""}>${escapeHtml(s.attributes.room)}</option>`).join("");
    this.innerHTML = `
      <style>
        .so-editor { display: grid; gap: 8px; padding: 8px 0; }
        .so-editor select, .so-editor input { font: inherit; padding: 8px; border-radius: 4px;
          border: 1px solid var(--divider-color); background: var(--card-background-color);
          color: var(--primary-text-color); }
      </style>
      <div class="so-editor">
        <label for="so-room">Room</label>
        <select id="so-room">${current ? "" : "<option value=''>Select a room</option>"}${options}</select>
        <label for="so-title">Title (optional)</label>
        <input id="so-title" value="${escapeHtml(this._config.title || "")}" placeholder="Room name">
        ${rooms.length ? "" : "<div>No enabled Smart Occupancy rooms found.</div>"}
      </div>`;
    this.querySelector("#so-room").addEventListener("change", (ev) => {
      const config = { ...this._config, area: ev.target.value };
      delete config.entity;
      this._changed(config);
    });
    this.querySelector("#so-title").addEventListener("change", (ev) => {
      const config = { ...this._config, title: ev.target.value };
      if (!ev.target.value) delete config.title;
      this._changed(config);
    });
  }

  _changed(config) {
    this._config = config;
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
  }
}

const STYLE = `<style>
  :host { display: block; }
  ha-card { container-type: inline-size; overflow: hidden; }
  .header { display: flex; align-items: center; gap: 12px; padding: 16px 16px 8px; }
  .state-icon { --mdc-icon-size: 32px; color: var(--state-icon-color, var(--secondary-text-color)); }
  .state-icon.confirmed_occupancy, .state-icon.possible_occupancy { color: var(--state-active-color, var(--amber-color, #ffc107)); }
  .state-icon.possible_vacancy { color: var(--warning-color, #ff9800); }
  .state-icon.uncertain { color: var(--error-color, #db4437); }
  .titles { flex: 1; min-width: 0; }
  .room { font-size: 1.2em; font-weight: 500; color: var(--primary-text-color);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .state { color: var(--secondary-text-color); }
  .badge { font-size: 0.75em; padding: 2px 8px; border-radius: 12px; white-space: nowrap;
    background: var(--secondary-background-color); color: var(--secondary-text-color);
    border: 1px solid var(--divider-color); }
  .badge.active { color: var(--success-color, #43a047); border-color: currentColor; }
  .body { padding: 0 16px 16px; display: grid; gap: 8px; color: var(--primary-text-color); }
  .body.missing { display: flex; gap: 8px; align-items: center; padding: 16px; }
  .row { display: grid; grid-template-columns: 9.5em 1fr; gap: 8px; align-items: start; }
  .label { color: var(--secondary-text-color); }
  .value { min-width: 0; overflow-wrap: anywhere; }
  .chips { display: flex; flex-wrap: wrap; gap: 4px; }
  .chip { padding: 1px 8px; border-radius: 10px; font-size: 0.9em;
    background: var(--secondary-background-color); border: 1px solid var(--divider-color); }
  .muted { color: var(--secondary-text-color); }
  .conf { display: flex; align-items: center; gap: 8px; }
  .bar { flex: 1; max-width: 200px; height: 8px; border-radius: 4px; background: var(--divider-color); overflow: hidden; }
  .bar > span { display: block; height: 100%; background: var(--primary-color); }
  .pending .value { color: var(--warning-color, #ff9800); }
  .reason { font-size: 0.9em; color: var(--secondary-text-color); border-left: 3px solid var(--divider-color);
    padding-left: 8px; overflow-wrap: anywhere; }
  .healths { display: grid; gap: 6px; grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .hchips { display: flex; flex-wrap: wrap; gap: 6px; }
  .hchip { display: inline-flex; align-items: center; gap: 4px; font-size: 0.85em; padding: 2px 8px; border-radius: 12px;
    border: 1px solid var(--divider-color); }
  .hchip ha-icon { --mdc-icon-size: 16px; }
  .hchip.ok ha-icon { color: var(--success-color, #43a047); }
  .hchip.warn { border-color: var(--warning-color, #ff9800); }
  .hchip.warn ha-icon { color: var(--warning-color, #ff9800); }
  .hchip.bad { border-color: var(--error-color, #db4437); }
  .hchip.bad ha-icon { color: var(--error-color, #db4437); }
  .warnings, .details { margin: 0; padding-left: 18px; font-size: 0.9em; overflow-wrap: anywhere; }
  .details { color: var(--secondary-text-color); font-size: 0.85em; }
  .link { border: none; background: none; color: var(--primary-color); padding: 0; font: inherit; font-size: 0.85em;
    text-decoration: underline; cursor: pointer; justify-self: start; }
  .health { border: 1px solid var(--divider-color); border-radius: 8px; padding: 6px 8px; min-width: 0; }
  .health-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .health-head ha-icon { --mdc-icon-size: 18px; }
  .health-title { font-weight: 500; }
  .health-status { margin-left: auto; font-size: 0.85em; }
  .health.ok ha-icon { color: var(--success-color, #43a047); }
  .health.warn ha-icon, .health.warn .health-status { color: var(--warning-color, #ff9800); }
  .health.bad ha-icon, .health.bad .health-status { color: var(--error-color, #db4437); }
  .health ul { margin: 4px 0 0; padding-left: 18px; font-size: 0.85em; overflow-wrap: anywhere; }
  .usable { font-size: 0.75em; padding: 0 6px; border-radius: 8px; border: 1px solid currentColor; }
  .usable.ok { color: var(--success-color, #43a047); }
  .usable.bad { color: var(--error-color, #db4437); }
  @container (max-width: 520px) {
    .healths { grid-template-columns: 1fr; }
  }
  @container (max-width: 360px) {
    .row { grid-template-columns: 1fr; gap: 2px; }
  }
</style>`;

if (!customElements.get(CARD_TYPE)) {
  customElements.define(CARD_TYPE, SmartOccupancyRoomCard);
  customElements.define(`${CARD_TYPE}-editor`, SmartOccupancyRoomCardEditor);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: CARD_TYPE,
    name: "Smart Occupancy Room Status",
    description: "Read-only occupancy state, evidence and health of one room.",
    preview: true,
    documentationURL: "https://github.com/havanissa/HomeAssistant",
  });
  console.info(`%c SMART-OCCUPANCY-ROOM-CARD %c ${CARD_VERSION} `, "background:#03a9f4;color:#fff", "");
}
