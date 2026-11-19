/*
 * Smart Occupancy & Lighting compact cards (served by the smart_occupancy companion integration, 1.0.0).
 *
 *   type: custom:smart-occupancy-house-card          # every room: occupancy, lighting, mood, warnings
 *   type: custom:smart-occupancy-mood-editor-card    # Smart Lighting: the current mood and a mood picker
 *   type: custom:smart-occupancy-room-lighting-card  # Smart Room Lighting: one room (area: <area_id>)
 *   type: custom:smart-occupancy-curtains-card       # Smart Curtains: windows by room, presets, modes
 *
 * Phase A6: the Smart Lighting App is the only place where anything is configured. These cards keep the
 * names of the retired editor cards so that existing dashboards keep working, but they are compact,
 * everyday cards: what is happening and the few actions people use daily (mood, resume automatic
 * lighting, a room's Observing / Automatic, a curtain preset or mode). Every action is relayed by the
 * companion to the App, which validates it with its own handlers and commands devices only through the
 * companion's fenced executor. The cards never call a Home Assistant service and never write a configuration.
 *
 * Data: one read-only websocket command (smart_occupancy/companion/view) and one subscription per
 * connection (smart_occupancy/companion/view/subscribe). When the App is away, the companion keeps the last
 * published state and the cards say so ("last seen"); nothing is inferred.
 */

const VERSION = "1.0.0";
const HOUSE_CARD = "smart-occupancy-house-card";
const MOOD_CARD = "smart-occupancy-mood-editor-card";
const ROOM_CARD = "smart-occupancy-room-lighting-card";
const CURTAINS_CARD = "smart-occupancy-curtains-card";
const DOCS = "https://github.com/havanissa/HomeAssistant";

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

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

// ------------------------------------------------------------------ words
const OCC_WORDS = {
  vacant: ["Vacant", "mdi:home-outline"],
  possible_occupancy: ["Possible occupancy", "mdi:account-question"],
  confirmed_occupancy: ["Occupied", "mdi:account"],
  possible_vacancy: ["Vacancy pending", "mdi:account-clock"],
  uncertain: ["Uncertain", "mdi:help-circle-outline"],
};
const MODE_WORDS = { active: "Automatic", observe: "Observing", paused: "Paused", blocked: "Blocked", disabled: "Off" };
const OWNER_WORDS = {
  app: ["Run by the App", "ok"],
  app_away: ["App away: last seen", "warn"],
  none: ["Not run by the App", "muted"],
};
const DOMAIN_WORDS = { occupancy: "Occupancy", lighting: "Lighting", curtains: "Curtains", forecast: "Predictions" };

const val = (view) => (view && typeof view === "object" && "value" in view ? view.value : view);
const attrs = (view) => (view && typeof view === "object" && view.attributes) || {};
const since = (iso, now = new Date()) => (iso ? `since ${fmtTime(iso, now)}` : "");

// ------------------------------------------------------------------ shared data (one subscription per connection)
const FEEDS = new WeakMap();
class Feed {
  constructor(conn) {
    this.conn = conn;
    this.view = null;
    this.error = null;
    this.listeners = new Set();
    this.timer = null;
    this.unsub = null;
    this.loading = null;
  }
  static of(hass) {
    const conn = hass && hass.connection;
    if (!conn) return null;
    let feed = FEEDS.get(conn);
    if (!feed) { feed = new Feed(conn); FEEDS.set(conn, feed); }
    return feed;
  }
  listen(fn) {
    this.listeners.add(fn);
    if (!this.unsub) this.start();
    else if (this.view) fn(this.view);
    return () => { this.listeners.delete(fn); };
  }
  start() {
    this.unsub = true;
    this.refresh();
    this.conn.subscribeMessage(() => this.schedule(), { type: "smart_occupancy/companion/view/subscribe" })
      .then((u) => { this.unsub = u; })
      .catch((err) => { this.error = err && err.message ? err.message : "subscribe failed"; this.emit(); });
    this.conn.addEventListener("ready", () => this.refresh());
  }
  schedule() {
    if (this.timer) return;
    this.timer = setTimeout(() => { this.timer = null; this.refresh(); }, 150);
  }
  refresh() {
    if (this.loading) return this.loading;
    this.loading = this.conn.sendMessagePromise({ type: "smart_occupancy/companion/view" })
      .then((view) => { this.view = view; this.error = null; })
      .catch((err) => { this.error = (err && (err.message || err.code)) || "Smart Occupancy & Lighting is not loaded"; })
      .then(() => { this.loading = null; this.emit(); });
    return this.loading;
  }
  emit() { for (const fn of this.listeners) fn(this.view, this.error); }
  async action(kind, params) {
    const result = await this.conn.sendMessagePromise({ type: "smart_occupancy/companion/action", kind, params });
    this.schedule();
    return result;
  }
}

// ------------------------------------------------------------------ base card
const STYLE = `<style>
  :host { display: block; }
  ha-card { padding: 12px 16px 14px; }
  .head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .head h2 { flex: 1; margin: 0; font-size: 1.1em; font-weight: 500; }
  .pill { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 999px; font-size: 0.8em;
          background: var(--secondary-background-color); color: var(--secondary-text-color); white-space: nowrap; }
  .pill.ok { background: rgba(76, 175, 80, 0.16); color: var(--success-color, #2e7d32); }
  .pill.warn { background: rgba(255, 152, 0, 0.18); color: var(--warning-color, #e65100); }
  .pill.bad { background: rgba(244, 67, 54, 0.16); color: var(--error-color, #c62828); }
  .pill.info { background: rgba(33, 150, 243, 0.14); color: var(--info-color, #1565c0); }
  .pill.muted { opacity: 0.8; }
  .muted { color: var(--secondary-text-color); }
  .small { font-size: 0.85em; }
  .rows { display: grid; gap: 6px; }
  .row { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr) auto; gap: 8px; align-items: center;
         padding: 6px 8px; border-radius: 8px; background: var(--secondary-background-color); }
  .row .name { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row .sub { display: block; font-size: 0.8em; color: var(--secondary-text-color); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row ha-icon { --mdc-icon-size: 20px; vertical-align: -5px; margin-right: 4px; }
  .acts { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
  .btn { font: inherit; font-size: 0.85em; padding: 5px 10px; border-radius: 8px; border: 1px solid var(--divider-color);
         background: var(--card-background-color); color: var(--primary-text-color); cursor: pointer; }
  .btn.primary { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .btn.selected { border-color: var(--primary-color); color: var(--primary-color); font-weight: 600; }
  .btn:disabled { opacity: 0.5; cursor: default; }
  .btn:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
  .banner { display: flex; gap: 8px; align-items: center; padding: 8px 10px; border-radius: 8px; margin-bottom: 8px; font-size: 0.9em; }
  .banner.warn { background: rgba(255, 152, 0, 0.14); }
  .banner.bad { background: rgba(244, 67, 54, 0.12); }
  .banner.info { background: rgba(33, 150, 243, 0.1); }
  .banner ha-icon { --mdc-icon-size: 20px; flex: none; }
  .moods { display: flex; gap: 6px; flex-wrap: wrap; }
  .foot { margin-top: 8px; font-size: 0.8em; color: var(--secondary-text-color); display: flex; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
  .err { color: var(--error-color, #c62828); font-size: 0.85em; margin-top: 6px; }
  ul.plain { margin: 4px 0 0; padding-left: 18px; font-size: 0.85em; }
  @container (max-width: 420px) { .row { grid-template-columns: minmax(0, 1fr); } .acts { justify-content: flex-start; } }
</style>`;

class BaseCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._view = null;
    this._error = null;
    this._busy = null;
    this._actionError = null;
    this._unlisten = null;
    this.shadowRoot.addEventListener("click", (ev) => {
      const el = ev.target.closest("[data-act]");
      if (el && !el.disabled) this._act(el.dataset.act, JSON.parse(el.dataset.params || "{}"));
    });
  }
  setConfig(config) { this._config = { ...(config || {}) }; this._render(); }
  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    setTimeZone(hass);
    if (first || !this._unlisten) this._attach();
    this._render();
  }
  connectedCallback() { if (this._hass) this._attach(); this._render(); }
  disconnectedCallback() { if (this._unlisten) { this._unlisten(); this._unlisten = null; } }
  getCardSize() { return 4; }
  getGridOptions() { return { columns: 12, min_columns: 6, rows: "auto" }; }
  _attach() {
    const feed = Feed.of(this._hass);
    if (!feed || this._unlisten) return;
    this._unlisten = feed.listen((view, error) => { this._view = view; this._error = error || null; this._render(); });
  }
  get admin() { return !!(this._hass && this._hass.user && this._hass.user.is_admin); }
  owner(domain) { return (this._view && this._view.owners && this._view.owners[domain]) || "none"; }
  ownerBanner(domain) {
    const state = this.owner(domain);
    if (state === "app") return "";
    const [text, tone] = OWNER_WORDS[state] || ["Unknown", "muted"];
    const at = this._view && this._view.views_at && this._view.views_at[domain];
    const detail = state === "app_away"
      ? `The Smart Lighting App is not connected. ${DOMAIN_WORDS[domain]} shows the last published state${at ? ` (${fmtTime(at)})` : ""}; nothing is decided or switched until the App is back.`
      : `${DOMAIN_WORDS[domain]} is not run by the Smart Lighting App. Open the App to set it up or take control.`;
    return `<div class="banner ${state === "app_away" ? "warn" : "info"}" role="status"><ha-icon icon="${state === "app_away" ? "mdi:cloud-off-outline" : "mdi:information-outline"}"></ha-icon><span><b>${esc(text)}.</b> ${esc(detail)}</span></div>`;
  }
  async _act(kind, params) {
    const feed = Feed.of(this._hass);
    if (!feed || !this.admin) return;
    this._busy = kind + JSON.stringify(params);
    this._actionError = null;
    this._render();
    try {
      await feed.action(kind, params);
    } catch (err) {
      this._actionError = (err && (err.message || err.code)) || "The action was refused";
    }
    this._busy = null;
    this._render();
  }
  button(label, kind, params, cls = "", title = "") {
    const key = kind + JSON.stringify(params);
    const disabled = !this.admin || this._busy ? "disabled" : "";
    return `<button type="button" class="btn ${cls}" data-act="${esc(kind)}" data-params='${esc(JSON.stringify(params))}' ${disabled} title="${esc(title || (this.admin ? "" : "Administrators only"))}" aria-busy="${this._busy === key}">${esc(label)}</button>`;
  }
  wrap(title, icon, body, extraPill = "") {
    const feed = Feed.of(this._hass);
    const err = this._error ? `<div class="err">${esc(this._error)}</div>` : "";
    const actErr = this._actionError ? `<div class="err" role="alert">${esc(this._actionError)}</div>` : "";
    const connected = this._view && this._view.app_connected;
    const pill = this._view ? `<span class="pill ${connected ? "ok" : "warn"}">${connected ? "App connected" : "App away"}</span>` : "";
    this.shadowRoot.innerHTML = `${STYLE}<ha-card>
      <div class="head"><ha-icon icon="${icon}"></ha-icon><h2>${esc(title)}</h2>${extraPill}${pill}</div>
      ${feed ? body : '<div class="muted small">Waiting for Home Assistant…</div>'}${err}${actErr}
    </ha-card>`;
  }
  _render() { /* overridden */ }
}

// ------------------------------------------------------------------ helpers of the room rows
function roomOccupancy(view, areaId, now) {
  const occ = ((view && view.occupancy) || {})[areaId];
  if (!occ) return { word: "No data", icon: "mdi:help-circle-outline", tone: "muted", sub: "" };
  const state = val(occ.state);
  const a = attrs(occ.state);
  const [word, icon] = OCC_WORDS[state] || [esc(state || "—"), "mdi:help-circle-outline"];
  const tone = state === "confirmed_occupancy" ? "ok" : state === "possible_vacancy" || state === "possible_occupancy" ? "info" : "muted";
  const parts = [];
  if (a.state_since) parts.push(since(a.state_since, now));
  if (a.vacancy_confirm_at && state === "possible_vacancy") parts.push(`vacant at ${fmtTime(a.vacancy_confirm_at, now)}`);
  if (a.reason) parts.push(a.reason);
  return { word, icon, tone, sub: parts.join(" · ") };
}

function roomLighting(view, areaId) {
  const lt = (((view && view.lighting) || {}).rooms || {})[areaId];
  if (!lt) return null;
  const a = attrs(lt.mood_lighting);
  const mode = val(lt.mood_lighting) || a.control_mode || "observe";
  const overrides = Array.isArray(a.manual_overrides) ? a.manual_overrides.length : a.manual_overrides && typeof a.manual_overrides === "object" ? Object.keys(a.manual_overrides).length : 0;
  const daylight = val(lt.daylight);
  return { mode, word: MODE_WORDS[mode] || mode, reason: a.control_mode_reason || "", mood: a.room_mood || a.global_mood || null, overrides,
    daylight, offline: Array.isArray(a.offline_lights) ? a.offline_lights.length : 0, enabled: a.control_enabled !== false };
}

function roomForecast(view, areaId) {
  const fc = (((view && view.forecast) || {}).rooms || {})[areaId];
  if (!fc || val(fc.forecast) == null) return null;
  return { probability: val(fc.forecast), predicted: val(fc.predicted), horizon: attrs(fc.forecast).horizon_minutes };
}

function roomRow(card, view, areaId, name, now, { actions = true } = {}) {
  const occ = roomOccupancy(view, areaId, now);
  const lt = roomLighting(view, areaId);
  const fc = roomForecast(view, areaId);
  const ltText = lt
    ? `<span class="pill ${lt.mode === "active" ? "ok" : lt.mode === "paused" || lt.mode === "blocked" ? "warn" : "muted"}" title="${esc(lt.reason)}">${esc(lt.word)}</span>${lt.overrides ? ` <span class="pill warn">${lt.overrides} manual</span>` : ""}${lt.offline ? ` <span class="pill bad">${lt.offline} offline</span>` : ""}`
    : '<span class="pill muted">No lighting</span>';
  const sub = [occ.sub, lt && lt.mood ? `mood ${lt.mood}` : "", lt && lt.daylight ? `daylight: ${lt.daylight}` : "", fc ? `${fc.probability}% in ${fc.horizon || 15} min` : ""].filter(Boolean).join(" · ");
  let acts = "";
  if (actions && lt && card.admin) {
    if (lt.overrides) acts += card.button("Resume", "room/resume", { area_id: areaId }, "primary", "Clear the manual overrides: automatic lighting takes over again");
    acts += lt.mode === "observe" || lt.mode === "disabled"
      ? card.button("Automatic", "room/mode", { area_id: areaId, mode: "automatic" }, "", "Let the App control this room's lights")
      : card.button("Observe", "room/mode", { area_id: areaId, mode: "observe" }, "", "Stop controlling this room's lights (nothing is switched)");
  }
  return `<div class="row" data-area="${esc(areaId)}">
    <div><span class="name"><ha-icon icon="${occ.icon}"></ha-icon>${esc(name)}</span><span class="sub"><span class="pill ${occ.tone}">${esc(occ.word)}</span> ${esc(sub)}</span></div>
    <div>${ltText}</div>
    <div class="acts">${acts}</div>
  </div>`;
}

function warnings(view) {
  const sys = (view && view.system) || {};
  const sensors = sys.sensors || {};
  const items = [];
  const status = sensors.status;
  if (status && val(status) && val(status) !== "healthy") items.push(`System: ${attrs(status).summary || val(status)}`);
  for (const key of ["unavailable_entities", "missing_entities"]) {
    const v = sensors[key];
    const n = Number(val(v) || 0);
    if (n) {
      const names = (attrs(v).items || attrs(v).entities || []).slice(0, 5).map((i) => (typeof i === "string" ? i : i.name || i.entity_id));
      items.push(`${n} ${key === "unavailable_entities" ? "unavailable" : "missing"} ${n === 1 ? "entity" : "entities"}${names.length ? `: ${names.join(", ")}` : ""}`);
    }
  }
  const mm = sys.mood_mismatch;
  if (mm && val(mm) === true) items.push(`Mood mismatch: main mood is ${attrs(mm).main_mood}, expected ${attrs(mm).expected_main_mood}`);
  for (const [domain, state] of Object.entries((view && view.owners) || {})) {
    if (state === "app_away") items.push(`${DOMAIN_WORDS[domain]}: the App is away; shown values are the last published ones`);
  }
  return items;
}

// ------------------------------------------------------------------ Card: the house
class SmartOccupancyHouseCard extends BaseCard {
  static getConfigElement() { return document.createElement(`${HOUSE_CARD}-editor`); }
  static getStubConfig() { return { title: "Home" }; }
  getCardSize() { return 6; }
  _render() {
    const view = this._view;
    const now = new Date();
    let body = "";
    if (view) {
      const rooms = Object.entries(view.rooms || {}).sort((a, b) => String(a[1].name).localeCompare(String(b[1].name)));
      const occupied = rooms.filter(([id]) => val((view.occupancy || {})[id] && view.occupancy[id].occupied) === true).length;
      body += this.ownerBanner("occupancy");
      const warn = warnings(view);
      if (warn.length && this._config.warnings !== false) body += `<div class="banner warn" role="status"><ha-icon icon="mdi:alert-outline"></ha-icon><span>${warn.map(esc).join("<br>")}</span></div>`;
      body += rooms.length
        ? `<div class="rows">${rooms.map(([id, r]) => roomRow(this, view, id, r.name, now, { actions: this._config.actions !== false })).join("")}</div>`
        : '<div class="muted">No rooms are configured. Open the Smart Lighting App to add rooms.</div>';
      const mood = val((view.system.sensors || {}).effective_mood);
      body += `<div class="foot"><span>${rooms.length ? `${occupied} of ${rooms.length} rooms occupied` : ""}</span><span>${mood ? `Mood: ${esc(mood)}` : ""}${view.lighting && view.lighting.manual_mode ? " · <b>Manual mode</b>" : ""}</span></div>`;
    } else if (!this._error) {
      body = '<div class="muted small">Loading…</div>';
    }
    this.wrap(this._config.title || "Home", "mdi:home-outline", body);
  }
}

// ------------------------------------------------------------------ Card: Smart Lighting (mood)
class SmartOccupancyMoodCard extends BaseCard {
  static getStubConfig() { return {}; }
  getCardSize() { return 3; }
  _render() {
    const view = this._view;
    let body = "";
    if (view) {
      const sensors = view.system.sensors || {};
      const eff = sensors.effective_mood;
      const current = val(eff);
      const a = attrs(eff);
      const select = view.system.mood_select || { enabled: false, options: [] };
      body += this.ownerBanner("lighting");
      if (view.lighting && view.lighting.manual_mode) body += '<div class="banner warn" role="status"><ha-icon icon="mdi:hand-back-right-outline"></ha-icon><span><b>Manual mode.</b> Automatic lighting is paused in every room until another mood is chosen.</span></div>';
      body += `<div class="rows"><div class="row"><div><span class="name"><ha-icon icon="mdi:palette-outline"></ha-icon>${esc(current || "No mood")}</span>
        <span class="sub">${esc(a.follows ? `follows the ${a.follows} source` : "")}${a.auto_mood && a.auto_mood !== current ? ` · automatic would be ${esc(a.auto_mood)}` : ""}${a.consistent === false ? " · mismatch" : ""}</span></div>
        <div>${(view.lighting && view.lighting.summary && view.lighting.summary.active_rooms || []).length} rooms automatic${view.lighting && view.lighting.summary && view.lighting.summary.overrides ? `, ${view.lighting.summary.overrides} manual` : ""}</div><div></div></div></div>`;
      if (select.enabled && select.options.length) {
        body += `<p class="small muted" style="margin:10px 0 4px">Choose a mood${this.admin ? "" : " (administrators only)"}:</p><div class="moods">${select.options.map((m) => this.button(m, "mood/select", { mood: m }, String(m).toLowerCase() === String(current || "").toLowerCase() ? "selected" : "")).join("")}</div>`;
      } else {
        body += `<p class="small muted" style="margin:10px 0 0">The mood follows ${esc(a.follows || "an external source")}; change it there. Mood definitions are edited in the Smart Lighting App.</p>`;
      }
    } else if (!this._error) {
      body = '<div class="muted small">Loading…</div>';
    }
    this.wrap(this._config.title || "Smart Lighting", "mdi:lightbulb-group-outline", body);
  }
}

// ------------------------------------------------------------------ Card: one room
class SmartOccupancyRoomLightingCard extends BaseCard {
  static getConfigElement() { return document.createElement(`${ROOM_CARD}-editor`); }
  static getStubConfig() { return { area: "" }; }
  setConfig(config) {
    if (!config || !config.area) throw new Error("Select a room (area)");
    super.setConfig(config);
  }
  _render() {
    const view = this._view;
    const areaId = this._config.area;
    const now = new Date();
    let body = "";
    let name = this._config.title || areaId;
    if (view) {
      const room = (view.rooms || {})[areaId];
      name = this._config.title || (room && room.name) || areaId;
      if (!room) {
        body = `<div class="banner info"><ha-icon icon="mdi:information-outline"></ha-icon><span>This room (${esc(areaId)}) is not configured in the Smart Lighting App.</span></div>`;
      } else {
        body += this.ownerBanner("lighting");
        body += `<div class="rows">${roomRow(this, view, areaId, room.name, now)}</div>`;
        const lt = roomLighting(view, areaId);
        const occ = (view.occupancy || {})[areaId];
        const obs = ((view.system || {}).room_observation || {})[areaId];
        const notes = [];
        if (lt && lt.reason) notes.push(lt.reason);
        if (occ && attrs(occ.state).active_sensors && attrs(occ.state).active_sensors.length) notes.push(`Active: ${attrs(occ.state).active_sensors.join(", ")}`);
        if (occ && attrs(occ.state).unavailable_sensors && attrs(occ.state).unavailable_sensors.length) notes.push(`Unavailable sensors: ${attrs(occ.state).unavailable_sensors.join(", ")}`);
        if (obs && attrs(obs).lights_unavailable && attrs(obs).lights_unavailable.length) notes.push(`Unavailable lights: ${attrs(obs).lights_unavailable.map((l) => l.name || l.entity_id).join(", ")}`);
        if (lt && lt.offline) notes.push(`${lt.offline} light(s) offline`);
        if (notes.length) body += `<ul class="plain">${notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>`;
        const last = occ && val(occ.last_activity);
        body += `<div class="foot"><span>${last ? `Last activity ${esc(fmtTime(last, now))}` : ""}</span><span>Settings: Smart Lighting App</span></div>`;
      }
    } else if (!this._error) {
      body = '<div class="muted small">Loading…</div>';
    }
    this.wrap(name, "mdi:lightbulb-on-outline", body);
  }
}

// ------------------------------------------------------------------ Card: curtains
class SmartOccupancyCurtainsCard extends BaseCard {
  static getStubConfig() { return {}; }
  getCardSize() { return 5; }
  _window(w, now) {
    const id = w.window_id;
    const presets = Array.isArray(w.presets) ? w.presets : [];
    const current = w.current && (w.current.preset_id || w.current.id || w.current.name) ? w.current : null;
    const currentId = current ? current.preset_id || current.id : w.selected_preset;
    const positions = (w.layers || []).map((l) => (l.position != null ? `${l.name || l.layer_id || "cover"} ${l.position}%` : l.state ? `${l.name || l.layer_id || "cover"} ${l.state}` : "")).filter(Boolean).join(" · ");
    const pills = [];
    pills.push(`<span class="pill ${w.mode === "automatic" ? "ok" : "muted"}">${esc(w.mode_word || w.mode || "")}</span>`);
    if (w.running) pills.push('<span class="pill info">Moving</span>');
    if (w.manual) pills.push(`<span class="pill warn" title="${esc(w.manual.reason || "")}">Manual hold${w.manual.until ? ` until ${esc(fmtTime(w.manual.until, now))}` : ""}</span>`);
    if (w.problems && w.problems.length) pills.push(`<span class="pill bad" title="${esc(w.problems.map((p) => p.text || p.title || p).join("\n"))}">${w.problems.length} problem${w.problems.length === 1 ? "" : "s"}</span>`);
    const why = w.auto && (w.auto.text || w.auto.reason) ? w.auto.text || w.auto.reason : w.last && (w.last.text || w.last.reason) ? w.last.text || w.last.reason : "";
    let acts = "";
    if (this.admin) {
      for (const p of presets.slice(0, 6)) {
        const pid = p.preset_id || p.id;
        acts += this.button(p.name || pid, "curtains/apply", { window_id: id, preset_id: pid }, pid === currentId ? "selected" : "", `Move to the preset ${p.name || pid} (asks the App; nothing moves while a cover is moving)`);
      }
      if (w.manual) acts += this.button("Resume", "curtains/resume", { window_id: id }, "primary", "End the manual hold: the App applies its decision again");
      acts += w.mode === "automatic"
        ? this.button("Manual", "curtains/mode", { window_id: id, mode: "manual" }, "", "Stop automatic control of this window (nothing moves)")
        : this.button("Automatic", "curtains/mode", { window_id: id, mode: "automatic" }, "", "Let the App control this window");
    }
    return `<div class="row" data-window="${esc(id)}">
      <div><span class="name"><ha-icon icon="mdi:curtains"></ha-icon>${esc(w.name || id)}</span><span class="sub">${esc(positions)}${why ? ` · ${esc(why)}` : ""}</span></div>
      <div>${pills.join(" ")}</div>
      <div class="acts">${acts}</div>
    </div>`;
  }
  _render() {
    const view = this._view;
    const now = new Date();
    let body = "";
    if (view) {
      const windows = Object.values((view.curtains || {}).windows || {});
      body += this.ownerBanner("curtains");
      if (!windows.length) {
        body += '<div class="muted">No windows are configured. Open the Smart Lighting App → Curtains.</div>';
      } else {
        const rooms = new Map();
        for (const w of windows) {
          const key = w.area_id || "";
          const name = w.area_name || (view.rooms && view.rooms[key] && view.rooms[key].name) || (key ? key : "No room");
          if (!rooms.has(key)) rooms.set(key, { name, windows: [] });
          rooms.get(key).windows.push(w);
        }
        const only = this._config.area ? [this._config.area] : [...rooms.keys()];
        for (const key of only) {
          const room = rooms.get(key);
          if (!room) continue;
          if (!this._config.area) body += `<p class="small muted" style="margin:8px 0 4px">${esc(room.name)}</p>`;
          body += `<div class="rows">${room.windows.sort((a, b) => String(a.name).localeCompare(String(b.name))).map((w) => this._window(w, now)).join("")}</div>`;
        }
        const s = view.curtains.summary || {};
        body += `<div class="foot"><span>${s.automatic != null ? `${s.automatic} of ${windows.length} automatic` : ""}${s.moving ? ` · ${s.moving} moving` : ""}</span><span>Settings: Smart Lighting App</span></div>`;
      }
    } else if (!this._error) {
      body = '<div class="muted small">Loading…</div>';
    }
    this.wrap(this._config.title || "Smart Curtains", "mdi:curtains", body);
  }
}

// ------------------------------------------------------------------ editors (an area picker; the title)
class AreaCardEditor extends HTMLElement {
  setConfig(config) { this._config = { ...(config || {}) }; this._render(); }
  set hass(hass) { this._hass = hass; this._render(); }
  _render() {
    if (!this._hass || !this._config) return;
    const areas = Object.values(this._hass.areas || {}).sort((a, b) => String(a.name).localeCompare(String(b.name)));
    const withArea = this.constructor.withArea;
    this.innerHTML = `<div style="display:grid;gap:12px;padding:8px 0">
      <label style="display:grid;gap:4px"><span>Title (optional)</span><input name="title" value="${esc(this._config.title || "")}" style="font:inherit;padding:6px"></label>
      ${withArea ? `<label style="display:grid;gap:4px"><span>Room (area)</span><select name="area" style="font:inherit;padding:6px"><option value="">—</option>${areas.map((a) => `<option value="${esc(a.area_id)}" ${a.area_id === this._config.area ? "selected" : ""}>${esc(a.name)}</option>`).join("")}</select></label>` : ""}
    </div>`;
    this.querySelectorAll("input,select").forEach((el) => el.addEventListener("change", () => {
      const config = { ...this._config };
      if (el.value) config[el.name] = el.value; else delete config[el.name];
      this._config = config;
      this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
    }));
  }
}
class HouseCardEditor extends AreaCardEditor { static withArea = false; }
class RoomCardEditor extends AreaCardEditor { static withArea = true; }

if (!customElements.get(HOUSE_CARD)) {
  customElements.define(HOUSE_CARD, SmartOccupancyHouseCard);
  customElements.define(`${HOUSE_CARD}-editor`, HouseCardEditor);
  customElements.define(MOOD_CARD, SmartOccupancyMoodCard);
  customElements.define(ROOM_CARD, SmartOccupancyRoomLightingCard);
  customElements.define(`${ROOM_CARD}-editor`, RoomCardEditor);
  customElements.define(CURTAINS_CARD, SmartOccupancyCurtainsCard);
  window.customCards = window.customCards || [];
  window.customCards.push(
    { type: HOUSE_CARD, name: "Smart Occupancy: Home", description: "Every room: occupancy, lighting mode, overrides, mood and warnings; Resume and Automatic / Observe.", preview: false, documentationURL: DOCS },
    { type: MOOD_CARD, name: "Smart Lighting", description: "The current mood and a mood picker. Moods are edited in the Smart Lighting App.", preview: false, documentationURL: DOCS },
    { type: ROOM_CARD, name: "Smart Room Lighting", description: "One room: occupancy, automatic lighting, overrides, daylight and health.", preview: false, documentationURL: DOCS },
    { type: CURTAINS_CARD, name: "Smart Curtains", description: "Windows by room: mode, presets, holds and what the App decided.", preview: false, documentationURL: DOCS },
  );
  console.info(`%c SMART-OCCUPANCY-CARDS %c ${VERSION} `, "background:#ff9800;color:#fff", "");
}
