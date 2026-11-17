// Presence building blocks (Phase A3.1): the timing editor, the review dialog,
// the state machine diagram and the debugging timeline. They only display the
// integration's data and collect edits; saving goes through the review.
import { html, nothing, svg, type TemplateResult } from "lit";

import { icon } from "../icons";
import { changeText } from "../rooms/model";
import { live, store } from "../store";
import { chip, StoreElement } from "../ui";
import {
  BASIS_INFO,
  bestUnit,
  buildEvents,
  clockOffset,
  diagnosticLink,
  duration,
  interpret,
  segments,
  STATE_INFO,
  timeFormatter,
  toSeconds,
  UNIT_SECONDS,
  type Diagnostic,
  type EventKind,
  type OccState,
  type PresenceReview,
  type PresenceRoom,
  type TimelineData,
  type TimelineEvent,
  type TimingView,
  type Unit,
} from "./model";
import { getRooms } from "../rooms/model";

/** Home Assistant's time zone (24-hour times everywhere in Presence). */
export function haTime(withSeconds = true): (iso: string | null | undefined) => string {
  return timeFormatter(store.overview?.home_assistant.info?.time_zone ?? null, withSeconds);
}

export function stateChip(state: OccState | null | undefined, label?: string): TemplateResult {
  if (!state) return chip("neutral", label ?? "Not observed");
  const info = STATE_INFO[state];
  return html`<span class="occ-chip occ-${state}">${icon(info.icon, 16)}<span>${label ?? info.label}</span></span>`;
}

export function levelIcon(level: string): TemplateResult {
  return icon(level === "error" ? "error" : level === "warning" ? "warning" : "info", 18);
}

export function basisChip(basis: string): TemplateResult {
  const info = BASIS_INFO[basis as keyof typeof BASIS_INFO] ?? { label: basis, help: "" };
  return html`<span class="basis basis-${basis}" title=${info.help}>${info.label}</span>`;
}

export function diagnosticItem(d: Diagnostic, areaId: string | null, opts: { room?: string | null; onGo?: (d: Diagnostic) => void } = {}): TemplateResult {
  const href = diagnosticLink(d, areaId);
  return html`<li class="diag diag-${d.level}" data-diag=${d.code}>
    <span class="diag-icon tone-${d.level}">${levelIcon(d.level)}</span>
    <div class="diag-body">
      <div class="diag-title"><strong>${d.title}</strong>${basisChip(d.basis)}${opts.room ? html`<span class="muted small">${opts.room}</span>` : nothing}</div>
      ${d.detail ? html`<p class="small">${d.detail}</p>` : nothing}
      ${d.action ? html`<p class="small muted">${d.action}</p>` : nothing}
    </div>
    <a class="btn diag-go" href=${href} data-testid="diag-go" @click=${(e: MouseEvent) => {
      if (opts.onGo) {
        e.preventDefault();
        opts.onGo(d);
      }
    }}>${icon("arrow-right", 18)}<span>Go to</span></a>
  </li>`;
}

// --------------------------------------------------------------- timing
export interface TimingEditorOptions {
  scope: "global" | "room";
  units: Map<string, Unit>;
  onValue: (key: string, value: number | null) => void;
  onUnit: (key: string, unit: Unit) => void;
  showAdvanced: boolean;
  onToggleAdvanced: () => void;
  highlight?: string | null;
  disabled?: boolean;
  inheriting?: Record<string, string[]>;
}

function timingRow(v: TimingView, o: TimingEditorOptions): TemplateResult {
  const unit = o.units.get(v.key) ?? bestUnit(v.effective);
  const shown = v.effective / UNIT_SECONDS[unit];
  const room = o.scope === "room";
  const state = room
    ? v.overridden
      ? html`<span class="inherit-tag override" data-testid="timing-override">${icon("pencil", 14)}Room value</span>`
      : html`<span class="inherit-tag" data-testid="timing-inherit">${icon("link", 14)}Global default</span>`
    : v.effective === v.item.default
      ? html`<span class="inherit-tag">${icon("check", 14)}Engine default</span>`
      : html`<span class="inherit-tag override">${icon("pencil", 14)}Changed (default ${duration(v.item.default)})</span>`;
  const reset = room
    ? v.overridden
      ? html`<button type="button" class="btn btn-small" data-testid="timing-use-global" ?disabled=${o.disabled} @click=${() => o.onValue(v.key, null)}>${icon("undo", 16)}Use global (${duration(v.global)})</button>`
      : nothing
    : v.effective !== v.item.default
      ? html`<button type="button" class="btn btn-small" ?disabled=${o.disabled} @click=${() => o.onValue(v.key, null)}>${icon("undo", 16)}Default</button>`
      : nothing;
  const inheriting = o.inheriting?.[v.key];
  return html`<div class="timing-row ${v.changed ? "changed" : ""} ${o.highlight === v.key ? "highlight" : ""} ${v.error ? "invalid" : ""}" data-timing=${v.key}>
    <div class="timing-text">
      <label class="timing-label" for="timing-${v.key}">${v.item.label}</label>
      <span class="muted small">${v.item.help}</span>
      ${v.item.applies_to ? html`<span class="small muted">Applies to ${v.item.applies_to}.</span>` : nothing}
      ${v.item.zero ? html`<span class="small muted">0 = ${v.item.zero}.</span>` : nothing}
      ${inheriting?.length ? html`<span class="small muted">Used by ${inheriting.length === 1 ? inheriting[0] : `${inheriting.length} rooms`} (inherited).</span>` : nothing}
    </div>
    <div class="timing-control">
      <div class="timing-input">
        <input id="timing-${v.key}" type="number" inputmode="decimal" min="0" step="any" .value=${String(Math.round(shown * 1000) / 1000)}
          aria-invalid=${v.error ? "true" : "false"} ?disabled=${o.disabled}
          @change=${(e: Event) => {
            const raw = (e.target as HTMLInputElement).value.trim();
            const n = Number(raw);
            if (raw === "" || !Number.isFinite(n)) return;
            o.onValue(v.key, toSeconds(n, unit));
          }} />
        <select aria-label="Unit of ${v.item.label}" .value=${unit} ?disabled=${o.disabled} @change=${(e: Event) => o.onUnit(v.key, (e.target as HTMLSelectElement).value as Unit)}>
          ${(["s", "min", "h"] as Unit[]).map((u) => html`<option value=${u} ?selected=${u === unit}>${u}</option>`)}
        </select>
      </div>
      <div class="timing-state">${state}${reset}</div>
      ${v.error ? html`<span class="small tone-error" role="alert">${v.error}</span>` : html`<span class="small muted">= ${duration(v.effective)}${room && v.overridden ? ` · global ${duration(v.global)}` : ""}</span>`}
    </div>
  </div>`;
}

export function timingEditor(views: TimingView[], o: TimingEditorOptions): TemplateResult {
  const basic = views.filter((v) => v.item.basic);
  const advanced = views.filter((v) => !v.item.basic);
  const advancedChanged = advanced.some((v) => v.changed || v.overridden && o.scope === "room");
  return html`<div class="timing-editor" data-testid="timing-editor">
    ${basic.map((v) => timingRow(v, o))}
    <button type="button" class="collapse-toggle" aria-expanded=${o.showAdvanced ? "true" : "false"} data-testid="timing-advanced" @click=${o.onToggleAdvanced}>
      ${icon(o.showAdvanced ? "chevron-down" : "chevron-right", 18)}<span>Advanced: stuck detection and start-up hold</span>
      ${!o.showAdvanced && advancedChanged ? chip("info", o.scope === "room" ? "Room values" : "Changed") : nothing}
    </button>
    ${o.showAdvanced ? advanced.map((v) => timingRow(v, o)) : nothing}
  </div>`;
}

// --------------------------------------------------------------- review
export interface ReviewOptions {
  review: PresenceReview | null;
  error: { message: string; code: string } | null;
  busy: boolean;
  confirmed: Set<string>;
  onConfirm: (code: string, on: boolean) => void;
  onSave: () => void;
  onClose: () => void;
  name: (id: string | undefined) => string;
  kindLabel: (k: unknown) => string;
  roleLabel: (r: unknown) => string;
  timingLabel: (key: string) => string;
}

export function reviewDialog(o: ReviewOptions): TemplateResult {
  const r = o.review;
  const missing = r ? r.requires_confirmation.filter((c) => !o.confirmed.has(c)) : [];
  const fmt = haTime(false);
  const timingText = (c: PresenceReview["changes"][0]): string => {
    const label = o.timingLabel(String(c.key));
    if (c.scope === "global") return `${label}: ${duration(c.old as number)} → ${duration(c.new as number)}`;
    const from = c.old === null || c.old === undefined ? `global (${duration(c.old_effective)})` : duration(c.old as number);
    const to = c.new === null || c.new === undefined ? `global (${duration(c.new_effective)})` : duration(c.new as number);
    return `${label}: ${from} → ${to}`;
  };
  const predictions = r?.impacts.find((i) => i.code === "prediction")?.predictions ?? [];
  return html`<dialog class="sheet sheet-full review-dialog presence-review" aria-labelledby="presence-review-title" data-testid="presence-review" @close=${o.onClose}>
    <div class="sheet-inner">
      <header class="sheet-head"><h2 id="presence-review-title">Review presence changes</h2>
        <button type="button" class="icon-btn" aria-label="Close" @click=${o.onClose}>${icon("close")}</button></header>
      <div class="sheet-body review-body">
        ${o.error ? html`<p class="notice notice-${o.error.code === "stale_revision" || o.error.code === "confirmation_required" ? "warning" : "error"}" role="alert" data-testid="review-error">${icon("warning", 20)}<span>${o.error.message}</span></p>` : nothing}
        ${!r ? (o.busy ? html`<p class="muted">Checking the changes with the integration…</p>` : nothing) : html`
          ${r.stale ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>The configuration was changed elsewhere since it was loaded. Close this review and reload, or save to try again (the integration refuses an outdated save).</span></p>` : nothing}
          ${r.errors.length ? html`<section class="review-block"><h3>${icon("error", 18)}Cannot be saved</h3><ul>${r.errors.map((e) => html`<li class="tone-error">${e.message}</li>`)}</ul></section>` : nothing}
          <section class="review-block" data-testid="review-changes"><h3>${icon("pencil", 18)}Changes</h3>
            ${r.changes.length ? html`<ul>${r.changes.map((c) => html`<li>${c.type === "timing" ? timingText(c) : changeText(c, o.name, (a) => a ?? "—", o.kindLabel, o.roleLabel)}</li>`)}</ul>` : html`<p class="muted">Nothing changes.</p>`}
          </section>
          ${r.affected_rooms.length ? html`<section class="review-block"><h3>${icon("home", 18)}Rooms affected</h3><p>${r.affected_rooms.map((a) => a.name).join(", ")}</p></section>` : nothing}
          ${predictions.length ? html`<section class="review-block" data-testid="review-predictions"><h3>${icon("eye", 18)}The engine right after saving</h3>
            <p class="muted small">Computed by the integration on a copy of each room's engine with the new settings and the current sensor values.</p>
            <div class="table-wrap"><table class="plain-table"><thead><tr><th scope="col">Room</th><th scope="col">Now</th><th scope="col">After saving</th></tr></thead>
            <tbody>${predictions.map((p) => html`<tr class=${p.state !== p.new_state ? "changed" : ""}><td>${p.name}</td>
              <td>${stateChip(p.state)}${p.vacancy_at ? html`<span class="small muted block">vacant at ${fmt(p.vacancy_at)}</span>` : nothing}</td>
              <td>${stateChip(p.new_state)}${p.new_vacancy_at ? html`<span class="small muted block">vacant at ${fmt(p.new_vacancy_at)}</span>` : nothing}</td></tr>`)}</tbody></table></div>
          </section>` : nothing}
          ${r.impacts.filter((i) => i.code !== "prediction").length ? html`<section class="review-block" data-testid="review-impacts"><h3>${icon("alert-octagon", 18)}Consequences</h3>
            <ul class="impact-list">${r.impacts.filter((i) => i.code !== "prediction").map((i) => html`<li class="impact impact-${i.level}">
              ${i.level === "confirm"
                ? html`<label class="confirm-row"><input type="checkbox" data-confirm=${i.code} .checked=${o.confirmed.has(i.code)} @change=${(e: Event) => o.onConfirm(i.code, (e.target as HTMLInputElement).checked)} />
                    <span><strong>${i.title}</strong><span class="block small">${i.detail}</span><span class="block small tone-warning">Confirm to save.</span></span></label>`
                : html`<span>${icon("info", 18)}</span><span><strong>${i.title}</strong><span class="block small muted">${i.detail}</span></span>`}
            </li>`)}</ul></section>` : nothing}
          ${r.conflicts?.length ? html`<section class="review-block"><h3>${icon("warning", 18)}Check</h3><ul>${r.conflicts.map((c) => html`<li>${o.name(c.entity_id)}: ${c.code === "sensor_shared" ? `also used by ${c.rooms.join(", ")}` : c.code === "outside_area" ? `in the area ${c.rooms.join(", ")}` : c.code === "category_mismatch" ? `looks like a ${c.rooms.join(", ")}` : c.code}</li>`)}</ul></section>` : nothing}
          ${r.problems_new.length ? html`<section class="review-block"><h3>${icon("warning", 18)}New problems</h3><ul>${r.problems_new.map((p) => html`<li>${p.title}: <span class="muted">${p.explanation}</span></li>`)}</ul></section>` : nothing}
          ${r.problems_resolved.length ? html`<section class="review-block"><h3>${icon("ok", 18)}Resolved</h3><ul>${r.problems_resolved.map((p) => html`<li>${p.title}</li>`)}</ul></section>` : nothing}
          <p class="muted small">${r.apply === "live" ? "Applied at once: the engine keeps its sensor values, no reload, no restart hold." : r.apply === "reload" ? "The integration reloads to apply this (about a second); occupancy is restored from its saved state." : ""}
            No device is switched by saving itself; the integration keeps running occupancy.</p>`}
      </div>
      <footer class="sheet-foot">
        <button type="button" class="btn" @click=${o.onClose}>Cancel</button>
        <button type="button" class="btn btn-primary" data-testid="presence-save" ?disabled=${o.busy || !r || !!r.errors.length || !r.changes.length || missing.length > 0 || !r.writable} @click=${o.onSave}>
          ${icon("save", 20)}${o.busy ? "Saving…" : missing.length ? `Confirm ${missing.length} item${missing.length > 1 ? "s" : ""} to save` : "Save"}</button>
      </footer>
    </div>
  </dialog>`;
}

// --------------------------------------------------------- state machine
const NODES: Record<OccState, { x: number; y: number }> = {
  vacant: { x: 85, y: 62 },
  possible_occupancy: { x: 270, y: 62 },
  confirmed_occupancy: { x: 455, y: 62 },
  possible_vacancy: { x: 640, y: 62 },
  uncertain: { x: 362, y: 172 },
};
const EDGES: Array<{ from: OccState; to: OccState; d: string }> = [
  { from: "vacant", to: "possible_occupancy", d: "M157 56 L196 56" },
  { from: "possible_occupancy", to: "confirmed_occupancy", d: "M342 56 L381 56" },
  { from: "confirmed_occupancy", to: "possible_vacancy", d: "M527 56 L566 56" },
  { from: "possible_vacancy", to: "confirmed_occupancy", d: "M566 74 Q546 96 527 74" },
  { from: "possible_vacancy", to: "vacant", d: "M640 88 Q640 132 362 132 Q85 132 85 88" },
  { from: "possible_occupancy", to: "possible_vacancy", d: "M270 36 Q270 6 455 6 Q640 6 640 36" },
  { from: "possible_occupancy", to: "vacant", d: "M196 74 Q176 96 157 74" },
];

/** The engine's states and transitions; the current state and the last transition are highlighted. */
export function stateMachine(state: OccState | null, last: { from: string | null | undefined; to: string | null | undefined } | null,
                             transitions: Array<{ from: string; to: string; when: string }>): TemplateResult {
  const when = (a: string, b: string): string => transitions.find((t) => t.from === a && t.to === b)?.when ?? "";
  return html`<figure class="state-machine" data-testid="state-machine" data-state=${state ?? "none"}>
    <svg viewBox="0 0 726 204" role="img" aria-label=${`Occupancy state machine; current state: ${state ? STATE_INFO[state].label : "not observed"}`}>
      <defs><marker id="sm-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="currentColor" /></marker></defs>
      ${EDGES.map((e) => {
        const hot = last && last.from === e.from && last.to === e.to;
        return svg`<path class="sm-edge ${hot ? "hot" : ""}" d=${e.d} marker-end="url(#sm-arrow)"><title>${when(e.from, e.to)}</title></path>`;
      })}
      <path class="sm-edge dashed" d="M362 150 L362 94" marker-end="url(#sm-arrow)" marker-start="url(#sm-arrow)"><title>${when("*", "uncertain")}</title></path>
      ${(Object.keys(NODES) as OccState[]).map((s) => {
        const n = NODES[s];
        const current = s === state;
        return svg`<g class="sm-node occ-${s} ${current ? "current" : ""}" data-node=${s}>
          <rect x=${n.x - 72} y=${n.y - 24} width="144" height="48" rx="24"></rect>
          <text x=${n.x} y=${n.y + 5} text-anchor="middle">${STATE_INFO[s].label}</text>
        </g>`;
      })}
    </svg>
    <figcaption class="muted small">The integration's occupancy states. Hover an arrow for when the engine takes it; the highlighted arrow is the latest transition.</figcaption>
  </figure>`;
}

// -------------------------------------------------------------- timeline
const KIND_LABEL: Record<EventKind, string> = {
  occupancy: "Occupancy decisions",
  sensor: "Sensor activity",
  availability: "Availability",
  config: "Configuration",
  start: "Starts and reloads",
};
const LIVE_LIMIT = 500;

export class SlPresenceTimeline extends StoreElement {
  static properties = {
    room: { attribute: false },
    log: { attribute: false },
    window: { state: true },
    data: { state: true },
    error: { state: true },
    loading: { state: true },
    kinds: { state: true },
    sensor: { state: true },
    open: { state: true },
    compact: { type: Boolean },
  };
  declare room: PresenceRoom | null;
  declare log: TimelineData["log"];
  declare window: number;
  declare data: TimelineData | null;
  declare error: string | null;
  declare loading: boolean;
  declare kinds: Set<EventKind>;
  declare sensor: string;
  declare open: string | null;
  declare compact: boolean;
  private liveEvents: TimelineEvent[] = [];
  private livePoints = new Map<string, Array<{ t: string; s: string }>>();
  private seen = new Map<string, string>();
  private loadedFor: string | null = null;

  constructor() {
    super();
    this.room = null;
    this.log = [];
    this.window = 3600;
    this.data = null;
    this.error = null;
    this.loading = false;
    this.kinds = new Set(["occupancy", "sensor", "availability", "config", "start"]);
    this.sensor = "";
    this.open = null;
    this.compact = false;
  }

  async load(): Promise<void> {
    if (!this.room) return;
    const area = this.room.area_id;
    this.loading = true;
    try {
      this.data = await getRooms<TimelineData>(`api/presence/rooms/${encodeURIComponent(area)}/timeline?window=${this.window}`);
      this.error = null;
      this.liveEvents = this.liveEvents.filter((e) => e.time > (this.data?.end ?? ""));
      for (const [id, pts] of this.livePoints) this.livePoints.set(id, pts.filter((p) => p.t > (this.data?.end ?? "")));
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
    } finally {
      this.loading = false;
    }
  }

  protected willUpdate(changed: Map<string, unknown>): void {
    if (changed.has("room") && this.room && this.room.area_id !== this.loadedFor) {
      this.loadedFor = this.room.area_id;
      this.liveEvents = [];
      this.livePoints.clear();
      this.seen.clear();
      void this.load();
    }
    this.collectLive();
  }

  /** Changes of the watched entities seen in the browser after the history was read (bounded). */
  private collectLive(): void {
    const room = this.room;
    if (!room) return;
    const values = this.data?.state_values ?? null;
    const sensors = new Map(room.sensors.map((s) => [s.entity_id, s]));
    const names = new Map(room.signals.map((s) => [s.entity_id, s.name]));
    const ids = [...sensors.keys(), ...(room.entities.state ? [room.entities.state] : [])];
    for (const id of ids) {
      const st = store.liveStates.get(id);
      if (!st?.last_changed || st.state === null) continue;
      const key = `${st.state}@${st.last_changed}`;
      const before = this.seen.get(id);
      this.seen.set(id, key);
      if (before === undefined || before === key) continue;
      const pts = this.livePoints.get(id) ?? [];
      pts.push({ t: st.last_changed, s: st.state });
      this.livePoints.set(id, pts.slice(-LIVE_LIMIT));
      if (id === room.entities.state) continue; // decisions come from the engine's log
      const s = sensors.get(id)!;
      const v = interpret(st.state, Boolean(s.inverted), values);
      const words = { active: ["activated", "ok"], inactive: ["cleared", "neutral"], unknown: ["reports no usable value", "warning"], unavailable: ["became unavailable", "warning"] }[v];
      const event: TimelineEvent = {
        id: `live-${id}-${st.last_changed}`, time: st.last_changed, kind: v === "unavailable" || v === "unknown" ? "availability" : "sensor",
        entity_id: id, title: `${names.get(id) ?? id} ${words[0]}`, detail: `State “${st.state}”`, state: st.state, tone: words[1] as TimelineEvent["tone"], source: "live",
      };
      this.liveEvents = [...this.liveEvents, event].slice(-LIVE_LIMIT);
    }
  }

  private merged(): TimelineData | null {
    if (!this.data) return null;
    const series = { ...this.data.history.series };
    for (const [id, pts] of this.livePoints) series[id] = [...(series[id] ?? []), ...pts];
    const logs = new Map(this.data.log.map((e) => [e.seq, e]));
    for (const e of this.log ?? []) if (e.time >= this.data.start) logs.set(e.seq, e);
    return { ...this.data, history: { ...this.data.history, series }, log: [...logs.values()].sort((a, b) => a.seq - b.seq) };
  }

  private lanes(data: TimelineData, start: number, end: number): TemplateResult {
    const width = 1000;
    const x = (t: number): number => ((t - start) / (end - start)) * width;
    const fmt = timeFormatter(data.time_zone, false);
    const ticks: number[] = [];
    const step = data.window <= 900 ? 120_000 : data.window <= 3600 ? 600_000 : data.window <= 21600 ? 3_600_000 : 4 * 3_600_000;
    for (let t = Math.ceil(start / step) * step; t < end; t += step) ticks.push(t);
    const room = this.room!;
    const stateId = data.entities.state;
    const statePoints = stateId ? [...(data.history.series[stateId] ?? [])] : [];
    for (const e of data.log) if (e.kind === "transition" && e.state && (e.area_id === room.area_id)) statePoints.push({ t: e.time, s: e.state });
    statePoints.sort((a, b) => (a.t < b.t ? -1 : 1));
    const rows: Array<{ id: string; label: string; segs: ReturnType<typeof segments>; kind: "state" | "sensor" }> = [
      { id: "__room", label: "Room", segs: segments(statePoints, start, end, (s) => s), kind: "state" },
      ...data.sensors.filter((s) => !this.sensor || s.entity_id === this.sensor).map((s) => ({
        id: s.entity_id, label: s.name, kind: "sensor" as const,
        segs: segments(data.history.series[s.entity_id] ?? [], start, end, (v) => interpret(v, Boolean(s.inverted), data.state_values)),
      })),
    ];
    return html`<div class="tl-lanes" data-testid="timeline-lanes">
      ${rows.map((r) => html`<div class="tl-lane"><span class="tl-lane-label" title=${r.label}>${r.label}</span>
        <svg class="tl-bar" viewBox="0 0 ${width} 20" preserveAspectRatio="none" role="img" aria-label=${`${r.label} over the window`}>
          ${r.segs.map((s) => svg`<rect class=${r.kind === "state" ? `seg-state occ-${s.value}` : `seg seg-${s.value}`} x=${x(s.start)} y="2" width=${Math.max(1, x(s.end) - x(s.start))} height="16"><title>${r.kind === "state" ? STATE_INFO[s.value as OccState]?.label ?? s.value : s.value}: ${fmt(new Date(s.start).toISOString())}–${fmt(new Date(s.end).toISOString())}</title></rect>`)}
        </svg></div>`)}
      <div class="tl-axis"><span class="tl-lane-label"></span><div class="tl-ticks">${ticks.map((t) => html`<span style="left:${(x(t) / width) * 100}%">${fmt(new Date(t).toISOString())}</span>`)}</div></div>
      <div class="tl-legend small muted"><span class="lg seg-active"></span>Active <span class="lg seg-inactive"></span>Clear <span class="lg seg-unknown"></span>No usable value <span class="lg seg-unavailable"></span>Unavailable</div>
    </div>`;
  }

  private details(e: TimelineEvent): TemplateResult {
    const fmt = haTime(true);
    const d = e.data;
    return html`<div class="tl-details" data-testid="timeline-details">
      <dl class="kv">
        <dt>Time</dt><dd>${fmt(e.time)}</dd>
        <dt>Source</dt><dd>${e.source === "engine" ? "The integration's presence log (engine decision)" : e.source === "recorder" ? "Home Assistant Recorder" : "Live (seen while this page was open)"}</dd>
        ${e.state ? html`<dt>State</dt><dd class="mono">${e.state}</dd>` : nothing}
        ${d?.reason ? html`<dt>Engine reason</dt><dd>${d.reason}</dd>` : e.detail ? html`<dt>Detail</dt><dd>${e.detail}</dd>` : nothing}
        ${d?.evidence?.length ? html`<dt>Evidence</dt><dd>${d.evidence.map((x) => `${x.name} (${x.status})`).join(", ")}</dd>` : nothing}
        ${d?.stuck?.length ? html`<dt>Ignored as stuck</dt><dd>${d.stuck.map((x) => x.name).join(", ")}</dd>` : nothing}
        ${d?.unknown?.length ? html`<dt>Without a value</dt><dd>${d.unknown.map((x) => x.name).join(", ")}</dd>` : nothing}
        ${d?.required_quiet ? html`<dt>Quiet time needed</dt><dd>${duration(d.required_quiet)}${d.vacancy_evidence?.length ? ` (fast: ${d.vacancy_evidence.join(", ")})` : ""}</dd>` : nothing}
        ${d?.vacancy_at ? html`<dt>Vacant at</dt><dd>${fmt(d.vacancy_at)} (if nothing happens)</dd>` : nothing}
        ${d?.confidence !== undefined ? html`<dt>Confidence</dt><dd>${d.confidence} / 100</dd>` : nothing}
        ${d?.starting_up ? html`<dt>Start-up</dt><dd>Held by the start-up hold</dd>` : nothing}
        ${d?.changes?.length ? html`<dt>Changes</dt><dd>${d.changes.map((c) => html`<span class="block small mono">${JSON.stringify(c)}</span>`)}</dd>` : nothing}
      </dl></div>`;
  }

  protected render(): unknown {
    const room = this.room;
    if (!room) return nothing;
    const data = this.merged();
    const fmt = haTime(true);
    const offset = clockOffset(data?.now ?? null);
    const end = Date.now() + offset;
    const start = end - this.window * 1000;
    const events = data ? buildEvents(data, this.liveEvents).filter((e) => this.kinds.has(e.kind) && (!this.sensor || e.entity_id === this.sensor || e.entity_id === null || e.kind === "occupancy")) : [];
    const list = [...events].reverse().slice(0, this.compact ? 8 : 400);
    return html`<section class="card timeline-card" data-testid="presence-timeline">
      <header class="card-head">${icon("history")}<h3>${this.compact ? "Recent events" : "Timeline"}</h3>
        ${store.link === "live" ? chip("ok", "Live") : chip("warning", "Not live")}
        <span class="spacer"></span>
        ${this.compact ? nothing : html`<label class="inline-field"><span class="visually-hidden">Window</span>
          <select data-testid="timeline-window" .value=${String(this.window)} @change=${(e: Event) => ((this.window = Number((e.target as HTMLSelectElement).value)), void this.load())}>
            ${(data?.windows ?? [900, 3600, 21600, 86400]).map((w) => html`<option value=${w} ?selected=${w === this.window}>Last ${duration(w)}</option>`)}
          </select></label>`}
        <button type="button" class="icon-btn" aria-label="Refresh the history" title="Read the Recorder again" ?disabled=${this.loading} @click=${() => void this.load()}>${icon("refresh", 20)}</button>
      </header>
      ${this.error ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>${this.error}</span></p>` : nothing}
      ${data && !data.history.available ? html`<p class="notice notice-info">${icon("info", 20)}<span>${data.history.error ?? "No Recorder history."} The timeline shows the integration's presence log and live changes only.</span></p>` : nothing}
      ${data?.history.truncated.length ? html`<p class="muted small">Very busy sensors: only their latest changes are shown (${data.history.truncated.length}).</p>` : nothing}
      ${this.compact ? nothing : html`<div class="tl-filters">
        <div class="chip-choices small-chips" role="group" aria-label="Event types">
          ${(Object.keys(KIND_LABEL) as EventKind[]).map((k) => html`<label class=${this.kinds.has(k) ? "selected" : ""}><input type="checkbox" data-kind=${k} .checked=${this.kinds.has(k)} @change=${(e: Event) => {
            const next = new Set(this.kinds);
            if ((e.target as HTMLInputElement).checked) next.add(k);
            else next.delete(k);
            this.kinds = next;
          }} />${KIND_LABEL[k]}</label>`)}
        </div>
        <label class="inline-field">Sensor <select data-testid="timeline-sensor" .value=${this.sensor} @change=${(e: Event) => (this.sensor = (e.target as HTMLSelectElement).value)}>
          <option value="">All sensors</option>${room.signals.map((s) => html`<option value=${s.entity_id} ?selected=${s.entity_id === this.sensor}>${s.name}</option>`)}
        </select></label>
      </div>
      ${data ? this.lanes(data, start, end) : nothing}`}
      ${!data && this.loading ? html`<p class="muted">Reading the history…</p>` : nothing}
      ${data ? (list.length ? html`<ol class="tl-events" data-testid="timeline-events">${list.map((e) => html`<li class="tl-event tone-${e.tone} ${this.open === e.id ? "open" : ""}" data-kind=${e.kind} data-source=${e.source}>
          <button type="button" class="tl-row" aria-expanded=${this.open === e.id ? "true" : "false"} @click=${() => (this.open = this.open === e.id ? null : e.id)}>
            <time class="mono small">${fmt(e.time)}</time>
            <span class="tl-dot"></span>
            <span class="tl-title">${e.title}${e.detail && e.kind !== "occupancy" ? html`<span class="muted small"> · ${e.detail}</span>` : nothing}
              ${e.kind === "occupancy" && e.detail ? html`<span class="block small muted">${e.detail}</span>` : nothing}</span>
            <span class="tl-src small muted">${e.source === "engine" ? "engine" : e.source === "recorder" ? "recorder" : "live"}</span>
          </button>
          ${this.open === e.id ? this.details(e) : nothing}
        </li>`)}</ol>` : html`<p class="muted">No events in this window${this.kinds.size < 5 || this.sensor ? " with these filters" : ""}.</p>`) : nothing}
      ${this.compact ? html`<p class="small"><a class="text-link" href=${`#/presence/${encodeURIComponent(room.area_id)}?tab=timeline`}>Open the full timeline</a></p>` : nothing}
    </section>`;
  }
}

customElements.define("sl-presence-timeline", SlPresenceTimeline);

/** Entity ids a presence page watches live (room state entities and sensors). */
export function watchPresence(rooms: PresenceRoom[]): void {
  const ids = new Set<string>();
  for (const r of rooms) {
    for (const s of r.sensors) ids.add(s.entity_id);
    for (const id of Object.values(r.entities)) if (id) ids.add(id);
    for (const z of r.zones) {
      if (z.radar?.presence) ids.add(z.radar.presence);
      if (z.radar?.distance) ids.add(z.radar.distance);
    }
  }
  live.watch([...ids]);
}
