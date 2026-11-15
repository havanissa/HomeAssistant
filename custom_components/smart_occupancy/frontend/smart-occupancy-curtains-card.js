/*
 * Smart Occupancy curtains card (served by the smart_occupancy integration, 0.6.3).
 *
 *   type: custom:smart-occupancy-curtains-card
 *   title: Curtains        # optional
 *   area: stova            # optional: only the windows of one room
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

const VERSION = "0.6.3";
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
    this.shadowRoot.innerHTML = `${STYLE}<ha-card>${w ? this._windowView(w) : this._overview()}</ha-card>`;
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
    let body = `<div class="header"><ha-icon icon="mdi:curtains"></ha-icon><div class="titles">
        <div class="title">${esc(this._config.title || "Curtains")}</div>
        <div class="sub">${windows.length} window${windows.length === 1 ? "" : "s"} · ${windows.filter((w) => w.mode === "automatic").length} automatic</div></div>
        ${this._canEdit ? `<button class="iconbtn" data-action="settings" aria-label="Curtain settings" title="Curtain settings" aria-pressed="${!!ui.settings}"><ha-icon icon="mdi:cog-outline"></ha-icon></button>` : ""}</div>
      <div class="pad">
      <div class="note">Windows in Automatic move on their own and explain every decision. Observing windows only say what they would do; every window can still get a preset you apply and confirm.</div>`;
    if (this._canEdit) {
      body += `<div class="actions"><button class="btn" data-action="add-window" ${ui.adding ? "disabled" : ""}><ha-icon icon="mdi:plus"></ha-icon>Add window</button>
        ${props.length ? `<button class="btn ghost" data-action="suggestions" aria-expanded="${!!ui.suggestions}">Suggestions (${props.length})</button>` : ""}</div>`;
    }
    if (ui.settings) body += this._settingsHtml();
    if (ui.suggestions && !ui.adding) body += this._proposalsHtml(props);
    if (ui.adding) body += this._addFormHtml();
    if (!windows.length && !ui.adding) {
      body += `<div class="muted">No windows yet.${this._canEdit ? " Add one, or start from a suggestion: covers that seem to belong together." : ""}</div>`;
    }
    body += `<div class="rooms">${rooms.map((room) => `<section class="room" aria-label="${esc(room.name)}"><div class="stitle">${esc(room.name)}</div>
      ${room.windows.map((id) => this._windowCard(this._win(id))).join("")}</section>`).join("")}</div>`;
    return `${body}</div>`;
  }

  _desired(w) {
    const d = w.auto && w.auto.decision;
    const p = d && d.preset_id ? w.presets.find((x) => x.preset_id === d.preset_id) : null;
    return p || w.presets.find((x) => x.preset_id === w.selected_preset) || null;
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
    const preset = d.preset_id ? w.presets.find((p) => p.preset_id === d.preset_id) : null;
    const what = preset ? esc(preset.name) : d.condition === "manual_override" || d.condition === "waiting" ? "keeps the position" : "no preset set";
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
      <div class="pad" role="tabpanel" id="so-cpanel" aria-labelledby="tab-${current[0]}">${this._enableHtml(w)}${this._confirmHtml(w)}${this._simHtml(w)}${content}</div>`;
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
        data-control="layer-label" data-layer="${esc(l.layer_id)}" ${ro}>${this._canEdit ? `<button class="link" data-action="remove-layer" data-layer="${esc(l.layer_id)}">Remove</button>` : ""}</div></div></div>`).join("");
    const free = m.candidates.filter((c) => !c.assigned_to);
    const sorted = [...free].sort((a, b) => (b.area_id === w.area_id) - (a.area_id === w.area_id) || a.name.localeCompare(b.name));
    const add = this._canEdit && w.layers.length < m.limits.layers ? `<div class="section"><div class="stitle small">Add a cover</div>
      <div class="fval"><select data-control="add-entity" aria-label="Cover to add"><option value="">Choose a cover…</option>
        ${sorted.map((c) => `<option value="${esc(c.entity_id)}" ${this._ui.addEntity === c.entity_id ? "selected" : ""}>${esc(c.name)} · ${esc(c.area_name || "No room")}${c.caps.position ? "" : " · open/close only"}</option>`).join("")}</select>
        <button class="btn small" data-action="add-layer" ${this._ui.addEntity ? "" : "disabled"}>Add</button></div>
      <div class="hint">Covers already in another window are not offered: one cover belongs to one window. A new cover is "Leave unchanged" in the existing presets until you set it.</div></div>` : "";
    return `${layers || `<div class="muted">No covers yet.</div>`}${this._statusHtml(`w-${w.window_id}`)}${add}`;
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
      <div class="stitle small">Which preset each condition uses</div>
      <div class="ctable">${conds.map((c) => `<div class="frow"><label for="cond-${c}"><ha-icon icon="${COND_ICON[c]}"></ha-icon> ${esc(m.words.conditions[c])}<br><span class="sub">${esc(COND_HINT[c] || "")}</span></label>
        <div class="fval"><select id="cond-${c}" data-control="cond" data-cond="${c}" ${ro}><option value="">${c === "glare" || c === "heat" ? "Not set (uses Direct sunlight)" : "Not set (keep the position)"}</option>
          ${w.presets.map((p) => `<option value="${esc(p.preset_id)}" ${w.conditions[c] === p.preset_id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></div></div>`).join("")}</div>
      <div class="hint"><b>Waiting for daylight</b> and a <b>manual override</b> never move anything: the covers stay where they are. <b>Useful solar heating</b> keeps the Morning / Normal daytime preset. Lighting moods are never used for curtains.</div>
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
    const groups = m.rules.groups.filter(([g]) => only(g)).map(([g, label, desc]) => {
      const fields = m.rules.schema.filter((r) => r.group === g);
      let body = fields.map((rule) => this._ruleField(w, rule, scope)).join("");
      if (g === "sleep" && scope === "window") body = `${this._flagSetRow(w, scope, "lamp_bypass", "sleep", "Sleep wins over the window lamp")}${this._flagSetRow(w, scope, "manual_bypass", "sleep", "Sleep ends a manual hold that began before it")}${body}`;
      if (g === "lamp" && scope === "window") {
        body += `<div class="frow"><label for="lamp-preset">Lamp On preset</label><div class="fval"><select id="lamp-preset" data-control="cond" data-cond="lamp" ${this._ro}><option value="">Not set</option>
          ${w.presets.map((p) => `<option value="${esc(p.preset_id)}" ${w.conditions.lamp === p.preset_id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></div></div>`;
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
      case "time": input = `<input type="text" class="time" inputmode="numeric" maxlength="5" autocomplete="off" spellcheck="false" pattern="([01][0-9]|2[0-3]):[0-5][0-9]" placeholder="HH:mm" aria-describedby="${id}-fmt" value="${esc(value || "")}" ${attrs}><span class="sub" id="${id}-fmt">24-hour, HH:mm</span>`; break;
      case "choice": input = `<select ${attrs}>${rule.choices.map(([c, l]) => `<option value="${c}" ${c === value ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`; break;
      case "entity": input = `<input type="text" list="dl-${rule.entity_list || ENTITY_LIST[rule.key] || "sleep"}" value="${esc(value || "")}" placeholder="none" ${attrs}>`; break;
      case "days": input = `<div class="segs days">${this._model.rules.weekdays.map((d, i) => `<button class="seg ${(value || []).includes(i) ? "on" : ""}" aria-pressed="${(value || []).includes(i)}" data-action="rule-day" data-key="${rule.key}" data-day="${i}" ${ro}>${d}</button>`).join("")}</div>`; break;
      case "set": input = `<div class="chks">${rule.choices.map(([c, l]) => `<label class="chk"><input type="checkbox" data-control="rule-set" data-key="${rule.key}" data-choice="${c}" ${(value || []).includes(c) ? "checked" : ""} ${ro}>${esc(l)}</label>`).join("")}</div>`; break;
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
        <div class="why">${fmtTime(x.at)} — ${esc(x.text)}</div></div>`).join("") : `<div class="muted">Nothing yet.</div>`}</details>`;
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
    else if (ctl === "cond") this._update(w, { conditions: { [el.dataset.cond]: el.value || null } });
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
        const used = this._model.conditions.filter((c) => w.conditions[c] === p.preset_id).map((c) => this._model.words.conditions[c]);
        const extra = used.length ? `\nIt is used for: ${used.join(", ")} (these become Not set).` : "";
        if (p && confirm(`Delete the preset ${p.name}?${extra}`)) {
          this._save(`p-${w.window_id}`, { type: `${WS}/preset/delete`, window_id: w.window_id, preset_id: p.preset_id }, "Deleted");
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
