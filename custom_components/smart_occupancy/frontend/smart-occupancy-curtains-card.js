/*
 * Smart Occupancy curtains card (served by the smart_occupancy integration, 0.6.1).
 *
 *   type: custom:smart-occupancy-curtains-card
 *   title: Curtains        # optional
 *   area: stova            # optional: only the windows of one room
 *
 * Overview: windows grouped by room, each with its covers and their positions,
 * the recognised preset (or Custom), its mode, sun exposure, manual change and
 * last action, and the selected preset with Apply. Open a window for its
 * settings: General, Covers, Presets, Automation, Sun protection, Diagnostics.
 *
 * The card never calls a service. Every change goes to the integration's
 * websocket API (admin only), and a preset moves covers only after Apply shows
 * exactly which covers move to which position and the person confirms it; the
 * integration then sends the commands and reports each cover's result.
 * Automatic curtain control comes in a later phase: nothing here starts it.
 */

const VERSION = "0.6.1";
const CARD = "smart-occupancy-curtains-card";
const WS = "smart_occupancy/curtains";

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[c]);
const fmtTime = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return esc(iso);
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
};
const pct = (v) => (v == null ? "—" : `${v} %`);
const deg = (v) => (v == null ? "—" : `${Math.round(v * 10) / 10}°`);

const TABS = [
  ["general", "General", "mdi:information-outline"],
  ["covers", "Covers", "mdi:curtains"],
  ["presets", "Presets", "mdi:tune-variant"],
  ["automation", "Automation", "mdi:robot-outline"],
  ["sun", "Sun protection", "mdi:weather-sunny"],
  ["diagnostics", "Diagnostics", "mdi:stethoscope"],
];
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
  morning: "After waking, once there is daylight", day: "Daylight, nobody asleep", sun: "Direct sun on this window",
  evening_privacy: "Dark outside, lights on inside", night: "The household's night", sleep: "Someone is asleep in this room",
  manual_override: "After a manual move (Leave as it is keeps the move)",
};

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
  }

  setConfig(config) {
    this._config = { ...(config || {}) };
    this._render();
  }

  static getStubConfig() { return {}; }
  getCardSize() { return 8; }
  getGridOptions() { return { columns: 12, min_columns: 6, rows: "auto" }; }

  set hass(hass) {
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
        <div class="sub">${windows.length} window${windows.length === 1 ? "" : "s"} · nothing moves on its own</div></div>
        ${this._canEdit ? `<button class="iconbtn" data-action="settings" aria-label="Curtain settings" title="Curtain settings" aria-pressed="${!!ui.settings}"><ha-icon icon="mdi:cog-outline"></ha-icon></button>` : ""}</div>
      <div class="pad">
      <div class="note">Covers move only when you apply a preset and confirm it. Automatic curtain control comes in a later phase.</div>`;
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

  _coverRow(l, w) {
    const op = w.last && w.last.status === "running" ? w.last.items.find((i) => i.layer_id === l.layer_id) : null;
    let state;
    if (l.missing) state = `<span class="bad">Not found</span>`;
    else if (!l.available) state = `<span class="warn">Unavailable</span>`;
    else state = `<b>${pct(l.position)}</b>${l.moving ? ` <span class="pill info">${l.state === "opening" ? "Opening" : "Closing"}</span>` : ""}`;
    const width = l.position == null ? 0 : l.position;
    return `<div class="crow"><ha-icon icon="${KIND_ICON[l.kind] || KIND_ICON.custom}"></ha-icon>
      <div class="cmain"><div class="cname">${esc(l.name)} <span class="sub">${esc(l.kind_word)}</span></div>
        <span class="bar" role="img" aria-label="${esc(l.name)}: ${l.position == null ? "position unknown" : `${l.position} % open`}"><i style="width:${width}%"></i></span></div>
      <div class="cval">${state}${op && op.target != null ? `<div class="sub">→ ${pct(op.target)}</div>` : ""}</div></div>`;
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

  _solarText(w) {
    if (!w.solar_protection) return `<span class="muted">Off</span>${w.exposure.status === "exposed" ? ` <span class="sub">· sun on the window now</span>` : ""}`;
    const tone = EXPO_TONE[w.exposure.status] || "muted";
    return `On · <span class="${tone === "warn" ? "warn" : "muted"}">${esc(w.exposure.text)}</span>`;
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
      <div class="kvs one">
        <div><span>Now</span><b>${this._currentText(w)}</b></div>
        <div><span>Sun protection</span><b>${this._solarText(w)}</b></div>
        <div><span>Manual change</span><b>${this._manualHtml(w)}</b></div>
        <div><span>Last action</span><b>${this._lastHtml(w)}</b></div>
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
      ${this._statusHtml("settings")}</div>`;
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
      automation: () => this._automationTab(w), sun: () => this._sunTab(w), diagnostics: () => this._diagTab(w),
    }[current[0]]();
    return `<div class="header"><button class="subback" data-action="back" aria-label="Back to all windows"><ha-icon icon="mdi:arrow-left"></ha-icon><span>Windows</span></button>
        <div class="titles"><div class="title">${esc(w.name)}</div><div class="sub">${esc(w.area_name || "No room")} · ${esc(dir)} · ${esc(w.mode_word)}</div></div></div>
      <div class="tabs" role="tablist" aria-label="${esc(w.name)}: sections">${TABS.map(([k, l, icon]) => {
        const on = k === current[0];
        return `<button role="tab" id="tab-${k}" aria-selected="${on}" aria-controls="so-cpanel" tabindex="${on ? 0 : -1}" class="tab ${on ? "on" : ""}"
          data-action="tab" data-tab="${k}" aria-label="${esc(l)}" data-tip="${esc(l)}"><ha-icon icon="${icon}"></ha-icon><span class="tlabel" aria-hidden="true">${l}</span></button>`;
      }).join("")}</div>
      <div class="tabcap" aria-hidden="true">${esc(current[1])}</div>
      <div class="pad" role="tabpanel" id="so-cpanel" aria-labelledby="tab-${current[0]}">${this._confirmHtml(w)}${content}</div>`;
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
        ${seg(w.mode === "off", `data-action="mode" data-mode="off"`, "Off")}${seg(w.mode === "observing", `data-action="mode" data-mode="observing"`, "Observing")}
        <button class="seg" disabled title="Automatic curtain control comes in a later phase">Automatic · later</button></div>
        <span class="sub">${w.mode === "off" ? "Off: presets cannot be applied to this window." : "Observing: nothing moves on its own; you can still apply a preset yourself."}</span></div></div>
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
    return `<div class="note">Prepared for the automatic rules of a later phase. Nothing here moves a cover now, and lighting moods are never used for curtains.</div>
      <div class="stitle small">This window will handle</div>
      <div class="chks col">${flag("privacy", "Privacy", "Close for privacy when it is dark outside")}
        ${flag("sleep_protection", "Sleep protection", "Stay closed while someone sleeps in this room")}
        ${flag("solar_protection", "Sun protection", "Shade when direct sun is on this window (see Sun protection)")}</div>
      <div class="stitle small">Which preset each situation will use</div>
      <div class="ctable">${m.conditions.map((c) => `<div class="frow"><label for="cond-${c}">${esc(m.words.conditions[c])}<br><span class="sub">${esc(COND_HINT[c])}</span></label>
        <div class="fval"><select id="cond-${c}" data-control="cond" data-cond="${c}" ${ro}><option value="">${c === "manual_override" ? "Leave as it is" : "Not set"}</option>
          ${w.presets.map((p) => `<option value="${esc(p.preset_id)}" ${w.conditions[c] === p.preset_id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></div></div>`).join("")}</div>
      ${this._statusHtml(`w-${w.window_id}`)}`;
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
        <span><b>Sun protection</b><br><span class="sub">Shown now; covers are not moved for the sun until a later phase.</span></span></label>
      <div class="sunbox ${e.status === "exposed" ? "hot" : ""}"><ha-icon icon="${e.status === "exposed" ? "mdi:white-balance-sunny" : "mdi:weather-sunny-off"}"></ha-icon>
        <div><b>${esc(e.text)}</b><div class="sub">${e.offset != null ? `The sun is ${Math.abs(e.offset)}° ${e.offset >= 0 ? "right" : "left"} of the window's facing direction (seen from inside)` : ""}</div></div></div>
      <div class="kvs">
        <div><span>Window faces</span><b>${w.effective_bearing == null ? "not set" : `${deg(w.effective_bearing)} ${esc(w.compass)}`}</b></div>
        <div><span>Sun azimuth</span><b>${deg(sun.azimuth)}</b></div>
        <div><span>Sun elevation</span><b>${deg(sun.elevation)}</b></div>
        <div><span>Sun angle</span><b>${deg(sun.sun_angle)}</b></div></div>
      <div class="frow"><label for="sol-w">Exposure width</label><div class="fval">± <input id="sol-w" type="number" class="num" min="${lim.exposure_width[0]}" max="${lim.exposure_width[1]}" value="${esc(w.solar.exposure_width)}" data-control="solar" data-key="exposure_width" ${ro}> ° <span class="sub">around the direction the window faces</span></div></div>
      <div class="frow"><label for="sol-e">Minimum elevation</label><div class="fval"><input id="sol-e" type="number" class="num" step="0.5" min="${lim.elevation[0]}" max="${lim.elevation[1]}" value="${esc(w.solar.min_elevation)}" data-control="solar" data-key="min_elevation" ${ro}> ° <span class="sub">lower sun does not reach the window</span></div></div>
      <div class="stitle small">Obstructions</div>
      <div class="hint">A hill, a house or trees: between two directions the sun must be higher to reach this window (e.g. east windows behind a hill).</div>
      ${obs || `<div class="muted">None.</div>`}
      ${this._canEdit && w.solar.obstructions.length < lim.obstructions ? `<button class="link" data-action="add-obstruction">Add obstruction</button>` : ""}
      ${this._statusHtml(`w-${w.window_id}`)}
      <details class="dladv"><summary>Sun angle check</summary><div class="hint">${esc(cal.text)}</div>
        <div class="hint">The sun angle is the house's own reference (0° = the sun faces the south-facing windows). Its relation to the standard azimuth is learned from readings over the day, never assumed.</div>
        ${this._canEdit ? `<button class="link" data-action="reset-calibration">Start the check again</button>` : ""}</details>`;
  }

  _diagTab(w) {
    const words = this._model.words;
    const sel = w.presets.find((p) => p.preset_id === w.selected_preset);
    const rows = w.layers.map((l) => {
      const diff = l.desired != null && l.position != null ? l.position - l.desired : null;
      return `<tr><td><b>${esc(l.name)}</b><div class="sub"><code>${esc(l.entity_id)}</code></div></td><td>${l.missing ? "not found" : esc(l.state)}</td>
        <td>${pct(l.position)}</td><td>${l.desired == null ? "—" : pct(l.desired)}</td>
        <td class="${l.matches_desired === false ? "warn" : ""}">${diff == null ? "—" : `${diff > 0 ? "+" : ""}${diff}`}</td>
        <td class="sub">${["position", "open", "close", "stop"].filter((f) => l.caps[f]).join(", ") || "none"}</td></tr>`;
    }).join("");
    const op = w.last;
    const opHtml = op ? `<div class="sub">${esc(op.preset_name)} · started ${fmtTime(op.started)}${op.finished ? ` · finished ${fmtTime(op.finished)}` : ""}</div>
      ${op.items.map((i) => `<div class="prow"><div class="pmain"><b>${esc(i.name)}</b><span class="sub">${i.target == null ? "unchanged" : `${pct(i.start_position)} → ${pct(i.target)} · reported ${pct(i.final_position)}`}${i.service ? ` · cover.${esc(i.service)}` : ""}</span></div>
        <span class="pill ${RESULT_TONE[i.status] || "muted"}">${esc(words.results[i.status] || i.status)}</span><div class="why">${esc(i.detail || "")}</div></div>`).join("")}` : `<div class="muted">No preset applied yet.</div>`;
    const conflicts = w.layers.flatMap((l) => l.conflicts.map((c) => ({ ...c, cover: l.name })));
    return `<div class="stitle small">Actual and desired${sel ? ` (${esc(sel.name)})` : ""}</div>
      <div class="tscroll"><table class="fct"><thead><tr><th>Cover</th><th>State</th><th>Actual</th><th>Desired</th><th>Diff</th><th>Supports</th></tr></thead><tbody>${rows}</tbody></table></div>
      ${!sel ? `<div class="hint">Select a preset to compare against it.</div>` : ""}
      <div class="kvs"><div><span>Recognised</span><b>${this._currentText(w)}</b></div><div><span>Manual change</span><b>${this._manualHtml(w)}</b></div></div>
      ${w.manual && this._canEdit ? `<button class="link" data-action="clear-manual">Forget the manual change</button>` : ""}
      <div class="stitle small">Last command</div>${op ? `<div>${this._lastHtml(w)}</div>` : ""}${opHtml}
      ${w.history.length > 1 ? `<details class="dladv"><summary>Earlier commands (${w.history.length - 1})</summary>${w.history.slice(1).map((h) => `<div class="prow"><div class="pmain"><b>${esc(h.preset_name)}</b><span class="sub">${fmtTime(h.started)}</span></div>
        <span class="pill ${OVERALL_TONE[h.status] || "muted"}">${esc(words.overall[h.status] || h.status)}</span><div class="why">${esc(h.explanation || "")}</div></div>`).join("")}</details>` : ""}
      <div class="stitle small">Other automations for these covers</div>
      ${conflicts.length ? conflicts.map((c) => `<div class="issue ${c.enabled ? "warn" : ""}"><ha-icon icon="${c.enabled ? "mdi:alert-outline" : "mdi:sleep"}"></ha-icon>
        <div class="imain"><b>${esc(c.name || c.automation)}</b> <span class="sub">${c.enabled ? "enabled" : "disabled"} · ${esc(c.cover)} · ${esc(c.detail)}</span></div></div>`).join("")
        : `<div class="muted">None found among ${this._model.automations} automation${this._model.automations === 1 ? "" : "s"} that move covers.</div>`}
      <div class="hint">Automations are only listed, never changed. Templated targets cannot be detected.</div>
      ${this._canEdit ? `<button class="link" data-action="rescan">Scan again</button>` : ""}
      <div class="stitle small">Checks</div>
      ${w.problems.length ? w.problems.map((p) => `<div class="issue ${p.severity === "warning" ? "warn" : ""}"><ha-icon icon="${p.severity === "warning" ? "mdi:alert-outline" : "mdi:information-outline"}"></ha-icon><div class="imain">${esc(p.text)}</div></div>`).join("") : `<div class="muted">No problems.</div>`}`;
  }

  // ------------------------------------------------------------------ events
  _update(w, fields) {
    return this._save(`w-${w.window_id}`, { type: `${WS}/window/update`, window_id: w.window_id, ...fields });
  }

  _layersPayload(w, change) {
    return w.layers.map((l) => ({ layer_id: l.layer_id, entity_id: l.entity_id, kind: l.kind, label: l.label, ...(change[l.layer_id] || {}) }));
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
    if (ctl === "w-name") this._update(w, { name: el.value });
    else if (ctl === "w-area") this._update(w, { area_id: el.value || null });
    else if (ctl === "w-bearing") this._update(w, el.value === "" ? { direction: w.direction, bearing: null } : { bearing: Number(el.value) });
    else if (ctl === "w-flag") this._update(w, { [el.dataset.flag]: el.checked });
    else if (ctl === "cond") this._update(w, { conditions: { [el.dataset.cond]: el.value || null } });
    else if (ctl === "layer-kind") this._update(w, { layers: this._layersPayload(w, { [el.dataset.layer]: { kind: el.value } }) });
    else if (ctl === "layer-label") this._update(w, { layers: this._layersPayload(w, { [el.dataset.layer]: { label: el.value.trim() || null } }) });
    else if (ctl === "solar") {
      const v = Number(el.value);
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
      case "open-window": this._view = el.dataset.id; this._ui.confirm = null; this._ui.preset = null; this._render(); return;
      case "back": this._view = null; this._ui.confirm = null; this._ui.preset = null; this._render(); return;
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
      default: break;
    }
    if (!w) return;
    const ed = this._ui.preset;
    switch (a) {
      case "direction": this._update(w, { direction: el.dataset.dir }); return;
      case "mode": this._update(w, { mode: el.dataset.mode }); return;
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
    description: "Windows by room: covers, presets and confirmed manual control. Nothing moves on its own.",
    preview: false,
    documentationURL: "https://github.com/havanissa/HomeAssistant",
  });
  console.info(`%c SMART-OCCUPANCY-CURTAINS-CARD %c ${VERSION} `, "background:#ff9800;color:#fff", "");
}
