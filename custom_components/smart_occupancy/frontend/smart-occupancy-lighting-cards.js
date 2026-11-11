/*
 * Smart Occupancy lighting cards (served by the smart_occupancy integration).
 *
 *   type: custom:smart-occupancy-mood-editor-card     # Smart Lighting Profiles
 *   title: Smart Lighting                             # optional
 *
 *   type: custom:smart-occupancy-room-lighting-card   # Smart Room Lighting
 *   area: kamar_nordur                                # area id of an enabled room
 *
 * Simple by default: one mood, one light type, one thing at a time.
 * Everything else (inheritance, raw values, diagnostics) is under Advanced.
 *
 * The cards are editors only: the integration owns the configuration
 * (websocket API, admin-only writes). The cards never call a light service and
 * a preview never changes a light. They refetch only when the integration
 * reports a change.
 */

const VERSION = "0.3.1";
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

// Named colours for quick, fun profiles (Veistla) and readable summaries.
const SWATCHES = [
  ["Amber", [255, 150, 40]], ["Orange", [255, 100, 20]], ["Red", [255, 30, 30]], ["Pink", [255, 70, 160]],
  ["Purple", [150, 50, 255]], ["Blue", [40, 80, 255]], ["Cyan", [30, 210, 255]], ["Green", [50, 220, 80]],
];
const WHITES = [["Warm", 2700], ["Soft", 3000], ["Neutral", 4000], ["Cool", 5000]];

const hsToRgb = ([h, s]) => {
  const f = (n) => { const k = (n + h / 60) % 6; return 255 * (1 - (s / 100) * Math.max(0, Math.min(k, 4 - k, 1))); };
  return [f(5), f(3), f(1)].map(Math.round);
};
const rgbCss = (rgb) => `rgb(${(rgb || [255, 255, 255]).join(",")})`;
const kelvinCss = (k) => {
  // Rough white-point tint for the colour dot.
  const t = Math.max(0, Math.min(1, ((k || 4000) - 2000) / 4500));
  return `rgb(255,${Math.round(170 + 75 * t)},${Math.round(90 + 165 * t)})`;
};
const colourName = (rgb) => {
  if (!rgb) return "";
  let best = null;
  for (const [name, c] of SWATCHES) {
    const d = Math.hypot(c[0] - rgb[0], c[1] - rgb[1], c[2] - rgb[2]);
    if (!best || d < best[1]) best = [name, d];
  }
  return best && best[1] < 90 ? best[0] : `RGB ${rgb.join(",")}`;
};

const ACTION_WORDS = { on: "On", off: "Off", ignore: "Leave unchanged", inherit: "Not set" };

// Short, readable result: "Off", "40 % · 2700 K", "Amber · 30 %", "Not set".
// A resolved setting nobody configured (no source for its action).
const isUnset = (s) => !s || !s.action || s.action === "inherit" || (s.action === "ignore" && !(s.sources && s.sources.action));
const summary = (s) => {
  if (isUnset(s)) return "Not set";
  if (s.action === "off") return "Off";
  if (s.action === "ignore") return "Leave unchanged";
  const parts = [];
  if (s.rgb_color) parts.push(colourName(s.rgb_color));
  else if (s.hs_color) parts.push(colourName(hsToRgb(s.hs_color)));
  if (s.brightness_pct != null) parts.push(`${s.brightness_pct} %`);
  if (s.color_temp_kelvin != null) parts.push(`${s.color_temp_kelvin} K`);
  if (s.effect) parts.push(s.effect);
  return parts.length ? parts.join(" · ") : "On";
};
const dot = (s) => {
  if (!s || s.action !== "on") return `<span class="dot ${s && s.action === "off" ? "off" : "none"}"></span>`;
  const c = s.rgb_color ? rgbCss(s.rgb_color) : s.hs_color ? rgbCss(hsToRgb(s.hs_color)) : kelvinCss(s.color_temp_kelvin);
  return `<span class="dot" style="background:${c}"></span>`;
};
const sourceText = (src, mood) => {
  if (!src) return "";
  if (src.level === "global") {
    return (src.mood || "").toLowerCase() === (mood || "").toLowerCase() ? "global" : `from ${src.mood}`;
  }
  return src.level === "room" ? "room" : "this light";
};

const OCC = {
  vacant: "Vacant", possible_occupancy: "Possible occupancy", confirmed_occupancy: "Confirmed occupancy",
  possible_vacancy: "Possible vacancy", uncertain: "Uncertain",
};
const HEALTH = {
  healthy: { label: "OK", tone: "ok", icon: "mdi:check-circle" },
  warning: { label: "Warning", tone: "warn", icon: "mdi:alert" },
  unavailable: { label: "Not usable", tone: "bad", icon: "mdi:close-circle" },
  config_error: { label: "Error", tone: "bad", icon: "mdi:close-octagon" },
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
    this._status = {};
    this._ui = {};
    this._editorCtx = {};
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

  getCardSize() { return 6; }
  getGridOptions() { return { columns: 12, min_columns: 6, rows: "auto" }; }

  _liveKey() {
    const ids = this._liveEntities ? this._liveEntities() : [];
    return ids.map((e) => this._hass?.states[e]?.last_updated || "").join("|");
  }

  _requestRender() {
    if (this._renderQueued) return;
    this._renderQueued = true;
    requestAnimationFrame(() => { this._renderQueued = false; this._render(); });
  }

  _scheduleFetch() {
    clearTimeout(this._fetchTimer);
    this._fetchTimer = setTimeout(() => this._fetch(), 150);
  }

  _ws(msg) { return this._hass.callWS(msg); }

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
      setTimeout(() => { if (this._status[key]?.ok) { delete this._status[key]; this._requestRender(); } }, 2000);
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

  // ------------------------------------------------- focused profile editor
  // ctx: {scope, mood, target, area_id, own, inherited, effective, caps, title, lightsText, rgbToggle}
  _editorHtml(key, ctx) {
    const own = ctx.own || {};
    const inh = ctx.inherited || {};
    const eff = { ...inh, ...Object.fromEntries(Object.entries(own).filter(([, v]) => v != null)) };
    if (own.action === "inherit" || !own.action) eff.action = inh.action || "ignore";
    if (own.rgb_color || own.hs_color) delete eff.color_temp_kelvin;
    if (own.color_temp_kelvin) { delete eff.rgb_color; delete eff.hs_color; }
    const ro = !this._canEdit ? "disabled" : "";
    const caps = ctx.caps || {};
    const k = esc(key);
    const room = ctx.scope !== "global";
    const set = (f) => own[f] != null;
    const from = (f) => (set(f) ? "" : (inh.sources || {})[f] ? `<span class="from">${esc(sourceText(inh.sources[f], ctx.mood))}</span>` : "");
    const seg = (value, label, current) => `<button class="seg ${current ? "on" : ""}" data-ed="${k}" data-set-action="${value}" ${ro}>${esc(label)}</button>`;
    const ownAction = own.action && own.action !== "inherit" ? own.action : null;
    const inheritedLabel = ACTION_WORDS[inh.action] && inh.action !== "inherit" && inh.action !== "ignore"
      ? summary(inh) : null;
    let html = `<div class="editor" data-editor="${k}">
      <div class="ehead"><div><div class="etitle">${esc(ctx.title)}</div>
        <div class="sub">${esc(ctx.lightsText || "")}</div></div>
        <button class="btn" data-action="close-editor">Done</button></div>`;
    if (ctx.rgbToggle) {
      html += `<label class="switch rgbtoggle"><input type="checkbox" data-ed="${k}" data-rgb-toggle ${ctx.target.endsWith("+rgb") ? "checked" : ""}>
        Only the RGB-tagged lights of this type (${ctx.rgbToggle} light${ctx.rgbToggle === 1 ? "" : "s"})</label>`;
    }
    html += `<div class="segs">
      ${room || inheritedLabel ? seg("inherit", room ? `Follow ${ctx.scope === "light" ? "room" : "global"}${inheritedLabel ? ` (${inheritedLabel})` : ""}` : `Not set${inheritedLabel ? ` (${inheritedLabel})` : ""}`, !ownAction) : ""}
      ${seg("on", "On", ownAction === "on")}${seg("off", "Off", ownAction === "off")}
      ${seg("ignore", "Leave unchanged", ownAction === "ignore")}</div>`;
    if (eff.action === "on") {
      if (caps.brightness !== false) {
        const b = eff.brightness_pct ?? 100;
        html += `<div class="frow"><label>Brightness ${from("brightness_pct")}</label><div class="fval">
          <input type="range" min="1" max="100" value="${esc(b)}" data-ed="${k}" data-field="brightness_pct" ${ro}>
          <b class="val" data-out="${k}">${esc(b)} %</b></div></div>`;
      }
      const colourMode = eff.rgb_color || eff.hs_color ? "colour" : eff.color_temp_kelvin != null ? "white" : "none";
      if (caps.color_temp || caps.color) {
        const lo = caps.min_kelvin || 2200; const hi = caps.max_kelvin || 6500;
        html += `<div class="frow"><label>White ${colourMode === "white" ? from("color_temp_kelvin") : ""}</label><div class="fval">
          ${WHITES.filter(([, kv]) => kv >= lo - 100 && kv <= hi + 100).map(([n, kv]) => `<button class="chip ${colourMode === "white" && Math.abs(eff.color_temp_kelvin - kv) < 60 ? "on" : ""}" data-ed="${k}" data-kelvin="${kv}" ${ro}>
            <span class="dot" style="background:${kelvinCss(kv)}"></span>${n}</button>`).join("")}
          ${colourMode === "white" ? `<input type="range" min="${lo}" max="${hi}" step="50" value="${esc(eff.color_temp_kelvin)}" data-ed="${k}" data-field="color_temp_kelvin" ${ro}>
            <b class="val" data-out-k="${k}">${esc(eff.color_temp_kelvin)} K</b>` : ""}</div></div>`;
      }
      if (caps.color) {
        const current = eff.rgb_color || (eff.hs_color ? hsToRgb(eff.hs_color) : null);
        html += `<div class="frow"><label>Colour ${colourMode === "colour" ? from(eff.rgb_color ? "rgb_color" : "hs_color") : ""}</label><div class="fval swatches">
          ${SWATCHES.map(([n, c]) => `<button class="swatch ${current && colourName(current) === n ? "on" : ""}" title="${n}" aria-label="${n}"
            style="background:${rgbCss(c)}" data-ed="${k}" data-rgb="${c.join(",")}" ${ro}></button>`).join("")}
          <input type="color" title="Any colour" value="#${(current || [255, 150, 40]).map((x) => x.toString(16).padStart(2, "0")).join("")}" data-ed="${k}" data-field="rgb_color" ${ro}></div></div>`;
      }
      if ((caps.effects || []).length) {
        html += `<div class="frow"><label>Effect ${from("effect")}</label><div class="fval"><select data-ed="${k}" data-field="effect" ${ro}>
          <option value="">None</option>${caps.effects.map((e) => `<option ${e === eff.effect ? "selected" : ""}>${esc(e)}</option>`).join("")}</select></div></div>`;
      }
      if (caps.known && caps.brightness === false && !caps.color_temp && !caps.color) {
        html += `<div class="hint">These lights can only be switched on and off.</div>`;
      }
    }
    // Advanced: transition, ambient, inheritance details, per-field reset.
    const adv = this._ui.editorAdv;
    html += `<details class="eadv" ${adv ? "open" : ""}><summary data-action="toggle-editor-adv">Advanced</summary>
      <div class="frow"><label>Transition</label><div class="fval"><input type="number" class="num" min="0" max="300" step="0.5"
        value="${esc(own.transition ?? "")}" placeholder="${esc(inh.transition ?? "–")}" data-ed="${k}" data-field="transition" ${ro}> s</div></div>
      <div class="frow"><label>Ambient</label><div class="fval"><select data-ed="${k}" data-field="persistent" ${ro}>
        <option value="inherit" ${own.persistent == null ? "selected" : ""}>Inherit (${inh.persistent ? "yes" : "no"})</option>
        <option value="yes" ${own.persistent === true ? "selected" : ""}>Yes: follow the mood even when the room is empty</option>
        <option value="no" ${own.persistent === false ? "selected" : ""}>No: only while occupied</option></select></div></div>
      <div class="kvlist">${["action", "brightness_pct", "color_temp_kelvin", "rgb_color", "hs_color", "effect", "transition", "persistent"]
        .filter((f) => own[f] != null || inh[f] != null).map((f) => `<div><span>${esc(f)}</span>
          <b>${esc(JSON.stringify(own[f] ?? inh[f]))}</b> <span class="sub">${own[f] != null ? "set here" : `inherited · ${esc(sourceText((inh.sources || {})[f], ctx.mood))}`}</span>
          ${own[f] != null && f !== "action" && !ro ? `<button class="link" data-ed="${k}" data-clear="${f}">reset</button>` : ""}</div>`).join("")}</div>
    </details>
    <div class="erow"><span class="sub">Result: <b>${esc(summary(ctx.effective))}</b></span>
      ${ctx.own && !ro ? `<button class="link" data-action="reset" data-key="${k}">${room ? "Remove override" : `Remove from ${esc(ctx.mood)}`}</button>` : ""}
      ${this._statusHtml(key)}${ro ? `<span class="muted">Read only (administrator required)</span>` : ""}</div>
    </div>`;
    return html;
  }

  _editorUpdate(key, change, immediate = true) {
    const ctx = this._editorCtx[key];
    if (!ctx) return;
    const own = { ...(ctx.own || { action: "inherit" }) };
    change(own, ctx);
    if (own.action === "off") ["brightness_pct", "color_temp_kelvin", "rgb_color", "hs_color", "effect"].forEach((f) => delete own[f]);
    Object.keys(own).forEach((f) => (own[f] === undefined || own[f] === "") && delete own[f]);
    if (!own.action) own.action = "inherit";
    ctx.own = own;
    const empty = Object.keys(own).length === 1 && own.action === "inherit";
    const msg = { type: `${WS}/profile/set`, scope: ctx.scope, mood: ctx.mood, target: ctx.target, setting: empty ? null : own };
    if (ctx.area_id) msg.area_id = ctx.area_id;
    this._saveTimers = this._saveTimers || {};
    clearTimeout(this._saveTimers[key]);
    if (immediate) this._render();
    this._saveTimers[key] = setTimeout(() => this._save(key, msg), immediate ? 0 : 400);
  }

  _onInput(ev) {
    const el = ev.target;
    if (!el.dataset?.ed) return;
    if (el.dataset.field === "brightness_pct") {
      const out = this.shadowRoot.querySelector(`[data-out="${CSS.escape(el.dataset.ed)}"]`);
      if (out) out.textContent = `${el.value} %`;
    } else if (el.dataset.field === "color_temp_kelvin") {
      const out = this.shadowRoot.querySelector(`[data-out-k="${CSS.escape(el.dataset.ed)}"]`);
      if (out) out.textContent = `${el.value} K`;
    }
  }

  _onChange(ev) {
    const el = ev.target;
    const key = el.dataset?.ed;
    if (key) {
      const f = el.dataset.field;
      if (el.dataset.rgbToggle !== undefined) { this._switchRgb(key, el.checked); return; }
      if (f === "brightness_pct") this._editorUpdate(key, (o) => { o.brightness_pct = Math.round(Number(el.value)); if (!o.action || o.action === "inherit") o.action = "on"; }, false);
      else if (f === "color_temp_kelvin") this._editorUpdate(key, (o) => { o.color_temp_kelvin = Math.round(Number(el.value)); delete o.rgb_color; delete o.hs_color; }, false);
      else if (f === "rgb_color") {
        const m = /^#(..)(..)(..)$/.exec(el.value);
        if (m) this._editorUpdate(key, (o) => { o.rgb_color = [1, 2, 3].map((i) => parseInt(m[i], 16)); delete o.color_temp_kelvin; delete o.hs_color; });
      } else if (f === "effect") this._editorUpdate(key, (o) => { o.effect = el.value || undefined; });
      else if (f === "transition") this._editorUpdate(key, (o) => { o.transition = el.value === "" ? undefined : Number(el.value); }, false);
      else if (f === "persistent") this._editorUpdate(key, (o) => { o.persistent = el.value === "inherit" ? undefined : el.value === "yes"; });
      return;
    }
    this._onControl?.(el);
  }

  _onClick(ev) {
    const el = ev.target.closest("[data-action],[data-set-action],[data-kelvin],[data-rgb],[data-clear]");
    if (!el || el.disabled) return;
    const key = el.dataset.ed;
    if (key && el.dataset.setAction) {
      this._editorUpdate(key, (o, ctx) => {
        o.action = el.dataset.setAction;
        if (o.action === "on" && ctx.inherited?.action !== "on" && o.brightness_pct == null && ctx.caps?.brightness !== false) o.brightness_pct = 70;
        if (o.action === "inherit" && ctx.scope !== "global") Object.keys(o).forEach((f) => f !== "action" && delete o[f]);
      });
      return;
    }
    if (key && el.dataset.kelvin) {
      this._editorUpdate(key, (o) => { o.color_temp_kelvin = Number(el.dataset.kelvin); delete o.rgb_color; delete o.hs_color; if (!o.action || o.action === "inherit") o.action = "on"; });
      return;
    }
    if (key && el.dataset.rgb) {
      this._editorUpdate(key, (o) => { o.rgb_color = el.dataset.rgb.split(",").map(Number); delete o.color_temp_kelvin; delete o.hs_color; if (!o.action || o.action === "inherit") o.action = "on"; });
      return;
    }
    if (key && el.dataset.clear) {
      this._editorUpdate(key, (o) => { delete o[el.dataset.clear]; });
      return;
    }
    const a = el.dataset.action;
    if (a === "reset") {
      const ctx = this._editorCtx[el.dataset.key];
      const msg = { type: `${WS}/profile/set`, scope: ctx.scope, mood: ctx.mood, target: ctx.target, setting: null };
      if (ctx.area_id) msg.area_id = ctx.area_id;
      ctx.own = null;
      this._save(el.dataset.key, msg, "Removed");
      return;
    }
    if (a === "toggle-editor-adv") { this._ui.editorAdv = !el.parentElement.open; return; }
    if (a === "close-editor") { this._ui.edit = null; this._render(); return; }
    this._onAction?.(a, el);
  }

  _switchRgb(key, on) {
    const ctx = this._editorCtx[key];
    const base = ctx.target.replace(/\+rgb$/, "");
    this._ui.edit = { ...this._ui.edit, target: on ? `${base}+rgb` : base };
    this._render();
  }

  _previewHtml(plans, detailed) {
    if (!plans) return `<div class="muted">Loading…</div>`;
    if (!plans.length) return `<div class="muted">No enabled rooms.</div>`;
    return plans.map((p) => {
      const c = p.counts || {};
      const items = p.items.filter((i) => detailed || i.decision === "change" || i.decision === "blocked");
      const rows = items.map((i) => {
        const tone = { change: "info", unchanged: "ok", blocked: "warn", skipped: "muted" }[i.decision];
        const word = { change: p.control_enabled ? "Will change" : "Would change", unchanged: "Already right", blocked: "Blocked", skipped: "Not controlled" }[i.decision];
        const st = this._hass?.states[i.entity_id];
        return `<div class="prow"><div class="pmain">${dot(i.resolved)}
          <b>${esc(i.name || i.entity_id)}</b> <span class="sub">${esc(st ? st.state : i.current?.state || "?")} → ${esc(summary(i.resolved))}</span></div>
          <span class="pill ${tone}">${esc(word)}</span>
          ${detailed || i.decision === "blocked" ? `<div class="why">${esc(i.reason)}</div>` : ""}</div>`;
      }).join("");
      return `<div class="plan"><div class="plan-head"><b>${esc(p.room)}</b><span class="sub">${esc(p.mood || "no mood")} ·
        ${c.change || 0} change · ${c.unchanged || 0} already right · ${c.blocked || 0} blocked${p.control_enabled ? "" : " · Observe: nothing is sent"}</span></div>
        ${p.note ? `<div class="note">${esc(p.note)}</div>` : ""}${rows || `<div class="sub">Nothing to change.</div>`}</div>`;
    }).join("") + `<button class="link" data-action="preview-detail">${detailed ? "Fewer details" : "Show every light and why"}</button>`;
  }

  _problemsHtml(problems) {
    if (!problems || !problems.length) return `<div class="muted">No problems.</div>`;
    return problems.map((p) => `<div class="problem pr-${p.severity}"><b>${esc(p.title)}</b>${p.room ? ` · ${esc(p.room)}` : ""}
      <div>${esc(p.explanation)}</div><div class="sub">${esc(p.action)}</div></div>`).join("");
  }

  _render() {
    if (!this.isConnected || !this._config) return;
    if (!this._hass || (!this._model && !this._error)) { this.shadowRoot.innerHTML = `${STYLE}<ha-card><div class="pad muted">Loading…</div></ha-card>`; return; }
    if (this._error && !this._model) {
      this.shadowRoot.innerHTML = `${STYLE}<ha-card><div class="pad bad">Smart Occupancy lighting is not available: ${esc(this._error)}</div></ha-card>`;
      return;
    }
    const active = this.shadowRoot.activeElement;
    const focusSel = active && active.dataset ? Object.entries(active.dataset).map(([k, v]) => `[data-${k.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}="${CSS.escape(v)}"]`).join("") : "";
    const typing = active && active.tagName === "INPUT" && ["text", "number"].includes(active.type) ? active.value : null;
    this._editorCtx = {};
    this.shadowRoot.innerHTML = `${STYLE}<ha-card>${this._body()}</ha-card>`;
    if (focusSel) {
      const el = this.shadowRoot.querySelector(`${active.tagName.toLowerCase()}${focusSel}`);
      if (el) { if (typing !== null) el.value = typing; el.focus({ preventScroll: true }); }
    }
  }
}

// ------------------------------------------------ Card 1: Smart Lighting Profiles
class SmartOccupancyMoodEditorCard extends SoLightingBase {
  static getConfigElement() { return document.createElement(`${MOOD_CARD}-editor`); }
  static getStubConfig() { return {}; }

  setConfig(config) { this._config = { ...(config || {}) }; this._render(); }

  _request() { return { type: `${WS}/editor`, mood: this._mood || null }; }

  // Shown as rows: types with a setting in this mood (own or inherited).
  _visibleTypes() {
    // "+ RGB" parts only when they have their own setting (otherwise they repeat their type).
    return this._model.types.filter((t) => t.configured || (!t.variant && (!isUnset(t.effective) || this._ui.showUnused)));
  }

  _body() {
    const m = this._model;
    const mood = this._mood || m.selected_mood;
    const status = m.mood || {};
    const info = m.moods.find((x) => x.name === mood) || {};
    const ui = this._ui;
    let body = `<div class="header"><ha-icon icon="mdi:palette-outline"></ha-icon><div class="titles">
        <div class="title">${esc(this._config.title || "Smart Lighting")}</div>
        <div class="sub">Now: <b>${esc(status.mood || "no mood")}</b>${status.source && status.source !== "main" ? ` · ${esc(status.detail)}` : ""}</div></div></div>
      <div class="pad"><div class="chips" role="tablist">${m.moods.filter((x) => x.in_main || x.has_profile || ui.allMoods || x.name === mood)
        .map((x) => `<button class="chip ${x.name === mood ? "on" : ""}" role="tab" data-action="mood" data-mood="${esc(x.name)}">${esc(x.name)}${x.name === status.mood ? " •" : ""}</button>`).join("")}
        ${m.moods.some((x) => !x.in_main && !x.has_profile && x.name !== mood) ? `<button class="link" data-action="all-moods">${ui.allMoods ? "fewer" : "more…"}</button>` : ""}</div>`;
    if (info.name && !info.in_main) {
      body += `<div class="note warn">${esc(mood)} is not an option of Main Mood yet, so it can't become the active mood. Add it to the Main Mood helper to use it.</div>`;
    }
    if (ui.edit && ui.edit.scope === "global") {
      const t = m.types.find((x) => x.target === ui.edit.target) || this._fakeType(ui.edit.target);
      const base = m.categories.find((c) => c.key === t.key);
      const key = `global|${mood}|${t.target}`;
      const rgbCount = base && base.kind !== "rgb" ? base.rgb_lights : 0;
      this._editorCtx[key] = {
        scope: "global", mood, target: t.target, own: t.own, inherited: t.inherited, effective: t.effective,
        caps: t.variant || t.rgb ? { ...t.caps, color: true } : t.caps, title: `${mood} · ${t.name}`,
        lightsText: `${t.lights.length} light${t.lights.length === 1 ? "" : "s"}${t.lights.length ? `: ${t.lights.slice(0, 4).map((l) => l.name || l.entity_id).join(", ")}${t.lights.length > 4 ? " …" : ""}` : ""}`,
        rgbToggle: rgbCount,
      };
      return `${body}${this._editorHtml(key, this._editorCtx[key])}</div>`;
    }
    const types = this._visibleTypes();
    // Types with lights but nothing set: one compact line of chips, not a block of "not set" rows.
    const unset = m.types.filter((t) => !types.includes(t) && !t.variant && t.lights.length);
    const hidden = m.types.filter((t) => !types.includes(t) && !t.variant && !t.lights.length).length;
    body += `<div class="rows">${types.map((t) => `<button class="trow" data-action="edit" data-target="${esc(t.target)}">
        ${dot(t.effective)}<span class="tname">${esc(t.name)}<span class="sub"> · ${t.lights.length} light${t.lights.length === 1 ? "" : "s"}</span></span>
        <span class="tval ${t.effective?.action && !["inherit", "ignore"].includes(t.effective.action) ? "" : "muted"}">${esc(summary(t.effective))}${!t.configured && t.effective?.action && t.effective.action !== "ignore" ? ` <span class="from">${esc(sourceText(t.effective.sources?.action, mood))}</span>` : ""}</span>
        <ha-icon icon="mdi:chevron-right"></ha-icon></button>`).join("") || (unset.length ? "" : `<div class="note">No light types yet. Add one under Advanced → Light types.</div>`)}</div>
      ${unset.length ? `<div class="unset"><span class="sub">${types.length ? "Not set" : `Nothing set for ${esc(mood)} yet`} — tap to set:</span>
        ${unset.map((t) => `<button class="chip small" data-action="edit" data-target="${esc(t.target)}">${esc(t.name)}</button>`).join("")}</div>` : ""}
      ${hidden ? `<button class="link" data-action="unused">${ui.showUnused ? "Hide unused light types" : `Show ${hidden} unused light type${hidden === 1 ? "" : "s"}`}</button>` : ""}
      ${(m.label_suggestions || []).length && this._canEdit ? `<div class="note">${m.label_suggestions.map((s) => `Label <b>${esc(s.name)}</b> found:
        <button class="link" data-action="add-suggested" data-label="${esc(s.label_id)}" data-name="${esc(s.name)}" data-kind="${esc(s.kind)}">use it as ${s.kind === "rgb" ? "the RGB tag" : "a light function"}</button>`).join("<br>")}${this._statusHtml("suggest")}</div>` : ""}
      <div class="actions"><button class="btn" data-action="preview">${ui.preview ? "Hide preview" : "Preview"}</button>
        <button class="btn ghost" data-action="advanced">${ui.advanced ? "Hide advanced" : "Advanced"}</button></div>`;
    if (ui.preview) {
      body += `<div class="section"><div class="stitle">Preview · ${esc(mood)}</div>
        <div class="sub">What each room would do in this mood. A preview never changes a light.</div>
        ${ui.previewData?.error ? `<div class="bad">${esc(ui.previewData.error)}</div>` : this._previewHtml(ui.previewData?.plans, ui.previewDetail)}</div>`;
    }
    if (ui.advanced) body += this._advancedHtml(mood, info);
    const errors = (m.problems || []).filter((p) => p.severity === "error");
    if (errors.length && !ui.advanced) body += `<div class="note bad">${errors.length} configuration error${errors.length === 1 ? "" : "s"} · <button class="link" data-action="advanced">Review</button></div>`;
    return `${body}</div>`;
  }

  _fakeType(target) {
    const cat = this._model.categories.find((c) => c.key === target.replace(/\+rgb$/, ""));
    return { target, key: cat?.key, name: `${cat?.name || target}${target.endsWith("+rgb") ? " + RGB" : ""}`, variant: target.endsWith("+rgb"),
      kind: cat?.kind, lights: [], caps: { color: true, brightness: true, color_temp: true }, own: null, inherited: {}, effective: {} };
  }

  _advancedHtml(mood, info) {
    const m = this._model;
    const s = m.settings;
    const ro = !this._canEdit ? "disabled" : "";
    const moodOpts = (cur, none) => `<option value="">${esc(none)}</option>${m.moods.filter((x) => x.name !== mood)
      .map((x) => `<option ${x.name === cur ? "selected" : ""}>${esc(x.name)}</option>`).join("")}`;
    const kinds = { source: "Light type", function: "Function", rgb: "RGB tag" };
    const handling = info.handling;
    return `<div class="section adv">
      <div class="stitle">Mood inheritance</div>
      <div class="frow"><label>${esc(mood)} inherits from</label><div class="fval"><select data-control="parent" ${ro}>${moodOpts(info.parent, s.fallback_mood ? `Fallback (${s.fallback_mood})` : "Nothing")}</select>${this._statusHtml("parent")}</div></div>
      <div class="frow"><label>Fallback mood</label><div class="fval"><select data-control="fallback" ${ro}><option value="">None</option>
        ${m.moods.map((x) => `<option ${x.name === s.fallback_mood ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select>${this._statusHtml("fallback")}</div></div>
      ${info.name && !info.in_main ? `<div class="frow"><label>While ${esc(mood)} is selected</label><div class="fval"><select data-control="handling" ${ro}>
        <option value="follow_main" ${!handling || handling.mode === "follow_main" ? "selected" : ""}>Keep following Main Mood</option>
        <option value="no_mood" ${handling?.mode === "no_mood" ? "selected" : ""}>No mood: change nothing</option>
        <option value="use_mood" ${handling?.mode === "use_mood" ? "selected" : ""}>Use another mood</option></select>
        ${handling?.mode === "use_mood" ? `<select data-control="handling-mood" ${ro}>${moodOpts(handling.mood, "Choose…")}</select>` : ""}${this._statusHtml("handling")}</div></div>` : ""}
      <div class="stitle">Light types</div>
      <div class="sub">A light can have several labels: a light type (Spottar, Loft ljós), a function (Uppljós) and the RGB tag. Changing light types reloads the integration.</div>
      ${m.categories.map((c) => `<div class="cat"><b>${esc(c.name)}</b>
        <select data-control="cat-kind" data-key="${esc(c.key)}" ${ro}>${Object.entries(kinds).map(([v, l]) => `<option value="${v}" ${v === c.kind ? "selected" : ""}>${l}</option>`).join("")}</select>
        <select data-control="cat-label" data-key="${esc(c.key)}" ${ro}><option value="">No label</option>${m.labels.map((l) => `<option value="${esc(l.label_id)}" ${l.label_id === c.label_id ? "selected" : ""}>${esc(l.name)}</option>`).join("")}</select>
        <span class="sub">${c.lights.length} light${c.lights.length === 1 ? "" : "s"}</span>
        ${ro ? "" : `<button class="link" data-action="cat-remove" data-key="${esc(c.key)}">remove</button>`}</div>`).join("")}
      ${ro ? "" : `<div class="cat"><input type="text" placeholder="New light type, e.g. Uppljós" data-control="cat-new-name">
        <select data-control="cat-new-kind">${Object.entries(kinds).map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select>
        <select data-control="cat-new-label"><option value="">Label…</option>${m.labels.map((l) => `<option value="${esc(l.label_id)}">${esc(l.name)}</option>`).join("")}</select>
        <button class="btn" data-action="cat-add">Add</button></div>`}${this._statusHtml("cat")}
      <div class="stitle">Settings</div>
      <div class="frow"><label>Exclusion label</label><div class="fval"><select data-control="exclude" ${ro}><option value="">None</option>
        ${m.labels.map((l) => `<option value="${esc(l.label_id)}" ${l.label_id === s.exclude_label ? "selected" : ""}>${esc(l.name)}</option>`).join("")}</select>${this._statusHtml("exclude")}</div></div>
      <div class="frow"><label>Respect manual changes</label><div class="fval"><input type="number" class="num" min="0" max="1440" value="${esc(s.manual_override_minutes)}" data-control="override-minutes" ${ro}> min
        <span class="sub">0 = until the room is empty</span>${this._statusHtml("override-minutes")}</div></div>
      <div class="frow"><label>Changes without a source</label><div class="fval"><select data-control="unattributed" ${ro}>
        <option value="ignore" ${s.unattributed === "ignore" ? "selected" : ""}>Not a manual change</option>
        <option value="manual" ${s.unattributed === "manual" ? "selected" : ""}>Treat as a manual change</option></select>${this._statusHtml("unattributed")}</div></div>
      <div class="stitle">Validation</div>${this._problemsHtml(m.problems)}
    </div>`;
  }

  _onAction(a, el) {
    const ui = this._ui;
    if (a === "mood") { this._mood = el.dataset.mood; ui.edit = null; ui.previewData = null; this._fetch(); if (ui.preview) this._loadPreview(); }
    else if (a === "all-moods") { ui.allMoods = !ui.allMoods; this._render(); }
    else if (a === "edit") { ui.edit = { scope: "global", target: el.dataset.target }; this._render(); }
    else if (a === "unused") { ui.showUnused = !ui.showUnused; this._render(); }
    else if (a === "advanced") { ui.advanced = !ui.advanced; this._render(); }
    else if (a === "preview") { ui.preview = !ui.preview; if (ui.preview) this._loadPreview(); this._render(); }
    else if (a === "preview-detail") { ui.previewDetail = !ui.previewDetail; this._render(); }
    else if (a === "add-suggested") {
      this._save("suggest", { type: `${WS}/category/set`, name: el.dataset.name, label_id: el.dataset.label, kind: el.dataset.kind }, "Added");
    } else if (a === "cat-remove") {
      if (confirm("Remove this light type and its profile entries?")) this._save("cat", { type: `${WS}/category/remove`, key: el.dataset.key }, "Removed");
    } else if (a === "cat-add") {
      const root = this.shadowRoot;
      const name = root.querySelector("[data-control=cat-new-name]").value.trim();
      if (!name) return;
      this._save("cat", { type: `${WS}/category/set`, name, kind: root.querySelector("[data-control=cat-new-kind]").value,
        label_id: root.querySelector("[data-control=cat-new-label]").value || null }, "Added");
    }
  }

  _loadPreview() {
    this._ui.previewData = null;
    this._ws({ type: `${WS}/preview`, mood: this._mood }).then((r) => { this._ui.previewData = r; this._render(); })
      .catch((err) => { this._ui.previewData = { error: err?.message || String(err) }; this._render(); });
  }

  _onControl(el) {
    const c = el.dataset.control;
    const mood = this._mood;
    const cat = (key) => this._model.categories.find((x) => x.key === key);
    if (c === "parent") this._save("parent", { type: `${WS}/mood/set`, mood, parent: el.value || null });
    else if (c === "fallback") this._save("fallback", { type: `${WS}/settings/set`, fallback_mood: el.value || null });
    else if (c === "handling") {
      if (el.value === "use_mood") { this._model.moods.find((x) => x.name === mood).handling = { mode: "use_mood" }; this._render(); return; }
      this._save("handling", { type: `${WS}/settings/set`, unrepresentable: { [mood]: el.value === "follow_main" ? null : { mode: el.value } } });
    } else if (c === "handling-mood") this._save("handling", { type: `${WS}/settings/set`, unrepresentable: { [mood]: { mode: "use_mood", mood: el.value } } });
    else if (c === "exclude") this._save("exclude", { type: `${WS}/settings/set`, exclude_label: el.value || null });
    else if (c === "override-minutes") this._save("override-minutes", { type: `${WS}/settings/set`, manual_override_minutes: Number(el.value) });
    else if (c === "unattributed") this._save("unattributed", { type: `${WS}/settings/set`, unattributed: el.value });
    else if (c === "cat-kind" || c === "cat-label") {
      const x = cat(el.dataset.key);
      this._save("cat", { type: `${WS}/category/set`, key: x.key, name: x.name, kind: c === "cat-kind" ? el.value : x.kind,
        label_id: c === "cat-label" ? (el.value || null) : x.label_id });
    }
  }
}

// ------------------------------------------------ Card 2: Smart Room Lighting
class SmartOccupancyRoomLightingCard extends SoLightingBase {
  static getConfigElement() { return document.createElement(`${ROOM_CARD}-editor`); }

  static getStubConfig(hass) {
    const rooms = lightingSensors(hass);
    const pilot = rooms.find((s) => s.attributes.room === "Kamar Norður") || rooms[0];
    return { area: pilot ? pilot.attributes.area_id : "" };
  }

  setConfig(config) {
    if (!config || !config.area) throw new Error("Select a room (area)");
    if (this._config && this._config.area !== config.area) { this._model = null; this._mood = null; this._ui = {}; }
    this._config = { ...config };
    if (this._hass) this._fetch();
    this._render();
  }

  _request() { return { type: `${WS}/editor`, area_id: this._config.area, mood: this._mood || null }; }

  _liveEntities() { return (this._model?.room?.lights || []).map((l) => l.entity_id); }

  _body() {
    const r = this._model.room;
    if (!r) return `<div class="pad bad">Room ${esc(this._config.area)} not found.</div>`;
    const d = r.diagnostics;
    const active = d.control_mode === "active";
    const tab = this._ui.tab || "overview";
    const tabs = [["overview", "Overview"], ["lights", "Lights"], ["overrides", "Overrides"], ["preview", "Preview"], ["advanced", "Advanced"]];
    const content = { overview: () => this._overview(r), lights: () => this._lights(r), overrides: () => this._overrides(r),
      preview: () => this._preview(r), advanced: () => this._advanced(r) }[tab]();
    return `<div class="header"><ha-icon icon="${active ? "mdi:lightbulb-auto" : "mdi:lightbulb-outline"}" class="${active ? "lit" : ""}"></ha-icon>
        <div class="titles"><div class="title">${esc(this._config.title || r.name)}</div>
        <div class="sub">${esc(OCC[r.occupancy.state] || "Occupancy unknown")}</div></div>
        <span class="badge ${active ? "ok" : d.control_mode === "blocked" ? "warn" : ""}">${active ? "Active" : d.control_mode === "blocked" ? "Blocked" : "Observe"}</span></div>
      <div class="tabs" role="tablist">${tabs.map(([k, l]) => `<button role="tab" class="tab ${k === tab ? "on" : ""}" data-action="tab" data-tab="${k}">${l}</button>`).join("")}</div>
      <div class="pad">${content}</div>`;
  }

  _name(r, entity) {
    return (r.lights.find((l) => l.entity_id === entity) || {}).name || this._hass.states[entity]?.attributes?.friendly_name || entity;
  }

  _issues(r) {
    const out = [];
    const ro = !this._canEdit;
    for (const g of r.group_issues || []) {
      if (g.dismissed) continue;
      out.push(`<div class="issue warn"><ha-icon icon="mdi:lightbulb-group"></ha-icon><div class="imain">
        <b>${esc(this._name(r, g.group))}</b> and ${g.members.length} of ${g.members_known ? "its" : "possibly its"} lights are both managed.
        <div class="sub">How should they be controlled?</div>
        ${ro ? "" : `<div class="ibtns"><button class="btn small" data-action="group" data-group="${esc(g.group)}" data-choice="group">As a group</button>
          <button class="btn small ghost" data-action="group" data-group="${esc(g.group)}" data-choice="individual">Individually</button>
          <button class="link" data-action="group" data-group="${esc(g.group)}" data-choice="dismiss">Ignore for now</button></div>`}</div></div>`);
    }
    const conflicts = new Set((r.diagnostics.conflicts || []).filter((c) => c.enabled && !c.acknowledged).map((c) => c.automation));
    if (conflicts.size) {
      out.push(`<div class="issue warn"><ha-icon icon="mdi:robot-confused-outline"></ha-icon><div class="imain">
        <b>${conflicts.size} lighting conflict${conflicts.size === 1 ? "" : "s"}</b>${r.settings.control_enabled ? " · those lights are left alone" : ""}
        <button class="link" data-action="tab" data-tab="advanced">Review</button></div></div>`);
    }
    const h = r.health || {};
    const skip = /lighting conflict|duplicate control/i;
    for (const [area, word] of [["occupancy", "Occupancy"], ["lighting", "Lighting"], ["configuration", "Configuration"]]) {
      for (const reason of (h[area]?.reasons || []).filter((x) => !skip.test(x)).slice(0, 3)) {
        out.push(`<div class="issue ${h[area].status === "healthy" ? "" : "warn"}"><span class="ilabel">${word}</span><div class="imain">${esc(reason)}</div></div>`);
      }
    }
    return out.join("");
  }

  _overview(r) {
    const d = r.diagnostics;
    const h = r.health || {};
    const hv = (x, extra = "") => { const i = HEALTH[x?.status] || HEALTH.healthy; return `<span class="${i.tone}">${i.label}</span>${extra}`; };
    const nConf = new Set((d.conflicts || []).filter((c) => c.enabled && !c.acknowledged).map((c) => c.automation)).size;
    const issues = this._issues(r);
    return `<div class="kvs">
        <div><span>Mood</span><b>${d.follows_global ? `follows global ${esc(d.room_mood || "—")}` : `room mood ${esc(d.room_mood)}`}</b></div>
        <div><span>Mode</span><b>${d.control_mode === "active" ? "Active" : d.control_mode === "blocked" ? "Blocked" : "Observe"}</b></div>
        <div><span>Occupancy</span><b>${hv(h.occupancy)}</b></div>
        <div><span>Lighting</span><b>${hv(h.lighting, nConf ? ` <span class="sub">(${nConf} conflict${nConf === 1 ? "" : "s"})</span>` : "")}</b></div>
        <div><span>Managed lights</span><b>${d.managed_lights}</b></div>
        <div><span>Last applied</span><b>${d.last_applied_profile ? `${esc(d.last_applied_profile)} · ${fmtTime(d.last_applied_at)}` : "never"}</b></div>
      </div>
      ${issues ? `<div class="issues">${issues}</div>` : `<div class="note ok">No warnings.</div>`}
      ${this._statusHtml("group")}
      <div class="actions"><button class="btn" data-action="tab" data-tab="lights">Review lights</button>
        <button class="btn ghost" data-action="tab" data-tab="overrides">Configure overrides</button>
        <button class="btn ghost" data-action="tab" data-tab="preview">Preview room</button>
        <button class="btn ghost" data-action="tab" data-tab="advanced">Advanced</button></div>`;
  }

  // Lights grouped by light type: one line per type, details on demand.
  _lights(r) {
    const ro = !this._canEdit;
    const groups = new Map();
    for (const l of r.lights) {
      const g = l.missing ? "Not managed" : l.category_name || (l.labels?.length ? l.labels[0] : "Not categorised");
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(l);
    }
    const typeOf = (name) => r.types.find((t) => t.name === name && !t.variant);
    const detailed = this._ui.detailed;
    const rows = [...groups.entries()].map(([name, lights]) => {
      const t = typeOf(name);
      const open = detailed || this._ui.openGroup === name;
      const managed = lights.filter((l) => !l.excluded).length;
      const on = lights.filter((l) => this._hass.states[l.entity_id]?.state === "on").length;
      const list = open ? `<div class="llist">${lights.map((l) => {
        const st = this._hass.states[l.entity_id];
        const badges = [
          l.is_group ? (l.opaque_group ? "group" : `group of ${l.members.length}`) : "",
          l.rgb ? "RGB" : "", (l.labels || []).filter((x) => x !== name).join(" + "), l.covered_by ? "via group" : "",
          l.excluded ? "not managed" : "", l.override ? "manual override" : "",
          (l.conflicts || []).some((c) => c.enabled && !c.acknowledged) ? "conflict" : "",
        ].filter(Boolean);
        return `<div class="lrow"><ha-icon icon="${l.is_group ? "mdi:lightbulb-group" : "mdi:lightbulb"}" class="${st?.state === "on" ? "lit" : ""}"></ha-icon>
          <div class="lmain"><b>${esc(l.name || l.entity_id)}</b> <span class="sub">${esc(st ? st.state : "missing")}${badges.length ? ` · ${esc(badges.join(" · "))}` : ""}</span></div>
          <label class="switch"><input type="checkbox" data-control="managed" data-entity="${esc(l.entity_id)}" ${(r.settings.excluded || []).includes(l.entity_id) ? "" : "checked"} ${ro || l.missing ? "disabled" : ""}> managed</label></div>`;
      }).join("")}</div>` : "";
      return `<div class="group"><button class="trow" data-action="open-group" data-group="${esc(name)}">${t ? dot(t.effective) : `<span class="dot none"></span>`}
        <span class="tname">${esc(name)}<span class="sub"> · ${managed} light${managed === 1 ? "" : "s"}${on ? ` · ${on} on` : ""}</span></span>
        <span class="tval">${t ? `${t.follows_global ? "follows global" : "overridden"} · ${esc(summary(t.effective))}` : ""}</span>
        <ha-icon icon="mdi:chevron-${open ? "up" : "down"}"></ha-icon></button>${list}</div>`;
    }).join("");
    return `<div class="sub">Mood: ${esc(r.diagnostics.room_mood || "none")}</div><div class="rows">${rows || `<div class="note">No managed lights. Give lights in this area a light-type label.</div>`}</div>
      <label class="switch"><input type="checkbox" data-control="detailed" ${detailed ? "checked" : ""}> Show every light</label>${this._statusHtml("room")}`;
  }

  _overrides(r) {
    const m = this._model;
    const mood = this._mood || m.selected_mood;
    const ro = !this._canEdit;
    const s = r.settings;
    const ui = this._ui;
    const roomMood = `<div class="frow"><label>Room mood</label><div class="fval"><select data-control="room-mood" ${ro ? "disabled" : ""}>
        <option value="">Follow global (now ${esc(r.diagnostics.global_mood || "none")})</option>
        ${m.moods.map((x) => `<option ${x.name === s.mood_override ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select>${this._statusHtml("room-mood")}</div></div>
      <div class="frow"><label>Edit mood</label><div class="fval"><select data-control="edit-mood">${m.moods.map((x) => `<option ${x.name === mood ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select></div></div>`;
    if (ui.edit && ui.edit.scope !== "global") {
      let ctx;
      if (ui.edit.scope === "room") {
        const t = r.types.find((x) => x.target === ui.edit.target);
        if (t) {
          const cat = m.categories.find((c) => c.key === t.key);
          const rgbCount = cat && cat.kind !== "rgb" ? r.lights.filter((l) => l.rgb && (l.labels || []).includes(cat.name) && !l.excluded).length : 0;
          ctx = { scope: "room", mood, target: t.target, area_id: r.area_id, own: t.own, inherited: t.inherited, effective: t.effective,
            caps: t.variant || t.rgb ? { ...t.caps, color: true } : t.caps, title: `${mood} · ${t.name} in ${r.name}`,
            lightsText: `${t.lights.length} light${t.lights.length === 1 ? "" : "s"} in this room`, rgbToggle: rgbCount };
        }
      } else {
        const l = r.lights.find((x) => x.entity_id === ui.edit.target);
        if (l) {
          ctx = { scope: "light", mood, target: l.entity_id, area_id: r.area_id, own: l.own, inherited: l.inherited, effective: l.effective,
            caps: { ...l.capabilities, known: l.capabilities?.known }, title: `${mood} · ${l.name || l.entity_id}`, lightsText: l.entity_id };
        }
      }
      if (ctx) {
        const key = `${ctx.scope}|${mood}|${ctx.target}`;
        this._editorCtx[key] = ctx;
        return roomMood + this._editorHtml(key, ctx);
      }
    }
    const types = r.types.filter((t) => t.lights.length || t.configured);
    const lightOverrides = r.lights.filter((l) => l.own);
    const choices = r.lights.filter((l) => !l.missing && !l.excluded && !l.covered_by && !l.own);
    return `${roomMood}
      <div class="stitle">Light types in ${esc(r.name)}</div>
      <div class="rows">${types.map((t) => `<button class="trow" data-action="edit-room" data-target="${esc(t.target)}">${dot(t.effective)}
        <span class="tname">${esc(t.name)}<span class="sub"> · ${t.lights.length}</span></span>
        <span class="tval">${t.configured ? `<span class="pill info">overridden</span> ` : `<span class="from">follows global</span> `}${esc(summary(t.effective))}</span>
        <ha-icon icon="mdi:chevron-right"></ha-icon></button>`).join("") || `<div class="muted">No light types in this room.</div>`}</div>
      <div class="stitle">Single lights</div>
      <div class="rows">${lightOverrides.map((l) => `<button class="trow" data-action="edit-light" data-target="${esc(l.entity_id)}">${dot(l.effective)}
        <span class="tname">${esc(l.name || l.entity_id)}</span><span class="tval">${esc(summary(l.effective))}</span><ha-icon icon="mdi:chevron-right"></ha-icon></button>`).join("")}</div>
      ${ro || !choices.length ? "" : `<div class="fval"><select data-control="pick-light"><option value="">Override a single light…</option>
        ${choices.map((l) => `<option value="${esc(l.entity_id)}">${esc(l.name || l.entity_id)}</option>`).join("")}</select></div>`}`;
  }

  _preview(r) {
    const m = this._model;
    const mood = this._ui.previewMood || this._mood || m.selected_mood;
    if (!this._ui.previewData && !this._ui.previewLoading) this._loadPreview(mood);
    return `<div class="fval"><select data-control="preview-mood">${m.moods.map((x) => `<option ${x.name === mood ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select>
      <span class="sub">A preview never changes a light.</span></div>
      ${this._ui.previewData?.error ? `<div class="bad">${esc(this._ui.previewData.error)}</div>` : this._previewHtml(this._ui.previewData?.plans, this._ui.previewDetail)}`;
  }

  _loadPreview(mood) {
    this._ui.previewLoading = true;
    this._ws({ type: `${WS}/preview`, area_id: this._config.area, mood: mood || null })
      .then((res) => { this._ui.previewData = res; })
      .catch((err) => { this._ui.previewData = { error: err?.message || String(err) }; })
      .finally(() => { this._ui.previewLoading = false; this._requestRender(); });
  }

  _advanced(r) {
    const ro = !this._canEdit;
    const d = r.diagnostics;
    const s = r.settings;
    const review = this._ui.review;
    const active = s.control_enabled;
    if (!review && !this._ui.reviewLoading) this._loadReview();
    const autos = review?.automations || [];
    const groups = r.group_issues || [];
    return `<div class="stitle">Control</div>
      ${active ? `<div class="note ok">Mood lighting is active since ${fmtTime(s.activated_at)}. Existing automations are not changed.</div>
          ${ro ? "" : `<div><button class="btn ghost" data-action="deactivate">Return to Observe</button></div>`}`
        : `<div class="note">Observe: nothing is controlled in this room. Review below, then enable.</div>
          ${ro || !review ? "" : this._ui.confirm ? `<div><button class="btn primary" data-action="activate">Confirm: enable mood lighting</button> <button class="link" data-action="cancel">Cancel</button></div>`
            : `<div><button class="btn primary" data-action="ask-activate">Enable mood lighting in ${esc(r.name)}</button></div>`}`}
      ${this._statusHtml("activation")}
      ${review ? `<div class="sub">Will control ${review.managed.length} light${review.managed.length === 1 ? "" : "s"}: ${esc(review.managed.map((l) => l.name || l.entity_id).join(", "))}${review.excluded.length ? ` · ${review.excluded.length} not managed` : ""}</div>` : ""}
      <div class="stitle">Automations that also control these lights</div>
      ${autos.length ? autos.map((a) => `<label class="auto ${a.enabled && !a.acknowledged ? "warn" : ""}"><input type="checkbox" data-control="ack" data-automation="${esc(a.automation)}" ${a.acknowledged ? "checked" : ""} ${ro ? "disabled" : ""}>
        <div><b>${esc(a.name || a.automation)}</b> <span class="sub">${a.enabled ? "enabled" : "disabled"} · ${esc(a.lights.map((l) => l.entity_id).join(", "))}</span>
        <div class="sub">${a.acknowledged ? "Allowed: both control these lights." : a.enabled ? "Not allowed: these lights are left alone while mood lighting is active. Tick to allow both." : "Disabled: no conflict."}</div></div></label>`).join("")
        : `<div class="muted">${review ? "None found." : "Checking…"}</div>`}
      <div class="stitle">Groups</div>
      ${groups.length ? groups.map((g) => `<div class="li">${esc(this._name(r, g.group))} · ${g.members.length} light(s)${g.dismissed ? " · warning ignored" : ""}
        ${ro ? "" : `<button class="link" data-action="group" data-group="${esc(g.group)}" data-choice="group">as group</button>
        <button class="link" data-action="group" data-group="${esc(g.group)}" data-choice="individual">individually</button>`}</div>`).join("") : `<div class="muted">No overlapping groups.</div>`}
      ${(s.excluded || []).length ? `<div class="sub">Not managed: ${esc(s.excluded.map((e) => this._name(r, e)).join(", "))}</div>` : ""}
      ${this._statusHtml("group")}
      <div class="stitle">Manual overrides</div>
      ${(d.manual_overrides || []).length ? d.manual_overrides.map((o) => `<div class="li">${esc(this._name(r, o.entity_id))} <span class="sub">${esc(o.source)} · ${o.until ? `until ${fmtTime(o.until)}` : "until the room is empty"}</span>
        ${ro ? "" : `<button class="link" data-action="clear-override" data-entity="${esc(o.entity_id)}">clear</button>`}</div>`).join("") : `<div class="muted">None.</div>`}
      <div class="frow"><label>Respect manual changes</label><div class="fval"><input type="number" class="num" min="0" max="1440" placeholder="${esc(this._model.settings.manual_override_minutes)}" value="${s.manual_override_minutes ?? ""}" data-control="room-minutes" ${ro ? "disabled" : ""}> min${this._statusHtml("room-minutes")}</div></div>
      <div class="frow"><label>Wall switch entities</label><div class="fval"><input type="text" value="${esc((s.manual_switches || []).join(", "))}" placeholder="event.switch_action" data-control="switches" ${ro ? "disabled" : ""}>${this._statusHtml("switches")}</div></div>
      <label class="switch"><input type="checkbox" data-control="auto-discover" ${s.auto_discover ? "checked" : ""} ${ro ? "disabled" : ""}> Manage labelled lights in this area automatically</label>${this._statusHtml("room")}
      <div class="stitle">Last decision</div>
      <div class="sub">${esc(d.last_reason || d.control_mode_reason)}${d.last_errors?.length ? ` · ${esc(d.last_errors.join("; "))}` : ""}</div>
      <div class="stitle">All diagnostics</div>${this._problemsHtml(r.problems)}`;
  }

  async _loadReview() {
    this._ui.reviewLoading = true;
    try { this._ui.review = await this._ws({ type: `${WS}/room/activation`, area_id: this._config.area }); }
    catch (err) { this._status.activation = { error: err?.message || String(err) }; }
    this._ui.reviewLoading = false;
    this._requestRender();
  }

  _onAction(a, el) {
    const area = this._config.area;
    const ui = this._ui;
    if (a === "tab") { ui.tab = el.dataset.tab; ui.edit = null; if (ui.tab === "preview") ui.previewData = null; if (ui.tab === "advanced") ui.review = null; this._render(); }
    else if (a === "open-group") { ui.openGroup = ui.openGroup === el.dataset.group ? null : el.dataset.group; this._render(); }
    else if (a === "edit-room") { ui.edit = { scope: "room", target: el.dataset.target }; this._render(); }
    else if (a === "edit-light") { ui.edit = { scope: "light", target: el.dataset.target }; this._render(); }
    else if (a === "preview-detail") { ui.previewDetail = !ui.previewDetail; this._render(); }
    else if (a === "group") this._save("group", { type: `${WS}/room/group_choice`, area_id: area, group: el.dataset.group, choice: el.dataset.choice }, "Done");
    else if (a === "ask-activate") { ui.confirm = true; this._render(); }
    else if (a === "cancel") { ui.confirm = false; this._render(); }
    else if (a === "activate") {
      ui.confirm = false;
      const ack = (ui.review?.automations || []).filter((x) => x.acknowledged).map((x) => x.automation);
      this._save("activation", { type: `${WS}/room/activate`, area_id: area, confirm: true, acknowledge: ack }, "Mood lighting enabled").then(() => this._loadReview());
    } else if (a === "deactivate") this._save("activation", { type: `${WS}/room/deactivate`, area_id: area }, "Back in Observe").then(() => this._loadReview());
    else if (a === "clear-override") this._save("activation", { type: `${WS}/override/clear`, area_id: area, entity_id: el.dataset.entity }, "Cleared");
  }

  _onControl(el) {
    const area = this._config.area;
    const c = el.dataset.control;
    const s = this._model.room.settings;
    if (c === "edit-mood") { this._mood = el.value; this._ui.edit = null; this._fetch(); }
    else if (c === "room-mood") this._save("room-mood", { type: `${WS}/room/set`, area_id: area, mood_override: el.value || null });
    else if (c === "pick-light" && el.value) { this._ui.edit = { scope: "light", target: el.value }; this._render(); }
    else if (c === "preview-mood") { this._ui.previewMood = el.value; this._loadPreview(el.value); }
    else if (c === "detailed") { this._ui.detailed = el.checked; this._render(); }
    else if (c === "managed") {
      const set = new Set(s.excluded || []);
      if (el.checked) set.delete(el.dataset.entity); else set.add(el.dataset.entity);
      this._save("room", { type: `${WS}/room/set`, area_id: area, excluded: [...set] });
    } else if (c === "auto-discover") this._save("room", { type: `${WS}/room/set`, area_id: area, auto_discover: el.checked });
    else if (c === "ack") {
      this._save("activation", { type: `${WS}/room/acknowledge`, area_id: area, automation: el.dataset.automation, acknowledged: el.checked },
        el.checked ? "Allowed" : "No longer allowed").then(() => this._loadReview());
    } else if (c === "room-minutes") this._save("room-minutes", { type: `${WS}/room/set`, area_id: area, manual_override_minutes: el.value === "" ? null : Number(el.value) });
    else if (c === "switches") this._save("switches", { type: `${WS}/room/set`, area_id: area, manual_switches: el.value.split(/[\s,]+/).filter(Boolean) });
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
      <input id="so-title" value="${esc(this._config.title || "")}" placeholder="Smart Lighting"></div>`;
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
  .header > ha-icon { --mdc-icon-size: 28px; color: var(--state-icon-color, var(--secondary-text-color)); }
  .header > ha-icon.lit { color: var(--state-light-active-color, var(--amber-color, #ffc107)); }
  .titles { flex: 1; min-width: 0; }
  .title { font-size: 1.2em; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pad { padding: 4px 16px 16px; display: grid; gap: 10px; }
  .sub, .hint { color: var(--secondary-text-color); font-size: 0.85em; }
  .from { color: var(--secondary-text-color); font-size: 0.8em; font-weight: normal; }
  .muted { color: var(--secondary-text-color); }
  .ok { color: var(--success-color, #43a047); }
  .warn { color: var(--warning-color, #ff9800); }
  .bad { color: var(--error-color, #db4437); }
  .badge { font-size: 0.75em; padding: 2px 10px; border-radius: 12px; white-space: nowrap; border: 1px solid var(--divider-color);
    color: var(--secondary-text-color); }
  .badge.ok { color: var(--success-color, #43a047); border-color: currentColor; }
  .badge.warn { color: var(--warning-color, #ff9800); border-color: currentColor; }
  button { font: inherit; cursor: pointer; color: var(--primary-text-color); }
  button:disabled { opacity: 0.5; cursor: default; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .chip { padding: 5px 14px; border-radius: 16px; border: 1px solid var(--divider-color); background: var(--card-background-color);
    display: inline-flex; gap: 6px; align-items: center; }
  .chip.on, .tab.on, .seg.on { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  .btn { padding: 6px 16px; border-radius: 18px; border: 1px solid var(--primary-color); background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .btn.ghost { background: transparent; color: var(--primary-color); }
  .btn.small { padding: 3px 12px; font-size: 0.9em; }
  .btn.primary { font-weight: 500; }
  .link { border: none; background: none; color: var(--primary-color); padding: 0 4px; text-decoration: underline; }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; }
  .tabs { display: flex; gap: 2px; padding: 0 12px; border-bottom: 1px solid var(--divider-color); overflow-x: auto; scrollbar-width: none; }
  .tab { padding: 8px 12px; border: none; border-radius: 8px 8px 0 0; background: none; white-space: nowrap; }
  .rows { display: grid; }
  .trow { display: flex; gap: 10px; align-items: center; width: 100%; padding: 10px 4px; border: none; border-bottom: 1px solid var(--divider-color);
    background: none; text-align: left; }
  .trow:hover { background: var(--secondary-background-color); }
  .tname { flex: 1; min-width: 0; font-weight: 500; overflow-wrap: anywhere; }
  .tval { text-align: right; overflow-wrap: anywhere; }
  .trow ha-icon { --mdc-icon-size: 20px; color: var(--secondary-text-color); }
  .dot { width: 14px; height: 14px; border-radius: 50%; flex: none; display: inline-block; border: 1px solid var(--divider-color); }
  .dot.off { background: var(--disabled-color, #bdbdbd); opacity: 0.4; }
  .dot.none { background: transparent; border-style: dashed; }
  .note { padding: 8px 10px; border-radius: 8px; background: var(--secondary-background-color); font-size: 0.9em; }
  .note.warn { border-left: 4px solid var(--warning-color, #ff9800); color: var(--primary-text-color); }
  .note.ok { border-left: 4px solid var(--success-color, #43a047); color: var(--primary-text-color); }
  .note.bad { border-left: 4px solid var(--error-color, #db4437); color: var(--primary-text-color); }
  .kvs { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 16px; }
  .kvs > div { display: flex; justify-content: space-between; gap: 8px; border-bottom: 1px solid var(--divider-color); padding: 4px 0; min-width: 0; }
  .kvs span:first-child { color: var(--secondary-text-color); }
  .kvs b { font-weight: 500; text-align: right; overflow-wrap: anywhere; }
  .issues { display: grid; gap: 6px; }
  .issue { display: flex; gap: 8px; align-items: flex-start; padding: 6px 8px; border-radius: 8px; border: 1px solid var(--divider-color); font-size: 0.92em; }
  .issue.warn { border-color: var(--warning-color, #ff9800); color: var(--primary-text-color); }
  .issue.warn ha-icon { color: var(--warning-color, #ff9800); --mdc-icon-size: 20px; }
  .ilabel { font-size: 0.75em; color: var(--secondary-text-color); min-width: 6.5em; padding-top: 2px; }
  .imain { flex: 1; min-width: 0; overflow-wrap: anywhere; }
  .ibtns { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 6px; }
  .pill { display: inline-block; font-size: 0.75em; padding: 1px 8px; border-radius: 10px; border: 1px solid currentColor; white-space: nowrap; }
  .pill.ok { color: var(--success-color, #43a047); }
  .pill.warn { color: var(--warning-color, #ff9800); }
  .pill.info { color: var(--primary-color); }
  .pill.muted { color: var(--secondary-text-color); }
  .section { display: grid; gap: 8px; border-top: 1px solid var(--divider-color); padding-top: 10px; }
  .stitle { font-weight: 500; margin-top: 6px; }
  .editor { display: grid; gap: 12px; padding: 12px; border: 1px solid var(--primary-color); border-radius: 12px; }
  .ehead { display: flex; justify-content: space-between; gap: 8px; align-items: flex-start; }
  .etitle { font-size: 1.1em; font-weight: 500; }
  .segs { display: flex; flex-wrap: wrap; gap: 0; border: 1px solid var(--divider-color); border-radius: 18px; overflow: hidden; width: fit-content; max-width: 100%; }
  .seg { padding: 6px 14px; border: none; border-right: 1px solid var(--divider-color); background: none; }
  .seg:last-child { border-right: none; }
  .frow { display: grid; grid-template-columns: 7.5em 1fr; gap: 8px; align-items: center; }
  .frow > label { color: var(--secondary-text-color); font-size: 0.92em; }
  .fval { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; min-width: 0; }
  .val { min-width: 3.5em; }
  .swatch { width: 30px; height: 30px; border-radius: 50%; border: 2px solid var(--card-background-color); box-shadow: 0 0 0 1px var(--divider-color); }
  .swatch.on { box-shadow: 0 0 0 3px var(--primary-color); }
  select, input[type=text], input[type=number] { font: inherit; padding: 6px 8px; border-radius: 6px; max-width: 100%;
    border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); }
  input[type=text] { flex: 1; min-width: 9em; }
  input.num { width: 5.5em; }
  input[type=range] { flex: 1; min-width: 8em; accent-color: var(--primary-color); }
  input[type=checkbox] { accent-color: var(--primary-color); }
  input[type=color] { width: 34px; height: 30px; border: 1px solid var(--divider-color); border-radius: 6px; background: none; padding: 1px; }
  .switch { display: inline-flex; gap: 6px; align-items: center; font-size: 0.92em; }
  .eadv > summary { cursor: pointer; color: var(--secondary-text-color); font-size: 0.92em; }
  .eadv[open] { display: grid; gap: 8px; }
  .kvlist { display: grid; gap: 2px; font-size: 0.85em; }
  .kvlist span:first-child { color: var(--secondary-text-color); display: inline-block; min-width: 9em; }
  .erow { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: space-between; border-top: 1px dashed var(--divider-color); padding-top: 8px; }
  .status { font-size: 0.85em; }
  .group .llist { padding: 4px 0 8px 24px; }
  .lrow { display: flex; gap: 8px; align-items: center; padding: 4px 0; }
  .lrow ha-icon { color: var(--state-icon-color, var(--secondary-text-color)); --mdc-icon-size: 20px; }
  .lrow ha-icon.lit { color: var(--state-light-active-color, var(--amber-color, #ffc107)); }
  .lmain { flex: 1; min-width: 0; overflow-wrap: anywhere; }
  .plan { display: grid; gap: 4px; margin-top: 4px; }
  .plan-head { display: flex; flex-wrap: wrap; gap: 6px; align-items: baseline; }
  .prow { display: grid; grid-template-columns: 1fr auto; gap: 2px 8px; align-items: center; padding: 4px 0; border-bottom: 1px solid var(--divider-color); }
  .pmain { display: flex; gap: 6px; align-items: center; min-width: 0; overflow-wrap: anywhere; flex-wrap: wrap; }
  .why { grid-column: 1 / -1; font-size: 0.85em; color: var(--secondary-text-color); }
  .cat { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; padding: 4px 0; border-bottom: 1px solid var(--divider-color); }
  .cat b { min-width: 7em; }
  .auto { display: flex; gap: 8px; align-items: flex-start; padding: 6px; border-radius: 8px; border: 1px solid var(--divider-color); }
  .auto.warn { border-color: var(--warning-color, #ff9800); color: var(--primary-text-color); }
  .li { padding: 2px 0; overflow-wrap: anywhere; }
  .problem { padding: 6px 8px; border-radius: 8px; border: 1px solid var(--divider-color); font-size: 0.88em; overflow-wrap: anywhere; }
  .problem.pr-warning { border-left: 4px solid var(--warning-color, #ff9800); }
  .problem.pr-error { border-left: 4px solid var(--error-color, #db4437); }
  .rgbtoggle { padding: 4px 0; }
  .unset { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .chip.small { padding: 2px 10px; font-size: 0.88em; }
  @container (max-width: 480px) {
    .kvs { grid-template-columns: 1fr; }
    .frow { grid-template-columns: 1fr; gap: 4px; }
    .tval { max-width: 50%; }
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
      name: "Smart Lighting Profiles",
      description: "Pick a mood and see what each light type does; edit one type at a time.",
      preview: false,
      documentationURL: "https://github.com/havanissa/HomeAssistant",
    },
    {
      type: ROOM_CARD,
      name: "Smart Room Lighting",
      description: "One room: mood, mode, health, lights by type, overrides and preview.",
      preview: false,
      documentationURL: "https://github.com/havanissa/HomeAssistant",
    },
  );
  console.info(`%c SMART-OCCUPANCY-LIGHTING-CARDS %c ${VERSION} `, "background:#ff9800;color:#fff", "");
}
