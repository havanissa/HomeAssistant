// Automation Import/Export Manager - shared interface helpers. No build step, no dependencies.

export const view = document.getElementById("view");
export const banners = document.getElementById("banners");
const dialog = document.getElementById("dialog");

export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const enc = encodeURIComponent;

async function parse(response) {
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

export async function api(path, body) {
  const options = { credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json", "X-AIE": "1" };
    options.body = JSON.stringify(body);
  }
  return parse(await fetch(path, options));
}

export async function upload(path, file) {
  return parse(await fetch(path, {
    method: "POST", credentials: "same-origin", body: file,
    headers: { "Content-Type": "application/zip", "X-AIE": "1", "X-Filename": (file.name || "").replace(/[^\x20-\x7e]/g, "_") },
  }));
}

export function toast(text) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 4500);
}

const pad = (n) => String(n).padStart(2, "0");
// 24-hour clock in the browser's time zone.
export function clock(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (d.toDateString() === new Date().toDateString()) return `today ${time}`;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${time}`;
}

export function bytes(n) {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} kB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

const STATUS = {
  completed: ["ok", "Completed"], verified: ["ok", "Verified"], restored: ["ok", "Restored"], deleted: ["ok", "Deleted"],
  removed: ["ok", "Removed"], gone: ["off", "Already gone"], incomplete: ["warn", "Incomplete"], partial: ["warn", "Partial"],
  awaiting_confirmation: ["info", "Waiting for your confirmation"], blocked: ["bad", "Blocked"], failed: ["bad", "Failed"],
  interrupted: ["bad", "Interrupted"], cancelled: ["off", "Closed"], running: ["info", "Running"], exporting: ["info", "Exporting"],
  deleting: ["info", "Deleting"], restoring: ["info", "Restoring"], rolling_back: ["info", "Rolling back"],
  pending: ["off", "Pending"], skipped_changed: ["warn", "Skipped: changed"], still_present: ["bad", "Still present"],
  writing: ["info", "Writing"], written: ["info", "Written"], metadata_applied: ["info", "Metadata applied"],
  kept_changed: ["warn", "Kept (changed)"], on: ["ok", "On"], off: ["off", "Off"], unavailable: ["bad", "Unavailable"],
};
export const chip = (status, text) => {
  const [cls, label] = STATUS[status] || ["off", status || "?"];
  return `<span class="status ${cls}" data-status="${esc(status)}">${esc(text || label)}</span>`;
};
export const RUNNING = ["running", "exporting", "deleting", "restoring", "rolling_back"];

export function problems(items, levels = ["error", "warning", "info"]) {
  const shown = (items || []).filter((p) => levels.includes(p.level));
  if (!shown.length) return "";
  return `<div>${shown.map((p) => `<div class="problem"><span class="dot ${esc(p.level)}"></span><span>${esc(p.message)}</span></div>`).join("")}</div>`;
}

export function labelChips(labels) {
  return (labels || []).map((l) => `<span class="chip label" title="Label ${esc(l.id)}">🏷 ${esc(l.name || l.id)}</span>`).join("");
}

export function automationChips(a) {
  const out = [];
  if (a.owner === "builder") out.push(`<span class="chip prot">Automation Builder · protected</span>`);
  else if (a.owner === "review") out.push(`<span class="chip rev">Ownership unclear · review</span>`);
  if (a.source === "external") out.push(`<span class="chip prot">External YAML · read-only</span>`);
  if (a.source === "integration" || a.source === "orphan") out.push(`<span class="chip prot">${a.source === "orphan" ? "Registry entry only" : "Integration"} · protected</span>`);
  if (a.area) out.push(`<span class="chip area">⌂ ${esc(a.area.name || a.area.id)}</span>`);
  for (const c of a.categories || []) out.push(`<span class="chip cat">▣ ${esc(c.name || c.id)}</span>`);
  out.push(labelChips(a.labels));
  if (a.blueprint) out.push(`<span class="chip" title="Blueprint">◇ ${esc(a.blueprint)}</span>`);
  if (a.registry_disabled) out.push(`<span class="chip">Disabled entity</span>`);
  if (a.hidden) out.push(`<span class="chip">Hidden</span>`);
  return `<div class="chips">${out.join("")}</div>`;
}

export function renderBanners(data) {
  const out = [];
  const ha = data && data.connection && data.connection.home_assistant;
  if (ha && !ha.connected) out.push(`<div class="notice warning">Not connected to Home Assistant (${esc(ha.last_error || ha.state)}). Archives stay available; nothing can be exported, deleted or restored until the connection is back.</div>`);
  if (data && data.user && !data.user.is_admin) out.push(`<div class="notice warning">This App is for Home Assistant administrators.</div>`);
  for (const op of (data && data.interrupted) || []) out.push(`<div class="notice error"><strong>An operation was interrupted:</strong> ${esc(op.title || op.kind)} (${esc(clock(op.created_at))}). <a href="#/operation/${enc(op.id)}">Check the current state</a> before doing anything else.</div>`);
  banners.innerHTML = out.join("");
}

// ------------------------------------------------------------------ dialogs
function openDialog(html) {
  dialog.innerHTML = html;
  if (!dialog.open) dialog.showModal();
}

// A confirmation for destructive or outward-facing steps. Resolves true or false.
export function ask({ title, body = "", ok = "OK", danger = false }) {
  return new Promise((resolve) => {
    openDialog(`<h2>${esc(title)}</h2><div class="dialog-body">${body}</div>
      <div class="actions"><button data-d="cancel">Cancel</button><span class="spacer"></span><button class="${danger ? "danger solid" : "primary"}" data-d="ok">${esc(ok)}</button></div>`);
    const done = (value) => { dialog.onclick = null; dialog.oncancel = null; dialog.close(); resolve(value); };
    dialog.onclick = (e) => { const d = e.target.closest("[data-d]"); if (d) done(d.dataset.d === "ok"); };
    dialog.oncancel = (e) => { e.preventDefault(); done(false); };
  });
}

// ------------------------------------------------------------------ theme
export function initTheme() {
  const select = document.getElementById("theme");
  let value = "auto";
  try { value = localStorage.getItem("aie-theme") || "auto"; } catch { /* storage may be blocked */ }
  const apply = (v) => {
    if (v === "light" || v === "dark") document.documentElement.dataset.theme = v;
    else delete document.documentElement.dataset.theme;
  };
  select.value = value;
  apply(value);
  select.onchange = () => {
    apply(select.value);
    try { localStorage.setItem("aie-theme", select.value); } catch { /* not remembered */ }
  };
}
