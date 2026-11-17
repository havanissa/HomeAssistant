// Room workspace (Phase A2): three panels - rooms and available devices
// (left), the room's tabs (centre) and an inspector for the selected item
// (right). Edits stay in a draft until they are reviewed and saved; the
// integration validates and stores them, and keeps running the automation.
// Nothing here sends a device command.
import { html, nothing, type TemplateResult } from "lit";

import { formatTime, plural } from "../format";
import { icon } from "../icons";
import { normalize } from "../lookup";
import { hashQuery, setLeaveGuard } from "../router";
import { live, store } from "../store";
import {
  ApiError,
  AVAILABILITY,
  changeGroup,
  changeText,
  diff,
  downloadJson,
  editable,
  getRooms,
  kindDefaultRole,
  kindsFor,
  payload,
  postRooms,
  rebase,
  STATUS,
  type Availability,
  type Candidate,
  type Change,
  type EntityInfo,
  type Review,
  type RoomConfig,
  type RoomsOverview,
  type RoomSummary,
  type SensorConfig,
  type Workspace,
} from "../rooms/model";
import { chip, kv, StoreElement, type Tone } from "../ui";
import { apiNotice, areaIcon, occupancyChip } from "./rooms";

export const TABS = [
  { key: "overview", title: "Overview", icon: "view-dashboard" },
  { key: "devices", title: "Devices", icon: "home" },
  { key: "presence", title: "Presence", icon: "motion-sensor" },
  { key: "lighting", title: "Lighting", icon: "lightbulb-group" },
  { key: "switches", title: "Switches", icon: "light-switch" },
  { key: "curtains", title: "Curtains", icon: "curtains" },
  { key: "diagnostics", title: "Diagnostics", icon: "stethoscope" },
] as const;

const GROUPS: Array<{ key: string; title: string }> = [
  { key: "presence", title: "Presence and contact sensors" },
  { key: "climate", title: "Temperature and light level" },
  { key: "lighting", title: "Lights and lighting outputs" },
  { key: "switches", title: "Wall switches and remotes" },
  { key: "curtains", title: "Curtains and covers" },
  { key: "other", title: "Other" },
];
const SENSOR_CATEGORIES = new Set(["pir", "presence", "door", "window", "pc_activity", "bed", "presence_input", "illuminance", "temperature"]);
const ROLE_HELP: Record<string, string> = {
  trigger: "Starts occupancy when it becomes active.",
  maintain: "Keeps the room occupied while active (cannot start it alone after a quiet period).",
  confirm_vacancy: "Holds occupancy; when it clears, vacancy is confirmed faster.",
  ignore: "Shown, but not used for occupancy.",
};
const CONDITION_LABEL: Record<string, string> = {
  active: "Active now", clear: "Clear", unavailable: "Unavailable", unknown: "No value", excluded: "Excluded",
  ignored: "Ignored", invalid: "State not understood", stuck: "Stuck", missing: "Missing",
};

type Assignment = { kind: "sensor"; sensor: SensorConfig } | { kind: "light" } | { kind: "reference" } | null;
type PickMode = "sensor" | "light" | "reference";

function availabilityChip(a: Availability | string): TemplateResult {
  const info = AVAILABILITY[a as Availability] ?? { tone: "neutral", label: a, help: "" };
  return html`<span class="has-tip" title=${info.help}>${chip(info.tone as Tone, info.label)}</span>`;
}

export class SlRoomEditor extends StoreElement {
  static properties = {
    areaId: { type: String },
    tab: { type: String },
    ws: { state: true },
    draft: { state: true },
    removeRoom: { state: true },
    selected: { state: true },
    leftOpen: { state: true },
    inspectorOpen: { state: true },
    loadError: { state: true },
    review: { state: true },
    reviewOpen: { state: true },
    reviewError: { state: true },
    confirmed: { state: true },
    busy: { state: true },
    notice: { state: true },
    changedElsewhere: { state: true },
    pick: { state: true },
    rooms: { state: true },
    deviceFilter: { state: true },
    deviceQuery: { state: true },
    roomQuery: { state: true },
  };
  declare areaId: string;
  declare tab: string;
  declare ws: Workspace | null;
  declare draft: RoomConfig | null;
  declare removeRoom: boolean;
  declare selected: string | null;
  declare leftOpen: boolean;
  declare inspectorOpen: boolean;
  declare loadError: string | null;
  declare review: Review | null;
  declare reviewOpen: boolean;
  declare reviewError: { message: string; code: string } | null;
  declare confirmed: Set<string>;
  declare busy: boolean;
  declare notice: string | null;
  declare changedElsewhere: boolean;
  declare pick: PickMode | null;
  declare rooms: RoomSummary[] | null;
  declare deviceFilter: "all" | "assigned" | "unassigned" | "attention";
  declare deviceQuery: string;
  declare roomQuery: string;
  private base: RoomConfig | null = null;
  private extra = new Map<string, EntityInfo>();
  private loadedArea: string | null = null;
  private seenRooms = -1;
  private seenRegistries = -1;
  private loading = false;
  /** Opened from the floor plan (shows the way back). */
  private fromFloorplan = false;
  private preparedFor: string | null = null;

  constructor() {
    super();
    this.areaId = "";
    this.tab = "overview";
    this.ws = null;
    this.draft = null;
    this.removeRoom = false;
    this.selected = null;
    this.leftOpen = window.innerWidth >= 1280;
    this.inspectorOpen = false;
    this.loadError = null;
    this.review = null;
    this.reviewOpen = false;
    this.reviewError = null;
    this.confirmed = new Set();
    this.busy = false;
    this.notice = null;
    this.changedElsewhere = false;
    this.pick = null;
    this.rooms = null;
    this.deviceFilter = "all";
    this.deviceQuery = "";
    this.roomQuery = "";
  }

  // ------------------------------------------------------------- lifecycle
  private onBeforeUnload = (event: BeforeUnloadEvent): void => {
    if (this.dirty()) {
      event.preventDefault();
      event.returnValue = "";
    }
  };

  connectedCallback(): void {
    super.connectedCallback();
    window.addEventListener("beforeunload", this.onBeforeUnload);
    setLeaveGuard(() => (this.dirty() ? `${this.ws?.area.name ?? "This room"} has unsaved changes. Leave without saving them?` : null));
    void this.loadRooms();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    window.removeEventListener("beforeunload", this.onBeforeUnload);
    setLeaveGuard(null);
    live.watch([]);
  }

  protected willUpdate(changed: Map<string, unknown>): void {
    if (changed.has("areaId") && this.areaId && this.areaId !== this.loadedArea) {
      this.reset();
      void this.load(false);
    }
    if (this.ws && !this.loading && !this.busy) {
      if (store.roomsVersion !== this.seenRooms) {
        this.seenRooms = store.roomsVersion;
        void this.onRoomsChanged();
      } else if (store.registriesVersion !== this.seenRegistries) {
        this.seenRegistries = store.registriesVersion;
        void this.load(true);
      }
    }
  }

  private reset(): void {
    this.ws = null;
    this.draft = null;
    this.base = null;
    this.removeRoom = false;
    this.selected = null;
    this.inspectorOpen = false;
    this.review = null;
    this.reviewOpen = false;
    this.notice = null;
    this.changedElsewhere = false;
    this.extra.clear();
  }

  private async loadRooms(): Promise<void> {
    try {
      this.rooms = (await getRooms<RoomsOverview>("api/rooms")).rooms;
    } catch {
      this.rooms = null;
    }
  }

  /** Load the workspace. ``keepDraft``: keep unsaved edits (they are then compared with the new configuration). */
  async load(keepDraft: boolean, refresh = false): Promise<void> {
    const area = this.areaId;
    this.loading = true;
    this.seenRooms = store.roomsVersion;
    this.seenRegistries = store.registriesVersion;
    try {
      const ws = await getRooms<Workspace>(`api/rooms/${encodeURIComponent(area)}${refresh ? "?refresh=1" : ""}`);
      if (area !== this.areaId) return;
      this.applyWorkspace(ws, keepDraft);
      this.loadError = null;
      if (this.preparedFor !== area) {
        this.preparedFor = area;
        void this.applyPrepared();
      }
    } catch (err) {
      this.loadError = err instanceof ApiError ? err.message : String(err);
    } finally {
      this.loading = false;
    }
  }

  private applyWorkspace(ws: Workspace, keepDraft: boolean): void {
    const wasDirty = this.dirty();
    const oldBase = this.base;
    this.ws = ws;
    this.loadedArea = ws.area.id;
    this.base = editable(ws.room.config);
    if (!keepDraft || !wasDirty || !this.draft || !oldBase) {
      this.draft = editable(ws.room.config);
      this.removeRoom = false;
    } else if (!this.removeRoom) {
      // Keep only the user's own edits, applied to the configuration as it is now.
      this.draft = rebase(oldBase, this.draft, this.base);
    }
    for (const id of Object.keys(ws.entities)) this.extra.delete(id);
    live.watch(Object.keys(ws.entities).concat([...this.extra.keys()]));
  }

  /**
   * A change prepared elsewhere (the floor plan: ?adjacent=<area>, ?assign=<entity>&as=sensor|light,
   * ?select=<entity>) goes into the draft only. Nothing is saved until it is reviewed here.
   */
  private async applyPrepared(): Promise<void> {
    const q = hashQuery();
    if (!q.toString()) return;
    // Drop the query at once, so a reload does not prepare it again.
    history.replaceState(null, "", window.location.hash.split("?")[0]);
    this.fromFloorplan = q.get("from") === "floorplan";
    const wanted = q.get("assign");
    if (wanted && /^[a-z0-9_]+\.[a-z0-9_]+$/.test(wanted) && !this.ws?.entities[wanted] && !this.extra.has(wanted)) {
      try {
        const details = await getRooms<{ entity: EntityInfo }>(`api/floorplan/entity/${encodeURIComponent(wanted)}`);
        this.extra.set(wanted, details.entity);
        live.watch(Object.keys(this.ws?.entities ?? {}).concat([...this.extra.keys()]));
      } catch {
        /* assigned with the defaults; the review still checks it */
      }
    }
    const ws = this.ws;
    const done: string[] = [];
    const adjacent = q.get("adjacent");
    if (ws && this.draft && adjacent && adjacent !== ws.area.id && ws.areas.some((a) => a.id === adjacent)) {
      if (!this.draft.adjacent_rooms.includes(adjacent)) {
        this.update_((d) => (d.adjacent_rooms = [...d.adjacent_rooms, adjacent]));
        done.push(`${this.areaName(adjacent)} added as an adjacent room`);
      } else done.push(`${this.areaName(adjacent)} is already an adjacent room`);
    }
    const assign = q.get("assign");
    if (ws && assign && /^[a-z0-9_]+\.[a-z0-9_]+$/.test(assign) && !this.assignment(assign)) {
      if (q.get("as") === "light" && assign.startsWith("light.")) this.assignLight(assign);
      else if (q.get("as") === "sensor") this.assignSensor(assign);
      if (this.assignment(assign)) done.push(`${this.name(assign)} assigned as ${q.get("as") === "light" ? "a light" : "a sensor"}`);
    }
    const select = q.get("select") ?? assign;
    if (select && /^[a-z0-9_]+\.[a-z0-9_]+$/.test(select)) this.select(select);
    if (done.length) this.notice = `Prepared from the floor plan: ${done.join("; ")}. Review and save to apply it, or Discard.`;
    this.requestUpdate();
  }

  private async onRoomsChanged(): Promise<void> {
    void this.loadRooms();
    if (!this.ws) return;
    const before = this.ws.room.revision;
    if (this.dirty()) {
      // Keep the edits; check whether this room was what changed.
      try {
        const fresh = await getRooms<Workspace>(`api/rooms/${encodeURIComponent(this.areaId)}`);
        if (fresh.room.revision !== before) this.changedElsewhere = true;
        else this.applyWorkspace(fresh, true);
      } catch {
        /* keep the current view */
      }
    } else {
      await this.load(false);
    }
  }

  // ----------------------------------------------------------------- draft
  private changes(): Change[] {
    if (!this.base || !this.draft || !this.ws) return [];
    if (this.removeRoom) return this.ws.room.configured ? [{ type: "room_removed" }] : [];
    return diff(this.base, this.draft, this.ws.room.configured);
  }

  dirty(): boolean {
    return this.changes().length > 0;
  }

  private update_(mutate: (d: RoomConfig) => void): void {
    if (!this.draft) return;
    const next: RoomConfig = { ...this.draft, sensors: this.draft.sensors.map((s) => ({ ...s })), lights: [...this.draft.lights], adjacent_rooms: [...this.draft.adjacent_rooms] };
    mutate(next);
    this.draft = next;
    this.removeRoom = false;
    this.notice = null;
  }

  private entity(id: string): EntityInfo {
    const known = this.ws?.entities[id] ?? this.extra.get(id);
    const liveState = store.liveStates.get(id);
    const base: EntityInfo = known ?? {
      entity_id: id, name: id, domain: id.split(".")[0], category: "other", category_label: "Other", category_confidence: "low",
      category_reason: "Not known to Home Assistant", suggested_kind: "other", device_class: null, unit: null, icon: null, state: null,
      last_changed: null, last_updated: null, availability: "missing", capabilities: [], platform: null, registered: false, hidden: false,
      entity_category: null, device_id: null, area_id: null, area: null, sensor_rooms: [], light_rooms: [], own: false,
    };
    if (!liveState) return base;
    return { ...base, state: liveState.state, last_changed: liveState.last_changed, last_updated: liveState.last_updated, availability: liveState.availability as Availability };
  }

  private name(id: string | undefined | null): string {
    if (!id) return "—";
    return this.entity(id).name;
  }

  private areaName(id: string | undefined): string {
    if (!id) return "—";
    return this.ws?.areas.find((a) => a.id === id)?.name ?? this.ws?.other_rooms[id]?.name ?? id;
  }

  private kindLabel = (k: unknown): string => this.ws?.schema?.kinds.find((x) => x.value === k)?.label ?? String(k ?? "—");
  private roleLabel = (r: unknown): string => (this.ws?.schema?.roles.find((x) => x.value === r)?.label ?? String(r ?? "—")).split(":")[0];

  private assignment(id: string): Assignment {
    const d = this.draft;
    if (!d) return null;
    const sensor = d.sensors.find((s) => s.entity_id === id);
    if (sensor) return { kind: "sensor", sensor };
    if (d.lights.includes(id)) return { kind: "light" };
    if (d.reference_occupancy_entity === id) return { kind: "reference" };
    return null;
  }

  private assignSensor(id: string, kind?: string): void {
    const e = this.entity(id);
    const schema = this.ws?.schema ?? null;
    const kinds = kindsFor(schema, id, e.suggested_kind);
    const chosen = kind ?? (kinds.find((k) => k.value === e.suggested_kind) ?? kinds[0])?.value ?? "other";
    this.update_((d) => {
      d.lights = d.lights.filter((l) => l !== id);
      if (!d.sensors.some((s) => s.entity_id === id))
        d.sensors.push({ entity_id: id, kind: chosen, role: kindDefaultRole(schema, chosen), inverted: false, enabled: true });
    });
    this.select(id);
  }

  private assignLight(id: string): void {
    this.update_((d) => {
      d.sensors = d.sensors.filter((s) => s.entity_id !== id);
      if (!d.lights.includes(id)) d.lights.push(id);
    });
    this.select(id);
  }

  private unassign(id: string): void {
    this.update_((d) => {
      d.sensors = d.sensors.filter((s) => s.entity_id !== id);
      d.lights = d.lights.filter((l) => l !== id);
      if (d.reference_occupancy_entity === id) d.reference_occupancy_entity = null;
    });
  }

  private setSensor(id: string, patch: Partial<SensorConfig>): void {
    const schema = this.ws?.schema ?? null;
    this.update_((d) => {
      const s = d.sensors.find((x) => x.entity_id === id);
      if (!s) return;
      if (patch.kind && patch.kind !== s.kind) {
        // Like the integration: a new type starts with that type's default role.
        const old = this.base?.sensors.find((x) => x.entity_id === id);
        s.role = old && old.kind === patch.kind ? old.role : kindDefaultRole(schema, patch.kind);
      }
      Object.assign(s, patch);
    });
  }

  private discard(): void {
    if (!this.base) return;
    this.draft = editable({ ...this.base });
    this.removeRoom = false;
    this.changedElsewhere = false;
  }

  private select(id: string | null): void {
    this.selected = id;
    this.inspectorOpen = id !== null;
  }

  private pickWith(mode: PickMode): void {
    this.pick = mode;
  }

  private onPicked(e: CustomEvent<{ candidate: Candidate; mode: PickMode }>): void {
    const { candidate: c, mode } = e.detail;
    if (!this.ws?.entities[c.entity_id]) {
      this.extra.set(c.entity_id, {
        ...c, category_confidence: "medium", category_reason: "From the device search", device_class: null, unit: null, icon: null,
        last_changed: null, last_updated: null, capabilities: [], platform: null, registered: true, hidden: false, entity_category: null, own: false,
      });
      live.watch(Object.keys(this.ws?.entities ?? {}).concat([...this.extra.keys()]));
    }
    if (mode === "light") this.assignLight(c.entity_id);
    else if (mode === "reference") {
      this.update_((d) => (d.reference_occupancy_entity = c.entity_id));
      this.select(c.entity_id);
    } else this.assignSensor(c.entity_id);
    this.pick = null;
  }

  // ---------------------------------------------------------- review/save
  private async openReview(): Promise<void> {
    if (!this.ws || !this.draft) return;
    this.busy = true;
    this.reviewError = null;
    this.reviewOpen = true;
    try {
      this.review = await postRooms<Review>(`api/rooms/${encodeURIComponent(this.areaId)}/review`, {
        room: this.removeRoom ? null : payload(this.draft),
        revision: this.ws.room.revision,
      });
      this.confirmed = new Set();
    } catch (err) {
      this.review = null;
      this.reviewError = { message: err instanceof Error ? err.message : String(err), code: err instanceof ApiError ? err.code : "error" };
    } finally {
      this.busy = false;
    }
    await this.updateComplete;
    const dialog = this.querySelector<HTMLDialogElement>("dialog.review-dialog");
    if (dialog && !dialog.open) dialog.showModal();
  }

  private closeReview(): void {
    this.querySelector<HTMLDialogElement>("dialog.review-dialog")?.close();
    this.reviewOpen = false;
  }

  /** Reviewed refresh: load the current configuration, keep the edits, review again against it. */
  private async refreshAndReview(): Promise<void> {
    await this.load(true, true);
    this.changedElsewhere = false;
    await this.openReview();
  }

  private async save(): Promise<void> {
    if (!this.ws || !this.draft || !this.review) return;
    this.busy = true;
    this.reviewError = null;
    try {
      const result = await postRooms<{ result: { applied: string; backup: string | null }; workspace: Workspace }>(
        `api/rooms/${encodeURIComponent(this.areaId)}/save`,
        { room: this.removeRoom ? null : payload(this.draft), revision: this.ws.room.revision, confirmed: [...this.confirmed] },
      );
      this.applyWorkspace(result.workspace, false);
      // Notices about this save itself are not "changed elsewhere".
      this.seenRooms = store.roomsVersion;
      this.closeReview();
      this.review = null;
      this.changedElsewhere = false;
      this.notice =
        result.result.applied === "live"
          ? "Saved. The integration applied the change at once."
          : result.result.applied === "none"
            ? "Nothing changed."
            : "Saved. The integration reloads to apply it; no light or cover is switched by the reload.";
      void this.loadRooms();
    } catch (err) {
      const code = err instanceof ApiError ? err.code : "error";
      if (code === "stale_revision" || code === "confirmation_required") {
        await this.refreshAndReview();
        this.reviewError = {
          code,
          message: code === "stale_revision"
            ? "The room was changed elsewhere while you were reviewing. The review below compares your edits with the current configuration."
            : "The consequences changed since the review. Check them again below.",
        };
      } else {
        this.reviewError = { message: err instanceof Error ? err.message : String(err), code };
      }
    } finally {
      this.busy = false;
    }
  }

  // ----------------------------------------------------------- rendering
  private renderHead(ws: Workspace): unknown {
    const status = STATUS[ws.summary.status];
    return html`<header class="editor-head">
      <a class="btn back-btn" href="#/rooms" data-testid="back-to-rooms">${icon("arrow-left", 20)}<span>Rooms</span></a>
      ${this.fromFloorplan ? html`<a class="btn" href="#/floorplan/configure" data-testid="back-to-floorplan">${icon("home-map-marker", 20)}<span>Floor plan</span></a>` : nothing}
      <span class="room-icon">${icon(areaIcon(ws.area.icon), 24)}</span>
      <div class="editor-title">
        <h2 tabindex="-1" data-testid="room-title">${ws.area.name}</h2>
        <span class="chips">${chip(status.tone, status.label)}
          ${ws.summary.enabled ? occupancyChip(ws.room.occupancy) : nothing}
          ${ws.room.lighting ? chip(ws.room.lighting.automatic ? "info" : "neutral", ws.room.lighting.automatic ? "Lighting: Automatic" : "Lighting: Observing") : nothing}</span>
      </div>
      <div class="editor-actions">
        <button type="button" class="icon-btn panel-toggle" aria-label=${this.leftOpen ? "Hide the room list" : "Show the room list"} aria-pressed=${this.leftOpen ? "true" : "false"} @click=${() => (this.leftOpen = !this.leftOpen)}>${icon("dock-left")}</button>
        <button type="button" class="icon-btn panel-toggle" aria-label=${this.inspectorOpen ? "Hide the inspector" : "Show the inspector"} aria-pressed=${this.inspectorOpen ? "true" : "false"} @click=${() => (this.inspectorOpen = !this.inspectorOpen)}>${icon("dock-right")}</button>
        <a class="icon-btn" href="#/floorplan/configure?area=${encodeURIComponent(ws.area.id)}" aria-label="Show on the floor plan" title="Show on the floor plan" data-testid="show-on-floorplan">${icon("home-map-marker")}</a>
        <button type="button" class="icon-btn" aria-label="Reload from Home Assistant" @click=${() => void this.load(true, true)}>${icon("refresh")}</button>
      </div>
    </header>`;
  }

  private renderUnsaved(): unknown {
    const changes = this.changes();
    const writable = this.ws?.status.writable;
    const offline = store.link === "offline";
    return html`<div class="unsaved-bar ${changes.length ? "visible" : ""}" role="region" aria-label="Unsaved changes" data-testid="unsaved-bar" ?hidden=${!changes.length}>
      <span class="unsaved-text">${icon("pencil", 20)}<span><strong>${plural(changes.length, "unsaved change", "unsaved changes")}</strong>
        <span class="muted"> · saved only after you review them</span></span></span>
      <span class="row-actions">
        <button type="button" class="btn" data-testid="discard" @click=${() => this.discard()}>${icon("undo", 20)}Discard</button>
        <button type="button" class="btn btn-primary" data-testid="review" ?disabled=${!writable || offline || this.busy} title=${!writable ? "The integration is not running or cannot be written" : ""} @click=${() => void this.openReview()}>
          ${icon("eye", 20)}Review changes
        </button>
      </span>
    </div>`;
  }

  private renderLeft(ws: Workspace): unknown {
    const words = normalize(this.roomQuery).split(/\s+/).filter(Boolean);
    const rooms = (this.rooms ?? []).filter((r) => !words.length || words.every((w) => normalize(r.name).includes(w)));
    const available = Object.values(ws.entities)
      .map((e) => this.entity(e.entity_id))
      .filter((e) => !this.assignment(e.entity_id) && e.area_id === ws.area.id && (SENSOR_CATEGORIES.has(e.category) || e.category === "light" || e.category === "light_group"));
    return html`<aside class="ws-left" aria-label="Rooms and available devices">
      <section class="panel-section">
        <h3 class="panel-title">${icon("floor-plan", 18)}Rooms</h3>
        <input class="panel-search" type="search" placeholder="Find a room" aria-label="Find a room" .value=${this.roomQuery} @input=${(e: Event) => (this.roomQuery = (e.target as HTMLInputElement).value)} />
        <nav class="room-nav" aria-label="Rooms">
          ${rooms.map(
            (r) => html`<a href="#/rooms/${encodeURIComponent(r.area_id)}/${this.tab}" class=${r.area_id === ws.area.id ? "active" : ""} aria-current=${r.area_id === ws.area.id ? "page" : nothing}>
              <span class="dot status-dot-${r.status}" aria-hidden="true"></span><span class="label">${r.name}</span>
              ${r.occupancy?.occupied ? html`<span class="occupied-dot" title="Occupied">${icon("account", 16)}</span>` : nothing}
            </a>`,
          )}
        </nav>
      </section>
      <section class="panel-section">
        <h3 class="panel-title">${icon("plus", 18)}Available in ${ws.area.name}</h3>
        ${available.length
          ? html`<ul class="device-palette" data-testid="available-devices">
              ${available.map((e) => html`<li>
                <button type="button" class="palette-item ${this.selected === e.entity_id ? "selected" : ""}" @click=${() => this.select(e.entity_id)}>
                  ${icon(ws.categories[e.category]?.icon ?? "puzzle", 20)}
                  <span class="palette-text"><span class="palette-name">${e.name}</span><span class="muted small">${e.category_label}</span></span>
                </button>
                <button type="button" class="icon-btn add-btn" aria-label="Assign ${e.name}" data-assign=${e.entity_id}
                  @click=${() => (e.domain === "light" ? this.assignLight(e.entity_id) : this.assignSensor(e.entity_id))}>${icon("plus", 20)}</button>
              </li>`)}
            </ul>`
          : html`<p class="muted small">Every sensor and light in this area is assigned. Devices from other areas can be added with
              “Add sensor” or “Add light”.</p>`}
        <div class="palette-actions">
          <button type="button" class="btn" @click=${() => this.pickWith("sensor")}>${icon("motion-sensor", 18)}Add sensor</button>
          <button type="button" class="btn" @click=${() => this.pickWith("light")}>${icon("lightbulb", 18)}Add light</button>
        </div>
      </section>
    </aside>`;
  }

  private renderTabs(): unknown {
    return html`<nav class="tabs" role="tablist" aria-label="Room sections">
      ${TABS.map((t) => html`<a role="tab" href="#/rooms/${encodeURIComponent(this.areaId)}/${t.key}" aria-selected=${this.tab === t.key ? "true" : "false"} class=${this.tab === t.key ? "active" : ""} data-tab=${t.key}>
        ${icon(t.icon, 18)}<span>${t.title}</span>${t.key === "diagnostics" && this.attentionCount() ? html`<span class="badge">${this.attentionCount()}</span>` : nothing}
      </a>`)}
    </nav>`;
  }

  private attentionCount(): number {
    return (this.ws?.summary.warnings ?? []).filter((w) => w.level !== "info").length;
  }

  private laterModule(module: string, where: string): TemplateResult {
    return html`<p class="notice notice-info later-note">${icon("info", 20)}<span><strong>${module} stays in the integration for now.</strong>
      This page shows it read-only. Configure it in ${where}; it moves to the App in a later phase, after your approval.</span></p>`;
  }

  // --- Overview
  private renderOverview(ws: Workspace): unknown {
    const d = this.draft!;
    const s = ws.summary;
    const occ = ws.room.occupancy;
    const lighting = ws.room.lighting;
    const warnings = s.warnings;
    return html`<div class="tab-grid">
      <section class="card" data-testid="room-status-card">
        <header class="card-head">${icon("floor-plan")}<h3>Room configuration</h3>${chip(STATUS[s.status].tone, STATUS[s.status].label)}</header>
        <label class="switch-row" data-testid="room-enabled">
          <input type="checkbox" role="switch" .checked=${d.enabled && !this.removeRoom} @change=${(e: Event) => this.update_((x) => (x.enabled = (e.target as HTMLInputElement).checked))} />
          <span><strong>Observe occupancy in this room</strong><span class="muted block small">The integration computes occupancy from the assigned sensors and creates its room entities. Turning this off removes them.</span></span>
        </label>
        ${kv([
          ["Sensors", plural(d.sensors.length, "sensor", "sensors")],
          ["Lights", plural(d.lights.length, "light", "lights")],
          ["Reference occupancy", d.reference_occupancy_entity ? this.name(d.reference_occupancy_entity) : "None"],
          ["Adjacent rooms", d.adjacent_rooms.length ? d.adjacent_rooms.map((a) => this.areaName(a)).join(", ") : "None"],
        ])}
        ${ws.room.configured
          ? html`<button type="button" class="btn btn-danger" data-testid="remove-room" @click=${() => ((this.removeRoom = true), (this.notice = null))}>${icon("delete", 20)}Remove room configuration…</button>`
          : html`<p class="muted small">This area has no room configuration yet. Assign sensors and lights, turn on observing, then review and save.</p>`}
        ${this.removeRoom ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>The room configuration will be removed when you review and save. Use Discard to keep it.</span></p>` : nothing}
      </section>
      <section class="card" data-testid="room-occupancy-card">
        <header class="card-head">${icon("account")}<h3>Occupancy now</h3>${s.enabled ? occupancyChip(occ) : nothing}</header>
        ${occ
          ? kv([
              ["Decision", occ.reason],
              ["Confidence", occ.confidence],
              ["Last activity", occ.last_activity ? `${formatTime(occ.last_activity)}${occ.last_activity_source ? ` · ${this.name(occ.last_activity_source)}` : ""}` : null],
              ["Vacant at", occ.vacancy_at ? formatTime(occ.vacancy_at) : null],
            ])
          : html`<p class="muted">${s.enabled ? "The integration has not reported a decision for this room (it may be starting or not running)." : "Occupancy is not observed in this room."}</p>`}
        <p class="muted small">From the Smart Occupancy integration's own occupancy engine; nothing is estimated here.</p>
      </section>
      <section class="card">
        <header class="card-head">${icon("home")}<h3>Devices</h3></header>
        <div class="stat-tiles">
          <div class="stat"><span class="stat-value tone-ok">${s.devices.online}</span><span class="stat-label">online</span></div>
          <div class="stat"><span class="stat-value ${s.devices.offline ? "tone-error" : ""}">${s.devices.offline}</span><span class="stat-label">offline</span></div>
          <div class="stat"><span class="stat-value ${s.devices.unavailable_entities ? "tone-warning" : ""}">${s.devices.unavailable_entities}</span><span class="stat-label">entities unavailable</span></div>
          <div class="stat"><span class="stat-value">${s.unassigned_candidates}</span><span class="stat-label">found, not assigned</span></div>
        </div>
        <a class="text-link" href="#/rooms/${encodeURIComponent(this.areaId)}/devices">Manage devices ${icon("chevron-right", 18)}</a>
      </section>
      <section class="card" data-testid="room-ownership">
        <header class="card-head">${icon("shield-check")}<h3>Who runs what</h3></header>
        <ul class="owner-list">
          <li>${icon("robot", 20)}<span><strong>Automation:</strong> Smart Occupancy integration${s.automation.running ? "" : " (not running)"} — occupancy, lighting, switches and curtains run there, unchanged.</span></li>
          <li>${icon("lightbulb-group", 20)}<span><strong>Lighting:</strong> ${lighting ? (lighting.automatic ? "Automatic (the integration controls the lights)" : "Observing (no light commands)") : "—"}</span></li>
          <li>${icon("light-switch", 20)}<span><strong>Switches:</strong> ${s.automation.switches_active ? `${s.automation.switches_active} Active` : "none Active"}</span></li>
          <li>${icon("curtains", 20)}<span><strong>Curtains:</strong> ${s.automation.curtains_automatic ? `${s.automation.curtains_automatic} window(s) Automatic` : "none Automatic"}</span></li>
          <li>${icon("pencil", 20)}<span><strong>This App:</strong> edits the room's configuration in the integration. It sends no device commands.</span></li>
        </ul>
      </section>
      <section class="card wide" data-testid="room-warnings">
        <header class="card-head">${icon("warning")}<h3>Needs attention</h3>${warnings.length ? chip(warnings.some((w) => w.level === "error") ? "error" : warnings.some((w) => w.level === "warning") ? "warning" : "info", String(warnings.length)) : chip("ok", "Nothing")}</header>
        ${warnings.length
          ? html`<ul class="warning-list">${warnings.map((w) => html`<li class="level-${w.level}">
              ${icon(w.level === "error" ? "error" : w.level === "warning" ? "warning" : "info", 20)}
              <span><strong>${w.title}</strong>${w.source === "integration" ? html` <span class="muted small">(integration)</span>` : nothing}<span class="block">${w.detail}</span>${w.action ? html`<span class="muted small block">${w.action}</span>` : nothing}</span>
              ${w.entity_id ? html`<button type="button" class="btn" @click=${() => this.select(w.entity_id!)}>Inspect</button>` : nothing}
            </li>`)}</ul>`
          : html`<p class="muted">No warnings for this room.</p>`}
      </section>
    </div>`;
  }

  // --- Devices
  private deviceRows(ws: Workspace): EntityInfo[] {
    const ids = new Set(Object.keys(ws.entities).concat([...this.extra.keys()]));
    const d = this.draft!;
    for (const s of d.sensors) ids.add(s.entity_id);
    for (const l of d.lights) ids.add(l);
    const words = normalize(this.deviceQuery).split(/\s+/).filter(Boolean);
    return [...ids]
      .map((id) => this.entity(id))
      .filter((e) => {
        const a = this.assignment(e.entity_id);
        if (this.deviceFilter === "assigned" && !a) return false;
        if (this.deviceFilter === "unassigned" && a) return false;
        if (this.deviceFilter === "attention" && e.availability === "ok") return false;
        return !words.length || words.every((w) => normalize(`${e.name} ${e.entity_id} ${e.category_label}`).includes(w));
      })
      .sort((a, b) => a.name.localeCompare(b.name));
  }

  private assignmentChip(id: string): TemplateResult {
    const a = this.assignment(id);
    if (!a) return chip("neutral", "Not assigned");
    if (a.kind === "light") return chip("info", "Light");
    if (a.kind === "reference") return chip("info", "Reference");
    return chip(a.sensor.enabled === false ? "pending" : "info", `${this.kindLabel(a.sensor.kind).split(" (")[0]} · ${this.roleLabel(a.sensor.role)}${a.sensor.enabled === false ? " · excluded" : ""}`);
  }

  private quickAction(e: EntityInfo): TemplateResult {
    const a = this.assignment(e.entity_id);
    if (a) return html`<button type="button" class="btn" data-unassign=${e.entity_id} @click=${(ev: Event) => (ev.stopPropagation(), this.unassign(e.entity_id))}>${icon("minus", 18)}Remove</button>`;
    if (e.domain === "light") return html`<button type="button" class="btn" data-assign=${e.entity_id} @click=${(ev: Event) => (ev.stopPropagation(), this.assignLight(e.entity_id))}>${icon("plus", 18)}Assign</button>`;
    if (SENSOR_CATEGORIES.has(e.category) || kindsFor(this.ws?.schema ?? null, e.entity_id, e.suggested_kind).length)
      return html`<button type="button" class="btn" data-assign=${e.entity_id} @click=${(ev: Event) => (ev.stopPropagation(), this.assignSensor(e.entity_id))}>${icon("plus", 18)}Assign</button>`;
    return html`<span class="muted small">Read-only here</span>`;
  }

  private renderDevices(ws: Workspace): unknown {
    const rows = this.deviceRows(ws);
    return html`<div class="devices-tab">
      <div class="table-toolbar">
        <label class="search-field">${icon("search", 20)}<input type="search" placeholder="Search devices" aria-label="Search devices" .value=${this.deviceQuery} @input=${(e: Event) => (this.deviceQuery = (e.target as HTMLInputElement).value)} /></label>
        <fieldset class="segmented" aria-label="Show">
          <legend class="visually-hidden">Show</legend>
          ${(["all", "assigned", "unassigned", "attention"] as const).map((f) => html`<label class=${this.deviceFilter === f ? "selected" : ""}><input type="radio" name="device-filter" .checked=${this.deviceFilter === f} @change=${() => (this.deviceFilter = f)} />${{ all: "All", assigned: "Assigned", unassigned: "Not assigned", attention: "Needs attention" }[f]}</label>`)}
        </fieldset>
        <span class="row-actions">
          <button type="button" class="btn" data-testid="add-sensor" @click=${() => this.pickWith("sensor")}>${icon("plus", 18)}Add sensor</button>
          <button type="button" class="btn" data-testid="add-light" @click=${() => this.pickWith("light")}>${icon("plus", 18)}Add light</button>
        </span>
      </div>
      ${GROUPS.map((g) => {
        const items = rows.filter((e) => (ws.categories[e.category]?.group ?? "other") === g.key);
        if (!items.length) return nothing;
        return html`<section class="device-group">
          <h3 class="section-title">${g.title} <span class="muted">${items.length}</span></h3>
          <div class="table-wrap"><table class="device-table">
            <thead><tr><th scope="col">Device</th><th scope="col">Type</th><th scope="col">Status</th><th scope="col">State</th><th scope="col">Assignment</th><th scope="col"><span class="visually-hidden">Action</span></th></tr></thead>
            <tbody>${items.map((e) => html`<tr class=${this.selected === e.entity_id ? "selected" : ""} data-entity=${e.entity_id} @click=${() => this.select(e.entity_id)} tabindex="0" @keydown=${(ev: KeyboardEvent) => ev.key === "Enter" && this.select(e.entity_id)}>
              <td><span class="cell-main">${icon(ws.categories[e.category]?.icon ?? "puzzle", 20)}<span><span class="cell-name">${e.name}</span><span class="muted small block mono">${e.entity_id}</span>
                ${e.area_id && e.area_id !== ws.area.id ? html`<span class="small tone-warning block">${icon("swap", 14)} From ${e.area ?? e.area_id}</span>` : nothing}</span></span></td>
              <td><span class="has-tip" title=${e.category_reason}>${e.category_label}</span>${e.category_confidence === "low" ? html`<span class="muted small block">uncertain</span>` : nothing}</td>
              <td>${availabilityChip(e.availability)}</td>
              <td><span class="nowrap">${e.state ?? "—"}${e.unit ? ` ${e.unit}` : ""}</span><span class="muted small block">${formatTime(e.last_changed)}</span></td>
              <td>${this.assignmentChip(e.entity_id)}</td>
              <td class="cell-action">${this.quickAction(e)}</td>
            </tr>`)}</tbody>
          </table></div>
        </section>`;
      })}
      ${!rows.length ? html`<div class="empty-state">${icon("home", 36)}<p class="muted">No devices match. Devices are found from this area in Home Assistant; use “Add sensor” or “Add light” for devices in other areas.</p></div>` : nothing}
    </div>`;
  }

  // --- Presence
  private renderPresence(ws: Workspace): unknown {
    const d = this.draft!;
    const schema = ws.schema;
    const signals = new Map((ws.room.occupancy?.signals ?? []).map((s) => [s.entity_id, s]));
    const zones = ws.room.lighting?.zones ?? [];
    const timing = ws.timing ?? {};
    const roomTiming = (ws.room.config.timing ?? {}) as Record<string, number>;
    return html`<div class="tab-stack">
      <section class="card">
        <header class="card-head">${icon("motion-sensor")}<h3>Occupancy sensors</h3>
          <button type="button" class="btn" data-testid="presence-add-sensor" @click=${() => this.pickWith("sensor")}>${icon("plus", 18)}Add sensor</button></header>
        ${d.sensors.length
          ? html`<div class="table-wrap"><table class="sensor-table" data-testid="sensor-table">
              <thead><tr><th scope="col">Sensor</th><th scope="col">Type</th><th scope="col">Role</th><th scope="col" class="center">Inverted</th><th scope="col" class="center">Included</th><th scope="col">Now</th><th scope="col"><span class="visually-hidden">Remove</span></th></tr></thead>
              <tbody>${d.sensors.map((s) => {
                const e = this.entity(s.entity_id);
                const sig = signals.get(s.entity_id);
                const inZones = zones.filter((z) => z.sensors.includes(s.entity_id)).map((z) => z.name);
                return html`<tr data-sensor=${s.entity_id} class=${this.selected === s.entity_id ? "selected" : ""}>
                  <td><button type="button" class="link-btn" @click=${() => this.select(s.entity_id)}>${e.name}</button><span class="muted small block mono">${s.entity_id}</span>
                    ${inZones.length ? html`<span class="small muted block">Zones: ${inZones.join(", ")}</span>` : nothing}</td>
                  <td><select aria-label="Type of ${e.name}" .value=${s.kind} @change=${(ev: Event) => this.setSensor(s.entity_id, { kind: (ev.target as HTMLSelectElement).value })}>
                    ${kindsFor(schema, s.entity_id, s.kind).map((k) => html`<option value=${k.value} ?selected=${k.value === s.kind}>${k.label}</option>`)}
                  </select></td>
                  <td><select aria-label="Role of ${e.name}" title=${ROLE_HELP[s.role] ?? ""} .value=${s.role} @change=${(ev: Event) => this.setSensor(s.entity_id, { role: (ev.target as HTMLSelectElement).value })}>
                    ${(schema?.roles ?? []).map((r) => html`<option value=${r.value} ?selected=${r.value === s.role}>${r.label.split(":")[0]}</option>`)}
                  </select></td>
                  <td class="center"><input type="checkbox" class="check" aria-label="${e.name} is inverted (active when off)" .checked=${Boolean(s.inverted)} @change=${(ev: Event) => this.setSensor(s.entity_id, { inverted: (ev.target as HTMLInputElement).checked })} /></td>
                  <td class="center"><input type="checkbox" class="check" aria-label="Include ${e.name} in occupancy" .checked=${s.enabled !== false} @change=${(ev: Event) => this.setSensor(s.entity_id, { enabled: (ev.target as HTMLInputElement).checked })} /></td>
                  <td>${availabilityChip(e.availability)}<span class="small block">${sig ? CONDITION_LABEL[sig.condition] ?? sig.condition : e.state ?? "—"}</span></td>
                  <td class="cell-action"><button type="button" class="icon-btn" aria-label="Remove ${e.name}" data-unassign=${s.entity_id} @click=${() => this.unassign(s.entity_id)}>${icon("delete", 20)}</button></td>
                </tr>`;
              })}</tbody>
            </table></div>
            <p class="muted small">Role: ${Object.entries(ROLE_HELP).map(([k, v]) => html`<span class="block"><strong>${this.roleLabel(k)}</strong> — ${v}</span>`)}</p>`
          : html`<div class="empty-state">${icon("motion-sensor", 36)}<p class="muted">No sensors yet. Add a PIR or radar sensor: without one the room's occupancy cannot be observed.</p></div>`}
      </section>
      <section class="card">
        <header class="card-head">${icon("link")}<h3>Reference and adjacent rooms</h3></header>
        <div class="field-row">
          <span class="field-label">Reference occupancy entity <span class="muted">(optional; your existing helper, for comparison only)</span></span>
          <div class="ref-row">
            <span>${d.reference_occupancy_entity ? html`${this.name(d.reference_occupancy_entity)} <span class="muted mono small">${d.reference_occupancy_entity}</span>` : html`<span class="muted">None</span>`}</span>
            <button type="button" class="btn" data-testid="pick-reference" @click=${() => this.pickWith("reference")}>${icon("pencil", 18)}Choose…</button>
            ${d.reference_occupancy_entity ? html`<button type="button" class="btn" @click=${() => this.update_((x) => (x.reference_occupancy_entity = null))}>${icon("close", 18)}Clear</button>` : nothing}
          </div>
        </div>
        <fieldset class="chip-choices">
          <legend class="field-label">Adjacent rooms <span class="muted">(movement into them confirms that someone left through a door)</span></legend>
          ${ws.areas.filter((a) => a.id !== ws.area.id).map((a) => html`<label class=${d.adjacent_rooms.includes(a.id) ? "selected" : ""}>
            <input type="checkbox" .checked=${d.adjacent_rooms.includes(a.id)} @change=${(e: Event) => this.update_((x) => {
              const on = (e.target as HTMLInputElement).checked;
              x.adjacent_rooms = on ? [...x.adjacent_rooms, a.id] : x.adjacent_rooms.filter((r) => r !== a.id);
            })} />${a.name}${ws.other_rooms[a.id]?.enabled ? nothing : html`<span class="muted small"> · not observed</span>`}</label>`)}
        </fieldset>
      </section>
      <section class="card">
        <header class="card-head">${icon("history")}<h3>Presence timing</h3>${chip("neutral", "Read-only")}</header>
        ${this.laterModule("Presence timing", "the Smart Room Lighting card (Presence tab) or Configure → Occupancy timing")}
        <div class="table-wrap"><table class="plain-table">
          <thead><tr><th scope="col">Setting</th><th scope="col">Global</th><th scope="col">This room</th></tr></thead>
          <tbody>${Object.entries(timing).map(([k, v]) => html`<tr><td>${k.replace(/_/g, " ")}</td><td>${v} s</td><td>${k in roomTiming ? html`<strong>${roomTiming[k]} s</strong>` : html`<span class="muted">global</span>`}</td></tr>`)}</tbody>
        </table></div>
      </section>
    </div>`;
  }

  // --- Lighting
  private renderLighting(ws: Workspace): unknown {
    const d = this.draft!;
    const lighting = ws.room.lighting;
    const byLabel = (lighting?.managed ?? []).filter((l) => !d.lights.includes(l));
    return html`<div class="tab-stack">
      <section class="card">
        <header class="card-head">${icon("lightbulb")}<h3>Assigned lights</h3>
          <button type="button" class="btn" data-testid="lighting-add-light" @click=${() => this.pickWith("light")}>${icon("plus", 18)}Add light</button></header>
        ${d.lights.length
          ? html`<ul class="entity-rows" data-testid="light-list">${d.lights.map((l) => {
              const e = this.entity(l);
              return html`<li data-light=${l} class=${this.selected === l ? "selected" : ""}>
                <button type="button" class="row-main" @click=${() => this.select(l)}>${icon(e.category === "light_group" ? "lightbulb-group" : "lightbulb", 22)}
                  <span><span class="cell-name">${e.name}</span><span class="muted small block mono">${l}</span><span class="small muted block">${e.capabilities.join(" · ")}</span></span></button>
                <span>${availabilityChip(e.availability)}<span class="small block">${e.state ?? "—"}</span></span>
                <button type="button" class="icon-btn" aria-label="Remove ${e.name}" data-unassign=${l} @click=${() => this.unassign(l)}>${icon("delete", 20)}</button>
              </li>`;
            })}</ul>`
          : html`<div class="empty-state">${icon("lightbulb", 36)}<p class="muted">No lights assigned. Lights with a light-type label in this area can also be managed automatically (see below).</p></div>`}
      </section>
      <section class="card">
        <header class="card-head">${icon("robot")}<h3>Automatic lighting</h3>${lighting ? chip(lighting.automatic ? "info" : "neutral", lighting.automatic ? "Automatic" : "Observing") : chip("neutral", "Unknown")}</header>
        ${this.laterModule("Lighting behaviour", "the Smart Room Lighting card")}
        ${lighting
          ? kv([
              ["Mode", lighting.automatic ? `Automatic since ${formatTime(lighting.activated_at)}` : "Observing — no light commands"],
              ["Also managed by label", byLabel.length ? byLabel.map((l) => this.name(l)).join(", ") : "None"],
              ["Relays (lighting outputs)", lighting.relays.length ? lighting.relays.map((r) => this.name(r)).join(", ") : "None"],
              ["Zones", lighting.zones.length ? lighting.zones.map((z) => `${z.name}${z.enabled ? "" : " (off)"}`).join(", ") : "None"],
              ["When vacant", lighting.vacancy_action?.replace(/_/g, " ") ?? null],
            ])
          : html`<p class="muted">The integration is not running, so its lighting state cannot be read.</p>`}
      </section>
    </div>`;
  }

  // --- Switches
  private renderSwitches(ws: Workspace): unknown {
    const lighting = ws.room.lighting;
    const configured = new Set((lighting?.switches ?? []).flatMap((s) => [s.event_entity, s.device_id]).filter(Boolean));
    const found = Object.values(ws.entities).filter((e) => e.category === "wall_switch" && !configured.has(e.entity_id) && !configured.has(e.device_id ?? ""));
    return html`<div class="tab-stack">
      ${this.laterModule("Wall switches and Priority Inputs", "the Smart Room Lighting card (Switches)")}
      <section class="card">
        <header class="card-head">${icon("light-switch")}<h3>Configured switches</h3></header>
        ${lighting?.switches.length
          ? html`<ul class="entity-rows">${lighting.switches.map((s) => html`<li><span class="row-main static">${icon("light-switch", 22)}<span><span class="cell-name">${s.name}</span>
              <span class="muted small block">${s.device_id ? ws.devices[s.device_id]?.name ?? "Device" : s.event_entity ?? ""}</span></span></span>
              ${chip(s.mode === "control" ? "info" : "neutral", s.mode === "control" ? "Active" : "Observing")}</li>`)}</ul>`
          : html`<p class="muted">No wall switch or remote is configured for this room.</p>`}
      </section>
      <section class="card">
        <header class="card-head">${icon("remote")}<h3>Found in this area, not configured</h3></header>
        ${found.length
          ? html`<ul class="entity-rows">${found.map((e) => html`<li><button type="button" class="row-main" @click=${() => this.select(e.entity_id)}>${icon("remote", 22)}<span><span class="cell-name">${e.name}</span><span class="muted small block mono">${e.entity_id}</span></span></button>${availabilityChip(e.availability)}</li>`)}</ul>`
          : html`<p class="muted">No other switch or remote found.</p>`}
      </section>
      ${lighting?.priority_inputs.length
        ? html`<section class="card"><header class="card-head">${icon("alert-octagon")}<h3>Priority Inputs</h3></header>
            <ul class="entity-rows">${lighting.priority_inputs.map((p) => html`<li><span class="row-main static">${icon("alert-octagon", 22)}<span><span class="cell-name">${p.name}</span><span class="muted small block mono">${p.entity_id ?? "—"}</span></span></span>${chip(p.enabled ? "info" : "neutral", p.enabled ? "Enabled" : "Off")}</li>`)}</ul></section>`
        : nothing}
    </div>`;
  }

  // --- Curtains
  private renderCurtains(ws: Workspace): unknown {
    const windows = ws.room.curtains;
    const inWindows = new Set(windows.flatMap((w) => w.covers));
    const found = Object.values(ws.entities).filter((e) => e.category === "cover" && !inWindows.has(e.entity_id));
    return html`<div class="tab-stack">
      ${this.laterModule("Curtains", "the Smart Curtains card")}
      <section class="card">
        <header class="card-head">${icon("curtains")}<h3>Windows</h3></header>
        ${windows.length
          ? html`<ul class="entity-rows">${windows.map((w) => html`<li><span class="row-main static">${icon("window", 22)}<span><span class="cell-name">${w.name}</span>
              <span class="small block">${w.covers.map((c) => html`<span class="cover-state">${this.name(c)}: ${this.entity(c).state ?? "—"}</span>`)}</span></span></span>
              ${chip(w.mode === "automatic" ? "info" : "neutral", w.mode.charAt(0).toUpperCase() + w.mode.slice(1))}</li>`)}</ul>`
          : html`<p class="muted">No window is configured for this area.</p>`}
      </section>
      <section class="card">
        <header class="card-head">${icon("blinds")}<h3>Covers found, not in a window</h3></header>
        ${found.length
          ? html`<ul class="entity-rows">${found.map((e) => html`<li><button type="button" class="row-main" @click=${() => this.select(e.entity_id)}>${icon("curtains", 22)}<span><span class="cell-name">${e.name}</span><span class="muted small block">${e.capabilities.join(" · ")}</span></span></button>${availabilityChip(e.availability)}</li>`)}</ul>`
          : html`<p class="muted">None.</p>`}
      </section>
    </div>`;
  }

  // --- Diagnostics
  private renderDiagnostics(ws: Workspace): unknown {
    const d = this.draft!;
    const assigned = new Set([...d.sensors.map((s) => s.entity_id), ...d.lights, ...(d.reference_occupancy_entity ? [d.reference_occupancy_entity] : [])]);
    const rows = [...new Set([...Object.keys(ws.entities), ...assigned])].map((id) => this.entity(id));
    const conflicts = ws.summary.warnings.filter((w) => ["light_conflict", "sensor_shared", "outside_area"].includes(w.code));
    const unassigned = rows.filter((e) => !assigned.has(e.entity_id) && e.area_id === ws.area.id && SENSOR_CATEGORIES.has(e.category) && !e.sensor_rooms.length);
    const occ = ws.room.occupancy;
    const classify = (e: EntityInfo): { tone: Tone; label: string; help: string } => {
      if (!assigned.has(e.entity_id)) return { tone: "neutral", label: "Present, not assigned", help: "The device is in this area but the room does not use it." };
      const a = AVAILABILITY[e.availability];
      return { tone: a.tone as Tone, label: a.label, help: a.help };
    };
    return html`<div class="tab-stack">
      <section class="card">
        <header class="card-head">${icon("stethoscope")}<h3>Entity status</h3></header>
        <ul class="legend">
          <li>${chip("error", "Device offline")} the physical device is unreachable (all its entities unavailable)</li>
          <li>${chip("warning", "Entity unavailable")} only this entity is unavailable</li>
          <li>${chip("error", "Missing")} the entity does not exist in Home Assistant</li>
          <li>${chip("neutral", "Present, not assigned")} found in this area, not used by the room</li>
          <li>${chip("warning", "Conflict")} used by several rooms, or from another area</li>
        </ul>
        <div class="table-wrap"><table class="device-table" data-testid="diag-table">
          <thead><tr><th scope="col">Entity</th><th scope="col">Status</th><th scope="col">State</th><th scope="col">Last changed</th><th scope="col">Device</th></tr></thead>
          <tbody>${rows.sort((a, b) => Number(assigned.has(b.entity_id)) - Number(assigned.has(a.entity_id)) || a.name.localeCompare(b.name)).map((e) => {
            const c = classify(e);
            const dev = e.device_id ? ws.devices[e.device_id] : null;
            return html`<tr data-entity=${e.entity_id} @click=${() => this.select(e.entity_id)}>
              <td><span class="cell-name">${e.name}</span><span class="muted small block mono">${e.entity_id}</span></td>
              <td><span class="has-tip" title=${c.help}>${chip(c.tone, c.label)}</span></td>
              <td>${e.state ?? "—"}${e.unit ? ` ${e.unit}` : ""}</td>
              <td>${formatTime(e.last_changed)}</td>
              <td>${dev ? html`${dev.name}<span class="muted small block">${dev.status}</span>` : html`<span class="muted">—</span>`}</td>
            </tr>`;
          })}</tbody>
        </table></div>
      </section>
      <section class="card">
        <header class="card-head">${icon("swap")}<h3>Configuration conflicts</h3>${chip(conflicts.length ? "warning" : "ok", String(conflicts.length))}</header>
        ${conflicts.length ? html`<ul class="warning-list">${conflicts.map((w) => html`<li class="level-${w.level}">${icon("warning", 20)}<span><strong>${w.title}</strong><span class="block">${w.detail}</span></span></li>`)}</ul>` : html`<p class="muted">None.</p>`}
        ${unassigned.length ? html`<p class="small">Present but unassigned sensors: ${unassigned.map((e) => e.name).join(", ")}</p>` : nothing}
      </section>
      <section class="card">
        <header class="card-head">${icon("puzzle")}<h3>Integration checks</h3></header>
        ${ws.room.problems.length
          ? html`<ul class="warning-list">${ws.room.problems.map((p) => html`<li class="level-${p.severity === "error" ? "error" : p.severity === "warning" ? "warning" : "info"}">${icon(p.severity === "error" ? "error" : p.severity === "warning" ? "warning" : "info", 20)}<span><strong>${p.title}</strong><span class="block">${p.explanation}</span><span class="muted small block">${p.action}</span></span></li>`)}</ul>`
          : html`<p class="muted">The integration reports no problem for this room.</p>`}
        ${occ?.signals?.length
          ? html`<h4 class="section-title">Occupancy decision inputs</h4><div class="table-wrap"><table class="plain-table"><thead><tr><th scope="col">Signal</th><th scope="col">Value</th><th scope="col">Contribution</th><th scope="col">Health</th></tr></thead>
              <tbody>${occ.signals.map((s) => html`<tr><td>${this.name(s.entity_id)}</td><td>${s.label}</td><td>${CONDITION_LABEL[s.condition] ?? s.condition}${s.status ? html`<span class="muted small block">${s.status}</span>` : nothing}</td><td>${s.health}</td></tr>`)}</tbody></table></div>`
          : nothing}
      </section>
      <section class="card">
        <header class="card-head">${icon("json")}<h3>Stored configuration</h3>
          <button type="button" class="btn" @click=${() => downloadJson({ area_id: ws.area.id, revision: ws.room.revision, config: ws.room.config }, `room-${ws.area.id}.json`)}>${icon("download", 18)}Download</button></header>
        ${kv([["Revision", html`<code>${ws.room.revision}</code>`], ["Read", formatTime(ws.generated_at)], ["Integration", `${ws.status.integration_version ?? "—"} (${ws.status.integration_state ?? "unknown"})`]])}
        <textarea class="json-view" readonly aria-label="Stored room configuration">${JSON.stringify(ws.room.config, null, 2)}</textarea>
      </section>
    </div>`;
  }

  // --- Inspector
  private renderInspector(ws: Workspace): unknown {
    const id = this.selected;
    if (!id) {
      return html`<aside class="ws-right inspector" aria-label="Inspector">
        <header class="inspector-head"><h3>Inspector</h3><button type="button" class="icon-btn" aria-label="Close the inspector" @click=${() => (this.inspectorOpen = false)}>${icon("close")}</button></header>
        <div class="empty-state small">${icon("tune", 36)}<p class="muted">Select a device to see its live state, identity and assignment.</p></div>
      </aside>`;
    }
    const e = this.entity(id);
    const a = this.assignment(id);
    const dev = e.device_id ? ws.devices[e.device_id] : null;
    const schema = ws.schema;
    const kinds = kindsFor(schema, id, e.suggested_kind);
    const otherRooms = [...new Set([...e.sensor_rooms, ...e.light_rooms])].filter((r) => r !== ws.area.id);
    const zones = (ws.room.lighting?.zones ?? []).filter((z) => z.sensors.includes(id));
    const refAllowed = (schema?.reference_domains ?? ["input_boolean", "binary_sensor"]).includes(e.domain);
    return html`<aside class="ws-right inspector" aria-label="Inspector" data-testid="inspector">
      <header class="inspector-head">
        <span class="inspector-icon">${icon(ws.categories[e.category]?.icon ?? "puzzle", 24)}</span>
        <div><h3>${e.name}</h3><code class="small">${id}</code></div>
        <button type="button" class="icon-btn" aria-label="Close the inspector" @click=${() => this.select(null)}>${icon("close")}</button>
      </header>
      <section class="inspector-section">
        <div class="live-state">${availabilityChip(e.availability)}<span class="state-value">${e.state ?? "—"}${e.unit ? ` ${e.unit}` : ""}</span></div>
        <p class="muted small">Last changed ${formatTime(e.last_changed)} · updated ${formatTime(e.last_updated)}</p>
        <p class="small">${AVAILABILITY[e.availability]?.help}</p>
      </section>
      <section class="inspector-section" data-testid="inspector-assignment">
        <h4>Assignment</h4>
        ${a?.kind === "sensor"
          ? html`<label class="field"><span class="field-label">Type</span>
                <select data-testid="inspector-kind" .value=${a.sensor.kind} @change=${(ev: Event) => this.setSensor(id, { kind: (ev.target as HTMLSelectElement).value })}>
                  ${kinds.map((k) => html`<option value=${k.value} ?selected=${k.value === a.sensor.kind}>${k.label}</option>`)}
                </select></label>
              <label class="field"><span class="field-label">Role</span>
                <select data-testid="inspector-role" .value=${a.sensor.role} @change=${(ev: Event) => this.setSensor(id, { role: (ev.target as HTMLSelectElement).value })}>
                  ${(schema?.roles ?? []).map((r) => html`<option value=${r.value} ?selected=${r.value === a.sensor.role}>${r.label.split(":")[0]}</option>`)}
                </select><span class="muted small">${ROLE_HELP[a.sensor.role]}</span></label>
              <label class="switch-row"><input type="checkbox" role="switch" .checked=${Boolean(a.sensor.inverted)} @change=${(ev: Event) => this.setSensor(id, { inverted: (ev.target as HTMLInputElement).checked })} />
                <span>Inverted <span class="muted small block">Counts as active when it is off (e.g. a “PC idle” sensor).</span></span></label>
              <label class="switch-row"><input type="checkbox" role="switch" .checked=${a.sensor.enabled !== false} @change=${(ev: Event) => this.setSensor(id, { enabled: (ev.target as HTMLInputElement).checked })} />
                <span>Included in occupancy <span class="muted small block">Off keeps the assignment but ignores the sensor.</span></span></label>
              <button type="button" class="btn btn-danger" data-testid="inspector-remove" @click=${() => this.unassign(id)}>${icon("minus", 18)}Remove from this room</button>`
          : a?.kind === "light"
            ? html`<p>${chip("info", "Light of this room")}</p><button type="button" class="btn btn-danger" data-testid="inspector-remove" @click=${() => this.unassign(id)}>${icon("minus", 18)}Remove from this room</button>`
            : a?.kind === "reference"
              ? html`<p>${chip("info", "Reference occupancy entity")}</p><button type="button" class="btn" @click=${() => this.update_((x) => (x.reference_occupancy_entity = null))}>${icon("close", 18)}Clear reference</button>`
              : html`<p class="muted">Not assigned to this room.</p>
                  <div class="assign-buttons">
                    ${e.domain === "light" ? html`<button type="button" class="btn btn-primary" data-testid="inspector-assign-light" @click=${() => this.assignLight(id)}>${icon("plus", 18)}Assign as light</button>` : nothing}
                    ${kinds.length && e.domain !== "light"
                      ? kinds.slice(0, 3).map((k, i) => html`<button type="button" class="btn ${i === 0 ? "btn-primary" : ""}" data-testid="inspector-assign-${k.value}" @click=${() => this.assignSensor(id, k.value)}>${icon("plus", 18)}As ${k.label.split(" (")[0]}</button>`)
                      : nothing}
                    ${refAllowed ? html`<button type="button" class="btn" @click=${() => (this.update_((x) => (x.reference_occupancy_entity = id)))}>${icon("link", 18)}Use as reference</button>` : nothing}
                  </div>`}
        ${otherRooms.length ? html`<p class="notice notice-warning small">${icon("swap", 18)}<span>Also used in ${otherRooms.map((r) => this.areaName(r)).join(", ")}.</span></p>` : nothing}
        ${e.area_id && e.area_id !== ws.area.id ? html`<p class="notice notice-warning small">${icon("swap", 18)}<span>This device is in ${e.area ?? e.area_id} in Home Assistant. It can still be used here; its Home Assistant area is not changed.</span></p>` : nothing}
        ${zones.length ? html`<p class="small muted">Used by lighting zones: ${zones.map((z) => z.name).join(", ")}</p>` : nothing}
      </section>
      <section class="inspector-section">
        <h4>Classification</h4>
        <p><strong>${e.category_label}</strong> <span class="muted small">· ${e.category_confidence === "user" ? "your choice" : `${e.category_confidence} confidence`}</span></p>
        <p class="muted small">${e.category_reason}. ${e.category_confidence === "user" ? "" : "Correct it by choosing the type when you assign it."}</p>
        ${e.capabilities.length ? html`<p class="cap-list">${e.capabilities.map((c) => html`<span class="cap">${c}</span>`)}</p>` : nothing}
      </section>
      <section class="inspector-section">
        <h4>Device</h4>
        ${dev
          ? kv([
              ["Name", dev.name],
              ["Status", dev.status === "online" ? "Online" : dev.status === "offline" ? "Offline (all entities unavailable)" : dev.status === "partial" ? "Partly unavailable" : dev.status],
              ["Manufacturer", dev.manufacturer],
              ["Model", [dev.model, dev.model_id].filter(Boolean).join(" · ") || null],
              ["Firmware", dev.sw_version],
              ["Identity", dev.identity.length ? html`${dev.identity.map((i) => html`<code class="block small">${i}</code>`)}` : null],
              ["Area", dev.area],
              ["Entities", String(dev.entity_ids.length)],
            ])
          : kv([["Device", "None (entity without a device)"], ["Area", e.area], ["Integration", e.platform]])}
      </section>
    </aside>`;
  }

  // --- Review dialog
  private renderReview(): unknown {
    if (!this.reviewOpen) return nothing;
    const r = this.review;
    const err = this.reviewError;
    const groups: Array<{ key: ReturnType<typeof changeGroup>; title: string }> = [
      { key: "added", title: "Added" },
      { key: "removed", title: "Removed" },
      { key: "classification", title: "Changed classifications" },
      { key: "settings", title: "Enabled / disabled settings" },
      { key: "relations", title: "Reference and adjacent rooms" },
    ];
    const name = (id: string | undefined) => (id ? (r?.entities[id]?.name ?? this.name(id)) : "—");
    const needs = r?.requires_confirmation ?? [];
    const ready = Boolean(r && !r.errors.length && !r.stale && r.changes.length && needs.every((c) => this.confirmed.has(c)) && r.writable && store.link !== "offline");
    return html`<dialog class="sheet sheet-full review-dialog" aria-labelledby="review-title" data-testid="review-dialog"
        @close=${() => (this.reviewOpen = false)}>
      <div class="sheet-inner">
        <header class="sheet-head">
          <h2 id="review-title">Review changes · ${this.ws?.area.name}</h2>
          <button type="button" class="icon-btn" aria-label="Close" @click=${() => this.closeReview()}>${icon("close")}</button>
        </header>
        <div class="sheet-body">
          ${err ? html`<p class="notice notice-error" role="alert">${icon("error", 20)}<span>${err.message}
              ${["connection_lost", "network", "not_connected", "api_unavailable", "not_loaded"].includes(err.code) ? html`<button type="button" class="btn" data-testid="retry-review" @click=${() => void this.refreshAndReview()}>${icon("refresh", 18)}Reload and review again</button>` : nothing}</span></p>` : nothing}
          ${this.busy && !r ? html`<p class="muted" role="status"><span class="spinner" aria-hidden="true"></span> Checking with the integration…</p>` : nothing}
          ${r?.stale ? html`<p class="notice notice-warning" data-testid="stale-notice">${icon("warning", 20)}<span>This room was changed elsewhere (for example in the integration's own room editor) after you opened it.
              <button type="button" class="btn" data-testid="refresh-review" @click=${() => void this.refreshAndReview()}>${icon("refresh", 18)}Load the current configuration and review again</button></span></p>` : nothing}
          ${r
            ? html`
                ${!r.changes.length ? html`<p class="muted">Nothing changes compared with the stored configuration.</p>` : nothing}
                ${groups.map((g) => {
                  const items = r.changes.filter((c) => changeGroup(c.type) === g.key);
                  return items.length
                    ? html`<section class="review-block" data-group=${g.key}><h3>${g.title}</h3><ul class="change-list">
                        ${items.map((c) => html`<li class="change-${g.key}">${icon(g.key === "added" ? "plus" : g.key === "removed" ? "minus" : "pencil", 18)}<span>${changeText(c, name, (a) => this.areaName(a), this.kindLabel, this.roleLabel)}</span></li>`)}
                      </ul></section>`
                    : nothing;
                })}
                ${r.errors.length ? html`<section class="review-block" data-group="errors"><h3 class="tone-error">Cannot be saved</h3><ul class="change-list">${r.errors.map((e) => html`<li>${icon("error", 18)}<span>${e.message}</span></li>`)}</ul></section>` : nothing}
                ${r.conflicts.length || r.problems_new.length
                  ? html`<section class="review-block" data-group="conflicts"><h3>Potential conflicts</h3><ul class="change-list">
                      ${r.conflicts.map((c) => html`<li>${icon("swap", 18)}<span>${name(c.entity_id)}: ${c.code === "outside_area" ? `is in ${c.rooms[0]} in Home Assistant (allowed; its area is not changed)` : c.code === "light_conflict" ? `is already a light of ${c.rooms.join(", ")}` : c.code === "category_mismatch" ? `is a ${c.rooms[0]}, assigned as a sensor` : `is also a sensor of ${c.rooms.join(", ")}`}</span></li>`)}
                      ${r.problems_new.map((p) => html`<li>${icon(p.severity === "error" ? "error" : "warning", 18)}<span><strong>${p.title}</strong> — ${p.explanation}</span></li>`)}
                    </ul></section>`
                  : nothing}
                ${r.attention.length
                  ? html`<section class="review-block" data-group="attention"><h3>Missing or unavailable</h3><ul class="change-list">${r.attention.map((a) => html`<li>${availabilityChip(a.availability)}<span>${a.name} <span class="muted mono small">${a.entity_id}</span></span></li>`)}</ul></section>`
                  : nothing}
                ${r.problems_resolved.length ? html`<section class="review-block"><h3>Resolved</h3><ul class="change-list">${r.problems_resolved.map((p) => html`<li>${icon("ok", 18)}<span>${p.title}${p.name ? ` · ${p.name}` : ""}</span></li>`)}</ul></section>` : nothing}
                ${r.impacts.length
                  ? html`<section class="review-block" data-group="impacts"><h3>What saving does</h3>
                      ${r.impacts.map((i) =>
                        i.level === "confirm"
                          ? html`<label class="confirm-row" data-impact=${i.code}><input type="checkbox" .checked=${this.confirmed.has(i.code)} @change=${(ev: Event) => {
                              const next = new Set(this.confirmed);
                              if ((ev.target as HTMLInputElement).checked) next.add(i.code);
                              else next.delete(i.code);
                              this.confirmed = next;
                            }} /><span><strong>${i.title}</strong><span class="muted block">${i.detail}</span></span></label>`
                          : html`<p class="impact-info">${icon("info", 18)}<span><strong>${i.title}.</strong> ${i.detail}</span></p>`,
                      )}
                    </section>`
                  : nothing}
                <p class="muted small">No device is switched by saving. The configuration is stored in the Smart Occupancy integration, which validates it again before saving.</p>
              `
            : nothing}
        </div>
        <footer class="sheet-foot">
          <button type="button" class="btn" @click=${() => this.closeReview()}>Cancel</button>
          <button type="button" class="btn btn-primary" data-testid="save" ?disabled=${!ready || this.busy} @click=${() => void this.save()}>
            ${icon("save", 20)}${this.busy && r ? "Saving…" : needs.length ? "Confirm and save" : "Save"}
          </button>
        </footer>
      </div>
    </dialog>`;
  }

  protected render(): unknown {
    const ws = this.ws;
    if (!ws) {
      return html`${this.loadError
        ? html`<div class="card empty-state" role="alert">${icon("error", 40)}<h3>The room could not be loaded</h3><p class="muted">${this.loadError}</p>
            <div class="actions"><a class="btn" href="#/rooms">${icon("arrow-left", 20)}All rooms</a><button type="button" class="btn" @click=${() => void this.load(false)}>${icon("refresh", 20)}Try again</button></div></div>`
        : html`<div class="card loading" role="status"><span class="spinner" aria-hidden="true"></span><span>Loading the room…</span></div>`}`;
    }
    let content: unknown;
    switch (this.tab) {
      case "devices": content = this.renderDevices(ws); break;
      case "presence": content = this.renderPresence(ws); break;
      case "lighting": content = this.renderLighting(ws); break;
      case "switches": content = this.renderSwitches(ws); break;
      case "curtains": content = this.renderCurtains(ws); break;
      case "diagnostics": content = this.renderDiagnostics(ws); break;
      default: content = this.renderOverview(ws);
    }
    return html`<div class="room-editor" data-area=${ws.area.id}>
      ${this.renderHead(ws)}
      ${apiNotice(ws.status)}
      ${this.changedElsewhere ? html`<p class="notice notice-warning" data-testid="changed-elsewhere">${icon("warning", 20)}<span>This room's configuration was changed elsewhere while you were editing. Your unsaved changes are kept;
          <button type="button" class="btn" @click=${() => void this.refreshAndReview()}>${icon("refresh", 18)}Review them against the current configuration</button></span></p>` : nothing}
      ${store.link === "offline" ? html`<p class="notice notice-error">${icon("offline", 20)}<span>The App backend cannot be reached. Your edits are kept here; saving is possible again when the connection is back.</span></p>` : nothing}
      ${this.notice ? html`<p class="notice notice-info" role="status" data-testid="saved-notice">${icon("ok", 20)}<span>${this.notice}</span></p>` : nothing}
      ${this.renderUnsaved()}
      <div class="workspace ${this.leftOpen ? "" : "left-collapsed"} ${this.inspectorOpen ? "inspector-open" : ""}">
        ${this.leftOpen ? this.renderLeft(ws) : nothing}
        <section class="ws-center" aria-label="Room">${this.renderTabs()}<div class="tab-panel" role="tabpanel">${content}</div></section>
        ${this.inspectorOpen ? this.renderInspector(ws) : nothing}
        ${this.inspectorOpen ? html`<div class="inspector-scrim" @click=${() => (this.inspectorOpen = false)}></div>` : nothing}
      </div>
      ${this.pick ? html`<sl-entity-pick .mode=${this.pick} .areaId=${ws.area.id} .schema=${ws.schema} .exclude=${this.pick === "light" ? this.draft!.lights : this.draft!.sensors.map((s) => s.entity_id)}
          @picked=${(e: CustomEvent<{ candidate: Candidate; mode: PickMode }>) => this.onPicked(e)} @close=${() => (this.pick = null)}></sl-entity-pick>` : nothing}
      ${this.renderReview()}
    </div>`;
  }
}

customElements.define("sl-room-editor", SlRoomEditor);

// ------------------------------------------------------------ entity picker
let candidateCache: { at: number; data: Candidate[] } | null = null;

export class SlEntityPick extends StoreElement {
  static properties = {
    mode: { type: String },
    areaId: { type: String },
    schema: { attribute: false },
    exclude: { attribute: false },
    all: { state: true },
    query: { state: true },
    scope: { state: true },
    error: { state: true },
  };
  declare mode: PickMode;
  declare areaId: string;
  declare schema: Workspace["schema"];
  declare exclude: string[];
  declare all: Candidate[] | null;
  declare query: string;
  declare scope: "suggested" | "all";
  declare error: string | null;

  constructor() {
    super();
    this.mode = "sensor";
    this.areaId = "";
    this.schema = null;
    this.exclude = [];
    this.all = null;
    this.query = "";
    this.scope = "suggested";
    this.error = null;
  }

  async connectedCallback(): Promise<void> {
    super.connectedCallback();
    await this.updateComplete;
    this.querySelector<HTMLDialogElement>("dialog")?.showModal();
    this.querySelector<HTMLInputElement>("input[type=search]")?.focus();
    try {
      if (!candidateCache || Date.now() - candidateCache.at > 30_000)
        candidateCache = { at: Date.now(), data: (await getRooms<{ entities: Candidate[] }>("api/rooms/candidates")).entities };
      this.all = candidateCache.data;
    } catch (err) {
      this.error = String(err);
    }
  }

  private allowed(c: Candidate): boolean {
    if (this.exclude.includes(c.entity_id)) return false;
    if (this.mode === "light") return c.domain === "light";
    if (this.mode === "reference") return (this.schema?.reference_domains ?? ["input_boolean", "binary_sensor"]).includes(c.domain);
    return kindsFor(this.schema, c.entity_id, c.suggested_kind).length > 0 && c.domain !== "light";
  }

  private suggested(c: Candidate): boolean {
    if (this.mode === "light") return true;
    if (this.mode === "reference") return c.domain === "input_boolean" || c.category === "presence_input";
    return SENSOR_CATEGORIES.has(c.category) && c.category !== "temperature" && c.category !== "illuminance";
  }

  private close(): void {
    this.querySelector<HTMLDialogElement>("dialog")?.close();
  }

  private choose(c: Candidate): void {
    this.dispatchEvent(new CustomEvent("picked", { detail: { candidate: c, mode: this.mode } }));
  }

  protected render(): unknown {
    const words = normalize(this.query).split(/\s+/).filter(Boolean);
    const items = (this.all ?? [])
      .filter((c) => this.allowed(c) && (this.scope === "all" || this.suggested(c)))
      .filter((c) => !words.length || words.every((w) => normalize(`${c.name} ${c.entity_id} ${c.area ?? ""} ${c.category_label}`).includes(w)))
      .sort((a, b) => Number(b.area_id === this.areaId) - Number(a.area_id === this.areaId) || a.name.localeCompare(b.name));
    const shown = items.slice(0, 80);
    const title = { sensor: "Add a sensor", light: "Add a light", reference: "Choose the reference occupancy entity" }[this.mode];
    return html`<dialog class="sheet sheet-full pick-dialog" aria-labelledby="pick-title" data-testid="entity-pick"
        @close=${() => this.dispatchEvent(new CustomEvent("close"))}
        @click=${(e: MouseEvent) => (e.target as Element).matches("dialog") && this.close()}>
      <div class="sheet-inner">
        <header class="sheet-head"><h2 id="pick-title">${title}</h2><button type="button" class="icon-btn" aria-label="Close" @click=${() => this.close()}>${icon("close")}</button></header>
        <div class="picker-search">${icon("search", 20)}<input type="search" autocomplete="off" spellcheck="false" placeholder="Search name, entity ID or area" aria-label="Search" .value=${this.query} @input=${(e: Event) => (this.query = (e.target as HTMLInputElement).value)} /></div>
        ${this.mode !== "light"
          ? html`<fieldset class="segmented pick-scope"><legend class="visually-hidden">Show</legend>
              <label class=${this.scope === "suggested" ? "selected" : ""}><input type="radio" name="scope" .checked=${this.scope === "suggested"} @change=${() => (this.scope = "suggested")} />Likely ${this.mode === "reference" ? "helpers" : "presence sensors"}</label>
              <label class=${this.scope === "all" ? "selected" : ""}><input type="radio" name="scope" .checked=${this.scope === "all"} @change=${() => (this.scope = "all")} />All allowed</label></fieldset>`
          : nothing}
        <div class="picker-results">
          ${this.error ? html`<p class="notice notice-error">${this.error}</p>` : !this.all ? html`<p class="muted pad">Loading…</p>` : !shown.length ? html`<p class="muted pad">Nothing matches.</p>` : html`<ul role="listbox" aria-label=${title}>
            ${shown.map((c) => html`<li role="option" aria-selected="false" data-value=${c.entity_id} @click=${() => this.choose(c)}>
              <span class="opt-primary">${c.name}</span>
              <span class="opt-secondary">${c.entity_id} · ${c.category_label} · ${c.area ?? "no area"}
                ${c.area_id && c.area_id !== this.areaId ? html`<span class="tone-warning"> · other area</span>` : nothing}
                ${c.sensor_rooms.length || c.light_rooms.length ? html`<span class="tone-warning"> · already used in another room</span>` : nothing}
                ${c.availability !== "ok" ? html`<span class="tone-warning"> · ${AVAILABILITY[c.availability]?.label}</span>` : nothing}</span>
            </li>`)}
          </ul>${items.length > shown.length ? html`<p class="muted small pad">Showing ${shown.length} of ${items.length}. Type more to narrow the list.</p>` : nothing}`}
        </div>
      </div>
    </dialog>`;
  }
}

customElements.define("sl-entity-pick", SlEntityPick);
