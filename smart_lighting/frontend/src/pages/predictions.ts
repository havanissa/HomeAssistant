// Predictions (Phase A5): the occupancy forecasts of the house. House overview, learned patterns, accuracy and
// settings. Forecasts are observation only (they command nothing); the authoritative ones come from whoever
// produces them (the integration, or the App after a reviewed handover). Route: #/predictions/<tab>.
import "../predictions/predictions.css";

import { html, nothing, type PropertyValues, type TemplateResult } from "lit";

import { download } from "../rooms/model";
import { icon } from "../icons";
import { setLeaveGuard } from "../router";
import { chip, kv, pageHeader } from "../ui";
import { haTime } from "../presence/components";
import { duration } from "../presence/model";
import {
  applyImport, cmd, forecast, predictionsRoom, reviewImport, HORIZONS,
  type GlobalSettings, type House, type ImportReview, type RoomDetail,
} from "../predictions/api";
import {
  adminNotice, confidenceChip, controlBanner, dayWord, decisionList, headline, learningChip, observationNotice,
  PredictionsElement, problemNotice, statusChip, CONFIDENCE,
} from "../predictions/components";
import { miniBar } from "../predictions/charts";
import { houseAccuracyPanel, patternsPanel, roomAccuracyPanel } from "../predictions/panels";
import { CONFIDENCE_OPTIONS, connectionsEditor, overridesTable } from "../predictions/editors";

const TABS = [
  { key: "house", title: "House", icon: "home" },
  { key: "patterns", title: "Patterns", icon: "history" },
  { key: "accuracy", title: "Accuracy", icon: "shield-check" },
  { key: "settings", title: "Settings", icon: "cog" },
] as const;
type Tab = (typeof TABS)[number]["key"];
const NEEDS_ROOM: Tab[] = ["patterns", "accuracy", "settings"];

type Dialog = { kind: "rebuild" } | { kind: "import"; file: unknown; review: ImportReview | null; error?: string; busy?: boolean } | null;

const NUMBER_FIELDS: Array<{ key: keyof GlobalSettings; label: string; unit: string; help: string }> = [
  { key: "window_days", label: "History window", unit: "days", help: "How far back the model learns" },
  { key: "half_life_days", label: "Half-life", unit: "days", help: "Recent days weigh more; 0 = all days equal" },
  { key: "refresh_minutes", label: "Learn every", unit: "min", help: "How often new history is read" },
  { key: "probability_threshold", label: "Threshold for \"predicted occupied\"", unit: "%", help: "Probability at the horizon above which a room is predicted occupied" },
  { key: "min_days", label: "Minimum days", unit: "days", help: "Before a room's forecasts are shown" },
  { key: "min_entries", label: "Minimum visits", unit: "visits", help: "Before a room's forecasts are shown" },
];

export class SlPredictions extends PredictionsElement {
  static properties = {
    ...PredictionsElement.properties, tab: { type: String }, room: { state: true }, detail: { state: true }, detailError: { state: true },
    draft: { state: true }, dialog: { state: true }, pickContext: { state: true },
  };
  declare tab: string;
  declare room: string;
  declare detail: RoomDetail | null;
  declare detailError: unknown;
  declare draft: Partial<GlobalSettings> | null;
  declare dialog: Dialog;
  declare pickContext: string;
  private loadingRoom: string | null = null;

  constructor() {
    super();
    this.tab = "house";
    this.room = "";
    this.detail = null;
    this.detailError = null;
    this.draft = null;
    this.dialog = null;
    this.pickContext = "";
  }

  connectedCallback(): void {
    super.connectedCallback();
    setLeaveGuard(() => (this.dirty() ? "The predictions settings have unsaved changes. Leave without saving them?" : null));
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    setLeaveGuard(null);
  }

  private get currentTab(): Tab {
    return (TABS.find((t) => t.key === this.tab)?.key ?? "house") as Tab;
  }

  private get house(): House | null {
    return this.status?.house ?? null;
  }

  protected willUpdate(changed: PropertyValues): void {
    super.willUpdate();
    if (changed.has("tab") && NEEDS_ROOM.includes(this.currentTab) && !this.detail && !this.loadingRoom) void this.reload();
  }

  async reload(): Promise<void> {
    const rooms = this.house?.rooms ?? [];
    if (!this.room || !rooms.some((r) => r.area_id === this.room)) this.room = rooms[0]?.area_id ?? "";
    if (!this.room || !NEEDS_ROOM.includes(this.currentTab)) return;
    await this.loadRoom(this.room);
  }

  private async loadRoom(areaId: string): Promise<void> {
    this.loadingRoom = areaId;
    try {
      const detail = await predictionsRoom(areaId);
      if (this.room === areaId) {
        this.detail = detail;
        this.detailError = null;
      }
    } catch (err) {
      if (this.room === areaId) {
        this.detail = null;
        this.detailError = err;
      }
    } finally {
      if (this.loadingRoom === areaId) this.loadingRoom = null;
    }
  }

  private pickRoom(areaId: string): void {
    if (areaId === this.room) return;
    this.room = areaId;
    this.detail = null;
    void this.loadRoom(areaId);
  }

  // ------------------------------------------------------------- settings draft
  private get global(): GlobalSettings | null {
    return this.house?.settings?.global ?? this.detail?.settings.global ?? null;
  }

  private value<K extends keyof GlobalSettings>(key: K): GlobalSettings[K] | undefined {
    return this.draft && key in this.draft ? (this.draft[key] as GlobalSettings[K]) : this.global?.[key];
  }

  private set<K extends keyof GlobalSettings>(key: K, value: GlobalSettings[K]): void {
    this.draft = { ...(this.draft ?? {}), [key]: value };
  }

  private changes(): Partial<GlobalSettings> {
    const g = this.global;
    const out: Partial<GlobalSettings> = {};
    if (!g || !this.draft) return out;
    for (const [k, v] of Object.entries(this.draft) as Array<[keyof GlobalSettings, unknown]>) {
      if (JSON.stringify(v) !== JSON.stringify(g[k])) (out as Record<string, unknown>)[k] = v;
    }
    return out;
  }

  private dirty(): boolean {
    return Object.keys(this.changes()).length > 0;
  }

  private async saveGlobal(): Promise<void> {
    const values = this.changes();
    if (!Object.keys(values).length) return;
    if (await this.run(() => forecast(cmd("settings"), { area_id: null, values }), "Global defaults saved")) this.draft = null;
  }

  // ------------------------------------------------------------- dialogs
  private async openDialog(d: Dialog): Promise<void> {
    this.dialog = d;
    this.error = null;
    await this.updateComplete;
    this.querySelector<HTMLDialogElement>("dialog.pr-dialog")?.showModal();
  }

  private closeDialog(): void {
    this.querySelector<HTMLDialogElement>("dialog.pr-dialog")?.close();
    this.dialog = null;
  }

  private async rebuild(): Promise<void> {
    if (await this.run(() => forecast(cmd("rebuild")), "Rebuilt from the Recorder")) this.closeDialog();
  }

  private async chooseImport(e: Event): Promise<void> {
    const input = e.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = "";
    if (!file) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(await file.text());
    } catch {
      await this.openDialog({ kind: "import", file: null, review: null, error: "This file is not a predictions export (JSON)." });
      return;
    }
    await this.openDialog({ kind: "import", file: parsed, review: null, busy: true });
    try {
      const review = await reviewImport(parsed);
      this.dialog = { kind: "import", file: parsed, review };
    } catch (err) {
      this.dialog = { kind: "import", file: parsed, review: null, error: err instanceof Error ? err.message : String(err) };
    }
  }

  private async confirmImport(): Promise<void> {
    const d = this.dialog;
    if (!d || d.kind !== "import" || !d.review) return;
    const review = d.review;
    if (await this.run(() => applyImport(d.file, review.current_revision), `Imported ${dayWord(review.days)} of learned data`)) this.closeDialog();
  }

  // ------------------------------------------------------------- house tab
  private houseTab(h: House): TemplateResult {
    const fmt = haTime(false);
    const l = h.learning;
    const c = h.coverage;
    const forecasting = h.rooms.filter((r) => r.now?.status === "ok").length;
    const refresh = h.settings?.global.refresh_minutes;
    return html`<div data-testid="predictions-house">
      <div class="stat-tiles pr-tiles-wide">
        <div class="stat"><span class="stat-value">${learningChip(l.state)}</span><span class="stat-label">learning${l.progress ? ` · ${l.progress[0]} of ${l.progress[1]}` : ""}</span></div>
        <div class="stat"><span class="stat-value">${c.days_learned ?? 0}<span class="small muted"> / ${c.window_days ?? "—"}</span></span><span class="stat-label">days learned / window</span></div>
        <div class="stat"><span class="stat-value small">${c.first_date ?? "—"} → ${c.last_date ?? "—"}</span><span class="stat-label">history coverage${c.today_included ? " · today included" : ""}</span></div>
        <div class="stat"><span class="stat-value small">${c.recorder_oldest ? fmt(c.recorder_oldest) : "—"}</span><span class="stat-label">oldest Recorder data</span></div>
        <div class="stat"><span class="stat-value">${forecasting}<span class="small muted"> / ${h.rooms.length}</span></span><span class="stat-label">rooms forecasting</span></div>
        <div class="stat"><span class="stat-value small">${fmt(l.last_update)}</span><span class="stat-label">last learned${refresh ? ` · every ${duration(refresh * 60)}` : ""}</span></div>
      </div>
      ${l.detail ? html`<p class="small muted">${l.detail}${c.days_without_data ? ` · ${dayWord(c.days_without_data)} without data` : ""}${l.last_error ? html` · <span class="tone-error">${l.last_error}</span>` : nothing}</p>` : nothing}
      <div class="grid engine-grid">
        ${this.nextRoomCard(h)}
        <section class="card wide"><header class="card-head">${icon("crystal-ball")}<h3>Rooms</h3><span class="small muted">forecasts from the ${h.source === "app" ? "App" : "integration"} · ${fmt(h.view_at)}</span></header>
          ${h.rooms.length ? html`<div class="pr-room-grid">${h.rooms.map((r) => this.roomCard(r))}</div>` : html`<p class="muted">No room is enabled.</p>`}
        </section>
        <section class="card wide"><header class="card-head">${icon("list")}<h3>House log</h3></header>
          <p class="muted small">Learning, model rebuilds and predicted movements between rooms. Nothing in it commanded a device.</p>
          ${decisionList(h.house_log, { empty: "Nothing recorded yet.", name: (id) => this.roomName(id), limit: 30 })}
        </section>
      </div>
    </div>`;
  }

  private nextRoomCard(h: House): TemplateResult {
    const n = h.next_room;
    const fmt = haTime();
    return html`<section class="card wide" data-testid="predictions-next-room">
      <header class="card-head">${icon("arrow-right")}<h3>Predicted next room</h3></header>
      ${n ? html`<div class="pr-next">
          <span class="pr-next-main"><a href="#/predictions/rooms/${n.area_id}">${this.roomName(n.area_id)}</a> <span class="muted">${Math.round(n.probability * 100)} %</span></span>
          <span class="small muted">after the arrival in ${this.roomName(n.source)} at ${fmt(new Date(n.since * 1000).toISOString())}</span>
          ${n.candidates.length > 1 ? html`<span class="small">Also possible: ${n.candidates.filter((c) => c[0] !== n.area_id).map((c) => `${this.roomName(c[0])} ${Math.round(c[1] * 100)} %`).join(", ")}</span>` : nothing}
        </div>`
        : html`<p class="muted">None: nobody arrived in a room recently enough to predict where they go next.</p>`}
    </section>`;
  }

  private roomCard(r: House["rooms"][number]): TemplateResult {
    const f = r.now;
    return html`<article class="pr-room-card" data-testid="predictions-room-card" data-area=${r.area_id}>
      <h4><a href="#/predictions/rooms/${r.area_id}">${r.name}</a>${statusChip(f?.status ?? (r.enabled ? null : "disabled"))}</h4>
      <p class="pr-row" style="margin:0">${headline(f)}</p>
      ${f && f.status === "ok" && f.probability !== null ? html`${miniBar(f.probability, f.threshold)}` : nothing}
      <p class="pr-row small" style="margin:0">${confidenceChip(f?.confidence?.level ?? (r.days ? null : "insufficient_data"), f?.confidence?.score)}
        <span class="muted">${dayWord(r.days)} · ${r.entries} visits · ${r.observed_hours} h observed</span></p>
      ${f?.explanation && f.status === "ok" && f.confidence?.level !== "insufficient_data" ? html`<p class="small" style="margin:0">${f.explanation}</p>` : nothing}
      ${f?.window?.text ? html`<p class="small muted" style="margin:0">${f.window.text}</p>` : nothing}
      <a class="small" href="#/predictions/rooms/${r.area_id}">Details ${icon("chevron-right", 14)}</a>
    </article>`;
  }

  // ------------------------------------------------------------- room selector
  private selector(label: string): TemplateResult {
    const rooms = this.house?.rooms ?? [];
    return html`<div class="pr-selector"><label class="pr-row"><span class="muted">${label}</span>
      <select data-testid="predictions-room-select" @change=${(e: Event) => this.pickRoom((e.target as HTMLSelectElement).value)}>
        ${rooms.map((r) => html`<option value=${r.area_id} ?selected=${r.area_id === this.room}>${r.name}</option>`)}</select></label>
      ${this.room ? html`<a class="btn btn-small" href="#/predictions/rooms/${this.room}">${icon("chevron-right", 16)}Room page</a>` : nothing}</div>`;
  }

  private roomDetailOr(render: (d: RoomDetail) => TemplateResult): TemplateResult {
    if (this.detailError) return problemNotice(this.detailError);
    if (!this.detail) return html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading the room…</span></div>`;
    return render(this.detail);
  }

  // ------------------------------------------------------------- settings tab
  private settingsTab(h: House): TemplateResult {
    const g = this.global;
    if (!g) return html`<p class="muted">Settings are not available yet.</p>`;
    const d = this.busy || !this.canEdit;
    const limits = this.detail?.settings.limits ?? {};
    const horizons = this.detail?.settings.horizons ?? [...HORIZONS];
    const levels = this.detail?.settings.confidence_options ?? CONFIDENCE_OPTIONS;
    const contexts = this.value("contexts");
    const workday = this.value("workday_entity");
    const candidates = this.detail?.context_candidates ?? [];
    const maxContexts = this.detail?.settings.max_contexts ?? 8;
    const name = (id: string) => candidates.find((c) => c.entity_id === id)?.name ?? id;
    const dirty = this.dirty();
    return html`<div data-testid="predictions-settings">
      ${!this.canEdit ? adminNotice() : nothing}
      <div class="grid engine-grid">
        <section class="card wide" data-testid="predictions-global"><header class="card-head">${icon("tune")}<h3>Global defaults</h3>${dirty ? chip("warning", "Unsaved changes") : nothing}</header>
          <p class="muted small">What every room follows unless it sets its own value (room overrides below). The thresholds decide when a probability becomes a "predicted occupied"; they never command anything.</p>
          <div class="pr-form">
            <label class="field field-check"><input type="checkbox" role="switch" data-field="enabled" .checked=${!!this.value("enabled")} ?disabled=${d} @change=${(e: Event) => this.set("enabled", (e.target as HTMLInputElement).checked)} /><span>Forecasts enabled</span></label>
            <label class="field"><span>Horizon (the headline probability looks this far ahead)</span>
              <select data-field="horizon_minutes" ?disabled=${d} @change=${(e: Event) => this.set("horizon_minutes", Number((e.target as HTMLSelectElement).value))}>
                ${horizons.map((x) => html`<option value=${x} ?selected=${this.value("horizon_minutes") === x}>${x} min</option>`)}</select></label>
            <label class="field"><span>Confidence needed before predicting</span>
              <select data-field="confidence_threshold" ?disabled=${d} @change=${(e: Event) => this.set("confidence_threshold", (e.target as HTMLSelectElement).value)}>
                ${levels.map((x) => html`<option value=${x} ?selected=${this.value("confidence_threshold") === x}>${CONFIDENCE[x]?.label ?? x}</option>`)}</select></label>
            ${NUMBER_FIELDS.map((f) => {
              const lim = limits[f.key];
              return html`<label class="field"><span>${f.label}</span>
                <input type="number" inputmode="numeric" data-field=${f.key} min=${lim?.min ?? nothing} max=${lim?.max ?? nothing} step="1" .value=${String(this.value(f.key) ?? "")} ?disabled=${d}
                  @change=${(e: Event) => { const raw = (e.target as HTMLInputElement).value.trim(); if (raw !== "") this.set(f.key, Number(raw) as never); }} />
                <span class="small muted">${f.unit} · ${f.help}${lim ? ` (${lim.min}–${lim.max}, default ${lim.default})` : ""}</span></label>`;
            })}
          </div>
          <h4>Context entities</h4>
          <p class="muted small">Entities whose on/off state changes how likely entries and exits are (people at home, a work-from-home switch, …). Automatic: the integration picks persons and the workday sensor. At most ${maxContexts}.</p>
          <div class="pr-row">
            <label class="pr-row"><input type="radio" name="pr-contexts" ?checked=${contexts === null || contexts === undefined} ?disabled=${d} @change=${() => this.set("contexts", null)} /> Automatic${this.detail?.settings.automatic_contexts.length ? html` <span class="small muted">(${this.detail.settings.automatic_contexts.map((c) => c.name).join(", ")})</span>` : nothing}</label>
            <label class="pr-row"><input type="radio" name="pr-contexts" ?checked=${Array.isArray(contexts)} ?disabled=${d} @change=${() => this.set("contexts", [...(this.detail?.settings.contexts.map((c) => c.entity_id) ?? [])])} /> Chosen by hand</label>
          </div>
          ${Array.isArray(contexts) ? html`<div class="pr-chips" data-testid="predictions-contexts">
              ${contexts.map((id) => html`<span class="pr-chip-remove">${name(id)}<button type="button" aria-label="Remove ${name(id)}" ?disabled=${d} @click=${() => this.set("contexts", contexts.filter((x) => x !== id))}>${icon("close", 14)}</button></span>`)}
              ${contexts.length < maxContexts ? html`<select class="pr-inline-select" aria-label="Add a context entity" ?disabled=${d || !candidates.length} @change=${(e: Event) => { const v = (e.target as HTMLSelectElement).value; if (v && !contexts.includes(v)) this.set("contexts", [...contexts, v]); (e.target as HTMLSelectElement).value = ""; }}>
                <option value="">Add an entity…</option>${candidates.filter((c) => !contexts.includes(c.entity_id)).map((c) => html`<option value=${c.entity_id}>${c.name} (${c.entity_id})</option>`)}</select>` : html`<span class="small muted">Maximum reached</span>`}
              ${!candidates.length ? html`<span class="small muted">Candidates load with a room (open the Patterns tab once).</span>` : nothing}
            </div>` : nothing}
          <h4>Workday source</h4>
          <p class="muted small">Tells workdays from days off, so the model learns both patterns. Automatic uses the Workday integration's sensor when there is one.</p>
          <div class="pr-row">
            <select class="pr-inline-select" data-field="workday_entity" ?disabled=${d} @change=${(e: Event) => { const v = (e.target as HTMLSelectElement).value; this.set("workday_entity", v === "__auto" ? null : v === "__none" ? "" : v); }}>
              <option value="__auto" ?selected=${workday === null || workday === undefined}>Automatic${this.detail?.settings.workday_automatic && this.detail.settings.workday_name ? ` (${this.detail.settings.workday_name})` : ""}</option>
              <option value="__none" ?selected=${workday === ""}>None (every day alike)</option>
              ${candidates.filter((c) => c.workday || c.entity_id === workday).map((c) => html`<option value=${c.entity_id} ?selected=${workday === c.entity_id}>${c.name} (${c.entity_id})</option>`)}
              ${candidates.filter((c) => !c.workday && c.entity_id.startsWith("binary_sensor.") && c.entity_id !== workday).slice(0, 60).map((c) => html`<option value=${c.entity_id}>${c.name} (${c.entity_id})</option>`)}
            </select>
            ${workday && candidates.length && !candidates.some((c) => c.entity_id === workday) ? html`<span class="small muted">${workday}</span>` : nothing}
          </div>
          <div class="pr-actions">
            <button type="button" class="btn btn-primary" data-testid="predictions-save-global" ?disabled=${d || !dirty} @click=${() => void this.saveGlobal()}>${icon("save", 18)}${this.busy ? "Saving…" : "Save global defaults"}</button>
            <button type="button" class="btn" ?disabled=${!dirty || this.busy} @click=${() => (this.draft = null)}>Discard</button>
            ${this.notice ? html`<span class="small tone-ok">${this.notice}</span>` : nothing}
          </div>
        </section>
        <section class="card wide"><header class="card-head">${icon("grid")}<h3>Room overrides</h3></header>
          <p class="muted small">A room can set its own value for these; "Inherit" follows the global default. Changes are saved at once.</p>
          ${overridesTable(h.rooms, h.settings?.rooms ?? {}, g, this, { horizons, confidenceOptions: levels, thresholdMin: 5, thresholdMax: 99 })}
        </section>
        <section class="card wide"><header class="card-head">${icon("link")}<h3>Room connections</h3></header>
          ${this.selector("Room")}
          ${this.roomDetailOr((det) => connectionsEditor(det.area_id, det.connections, this))}
        </section>
        <section class="card"><header class="card-head">${icon("sync")}<h3>Learning</h3></header>
          <p class="muted small">Learning reads completed days from Home Assistant's Recorder. "Learn now" reads what is new; a rebuild forgets every learned day and reads the whole window again.</p>
          <div class="pr-actions">
            <button type="button" class="btn" data-testid="predictions-learn" ?disabled=${d} @click=${() => void this.run(() => forecast(cmd("learn")), "Learned the latest history")}>${icon("refresh", 18)}Learn now</button>
            <button type="button" class="btn btn-danger" data-testid="predictions-rebuild" ?disabled=${d} @click=${() => void this.openDialog({ kind: "rebuild" })}>${icon("restore", 18)}Rebuild from history…</button>
          </div>
          ${kv([["Learning", learningChip(h.learning.state)], ["Detail", h.learning.detail], ["Recorder queries", h.learning.queries], ["Last learning took", h.learning.last_duration_seconds !== null && h.learning.last_duration_seconds !== undefined ? `${h.learning.last_duration_seconds} s` : null]])}
        </section>
        <section class="card"><header class="card-head">${icon("export")}<h3>Export and import</h3></header>
          <p class="muted small">The learned days and settings as a file. Importing replaces the learned days (a backup is kept); it is possible only while the App produces the forecasts.</p>
          <div class="pr-actions">
            <button type="button" class="btn" data-testid="predictions-export" ?disabled=${!this.canEdit} @click=${() => download("api/predictions/export")}>${icon("download", 18)}Export learned data</button>
            <label class="btn file-btn ${d ? "disabled" : ""}" data-testid="predictions-import">${icon("file-upload", 18)}Import a file…<input type="file" accept="application/json,.json" ?disabled=${d} @change=${(e: Event) => void this.chooseImport(e)} /></label>
          </div>
        </section>
      </div>
    </div>`;
  }

  private dialogTemplate(): TemplateResult | typeof nothing {
    const d = this.dialog;
    if (!d) return nothing;
    const title = d.kind === "rebuild" ? "Rebuild the model from history" : "Import learned data";
    return html`<dialog class="sheet pr-dialog" aria-labelledby="pr-dialog-title" data-testid="predictions-settings-dialog" @close=${() => (this.dialog = null)}>
      <div class="sheet-inner">
        <header class="sheet-head"><h2 id="pr-dialog-title">${title}</h2><button type="button" class="icon-btn" aria-label="Close" @click=${() => this.closeDialog()}>${icon("close")}</button></header>
        <div class="sheet-body">
          ${this.error ? problemNotice(this.error) : nothing}
          ${d.kind === "rebuild" ? html`<p>Every learned day is forgotten and the whole history window is read again from Home Assistant's Recorder. Days the Recorder no longer holds are lost for good; the forecasts show "learning" until the rebuild finishes. Nothing is switched: forecasts command nothing.</p>`
          : d.error ? html`<p class="notice notice-error">${icon("error", 20)}<span>${d.error}</span></p>`
          : !d.review ? html`<p class="muted">Checking the file…</p>`
          : html`${kv([["Learned days in the file", d.review.days], d.review.invalid_days ? ["Invalid days (skipped)", d.review.invalid_days] : null,
              ["Exported", d.review.exported_at ? haTime()(d.review.exported_at) : "—"], ["Learned days now", d.review.current_days],
              ["Same data", d.review.same ? chip("ok", "Identical: nothing to import") : chip("warning", "Different")],
              ["Writable", d.review.writable ? chip("ok", "The App produces the forecasts") : chip("error", "Hand the forecasts over to the App first")]])}
            <p class="small muted">Importing replaces the learned days with the file's and rebuilds the model; a backup of the current data is kept in the App.</p>`}
        </div>
        <footer class="sheet-foot">
          <button type="button" class="btn" @click=${() => this.closeDialog()}>Cancel</button>
          ${d.kind === "rebuild"
            ? html`<button type="button" class="btn btn-danger" data-testid="predictions-rebuild-confirm" ?disabled=${this.busy} @click=${() => void this.rebuild()}>${icon("restore", 18)}${this.busy ? "Rebuilding…" : "Forget and rebuild"}</button>`
            : html`<button type="button" class="btn btn-primary" data-testid="predictions-import-confirm" ?disabled=${this.busy || !d.review || d.review.same || !d.review.writable} @click=${() => void this.confirmImport()}>${icon("import", 18)}${this.busy ? "Importing…" : "Replace the learned data"}</button>`}
        </footer>
      </div>
    </dialog>`;
  }

  // ------------------------------------------------------------- render
  protected render(): unknown {
    const tab = this.currentTab;
    const h = this.house;
    const header = pageHeader("Predictions", "What the house has learned about when its rooms are occupied, and how likely each room is to be occupied soon.",
      html`<div class="page-actions"><a class="btn" href="#/predictions/control" data-testid="predictions-control-button">${icon("swap", 18)}Control</a></div>`);
    const body = !this.status
      ? (this.error ? problemNotice(this.error) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`)
      : !h ? html`<p class="notice notice-warning" data-testid="predictions-house-error">${icon("warning", 20)}<span>${this.status.house_error ?? "The forecasts are not available yet."}</span></p>`
      : tab === "house" ? this.houseTab(h)
      : tab === "patterns" ? html`<div data-testid="predictions-patterns">${this.selector("Room")}${this.roomDetailOr((d) => patternsPanel(d))}</div>`
      : tab === "accuracy" ? html`<div data-testid="predictions-accuracy">${houseAccuracyPanel(h.accuracy)}<h3 class="pr-row" style="margin:1rem 0 0.5rem">Per room</h3>${this.selector("Room")}
          ${this.roomDetailOr((d) => roomAccuracyPanel(d.accuracy, d.settings.effective.probability_threshold))}</div>`
      : this.settingsTab(h);
    return html`${header}
      ${controlBanner(this.status)}
      ${observationNotice()}
      <nav class="tabs" aria-label="Predictions">
        ${TABS.map((t) => html`<a href="#/predictions/${t.key}" class=${t.key === tab ? "active" : ""} aria-current=${t.key === tab ? "page" : "false"} data-tab=${t.key}>${icon(t.icon, 20)}${t.title}</a>`)}
      </nav>
      ${this.status?.notices.map((n) => html`<p class="notice notice-warning">${icon("warning", 20)}<span>${n.text}</span></p>`)}
      ${this.error && this.status && !this.dialog ? problemNotice(this.error) : nothing}
      ${this.notice && tab !== "settings" ? html`<p class="notice" role="status">${icon("ok", 20)}<span>${this.notice}</span></p>` : nothing}
      ${body}
      ${this.dialogTemplate()}`;
  }
}

customElements.define("sl-predictions", SlPredictions);
