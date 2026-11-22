// Automation Builder - presence sources (Phase 0.3): rooms, named zones, position zones and persons, shared by every
// designer. A source only says where the information comes from; it detects nothing itself. Saved on change.

import { api, ask, change, entities, entityName, errorText, esc, icon, loadEntities, pickEntity, toast, view } from "./ui.js";

const clone = (v) => JSON.parse(JSON.stringify(v));
const KINDS = [["room", "Room presence"], ["zone", "Named zone (its own entity)"], ["position", "Position zone (drawn on a sensor's coordinates)"], ["person", "Person or tracker (geographic zone)"]];
const PURPOSE = { room: "presence", zone: "presence", person: "person" };
const blank = (kind = "zone") => ({ id: null, name: "", kind, area_id: null, entity_id: null, active_state: null, possible: null, entry_seconds: 0, exit_seconds: 0, respect_fresh: false,
  position: kind === "position" ? { targets: [{ x: { entity_id: null, attribute: null }, y: { entity_id: null, attribute: null } }], units: null, notes: "", shape: { type: "polygon", points: [] }, ignore_origin: false, max_age_seconds: null, bounds: null } : null });

export async function presencePage(current) {
  let data = await api("api/presence");
  if (!current()) return null;
  let editing = null;     // the source being edited (a copy)
  let chain = Promise.resolve();
  await loadEntities(data.sources.flatMap((s) => [s.entity_id, ...((s.position || {}).targets || []).flatMap((t) => [t.x.entity_id, t.y.entity_id])]).filter(Boolean));

  const status = (text, cls = "") => { const el = view.querySelector("#z-status"); if (el) { el.className = `save-status ${cls}`; el.textContent = text; } };
  function save() {
    if (!editing || !editing.name.trim()) { status("Give the source a name to save it."); return chain; }
    status("Saving…");
    chain = chain.then(async () => {
      if (!editing) return;
      const sent = clone(editing);  // taken now: a save queued behind the first one carries the id it was given
      try {
        const result = await change("api/presence/save", { source: sent });
        if (!current()) return;
        if (editing && !editing.id) editing.id = result.source_id;
        data = await api("api/presence");
        draw();
        status("Saved · deploy the devices that use it", "ok");
      } catch (err) { status(`Not saved: ${errorText(err)}`, "bad"); }
    });
    return chain;
  }

  const liveText = (s) => {
    const l = s.live || {};
    if (s.kind === "position") return l.state ? `now ${l.state}` : "";
    if (!s.entity_id) return "UNSET: no entity";
    return `now ${l.state ?? "?"}${l.fresh === false ? " (stale)" : ""}`;
  };
  function listHtml() {
    const groups = {};
    data.sources.forEach((s) => { (groups[s.area || "Without a room"] = groups[s.area || "Without a room"] || []).push(s); });
    return Object.entries(groups).map(([area, items]) => `<h2>${icon("room")}${esc(area)}</h2><div class="grid">${items.map((s) => `<div class="card zone-card" data-source="${esc(s.id)}">
        <div class="row"><strong>${esc(s.name)}</strong><span class="badge">${esc(KINDS.find(([k]) => k === s.kind)[1].split(" (")[0])}</span><span class="spacer"></span><button data-act="edit" data-id="${esc(s.id)}">Edit</button></div>
        <div class="sub">${s.kind === "position" ? `${(s.position.targets || []).length} target(s), ${s.position.shape ? `${s.position.shape.type} with ${s.position.shape.points.length} point(s)` : "no shape"}` : esc(s.entity_name || s.entity_id || "UNSET")}${s.kind === "person" ? ` at ${esc(s.active_state || "UNSET")}` : ""}</div>
        <div class="sub">${esc(liveText(s))}${s.entry_seconds ? ` · entry ${s.entry_seconds} s` : ""}${s.exit_seconds ? ` · exit ${s.exit_seconds} s` : ""}${s.respect_fresh ? " · honours “fresh”" : ""}</div>
        <div class="sub">${s.used_by.length ? `Used by ${s.used_by.map(esc).join(", ")}` : "Not used yet"}</div></div>`).join("")}</div>`).join("");
  }

  function canvas(s) {
    const pos = s.position;
    const pts = (pos.shape && pos.shape.points) || [];
    const live = ((data.sources.find((x) => x.id === s.id) || {}).live || {}).targets || [];
    const all = [...pts, ...live.filter((t) => t[0] != null && t[1] != null), [0, 0]];
    const b = pos.bounds || { x: [Math.min(...all.map((p) => p[0])) - 500, Math.max(...all.map((p) => p[0])) + 500], y: [Math.min(0, ...all.map((p) => p[1])) - 200, Math.max(...all.map((p) => p[1])) + 500] };
    const W = 320; const H = 240;
    const sx = (x) => ((x - b.x[0]) / (b.x[1] - b.x[0])) * W;
    const sy = (y) => H - ((y - b.y[0]) / (b.y[1] - b.y[0])) * H;
    const shape = pos.shape && pts.length ? (pos.shape.type === "rect" && pts.length === 2
      ? `<rect class="zone" x="${Math.min(sx(pts[0][0]), sx(pts[1][0]))}" y="${Math.min(sy(pts[0][1]), sy(pts[1][1]))}" width="${Math.abs(sx(pts[1][0]) - sx(pts[0][0]))}" height="${Math.abs(sy(pts[1][1]) - sy(pts[0][1]))}"/>`
      : `<polygon class="zone" points="${pts.map((p) => `${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join(" ")}"/>`) : "";
    return `<svg class="zone-canvas" viewBox="0 0 ${W} ${H}" data-act="canvas" data-bx="${b.x.join(",")}" data-by="${b.y.join(",")}" role="img" aria-label="Zone shape in the sensor's coordinates">
      <rect class="bg" width="${W}" height="${H}"/>${shape}${pts.map((p) => `<circle class="pt" cx="${sx(p[0]).toFixed(1)}" cy="${sy(p[1]).toFixed(1)}" r="4"/>`).join("")}
      <circle class="sensor" cx="${sx(0).toFixed(1)}" cy="${sy(0).toFixed(1)}" r="5"><title>The sensor (0, 0)</title></circle>
      ${live.filter((t) => t[0] != null && t[1] != null).map((t) => `<circle class="target" cx="${sx(t[0]).toFixed(1)}" cy="${sy(t[1]).toFixed(1)}" r="6"><title>A target now</title></circle>`).join("")}
      <text x="4" y="${H - 4}" class="cdir small">${b.x[0]}, ${b.y[0]}</text><text x="${W - 4}" y="12" class="cdir small end">${b.x[1]}, ${b.y[1]}</text></svg>`;
  }

  function editorHtml() {
    const s = editing;
    const entityBtn = (path, entityId, purpose, label) => `<button type="button" class="entity ${entityId ? "" : "unset"}" data-act="pick" data-path="${esc(path)}" data-purpose="${esc(purpose)}">${entityId ? esc(entityName(entityId)) : `UNSET · ${esc(label)}`}</button>`;
    let kindFields = "";
    if (s.kind === "room" || s.kind === "zone") {
      kindFields = `<div class="field"><label>Entity</label><span>${entityBtn("entity_id", s.entity_id, PURPOSE[s.kind], "on = occupied")} ${s.entity_id && entities[s.entity_id] ? `<span class="badge">now ${esc(entities[s.entity_id].state)}</span>` : ""}</span>
          <div class="help">A binary sensor, toggle helper or switch that is on while ${s.kind === "room" ? "the room" : "this zone"} is occupied. For a radar zone, its zone entity.</div></div>`;
      if (s.kind === "room") {
        kindFields += `<div class="field"><label>Possibly occupied (optional)</label><span>${entityBtn("possible.entity_id", (s.possible || {}).entity_id, "any", "an entity")} state <input type="text" data-chg="text" data-path="possible.state" value="${esc((s.possible || {}).state || "")}" placeholder="for example possible" aria-label="State for possibly occupied"></span>
          <div class="help">Only where the source supplies it (for example a state sensor of Smart Occupancy Detection).</div></div>`;
      }
      kindFields += `<div class="field"><label>Stale information</label><label class="switch"><input type="checkbox" data-chg="flag" data-path="respect_fresh" ${s.respect_fresh ? "checked" : ""}> Nothing acts while the entity reports <span class="mono">fresh: false</span></label></div>`;
    }
    if (s.kind === "person") {
      kindFields = `<div class="field"><label>Person or tracker</label><span>${entityBtn("entity_id", s.entity_id, "person", "a person")}</span></div>
        <div class="field"><label>Counts as present at</label><span><input type="text" list="zones-list" data-chg="text" data-path="active_state" value="${esc(s.active_state || "")}" placeholder="home" aria-label="Zone"><datalist id="zones-list"><option value="home"></datalist></span>
          <div class="help">A geographic zone of Home Assistant (home, or the name of another zone). This is not an indoor zone, and it is never inferred from anonymous sensors.</div></div>`;
    }
    if (s.kind === "position") {
      const pos = s.position;
      kindFields = `<h3>Where the positions come from</h3>${pos.targets.map((t, i) => `<div class="field"><label>Target ${i + 1}</label><span>x ${entityBtn(`position.targets.${i}.x.entity_id`, t.x.entity_id, "any", "x")} attribute <input type="text" class="short" data-chg="text" data-path="position.targets.${i}.x.attribute" value="${esc(t.x.attribute || "")}" placeholder="(state)" aria-label="x attribute">
          y ${entityBtn(`position.targets.${i}.y.entity_id`, t.y.entity_id, "any", "y")} attribute <input type="text" class="short" data-chg="text" data-path="position.targets.${i}.y.attribute" value="${esc(t.y.attribute || "")}" placeholder="(state)" aria-label="y attribute">
          ${pos.targets.length > 1 ? `<button type="button" class="link danger-text" data-act="tdrop" data-i="${i}">Remove</button>` : ""}</span></div>`).join("")}
        <div class="actions"><button type="button" data-act="tadd" ${pos.targets.length < 3 ? "" : "disabled"}>Add a target</button></div>
        <div class="field"><label>Units</label><select data-chg="text" data-path="position.units"><option value="">not stated</option>${["mm", "cm", "m"].map((u) => `<option ${pos.units === u ? "selected" : ""}>${u}</option>`).join("")}</select></div>
        <div class="field"><label>Coordinate system and calibration</label><textarea data-chg="text" data-path="position.notes" rows="2" maxlength="600" placeholder="For example: origin at the sensor, x to the right looking from it, y away from it; checked by standing at the sofa">${esc(pos.notes || "")}</textarea></div>
        <div class="field"><label>No target</label><label class="switch"><input type="checkbox" data-chg="flag" data-path="position.ignore_origin" ${pos.ignore_origin ? "checked" : ""}> A reading of exactly (0, 0) means “no target” (some radars report it so)</label></div>
        <div class="field"><label>Readings older than</label><span><input type="number" min="1" max="3600" data-chg="num" data-path="position.max_age_seconds" value="${esc(pos.max_age_seconds ?? "")}" placeholder="not checked"> s are ignored</span></div>
        <h3>Shape</h3><div class="row"><select data-chg="shape" aria-label="Shape"><option value="polygon" ${pos.shape && pos.shape.type === "polygon" ? "selected" : ""}>Polygon (tap to add points)</option><option value="rect" ${pos.shape && pos.shape.type === "rect" ? "selected" : ""}>Rectangle (tap two corners)</option></select>
          <button type="button" data-act="clearshape">Clear the shape</button></div>
        <div class="zone-edit">${canvas(s)}<ol class="plain small">${((pos.shape || {}).points || []).map((p, i) => `<li class="row">${i + 1}. x <input type="number" step="any" class="short" data-chg="pt" data-i="${i}" data-k="0" value="${p[0]}" aria-label="x of point ${i + 1}"> y <input type="number" step="any" class="short" data-chg="pt" data-i="${i}" data-k="1" value="${p[1]}" aria-label="y of point ${i + 1}"> <button type="button" class="link danger-text" data-act="ptdrop" data-i="${i}">Remove</button></li>`).join("")}</ol></div>
        <p class="muted small">The shape is drawn in the sensor's own coordinates (the dot at the bottom is the sensor). It compiles into a template that Home Assistant evaluates by itself. Positions are anonymous: a zone never knows who is in it.</p>`;
    }
    return `<div class="card review" id="z-editor"><div class="row"><h2>${s.id ? "Edit presence source" : "New presence source"}</h2><span class="spacer"></span><span class="save-status" id="z-status"></span></div>
      <div class="field"><label for="z-name">Name</label><input type="text" id="z-name" data-chg="text" data-path="name" maxlength="80" value="${esc(s.name)}" placeholder="for example Sofa"></div>
      <div class="field"><label for="z-area">Room</label><select id="z-area" data-chg="text" data-path="area_id"><option value="">Without a room</option>${data.areas.map((a) => `<option value="${esc(a.area_id)}" ${a.area_id === s.area_id ? "selected" : ""}>${esc(a.name)}</option>`).join("")}</select>
        <div class="help">Organises the list and the choices in the designers. A designer can use a source of another room when you add it there on purpose.</div></div>
      <div class="field"><label for="z-kind">Kind</label><select id="z-kind" data-chg="kind">${KINDS.map(([k, l]) => `<option value="${k}" ${k === s.kind ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></div>
      ${kindFields}
      <div class="field"><label>Entry delay</label><span><input type="number" min="0" max="3600" data-chg="num" data-path="entry_seconds" value="${s.entry_seconds}"> s</span><div class="help">How long it must hold before it counts as entered.</div></div>
      <div class="field"><label>Exit delay</label><span><input type="number" min="0" max="86400" data-chg="num" data-path="exit_seconds" value="${s.exit_seconds}"> s</span><div class="help">How long it must be left before it counts as vacant.</div></div>
      <div class="actions"><button data-act="close">Done</button>${s.id ? `<button class="danger" data-act="delete">Delete</button>` : ""}</div></div>`;
  }

  function draw() {
    view.innerHTML = `<h1>Presence sources<span class="spacer"></span><button class="primary" data-act="new">Add a source</button></h1>
      <p class="muted">Rooms, named zones inside rooms, position zones and persons, for the designers' presence matrices. Nothing here detects presence: each source reads an entity of Home Assistant (for example of Smart Occupancy Detection, a radar or a phone). Smart Occupancy Detection is not required.</p>
      ${editing ? editorHtml() : ""}${data.sources.length ? listHtml() : `<div class="card empty"><p class="muted">No presence sources yet.</p></div>`}`;
  }

  const get = (path) => path.split(".").reduce((o, k) => (o == null ? o : o[k]), editing);
  function set(path, value) {
    const keys = path.split(".");
    const last = keys.pop();
    let holder = editing;
    keys.forEach((k) => { if (holder[k] == null) holder[k] = {}; holder = holder[k]; });
    holder[last] = value;
  }
  draw();
  return {
    act: {
      new: () => { editing = blank("zone"); draw(); view.querySelector("#z-name").focus(); },
      edit: (el) => { editing = clone(data.sources.find((s) => s.id === el.dataset.id)); ["live", "used_by", "area", "entity_name"].forEach((k) => delete editing[k]); draw(); },
      close: () => { editing = null; draw(); },
      pick: async (el) => {
        const chosen = await pickEntity(el.dataset.purpose, { keep: get(el.dataset.path) || undefined });
        if (!chosen || !current()) return;
        await loadEntities([chosen.entity_id]);
        set(el.dataset.path, chosen.entity_id);
        if (!editing.name && editing.kind !== "position") editing.name = chosen.name;
        draw(); save();
      },
      tadd: () => { editing.position.targets.push({ x: { entity_id: null, attribute: null }, y: { entity_id: null, attribute: null } }); draw(); },
      tdrop: (el) => { editing.position.targets.splice(Number(el.dataset.i), 1); draw(); save(); },
      clearshape: () => { editing.position.shape = { type: (editing.position.shape || {}).type || "polygon", points: [] }; draw(); save(); },
      ptdrop: (el) => { editing.position.shape.points.splice(Number(el.dataset.i), 1); draw(); save(); },
      canvas: (el, e) => {
        const svg = el.closest("svg");
        const box = svg.getBoundingClientRect();
        const bx = svg.dataset.bx.split(",").map(Number);
        const by = svg.dataset.by.split(",").map(Number);
        const x = Math.round((bx[0] + ((e.clientX - box.left) / box.width) * (bx[1] - bx[0])) / 10) * 10;
        const y = Math.round((by[1] - ((e.clientY - box.top) / box.height) * (by[1] - by[0])) / 10) * 10;
        const shape = editing.position.shape = editing.position.shape || { type: "polygon", points: [] };
        if (shape.type === "rect" && shape.points.length >= 2) shape.points = [];
        shape.points.push([x, y]);
        draw(); save();
      },
      delete: async () => {
        if (!await ask({ title: `Delete “${editing.name}”?`, body: "<p>Designers that use it must not use it any more (deleting is refused otherwise).</p>", ok: "Delete", danger: true })) return;
        try { await change("api/presence/delete", { source_id: editing.id }); editing = null; data = await api("api/presence"); draw(); toast("Deleted."); } catch (err) { toast(errorText(err)); }
      },
    },
    chg: {
      text: (el) => { set(el.dataset.path, el.value === "" && el.dataset.path !== "name" ? null : el.value); save(); },
      num: (el) => { set(el.dataset.path, el.value === "" ? (el.dataset.path.endsWith("seconds") && !el.dataset.path.includes("max_age") ? 0 : null) : Number(el.value)); save(); },
      flag: (el) => { set(el.dataset.path, el.checked); save(); },
      kind: (el) => { const { id, name, area_id: area } = editing; editing = { ...blank(el.value), id, name, area_id: area }; draw(); if (id) save(); },
      shape: (el) => { editing.position.shape = { type: el.value, points: [] }; draw(); save(); },
      pt: (el) => { editing.position.shape.points[Number(el.dataset.i)][Number(el.dataset.k)] = Number(el.value); draw(); save(); },
    },
    inp: {},
  };
}
