// Automation Import/Export Manager - router, Overview, Archives, operation pages.
import { api, ask, bytes, chip, clock, enc, esc, initTheme, problems, renderBanners, RUNNING, toast, view } from "./ui.js";
import { renderExport, renderCleanup } from "./export.js";
import { renderImport, renderRestore } from "./restore.js";

let pollTimer = null;
export function stopPolling() { clearTimeout(pollTimer); pollTimer = null; }
export function poll(fn, ms = 1000) { stopPolling(); pollTimer = setTimeout(fn, ms); }

const PURPOSE = { export: "Export", backup: "Full backup", export_delete: "Export & Delete", pre_restore: "Before a restore", uploaded: "Uploaded", yaml_import: "YAML file" };
export const purposeText = (p) => PURPOSE[p] || p || "Archive";
const KIND = { export: "Export", cleanup: "Export & Delete", restore: "Restore", rollback: "Rollback", yaml_import: "YAML import", upload: "Upload", archive_delete: "Archive deleted" };

export function opLink(op) {
  const target = op.kind === "cleanup" ? `#/cleanup/${enc(op.id)}` : `#/operation/${enc(op.id)}`;
  return `<a href="${target}">${esc(op.title || KIND[op.kind] || op.kind)}</a>`;
}

export function verificationChip(v) {
  if (!v) return chip("pending", "Not verified");
  return chip(v.status === "verified" ? "verified" : v.status === "incomplete" ? "incomplete" : "failed",
    v.status === "verified" ? "Verified" : v.status === "incomplete" ? "Verified with exceptions" : "Verification failed");
}

export function checksList(report) {
  if (!report || !report.checks) return "";
  return `<ul class="checks">${report.checks.map((c) => `<li><div class="row">${c.ok ? chip("completed", "Passed") : chip("failed", "Failed")}<span>${esc(c.title)}</span></div>
    ${c.details && c.details.length ? `<div class="small muted">${c.details.map((d) => esc(d)).join("<br>")}${c.more ? `<br>… and ${c.more} more` : ""}</div>` : ""}</li>`).join("")}</ul>`;
}

export function logList(op) {
  const entries = (op.log || []).slice().reverse();
  if (!entries.length) return "";
  return `<details><summary>Operation log (${entries.length})</summary><div class="log">${entries.map((e) => `<div class="${esc(e.level)}"><span class="mono">${esc(clock(e.at))}</span> ${esc(e.message)}</div>`).join("")}</div></details>`;
}

// ------------------------------------------------------------------ Overview
async function renderOverview() {
  const data = await api("api/overview");
  renderBanners(data);
  if (!data.user.is_admin) { view.innerHTML = `<div class="card empty">Only Home Assistant administrators can use this App.</div>`; return; }
  const s = data.summary;
  const tile = (n, t, href) => `<a class="tile" href="${href}"><div class="n">${n ?? "–"}</div><div class="t">${esc(t)}</div></a>`;
  view.innerHTML = `
    <h1>Overview</h1>
    ${problems(data.problems, ["error", "warning"])}
    <div class="tiles">
      ${tile(s && s.total, "Automations in total", "#/export")}
      ${tile(s && s.ordinary, "Ordinary UI automations", "#/export?show=ordinary")}
      ${tile(s && s.protected, "Protected (Automation Builder, integrations)", "#/export?show=protected")}
      ${tile(s && s.review, "Ownership unclear (review)", "#/export?show=review")}
      ${tile(s && s.external, "From external YAML files", "#/export?show=external")}
      ${tile(data.archives, "Stored archives", "#/archives")}
    </div>
    <div class="card hero">
      <h2>Export &amp; Delete Legacy Automations</h2>
      <p>Archive every ordinary automation (not Automation Builder's) with its YAML, labels, categories, areas, entity ids and on/off state, verify the archive, and delete them only after you confirmed the final list. They can be restored from the archive at any time.</p>
      <div class="actions"><a class="button primary" href="#/cleanup/new" id="start-cleanup">Export &amp; Delete Legacy Automations…</a>
        <a class="button" href="#/export">Export only</a>
        <button id="backup-all">Back up all automations (including protected)</button></div>
    </div>
    ${data.awaiting.length ? `<div class="card"><h2>Waiting for your confirmation</h2><ul class="checks">${data.awaiting.map((o) => `<li>${opLink(o)} <span class="muted small">${esc(clock(o.created_at))} · ${esc(o.summary || "")}</span></li>`).join("")}</ul></div>` : ""}
    <div class="card"><h2>Recent operations</h2>${data.operations.length ? `<ul class="checks">${data.operations.map((o) => `<li><div class="row">${chip(o.status)} ${opLink(o)} <span class="muted small">${esc(clock(o.created_at))}</span></div>${o.summary ? `<div class="small muted">${esc(o.summary)}</div>` : ""}</li>`).join("")}</ul>` : `<p class="muted">Nothing yet.</p>`}</div>`;
  view.querySelector("#backup-all").onclick = async () => {
    try {
      const op = await api("api/export", { mode: "all", label: "Full backup" });
      location.hash = `#/operation/${enc(op.id)}`;
    } catch (err) { toast(err.message); }
  };
}

// ------------------------------------------------------------------ Archives
async function renderArchives() {
  const data = await api("api/archives");
  view.innerHTML = `<h1>Archives</h1>
    <p class="muted">Archives are stored in this App (and in Home Assistant's backups of it). They can contain private data from your automations: keep downloaded copies safe. Nothing is ever uploaded anywhere.</p>
    ${data.archives.length ? `<div class="card tscroll"><table id="archives"><thead><tr><th>Created</th><th>Kind</th><th>Automations</th><th>Home Assistant</th><th>Verification</th><th>Size</th><th></th></tr></thead><tbody>
    ${data.archives.map((a) => `<tr data-id="${esc(a.archive_id)}"><td>${esc(clock(a.created_at || a.uploaded_at))}<div class="mono">${esc(a.archive_id)}</div>${a.label ? `<div class="small">${esc(a.label)}</div>` : ""}</td>
      <td>${esc(purposeText(a.purpose))}${a.missing ? `<div>${chip("failed", "File missing")}</div>` : ""}</td><td>${esc(a.count ?? "?")}</td>
      <td>${esc(a.ha_version || "")}<div class="small muted">${esc(a.location_name || "")}</div></td>
      <td>${verificationChip(a.verification)}${a.complete === false ? `<div class="small muted">not every automation is complete</div>` : ""}</td><td>${esc(bytes(a.size))}</td>
      <td><div class="actions"><a class="button" href="api/archives/${enc(a.archive_id)}/download" download>Download</a><a class="button" href="#/archive/${enc(a.archive_id)}">Inspect</a><a class="button" href="#/restore/${enc(a.archive_id)}">Restore…</a><button class="danger" data-delete="${esc(a.archive_id)}">Delete</button></div></td></tr>`).join("")}
    </tbody></table></div>` : `<div class="card empty"><p>No archives yet.</p><a class="button primary" href="#/export">Export automations</a></div>`}`;
  view.querySelectorAll("[data-delete]").forEach((b) => { b.onclick = () => deleteArchive(b.dataset.delete); });
}

async function deleteArchive(id) {
  const ok = await ask({ title: "Delete this archive?", body: `<p>The archive <span class="mono">${esc(id)}</span> is removed from the App. Downloaded copies are not affected. This cannot be undone.</p>`, ok: "Delete archive", danger: true });
  if (!ok) return;
  try {
    await api("api/archives/delete", { archive_id: id, confirm: true });
    toast("Archive deleted");
    route();
  } catch (err) { toast(err.message); }
}

async function renderArchive(id) {
  const data = await api(`api/archives/${enc(id)}`);
  if (data.error) { view.innerHTML = `<h1>Archive</h1><div class="notice error">${esc(data.error)}</div>`; return; }
  const s = data.summary;
  const labels = data.labels || {};
  const cats = data.categories || {};
  const areas = data.areas || {};
  const catName = (scope, cid) => ((cats[scope] || {})[cid] || {}).name || cid;
  view.innerHTML = `<h1><a href="#/archives" aria-label="Back">←</a> Archive ${verificationChip(data.entry.verification)}</h1>
    <div class="card"><dl class="kv">
      <dt>Created</dt><dd>${esc(clock(s.created_at))} <span class="mono">${esc(s.archive_id)}</span></dd>
      <dt>Kind</dt><dd>${esc(purposeText(data.entry.purpose))}${data.entry.original_purpose ? ` (${esc(purposeText(data.entry.original_purpose))})` : ""}</dd>
      <dt>Home Assistant</dt><dd>${esc((s.home_assistant || {}).version || "?")} · ${esc((s.home_assistant || {}).location_name || "")} ${data.same_installation ? chip("completed", "This installation") : chip("incomplete", "Another installation")}</dd>
      <dt>Contents</dt><dd>${esc(s.counts.automations)} automation(s), ${esc(s.counts.complete)} complete · ${esc(s.counts.labels)} label(s) · ${esc(s.counts.categories)} categor${s.counts.categories === 1 ? "y" : "ies"} · ${esc(s.counts.areas)} area(s) · ${esc(s.counts.blueprints)} blueprint(s), ${esc(s.counts.blueprint_files)} included</dd>
      <dt>Size</dt><dd>${esc(bytes(s.size))} · SHA-256 <span class="mono">${esc(s.sha256)}</span></dd>
    </dl>
    <div class="actions"><a class="button primary" href="#/restore/${enc(id)}">Restore…</a><a class="button" href="api/archives/${enc(id)}/download" download>Download</a><button id="verify">Verify again (compare with Home Assistant)</button></div>
    <div id="verify-result"></div></div>
    ${(data.warnings || []).length ? `<div class="card"><h2>Warnings (${data.warnings.length})</h2>${problems(data.warnings)}</div>` : ""}
    <div class="card"><h2>Automations</h2>${(data.inventory || []).map((a) => {
      const assoc = (data.associations || {})[a.key] || {};
      const chips = [
        a.owner === "builder" ? `<span class="chip prot">Automation Builder</span>` : "",
        a.source === "external" ? `<span class="chip prot">External YAML (read-only copy)</span>` : "",
        a.source === "yaml_file" ? `<span class="chip">From a YAML file</span>` : "",
        assoc.area_id ? `<span class="chip area">⌂ ${esc((areas[assoc.area_id] || {}).name || assoc.area_id)}</span>` : "",
        ...Object.entries(assoc.categories || {}).map(([sc, c]) => `<span class="chip cat">▣ ${esc(catName(sc, c))}</span>`),
        ...(assoc.labels || []).map((l) => `<span class="chip label">🏷 ${esc((labels[l] || {}).name || l)}</span>`),
        a.blueprint ? `<span class="chip">◇ ${esc(a.blueprint)}</span>` : "",
      ].join("");
      return `<div class="auto"><span class="sel">${a.complete ? "✔" : "⚠"}</span><div><div class="name">${esc(a.alias)}</div><div class="mono">${esc(a.entity_id || "")} · id ${esc(a.config_id || "–")}</div></div>
        <div class="side">${assoc.state ? chip(assoc.state) : ""}${assoc.disabled_by ? chip("off", "Disabled entity") : ""}</div>
        <div class="meta"><div class="chips">${chips}</div>${a.problems.length ? `<div class="small muted">${esc(a.problems.join(", "))}</div>` : ""}</div></div>`;
    }).join("")}</div>
    <div class="card"><details><summary>Files (${Object.keys(data.files || {}).length})</summary><div class="tscroll"><table><tbody>${Object.entries(data.files || {}).map(([n, f]) => `<tr><td class="mono">${esc(n)}</td><td>${esc(bytes(f.size))}</td><td class="mono">${esc(f.sha256.slice(0, 16))}…</td></tr>`).join("")}</tbody></table></div></details>
    <details><summary>Compatibility</summary><pre class="code">${esc(JSON.stringify(data.compatibility, null, 2))}</pre></details></div>`;
  view.querySelector("#verify").onclick = async (e) => {
    e.target.disabled = true;
    try {
      const report = await api("api/archives/verify", { archive_id: id, live: true });
      view.querySelector("#verify-result").innerHTML = `<h3>Verification ${verificationChip(report)}</h3>${checksList(report)}`;
    } catch (err) { toast(err.message); }
    e.target.disabled = false;
  };
}

// ------------------------------------------------------------------ Operations
const ITEM_TEXT = { pending: "Pending", deleting: "Deleting", deleted: "Deleted", gone: "Already gone", skipped_changed: "Skipped (changed after export)", failed: "Failed", still_present: "Still present" };

export async function renderOperation(id) {
  const op = await api(`api/operations/${enc(id)}`);
  if (op.kind === "cleanup") return renderCleanup(id, op);
  const items = Object.entries(op.items || {});
  const result = op.result || {};
  const running = RUNNING.includes(op.status);
  view.innerHTML = `<h1><a href="#/" aria-label="Back">←</a> ${esc(op.title || op.kind)} ${chip(op.status)}</h1>
    <div class="card"><dl class="kv"><dt>Started</dt><dd>${esc(clock(op.created_at))} <span class="mono">${esc(op.id)}</span></dd>
      ${op.summary ? `<dt>Result</dt><dd>${esc(op.summary)}</dd>` : ""}${op.error ? `<dt>Error</dt><dd class="danger-text">${esc(op.error)}</dd>` : ""}
      ${op.archive_id ? `<dt>Archive</dt><dd><a href="#/archive/${enc(op.archive_id)}">${esc(op.archive_id)}</a> · <a href="api/archives/${enc(op.archive_id)}/download" download>Download</a></dd>` : ""}
      ${op.pre_restore_archive ? `<dt>Backup before replacing</dt><dd><a href="#/archive/${enc(op.pre_restore_archive)}">${esc(op.pre_restore_archive)}</a></dd>` : ""}
    </dl>${running ? `<p class="muted">Working… this page updates by itself.</p>` : ""}
    ${op.status === "interrupted" ? `<div class="notice error">The App stopped while this operation was ${esc(op.interrupted_status)}. Check Home Assistant's current state to record what was really done.</div><div class="actions"><button class="primary" id="reconcile">Check the current state</button></div>` : ""}
    ${op.kind === "restore" && ["completed", "incomplete", "failed", "partial"].includes(op.status) && !op.rolled_back_by ? `<div class="actions"><button class="danger" id="rollback">Roll back this restore…</button></div>` : ""}
    ${op.rolled_back_by ? `<p>Rolled back by <a href="#/operation/${enc(op.rolled_back_by)}">${esc(op.rolled_back_by)}</a>.</p>` : ""}
    ${op.kind === "restore" && op.status === "incomplete" ? `<div class="notice warning">The restoration is <strong>incomplete</strong>: something differs from the archive (see below). Run the restore again to finish it, roll it back, or fix it in Home Assistant.</div>` : ""}
    ${op.kind === "export" && op.verification ? `<h3>Verification ${verificationChip(op.verification)}</h3>${checksList(op.verification)}` : ""}
    ${op.kind === "yaml_import" && op.archive_id ? `<div class="actions"><a class="button primary" href="#/restore/${enc(op.archive_id)}">Preview the import…</a></div>` : ""}
    ${(op.refused || []).length ? `<h3>Not imported</h3>${op.refused.map((r) => `<div class="problem"><span class="dot warning"></span><span>Entry ${esc(r.index)}${r.alias ? ` (${esc(r.alias)})` : ""}: ${esc(r.reason)}</span></div>`).join("")}` : ""}
    </div>
    ${items.length ? `<div class="card"><h2>Automations</h2><div class="tscroll"><table id="op-items"><thead><tr><th>Automation</th><th>Status</th><th>Details</th></tr></thead><tbody>${items.map(([key, it]) => {
      const r = (result.results || {})[key] || {};
      const disc = it.discrepancies || r.discrepancies || [];
      return `<tr data-key="${esc(key)}"><td>${esc(it.alias || key)}<div class="mono">${esc(it.entity_id || r.entity_id || it.target_id || key)}</div></td><td>${chip(it.status, ITEM_TEXT[it.status])}</td><td class="small">${esc(it.message || "")}${disc.length ? `<ul>${disc.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : ""}</td></tr>`;
    }).join("")}</tbody></table></div>
    ${result.created && Object.values(result.created).some((v) => Object.keys(v).length) ? `<h3>Created in Home Assistant</h3><div class="small">${Object.entries(result.created).filter(([, v]) => Object.keys(v).length).map(([k, v]) => `${esc(k)}: ${Object.values(v).map(esc).join(", ")}`).join("<br>")}</div>` : ""}</div>` : ""}
    <div class="card">${logList(op)}</div>`;
  const reconcile = view.querySelector("#reconcile");
  if (reconcile) reconcile.onclick = async () => {
    reconcile.disabled = true;
    try { await api("api/operations/reconcile", { operation_id: id }); route(); } catch (err) { toast(err.message); reconcile.disabled = false; }
  };
  const rollback = view.querySelector("#rollback");
  if (rollback) rollback.onclick = async () => {
    const ok = await ask({ title: "Roll back this restore?", body: `<p>Automations this restore created are removed again (unless they changed since). Automations it replaced get their previous configuration and metadata back from the backup taken before the restore. Labels, categories and areas it created are kept.</p>`, ok: "Roll back", danger: true });
    if (!ok) return;
    try { const rb = await api("api/restore/rollback", { operation_id: id, confirm: true }); location.hash = `#/operation/${enc(rb.id)}`; } catch (err) { toast(err.message); }
  };
  if (running) poll(() => { if (location.hash === `#/operation/${id}`) route(); });
}

// ------------------------------------------------------------------ router
export async function route() {
  stopPolling();
  const hash = location.hash || "#/";
  const [path, query] = hash.slice(1).split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  const section = parts[0] || "overview";
  const nav = { overview: "overview", export: "export", cleanup: "export", import: "import", restore: "import", archives: "archives", archive: "archives", operation: "overview" }[section];
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === nav));
  try {
    if (section === "overview") await renderOverview();
    else if (section === "export") await renderExport(new URLSearchParams(query || ""));
    else if (section === "cleanup") await renderCleanup(parts[1] || "new");
    else if (section === "import") await renderImport();
    else if (section === "restore") await renderRestore(parts[1], new URLSearchParams(query || ""));
    else if (section === "archives") await renderArchives();
    else if (section === "archive") await renderArchive(parts[1]);
    else if (section === "operation") await renderOperation(parts[1]);
    else view.innerHTML = `<div class="card empty">Not found. <a href="#/">Overview</a></div>`;
  } catch (err) {
    view.innerHTML = `<div class="notice error">${esc(err.message)}</div>`;
  }
}

initTheme();
window.addEventListener("hashchange", route);
api("api/overview").then(renderBanners).catch(() => {});
route();
