/*
 * Smart Occupancy curtains card (served by the smart_occupancy integration, 0.6.6).
 *
 *   type: custom:smart-occupancy-curtains-card
 *   title: Curtains        # optional
 *   area: stova            # optional: only the windows of one room
 *   page: modes            # optional start page: overview, modes, schedule, windows, diagnostics
 *
 * 0.6.6 (Phase 4.6.4 addendum): Overview · Global Modes · Global Schedule ·
 * Windows · Diagnostics. Global Schedule (when) and Global Modes (which preset)
 * are the only configuration of the normal routine; window settings define the
 * window itself, its presets and its protections, and show (read only) which
 * presets Global Modes gives it, with links. Old per-place settings stay
 * active until the reviewed migration (banner → "Review migration").
 *
 * 0.6.4 (Global Curtain Modes): Curtains → Global Modes. Mode cards (Morning,
 * Daytime, Evening, Night, Sleep, Sun Protection; Window Lamp, Glare and
 * Excessive Heat under Advanced) and, for the selected mode, which preset each
 * window or single cover uses when the automatic controller selects that mode.
 * The All windows matrix edits the same configuration. Selecting a mode card
 * only changes the view; edits are a draft until "Review & save", and a change
 * that would move an Automatic window now needs an explicit confirmation and
 * policy. Window overrides (from 0.6.3 or Window settings) are shown with
 * their effect and can be moved to Global Modes.
 *
 * Overview: windows grouped by room, each with its covers (actual and desired
 * position), the recognised preset (or Custom), its mode, the automatic
 * condition and why it moves or does not, sleep / lamp / manual hold / light
 * evidence, the last movement and its verification, Resume, and the selected
 * preset with Apply. Open a window for its settings: General, Covers, Presets,
 * Automatic, Sun protection, Diagnostics.
 *
 * The card never calls a service. Every change goes to the integration's
 * websocket API (admin only). A preset moves covers only after Apply shows
 * exactly which covers move to which position and the person confirms it.
 * Automatic is enabled per window only after a review of the intended preset,
 * safety conditions and conflicts; from then on the integration moves that
 * window on its own (0.6.2).
 *
 * 0.6.3: every window shows its sun angle, light, temperatures, the direct-sun
 * and temperature assessments and the next re-evaluation; the Environment tab
 * holds the sensors (with their validity), temperature-aware sun and glare.
 * Switching on a new automatic feature first shows what it would do now. All
 * times are 24-hour HH:mm in Home Assistant's time zone, also the time fields.
 */

const VERSION = "0.6.6";
const CARD = "smart-occupancy-curtains-card";
const WS = "smart_occupancy/curtains";

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[c]);
// <time24> (0.6.3) Every clock time is 24-hour HH:mm in Home Assistant's time zone, whatever the browser
// locale (en-US would otherwise show AM/PM, Danish "14.30"). Stored times stay ISO 8601; this is display only.
let TIME_ZONE;  // undefined = the browser's own zone (the person chose "local" time in their profile)
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const TIME_PARTS = new Map();
const setTimeZone = (hass) => {
  const zone = hass && hass.locale && hass.locale.time_zone === "local" ? undefined : hass && hass.config && hass.config.time_zone;
  TIME_ZONE = zone || undefined;
};
const timeParts = (d) => {
  const key = TIME_ZONE || "";
  let f = TIME_PARTS.get(key);
  if (!f) {
    const opts = { hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" };
    try { f = new Intl.DateTimeFormat("en-GB", { ...opts, timeZone: TIME_ZONE }); } catch (_e) { f = new Intl.DateTimeFormat("en-GB", opts); }
    TIME_PARTS.set(key, f);
  }
  const p = {};
  for (const part of f.formatToParts(d)) p[part.type] = part.value;
  if (p.hour === "24") p.hour = "00";
  return p;
};
const hhmm = (d) => { const p = timeParts(d); return `${p.hour}:${p.minute}`; };
const dayKey = (d) => { const p = timeParts(d); return `${p.year}-${p.month}-${p.day}`; };
// "07:00" today, "14 Nov 23:55" on another day.
const fmtTime = (iso, now = new Date()) => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return esc(iso);
  if (dayKey(d) === dayKey(now)) return hhmm(d);
  const p = timeParts(d);
  return `${Number(p.day)} ${MONTHS[Number(p.month) - 1]} ${p.hour}:${p.minute}`;
};
// "2026-11-15 07:00:05" (tooltips).
const fullTime = (iso) => {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso || "");
  const p = timeParts(d);
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`;
};
// A time typed by a person: "7:30", "0730", "7.30", "07:30" -> "07:30"; null unless 00:00-23:59.
const parseHHMM = (text) => {
  const t = String(text == null ? "" : text).trim().replace(/[.h]/g, ":");
  let m = /^(\d{1,2}):(\d{2})$/.exec(t) || /^(\d{1,2})(\d{2})$/.exec(t);
  if (!m && /^\d{1,2}$/.test(t)) m = [t, t, "00"];
  if (!m) return null;
  const h = Number(m[1]);
  const mi = Number(m[2]);
  if (h > 23 || mi > 59) return null;
  return `${String(h).padStart(2, "0")}:${String(mi).padStart(2, "0")}`;
};
// </time24>
const pct = (v) => (v == null ? "—" : `${v} %`);
const deg = (v) => (v == null ? "—" : `${Math.round(v * 10) / 10}°`);

const TABS = [
  ["general", "General", "mdi:information-outline"],
  ["covers", "Covers", "mdi:curtains"],
  ["presets", "Presets", "mdi:tune-variant"],
  ["automation", "Automatic", "mdi:robot-outline"],
  ["sun", "Sun protection", "mdi:weather-sunny"],
  ["environment", "Environment", "mdi:sun-thermometer-outline"],
  ["diagnostics", "Diagnostics", "mdi:stethoscope"],
];
const ENV_GROUPS = new Set(["environment", "thermal"]);
const READING_TONE = { ok: "ok", none: "muted", missing: "bad", unavailable: "warn", stale: "warn", stuck: "bad", implausible: "bad" };
const DIRECT_TONE = { confirmed: "warn", likely: "warn", not_detected: "muted", uncertain: "warn", not_exposed: "muted", off: "muted" };
const DIRECT_WORD = { confirmed: "Direct sunlight", likely: "Direct sun likely", not_detected: "Diffuse daylight", uncertain: "Cannot be confirmed",
  not_exposed: "Sun not in front", off: "Sun protection off" };
const BAND_TONE = { cold: "info", comfortable: "ok", warm: "warn", hot: "bad" };
const ROLE_RULE = { outdoor_lux: "lux_entity", outdoor_temp: "outdoor_temp_entity", weather: "weather_entity", cloud: "cloud_entity" };
const KIND_ICON = { blackout: "mdi:curtains-closed", sheer: "mdi:curtains", shade: "mdi:roller-shade", custom: "mdi:blinds" };
const MODE_TONE = { off: "muted", observing: "info", automatic: "ok" };
const OVERALL_TONE = { running: "info", succeeded: "ok", nothing_to_do: "ok", partial: "warn", timeout: "warn", failed: "bad",
  interrupted: "warn" };
const RESULT_TONE = { reached: "ok", already: "ok", unchanged: "muted", sent: "info", moving: "info", failed: "bad",
  timeout: "warn", unavailable: "warn", missing: "bad", unsupported: "warn", busy: "warn", interrupted: "warn" };
const PLAN_TONE = { move: "info", already: "ok", unchanged: "muted", unavailable: "warn", missing: "bad", unsupported: "warn",
  busy: "warn" };
const EXPO_TONE = { exposed: "warn", outside_sector: "muted", too_low: "muted", below_horizon: "muted", obstructed: "muted",
  no_direction: "muted", unknown: "muted" };
const COND_HINT = {
  morning: "After the night (and sleep), once daylight is measured", day: "Daylight, nobody asleep",
  sun: "The sun in front of this window and strong measured light", evening_privacy: "Dark outside in the evening",
  glare: "Glare-sensitive window in direct sunlight (see Environment); not set = the Direct sunlight preset",
  heat: "Direct sunlight while the room is above its maximum temperature; not set = the Direct sunlight preset",
  night: "The household night (see Night and morning)", sleep: "Sleep mode is on in this room",
  lamp: "While the window lamp is on (see Window lamp)",
};
const COND_ICON = {
  sleep: "mdi:sleep", night: "mdi:weather-night", waiting: "mdi:timer-sand", morning: "mdi:weather-sunset-up",
  day: "mdi:white-balance-sunny", evening_privacy: "mdi:eye-off-outline", sun: "mdi:weather-sunny-alert",
  lamp: "mdi:lamp", manual_override: "mdi:hand-back-right-outline", glare: "mdi:sunglasses", heat: "mdi:thermometer-high",
  solar_heating: "mdi:sun-thermometer",
};
const STATUS_TONE = { moved: "ok", moving: "info", would_move: "info", at_target: "ok", keep: "muted", hold: "warn",
  blocked: "warn", failed: "bad", off: "muted", released: "info", hold_started: "warn", enabled: "ok", resumed: "info",
  mode: "muted" };
const SOURCE_WORD = { window: "this window", room: "the room", global: "all windows", default: "the default" };
// Global Modes (0.6.4)
const SRC_TAG = { global: "G", room: "R", window: "W", cover: "C", mixed: "M" };
const SRC_TONE = { global: "info", room: "warn", window: "warn", cover: "warn", mixed: "muted" };
const SRC_WORD = { global: "Global Mode", room: "Room override", window: "Window override", cover: "Cover override", mixed: "Mixed" };
const MODE_STATUS_TONE = { automatic: "ok", observing: "info", off: "muted" };
const PAGES = ["overview", "modes", "schedule", "windows", "diagnostics"];
// The condition a window is in -> the Global Modes card that sets its preset (0.6.6 links).
const MODE_OF = { morning: "morning", day: "day", solar_heating: "day", evening_privacy: "evening_privacy", night: "night", sleep: "sleep",
  sun: "sun", glare: "glare", heat: "heat", lamp: "lamp" };
// Global Schedule (0.6.5)
const SCH_ICON = { morning: "mdi:weather-sunset-up", day: "mdi:white-balance-sunny", evening: "mdi:weather-sunset-down", night: "mdi:weather-night" };
const SCH_EXPLAIN = {
  duration: "Each window's Morning lasts this many minutes from when it actually began (daylight, after sleep).",
  until_time: "Daytime begins at the time of day. A Morning that could not begin before it (dark, asleep) is skipped, never started late.",
  not_before: "Daytime begins at the time of day, but only once Morning has lasted its minutes.",
  legacy: "As 0.6.4: Evening privacy while it is dark after 12:00; it ends again if it gets lighter.",
  time: "Evening begins at the configured time, whatever the light.",
  event: "Evening begins at sunset or measured dusk (see below); it waits when neither can be determined.",
  first: "Whichever comes first: e.g. dusk at 16:35 in winter, 18:00 in summer.",
  last: "Whichever comes last: e.g. 18:00 in winter, dusk in summer. Without sun or light data the time is used.",
  dusk: "Outdoor light below the dark level for the stable period; without a working sensor the sun below the dark elevation.",
  sunset: "The sun's upper edge below the horizon (from sun.sun).",
};
const ENTITY_LIST = { sleep_entity: "sleep", lux_entity: "lux", lamp_entity: "light" };
const num1 = (v) => (v == null ? "—" : `${Math.round(v * 10) / 10}`);

// One subscription per connection, shared by every curtains card on the page.
const SUBS = new WeakMap();
function subscribe(hass, callback) {
  const conn = hass.connection;
  let entry = SUBS.get(conn);
  if (!entry) {
    entry = { callbacks: new Set(), unsub: null };
    SUBS.set(conn, entry);
    entry.unsub = conn.subscribeMessage(() => entry.callbacks.forEach((cb) => cb()), { type: `${WS}/subscribe` })
      .catch(() => null);
  }
  entry.callbacks.add(callback);
  return () => {
    entry.callbacks.delete(callback);
    if (!entry.callbacks.size) {
      SUBS.delete(conn);
      Promise.resolve(entry.unsub).then((u) => typeof u === "function" && u());
    }
  };
}

class SmartOccupancyCurtainsCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._model = null;
    this._error = null;
    this._view = null;      // null = overview, otherwise a window id
    this._page = null;      // "windows" or "modes" (Global Modes, 0.6.4); null = from the card config
    this._tab = "general";
    this._ui = {};
    this._status = {};
    this.shadowRoot.addEventListener("click", (ev) => this._onClick(ev));
    this.shadowRoot.addEventListener("change", (ev) => this._onChange(ev));
    this.shadowRoot.addEventListener("input", (ev) => this._onInput(ev));
    this.shadowRoot.addEventListener("toggle", (ev) => {
      const g = ev.target?.dataset?.group;
      if (g) { this._ui.open = { ...(this._ui.open || {}), [g]: ev.target.open }; }
    }, true);
  }

  setConfig(config) {
    this._config = { ...(config || {}) };
    this._render();
  }

  static getStubConfig() { return {}; }
  getCardSize() { return 8; }
  getGridOptions() { return { columns: 12, min_columns: 6, rows: "auto" }; }

  set hass(hass) {
    setTimeZone(hass);
    const first = !this._hass || this._hass.connection !== hass.connection;
    this._hass = hass;
    if (first) {
      if (this._unsub) this._unsub();
      if (this.isConnected) this._unsub = subscribe(hass, () => this._scheduleFetch());
      this._fetch();
    }
  }

  connectedCallback() {
    if (this._hass && !this._unsub) {
      this._unsub = subscribe(this._hass, () => this._scheduleFetch());
      this._fetch();
    }
    this._render();
  }

  disconnectedCallback() {
    if (this._unsub) this._unsub();
    this._unsub = null;
    clearTimeout(this._fetchTimer);
  }

  _ws(msg) { return this._hass.callWS(msg); }

  _scheduleFetch() {
    clearTimeout(this._fetchTimer);
    this._fetchTimer = setTimeout(() => this._fetch(), 150);
  }

  async _fetch() {
    if (!this._hass || !this._config) return;
    try {
      this._model = await this._ws({ type: `${WS}/model` });
      this._error = null;
      if (this._view && !this._win(this._view)) this._view = null;
    } catch (err) {
      this._error = err?.message || String(err);
    }
    this._requestRender();
  }

  _requestRender() {
    if (this._renderQueued) return;
    this._renderQueued = true;
    requestAnimationFrame(() => { this._renderQueued = false; this._render(); });
  }

  async _save(key, msg, okText = "Saved") {
    this._status[key] = { busy: true };
    this._requestRender();
    let result = null;
    try {
      result = await this._ws(msg);
      this._status[key] = { ok: okText };
      setTimeout(() => { if (this._status[key]?.ok) { delete this._status[key]; this._requestRender(); } }, 2000);
    } catch (err) {
      this._status[key] = { error: err?.message || String(err) };
    }
    this._scheduleFetch();
    this._requestRender();
    return result;
  }

  _statusHtml(key) {
    const s = this._status[key];
    if (!s) return "";
    if (s.busy) return `<span class="status muted">Saving…</span>`;
    if (s.error) return `<span class="status bad" role="alert">${esc(s.error)}</span>`;
    return `<span class="status ok" role="status">✓ ${esc(s.ok)}</span>`;
  }

  get _canEdit() { return !!this._model?.can_edit; }
  get _ro() { return this._canEdit ? "" : "disabled"; }
  _win(id) { return this._model?.windows.find((w) => w.window_id === id); }
  _windows() {
    const all = this._model?.windows || [];
    return this._config.area ? all.filter((w) => w.area_id === this._config.area) : all;
  }

  // ------------------------------------------------------------------ render
  _render() {
    if (!this.isConnected || !this._config) return;
    if (!this._hass || (!this._model && !this._error)) {
      this.shadowRoot.innerHTML = `${STYLE}<ha-card><div class="pad muted">Loading…</div></ha-card>`;
      return;
    }
    if (this._error && !this._model) {
      this.shadowRoot.innerHTML = `${STYLE}<ha-card><div class="pad bad">Smart Occupancy curtains are not available: ${esc(this._error)}</div></ha-card>`;
      return;
    }
    const active = this.shadowRoot.activeElement;
    const focusSel = active && active.dataset ? Object.entries(active.dataset).map(([k, v]) => `[data-${k.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}="${CSS.escape(v)}"]`).join("") : "";
    const typing = active && active.tagName === "INPUT" && ["text", "number"].includes(active.type) ? active.value : null;
    const w = this._view && this._win(this._view);
    const name = this._pageName();
    const page = w ? this._windowView(w) : name === "modes" ? this._modesPage() : name === "schedule" ? this._schedulePage()
      : name === "windows" ? this._windowsPage() : name === "diagnostics" ? this._diagPage() : this._overview();
    this.shadowRoot.innerHTML = `${STYLE}<ha-card>${page}</ha-card>`;
    if (focusSel) {
      const el = this.shadowRoot.querySelector(`${active.tagName.toLowerCase()}${focusSel}`);
      if (el) { if (typing !== null) el.value = typing; el.focus({ preventScroll: true }); }
    }
  }

  _overview() {
    const m = this._model;
    const windows = this._windows();
    const ids = new Set(windows.map((w) => w.window_id));
    const rooms = m.rooms.map((r) => ({ ...r, windows: r.windows.filter((id) => ids.has(id)) })).filter((r) => r.windows.length);
    const props = this._config.area ? m.proposals.filter((p) => p.area_id === this._config.area) : m.proposals;
    const ui = this._ui;
    const h = m.schedule && m.schedule.house;
    let body = `<div class="header"><ha-icon icon="mdi:curtains"></ha-icon><div class="titles">
        <div class="title">${esc(this._config.title || "Curtains")}</div>
        <div class="sub">${windows.length} window${windows.length === 1 ? "" : "s"} · ${windows.filter((w) => w.mode === "automatic").length} automatic${h ? ` · ${esc(h.word)} now` : ""}</div></div></div>
      ${this._pageTabs()}
      <div class="pad">${this._reviewHtml()}${this._migrationHtml()}
      <div class="note">Every window follows the <button class="link" data-action="page" data-page="schedule">Global Schedule</button> and uses its presets from <button class="link" data-action="page" data-page="modes">Global Modes</button>; sleep, the window lamp, the sun and manual moves can take over for a while. Observing windows only say what they would do.</div>`;
    if (!windows.length) {
      body += `<div class="muted">No windows yet.${this._canEdit ? ` <button class="link" data-action="page" data-page="windows">Add one on the Windows page</button>.` : ""}</div>`;
    }
    body += `<div class="rooms">${rooms.map((room) => `<section class="room" aria-label="${esc(room.name)}"><div class="stitle">${esc(room.name)}</div>
      ${room.windows.map((id) => this._windowCard(this._win(id))).join("")}</section>`).join("")}</div>`;
    return `${body}</div>`;
  }

  _desired(w) {
    const d = w.auto && w.auto.decision;
    if (d && d.preset_id && d.target && d.target.state === "target") return { preset_id: d.preset_id, name: d.preset_name, positions: d.target.positions };
    return w.presets.find((x) => x.preset_id === w.selected_preset) || null;
  }

  _coverRow(l, w) {
    const op = w.last && w.last.status === "running" ? w.last.items.find((i) => i.layer_id === l.layer_id) : null;
    const want = this._desired(w);
    const target = op && op.target != null ? op.target : want ? want.positions[l.layer_id] : null;
    let state;
    if (l.missing) state = `<span class="bad">Not found</span>`;
    else if (!l.available) state = `<span class="warn">Unavailable</span>`;
    else state = `<b>${pct(l.position)}</b>${l.moving ? ` <span class="pill info">${l.state === "opening" ? "Opening" : "Closing"}</span>` : ""}`;
    const width = l.position == null ? 0 : l.position;
    return `<div class="crow"><ha-icon icon="${KIND_ICON[l.kind] || KIND_ICON.custom}"></ha-icon>
      <div class="cmain"><div class="cname">${esc(l.name)} <span class="sub">${esc(l.kind_word)}</span></div>
        <span class="bar" role="img" aria-label="${esc(l.name)}: ${l.position == null ? "position unknown" : `${l.position} % open`}"><i style="width:${width}%"></i></span></div>
      <div class="cval">${state}${target != null && (op || target !== l.position) ? `<div class="sub" title="Desired">→ ${pct(target)}</div>` : ""}</div></div>`;
  }

  _currentText(w) {
    const c = w.current;
    if (c.status === "preset") return `<b>${esc(c.text)}</b>`;
    if (c.status === "custom") return `<b>Custom position</b>`;
    return `<span class="muted">${esc(c.text)}</span>`;
  }

  _lastHtml(w) {
    const op = w.last;
    if (!op) return `<span class="muted">No preset applied yet</span>`;
    const word = this._model.words.overall[op.status] || op.status;
    return `<span class="pill ${OVERALL_TONE[op.status] || "muted"}">${esc(word)}</span> ${esc(op.explanation || "")}
      <span class="sub">· ${fmtTime(op.finished || op.started)}</span>`;
  }

  _manualHtml(w) {
    const m = w.manual;
    if (!m) return `<span class="muted">None</span>`;
    const who = m.source === "person" ? "by a person in Home Assistant" : "outside Smart Occupancy";
    const l = w.layers.find((x) => x.entity_id === m.entity_id);
    const name = w.layers.length > 1 ? `${esc(l ? l.label || l.kind_word : m.name)} ` : "";
    return `${name}${pct(m.from)} → ${pct(m.to)} <span class="sub">${fmtTime(m.at)}, ${who}</span>`;
  }

  // Sun, light, temperatures and the direct-sun / temperature assessments (0.6.3), compact for the overview.
  _envRows(w) {
    const env = w.auto && w.auto.environment;
    if (!env || w.mode === "off") return "";
    const sun = env.sun;
    const r = env.readings || {};
    const rows = [];
    const rel = sun.offset == null ? "—" : `${Math.abs(Math.round(sun.offset))}° ${sun.offset >= 0 ? "right" : "left"} of the window`;
    rows.push(`<div><span>Sun</span><b>${sun.elevation == null ? "position unavailable" : `${esc(rel)} · elevation ${deg(sun.elevation)}`}</b></div>`);
    if (w.solar_protection && env.direct_sun) {
      const d = env.direct_sun;
      rows.push(`<div><span>Direct sun</span><b><span class="pill ${DIRECT_TONE[d.status] || "muted"}">${esc(DIRECT_WORD[d.status] || d.status)}</span></b></div>`);
    }
    rows.push(`<div><span>Outdoor light</span><b>${this._readingShort(r.outdoor_lux)}</b></div>`);
    const temps = [];
    if (r.room_temp && r.room_temp.status !== "none") temps.push(`room ${this._readingShort(r.room_temp)}${env.band_word && env.thermal_enabled ? ` <span class="pill ${BAND_TONE[env.band] || "muted"}">${esc(env.band_word)}</span>` : ""}`);
    if (r.outdoor_temp && r.outdoor_temp.status !== "none") temps.push(`outdoor ${this._readingShort(r.outdoor_temp)}`);
    if (temps.length) rows.push(`<div><span>Temperature</span><b>${temps.join(" · ")}</b></div>`);
    const d = w.auto.decision;
    if (d && (["glare", "heat", "solar_heating"].includes(d.base) || env.thermal_enabled || env.glare.enabled)) {
      const word = ["glare", "heat", "solar_heating"].includes(d.base) ? d.base_word : env.thermal && env.thermal.state !== "off" ? "Temperature" : "Glare";
      const band = env.thermal && env.thermal.state === "unknown" ? " · temperature unknown" : "";
      rows.push(`<div><span>Glare / heat</span><b>${esc(word)}${band}</b></div>`);
    }
    return rows.join("");
  }

  _readingShort(x) {
    if (!x) return `<span class="muted">—</span>`;
    if (x.status === "none") return `<span class="muted">not chosen</span>`;
    const val = x.value == null ? "" : x.unit === "lx" ? `${Math.round(x.value)} lx` : `${num1(x.value)} ${esc(x.unit)}`;
    if (x.status === "ok") return esc(val);
    return `${val ? `${esc(val)} ` : ""}<span class="pill ${READING_TONE[x.status] || "warn"}" title="${esc(x.detail || "")}">${esc(x.status_word)}</span>`;
  }

  _readingRow(label, x) {
    if (!x) return `<div class="prow"><div class="pmain"><b>${esc(label)}</b><span class="sub">not evaluated yet</span></div></div>`;
    const val = x.value == null ? "" : x.unit === "lx" ? `${Math.round(x.value)} lx` : `${num1(x.value)} ${esc(x.unit)}`;
    const since = x.since && x.status !== "none" ? ` · value since <time datetime="${esc(x.since)}">${fmtTime(x.since)}</time>` : "";
    const rep = x.reported && x.status !== "none" ? ` · reported <time datetime="${esc(x.reported)}">${fmtTime(x.reported)}</time>` : "";
    return `<div class="prow"><div class="pmain"><b>${esc(label)}</b>${val ? `<span>${esc(val)}</span>` : ""}<span class="sub">${x.entity_id ? `<code>${esc(x.entity_id)}</code>` : ""}${since}${rep}</span></div>
      <span class="pill ${READING_TONE[x.status] || "muted"}">${esc(x.status_word)}</span>
      ${x.detail && x.status !== "ok" ? `<div class="why">${esc(x.detail)}</div>` : ""}</div>`;
  }

  _solarText(w) {
    if (!w.solar_protection) return `<span class="muted">Off</span>${w.exposure.status === "exposed" ? ` <span class="sub">· sun on the window now</span>` : ""}`;
    const tone = EXPO_TONE[w.exposure.status] || "muted";
    return `On · <span class="${tone === "warn" ? "warn" : "muted"}">${esc(w.exposure.text)}</span>`;
  }

  // Automatic: the condition, its preset, why, and the evidence (overview and Automatic tab).
  _autoBlock(w, full = false) {
    const a = w.auto;
    if (w.mode === "off") return `<div class="auto muted"><ha-icon icon="mdi:power"></ha-icon><div class="amain">Off: curtains are not evaluated.</div></div>`;
    const d = a && a.decision;
    if (!d) return `<div class="auto muted"><div class="amain">Evaluating…</div></div>`;
    const tgt = d.target || {};
    const what = d.preset_name ? `${esc(d.preset_name)} <span class="pill ${SRC_TONE[d.source] || "muted"}" title="Where the preset comes from">${esc(d.source_word || "")}</span>`
      : d.condition === "manual_override" || d.condition === "waiting" ? "keeps the position"
      : tgt.state === "no_action" ? `No action <span class="sub">(${esc(d.source_word || "")})</span>` : "no preset assigned";
    const st = a.status;
    const extra = st && st.text && st.text !== d.text ? `<div class="astatus"><span class="pill ${STATUS_TONE[st.action] || "muted"}">${esc(st.word)}</span> ${esc(st.text)}</div>`
      : st ? `<div class="astatus"><span class="pill ${STATUS_TONE[st.action] || "muted"}">${esc(st.word)}</span> <span class="sub">since ${fmtTime(st.at)}</span></div>` : "";
    const chips = [];
    if (a.sleep.enabled) chips.push(`<span class="chip small ${a.sleep.active ? "on" : ""}" title="${esc(a.sleep.entity || "")}"><ha-icon icon="mdi:sleep"></ha-icon>Sleep ${esc(a.sleep.state || "—")}</span>`);
    if (a.lamp.enabled) chips.push(`<span class="chip small ${a.lamp.active ? "on" : ""}" title="${esc(a.lamp.entity || "")}"><ha-icon icon="mdi:lamp"></ha-icon>Lamp ${esc(a.lamp.state || "—")}${a.lamp.active ? " · override" : ""}</span>`);
    if (a.hold) chips.push(`<span class="chip small warnchip"><ha-icon icon="mdi:hand-back-right-outline"></ha-icon>Manual hold · ${esc(a.hold.policy_word)}</span>`);
    const light = a.light;
    chips.push(`<span class="chip small" title="${esc(light.entity || "")}"><ha-icon icon="mdi:brightness-6"></ha-icon>${light.entity ? `${light.value == null ? "light unavailable" : `${Math.round(light.value)} lx`}${light.daylight === true ? " · daylight" : light.daylight === false ? " · dark" : ""}` : light.fallback === "sun" ? "no light sensor · sun fallback" : "no light sensor"}</span>`);
    if (w.solar_protection) chips.push(`<span class="chip small ${w.exposure.exposed ? "warnchip" : ""}"><ha-icon icon="mdi:weather-sunny"></ha-icon>${esc(w.exposure.exposed ? "sun in front" : w.exposure.text)}${light.strong ? " · strong" : ""}</span>`);
    const lr = a.environment && a.environment.readings && a.environment.readings.outdoor_lux;
    if (lr && ["stuck", "implausible", "stale", "unavailable", "missing"].includes(lr.status)) chips.push(`<span class="chip small warnchip" title="${esc(lr.detail || "")}"><ha-icon icon="mdi:alert-outline"></ha-icon>light sensor ${esc(lr.status_word.toLowerCase())}</span>`);
    const resume = this._canEdit && (a.hold || (st && ["blocked", "failed"].includes(st.action) && st.code === "gave_up"))
      ? `<button class="btn small" data-action="resume" data-window="${esc(w.window_id)}"><ha-icon icon="mdi:play-circle-outline"></ha-icon>Resume automatic curtains</button>` : "";
    return `<div class="auto ${w.mode}"><ha-icon icon="${COND_ICON[d.condition] || "mdi:robot-outline"}"></ha-icon><div class="amain">
        <div class="ahead"><b>${esc(d.condition_word)}</b> · ${what}${w.mode === "observing" ? ` <span class="pill info">Observing</span>` : ""}</div>
        <div class="sub managed">Managed in ${MODE_OF[d.condition] ? `<button class="link" data-action="goto-mode" data-mode="${MODE_OF[d.condition]}">Global Modes → ${esc(this._modeWord(MODE_OF[d.condition]))}</button> and ` : ""}<button class="link" data-action="page" data-page="schedule">Global Schedule</button></div>
        <div class="atext">${esc(d.text)}</div>${extra}
        <div class="chips">${chips.join("")}</div>
        ${full && d.evidence && d.evidence.length ? `<div class="sub">${d.evidence.map(esc).join(" · ")}</div>` : ""}
        ${full && d.direct_sun && d.direct_sun.status && d.direct_sun.status !== "off" ? `<div class="sub"><b>Direct sun:</b> ${esc(d.direct_sun.text)}</div>` : ""}
        ${full && d.thermal && d.thermal.state !== "off" ? `<div class="sub"><b>Temperature:</b> ${esc(d.thermal.text)}</div>` : ""}
        ${resume ? `<div class="actions">${resume}${this._statusHtml(`resume-${w.window_id}`)}</div>` : this._statusHtml(`resume-${w.window_id}`)}</div></div>`;
  }

  _enableHtml(w) {
    const e = this._ui.enable;
    if (!e || e.window_id !== w.window_id) return "";
    if (!e.review) return `<div class="confirm ask" role="dialog" aria-label="Enable Automatic"><div class="muted">${e.error ? `<span class="bad">${esc(e.error)}</span>` : "Checking the window…"}</div>
      <div class="actions"><button class="btn ghost" data-action="close-enable">Close</button></div></div>`;
    const r = e.review;
    const d = r.decision;
    const rows = r.items.map((i) => `<div class="prow"><div class="pmain"><ha-icon icon="${KIND_ICON[i.kind] || KIND_ICON.custom}"></ha-icon><b>${esc(i.name)}</b>
        <span class="sub">${i.target == null ? "Leave unchanged" : `${pct(i.current)} → <b>${pct(i.target)}</b>`}</span></div>
        <span class="pill ${PLAN_TONE[i.decision] || "muted"}">${esc(i.word)}</span></div>`).join("");
    const issues = (list, tone) => list.map((t) => `<div class="issue ${tone}"><ha-icon icon="${tone === "bad" ? "mdi:close-octagon-outline" : "mdi:alert-outline"}"></ha-icon><div class="imain">${esc(t)}</div></div>`).join("");
    return `<div class="confirm ask" role="dialog" aria-label="Enable Automatic">
      <div class="chead">Enable Automatic for <b>${esc(w.name)}</b>?</div>
      <div>Now: <b>${esc(d ? d.condition_word : "—")}</b> → <b>${esc(r.preset_name || "keeps the position")}</b></div>
      <div class="sub">${esc(d ? d.text : "")}</div>
      ${rows ? `<div class="stitle small">${r.moves ? `${r.moves} cover${r.moves === 1 ? "" : "s"} will move at once` : "No cover needs to move now"}</div>${rows}` : ""}
      ${r.blockers.length ? `<div class="stitle small">Must be resolved first</div>${issues(r.blockers, "bad")}` : ""}
      ${r.warnings.length ? `<div class="stitle small">Good to know</div>${issues(r.warnings, "warn")}` : ""}
      <details class="dladv"><summary>Order of precedence</summary><ol class="prec">${r.precedence.map((p) => `<li>${esc(p.word)}</li>`).join("")}</ol></details>
      <div class="note warn">From now on this window moves on its own, without asking again. Every movement is explained here and in Diagnostics. Other windows stay as they are.</div>
      ${e.error ? `<div class="bad" role="alert">${esc(e.error)}</div>` : ""}
      <div class="actions"><button class="btn primary" data-action="enable-confirm" ${r.blockers.length || e.sending ? "disabled" : ""}>Enable Automatic</button>
        <button class="btn ghost" data-action="close-enable" ${e.sending ? "disabled" : ""}>Cancel</button></div></div>`;
  }

  _windowCard(w) {
    if (!w) return "";
    const ro = this._ro;
    const sel = w.selected_preset;
    const running = w.running;
    const dir = w.compass ? `${w.compass}${w.bearing != null ? ` ${Math.round(w.bearing)}°` : ""}` : "No direction";
    const can = this._canEdit && w.mode !== "off" && !running && sel && w.layers.length;
    return `<article class="wcard ${w.problems.some((p) => p.severity === "warning") ? "haswarn" : ""}" aria-label="${esc(w.name)}">
      <div class="whead"><button class="wname" data-action="open-window" data-id="${esc(w.window_id)}">${esc(w.name)}<ha-icon icon="mdi:chevron-right"></ha-icon></button>
        <span class="badge" title="Direction">${esc(dir)}</span>
        <span class="pill ${MODE_TONE[w.mode]}" title="Operating mode">${esc(w.mode_word)}</span></div>
      ${w.layers.length ? w.layers.map((l) => this._coverRow(l, w)).join("") : `<div class="muted">No covers yet.</div>`}
      ${this._autoBlock(w)}
      <div class="kvs one">
        <div><span>Now</span><b>${this._currentText(w)}</b></div>
        <div><span>Sun protection</span><b>${this._solarText(w)}</b></div>
        ${this._envRows(w)}
        <div><span>Manual change</span><b>${this._manualHtml(w)}</b></div>
        <div><span>Last movement</span><b>${this._lastHtml(w)}</b></div>
        ${w.mode !== "off" ? `<div><span>Next check</span><b>${w.auto && w.auto.next_evaluation ? `<time datetime="${esc(w.auto.next_evaluation)}">${fmtTime(w.auto.next_evaluation)}</time>` : "—"}</b></div>` : ""}
      </div>
      ${w.problems.filter((p) => p.severity === "warning").slice(0, 2).map((p) => `<div class="issue warn"><ha-icon icon="mdi:alert-outline"></ha-icon><div class="imain">${esc(p.text)}</div></div>`).join("")}
      <div class="ctl"><label class="sr" for="sel-${esc(w.window_id)}">Preset for ${esc(w.name)}</label>
        <select id="sel-${esc(w.window_id)}" data-control="sel-preset" data-window="${esc(w.window_id)}" ${ro || running ? "disabled" : ""}>
          <option value="" ${sel ? "" : "selected"}>Choose a preset…</option>
          ${w.presets.map((p) => `<option value="${esc(p.preset_id)}" ${p.preset_id === sel ? "selected" : ""}>${esc(p.name)}${p.matches ? " ✓" : ""}</option>`).join("")}</select>
        <button class="btn" data-action="apply" data-window="${esc(w.window_id)}" data-preset="${esc(sel || "")}" ${can ? "" : "disabled"}
          title="${w.mode === "off" ? "This window is Off" : running ? "A preset is being applied" : !sel ? "Choose a preset first" : "Shows what moves, then asks you to confirm"}">Apply…</button>
        ${sel ? `<button class="btn ghost" data-action="preview" data-window="${esc(w.window_id)}" data-preset="${esc(sel)}">Preview</button>` : ""}
        ${this._statusHtml(`apply-${w.window_id}`)}${this._statusHtml(`sel-${w.window_id}`)}</div>
      ${this._confirmHtml(w)}
    </article>`;
  }

  // Preview (nothing moves) and the confirmation that lists every cover and its target.
  _confirmHtml(w) {
    const c = this._ui.confirm;
    if (!c || c.window_id !== w.window_id) return "";
    if (!c.plan) return `<div class="confirm" role="dialog" aria-label="Preset"><div class="muted">${c.error ? `<span class="bad">${esc(c.error)}</span>` : "Checking the covers…"}</div>
      <div class="actions"><button class="btn ghost" data-action="close-confirm">Close</button></div></div>`;
    const p = c.plan;
    const preview = c.mode === "preview";
    const rows = p.items.map((i) => `<div class="prow"><div class="pmain"><ha-icon icon="${KIND_ICON[i.kind] || KIND_ICON.custom}"></ha-icon><b>${esc(i.name)}</b>
        <span class="sub">${i.target == null ? "Leave unchanged" : `${pct(i.current)} → <b>${pct(i.target)}</b>`}</span></div>
        <span class="pill ${PLAN_TONE[i.decision] || "muted"}">${esc(i.word)}</span>
        ${i.decision !== "unchanged" ? `<div class="why">${esc(i.reason)}</div>` : ""}</div>`).join("");
    const head = preview ? `Preview: <b>${esc(p.preset_name)}</b> — nothing moves`
      : `Apply <b>${esc(p.preset_name)}</b> to ${esc(w.name)}?`;
    const moves = p.moves;
    let foot;
    if (preview) {
      foot = `<button class="btn" data-action="apply" data-window="${esc(w.window_id)}" data-preset="${esc(p.preset_id)}" ${this._canEdit && w.mode !== "off" && !w.running ? "" : "disabled"}>Apply…</button>
        <button class="btn ghost" data-action="close-confirm">Close</button>`;
    } else if (!moves) {
      foot = `<span class="sub">No cover needs a command.</span><button class="btn ghost" data-action="close-confirm">Close</button>`;
    } else {
      foot = `<button class="btn primary" data-action="apply-confirm" ${c.sending ? "disabled" : ""}>Move ${moves} cover${moves === 1 ? "" : "s"}</button>
        <button class="btn ghost" data-action="close-confirm" ${c.sending ? "disabled" : ""}>Cancel</button>`;
    }
    return `<div class="confirm ${preview ? "" : "ask"}" role="dialog" aria-label="${preview ? "Preview" : "Confirm preset"}">
      <div class="chead">${head}</div>${rows}
      ${!preview && moves ? `<div class="note warn">This sends real commands to ${moves} cover${moves === 1 ? "" : "s"}${w.mode === "observing" ? " (Observing: allowed because you ask for it)" : ""}.</div>` : ""}
      ${c.error ? `<div class="bad" role="alert">${esc(c.error)}</div>` : ""}
      <div class="actions">${foot}</div></div>`;
  }

  _settingsHtml() {
    const s = this._model.settings;
    const sun = this._model.sun;
    const lim = this._model.limits;
    const ro = this._ro;
    return `<div class="section"><div class="stitle">Settings for every window</div>
      <div class="frow"><label for="set-tol">Position tolerance</label><div class="fval"><input id="set-tol" type="number" class="num" min="${lim.tolerance[0]}" max="${lim.tolerance[1]}"
        value="${esc(s.tolerance)}" data-control="set-num" data-key="tolerance" ${ro}> points <span class="sub">a cover this close to a target is at it and gets no command</span></div></div>
      <div class="frow"><label for="set-to">Verify for</label><div class="fval"><input id="set-to" type="number" class="num" min="${lim.verify_timeout[0]}" max="${lim.verify_timeout[1]}"
        value="${esc(s.verify_timeout)}" data-control="set-num" data-key="verify_timeout" ${ro}> s <span class="sub">how long to wait for the reported position</span></div></div>
      <div class="frow"><label for="set-az">Sun azimuth</label><div class="fval"><input id="set-az" type="text" value="${esc(s.azimuth_entity)}" data-control="set-ent" data-key="azimuth_entity" ${ro}></div></div>
      <div class="frow"><label for="set-el">Sun elevation</label><div class="fval"><input id="set-el" type="text" value="${esc(s.elevation_entity)}" data-control="set-ent" data-key="elevation_entity" ${ro}></div></div>
      <div class="frow"><label for="set-sa">Sun angle</label><div class="fval"><input id="set-sa" type="text" value="${esc(s.sun_angle_entity || "")}" placeholder="none" data-control="set-ent" data-key="sun_angle_entity" ${ro}>
        <span class="sub">${sun.sources.sun_angle && !sun.sources.sun_angle_exists ? "not found" : ""}</span></div></div>
      <div class="hint">sun.sun gives the standard azimuth and elevation. The sun angle (0° = the sun faces the south-facing windows) is shown and checked against the azimuth; it is not used to move anything.</div>
      ${this._statusHtml("settings")}${this._qualityHtml()}</div>`;
  }

  // Sensor-quality summary (0.6.3): every candidate per role with its validity now.
  _qualityHtml() {
    const env = this._model.environment;
    if (!env) return "";
    const roles = env.roles.filter((r) => r.candidates.length || r.chosen.length);
    const body = roles.map((r) => {
      const key = ROLE_RULE[r.role];
      const items = r.candidates.slice(0, 8).map((x) => {
        const val = x.value == null ? "" : x.unit === "lx" ? `${Math.round(x.value)} lx` : `${num1(x.value)} ${esc(x.unit)}`;
        const isGlobal = key && env.global[key] === x.entity_id;
        return `<div class="prow"><div class="pmain"><b>${esc(x.name)}</b><span class="sub"><code>${esc(x.entity_id)}</code>${val ? ` · ${esc(val)}` : ""}${x.since ? ` · since ${fmtTime(x.since)}` : ""}${x.in_use ? " · in use" : ""}${isGlobal ? " · all windows" : ""}</span></div>
          <span class="pill ${READING_TONE[x.status] || "muted"}">${esc(x.status_word)}</span>
          ${x.status !== "ok" && x.detail ? `<div class="why">${esc(x.detail)}</div>` : ""}
          ${key && this._canEdit && !isGlobal && x.status === "ok" ? `<div class="why"><button class="link" data-action="env-use" data-key="${key}" data-entity="${esc(x.entity_id)}">Use for all windows</button></div>` : ""}</div>`;
      }).join("");
      return `<details class="dladv" data-group="q-${r.role}" ${this._ui.open?.[`q-${r.role}`] ? "open" : ""}><summary>${esc(r.word)} <span class="sub">${r.candidates.filter((x) => x.status === "ok").length} valid of ${r.candidates.length}</span></summary>
        ${items || `<div class="muted">None found.</div>`}
        ${key ? "" : `<div class="hint">Chosen per room or window (its Environment tab).</div>`}</details>`;
    }).join("");
    return `<div class="stitle small">Environment sensors</div>
      <div class="hint">Found by type and unit; nothing is used until you choose it. A sensor that reports the same daylight value for hours is marked stuck and is not trusted.</div>${body || `<div class="muted">No light, temperature or weather entities found.</div>`}
      ${this._statusHtml("env-use")}`;
  }

  _proposalsHtml(props) {
    if (!props.length) return "";
    return `<div class="section"><div class="stitle">Suggested windows</div><div class="hint">Covers that seem to belong together (same room, same name apart from the layer). Review each one before it is created; nothing moves.</div>
      ${props.map((p) => `<div class="prop"><div class="pmain"><b>${esc(p.name)}</b> <span class="sub">${esc(p.area_name || "No room")}</span></div>
        <div class="sub">${p.covers.map((c) => `${esc(c.name)} (${esc(this._model.words.kinds[c.kind])})`).join(" · ")}</div>
        <button class="btn small" data-action="review-proposal" data-key="${esc(p.key)}">Review</button></div>`).join("")}</div>`;
  }

  _addFormHtml() {
    const f = this._ui.adding;
    const m = this._model;
    const chosen = new Map(f.layers.map((l) => [l.entity_id, l]));
    const cands = [...m.candidates].sort((a, b) => (b.area_id === f.area_id) - (a.area_id === f.area_id));
    return `<div class="editor" role="group" aria-label="New window"><div class="ehead"><div class="etitle">New window</div></div>
      <div class="frow"><label for="nw-name">Name</label><div class="fval"><input id="nw-name" type="text" maxlength="60" value="${esc(f.name)}" data-control="nw-name" placeholder="e.g. Stova Suður"></div></div>
      <div class="frow"><label for="nw-area">Room</label><div class="fval"><select id="nw-area" data-control="nw-area"><option value="">No room</option>
        ${m.areas.map((a) => `<option value="${esc(a.area_id)}" ${a.area_id === f.area_id ? "selected" : ""}>${esc(a.name)}</option>`).join("")}</select></div></div>
      <div class="frow"><label for="nw-dir">Direction</label><div class="fval"><select id="nw-dir" data-control="nw-dir"><option value="">Not set yet</option>
        ${m.compass.map((d) => `<option ${d === f.direction ? "selected" : ""}>${d}</option>`).join("")}</select><span class="sub">exact degrees under General later</span></div></div>
      <div class="stitle small">Covers</div>
      <div class="clist">${cands.map((c) => {
        const own = chosen.get(c.entity_id);
        const taken = !!c.assigned_to;
        const other = taken ? this._win(c.assigned_to) : null;
        return `<div class="cand ${taken ? "taken" : ""}"><label class="chk"><input type="checkbox" data-control="nw-cover" data-entity="${esc(c.entity_id)}" ${own ? "checked" : ""} ${taken ? "disabled" : ""}>
          <span><b>${esc(c.name)}</b> <span class="sub">${esc(c.area_name || "No room")} · ${c.available ? pct(c.position) : "unavailable"} · ${c.caps.position ? "positions" : "open / close only"}${other ? ` · in ${esc(other.name)}` : ""}</span></span></label>
          ${own ? `<select data-control="nw-kind" data-entity="${esc(c.entity_id)}" aria-label="Type of ${esc(c.name)}">${m.kinds.map((k) => `<option value="${k}" ${k === own.kind ? "selected" : ""}>${esc(m.words.kinds[k])}</option>`).join("")}</select>` : ""}</div>`;
      }).join("") || `<div class="muted">No cover entities found.</div>`}</div>
      <div class="hint">Presets are created from the covers: Fully open / Half open / Fully closed, or five coordinated presets when a window has a sheer and a blackout layer. Every position can be changed afterwards.</div>
      <div class="erow"><span>${this._statusHtml("create")}</span><div class="actions"><button class="btn" data-action="create-window" ${f.name.trim() ? "" : "disabled"}>Create window</button>
        <button class="btn ghost" data-action="cancel-add">Cancel</button></div></div></div>`;
  }

  // ------------------------------------------------------------- window view
  _windowView(w) {
    const tab = this._tab;
    const current = TABS.find(([k]) => k === tab) || TABS[0];
    const dir = w.compass ? `${w.compass}${w.bearing != null ? ` (${Math.round(w.bearing)}°)` : ""}` : "no direction";
    const content = {
      general: () => this._generalTab(w), covers: () => this._coversTab(w), presets: () => this._presetsTab(w),
      automation: () => this._automationTab(w), sun: () => this._sunTab(w), environment: () => this._environmentTab(w),
      diagnostics: () => this._diagTab(w),
    }[current[0]]();
    return `<div class="header"><button class="subback" data-action="back" aria-label="Back to all windows"><ha-icon icon="mdi:arrow-left"></ha-icon><span>Windows</span></button>
        <div class="titles"><div class="title">${esc(w.name)}</div><div class="sub">${esc(w.area_name || "No room")} · ${esc(dir)} · ${esc(w.mode_word)}</div></div></div>
      <div class="tabs" role="tablist" aria-label="${esc(w.name)}: sections">${TABS.map(([k, l, icon]) => {
        const on = k === current[0];
        return `<button role="tab" id="tab-${k}" aria-selected="${on}" aria-controls="so-cpanel" tabindex="${on ? 0 : -1}" class="tab ${on ? "on" : ""}"
          data-action="tab" data-tab="${k}" aria-label="${esc(l)}" data-tip="${esc(l)}"><ha-icon icon="${icon}"></ha-icon><span class="tlabel" aria-hidden="true">${l}</span></button>`;
      }).join("")}</div>
      <div class="tabcap" aria-hidden="true">${esc(current[1])}</div>
      <div class="pad" role="tabpanel" id="so-cpanel" aria-labelledby="tab-${current[0]}">${this._enableHtml(w)}${this._confirmHtml(w)}${this._simHtml(w)}${this._reviewHtml()}${content}</div>`;
  }

  _generalTab(w) {
    const m = this._model;
    const ro = this._ro;
    const seg = (on, attrs, label, extra = "") => `<button class="seg ${on ? "on" : ""}" aria-pressed="${on}" ${attrs} ${ro} ${extra}>${label}</button>`;
    return `<div class="frow"><label for="w-name">Name</label><div class="fval"><input id="w-name" type="text" maxlength="60" value="${esc(w.name)}" data-control="w-name" ${ro}></div></div>
      <div class="frow"><label for="w-area">Room</label><div class="fval"><select id="w-area" data-control="w-area" ${ro}><option value="">No room</option>
        ${m.areas.map((a) => `<option value="${esc(a.area_id)}" ${a.area_id === w.area_id ? "selected" : ""}>${esc(a.name)}</option>`).join("")}</select></div></div>
      <div class="frow"><label>Direction</label><div class="fval"><div class="segs compass" role="group" aria-label="Compass direction">
        ${m.compass.map((d) => seg(w.direction === d, `data-action="direction" data-dir="${d}"`, d)).join("")}</div></div></div>
      <div class="frow"><label for="w-bearing">Exact</label><div class="fval"><input id="w-bearing" type="number" class="num" min="0" max="360" step="0.5"
        value="${esc(w.bearing ?? "")}" placeholder="${esc(w.effective_bearing ?? "°")}" data-control="w-bearing" ${ro}> ° <span class="sub">0 = north, 90 = east, 180 = south; replaces the compass point</span></div></div>
      <div class="frow"><label>Mode</label><div class="fval"><div class="segs" role="group" aria-label="Operating mode">
        ${seg(w.mode === "off", `data-action="mode" data-mode="off"`, "Off")}${seg(w.mode === "observing", `data-action="mode" data-mode="observing"`, "Observing")}${seg(w.mode === "automatic", `data-action="mode" data-mode="automatic"`, "Automatic…")}</div>
        <span class="sub">${w.mode === "off" ? "Off: not evaluated, and presets cannot be applied to this window." : w.mode === "observing" ? "Observing: decides and explains what it would do; nothing moves on its own. You can still apply a preset yourself." : "Automatic: moves this window on its own and explains every decision. A preset you apply yourself is kept like a manual move."}</span></div></div>
      ${this._statusHtml(`w-${w.window_id}`)}
      ${this._canEdit ? `<div class="erow"><span class="sub">Window id <code>${esc(w.window_id)}</code></span><button class="btn warnbtn small" data-action="remove-window">Delete window</button></div>` : ""}`;
  }

  _coversTab(w) {
    const m = this._model;
    const ro = this._ro;
    const layers = w.layers.map((l) => `<div class="lcard"><div class="crow"><ha-icon icon="${KIND_ICON[l.kind]}"></ha-icon>
        <div class="cmain"><div class="cname">${esc(l.name)}</div><div class="sub"><code>${esc(l.entity_id)}</code> · ${l.caps.position ? "positions" : l.caps.open || l.caps.close ? "open / close only" : "no movement reported"}${l.members.length ? ` · group of ${l.members.length}` : ""}</div>
          <span class="bar"><i style="width:${l.position ?? 0}%"></i></span></div>
        <div class="cval">${l.missing ? `<span class="bad">Not found</span>` : !l.available ? `<span class="warn">Unavailable</span>` : `<b>${pct(l.position)}</b>`}</div></div>
      <div class="frow"><label for="lk-${esc(l.layer_id)}">Type</label><div class="fval"><select id="lk-${esc(l.layer_id)}" data-control="layer-kind" data-layer="${esc(l.layer_id)}" ${ro}>
        ${m.kinds.map((k) => `<option value="${k}" ${k === l.kind ? "selected" : ""}>${esc(m.words.kinds[k])}</option>`).join("")}</select></div></div>
      <div class="frow"><label for="ll-${esc(l.layer_id)}">Own name</label><div class="fval"><input id="ll-${esc(l.layer_id)}" type="text" maxlength="60" value="${esc(l.label || "")}" placeholder="${esc(l.entity_name)}"
        data-control="layer-label" data-layer="${esc(l.layer_id)}" ${ro}>${this._canEdit ? `<button class="link" data-action="remove-layer" data-layer="${esc(l.layer_id)}">Remove</button>` : ""}</div></div>
      ${w.layered ? this._coverPresetsHtml(w, l) : ""}</div>`).join("");
    const free = m.candidates.filter((c) => !c.assigned_to);
    const sorted = [...free].sort((a, b) => (b.area_id === w.area_id) - (a.area_id === w.area_id) || a.name.localeCompare(b.name));
    const add = this._canEdit && w.layers.length < m.limits.layers ? `<div class="section"><div class="stitle small">Add a cover</div>
      <div class="fval"><select data-control="add-entity" aria-label="Cover to add"><option value="">Choose a cover…</option>
        ${sorted.map((c) => `<option value="${esc(c.entity_id)}" ${this._ui.addEntity === c.entity_id ? "selected" : ""}>${esc(c.name)} · ${esc(c.area_name || "No room")}${c.caps.position ? "" : " · open/close only"}</option>`).join("")}</select>
        <button class="btn small" data-action="add-layer" ${this._ui.addEntity ? "" : "disabled"}>Add</button></div>
      <div class="hint">Covers already in another window are not offered: one cover belongs to one window. A new cover is "Leave unchanged" in the existing presets until you set it.</div></div>` : "";
    const single = w.layers.length === 1 ? `<div class="hint">With one cover, this window's presets (Presets tab) are the cover's presets.</div>` : "";
    return `${layers || `<div class="muted">No covers yet.</div>`}${single}${this._statusHtml(`w-${w.window_id}`)}${this._statusHtml(`cp-${w.window_id}`)}${add}`;
  }

  _positionsText(w, p) {
    return w.layers.map((l) => `${esc(l.label || l.kind_word)} ${p.positions[l.layer_id] == null ? "unchanged" : pct(p.positions[l.layer_id])}`).join(" · ");
  }

  _presetsTab(w) {
    const ed = this._ui.preset && this._ui.preset.window_id === w.window_id ? this._ui.preset : null;
    const ro = this._ro;
    const can = this._canEdit;
    let html = `<div class="hint">Select the preset you want, then Apply. Selecting, editing and previewing never move anything.</div>`;
    if (!w.layers.length) return `${html}<div class="muted">Add covers first.</div>`;
    html += w.presets.map((p) => {
      if (ed && ed.preset_id === p.preset_id) return this._presetEditor(w, ed);
      const selected = p.preset_id === w.selected_preset;
      return `<div class="pcard ${selected ? "sel" : ""}"><div class="phead"><b>${esc(p.name)}</b>
          ${selected ? `<span class="pill info">Selected</span>` : ""}${p.matches ? `<span class="pill ok">Matches now</span>` : ""}</div>
        <div class="sub">${this._positionsText(w, p)}</div>
        <div class="ibtns">${can && !selected ? `<button class="btn small ghost" data-action="select" data-preset="${esc(p.preset_id)}">Select</button>` : ""}
          <button class="btn small ghost" data-action="preview" data-window="${esc(w.window_id)}" data-preset="${esc(p.preset_id)}">Preview</button>
          ${can ? `<button class="btn small" data-action="apply" data-window="${esc(w.window_id)}" data-preset="${esc(p.preset_id)}" ${w.mode === "off" || w.running ? "disabled" : ""}>Apply…</button>
          <button class="link" data-action="edit-preset" data-preset="${esc(p.preset_id)}">Edit</button>
          <button class="link" data-action="dup-preset" data-preset="${esc(p.preset_id)}">Duplicate</button>
          <button class="link" data-action="del-preset" data-preset="${esc(p.preset_id)}">Delete</button>` : ""}</div></div>`;
    }).join("");
    if (ed && !ed.preset_id) html += this._presetEditor(w, ed);
    if (can && !ed && w.presets.length < this._model.limits.presets) html += `<div class="actions"><button class="btn" data-action="new-preset" ${ro}><ha-icon icon="mdi:plus"></ha-icon>New preset</button></div>`;
    return html + this._statusHtml(`p-${w.window_id}`);
  }

  _presetEditor(w, ed) {
    const rows = w.layers.map((l) => {
      const v = ed.positions[l.layer_id];
      const leave = v == null;
      const id = esc(l.layer_id);
      const warn = !l.caps.position && !leave && v !== 0 && v !== 100;
      return `<div class="perow"><div class="pename"><ha-icon icon="${KIND_ICON[l.kind]}"></ha-icon><b>${esc(l.name)}</b> <span class="sub">${esc(l.kind_word)} · now ${pct(l.position)}</span></div>
        <div class="fval"><input type="range" min="0" max="100" step="1" value="${esc(leave ? (l.position ?? 50) : v)}" data-control="pe-range" data-layer="${id}" aria-label="${esc(l.name)} position" ${leave ? "disabled" : ""}>
          <input type="number" class="num" min="0" max="100" step="1" value="${esc(leave ? "" : v)}" data-control="pe-num" data-layer="${id}" aria-label="${esc(l.name)} position in %" ${leave ? "disabled" : ""}> %
          <label class="chk"><input type="checkbox" data-control="pe-leave" data-layer="${id}" ${leave ? "checked" : ""}>Leave unchanged</label></div>
        <div class="chips">${[0, 25, 50, 75, 100].map((q) => `<button class="chip small ${v === q ? "on" : ""}" data-action="quick" data-layer="${id}" data-value="${q}">${q} %</button>`).join("")}</div>
        ${warn ? `<div class="sub warn">This cover can only open or close fully: use 0 % or 100 %.</div>` : ""}</div>`;
    }).join("");
    const bad = ed.error ? `<div class="bad" role="alert">${esc(ed.error)}</div>` : "";
    return `<div class="editor" role="group" aria-label="${ed.preset_id ? "Edit preset" : "New preset"}"><div class="ehead"><div class="etitle">${ed.preset_id ? "Edit preset" : "New preset"}</div></div>
      <div class="frow"><label for="pe-name">Name</label><div class="fval"><input id="pe-name" type="text" maxlength="60" value="${esc(ed.name)}" data-control="pe-name"></div></div>
      ${rows}${bad}<div class="hint">0 % is closed, 100 % is fully open. Saving changes the preset only; it does not move the covers.</div>
      <div class="erow"><span>${this._statusHtml(`p-${w.window_id}`)}</span><div class="actions"><button class="btn" data-action="save-preset" ${ed.name.trim() ? "" : "disabled"}>Save</button>
        <button class="btn ghost" data-action="cancel-preset">Cancel</button></div></div></div>`;
  }

  _automationTab(w) {
    const m = this._model;
    const ro = this._ro;
    const flag = (key, label, hint) => `<label class="chk ptoggle"><input type="checkbox" data-control="w-flag" data-flag="${key}" ${w[key] ? "checked" : ""} ${ro}>
      <span><b>${label}</b><br><span class="sub">${hint}</span></span></label>`;
    const conds = m.conditions.filter((c) => c !== "manual_override");
    return `${this._autoBlock(w, true)}
      <div class="stitle small">This window handles</div>
      <div class="chks col">${flag("sleep_protection", "Sleep mode", "Stays closed while sleep is on (see Sleep mode below)")}
        ${flag("privacy", "Evening privacy", "Closes when it gets dark in the evening")}
        ${flag("solar_protection", "Sun protection", "Shades when the sun is in front of the window and the light is strong")}</div>
      ${this._windowPresetsUsedHtml(w)}
      <div class="hint"><b>Waiting for daylight</b> and a <b>manual override</b> never move anything: the covers stay where they are. <b>Useful solar heating</b> keeps the Morning / Daytime preset. A mode without any assignment leaves the covers where they are. Lighting moods are never used for curtains.</div>
      ${this._statusHtml(`w-${w.window_id}`)}
      ${this._rulesHtml(w, (g) => !ENV_GROUPS.has(g))}`;
  }

  // ---------------------------------------------------- automatic rule settings
  _ruleValue(w, scope, key) {
    const r = this._model.rules;
    if (scope === "window") return { value: w.auto.values[key], source: w.auto.sources[key] };
    const room = scope === "room" ? (r.rooms[w.area_id] || {}) : {};
    if (scope === "room" && key in room) return { value: room[key], source: "room" };
    if (key in r.global) return { value: r.global[key], source: "global" };
    return { value: r.defaults[key], source: "default" };
  }

  _rulesHtml(w, only = () => true, title = "Automatic settings") {
    const m = this._model;
    const scope = this._ui.scope && (this._ui.scope !== "room" || w.area_id) ? this._ui.scope : "window";
    const segs = [["window", "This window"], ...(w.area_id ? [["room", `Room ${w.area_name || ""}`]] : []), ["global", "All windows"]];
    const lists = Object.entries(m.entities).map(([k, items]) => `<datalist id="dl-${k}">${items.map((i) => `<option value="${esc(i.entity_id)}">${esc(i.name)}</option>`).join("")}</datalist>`).join("");
    const groups = m.rules.groups.filter(([g]) => only(g) && g !== "schedule").map(([g, label, desc]) => {
      const fields = m.rules.schema.filter((r) => r.group === g);
      let body = fields.map((rule) => this._ruleField(w, rule, scope)).join("");
      if (g === "sleep" && scope === "window") body = `${this._flagSetRow(w, scope, "lamp_bypass", "sleep", "Sleep wins over the window lamp")}${this._flagSetRow(w, scope, "manual_bypass", "sleep", "Sleep ends a manual hold that began before it")}${body}`;
      if (g === "lamp" && scope === "window") {
        body += `<div class="frow"><label>Window Lamp preset</label><div class="fval"><span>${esc(w.effective.lamp.text)}</span><button class="link" data-action="goto-mode" data-mode="lamp">Set in Global Modes → Window Lamp</button></div></div>`;
      }
      if (g === "lamp" && scope !== "window") body = `<div class="hint">The lamp and its preset are chosen per window.</div>${body}`;
      return `<details class="rgroup" ${this._ui.open?.[g] ? "open" : ""} data-group="${g}"><summary>${esc(label)}</summary><div class="hint">${esc(desc)}</div>${body}</details>`;
    }).join("");
    const warn = w.auto.warnings.length ? w.auto.warnings.map((t) => `<div class="issue warn"><ha-icon icon="mdi:alert-outline"></ha-icon><div class="imain">${esc(t)}</div></div>`).join("") : "";
    return `<div class="stitle small">${esc(title)}</div>
      <div class="frow"><label>Settings for</label><div class="fval"><div class="segs" role="group" aria-label="Settings for">${segs.map(([k, l]) => `<button class="seg ${k === scope ? "on" : ""}" aria-pressed="${k === scope}" data-action="scope" data-scope="${k}">${esc(l)}</button>`).join("")}</div></div></div>
      <div class="hint">${scope === "window" ? "Values set here apply to this window only; the rest come from its room or all windows." : scope === "room" ? "Values set here apply to every window in this room that does not set its own." : "Defaults for every window, unless a room or window sets its own."}</div>
      ${warn}${lists}${groups}${this._statusHtml("rules")}`;
  }

  _flagSetRow(w, scope, key, choice, label) {
    const { value } = this._ruleValue(w, scope, key);
    return `<label class="chk rrow"><input type="checkbox" data-control="rule-flagset" data-key="${key}" data-choice="${choice}" ${(value || []).includes(choice) ? "checked" : ""} ${this._ro}><span>${esc(label)}</span></label>`;
  }

  _ruleField(w, rule, scope) {
    if (!rule.scopes.includes(scope)) return "";
    const ro = this._ro;
    const { value, source } = this._ruleValue(w, scope, rule.key);
    const id = `r-${rule.key}`;
    const attrs = `id="${id}" data-control="rule" data-key="${rule.key}" data-kind="${rule.kind}" ${ro}`;
    let input;
    switch (rule.kind) {
      case "bool": input = `<input type="checkbox" ${attrs} ${value ? "checked" : ""}>`; break;
      case "int": case "float":
        input = `<input type="number" class="num" min="${rule.low}" max="${rule.high}" step="${rule.step || (rule.kind === "float" ? 0.5 : 1)}" value="${esc(value ?? "")}" ${attrs}>${rule.unit ? ` ${esc(rule.unit)}` : ""}`; break;
      // 24-hour time as text: a native time field shows AM/PM in some browser locales.
      case "time": input = `<input type="text" class="time" inputmode="numeric" maxlength="8" autocomplete="off" spellcheck="false" pattern="([01][0-9]|2[0-3]):[0-5][0-9]" placeholder="HH:mm" aria-describedby="${id}-fmt" value="${esc(value || "")}" ${attrs}><span class="sub" id="${id}-fmt">24-hour, HH:mm</span>`; break;
      case "choice": input = `<select ${attrs}>${rule.choices.map(([c, l]) => `<option value="${c}" ${c === value ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`; break;
      case "entity": input = `<input type="text" list="dl-${rule.entity_list || ENTITY_LIST[rule.key] || "sleep"}" value="${esc(value || "")}" placeholder="none" ${attrs}>`; break;
      case "days": input = `<div class="segs days">${this._model.rules.weekdays.map((d, i) => `<button class="seg ${(value || []).includes(i) ? "on" : ""}" aria-pressed="${(value || []).includes(i)}" data-action="rule-day" data-key="${rule.key}" data-day="${i}" ${ro}>${d}</button>`).join("")}</div>`; break;
      case "set": input = `<div class="chks">${rule.choices.map(([c, l]) => `<label class="chk"><input type="checkbox" data-control="rule-set" data-key="${rule.key}" data-choice="${c}" ${(value || []).includes(c) ? "checked" : ""} ${ro}>${esc(l)}</label>`).join("")}</div>`; break;
      case "dayplan": input = `<span class="sub">Set on Curtains → Global Schedule → Advanced</span>`; break;
      default: input = esc(value);
    }
    const own = source === scope;
    const origin = own && this._canEdit ? `<button class="link" data-action="rule-reset" data-key="${rule.key}">Use the inherited value</button>`
      : `<span class="sub">${own ? "set here" : `from ${SOURCE_WORD[source] || source}`}</span>`;
    return `<div class="frow"><label for="${id}">${esc(rule.label)}${rule.hint ? `<br><span class="sub">${esc(rule.hint)}</span>` : ""}</label>
      <div class="fval">${input} ${origin}</div></div>`;
  }

  _sunTab(w) {
    const sun = this._model.sun;
    const ro = this._ro;
    const lim = this._model.limits;
    const e = w.exposure;
    const cal = sun.calibration;
    const obs = w.solar.obstructions.map((o, i) => `<div class="obs"><span class="sub">From</span><input type="number" class="num" min="0" max="360" value="${esc(o.from_azimuth)}" data-control="obs" data-index="${i}" data-field="from_azimuth" aria-label="From azimuth" ${ro}>°
        <span class="sub">to</span><input type="number" class="num" min="0" max="360" value="${esc(o.to_azimuth)}" data-control="obs" data-index="${i}" data-field="to_azimuth" aria-label="To azimuth" ${ro}>°
        <span class="sub">needs above</span><input type="number" class="num" min="0" max="90" value="${esc(o.min_elevation)}" data-control="obs" data-index="${i}" data-field="min_elevation" aria-label="Minimum elevation" ${ro}>°
        ${this._canEdit ? `<button class="link" data-action="remove-obstruction" data-index="${i}">Remove</button>` : ""}</div>`).join("");
    return `<label class="chk ptoggle"><input type="checkbox" data-control="w-flag" data-flag="solar_protection" ${w.solar_protection ? "checked" : ""} ${ro}>
        <span><b>Sun protection</b><br><span class="sub">Shades when the sun is in front of this window and direct sunshine is confirmed. Temperature and glare: see Environment.</span></span></label>
      <div class="sunbox ${e.status === "exposed" ? "hot" : ""}"><ha-icon icon="${e.status === "exposed" ? "mdi:white-balance-sunny" : "mdi:weather-sunny-off"}"></ha-icon>
        <div><b>${esc(e.text)}</b><div class="sub">${e.offset != null ? `The sun is ${Math.abs(e.offset)}° ${e.offset >= 0 ? "right" : "left"} of the window's facing direction (seen from inside)` : ""}</div></div></div>
      <div class="kvs">
        <div><span>Window faces</span><b>${w.effective_bearing == null ? "not set" : `${deg(w.effective_bearing)} ${esc(w.compass)}`}</b></div>
        <div><span>Sun azimuth</span><b>${deg(sun.azimuth)}</b></div>
        <div><span>Sun elevation</span><b>${deg(sun.elevation)}</b></div>
        <div><span>Sun angle</span><b>${deg(sun.sun_angle)}</b></div>
        ${this._sunAngleRows(w)}</div>
      <div class="frow"><label for="sol-w">Exposure width</label><div class="fval">± <input id="sol-w" type="number" class="num" min="${lim.exposure_width[0]}" max="${lim.exposure_width[1]}" value="${esc(w.solar.exposure_width)}" data-control="solar" data-key="exposure_width" ${ro}> ° <span class="sub">around the direction the window faces</span></div></div>
      <div class="frow"><label for="sol-e">Minimum elevation</label><div class="fval"><input id="sol-e" type="number" class="num" step="0.5" min="${lim.elevation[0]}" max="${lim.elevation[1]}" value="${esc(w.solar.min_elevation)}" data-control="solar" data-key="min_elevation" ${ro}> ° <span class="sub">lower sun does not reach the window (low winter sun)</span></div></div>
      <div class="frow"><label for="sol-x">Overhang</label><div class="fval"><input id="sol-x" type="number" class="num" step="1" min="${lim.max_elevation[0]}" max="${lim.max_elevation[1]}" value="${esc(w.solar.max_elevation ?? "")}" placeholder="none" data-control="solar" data-key="max_elevation" ${ro}> ° <span class="sub">eaves or a balcony: a sun higher than this (summer) does not reach the glass; empty = none</span></div></div>
      <div class="stitle small">Obstructions</div>
      <div class="hint">A hill, a house or trees: between two directions the sun must be higher to reach this window (e.g. east windows behind a hill).</div>
      ${obs || `<div class="muted">None.</div>`}
      ${this._canEdit && w.solar.obstructions.length < lim.obstructions ? `<button class="link" data-action="add-obstruction">Add obstruction</button>` : ""}
      ${this._statusHtml(`w-${w.window_id}`)}
      <details class="dladv"><summary>Sun angle check</summary><div class="hint">${esc(cal.text)}</div>
        <div class="hint">The sun angle is the house's own reference (0° = the sun faces the south-facing windows). Its relation to the standard azimuth is learned from readings over the day, never assumed.</div>
        ${this._canEdit ? `<button class="link" data-action="reset-calibration">Start the check again</button>` : ""}</details>`;
  }

  _sunAngleRows(w) {
    const env = w.auto && w.auto.environment;
    if (!env) return "";
    const s = env.sun;
    const side = (v) => (v == null ? "—" : `${Math.abs(Math.round(v * 10) / 10)}° ${v >= 0 ? "right" : "left"}`);
    return `<div><span>From the window</span><b>${side(s.offset)}</b></div>
      <div><span>Incidence</span><b>${s.incidence == null ? "—" : `${deg(s.incidence)} <span class="sub">(0° = straight on)</span>`}</b></div>
      ${s.offset_via_sun_angle != null ? `<div><span>Via sun angle</span><b>${side(s.offset_via_sun_angle)}</b></div>` : ""}`;
  }

  // Environment / Solar (0.6.3): the sensors this window uses and whether each can be trusted now, the
  // direct-sun and temperature assessments, and the environment, temperature and glare settings.
  _environmentTab(w) {
    const a = w.auto || {};
    const env = a.environment;
    if (w.mode === "off" || !env) return `<div class="muted">This window is Off: nothing is evaluated.</div>${this._rulesHtml(w, (g) => ENV_GROUPS.has(g), "Environment, temperature and glare")}`;
    const r = env.readings || {};
    const d = a.decision || {};
    const ds = env.direct_sun;
    const th = env.thermal;
    const c = env.comfort;
    return `<div class="stitle small">Measurements now</div>
      ${this._readingRow("Outdoor light", r.outdoor_lux)}
      ${r.indoor_lux ? this._readingRow("Indoor light", r.indoor_lux) : ""}
      ${this._readingRow("Room temperature", r.room_temp)}
      ${this._readingRow("Outdoor temperature", r.outdoor_temp)}
      ${this._readingRow("Weather", r.weather ? { ...r.weather, value: r.weather.value, unit: r.weather.value == null ? "" : "% cloud" } : null)}
      <div class="hint">Only valid readings are used. Missing, stale, stuck or implausible readings never open a window, and a forecast never overrides a working light sensor.</div>
      <div class="kvs">
        <div><span>Sun</span><b>${env.sun.elevation == null ? "unavailable" : `azimuth ${deg(env.sun.azimuth)} · elevation ${deg(env.sun.elevation)}`}</b></div>
        ${this._sunAngleRows(w)}
        <div><span>Geometry</span><b>${esc(env.sun.text)}</b></div>
        <div><span>Direct sun</span><b>${ds ? `<span class="pill ${DIRECT_TONE[ds.status] || "muted"}">${esc(DIRECT_WORD[ds.status] || ds.status)}</span>` : "—"}</b></div>
        <div><span>Room</span><b>${env.band_word ? `<span class="pill ${BAND_TONE[env.band] || "muted"}">${esc(env.band_word)}</span>` : "—"}${c ? ` <span class="sub">comfortable ${num1(c.low)}–${num1(c.high)} °C, max ${num1(c.top)} °C${c.source !== "setting" ? ` (thermostat ${esc(c.source)})` : ""}</span>` : ""}</b></div>
        <div><span>Decision</span><b>${esc(d.condition_word || "—")}</b></div></div>
      ${ds ? `<div class="note">${esc(ds.text)}</div>` : ""}
      ${th && th.state !== "off" ? `<div class="note">${esc(th.text)}</div>` : ""}
      ${w.solar_protection ? "" : `<div class="note warn">Sun protection is off for this window (Sun protection tab): direct sunshine, temperature and glare do not move it.</div>`}
      ${this._rulesHtml(w, (g) => ENV_GROUPS.has(g), "Environment, temperature and glare")}`;
  }

  // The predicted result of a settings change, before it is saved (0.6.3).
  _simHtml(w) {
    const sim = this._ui.sim;
    if (!sim || sim.window_id !== w.window_id) return "";
    const rule = this._model.rules.schema.find((x) => x.key === sim.key);
    const label = rule ? rule.label : sim.key;
    if (!sim.result) return `<div class="confirm ask" role="dialog" aria-label="Predicted result"><div class="muted">${sim.error ? `<span class="bad">${esc(sim.error)}</span>` : "Working out what would happen…"}</div>
      <div class="actions"><button class="btn ghost" data-action="sim-cancel">Close</button></div></div>`;
    const rows = sim.result.windows.map((x) => {
      if (x.mode === "off") return `<div class="prow"><div class="pmain"><b>${esc(x.name)}</b><span class="sub">Off: not evaluated</span></div></div>`;
      const cur = x.current ? `${esc(x.current.condition_word)}${x.current_preset_name ? ` → ${esc(x.current_preset_name)}` : ""}` : "—";
      const next = `${esc(x.predicted.condition_word)}${x.preset_name ? ` → ${esc(x.preset_name)}` : ""}`;
      const what = !x.changes ? "No change now" : x.mode === "automatic" ? (x.moves ? `${x.moves} cover${x.moves === 1 ? "" : "s"} would move at once` : "No cover needs to move") : "Observing: nothing moves";
      return `<div class="prow"><div class="pmain"><b>${esc(x.name)}</b><span class="sub">now ${cur}</span><span>→ ${next}</span></div>
        <span class="pill ${x.changes ? (x.mode === "automatic" && x.moves ? "warn" : "info") : "ok"}">${esc(what)}</span>
        <div class="why">${esc(x.predicted.text)}</div></div>`;
    }).join("");
    return `<div class="confirm ask" role="dialog" aria-label="Predicted result">
      <div class="chead">Before you switch on <b>${esc(label)}</b>: what would happen now</div>${rows || `<div class="muted">No window is affected.</div>`}
      <div class="hint">Predicted from the current measurements; nothing is saved or moved yet.</div>
      ${sim.error ? `<div class="bad" role="alert">${esc(sim.error)}</div>` : ""}
      <div class="actions"><button class="btn primary" data-action="sim-apply" ${sim.sending ? "disabled" : ""}>Switch on</button>
        <button class="btn ghost" data-action="sim-cancel" ${sim.sending ? "disabled" : ""}>Cancel</button></div></div>`;
  }

  _diagTab(w) {
    const words = this._model.words;
    const sel = this._desired(w);
    const tol = this._model.settings.tolerance;
    const rows = w.layers.map((l) => {
      const desired = sel ? sel.positions[l.layer_id] : null;
      const diff = desired != null && l.position != null ? l.position - desired : null;
      return `<tr><td><b>${esc(l.name)}</b><div class="sub"><code>${esc(l.entity_id)}</code></div></td><td>${l.missing ? "not found" : esc(l.state)}</td>
        <td>${pct(l.position)}</td><td>${desired == null ? "—" : pct(desired)}</td>
        <td class="${diff != null && Math.abs(diff) > tol ? "warn" : ""}">${diff == null ? "—" : `${diff > 0 ? "+" : ""}${diff}`}</td>
        <td class="sub">${["position", "open", "close", "stop"].filter((f) => l.caps[f]).join(", ") || "none"}</td></tr>`;
    }).join("");
    const op = w.last;
    const opHtml = op ? `<div class="sub">${esc(op.preset_name)} · started ${fmtTime(op.started)}${op.finished ? ` · finished ${fmtTime(op.finished)}` : ""}</div>
      ${op.items.map((i) => `<div class="prow"><div class="pmain"><b>${esc(i.name)}</b><span class="sub">${i.target == null ? "unchanged" : `${pct(i.start_position)} → ${pct(i.target)} · reported ${pct(i.final_position)}`}${i.service ? ` · cover.${esc(i.service)}` : ""}</span></div>
        <span class="pill ${RESULT_TONE[i.status] || "muted"}">${esc(words.results[i.status] || i.status)}</span><div class="why">${esc(i.detail || "")}</div></div>`).join("")}` : `<div class="muted">No preset applied yet.</div>`;
    const conflicts = w.layers.flatMap((l) => l.conflicts.map((c) => ({ ...c, cover: l.name })));
    const allowed = new Set(w.allowed_automations || []);
    return `<div class="stitle small">Actual and desired${sel ? ` (${esc(sel.name)})` : ""}</div>
      <div class="tscroll"><table class="fct"><thead><tr><th>Cover</th><th>State</th><th>Actual</th><th>Desired</th><th>Diff</th><th>Supports</th></tr></thead><tbody>${rows}</tbody></table></div>
      ${!sel ? `<div class="hint">Desired = the automatic preset, or the selected preset when there is none.</div>` : ""}
      ${this._autoDiag(w)}
      <div class="kvs"><div><span>Recognised</span><b>${this._currentText(w)}</b></div><div><span>Manual change</span><b>${this._manualHtml(w)}</b></div></div>
      ${w.manual && this._canEdit ? `<button class="link" data-action="clear-manual">Forget the manual change</button>` : ""}
      <div class="stitle small">Last command</div>${op ? `<div>${this._lastHtml(w)}</div>` : ""}${opHtml}
      ${w.history.length > 1 ? `<details class="dladv"><summary>Earlier commands (${w.history.length - 1})</summary>${w.history.slice(1).map((h) => `<div class="prow"><div class="pmain"><b>${esc(h.preset_name)}</b><span class="sub">${fmtTime(h.started)}</span></div>
        <span class="pill ${OVERALL_TONE[h.status] || "muted"}">${esc(words.overall[h.status] || h.status)}</span><div class="why">${esc(h.explanation || "")}</div></div>`).join("")}</details>` : ""}
      <div class="stitle small">Other automations for these covers</div>
      ${conflicts.length ? conflicts.map((c) => `<div class="issue ${c.enabled && !allowed.has(c.automation) ? "warn" : ""}"><ha-icon icon="${c.enabled ? "mdi:alert-outline" : "mdi:sleep"}"></ha-icon>
        <div class="imain"><b>${esc(c.name || c.automation)}</b> <span class="sub">${c.enabled ? "enabled" : "disabled"} · ${esc(c.cover)} · ${esc(c.detail)}${allowed.has(c.automation) ? " · allowed with Automatic" : ""}</span>
        ${this._canEdit ? `<div><button class="link" data-action="allow-auto" data-automation="${esc(c.automation)}" data-allowed="${allowed.has(c.automation) ? "0" : "1"}">${allowed.has(c.automation) ? "Undo: it blocks Automatic again" : "It does not compete: allow it with Automatic"}</button></div>` : ""}</div></div>`).join("")
        : `<div class="muted">None found among ${this._model.automations} automation${this._model.automations === 1 ? "" : "s"} that move covers.</div>`}
      <div class="hint">Automations are only listed, never changed, disabled or deleted. While an enabled one is listed (and not allowed), this window cannot be switched to Automatic, and an Automatic window sends nothing. Templated targets cannot be detected.</div>
      ${this._canEdit ? `<button class="link" data-action="rescan">Scan again</button>` : ""}
      <div class="stitle small">Checks</div>
      ${w.problems.length ? w.problems.map((p) => `<div class="issue ${p.severity === "warning" ? "warn" : ""}"><ha-icon icon="${p.severity === "warning" ? "mdi:alert-outline" : "mdi:information-outline"}"></ha-icon><div class="imain">${esc(p.text)}</div></div>`).join("") : `<div class="muted">No problems.</div>`}`;
  }

  _autoDiag(w) {
    const a = w.auto;
    const d = a.decision;
    const log = a.log || [];
    return `<div class="stitle small">Automatic decisions</div>
      <div class="kvs"><div><span>Condition</span><b>${d ? esc(d.condition_word) : "—"}${d && d.base !== d.condition ? ` <span class="sub">(automatic: ${esc(d.base_word)})</span>` : ""}</b></div>
        <div><span>Moves today</span><b>${a.moves_today} of ${a.values.max_moves}</b></div>
        <div><span>Last move</span><b>${fmtTime(a.last_move_at)}</b></div>
        <div><span>Next check</span><b>${a.next_evaluation ? fmtTime(a.next_evaluation) : d ? fmtTime(d.next_check) : "—"}</b></div>
        <div><span>Morning since</span><b>${fmtTime(a.morning_since)}</b></div>
        <div><span>Manual hold</span><b>${a.hold ? `${esc(a.hold.policy_word)} · ${fmtTime(a.hold.created)}` : "None"}</b></div></div>
      ${d && d.evidence.length ? `<div class="sub">${d.evidence.map(esc).join(" · ")}</div>` : ""}
      <details class="dladv"><summary>Order of precedence</summary><ol class="prec">${a.precedence.map((p) => `<li>${esc(p.word)}</li>`).join("")}</ol></details>
      ${d && d.direct_sun && d.direct_sun.status && d.direct_sun.status !== "off" ? `<div class="sub"><b>Direct sun:</b> ${esc(d.direct_sun.text)}</div>` : ""}
      ${d && d.thermal && d.thermal.state !== "off" ? `<div class="sub"><b>Temperature:</b> ${esc(d.thermal.text)}</div>` : ""}
      <details class="dladv" open><summary>Decision log (${log.length})</summary>${log.length ? log.map((x) => `<div class="prow"><div class="pmain"><time class="ltime" datetime="${esc(x.at)}" title="${esc(fullTime(x.at))}">${fmtTime(x.at)}</time><span class="pill ${STATUS_TONE[x.action] || "muted"}">${esc(x.word)}</span>${x.mode && x.mode !== "automatic" ? `<span class="sub">${esc(x.mode)}</span>` : ""}</div>
        <div class="why">${fmtTime(x.at)} — ${esc(x.summary || x.text)}${x.summary && x.summary !== x.text ? `<br><span class="sub">${esc(x.text)}</span>` : ""}</div></div>`).join("") : `<div class="muted">Nothing yet.</div>`}</details>`;
  }

  // ------------------------------------------------------- Global Modes (0.6.4)
  _pageName() { return this._page || (PAGES.includes(this._config.page) ? this._config.page : "overview"); }

  _pageTabs() {
    const on = this._pageName();
    const tab = (k, label, icon) => `<button class="seg ${on === k ? "on" : ""}" aria-pressed="${on === k}" data-action="page" data-page="${k}"><ha-icon icon="${icon}"></ha-icon>${label}</button>`;
    return `<div class="pagetabs"><div class="segs" role="group" aria-label="Curtains">${tab("overview", "Overview", "mdi:view-dashboard-outline")}${tab("modes", "Global Modes", "mdi:view-grid-outline")}${tab("schedule", "Global Schedule", "mdi:calendar-clock")}${tab("windows", "Windows", "mdi:window-closed-variant")}${tab("diagnostics", "Diagnostics", "mdi:stethoscope")}</div></div>`;
  }

  _gm() {
    if (!this._ui.gm) this._ui.gm = { mode: "morning", view: "modes", ops: [], draft: null, error: null, add: null, cell: null, advCols: false };
    return this._ui.gm;
  }

  _gmModel() { return this._gm().draft || this._model.global_modes; }

  async _gmEdit(op) {
    const gm = this._gm();
    const ops = [...gm.ops, ...(Array.isArray(op) ? op : [op])];
    gm.busy = true;
    this._render();
    try {
      const res = await this._ws({ type: `${WS}/modes/change`, ops, dry_run: true });
      gm.ops = ops;
      gm.draft = res.global_modes;
      gm.pending = res;
      gm.error = null;
      gm.add = null;
      gm.cell = null;
    } catch (err) {
      gm.error = { code: err?.code, message: err?.message || String(err), op };
    }
    gm.busy = false;
    this._render();
  }

  _gmAdd(replace) {
    const gm = this._gm();
    if (replace) {
      if (gm.error && gm.error.op && !Array.isArray(gm.error.op)) this._gmEdit({ ...gm.error.op, replace: true });
      return;
    }
    const add = gm.add;
    if (!add || !add.target || !add.preset) return;
    const [window_id, layer_id] = add.target.split("|");
    this._gmEdit({ op: "assign", mode: gm.mode, window_id, layer_id: layer_id || null, preset_id: add.preset });
  }

  // A change made outside the Global Modes draft (Window settings): saved at once unless it would move an
  // Automatic window now (or it is a migration), which is reviewed and confirmed first.
  async _quickChange(ops, key, forceReview = false) {
    return this._quickMsg({ type: `${WS}/modes/change`, ops }, key, forceReview);
  }

  // A reviewed change (Global Modes ops, or schedule settings, 0.6.5): saved at once unless it would move an
  // Automatic window now (or a review is asked for), which is shown and confirmed first.
  async _quickMsg(base, key, forceReview = false) {
    this._status[key] = { busy: true };
    this._requestRender();
    try {
      const res = await this._ws({ ...base, dry_run: true });
      delete this._status[key];
      if (res.needs_confirm || forceReview) {
        this._ui.review = { base, result: res, key, policy: null };
      } else {
        await this._ws(base);
        this._status[key] = { ok: "Saved" };
        setTimeout(() => { if (this._status[key]?.ok) { delete this._status[key]; this._requestRender(); } }, 2000);
      }
    } catch (err) {
      this._status[key] = { error: err?.message || String(err) };
    }
    this._scheduleFetch();
    this._requestRender();
  }

  async _openReview(ops, key) {
    if (!ops || !ops.length) return;
    await this._openReviewMsg({ type: `${WS}/modes/change`, ops }, key);
  }

  async _openReviewMsg(base, key) {
    this._ui.review = { base, key, result: null, policy: null };
    this._render();
    try {
      const res = await this._ws({ ...base, dry_run: true });
      if (this._ui.review) this._ui.review.result = res;
    } catch (err) {
      if (this._ui.review) this._ui.review.error = err?.message || String(err);
    }
    this._render();
  }

  async _saveReview() {
    const r = this._ui.review;
    if (!r || !r.result || r.sending) return;
    if (r.result.needs_confirm && !r.policy) return;
    r.sending = true;
    this._render();
    try {
      await this._ws({ ...r.base, confirm: !!r.result.needs_confirm, policy: r.policy || "now" });
      this._ui.review = null;
      if (r.key === "global") Object.assign(this._gm(), { ops: [], draft: null, pending: null, error: null, cell: null });
      else if (r.key === "schedule") Object.assign(this._sch(), { values: {}, error: null });
      else { this._status[r.key] = { ok: "Saved" }; setTimeout(() => { delete this._status[r.key]; this._requestRender(); }, 2000); }
    } catch (err) {
      r.sending = false;
      r.error = err?.message || String(err);
    }
    this._scheduleFetch();
    this._render();
  }

  // The review before a mapping change is saved: what changes, what each affected window would do now.
  _reviewHtml() {
    const r = this._ui.review;
    if (!r) return "";
    if (!r.result) return `<div class="confirm ask" role="dialog" aria-label="Review changes"><div class="muted">${r.error ? `<span class="bad">${esc(r.error)}</span>` : "Working out what the change would do…"}</div>
      <div class="actions"><button class="btn ghost" data-action="review-cancel">Close</button></div></div>`;
    const res = r.result;
    const status = (e) => {
      if (e.mode === "off") return `<span class="pill muted">Off: not evaluated</span>`;
      if (e.hold) return `<span class="pill warn">Manual hold: kept</span>`;
      if (!e.active) return `<span class="pill muted">Not active now: no movement</span>`;
      if (e.mode === "observing") return `<span class="pill info">Observing: nothing moves</span>`;
      if (e.conflicts.length) return `<span class="pill warn">Blocked by automations</span>`;
      return e.moves ? `<span class="pill warn">Would move ${e.moves} cover${e.moves === 1 ? "" : "s"}</span>` : `<span class="pill ok">Already there</span>`;
    };
    const rows = res.impact.map((e) => `<div class="prow"><div class="pmain"><b>${esc(e.name)}</b><span class="sub">${esc(e.room)} · ${esc(e.mode[0].toUpperCase() + e.mode.slice(1))}</span></div>${status(e)}
        <div class="why">${e.changes.map((c) => `${esc(c.word)}: ${esc(c.before)} → <b>${esc(c.after)}</b>`).join("<br>")}</div>
        ${e.condition ? `<div class="why">Now: <b>${esc(e.condition_word)}</b>${e.before ? ` (${esc(e.before)})` : ""}${e.active && e.predicted ? ` → <b>${esc(e.predicted_word)}</b>: ${esc(e.after || "keeps the position")}` : ""}</div>` : ""}
        ${e.blocked && e.status ? `<div class="why">Currently prevented: ${esc(e.status)}</div>` : ""}
        ${e.conflicts.length ? `<div class="why warn">Enabled automations may also move these covers: ${e.conflicts.map(esc).join(", ")}. Nothing is sent while they are enabled.</div>` : ""}
        ${e.active && e.items.length ? `<div class="why">${e.items.filter((i) => i.decision !== "unchanged").map((i) => `${esc(i.name)}: ${pct(i.current)} → ${pct(i.target)} (${esc(i.word)})`).join(" · ")}</div>` : ""}</div>`).join("");
    const needs = res.needs_confirm;
    const pol = (k, label, hint) => `<label class="chk ptoggle"><input type="radio" name="gm-policy" value="${k}" data-control="gm-policy" ${r.policy === k ? "checked" : ""}><span><b>${label}</b><br><span class="sub">${hint}</span></span></label>`;
    return `<div class="confirm ask" role="dialog" aria-label="Review changes">
      <div class="chead">Review ${res.changes.length} change${res.changes.length === 1 ? "" : "s"}</div>
      <ul class="clines">${res.changes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>
      ${res.transitions ? `<div class="stitle small">Next transitions</div><div class="trcmp">
        <div><div class="sub">Now</div>${this._transList(res.transitions.before)}</div><div><div class="sub">Proposed</div>${this._transList(res.transitions.after)}</div></div>` : ""}
      <div class="stitle small">${res.impact.length ? `Affected windows (${res.impact.length})` : "No window's effective preset changes"}</div>${rows}
      ${needs ? `<div class="stitle small">Automatic windows whose current position would change</div>
        <div class="chks col" role="radiogroup" aria-label="When to apply">${pol("now", "Apply now", "The automatic controller moves them now, after all its normal safety checks (manual holds, movement limits, competing automations).")}
          ${pol("next_change", "At the next change of condition", "They keep their position now; the new preset is used from their next condition (e.g. when Morning becomes Daytime).")}</div>` : `<div class="note">Saving changes the configuration only; nothing moves because of it.</div>`}
      ${r.error ? `<div class="bad" role="alert">${esc(r.error)}</div>` : ""}
      <div class="actions"><button class="btn primary" data-action="review-save" ${r.sending || (needs && !r.policy) ? "disabled" : ""}>${needs ? "Save and confirm" : "Save"}</button>
        <button class="btn ghost" data-action="review-cancel" ${r.sending ? "disabled" : ""}>Cancel</button></div></div>`;
  }

  _modesPage() {
    const gm = this._gm();
    const mm = this._gmModel();
    const total = mm.modes.reduce((n, m) => n + m.count, 0);
    let body = `<div class="header"><ha-icon icon="mdi:view-grid-outline"></ha-icon><div class="titles">
        <div class="title">${esc(this._config.title || "Curtains")} · Global Modes</div>
        <div class="sub">${total} assignment${total === 1 ? "" : "s"} · ${this._model.windows.length} window${this._model.windows.length === 1 ? "" : "s"}</div></div></div>
      ${this._pageTabs()}<div class="pad">${this._reviewHtml()}`;
    body += `<div class="note">Which preset each window or cover uses when the automatic controller selects a mode for it. Each window still decides its own mode; a window without an assignment keeps its position. Selecting and editing never move anything.</div>`;
    body += `<div class="segs" role="group" aria-label="View">${[["modes", "Modes"], ["matrix", "All windows"]].map(([k, l]) => `<button class="seg ${gm.view === k ? "on" : ""}" aria-pressed="${gm.view === k}" data-action="gm-view" data-view="${k}">${l}</button>`).join("")}</div>`;
    body += this._migrationHtml();
    if (gm.ops.length) {
      body += `<div class="draftbar" role="status"><span><b>${gm.ops.length} unsaved change${gm.ops.length === 1 ? "" : "s"}</b> <span class="sub">${gm.pending && gm.pending.needs_confirm ? "· would move an Automatic window: reviewed before saving" : "· nothing moves until you save"}</span></span>
        <div class="actions"><button class="btn small" data-action="gm-review" ${gm.busy ? "disabled" : ""}>Review & save</button><button class="btn small ghost" data-action="gm-discard">Discard</button></div></div>`;
    }
    const inEditor = gm.view === "modes" && gm.error && gm.error.op && !Array.isArray(gm.error.op) && gm.error.op.op === "assign";
    if (gm.error && !inEditor) {
      body += `<div class="issue bad" role="alert"><ha-icon icon="mdi:close-octagon-outline"></ha-icon><div class="imain">${esc(gm.error.message)}
        <div class="actions">${gm.error.code === "overlap" ? `<button class="btn small" data-action="gm-replace">Replace the existing assignment</button>` : ""}<button class="btn small ghost" data-action="gm-error-close">Close</button></div></div></div>`;
    }
    body += gm.view === "matrix" ? this._matrixHtml(mm) : this._modesHtml(mm);
    body += this._gmLogHtml(mm);
    return `${body}</div>`;
  }

  _modeCard(m) {
    const on = m.key === this._gm().mode;
    return `<button class="mcard ${on ? "on" : ""}" aria-pressed="${on}" data-action="gm-mode" data-mode="${m.key}" title="${esc(m.hint)}"><ha-icon icon="${m.icon}"></ha-icon>
      <span class="mname">${esc(m.word)}</span><span class="mcount">${m.count ? `${m.count} assigned` : "none"}${m.others.length ? ` · ${m.others.length} override${m.others.length === 1 ? "" : "s"}` : ""}</span></button>`;
  }

  _modesHtml(mm) {
    const gm = this._gm();
    const common = mm.modes.filter((m) => !m.advanced);
    const adv = mm.modes.filter((m) => m.advanced);
    const mode = mm.modes.find((m) => m.key === gm.mode) || common[0];
    const advOpen = mode.advanced || this._ui.open?.["gm-adv"];
    return `<div class="mgrid" role="group" aria-label="Modes">${common.map((m) => this._modeCard(m)).join("")}</div>
      <details class="dladv" data-group="gm-adv" ${advOpen ? "open" : ""}><summary>Advanced: ${adv.map((m) => esc(m.word)).join(", ")}</summary>
        <div class="mgrid">${adv.map((m) => this._modeCard(m)).join("")}</div>
        <div class="hint">These need their own preset only where they should differ: Glare and Excessive Heat use a cover's Sun Protection preset when nothing is assigned; Useful solar heating keeps the Morning / Daytime preset. Manual holds and Waiting for daylight never move anything.</div></details>
      ${this._modeEditor(mode)}`;
  }

  _modeEditor(m) {
    const gm = this._gm();
    const can = this._canEdit;
    const ro = can ? "" : "disabled";
    const rows = m.rows.map((r) => {
      const st = r.status;
      const pills = [`<span class="pill ${MODE_STATUS_TONE[st.mode]}">${esc(st.mode_word)}</span>`];
      if (r.active) pills.push(`<span class="pill ok" title="The controller has selected ${esc(m.word)} for this window now">Active now</span>`);
      else if (st.condition_word && st.mode !== "off") pills.push(`<span class="pill muted" title="What the controller has selected for this window now">Now: ${esc(st.condition_word)}</span>`);
      if (st.hold) pills.push(`<span class="pill warn">Manual hold</span>`);
      if (st.deferred) pills.push(`<span class="pill warn">Waits for next condition</span>`);
      const hidden = r.hidden.map((h) => `<div class="why warn"><ha-icon icon="mdi:layers-outline"></ha-icon> ${esc(h.word)}${h.cover ? ` (${esc(h.cover)})` : ""}: <b>${esc(h.label)}</b> hides this setting.
        <span class="sub">(from an earlier version; the migration resolves it)</span></div>`).join("");
      const id = `gm-${esc(r.window_id)}-${esc(r.layer_id || "w")}`;
      return `<div class="arow ${r.missing || r.problems.length ? "bad" : ""}">
        <div class="aname"><b>${esc(r.name)}</b><span class="sub">${esc(r.room)} · ${esc(r.type_word)}</span><span class="apills">${pills.join("")}</span></div>
        <label class="sr" for="${id}">Preset for ${esc(r.name)}</label>
        <select id="${id}" data-control="gm-preset" data-window="${esc(r.window_id)}" data-layer="${esc(r.layer_id || "")}" ${ro}>
          ${r.missing ? `<option value="" selected>Missing preset — choose one</option>` : ""}
          ${r.options.map((o) => `<option value="${esc(o.preset_id)}" ${o.preset_id === r.preset_id ? "selected" : ""} title="${esc(o.detail)}">${esc(o.name)}</option>`).join("")}</select>
        ${can ? `<button class="iconbtn" data-action="gm-remove" data-window="${esc(r.window_id)}" data-layer="${esc(r.layer_id || "")}" aria-label="Remove ${esc(r.name)} from ${esc(m.word)}" title="Remove from ${esc(m.word)}"><ha-icon icon="mdi:close"></ha-icon></button>` : ""}
        <div class="why">${esc(r.effective)}${r.options.find((o) => o.preset_id === r.preset_id) ? ` · ${esc(r.options.find((o) => o.preset_id === r.preset_id).detail)}` : ""}</div>
        ${hidden}${r.problems.map((p) => `<div class="why warn">${esc(p)}</div>`).join("")}
        ${r.conflicts.length ? `<div class="why warn">Competing automations: ${r.conflicts.map(esc).join(", ")}</div>` : ""}</div>`;
    }).join("");
    const assigned = new Set(m.rows.map((r) => `${r.window_id}|${r.layer_id || ""}`));
    const targets = this._gmModel().targets.filter((t) => t.has_covers);
    const groups = [...new Set(targets.map((t) => t.room))];
    const add = gm.add || {};
    const chosen = add.target ? targets.find((t) => `${t.window_id}|${t.layer_id || ""}` === add.target) : null;
    const err = gm.view === "modes" && gm.error && gm.error.op && !Array.isArray(gm.error.op) && gm.error.op.op === "assign"
      ? `<div class="issue bad" role="alert"><ha-icon icon="mdi:close-octagon-outline"></ha-icon><div class="imain">${esc(gm.error.message)}
          <div class="actions">${gm.error.code === "overlap" ? `<button class="btn small" data-action="gm-replace">Replace the existing assignment</button>` : ""}<button class="btn small ghost" data-action="gm-error-close">Cancel</button></div></div></div>` : "";
    const others = m.others.length ? `<div class="stitle small">Still set outside Global Modes (from an earlier version, ${m.others.length})</div>
      ${m.others.map((o) => `<div class="prow"><div class="pmain"><b>${esc(o.name)}</b><span class="sub">${esc(o.room)}</span></div><span class="pill warn">${esc(o.word)}: ${esc(o.label)}</span></div>`).join("")}
      <div class="hint">Active until the migration moves them into Global Modes (Review migration above).</div>` : "";
    return `<section class="meditor" aria-label="${esc(m.word)} assignments">
      <div class="mhead"><ha-icon icon="${m.icon}"></ha-icon><div><b>${esc(m.word)}</b><div class="sub">${esc(m.hint)}</div></div></div>
      ${m.warnings.slice(0, 3).map((t) => `<div class="issue warn"><ha-icon icon="mdi:alert-outline"></ha-icon><div class="imain">${esc(t)}</div></div>`).join("")}
      ${m.warnings.length > 3 ? `<details class="dladv"><summary>${m.warnings.length - 3} more warning${m.warnings.length - 3 === 1 ? "" : "s"}</summary>${m.warnings.slice(3).map((t) => `<div class="issue warn"><div class="imain">${esc(t)}</div></div>`).join("")}</details>` : ""}
      ${rows ? `<div class="arows">${rows}</div>` : `<div class="muted">No window or cover is assigned to ${esc(m.word)}: they keep their position when the controller selects ${esc(m.word)}.</div>`}
      ${can ? `<div class="addrow" role="group" aria-label="Add a window or cover to ${esc(m.word)}">
        <select data-control="gm-add-target" aria-label="Window or cover"><option value="">Add Window / Cover…</option>
          ${groups.map((g) => `<optgroup label="${esc(g)}">${targets.filter((t) => t.room === g).map((t) => {
            const key = `${t.window_id}|${t.layer_id || ""}`;
            return `<option value="${esc(key)}" ${assigned.has(key) ? "disabled" : ""} ${add.target === key ? "selected" : ""}>${esc(t.name)} · ${esc(t.type_word)}${assigned.has(key) ? " (assigned)" : ""}</option>`;
          }).join("")}</optgroup>`).join("")}</select>
        <select data-control="gm-add-preset" aria-label="Preset" ${chosen ? "" : "disabled"}><option value="">Preset…</option>
          ${chosen ? chosen.options.map((o) => `<option value="${esc(o.preset_id)}" ${add.preset === o.preset_id ? "selected" : ""}>${esc(o.name)} (${esc(o.detail)})</option>`).join("") : ""}</select>
        <button class="btn small" data-action="gm-add" ${chosen && add.preset && !gm.busy ? "" : "disabled"}><ha-icon icon="mdi:plus"></ha-icon>Add</button></div>` : ""}
      ${err}${others}</section>`;
  }

  _matrixHtml(mm) {
    const gm = this._gm();
    const modes = mm.modes.filter((m) => gm.advCols || !m.advanced);
    const cell = (row, m) => {
      if (!row.cells) return `<td></td>`;
      const c = row.cells[m.key];
      const on = gm.cell && gm.cell.mode === m.key && gm.cell.kind === row.kind && (gm.cell.window_id || null) === (row.window_id || null)
        && (gm.cell.layer_id || null) === (row.layer_id || null) && (gm.cell.area_id || null) === (row.area_id || null);
      const tag = c.source ? `<span class="srctag ${SRC_TONE[c.source] || "muted"}" title="${esc(SRC_WORD[c.source] || c.source)}">${SRC_TAG[c.source] || "?"}</span>` : "";
      const tone = c.state === "nothing" || c.state === "inherit" ? "muted" : c.state === "no_action" ? "na" : c.state === "missing" ? "bad" : "";
      return `<td><button class="mxcell ${tone} ${on ? "on" : ""} ${row.kind === "cover" && c.inherited ? "inh" : ""}" data-action="gm-cell" data-kind="${row.kind}" data-window="${esc(row.window_id || "")}" data-layer="${esc(row.layer_id || "")}" data-area="${esc(row.area_id || "")}" data-mode="${m.key}"
        title="${esc(c.text || `${c.label}${c.source_word ? ` — ${c.source_word}` : ""}`)}" ${this._canEdit && row.kind !== "room" ? "" : "disabled"}>${esc(c.label)}${tag}</button></td>`;
    };
    const rows = mm.matrix.map((row) => {
      if (row.kind === "room") return `<tr class="mxroom"><th scope="row">${esc(row.name)}${row.area_id ? `<span class="sub"> · room</span>` : ""}</th>${modes.map((m) => cell(row, m)).join("")}</tr>`;
      if (row.kind === "window") return `<tr><th scope="row"><span class="mxname">${esc(row.name)}</span> <span class="pill ${MODE_STATUS_TONE[row.mode]}">${esc(row.mode[0].toUpperCase() + row.mode.slice(1))}</span></th>${modes.map((m) => cell(row, m)).join("")}</tr>`;
      return `<tr class="mxcover"><th scope="row"><span class="sub">↳</span> ${esc(row.name)}</th>${modes.map((m) => cell(row, m)).join("")}</tr>`;
    }).join("");
    return `${this._cellEditor(mm)}
      <div class="tscroll mxwrap"><table class="mx"><thead><tr><th scope="col">Window / Cover</th>${modes.map((m) => `<th scope="col"><ha-icon icon="${m.icon}"></ha-icon><br>${esc(m.word)}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>
      <div class="actions"><button class="link" data-action="gm-adv-cols">${gm.advCols ? "Hide" : "Show"} Window Lamp, Glare and Excessive Heat</button></div>
      <div class="hint"><span class="srctag info">G</span> Global Modes${this._model.centralized ? "" : ` · <span class="srctag warn">R</span> <span class="srctag warn">W</span> <span class="srctag warn">C</span> old room / window / cover overrides (until the migration)`}. “—” = no assignment (keeps its position); cover rows show the window's target unless the cover is assigned on its own.</div>`;
  }

  _cellEditor(mm) {
    const c = this._gm().cell;
    if (!c) return "";
    const row = mm.matrix.find((r) => r.kind === c.kind && (r.window_id || null) === (c.window_id || null) && (r.layer_id || null) === (c.layer_id || null)
      && (r.area_id || null) === (c.area_id || null));
    const mode = mm.modes.find((m) => m.key === c.mode);
    if (!row || !mode) return "";
    const cell = row.cells[c.mode];
    let opts;
    if (row.kind === "room") {
      opts = `<option value="r:" ${!cell.value ? "selected" : ""}>Inherit (no room override)</option><option value="r:no_action" ${cell.value === "no_action" ? "selected" : ""}>No action in this room</option>
        ${row.options.map((o) => `<option value="r:${esc(o.preset_id)}" ${cell.value === o.preset_id ? "selected" : ""}>${esc(o.name)}</option>`).join("")}`;
    } else {
      const lvl = row.kind === "cover" ? "cover" : "window";
      opts = `<optgroup label="Global ${esc(mode.word)}"><option value="g:" ${!cell.global ? "selected" : ""}>Not assigned</option>
          ${row.options.map((o) => `<option value="g:${esc(o.preset_id)}" ${cell.global === o.preset_id ? "selected" : ""}>${esc(o.name)} (${esc(o.detail)})</option>`).join("")}</optgroup>
        `;
      void lvl;
    }
    const name = row.kind === "cover" ? `${row.window_name} — ${row.name}` : row.name;
    return `<div class="cellbox" role="group" aria-label="${esc(name)}, ${esc(mode.word)}"><div><b>${esc(name)}</b> · ${esc(mode.word)}</div>
      <div class="sub">Now: ${esc(cell.text || `${cell.label}${cell.source_word ? ` — ${cell.source_word}` : ""}`)}</div>
      ${cell.override ? `<div class="sub warn">An old ${row.kind} override (from an earlier version) still decides here until the migration.</div>` : ""}
      <div class="fval"><select data-control="gm-cell-value" aria-label="Setting">${opts}</select><button class="btn small ghost" data-action="gm-cell-close">Close</button></div></div>`;
  }

  _gmCellValue(value) {
    const c = this._gm().cell;
    if (!c) return;
    const [kind, v] = [value.slice(0, 1), value.slice(2)];
    if (kind === "r") { this._gmEdit({ op: "set", level: "room", mode: c.mode, area_id: c.area_id, value: v || null }); return; }
    if (kind === "g") {
      if (v) this._gmEdit({ op: "assign", mode: c.mode, window_id: c.window_id, layer_id: c.layer_id, preset_id: v });
      else this._gmEdit({ op: "unassign", mode: c.mode, window_id: c.window_id, layer_id: c.layer_id });
      return;
    }
    this._gmEdit({ op: "set", level: c.kind === "cover" ? "cover" : "window", mode: c.mode, window_id: c.window_id, layer_id: c.layer_id, value: v || null });
  }

  _gmLogHtml(mm) {
    const log = (this._model.global_modes && this._model.global_modes.log) || [];
    return `<details class="dladv" data-group="gm-log" ${this._ui.open?.["gm-log"] ? "open" : ""}><summary>Recent automatic decisions (${log.length})</summary>
      ${log.length ? log.map((x) => `<div class="logline"><time class="ltime" datetime="${esc(x.at)}" title="${esc(fullTime(x.at))}">${fmtTime(x.at)}</time> — ${esc(x.summary)}</div>`).join("") : `<div class="muted">Nothing yet.</div>`}</details>`;
  }

  // Covers tab: the presets of one cover of a layered window (used when it is assigned on its own).
  _coverPresetsHtml(w, l) {
    const ro = this._ro;
    return `<details class="dladv" data-group="cp-${esc(l.layer_id)}" ${this._ui.open?.[`cp-${l.layer_id}`] ? "open" : ""}><summary>Presets of this cover (${l.presets.length})</summary>
      <div class="hint">Used when this cover is assigned on its own in Global Modes; that never moves the other covers of the window. Changing a position here changes every assignment that uses the preset.</div>
      ${l.presets.map((p) => `<div class="cprow"><input type="text" maxlength="60" value="${esc(p.name)}" aria-label="Preset name" data-control="cp-name" data-layer="${esc(l.layer_id)}" data-preset="${esc(p.preset_id)}" ${ro}>
        <input type="number" class="num" min="0" max="100" step="1" value="${esc(p.position)}" aria-label="${esc(p.name)} position in %" data-control="cp-pos" data-layer="${esc(l.layer_id)}" data-preset="${esc(p.preset_id)}" ${ro}> %
        ${this._canEdit ? `<button class="link" data-action="cp-del" data-layer="${esc(l.layer_id)}" data-preset="${esc(p.preset_id)}">Delete</button>` : ""}</div>`).join("")}
      ${this._canEdit && l.presets.length < 12 ? `<button class="btn small ghost" data-action="cp-add" data-layer="${esc(l.layer_id)}"><ha-icon icon="mdi:plus"></ha-icon>Add preset</button>` : ""}</details>`;
  }

  // ---------------------------------------------------------- 0.6.6 pages
  _modeWord(key) {
    const mm = this._model.global_modes;
    const m = mm && mm.modes.find((x) => x.key === key);
    return m ? m.word : key;
  }

  // Window settings: which preset this window gets in each mode — read only, managed in Global Modes.
  _windowPresetsUsedHtml(w) {
    const mm = this._model.global_modes;
    const row = (m) => `<div class="prow"><div class="pmain"><ha-icon icon="${m.icon}"></ha-icon><b>${esc(m.word)}</b></div>
      <button class="link" data-action="goto-mode" data-mode="${m.key}">Edit</button><div class="why">${esc(w.effective[m.key].text)}</div></div>`;
    return `<div class="stitle small">Presets per mode <span class="sub">— managed in Global Modes</span></div>
      <div class="hint">When each mode happens is set on <button class="link" data-action="page" data-page="schedule">Global Schedule</button>; which of this window's presets it uses is set on <button class="link" data-action="page" data-page="modes">Global Modes</button>. This window's presets are edited in its Presets and Covers tabs.</div>
      ${mm.modes.filter((m) => !m.advanced).map(row).join("")}
      <details class="dladv" data-group="pu-adv" ${this._ui.open?.["pu-adv"] ? "open" : ""}><summary>Window Lamp, Glare, Excessive Heat</summary>${mm.modes.filter((m) => m.advanced).map(row).join("")}</details>`;
  }

  _migrationHtml() {
    const mig = this._model.migration;
    if (!mig || !mig.pending) return "";
    return `<div class="issue warn" role="status"><ha-icon icon="mdi:swap-horizontal-bold"></ha-icon><div class="imain"><b>Settings from an earlier version are still active</b><br>${esc(mig.text)}. Until then every window behaves exactly as before.
      ${this._canEdit ? `<div class="actions"><button class="btn small" data-action="mig-open">Review migration</button>${mig.required ? `<span class="sub">${mig.required} need${mig.required === 1 ? "s" : ""} your choice</span>` : ""}</div>` : ""}</div></div>${this._migrationDialog()}`;
  }

  async _migReview() {
    const g = this._ui.mig;
    if (!g) return;
    g.busy = true;
    this._render();
    try {
      g.result = await this._ws({ type: `${WS}/migration/change`, choices: g.choices, dry_run: true });
      g.error = null;
    } catch (err) {
      g.error = err?.message || String(err);
    }
    g.busy = false;
    this._render();
  }

  async _migApply() {
    const g = this._ui.mig;
    if (!g || !g.result || g.busy) return;
    if (g.result.missing.length || (g.result.needs_confirm && !g.policy)) return;
    g.busy = true;
    this._render();
    try {
      await this._ws({ type: `${WS}/migration/change`, choices: g.choices, confirm: !!g.result.needs_confirm, policy: g.policy || "now" });
      this._ui.mig = null;
    } catch (err) {
      g.busy = false;
      g.error = err?.message || String(err);
    }
    this._scheduleFetch();
    this._render();
  }

  _migrationDialog() {
    const g = this._ui.mig;
    if (!g) return "";
    if (!g.result) return `<div class="confirm ask" role="dialog" aria-label="Migration"><div class="muted">${g.error ? `<span class="bad">${esc(g.error)}</span>` : "Working out the migration…"}</div>
      <div class="actions"><button class="btn ghost" data-action="mig-close">Close</button></div></div>`;
    const r = g.result;
    const words = r.words || {};
    const groups = [["conflict", "Hidden Global Modes assignments"], ["inexact", "Cannot be kept exactly"], ["schedule", "Room and window schedules"],
      ["morning", "Windows without a Morning preset"], ["mapping", "Moved to Global Modes unchanged"]];
    const item = (x) => `<div class="prow"><div class="pmain"><b>${esc(x.title)}</b>${x.required ? `<span class="pill warn">choice needed</span>` : ""}</div>
      <div class="why">${esc(x.detail)}</div>
      ${x.choices.length ? `<div class="why chks col" role="radiogroup" aria-label="${esc(x.title)}">${x.choices.map((c) => `<label class="chk"><input type="radio" name="mig-${esc(x.id)}" value="${c}" data-control="mig-choice" data-id="${esc(x.id)}" ${(g.choices[x.id] || x.default) === c ? "checked" : ""}>${esc(words[c] || c)}</label>`).join("")}</div>` : ""}</div>`;
    const sections = groups.map(([k, title]) => {
      const list = r.items.filter((x) => x.kind === k);
      if (!list.length) return "";
      const body = list.map(item).join("");
      return k === "mapping" ? `<details class="dladv"><summary>${esc(title)} (${list.length})</summary>${body}</details>` : `<div class="stitle small">${esc(title)} (${list.length})</div>${body}`;
    }).join("");
    const impact = r.impact.map((e) => `<div class="prow"><div class="pmain"><b>${esc(e.name)}</b><span class="sub">${esc(e.room)} · ${esc(e.mode)}</span></div>
      <span class="pill ${e.moves ? "warn" : "muted"}">${e.moves ? `Would move ${e.moves}` : "No movement"}</span><div class="why">${e.changes.map((c) => `${esc(c.before)} → <b>${esc(c.after)}</b>`).join("<br>")}</div></div>`).join("");
    const pol = (k, label, hint) => `<label class="chk ptoggle"><input type="radio" name="mig-policy" value="${k}" data-control="mig-policy" ${g.policy === k ? "checked" : ""}><span><b>${label}</b><br><span class="sub">${hint}</span></span></label>`;
    const blocked = r.missing.length || (r.needs_confirm && !g.policy) || g.busy;
    return `<div class="confirm ask" role="dialog" aria-label="Migration">
      <div class="chead">Move everything to Global Schedule and Global Modes</div>
      <div class="sub">Each old window, cover or room setting becomes a Global Modes assignment with exactly the same positions (a cover preset is added where one is needed). Afterwards the old settings are removed and only Global Schedule and Global Modes configure the routine.</div>
      ${sections}
      ${r.missing.length ? `<div class="note warn">${r.missing.length} item${r.missing.length === 1 ? " needs" : "s need"} a choice before the migration can be applied.</div>` : ""}
      ${r.changes.length ? `<details class="dladv"><summary>What will be saved (${r.changes.length})</summary><ul class="clines">${r.changes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></details>` : ""}
      ${!r.missing.length ? (impact ? `<div class="stitle small">Windows that would act differently now</div>${impact}` : `<div class="note">No window changes what it does now.</div>`) : ""}
      ${r.needs_confirm ? `<div class="chks col" role="radiogroup" aria-label="When to apply">${pol("now", "Apply now", "Through every normal safety check.")}${pol("next_change", "At the next change of condition", "They keep their position until their condition changes.")}</div>` : ""}
      ${g.error ? `<div class="bad" role="alert">${esc(g.error)}</div>` : ""}
      <div class="actions"><button class="btn primary" data-action="mig-apply" ${blocked ? "disabled" : ""}>Apply migration</button><button class="btn ghost" data-action="mig-close" ${g.busy ? "disabled" : ""}>Cancel</button></div></div>`;
  }

  // Windows: configure the physical windows (adding, the presets and protections are in each window's settings).
  _windowsPage() {
    const m = this._model;
    const ui = this._ui;
    const windows = this._windows();
    const props = this._config.area ? m.proposals.filter((p) => p.area_id === this._config.area) : m.proposals;
    let body = `<div class="header"><ha-icon icon="mdi:window-closed-variant"></ha-icon><div class="titles"><div class="title">${esc(this._config.title || "Curtains")} · Windows</div>
        <div class="sub">Windows, their covers, presets and protections</div></div>
        ${this._canEdit ? `<button class="iconbtn" data-action="settings" aria-label="Curtain settings" title="Curtain settings" aria-pressed="${!!ui.settings}"><ha-icon icon="mdi:cog-outline"></ha-icon></button>` : ""}</div>
      ${this._pageTabs()}<div class="pad">${this._reviewHtml()}
      <div class="hint">A window's settings define its covers, its adjustable presets (also for single covers), its direction and sun protection, sleep protection, the window lamp, manual-move behaviour and its mode (Off / Observing / Automatic). When things happen and which preset is used are set on Global Schedule and Global Modes.</div>`;
    if (this._canEdit) {
      body += `<div class="actions"><button class="btn" data-action="add-window" ${ui.adding ? "disabled" : ""}><ha-icon icon="mdi:plus"></ha-icon>Add window</button>
        ${props.length ? `<button class="btn ghost" data-action="suggestions" aria-expanded="${!!ui.suggestions}">Suggestions (${props.length})</button>` : ""}</div>`;
    }
    if (ui.settings) body += this._settingsHtml();
    if (ui.suggestions && !ui.adding) body += this._proposalsHtml(props);
    if (ui.adding) body += this._addFormHtml();
    if (!windows.length && !ui.adding) body += `<div class="muted">No windows yet.</div>`;
    body += `<div class="wlist">${windows.map((w) => `<button class="wrow" data-action="open-window" data-id="${esc(w.window_id)}">
        <span class="wmain"><b>${esc(w.name)}</b><span class="sub">${esc(w.area_name || "No room")} · ${esc(w.compass || "no direction")} · ${w.layers.length} cover${w.layers.length === 1 ? "" : "s"} · ${w.presets.length} preset${w.presets.length === 1 ? "" : "s"}</span></span>
        <span class="pill ${MODE_TONE[w.mode]}">${esc(w.mode_word)}</span>${w.problems.some((p) => p.severity === "warning") ? `<ha-icon icon="mdi:alert-outline" class="warn"></ha-icon>` : ""}<ha-icon icon="mdi:chevron-right"></ha-icon></button>`).join("")}</div>`;
    return `${body}</div>`;
  }

  // Diagnostics: why windows do what they do, overrides in force, failures and conflicts, for the whole house.
  _diagPage() {
    const m = this._model;
    const windows = this._windows();
    const log = (m.global_modes && m.global_modes.log) || [];
    const failures = windows.filter((w) => w.last && ["failed", "partial", "timeout"].includes(w.last.status));
    const holds = windows.filter((w) => w.auto && w.auto.hold);
    const blocked = windows.filter((w) => w.auto && w.auto.status && w.auto.status.action === "blocked");
    const conflicts = windows.flatMap((w) => w.layers.flatMap((l) => l.conflicts.filter((c) => c.enabled && !(w.allowed_automations || []).includes(c.automation)).map((c) => ({ w, c }))));
    const problems = windows.flatMap((w) => w.problems.filter((p) => p.severity === "warning").map((p) => ({ w, p })));
    const line = (w, text, tone = "muted") => `<div class="prow"><div class="pmain"><button class="link" data-action="open-window" data-id="${esc(w.window_id)}">${esc(w.name)}</button></div><span class="pill ${tone}">${esc(w.mode_word)}</span><div class="why">${esc(text)}</div></div>`;
    return `<div class="header"><ha-icon icon="mdi:stethoscope"></ha-icon><div class="titles"><div class="title">${esc(this._config.title || "Curtains")} · Diagnostics</div>
        <div class="sub">${esc(m.migration ? m.migration.text : "")}</div></div></div>
      ${this._pageTabs()}<div class="pad">${this._migrationHtml()}
      <div class="stitle small">Now</div>${windows.map((w) => {
        const d = w.auto && w.auto.decision;
        return line(w, w.mode === "off" ? "Off: not evaluated" : d ? `${d.condition_word}${d.preset_name ? ` → ${d.preset_name} (${d.source_word})` : ""}: ${d.text}` : "Evaluating…", MODE_TONE[w.mode]);
      }).join("") || `<div class="muted">No windows.</div>`}
      <div class="stitle small">Manual holds (${holds.length})</div>${holds.map((w) => line(w, `${w.auto.hold.policy_word} · since ${fmtTime(w.auto.hold.created)}`, "warn")).join("") || `<div class="muted">None.</div>`}
      <div class="stitle small">Prevented now (${blocked.length})</div>${blocked.map((w) => line(w, w.auto.status.text, "warn")).join("") || `<div class="muted">None.</div>`}
      <div class="stitle small">Failed movements (${failures.length})</div>${failures.map((w) => line(w, w.last.explanation, "bad")).join("") || `<div class="muted">None.</div>`}
      <div class="stitle small">Competing automations (${conflicts.length})</div>${conflicts.map(({ w, c }) => line(w, `${c.name || c.automation}: ${c.detail}`, "warn")).join("") || `<div class="muted">None.</div>`}
      <div class="stitle small">Problems (${problems.length})</div>${problems.map(({ w, p }) => line(w, p.text, "warn")).join("") || `<div class="muted">None.</div>`}
      <details class="dladv" open><summary>Recent automatic decisions (${log.length})</summary>
        ${log.map((x) => `<div class="logline"><time class="ltime" datetime="${esc(x.at)}" title="${esc(fullTime(x.at))}">${fmtTime(x.at)}</time> — <b>${esc(x.window)}</b>: ${esc(x.summary)}</div>`).join("") || `<div class="muted">Nothing yet.</div>`}</details>
      <div class="hint">Each window's own Diagnostics tab has its full decision log, evidence, precedence and the competing automations you can allow.</div></div>`;
  }

  // ---------------------------------------------------- Global Schedule (0.6.5)
  _sch() {
    if (!this._ui.sch) this._ui.sch = { open: null, values: {}, error: null };
    return this._ui.sch;
  }

  _schVal(key) {
    const d = this._sch().values;
    return key in d ? d[key] : this._model.schedule.values[key];
  }

  _rule(key) { return this._model.rules.schema.find((r) => r.key === key); }

  _schSet(key, value) {
    const s = this._sch();
    s.error = null;
    if (JSON.stringify(value) === JSON.stringify(this._model.schedule.values[key])) delete s.values[key];
    else s.values[key] = value;
    this._render();
  }

  _transList(list) {
    if (!list || !list.length) return `<div class="muted">None scheduled</div>`;
    return list.slice(0, 5).map((t) => `<div class="trow"><ha-icon icon="${SCH_ICON[t.phase]}"></ha-icon><time datetime="${esc(t.at)}">${t.approx ? "≈ " : ""}${fmtTime(t.at)}</time> <b>${esc(t.word)}</b><span class="sub">${esc(t.reason)}</span></div>`).join("");
  }

  _schTime(key, label) {
    const id = `sch-${key}`;
    const draft = key in this._sch().values;
    return `<div class="frow"><label for="${id}">${esc(label)}</label><div class="fval"><input id="${id}" type="text" class="time ${draft ? "draft" : ""}" inputmode="numeric" maxlength="8" autocomplete="off" spellcheck="false"
      pattern="([01][0-9]|2[0-3]):[0-5][0-9]" placeholder="HH:mm" aria-describedby="${id}-fmt" value="${esc(this._schVal(key) || "")}" data-control="sch-time" data-key="${key}" ${this._ro}>
      <span class="sub" id="${id}-fmt">24-hour, HH:mm</span>${this._schSource(key)}</div></div>`;
  }

  _schSource(key) {
    const src = this._model.schedule.sources[key];
    return key in this._sch().values ? `<span class="pill warn">unsaved</span>` : src === "default" ? `<span class="sub">default</span>` : "";
  }

  _schChoice(key, explain = {}) {
    const rule = this._rule(key);
    const v = this._schVal(key);
    return `<div class="chks col" role="radiogroup" aria-label="${esc(rule.label)}">${rule.choices.map(([c, l]) => `<label class="chk ptoggle"><input type="radio" name="sch-${key}" value="${c}" data-control="sch-choice" data-key="${key}" ${v === c ? "checked" : ""} ${this._ro}>
      <span><b>${esc(l)}</b>${explain[c] ? `<br><span class="sub">${esc(explain[c])}</span>` : ""}</span></label>`).join("")}</div>`;
  }

  _schBool(key, label, hint) {
    return `<label class="chk ptoggle"><input type="checkbox" data-control="sch-bool" data-key="${key}" ${this._schVal(key) ? "checked" : ""} ${this._ro}><span><b>${esc(label)}</b>${hint ? `<br><span class="sub">${esc(hint)}</span>` : ""}</span></label>`;
  }

  _schDays(key, label) {
    const v = this._schVal(key) || [];
    return `<div class="frow"><label>${esc(label)}</label><div class="fval"><div class="segs days" role="group" aria-label="${esc(label)}">${this._model.rules.weekdays.map((d, i) => `<button class="seg ${v.includes(i) ? "on" : ""}" aria-pressed="${v.includes(i)}" data-action="sch-day" data-key="${key}" data-day="${i}" ${this._ro}>${d}</button>`).join("")}</div>${this._schSource(key)}</div></div>`;
  }

  _schSummary(phase) {
    const v = (k) => this._schVal(k);
    const freeDiff = (a, b) => (v(a) !== v(b) ? ` · free days ${v(b)}` : "");
    if (phase === "morning") return v("morning_enabled") ? { rule: `From ${v("night_end_workday")}${freeDiff("night_end_workday", "night_end_free")} · needs confirmed daylight, nobody asleep`, time: v("night_end_workday") } : { rule: "Off: Daytime starts directly when there is daylight", time: "—" };
    if (phase === "day") {
      const m = v("morning_mode");
      if (m === "duration") return { rule: `${v("morning_minutes")} min after Morning begins`, time: `+${v("morning_minutes")} min` };
      return { rule: `At ${v("day_from_workday")}${freeDiff("day_from_workday", "day_from_free")}${m === "not_before" ? `, not before Morning has lasted ${v("morning_minutes")} min` : ""}`, time: v("day_from_workday") };
    }
    if (phase === "evening") {
      const r = v("evening_rule");
      const ev = v("evening_event") === "sunset" ? "sunset" : "dusk";
      const t = `${v("evening_time_workday")}${freeDiff("evening_time_workday", "evening_time_free")}`;
      const rule = { legacy: "When it is dark after 12:00 (0.6.4 behaviour, not kept)", time: `At ${t}`, event: `At ${ev}`, first: `${t} or ${ev}, whichever comes first`, last: `${t} or ${ev}, whichever comes last` }[r];
      return { rule, time: r === "legacy" || r === "event" ? ev : v("evening_time_workday") };
    }
    if (!v("night_enabled")) return { rule: "Off", time: "—" };
    const late = v("late_evenings") === "custom" ? (v("late_days") || []).map((d) => this._model.rules.weekdays[d]).join(", ") || "none" : "evenings before a free day";
    return { rule: `${v("night_start_workday")} · late evenings (${late}) ${v("night_start_free")}`, time: v("night_start_workday") };
  }

  _schEditor(phase) {
    const sleepHint = "Sleep settings apply to windows with sleep protection on.";
    if (phase === "morning") return `${this._schBool("morning_enabled", "Morning phase", "Off: the day starts directly with Daytime once there is daylight")}
      ${this._schTime("night_end_workday", "Earliest Morning (normal days)")}${this._schTime("night_end_free", "Earliest Morning (free days)")}
      <div class="note">Morning makes the Morning preset <b>eligible</b> from this time. It is applied only once confirmed daylight is measured and nobody sleeps in a sleep-protected room; in the dark, with a missing sensor or while asleep the covers keep their position. Night ends at this time too.</div>
      <div class="stitle small">After sleep</div><div class="hint">${sleepHint}</div>${this._schChoice("sleep_end")}
      ${this._schBool("sleep_require_daylight", "Opening after sleep needs daylight", "Off: when sleep ends after the Morning time, Morning starts at once, also in the dark")}`;
    if (phase === "day") return `${this._schChoice("morning_mode", SCH_EXPLAIN)}
      <div class="frow"><label for="sch-mm">Morning lasts</label><div class="fval"><input id="sch-mm" type="number" class="num" min="0" max="720" value="${esc(this._schVal("morning_minutes"))}" data-control="sch-num" data-key="morning_minutes" ${this._ro}> min${this._schSource("morning_minutes")}</div></div>
      ${this._schTime("day_from_workday", "Daytime at (normal days)")}${this._schTime("day_from_free", "Daytime at (free days)")}
      <div class="hint">A window whose Morning could not start (dark, asleep) is not opened by Daytime either: opening always needs daylight. A finished Morning never starts again that day. Sun Protection, Glare and Excessive Heat still win over Daytime.</div>`;
    if (phase === "evening") return `${this._schChoice("evening_rule", SCH_EXPLAIN)}
      <div class="stitle small">Solar event</div>${this._schChoice("evening_event", SCH_EXPLAIN)}
      ${this._schTime("evening_time_workday", "Evening at (normal days)")}${this._schTime("evening_time_free", "Evening at (free days)")}
      <div class="hint">Evening applies the Evening preset to windows with evening privacy on. Once started it lasts until Night: a passing cloud does not end it. Dusk needs the outdoor light below the dark level for its stable period (or the sun below the dark elevation without a sensor). When neither is available, a time rule still starts Evening at its time; otherwise the reason it waits is shown.</div>`;
    return `${this._schBool("night_enabled", "Night", "Closes for the household night; works without any light sensor")}
      ${this._schTime("night_start_workday", "Night from (normal evenings)")}${this._schTime("night_start_free", "Night from (late evenings)")}
      <div class="frow"><label>Late evenings</label><div class="fval">${this._schChoice("late_evenings")}</div></div>
      ${this._schVal("late_evenings") === "custom" ? this._schDays("late_days", "Late evenings on") : ""}
      <div class="note">Night belongs to the <b>evening</b> it starts on: Friday 23:55 is Friday evening. A time after midnight (e.g. 00:30) still belongs to the evening before. Night ends at the next day's Morning time (that day's own profile).</div>`;
  }

  _timelineHtml(h) {
    const segs = h.timeline || [];
    const p = timeParts(new Date());
    const nowMin = Number(p.hour) * 60 + Number(p.minute);
    return `<div class="tl" role="img" aria-label="Today: ${segs.map((s) => `${s.word} ${String(Math.floor(s.from / 60)).padStart(2, "0")}:${String(s.from % 60).padStart(2, "0")}`).join(", ")}">
      ${segs.map((s) => `<i class="tlseg ph-${s.phase}" style="left:${(s.from / 14.4).toFixed(2)}%;width:${((s.to - s.from) / 14.4).toFixed(2)}%" title="${esc(s.word)}"></i>`).join("")}
      <i class="tlnow" style="left:${(nowMin / 14.4).toFixed(2)}%"></i></div>
      <div class="tlaxis" aria-hidden="true"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
      <div class="tllegend">${["night", "morning", "day", "evening"].map((k) => `<span><i class="ph-${k}"></i>${esc({ night: "Night", morning: "Morning", day: "Daytime", evening: "Evening" }[k])}</span>`).join("")}<span class="sub">≈ Morning and Evening may wait for daylight or dusk</span></div>`;
  }

  _schedulePage() {
    const m = this._model.schedule;
    const sch = this._sch();
    const h = m.house;
    const next = h.next[0];
    const draft = Object.keys(sch.values).length;
    let body = `<div class="header"><ha-icon icon="mdi:calendar-clock"></ha-icon><div class="titles">
        <div class="title">${esc(this._config.title || "Curtains")} · Global Schedule</div>
        <div class="sub">${esc(m.implementation)} · ${esc(m.time_zone || "")}</div></div></div>
      ${this._pageTabs()}<div class="pad">${this._reviewHtml()}${this._migrationHtml()}`;
    body += `<div class="schstatus"><ha-icon icon="${SCH_ICON[h.phase]}"></ha-icon><div class="amain">
        <div><span class="sub">Household phase now</span><br><b class="big">${esc(h.word)}</b>${h.since ? ` <span class="sub">since ${fmtTime(h.since)}</span>` : ""}</div>
        <div class="sub">${esc(h.reason)}</div>
        ${h.waiting ? `<div class="warn">Waiting: ${esc(h.waiting)}</div>` : ""}
        ${next ? `<div>Next: <b>${esc(next.word)}</b> ${next.approx ? "≈ " : ""}<time datetime="${esc(next.at)}">${fmtTime(next.at)}</time> <span class="sub">— ${esc(next.reason)}</span></div>` : ""}
        <div class="chips"><span class="chip small"><ha-icon icon="mdi:brightness-6"></ha-icon>${m.lux_entity ? `Opening needs daylight from ${esc(m.lux_entity)}` : "No outdoor light sensor"}</span>
          <span class="chip small"><ha-icon icon="mdi:sleep"></ha-icon>Sleep keeps sleep-protected rooms closed</span></div></div></div>
      <div class="hint">A phase is a reference for every window, not a command: each window still decides on its own (sleep, daylight, sun, lamp, manual holds) and uses its own presets from Global Modes; a window without an assignment keeps its position.</div>`;
    body += m.warnings.map((t) => `<div class="issue warn"><ha-icon icon="mdi:alert-outline"></ha-icon><div class="imain">${esc(t)}</div></div>`).join("");
    body += this._timelineHtml(h);
    if (draft) {
      body += `<div class="draftbar" role="status"><span><b>${draft} unsaved change${draft === 1 ? "" : "s"}</b> <span class="sub">· nothing changes until you review and save</span></span>
        <div class="actions"><button class="btn small" data-action="sch-review">Review & save</button><button class="btn small ghost" data-action="sch-discard">Discard</button></div></div>`;
    }
    if (sch.error) body += `<div class="issue bad" role="alert"><ha-icon icon="mdi:close-octagon-outline"></ha-icon><div class="imain">${esc(sch.error)}</div></div>`;
    body += `<div class="schrows">${["morning", "day", "evening", "night"].map((ph) => {
      const sum = this._schSummary(ph);
      const open = sch.open === ph;
      return `<div class="schrow ${open ? "open" : ""} ${h.phase === ph ? "now" : ""}"><button class="schhead" data-action="sch-open" data-phase="${ph}" aria-expanded="${open}">
          <ha-icon icon="${SCH_ICON[ph]}"></ha-icon><span class="schname">${esc({ morning: "Morning", day: "Daytime", evening: "Evening", night: "Night" }[ph])}${h.phase === ph ? ` <span class="pill ok">now</span>` : ""}</span>
          <span class="schrule">${esc(sum.rule)}</span><span class="schtime">${esc(sum.time)}</span><ha-icon icon="mdi:chevron-${open ? "up" : "down"}"></ha-icon></button>
        ${open ? `<div class="schbody">${this._schEditor(ph)}</div>` : ""}</div>`;
    }).join("")}</div>`;
    body += `<div class="section"><div class="stitle small">Days</div>${this._schDays("free_days", "Free days")}
      <div class="hint">A free day uses the free-day Morning, Daytime and Evening times. Its night before follows the late evening schedule only when “Late evenings” is “Evenings before a free day”.</div></div>`;
    body += `<details class="dladv" data-group="sch-adv" ${this._ui.open?.["sch-adv"] ? "open" : ""}><summary>Advanced: per-day times, room and window schedules</summary>${this._schAdvanced()}</details>`;
    body += `<details class="dladv" data-group="sch-log" ${this._ui.open?.["sch-log"] ? "open" : ""}><summary>Upcoming transitions and recent phases</summary>
      <div class="stitle small">Upcoming</div>${this._transList(h.next)}
      <div class="stitle small">Recent</div>${(h.log || []).length ? h.log.map((x) => `<div class="logline"><time class="ltime" datetime="${esc(x.at)}" title="${esc(fullTime(x.at))}">${fmtTime(x.at)}</time> — ${esc(x.text)}</div>`).join("") : `<div class="muted">Nothing yet.</div>`}</details>`;
    return `${body}</div>`;
  }

  _schAdvanced() {
    const m = this._model.schedule;
    const plan = this._schVal("day_overrides") || {};
    const days = this._model.rules.weekdays;
    const free = this._schVal("free_days") || [];
    const ph = [["morning", "Morning"], ["day", "Daytime"], ["evening", "Evening"], ["night", "Night (that evening)"]];
    const prof = (i, key) => {
      const isFree = free.includes(i);
      if (key === "morning") return this._schVal(isFree ? "night_end_free" : "night_end_workday");
      if (key === "day") return this._schVal(isFree ? "day_from_free" : "day_from_workday");
      if (key === "evening") return this._schVal(isFree ? "evening_time_free" : "evening_time_workday");
      const late = this._schVal("late_evenings") === "custom" ? (this._schVal("late_days") || []).includes(i) : free.includes((i + 1) % 7);
      return this._schVal(late ? "night_start_free" : "night_start_workday");
    };
    const table = `<div class="tscroll"><table class="fct schplan"><thead><tr><th>Day</th>${ph.map(([, l]) => `<th>${esc(l)}</th>`).join("")}</tr></thead><tbody>
      ${days.map((d, i) => `<tr><th scope="row">${esc(d)}<span class="sub"> ${free.includes(i) ? "free" : "normal"}</span></th>${ph.map(([k, l]) => `<td><input type="text" class="time" inputmode="numeric" maxlength="8" placeholder="${esc(prof(i, k))}" aria-label="${esc(d)} ${esc(l)} (blank = ${esc(prof(i, k))})"
        value="${esc((plan[String(i)] || {})[k] || "")}" data-control="sch-dayplan" data-day="${i}" data-phase="${k}" ${this._ro}></td>`).join("")}</tr>`).join("")}</tbody></table></div>
      <div class="hint">Blank = the normal / free-day time shown in grey. “Night” on Friday is the Night that starts on Friday evening (00:30 there = Saturday 00:30). “Morning” on Saturday is Saturday's earliest Morning and ends Friday's night. All times 24-hour HH:mm.</div>`;
    const ov = m.overrides.filter((o) => o.keys.some((k) => !["sleep_end", "sleep_require_daylight"].includes(k.key)));
    const old = ov.length ? `<div class="stitle small">Old room and window schedules (until the migration)</div>${ov.map((o) => `<div class="prow"><div class="pmain"><b>${esc(o.name)}</b><span class="sub">${o.scope === "room" ? "Room" : "Window"}</span></div>
        <div class="why">${o.keys.map((k) => `${esc(k.label)}: <b>${esc(k.value)}</b>`).join(" · ")}</div></div>`).join("")}` : "";
    return `<div class="stitle small">Per-day times</div>${table}
      <div class="hint">This page is the only schedule: every room and window follows it. Windows still differ through their presets in Global Modes and their own protections (sleep, lamp, sun, manual moves).</div>${old}`;
  }

  // ------------------------------------------------------------------ events
  _update(w, fields) {
    return this._save(`w-${w.window_id}`, { type: `${WS}/window/update`, window_id: w.window_id, ...fields });
  }

  _layersPayload(w, change) {
    return w.layers.map((l) => ({ layer_id: l.layer_id, entity_id: l.entity_id, kind: l.kind, label: l.label, ...(change[l.layer_id] || {}) }));
  }

  async _openEnable(w) {
    this._ui.enable = { window_id: w.window_id, review: null };
    this._render();
    try {
      const review = await this._ws({ type: `${WS}/auto/preview`, window_id: w.window_id });
      if (this._ui.enable?.window_id === w.window_id) this._ui.enable.review = review;
    } catch (err) {
      if (this._ui.enable) this._ui.enable.error = err?.message || String(err);
    }
    this._render();
  }

  async _enableConfirmed() {
    const e = this._ui.enable;
    if (!e || !e.review || e.sending) return;
    e.sending = true;
    this._render();
    try {
      await this._ws({ type: `${WS}/auto/enable`, window_id: e.window_id, confirm: true });
      this._ui.enable = null;
    } catch (err) {
      e.sending = false;
      e.error = err?.message || String(err);
    }
    this._scheduleFetch();
    this._render();
  }

  _ruleMsg(w, type, key, value) {
    const scope = this._ui.scope && (this._ui.scope !== "room" || w.area_id) ? this._ui.scope : "window";
    const msg = { type, scope, values: { [key]: value } };
    if (scope === "window") msg.window_id = w.window_id;
    if (scope === "room") msg.area_id = w.area_id;
    return msg;
  }

  _saveRule(w, key, value) {
    const rule = this._model.rules.schema.find((x) => x.key === key);
    // 0.6.5: schedule settings (also room / window ones) are previewed like the Global Schedule page.
    if (rule && (rule.group === "schedule" || ["sleep_end", "sleep_require_daylight"].includes(key))) {
      return this._quickMsg(this._ruleMsg(w, `${WS}/schedule/change`, key, value), "rules");
    }
    return this._save("rules", this._ruleMsg(w, `${WS}/auto/settings`, key, value));
  }

  async _openSim(w, key, value) {
    const msg = this._ruleMsg(w, `${WS}/auto/simulate`, key, value);
    this._ui.sim = { window_id: w.window_id, key, value, msg, result: null };
    this._render();
    try {
      const result = await this._ws(msg);
      if (this._ui.sim && this._ui.sim.msg === msg) this._ui.sim.result = result;
    } catch (err) {
      if (this._ui.sim) this._ui.sim.error = err?.message || String(err);
    }
    this._render();
  }

  async _simApply() {
    const sim = this._ui.sim;
    if (!sim || !sim.result || sim.sending) return;
    sim.sending = true;
    this._render();
    const ok = await this._save("rules", { ...sim.msg, type: `${WS}/auto/settings` });
    if (ok) this._ui.sim = null;
    else { sim.sending = false; sim.error = this._status.rules?.error; }
    this._render();
  }

  async _openPlan(windowId, presetId, mode) {
    this._ui.confirm = { window_id: windowId, preset_id: presetId, mode, plan: null };
    this._render();
    try {
      const plan = await this._ws({ type: `${WS}/preview`, window_id: windowId, preset_id: presetId });
      if (this._ui.confirm?.window_id === windowId) this._ui.confirm.plan = plan;
    } catch (err) {
      if (this._ui.confirm) this._ui.confirm.error = err?.message || String(err);
    }
    this._render();
  }

  async _applyConfirmed() {
    const c = this._ui.confirm;
    if (!c || !c.plan || c.sending) return;
    c.sending = true;
    this._render();
    try {
      await this._ws({ type: `${WS}/apply`, window_id: c.window_id, preset_id: c.preset_id, confirm: true, expected: c.plan.positions });
      this._ui.confirm = null;
      this._status[`apply-${c.window_id}`] = { ok: "Commands sent" };
      setTimeout(() => { delete this._status[`apply-${c.window_id}`]; this._requestRender(); }, 3000);
    } catch (err) {
      c.sending = false;
      c.error = err?.message || String(err);
    }
    this._scheduleFetch();
    this._render();
  }

  _onInput(ev) {
    const el = ev.target;
    const ctl = el.dataset?.control;
    const ed = this._ui.preset;
    if (ctl === "nw-name" && this._ui.adding) {
      this._ui.adding.name = el.value;
      const btn = this.shadowRoot.querySelector('[data-action="create-window"]');
      if (btn) btn.disabled = !el.value.trim();
    } else if (ctl === "pe-name" && ed) {
      ed.name = el.value;
      const btn = this.shadowRoot.querySelector('[data-action="save-preset"]');
      if (btn) btn.disabled = !el.value.trim();
    } else if ((ctl === "pe-range" || ctl === "pe-num") && ed) {
      const v = Math.round(Number(el.value));
      if (el.value === "" || Number.isNaN(v)) return;
      ed.positions[el.dataset.layer] = Math.max(0, Math.min(100, v));
      const other = this.shadowRoot.querySelector(`[data-control="${ctl === "pe-range" ? "pe-num" : "pe-range"}"][data-layer="${CSS.escape(el.dataset.layer)}"]`);
      if (other) other.value = String(ed.positions[el.dataset.layer]);
    }
  }

  _onChange(ev) {
    const el = ev.target;
    const ctl = el.dataset?.control;
    if (!ctl) return;
    const w = this._view ? this._win(this._view) : null;
    const f = this._ui.adding;
    const ed = this._ui.preset;
    switch (ctl) {
      case "nw-area": f.area_id = el.value || null; this._render(); return;
      case "nw-dir": f.direction = el.value || null; return;
      case "nw-cover": {
        const cand = this._model.candidates.find((c) => c.entity_id === el.dataset.entity);
        if (el.checked) f.layers.push({ entity_id: el.dataset.entity, kind: cand ? cand.kind : "custom" });
        else f.layers = f.layers.filter((l) => l.entity_id !== el.dataset.entity);
        if (el.checked && !f.area_id && cand?.area_id) f.area_id = cand.area_id;
        this._render();
        return;
      }
      case "nw-kind": { const l = f.layers.find((x) => x.entity_id === el.dataset.entity); if (l) l.kind = el.value; return; }
      case "sel-preset": {
        const id = el.dataset.window;
        this._save(`sel-${id}`, { type: `${WS}/preset/select`, window_id: id, preset_id: el.value || null }, "Selected");
        return;
      }
      case "set-num": {
        const v = Number(el.value);
        if (el.value === "" || Number.isNaN(v)) return;
        this._save("settings", { type: `${WS}/settings`, [el.dataset.key]: Math.round(v) });
        return;
      }
      case "set-ent": this._save("settings", { type: `${WS}/settings`, [el.dataset.key]: el.value.trim() || null }); return;
      case "pe-leave": {
        if (!ed) return;
        const l = this._win(ed.window_id)?.layers.find((x) => x.layer_id === el.dataset.layer);
        ed.positions[el.dataset.layer] = el.checked ? null : (l?.position ?? 50);
        this._render();
        return;
      }
      case "pe-range": case "pe-num": this._render(); return;
      case "add-entity": this._ui.addEntity = el.value || null; this._render(); return;
      case "gm-preset": this._gmEdit({ op: "assign", mode: this._gm().mode, window_id: el.dataset.window, layer_id: el.dataset.layer || null, preset_id: el.value }); return;
      case "gm-add-target": this._gm().add = { target: el.value || null, preset: null }; this._render(); return;
      case "gm-add-preset": this._gm().add = { ...(this._gm().add || {}), preset: el.value || null }; this._render(); return;
      case "gm-cell-value": this._gmCellValue(el.value); return;
      case "gm-policy": if (this._ui.review) { this._ui.review.policy = el.value; this._render(); } return;
      case "mig-choice": if (this._ui.mig) { this._ui.mig.choices[el.dataset.id] = el.value; this._migReview(); } return;
      case "mig-policy": if (this._ui.mig) { this._ui.mig.policy = el.value; this._render(); } return;
      case "sch-time": {
        const v = parseHHMM(el.value);
        if (v === null) { this._sch().error = `Use 24-hour time HH:mm (00:00–23:59), e.g. 07:00 or 23:55 — not “${el.value}”`; el.setAttribute("aria-invalid", "true"); this._requestRender(); return; }
        el.value = v;
        this._schSet(el.dataset.key, v);
        return;
      }
      case "sch-choice": this._schSet(el.dataset.key, el.value); return;
      case "sch-bool": this._schSet(el.dataset.key, el.checked); return;
      case "sch-num": { const v = Number(el.value); if (el.value === "" || Number.isNaN(v)) return; this._schSet(el.dataset.key, Math.round(v)); return; }
      case "sch-dayplan": {
        const text = el.value.trim();
        const v = text === "" ? "" : parseHHMM(text);
        if (v === null) { this._sch().error = `Use 24-hour time HH:mm, e.g. 23:55 — not “${el.value}”`; el.setAttribute("aria-invalid", "true"); this._requestRender(); return; }
        if (v) el.value = v;
        const plan = JSON.parse(JSON.stringify(this._schVal("day_overrides") || {}));
        const day = plan[el.dataset.day] || {};
        if (v) day[el.dataset.phase] = v; else delete day[el.dataset.phase];
        if (Object.keys(day).length) plan[el.dataset.day] = day; else delete plan[el.dataset.day];
        this._schSet("day_overrides", plan);
        return;
      }
      default: break;
    }
    if (!w) return;
    if (ctl === "rule") {
      const k = el.dataset.kind;
      let v;
      if (k === "bool") v = el.checked;
      else if (k === "int" || k === "float") {
        if (el.value === "" || Number.isNaN(Number(el.value))) return;
        v = k === "int" ? Math.round(Number(el.value)) : Number(el.value);
      } else if (k === "entity") v = el.value.trim() || null;
      else if (k === "time") {
        v = parseHHMM(el.value);
        if (v === null) {
          this._status.rules = { error: `Use 24-hour time HH:mm (00:00–23:59), e.g. 07:00 or 22:30 — not “${el.value}”` };
          el.setAttribute("aria-invalid", "true");
          this._requestRender();
          return;
        }
        el.value = v;
      } else v = el.value;
      const rule = this._model.rules.schema.find((x) => x.key === el.dataset.key);
      if (rule && rule.group === "schedule") { this._saveRule(w, el.dataset.key, v); return; }
      // A new automatic feature is switched on only after its predicted result has been shown.
      if (rule && rule.confirm && (rule.kind === "bool" ? v === true : v !== rule.default)) {
        this._openSim(w, el.dataset.key, v);
        return;
      }
      this._saveRule(w, el.dataset.key, v);
      return;
    }
    if (ctl === "rule-set" || ctl === "rule-flagset") {
      const scope = this._ui.scope && (this._ui.scope !== "room" || w.area_id) ? this._ui.scope : "window";
      const cur = new Set(this._ruleValue(w, scope, el.dataset.key).value || []);
      if (el.checked) cur.add(el.dataset.choice); else cur.delete(el.dataset.choice);
      this._saveRule(w, el.dataset.key, [...cur]);
      return;
    }
    if (ctl === "w-name") this._update(w, { name: el.value });
    else if (ctl === "w-area") this._update(w, { area_id: el.value || null });
    else if (ctl === "w-bearing") this._update(w, el.value === "" ? { direction: w.direction, bearing: null } : { bearing: Number(el.value) });
    else if (ctl === "w-flag") this._update(w, { [el.dataset.flag]: el.checked });
    else if (ctl === "cp-name" || ctl === "cp-pos") {
      const msg = { type: `${WS}/cover_preset/save`, window_id: w.window_id, layer_id: el.dataset.layer, preset_id: el.dataset.preset };
      if (ctl === "cp-name") { if (!el.value.trim()) return; msg.name = el.value.trim(); } else { if (el.value === "" || Number.isNaN(Number(el.value))) return; msg.position = Number(el.value); }
      this._save(`cp-${w.window_id}`, msg);
    }
    else if (ctl === "layer-kind") this._update(w, { layers: this._layersPayload(w, { [el.dataset.layer]: { kind: el.value } }) });
    else if (ctl === "layer-label") this._update(w, { layers: this._layersPayload(w, { [el.dataset.layer]: { label: el.value.trim() || null } }) });
    else if (ctl === "solar") {
      const v = Number(el.value);
      if (el.value === "" && el.dataset.key === "max_elevation") { this._update(w, { solar: { max_elevation: null } }); return; }
      if (el.value === "" || Number.isNaN(v)) return;
      this._update(w, { solar: { [el.dataset.key]: v } });
    } else if (ctl === "obs") {
      const obs = w.solar.obstructions.map((o) => ({ ...o }));
      obs[Number(el.dataset.index)][el.dataset.field] = Number(el.value);
      this._update(w, { solar: { obstructions: obs } });
    }
  }

  _onClick(ev) {
    const el = ev.target.closest("[data-action]");
    if (!el || el.disabled) return;
    const a = el.dataset.action;
    const w = this._view ? this._win(this._view) : null;
    switch (a) {
      case "open-window": this._view = el.dataset.id; this._ui.confirm = null; this._ui.preset = null; this._ui.enable = null; this._ui.sim = null; this._render(); return;
      case "back": this._view = null; this._ui.confirm = null; this._ui.preset = null; this._ui.enable = null; this._ui.sim = null; this._render(); return;
      case "tab": this._tab = el.dataset.tab; this._ui.preset = null; this._render(); return;
      case "settings": this._ui.settings = !this._ui.settings; this._render(); return;
      case "suggestions": this._ui.suggestions = !this._ui.suggestions; this._render(); return;
      case "add-window": this._ui.adding = { name: "", area_id: this._config.area || null, direction: null, layers: [] }; this._render(); return;
      case "cancel-add": this._ui.adding = null; this._render(); return;
      case "review-proposal": {
        const p = this._model.proposals.find((x) => x.key === el.dataset.key);
        if (p) this._ui.adding = { name: p.name, area_id: p.area_id, direction: null, layers: p.covers.map((c) => ({ entity_id: c.entity_id, kind: c.kind })) };
        this._render();
        return;
      }
      case "create-window": this._createWindow(); return;
      case "preview": this._openPlan(el.dataset.window, el.dataset.preset, "preview"); return;
      case "apply": this._openPlan(el.dataset.window, el.dataset.preset, "confirm"); return;
      case "apply-confirm": this._applyConfirmed(); return;
      case "close-confirm": this._ui.confirm = null; this._render(); return;
      case "resume":
        this._save(`resume-${el.dataset.window}`, { type: `${WS}/resume`, window_id: el.dataset.window }, "Resumed");
        return;
      case "close-enable": this._ui.enable = null; this._render(); return;
      case "sim-cancel": this._ui.sim = null; this._render(); return;
      case "sim-apply": this._simApply(); return;
      case "env-use":
        this._save("env-use", { type: `${WS}/auto/settings`, scope: "global", values: { [el.dataset.key]: el.dataset.entity } }, "Used for all windows");
        return;
      case "enable-confirm": this._enableConfirmed(); return;
      case "page": this._page = el.dataset.page; this._view = null; this._ui.mig = null; this._render(); return;
      case "goto-mode": this._page = "modes"; this._view = null; Object.assign(this._gm(), { mode: el.dataset.mode, view: "modes" }); this._render(); return;
      case "mig-open": this._ui.mig = { choices: {}, result: null, policy: null }; this._migReview(); return;
      case "mig-close": this._ui.mig = null; this._render(); return;
      case "mig-apply": this._migApply(); return;
      case "gm-mode": this._gm().mode = el.dataset.mode; this._gm().add = null; this._gm().error = null; this._render(); return;
      case "gm-view": this._gm().view = el.dataset.view; this._gm().cell = null; this._render(); return;
      case "gm-adv-cols": this._gm().advCols = !this._gm().advCols; this._render(); return;
      case "gm-remove": this._gmEdit({ op: "unassign", mode: el.dataset.mode || this._gm().mode, window_id: el.dataset.window, layer_id: el.dataset.layer || null }); return;
      case "gm-add": this._gmAdd(false); return;
      case "gm-replace": this._gmAdd(true); return;
      case "gm-error-close": this._gm().error = null; this._render(); return;
      case "gm-cell": this._gm().cell = { kind: el.dataset.kind, window_id: el.dataset.window || null, layer_id: el.dataset.layer || null, area_id: el.dataset.area || null, mode: el.dataset.mode }; this._render(); return;
      case "gm-cell-close": this._gm().cell = null; this._render(); return;
      case "gm-discard": Object.assign(this._gm(), { ops: [], draft: null, error: null, cell: null }); this._render(); return;
      case "gm-review": this._openReview(this._gm().ops, "global"); return;
      case "review-cancel": this._ui.review = null; this._render(); return;
      case "review-save": this._saveReview(); return;
      case "sch-open": { const s = this._sch(); s.open = s.open === el.dataset.phase ? null : el.dataset.phase; this._render(); return; }
      case "sch-day": {
        const cur = new Set(this._schVal(el.dataset.key) || []);
        const d = Number(el.dataset.day);
        if (cur.has(d)) cur.delete(d); else cur.add(d);
        this._schSet(el.dataset.key, [...cur].sort());
        return;
      }
      case "sch-discard": Object.assign(this._sch(), { values: {}, error: null }); this._render(); return;
      case "sch-review": this._openReviewMsg({ type: `${WS}/schedule/change`, scope: "global", values: { ...this._sch().values } }, "schedule"); return;
      default: break;
    }
    if (!w) return;
    const ed = this._ui.preset;
    switch (a) {
      case "direction": this._update(w, { direction: el.dataset.dir }); return;
      case "mode":
        if (el.dataset.mode === "automatic") { if (w.mode !== "automatic") this._openEnable(w); return; }
        this._ui.enable = null;
        this._update(w, { mode: el.dataset.mode });
        return;
      case "scope": this._ui.scope = el.dataset.scope; this._render(); return;
      case "rule-reset": this._saveRule(w, el.dataset.key, null); return;
      case "rule-day": {
        const scope = this._ui.scope && (this._ui.scope !== "room" || w.area_id) ? this._ui.scope : "window";
        const cur = new Set(this._ruleValue(w, scope, el.dataset.key).value || []);
        const d = Number(el.dataset.day);
        if (cur.has(d)) cur.delete(d); else cur.add(d);
        this._saveRule(w, el.dataset.key, [...cur].sort());
        return;
      }
      case "allow-auto":
        this._save(`w-${w.window_id}`, { type: `${WS}/automation/allow`, window_id: w.window_id, automation: el.dataset.automation, allowed: el.dataset.allowed === "1" }, "Saved");
        return;
      case "remove-window":
        if (confirm(`Delete the window ${w.name} and its presets? The covers are not moved.`)) {
          this._save("delete", { type: `${WS}/window/remove`, window_id: w.window_id }, "Deleted");
          this._view = null;
          this._render();
        }
        return;
      case "remove-layer":
        this._update(w, { layers: this._layersPayload(w, {}).filter((l) => l.layer_id !== el.dataset.layer) });
        return;
      case "add-layer": {
        const cand = this._model.candidates.find((c) => c.entity_id === this._ui.addEntity);
        if (!cand) return;
        this._ui.addEntity = null;
        this._update(w, { layers: [...this._layersPayload(w, {}), { entity_id: cand.entity_id, kind: cand.kind }] });
        return;
      }
      case "select":
        this._save(`p-${w.window_id}`, { type: `${WS}/preset/select`, window_id: w.window_id, preset_id: el.dataset.preset }, "Selected");
        return;
      case "new-preset":
        this._ui.preset = { window_id: w.window_id, preset_id: null, name: "", positions: Object.fromEntries(w.layers.map((l) => [l.layer_id, l.position ?? 50])) };
        this._render();
        return;
      case "edit-preset": {
        const p = w.presets.find((x) => x.preset_id === el.dataset.preset);
        if (p) this._ui.preset = { window_id: w.window_id, preset_id: p.preset_id, name: p.name, positions: { ...p.positions } };
        this._render();
        return;
      }
      case "cancel-preset": this._ui.preset = null; this._render(); return;
      case "save-preset": this._savePreset(w); return;
      case "quick":
        if (ed) { ed.positions[el.dataset.layer] = Number(el.dataset.value); this._render(); }
        return;
      case "dup-preset":
        this._save(`p-${w.window_id}`, { type: `${WS}/preset/duplicate`, window_id: w.window_id, preset_id: el.dataset.preset }, "Duplicated");
        return;
      case "del-preset": {
        const p = w.presets.find((x) => x.preset_id === el.dataset.preset);
        const used = (w.usage && w.usage[p.preset_id]) || [];
        const extra = used.length ? `\nIt is used for: ${used.join(", ")}. Window overrides using it go back to the Global setting; Global Modes assignments using it are removed.` : "";
        if (p && confirm(`Delete the preset ${p.name}?${extra}`)) {
          this._save(`p-${w.window_id}`, { type: `${WS}/preset/delete`, window_id: w.window_id, preset_id: p.preset_id }, "Deleted");
        }
        return;
      }
      case "cp-add": {
        const l = w.layers.find((x) => x.layer_id === el.dataset.layer);
        if (!l) return;
        let n = l.presets.length + 1;
        while (l.presets.some((x) => x.name === `Preset ${n}`)) n += 1;
        this._save(`cp-${w.window_id}`, { type: `${WS}/cover_preset/save`, window_id: w.window_id, layer_id: l.layer_id, name: `Preset ${n}`, position: l.position ?? 50 }, "Added");
        return;
      }
      case "cp-del": {
        const l = w.layers.find((x) => x.layer_id === el.dataset.layer);
        const cp = l && l.presets.find((x) => x.preset_id === el.dataset.preset);
        if (cp && confirm(`Delete the preset ${cp.name} of ${l.name}? Assignments that use it are removed; nothing moves.`)) {
          this._save(`cp-${w.window_id}`, { type: `${WS}/cover_preset/delete`, window_id: w.window_id, layer_id: l.layer_id, preset_id: cp.preset_id }, "Deleted");
        }
        return;
      }
      case "clear-manual": this._save(`w-${w.window_id}`, { type: `${WS}/manual/clear`, window_id: w.window_id }, "Cleared"); return;
      case "rescan": this._save(`w-${w.window_id}`, { type: `${WS}/rescan` }, "Scanned"); return;
      case "reset-calibration": this._save(`w-${w.window_id}`, { type: `${WS}/calibration/reset` }, "Restarted"); return;
      case "add-obstruction": {
        const bearing = w.effective_bearing ?? 90;
        const obs = [...w.solar.obstructions, { from_azimuth: (bearing + 330) % 360, to_azimuth: (bearing + 30) % 360, min_elevation: 10 }];
        this._update(w, { solar: { obstructions: obs } });
        return;
      }
      case "remove-obstruction":
        this._update(w, { solar: { obstructions: w.solar.obstructions.filter((_o, i) => i !== Number(el.dataset.index)) } });
        return;
      default: break;
    }
  }

  async _createWindow() {
    const f = this._ui.adding;
    const msg = { type: `${WS}/window/create`, name: f.name.trim(), area_id: f.area_id || null, layers: f.layers };
    if (f.direction) msg.direction = f.direction;
    const result = await this._save("create", msg, "Created");
    if (result && result.window_id) {
      this._ui.adding = null;
      this._ui.suggestions = false;
      this._view = result.window_id;
      this._tab = f.layers.length ? "presets" : "covers";
    }
    this._render();
  }

  async _savePreset(w) {
    const ed = this._ui.preset;
    const msg = { type: `${WS}/preset/save`, window_id: w.window_id, name: ed.name.trim(), positions: ed.positions };
    if (ed.preset_id) msg.preset_id = ed.preset_id;
    const result = await this._save(`p-${w.window_id}`, msg);
    if (result) this._ui.preset = null;
    else ed.error = this._status[`p-${w.window_id}`]?.error;
    this._render();
  }
}

const STYLE = `<style>
  :host { display: block; }
  .auto { display: flex; gap: 10px; align-items: flex-start; padding: 10px; margin: 8px 0; border-radius: 10px; border: 1px solid var(--divider-color);
    background: var(--secondary-background-color); }
  .auto.automatic { border-color: var(--success-color, #43a047); }
  .auto > ha-icon { --mdc-icon-size: 22px; color: var(--state-icon-color, var(--secondary-text-color)); flex: none; margin-top: 2px; }
  .auto .amain { flex: 1; min-width: 0; display: grid; gap: 4px; }
  .auto .atext { overflow-wrap: anywhere; }
  .auto .chips { display: flex; flex-wrap: wrap; gap: 4px; }
  .auto .chip ha-icon { --mdc-icon-size: 14px; margin-right: 2px; }
  .chip.warnchip { border-color: var(--warning-color, #ffa600); }
  .rgroup { border: 1px solid var(--divider-color); border-radius: 10px; padding: 6px 10px; margin: 6px 0; }
  .rgroup > summary { cursor: pointer; font-weight: 600; padding: 4px 0; }
  .rrow { margin: 4px 0; }
  ol.prec { margin: 4px 0 4px 18px; padding: 0; }
  .segs.days .seg { min-width: 38px; }
  ha-card { container-type: inline-size; overflow: hidden; color: var(--primary-text-color); }
  .header { display: flex; align-items: center; gap: 12px; padding: 16px 16px 8px; }
  .header > ha-icon { --mdc-icon-size: 28px; color: var(--state-icon-color, var(--secondary-text-color)); }
  .titles { flex: 1; min-width: 0; }
  .title { font-size: 1.2em; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pad { padding: 4px 16px 16px; display: grid; gap: 10px; grid-template-columns: minmax(0, 1fr); }
  .sub, .hint { color: var(--secondary-text-color); font-size: 0.85em; }
  .muted { color: var(--secondary-text-color); }
  .ok { color: var(--success-color, #43a047); }
  .warn { color: var(--warning-color, #ff9800); }
  .bad { color: var(--error-color, #db4437); }
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  .badge { font-size: 0.75em; padding: 2px 10px; border-radius: 12px; white-space: nowrap; border: 1px solid var(--divider-color); color: var(--secondary-text-color); }
  button { font: inherit; cursor: pointer; color: var(--primary-text-color); }
  button:disabled { opacity: 0.5; cursor: default; }
  .btn { padding: 6px 16px; border-radius: 18px; border: 1px solid var(--primary-color); background: var(--primary-color); color: var(--text-primary-color, #fff); min-height: 36px; }
  .btn.ghost { background: transparent; color: var(--primary-color); }
  .btn.small { padding: 3px 12px; font-size: 0.9em; min-height: 32px; }
  .btn.warnbtn { background: transparent; color: var(--warning-color, #ff9800); border-color: var(--warning-color, #ff9800); }
  .btn.primary { font-weight: 500; }
  .btn ha-icon { --mdc-icon-size: 16px; margin-right: 4px; vertical-align: -3px; }
  .iconbtn { border: none; background: none; padding: 6px; border-radius: 50%; min-width: 40px; min-height: 40px; }
  .iconbtn[aria-pressed="true"] { background: var(--secondary-background-color); }
  .iconbtn ha-icon { --mdc-icon-size: 22px; color: var(--secondary-text-color); }
  .link { border: none; background: none; color: var(--primary-color); padding: 0 4px; text-decoration: underline; min-height: 28px; }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .chip { padding: 5px 14px; border-radius: 16px; border: 1px solid var(--divider-color); background: var(--card-background-color); display: inline-flex; gap: 6px; align-items: center; }
  .chip.small { padding: 2px 10px; font-size: 0.88em; min-height: 30px; }
  .chip.on, .tab.on, .seg.on { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  .tabs { display: flex; flex-wrap: wrap; gap: 2px; padding: 0 12px; border-bottom: 1px solid var(--divider-color); }
  .tab { position: relative; display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-width: 40px; min-height: 40px;
    padding: 6px 10px; border: none; border-radius: 8px 8px 0 0; background: none; white-space: nowrap; touch-action: manipulation; }
  .tab ha-icon { --mdc-icon-size: 22px; }
  .tab:not(.on) .tlabel { display: none; }
  .tab:not(.on):hover { background: var(--secondary-background-color); }
  .tab:focus-visible, .seg:focus-visible, .btn:focus-visible, .wname:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
  .tab[data-tip]:not(.on):hover::after, .tab[data-tip]:not(.on):focus-visible::after { content: attr(data-tip); position: absolute; top: calc(100% + 4px);
    left: 50%; transform: translateX(-50%); z-index: 3; padding: 3px 8px; border-radius: 6px; font-size: 0.8em; pointer-events: none;
    background: var(--primary-text-color); color: var(--card-background-color, #fff); }
  .tab[data-tip]:first-child::after { left: 0; transform: none; }
  .tab[data-tip]:last-child::after { left: auto; right: 0; transform: none; }
  .tabcap { display: none; padding: 6px 16px 0; font-weight: 500; }
  .subback { display: inline-flex; align-items: center; gap: 6px; min-height: 36px; padding: 4px 12px; border-radius: 18px; border: 1px dashed var(--divider-color);
    background: none; white-space: nowrap; color: var(--secondary-text-color); }
  .subback ha-icon { --mdc-icon-size: 18px; }
  .segs { display: flex; flex-wrap: wrap; gap: 0; border: 1px solid var(--divider-color); border-radius: 18px; overflow: hidden; width: fit-content; max-width: 100%; }
  .seg { padding: 6px 14px; border: none; border-right: 1px solid var(--divider-color); background: none; min-height: 36px; }
  .seg:last-child { border-right: none; }
  .segs.compass .seg { padding: 6px 10px; min-width: 40px; }
  .frow { display: grid; grid-template-columns: 9em minmax(0, 1fr); gap: 8px; align-items: center; }
  .frow > label { color: var(--secondary-text-color); font-size: 0.92em; }
  .fval { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; min-width: 0; }
  select, input[type=text], input[type=number] { font: inherit; padding: 6px 8px; border-radius: 6px; max-width: 100%; min-height: 36px; box-sizing: border-box;
    border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); }
  input[type=text] { flex: 1; min-width: 9em; }
  input.num { width: 5.5em; }
  input[type=range] { flex: 1; min-width: 8em; accent-color: var(--primary-color); }
  input[type=checkbox] { accent-color: var(--primary-color); width: 18px; height: 18px; }
  .note { padding: 8px 10px; border-radius: 8px; background: var(--secondary-background-color); font-size: 0.9em; }
  .note.warn { border-left: 4px solid var(--warning-color, #ff9800); color: var(--primary-text-color); }
  .pill { display: inline-block; font-size: 0.75em; padding: 1px 8px; border-radius: 10px; border: 1px solid currentColor; white-space: nowrap; }
  .pill.ok { color: var(--success-color, #43a047); }
  .pill.warn { color: var(--warning-color, #ff9800); }
  .pill.bad { color: var(--error-color, #db4437); }
  .pill.info { color: var(--primary-color); }
  .pill.muted { color: var(--secondary-text-color); }
  .section { display: grid; gap: 8px; border-top: 1px solid var(--divider-color); padding-top: 10px; }
  .stitle { font-weight: 500; margin-top: 6px; }
  .stitle.small { font-size: 0.95em; margin-top: 8px; }
  .status { font-size: 0.85em; }
  .kvs { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 16px; }
  .kvs.one { grid-template-columns: minmax(0, 1fr); gap: 2px; }
  .kvs > div { display: flex; justify-content: space-between; gap: 8px; border-bottom: 1px solid var(--divider-color); padding: 4px 0; min-width: 0; }
  .kvs > div > span:first-child { color: var(--secondary-text-color); white-space: nowrap; }
  .kvs b { font-weight: 500; text-align: right; overflow-wrap: anywhere; min-width: 0; }
  .issue { display: flex; gap: 8px; align-items: flex-start; padding: 6px 8px; border-radius: 8px; border: 1px solid var(--divider-color); font-size: 0.92em; }
  .issue ha-icon { --mdc-icon-size: 20px; color: var(--secondary-text-color); flex: none; }
  .issue.warn { border-color: var(--warning-color, #ff9800); }
  .issue.warn ha-icon { color: var(--warning-color, #ff9800); }
  .issue.bad { border-color: var(--error-color, #db4437); }
  .issue.bad ha-icon { color: var(--error-color, #db4437); }
  .imain { flex: 1; min-width: 0; overflow-wrap: anywhere; }
  .ibtns { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 6px; }
  .editor { display: grid; gap: 10px; padding: 12px; border: 1px solid var(--primary-color); border-radius: 12px; }
  .ehead { display: flex; justify-content: space-between; gap: 8px; align-items: flex-start; }
  .etitle { font-size: 1.1em; font-weight: 500; }
  .erow { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: space-between; border-top: 1px dashed var(--divider-color); padding-top: 8px; }
  .chk { display: inline-flex; align-items: center; gap: 8px; overflow-wrap: anywhere; min-height: 36px; }
  .chk.ptoggle { align-items: flex-start; }
  .chks.col { display: grid; gap: 4px; }
  details.dladv { margin-top: 8px; border-top: 1px solid var(--divider-color); padding-top: 6px; }
  details.dladv > summary { cursor: pointer; font-weight: 500; padding: 4px 0; }
  code { font-size: 0.85em; opacity: 0.8; overflow-wrap: anywhere; }
  .tscroll { overflow-x: auto; }
  .fct { border-collapse: collapse; width: 100%; font-size: 0.9em; }
  .fct th, .fct td { text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--divider-color); vertical-align: top; }
  .fct th { color: var(--secondary-text-color); font-weight: 500; }
  /* Curtains (0.6.1) */
  .rooms { display: grid; gap: 12px 16px; grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr)); align-items: start; }
  .room { display: grid; gap: 8px; min-width: 0; }
  .wcard { display: grid; gap: 8px; padding: 10px 12px; border: 1px solid var(--divider-color); border-radius: 12px; min-width: 0; align-content: start; }
  .wcard.haswarn { border-color: var(--warning-color, #ff9800); }
  .whead { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .wname { flex: 1 1 auto; min-width: 0; display: inline-flex; align-items: center; gap: 2px; border: none; background: none; padding: 4px 0; font-weight: 500;
    font-size: 1.05em; text-align: left; overflow-wrap: anywhere; min-height: 36px; }
  .wname ha-icon { --mdc-icon-size: 18px; color: var(--secondary-text-color); }
  .crow { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 8px; align-items: center; }
  .crow > ha-icon { --mdc-icon-size: 22px; color: var(--state-icon-color, var(--secondary-text-color)); }
  .cmain { min-width: 0; display: grid; gap: 3px; }
  .cname { overflow-wrap: anywhere; }
  .cval { text-align: right; white-space: nowrap; }
  .bar { display: block; height: 6px; border-radius: 3px; background: var(--divider-color); overflow: hidden; }
  .bar > i { display: block; height: 100%; background: var(--primary-color); border-radius: 3px; }
  .ctl { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .ctl select { flex: 1 1 10em; min-width: 0; }
  .confirm { display: grid; gap: 6px; padding: 10px; border: 1px solid var(--divider-color); border-radius: 10px; background: var(--secondary-background-color); }
  .confirm.ask { border-color: var(--primary-color); }
  .chead { font-size: 1.02em; }
  .prow { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 2px 8px; align-items: center; padding: 4px 0; border-bottom: 1px solid var(--divider-color); }
  .pmain { display: flex; gap: 6px; align-items: center; min-width: 0; overflow-wrap: anywhere; flex-wrap: wrap; }
  .pmain ha-icon { --mdc-icon-size: 18px; color: var(--secondary-text-color); }
  .why { grid-column: 1 / -1; font-size: 0.85em; color: var(--secondary-text-color); overflow-wrap: anywhere; }
  .prop { display: grid; gap: 4px; padding: 8px; border: 1px dashed var(--divider-color); border-radius: 8px; justify-items: start; }
  .clist { display: grid; gap: 4px; max-height: 360px; overflow-y: auto; }
  .cand { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; justify-content: space-between; padding: 2px 0; border-bottom: 1px solid var(--divider-color); }
  .cand.taken { opacity: 0.6; }
  .lcard, .pcard { display: grid; gap: 6px; padding: 10px; border: 1px solid var(--divider-color); border-radius: 10px; }
  .pcard.sel { border-color: var(--primary-color); }
  .phead { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .perow { display: grid; gap: 4px; padding: 6px 0; border-bottom: 1px solid var(--divider-color); }
  .pename { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .pename ha-icon { --mdc-icon-size: 18px; color: var(--secondary-text-color); }
  .sunbox { display: flex; gap: 10px; align-items: center; padding: 8px 10px; border-radius: 8px; background: var(--secondary-background-color); }
  .sunbox ha-icon { --mdc-icon-size: 28px; color: var(--secondary-text-color); }
  .sunbox.hot ha-icon { color: var(--warning-color, #ff9800); }
  .obs { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; padding: 4px 0; border-bottom: 1px dashed var(--divider-color); }
  .ctable { display: grid; gap: 6px; }
  /* 0.6.3 */
  input.time { flex: 0 0 auto; width: 5.5em; min-width: 5.5em; font-variant-numeric: tabular-nums; }
  input[aria-invalid="true"] { border-color: var(--error-color, #db4437); }
  time.ltime { font-variant-numeric: tabular-nums; font-weight: 500; }
  /* Global Modes (0.6.4) */
  .pagetabs { padding: 0 16px 6px; }
  .pagetabs .seg ha-icon { --mdc-icon-size: 18px; margin-right: 4px; vertical-align: -3px; }
  .mgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(104px, 1fr)); gap: 8px; }
  .mcard { display: grid; justify-items: center; align-content: center; gap: 2px; padding: 10px 6px; min-height: 84px; border-radius: 12px;
    border: 1px solid var(--divider-color); background: var(--card-background-color); text-align: center; touch-action: manipulation; }
  .mcard ha-icon { --mdc-icon-size: 26px; color: var(--state-icon-color, var(--secondary-text-color)); }
  .mcard.on { border-color: var(--primary-color); box-shadow: inset 0 0 0 1px var(--primary-color); }
  .mcard.on ha-icon { color: var(--primary-color); }
  .mcard:focus-visible, .mxcell:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
  .mname { font-weight: 500; }
  .mcount { font-size: 0.8em; color: var(--secondary-text-color); }
  .meditor { display: grid; gap: 8px; padding: 12px; border: 1px solid var(--primary-color); border-radius: 12px; min-width: 0; }
  .mhead { display: flex; gap: 10px; align-items: center; }
  .mhead ha-icon { --mdc-icon-size: 26px; color: var(--primary-color); }
  .arows { display: grid; }
  .arow { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr) auto; gap: 4px 8px; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--divider-color); }
  .arow.bad { border-left: 3px solid var(--error-color, #db4437); padding-left: 6px; }
  .arow select { width: 100%; }
  .arow .why ha-icon { --mdc-icon-size: 16px; vertical-align: -3px; }
  .aname { display: flex; flex-wrap: wrap; gap: 2px 8px; align-items: center; min-width: 0; overflow-wrap: anywhere; }
  .apills { display: inline-flex; flex-wrap: wrap; gap: 4px; }
  .addrow { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr) auto; gap: 8px; align-items: center; }
  .addrow select { width: 100%; }
  .draftbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: space-between; padding: 8px 10px; border-radius: 10px;
    border: 1px solid var(--warning-color, #ff9800); background: var(--secondary-background-color); position: sticky; top: 0; z-index: 2; }
  ul.clines { margin: 2px 0 2px 18px; padding: 0; }
  .mxwrap { border: 1px solid var(--divider-color); border-radius: 10px; }
  table.mx { border-collapse: separate; border-spacing: 0; width: 100%; font-size: 0.88em; }
  table.mx th, table.mx td { padding: 3px 4px; border-bottom: 1px solid var(--divider-color); text-align: left; vertical-align: middle; }
  table.mx thead th { font-weight: 500; color: var(--secondary-text-color); text-align: center; min-width: 92px; }
  table.mx thead th ha-icon { --mdc-icon-size: 18px; }
  table.mx tbody th { position: sticky; left: 0; z-index: 1; background: var(--card-background-color); font-weight: 500; min-width: 120px; max-width: 180px; overflow-wrap: anywhere; }
  table.mx thead th:first-child { position: sticky; left: 0; z-index: 1; background: var(--card-background-color); text-align: left; }
  tr.mxroom th, tr.mxroom td { background: var(--secondary-background-color); }
  tr.mxroom th { background: var(--secondary-background-color); }
  tr.mxcover th { font-weight: 400; padding-left: 14px; }
  .mxcell { width: 100%; min-height: 32px; padding: 2px 6px; border-radius: 6px; border: 1px solid transparent; background: none; text-align: left;
    display: flex; gap: 4px; align-items: center; justify-content: space-between; overflow-wrap: anywhere; }
  .mxcell:not(:disabled):hover { border-color: var(--divider-color); }
  .mxcell.on { border-color: var(--primary-color); }
  .mxcell.muted, .mxcell.inh { color: var(--secondary-text-color); }
  .mxcell.na { font-style: italic; }
  .mxcell.bad { color: var(--error-color, #db4437); }
  .mxcell:disabled { opacity: 1; }
  .srctag { display: inline-block; font-size: 0.7em; font-weight: 600; min-width: 1.4em; text-align: center; padding: 0 3px; border-radius: 4px; border: 1px solid currentColor; font-style: normal; }
  .srctag.info { color: var(--primary-color); }
  .srctag.warn { color: var(--warning-color, #ff9800); }
  .srctag.muted { color: var(--secondary-text-color); }
  .cellbox { display: grid; gap: 6px; padding: 10px; border: 1px solid var(--primary-color); border-radius: 10px; background: var(--secondary-background-color); }
  .cellbox select { flex: 1 1 14em; }
  .logline { padding: 3px 0; border-bottom: 1px solid var(--divider-color); overflow-wrap: anywhere; font-size: 0.92em; }
  .cprow { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; padding: 3px 0; }
  .cprow input[type=text] { flex: 1 1 10em; }
  .stitle ha-icon, .frow label ha-icon { --mdc-icon-size: 18px; vertical-align: -3px; }
  @container (max-width: 560px) {
    .arow { grid-template-columns: minmax(0, 1fr) auto; }
    .arow .aname { grid-column: 1 / -1; }
    .addrow { grid-template-columns: minmax(0, 1fr); }
    .pagetabs { padding: 0 12px 6px; }
    .pagetabs .segs { width: 100%; }
    .pagetabs .seg { flex: 1 1 0; }
  }
  /* Global Schedule (0.6.5) */
  .schstatus { display: flex; gap: 12px; align-items: flex-start; padding: 12px; border-radius: 12px; border: 1px solid var(--primary-color); background: var(--secondary-background-color); }
  .schstatus > ha-icon { --mdc-icon-size: 34px; color: var(--primary-color); flex: none; }
  .schstatus .amain { display: grid; gap: 4px; min-width: 0; flex: 1; overflow-wrap: anywhere; }
  .big { font-size: 1.3em; font-weight: 500; }
  .tl { position: relative; height: 18px; border-radius: 9px; overflow: hidden; background: var(--divider-color); }
  .tlseg { position: absolute; top: 0; bottom: 0; }
  .ph-night { background: #3f51b5; } .ph-morning { background: #ffb74d; } .ph-day { background: #fdd835; } .ph-evening { background: #8e6cbf; }
  .tlnow { position: absolute; top: -2px; bottom: -2px; width: 2px; background: var(--primary-text-color); }
  .tlaxis { display: flex; justify-content: space-between; font-size: 0.75em; color: var(--secondary-text-color); font-variant-numeric: tabular-nums; }
  .tllegend { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 0.8em; align-items: center; }
  .tllegend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: -1px; }
  .schrows { display: grid; gap: 6px; }
  .schrow { border: 1px solid var(--divider-color); border-radius: 12px; min-width: 0; }
  .schrow.open { border-color: var(--primary-color); }
  .schrow.now { box-shadow: inset 4px 0 0 var(--success-color, #43a047); }
  .schhead { width: 100%; display: grid; grid-template-columns: auto 8em minmax(0, 1fr) auto auto; gap: 8px; align-items: center; padding: 10px 12px; border: none; background: none; text-align: left; min-height: 48px; }
  .schhead:focus-visible { outline: 2px solid var(--primary-color); outline-offset: -2px; border-radius: 12px; }
  .schhead > ha-icon:first-child { --mdc-icon-size: 24px; color: var(--state-icon-color, var(--secondary-text-color)); }
  .schname { font-weight: 500; }
  .schrule { color: var(--secondary-text-color); font-size: 0.9em; overflow-wrap: anywhere; }
  .schtime { font-variant-numeric: tabular-nums; font-weight: 500; }
  .schbody { display: grid; gap: 8px; padding: 0 12px 12px; }
  input.time.draft { border-color: var(--warning-color, #ff9800); }
  .trow { display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: baseline; padding: 3px 0; border-bottom: 1px solid var(--divider-color); }
  .trow ha-icon { --mdc-icon-size: 16px; }
  .trow time { font-variant-numeric: tabular-nums; font-weight: 500; }
  .trcmp { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  table.schplan input.time { width: 5.2em; min-width: 5.2em; }
  @container (max-width: 560px) {
    .schhead { grid-template-columns: auto minmax(0, 1fr) auto auto; }
    .schhead .schrule { grid-column: 2 / -1; grid-row: 2; }
    .trcmp { grid-template-columns: 1fr; }
  }
  /* 0.6.6 */
  .pagetabs .segs { flex-wrap: wrap; }
  .wlist { display: grid; gap: 6px; }
  .wrow { display: flex; gap: 8px; align-items: center; width: 100%; padding: 10px 12px; border: 1px solid var(--divider-color); border-radius: 12px; background: none; text-align: left; min-height: 48px; }
  .wrow:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
  .wrow .wmain { flex: 1; min-width: 0; display: grid; gap: 2px; overflow-wrap: anywhere; }
  .wrow ha-icon { --mdc-icon-size: 20px; color: var(--secondary-text-color); }
  .wrow ha-icon.warn { color: var(--warning-color, #ff9800); }
  .managed .link { padding: 0 2px; min-height: 24px; font-size: 1em; }
  @container (max-width: 560px) {
    .pagetabs .seg { flex: 1 1 30%; padding: 6px 6px; font-size: 0.9em; }
  }
  @container (max-width: 520px) {
    .tabs { flex-wrap: nowrap; padding: 0 4px; }
    .tab { flex: 1 1 0; min-width: 34px; padding: 6px 0; }
    .tab.on .tlabel { display: none; }
    .tabcap { display: block; }
  }
  @container (max-width: 480px) {
    .pad { padding: 4px 12px 12px; }
    .header { padding: 12px 12px 6px; flex-wrap: wrap; }
    .kvs { grid-template-columns: 1fr; }
    .frow { grid-template-columns: 1fr; gap: 4px; }
    .ctl .btn { flex: 1 1 auto; }
  }
</style>`;

if (!customElements.get(CARD)) {
  customElements.define(CARD, SmartOccupancyCurtainsCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: CARD,
    name: "Smart Curtains",
    description: "Windows by room: covers, presets, automatic sun, temperature and night control per window, with explanations.",
    preview: false,
    documentationURL: "https://github.com/havanissa/HomeAssistant",
  });
  console.info(`%c SMART-OCCUPANCY-CURTAINS-CARD %c ${VERSION} `, "background:#ff9800;color:#fff", "");
}
