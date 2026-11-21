// Automation Builder - the visual rule editor: IF (triggers) -> AND/OR (conditions) -> THEN (actions).
// The rule is saved as soon as it is complete and valid; the summary and the generated YAML follow every change.

import { api, ask, change, enc, entities, entityName, errorText, esc, loadEntities, notes, pickEntity, session, toast, view } from "./ui.js";

const TRIGGER_TYPES = [["state", "State of an entity"], ["numeric", "Number crosses a threshold"], ["time", "Time of day"], ["sun", "Sunrise or sunset"], ["occupancy", "Room occupancy"]];
const CONDITION_TYPES = [["state", "Entity state"], ["numeric", "Number"], ["time", "Time"]];
const DAYS = [["mon", "Mon"], ["tue", "Tue"], ["wed", "Wed"], ["thu", "Thu"], ["fri", "Fri"], ["sat", "Sat"], ["sun", "Sun"]];
const BLANK_TRIGGER = {
  state: () => ({ type: "state", entity_id: null, to: null, from: null, for_seconds: 0 }),
  numeric: () => ({ type: "numeric", entity_id: null, above: null, below: null, for_seconds: 0 }),
  time: () => ({ type: "time", at: "07:00", weekdays: [] }),
  sun: () => ({ type: "sun", event: "sunset", offset_minutes: 0 }),
  occupancy: () => ({ type: "occupancy", entity_id: null, to: "occupied" }),
};
const BLANK_CONDITION = {
  state: () => ({ type: "state", entity_id: null, state: null, negate: false, for_seconds: 0 }),
  numeric: () => ({ type: "numeric", entity_id: null, above: null, below: null }),
  time: () => ({ type: "time", after: null, before: null, weekdays: [] }),
};
const PURPOSE = { numeric: "numeric", occupancy: "occupancy" };

const clone = (v) => JSON.parse(JSON.stringify(v));
const get = (obj, path) => path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
function set(obj, path, value) {
  const keys = path.split(".");
  const last = keys.pop();
  const holder = keys.reduce((o, k) => o[k], obj);
  if (value === undefined) delete holder[last]; else holder[last] = value;
}
const hex = (rgb) => "#" + (rgb || [255, 180, 107]).map((c) => Number(c).toString(16).padStart(2, "0")).join("");
const rgb = (value) => [1, 3, 5].map((i) => parseInt(value.slice(i, i + 2), 16));

function options(list, current) {
  return list.map(([value, label]) => `<option value="${esc(value)}" ${String(value) === String(current ?? "") ? "selected" : ""}>${esc(label)}</option>`).join("");
}
function duration(path, seconds) {
  const unit = seconds && seconds % 3600 === 0 ? 3600 : seconds && seconds % 60 === 0 ? 60 : 1;
  return `<span class="dur" data-dur="${esc(path)}"><input type="number" min="0" step="1" value="${seconds ? seconds / unit : ""}" placeholder="0" data-f="dur" aria-label="Duration">
    <select data-f="dur" aria-label="Unit">${options([[1, "s"], [60, "min"], [3600, "h"]], unit)}</select></span>`;
}
function weekdays(path, chosen) {
  return `<span class="days">${DAYS.map(([d, label]) => `<label><input type="checkbox" data-f="day" data-path="${esc(path)}" value="${d}" ${chosen.includes(d) ? "checked" : ""}>${label}</label>`).join("")}</span>`;
}
function entityButton(path, entityId, purpose) {
  const known = entityId && entities[entityId];
  const label = entityId ? `${esc(entityName(entityId))}${known && known.exists === false ? " (missing)" : ""}` : "Choose entity…";
  return `<button type="button" class="entity ${entityId ? "" : "unset"}" data-act="pick" data-path="${esc(path)}" data-purpose="${esc(purpose || "any")}" title="${esc(entityId || "")}">${label}</button>`;
}
function stateInput(path, value, entityId, placeholder) {
  const id = `dl-${path.replace(/\W/g, "-")}`;
  const suggestions = (entityId && entities[entityId] && entities[entityId].suggested_states) || [];
  return `<input type="text" class="state" list="${id}" data-f="text" data-path="${esc(path)}" value="${esc(value ?? "")}" placeholder="${esc(placeholder)}" autocomplete="off"><datalist id="${id}">${suggestions.map((s) => `<option value="${esc(s)}">`).join("")}</datalist>`;
}
const number = (path, value, attrs = "") => `<input type="number" step="any" data-f="number" data-path="${esc(path)}" value="${esc(value ?? "")}" ${attrs}>`;

export async function editorPage(deviceId, ruleId, current) {
  const [dev, catalog] = await Promise.all([api(`api/devices/${enc(deviceId)}`), session.catalog || api("api/catalog")]);
  if (!current()) return null;
  session.catalog = catalog;
  const device = dev.device;
  const stored = ruleId ? device.rules.find((r) => r.id === ruleId) : null;
  if (ruleId && !stored) throw new Error("This rule does not exist (anymore).");
  const rule = stored ? clone(stored) : {
    name: "", enabled: true, triggers: [], conditions: { type: "and", items: [] }, actions: [], retrigger: "restart",
    recheck: { enabled: true, first_seconds: null, second_seconds: null, periodic_minutes: null, override_entity: null },
  };
  const refs = [...device.targets, rule.recheck.override_entity];
  const collect = (group) => group.items.forEach((i) => (i.items ? collect(i) : refs.push(i.entity_id)));
  rule.triggers.forEach((t) => refs.push(t.entity_id));
  collect(rule.conditions);
  await loadEntities(refs);
  if (!current()) return null;

  const targetDomains = [...new Set(device.targets.map((t) => t.split(".")[0]))];
  const presets = [];  // [value "domain.service", label, preset]
  targetDomains.forEach((domain) => {
    const info = catalog.domains[domain];
    (info ? info.actions : []).forEach((p) => presets.push([p.action, targetDomains.length > 1 ? `${info.label}: ${p.label}` : p.label, p]));
  });
  const presetOf = (action) => (presets.find(([value]) => value === action.action) || [])[2];
  const custom = new WeakSet();  // actions the user entered by hand
  rule.actions.forEach((a) => {
    const preset = a.type === "service" && presetOf(a);
    const known = preset ? (preset.fields || []).map((f) => f.key) : [];
    if (a.type === "service" && (!preset || Object.keys(a.data).some((k) => !known.includes(k)) || a.target === "none")) custom.add(a);
  });
  const matching = (action) => device.targets.filter((t) => t.split(".")[0] === action.action.split(".")[0]);
  const defaultTarget = (action) => { const m = matching(action); return m.length === device.targets.length ? "all" : m; };

  // ---------------------------------------------------------------- drawing
  function triggerHtml(t, i) {
    const p = `triggers.${i}`;
    let body = "";
    if (t.type === "state") body = `${entityButton(`${p}.entity_id`, t.entity_id)} <span>changes from</span> ${stateInput(`${p}.from`, t.from, t.entity_id, "any state")} <span>to</span> ${stateInput(`${p}.to`, t.to, t.entity_id, "any state")} <span>and stays so for</span> ${duration(`${p}.for_seconds`, t.for_seconds)}`;
    else if (t.type === "numeric") body = `${entityButton(`${p}.entity_id`, t.entity_id, "numeric")} <span>rises above</span> ${number(`${p}.above`, t.above)} <span>or drops below</span> ${number(`${p}.below`, t.below)} <span>and stays so for</span> ${duration(`${p}.for_seconds`, t.for_seconds)}`;
    else if (t.type === "time") body = `<span>at</span> <input type="time" data-f="text" data-path="${p}.at" value="${esc((t.at || "").slice(0, 5))}"> <span>on</span> ${weekdays(`${p}.weekdays`, t.weekdays)} <span class="muted small">(none ticked: every day)</span>`;
    else if (t.type === "sun") body = `<select data-f="text" data-path="${p}.event">${options([["sunrise", "Sunrise"], ["sunset", "Sunset"]], t.event)}</select> <span>offset</span> ${number(`${p}.offset_minutes`, t.offset_minutes || "", 'step="1" placeholder="0"')} <span>min <span class="muted small">(negative: before)</span></span>`;
    else if (t.type === "occupancy") body = `${entityButton(`${p}.entity_id`, t.entity_id, "occupancy")} <select data-f="text" data-path="${p}.to">${options([["occupied", "becomes occupied"], ["vacant", "becomes vacant"]], t.to)}</select> <span class="muted small">Automatic rechecks are added, see Advanced.</span>`;
    return `${i ? `<div class="joiner">OR</div>` : ""}<div class="part" data-part="trigger">
      <div class="row"><select data-f="trigger-type" data-index="${i}" aria-label="Trigger type">${options(TRIGGER_TYPES, t.type)}</select><span class="spacer"></span><button type="button" class="link" data-act="remove" data-path="triggers" data-index="${i}">Remove</button></div>
      <div class="line">${body}</div></div>`;
  }

  function conditionHtml(c, path, listPath, i, joiner) {
    const join = i ? `<div class="joiner">${joiner}</div>` : "";
    if (c.items) {
      return `${join}<div class="part group"><div class="row"><select data-f="text" data-path="${path}.type" aria-label="Group">${options([["and", "All of these (AND)"], ["or", "Any of these (OR)"]], c.type)}</select><span class="spacer"></span><button type="button" class="link" data-act="remove" data-path="${listPath}" data-index="${i}">Remove group</button></div>
        ${c.items.map((item, j) => conditionHtml(item, `${path}.items.${j}`, `${path}.items`, j, c.type.toUpperCase())).join("")}
        <div class="actions"><button type="button" data-act="add-condition" data-path="${path}.items">Add condition to the group</button></div></div>`;
    }
    let body = "";
    if (c.type === "state") body = `${entityButton(`${path}.entity_id`, c.entity_id)} <select data-f="bool" data-path="${path}.negate">${options([["false", "is"], ["true", "is not"]], String(c.negate))}</select> ${stateInput(`${path}.state`, c.state, c.entity_id, "state")} <span>for at least</span> ${duration(`${path}.for_seconds`, c.for_seconds)}`;
    else if (c.type === "numeric") body = `${entityButton(`${path}.entity_id`, c.entity_id, "numeric")} <span>is above</span> ${number(`${path}.above`, c.above)} <span>and below</span> ${number(`${path}.below`, c.below)}`;
    else if (c.type === "time") body = `<span>after</span> <input type="time" data-f="text" data-path="${path}.after" value="${esc((c.after || "").slice(0, 5))}"> <span>before</span> <input type="time" data-f="text" data-path="${path}.before" value="${esc((c.before || "").slice(0, 5))}"> <span>on</span> ${weekdays(`${path}.weekdays`, c.weekdays)}`;
    return `${join}<div class="part" data-part="condition">
      <div class="row"><select data-f="condition-type" data-path="${listPath}" data-index="${i}" aria-label="Condition type">${options(CONDITION_TYPES, c.type)}</select><span class="spacer"></span><button type="button" class="link" data-act="remove" data-path="${listPath}" data-index="${i}">Remove</button></div>
      <div class="line">${body}</div></div>`;
  }

  function fieldHtml(action, field, path) {
    const value = action.data[field.key];
    const label = `<span>${esc(field.label)}</span>`;
    if (field.kind === "number") return `<label class="afield">${label} ${number(`${path}.data.${field.key}`, value, `min="${field.min ?? ""}" max="${field.max ?? ""}" step="${field.step ?? 1}" placeholder="${field.required ? "" : "keep"}"`)} ${esc(field.unit || "")}</label>`;
    if (field.kind === "color") return `<label class="afield">${label} <input type="checkbox" data-f="color-on" data-path="${path}.data.${field.key}" ${value ? "checked" : ""} aria-label="Set a colour"> <input type="color" data-f="color" data-path="${path}.data.${field.key}" value="${hex(value)}" ${value ? "" : "disabled"}></label>`;
    if (field.kind === "option") {
      const ids = action.target === "all" ? matching(action) : Array.isArray(action.target) ? action.target : [];
      const choices = [...new Set(ids.flatMap((id) => (entities[id] && entities[id].attributes && entities[id].attributes[field.attribute]) || []))].map(String);
      if (value != null && !choices.includes(String(value))) choices.unshift(String(value));
      if (!choices.length) return `<label class="afield">${label} <input type="text" data-f="text-data" data-path="${path}.data.${field.key}" value="${esc(value ?? "")}"></label>`;
      return `<label class="afield">${label} <select data-f="text-data" data-path="${path}.data.${field.key}">${options([["", "Choose…"], ...choices.map((c) => [c, c])], value)}</select></label>`;
    }
    return "";
  }

  function actionHtml(a, i) {
    const path = `actions.${i}`;
    const kind = a.type === "delay" ? "delay" : custom.has(a) ? "custom" : a.action;
    let body = "";
    if (a.type === "delay") body = `<span>Wait</span> ${duration(`${path}.seconds`, a.seconds)} <span class="muted small">then continue with the next action</span>`;
    else if (custom.has(a)) {
      body = `<label class="afield"><span>Action</span> <input type="text" data-f="text" data-path="${path}.action" value="${esc(a.action)}" placeholder="domain.action, for example notify.send" autocomplete="off" spellcheck="false"></label>
        <label class="afield"><span>Controls</span> <select data-f="custom-target" data-index="${i}">${options([["all", "The selected entities of this device"], ["none", "No entity"]], a.target === "none" ? "none" : "all")}</select></label>
        <label class="afield wide"><span>Data (JSON)</span> <textarea data-f="json" data-path="${path}.data" rows="3" spellcheck="false">${esc(Object.keys(a.data).length ? JSON.stringify(a.data, null, 1) : "")}</textarea></label>
        <div class="muted small">Automation Builder cannot check what a hand-entered action does, so it is never repeated automatically.</div>`;
    } else {
      const preset = presetOf(a);
      const m = matching(a);
      const chosen = a.target === "all" ? m : a.target;
      const who = m.length > 1 ? `<span class="days">${m.map((id) => `<label><input type="checkbox" data-f="action-target" data-index="${i}" value="${esc(id)}" ${chosen.includes(id) ? "checked" : ""}>${esc(entityName(id))}</label>`).join("")}</span>` : `<span class="muted">${esc(entityName(m[0] || ""))}</span>`;
      body = `${who}${(preset.fields || []).map((f) => fieldHtml(a, f, path)).join("")}`;
    }
    return `${i ? `<div class="joiner">THEN</div>` : ""}<div class="part" data-part="action">
      <div class="row"><select data-f="action-kind" data-index="${i}" aria-label="Action">${options([...presets.map(([v, l]) => [v, l]), ["delay", "Wait (delay)"], ["custom", "Other action…"]], kind)}</select><span class="spacer"></span><button type="button" class="link" data-act="remove" data-path="actions" data-index="${i}">Remove</button></div>
      <div class="line">${body}</div></div>`;
  }

  function advancedHtml() {
    const occupancy = rule.triggers.some((t) => t.type === "occupancy");
    const delay = rule.actions.some((a) => a.type === "delay");
    const d = catalog.settings.occupancy;
    const r = rule.recheck;
    let out = "";
    if (occupancy) {
      out += `<h3>Occupancy rechecks</h3>
        <p class="muted small">After the occupancy change, the automation looks again: is the room still in that state, do the conditions hold, and is the device as this rule leaves it? Only then, and only if nobody changed the device meanwhile, the actions run once more.</p>
        <div class="field"><label for="rc-on">Recheck automatically</label><input type="checkbox" id="rc-on" data-f="check" data-path="recheck.enabled" ${r.enabled ? "checked" : ""}></div>
        <div class="field"><label for="rc-1">First check after</label><span>${number("recheck.first_seconds", r.first_seconds, `id="rc-1" min="1" max="60" step="1" placeholder="${d.first_seconds}"`)} s</span></div>
        <div class="field"><label for="rc-2">Second check after</label><span>${number("recheck.second_seconds", r.second_seconds, `id="rc-2" min="2" max="3600" step="1" placeholder="${d.second_seconds}"`)} s</span></div>
        <div class="field"><label for="rc-p">Periodic check</label><select id="rc-p" data-f="number" data-path="recheck.periodic_minutes">${options([["", `Default (${d.periodic_minutes ? `every ${d.periodic_minutes} min` : "off"})`], [0, "Off"], ...[1, 2, 3, 5, 10, 15, 30].map((m) => [m, `Every ${m} min`])], r.periodic_minutes)}</select></div>
        <div class="field"><label>Manual override signal</label><span>${entityButton("recheck.override_entity", r.override_entity, "override")}${r.override_entity ? ` <button type="button" class="link" data-act="clear" data-path="recheck.override_entity">Clear</button>` : ""}</span>
          <div class="help">Optional. While this entity is on, the rechecks correct nothing.</div></div>
        <div id="recheck-info" class="small"></div>`;
    }
    if (delay) {
      out += `<h3>When triggered again during a wait</h3>
        <div class="field"><label for="retrigger">Then</label><select id="retrigger" data-f="text" data-path="retrigger">${options(catalog.retrigger.map((m) => [m.value, m.label]), rule.retrigger)}</select>
          <div class="help" id="retrigger-info"></div></div>`;
    }
    return out || `<p class="muted small">Nothing here applies to this rule. Occupancy rechecks appear for a “Room occupancy” trigger, the retrigger behaviour for a rule with a wait.</p>`;
  }

  let drawn = false;  // the first drawing must not inherit anything from the page shown before
  function draw() {
    const open = new Set(drawn ? [...view.querySelectorAll("details[data-keep][open]")].map((d) => d.dataset.keep) : []);
    const side = drawn ? view.querySelector("#side") : null;
    const sideHtml = side ? side.innerHTML : "";
    drawn = true;
    const top = rule.conditions;
    view.innerHTML = `<h1><a class="back" href="#/devices/${enc(deviceId)}" aria-label="Back to the device">←</a>${ruleId || rule.id ? "Edit rule" : "New rule"} <span class="muted small">for ${esc(device.name)}</span></h1>
      <div class="two"><div id="form">
        <div class="card"><div class="field"><label for="rule-name">Name</label><input type="text" id="rule-name" data-f="text" data-path="name" maxlength="80" value="${esc(rule.name)}" placeholder="For example: Motion at night"></div>
          <div class="field"><label for="rule-enabled">Enabled</label><input type="checkbox" id="rule-enabled" data-f="check" data-path="enabled" ${rule.enabled ? "checked" : ""}></div></div>
        <div class="card block if"><h2><span class="kw">IF</span> one of these happens</h2>
          ${rule.triggers.map(triggerHtml).join("") || `<p class="muted">Add what starts the rule.</p>`}
          <div class="actions"><button type="button" data-act="add-trigger">Add trigger</button></div></div>
        <div class="card block and"><h2><span class="kw">AND</span> <select data-f="text" data-path="conditions.type" aria-label="How the conditions combine">${options([["and", "all of these hold"], ["or", "any of these holds"]], top.type)}</select></h2>
          ${top.items.map((c, i) => conditionHtml(c, `conditions.items.${i}`, "conditions.items", i, top.type.toUpperCase())).join("") || `<p class="muted">Optional: without conditions the rule always runs.</p>`}
          <div class="actions"><button type="button" data-act="add-condition" data-path="conditions.items">Add condition</button><button type="button" data-act="add-group">Add group</button></div></div>
        <div class="card block then"><h2><span class="kw">THEN</span> do this</h2>
          ${rule.actions.map(actionHtml).join("") || `<p class="muted">Add what the rule does with ${esc(device.targets.map(entityName).join(", "))}.</p>`}
          <div class="actions"><button type="button" data-act="add-action">Add action</button></div></div>
        <div class="card"><details data-keep="advanced"><summary>Advanced</summary>${advancedHtml()}</details></div>
        <div class="actions"><a class="button" href="#/devices/${enc(deviceId)}">Back to ${esc(device.name)}</a><span class="spacer"></span>${rule.id ? `<button type="button" class="danger" data-act="delete">Delete rule</button>` : ""}</div>
      </div><div><div class="card sticky" id="side">${sideHtml || `<p class="muted">Checking…</p>`}</div></div></div>`;
    view.querySelectorAll("details[data-keep]").forEach((d) => { if (open.has(d.dataset.keep)) d.open = true; });
  }

  // --------------------------------------------------------------- preview and save
  let seq = 0;
  let chain = Promise.resolve();
  let timer = null;
  let last = null;

  function showPreview(p, status, cls) {
    last = p;
    const side = view.querySelector("#side");
    if (!side) return;
    const yamlOpen = side.querySelector("details[open]") !== null;
    const s = p.summary;
    const auto = p.automation;
    side.innerHTML = `<div class="save-status ${cls}" id="save-status">${status}</div>
      ${p.valid ? `<div class="ifthen big" id="summary"><span class="kw">IF</span><span>${esc(s.if)}</span>${s.and ? `<span class="kw">AND</span><span>${esc(s.and)}</span>` : ""}<span class="kw">THEN</span><span>${esc(s.then)}</span></div>` : `<div id="problems">${notes(p.problems.map((x) => ({ level: "error", message: x.message })))}</div>`}
      ${p.valid ? notes(p.conflicts) + notes(p.notes) : ""}
      ${p.valid && auto ? `<p class="small" id="auto-info">Goes into the automation <strong>${esc(auto.alias)}</strong>${auto.rules > 1 ? `, together with ${auto.rules - 1} other rule${auto.rules > 2 ? "s" : ""} of this device` : ""} (mode ${esc(auto.mode)}).${auto.role === "separate" ? ` ${esc(auto.reason)}` : ""}</p>` : ""}
      ${p.valid && !auto ? `<p class="small muted">Not part of an automation while it is disabled.</p>` : ""}
      ${p.valid && p.yaml ? `<details ${yamlOpen ? "open" : ""}><summary>Generated YAML</summary><pre class="code" id="yaml">${esc(p.yaml)}</pre></details>` : ""}
      ${p.valid && rule.id ? `<div class="actions"><a class="button primary" href="#/devices/${enc(deviceId)}">Review and deploy</a></div>` : ""}`;
    const info = view.querySelector("#recheck-info");
    if (info && p.valid) {
      const r = p.recheck;
      info.innerHTML = r.active ? `<div class="notice info">Generated checks: ${r.first_seconds} s and ${r.second_seconds} s after the change${r.periodic_minutes ? `, then every ${r.periodic_minutes} min while it lasts` : ""}.</div>`
        : `<div class="notice warning">No rechecks for this rule${r.enabled ? `, because ${esc(r.why_not || "")}` : " (turned off)"}. It still runs when the occupancy changes.</div>`;
    }
    const retrigger = view.querySelector("#retrigger-info");
    if (retrigger && p.valid && auto) retrigger.textContent = auto.role === "separate" ? auto.reason : "The rule stays in the device's shared automation.";
  }

  function sync(save) {
    const mine = ++seq;
    chain = chain.then(async () => {
      if (mine !== seq || !current()) return;
      try {
        const p = await api("api/rules/preview", { device_id: deviceId, rule });
        if (mine !== seq || !current()) return;
        if (!p.valid) { showPreview(p, `<strong>Not saved yet:</strong> the rule is not complete.`, "bad"); return; }
        if (!save) { showPreview(p, "Saved.", "ok"); return; }
        const result = await change("api/rules/save", { device_id: deviceId, rule });
        if (!rule.id) {
          rule.id = result.rule_id;
          history.replaceState(null, "", `#/rules/${enc(deviceId)}/${enc(rule.id)}`);
        }
        if (!rule.name) { rule.name = p.rule.name; const input = view.querySelector("#rule-name"); if (input && !input.value) input.value = rule.name; }
        showPreview(p, `<strong>Saved.</strong> Home Assistant gets it when you deploy.`, "ok");
      } catch (err) {
        if (current()) showPreview(last && last.valid ? last : { valid: false, problems: [] }, `<strong>Not saved:</strong> ${esc(errorText(err))}`, "bad");
      }
    });
  }
  const changed = (redraw) => {
    if (redraw) draw();
    clearTimeout(timer);
    timer = setTimeout(() => sync(true), redraw ? 0 : 350);
  };

  // ---------------------------------------------------------------- handlers
  const fields = {
    text: (el) => set(rule, el.dataset.path, el.value === "" ? null : el.value),
    "text-data": (el) => set(rule, el.dataset.path, el.value === "" ? undefined : el.value),
    number: (el) => {
      const inData = el.dataset.path.includes(".data.");
      set(rule, el.dataset.path, el.value === "" ? (inData ? undefined : null) : Number(el.value));
    },
    bool: (el) => set(rule, el.dataset.path, el.value === "true"),
    check: (el) => set(rule, el.dataset.path, el.checked),
    color: (el) => set(rule, el.dataset.path, rgb(el.value)),
    "color-on": (el) => {
      const picker = el.parentElement.querySelector("input[type=color]");
      picker.disabled = !el.checked;
      set(rule, el.dataset.path, el.checked ? rgb(picker.value) : undefined);
    },
    json: (el) => {
      try {
        const value = el.value.trim() ? JSON.parse(el.value) : {};
        if (typeof value !== "object" || Array.isArray(value) || value === null) throw new Error("not an object");
        el.classList.remove("invalid");
        set(rule, el.dataset.path, value);
      } catch { el.classList.add("invalid"); return false; }
      return true;
    },
    dur: (el) => {
      const box = el.closest("[data-dur]");
      const amount = Number(box.querySelector("input").value || 0);
      set(rule, box.dataset.dur, Math.round(amount * Number(box.querySelector("select").value)));
    },
    day: (el) => {
      const all = [...view.querySelectorAll(`input[data-f="day"][data-path="${el.dataset.path}"]`)];
      set(rule, el.dataset.path, all.filter((c) => c.checked).map((c) => c.value));
    },
  };
  const structural = {
    "trigger-type": (el) => {
      const i = Number(el.dataset.index);
      const old = rule.triggers[i];
      const fresh = BLANK_TRIGGER[el.value]();
      if ("entity_id" in fresh && old.entity_id && (el.value !== "occupancy" || old.entity_id.startsWith("binary_sensor."))) fresh.entity_id = old.entity_id;
      rule.triggers[i] = fresh;
    },
    "condition-type": (el) => {
      const list = get(rule, el.dataset.path);
      const old = list[Number(el.dataset.index)];
      const fresh = BLANK_CONDITION[el.value]();
      if ("entity_id" in fresh && old.entity_id) fresh.entity_id = old.entity_id;
      list[Number(el.dataset.index)] = fresh;
    },
    "action-kind": (el) => {
      const i = Number(el.dataset.index);
      if (el.value === "delay") rule.actions[i] = { type: "delay", seconds: 60 };
      else if (el.value === "custom") {
        const old = rule.actions[i];
        const action = { type: "service", action: old.type === "service" ? old.action : "", target: "all", data: old.type === "service" ? old.data : {} };
        custom.add(action);
        rule.actions[i] = action;
      } else {
        const action = { type: "service", action: el.value, target: "all", data: {} };
        action.target = defaultTarget(action);
        rule.actions[i] = action;
      }
    },
    "action-target": (el) => {
      const i = Number(el.dataset.index);
      const chosen = [...view.querySelectorAll(`input[data-f="action-target"][data-index="${i}"]`)].filter((c) => c.checked).map((c) => c.value);
      rule.actions[i].target = chosen.length === device.targets.length ? "all" : chosen;
    },
    "custom-target": (el) => { rule.actions[Number(el.dataset.index)].target = el.value; },
  };
  const onField = (el, isChange) => {
    const kind = el.dataset.f;
    if (structural[kind]) { if (isChange) { structural[kind](el); changed(true); } return; }
    if (!fields[kind]) return;
    if (fields[kind](el) === false) return;
    // A change of the top-level operator or a group's operator changes the joiner labels: draw again.
    changed(isChange && /(^|\.)type$/.test(el.dataset.path || ""));
  };
  const form = (e, isChange) => { const el = e.target.closest("[data-f]"); if (el) onField(el, isChange); };
  const onInput = (e) => { if (e.target.matches("input[type=text], input[type=number], textarea, input[type=color]")) form(e, false); };
  const onChange = (e) => { if (!e.target.matches("input[type=text], input[type=number], textarea")) form(e, true); };
  view.addEventListener("input", onInput);
  view.addEventListener("change", onChange);
  window.addEventListener("hashchange", () => { view.removeEventListener("input", onInput); view.removeEventListener("change", onChange); clearTimeout(timer); }, { once: true });

  draw();
  if (stored) sync(false); else showPreview({ valid: false, problems: [{ message: "Add a trigger (IF) and an action (THEN)." }] }, "A new rule: it is saved as soon as it is complete.", "");

  return {
    chg: {},
    act: {
      "add-trigger": () => { rule.triggers.push(BLANK_TRIGGER.state()); changed(true); },
      "add-condition": (el) => { get(rule, el.dataset.path).push(BLANK_CONDITION.state()); changed(true); },
      "add-group": () => { rule.conditions.items.push({ type: rule.conditions.type === "and" ? "or" : "and", items: [BLANK_CONDITION.state()] }); changed(true); },
      "add-action": () => {
        const first = presets[0];
        const action = first ? { type: "service", action: first[0], target: "all", data: {} } : { type: "service", action: "", target: "all", data: {} };
        if (first) action.target = defaultTarget(action); else custom.add(action);
        rule.actions.push(action);
        changed(true);
      },
      remove: (el) => { get(rule, el.dataset.path).splice(Number(el.dataset.index), 1); changed(true); },
      clear: (el) => { set(rule, el.dataset.path, null); changed(true); },
      pick: async (el) => {
        const path = el.dataset.path;
        const holder = get(rule, path.split(".").slice(0, -1).join("."));
        const purpose = el.dataset.purpose !== "any" ? el.dataset.purpose : PURPOSE[holder && holder.type] || "any";
        const chosen = await pickEntity(purpose);
        if (!chosen || !current()) return;
        await loadEntities([chosen.entity_id]);
        set(rule, path, chosen.entity_id);
        changed(true);
      },
      delete: async () => {
        if (!await ask({ title: `Delete the rule “${rule.name || "this rule"}”?`, body: "<p>The rule is removed here. Home Assistant keeps running it until you deploy.</p>", ok: "Delete", danger: true })) return;
        try {
          seq += 1;
          await chain;
          await change("api/rules/delete", { device_id: deviceId, rule_id: rule.id });
          toast("Rule deleted.");
          location.hash = `#/devices/${enc(deviceId)}`;
        } catch (err) { toast(errorText(err)); }
      },
    },
  };
}
