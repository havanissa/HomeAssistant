// Rooms overview (Phase A2): every Home Assistant area with its room
// configuration status, occupancy, assignments, device health, who runs the
// automation and what needs attention. Opens the room workspace.
import { html, nothing, type TemplateResult } from "lit";

import { formatTime, plural } from "../format";
import { icon, ICONS } from "../icons";
import { normalize } from "../lookup";
import { navigatePath } from "../router";
import { store } from "../store";
import {
  ApiError,
  downloadJson,
  getRooms,
  OCCUPANCY_LABEL,
  postRooms,
  STATUS,
  type Backup,
  type BackupInfo,
  type RestoreReview,
  type RoomsOverview,
  type RoomStatus,
  type RoomSummary,
} from "../rooms/model";
import { chip, pageHeader, StoreElement } from "../ui";

type Filter = "all" | RoomStatus;
type Sort = "name" | "status" | "occupancy" | "attention";

const FILTERS: Array<{ value: Filter; label: string }> = [
  { value: "all", label: "All" },
  { value: "configured", label: "Configured" },
  { value: "not_configured", label: "Not configured" },
  { value: "disabled", label: "Disabled" },
  { value: "problem", label: "Problem" },
];
const SORTS: Array<{ value: Sort; label: string }> = [
  { value: "name", label: "Name" },
  { value: "status", label: "Status" },
  { value: "occupancy", label: "Occupancy" },
  { value: "attention", label: "Needs attention" },
];
const STATUS_ORDER: Record<RoomStatus, number> = { problem: 0, configured: 1, disabled: 2, not_configured: 3 };

export function areaIcon(name: string | null | undefined): string {
  const key = (name ?? "").replace(/^mdi:/, "");
  return key && ICONS[key] ? key : "sofa";
}

export function occupancyChip(o: RoomSummary["occupancy"] | null | undefined): TemplateResult {
  if (!o || !o.state) return chip("neutral", "No occupancy data");
  const label = OCCUPANCY_LABEL[o.state] ?? o.state;
  return chip(o.occupied ? "ok" : o.state === "possible_occupancy" ? "info" : "neutral", label);
}

export function apiNotice(status: RoomsOverview["status"] | undefined): unknown {
  if (!status) return nothing;
  if (status.api === "unsupported")
    return html`<p class="notice notice-warning" data-testid="rooms-api-notice">${icon("warning", 20)}<span>${status.error} Update Smart Occupancy &
        Lighting (HACS) to edit rooms here; everything below is read from Home Assistant.</span></p>`;
  if (status.api === "error" || status.api === "unavailable")
    return html`<p class="notice notice-warning" data-testid="rooms-api-notice">${icon("warning", 20)}<span>The integration's room configuration
        could not be read: ${status.error}</span></p>`;
  if (!status.loaded)
    return html`<p class="notice notice-warning" data-testid="rooms-api-notice">${icon("warning", 20)}<span>Smart Occupancy & Lighting is not
        running (state: ${status.integration_state ?? "unknown"}). Rooms are shown from its stored configuration; saving is
        possible again once it runs.</span></p>`;
  return nothing;
}

export class SlRooms extends StoreElement {
  static properties = {
    data: { state: true },
    error: { state: true },
    query: { state: true },
    filter: { state: true },
    sort: { state: true },
    backupsOpen: { state: true },
  };
  declare data: RoomsOverview | null;
  declare error: string | null;
  declare query: string;
  declare filter: Filter;
  declare sort: Sort;
  declare backupsOpen: boolean;
  private seenRooms = -1;
  private seenRegistries = -1;
  private loading = false;

  constructor() {
    super();
    this.data = null;
    this.error = null;
    this.query = "";
    this.filter = "all";
    this.sort = "name";
    this.backupsOpen = false;
  }

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }

  async load(): Promise<void> {
    if (this.loading) return;
    this.loading = true;
    this.seenRooms = store.roomsVersion;
    this.seenRegistries = store.registriesVersion;
    try {
      this.data = await getRooms<RoomsOverview>("api/rooms");
      this.error = null;
    } catch (err) {
      this.error = err instanceof ApiError ? err.message : String(err);
    } finally {
      this.loading = false;
    }
  }

  protected willUpdate(): void {
    // Live: reload when the configuration (also from the integration's own editor) or the registries change.
    if (this.data && (store.roomsVersion !== this.seenRooms || store.registriesVersion !== this.seenRegistries)) void this.load();
  }

  private visible(): RoomSummary[] {
    const words = normalize(this.query).split(/\s+/).filter(Boolean);
    const rooms = (this.data?.rooms ?? []).filter((r) => {
      if (this.filter !== "all" && r.status !== this.filter) return false;
      if (!words.length) return true;
      const text = normalize([r.name, r.area_id, ...r.sensor_ids, ...r.light_ids].join(" "));
      return words.every((w) => text.includes(w));
    });
    const attention = (r: RoomSummary) => r.warnings.filter((w) => w.level !== "info").length;
    const occ = (r: RoomSummary) => (r.occupancy?.occupied ? 0 : r.occupancy?.state === "possible_occupancy" ? 1 : r.occupancy ? 2 : 3);
    const byName = (a: RoomSummary, b: RoomSummary) => a.name.localeCompare(b.name);
    return rooms.sort((a, b) => {
      if (this.sort === "status") return STATUS_ORDER[a.status] - STATUS_ORDER[b.status] || byName(a, b);
      if (this.sort === "occupancy") return occ(a) - occ(b) || byName(a, b);
      if (this.sort === "attention") return attention(b) - attention(a) || byName(a, b);
      return byName(a, b);
    });
  }

  private open(r: RoomSummary, event: MouseEvent): void {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey) return;
    event.preventDefault();
    navigatePath("rooms", r.area_id);
  }

  private renderCard(r: RoomSummary): TemplateResult {
    const status = STATUS[r.status];
    const issues = r.warnings.filter((w) => w.level !== "info");
    const d = r.devices;
    const lighting = r.automation.lighting;
    return html`<a
      class="room-card card status-${r.status}"
      href="#/rooms/${encodeURIComponent(r.area_id)}"
      data-room=${r.area_id}
      @click=${(e: MouseEvent) => this.open(r, e)}
    >
      <header class="room-card-head">
        <span class="room-icon">${icon(areaIcon(r.icon), 22)}</span>
        <span class="room-title">
          <span class="room-name">${r.name}</span>
          ${r.area_exists ? nothing : html`<span class="small tone-error">Area deleted</span>`}
        </span>
        ${chip(status.tone, status.label)}
      </header>
      <div class="room-occupancy">${r.configured && r.enabled ? occupancyChip(r.occupancy) : html`<span class="muted small">Occupancy not observed</span>`}
        ${r.occupancy?.reason ? html`<span class="muted small room-reason">${r.occupancy.reason}</span>` : nothing}</div>
      <dl class="room-stats">
        <div><dt>${icon("motion-sensor", 18)}<span class="visually-hidden">Sensors</span></dt><dd>${plural(r.sensors, "sensor", "sensors")}</dd></div>
        <div><dt>${icon("lightbulb", 18)}<span class="visually-hidden">Lights</span></dt><dd>${plural(r.lights, "light", "lights")}</dd></div>
        <div title="Devices in this room and of its assigned entities">
          <dt>${icon("home", 18)}<span class="visually-hidden">Devices</span></dt>
          <dd>
            <span class=${d.online ? "tone-ok" : "muted"}>${d.online} online</span>${d.offline ? html` · <span class="tone-error">${d.offline} offline</span>` : nothing}${d.partial || d.unavailable_entities
              ? html` · <span class="tone-warning">${d.unavailable_entities} unavailable</span>`
              : nothing}${d.missing_entities ? html` · <span class="tone-error">${d.missing_entities} missing</span>` : nothing}
          </dd>
        </div>
      </dl>
      <p class="room-owner small muted">
        ${icon(lighting === "automatic" ? "robot" : "observe", 16)}
        <span
          >${r.configured
            ? html`Automation: Smart Occupancy integration${r.automation.running ? "" : " (not running)"} · lighting
              ${lighting ?? "unknown"}`
            : r.unassigned_candidates
              ? html`${plural(r.unassigned_candidates, "sensor or light", "sensors or lights")} found in this area`
              : html`No sensors or lights found in this area`}</span
        >
      </p>
      ${issues.length
        ? html`<ul class="room-warnings">
            ${issues.slice(0, 2).map((w) => html`<li class="tone-${w.level === "error" ? "error" : "warning"}">${icon(w.level === "error" ? "error" : "warning", 16)}<span>${w.title}${w.entity_id ? html` · <span class="muted">${w.entity_id}</span>` : nothing}</span></li>`)}
            ${issues.length > 2 ? html`<li class="muted">+ ${issues.length - 2} more</li>` : nothing}
          </ul>`
        : nothing}
    </a>`;
  }

  protected render(): unknown {
    const data = this.data;
    const rooms = data ? this.visible() : [];
    const counts = new Map<Filter, number>([["all", data?.rooms.length ?? 0]]);
    for (const r of data?.rooms ?? []) counts.set(r.status, (counts.get(r.status) ?? 0) + 1);
    return html`
      ${pageHeader(
        "Rooms",
        "Every Home Assistant area, its devices and its room configuration. Changes are saved in the Smart Occupancy integration, which keeps running all automation.",
        html`<div class="actions">
          <button type="button" class="btn" @click=${() => void this.load()} aria-label="Refresh">${icon("refresh", 20)}<span>Refresh</span></button>
          <button type="button" class="btn" data-testid="open-backups" @click=${() => (this.backupsOpen = true)}>
            ${icon("history", 20)}<span>Backups</span>
          </button>
        </div>`,
      )}
      ${this.error ? html`<p class="notice notice-error" role="alert">${icon("error", 20)}<span>${this.error}</span></p>` : nothing}
      ${apiNotice(data?.status)}
      <div class="rooms-toolbar">
        <label class="search-field">
          ${icon("search", 20)}
          <input type="search" placeholder="Search rooms, sensors or lights" aria-label="Search rooms" .value=${this.query}
            @input=${(e: Event) => (this.query = (e.target as HTMLInputElement).value)} />
        </label>
        <fieldset class="segmented" aria-label="Filter by status">
          <legend class="visually-hidden">Filter by status</legend>
          ${FILTERS.map(
            (f) => html`<label class=${this.filter === f.value ? "selected" : ""}>
              <input type="radio" name="room-filter" .checked=${this.filter === f.value} @change=${() => (this.filter = f.value)} />
              ${f.label} <span class="count">${counts.get(f.value) ?? 0}</span>
            </label>`,
          )}
        </fieldset>
        <label class="select-field">
          <span>Sort</span>
          <select aria-label="Sort rooms" .value=${this.sort} @change=${(e: Event) => (this.sort = (e.target as HTMLSelectElement).value as Sort)}>
            ${SORTS.map((s) => html`<option value=${s.value} ?selected=${this.sort === s.value}>${s.label}</option>`)}
          </select>
        </label>
      </div>
      ${!data && !this.error
        ? html`<div class="card loading" role="status"><span class="spinner" aria-hidden="true"></span><span>Loading rooms…</span></div>`
        : nothing}
      ${data && !rooms.length
        ? html`<div class="card empty-state">
            ${icon("floor-plan", 40)}
            <h3>${data.rooms.length ? "No room matches" : "No areas in Home Assistant yet"}</h3>
            <p class="muted">
              ${data.rooms.length
                ? "Change the search or the status filter."
                : "Create areas under Settings → Areas, labels & zones and put your devices in them. They appear here automatically."}
            </p>
          </div>`
        : nothing}
      <div class="room-grid" data-testid="room-grid">${rooms.map((r) => this.renderCard(r))}</div>
      ${data ? html`<p class="muted small">Read ${formatTime(data.generated_at)} · ${data.status.integration_version ? `integration ${data.status.integration_version}` : "integration version unknown"}</p>` : nothing}
      ${this.backupsOpen ? html`<sl-room-backups @close=${() => ((this.backupsOpen = false), void this.load())}></sl-room-backups>` : nothing}
    `;
  }
}

customElements.define("sl-rooms", SlRooms);

// ---------------------------------------------------------------- backups
export class SlRoomBackups extends StoreElement {
  static properties = {
    backups: { state: true },
    review: { state: true },
    pending: { state: true },
    confirmed: { state: true },
    error: { state: true },
    done: { state: true },
    busy: { state: true },
  };
  declare backups: BackupInfo[] | null;
  declare review: RestoreReview | null;
  declare pending: Backup | null;
  declare confirmed: Set<string>;
  declare error: string | null;
  declare done: string | null;
  declare busy: boolean;

  constructor() {
    super();
    this.backups = null;
    this.review = null;
    this.pending = null;
    this.confirmed = new Set();
    this.error = null;
    this.done = null;
    this.busy = false;
  }

  async connectedCallback(): Promise<void> {
    super.connectedCallback();
    await this.updateComplete;
    this.querySelector<HTMLDialogElement>("dialog")?.showModal();
    try {
      this.backups = (await getRooms<{ backups: BackupInfo[] }>("api/rooms/backups")).backups;
    } catch (err) {
      this.error = String(err);
      this.backups = [];
    }
  }

  private close(): void {
    this.querySelector<HTMLDialogElement>("dialog")?.close();
  }

  private async export(): Promise<void> {
    try {
      const data = await getRooms<Backup>("api/rooms/export");
      downloadJson(data, `smart-occupancy-rooms-${data.created_at.replace(/[:-]/g, "").slice(0, 15)}.json`);
    } catch (err) {
      this.error = String(err);
    }
  }

  private async download(id: string): Promise<void> {
    try {
      downloadJson(await getRooms<Backup>(`api/rooms/backups/${encodeURIComponent(id)}`), `smart-occupancy-${id}.json`);
    } catch (err) {
      this.error = String(err);
    }
  }

  private async reviewBackup(backup: Backup): Promise<void> {
    this.error = null;
    this.done = null;
    this.busy = true;
    try {
      this.review = await postRooms<RestoreReview>("api/rooms/restore/review", { backup });
      this.pending = backup;
      this.confirmed = new Set();
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
    } finally {
      this.busy = false;
    }
  }

  private async fromStored(id: string): Promise<void> {
    try {
      await this.reviewBackup(await getRooms<Backup>(`api/rooms/backups/${encodeURIComponent(id)}`));
    } catch (err) {
      this.error = String(err);
    }
  }

  private async fromFile(event: Event): Promise<void> {
    const file = (event.target as HTMLInputElement).files?.[0];
    if (!file) return;
    try {
      const data = JSON.parse(await file.text()) as Backup;
      await this.reviewBackup(data);
    } catch {
      this.error = "This file is not a valid room configuration backup (JSON).";
    }
  }

  private async restore(): Promise<void> {
    if (!this.review || !this.pending) return;
    this.busy = true;
    this.error = null;
    try {
      await postRooms("api/rooms/restore", { backup: this.pending, revision: this.review.revision, confirmed: [...this.confirmed] });
      this.done = `Restored. ${plural(this.review.rooms.length, "room was", "rooms were")} replaced; the integration reloads. The configuration before the restore was kept as a backup.`;
      this.review = null;
      this.pending = null;
      this.backups = (await getRooms<{ backups: BackupInfo[] }>("api/rooms/backups")).backups;
    } catch (err) {
      this.error = err instanceof ApiError && err.code === "stale_revision"
        ? "The room configuration changed after the review. Review the backup again."
        : err instanceof Error ? err.message : String(err);
      if (err instanceof ApiError && err.code === "stale_revision") this.review = null;
    } finally {
      this.busy = false;
    }
  }

  private renderReview(r: RestoreReview): unknown {
    const needs = r.requires_confirmation;
    const ready = !r.errors.length && needs.every((c) => this.confirmed.has(c)) && r.writable && !r.stale;
    return html`<section class="review-block" data-testid="restore-review">
      <h3>Review the restore</h3>
      ${!r.rooms.length ? html`<p class="muted">The backup is identical to the current configuration. Nothing to restore.</p>` : nothing}
      <ul class="change-list">
        ${r.rooms.map(
          (room) => html`<li>
            <strong>${room.name ?? room.area_id}</strong> · ${plural(room.changes.length, "change", "changes")}
            ${room.area_exists ? nothing : chip("warning", "Area no longer exists")}
            ${room.missing_entities.length ? html`<span class="small tone-warning block">Missing entities: ${room.missing_entities.join(", ")}</span>` : nothing}
          </li>`,
        )}
      </ul>
      ${r.errors.map((e) => html`<p class="notice notice-error">${icon("error", 20)}<span>${e.message}</span></p>`)}
      ${r.impacts.map((i) =>
        i.level === "confirm"
          ? html`<label class="confirm-row"><input type="checkbox" .checked=${this.confirmed.has(i.code)} @change=${(e: Event) => {
              const next = new Set(this.confirmed);
              if ((e.target as HTMLInputElement).checked) next.add(i.code);
              else next.delete(i.code);
              this.confirmed = next;
            }} /><span><strong>${i.title}</strong><span class="muted block">${i.detail}</span></span></label>`
          : html`<p class="notice notice-info">${icon("info", 20)}<span><strong>${i.title}</strong> ${i.detail}</span></p>`,
      )}
      <div class="actions">
        <button type="button" class="btn" @click=${() => ((this.review = null), (this.pending = null))}>Cancel</button>
        <button type="button" class="btn btn-primary" ?disabled=${!ready || this.busy || !r.rooms.length} data-testid="restore-confirm" @click=${() => void this.restore()}>
          ${icon("restore", 20)}Restore
        </button>
      </div>
    </section>`;
  }

  protected render(): unknown {
    return html`<dialog class="sheet sheet-full backups-dialog" aria-labelledby="backups-title"
        @close=${() => this.dispatchEvent(new CustomEvent("close"))}
        @click=${(e: MouseEvent) => (e.target as Element).matches("dialog") && this.close()}>
      <div class="sheet-inner">
        <header class="sheet-head">
          <h2 id="backups-title">Room configuration backups</h2>
          <button type="button" class="icon-btn" aria-label="Close" @click=${() => this.close()}>${icon("close")}</button>
        </header>
        <div class="sheet-body">
          <p class="muted">
            The App keeps the room configuration as it was before each change it saves (the latest 30). You can download
            the current configuration, or restore a backup after reviewing exactly what changes.
          </p>
          <div class="actions">
            <button type="button" class="btn" data-testid="export-rooms" @click=${() => void this.export()}>${icon("download", 20)}Export current configuration</button>
            <label class="btn file-btn">${icon("file-upload", 20)}Restore from a file…<input type="file" accept="application/json,.json" @change=${(e: Event) => void this.fromFile(e)} /></label>
          </div>
          ${this.error ? html`<p class="notice notice-error" role="alert">${icon("error", 20)}<span>${this.error}</span></p>` : nothing}
          ${this.done ? html`<p class="notice notice-info" role="status">${icon("ok", 20)}<span>${this.done}</span></p>` : nothing}
          ${this.review ? this.renderReview(this.review) : nothing}
          <h3 class="section-title">Automatic backups</h3>
          ${this.backups === null
            ? html`<p class="muted">Loading…</p>`
            : !this.backups.length
              ? html`<p class="muted">No backups yet. One is made before every change saved from this App.</p>`
              : html`<ul class="backup-list">
                  ${this.backups.map(
                    (b) => html`<li>
                      <span><strong>${formatTime(b.created_at)}</strong> · ${b.reason ?? "Backup"}<span class="muted small block">${plural(b.rooms, "room", "rooms")} · revision ${b.revision ?? "—"}</span></span>
                      <span class="row-actions">
                        <button type="button" class="btn" @click=${() => void this.download(b.id)}>${icon("download", 18)}Download</button>
                        <button type="button" class="btn" @click=${() => void this.fromStored(b.id)}>${icon("restore", 18)}Restore…</button>
                      </span>
                    </li>`,
                  )}
                </ul>`}
        </div>
      </div>
    </dialog>`;
  }
}

customElements.define("sl-room-backups", SlRoomBackups);
