// Automation Builder - the specialised designers (Phase 0.3): Lighting and Cover, the template gallery, simulation.
// A designer is saved as soon as it changes (like every setting here); nothing reaches Home Assistant before the
// device is deployed. Everything shown as resolved ("DEFAULT: 30%") comes from the backend, which also generates
// the rules: the interface never decides what a cell means.

import { api, ask, change, enc, entities, entityName, errorText, esc, icon, loadEntities, notes, pickEntity, session, toast, view } from "./ui.js";
import { lightingPage } from "./lighting.js";
import { heatingPage } from "./heating.js";

const clone = (v) => JSON.parse(JSON.stringify(v));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const opt = (list, current) => list.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(current ?? "") ? "selected" : ""}>${esc(l)}</option>`).join("");
const LEVEL = { blocking: "error", warning: "warning", info: "info" };
const hexOf = (rgb) => "#" + (rgb || [255, 180, 107]).map((c) => Number(c).toString(16).padStart(2, "0")).join("");
const rgbOf = (value) => [1, 3, 5].map((i) => parseInt(value.slice(i, i + 2), 16));
const SEP = "\u0001";
const short = (id) => (id && id.includes(".") ? id.split(".")[1] : id);

// ------------------------------------------------------------------ labels (same wording as the backend)
export function lightLabel(cell) {
  if (!cell) return "UNSET";
  if (cell.kind === "none") return "NO ACTION";
  if (cell.kind === "mood") return "USE MOOD";
  if (cell.kind === "default") return "USE DEFAULT";
  const extra = cell.transition ? [`${cell.transition} s fade`] : [];
  if (cell.kind === "off") return ["OFF", ...extra].join(" · ");
  const parts = [];
  if (cell.brightness_pct != null) parts.push(`${cell.brightness_pct}%`);
  if (cell.color_temp_kelvin != null) parts.push(`${cell.color_temp_kelvin}K`);
  if (cell.color && cell.color.rgb) parts.push(`RGB ${cell.color.rgb.join(",")}`);
  if (cell.color && cell.color.hs) parts.push(`HS ${cell.color.hs.join("/")}`);
  if (cell.effect) parts.push(`effect ${cell.effect}`);
  return (parts.length ? [...parts, ...extra] : ["ON", ...extra]).join(" · ");
}
export function coverLabel(cell) {
  if (!cell) return "UNSET";
  if (cell.kind === "none") return "NO ACTION";
  if (cell.kind === "default") return "USE DEFAULT";
  if (cell.kind === "open" || cell.kind === "close") return cell.kind.toUpperCase();
  const parts = [];
  if (cell.position != null) parts.push(`${cell.position}%`);
  if (cell.tilt != null) parts.push(`tilt ${cell.tilt}%`);
  return parts.join(" · ") || "POSITION";
}
const STATE_CLASS = { unset: "unset", explicit: "explicit", default: "inherited", none: "none", missing_default: "bad" };
const SOURCE_TEXT = { cell: "set in this cell", "row default": "from the row default", "matrix default": "from the matrix default" };

// ------------------------------------------------------------------ the matrix editor (shared by every designer)
// spec: { id, title, rows:[{key,label,sub}], cols:[{key,label,sub}], get(r,c), view(r,c)?, label(cell), rowDefaults?, matrixDefault? }
export function matrixHtml(spec, sel) {
  const chosen = sel.m === spec.id ? sel.keys : new Set();
  const cellButton = (r, c, cell, shown) => {
    const key = `${r}${SEP}${c}`;
    const st = shown ? shown.state : (cell ? (cell.kind === "none" ? "none" : "explicit") : "unset");
    const label = shown ? shown.label : spec.label(cell);
    const title = shown && shown.source ? SOURCE_TEXT[shown.source] || shown.source : st === "unset" ? "Nothing configured" : "";
    return `<button type="button" class="mcell st-${STATE_CLASS[st] || "explicit"} ${chosen.has(key) ? "sel" : ""} ${cell && cell.kind === "off" ? "is-off" : ""}" data-act="mcell" data-m="${esc(spec.id)}" data-r="${esc(r)}" data-c="${esc(c)}" title="${esc(title)}" aria-pressed="${chosen.has(key)}">${esc(label)}${shown && shown.source && shown.source !== "cell" ? `<small>${esc(shown.source)}</small>` : ""}</button>`;
  };
  const head = spec.cols.map((c) => `<th><button type="button" class="link mhead" data-act="mcol" data-m="${esc(spec.id)}" data-c="${esc(c.key)}" title="Select the column">${esc(c.label)}</button>${c.sub ? `<br><span class="muted small">${esc(c.sub)}</span>` : ""}</th>`).join("");
  const body = spec.rows.map((r) => `<tr><th scope="row"><button type="button" class="link mhead" data-act="mrow" data-m="${esc(spec.id)}" data-r="${esc(r.key)}" title="Select the row">${esc(r.label)}</button>${r.sub ? `<br><span class="mono">${esc(r.sub)}</span>` : ""}</th>
      ${spec.cols.map((c) => `<td>${cellButton(r.key, c.key, spec.get(r.key, c.key), spec.view ? spec.view(r.key, c.key) : null)}</td>`).join("")}
      ${spec.rowDefaults ? `<td class="rowdef">${cellButton(r.key, "__row", spec.rowDefaults.get(r.key), null)}</td>` : ""}</tr>`).join("");
  const n = chosen.size;
  return `<div class="matrix-box" data-matrix="${esc(spec.id)}">
    <div class="row mtools"><strong>${esc(spec.title)}</strong><span class="spacer"></span>
      <span class="muted small">${n ? `${plural(n, "cell")} selected` : "Tap cells to select them"}</span>
      <button type="button" class="link" data-act="mall" data-m="${esc(spec.id)}">Select all</button>
      <button type="button" class="link" data-act="mnone" data-m="${esc(spec.id)}" ${n ? "" : "disabled"}>Clear selection</button>
      <button type="button" class="link" data-act="mcopy" data-m="${esc(spec.id)}" ${n ? "" : "disabled"}>Copy</button>
      <button type="button" class="link" data-act="mpaste" data-m="${esc(spec.id)}" ${n && sel.clip && sel.clip.kind === spec.kind ? "" : "disabled"}>Paste</button>
      <button type="button" class="link danger-text" data-act="mclear" data-m="${esc(spec.id)}" ${n ? "" : "disabled"}>Set to Unset</button></div>
    <div class="tscroll"><table class="matrix"><thead><tr><th></th>${head}${spec.rowDefaults ? `<th class="rowdef">Row default<br><span class="muted small">optional</span></th>` : ""}</tr></thead><tbody>${body}</tbody></table></div>
    ${spec.matrixDefault ? `<div class="row small mdefault"><span>Matrix default <span class="muted">(optional: fills every unset cell of every column that has no row default)</span>:</span>${cellButton("__matrix", "__matrix", spec.matrixDefault.get(), null)}</div>` : ""}
    <p class="muted small legend"><span class="mcell st-unset">UNSET</span> nothing happens (or a configured default applies) · <span class="mcell st-inherited">DEFAULT: …</span> taken from a default · <span class="mcell st-none">NO ACTION</span> explicitly nothing · Off and Closed are commands.</p></div>`;
}

// ------------------------------------------------------------------ cell editors
export function capsOf(rows, caps) {
  const known = rows.map((r) => caps[r]).filter((c) => c && !c.unknown);
  const all = (k) => known.length === rows.length && known.every((c) => c[k]);
  return { known: known.length === rows.length, brightness: all("brightness"), color_temp: all("color_temp"), color: all("color"), effect: all("effect"),
    position: all("position"), tilt: all("tilt"), open: all("open"), close: all("close"),
    min_kelvin: Math.max(1000, ...known.map((c) => c.min_kelvin || 1000)), max_kelvin: Math.min(12000, ...known.map((c) => c.max_kelvin || 12000)),
    effects: known.length ? [...new Set(known.flatMap((c) => c.effects || []))].filter((e) => known.every((c) => (c.effects || []).includes(e))) : [] };
}

function lightEditor(cell, kinds, cap, single) {
  const kind = cell ? cell.kind : "unset";
  const KINDS = [["unset", "Unset"], ["default", "Use default"], ["none", "No action"], ["off", "Off"], ["on", "On"], ["mood", "Use mood"]].filter(([k]) => k === "unset" || kinds.includes(k));
  // An empty field means "leave this attribute as it is": nothing is filled in for you.
  const field = (key, label, html, isSet) => `<div class="cfield"><span class="flabel">${esc(label)}</span>${html}${isSet ? ` <button type="button" class="link small" data-act="cunset" data-key="${key}">unchanged</button>` : ` <span class="muted small">unchanged</span>`}</div>`;
  let body = "";
  if (kind === "on") {
    const c = cell;
    if (cap.brightness || !cap.known) body += field("brightness_pct", "Brightness", `<input type="number" min="1" max="100" value="${c.brightness_pct ?? ""}" placeholder="—" data-chg="cval" data-key="brightness_pct" aria-label="Brightness percent"> %${c.brightness_pct != null ? ` <input type="range" min="1" max="100" value="${c.brightness_pct}" data-chg="cval" data-key="brightness_pct" aria-label="Brightness">` : ""}`, c.brightness_pct != null);
    if (cap.color_temp || !cap.known) body += field("color_temp_kelvin", "Colour temperature", `<input type="number" min="${cap.min_kelvin}" max="${cap.max_kelvin}" step="50" value="${c.color_temp_kelvin ?? ""}" placeholder="—" data-chg="cval" data-key="color_temp_kelvin" aria-label="Colour temperature"> K <span class="muted small">${cap.min_kelvin}–${cap.max_kelvin} K</span>`, c.color_temp_kelvin != null);
    if (cap.color || !cap.known) body += field("color", "Colour", `<input type="color" value="${hexOf(c.color && c.color.rgb ? c.color.rgb : [255, 255, 255])}" data-chg="cval" data-key="color" aria-label="Colour" class="${c.color ? "" : "unchosen"}">`, Boolean(c.color));
    if (cap.effect && cap.effects.length) body += field("effect", "Effect", `<select data-chg="cval" data-key="effect" aria-label="Effect"><option value="">—</option>${opt(cap.effects.map((e) => [e, e]), c.effect)}</select>`, Boolean(c.effect));
  }
  if (kind === "on" || kind === "off") {
    body += field("transition", "Transition", `<input type="number" min="0" max="300" step="0.5" value="${cell.transition ?? ""}" placeholder="—" data-chg="cval" data-key="transition" aria-label="Transition seconds"> s`, cell.transition != null);
  }
  const unsupported = !cap.known ? `<p class="muted small">Some of these lights do not report what they support right now; every control is shown.</p>`
    : kind === "on" && !cap.brightness ? `<p class="muted small">These lights only switch on and off.</p>` : "";
  return `<div class="cell-editor"><div class="kinds" role="radiogroup" aria-label="What happens">${KINDS.map(([k, l]) => `<label class="kind ${k === kind ? "on" : ""}"><input type="radio" name="ckind" value="${k}" data-chg="ckind" ${k === kind ? "checked" : ""}>${esc(l)}</label>`).join("")}</div>
    ${body ? `<div class="cfields">${body}</div>` : ""}${unsupported}
    ${single ? "" : `<p class="muted small">Changes apply to every selected cell; fields you do not touch keep their own values.</p>`}</div>`;
}

function coverEditor(cell, kinds, cap, single) {
  const kind = cell ? cell.kind : "unset";
  const KINDS = [["unset", "Unset"], ["default", "Use default"], ["none", "No action"], ["open", "Open"], ["close", "Close"], ["position", "Position"]]
    .filter(([k]) => k === "unset" || kinds.includes(k)).filter(([k]) => !cap.known || k !== "position" || cap.position || cap.tilt)
    .filter(([k]) => !cap.known || (k !== "open" || cap.open) && (k !== "close" || cap.close));
  let body = "";
  if (kind === "position") {
    const field = (key, label, ok) => (ok || !cap.known ? `<div class="cfield"><span class="flabel">${esc(label)}</span><input type="number" min="0" max="100" value="${cell[key] ?? ""}" placeholder="—" data-chg="cval" data-key="${key}" aria-label="${esc(label)} percent"> %${cell[key] != null ? ` <input type="range" min="0" max="100" value="${cell[key]}" data-chg="cval" data-key="${key}" aria-label="${esc(label)}"> <button type="button" class="link small" data-act="cunset" data-key="${key}">unchanged</button>` : ` <span class="muted small">unchanged</span>`}</div>` : "");
    body = field("position", "Position (100 = open)", cap.position) + field("tilt", "Tilt", cap.tilt);
  }
  return `<div class="cell-editor"><div class="kinds" role="radiogroup" aria-label="What happens">${KINDS.map(([k, l]) => `<label class="kind ${k === kind ? "on" : ""}"><input type="radio" name="ckind" value="${k}" data-chg="ckind" ${k === kind ? "checked" : ""}>${esc(l)}</label>`).join("")}</div>
    ${body ? `<div class="cfields">${body}</div>` : ""}${cap.known && !cap.position && !cap.tilt ? `<p class="muted small">These covers only open and close.</p>` : ""}
    ${kind === "position" && cell.position == null && cell.tilt == null ? `<p class="small">Give a position, a tilt or both.</p>` : ""}
    ${single ? "" : `<p class="muted small">Changes apply to every selected cell.</p>`}</div>`;
}

function newCell(kind, family) {
  if (kind === "unset") return null;
  if (family === "light") {
    if (kind === "on") return { kind: "on", brightness_pct: null, color_temp_kelvin: null, color: null, effect: null, transition: null };
    if (kind === "off") return { kind: "off", transition: null };
    return { kind };
  }
  if (kind === "position") return { kind: "position", position: null, tilt: null };
  return { kind };
}

// ------------------------------------------------------------------ the page
export async function designerPage(deviceId, designerId, tab, current) {
  let data = await api(`api/designers/${enc(deviceId)}/${enc(designerId)}`);
  if (!current()) return null;
  if (data.designer.config.kind === "lighting" && data.designer.config.version === 2) return lightingPage(deviceId, designerId, tab, current, data);
  if (data.designer.config.kind === "heating") return heatingPage(deviceId, designerId, tab, current, data);
  if (data.designer.config.kind === "light_level") { location.hash = `#/light-level/rooms/${enc(data.designer.config.room)}`; return null; }
  let d = clone(data.designer.config);
  const kind = d.kind;
  const sel = { m: null, keys: new Set(), clip: null };
  let sim = { inputs: null, result: null, scenario: { states: {}, trigger: null, time: null, weekday: null } };
  let sun = null;
  let chain = Promise.resolve();
  let pending = false;
  const tabs = kind === "lighting"
    ? [["combine", "Combination"], ["mood", "Mood"], ["presence", "Presence / Zones"], ["lux", "Light Level"], ["schedule", "Schedule"], ["override", "Overrides"], ["simulate", "Simulate"], ["yaml", "YAML"]]
    : [["covers", "Covers & controls"], ["sun", "Sun Position"], ["modes", "Cover Moods"], ["mapping", "Normal Mood Mapping"], ["presence", "Presence / Zones"], ["schedule", "Schedule"], ["override", "Overrides"], ["simulate", "Simulate"], ["yaml", "YAML"]];
  tab = tabs.some(([k]) => k === tab) ? tab : tabs[0][0];
  const rowsKey = kind === "lighting" ? "lights" : "covers";
  const family = kind === "lighting" ? "light" : "cover";
  const caps = () => data.capabilities || {};
  const sources = () => data.presence || [];
  const srcById = (id) => sources().find((s) => s.id === id);
  const colLabel = (c) => { const s = srcById(c.source); if (!s) return "(missing source)"; const words = { occupied: "occupied", vacant: "vacant", possible: "possibly occupied" }; return s.kind === "person" ? `${s.name} ${c.state === "occupied" ? "at" : "not at"} ${s.active_state || "…"}` : `${s.name} ${words[c.state]}`; };
  const colId = (c) => `${c.source}:${c.state}`;
  const status = (text, cls = "") => { const el = view.querySelector("#d-status"); if (el) { el.className = `save-status ${cls}`; el.textContent = text; } };
  await loadEntities([...data.device.targets, ...sources().map((s) => s.entity_id).filter(Boolean)]);

  function save() {
    pending = true;
    status("Saving…");
    chain = chain.then(async () => {
      const sent = clone(d);
      try {
        const result = await change("api/designers/save", { device_id: deviceId, designer: sent });
        if (!current()) return;
        data = result;
        if (JSON.stringify(sent) === JSON.stringify(d)) d = clone(result.designer.config);
        pending = false;
        draw();
        status(result.blocking ? `Saved · ${plural(result.blocking, "thing")} to resolve before deploying` : "Saved · deploy the device to apply it", result.blocking ? "" : "ok");
      } catch (err) {
        pending = false;
        status(`Not saved: ${errorText(err)}`, "bad");
      }
    });
    return chain;
  }

  // ------------------------------------------------------------ matrices of this designer
  function matrixSpec(id) {
    const rows = d[rowsKey].map((r) => ({ key: r, label: entityName(r), sub: r }));
    const label = family === "light" ? lightLabel : coverLabel;
    const resolved = (layer) => (r, c) => (((data.designer.view.resolved || {})[layer] || {})[r] || {})[c] || null;
    const layerSpec = (layer, cols, title, kinds) => {
      const m = d[layer];
      return { id, kind: family, title, rows, cols, label, kinds,
        get: (r, c) => ((m.cells[r] || {})[c]) || null,
        set: (r, c, cell) => { m.cells[r] = m.cells[r] || {}; if (cell) m.cells[r][c] = cell; else delete m.cells[r][c]; if (!Object.keys(m.cells[r]).length) delete m.cells[r]; },
        view: resolved(layer),
        rowDefaults: { get: (r) => m.row_defaults[r] || null, set: (r, cell) => { if (cell) m.row_defaults[r] = cell; else delete m.row_defaults[r]; } },
        matrixDefault: { get: () => m.default, set: (cell) => { m.default = cell; } } };
    };
    if (id === "mood") return layerSpec("mood", d.mood.values.map((v) => ({ key: v, label: v })), "Mood matrix: what each light does in each mood", ["default", "none", "off", "on"]);
    if (id === "presence") return layerSpec("presence", d.presence.columns.map((c, i) => ({ key: colId(c), label: colLabel(c), sub: d.presence.policy === "priority" ? `priority ${i + 1}` : "" })), "Presence matrix: what each light does in each situation", ["default", "none", "off", "on", "mood"]);
    if (id === "lux") return layerSpec("lux", d.lux.bands.map((b) => ({ key: b.id, label: b.name || "(unnamed)", sub: b.below != null ? `below ${b.below}` : "the brightest" })), "Light-level matrix: what each light does in each band", ["default", "none", "off", "on", "mood"]);
    if (id === "modes") return layerSpec("modes", d.modes.names.map((n) => ({ key: n, label: n })), "Cover modes: what each cover does in each mode", ["default", "none", "open", "close", "position"]);
    if (id === "entries") {
      return { id, kind: family, title: "Timed entries: what happens at each time", rows, label,
        cols: d.schedule.entries.map((e) => ({ key: e.id, label: e.name || `at ${(e.at || "").slice(0, 5)}`, sub: e.mode ? `mode ${e.mode}` : "" })),
        kinds: family === "light" ? ["none", "off", "on", "mood"] : ["none", "open", "close", "position"],
        get: (r, c) => ((d.schedule.entries.find((e) => e.id === c) || {}).cells || {})[r] || null,
        set: (r, c, cell) => { const e = d.schedule.entries.find((x) => x.id === c); if (cell) e.cells[r] = cell; else delete e.cells[r]; } };
    }
    return null;
  }
  const selected = () => [...sel.keys].map((k) => k.split(SEP));
  function getCell(spec, r, c) {
    if (r === "__matrix") return spec.matrixDefault.get();
    if (c === "__row") return spec.rowDefaults.get(r);
    return spec.get(r, c);
  }
  function setCell(spec, r, c, cell) {
    if (cell && cell.kind === "default" && (c === "__row" || r === "__matrix")) return;  // a default is never "use default"
    if (r === "__matrix") spec.matrixDefault.set(cell);
    else if (c === "__row") spec.rowDefaults.set(r, cell);
    else spec.set(r, c, cell);
  }
  function editorHtml(spec) {
    if (sel.m !== spec.id || !sel.keys.size) return `<p class="muted small">Select one or more cells to set what happens. Copy and paste work on any selection (a column, the rows of some lights, single cells).</p>`;
    const items = selected();
    const first = getCell(spec, items[0][0], items[0][1]);
    const defaults = items.some(([r, c]) => c === "__row" || r === "__matrix");
    const kinds = spec.kinds.filter((k) => !(defaults && k === "default"));
    const rows = [...new Set(items.map(([r]) => r).filter((r) => r !== "__matrix"))];
    const cap = capsOf(rows.length ? rows : d[rowsKey], caps());
    return family === "light" ? lightEditor(first, kinds, cap, items.length === 1) : coverEditor(first, kinds, cap, items.length === 1);
  }
  const matrixBlock = (id) => { const spec = matrixSpec(id); return `${matrixHtml(spec, sel)}<div class="editor-box" id="ed-${esc(id)}">${editorHtml(spec)}</div>`; };

  // ------------------------------------------------------------ pieces
  const entityBtn = (path, entityId, purpose, label = "Choose…") => `<button type="button" class="entity ${entityId ? "" : "unset"}" data-act="pick" data-path="${esc(path)}" data-purpose="${esc(purpose)}" title="${esc(entityId || "")}">${entityId ? esc(entityName(entityId)) : `UNSET · ${esc(label)}`}</button>${entityId ? ` <button type="button" class="link small" data-act="unpick" data-path="${esc(path)}">clear</button>` : ""}`;
  const toggle = (path, on, text, help = "") => `<label class="switch"><input type="checkbox" data-chg="flag" data-path="${esc(path)}" ${on ? "checked" : ""}> ${esc(text)}</label>${help ? `<div class="help">${esc(help)}</div>` : ""}`;
  const num = (path, value, attrs = "", unit = "") => `<input type="number" step="any" data-chg="num" data-path="${esc(path)}" value="${esc(value ?? "")}" placeholder="unset" ${attrs}>${unit ? ` ${esc(unit)}` : ""}`;
  const get = (path) => path.split("/").reduce((o, k) => (o == null ? o : o[k]), d);
  function set(path, value) { const keys = path.split("/"); const last = keys.pop(); const holder = keys.reduce((o, k) => o[k], d); holder[last] = value; }
  const layerProblems = (layer) => notes(data.designer.problems.filter((p) => p.layer === layer || (!layer && !p.layer)).map((p) => ({ level: p.level === "blocking" ? "error" : p.level, message: p.message })));
  const live = (entityId) => { const e = entities[entityId]; return e ? `<span class="badge">now: ${esc(e.state ?? "?")}${e.unit ? ` ${esc(e.unit)}` : ""}</span>` : ""; };

  function columnsEditor(path, columns, help) {
    const choices = sources().flatMap((s) => (s.kind === "room" ? ["occupied", "vacant", "possible"] : ["occupied", "vacant"]).map((st) => ({ source: s.id, state: st, label: colLabel({ source: s.id, state: st }), area: s.area })));
    const used = new Set(columns.map(colId));
    return `<div class="columns-editor"><ol class="plain cols">${columns.map((c, i) => `<li class="row"><span class="badge">${i + 1}</span><span>${esc(colLabel(c))}${srcById(c.source) && srcById(c.source).area ? ` <span class="muted small">${esc(srcById(c.source).area)}</span>` : ""}</span><span class="spacer"></span>
        <button type="button" class="link" data-act="colmove" data-path="${esc(path)}" data-i="${i}" data-dir="-1" ${i ? "" : "disabled"} aria-label="Move left">←</button><button type="button" class="link" data-act="colmove" data-path="${esc(path)}" data-i="${i}" data-dir="1" ${i < columns.length - 1 ? "" : "disabled"} aria-label="Move right">→</button>
        <button type="button" class="link danger-text" data-act="coldrop" data-path="${esc(path)}" data-i="${i}">Remove</button></li>`).join("") || `<li class="muted">No situations yet.</li>`}</ol>
      <div class="row"><select id="addcol-${esc(path.replace(/\W/g, "-"))}" aria-label="Situation to add"><option value="">Add a situation…</option>${choices.filter((c) => !used.has(colId(c))).map((c) => `<option value="${esc(colId(c))}">${esc(c.label)}${c.area ? ` (${esc(c.area)})` : ""}</option>`).join("")}</select>
        <button type="button" data-act="coladd" data-path="${esc(path)}">Add</button><a class="button" href="#/presence">Presence sources…</a></div>
      ${help ? `<p class="muted small">${esc(help)}</p>` : ""}</div>`;
  }

  // ------------------------------------------------------------ lighting tabs
  function lightingTab() {
    const enabled = ["mood", "presence", "lux"].filter((l) => d[l].enabled);
    const NAMES = { mood: "Mood", presence: "Presence", lux: "Light level" };
    const primary = d.primary || (enabled.length === 1 ? enabled[0] : null);
    if (tab === "combine") {
      const lights = data.device.targets.filter((t) => t.startsWith("light."));
      const roles = ["mood", "presence", "lux"].map((l) => {
        if (!d[l].enabled) return `<div class="role off"><strong>${NAMES[l]}</strong><span class="muted small">off</span></div>`;
        if (l === primary) return `<div class="role decides"><strong>${NAMES[l]}</strong><span>DECIDES: its source changing makes the lights act, by its cells</span></div>`;
        if (l === "mood") return `<div class="role palette"><strong>Mood</strong><span>PALETTE: supplies the setting where a cell says “Use mood”${d.mood.apply_on_change ? "; re-applied when the mood changes" : ""}</span></div>`;
        const acts = d[l].apply_on_change ? "; also acts when it changes" : "; never sends a command by itself";
        return `<div class="role gate"><strong>${NAMES[l]}</strong><span>PERMISSION: ${l === "lux" ? "allows ON / OFF only in the bands you tick" : "allows ON only while the situations you tick hold"}${acts}</span></div>`;
      }).join(`<span class="arrow" aria-hidden="true">→</span>`);
      return `<div class="card"><h2>Lights (rows)</h2>
          ${lights.map((l) => `<label class="pick"><input type="checkbox" data-chg="row" data-id="${esc(l)}" ${d.lights.includes(l) ? "checked" : ""}>${icon("light")}<span>${esc(entityName(l))}<br><span class="mono">${esc(l)}</span></span><span class="spacer"></span><span class="muted small">${esc(capText(caps()[l]))}</span></label>`).join("")}
          <p class="muted small">Only the lights ticked here take part. Lights of the device that are not ticked keep their ordinary rules only.</p></div>
        <div class="card"><h2>Controls</h2>
          <div class="field"><label>Mood matrix</label>${toggle("mood/enabled", d.mood.enabled, "On")}</div>
          <div class="field"><label>Presence matrix</label>${toggle("presence/enabled", d.presence.enabled, "On")}</div>
          <div class="field"><label>Light-level matrix</label>${toggle("lux/enabled", d.lux.enabled, "On")}</div>
          <div class="field"><label>Schedule</label>${toggle("schedule/enabled", d.schedule.enabled, "On")}</div>
          <p class="muted small">Nothing is turned on for you. A control that is off generates nothing.</p></div>
        <div class="card" id="combination"><h2>How they work together</h2>
          ${enabled.length > 1 ? `<div class="field"><label for="primary">Decides what the lights do</label><select id="primary" data-chg="primary"><option value="">Choose…</option>${opt(enabled.map((l) => [l, NAMES[l]]), d.primary)}</select>
            <div class="help">Exactly one control decides. No precedence is assumed between them: the others are the mood palette or permissions.</div></div>` : ""}
          <div class="roles">${roles}</div>
          ${primary === "presence" ? `<div class="field"><label for="policy">When situations overlap</label><select id="policy" data-chg="text" data-path="presence/policy"><option value="">Choose… (needed when two can hold at once)</option>${opt([["priority", "Priority: the left column wins"], ["exclusive", "Nothing while ambiguous"], ["compatible", "Both, when they want the same"]], d.presence.policy)}</select>
            <div class="help">Priority uses the column order of the presence matrix: while a column holds whose cell for a light is set (also through a default) or NO ACTION, the columns to its right do not act on that light; an UNSET cell does not hold them back. “Nothing while ambiguous” acts only when one such column holds. “Both” reports a conflict when two situations want different things.</div></div>
            <div class="field"><label>Occupancy rechecks</label>${toggle("rechecks", d.rechecks, "Correct lost commands", "Only for binary sensors without delays, with the safety rules of the ordinary occupancy rechecks (never a toggle, never against a manual change, not right after a restart, not while the source is stale).")}</div>` : ""}
          ${layerProblems(null)}
          <p class="muted small">${plural(data.designer.rules.length, "generated rule")} in the device's one automation (${plural(data.own_rules, "ordinary rule")} besides). <a href="#/devices/${enc(deviceId)}">Advanced rules and deployment</a></p></div>
        ${conversionCard()}`;
    }
    if (tab === "mood") {
      const options = ((entities[d.mood.entity_id] || {}).attributes || {}).options || [];
      return `<div class="card">${d.mood.enabled ? "" : `<div class="notice warning">The mood matrix is off (Combination). Its cells are kept but generate nothing.</div>`}
          <div class="field"><label>Mood source</label><span>${entityBtn("mood/entity_id", d.mood.entity_id, "mood", "the mood selector")} ${live(d.mood.entity_id)}</span>
            <div class="help">An ordinary dropdown helper (input_select) or any entity whose state is the mood. Chosen by you only.</div></div>
          <div class="field"><label>Mood values (columns)</label><span class="days">${[...new Set([...options, ...d.mood.values])].map((o) => `<label><input type="checkbox" data-chg="value" value="${esc(o)}" ${d.mood.values.includes(o) ? "checked" : ""}>${esc(o)}</label>`).join("") || `<span class="muted">Choose the source to see its options.</span>`}</span></div>
          <div class="field"><label>Automatic mode</label><span>${d.mood.auto ? `“${esc(d.mood.auto.value)}” means: read the effective mood from ${entityBtn("mood/auto/entity_id", d.mood.auto.entity_id, "mood", "the effective mood")} <button type="button" class="link" data-act="noauto">remove</button>`
    : `<select data-chg="auto"><option value="">None</option>${opt(options.filter((o) => !d.mood.values.includes(o)).map((o) => [o, `“${o}” is a mode, not a mood`]), "")}</select>`}</span>
            <div class="help">For a selector with a value such as “Auto” that hands the choice to something else: that value is then no column.</div></div>
          ${d.primary !== "mood" && d.lights.length ? `<div class="field"><label>When the mood changes</label>${toggle("mood/apply_on_change", d.mood.apply_on_change, "Re-apply it to lights whose situation uses the mood")}</div>` : ""}
          ${layerProblems("mood")}</div>
        <div class="card">${d.mood.values.length && d.lights.length ? matrixBlock("mood") : `<p class="muted">Choose lights and mood values to get the matrix.</p>`}</div>`;
    }
    if (tab === "presence") {
      const isPrimary = primary === "presence";
      const gate = !isPrimary && d.presence.enabled ? `<div class="card"><h2>Permission: ON only while …</h2><div class="tscroll"><table class="matrix"><thead><tr><th></th>${d.presence.columns.map((c) => `<th>${esc(colLabel(c))}</th>`).join("")}<th>Not restricted</th></tr></thead><tbody>
          ${d.lights.map((l) => { const g = d.presence.gate[l]; return `<tr><th scope="row">${esc(entityName(l))}</th>${d.presence.columns.map((c) => `<td><input type="checkbox" data-chg="pgate" data-light="${esc(l)}" data-col="${esc(colId(c))}" ${g && g.includes(colId(c)) ? "checked" : ""} ${g ? "" : "disabled"} aria-label="Allow ON while ${esc(colLabel(c))}"></td>`).join("")}<td><input type="checkbox" data-chg="pgate-free" data-light="${esc(l)}" ${g ? "" : "checked"} aria-label="Not restricted"></td></tr>`; }).join("")}</tbody></table></div>
          <div class="field"><label>When a situation starts</label>${toggle("presence/apply_on_change", d.presence.apply_on_change, "Also act (apply the deciding matrix)", "Off: the permission never sends a command by itself.")}</div></div>` : "";
      return `<div class="card">${d.presence.enabled ? "" : `<div class="notice warning">The presence matrix is off (Combination).</div>`}
          <h2>Situations (columns)</h2>${columnsEditor("presence/columns", d.presence.columns, "Each situation comes from a presence source: a room, a named zone, a position zone or a person. A zone becoming vacant does not make its room vacant. The order is the priority when “Priority” is chosen.")}
          ${layerProblems("presence")}</div>
        ${isPrimary && d.presence.columns.length && d.lights.length ? `<div class="card">${matrixBlock("presence")}</div>` : gate}`;
    }
    if (tab === "lux") {
      const isPrimary = primary === "lux";
      const lux = entities[d.lux.entity_id];
      const value = lux ? Number(lux.state) : NaN;
      const nowBand = Number.isFinite(value) ? d.lux.bands.find((b) => b.below == null || value < b.below) : null;
      const bands = d.lux.bands.map((b, i) => `<li class="row band"><input type="text" data-chg="text" data-path="lux/bands/${i}/name" value="${esc(b.name)}" maxlength="40" aria-label="Band name">
          ${i < d.lux.bands.length - 1 ? `<span>below</span>${num(`lux/bands/${i}/below`, b.below, 'min="0" aria-label="Upper limit"', (lux && lux.unit) || "")}` : `<span class="muted">and brighter</span>`}
          ${nowBand && nowBand.id === b.id ? `<span class="badge">now</span>` : ""}<span class="spacer"></span><button type="button" class="link danger-text" data-act="banddrop" data-i="${i}" ${d.lux.bands.length > 2 ? "" : "disabled"}>Remove</button></li>`).join("");
      const gate = !isPrimary && d.lux.enabled ? `<div class="card"><h2>Permissions</h2><p class="muted small">“Allow ON when dark” only permits: it never switches a light on by itself (unless you tick “also act” below).</p>
          <div class="tscroll"><table class="matrix"><thead><tr><th></th>${d.lux.bands.map((b) => `<th>${esc(b.name)}</th>`).join("")}</tr></thead><tbody>
          ${d.lights.flatMap((l) => ["on", "off"].map((k) => { const g = (d.lux.gate[l] || {})[k]; return `<tr><th scope="row">${esc(entityName(l))} <span class="badge">${k.toUpperCase()}</span><br><label class="small"><input type="checkbox" data-chg="lgate-free" data-light="${esc(l)}" data-k="${k}" ${g == null ? "checked" : ""}> not restricted</label></th>${d.lux.bands.map((b) => `<td><input type="checkbox" data-chg="lgate" data-light="${esc(l)}" data-k="${k}" data-band="${esc(b.id)}" ${g && g.includes(b.id) ? "checked" : ""} ${g == null ? "disabled" : ""} aria-label="Allow ${k} in ${esc(b.name)}"></td>`).join("")}</tr>`; })).join("")}</tbody></table></div>
          <div class="field"><label>When the light level changes</label>${toggle("lux/apply_on_change", d.lux.apply_on_change, "Also act (apply the deciding matrix)")}</div></div>` : "";
      return `<div class="card">${d.lux.enabled ? "" : `<div class="notice warning">The light-level matrix is off (Combination).</div>`}
          <div class="field"><label>Light-level sensor</label><span>${entityBtn("lux/entity_id", d.lux.entity_id, "numeric", "the lux sensor")} ${live(d.lux.entity_id)}</span></div>
          <h3>Bands, from dark to bright</h3><ol class="plain">${bands}</ol>
          <div class="actions"><button type="button" data-act="bandadd" ${d.lux.bands.length < 6 ? "" : "disabled"}>Add a band</button></div>
          <p class="muted small">You choose the names and limits; no threshold is assumed.</p>
          <div class="field"><label>Hysteresis</label><span>${num("lux/hysteresis", d.lux.hysteresis, 'min="0"', (lux && lux.unit) || "")}</span><div class="help">A brighter band starts only this much above its limit (against flicker around a threshold). Checks at other moments use the plain limits.</div></div>
          <div class="field"><label>Delay</label><span>${num("lux/delay_seconds", d.lux.delay_seconds, 'min="0" max="3600"', "s")}</span><div class="help">How long a new level must hold before the lights react.</div></div>
          ${layerProblems("lux")}</div>
        ${isPrimary && d.lux.bands.length && d.lights.length ? `<div class="card">${matrixBlock("lux")}</div>` : gate}`;
    }
    if (tab === "schedule") return scheduleTab();
    if (tab === "override") return overrideTab();
    return null;
  }

  function conversionCard() {
    const c = data.conversion;
    if (!c) return "";
    return `<div class="card" id="conversion"><h2>The new Lighting Designer</h2>
      <p class="small">This designer is of version 1 (Phase 0.3). It keeps working exactly as before. Version 2 has Truth and Brightness Matrices, Colour &amp; Mood, Switches &amp; Remotes, Advanced Rules and an explicit priority, and the lights return to the calculated state by themselves when an override ends.</p>
      ${c.refused.length ? `<div class="notice warning"><strong>Not converted automatically:</strong><ul>${c.refused.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>`
    : `<p class="small"><strong>What changes:</strong></p><ul class="small">${c.changes.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
         <div class="actions"><button data-act="convert">Convert to version 2…</button></div>`}
      <p class="muted small">Nothing reaches Home Assistant before you deploy the device; the automation deployed now keeps running until then. The automatic backups keep this version.</p></div>`;
  }

  function scheduleTab() {
    const s = d.schedule;
    const windows = s.windows.map((w, i) => `<li class="row"><span>from</span><input type="time" data-chg="time" data-path="schedule/windows/${i}/after" value="${esc((w.after || "").slice(0, 5))}" aria-label="From">
        <span>until</span><input type="time" data-chg="time" data-path="schedule/windows/${i}/before" value="${esc((w.before || "").slice(0, 5))}" aria-label="Until">
        <span class="days">${["mon", "tue", "wed", "thu", "fri", "sat", "sun"].map((day) => `<label><input type="checkbox" data-chg="wday" data-path="schedule/windows/${i}/weekdays" value="${day}" ${w.weekdays.includes(day) ? "checked" : ""}>${day}</label>`).join("")}</span>
        <button type="button" class="link danger-text" data-act="windrop" data-i="${i}">Remove</button></li>`).join("");
    const entries = s.entries.map((e, i) => `<li class="row"><input type="text" data-chg="text" data-path="schedule/entries/${i}/name" value="${esc(e.name)}" placeholder="name" maxlength="40" aria-label="Name">
        <span>at</span><input type="time" data-chg="time" data-path="schedule/entries/${i}/at" value="${esc((e.at || "").slice(0, 5))}" aria-label="Time">
        <span class="days">${["mon", "tue", "wed", "thu", "fri", "sat", "sun"].map((day) => `<label><input type="checkbox" data-chg="wday" data-path="schedule/entries/${i}/weekdays" value="${day}" ${e.weekdays.includes(day) ? "checked" : ""}>${day}</label>`).join("")}</span>
        ${kind === "cover" && d.modes.names.length ? `<select data-chg="text" data-path="schedule/entries/${i}/mode" aria-label="Apply a mode"><option value="">its own cells</option>${opt(d.modes.names.map((m) => [m, `apply mode ${m}`]), e.mode)}</select>` : ""}
        <button type="button" class="link danger-text" data-act="entrydrop" data-i="${i}">Remove</button></li>`).join("");
    return `<div class="card">${s.enabled ? "" : `<div class="notice warning">The schedule is off (${kind === "lighting" ? "Combination" : "Covers & controls"}).</div>`}
        <h2>${kind === "lighting" ? "Active only during" : "Sun shading only during"}</h2><ul class="plain">${windows || `<li class="muted">Always (no time window).</li>`}</ul>
        <div class="actions"><button type="button" data-act="winadd">Add a time window</button></div>
        <h2>Timed entries</h2><ul class="plain">${entries || `<li class="muted">None.</li>`}</ul>
        <div class="actions"><button type="button" data-act="entryadd">Add a timed entry</button></div>
        <p class="muted small">A timed entry acts at its time (only the manual override stops it). The time windows limit when the ${kind === "lighting" ? "deciding matrix acts" : "sun shading acts"}.</p>${layerProblems("schedule")}</div>
      ${s.entries.length && d[rowsKey].length ? `<div class="card">${matrixBlock("entries")}</div>` : ""}`;
  }

  function overrideTab() {
    const o = d.override;
    return `<div class="card"><h2>Manual override</h2>
        <div class="field"><label>Pause while this is on</label><span>${entityBtn("override/entity_id", o.entity_id, "override", "an override switch")}</span><div class="help">A toggle helper, switch or binary sensor. While it is on, nothing of this designer acts.</div></div>
        <div class="field"><label>Pause after a change by hand</label><span>${num("override/manual_minutes", o.manual_minutes, 'min="1" max="1440"', "min")}</span>
          <div class="help">A ${family} changed by a person or a wall switch (not by an automation or script) is left alone this long. After a restart of Home Assistant every state counts as changed by hand for this time.</div></div>
        <p class="muted small">The device's ordinary rules keep their own override settings. Neither the designer nor the ordinary rules have priority by themselves.</p></div>`;
  }

  // ------------------------------------------------------------ cover tabs
  function coverTab() {
    if (tab === "covers") {
      const covers = data.device.targets.filter((t) => t.startsWith("cover."));
      return `<div class="card"><h2>Covers (rows)</h2>${covers.map((c) => `<label class="pick"><input type="checkbox" data-chg="row" data-id="${esc(c)}" ${d.covers.includes(c) ? "checked" : ""}>${icon("cover")}<span>${esc(entityName(c))}<br><span class="mono">${esc(c)}</span></span><span class="spacer"></span><span class="muted small">${esc(capText(caps()[c]))}</span></label>`).join("")}</div>
        <div class="card"><h2>Controls</h2>
          <div class="field"><label>Sun position</label>${toggle("sun/enabled", d.sun.enabled, "On")}</div>
          <div class="field"><label>Cover modes</label>${toggle("modes/enabled", d.modes.enabled, "On")}</div>
          <div class="field"><label>Schedule</label>${toggle("schedule/enabled", d.schedule.enabled, "On")}</div>
          <p class="muted small">Nothing is turned on for you. When sun shading and a mode both want a cover, you choose per mode which wins (Cover Moods).</p>${layerProblems(null)}
          <p class="muted small">${plural(data.designer.rules.length, "generated rule")} in the device's one automation. <a href="#/devices/${enc(deviceId)}">Advanced rules and deployment</a></p></div>`;
    }
    if (tab === "sun") return sunTab();
    if (tab === "modes") {
      const m = d.modes;
      const policy = d.sun.enabled ? `<div class="card"><h2>Sun shading during each mode</h2>${m.names.map((n) => `<div class="field"><label>${esc(n)}</label><select data-chg="policy" data-mode="${esc(n)}"><option value="">Choose… (needed when both want a cover)</option>${opt([["mode_wins", "The mode wins: no sun shading"], ["sun_wins", "Sun shading wins; the mode returns afterwards"]], m.sun_policy[n])}</select></div>`).join("") || `<p class="muted">No modes yet.</p>`}</div>` : "";
      return `<div class="card">${m.enabled ? "" : `<div class="notice warning">Cover modes are off (Covers & controls).</div>`}
          <h2>Modes</h2><ul class="plain">${m.names.map((n, i) => `<li class="row"><input type="text" data-chg="modename" data-i="${i}" value="${esc(n)}" maxlength="60" aria-label="Mode name"><span class="spacer"></span><button type="button" class="link danger-text" data-act="modedrop" data-i="${i}">Remove</button></li>`).join("") || `<li class="muted">No modes yet.</li>`}</ul>
          <div class="row"><input type="text" id="newmode" maxlength="60" placeholder="New mode name"><button type="button" data-act="modeadd">Add</button>
            <span class="muted small">Ideas: ${["Daylight", "Privacy", "Sun Shading", "Sleep", "View"].filter((n) => !m.names.includes(n)).map((n) => `<button type="button" class="link small" data-act="modeidea" data-name="${esc(n)}">${esc(n)}</button>`).join(" ")}</span></div>
          <div class="field"><label for="driver">A mode is active when</label><select id="driver" data-chg="text" data-path="modes/driver"><option value="">Only applied by timed entries</option>${opt([["selector", "a cover-mode selector shows its name"], ["mood", "a household mood mapped to it is active"]], m.driver)}</select></div>
          ${m.driver === "selector" ? `<div class="field"><label>Cover-mode selector</label><span>${entityBtn("modes/entity_id", m.entity_id, "mood", "the selector")} ${live(m.entity_id)}</span><div class="help">Its option names must be the mode names.</div></div>` : ""}
          ${m.driver === "mood" ? `<p class="muted small">Choose the mapping under Normal Mood Mapping.</p>` : ""}${layerProblems("modes")}</div>
        <div class="card">${m.names.length && d.covers.length ? matrixBlock("modes") : `<p class="muted">Add modes and covers to get the matrix.</p>`}</div>${policy}`;
    }
    if (tab === "mapping") {
      const m = d.modes;
      const options = ((entities[m.mood_entity] || {}).attributes || {}).options || [];
      const values = [...new Set([...options, ...Object.keys(m.mood_map)])];
      return `<div class="card"><h2>Household moods → cover modes</h2>
        <p class="muted small">Household moods and cover modes are separate. Map only what you want; an unmapped mood does nothing to the covers. ${m.driver === "mood" ? "" : "<strong>The modes do not follow the mood now</strong> (Cover Moods: “A mode is active when”)."}</p>
        <div class="field"><label>Mood selector</label><span>${entityBtn("modes/mood_entity", m.mood_entity, "mood", "the mood selector")} ${live(m.mood_entity)}</span></div>
        ${values.length ? `<div class="tscroll"><table id="mood-map"><thead><tr><th>Mood</th><th>Cover mode</th></tr></thead><tbody>${values.map((v) => `<tr><td>${esc(v)}</td><td><select data-chg="map" data-value="${esc(v)}" aria-label="Mode for ${esc(v)}"><option value="">UNSET (no change)</option>${opt(m.names.map((n) => [n, n]), m.mood_map[v])}</select></td></tr>`).join("")}</tbody></table></div>` : `<p class="muted">Choose the mood selector to see its moods.</p>`}
        ${layerProblems("modes")}</div>`;
    }
    if (tab === "presence") {
      return `<div class="card"><h2>Sun shading only while …</h2>${columnsEditor("sun/presence", d.sun.presence, "Leave empty for sun shading regardless of presence. With situations, the sun shading acts only while one of them holds (for example: a zone occupied, or everybody away).")}${layerProblems("sun")}</div>`;
    }
    if (tab === "schedule") return scheduleTab();
    if (tab === "override") return overrideTab();
    return null;
  }

  function sunTab() {
    const s = d.sun;
    const shared = `<div class="card"><h2>All windows</h2>${s.enabled ? "" : `<div class="notice warning">Sun position is off (Covers & controls).</div>`}
        <div class="field"><label>Hysteresis</label><span>${num("sun/hysteresis", s.hysteresis, 'min="0" max="45"', "°")}</span><div class="help">The sun counts as gone only this far outside the sector and the elevation range.</div></div>
        <div class="field"><label>Minimum change</label><span>${num("sun/min_change", s.min_change, 'min="1" max="100"', "%")}</span><div class="help">Smaller changes of position are not made (needed for a position that follows the sun).</div></div>
        <div class="field"><label>Minimum time between movements</label><span>${num("sun/min_interval_minutes", s.min_interval_minutes, 'min="1" max="1440"', "min")}</span></div>
        <div class="field"><label>Only when the light level is above</label><span>${entityBtn("sun/lux/entity_id", (s.lux || {}).entity_id, "numeric", "a lux sensor")} ${s.lux ? num("sun/lux/above", s.lux.above) + " hysteresis " + num("sun/lux/hysteresis", s.lux.hysteresis, 'min="0"') : ""}</span></div>
        <div class="field"><label>Only when the outdoor temperature is above</label><span>${entityBtn("sun/temperature/entity_id", (s.temperature || {}).entity_id, "numeric", "a temperature sensor")} ${s.temperature ? num("sun/temperature/above", s.temperature.above, "", "°") : ""}</span></div>
        <div class="field"><label>Only in these weather conditions</label><span>${entityBtn("sun/weather/entity_id", (s.weather || {}).entity_id, "weather", "a weather entity")} ${s.weather ? `<span class="days">${["sunny", "clear-night", "partlycloudy", "cloudy", "fog", "rainy", "snowy", "windy", "exceptional"].map((w) => `<label><input type="checkbox" data-chg="weather" value="${w}" ${(s.weather.states || []).includes(w) ? "checked" : ""}>${w}</label>`).join("")}</span>` : ""}</span><div class="help">The current condition of the weather entity (forecasts need a service call with a response, which this generated automation does not make).</div></div>
        <div class="field"><label>When sun or sensor data are unavailable</label><span>${coverCellSelect("sun/fallback", s.fallback)}</span><div class="help">UNSET and NO ACTION: nothing moves.</div></div>
        <h3>Weather protection (awnings and other exterior covers)</h3>
        <div class="field"><label>Wind sensor and limit</label><span>${entityBtn("sun/safety/wind_entity", s.safety.wind_entity, "numeric", "a wind sensor")} ${num("sun/safety/wind_above", s.safety.wind_above, 'min="0"')}</span></div>
        <div class="field"><label>Rain sensor</label><span>${entityBtn("sun/safety/rain_entity", s.safety.rain_entity, "binary", "a rain sensor")}</span></div>
        <div class="field"><label>Own protection</label>${toggle("sun/safety/acknowledged", s.safety.acknowledged, "The weather-sensitive covers have their own independent wind and rain protection")}</div>
        ${layerProblems("sun")}</div>`;
    const windows = d.covers.map((c) => sunWindow(c)).join("");
    return `<div class="card" id="sun-now">${sunNow()}</div>${windows}${shared}`;
  }

  function sunNow() {
    if (!sun) return `<p class="muted">Reading the sun…</p>`;
    if (!sun.available) return `<div class="notice warning">Home Assistant reports no sun position (sun.sun). The sun shading cannot work without it.</div>`;
    return `<div class="row"><strong>The sun now</strong><span>azimuth ${sun.azimuth}° (Home Assistant) = <strong>${Math.round(sun.azimuth_user)}°</strong> (0° = South)</span><span>elevation <strong>${sun.elevation}°</strong></span><span class="muted small">${esc(sun.convention)}</span></div>`;
  }

  function sunWindow(cover) {
    const w = d.sun.windows[cover];
    const cap = caps()[cover] || {};
    if (!w) return `<div class="card"><div class="row"><h2>${esc(entityName(cover))}</h2><span class="spacer"></span><button type="button" data-act="winsetup" data-cover="${esc(cover)}">Set up its window</button></div><p class="muted small">No sun shading for this cover.</p></div>`;
    const p = `sun/windows/${cover}`;
    const response = (which, r, allowClose) => {
      const k = r ? r.kind : "";
      const kinds = [["", "Nothing"], ["fixed", "A fixed value"], ...(allowClose ? [["close", "Close"]] : []), ["curve", "Follows the sun"]];
      let body = "";
      if (k === "fixed") body = num(`${p}/${which}/value`, r.value, 'min="0" max="100"', "%");
      if (k === "curve") body = `<select data-chg="text" data-path="${p}/${which}/basis" aria-label="Angle"><option value="">angle…</option>${opt([["elevation", "sun elevation"], ["profile", "profile angle"]], r.basis)}</select>
        at ${num(`${p}/${which}/points/0/0`, (r.points[0] || [])[0], 'min="-10" max="90" aria-label="First angle"', "°")} → ${num(`${p}/${which}/points/0/1`, (r.points[0] || [])[1], 'min="0" max="100" aria-label="First position"', "%")},
        at ${num(`${p}/${which}/points/1/0`, (r.points[1] || [])[0], 'min="-10" max="90" aria-label="Second angle"', "°")} → ${num(`${p}/${which}/points/1/1`, (r.points[1] || [])[1], 'min="0" max="100" aria-label="Second position"', "%")}`;
      return `<select data-chg="response" data-path="${p}/${which}" aria-label="${which}">${opt(kinds, k)}</select> ${body}`;
    };
    const center = (w.orientation ?? 0) + (w.offset || 0);
    return `<div class="card sunwin" data-cover="${esc(cover)}"><div class="row"><h2>${esc(entityName(cover))}</h2><span class="muted small">${esc(capText(cap))}</span><span class="spacer"></span><button type="button" class="link danger-text" data-act="windel" data-cover="${esc(cover)}">No sun shading for this cover</button></div>
      <div class="sungrid"><div>${compass(w, cover)}<p class="muted small">Tap the dial to turn the window towards that direction. N is up; 0° = South.</p></div>
        <div>${elevationArc(w)}${responseChart(w)}</div></div>
      <div class="field"><label>The window faces</label><span>${num(`${p}/orientation`, w.orientation, 'min="-180" max="180" aria-label="Orientation"', "°")} <span class="muted small">${w.orientation == null ? "" : `${compassWord(w.orientation)} · Home Assistant azimuth ${Math.round(((w.orientation + 180) % 360 + 360) % 360)}°`}</span></span></div>
      <div class="field"><label>Fine adjustment</label><span>${num(`${p}/offset`, w.offset, 'min="-90" max="90"', "°")}</span><div class="help">An offset added to the direction, for example after checking it with the sun.</div></div>
      <div class="field"><label>Sector</label><span>${num(`${p}/left`, w.left, 'min="0" max="89" aria-label="Counter-clockwise"', "°")} counter-clockwise, ${num(`${p}/right`, w.right, 'min="0" max="89" aria-label="Clockwise"', "°")} clockwise of it</span><div class="help">Where sunlight comes in (obstacles, walls, eaves). Now: sector ${w.left != null && w.right != null && w.orientation != null ? `${Math.round(center - w.left)}° … ${Math.round(center + w.right)}°` : "UNSET"}.</div></div>
      <div class="field"><label>Sun elevation</label><span>from ${num(`${p}/elevation_min`, w.elevation_min, 'min="-10" max="90"', "°")} to ${num(`${p}/elevation_max`, w.elevation_max, 'min="-10" max="90"', "°")}</span><div class="help">The lowest is required (0° is the horizon); without a highest, any elevation above it counts.</div></div>
      <div class="field"><label>Position in the sun</label><span>${response("position", w.position, true)}</span></div>
      ${cap.tilt || cap.unknown ? `<div class="field"><label>Tilt in the sun</label><span>${response("tilt", w.tilt, false)}</span></div>` : ""}
      <div class="field"><label>When the sun has gone</label><span>${coverCellSelect(`${p}/leave`, w.leave)}</span></div>
      <div class="field"><label>Weather-sensitive</label>${toggle(`${p}/weather_sensitive`, w.weather_sensitive, "An awning or another exterior cover", cap.weather_sensitive ? "Home Assistant reports this cover as an awning." : "")}</div>
      <div class="preview small" id="pv-${esc(short(cover))}">${previewText(w)}</div></div>`;
  }

  function coverCellSelect(path, cell) {
    const k = cell ? cell.kind : "";
    return `<select data-chg="ccell" data-path="${esc(path)}" aria-label="What happens">${opt([["", "UNSET"], ["none", "No action"], ["open", "Open"], ["close", "Close"], ["position", "Position"]], k)}</select>${k === "position" ? " " + num(`${path}/position`, cell.position, 'min="0" max="100"', "%") : ""}`;
  }

  // the sun geometry, as solar.py computes it
  const norm = (a) => { let v = ((a % 360) + 360) % 360; if (v > 180) v -= 360; return v; };
  const toHa = (u) => ((u + 180) % 360 + 360) % 360;
  function inSector(az, w) { const c = (w.orientation || 0) + (w.offset || 0); const rel = norm(norm(az - 180) - c); return rel > -w.left && rel < w.right; }
  function respond(r, el, az, w) {
    if (!r || r.kind !== "curve" || r.points.length !== 2 || r.points.flat().some((v) => v == null)) return r && r.kind === "fixed" ? r.value : r && r.kind === "close" ? 0 : null;
    const c = (w.orientation || 0) + (w.offset || 0);
    const rel = norm(norm(az - 180) - c) * Math.PI / 180;
    const a = r.basis === "elevation" ? el : Math.atan2(Math.tan(el * Math.PI / 180), Math.cos(rel)) * 180 / Math.PI;
    const [[a1, p1], [a2, p2]] = [...r.points].sort((x, y) => x[0] - y[0]);
    const v = a <= a1 ? p1 : a >= a2 ? p2 : p1 + (a - a1) * (p2 - p1) / (a2 - a1);
    return Math.round(Math.max(0, Math.min(100, v)));
  }
  function previewText(w) {
    if (w.orientation == null || w.left == null || w.right == null || w.elevation_min == null) return `<span class="muted">UNSET: direction, sector and lowest elevation are needed before anything is calculated.</span>`;
    if (!sun || !sun.available) return "";
    const inside = inSector(sun.azimuth, w) && sun.elevation > w.elevation_min && (w.elevation_max == null || sun.elevation < w.elevation_max);
    const pos = inside ? respond(w.position, sun.elevation, sun.azimuth, w) : null;
    const path = (sun.path || []).filter((pt) => pt.elevation > 0);
    const day = path.map((pt) => ({ at: pt.at.slice(11, 16), on: inSector(pt.azimuth, w) && pt.elevation > w.elevation_min && (w.elevation_max == null || pt.elevation < w.elevation_max), pos: respond(w.position, pt.elevation, pt.azimuth, w) }));
    const first = day.find((x) => x.on);
    const last = [...day].reverse().find((x) => x.on);
    return `<strong>Preview (a calculation, not a test):</strong> now the sun is ${inside ? `in this window's sector: position ${pos == null ? "—" : `${pos}%`}` : "not shining in"}. ${first ? `Today it shines in from about ${first.at} to ${last.at} UTC.` : "Today it does not shine in."}`;
  }
  function compassWord(u) { const ha = toHa(u); return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(ha / 45) % 8]; }
  const xy = (az, r) => [Math.sin(az * Math.PI / 180) * r, -Math.cos(az * Math.PI / 180) * r];
  function compass(w, cover) {
    const R = 90;
    const center = (w.orientation ?? 0) + (w.offset || 0);
    let wedge = "";
    if (w.orientation != null && w.left != null && w.right != null) {
      const a1 = toHa(center - w.left);
      const a2 = toHa(center + w.right);
      const [x1, y1] = xy(a1, R);
      const [x2, y2] = xy(a2, R);
      const large = w.left + w.right > 180 ? 1 : 0;
      wedge = `<path class="wedge" d="M0 0 L${x1.toFixed(1)} ${y1.toFixed(1)} A${R} ${R} 0 ${large} 1 ${x2.toFixed(1)} ${y2.toFixed(1)} Z"/>`;
    }
    const facing = w.orientation != null ? (() => { const [x, y] = xy(toHa(center), R - 8); return `<line class="facing" x1="0" y1="0" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}"/><circle class="facing-dot" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="6"/>`; })() : "";
    const path = sun && sun.path ? sun.path.filter((p) => p.elevation > 0).map((p) => xy(p.azimuth, R * (90 - p.elevation) / 90).map((v) => v.toFixed(1)).join(",")).join(" ") : "";
    const now = sun && sun.available && sun.elevation > 0 ? (() => { const [x, y] = xy(sun.azimuth, R * (90 - sun.elevation) / 90); return `<circle class="sun" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="7"><title>The sun now</title></circle>`; })() : "";
    return `<svg class="compass" viewBox="-110 -110 220 220" role="img" aria-label="Compass: window direction and sector" data-act="compass" data-cover="${esc(cover)}">
      <circle class="ring" r="${R}"/><circle class="ring faint" r="${R * 2 / 3}"/><circle class="ring faint" r="${R / 3}"/>
      ${wedge}${path ? `<polyline class="path" points="${path}"/>` : ""}${facing}${now}
      <text x="0" y="-96" class="cdir">N</text><text x="100" y="4" class="cdir">E</text><text x="0" y="106" class="cdir">S</text><text x="-100" y="4" class="cdir">W</text></svg>`;
  }
  function elevationArc(w) {
    const R = 80;
    const ray = (deg, cls) => { const a = deg * Math.PI / 180; return `<line class="${cls}" x1="0" y1="0" x2="${(Math.cos(a) * R).toFixed(1)}" y2="${(-Math.sin(a) * R).toFixed(1)}"/>`; };
    return `<svg class="elev" viewBox="-10 -90 100 100" role="img" aria-label="Sun elevation range"><path class="ring" d="M${R} 0 A${R} ${R} 0 0 0 0 ${-R}"/><line class="ring" x1="0" y1="0" x2="${R}" y2="0"/>
      ${w.elevation_min != null ? ray(w.elevation_min, "lim") : ""}${w.elevation_max != null ? ray(w.elevation_max, "lim") : ""}${sun && sun.available ? ray(Math.max(-10, sun.elevation), "sunray") : ""}
      <text x="${R - 6}" y="9" class="cdir small">0°</text><text x="2" y="${-R + 8}" class="cdir small">90°</text></svg>`;
  }
  function responseChart(w) {
    const r = w.position;
    if (!r || r.kind !== "curve" || r.points.length !== 2 || r.points.flat().some((v) => v == null)) return "";
    const pts = [];
    for (let a = 0; a <= 90; a += 5) { const [[a1, p1], [a2, p2]] = [...r.points].sort((x, y) => x[0] - y[0]); const v = a <= a1 ? p1 : a >= a2 ? p2 : p1 + (a - a1) * (p2 - p1) / (a2 - a1); pts.push(`${a},${(100 - v).toFixed(1)}`); }
    return `<svg class="curve" viewBox="-8 -4 104 112" role="img" aria-label="Position against the angle"><rect class="ring" x="0" y="0" width="90" height="100" fill="none"/><polyline class="path" points="${pts.join(" ")}"/>
      <text x="0" y="110" class="cdir small">0°</text><text x="80" y="110" class="cdir small">90°</text><text x="-7" y="6" class="cdir small">100</text></svg>`;
  }

  // ------------------------------------------------------------ simulation and YAML
  function simulateTab() {
    if (!sim.inputs) return `<div class="card"><p class="muted">Loading the inputs…</p></div>`;
    const sc = sim.scenario;
    const inputs = sim.inputs.inputs.map((e) => {
      const st = sc.states[e.entity_id];
      const value = st ? st.state : e.state;
      const origin = st && st.origin === "test" ? `<span class="badge warn">test value</span>` : `<span class="badge">live</span>`;
      const sunAttrs = e.entity_id === "sun.sun" ? ` elevation <input type="number" step="any" data-chg="simattr" data-id="sun.sun" data-attr="elevation" value="${esc((st || e).attributes.elevation ?? "")}" aria-label="Sun elevation"> azimuth <input type="number" step="any" data-chg="simattr" data-id="sun.sun" data-attr="azimuth" value="${esc((st || e).attributes.azimuth ?? "")}" aria-label="Sun azimuth">` : "";
      const position = e.entity_id.startsWith("cover.") ? ` position <input type="number" min="0" max="100" data-chg="simattr" data-id="${esc(e.entity_id)}" data-attr="current_position" value="${esc((st || e).attributes.current_position ?? "")}" aria-label="Current position">` : "";
      return `<tr><td>${esc(e.name)}<br><span class="mono">${esc(e.entity_id)}</span><br><span class="muted small">${esc(e.uses.join(", "))}</span></td>
        <td><input type="text" list="sug-${esc(short(e.entity_id))}" data-chg="simstate" data-id="${esc(e.entity_id)}" value="${esc(value ?? "")}" aria-label="State of ${esc(e.name)}"><datalist id="sug-${esc(short(e.entity_id))}">${(e.suggested || []).map((s) => `<option value="${esc(s)}">`).join("")}</datalist>${sunAttrs}${position}
        ${e.entity_id.startsWith("light.") || e.entity_id.startsWith("cover.") ? `<label class="small"><input type="checkbox" data-chg="simmanual" data-id="${esc(e.entity_id)}" ${st && st.manual ? "checked" : ""}> changed by hand just now</label>` : ""}</td><td>${origin}</td></tr>`;
    }).join("");
    const r = sim.result;
    const outcome = r ? `<div class="card" id="sim-result"><h2>Expected result</h2>
        ${r.outcome.map((o) => `<div class="problem" data-entity="${esc(o.entity_id)}"><span class="dot ${o.conflict ? "error" : o.final ? "good" : "info"}"></span><span><strong>${esc(o.name)}</strong>: ${o.final ? esc(callText(o.final)) : "nothing happens"}${o.conflict ? ` <span class="status bad">conflict: ${o.calls.map((c) => esc(`${c.rule}: ${callText(c)}`)).join("; ")}</span>` : ""}</span></div>`).join("")}
        <h3>Rules</h3>${r.rules.filter((x) => x.started).map((x) => `<div class="simrule ${x.result}"><strong>${esc(x.name)}</strong> <span class="badge ${x.result === "runs" ? "" : "warn"}">${{ runs: "runs", blocked: "conditions do not hold", unknown: "cannot be evaluated offline" }[x.result]}</span>
          ${x.failed.length ? `<div class="small">Failed: ${x.failed.map(esc).join("; ")}</div>` : ""}${x.unknown.length ? `<div class="small">Unknown: ${x.unknown.map(esc).join("; ")}</div>` : ""}
          ${x.generated && x.generated.resolution && x.generated.resolution.source && x.generated.resolution.source !== "cell" ? `<div class="small muted">Cell ${esc(x.generated.resolution.state)} · ${esc(x.generated.resolution.source)}</div>` : ""}</div>`).join("") || `<p class="muted">No rule is started by this trigger.</p>`}
        ${r.default ? `<p class="small">Default rule: ${esc(r.default.result)}</p>` : ""}
        ${r.missing.length ? `<p class="small">Not in the scenario (counted as missing, not as unavailable): ${r.missing.map(esc).join(", ")}</p>` : ""}
        ${notes(r.problems.map((p) => ({ level: "error", message: p.message })))}
        <p class="muted small">${esc(r.note)}</p></div>` : "";
    return `<div class="card"><h2>Scenario</h2><p class="muted small">Live values come from Home Assistant; what you change becomes a test value. Nothing is sent to any device.</p>
        <div class="field"><label for="sim-trigger">What just happened</label><select id="sim-trigger" data-chg="simtrigger"><option value="">Choose…</option>${opt(sim.inputs.triggers.map((t) => [t.key, t.text]), sc.trigger)}</select></div>
        <div class="field"><label>Time</label><span><input type="time" data-chg="simtime" value="${esc((sc.time || "").slice(0, 5))}" aria-label="Time"> <select data-chg="simday" aria-label="Weekday">${opt([["", "weekday…"], ...sim.inputs.weekdays.map((w) => [w, w])], sc.weekday)}</select></span></div>
        <div class="tscroll"><table id="sim-inputs"><thead><tr><th>Entity</th><th>State</th><th></th></tr></thead><tbody>${inputs}</tbody></table></div>
        <div class="actions"><button class="primary" data-act="simrun" ${sc.trigger ? "" : "disabled"}>Show what would happen</button><button data-act="simlive">Back to live values</button></div></div>${outcome}`;
  }
  function yamlTab() {
    return `<div class="card"><h2>Generated automation</h2>
      ${data.automation ? `<p class="small">${esc(data.automation.alias)} · mode ${esc(data.automation.mode)} · ${plural(data.automation.rules, "rule")} (${plural(data.designer.rules.length, "from this designer")})</p>` : `<p class="muted">Nothing is generated yet.</p>`}
      ${data.conflicts.length ? notes(data.conflicts.map((c) => ({ level: c.level === "blocking" && !c.acknowledged ? "error" : "warning", message: c.message }))) : ""}
      ${data.yaml ? `<pre class="code" id="d-yaml">${esc(data.yaml)}</pre>` : ""}
      <p class="muted small">A preview of what deploying writes. It is not proof that it works: deploy and test it in Home Assistant. The YAML is generated from the designer; edit the designer, not the YAML.</p>
      <div class="actions"><a class="button primary" href="#/devices/${enc(deviceId)}">Review and deploy on the device page</a><a class="button" href="#/blueprint/${enc(deviceId)}">Export as blueprint</a></div></div>
      <div class="card"><h2>Generated rules</h2><ul class="plain">${data.designer.rules.map((x) => `<li><strong>${esc(x.name)}</strong><br><span class="small muted">${esc(x.summary)}</span></li>`).join("") || `<li class="muted">None.</li>`}</ul></div>`;
  }
  const callText = (c) => `${c.action}${Object.keys(c.data || {}).length ? " " + Object.entries(c.data).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(",") : v}`).join(" ") : ""}`;

  // ------------------------------------------------------------ drawing
  function draw() {
    const problems = data.designer.problems.filter((p) => p.level === "blocking");
    const body = tab === "simulate" ? simulateTab() : tab === "yaml" ? yamlTab() : (kind === "lighting" ? lightingTab() : coverTab());
    view.innerHTML = `<h1><a class="back" href="#/devices/${enc(deviceId)}" aria-label="Back to the device">←</a>${icon(family)}<span id="title">${esc(d.name)}</span> <span class="muted small">${esc(data.device.name)}</span>
        <span class="spacer"></span><label class="switch small"><input type="checkbox" data-chg="flag" data-path="enabled" ${d.enabled ? "checked" : ""}> Enabled</label></h1>
      <nav class="tabs" aria-label="Designer sections">${tabs.map(([k, l]) => `<a href="#/design/${enc(deviceId)}/${enc(designerId)}/${k}" class="${k === tab ? "active" : ""}" ${k === tab ? 'aria-current="page"' : ""}>${esc(l)}</a>`).join("")}<a href="#/devices/${enc(deviceId)}">Advanced Rules</a></nav>
      <div class="row"><span class="save-status" id="d-status"></span></div>
      ${problems.length ? `<div class="notice error" id="d-problems"><strong>Before this can be deployed:</strong><ul>${problems.map((p) => `<li>${esc(p.message)}</li>`).join("")}</ul></div>` : ""}
      ${(data.designer.conflicts || []).filter((c) => !c.acknowledged).length ? `<div class="notice warning">${data.designer.conflicts.map((c) => esc(c.message)).join("<br>")} <a href="#/devices/${enc(deviceId)}">Resolve or confirm on the device page</a></div>` : ""}
      <div id="d-body">${body || ""}</div>`;
  }

  // ------------------------------------------------------------ handlers
  function matrixOf(el) { return matrixSpec(el.dataset.m); }
  function applyToSelection(fn) {
    const spec = matrixSpec(sel.m);
    selected().forEach(([r, c]) => setCell(spec, r, c, fn(getCell(spec, r, c), r, c)));
    save();
    draw();
  }
  const act = {
    pick: async (el) => {
      const chosen = await pickEntity(el.dataset.purpose, { keep: get(el.dataset.path) || undefined });
      if (!chosen || !current()) return;
      await loadEntities([chosen.entity_id]);
      const path = el.dataset.path;
      const parent = path.split("/").slice(0, -1).join("/");
      if (parent && get(parent) == null) {  // an optional sensor block (light level, temperature, weather) appears
        set(parent, parent.endsWith("weather") ? { entity_id: null, states: [] } : { entity_id: null, above: null, hysteresis: null });
      }
      set(path, chosen.entity_id);
      save(); draw();
    },
    unpick: (el) => {
      const path = el.dataset.path;
      set(path, null);
      const parent = path.split("/").slice(0, -1).join("/");
      if (["sun/lux", "sun/temperature", "sun/weather"].includes(parent)) set(parent, null);
      save(); draw();
    },
    mcell: (el) => {
      const key = `${el.dataset.r}${SEP}${el.dataset.c}`;
      if (sel.m !== el.dataset.m) { sel.m = el.dataset.m; sel.keys = new Set(); }
      if (sel.keys.has(key)) sel.keys.delete(key); else sel.keys.add(key);
      draw();
    },
    mrow: (el) => { const spec = matrixOf(el); sel.m = spec.id; sel.keys = new Set(spec.cols.map((c) => `${el.dataset.r}${SEP}${c.key}`)); draw(); },
    mcol: (el) => { const spec = matrixOf(el); sel.m = spec.id; sel.keys = new Set(spec.rows.map((r) => `${r.key}${SEP}${el.dataset.c}`)); draw(); },
    mall: (el) => { const spec = matrixOf(el); sel.m = spec.id; sel.keys = new Set(spec.rows.flatMap((r) => spec.cols.map((c) => `${r.key}${SEP}${c.key}`))); draw(); },
    mnone: () => { sel.keys = new Set(); draw(); },
    mcopy: (el) => {
      const spec = matrixOf(el);
      const items = selected();
      const rowsOrder = [...spec.rows.map((r) => r.key), "__matrix"];
      const colsOrder = [...spec.cols.map((c) => c.key), "__row", "__matrix"];
      const r0 = Math.min(...items.map(([r]) => rowsOrder.indexOf(r)));
      const c0 = Math.min(...items.map(([, c]) => colsOrder.indexOf(c)));
      sel.clip = { kind: spec.kind, cells: items.map(([r, c]) => ({ dr: rowsOrder.indexOf(r) - r0, dc: colsOrder.indexOf(c) - c0, cell: clone(getCell(spec, r, c)) })) };
      toast(`Copied ${plural(items.length, "cell")}.`);
      draw();
    },
    mpaste: (el) => {
      const spec = matrixOf(el);
      if (!sel.clip) return;
      const items = selected();
      const rowsOrder = spec.rows.map((r) => r.key);
      const colsOrder = [...spec.cols.map((c) => c.key), "__row"];
      if (sel.clip.cells.length === 1) {
        items.forEach(([r, c]) => setCell(spec, r, c, clone(sel.clip.cells[0].cell)));
      } else {
        const r0 = Math.min(...items.map(([r]) => rowsOrder.indexOf(r)).filter((i) => i >= 0));
        const c0 = Math.min(...items.map(([, c]) => colsOrder.indexOf(c)).filter((i) => i >= 0));
        sel.clip.cells.forEach(({ dr, dc, cell }) => { const r = rowsOrder[r0 + dr]; const c = colsOrder[c0 + dc]; if (r && c) setCell(spec, r, c, clone(cell)); });
      }
      save(); draw();
    },
    mclear: () => applyToSelection(() => null),
    cunset: (el) => applyToSelection((cell) => (cell ? { ...cell, [el.dataset.key]: null } : cell)),
    coladd: (el) => {
      const select = view.querySelector(`#addcol-${el.dataset.path.replace(/\W/g, "-")}`);
      if (!select || !select.value) return;
      const [source, state] = select.value.split(":");
      get(el.dataset.path).push({ source, state });
      save(); draw();
    },
    colmove: (el) => {
      const list = get(el.dataset.path);
      const i = Number(el.dataset.i);
      const j = i + Number(el.dataset.dir);
      [list[i], list[j]] = [list[j], list[i]];
      save(); draw();
    },
    coldrop: (el) => {
      const list = get(el.dataset.path);
      const [gone] = list.splice(Number(el.dataset.i), 1);
      if (el.dataset.path === "presence/columns") {  // its cells and permissions go with it
        const id = colId(gone);
        Object.values(d.presence.cells).forEach((row) => delete row[id]);
        Object.keys(d.presence.cells).forEach((l) => { if (!Object.keys(d.presence.cells[l]).length) delete d.presence.cells[l]; });
        Object.values(d.presence.gate).forEach((g) => { const k = g.indexOf(id); if (k >= 0) g.splice(k, 1); });
      }
      save(); draw();
    },
    bandadd: () => {
      const id = `b_${Math.random().toString(16).slice(2, 6).padEnd(4, "0")}`;
      d.lux.bands.splice(Math.max(0, d.lux.bands.length - 1), 0, { id, name: "", below: null });
      if (d.lux.bands.length === 1) d.lux.bands.push({ id: `b_${Math.random().toString(16).slice(2, 6).padEnd(4, "0")}`, name: "", below: null });
      draw();  // saved once it has a name
    },
    banddrop: (el) => {
      const [gone] = d.lux.bands.splice(Number(el.dataset.i), 1);
      if (d.lux.bands.length) d.lux.bands[d.lux.bands.length - 1].below = null;
      Object.values(d.lux.cells).forEach((row) => delete row[gone.id]);
      Object.values(d.lux.gate).forEach((g) => ["on", "off"].forEach((k) => { if (g[k]) g[k] = g[k].filter((b) => b !== gone.id); }));
      save(); draw();
    },
    noauto: () => { d.mood.auto = null; save(); draw(); },
    winadd: () => { d.schedule.windows.push({ after: null, before: null, weekdays: [] }); draw(); },
    windrop: (el) => { d.schedule.windows.splice(Number(el.dataset.i), 1); save(); draw(); },
    entryadd: () => { d.schedule.entries.push({ id: `s_${Math.random().toString(16).slice(2, 6).padEnd(4, "0")}`, name: "", at: null, weekdays: [], cells: {}, mode: null }); draw(); },
    entrydrop: (el) => { d.schedule.entries.splice(Number(el.dataset.i), 1); save(); draw(); },
    modeadd: () => { const el = view.querySelector("#newmode"); const name = (el.value || "").trim(); if (!name || d.modes.names.includes(name)) return; d.modes.names.push(name); save(); draw(); },
    modeidea: (el) => { if (!d.modes.names.includes(el.dataset.name)) d.modes.names.push(el.dataset.name); save(); draw(); },
    modedrop: (el) => {
      const [gone] = d.modes.names.splice(Number(el.dataset.i), 1);
      Object.values(d.modes.cells).forEach((row) => delete row[gone]);
      Object.keys(d.modes.mood_map).forEach((k) => { if (d.modes.mood_map[k] === gone) delete d.modes.mood_map[k]; });
      delete d.modes.sun_policy[gone];
      d.schedule.entries.forEach((e) => { if (e.mode === gone) e.mode = null; });
      save(); draw();
    },
    winsetup: (el) => {
      d.sun.windows[el.dataset.cover] = { orientation: null, offset: null, left: null, right: null, elevation_min: null, elevation_max: null, position: null, tilt: null, leave: null, weather_sensitive: false };
      save(); draw();
    },
    windel: (el) => { delete d.sun.windows[el.dataset.cover]; save(); draw(); },
    compass: (el, e) => {
      const svg = el.closest("svg");
      const box = svg.getBoundingClientRect();
      const x = (e.clientX - box.left) / box.width * 220 - 110;
      const y = (e.clientY - box.top) / box.height * 220 - 110;
      const ha = ((Math.atan2(x, -y) * 180 / Math.PI) + 360) % 360;
      d.sun.windows[svg.dataset.cover].orientation = Math.round(norm(ha - 180));
      save(); draw();
    },
    simrun: async () => {
      try {
        sim.result = await api("api/designers/simulate", { device_id: deviceId, scenario: scenario() });
        if (current()) draw();
      } catch (err) { toast(errorText(err)); }
    },
    simlive: () => { sim.scenario.states = {}; sim.result = null; draw(); },
    convert: async () => {
      const ok = await ask({ title: "Convert to the new Lighting Designer?", ok: "Convert",
        body: `<ul>${data.conversion.changes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul><p class="small">Saved now; deployed only when you deploy the device.</p>` });
      if (!ok) return;
      try {
        await change("api/designers/convert", { device_id: deviceId, designer_id: designerId, apply: true });
        toast("Converted. Review it, then deploy the device.");
        location.hash = `#/design/${enc(deviceId)}/${enc(designerId)}/lights`;
      } catch (err) { toast(errorText(err)); }
    },
  };
  function scenario() {
    const states = {};
    sim.inputs.inputs.forEach((e) => { states[e.entity_id] = { state: e.state, attributes: { ...e.attributes }, origin: "live" }; });
    Object.entries(sim.scenario.states).forEach(([id, st]) => { states[id] = { ...(states[id] || {}), ...st, attributes: { ...((states[id] || {}).attributes || {}), ...(st.attributes || {}) } }; });
    Object.values(states).forEach((st) => { if (st.manual) st.age = 0; });
    return { states, trigger: sim.scenario.trigger, time: sim.scenario.time, weekday: sim.scenario.weekday };
  }
  const testState = (id) => (sim.scenario.states[id] = sim.scenario.states[id] || { origin: "test", attributes: {} });
  const chg = {
    flag: (el) => { set(el.dataset.path, el.checked); save(); draw(); },
    num: (el) => { set(el.dataset.path, el.value === "" ? null : Number(el.value)); save(); },
    text: (el) => { set(el.dataset.path, el.value === "" ? null : el.value); if (el.dataset.path.startsWith("lux/bands") && d.lux.bands.some((b) => !b.name)) { draw(); return; } save(); draw(); },
    time: (el) => { set(el.dataset.path, el.value ? `${el.value}:00` : null); save(); },
    wday: (el) => { const list = get(el.dataset.path); const i = list.indexOf(el.value); if (el.checked && i < 0) list.push(el.value); if (!el.checked && i >= 0) list.splice(i, 1); save(); },
    row: (el) => {
      const list = d[rowsKey];
      if (el.checked) list.push(el.dataset.id); else list.splice(list.indexOf(el.dataset.id), 1);
      if (!el.checked) {  // the row's cells go with it
        ["mood", "presence", "lux", "modes"].forEach((l) => { if (d[l]) { delete d[l].cells[el.dataset.id]; delete d[l].row_defaults[el.dataset.id]; if (d[l].gate) delete d[l].gate[el.dataset.id]; } });
        d.schedule.entries.forEach((e) => delete e.cells[el.dataset.id]);
        if (d.sun) delete d.sun.windows[el.dataset.id];
      }
      save(); draw();
    },
    primary: (el) => { d.primary = el.value || null; save(); draw(); },
    value: (el) => {
      const list = d.mood.values;
      if (el.checked) list.push(el.value); else { list.splice(list.indexOf(el.value), 1); Object.values(d.mood.cells).forEach((row) => delete row[el.value]); }
      save(); draw();
    },
    auto: (el) => { if (el.value) { d.mood.auto = { value: el.value, entity_id: null }; save(); draw(); } },
    pgate: (el) => { const g = d.presence.gate[el.dataset.light]; const i = g.indexOf(el.dataset.col); if (el.checked && i < 0) g.push(el.dataset.col); if (!el.checked && i >= 0) g.splice(i, 1); save(); draw(); },
    "pgate-free": (el) => { if (el.checked) delete d.presence.gate[el.dataset.light]; else d.presence.gate[el.dataset.light] = []; save(); draw(); },
    lgate: (el) => { const g = d.lux.gate[el.dataset.light][el.dataset.k]; const i = g.indexOf(el.dataset.band); if (el.checked && i < 0) g.push(el.dataset.band); if (!el.checked && i >= 0) g.splice(i, 1); save(); draw(); },
    "lgate-free": (el) => { const g = d.lux.gate[el.dataset.light] = d.lux.gate[el.dataset.light] || { on: null, off: null }; g[el.dataset.k] = el.checked ? null : []; if (g.on == null && g.off == null) delete d.lux.gate[el.dataset.light]; save(); draw(); },
    ckind: (el) => applyToSelection((cell) => (cell && cell.kind === el.value ? cell : newCell(el.value, family))),
    cval: (el) => applyToSelection((cell) => {
      if (!cell) return cell;
      const out = { ...cell };
      const key = el.dataset.key;
      if (key === "color") out.color = { rgb: rgbOf(el.value) };
      else if (key === "effect") out.effect = el.value || null;
      else out[key] = el.value === "" ? null : Number(el.value);
      return out;
    }),
    response: (el) => {
      const v = el.value;
      set(el.dataset.path, v === "" ? null : v === "fixed" ? { kind: "fixed", value: null } : v === "close" ? { kind: "close" } : { kind: "curve", basis: null, points: [[null, null], [null, null]] });
      save(); draw();
    },
    ccell: (el) => { set(el.dataset.path, el.value === "" ? null : el.value === "position" ? { kind: "position", position: null, tilt: null } : { kind: el.value }); save(); draw(); },
    weather: (el) => { const list = d.sun.weather.states; const i = list.indexOf(el.value); if (el.checked && i < 0) list.push(el.value); if (!el.checked && i >= 0) list.splice(i, 1); save(); },
    policy: (el) => { if (el.value) d.modes.sun_policy[el.dataset.mode] = el.value; else delete d.modes.sun_policy[el.dataset.mode]; save(); draw(); },
    modename: (el) => {
      const i = Number(el.dataset.i);
      const old = d.modes.names[i];
      const name = el.value.trim();
      if (!name || d.modes.names.includes(name)) { el.value = old; return; }
      d.modes.names[i] = name;
      Object.values(d.modes.cells).forEach((row) => { if (old in row) { row[name] = row[old]; delete row[old]; } });
      Object.keys(d.modes.mood_map).forEach((k) => { if (d.modes.mood_map[k] === old) d.modes.mood_map[k] = name; });
      if (old in d.modes.sun_policy) { d.modes.sun_policy[name] = d.modes.sun_policy[old]; delete d.modes.sun_policy[old]; }
      d.schedule.entries.forEach((e) => { if (e.mode === old) e.mode = name; });
      save(); draw();
    },
    map: (el) => { if (el.value) d.modes.mood_map[el.dataset.value] = el.value; else delete d.modes.mood_map[el.dataset.value]; save(); },
    simtrigger: (el) => { sim.scenario.trigger = el.value || null; draw(); },
    simtime: (el) => { sim.scenario.time = el.value ? `${el.value}:00` : null; },
    simday: (el) => { sim.scenario.weekday = el.value || null; },
    simstate: (el) => { testState(el.dataset.id).state = el.value; draw(); },
    simattr: (el) => { const st = testState(el.dataset.id); st.attributes = { ...(st.attributes || {}), [el.dataset.attr]: el.value === "" ? null : Number(el.value) }; },
    simmanual: (el) => { testState(el.dataset.id).manual = el.checked; },
  };

  draw();
  if (tab === "simulate") {
    api(`api/simulation/${enc(deviceId)}`).then((inputs) => { if (current()) { sim.inputs = inputs; draw(); } }).catch((err) => toast(errorText(err)));
  }
  if (kind === "cover" && tab === "sun") {
    api("api/sun").then((s) => { if (current()) { sun = s; draw(); } }).catch(() => {});
  }
  void pending;
  return { act, chg, inp: {} };
}

export function capText(cap) {
  if (!cap) return "";
  if (cap.unknown) return "capabilities not reported right now";
  if (cap.domain === "light") return [cap.brightness ? "dims" : "on/off only", cap.color_temp ? "colour temperature" : null, cap.color ? "colours" : null, cap.effect ? "effects" : null].filter(Boolean).join(" · ");
  if (cap.domain === "cover") return [cap.position ? "positions" : "open/close only", cap.tilt ? "tilt" : null, cap.reports_position ? "reports its position" : "no position feedback", cap.weather_sensitive ? "awning" : null].filter(Boolean).join(" · ");
  return "";
}

// ------------------------------------------------------------------ the template gallery
export async function galleryPage(deviceId, current) {
  const data = await api(`api/gallery/${enc(deviceId)}`);
  if (!current()) return null;
  const categories = [...new Set(data.templates.map((t) => t.category))];
  view.innerHTML = `<h1><a class="back" href="#/devices/${enc(deviceId)}" aria-label="Back to the device">←</a>New automation <span class="muted small">${esc(data.device.name)}</span></h1>
    <p class="muted">Choose a starting point. A template turns on the controls it is about and lists what it needs; it chooses no entity and no value for you. Everything it leaves open is shown as UNSET.</p>
    ${categories.map((cat) => `<h2>${esc(cat)}</h2><div class="grid">${data.templates.filter((t) => t.category === cat).map((t) => `<div class="card tpl" data-template="${esc(t.id)}"><h3>${esc(t.name)}</h3><p class="small">${esc(t.description)}</p>
        <p class="small"><strong>Needs:</strong> ${t.required.map(esc).join("; ")}</p>${t.optional.length ? `<p class="small muted"><strong>Optional:</strong> ${t.optional.map(esc).join("; ")}</p>` : ""}
        <div class="actions"><button class="primary" data-act="use" data-id="${esc(t.id)}">${data.existing[t.designer] ? "Add to the designer" : "Use this"}</button></div></div>`).join("")}</div>`).join("")}
    <h2>Any device</h2><div class="grid"><div class="card tpl"><h3>Ordinary rule (IF / AND-OR / THEN)</h3><p class="small">The general rule editor: triggers, conditions and actions for anything Home Assistant can control.</p><div class="actions"><a class="button" href="#/rules/${enc(deviceId)}/new">Write a rule</a></div></div>
      ${data.kinds.map((k) => (data.existing[k] ? `<div class="card tpl"><h3>Open the ${k === "lighting" ? "Lighting" : "Cover"} Designer</h3><div class="actions"><a class="button" href="#/design/${enc(deviceId)}/${enc(data.existing[k])}">Open</a></div></div>`
    : `<div class="card tpl"><h3>Empty ${k === "lighting" ? "Lighting" : "Cover"} Designer</h3><p class="small">${k === "lighting" ? "No matrices, no switches, nothing set." : "Every control off, nothing set."}</p><div class="actions"><button data-act="blank" data-kind="${esc(k)}">Create</button></div></div>`)).join("")}</div>
    <h2>Planned</h2><ul class="plain small">${data.planned.map((p) => `<li><strong>${esc(p.category)}</strong>: ${esc(p.text)}</li>`).join("")}</ul><p class="muted small">${esc(data.note)}</p>`;
  return {
    chg: {},
    act: {
      use: async (el) => {
        try {
          const result = await change("api/gallery/apply", { device_id: deviceId, template: el.dataset.id });
          const tpl = data.templates.find((t) => t.id === el.dataset.id) || {};
          location.hash = `#/design/${enc(deviceId)}/${enc(result.designer_id)}${tpl.tab ? `/${enc(tpl.tab)}` : ""}`;
        } catch (err) { toast(errorText(err)); }
      },
      blank: async (el) => {
        try {
          const result = await change("api/designers/create", { device_id: deviceId, kind: el.dataset.kind });
          location.hash = `#/design/${enc(deviceId)}/${enc(result.designer_id)}`;
        } catch (err) { toast(errorText(err)); }
      },
    },
  };
}

// ------------------------------------------------------------------ the device page's designer card
const DESIGNER_ICON = { lighting: "light", cover: "cover", heating: "climate", light_level: "light" };
const DESIGNER_TEXT = { lighting: "Lighting Designer: Truth and Brightness Matrices, Colour & Mood, switches and remotes, Advanced Rules",
  cover: "Cover Designer: sun angle, cover modes, mood mapping",
  heating: "Heating Designer: schedules, day/night, presence, windows, away/home and manual override (no prediction, no other App needed)" };
export function designersCard(deviceId, info) {
  if (!info || (!info.available.length && !info.items.length)) return "";
  const items = info.items.map((i) => {
    const blocking = i.problems.filter((p) => p.level === "blocking").length;
    if (i.kind === "light_level") {
      return `<div class="rule designer-item" data-designer="${esc(i.id)}"><div class="row">${icon("light")}<a class="name" href="#/light-level/rooms/${enc(i.config.room)}">The light level of this room</a><span class="spacer"></span><a class="button" href="#/light-level/rooms/${enc(i.config.room)}">Open on the Light Level page</a></div>
        <div class="sub">${plural(i.rules.length, "generated rule")}${blocking ? ` · <span class="status bad">${plural(blocking, "thing")} to resolve</span>` : ""} · configured on the Light Level page; deploy it here</div></div>`;
    }
    return `<div class="rule designer-item" data-designer="${esc(i.id)}"><div class="row">${icon(DESIGNER_ICON[i.kind] || "device")}<a class="name" href="#/design/${enc(deviceId)}/${enc(i.id)}">${esc(i.name || i.kind)}</a>
        ${i.supported ? "" : `<span class="badge warn">made by another version</span>`}${i.enabled ? "" : `<span class="badge">disabled</span>`}<span class="spacer"></span><a class="button" href="#/design/${enc(deviceId)}/${enc(i.id)}">Open</a>
        <button class="link danger-text" data-act="designer-delete" data-id="${esc(i.id)}">Delete</button></div>
      <div class="sub">${plural(i.rules.length, "generated rule")}${blocking ? ` · <span class="status bad">${plural(blocking, "thing")} to resolve</span>` : ""}</div></div>`;
  }).join("");
  const missing = info.available.filter((k) => !info.items.some((i) => i.kind === k));
  return `<div class="card" id="designers"><div class="row"><h2>Specialised designers</h2><span class="spacer"></span>${info.items.some((i) => i.kind === "light_level") ? "" : `<a class="button" href="#/gallery/${enc(deviceId)}">Templates…</a>`}</div>
    ${items}${missing.map((k) => `<div class="rule"><div class="row">${icon(DESIGNER_ICON[k] || "device")}<span>${esc(DESIGNER_TEXT[k] || k)}</span><span class="spacer"></span><button data-act="designer-create" data-kind="${esc(k)}">Create</button></div></div>`).join("")}
    <p class="muted small">A designer generates ordinary rules of this device; they run in its one automation, next to the rules below.</p></div>`;
}

export { session };
