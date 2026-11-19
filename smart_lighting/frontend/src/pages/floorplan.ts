// Floor plan (Phase A2.1): an optional, visual way to set up the home.
//
//   Build      draw floors, rooms, walls, doors and windows; import a background
//   Configure  link rooms to Home Assistant areas, place devices, relationships
//   Live       read-only: occupancy, lights, covers, sensors and availability
//
// The plan is the App's own data (stored by the backend in /data/floorplan).
// Placing an icon is visual only: it never changes a Home Assistant area or an
// automation assignment. Assignment and adjacency changes are prepared as a
// draft in the Rooms editor, where the integration reviews and saves them.
import { html, nothing, type TemplateResult } from "lit";
import { live as liveValue } from "lit/directives/live.js";

import { formatTime, plural } from "../format";
import { icon } from "../icons";
import { inspectLight } from "../lighting/light-inspector";
import { normalize } from "../lookup";
import { navigatePath, setLeaveGuard } from "../router";
import { ApiError, AVAILABILITY, downloadJson, getRooms, type Availability } from "../rooms/model";
import { live, store } from "../store";
import { chip, StoreElement, type Tone } from "../ui";
import {
  getBackups,
  getContext,
  getEntityDetails,
  getPlan,
  imageUrl,
  importPlan,
  restoreBackup,
  reviewImport,
  savePlan,
  uploadImage,
  type BackupInfo,
  type ContextEntity,
  type EntityDetails,
  type ImportReview,
  type PlanContext,
  type PlanStatus,
} from "../floorplan/api";
import type { CurtainOverlay, EditDetail, Mode, PlacementView, RoomView, Selection, SlPlanCanvas, Tool } from "../floorplan/canvas";
import "../floorplan/canvas";
import {
  coverView,
  curtainEntityIds,
  curtainSummary,
  fetchCurtainModel,
  groupedWindows,
  layerText,
  linkedWindows,
  MODE_WORD,
  setWindowBearing,
  windowAttention,
  type CurtainModel,
  type CurtainWindow,
  type LinkedWindow,
} from "../floorplan/curtains";
import { bearingDifference, bearingText, openingOrientation } from "../floorplan/orientation";
import {
  calibrate,
  deleteOpening,
  deleteRoom,
  deleteVertex,
  insertVertex,
  moveRoom,
  openingsLostWithRoom,
  placeEntity,
  removePlacement,
  roomAt,
  roomPoints,
  setWallLength,
  updateOpening,
  updatePlacement,
  vertexMap,
  wallChain,
  wallEnds,
  wallLength,
  wallRooms,
  moveVertex,
  movePlacement,
} from "../floorplan/edit";
import { area as polygonArea, centroid, dist, perimeter, type Pt } from "../floorplan/geometry";
import { History } from "../floorplan/history";
import { DEVICE_TONE_LABEL, deviceView, GROUPS, ROOM_TONE_LABEL, roomLive, type DeviceTone, type RoomTone } from "../floorplan/live";
import {
  clonePlan,
  duplicateAreaLinks,
  floorById,
  newFloor,
  OPENING_LABEL,
  roomLabel,
  samePlan,
  sortedFloors,
  type Floor,
  type Opening,
  type OpeningType,
  type Placement,
  type Plan,
  type Room,
} from "../floorplan/model";
import { ADJACENCY_HELP, suggest, type Suggestion } from "../floorplan/suggestions";
import { formatArea, formatLength, parseLength, type Units } from "../floorplan/units";
import { clock, clockOffset, countdown, type PresenceOverview } from "../presence/model";
import { entityWarnings, overlayView, sensorIndex } from "../presence/overlay";
import { stateChip } from "../presence/components";
import { LiveReload } from "./presence";
import type { EngineStatus } from "../presence/engine";

const MODES: Array<{ key: Mode; title: string; icon: string; help: string }> = [
  { key: "build", title: "Build", icon: "rectangle", help: "Draw floors, rooms, doors and windows" },
  { key: "configure", title: "Configure", icon: "tune", help: "Link areas and place devices" },
  { key: "live", title: "Live", icon: "eye", help: "Watch occupancy and devices (read-only)" },
];

const TOOLS: Array<{ key: Tool; title: string; icon: string; keyHint: string }> = [
  { key: "select", title: "Select", icon: "cursor", keyHint: "V" },
  { key: "rect", title: "Rectangle room", icon: "rectangle", keyHint: "R" },
  { key: "polygon", title: "Polygon room", icon: "polygon", keyHint: "P" },
  { key: "door", title: "Door", icon: "door", keyHint: "D" },
  { key: "window", title: "Window", icon: "window", keyHint: "W" },
  { key: "opening", title: "Open-plan opening", icon: "open-plan", keyHint: "O" },
  { key: "calibrate", title: "Calibrate", icon: "ruler", keyHint: "K" },
];
const TOOL_KEYS: Record<string, Tool> = { v: "select", r: "rect", p: "polygon", d: "door", w: "window", o: "opening", k: "calibrate" };

const GRID_STEPS = [0.05, 0.1, 0.25, 0.5, 1];
const SENSOR_CATEGORIES = new Set(["pir", "presence", "door", "window", "pc_activity", "bed", "presence_input", "illuminance", "temperature"]);
const NORTH = [
  { value: "", label: "Not set" },
  ...["North", "North-east", "East", "South-east", "South", "South-west", "West", "North-west"].map((label, i) => ({ value: String(i * 45), label: `${label} (${i * 45}°)` })),
];
const PALETTE_LIMIT = 250;

type Dialog =
  | { kind: "confirm"; title: string; body: string; confirm: string; danger?: boolean; action: () => void }
  | { kind: "floor"; floorId: string | null; name: string; level: number; ha_floor_id: string | null }
  | { kind: "calibrate"; p1: Pt; p2: Pt; value: string; drawing: boolean; error?: string }
  | { kind: "conflict" }
  | { kind: "link"; roomId: string; areaId: string; others: Array<{ floor: Floor; room: Room }> }
  | { kind: "backups"; list: BackupInfo[] | null; error?: string; busy?: boolean }
  | { kind: "import"; file: unknown; review: ImportReview | null; error?: string; busy?: boolean }
  | { kind: "replace"; placementId: string; query: string }
  /** Phase A5: apply the orientation the plan suggests to a curtain window (the floor plan's only write). */
  | { kind: "bearing"; windowId: string; name: string; current: number | null; currentCompass: string | null; explicit: boolean; direction: string | null; suggested: number; compass: string; busy?: boolean; error?: string };

function prefNumber(key: string, fallback: number): number {
  try {
    const v = Number(window.localStorage.getItem(`smart-lighting.floorplan.${key}`));
    return Number.isFinite(v) && v > 0 ? v : fallback;
  } catch {
    return fallback;
  }
}

function prefBool(key: string, fallback: boolean): boolean {
  try {
    const v = window.localStorage.getItem(`smart-lighting.floorplan.${key}`);
    return v === null ? fallback : v === "1";
  } catch {
    return fallback;
  }
}

function setPref(key: string, value: string): void {
  try {
    window.localStorage.setItem(`smart-lighting.floorplan.${key}`, value);
  } catch {
    /* storage unavailable */
  }
}

function hashQuery(): URLSearchParams {
  return new URLSearchParams(window.location.hash.split("?")[1] ?? "");
}

export class SlFloorplan extends StoreElement {
  static properties = {
    mode: { type: String },
    plan: { state: true },
    ctx: { state: true },
    status: { state: true },
    loadError: { state: true },
    ctxError: { state: true },
    floorId: { state: true },
    tool: { state: true },
    selection: { state: true },
    presence: { state: true },
    shadow: { state: true },
    shadowData: { state: true },
    presenceData: { state: true },
    presenceError: { state: true },
    leftOpen: { state: true },
    inspectorOpen: { state: true },
    leftTab: { state: true },
    query: { state: true },
    groups: { state: true },
    hidePlaced: { state: true },
    placing: { state: true },
    saving: { state: true },
    saveError: { state: true },
    notice: { state: true },
    changedElsewhere: { state: true },
    dialog: { state: true },
    details: { state: true },
    grid: { state: true },
    snap: { state: true },
    showDims: { state: true },
    showValues: { state: true },
    chain: { state: true },
    problemsOnly: { state: true },
    showDismissed: { state: true },
    paletteLimit: { state: true },
    menuOpen: { state: true },
    curtainModel: { state: true },
    curtainError: { state: true },
  };
  /** From the address (#/floorplan/<mode>); an empty or unknown mode is replaced by the default. */
  declare mode: Mode;
  declare plan: Plan | null;
  declare ctx: PlanContext | null;
  declare status: PlanStatus | null;
  declare loadError: string | null;
  declare ctxError: string | null;
  declare floorId: string | null;
  declare tool: Tool;
  declare selection: Selection | null;
  declare leftOpen: boolean;
  declare inspectorOpen: boolean;
  declare leftTab: "devices" | "areas" | "relations" | "curtains";
  declare query: string;
  declare groups: Set<string>;
  declare hidePlaced: boolean;
  declare placing: { entity_id: string; name: string; entity_uid: string | null; device_id: string | null } | null;
  declare saving: boolean;
  declare saveError: string | null;
  declare notice: { text: string; tone: Tone } | null;
  declare changedElsewhere: boolean;
  declare dialog: Dialog | null;
  declare details: EntityDetails | null;
  declare grid: number;
  declare snap: boolean;
  declare showDims: boolean;
  declare showValues: boolean;
  declare chain: boolean;
  declare problemsOnly: boolean;
  /** Phase A3.1: the Presence overlay of the Live mode (the integration's presence view). */
  declare presence: boolean;
  /** Phase A3.2: optional shadow comparison diagnostics on the presence overlay (the App's engine vs the integration's). */
  declare shadow: boolean;
  declare shadowData: EngineStatus | null;
  declare presenceData: PresenceOverview | null;
  declare presenceError: string | null;
  private presenceOffset = 0;
  private presenceLoading = false;
  private presenceSeenRooms = -1;
  private presenceTimer: number | undefined;
  private presenceReload = new LiveReload(() => this.loadPresence(), () => (this.watched ? this.watched.split(",") : []));
  declare showDismissed: boolean;
  declare paletteLimit: number;
  declare menuOpen: boolean;
  /** Phase A5: the curtain subsystem's model (windows, covers, automatic status), read for Configure and Live. */
  declare curtainModel: CurtainModel | null;
  declare curtainError: string | null;
  private curtainLoading = false;
  private curtainSeenModels = -1;
  private curtainTimer: number | undefined;
  private curtainReload = new LiveReload(() => this.loadCurtains(), () => this.curtainIds());

  private base: Plan | null = null;
  private revision = "";
  private history = new History();
  private seenFloorplan = -1;
  private seenRegistries = -1;
  private seenRooms = -1;
  private watched = "";
  private detailsFor: string | null = null;
  private noticeTimer: number | undefined;
  private initialQuery = hashQuery();

  constructor() {
    super();
    this.mode = "build";
    this.plan = null;
    this.ctx = null;
    this.status = null;
    this.loadError = null;
    this.ctxError = null;
    this.floorId = null;
    this.tool = "select";
    this.selection = null;
    this.leftOpen = window.innerWidth >= 700;
    this.inspectorOpen = window.innerWidth >= 1040;
    this.leftTab = "devices";
    this.query = "";
    this.groups = new Set();
    this.hidePlaced = false;
    this.placing = null;
    this.saving = false;
    this.saveError = null;
    this.notice = null;
    this.changedElsewhere = false;
    this.dialog = null;
    this.details = null;
    this.grid = prefNumber("grid", 0.1);
    this.snap = prefBool("snap", true);
    this.showDims = prefBool("dims", false);
    this.showValues = prefBool("values", true);
    this.chain = true;
    this.problemsOnly = false;
    this.presence = prefBool("presence", false);
    this.shadow = prefBool("shadow", false);
    this.shadowData = null;
    this.presenceData = null;
    this.presenceError = null;
    this.showDismissed = false;
    this.paletteLimit = PALETTE_LIMIT;
    this.menuOpen = false;
    this.curtainModel = null;
    this.curtainError = null;
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
    window.addEventListener("keydown", this.onKey);
    setLeaveGuard(() => (this.dirty() ? "The floor plan has unsaved changes. Leave without saving them?" : null));
    void this.load();
    void this.loadContext();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    window.removeEventListener("beforeunload", this.onBeforeUnload);
    window.removeEventListener("keydown", this.onKey);
    setLeaveGuard(null);
    live.watch([]);
    this.presenceReload.stop();
    window.clearInterval(this.presenceTimer);
    this.curtainReload.stop();
    window.clearTimeout(this.curtainTimer);
  }

  // ------------------------------------------------------------- curtains (Phase A5)
  /** The curtain model, read once per page view and again when the curtain configuration changes. */
  async loadCurtains(): Promise<void> {
    if (this.curtainLoading) return;
    this.curtainLoading = true;
    try {
      this.curtainModel = await fetchCurtainModel();
      this.curtainError = null;
      this.updateWatch();
    } catch (err) {
      // The curtain bridge may be unavailable (integration offline, older integration): the plan still works.
      this.curtainError = err instanceof ApiError ? err.message : String(err);
    } finally {
      this.curtainLoading = false;
    }
  }

  private curtainsOn(): boolean {
    return this.mode === "live" || this.mode === "configure";
  }

  /** The window openings of the shown floor with their curtain windows. */
  private links(floor = this.floor()): LinkedWindow[] {
    return floor ? linkedWindows(floor, this.curtainModel) : [];
  }

  private curtainIds(): string[] {
    return this.mode === "live" ? curtainEntityIds(this.links()) : [];
  }

  private curtainWindow(opening: Opening): { window: CurtainWindow | null; dangling: boolean } {
    const window = opening.curtain_window_id ? (this.curtainModel?.windows.find((w) => w.window_id === opening.curtain_window_id) ?? null) : null;
    return { window, dangling: Boolean(opening.curtain_window_id && this.curtainModel && !window) };
  }

  private curtainOverlays(floor: Floor): Map<string, CurtainOverlay> {
    const out = new Map<string, CurtainOverlay>();
    if (this.mode !== "live") return out;
    for (const l of this.links(floor)) {
      if (!l.window) continue;
      const v = coverView(l.window, store.liveStates);
      out.set(l.opening.id, { position: v.position, tone: v.tone, title: `${l.window.name} · ${v.text} · ${MODE_WORD[l.window.mode]}` });
    }
    return out;
  }

  /** Link a window opening to a curtain window (or unlink): a normal plan edit, undoable, saved with the plan. */
  private linkCurtain(opening: Opening, windowId: string | null): void {
    const floor = this.floor();
    if (!floor || !this.writable()) return;
    const r = updateOpening(floor, opening.id, { curtain_window_id: windowId });
    if (r.ok) this.applyFloor(r.value, windowId ? "Link curtain window" : "Unlink curtain window");
    else this.flash(r.error, "warning");
  }

  private openBearingDialog(window: CurtainWindow, suggested: number, compass: string): void {
    this.dialog = {
      kind: "bearing",
      windowId: window.window_id,
      name: window.name,
      current: window.effective_bearing,
      currentCompass: window.compass,
      explicit: window.bearing !== null && window.bearing !== undefined,
      direction: window.direction,
      suggested,
      compass,
    };
  }

  private async applyBearing(d: Extract<Dialog, { kind: "bearing" }>): Promise<void> {
    this.dialog = { ...d, busy: true, error: undefined };
    try {
      await setWindowBearing(d.windowId, d.suggested);
      this.querySelector<HTMLDialogElement>("dialog.fp-dialog")?.close();
      this.dialog = null;
      this.flash(`${d.name} now faces ${bearingText(d.suggested, d.compass)} in the curtain configuration.`);
      await this.loadCurtains();
    } catch (err) {
      this.dialog = { ...d, busy: false, error: err instanceof ApiError ? err.message : String(err) };
    }
  }

  async loadPresence(): Promise<void> {
    if (this.presenceLoading) return;
    this.presenceLoading = true;
    try {
      const data = await getRooms<PresenceOverview>("api/presence");
      this.presenceOffset = clockOffset(data.now);
      this.presenceData = data;
      this.shadowData = this.shadow ? await getRooms<EngineStatus>("api/engine").catch(() => null) : null;
      this.presenceError = null;
      this.updateWatch();
    } catch (err) {
      this.presenceError = err instanceof Error ? err.message : String(err);
    } finally {
      this.presenceLoading = false;
    }
  }

  private setPresence(on: boolean): void {
    this.presence = on;
    setPref("presence", on ? "1" : "0");
    window.clearInterval(this.presenceTimer);
    if (on) {
      void this.loadPresence();
      this.presenceTimer = window.setInterval(() => this.requestUpdate(), 1000);
    } else {
      this.presenceData = null;
      this.updateWatch();
    }
  }

  private presenceOn(): boolean {
    return this.mode === "live" && this.presence;
  }

  protected willUpdate(changed: Map<string, unknown>): void {
    if (this.presenceOn()) {
      if (!this.presenceData && !this.presenceLoading && !this.presenceError) {
        void this.loadPresence();
        window.clearInterval(this.presenceTimer);
        this.presenceTimer = window.setInterval(() => this.requestUpdate(), 1000);
      }
      if (this.presenceData) this.presenceReload.check();
      if (store.roomsVersion !== this.presenceSeenRooms) {
        const first = this.presenceSeenRooms === -1;
        this.presenceSeenRooms = store.roomsVersion;
        if (!first) void this.loadPresence();
      }
    }
    if (this.curtainsOn() && this.plan) {
      // Phase A5: the curtain model, once per page view; again (debounced) when the curtain configuration or a cover changes.
      if (!this.curtainModel && !this.curtainLoading && !this.curtainError) void this.loadCurtains();
      if (store.curtainsModelsVersion !== this.curtainSeenModels) {
        const first = this.curtainSeenModels === -1;
        this.curtainSeenModels = store.curtainsModelsVersion;
        if (!first) {
          window.clearTimeout(this.curtainTimer);
          this.curtainTimer = window.setTimeout(() => void this.loadCurtains(), 800);
        }
      }
      if (this.curtainModel && this.mode === "live") this.curtainReload.check();
    }
    if (!MODES.some((m) => m.key === this.mode) && this.plan) {
      // Default: watch a drawn home live; start drawing when there is nothing yet.
      const mode: Mode = this.plan.floors.some((f) => f.rooms.length) ? "live" : "build";
      history.replaceState(null, "", `#/floorplan/${mode}${window.location.hash.includes("?") ? `?${window.location.hash.split("?")[1]}` : ""}`);
      this.mode = mode;
    }
    if (changed.has("mode")) {
      this.tool = "select";
      this.placing = null;
      // Outside Build, rooms, device icons and windows (curtains, Phase A5) can stay selected.
      const isWindow = this.selection?.kind === "opening" && this.floor()?.openings.find((o) => o.id === this.selection!.id)?.type === "window";
      if (this.mode === "live" && this.selection && !["room", "placement"].includes(this.selection.kind) && !isWindow) this.selection = null;
      if (this.mode === "configure" && this.selection && !["room", "placement"].includes(this.selection.kind) && !isWindow) this.selection = null;
      if (this.mode === "build" && this.selection?.kind === "placement") this.selection = null;
    }
    if (store.floorplanVersion !== this.seenFloorplan) {
      this.seenFloorplan = store.floorplanVersion;
      const announced = store.floorplanRevision;
      if (announced && this.plan && announced !== this.revision) {
        if (this.dirty()) this.changedElsewhere = true;
        else void this.load(true);
      }
    }
    if (this.ctx && (store.registriesVersion !== this.seenRegistries || store.roomsVersion !== this.seenRooms)) void this.loadContext();
    this.updateWatch();
  }

  protected updated(): void {
    const dialog = this.querySelector<HTMLDialogElement>("dialog.fp-dialog");
    if (dialog && !dialog.open) dialog.showModal();
  }

  async load(quiet = false): Promise<void> {
    try {
      const doc = await getPlan();
      this.base = clonePlan(doc.plan);
      this.plan = clonePlan(doc.plan);
      this.revision = doc.revision;
      this.status = doc.status;
      this.loadError = null;
      this.changedElsewhere = false;
      this.history.clear(); // undo steps refer to the version that was replaced
      const floors = sortedFloors(this.plan);
      const wanted = this.initialQuery.get("floor");
      if (!this.floorId || !floorById(this.plan, this.floorId)) this.floorId = (wanted && floorById(this.plan, wanted) ? wanted : floors[floors.length - 1]?.id) ?? null;
      if (this.selection && !this.selectionExists(this.selection)) this.selection = null;
      if (quiet) this.flash("The floor plan was updated from another session.", "info");
      const area = this.initialQuery.get("area");
      if (area) {
        this.initialQuery.delete("area");
        this.focusArea(area);
      }
    } catch (err) {
      this.loadError = err instanceof ApiError ? err.message : String(err);
    }
  }

  async loadContext(): Promise<void> {
    this.seenRegistries = store.registriesVersion;
    this.seenRooms = store.roomsVersion;
    try {
      this.ctx = await getContext();
      this.ctxError = null;
    } catch (err) {
      this.ctxError = err instanceof ApiError ? err.message : String(err);
    }
  }

  private focusArea(areaId: string): void {
    if (!this.plan) return;
    for (const floor of this.plan.floors) {
      const room = floor.rooms.find((r) => r.area_id === areaId);
      if (room) {
        this.floorId = floor.id;
        this.select({ kind: "room", id: room.id });
        void this.updateComplete.then(() => this.canvas()?.centreOn(centroid(roomPoints(floor, room))));
        return;
      }
    }
    this.flash("This area is not on the floor plan yet. Link it to a drawn room under Configure → Areas.", "info");
  }

  private updateWatch(): void {
    const floor = this.floor();
    if (!floor || this.mode === "build") {
      if (this.watched) {
        this.watched = "";
        live.watch([]);
      }
      return;
    }
    const ids = new Set<string>();
    for (const p of floor.placements) ids.add(this.resolve(p).entity_id);
    for (const room of floor.rooms) {
      const ents = room.area_id ? this.ctx?.room_entities[room.area_id] : undefined;
      if (ents?.state) ids.add(ents.state);
      if (ents?.mood_lighting) ids.add(ents.mood_lighting);
      // Presence overlay: the room's sensors too (their changes refresh the engine's view).
      const pr = this.presenceOn() && room.area_id ? this.presenceData?.rooms.find((r) => r.area_id === room.area_id) : undefined;
      for (const s of pr?.sensors ?? []) ids.add(s.entity_id);
    }
    // Curtains (Phase A5): the covers of linked windows, for live positions on the plan.
    for (const id of this.curtainIds()) ids.add(id);
    const key = [...ids].sort().join(",");
    if (key !== this.watched) {
      this.watched = key;
      live.watch([...ids]);
    }
  }

  // ------------------------------------------------------------ helpers
  private canvas(): SlPlanCanvas | null {
    return this.querySelector("sl-plan-canvas");
  }

  floor(): Floor | undefined {
    return this.plan ? floorById(this.plan, this.floorId) : undefined;
  }

  dirty(): boolean {
    return Boolean(this.plan && this.base && !samePlan(this.plan, this.base));
  }

  private writable(): boolean {
    return Boolean(this.status?.writable);
  }

  private units(): Units {
    return this.ctx?.units ?? "metric";
  }

  private areaName = (id: string): string => this.ctx?.areas.find((a) => a.id === id)?.name ?? id;

  private flash(text: string, tone: Tone = "ok"): void {
    this.notice = { text, tone };
    window.clearTimeout(this.noticeTimer);
    this.noticeTimer = window.setTimeout(() => (this.notice = null), tone === "error" ? 9000 : 5000);
  }

  private selectionExists(sel: Selection): boolean {
    const f = this.floor();
    if (!f) return false;
    switch (sel.kind) {
      case "room":
        return f.rooms.some((r) => r.id === sel.id);
      case "wall":
        return f.walls.some((w) => w.id === sel.id);
      case "vertex":
        return f.vertices.some((v) => v.id === sel.id);
      case "opening":
        return f.openings.some((o) => o.id === sel.id);
      case "placement":
        return f.placements.some((p) => p.id === sel.id);
      case "background":
        return Boolean(f.background);
    }
  }

  private entityIndex(): { byId: Map<string, ContextEntity>; byUid: Map<string, ContextEntity> } {
    const byId = new Map<string, ContextEntity>();
    const byUid = new Map<string, ContextEntity>();
    for (const e of this.ctx?.entities ?? []) {
      byId.set(e.entity_id, e);
      if (e.entity_uid) byUid.set(e.entity_uid, e);
    }
    return { byId, byUid };
  }

  private indexCache: { ctx: PlanContext | null; index: ReturnType<SlFloorplan["entityIndex"]> } | null = null;

  private index(): ReturnType<SlFloorplan["entityIndex"]> {
    if (!this.indexCache || this.indexCache.ctx !== this.ctx) this.indexCache = { ctx: this.ctx, index: this.entityIndex() };
    return this.indexCache.index;
  }

  /** The placement's entity now: renamed entities are found by their registry id. */
  private resolve(p: Placement): { entity_id: string; entity: ContextEntity | undefined; renamed: boolean; missing: boolean } {
    const { byId, byUid } = this.index();
    const direct = byId.get(p.entity_id);
    if (direct) return { entity_id: p.entity_id, entity: direct, renamed: false, missing: false };
    const moved = p.entity_uid ? byUid.get(p.entity_uid) : undefined;
    if (moved) return { entity_id: moved.entity_id, entity: moved, renamed: true, missing: false };
    return { entity_id: p.entity_id, entity: undefined, renamed: false, missing: Boolean(this.ctx) };
  }

  private placedIds(): Map<string, { floor: Floor; placement: Placement }> {
    const out = new Map<string, { floor: Floor; placement: Placement }>();
    for (const floor of this.plan?.floors ?? []) for (const p of floor.placements) out.set(this.resolve(p).entity_id, { floor, placement: p });
    return out;
  }

  // ---------------------------------------------------------------- edits
  private applyFloor(floor: Floor, label: string, select?: Selection | null): void {
    if (!this.plan) return;
    this.history.push(this.plan, label);
    this.plan = { ...this.plan, floors: this.plan.floors.map((f) => (f.id === floor.id ? floor : f)) };
    if (select !== undefined) this.select(select);
    else if (this.selection && !this.selectionExists(this.selection)) this.select(null);
    this.saveError = null;
  }

  private applyPlan(plan: Plan, label: string): void {
    if (!this.plan) return;
    this.history.push(this.plan, label);
    this.plan = plan;
    if (!floorById(plan, this.floorId)) this.floorId = sortedFloors(plan)[0]?.id ?? null;
    if (this.selection && !this.selectionExists(this.selection)) this.select(null);
    this.saveError = null;
  }

  private editFloor(label: string, mutate: (f: Floor) => void): void {
    const floor = this.floor();
    if (!floor) return;
    const next = structuredClone(floor);
    mutate(next);
    this.applyFloor(next, label);
  }

  private undo(): void {
    if (!this.plan) return;
    const label = this.history.undoLabel;
    const prev = this.history.undo(this.plan);
    if (!prev) return;
    this.plan = prev;
    if (!floorById(prev, this.floorId)) this.floorId = sortedFloors(prev)[0]?.id ?? null;
    if (this.selection && !this.selectionExists(this.selection)) this.select(null);
    this.flash(`Undone: ${label}`, "info");
  }

  private redo(): void {
    if (!this.plan) return;
    const label = this.history.redoLabel;
    const next = this.history.redo(this.plan);
    if (!next) return;
    this.plan = next;
    if (!floorById(next, this.floorId)) this.floorId = sortedFloors(next)[0]?.id ?? null;
    if (this.selection && !this.selectionExists(this.selection)) this.select(null);
    this.flash(`Redone: ${label}`, "info");
  }

  private select(sel: Selection | null): void {
    this.selection = sel;
    // The inspector opens on selection where it is a column, and always outside Build (where it is the main view).
    if (sel && (window.innerWidth >= 1040 || this.mode !== "build")) this.inspectorOpen = true;
    if (sel?.kind === "placement") {
      const p = this.floor()?.placements.find((x) => x.id === sel.id);
      if (p) void this.loadDetails(this.resolve(p).entity_id);
    } else {
      this.details = null;
      this.detailsFor = null;
    }
  }

  private async loadDetails(entityId: string): Promise<void> {
    this.detailsFor = entityId;
    try {
      const d = await getEntityDetails(entityId);
      if (this.detailsFor === entityId) this.details = d;
    } catch {
      if (this.detailsFor === entityId) this.details = null;
    }
  }

  private onCanvasEdit(e: CustomEvent<EditDetail>): void {
    this.applyFloor(e.detail.floor, e.detail.label, e.detail.select);
    if (e.detail.done) this.tool = "select";
  }

  private onPlace(e: CustomEvent<{ x: number; y: number; entity_id: string }>): void {
    const floor = this.floor();
    if (!floor || !this.writable()) return;
    const placed = this.placedIds().get(e.detail.entity_id);
    if (placed) {
      if (placed.floor.id === floor.id) {
        this.applyFloor(movePlacement(floor, placed.placement.id, e.detail), "Move device icon", { kind: "placement", id: placed.placement.id });
      } else this.flash(`This device is already placed on ${placed.floor.name}. Remove it there first to place it here.`, "warning");
      this.placing = null;
      return;
    }
    const entity = this.index().byId.get(e.detail.entity_id) ?? (this.placing?.entity_id === e.detail.entity_id ? this.placing : null);
    if (!entity) return;
    const { floor: next, placementId } = placeEntity(floor, { entity_id: entity.entity_id, entity_uid: entity.entity_uid, device_id: entity.device_id }, e.detail);
    this.applyFloor(next, "Place device", { kind: "placement", id: placementId });
    this.placing = null;
  }

  private onCalibrate(e: CustomEvent<{ p1: Pt; p2: Pt }>): void {
    const floor = this.floor();
    this.dialog = { kind: "calibrate", p1: e.detail.p1, p2: e.detail.p2, value: "", drawing: Boolean(floor?.rooms.length) };
  }

  private deleteSelection(): void {
    const floor = this.floor();
    const sel = this.selection;
    if (!floor || !sel || !this.writable() || this.mode === "live") return;
    switch (sel.kind) {
      case "room": {
        const room = floor.rooms.find((r) => r.id === sel.id);
        if (!room) return;
        const lost = openingsLostWithRoom(floor, room.id);
        this.dialog = {
          kind: "confirm",
          title: `Delete ${roomLabel(room, this.areaName)}?`,
          body: `The room${lost.length ? ` and ${plural(lost.length, "door or window", "doors and windows")} on its own walls` : ""} will be removed from the plan. Device icons stay where they are, and nothing changes in Home Assistant or in the integration. You can undo this.`,
          confirm: "Delete room",
          danger: true,
          action: () => this.applyFloor(deleteRoom(floor, room.id), "Delete room", null),
        };
        return;
      }
      case "vertex": {
        const r = deleteVertex(floor, sel.id);
        if (r.ok) this.applyFloor(r.value, "Delete corner", null);
        else this.flash(r.error, "warning");
        return;
      }
      case "opening":
        this.applyFloor(deleteOpening(floor, sel.id), "Delete opening", null);
        this.flash("Removed. Undo brings it back.", "info");
        return;
      case "placement":
        this.applyFloor(removePlacement(floor, sel.id), "Remove device icon", null);
        this.flash("The icon was removed from the plan (the device is not changed). Undo brings it back.", "info");
        return;
      case "background":
        this.dialog = {
          kind: "confirm",
          title: "Remove the background image?",
          body: "The image is removed from this floor; your drawing stays. The image file is kept with earlier backups.",
          confirm: "Remove background",
          danger: true,
          action: () => this.editFloor("Remove background", (f) => (f.background = null)),
        };
        return;
      case "wall":
        this.flash("Walls belong to rooms: move the wall, delete a corner, or delete the room.", "info");
    }
  }

  private onKey = (e: KeyboardEvent): void => {
    const target = e.target as HTMLElement | null;
    if (target && (target.closest("input, textarea, select, [contenteditable]") || target.closest("dialog"))) return;
    const mod = e.ctrlKey || e.metaKey;
    const canvas = this.canvas();
    if (mod && e.key.toLowerCase() === "s") {
      e.preventDefault();
      void this.save();
    } else if (mod && e.key.toLowerCase() === "z") {
      e.preventDefault();
      if (e.shiftKey) this.redo();
      else this.undo();
    } else if (mod && e.key.toLowerCase() === "y") {
      e.preventDefault();
      this.redo();
    } else if (e.key === "Escape") {
      if (this.menuOpen) this.menuOpen = false;
      else if (this.placing) this.placing = null;
      else if (this.tool !== "select") this.tool = "select";
      else if (this.selection) this.select(null);
      canvas?.resetTool();
    } else if (e.key === "Enter" && this.tool === "polygon") {
      canvas?.finishPolygon();
    } else if ((e.key === "Delete" || e.key === "Backspace") && !mod) {
      if (this.tool === "polygon" && canvas?.poly.length) canvas.undoPoint();
      else this.deleteSelection();
      e.preventDefault();
    } else if (!mod && !e.altKey && this.mode === "build" && TOOL_KEYS[e.key.toLowerCase()] && this.writable()) {
      this.tool = TOOL_KEYS[e.key.toLowerCase()];
    } else if (e.key.startsWith("Arrow") && this.selection && this.mode !== "live") {
      const step = e.shiftKey ? 1 : this.grid || 0.1;
      const d = { x: e.key === "ArrowLeft" ? -step : e.key === "ArrowRight" ? step : 0, y: e.key === "ArrowUp" ? -step : e.key === "ArrowDown" ? step : 0 };
      if (this.nudge(d)) e.preventDefault();
    } else if ((e.key === "+" || e.key === "=") && canvas) canvas.zoomBy(1.25);
    else if (e.key === "-" && canvas) canvas.zoomBy(0.8);
    else if (e.key === "0" && canvas) canvas.fit();
  };

  private nudge(d: Pt): boolean {
    const floor = this.floor();
    const sel = this.selection;
    if (!floor || !sel || !this.writable()) return false;
    if (sel.kind === "placement") {
      const p = floor.placements.find((x) => x.id === sel.id);
      if (!p) return false;
      this.applyFloor(movePlacement(floor, p.id, { x: p.x + d.x, y: p.y + d.y }), "Move device icon");
      return true;
    }
    if (this.mode !== "build") return false;
    if (sel.kind === "room") {
      const r = moveRoom(floor, sel.id, d, true);
      if (r.ok) this.applyFloor(r.value, "Move room");
      else this.flash(r.error, "warning");
      return true;
    }
    if (sel.kind === "vertex") {
      const v = floor.vertices.find((x) => x.id === sel.id);
      if (!v) return false;
      const r = moveVertex(floor, v.id, { x: v.x + d.x, y: v.y + d.y });
      if (r.ok) this.applyFloor(r.value, "Move corner");
      else this.flash(r.error, "warning");
      return true;
    }
    return false;
  }

  // --------------------------------------------------------------- saving
  async save(): Promise<void> {
    if (!this.plan || this.saving || !this.dirty()) return;
    const duplicates = duplicateAreaLinks(this.plan);
    if (duplicates.length) {
      this.saveError = `${duplicates.map((a) => this.areaName(a)).join(", ")} ${duplicates.length === 1 ? "is" : "are"} linked to more than one room. Mark the rooms as one shared space or link only one (Configure → select the room).`;
      return;
    }
    this.saving = true;
    this.saveError = null;
    const sent = clonePlan(this.plan);
    try {
      const result = await savePlan(sent, this.revision);
      this.base = sent;
      this.revision = result.revision;
      if (this.status) this.status = { ...this.status, revision: result.revision, saved_at: result.saved_at, notices: [] };
      this.changedElsewhere = false;
      this.flash(result.saved ? `Saved at ${formatTime(result.saved_at)}.` : "Nothing to save.");
    } catch (err) {
      if (err instanceof ApiError && err.code === "stale_revision") this.dialog = { kind: "conflict" };
      else this.saveError = err instanceof ApiError ? err.message : String(err);
    } finally {
      this.saving = false;
    }
  }

  private async overwrite(): Promise<void> {
    try {
      const latest = await getPlan();
      this.revision = latest.revision;
      this.dialog = null;
      await this.save();
    } catch (err) {
      this.saveError = err instanceof ApiError ? err.message : String(err);
    }
  }

  // ------------------------------------------------------------- floors
  private openFloorDialog(floor: Floor | null): void {
    const levels = (this.plan?.floors ?? []).map((f) => f.level);
    this.dialog = floor
      ? { kind: "floor", floorId: floor.id, name: floor.name, level: floor.level, ha_floor_id: floor.ha_floor_id }
      : { kind: "floor", floorId: null, name: levels.length ? `Floor ${levels.length + 1}` : "Ground floor", level: levels.length ? Math.max(...levels) + 1 : 0, ha_floor_id: null };
  }

  private saveFloorDialog(d: Extract<Dialog, { kind: "floor" }>): void {
    if (!this.plan) return;
    const name = d.name.trim();
    if (!name) return;
    if (d.floorId) {
      const plan = clonePlan(this.plan);
      const f = floorById(plan, d.floorId);
      if (f) Object.assign(f, { name, level: d.level, ha_floor_id: d.ha_floor_id });
      this.applyPlan(plan, "Edit floor");
    } else {
      const f = { ...newFloor(name, d.level), ha_floor_id: d.ha_floor_id };
      this.applyPlan({ ...this.plan, floors: [...this.plan.floors, f] }, "Add floor");
      this.floorId = f.id;
    }
    this.dialog = null;
  }

  private floorsFromHomeAssistant(): void {
    if (!this.plan || !this.ctx?.floors.length) return;
    const existing = new Set(this.plan.floors.map((f) => f.ha_floor_id));
    const add = this.ctx.floors.filter((f) => !existing.has(f.id)).map((f, i) => ({ ...newFloor(f.name, typeof f.level === "number" ? f.level : i), ha_floor_id: f.id }));
    if (!add.length) return;
    this.applyPlan({ ...this.plan, floors: [...this.plan.floors, ...add] }, "Add floors from Home Assistant");
    this.floorId = add[0].id;
  }

  private deleteFloor(floor: Floor): void {
    this.dialog = {
      kind: "confirm",
      title: `Delete ${floor.name}?`,
      body: `${plural(floor.rooms.length, "room", "rooms")}, ${plural(floor.openings.length, "door or window", "doors and windows")} and ${plural(floor.placements.length, "device icon", "device icons")} on this floor are removed from the plan. Nothing changes in Home Assistant. You can undo this until you leave the page.`,
      confirm: "Delete floor",
      danger: true,
      action: () => {
        if (!this.plan) return;
        this.applyPlan({ ...this.plan, floors: this.plan.floors.filter((f) => f.id !== floor.id) }, "Delete floor");
      },
    };
  }

  // ---------------------------------------------------------- background
  private async importBackground(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = "";
    const floor = this.floor();
    if (!file || !floor) return;
    try {
      this.flash("Uploading the image…", "info");
      const info = await uploadImage(file);
      const pts = [...floor.vertices];
      const minX = pts.length ? Math.min(...pts.map((p) => p.x)) : 0;
      const minY = pts.length ? Math.min(...pts.map((p) => p.y)) : 0;
      const span = pts.length ? Math.max(Math.max(...pts.map((p) => p.x)) - minX, Math.max(...pts.map((p) => p.y)) - minY, 4) * 1.15 : 15;
      const scale = span / Math.max(info.width_px, info.height_px);
      this.editFloor("Import background", (f) => {
        f.background = { image_id: info.image_id, width_px: info.width_px, height_px: info.height_px, x: minX, y: minY, scale, rotation: 0, opacity: 0.5, visible: true, locked: false };
      });
      this.select({ kind: "background", id: floor.id });
      this.flash("Image imported. Use Calibrate with a known distance to set its real size, then trace the rooms over it.");
    } catch (err) {
      this.flash(err instanceof ApiError ? err.message : String(err), "error");
    }
  }

  private applyCalibration(d: Extract<Dialog, { kind: "calibrate" }>): void {
    const floor = this.floor();
    const real = parseLength(d.value, this.units());
    if (!floor || real === null) {
      this.dialog = { ...d, error: "Enter the real distance, for example 3.6 (metres)." };
      return;
    }
    const r = calibrate(floor, d.p1, d.p2, real, d.drawing);
    if (!r.ok) {
      this.dialog = { ...d, error: r.error };
      return;
    }
    this.applyFloor(r.value.floor, "Calibrate");
    this.dialog = null;
    this.tool = "select";
    this.flash(`Calibrated: everything ${d.drawing || !floor.background ? "" : "on the image "}was scaled by ${r.value.factor.toFixed(3)}. Dimensions on this floor are now marked as measured.`);
    void this.updateComplete.then(() => this.canvas()?.fit());
  }

  // ---------------------------------------------------------- area links
  private linkArea(room: Room, areaId: string | null): void {
    const floor = this.floor();
    if (!floor || !this.plan) return;
    if (areaId) {
      const others = this.plan.floors.flatMap((f) => f.rooms.filter((r) => r.area_id === areaId && r.id !== room.id).map((r) => ({ floor: f, room: r })));
      if (others.length) {
        this.dialog = { kind: "link", roomId: room.id, areaId, others };
        return;
      }
    }
    const name = areaId ? this.areaName(areaId) : null;
    this.editFloor(areaId ? "Link area" : "Unlink area", (f) => {
      const r = f.rooms.find((x) => x.id === room.id);
      if (!r) return;
      r.area_id = areaId;
      r.area_name = name;
      r.shares_area = false;
      if (areaId && (!r.name || /^Room \d+$/.test(r.name))) r.name = name ?? "";
    });
  }

  private resolveLink(d: Extract<Dialog, { kind: "link" }>, how: "move" | "share"): void {
    if (!this.plan) return;
    const plan = clonePlan(this.plan);
    const name = this.areaName(d.areaId);
    for (const f of plan.floors) {
      for (const r of f.rooms) {
        if (r.id === d.roomId) Object.assign(r, { area_id: d.areaId, area_name: name, shares_area: how === "share", name: r.name && !/^Room \d+$/.test(r.name) ? r.name : name });
        else if (r.area_id === d.areaId) {
          if (how === "move") Object.assign(r, { area_id: null, area_name: null, shares_area: false });
          else r.shares_area = true;
        }
      }
    }
    this.applyPlan(plan, how === "share" ? "Link area (shared space)" : "Move area link");
    this.dialog = null;
  }

  private repairRenamed(): void {
    if (!this.plan) return;
    const plan = clonePlan(this.plan);
    let n = 0;
    for (const f of plan.floors) {
      for (const p of f.placements) {
        const r = this.resolve(p);
        if (r.renamed) {
          p.entity_id = r.entity_id;
          n += 1;
        }
      }
      for (const room of f.rooms) {
        if (room.area_id && this.ctx?.areas.some((a) => a.id === room.area_id) && room.area_name !== this.areaName(room.area_id)) room.area_name = this.areaName(room.area_id);
      }
    }
    this.applyPlan(plan, "Update renamed entities and areas");
    this.flash(`${plural(n, "device icon was", "device icons were")} updated. Save to keep it.`);
  }

  /** Rooms editor with a prepared (unsaved) change; the integration reviews it there before saving. */
  private openRoomsEditor(areaId: string, tab: string, params: Record<string, string> = {}): void {
    const proceed = () => {
      const query = new URLSearchParams({ ...params, from: "floorplan" }).toString();
      setLeaveGuard(null);
      window.location.hash = `/rooms/${encodeURIComponent(areaId)}/${tab}?${query}`;
    };
    if (this.dirty()) {
      this.dialog = {
        kind: "confirm",
        title: "Save the floor plan first?",
        body: "The floor plan has unsaved changes. Save them before opening the Rooms editor, or they are lost.",
        confirm: "Save and continue",
        action: () => void this.save().then(() => !this.dirty() && proceed()),
      };
      return;
    }
    proceed();
  }

  // ---------------------------------------------------------- import/export
  private async exportPlan(): Promise<void> {
    try {
      const data = await getRooms<{ exported_at: string }>("api/floorplan/export");
      downloadJson(data, `smart-lighting-floorplan-${data.exported_at.replace(/[:-]/g, "").slice(0, 15)}.json`);
    } catch (err) {
      this.flash(err instanceof ApiError ? err.message : String(err), "error");
    }
  }

  private async chooseImport(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = "";
    if (!file) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(await file.text());
    } catch {
      this.dialog = { kind: "import", file: null, review: null, error: "This file is not a floor plan export (JSON)." };
      return;
    }
    this.dialog = { kind: "import", file: parsed, review: null, busy: true };
    try {
      const review = await reviewImport(parsed);
      this.dialog = { kind: "import", file: parsed, review };
    } catch (err) {
      this.dialog = { kind: "import", file: parsed, review: null, error: err instanceof ApiError ? err.message : String(err) };
    }
  }

  private async confirmImport(d: Extract<Dialog, { kind: "import" }>): Promise<void> {
    if (!d.review) return;
    this.dialog = { ...d, busy: true };
    try {
      await importPlan(d.file, d.review.revision);
      this.dialog = null;
      await this.load();
      this.flash("Imported. The previous floor plan was kept as a backup.");
    } catch (err) {
      this.dialog = { ...d, busy: false, error: err instanceof ApiError ? (err.code === "stale_revision" ? "The floor plan changed meanwhile. Choose the file again to review it." : err.message) : String(err) };
    }
  }

  private async openBackups(): Promise<void> {
    this.dialog = { kind: "backups", list: null };
    try {
      this.dialog = { kind: "backups", list: await getBackups() };
    } catch (err) {
      this.dialog = { kind: "backups", list: [], error: String(err) };
    }
  }

  private async restore(b: BackupInfo): Promise<void> {
    const d = this.dialog;
    if (d?.kind !== "backups") return;
    this.dialog = { ...d, busy: true };
    try {
      await restoreBackup(b.id, this.revision);
      this.dialog = null;
      await this.load();
      this.flash(`Restored the version from ${formatTime(b.created_at)}. The version before was kept as a backup.`);
    } catch (err) {
      this.dialog = { ...d, busy: false, error: err instanceof ApiError ? err.message : String(err) };
    }
  }

  // ------------------------------------------------------------ rendering
  private roomViews(floor: Floor): Map<string, RoomView> {
    const out = new Map<string, RoomView>();
    const dup = new Set(this.plan ? duplicateAreaLinks(this.plan) : []);
    for (const room of floor.rooms) {
      const title = roomLabel(room, this.areaName);
      const areaKnown = !room.area_id || !this.ctx || this.ctx.areas.some((a) => a.id === room.area_id);
      if (this.mode === "build") {
        out.set(room.id, { tone: "build", title, subtitle: room.area_id && room.name && room.name !== this.areaName(room.area_id) ? this.areaName(room.area_id) : null });
      } else if (this.mode === "configure") {
        const problem = !areaKnown || (room.area_id && dup.has(room.area_id));
        const cfg = room.area_id ? this.ctx?.rooms[room.area_id] : undefined;
        out.set(room.id, {
          tone: problem ? "problem" : room.area_id ? "linked" : "unlinked",
          title,
          subtitle: !room.area_id ? "Not linked" : !areaKnown ? "Area deleted in Home Assistant" : room.name !== this.areaName(room.area_id) ? `Area: ${this.areaName(room.area_id)}` : "Linked",
          detail: room.area_id && areaKnown ? (cfg?.enabled ? "Observed by Smart Occupancy" : "Not observed by Smart Occupancy") : null,
        });
      } else {
        const l = roomLive(areaKnown ? room.area_id : null, this.ctx, store.liveStates);
        const pr = this.presenceOn() && room.area_id ? this.presenceData?.rooms.find((r) => r.area_id === room.area_id) : undefined;
        if (pr) {
          // Presence overlay: the engine's countdown (only when it reports one) and the room's warnings.
          const cd = countdown(pr, this.presenceOffset);
          const warn = pr.counts.errors + pr.counts.warnings;
          const cmp = this.shadow ? this.shadowData?.comparison.rooms.find((r) => r.area_id === room.area_id) : undefined;
          const extra = [cd ? `${cd.label} ${clock(cd.seconds)}` : `${pr.counts.active} of ${pr.counts.sensors} sensors active`, warn ? plural(warn, "warning", "warnings") : null,
            cmp && cmp.status === "disagree" ? "engines differ" : null].filter(Boolean).join(" · ");
          out.set(room.id, { tone: l.tone, title, subtitle: l.label, detail: extra || null });
        } else {
          const extra = [l.lighting, l.overrides ? plural(l.overrides, "manual override", "manual overrides") : null].filter(Boolean).join(" · ");
          out.set(room.id, { tone: l.tone, title, subtitle: l.label, detail: extra || null });
        }
      }
    }
    return out;
  }

  private placementViews(floor: Floor): PlacementView[] {
    const out: PlacementView[] = [];
    const overlay = this.presenceOn() && this.presenceData;
    const sensors = overlay ? sensorIndex(this.presenceData) : null;
    const warnings = overlay ? entityWarnings(this.presenceData) : null;
    for (const p of floor.placements) {
      const r = this.resolve(p);
      const e = r.entity;
      const view = deviceView(r.entity_id, store.liveStates.get(r.entity_id), e);
      const group = e?.group ?? "other";
      if (sensors && warnings) {
        // Presence overlay: presence sensors and contacts only, as the engine sees them.
        const uses = sensors.get(r.entity_id);
        if (!uses && group !== "presence") continue;
        const ov = uses ? overlayView(uses) : { tone: view.tone === "active" ? "idle" as DeviceTone : view.tone, text: `${view.text} · not used` };
        const warn = warnings.get(r.entity_id);
        out.push({
          id: p.id, x: p.x, y: p.y, icon: e?.icon ?? "puzzle", tone: ov.tone,
          title: `${p.label || e?.name || r.entity_id} · ${ov.text}${uses ? ` · ${uses.map((u) => u.room.name).join(", ")}` : ""}`,
          value: ov.text, badge: warn?.length || ov.tone === "unavailable" ? "!" : null,
        });
        continue;
      }
      if (this.mode === "live") {
        if (this.groups.size && !this.groups.has(group)) continue;
        if (this.problemsOnly && !["unavailable", "unknown", "missing"].includes(view.tone)) continue;
      }
      const tone = this.mode === "live" ? view.tone : r.missing ? "missing" : view.tone === "unavailable" ? "unavailable" : "placed";
      out.push({
        id: p.id,
        x: p.x,
        y: p.y,
        icon: e?.icon ?? "puzzle",
        tone,
        title: `${p.label || e?.name || r.entity_id} · ${view.text}`,
        value: this.mode === "live" ? view.text : null,
        badge: view.tone === "missing" || view.tone === "unavailable" ? "!" : view.tone === "unknown" && this.mode === "live" ? "?" : null,
      });
    }
    return out;
  }

  private renderHead(): TemplateResult {
    const dirty = this.dirty();
    const status = this.saving
      ? chip("info", "Saving…")
      : this.saveError
        ? chip("error", "Not saved")
        : !this.writable()
          ? chip("warning", "Read-only")
          : dirty
            ? chip("warning", "Unsaved changes")
            : chip("ok", this.status?.saved_at ? `Saved ${formatTime(this.status.saved_at)}` : "Saved");
    return html`<header class="fp-head">
      <div class="fp-title"><h2 tabindex="-1">Floor plan</h2><span data-testid="fp-save-status">${status}</span></div>
      <nav class="fp-modes segmented" aria-label="Floor plan mode">
        ${MODES.map((m) => html`<a href="#/floorplan/${m.key}" class=${this.mode === m.key ? "selected" : ""} aria-current=${this.mode === m.key ? "page" : nothing} title=${m.help} data-mode=${m.key}>${icon(m.icon, 18)}<span>${m.title}</span></a>`)}
      </nav>
      <div class="fp-actions">
        <button type="button" class="icon-btn" aria-label=${this.history.undoLabel ? `Undo: ${this.history.undoLabel}` : "Undo"} title=${this.history.undoLabel ? `Undo: ${this.history.undoLabel} (Ctrl+Z)` : "Nothing to undo"} ?disabled=${!this.history.canUndo || this.mode === "live"} data-testid="fp-undo" @click=${() => this.undo()}>${icon("undo")}</button>
        <button type="button" class="icon-btn" aria-label=${this.history.redoLabel ? `Redo: ${this.history.redoLabel}` : "Redo"} title=${this.history.redoLabel ? `Redo: ${this.history.redoLabel} (Ctrl+Shift+Z)` : "Nothing to redo"} ?disabled=${!this.history.canRedo || this.mode === "live"} data-testid="fp-redo" @click=${() => this.redo()}>${icon("redo")}</button>
        ${dirty ? html`<button type="button" class="btn" data-testid="fp-discard" @click=${() => (this.dialog = { kind: "confirm", title: "Discard the unsaved changes?", body: "The floor plan goes back to the last saved version.", confirm: "Discard", danger: true, action: () => void this.load() })}>${icon("close", 18)}Discard</button>` : nothing}
        <button type="button" class="btn btn-primary" data-testid="fp-save" ?disabled=${!dirty || this.saving || !this.writable()} @click=${() => void this.save()}>${icon("save", 20)}Save</button>
        <div class="fp-menu">
          <button type="button" class="icon-btn" aria-label="More actions" aria-haspopup="menu" aria-expanded=${this.menuOpen ? "true" : "false"} title="Export, import and backups" data-testid="fp-menu" @click=${() => (this.menuOpen = !this.menuOpen)}>${icon("dots-vertical")}</button>
          ${this.menuOpen
            ? html`<div class="fp-menu-list" role="menu" @click=${() => (this.menuOpen = false)}>
                <button type="button" role="menuitem" data-testid="fp-export" @click=${() => void this.exportPlan()}>${icon("download", 18)}Export (with images)</button>
                <label role="menuitem" class=${this.writable() ? "" : "disabled"} @click=${(e: Event) => e.stopPropagation()}>${icon("import", 18)}Import a file…<input type="file" accept="application/json,.json" data-testid="fp-import-file" ?disabled=${!this.writable()} @change=${(e: Event) => ((this.menuOpen = false), void this.chooseImport(e))} /></label>
                <button type="button" role="menuitem" data-testid="fp-backups" @click=${() => void this.openBackups()}>${icon("history", 18)}Backups…</button>
              </div>`
            : nothing}
        </div>
        <button type="button" class="icon-btn panel-toggle" aria-label=${this.leftOpen ? "Hide the side panel" : "Show the side panel"} aria-pressed=${this.leftOpen ? "true" : "false"} data-testid="fp-toggle-left" @click=${() => (this.leftOpen = !this.leftOpen)}>${icon("dock-left")}</button>
        <button type="button" class="icon-btn panel-toggle" aria-label=${this.inspectorOpen ? "Hide the inspector" : "Show the inspector"} aria-pressed=${this.inspectorOpen ? "true" : "false"} data-testid="fp-toggle-right" @click=${() => (this.inspectorOpen = !this.inspectorOpen)}>${icon("dock-right")}</button>
      </div>
    </header>`;
  }

  private renderNotices(): unknown {
    const items: TemplateResult[] = [];
    const st = this.status;
    if (st?.read_only) items.push(html`<p class="notice notice-warning" data-testid="fp-read-only">${icon("lock", 20)}<span>${st.read_only}</span></p>`);
    for (const n of st?.notices ?? []) items.push(html`<p class="notice notice-warning" data-testid="fp-notice-${n.code}">${icon("warning", 20)}<span>${n.message}</span></p>`);
    if (this.changedElsewhere)
      items.push(html`<p class="notice notice-warning" data-testid="fp-changed-elsewhere">${icon("sync", 20)}<span>The floor plan was saved in another browser session. Your unsaved changes are kept here.
        <button type="button" class="btn" @click=${() => void this.load()}>${icon("refresh", 18)}Load the saved version (discards yours)</button></span></p>`);
    if (this.saveError) items.push(html`<p class="notice notice-error" role="alert" data-testid="fp-save-error">${icon("error", 20)}<span>${this.saveError} Your changes are kept here.</span></p>`);
    if (this.ctxError) items.push(html`<p class="notice notice-warning">${icon("offline", 20)}<span>Home Assistant data could not be read (${this.ctxError}). Drawing works; areas, devices and live states return when the connection is back.</span></p>`);
    if (store.link === "offline") items.push(html`<p class="notice notice-error">${icon("offline", 20)}<span>The App backend cannot be reached. Your edits are kept in this page; save when the connection is back.</span></p>`);
    if (this.plan && this.ctx) {
      const renamed = this.plan.floors.flatMap((f) => f.placements).filter((p) => this.resolve(p).renamed).length;
      const areaRenamed = this.plan.floors.flatMap((f) => f.rooms).filter((r) => r.area_id && r.area_name && this.ctx!.areas.some((a) => a.id === r.area_id) && r.area_name !== this.areaName(r.area_id)).length;
      if ((renamed || areaRenamed) && this.writable() && this.mode !== "live")
        items.push(html`<p class="notice notice-info" data-testid="fp-renamed">${icon("info", 20)}<span>${renamed ? `${plural(renamed, "device icon points", "device icons point")} to an entity that was renamed in Home Assistant (found by its registry id). ` : ""}${areaRenamed ? `${plural(areaRenamed, "linked area was", "linked areas were")} renamed (the link is kept). ` : ""}
          <button type="button" class="btn" @click=${() => this.repairRenamed()}>${icon("sync", 18)}Update the plan</button></span></p>`);
    }
    if (this.notice) items.push(html`<p class="notice notice-${this.notice.tone === "error" ? "error" : this.notice.tone === "warning" ? "warning" : "info"} fp-toast" role="status" data-testid="fp-notice">${icon(this.notice.tone === "ok" ? "ok" : this.notice.tone === "error" ? "error" : "info", 20)}<span>${this.notice.text}</span></p>`);
    return items.length ? html`<div class="fp-notices">${items}</div>` : nothing;
  }

  private renderFloors(): TemplateResult {
    const floors = this.plan ? sortedFloors(this.plan) : [];
    const editable = this.mode === "build" && this.writable();
    const haUnused = (this.ctx?.floors ?? []).filter((f) => !(this.plan?.floors ?? []).some((x) => x.ha_floor_id === f.id));
    return html`<section class="panel-section" data-testid="fp-floors">
      <h3 class="panel-title">${icon("layers", 18)}Floors</h3>
      ${floors.length
        ? html`<ul class="fp-floor-list">${floors.map((f) => html`<li>
            <button type="button" class="fp-floor ${f.id === this.floorId ? "active" : ""}" aria-pressed=${f.id === this.floorId ? "true" : "false"} data-floor=${f.id} @click=${() => ((this.floorId = f.id), this.select(null))}>
              <span class="label">${f.name}</span><span class="muted small">${plural(f.rooms.length, "room", "rooms")}</span>
            </button>
            ${editable ? html`<button type="button" class="icon-btn" aria-label="Edit ${f.name}" @click=${() => this.openFloorDialog(f)}>${icon("pencil", 18)}</button>` : nothing}
          </li>`)}</ul>`
        : html`<p class="muted small">No floors yet.</p>`}
      ${editable
        ? html`<div class="palette-actions">
            <button type="button" class="btn" data-testid="fp-add-floor" @click=${() => this.openFloorDialog(null)}>${icon("plus", 18)}Add floor</button>
            ${haUnused.length ? html`<button type="button" class="btn" data-testid="fp-floors-from-ha" @click=${() => this.floorsFromHomeAssistant()}>${icon("home-assistant", 18)}Add ${plural(haUnused.length, "Home Assistant floor", "Home Assistant floors")}</button>` : nothing}
          </div>`
        : nothing}
    </section>`;
  }

  private renderBuildPanel(): TemplateResult {
    const floor = this.floor();
    const editable = this.writable();
    const north = this.plan?.building.north_deg;
    return html`
      <section class="panel-section">
        <h3 class="panel-title">${icon("pencil", 18)}Tools</h3>
        <div class="fp-tools" role="toolbar" aria-label="Drawing tools">
          ${TOOLS.map((t) => html`<button type="button" class="fp-tool ${this.tool === t.key ? "active" : ""}" aria-pressed=${this.tool === t.key ? "true" : "false"} data-tool=${t.key}
              ?disabled=${!floor || (!editable && t.key !== "select") || (t.key === "calibrate" && !floor?.rooms.length && !floor?.background)}
              title="${t.title} (${t.keyHint})" @click=${() => ((this.tool = t.key), t.key !== "select" && this.select(null))}>
            ${icon(t.icon, 20)}<span>${t.title}</span></button>`)}
        </div>
      </section>
      ${this.renderFloors()}
      ${floor
        ? html`<section class="panel-section">
            <h3 class="panel-title">${icon("image", 18)}Background image</h3>
            ${floor.background
              ? html`<div class="palette-actions">
                  <button type="button" class="btn" data-testid="fp-select-bg" @click=${() => this.select({ kind: "background", id: floor.id })}>${icon("tune", 18)}Adjust</button>
                  <button type="button" class="btn" ?disabled=${!editable} @click=${() => this.editFloor(floor.background!.visible ? "Hide background" : "Show background", (f) => (f.background!.visible = !f.background!.visible))}>${icon(floor.background.visible ? "eye-off" : "eye", 18)}${floor.background.visible ? "Hide" : "Show"}</button>
                </div>`
              : html`<p class="muted small">Import a PNG, JPEG or WebP floor plan (up to 15 MB) to trace rooms over it. It is stored in the App and shown only through Home Assistant.</p>`}
            <label class="btn file-btn ${editable ? "" : "disabled"}">${icon("upload", 18)}${floor.background ? "Replace image…" : "Import image…"}
              <input type="file" accept="image/png,image/jpeg,image/webp" data-testid="fp-bg-file" ?disabled=${!editable} @change=${(e: Event) => void this.importBackground(e)} /></label>
          </section>`
        : nothing}
      <section class="panel-section">
        <h3 class="panel-title">${icon("grid", 18)}Drawing aids</h3>
        <label class="select-field fp-field"><span>Grid</span>
          <select aria-label="Grid size" @change=${(e: Event) => ((this.grid = Number((e.target as HTMLSelectElement).value)), setPref("grid", String(this.grid)))}>
            ${GRID_STEPS.map((g) => html`<option value=${g} ?selected=${this.grid === g}>${formatLength(g, this.units())}</option>`)}
          </select></label>
        <label class="switch-row compact"><input type="checkbox" role="switch" .checked=${this.snap} @change=${(e: Event) => ((this.snap = (e.target as HTMLInputElement).checked), setPref("snap", this.snap ? "1" : "0"))} /><span>Snap to grid, corners and walls <span class="muted small block">Hold Alt to place freely</span></span></label>
        <label class="switch-row compact"><input type="checkbox" role="switch" data-testid="fp-dims" .checked=${this.showDims} @change=${(e: Event) => ((this.showDims = (e.target as HTMLInputElement).checked), setPref("dims", this.showDims ? "1" : "0"))} /><span>Show measurements</span></label>
        <label class="select-field fp-field"><span>${icon("compass", 18)}Top of the plan faces</span>
          <select aria-label="North direction" data-testid="fp-north" ?disabled=${!editable} @change=${(e: Event) => {
            const v = (e.target as HTMLSelectElement).value;
            if (this.plan) this.applyPlan({ ...this.plan, building: { ...this.plan.building, north_deg: v === "" ? null : Number(v) } }, "Set north direction");
          }}>${NORTH.map((n) => html`<option value=${n.value} ?selected=${(north === null || north === undefined ? "" : String(north)) === n.value}>${n.label}</option>`)}</select></label>
        <p class="muted small">Coordinates are metres. ${floor ? (floor.measured ? "This floor's dimensions are measured." : "This floor is a sketch until you calibrate it or type exact wall lengths.") : ""}</p>
      </section>`;
  }

  private renderDevicesTab(): TemplateResult {
    const floor = this.floor();
    const placed = this.placedIds();
    const words = normalize(this.query).split(/\s+/).filter(Boolean);
    const floorAreas = new Set((floor?.rooms ?? []).map((r) => r.area_id).filter(Boolean));
    const all = (this.ctx?.entities ?? []).filter((e) => {
      if (this.groups.size && !this.groups.has(e.group)) return false;
      if (!this.groups.size && e.group === "other" && !words.length) return false;
      if (this.hidePlaced && placed.has(e.entity_id)) return false;
      return !words.length || words.every((w) => normalize(`${e.name} ${e.entity_id} ${e.area ?? ""} ${e.device ?? ""} ${e.category_label}`).includes(w));
    });
    all.sort((a, b) => Number(floorAreas.has(b.area_id)) - Number(floorAreas.has(a.area_id)) || (a.area ?? "~").localeCompare(b.area ?? "~") || a.name.localeCompare(b.name));
    const shown = all.slice(0, this.paletteLimit);
    const editable = this.writable();
    return html`<div class="fp-palette" data-testid="fp-palette">
      <input class="panel-search" type="search" placeholder="Search devices, entity IDs, areas" aria-label="Search devices" .value=${this.query} @input=${(e: Event) => ((this.query = (e.target as HTMLInputElement).value), (this.paletteLimit = PALETTE_LIMIT))} />
      <div class="chip-choices small-chips" role="group" aria-label="Device types">
        ${GROUPS.map((g) => html`<label class=${this.groups.has(g.key) ? "selected" : ""}><input type="checkbox" .checked=${this.groups.has(g.key)} @change=${(e: Event) => {
          const next = new Set(this.groups);
          if ((e.target as HTMLInputElement).checked) next.add(g.key);
          else next.delete(g.key);
          this.groups = next;
        }} />${icon(g.icon, 16)}${g.label}</label>`)}
      </div>
      <label class="switch-row compact"><input type="checkbox" role="switch" .checked=${this.hidePlaced} @change=${(e: Event) => (this.hidePlaced = (e.target as HTMLInputElement).checked)} /><span>Only devices not on the plan</span></label>
      ${this.placing ? html`<p class="notice notice-info small" data-testid="fp-placing">${icon("tap", 18)}<span>Click or tap on the plan to place <strong>${this.placing.name}</strong>. <button type="button" class="btn" @click=${() => (this.placing = null)}>Cancel</button></span></p>` : nothing}
      ${!this.ctx ? html`<p class="muted small">Loading devices from Home Assistant…</p>` : !all.length ? html`<p class="muted small">No device matches.${!this.groups.size ? " Other entity types are listed when you search or choose “Other”." : ""}</p>` : nothing}
      <ul class="device-palette fp-device-list">
        ${shown.map((e) => {
          const where = placed.get(e.entity_id);
          return html`<li>
            <button type="button" class="palette-item ${this.placing?.entity_id === e.entity_id ? "selected" : ""}" draggable=${editable && !where ? "true" : "false"} data-entity=${e.entity_id}
              title=${where ? `On ${where.floor.name}: click to show it` : "Drag onto the plan, or click and then tap where it is"}
              @dragstart=${(ev: DragEvent) => {
                ev.dataTransfer?.setData("application/x-sl-entity", e.entity_id);
                if (ev.dataTransfer) ev.dataTransfer.effectAllowed = "copy";
              }}
              @click=${() => {
                if (where) {
                  this.floorId = where.floor.id;
                  this.select({ kind: "placement", id: where.placement.id });
                  void this.updateComplete.then(() => this.canvas()?.centreOn(where.placement));
                } else if (editable) this.placing = { entity_id: e.entity_id, name: e.name, entity_uid: e.entity_uid, device_id: e.device_id };
              }}>
              ${icon(e.icon, 20)}
              <span class="palette-text"><span class="palette-name">${e.name}</span><span class="muted small">${e.category_label}${e.area ? ` · ${e.area}` : ""}</span></span>
              ${where ? html`<span class="fp-placed" title="On the plan">${icon("check", 16)}</span>` : nothing}
              ${e.availability !== "ok" ? html`<span class="tone-warning small">${AVAILABILITY[e.availability]?.label ?? e.availability}</span>` : nothing}
            </button>
          </li>`;
        })}
      </ul>
      ${all.length > shown.length ? html`<button type="button" class="btn" @click=${() => (this.paletteLimit += PALETTE_LIMIT)}>Show ${Math.min(PALETTE_LIMIT, all.length - shown.length)} more of ${all.length - shown.length}</button>` : nothing}
    </div>`;
  }

  private renderAreasTab(): TemplateResult {
    const floor = this.floor();
    const linked = new Set(this.plan?.floors.flatMap((f) => f.rooms.map((r) => r.area_id)).filter(Boolean) as string[]);
    const unlinked = (this.ctx?.areas ?? []).filter((a) => !linked.has(a.id));
    const sel = this.selection?.kind === "room" ? floor?.rooms.find((r) => r.id === this.selection!.id) : undefined;
    const dups = this.plan ? duplicateAreaLinks(this.plan) : [];
    return html`<div class="fp-areas" data-testid="fp-areas">
      ${this.ctx && !this.ctx.areas.length ? html`<p class="notice notice-info small">${icon("info", 18)}<span>Home Assistant has no areas yet. You can still draw rooms and place devices; link the rooms when you create areas (Settings → Areas, labels & zones). Areas are never created from here.</span></p>` : nothing}
      ${dups.length ? html`<p class="notice notice-warning small">${icon("warning", 18)}<span>${dups.map((a) => this.areaName(a)).join(", ")} ${dups.length === 1 ? "is" : "are"} linked to several rooms. Select one of them to fix it.</span></p>` : nothing}
      <h4 class="fp-sub">Rooms on ${floor?.name ?? "this floor"}</h4>
      ${floor?.rooms.length
        ? html`<ul class="fp-room-list">${floor.rooms.map((r) => {
            const known = !r.area_id || !this.ctx || this.ctx.areas.some((a) => a.id === r.area_id);
            const state = !r.area_id ? chip("neutral", "Not linked") : !known ? chip("error", "Area deleted") : dups.includes(r.area_id) ? chip("warning", "Linked twice") : chip("ok", this.areaName(r.area_id));
            return html`<li><button type="button" class="fp-room-row ${this.selection?.kind === "room" && this.selection.id === r.id ? "selected" : ""}" data-room-row=${r.id} @click=${() => this.select({ kind: "room", id: r.id })}>
              <span class="label">${roomLabel(r, this.areaName)}</span>${state}</button></li>`;
          })}</ul>`
        : html`<p class="muted small">No rooms on this floor. Draw them in Build.</p>`}
      <h4 class="fp-sub">Home Assistant areas not on the plan (${unlinked.length})</h4>
      ${unlinked.length
        ? html`<ul class="fp-area-list">${unlinked.map((a) => html`<li><span>${a.name}${a.floor_id ? html` <span class="muted small">· ${this.ctx?.floors.find((f) => f.id === a.floor_id)?.name ?? a.floor_id}</span>` : nothing}</span>
            <button type="button" class="btn" data-link-area=${a.id} ?disabled=${!sel || !this.writable()} title=${sel ? `Link to ${roomLabel(sel, this.areaName)}` : "Select a drawn room first"} @click=${() => sel && this.linkArea(sel, a.id)}>${icon("link", 16)}Link</button></li>`)}</ul>`
        : html`<p class="muted small">${this.ctx?.areas.length ? "Every area is linked to a drawn room." : "—"}</p>`}
      <p class="muted small">Links use Home Assistant's stable area ids, so renaming an area keeps the link. Nothing here creates, renames or deletes areas.</p>
    </div>`;
  }

  private suggestions(): Suggestion[] {
    if (!this.plan) return [];
    return suggest(this.plan, this.ctx?.rooms ?? {}, this.areaName);
  }

  private renderRelationsTab(): TemplateResult {
    const all = this.suggestions();
    const dismissed = new Set(this.plan?.dismissed ?? []);
    const active = all.filter((s) => !dismissed.has(s.key));
    const hidden = all.filter((s) => dismissed.has(s.key));
    const writable = Boolean(this.ctx?.integration.writable);
    const list = this.showDismissed ? hidden : active;
    return html`<div class="fp-relations" data-testid="fp-relations">
      <p class="muted small">${ADJACENCY_HELP}</p>
      <p class="muted small">Suggestions are never applied automatically. “Review” opens the room in the Rooms editor with the change prepared; the integration checks it before you save.</p>
      ${!all.length ? html`<p class="muted small">No relationships found yet. Rooms that share a wall, a door or an open-plan opening appear here.</p>` : nothing}
      <ul class="fp-suggestions">
        ${list.map((s) => html`<li class="fp-suggestion strength-${s.strength}" data-suggestion=${s.key}>
          <header><strong>${s.title}</strong>${chip(s.strength === "strong" ? "info" : s.strength === "possible" ? "neutral" : "pending", s.strength === "strong" ? "Likely" : s.strength === "possible" ? "Possible" : "Note")}</header>
          <p class="small">${s.reason}</p>
          ${s.kind === "orientation" || s.kind === "not_connected"
            ? s.kind === "not_connected" ? html`<div class="row-actions">${s.areas.map((a) => html`<button type="button" class="btn" @click=${() => this.openRoomsEditor(a, "presence")}>${icon("pencil", 16)}Review ${this.areaName(a)}</button>`)}</div>` : nothing
            : s.areas.length < 2
              ? html`<p class="muted small">Link both rooms to Home Assistant areas to use this.</p>`
              : !s.changes.length && !s.unobserved.length
                ? html`<p class="small tone-ok">${icon("ok", 16)} Already adjacent in Smart Occupancy.</p>`
                : html`<div class="row-actions">${s.changes.map((c) => html`<button type="button" class="btn btn-primary" data-review-adjacent=${`${c.area_id}:${c.add}`} ?disabled=${!writable}
                      title=${writable ? "" : "The integration is not running or cannot be written"} @click=${() => this.openRoomsEditor(c.area_id, "presence", { adjacent: c.add })}>
                      ${icon("eye", 16)}Review: ${this.areaName(c.area_id)} → ${this.areaName(c.add)}</button>`)}</div>
                  ${s.unobserved.length ? html`<p class="muted small">${s.unobserved.map((a) => this.areaName(a)).join(", ")} ${s.unobserved.length === 1 ? "is" : "are"} not observed by Smart Occupancy, so adjacency is not used there.</p>` : nothing}`}
          <div class="row-actions">
            ${s.rooms.length ? html`<button type="button" class="btn" @click=${() => (this.floorId = s.floor_id, this.select({ kind: "room", id: s.rooms[0] }))}>${icon("eye", 16)}Show</button>` : nothing}
            <button type="button" class="btn" ?disabled=${!this.writable()} @click=${() => this.plan && this.applyPlan({ ...this.plan, dismissed: dismissed.has(s.key) ? this.plan.dismissed.filter((k) => k !== s.key) : [...this.plan.dismissed, s.key] }, dismissed.has(s.key) ? "Restore suggestion" : "Dismiss suggestion")}>
              ${icon(dismissed.has(s.key) ? "restore" : "close", 16)}${dismissed.has(s.key) ? "Restore" : "Dismiss"}</button>
          </div>
        </li>`)}
      </ul>
      ${hidden.length || this.showDismissed ? html`<button type="button" class="btn" @click=${() => (this.showDismissed = !this.showDismissed)}>${this.showDismissed ? "Show suggestions" : `Show dismissed (${hidden.length})`}</button>` : nothing}
    </div>`;
  }

  private renderConfigurePanel(): TemplateResult {
    const tabs: Array<{ key: SlFloorplan["leftTab"]; title: string; icon: string }> = [
      { key: "devices", title: "Devices", icon: "home" },
      { key: "areas", title: "Areas", icon: "link" },
      { key: "relations", title: "Relations", icon: "swap" },
      { key: "curtains", title: "Curtains", icon: "curtains" },
    ];
    const count = this.suggestions().filter((s) => !(this.plan?.dismissed ?? []).includes(s.key) && s.changes.length).length;
    const dangling = this.links().filter((l) => l.dangling).length;
    return html`${this.renderFloors()}
      <nav class="tabs fp-tabs fp-tabs-4" role="tablist" aria-label="Configure">
        ${tabs.map((t) => html`<button type="button" role="tab" aria-selected=${this.leftTab === t.key ? "true" : "false"} class=${this.leftTab === t.key ? "active" : ""} data-left-tab=${t.key} @click=${() => (this.leftTab = t.key)}>
          ${icon(t.icon, 18)}<span>${t.title}</span>${t.key === "relations" && count ? html`<span class="badge">${count}</span>` : nothing}${t.key === "curtains" && dangling ? html`<span class="badge">${dangling}</span>` : nothing}</button>`)}
      </nav>
      <section class="panel-section" role="tabpanel">
        ${this.leftTab === "devices" ? this.renderDevicesTab() : this.leftTab === "areas" ? this.renderAreasTab() : this.leftTab === "curtains" ? this.renderCurtainsTab() : this.renderRelationsTab()}
      </section>`;
  }

  /** Phase A5 (Configure): which window openings on this floor show a curtain window. */
  private renderCurtainsTab(): TemplateResult {
    const floor = this.floor();
    const links = this.links(floor);
    const linked = links.filter((l) => l.window);
    const unlinked = links.filter((l) => !l.opening.curtain_window_id);
    const dangling = links.filter((l) => l.dangling);
    const selectedId = this.selection?.kind === "opening" ? this.selection.id : null;
    const row = (l: LinkedWindow, state: unknown) => html`<li><button type="button" class="fp-room-row ${selectedId === l.opening.id ? "selected" : ""}" data-window-row=${l.opening.id}
        @click=${() => this.select({ kind: "opening", id: l.opening.id })}>${icon("window", 18)}<span class="label">${this.openingTitle(floor, l.opening)}</span>${state}</button></li>`;
    return html`<div class="fp-curtains" data-testid="fp-curtains-tab">
      ${this.curtainNotice()}
      <p class="muted small">Link a drawn window to a window of the curtain subsystem to see its covers on the plan and to suggest its orientation. The link is stored in the floor plan only; covers, presets and the automation stay in Curtains.</p>
      ${!floor ? nothing : !links.length
        ? html`<p class="muted small">No windows on ${floor.name}. Draw them in Build (W).</p>`
        : html`<div class="stat-tiles fp-stats">
              <div class="stat"><span class="stat-value">${linked.length}</span><span class="stat-label">linked</span></div>
              <div class="stat"><span class="stat-value">${unlinked.length}</span><span class="stat-label">not linked</span></div>
              ${dangling.length ? html`<div class="stat"><span class="stat-value tone-warning">${dangling.length}</span><span class="stat-label">need attention</span></div>` : nothing}
            </div>
            ${dangling.length ? html`<h4 class="fp-sub">Linked window no longer exists</h4><ul class="fp-room-list">${dangling.map((l) => row(l, chip("warning", "Missing")))}</ul>` : nothing}
            <h4 class="fp-sub">Linked (${linked.length})</h4>
            ${linked.length ? html`<ul class="fp-room-list">${linked.map((l) => row(l, chip(l.window!.mode === "automatic" ? "info" : "neutral", l.window!.name)))}</ul>` : html`<p class="muted small">None yet.</p>`}
            <h4 class="fp-sub">Not linked (${unlinked.length})</h4>
            ${unlinked.length ? html`<ul class="fp-room-list">${unlinked.map((l) => row(l, chip("neutral", "Not linked")))}</ul>` : html`<p class="muted small">Every window is linked.</p>`}`}
      ${this.curtainModel && !this.curtainModel.windows.length ? html`<p class="muted small">The curtain subsystem has no windows yet. <a class="text-link" href="#/curtains/windows">Set them up in Curtains</a>.</p>` : nothing}
    </div>`;
  }

  private curtainNotice(): unknown {
    if (this.curtainError) return html`<p class="notice notice-warning small" data-testid="fp-curtains-unavailable">${icon("offline", 18)}<span>Curtain data unavailable (${this.curtainError}). Links can still be set; the windows' covers and status return when the curtain subsystem is reachable. <button type="button" class="btn" @click=${() => { this.curtainError = null; void this.loadCurtains(); }}>${icon("refresh", 16)}Try again</button></span></p>`;
    if (!this.curtainModel) return html`<p class="muted small">Loading curtain windows…</p>`;
    return nothing;
  }

  /** "Window · Living (2.5 m)" */
  private openingTitle(floor: Floor | undefined, o: Opening): string {
    const wall = floor?.walls.find((w) => w.id === o.wall_id);
    const rooms = floor && wall ? wallRooms(floor, wall).map((r) => roomLabel(r, this.areaName)) : [];
    return `${OPENING_LABEL[o.type]}${rooms.length ? ` · ${rooms.join(" / ")}` : ""} · ${formatLength(o.width, this.units())}`;
  }

  /** Phase A5 (Live): the curtain windows on this floor at a glance. */
  private renderCurtainLiveSummary(): unknown {
    const floor = this.floor();
    const links = this.links(floor);
    if (!floor || !links.some((l) => l.opening.curtain_window_id)) return nothing;
    const s = curtainSummary(links, store.liveStates);
    return html`<section class="panel-section" data-testid="fp-curtain-summary">
      <h3 class="panel-title">${icon("curtains", 18)}Curtains</h3>
      ${this.curtainNotice()}
      ${this.curtainModel
        ? html`<div class="stat-tiles fp-stats">
              <div class="stat"><span class="stat-value">${s.automatic}</span><span class="stat-label">of ${s.linked} automatic</span></div>
              <div class="stat"><span class="stat-value">${s.holds}</span><span class="stat-label">manual holds</span></div>
              <div class="stat"><span class="stat-value">${s.moving}</span><span class="stat-label">moving</span></div>
              <div class="stat"><span class="stat-value ${s.unavailable || s.dangling ? "tone-warning" : ""}">${s.unavailable + s.dangling}</span><span class="stat-label">unavailable</span></div>
            </div>
            ${s.attention.length ? html`<h4 class="fp-sub">Need attention</h4><ul class="fp-problem-list">${s.attention.slice(0, 8).map((a) => html`<li><button type="button" class="fp-room-row" data-attention=${a.opening.id} @click=${() => this.select({ kind: "opening", id: a.opening.id })}>
                ${icon("curtains", 18)}<span class="label">${a.window?.name ?? this.openingTitle(floor, a.opening)}</span><span class="tone-warning small">${a.reasons[0]}</span></button></li>`)}</ul>` : html`<p class="muted small">All linked windows report normally.</p>`}`
        : nothing}
    </section>`;
  }

  /** Phase A5: the Curtains section of a window opening's inspector (Configure: the link and the orientation; Live: the covers now). */
  private renderOpeningCurtains(floor: Floor, o: Opening): unknown {
    if (o.type !== "window") return nothing;
    const { window, dangling } = this.curtainWindow(o);
    const wall = floor.walls.find((w) => w.id === o.wall_id);
    const wallAreas = wall ? wallRooms(floor, wall).map((r) => r.area_id) : [];
    const editable = this.writable() && this.mode === "configure";
    const link = window ? html`<a class="btn" data-testid="fp-open-curtain" href=${`#/curtains/windows/${encodeURIComponent(window.window_id)}`}>${icon("curtains", 16)}Open ${window.name} in Curtains</a>` : nothing;
    if (this.mode === "live") {
      if (!o.curtain_window_id) return html`<section class="inspector-section" data-testid="fp-window-curtains"><h4>Curtains</h4><p class="muted small">Not linked to a curtain window. Link it under Configure → Curtains.</p></section>`;
      if (this.curtainError || !this.curtainModel) return html`<section class="inspector-section" data-testid="fp-window-curtains"><h4>Curtains</h4>${this.curtainNotice()}</section>`;
      if (!window) return html`<section class="inspector-section" data-testid="fp-window-curtains"><h4>Curtains</h4><p class="notice notice-warning small">${icon("warning", 18)}<span>The linked curtain window (${o.curtain_window_id}) no longer exists. Unlink or relink it under Configure → Curtains.</span></p></section>`;
      const cover = coverView(window, store.liveStates);
      const auto = window.auto;
      const sun = auto?.environment?.sun;
      const glare = auto?.environment?.glare;
      const attention = windowAttention(window, store.liveStates);
      const hold = auto?.hold as { policy_word?: string; reason?: string; until?: string | null } | null | undefined;
      return html`<section class="inspector-section" data-testid="fp-window-curtains">
        <h4>${window.name} <span class="muted small">· ${window.area_name ?? "no room"}</span></h4>
        <p>${chip(window.mode === "automatic" ? "ok" : window.mode === "observing" ? "info" : "neutral", MODE_WORD[window.mode])} <span class="small">${cover.text}${window.current?.text && window.current.status === "preset" ? ` · ${window.current.text}` : ""}</span></p>
        <ul class="plain-list small fp-cover-list">${window.layers.map((l) => {
          const t = layerText(l, store.liveStates);
          return html`<li><span>${l.kind_word ?? l.kind} <strong>${l.name}</strong></span><span class=${t.tone === "unavailable" ? "tone-warning" : t.tone === "moving" ? "tone-info" : "muted"}>${t.text}</span></li>`;
        })}</ul>
        ${kvRows([
          ["Position", window.current?.text ?? null],
          ["Automatic", auto?.status?.word ? html`<strong>${auto.status.word}</strong>${auto.status.text ? html` <span class="muted">${auto.status.text}</span>` : nothing}` : window.mode === "automatic" ? "Waiting for the first evaluation" : "Not automatic"],
          ["Condition", auto?.status?.condition ? ((this.curtainModel.words?.conditions ?? {})[auto.status.condition] ?? auto.status.condition) : null],
          ["Manual hold", hold ? `${hold.policy_word ?? "Held"}${hold.reason ? ` · ${hold.reason}` : ""}` : "None"],
          ["Sun", sun?.text ?? window.exposure?.text ?? null],
          ["Glare", glare?.enabled ? (glare.active ? "Glare protection active" : "Watching") : null],
          ["Orientation", bearingText(window.effective_bearing, window.compass)],
        ])}
        ${attention.length ? html`<ul class="plain-list small">${attention.map((t) => html`<li class="tone-warning">${icon("warning", 14)} ${t}</li>`)}</ul>` : nothing}
        ${link}
        <p class="muted small">From the curtain subsystem (read-only here). The bar on the plan shows the covered share of the window.</p>
      </section>`;
    }
    // Configure: the link and the orientation suggestion.
    const groups = this.curtainModel ? groupedWindows(this.curtainModel, wallAreas) : [];
    const orientation = openingOrientation(floor, o, this.plan?.building.north_deg ?? null);
    const diff = window && orientation.bearing !== null && window.effective_bearing !== null ? bearingDifference(orientation.bearing, window.effective_bearing) : null;
    return html`<section class="inspector-section" data-testid="fp-window-curtains">
      <h4>Curtains</h4>
      ${this.curtainNotice()}
      <label class="field"><span class="field-label">Curtain window</span>
        <select data-testid="fp-curtain-select" ?disabled=${!editable} .value=${liveValue(o.curtain_window_id ?? "")} @change=${(e: Event) => this.linkCurtain(o, (e.target as HTMLSelectElement).value || null)}>
          <option value="" ?selected=${!o.curtain_window_id}>Not linked</option>
          ${groups.map((g) => html`<optgroup label=${`${g.name}${wallAreas.includes(g.area_id) ? " (this wall)" : ""}`}>${g.windows.map((w) => html`<option value=${w.window_id} ?selected=${o.curtain_window_id === w.window_id}>${w.name}${w.compass ? ` · ${w.compass}` : ""}</option>`)}</optgroup>`)}
          ${dangling ? html`<option value=${o.curtain_window_id!} selected>${o.curtain_window_id} (no longer exists)</option>` : nothing}
          ${!this.curtainModel && o.curtain_window_id ? html`<option value=${o.curtain_window_id} selected>${o.curtain_window_id}</option>` : nothing}
        </select></label>
      ${dangling ? html`<p class="notice notice-warning small" data-testid="fp-curtain-dangling">${icon("warning", 18)}<span>This curtain window no longer exists. Choose another or unlink it.
          ${editable ? html`<button type="button" class="btn" data-testid="fp-curtain-unlink" @click=${() => this.linkCurtain(o, null)}>${icon("link-off", 16)}Unlink</button>` : nothing}</span></p>` : nothing}
      ${window ? html`<p class="small">${chip(window.mode === "automatic" ? "ok" : window.mode === "observing" ? "info" : "neutral", MODE_WORD[window.mode])} ${plural(window.layers.length, "cover", "covers")}${window.area_name ? ` · ${window.area_name}` : ""}</p>${link}` : nothing}
      ${this.curtainModel && !this.curtainModel.windows.length ? html`<p class="muted small">The curtain subsystem has no windows yet. <a class="text-link" href="#/curtains/windows">Set them up in Curtains</a>.</p>` : nothing}
      <h4 class="section-title">Orientation</h4>
      ${orientation.status === "no_north"
        ? html`<p class="small muted" data-testid="fp-orientation">Set north first (Build → Drawing aids → “Top of the plan faces”) to suggest the direction this window faces.</p>`
        : orientation.status === "shared_wall"
          ? html`<p class="small muted" data-testid="fp-orientation">This wall is shared between ${orientation.rooms.map((id) => roomLabel(floor.rooms.find((r) => r.id === id)!, this.areaName)).join(" and ")}, so it has no single outside: no orientation is suggested.</p>`
          : orientation.status !== "ok"
            ? html`<p class="small muted" data-testid="fp-orientation">The wall of this window is not part of a room, so no orientation can be suggested.</p>`
            : html`<dl class="kv" data-testid="fp-orientation">
                <dt>Suggested</dt><dd><strong data-testid="fp-orientation-suggested">${bearingText(orientation.bearing, orientation.compass)}</strong></dd>
                <dt>In Curtains</dt><dd>${window ? bearingText(window.effective_bearing, window.compass) : o.curtain_window_id ? "—" : "Link a curtain window first"}</dd>
              </dl>
              ${window && diff !== null && diff < 1 ? html`<p class="small tone-ok">${icon("ok", 16)} The curtain window already faces this way.</p>` : nothing}
              ${window && (diff === null || diff >= 1)
                ? html`<button type="button" class="btn btn-primary" data-testid="fp-use-orientation" ?disabled=${!this.curtainModel?.can_edit} title=${this.curtainModel?.can_edit ? "" : "Only administrators can change the curtain configuration"}
                    @click=${() => this.openBearingDialog(window, orientation.bearing!, orientation.compass!)}>${icon("compass", 16)}Use this orientation…</button>`
                : nothing}`}
      <p class="muted small">A 2D plan gives the direction a window faces, not its sun exposure: obstructions, the horizon and neighbouring buildings are set in Curtains. Nothing is applied without your confirmation.</p>
    </section>`;
  }

  private renderLivePanel(): TemplateResult {
    const floor = this.floor();
    const rooms = floor?.rooms ?? [];
    const lives = rooms.map((r) => roomLive(r.area_id, this.ctx, store.liveStates));
    const devices = (floor?.placements ?? []).map((p) => {
      const r = this.resolve(p);
      return { p, r, v: deviceView(r.entity_id, store.liveStates.get(r.entity_id), r.entity) };
    });
    const occupied = lives.filter((l) => l.tone === "occupied" || l.tone === "possible").length;
    const lightsOn = devices.filter((d) => d.r.entity_id.startsWith("light.") && d.v.tone === "active").length;
    const problems = devices.filter((d) => ["unavailable", "missing", "unknown"].includes(d.v.tone));
    const roomTones: RoomTone[] = ["occupied", "possible", "vacating", "vacant", "uncertain", "unavailable", "not_observed", "not_linked"];
    const deviceTones: DeviceTone[] = ["active", "idle", "off", "unknown", "unavailable", "missing"];
    return html`${this.renderFloors()}
      <section class="panel-section" data-testid="fp-presence-overlay">
        <label class="switch-row compact"><input type="checkbox" role="switch" data-testid="fp-presence-toggle" .checked=${this.presence} @change=${(e: Event) => this.setPresence((e.target as HTMLInputElement).checked)} />
          <span>Presence overlay<span class="small muted block">Occupancy decisions, sensors as the engine sees them, warnings</span></span></label>
        ${this.presence ? this.renderPresenceSummary(rooms.map((r) => r.area_id)) : nothing}
        ${this.presence ? html`<label class="switch-row compact"><input type="checkbox" role="switch" data-testid="fp-shadow-toggle" .checked=${this.shadow}
            @change=${(e: Event) => { this.shadow = (e.target as HTMLInputElement).checked; setPref("shadow", this.shadow ? "1" : "0"); void this.loadPresence(); }} />
          <span>Engine comparison<span class="small muted block">Mark rooms where the App's engine and the integration's differ (diagnostics only)</span></span></label>` : nothing}
      </section>
      <section class="panel-section" data-testid="fp-live-summary">
        <h3 class="panel-title">${icon("eye", 18)}Now on ${floor?.name ?? "this floor"}</h3>
        <div class="stat-tiles fp-stats">
          <div class="stat"><span class="stat-value">${occupied}</span><span class="stat-label">of ${rooms.length} rooms occupied</span></div>
          <div class="stat"><span class="stat-value">${lightsOn}</span><span class="stat-label">lights on</span></div>
          <div class="stat"><span class="stat-value ${problems.length ? "tone-warning" : ""}">${problems.length}</span><span class="stat-label">devices need attention</span></div>
        </div>
        ${problems.length ? html`<ul class="fp-problem-list">${problems.slice(0, 8).map((d) => html`<li><button type="button" class="fp-room-row" @click=${() => this.select({ kind: "placement", id: d.p.id })}>${icon(d.r.entity?.icon ?? "puzzle", 18)}<span class="label">${d.p.label || d.r.entity?.name || d.r.entity_id}</span><span class="tone-${d.v.tone === "unknown" ? "neutral" : "warning"} small">${d.v.text}</span></button></li>`)}</ul>` : nothing}
      </section>
      ${this.renderCurtainLiveSummary()}
      <section class="panel-section">
        <h3 class="panel-title">${icon("filter", 18)}Show</h3>
        <div class="chip-choices small-chips" role="group" aria-label="Device types">
          ${GROUPS.map((g) => html`<label class=${this.groups.has(g.key) ? "selected" : ""}><input type="checkbox" .checked=${this.groups.has(g.key)} @change=${(e: Event) => {
            const next = new Set(this.groups);
            if ((e.target as HTMLInputElement).checked) next.add(g.key);
            else next.delete(g.key);
            this.groups = next;
          }} />${icon(g.icon, 16)}${g.label}</label>`)}
        </div>
        <label class="switch-row compact"><input type="checkbox" role="switch" data-testid="fp-problems-only" .checked=${this.problemsOnly} @change=${(e: Event) => (this.problemsOnly = (e.target as HTMLInputElement).checked)} /><span>Only devices that need attention</span></label>
        <label class="switch-row compact"><input type="checkbox" role="switch" .checked=${this.showValues} @change=${(e: Event) => ((this.showValues = (e.target as HTMLInputElement).checked), setPref("values", this.showValues ? "1" : "0"))} /><span>Show values under the icons</span></label>
      </section>
      <section class="panel-section fp-legend" data-testid="fp-legend">
        <h3 class="panel-title">${icon("info", 18)}Legend</h3>
        <ul>${roomTones.map((t) => html`<li><span class="fp-swatch room tone-${t}"></span>${ROOM_TONE_LABEL[t]}</li>`)}</ul>
        <ul>${deviceTones.map((t) => html`<li><span class="fp-swatch device tone-${t}"></span>${DEVICE_TONE_LABEL[t]}</li>`)}</ul>
        <p class="muted small">Occupancy comes from the Smart Occupancy integration's own room states. Icons are where you placed the devices: they are not measured positions, people's locations or sensor coverage. No sensor coverage is drawn: the integration does not know it. Radar distance zones are listed in a room's Presence section (configured band and measured distance).</p>
        <ul><li><span class="fp-swatch curtain"></span>Curtain bar on a linked window: the covered share (dashed grey: position unknown or unavailable)</li></ul>
      </section>`;
  }

  private renderPresenceSummary(areaIds: Array<string | null>): TemplateResult {
    const data = this.presenceData;
    if (this.presenceError) return html`<p class="notice notice-warning small">${icon("warning", 18)}<span>${this.presenceError}</span></p>`;
    if (!data) return html`<p class="muted small">Loading presence…</p>`;
    const rooms = data.rooms.filter((r) => areaIds.includes(r.area_id));
    const warnings = [...entityWarnings(data).entries()].filter(([, list]) => list.some((d) => areaIds.includes(d.area_id ?? null)));
    return html`<div data-testid="fp-presence-summary">
      <ul class="fp-problem-list">${rooms.map((r) => {
        const cd = countdown(r, this.presenceOffset);
        return html`<li><button type="button" class="fp-room-row" @click=${() => this.focusArea(r.area_id)}>${stateChip(r.occupancy?.state, r.occupancy?.state_label)}
          <span class="label">${r.name}</span><span class="small muted">${cd ? `${cd.label} ${clock(cd.seconds)}` : ""}</span></button></li>`;
      })}</ul>
      ${warnings.length ? html`<h4 class="section-title">Sensor warnings</h4><ul class="fp-problem-list">${warnings.slice(0, 8).map(([id, list]) => html`<li>
          <a class="fp-room-row" href=${`#/presence/${encodeURIComponent(list[0].area_id ?? "")}?sensor=${encodeURIComponent(id)}`}>${icon("warning", 18)}<span class="label">${list[0].title}</span></a></li>`)}</ul>` : nothing}
      <p class="muted small">From the Smart Occupancy integration's engine. Icons show where you placed sensors, not what they cover; no person positions are drawn.</p>
    </div>`;
  }

  private presenceRoomSection(areaId: string | null): unknown {
    if (!this.presenceOn() || !areaId) return nothing;
    const pr = this.presenceData?.rooms.find((r) => r.area_id === areaId);
    if (!pr) return html`<section class="inspector-section" data-testid="fp-room-presence"><h4>Presence</h4><p class="muted small">This area has no room configuration in Smart Occupancy.</p>
      <a class="btn" href=${`#/rooms/${encodeURIComponent(areaId)}/presence`}>${icon("floor-plan", 16)}Set it up in Rooms</a></section>`;
    const cd = countdown(pr, this.presenceOffset);
    const radar = pr.zones.filter((z) => z.detection === "radar" && z.radar);
    return html`<section class="inspector-section" data-testid="fp-room-presence"><h4>Presence</h4>
      <p>${stateChip(pr.occupancy?.state, pr.occupancy?.state_label)} ${cd ? html`<span class="small">${cd.label} <strong class="mono">${clock(cd.seconds)}</strong></span>` : nothing}</p>
      ${pr.occupancy ? html`<p class="small">${pr.occupancy.reason}</p>` : nothing}
      ${this.shadowRoom(areaId)}
      <p class="small muted">${pr.counts.active} of ${pr.counts.sensors} sensors active${pr.counts.unavailable ? ` · ${pr.counts.unavailable} unavailable` : ""}${pr.counts.errors + pr.counts.warnings ? ` · ${plural(pr.counts.errors + pr.counts.warnings, "warning", "warnings")}` : ""}</p>
      ${radar.length ? html`<h4 class="section-title">Radar distance zones</h4><ul class="plain-list small">${radar.map((z) => html`<li><strong>${z.name}</strong>: configured ${z.band}${z.live_radar ? ` · measured ${z.live_radar.distance_valid ? z.live_radar.distance_text : z.live_radar.distance_text ?? "no valid value"}` : ""} · ${z.active ? "active" : "inactive"}</li>`)}</ul>
        <p class="muted small">A radar reports a distance, not a direction: the band is every point at that distance it can see. Nothing is drawn for it on the plan.</p>` : nothing}
      <a class="btn btn-primary" data-testid="fp-open-presence" href=${`#/presence/${encodeURIComponent(areaId)}`}>${icon("motion-sensor", 16)}Open the Presence workspace</a>
    </section>`;
  }

  /** Phase A3.2: the shadow comparison of one room (only with the "Engine comparison" switch). */
  private shadowRoom(areaId: string): unknown {
    if (!this.shadow) return nothing;
    const cmp = this.shadowData?.comparison.rooms.find((r) => r.area_id === areaId);
    if (!cmp) return html`<p class="small muted" data-testid="fp-shadow">Engine comparison: not available for this room.</p>`;
    const label = cmp.status === "agree" ? "both engines agree" : cmp.status === "disagree" ? `the engines differ (${cmp.open_for_s} s)` : cmp.status === "settling" ? "settling" : "not known yet";
    return html`<p class="small" data-testid="fp-shadow">App engine: ${stateChip(cmp.app?.state)} <span class=${cmp.status === "disagree" ? "tone-error" : "muted"}>${label}</span>
      <a class="text-link" href="#/presence/engine">Engine migration</a></p>`;
  }

  private presenceSensorSection(entityId: string): unknown {
    if (!this.presenceOn() || !this.presenceData) return nothing;
    const uses = sensorIndex(this.presenceData).get(entityId);
    const warn = entityWarnings(this.presenceData).get(entityId) ?? [];
    if (!uses) return html`<section class="inspector-section" data-testid="fp-sensor-presence"><h4>Presence</h4><p class="small muted">Not used for occupancy by any room.</p></section>`;
    return html`<section class="inspector-section" data-testid="fp-sensor-presence"><h4>Presence</h4>
      ${uses.map((u) => html`<p class="small"><strong>${u.room.name}</strong>: ${u.signal.kind_name} · ${u.signal.role_name.split(":")[0]}${u.signal.inverted ? " · inverted" : ""} — ${overlayView([u]).text}</p>
        <a class="btn" data-testid="fp-open-sensor" href=${`#/presence/${encodeURIComponent(u.room.area_id)}?sensor=${encodeURIComponent(entityId)}`}>${icon("motion-sensor", 16)}Open in the Presence inspector</a>`)}
      ${warn.length ? html`<ul class="plain-list small">${warn.map((d) => html`<li class="tone-${d.level}">${d.title}</li>`)}</ul>` : nothing}
    </section>`;
  }

  // ------------------------------------------------------------ inspector
  private inspectorShell(title: string, iconName: string, body: unknown, subtitle?: unknown): TemplateResult {
    return html`<aside class="fp-inspector inspector" aria-label="Inspector" data-testid="fp-inspector">
      <header class="inspector-head">
        <span class="inspector-icon">${icon(iconName, 24)}</span>
        <div><h3>${title}</h3>${subtitle ?? nothing}</div>
        <button type="button" class="icon-btn" aria-label="Close the inspector" @click=${() => (this.inspectorOpen = false)}>${icon("close")}</button>
      </header>
      ${body}
    </aside>`;
  }

  private numberField(label: string, value: number, apply: (v: number) => void, opts: { step?: number; min?: number; max?: number; unit?: string; testid?: string } = {}): TemplateResult {
    return html`<label class="field fp-num"><span class="field-label">${label}</span>
      <span class="fp-num-row"><input type="number" inputmode="decimal" step=${opts.step ?? 0.01} min=${opts.min ?? nothing} max=${opts.max ?? nothing} .value=${String(Math.round(value * 1000) / 1000)} data-testid=${opts.testid ?? nothing} ?disabled=${!this.writable() || this.mode === "live"}
        @change=${(e: Event) => {
          const v = Number((e.target as HTMLInputElement).value);
          if (Number.isFinite(v)) apply(v);
        }} />${opts.unit ? html`<span class="muted small">${opts.unit}</span>` : nothing}</span></label>`;
  }

  private renderInspector(): unknown {
    if (!this.inspectorOpen) return nothing;
    const floor = this.floor();
    const sel = this.selection;
    if (!floor || !sel) return this.renderFloorInspector(floor);
    switch (sel.kind) {
      case "room": {
        const room = floor.rooms.find((r) => r.id === sel.id);
        return room ? this.renderRoomInspector(floor, room) : nothing;
      }
      case "wall":
        return this.renderWallInspector(floor, sel.id);
      case "vertex":
        return this.renderVertexInspector(floor, sel.id);
      case "opening":
        return this.renderOpeningInspector(floor, sel.id);
      case "placement": {
        const p = floor.placements.find((x) => x.id === sel.id);
        return p ? this.renderPlacementInspector(floor, p) : nothing;
      }
      case "background":
        return this.renderBackgroundInspector(floor);
    }
  }

  private renderFloorInspector(floor: Floor | undefined): TemplateResult {
    if (!floor) {
      return this.inspectorShell("Floor plan", "home-map-marker", html`<section class="inspector-section">
        <p>Draw your home to link rooms to Home Assistant areas and place devices visually. The floor plan is optional: the Rooms editor works without it.</p>
        ${this.writable() ? html`<button type="button" class="btn btn-primary" data-testid="fp-first-floor" @click=${() => (this.mode === "build" ? this.openFloorDialog(null) : navigatePath("floorplan", "build"))}>${icon("plus", 18)}Add the first floor</button>` : nothing}
      </section>`);
    }
    const devices = floor.placements.length;
    return this.inspectorShell(floor.name, "layers", html`<section class="inspector-section">
        ${this.mode === "build"
          ? html`<p class="muted small">Select a room, wall, corner, door or window to edit it. Drag a selected room to move it; drag a wall to move it sideways; double-click a wall to add a corner.</p>`
          : this.mode === "configure"
            ? html`<p class="muted small">Select a room to link its area. Drag devices from the list onto the plan, or click one and then tap where it is. Placing an icon changes nothing in Home Assistant or in the automation.</p>`
            : html`<p class="muted small">Select a room or a device to see its live state. Live mode is read-only.</p>`}
        <dl class="kv">
          <dt>Rooms</dt><dd>${floor.rooms.length} (${floor.rooms.filter((r) => r.area_id).length} linked)</dd>
          <dt>Doors and windows</dt><dd>${floor.openings.length}</dd>
          <dt>Device icons</dt><dd>${devices}</dd>
          <dt>Dimensions</dt><dd>${floor.measured ? "Measured" : "Sketch"}</dd>
          <dt>Home Assistant floor</dt><dd>${floor.ha_floor_id ? (this.ctx?.floors.find((f) => f.id === floor.ha_floor_id)?.name ?? floor.ha_floor_id) : "—"}</dd>
        </dl>
        ${this.mode === "build" && this.writable()
          ? html`<div class="row-actions">
              <button type="button" class="btn" @click=${() => this.openFloorDialog(floor)}>${icon("pencil", 18)}Edit floor</button>
              <label class="switch-row compact"><input type="checkbox" role="switch" .checked=${floor.measured} @change=${(e: Event) => this.editFloor("Dimensions", (f) => (f.measured = (e.target as HTMLInputElement).checked))} /><span>Dimensions are measured</span></label>
              <button type="button" class="btn btn-danger" data-testid="fp-delete-floor" @click=${() => this.deleteFloor(floor)}>${icon("delete", 18)}Delete floor…</button>
            </div>`
          : nothing}
      </section>`);
  }

  private renderRoomInspector(floor: Floor, room: Room): TemplateResult {
    const map = vertexMap(floor);
    const pts = roomPoints(floor, room, map);
    const units = this.units();
    const editable = this.writable() && this.mode !== "live";
    const known = !room.area_id || !this.ctx || this.ctx.areas.some((a) => a.id === room.area_id);
    const cfg = room.area_id ? this.ctx?.rooms[room.area_id] : undefined;
    const inside = floor.placements.filter((p) => roomAt(floor, p)?.id === room.id);
    const linkedElsewhere = new Map<string, string>();
    for (const f of this.plan?.floors ?? []) for (const r of f.rooms) if (r.area_id && r.id !== room.id) linkedElsewhere.set(r.area_id, roomLabel(r, this.areaName));
    const l = this.mode === "live" ? roomLive(known ? room.area_id : null, this.ctx, store.liveStates) : null;
    const body = html`
      ${this.mode !== "live"
        ? html`<section class="inspector-section">
            <label class="field"><span class="field-label">Name on the plan</span>
              <input type="text" maxlength="100" data-testid="fp-room-name" .value=${room.name} placeholder=${room.area_id ? this.areaName(room.area_id) : "Room name"} ?disabled=${!editable}
                @change=${(e: Event) => this.editFloor("Rename room", (f) => (f.rooms.find((r) => r.id === room.id)!.name = (e.target as HTMLInputElement).value.trim().slice(0, 100)))} /></label>
          </section>`
        : nothing}
      ${l
        ? html`<section class="inspector-section" data-testid="fp-room-live">
            <h4>Now</h4>
            <p>${chip(l.tone === "occupied" ? "ok" : l.tone === "possible" || l.tone === "vacating" ? "info" : l.tone === "unavailable" ? "error" : "neutral", l.label)}</p>
            ${kvRows([["Why", l.reason], ["Lighting", l.lighting ?? "Not reported"], ["Manual overrides", l.overrides === null ? "Not reported by the integration" : String(l.overrides)]])}
            <p class="muted small">From the Smart Occupancy integration (read-only).</p>
          </section>`
        : nothing}
      ${this.presenceRoomSection(known ? room.area_id : null)}
      <section class="inspector-section" data-testid="fp-room-area">
        <h4>Home Assistant area</h4>
        ${this.mode === "configure" && editable
          ? html`<label class="field"><span class="field-label">Linked area</span>
              <select data-testid="fp-area-select" .value=${liveValue(room.area_id ?? "")} @change=${(e: Event) => this.linkArea(room, (e.target as HTMLSelectElement).value || null)}>
                <option value="" ?selected=${!room.area_id}>Not linked</option>
                ${(this.ctx?.areas ?? []).map((a) => html`<option value=${a.id} ?selected=${room.area_id === a.id}>${a.name}${linkedElsewhere.has(a.id) ? ` (linked to ${linkedElsewhere.get(a.id)})` : ""}</option>`)}
                ${room.area_id && !known ? html`<option value=${room.area_id} selected>${room.area_name ?? room.area_id} (deleted)</option>` : nothing}
              </select></label>`
          : html`<p>${room.area_id ? (known ? this.areaName(room.area_id) : `${room.area_name ?? room.area_id} (deleted in Home Assistant)`) : "Not linked"}</p>`}
        ${room.area_id && !known ? html`<p class="notice notice-warning small">${icon("warning", 18)}<span>This area no longer exists in Home Assistant. Link the room to another area, or unlink it. The drawing is kept.</span></p>` : nothing}
        ${room.area_id && known && room.area_name && room.area_name !== this.areaName(room.area_id) ? html`<p class="muted small">Renamed in Home Assistant (was “${room.area_name}”). The link is kept.</p>` : nothing}
        ${room.shares_area ? html`<p class="small">${chip("info", "Shared space")} This area is shared with other drawn rooms (open plan).</p>` : nothing}
        ${room.shares_area && editable && this.mode === "configure" ? html`<button type="button" class="btn" @click=${() => this.editFloor("Separate from shared space", (f) => {
          const r = f.rooms.find((x) => x.id === room.id)!;
          r.shares_area = false;
          r.area_id = null;
          r.area_name = null;
        })}>${icon("link-off", 16)}Unlink this part</button>` : nothing}
        ${room.area_id && known
          ? html`<p class="small">${cfg?.configured ? (cfg.enabled ? chip("ok", "Observed by Smart Occupancy") : chip("pending", "Configured, not observed")) : chip("neutral", "Not configured in Smart Occupancy")}
              ${cfg?.lighting ? chip(cfg.lighting === "automatic" ? "info" : "neutral", cfg.lighting === "automatic" ? "Lighting: Automatic" : "Lighting: Observing") : nothing}</p>
            <div class="row-actions"><button type="button" class="btn" data-testid="fp-open-room" @click=${() => this.openRoomsEditor(room.area_id!, "overview")}>${icon("pencil", 16)}Open in the Rooms editor</button></div>`
          : nothing}
      </section>
      ${this.mode === "build"
        ? html`<section class="inspector-section">
            <h4>Measurements ${floor.measured ? "" : html`<span class="muted small">(sketch)</span>`}</h4>
            ${kvRows([["Floor area", formatArea(polygonArea(pts), units)], ["Perimeter", formatLength(perimeter(pts), units)], ["Corners", String(pts.length)]])}
            ${editable ? html`<button type="button" class="btn btn-danger" data-testid="fp-delete-room" @click=${() => this.deleteSelection()}>${icon("delete", 18)}Delete room…</button>` : nothing}
          </section>`
        : html`<section class="inspector-section">
            <h4>Devices placed here (${inside.length})</h4>
            ${inside.length ? html`<ul class="fp-inside">${inside.map((p) => {
              const r = this.resolve(p);
              const v = deviceView(r.entity_id, store.liveStates.get(r.entity_id), r.entity);
              return html`<li><button type="button" class="fp-room-row" @click=${() => this.select({ kind: "placement", id: p.id })}>${icon(r.entity?.icon ?? "puzzle", 18)}<span class="label">${p.label || r.entity?.name || r.entity_id}</span><span class="muted small">${v.text}</span></button></li>`;
            })}</ul>` : html`<p class="muted small">None yet.</p>`}
          </section>`}`;
    return this.inspectorShell(roomLabel(room, this.areaName), "floor-plan", body, html`<span class="muted small">Room on ${floor.name}</span>`);
  }

  private renderWallInspector(floor: Floor, wallId: string): unknown {
    const wall = floor.walls.find((w) => w.id === wallId);
    if (!wall) return nothing;
    const map = vertexMap(floor);
    const length = wallLength(floor, wall, map);
    const rooms = wallRooms(floor, wall);
    const chain = wallChain(floor, wallId).length;
    const editable = this.writable();
    const ends = wallEnds(floor, wall, map)!;
    const mid = { x: (ends[0].x + ends[1].x) / 2, y: (ends[0].y + ends[1].y) / 2 };
    const openings = floor.openings.filter((o) => o.wall_id === wallId);
    return this.inspectorShell("Wall", "wall", html`<section class="inspector-section">
        ${kvRows([["Rooms", rooms.map((r) => roomLabel(r, this.areaName)).join(" · ") || "—"], ["Type", rooms.length > 1 ? "Shared wall" : "Outside wall"], ["Doors and windows", String(openings.length)]])}
        <label class="field"><span class="field-label">Length</span>
          <input type="text" inputmode="decimal" data-testid="fp-wall-length" .value=${formatLength(length, this.units())} ?disabled=${!editable}
            @change=${(e: Event) => {
              const v = parseLength((e.target as HTMLInputElement).value.replace(/\s*m$/, ""), this.units());
              if (v === null) return this.flash("Enter a length, for example 3.20", "warning");
              const r = setWallLength(floor, wallId, v);
              if (r.ok) this.applyFloor(r.value, "Set wall length");
              else this.flash(r.error, "warning");
            }} /><span class="muted small">Moves the wall's end corner along the wall.</span></label>
        <fieldset class="segmented fp-chain" aria-label="When dragged">
          <legend class="visually-hidden">When dragged</legend>
          <label class=${this.chain ? "selected" : ""}><input type="radio" name="fp-chain" .checked=${this.chain} @change=${() => (this.chain = true)} />Whole straight wall${chain > 2 ? ` (${chain - 1} parts)` : ""}</label>
          <label class=${!this.chain ? "selected" : ""}><input type="radio" name="fp-chain" .checked=${!this.chain} @change=${() => (this.chain = false)} />This part only</label>
        </fieldset>
        <p class="muted small">Drag the wall to move it sideways (Shift: this part only). Rooms on both sides follow.</p>
        ${editable
          ? html`<div class="row-actions">
              <button type="button" class="btn" data-testid="fp-add-corner" @click=${() => {
                const r = insertVertex(floor, wallId, mid);
                if (r.ok) this.applyFloor(r.value.floor, "Add corner", { kind: "vertex", id: r.value.vertexId });
                else this.flash(r.error, "warning");
              }}>${icon("vertex", 16)}Add a corner in the middle</button>
              ${(["door", "window", "opening"] as OpeningType[]).map((t) => html`<button type="button" class="btn" @click=${() => (this.tool = t)}>${icon(t === "opening" ? "open-plan" : t, 16)}${OPENING_LABEL[t]}</button>`)}
            </div>`
          : nothing}
      </section>`);
  }

  private renderVertexInspector(floor: Floor, vertexId: string): unknown {
    const v = floor.vertices.find((x) => x.id === vertexId);
    if (!v) return nothing;
    const rooms = floor.rooms.filter((r) => r.vertices.includes(vertexId));
    const move = (to: Pt) => {
      const r = moveVertex(floor, vertexId, to);
      if (r.ok) this.applyFloor(r.value, "Move corner");
      else this.flash(r.error, "warning");
    };
    return this.inspectorShell("Corner", "vertex", html`<section class="inspector-section">
        ${kvRows([["Rooms", rooms.map((r) => roomLabel(r, this.areaName)).join(" · ")]])}
        <div class="fp-xy">
          ${this.numberField("x", v.x, (x) => move({ x, y: v.y }), { unit: "m", testid: "fp-vertex-x" })}
          ${this.numberField("y", v.y, (y) => move({ x: v.x, y }), { unit: "m", testid: "fp-vertex-y" })}
        </div>
        <p class="muted small">Drag the corner to reshape every room that shares it. Arrow keys move it by one grid step.</p>
        ${this.writable() ? html`<button type="button" class="btn btn-danger" data-testid="fp-delete-corner" @click=${() => this.deleteSelection()}>${icon("delete", 18)}Delete corner</button>` : nothing}
      </section>`);
  }

  private renderOpeningInspector(floor: Floor, id: string): unknown {
    const o = floor.openings.find((x) => x.id === id);
    if (!o) return nothing;
    const wall = floor.walls.find((w) => w.id === o.wall_id);
    const length = wall ? wallLength(floor, wall) : 0;
    const editable = this.writable();
    const patch = (p: Partial<typeof o>, label: string) => {
      const r = updateOpening(floor, id, p);
      if (r.ok) this.applyFloor(r.value, label);
      else this.flash(r.error, "warning");
    };
    const rooms = wall ? wallRooms(floor, wall) : [];
    const subtitle = html`<span class="muted small">${rooms.length ? `${rooms.length > 1 ? "Between " : "In "}${rooms.map((r) => roomLabel(r, this.areaName)).join(" and ")}` : `On ${floor.name}`}</span>`;
    if (this.mode !== "build") {
      // Configure and Live (Phase A5): the window's curtain link, orientation and covers; geometry is edited in Build.
      const cw = o.type === "window" ? this.curtainWindow(o).window : null;
      return this.inspectorShell(cw?.name ?? OPENING_LABEL[o.type], o.type === "opening" ? "open-plan" : o.type, html`
        ${this.renderOpeningCurtains(floor, o)}
        <section class="inspector-section">
          ${kvRows([["Type", OPENING_LABEL[o.type]], ["Width", formatLength(o.width, this.units())]])}
          <p class="muted small">Its position and size are edited in Build.</p>
        </section>`, subtitle);
    }
    return this.inspectorShell(OPENING_LABEL[o.type], o.type === "opening" ? "open-plan" : o.type, html`<section class="inspector-section">
        <label class="field"><span class="field-label">Type</span>
          <select data-testid="fp-opening-type" ?disabled=${!editable} @change=${(e: Event) => patch({ type: (e.target as HTMLSelectElement).value as OpeningType }, "Change opening type")}>
            ${(["door", "window", "opening"] as OpeningType[]).map((t) => html`<option value=${t} ?selected=${o.type === t}>${OPENING_LABEL[t]}</option>`)}
          </select></label>
        <div class="fp-xy">
          ${this.numberField("Width", o.width, (w) => patch({ width: w }, "Change width"), { min: 0.1, max: length, unit: "m", testid: "fp-opening-width" })}
          ${this.numberField("From the wall's start", o.offset, (v) => patch({ offset: v }, "Move opening"), { min: 0, max: length, unit: "m" })}
        </div>
        ${o.type === "door"
          ? html`<label class="field"><span class="field-label">Hinge</span>
              <select ?disabled=${!editable} @change=${(e: Event) => patch({ swing: (e.target as HTMLSelectElement).value as "left" | "right" }, "Flip door")}>
                <option value="left" ?selected=${o.swing !== "right"}>At the start of the wall</option>
                <option value="right" ?selected=${o.swing === "right"}>At the end of the wall</option>
              </select></label>`
          : nothing}
        <p class="muted small">Drag it along the wall to move it. It stays on its wall when the wall moves. A drawn door does not make occupancy spread between rooms.</p>
        ${o.type === "window" && o.curtain_window_id ? html`<p class="small">${chip("info", "Linked to a curtain window")} <a class="text-link" href="#/floorplan/configure">Manage it under Configure</a></p>` : nothing}
        ${editable ? html`<button type="button" class="btn btn-danger" data-testid="fp-delete-opening" @click=${() => this.deleteSelection()}>${icon("delete", 18)}Delete</button>` : nothing}
      </section>`, subtitle);
  }

  private renderBackgroundInspector(floor: Floor): unknown {
    const bg = floor.background;
    if (!bg) return nothing;
    const editable = this.writable();
    const set = (patch: Partial<typeof bg>, label: string) => this.editFloor(label, (f) => Object.assign(f.background!, patch));
    return this.inspectorShell("Background image", "image", html`<section class="inspector-section" data-testid="fp-bg-inspector">
        <label class="field"><span class="field-label">Opacity ${Math.round(bg.opacity * 100)} %</span>
          <input type="range" min="0.05" max="1" step="0.05" .value=${String(bg.opacity)} ?disabled=${!editable} data-testid="fp-bg-opacity" @change=${(e: Event) => set({ opacity: Number((e.target as HTMLInputElement).value) }, "Background opacity")} /></label>
        <div class="fp-xy">
          ${this.numberField("Width", bg.width_px * bg.scale, (w) => w > 0 && set({ scale: w / bg.width_px }, "Scale background"), { min: 0.1, unit: "m", testid: "fp-bg-width" })}
          ${this.numberField("Rotation", bg.rotation, (r) => set({ rotation: ((r % 360) + 360) % 360 }, "Rotate background"), { step: 0.5, unit: "°", testid: "fp-bg-rotation" })}
          ${this.numberField("Left (x)", bg.x, (x) => set({ x }, "Move background"), { unit: "m" })}
          ${this.numberField("Top (y)", bg.y, (y) => set({ y }, "Move background"), { unit: "m" })}
        </div>
        <p class="muted small">${bg.width_px} × ${bg.height_px} px · ${formatLength(bg.scale * 100, this.units())} per 100 px</p>
        <label class="switch-row compact"><input type="checkbox" role="switch" .checked=${bg.locked} ?disabled=${!editable} @change=${(e: Event) => set({ locked: (e.target as HTMLInputElement).checked }, "Lock background")} /><span>Locked (cannot be dragged)</span></label>
        <label class="switch-row compact"><input type="checkbox" role="switch" .checked=${bg.visible} ?disabled=${!editable} @change=${(e: Event) => set({ visible: (e.target as HTMLInputElement).checked }, "Show background")} /><span>Visible</span></label>
        ${editable
          ? html`<div class="row-actions">
              <button type="button" class="btn btn-primary" data-testid="fp-calibrate" @click=${() => (this.tool = "calibrate")}>${icon("ruler", 18)}Calibrate with a known distance</button>
              <button type="button" class="btn btn-danger" data-testid="fp-remove-bg" @click=${() => this.deleteSelection()}>${icon("delete", 18)}Remove…</button>
            </div>`
          : nothing}
        <p class="muted small">Drag the image to move it (when not locked). The image is only served to Home Assistant users through the App.</p>
      </section>`);
  }

  private renderPlacementInspector(floor: Floor, p: Placement): TemplateResult {
    const r = this.resolve(p);
    const e = r.entity;
    const d = this.details && this.details.entity.entity_id === r.entity_id ? this.details : null;
    const liveState = store.liveStates.get(r.entity_id);
    const view = deviceView(r.entity_id, liveState, e);
    const room = roomAt(floor, p);
    const roomArea = room?.area_id ?? null;
    const uses = d?.uses ?? [];
    const editable = this.writable() && this.mode !== "live";
    const category = d?.entity.category ?? e?.category ?? "other";
    const as = (d?.entity.domain ?? r.entity_id.split(".")[0]) === "light" ? "light" : SENSOR_CATEGORIES.has(category) ? "sensor" : null;
    const usedHere = roomArea ? uses.some((u) => u.area_id === roomArea && (u.as === "sensor" || u.as === "light")) : false;
    const availability = (liveState?.availability ?? d?.entity.availability ?? e?.availability ?? (r.missing ? "missing" : "unknown")) as Availability;
    const haArea = d?.entity.area ?? e?.area ?? null;
    const body = html`
      <section class="inspector-section">
        <div class="live-state">${chip((AVAILABILITY[availability]?.tone ?? "neutral") as Tone, AVAILABILITY[availability]?.label ?? availability)}<span class="state-value" data-testid="fp-device-state">${view.text}</span></div>
        <p class="muted small">Last changed ${formatTime(liveState?.last_changed ?? d?.entity.last_changed)}</p>
        ${r.renamed ? html`<p class="notice notice-info small">${icon("info", 18)}<span>Renamed in Home Assistant from ${p.entity_id}; found by its registry id.</span></p>` : nothing}
        ${r.missing ? html`<p class="notice notice-warning small">${icon("warning", 18)}<span>${p.entity_id} no longer exists in Home Assistant. Replace it with another entity, or remove the icon.</span></p>` : nothing}
      </section>
      ${this.presenceSensorSection(r.entity_id)}
      ${/^(light|switch)\./.test(r.entity_id) ? html`<section class="inspector-section" data-testid="fp-light-inspect"><h4>Lighting</h4>
        <button type="button" class="btn btn-small" data-testid="fp-open-light-inspector" @click=${() => inspectLight(r.entity_id)}>${icon("information-outline", 16)}What controls this light?</button></section>` : nothing}
      <section class="inspector-section" data-testid="fp-device-identity">
        <h4>Device</h4>
        ${kvRows([
          ["Entity ID", html`<code class="small">${r.entity_id}</code>`],
          ["Type", d?.entity.category_label ?? e?.category_label],
          ["Device", d?.device ? `${d.device.name}${d.device.model ? ` · ${[d.device.manufacturer, d.device.model].filter(Boolean).join(" ")}` : ""}` : (e?.device ?? "No device")],
          ["Home Assistant area", haArea ?? "None"],
          ["Capabilities", d?.entity.capabilities.length ? d.entity.capabilities.join(", ") : null],
        ])}
      </section>
      <section class="inspector-section" data-testid="fp-device-placement">
        <h4>On the floor plan <span class="muted small">(visual only)</span></h4>
        <p class="small">${room ? html`Inside <strong>${roomLabel(room, this.areaName)}</strong>${roomArea ? ` (area ${this.areaName(roomArea)})` : " (not linked to an area)"}` : "Not inside a drawn room"}.</p>
        ${roomArea && haArea && d?.entity.area_id && d.entity.area_id !== roomArea ? html`<p class="muted small">Its Home Assistant area is ${haArea}. Moving the icon does not change that.</p>` : nothing}
        ${editable
          ? html`<label class="field"><span class="field-label">Label on the plan</span>
              <input type="text" maxlength="100" placeholder=${e?.name ?? ""} .value=${p.label ?? ""} @change=${(ev: Event) => this.applyFloor(updatePlacement(floor, p.id, { label: (ev.target as HTMLInputElement).value.trim() || null }), "Label device icon")} /></label>`
          : nothing}
      </section>
      <section class="inspector-section" data-testid="fp-device-automation">
        <h4>Automation <span class="muted small">(Smart Occupancy integration)</span></h4>
        ${!d ? html`<p class="muted small">Loading…</p>` : uses.length
          ? html`<ul class="fp-uses">${uses.map((u) => html`<li>${chip("info", u.area)}<span class="small">${u.as === "sensor" ? `Sensor · ${u.kind_label ?? u.kind} · ${u.role_label ?? u.role}${u.enabled === false ? " · excluded" : ""}${u.inverted ? " · inverted" : ""}` : u.as === "light" ? "Managed light" : u.as === "reference" ? "Reference occupancy entity" : u.as === "relay" ? "Lighting relay" : u.as === "switch" ? `Wall switch ${u.name ?? ""} · ${u.mode}` : `Curtain ${u.name ?? ""} · ${u.mode}`}</span></li>`)}</ul>`
          : html`<p class="small">Not used by the automation.</p>`}
        ${roomArea && as && !usedHere && this.ctx?.areas.some((a) => a.id === roomArea)
          ? html`<p class="muted small">It is placed in ${this.areaName(roomArea)} but not assigned there.</p>
            <button type="button" class="btn btn-primary" data-testid="fp-assign" ?disabled=${!this.ctx?.integration.writable} @click=${() => this.openRoomsEditor(roomArea, "devices", { assign: r.entity_id, as })}>
              ${icon("eye", 16)}Assign to ${this.areaName(roomArea)} in the Rooms editor…</button>`
          : nothing}
        ${uses.filter((u) => u.area_id !== roomArea).map((u) => html`<button type="button" class="btn" @click=${() => this.openRoomsEditor(u.area_id, "devices", { select: r.entity_id })}>${icon("pencil", 16)}Open ${u.area} in the Rooms editor</button>`)}
        <p class="muted small">Moving the icon never changes an assignment. Assignments change only in the Rooms editor, after the integration's review.</p>
      </section>
      ${d?.siblings.length && editable
        ? html`<section class="inspector-section"><h4>Also on this device</h4>
            <ul class="fp-inside">${d.siblings.slice(0, 8).map((s) => {
              const placedAlready = this.placedIds().has(s.entity_id);
              return html`<li><span class="small">${s.name} <span class="muted">· ${s.category_label}</span></span>${placedAlready ? chip("ok", "On the plan") : html`<button type="button" class="btn" @click=${() => {
                const ent = this.index().byId.get(s.entity_id);
                const res = placeEntity(floor, { entity_id: s.entity_id, entity_uid: ent?.entity_uid ?? null, device_id: ent?.device_id ?? null }, { x: p.x + 0.3, y: p.y + 0.3 });
                this.applyFloor(res.floor, "Place device", { kind: "placement", id: res.placementId });
              }}>${icon("plus", 16)}Place</button>`}</li>`;
            })}</ul></section>`
        : nothing}
      ${editable
        ? html`<section class="inspector-section"><div class="row-actions">
            <button type="button" class="btn" data-testid="fp-replace" @click=${() => (this.dialog = { kind: "replace", placementId: p.id, query: "" })}>${icon("swap", 16)}Replace with another entity…</button>
            <button type="button" class="btn btn-danger" data-testid="fp-remove-icon" @click=${() => this.deleteSelection()}>${icon("delete", 16)}Remove from the plan</button>
          </div></section>`
        : nothing}`;
    return this.inspectorShell(p.label || e?.name || d?.entity.name || r.entity_id, e?.icon ?? "puzzle", body, html`<code class="small">${r.entity_id}</code>`);
  }

  // -------------------------------------------------------------- dialogs
  private renderDialog(): unknown {
    const d = this.dialog;
    if (!d) return nothing;
    const close = () => {
      this.querySelector<HTMLDialogElement>("dialog.fp-dialog")?.close();
      this.dialog = null;
    };
    const shell = (title: string, body: unknown, foot: unknown, wide = false) => html`<dialog class="sheet fp-dialog ${wide ? "sheet-full" : ""}" aria-labelledby="fp-dialog-title" data-testid="fp-dialog-${d.kind}"
        @close=${() => (this.dialog = null)} @cancel=${() => (this.dialog = null)}>
      <div class="sheet-inner">
        <header class="sheet-head"><h2 id="fp-dialog-title">${title}</h2><button type="button" class="icon-btn" aria-label="Close" @click=${close}>${icon("close")}</button></header>
        <div class="sheet-body">${body}</div>
        <footer class="sheet-foot">${foot}</footer>
      </div>
    </dialog>`;
    switch (d.kind) {
      case "confirm":
        return shell(d.title, html`<p>${d.body}</p>`, html`<button type="button" class="btn" @click=${close}>Cancel</button>
          <button type="button" class="btn ${d.danger ? "btn-danger" : "btn-primary"}" data-testid="fp-confirm" @click=${() => (close(), d.action())}>${d.confirm}</button>`);
      case "floor":
        return shell(d.floorId ? "Edit floor" : "Add floor", html`
            <label class="field"><span class="field-label">Name</span><input type="text" maxlength="100" data-testid="fp-floor-name" .value=${d.name} @input=${(e: Event) => (this.dialog = { ...d, name: (e.target as HTMLInputElement).value })} /></label>
            <label class="field"><span class="field-label">Level (0 = ground floor, negative = basement)</span><input type="number" step="1" min="-20" max="200" .value=${String(d.level)} @input=${(e: Event) => (this.dialog = { ...d, level: Math.round(Number((e.target as HTMLInputElement).value) || 0) })} /></label>
            <label class="field"><span class="field-label">Home Assistant floor (optional)</span>
              <select @change=${(e: Event) => (this.dialog = { ...d, ha_floor_id: (e.target as HTMLSelectElement).value || null })}>
                <option value="" ?selected=${!d.ha_floor_id}>None</option>
                ${(this.ctx?.floors ?? []).map((f) => html`<option value=${f.id} ?selected=${d.ha_floor_id === f.id}>${f.name}</option>`)}
              </select></label>`,
          html`<button type="button" class="btn" @click=${close}>Cancel</button><button type="button" class="btn btn-primary" data-testid="fp-floor-save" ?disabled=${!d.name.trim()} @click=${() => this.saveFloorDialog(d)}>${d.floorId ? "Apply" : "Add floor"}</button>`);
      case "calibrate": {
        const now = dist(d.p1, d.p2);
        return shell("Calibrate with a known distance", html`
            <p>The two points are ${formatLength(now, this.units())} apart on the plan now. Enter their real distance:</p>
            <label class="field"><span class="field-label">Real distance (${this.units() === "imperial" ? "feet and inches, e.g. 12' 6\"" : "metres"})</span>
              <input type="text" inputmode="decimal" data-testid="fp-calib-value" autofocus .value=${d.value} @input=${(e: Event) => (this.dialog = { ...d, value: (e.target as HTMLInputElement).value, error: undefined })} @keydown=${(e: KeyboardEvent) => e.key === "Enter" && this.applyCalibration(d)} /></label>
            ${this.floor()?.background && this.floor()?.rooms.length
              ? html`<label class="switch-row compact"><input type="checkbox" role="switch" .checked=${d.drawing} @change=${(e: Event) => (this.dialog = { ...d, drawing: (e.target as HTMLInputElement).checked })} /><span>Scale the drawing too <span class="muted small block">Keeps rooms, doors and device icons on the image.</span></span></label>`
              : nothing}
            ${d.error ? html`<p class="notice notice-error small">${icon("error", 18)}<span>${d.error}</span></p>` : nothing}`,
          html`<button type="button" class="btn" @click=${() => (close(), (this.tool = "select"))}>Cancel</button><button type="button" class="btn btn-primary" data-testid="fp-calib-apply" @click=${() => this.applyCalibration(d)}>Calibrate</button>`);
      }
      case "conflict":
        return shell("Saved in another session", html`<p>Someone saved the floor plan in another browser session after you opened it. Saving now would replace their version.</p>
            <p class="muted small">The version that is replaced is kept as a backup either way.</p>`,
          html`<button type="button" class="btn" @click=${() => (close(), void this.load())}>Load their version (discard mine)</button>
            <button type="button" class="btn btn-primary" data-testid="fp-overwrite" @click=${() => void this.overwrite()}>Save mine anyway</button>`);
      case "link": {
        const room = this.floor()?.rooms.find((r) => r.id === d.roomId);
        return shell(`${this.areaName(d.areaId)} is already linked`, html`<p>${this.areaName(d.areaId)} is linked to ${d.others.map((o) => `${roomLabel(o.room, this.areaName)} (${o.floor.name})`).join(", ")}. A Home Assistant area normally belongs to one drawn room.</p>
            <p class="muted small">Use a shared space for one area drawn as several parts, for example an L-shaped open-plan kitchen and living room.</p>`,
          html`<button type="button" class="btn" @click=${close}>Cancel</button>
            <button type="button" class="btn" data-testid="fp-link-share" @click=${() => this.resolveLink(d, "share")}>Model as one shared space</button>
            <button type="button" class="btn btn-primary" data-testid="fp-link-move" @click=${() => this.resolveLink(d, "move")}>Move the link to ${room ? roomLabel(room, this.areaName) : "this room"}</button>`);
      }
      case "backups":
        return shell("Floor plan backups", html`
            <p class="muted">The App keeps earlier versions of the floor plan (the latest 30): before changes, at most one every 10 minutes while you edit, and always before an import or a restore.</p>
            ${d.error ? html`<p class="notice notice-error small">${icon("error", 18)}<span>${d.error}</span></p>` : nothing}
            ${d.list === null ? html`<p class="muted">Loading…</p>` : !d.list.length ? html`<p class="muted">No backups yet.</p>`
              : html`<ul class="backup-list">${d.list.map((b) => html`<li>
                  <span><strong>${formatTime(b.created_at)}</strong> · ${b.reason ?? "Backup"}<span class="muted small block">${b.summary ? `${plural(b.summary.floors, "floor", "floors")} · ${plural(b.summary.rooms, "room", "rooms")} · ${plural(b.summary.placements, "device icon", "device icons")}` : ""}</span></span>
                  <span class="row-actions">
                    <button type="button" class="btn" @click=${() => void getRooms(`api/floorplan/backups/${encodeURIComponent(b.id)}`).then((data) => downloadJson(data, `smart-lighting-${b.id}.json`))}>${icon("download", 18)}Download</button>
                    <button type="button" class="btn" ?disabled=${d.busy || !this.writable()} data-restore=${b.id} @click=${() => (this.dirty() ? (this.dialog = { ...d, error: "Save or discard your changes first." }) : void this.restore(b))}>${icon("restore", 18)}Restore</button>
                  </span></li>`)}</ul>`}`,
          html`<button type="button" class="btn" @click=${close}>Close</button>`, true);
      case "import":
        return shell("Import a floor plan", html`
            ${d.busy && !d.review ? html`<p class="muted" role="status"><span class="spinner" aria-hidden="true"></span> Checking the file…</p>` : nothing}
            ${d.error ? html`<p class="notice notice-error small">${icon("error", 18)}<span>${d.error}</span></p>` : nothing}
            ${d.review
              ? html`<p>The file has ${plural(d.review.summary.floors, "floor", "floors")}, ${plural(d.review.summary.rooms, "room", "rooms")} (${d.review.summary.linked_rooms} linked), ${plural(d.review.summary.openings, "door or window", "doors and windows")} and ${plural(d.review.summary.placements, "device icon", "device icons")}.</p>
                <p>It <strong>replaces</strong> the current floor plan (${plural(d.review.current.floors, "floor", "floors")}, ${plural(d.review.current.rooms, "room", "rooms")}), which is kept as a backup. Nothing changes in Home Assistant.</p>
                ${d.review.identical ? html`<p class="muted">The file is identical to the current floor plan.</p>` : nothing}
                ${d.review.warnings.map((w) => html`<p class="notice notice-warning small">${icon("warning", 18)}<span>${w}</span></p>`)}
                ${this.dirty() ? html`<p class="notice notice-warning small">${icon("warning", 18)}<span>Your unsaved changes are discarded by the import.</span></p>` : nothing}`
              : nothing}`,
          html`<button type="button" class="btn" @click=${close}>Cancel</button><button type="button" class="btn btn-primary" data-testid="fp-import-confirm" ?disabled=${!d.review || d.busy || d.review.identical || !d.review.writable} @click=${() => void this.confirmImport(d)}>Replace the floor plan</button>`);
      case "replace": {
        const words = normalize(d.query).split(/\s+/).filter(Boolean);
        const list = (this.ctx?.entities ?? []).filter((e) => !words.length || words.every((w) => normalize(`${e.name} ${e.entity_id} ${e.area ?? ""}`).includes(w))).slice(0, 80);
        const floor = this.floor();
        return shell("Replace with another entity", html`
            <p class="muted small">The icon keeps its position and label and points to the chosen entity instead. Nothing changes in Home Assistant.</p>
            <input class="panel-search" type="search" autofocus placeholder="Search name, entity ID or area" aria-label="Search" .value=${d.query} @input=${(e: Event) => (this.dialog = { ...d, query: (e.target as HTMLInputElement).value })} />
            <ul class="device-palette">${list.map((e) => html`<li><button type="button" class="palette-item" data-pick=${e.entity_id} @click=${() => {
              if (floor) this.applyFloor(updatePlacement(floor, d.placementId, { entity_id: e.entity_id, entity_uid: e.entity_uid, device_id: e.device_id }), "Replace entity", { kind: "placement", id: d.placementId });
              close();
            }}>${icon(e.icon, 20)}<span class="palette-text"><span class="palette-name">${e.name}</span><span class="muted small">${e.entity_id}${e.area ? ` · ${e.area}` : ""}</span></span></button></li>`)}</ul>`,
          html`<button type="button" class="btn" @click=${close}>Cancel</button>`, true);
      }
      case "bearing":
        return shell(`Set the orientation of ${d.name}?`, html`
            <p>The floor plan suggests the direction this window faces from its wall and the north direction you set. Review it before it is applied:</p>
            <dl class="kv fp-bearing-compare" data-testid="fp-bearing-compare">
              <dt>Now in Curtains</dt><dd>${d.current === null ? "Not set" : bearingText(d.current, d.currentCompass)}${d.current !== null ? html` <span class="muted small">(${d.explicit ? "set explicitly" : `from the compass direction ${d.direction ?? ""}`})</span>` : nothing}</dd>
              <dt>Suggested</dt><dd><strong>${bearingText(d.suggested, d.compass)}</strong></dd>
            </dl>
            ${d.explicit ? html`<p class="notice notice-warning small">${icon("warning", 18)}<span>This replaces the bearing that was set explicitly in the curtain configuration (${bearingText(d.current, d.currentCompass)}).</span></p>` : d.direction ? html`<p class="muted small">The bearing takes precedence over the compass direction “${d.direction}” set in Curtains.</p>` : nothing}
            <p class="muted small">A 2D plan gives the direction only. Sun exposure also depends on obstructions, the horizon and neighbouring buildings, which are set in Curtains. Nothing else changes: no cover moves.</p>
            ${d.error ? html`<p class="notice notice-error small">${icon("error", 18)}<span>${d.error}</span></p>` : nothing}`,
          html`<button type="button" class="btn" ?disabled=${d.busy} @click=${close}>Cancel</button>
            <button type="button" class="btn btn-primary" data-testid="fp-bearing-apply" ?disabled=${d.busy} @click=${() => void this.applyBearing(d)}>${d.busy ? "Applying…" : `Set to ${bearingText(d.suggested, d.compass)}`}</button>`);
    }
  }

  protected render(): unknown {
    if (!this.plan) {
      return this.loadError
        ? html`<div class="card empty-state" role="alert">${icon("error", 40)}<h3>The floor plan could not be loaded</h3><p class="muted">${this.loadError}</p><button type="button" class="btn" @click=${() => void this.load()}>${icon("refresh", 20)}Try again</button></div>`
        : html`<div class="card loading" role="status"><span class="spinner" aria-hidden="true"></span><span>Loading the floor plan…</span></div>`;
    }
    const floor = this.floor();
    const readonly = !this.writable() || this.mode === "live";
    return html`<div class="fp-page mode-${this.mode}" data-fp-mode=${this.mode}>
      ${this.renderHead()}
      ${this.renderNotices()}
      <div class="fp-workspace ${this.leftOpen ? "" : "left-collapsed"} ${this.inspectorOpen ? "inspector-open" : ""}">
        ${this.leftOpen ? html`<aside class="fp-left ws-left" aria-label=${`${this.mode} tools`} data-testid="fp-left">${this.mode === "build" ? this.renderBuildPanel() : this.mode === "configure" ? this.renderConfigurePanel() : this.renderLivePanel()}</aside>` : nothing}
        <section class="fp-center" aria-label="Floor plan">
          <sl-plan-canvas
            .floor=${floor ?? null}
            .mode=${this.mode}
            .tool=${this.mode === "build" ? this.tool : "select"}
            .selection=${this.selection}
            .grid=${this.grid}
            .snap=${this.snap}
            .showDims=${this.showDims}
            .showValues=${this.showValues}
            .units=${this.units()}
            .readonly=${readonly}
            .chain=${this.chain}
            .rooms=${floor ? this.roomViews(floor) : new Map()}
            .placements=${floor ? this.placementViews(floor) : []}
            .placing=${this.mode === "configure" ? this.placing : null}
            .curtains=${floor ? this.curtainOverlays(floor) : new Map()}
            .imageUrl=${imageUrl}
            @plan-edit=${(e: CustomEvent<EditDetail>) => this.onCanvasEdit(e)}
            @plan-select=${(e: CustomEvent<Selection | null>) => this.select(e.detail)}
            @plan-notice=${(e: CustomEvent<string>) => this.flash(e.detail, "warning")}
            @plan-place=${(e: CustomEvent<{ x: number; y: number; entity_id: string }>) => this.onPlace(e)}
            @plan-calibrate=${(e: CustomEvent<{ p1: Pt; p2: Pt }>) => this.onCalibrate(e)}
          ></sl-plan-canvas>
        </section>
        ${this.renderInspector()}
        ${this.inspectorOpen ? html`<div class="inspector-scrim fp-scrim" @click=${() => (this.inspectorOpen = false)}></div>` : nothing}
      </div>
      ${this.renderDialog()}
    </div>`;
  }
}

function kvRows(rows: Array<[string, unknown]>): TemplateResult {
  return html`<dl class="kv">${rows.map(([k, v]) => html`<dt>${k}</dt><dd>${v === null || v === undefined || v === "" ? "—" : v}</dd>`)}</dl>`;
}

customElements.define("sl-floorplan", SlFloorplan);
