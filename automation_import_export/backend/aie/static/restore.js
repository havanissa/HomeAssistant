// Automation Import/Export Manager - Import (upload) and Restore (preview, collisions, mapping, confirmation).
import { api, ask, chip, enc, esc, problems, toast, upload, view } from "./ui.js";
import { checksList } from "./app.js";

export async function renderImport() {
  view.innerHTML = `<h1>Import</h1>
    <div class="grid">
      <div class="card"><h2>Archive from this App</h2><p>A <code>.zip</code> archive exported by the Automation Import/Export Manager (from this or another Home Assistant). It is checked completely before anything else happens.</p>
        <div class="dropzone" id="zip-drop"><p>Drop the archive here or</p><label class="button primary">Choose a .zip file<input type="file" id="zip" class="vh" accept=".zip,application/zip"></label></div><div id="zip-result"></div></div>
      <div class="card"><h2>Home Assistant automation YAML</h2><p>An ordinary <code>automations.yaml</code> (or a file with one automation). Only the configurations are imported: such files have no labels, categories or areas. Entries with <code>!secret</code> or <code>!include</code> are refused.</p>
        <div class="dropzone" id="yaml-drop"><p>Drop the file here or</p><label class="button">Choose a .yaml file<input type="file" id="yaml" class="vh" accept=".yaml,.yml,text/yaml"></label></div><div id="yaml-result"></div></div>
    </div>`;
  const zipResult = view.querySelector("#zip-result");
  const yamlResult = view.querySelector("#yaml-result");
  const sendZip = async (file) => {
    zipResult.innerHTML = `<p class="muted">Checking ${esc(file.name)}…</p>`;
    try {
      const res = await upload("api/archives/upload", file);
      zipResult.innerHTML = `<div class="notice ${res.verification.ok ? "ok" : "warning"}">${res.duplicate ? "This archive is already stored." : "Stored."} Verification: <strong>${esc(res.verification.status)}</strong>.</div>${checksList(res.verification)}<div class="actions"><a class="button primary" href="#/restore/${enc(res.archive.archive_id)}">Preview the restoration…</a><a class="button" href="#/archive/${enc(res.archive.archive_id)}">Inspect</a></div>`;
    } catch (err) {
      zipResult.innerHTML = `<div class="notice error" id="upload-error"><strong>Refused:</strong> ${esc(err.message)}</div>${err.data && err.data.report ? checksList(err.data.report) : ""}`;
    }
  };
  const sendYaml = async (file) => {
    yamlResult.innerHTML = `<p class="muted">Reading ${esc(file.name)}…</p>`;
    try {
      if (file.size > 8 * 1024 * 1024) throw new Error("The file is larger than 8 MB");
      const text = await file.text();
      const res = await api("api/import/yaml", { filename: file.name, text });
      yamlResult.innerHTML = `<div class="notice ok">${esc(res.imported)} automation(s) read.${res.refused.length ? ` ${esc(res.refused.length)} refused.` : ""}</div>
        ${res.refused.map((r) => `<div class="problem"><span class="dot warning"></span><span>Entry ${esc(r.index)}${r.alias ? ` (${esc(r.alias)})` : ""}: ${esc(r.reason)}</span></div>`).join("")}
        <div class="actions"><a class="button primary" href="#/restore/${enc(res.archive_id)}">Preview the import…</a></div>`;
    } catch (err) {
      yamlResult.innerHTML = `<div class="notice error" id="yaml-error"><strong>Refused:</strong> ${esc(err.message)}</div>${(err.data && err.data.refused || []).map((r) => `<div class="problem"><span class="dot warning"></span><span>Entry ${esc(r.index)}: ${esc(r.reason)}</span></div>`).join("")}`;
    }
  };
  view.querySelector("#zip").onchange = (e) => e.target.files[0] && sendZip(e.target.files[0]);
  view.querySelector("#yaml").onchange = (e) => e.target.files[0] && sendYaml(e.target.files[0]);
  for (const [id, send] of [["#zip-drop", sendZip], ["#yaml-drop", sendYaml]]) {
    const zone = view.querySelector(id);
    zone.ondragover = (e) => { e.preventDefault(); zone.classList.add("over"); };
    zone.ondragleave = () => zone.classList.remove("over");
    zone.ondrop = (e) => { e.preventDefault(); zone.classList.remove("over"); if (e.dataTransfer.files[0]) send(e.dataTransfer.files[0]); };
  }
}

// ------------------------------------------------------------------ Restore
const ACTION_TEXT = { not_selected: "Not selected", create: "Create", replace: "Replace (after a backup)", new: "Restore as a new automation", metadata: "Already there: restore its metadata", skip: "Skip", blocked: "Blocked" };
const ACK_TEXT = (code, plan) => {
  const [kind, ...rest] = code.split(":");
  const ref = rest.join(":");
  const item = plan.items.find((i) => i.key === ref);
  const name = item ? `“${item.alias}”` : ref;
  return {
    drop_label: `Do not restore the label ${ref}`, drop_category: `Do not restore the category ${ref}`, drop_area: `Do not restore the area ${ref}`,
    entity_id: `${name} cannot get its original entity id back (it is taken): accept the id Home Assistant assigns`,
    copy: `${name} is restored as a separate copy with a new id (its original is maintained elsewhere)`,
    no_metadata: `${name} is restored without labels, category or area (the archive has none for it)`,
    blueprint_missing: `The blueprint ${ref} is missing: the automation stays unavailable until it is installed`,
    blueprint_differs: `The installed blueprint ${ref} differs from the archived one: the installed one is used`,
    invalid: `${name} is restored although Home Assistant reports its configuration as invalid`,
  }[kind] || code;
};

export async function renderRestore(archiveId, params) {
  const decisions = { collisions: {}, new_ids: {}, labels: {}, categories: {}, areas: {}, acknowledgements: [] };
  const preset = params && params.get("keys");
  if (preset) decisions.selection = preset.split(",").filter(Boolean);
  let plan = null;
  let busy = false;
  const load = async () => {
    try {
      plan = await api("api/restore/plan", { archive_id: archiveId, decisions });
      draw();
    } catch (err) {
      view.innerHTML = `<h1>Restore</h1><div class="notice error">${esc(err.message)}</div>`;
    }
  };
  view.innerHTML = `<h1>Restore</h1><p class="muted">Comparing the archive with Home Assistant…</p>`;

  const metaRow = (kind, entry, live) => {
    const key = kind === "categories" ? `${entry.scope}:${entry.source_id}` : entry.source_id;
    const current = entry.action === "use" ? `use:${entry.target}` : entry.action;
    const opts = [`<option value="create" ${current === "create" ? "selected" : ""}>Create “${esc(entry.name)}”</option>`,
      ...live.map((x) => `<option value="use:${esc(x.id)}" ${current === `use:${x.id}` ? "selected" : ""}>Use existing: ${esc(x.name)}${x.id === entry.source_id ? " (same id)" : ""}</option>`),
      `<option value="drop" ${current === "drop" ? "selected" : ""}>Do not restore (needs acknowledgement)</option>`,
      entry.action === "unresolved" ? `<option value="unresolved" selected disabled>Choose…</option>` : ""].join("");
    return `<div class="map-row"><div><strong>${esc(entry.name)}</strong> <span class="mono">${esc(entry.source_id)}</span>${entry.note ? `<div class="small muted">${esc(entry.note)}</div>` : ""}</div>
      <select data-map="${kind}" data-key="${esc(key)}" aria-label="${esc(entry.name)}">${opts}</select></div>`;
  };

  const draw = () => {
    const c = plan.counts;
    const writing = plan.items.filter((i) => ["create", "replace", "new", "metadata"].includes(i.action));
    const live = plan.live_choices || { labels: [], categories: [], areas: [] };
    view.innerHTML = `<h1><a href="#/archive/${enc(archiveId)}" aria-label="Back">←</a> Restore ${plan.same_installation ? chip("completed", "From this installation") : chip("incomplete", "From another installation")}</h1>
      <div class="card"><p>Archive of ${esc(plan.source.location_name || "?")} · Home Assistant ${esc(plan.source.ha_version || "?")} · created ${esc(plan.source.created_at || "")}</p>
        ${plan.same_installation ? "" : `<div class="notice warning">This archive comes from another Home Assistant installation. Check the label, category and area mapping below: ids differ between installations.</div>`}
        <div class="chips">${Object.entries(c).filter(([a, n]) => n && a !== "not_selected").map(([a, n]) => `<span class="chip">${esc(ACTION_TEXT[a])}: ${esc(n)}</span>`).join("")}</div></div>
      <div class="card"><h2>Automations</h2><div id="items">${plan.items.map((i) => itemRow(i, decisions)).join("")}</div></div>
      ${plan.labels.length + plan.categories.length + plan.areas.length ? `<div class="card"><details open><summary>Labels, categories and areas</summary>
        ${plan.labels.length ? `<h3>Labels</h3>${plan.labels.map((e) => metaRow("labels", e, live.labels)).join("")}` : ""}
        ${plan.categories.length ? `<h3>Categories</h3>${plan.categories.map((e) => metaRow("categories", e, live.categories.filter((x) => x.scope === e.scope))).join("")}` : ""}
        ${plan.areas.length ? `<h3>Areas</h3>${plan.areas.map((e) => metaRow("areas", e, live.areas)).join("")}` : ""}
      </details></div>` : ""}
      ${plan.blueprints.length ? `<div class="card"><h2>Blueprints</h2>${plan.blueprints.map((b) => `<div class="problem"><span class="dot ${b.action === "missing" ? "error" : b.ack ? "warning" : "ok"}"></span><span><span class="mono">${esc(b.path)}</span>: ${esc({ keep: "installed", install: "will be installed from the archive", missing: "missing" }[b.action])}${b.note ? ` – ${esc(b.note)}` : ""}</span></div>`).join("")}</div>` : ""}
      ${plan.acknowledgements_required.length ? `<div class="card"><h2>Deviations from an exact restoration</h2><p>Each of these must be acknowledged before the restore can run.</p>${plan.acknowledgements_required.map((a) => `<label class="check"><input type="checkbox" data-ack="${esc(a)}" ${decisions.acknowledgements.includes(a) ? "checked" : ""}> ${esc(ACK_TEXT(a, plan))}</label>`).join("<br>")}</div>` : ""}
      <div class="sticky-actions"><div class="row"><span class="muted">${plan.ready ? `${esc(writing.length)} automation(s) will be restored` : plan.acknowledgements_missing.length ? "Acknowledge the deviations above" : "Nothing to restore with these choices"}</span><span class="spacer"></span>
        <button class="primary" id="run" ${plan.ready ? "" : "disabled"}>Restore ${esc(writing.length)} automation(s)…</button></div></div>`;
    bind();
  };

  const itemRow = (i, d) => {
    const included = i.action !== "not_selected";
    const choices = i.collision === "different" ? ["skip", "replace", "new"] : i.collision === "protected" || i.collision === "external" ? ["skip", "new"] : i.collision === "identical" ? ["metadata", "new"] : null;
    const current = d.collisions[i.key] || (i.action === "blocked" ? i.action_planned || "skip" : i.action);
    return `<div class="auto" data-item="${esc(i.key)}"><span class="sel"><input type="checkbox" data-include="${esc(i.key)}" ${included ? "checked" : ""} aria-label="Include ${esc(i.alias)}"></span>
      <div><div class="name">${esc(i.alias)}</div><div class="mono">id ${esc(i.target_id || i.config_id || "–")}${i.entity_target ? ` · ${esc(i.entity_target)}` : ""}</div></div>
      <div class="side">${chip(i.action === "blocked" ? "blocked" : ["skip", "not_selected"].includes(i.action) ? "pending" : "running", ACTION_TEXT[i.action])}</div>
      <div class="meta">${i.reason ? `<div class="small">${esc(i.reason)}</div>` : ""}
        ${choices ? `<label class="small">If it exists: <select data-collision="${esc(i.key)}">${choices.map((a) => `<option value="${a}" ${current === a ? "selected" : ""}>${esc(ACTION_TEXT[a])}</option>`).join("")}</select></label>` : ""}
        ${(i.diff || []).length ? `<details><summary>Differences from Home Assistant (${i.diff.length})</summary><ul class="small mono">${i.diff.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}
        ${i.metadata ? `<div class="small muted">${[i.metadata.area ? `area ${esc(i.metadata.area)}` : "", i.metadata.categories && Object.keys(i.metadata.categories).length ? "category" : "", i.metadata.labels.length ? `${i.metadata.labels.length} label(s)` : "", i.state ? `turned ${esc(i.state)}` : "", i.metadata.disabled_by ? "disabled entity" : ""].filter(Boolean).join(" · ")}</div>` : ""}
        ${problems(i.problems)}</div></div>`;
  };

  const bind = () => {
    view.querySelectorAll("[data-include]").forEach((box) => {
      box.onchange = () => {
        const current = new Set(plan.selection);
        if (box.checked) current.add(box.dataset.include); else current.delete(box.dataset.include);
        decisions.selection = plan.items.map((i) => i.key).filter((k) => current.has(k));
        load();
      };
    });
    view.querySelectorAll("[data-collision]").forEach((sel) => { sel.onchange = () => { decisions.collisions[sel.dataset.collision] = sel.value; load(); }; });
    view.querySelectorAll("[data-map]").forEach((sel) => {
      sel.onchange = () => {
        const v = sel.value;
        decisions[sel.dataset.map][sel.dataset.key] = v.startsWith("use:") ? { action: "use", target: v.slice(4) } : { action: v };
        load();
      };
    });
    view.querySelectorAll("[data-ack]").forEach((box) => {
      box.onchange = () => {
        const set = new Set(decisions.acknowledgements);
        if (box.checked) set.add(box.dataset.ack); else set.delete(box.dataset.ack);
        decisions.acknowledgements = [...set];
        load();
      };
    });
    const run = view.querySelector("#run");
    if (run) run.onclick = async () => {
      if (busy) return;
      const writing = plan.items.filter((i) => ["create", "replace", "new", "metadata"].includes(i.action));
      const replaced = writing.filter((i) => i.action === "replace").length;
      const ok = await ask({ title: `Restore ${writing.length} automation(s)?`, body: `<p>${writing.length} automation(s) are written to Home Assistant with their labels, categories, areas, entity ids and on/off state.${replaced ? ` ${replaced} existing automation(s) are replaced; they are backed up first.` : ""} Missing labels, categories and areas are created as shown.</p><p>Afterwards everything is compared with the archive; any difference is reported as an incomplete restoration.</p>`, ok: "Restore" });
      if (!ok) return;
      busy = true;
      try {
        const op = await api("api/restore/execute", { archive_id: archiveId, decisions, plan_hash: plan.plan_hash, confirm: true });
        location.hash = `#/operation/${enc(op.id)}`;
      } catch (err) {
        busy = false;
        if (err.data && err.data.plan) { plan = err.data.plan; draw(); }
        toast(err.message);
      }
    };
  };
  await load();
}
