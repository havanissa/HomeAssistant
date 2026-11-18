// Presence (Phase A3.1): every room's occupancy as the integration's engine
// decides it, global occupancy timing defaults and presence diagnostics.
// The engine runs in the integration; this page shows and configures it.
// Nothing here sends a device command.
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { setLeaveGuard } from "../router";
import { live, store } from "../store";
import { ApiError, getRooms, postRooms, type BackupInfo } from "../rooms/model";
import { chip, pageHeader, StoreElement } from "../ui";
import { basisChip, diagnosticItem, haTime, reviewDialog, stateChip, timingEditor, watchPresence } from "../presence/components";
import {
  ago,
  CONDITION_INFO,
  clock,
  clockOffset,
  countdown,
  duration,
  matches,
  secondsSince,
  timingChange,
  timingViews,
  type Diagnostic,
  type Filter,
  type Level,
  type PresenceOverview,
  type PresenceReview,
  type PresenceRoom,
  type Unit,
} from "../presence/model";

const FILTERS: Array<{ key: Filter; label: string }> = [
  { key: "all", label: "All" },
  { key: "occupied", label: "Occupied" },
  { key: "possible", label: "Possible" },
  { key: "vacant", label: "Vacant" },
  { key: "uncertain", label: "Uncertain" },
  { key: "unavailable", label: "Unavailable sensors" },
  { key: "problems", label: "Problems" },
];

/**
 * Reload a presence view when one of its watched entities changes (debounced), never on a timer: the room state
 * entity changes whenever the engine evaluates something new, including its own deadlines.
 */
export class LiveReload {
  private signature = "";
  private timer: number | undefined;
  private last = 0;
  constructor(private readonly reload: () => Promise<void>, private readonly ids: () => string[]) {}

  check(): void {
    const sig = this.ids().map((id) => store.liveStates.get(id)?.last_updated ?? "").join("|");
    if (sig === this.signature) return;
    const first = this.signature === "";
    this.signature = sig;
    if (first) return;
    window.clearTimeout(this.timer);
    const wait = Math.max(500, 1500 - (Date.now() - this.last));
    this.timer = window.setTimeout(() => {
      this.last = Date.now();
      void this.reload();
    }, wait);
  }

  stop(): void {
    window.clearTimeout(this.timer);
  }
}

export function apiProblem(error: string | null, code: string | null): TemplateResult {
  return html`<div class="card empty-state" role="alert" data-testid="presence-error">${icon(code === "unsupported" ? "shield-alert" : "error", 40)}
    <h3>${code === "unsupported" ? "The integration needs an update" : "Presence could not be loaded"}</h3>
    <p class="muted">${error}</p></div>`;
}

function sensorDot(condition: string): TemplateResult {
  return html`<span class="sig-dot sig-${condition}" aria-hidden="true"></span>`;
}

// ------------------------------------------------------------- dashboard
export class SlPresence extends StoreElement {
  static properties = { data: { state: true }, error: { state: true }, filter: { state: true }, query: { state: true }, tick: { state: true } };
  declare data: PresenceOverview | null;
  declare error: { message: string; code: string } | null;
  declare filter: Filter;
  declare query: string;
  declare tick: number;
  private offset = 0;
  private timer: number | undefined;
  private reloader = new LiveReload(() => this.load(), () => this.watchIds());
  private seenRooms = -1;

  constructor() {
    super();
    this.data = null;
    this.error = null;
    this.filter = "all";
    this.query = "";
    this.tick = 0;
  }

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
    this.timer = window.setInterval(() => (this.tick += 1), 1000);
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    window.clearInterval(this.timer);
    this.reloader.stop();
    live.watch([]);
  }

  private watchIds(): string[] {
    return (this.data?.rooms ?? []).flatMap((r) => [...Object.values(r.entities).filter(Boolean) as string[], ...r.sensors.map((s) => s.entity_id)]);
  }

  async load(): Promise<void> {
    try {
      const data = await getRooms<PresenceOverview>("api/presence");
      this.offset = clockOffset(data.now);
      const first = !this.data;
      this.data = data;
      this.error = null;
      if (first) watchPresence(data.rooms);
    } catch (err) {
      this.error = { message: err instanceof Error ? err.message : String(err), code: err instanceof ApiError ? err.code : "error" };
    }
  }

  protected willUpdate(): void {
    if (this.data) this.reloader.check();
    if (store.roomsVersion !== this.seenRooms) {
      const first = this.seenRooms === -1;
      this.seenRooms = store.roomsVersion;
      if (!first) void this.load();
    }
  }

  private card(r: PresenceRoom): TemplateResult {
    const occ = r.occupancy;
    const cd = countdown(r, this.offset);
    const since = occ?.state_since ? secondsSince(occ.state_since, this.offset) : null;
    const last = occ?.last_activity ? secondsSince(occ.last_activity, this.offset) : null;
    const top = r.diagnostics.filter((d) => d.level !== "info").slice(0, 2);
    const shown = r.signals.slice(0, 6);
    const href = `#/presence/${encodeURIComponent(r.area_id)}`;
    return html`<li class="presence-card ${occ ? `occ-border-${occ.state}` : "occ-border-none"}" data-room=${r.area_id} data-tags=${r.tags.join(" ")}>
      <a class="presence-card-link" href=${href} data-testid="presence-room-link">
        <header class="presence-card-head">
          <span class="presence-card-name">${r.name}</span>
          ${r.enabled ? stateChip(occ?.state, occ?.state_label) : chip("pending", "Not observed")}
        </header>
        <div class="presence-card-body">
          ${occ ? html`<div class="presence-meta">
              <span>${since !== null ? `For ${duration(Math.max(0, Math.floor(since / 60)) * 60 || Math.max(0, Math.floor(since)))}` : ""}</span>
              ${occ.confidence ? html`<span class="confidence" title=${`Evidence quality, not a probability: ${occ.confidence.basis}`}>
                  <span class="meter"><span style="width:${occ.confidence.score}%"></span></span><span class="small">${occ.confidence.score}</span></span>` : nothing}
            </div>
            ${cd ? html`<div class="countdown" data-testid="countdown"><span>${cd.label}</span><strong class="mono">${clock(cd.seconds)}</strong>
                ${cd.total ? html`<span class="meter thin"><span style="width:${Math.max(0, Math.min(100, 100 - (cd.seconds / cd.total) * 100))}%"></span></span>` : nothing}</div>` : nothing}
            <p class="small muted presence-reason">${occ.reason}</p>
            <p class="small">${last !== null ? html`Last activity ${ago(last)}${occ.last_activity_name ? html` · <span class="muted">${occ.last_activity_name}</span>` : nothing}` : html`<span class="muted">No activity recorded</span>`}</p>`
          : html`<p class="muted small">${r.enabled ? "The integration is not running this room's engine." : "This room is not enabled, so its occupancy is not observed."}</p>`}
          ${shown.length ? html`<ul class="sig-list">${shown.map((s) => html`<li title=${`${s.kind_name} · ${s.role_name}`}>${sensorDot(s.condition)}<span class="sig-name">${s.name}</span>
              <span class="small ${s.condition === "unavailable" || s.condition === "invalid" || s.condition === "stuck" ? "tone-warning" : "muted"}">${CONDITION_INFO[s.condition]?.label ?? s.condition}</span></li>`)}
              ${r.signals.length > shown.length ? html`<li class="muted small">+ ${r.signals.length - shown.length} more</li>` : nothing}</ul>`
            : r.enabled ? html`<p class="small tone-warning">No sensors assigned.</p>` : nothing}
        </div>
        <footer class="presence-card-foot small">
          <span>${r.counts.sensors} sensor${r.counts.sensors === 1 ? "" : "s"}${r.counts.unavailable ? html` · <span class="tone-warning">${r.counts.unavailable} unavailable</span>` : nothing}</span>
          <span>${r.counts.overrides ? `${r.counts.overrides} timing override${r.counts.overrides > 1 ? "s" : ""}` : "Global timing"}</span>
          ${r.automation.lighting_automatic ? chip("info", "Automatic lighting") : nothing}
        </footer>
        ${top.length ? html`<ul class="presence-card-warn">${top.map((d) => html`<li class="tone-${d.level}">${icon(d.level === "error" ? "error" : "warning", 16)}<span>${d.title}</span></li>`)}</ul>` : nothing}
      </a>
    </li>`;
  }

  protected render(): unknown {
    const data = this.data;
    const engine = store.engine ?? store.overview?.engine ?? null;
    const appDecides = engine?.owner === "app";
    const header = pageHeader("Presence", appDecides ? "Occupancy of every room, as the App's occupancy engine decides it."
      : "Occupancy of every room, as the Smart Occupancy integration decides it.", html`<div class="page-actions">
      <a class="btn" href="#/presence/engine" data-testid="presence-engine-link">${icon("swap", 20)}Engine ${chip(appDecides ? "ok" : "info", appDecides ? "App" : "Integration")}</a>
      <a class="btn" href="#/presence/diagnostics" data-testid="presence-diagnostics-link">${icon("stethoscope", 20)}Diagnostics${data ? html` ${chip(data.counts.problems ? "warning" : "ok", String(data.counts.problems))}` : nothing}</a>
      <a class="btn" href="#/presence/settings" data-testid="presence-settings-link">${icon("tune", 20)}Global timing</a></div>`);
    if (!data) return html`${header}${this.error ? apiProblem(this.error.message, this.error.code) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading presence…</span></div>`}`;
    const rooms = data.rooms.filter((r) => matches(r, this.filter, this.query));
    return html`${header}
      <p class="notice notice-info ownership" data-testid="presence-ownership">${icon("info", 20)}<span>${appDecides
        ? html`Room occupancy is decided by the <strong>App's occupancy engine</strong>; the Smart Occupancy integration (version ${data.integration_version}) publishes it and keeps its own engine ready to take over. Settings are stored in the integration. <a class="text-link" href="#/presence/engine">Engine migration</a>`
        : html`The occupancy engine runs in the <strong>Smart Occupancy integration</strong> (version ${data.integration_version})${engine?.mode === "shadow" ? html`; the App's engine runs next to it in shadow mode (<a class="text-link" href="#/presence/engine">compare</a>)` : nothing}. Here you see and configure it; settings are stored in the integration.`}
        ${data.loaded ? nothing : html`<strong class="tone-warning"> The integration is not running.</strong>`}</span></p>
      <div class="toolbar presence-toolbar">
        <div class="chip-choices filter-chips" role="group" aria-label="Filter rooms">
          ${FILTERS.map((f) => html`<label class=${this.filter === f.key ? "selected" : ""}><input type="radio" name="presence-filter" data-filter=${f.key} .checked=${this.filter === f.key} @change=${() => (this.filter = f.key)} />${f.label}
            <span class="count">${f.key === "all" ? data.rooms.length : data.counts[f.key]}</span></label>`)}
        </div>
        <input class="search-input" type="search" placeholder="Search rooms or sensors" aria-label="Search rooms or sensors" .value=${this.query} @input=${(e: Event) => (this.query = (e.target as HTMLInputElement).value)} />
      </div>
      ${rooms.length ? html`<ul class="presence-grid" data-testid="presence-grid">${rooms.map((r) => this.card(r))}</ul>`
        : html`<div class="card empty-state">${icon("filter", 36)}<p class="muted">${data.rooms.length ? "No room matches the filter." : "No room is configured yet. Set rooms up in Rooms first."}</p></div>`}
      ${data.unconfigured.length ? html`<section class="card unconfigured-card"><header class="card-head">${icon("home")}<h3>Areas without a room configuration</h3></header>
        <p class="muted small">Their occupancy is not observed. Assign sensors in the Rooms editor to start.</p>
        <ul class="link-chips">${data.unconfigured.map((a) => html`<li><a class="btn btn-small" href=${`#/rooms/${encodeURIComponent(a.area_id)}/presence`}>${a.name}</a></li>`)}</ul></section>` : nothing}`;
  }
}

customElements.define("sl-presence", SlPresence);

// ------------------------------------------------------------ diagnostics
interface DiagData {
  items: Diagnostic[];
  rooms: Array<{ area_id: string; name: string; tags: string[]; counts: PresenceRoom["counts"] }>;
  now: string;
}

export class SlPresenceDiagnostics extends StoreElement {
  static properties = { data: { state: true }, error: { state: true }, level: { state: true }, basis: { state: true } };
  declare data: DiagData | null;
  declare error: { message: string; code: string } | null;
  declare level: Level | "all";
  declare basis: string;
  private reloader = new LiveReload(() => this.load(), () => []);

  constructor() {
    super();
    this.data = null;
    this.error = null;
    this.level = "all";
    this.basis = "all";
  }

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.reloader.stop();
  }

  async load(): Promise<void> {
    try {
      this.data = await getRooms<DiagData>("api/presence/diagnostics");
      this.error = null;
    } catch (err) {
      this.error = { message: err instanceof Error ? err.message : String(err), code: err instanceof ApiError ? err.code : "error" };
    }
  }

  protected render(): unknown {
    const header = pageHeader("Presence diagnostics", "Why occupancy detection may not work as expected. Nothing here changes a setting.",
      html`<div class="page-actions"><a class="btn" href="#/presence">${icon("arrow-left", 20)}Presence</a>
        <button type="button" class="btn" @click=${() => void this.load()}>${icon("refresh", 20)}Check again</button></div>`);
    const data = this.data;
    if (!data) return html`${header}${this.error ? apiProblem(this.error.message, this.error.code) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Checking…</span></div>`}`;
    const items = data.items.filter((d) => (this.level === "all" || d.level === this.level) && (this.basis === "all" || d.basis === this.basis));
    const count = (l: Level): number => data.items.filter((d) => d.level === l).length;
    const byRoom = new Map<string, Diagnostic[]>();
    for (const d of items) {
      const key = d.room ?? "Home";
      byRoom.set(key, [...(byRoom.get(key) ?? []), d]);
    }
    return html`${header}
      <p class="muted small">${basisChip("measured")} the engine's current state or the sensors right now · ${basisChip("integration")} the integration's own checks · ${basisChip("heuristic")} a hint from the configuration that may be intended.</p>
      <div class="toolbar">
        <div class="chip-choices" role="group" aria-label="Severity">
          ${(["all", "error", "warning", "info"] as const).map((l) => html`<label class=${this.level === l ? "selected" : ""}><input type="radio" name="diag-level" .checked=${this.level === l} @change=${() => (this.level = l)} />${l === "all" ? "All" : l === "error" ? "Errors" : l === "warning" ? "Warnings" : "Info"}
            <span class="count">${l === "all" ? data.items.length : count(l)}</span></label>`)}
        </div>
        <label class="inline-field">Basis <select .value=${this.basis} @change=${(e: Event) => (this.basis = (e.target as HTMLSelectElement).value)}>
          <option value="all">All</option><option value="measured">Measured</option><option value="integration">Health checks</option><option value="heuristic">Suggestions</option></select></label>
      </div>
      ${items.length ? [...byRoom.entries()].map(([room, list]) => html`<section class="card diag-card" data-testid="diag-room"><header class="card-head">${icon("home")}<h3>${room}</h3>
          ${list[0].area_id ? html`<a class="btn btn-small" href=${`#/presence/${encodeURIComponent(list[0].area_id)}`}>${icon("motion-sensor", 16)}Open</a>` : nothing}</header>
          <ul class="diag-list">${list.map((d) => diagnosticItem(d, d.area_id ?? null))}</ul></section>`)
        : html`<div class="card empty-state">${icon("ok", 36)}<p class="muted">${data.items.length ? "Nothing matches the filter." : "No problems found."}</p></div>`}`;
  }
}

customElements.define("sl-presence-diagnostics", SlPresenceDiagnostics);

// ------------------------------------------------------------ global timing
export class SlPresenceSettings extends StoreElement {
  static properties = {
    data: { state: true }, error: { state: true }, draft: { state: true }, advanced: { state: true }, review: { state: true },
    reviewError: { state: true }, busy: { state: true }, confirmed: { state: true }, notice: { state: true }, backups: { state: true },
  };
  declare data: PresenceOverview | null;
  declare error: { message: string; code: string } | null;
  declare draft: Record<string, number | null>;
  declare advanced: boolean;
  declare review: PresenceReview | null;
  declare reviewError: { message: string; code: string } | null;
  declare busy: boolean;
  declare confirmed: Set<string>;
  declare notice: string | null;
  declare backups: BackupInfo[] | null;
  private units = new Map<string, Unit>();
  private reviewOpen = false;

  constructor() {
    super();
    this.data = null;
    this.error = null;
    this.draft = {};
    this.advanced = false;
    this.review = null;
    this.reviewError = null;
    this.busy = false;
    this.confirmed = new Set();
    this.notice = null;
    this.backups = null;
  }

  connectedCallback(): void {
    super.connectedCallback();
    setLeaveGuard(() => (this.dirty() ? "The global timing has unsaved changes. Leave without saving them?" : null));
    void this.load();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    setLeaveGuard(null);
  }

  private views() {
    const d = this.data;
    return d ? timingViews(d.timing_schema, "global", d.global.timing, {}, this.draft) : [];
  }

  dirty(): boolean {
    return Object.keys(timingChange(this.views())).length > 0;
  }

  async load(): Promise<void> {
    try {
      this.data = await getRooms<PresenceOverview>("api/presence");
      this.error = null;
    } catch (err) {
      this.error = { message: err instanceof Error ? err.message : String(err), code: err instanceof ApiError ? err.code : "error" };
    }
  }

  private async loadBackups(): Promise<void> {
    try {
      this.backups = ((await getRooms<{ backups: Array<BackupInfo & { occupancy?: boolean }> }>("api/rooms/backups")).backups).filter((b) => b.occupancy);
    } catch {
      this.backups = [];
    }
  }

  /** Put a backup's global timing into the draft (reviewed and saved like any edit). */
  private async useBackup(id: string): Promise<void> {
    const backup = await getRooms<{ occupancy?: Record<string, number> }>(`api/rooms/backups/${encodeURIComponent(id)}`);
    if (!backup.occupancy || !this.data) return;
    const next: Record<string, number | null> = {};
    for (const item of this.data.timing_schema) if (typeof backup.occupancy[item.key] === "number") next[item.key] = backup.occupancy[item.key];
    this.draft = next;
    this.notice = "The backup's values are in the editor. Review and save to apply them, or Discard.";
  }

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
      this.review = await postRooms<PresenceReview>("api/presence/review", { area_id: null, change: { timing: timingChange(this.views()) }, revision: this.data.global.revision });
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
    try {
      await postRooms("api/presence/save", { area_id: null, change: { timing: timingChange(this.views()) }, revision: this.data.global.revision, confirmed: [...this.confirmed] });
      this.draft = {};
      this.closeReview();
      this.notice = "Saved. The integration applied the new defaults at once to every room that inherits them.";
      await this.load();
    } catch (err) {
      const code = err instanceof ApiError ? err.code : "error";
      if (code === "stale_revision" || code === "confirmation_required") {
        await this.load();
        try {
          this.review = await postRooms<PresenceReview>("api/presence/review", { area_id: null, change: { timing: timingChange(this.views()) }, revision: this.data!.global.revision });
          this.confirmed = new Set();
        } catch {
          this.review = null; /* the error below explains */
        }
      }
      this.reviewError = {
        code,
        message: code === "stale_revision" ? "The global timing was changed elsewhere. The review below is against the current values." : code === "confirmation_required" ? "The consequences changed since the review. Check them again." : err instanceof Error ? err.message : String(err),
      };
    } finally {
      this.busy = false;
    }
  }

  protected render(): unknown {
    const header = pageHeader("Global occupancy timing", "Defaults for every room. A room can override any of them in its Presence workspace.",
      html`<div class="page-actions"><a class="btn" href="#/presence">${icon("arrow-left", 20)}Presence</a></div>`);
    const data = this.data;
    if (!data) return html`${header}${this.error ? apiProblem(this.error.message, this.error.code) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`}`;
    const views = this.views();
    const dirty = this.dirty();
    const invalid = views.some((v) => v.error);
    const inheriting: Record<string, string[]> = {};
    for (const item of data.timing_schema) inheriting[item.key] = data.rooms.filter((r) => r.enabled && !(item.key in r.timing_overrides)).map((r) => r.name);
    const overrides = data.rooms.filter((r) => Object.keys(r.timing_overrides).length);
    return html`${header}
      ${this.notice ? html`<p class="notice notice-info" role="status" data-testid="saved-notice">${icon("ok", 20)}<span>${this.notice}</span></p>` : nothing}
      ${dirty ? html`<div class="unsaved-bar" data-testid="unsaved-bar"><span class="unsaved-text">${icon("pencil", 20)}<strong>Unsaved changes</strong></span>
        <span class="row-actions"><button type="button" class="btn" @click=${() => ((this.draft = {}), (this.notice = null))}>Discard</button>
        <button type="button" class="btn btn-primary" data-testid="presence-review-open" ?disabled=${invalid || !data.writable} @click=${() => void this.openReview()}>${icon("check", 20)}Review and save</button></span></div>` : nothing}
      <div class="settings-layout">
        <section class="card wide"><header class="card-head">${icon("history")}<h3>Defaults</h3></header>
          ${timingEditor(views, {
            scope: "global", units: this.units, showAdvanced: this.advanced, onToggleAdvanced: () => (this.advanced = !this.advanced),
            onValue: (k, v) => ((this.draft = { ...this.draft, [k]: v }), (this.notice = null)),
            onUnit: (k, u) => (this.units.set(k, u), this.requestUpdate()), disabled: !data.writable, inheriting,
          })}
        </section>
        <aside class="settings-side">
          <section class="card"><header class="card-head">${icon("home")}<h3>Room overrides</h3></header>
            ${overrides.length ? html`<ul class="plain-list">${overrides.map((r) => html`<li><a class="text-link" href=${`#/presence/${encodeURIComponent(r.area_id)}?tab=timing`}>${r.name}</a>
                <span class="small muted block">${Object.entries(r.timing_overrides).map(([k, v]) => `${data.timing_schema.find((t) => t.key === k)?.label ?? k} ${duration(v)}`).join(" · ")}</span></li>`)}</ul>`
              : html`<p class="muted small">Every room uses these defaults.</p>`}
          </section>
          <section class="card"><header class="card-head">${icon("restore")}<h3>From a backup</h3></header>
            <p class="muted small">The App stores the global defaults with every room backup (before each change). Load a backup's values into the editor, then review and save.</p>
            ${this.backups === null ? html`<button type="button" class="btn" data-testid="load-backups" @click=${() => void this.loadBackups()}>${icon("history", 18)}Show backups</button>`
              : this.backups.length ? html`<ul class="plain-list">${this.backups.slice(0, 10).map((b) => html`<li><button type="button" class="link-btn" @click=${() => void this.useBackup(b.id)}>${haTime(false)(b.created_at)}</button><span class="small muted block">${b.reason ?? ""}</span></li>`)}</ul>`
                : html`<p class="muted small">No backup with global defaults yet.</p>`}
          </section>
        </aside>
      </div>
      ${this.reviewOpen ? reviewDialog({
        review: this.review, error: this.reviewError, busy: this.busy, confirmed: this.confirmed,
        onConfirm: (c, on) => { const n = new Set(this.confirmed); if (on) n.add(c); else n.delete(c); this.confirmed = n; },
        onSave: () => void this.save(), onClose: () => this.closeReview(),
        name: (id) => id ?? "—", kindLabel: (k) => String(k), roleLabel: (r) => String(r),
        timingLabel: (k) => data.timing_schema.find((t) => t.key === k)?.label ?? k,
      }) : nothing}`;
  }
}

customElements.define("sl-presence-settings", SlPresenceSettings);
