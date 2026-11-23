// Automation Builder - the Lighting Designer, version 2: Truth and Brightness Matrices, Colour & Mood, Switches &
// Remotes, Advanced Rules, Priority & Resolution, simulation. Saved as soon as something changes; nothing reaches
// Home Assistant before the device is deployed. What a cell means after defaults, who controls a light now and what
// a simulation predicts all come from the backend (the same plan the compiler uses): the interface decides nothing.

import { api, ask, change, enc, entities, entityName, errorText, esc, icon, loadEntities, notes, pickEntity, toast, view } from "./ui.js";
import { capText, capsOf, matrixHtml } from "./designer.js";

const clone = (v) => JSON.parse(JSON.stringify(v));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const opt = (list, current) => list.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(current ?? "") ? "selected" : ""}>${esc(l)}</option>`).join("");
const SEP = "\u0001";
const newId = (prefix) => `${prefix}_${Math.random().toString(16).slice(2, 6).padEnd(4, "0")}`;
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

const LAYER = { advanced: "Advanced Rules", override: "Switch & remote overrides", mood: "Colour & Mood", brightness: "Brightness Matrix", truth: "Truth Matrix" };
const LAYER_HELP = {
  advanced: "“while …” rules: ON/OFF, brightness, colour, effect while their conditions hold",
  override: "a manual override started by a switch or remote: while it lasts, the layers below change nothing",
  mood: "colour temperature, colour, effect, transition, and brightness when a mood sets one",
  brightness: "brightness while the light may be on (never switches it on by itself)",
  truth: "whether the light is normally ON or OFF",
};
const PROPS = { on: "On/off", brightness: "Brightness", color: "Colour", effect: "Effect", transition: "Transition" };
const POLICY = { truth: [["priority", "An explicit ranking (Priority & Resolution)"], ["on_wins", "ON wins"], ["off_wins", "OFF wins"], ["exclusive", "Nothing while they disagree"]],
  brightness: [["priority", "An explicit ranking (Priority & Resolution)"], ["max", "The highest wins"], ["min", "The lowest wins"], ["exclusive", "Nothing while they disagree"]] };
const COMBINE = { truth: [["priority", "Explicit order (first that decides)"], ["on_wins", "ON wins"], ["off_wins", "OFF wins"]],
  brightness: [["priority", "Explicit order (first that decides)"], ["max", "The highest"], ["min", "The lowest"]] };
const ACTIONS = [["", "Nothing assigned (UNSET)"], ["none", "No action (explicitly ignored)"], ["toggle", "Toggle"], ["on", "Turn on"], ["off", "Turn off"], ["brightness", "Set brightness"], ["step", "Change brightness"],
  ["mood", "Apply a mood's settings"], ["select_mood", "Select a mood (the mood selector)"], ["override", "Start a manual override only"], ["resume", "Resume automatic control"], ["advanced", "Run an Advanced Rule"], ["dim", "Dim while held"], ["native", "Light action (data)"]];
const INTERACTION = { on: "Switched on", off: "Switched off", changed: "Switched", single: "Single press", double: "Double press", triple: "Triple press", long: "Long press", release: "Release", hold: "Hold", up: "Brightness up", down: "Brightness down", other: "Other" };

// labels in the same words as the backend
const truthLabel = (c) => (!c ? "UNSET" : { on: "ON", off: "OFF", none: "NO ACTION", default: "USE DEFAULT" }[c.kind]);
const brightLabel = (c) => (!c ? "UNSET" : c.kind === "set" ? (c.pct == null ? "…%" : `${c.pct}%`) : { none: "NO ACTION", default: "USE DEFAULT" }[c.kind]);
function attrsLabel(a) {
  const parts = [];
  if (a.brightness_pct != null) parts.push(a.brightness_pct === "keep" ? "brightness unchanged" : `${a.brightness_pct}%`);
  if (a.color === "keep") parts.push("colour unchanged");
  else if (a.color_temp_kelvin != null) parts.push(`${a.color_temp_kelvin}K`);
  else if (a.color && a.color.rgb) parts.push(`RGB ${a.color.rgb.join(",")}`);
  else if (a.color && a.color.hs) parts.push(`HS ${a.color.hs.join("/")}`);
  if (a.effect) parts.push(a.effect === "keep" ? "effect unchanged" : `effect ${a.effect}`);
  if (a.transition) parts.push(`${a.transition} s fade`);
  return parts.join(" · ");
}
const moodLabel = (c) => (!c ? "UNSET" : c.kind === "set" ? attrsLabel(c) || "(nothing set)" : { none: "NO ACTION", default: "USE DEFAULT" }[c.kind]);
const hexOf = (rgb) => "#" + (rgb || [255, 180, 107]).map((v) => Number(v).toString(16).padStart(2, "0")).join("");
const rgbOf = (value) => [1, 3, 5].map((i) => parseInt(value.slice(i, i + 2), 16));
const valueText = (v) => (v == null ? "—" : Array.isArray(v) ? v.join(",") : typeof v === "object" ? (v.ct ? `${v.ct}K` : v.rgb ? `RGB ${v.rgb.join(",")}` : v.hs ? `HS ${v.hs.join("/")}` : JSON.stringify(v))
  : v === "keep" ? "unchanged" : typeof v === "number" ? `${v}` : String(v).toUpperCase());

export async function lightingPage(deviceId, designerId, tab, current, first) {
  let data = first;
  let d = clone(data.designer.config);
  const sel = { m: null, keys: new Set(), clip: null };
  let chain = Promise.resolve();
  let sim = { inputs: null, result: null, scenario: { states: {}, event: null, time: null, weekday: null } };
  let finder = { rooms: null, area: "", devices: null, chosen: null, found: null, manual: null, target: null };
  const expanded = new Set();   // mapping ids whose "only when …" is open
  const adding = {};            // switch id -> the "+ Add Interaction" form ({interaction, kind})
  const tabs = [["lights", "Lights"], ["truth", "Truth Matrix"], ["brightness", "Brightness Matrix"], ["mood", "Colour & Mood"], ["switches", "Switches & Remotes"],
    ["advanced", "Advanced Rules"], ["priority", "Priority & Resolution"], ["simulate", "Simulate"], ["yaml", "YAML"]];
  tab = tabs.some(([k]) => k === tab) ? tab : "lights";
  const caps = () => data.capabilities || {};
  const sources = () => data.presence || [];
  const srcById = (id) => sources().find((s) => s.id === id);
  const status = (text, cls = "") => { const el = view.querySelector("#d-status"); if (el) { el.className = `save-status ${cls}`; el.textContent = text; } };
  const problemsOf = (layer) => notes((data.designer.problems || []).filter((p) => (layer ? p.layer === layer : !p.layer)).map((p) => ({ level: p.level === "blocking" ? "error" : p.level, message: p.message })));
  const resolvedOf = (key) => (r, c) => ((((data.designer.view || {}).resolved || {})[key] || {})[r] || {})[c] || null;
  await loadEntities([...data.device.targets, ...sources().map((s) => s.entity_id).filter(Boolean), d.mood.entity_id,
    ...["truth", "brightness"].flatMap((l) => d[l].matrices.flatMap((m) => m.columns.map((c) => c.entity_id))), ...d.switches.map((w) => w.entity_id)].filter(Boolean));

  // ------------------------------------------------------------ saving
  function incomplete() {
    if (d.brightness.matrices.some((m) => Object.values(m.cells).some((row) => Object.values(row).some((c) => c.kind === "set" && c.pct == null)))) return "Type a brightness (0–100 %) to save";
    const open = [...d.advanced.flatMap((r) => r.conditions.items), ...d.switches.flatMap((w) => w.mappings.flatMap((m) => (m.conditions || { items: [] }).items))].find((c) => (c.type === "state" && !c.state) || (c.type === "numeric" && c.above == null && c.below == null)
      || (c.type === "time" && !c.after && !c.before && !(c.weekdays || []).length));
    if (open) return "Complete the new condition (a state, a limit or a time) to save";
    const actions = [...d.switches.flatMap((w) => w.mappings.map((m) => m.action)), ...d.advanced.flatMap((r) => r.actions)];
    const half = actions.find((a) => ((a.kind === "mood" || a.kind === "select_mood") && !a.value) || (a.kind === "advanced" && !a.rule)
      || (a.kind === "dim" && (!a.direction || !a.release)));
    return half ? `Choose ${half.kind === "advanced" ? "the Advanced Rule" : half.kind === "dim" ? "the direction and the release" : "the mood"} to save` : "";
  }
  function save() {
    const missing = incomplete();
    if (missing) { status(missing); return chain; }
    status("Saving…");
    chain = chain.then(async () => {
      const sent = clone(d);
      try {
        const result = await change("api/designers/save", { device_id: deviceId, designer: sent });
        if (!current()) return;
        data = result;
        if (JSON.stringify(sent) === JSON.stringify(d)) d = clone(result.designer.config);
        draw();
        status(result.blocking ? `Saved · ${plural(result.blocking, "thing")} to resolve before deploying` : "Saved · deploy the device to apply it", result.blocking ? "" : "ok");
      } catch (err) {
        status(`Not saved: ${errorText(err)}`, "bad");
      }
    });
    return chain;
  }

  // ------------------------------------------------------------ pieces
  const get = (path) => path.split("/").reduce((o, k) => (o == null ? o : o[k]), d);
  function set(path, value) { const keys = path.split("/"); const last = keys.pop(); const holder = keys.reduce((o, k) => o[k], d); holder[last] = value; }
  const entityBtn = (path, entityId, purpose, label = "Choose…") => `<button type="button" class="entity ${entityId ? "" : "unset"}" data-act="pick" data-path="${esc(path)}" data-purpose="${esc(purpose)}" title="${esc(entityId || "")}">${entityId ? esc(entityName(entityId)) : `UNSET · ${esc(label)}`}</button>`;
  const toggle = (path, on, text, help = "") => `<label class="switch"><input type="checkbox" data-chg="flag" data-path="${esc(path)}" ${on ? "checked" : ""}> ${esc(text)}</label>${help ? `<div class="help">${esc(help)}</div>` : ""}`;
  const num = (path, value, attrs = "", unit = "") => `<input type="number" step="any" data-chg="num" data-path="${esc(path)}" value="${esc(value ?? "")}" placeholder="unset" ${attrs}>${unit ? ` ${esc(unit)}` : ""}`;
  const txt = (path, value, attrs = "") => `<input type="text" data-chg="text" data-path="${esc(path)}" value="${esc(value ?? "")}" ${attrs}>`;
  const live = (entityId) => { const e = entities[entityId]; return e ? `<span class="badge">now: ${esc(e.state ?? "?")}${e.unit ? ` ${esc(e.unit)}` : ""}</span>` : ""; };
  const lightBoxes = (path, chosen) => `<span class="days">${d.lights.map((l) => `<label><input type="checkbox" data-chg="lightset" data-path="${esc(path)}" value="${esc(l)}" ${chosen.includes(l) ? "checked" : ""}>${esc(entityName(l))}</label>`).join("") || `<span class="muted">Choose the lights first (Lights).</span>`}</span>`;

  function columnLabel(c) {
    if (c.label) return c.label;
    if (c.kind === "presence") { const s = srcById(c.source); return s ? `${s.name} ${({ occupied: "occupied", vacant: "vacant", possible: "possibly occupied" })[c.state]}` : "(missing source)"; }
    if (c.kind === "state") return `${c.entity_id ? entityName(c.entity_id) : "…"} is ${c.state ?? "…"}`;
    if (c.kind === "numeric") return `${c.entity_id ? entityName(c.entity_id) : "…"} ${c.from != null && c.below != null ? `${c.from}–${c.below}` : c.below != null ? `below ${c.below}` : c.from != null ? `from ${c.from}` : "…"}`;
    if (c.kind === "time") return `${(c.after || "00:00").slice(0, 5)}–${(c.before || "24:00").slice(0, 5)}${c.weekdays.length ? ` ${c.weekdays.join(",")}` : ""}`;
    return "Always";
  }

  // ------------------------------------------------------------ light-level matrices
  const levelRoom = (m) => (data.levels || []).find((r) => r.area_id === (m.level || {}).room) || null;
  function levelCols(m) {
    const fromView = ((((data.designer.view || {}).resolved || {})[m.id]) || {}).__columns;
    if (fromView) return fromView;
    const room = levelRoom(m);
    return room ? room.bands.map((b) => ({ id: b.id, label: b.name })) : [];
  }
  function levelColumns(m) {
    const room = levelRoom(m);
    if (!room) return `<div class="notice error">This matrix uses the light level of a room that has none anymore (${esc((m.level || {}).room || "?")}). Delete the matrix, or configure the room under <a href="#/light-level">Light Level</a>.</div>`;
    return `<p class="small">Columns: the light levels of <a href="#/light-level/rooms/${enc(room.area_id)}">${esc(room.name)}</a>${room.template ? ` (template “${esc(room.template)}”)` : ""} · now: <strong>${esc((room.bands.find((b) => b.id === room.band) || {}).name || room.band || "no level yet")}</strong>
      <br><span class="muted">A cell without its own value shows the room's value (<em>room light level</em>) or the global default matrix's (<em>global default matrix</em>). Setting a cell here overrides it for this light only; “Set to Unset” returns it to the inherited value.</span></p>`;
  }

  // ------------------------------------------------------------ matrices
  function matrixSpec(id) {
    const rows = d.lights.map((r) => ({ key: r, label: entityName(r), sub: r }));
    if (id === "mood") {
      const m = d.mood;
      return { id, kind: "mood", title: "Colour & Mood: the settings of each light in each mood", rows, label: moodLabel,
        cols: m.values.map((v) => ({ key: v, label: v })), view: resolvedOf("mood"),
        get: (r, c) => ((m.cells[r] || {})[c]) || null,
        set: (r, c, cell) => { m.cells[r] = m.cells[r] || {}; if (cell) m.cells[r][c] = cell; else delete m.cells[r][c]; if (!Object.keys(m.cells[r]).length) delete m.cells[r]; },
        rowDefaults: { get: (r) => m.row_defaults[r] || null, set: (r, cell) => { if (cell) m.row_defaults[r] = cell; else delete m.row_defaults[r]; } },
        matrixDefault: { get: () => m.default, set: (cell) => { m.default = cell; } } };
    }
    const [layer, mid] = id.split(":");
    const m = d[layer].matrices.find((x) => x.id === mid);
    if (!m) return null;
    return { id, kind: layer, title: `${m.name}: ${layer === "truth" ? "ON or OFF in each situation" : "the brightness in each situation"}`, rows,
      label: layer === "truth" ? truthLabel : brightLabel, view: resolvedOf(mid),
      cols: m.level ? levelCols(m).map((c) => ({ key: c.id, label: c.label, sub: "light level" }))
        : m.columns.map((c) => ({ key: c.id, label: columnLabel(c), sub: { presence: "presence", state: "state", numeric: "range", time: "time", always: "" }[c.kind] })),
      get: (r, c) => ((m.cells[r] || {})[c]) || null,
      set: (r, c, cell) => { m.cells[r] = m.cells[r] || {}; if (cell) m.cells[r][c] = cell; else delete m.cells[r][c]; if (!Object.keys(m.cells[r]).length) delete m.cells[r]; },
      rowDefaults: { get: (r) => m.row_defaults[r] || null, set: (r, cell) => { if (cell) m.row_defaults[r] = cell; else delete m.row_defaults[r]; } },
      matrixDefault: { get: () => m.default, set: (cell) => { m.default = cell; } } };
  }
  const selected = () => [...sel.keys].map((k) => k.split(SEP));
  const getCell = (spec, r, c) => (r === "__matrix" ? spec.matrixDefault.get() : c === "__row" ? spec.rowDefaults.get(r) : spec.get(r, c));
  function setCell(spec, r, c, cell) {
    if (cell && cell.kind === "default" && (c === "__row" || r === "__matrix")) return;
    if (r === "__matrix") spec.matrixDefault.set(cell); else if (c === "__row") spec.rowDefaults.set(r, cell); else spec.set(r, c, cell);
  }
  function cellEditor(spec) {
    if (sel.m !== spec.id || !sel.keys.size) return `<p class="muted small">Select one or more cells to set them. Copy and paste work on any selection.</p>`;
    const items = selected();
    const cell = getCell(spec, items[0][0], items[0][1]);
    const defaults = items.some(([r, c]) => c === "__row" || r === "__matrix");
    const kind = cell ? cell.kind : "unset";
    const base = spec.kind === "truth" ? [["unset", "Unset"], ["default", "Use default"], ["none", "No action"], ["on", "ON"], ["off", "OFF"]]
      : spec.kind === "brightness" ? [["unset", "Unset"], ["default", "Use default"], ["none", "No action"], ["set", "Brightness"]]
        : [["unset", "Unset"], ["default", "Use default"], ["none", "No action"], ["set", "Settings"]];
    const kinds = base.filter(([k]) => !(defaults && k === "default"));
    let body = "";
    if (spec.kind === "brightness" && kind === "set") {
      body = `<div class="cfield"><span class="flabel">Brightness</span><input type="number" min="0" max="100" value="${esc(cell.pct ?? "")}" placeholder="—" data-chg="cval" data-key="pct" aria-label="Brightness percent"> %
        ${cell.pct != null ? `<input type="range" min="0" max="100" value="${cell.pct}" data-chg="cval" data-key="pct" aria-label="Brightness">` : ""}</div>
        ${cell.pct === 0 ? `<p class="small">0 % is never an OFF by itself: its meaning is chosen below the matrices.</p>` : ""}`;
    }
    if (spec.kind === "mood" && kind === "set") {
      const rows = [...new Set(items.map(([r]) => r).filter((r) => r !== "__matrix"))];
      body = attrsFields(cell, capsOf(rows.length ? rows : d.lights, caps()), "cval", false);
    }
    return `<div class="cell-editor"><div class="kinds" role="radiogroup" aria-label="What this cell says">${kinds.map(([k, l]) => `<label class="kind ${k === kind ? "on" : ""}"><input type="radio" name="ckind" value="${k}" data-chg="ckind" ${k === kind ? "checked" : ""}>${esc(l)}</label>`).join("")}</div>
      ${body ? `<div class="cfields">${body}</div>` : ""}${items.length > 1 ? `<p class="muted small">Changes apply to every selected cell.</p>` : ""}</div>`;
  }
  // light attributes (a mood cell or an Advanced Rule): empty = unchanged; "keep" only for rules
  function attrsFields(a, cap, chg, keep) {
    const unset = (key) => `<button type="button" class="link small" data-act="${chg === "cval" ? "cunset" : "aunset"}" data-key="${key}" ${chg !== "cval" ? `data-path="${esc(chg)}"` : ""}>unchanged</button>`;
    const keepBox = (key) => (keep ? ` <label class="small"><input type="checkbox" data-chg="keep" data-path="${esc(chg)}" data-key="${key}" ${a[key] === "keep" ? "checked" : ""}> NO ACTION (keep)</label>` : "");
    const data = chg === "cval" ? `data-chg="cval"` : `data-chg="attr" data-path="${esc(chg)}"`;
    const field = (key, label, html) => `<div class="cfield"><span class="flabel">${esc(label)}</span>${html}${a[key] != null && a[key] !== "keep" ? ` ${unset(key)}` : ` <span class="muted small">${a[key] === "keep" ? "" : "unset"}</span>`}${keepBox(key)}</div>`;
    let out = "";
    if (cap.brightness || !cap.known) out += field("brightness_pct", "Brightness", a.brightness_pct === "keep" ? "" : `<input type="number" min="1" max="100" value="${esc(a.brightness_pct ?? "")}" ${data} data-key="brightness_pct" aria-label="Brightness percent"> %`);
    if (cap.color_temp || !cap.known) out += field("color_temp_kelvin", "Colour temperature", a.color === "keep" ? "" : `<input type="number" min="${cap.min_kelvin}" max="${cap.max_kelvin}" step="50" value="${esc(a.color_temp_kelvin ?? "")}" ${data} data-key="color_temp_kelvin" aria-label="Colour temperature"> K`);
    if (cap.color || !cap.known) out += field("color", "Colour", a.color === "keep" ? "" : `<input type="color" value="${hexOf(a.color && a.color.rgb)}" ${data} data-key="color" aria-label="Colour" class="${a.color ? "" : "unchosen"}">`);
    if (cap.effect && cap.effects.length) out += field("effect", "Effect", a.effect === "keep" ? "" : `<select ${data} data-key="effect" aria-label="Effect"><option value="">—</option>${opt(cap.effects.map((e) => [e, e]), a.effect)}</select>`);
    out += `<div class="cfield"><span class="flabel">Transition</span><input type="number" min="0" max="300" step="0.5" value="${esc(a.transition ?? "")}" ${data} data-key="transition" aria-label="Transition seconds"> s</div>`;
    return out;
  }
  const matrixBlock = (id) => { const spec = matrixSpec(id); return spec ? `${matrixHtml(spec, sel)}<div class="editor-box">${cellEditor(spec)}</div>` : ""; };

  function columnsEditor(layer, m) {
    const pres = sources().flatMap((s) => (s.kind === "room" ? ["occupied", "vacant", "possible"] : ["occupied", "vacant"]).map((st) => [`${s.id}:${st}`, `${s.name} ${st === "possible" ? "possibly occupied" : st}${s.area ? ` (${s.area})` : ""}`]));
    const base = `${layer}/matrices/${d[layer].matrices.indexOf(m)}`;
    const items = m.columns.map((c, i) => {
      const p = `${base}/columns/${i}`;
      let detail = "";
      if (c.kind === "state") detail = `${entityBtn(`${p}/entity_id`, c.entity_id, "any", "an entity")} is ${txt(`${p}/state`, c.state, 'placeholder="state" aria-label="State" list="sugg-state"')} for ${num(`${p}/for_seconds`, c.for_seconds || null, 'min="0" aria-label="Held for"', "s")} ${live(c.entity_id)}`;
      if (c.kind === "numeric") detail = `${entityBtn(`${p}/entity_id`, c.entity_id, "numeric", "a sensor")} from ${num(`${p}/from`, c.from, 'aria-label="From"')} below ${num(`${p}/below`, c.below, 'aria-label="Below"')} delay ${num(`${p}/for_seconds`, c.for_seconds || null, 'min="0" aria-label="Delay"', "s")} ${live(c.entity_id)}`;
      if (c.kind === "time") detail = `from <input type="time" data-chg="time" data-path="${p}/after" value="${esc((c.after || "").slice(0, 5))}" aria-label="From"> until <input type="time" data-chg="time" data-path="${p}/before" value="${esc((c.before || "").slice(0, 5))}" aria-label="Until">
        <span class="days">${DAYS.map((day) => `<label><input type="checkbox" data-chg="wday" data-path="${p}/weekdays" value="${day}" ${c.weekdays.includes(day) ? "checked" : ""}>${day}</label>`).join("")}</span>`;
      if (c.kind === "presence") detail = `<span class="muted small">${esc((srcById(c.source) || {}).area || "")}</span>`;
      return `<li class="row" data-col="${esc(c.id)}"><strong>${esc(columnLabel(c))}</strong> ${detail}<span class="spacer"></span>
        ${txt(`${p}/label`, c.label, 'placeholder="name (optional)" maxlength="60" aria-label="Column name"')}
        <button type="button" class="link danger-text" data-act="coldrop" data-layer="${layer}" data-m="${esc(m.id)}" data-i="${i}">Remove</button></li>`;
    }).join("");
    return `<ol class="plain cols">${items || `<li class="muted">No columns yet.</li>`}</ol>
      <div class="row"><select id="add-${esc(m.id)}" aria-label="Column to add"><option value="">Add a column…</option>
        <optgroup label="Presence and occupancy">${opt(pres, "")}</optgroup>
        <optgroup label="Other sources"><option value="numeric">Light level or another number (a range)</option><option value="state">An entity's state (a switch, mood selector, media player …)</option><option value="time">A time of day / weekdays</option><option value="always">Always</option></optgroup></select>
        <button type="button" data-act="coladd" data-layer="${layer}" data-m="${esc(m.id)}">Add</button><a class="button" href="#/presence">Presence sources…</a></div>
      <datalist id="sugg-state">${["on", "off", "playing", "paused", "idle", "home", "not_home"].map((s) => `<option value="${s}">`).join("")}</datalist>`;
  }

  function layerTab(layer) {
    const L = d[layer];
    const clashes = (data.designer.view || {}).clashes || {};
    const cards = L.matrices.map((m, i) => {
      const base = `${layer}/matrices/${i}`;
      const overlap = (clashes[m.id] || []).length;
      return `<div class="card matrix-card" data-matrix-card="${esc(m.id)}"><div class="row"><h2>${txt(`${base}/name`, m.name, 'maxlength="40" aria-label="Matrix name"')}</h2>
          <label class="switch"><input type="checkbox" data-chg="flag" data-path="${base}/enabled" ${m.enabled ? "checked" : ""}> On</label><span class="spacer"></span>
          <button type="button" class="link danger-text" data-act="mdrop" data-layer="${layer}" data-i="${i}">Delete the matrix</button></div>
        ${m.level ? levelColumns(m) : `<h3>Columns (situations)</h3>${columnsEditor(layer, m)}`}
        ${!m.level && m.columns.length > 1 && overlap ? `<div class="field"><label for="pol-${esc(m.id)}">When columns hold at the same time</label><select id="pol-${esc(m.id)}" data-chg="text" data-path="${base}/policy"><option value="">Choose… (needed when they want different things)</option>${opt(POLICY[layer], m.policy)}</select>
          <div class="help">${plural(overlap, "pair")} of columns can hold together (for example two zones occupied at once). The order of the columns decides nothing.${m.policy === "priority" ? " Rank them under Priority & Resolution." : ""}</div></div>` : ""}
        ${(m.level ? levelCols(m).length : m.columns.length) && d.lights.length ? matrixBlock(`${layer}:${m.id}`) : `<p class="muted">${m.level ? "The room's light level has no template yet." : "Choose lights and add columns to get the matrix."}</p>`}</div>`;
    }).join("");
    const rooms = data.levels || [];
    const levelAdd = `<div class="row"><select id="lvl-room-${layer}" aria-label="Room of the light level"><option value="">A room's light level…</option>${opt(rooms.map((r) => [r.area_id, `${r.name}${r.template ? ` (${r.template})` : " (no template yet)"}`]), "")}</select>
        <button type="button" data-act="ladd" data-layer="${layer}" ${rooms.length && L.matrices.length < 4 ? "" : "disabled"}>Add a Light Level matrix</button>
        ${rooms.length ? "" : `<span class="muted small">No room has a light level yet: <a href="#/light-level">Light Level</a>.</span>`}</div>`;
    const intro = layer === "truth"
      ? "A Truth Matrix decides whether a light is normally ON or OFF. Cells: ON, OFF, NO ACTION (leave it), USE DEFAULT, or UNSET (this matrix does not decide)."
      : "A Brightness Matrix gives the brightness while the light may be on. It never switches a light on by itself, and never overrides an OFF.";
    return `<div class="card"><p class="small">${intro}</p>${problemsOf(layer)}
        <div class="actions"><button type="button" data-act="madd" data-layer="${layer}" ${L.matrices.length < 4 ? "" : "disabled"}>Add a ${layer === "truth" ? "Truth" : "Brightness"} Matrix</button></div>
        ${levelAdd}<p class="muted small">A Light Level matrix uses the light levels of a room (Light Level page): its columns are the room's levels, and its cells come from the room (or the global default matrix the room adopted) until you set a light's own value here.</p>
        ${layer === "brightness" ? `<div class="field"><label for="zero">0 % means</label><select id="zero" data-chg="text" data-path="brightness/zero"><option value="">Choose… (needed when a cell is 0 %)</option>${opt([["off", "Switch the light off"], ["minimum", "The lowest brightness (1 %)"], ["unchanged", "Leave the brightness as it is"]], d.brightness.zero)}</select></div>` : ""}</div>${cards}`;
  }

  // ------------------------------------------------------------ tabs
  function lightsTab() {
    const lights = data.device.targets.filter((t) => t.startsWith("light."));
    const roles = d.priority.map((l, i) => `<div class="role"><strong>${i + 1}. ${esc(LAYER[l])}</strong><span class="small">${esc(LAYER_HELP[l])}</span></div>`).join(`<span class="arrow" aria-hidden="true">›</span>`);
    return `<div class="card"><h2>Lights (rows)</h2>
        ${lights.map((l) => `<label class="pick"><input type="checkbox" data-chg="row" data-id="${esc(l)}" ${d.lights.includes(l) ? "checked" : ""}>${icon("light")}<span>${esc(entityName(l))}<br><span class="mono">${esc(l)}</span></span><span class="spacer"></span><span class="muted small">${esc(capText(caps()[l]))}</span></label>`).join("")}
        <p class="muted small">Only the lights ticked here take part. Each gets an automation of its own in Home Assistant.</p></div>
      <div class="card"><h2>How a light is controlled</h2><p class="small">Each property is decided on its own, by the first layer that decides it (highest first). Change the order under Priority &amp; Resolution.</p>
        <div class="roles">${roles}</div>
        <p class="small">OFF from the resolution switches the light off; ON switches it on with the resolved settings; otherwise an ON light gets the resolved brightness and colour and an OFF light stays off. When an Advanced Rule or an override ends, the light returns to what the matrices say <em>now</em>.</p>
        <div class="field"><label>Reconciliation</label><span><select data-chg="numsel" data-path="reconcile_minutes" aria-label="Reconciliation"><option value="">Only when an input changes</option>${opt([1, 2, 5, 10, 15, 30].map((m) => [m, `Also every ${m} min`]), d.reconcile_minutes)}</select></span>
          <div class="help">A light is evaluated when one of its inputs changes (not when it is deployed). A periodic evaluation also corrects a lost command; it changes nothing that is already right.</div></div>
        <div class="field"><label>A light comes back</label>${toggle("on_available", d.on_available, "Apply the calculated state when an unavailable light is available again", "Off: a light that was without power (for example by a wall switch) is left as it comes back.")}</div>
        ${problemsOf(null)}
        <p class="muted small">${plural(data.designer.rules.length, "generated rule")} · ${plural((data.automations || []).length, "automation")}. <a href="#/devices/${enc(deviceId)}">Review and deploy on the device page</a></p></div>`;
  }

  function moodTab() {
    const m = d.mood;
    const options = ((entities[m.entity_id] || {}).attributes || {}).options || [];
    return `<div class="card"><div class="field"><label>Colour &amp; Mood</label>${toggle("mood/enabled", m.enabled, "On", "Optional. A mood never switches a light on by itself; its settings apply while the light is on.")}</div>
        <div class="field"><label>Mood source</label><span>${entityBtn("mood/entity_id", m.entity_id, "mood", "the mood selector")} ${live(m.entity_id)}</span></div>
        <div class="field"><label>Moods (columns)</label><span class="days">${[...new Set([...options, ...m.values])].map((o) => `<label><input type="checkbox" data-chg="value" value="${esc(o)}" ${m.values.includes(o) ? "checked" : ""}>${esc(o)}</label>`).join("") || `<span class="muted">Choose the source to see its options.</span>`}</span></div>
        <div class="field"><label>Automatic mode</label><span>${m.auto ? `“${esc(m.auto.value)}” reads the effective mood from ${entityBtn("mood/auto/entity_id", m.auto.entity_id, "mood", "the effective mood")} <button type="button" class="link" data-act="noauto">remove</button>`
    : `<select data-chg="auto"><option value="">None</option>${opt(options.filter((o) => !m.values.includes(o)).map((o) => [o, `“${o}” is a mode, not a mood`]), "")}</select>`}</span></div>
        ${problemsOf("mood")}</div>
      <div class="card">${m.values.length && d.lights.length ? matrixBlock("mood") : `<p class="muted">Choose lights and moods to get the matrix.</p>`}</div>`;
  }

  function switchesTab() {
    const o = d.override;
    const cards = d.switches.map((w, wi) => {
      const evName = (i) => `${i.label || INTERACTION[i.kind]}${i.button && !(i.label || "").includes(i.button) ? ` · ${i.button}` : ""}`;
      const uses = (i) => w.mappings.filter((m) => m.interaction === i.id).length;
      const events = w.interactions.map((i, ii) => `<li class="row" data-interaction="${esc(i.id)}"><span>${esc(evName(i))} <span class="badge">${esc(INTERACTION[i.kind])}</span> <span class="muted small">${esc(i.trigger ? i.trigger.type : "?")}${uses(i) ? ` · ${plural(uses(i), "action")}` : " · nothing assigned"}</span></span><span class="spacer"></span>
          <button type="button" class="link small danger-text" data-act="idrop" data-w="${wi}" data-ii="${ii}" aria-label="Remove the event ${esc(evName(i))}">remove</button></li>`).join("");
      const rows = w.mappings.map((m, mi) => {
        const p = `switches/${wi}/mappings/${mi}`;
        const a = m.action;
        const i = w.interactions.find((x) => x.id === m.interaction);
        let params = "";
        if (a.kind === "brightness") params = num(`${p}/action/pct`, a.pct, 'min="1" max="100" aria-label="Brightness"', "%");
        if (a.kind === "step") params = num(`${p}/action/pct`, a.pct, 'min="-100" max="100" aria-label="Change"', "% (negative = darker)");
        if (a.kind === "mood" || a.kind === "select_mood") params = `<select data-chg="text" data-path="${p}/action/value" aria-label="Mood"><option value="">mood…</option>${opt([...new Set([...d.mood.values, ...(((entities[d.mood.entity_id] || {}).attributes || {}).options || [])])].map((v) => [v, v]), a.value)}</select>`;
        if (a.kind === "advanced") params = `<select data-chg="text" data-path="${p}/action/rule" aria-label="Advanced Rule"><option value="">rule…</option>${opt(d.advanced.filter((r) => r.kind === "when").map((r) => [r.id, r.name]), a.rule)}</select>`;
        if (a.kind === "dim") params = `<select data-chg="text" data-path="${p}/action/direction" aria-label="Direction"><option value="">direction…</option>${opt([["up", "brighter"], ["down", "darker"]], a.direction)}</select>
          ${num(`${p}/action/step`, a.step, 'min="1" max="50" aria-label="Step"', "% every")} ${num(`${p}/action/interval`, a.interval, 'min="1" max="5" aria-label="Interval"', "s until")}
          <select data-chg="text" data-path="${p}/action/release" aria-label="Release"><option value="">the release…</option>${opt(w.interactions.filter((x) => x.id !== m.interaction).map((x) => [x.id, evName(x)]), a.release)}</select>`;
        if (a.kind === "native") params = `<select data-chg="text" data-path="${p}/action/action" aria-label="Light action">${opt([["light.turn_on", "light.turn_on"], ["light.turn_off", "light.turn_off"], ["light.toggle", "light.toggle"]], a.action)}</select>
          <input type="text" data-chg="json" data-path="${p}/action/data" value="${esc(JSON.stringify(a.data || {}))}" aria-label="Data (JSON)">`;
        const conds = (m.conditions || { items: [] }).items;
        const open = expanded.has(m.id) || conds.length > 0 && expanded.has(`${m.id}:c`);
        const condRows = conds.map((c, j) => `<li class="row">${condEdit(c, `${p}/conditions/items/${j}`)}<span class="spacer"></span><button type="button" class="link small danger-text" data-act="xcdrop" data-w="${wi}" data-mi="${mi}" data-j="${j}">remove</button></li>`).join("");
        const more = open ? `<div class="mapping-more"><div class="field"><label>Only when</label><span><select data-chg="xctype" data-w="${wi}" data-mi="${mi}" aria-label="Combine">${opt([["and", "all of these hold"], ["or", "any of these holds"]], (m.conditions || {}).type || "and")}</select></span></div>
            <ul class="plain">${condRows || `<li class="muted small">No condition: it acts whenever the event happens.</li>`}</ul>
            <div class="row"><select id="mcnew-${wi}-${mi}" aria-label="Condition to add"><option value="">Add a condition…</option><option value="state">An entity is in a state</option><option value="numeric">A number is above / below</option><option value="time">A time of day</option></select><button type="button" data-act="xcadd" data-w="${wi}" data-mi="${mi}">Add</button></div></div>` : "";
        return `<li class="mapping-row${m.enabled ? "" : " disabled"}" data-mapping="${esc(m.id)}" data-interaction="${esc(m.interaction || "")}">
          <div class="row wrap"><span class="badge">${mi + 1}</span><label class="small"><input type="checkbox" data-chg="flag" data-path="${p}/enabled" ${m.enabled ? "checked" : ""} aria-label="Enabled"> on</label>
            <select data-chg="xint" data-w="${wi}" data-mi="${mi}" aria-label="Event">${i ? "" : `<option value="">event…</option>`}${opt(w.interactions.map((x) => [x.id, evName(x)]), m.interaction)}</select> →
            <select data-chg="maction" data-w="${wi}" data-mi="${mi}" aria-label="Action">${opt(ACTIONS.filter(([k]) => k !== ""), a.kind)}</select> ${params}</div>
          <div class="row wrap">${!["none", "select_mood"].includes(a.kind) ? `<span class="small muted">Lights:</span> ${lightBoxes(`${p}/lights`, m.lights)}` : ""}
            ${!["override", "resume", "dim", "none", "select_mood"].includes(a.kind) ? `<label class="small"><input type="checkbox" data-chg="flag" data-path="${p}/override" ${m.override ? "checked" : ""}> and start a manual override</label>` : a.kind === "override" ? `<span class="small">starts an override</span>` : ""}
            <span class="spacer"></span>
            <button type="button" class="link small" data-act="xmore" data-id="${esc(m.id)}" aria-expanded="${open}">Only when${conds.length ? ` (${conds.length})` : ""}…</button>
            <button type="button" class="link" data-act="xmove" data-w="${wi}" data-mi="${mi}" data-dir="-1" ${mi ? "" : "disabled"} aria-label="Move up">↑</button><button type="button" class="link" data-act="xmove" data-w="${wi}" data-mi="${mi}" data-dir="1" ${mi < w.mappings.length - 1 ? "" : "disabled"} aria-label="Move down">↓</button>
            <button type="button" class="link danger-text" data-act="xdrop" data-w="${wi}" data-mi="${mi}" aria-label="Delete this interaction">✕</button></div>${more}</li>`;
      }).join("");
      const form = adding[w.id];
      const addForm = form ? `<div class="row wrap add-mapping"><select data-chg="xnew" data-w="${esc(w.id)}" data-key="interaction" aria-label="Event of the new interaction"><option value="">event…</option>${opt(w.interactions.map((x) => [x.id, evName(x)]), form.interaction)}</select> →
          <select data-chg="xnew" data-w="${esc(w.id)}" data-key="kind" aria-label="Action of the new interaction"><option value="">action…</option>${opt(ACTIONS.filter(([k]) => k !== ""), form.kind)}</select>
          <button type="button" class="primary" data-act="xcreate" data-w="${wi}" ${form.interaction && form.kind ? "" : "disabled"}>Add</button><button type="button" class="link" data-act="xcancel" data-w="${esc(w.id)}">Cancel</button></div>` : "";
      return `<div class="card switch-card" data-switch="${esc(w.id)}"><div class="row"><h2>${icon(w.kind === "latched" ? "switch" : "event")} ${txt(`switches/${wi}/name`, w.name, 'maxlength="60" aria-label="Name"')}</h2>
          <span class="badge">${w.kind === "latched" ? "latched switch (stays on/off)" : "momentary button or remote"}</span><span class="spacer"></span>
          <button type="button" class="link danger-text" data-act="wdrop" data-w="${wi}">Remove</button></div>
        <p class="muted small">${w.entity_id ? esc(w.entity_id) : ""}${w.kind === "latched" && w.entity_id ? ` · also usable as a matrix source: add a column “${esc(entityName(w.entity_id))} is on”` : ""}</p>
        ${w.kind === "latched" ? `<div class="field"><label>Feedback</label>${toggle(`switches/${wi}/ignore_automations`, w.ignore_automations, "Ignore changes of this switch made by an automation", "When the switch follows the light (or another automation sets it), that change is not a press. A change by hand, in the app or on the device still counts.")}</div>` : ""}
        <h3>Events of this control</h3><ul class="plain events">${events || `<li class="muted">No events yet.</li>`}</ul>
        <div class="row">${w.ha_device_id || w.entity_id ? `<button type="button" data-act="fmore" data-w="${wi}">Find more events of this device…</button>` : ""}<button type="button" data-act="fmanual" data-w="${wi}">Add an event by hand…</button></div>
        <h3>Interactions</h3><p class="muted small">Each line is one interaction: an event, what it does, and to which lights. The same event and the same light can be used in as many lines as you need (for example a single press turns Light1 on and a double press turns it off). Lines run in this order.</p>
        <ol class="plain mappings">${rows || `<li class="muted">No interactions: nothing is assigned for you.</li>`}</ol>${addForm}
        <div class="actions"><button type="button" data-act="iadd" data-w="${esc(w.id)}" ${w.interactions.length ? "" : "disabled"}>+ Add Interaction</button></div>
        <p class="muted small">Without “start a manual override” an action is one-shot: the next evaluation applies the matrices again.</p></div>`;
    }).join("");
    const overridden = d.switches.some((w) => w.mappings.some((m) => m.override || m.action.kind === "override")) || o.manual;
    const policy = `<div class="card" id="override-policy"><h2>Manual override</h2>
        <p class="small">While a light's override lasts, the layers below it in the priority change nothing; when it ends the light returns to the calculated baseline. The state is kept in Home Assistant helpers that Automation Builder creates when you deploy (they survive restarts; the App does not need to run).</p>
        <div class="field"><label for="opolicy">An override lasts</label><select id="opolicy" data-chg="text" data-path="override/policy"><option value="">Choose…${overridden ? " (needed)" : ""}</option>${opt([["until_resumed", "Until “resume” (or the helper is turned off)"], ["duration", "A time"], ["until_vacant", "Until a room or zone is vacant"], ["until_event", "Until an event"]], o.policy)}</select></div>
        ${o.policy === "duration" ? `<div class="field"><label>Duration</label><span>${num("override/minutes", o.minutes, 'min="1" max="1440"', "min")}</span></div>` : ""}
        ${o.policy === "until_vacant" ? `<div class="field"><label for="osrc">Vacancy of</label><select id="osrc" data-chg="text" data-path="override/source"><option value="">Choose…</option>${opt(sources().map((s) => [s.id, s.name]), o.source)}</select></div>` : ""}
        ${o.policy === "until_event" ? `<div class="field"><label>Ends</label><span>${(o.release || []).map((t, i) => `<span class="badge">${esc(releaseText(t))}</span> <button type="button" class="link small" data-act="reldrop" data-i="${i}">remove</button>`).join(" ")}
          <input type="time" id="rel-at" aria-label="At"><button type="button" data-act="reladd">Add a time</button>
          <select id="rel-int" aria-label="Interaction"><option value="">or an interaction of a switch…</option>${opt(d.switches.flatMap((w) => w.interactions.filter((i) => i.trigger).map((i) => [i.id, `${w.name}: ${i.label || INTERACTION[i.kind]}`])), "")}</select><button type="button" data-act="relint">Add</button></span></div>` : ""}
        <div class="field"><label>Changes by hand</label>${toggle("override/manual", o.manual, "Also start an override when a light is switched on or off by something that is not an automation", "Home Assistant's context tells: a change by an automation has a parent; a change in the app, by voice or on the device has none. Only on/off changes count (not attribute reports). A smart bulb powered by a wall switch reports unavailable instead: that is not detected.")}</div>
        ${(data.helpers || []).length ? `<h3>Helpers</h3><ul class="plain small">${data.helpers.map((h) => `<li><span class="mono">${esc(h.entity_id)}</span> · ${esc(h.purpose)} · ${h.exists ? `exists${h.state ? ` (${esc(h.state)})` : ""}${h.owned ? ", created by Automation Builder" : ", <strong>not created by Automation Builder</strong>"}` : "created when you deploy"}</li>`).join("")}</ul>` : ""}
        ${problemsOf("switches")}</div>`;
    const adoptable = (data.adoptable || []).map((r) => `<li class="row" data-adopt="${esc(r.rule_id)}"><span><strong>${esc(r.name)}</strong><br><span class="small muted">${esc(r.trigger)} → ${esc(r.action.kind)} ${esc(r.lights.map(entityName).join(", "))}</span></span><span class="spacer"></span><button type="button" data-act="adopt" data-id="${esc(r.rule_id)}">Move here</button></li>`).join("");
    const adopt = adoptable ? `<div class="card" id="adoptable"><h2>Button rules of this device</h2><p class="small">These ordinary rules can be expressed exactly as switch actions. <em>Move here</em> replaces the rule by an interaction and its action (nothing runs twice); other rules stay as they are.</p><ul class="plain">${adoptable}</ul></div>` : "";
    return `${finderCard()}${adopt}${cards}${policy}`;
  }
  function releaseText(t) {
    if (t.type === "time") return `at ${t.at.slice(0, 5)}`;
    for (const w of d.switches) for (const i of w.interactions) if (JSON.stringify(i.trigger) === JSON.stringify(t)) return `${w.name}: ${i.label || INTERACTION[i.kind]}`;
    return t.type === "state" ? `${t.entity_id} → ${t.to}` : t.type;
  }

  function finderCard() {
    const f = finder;
    const rooms = f.rooms ? `<select data-chg="froom" aria-label="Room"><option value="">Room…</option>${opt([...f.rooms.map((r) => [r.area_id ?? "__none", r.name]), ["*", "Every room"]], f.area)}</select>` : `<button type="button" data-act="frooms">Add a switch or remote…</button>`;
    const devices = f.devices ? `<ul class="plain">${f.devices.map((dv, i) => `<li class="row"><strong>${esc(dv.name)}</strong> <span class="muted small">${esc([dv.area, dv.model].filter(Boolean).join(" · "))}</span><span class="spacer"></span>
        ${dv.device_id ? `<button type="button" data-act="fpick" data-i="${i}">Its device triggers</button>` : ""}${dv.entities.map((e) => `<button type="button" data-act="fpick" data-i="${i}" data-entity="${esc(e.entity_id)}">${esc(e.name)} <span class="mono">${esc(e.entity_id)}</span></button>`).join("")}</li>`).join("") || `<li class="muted">Nothing here.</li>`}</ul>` : "";
    const into = f.target != null ? d.switches[f.target] : null;
    const known = (i) => Boolean(into && into.interactions.some((x) => JSON.stringify(x.trigger) === JSON.stringify(i.trigger)));
    const found = f.found ? `<div class="notice info"><strong>${esc(f.chosen.label)}</strong> · ${esc(f.found.kind === "latched" ? "a latched switch" : f.found.kind ? "a momentary button or remote" : "nothing discovered")}
        ${f.found.interactions.map((i, n) => `<label class="pick"><input type="checkbox" data-chg="fint" data-n="${n}" ${i.take ? "checked" : ""} ${known(i) ? "disabled" : ""}><span>${esc(i.label)} <span class="badge">${esc(INTERACTION[i.kind])}</span> <span class="muted small">${esc(i.source)}${i.button ? ` · ${esc(i.button)}` : ""}${known(i) ? " · added already" : ""}</span></span></label>`).join("")}
        ${f.found.notes.map((n) => `<p class="small">${esc(n)}</p>`).join("")}
        <div class="actions"><button class="primary" data-act="fadd" ${f.found.interactions.some((i) => i.take) ? "" : "disabled"}>${into ? `Add the ticked events to “${esc(into.name)}”` : "Add with the ticked events"}</button>${into ? `<button type="button" class="link" data-act="fclose">Cancel</button>` : ""}</div></div>` : "";
    const manual = f.manual ? `<div class="cfields"><select data-chg="fman" data-key="type" aria-label="Kind">${opt([["zha", "ZHA event (zha_event)"], ["mqtt", "Zigbee2MQTT / MQTT message"], ["event", "Another event"]], f.manual.type)}</select>
        ${f.manual.type === "zha" ? `<input type="text" data-chg="fman" data-key="ieee" value="${esc(f.manual.ieee || "")}" placeholder="device IEEE (00:11:…)" aria-label="Device IEEE"> <input type="text" data-chg="fman" data-key="command" value="${esc(f.manual.command || "")}" placeholder="command (on, toggle …)" aria-label="Command">` : ""}
        ${f.manual.type === "mqtt" ? `<input type="text" data-chg="fman" data-key="topic" value="${esc(f.manual.topic || "")}" placeholder="zigbee2mqtt/<name>/action" aria-label="Topic"> <input type="text" data-chg="fman" data-key="payload" value="${esc(f.manual.payload || "")}" placeholder="payload (single, double …)" aria-label="Payload">` : ""}
        ${f.manual.type === "event" ? `<input type="text" data-chg="fman" data-key="event_type" value="${esc(f.manual.event_type || "")}" placeholder="event type" aria-label="Event type"> <input type="text" data-chg="fman" data-key="data" value="${esc(f.manual.data || "{}")}" placeholder='{"key": "value"}' aria-label="Event data">` : ""}
        <select data-chg="fman" data-key="kind" aria-label="Interaction">${opt(Object.entries(INTERACTION), f.manual.kind)}</select>
        <button type="button" data-act="fmanadd">${f.manual.target != null && d.switches[f.manual.target] ? `Add this event to “${esc(d.switches[f.manual.target].name)}”` : "Add this event"}</button><button type="button" class="link" data-act="fclose">Cancel</button></div>` : "";
    return `<div class="card" id="finder"><h2>Switches &amp; Remotes</h2><p class="small">Room → device → entity or event. Only what Home Assistant offers for it is listed; anything else can be mapped by hand.</p>
      <div class="row">${rooms}${f.rooms ? `<button type="button" data-act="fmanual">Manual event…</button>` : ""}</div>${devices}${found}${manual}</div>`;
  }

  function advancedTab() {
    const rules = d.advanced.map((r, i) => {
      const p = `advanced/${i}`;
      const conds = r.conditions.items.map((c, j) => `<li class="row">${condEdit(c, `${p}/conditions/items/${j}`)}<span class="spacer"></span><button type="button" class="link small danger-text" data-act="cdrop" data-r="${i}" data-j="${j}">remove</button></li>`).join("");
      const body = r.kind === "while"
        ? `<div class="field"><label>On/off</label><select data-chg="truthset" data-r="${i}" aria-label="On or off">${opt([["", "UNSET"], ["on", "ON"], ["off", "OFF"], ["keep", "NO ACTION (keep)"]], r.set.truth)}</select></div>
           <div class="cfields">${attrsFields(r.set, capsOf(r.lights.length ? r.lights : d.lights, caps()), `${p}/set`, true)}</div>`
        : `<div class="field"><label>When</label><span>${r.triggers.map((t, j) => `<span class="badge">${esc(t.type === "time" ? `at ${t.at.slice(0, 5)}` : t.type === "state" ? `${t.entity_id} → ${t.to}` : t.type)}</span> <button type="button" class="link small" data-act="tdrop" data-r="${i}" data-j="${j}">remove</button>`).join(" ")}
             <input type="time" id="tat-${i}" aria-label="At"><button type="button" data-act="tadd" data-r="${i}">Add a time</button></span><div class="help">Or run it from a switch (Switches &amp; Remotes → “Run an Advanced Rule”).</div></div>
           <div class="field"><label>Then</label><span><select data-chg="wact" data-r="${i}" aria-label="Action">${opt(ACTIONS.filter(([k]) => ["", "toggle", "on", "off", "brightness", "step", "mood", "resume"].includes(k)), (r.actions[0] || {}).kind || "")}</select>
             ${(r.actions[0] || {}).kind === "brightness" ? num(`${p}/actions/0/pct`, r.actions[0].pct, 'min="1" max="100"', "%") : ""}${(r.actions[0] || {}).kind === "step" ? num(`${p}/actions/0/pct`, r.actions[0].pct, 'min="-100" max="100"', "%") : ""}
             ${(r.actions[0] || {}).kind === "mood" ? `<select data-chg="text" data-path="${p}/actions/0/value"><option value="">mood…</option>${opt(d.mood.values.map((v) => [v, v]), r.actions[0].value)}</select>` : ""}</span></div>`;
      return `<div class="card adv" data-rule="${esc(r.id)}"><div class="row"><span class="badge">${i + 1}</span><h2>${txt(`${p}/name`, r.name, 'maxlength="60" aria-label="Rule name"')}</h2>
          <span class="badge">${r.kind === "while" ? "while … (conditional override)" : "when … (one-shot)"}</span>
          <label class="switch"><input type="checkbox" data-chg="flag" data-path="${p}/enabled" ${r.enabled ? "checked" : ""}> On</label><span class="spacer"></span>
          <button type="button" class="link" data-act="rmove" data-i="${i}" data-dir="-1" ${i ? "" : "disabled"} aria-label="Higher priority">↑</button><button type="button" class="link" data-act="rmove" data-i="${i}" data-dir="1" ${i < d.advanced.length - 1 ? "" : "disabled"} aria-label="Lower priority">↓</button>
          <button type="button" class="link danger-text" data-act="rdrop" data-i="${i}">Delete</button></div>
        <div class="field"><label>Lights</label>${lightBoxes(`${p}/lights`, r.lights)}</div>
        <div class="field"><label>${r.kind === "while" ? "While" : "Only if"}</label><span><select data-chg="ctype" data-r="${i}" aria-label="Combine">${opt([["and", "all of these hold"], ["or", "any of these holds"]], r.conditions.type)}</select></span></div>
        <ul class="plain">${conds || `<li class="muted">No condition.</li>`}</ul>
        <div class="row"><select id="cnew-${i}" aria-label="Condition to add"><option value="">Add a condition…</option><option value="state">An entity is in a state</option><option value="numeric">A number is above / below</option><option value="time">A time of day</option>${sources().filter((s) => s.entity_id).map((s) => `<option value="src:${esc(s.id)}">${esc(s.name)} occupied</option>`).join("")}</select><button type="button" data-act="cadd" data-r="${i}">Add</button></div>
        <h3>${r.kind === "while" ? "Sets while active" : "Does"}</h3>${body}</div>`;
    }).join("");
    const ordinary = (data.ordinary_rules || []).map((r) => `<li><a href="#/rules/${enc(deviceId)}/${enc(r.id)}">${esc(r.name)}</a>${r.enabled ? "" : ` <span class="badge">disabled</span>`}<br><span class="small muted">${esc(r.summary)}</span></li>`).join("");
    return `<div class="card"><p class="small">Advanced Rules are exceptions to the matrices. A “while” rule sets properties while its conditions hold and releases them by itself (the light returns to the calculated baseline). A “when” rule acts once at its moment. Their order (1 = highest) is their priority among themselves.</p>
        <div class="actions"><button type="button" data-act="radd" data-kind="while">Add a “while …” rule</button><button type="button" data-act="radd" data-kind="when">Add a “when …” rule</button></div>${problemsOf("advanced")}</div>${rules}
      <div class="card"><h2>Ordinary rules of this device (IF / AND-OR / THEN)</h2><p class="muted small">The generic rule editor, for anything the designer does not cover. Ordinary rules act on their own; they are outside this priority (the conflict check reports when both can act at once).</p>
        <ul class="plain">${ordinary || `<li class="muted">None.</li>`}</ul><div class="actions"><a class="button" href="#/rules/${enc(deviceId)}/new">Write an ordinary rule</a></div></div>`;
  }
  function condEdit(c, cp) {
    if (c.type === "state") return `${entityBtn(`${cp}/entity_id`, c.entity_id, "any")} <label class="small"><input type="checkbox" data-chg="flag" data-path="${cp}/negate" ${c.negate ? "checked" : ""}> is not</label> ${txt(`${cp}/state`, c.state, 'placeholder="state" aria-label="State" list="sugg-state"')} ${live(c.entity_id)}`;
    if (c.type === "numeric") return `${entityBtn(`${cp}/entity_id`, c.entity_id, "numeric")} above ${num(`${cp}/above`, c.above, 'aria-label="Above"')} below ${num(`${cp}/below`, c.below, 'aria-label="Below"')} ${live(c.entity_id)}`;
    if (c.type === "time") return `time from <input type="time" data-chg="time" data-path="${cp}/after" value="${esc((c.after || "").slice(0, 5))}" aria-label="From"> until <input type="time" data-chg="time" data-path="${cp}/before" value="${esc((c.before || "").slice(0, 5))}" aria-label="Until">`;
    return esc(condText(c));
  }
  function condText(c) {
    if (c.type === "state") return `${entityName(c.entity_id)}${c.attribute ? `.${c.attribute}` : ""} is ${c.negate ? "not " : ""}${c.state}${c.for_seconds ? ` for ${c.for_seconds} s` : ""}`;
    if (c.type === "numeric") return `${entityName(c.entity_id)}${c.above != null ? ` above ${c.above}` : ""}${c.below != null ? ` below ${c.below}` : ""}`;
    if (c.type === "time") return `time ${(c.after || "00:00").slice(0, 5)}–${(c.before || "24:00").slice(0, 5)}`;
    return c.type;
  }

  function priorityTab() {
    const order = d.priority.map((l, i) => `<li class="row" data-layer="${l}"><span class="badge">${i + 1}</span><span><strong>${esc(LAYER[l])}</strong><br><span class="small muted">${esc(LAYER_HELP[l])}</span></span><span class="spacer"></span>
        <button type="button" class="link" data-act="pmove" data-i="${i}" data-dir="-1" ${i ? "" : "disabled"} aria-label="Higher">↑</button><button type="button" class="link" data-act="pmove" data-i="${i}" data-dir="1" ${i < d.priority.length - 1 ? "" : "disabled"} aria-label="Lower">↓</button></li>`).join("");
    const combine = ["truth", "brightness"].filter((l) => d[l].matrices.length > 1).map((l) => `<div class="field"><label for="comb-${l}">${esc(LAYER[l])}: several matrices</label><select id="comb-${l}" data-chg="text" data-path="${l}/combine"><option value="">Choose…</option>${opt(COMBINE[l], d[l].combine)}</select>
        <div class="help">Order of the matrices (explicit): ${d[l].matrices.map((m, i) => `${i + 1}. ${esc(m.name)}`).join(", ")} <span>${d[l].matrices.map((m, i) => (i ? `<button type="button" class="link small" data-act="mup" data-layer="${l}" data-i="${i}">move “${esc(m.name)}” up</button>` : "")).join("")}</span></div></div>`).join("");
    const ranking = ["truth", "brightness"].flatMap((l) => d[l].matrices.filter((m) => m.policy === "priority").map((m) => {
      const ranked = m.ranks.map((id, i) => `<li class="row"><span class="badge">${i + 1}</span>${esc(columnLabel(m.columns.find((c) => c.id === id) || {}))}<span class="spacer"></span>
          <button type="button" class="link" data-act="kmove" data-layer="${l}" data-m="${esc(m.id)}" data-i="${i}" data-dir="-1" ${i ? "" : "disabled"} aria-label="Higher">↑</button><button type="button" class="link" data-act="kmove" data-layer="${l}" data-m="${esc(m.id)}" data-i="${i}" data-dir="1" ${i < m.ranks.length - 1 ? "" : "disabled"} aria-label="Lower">↓</button>
          <button type="button" class="link small" data-act="kdrop" data-layer="${l}" data-m="${esc(m.id)}" data-i="${i}">unrank</button></li>`).join("");
      const open = m.columns.filter((c) => !m.ranks.includes(c.id)).map((c) => `<button type="button" class="link small" data-act="kadd" data-layer="${l}" data-m="${esc(m.id)}" data-c="${esc(c.id)}">rank “${esc(columnLabel(c))}”</button>`).join(" ");
      return `<div class="card" data-ranking="${esc(m.id)}"><h3>${esc(LAYER[l])} “${esc(m.name)}”: ranking of its columns</h3><ol class="plain">${ranked || `<li class="muted">Nothing ranked yet.</li>`}</ol><p class="small">${open || ""}</p></div>`;
    })).join("");
    const lv = data.live || {};
    const rows = (lv.lights || []).filter((l) => l.controlled).map((l) => `<tr data-light="${esc(l.entity_id)}"><th scope="row">${esc(l.name)}<br><span class="muted small">${esc(l.state || "?")}</span></th>
        ${Object.keys(PROPS).map((p) => `<td>${l.resolved[p] ? `<strong>${esc(valueText(l.resolved[p][0]))}</strong><br><span class="small muted">${esc(l.resolved[p][1])}</span>` : `<span class="muted">—</span>`}</td>`).join("")}
        <td>${l.override === true ? `<span class="badge warn">active</span>` : l.override === false ? "no" : "—"}</td>
        <td class="small">${l.command.service ? `${esc(l.command.service)} ${esc(JSON.stringify(l.command.data || {}))}` : esc(l.command.why || "nothing")}</td></tr>`).join("");
    return `<div class="card"><h2>Priority (highest first)</h2><p class="small">Resolution is per property: a layer only competes for what it can set. Truth gating stays: brightness and colour never switch a light on, and a 0 % is never an OFF by itself.</p>
        <ol class="plain" id="priority">${order}</ol>${combine}</div>${ranking}
      <div class="card" id="control"><h2>Who controls each light now</h2>${lv.available ? `<div class="tscroll"><table><thead><tr><th>Light</th>${Object.values(PROPS).map((p) => `<th>${p}</th>`).join("")}<th>Override</th><th>Its automation would send</th></tr></thead><tbody>${rows || `<tr><td colspan="8" class="muted">Nothing controlled yet.</td></tr>`}</tbody></table></div>
        <p class="muted small">Calculated from Home Assistant's states at ${esc((lv.at || "").slice(11, 19))} UTC, with the deployed rules' meaning (deploy to apply changes).</p>` : `<p class="muted">Not connected to Home Assistant.</p>`}
        ${notes((data.designer.problems || []).filter((p) => p.level === "blocking").map((p) => ({ level: "error", message: p.message })))}
        ${(data.conflicts || []).length ? notes(data.conflicts.map((c) => ({ level: c.level === "blocking" && !c.acknowledged ? "error" : "warning", message: c.message }))) : ""}</div>`;
  }

  function simulateTab() {
    if (!sim.inputs) return `<div class="card"><p class="muted">Loading the inputs…</p></div>`;
    const sc = sim.scenario;
    const inputs = sim.inputs.inputs.map((e) => {
      const st = sc.states[e.entity_id];
      const value = st ? st.state : e.state;
      return `<tr><td>${esc(e.name)}<br><span class="mono">${esc(e.entity_id)}</span></td><td><input type="text" list="sug-${esc(e.entity_id.replace(/\W/g, "-"))}" data-chg="simstate" data-id="${esc(e.entity_id)}" value="${esc(value ?? "")}" aria-label="State of ${esc(e.name)}"><datalist id="sug-${esc(e.entity_id.replace(/\W/g, "-"))}">${(e.suggested || []).map((s) => `<option value="${esc(s)}">`).join("")}</datalist>
        ${e.entity_id.startsWith("light.") ? ` brightness <input type="number" min="0" max="255" data-chg="simattr" data-id="${esc(e.entity_id)}" data-attr="brightness" value="${esc(((st || e).attributes || {}).brightness ?? "")}" aria-label="Brightness 0-255">` : ""}</td>
        <td>${st && st.origin === "test" ? `<span class="badge warn">test value</span>` : `<span class="badge">live</span>`}</td></tr>`;
    }).join("");
    const r = sim.result && sim.result.lighting ? sim.result.lighting[designerId] : null;
    const result = r ? `<div class="card" id="sim-result"><h2>Expected result</h2>
        ${r.event ? `<p><strong>${esc(r.event.switch)}: ${esc(r.event.interaction)}</strong> (an event, not a state)</p><ul class="small">${r.event.happened.map((h) => `<li>${esc(h.text)}</li>`).join("") || "<li>No action is mapped to it.</li>"}</ul>` : ""}
        ${r.lights.filter((l) => l.controlled).map((l) => `<div class="simlight" data-light="${esc(l.entity_id)}"><h3>${esc(l.name)} <span class="muted small">${esc(l.state || "?")}</span></h3>
          <p class="small">${l.matrices.map((m) => `${esc(m.name)}: <strong>${m.value ? esc(valueText(m.value[0])) : "UNSET"}</strong>`).join(" · ")}${l.mood ? ` · mood ${esc(l.mood)}` : ""}</p>
          <p class="small">${l.advanced.map((a) => `${esc(a.name)}: ${a.active === null ? "unknown" : a.active ? "<strong>active</strong>" : "inactive"}`).join(" · ")}${l.override != null ? ` · override ${l.override ? "<strong>active</strong>" : "inactive"}${l.override_ends ? ` (${esc(l.override_ends)})` : ""}` : ""}</p>
          <p class="small">${Object.keys(PROPS).map((p) => `${PROPS[p]}: ${l.resolved[p] ? `<strong>${esc(valueText(l.resolved[p][0]))}</strong> <span class="muted">(${esc(l.resolved[p][1])})</span>` : "—"}`).join(" · ")}</p>
          <p class="result-line"><strong>${l.command.service ? `light.${esc(l.command.service)} ${esc(JSON.stringify(l.command.data || {}))}` : `nothing is sent (${esc(l.command.why)})`}</strong></p>
          ${l.unknown.length ? `<p class="small warn-line">${l.unknown.map(esc).join("; ")}</p>` : ""}</div>`).join("")}
        ${r.missing.length ? `<p class="small">Not in the scenario: ${r.missing.map(esc).join(", ")}</p>` : ""}
        <p class="muted small">A prediction with the compiler's own plan. Nothing was sent to any device.</p></div>` : "";
    return `<div class="card"><h2>Scenario</h2><p class="muted small">States are persistent; an event (a press) happens once. Live values come from Home Assistant; what you change becomes a test value.</p>
        <div class="field"><label for="sim-event">What just happened</label><select id="sim-event" data-chg="simevent"><option value="">Nothing (only the states)</option>${opt((sim.inputs.events || []).map((e) => [e.id, `${e.switch}: ${e.label}`]), sc.event)}</select></div>
        <div class="field"><label>Time</label><span><input type="time" data-chg="simtime" value="${esc((sc.time || "").slice(0, 5))}" aria-label="Time"> <select data-chg="simday" aria-label="Weekday">${opt([["", "weekday…"], ...sim.inputs.weekdays.map((w) => [w, w])], sc.weekday)}</select></span></div>
        <div class="tscroll"><table id="sim-inputs"><thead><tr><th>Entity</th><th>State</th><th></th></tr></thead><tbody>${inputs}</tbody></table></div>
        <div class="actions"><button class="primary" data-act="simrun">Show what would happen</button><button data-act="simlive">Back to live values</button></div></div>${result}`;
  }

  function yamlTab() {
    return `<div class="card"><h2>Generated automations</h2><ul class="plain small">${(data.automations || []).map((a) => `<li>${esc(a.alias)} · ${esc(a.role === "group" ? "one light" : a.role)} · mode ${esc(a.mode)} · ${plural(a.rules, "rule")}</li>`).join("") || `<li class="muted">Nothing is generated yet.</li>`}</ul>
      ${data.yaml ? `<pre class="code" id="d-yaml">${esc(data.yaml)}</pre>` : ""}
      <p class="muted small">A preview of what deploying writes. Edit the designer, not the YAML.</p>
      <div class="actions"><a class="button primary" href="#/devices/${enc(deviceId)}">Review and deploy on the device page</a></div></div>`;
  }

  // ------------------------------------------------------------ drawing
  function draw() {
    const problems = (data.designer.problems || []).filter((p) => p.level === "blocking");
    const body = { lights: lightsTab, truth: () => layerTab("truth"), brightness: () => layerTab("brightness"), mood: moodTab, switches: switchesTab,
      advanced: advancedTab, priority: priorityTab, simulate: simulateTab, yaml: yamlTab }[tab]();
    view.innerHTML = `<h1><a class="back" href="#/devices/${enc(deviceId)}" aria-label="Back to the device">←</a>${icon("light")}<span id="title">${esc(d.name)}</span> <span class="muted small">${esc(data.device.name)}</span>
        <span class="spacer"></span><label class="switch small"><input type="checkbox" data-chg="flag" data-path="enabled" ${d.enabled ? "checked" : ""}> Enabled</label></h1>
      <nav class="tabs" aria-label="Lighting Designer sections">${tabs.map(([k, l]) => `<a href="#/design/${enc(deviceId)}/${enc(designerId)}/${k}" class="${k === tab ? "active" : ""}" ${k === tab ? 'aria-current="page"' : ""}>${esc(l)}</a>`).join("")}</nav>
      <div class="row"><span class="save-status" id="d-status"></span></div>
      ${problems.length ? `<div class="notice error" id="d-problems"><strong>Before this can be deployed:</strong><ul>${problems.map((p) => `<li>${esc(p.message)}</li>`).join("")}</ul></div>` : ""}
      <div id="d-body">${body}</div>`;
  }

  // ------------------------------------------------------------ handlers
  function applyToSelection(fn) {
    const spec = matrixSpec(sel.m);
    selected().forEach(([r, c]) => setCell(spec, r, c, fn(getCell(spec, r, c), r, c)));
    save(); draw();
  }
  const matrixOf = (el) => d[el.dataset.layer].matrices.find((m) => m.id === el.dataset.m);
  const act = {
    pick: async (el) => {
      const chosen = await pickEntity(el.dataset.purpose, { keep: get(el.dataset.path) || undefined });
      if (!chosen || !current()) return;
      await loadEntities([chosen.entity_id]);
      set(el.dataset.path, chosen.entity_id);
      save(); draw();
    },
    mcell: (el) => { const key = `${el.dataset.r}${SEP}${el.dataset.c}`; if (sel.m !== el.dataset.m) { sel.m = el.dataset.m; sel.keys = new Set(); } if (sel.keys.has(key)) sel.keys.delete(key); else sel.keys.add(key); draw(); },
    mrow: (el) => { const spec = matrixSpec(el.dataset.m); sel.m = spec.id; sel.keys = new Set(spec.cols.map((c) => `${el.dataset.r}${SEP}${c.key}`)); draw(); },
    mcol: (el) => { const spec = matrixSpec(el.dataset.m); sel.m = spec.id; sel.keys = new Set(spec.rows.map((r) => `${r.key}${SEP}${el.dataset.c}`)); draw(); },
    mall: (el) => { const spec = matrixSpec(el.dataset.m); sel.m = spec.id; sel.keys = new Set(spec.rows.flatMap((r) => spec.cols.map((c) => `${r.key}${SEP}${c.key}`))); draw(); },
    mnone: () => { sel.keys = new Set(); draw(); },
    mcopy: (el) => { const spec = matrixSpec(el.dataset.m); const items = selected(); const rows = [...spec.rows.map((r) => r.key), "__matrix"]; const cols = [...spec.cols.map((c) => c.key), "__row", "__matrix"];
      const r0 = Math.min(...items.map(([r]) => rows.indexOf(r))); const c0 = Math.min(...items.map(([, c]) => cols.indexOf(c)));
      sel.clip = { kind: spec.kind, cells: items.map(([r, c]) => ({ dr: rows.indexOf(r) - r0, dc: cols.indexOf(c) - c0, cell: clone(getCell(spec, r, c)) })) }; toast(`Copied ${plural(items.length, "cell")}.`); draw(); },
    mpaste: (el) => { const spec = matrixSpec(el.dataset.m); if (!sel.clip) return; const items = selected(); const rows = spec.rows.map((r) => r.key); const cols = [...spec.cols.map((c) => c.key), "__row"];
      if (sel.clip.cells.length === 1) items.forEach(([r, c]) => setCell(spec, r, c, clone(sel.clip.cells[0].cell)));
      else { const r0 = Math.min(...items.map(([r]) => rows.indexOf(r)).filter((i) => i >= 0)); const c0 = Math.min(...items.map(([, c]) => cols.indexOf(c)).filter((i) => i >= 0));
        sel.clip.cells.forEach(({ dr, dc, cell }) => { const r = rows[r0 + dr]; const c = cols[c0 + dc]; if (r && c) setCell(spec, r, c, clone(cell)); }); }
      save(); draw(); },
    mclear: () => applyToSelection(() => null),
    cunset: (el) => applyToSelection((cell) => (cell ? { ...cell, [el.dataset.key]: null } : cell)),
    aunset: (el) => { const a = get(el.dataset.path); a[el.dataset.key] = null; save(); draw(); },
    ladd: (el) => {
      const area = (view.querySelector(`#lvl-room-${el.dataset.layer}`) || {}).value;
      const room = (data.levels || []).find((r) => r.area_id === area);
      if (!room) { toast("Choose the room first."); return; }
      d[el.dataset.layer].matrices.push({ id: newId("m"), name: `Light level · ${room.name}`.slice(0, 40), enabled: true, level: { room: area }, columns: [], cells: {}, row_defaults: {}, default: null, policy: null, ranks: [] });
      save(); draw();
    },
    madd: (el) => { const L = d[el.dataset.layer]; L.matrices.push({ id: newId("m"), name: el.dataset.layer === "truth" ? "Truth" : "Brightness", enabled: true, columns: [], cells: {}, row_defaults: {}, default: null, policy: null, ranks: [] }); save(); draw(); },
    mdrop: async (el) => { const L = d[el.dataset.layer]; const m = L.matrices[Number(el.dataset.i)];
      if (!(await ask({ title: `Delete the matrix “${m.name}”?`, body: "Its columns and cells go with it (the automatic backups keep the previous configuration).", ok: "Delete", danger: true }))) return;
      L.matrices.splice(Number(el.dataset.i), 1); save(); draw(); },
    mup: (el) => { const L = d[el.dataset.layer].matrices; const i = Number(el.dataset.i); [L[i - 1], L[i]] = [L[i], L[i - 1]]; save(); draw(); },
    coladd: (el) => {
      const m = matrixOf(el);
      const value = (view.querySelector(`#add-${el.dataset.m}`) || {}).value;
      if (!value) return;
      const id = newId("c");
      if (value.includes(":")) { const [source, state] = value.split(":"); m.columns.push({ id, kind: "presence", label: "", source, state }); }
      else if (value === "numeric") m.columns.push({ id, kind: "numeric", label: "", entity_id: null, attribute: null, from: null, below: null, for_seconds: 0 });
      else if (value === "state") m.columns.push({ id, kind: "state", label: "", entity_id: null, state: null, attribute: null, for_seconds: 0 });
      else if (value === "time") m.columns.push({ id, kind: "time", label: "", after: null, before: null, weekdays: [] });
      else m.columns.push({ id, kind: "always", label: "" });
      save(); draw();
    },
    coldrop: (el) => { const m = matrixOf(el); const [gone] = m.columns.splice(Number(el.dataset.i), 1);
      Object.values(m.cells).forEach((row) => delete row[gone.id]); Object.keys(m.cells).forEach((l) => { if (!Object.keys(m.cells[l]).length) delete m.cells[l]; });
      m.ranks = m.ranks.filter((r) => r !== gone.id); save(); draw(); },
    kadd: (el) => { const m = matrixOf(el); m.ranks.push(el.dataset.c); save(); draw(); },
    kdrop: (el) => { const m = matrixOf(el); m.ranks.splice(Number(el.dataset.i), 1); save(); draw(); },
    kmove: (el) => { const m = matrixOf(el); const i = Number(el.dataset.i); const j = i + Number(el.dataset.dir); [m.ranks[i], m.ranks[j]] = [m.ranks[j], m.ranks[i]]; save(); draw(); },
    pmove: (el) => { const i = Number(el.dataset.i); const j = i + Number(el.dataset.dir); [d.priority[i], d.priority[j]] = [d.priority[j], d.priority[i]]; save(); draw(); },
    noauto: () => { d.mood.auto = null; save(); draw(); },
    // switches
    frooms: async () => { try { const r = await api("api/rooms"); finder.rooms = r.rooms; draw(); } catch (err) { toast(errorText(err)); } },
    fpick: async (el) => {
      const dv = finder.devices[Number(el.dataset.i)];
      const entity = el.dataset.entity || null;
      try {
        const found = await api(`api/switch-interactions?${dv.device_id && !entity ? `device_id=${enc(dv.device_id)}` : ""}${entity ? `entity_id=${enc(entity)}` : ""}`);
        finder.chosen = { device: dv, entity, label: entity ? `${dv.name}: ${entity}` : `${dv.name} (device triggers)` };
        finder.found = { ...found, interactions: found.interactions.map((i) => ({ ...i, take: false })) };
        draw();
      } catch (err) { toast(errorText(err)); }
    },
    fadd: () => {
      const f = finder.found;
      const taken = f.interactions.filter((i) => i.take).map((i) => ({ id: newId("i"), kind: i.kind, label: i.label, trigger: i.trigger, ...(i.button ? { button: i.button } : {}) }));
      const into = finder.target != null ? d.switches[finder.target] : null;
      if (into) {
        taken.forEach((i) => { if (!into.interactions.some((x) => JSON.stringify(x.trigger) === JSON.stringify(i.trigger))) into.interactions.push(i); });
      } else {
        const kind = f.kind || "momentary";
        d.switches.push({ id: newId("w"), name: finder.chosen.device.name || "Switch", kind, entity_id: finder.chosen.entity, ha_device_id: finder.chosen.device.device_id,
          interactions: taken, mappings: [], ...(kind === "latched" ? { ignore_automations: true } : {}) });
      }
      finder = { rooms: finder.rooms, area: finder.area, devices: null, chosen: null, found: null, manual: null, target: null };
      save(); draw();
    },
    fclose: () => { finder = { ...finder, chosen: null, found: null, manual: null, target: null }; draw(); },
    fmore: async (el) => {
      const w = d.switches[Number(el.dataset.w)];
      try {
        const found = await api(`api/switch-interactions?${w.ha_device_id ? `device_id=${enc(w.ha_device_id)}` : ""}${w.entity_id ? `${w.ha_device_id ? "&" : ""}entity_id=${enc(w.entity_id)}` : ""}`);
        finder.target = Number(el.dataset.w);
        finder.chosen = { device: { name: w.name, device_id: w.ha_device_id }, entity: w.entity_id, label: w.name };
        finder.found = { ...found, interactions: found.interactions.map((i) => ({ ...i, take: false })) };
        draw();
        const card = view.querySelector("#finder"); if (card) card.scrollIntoView({ block: "nearest" });
      } catch (err) { toast(errorText(err)); }
    },
    fmanual: (el) => { finder.manual = { type: "zha", kind: "single", target: el && el.dataset.w != null ? Number(el.dataset.w) : null }; draw(); },
    iadd: (el) => { const w = d.switches.find((x) => x.id === el.dataset.w); const unused = w.interactions.find((i) => !w.mappings.some((m) => m.interaction === i.id)); adding[w.id] = { interaction: (unused || w.interactions[0] || {}).id || "", kind: "" }; draw(); },
    xcancel: (el) => { delete adding[el.dataset.w]; draw(); },
    xcreate: (el) => {
      const w = d.switches[Number(el.dataset.w)];
      const form = adding[w.id];
      if (!form || !form.interaction || !form.kind) return;
      w.mappings.push({ id: newId("p"), interaction: form.interaction, enabled: true, lights: d.lights.length === 1 ? [d.lights[0]] : [], action: newAction(form.kind), override: false });
      delete adding[w.id];
      save(); draw();
    },
    xdrop: (el) => { d.switches[Number(el.dataset.w)].mappings.splice(Number(el.dataset.mi), 1); save(); draw(); },
    xmove: (el) => { const list = d.switches[Number(el.dataset.w)].mappings; const i = Number(el.dataset.mi); const j = i + Number(el.dataset.dir); [list[i], list[j]] = [list[j], list[i]]; save(); draw(); },
    xmore: (el) => { const id = el.dataset.id; if (expanded.has(id)) { expanded.delete(id); expanded.delete(`${id}:c`); } else expanded.add(id); draw(); },
    xcadd: async (el) => {
      const m = d.switches[Number(el.dataset.w)].mappings[Number(el.dataset.mi)];
      const kind = (view.querySelector(`#mcnew-${el.dataset.w}-${el.dataset.mi}`) || {}).value;
      if (!kind) return;
      m.conditions = m.conditions || { type: "and", items: [] };
      expanded.add(m.id); expanded.add(`${m.id}:c`);
      if (kind === "time") { m.conditions.items.push({ type: "time", after: null, before: null, weekdays: [] }); draw(); return; }
      const chosen = await pickEntity(kind === "numeric" ? "numeric" : "any");
      if (!chosen || !current()) return;
      await loadEntities([chosen.entity_id]);
      m.conditions.items.push(kind === "state" ? { type: "state", entity_id: chosen.entity_id, state: null, negate: false, for_seconds: 0, attribute: null }
        : { type: "numeric", entity_id: chosen.entity_id, above: null, below: null, attribute: null });
      draw();  // saved once the state or a limit is given
    },
    xcdrop: (el) => { const m = d.switches[Number(el.dataset.w)].mappings[Number(el.dataset.mi)]; m.conditions.items.splice(Number(el.dataset.j), 1); if (!m.conditions.items.length) delete m.conditions; save(); draw(); },
    fmanadd: () => {
      const m = finder.manual;
      let trigger = null;
      if (m.type === "zha" && m.ieee && m.command) trigger = { type: "event", event_type: "zha_event", event_data: { device_ieee: m.ieee, command: m.command } };
      if (m.type === "mqtt" && m.topic) trigger = { type: "mqtt", topic: m.topic, payload: m.payload || null };
      if (m.type === "event" && m.event_type) { let dataObj = {}; try { dataObj = JSON.parse(m.data || "{}"); } catch { toast("The event data is not JSON."); return; } trigger = { type: "event", event_type: m.event_type, event_data: dataObj }; }
      if (!trigger) { toast("Fill in the event first."); return; }
      let w = m.target != null && d.switches[m.target] && d.switches[m.target].kind === "momentary" ? d.switches[m.target] : d.switches.find((x) => x.manual);
      if (!w) { w = { id: newId("w"), name: "Remote (manual events)", kind: "momentary", entity_id: null, ha_device_id: null, interactions: [], mappings: [], manual: true }; d.switches.push(w); }
      w.interactions.push({ id: newId("i"), kind: m.kind, label: `${INTERACTION[m.kind]} (${m.type === "zha" ? m.command : m.type === "mqtt" ? m.payload || m.topic : m.event_type})`, trigger });
      finder.manual = null; save(); draw();
    },
    wdrop: async (el) => { const w = d.switches[Number(el.dataset.w)]; if (!(await ask({ title: `Remove “${w.name}”?`, body: "Its interactions and actions go with it.", ok: "Remove", danger: true }))) return; d.switches.splice(Number(el.dataset.w), 1); save(); draw(); },
    idrop: async (el) => {
      const w = d.switches[Number(el.dataset.w)];
      const i = w.interactions[Number(el.dataset.ii)];
      const used = w.mappings.filter((m) => m.interaction === i.id).length;
      if (used && !(await ask({ title: `Remove the event “${i.label || INTERACTION[i.kind]}”?`, body: `${plural(used, "interaction")} of this switch use it and go with it. The other interactions stay.`, ok: "Remove", danger: true }))) return;
      w.interactions.splice(w.interactions.indexOf(i), 1);
      w.mappings = w.mappings.filter((m) => m.interaction !== i.id);
      w.mappings.forEach((m) => { if (m.action.kind === "dim" && m.action.release === i.id) m.action.release = null; });
      save(); draw();
    },
    reladd: () => { const at = (view.querySelector("#rel-at") || {}).value; if (!at) return; d.override.release.push({ type: "time", at: `${at}:00`, weekdays: [] }); save(); draw(); },
    reldrop: (el) => { d.override.release.splice(Number(el.dataset.i), 1); save(); draw(); },
    relint: () => { const id = (view.querySelector("#rel-int") || {}).value; const i = d.switches.flatMap((w) => w.interactions).find((x) => x.id === id); if (!i) return; d.override.release.push(clone(i.trigger)); save(); draw(); },
    adopt: async (el) => {
      try {
        data = await change("api/designers/adopt", { device_id: deviceId, designer_id: designerId, rule_id: el.dataset.id });
        d = clone(data.designer.config);
        toast("Moved: the ordinary rule is replaced by a switch action. Deploy the device to apply it.");
        draw();
      } catch (err) { toast(errorText(err)); }
    },
    // advanced rules
    radd: (el) => { d.advanced.push({ id: newId("a"), name: el.dataset.kind === "while" ? "While …" : "When …", enabled: true, kind: el.dataset.kind, lights: [], conditions: { type: "and", items: [] },
      set: el.dataset.kind === "while" ? { truth: null, brightness_pct: null, color_temp_kelvin: null, color: null, effect: null, transition: null } : null, triggers: [], actions: [] }); save(); draw(); },
    rdrop: async (el) => { const r = d.advanced[Number(el.dataset.i)]; if (!(await ask({ title: `Delete “${r.name}”?`, ok: "Delete", danger: true }))) return; d.advanced.splice(Number(el.dataset.i), 1); save(); draw(); },
    rmove: (el) => { const i = Number(el.dataset.i); const j = i + Number(el.dataset.dir); [d.advanced[i], d.advanced[j]] = [d.advanced[j], d.advanced[i]]; save(); draw(); },
    cadd: async (el) => {
      const r = d.advanced[Number(el.dataset.r)];
      const kind = (view.querySelector(`#cnew-${el.dataset.r}`) || {}).value;
      if (!kind) return;
      if (kind.startsWith("src:")) { const s = srcById(kind.slice(4)); r.conditions.items.push({ type: "state", entity_id: s.entity_id, state: "on", negate: false, for_seconds: 0, attribute: null }); save(); draw(); return; }
      if (kind === "time") { r.conditions.items.push({ type: "time", after: null, before: null, weekdays: [] }); draw(); return; }  // saved once a time is given
      const chosen = await pickEntity(kind === "numeric" ? "numeric" : "any");
      if (!chosen || !current()) return;
      await loadEntities([chosen.entity_id]);
      r.conditions.items.push(kind === "state" ? { type: "state", entity_id: chosen.entity_id, state: null, negate: false, for_seconds: 0, attribute: null }
        : { type: "numeric", entity_id: chosen.entity_id, above: null, below: null, attribute: null });
      draw();  // saved once the state or a limit is given
    },
    cdrop: (el) => { d.advanced[Number(el.dataset.r)].conditions.items.splice(Number(el.dataset.j), 1); save(); draw(); },
    tadd: (el) => { const at = (view.querySelector(`#tat-${el.dataset.r}`) || {}).value; if (!at) return; d.advanced[Number(el.dataset.r)].triggers.push({ type: "time", at: `${at}:00`, weekdays: [] }); save(); draw(); },
    tdrop: (el) => { d.advanced[Number(el.dataset.r)].triggers.splice(Number(el.dataset.j), 1); save(); draw(); },
    simrun: async () => { try { sim.result = await api("api/designers/simulate", { device_id: deviceId, scenario: scenario() }); if (current()) draw(); } catch (err) { toast(errorText(err)); } },
    simlive: () => { sim.scenario.states = {}; sim.result = null; draw(); },
  };
  function scenario() {
    const states = {};
    sim.inputs.inputs.forEach((e) => { states[e.entity_id] = { state: e.state, attributes: { ...e.attributes }, origin: "live" }; });
    Object.entries(sim.scenario.states).forEach(([id, st]) => { states[id] = { ...(states[id] || {}), ...st, attributes: { ...((states[id] || {}).attributes || {}), ...(st.attributes || {}) } }; });
    return { states, event: sim.scenario.event, time: sim.scenario.time, weekday: sim.scenario.weekday };
  }
  const testState = (id) => (sim.scenario.states[id] = sim.scenario.states[id] || { origin: "test", attributes: {} });
  function newAction(kind) {
    const action = { kind };
    if (kind === "brightness") action.pct = 100;
    if (kind === "step") action.pct = 10;
    if (kind === "dim") Object.assign(action, { direction: null, step: 10, interval: 1, release: null });
    if (kind === "native") Object.assign(action, { action: "light.turn_on", data: {} });
    if (kind === "mood" || kind === "select_mood") action.value = null;
    if (kind === "advanced") action.rule = null;
    return action;
  }
  const chg = {
    flag: (el) => { set(el.dataset.path, el.checked); save(); draw(); },
    num: (el) => { set(el.dataset.path, el.value === "" ? null : Number(el.value)); save(); draw(); },
    numsel: (el) => { set(el.dataset.path, el.value === "" ? null : Number(el.value)); save(); draw(); },
    text: (el) => { set(el.dataset.path, el.value === "" ? null : el.value); save(); draw(); },
    json: (el) => { try { set(el.dataset.path, JSON.parse(el.value || "{}")); save(); } catch { toast("Not valid JSON."); } },
    time: (el) => { set(el.dataset.path, el.value ? `${el.value}:00` : null); save(); },
    wday: (el) => { const list = get(el.dataset.path); const i = list.indexOf(el.value); if (el.checked && i < 0) list.push(el.value); if (!el.checked && i >= 0) list.splice(i, 1); save(); },
    lightset: (el) => { const list = get(el.dataset.path); const i = list.indexOf(el.value); if (el.checked && i < 0) list.push(el.value); if (!el.checked && i >= 0) list.splice(i, 1); save(); draw(); },
    row: (el) => {
      const list = d.lights;
      if (el.checked) list.push(el.dataset.id); else list.splice(list.indexOf(el.dataset.id), 1);
      if (!el.checked) {  // the row's cells and its uses go with it
        const id = el.dataset.id;
        ["truth", "brightness"].forEach((l) => d[l].matrices.forEach((m) => { delete m.cells[id]; delete m.row_defaults[id]; }));
        delete d.mood.cells[id]; delete d.mood.row_defaults[id];
        d.switches.forEach((w) => w.mappings.forEach((m) => { m.lights = m.lights.filter((x) => x !== id); }));
        d.advanced.forEach((r) => { r.lights = r.lights.filter((x) => x !== id); });
      }
      save(); draw();
    },
    value: (el) => { const list = d.mood.values; if (el.checked) list.push(el.value); else { list.splice(list.indexOf(el.value), 1); Object.values(d.mood.cells).forEach((row) => delete row[el.value]); } save(); draw(); },
    auto: (el) => { if (el.value) { d.mood.auto = { value: el.value, entity_id: null }; save(); draw(); } },
    ckind: (el) => applyToSelection((cell) => {
      const k = el.value;
      if (k === "unset") return null;
      if (cell && cell.kind === k) return cell;
      if (k === "set") return sel.m === "mood" ? { kind: "set", brightness_pct: null, color_temp_kelvin: null, color: null, effect: null, transition: null } : { kind: "set", pct: null };
      return { kind: k };
    }),
    cval: (el) => applyToSelection((cell) => {
      if (!cell) return cell;
      const out = { ...cell };
      const key = el.dataset.key;
      if (key === "color") { out.color = { rgb: rgbOf(el.value) }; out.color_temp_kelvin = null; }
      else if (key === "effect") out.effect = el.value || null;
      else { out[key] = el.value === "" ? null : Number(el.value); if (key === "color_temp_kelvin" && out[key] != null) out.color = null; }
      return out;
    }),
    attr: (el) => {
      const a = get(el.dataset.path);
      const key = el.dataset.key;
      if (key === "color") { a.color = { rgb: rgbOf(el.value) }; a.color_temp_kelvin = null; }
      else if (key === "effect") a.effect = el.value || null;
      else { a[key] = el.value === "" ? null : Number(el.value); if (key === "color_temp_kelvin" && a[key] != null) a.color = null; }
      save(); draw();
    },
    keep: (el) => { const a = get(el.dataset.path); const key = el.dataset.key; if (key === "color_temp_kelvin" || key === "color") { a.color = el.checked ? "keep" : null; a.color_temp_kelvin = null; } else a[key] = el.checked ? "keep" : null; save(); draw(); },
    truthset: (el) => { d.advanced[Number(el.dataset.r)].set.truth = el.value || null; save(); draw(); },
    ctype: (el) => { d.advanced[Number(el.dataset.r)].conditions.type = el.value; save(); },
    wact: (el) => { const r = d.advanced[Number(el.dataset.r)]; r.actions = el.value ? [{ kind: el.value, ...(el.value === "brightness" ? { pct: 100 } : el.value === "step" ? { pct: 10 } : el.value === "mood" ? { value: null } : {}) }] : []; save(); draw(); },
    maction: (el) => { const m = d.switches[Number(el.dataset.w)].mappings[Number(el.dataset.mi)]; m.action = newAction(el.value); if (["none", "select_mood", "override", "resume", "dim"].includes(el.value)) m.override = false; save(); draw(); },
    xint: (el) => { const m = d.switches[Number(el.dataset.w)].mappings[Number(el.dataset.mi)]; m.interaction = el.value || null; save(); draw(); },
    xctype: (el) => { const m = d.switches[Number(el.dataset.w)].mappings[Number(el.dataset.mi)]; m.conditions = m.conditions || { type: "and", items: [] }; m.conditions.type = el.value; if (m.conditions.items.length) save(); },
    xnew: (el) => { adding[el.dataset.w][el.dataset.key] = el.value; draw(); },
    froom: async (el) => {
      finder.area = el.value;
      finder.devices = null; finder.found = null;
      if (!el.value) { draw(); return; }
      try { const r = await api(`api/switch-sources?area=${enc(el.value === "__none" ? "" : el.value)}`); finder.devices = r.devices; draw(); } catch (err) { toast(errorText(err)); }
    },
    fint: (el) => { finder.found.interactions[Number(el.dataset.n)].take = el.checked; draw(); },
    fman: (el) => { finder.manual[el.dataset.key] = el.value; if (el.dataset.key === "type") draw(); },
    simevent: (el) => { sim.scenario.event = el.value || null; },
    simtime: (el) => { sim.scenario.time = el.value ? `${el.value}:00` : null; },
    simday: (el) => { sim.scenario.weekday = el.value || null; },
    simstate: (el) => { testState(el.dataset.id).state = el.value; draw(); },
    simattr: (el) => { const st = testState(el.dataset.id); st.attributes = { ...(st.attributes || {}), [el.dataset.attr]: el.value === "" ? null : Number(el.value) }; },
  };

  draw();
  if (tab === "simulate") api(`api/simulation/${enc(deviceId)}`).then((inputs) => { if (current()) { sim.inputs = inputs; draw(); } }).catch((err) => toast(errorText(err)));
  return { act, chg, inp: {} };
}
