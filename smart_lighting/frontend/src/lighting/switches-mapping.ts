// Phase A4: Switches page, the fields of one button mapping (``switch/mapping``). Only the fields the chosen
// action uses are shown; the integration validates every entity again before saving.
import { html, nothing, type TemplateResult } from "lit";

import { LIGHT_ACTIONS, type Mapping, type SwitchesModel } from "./switches-model";

type Change = (patch: Partial<Mapping>) => void;

function select(label: string, value: string, options: [string, string][], onChange: (v: string) => void, disabled: boolean,
                field: string): TemplateResult {
  return html`<label class="field"><span>${label}</span>
    <select data-field=${field} ?disabled=${disabled} @change=${(e: Event) => onChange((e.target as HTMLSelectElement).value)}>
      ${options.map(([v, l]) => html`<option value=${v} ?selected=${v === value}>${l}</option>`)}
    </select></label>`;
}

function number(label: string, value: number | null | undefined, min: number, max: number, step: number,
                onChange: (v: number | null) => void, disabled: boolean, field: string, hint?: string): TemplateResult {
  return html`<label class="field"><span>${label}</span>
    <input type="number" data-field=${field} min=${min} max=${max} step=${step} .value=${value == null ? "" : String(value)} ?disabled=${disabled}
      @change=${(e: Event) => { const raw = (e.target as HTMLInputElement).value.trim(); onChange(raw === "" ? null : Number(raw)); }} />
    ${hint ? html`<span class="small muted">${hint}</span>` : nothing}</label>`;
}

/** The action selector and the fields of the chosen action. */
export function mappingFields(m: Mapping, sw: SwitchesModel, moods: string[], onChange: Change, disabled: boolean): TemplateResult {
  const parts: TemplateResult[] = [
    select("Action", m.action || "none", sw.actions.map((a) => [a.value, a.label]), (v) => onChange({ action: v }), disabled, "action"),
  ];
  if (LIGHT_ACTIONS.includes(m.action) && m.action !== "dim_stop") {
    const target = m.target || "room";
    const options: [string, string][] = [["room", "All room lights"],
      ...sw.targets.map((t): [string, string] => [t.entity_id, `${t.name || t.entity_id}${t.group ? " (group)" : ""}`])];
    if (target !== "room" && !sw.targets.some((t) => t.entity_id === target)) options.push([target, target]);
    parts.push(select("Lights", target, options, (v) => onChange({ target: v }), disabled, "target"));
  }
  if (m.action === "on") {
    parts.push(select("Switch on with", m.on_values || "mood", [["mood", "The current mood"], ["last", "The light's last values"], ["fixed", "Fixed values"]],
      (v) => onChange({ on_values: v as Mapping["on_values"] }), disabled, "on_values"));
    if (m.on_values === "fixed") {
      parts.push(number("Brightness (%)", m.brightness, 1, 100, 1, (v) => onChange({ brightness: v }), disabled, "brightness"));
      parts.push(number("Colour temperature (K)", m.color_temp, 1000, 10000, 50, (v) => onChange({ color_temp: v }), disabled, "color_temp"));
    }
  }
  if (m.action === "set_brightness") parts.push(number("Brightness (%)", m.brightness, 1, 100, 1, (v) => onChange({ brightness: v }), disabled, "brightness"));
  if (m.action === "set_color_temp") parts.push(number("Colour temperature (K)", m.color_temp, 1000, 10000, 50, (v) => onChange({ color_temp: v }), disabled, "color_temp"));
  if (m.action === "mood" || m.action === "mood_auto") {
    const scope = m.scope || "room";
    if (m.action === "mood") {
      const names = scope === "global" ? sw.global_mood_options.filter((o) => o !== "Auto") : moods;
      const options: [string, string][] = [["", "Choose…"], ...names.map((n): [string, string] => [n, n])];
      if (m.mood && !names.includes(m.mood)) options.push([m.mood, `${m.mood} (not available)`]);
      parts.push(select("Mood", m.mood || "", options, (v) => onChange({ mood: v || null }), disabled, "mood"));
    }
    parts.push(select("Where", scope, [["room", "This room"], ["global", sw.global_mood_helper ? "All rooms (mood selector)" : "All rooms (no mood selector set up)"]],
      (v) => onChange({ scope: v as "room" | "global" }), disabled, "scope"));
    if (m.action === "mood" && scope !== "global") {
      parts.push(number("For (minutes)", m.minutes, 1, 1440, 1, (v) => onChange({ minutes: v }), disabled, "minutes", "empty: until changed"));
    }
  }
  if (m.action === "scene" || m.action === "script") {
    const list = m.action === "scene" ? sw.scenes : sw.scripts;
    parts.push(select(m.action === "scene" ? "Scene" : "Script", m.entity || "",
      [["", list.length ? "Choose…" : `No ${m.action} found`], ...list.map((x): [string, string] => [x.entity_id, x.name])],
      (v) => onChange({ entity: v || null }), disabled, "entity"));
  }
  if (m.action === "wled_preset") {
    if (!sw.wled_presets.length) parts.push(html`<p class="small muted sw-wide">No WLED preset selector found (WLED integration).</p>`);
    else {
      parts.push(select("WLED device", m.entity || "", [["", "Choose…"], ...sw.wled_presets.map((x): [string, string] => [x.entity_id, x.name || x.entity_id])],
        (v) => onChange({ entity: v || null, option: null }), disabled, "entity"));
      const preset = sw.wled_presets.find((x) => x.entity_id === m.entity);
      if (preset) parts.push(select("Preset", m.option || "", [["", "Choose…"], ...preset.options.map((o): [string, string] => [o, o])],
        (v) => onChange({ option: v || null }), disabled, "option"));
    }
  }
  return html`<div class="sw-map-fields">${parts}</div>`;
}
