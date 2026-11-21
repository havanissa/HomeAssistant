// Smart Occupancy Detection - the web interface (Overview, Rooms, Settings). No build step, no dependencies.
// Every change is reviewed first; the review says whether a room's decision would change at once.

const view = document.getElementById("view");
const banners = document.getElementById("banners");
let pollTimer = null;
let user = { is_admin: false };
let navToken = 0;

// ------------------------------------------------------------------ helpers
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const enc = encodeURIComponent;

async function api(path, body) {
  const options = { credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json", "X-SOD": "1" };
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
  return data;
}

function toast(text) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 3500);
}

const pad = (n) => String(n).padStart(2, "0");
function clock(iso, withSeconds = true) {
  if (!iso) return "";
  const d = new Date(iso);
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}${withSeconds ? ":" + pad(d.getSeconds()) : ""}`;
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return time;
  return `${d.getDate()}.${d.getMonth() + 1}. ${time}`;
}
function duration(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return seconds % 60 && minutes < 10 ? `${minutes} min ${seconds % 60} s` : `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
  return `${Math.floor(hours / 24)} days`;
}
const since = (iso) => (iso ? duration((Date.now() - new Date(iso)) / 1000) : "");
function countdown(iso) {
  const s = Math.max(0, Math.round((new Date(iso) - Date.now()) / 1000));
  return `${Math.floor(s / 60)}:${pad(s % 60)}`;
}
const STATUS_TEXT = { occupied: "Occupied", vacant: "Vacant", uncertain: "Uncertain", off: "Off" };
const chip = (status, big = false) => `<span class="status ${esc(status)}${big ? " big" : ""}">${esc(STATUS_TEXT[status] || status)}</span>`;

function subline(room) {
  if (!room.enabled) return "Detection turned off";
  if (room.state === "possible_vacancy" && room.vacancy_at) return `Vacancy pending: vacant in ${countdown(room.vacancy_at)} unless there is activity`;
  if (room.state === "possible_occupancy") return room.occupied ? "Possible occupancy: waiting for confirmation" : "Door movement, no presence detected";
  if (room.state === "uncertain") return room.occupied === true ? "Sensors offline: kept as occupied" : room.occupied === false ? "Sensors offline: kept as vacant" : "Sensors offline";
  if (room.state === "confirmed_occupancy") return "Confirmed";
  return room.label || "";
}

// Replace the page without closing what the user opened or losing keyboard focus (polling re-renders it).
function render(html) {
  const open = new Set([...view.querySelectorAll("details[data-keep][open]")].map((d) => d.dataset.keep));
  const active = document.activeElement;
  const focusKey = active && view.contains(active) ? (active.getAttribute("href") || active.id || "") : "";
  view.innerHTML = html;
  view.querySelectorAll("details[data-keep]").forEach((d) => { if (open.has(d.dataset.keep)) d.open = true; });
  if (focusKey) {
    const again = [...view.querySelectorAll("a[href], [id]")].find((el) => (el.getAttribute("href") || el.id) === focusKey);
    if (again) again.focus({ preventScroll: true });
  }
}

function setPoll(fn, ms) {
  clearInterval(pollTimer);
  pollTimer = setInterval(() => { if (!document.hidden) fn().catch(() => {}); }, ms);
}

function problemsList(problems) {
  if (!problems || !problems.length) return "";
  return `<div>${problems.map((p) => `<div class="problem"><span class="dot ${esc(p.level || "warning")}"></span><span>${esc(p.text || p.message)}</span></div>`).join("")}</div>`;
}

function renderBanners(data) {
  const out = [];
  const ha = data.connection && data.connection.home_assistant;
  const link = data.connection && data.connection.companion;
  if (ha && !ha.connected) out.push(`<div class="notice warning">Not connected to Home Assistant (${esc(ha.last_error || ha.state)}). Decisions are frozen; no room is made vacant until the connection is back. Changes cannot be made meanwhile.</div>`);
  const compat = link && link.compatibility;
  if (compat && compat.level === "warning" && compat.message) out.push(`<div class="notice info"><strong>Update recommended:</strong> ${esc(compat.message)}</div>`);
  if (data.connection && data.connection.fresh === false && ha && ha.connected && link && link.has_token && link.state === "waiting") {
    out.push(`<div class="notice warning">The occupancy entities in Home Assistant are not fresh right now (${esc(link.detail || "reconnecting")}); they keep their last state.</div>`);
  }
  if (link && ha && ha.connected && !link.publishing && ["not_installed", "not_set_up", "not_paired", "pair_mismatch", "busy", "error", "blocked", "incompatible"].includes(link.state)) {
    out.push(`<div class="notice info"><strong>Home Assistant entities:</strong> ${esc(link.detail || link.state)} <a href="#/settings">Settings</a></div>`);
  }
  if (data.recovery) out.push(`<div class="notice error">The configuration file was damaged. ${data.recovery.used ? `The copy ${esc(data.recovery.used)} is used.` : "No usable copy was found."} Details in Settings → Diagnostics.</div>`);
  if (data.runtime && data.runtime.self_test && !data.runtime.self_test.passed) out.push(`<div class="notice error">The engine self-test failed: nothing is published to Home Assistant.</div>`);
  if (data.runtime && data.runtime.state_notice) out.push(`<div class="notice warning">${esc(data.runtime.state_notice)}</div>`);
  banners.innerHTML = out.join("");
}

// ------------------------------------------------------------------ router
async function route() {
  clearInterval(pollTimer);
  const token = ++navToken;
  const hash = location.hash.replace(/^#\/?/, "");
  const parts = hash.split("/").filter(Boolean).map(decodeURIComponent);
  const section = parts[0] || "overview";
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === section));
  try {
    if (section === "overview") await overviewPage(token);
    else if (section === "rooms" && parts[1] === "new") await editorPage(null, token);
    else if (section === "rooms" && parts[1] && parts[2] === "edit") await editorPage(parts[1], token);
    else if (section === "rooms" && parts[1]) await roomPage(parts[1], token);
    else if (section === "rooms") await roomsPage(token);
    else if (section === "settings") await settingsPage(token);
    else location.hash = "#/";
  } catch (err) {
    if (token === navToken) view.innerHTML = `<div class="notice error">${esc(err.message)}</div>`;
  }
}
window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ overview
function roomCard(room) {
  const warn = room.warnings ? `<div class="warn-line">⚠ ${esc(room.problems.filter((p) => p.sensor).map((p) => p.text)[0] || "Sensor problem")}${room.warnings > 1 ? ` (+${room.warnings - 1} more)` : ""}</div>` : "";
  const contributing = room.enabled ? (room.contributing.length ? `Active: ${esc(room.contributing.join(", "))}` : "No sensor active right now") : "";
  const activity = room.last_activity ? ` · last activity ${esc(since(room.last_activity))} ago` : "";
  return `<a class="card room" href="#/rooms/${enc(room.area_id)}">
    <div class="row"><span class="name">${esc(room.name)}</span><span class="spacer"></span>${chip(room.status)}</div>
    <div class="sub">${esc(subline(room))}${room.since && room.enabled ? ` · for ${esc(since(room.since))}` : ""}</div>
    <div class="contrib">${contributing}${activity}</div>${warn}</a>`;
}

async function overviewPage(token) {
  const load = async () => {
    const data = await api("api/overview");
    if (token !== navToken) return;
    user = data.user;
    renderBanners(data);
    if (!data.rooms.length) {
      view.innerHTML = `<h1>Overview</h1><div class="card empty"><h2>No rooms yet</h2><p class="muted">Add a room, choose its sensors, and Smart Occupancy Detection publishes its occupancy to Home Assistant.</p>${user.is_admin ? `<a class="button primary" href="#/rooms/new">Add a room</a>` : ""}</div>`;
      return;
    }
    const occupied = data.rooms.filter((r) => r.status === "occupied").length;
    render(`<h1>Overview <span class="muted small">${occupied} of ${data.rooms.length} rooms occupied</span></h1><div class="grid">${data.rooms.map(roomCard).join("")}</div>`);
  };
  await load();
  setPoll(load, 2000);
}

// ------------------------------------------------------------------ rooms list
async function roomsPage(token) {
  const data = await api("api/overview");
  if (token !== navToken) return;
  user = data.user;
  renderBanners(data);
  const rows = data.rooms.map((r) => `<tr><td><a href="#/rooms/${enc(r.area_id)}">${esc(r.name)}</a></td><td>${chip(r.status)}</td><td>${r.sensor_count}</td><td>${r.warnings ? `<span class="badge warn">${r.warnings} sensor problem${r.warnings > 1 ? "s" : ""}</span>` : ""}</td><td>${user.is_admin ? `<a href="#/rooms/${enc(r.area_id)}/edit">Edit</a>` : ""}</td></tr>`).join("");
  view.innerHTML = `<h1>Rooms <span class="spacer"></span>${user.is_admin ? `<a class="button primary" href="#/rooms/new">Add a room</a>` : ""}</h1>
    <div class="card">${data.rooms.length ? `<div class="tscroll"><table><thead><tr><th>Room</th><th>Now</th><th>Sensors</th><th></th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : `<p class="muted">No rooms are configured yet.</p>`}</div>
    <p class="muted small">A room is a Home Assistant area. Create areas in Home Assistant (Settings → Areas, labels &amp; zones); renaming an area later keeps its room and its entities.</p>`;
}

// ------------------------------------------------------------------ room detail
const KIND = { pir: "Motion (PIR)", radar: "Presence (radar)", door: "Door", pc_activity: "PC activity", bed: "Bed", other: "Other" };
const ROLE = { trigger: "Triggers", maintain: "Maintains", confirm_vacancy: "Maintains, confirms vacancy", ignore: "Ignored" };
const TIMING_LABEL = { correlation_window: "Occupancy confirmation", inactivity_timeout: "Vacancy timeout", exit_confirmation: "Fast exit timeout", dropout_grace: "Radar dropout grace", trigger_stuck_after: "Motion sensor stuck after", presence_stuck_after: "Presence sensor stuck after", startup_hold: "Start-up hold" };
const BASIC = ["correlation_window", "inactivity_timeout", "exit_confirmation"];

function timelineList(entries) {
  if (!entries.length) return `<p class="muted">Nothing recorded yet.</p>`;
  return `<ul class="plain timeline">${entries.map((e) => {
    let text = e.text || "";
    if (e.kind === "state") text = `<strong>${esc(STATE_LABEL[e.state] || e.state)}</strong>${e.source ? ` <span class="muted">(${esc(e.source)})</span>` : ""}<div class="small muted">${esc(e.reason)}</div>`;
    else text = esc(text);
    return `<li><time>${esc(clock(e.at))}</time><div>${text}</div></li>`;
  }).join("")}</ul>`;
}
const STATE_LABEL = { vacant: "Vacant", possible_occupancy: "Possible occupancy", confirmed_occupancy: "Occupied", possible_vacancy: "Vacancy pending", uncertain: "Uncertain" };

function timingValue(seconds, isDefault) {
  return `${esc(duration(seconds))}${isDefault ? ` <span class="muted small">(default)</span>` : ` <span class="badge">room setting</span>`}`;
}

async function roomPage(areaId, token) {
  const load = async () => {
    const data = await api(`api/rooms/${enc(areaId)}`);
    if (token !== navToken) return;
    const r = data.room;
    const timing = { ...data.global_timing, ...(data.config.timing || {}) };
    const own = data.config.timing || {};
    const signals = r.signals.map((s) => `<tr><td>${esc(s.name)}<div class="mono">${esc(s.entity_id)}</div></td><td>${esc(KIND[s.kind] || s.kind)}${s.inverted ? ` <span class="badge">inverted</span>` : ""}</td><td>${esc(ROLE[s.role] || s.role)}${s.enabled ? "" : ` <span class="badge warn">excluded</span>`}</td><td class="state">${s.exists ? esc(s.ha_state) : `<span class="badge warn">missing</span>`}</td><td>${esc(s.status_label || "")}</td><td class="small muted">${s.last_changed ? `${esc(since(s.last_changed))} ago` : ""}</td></tr>`).join("");
    const activity = r.activity.length ? `<h3>Activity entities</h3><ul class="plain">${r.activity.map((a) => `<li>${esc(a.name)} <span class="mono">${esc(a.entity_id)}</span>${a.exists ? "" : ` <span class="badge warn">missing</span>`}</li>`).join("")}</ul>` : "";
    const pending = r.state === "possible_vacancy" && r.vacancy_at ? `<dt>Vacant in</dt><dd><strong>${esc(countdown(r.vacancy_at))}</strong> <span class="muted">(at ${esc(clock(r.vacancy_at))}, unless there is activity)</span></dd>` : "";
    const e = r.engine;
    const adjacent = (data.config.adjacent || []).map((a) => data.rooms[a] || a);
    renderBanners(data);
    render(`<h1>${esc(r.name)} ${chip(r.status, true)} <span class="spacer"></span>${user.is_admin ? `<a class="button" href="#/rooms/${enc(areaId)}/edit">Edit room</a>` : ""}</h1>
    <div class="two"><div>
      <div class="card"><dl class="kv">
        <dt>Now</dt><dd><strong>${esc(subline(r))}</strong>${r.since && r.enabled ? ` <span class="muted">for ${esc(since(r.since))} (since ${esc(clock(r.since))})</span>` : ""}</dd>
        <dt>Why</dt><dd>${esc(r.reason || "")}</dd>
        <dt>Confidence</dt><dd>${r.confidence == null ? "" : `${r.confidence}%`}</dd>
        <dt>Last activity</dt><dd>${r.last_activity ? `${esc(clock(r.last_activity))} <span class="muted">(${esc(since(r.last_activity))} ago${r.last_activity_source ? `, ${esc(r.last_activity_source)}` : ""})</span>` : `<span class="muted">none yet</span>`}</dd>
        ${pending}
      </dl>${r.problems.length ? `<h3>Notes</h3>${problemsList(r.problems)}` : ""}</div>
      <div class="card"><h2>Sensors</h2><div class="tscroll"><table class="sensors"><thead><tr><th>Sensor</th><th>Type</th><th>Role</th><th>State</th><th>Contribution</th><th>Last change</th></tr></thead><tbody>${signals || `<tr><td colspan="6" class="muted">No sensors</td></tr>`}</tbody></table></div>${activity}</div>
      ${data.pilot ? pilotCard(data.pilot) : ""}
      <div class="card"><h2>Recent detection</h2>${timelineList(r.timeline)}</div>
    </div><div>
      <div class="card"><h2>Timing</h2><dl class="kv">${BASIC.map((k) => `<dt>${esc(TIMING_LABEL[k])}</dt><dd>${timingValue(timing[k], own[k] === undefined)}</dd>`).join("")}</dl>
        <details data-keep="timing"><summary>Advanced timing</summary><dl class="kv">${Object.keys(TIMING_LABEL).filter((k) => !BASIC.includes(k)).map((k) => `<dt>${esc(TIMING_LABEL[k])}</dt><dd>${timing[k] === 0 && k.includes("stuck") ? "never" : timingValue(timing[k], own[k] === undefined)}</dd>`).join("")}</dl></details>
        ${adjacent.length ? `<h3>Adjacent rooms</h3><p>${esc(adjacent.join(", "))}</p>` : ""}
      </div>
      ${e ? `<div class="card"><details data-keep="diagnostics"><summary>Advanced diagnostics</summary><dl class="kv small">
        <dt>Engine state</dt><dd class="mono">${esc(e.state)} / ${esc(e.reason_code)}</dd>
        <dt>Visit confirmed</dt><dd>${e.visit_confirmed ? "yes" : "no"}</dd>
        <dt>Quiet since</dt><dd>${esc(clock(e.quiet_since) || "–")}</dd>
        <dt>Required quiet</dt><dd>${e.required_quiet == null ? "–" : esc(duration(e.required_quiet))}</dd>
        <dt>Vacancy evidence</dt><dd>${esc(e.vacancy_evidence.join("; ") || "–")}</dd>
        <dt>Confidence basis</dt><dd>${esc(e.confidence.basis)} (evidence ${esc(e.confidence.evidence_strength)}, coverage ${esc(e.confidence.sensor_coverage)})</dd>
        <dt>Adjacent movement</dt><dd>${esc(Object.entries(e.adjacent_motion).map(([a, t]) => `${data.rooms[a] || a} ${clock(t)}`).join(", ") || "–")}</dd>
      </dl></details></div>` : ""}
    </div></div>`);
  };
  if (!user.is_admin) { try { user = (await api("api/overview")).user; } catch { /* keep */ } }
  await load();
  setPoll(load, 2000);
}

function pilotCard(p) {
  const recent = p.recent.slice(0, 6).map((d) => `<li><time>${esc(clock(d.started_at))}</time><div><strong>${d.ours.occupied ? "Occupied" : "Vacant"}</strong> here, <strong>${d.reference.occupied ? "occupied" : "vacant"}</strong> in the reference ${d.seconds != null ? `for ${esc(duration(d.seconds))}` : "(still)"}<div class="small muted">${esc(d.explanation)}</div></div></li>`).join("");
  return `<div class="card"><h2>Pilot comparison</h2><dl class="kv">
    <dt>Reference</dt><dd class="mono">${esc(p.reference)} (${esc(p.reference_state || "not found")})</dd>
    <dt>Right now</dt><dd>${p.now === "agree" ? "Same result" : p.now === "differ" ? `<strong>Different</strong>${p.pending_since ? " (shorter than a minute so far)" : ""}` : "Not comparable (one side unknown)"}</dd>
    <dt>Agreement</dt><dd>${p.agreement == null ? "–" : `${p.agreement}% of ${esc(duration(p.compared_seconds))}`} · ${p.differences} difference(s) since ${esc(clock(p.since))}</dd>
  </dl>${recent ? `<h3>Recent differences</h3><ul class="plain timeline">${recent}</ul>` : ""}</div>`;
}

// ------------------------------------------------------------------ editor
const UNITS = [["s", 1], ["min", 60], ["h", 3600]];
function durationInput(key, value, def, min = 0) {
  const unit = value === undefined ? (def % 3600 === 0 && def >= 3600 ? 3600 : def % 60 === 0 && def >= 60 ? 60 : 1)
    : (value % 3600 === 0 && value >= 3600 ? 3600 : value % 60 === 0 && value >= 60 ? 60 : 1);
  const shown = value === undefined ? "" : value / unit;
  const never = key.includes("stuck");
  const defText = never && def === 0 ? "never" : duration(def);
  return `<span class="row"><input type="number" min="${min}" step="any" id="t-${esc(key)}" data-timing="${esc(key)}" value="${esc(shown)}" placeholder="${never && def === 0 ? "never" : esc(def / unit)}" aria-label="${esc(TIMING_LABEL[key])}">
    <select data-unit="${esc(key)}" aria-label="${esc(TIMING_LABEL[key])}: unit">${UNITS.map(([u, f]) => `<option value="${f}"${f === unit ? " selected" : ""}>${u}</option>`).join("")}</select>
    <span class="muted small">${value === undefined ? `default: ${esc(defText)}` : never ? "0 = never" : ""}</span></span>`;
}

function timingFields(timing, defaults, base, keys) {
  return keys.map((k) => `<div class="field"><label for="t-${esc(k)}">${esc(defaults[k].label)}</label>${durationInput(k, timing[k], base[k], defaults[k].min)}<div class="help">${esc(defaults[k].help)}</div></div>`).join("");
}

function readTiming(container, timing) {
  const out = { ...timing };
  container.querySelectorAll("input[data-timing]").forEach((input) => {
    const key = input.dataset.timing;
    const factor = Number(container.querySelector(`select[data-unit="${key}"]`).value);
    if (input.value === "") delete out[key];
    else out[key] = Math.round(Number(input.value) * factor);
  });
  return out;
}

const FIELD = { enabled: "detection on/off", sensors: "sensors", activity: "activity entities", timing: "timing", adjacent: "adjacent rooms", name: "name" };
function where(path) {
  if (!path) return "";
  const m = path.match(/^rooms\.[^.]+\.(sensors|activity|adjacent)\[(\d+)\](?:\.(\w+))?/);
  if (m) return `${{ sensors: "Sensor", activity: "Activity entity", adjacent: "Adjacent room" }[m[1]]} ${Number(m[2]) + 1}${m[3] ? ` (${m[3].replace("_", " ")})` : ""}: `;
  const t = path.match(/timing\.(\w+)$/);
  if (t) return `${TIMING_LABEL[t[1]] || t[1]}: `;
  const r = path.match(/^rooms\.[^.]+\.(\w+)$/);
  if (r) return `${FIELD[r[1]] || r[1]}: `;
  return "";
}

function reviewCard(review, { saveLabel, onSave, extra = "" }) {
  const impact = Object.values(review.impact || {}).map((i) => {
    let text;
    if (!i.before) text = `<strong>${esc(i.name)}</strong> starts as ${esc(i.after.label)}.`;
    else if (i.changes_now) text = `<strong>${esc(i.name)}</strong> changes immediately: ${esc(i.before.label)} → <strong>${esc(i.after.label)}</strong>.`;
    else text = `<strong>${esc(i.name)}</strong> stays ${esc(i.after.label)}: no immediate change.`;
    if (i.restarts) text += ` Its sensors change, so it keeps its decision and no vacancy is confirmed during the start-up hold.`;
    return `<li>${text}</li>`;
  }).join("");
  const changes = (review.changes || []).map((c) => `<li>${esc({ room_added: "Room added", room_removed: "Room removed", room_changed: "Room changed", global_timing: "Global timing changed" }[c.type] || c.type)}${c.name ? `: ${esc(c.name)}` : ""}${c.fields ? ` <span class="muted">(${esc(c.fields.map((f) => FIELD[f] || f).join(", "))})</span>` : ""}${c.keys ? ` <span class="muted">(${esc(c.keys.map((k) => TIMING_LABEL[k] || k).join(", "))})</span>` : ""}</li>`).join("");
  const problems = (review.problems || []).map((p) => `<div class="problem"><span class="dot error"></span><span>${esc(where(p.path))}${esc(p.message)}</span></div>`).join("");
  const warnings = (review.warnings || []).map((p) => `<div class="problem"><span class="dot warning"></span><span>${esc(p.message)}</span></div>`).join("");
  const el = document.createElement("div");
  el.className = "card review";
  el.innerHTML = `<h2>Review</h2>${problems ? `<p><strong>This cannot be saved yet:</strong></p>${problems}` : ""}${warnings}
    ${changes ? `<h3>Changes</h3><ul>${changes}</ul>` : review.valid ? `<p class="muted">Nothing changes.</p>` : ""}
    ${impact ? `<h3>Effect on occupancy</h3><ul>${impact}</ul>` : review.impact_unknown && review.valid ? `<p class="notice warning">The effect on occupancy cannot be predicted while Home Assistant is not connected.</p>` : ""}${extra}
    <div class="review-error"></div>
    <div class="actions">${review.valid && (review.changes || []).length ? `<button class="primary" data-act="save">${esc(saveLabel)}</button>` : ""}</div>`;
  const button = el.querySelector("[data-act=save]");
  if (button) button.addEventListener("click", async () => {
    button.disabled = true;
    const slot = el.querySelector(".review-error");
    try { await onSave(); } catch (err) {
      if (err.status === 409) {
        button.remove();
        slot.innerHTML = `<div class="notice warning">The configuration was changed meanwhile (another window or an import). Nothing was saved: review your change again.</div>`;
      } else {
        button.disabled = false;
        slot.innerHTML = `<div class="notice error">${esc(errorText(err))}</div>`;
      }
    }
  });
  return el;
}

function errorText(err) {
  const problems = err.data && err.data.problems && err.data.problems.length ? `: ${err.data.problems.map((p) => p.message).join("; ")}` : "";
  return `${err.message}${problems}`;
}
function showError(err) { toast(errorText(err)); }

// Changes are applied as they are made: validated and saved one after the other, the result shown in a status line.
function impactText(review) {
  const items = Object.values(review.impact || {}).map((i) => {
    if (!i.before) return `${i.name} starts as ${i.after.label}`;
    if (i.changes_now) return `${i.name} changes now: ${i.before.label} → ${i.after.label}`;
    return `${i.name} stays ${i.after.label}${i.restarts ? " (sensors changed: start-up hold)" : ""}`;
  });
  return items.join("; ");
}

// ``base`` is the revision the page was loaded with: a save is checked against it, so a change made meanwhile in
// another window (or by another administrator) is refused (409) instead of overwritten.
function autosaver(statusId, { review, save, saved, base }) {
  let revision = base;
  let chain = Promise.resolve();
  let pending = 0;
  const status = (html, cls = "") => {
    const el = document.getElementById(statusId);
    if (el) { el.className = `save-status ${cls}`; el.innerHTML = html; }
  };
  return () => {
    pending += 1;
    const mine = pending;
    status("Saving…");
    chain = chain.then(async () => {
      if (mine !== pending) return;  // a newer change follows: save that one
      try {
        const r = await review();
        if (!r.valid) {
          status(`<strong>Not saved:</strong> ${(r.problems || []).map((p) => esc(where(p.path) + p.message)).join("; ")}`, "bad");
          return;
        }
        if (!(r.changes || []).length) { status("All changes saved.", "ok"); return; }
        const result = await save(revision === undefined ? r.revision : revision);
        if (result && result.revision) revision = result.revision;
        const effect = impactText(r);
        const warnings = (r.warnings || []).map((w) => esc(w.message)).join(" ");
        status(`<strong>Saved.</strong> ${esc(effect)}${warnings ? `<div class="small">${warnings}</div>` : ""}`, "ok");
        if (saved) saved(result, r);
      } catch (err) {
        status(err.status === 409 ? "Changed in another window meanwhile: reload the page." : `<strong>Not saved:</strong> ${esc(errorText(err))}`, "bad");
      }
    });
  };
}

async function editorPage(areaId, token) {
  const meta = await api("api/areas");
  if (token !== navToken) return;
  const defaults = meta.defaults;
  let isNew = !areaId;
  let revision = meta.revision;
  let room = { enabled: true, sensors: [], activity: [], timing: {}, adjacent: [] };
  let name = "";
  if (!isNew) {
    const data = await api(`api/rooms/${enc(areaId)}`);
    room = structuredClone(data.config);
    revision = data.revision;
    name = data.room.name;
  }
  let candidates = [];
  let searchResults = [];
  let searchFor = null;
  let reviewEl = null;
  const kindDefaults = Object.fromEntries(defaults.kinds.map((k) => [k.value, k]));

  async function chooseArea(id) {
    collect();
    areaId = id;
    name = (meta.areas.find((a) => a.area_id === id) || {}).name || id;
    candidates = id ? (await api(`api/discover/${enc(id)}`)).candidates : [];
    if (token !== navToken) return;
    draw();
  }

  function sensorRow(s, i) {
    const live = candidates.find((c) => c.entity_id === s.entity_id) || searchResults.find((c) => c.entity_id === s.entity_id) || {};
    const kindOptions = defaults.kinds.filter((k) => !k.domains || k.domains.includes(s.entity_id.split(".")[0]) || k.value === s.kind);
    return `<div class="editor-sensor" data-i="${i}">
      <input type="checkbox" data-f="enabled" ${s.enabled !== false ? "checked" : ""} title="Use this sensor" aria-label="Use ${esc(s.entity_id)}">
      <div><div>${esc(live.name || s.name || s.entity_id)}</div><div class="mono">${esc(s.entity_id)}${live.state ? ` · ${esc(live.state)}` : ""}</div>${s.enabled === false ? `<span class="badge warn">excluded: kept, not used</span>` : ""}${s.kind === "other" && s.role === "ignore" ? `<span class="badge warn">choose its type and role</span>` : ""}</div>
      <select data-f="kind" aria-label="Sensor type">${kindOptions.map((k) => `<option value="${esc(k.value)}"${k.value === s.kind ? " selected" : ""}>${esc(k.label)}</option>`).join("")}</select>
      <select data-f="role" aria-label="Role">${defaults.roles.map((r) => `<option value="${esc(r.value)}"${r.value === s.role ? " selected" : ""}>${esc(r.label)}</option>`).join("")}</select>
      <label class="small"><input type="checkbox" data-f="inverted" ${s.inverted ? "checked" : ""}> Inverted</label>
      <button class="link" data-f="remove" title="Remove from this room" aria-label="Remove ${esc(s.entity_id)}">Remove</button></div>`;
  }

  function draw() {
    reviewEl = null;
    const configured = new Set(room.sensors.map((s) => s.entity_id));
    const suggestions = candidates.filter((c) => !configured.has(c.entity_id));
    const others = Object.entries(meta.rooms).filter(([a]) => a !== areaId);
    const areaSelect = isNew ? `<div class="card"><h2>1. Area</h2><p class="muted">A room is a Home Assistant area.</p>
      <select id="area" aria-label="Area"><option value="">Choose an area…</option>${meta.areas.filter((a) => !a.configured).map((a) => `<option value="${esc(a.area_id)}"${a.area_id === areaId ? " selected" : ""}>${esc(a.name)}</option>`).join("")}</select>
      ${meta.areas.filter((a) => !a.configured).length ? "" : `<p class="muted">Every area already has a room. Create a new area in Home Assistant first.</p>`}</div>` : "";
    const body = !areaId ? "" : `
      <div class="card"><h2>${isNew ? "2. " : ""}Sensors</h2>
        <p class="muted small">Triggers start occupancy (motion); maintains keep it while active (radar, PC, bed); door contacts count as movement and exit evidence. Inverted: the sensor means "active" when it is off.</p>
        <div id="sensors">${room.sensors.length ? room.sensors.map(sensorRow).join("") : `<p class="muted">No sensors selected yet.</p>`}</div>
        ${suggestions.length ? `<h3>Found in this area</h3><p class="muted small">Suggestions only: add the ones that should take part.</p><div class="results">${suggestions.map((c) => `<div><span class="spacer">${esc(c.name)} <span class="mono">${esc(c.entity_id)}${c.state ? ` · ${esc(c.state)}` : ""}</span> ${c.suggested_kind ? `<span class="badge">${esc(kindDefaults[c.suggested_kind].label)}</span>` : `<span class="badge warn">${c.informational ? "window/opening" : "unknown type"}</span>`}</span><button data-add="${esc(c.entity_id)}">Add</button></div>`).join("")}</div>
        ${suggestions.some((c) => c.suggested_kind) ? `<div class="actions"><button id="add-suggested">Add all suggested</button></div>` : ""}` : ""}
        <h3>Add another sensor</h3><p class="muted small">Any area: a PC activity entity, a bed sensor, a sensor shared with another room…</p>
        <input type="search" id="search-sensor" aria-label="Search a sensor" placeholder="Search by name or entity id">
        <div id="search-results-sensor"></div>
      </div>
      <div class="card"><h2>${isNew ? "3. " : ""}Timing</h2><p class="muted small">Empty fields use the global default (Settings).</p>
        <div id="timing-basic">${timingFields(room.timing, defaults.timing, meta.global_timing, BASIC)}</div>
        <details id="timing-advanced-details"><summary>Advanced</summary><div id="timing-advanced">${timingFields(room.timing, defaults.timing, meta.global_timing, Object.keys(defaults.timing).filter((k) => !BASIC.includes(k)))}</div>
          <h3>Adjacent rooms</h3><p class="muted small">Movement in an adjacent room right after a door movement here counts as evidence that someone left (faster vacancy).</p>
          ${others.length ? others.map(([a, n]) => `<label class="small"><input type="checkbox" data-adjacent="${esc(a)}" ${room.adjacent.includes(a) ? "checked" : ""}> ${esc(n)}</label>`).join(" ") : `<p class="muted small">No other rooms yet.</p>`}
          <h3>Activity entities</h3><p class="muted small">A change of these (a wall switch, a remote, a button) refreshes an occupied room's vacancy timer. They never make a room occupied.</p>
          <ul class="plain">${room.activity.map((e, i) => `<li class="row"><span class="mono spacer">${esc(e)}</span><button class="link" data-remove-activity="${i}">Remove</button></li>`).join("")}</ul>
          <input type="search" id="search-activity" aria-label="Search an activity entity" placeholder="Search an entity to add"><div id="search-results-activity"></div>
        </details>
      </div>
      ${isNew ? "" : `<div class="card"><h2>Detection</h2><label><input type="checkbox" id="room-enabled" ${room.enabled ? "checked" : ""}> Detect occupancy in this room</label><p class="muted small">When off, the room's entities stay in Home Assistant but are unavailable.</p></div>`}
      <div id="review-slot"></div>
      <div class="actions sticky-actions"><div id="save-status" class="save-status" role="status">${isNew ? "Changes are saved as you make them. Add a sensor that can detect presence to create the room." : "Changes are saved as you make them."}</div><span class="spacer"></span><a class="button primary" href="#/rooms/${enc(areaId)}">Done</a>${isNew ? "" : `<button class="danger" id="delete">Delete room</button>`}</div>`;
    view.innerHTML = `<h1>${isNew ? "Add a room" : `Edit ${esc(name)}`}</h1>${areaSelect}${body}`;
    bind();
  }

  const autosave = autosaver("save-status", {
    base: revision,
    review: () => api("api/rooms/review", { area_id: areaId, room }),
    save: (revision) => api("api/rooms/save", { area_id: areaId, room, expected_revision: revision }),
    saved: () => {
      if (isNew) {  // from now on this is the room's editor
        isNew = false;
        history.replaceState(null, "", `#/rooms/${enc(areaId)}/edit`);
        meta.rooms[areaId] = name;
      }
    },
  });

  function collect() {
    const basic = document.getElementById("timing-basic");
    const advanced = document.getElementById("timing-advanced");
    if (basic) room.timing = readTiming(basic, room.timing);
    if (advanced) room.timing = readTiming(advanced, room.timing);
    const enabled = document.getElementById("room-enabled");
    if (enabled) room.enabled = enabled.checked;
    room.adjacent = [...view.querySelectorAll("[data-adjacent]")].filter((c) => c.checked).map((c) => c.dataset.adjacent);
    return room;
  }

  function addSensor(entityId, info) {
    if (room.sensors.some((s) => s.entity_id === entityId)) return;
    const domain = entityId.split(".")[0];
    // Without a suggestion nothing is guessed: "Other" is ignored until the user chooses a type and a role.
    const kind = (info && info.suggested_kind) || "other";
    void domain;
    room.sensors.push({ entity_id: entityId, kind, role: kindDefaults[kind].default_role, inverted: false, enabled: true });
  }

  async function search(kind, query) {
    let box = document.getElementById(`search-results-${kind}`);
    if (!box) return;
    if (!query || query.length < 2) { box.innerHTML = ""; return; }
    const data = await api(`api/entities?q=${enc(query)}${kind === "activity" ? "&for=activity" : ""}`);
    if (token !== navToken || !document.getElementById(`search-results-${kind}`)) return;
    searchResults = data.results;
    searchFor = kind;
    box = document.getElementById(`search-results-${kind}`);
    box.innerHTML = data.results.length ? `<div class="results">${data.results.map((c) => `<div><span class="spacer">${esc(c.name)} <span class="mono">${esc(c.entity_id)}${c.area ? ` · ${esc(c.area)}` : ""}</span></span><button data-pick="${esc(c.entity_id)}" data-for="${kind}">Add</button></div>`).join("")}</div>` : `<p class="muted small">Nothing found.</p>`;
    box.querySelectorAll("[data-pick]").forEach((b) => b.addEventListener("click", () => {
      collect();
      const info = searchResults.find((c) => c.entity_id === b.dataset.pick);
      if (b.dataset.for === "activity") { if (!room.activity.includes(b.dataset.pick)) room.activity.push(b.dataset.pick); }
      else addSensor(b.dataset.pick, info);
      const open = document.getElementById("timing-advanced-details")?.open;
      draw();
      if (open) document.getElementById("timing-advanced-details").open = true;
      autosave();
    }));
  }

  function bind() {
    const area = document.getElementById("area");
    if (area) area.addEventListener("change", () => chooseArea(area.value).catch(showError));
    view.querySelectorAll(".editor-sensor").forEach((row) => {
      const s = room.sensors[Number(row.dataset.i)];
      row.querySelectorAll("[data-f]").forEach((el) => el.addEventListener(el.tagName === "BUTTON" ? "click" : "change", () => {
        const f = el.dataset.f;
        collect();
        if (f === "remove") room.sensors.splice(Number(row.dataset.i), 1);
        else if (f === "enabled" || f === "inverted") s[f] = el.checked;
        else if (f === "kind") { s.kind = el.value; s.role = kindDefaults[el.value].default_role; }
        else s[f] = el.value;
        draw();
        autosave();
      }));
    });
    view.querySelectorAll("[data-add]").forEach((b) => b.addEventListener("click", () => { collect(); addSensor(b.dataset.add, candidates.find((c) => c.entity_id === b.dataset.add)); draw(); autosave(); }));
    document.getElementById("add-suggested")?.addEventListener("click", () => { collect(); candidates.filter((c) => c.suggested_kind).forEach((c) => addSensor(c.entity_id, c)); draw(); autosave(); });
    view.querySelectorAll("[data-remove-activity]").forEach((b) => b.addEventListener("click", () => { collect(); room.activity.splice(Number(b.dataset.removeActivity), 1); draw(); document.getElementById("timing-advanced-details").open = true; autosave(); }));
    for (const kind of ["sensor", "activity"]) {
      const input = document.getElementById(`search-${kind}`);
      if (input) { let t; input.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => search(kind, input.value.trim()).catch(showError), 250); }); }
    }
    view.querySelectorAll("input[data-timing], select[data-unit], [data-adjacent], #room-enabled").forEach((el) => el.addEventListener("change", () => { collect(); autosave(); }));
    document.getElementById("delete")?.addEventListener("click", async () => {
      try {
        const review = await api("api/rooms/delete/review", { area_id: areaId });
        const slot = document.getElementById("review-slot");
        slot.innerHTML = "";
        slot.appendChild(reviewCard(review, { saveLabel: "Delete room", extra: `<p class="notice warning">Deleting the room removes its occupancy entities from Home Assistant. Automations that use them stop working.</p>`, onSave: async () => {
          await api("api/rooms/delete", { area_id: areaId, expected_revision: review.revision });
          toast("Room deleted");
          location.hash = "#/rooms";
        } }));
        slot.scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (err) { showError(err); }
    });
  }

  if (!isNew) candidates = (await api(`api/discover/${enc(areaId)}`)).candidates;
  if (token !== navToken) return;
  draw();
}

// ------------------------------------------------------------------ settings
function connectionCard(c) {
  const ha = c.home_assistant || {};
  const link = c.companion || {};
  const versions = c.versions || {};
  const view = link.companion_view || {};
  const state = { publishing: "Publishing", not_installed: "Integration not installed", not_set_up: "Integration not set up", not_paired: "Not paired", pair_mismatch: "Paired with another App installation", busy: "Another App session publishes", blocked: "Blocked", waiting: "Waiting", error: "Error", incompatible: "Incompatible versions" }[link.state] || link.state;
  let actions = "";
  if (user.is_admin) {
    if (["not_paired"].includes(link.state)) actions = `<button class="primary" id="pair">Pair with Home Assistant</button>`;
    else if (link.state === "pair_mismatch") actions = `<button class="primary" id="pair-replace">Pair this App (replaces the other)</button>`;
    else if (link.has_token) actions = `<button id="unpair">Unpair</button>`;
    if (link.has_token && !link.trusted && link.state !== "publishing") actions += ` <button id="forget">Forget pairing</button>`;
  }
  const compat = link.compatibility || {};
  const errors = (link.errors || []).slice(0, 8).concat((ha.history || []).filter((h) => h.error).slice(0, 4).map((h) => ({ at: h.at, code: "home_assistant", message: h.error })));
  return `<div class="card"><h2>Connection</h2><dl class="kv">
    <dt>Versions</dt><dd>App ${esc(versions.app)} · integration ${esc(versions.companion || "not found")} · engine ${esc(versions.engine)}</dd>
    ${compat.message ? `<dt>Compatibility</dt><dd><span class="badge ${compat.level === "ok" ? "" : "warn"}">${esc(compat.level)}</span> ${esc(compat.message)}</dd>` : ""}
    <dt>Home Assistant</dt><dd>${ha.connected ? `Connected (${esc(ha.ha_version)})` : `<strong>Not connected</strong> ${esc(ha.last_error || "")}`}</dd>
    <dt>Occupancy entities</dt><dd><strong>${esc(state)}</strong>${link.detail ? `<div class="small muted">${esc(link.detail)}</div>` : ""}</dd>
    <dt>Data in Home Assistant</dt><dd>${c.fresh ? `<span class="status occupied">Fresh</span>` : `<span class="status uncertain">Not fresh</span> <span class="small muted">the entities keep their last state</span>`}${view.fresh !== undefined && view.fresh !== null ? ` <span class="small muted">(the integration reports ${view.fresh ? "fresh" : "stale"})</span>` : ""}</dd>
    <dt>Last successful communication</dt><dd>${link.last_ok_at ? `${esc(clock(link.last_ok_at))} <span class="muted small">(${esc(since(link.last_ok_at))} ago)</span>` : "–"}</dd>
    ${link.last_publish_at ? `<dt>Last published change</dt><dd>${esc(clock(link.last_publish_at))}</dd>` : ""}
  </dl>${["not_installed", "not_set_up"].includes(link.state) ? `<p class="small">Install the <strong>Smart Occupancy Detection</strong> integration with HACS: HACS → ⋮ → Custom repositories → <span class="mono">https://github.com/havanissa/smart-occupancy-detection</span> (type Integration) → download, restart Home Assistant, then Settings → Devices &amp; services → Add integration → Smart Occupancy Detection. See the documentation.</p>` : ""}
  ${errors.length ? `<details data-keep="errors"><summary>Recent connection errors (${errors.length})</summary><ul class="plain small">${errors.map((e) => `<li><time>${esc(clock(e.at))}</time> ${esc(e.code)}: ${esc(e.message)}</li>`).join("")}</ul></details>` : ""}
  <div class="actions">${actions}</div></div>`;
}

async function loadPilot(admin, token) {
  const data = await api("api/pilot");
  const box = document.getElementById("pilot-rooms");
  if (!box || token !== navToken) return;
  const rows = Object.entries(data.rooms);
  if (!rows.length) { box.innerHTML = `<p class="muted small">No rooms yet.</p>`; return; }
  box.innerHTML = rows.map(([a, r]) => `<div class="field"><label for="pilot-${esc(a)}">${esc(r.name)}</label>
    <span class="row"><input type="text" id="pilot-${esc(a)}" data-pilot="${esc(a)}" value="${esc(r.reference || "")}" placeholder="${esc(r.suggestion || "binary_sensor.…")}" ${admin ? "" : "disabled"} aria-label="Reference for ${esc(r.name)}">
    ${admin && r.suggestion && r.suggestion !== r.reference ? `<button class="link" data-pilot-use="${esc(a)}" data-entity="${esc(r.suggestion)}">Use Smart Comfort's</button>` : ""}
    ${r.summary ? `<span class="small muted">${r.summary.agreement == null ? "comparing…" : `${r.summary.agreement}% same`} · ${r.summary.differences} difference(s)</span>` : ""}</span></div>`).join("") + `<div id="pilot-status" class="save-status" role="status"></div>`;
  const set = async (area, entity) => {
    const status = document.getElementById("pilot-status");
    try { await api("api/pilot/reference", { area_id: area, entity_id: entity || null }); status.className = "save-status ok"; status.textContent = "Saved."; loadPilot(admin, token); }
    catch (err) { status.className = "save-status bad"; status.textContent = `Not saved: ${errorText(err)}`; }
  };
  box.querySelectorAll("[data-pilot]").forEach((input) => input.addEventListener("change", () => set(input.dataset.pilot, input.value.trim())));
  box.querySelectorAll("[data-pilot-use]").forEach((b) => b.addEventListener("click", () => set(b.dataset.pilotUse, b.dataset.entity)));
}

function readFile(input) {
  return new Promise((resolve, reject) => {
    const file = input.files[0];
    if (!file) return reject(new Error("Choose a file first"));
    const reader = new FileReader();
    reader.onload = () => { try { resolve(JSON.parse(reader.result)); } catch { reject(new Error("The file is not JSON")); } };
    reader.onerror = () => reject(new Error("The file cannot be read"));
    reader.readAsText(file);
  });
}

async function settingsPage(token) {
  const data = await api("api/settings");
  if (token !== navToken) return;
  if (!user.is_admin) { try { user = (await api("api/overview")).user; } catch { /* keep */ } }
  let timing = { ...data.timing };
  const defaults = data.defaults.timing;
  const base = Object.fromEntries(Object.entries(defaults).map(([k, v]) => [k, v.default]));
  const rt = data.runtime || {};
  const admin = user.is_admin;
  view.innerHTML = `<h1>Settings</h1><div class="two"><div>
    <div id="connection">${connectionCard(data.connection)}</div>
    <div class="card"><h2>Detection timing</h2><p class="muted small">Defaults for every room; a room can override them in its own settings. Empty fields use the built-in default.</p>
      <div id="g-basic">${timingFields(timing, defaults, base, BASIC)}</div>
      <details><summary>Advanced</summary><div id="g-advanced">${timingFields(timing, defaults, base, Object.keys(defaults).filter((k) => !BASIC.includes(k)))}</div></details>
      ${admin ? `<div id="timing-status" class="save-status" role="status">Changes are saved as you make them.</div>` : ""}</div>
  </div><div>
    <div class="card"><h2>Backups and transfer</h2>
      <div class="actions"><a class="button" href="api/export" download>Export configuration</a></div>
      ${admin ? `<h3>Import an export</h3><input type="file" id="import-file" accept="application/json,.json"> <button id="import-review">Review import</button><div id="import-slot"></div>
      <h3>Import from Smart Comfort</h3><p class="muted small">Upload an export from Smart Comfort (the configuration export, a rooms backup or a migration backup). Only rooms, sensors, roles, timing and adjacent rooms are taken; Smart Comfort is not changed.</p>
      <input type="file" id="sc-file" accept="application/json,.json"> <button id="sc-review">Review</button><div id="sc-slot"></div>` : ""}
      <h3>Automatic backups</h3><p class="muted small">A backup is made before every change (the last 30 are kept).</p>
      ${data.backups.length ? `<ul class="plain">${data.backups.slice(0, 10).map((b) => `<li class="row"><span class="spacer">${esc(clock(b.backed_up_at) || b.name)} <span class="muted small">${b.rooms ?? "?"} rooms</span></span>${admin && !b.damaged ? `<button class="link" data-restore="${esc(b.name)}">Restore…</button>` : ""}</li>`).join("")}</ul><div id="restore-slot"></div>` : `<p class="muted small">No backups yet.</p>`}
    </div>
    <div class="card"><h2>Diagnostics</h2><dl class="kv small">
      <dt>Engine</dt><dd>${esc(rt.engine_version)} · self-test ${rt.self_test && rt.self_test.passed ? `passed (${rt.self_test.scenarios} scenarios)` : "<strong>failed</strong>"}</dd>
      <dt>Running since</dt><dd>${esc(clock(rt.started_at))}</dd>
      <dt>Events / evaluations</dt><dd>${rt.counters ? `${rt.counters.events} / ${rt.counters.evaluations}` : ""}</dd>
      <dt>Configuration</dt><dd class="mono">${esc(data.revision)}${data.saved_at ? ` · saved ${esc(clock(data.saved_at))}` : ""}</dd>
      ${data.recovery ? `<dt>Recovery</dt><dd>${esc(data.recovery.used ? `used ${data.recovery.used}` : "no usable file")}: ${esc((data.recovery.errors || []).join("; "))}</dd>` : ""}
    </dl><div class="actions"><a class="button" href="api/diagnostics" download>Download diagnostics</a></div><p class="small muted">Secrets, user ids, addresses and people are removed from the file.</p></div>
    <div class="card" id="pilot-card"><h2>Pilot comparison</h2><p class="muted small">Compare a room with another occupancy output (Smart Comfort's, normally) and log every difference longer than a minute, with the sensor evidence. Nothing is adjusted.</p><div id="pilot-rooms"><p class="muted">Loading…</p></div>
      <div class="actions"><a class="button" href="api/pilot/report.csv" download>Download differences (CSV)</a><a class="button" href="api/pilot/report.json" download>JSON</a></div></div>
  </div></div>`;
  loadPilot(admin, token);

  const bindConnection = () => {
    const pair = async (replace) => {
      try {
        const result = await api("api/companion/pair", { replace });
        toast(result.state === "publishing" ? "Paired: the occupancy entities are published" : "Paired; publishing has not started yet (see Connection)");
      }
      catch (err) {
        if (err.data && err.data.error === "already_paired" && confirm("The integration is paired with another App installation. Pair this one instead? The other stops publishing.")) return pair(true);
        showError(err);
      }
      if (token === navToken) settingsPage(token);
    };
    document.getElementById("pair")?.addEventListener("click", () => pair(false));
    document.getElementById("pair-replace")?.addEventListener("click", () => { if (confirm("Pair this App? The other App installation stops publishing.")) pair(true); });
    document.getElementById("forget")?.addEventListener("click", async () => {
      if (!confirm("Forget this App's pairing? It will not pair again by itself; press Pair to pair it again.")) return;
      try { await api("api/companion/unpair", {}); toast("Pairing forgotten"); } catch (err) { showError(err); }
      if (token === navToken) settingsPage(token);
    });
    document.getElementById("unpair")?.addEventListener("click", async () => {
      if (!confirm("Unpair? The occupancy entities keep their last state and show that they are not fresh until the App is paired again.")) return;
      try { await api("api/companion/unpair", {}); toast("Unpaired"); } catch (err) { showError(err); }
      if (token === navToken) settingsPage(token);
    });
  };
  bindConnection();
  setPoll(async () => {
    const fresh = await api("api/settings");
    if (token !== navToken) return;
    renderBanners(fresh);
    document.getElementById("connection").innerHTML = connectionCard(fresh.connection);
    bindConnection();
  }, 5000);

  const readGlobal = () => {
    timing = readTiming(document.getElementById("g-basic"), timing);
    timing = readTiming(document.getElementById("g-advanced"), timing);
    return { ...base, ...timing };
  };
  const saveTiming = autosaver("timing-status", {
    base: data.revision,
    review: () => api("api/timing/review", { timing: readGlobal() }),
    save: (revision) => api("api/timing/save", { timing: readGlobal(), expected_revision: revision }),
  });
  if (admin) view.querySelectorAll("#g-basic input, #g-basic select, #g-advanced input, #g-advanced select").forEach((el) => el.addEventListener("change", saveTiming));

  document.getElementById("import-review")?.addEventListener("click", async () => {
    try {
      const document_ = await readFile(document.getElementById("import-file"));
      const review = await api("api/import/review", { document: document_ });
      const slot = document.getElementById("import-slot");
      slot.innerHTML = "";
      slot.appendChild(reviewCard(review, { saveLabel: "Import (replaces the configuration)", onSave: async () => {
        await api("api/import/apply", { document: document_, expected_revision: review.revision });
        toast("Configuration imported");
        if (token === navToken) settingsPage(token);
      } }));
    } catch (err) { showError(err); }
  });

  view.querySelectorAll("[data-restore]").forEach((b) => b.addEventListener("click", async () => {
    try {
      const review = await api("api/backups/review", { name: b.dataset.restore });
      const slot = document.getElementById("restore-slot");
      slot.innerHTML = "";
      slot.appendChild(reviewCard(review, { saveLabel: "Restore this backup", onSave: async () => {
        await api("api/backups/restore", { name: b.dataset.restore, expected_revision: review.revision });
        toast("Backup restored");
        if (token === navToken) settingsPage(token);
      } }));
    } catch (err) { showError(err); }
  }));

  document.getElementById("sc-review")?.addEventListener("click", async () => {
    const slot = document.getElementById("sc-slot");
    try {
      const document_ = await readFile(document.getElementById("sc-file"));
      const review = await api("api/smart-comfort/review", { document: document_ });
      if (!review.rooms.length) { slot.innerHTML = `<p class="muted">The export contains no rooms with occupancy sensors.</p>`; return; }
      slot.innerHTML = `<div class="card review"><h2>Rooms in the export</h2>${review.rooms.map((r) => `<div class="field"><label><input type="checkbox" data-sc="${esc(r.area_id)}" ${r.area_exists ? "checked" : ""}> ${esc(r.name)}</label>
        <div class="small">${r.room.sensors.length} sensor(s): ${esc(r.room.sensors.map((s) => `${s.entity_id} (${KIND[s.kind] || s.kind}, ${ROLE[s.role] || s.role}${s.inverted ? ", inverted" : ""}${s.enabled ? "" : ", excluded"})`).join("; "))}
        ${r.replaces ? `<div><span class="badge warn">replaces the room configured here</span></div>` : ""}${r.area_exists ? "" : `<div><span class="badge warn">this area does not exist in Home Assistant</span></div>`}
        ${r.missing_entities.length ? `<div class="muted">Not found in Home Assistant: ${esc(r.missing_entities.join(", "))}</div>` : ""}${r.notes.map((n) => `<div class="muted">${esc(n)}</div>`).join("")}</div></div>`).join("")}
        <label><input type="checkbox" id="sc-timing"> Also take the global timing ${review.timing_differs ? "(it differs from the current one)" : "(same as now)"}</label>
        <div class="actions"><button class="primary" id="sc-preview">Preview the import</button></div><div id="sc-preview-slot"></div></div>`;
      document.getElementById("sc-preview").addEventListener("click", async () => {
        const rooms = [...slot.querySelectorAll("[data-sc]")].filter((c) => c.checked).map((c) => c.dataset.sc);
        const includeTiming = document.getElementById("sc-timing").checked;
        try {
          const preview = await api("api/smart-comfort/preview", { document: document_, rooms, include_timing: includeTiming });
          const target = document.getElementById("sc-preview-slot");
          target.innerHTML = "";
          target.appendChild(reviewCard(preview, { saveLabel: "Import these rooms", extra: preview.notes.map((n) => `<p class="small muted">${esc(n)}</p>`).join(""), onSave: async () => {
            await api("api/smart-comfort/apply", { document: document_, rooms, include_timing: includeTiming, expected_revision: preview.revision });
            toast("Imported from Smart Comfort");
            location.hash = "#/";
          } }));
        } catch (err) { showError(err); }
      });
    } catch (err) { showError(err); }
  });
}

route();
