// Automation Builder - the Heating Designer (0.5) and the heating profiles (templates and room profiles).
// Ordinary thermostat automation: schedules, day/night, a mode helper, presence, windows, away/home and manual
// override, with an explicit priority. No prediction, no learning, no other App. Saved as soon as something changes;
// nothing reaches Home Assistant before the device is deployed. Values, inheritance and previews come from the
// backend (the same plan the compiler uses): the interface decides nothing.

import { api, ask, change, enc, entities, entityName, errorText, esc, icon, loadEntities, notes, pickEntity, toast, view } from "./ui.js";

const clone = (v) => JSON.parse(JSON.stringify(v));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const opt = (list, current) => list.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(current ?? "") ? "selected" : ""}>${esc(l)}</option>`).join("");
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const LAYER = { window: "Windows & doors", override: "Manual override", away: "Away / Home", presence: "Presence", schedule: "Schedule & modes" };
const LAYER_HELP = { window: "an open window applies its policy (a mode, frost protection, OFF or nothing)", override: "a manual change holds until its policy ends it",
  away: "nobody home → Away; back home → Return (optional)", presence: "the room vacant for a while → a setback mode", schedule: "the time of day (or a helper) names the mode" };
const SOURCE = { thermostat: "Thermostat override", room: "Room profile", global: "Global", unset: "Unset" };
const deg = (v) => (v == null ? "—" : `${v}°`);
const cellText = (c) => (!c ? "UNSET" : c.kind === "set" ? deg(c.temp) : c.kind === "preset" ? `preset ${c.preset}` : "NO ACTION");

export async function heatingPage(deviceId, designerId, tab, current, first) {
  let data = first;
  let d = clone(data.designer.config);
  let chain = Promise.resolve();
  let sim = { inputs: null, result: null, scenario: { states: {}, time: null, weekday: null } };
  const tabs = [["thermostats", "Thermostats"], ["schedule", "Schedule & modes"], ["presence", "Presence"], ["window", "Windows & doors"], ["away", "Away / Home"],
    ["override", "Manual override"], ["priority", "Priority"], ["preview", "Preview"], ["yaml", "YAML"]];
  tab = tabs.some(([k]) => k === tab) ? tab : "thermostats";
  const caps = () => data.capabilities || {};
  const profiles = () => data.profiles || { rooms: [], templates: [] };
  const resolved = () => ((data.designer.view || {}).resolved) || { modes: [], cells: {} };
  const modes = () => resolved().modes || [];
  const status = (text, cls = "") => { const el = view.querySelector("#d-status"); if (el) { el.className = `save-status ${cls}`; el.textContent = text; } };
  const problemsOf = (layer) => notes((data.designer.problems || []).filter((p) => (layer ? p.layer === layer : !p.layer)).map((p) => ({ level: p.level === "blocking" ? "error" : p.level, message: p.message })));
  await loadEntities([...data.device.targets, ...d.window.sensors, d.away.entity_id, d.modes.entity.entity_id, (d.sensor || {}).entity_id].filter(Boolean));

  function incomplete() {
    const slots = [...d.modes.schedule.all, ...d.modes.schedule.weekdays, ...d.modes.schedule.weekend];
    if (slots.some((s) => !s.at)) return "Give the new entry its time to save";
    if (slots.some((s) => !s.mode && s.temp == null)) return "Choose the entry's mode or temperature to save";
    return "";
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
      } catch (err) { status(`Not saved: ${errorText(err)}`, "bad"); }
    });
    return chain;
  }

  const get = (path) => path.split("/").reduce((o, k) => (o == null ? o : o[k]), d);
  function set(path, value) { const keys = path.split("/"); const last = keys.pop(); const holder = keys.reduce((o, k) => o[k], d); holder[last] = value; }
  const entityBtn = (act, entityId, label) => `<button type="button" class="entity ${entityId ? "" : "unset"}" data-act="${esc(act)}" title="${esc(entityId || "")}">${entityId ? esc(entityName(entityId)) : `UNSET · ${esc(label)}`}</button>`;
  const toggle = (path, on, text, help = "") => `<label class="switch"><input type="checkbox" data-chg="flag" data-path="${esc(path)}" ${on ? "checked" : ""}> ${esc(text)}</label>${help ? `<div class="help">${esc(help)}</div>` : ""}`;
  const num = (path, value, attrs = "", unit = "") => `<input type="number" step="any" data-chg="num" data-path="${esc(path)}" value="${esc(value ?? "")}" placeholder="unset" ${attrs}>${unit ? ` ${esc(unit)}` : ""}`;
  const live = (entityId) => { const e = entities[entityId]; return e ? `<span class="badge">now: ${esc(e.state ?? "?")}${e.unit ? ` ${esc(e.unit)}` : ""}</span>` : ""; };
  const modeSel = (path, value, extra = []) => `<select data-chg="text" data-path="${esc(path)}" aria-label="Mode"><option value="">Choose a mode…</option>${opt([...modes().map((m) => [m.id, m.name]), ...extra], value)}</select>`;
  function capText(c) {
    if (!c || c.unknown) return "capabilities unknown (unavailable?)";
    return [c.hvac_modes.join(", "), c.min_temp != null ? `${c.min_temp}–${c.max_temp}°${c.step ? ` step ${c.step}` : ""}` : "", c.range && !c.target ? "a temperature range" : "",
      c.presets && c.preset_modes.length ? `presets: ${c.preset_modes.join(", ")}` : "", c.measures ? "reports a temperature" : "no own temperature", c.reports_action ? "reports heating activity" : ""].filter(Boolean).join(" · ");
  }

  // ------------------------------------------------------------ tabs
  function thermostatsTab() {
    const rows = data.device.targets.filter((t) => t.startsWith("climate."));
    const room = profiles().rooms.find((r) => r.area_id === d.profile.room);
    const heat = [...new Set(d.thermostats.flatMap((t) => ((caps()[t] || {}).heating_modes) || []))];
    const table = d.thermostats.length && modes().length ? `<div class="tscroll"><table class="levels"><thead><tr><th>Thermostat</th>${modes().map((m) => `<th>${esc(m.name)}</th>`).join("")}</tr></thead><tbody>
        ${d.thermostats.map((t) => `<tr data-thermostat="${esc(t)}"><th scope="row">${esc(entityName(t))}</th>${modes().map((m) => {
          const res = ((resolved().cells || {})[t] || {})[m.id] || { cell: null, source: "unset" };
          const own = ((d.cells[t] || {})[m.id]) || null;
          const presets = ((caps()[t] || {}).preset_modes) || [];
          return `<td><select data-chg="tcell" data-t="${esc(t)}" data-m="${esc(m.id)}" aria-label="${esc(entityName(t))} ${esc(m.name)}">${opt([["", "inherit"], ["set", "°"], ["none", "NO ACTION"], ...(presets.length ? [["preset", "preset"]] : [])], own ? own.kind : "")}</select>
            ${own && own.kind === "set" ? `<input type="number" step="0.5" min="4" max="35" data-chg="ttemp" data-t="${esc(t)}" data-m="${esc(m.id)}" value="${esc(own.temp ?? "")}" aria-label="°">` : ""}
            ${own && own.kind === "preset" ? `<select data-chg="tpreset" data-t="${esc(t)}" data-m="${esc(m.id)}" aria-label="Preset"><option value="">preset…</option>${opt(presets.map((p) => [p, p]), own.preset)}</select>` : ""}
            <br><span class="small">${esc(cellText(res.cell))}</span> <span class="badge src-${esc(res.source)}">${esc(SOURCE[res.source])}</span></td>`;
        }).join("")}</tr>`).join("")}</tbody></table></div>` : "";
    return `<div class="card"><h2>Thermostats (rows)</h2>
        ${rows.map((t) => `<label class="pick"><input type="checkbox" data-chg="row" data-id="${esc(t)}" ${d.thermostats.includes(t) ? "checked" : ""}>${icon("climate")}<span>${esc(entityName(t))}<br><span class="mono">${esc(t)}</span></span><span class="spacer"></span><span class="muted small">${esc(capText(caps()[t]))}</span></label>`).join("") || `<p class="muted">This device controls no thermostat.</p>`}
        <p class="muted small">Each ticked thermostat gets an automation of its own. A thermostat's own reading is not assumed to be the room's temperature.</p></div>
      <div class="card"><h2>Room heating profile</h2>
        <div class="field"><label for="hroom">Derives from</label><select id="hroom" data-chg="text" data-path="profile/room"><option value="">None (UNSET)</option>${opt(profiles().rooms.map((r) => [r.area_id, `${r.name} (${r.template_name || "no template"})`]), d.profile.room)}</select>
          <div class="help">The temperatures of the modes come from the room profile (and its template) unless a thermostat sets its own below. <a href="#/heating">Heating profiles</a></div></div>
        ${room ? "" : `<p class="muted small">No profile chosen: nothing is chosen for you. Create one on the <a href="#/heating">Heating</a> page.</p>`}
        ${table}${problemsOf(null)}</div>
      <div class="card"><h2>Thermostat behaviour</h2>
        <div class="field"><label for="hoff">A thermostat that is OFF</label><select id="hoff" data-chg="text" data-path="when_off">${opt([["leave", "Is left off (nothing is sent)"], ["heat", "Is switched to an HVAC mode with the setpoint"]], d.when_off)}</select></div>
        ${d.when_off === "heat" ? `<div class="field"><label for="hmode">HVAC mode</label><select id="hmode" data-chg="text" data-path="hvac_mode"><option value="">Choose…</option>${opt(heat.map((m) => [m, m]), d.hvac_mode)}</select><div class="help">Only the heating modes the thermostats offer are listed.</div></div>` : ""}
        <div class="field"><label>Room temperature sensor</label><span>${entityBtn("hsensor", (d.sensor || {}).entity_id, "optional")} ${(d.sensor || {}).entity_id ? `${live(d.sensor.entity_id)} <button type="button" class="link small" data-act="hsensor-clear">clear</button>` : ""}</span><div class="help">Optional: shown in the preview. It never steers the thermostat (the device's own control is not assumed to accept an external sensor).</div></div>
        <div class="field"><label>Reconciliation</label><span><select data-chg="numsel" data-path="reconcile_minutes" aria-label="Reconciliation"><option value="">Only when an input changes</option>${opt([5, 10, 15, 30].map((m) => [m, `Also every ${m} min`]), d.reconcile_minutes)}</select></span><div class="help">A periodic evaluation corrects a lost command; it sends nothing when the thermostat already is right.</div></div></div>
      <div class="card"><h2>Start from a template</h2><p class="small">Fills in illustrative times and modes (sensors, persons and presence are never chosen for you). Adjust everything afterwards.</p>
        <div class="actions">${(data.quick || []).map((q) => `<button type="button" data-act="quick" data-kind="${esc(q.id)}">${esc(q.name)}</button>`).join("")}</div>
        <p class="muted small">Custom Heating Matrix: the rooms × modes table of temperatures is on the <a href="#/heating">Heating</a> page; this thermostat's own values are the table above.</p></div>`;
  }

  function slotRows(key) {
    const list = d.modes.schedule[key];
    return list.map((s, i) => `<li class="row slot-row"><input type="time" data-chg="slot-at" data-key="${key}" data-i="${i}" value="${esc((s.at || "").slice(0, 5))}" aria-label="From">
        <select data-chg="slot-kind" data-key="${key}" data-i="${i}" aria-label="Sets">${opt([["mode", "a mode"], ["temp", "a temperature"]], s.mode || s.temp == null ? "mode" : "temp")}</select>
        ${s.mode || s.temp == null ? `<select data-chg="slot-mode" data-key="${key}" data-i="${i}" aria-label="Mode"><option value="">mode…</option>${opt(modes().map((m) => [m.id, m.name]), s.mode)}</select>`
          : `<input type="number" step="0.5" min="4" max="35" data-chg="slot-temp" data-key="${key}" data-i="${i}" value="${esc(s.temp ?? "")}" aria-label="Temperature"> °`}
        <span class="spacer"></span><button type="button" class="link small danger-text" data-act="slot-drop" data-key="${key}" data-i="${i}">remove</button></li>`).join("");
  }
  function scheduleTab() {
    const m = d.modes;
    const s = m.schedule;
    const lists = s.split === "all" ? [["all", "Every day"]] : [["weekdays", "Monday – Friday"], ["weekend", "Saturday – Sunday"]];
    const ent = m.entity;
    const options = ent.entity_id ? (((entities[ent.entity_id] || {}).attributes || {}).options || (["input_boolean", "binary_sensor"].includes(ent.entity_id.split(".")[0]) ? ["on", "off"] : [])) : [];
    const known = [...new Set([...options, ...Object.keys(ent.map)])];
    return `<div class="card"><h2>What names the mode</h2>
        <div class="field"><label for="msrc">Source</label><select id="msrc" data-chg="text" data-path="modes/source">${opt([["", "Nothing (only presence, windows or away decide)"], ["schedule", "A schedule (times of day)"], ["entity", "A helper (its state names the mode)"]], m.source)}</select></div>${problemsOf("schedule")}</div>
      ${m.source === "schedule" ? `<div class="card"><h2>Schedule</h2>
        <div class="field"><label for="msplit">Days</label><select id="msplit" data-chg="text" data-path="modes/schedule/split">${opt([["all", "The same every day"], ["weekdays", "Weekdays and weekend differ"]], s.split)}</select></div>
        ${lists.map(([key, title]) => `<h3>${esc(title)}</h3><ul class="plain">${slotRows(key) || `<li class="muted">No entry.</li>`}</ul><div class="actions"><button type="button" data-act="slot-add" data-key="${key}">Add an entry</button></div>`).join("")}
        <p class="muted small">From each time until the next entry. Before the first entry of a day, the last entry of the day before applies.</p>
        <h3>Day / night</h3><div class="row"><label class="small">Day from <input type="time" id="dn-day" value="06:30"></label> ${modeSel("__dn_day", "", [])} <label class="small">night from <input type="time" id="dn-night" value="22:30"></label> <select id="dn-nmode" aria-label="Night mode"><option value="">night mode…</option>${opt(modes().map((x) => [x.id, x.name]), "")}</select>
          <button type="button" data-act="daynight">Set as the schedule</button></div><p class="muted small">Replaces the schedule by two entries every day.</p></div>` : ""}
      ${m.source === "entity" ? `<div class="card"><h2>Mode helper</h2>
        <div class="field"><label>Helper</label><span>${entityBtn("mentity", ent.entity_id, "choose a helper")} ${live(ent.entity_id)}</span><div class="help">For example a dropdown (input_select) with Day/Night/Away, or a day/night input_boolean.</div></div>
        ${ent.entity_id ? `<div class="tscroll"><table class="levels"><thead><tr><th>State</th><th>Mode</th></tr></thead><tbody>${known.map((o) => `<tr><th scope="row">${esc(o)}</th><td><select data-chg="emap" data-o="${esc(o)}" aria-label="${esc(o)}"><option value="">UNSET (decides nothing)</option>${opt(modes().map((x) => [x.id, x.name]), ent.map[o])}</select></td></tr>`).join("")}</tbody></table></div>` : ""}</div>` : ""}`;
  }

  function presenceTab() {
    const p = d.presence;
    const sources = (data.presence || []).filter((s) => ["room", "zone"].includes(s.kind) && s.entity_id);
    return `<div class="card"><h2>Presence-based heating</h2><div class="field"><label>Presence</label>${toggle("presence/enabled", p.enabled, "Use a room's presence", "Optional. Any presence source works (Smart Occupancy Detection is not needed).")}</div>
      ${p.enabled ? `<div class="field"><label for="psrc">Source</label><select id="psrc" data-chg="text" data-path="presence/source"><option value="">Choose…</option>${opt(sources.map((s) => [s.id, `${s.name}${s.area ? ` (${s.area})` : ""}`]), p.source)}</select><div class="help">Room and zone sources of the <a href="#/presence">Presence</a> page.</div></div>
        <div class="field"><label>Vacant for</label><span>${num("presence/vacant_minutes", p.vacant_minutes, 'min="0" max="1440"', "min")} → ${modeSel("presence/mode", p.mode)}</span></div>
        <div class="field"><label>While occupied</label><span><select data-chg="text" data-path="presence/occupied_mode" aria-label="Occupied mode"><option value="">The schedule decides</option>${opt(modes().map((m) => [m.id, m.name]), p.occupied_mode)}</select></span></div>` : ""}${problemsOf("presence")}</div>`;
  }

  function windowTab() {
    const w = d.window;
    return `<div class="card"><h2>Window-aware heating</h2><div class="field"><label>Windows</label>${toggle("window/enabled", w.enabled, "React to window and door sensors")}</div>
      ${w.enabled ? `<div class="field"><label>Sensors</label><span><ul class="plain">${w.sensors.map((s) => `<li class="row">${esc(entityName(s))} ${live(s)}<span class="spacer"></span><button type="button" class="link small danger-text" data-act="wdrop" data-e="${esc(s)}">remove</button></li>`).join("") || `<li class="muted">None chosen.</li>`}</ul><button type="button" data-act="wadd">Add a sensor…</button></span></div>
        <div class="field"><label>Open for</label><span>${num("window/open_seconds", w.open_seconds, 'min="0" max="3600"', "s")} → <select data-chg="text" data-path="window/action" aria-label="While open"><option value="">Choose…</option>${opt([["mode", "a mode"], ["frost", "frost protection (the template's frost temperature)"], ["off", "OFF (HVAC mode off)"], ["none", "No action"]], w.action)}</select> ${w.action === "mode" ? modeSel("window/mode", w.mode) : ""}</span>
          <div class="help">Heating is never switched off by itself: choose. OFF needs “switch to an HVAC mode” under Thermostats, so the heating comes back.</div></div>
        <div class="field"><label>Closed for</label><span>${num("window/close_seconds", w.close_seconds, 'min="0" max="3600"', "s")} → back to the profile and schedule as they are then</span></div>
        <div class="field"><label>A sensor is unavailable</label><select data-chg="text" data-path="window/unavailable" aria-label="Unavailable">${opt([["", "Choose…"], ["ignore", "Ignore it (as if closed)"], ["open", "Treat it as open"], ["keep", "Leave the thermostat as it is"]], w.unavailable)}</select></div>` : ""}${problemsOf("window")}</div>`;
  }

  function awayTab() {
    const a = d.away;
    return `<div class="card"><h2>Away / Home</h2><div class="field"><label>Away</label>${toggle("away/enabled", a.enabled, "Use who is home", "Nothing is chosen for you: pick the person, tracker, group or helper.")}</div>
      ${a.enabled ? `<div class="field"><label>Who is home</label><span>${entityBtn("away-entity", a.entity_id, "choose")} ${live(a.entity_id)}</span></div>
        <div class="field"><label for="ahome">Its “home” state</label><input id="ahome" type="text" data-chg="text" data-path="away/home_state" value="${esc(a.home_state || "")}" placeholder="home (or on)" maxlength="60"></div>
        <div class="field"><label>Away for</label><span>${num("away/away_minutes", a.away_minutes, 'min="0" max="1440"', "min")} → ${modeSel("away/mode", a.mode)}</span></div>
        <div class="field"><label>Back home</label><span><select data-chg="text" data-path="away/return_mode" aria-label="Return mode"><option value="">The schedule decides</option>${opt(modes().map((m) => [m.id, m.name]), a.return_mode)}</select>${a.return_mode ? ` for ${num("away/return_minutes", a.return_minutes, 'min="1" max="600"', "min")}` : ""}</span>
          <div class="help">A return mode lasts for the given time after coming home (also after a Home Assistant restart, when the state is new).</div></div>` : ""}${problemsOf("away")}</div>`;
  }

  function overrideTab() {
    const o = d.override;
    return `<div class="card"><h2>Manual thermostat override</h2><div class="field"><label>Manual changes</label>${toggle("override/manual", o.manual, "A setpoint changed by hand suspends the automatic setpoint", "By hand, on the device or in the app: Home Assistant's context tells (a change by an automation has a parent). A thermostat that reports back the setpoint it was sent is not a manual change.")}</div>
      ${o.manual ? `<div class="field"><label for="opol">It lasts</label><select id="opol" data-chg="text" data-path="override/policy"><option value="">Choose…</option>${opt([["until_next", "Until the next scheduled change"], ["duration", "A time"], ["until_resumed", "Until resumed"], ["until_event", "Until an event"]], o.policy)}</select></div>
        ${o.policy === "duration" ? `<div class="field"><label>Duration</label><span>${num("override/minutes", o.minutes, 'min="1" max="1440"', "min")}</span></div>` : ""}
        ${o.policy === "until_event" ? `<div class="field"><label>Ends</label><span>${o.release.map((t, i) => `<span class="badge">${esc(t.type === "time" ? `at ${t.at.slice(0, 5)}` : t.type === "state" ? `${t.entity_id} changes` : t.type)}</span> <button type="button" class="link small" data-act="reldrop" data-i="${i}">remove</button>`).join(" ")}
          <input type="time" id="rel-at" aria-label="At"><button type="button" data-act="reladd">Add a time</button></span></div>` : ""}
        <div class="field"><label>Resume automatic</label><span>${o.resume.map((t, i) => `<span class="badge">${esc(t.type === "state" ? `${entityName(t.entity_id)} changes` : t.type)}</span> <button type="button" class="link small" data-act="resdrop" data-i="${i}">remove</button>`).join(" ")}
          <button type="button" data-act="resadd">Add a button or helper…</button></span><div class="help">Also: turning the override helper off in Home Assistant resumes at once. The setpoint is then what the profile and schedule say now.</div></div>
        ${(data.helpers || []).length ? `<h3>Helpers</h3><ul class="plain small">${data.helpers.map((h) => `<li><span class="mono">${esc(h.entity_id)}</span> · ${esc(h.purpose)} · ${h.exists ? `exists${h.state ? ` (${esc(h.state)})` : ""}${h.owned ? "" : ", <strong>not created by Automation Builder</strong>"}` : "created when you deploy"}</li>`).join("")}</ul>` : ""}` : ""}${problemsOf("override")}</div>`;
  }

  function priorityTab() {
    return `<div class="card"><h2>Priority</h2><p class="small">The first layer that decides wins; the others wait. When a temporary layer ends (a window closes, the override ends), the setpoint is what the layers below say now - never an earlier target.</p>
      <ol class="plain">${d.priority.map((l, i) => `<li class="row" data-layer="${l}"><span class="badge">${i + 1}</span><span><strong>${esc(LAYER[l])}</strong><br><span class="small muted">${esc(LAYER_HELP[l])}</span></span><span class="spacer"></span>
        <button type="button" class="link" data-act="pmove" data-i="${i}" data-dir="-1" ${i ? "" : "disabled"} aria-label="Higher">↑</button><button type="button" class="link" data-act="pmove" data-i="${i}" data-dir="1" ${i < d.priority.length - 1 ? "" : "disabled"} aria-label="Lower">↓</button></li>`).join("")}</ol></div>
      <div class="card"><h2>Ordinary rules of this device</h2><p class="muted small">Rules of the rule editor run next to the designer; the conflict check reports when both can set the same thermostat at the same moment.</p><div class="actions"><a class="button" href="#/devices/${enc(deviceId)}">Device page</a></div></div>`;
  }

  function resultText(r) {
    if (!r.result) return "Nothing decides now: nothing is sent";
    const [v, by] = r.result;
    return `${v === "keep" ? "NO ACTION (left as it is)" : v === "off" ? "OFF" : String(v).startsWith("preset:") ? `preset ${String(v).slice(7)}` : deg(v)} · by ${by}`;
  }
  function cmdText(c) {
    if (!c || !c.service) return "nothing to send (already right, off, unavailable or nothing decided)";
    return `climate.${c.service} ${JSON.stringify(c.data)}`;
  }
  function previewBlock(res, title) {
    if (!res) return "";
    return `<div class="card"><h2>${esc(title)}</h2>${(res.thermostats || []).map((t) => `<div class="heat-thermostat" data-thermostat="${esc(t.entity_id)}"><h3>${icon("climate")}${esc(t.name)}</h3>
        <dl class="kv"><dt>Now</dt><dd>${esc(t.state ?? "?")}${t.hvac_action ? ` · ${esc(t.hvac_action)}` : ""} · target ${esc(deg(t.target ?? t.target_low))} · measured ${esc(deg(t.measured))}${t.room_temperature != null ? ` · room sensor ${esc(deg(t.room_temperature))}` : ""}</dd>
          <dt>Decides</dt><dd class="heat-now">${esc(resultText(t))}</dd><dt>Command</dt><dd class="mono small">${esc(cmdText(t.command))}</dd>
          ${t.override != null ? `<dt>Override</dt><dd>${t.override ? "active" : "not active"}</dd>` : ""}</dl>
        <ul class="plain small">${t.layers.map((l) => `<li>${l.holds ? "✔" : l.holds === false ? "·" : "?"} ${esc(LAYER[l.layer])}: ${esc(l.by)}</li>`).join("") || `<li class="muted">No layer applies.</li>`}</ul>
        ${t.unknown.length ? notes(t.unknown.map((u) => ({ level: "warning", message: u }))) : ""}</div>`).join("")}
      ${res.next ? `<p class="small">Next scheduled change: ${esc(res.next.weekday)} ${esc(res.next.at.slice(0, 5))} → ${esc(res.next.mode_name || deg(res.next.temp))}</p>` : ""}
      <p class="muted small">A rule preview, not a thermal prediction: it shows which setpoint the rules choose, not when the room gets warm.</p></div>`;
  }
  function previewTab() {
    const inputs = (sim.inputs || { inputs: [] }).inputs;
    const rows = inputs.map((e) => `<li class="row"><span>${esc(e.name)} <span class="mono small">${esc(e.entity_id)}</span></span><span class="spacer"></span>
      ${e.suggested && e.suggested.length ? `<select data-chg="simstate" data-id="${esc(e.entity_id)}" aria-label="${esc(e.name)}">${opt(e.suggested.map((s) => [s, s]), ((sim.scenario.states[e.entity_id] || {}).state) ?? e.state)}</select>`
        : `<input type="text" data-chg="simstate" data-id="${esc(e.entity_id)}" value="${esc(((sim.scenario.states[e.entity_id] || {}).state) ?? e.state ?? "")}" aria-label="${esc(e.name)}">`}</li>`).join("");
    return `${previewBlock(data.live && data.live.available ? data.live : null, "Now (Home Assistant's states)")}
      <div class="card"><h2>Simulate</h2><p class="small">Change states and the time; nothing is sent to a thermostat.</p>
        <div class="row"><label class="small">Time <input type="time" data-chg="simtime" value="${esc((sim.scenario.time || "").slice(0, 5))}"></label>
          <label class="small">Day <select data-chg="simday" aria-label="Weekday"><option value="">today</option>${opt(DAYS.map((x) => [x, x]), sim.scenario.weekday)}</select></label>
          <button type="button" class="primary" data-act="simrun">Simulate</button><button type="button" class="link" data-act="simlive">Back to the live states</button></div>
        <ul class="plain">${rows || `<li class="muted">Loading the inputs…</li>`}</ul></div>
      ${sim.result ? previewBlock((sim.result.heating || {})[designerId], "Simulation") : ""}`;
  }

  function draw() {
    if (!current()) return;
    const body = { thermostats: thermostatsTab, schedule: scheduleTab, presence: presenceTab, window: windowTab, away: awayTab, override: overrideTab,
      priority: priorityTab, preview: previewTab, yaml: () => `<div class="card"><pre class="yaml">${esc(data.yaml || "# Nothing to generate yet.")}</pre></div>` }[tab]();
    const keep = view.querySelector("#d-status");
    const kept = keep ? [keep.textContent, keep.className] : null;
    const blocking = (data.designer.problems || []).filter((p) => p.level === "blocking").length;
    view.innerHTML = `<p><a href="#/devices/${enc(deviceId)}">← ${esc(data.device.name)}</a></p>
      <h1>${icon("climate")}<input type="text" class="title-input" data-chg="text" data-path="name" value="${esc(d.name)}" maxlength="80" aria-label="Designer name">
        <label class="switch"><input type="checkbox" data-chg="flag" data-path="enabled" ${d.enabled ? "checked" : ""}> On</label><span class="spacer"></span>
        ${blocking ? `<span class="status bad">${plural(blocking, "thing")} to resolve</span>` : ""}<a class="button primary" href="#/devices/${enc(deviceId)}">Review &amp; deploy</a></h1>
      <p class="save-status" id="d-status" role="status" aria-live="polite"></p>
      <nav class="tabs" aria-label="Heating Designer">${tabs.map(([k, l]) => `<a href="#/design/${enc(deviceId)}/${enc(designerId)}/${k}" class="${k === tab ? "active" : ""}" ${k === tab ? 'aria-current="page"' : ""}>${esc(l)}</a>`).join("")}</nav>
      ${body}`;
    if (kept) { const el = view.querySelector("#d-status"); if (el) { el.textContent = kept[0]; el.className = kept[1]; } }
  }

  const scenario = () => {
    const states = {};
    ((sim.inputs || {}).inputs || []).forEach((e) => { states[e.entity_id] = { state: e.state, attributes: { ...e.attributes }, origin: "live" }; });
    Object.entries(sim.scenario.states).forEach(([id, st]) => { states[id] = { ...(states[id] || {}), ...st, origin: "test" }; });
    return { states, time: sim.scenario.time, weekday: sim.scenario.weekday };
  };
  const pick = async (purpose, apply) => { const found = await pickEntity(purpose); if (!found || !current()) return; await loadEntities([found.entity_id]); apply(found.entity_id); save(); };
  const act = {
    quick: async (el) => {
      try { data = await change("api/heating/quick", { device_id: deviceId, designer_id: designerId, kind: el.dataset.kind }); d = clone(data.designer.config); draw(); status("Template applied · adjust it, then deploy", "ok"); }
      catch (err) { toast(errorText(err)); }
    },
    hsensor: () => pick("numeric", (e) => { d.sensor = { entity_id: e }; }),
    "hsensor-clear": () => { d.sensor = null; save(); },
    mentity: () => pick("mode", (e) => { d.modes.entity.entity_id = e; d.modes.entity.map = {}; }),
    "away-entity": () => pick("home", (e) => { d.away.entity_id = e; if (!d.away.home_state) d.away.home_state = ["person", "device_tracker", "group", "zone"].includes(e.split(".")[0]) ? (e.startsWith("zone.") ? null : "home") : "on"; }),
    wadd: () => pick("window", (e) => { if (!d.window.sensors.includes(e)) d.window.sensors.push(e); }),
    wdrop: (el) => { d.window.sensors = d.window.sensors.filter((s) => s !== el.dataset.e); save(); },
    "slot-add": (el) => { d.modes.schedule[el.dataset.key].push({ at: null, mode: null, temp: null }); draw(); status("Give the new entry its time and mode to save it."); },
    "slot-drop": (el) => { d.modes.schedule[el.dataset.key].splice(Number(el.dataset.i), 1); save(); draw(); },
    daynight: () => {
      const day = (view.querySelector("#dn-day") || {}).value; const night = (view.querySelector("#dn-night") || {}).value;
      const dm = (view.querySelector('[data-path="__dn_day"]') || {}).value; const nm = (view.querySelector("#dn-nmode") || {}).value;
      if (!day || !night || !dm || !nm) { toast("Give both times and both modes."); return; }
      d.modes.schedule = { split: "all", weekdays: [], weekend: [], all: [{ at: `${day}:00`, mode: dm, temp: null }, { at: `${night}:00`, mode: nm, temp: null }] };
      save(); draw();
    },
    reladd: () => { const at = (view.querySelector("#rel-at") || {}).value; if (!at) return; d.override.release.push({ type: "time", at: `${at}:00`, weekdays: [] }); save(); draw(); },
    reldrop: (el) => { d.override.release.splice(Number(el.dataset.i), 1); save(); draw(); },
    resadd: () => pick("any", (e) => { d.override.resume.push({ type: "state", entity_id: e, to: null, from: null, for_seconds: 0, attribute: null }); }),
    resdrop: (el) => { d.override.resume.splice(Number(el.dataset.i), 1); save(); draw(); },
    pmove: (el) => { const i = Number(el.dataset.i); const j = i + Number(el.dataset.dir); [d.priority[i], d.priority[j]] = [d.priority[j], d.priority[i]]; save(); draw(); },
    simrun: async () => { try { sim.result = await api("api/designers/simulate", { device_id: deviceId, scenario: scenario() }); if (current()) draw(); } catch (err) { toast(errorText(err)); } },
    simlive: () => { sim.scenario.states = {}; sim.result = null; draw(); },
  };
  const numOf = (v) => (v === "" ? null : Number(v));
  const cellOf = (t, m) => { d.cells[t] = d.cells[t] || {}; return d.cells[t]; };
  const chg = {
    flag: (el) => { set(el.dataset.path, el.checked); save(); draw(); },
    num: (el) => { set(el.dataset.path, numOf(el.value)); save(); },
    numsel: (el) => { set(el.dataset.path, el.value === "" ? null : Number(el.value)); save(); draw(); },
    text: (el) => { if (el.dataset.path.startsWith("__")) return; set(el.dataset.path, el.value === "" ? null : el.value); if (el.dataset.path === "name" && !el.value.trim()) return; save(); draw(); },
    row: (el) => {
      if (el.checked) d.thermostats.push(el.dataset.id);
      else { d.thermostats = d.thermostats.filter((t) => t !== el.dataset.id); delete d.cells[el.dataset.id]; }
      save(); draw();
    },
    tcell: (el) => {
      const row = cellOf(el.dataset.t);
      if (!el.value) delete row[el.dataset.m];
      else row[el.dataset.m] = el.value === "set" ? { kind: "set", temp: (row[el.dataset.m] || {}).temp ?? 20 } : el.value === "preset" ? { kind: "preset", preset: null } : { kind: "none" };
      if (!Object.keys(row).length) delete d.cells[el.dataset.t];
      if (el.value !== "preset") save();
      draw();
    },
    ttemp: (el) => { const c = cellOf(el.dataset.t)[el.dataset.m]; if (c) { c.temp = numOf(el.value); if (c.temp != null) save(); } },
    tpreset: (el) => { const c = cellOf(el.dataset.t)[el.dataset.m]; if (c) { c.preset = el.value || null; if (c.preset) save(); } },
    "slot-at": (el) => { d.modes.schedule[el.dataset.key][Number(el.dataset.i)].at = el.value ? `${el.value}:00` : null; save(); },
    "slot-kind": (el) => { const s = d.modes.schedule[el.dataset.key][Number(el.dataset.i)]; if (el.value === "temp") { s.mode = null; s.temp = s.temp ?? 20; } else { s.temp = null; } save(); draw(); },
    "slot-mode": (el) => { const s = d.modes.schedule[el.dataset.key][Number(el.dataset.i)]; s.mode = el.value || null; s.temp = null; save(); },
    "slot-temp": (el) => { const s = d.modes.schedule[el.dataset.key][Number(el.dataset.i)]; s.temp = numOf(el.value); s.mode = null; save(); },
    emap: (el) => { if (el.value) d.modes.entity.map[el.dataset.o] = el.value; else delete d.modes.entity.map[el.dataset.o]; save(); },
    simtime: (el) => { sim.scenario.time = el.value ? `${el.value}:00` : null; },
    simday: (el) => { sim.scenario.weekday = el.value || null; },
    simstate: (el) => { sim.scenario.states[el.dataset.id] = { ...(sim.scenario.states[el.dataset.id] || {}), state: el.value }; },
  };

  draw();
  if (tab === "preview") api(`api/simulation/${enc(deviceId)}`).then((inputs) => { if (current()) { sim.inputs = inputs; draw(); } }).catch((err) => toast(errorText(err)));
  return { act, chg, inp: {} };
}

// ================================================================== heating profiles (top-level "Heating" page)
export async function heatingProfilesPage(tab, item, current) {
  let data = await api("api/heating");
  if (!current()) return null;
  let work = item ? clone(data.templates.find((t) => t.id === item) || null) : null;
  let chain = Promise.resolve();
  const status = (text, cls = "") => { const el = view.querySelector("#h-status"); if (el) { el.className = `save-status ${cls}`; el.textContent = text; } };
  async function reload() { data = await api("api/heating"); if (current()) draw(); }
  const run = (fn) => { status("Saving…"); chain = chain.then(async () => { try { await fn(); data = await api("api/heating"); if (!current()) return; if (work) work = clone(data.templates.find((t) => t.id === work.id) || work); draw(); status("Saved · deploy the thermostats that use it", "ok"); } catch (err) { status(`Not saved: ${errorText(err)}`, "bad"); } }); };
  const saveTemplate = () => { const { id, name, modes, frost } = clone(work); run(() => change("api/heating/template/save", { template: { id, name, modes, frost } })); };

  function matrixHtml() {
    const templates = data.templates;
    if (!data.rooms.length) return `<p class="muted">No room profile yet.</p>`;
    return data.rooms.map((r) => {
      const t = templates.find((x) => x.id === r.template);
      const modes = (t || {}).modes || [];
      return `<div class="card heat-room" data-room="${esc(r.area_id)}"><div class="row"><h2>${icon("room")}${esc(r.name)}</h2><span class="spacer"></span>
          <select data-chg="rtpl" data-area="${esc(r.area_id)}" aria-label="Template of ${esc(r.name)}">${opt(templates.map((x) => [x.id, x.name]), r.template)}</select>
          <button type="button" class="link danger-text" data-act="rdel" data-area="${esc(r.area_id)}">Remove</button></div>
        <div class="tscroll"><table class="levels"><thead><tr><th></th>${modes.map((m) => `<th>${esc(m.name)}</th>`).join("")}</tr></thead><tbody><tr><th scope="row">Room</th>${modes.map((m) => {
          const own = r.overrides[m.id];
          const res = r.cells[m.id] || { cell: null, source: "unset" };
          return `<td><select data-chg="rcell" data-area="${esc(r.area_id)}" data-m="${esc(m.id)}" aria-label="${esc(r.name)} ${esc(m.name)}">${opt([["", "inherit"], ["set", "°"], ["none", "NO ACTION"]], own ? own.kind : "")}</select>${own && own.kind === "set" ? ` <input type="number" step="0.5" min="4" max="35" data-chg="rtemp" data-area="${esc(r.area_id)}" data-m="${esc(m.id)}" value="${esc(own.temp ?? "")}" aria-label="°">` : ""}
            <br><span class="small">${esc(cellText(res.cell))}</span> <span class="badge src-${esc(res.source)}">${esc(SOURCE[res.source])}</span></td>`;
        }).join("")}</tr></tbody></table></div>
        <p class="muted small">${r.used_by.length ? `Used by ${r.used_by.map(esc).join("; ")}` : "No thermostat uses it yet (open a thermostat's Heating Designer and choose this room)."}</p></div>`;
    }).join("");
  }
  function templateEditor() {
    const t = work;
    return `<p><a href="#/heating">← Heating</a></p><h1>${esc(t.name)} <span class="badge">v${t.version}</span></h1><p class="save-status" id="h-status" role="status" aria-live="polite"></p>
      <div class="card"><div class="field"><label for="htn">Name</label><input id="htn" type="text" data-chg="tname" value="${esc(t.name)}" maxlength="60"></div>
        <div class="tscroll"><table class="levels"><thead><tr><th>Mode</th><th>Temperature</th><th></th></tr></thead><tbody>${t.modes.map((m, i) => `<tr><td><input type="text" data-chg="mname" data-i="${i}" value="${esc(m.name)}" maxlength="40" aria-label="Mode name"><br><span class="mono small">${esc(m.id)}</span></td>
          <td><input type="number" step="0.5" min="4" max="35" data-chg="mtemp" data-i="${i}" value="${esc(m.temp ?? "")}" placeholder="unset" aria-label="${esc(m.name)} °"> °</td>
          <td><button type="button" class="link danger-text" data-act="mdrop" data-i="${i}" ${t.modes.length > 1 ? "" : "disabled"}>remove</button></td></tr>`).join("")}</tbody></table></div>
        <div class="row"><input type="text" id="hm-new" placeholder="New mode name" maxlength="40" aria-label="New mode name"><button type="button" data-act="madd" ${t.modes.length < 8 ? "" : "disabled"}>Add a mode</button></div>
        <div class="field"><label for="hfrost">Frost protection</label><span><input id="hfrost" type="number" step="0.5" min="4" max="35" data-chg="frost" value="${esc(t.frost ?? "")}" placeholder="unset"> °</span><div class="help">Used by “frost protection” while a window is open.</div></div></div>
      <div class="card"><h2>Room profiles that use it</h2><p>${(data.templates.find((x) => x.id === t.id) || {}).used_by.map(esc).join(", ") || "None."}</p><p class="muted small">A room's own values stay when the template changes.</p></div>`;
  }
  function draw() {
    if (!current()) return;
    if (work) { view.innerHTML = templateEditor(); return; }
    const free = data.areas.filter((a) => !a.configured);
    view.innerHTML = `<h1>Heating</h1><p class="save-status" id="h-status" role="status" aria-live="polite"></p>
      <div class="card"><p class="small">Heating templates name modes with temperatures (for example Comfort, Setback, Night, Away). A room profile derives from a template and may set its own values; a thermostat may override single values in its Heating Designer. Global → room → thermostat; unset stays unset. Nothing is assigned to a thermostat here.</p></div>
      <div class="card"><h2>Heating templates</h2><ul class="plain">${data.templates.map((t) => `<li class="row" data-template="${esc(t.id)}"><span><strong>${esc(t.name)}</strong> <span class="badge">v${t.version}</span>${t.default ? ` <span class="badge">standard</span>` : ""}<br><span class="small muted">${t.modes.map((m) => `${esc(m.name)} ${esc(deg(m.temp))}`).join(" · ")}${t.frost != null ? ` · frost ${esc(deg(t.frost))}` : ""}</span></span><span class="spacer"></span>
          <a class="button" href="#/heating/templates/${enc(t.id)}">Edit</a><button type="button" class="link danger-text" data-act="tdel" data-id="${esc(t.id)}">Delete</button></li>`).join("") || `<li class="muted">None yet.</li>`}</ul>
        <div class="actions"><button type="button" data-act="tpreset">Add “Standard Heating” (example values)</button></div></div>
      <h2>Room profiles (Custom Heating Matrix)</h2>
      <div class="card"><div class="row"><select id="h-new-room" aria-label="Room"><option value="">Add a room profile…</option>${opt(free.map((a) => [a.area_id, a.name]), "")}</select>
        <select id="h-new-tpl" aria-label="Template"><option value="">with the template…</option>${opt(data.templates.map((t) => [t.id, t.name]), data.default_template || "")}</select><button type="button" class="primary" data-act="radd" ${free.length && data.templates.length ? "" : "disabled"}>Add</button></div></div>
      ${matrixHtml()}`;
  }
  const act = {
    tpreset: () => run(() => change("api/heating/template/save", { template: "preset" })),
    tdel: async (el) => { const t = data.templates.find((x) => x.id === el.dataset.id); if (!(await ask({ title: `Delete “${t.name}”?`, ok: "Delete", danger: true }))) return; run(() => change("api/heating/template/delete", { template_id: t.id })); },
    radd: () => {
      const area = (view.querySelector("#h-new-room") || {}).value; const tpl = (view.querySelector("#h-new-tpl") || {}).value;
      if (!area || !tpl) { toast("Choose the room and the template."); return; }
      run(() => change("api/heating/room/save", { room: { area_id: area, template: tpl, overrides: {} } }));
    },
    rdel: async (el) => { if (!(await ask({ title: "Remove this room profile?", ok: "Remove", danger: true }))) return; run(() => change("api/heating/room/delete", { area_id: el.dataset.area })); },
    madd: () => {
      const name = ((view.querySelector("#hm-new") || {}).value || "").trim();
      if (!name) { toast("Give the mode a name."); return; }
      let ident = name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").replace(/^([^a-z])/, "m_$1").slice(0, 24) || "mode";
      while (work.modes.some((m) => m.id === ident) || ["off", "keep", "frost"].includes(ident)) ident = `${ident.slice(0, 21)}_${Math.floor(Math.random() * 90 + 10)}`;
      work.modes.push({ id: ident, name, temp: null });
      saveTemplate();
    },
    mdrop: (el) => { work.modes.splice(Number(el.dataset.i), 1); saveTemplate(); },
  };
  const roomOf = (area) => clone(data.rooms.find((r) => r.area_id === area));
  const chg = {
    rtpl: (el) => { const r = roomOf(el.dataset.area); run(() => change("api/heating/room/save", { room: { area_id: r.area_id, template: el.value, overrides: r.overrides } })); },
    rcell: (el) => {
      const r = roomOf(el.dataset.area);
      if (!el.value) delete r.overrides[el.dataset.m];
      else r.overrides[el.dataset.m] = el.value === "set" ? { kind: "set", temp: (r.overrides[el.dataset.m] || {}).temp ?? 20 } : { kind: "none" };
      run(() => change("api/heating/room/save", { room: { area_id: r.area_id, template: r.template, overrides: r.overrides } }));
    },
    rtemp: (el) => {
      const r = roomOf(el.dataset.area);
      if (el.value === "") return;
      r.overrides[el.dataset.m] = { kind: "set", temp: Number(el.value) };
      run(() => change("api/heating/room/save", { room: { area_id: r.area_id, template: r.template, overrides: r.overrides } }));
    },
    tname: (el) => { work.name = el.value; if (el.value.trim()) saveTemplate(); },
    mname: (el) => { work.modes[Number(el.dataset.i)].name = el.value; if (el.value.trim()) saveTemplate(); },
    mtemp: (el) => { work.modes[Number(el.dataset.i)].temp = el.value === "" ? null : Number(el.value); saveTemplate(); },
    frost: (el) => { work.frost = el.value === "" ? null : Number(el.value); saveTemplate(); },
  };
  draw();
  return { act, chg, inp: {} };
}
