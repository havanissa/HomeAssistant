// Smart Adaptive & Predictive Climate - the interface. No build step, no dependencies.
// Observation and recommendation only: nothing on these pages can command a device.

const view = document.getElementById("view");
const banners = document.getElementById("banners");
const dialog = document.getElementById("dialog");

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const enc = encodeURIComponent;
const session = { revision: null };
let navToken = 0;
let page = { act: {}, chg: {}, inp: {} };
let pollTimer = null;

async function api(path, body) {
  const options = { credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json", "X-SPC": "1" };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data = null;
  try { data = await response.json(); } catch { data = null; }
  if (!response.ok) {
    const err = new Error((data && data.message) || `HTTP ${response.status}`);
    err.data = data || {};
    err.status = response.status;
    throw err;
  }
  if (data && typeof data.revision === "string") session.revision = data.revision;
  return data;
}
const change = (path, body) => api(path, { ...body, expected_revision: session.revision });
function errorText(err) {
  if (err.status === 409 && err.data && err.data.error === "stale") return "Changed in another window meanwhile: reload the page.";
  const problems = (err.data && err.data.problems) || [];
  return problems.length ? problems.map((p) => p.message).join("; ") : err.message;
}
function toast(text) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 4000);
}
const pad = (n) => String(n).padStart(2, "0");
function clock(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (d.toDateString() === new Date().toDateString()) return `today ${time}`;
  const tomorrow = new Date(Date.now() + 86400000);
  if (d.toDateString() === tomorrow.toDateString()) return `tomorrow ${time}`;
  return `${d.getDate()}.${d.getMonth() + 1}. ${time}`;
}
const deg = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v) ? "–" : `${Number(v).toFixed(d)} °C`);
const rate = (v) => (v === null || v === undefined ? "–" : `${v > 0 ? "+" : ""}${Number(v).toFixed(1)} °C/h`);
const STATUS_CLASS = { high: "ok", moderate: "ok", low: "pending", preliminary: "pending", collecting: "off", waiting: "off", training: "pending", insufficient: "off", unidentifiable: "bad", not_configured: "bad", disabled: "off" };
const chip = (status, text) => `<span class="status ${STATUS_CLASS[status] || "off"}" data-status="${esc(status)}">${esc(text)}</span>`;
const KIND = { measured: "measured", forecast: "forecast", forecast_: "forecast", measured_held: "measured, held", assumed: "assumed", none: "none", not_needed: "not needed", hypothetical: "hypothetical", model: "model", assumed_off: "assumed off (not reported)", learned: "learned" };
const kindBadge = (k) => `<span class="badge kind-${esc(String(k).split("_")[0])}">${esc(KIND[k] || String(k).replace(/_/g, " "))}</span>`;

function openDialog(html, wide = false) {
  dialog.className = wide ? "wide" : "";
  dialog.innerHTML = html;
  if (!dialog.open) dialog.showModal();
}
function ask({ title, body = "", ok = "OK", danger = false }) {
  return new Promise((resolve) => {
    openDialog(`<h2>${esc(title)}</h2><div class="dialog-body">${body}</div>
      <div class="actions"><button data-d="cancel">Cancel</button><span class="spacer"></span><button class="${danger ? "danger" : "primary"}" data-d="ok">${esc(ok)}</button></div>`);
    const done = (value) => { dialog.onclick = null; dialog.oncancel = null; dialog.close(); resolve(value); };
    dialog.onclick = (e) => { const d = e.target.closest("[data-d]"); if (d) done(d.dataset.d === "ok"); };
    dialog.oncancel = (e) => { e.preventDefault(); done(false); };
  });
}
// The entity picker: search by name, area, device or id within one kind of input. Resolves an entity or null.
function pickEntity(category, title, area = null) {
  return new Promise((resolve) => {
    openDialog(`<h2>${esc(title)}</h2>
      ${area ? `<label class="pick-room"><input type="checkbox" id="pick-area" checked> Only this room</label>` : ""}
      <input type="search" id="pick-q" placeholder="Search by name, area, device or entity id" autocomplete="off">
      <div class="results" id="pick-results"><div class="muted">Searching…</div></div>
      <div class="actions"><button data-d="cancel">Cancel</button></div>`, true);
    const input = dialog.querySelector("#pick-q");
    const box = dialog.querySelector("#pick-results");
    const only = dialog.querySelector("#pick-area");
    let found = [];
    let seq = 0;
    const done = (value) => { dialog.onclick = null; dialog.oncancel = null; input.oninput = null; dialog.close(); resolve(value); };
    const search = async () => {
      const mine = ++seq;
      try {
        const data = await api(`api/entities?category=${enc(category || "")}&q=${enc(input.value)}${only && only.checked ? `&area=${enc(area)}` : ""}`);
        if (mine !== seq) return;
        found = data.results;
        box.innerHTML = found.length ? found.map((e, i) => `<button class="result" data-i="${i}"><span><strong>${esc(e.name)}</strong> <span class="muted small">${esc([e.area, e.device_name].filter(Boolean).join(" · "))}</span><br><span class="mono">${esc(e.entity_id)}</span></span><span class="spacer"></span><span class="badge">${esc(e.state ?? "?")}${e.unit ? " " + esc(e.unit) : ""}</span></button>`).join("")
          : `<div class="muted">Nothing found${only && only.checked ? " in this room. Untick “Only this room” to search everywhere." : "."}</div>`;
      } catch (err) { if (mine === seq) box.innerHTML = `<div class="notice error">${esc(errorText(err))}</div>`; }
    };
    let timer = null;
    input.oninput = () => { clearTimeout(timer); timer = setTimeout(search, 200); };
    if (only) only.onchange = search;
    dialog.onclick = (e) => {
      const r = e.target.closest("[data-i]");
      if (r) return done(found[Number(r.dataset.i)]);
      if (e.target.closest("[data-d]")) done(null);
    };
    dialog.oncancel = (e) => { e.preventDefault(); done(null); };
    search();
    input.focus();
  });
}

view.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (el && page.act[el.dataset.act]) { e.preventDefault(); page.act[el.dataset.act](el, e); }
});
view.addEventListener("change", (e) => {
  const el = e.target.closest("[data-chg]");
  if (el && page.chg[el.dataset.chg]) page.chg[el.dataset.chg](el, e);
});
view.addEventListener("input", (e) => {
  const el = e.target.closest("[data-inp]");
  if (el && page.inp && page.inp[el.dataset.inp]) page.inp[el.dataset.inp](el, e);
});

function renderBanners(data) {
  const out = [];
  const ha = data.connection && data.connection.home_assistant;
  const link = data.connection && data.connection.companion;
  if (ha && !ha.connected) out.push(`<div class="notice warning">Not connected to Home Assistant (${esc(ha.last_error || ha.state)}). Predictions pause until it is back; nothing is lost.</div>`);
  if (link && link.state && !["publishing", "connected", "ready"].includes(link.state)) {
    out.push(`<div class="notice info">Home Assistant entities: ${esc(link.text || link.state)}. <a href="#/settings">Settings</a></div>`);
  }
  if (data.recovery) out.push(`<div class="notice error">The configuration file was damaged; a backup is used. Details in Settings.</div>`);
  banners.innerHTML = out.join("");
}

// ------------------------------------------------------------------ charts
// A small SVG line chart. series: [{values, cls, label, dash?}], band: {low, high}, shade: [bool] (heating).
function chart({ times, series, band = null, shade = null, height = 220, now = null }) {
  const W = 760, H = height, L = 40, R = 10, T = 10, B = 26;
  const all = series.flatMap((s) => s.values).concat(band ? [...band.low, ...band.high] : []).filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
  if (!all.length || times.length < 2) return `<p class="muted">Nothing to show yet.</p>`;
  let lo = Math.floor(Math.min(...all) - 0.5), hi = Math.ceil(Math.max(...all) + 0.5);
  if (hi - lo < 3) { hi += 1; lo -= 1; }
  const t0 = new Date(times[0]).getTime(), t1 = new Date(times[times.length - 1]).getTime();
  const x = (t) => L + ((new Date(t).getTime() - t0) / Math.max(1, t1 - t0)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const path = (vals) => {
    let d = "", pen = false;
    vals.forEach((v, i) => {
      if (v === null || v === undefined || Number.isNaN(v)) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(times[i]).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };
  let out = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Temperature chart">`;
  if (shade) {
    shade.forEach((on, i) => {
      if (on && i < times.length - 1) out += `<rect class="heat" x="${x(times[i]).toFixed(1)}" y="${T}" width="${Math.max(1, x(times[i + 1]) - x(times[i])).toFixed(1)}" height="${H - T - B}"></rect>`;
    });
  }
  for (let v = lo; v <= hi; v += Math.max(1, Math.round((hi - lo) / 5))) {
    out += `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"></line><text class="axis" x="4" y="${y(v) + 4}">${v}°</text>`;
  }
  const span = t1 - t0;
  const tick = span > 4 * 86400000 ? 86400000 : span > 36 * 3600000 ? 6 * 3600000 : 3 * 3600000;
  for (let t = Math.ceil(t0 / tick) * tick; t <= t1; t += tick) {
    const d = new Date(t);
    out += `<text class="axis" x="${x(d).toFixed(1)}" y="${H - 8}" text-anchor="middle">${tick >= 86400000 ? `${d.getDate()}.${d.getMonth() + 1}.` : `${pad(d.getHours())}:00`}</text>`;
  }
  if (band) {
    const pts = [];
    band.high.forEach((v, i) => { if (v !== null && v !== undefined && !Number.isNaN(v)) pts.push(`${x(times[i]).toFixed(1)},${y(v).toFixed(1)}`); });
    for (let i = band.low.length - 1; i >= 0; i--) { const v = band.low[i]; if (v !== null && v !== undefined && !Number.isNaN(v)) pts.push(`${x(times[i]).toFixed(1)},${y(v).toFixed(1)}`); }
    if (pts.length > 2) out += `<polygon class="band" points="${pts.join(" ")}"></polygon>`;
  }
  series.forEach((s) => { out += `<path class="line ${esc(s.cls)}" d="${path(s.values)}"></path>`; });
  if (now) out += `<line class="now" x1="${x(now)}" x2="${x(now)}" y1="${T}" y2="${H - B}"></line>`;
  out += `</svg><div class="legend">${series.map((s) => `<span class="key ${esc(s.cls)}">${esc(s.label)}</span>`).join("")}${band ? `<span class="key band">80 % uncertainty</span>` : ""}${shade ? `<span class="key heat">heating</span>` : ""}</div>`;
  return out;
}

// ------------------------------------------------------------------ overview
function card(r) {
  const s = r.status || {};
  const trend = r.heating_now ? `heating · ${rate(r.heating_rate)}` : rate(r.cooling_rate);
  const pre = r.preheat_start ? `Preheat from ${esc(clock(r.preheat_start))}` : r.state === "not_needed" ? "No preheat needed" : r.state === "below_target" ? "Below target now" : "";
  return `<a class="card room" href="#/rooms/${enc(r.area_id)}" data-room="${esc(r.area_id)}">
    <div class="row"><span class="name">${esc(r.name)}</span><span class="spacer"></span>${chip(s.state, s.text)}</div>
    <div class="temps"><div><span class="big">${deg(r.temp_c)}</span><span class="muted small">now</span></div>
      <div><span>${deg(r.target_c)}</span><span class="muted small">target</span></div>
      <div><span>${deg(r.predicted_c)}</span><span class="muted small">in 1 h</span></div></div>
    <div class="sub">${r.available ? esc(trend) : esc(r.reason || "No prediction yet")}${r.window_open ? " · window open" : ""}</div>
    ${pre ? `<div class="sub strong">${pre}</div>` : ""}
    ${r.observation_only ? `<div class="sub muted">Observation only (no thermostat)</div>` : ""}</a>`;
}

async function overviewPage(token) {
  const data = await api("api/overview");
  if (token !== navToken) return;
  renderBanners(data);
  page = { act: {}, chg: {}, inp: {} };
  const acq = data.acquisition || {};
  const progress = acq.state === "running" ? `<div class="notice info">Reading history from Home Assistant's Recorder: ${acq.records} records so far, ${acq.pending} windows to go.</div>` : "";
  if (!data.groups.length) {
    view.innerHTML = `<h1>Overview</h1>${progress}<div class="card empty"><h2>No rooms yet</h2>
      <p class="muted">Choose a room (a Home Assistant area), its temperature sensor and, if it has one, its thermostat. The App reads the room's history, learns how it warms and cools, and predicts when heating should start. It never controls a device: automations do, using the entities it publishes.</p>
      <a class="button primary" href="#/add">Add a room</a></div>`;
    return;
  }
  view.innerHTML = `<h1>Overview <span class="spacer"></span><a class="button primary" href="#/add">Add a room</a></h1>${progress}
    ${data.groups.map((g) => `<section class="group" data-group="${esc(g.kind)}"><div class="row"><h2>${esc(g.name)}</h2>${g.kind === "zone" ? `<span class="badge">thermal zone</span>` : ""}<span class="spacer"></span>
      ${g.kind === "zone" ? `<a class="button" href="#/zones">Edit zone</a>` : `<a class="button" href="#/add">Add a room</a>`}</div>
      <div class="grid">${g.rooms.map(card).join("")}</div></section>`).join("")}
    <p class="muted small">Every value is a prediction or recommendation from the learned model, never a command. ${data.forecast.hours ? `Weather forecast: ${data.forecast.hours} hours from ${esc(data.forecast.entity_id)}.` : "No weather forecast is used."}</p>`;
  setPoll(token, () => overviewPage(token), 60000);
}

function setPoll(token, fn, ms) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => { if (token === navToken && !dialog.open && !document.activeElement.matches("input, select, textarea")) fn(); else setPoll(token, fn, ms); }, ms);
}

// ------------------------------------------------------------------ add a room: area -> devices -> entities
const ROLES = [["", "Not used"], ["temperature", "Room temperature (primary)"], ["secondary", "Room temperature (extra)"], ["thermostats", "Thermostat / TRV"], ["openings_window", "Window sensor"], ["openings_door", "Door sensor"], ["occupancy", "Occupancy"], ["humidity", "Humidity"], ["sunlight", "Sunlight"], ["covers", "Cover / blind"], ["energy", "Energy / power"], ["running", "Heating system state"]];
const ROLE_FOR = { temperature: "temperature", thermostats: "thermostats", humidity: "humidity", openings: "openings_window", occupancy: "occupancy", sunlight: "sunlight", covers: "covers", energy: "energy", running: "running" };

async function addPage(token, areaId) {
  const areas = (await api("api/areas")).areas;
  if (token !== navToken) return;
  let step = areaId ? 2 : 1;
  let area = null;
  let chosenDevices = new Set();
  const roles = {};   // entity_id -> role (only what the user chose)
  let comfort = { day_c: 21, start: "06:30", end: "22:30" };
  async function loadArea(id) { area = await api(`api/areas/${enc(id)}`); }
  if (areaId) await loadArea(areaId);
  if (token !== navToken) return;
  function crumbs() {
    return `<nav class="crumbs"><button class="link" data-act="step" data-step="1" ${step === 1 ? "disabled" : ""}>Room${area ? `: ${esc(area.name)}` : ""}</button> › <button class="link" data-act="step" data-step="2" ${step < 3 ? "disabled" : ""}>Devices</button> › <span>Entities and comfort</span></nav>`;
  }
  function draw() {
    let body = "";
    if (step === 1) {
      body = `<h2>1. Which room?</h2><input type="search" data-inp="q" id="q" placeholder="Search areas" autocomplete="off">
        <div class="choices" id="areas">${areas.map((a) => `<button class="choice" data-act="area" data-id="${esc(a.area_id)}" data-name="${esc(a.name.toLowerCase())}" ${a.configured ? "disabled" : ""}><span><strong>${esc(a.name)}</strong><br><span class="muted small">${a.temperature_sensors} temperature sensor${a.temperature_sensors === 1 ? "" : "s"} · ${a.thermostats} thermostat${a.thermostats === 1 ? "" : "s"}${a.configured ? " · configured already" : ""}</span></span><span class="spacer"></span>›</button>`).join("") || `<p class="muted">Home Assistant has no areas. Create areas there and assign your sensors.</p>`}</div>`;
    } else if (step === 2) {
      body = `<h2>2. Which devices in ${esc(area.name)}?</h2><p class="muted small">Tick the devices whose entities belong to this room's climate. Nothing is selected for you.</p>
        <div id="devices">${area.devices.map((d, i) => `<label class="pick"><input type="checkbox" data-chg="device" data-i="${i}" ${chosenDevices.has(i) ? "checked" : ""}><span><strong>${esc(d.name)}</strong> <span class="muted small">${esc(d.model || "")}</span><br><span class="muted small">${d.entities.map((e) => esc(e.name)).join(", ")}</span></span></label>`).join("") || `<p class="muted">No climate-related entities in this area.</p>`}</div>
        <div class="actions"><button class="primary" data-act="step" data-step="3" ${chosenDevices.size ? "" : "disabled"}>Next</button><button data-act="step" data-step="3">Skip: choose entities by search</button></div>`;
    } else {
      const ents = [...chosenDevices].flatMap((i) => area.devices[i].entities);
      body = `<h2>3. What is each entity for?</h2>
        <p class="muted small">Suggestions are marked; only what you choose is used. An independent room sensor is better than a thermostat's own reading, which measures next to the heater.</p>
        <div id="entities">${ents.map((e) => {
          const suggested = ROLE_FOR[e.categories[0]];
          return `<div class="entity-row"><span><strong>${esc(e.name)}</strong> <span class="badge">${esc(e.state ?? "")}${e.unit ? " " + esc(e.unit) : ""}</span><br><span class="mono">${esc(e.entity_id)}</span>${suggested ? ` <span class="muted small">suggested: ${esc(ROLES.find((r) => r[0] === suggested)[1])}</span>` : ""}${e.caution ? `<br><span class="caution small">${esc(e.caution)}</span>` : ""}</span>
            <select data-chg="role" data-id="${esc(e.entity_id)}" aria-label="Role of ${esc(e.name)}">${ROLES.map(([v, l]) => `<option value="${v}" ${roles[e.entity_id] === v ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></div>`;
        }).join("") || `<p class="muted">No devices chosen.</p>`}</div>
        <div class="actions"><button data-act="search-temp">Find a temperature sensor elsewhere…</button><button data-act="search-thermostat">Find a thermostat elsewhere…</button></div>
        <h3>Comfort</h3>
        <div class="field"><label for="day">Comfort temperature</label><span><input type="number" id="day" data-inp="comfort" data-key="day_c" min="5" max="30" step="0.5" value="${comfort.day_c}"> °C</span></div>
        <div class="field"><label>Comfort period</label><span><input type="time" data-inp="comfort" data-key="start" value="${comfort.start}"> to <input type="time" data-inp="comfort" data-key="end" value="${comfort.end}"> <span class="muted small">(weekdays and weekends; refine later)</span></span></div>
        <div id="summary"></div>
        <div class="actions"><button class="primary" data-act="create">Add the room</button><a class="button" href="#/">Cancel</a></div>
        <p class="muted small">Window orientation, outdoor and weather sources, neighbouring rooms and the other optional inputs can be added on the room's Settings tab. None of them is required.</p>`;
    }
    view.innerHTML = `<h1>Add a room</h1>${crumbs()}<div class="card" id="wizard" data-step="${step}">${body}</div>`;
    if (step === 3) summary();
  }
  function room() {
    const r = { name: area.name, temperature: null, secondary: [], thermostats: [], openings: [], occupancy: [], humidity: [], sunlight: [], energy: [], running: [], windows: [],
      comfort: { day_c: Number(comfort.day_c), night_c: null, weekday: { start: comfort.start, end: comfort.end }, weekend: { start: comfort.start, end: comfort.end }, away_entity: null, away_c: null, follow_occupancy: false } };
    const covers = [];
    Object.entries(roles).forEach(([id, role]) => {
      if (!role) return;
      if (role === "temperature") { if (r.temperature) r.secondary.push(id); else r.temperature = id; }
      else if (role === "openings_window" || role === "openings_door") r.openings.push({ entity_id: id, kind: role.endsWith("door") ? "door" : "window" });
      else if (role === "covers") covers.push(id);
      else r[role].push(id);
    });
    return { r, covers };
  }
  function summary() {
    const { r } = room();
    const el = view.querySelector("#summary");
    const need = !r.temperature && !r.secondary.length && !r.thermostats.length;
    el.innerHTML = need ? `<div class="notice warning">Required: a room temperature sensor (or at least a thermostat that measures the room).</div>`
      : `<div class="notice info">Temperature: ${esc(r.temperature || r.secondary[0] || `${r.thermostats[0]} (its own reading)`)}${r.thermostats.length ? ` · ${r.thermostats.length} thermostat(s), observed only` : " · observation only (no thermostat)"}</div>`;
    view.querySelector("[data-act=create]").disabled = need;
  }
  page = {
    inp: {
      q: (el) => view.querySelectorAll("#areas .choice").forEach((b) => { b.hidden = !b.dataset.name.includes(el.value.toLowerCase()); }),
      comfort: (el) => { comfort[el.dataset.key] = el.value; },
    },
    chg: {
      device: (el) => { const i = Number(el.dataset.i); if (el.checked) chosenDevices.add(i); else chosenDevices.delete(i); draw(); },
      role: (el) => { roles[el.dataset.id] = el.value; summary(); },
    },
    act: {
      area: async (el) => { await loadArea(el.dataset.id); step = 2; chosenDevices = new Set(); draw(); },
      step: (el) => { step = Number(el.dataset.step); draw(); },
      "search-temp": async () => { const e = await pickEntity("temperature", "Choose a temperature sensor"); if (e) { roles[e.entity_id] = "temperature"; area.devices.push({ name: e.name, entities: [{ ...e, categories: ["temperature"] }] }); chosenDevices.add(area.devices.length - 1); draw(); } },
      "search-thermostat": async () => { const e = await pickEntity("thermostats", "Choose a thermostat"); if (e) { roles[e.entity_id] = "thermostats"; area.devices.push({ name: e.name, entities: [{ ...e, categories: ["thermostats"] }] }); chosenDevices.add(area.devices.length - 1); draw(); } },
      create: async () => {
        const { r, covers } = room();
        if (covers.length) r.windows = [];  // covers belong to windows: added on the Windows section
        try {
          session.revision = area.revision;
          await change("api/rooms/save", { area_id: area.area_id, room: r });
          toast(covers.length ? "Room added. Describe its windows (Settings tab) to use the covers." : "Room added. Its history is being read.");
          location.hash = `#/rooms/${enc(area.area_id)}`;
        } catch (err) { toast(errorText(err)); }
      },
    },
  };
  draw();
}

// ------------------------------------------------------------------ one room
const TABS = [["overview", "Overview"], ["history", "History"], ["model", "Thermal model"], ["prediction", "Prediction"], ["simulate", "Simulation"], ["settings", "Settings"]];

async function roomPage(areaId, tab, token) {
  let data = await api(`api/rooms/${enc(areaId)}`);
  if (token !== navToken) return;
  const tabs = `<nav class="tabs">${TABS.map(([k, l]) => `<a href="#/rooms/${enc(areaId)}/${k}" class="${k === tab ? "active" : ""}">${esc(l)}</a>`).join("")}</nav>`;
  const head = () => `<h1><a class="back" href="#/" aria-label="Back">←</a>${esc(data.room.name)} ${chip(data.status.state, data.status.text)}</h1>${tabs}`;
  page = { act: {}, chg: {}, inp: {} };
  if (tab === "overview") view.innerHTML = head() + roomOverview(data);
  else if (tab === "history") { view.innerHTML = head() + `<div class="card"><div class="row"><h2>Measured</h2><span class="spacer"></span><select data-chg="hours" aria-label="Period">${[[24, "1 day"], [72, "3 days"], [168, "7 days"], [720, "30 days"]].map(([v, l]) => `<option value="${v}" ${v === 72 ? "selected" : ""}>${l}</option>`).join("")}</select></div><div id="hist"><p class="muted">Loading…</p></div></div>` + coverageCard(data); page.chg.hours = (el) => history(areaId, Number(el.value)); history(areaId, 72); }
  else if (tab === "model") view.innerHTML = head() + modelTab(data);
  else if (tab === "prediction") view.innerHTML = head() + predictionTab(data);
  else if (tab === "simulate") { view.innerHTML = head() + simulateTab(data); simulateHandlers(areaId, data); }
  else if (tab === "settings") { view.innerHTML = head(); settingsTab(areaId, data, token); return; }
  page.act.retrain = async (el) => { el.disabled = true; el.textContent = "Learning…"; try { await api("api/rooms/retrain", { area_id: areaId }); toast("Model updated."); } catch (err) { toast(errorText(err)); } route(); };
  if (tab === "overview" || tab === "prediction") setPoll(token, () => roomPage(areaId, tab, token), 60000);
}

function roomOverview(data) {
  const v = data.view || {};
  const p = v.prediction || {};
  const now = v.now || {};
  const published = data.published || {};
  const pubRows = Object.entries(published).map(([k, e]) => `<tr><td>${esc(e.name)}</td><td class="mono">${esc(e.platform)}.spc_…_${esc(k)}</td><td>${e.value === null ? `<span class="muted">unknown</span>` : esc(e.value === true ? "on" : e.value === false ? "off" : `${e.value}${e.unit ? " " + e.unit : ""}`)}</td></tr>`).join("");
  return `<div class="two"><div>
    <div class="card"><div class="temps"><div><span class="big">${deg(now.temp_c)}</span><span class="muted small">measured now${now.source ? ` · ${esc(now.source)}` : ""}</span></div>
      <div><span>${deg(v.target_now)}</span><span class="muted small">target (${esc(v.target_reason || "")})</span></div>
      <div><span>${deg(p.predicted_1h)}</span><span class="muted small">predicted in 1 h</span></div></div>
      ${(now.problems || []).map((t) => `<div class="notice warning">${esc(t)}</div>`).join("")}
      ${p.available ? `<h3>Recommendation</h3>
        <p class="strong" id="recommendation">${p.state === "not_needed" ? "No preheat needed" : p.state === "unknown_heating" ? "No heating response learned yet" : p.preheat_start ? `Start heating ${esc(clock(p.preheat_start))}${p.requirement ? ` to reach ${deg(p.requirement.target_c)} at ${esc(clock(p.requirement.t))}` : ""}` : "–"}</p>
        <ul class="plain small" id="explanation">${(p.explanation || []).map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
        ${!p.reliable ? `<div class="notice warning">The model's confidence (${esc(p.confidence)}) is below the level set for recommendations; the recommendation entities stay “unknown”.</div>` : ""}
        ${(p.warnings || []).map((w) => `<div class="notice info small">${esc(w)}</div>`).join("")}`
      : `<div class="notice info">${esc(p.reason || "No prediction yet.")}</div>`}
    </div>
    <div class="card"><h2>What is missing</h2>${data.missing.length ? data.missing.map((m) => `<div class="problem"><span class="badge ${m.level === "required" || m.level === "warning" ? "warn" : ""}">${esc(m.level)}</span> <span>${esc(m.text)}</span></div>`).join("") : `<p class="muted">Nothing: every input is configured.</p>`}</div>
  </div><div>
    <div class="card"><h2>Published entities</h2><p class="muted small">Ordinary Home Assistant sensors for automations (for example built with Automation Builder). They are recommendations, not commands, and become unavailable when the App is not running.</p>
      ${pubRows ? `<div class="tscroll"><table><tbody>${pubRows}</tbody></table></div>` : `<p class="muted">Nothing published yet.</p>`}</div>
    <div class="card"><h2>Inputs now</h2><dl class="kv small">
      <dt>Heating</dt><dd>${now.heating === null || now.heating === undefined ? "not reported" : now.heating > 0 ? "heating" : "not heating"}</dd>
      <dt>Thermostat reading</dt><dd>${deg(now.thermostat_c)}</dd>
      <dt>Window</dt><dd>${now.window_open === null || now.window_open === undefined ? "unknown / no sensor" : now.window_open ? "open" : "closed"}</dd>
      <dt>Outdoor</dt><dd>${p.inputs ? kindBadge(p.inputs.outdoor) : "–"}</dd><dt>Sky</dt><dd>${p.inputs ? kindBadge(p.inputs.sky) : "–"}</dd></dl></div>
  </div></div>`;
}

async function history(areaId, hours) {
  const box = view.querySelector("#hist");
  try {
    const h = await api(`api/rooms/${enc(areaId)}/history?hours=${hours}`);
    const stats = Object.values(h.statistics || {})[0] || [];
    box.innerHTML = chart({ times: h.times, series: [{ values: h.temp, cls: "measured", label: "room (measured)" }, { values: h.setpoint, cls: "target", label: "setpoint" }, { values: h.outdoor, cls: "outdoor", label: "outdoor" }], shade: h.heating.map((v) => v > 0) })
      + (stats.length ? `<p class="muted small">Before the detailed history, Home Assistant's long-term statistics have ${stats.length} hourly means for this sensor; they are shown and kept, but not used for learning (hourly aggregates, not measurements of change).</p>` : "")
      + `<p class="muted small">Gaps are times without observations; they are never filled in.</p>`;
  } catch (err) { box.innerHTML = `<div class="notice error">${esc(errorText(err))}</div>`; }
}

function coverageCard(data) {
  const rows = Object.entries(data.coverage || {}).map(([e, c]) => `<tr><td>${esc(data.names[e] || e)}<br><span class="mono">${esc(e)}</span></td><td>${c ? esc(c.kind) : ""}</td><td>${c ? `${c.days} days` : "–"}</td><td>${c ? `${c.gaps} (${c.gap_hours} h)` : ""}</td><td>${c && c.recorder_limit ? esc(clock(c.recorder_limit)) : "–"}</td></tr>`).join("");
  return `<div class="card"><h2>Data coverage</h2><div class="tscroll"><table><thead><tr><th>Entity</th><th>Kind</th><th>Observed</th><th>Gaps</th><th>Recorder starts</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
}

const BASIS = { observed: "estimated from data", reported: "estimated from data", inferred: "estimated (heating inferred)", geometry: "estimated", "geometry+measured": "estimated with measured sunlight", "geometry+cloud": "estimated with cloud coverage", default: "assumed default", fixed: "assumed" };
function modelTab(data) {
  const m = data.model;
  if (!m) return `<div class="card"><h2>Thermal model</h2><p>${esc(data.status.text)}${data.status.reason ? `: ${esc(data.status.reason)}` : ""}.</p><p class="muted small">A first, preliminary model needs about a day of usable observations; it is called reliable only after it has predicted held-out data well (the minimum is set in Settings, 14 days by default).</p><button data-act="retrain">Learn now</button></div>`;
  const params = Object.entries(m.parameters || {}).map(([k, p]) => `<tr><td>${esc(p.text || k)}</td><td>${p.available ? `${esc(p.value)} ${esc(p.unit || "")}` : `<span class="muted">${esc(p.reason || "not available")}</span>`}</td><td>${p.se !== null && p.se !== undefined && p.available ? `± ${esc(p.se)}` : ""}</td><td>${esc(BASIS[p.basis] || p.basis || "")}</td><td>${esc(p.confidence || "")}</td></tr>`).join("");
  const val = m.validation || {};
  const vrows = Object.entries(val.horizons || {}).map(([h, x]) => x.available ? `<tr><td>${Number(h) / 60} h</td><td>${x.model.mae} °C</td><td>${x.persistence.mae} °C</td><td>${x.skill ?? "–"}</td><td>${x.origins}</td></tr>` : `<tr><td>${Number(h) / 60} h</td><td colspan="4" class="muted">${esc(x.reason || "not enough held-out data")}</td></tr>`).join("");
  const ch = m.characteristics || {};
  const warm = (ch.warming || []).map((w) => `<tr><td>${w.outdoor_c} °C</td><td>${w.start_c} °C</td><td>${rate(w.rise_per_h)}</td></tr>`).join("");
  const cool = (ch.cooling || []).map((w) => `<tr><td>${w.outdoor_c} °C</td><td>${rate(w.change_per_h)}</td></tr>`).join("");
  const wl = m.window_loss || {};
  return `<div class="two"><div>
    <div class="card"><div class="row"><h2>Learned parameters</h2><span class="spacer"></span><button data-act="retrain">Learn again now</button></div>
      <p class="small">Trained ${esc(clock(m.trained_at))} · confidence <strong>${esc((m.confidence || {}).level)}</strong>${m.preliminary ? " · preliminary" : ""}${m.current ? "" : " · <strong>the inputs changed since; it is being retrained</strong>"}</p>
      <div class="tscroll"><table><thead><tr><th>Parameter</th><th>Value</th><th>±</th><th>Kind</th><th>Confidence</th></tr></thead><tbody>${params}</tbody></table></div>
      <ul class="plain small">${(m.explanations || []).map((t) => `<li>${esc(typeof t === "string" ? t : t.text || "")}</li>`).join("")}</ul></div>
    <div class="card"><h2>How well it predicts</h2><p class="muted small">Measured on the last quarter of the observations, which the learning did not see, with the conditions that really happened (a perfect forecast). Persistence = “the temperature stays as it is”. Skill 1 = perfect, 0 = no better than persistence.</p>
      ${vrows ? `<div class="tscroll"><table><thead><tr><th>Ahead</th><th>Error (model)</th><th>Error (persistence)</th><th>Skill</th><th>Cases</th></tr></thead><tbody>${vrows}</tbody></table></div>` : `<p class="muted">No held-out validation yet.</p>`}</div>
  </div><div>
    <div class="card"><h2>What the model says about the room</h2>
      <dl class="kv small"><dt>Time constant</dt><dd>${ch.time_constant_h ?? "–"} h</dd><dt>Heating delay</dt><dd>${ch.lag_min ?? "–"} min</dd><dt>Radiator inertia</dt><dd>${ch.emitter_min ?? "–"} min</dd>
        <dt>Rise after heating stops</dt><dd>${ch.afterheat_c !== undefined ? deg(ch.afterheat_c, 2) : "–"}</dd><dt>Full sun adds</dt><dd>${ch.solar_c_per_h_full_sun !== undefined ? rate(ch.solar_c_per_h_full_sun) : "not measurable"}</dd>
        <dt>At night</dt><dd>${ch.night_c_per_h !== undefined ? rate(ch.night_c_per_h) : "no measurable difference"}</dd>
        <dt>Open window</dt><dd>${wl.available ? `extra loss ${wl.value_per_h}/h (from ${wl.hours} h in ${wl.openings} openings)` : `not measured yet${wl.reason ? ` (${esc(wl.reason)})` : ""}`}</dd></dl>
      ${warm ? `<h3>Warming with full heating</h3><div class="tscroll"><table><thead><tr><th>Outdoor</th><th>From</th><th>Rise</th></tr></thead><tbody>${warm}</tbody></table></div>` : ""}
      ${cool ? `<h3>Without heating, at 20 °C</h3><div class="tscroll"><table><thead><tr><th>Outdoor</th><th>Change</th></tr></thead><tbody>${cool}</tbody></table></div>` : ""}
      <p class="muted small">Computed from the learned model under the stated conditions (no sun). Correlation in the data is not proof of a physical cause; terms the data could not identify are left out rather than guessed.</p></div>
  </div></div>`;
}

function predictionTab(data) {
  const p = (data.view || {}).prediction || {};
  if (!p.available) return `<div class="card"><p>${esc(p.reason || "No prediction yet.")}</p></div>`;
  const s = p.steps;
  return `<div class="card"><h2>Next ${p.horizon_h} hours</h2>
    ${chart({ times: s.times, series: [{ values: s.expected, cls: "predicted", label: p.inputs.heating_course === "until_setpoint" ? `predicted, thermostat as set now (${p.inputs.setpoint_c} °C)` : "predicted, heating as now" }, { values: s.passive, cls: "passive", label: "without heating" }, { values: s.target, cls: "target", label: "comfort target" }], band: { low: s.low, high: s.high } })}
    <p class="small">Inputs: outdoor ${kindBadge(p.inputs.outdoor)} · sky ${kindBadge(p.inputs.sky)} · heating now ${kindBadge(p.inputs.heating_now)}. ${p.solar_gain_c ? `Sun: up to +${p.solar_gain_c} °C (an estimate).` : ""}</p>
    ${(p.warnings || []).map((w) => `<div class="notice info small">${esc(w)}</div>`).join("")}
    <div class="tscroll"><table><thead><tr><th>Time</th><th>Predicted</th><th>80 % range</th><th>Without heating</th><th>Target</th></tr></thead><tbody>
      ${p.hourly.filter((_, i) => i % 3 === 0).map((h) => `<tr><td>${esc(clock(h.t))}</td><td>${deg(h.expected)}</td><td>${h.low === null ? "–" : `${deg(h.low)} – ${deg(h.high)}`}</td><td>${deg(h.passive)}</td><td>${deg(h.target)}</td></tr>`).join("")}</tbody></table></div></div>`;
}

function simulateTab(data) {
  const now = (data.view || {}).now || {};
  return `<div class="two"><div><div class="card" id="sim-form"><h2>What if…</h2>
    <p class="muted small">A hypothetical scenario computed with this room's learned model. Nothing is sent to any device.</p>
    <div class="field"><label for="sim-heat">Heating</label><select id="sim-heat" data-inp="sim" data-key="heating"><option value="as_now">as it is now</option><option value="off">off</option><option value="on">on all the time</option><option value="window">on during a period</option></select></div>
    <div class="field" id="heat-period"><label>Heating period</label><span>from <input type="number" data-inp="sim" data-key="heat_from_min" min="0" value="0"> to <input type="number" data-inp="sim" data-key="heat_until_min" min="0" value="120"> minutes from now</span></div>
    <div class="field"><label for="sim-start">Start temperature</label><span><input type="number" id="sim-start" step="0.1" data-inp="sim" data-key="start_c" placeholder="${now.temp_c ?? ""}"> °C</span></div>
    <div class="field"><label for="sim-target">Target</label><span><input type="number" id="sim-target" step="0.5" data-inp="sim" data-key="target_c" placeholder="${(data.view || {}).target_now ?? 21}"> °C</span></div>
    <div class="field"><label for="sim-out">Outdoor</label><span><input type="number" id="sim-out" step="1" data-inp="sim" data-key="outdoor_offset" value="0"> °C warmer/colder than forecast, or fixed at <input type="number" step="1" data-inp="sim" data-key="outdoor_c"> °C</span></div>
    <div class="field"><label for="sim-cloud">Cloud cover</label><span><input type="number" id="sim-cloud" min="0" max="100" data-inp="sim" data-key="cloud_coverage" placeholder="forecast"> %</span></div>
    <div class="field"><label><input type="checkbox" data-inp="sim" data-key="window_open"> A window is open</label><span>from <input type="number" data-inp="sim" data-key="window_from_min" value="0"> to <input type="number" data-inp="sim" data-key="window_until_min" value="30"> minutes</span></div>
    <div class="field"><label for="sim-hours">Hours</label><input type="number" id="sim-hours" min="1" max="48" data-inp="sim" data-key="hours" value="12"></div>
    <div class="actions"><button class="primary" data-act="simulate">Simulate</button></div></div></div>
    <div><div class="card"><h2>Result</h2><div id="sim-result"><p class="muted">Choose a scenario and press Simulate.</p></div></div></div></div>`;
}
function simulateHandlers(areaId) {
  const sc = { heating: "as_now", hours: 12 };
  page.inp.sim = (el) => {
    const k = el.dataset.key;
    if (el.type === "checkbox") sc[k] = el.checked;
    else if (el.tagName === "SELECT") sc[k] = el.value;
    else sc[k] = el.value === "" ? null : Number(el.value);
  };
  view.querySelector("#sim-form").addEventListener("change", (e) => { const el = e.target.closest("[data-inp]"); if (el) page.inp.sim(el); });
  page.act.simulate = async () => {
    const box = view.querySelector("#sim-result");
    box.innerHTML = `<p class="muted">Computing…</p>`;
    try {
      const r = await api("api/rooms/simulate", { area_id: areaId, scenario: sc });
      if (!r.available) { box.innerHTML = `<div class="notice warning">${esc(r.reason)}</div>`; return; }
      const target = r.times.map(() => r.target_c);
      box.innerHTML = chart({ times: r.times, series: [{ values: r.baseline, cls: "passive", label: "as now (model)" }, { values: r.scenario, cls: "hypothetical", label: "scenario (hypothetical)" }, { values: target, cls: "target", label: "target" }], band: { low: r.low, high: r.high }, shade: r.heating.map((v) => v > 0) })
        + `<p id="sim-reach">${r.target_c === null || r.target_c === undefined ? "" : r.reach_min === null ? `The scenario does not reach ${deg(r.target_c)} within ${sc.hours || 12} h.` : `The scenario reaches ${deg(r.target_c)} after about ${Math.round(r.reach_min)} minutes.`}</p>
          <p class="small">Inputs: outdoor ${kindBadge(r.inputs.outdoor)} · sky ${kindBadge(r.inputs.sky)} · open-window effect ${kindBadge(r.inputs.window_loss)}</p>${(r.warnings || []).map((w) => `<div class="notice info small">${esc(w)}</div>`).join("")}`;
    } catch (err) { box.innerHTML = `<div class="notice error">${esc(errorText(err))}</div>`; }
  };
}

// --- room settings: every field saves when it changes
function settingsTab(areaId, data, token) {
  const room = JSON.parse(JSON.stringify(data.room));
  const names = data.names || {};
  const save = async (text = "Saved.") => {
    const status = view.querySelector("#save-status");
    try {
      await change("api/rooms/save", { area_id: areaId, room });
      if (status) { status.className = "save-status ok"; status.textContent = text; }
    } catch (err) { if (status) { status.className = "save-status bad"; status.textContent = `Not saved: ${errorText(err)}`; } }
  };
  const ent = (id) => id ? `<span>${esc(names[id] || id)}</span> <span class="mono">${esc(id)}</span>` : `<span class="muted">not set</span>`;
  const list = (key, label, category, title) => `<div class="field"><label>${esc(label)}</label><span>${(room[key] || []).map((id, i) => `<span class="tag">${ent(id)} <button class="link" data-act="remove" data-key="${key}" data-i="${i}" aria-label="Remove">×</button></span>`).join(" ") || `<span class="muted">none</span>`} <button class="link" data-act="add" data-key="${key}" data-category="${category}" data-title="${esc(title)}">Add…</button></span></div>`;
  const one = (key, label, category, title, holder = "room") => {
    const value = holder === "comfort" ? room.comfort[key] : room[key];
    return `<div class="field"><label>${esc(label)}</label><span>${ent(value)} <button class="link" data-act="set" data-key="${key}" data-holder="${holder}" data-category="${category}" data-title="${esc(title)}">${value ? "Change…" : "Choose…"}</button>${value ? ` <button class="link" data-act="clear" data-key="${key}" data-holder="${holder}">Clear</button>` : ""}</span></div>`;
  };
  const window = (w, i) => `<div class="part" data-window="${esc(w.id)}"><div class="row"><input type="text" data-inp="win" data-i="${i}" data-key="name" value="${esc(w.name)}" aria-label="Window name"><span class="spacer"></span><button class="link danger-text" data-act="del-window" data-i="${i}">Remove</button></div>
    <div class="field"><label>Faces</label><span><input type="number" min="0" max="359" data-inp="win" data-i="${i}" data-key="orientation" value="${w.orientation ?? ""}" aria-label="Orientation"> ° <span class="muted small">0° = South, 90° = West, 180° = North, 270° = East</span> ${[["S", 0], ["SE", 315], ["E", 270], ["NE", 225], ["N", 180], ["NW", 135], ["W", 90], ["SW", 45]].map(([n, v]) => `<button class="link" data-act="orient" data-i="${i}" data-v="${v}">${n}</button>`).join("")}</span></div>
    <div class="field"><label>Glass area</label><span><input type="number" min="0.1" max="50" step="0.1" data-inp="win" data-i="${i}" data-key="area_m2" value="${w.area_m2}"> m²</span></div>
    <div class="field"><label>Fixed shading</label><span><input type="number" min="0" max="100" step="5" data-inp="win" data-i="${i}" data-key="shading_pct" value="${Math.round(w.shading * 100)}"> % of the direct sun is always blocked (eaves, trees)</span></div>
    <div class="field"><label>Obstruction</label><span>${(w.obstructions || []).map((o, j) => `<span class="tag">${o.from}°–${o.to}° below ${o.elevation}° <button class="link" data-act="del-obs" data-i="${i}" data-j="${j}">×</button></span>`).join(" ")} from <input type="number" min="0" max="359" id="obs-from-${i}" aria-label="From"> to <input type="number" min="0" max="359" id="obs-to-${i}" aria-label="To"> below <input type="number" min="1" max="90" id="obs-el-${i}" aria-label="Elevation"> ° <button class="link" data-act="add-obs" data-i="${i}">Add</button>
      <div class="help">A hillside or building that hides the sun in these directions (same convention) below this elevation.</div></span></div>
    <div class="field"><label>Covers</label><span>${w.covers.map((c, j) => `<span class="tag">${ent(c)} <button class="link" data-act="del-cover" data-i="${i}" data-j="${j}">×</button></span>`).join(" ") || `<span class="muted">none</span>`} <button class="link" data-act="add-cover" data-i="${i}">Add…</button><div class="help">Observed only: their position tells how much sun gets in. The App never moves them.</div></span></div></div>`;
  function draw() {
    const c = room.comfort;
    view.querySelector("#settings-body").innerHTML = `
      <div class="card"><h2>Essentials</h2><span class="save-status" id="save-status"></span>
        <div class="field"><label for="r-name">Name</label><input type="text" id="r-name" data-inp="name" value="${esc(room.name)}" maxlength="80"></div>
        <div class="field"><label for="r-enabled">Enabled</label><input type="checkbox" id="r-enabled" data-chg="enabled" ${room.enabled ? "checked" : ""}></div>
        ${one("temperature", "Room temperature", "temperature", "Choose the room temperature sensor")}
        ${list("secondary", "Extra room sensors", "temperature", "Choose another room temperature sensor")}
        ${list("thermostats", "Thermostats / TRVs (observed)", "thermostats", "Choose a thermostat")}
        ${one("outdoor", "Outdoor temperature (this room)", "outdoor", "Choose an outdoor temperature source")}
        <p class="muted small">Without its own outdoor source the room uses the one in Settings.</p></div>
      <div class="card"><h2>Comfort</h2>
        <div class="field"><label>Comfort temperature</label><span><input type="number" min="5" max="30" step="0.5" data-chg="comfort" data-key="day_c" value="${c.day_c}"> °C</span></div>
        <div class="field"><label>Outside the comfort period</label><span><input type="number" min="5" max="30" step="0.5" data-chg="comfort" data-key="night_c" value="${c.night_c ?? ""}" placeholder="no requirement"> °C</span></div>
        <div class="field"><label>Weekdays</label><span><input type="time" data-chg="period" data-day="weekday" data-key="start" value="${c.weekday.start}"> to <input type="time" data-chg="period" data-day="weekday" data-key="end" value="${c.weekday.end}"></span></div>
        <div class="field"><label>Weekends</label><span><input type="time" data-chg="period" data-day="weekend" data-key="start" value="${c.weekend.start}"> to <input type="time" data-chg="period" data-day="weekend" data-key="end" value="${c.weekend.end}"></span></div>
        ${one("away_entity", "Away (optional)", "away", "Choose an entity that says nobody is home", "comfort")}
        <div class="field"><label>When away</label><span><input type="number" min="5" max="30" step="0.5" data-chg="comfort" data-key="away_c" value="${c.away_c ?? ""}" placeholder="no requirement"> °C</span></div></div>
      <div class="card"><h2>Windows and sun</h2><p class="muted small">Describe the windows that let direct sun in. The sun's position is computed for your home's location; the learned effect comes from the room's own temperatures.</p>
        ${room.windows.map(window).join("")}<div class="actions"><button data-act="add-window">Add a window</button></div></div>
      <div class="card"><details ${data._open ? "open" : ""}><summary>Optional inputs</summary>
        <div class="field"><label>Window and door sensors</label><span>${room.openings.map((o, i) => `<span class="tag">${ent(o.entity_id)} <select data-chg="opening-kind" data-i="${i}" aria-label="Kind"><option value="window" ${o.kind === "window" ? "selected" : ""}>window</option><option value="door" ${o.kind === "door" ? "selected" : ""}>door</option></select> <button class="link" data-act="remove-opening" data-i="${i}">×</button></span>`).join(" ") || `<span class="muted">none</span>`} <button class="link" data-act="add-opening">Add…</button></span></div>
        ${list("humidity", "Humidity", "humidity", "Choose a humidity sensor")}
        ${list("sunlight", "Sunlight / irradiance", "sunlight", "Choose a sunlight sensor")}
        ${list("energy", "Energy / power", "energy", "Choose an energy or power sensor")}
        ${list("running", "Heating system state", "running", "Choose the heating system's state")}
        ${list("occupancy", "Occupancy (optional)", "occupancy", "Choose an occupancy entity")}
        <div class="field"><label>Learn usage from occupancy</label><span><input type="checkbox" data-chg="occ-learning" ${room.occupancy_learning ? "checked" : ""}> <span class="muted small">Learns weekday and weekend patterns from the chosen entities' history. Off by default.</span></span></div>
        <div class="field"><label>Comfort only when usually occupied</label><span><input type="checkbox" data-chg="follow" ${c.follow_occupancy ? "checked" : ""}> <span class="muted small">Needs the learning above (14 days); the schedule still sets the outer limits.</span></span></div>
        <p class="muted small">Humidity, energy, heating-system state and wind are observed and kept for later; this version does not use them in the model.</p></details></div>
      <div class="card"><h2>Remove</h2><p class="muted small">The room's observations and model are kept, so adding it again continues where it stopped. Its entities are removed from Home Assistant.</p><button class="danger" data-act="delete-room">Remove the room</button></div>`;
  }
  view.insertAdjacentHTML("beforeend", `<div id="settings-body"></div>`);
  page = {
    inp: {
      name: (el) => { room.name = el.value; clearTimeout(page.t); page.t = setTimeout(save, 600); },
      win: (el) => {
        const w = room.windows[Number(el.dataset.i)];
        const k = el.dataset.key;
        if (k === "name") w.name = el.value;
        else if (k === "shading_pct") w.shading = el.value === "" ? 0 : Number(el.value) / 100;
        else w[k] = el.value === "" ? null : Number(el.value);
        clearTimeout(page.t); page.t = setTimeout(save, 600);
      },
    },
    chg: {
      enabled: (el) => { room.enabled = el.checked; save(); },
      comfort: (el) => { room.comfort[el.dataset.key] = el.value === "" ? null : Number(el.value); save(); },
      period: (el) => { room.comfort[el.dataset.day][el.dataset.key] = el.value; save(); },
      "opening-kind": (el) => { room.openings[Number(el.dataset.i)].kind = el.value; save(); },
      "occ-learning": (el) => { room.occupancy_learning = el.checked; save(); },
      follow: (el) => { room.comfort.follow_occupancy = el.checked; save(); },
    },
    act: {
      add: async (el) => { const e = await pickEntity(el.dataset.category, el.dataset.title, areaId); if (!e) return; names[e.entity_id] = e.name; room[el.dataset.key] = [...(room[el.dataset.key] || []), e.entity_id]; draw(); save(); },
      remove: (el) => { room[el.dataset.key].splice(Number(el.dataset.i), 1); draw(); save(); },
      set: async (el) => { const e = await pickEntity(el.dataset.category, el.dataset.title, el.dataset.category === "outdoor" || el.dataset.category === "away" ? null : areaId); if (!e) return; names[e.entity_id] = e.name; if (el.dataset.holder === "comfort") room.comfort[el.dataset.key] = e.entity_id; else room[el.dataset.key] = e.entity_id; draw(); save(); },
      clear: (el) => { if (el.dataset.holder === "comfort") room.comfort[el.dataset.key] = null; else room[el.dataset.key] = null; draw(); save(); },
      "add-opening": async () => { const e = await pickEntity("openings", "Choose a window or door sensor", areaId); if (!e) return; names[e.entity_id] = e.name; room.openings.push({ entity_id: e.entity_id, kind: e.device_class === "door" ? "door" : "window" }); draw(); save(); },
      "remove-opening": (el) => { room.openings.splice(Number(el.dataset.i), 1); draw(); save(); },
      "add-window": async () => { const { id } = await api("api/windows/new-id", {}); room.windows.push({ id, name: `Window ${room.windows.length + 1}`, orientation: null, area_m2: 1.5, shading: 0, obstructions: [], covers: [] }); draw(); },
      "del-window": (el) => { room.windows.splice(Number(el.dataset.i), 1); draw(); save(); },
      orient: (el) => { room.windows[Number(el.dataset.i)].orientation = Number(el.dataset.v); draw(); save(); },
      "add-obs": (el) => {
        const i = el.dataset.i;
        const o = { from: Number(view.querySelector(`#obs-from-${i}`).value), to: Number(view.querySelector(`#obs-to-${i}`).value), elevation: Number(view.querySelector(`#obs-el-${i}`).value) };
        if ([o.from, o.to, o.elevation].some((v) => Number.isNaN(v))) { toast("Enter from, to and elevation."); return; }
        room.windows[Number(i)].obstructions.push(o); draw(); save();
      },
      "del-obs": (el) => { room.windows[Number(el.dataset.i)].obstructions.splice(Number(el.dataset.j), 1); draw(); save(); },
      "add-cover": async (el) => { const e = await pickEntity("covers", "Choose a cover of this window", areaId); if (!e) return; names[e.entity_id] = e.name; room.windows[Number(el.dataset.i)].covers.push(e.entity_id); draw(); save(); },
      "del-cover": (el) => { room.windows[Number(el.dataset.i)].covers.splice(Number(el.dataset.j), 1); draw(); save(); },
      "delete-room": async () => {
        if (!await ask({ title: `Remove ${room.name}?`, body: "<p>Its entities are removed from Home Assistant. Observations and the model are kept.</p>", ok: "Remove", danger: true })) return;
        try { await change("api/rooms/delete", { area_id: areaId }); toast("Room removed."); location.hash = "#/"; } catch (err) { toast(errorText(err)); }
      },
    },
  };
  session.revision = data.revision;
  draw();
}

// ------------------------------------------------------------------ thermal zones
async function zonesPage(token) {
  const data = await api("api/settings");
  if (token !== navToken) return;
  const zones = data.zones;
  const rooms = data.rooms;
  let editing = null;
  function draw() {
    const inZone = new Set(Object.entries(zones).filter(([id]) => !editing || id !== editing.id).flatMap(([, z]) => z.rooms));
    view.innerHTML = `<h1>Thermal zones <span class="spacer"></span><button class="primary" data-act="new">New zone</button></h1>
      <p class="muted small">Rooms that exchange heat - an open-plan living area, a hall with a staircase, rooms joined without a door. Each room keeps its own temperature, target and prediction; the App tests whether a connected room measurably influences it and uses that only where the data shows it. A room belongs to one zone.</p>
      ${Object.entries(zones).map(([id, z]) => `<div class="card" data-zone="${esc(id)}"><div class="row"><h2>${esc(z.name)}</h2><span class="spacer"></span><button data-act="edit" data-id="${esc(id)}">Edit</button><button class="danger" data-act="delete" data-id="${esc(id)}">Delete</button></div>
        <ul class="plain">${z.connections.map((c) => `<li>${esc(rooms[c.a] || c.a)} ⇄ ${esc(rooms[c.b] || c.b)} · ${esc(c.strength)}${c.door ? ` · door sensor ${esc(c.door)} (closed = barrier)` : ""}</li>`).join("")}</ul></div>`).join("") || `<p class="muted">No thermal zones.</p>`}
      ${editing ? `<div class="card review" id="zone-editor"><h2>${editing.id ? "Edit" : "New"} thermal zone</h2>
        <div class="field"><label for="z-name">Name</label><input type="text" id="z-name" data-inp="zname" value="${esc(editing.zone.name)}"></div>
        <div class="field"><label>Rooms</label><span>${Object.entries(rooms).map(([a, n]) => `<label><input type="checkbox" data-chg="zroom" value="${esc(a)}" ${editing.zone.rooms.includes(a) ? "checked" : ""} ${inZone.has(a) ? "disabled" : ""}> ${esc(n)}${inZone.has(a) ? " (in another zone)" : ""}</label>`).join(" ")}</span></div>
        ${editing.zone.rooms.length >= 2 ? `<h3>Connections</h3>${pairs(editing.zone.rooms).map(([a, b]) => { const c = editing.zone.connections.find((x) => x.a === [a, b].sort()[0] && x.b === [a, b].sort()[1]); return `<div class="field"><label>${esc(rooms[a])} ⇄ ${esc(rooms[b])}</label><span><select data-chg="strength" data-a="${esc(a)}" data-b="${esc(b)}" aria-label="Strength">${[["", "not connected"], ["weak", "weak (small opening)"], ["normal", "normal (doorway)"], ["strong", "strong (wide opening)"], ["open", "open plan"]].map(([v, l]) => `<option value="${v}" ${(c ? c.strength : "") === v ? "selected" : ""}>${l}</option>`).join("")}</select>
          ${c ? ` <button class="link" data-act="door" data-a="${esc(a)}" data-b="${esc(b)}">${c.door ? `Door: ${esc(c.door)}` : "Add a door sensor…"}</button>` : ""}</span></div>`; }).join("")}
          <p class="muted small">Strength orders which connections are tested first; how strong the exchange really is comes from the data. Without measurable exchange the rooms are modelled independently.</p>` : `<p class="muted small">Choose at least two rooms.</p>`}
        <div class="actions"><button class="primary" data-act="save">Save</button><button data-act="cancel">Cancel</button></div></div>` : ""}`;
  }
  const pairs = (list) => list.flatMap((a, i) => list.slice(i + 1).map((b) => [a, b]));
  page = {
    inp: { zname: (el) => { editing.zone.name = el.value; } },
    chg: {
      zroom: (el) => { const r = editing.zone.rooms; if (el.checked) r.push(el.value); else editing.zone.rooms = r.filter((x) => x !== el.value); editing.zone.connections = editing.zone.connections.filter((c) => editing.zone.rooms.includes(c.a) && editing.zone.rooms.includes(c.b)); draw(); },
      strength: (el) => { const [a, b] = [el.dataset.a, el.dataset.b].sort(); editing.zone.connections = editing.zone.connections.filter((c) => !(c.a === a && c.b === b)); if (el.value) editing.zone.connections.push({ a, b, strength: el.value, door: null }); draw(); },
    },
    act: {
      new: () => { editing = { id: null, zone: { name: "Thermal zone", rooms: [], connections: [] } }; draw(); },
      edit: (el) => { editing = { id: el.dataset.id, zone: JSON.parse(JSON.stringify(zones[el.dataset.id])) }; draw(); },
      cancel: () => { editing = null; draw(); },
      door: async (el) => { const e = await pickEntity("openings", "Choose the door sensor between the rooms"); const [a, b] = [el.dataset.a, el.dataset.b].sort(); const c = editing.zone.connections.find((x) => x.a === a && x.b === b); if (c) c.door = e ? e.entity_id : null; draw(); },
      save: async () => { try { await change("api/zones/save", { zone_id: editing.id, zone: editing.zone }); toast("Zone saved."); route(); } catch (err) { toast(errorText(err)); } },
      delete: async (el) => { if (!await ask({ title: "Delete this thermal zone?", body: "<p>The rooms stay; they are then modelled independently.</p>", ok: "Delete", danger: true })) return; try { await change("api/zones/delete", { zone_id: el.dataset.id }); route(); } catch (err) { toast(errorText(err)); } },
    },
  };
  draw();
}

// ------------------------------------------------------------------ settings
async function settingsPage(token) {
  let data = await api("api/settings");
  if (token !== navToken) return;
  renderBanners(data);
  const s = data.settings;
  let pendingImport = null;
  const link = (data.connection || {}).companion || {};
  const save = async () => { try { await change("api/settings/save", { settings: s }); toast("Saved."); } catch (err) { toast(errorText(err)); } };
  const cov = Object.entries(data.coverage || {}).map(([e, c]) => `<tr><td class="mono">${esc(e)}</td><td>${esc(c.kind)}</td><td>${c.days} d</td><td>${c.gaps}</td><td>${c.recorder_limit ? esc(clock(c.recorder_limit)) : "–"}</td><td>${c.statistics_hours || ""}</td></tr>`).join("");
  view.innerHTML = `<h1>Settings</h1><div class="two"><div>
    <div class="card"><h2>General</h2>
      <div class="field"><label>Outdoor temperature</label><span>${s.outdoor ? `<span class="mono">${esc(s.outdoor)}</span>` : `<span class="muted">not set</span>`} <button class="link" data-act="pick" data-key="outdoor" data-category="outdoor">Choose…</button>${s.outdoor ? ` <button class="link" data-act="clear" data-key="outdoor">Clear</button>` : ""}</span></div>
      <div class="field"><label>Weather (forecast)</label><span>${s.weather ? `<span class="mono">${esc(s.weather)}</span>` : `<span class="muted">not set</span>`} <button class="link" data-act="pick" data-key="weather" data-category="weather">Choose…</button>${s.weather ? ` <button class="link" data-act="clear" data-key="weather">Clear</button>` : ""}
        <div class="help">Gives the hourly outdoor temperature and cloud forecast. Without it the present values are assumed to hold.</div></span></div>
      <div class="field"><label for="hd">History to learn from</label><span><input type="number" id="hd" min="7" max="120" data-chg="num" data-key="history_days" value="${s.history_days}"> days <span class="muted small">(read from the Recorder where it still has it; Recorder retention is not changed)</span></span></div>
      <div class="field"><label for="md">Minimum history</label><span><input type="number" id="md" min="3" max="60" data-chg="num" data-key="min_history_days" value="${s.min_history_days}"> days before a model is more than preliminary</span></div>
      <div class="field"><label for="hz">Forecast horizon</label><span><input type="number" id="hz" min="6" max="72" data-chg="num" data-key="horizon_hours" value="${s.horizon_hours}"> hours</span></div>
      <div class="field"><label for="rf">Recommend from</label><select id="rf" data-chg="text" data-key="recommend_from">${["low", "moderate", "high"].map((v) => `<option value="${v}" ${s.recommend_from === v ? "selected" : ""}>${v} confidence</option>`).join("")}</select>
        <div class="help">Below this confidence the recommendation entities stay “unknown”; predictions are still shown here.</div></div></div>
    <div class="card"><h2>Home Assistant entities</h2><p class="small">The companion integration <span class="mono">smart_predictive_climate</span> publishes the predictions as sensors. Status: <strong>${esc(link.text || link.state || "unknown")}</strong></p>
      <div class="actions"><button data-act="pair">${link.paired ? "Pair again" : "Pair with the companion"}</button>${link.paired ? `<button class="danger" data-act="unpair">Forget the pairing</button>` : ""}</div>
      <p class="muted small">Install the companion through HACS (repository havanissa/smart-predictive-climate) and add it in Settings → Devices & services. It registers no service and can control nothing.</p></div>
    <div class="card"><h2>Backup and restore</h2><div class="actions"><a class="button" href="api/export" download="smart-predictive-climate-config.json">Download configuration</a><label class="button">Import…<input type="file" data-chg="import" accept=".json,application/json" hidden></label></div><div id="import-review"></div>
      <h3>Automatic backups</h3>${data.backups.length ? `<div class="tscroll"><table><thead><tr><th>When</th><th>Rooms</th><th></th></tr></thead><tbody>${data.backups.slice(0, 15).map((b) => `<tr><td>${esc(clock(b.backed_up_at))}</td><td>${b.rooms ?? "?"}</td><td>${b.damaged ? "damaged" : `<button class="link" data-act="restore" data-name="${esc(b.name)}">Restore</button>`}</td></tr>`).join("")}</tbody></table></div>` : `<p class="muted">None yet.</p>`}</div>
  </div><div>
    <div class="card"><h2>History acquisition</h2><dl class="kv small"><dt>State</dt><dd>${esc(data.acquisition.state)}${data.acquisition.error ? ` · ${esc(data.acquisition.error)}` : ""}</dd><dt>Windows read</dt><dd>${data.acquisition.windows}</dd><dt>Records</dt><dd>${data.acquisition.records}</dd><dt>Statistics</dt><dd>${esc(data.acquisition.statistics || "–")}</dd><dt>Store</dt><dd>${esc(JSON.stringify(data.store))}</dd></dl>
      ${cov ? `<div class="tscroll"><table><thead><tr><th>Entity</th><th>Kind</th><th>Observed</th><th>Gaps</th><th>Recorder starts</th><th>Hourly stats</th></tr></thead><tbody>${cov}</tbody></table></div>` : ""}</div>
    <div class="card"><h2>Diagnostics</h2><div class="actions"><a class="button" href="api/diagnostics" download="smart-predictive-climate-diagnostics.json">Download diagnostics</a></div></div>
  </div></div>`;
  page = {
    act: {
      pick: async (el) => { const e = await pickEntity(el.dataset.category, el.dataset.category === "weather" ? "Choose a weather entity" : "Choose an outdoor temperature source"); if (e) { s[el.dataset.key] = e.entity_id; await save(); route(); } },
      clear: async (el) => { s[el.dataset.key] = null; await save(); route(); },
      pair: async () => { try { await api("api/companion/pair", { replace: true }); toast("Paired."); } catch (err) { toast(errorText(err)); } route(); },
      unpair: async () => { if (!await ask({ title: "Forget the pairing?", body: "<p>The entities become unavailable until the App is paired again.</p>", ok: "Forget", danger: true })) return; try { await api("api/companion/unpair", {}); } catch (err) { toast(errorText(err)); } route(); },
      restore: async (el) => {
        try {
          const review = await api("api/backups/review", { name: el.dataset.name });
          if (!await ask({ title: "Restore this backup?", body: `<ul>${review.changes.map((c) => `<li>${esc(c.name)}: ${esc(c.type)}</li>`).join("") || "<li>Nothing changes.</li>"}</ul><p class="small">${esc(review.note)}</p>`, ok: "Restore" })) return;
          await change("api/backups/restore", { name: el.dataset.name }); toast("Restored."); route();
        } catch (err) { toast(errorText(err)); }
      },
      "import-apply": async () => { try { await change("api/import/apply", { document: pendingImport }); toast("Imported."); route(); } catch (err) { toast(errorText(err)); } },
    },
    chg: {
      num: (el) => { s[el.dataset.key] = Number(el.value); save(); },
      text: (el) => { s[el.dataset.key] = el.value; save(); },
      import: async (el) => {
        const box = view.querySelector("#import-review");
        try {
          pendingImport = JSON.parse(await el.files[0].text());
          const review = await api("api/import/review", { document: pendingImport });
          box.innerHTML = `<div class="notice info"><ul>${review.changes.map((c) => `<li>${esc(c.name)}: ${esc(c.type)}</li>`).join("") || "<li>Nothing changes.</li>"}</ul><p class="small">${esc(review.note)}</p><button class="primary" data-act="import-apply">Replace the configuration</button></div>`;
        } catch (err) { box.innerHTML = `<div class="notice error">${esc(errorText(err))}</div>`; }
      },
    },
    inp: {},
  };
}

// ------------------------------------------------------------------ router
async function route() {
  const token = ++navToken;
  clearTimeout(pollTimer);
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const section = parts[0] || "overview";
  const NAV = { add: "overview", rooms: "rooms" };
  document.querySelectorAll("header nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#/${NAV[section] === "overview" ? "" : section === "overview" ? "" : section}`));
  page = { act: {}, chg: {}, inp: {} };
  try {
    if (section === "overview") await overviewPage(token);
    else if (section === "add") await addPage(token, parts[1] || null);
    else if (section === "rooms" && parts[1]) await roomPage(parts[1], parts[2] || "overview", token);
    else if (section === "rooms") await overviewPage(token);
    else if (section === "zones") await zonesPage(token);
    else if (section === "settings") await settingsPage(token);
    else location.hash = "#/";
  } catch (err) {
    if (token === navToken) view.innerHTML = `<div class="notice error">${esc(errorText(err))}</div><p><a href="#/">Back to the overview</a></p>`;
  }
}
// On narrow screens tables are shown as labelled rows (CSS); each cell gets its column's heading as a label.
function labelTables(root) {
  root.querySelectorAll(".tscroll table").forEach((table) => {
    const heads = [...table.querySelectorAll("thead th")].map((th) => th.textContent.trim());
    if (!heads.length) return;
    table.classList.add("stack");
    table.querySelectorAll("tbody tr").forEach((tr) => [...tr.children].forEach((td, i) => { if (heads[i]) td.dataset.label = heads[i]; }));
  });
}
new MutationObserver(() => labelTables(view)).observe(view, { childList: true, subtree: true });
window.addEventListener("hashchange", route);
route();
