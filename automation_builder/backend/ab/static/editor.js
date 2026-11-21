// Automation Builder - the visual rule editor: IF (triggers) -> AND/OR (conditions) -> THEN (actions).
// The rule is saved as soon as it is complete and valid; the summary and the generated YAML follow every change.
// Basic rules need only the first entries of each list; everything else is under "More" and nests on demand.

import { api, ask, change, enc, entities, entityName, errorText, esc, loadEntities, notes, pickDevice, pickEntity, session, toast, view } from "./ui.js";

const TRIGGER_BASIC = [["state", "State of an entity"], ["numeric", "Number crosses a threshold"], ["time", "Time of day"], ["sun", "Sunrise or sunset"], ["occupancy", "Room occupancy"]];
const TRIGGER_MORE = [["button", "Button or remote (event entity)"], ["device", "Device trigger (from Home Assistant)"], ["helper", "Helper changes"], ["elevation", "Sun elevation"], ["template", "Template becomes true"], ["event", "Event (entered by hand)"], ["mqtt", "MQTT message (entered by hand)"], ["pattern", "Time pattern"], ["homeassistant", "Home Assistant starts or stops"]];
const CONDITION_BASIC = [["state", "Entity state"], ["numeric", "Number"], ["time", "Time"]];
const CONDITION_MORE = [["sun", "Sun"], ["template", "Template"], ["trigger", "Which trigger started the rule"], ["device", "Device condition (from Home Assistant)"]];
const ACTION_MORE = [["if", "If … then … else"], ["choose", "Choose between branches"], ["wait_trigger", "Wait for a trigger"], ["wait_condition", "Wait until a condition holds"], ["repeat", "Repeat"], ["variables", "Set variables"], ["notify", "Send a notification"], ["script", "Run a script"], ["scene", "Activate a scene"], ["device", "Device action (from Home Assistant)"]];
const GROUPS = [["and", "All of these (AND)"], ["or", "Any of these (OR)"], ["not", "None of these (NOT)"]];
const JOIN = { and: "AND", or: "OR", not: "NOR" };
const DAYS = [["mon", "Mon"], ["tue", "Tue"], ["wed", "Wed"], ["thu", "Thu"], ["fri", "Fri"], ["sat", "Sat"], ["sun", "Sun"]];
const BLANK_TRIGGER = {
  state: () => ({ type: "state", entity_id: null, to: null, from: null, for_seconds: 0, attribute: null }),
  numeric: () => ({ type: "numeric", entity_id: null, above: null, below: null, for_seconds: 0, attribute: null }),
  time: () => ({ type: "time", at: "07:00", weekdays: [] }),
  sun: () => ({ type: "sun", event: "sunset", offset_minutes: 0 }),
  occupancy: () => ({ type: "occupancy", entity_id: null, to: "occupied" }),
  button: () => ({ type: "button", entity_id: null, event_types: [] }),
  device: () => ({ type: "device", config: {}, label: "" }),
  helper: () => ({ type: "state", entity_id: null, to: null, from: null, for_seconds: 0, attribute: null }),
  elevation: () => ({ type: "numeric", entity_id: "sun.sun", above: null, below: -4, for_seconds: 0, attribute: "elevation" }),
  template: () => ({ type: "template", value_template: "", for_seconds: 0 }),
  event: () => ({ type: "event", event_type: "", event_data: {} }),
  mqtt: () => ({ type: "mqtt", topic: "", payload: null }),
  pattern: () => ({ type: "pattern", hours: null, minutes: "/15", seconds: null }),
  homeassistant: () => ({ type: "homeassistant", event: "start" }),
};
const BLANK_CONDITION = {
  state: () => ({ type: "state", entity_id: null, state: null, negate: false, for_seconds: 0, attribute: null }),
  numeric: () => ({ type: "numeric", entity_id: null, above: null, below: null, attribute: null }),
  time: () => ({ type: "time", after: null, before: null, weekdays: [] }),
  sun: () => ({ type: "sun", after: "sunset", before: null, after_offset_minutes: 0, before_offset_minutes: 0 }),
  template: () => ({ type: "template", value_template: "" }),
  trigger: () => ({ type: "trigger", triggers: [0] }),
  device: () => ({ type: "device", config: {}, label: "" }),
};
const group = (type = "and") => ({ type, items: [] });
const BLANK_ACTION = {
  delay: () => ({ type: "delay", seconds: 60 }),
  if: () => ({ type: "if", conditions: { type: "and", items: [BLANK_CONDITION.state()] }, then: [], else: [] }),
  choose: () => ({ type: "choose", branches: [{ conditions: { type: "and", items: [BLANK_CONDITION.state()] }, actions: [] }], default: [] }),
  wait_trigger: () => ({ type: "wait_trigger", triggers: [BLANK_TRIGGER.state()], timeout_seconds: null, on_timeout: "continue" }),
  wait_condition: () => ({ type: "wait_condition", conditions: { type: "and", items: [BLANK_CONDITION.state()] }, timeout_seconds: null, on_timeout: "continue" }),
  repeat: () => ({ type: "repeat", mode: "count", count: 2, conditions: group(), actions: [] }),
  variables: () => ({ type: "variables", variables: {} }),
  notify: () => ({ type: "notify", action: "", entity_id: null, title: null, message: "" }),
  script: () => ({ type: "script", entity_id: null, wait: false, variables: {} }),
  scene: () => ({ type: "scene", entity_id: null }),
  device: () => ({ type: "device", config: {}, label: "" }),
};
const PURPOSE = { numeric: "numeric", occupancy: "occupancy", button: "button", script: "script", scene: "scene", notify: "notify" };

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
const grouped = (basic, more, current) => `${options(basic, current)}<optgroup label="More">${options(more, current)}</optgroup>`;
function duration(path, seconds, placeholder = "0") {
  const unit = seconds && seconds % 3600 === 0 ? 3600 : seconds && seconds % 60 === 0 ? 60 : 1;
  return `<span class="dur" data-dur="${esc(path)}"><input type="number" min="0" step="1" value="${seconds ? seconds / unit : ""}" placeholder="${esc(placeholder)}" data-f="dur" aria-label="Duration">
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
const text = (path, value, placeholder = "", cls = "") => `<input type="text" class="${cls}" data-f="text" data-path="${esc(path)}" value="${esc(value ?? "")}" placeholder="${esc(placeholder)}" autocomplete="off" spellcheck="false">`;
const area = (path, value, placeholder, kind = "text") => `<textarea data-f="${kind}" data-path="${esc(path)}" rows="2" spellcheck="false" placeholder="${esc(placeholder)}">${esc(value ?? "")}</textarea>`;
const jsonText = (value) => (value && Object.keys(value).length ? JSON.stringify(value, null, 1) : "");
function attributeField(path, item) {
  return item.attribute != null ? ` <label class="afield"><span>attribute</span> ${text(`${path}.attribute`, item.attribute, "for example elevation")}</label>`
    : ` <button type="button" class="link small" data-act="attribute" data-path="${esc(path)}.attribute">Use an attribute</button>`;
}

// Every entity id and action that a rule mentions, nested parts included.
function walkActions(actions, fn) {
  (actions || []).forEach((a) => {
    fn(a);
    ["then", "else", "default", "actions"].forEach((k) => Array.isArray(a[k]) && walkActions(a[k], fn));
    (a.branches || []).forEach((b) => walkActions(b.actions, fn));
  });
}
function groupsOf(action) {
  const out = [];
  if (action.conditions && action.conditions.items) out.push(action.conditions);
  (action.branches || []).forEach((b) => out.push(b.conditions));
  return out;
}
function collectEntities(rule, out) {
  const fromGroup = (g) => g.items.forEach((i) => (i.items ? fromGroup(i) : out.push(i.entity_id)));
  rule.triggers.forEach((t) => out.push(t.entity_id, t.type === "time" && String(t.at).includes(".") ? t.at : null));
  fromGroup(rule.conditions);
  walkActions(rule.actions, (a) => {
    out.push(a.entity_id);
    (a.triggers || []).forEach((t) => out.push(t.entity_id));
    groupsOf(a).forEach(fromGroup);
  });
}

export async function editorPage(deviceId, ruleId, current) {
  const [dev, catalog, notify] = await Promise.all([api(`api/devices/${enc(deviceId)}`), session.catalog || api("api/catalog"),
    session.notify || api("api/notify-targets").catch(() => ({ actions: [], entities: [] }))]);
  if (!current()) return null;
  session.catalog = catalog;
  session.notify = notify;
  const device = dev.device;
  const stored = ruleId ? device.rules.find((r) => r.id === ruleId) : null;
  if (ruleId && !stored) throw new Error("This rule does not exist (anymore).");
  const rule = stored ? clone(stored) : {
    name: "", enabled: true, triggers: [], conditions: { type: "and", items: [] }, actions: [], retrigger: "restart",
    recheck: { enabled: true, first_seconds: null, second_seconds: null, periodic_minutes: null, override_entity: null },
  };
  const refs = [...device.targets, rule.recheck.override_entity];
  collectEntities(rule, refs);
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
  walkActions(rule.actions, (a) => {
    const preset = a.type === "service" && presetOf(a);
    const known = preset ? (preset.fields || []).map((f) => f.key) : [];
    if (a.type === "service" && (!preset || Object.keys(a.data).some((k) => !known.includes(k)) || a.target === "none")) custom.add(a);
  });
  const matching = (action) => device.targets.filter((t) => t.split(".")[0] === action.action.split(".")[0]);
  const defaultTarget = (action) => { const m = matching(action); return m.length === device.targets.length ? "all" : m; };
  const targetDevices = [...new Map(device.targets.map((t) => entities[t]).filter((e) => e && e.device_id).map((e) => [e.device_id, e.device_name || e.device_id])).entries()];
  const offered = {};  // "<kind>:<device id>" -> what Home Assistant offers for that device (or an error text)
  const deviceNames = {};
  async function loadOffers(kind, deviceId2) {
    const key = `${kind}:${deviceId2}`;
    if (!deviceId2 || offered[key]) return;
    try {
      const found = await api(`api/device-automations?kind=${enc(kind)}&device_id=${enc(deviceId2)}`);
      offered[key] = found.items;
      deviceNames[deviceId2] = found.name || deviceId2;
    } catch (err) { offered[key] = errorText(err); }
  }
  const pendingOffers = [];
  rule.triggers.forEach((t) => t.type === "device" && pendingOffers.push(loadOffers("trigger", t.config.device_id)));
  const leafOffers = (g) => g.items.forEach((i) => (i.items ? leafOffers(i) : i.type === "device" && pendingOffers.push(loadOffers("condition", i.config.device_id))));
  leafOffers(rule.conditions);
  walkActions(rule.actions, (a) => {
    if (a.type === "device") pendingOffers.push(loadOffers("action", a.config.device_id));
    (a.triggers || []).forEach((t) => t.type === "device" && pendingOffers.push(loadOffers("trigger", t.config.device_id)));
    groupsOf(a).forEach(leafOffers);
  });
  await Promise.all(pendingOffers);
  if (!current()) return null;

  // ---------------------------------------------------------------- drawing
  function deviceChoice(kind, item, path) {
    const id = item.config.device_id;
    const list = offered[`${kind}:${id}`];
    const who = kind === "action"
      ? (targetDevices.length ? `<select data-f="device-own" data-path="${esc(path)}" data-kind="${kind}" aria-label="Device">${options([["", "Choose device…"], ...targetDevices], id)}</select>` : `<span class="muted small">The controlled entities belong to no Home Assistant device, so there are no device actions. Use the ready-made actions instead.</span>`)
      : `<button type="button" class="entity ${id ? "" : "unset"}" data-act="pick-device" data-path="${esc(path)}" data-kind="${kind}">${id ? esc(deviceNames[id] || id) : "Choose device…"}</button>`;
    let what = "";
    if (id && typeof list === "string") what = `<span class="notice error small">${esc(list)}</span>`;
    else if (id && list && !list.length) what = `<span class="muted small">Home Assistant offers no ${kind}s for this device.${kind === "trigger" ? " For a button or remote, try “Button or remote (event entity)” or the event entered by hand." : ""}</span>`;
    else if (id && list) {
      const currentIndex = list.findIndex((o) => JSON.stringify(o.config) === JSON.stringify(item.config));
      what = `<select data-f="device-offer" data-path="${esc(path)}" data-kind="${kind}" aria-label="${kind}">${options([["", `Choose ${kind}…`], ...list.map((o, i) => [i, o.label])], currentIndex < 0 ? "" : currentIndex)}</select>${currentIndex < 0 && item.config.type ? ` <span class="muted small">currently: ${esc(item.label)}</span>` : ""}`;
    }
    return `${who} ${what} <div class="muted small">Only what Home Assistant itself offers for the device is listed.</div>`;
  }

  function triggerBody(t, p, wait) {
    if (t.type === "state") return `${entityButton(`${p}.entity_id`, t.entity_id)} <span>changes from</span> ${stateInput(`${p}.from`, t.from, t.entity_id, "any state")} <span>to</span> ${stateInput(`${p}.to`, t.to, t.entity_id, "any state")} <span>and stays so for</span> ${duration(`${p}.for_seconds`, t.for_seconds)}${attributeField(p, t)}`;
    if (t.type === "numeric") return `${entityButton(`${p}.entity_id`, t.entity_id, "numeric")} <span>rises above</span> ${number(`${p}.above`, t.above)} <span>or drops below</span> ${number(`${p}.below`, t.below)} <span>and stays so for</span> ${duration(`${p}.for_seconds`, t.for_seconds)}${attributeField(p, t)}`;
    if (t.type === "time") {
      const helper = String(t.at || "").includes(".");
      return `<select data-f="time-source" data-path="${p}" aria-label="Time source">${options([["fixed", "at"], ["helper", "at the time of"]], helper ? "helper" : "fixed")}</select> ${helper ? entityButton(`${p}.at`, t.at, "time") : `<input type="time" data-f="text" data-path="${p}.at" value="${esc((t.at || "").slice(0, 5))}">`}${wait ? "" : ` <span>on</span> ${weekdays(`${p}.weekdays`, t.weekdays)} <span class="muted small">(none ticked: every day)</span>`}`;
    }
    if (t.type === "sun") return `<select data-f="text" data-path="${p}.event">${options([["sunrise", "Sunrise"], ["sunset", "Sunset"]], t.event)}</select> <span>offset</span> ${number(`${p}.offset_minutes`, t.offset_minutes || "", 'step="1" placeholder="0"')} <span>min <span class="muted small">(negative: before)</span></span>`;
    if (t.type === "occupancy") return `${entityButton(`${p}.entity_id`, t.entity_id, "occupancy")} <select data-f="text" data-path="${p}.to">${options([["occupied", "becomes occupied"], ["vacant", "becomes vacant"]], t.to)}</select> <span class="muted small">Automatic rechecks are added, see Advanced.</span>`;
    if (t.type === "button") {
      const kinds = [...new Set([...((t.entity_id && entities[t.entity_id] && entities[t.entity_id].attributes && entities[t.entity_id].attributes.event_types) || []), ...t.event_types])];
      const presses = wait ? `<span class="muted small">A wait reacts to any event of the button.</span>`
        : kinds.length ? `<span>reports</span> <span class="days">${kinds.map((k) => `<label><input type="checkbox" data-f="list-item" data-path="${p}.event_types" value="${esc(k)}" ${t.event_types.includes(k) ? "checked" : ""}>${esc(k)}</label>`).join("")}</span> <span class="muted small">(none ticked: any event)</span>`
          : `<span class="muted small">${t.entity_id ? "This entity lists no event types: the rule reacts to any event." : "The presses the button reports (single, double, long …) are listed once it is chosen."}</span>`;
      return `${entityButton(`${p}.entity_id`, t.entity_id, "button")} ${presses}`;
    }
    if (t.type === "device") return deviceChoice("trigger", t, p);
    if (t.type === "template") return `<label class="afield wide"><span>Template</span> ${area(`${p}.value_template`, t.value_template, "{{ states('sensor.x') | float(0) > 5 }}")}</label> <span>and stays true for</span> ${duration(`${p}.for_seconds`, t.for_seconds)}`;
    if (t.type === "event") return `<label class="afield"><span>Event type</span> ${text(`${p}.event_type`, t.event_type, "for example zha_event")}</label>
      <label class="afield wide"><span>Event data (JSON, optional)</span> ${area(`${p}.event_data`, jsonText(t.event_data), '{"device_ieee": "00:11:22:…", "command": "on"}', "json")}</label>
      <div class="muted small">Entered by hand: Automation Builder cannot check that this event exists. For ZHA remotes without device triggers use <span class="mono">zha_event</span> with the remote's <span class="mono">device_ieee</span> and <span class="mono">command</span> (see Developer tools → Events in Home Assistant).</div>`;
    if (t.type === "mqtt") return `<label class="afield wide"><span>Topic</span> ${text(`${p}.topic`, t.topic, "zigbee2mqtt/My remote/action", "long")}</label> <label class="afield"><span>Payload (optional)</span> ${text(`${p}.payload`, t.payload, "single")}</label>
      <div class="muted small">Entered by hand; needs the MQTT integration. Zigbee2MQTT publishes a button's action on <span class="mono">zigbee2mqtt/&lt;name&gt;/action</span>.</div>`;
    if (t.type === "pattern") return `<label class="afield"><span>Hours</span> ${text(`${p}.hours`, t.hours, "*", "short")}</label> <label class="afield"><span>Minutes</span> ${text(`${p}.minutes`, t.minutes, "/15", "short")}</label> <label class="afield"><span>Seconds</span> ${text(`${p}.seconds`, t.seconds, "0", "short")}</label> <span class="muted small">A number, /N for “every N”, or *.</span>`;
    if (t.type === "homeassistant") return `<span>Home Assistant</span> <select data-f="text" data-path="${p}.event">${options([["start", "starts"], ["shutdown", "shuts down"]], t.event)}</select>`;
    return "";
  }
  const triggerKind = (t) => (t.type === "numeric" && t.entity_id === "sun.sun" && t.attribute === "elevation" ? "elevation" : t.type);
  function triggerHtml(t, i, listPath = "triggers", wait = false) {
    const p = `${listPath}.${i}`;
    const more = TRIGGER_MORE;
    const basic = wait ? TRIGGER_BASIC.filter(([v]) => v !== "occupancy") : TRIGGER_BASIC;
    return `${i ? `<div class="joiner">OR</div>` : ""}<div class="part" data-part="trigger">
      <div class="row"><select data-f="trigger-type" data-path="${listPath}" data-index="${i}" aria-label="Trigger type">${grouped(basic, more, triggerKind(t))}</select><span class="spacer"></span><button type="button" class="link" data-act="remove" data-path="${listPath}" data-index="${i}">Remove</button></div>
      <div class="line">${triggerBody(t, p, wait)}</div></div>`;
  }

  function conditionHtml(c, path, listPath, i, joiner) {
    const join = i ? `<div class="joiner">${joiner}</div>` : "";
    if (c.items) {
      return `${join}<div class="part group"><div class="row"><select data-f="text" data-path="${path}.type" aria-label="Group">${options(GROUPS, c.type)}</select><span class="spacer"></span><button type="button" class="link" data-act="remove" data-path="${listPath}" data-index="${i}">Remove group</button></div>
        ${c.items.map((item, j) => conditionHtml(item, `${path}.items.${j}`, `${path}.items`, j, JOIN[c.type])).join("")}
        <div class="actions"><button type="button" data-act="add-condition" data-path="${path}.items">Add condition to the group</button></div></div>`;
    }
    let body = "";
    if (c.type === "state") body = `${entityButton(`${path}.entity_id`, c.entity_id)} <select data-f="bool" data-path="${path}.negate">${options([["false", "is"], ["true", "is not"]], String(c.negate))}</select> ${stateInput(`${path}.state`, c.state, c.entity_id, "state")} <span>for at least</span> ${duration(`${path}.for_seconds`, c.for_seconds)}${attributeField(path, c)}`;
    else if (c.type === "numeric") body = `${entityButton(`${path}.entity_id`, c.entity_id, "numeric")} <span>is above</span> ${number(`${path}.above`, c.above)} <span>and below</span> ${number(`${path}.below`, c.below)}${attributeField(path, c)}`;
    else if (c.type === "time") body = `<span>after</span> <input type="time" data-f="text" data-path="${path}.after" value="${esc((c.after || "").slice(0, 5))}"> <span>before</span> <input type="time" data-f="text" data-path="${path}.before" value="${esc((c.before || "").slice(0, 5))}"> <span>on</span> ${weekdays(`${path}.weekdays`, c.weekdays)}`;
    else if (c.type === "sun") {
      const side = (key) => `<span>${key}</span> <select data-f="text" data-path="${path}.${key}">${options([["", "(not used)"], ["sunrise", "sunrise"], ["sunset", "sunset"]], c[key])}</select> ${number(`${path}.${key}_offset_minutes`, c[`${key}_offset_minutes`] || "", 'step="1" placeholder="0" aria-label="Offset in minutes"')} <span>min</span>`;
      body = `${side("after")} ${side("before")} <span class="muted small">(offset: negative is earlier)</span>`;
    } else if (c.type === "template") body = `<label class="afield wide"><span>Template</span> ${area(`${path}.value_template`, c.value_template, "{{ is_state('person.anna', 'home') }}")}</label>`;
    else if (c.type === "trigger") {
      body = rule.triggers.length ? `<span>The rule was started by</span> <span class="days stack">${rule.triggers.map((t, j) => `<label><input type="checkbox" data-f="trigger-ref" data-path="${path}.triggers" value="${j}" ${c.triggers.includes(j) ? "checked" : ""}>${esc(triggerLabel(t, j))}</label>`).join("")}</span>` : `<span class="muted">Add triggers (IF) first.</span>`;
    } else if (c.type === "device") body = deviceChoice("condition", c, path);
    return `${join}<div class="part" data-part="condition">
      <div class="row"><select data-f="condition-type" data-path="${listPath}" data-index="${i}" aria-label="Condition type">${grouped(CONDITION_BASIC, CONDITION_MORE, c.type)}</select><span class="spacer"></span><button type="button" class="link" data-act="remove" data-path="${listPath}" data-index="${i}">Remove</button></div>
      <div class="line">${body}</div></div>`;
  }
  function triggerLabel(t, j) {
    const name = t.entity_id ? entityName(t.entity_id) : "";
    const kind = ([...TRIGGER_BASIC, ...TRIGGER_MORE].find(([v]) => v === t.type) || [null, t.type])[1];
    const detail = t.type === "button" && t.event_types.length ? ` (${t.event_types.join(", ")})` : t.type === "state" && t.to ? ` → ${t.to}` : t.type === "device" ? `: ${t.label}` : t.type === "time" ? ` ${String(t.at).slice(0, 5)}` : "";
    return `${j + 1}. ${name || kind}${detail}`;
  }
  // A condition tree that belongs to an action (if, branch, wait, repeat).
  function conditionsBlock(g, path, label) {
    return `<div class="nest"><div class="row"><span class="kw small">${esc(label)}</span><select data-f="text" data-path="${path}.type" aria-label="How the conditions combine">${options([["and", "all of these hold"], ["or", "any of these holds"], ["not", "none of these holds"]], g.type)}</select></div>
      ${g.items.map((c, i) => conditionHtml(c, `${path}.items.${i}`, `${path}.items`, i, JOIN[g.type])).join("")}
      <div class="actions"><button type="button" data-act="add-condition" data-path="${path}.items">Add condition</button><button type="button" data-act="add-group" data-path="${path}">Add group</button></div></div>`;
  }
  function sequenceBlock(list, path, label, hint) {
    return `<div class="nest"><div class="row"><span class="kw small">${esc(label)}</span>${hint ? `<span class="muted small">${esc(hint)}</span>` : ""}</div>
      ${list.map((a, i) => actionHtml(a, path, i)).join("")}
      <div class="actions"><button type="button" data-act="add-action" data-path="${path}">Add action</button></div></div>`;
  }
  function timeoutHtml(a, path) {
    return `<div class="line"><span>Wait at most</span> ${duration(`${path}.timeout_seconds`, a.timeout_seconds, "no limit")} ${a.timeout_seconds ? `<span>and then</span> <select data-f="text" data-path="${path}.on_timeout">${options([["continue", "continue with the next action"], ["stop", "skip the actions after this wait"]], a.on_timeout)}</select>` : `<span class="muted small">(empty: wait as long as it takes)</span>`}</div>`;
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

  function actionBody(a, path) {
    if (a.type === "delay") return `<span>Wait</span> ${duration(`${path}.seconds`, a.seconds)} <span class="muted small">then continue with the next action</span>`;
    if (a.type === "if") return `</div>${conditionsBlock(a.conditions, `${path}.conditions`, "IF")}${sequenceBlock(a.then, `${path}.then`, "THEN")}${sequenceBlock(a.else, `${path}.else`, "ELSE", "optional")}<div class="line">`;
    if (a.type === "choose") return `</div>${a.branches.map((b, j) => `<div class="branch"><div class="row"><strong>Branch ${j + 1}</strong><span class="muted small">${j ? "otherwise, when" : "when"}</span><span class="spacer"></span><button type="button" class="link" data-act="remove" data-path="${path}.branches" data-index="${j}">Remove branch</button></div>
        ${conditionsBlock(b.conditions, `${path}.branches.${j}.conditions`, "WHEN")}${sequenceBlock(b.actions, `${path}.branches.${j}.actions`, "DO")}</div>`).join("")}
      <div class="actions"><button type="button" data-act="add-branch" data-path="${path}.branches">Add branch</button></div>
      ${sequenceBlock(a.default, `${path}.default`, "OTHERWISE", "optional: when no branch matches")}<div class="muted small">Only the first branch whose conditions hold runs.</div><div class="line">`;
    if (a.type === "wait_trigger") return `</div><div class="nest"><div class="row"><span class="kw small">UNTIL</span><span class="muted small">one of these happens</span></div>
        ${a.triggers.map((t, j) => triggerHtml(t, j, `${path}.triggers`, true)).join("")}
        <div class="actions"><button type="button" data-act="add-trigger" data-path="${path}.triggers">Add trigger</button></div></div>${timeoutHtml(a, path)}<div class="line">`;
    if (a.type === "wait_condition") return `</div>${conditionsBlock(a.conditions, `${path}.conditions`, "UNTIL")}${timeoutHtml(a, path)}<div class="muted small">Continues at once when the conditions already hold. Entity states, numbers and templates can be watched.</div><div class="line">`;
    if (a.type === "repeat") return `<select data-f="repeat-mode" data-path="${path}" aria-label="Repeat">${options([["count", "a number of times"], ["while", "while conditions hold"], ["until", "until conditions hold"]], a.mode)}</select> ${a.mode === "count" ? `${number(`${path}.count`, a.count, 'min="1" max="1000" step="1" aria-label="Count"')} <span>times</span>` : ""}</div>
      ${a.mode !== "count" ? conditionsBlock(a.conditions, `${path}.conditions`, a.mode.toUpperCase()) : ""}${sequenceBlock(a.actions, `${path}.actions`, "REPEAT", a.mode === "count" ? "" : "needs a wait, for example a delay")}<div class="line">`;
    if (a.type === "variables") return `<label class="afield wide"><span>Variables (JSON)</span> ${area(`${path}.variables`, jsonText(a.variables), '{"level": "{{ states(\'sensor.lux\') | int(0) }}"}', "json")}</label>
      <div class="muted small">Later actions of this rule can use them in templates, for example <span class="mono">{{ level }}</span> as a value under “Other action…”.</div>`;
    if (a.type === "notify") {
      const choices = [...new Set([a.action, ...notify.actions, ...(notify.entities.length ? ["notify.send_message"] : [])].filter(Boolean))];
      return `<label class="afield"><span>Send with</span> <select data-f="notify-action" data-path="${path}">${options([["", "Choose…"], ...choices.map((c) => [c, c])], a.action)}</select></label>
        ${a.action === "notify.send_message" ? `<label class="afield"><span>To</span> ${entityButton(`${path}.entity_id`, a.entity_id, "notify")}</label>` : ""}
        <label class="afield"><span>Title (optional)</span> ${text(`${path}.title`, a.title, "", "long")}</label>
        <label class="afield wide"><span>Message</span> ${area(`${path}.message`, a.message, "Templates are allowed: The lamp was turned on by {{ trigger.to_state.name }}")}</label>`;
    }
    if (a.type === "script") return `${entityButton(`${path}.entity_id`, a.entity_id, "script")} <label><input type="checkbox" data-f="check" data-path="${path}.wait" ${a.wait ? "checked" : ""}> wait until the script has finished</label>
      <label class="afield wide"><span>Variables for the script (JSON, optional)</span> ${area(`${path}.variables`, jsonText(a.variables), '{"room": "kamar"}', "json")}</label>`;
    if (a.type === "scene") return `${entityButton(`${path}.entity_id`, a.entity_id, "scene")}`;
    if (a.type === "device") return deviceChoice("action", a, path);
    if (custom.has(a)) {
      return `<label class="afield"><span>Action</span> <input type="text" data-f="text" data-path="${path}.action" value="${esc(a.action)}" placeholder="domain.action, for example notify.send" autocomplete="off" spellcheck="false"></label>
        <label class="afield"><span>Controls</span> <select data-f="custom-target" data-path="${path}">${options([["all", "The selected entities of this device"], ["none", "No entity"]], a.target === "none" ? "none" : "all")}</select></label>
        <label class="afield wide"><span>Data (JSON, templates allowed)</span> <textarea data-f="json" data-path="${path}.data" rows="3" spellcheck="false">${esc(jsonText(a.data))}</textarea></label>
        <div class="muted small">Automation Builder cannot check what a hand-entered action does, so it is never repeated automatically.</div>`;
    }
    const preset = presetOf(a);
    const m = matching(a);
    const chosen = a.target === "all" ? m : a.target;
    const who = m.length > 1 ? `<span class="days">${m.map((id) => `<label><input type="checkbox" data-f="action-target" data-path="${path}" value="${esc(id)}" ${chosen.includes(id) ? "checked" : ""}>${esc(entityName(id))}</label>`).join("")}</span>` : `<span class="muted">${esc(entityName(m[0] || ""))}</span>`;
    return `${who}${(preset.fields || []).map((f) => fieldHtml(a, f, path)).join("")}`;
  }
  function actionHtml(a, listPath, i) {
    const path = `${listPath}.${i}`;
    const kind = a.type === "service" ? (custom.has(a) ? "custom" : a.action) : a.type;
    const nested = ["if", "choose", "wait_trigger", "wait_condition", "repeat"].includes(a.type);
    return `${i ? `<div class="joiner">THEN</div>` : ""}<div class="part ${nested ? "nested" : ""}" data-part="action" data-kind="${esc(a.type)}">
      <div class="row"><select data-f="action-kind" data-path="${listPath}" data-index="${i}" aria-label="Action">${options([...presets.map(([v, l]) => [v, l]), ["delay", "Wait (delay)"], ["custom", "Other action…"]], kind)}<optgroup label="More">${options(ACTION_MORE, kind)}</optgroup></select><span class="spacer"></span>
        <button type="button" class="link" data-act="shift" data-path="${listPath}" data-index="${i}" data-dir="-1" ${i ? "" : "disabled"} aria-label="Move up">↑</button>
        <button type="button" class="link" data-act="remove" data-path="${listPath}" data-index="${i}">Remove</button></div>
      <div class="line">${actionBody(a, path)}</div></div>`;
  }

  function advancedHtml() {
    const occupancy = rule.triggers.some((t) => t.type === "occupancy");
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
    out += `<div id="retrigger-box" ${last && last.valid && last.has_delay ? "" : "hidden"}><h3>When triggered again while it waits</h3>
        <div class="field"><label for="retrigger">Then</label><select id="retrigger" data-f="text" data-path="retrigger">${options(catalog.retrigger.map((m) => [m.value, m.label]), rule.retrigger)}</select>
          <div class="help" id="retrigger-info"></div></div></div>`;
    return `${out}<p class="muted small" id="advanced-none" ${occupancy || (last && last.valid && last.has_delay) ? "hidden" : ""}>Nothing here applies to this rule. Occupancy rechecks appear for a “Room occupancy” trigger, the retrigger behaviour for a rule that waits (a delay, a wait, an open-ended repeat).</p>`;
  }

  let drawn = false;  // the first drawing must not inherit anything from the page shown before
  let last = null;
  function draw() {
    const open = new Set(drawn ? [...view.querySelectorAll("details[data-keep][open]")].map((d) => d.dataset.keep) : []);
    const side = drawn ? view.querySelector("#side") : null;
    const sideHtml = side ? side.innerHTML : "";
    const scroll = window.scrollY;
    drawn = true;
    const top = rule.conditions;
    view.innerHTML = `<h1><a class="back" href="#/devices/${enc(deviceId)}" aria-label="Back to the device">←</a>${ruleId || rule.id ? "Edit rule" : "New rule"} <span class="muted small">for ${esc(device.name)}</span></h1>
      <div class="two"><div id="form">
        <div class="card"><div class="field"><label for="rule-name">Name</label><input type="text" id="rule-name" data-f="text" data-path="name" maxlength="80" value="${esc(rule.name)}" placeholder="For example: Motion at night"></div>
          <div class="field"><label for="rule-enabled">Enabled</label><input type="checkbox" id="rule-enabled" data-f="check" data-path="enabled" ${rule.enabled ? "checked" : ""}></div></div>
        <div class="card block if"><h2><span class="kw">IF</span> one of these happens</h2>
          ${rule.triggers.map((t, i) => triggerHtml(t, i)).join("") || `<p class="muted">Add what starts the rule.</p>`}
          <div class="actions"><button type="button" data-act="add-trigger" data-path="triggers">Add trigger</button></div></div>
        <div class="card block and"><h2><span class="kw">AND</span> <select data-f="text" data-path="conditions.type" aria-label="How the conditions combine">${options([["and", "all of these hold"], ["or", "any of these holds"], ["not", "none of these holds"]], top.type)}</select></h2>
          ${top.items.map((c, i) => conditionHtml(c, `conditions.items.${i}`, "conditions.items", i, JOIN[top.type])).join("") || `<p class="muted">Optional: without conditions the rule always runs.</p>`}
          <div class="actions"><button type="button" data-act="add-condition" data-path="conditions.items">Add condition</button><button type="button" data-act="add-group" data-path="conditions">Add group</button></div></div>
        <div class="card block then"><h2><span class="kw">THEN</span> do this</h2>
          ${rule.actions.map((a, i) => actionHtml(a, "actions", i)).join("") || `<p class="muted">Add what the rule does with ${esc(device.targets.map(entityName).join(", "))}.</p>`}
          <div class="actions"><button type="button" data-act="add-action" data-path="actions">Add action</button></div></div>
        <div class="card"><details data-keep="advanced"><summary>Advanced</summary>${advancedHtml()}</details></div>
        <div class="actions"><a class="button" href="#/devices/${enc(deviceId)}">Back to ${esc(device.name)}</a><span class="spacer"></span>${rule.id ? `<button type="button" class="danger" data-act="delete">Delete rule</button>` : ""}</div>
      </div><div><div class="card sticky" id="side">${sideHtml || `<p class="muted">Checking…</p>`}</div></div></div>`;
    view.querySelectorAll("details[data-keep]").forEach((d) => { if (open.has(d.dataset.keep)) d.open = true; });
    window.scrollTo(0, scroll);
  }

  // --------------------------------------------------------------- preview and save
  let seq = 0;
  let chain = Promise.resolve();
  let timer = null;

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
    const box = view.querySelector("#retrigger-box");
    if (box && p.valid) {
      box.hidden = !p.has_delay;
      const none = view.querySelector("#advanced-none");
      if (none) none.hidden = p.has_delay || rule.triggers.some((t) => t.type === "occupancy");
      const retrigger = view.querySelector("#retrigger-info");
      if (retrigger && auto) retrigger.textContent = auto.role === "separate" ? auto.reason : "The rule stays in the device's shared automation.";
    }
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

  // A trigger was removed: conditions that name triggers by position follow it.
  function triggerRemoved(index) {
    const fix = (g) => g.items.forEach((c) => {
      if (c.items) fix(c);
      else if (c.type === "trigger") c.triggers = c.triggers.filter((j) => j !== index).map((j) => (j > index ? j - 1 : j));
    });
    fix(rule.conditions);
    walkActions(rule.actions, (a) => groupsOf(a).forEach(fix));
  }

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
      const seconds = Math.round(amount * Number(box.querySelector("select").value));
      set(rule, box.dataset.dur, box.dataset.dur.endsWith("timeout_seconds") ? seconds || null : seconds);
    },
    day: (el) => {
      const all = [...view.querySelectorAll(`input[data-f="day"][data-path="${el.dataset.path}"]`)];
      set(rule, el.dataset.path, all.filter((c) => c.checked).map((c) => c.value));
    },
    "list-item": (el) => {
      const all = [...view.querySelectorAll(`input[data-f="list-item"][data-path="${el.dataset.path}"]`)];
      set(rule, el.dataset.path, all.filter((c) => c.checked).map((c) => c.value));
    },
    "trigger-ref": (el) => {
      const all = [...view.querySelectorAll(`input[data-f="trigger-ref"][data-path="${el.dataset.path}"]`)];
      set(rule, el.dataset.path, all.filter((c) => c.checked).map((c) => Number(c.value)));
    },
  };
  const structural = {
    "trigger-type": (el) => {
      const list = get(rule, el.dataset.path);
      const i = Number(el.dataset.index);
      const old = list[i];
      const fresh = BLANK_TRIGGER[el.value]();
      const keep = { occupancy: (id) => id.startsWith("binary_sensor."), button: (id) => id.startsWith("event."), elevation: () => false };
      if ("entity_id" in fresh && old.entity_id && (keep[el.value] || (() => true))(old.entity_id)) fresh.entity_id = old.entity_id;
      list[i] = fresh;
      if (el.value === "helper") setTimeout(() => page.act.pick({ dataset: { path: `${el.dataset.path}.${i}.entity_id`, purpose: "helper" } }), 0);
    },
    "condition-type": (el) => {
      const list = get(rule, el.dataset.path);
      const old = list[Number(el.dataset.index)];
      const fresh = BLANK_CONDITION[el.value]();
      if ("entity_id" in fresh && old.entity_id) fresh.entity_id = old.entity_id;
      list[Number(el.dataset.index)] = fresh;
    },
    "action-kind": (el) => {
      const list = get(rule, el.dataset.path);
      const i = Number(el.dataset.index);
      const old = list[i];
      if (BLANK_ACTION[el.value]) list[i] = BLANK_ACTION[el.value]();
      else if (el.value === "custom") {
        const action = { type: "service", action: old.type === "service" ? old.action : "", target: "all", data: old.type === "service" ? old.data : {} };
        custom.add(action);
        list[i] = action;
      } else {
        const action = { type: "service", action: el.value, target: "all", data: {} };
        action.target = defaultTarget(action);
        list[i] = action;
      }
    },
    "action-target": (el) => {
      const chosen = [...view.querySelectorAll(`input[data-f="action-target"][data-path="${el.dataset.path}"]`)].filter((c) => c.checked).map((c) => c.value);
      get(rule, el.dataset.path).target = chosen.length === device.targets.length ? "all" : chosen;
    },
    "custom-target": (el) => { get(rule, el.dataset.path).target = el.value; },
    "time-source": (el) => {
      if (el.value === "helper") setTimeout(() => page.act.pick({ dataset: { path: `${el.dataset.path}.at`, purpose: "time" } }), 0);
      else get(rule, el.dataset.path).at = "07:00";
    },
    "repeat-mode": (el) => {
      const a = get(rule, el.dataset.path);
      a.mode = el.value;
      a.count = el.value === "count" ? a.count || 2 : null;
      if (el.value !== "count" && !a.conditions.items.length) a.conditions = { type: "and", items: [BLANK_CONDITION.state()] };
      if (el.value !== "count" && !a.actions.some((x) => x.type === "delay")) a.actions.unshift({ type: "delay", seconds: 5 });
    },
    "notify-action": (el) => { const a = get(rule, el.dataset.path); a.action = el.value; if (el.value !== "notify.send_message") a.entity_id = null; },
    "device-own": async (el) => {
      const item = get(rule, el.dataset.path);
      item.config = el.value ? { device_id: el.value } : {};
      item.label = "";
      await loadOffers(el.dataset.kind, el.value);
    },
    "device-offer": (el) => {
      const item = get(rule, el.dataset.path);
      const offer = (offered[`${el.dataset.kind}:${item.config.device_id}`] || [])[Number(el.value)];
      if (el.value !== "" && offer) { item.config = clone(offer.config); item.label = offer.label; }
    },
  };
  const onField = async (el, isChange) => {
    const kind = el.dataset.f;
    if (structural[kind]) { if (isChange) { await structural[kind](el); if (current()) changed(true); } return; }
    if (!fields[kind]) return;
    if (fields[kind](el) === false) return;
    // A change that alters what is shown (a group's operator, a timeout that appears, ticked boxes) draws again.
    changed(isChange && (/(^|\.)(type|event|after|before)$/.test(el.dataset.path || "") || kind === "dur" && el.closest("[data-dur]").dataset.dur.endsWith("timeout_seconds")));
  };
  const form = (e, isChange) => { const el = e.target.closest("[data-f]"); if (el) onField(el, isChange); };
  const typed = "input[type=text], input[type=number], textarea, input[type=color]";
  const onInput = (e) => { if (e.target.matches(typed)) form(e, false); };
  const onChange = (e) => { if (!e.target.matches("input[type=text], input[type=number], textarea") || e.target.closest("[data-dur$=timeout_seconds]")) form(e, true); };
  view.addEventListener("input", onInput);
  view.addEventListener("change", onChange);
  window.addEventListener("hashchange", () => { view.removeEventListener("input", onInput); view.removeEventListener("change", onChange); clearTimeout(timer); }, { once: true });

  draw();
  if (stored) sync(false); else showPreview({ valid: false, problems: [{ message: "Add a trigger (IF) and an action (THEN)." }] }, "A new rule: it is saved as soon as it is complete.", "");

  const page = {
    chg: {},
    act: {
      "add-trigger": (el) => { get(rule, el.dataset.path).push(BLANK_TRIGGER.state()); changed(true); },
      "add-condition": (el) => { get(rule, el.dataset.path).push(BLANK_CONDITION.state()); changed(true); },
      "add-group": (el) => { const g = get(rule, el.dataset.path); g.items.push({ type: g.type === "and" ? "or" : "and", items: [BLANK_CONDITION.state()] }); changed(true); },
      "add-branch": (el) => { get(rule, el.dataset.path).push(BLANK_ACTION.choose().branches[0]); changed(true); },
      "add-action": (el) => {
        const first = presets[0];
        const action = first ? { type: "service", action: first[0], target: "all", data: {} } : { type: "service", action: "", target: "all", data: {} };
        if (first) action.target = defaultTarget(action); else custom.add(action);
        get(rule, el.dataset.path).push(action);
        changed(true);
      },
      remove: (el) => {
        get(rule, el.dataset.path).splice(Number(el.dataset.index), 1);
        if (el.dataset.path === "triggers") triggerRemoved(Number(el.dataset.index));
        changed(true);
      },
      shift: (el) => {
        const list = get(rule, el.dataset.path);
        const i = Number(el.dataset.index);
        const j = i + Number(el.dataset.dir);
        if (j < 0 || j >= list.length) return;
        [list[i], list[j]] = [list[j], list[i]];
        changed(true);
      },
      attribute: (el) => { set(rule, el.dataset.path, ""); draw(); },
      clear: (el) => { set(rule, el.dataset.path, null); changed(true); },
      pick: async (el) => {
        const path = el.dataset.path;
        const holder = get(rule, path.split(".").slice(0, -1).join("."));
        const purpose = el.dataset.purpose !== "any" ? el.dataset.purpose : PURPOSE[holder && holder.type] || "any";
        const chosen = await pickEntity(purpose);
        if (!chosen || !current()) return;
        delete entities[chosen.entity_id];  // read it again with its details (options, event types)
        await loadEntities([chosen.entity_id]);
        set(rule, path, chosen.entity_id);
        if (holder && holder.type === "button") holder.event_types = [];
        changed(true);
      },
      "pick-device": async (el) => {
        const chosen = await pickDevice();
        if (!chosen || !current()) return;
        const item = get(rule, el.dataset.path);
        item.config = { device_id: chosen.device_id };
        item.label = "";
        deviceNames[chosen.device_id] = chosen.name;
        await loadOffers(el.dataset.kind, chosen.device_id);
        if (current()) changed(true);
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
  return page;
}
