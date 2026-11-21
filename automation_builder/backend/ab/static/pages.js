// Automation Builder - the Phase 0.2 workflows: blueprint export, import of existing automations, mood rules.

import { api, ask, change, chip, clock, enc, entities, errorText, esc, loadEntities, notes, pickEntity, renderBanners, session, toast, view } from "./ui.js";

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const LEVEL = { ok: "info", warning: "warning", error: "error" };

// ------------------------------------------------------------------ blueprint export
function defaultText(input) {
  const v = input.default;
  if (v && typeof v === "object" && !Array.isArray(v) && "seconds" in v) return [v.hours && `${v.hours} h`, v.minutes && `${v.minutes} min`, (v.seconds || (!v.hours && !v.minutes)) && `${v.seconds} s`].filter(Boolean).join(" ");
  return typeof v === "object" ? JSON.stringify(v) : String(v);
}

export async function blueprintPage(deviceId, current) {
  let data = await api(`api/blueprint/${enc(deviceId)}`);
  if (!current()) return null;
  let settings = { name: data.settings.name || "", description: data.settings.description || "", automation: data.settings.automation || null, inputs: { ...(data.settings.inputs || {}) } };
  let timer = null;
  let chain = Promise.resolve();

  function inputRow(i) {
    const custom = settings.inputs[i.key] || {};
    return `<div class="bp-input ${i.exposed ? "on" : ""}" data-input="${esc(i.key)}">
      <label class="pick"><input type="checkbox" data-chg="expose" data-key="${esc(i.key)}" ${i.exposed ? "checked" : ""}>
        <span><strong>${esc(i.shown_name)}</strong><br><span class="muted small">Now: ${esc(defaultText(i))}${i.exposed ? "" : " (stays fixed)"}</span></span></label>
      ${i.exposed ? `<div class="bp-fields">
        <label class="afield wide"><span>Name</span> <input type="text" data-inp="meta" data-key="${esc(i.key)}" data-field="name" maxlength="80" value="${esc(custom.name || "")}" placeholder="${esc(i.name)}"></label>
        <label class="afield wide"><span>Description</span> <input type="text" data-inp="meta" data-key="${esc(i.key)}" data-field="description" maxlength="300" value="${esc(custom.description || "")}" placeholder="${esc(i.description)}"></label>
        <div class="muted small">Identifier <span class="mono">${esc(i.key)}</span> · default: ${esc(defaultText(i))} · selector: ${esc(Object.keys(i.selector)[0])}</div></div>` : ""}</div>`;
  }

  function side() {
    const exposed = data.inputs.filter((i) => i.exposed).length;
    const installed = data.installed;
    return `<div class="save-status" id="bp-status"></div>
      <h2>Validation</h2><div id="bp-validation">${data.validation.map((v) => `<div class="problem" data-level="${esc(v.level)}"><span class="dot ${LEVEL[v.level]} ${v.level === "ok" ? "good" : ""}"></span><span>${esc(v.message)}</span></div>`).join("")}</div>
      <div class="actions">
        <a class="button ${data.valid ? "primary" : "disabled"}" id="bp-download" ${data.valid ? `href="api/blueprint/${enc(deviceId)}/download" download="${esc(data.filename)}"` : 'aria-disabled="true"'}>Download blueprint</a>
        <button data-act="install" id="bp-install" ${data.valid ? "" : "disabled"}>${installed ? "Install again in Home Assistant" : "Install in Home Assistant"}</button></div>
      <p class="muted small">${plural(exposed, "input")}. ${installed ? `Installed ${esc(clock(installed.installed_at))} as <span class="mono">blueprints/automation/${esc(data.path)}</span>${installed.current ? "" : " – the file there is an older version"}.` : `Installing saves the file as <span class="mono">blueprints/automation/${esc(data.path || "")}</span> through Home Assistant's own blueprint command. Nothing else changes: this device and its automation stay as they are.`}</p>
      ${data.yaml ? `<details open data-keep="bp-yaml"><summary>Generated blueprint (YAML)</summary><pre class="code" id="bp-yaml">${esc(data.yaml)}</pre></details>` : ""}`;
  }

  function draw() {
    const groups = data.groups.map((g) => `<h3>${esc(g)}</h3>${data.inputs.filter((i) => i.group === g).map(inputRow).join("")}`).join("");
    view.innerHTML = `<h1><a class="back" href="#/devices/${enc(deviceId)}" aria-label="Back to the device">←</a>Export as blueprint <span class="muted small">${esc(data.device_name)}</span></h1>
      <div class="two"><div>
        <div class="card"><div class="field"><label for="bp-name">Blueprint name</label><input type="text" id="bp-name" data-inp="top" data-field="name" maxlength="80" value="${esc(settings.name)}" placeholder="${esc(data.device_name)}"></div>
          <div class="field"><label for="bp-description">Description</label><textarea id="bp-description" data-inp="top" data-field="description" rows="2" maxlength="1000" placeholder="${esc(data.description || "")}">${esc(settings.description)}</textarea></div>
          ${data.automations.length > 1 ? `<div class="field"><label for="bp-automation">Automation</label><select id="bp-automation" data-chg="automation">${data.automations.map((a) => `<option value="${esc(a.id)}" ${data.automation && a.id === data.automation.id ? "selected" : ""}>${esc(a.alias)}</option>`).join("")}</select>
            <div class="help">A blueprint holds one automation; this device compiles into ${data.automations.length}.</div></div>` : ""}</div>
        <div class="card" id="bp-inputs"><div class="row"><h2>Configurable inputs</h2><span class="spacer"></span><button class="link" data-act="all">Select all</button><button class="link" data-act="none">Select none</button></div>
          <p class="muted small">Tick what a user of the blueprint may choose. Everything else stays exactly as in your rules. The identifiers do not change when you rename a rule or change a value.</p>
          ${groups || `<p class="muted">This device has no enabled rules to export.</p>`}</div>
      </div><div><div class="card sticky" id="bp-side">${side()}</div></div></div>`;
  }

  function save() {
    chain = chain.then(async () => {
      const status = view.querySelector("#bp-status");
      try {
        await change("api/devices/update", { device_id: deviceId, patch: { blueprint: settings } });
        data = await api(`api/blueprint/${enc(deviceId)}`);
        if (!current()) return;
        view.querySelector("#bp-side").innerHTML = side();
        const s = view.querySelector("#bp-status");
        if (s) { s.className = "save-status ok"; s.textContent = "Saved."; }
      } catch (err) {
        if (status) { status.className = "save-status bad"; status.textContent = `Not saved: ${errorText(err)}`; }
      }
    });
    return chain;
  }
  const later = () => { clearTimeout(timer); timer = setTimeout(save, 400); };
  const redraw = async () => { clearTimeout(timer); await save(); if (current()) draw(); };

  draw();
  return {
    inp: {
      top: (el) => { settings[el.dataset.field] = el.value; later(); },
      meta: (el) => { settings.inputs[el.dataset.key] = { ...(settings.inputs[el.dataset.key] || {}), [el.dataset.field]: el.value }; later(); },
    },
    chg: {
      expose: (el) => { if (el.checked) settings.inputs[el.dataset.key] = settings.inputs[el.dataset.key] || {}; else delete settings.inputs[el.dataset.key]; redraw(); },
      automation: (el) => { settings.automation = el.value; settings.inputs = {}; redraw(); },
    },
    act: {
      all: () => { data.inputs.forEach((i) => { settings.inputs[i.key] = settings.inputs[i.key] || {}; }); redraw(); },
      none: () => { settings.inputs = {}; redraw(); },
      install: async () => {
        await chain;
        const install = (overwrite) => api("api/blueprint/install", { device_id: deviceId, overwrite });
        try {
          let result;
          try { result = await install(false); } catch (err) {
            if (!(err.data && err.data.error === "exists")) throw err;
            if (!await ask({ title: "Replace the installed blueprint?", body: `<p>${esc(err.message)}</p>`, ok: "Replace", danger: !err.data.installed_by_app })) return;
            result = await install(true);
          }
          toast(result.message);
          data = await api(`api/blueprint/${enc(deviceId)}`);
          if (current()) view.querySelector("#bp-side").innerHTML = side();
        } catch (err) { toast(errorText(err)); }
      },
    },
  };
}

// ------------------------------------------------------------------ import of existing automations
export async function importListPage(current) {
  const [shell, data] = await Promise.all([api("api/overview"), api("api/automations")]);
  if (!current()) return null;
  renderBanners(shell);
  const rows = data.automations.map((a) => `<tr><td><a href="#/import/${enc(a.entity_id)}">${esc(a.name)}</a><br><span class="mono">${esc(a.entity_id)}</span></td>
    <td>${a.state === "on" ? "on" : `<span class="badge">${esc(a.state || "?")}</span>`}</td><td>${a.last_triggered ? esc(clock(a.last_triggered)) : "never"}</td>
    <td>${a.imported ? `<span class="badge">copied to <a href="#/devices/${enc(a.imported.device_id)}">${esc(a.imported.device)}</a></span>` : `<a class="button" href="#/import/${enc(a.entity_id)}">Analyse</a>`}</td></tr>`).join("");
  view.innerHTML = `<h1>Import an automation</h1>
    <div class="card"><p class="muted small">These automations exist in Home Assistant and are not managed here. Analysing one shows what it does and whether it can become a rule. An import is a copy: the original is never changed, turned off or deleted by Automation Builder.</p>
      ${data.automations.length ? `<div class="tscroll"><table id="import-list"><thead><tr><th>Automation</th><th>State</th><th>Last triggered</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : `<p class="muted">Home Assistant has no automations besides the ones managed here.</p>`}</div>`;
  return { act: {}, chg: {} };
}

export async function importPage(entityId, current) {
  const data = await api("api/import/analyse", { entity_id: entityId });
  if (!current()) return null;
  const a = data.automation;
  const s = data.summary;
  const unsupported = data.unsupported.map((u) => `<div class="problem" data-unsupported><span class="dot error"></span><span><strong>${esc(u.what)}</strong>: ${esc(u.why)}${u.path ? ` <span class="mono">${esc(u.path)}</span>` : ""}</span></div>`).join("");
  const destinations = [["new", "A new device in Automation Builder"], ...data.destinations.map((d) => [d.device_id, `Add to “${d.name}”`])];
  view.innerHTML = `<h1><a class="back" href="#/import" aria-label="Back to the list">←</a>${esc(a.name)} <span class="muted small">${esc(a.entity_id)} · mode ${esc(a.mode)} · ${a.state === "on" ? "turned on" : esc(a.state || "")}</span></h1>
    <div class="two"><div>
      <div class="card" id="import-result"><h2>${data.can_convert ? "Can be converted faithfully" : "Cannot be converted"}</h2>
        ${data.can_convert ? `<div class="ifthen big" id="summary"><span class="kw">IF</span><span>${esc(s.if)}</span>${s.and ? `<span class="kw">AND</span><span>${esc(s.and)}</span>` : ""}<span class="kw">THEN</span><span>${esc(s.then)}</span></div>`
    : `<div class="notice error">Automation Builder cannot represent everything this automation does, so it offers no conversion: nothing is ever left out silently. The automation keeps working in Home Assistant as it is.</div>`}
        ${unsupported ? `<h3>Not supported</h3>${unsupported}` : ""}
        ${data.controlled.length ? `<h3>Controlled entities</h3><ul class="plain">${data.controlled.map((c) => `<li>${esc(c.name)} <span class="mono">${esc(c.entity_id)}</span></li>`).join("")}</ul>` : ""}
        ${data.notes.length ? `<h3>What changes in the copy</h3>${notes(data.notes.map((n) => ({ level: "info", message: n })))}` : ""}
        ${data.overlaps.length ? `<h3>Overlaps with managed rules</h3>${notes(data.overlaps)}` : ""}
      </div>
      ${data.can_convert ? `<div class="card" id="import-convert"><h2>Create a copy</h2>
        <div class="field"><label for="destination">Where</label><select id="destination">${destinations.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("")}</select>
          <div class="help">A copy never joins an existing device unless you choose that here.</div></div>
        <div class="notice info">The original automation stays as it is and keeps running. The copy is not deployed by this step. Before you deploy it, turn the original off in Home Assistant, otherwise both would act; Automation Builder refuses that deployment until you do, or until you explicitly allow both.</div>
        <div class="actions"><button class="primary" data-act="convert">Create the copy</button></div></div>` : ""}
    </div><div>
      <div class="card"><h2>What it contains</h2>
        <dl class="kv"><dt>Triggers</dt><dd>${data.counts.triggers ?? "?"}</dd><dt>Conditions</dt><dd>${data.counts.conditions ?? "?"}</dd><dt>Actions</dt><dd>${data.counts.actions ?? "?"}</dd></dl>
        ${data.original_yaml ? `<details open><summary>The automation in Home Assistant (YAML)</summary><pre class="code" id="original-yaml">${esc(data.original_yaml)}</pre></details>` : ""}
        ${data.yaml ? `<details><summary>What Automation Builder would generate</summary><pre class="code" id="converted-yaml">${esc(data.yaml)}</pre></details>` : ""}
        ${a.id ? `<div class="row small"><a href="/config/automation/edit/${enc(a.id)}" target="_top">Open in Home Assistant</a></div>` : ""}</div>
    </div></div>`;
  return {
    chg: {},
    act: {
      convert: async (el) => {
        el.disabled = true;
        try {
          session.revision = data.revision;
          const result = await change("api/import/convert", { entity_id: entityId, destination: view.querySelector("#destination").value });
          toast("Copied. The original automation is unchanged and still running.");
          location.hash = `#/devices/${enc(result.device_id)}`;
        } catch (err) { toast(errorText(err)); el.disabled = false; }
      },
    },
  };
}

// ------------------------------------------------------------------ mood rules (a convenience, not a feature of its own)
export async function moodPage(deviceId, current) {
  const [dev, catalog] = await Promise.all([api(`api/devices/${enc(deviceId)}`), session.catalog || api("api/catalog")]);
  if (!current()) return null;
  session.catalog = catalog;
  const device = dev.device;
  const domains = [...new Set(device.targets.map((t) => t.split(".")[0]))];
  const presets = domains.flatMap((d) => ((catalog.domains[d] || {}).actions || []).filter((p) => p.effect !== "non_idempotent").map((p) => ({ ...p, domain: d, label: domains.length > 1 ? `${catalog.domains[d].label}: ${p.label}` : p.label })));
  let selector = null;
  const rows = {};  // option -> { action, data }

  function draw() {
    const optionsList = (selector && entities[selector] && entities[selector].attributes && entities[selector].attributes.options) || [];
    const existing = new Set(device.rules.flatMap((r) => r.triggers.filter((t) => t.type === "state" && t.entity_id === selector && t.to).map((t) => t.to)));
    view.innerHTML = `<h1><a class="back" href="#/devices/${enc(deviceId)}" aria-label="Back to the device">←</a>Rules from a dropdown helper <span class="muted small">for ${esc(device.name)}</span></h1>
      <div class="card"><p class="muted small">A shortcut for a “mood” or “scene” selector: choose a dropdown helper and what this device does for the values you pick. Each line becomes an ordinary rule of <strong>this device only</strong> (IF the helper changes to the value THEN the action), which you can edit like any other. Nothing is set up for other devices, and no naming is assumed.</p>
        <div class="field"><label>Dropdown helper</label><span><button type="button" class="entity ${selector ? "" : "unset"}" data-act="pick">${selector ? esc((entities[selector] || {}).name || selector) : "Choose helper…"}</button></span></div>
        ${selector && !optionsList.length ? `<div class="notice warning">This helper reports no options.</div>` : ""}
        ${optionsList.length ? `<div class="tscroll"><table id="mood-table"><thead><tr><th>When it becomes</th><th>This device</th><th></th></tr></thead><tbody>${optionsList.map((o) => {
    const row = rows[o] || { action: "", data: {} };
    const preset = presets.find((p) => p.action === row.action);
    const fields = preset ? (preset.fields || []).filter((f) => f.kind === "number").map((f) => `<label class="afield"><span>${esc(f.label)}</span> <input type="number" data-inp="field" data-option="${esc(o)}" data-key="${esc(f.key)}" min="${f.min ?? ""}" max="${f.max ?? ""}" step="${f.step ?? 1}" value="${esc(row.data[f.key] ?? "")}" placeholder="${f.required ? "" : "keep"}"> ${esc(f.unit || "")}</label>`).join(" ") : "";
    return `<tr><td><strong>${esc(o)}</strong>${existing.has(o) ? `<br><span class="badge">has a rule already</span>` : ""}</td>
            <td><select data-chg="action" data-option="${esc(o)}" aria-label="Action for ${esc(o)}"><option value="">No rule</option>${presets.map((p) => `<option value="${esc(p.action)}" ${p.action === row.action ? "selected" : ""}>${esc(p.label)}</option>`).join("")}</select></td><td>${fields}</td></tr>`;
  }).join("")}</tbody></table></div>
        <div class="actions"><button class="primary" data-act="create" ${Object.values(rows).some((r) => r.action) ? "" : "disabled"}>Create the rules</button><a class="button" href="#/devices/${enc(deviceId)}">Cancel</a></div>` : ""}</div>`;
  }
  draw();
  return {
    inp: { field: (el) => { const row = rows[el.dataset.option]; if (el.value === "") delete row.data[el.dataset.key]; else row.data[el.dataset.key] = Number(el.value); } },
    chg: { action: (el) => { rows[el.dataset.option] = { action: el.value, data: {} }; draw(); } },
    act: {
      pick: async () => {
        const chosen = await pickEntity("select");
        if (!chosen || !current()) return;
        delete entities[chosen.entity_id];
        await loadEntities([chosen.entity_id]);
        selector = chosen.entity_id;
        Object.keys(rows).forEach((k) => delete rows[k]);
        draw();
      },
      create: async (el) => {
        el.disabled = true;
        const name = (entities[selector] || {}).name || selector;
        let made = 0;
        try {
          for (const [option, row] of Object.entries(rows)) {
            if (!row.action) continue;
            const matching = device.targets.filter((t) => t.split(".")[0] === row.action.split(".")[0]);
            await change("api/rules/save", { device_id: deviceId, rule: {
              name: `${name}: ${option}`.slice(0, 80), enabled: true,
              triggers: [{ type: "state", entity_id: selector, to: option, from: null, for_seconds: 0 }],
              conditions: { type: "and", items: [] },
              actions: [{ type: "service", action: row.action, target: matching.length === device.targets.length ? "all" : matching, data: row.data }],
            } });
            made += 1;
          }
          toast(`${plural(made, "rule")} created. Deploy the device to apply them.`);
          location.hash = `#/devices/${enc(deviceId)}`;
        } catch (err) { toast(`${made ? `${plural(made, "rule")} created, then: ` : ""}${errorText(err)}`); el.disabled = false; }
      },
    },
  };
}

export { chip };
