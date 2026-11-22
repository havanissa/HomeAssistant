// Automation Import/Export Manager - the Export page (inventory) and the Export & Delete workflow.
import { api, ask, automationChips, chip, clock, enc, esc, problems, toast, view } from "./ui.js";
import { checksList, logList, poll, route } from "./app.js";

const state = { selected: new Set(), filters: { q: "", area: "", category: "", label: "", enabled: "", show: "all" }, group: "none", data: null };

const SHOW = {
  all: (a) => true,
  ordinary: (a) => a.source === "ui" && a.owner === "ordinary",
  eligible: (a) => a.deletable && a.owner === "ordinary",
  protected: (a) => a.protected,
  review: (a) => a.owner === "review",
  external: (a) => a.source === "external",
  blocked: (a) => a.source === "ui" && (a.problems || []).some((p) => p.level === "error"),
};

function matches(a) {
  const f = state.filters;
  if (!SHOW[f.show](a)) return false;
  if (f.area && (f.area === "-" ? a.area : (!a.area || a.area.id !== f.area))) return false;
  if (f.category && (f.category === "-" ? a.categories.length : !a.categories.some((c) => c.id === f.category))) return false;
  if (f.label && (f.label === "-" ? a.labels.length : !a.labels.some((l) => l.id === f.label))) return false;
  if (f.enabled === "on" && a.state !== "on") return false;
  if (f.enabled === "off" && !(a.state === "off" || a.registry_disabled)) return false;
  if (f.q) {
    const hay = [a.name, a.alias, a.entity_id, a.config_id, a.description, a.area && a.area.name, ...a.labels.map((l) => l.name), ...a.categories.map((c) => c.name), a.blueprint].join(" ").toLowerCase();
    if (!hay.includes(f.q.toLowerCase())) return false;
  }
  return true;
}

function groups(items) {
  const g = state.group;
  const out = new Map();
  const add = (k, a) => { if (!out.has(k)) out.set(k, []); out.get(k).push(a); };
  for (const a of items) {
    if (g === "category") add(a.categories.length ? a.categories.map((c) => c.name || c.id).join(", ") : "No category", a);
    else if (g === "area") add(a.area ? a.area.name || a.area.id : "No area", a);
    else if (g === "label") { if (!a.labels.length) add("No label", a); for (const l of a.labels) add(`🏷 ${l.name || l.id}`, a); }
    else if (g === "source") add({ ui: a.owner === "builder" ? "Automation Builder (protected)" : a.owner === "review" ? "Ownership unclear (review)" : "Ordinary UI automations", external: "External YAML files (read-only)", integration: "Integrations (protected)", orphan: "Registry entries without an automation" }[a.source] || a.source, a);
    else add("", a);
  }
  return [...out.entries()].sort((x, y) => (x[0].startsWith("No ") ? 1 : 0) - (y[0].startsWith("No ") ? 1 : 0) || x[0].localeCompare(y[0]));
}

function row(a, selectable = true) {
  const sel = selectable ? `<input type="checkbox" data-key="${esc(a.key)}" aria-label="Select ${esc(a.alias)}" ${state.selected.has(a.key) ? "checked" : ""}>` : "";
  const enabled = a.registry_disabled ? chip("off", "Disabled entity") : a.state ? chip(a.state) : chip("off", "Not loaded");
  return `<div class="auto" data-row="${esc(a.key)}"><span class="sel">${sel}</span>
    <div><div class="name">${esc(a.name)}</div>${a.name !== a.alias ? `<div class="small muted">Alias: ${esc(a.alias)}</div>` : ""}<div class="mono">${esc(a.entity_id || "no entity")} · id ${esc(a.config_id || "none")}</div></div>
    <div class="side">${enabled}</div>
    <div class="meta">${automationChips(a)}${problems(a.problems, ["error"])}</div>
    <details><summary>Details</summary><dl class="kv">
      <dt>Source</dt><dd>${esc({ ui: "automations.yaml (editable in the UI)", external: "Another YAML file (read-only)", integration: "An integration", orphan: "Registry entry only" }[a.source] || a.source)}${a.config_origin ? ` · read via ${esc(a.config_origin)}` : ""}</dd>
      <dt>Owner</dt><dd>${esc({ ordinary: "Ordinary", builder: "Automation Builder (protected)", review: "Unclear (review)" }[a.owner])}</dd>
      <dt>Can be deleted</dt><dd>${a.deletable ? (a.owner === "review" ? "Only when included explicitly" : "Yes") : "No"}</dd>
      <dt>Mode</dt><dd>${esc(a.mode || "")}</dd><dt>Last triggered</dt><dd>${esc(clock(a.last_triggered) || "never")}</dd>
      ${a.description ? `<dt>Description</dt><dd>${esc(a.description)}</dd>` : ""}
      <dt>Refers to</dt><dd class="small">${esc([...(a.references.entities || []), ...(a.references.devices || []).map((d) => `device ${d}`)].slice(0, 30).join(", ") || "nothing found")}</dd>
    </dl>${problems(a.problems, ["warning", "info"])}</details></div>`;
}

function options(list, value, none) {
  return `<option value="">Any</option><option value="-" ${value === "-" ? "selected" : ""}>${esc(none)}</option>` + list.map((x) => `<option value="${esc(x.id)}" ${value === x.id ? "selected" : ""}>${esc(x.name || x.id)}</option>`).join("");
}

export async function renderExport(params) {
  if (params && params.get("show") && SHOW[params.get("show")]) state.filters.show = params.get("show");
  view.innerHTML = `<h1>Export</h1><p class="muted">Reading every automation from Home Assistant…</p>`;
  const data = await api("api/inventory");
  state.data = data;
  for (const key of [...state.selected]) if (!data.items.some((a) => a.key === key)) state.selected.delete(key);
  const s = data.summary;
  view.innerHTML = `<h1>Export <span class="small muted">${esc(s.total)} automations · read ${esc(clock(data.taken_at))}</span></h1>
    ${problems(data.problems, ["error", "warning", "info"])}
    ${data.file_scan && data.file_scan.duplicate_ids && data.file_scan.duplicate_ids.length ? `<div class="notice error">automations.yaml contains duplicate ids (${esc(data.file_scan.duplicate_ids.join(", "))}): those automations are blocked from deletion.</div>` : ""}
    <div class="card"><div class="filters">
      <label>Search<input type="search" id="f-q" value="${esc(state.filters.q)}" placeholder="Name, entity id, label, area…"></label>
      <label>Show<select id="f-show">${Object.entries({ all: "All automations", ordinary: "Ordinary UI automations", eligible: "Eligible for cleanup", protected: "Protected", review: "Ownership unclear", external: "External YAML", blocked: "Blocked" }).map(([k, v]) => `<option value="${k}" ${state.filters.show === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      <label>Area<select id="f-area">${options(data.areas, state.filters.area, "No area")}</select></label>
      <label>Category<select id="f-category">${options(data.categories.filter((c) => c.scope === "automation"), state.filters.category, "No category")}</select></label>
      <label>Label<select id="f-label">${options(data.labels, state.filters.label, "No label")}</select></label>
      <label>State<select id="f-enabled"><option value="">Any</option><option value="on" ${state.filters.enabled === "on" ? "selected" : ""}>Enabled</option><option value="off" ${state.filters.enabled === "off" ? "selected" : ""}>Disabled</option></select></label>
      <label>Group by<select id="f-group">${Object.entries({ none: "Nothing", category: "Category", area: "Area", label: "Label", source: "Source" }).map(([k, v]) => `<option value="${k}" ${state.group === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>
    </div></div>
    <div class="card"><div class="row"><label class="check"><input type="checkbox" id="sel-all"> Select all shown</label><span class="spacer"></span><span class="muted small" id="shown"></span></div><div id="list"></div></div>
    <div class="sticky-actions"><div class="row"><span id="sel-count" class="muted"></span><span class="spacer"></span>
      <button id="export" class="primary">Export selected</button>
      <button id="cleanup" class="danger">Export &amp; Delete selected…</button>
      <button id="backup">Back up everything</button></div></div>`;
  const draw = () => {
    const shown = data.items.filter(matches);
    view.querySelector("#shown").textContent = `${shown.length} shown`;
    view.querySelector("#list").innerHTML = shown.length ? groups(shown).map(([name, items]) => `<div class="group">${name ? `<h3>${esc(name)} <span class="muted small">${items.length}</span></h3>` : ""}${items.map((a) => row(a)).join("")}</div>`).join("") : `<p class="muted">Nothing matches the filters.</p>`;
    view.querySelector("#sel-all").checked = shown.length > 0 && shown.every((a) => state.selected.has(a.key));
    const chosen = data.items.filter((a) => state.selected.has(a.key));
    const eligible = chosen.filter((a) => a.deletable);
    view.querySelector("#sel-count").textContent = `${chosen.length} selected${chosen.length ? ` · ${eligible.length} can be deleted` : ""}`;
    view.querySelector("#export").disabled = !chosen.length;
    view.querySelector("#cleanup").disabled = !eligible.length;
  };
  const bind = (id, key, target = state.filters) => { view.querySelector(id).oninput = (e) => { target[key] = e.target.value; draw(); }; };
  bind("#f-q", "q"); bind("#f-show", "show"); bind("#f-area", "area"); bind("#f-category", "category"); bind("#f-label", "label"); bind("#f-enabled", "enabled");
  view.querySelector("#f-group").oninput = (e) => { state.group = e.target.value; draw(); };
  view.querySelector("#list").onchange = (e) => {
    const box = e.target.closest("input[data-key]");
    if (!box) return;
    if (box.checked) state.selected.add(box.dataset.key); else state.selected.delete(box.dataset.key);
    view.querySelectorAll(`input[data-key="${CSS.escape(box.dataset.key)}"]`).forEach((b) => { b.checked = box.checked; });
    draw();
  };
  view.querySelector("#sel-all").onchange = (e) => {
    for (const a of data.items.filter(matches)) { if (e.target.checked) state.selected.add(a.key); else state.selected.delete(a.key); }
    draw();
  };
  view.querySelector("#export").onclick = async () => {
    try {
      const op = await api("api/export", { keys: [...state.selected], filters: state.filters });
      location.hash = `#/operation/${enc(op.id)}`;
    } catch (err) { toast(err.message); }
  };
  view.querySelector("#backup").onclick = async () => {
    try { const op = await api("api/export", { mode: "all", label: "Full backup" }); location.hash = `#/operation/${enc(op.id)}`; } catch (err) { toast(err.message); }
  };
  view.querySelector("#cleanup").onclick = () => {
    cleanupPreset = data.items.filter((a) => state.selected.has(a.key) && a.deletable).map((a) => a.key);
    location.hash = "#/cleanup/new";
  };
  draw();
}

// ------------------------------------------------------------------ Export & Delete
let cleanupPreset = null;
const STEPS = ["Inventory", "Exclusions", "Dependencies", "Export", "Verify", "Backup saved", "Download", "Deletion preview", "Confirmation", "Delete", "Verify removal", "Protected unchanged", "Report"];

function steps(now) {
  return `<ol class="steps">${STEPS.map((s, i) => `<li class="${i < now ? "done" : i === now ? "now" : ""}">${esc(s)}</li>`).join("")}</ol>`;
}

async function renderNewCleanup() {
  view.innerHTML = `<h1>Export &amp; Delete Legacy Automations</h1><p class="muted">Reading every automation from Home Assistant…</p>`;
  const data = await api("api/inventory");
  const eligible = data.items.filter((a) => a.deletable && a.owner === "ordinary");
  const review = data.items.filter((a) => a.owner === "review" && a.deletable);
  const excluded = data.items.filter((a) => !(a.deletable && (a.owner === "ordinary" || a.owner === "review")));
  const chosen = new Set(cleanupPreset ? cleanupPreset.filter((k) => eligible.some((a) => a.key === k) || review.some((a) => a.key === k)) : eligible.map((a) => a.key));
  cleanupPreset = null;
  const includeReview = new Set([...chosen].filter((k) => review.some((a) => a.key === k)));
  view.innerHTML = `<h1><a href="#/" aria-label="Back">←</a> Export &amp; Delete Legacy Automations</h1>
    ${steps(1)}
    <div class="notice info">Nothing is deleted in this step. The selected automations are exported to an archive, the archive is verified (including a lossless comparison with Home Assistant), and you then see the final list and confirm it explicitly. Automation Builder's automations, external YAML automations and protected automations are never deleted.</div>
    <div class="card"><h2>Will be exported and offered for deletion <span class="muted small" id="n-chosen"></span></h2>
      <div class="row"><label class="check"><input type="checkbox" id="all" checked> All eligible ordinary automations (${eligible.length})</label></div>
      <div id="eligible">${eligible.map((a) => row(a)).join("") || `<p class="muted">There are no ordinary automations that can be deleted.</p>`}</div></div>
    ${review.length ? `<div class="card"><h2>Ownership unclear (${review.length})</h2><p>These look like Automation Builder's automations but are not in its id namespace. They are excluded unless you include them one by one.</p><div id="review">${review.map((a) => `<div class="auto"><span class="sel"><input type="checkbox" data-review="${esc(a.key)}" ${includeReview.has(a.key) ? "checked" : ""} aria-label="Include ${esc(a.alias)}"></span><div><div class="name">${esc(a.alias)}</div><div class="mono">${esc(a.entity_id)} · id ${esc(a.config_id)}</div></div><div class="side"></div><div class="meta">${automationChips(a)}${problems(a.problems, ["warning"])}</div></div>`).join("")}</div></div>` : ""}
    <div class="card"><details ${excluded.length < 8 ? "open" : ""}><summary>Excluded and never deleted (${excluded.length})</summary>${excluded.map((a) => `<div class="auto"><span class="sel">🔒</span><div><div class="name">${esc(a.alias)}</div><div class="mono">${esc(a.entity_id || "")} · id ${esc(a.config_id || "none")}</div></div><div class="side">${a.state ? chip(a.state) : ""}</div><div class="meta">${automationChips(a)}${problems(a.problems, ["error"])}</div></div>`).join("") || `<p class="muted">None.</p>`}</details></div>
    <div class="sticky-actions"><div class="row"><span class="muted" id="summary"></span><span class="spacer"></span><button class="primary" id="go">Analyse, export and verify</button></div></div>`;
  const sync = () => {
    const n = chosen.size;
    view.querySelector("#n-chosen").textContent = `${[...chosen].filter((k) => !review.some((a) => a.key === k)).length} of ${eligible.length} selected`;
    view.querySelector("#summary").textContent = `${n} automation(s) will be exported${includeReview.size ? ` (${includeReview.size} with unclear ownership)` : ""}`;
    view.querySelector("#go").disabled = !n;
    view.querySelector("#all").checked = eligible.every((a) => chosen.has(a.key));
  };
  view.querySelector("#eligible").onchange = (e) => {
    const box = e.target.closest("input[data-key]");
    if (!box) return;
    if (box.checked) chosen.add(box.dataset.key); else chosen.delete(box.dataset.key);
    sync();
  };
  view.querySelector("#all").onchange = (e) => {
    for (const a of eligible) { if (e.target.checked) chosen.add(a.key); else chosen.delete(a.key); }
    view.querySelectorAll("#eligible input[data-key]").forEach((b) => { b.checked = e.target.checked; });
    sync();
  };
  const rev = view.querySelector("#review");
  if (rev) rev.onchange = async (e) => {
    const box = e.target.closest("input[data-review]");
    if (!box) return;
    if (box.checked) {
      const ok = await ask({ title: "Include an automation with unclear ownership?", body: `<p>This automation carries Automation Builder's markers but is not in its id namespace. Include it in the cleanup only if you are sure Automation Builder does not manage it.</p>`, ok: "Include it" });
      if (!ok) { box.checked = false; return; }
      chosen.add(box.dataset.review); includeReview.add(box.dataset.review);
    } else { chosen.delete(box.dataset.review); includeReview.delete(box.dataset.review); }
    sync();
  };
  view.querySelector("#go").onclick = async (e) => {
    e.target.disabled = true;
    try {
      const op = await api("api/cleanup/start", { keys: [...chosen], include_review: [...includeReview] });
      location.hash = `#/cleanup/${enc(op.id)}`;
    } catch (err) { toast(err.message); e.target.disabled = false; }
  };
  sync();
}

export async function renderCleanup(id, op) {
  if (id === "new") return renderNewCleanup();
  op = op || await api(`api/operations/${enc(id)}`);
  const preview = op.preview || { delete: [], blocked: {} };
  const report = op.report;
  const step = { exporting: 3, awaiting_confirmation: 8, deleting: 9, completed: 12, partial: 12, failed: 12, blocked: 4, interrupted: 9, cancelled: 8 }[op.status] ?? 3;
  const items = op.items || {};
  const excluded = Object.entries((op.selection || {}).excluded || {});
  const deps = Object.entries(op.dependencies || {}).filter(([, d]) => (d.problems || []).length);
  view.innerHTML = `<h1><a href="#/" aria-label="Back">←</a> Export &amp; Delete ${chipFor(op)}</h1>${steps(step)}
    <div class="card"><dl class="kv"><dt>Started</dt><dd>${esc(clock(op.created_at))} <span class="mono">${esc(op.id)}</span></dd>${op.summary ? `<dt>Status</dt><dd>${esc(op.summary)}</dd>` : ""}${op.error ? `<dt>Error</dt><dd class="danger-text">${esc(op.error)}</dd>` : ""}</dl>
      ${op.status === "exporting" ? `<p class="muted">Exporting and verifying… this page updates by itself.</p>` : ""}
      ${op.status === "deleting" ? `<p class="muted">Deleting… this page updates by itself.</p>` : ""}
      ${op.status === "interrupted" ? `<div class="notice error">The App stopped while this operation was ${esc(op.interrupted_status)}. Check Home Assistant's current state first: it records which automations were really deleted.</div><div class="actions"><button class="primary" id="reconcile">Check the current state</button></div>` : ""}
    </div>
    ${op.archive_id ? `<div class="card"><h2>Archive</h2><p>The archive is stored in the App${op.verification ? ` and its verification is <strong>${esc(op.verification.status)}</strong>` : ""}. Download a copy before deleting.</p>
      <div class="actions"><a class="button primary" id="download" href="api/archives/${enc(op.archive_id)}/download" download>Download the archive</a><a class="button" href="#/archive/${enc(op.archive_id)}">Inspect</a>${["completed", "partial", "failed"].includes(op.status) ? `<a class="button" href="#/restore/${enc(op.archive_id)}">Restore from this archive…</a>` : ""}</div>
      ${op.verification ? `<details ${op.verification.ok ? "" : "open"}><summary>Verification checks</summary>${checksList(op.verification)}</details>` : ""}</div>` : ""}
    ${deps.length ? `<div class="card"><details><summary>Dependency warnings (${deps.length})</summary>${deps.map(([k, d]) => `<div><strong>${esc((items[k] || {}).alias || k)}</strong>${problems(d.problems)}</div>`).join("")}</details></div>` : ""}
    ${op.status === "awaiting_confirmation" ? confirmBox(op) : ""}
    ${Object.keys(preview.blocked || {}).length || excluded.length ? `<div class="card"><h2>Not deleted</h2>${[...Object.entries(preview.blocked || {}).map(([k, why]) => [k, Array.isArray(why) ? why.join("; ") : why]), ...excluded].map(([k, why]) => `<div class="problem"><span class="dot warning"></span><span><strong>${esc(((op.protected_before || {})[k] || {}).alias || (items[k] || {}).alias || k)}</strong>: ${esc(why)}</span></div>`).join("")}</div>` : ""}
    ${Object.keys(items).length ? `<div class="card"><h2>Automations</h2><div class="tscroll"><table id="op-items"><thead><tr><th>Automation</th><th>Status</th><th></th></tr></thead><tbody>${Object.entries(items).map(([k, it]) => `<tr data-key="${esc(k)}"><td>${esc(it.alias || k)}<div class="mono">${esc(k)}</div></td><td>${chip(it.status)}</td><td class="small">${esc(it.message || "")}${it.removal_verified ? " Removal verified." : ""}</td></tr>`).join("")}</tbody></table></div></div>` : ""}
    ${report ? reportCard(op, report) : ""}
    ${op.resumable ? `<div class="card"><h2>Continue</h2><p>Some automations were not deleted before the interruption.</p><div class="actions"><button class="primary" id="resume">Verify again and continue the deletion…</button><a class="button" href="#/restore/${enc(op.archive_id)}">Restore the deleted ones…</a><button id="close">Close without continuing</button></div></div>` : ""}
    <div class="card">${logList(op)}</div>`;
  bindCleanup(op);
  if (["exporting", "deleting", "running"].includes(op.status)) poll(() => { if (location.hash === `#/cleanup/${op.id}`) route(); });
}

function chipFor(op) { return chip(op.status); }

function confirmBox(op) {
  const list = op.preview.delete;
  return `<div class="card"><h2>Final deletion preview: ${list.length} automation(s)</h2>
    <p>These automations will be deleted from Home Assistant. Each one is compared with the archive again right before it is deleted; one that changed meanwhile is skipped.</p>
    <details open><summary>Show the list</summary><ol>${list.map((d) => `<li>${esc(d.alias)} <span class="mono">${esc(d.entity_id || "")} · ${esc(d.config_id)}</span></li>`).join("")}</ol></details>
    <div class="confirm-box"><label class="check"><input type="checkbox" id="ack"> I downloaded the archive, or I rely on the copy stored in this App.</label>
      <p><label>Type <strong>${list.length}</strong> to confirm: <input type="text" id="count" inputmode="numeric" autocomplete="off"></label></p>
      <div class="actions"><button class="danger solid" id="delete" disabled>Delete ${list.length} automation(s)</button><button id="cancel">Cancel – delete nothing</button></div></div></div>`;
}

function reportCard(op, r) {
  const c = r.counts || {};
  return `<div class="card"><h2>Report</h2><dl class="kv">
    <dt>Deleted</dt><dd>${esc(c.deleted || 0)}${c.gone ? ` (+${esc(c.gone)} already gone)` : ""}</dd>
    <dt>Skipped (changed after the export)</dt><dd>${esc(c.skipped_changed || 0)}</dd>
    <dt>Failed</dt><dd>${esc((c.failed || 0) + (c.still_present || 0))}</dd>
    <dt>Protected / excluded automations checked</dt><dd>${esc(r.protected_checked)} · ${r.protected_changed.length ? `<span class="danger-text">${esc(r.protected_changed.length)} changed during the operation (not by this App): ${esc(r.protected_changed.map((x) => x.alias || x.key).join(", "))}</span>` : `<span class="ok-text">all unchanged</span>`}</dd>
    <dt>Still active in Home Assistant</dt><dd>${esc(r.remaining_active.length)} automation(s)${r.external_remaining ? `, ${esc(r.external_remaining)} of them from external YAML files (not deletable here)` : ""}</dd>
  </dl>${r.remaining_active.length ? `<details><summary>Show the remaining automations</summary><ul>${r.remaining_active.map((a) => `<li>${esc(a.alias)} <span class="muted small">${esc(a.source)} · ${esc(a.state)}</span></li>`).join("")}</ul></details>` : ""}</div>`;
}

function bindCleanup(op) {
  const reconcile = view.querySelector("#reconcile");
  if (reconcile) reconcile.onclick = async () => { reconcile.disabled = true; try { await api("api/operations/reconcile", { operation_id: op.id }); route(); } catch (err) { toast(err.message); reconcile.disabled = false; } };
  const resume = view.querySelector("#resume");
  if (resume) resume.onclick = async () => { try { await api("api/cleanup/resume", { operation_id: op.id }); route(); } catch (err) { toast(err.message); } };
  const close = view.querySelector("#close");
  if (close) close.onclick = async () => { try { await api("api/operations/cancel", { operation_id: op.id }); route(); } catch (err) { toast(err.message); } };
  const del = view.querySelector("#delete");
  if (!del) return;
  const count = view.querySelector("#count");
  const ack = view.querySelector("#ack");
  const n = op.preview.delete.length;
  const sync = () => { del.disabled = !(ack.checked && count.value.trim() === String(n)); };
  count.oninput = sync; ack.onchange = sync;
  view.querySelector("#cancel").onclick = async () => { try { await api("api/operations/cancel", { operation_id: op.id }); toast("Nothing was deleted"); route(); } catch (err) { toast(err.message); } };
  del.onclick = async () => {
    del.disabled = true;
    try {
      await api("api/cleanup/confirm", { operation_id: op.id, plan_hash: op.plan_hash, confirm_count: Number(count.value.trim()), archive_ack: ack.checked });
      route();
    } catch (err) { toast(err.message); sync(); }
  };
}
