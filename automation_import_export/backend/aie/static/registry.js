// Automation Import/Export Manager - Helpers, labels & categories: usage overview, backups, Back up & Delete, restore.
import { api, ask, chip, clock, enc, esc, problems, toast, view } from "./ui.js";
import { checksList, logList, poll, route } from "./app.js";

const TABS = { helpers: "Helpers", labels: "Labels", categories: "Categories" };
const USAGE = { automations: "Automations", scripts: "Scripts", scenes: "Scenes", dashboards: "Dashboards", helpers: "Helpers (templates, groups …)", yaml: "YAML files", entities: "Entities", devices: "Devices", areas: "Areas" };
const state = { tab: "helpers", selected: new Set(), q: "", show: "all", type: "", data: null };

const usageChip = (x) => x.count ? `<span class="status info" data-status="used">Used by ${esc(x.count)}</span>` : `<span class="status warn" data-status="unused">Not used</span>`;

function usageList(x) {
  const groups = Object.entries(x.usage || {}).filter(([, v]) => v.length);
  if (!groups.length) return `<p class="small muted">No reference was found in automations, scripts, scenes, dashboards, helpers or YAML files${state.tab !== "helpers" ? ", and nothing carries it" : ""}.</p>`;
  return `<dl class="kv small">${groups.map(([k, v]) => `<dt>${esc(USAGE[k] || k)} (${v.length})</dt><dd>${v.slice(0, 40).map((r) => `${esc(r.name)}${r.name !== r.id ? ` <span class="mono">${esc(r.id)}</span>` : ""}`).join("<br>")}${v.length > 40 ? `<br>… and ${v.length - 40} more` : ""}</dd>`).join("")}</dl>`;
}

function itemsOf(data) { return data[state.tab] || []; }

function matches(x) {
  if (state.show === "unused" && x.count) return false;
  if (state.show === "used" && !x.count) return false;
  if (state.type) {
    if (state.tab === "helpers" && (state.type === "yaml" || state.type === "entry" ? x.kind !== state.type : x.kind !== "storage" || x.domain !== state.type)) return false;
    if (state.tab === "categories" && x.scope !== state.type) return false;
  }
  if (state.q) {
    const hay = [x.name, x.id, x.entity_id, x.type, x.scope_name, x.description, ...(x.entities || [])].join(" ").toLowerCase();
    if (!hay.includes(state.q.toLowerCase())) return false;
  }
  return true;
}

function row(x, data) {
  const selectable = x.deletable;
  const sub = state.tab === "helpers" ? `${x.entity_id || (x.entities || []).join(", ") || "no entity"}${x.kind === "storage" ? ` · ${x.type}` : ""}` : state.tab === "categories" ? `${x.scope_name} · id ${x.id}` : `id ${x.id}`;
  const chips = [];
  if (state.tab === "helpers") {
    chips.push(`<span class="chip">${esc(x.type)}</span>`);
    if (x.kind === "yaml") chips.push(`<span class="chip prot">YAML · read-only</span>`);
    if (x.kind === "entry") chips.push(`<span class="chip prot">Integration helper · read-only</span>`);
    if (x.area) chips.push(`<span class="chip area">⌂ ${esc(data.areas[x.area] || x.area)}</span>`);
    for (const l of x.labels || []) chips.push(`<span class="chip label">🏷 ${esc(data.label_names[l] || l)}</span>`);
  }
  if (state.tab === "labels" && x.color) chips.push(`<span class="chip">Colour: ${esc(x.color)}</span>`);
  if (state.tab === "labels" && x.description) chips.push(`<span class="chip">${esc(x.description)}</span>`);
  return `<div class="auto" data-row="${esc(x.key)}"><span class="sel">${selectable ? `<input type="checkbox" data-key="${esc(x.key)}" aria-label="Select ${esc(x.name)}" ${state.selected.has(x.key) ? "checked" : ""}>` : ""}</span>
    <div><div class="name">${esc(x.name || x.id)}</div><div class="mono">${esc(sub)}</div></div>
    <div class="side">${usageChip(x)}</div>
    <div class="meta"><div class="chips">${chips.join("")}</div>${x.note ? `<div class="small muted">${esc(x.note)}</div>` : ""}</div>
    <details><summary>${x.count ? `Where it is used (${esc(x.count)})` : "Where it is used"}</summary>${usageList(x)}</details></div>`;
}

export function forgetRegistry() { state.data = null; }

export async function renderRegistry(params) {
  if (params && TABS[params.get("tab")]) state.tab = params.get("tab");
  const fresh = state.data && Date.now() - state.loadedAt < 120000 && !(params && params.get("refresh"));
  if (!fresh) {
    view.innerHTML = `<h1>Helpers, labels &amp; categories</h1><p class="muted">Reading helpers, labels and categories and searching where they are used…</p>`;
    state.data = await api("api/registry");
    state.loadedAt = Date.now();
  }
  const data = state.data;
  const all = new Set([...data.helpers, ...data.labels, ...data.categories].filter((x) => x.deletable).map((x) => x.key));
  for (const key of [...state.selected]) if (!all.has(key)) state.selected.delete(key);
  const s = data.summary;
  const searched = data.searched || {};
  view.innerHTML = `<h1>Helpers, labels &amp; categories <span class="small muted">read ${esc(clock(data.taken_at))}</span> <a class="button" id="refresh" href="#/registry?tab=${state.tab}&amp;refresh=1">Read again</a></h1>
    ${problems(data.problems, ["error", "warning", "info"])}
    <div class="tiles">
      <a class="tile" href="#/registry?tab=helpers" data-tab="helpers"><div class="n">${esc(s.helpers)}</div><div class="t">Helpers · ${esc(s.helpers_unused)} not used</div></a>
      <a class="tile" href="#/registry?tab=labels" data-tab="labels"><div class="n">${esc(s.labels)}</div><div class="t">Labels · ${esc(s.labels_unused)} not used</div></a>
      <a class="tile" href="#/registry?tab=categories" data-tab="categories"><div class="n">${esc(s.categories)}</div><div class="t">Categories · ${esc(s.categories_unused)} not used</div></a>
    </div>
    <details class="card"><summary>What “used” means</summary><p class="small">Searched: ${esc(searched.automations || 0)} automations, ${esc(searched.scripts || 0)} scripts, ${esc(searched.scenes || 0)} scenes, ${esc(searched.dashboards || 0)} dashboards, ${esc(searched.helpers || 0)} integration helpers (templates, groups …) and ${esc(searched.yaml || 0)} YAML files${data.config_folder ? "" : " (the configuration folder is not readable: YAML files and integration helpers were not searched)"}. A helper counts as used when its entity id (also inside templates) or its registry id appears there; a label when something carries it or targets it; a category when an entity carries it.</p>
      <p class="small"><strong>Not searched:</strong> Node-RED, AppDaemon and other Apps, ESPHome devices configured outside Home Assistant's configuration folder, voice assistants and external services. “Not used” is a strong hint, not a guarantee – every deletion is backed up first and can be restored.</p></details>
    <nav class="tabs" aria-label="Kind">${Object.entries(TABS).map(([k, v]) => `<a href="#/registry?tab=${k}" class="${state.tab === k ? "active" : ""}" data-tab="${k}">${esc(v)} <span class="muted small">${esc(data[k].length)}</span></a>`).join("")}</nav>
    <div class="card"><div class="filters">
      <label>Search<input type="search" id="f-q" value="${esc(state.q)}" placeholder="Name, id, entity id…"></label>
      <label>Show<select id="f-show">${Object.entries({ all: "Everything", unused: "Not used", used: "Used" }).map(([k, v]) => `<option value="${k}" ${state.show === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      ${state.tab === "helpers" ? `<label>Type<select id="f-type"><option value="">Any</option>${[...new Map(data.helpers.filter((h) => h.kind === "storage").map((h) => [h.domain, h.type])).entries()].map(([d, t]) => `<option value="${esc(d)}" ${state.type === d ? "selected" : ""}>${esc(t)}</option>`).join("")}<option value="yaml" ${state.type === "yaml" ? "selected" : ""}>Defined in YAML</option><option value="entry" ${state.type === "entry" ? "selected" : ""}>Integration helpers</option></select></label>` : ""}
      ${state.tab === "categories" ? `<label>Scope<select id="f-type"><option value="">Any</option>${[...new Map(data.categories.map((c) => [c.scope, c.scope_name])).entries()].map(([sc, n]) => `<option value="${esc(sc)}" ${state.type === sc ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></label>` : ""}
    </div></div>
    <div class="card"><div class="row" id="sel-bar"><button type="button" id="sel-shown">Select all shown</button><button type="button" id="sel-none">Deselect all</button><span class="spacer"></span><span class="muted small" id="shown"></span></div><div id="list"></div></div>
    <div class="sticky-actions"><div class="row"><span id="sel-count" class="muted"></span><span class="spacer"></span>
      <button id="backup-sel" class="primary">Back up selected</button>
      <button id="cleanup" class="danger">Back up &amp; delete selected…</button>
      <button id="backup-all">Back up everything</button></div></div>`;
  const draw = () => {
    const shown = itemsOf(data).filter(matches);
    view.querySelector("#shown").textContent = `${shown.length} shown`;
    view.querySelector("#list").innerHTML = shown.length ? shown.map((x) => row(x, data)).join("") : `<p class="muted">Nothing matches the filters.</p>`;
    const selectableShown = shown.filter((x) => x.deletable);
    view.querySelector("#sel-shown").disabled = !selectableShown.length || selectableShown.every((x) => state.selected.has(x.key));
    view.querySelector("#sel-none").disabled = !state.selected.size;
    const chosen = [...data.helpers, ...data.labels, ...data.categories].filter((x) => state.selected.has(x.key));
    const inUse = chosen.filter((x) => x.count).length;
    view.querySelector("#sel-count").textContent = `${chosen.length} selected${chosen.length ? ` (${["helpers", "labels", "categories"].map((t) => `${data[t].filter((x) => state.selected.has(x.key)).length} ${t}`).join(", ")})` : ""}${inUse ? ` · ${inUse} still in use` : ""}`;
    view.querySelector("#backup-sel").disabled = !chosen.length;
    view.querySelector("#cleanup").disabled = !chosen.length;
  };
  view.querySelectorAll("[data-tab]").forEach((a) => { a.onclick = () => { if (state.tab !== a.dataset.tab) state.type = ""; }; });
  view.querySelector("#f-q").oninput = (e) => { state.q = e.target.value; draw(); };
  view.querySelector("#f-show").oninput = (e) => { state.show = e.target.value; draw(); };
  const type = view.querySelector("#f-type");
  if (type) type.oninput = (e) => { state.type = e.target.value; draw(); };
  view.querySelector("#list").onchange = (e) => {
    const box = e.target.closest("input[data-key]");
    if (!box) return;
    if (box.checked) state.selected.add(box.dataset.key); else state.selected.delete(box.dataset.key);
    draw();
  };
  view.querySelector("#sel-shown").onclick = () => { for (const x of itemsOf(data).filter(matches)) if (x.deletable) state.selected.add(x.key); draw(); };
  view.querySelector("#sel-none").onclick = () => { state.selected.clear(); draw(); };
  view.querySelector("#backup-sel").onclick = async () => {
    try { const op = await api("api/registry/backup", { keys: [...state.selected] }); location.hash = `#/operation/${enc(op.id)}`; } catch (err) { toast(err.message); }
  };
  view.querySelector("#backup-all").onclick = async () => {
    try { const op = await api("api/registry/backup", { mode: "all", label: "All helpers, labels and categories" }); location.hash = `#/operation/${enc(op.id)}`; } catch (err) { toast(err.message); }
  };
  view.querySelector("#cleanup").onclick = async () => {
    const chosen = [...data.helpers, ...data.labels, ...data.categories].filter((x) => state.selected.has(x.key));
    const inUse = chosen.filter((x) => x.count);
    const ok = await ask({ title: `Back up and delete ${chosen.length} item(s)?`, body: `<p>First an archive of the selected helpers, labels and categories is made and verified (with where the labels and categories are assigned). Then you see the final list and confirm it. Nothing is deleted before that.</p>${inUse.length ? `<p class="danger-text"><strong>${inUse.length} of them are still in use:</strong> ${inUse.slice(0, 8).map((x) => esc(x.name)).join(", ")}${inUse.length > 8 ? " …" : ""}</p>` : ""}`, ok: "Back up and continue" });
    if (!ok) return;
    try { const op = await api("api/registry/cleanup/start", { keys: chosen.map((x) => x.key) }); state.selected.clear(); state.data = null; location.hash = `#/regcleanup/${enc(op.id)}`; } catch (err) { toast(err.message); }
  };
  draw();
}

// ------------------------------------------------------------------ Back up & Delete
export async function renderRegCleanup(id, op) {
  op = op || await api(`api/operations/${enc(id)}`);
  const preview = op.preview || { delete: [], blocked: {} };
  const items = op.items || {};
  view.innerHTML = `<h1><a href="#/registry" aria-label="Back">←</a> Back up &amp; delete ${chip(op.status)}</h1>
    <div class="card"><dl class="kv"><dt>Started</dt><dd>${esc(clock(op.created_at))} <span class="mono">${esc(op.id)}</span></dd>${op.summary ? `<dt>Status</dt><dd>${esc(op.summary)}</dd>` : ""}${op.error ? `<dt>Error</dt><dd class="danger-text">${esc(op.error)}</dd>` : ""}</dl>
      ${op.status === "exporting" ? `<p class="muted">Backing up and verifying… this page updates by itself.</p>` : ""}
      ${op.status === "deleting" ? `<p class="muted">Deleting… this page updates by itself.</p>` : ""}
      ${op.status === "interrupted" ? `<div class="notice error">The App stopped while this operation was ${esc(op.interrupted_status)}. Check Home Assistant's current state first.</div><div class="actions"><button class="primary" id="reconcile">Check the current state</button></div>` : ""}</div>
    ${op.archive_id ? `<div class="card"><h2>Archive</h2><p>The archive is stored in the App${op.verification ? ` and its verification is <strong>${esc(op.verification.status)}</strong>` : ""}. Download a copy before deleting.</p>
      <div class="actions"><a class="button primary" id="download" href="api/archives/${enc(op.archive_id)}/download" download>Download the archive</a><a class="button" href="#/archive/${enc(op.archive_id)}">Inspect</a>${["completed", "partial", "failed"].includes(op.status) ? `<a class="button" href="#/regrestore/${enc(op.archive_id)}">Restore from this archive…</a>` : ""}</div>
      ${op.verification ? `<details ${op.verification.ok ? "" : "open"}><summary>Verification checks</summary>${checksList(op.verification)}</details>` : ""}</div>` : ""}
    ${op.status === "awaiting_confirmation" ? confirmBox(op) : ""}
    ${Object.keys(preview.blocked || {}).length ? `<div class="card"><h2>Not deleted</h2>${Object.entries(preview.blocked).map(([k, why]) => `<div class="problem"><span class="dot warning"></span><span><strong>${esc(k)}</strong>: ${esc([].concat(why).join("; "))}</span></div>`).join("")}</div>` : ""}
    ${Object.keys(items).length && op.status !== "awaiting_confirmation" ? `<div class="card"><h2>Items</h2><div class="tscroll"><table id="op-items"><thead><tr><th>Item</th><th>Status</th><th></th></tr></thead><tbody>${Object.entries(items).map(([k, it]) => `<tr data-key="${esc(k)}"><td>${esc(it.alias || k)}<div class="mono">${esc(k)}</div></td><td>${chip(it.status)}</td><td class="small">${esc(it.message || "")}${it.removal_verified ? " Removal verified." : ""}</td></tr>`).join("")}</tbody></table></div></div>` : ""}
    <div class="card">${logList(op)}</div>`;
  bind(op);
  if (["exporting", "deleting", "running"].includes(op.status)) poll(() => { if (location.hash === `#/regcleanup/${op.id}`) route(); });
}

function confirmBox(op) {
  const list = op.preview.delete;
  const inUse = list.filter((d) => d.count);
  const kind = { helper: "Helper", label: "Label", category: "Category" };
  return `<div class="card"><h2>Final deletion preview: ${list.length} item(s)</h2>
    <p>These are deleted from Home Assistant. Each one is compared with the archive again right before it is deleted; one that changed meanwhile is skipped. Deleting a label removes it from every entity, device and area; deleting a category removes it from every entity; deleting a helper removes its entity. The archive restores all of that.</p>
    <div id="del-list">${list.map((d) => `<div class="auto" data-del="${esc(d.key)}"><span class="sel">${d.count ? "⚠" : "✔"}</span><div><div class="name">${esc(d.name)}</div><div class="mono">${esc(kind[d.kind] || d.kind)}${d.detail && d.detail !== kind[d.kind] ? ` · ${esc(d.detail)}` : ""} · ${esc(d.key)}</div></div><div class="side">${d.count ? `<span class="status warn">Used by ${esc(d.count)}</span>` : `<span class="status ok">Not used</span>`}</div>${d.count ? `<details><summary>Where it is used</summary>${usageList(d)}</details>` : ""}</div>`).join("")}</div>
    <div class="confirm-box"><label class="check"><input type="checkbox" id="ack"> I downloaded the archive, or I rely on the copy stored in this App.</label>
      ${inUse.length ? `<br><label class="check"><input type="checkbox" id="inuse"> ${esc(inUse.length)} of them are still in use: delete them anyway.</label>` : ""}
      <p><label>Type <strong>${list.length}</strong> to confirm: <input type="text" id="count" inputmode="numeric" autocomplete="off"></label></p>
      <div class="actions"><button class="danger solid" id="delete" disabled>Delete ${list.length} item(s)</button><button id="cancel">Cancel – delete nothing</button></div></div></div>`;
}

function bind(op) {
  const reconcile = view.querySelector("#reconcile");
  if (reconcile) reconcile.onclick = async () => { reconcile.disabled = true; try { await api("api/operations/reconcile", { operation_id: op.id }); route(); } catch (err) { toast(err.message); reconcile.disabled = false; } };
  const del = view.querySelector("#delete");
  if (!del) return;
  const count = view.querySelector("#count");
  const ack = view.querySelector("#ack");
  const inuse = view.querySelector("#inuse");
  const n = op.preview.delete.length;
  const sync = () => { del.disabled = !(ack.checked && (!inuse || inuse.checked) && count.value.trim() === String(n)); };
  count.oninput = sync; ack.onchange = sync; if (inuse) inuse.onchange = sync;
  view.querySelector("#cancel").onclick = async () => { try { await api("api/operations/cancel", { operation_id: op.id }); toast("Nothing was deleted"); route(); } catch (err) { toast(err.message); } };
  del.onclick = async () => {
    del.disabled = true;
    try {
      await api("api/registry/cleanup/confirm", { operation_id: op.id, plan_hash: op.plan_hash, confirm_count: Number(count.value.trim()), archive_ack: ack.checked, in_use_ack: inuse ? inuse.checked : false });
      route();
    } catch (err) { toast(err.message); sync(); }
  };
}

// ------------------------------------------------------------------ Restore
const ACTION = { create: "Create", exists: "Already there", use_existing: "Use the existing one (same name)", update: "Update it", replace: "Replace it", metadata: "Restore its settings", skip: "Skip", not_selected: "Not selected", blocked: "Blocked" };
const CHOICE = { skip: "Skip", update: "Update it (after a backup)", replace: "Replace it (after a backup)" };

export async function renderRegRestore(archiveId) {
  const decisions = { collisions: {}, assignments: true };
  let plan = null;
  let busy = false;
  const load = async () => {
    try { plan = await api("api/registry/restore/plan", { archive_id: archiveId, decisions }); draw(); } catch (err) { view.innerHTML = `<h1>Restore</h1><div class="notice error">${esc(err.message)}</div>`; }
  };
  view.innerHTML = `<h1>Restore</h1><p class="muted">Comparing the archive with Home Assistant…</p>`;
  const kindName = { label: "Label", category: "Category", helper: "Helper" };
  const itemRow = (i) => {
    const included = i.action !== "not_selected";
    const a = i.assign;
    const assignText = a ? [a.entities ? `${a.entities} entit${a.entities === 1 ? "y" : "ies"}` : "", a.devices ? `${a.devices} device(s)` : "", a.areas ? `${a.areas} area(s)` : ""].filter(Boolean).join(", ") : "";
    return `<div class="auto" data-item="${esc(i.key)}"><span class="sel"><input type="checkbox" data-include="${esc(i.key)}" ${included ? "checked" : ""} aria-label="Include ${esc(i.name)}"></span>
      <div><div class="name">${esc(i.name || i.id)}</div><div class="mono">${esc(kindName[i.kind])}${i.type ? ` · ${esc(i.type)}` : ""}${i.scope ? ` · ${esc(i.scope)}` : ""} · ${esc(i.entity_id || i.id)}</div></div>
      <div class="side">${chip(i.action === "blocked" ? "blocked" : ["skip", "not_selected", "exists"].includes(i.action) ? "pending" : "running", ACTION[i.action])}</div>
      <div class="meta">${i.reason ? `<div class="small">${esc(i.reason)}</div>` : ""}
        ${i.choices ? `<label class="small">It exists with other settings: <select data-collision="${esc(i.key)}">${i.choices.map((c) => `<option value="${c}" ${i.action === c ? "selected" : ""}>${esc(CHOICE[c])}</option>`).join("")}</select></label>` : ""}
        ${(i.diff || []).length ? `<details><summary>Differences (${i.diff.length})</summary><ul class="small mono">${i.diff.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}
        ${(i.notes || []).map((n) => `<div class="small muted">${esc(n)}</div>`).join("")}
        ${assignText ? `<div class="small">Put back on ${esc(assignText)}</div>` : ""}${a && a.missing ? `<div class="small muted">${esc(a.missing)} entit(ies)/device(s)/area(s) of the archive no longer exist</div>` : ""}${a && a.conflicts ? `<div class="small muted">${esc(a.conflicts)} entit(ies) have another category in this scope: kept</div>` : ""}</div></div>`;
  };
  const draw = () => {
    const writing = plan.items.filter((i) => ["create", "update", "replace", "metadata"].includes(i.action));
    const selected = plan.items.filter((i) => i.action !== "not_selected").length;
    view.innerHTML = `<h1><a href="#/archive/${enc(archiveId)}" aria-label="Back">←</a> Restore helpers, labels &amp; categories ${plan.same_installation ? chip("completed", "From this installation") : chip("incomplete", "From another installation")}</h1>
      <div class="card"><p>Archive of ${esc(plan.source.location_name || "?")} · Home Assistant ${esc(plan.source.ha_version || "?")} · created ${esc(plan.source.created_at || "")}</p>
        <div class="chips">${Object.entries(plan.counts).filter(([a, n]) => n && a !== "not_selected").map(([a, n]) => `<span class="chip">${esc(ACTION[a])}: ${esc(n)}</span>`).join("")}</div>
        <label class="check"><input type="checkbox" id="assign" ${plan.assignments ? "checked" : ""}> Put labels and categories back on the entities, devices and areas that carried them (${esc(plan.assignments_total)} to restore)</label></div>
      <div class="card"><h2>Items</h2><div class="row" id="sel-bar"><button type="button" id="sel-all" ${selected === plan.items.length ? "disabled" : ""}>Select all</button><button type="button" id="sel-none" ${selected ? "" : "disabled"}>Deselect all</button><span class="spacer"></span><span class="muted small" id="sel-count">${esc(selected)} of ${esc(plan.items.length)} selected</span></div>
        <div id="items">${plan.items.map(itemRow).join("")}</div></div>
      <div class="sticky-actions"><div class="row"><span class="muted">${plan.ready ? `${esc(writing.length)} item(s) written${plan.assignments && plan.assignments_total ? `, ${esc(plan.assignments_total)} assignment(s) restored` : ""}` : "Nothing to restore with these choices"}</span><span class="spacer"></span>
        <button class="primary" id="run" ${plan.ready ? "" : "disabled"}>Restore…</button></div></div>`;
    view.querySelectorAll("[data-include]").forEach((box) => {
      box.onchange = () => {
        const current = new Set(plan.selection);
        if (box.checked) current.add(box.dataset.include); else current.delete(box.dataset.include);
        decisions.selection = plan.items.map((i) => i.key).filter((k) => current.has(k));
        load();
      };
    });
    view.querySelector("#sel-all").onclick = () => { decisions.selection = plan.items.map((i) => i.key); load(); };
    view.querySelector("#sel-none").onclick = () => { decisions.selection = []; load(); };
    view.querySelector("#assign").onchange = (e) => { decisions.assignments = e.target.checked; load(); };
    view.querySelectorAll("[data-collision]").forEach((sel) => { sel.onchange = () => { decisions.collisions[sel.dataset.collision] = sel.value; load(); }; });
    view.querySelector("#run").onclick = async () => {
      if (busy) return;
      const ok = await ask({ title: "Restore now?", body: `<p>${writing.length} item(s) are written to Home Assistant${plan.assignments ? " and labels and categories are put back where they were" : ""}. Items that are updated or replaced are backed up first. Afterwards everything is compared with the archive.</p>`, ok: "Restore" });
      if (!ok) return;
      busy = true;
      try {
        const op = await api("api/registry/restore/execute", { archive_id: archiveId, decisions, plan_hash: plan.plan_hash, confirm: true });
        location.hash = `#/operation/${enc(op.id)}`;
      } catch (err) { busy = false; if (err.data && err.data.plan) { plan = err.data.plan; draw(); } toast(err.message); }
    };
  };
  await load();
}

// ------------------------------------------------------------------ Archive inspection
export function renderRegArchive(id, data, verificationChip) {
  const s = data.summary;
  const items = data.items || [];
  const kindName = { label: "Label", category: "Category", helper: "Helper" };
  view.innerHTML = `<h1><a href="#/archives" aria-label="Back">←</a> Archive ${verificationChip(data.entry.verification)}</h1>
    <div class="card"><dl class="kv">
      <dt>Created</dt><dd>${esc(clock(s.created_at))} <span class="mono">${esc(s.archive_id)}</span></dd>
      <dt>Kind</dt><dd>Helpers, labels &amp; categories</dd>
      <dt>Home Assistant</dt><dd>${esc((s.home_assistant || {}).version || "?")} · ${esc((s.home_assistant || {}).location_name || "")} ${data.same_installation ? chip("completed", "This installation") : chip("incomplete", "Another installation")}</dd>
      <dt>Contents</dt><dd>${esc(s.counts.helpers)} helper(s) · ${esc(s.counts.labels)} label(s) · ${esc(s.counts.categories)} categor${s.counts.categories === 1 ? "y" : "ies"} · ${esc(s.counts.assignments)} assignment(s)</dd>
      <dt>Size</dt><dd>${esc(s.size)} bytes · SHA-256 <span class="mono">${esc(s.sha256)}</span></dd>
    </dl>
    <div class="actions"><a class="button primary" href="#/regrestore/${enc(id)}">Restore…</a><a class="button" href="api/archives/${enc(id)}/download" download>Download</a><button id="verify">Verify again (compare with Home Assistant)</button></div>
    <div id="verify-result"></div></div>
    <div class="card"><h2>Items</h2>${items.map((i) => `<div class="auto"><span class="sel">✔</span><div><div class="name">${esc(i.name)}</div><div class="mono">${esc(kindName[i.kind] || i.kind)} · ${esc(i.entity_id || i.key)}</div></div></div>`).join("")}</div>`;
  view.querySelector("#verify").onclick = async (e) => {
    e.target.disabled = true;
    try {
      const report = await api("api/archives/verify", { archive_id: id, live: true });
      view.querySelector("#verify-result").innerHTML = `<h3>Verification ${verificationChip(report)}</h3>${checksList(report)}`;
    } catch (err) { toast(err.message); }
    e.target.disabled = false;
  };
}
