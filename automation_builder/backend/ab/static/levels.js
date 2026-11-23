// Automation Builder - the Light Level Manager (0.5): light-level templates, default Truth/Brightness matrices and the
// light level of each room (its sensor, inherited limits and room overrides, an optional ambient estimate).
// Saved as soon as something changes; nothing reaches Home Assistant before the room's light level is deployed.
// Inheritance, live readings and the band a reading calls for all come from the backend: the interface decides nothing.

import { api, ask, change, chip, clock, enc, entities, entityName, errorText, esc, icon, loadEntities, notes, pickEntity, pickLux, toast, view } from "./ui.js";

const clone = (v) => JSON.parse(JSON.stringify(v));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const opt = (list, current) => list.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(current ?? "") ? "selected" : ""}>${esc(l)}</option>`).join("");
const TABS = [["rooms", "Room Light Levels"], ["templates", "Global Templates"], ["matrices", "Matrix Defaults"], ["sources", "Ambient Lux Sources"], ["diagnostics", "Diagnostics"]];
const SOURCE = { global: "Global", room: "Room override", unset: "Unset", none: "" };
const lux = (v) => (v == null ? "—" : `${Math.round(v * 10) / 10} lx`);
const secs = (v) => (v == null ? "Unset" : v >= 60 && v % 60 === 0 ? `${v / 60} min` : `${v} s`);
const truthText = (c) => (!c ? "UNSET" : { on: "ON", off: "OFF", none: "NO ACTION" }[c.kind]);
const brightText = (c) => (!c ? "UNSET" : c.kind === "set" ? `${c.pct ?? "…"}%` : "NO ACTION");
const cellText = (layer, c) => (layer === "truth" ? truthText(c) : brightText(c));
const slugOf = (name) => (String(name || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").replace(/^([^a-z])/, "l_$1") || "level").slice(0, 24);

export async function levelsPage(tab, item, current) {
  let data = await api("api/light-levels");
  if (!current()) return null;
  tab = TABS.some(([k]) => k === tab) ? tab : "rooms";
  let chain = Promise.resolve();
  let work = null;        // the item being edited (a copy): a room, a template or a matrix
  const status = (text, cls = "") => { const el = view.querySelector("#l-status"); if (el) { el.className = `save-status ${cls}`; el.textContent = text; } };
  await loadEntities(data.rooms.flatMap((r) => [r.source && r.source.entity_id, r.ambient.reference && r.ambient.reference.entity_id, ...r.ambient.lights]).filter(Boolean));

  const room = () => data.rooms.find((r) => r.area_id === item);
  const template = (id) => data.templates.find((t) => t.id === id);
  const matrix = (id) => data.matrices.find((m) => m.id === id);
  if (tab === "rooms" && item && room()) work = clone(room());
  if (tab === "templates" && item && template(item)) work = clone(template(item));
  if (tab === "matrices" && item && matrix(item)) work = clone(matrix(item));

  async function reload() { data = await api("api/light-levels"); if (!current()) return; draw(); }
  function save(kind) {
    status("Saving…");
    chain = chain.then(async () => {
      if (!work) return;
      const sent = clone(work);
      try {
        if (kind === "room") {
          const { area_id, template, source, overrides, matrices, matrix_overrides, ambient } = sent;
          await change("api/light-levels/room/save", { room: { area_id, template, source, overrides, matrices, matrix_overrides, ambient } });
        } else if (kind === "template") {
          const { id, name, bands, hysteresis, darker_seconds, brighter_seconds, unavailable_seconds } = sent;
          await change("api/light-levels/template/save", { template: { id, name, bands, hysteresis, darker_seconds, brighter_seconds, unavailable_seconds } });
        } else {
          const { id, name, layer, template: t, cells } = sent;
          await change("api/light-levels/matrix/save", { matrix: { id, name, layer, template: t, cells } });
        }
        data = await api("api/light-levels");
        if (!current()) return;
        const fresh = kind === "room" ? room() : kind === "template" ? template(sent.id) : matrix(sent.id);
        if (fresh && JSON.stringify(sent) === JSON.stringify(work)) work = clone(fresh);
        draw();
        status(kind === "room" ? "Saved · deploy the room's light level to apply it" : "Saved · the rooms that use it show changes to deploy", "ok");
      } catch (err) { status(`Not saved: ${errorText(err)}`, "bad"); }
    });
    return chain;
  }

  // ------------------------------------------------------------ pieces
  const tabs = () => `<nav class="tabs" aria-label="Light Level">${TABS.map(([k, l]) => `<a href="#/light-level/${k}" class="${k === tab ? "active" : ""}" ${k === tab ? 'aria-current="page"' : ""}>${esc(l)}</a>`).join("")}</nav>`;
  const tag = (source) => (source && SOURCE[source] ? `<span class="badge src-${esc(source)}">${esc(SOURCE[source])}</span>` : "");
  const statusChip = (r) => (r.status ? chip(r.status.status, r.status.text) : chip("unknown", "No device"));
  const bandNow = (r) => (r.band_name ? r.band_name : r.band === "no_reading" ? "no reading" : r.band ? r.band : "not deployed yet");

  // ------------------------------------------------------------ rooms
  function roomsList() {
    const free = data.areas.filter((a) => !a.configured);
    const cards = data.rooms.map((r) => `<a class="card level-room" href="#/light-level/rooms/${enc(r.area_id)}" data-room="${esc(r.area_id)}">
        <div class="row">${icon("room")}<strong>${esc(r.name)}</strong><span class="spacer"></span>${statusChip(r)}</div>
        <div class="sub">${r.effective.template ? `Template ${esc((template(r.template) || {}).name || "?")}` : "<span class=\"status bad\">No template</span>"} · ${r.source_state.entity_id ? esc(r.source_state.name || r.source_state.entity_id) : "UNSET: no sensor"}</div>
        <div class="sub">Now <strong>${esc(bandNow(r))}</strong>${r.reading.value != null ? ` · ${esc(lux(r.reading.value))}${r.reading.estimated ? " (estimated)" : ""}` : r.source_state.entity_id ? " · no reading" : ""}${r.computed && r.computed !== r.band ? ` · the reading calls for ${esc((r.effective.bands.find((b) => b.id === r.computed) || {}).name || r.computed)}` : ""}</div>
        <div class="sub">${r.used_by.length ? `Used by ${r.used_by.map((u) => esc(u.device)).join(", ")}` : "No light uses it yet"}${r.problems.length ? ` · <span class="status bad">${plural(r.problems.length, "problem")}</span>` : ""}</div></a>`).join("");
    return `<div class="card"><p class="small">Each room has its own light level: a sensor, a template of named light levels (inherited, with the room's own overrides) and, optionally, an ambient estimate. In Home Assistant it is one helper per room that every light of the room reads, so they all agree. Nothing is assigned for you: a room without a sensor has no light level.</p>
        <div class="row"><select id="l-new-room" aria-label="Room"><option value="">Add a room…</option>${opt(free.map((a) => [a.area_id, a.name]), "")}</select><button type="button" class="primary" data-act="room-add" ${free.length ? "" : "disabled"}>Add</button>
        ${data.templates.length ? "" : `<span class="muted small">Add a template first (Global Templates), or the room starts without one.</span>`}</div></div>
      <div class="grid">${cards || `<p class="muted">No room has a light level yet.</p>`}</div>`;
  }

  function roomEditor() {
    const r = work;
    const live = room();
    const eff = live.effective;
    const t = template(r.template);
    const bands = eff.bands.map((b, i) => {
      const over = (r.overrides.bands || {})[b.id];
      const last = i === eff.bands.length - 1;
      return `<tr data-band="${esc(b.id)}"><th scope="row">${esc(b.name)}<br><span class="mono small">${esc(b.id)}</span></th>
        <td>${last ? `<span class="muted">from ${esc(lux(eff.bands[i - 1] ? eff.bands[i - 1].below : 0))}</span>` : `${esc(lux(b.global))}`}</td>
        <td>${last ? "" : `<input type="number" min="0.1" step="any" data-chg="rband" data-band="${esc(b.id)}" value="${esc(over ? over.below : "")}" placeholder="inherit" aria-label="${esc(b.name)}: room limit"> lx`}</td>
        <td>${last ? "" : `${esc(lux(b.below))} — ${tag(b.source)}${over ? ` <button type="button" class="link small" data-act="rband-reset" data-band="${esc(b.id)}">reset</button>` : ""}`}</td></tr>`;
    }).join("");
    const settings = data.settings.map(({ id, name }) => {
      const s = eff.settings[id];
      const own = Object.prototype.hasOwnProperty.call(r.overrides, id);
      const unit = id === "hysteresis" ? "lx" : "s";
      const shown = (v) => (id === "hysteresis" ? (v == null ? "Unset" : lux(v)) : id === "unavailable_seconds" && v == null ? "Unset (keep the last level)" : secs(v));
      return `<tr data-setting="${esc(id)}"><th scope="row">${esc(name)}</th><td>${esc(shown(s.global))}</td>
        <td><input type="number" min="0" step="any" data-chg="rset" data-key="${esc(id)}" value="${esc(own && r.overrides[id] != null ? r.overrides[id] : "")}" placeholder="${own ? "unset" : "inherit"}" aria-label="${esc(name)}: room value"> ${unit}</td>
        <td>${esc(shown(s.value))} — ${tag(s.source)}${own ? ` <button type="button" class="link small" data-act="rset-reset" data-key="${esc(id)}">reset</button>` : ""}</td></tr>`;
    }).join("");
    const mats = ["truth", "brightness"].map((layer) => {
      const choices = data.matrices.filter((m) => m.layer === layer && m.template === r.template);
      const chosen = matrix(r.matrices[layer]);
      const cells = eff.bands.map((b) => {
        const own = (r.matrix_overrides[layer] || {})[b.id];
        const global = chosen ? chosen.cells[b.id] : null;
        return `<tr><th scope="row">${esc(b.name)}</th><td>${chosen ? esc(cellText(layer, global)) : "—"}</td>
          <td>${layer === "truth" ? `<select data-chg="rcell" data-layer="truth" data-band="${esc(b.id)}" aria-label="${esc(b.name)}: room value">${opt([["", "inherit"], ["on", "ON"], ["off", "OFF"], ["none", "NO ACTION"]], own ? own.kind : "")}</select>`
            : `<select data-chg="rcell" data-layer="brightness" data-band="${esc(b.id)}" aria-label="${esc(b.name)}: room value">${opt([["", "inherit"], ["set", "%"], ["none", "NO ACTION"]], own ? own.kind : "")}</select>${own && own.kind === "set" ? ` <input type="number" min="0" max="100" data-chg="rpct" data-band="${esc(b.id)}" value="${esc(own.pct ?? "")}" aria-label="${esc(b.name)}: %"> %` : ""}`}</td>
          <td>${esc(cellText(layer, own || global))} — ${tag(own ? "room" : global ? "global" : "unset")}</td></tr>`;
      }).join("");
      return `<div class="field"><label for="rm-${layer}">${layer === "truth" ? "Truth Matrix" : "Brightness Matrix"}</label><span><select id="rm-${layer}" data-chg="rmat" data-layer="${layer}"><option value="">None (UNSET)</option>${opt(choices.map((m) => [m.id, `${m.name} (v${m.version})`]), r.matrices[layer])}</select>
          ${choices.length ? "" : ` <span class="muted small">No ${layer} matrix for this template yet (Matrix Defaults).</span>`}</span></div>
        <div class="tscroll"><table class="levels"><thead><tr><th>Light level</th><th>Global</th><th>Room</th><th>Effective</th></tr></thead><tbody>${cells}</tbody></table></div>`;
    }).join("");
    const a = r.ambient;
    const method = `<div class="field"><label for="amb">Ambient estimate</label><select id="amb" data-chg="amethod">${opt(data.methods.map((m) => [m.id, m.name]), a.method)}</select>
        <div class="help">A normal light sensor cannot tell daylight from lamplight. An estimate is labelled “estimated”; without enough data the level has no reading instead of a guess. The App never switches a light to calibrate.</div></div>`;
    let ambient = "";
    if (a.method === "reference") ambient = `<div class="field"><label>Reference sensor</label><span><button type="button" class="entity ${a.reference ? "" : "unset"}" data-act="aref">${a.reference ? esc(entityName(a.reference.entity_id)) : "UNSET · choose a sensor the lamps do not reach"}</button>
        × <input type="number" min="0.001" step="any" data-chg="afactor" value="${esc(a.factor ?? "")}" placeholder="1" aria-label="Factor"></span><div class="help">For example an outdoor or window sensor; the factor scales it to this room.</div></div>`;
    if (a.method === "off_sample" || a.method === "calibrated") {
      const lights = a.lights.map((l) => {
        const obs = live.observed[l];
        const contrib = a.method === "calibrated" ? ` · contributes <input type="number" min="0" step="any" data-chg="acontrib" data-light="${esc(l)}" value="${esc(a.contributions[l] ?? "")}" placeholder="UNSET" aria-label="${esc(entityName(l))}: lux at full brightness"> lx at full brightness
          ${obs ? `<span class="small muted">observed ${esc(lux(obs.median))} (${plural(obs.count, "observation")}, ${esc(obs.confidence)})</span> <button type="button" class="link small" data-act="atake" data-light="${esc(l)}" data-v="${esc(obs.median)}">use it</button>` : `<span class="small muted">no observation yet</span>`}` : "";
        return `<li class="row">${icon("light")}<span>${esc(entityName(l))}</span>${contrib}<span class="spacer"></span><button type="button" class="link small danger-text" data-act="adrop" data-light="${esc(l)}">remove</button></li>`;
      }).join("");
      ambient = `<div class="field"><label>Lights taken into account</label><span><ul class="plain">${lights || `<li class="muted">None chosen.</li>`}</ul><button type="button" data-act="aadd">Add a light…</button></span></div>
        ${a.method === "off_sample" ? `<div class="field"><label>Settle time</label><span><input type="number" min="0" max="600" data-chg="anum" data-key="settle_seconds" value="${esc(a.settle_seconds ?? "")}" aria-label="Settle time"> s after all are off</span></div>
          <div class="field"><label>A sample is valid for</label><span><input type="number" min="1" max="1440" data-chg="anum" data-key="max_age_minutes" value="${esc(a.max_age_minutes ?? "")}" placeholder="UNSET" aria-label="Valid for"> min (then: no reading)</span></div>`
          : `<p class="muted small">Observations: when one of these lights changes while the sensor keeps reporting, the App notes how much the reading moved (it only watches; it never switches a light). Take a value over only when it is consistent.</p>`}`;
    }
    const read = live.reading;
    const liveBox = `<dl class="kv"><dt>Sensor</dt><dd>${live.source_state.entity_id ? `${esc(live.source_state.name || "")} <span class="mono">${esc(live.source_state.entity_id)}</span>` : "UNSET"}</dd>
        <dt>Reading</dt><dd>${live.source_state.entity_id ? `${esc(live.source_state.state ?? "?")}${live.source_state.unit ? ` ${esc(live.source_state.unit)}` : ""} · ${live.source_state.available ? "available" : "<strong>unavailable</strong>"}${live.source_state.last_updated ? ` · updated ${esc(clock(live.source_state.last_updated))}` : ""}` : "—"}</dd>
        ${read.method !== "none" ? `<dt>Raw</dt><dd>${esc(lux(read.raw))}</dd><dt>Lamps (estimated)</dt><dd>${esc(lux(read.artificial))}</dd>` : ""}
        <dt>Used for the level</dt><dd>${esc(lux(read.value))} · ${read.estimated ? "estimated" : "measured"} (${esc(read.method_text)})${read.confidence ? ` · ${esc(read.confidence)}` : ""}</dd>
        <dt>Level now</dt><dd><strong>${esc(bandNow(live))}</strong> <span class="muted small">(${esc(live.helpers.band)})</span></dd>
        <dt>The reading calls for</dt><dd>${esc((eff.bands.find((b) => b.id === live.computed) || {}).name || "no level (no reading)")}${live.computed && live.computed !== live.band ? " · changes after the stabilisation delay" : ""}</dd></dl>`;
    const users = live.used_by.map((u) => `<li><a href="#/design/${enc(u.device_id)}/${enc(u.designer_id)}/truth">${esc(u.device)}</a> · ${esc(u.lights.map(entityName).join(", "))} <span class="muted small">(${esc(u.matrices.map((m) => `${m.layer} matrix “${m.name}”`).join(", "))})</span></li>`).join("");
    const cands = live.candidates.map((c) => `<li class="row"><span>${esc(c.device)} · ${esc(c.lights.map(entityName).join(", ") || "no lights yet")}</span><span class="spacer"></span><button type="button" data-act="use-in" data-device="${esc(c.device_id)}" data-designer="${esc(c.designer_id)}">Add Light Level matrices there</button></li>`).join("");
    return `<p><a href="#/light-level/rooms">← All rooms</a></p>
      <h1>${icon("room")}${esc(live.name)} <span class="spacer"></span>${statusChip(live)}<a class="button primary" href="#/devices/${enc(live.device_id)}">Review &amp; deploy</a></h1>
      <p class="save-status" id="l-status" role="status" aria-live="polite"></p>
      ${live.area_exists ? "" : `<div class="notice warning">This area no longer exists in Home Assistant. The light level keeps working; remove it if it is no longer needed.</div>`}
      ${notes(live.problems)}${live.loop.map((t) => `<div class="notice warning">${esc(t)}</div>`).join("")}
      <div class="two"><div>
        <div class="card"><h2>1. Light sensor</h2><div class="field"><label>Measurement source</label><span><button type="button" class="entity ${r.source ? "" : "unset"}" data-act="rsrc">${r.source ? esc(entityName(r.source.entity_id)) : "UNSET · choose the room's light sensor"}</button>
          ${r.source ? `<button type="button" class="link small" data-act="rsrc-clear">clear</button>` : ""}</span><div class="help">Physical sensors, template or filtered sensors. A room never uses another room's sensor by itself; sharing one is a choice you make here.</div></div></div>
        <div class="card"><h2>2. Template</h2><div class="field"><label for="rtpl">Light-level template</label><select id="rtpl" data-chg="rtpl"><option value="">None (UNSET)</option>${opt(data.templates.map((x) => [x.id, `${x.name} (v${x.version})`]), r.template)}</select></div>
          ${t ? `<p class="muted small">Inherited values are marked <span class="badge src-global">Global</span>; the room's own <span class="badge src-room">Room override</span> stays when the template changes. Clear a room value (or “reset”) to inherit again.</p>
          <div class="tscroll"><table class="levels"><thead><tr><th>Light level</th><th>Global limit (below)</th><th>Room</th><th>Effective</th></tr></thead><tbody>${bands}</tbody></table></div>
          <h3>Stabilisation</h3><div class="tscroll"><table class="levels"><thead><tr><th></th><th>Global</th><th>Room</th><th>Effective</th></tr></thead><tbody>${settings}</tbody></table></div>
          ${eff.orphans.length ? `<p class="small muted">Room limits for levels the template no longer has (kept, not used): ${esc(eff.orphans.join(", "))}</p>` : ""}` : `<p class="muted">Choose a template: its light levels and their limits are inherited.</p>`}</div>
        ${t ? `<div class="card"><h2>3. Default matrices</h2><p class="small">The values the lights of this room start from. A light's own value (in its Lighting Designer) wins; a room value wins over the global one.</p>${mats}</div>` : ""}
        <div class="card"><h2>4. Ambient estimate (optional)</h2>${method}${ambient}</div>
      </div><div>
        <div class="card"><h2>Now</h2>${liveBox}</div>
        <div class="card"><h2>Lights that use it</h2><ul class="plain">${users || `<li class="muted">None yet.</li>`}</ul>
          ${cands ? `<h3>Lighting Designers in this room</h3><ul class="plain">${cands}</ul>` : `<p class="muted small">Add a Light Level matrix in a light's Lighting Designer (Truth or Brightness Matrix tab).</p>`}</div>
        <div class="card"><h2>In Home Assistant</h2><p class="small">One helper (<span class="mono">${esc(live.helpers.band)}</span>${r.ambient.method === "off_sample" ? ` and <span class="mono">${esc(live.helpers.ambient)}</span>` : ""}) and one automation, created when you deploy. They run without the App.</p>
          <div class="actions"><a class="button" href="#/devices/${enc(live.device_id)}">Review &amp; deploy</a><button type="button" class="danger" data-act="room-drop">Remove this room's light level</button></div></div>
      </div></div>`;
  }

  // ------------------------------------------------------------ templates
  function templatesList() {
    const cards = data.templates.map((t) => `<div class="card level-template" data-template="${esc(t.id)}"><div class="row"><strong>${esc(t.name)}</strong> <span class="badge">v${t.version}</span>${t.default ? `<span class="badge">standard for new rooms</span>` : ""}<span class="spacer"></span>
        <a class="button" href="#/light-level/templates/${enc(t.id)}">Edit</a><button type="button" data-act="tdup" data-id="${esc(t.id)}">Duplicate</button><button type="button" class="link danger-text" data-act="tdel" data-id="${esc(t.id)}">Delete</button></div>
        <div class="sub">${t.bands.map((b, i) => `${esc(b.name)} ${i ? `≥ ${esc(lux(t.bands[i - 1].below))}` : ""}`).join(" · ")}</div>
        <div class="sub">Hysteresis ${esc(t.hysteresis == null ? "Unset" : lux(t.hysteresis))} · darker after ${esc(secs(t.darker_seconds))} · brighter after ${esc(secs(t.brighter_seconds))}</div>
        <div class="sub">${t.used_by.length ? `Rooms: ${t.used_by.map((u) => esc(u.name)).join(", ")}` : "No room uses it"}</div></div>`).join("");
    return `<div class="card"><p class="small">A template names light levels from dark to bright, with an upper limit for each but the brightest (so there are no gaps and no overlaps), a hysteresis and stabilisation delays. Rooms inherit it and may override single values. Ids never change when a level is renamed.</p>
        <div class="actions"><button type="button" data-act="tpreset">Add “Standard light levels” (example values)</button><button type="button" data-act="tnew">New template</button>
        <label class="small">Standard for new rooms <select data-chg="tdefault" aria-label="Standard template"><option value="">None</option>${opt(data.templates.map((t) => [t.id, t.name]), data.default_template)}</select></label></div></div>
      <div class="grid">${cards || `<p class="muted">No template yet.</p>`}</div>`;
  }

  function templateEditor() {
    const t = work;
    const live = template(t.id) || t;
    const rows = t.bands.map((b, i) => {
      const last = i === t.bands.length - 1;
      return `<tr data-band="${esc(b.id)}"><td><input type="text" data-chg="tbname" data-i="${i}" value="${esc(b.name)}" maxlength="40" aria-label="Name of level ${i + 1}"><br><span class="mono small">${esc(b.id)}</span></td>
        <td>${i ? esc(lux(t.bands[i - 1].below)) : "0 lx"}</td>
        <td>${last ? `<span class="muted">no upper limit</span>` : `<input type="number" min="0.1" step="any" data-chg="tbelow" data-i="${i}" value="${esc(b.below ?? "")}" aria-label="Upper limit of ${esc(b.name)}"> lx`}</td>
        <td><button type="button" class="link" data-act="tbup" data-i="${i}" ${i ? "" : "disabled"} aria-label="Move darker">↑</button><button type="button" class="link" data-act="tbdown" data-i="${i}" ${last ? "disabled" : ""} aria-label="Move brighter">↓</button>
          <button type="button" class="link danger-text" data-act="tbdrop" data-i="${i}" ${t.bands.length > 2 ? "" : "disabled"}>remove</button></td></tr>`;
    }).join("");
    const users = live.used_by || [];
    const affected = users.map((u) => `<li>${esc(u.name)}: ${Object.keys(u.bands).length || u.settings.length ? `keeps its own ${esc([...Object.entries(u.bands).map(([b, v]) => `${(t.bands.find((x) => x.id === b) || { name: b }).name} ${lux(v)}`), ...u.settings].join(", "))}; inherits the rest` : "inherits everything"}${u.deployed ? " · deployed: changes apply when you deploy it again" : " · not deployed"}</li>`).join("");
    return `<p><a href="#/light-level/templates">← All templates</a></p>
      <h1>${esc(t.name)} <span class="badge">v${live.version}</span></h1><p class="save-status" id="l-status" role="status" aria-live="polite"></p>
      <div class="card"><div class="field"><label for="tname">Name</label><input id="tname" type="text" data-chg="tname" value="${esc(t.name)}" maxlength="60"></div>
        <h2>Light levels (dark → bright)</h2><div class="tscroll"><table class="levels"><thead><tr><th>Name / id</th><th>From</th><th>Below</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
        <div class="row"><input type="text" id="tb-new" placeholder="New level name" maxlength="40" aria-label="New level name"><button type="button" data-act="tbadd" ${t.bands.length < 8 ? "" : "disabled"}>Add a level</button><span class="muted small">It is added before the brightest; give it its limit.</span></div>
        <h2>Stabilisation</h2>
        <div class="field"><label for="th">Hysteresis</label><span><input id="th" type="number" min="0" step="any" data-chg="tset" data-key="hysteresis" value="${esc(t.hysteresis ?? "")}" placeholder="unset"> lx</span><div class="help">The level changes only when the reading is past a limit by this much (against a lamp that lifts the reading a little, and a noisy sensor).</div></div>
        <div class="field"><label for="tdk">Darker after</label><span><input id="tdk" type="number" min="0" max="3600" data-chg="tset" data-key="darker_seconds" value="${esc(t.darker_seconds ?? "")}" placeholder="unset"> s</span></div>
        <div class="field"><label for="tbr">Brighter after</label><span><input id="tbr" type="number" min="0" max="3600" data-chg="tset" data-key="brighter_seconds" value="${esc(t.brighter_seconds ?? "")}" placeholder="unset"> s</span><div class="help">The reading must stay past the limit this long (a passing cloud, a lamp switched on a moment ago).</div></div>
        <div class="field"><label for="tun">Without a reading for</label><span><input id="tun" type="number" min="0" max="86400" data-chg="tset" data-key="unavailable_seconds" value="${esc(t.unavailable_seconds ?? "")}" placeholder="unset: keep the last level"> s → no reading</span><div class="help">Then no matrix column holds: the lights are left as they are. Unset keeps the last level.</div></div></div>
      <div class="card"><h2>Rooms that use it</h2><ul class="plain">${affected || `<li class="muted">None.</li>`}</ul><p class="muted small">Saving changes nothing in Home Assistant: each room shows “changes not deployed” and its deployment review shows exactly what changes. Room overrides are never replaced.</p></div>`;
  }

  // ------------------------------------------------------------ default matrices
  function matricesList() {
    const cards = data.matrices.map((m) => {
      const t = template(m.template);
      return `<div class="card level-matrix" data-matrix="${esc(m.id)}"><div class="row"><strong>${esc(m.name)}</strong> <span class="badge">${m.layer === "truth" ? "Truth" : "Brightness"}</span><span class="badge">v${m.version}</span>${m.default ? `<span class="badge">standard</span>` : ""}<span class="spacer"></span>
        <a class="button" href="#/light-level/matrices/${enc(m.id)}">Edit</a><button type="button" class="link danger-text" data-act="mxdel" data-id="${esc(m.id)}">Delete</button></div>
        <div class="sub">${t ? t.bands.map((b) => `${esc(b.name)} ${esc(cellText(m.layer, m.cells[b.id]))}`).join(" · ") : "template missing"}</div>
        <div class="sub">${m.used_by.length ? `Rooms: ${m.used_by.map(esc).join(", ")}` : "No room uses it"}</div></div>`;
    }).join("");
    return `<div class="card"><p class="small">A default matrix is a starting configuration for rooms, never a command to every light: a room adopts it on purpose, and only lights that have a Light Level matrix follow it.</p>
        <div class="row"><select id="mx-tpl" aria-label="Template"><option value="">For the template…</option>${opt(data.templates.map((t) => [t.id, t.name]), "")}</select>
          <button type="button" data-act="mxnew" data-layer="truth">New Truth Matrix</button><button type="button" data-act="mxnew" data-layer="brightness">New Brightness Matrix</button></div>
        <div class="row"><label class="small">Standard Truth Matrix for new rooms <select data-chg="mxdefault" data-layer="truth" aria-label="Standard Truth Matrix"><option value="">None</option>${opt(data.matrices.filter((m) => m.layer === "truth").map((m) => [m.id, m.name]), data.default_matrices.truth)}</select></label>
          <label class="small">Standard Brightness Matrix <select data-chg="mxdefault" data-layer="brightness" aria-label="Standard Brightness Matrix"><option value="">None</option>${opt(data.matrices.filter((m) => m.layer === "brightness").map((m) => [m.id, m.name]), data.default_matrices.brightness)}</select></label></div></div>
      <div class="grid">${cards || `<p class="muted">No default matrix yet.</p>`}</div>`;
  }

  function matrixEditor() {
    const m = work;
    const t = template(m.template);
    const rows = (t ? t.bands : []).map((b) => {
      const c = m.cells[b.id];
      const edit = m.layer === "truth"
        ? `<select data-chg="mxcell" data-band="${esc(b.id)}" aria-label="${esc(b.name)}">${opt([["", "UNSET"], ["on", "ON"], ["off", "OFF"], ["none", "NO ACTION"]], c ? c.kind : "")}</select>`
        : `<select data-chg="mxcell" data-band="${esc(b.id)}" aria-label="${esc(b.name)}">${opt([["", "UNSET"], ["set", "%"], ["none", "NO ACTION"]], c ? c.kind : "")}</select>${c && c.kind === "set" ? ` <input type="number" min="0" max="100" data-chg="mxpct" data-band="${esc(b.id)}" value="${esc(c.pct ?? "")}" aria-label="${esc(b.name)} %"> %` : ""}`;
      return `<tr><th scope="row">${esc(b.name)}</th><td>${edit}</td></tr>`;
    }).join("");
    return `<p><a href="#/light-level/matrices">← All default matrices</a></p>
      <h1>${esc(m.name)} <span class="badge">${m.layer === "truth" ? "Truth Matrix" : "Brightness Matrix"}</span></h1><p class="save-status" id="l-status" role="status" aria-live="polite"></p>
      <div class="card"><div class="field"><label for="mxname">Name</label><input id="mxname" type="text" data-chg="mxname" value="${esc(m.name)}" maxlength="60"></div>
        <p class="small">Template: ${esc(t ? t.name : "missing")}. ${m.layer === "truth" ? "ON or OFF per light level. OFF gates every brightness action." : "The brightness per light level (it never switches a light on; 0 % follows the light's Lighting Designer setting)."}</p>
        <div class="tscroll"><table class="levels"><thead><tr><th>Light level</th><th>${m.layer === "truth" ? "State" : "Brightness"}</th></tr></thead><tbody>${rows}</tbody></table></div></div>
      <div class="card"><h2>Rooms that use it</h2><p>${(matrix(m.id) || {}).used_by && matrix(m.id).used_by.length ? matrix(m.id).used_by.map(esc).join(", ") : "None."}</p></div>`;
  }

  // ------------------------------------------------------------ sources and diagnostics
  function sourcesTab() {
    const rows = data.rooms.map((r) => `<tr><th scope="row"><a href="#/light-level/rooms/${enc(r.area_id)}">${esc(r.name)}</a></th>
        <td>${r.source_state.entity_id ? `${esc(r.source_state.name || "")}<br><span class="mono small">${esc(r.source_state.entity_id)}</span>` : "UNSET"}</td>
        <td>${esc(r.source_state.state ?? "—")}${r.source_state.unit ? ` ${esc(r.source_state.unit)}` : ""}${r.source_state.entity_id && !r.source_state.available ? " <strong>(unavailable)</strong>" : ""}</td>
        <td>${esc(r.source_state.last_updated ? clock(r.source_state.last_updated) : "—")}</td>
        <td>${r.reading.estimated ? "estimated" : "measured"}<br><span class="small muted">${esc(r.reading.method_text)}</span></td>
        <td>${esc(lux(r.reading.value))}${r.reading.confidence ? `<br><span class="small muted">${esc(r.reading.confidence)}</span>` : ""}</td></tr>`).join("");
    const shared = {};
    data.rooms.forEach((r) => { if (r.source_state.entity_id) (shared[r.source_state.entity_id] = shared[r.source_state.entity_id] || []).push(r.name); });
    const multi = Object.entries(shared).filter(([, names]) => names.length > 1);
    return `<div class="card"><h2>Measurement sources</h2><div class="tscroll"><table class="levels"><thead><tr><th>Room</th><th>Source</th><th>Reading</th><th>Last update</th><th>Measured or estimated</th><th>Used for the level</th></tr></thead><tbody>${rows || `<tr><td colspan="6" class="muted">No room yet.</td></tr>`}</tbody></table></div>
      ${multi.length ? `<p class="small">Shared on purpose: ${multi.map(([e, names]) => `${esc(entityName(e))} (${names.map(esc).join(", ")})`).join("; ")}</p>` : ""}
      <p class="muted small">Ambient Lux Helper: with “sampled while the lights are off” each room gets a number helper with the last sample. Active calibration (switching lights on purpose to measure them) is not offered: the App never controls a light itself.</p></div>`;
  }

  function diagnosticsTab() {
    return data.rooms.map((r) => `<div class="card" data-diag="${esc(r.area_id)}"><div class="row"><h2>${esc(r.name)}</h2><span class="spacer"></span>${statusChip(r)}</div>
        <dl class="kv"><dt>Helper</dt><dd class="mono">${esc(r.helpers.band)} = ${esc(r.band ?? "(not created yet)")}</dd>
          <dt>The reading calls for</dt><dd>${esc(r.computed ?? "no level")}</dd><dt>Value</dt><dd>${esc(lux(r.reading.value))} (${r.reading.estimated ? "estimated" : "measured"})</dd>
          <dt>Effective limits</dt><dd>${r.effective.bands.slice(0, -1).map((b) => `${esc(b.name)} &lt; ${esc(lux(b.below))} (${esc(SOURCE[b.source] || "")})`).join(" · ") || "—"}</dd>
          <dt>Hysteresis / delays</dt><dd>${esc(lux(r.effective.settings.hysteresis.value))} · darker ${esc(secs(r.effective.settings.darker_seconds.value))} · brighter ${esc(secs(r.effective.settings.brighter_seconds.value))} · no reading after ${esc(secs(r.effective.settings.unavailable_seconds.value))}</dd>
          <dt>Observations</dt><dd>${Object.entries(r.observed).map(([l, o]) => `${esc(entityName(l))}: ${o ? `${esc(lux(o.median))} × ${o.count} (${esc(o.confidence)})` : "none"}`).join("<br>") || "—"}</dd></dl>
        ${notes(r.problems)}${r.loop.map((t) => `<div class="notice warning">${esc(t)}</div>`).join("")}
        ${Object.keys(r.observed).length ? `<button type="button" class="link small" data-act="obs-clear" data-area="${esc(r.area_id)}">Forget the observations</button>` : ""}</div>`).join("") || `<p class="muted">No room yet.</p>`;
  }

  // ------------------------------------------------------------ draw
  function draw() {
    if (!current()) return;
    let body;
    if (tab === "rooms") body = work ? roomEditor() : roomsList();
    else if (tab === "templates") body = work ? templateEditor() : templatesList();
    else if (tab === "matrices") body = work ? matrixEditor() : matricesList();
    else if (tab === "sources") body = sourcesTab();
    else body = diagnosticsTab();
    const statusLine = view.querySelector("#l-status");
    const keep = statusLine ? [statusLine.textContent, statusLine.className] : null;
    view.innerHTML = `${work ? "" : `<h1>Light Level</h1>`}${tabs()}${work ? "" : `<p class="save-status" id="l-status" role="status" aria-live="polite"></p>`}${body}`;
    if (keep) { const el = view.querySelector("#l-status"); if (el) { el.textContent = keep[0]; el.className = keep[1]; } }
  }

  const act = {
    "room-add": async () => {
      const area = (view.querySelector("#l-new-room") || {}).value;
      if (!area) { toast("Choose the room first."); return; }
      try { await change("api/light-levels/room/save", { room: { area_id: area, create: true } }); location.hash = `#/light-level/rooms/${enc(area)}`; }
      catch (err) { toast(errorText(err)); }
    },
    "room-drop": async () => {
      const r = room();
      const yes = await ask({ title: `Remove the light level of ${r.name}?`, body: `<p>Its automation and helpers are removed from Home Assistant${r.status && r.status.status !== "not_deployed" ? "" : " (nothing is deployed yet)"}; the room's settings go. Templates and default matrices stay.</p>`, ok: "Remove", danger: true });
      if (!yes) return;
      try { await change("api/light-levels/room/delete", { area_id: r.area_id, overwrite: false }); location.hash = "#/light-level/rooms"; }
      catch (err) {
        if (err.data && err.data.error === "modified" && await ask({ title: "Changed in Home Assistant", body: `<p>${esc(errorText(err))}</p><p>Remove it anyway?</p>`, ok: "Remove anyway", danger: true })) {
          try { await change("api/light-levels/room/delete", { area_id: r.area_id, overwrite: true }); location.hash = "#/light-level/rooms"; } catch (e2) { toast(errorText(e2)); }
        } else toast(errorText(err));
      }
    },
    rsrc: async () => { const found = await pickLux(work.area_id, room().name); if (!found || !current()) return; await loadEntities([found.entity_id]); work.source = { entity_id: found.entity_id, attribute: null }; save("room"); },
    "rsrc-clear": () => { work.source = null; save("room"); },
    "rband-reset": (el) => { delete work.overrides.bands[el.dataset.band]; save("room"); },
    "rset-reset": (el) => { delete work.overrides[el.dataset.key]; save("room"); },
    aref: async () => { const found = await pickLux(null, "the reference"); if (!found || !current()) return; await loadEntities([found.entity_id]); work.ambient.reference = { entity_id: found.entity_id, attribute: null }; save("room"); },
    aadd: async () => { const found = await pickEntity("light", { room: { area_id: work.area_id, name: room().name }, only: true }); if (!found || !current()) return; await loadEntities([found.entity_id]); if (!work.ambient.lights.includes(found.entity_id)) work.ambient.lights.push(found.entity_id); save("room"); },
    adrop: (el) => { work.ambient.lights = work.ambient.lights.filter((l) => l !== el.dataset.light); delete work.ambient.contributions[el.dataset.light]; save("room"); },
    atake: (el) => { work.ambient.contributions[el.dataset.light] = Number(el.dataset.v); save("room"); },
    "use-in": async (el) => {
      try {
        const page = await api(`api/designers/${enc(el.dataset.device)}/${enc(el.dataset.designer)}`);
        const d = clone(page.designer.config);
        const name = `Light level · ${room().name}`.slice(0, 40);
        const id = () => `m_${Math.random().toString(16).slice(2, 6).padEnd(4, "0")}`;
        if (d.truth.matrices.length >= 4 || d.brightness.matrices.length >= 4) { toast("That designer has four matrices of a kind already."); return; }
        d.truth.matrices.push({ id: id(), name, enabled: true, level: { room: work.area_id }, columns: [], cells: {}, row_defaults: {}, default: null, policy: null, ranks: [] });
        d.brightness.matrices.push({ id: id(), name, enabled: true, level: { room: work.area_id }, columns: [], cells: {}, row_defaults: {}, default: null, policy: null, ranks: [] });
        await change("api/designers/save", { device_id: el.dataset.device, designer: d });
        toast("Added: a Truth and a Brightness Matrix from this room's light level. Deploy that device to apply it.");
        reload();
      } catch (err) { toast(errorText(err)); }
    },
    tpreset: async () => { try { const r = await change("api/light-levels/template/preset", {}); location.hash = `#/light-level/templates/${enc(r.template_id)}`; } catch (err) { toast(errorText(err)); } },
    tnew: async () => {
      try {
        const r = await change("api/light-levels/template/save", { template: { name: "New light levels", bands: [{ id: "dark", name: "Dark", below: 50 }, { id: "bright", name: "Bright", below: null }], hysteresis: null, darker_seconds: null, brighter_seconds: null, unavailable_seconds: null } });
        location.hash = `#/light-level/templates/${enc(r.template_id)}`;
      } catch (err) { toast(errorText(err)); }
    },
    tdup: async (el) => { try { const r = await change("api/light-levels/template/duplicate", { template_id: el.dataset.id }); location.hash = `#/light-level/templates/${enc(r.template_id)}`; } catch (err) { toast(errorText(err)); } },
    tdel: async (el) => {
      const t = template(el.dataset.id);
      if (!(await ask({ title: `Delete “${t.name}”?`, body: t.used_by.length ? `<p>Rooms use it: ${t.used_by.map((u) => esc(u.name)).join(", ")}. Choose another template there first.</p>` : "", ok: "Delete", danger: true }))) return;
      try { await change("api/light-levels/template/delete", { template_id: t.id }); reload(); } catch (err) { toast(errorText(err)); }
    },
    tbadd: () => {
      const name = ((view.querySelector("#tb-new") || {}).value || "").trim();
      if (!name) { toast("Give the new level a name."); return; }
      let ident = slugOf(name);
      while (work.bands.some((b) => b.id === ident) || ["no_reading", "unknown", "unavailable", "none", "off", "on"].includes(ident)) ident = `${ident.slice(0, 21)}_${Math.floor(Math.random() * 90 + 10)}`;
      work.bands.splice(work.bands.length - 1, 0, { id: ident, name, below: null });
      draw(); status("Give the new level its upper limit to save it.");
    },
    tbdrop: async (el) => {
      const i = Number(el.dataset.i);
      const b = work.bands[i];
      if (!(await ask({ title: `Remove the level “${b.name}”?`, body: "<p>Matrices and rooms that set a value for it keep that value, unused, until you change them.</p>", ok: "Remove", danger: true }))) return;
      work.bands.splice(i, 1);
      if (work.bands.length) work.bands[work.bands.length - 1].below = null;
      save("template");
    },
    tbup: (el) => swapBands(Number(el.dataset.i), -1),
    tbdown: (el) => swapBands(Number(el.dataset.i), 1),
    mxnew: async (el) => {
      const tpl = (view.querySelector("#mx-tpl") || {}).value;
      if (!tpl) { toast("Choose the template first."); return; }
      try {
        const r = await change("api/light-levels/matrix/save", { matrix: { name: el.dataset.layer === "truth" ? "Default Truth Matrix" : "Default Brightness Matrix", layer: el.dataset.layer, template: tpl, cells: {} } });
        location.hash = `#/light-level/matrices/${enc(r.matrix_id)}`;
      } catch (err) { toast(errorText(err)); }
    },
    mxdel: async (el) => {
      const m = matrix(el.dataset.id);
      if (!(await ask({ title: `Delete “${m.name}”?`, ok: "Delete", danger: true }))) return;
      try { await change("api/light-levels/matrix/delete", { matrix_id: m.id }); reload(); } catch (err) { toast(errorText(err)); }
    },
    "obs-clear": async (el) => { try { await change("api/light-levels/observations/clear", { area_id: el.dataset.area }); reload(); } catch (err) { toast(errorText(err)); } },
  };
  function swapBands(i, dir) {
    const j = i + dir;
    // the names and ids move; the limits stay where they are (the order is dark → bright)
    const limits = work.bands.map((b) => b.below);
    [work.bands[i], work.bands[j]] = [work.bands[j], work.bands[i]];
    work.bands.forEach((b, k) => { b.below = limits[k]; });
    save("template");
  }
  const num = (v) => (v === "" ? null : Number(v));
  const chg = {
    rband: (el) => { const v = num(el.value); if (v == null) delete work.overrides.bands[el.dataset.band]; else work.overrides.bands[el.dataset.band] = { below: v }; save("room"); },
    rset: (el) => { if (el.value === "") delete work.overrides[el.dataset.key]; else work.overrides[el.dataset.key] = num(el.value); save("room"); },
    rtpl: (el) => { work.template = el.value || null; work.matrices = { truth: null, brightness: null }; save("room"); },
    rmat: (el) => { work.matrices[el.dataset.layer] = el.value || null; save("room"); },
    rcell: (el) => {
      const cells = work.matrix_overrides[el.dataset.layer];
      if (!el.value) delete cells[el.dataset.band];
      else cells[el.dataset.band] = el.value === "set" ? { kind: "set", pct: (cells[el.dataset.band] || {}).pct ?? 50 } : { kind: el.value };
      save("room");
    },
    rpct: (el) => { const c = work.matrix_overrides.brightness[el.dataset.band]; if (c) { c.pct = num(el.value); if (c.pct != null) save("room"); } },
    amethod: (el) => { work.ambient.method = el.value; save("room"); },
    afactor: (el) => { work.ambient.factor = num(el.value); save("room"); },
    anum: (el) => { work.ambient[el.dataset.key] = num(el.value); save("room"); },
    acontrib: (el) => { const v = num(el.value); if (v == null) delete work.ambient.contributions[el.dataset.light]; else work.ambient.contributions[el.dataset.light] = v; save("room"); },
    tname: (el) => { work.name = el.value; if (el.value.trim()) save("template"); else status("Give the template a name to save it."); },
    tbname: (el) => { work.bands[Number(el.dataset.i)].name = el.value; if (el.value.trim()) save("template"); },
    tbelow: (el) => { work.bands[Number(el.dataset.i)].below = num(el.value); if (work.bands.every((b, k) => k === work.bands.length - 1 || b.below != null)) save("template"); },
    tset: (el) => { work[el.dataset.key] = num(el.value); save("template"); },
    tdefault: async (el) => { try { await change("api/light-levels/defaults", { defaults: { default_template: el.value || null } }); reload(); status("Saved", "ok"); } catch (err) { toast(errorText(err)); } },
    mxdefault: async (el) => { try { await change("api/light-levels/defaults", { defaults: { default_matrices: { [el.dataset.layer]: el.value || null } } }); reload(); status("Saved", "ok"); } catch (err) { toast(errorText(err)); } },
    mxname: (el) => { work.name = el.value; if (el.value.trim()) save("matrix"); },
    mxcell: (el) => {
      if (!el.value) delete work.cells[el.dataset.band];
      else work.cells[el.dataset.band] = el.value === "set" ? { kind: "set", pct: (work.cells[el.dataset.band] || {}).pct ?? 50 } : { kind: el.value };
      save("matrix");
    },
    mxpct: (el) => { const c = work.cells[el.dataset.band]; if (c) { c.pct = num(el.value); if (c.pct != null) save("matrix"); } },
  };

  draw();
  return { act, chg, inp: {} };
}
