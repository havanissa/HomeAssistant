// Automation Builder - the pages: Overview, Devices, one device (rules, conflicts, deployment), Settings.
// Rules are saved as soon as they change. Nothing reaches Home Assistant until "Deploy" is pressed.

import { api, ask, change, chip, clock, enc, errorText, esc, icon, notes, pickEntity, renderBanners, session, toast, view } from "./ui.js";
import { editorPage } from "./editor.js";
import { blueprintPage, importListPage, importPage, moodPage } from "./pages.js";

let navToken = 0;
let page = { act: {}, chg: {} };  // the current page's handlers for [data-act] clicks and [data-chg] changes

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

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const controllable = (n) => `${n} controllable ${n === 1 ? "entity" : "entities"}`;

async function loadShell() {
  const data = await api("api/overview");
  session.user = data.user;
  renderBanners(data);
  return data;
}

// ------------------------------------------------------------------ overview
function deviceCard(d) {
  const rules = d.summaries.slice(0, 3).map((r) => `<li class="${r.enabled ? "" : "muted"}">${esc(r.text)}${r.enabled ? "" : " (disabled)"}</li>`).join("");
  const more = d.summaries.length > 3 ? `<li class="muted">+ ${d.summaries.length - 3} more</li>` : "";
  const warn = d.warnings ? `<div class="warn-line">⚠ ${plural(d.warnings, "warning")}${d.blocking ? ` (${plural(d.blocking, "conflict")} to resolve)` : ""}${d.turned_off ? " · the automation is turned off in Home Assistant" : ""}</div>` : "";
  return `<a class="card device" href="#/devices/${enc(d.id)}">
    <div class="row"><span class="name">${esc(d.name)}</span><span class="spacer"></span>${chip(d.enabled ? d.status : "nothing", d.enabled ? d.status_text : "Disabled")}</div>
    <div class="sub">Controls ${d.targets.map((x) => `${icon(x.domain)}${esc(x.name)}`).join(", ")}</div>
    <div class="sub">${d.rules_enabled} of ${plural(d.rules, "rule")} enabled · ${plural(d.automations, "automation")}${d.last_sync ? ` · deployed ${esc(clock(d.last_sync))}` : ""}</div>
    ${rules || more ? `<ul class="rules-mini">${rules}${more}${d.default ? `<li>DEFAULT: when no rule matched</li>` : ""}</ul>` : `<div class="sub">No rules yet.</div>`}${warn}</a>`;
}

function orphanList(orphans) {
  if (!orphans.length) return "";
  return `<div class="notice warning"><strong>Leftover automations.</strong> These were deployed by Automation Builder for devices that are no longer in its configuration (after an import or a restore). They still run in Home Assistant.
    <ul class="plain">${orphans.map((o) => `<li class="row"><span>${esc(o.alias || o.id)} <span class="mono">${esc(o.entity_id || o.id)}</span></span><span class="spacer"></span><button class="danger" data-act="orphan" data-id="${esc(o.id)}">Remove from Home Assistant</button></li>`).join("")}</ul></div>`;
}

async function removeOrphan(el, reload) {
  const yes = await ask({ title: "Remove this automation from Home Assistant?", body: `<p>${esc(el.dataset.id)} is deleted from Home Assistant. This cannot be undone here.</p>`, ok: "Remove", danger: true });
  if (!yes) return;
  try {
    await api("api/orphans/remove", { automation_id: el.dataset.id, overwrite: true });
    toast("Removed from Home Assistant.");
  } catch (err) { toast(errorText(err)); }
  reload();
}

async function overviewPage(token) {
  const data = await loadShell();
  if (token !== navToken) return;
  page = { act: { orphan: (el) => removeOrphan(el, route) }, chg: {} };
  if (!data.user.is_admin) { view.innerHTML = `<h1>Overview</h1>`; return; }
  if (!data.devices.length) {
    view.innerHTML = `<h1>Overview</h1>${orphanList(data.orphans)}<div class="card empty"><h2>No devices yet</h2>
      <p class="muted">Choose a device to control - a light, a switch, a cover, a thermostat, anything Home Assistant can control - and give it rules: IF something happens AND conditions hold THEN do this. All rules of one device become one Home Assistant automation.</p>
      <a class="button primary" href="#/devices/new">Add a device</a></div>`;
    return;
  }
  const t = data.totals;
  const byId = Object.fromEntries(data.devices.map((d) => [d.id, d]));
  const closed = new Set(JSON.parse(sessionStorage.getItem("ab-rooms-closed") || "[]"));
  const groups = data.rooms.map((g) => {
    const devices = g.device_ids.map((id) => byId[id]);
    const add = g.kind === "multiple" ? "" : `<a class="button" data-add-room href="#/devices/new/${enc(g.kind === "room" ? g.area_id : "unassigned")}">+ Add device</a>`;
    const hint = g.kind === "multiple" ? `<p class="muted small">These devices control entities in more than one room. Each is listed once, here, and still has one automation of its own.</p>`
      : g.kind === "unassigned" ? `<p class="muted small">Entities that have no area in Home Assistant.</p>` : "";
    return `<details class="room" data-room="${esc(g.key)}" ${closed.has(g.key) ? "" : "open"}><summary><span class="room-name">${icon(g.kind === "room" ? "room" : g.kind)}${esc(g.name)}</span>
        <span class="muted small">${plural(devices.length, "device")} · ${plural(g.rules, "rule")}${g.warnings ? ` · ${plural(g.warnings, "warning")}` : ""}</span><span class="spacer"></span>${add}</summary>
      ${hint}<div class="grid">${devices.map(deviceCard).join("")}</div></details>`;
  }).join("");
  view.innerHTML = `<h1>Overview <span class="muted small">${plural(t.devices, "device")} · ${plural(t.rules, "rule")} · ${plural(t.automations, "automation")}${t.warnings ? ` · ${plural(t.warnings, "warning")}` : ""}</span><span class="spacer"></span><a class="button primary" href="#/devices/new">Add a device</a></h1>
    ${orphanList(data.orphans)}<div id="rooms">${groups}</div>
    <p class="muted small">Rooms are Home Assistant's areas. They only organise this page: every device has its own automation, whichever room it is listed in.</p>`;
  view.querySelectorAll("details.room").forEach((el) => el.addEventListener("toggle", () => {
    const now = [...view.querySelectorAll("details.room:not([open])")].map((d) => d.dataset.room);
    sessionStorage.setItem("ab-rooms-closed", JSON.stringify(now));
  }));
}

// ------------------------------------------------------------------ devices
async function devicesPage(token) {
  const data = await loadShell();
  if (token !== navToken) return;
  page = { act: {}, chg: {} };
  const rows = data.devices.map((d) => `<tr><td><a href="#/devices/${enc(d.id)}">${esc(d.name)}</a>${d.enabled ? "" : ` <span class="badge">disabled</span>`}</td>
    <td>${esc(d.room.name)}</td>
    <td>${d.targets.map((t) => `${icon(t.domain)}${esc(t.name)} <span class="mono">${esc(t.entity_id)}</span>`).join("<br>")}</td>
    <td>${d.rules_enabled} / ${d.rules}</td><td>${chip(d.status, d.status_text)}</td>
    <td>${d.warnings ? `<span class="badge warn">${plural(d.warnings, "warning")}</span>` : ""}</td></tr>`).join("");
  view.innerHTML = `<h1>Devices <span class="spacer"></span><a class="button" href="#/import">Import an automation</a><a class="button primary" href="#/devices/new">Add a device</a></h1>
    <div class="card">${data.devices.length ? `<div class="tscroll"><table><thead><tr><th>Device</th><th>Room</th><th>Controls</th><th>Rules on</th><th>Automation</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : `<p class="muted">No devices are managed yet.</p>`}</div>
    <p class="muted small">A device here is what one automation controls: one entity, or several entities you chose to control together. A physical Home Assistant device can have several outputs; you choose exactly which ones.</p>`;
}

// Add a device in three steps: room -> device -> entities. Only the choices of one step are shown at a time;
// going back keeps what was ticked, so entities of several devices or rooms can be combined on purpose.
async function newDevicePage(token, presetArea) {
  await loadShell();
  if (token !== navToken) return;
  const chosen = new Map();  // entity_id -> entity (with the Home Assistant device it came from)
  let step = 1;
  let room = null;           // { area_id, name }
  let device = null;         // the Home Assistant device (or the "entities without a device" entry) of step 3
  let rooms = [];
  let devices = [];
  let nameEdited = false;
  let name = "";
  let seq = 0;
  const key = (r) => (r.area_id === null ? "unassigned" : r.area_id);

  const crumbs = () => `<nav class="crumbs" aria-label="Selection path">
      <button type="button" class="link" data-act="to-rooms" ${step === 1 ? "disabled" : ""}>${icon("room")}${room ? esc(room.name) : "Room"}</button><span aria-hidden="true">›</span>
      <button type="button" class="link" data-act="to-devices" ${step < 3 ? "disabled" : ""}>${icon("device")}${device ? esc(device.name) : "Device"}</button><span aria-hidden="true">›</span>
      <span class="${step === 3 ? "" : "muted"}">${icon("light")}Entities</span></nav>`;

  function stepHtml() {
    if (step === 1) {
      return `<h2>1. Which room?</h2><input type="search" id="q" data-inp="q" placeholder="Search rooms" autocomplete="off" aria-label="Search rooms">
        <div class="choices" id="rooms-list">${rooms.length ? rooms.map((r, i) => `<button type="button" class="choice" data-act="room" data-i="${i}" data-room="${esc(key(r))}">${icon(r.unassigned ? "unassigned" : "room")}
          <span><strong>${esc(r.name)}</strong><br><span class="muted small">${r.entities ? `${plural(r.devices, "device")} · ${controllable(r.entities)}` : "nothing to control here"}</span></span><span class="spacer"></span><span aria-hidden="true">›</span></button>`).join("") : `<p class="muted">No room found.</p>`}</div>
        <p class="muted small">Rooms are the areas of Home Assistant. “Unassigned” holds what has no area.</p>`;
    }
    if (step === 2) {
      return `<h2>2. Which device in ${esc(room.name)}?</h2><input type="search" id="q" data-inp="q" placeholder="Search devices in this room" autocomplete="off" aria-label="Search devices">
        <div class="choices" id="devices-list">${devices.length ? devices.map((d, i) => {
    const picked = d.entities.filter((e) => chosen.has(e.entity_id)).length;
    return `<button type="button" class="choice" data-act="device" data-i="${i}" data-device="${esc(d.device_id || "loose")}">${icon(d.device_id ? "device" : "loose")}
          <span><strong>${esc(d.name)}</strong>${picked ? ` <span class="badge">${picked} selected</span>` : ""}<br><span class="muted small">${esc(d.model || "")}${d.model ? " · " : ""}${d.domains.map((x) => icon(x)).join("")} ${controllable(d.entities.length)}</span></span><span class="spacer"></span><span aria-hidden="true">›</span></button>`;
  }).join("") : `<p class="muted">Nothing controllable found in this room.</p>`}</div>
        <div class="actions"><button type="button" data-act="to-rooms">← Rooms</button></div>`;
    }
    return `<h2>3. What should be controlled on ${esc(device.name)}?</h2>
      <div id="entities-list">${device.entities.map((e) => `<label class="pick"><input type="checkbox" data-chg="tick" data-id="${esc(e.entity_id)}" ${chosen.has(e.entity_id) ? "checked" : ""}>${icon(e.domain)}
        <span><strong>${esc(e.name)}</strong> <span class="badge">${esc(e.domain_label)}</span>${e.area && room && e.area !== room.name ? ` <span class="badge">${esc(e.area)}</span>` : ""}<br><span class="mono">${esc(e.entity_id)}</span></span><span class="spacer"></span><span class="muted small">${esc(e.state ?? "")}</span></label>`).join("")}</div>
      <p class="muted small">Nothing is ticked for you. Only what you tick is controlled, never every output of a device.</p>
      <div class="actions"><button type="button" data-act="to-devices">← Devices in ${esc(room.name)}</button></div>`;
  }

  function sideHtml() {
    const list = [...chosen.values()];
    if (!nameEdited) name = list.length === 1 ? list[0].name : (list.length && list.every((e) => e.from === list[0].from) && list[0].from) || (list[0] && list[0].name) || "";
    return `<h2>Selected</h2>${list.length ? `<ul class="plain" id="chosen">${list.map((e) => `<li class="row">${icon(e.domain)}<span>${esc(e.name)}<br><span class="mono">${esc(e.entity_id)}</span></span><span class="spacer"></span><button type="button" class="link" data-act="drop" data-id="${esc(e.entity_id)}">Remove</button></li>`).join("")}</ul>` : `<p class="muted" id="chosen">Nothing selected yet.</p>`}
      <div class="field"><label for="name">Name</label><input type="text" id="name" data-inp="name" maxlength="80" value="${esc(name)}"></div>
      <div class="actions"><button class="primary" id="create" data-act="create" ${list.length ? "" : "disabled"}>Add device</button><a class="button" href="#/">Cancel</a></div>
      <p class="muted small">Next you get an empty rule to fill in. Nothing reaches Home Assistant before you deploy.</p>`;
  }

  function draw(focus) {
    view.innerHTML = `<h1>Add a device</h1>${crumbs()}
      <div class="two"><div><div class="card" id="wizard" data-step="${step}">${stepHtml()}</div></div><div><div class="card" id="selected">${sideHtml()}</div></div></div>`;
    if (focus) { const q = view.querySelector("#q"); if (q) q.focus(); }
  }
  const redrawSide = () => { view.querySelector("#selected").innerHTML = sideHtml(); };

  async function loadRooms(q = "") {
    const mine = ++seq;
    const data = await api(`api/rooms?q=${enc(q)}`);
    if (mine !== seq || token !== navToken) return false;
    rooms = data.rooms;
    return true;
  }
  async function loadDevices(q = "") {
    const mine = ++seq;
    const data = await api(`api/room-devices?q=${enc(q)}${room.area_id === null ? "" : `&area=${enc(room.area_id)}`}`);
    if (mine !== seq || token !== navToken) return false;
    devices = data.devices;
    if (!room.name) room.name = data.name;
    return true;
  }
  const fail = (err) => { const box = view.querySelector("#wizard"); if (box) box.insertAdjacentHTML("beforeend", `<div class="notice error">${esc(errorText(err))}</div>`); };
  // Typing in a search box replaces the list only, so the box keeps the focus and the cursor.
  async function refine(value) {
    try {
      if (step === 1 && await loadRooms(value)) { const fresh = document.createElement("div"); fresh.innerHTML = stepHtml(); view.querySelector("#rooms-list").replaceWith(fresh.querySelector("#rooms-list")); }
      if (step === 2 && await loadDevices(value)) { const fresh = document.createElement("div"); fresh.innerHTML = stepHtml(); view.querySelector("#devices-list").replaceWith(fresh.querySelector("#devices-list")); }
    } catch (err) { fail(err); }
  }
  let timer = null;
  page = {
    inp: {
      q: (el) => { clearTimeout(timer); timer = setTimeout(() => { if (token === navToken) refine(el.value); }, 200); },
      name: (el) => { nameEdited = true; name = el.value; },
    },
    chg: {
      tick: (el) => {
        const entity = device.entities.find((e) => e.entity_id === el.dataset.id);
        if (el.checked) chosen.set(entity.entity_id, { ...entity, from: device.device_id ? device.name : null, ha_device_id: device.device_id });
        else chosen.delete(entity.entity_id);
        redrawSide();
      },
    },
    act: {
      room: async (el) => {
        room = rooms[Number(el.dataset.i)];
        try { if (await loadDevices()) { step = 2; draw(true); } } catch (err) { fail(err); }
      },
      device: (el) => { device = devices[Number(el.dataset.i)]; step = 3; draw(); },
      "to-rooms": async () => { try { if (await loadRooms()) { step = 1; device = null; draw(true); } } catch (err) { fail(err); } },
      "to-devices": async () => { try { if (await loadDevices()) { step = 2; draw(true); } } catch (err) { fail(err); } },
      drop: (el) => { chosen.delete(el.dataset.id); draw(); },
      create: async () => {
        const list = [...chosen.values()];
        const from = new Set(list.map((e) => e.ha_device_id || ""));
        try {
          const result = await change("api/devices/create", { name, targets: list.map((e) => e.entity_id), ha_device_id: from.size === 1 ? [...from][0] || null : null });
          location.hash = `#/rules/${enc(result.device_id)}/new`;  // straight into an empty rule
        } catch (err) { toast(errorText(err)); }
      },
    },
  };
  try {
    if (presetArea) {
      room = { area_id: presetArea === "unassigned" ? null : presetArea, name: presetArea === "unassigned" ? "Unassigned" : "" };
      if (await loadDevices()) { step = 2; draw(true); }
    } else if (await loadRooms()) draw(true);
  } catch (err) {
    if (token === navToken) { room = null; step = 1; rooms = []; draw(); fail(err); }
  }
}

// ------------------------------------------------------------------ one device
const OPERATION = { create: "Will be created", update: "Will be replaced by the new version", delete: "Will be removed from Home Assistant", none: "Already up to date", forget: "Already gone from Home Assistant" };
const EXTERNAL = {
  modified: "This automation was changed in Home Assistant outside Automation Builder since the last deployment. Deploying replaces that change.",
  foreign: "An automation with this id exists in Home Assistant, but Automation Builder has no record of deploying it. Deploying replaces it.",
  missing: "This automation was deleted in Home Assistant outside Automation Builder. Deploying creates it again.",
};

function diffHtml(diff) {
  return diff.split("\n").map((line) => {
    const cls = line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : "";
    return `<span class="${cls}">${esc(line)}</span>`;
  }).join("\n");
}

function planHtml(plan) {
  const items = plan.items.map((i) => `<div class="plan-item" data-op="${esc(i.operation)}">
      <div class="row"><strong>${esc(i.alias || i.id)}</strong><span class="badge ${i.operation === "delete" ? "warn" : ""}">${esc(OPERATION[i.operation])}</span></div>
      <div class="mono">${esc(i.id)}${i.mode ? ` · mode ${esc(i.mode)}` : ""}</div>
      ${i.reason ? `<div class="sub">${esc(i.reason)}</div>` : ""}
      ${i.external ? `<div class="notice ${i.external === "missing" ? "info" : "error"}" data-external="${esc(i.external)}">${esc(EXTERNAL[i.external])}</div>` : ""}
      ${i.errors.length ? `<div class="notice error"><strong>Home Assistant does not accept this automation:</strong> ${i.errors.map(esc).join("; ")}</div>` : ""}
      ${i.diff ? `<details data-keep="diff-${esc(i.id)}" ${i.operation === "update" ? "open" : ""}><summary>What changes in Home Assistant</summary><pre class="code diff">${diffHtml(i.diff)}</pre></details>` : ""}
      ${i.yaml ? `<details data-keep="yaml-${esc(i.id)}"><summary>Complete automation (YAML)</summary><pre class="code">${esc(i.yaml)}</pre></details>` : ""}
    </div>`).join("");
  const blocked = plan.reasons.length ? `<div class="notice error" id="plan-blocked">${plan.reasons.map(esc).join(" ")}</div>` : "";
  const duplicates = (plan.duplicates || []).map((o) => `<div class="row" data-duplicate="${esc(o.rule_id)}"><span>Original: <strong>${esc(o.alias)}</strong> <span class="mono">${esc(o.entity_id || "")}</span></span><a href="/config/automation/edit/${enc(o.automation_id)}" target="_top">Open in Home Assistant</a><button data-act="allow-both" data-id="${esc(o.rule_id)}">Allow both to run</button></div>`).join("");
  const nothing = !plan.changes ? `<div class="notice info" id="plan-nothing">Home Assistant already has exactly this. Nothing to deploy.</div>` : "";
  const overwrite = plan.needs_overwrite ? `<label class="confirm-line"><input type="checkbox" id="overwrite"> Replace the automation that was changed outside Automation Builder</label>` : "";
  return `<div class="card review" id="plan"><h2>Review before deploying</h2>
    ${items || `<p class="muted">This device has no enabled rules and nothing deployed.</p>`}
    ${notes(plan.conflicts.filter((c) => c.level === "blocking" && !c.acknowledged))}${blocked}${duplicates}${nothing}${overwrite}
    <div class="actions"><button class="primary" id="deploy" data-act="deploy" ${plan.can_deploy ? "" : "disabled"}>Deploy to Home Assistant</button><button data-act="plan-close">Close</button></div>
    <p class="muted small">Home Assistant validated this automation. Deploying writes it to automations.yaml, reloads it and confirms that Home Assistant runs it. If that fails, the previous automation is put back.</p></div>`;
}

function automationCard(a, haBase) {
  const traces = a.traces && a.traces.length ? `<details data-keep="tr-${esc(a.id)}"><summary>Last runs (${a.traces.length})</summary><div class="tscroll"><table><thead><tr><th>When</th><th>Trigger</th><th>Result</th></tr></thead><tbody>
      ${a.traces.map((t) => `<tr><td>${esc(clock(t.start))}</td><td>${esc(t.trigger || "")}</td><td>${esc(t.error || t.result || t.state || "")}</td></tr>`).join("")}</tbody></table></div></details>` : "";
  const links = a.entity_id ? `<div class="row small"><a href="/config/automation/trace/${enc(a.id)}" target="_top">Traces in Home Assistant</a><a href="/config/automation/edit/${enc(a.id)}" target="_top">Open in Home Assistant</a></div>` : "";
  return `<div class="plan-item" data-automation="${esc(a.id)}">
    <div class="row"><strong>${esc(a.alias || a.id)}</strong>${chip(a.status, a.status_text)}${a.turned_off ? `<span class="badge warn">turned off in Home Assistant</span>` : ""}${a.wanted ? "" : `<span class="badge warn">no longer needed: removed at the next deployment</span>`}</div>
    <dl class="kv small">
      <dt>Automation id</dt><dd class="mono">${esc(a.id)}</dd>
      <dt>Entity</dt><dd class="mono">${esc(a.entity_id || "not in Home Assistant")}</dd>
      ${a.mode ? `<dt>Mode</dt><dd>${esc(a.mode)} · ${plural(a.rule_ids.length, "rule")}</dd>` : ""}
      <dt>Last deployed</dt><dd>${a.deployed_at ? esc(clock(a.deployed_at)) : "never"}</dd>
      <dt>Last triggered</dt><dd>${a.last_triggered ? esc(clock(a.last_triggered)) : "not yet"}</dd>
    </dl>${a.reason ? `<div class="sub">${esc(a.reason)}</div>` : ""}${links}${traces}</div>`;
}

const OUTCOME = { finished: "ran", running: "still running", failed: "failed", blocked: "blocked", aborted: "stopped early", cancelled: "cancelled", error: "failed" };
const VERDICT = {
  confirmed: ["ok", "Reported state matches"], mismatch: ["bad", "Reported state differs"], changed_since: ["off", "Changed by something else since"],
  unavailable: ["off", "Entity not available"], not_verifiable: ["off", "Cannot be verified"], pending: ["pending", "Checking the reported state…"],
  waiting: ["pending", "Still running"], too_late: ["off", "Seen too late to verify"], nothing: ["off", "No action to verify"],
};

function ruleDiagnostics(r, d) {
  if (!d) return "";
  const trace = (ref, text) => (ref && d.automation_id ? `<a href="/config/automation/trace/${enc(ref.automation_id || d.automation_id)}?run_id=${enc(ref.run_id)}" target="_top">${esc(text)}</a>` : esc(text));
  const v = d.verification;
  const verdict = v && d.last_executed && v.run_id === d.last_executed.run_id ? VERDICT[v.state] : null;
  const line = [];
  line.push(chip(d.sync.status, d.sync.text));
  if (d.turned_off) line.push(`<span class="badge warn">automation turned off in Home Assistant</span>`);
  if (d.entity_problems) line.push(`<span class="badge warn">${plural(d.entity_problems, "entity")} missing or unavailable</span>`);
  if (d.last_outcome && d.last_outcome.outcome === "failed") line.push(`<span class="status bad">Last run failed</span>`);
  if (verdict && ["mismatch"].includes(v.state)) line.push(`<span class="status bad">${esc(verdict[1])}</span>`);
  const checks = v && v.checks && v.checks.length ? `<ul class="plain small" data-checks>${v.checks.map((c) => `<li data-verdict="${esc(c.verdict)}"><span class="status ${VERDICT[c.verdict][0]}">${esc(VERDICT[c.verdict][1])}</span> ${esc(entityLabel(d, c.entity_id))}: ${c.expected ? `expected ${esc(c.expected)}, reports ${esc(c.reported)}` : ""}${c.detail ? ` <span class="muted">(${esc(c.detail)})</span>` : ""}</li>`).join("")}</ul>` : "";
  const blockedLater = d.last_blocked && (!d.last_executed || d.last_blocked.at > d.last_executed.at);
  return `<div class="diag" data-diag="${esc(r.id)}"><div class="row small">${line.join("")}</div>
    <details data-keep="diag-${esc(r.id)}"><summary>Execution: ${d.last_triggered ? `triggered ${esc(clock(d.last_triggered.at))}` : "not triggered yet"}${d.last_executed ? ` · executed ${esc(clock(d.last_executed.at))}` : ""}${blockedLater ? " · blocked by a condition" : ""}</summary>
      <dl class="kv small">
        <dt>Last triggered</dt><dd>${d.last_triggered ? trace(d.last_triggered, clock(d.last_triggered.at)) : "not seen yet"}${d.runs ? ` <span class="muted">(${d.runs} seen)</span>` : ""}</dd>
        <dt>Last executed</dt><dd>${d.last_executed ? `${trace(d.last_executed, clock(d.last_executed.at))}${d.last_executed.recheck ? " (occupancy recheck)" : ""}` : "not seen yet"}${d.executions ? ` <span class="muted">(${d.executions} seen)</span>` : ""}</dd>
        <dt>Last blocked by conditions</dt><dd>${d.last_blocked ? `${trace(d.last_blocked, clock(d.last_blocked.at))}: ${esc(d.last_blocked.by)}` : "not seen yet"}</dd>
        <dt>Last outcome</dt><dd data-outcome="${esc((d.last_outcome || {}).outcome || "")}">${d.last_outcome ? `${esc(OUTCOME[d.last_outcome.outcome] || d.last_outcome.outcome)}${d.last_outcome.error ? `: ${esc(d.last_outcome.error)}` : ""}` : "–"}</dd>
        ${d.last_recheck ? `<dt>Last occupancy recheck</dt><dd>${esc(clock(d.last_recheck.at))}: ${d.last_recheck.executed ? "repeated the actions" : `nothing to do (${esc(d.last_recheck.skipped_because || "everything as expected")})`}</dd>` : ""}
        <dt>Action verification</dt><dd data-verification="${esc(verdict ? v.state : "")}">${verdict ? `<span class="status ${verdict[0]}">${esc(verdict[1])}</span>` : "–"}</dd>
        <dt>Entities</dt><dd>${d.entities.map((e) => `<span class="ent ${e.status}">${esc(e.name)}${e.status === "ok" ? "" : ` (${esc(e.status)})`}</span>`).join(", ")}</dd>
      </dl>${checks}
      <p class="muted small">Read from Home Assistant's traces. “Executed” means Home Assistant accepted the actions; the verification compares what the entity <em>reports</em> a few seconds later, which is not proof of what a physical device did.</p></details></div>`;
}
// The optional fallback of a device, below its rules.
function defaultHtml(d, summary) {
  const rule = d.default_rule;
  if (!rule) {
    return `<div class="rule default" id="default-rule"><div class="row"><span class="name">Default</span><span class="badge">optional</span><span class="spacer"></span><a class="button" href="#/default/${enc(d.id)}">Add a default</a></div>
      <p class="muted small">What this device does when one of its rules' triggers fired and none of the rules matched. Nothing is configured.</p></div>`;
  }
  return `<div class="rule default ${rule.enabled ? "" : "disabled"}" id="default-rule"><div class="row"><label class="switch"><input type="checkbox" data-chg="default-enabled" ${rule.enabled ? "checked" : ""} aria-label="Default enabled"></label>
      <a class="name" href="#/default/${enc(d.id)}">Default</a><span class="spacer"></span><a class="button" href="#/default/${enc(d.id)}">Edit</a></div>
    <div class="ifthen"><span class="kw">DEFAULT</span><span>one of the triggers above fired and no rule matched${rule.override_entity ? " (and the manual override signal is off)" : ""}</span><span class="kw">THEN</span><span>${esc(summary ? summary.then : "…")}</span></div>
    ${d.rules.some((r) => r.enabled) ? "" : `<p class="muted small">It never runs while no rule is enabled: it has no trigger of its own.</p>`}</div>`;
}

const entityLabel = (d, id) => ((d.entities.find((e) => e.entity_id === id) || {}).name || id);

async function devicePage(deviceId, token) {
  let data = null;
  let plan = null;

  async function load() {
    const fresh = await api(`api/devices/${enc(deviceId)}`);
    if (token !== navToken) return false;
    data = fresh;
    renderBanners({ connection: data.connection, user: session.user });
    return true;
  }
  const act = async (fn, okText) => {
    try { await fn(); if (okText) toast(okText); } catch (err) { toast(errorText(err)); }
    plan = null;
    if (await load()) draw();
  };

  function draw() {
    const d = data.device;
    const live = data.live;
    const rules = d.rules.map((r, i) => {
      const s = data.summaries[r.id];
      const own = live.automations.find((a) => a.role === "separate" && a.rule_ids.includes(r.id));
      return `<div class="rule ${r.enabled ? "" : "disabled"}" data-rule="${esc(r.id)}">
        <div class="row"><label class="switch"><input type="checkbox" data-chg="rule-enabled" data-id="${esc(r.id)}" ${r.enabled ? "checked" : ""} aria-label="Rule enabled"></label>
          <a class="name" href="#/rules/${enc(d.id)}/${enc(r.id)}">${esc(r.name)}</a>${own ? `<span class="badge">own automation</span>` : ""}${r.triggers.some((t) => t.type === "occupancy") ? `<span class="badge">occupancy</span>` : ""}
${(data.rule_diagnostics[r.id] || {}).advanced ? `<span class="badge">advanced</span>` : ""}${r.imported_from ? `<span class="badge">imported</span>` : ""}
          <span class="spacer"></span>
          <button class="link" data-act="move" data-id="${esc(r.id)}" data-dir="up" ${i === 0 ? "disabled" : ""} aria-label="Move up">↑</button>
          <button class="link" data-act="move" data-id="${esc(r.id)}" data-dir="down" ${i === d.rules.length - 1 ? "disabled" : ""} aria-label="Move down">↓</button>
          <a class="button" href="#/rules/${enc(d.id)}/${enc(r.id)}">Edit</a>
          <button class="link danger-text" data-act="rule-delete" data-id="${esc(r.id)}">Delete</button></div>
        <div class="ifthen"><span class="kw">IF</span><span>${esc(s.if)}</span>${s.and ? `<span class="kw">AND</span><span>${esc(s.and)}</span>` : ""}<span class="kw">THEN</span><span>${esc(s.then)}</span></div>
        ${ruleDiagnostics(r, data.rule_diagnostics[r.id])}</div>`;
    }).join("");
    const originals = data.originals.filter((o) => o.entity_id).map((o) => `<div class="problem" data-original="${esc(o.rule_id)}"><span class="dot ${o.running && !o.allow_both ? "warning" : "info"}"></span><span>“${esc(o.rule_name)}” is a copy of the automation “${esc(o.alias)}”, which ${o.running ? `is still turned on in Home Assistant${o.allow_both ? " (you allowed both to run)" : ": turn it off there before deploying, or both would act"}` : "is turned off in Home Assistant"}. <a href="/config/automation/edit/${enc(o.automation_id)}" target="_top">Open the original</a>${o.allow_both ? ` <button class="link" data-act="disallow-both" data-id="${esc(o.rule_id)}">Do not allow both</button>` : ""}</span></div>`).join("");
    const conflicts = data.conflicts.map((c) => `<div class="problem" data-conflict="${esc(c.code)}"><span class="dot ${c.level === "blocking" && !c.acknowledged ? "error" : "warning"}"></span><span>${esc(c.message)}
        ${c.level === "blocking" ? (c.acknowledged ? ` <span class="badge">order confirmed</span> <button class="link" data-act="unack" data-key="${esc(c.key)}">Withdraw</button>` : ` <button class="link" data-act="ack" data-key="${esc(c.key)}">Confirm this order</button>`) : ""}</span></div>`).join("");
    const related = data.related.map((r) => `<div class="problem"><span class="dot info"></span><span>${esc(r.message)}${r.automation_id ? ` <a href="/config/automation/edit/${enc(r.automation_id)}" target="_top">Open</a>` : ""}</span></div>`).join("");
    const mismatch = data.mismatches.map((m) => `<div class="problem"><span class="dot warning"></span><span>${esc(m.message)}</span></div>`).join("");
    const result = data.last_result ? `<div class="notice ${data.last_result.ok ? "info" : "error"}" id="last-result">${esc(data.last_result.message)} <span class="muted small">${esc(clock(data.last_result.at))}</span></div>` : "";
    const external = ["modified", "missing", "foreign"].includes(live.status) ? `<div class="notice error" id="external">${esc(live.status_text)}. Automation Builder did not touch it. “Review and deploy” shows the difference; deploying replaces the outside change only if you confirm it.</div>` : "";
    const anything = notes(data.notes) + notes(data.entity_problems) + conflicts + originals + mismatch + related;
    view.innerHTML = `<h1><a class="back" href="#/devices" aria-label="Back to devices">←</a><span id="title">${esc(d.name)}</span> ${chip(d.enabled ? live.status : "nothing", d.enabled ? live.status_text : "Disabled")}</h1>
      ${result}${external}
      <div class="two"><div>
        <div class="card"><div class="row"><h2>Rules</h2><span class="spacer"></span><a class="button" href="#/mood/${enc(d.id)}" title="One rule per value of a dropdown helper">From a dropdown helper…</a><a class="button primary" href="#/rules/${enc(d.id)}/new">Add rule</a></div>
          ${rules || `<p class="muted">No rules yet. A rule is: IF a trigger happens AND conditions hold THEN actions run.</p>`}
          ${d.rules.length > 1 ? `<p class="muted small">Rules that match the same trigger run in this order.</p>` : ""}
          ${defaultHtml(d, data.default_summary)}</div>
        <div class="card" id="checks"><h2>Conflicts and notes</h2>${anything || `<p class="muted">Nothing to report.</p>`}</div>
        ${plan ? planHtml(plan) : ""}
      </div><div>
        <div class="card" id="deployment"><div class="row"><h2>Home Assistant automation</h2><span class="spacer"></span><button class="primary" data-act="plan">Review and deploy</button></div>
          ${live.automations.map((a) => automationCard(a)).join("") || `<p class="muted">Nothing to deploy: this device has no enabled rules.</p>`}
          ${data.yaml ? `<details data-keep="yaml"><summary>Generated YAML</summary><pre class="code" id="yaml">${esc(data.yaml)}</pre></details>` : ""}
          <div class="actions"><a class="button" id="export-blueprint" href="#/blueprint/${enc(d.id)}">Export as blueprint</a></div>
          <details data-keep="versions"><summary>Earlier versions</summary>
            <p class="muted small">These change the rules here only. Deploying stays a separate step.</p>
            <div class="actions"><button data-act="restore" data-version="deployed" ${live.deployed ? "" : "disabled"}>Discard changes since the last deployment</button>
            <button data-act="restore" data-version="previous" ${live.automations.some((a) => a.previous_deployments) ? "" : "disabled"}>Go back to the deployment before</button></div></details>
        </div>
        <div class="card"><h2>Device</h2>
          <div class="field"><label for="dev-name">Name</label><input type="text" id="dev-name" data-chg="name" maxlength="80" value="${esc(d.name)}"></div>
          <div class="field"><label for="dev-enabled">Enabled</label><label class="switch"><input type="checkbox" id="dev-enabled" data-chg="enabled" ${d.enabled ? "checked" : ""}> <span class="muted small">Off: the automation is removed at the next deployment, the rules are kept.</span></label></div>
          <div class="field"><label>Room</label><span id="dev-room">${icon(data.room.kind === "room" ? "room" : data.room.kind)}${esc(data.room.name)}${data.room.kind === "multiple" ? ` <span class="muted small">(${esc(data.room.areas.map((a) => a.name).join(", "))}${data.room.partly_unassigned ? ", unassigned" : ""})</span>` : ""}</span>
            <div class="help">From the areas of the controlled entities in Home Assistant. It only decides where the device is listed.</div></div>
          <h3>Controlled entities</h3>
          <ul class="plain">${data.targets.map((t) => `<li class="row">${icon(t.domain)}<span>${esc(t.name)} <span class="badge">${esc(t.domain_label)}</span><br><span class="mono">${esc(t.entity_id)}</span></span><span class="spacer"></span><span class="muted small">${t.exists ? esc(t.state ?? "") : "missing"}</span>${data.targets.length > 1 ? `<button class="link" data-act="target-drop" data-id="${esc(t.entity_id)}">Remove</button>` : ""}</li>`).join("")}</ul>
          <div class="actions"><button data-act="target-add">Add an entity</button></div>
          ${data.ha_device ? `<p class="muted small">Chosen from the Home Assistant device “${esc(data.ha_device)}”.</p>` : ""}
          <span class="save-status" id="dev-status"></span>
          <div class="actions"><button class="danger" data-act="device-delete">Delete this device</button></div>
        </div>
      </div></div>`;
    const keep = view.querySelector("#plan");
    if (keep && draw.scroll) { keep.scrollIntoView({ block: "nearest" }); draw.scroll = false; }
  }

  const patch = (p, ok) => act(() => change("api/devices/update", { device_id: deviceId, patch: p }), ok);
  page = {
    chg: {
      name: (el) => patch({ name: el.value }, "Saved."),
      enabled: (el) => patch({ enabled: el.checked }, "Saved. Deploy to apply it in Home Assistant."),
      "default-enabled": (el) => patch({ default_rule: { ...data.device.default_rule, enabled: el.checked } }, "Saved. Deploy to apply it in Home Assistant."),
      "rule-enabled": (el) => {
        const rule = data.device.rules.find((r) => r.id === el.dataset.id);
        act(() => change("api/rules/save", { device_id: deviceId, rule: { ...rule, enabled: el.checked } }), "Saved. Deploy to apply it in Home Assistant.");
      },
    },
    act: {
      move: (el) => act(() => change("api/rules/move", { device_id: deviceId, rule_id: el.dataset.id, direction: el.dataset.dir })),
      ack: (el) => patch({ acknowledge: el.dataset.key }, "Order confirmed."),
      unack: (el) => patch({ unacknowledge: el.dataset.key }),
      "rule-delete": async (el) => {
        const rule = data.device.rules.find((r) => r.id === el.dataset.id);
        if (!await ask({ title: `Delete the rule “${rule.name}”?`, body: "<p>The rule is removed here. Home Assistant keeps running it until you deploy.</p>", ok: "Delete", danger: true })) return;
        act(() => change("api/rules/delete", { device_id: deviceId, rule_id: rule.id }), "Rule deleted.");
      },
      "target-add": async () => {
        const chosen = await pickEntity("target");
        if (chosen) patch({ targets: [...data.device.targets, chosen.entity_id] }, "Saved.");
      },
      "target-drop": (el) => patch({ targets: data.device.targets.filter((t) => t !== el.dataset.id) }, "Saved."),
      restore: async (el) => {
        const which = el.dataset.version;
        if (!await ask({ title: which === "deployed" ? "Discard the changes since the last deployment?" : "Go back to the deployment before the last one?", body: "<p>The rules of this device are replaced by that version (the current ones go to the backups in Settings). Nothing changes in Home Assistant until you deploy.</p>", ok: "Restore the rules" })) return;
        act(() => change("api/devices/restore", { device_id: deviceId, version: which }), "Rules restored.");
      },
      plan: async () => {
        try { plan = await api("api/devices/plan", { device_id: deviceId }); draw.scroll = true; draw(); } catch (err) { toast(errorText(err)); }
      },
      "plan-close": () => { plan = null; draw(); },
      "allow-both": async (el) => {
        const rule = data.device.rules.find((r) => r.id === el.dataset.id);
        if (!await ask({ title: "Let the original and the copy both run?", body: "<p>Both automations will react to the same triggers and act on the same entities. Choose this only if that is what you want; otherwise turn the original off in Home Assistant.</p>", ok: "Allow both" })) return;
        await act(() => change("api/rules/save", { device_id: deviceId, rule: { ...rule, imported_from: { ...rule.imported_from, allow_both: true } } }), "Saved.");
        page.act.plan();
      },
      "disallow-both": (el) => {
        const rule = data.device.rules.find((r) => r.id === el.dataset.id);
        act(() => change("api/rules/save", { device_id: deviceId, rule: { ...rule, imported_from: { ...rule.imported_from, allow_both: false } } }), "Saved.");
      },
      deploy: async (el) => {
        const box = view.querySelector("#overwrite");
        if (box && !box.checked) { toast("Tick the box to replace the automation that was changed outside Automation Builder."); return; }
        el.disabled = true;
        el.textContent = "Deploying…";
        try {
          const result = await change("api/devices/deploy", { device_id: deviceId, plan_hash: plan.plan_hash, overwrite: Boolean(box && box.checked) });
          toast(result.message);
        } catch (err) { toast(errorText(err)); }
        plan = null;
        if (await load()) draw();
      },
      "device-delete": async () => {
        const deployed = data.live.deployed;
        if (!await ask({ title: `Delete “${data.device.name}”?`, body: `<p>The device and its ${plural(data.device.rules.length, "rule")} are removed from Automation Builder${deployed ? ", and <strong>its automation is deleted from Home Assistant</strong>" : ""}. The rules stay in the backups (Settings).</p>`, ok: "Delete", danger: true })) return;
        try {
          await change("api/devices/delete", { device_id: deviceId, overwrite: true });
          toast("Device deleted.");
          location.hash = "#/devices";
        } catch (err) { toast(errorText(err)); }
      },
    },
  };
  if (await load()) draw();
}

// ------------------------------------------------------------------ rule editor landing
async function rulesPage(token) {
  const data = await loadShell();
  if (token !== navToken) return;
  page = { act: {}, chg: {} };
  if (!data.devices.length) {
    view.innerHTML = `<h1>Rule Editor</h1><div class="card empty"><h2>Add a device first</h2><p class="muted">A rule belongs to the device it controls.</p><a class="button primary" href="#/devices/new">Add a device</a></div>`;
    return;
  }
  view.innerHTML = `<h1>Rule Editor</h1>${data.devices.map((d) => `<div class="card">
      <div class="row"><h2><a href="#/devices/${enc(d.id)}">${esc(d.name)}</a></h2>${chip(d.status, d.status_text)}<span class="spacer"></span><a class="button primary" href="#/rules/${enc(d.id)}/new">New rule</a></div>
      ${d.summaries.length ? `<ul class="plain">${d.summaries.map((r) => `<li class="row"><a href="#/rules/${enc(d.id)}/${enc(r.id)}"><strong>${esc(r.name)}</strong></a>${r.enabled ? "" : `<span class="badge">disabled</span>`}<span class="muted">${esc(r.text)}</span></li>`).join("")}</ul>` : `<p class="muted">No rules yet.</p>`}
    </div>`).join("")}`;
}

// ------------------------------------------------------------------ settings
async function settingsPage(token) {
  await loadShell();
  let data = await api("api/settings");
  if (token !== navToken) return;
  let pendingImport = null;

  function draw() {
    const o = data.settings.occupancy;
    const ha = data.connection.home_assistant || {};
    const m = data.mechanism;
    view.innerHTML = `<h1>Settings</h1>
      <div class="two"><div>
      <div class="card"><h2>Occupancy rechecks</h2>
        <p class="muted small">Only rules with an occupancy trigger get these checks, generated into the device's automation. Each rule can override them under “Advanced”. Changes apply at the next deployment of a device.</p>
        <div class="field"><label for="first">First check after</label><span><input type="number" id="first" data-chg="occ" data-key="first_seconds" min="1" max="60" value="${o.first_seconds}"> s</span></div>
        <div class="field"><label for="second">Second check after</label><span><input type="number" id="second" data-chg="occ" data-key="second_seconds" min="2" max="3600" value="${o.second_seconds}"> s</span></div>
        <div class="field"><label for="periodic">Periodic check</label><select id="periodic" data-chg="occ" data-key="periodic_minutes">${data.periodic_choices.map((c) => `<option value="${c}" ${c === o.periodic_minutes ? "selected" : ""}>${c ? `every ${c} min` : "off"}</option>`).join("")}</select></div>
        <div class="field"><label for="grace">Start-up grace</label><span><input type="number" id="grace" data-chg="occ" data-key="startup_grace_seconds" min="0" max="3600" value="${o.startup_grace_seconds}"> s</span>
          <div class="help">Occupancy changes this soon after Home Assistant starts (or after a deployment) are not corrected by rechecks: right after a start, nothing tells whether a lamp was switched by a person.</div></div>
        <span class="save-status" id="occ-status"></span></div>
      <div class="card"><h2>Backup and restore</h2>
        <p class="muted small">The rules and settings. Restoring never changes Home Assistant by itself: deploy each device afterwards.</p>
        <div class="actions"><a class="button" href="api/export" download="automation-builder-config.json">Download configuration</a>
          <label class="button">Import…<input type="file" id="import-file" data-chg="import-file" accept="application/json,.json" hidden></label></div>
        <div id="import-review"></div>
        <h3>Automatic backups</h3>
        ${data.backups.length ? `<div class="tscroll"><table><thead><tr><th>Before</th><th>When</th><th>Devices</th><th>Rules</th><th></th></tr></thead><tbody>${data.backups.slice(0, 15).map((b) => `<tr><td>${esc(b.before || "")}</td><td>${esc(clock(b.backed_up_at))}</td><td>${b.devices ?? "?"}</td><td>${b.rules ?? "?"}</td><td>${b.damaged ? "damaged" : `<button class="link" data-act="backup" data-name="${esc(b.name)}">Restore</button>`}</td></tr>`).join("")}</tbody></table></div>` : `<p class="muted">None yet: a backup is written before every change.</p>`}
      </div></div><div>
      <div class="card"><h2>Home Assistant</h2>
        <dl class="kv"><dt>Connection</dt><dd>${ha.connected ? `Connected to Home Assistant ${esc(ha.ha_version || "")}` : esc(ha.last_error || ha.state || "not connected")}</dd>
          <dt>Automation Builder</dt><dd>${esc(data.connection.versions.app)}</dd>
          <dt>Configuration saved</dt><dd>${data.saved_at ? esc(clock(data.saved_at)) : "never"}</dd></dl>
        <details data-keep="mech"><summary>How deployment works</summary>
          <p class="small">Automation Builder writes each device's automation through the endpoint Home Assistant's own automation editor uses (<span class="mono">${esc(m.endpoint)}</span>), which stores it in automations.yaml and reloads that one automation. It only ever writes ids that start with <span class="mono">automation_builder_</span>, calls no service and never touches other automations. A blueprint is installed only when you press “Install”, through Home Assistant's blueprint command, into <span class="mono">blueprints/automation/automation_builder/</span>. Execution diagnostics are read from Home Assistant's traces of these automations.</p>
          <dl class="kv small"><dt>Websocket</dt><dd class="mono">${esc(m.websocket)}</dd><dt>API</dt><dd class="mono">${esc(m.api)}</dd>
            <dt>Commands used</dt><dd class="mono">${esc(m.commands.join(", ") || "none yet")}</dd>
            <dt>Requests</dt><dd class="mono">${esc(Object.entries(m.requests).map(([k, v]) => `${k} ${v}`).join(", ") || "none yet")}</dd></dl></details>
        <div class="actions"><a class="button" href="api/diagnostics" download="automation-builder-diagnostics.json">Download diagnostics</a></div>
        ${data.recovery ? `<div class="notice error">Configuration recovery: ${esc(JSON.stringify(data.recovery))}</div>` : ""}
      </div></div></div>`;
  }
  const status = (text, cls = "") => { const el = view.querySelector("#occ-status"); if (el) { el.className = `save-status ${cls}`; el.textContent = text; } };

  page = {
    chg: {
      occ: async (el) => {
        const occupancy = { ...data.settings.occupancy, [el.dataset.key]: Number(el.value) };
        try {
          await change("api/settings/save", { settings: { ...data.settings, occupancy } });
          data.settings.occupancy = occupancy;
          status("Saved. Deploy a device to apply it there.", "ok");
        } catch (err) { status(`Not saved: ${errorText(err)}`, "bad"); }
      },
      "import-file": async (el) => {
        const file = el.files[0];
        if (!file) return;
        const box = view.querySelector("#import-review");
        try {
          pendingImport = JSON.parse(await file.text());
          const review = await api("api/import/review", { document: pendingImport });
          box.innerHTML = reviewHtml(review, "import");
        } catch (err) { pendingImport = null; box.innerHTML = `<div class="notice error">${esc(errorText(err))}</div>`; }
      },
    },
    act: {
      "import-apply": async () => {
        try { await change("api/import/apply", { document: pendingImport }); toast("Configuration imported."); route(); } catch (err) { toast(errorText(err)); }
      },
      backup: async (el) => {
        try {
          const review = await api("api/backups/review", { name: el.dataset.name });
          if (!await ask({ title: "Restore this backup?", body: reviewHtml(review, null), ok: "Restore" })) return;
          await change("api/backups/restore", { name: el.dataset.name });
          toast("Backup restored.");
          route();
        } catch (err) { toast(errorText(err)); }
      },
    },
  };
  draw();
}

function reviewHtml(review, apply) {
  const TYPE = { added: "added", removed: "removed", changed: "changed", settings: "changed" };
  const rows = review.changes.map((c) => `<li>${esc(c.name)}: ${TYPE[c.type]}${c.type !== "settings" ? ` (${plural(c.rules, "rule")})` : ""}${c.deployed ? ` <span class="badge warn">its automation stays in Home Assistant until you remove it on the Overview</span>` : ""}</li>`).join("");
  return `<div class="notice info" id="review"><strong>${review.changes.length ? "This replaces the current rules:" : "Nothing would change."}</strong><ul>${rows}</ul><p class="small">${esc(review.note)}</p>
    ${apply && review.changes.length ? `<div class="actions"><button class="primary" data-act="${apply}-apply">Replace the configuration</button></div>` : ""}</div>`;
}

// ------------------------------------------------------------------ router
async function route() {
  const token = ++navToken;
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const section = parts[0] || "overview";
  const NAV = { blueprint: "devices", mood: "devices", default: "rules" };
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === (NAV[section] || section)));
  page = { act: {}, chg: {} };
  const current = () => token === navToken;
  try {
    if (section === "overview") await overviewPage(token);
    else if (section === "devices" && parts[1] === "new") await newDevicePage(token, parts[2] || null);
    else if (section === "devices" && parts[1]) await devicePage(parts[1], token);
    else if (section === "devices") await devicesPage(token);
    else if (section === "rules" && parts[1] && parts[2]) { await loadShell(); if (current()) page = await editorPage(parts[1], parts[2] === "new" ? null : parts[2], current) || page; }
    else if (section === "default" && parts[1]) { await loadShell(); if (current()) page = await editorPage(parts[1], "default", current) || page; }
    else if (section === "rules") await rulesPage(token);
    else if (section === "blueprint" && parts[1]) { await loadShell(); if (current()) page = await blueprintPage(parts[1], current) || page; }
    else if (section === "import" && parts[1]) { await loadShell(); if (current()) page = await importPage(parts[1], current) || page; }
    else if (section === "import") page = await importListPage(current) || page;
    else if (section === "mood" && parts[1]) { await loadShell(); if (current()) page = await moodPage(parts[1], current) || page; }
    else if (section === "settings") await settingsPage(token);
    else location.hash = "#/";
  } catch (err) {
    if (current()) view.innerHTML = `<div class="notice error">${esc(errorText(err))}</div><p><a href="#/">Back to the overview</a></p>`;
  }
}
window.addEventListener("hashchange", route);
route();
