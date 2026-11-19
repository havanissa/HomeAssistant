// Phase A5: the editors shared by the Predictions settings tab and the room page: a room's overrides (every value
// may inherit the global default) and the room connections (which rooms a person walks between directly).
// Every change is one of the forecast commands, written where the forecasts are produced.
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { chip } from "../ui";
import { cmd, forecast, HORIZONS, type Connections, type GlobalSettings, type RoomOverrides } from "./api";
import { CONFIDENCE, type PredictionsHost } from "./components";

export const CONFIDENCE_OPTIONS = ["low", "moderate", "high"];

export interface OverrideOptions {
  horizons?: number[];
  confidenceOptions?: string[];
  thresholdMin?: number;
  thresholdMax?: number;
}

function confidenceWord(level: string): string {
  return CONFIDENCE[level]?.label.replace(" confidence", "") ?? level;
}

export function saveRoom(host: PredictionsHost, areaId: string, values: Partial<RoomOverrides>): Promise<boolean> {
  return host.run(() => forecast(cmd("settings"), { area_id: areaId, values }), "Saved");
}

export function resetRoom(host: PredictionsHost, areaId: string): Promise<boolean> {
  return host.run(() => forecast(cmd("settings"), { area_id: areaId, reset_all: true }), "The room follows the global defaults again");
}

/** The four override controls of a room: "inherit" shows the global value. */
export function overrideControls(areaId: string, own: RoomOverrides, g: GlobalSettings, host: PredictionsHost, o: OverrideOptions = {}): {
  enabled: TemplateResult; horizon: TemplateResult; threshold: TemplateResult; confidence: TemplateResult; overridden: boolean;
} {
  const d = host.busy || !host.canEdit;
  const horizons = o.horizons ?? [...HORIZONS];
  const levels = o.confidenceOptions ?? CONFIDENCE_OPTIONS;
  const tag = (isOwn: boolean, globalText: string) => isOwn ? html`<span class="block pr-override">room value</span>` : html`<span class="block pr-inherit">global: ${globalText}</span>`;
  return {
    overridden: Object.values(own).some((v) => v !== null && v !== undefined),
    enabled: html`<select ?disabled=${d} aria-label="Forecasts enabled" data-field="enabled" @change=${(e: Event) => { const v = (e.target as HTMLSelectElement).value; void saveRoom(host, areaId, { enabled: v === "" ? null : v === "on" }); }}>
        <option value="" ?selected=${own.enabled === null}>Inherit</option><option value="on" ?selected=${own.enabled === true}>On</option><option value="off" ?selected=${own.enabled === false}>Off</option></select>
      ${tag(own.enabled !== null, g.enabled ? "on" : "off")}`,
    horizon: html`<select ?disabled=${d} aria-label="Horizon" data-field="horizon_minutes" @change=${(e: Event) => { const v = (e.target as HTMLSelectElement).value; void saveRoom(host, areaId, { horizon_minutes: v === "" ? null : Number(v) }); }}>
        <option value="" ?selected=${own.horizon_minutes === null}>Inherit</option>${horizons.map((h) => html`<option value=${h} ?selected=${own.horizon_minutes === h}>${h} min</option>`)}</select>
      ${tag(own.horizon_minutes !== null, `${g.horizon_minutes} min`)}`,
    threshold: html`<input type="number" inputmode="numeric" min=${o.thresholdMin ?? 5} max=${o.thresholdMax ?? 99} step="1" placeholder=${String(g.probability_threshold)} aria-label="Probability threshold (%)" data-field="probability_threshold"
        .value=${own.probability_threshold === null ? "" : String(own.probability_threshold)} ?disabled=${d}
        @change=${(e: Event) => { const raw = (e.target as HTMLInputElement).value.trim(); void saveRoom(host, areaId, { probability_threshold: raw === "" ? null : Number(raw) }); }} />
      ${tag(own.probability_threshold !== null, `${g.probability_threshold} %`)}`,
    confidence: html`<select ?disabled=${d} aria-label="Confidence needed" data-field="confidence_threshold" @change=${(e: Event) => { const v = (e.target as HTMLSelectElement).value; void saveRoom(host, areaId, { confidence_threshold: v === "" ? null : v }); }}>
        <option value="" ?selected=${own.confidence_threshold === null}>Inherit</option>${levels.map((l) => html`<option value=${l} ?selected=${own.confidence_threshold === l}>${confidenceWord(l)}</option>`)}</select>
      ${tag(own.confidence_threshold !== null, confidenceWord(g.confidence_threshold))}`,
  };
}

/** The overrides of every room as a table (house settings). */
export function overridesTable(rooms: Array<{ area_id: string; name: string }>, overrides: Record<string, RoomOverrides>, g: GlobalSettings, host: PredictionsHost, o: OverrideOptions = {}): TemplateResult {
  const empty: RoomOverrides = { enabled: null, horizon_minutes: null, probability_threshold: null, confidence_threshold: null };
  return html`<div class="table-wrap"><table class="plain-table pr-overrides" data-testid="predictions-overrides">
    <thead><tr><th scope="col">Room</th><th scope="col">Forecasts</th><th scope="col">Horizon</th><th scope="col">Threshold (%)</th><th scope="col">Confidence needed</th><th scope="col"></th></tr></thead>
    <tbody>${rooms.map((r) => {
      const own = { ...empty, ...(overrides[r.area_id] ?? {}) };
      const c = overrideControls(r.area_id, own, g, host, o);
      return html`<tr data-area=${r.area_id}><td><a href="#/predictions/rooms/${r.area_id}/settings">${r.name}</a></td>
        <td>${c.enabled}</td><td>${c.horizon}</td><td>${c.threshold}</td><td>${c.confidence}</td>
        <td>${c.overridden ? html`<button type="button" class="btn btn-small" ?disabled=${host.busy || !host.canEdit} @click=${() => void resetRoom(host, r.area_id)}>${icon("undo", 16)}Inherit all</button>` : nothing}</td></tr>`;
    })}</tbody></table></div>`;
}

/** The overrides of one room as a form (room page). */
export function roomOverridesForm(areaId: string, own: RoomOverrides, g: GlobalSettings, host: PredictionsHost, o: OverrideOptions = {}): TemplateResult {
  const c = overrideControls(areaId, own, g, host, o);
  return html`<div class="pr-form pr-overrides" data-testid="predictions-room-overrides">
    <label class="field"><span>Forecasts</span>${c.enabled}</label>
    <label class="field"><span>Horizon (the time the headline probability looks ahead)</span>${c.horizon}</label>
    <label class="field"><span>Threshold for "predicted occupied" (%)</span>${c.threshold}</label>
    <label class="field"><span>Confidence needed before predicting</span>${c.confidence}</label>
  </div>
  <div class="pr-actions">${c.overridden ? html`<button type="button" class="btn" ?disabled=${host.busy || !host.canEdit} @click=${() => void resetRoom(host, areaId)}>${icon("undo", 18)}Follow the global defaults for everything</button>` : html`<span class="small muted">This room follows the global defaults.</span>`}</div>`;
}

/** The connections of one room: the ones in use, suggestions, add and reset. */
export function connectionsEditor(areaId: string, conns: Connections, host: PredictionsHost): TemplateResult {
  const d = host.busy || !host.canEdit;
  const addFrom = (e: Event) => {
    const row = (e.target as HTMLElement).closest(".pr-connect");
    const other = row?.querySelector<HTMLSelectElement>("select[data-role=other]")?.value ?? "";
    const kind = row?.querySelector<HTMLSelectElement>("select[data-role=kind]")?.value ?? conns.kinds[0]?.key ?? "door";
    if (other) void act("add", areaId, other, kind);
  };
  const act = (action: "add" | "remove" | "reset", a?: string, b?: string, k?: string) =>
    host.run(() => forecast(cmd("connection"), action === "reset" ? { action } : { action, a, b, kind: k ?? "door" }),
      action === "reset" ? "Back to the suggested connections" : "Saved");
  const kindName = (k: string) => conns.kinds.find((x) => x.key === k)?.name ?? k;
  return html`<div data-testid="predictions-connections">
    <p class="small muted">${conns.configured ? "Connections are configured by hand (the suggested ones were the starting point)." : "The connections in use are the suggested ones: adjacent rooms from the Rooms configuration and the floor plan."}
      Movement forecasts only consider rooms that are connected.</p>
    ${conns.items.length ? html`<ul class="plain-list">${conns.items.map((c) => html`<li class="pr-row" data-other=${c.other}>
        <strong><a href="#/predictions/rooms/${c.other}/settings">${c.other_name}</a></strong>
        <select class="pr-inline-select" aria-label="Kind of connection" ?disabled=${d} @change=${(e: Event) => void act("add", c.a, c.b, (e.target as HTMLSelectElement).value)}>
          ${conns.kinds.map((k) => html`<option value=${k.key} ?selected=${k.key === c.kind}>${k.name}</option>`)}</select>
        <button type="button" class="btn btn-small" ?disabled=${d} @click=${() => void act("remove", c.a, c.b)}>${icon("link-off", 16)}Remove</button></li>`)}</ul>`
      : html`<p class="muted">No connection: movement to and from this room is not forecast.</p>`}
    ${conns.suggestions.length ? html`<h4>Suggested</h4><ul class="plain-list">${conns.suggestions.map((s) => html`<li class="pr-row">
        <span>${s.other_name} <span class="small muted">· ${kindName(s.kind)} · ${s.reason === "learned" ? `${s.moves ?? 0} moves seen between the rooms` : "from the Rooms configuration"}</span></span>
        <button type="button" class="btn btn-small" ?disabled=${d} @click=${() => void act("add", s.a, s.b, s.kind)}>${icon("plus", 16)}Add</button></li>`)}</ul>` : nothing}
    ${conns.rooms.length ? html`<h4>Add a connection</h4><div class="pr-row pr-connect">
      <select class="pr-inline-select" aria-label="Other room" data-role="other" data-testid="predictions-connection-room" ?disabled=${d}>
        <option value="">Choose a room…</option>${conns.rooms.filter((r) => !conns.items.some((c) => c.other === r.area_id)).map((r) => html`<option value=${r.area_id}>${r.name}</option>`)}</select>
      <select class="pr-inline-select" aria-label="Kind" data-role="kind" ?disabled=${d}>
        ${conns.kinds.map((k) => html`<option value=${k.key}>${k.name}</option>`)}</select>
      <button type="button" class="btn" ?disabled=${d} data-testid="predictions-connection-add" @click=${addFrom}>${icon("link", 18)}Connect</button>
    </div>` : nothing}
    <div class="pr-actions">${conns.configured ? html`<button type="button" class="btn" ?disabled=${d} @click=${() => void act("reset")}>${icon("restore", 18)}Back to the suggested connections (all rooms)</button>` : chip("neutral", "Suggested connections in use")}</div>
  </div>`;
}
