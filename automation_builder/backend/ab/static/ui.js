// Automation Builder - shared interface helpers. No build step, no dependencies.

export const view = document.getElementById("view");
export const banners = document.getElementById("banners");
const dialog = document.getElementById("dialog");

export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const enc = encodeURIComponent;

// The configuration revision the interface last saw: every change is checked against it, so a change made
// meanwhile in another window is refused (409) instead of overwritten.
export const session = { revision: null, user: { is_admin: false }, catalog: null };

export async function api(path, body) {
  const options = { credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json", "X-AB": "1" };
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

// A change of the configuration, with the revision this window knows.
export const change = (path, body) => api(path, { ...body, expected_revision: session.revision });

export function errorText(err) {
  if (err.status === 409 && err.data && err.data.error === "stale") return "Changed in another window meanwhile: reload the page.";
  const problems = (err.data && err.data.problems) || [];
  return problems.length ? problems.map((p) => p.message).join("; ") : err.message;
}

export function toast(text) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 4000);
}

const pad = (n) => String(n).padStart(2, "0");
export function clock(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (d.toDateString() === new Date().toDateString()) return `today ${time}`;
  return `${d.getDate()}.${d.getMonth() + 1}.${d.getFullYear()} ${time}`;
}

const CHIP = { in_sync: "ok", changes_pending: "pending", not_deployed: "pending", pending: "pending", modified: "bad", missing: "bad", foreign: "bad", unknown: "off", nothing: "off", off: "off" };
export const chip = (status, text) => `<span class="status ${CHIP[status] || "off"}" data-status="${esc(status)}">${esc(text)}</span>`;

export function notes(items) {
  if (!items || !items.length) return "";
  return `<div>${items.map((p) => `<div class="problem"><span class="dot ${esc(p.level === "blocking" ? "error" : p.level || "warning")}"></span><span>${esc(p.message)}</span></div>`).join("")}</div>`;
}

export function renderBanners(data) {
  const out = [];
  const ha = data.connection && data.connection.home_assistant;
  if (ha && !ha.connected) out.push(`<div class="notice warning">Not connected to Home Assistant (${esc(ha.last_error || ha.state)}). Your rules stay editable; entities cannot be searched and nothing can be deployed until the connection is back.</div>`);
  if (data.recovery) out.push(`<div class="notice error">The configuration file was damaged. ${data.recovery.used ? `The copy ${esc(data.recovery.used)} is used.` : "No usable copy was found."} Details in Settings.</div>`);
  if (data.deploy_problem) out.push(`<div class="notice error">${esc(data.deploy_problem)}. Automations deployed earlier show as “not deployed by this App” until you deploy them again.</div>`);
  if (data.user && !data.user.is_admin) out.push(`<div class="notice warning">Automation Builder is for Home Assistant administrators.</div>`);
  banners.innerHTML = out.join("");
}

// ------------------------------------------------------------------ dialogs
function openDialog(html, wide = false) {
  dialog.className = wide ? "wide" : "";
  dialog.innerHTML = html;
  if (!dialog.open) dialog.showModal();
}

// A confirmation for destructive or outward-facing steps. Resolves true or false.
export function ask({ title, body = "", ok = "OK", danger = false }) {
  return new Promise((resolve) => {
    openDialog(`<h2>${esc(title)}</h2><div class="dialog-body">${body}</div>
      <div class="actions"><button data-d="cancel">Cancel</button><span class="spacer"></span><button class="${danger ? "danger" : "primary"}" data-d="ok">${esc(ok)}</button></div>`);
    const done = (value) => { dialog.onclick = null; dialog.oncancel = null; dialog.close(); resolve(value); };
    dialog.onclick = (e) => { const d = e.target.closest("[data-d]"); if (d) done(d.dataset.d === "ok"); };
    dialog.oncancel = (e) => { e.preventDefault(); done(false); };
  });
}

const PURPOSE_TITLE = {
  target: "Choose an entity to control", occupancy: "Choose an occupancy sensor", numeric: "Choose a sensor with a number",
  override: "Choose the manual override signal", any: "Choose an entity", button: "Choose a button or remote (event entity)",
  helper: "Choose a helper", script: "Choose a script", scene: "Choose a scene", notify: "Choose where the notification goes",
  time: "Choose a date/time helper", select: "Choose a dropdown helper",
};

// The entity picker: search by name, area, device or entity id. Resolves the chosen entity (or null).
export function pickEntity(purpose = "any") {
  return new Promise((resolve) => {
    openDialog(`<h2>${esc(PURPOSE_TITLE[purpose] || PURPOSE_TITLE.any)}</h2>
      <input type="search" id="pick-q" placeholder="Search by name, area, device or entity id" autocomplete="off">
      <div class="results" id="pick-results"><div class="muted">Searching…</div></div>
      <div class="actions"><button data-d="cancel">Cancel</button></div>`, true);
    const input = dialog.querySelector("#pick-q");
    const box = dialog.querySelector("#pick-results");
    let found = [];
    let seq = 0;
    const done = (value) => { dialog.onclick = null; dialog.oncancel = null; input.oninput = null; dialog.close(); resolve(value); };
    const search = async () => {
      const mine = ++seq;
      try {
        const data = await api(`api/entities?purpose=${enc(purpose)}&q=${enc(input.value)}`);
        if (mine !== seq) return;
        found = data.results;
        box.innerHTML = found.length ? found.map((e, i) => `<button class="result" data-i="${i}">
            <span><strong>${esc(e.name)}</strong> <span class="muted small">${esc([e.area, e.device_name].filter(Boolean).join(" · "))}</span><br><span class="mono">${esc(e.entity_id)}</span></span>
            <span class="spacer"></span><span class="badge">${esc(e.state ?? "?")}${e.unit ? " " + esc(e.unit) : ""}</span></button>`).join("")
          : `<div class="muted">Nothing found.</div>`;
      } catch (err) {
        if (mine === seq) box.innerHTML = `<div class="notice error">${esc(err.message)}</div>`;
      }
    };
    let timer = null;
    input.oninput = () => { clearTimeout(timer); timer = setTimeout(search, 200); };
    dialog.onclick = (e) => {
      const result = e.target.closest("[data-i]");
      if (result) return done(found[Number(result.dataset.i)]);
      if (e.target.closest("[data-d]")) done(null);
    };
    dialog.oncancel = (e) => { e.preventDefault(); done(null); };
    search();
    input.focus();
  });
}

// The device picker (for device triggers and conditions): every Home Assistant device. Resolves the device or null.
export function pickDevice() {
  return new Promise((resolve) => {
    openDialog(`<h2>Choose a device</h2>
      <input type="search" id="pick-q" placeholder="Search by name, area, manufacturer or model" autocomplete="off">
      <div class="results" id="pick-results"><div class="muted">Searching…</div></div>
      <div class="actions"><button data-d="cancel">Cancel</button></div>`, true);
    const input = dialog.querySelector("#pick-q");
    const box = dialog.querySelector("#pick-results");
    let found = [];
    let seq = 0;
    const done = (value) => { dialog.onclick = null; dialog.oncancel = null; input.oninput = null; dialog.close(); resolve(value); };
    const search = async () => {
      const mine = ++seq;
      try {
        const data = await api(`api/all-devices?q=${enc(input.value)}`);
        if (mine !== seq) return;
        found = data.devices;
        box.innerHTML = found.length ? found.map((d, i) => `<button class="result" data-i="${i}"><span><strong>${esc(d.name)}</strong><br><span class="muted small">${esc([d.area, d.model].filter(Boolean).join(" · "))}</span></span></button>`).join("")
          : `<div class="muted">Nothing found.</div>`;
      } catch (err) {
        if (mine === seq) box.innerHTML = `<div class="notice error">${esc(err.message)}</div>`;
      }
    };
    let timer = null;
    input.oninput = () => { clearTimeout(timer); timer = setTimeout(search, 200); };
    dialog.onclick = (e) => {
      const result = e.target.closest("[data-i]");
      if (result) return done(found[Number(result.dataset.i)]);
      if (e.target.closest("[data-d]")) done(null);
    };
    dialog.oncancel = (e) => { e.preventDefault(); done(null); };
    search();
    input.focus();
  });
}

// Entity names, states and options the interface has seen (for labels and for the choices in selectors).
export const entities = {};
export async function loadEntities(ids) {
  const missing = [...new Set(ids)].filter((id) => id && !entities[id]);
  if (!missing.length) return;
  try {
    const data = await api(`api/entity?ids=${enc(missing.join(","))}`);
    Object.assign(entities, data.entities);
  } catch { /* names are a convenience: the ids are shown instead */ }
}
export const entityName = (id) => (entities[id] && entities[id].name) || id;
