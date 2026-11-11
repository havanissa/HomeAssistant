/*
 * Smart Occupancy mood lighting cards (served by the smart_occupancy integration).
 *
 *   type: custom:smart-occupancy-mood-editor-card     # Global Mood Editor
 *   title: Mood lighting                              # optional
 *
 *   type: custom:smart-occupancy-room-lighting-card   # Room Mood Lighting Editor
 *   area: kamar_nordur                                # area id of an enabled room
 *
 * The cards are editors only: the integration owns and stores the
 * configuration (websocket API, admin-only writes) and keeps working without
 * any dashboard open. The cards never call a light service; a preview never
 * changes a light. They refetch only when the integration reports a change.
 */

const VERSION = "0.3.0";
const MOOD_CARD = "smart-occupancy-mood-editor-card";
const ROOM_CARD = "smart-occupancy-room-lighting-card";
const WS = "smart_occupancy/lighting";

// ------------------------------------------------------------------ helpers
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

const hexToRgb = (hex) => {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || "");
  return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : null;
};
const rgbToHex = (rgb) => "#" + (rgb || [255, 255, 255]).map((c) => Number(c).toString(16).padStart(2, "0")).join("");

const ACTIONS = { inherit: "Inherit", on: "On", off: "Off", ignore: "Ignore" };
const DECISIONS = {
  change: { label: "Change", tone: "info" },
  unchanged: { label: "Unchanged", tone: "ok" },
  blocked: { label: "Blocked", tone: "warn" },
  skipped: { label: "Not controlled", tone: "muted" },
};
const MODES = {
  observe: { label: "Observe", icon: "mdi:eye-outline", tone: "muted" },
  active: { label: "Mood lighting active", icon: "mdi:lightbulb-auto", tone: "ok" },
  blocked: { label: "Blocked", icon: "mdi:lightbulb-alert", tone: "warn" },
};
const HEALTH = {
  healthy: { label: "OK", icon: "mdi:check-circle", tone: "ok" },
  warning: { label: "Warning", icon: "mdi:alert", tone: "warn" },
  unavailable: { label: "Not usable", icon: "mdi:close-circle", tone: "bad" },
  config_error: { label: "Error", icon: "mdi:close-octagon", tone: "bad" },
};
const OCC = {
  vacant: "Vacant", possible_occupancy: "Possible occupancy", confirmed_occupancy: "Occupied",
  possible_vacancy: "Possible vacancy", uncertain: "Uncertain",
};

const sourceText = (src, mood) => {
  if (!src) return "";
  if (src.level === "global") {
    return (src.mood || "").toLowerCase() === (mood || "").toLowerCase() ? `global ${src.mood}` : `inherited from ${src.mood}`;
  }
  return src.level === "room" ? "room override" : "light override";
};

const colourText = (s) => {
  if (!s) return "";
  if (s.color_temp_kelvin != null) return `${s.color_temp_kelvin} K`;
  if (s.rgb_color) return `RGB ${s.rgb_color.join(",")}`;
  if (s.hs_color) return `HS ${s.hs_color.join(",")}`;
  return "";
};

const summary = (s) => {
  if (!s) return "—";
  if (s.action === "off") return "Off";
  if (s.action === "ignore" || s.action === "inherit") return s.action === "ignore" ? "Ignore" : "—";
  const parts = ["On"];
  if (s.brightness_pct != null) parts.push(`${s.brightness_pct} %`);
  const c = colourText(s);
  if (c) parts.push(c);
  if (s.effect) parts.push(s.effect);
  return parts.join(" · ");
};

// One subscription per connection, shared by every card on the page.
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

// --------------------------------------------------------------- base card
class SoLightingBase extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._model = null;
    this._error = null;
    this._mood = null;
    this._drafts = {};
    this._status = {};
    this._preview = null;
    this._ui = {};
    this.shadowRoot.addEventListener("click", (ev) => this._onClick(ev));
    this.shadowRoot.addEventListener("change", (ev) => this._onChange(ev));
    this.shadowRoot.addEventListener("input", (ev) => this._onInput(ev));
  }

  set hass(hass) {
    const first = !this._hass || this._hass.connection !== hass.connection;
    this._hass = hass;
    if (first) {
      if (this._unsub) this._unsub();
      if (this.isConnected) this._unsub = subscribe(hass, () => this._scheduleFetch());
      this._fetch();
      return;
    }
    // Light states are shown live; re-render only when one of them changed.
    const key = this._liveKey();
    if (key !== this._lastLiveKey) {
      this._lastLiveKey = key;
      this._requestRender();
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

  getCardSize() { return 8; }
  getGridOptions() { return { columns: 12, min_columns: 6, rows: "auto" }; }

  _liveKey() {
    const ids = this._liveEntities ? this._liveEntities() : [];
    return ids.map((e) => this._hass?.states[e]?.last_updated || "").join("|");
  }

  _requestRender() {
    // Coalesce bursts (state updates, save results, refetches) into one render per frame.
    if (this._renderQueued) return;
    this._renderQueued = true;
    requestAnimationFrame(() => { this._renderQueued = false; this._render(); });
  }

  // The focused field survives a re-render: same field, typed value and cursor.
  _focusKey(el) {
    if (!el || !el.dataset) return null;
    const d = el.dataset;
    const parts = ["edit", "field", "toggle", "control", "entity", "automation", "action", "key", "tab", "mood"]
      .filter((k) => d[k] !== undefined).map((k) => `[data-${k}="${CSS.escape(d[k])}"]`);
    const type = el.tagName === "INPUT" ? `[type="${el.type}"]` : "";
    return parts.length ? `${el.tagName.toLowerCase()}${type}${parts.join("")}` : null;
  }

  _scheduleFetch() {
    clearTimeout(this._fetchTimer);
    this._fetchTimer = setTimeout(() => this._fetch(), 150);
  }

  async _ws(msg) {
    return this._hass.callWS(msg);
  }

  async _fetch() {
    if (!this._hass || !this._config) return;
    try {
      this._model = await this._ws(this._request());
      this._error = null;
      if (!this._mood) this._mood = this._model.selected_mood;
    } catch (err) {
      this._error = err?.message || String(err);
    }
    this._requestRender();
  }

  async _save(key, msg, okText = "Saved") {
    this._status[key] = { busy: true };
    this._requestRender();
    try {
      await this._ws(msg);
      this._status[key] = { ok: okText };
      delete this._drafts[key];
      setTimeout(() => {
        if (this._status[key]?.ok) { delete this._status[key]; this._requestRender(); }
      }, 2500);
    } catch (err) {
      this._status[key] = { error: err?.message || String(err) };
    }
    this._scheduleFetch();
    this._requestRender();
  }

  _statusHtml(key) {
    const s = this._status[key];
    if (!s) return "";
    if (s.busy) return `<span class="status muted">Saving…</span>`;
    if (s.error) return `<span class="status bad" role="alert">${esc(s.error)}</span>`;
    return `<span class="status ok">✓ ${esc(s.ok)}</span>`;
  }

  get _canEdit() { return !!this._model?.can_edit; }

  // ---------------------------------------------------- setting editor
  // key: editor id; ctx: {scope, mood, target, area_id, own, inherited, effective, caps}
  _editorHtml(key, ctx) {
    const own = this._drafts[key] || ctx.own || { action: "inherit" };
    const inh = ctx.inherited || {};
    const eff = ctx.effective || {};
    const ro = !this._canEdit;
    const mood = ctx.mood;
    const dis = ro ? "disabled" : "";
    const src = (f) => {
      const s = (inh.sources || {})[f];
      return s ? ` <span class="src">(${esc(sourceText(s, mood))})</span>` : "";
    };
    const inhVal = (f, fmt = (v) => v) => (inh[f] != null ? `${esc(fmt(inh[f]))}${src(f)}` : "<span class=muted>not set</span>");
    const colourMode = own.color_temp_kelvin != null ? "kelvin" : own.rgb_color ? "rgb" : own.hs_color ? "hs" : "inherit";
    const caps = ctx.caps;
    const capNote = (ok, text) => (caps && !ok ? `<span class="warn-text"> · ${esc(text)}</span>` : "");
    const opt = (v, label, cur) => `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(label)}</option>`;
    const actionInherited = inh.action && inh.action !== "ignore" ? `${ACTIONS[inh.action]}${src("action")}` : "Ignore";
    const effects = (ctx.effects || []).map((e) => `<option value="${esc(e)}">`).join("");
    const row = (label, body, help = "") => `<div class="frow"><label>${esc(label)}</label><div class="fval">${body}${help}</div></div>`;
    const setBox = (field, on) => `<label class="setbox"><input type="checkbox" data-edit="${esc(key)}" data-toggle="${field}" ${on ? "checked" : ""} ${dis}> set</label>`;
    const brightness = own.brightness_pct != null;
    const effect = own.effect != null;
    const transition = own.transition != null;
    const persistent = own.persistent === true ? "yes" : own.persistent === false ? "no" : "inherit";
    return `<div class="editor" data-editor="${esc(key)}">
      ${row("Action", `<select data-edit="${esc(key)}" data-field="action" ${dis}>
          ${opt("inherit", `Inherit (${ACTIONS[inh.action] || "Ignore"})`, own.action)}${opt("on", "On", own.action)}
          ${opt("off", "Off", own.action)}${opt("ignore", "Ignore (send nothing)", own.action)}</select>`,
        own.action === "inherit" ? `<div class="hint">Inherited: ${actionInherited}</div>` : "")}
      ${row("Brightness", `${setBox("brightness_pct", brightness)}
          ${brightness ? `<input type="range" min="1" max="100" value="${esc(own.brightness_pct)}" data-edit="${esc(key)}" data-field="brightness_pct" ${dis}>
          <input type="number" class="num" min="1" max="100" value="${esc(own.brightness_pct)}" data-edit="${esc(key)}" data-field="brightness_pct" ${dis}> %`
          : `<span class="inh">${inhVal("brightness_pct", (v) => `${v} %`)}</span>`}`,
        capNote(caps?.brightness, "this light is on/off only"))}
      ${row("Colour", `<select data-edit="${esc(key)}" data-field="color_mode" ${dis}>
          ${opt("inherit", "Inherit", colourMode)}${opt("kelvin", "Colour temperature", colourMode)}
          ${opt("rgb", "RGB colour", colourMode)}${opt("hs", "Hue / saturation", colourMode)}</select>
          ${colourMode === "kelvin" ? `<input type="number" class="num" min="1000" max="10000" step="50" value="${esc(own.color_temp_kelvin)}" data-edit="${esc(key)}" data-field="color_temp_kelvin" ${dis}> K` : ""}
          ${colourMode === "rgb" ? `<input type="color" value="${rgbToHex(own.rgb_color)}" data-edit="${esc(key)}" data-field="rgb_color" ${dis}>` : ""}
          ${colourMode === "hs" ? `<input type="number" class="num" min="0" max="360" value="${esc(own.hs_color?.[0] ?? 30)}" data-edit="${esc(key)}" data-field="hs_h" ${dis}>°
             <input type="number" class="num" min="0" max="100" value="${esc(own.hs_color?.[1] ?? 80)}" data-edit="${esc(key)}" data-field="hs_s" ${dis}> %` : ""}
          ${colourMode === "inherit" ? `<span class="inh">${colourText(inh) ? `${esc(colourText(inh))}${src(inh.color_temp_kelvin != null ? "color_temp_kelvin" : inh.rgb_color ? "rgb_color" : "hs_color")}` : "<span class=muted>not set</span>"}</span>` : ""}`,
        colourMode === "kelvin" ? capNote(caps?.color_temp || caps?.color, "no colour temperature support")
          : colourMode !== "inherit" ? capNote(caps?.color, "no colour support") : "")}
      ${row("Effect", `${setBox("effect", effect)}
          ${effect ? `<input type="text" list="fx-${esc(key)}" value="${esc(own.effect)}" data-edit="${esc(key)}" data-field="effect" ${dis}><datalist id="fx-${esc(key)}">${effects}</datalist>`
          : `<span class="inh">${inhVal("effect")}</span>`}`)}
      ${row("Transition", `${setBox("transition", transition)}
          ${transition ? `<input type="number" class="num" min="0" max="300" step="0.5" value="${esc(own.transition)}" data-edit="${esc(key)}" data-field="transition" ${dis}> s`
          : `<span class="inh">${inhVal("transition", (v) => `${v} s`)}</span>`}`)}
      ${row("Ambient", `<select data-edit="${esc(key)}" data-field="persistent" ${dis}>
          ${opt("inherit", `Inherit (${inh.persistent ? "yes" : "no"})`, persistent)}${opt("yes", "Yes: follow the mood even when vacant", persistent)}
          ${opt("no", "No: only while occupied", persistent)}</select>`)}
      <div class="erow"><span class="eff">Result: <b>${esc(summary(eff))}</b></span>
        ${ro ? `<span class="muted">Read only (administrator required)</span>`
          : `<button class="btn" data-action="reset" data-key="${esc(key)}" ${ctx.own ? "" : "disabled"}>Remove override</button>`}
        ${this._statusHtml(key)}</div>
    </div>`;
  }

  _editorChanged(key, el) {
    const ctx = this._editorCtx[key];
    if (!ctx) return;
    const own = { ...(this._drafts[key] || ctx.own || { action: "inherit" }) };
    const f = el.dataset.field;
    const t = el.dataset.toggle;
    if (t) {
      if (el.checked) {
        const inh = ctx.inherited || {};
        own[t] = { brightness_pct: inh.brightness_pct ?? 70, effect: inh.effect ?? (ctx.effects?.[0] || ""),
          transition: inh.transition ?? 1 }[t];
      } else delete own[t];
    } else if (f === "action") own.action = el.value;
    else if (f === "brightness_pct") own.brightness_pct = Math.round(Number(el.value));
    else if (f === "color_mode") {
      const inh = ctx.inherited || {};
      delete own.color_temp_kelvin; delete own.rgb_color; delete own.hs_color;
      if (el.value === "kelvin") own.color_temp_kelvin = inh.color_temp_kelvin ?? 2700;
      if (el.value === "rgb") own.rgb_color = inh.rgb_color ?? [255, 160, 60];
      if (el.value === "hs") own.hs_color = inh.hs_color ?? [30, 80];
    } else if (f === "color_temp_kelvin") own.color_temp_kelvin = Math.round(Number(el.value));
    else if (f === "rgb_color") own.rgb_color = hexToRgb(el.value);
    else if (f === "hs_h") own.hs_color = [Number(el.value), own.hs_color?.[1] ?? 80];
    else if (f === "hs_s") own.hs_color = [own.hs_color?.[0] ?? 30, Number(el.value)];
    else if (f === "effect") own.effect = el.value.trim();
    else if (f === "transition") own.transition = Number(el.value);
    else if (f === "persistent") {
      if (el.value === "inherit") delete own.persistent; else own.persistent = el.value === "yes";
    }
    if (own.action === "off") {
      delete own.brightness_pct; delete own.color_temp_kelvin; delete own.rgb_color; delete own.hs_color; delete own.effect;
    }
    if (own.effect === "") delete own.effect;
    this._drafts[key] = own;
    const msg = { type: `${WS}/profile/set`, scope: ctx.scope, mood: ctx.mood, target: ctx.target, setting: own };
    if (ctx.area_id) msg.area_id = ctx.area_id;
    clearTimeout(this._saveTimers?.[key]);
    this._saveTimers = this._saveTimers || {};
    const immediate = el.tagName === "SELECT" || el.type === "checkbox" || el.type === "color";
    if (immediate) this._render();
    this._saveTimers[key] = setTimeout(() => this._save(key, msg), immediate ? 0 : 500);
  }

  _onInput(ev) {
    const el = ev.target;
    // Keep the range and the number field of brightness in step while dragging.
    if (el.dataset?.field === "brightness_pct") {
      this.shadowRoot.querySelectorAll(`[data-edit="${CSS.escape(el.dataset.edit)}"][data-field="brightness_pct"]`)
        .forEach((o) => { if (o !== el) o.value = el.value; });
    }
  }

  _onChange(ev) {
    const el = ev.target;
    if (el.dataset?.edit) { this._editorChanged(el.dataset.edit, el); return; }
    this._onControl?.(el);
  }

  _onClick(ev) {
    const el = ev.target.closest("[data-action]");
    if (!el || el.disabled) return;
    const a = el.dataset.action;
    if (a === "reset") {
      const ctx = this._editorCtx[el.dataset.key];
      delete this._drafts[el.dataset.key];
      const msg = { type: `${WS}/profile/set`, scope: ctx.scope, mood: ctx.mood, target: ctx.target, setting: null };
      if (ctx.area_id) msg.area_id = ctx.area_id;
      this._save(el.dataset.key, msg, "Override removed");
      return;
    }
    this._onAction?.(a, el);
  }

  _previewHtml(plans) {
    if (!plans) return "";
    if (!plans.length) return `<div class="muted">No enabled rooms.</div>`;
    return plans.map((p) => {
      const c = p.counts || {};
      const rows = p.items.map((i) => {
        const d = DECISIONS[i.decision] || { label: i.decision, tone: "muted" };
        const cur = i.current || {};
        const req = i.requested || {};
        const st = this._hass?.states[i.entity_id];
        const curState = st ? st.state : cur.state;
        return `<tr class="row-${d.tone}">
          <td data-l="Light"><b>${esc(i.name || i.entity_id)}</b><div class="sub">${esc(i.entity_id)}${i.is_group ? " · group" : ""}</div></td>
          <td data-l="Category">${esc(i.category_name || i.category || "—")}</td>
          <td data-l="Current">${esc(curState ?? "—")}${cur.brightness_pct != null ? ` · ${cur.brightness_pct} %` : ""}${cur.color_temp_kelvin ? ` · ${cur.color_temp_kelvin} K` : ""}</td>
          <td data-l="Requested">${esc(req.state || "—")}</td>
          <td data-l="Brightness">${req.brightness_pct != null ? `${req.brightness_pct} %` : "—"}</td>
          <td data-l="Colour">${esc(colourText(req) || "—")}</td>
          <td data-l="Source">${esc(Object.entries(i.resolved?.sources || {}).filter(([k]) => ["action", "brightness_pct", "color_temp_kelvin", "rgb_color", "hs_color"].includes(k))
            .map(([k, s]) => sourceText(s, p.mood)).filter((v, idx, arr) => arr.indexOf(v) === idx).join(", ") || "—")}</td>
          <td data-l="Decision"><span class="pill ${d.tone}">${esc(d.label)}</span>${i.override ? ` <span class="pill warn">manual override</span>` : ""}</td>
          <td data-l="Why" class="why">${esc(i.reason)}${(i.unsupported || []).length ? `<div class="warn-text">Unsupported: ${esc(i.unsupported.map((u) => u.message).join("; "))}</div>` : ""}</td>
        </tr>`;
      }).join("");
      return `<div class="plan">
        <div class="plan-head"><b>${esc(p.room)}</b> · ${esc(p.mood || "no mood")} · ${esc(OCC[p.occupancy_state] || p.occupancy_state || "?")}
          <span class="muted"> · ${c.change || 0} change, ${c.unchanged || 0} unchanged, ${c.blocked || 0} blocked, ${c.skipped || 0} not controlled</span>
          ${p.control_enabled ? "" : `<span class="pill muted">Observe: nothing is sent</span>`}</div>
        ${p.note ? `<div class="note">${esc(p.note)}</div>` : ""}
        ${rows ? `<table class="ptable"><thead><tr><th>Light</th><th>Category</th><th>Current</th><th>Requested</th><th>Brightness</th><th>Colour</th><th>Source</th><th>Decision</th><th>Why</th></tr></thead><tbody>${rows}</tbody></table>`
          : `<div class="muted">No managed lights.</div>`}
      </div>`;
    }).join("");
  }

  _problemsHtml(problems, title = "Validation") {
    const list = (problems || []).filter((p) => p.severity !== "info" || this._ui.showInfo);
    const infos = (problems || []).filter((p) => p.severity === "info").length;
    if (!list.length && !infos) return "";
    return `<div class="problems"><div class="ptitle">${esc(title)}</div>
      ${list.map((p) => `<div class="problem pr-${p.severity}">
        <ha-icon icon="${p.severity === "error" ? "mdi:close-octagon" : p.severity === "warning" ? "mdi:alert" : "mdi:information-outline"}"></ha-icon>
        <div><b>${esc(p.title)}</b>${p.room ? ` · ${esc(p.room)}` : ""}<div>${esc(p.explanation)}</div><div class="sub">${esc(p.action)}</div></div></div>`).join("")}
      ${infos ? `<button class="link" data-action="toggle-info">${this._ui.showInfo ? "Hide" : "Show"} ${infos} note(s)</button>` : ""}
    </div>`;
  }

  _shell(body) {
    return `${STYLE}<ha-card>${body}</ha-card>`;
  }

  _render() {
    if (!this.isConnected || !this._config) return;
    if (!this._hass) { this.shadowRoot.innerHTML = this._shell(`<div class="pad muted">Loading…</div>`); return; }
    if (this._error && !this._model) {
      this.shadowRoot.innerHTML = this._shell(`<div class="pad bad"><ha-icon icon="mdi:alert-circle-outline"></ha-icon>
        Smart Occupancy mood lighting is not available: ${esc(this._error)}</div>`);
      return;
    }
    if (!this._model) { this.shadowRoot.innerHTML = this._shell(`<div class="pad muted">Loading…</div>`); return; }
    const scroll = this.shadowRoot.querySelector(".scroll-keep")?.scrollTop;
    const active = this.shadowRoot.activeElement;
    const focusKey = this._focusKey(active);
    const typing = active && active.tagName === "INPUT" && !["checkbox", "range", "color"].includes(active.type)
      ? { value: active.value, start: active.selectionStart, end: active.selectionEnd } : null;
    this._editorCtx = {};
    this.shadowRoot.innerHTML = this._shell(this._body());
    if (scroll) { const s = this.shadowRoot.querySelector(".scroll-keep"); if (s) s.scrollTop = scroll; }
    if (focusKey) {
      const el = this.shadowRoot.querySelector(focusKey);
      if (el) {
        if (typing && el.value !== typing.value) el.value = typing.value;
        el.focus({ preventScroll: true });
        try { if (typing && typing.start != null) el.setSelectionRange(typing.start, typing.end); } catch (_e) { /* number inputs */ }
      }
    }
  }
}

// --------------------------------------------------------- Global Mood Editor
class SmartOccupancyMoodEditorCard extends SoLightingBase {
  static getConfigElement() { return document.createElement(`${MOOD_CARD}-editor`); }
  static getStubConfig() { return {}; }

  setConfig(config) {
    this._config = { ...(config || {}) };
    this._render();
  }

  _request() { return { type: `${WS}/editor`, mood: this._mood || null }; }

  _liveEntities() { return []; }

  _body() {
    const m = this._model;
    const mood = this._mood || m.selected_mood;
    const status = m.mood || {};
    const moodInfo = m.moods.find((x) => x.name === mood) || {};
    const ro = !this._canEdit;
    const cat = this._ui.category || m.categories[0]?.key;
    const effects = (key) => [...new Set((m.categories.find((c) => c.key === key)?.lights || [])
      .flatMap((l) => this._hass.states[l.entity_id]?.attributes?.effect_list || []))];
    const rows = m.categories.map((c) => {
      const e = m.global[c.key] || {};
      const eff = e.effective || {};
      const own = e.own;
      const srcs = [...new Set(Object.values(eff.sources || {}).map((s) => sourceText(s, mood)))].join(", ");
      return `<tr class="${c.key === cat ? "sel" : ""}" data-action="pick-category" data-key="${esc(c.key)}" tabindex="0">
        <td data-l="Category"><b>${esc(c.name)}</b><div class="sub">${c.label_name ? `label ${esc(c.label_name)}` : c.label_missing ? "<span class=warn-text>label deleted</span>" : "<span class=warn-text>no label</span>"} · ${c.lights.length} light(s)</div></td>
        <td data-l="Profile">${esc(summary(eff))}</td>
        <td data-l="Source">${own ? `<span class="pill info">set here</span> ` : ""}<span class="sub">${esc(srcs || "nothing set")}</span></td>
      </tr>`;
    }).join("");
    const catObj = m.categories.find((c) => c.key === cat);
    let editor = "";
    if (catObj && mood) {
      const key = `global|${mood}|${cat}`;
      const e = m.global[cat] || {};
      this._editorCtx[key] = { scope: "global", mood, target: cat, own: e.own, inherited: e.inherited, effective: e.effective, effects: effects(cat) };
      editor = `<div class="section"><div class="stitle">${esc(catObj.name)} in ${esc(mood)}</div>${this._editorHtml(key, this._editorCtx[key])}</div>`;
    }
    const moodOpts = (cur, none) => `${none ? `<option value="">${esc(none)}</option>` : ""}${m.moods.filter((x) => x.name !== mood)
      .map((x) => `<option value="${esc(x.name)}" ${x.name === cur ? "selected" : ""}>${esc(x.name)}</option>`).join("")}`;
    const handling = moodInfo.handling;
    const s = m.settings;
    return `
      <div class="header"><ha-icon icon="mdi:palette-outline"></ha-icon>
        <div class="titles"><div class="title">${esc(this._config.title || "Mood lighting profiles")}</div>
          <div class="sub">Now: <b>${esc(status.mood || "no mood")}</b> · ${esc(status.detail || "")}</div></div></div>
      <div class="pad">
        <div class="chips" role="tablist">${m.moods.map((x) => `<button class="chip ${x.name === mood ? "on" : ""}" role="tab" data-action="pick-mood" data-mood="${esc(x.name)}"
          title="${x.in_main ? "Option of Main Mood" : "Not an option of Main Mood"}">${x.in_main ? "" : "⚠ "}${esc(x.name)}${x.name === status.mood ? " •" : ""}</button>`).join("")}</div>
        ${moodInfo.name ? `<div class="moodinfo">
          ${moodInfo.in_main ? `<span class="pill ok">Main Mood option</span>` : `<div class="note warn">'${esc(mood)}' is not an option of Main Mood, so it never becomes the effective mood. Add it to the Main Mood helper (Settings → Devices & services → Helpers) to use it; until then it can be a room mood or a parent/fallback mood.${moodInfo.in_auto || moodInfo.in_manual ? ` It is offered by ${[moodInfo.in_auto ? "Auto Mood" : "", moodInfo.in_manual ? "Manual Mood" : ""].filter(Boolean).join(" and ")}.` : ""}</div>`}
          <div class="frow"><label>Inherits from</label><div class="fval"><select data-control="parent" ${ro ? "disabled" : ""}>${moodOpts(moodInfo.parent, s.fallback_mood ? `Fallback mood (${s.fallback_mood})` : "Nothing")}</select>${this._statusHtml("parent")}</div></div>
          ${!moodInfo.in_main ? `<div class="frow"><label>While ${esc(mood)} is selected</label><div class="fval">
            <select data-control="handling" ${ro ? "disabled" : ""}>
              <option value="follow_main" ${handling?.mode === "follow_main" ? "selected" : ""}>Keep following Main Mood (default)</option>
              <option value="no_mood" ${handling?.mode === "no_mood" ? "selected" : ""}>No mood: change nothing</option>
              <option value="use_mood" ${handling?.mode === "use_mood" ? "selected" : ""}>Use another mood</option></select>
            ${handling?.mode === "use_mood" ? `<select data-control="handling-mood" ${ro ? "disabled" : ""}>${moodOpts(handling.mood, "Choose…")}</select>` : ""}
            ${this._statusHtml("handling")}</div></div>` : ""}
        </div>` : `<div class="note">No moods found. Check the mood helpers in the integration options.</div>`}
        ${m.categories.length ? `<table class="ctable"><thead><tr><th>Category</th><th>${esc(mood || "")}</th><th>Source</th></tr></thead><tbody>${rows}</tbody></table>`
          : `<div class="note">No light categories. Add them in Settings → Devices & services → Smart Occupancy → Configure → Light categories.</div>`}
        ${editor}
        <div class="section"><div class="stitle">Preview <button class="btn" data-action="preview">Preview ${esc(mood || "")}</button>
          ${this._preview ? `<button class="link" data-action="close-preview">Close</button>` : ""}</div>
          <div class="sub">Shows what each enabled room would do in this mood. A preview never changes a light.</div>
          ${this._preview?.error ? `<div class="bad">${esc(this._preview.error)}</div>` : ""}
          <div class="scroll-keep">${this._previewHtml(this._preview?.plans)}</div></div>
        <details class="section" ${this._ui.settings ? "open" : ""}><summary data-action="settings-toggle">Settings</summary>
          <div class="frow"><label>Fallback mood</label><div class="fval"><select data-control="fallback" ${ro ? "disabled" : ""}><option value="">None</option>
            ${m.moods.map((x) => `<option value="${esc(x.name)}" ${x.name === s.fallback_mood ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select>
            <div class="hint">Every mood inherits unset values from it (after its own parent).</div>${this._statusHtml("fallback")}</div></div>
          <div class="frow"><label>Exclusion label</label><div class="fval"><select data-control="exclude" ${ro ? "disabled" : ""}><option value="">None</option>
            ${m.labels.map((l) => `<option value="${esc(l.label_id)}" ${l.label_id === s.exclude_label ? "selected" : ""}>${esc(l.name)}</option>`).join("")}</select>
            <div class="hint">Lights or groups with this label are never controlled.</div>${this._statusHtml("exclude")}</div></div>
          <div class="frow"><label>Manual override</label><div class="fval"><input type="number" class="num" min="0" max="1440" value="${esc(s.manual_override_minutes)}" data-control="override-minutes" ${ro ? "disabled" : ""}> min
            <div class="hint">How long a manual change is respected (0 = until the room is vacant).</div>${this._statusHtml("override-minutes")}</div></div>
          <div class="frow"><label>Changes without context</label><div class="fval"><select data-control="unattributed" ${ro ? "disabled" : ""}>
            <option value="ignore" ${s.unattributed === "ignore" ? "selected" : ""}>Not a manual change (default)</option>
            <option value="manual" ${s.unattributed === "manual" ? "selected" : ""}>Treat as a manual change</option></select>
            <div class="hint">For lights changed outside Home Assistant (e.g. a directly bound switch).</div>${this._statusHtml("unattributed")}</div></div>
        </details>
        ${this._problemsHtml(m.problems)}
      </div>`;
  }

  _onAction(a, el) {
    if (a === "pick-mood") { this._mood = el.dataset.mood; this._preview = null; this._fetch(); }
    else if (a === "pick-category") { this._ui.category = el.dataset.key; this._render(); }
    else if (a === "toggle-info") { this._ui.showInfo = !this._ui.showInfo; this._render(); }
    else if (a === "settings-toggle") { this._ui.settings = !el.parentElement.open; }
    else if (a === "close-preview") { this._preview = null; this._render(); }
    else if (a === "preview") {
      this._preview = { plans: null };
      this._ws({ type: `${WS}/preview`, mood: this._mood })
        .then((r) => { this._preview = r; this._render(); })
        .catch((err) => { this._preview = { error: err?.message || String(err) }; this._render(); });
    }
  }

  _onControl(el) {
    const c = el.dataset.control;
    const mood = this._mood;
    if (c === "parent") this._save("parent", { type: `${WS}/mood/set`, mood, parent: el.value || null });
    else if (c === "handling") {
      const value = el.value === "follow_main" ? null : { mode: el.value, mood: null };
      if (el.value === "use_mood") { this._ui.useMood = true; this._model.moods.find((x) => x.name === mood).handling = { mode: "use_mood" }; this._render(); return; }
      this._save("handling", { type: `${WS}/settings/set`, unrepresentable: { [mood]: value } });
    } else if (c === "handling-mood") {
      this._save("handling", { type: `${WS}/settings/set`, unrepresentable: { [mood]: { mode: "use_mood", mood: el.value } } });
    } else if (c === "fallback") this._save("fallback", { type: `${WS}/settings/set`, fallback_mood: el.value || null });
    else if (c === "exclude") this._save("exclude", { type: `${WS}/settings/set`, exclude_label: el.value || null });
    else if (c === "override-minutes") this._save("override-minutes", { type: `${WS}/settings/set`, manual_override_minutes: Number(el.value) });
    else if (c === "unattributed") this._save("unattributed", { type: `${WS}/settings/set`, unattributed: el.value });
  }
}

// ------------------------------------------------ Room Mood Lighting Editor
class SmartOccupancyRoomLightingCard extends SoLightingBase {
  static getConfigElement() { return document.createElement(`${ROOM_CARD}-editor`); }

  static getStubConfig(hass) {
    const rooms = lightingSensors(hass);
    const pilot = rooms.find((s) => s.attributes.room === "Kamar Norður") || rooms[0];
    return { area: pilot ? pilot.attributes.area_id : "" };
  }

  setConfig(config) {
    if (!config || !config.area) throw new Error("Select a room (area)");
    if (this._config && this._config.area !== config.area) { this._model = null; this._mood = null; this._preview = null; }
    this._config = { ...config };
    this._ui.tab = this._ui.tab || "lights";
    if (this._hass) this._fetch();
    this._render();
  }

  _request() { return { type: `${WS}/editor`, area_id: this._config.area, mood: this._mood || null }; }

  _liveEntities() { return (this._model?.room?.lights || []).map((l) => l.entity_id); }

  _body() {
    const m = this._model;
    const r = m.room;
    if (!r) return `<div class="pad bad">Room ${esc(this._config.area)} not found.</div>`;
    const d = r.diagnostics;
    const mode = MODES[d.control_mode] || MODES.observe;
    const tab = this._ui.tab;
    const tabs = [["lights", "Lights"], ["profiles", "Profiles"], ["preview", "Preview"], ["control", "Control"]];
    const health = r.health || {};
    const hrow = (title, h) => {
      const info = HEALTH[h?.status] || HEALTH.healthy;
      const reasons = (h?.reasons || []).map((x) => `<li>${esc(x)}</li>`).join("");
      return `<div class="health hl-${info.tone}"><div class="hhead"><ha-icon icon="${info.icon}"></ha-icon><b>${esc(title)}</b><span class="hstatus">${esc(info.label)}</span></div>${reasons ? `<ul>${reasons}</ul>` : ""}</div>`;
    };
    const kv = (k, v, cls = "") => `<div class="kv ${cls}"><span>${esc(k)}</span><b>${v}</b></div>`;
    const counts = d.last_counts || {};
    const body = { lights: () => this._lightsTab(r), profiles: () => this._profilesTab(r), preview: () => this._previewTab(r),
      control: () => this._controlTab(r) }[tab]();
    return `
      <div class="header"><ha-icon icon="${mode.icon}" class="${mode.tone}"></ha-icon>
        <div class="titles"><div class="title">${esc(this._config.title || r.name)}</div>
          <div class="sub">${esc(r.occupancy.state ? OCC[r.occupancy.state] || r.occupancy.state : "occupancy unknown")}${r.occupancy.confidence != null ? ` · evidence ${r.occupancy.confidence}%` : ""}</div></div>
        <span class="badge ${mode.tone}" title="${esc(d.control_mode_reason)}">${esc(mode.label)}</span></div>
      <div class="pad">
        <div class="grid">
          ${kv("Global mood", esc(d.global_mood || "none"))}
          ${kv("Room mood", esc(d.room_mood || "none"))}
          ${kv("Follows global", d.follows_global ? "yes" : "no (room mood)")}
          ${kv("Control", esc(mode.label))}
          ${kv("Managed lights", d.managed_lights)}
          ${kv("Offline", d.offline_lights.length, d.offline_lights.length ? "warn" : "")}
          ${kv("Unsupported", d.unsupported.length, d.unsupported.length ? "warn" : "")}
          ${kv("Conflicts", d.blocking_automations.length ? `${d.blocking_automations.length} blocking` : d.conflicts.length ? `${d.conflicts.length} (none blocking)` : "0", d.blocking_automations.length ? "warn" : "")}
          ${kv("Manual overrides", d.manual_overrides.length, d.manual_overrides.length ? "info" : "")}
          ${kv("Last applied", d.last_applied_profile ? `${esc(d.last_applied_profile)} · ${fmtTime(d.last_applied_at)}` : "never")}
          ${kv("Last result", d.last_result ? `${esc(d.last_result.replace("_", " "))} · ${fmtTime(d.last_decision_at)}` : "—", d.last_result === "failed" || d.last_result === "partial" ? "warn" : "")}
        </div>
        ${d.last_reason ? `<div class="reason">Last decision: ${esc(d.last_reason)}${d.last_errors?.length ? `<div class="bad">${esc(d.last_errors.join("; "))}</div>` : ""}</div>` : `<div class="reason">${esc(d.control_mode_reason)}</div>`}
        ${d.global_mood_detail && d.mood_source !== "main" ? `<div class="note warn">${esc(d.global_mood_detail)}</div>` : ""}
        <div class="healths">${hrow("Occupancy", health.occupancy)}${hrow("Lighting", health.lighting)}${hrow("Configuration", health.configuration)}</div>
        <div class="tabs" role="tablist">${tabs.map(([k, l]) => `<button role="tab" class="tab ${k === tab ? "on" : ""}" data-action="tab" data-tab="${k}">${l}</button>`).join("")}</div>
        <div class="tabbody">${body}</div>
      </div>`;
  }

  _lightsTab(r) {
    const ro = !this._canEdit;
    const s = r.settings;
    const rows = r.lights.map((l) => {
      const st = this._hass.states[l.entity_id];
      const state = st ? st.state : l.state || "missing";
      const b = st?.attributes?.brightness;
      const badges = [
        l.is_group ? `<span class="pill">${l.opaque_group ? "group (members hidden)" : `group of ${l.members.length}`}</span>` : "",
        l.discovered ? `<span class="pill">by label</span>` : l.explicit ? `<span class="pill">configured</span>` : "",
        l.covered_by ? `<span class="pill muted">via ${esc(l.covered_by)}</span>` : "",
        l.excluded ? `<span class="pill muted">excluded (${esc(l.excluded)})</span>` : "",
        l.override ? `<span class="pill warn">manual ${esc(l.override.source)}${l.override.until ? ` until ${fmtTime(l.override.until)}` : " until vacant"}</span>
          ${ro ? "" : `<button class="link" data-action="clear-override" data-entity="${esc(l.entity_id)}">clear</button>`}` : "",
        (l.conflicts || []).filter((c) => c.enabled && !c.acknowledged).length ? `<span class="pill warn">automation conflict</span>` : "",
        ["unavailable", "unknown", "missing"].includes(state) ? `<span class="pill bad">${esc(state)}</span>` : "",
      ].join(" ");
      const excludedByRoom = (s.excluded || []).includes(l.entity_id);
      return `<div class="light">
        <ha-icon icon="${l.is_group ? "mdi:lightbulb-group" : "mdi:lightbulb"}" class="${state === "on" ? "lit" : ""}"></ha-icon>
        <div class="lmain"><b>${esc(l.name || l.entity_id)}</b><div class="sub">${esc(l.entity_id)} · ${esc(l.category_name || "no category")} · ${esc(state)}${state === "on" && b != null ? ` ${Math.round(b / 2.55)} %` : ""}</div><div>${badges}</div></div>
        <label class="switch" title="Managed by mood lighting"><input type="checkbox" data-control="exclude-light" data-entity="${esc(l.entity_id)}" ${excludedByRoom ? "" : "checked"} ${ro || l.missing ? "disabled" : ""}> managed</label>
      </div>`;
    }).join("");
    return `<div class="row-between"><label class="switch"><input type="checkbox" data-control="auto-discover" ${s.auto_discover ? "checked" : ""} ${ro ? "disabled" : ""}>
        Also manage lights in this area that have a category label</label>${this._statusHtml("room")}</div>
      ${rows || `<div class="note">No managed lights. Add category labels to lights in this area, or select lights in the integration options (Configure → Rooms).</div>`}`;
  }

  _profilesTab(r) {
    const m = this._model;
    const mood = this._mood || m.selected_mood;
    const ro = !this._canEdit;
    const s = r.settings;
    const moodOpts = m.moods.map((x) => `<option value="${esc(x.name)}" ${x.name === mood ? "selected" : ""}>${esc(x.name)}${x.in_main ? "" : " (not in Main Mood)"}</option>`).join("");
    const overrideOpts = `<option value="">Follow the global mood (now ${esc(r.diagnostics.global_mood || "none")})</option>` +
      m.moods.map((x) => `<option value="${esc(x.name)}" ${x.name === s.mood_override ? "selected" : ""}>${esc(x.name)}</option>`).join("");
    const cats = m.categories.map((c) => {
      const e = r.categories[c.key] || {};
      const key = `room|${mood}|${c.key}`;
      const effects = [...new Set(r.lights.filter((l) => l.category === c.key).flatMap((l) => l.capabilities?.effects || []))];
      this._editorCtx[key] = { scope: "room", mood, target: c.key, area_id: r.area_id, own: e.own, inherited: e.inherited, effective: e.effective, effects };
      const open = this._ui.open === key;
      return `<div class="item ${open ? "open" : ""}"><button class="ihead" data-action="open" data-key="${esc(key)}" aria-expanded="${open}">
          <b>${esc(c.name)}</b><span>${esc(summary(e.effective))}</span>${e.own ? `<span class="pill info">room override</span>` : `<span class="pill muted">global</span>`}</button>
        ${open ? this._editorHtml(key, this._editorCtx[key]) : ""}</div>`;
    }).join("");
    const lights = r.lights.filter((l) => !l.missing && !l.excluded && !l.covered_by).map((l) => {
      const key = `light|${mood}|${l.entity_id}`;
      this._editorCtx[key] = { scope: "light", mood, target: l.entity_id, area_id: r.area_id, own: l.own, inherited: l.inherited, effective: l.effective,
        caps: l.capabilities, effects: l.capabilities?.effects || [] };
      const open = this._ui.open === key;
      return `<div class="item ${open ? "open" : ""}"><button class="ihead" data-action="open" data-key="${esc(key)}" aria-expanded="${open}">
          <b>${esc(l.name || l.entity_id)}</b><span>${esc(summary(l.effective))}</span>${l.own ? `<span class="pill info">light override</span>` : `<span class="pill muted">${esc(l.category_name || "no category")}</span>`}</button>
        ${open ? this._editorHtml(key, this._editorCtx[key]) : ""}</div>`;
    }).join("");
    return `<div class="frow"><label>Room mood</label><div class="fval"><select data-control="mood-override" ${ro ? "disabled" : ""}>${overrideOpts}</select>
        <div class="hint">A room mood replaces the global mood in this room only.</div>${this._statusHtml("mood-override")}</div></div>
      <div class="frow"><label>Edit mood</label><div class="fval"><select data-control="edit-mood">${moodOpts}</select></div></div>
      <div class="stitle">Categories in ${esc(r.name)}</div>${cats || `<div class="muted">No categories.</div>`}
      <div class="stitle">Individual lights</div>${lights || `<div class="muted">No lights.</div>`}`;
  }

  _previewTab(r) {
    const m = this._model;
    const mood = this._ui.previewMood || this._mood || m.selected_mood;
    const opts = m.moods.map((x) => `<option value="${esc(x.name)}" ${x.name === mood ? "selected" : ""}>${esc(x.name)}</option>`).join("");
    return `<div class="row-between"><div class="fval"><select data-control="preview-mood">${opts}</select>
        <button class="btn" data-action="preview">Preview</button></div>
        <span class="sub">A preview never changes a light.</span></div>
      ${this._preview?.error ? `<div class="bad">${esc(this._preview.error)}</div>` : ""}
      <div class="scroll-keep">${this._previewHtml(this._preview?.plans)}</div>`;
  }

  _controlTab(r) {
    const ro = !this._canEdit;
    const d = r.diagnostics;
    const s = r.settings;
    const review = this._ui.review;
    const active = s.control_enabled;
    const autos = review?.automations || [];
    const overrides = d.manual_overrides || [];
    const conflictsHtml = autos.length ? autos.map((a) => `<label class="auto ${a.enabled && !a.acknowledged ? "warn" : ""}">
        <input type="checkbox" data-control="ack" data-automation="${esc(a.automation)}" ${a.acknowledged ? "checked" : ""} ${ro ? "disabled" : ""}>
        <div><b>${esc(a.name || a.automation)}</b> <span class="sub">${esc(a.automation)} · ${a.enabled ? "enabled" : "disabled"}</span>
        <div class="sub">${esc(a.lights.map((l) => `${l.entity_id}: ${l.detail}`).join("; "))}</div>
        <div class="sub">${a.acknowledged ? "Acknowledged: both control these lights." : a.enabled ? "Not acknowledged: these lights are left alone while mood lighting is active." : "Disabled: no conflict while it stays disabled."}</div></div></label>`).join("")
      : `<div class="muted">No enabled or disabled automation appears to control these lights${review ? "" : " (open the review to check)"}.</div>`;
    const reviewHtml = review ? `
      <div class="stitle">Lights mood lighting will control (${review.managed.length})</div>
      ${review.managed.map((l) => `<div class="li">${esc(l.name || l.entity_id)} <span class="sub">${esc(l.entity_id)} · ${esc(l.category || "no category")}${l.discovered ? " · by label" : ""}</span></div>`).join("") || `<div class="muted">None</div>`}
      ${review.excluded.length ? `<div class="stitle">Excluded</div>${review.excluded.map((l) => `<div class="li">${esc(l.name || l.entity_id)} <span class="sub">${esc(l.reason)}</span></div>`).join("")}` : ""}
      ${review.covered_by_group.length ? `<div class="stitle">Controlled through a group</div>${review.covered_by_group.map((l) => `<div class="li">${esc(l.entity_id)} <span class="sub">via ${esc(l.group)}</span></div>`).join("")}` : ""}
      ${review.opaque_groups.length ? `<div class="note warn">Groups whose members Home Assistant cannot see: ${esc(review.opaque_groups.join(", "))}. Make sure their member lights are not managed too.</div>` : ""}
      <div class="stitle">Existing automations that may also control them</div>${conflictsHtml}
      <div class="stitle">What happens now (${esc(review.plan.mood || "no mood")})</div>${this._previewHtml([{ ...review.plan, room: review.room }])}`
      : "";
    return `<div class="control">
      ${active ? `<div class="note ok">Mood lighting is active in ${esc(r.name)} since ${fmtTime(s.activated_at)}. Existing automations are not changed or disabled.</div>
          ${ro ? "" : `<button class="btn" data-action="deactivate">Return to Observe</button>`}`
        : `<div class="note">${esc(r.name)} is in Observe mode: nothing is controlled. Review what would be controlled before enabling.</div>`}
      <div class="row-between"><button class="btn" data-action="review">${review ? "Refresh review" : "Review lights and conflicts"}</button>
        ${!active && review && !ro ? `${this._ui.confirm ? `<button class="btn primary" data-action="activate">Confirm: enable mood lighting</button>
            <button class="link" data-action="cancel-activate">Cancel</button>` : `<button class="btn primary" data-action="ask-activate">Enable mood lighting in ${esc(r.name)}</button>`}` : ""}
        ${this._statusHtml("activation")}</div>
      ${reviewHtml}
      <div class="stitle">Manual overrides</div>
      ${overrides.length ? overrides.map((o) => `<div class="li">${esc(o.entity_id)} <span class="sub">${esc(o.source)} at ${fmtTime(o.since)} · ${o.until ? `until ${fmtTime(o.until)}` : "until the room is vacant"}</span>
          ${ro ? "" : `<button class="link" data-action="clear-override" data-entity="${esc(o.entity_id)}">clear</button>`}</div>`).join("")
        : `<div class="muted">None. A light changed in the app or at a configured wall switch is left alone for a while.</div>`}
      <div class="frow"><label>Respect manual changes for</label><div class="fval">
        <input type="number" class="num" min="0" max="1440" placeholder="${esc(this._model.settings.manual_override_minutes)}" value="${s.manual_override_minutes ?? ""}" data-control="room-minutes" ${ro ? "disabled" : ""}> min
        <div class="hint">Empty: the global setting (${esc(this._model.settings.manual_override_minutes)} min). 0: until the room is vacant.</div>${this._statusHtml("room-minutes")}</div></div>
      <div class="frow"><label>Wall switch entities</label><div class="fval">
        <input type="text" value="${esc((s.manual_switches || []).join(", "))}" placeholder="event.kamar_switch_action, sensor.remote_action" data-control="switches" ${ro ? "disabled" : ""}>
        <div class="hint">A light change right after one of these changed is treated as a manual change.</div>${this._statusHtml("switches")}</div></div>
      ${this._problemsHtml(r.problems, "Room diagnostics")}
    </div>`;
  }

  async _loadReview() {
    try {
      this._ui.review = await this._ws({ type: `${WS}/room/activation`, area_id: this._config.area });
    } catch (err) {
      this._status.activation = { error: err?.message || String(err) };
    }
    this._render();
  }

  _onAction(a, el) {
    const area = this._config.area;
    if (a === "tab") { this._ui.tab = el.dataset.tab; this._render(); if (this._ui.tab === "control" && !this._ui.review) this._loadReview(); }
    else if (a === "open") { this._ui.open = this._ui.open === el.dataset.key ? null : el.dataset.key; this._render(); }
    else if (a === "toggle-info") { this._ui.showInfo = !this._ui.showInfo; this._render(); }
    else if (a === "preview") {
      this._preview = { plans: null };
      this._ws({ type: `${WS}/preview`, area_id: area, mood: this._ui.previewMood || this._mood || null })
        .then((r) => { this._preview = r; this._render(); })
        .catch((err) => { this._preview = { error: err?.message || String(err) }; this._render(); });
    } else if (a === "review") this._loadReview();
    else if (a === "ask-activate") { this._ui.confirm = true; this._render(); }
    else if (a === "cancel-activate") { this._ui.confirm = false; this._render(); }
    else if (a === "activate") {
      this._ui.confirm = false;
      const ack = (this._ui.review?.automations || []).filter((x) => x.acknowledged).map((x) => x.automation);
      this._save("activation", { type: `${WS}/room/activate`, area_id: area, confirm: true, acknowledge: ack }, "Mood lighting enabled")
        .then(() => this._loadReview());
    } else if (a === "deactivate") {
      this._save("activation", { type: `${WS}/room/deactivate`, area_id: area }, "Back in Observe mode").then(() => this._loadReview());
    } else if (a === "clear-override") {
      this._save("activation", { type: `${WS}/override/clear`, area_id: area, entity_id: el.dataset.entity }, "Override cleared");
    }
  }

  _onControl(el) {
    const area = this._config.area;
    const c = el.dataset.control;
    const s = this._model.room.settings;
    if (c === "edit-mood") { this._mood = el.value; this._fetch(); }
    else if (c === "preview-mood") { this._ui.previewMood = el.value; }
    else if (c === "mood-override") this._save("mood-override", { type: `${WS}/room/set`, area_id: area, mood_override: el.value || null });
    else if (c === "auto-discover") this._save("room", { type: `${WS}/room/set`, area_id: area, auto_discover: el.checked });
    else if (c === "exclude-light") {
      const set = new Set(s.excluded || []);
      if (el.checked) set.delete(el.dataset.entity); else set.add(el.dataset.entity);
      this._save("room", { type: `${WS}/room/set`, area_id: area, excluded: [...set] });
    } else if (c === "ack") {
      this._save("activation", { type: `${WS}/room/acknowledge`, area_id: area, automation: el.dataset.automation, acknowledged: el.checked },
        el.checked ? "Acknowledged" : "Acknowledgement withdrawn").then(() => this._loadReview());
    } else if (c === "room-minutes") {
      this._save("room-minutes", { type: `${WS}/room/set`, area_id: area, manual_override_minutes: el.value === "" ? null : Number(el.value) });
    } else if (c === "switches") {
      const ids = el.value.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
      this._save("switches", { type: `${WS}/room/set`, area_id: area, manual_switches: ids });
    }
  }
}

function lightingSensors(hass) {
  if (!hass) return [];
  return Object.values(hass.states)
    .filter((s) => s.entity_id.startsWith("sensor.") && s.attributes.integration === "smart_occupancy"
      && s.attributes.control_mode_reason !== undefined && s.attributes.area_id)
    .sort((a, b) => String(a.attributes.room).localeCompare(String(b.attributes.room)));
}

// ------------------------------------------------------ card config editors
class SoMoodEditorCardEditor extends HTMLElement {
  setConfig(config) { this._config = { ...config }; this._render(); }
  set hass(hass) { const first = !this._hass; this._hass = hass; if (first) this._render(); }
  _render() {
    if (!this._config) return;
    this.innerHTML = `${EDITOR_STYLE}<div class="so-editor"><label for="so-title">Title (optional)</label>
      <input id="so-title" value="${esc(this._config.title || "")}" placeholder="Mood lighting profiles"></div>`;
    this.querySelector("#so-title").addEventListener("change", (ev) => {
      const config = { ...this._config, title: ev.target.value };
      if (!ev.target.value) delete config.title;
      this._config = config;
      this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
    });
  }
}

class SoRoomLightingCardEditor extends HTMLElement {
  setConfig(config) { this._config = { ...config }; this._render(); }
  set hass(hass) { const first = !this._hass; this._hass = hass; if (first) this._render(); }
  _render() {
    if (!this._hass || !this._config) return;
    const rooms = lightingSensors(this._hass);
    const current = this._config.area || "";
    this.innerHTML = `${EDITOR_STYLE}<div class="so-editor">
      <label for="so-room">Room</label>
      <select id="so-room">${current ? "" : "<option value=''>Select a room</option>"}${rooms.map((s) => `<option value="${esc(s.attributes.area_id)}" ${s.attributes.area_id === current ? "selected" : ""}>${esc(s.attributes.room)}</option>`).join("")}</select>
      <label for="so-title">Title (optional)</label>
      <input id="so-title" value="${esc(this._config.title || "")}" placeholder="Room name">
      ${rooms.length ? "" : "<div>No enabled Smart Occupancy rooms found.</div>"}</div>`;
    const emit = (config) => {
      this._config = config;
      this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
    };
    this.querySelector("#so-room").addEventListener("change", (ev) => emit({ ...this._config, area: ev.target.value }));
    this.querySelector("#so-title").addEventListener("change", (ev) => {
      const config = { ...this._config, title: ev.target.value };
      if (!ev.target.value) delete config.title;
      emit(config);
    });
  }
}

const EDITOR_STYLE = `<style>
  .so-editor { display: grid; gap: 8px; padding: 8px 0; }
  .so-editor select, .so-editor input { font: inherit; padding: 8px; border-radius: 4px;
    border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); }
</style>`;

const STYLE = `<style>
  :host { display: block; }
  ha-card { container-type: inline-size; overflow: hidden; color: var(--primary-text-color); }
  .header { display: flex; align-items: center; gap: 12px; padding: 16px 16px 8px; }
  .header > ha-icon { --mdc-icon-size: 30px; color: var(--state-icon-color, var(--secondary-text-color)); }
  .header > ha-icon.ok { color: var(--state-active-color, var(--amber-color, #ffc107)); }
  .header > ha-icon.warn { color: var(--warning-color, #ff9800); }
  .titles { flex: 1; min-width: 0; }
  .title { font-size: 1.2em; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pad { padding: 0 16px 16px; display: grid; gap: 10px; }
  .sub, .hint, .src { color: var(--secondary-text-color); font-size: 0.85em; }
  .muted { color: var(--secondary-text-color); }
  .ok { color: var(--success-color, #43a047); }
  .warn-text { color: var(--warning-color, #ff9800); }
  .bad { color: var(--error-color, #db4437); }
  .badge { font-size: 0.75em; padding: 2px 10px; border-radius: 12px; white-space: nowrap; border: 1px solid var(--divider-color);
    background: var(--secondary-background-color); color: var(--secondary-text-color); }
  .badge.ok { color: var(--success-color, #43a047); border-color: currentColor; }
  .badge.warn { color: var(--warning-color, #ff9800); border-color: currentColor; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip, .tab, .btn, .ihead, .link { font: inherit; cursor: pointer; color: var(--primary-text-color); }
  .chip { padding: 4px 12px; border-radius: 16px; border: 1px solid var(--divider-color); background: var(--card-background-color); }
  .chip.on, .tab.on { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  .btn { padding: 6px 14px; border-radius: 18px; border: 1px solid var(--primary-color); background: transparent; color: var(--primary-color); }
  .btn.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .btn:disabled { opacity: 0.5; cursor: default; }
  .link { border: none; background: none; color: var(--primary-color); padding: 0 4px; text-decoration: underline; }
  .tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--divider-color); overflow-x: auto; }
  .tab { padding: 8px 14px; border: none; border-radius: 8px 8px 0 0; background: none; }
  .tabbody { display: grid; gap: 10px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 6px; }
  .kv { border: 1px solid var(--divider-color); border-radius: 8px; padding: 6px 8px; display: grid; gap: 2px; min-width: 0; }
  .kv span { font-size: 0.8em; color: var(--secondary-text-color); }
  .kv b { font-weight: 500; overflow-wrap: anywhere; }
  .kv.warn b { color: var(--warning-color, #ff9800); }
  .kv.info b { color: var(--primary-color); }
  .reason { font-size: 0.9em; color: var(--secondary-text-color); border-left: 3px solid var(--divider-color); padding-left: 8px; overflow-wrap: anywhere; }
  .note { padding: 8px 10px; border-radius: 8px; background: var(--secondary-background-color); font-size: 0.9em; }
  .note.warn { border-left: 4px solid var(--warning-color, #ff9800); }
  .note.ok { border-left: 4px solid var(--success-color, #43a047); color: var(--primary-text-color); }
  .healths { display: grid; gap: 6px; grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .health { border: 1px solid var(--divider-color); border-radius: 8px; padding: 6px 8px; min-width: 0; }
  .hhead { display: flex; align-items: center; gap: 6px; }
  .hhead ha-icon { --mdc-icon-size: 18px; }
  .hstatus { margin-left: auto; font-size: 0.85em; }
  .health.hl-ok ha-icon { color: var(--success-color, #43a047); }
  .health.hl-warn ha-icon, .health.hl-warn .hstatus { color: var(--warning-color, #ff9800); }
  .health.hl-bad ha-icon, .health.hl-bad .hstatus { color: var(--error-color, #db4437); }
  .health ul { margin: 4px 0 0; padding-left: 18px; font-size: 0.85em; overflow-wrap: anywhere; }
  .pill { display: inline-block; font-size: 0.75em; padding: 1px 8px; border-radius: 10px; border: 1px solid var(--divider-color);
    background: var(--secondary-background-color); white-space: nowrap; }
  .pill.ok { color: var(--success-color, #43a047); border-color: currentColor; }
  .pill.warn { color: var(--warning-color, #ff9800); border-color: currentColor; }
  .pill.bad { color: var(--error-color, #db4437); border-color: currentColor; }
  .pill.info { color: var(--primary-color); border-color: currentColor; }
  .pill.muted { color: var(--secondary-text-color); }
  table { width: 100%; border-collapse: collapse; font-size: 0.9em; }
  th { text-align: left; font-weight: 500; color: var(--secondary-text-color); padding: 4px 6px; border-bottom: 1px solid var(--divider-color); }
  td { padding: 6px; border-bottom: 1px solid var(--divider-color); vertical-align: top; overflow-wrap: anywhere; }
  .ctable tr { cursor: pointer; }
  .ctable tr.sel { background: var(--secondary-background-color); }
  .ptable td.why { min-width: 12em; }
  .ptable tr.row-warn td:first-child { border-left: 3px solid var(--warning-color, #ff9800); }
  .ptable tr.row-info td:first-child { border-left: 3px solid var(--primary-color); }
  .plan { display: grid; gap: 6px; margin-top: 8px; }
  .plan-head { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .section { display: grid; gap: 8px; border-top: 1px solid var(--divider-color); padding-top: 10px; }
  .section > summary { cursor: pointer; font-weight: 500; }
  .stitle { font-weight: 500; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 4px; }
  .moodinfo { display: grid; gap: 6px; }
  .editor { display: grid; gap: 6px; padding: 8px; border: 1px solid var(--divider-color); border-radius: 8px; }
  .frow { display: grid; grid-template-columns: 9em 1fr; gap: 8px; align-items: center; }
  .frow > label { color: var(--secondary-text-color); }
  .fval { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; min-width: 0; }
  .fval .hint { flex-basis: 100%; }
  select, input[type=text], input[type=number] { font: inherit; padding: 6px 8px; border-radius: 4px; max-width: 100%;
    border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); }
  input[type=text] { flex: 1; min-width: 10em; }
  input.num { width: 5.5em; }
  input[type=range] { flex: 1; min-width: 8em; accent-color: var(--primary-color); }
  input[type=checkbox] { accent-color: var(--primary-color); }
  input[type=color] { width: 3em; height: 2em; border: 1px solid var(--divider-color); border-radius: 4px; background: none; }
  .setbox { font-size: 0.85em; color: var(--secondary-text-color); display: inline-flex; gap: 4px; align-items: center; }
  .inh { color: var(--secondary-text-color); font-style: italic; }
  .erow { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: space-between; border-top: 1px dashed var(--divider-color); padding-top: 6px; }
  .status { font-size: 0.85em; }
  .status.ok { color: var(--success-color, #43a047); }
  .item { border: 1px solid var(--divider-color); border-radius: 8px; }
  .item.open { border-color: var(--primary-color); }
  .item .editor { border: none; border-top: 1px solid var(--divider-color); border-radius: 0; }
  .ihead { width: 100%; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: space-between; padding: 8px 10px;
    border: none; background: none; text-align: left; }
  .ihead span:not(.pill) { color: var(--secondary-text-color); flex: 1; text-align: right; }
  .light { display: flex; gap: 10px; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--divider-color); }
  .light > ha-icon { color: var(--state-icon-color, var(--secondary-text-color)); }
  .light > ha-icon.lit { color: var(--state-light-active-color, var(--amber-color, #ffc107)); }
  .lmain { flex: 1; min-width: 0; overflow-wrap: anywhere; }
  .switch { display: inline-flex; gap: 6px; align-items: center; font-size: 0.9em; }
  .row-between { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: space-between; }
  .auto { display: flex; gap: 8px; align-items: flex-start; padding: 6px; border-radius: 8px; border: 1px solid var(--divider-color); }
  .auto.warn { border-color: var(--warning-color, #ff9800); }
  .li { padding: 2px 0; overflow-wrap: anywhere; }
  .problems { display: grid; gap: 6px; }
  .ptitle { font-weight: 500; }
  .problem { display: flex; gap: 8px; align-items: flex-start; padding: 6px 8px; border-radius: 8px; border: 1px solid var(--divider-color); font-size: 0.9em; overflow-wrap: anywhere; }
  .problem.pr-warning ha-icon { color: var(--warning-color, #ff9800); }
  .problem.pr-error ha-icon { color: var(--error-color, #db4437); }
  .problem.pr-info ha-icon { color: var(--secondary-text-color); }
  .control { display: grid; gap: 8px; }
  @container (max-width: 640px) {
    .healths { grid-template-columns: 1fr; }
    .ptable thead { display: none; }
    .ptable tr { display: grid; grid-template-columns: 1fr 1fr; border-bottom: 1px solid var(--divider-color); padding: 4px 0; }
    .ptable td { border: none; padding: 2px 4px; }
    .ptable td::before { content: attr(data-l); display: block; font-size: 0.75em; color: var(--secondary-text-color); }
    .ptable td[data-l="Light"], .ptable td.why { grid-column: 1 / -1; }
    .ctable thead { display: none; }
    .ctable tr { display: grid; grid-template-columns: 1fr; padding: 4px 0; border-bottom: 1px solid var(--divider-color); }
    .ctable td { border: none; padding: 2px 4px; }
  }
  @container (max-width: 420px) {
    .frow { grid-template-columns: 1fr; gap: 2px; }
    .grid { grid-template-columns: 1fr 1fr; }
  }
</style>`;

if (!customElements.get(MOOD_CARD)) {
  customElements.define(MOOD_CARD, SmartOccupancyMoodEditorCard);
  customElements.define(`${MOOD_CARD}-editor`, SoMoodEditorCardEditor);
  customElements.define(ROOM_CARD, SmartOccupancyRoomLightingCard);
  customElements.define(`${ROOM_CARD}-editor`, SoRoomLightingCardEditor);
  window.customCards = window.customCards || [];
  window.customCards.push(
    {
      type: MOOD_CARD,
      name: "Smart Occupancy Global Mood Editor",
      description: "Mood lighting profiles per light category, with inheritance and a preview.",
      preview: false,
      documentationURL: "https://github.com/havanissa/HomeAssistant",
    },
    {
      type: ROOM_CARD,
      name: "Smart Occupancy Room Mood Lighting",
      description: "Room and light overrides, activation, conflicts and diagnostics for one room.",
      preview: false,
      documentationURL: "https://github.com/havanissa/HomeAssistant",
    },
  );
  console.info(`%c SMART-OCCUPANCY-LIGHTING-CARDS %c ${VERSION} `, "background:#ff9800;color:#fff", "");
}
