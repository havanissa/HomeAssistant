// Presence workspace of one room (Phase A3.1): sensors (left), the engine's
// live decision with its timeline, timing and diagnostics (centre), and an
// inspector for the selected sensor (right). Sensor and timing edits stay in
// a draft until they are reviewed and saved; the integration validates and
// stores them (the same configuration the Rooms editor edits) and keeps
// deciding occupancy. Nothing here sends a device command.
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { hashQuery, setLeaveGuard } from "../router";
import { live, store } from "../store";
import {
  ApiError,
  AVAILABILITY,
  getRooms,
  kindDefaultRole,
  kindsFor,
  postRooms,
  rebase,
  type Candidate as RoomsCandidate,
  type SensorConfig,
} from "../rooms/model";
import { chip, kv, StoreElement, type Tone } from "../ui";
import { diagnosticItem, haTime, reviewDialog, stateChip, stateMachine, timingEditor, watchPresence } from "../presence/components";
import {
  ago,
  clock,
  clockOffset,
  CONDITION_INFO,
  DOOR_HELP,
  duration,
  ROLE_HELP,
  secondsSince,
  secondsUntil,
  STATE_INFO,
  timingChange,
  timingViews,
  visitChain,
  type Candidate,
  type Diagnostic,
  type PresenceChange,
  type PresenceReview,
  type PresenceWorkspace,
  type Signal,
  type Unit,
} from "../presence/model";
import { apiProblem, LiveReload } from "./presence";

type Tab = "live" | "timeline" | "timing" | "diagnostics";
const TABS: Array<{ key: Tab; title: string; icon: string }> = [
  { key: "live", title: "Live", icon: "eye" },
  { key: "timeline", title: "Timeline", icon: "history" },
  { key: "timing", title: "Timing", icon: "tune" },
  { key: "diagnostics", title: "Diagnostics", icon: "stethoscope" },
];
const KIND_ICON: Record<string, string> = { pir: "motion-sensor", radar: "radar", door: "door", pc_activity: "monitor", bed: "bed", other: "puzzle" };
const EFFECT_TEXT: Record<string, string> = {
  trigger: "Trigger: its activation starts (possible) occupancy and holds it while active.",
  presence: "Presence: holds the room occupied while active; short clears are bridged.",
  door: "Door: only its movements count (activity and exit evidence).",
  none: "Not used for occupancy (excluded or role Ignore).",
};
const STATUS_TEXT: Record<string, string> = {
  active: "Counts as evidence of presence or activity now",
  grace: "Cleared moments ago; bridged as a dropout",
  cleared: "Reports no presence",
  recent: "Moved within the correlation window",
  idle: "No recent movement",
  stuck: "Active too long: ignored as stuck",
  unknown: "No usable value: neither presence nor vacancy",
  ignored: "Not used",
};

function norm(sensors: SensorConfig[]): SensorConfig[] {
  return sensors.map((s) => ({ entity_id: s.entity_id, kind: s.kind, role: s.role, inverted: Boolean(s.inverted), enabled: s.enabled !== false }));
}

export class SlPresenceRoom extends StoreElement {
  static properties = {
    areaId: { type: String },
    data: { state: true },
    error: { state: true },
    tab: { state: true },
    sensors: { state: true },
    timing: { state: true },
    resetTiming: { state: true },
    selected: { state: true },
    inspectorOpen: { state: true },
    leftOpen: { state: true },
    advanced: { state: true },
    sensorAdvanced: { state: true },
    review: { state: true },
    reviewError: { state: true },
    busy: { state: true },
    confirmed: { state: true },
    notice: { state: true },
    changedElsewhere: { state: true },
    pick: { state: true },
    tick: { state: true },
  };
  declare areaId: string;
  declare data: PresenceWorkspace | null;
  declare error: { message: string; code: string } | null;
  declare tab: Tab;
  declare sensors: SensorConfig[] | null;
  declare timing: Record<string, number | null>;
  declare resetTiming: boolean;
  declare selected: string | null;
  declare inspectorOpen: boolean;
  declare leftOpen: boolean;
  declare advanced: boolean;
  declare sensorAdvanced: boolean;
  declare review: PresenceReview | null;
  declare reviewError: { message: string; code: string } | null;
  declare busy: boolean;
  declare confirmed: Set<string>;
  declare notice: string | null;
  declare changedElsewhere: boolean;
  declare pick: boolean;
  declare tick: number;
  private offset = 0;
  private highlight: string | null = null;
  private loadedArea: string | null = null;
  private reviewOpen = false;
  /** The change and revision shown in the open review: what Save sends. */
  private reviewed: { change: PresenceChange; revision: string } | null = null;
  private units = new Map<string, Unit>();
  private timer: number | undefined;
  private seenRooms = -1;
  private extra = new Map<string, Candidate>();
  private reloader = new LiveReload(() => this.load(), () => this.watchIds());

  constructor() {
    super();
    this.areaId = "";
    this.data = null;
    this.error = null;
    this.tab = "live";
    this.sensors = null;
    this.timing = {};
    this.resetTiming = false;
    this.selected = null;
    this.inspectorOpen = false;
    this.leftOpen = true;
    this.advanced = false;
    this.sensorAdvanced = false;
    this.review = null;
    this.reviewError = null;
    this.busy = false;
    this.confirmed = new Set();
    this.notice = null;
    this.changedElsewhere = false;
    this.pick = false;
    this.tick = 0;
  }

  // ------------------------------------------------------------ lifecycle
  private onBeforeUnload = (event: BeforeUnloadEvent): void => {
    if (this.dirty()) {
      event.preventDefault();
      event.returnValue = "";
    }
  };

  connectedCallback(): void {
    super.connectedCallback();
    window.addEventListener("beforeunload", this.onBeforeUnload);
    setLeaveGuard(() => (this.dirty() ? `${this.data?.room.name ?? "This room"} has unsaved presence changes. Leave without saving them?` : null));
    this.timer = window.setInterval(() => (this.tick += 1), 1000);
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    window.removeEventListener("beforeunload", this.onBeforeUnload);
    setLeaveGuard(null);
    window.clearInterval(this.timer);
    this.reloader.stop();
    live.watch([]);
  }

  protected willUpdate(changed: Map<string, unknown>): void {
    if (changed.has("areaId") && this.areaId && this.areaId !== this.loadedArea) {
      this.loadedArea = this.areaId;
      this.reset();
      this.readQuery();
      void this.load();
    }
    if (this.data) this.reloader.check();
    if (store.roomsVersion !== this.seenRooms) {
      const first = this.seenRooms === -1;
      this.seenRooms = store.roomsVersion;
      if (!first && !this.busy) void this.onConfigChanged();
    }
  }

  private reset(): void {
    this.data = null;
    this.sensors = null;
    this.timing = {};
    this.resetTiming = false;
    this.selected = null;
    this.inspectorOpen = false;
    this.notice = null;
    this.changedElsewhere = false;
    this.extra.clear();
  }

  /** ``?sensor=``, ``?tab=``, ``?setting=`` from the dashboard, the diagnostics or the floor plan. */
  private readQuery(): void {
    const q = hashQuery();
    if (!q.toString()) return;
    history.replaceState(null, "", window.location.hash.split("?")[0]);
    const tab = q.get("tab") as Tab | null;
    if (tab && TABS.some((t) => t.key === tab)) this.tab = tab;
    const setting = q.get("setting");
    if (setting) {
      this.tab = "timing";
      this.highlight = setting;
      if (!["inactivity_timeout", "exit_confirmation", "correlation_window", "dropout_grace"].includes(setting)) this.advanced = true;
    }
    const sensor = q.get("sensor");
    if (sensor && /^[a-z0-9_]+\.[a-z0-9_]+$/.test(sensor)) this.select(sensor);
  }

  private watchIds(): string[] {
    const r = this.data?.room;
    if (!r) return [];
    return [...(Object.values(r.entities).filter(Boolean) as string[]), ...r.sensors.map((s) => s.entity_id)];
  }

  /**
   * Load the workspace. With unsaved edits the draft's base (sensors, timing, revision) is kept, so a save is
   * checked against the configuration the edits were made on; only the live view is refreshed. ``rebase``
   * replays the edits onto the current configuration instead (after a change elsewhere).
   */
  async load(rebaseDraft = false): Promise<void> {
    const area = this.areaId;
    try {
      let data = await getRooms<PresenceWorkspace>(`api/presence/rooms/${encodeURIComponent(area)}`);
      if (area !== this.areaId) return;
      this.offset = clockOffset(data.now);
      const first = !this.data;
      const old = this.data;
      if (old && this.dirty()) {
        if (data.room.revision !== old.room.revision) {
          if (rebaseDraft) {
            if (this.sensors) {
              const shape = (sensors: SensorConfig[]) => ({ enabled: true, sensors: norm(sensors), lights: [], reference_occupancy_entity: null, adjacent_rooms: [] });
              this.sensors = rebase(shape(old.room.sensors), shape(this.sensors), shape(data.room.sensors)).sensors;
            }
            this.changedElsewhere = false;
          } else {
            this.changedElsewhere = true;
            const keep = { sensors: old.room.sensors, timing: old.room.timing, timing_overrides: old.room.timing_overrides, revision: old.room.revision };
            data = { ...data, room: { ...data.room, ...keep } };
          }
        }
      }
      this.data = data;
      this.error = null;
      if (first) {
        watchPresence([data.room]);
        if (this.highlight) void this.updateComplete.then(() => this.querySelector(`[data-timing="${this.highlight}"]`)?.scrollIntoView({ block: "center" }));
      }
    } catch (err) {
      this.error = { message: err instanceof Error ? err.message : String(err), code: err instanceof ApiError ? err.code : "error" };
    }
  }

  private async onConfigChanged(): Promise<void> {
    if (this.data) await this.load();
  }

  // ----------------------------------------------------------------- draft
  private base(): SensorConfig[] {
    return norm(this.data?.room.sensors ?? []);
  }

  private draft(): SensorConfig[] {
    return this.sensors ?? this.base();
  }

  private sensorsChanged(): boolean {
    return this.sensors !== null && JSON.stringify(norm(this.sensors)) !== JSON.stringify(this.base());
  }

  private views() {
    const d = this.data;
    return d ? timingViews(d.timing_schema, "room", d.global.timing, d.room.timing_overrides, this.timing, this.resetTiming) : [];
  }

  private change(): PresenceChange {
    const out: PresenceChange = {};
    if (this.sensorsChanged()) out.sensors = norm(this.sensors!);
    const t = timingChange(this.views());
    if (this.resetTiming) out.reset_timing = true;
    if (Object.keys(t).length) out.timing = t;
    return out;
  }

  dirty(): boolean {
    const c = this.change();
    return Boolean(c.sensors || c.timing || c.reset_timing);
  }

  private editSensors(mutate: (list: SensorConfig[]) => void): void {
    const list = norm(this.draft());
    mutate(list);
    this.sensors = list;
    this.notice = null;
  }

  private setSensor(id: string, patch: Partial<SensorConfig>): void {
    const schema = this.data?.schema ?? null;
    this.editSensors((list) => {
      const s = list.find((x) => x.entity_id === id);
      if (!s) return;
      if (patch.kind && patch.kind !== s.kind) {
        const old = this.base().find((x) => x.entity_id === id);
        s.role = old && old.kind === patch.kind ? old.role : kindDefaultRole(schema, patch.kind);
      }
      Object.assign(s, patch);
    });
  }

  private addSensor(c: Candidate): void {
    const schema = this.data?.schema ?? null;
    const kinds = kindsFor(schema, c.entity_id, c.suggested_kind);
    const kind = (kinds.find((k) => k.value === c.suggested_kind) ?? kinds[0])?.value ?? "other";
    this.extra.set(c.entity_id, c);
    this.editSensors((list) => {
      if (!list.some((s) => s.entity_id === c.entity_id)) list.push({ entity_id: c.entity_id, kind, role: kindDefaultRole(schema, kind), inverted: false, enabled: true });
    });
    const ids = [...this.watchIds(), ...this.extra.keys()];
    live.watch(ids);
    this.select(c.entity_id);
  }

  private removeSensor(id: string): void {
    this.editSensors((list) => list.splice(list.findIndex((s) => s.entity_id === id), 1));
    if (this.selected === id) this.select(null);
  }

  private discard(): void {
    this.sensors = null;
    this.timing = {};
    this.resetTiming = false;
    this.changedElsewhere = false;
    this.notice = null;
  }

  private select(id: string | null): void {
    this.selected = id;
    this.inspectorOpen = id !== null;
    this.sensorAdvanced = false;
  }

  // ---------------------------------------------------------- review/save
  private async openReview(): Promise<void> {
    if (!this.data) return;
    this.busy = true;
    this.reviewError = null;
    this.review = null;
    this.reviewOpen = true;
    this.requestUpdate();
    await this.updateComplete;
    this.querySelector<HTMLDialogElement>("dialog.presence-review")?.showModal();
    try {
      this.reviewed = { change: this.change(), revision: this.data.room.revision };
      this.review = await postRooms<PresenceReview>("api/presence/review", { area_id: this.areaId, ...this.reviewed });
      this.confirmed = new Set();
    } catch (err) {
      this.reviewError = { message: err instanceof Error ? err.message : String(err), code: err instanceof ApiError ? err.code : "error" };
    } finally {
      this.busy = false;
    }
  }

  private closeReview(): void {
    this.querySelector<HTMLDialogElement>("dialog.presence-review")?.close();
    this.reviewOpen = false;
    this.review = null;
  }

  private async save(): Promise<void> {
    if (!this.data || !this.review) return;
    this.busy = true;
    this.reviewError = null;
    try {
      // Exactly what was reviewed, against the revision it was reviewed on.
      const reviewed = this.reviewed ?? { change: this.change(), revision: this.data.room.revision };
      const result = await postRooms<{ result: { applied: string } }>("api/presence/save", { area_id: this.areaId, ...reviewed, confirmed: [...this.confirmed] });
      this.seenRooms = store.roomsVersion;
      this.discard();
      this.closeReview();
      this.notice = result.result.applied === "live"
        ? "Saved. The integration applied it at once; the engine kept every sensor's value."
        : result.result.applied === "reload" ? "Saved. The integration reloaded to apply it; occupancy was restored from its saved state." : "Nothing changed.";
      await this.load();
    } catch (err) {
      const code = err instanceof ApiError ? err.code : "error";
      if (code === "stale_revision" || code === "confirmation_required") {
        await this.load(true);
        try {
          this.reviewed = { change: this.change(), revision: this.data.room.revision };
          this.review = await postRooms<PresenceReview>("api/presence/review", { area_id: this.areaId, ...this.reviewed });
          this.confirmed = new Set();
        } catch {
          /* the error below explains */
        }
      }
      this.reviewError = {
        code,
        message: code === "stale_revision" ? "This room was changed elsewhere while you were reviewing. The review below compares your edits with the current configuration."
          : code === "confirmation_required" ? "The consequences changed since the review. Check them again below." : err instanceof Error ? err.message : String(err),
      };
    } finally {
      this.busy = false;
    }
  }

  // --------------------------------------------------------------- helpers
  private signal(id: string): Signal | undefined {
    return this.data?.room.signals.find((s) => s.entity_id === id);
  }

  private nameOf = (id: string | undefined): string => {
    if (!id) return "—";
    return this.signal(id)?.name ?? this.data?.candidates.find((c) => c.entity_id === id)?.name ?? this.extra.get(id)?.name ?? id;
  };

  private kindLabel = (k: unknown): string => this.data?.schema?.kinds.find((x) => x.value === k)?.label ?? String(k ?? "—");
  private roleLabel = (r: unknown): string => (this.data?.schema?.roles.find((x) => x.value === r)?.label ?? String(r ?? "—")).split(":")[0];

  private liveCondition(id: string): string {
    const sig = this.signal(id);
    const st = store.liveStates.get(id);
    if (sig) return sig.condition;
    if (st?.availability && st.availability !== "ok") return "unavailable";
    return "clear";
  }

  // ------------------------------------------------------------ rendering
  private renderHead(ws: PresenceWorkspace): TemplateResult {
    const r = ws.room;
    const occ = r.occupancy;
    const fmt = haTime(true);
    return html`<header class="editor-head">
      <a class="btn back-btn" href="#/presence" data-testid="back-to-presence">${icon("arrow-left", 20)}<span>Presence</span></a>
      <div class="editor-title">
        <h2 tabindex="-1">${r.name}</h2>
        <div class="chips">${r.enabled ? stateChip(occ?.state, occ?.state_label) : chip("pending", "Not observed")}
          ${occ?.state_since ? html`<span class="muted small">since ${fmt(occ.state_since)}</span>` : nothing}
          ${r.automation.lighting_automatic ? chip("info", r.automation.lighting_paused ? "Automatic lighting (paused)" : "Automatic lighting") : chip("neutral", "Lighting: Observing")}
          ${chip("neutral", "Engine: Smart Occupancy integration")}</div>
      </div>
      <div class="editor-actions">
        <a class="btn" href=${`#/rooms/${encodeURIComponent(r.area_id)}/presence`} data-testid="open-rooms-editor">${icon("floor-plan", 20)}<span>Rooms editor</span></a>
        <a class="btn" href="#/floorplan/live">${icon("home-map-marker", 20)}<span>Floor plan</span></a>
        <button type="button" class="icon-btn panel-toggle" aria-pressed=${this.leftOpen ? "true" : "false"} aria-label="Show sensors panel" @click=${() => (this.leftOpen = !this.leftOpen)}>${icon("dock-left")}</button>
      </div>
    </header>`;
  }

  private renderUnsaved(): unknown {
    if (!this.dirty()) return nothing;
    const c = this.change();
    const parts = [c.sensors ? "sensors" : null, c.timing || c.reset_timing ? "timing" : null].filter(Boolean).join(" and ");
    const invalid = this.views().some((v) => v.error);
    return html`<div class="unsaved-bar" data-testid="unsaved-bar"><span class="unsaved-text">${icon("pencil", 20)}<strong>Unsaved changes</strong><span class="muted">(${parts})</span></span>
      <span class="row-actions"><button type="button" class="btn" data-testid="presence-discard" @click=${() => this.discard()}>Discard</button>
      <button type="button" class="btn btn-primary" data-testid="presence-review-open" ?disabled=${invalid || !this.data?.writable} @click=${() => void this.openReview()}>${icon("check", 20)}Review and save</button></span></div>`;
  }

  private renderLeft(ws: PresenceWorkspace): TemplateResult {
    const draft = this.draft();
    const assigned = new Set(draft.map((s) => s.entity_id));
    const base = new Set(this.base().map((s) => s.entity_id));
    const candidates = ws.candidates.filter((c) => !assigned.has(c.entity_id));
    const removed = this.base().filter((s) => !assigned.has(s.entity_id));
    return html`<aside class="ws-left presence-left" aria-label="Sensors" data-testid="presence-left">
      <section class="panel-section">
        <label class="panel-title" for="room-switch">${icon("home", 18)}Room</label>
        <select id="room-switch" data-testid="room-switch" .value=${ws.room.area_id} @change=${(e: Event) => (window.location.hash = `/presence/${encodeURIComponent((e.target as HTMLSelectElement).value)}`)}>
          ${ws.rooms.map((r) => html`<option value=${r.area_id} ?selected=${r.area_id === ws.room.area_id}>${r.name}${r.enabled ? "" : " · not observed"}</option>`)}
        </select>
      </section>
      <section class="panel-section" data-testid="assigned-sensors">
        <h3 class="panel-title">${icon("motion-sensor", 18)}Sensors (${draft.length})</h3>
        ${draft.length ? html`<ul class="sensor-nav">${draft.map((s) => {
          const sig = this.signal(s.entity_id);
          const cond = this.liveCondition(s.entity_id);
          const isNew = !base.has(s.entity_id);
          return html`<li><button type="button" class="sensor-nav-item ${this.selected === s.entity_id ? "selected" : ""} ${s.enabled === false ? "excluded" : ""}" data-sensor=${s.entity_id} @click=${() => this.select(s.entity_id)}>
            <span class="sig-dot sig-${cond}" aria-hidden="true"></span>${icon(KIND_ICON[s.kind] ?? "puzzle", 20)}
            <span class="palette-text"><span class="palette-name">${this.nameOf(s.entity_id)}</span>
              <span class="small muted">${this.kindLabel(s.kind)} · ${s.kind === "door" ? "movements" : this.roleLabel(s.role)}${s.inverted ? " · inverted" : ""}${s.enabled === false ? " · excluded" : ""}</span></span>
            ${isNew ? chip("info", "New") : html`<span class="small ${cond === "active" ? "tone-ok" : cond === "clear" ? "muted" : "tone-warning"}">${sig ? CONDITION_INFO[sig.condition]?.label ?? sig.condition : "—"}</span>`}
          </button></li>`;
        })}</ul>` : html`<p class="muted small">No sensors. Without one, the room's occupancy cannot be observed.</p>`}
        ${removed.length ? html`<p class="small tone-warning">${removed.length} sensor${removed.length > 1 ? "s" : ""} removed (unsaved): ${removed.map((s) => this.nameOf(s.entity_id)).join(", ")}</p>` : nothing}
      </section>
      <section class="panel-section" data-testid="candidate-sensors">
        <h3 class="panel-title">${icon("plus", 18)}Available in this area</h3>
        ${candidates.length ? html`<ul class="device-palette">${candidates.map((c) => html`<li>
            <button type="button" class="palette-item ${this.selected === c.entity_id ? "selected" : ""}" @click=${() => this.select(c.entity_id)}>
              <span class="sig-dot sig-${c.availability === "ok" ? (c.state === "on" ? "active" : "clear") : "unavailable"}" aria-hidden="true"></span>
              <span class="palette-text"><span class="palette-name">${c.name}</span><span class="small muted">${c.category_label}${c.sensor_rooms.length ? " · used elsewhere" : ""}</span></span></button>
            <button type="button" class="icon-btn add-btn" aria-label="Add ${c.name}" data-add=${c.entity_id} ?disabled=${!ws.writable} @click=${() => this.addSensor(c)}>${icon("plus", 20)}</button></li>`)}</ul>`
          : html`<p class="muted small">No other presence sensor found in this area.</p>`}
        <button type="button" class="btn" data-testid="presence-add-other" ?disabled=${!ws.writable} @click=${() => (this.pick = true)}>${icon("search", 18)}Add another sensor…</button>
      </section>
    </aside>`;
  }

  private renderTabs(ws: PresenceWorkspace): TemplateResult {
    const diags = ws.room.diagnostics.filter((d) => d.level !== "info").length;
    return html`<nav class="tabs" role="tablist" aria-label="Presence">${TABS.map((t) => html`<a href="#" role="tab" class=${this.tab === t.key ? "active" : ""} aria-selected=${this.tab === t.key ? "true" : "false"} data-tab=${t.key}
      @click=${(e: Event) => (e.preventDefault(), (this.tab = t.key))}>${icon(t.icon, 18)}<span>${t.title}</span>
      ${t.key === "diagnostics" && diags ? html`<span class="tab-badge">${diags}</span>` : nothing}
      ${t.key === "timing" && (Object.keys(timingChange(this.views())).length || this.resetTiming) ? html`<span class="tab-badge info">•</span>` : nothing}</a>`)}</nav>`;
  }

  private progress(label: string, elapsed: number | null, total: number | null, at: string | null, testid: string): TemplateResult {
    const left = secondsUntil(at, this.offset);
    const pct = elapsed !== null && total ? Math.max(0, Math.min(100, (elapsed / total) * 100)) : null;
    return html`<div class="progress-block" data-testid=${testid}>
      <div class="progress-head"><span>${label}</span><strong class="mono">${left !== null ? clock(left) : "—"}</strong></div>
      <div class="meter wide"><span style="width:${pct ?? 0}%"></span></div>
      <span class="small muted">${total ? `${duration(Math.max(0, Math.floor(elapsed ?? 0)))} of ${duration(total)}` : "Exact timing not reported"}${at ? ` · at ${haTime(true)(at)}` : ""}</span>
    </div>`;
  }

  private renderLive(ws: PresenceWorkspace): TemplateResult {
    const r = ws.room;
    const occ = r.occupancy;
    const fmt = haTime(true);
    if (!r.enabled || !occ) {
      return html`<div class="tab-stack"><section class="card empty-state">${icon("motion-sensor", 36)}
        <p class="muted">${r.enabled ? "The integration is not running this room's engine right now." : "This room is not enabled, so its occupancy is not observed. Enable it in the Rooms editor."}</p>
        <a class="btn" href=${`#/rooms/${encodeURIComponent(r.area_id)}/overview`}>${icon("floor-plan", 18)}Rooms editor</a></section></div>`;
    }
    const transitions = ws.log.filter((e) => e.kind === "transition" && e.area_id === r.area_id);
    const last = transitions.at(-1) ?? null;
    const chain = visitChain(ws.log, r.area_id);
    const window = r.timing.find((t) => t.key === "correlation_window")?.value ?? null;
    const sinceState = secondsSince(occ.state_since, this.offset);
    const holding = r.signals.filter((s) => s.status === "active" || s.status === "grace");
    const groups: Array<{ title: string; list: Signal[] }> = [
      { title: "Evidence now", list: holding },
      { title: "Door movements", list: r.signals.filter((s) => s.status === "recent") },
      { title: "Clear", list: r.signals.filter((s) => s.status === "cleared" || s.status === "idle") },
      { title: "Not usable", list: r.signals.filter((s) => s.status === "unknown" || s.status === "stuck") },
      { title: "Not used", list: r.signals.filter((s) => s.status === null || s.status === "ignored") },
    ];
    return html`<div class="tab-stack">
      <section class="card live-card occ-border-${occ.state}" data-testid="live-card">
        <div class="live-headline">
          <span class="occ-big occ-${occ.state}">${icon(STATE_INFO[occ.state].icon, 28)}<span data-testid="live-state">${occ.state_label}</span></span>
          <span class="muted">for ${sinceState !== null ? duration(Math.max(0, Math.floor(sinceState))) : "—"}</span>
          ${occ.confidence ? html`<span class="confidence big" title=${`${occ.confidence.formula}. ${occ.confidence.basis}. Evidence quality, not a probability.`}>
            <span class="small muted">Confidence</span><span class="meter"><span style="width:${occ.confidence.score}%"></span></span><strong>${occ.confidence.score}</strong></span>` : html`<span class="muted small">Confidence not reported</span>`}
        </div>
        <blockquote class="engine-reason" data-testid="engine-reason">${occ.reason}</blockquote>
        ${occ.state === "possible_occupancy" ? this.progress("Confirmation", sinceState, window, r.confirm_at, "confirm-progress") : nothing}
        ${occ.state === "possible_vacancy" ? this.progress(r.explain?.vacancy_path === "fast" ? "Fast vacancy confirmation" : "Vacancy confirmation", secondsSince(occ.quiet_since, this.offset), occ.required_quiet, occ.vacancy_at, "vacancy-progress") : nothing}
        ${r.explain?.next.length ? html`<div class="next-steps" data-testid="next-steps"><h4>What happens next</h4><ul>${r.explain.next.map((n) => {
            const left = secondsUntil(n.at, this.offset);
            return html`<li data-next=${n.code}><span>${n.text}</span>${n.at ? html`<span class="next-at"><span class="mono">${fmt(n.at)}</span>${left !== null && left > 0 ? html` · in <strong class="mono">${clock(left)}</strong>` : nothing}</span>` : nothing}</li>`;
          })}</ul></div>` : nothing}
        ${stateMachine(occ.state, last ? { from: last.previous_state, to: last.state } : null, ws.transitions)}
      </section>
      <div class="tab-grid">
        <section class="card" data-testid="evidence"><header class="card-head">${icon("motion-sensor")}<h3>Detection sources</h3></header>
          ${groups.filter((g) => g.list.length).map((g) => html`<h4 class="section-title">${g.title}</h4><ul class="evidence-list">${g.list.map((s) => html`<li>
              <button type="button" class="row-main" @click=${() => this.select(s.entity_id)}><span class="sig-dot sig-${s.condition}"></span>
              <span><span class="cell-name">${s.name}</span><span class="small muted block">${STATUS_TEXT[s.status ?? "ignored"] ?? s.status}${s.since ? ` · since ${fmt(s.since)}` : ""}</span></span></button>
              <span class="small">${s.label}</span></li>`)}</ul>`)}
          ${occ.zone_holds.length ? html`<p class="small">Held by the zones ${occ.zone_holds.join(", ")}.</p>` : nothing}
          ${occ.preliminary_signals.length ? html`<p class="small muted">Possible occupancy lighting is active: ${occ.preliminary_signals.map((e) => this.nameOf(e)).join(", ")} cannot confirm occupancy by themselves.</p>` : nothing}
          <p class="small muted">Last activity: ${occ.last_activity ? html`${fmt(occ.last_activity)} (${ago(secondsSince(occ.last_activity, this.offset))})${occ.last_activity_name ? ` · ${occ.last_activity_name}` : ""}` : "none recorded"}</p>
        </section>
        <section class="card" data-testid="visit-chain"><header class="card-head">${icon("arrow-right")}<h3>This visit, step by step</h3></header>
          ${chain.length ? html`<ol class="chain">${chain.map((c) => html`<li class=${c.state ? `chain-state occ-${c.state}` : "chain-evidence"}><time class="mono small">${fmt(c.time)}</time><span>${c.text}</span></li>`)}</ol>`
            : html`<p class="muted small">No decisions recorded since the integration started. The integration's log keeps the latest decisions while Home Assistant runs; older ones are in the Timeline (Recorder).</p>`}
        </section>
      </div>
      ${r.zones.length ? this.renderZones(ws) : nothing}
      <sl-presence-timeline compact .room=${r} .log=${ws.log}></sl-presence-timeline>
    </div>`;
  }

  private renderZones(ws: PresenceWorkspace): TemplateResult {
    const fmt = haTime(true);
    return html`<section class="card" data-testid="presence-zones"><header class="card-head">${icon("radar")}<h3>Lighting zones</h3>${chip("neutral", "Read-only")}</header>
      <p class="muted small">Zones belong to the lighting configuration (Smart Room Lighting card). Radar distance zones use a radial band: a distance from the radar in any direction it can see, not a position.</p>
      <ul class="zone-list">${ws.room.zones.map((z) => {
        const lr = z.live_radar;
        return html`<li class="zone ${z.active ? "active" : ""}">
          <div><strong>${z.name}</strong> ${chip(z.active ? "ok" : z.available === false ? "warning" : "neutral", z.active ? "Active" : z.available === false ? "Unavailable" : "Inactive")}
            ${z.holds_room ? html`<span class="small muted"> · holds the room</span>` : nothing}${z.enabled ? nothing : html`<span class="small muted"> · off</span>`}</div>
          ${z.detection === "radar" && z.radar ? html`<div class="radar-band" data-testid="radar-band">
              ${this.bandBar(z.radar.min_m, z.radar.max_m, z.radar.hysteresis, lr?.distance_valid ? lr.distance_m : null)}
              ${kv([
                ["Configured boundary", `${z.band}${z.radar.hysteresis ? ` (± ${z.radar.hysteresis} m hysteresis)` : ""}`],
                ["Measured distance", lr ? (lr.distance_valid ? `${lr.distance_text} (${lr.distance_name ?? z.radar.distance})` : lr.distance_text ?? "No valid measurement") : "—"],
                ["Radar presence", lr ? (lr.presence === null ? "Unknown" : lr.presence ? "Detected" : "Clear") : "—"],
                ["Zone decision", z.reason ?? "—"],
              ])}</div>`
            : html`<p class="small muted">Sensors: ${z.sensors.map((e) => this.nameOf(e)).join(", ") || "the room's"}${z.since ? ` · since ${fmt(z.since)}` : ""}</p>`}
        </li>`;
      })}</ul></section>`;
  }

  /** Configured band and the measured distance on one axis (0 to a bit past the band). No direction, no position. */
  private bandBar(min: number, max: number, hyst: number, measured: number | null): TemplateResult {
    const top = Math.max(max + hyst, measured ?? 0) * 1.25 || 1;
    const pct = (m: number): number => Math.max(0, Math.min(100, (m / top) * 100));
    return html`<div class="band-axis" role="img" aria-label=${`Band ${min} to ${max} m${measured !== null ? `, measured ${measured} m` : ""}`}>
      <span class="band-hyst" style="left:${pct(Math.max(0, min - hyst))}%;width:${pct(max + hyst) - pct(Math.max(0, min - hyst))}%"></span>
      <span class="band" style="left:${pct(min)}%;width:${pct(max) - pct(min)}%"></span>
      ${measured !== null ? html`<span class="band-mark" style="left:${pct(measured)}%" title="Measured distance"></span>` : nothing}
      <span class="band-zero">0 m</span><span class="band-top">${Math.round(top * 10) / 10} m</span>
    </div>`;
  }

  private renderTiming(ws: PresenceWorkspace): TemplateResult {
    const views = this.views();
    const overrides = views.filter((v) => v.overridden).length;
    return html`<div class="tab-stack">
      <section class="card" data-testid="room-timing"><header class="card-head">${icon("tune")}<h3>Occupancy timing</h3>
        ${chip(overrides ? "info" : "neutral", overrides ? `${overrides} room value${overrides > 1 ? "s" : ""}` : "All global")}
        <span class="spacer"></span>
        <button type="button" class="btn btn-small" data-testid="timing-reset-all" ?disabled=${!overrides || !ws.writable} @click=${() => ((this.resetTiming = true), (this.timing = {}))}>${icon("undo", 16)}Use global for all</button></header>
        <p class="muted small">Each value is the room's own or inherits the <a class="text-link" href="#/presence/settings">global default</a>. Typing a value overrides it for this room; “Use global” removes the override. The integration validates every value when you save.</p>
        ${timingEditor(views, {
          scope: "room", units: this.units, showAdvanced: this.advanced, onToggleAdvanced: () => (this.advanced = !this.advanced), highlight: this.highlight,
          onValue: (k, v) => ((this.timing = { ...this.timing, [k]: v }), (this.notice = null)),
          onUnit: (k, u) => (this.units.set(k, u), this.requestUpdate()), disabled: !ws.writable,
        })}
      </section>
    </div>`;
  }

  private renderDiagnostics(ws: PresenceWorkspace): TemplateResult {
    const list = ws.room.diagnostics;
    const go = (d: Diagnostic): void => {
      if (d.target === "sensors" && d.entity_id) this.select(d.entity_id);
      else if (d.target === "timing") {
        this.tab = "timing";
        this.highlight = d.setting;
        if (d.setting && !["inactivity_timeout", "exit_confirmation", "correlation_window", "dropout_grace"].includes(d.setting)) this.advanced = true;
        void this.updateComplete.then(() => this.querySelector(`[data-timing="${d.setting}"]`)?.scrollIntoView({ block: "center" }));
      } else if (d.target === "rooms_editor") window.location.hash = `/rooms/${encodeURIComponent(ws.room.area_id)}/presence`;
      else this.tab = "live";
    };
    return html`<div class="tab-stack"><section class="card" data-testid="room-diagnostics"><header class="card-head">${icon("stethoscope")}<h3>Diagnostics</h3></header>
      ${list.length ? html`<ul class="diag-list">${list.map((d) => diagnosticItem(d, ws.room.area_id, { onGo: go }))}</ul>` : html`<p class="muted">No problems found for this room.</p>`}
      <p class="muted small">Nothing is changed automatically. “Suggestion” items may be intended.</p></section></div>`;
  }

  private renderInspector(ws: PresenceWorkspace): TemplateResult {
    const id = this.selected;
    const sensor = id ? this.draft().find((s) => s.entity_id === id) : undefined;
    const candidate = id && !sensor ? ws.candidates.find((c) => c.entity_id === id) ?? this.extra.get(id) : undefined;
    let body: TemplateResult;
    if (sensor) body = this.sensorInspector(ws, sensor);
    else if (candidate) body = this.candidateInspector(ws, candidate);
    else body = this.roomInspector(ws);
    return html`<aside class="inspector presence-inspector" aria-label="Inspector" data-testid="presence-inspector">${body}</aside>`;
  }

  private inspectorHead(title: string, subtitle: unknown, iconName: string): TemplateResult {
    return html`<header class="inspector-head"><span class="inspector-icon">${icon(iconName, 22)}</span><div><h3>${title}</h3><span class="muted small mono">${subtitle}</span></div>
      ${this.selected ? html`<button type="button" class="icon-btn" aria-label="Close the inspector" @click=${() => this.select(null)}>${icon("close")}</button>` : nothing}</header>`;
  }

  private sensorInspector(ws: PresenceWorkspace, s: SensorConfig): TemplateResult {
    const sig = this.signal(s.entity_id);
    const st = store.liveStates.get(s.entity_id);
    const fmt = haTime(true);
    const schema = ws.schema;
    const suggested = sig?.suggested_kind ?? this.extra.get(s.entity_id)?.suggested_kind ?? "other";
    const kinds = kindsFor(schema, s.entity_id, suggested);
    const availability = (st?.availability ?? sig?.availability ?? "ok") as keyof typeof AVAILABILITY;
    const avail = AVAILABILITY[availability] ?? AVAILABILITY.ok;
    const base = this.base().find((x) => x.entity_id === s.entity_id);
    const changed = base && JSON.stringify(base) !== JSON.stringify(norm([s])[0]);
    const timing = ws.room.timing;
    const value = (k: string): string => duration(timing.find((t) => t.key === k)?.value ?? null);
    const door = s.kind === "door";
    return html`${this.inspectorHead(this.nameOf(s.entity_id), s.entity_id, KIND_ICON[s.kind] ?? "puzzle")}
      <section class="inspector-section" data-testid="inspector-live"><h4>Now</h4>
        <div class="live-state">${chip((CONDITION_INFO[sig?.condition ?? "clear"]?.tone ?? "neutral") as Tone, sig ? CONDITION_INFO[sig.condition]?.label ?? sig.condition : "Not observed yet")}
          <span class="state-value">${sig?.label ?? st?.state ?? "—"}</span></div>
        ${sig ? html`<p class="small">${STATUS_TEXT[sig.status ?? "ignored"] ?? ""}</p>` : html`<p class="small muted">Not used by the engine until it is saved.</p>`}
        ${kv([
          ["Last changed", fmt(st?.last_changed ?? sig?.last_changed ?? null)],
          sig?.since ? ["Current value since (engine)", fmt(sig.since)] : null,
          sig?.deadlines?.sustained_at && ws.room.occupancy?.state === "possible_occupancy" ? ["Confirms occupancy by itself at", `${fmt(sig.deadlines.sustained_at)} (if still active)`] : null,
          sig?.deadlines?.stuck_at ? ["Ignored as stuck from", fmt(sig.deadlines.stuck_at)] : null,
          sig?.deadlines?.grace_until ? ["Dropout bridged until", fmt(sig.deadlines.grace_until)] : null,
          sig?.deadlines?.recent_until ? ["Counts as recent movement until", fmt(sig.deadlines.recent_until)] : null,
          sig?.preliminary ? ["Possible occupancy lighting", "Starts it; cannot confirm occupancy alone"] : null,
        ])}
      </section>
      <section class="inspector-section" data-testid="inspector-config"><h4>Configuration${changed ? html` ${chip("info", "Changed")}` : nothing}</h4>
        <label class="field-label" for="kind-select">Sensor type</label>
        <select id="kind-select" data-testid="kind-select" .value=${s.kind} ?disabled=${!ws.writable} @change=${(e: Event) => this.setSensor(s.entity_id, { kind: (e.target as HTMLSelectElement).value })}>
          ${kinds.map((k) => html`<option value=${k.value} ?selected=${k.value === s.kind}>${k.label}</option>`)}</select>
        ${sig?.ha_category ? html`<p class="small muted">Home Assistant suggests: <strong>${sig.ha_category_label}</strong> (${sig.ha_category_reason}; ${sig.ha_category_confidence} confidence).
          ${suggested && suggested !== s.kind && kinds.some((k) => k.value === suggested) ? html`<button type="button" class="link-btn" @click=${() => this.setSensor(s.entity_id, { kind: suggested })}>Use ${this.kindLabel(suggested)}</button>` : nothing}</p>` : nothing}
        <fieldset class="role-choices" data-testid="role-choices"><legend class="field-label">Role</legend>
          ${(schema?.roles ?? []).map((r) => html`<label class="role-choice ${s.role === r.value ? "selected" : ""}"><input type="radio" name="role-${s.entity_id}" value=${r.value} data-role=${r.value} .checked=${s.role === r.value} ?disabled=${!ws.writable}
              @change=${() => this.setSensor(s.entity_id, { role: r.value })} />
            <span><strong>${ROLE_HELP[r.value]?.title ?? r.label}</strong><span class="small muted block">${ROLE_HELP[r.value]?.text ?? r.label}</span></span></label>`)}
        </fieldset>
        ${door ? html`<p class="small notice notice-info">${icon("door", 18)}<span>${DOOR_HELP}</span></p>` : nothing}
        <label class="switch-row"><input type="checkbox" role="switch" data-testid="inverted-toggle" .checked=${Boolean(s.inverted)} ?disabled=${!ws.writable} @change=${(e: Event) => this.setSensor(s.entity_id, { inverted: (e.target as HTMLInputElement).checked })} />
          <span>Inverted<span class="small muted block">Counts as active when it is off (for example a “PC idle” sensor).</span></span></label>
        <label class="switch-row"><input type="checkbox" role="switch" data-testid="enabled-toggle" .checked=${s.enabled !== false} ?disabled=${!ws.writable} @change=${(e: Event) => this.setSensor(s.entity_id, { enabled: (e.target as HTMLInputElement).checked })} />
          <span>Included in occupancy<span class="small muted block">Excluded sensors keep their type and role; the engine ignores them.</span></span></label>
        <p class="small muted">${EFFECT_TEXT[s.enabled === false || s.role === "ignore" ? "none" : door ? "door" : s.role === "trigger" ? "trigger" : "presence"]}</p>
      </section>
      <section class="inspector-section"><h4>Availability and device</h4>
        <div class="live-state"><span class="has-tip" title=${avail.help}>${chip(avail.tone as Tone, avail.label)}</span></div>
        <p class="small muted">${avail.help}</p>
        ${kv([
          ["Device", sig?.device ?? "—"],
          ["Model", sig?.device_model ?? "—"],
          ["Area in Home Assistant", sig?.area_ha ?? "—"],
          ["Device class", sig?.device_class ?? "—"],
        ])}
      </section>
      <section class="inspector-section">
        <button type="button" class="collapse-toggle" aria-expanded=${this.sensorAdvanced ? "true" : "false"} @click=${() => (this.sensorAdvanced = !this.sensorAdvanced)}>${icon(this.sensorAdvanced ? "chevron-down" : "chevron-right", 18)}<span>Advanced</span></button>
        ${this.sensorAdvanced ? html`${kv([
            ["Entity", html`<span class="mono">${s.entity_id}</span>`],
            ["Raw state", html`<span class="mono">${st?.state ?? sig?.state ?? "—"}</span>`],
            ["Engine value", sig?.value ?? "—"],
            ["Also used by", sig?.other_rooms?.length ? sig.other_rooms.map((a) => ws.areas.find((x) => x.area_id === a)?.name ?? a).join(", ") : "No other room"],
            ["Correlation window", value("correlation_window")],
            [door ? "Exit evidence" : s.role === "trigger" ? "Stuck after" : "Dropout bridge", door ? `movement then no activity (fast vacancy ${value("exit_confirmation")})` : s.role === "trigger" ? value("trigger_stuck_after") : value("dropout_grace")],
            s.role !== "trigger" && !door ? ["Stuck after", timing.find((t) => t.key === "presence_stuck_after")?.value ? value("presence_stuck_after") : "off"] : null,
            sig?.lost_while_active ? ["Dropped out while active", "Occupancy is held one extra vacancy timeout"] : null,
          ])}
          <p class="small muted">Timing is set for the whole room in the Timing tab.</p>` : nothing}
        <button type="button" class="btn btn-danger" data-testid="remove-sensor" ?disabled=${!ws.writable} @click=${() => this.removeSensor(s.entity_id)}>${icon("delete", 18)}Remove from this room</button>
      </section>`;
  }

  private candidateInspector(ws: PresenceWorkspace, c: Candidate): TemplateResult {
    const st = store.liveStates.get(c.entity_id);
    return html`${this.inspectorHead(c.name, c.entity_id, "plus")}
      <section class="inspector-section"><h4>Not assigned to this room</h4>
        ${kv([["Home Assistant suggests", `${c.category_label} (${c.category_reason})`], ["State", st?.state ?? c.state ?? "—"], ["Area", c.area ?? "—"],
              ["Also used by", c.sensor_rooms.length ? c.sensor_rooms.join(", ") : "No room"]])}
        <button type="button" class="btn btn-primary" data-add=${c.entity_id} ?disabled=${!ws.writable} @click=${() => this.addSensor(c)}>${icon("plus", 18)}Add to this room</button>
        <p class="small muted">It is added to the draft as a ${this.kindLabel(c.suggested_kind)}; change its type and role before saving.</p>
      </section>`;
  }

  private roomInspector(ws: PresenceWorkspace): TemplateResult {
    const r = ws.room;
    const views = this.views();
    return html`${this.inspectorHead(r.name, r.area_id, "home")}
      <section class="inspector-section"><h4>Room</h4>
        ${kv([
          ["Observed", r.enabled ? "Yes" : "No (not enabled)"],
          ["Automatic lighting", r.automation.lighting_automatic ? (r.automation.lighting_paused ? "Yes (paused by the Manual mood)" : "Yes") : "No (Observing)"],
          ["When vacant", (r.automation.vacancy_action ?? "—").replace(/_/g, " ")],
          ["Automatic curtains", r.automation.curtains_automatic.length ? r.automation.curtains_automatic.join(", ") : "None"],
          ["Reference helper", r.reference_occupancy_entity ?? "None"],
          ["Adjacent rooms", r.adjacent_rooms.length ? r.adjacent_rooms.map((a) => ws.areas.find((x) => x.area_id === a)?.name ?? a).join(", ") : "None"],
        ])}
      </section>
      <section class="inspector-section"><h4>Timing in force</h4>
        <ul class="plain-list small">${views.map((v) => html`<li><span>${v.item.label}</span>: <strong>${duration(v.effective)}</strong> ${v.overridden ? chip("info", "room") : html`<span class="muted">global</span>`}</li>`)}</ul>
        <button type="button" class="btn btn-small" @click=${() => (this.tab = "timing")}>${icon("tune", 16)}Edit timing</button>
      </section>
      <section class="inspector-section"><h4>Entities</h4>
        <ul class="plain-list small mono">${Object.entries(r.entities).filter(([, v]) => v).map(([k, v]) => html`<li>${k}: ${v}</li>`)}</ul>
        <p class="small muted">Select a sensor on the left to inspect and configure it.</p>
      </section>`;
  }

  protected render(): unknown {
    const ws = this.data;
    if (!ws) {
      return this.error
        ? html`${apiProblem(this.error.message, this.error.code)}<div class="actions"><a class="btn" href="#/presence">${icon("arrow-left", 20)}Presence</a>
            ${this.error.code === "not_configured" ? html`<a class="btn" href=${`#/rooms/${encodeURIComponent(this.areaId)}/presence`}>${icon("floor-plan", 20)}Set it up in Rooms</a>` : nothing}</div>`
        : html`<div class="card loading" role="status"><span class="spinner" aria-hidden="true"></span><span>Loading the room…</span></div>`;
    }
    let content: unknown;
    switch (this.tab) {
      case "timeline": content = html`<sl-presence-timeline .room=${ws.room} .log=${ws.log}></sl-presence-timeline>`; break;
      case "timing": content = this.renderTiming(ws); break;
      case "diagnostics": content = this.renderDiagnostics(ws); break;
      default: content = this.renderLive(ws);
    }
    const narrowInspector = this.selected !== null;
    return html`<div class="room-editor presence-room" data-area=${ws.room.area_id}>
      ${this.renderHead(ws)}
      ${!ws.writable ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>The integration is not running: presence can be viewed, not saved.</span></p>` : nothing}
      ${this.changedElsewhere ? html`<p class="notice notice-warning" data-testid="changed-elsewhere">${icon("warning", 20)}<span>This room's configuration was changed elsewhere while you were editing. Your unsaved changes are kept;
          <button type="button" class="btn" data-testid="review-current" @click=${() => void this.load(true).then(() => this.openReview())}>${icon("refresh", 18)}Review them against the current configuration</button></span></p>` : nothing}
      ${store.link === "offline" ? html`<p class="notice notice-error">${icon("offline", 20)}<span>The App backend cannot be reached. Your edits are kept here.</span></p>` : nothing}
      ${this.notice ? html`<p class="notice notice-info" role="status" data-testid="saved-notice">${icon("ok", 20)}<span>${this.notice}</span></p>` : nothing}
      ${this.renderUnsaved()}
      <div class="workspace presence-workspace inspector-open ${this.leftOpen ? "" : "left-collapsed"} ${narrowInspector ? "inspector-narrow-open" : ""}">
        ${this.leftOpen ? this.renderLeft(ws) : nothing}
        <section class="ws-center" aria-label="Detection">${this.renderTabs(ws)}<div class="tab-panel" role="tabpanel">${content}</div></section>
        ${this.renderInspector(ws)}
        ${narrowInspector ? html`<div class="inspector-scrim" @click=${() => this.select(null)}></div>` : nothing}
      </div>
      ${this.pick ? html`<sl-entity-pick .mode=${"sensor"} .areaId=${ws.room.area_id} .schema=${ws.schema} .exclude=${this.draft().map((s) => s.entity_id)}
          @picked=${(e: CustomEvent<{ candidate: RoomsCandidate }>) => {
            const c = e.detail.candidate;
            this.addSensor({ ...c, category_confidence: "medium", category_reason: "From the device search", device_class: null, last_changed: null });
            this.pick = false;
          }} @close=${() => (this.pick = false)}></sl-entity-pick>` : nothing}
      ${this.reviewOpen ? reviewDialog({
        review: this.review, error: this.reviewError, busy: this.busy, confirmed: this.confirmed,
        onConfirm: (c, on) => { const n = new Set(this.confirmed); if (on) n.add(c); else n.delete(c); this.confirmed = n; },
        onSave: () => void this.save(), onClose: () => this.closeReview(),
        name: this.nameOf, kindLabel: this.kindLabel, roleLabel: this.roleLabel,
        timingLabel: (k) => ws.timing_schema.find((t) => t.key === k)?.label ?? k,
      }) : nothing}
    </div>`;
  }
}

customElements.define("sl-presence-room", SlPresenceRoom);
