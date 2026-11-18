// Moods → Profiles (Phase A4): the profile editor. Choose the profile (Occupied / Vacant), the mood (or every mood)
// and the level (Global, one room's light types, one room's single lights); the table shows each target's own value,
// what it inherits and the result. Editing happens in the inspector and is saved with profile/set. Copy, reset,
// Vacant defaults, a per-room preview (nothing is sent) and an explicitly confirmed Test (sends real commands).
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { chip } from "../ui";
import { cmd, lighting } from "./api";
import { describeSetting, LightingElement, problemNotice, settingEditor, type Capabilities, type Setting } from "./components";
import {
  compact,
  editor,
  isUnset,
  moodChain,
  moodLabel,
  sameSetting,
  settingCell,
  sourceLabels,
  stateText,
  type EditorModel,
  type PreviewPlan,
  type ProfileName,
  type Resolved,
  type RoomLight,
  type TypeEntry,
} from "./moods-model";

type Scope = "global" | "room" | "light";

interface Row {
  id: string;
  name: string;
  sub: string;
  caps: Capabilities | null;
  own: Setting | null;
  inherited: Resolved | null;
  effective: Resolved | null;
  now?: Resolved | null;
  variant?: boolean;
  configured: boolean;
  hasLights: boolean;
}

const DECISION: Record<string, ["ok" | "info" | "warning" | "neutral" | "error", string]> = {
  change: ["info", "Would change"], unchanged: ["neutral", "Already so"], blocked: ["warning", "Blocked"],
  skipped: ["neutral", "Skipped"], error: ["error", "Error"],
};

export class SlMoodsProfiles extends LightingElement {
  static properties = {
    ...LightingElement.properties,
    model: { state: true }, roomModel: { state: true }, roomError: { state: true },
    profile: { state: true }, mood: { state: true }, scope: { state: true }, areaId: { state: true },
    selected: { state: true }, draft: { state: true }, showAll: { state: true }, dialog: { state: true },
    copyFrom: { state: true }, previewOpen: { state: true }, previewArea: { state: true }, preview: { state: true },
    previewError: { state: true }, understood: { state: true }, testResult: { state: true }, loading: { state: true },
  };
  declare model: EditorModel | null;
  declare roomModel: EditorModel | null;
  declare roomError: unknown;
  declare profile: ProfileName;
  declare mood: string | null;
  declare scope: Scope;
  declare areaId: string | null;
  declare selected: string | null;
  declare draft: Setting | null;
  declare showAll: boolean;
  declare dialog: "copy" | "reset" | "test" | null;
  declare copyFrom: { profile: ProfileName; mood: string };
  declare previewOpen: boolean;
  declare previewArea: string | null;
  declare preview: PreviewPlan | null;
  declare previewError: unknown;
  declare understood: boolean;
  declare testResult: { sent: string[]; errors: Record<string, string>; override_minutes: number; room: string } | null;
  declare loading: boolean;
  private draftFor: string | null = null;

  constructor() {
    super();
    this.model = null;
    this.roomModel = null;
    this.roomError = null;
    this.profile = "occupied";
    this.mood = null;
    this.scope = "global";
    this.areaId = null;
    this.selected = null;
    this.draft = null;
    this.showAll = false;
    this.dialog = null;
    this.copyFrom = { profile: "occupied", mood: "" };
    this.previewOpen = false;
    this.previewArea = null;
    this.preview = null;
    this.previewError = null;
    this.understood = false;
    this.testResult = null;
    this.loading = false;
  }

  private get editable(): boolean {
    return this.canEdit && (this.model?.can_edit ?? true);
  }

  private get all(): string {
    return this.model?.all_moods ?? "*";
  }

  async reload(): Promise<void> {
    this.loading = true;
    try {
      const model = await editor({ mood: this.mood, profile: this.profile });
      this.model = model;
      this.mood ??= model.selected_mood;
      if (!this.areaId || !model.rooms.some((r) => r.area_id === this.areaId)) this.areaId = model.rooms[0]?.area_id ?? null;
      if (this.scope !== "global" && this.areaId) {
        try {
          this.roomModel = await editor({ mood: this.mood, profile: this.profile, area_id: this.areaId });
          this.roomError = null;
        } catch (err) {
          this.roomModel = null;
          this.roomError = err;
        }
      }
      this.syncDraft();
      if (this.previewOpen) void this.loadPreview();
    } catch (err) {
      this.error = err;
    } finally {
      this.loading = false;
    }
  }

  /** Changing what is shown: drop the inspector and reload. */
  private choose(patch: Partial<{ profile: ProfileName; mood: string; scope: Scope; areaId: string }>): void {
    if (patch.profile !== undefined) this.profile = patch.profile;
    if (patch.mood !== undefined) this.mood = patch.mood;
    if (patch.scope !== undefined) this.scope = patch.scope;
    if (patch.areaId !== undefined) {
      this.areaId = patch.areaId;
      this.previewArea = patch.areaId;
    }
    this.selected = null;
    this.draft = null;
    this.draftFor = null;
    this.notice = null;
    this.testResult = null;
    void this.reload();
  }

  // ------------------------------------------------------------------ rows
  private rows(): Row[] {
    if (this.scope === "global") return (this.model?.types ?? []).map((t) => this.typeRow(t));
    const room = this.roomModel?.room;
    if (!room) return [];
    if (this.scope === "room") return room.types.map((t) => this.typeRow(t));
    return room.lights.filter((l) => !l.missing).map((l) => this.lightRow(l));
  }

  private typeRow(t: TypeEntry): Row {
    const n = t.lights.length;
    return {
      id: t.target, name: t.name, sub: `${n} light${n === 1 ? "" : "s"}${t.variant ? " · colour part" : ""}`,
      caps: t.caps, own: t.own, inherited: t.inherited, effective: t.effective, variant: t.variant,
      configured: t.configured, hasLights: n > 0,
    };
  }

  private lightRow(l: RoomLight): Row {
    return {
      id: l.entity_id, name: l.name, sub: [l.category_name ?? "No light type", l.rgb ? "RGB" : null, l.excluded ? "excluded" : null].filter(Boolean).join(" · "),
      caps: l.capabilities ?? null, own: l.own ?? null, inherited: l.inherited ?? null, effective: l.effective ?? null,
      now: l.now ?? null, configured: !!l.own, hasLights: true,
    };
  }

  private visible(rows: Row[]): Row[] {
    if (this.showAll || this.scope === "light") return rows;
    if (this.mood === this.all) return rows.filter((r) => r.configured || (!r.variant && r.hasLights));
    return rows.filter((r) => r.configured || (!r.variant && (r.hasLights || !isUnset(r.effective))));
  }

  private current(): Row | null {
    return this.selected ? this.rows().find((r) => r.id === this.selected) ?? null : null;
  }

  private syncDraft(): void {
    const row = this.current();
    if (!row) {
      if (this.selected && this.scope !== "light") this.selected = null;
      return;
    }
    // Keep the user's unsaved draft across live reloads; refresh it when the saved value changed underneath.
    const key = `${this.profile}|${this.mood}|${this.scope}|${this.areaId}|${row.id}`;
    if (this.draftFor !== key) {
      this.draftFor = key;
      this.draft = row.own ? { ...row.own } : null;
    }
  }

  private select(id: string): void {
    this.selected = id;
    this.draftFor = null;
    this.notice = null;
    this.syncDraft();
  }

  // ---------------------------------------------------------------- actions
  private async save(row: Row, setting: Setting | null): Promise<void> {
    const params: Record<string, unknown> = {
      scope: this.scope, mood: this.mood, target: row.id, setting: compact(setting), profile: this.profile,
    };
    if (this.scope !== "global") params.area_id = this.areaId;
    this.draftFor = null;
    await this.run(() => lighting(cmd("profile/set"), params), setting ? `Saved ${row.name}` : `Removed the entry of ${row.name}; it inherits again`);
  }

  private async copy(): Promise<void> {
    const params: Record<string, unknown> = {
      from_profile: this.copyFrom.profile, from_mood: this.copyFrom.mood, to_profile: this.profile, to_mood: this.mood,
    };
    if (this.scope !== "global") params.area_id = this.areaId;
    const ok = await this.run(async () => {
      const r = await lighting<{ types: number; lights: number }>(cmd("profile/copy"), params);
      return r;
    }, `Copied ${this.profileName(this.copyFrom.profile)} · ${this.copyFrom.mood} into ${this.profileName(this.profile)} · ${this.mood}`);
    if (ok) this.closeDialog();
  }

  private async reset(): Promise<void> {
    const params: Record<string, unknown> = { profile: this.profile, mood: this.mood };
    if (this.scope !== "global") params.area_id = this.areaId;
    let inherits: string[] = [];
    const ok = await this.run(async () => {
      inherits = (await lighting<{ inherits_from: string[] }>(cmd("profile/reset"), params)).inherits_from ?? [];
    });
    if (ok) {
      this.notice = `Reset: ${this.mood} ${this.levelName()} now inherits${inherits.length ? ` from ${inherits.join(" → ")}` : " nothing (no parent or fallback mood)"}.`;
      this.closeDialog();
    }
  }

  private async loadPreview(): Promise<void> {
    const area = this.previewArea ?? this.areaId;
    if (!area || !this.mood || this.mood === this.all) {
      this.preview = null;
      return;
    }
    this.previewError = null;
    try {
      const r = await lighting<{ plans: PreviewPlan[] }>(cmd("preview"), { area_id: area, mood: this.mood, profile: this.profile });
      this.preview = r.plans[0] ?? null;
    } catch (err) {
      this.preview = null;
      this.previewError = err;
    }
  }

  private async test(): Promise<void> {
    const area = this.previewArea ?? this.areaId;
    const room = this.model?.rooms.find((r) => r.area_id === area)?.name ?? area ?? "";
    let result: { sent: string[]; errors: Record<string, string>; override_minutes: number } | null = null;
    const ok = await this.run(async () => {
      result = await lighting(cmd("profile/test"), { area_id: area, profile: this.profile, mood: this.mood, confirm: true });
    });
    if (ok && result) {
      this.testResult = { ...(result as { sent: string[]; errors: Record<string, string>; override_minutes: number }), room };
      this.closeDialog();
      void this.loadPreview();
    }
  }

  private async openDialog(kind: "copy" | "reset" | "test"): Promise<void> {
    this.dialog = kind;
    this.understood = false;
    this.error = null;
    if (kind === "copy") {
      const other = this.model?.moods.find((m) => m.name !== this.mood && m.has_profile) ?? this.model?.moods.find((m) => m.name !== this.mood);
      this.copyFrom = this.profile === "vacant" ? { profile: "occupied", mood: this.mood ?? "" } : { profile: "occupied", mood: other?.name ?? "" };
    }
    if (kind === "test") await this.loadPreview();
    await this.updateComplete;
    this.querySelector<HTMLDialogElement>("dialog.mo-dialog")?.showModal();
  }

  private closeDialog(): void {
    this.querySelector<HTMLDialogElement>("dialog.mo-dialog")?.close();
    this.dialog = null;
  }

  private async setVacant(params: Record<string, unknown>, room: boolean): Promise<void> {
    await this.run(() => lighting(cmd("vacant/set"), room ? { area_id: this.areaId, ...params } : params),
      room ? "Saved the room's Vacant settings" : "Saved the Vacant defaults");
  }

  // ------------------------------------------------------------------ words
  private profileName(p: ProfileName): string {
    return p === "vacant" ? "Vacant" : "Occupied";
  }

  private roomName(): string {
    return this.model?.rooms.find((r) => r.area_id === this.areaId)?.name ?? this.areaId ?? "";
  }

  private levelName(): string {
    return this.scope === "global" ? "(global)" : `in ${this.roomName()}`;
  }

  private columns(): { inherited: string; own: string } {
    if (this.scope === "global") return { inherited: "Inherited", own: "Global value" };
    if (this.scope === "room") return { inherited: "Inherited (global)", own: "This room" };
    return { inherited: "Inherited (light type)", own: "This light" };
  }

  // ----------------------------------------------------------------- render
  private side(m: EditorModel): TemplateResult {
    const status = m.mood;
    const all = this.all;
    const seg = <T extends string>(name: string, value: T, current: T, label: string, onPick: () => void) => html`<label class=${value === current ? "selected" : ""}>
      <input type="radio" name=${name} .checked=${value === current} @change=${onPick} />${label}</label>`;
    return html`<aside class="mo-side" aria-label="What to edit">
      <div class="mo-side-section">
        <span class="mo-side-title">Profile</span>
        <fieldset class="segmented mo-seg"><legend class="visually-hidden">Profile</legend>
          ${seg("mo-profile", "occupied", this.profile, "Occupied", () => this.choose({ profile: "occupied" }))}
          ${seg("mo-profile", "vacant", this.profile, "Vacant", () => this.choose({ profile: "vacant" }))}</fieldset>
        <span class="small muted">${this.profile === "vacant" ? "What lights do in an empty room (rooms whose vacancy action is the Vacant profile)." : "What lights do while the room is occupied."}</span>
      </div>
      <div class="mo-side-section">
        <span class="mo-side-title">Mood <span class="muted small">· now ${status.manual_mode ? "Manual" : status.mood ?? "no mood"}</span></span>
        <ul class="mo-mood-list" role="listbox" aria-label="Mood">
          ${m.moods.map((x) => html`<li><button type="button" role="option" aria-selected=${x.name === this.mood} class="mo-mood-item ${x.name === this.mood ? "selected" : ""}"
              data-mood=${x.name} @click=${() => this.choose({ mood: x.name })}>
            <span class="mo-mood-name">${x.name}</span>
            ${x.name === status.mood ? html`<span class="mo-now">now</span>` : nothing}
            ${x.has_profile ? html`<span class="mo-dot" title="Has global profile entries" aria-label="has profile entries"></span>` : nothing}
            ${!x.in_main ? html`<span class="tone-warning" title="Not an option of Main Mood: it cannot become the active mood">${icon("warning", 16)}</span>` : nothing}
          </button></li>`)}
          <li><button type="button" role="option" aria-selected=${this.mood === all} class="mo-mood-item mo-every ${this.mood === all ? "selected" : ""}" data-mood=${all}
              @click=${() => this.choose({ mood: all })} title="Settings for every mood, e.g. follow the global brightness">
            <span class="mo-mood-name">Every mood</span><span class="small muted">${all}</span></button></li>
        </ul>
      </div>
      <div class="mo-side-section">
        <span class="mo-side-title">Level</span>
        <fieldset class="mo-levels"><legend class="visually-hidden">Level</legend>
          ${([["global", "Global", "home", "Light types everywhere"], ["room", "Room", "home-map-marker", "One room's light types"], ["light", "Single light", "lightbulb", "One light in a room"]] as const).map(([k, t, ic, sub]) => html`
            <label class="mo-level ${this.scope === k ? "selected" : ""}"><input type="radio" name="mo-scope" value=${k} .checked=${this.scope === k} @change=${() => this.choose({ scope: k })} />
              ${icon(ic, 18)}<span><span class="mo-level-name">${t}</span><span class="block small muted">${sub}</span></span></label>`)}</fieldset>
        ${this.scope !== "global" ? html`<label class="mo-field"><span>Room</span>
          <select data-testid="mo-room" @change=${(e: Event) => this.choose({ areaId: (e.target as HTMLSelectElement).value })}>
            ${m.rooms.map((r) => html`<option value=${r.area_id} ?selected=${r.area_id === this.areaId}>${r.name}</option>`)}</select></label>` : nothing}
        ${this.scope === "light" && this.roomModel?.room ? html`<label class="mo-field"><span>Light</span>
          <select data-testid="mo-light" @change=${(e: Event) => this.select((e.target as HTMLSelectElement).value)}>
            <option value="" ?selected=${!this.selected}>Choose a light…</option>
            ${this.roomModel.room.lights.filter((l) => !l.missing).map((l) => html`<option value=${l.entity_id} ?selected=${l.entity_id === this.selected}>${l.name}</option>`)}</select></label>` : nothing}
        <span class="small muted">Rooms inherit the global values; single lights inherit their light type in the room.</span>
      </div>
    </aside>`;
  }

  private toolbar(): TemplateResult {
    const every = this.mood === this.all;
    const ed = this.editable;
    return html`<div class="mo-toolbar">
      <div class="mo-toolbar-title"><h3>${this.profileName(this.profile)} · ${moodLabel(this.mood, this.all)}</h3>
        <span class="small muted">${this.scope === "global" ? "Global light types" : this.scope === "room" ? `Light types in ${this.roomName()}` : `Single lights in ${this.roomName()}`}</span></div>
      <div class="mo-toolbar-actions">
        ${ed ? html`<button type="button" class="btn btn-small" data-testid="mo-copy" ?disabled=${every || this.busy} @click=${() => void this.openDialog("copy")}
            title=${every ? "Choose a mood to copy into" : ""}>${icon("copy", 18)}Copy from…</button>
          <button type="button" class="btn btn-small" data-testid="mo-reset" ?disabled=${this.busy} @click=${() => void this.openDialog("reset")}>${icon("restore", 18)}Reset…</button>` : nothing}
        <button type="button" class="btn btn-small ${this.previewOpen ? "mo-on" : ""}" data-testid="mo-preview" aria-pressed=${this.previewOpen} ?disabled=${every}
          @click=${() => { this.previewOpen = !this.previewOpen; this.previewArea ??= this.areaId; if (this.previewOpen) void this.loadPreview(); }}>${icon("eye", 18)}Preview</button>
      </div>
    </div>`;
  }

  private table(): TemplateResult {
    if (this.scope !== "global" && this.roomError) return html`${problemNotice(this.roomError)}
      <p class="small muted">The room's profile model could not be loaded. <button type="button" class="btn btn-small" @click=${() => void this.reload()}>${icon("refresh", 16)}Try again</button></p>`;
    if (this.scope !== "global" && !this.roomModel) return html`<p class="muted" role="status">${this.areaId ? "Loading the room…" : "No room has lighting enabled yet."}</p>`;
    const rows = this.rows();
    const shown = this.visible(rows);
    const hidden = rows.length - shown.length;
    const cols = this.columns();
    const light = this.scope === "light";
    if (!rows.length) {
      return html`<div class="mo-empty">${icon("lightbulb-group", 32)}<p>${light ? "This room has no lights under automatic control." : html`No light types yet. Add them under <a href="#/moods/types">Light types</a>.`}</p></div>`;
    }
    if (!shown.length) {
      return html`<div class="mo-empty">${icon("lightbulb-group", 32)}<p>No light type has lights ${this.scope === "room" ? "in this room" : "yet"} or a value for this mood.</p>
        <button type="button" class="btn btn-small" data-testid="mo-show-all" @click=${() => (this.showAll = true)}>Show all ${rows.length} light type${rows.length === 1 ? "" : "s"}</button></div>`;
    }
    return html`<div class="table-wrap"><table class="plain-table mo-table" data-testid="mo-targets">
        <thead><tr><th scope="col">${light ? "Light" : "Light type"}</th><th scope="col">${cols.inherited}</th><th scope="col">${cols.own}</th><th scope="col">Result</th>${light ? html`<th scope="col">Now</th>` : nothing}</tr></thead>
        <tbody>${shown.map((r) => this.row(r, light))}</tbody></table></div>
      ${hidden || this.showAll ? html`<button type="button" class="btn btn-small mo-more" @click=${() => (this.showAll = !this.showAll)}>${this.showAll ? "Hide light types without lights or values" : `Show ${hidden} more (no lights here, or colour parts)`}</button>` : nothing}
      <p class="small muted mo-legend">${icon("info", 16)}<span>A value at this level overrides the inherited one; an empty cell inherits. ${this.mood === this.all
        ? "Every mood: what light types follow (for example the global brightness) unless a mood sets its own value. It never switches lights on or off."
        : "Moods inherit from their parent mood, then from the fallback mood (see Moods)."}</span></p>`;
  }

  private row(r: Row, light: boolean): TemplateResult {
    const all = this.all;
    const from = sourceLabels(r.effective, this.mood, all);
    return html`<tr class="${r.id === this.selected ? "selected" : ""} ${r.own ? "mo-has-own" : ""}" data-target=${r.id} @click=${() => this.select(r.id)}>
      <td><button type="button" class="mo-row-btn" aria-label="Edit ${r.name}" @click=${(e: Event) => { e.stopPropagation(); this.select(r.id); }}>
        <span class="mo-row-name">${r.name}</span><span class="block small muted">${r.sub}</span></button></td>
      <td>${settingCell(r.inherited, { empty: "Nothing (left unchanged)", from: sourceLabels(r.inherited, this.mood, all) })}</td>
      <td>${r.own ? html`<span class="inherit-tag override">${icon("pencil", 14)}set here</span> ${settingCell(r.own)}` : html`<span class="inherit-tag">${icon("arrow-left", 14)}inherits</span>`}</td>
      <td>${settingCell(r.effective, { empty: "Left unchanged", strong: true, from: r.own ? undefined : from })}</td>
      ${light ? html`<td>${settingCell(r.now, { empty: "—" })}</td>` : nothing}
    </tr>`;
  }

  private inspector(): TemplateResult | typeof nothing {
    const r = this.current();
    if (!r || !this.model) return nothing;
    const all = this.all;
    const every = this.mood === all;
    const chain = every || !this.mood ? [] : moodChain(this.model.moods, this.mood, this.model.settings.fallback_mood).slice(1);
    const dirty = !sameSetting(this.draft, r.own);
    const ed = this.editable;
    const levelWord = this.scope === "global" ? "Global" : this.scope === "room" ? this.roomName() : "This light";
    return html`<aside class="mo-inspector" aria-label="Edit ${r.name}" data-testid="mo-inspector">
      <header class="mo-insp-head">
        <span class="inspector-icon">${icon(this.scope === "light" ? "lightbulb" : "lightbulb-group", 22)}</span>
        <div><h3>${r.name}</h3><span class="small muted">${this.profileName(this.profile)} · ${moodLabel(this.mood, all)} · ${levelWord}</span></div>
        <button type="button" class="icon-btn" aria-label="Close" @click=${() => { this.selected = null; this.draftFor = null; }}>${icon("close")}</button>
      </header>
      <section class="mo-insp-section">
        <h4>Inheritance</h4>
        <ol class="mo-chain">
          <li><span class="mo-chain-level">Inherited</span>${settingCell(r.inherited, { empty: "Nothing: the light is left unchanged", from: sourceLabels(r.inherited, this.mood, all) })}</li>
          <li class=${r.own ? "mo-chain-own" : ""}><span class="mo-chain-level">${levelWord}</span>${r.own ? settingCell(r.own) : html`<span class="muted">Not set here (inherits)</span>`}</li>
          <li class="mo-chain-result"><span class="mo-chain-level">Result</span>${settingCell(r.effective, { empty: "Left unchanged", strong: true })}</li>
        </ol>
        ${chain.length ? html`<p class="small muted">${this.mood} inherits from ${chain.join(" → ")}.</p>` : nothing}
        ${r.now !== undefined ? html`<p class="small muted">Right now (current mood and global values): ${describeSetting(r.now)}</p>` : nothing}
      </section>
      <section class="mo-insp-section">
        <h4>${r.own ? "Value at this level" : "Set a value at this level"}</h4>
        ${ed ? nothing : html`<p class="small muted">Only Home Assistant administrators can change profiles.</p>`}
        ${settingEditor(this.draft, r.caps, (s) => (this.draft = s), { allMoods: every, vacant: this.profile === "vacant", disabled: !ed || this.busy })}
        ${this.profile === "vacant" && this.draft?.action === "on" ? html`<p class="small muted">Vacant: lights the profile switches on stay on while the room is empty (ambient is an Occupied setting).</p>` : nothing}
      </section>
      ${ed ? html`<footer class="mo-insp-foot">
        ${dirty ? html`<p class="small mo-review" data-testid="mo-review"><strong>Will save:</strong> ${this.draft ? describeSetting(this.draft) : "remove this entry (inherit again)"}
          <span class="block muted">Now: ${r.own ? describeSetting(r.own) : "not set (inherits)"}</span></p>` : html`<p class="small muted">No unsaved changes.</p>`}
        <div class="mo-foot-actions">
          <button type="button" class="btn" ?disabled=${!dirty || this.busy} @click=${() => { this.draft = r.own ? { ...r.own } : null; }}>${icon("undo", 18)}Discard</button>
          <button type="button" class="btn btn-primary" data-testid="mo-save" ?disabled=${!dirty || this.busy} @click=${() => void this.save(r, this.draft)}>${icon("save", 18)}${this.busy ? "Saving…" : "Save"}</button>
        </div>
        <span class="small muted">Saving never switches a light; the change is used at the next trigger.</span>
      </footer>` : nothing}
    </aside>`;
  }

  private vacantCard(m: EditorModel): TemplateResult | typeof nothing {
    if (this.profile !== "vacant") return nothing;
    const ed = this.editable && !this.busy;
    if (this.scope === "global") {
      const d = m.vacant_defaults;
      return html`<section class="card mo-card" data-testid="mo-vacant-defaults"><header class="card-head">${icon("tune")}<h3>Vacant defaults (all rooms)</h3></header>
        <div class="mo-form-grid">
          <label class="mo-field"><span>Lights the Vacant profile does not set</span>
            <select ?disabled=${!ed} @change=${(e: Event) => void this.setVacant({ others: (e.target as HTMLSelectElement).value }, false)}>
              <option value="off" ?selected=${d.others === "off"}>Turn off</option><option value="leave" ?selected=${d.others === "leave"}>Leave unchanged</option></select></label>
          <label class="mo-field"><span>Switch on only below (lx)</span>
            <input type="number" min="0" max="2000" step="1" placeholder="Each room's normal threshold" .value=${String(d.on_below ?? "")} ?disabled=${!ed}
              @change=${(e: Event) => { const v = (e.target as HTMLInputElement).value; void this.setVacant({ on_below: v === "" ? null : Number(v) }, false); }} /></label>
        </div>
        <p class="small muted">Rooms can override these (choose Level → Room).</p></section>`;
    }
    const v = this.roomModel?.room?.vacant;
    if (!v) return nothing;
    const words = v.others_words;
    return html`<section class="card mo-card" data-testid="mo-vacant-room"><header class="card-head">${icon("tune")}<h3>Vacant settings of ${this.roomName()}</h3>
        ${v.used ? chip("ok", "Vacancy uses the Vacant profile") : chip("neutral", "Not used by this room")}</header>
      ${v.used ? nothing : html`<p class="small muted">This room's vacancy action is not the Vacant profile, so these settings are not used until it is (Lighting → room).</p>`}
      <div class="mo-form-grid">
        <label class="mo-field"><span>Lights the Vacant profile does not set</span>
          <select ?disabled=${!ed} @change=${(e: Event) => { const x = (e.target as HTMLSelectElement).value; void this.setVacant(x === "mood" && !v.settings.mood ? { others: x, mood: this.mood } : { others: x || null }, true); }}>
            <option value="" ?selected=${!v.settings.others}>Global default (${words[v.global.others] ?? v.global.others})</option>
            ${Object.entries(words).map(([k, w]) => html`<option value=${k} ?selected=${v.settings.others === k}>${w}</option>`)}</select></label>
        ${v.settings.others === "mood" ? html`<label class="mo-field"><span>Like mood</span>
          <select ?disabled=${!ed} @change=${(e: Event) => void this.setVacant({ mood: (e.target as HTMLSelectElement).value || null }, true)}>
            ${m.moods.map((x) => html`<option ?selected=${x.name === v.settings.mood}>${x.name}</option>`)}</select></label>` : nothing}
        <label class="mo-field"><span>Switch on only below (lx)</span>
          <input type="number" min="0" max="2000" step="1" placeholder=${`Global default (${v.global.on_below ?? `normal: ${v.normal_on_below ?? "—"}`})`} .value=${String(v.settings.on_below ?? "")} ?disabled=${!ed}
            @change=${(e: Event) => { const x = (e.target as HTMLInputElement).value; void this.setVacant({ on_below: x === "" ? null : Number(x) }, true); }} /></label>
      </div>
      <p class="small muted">In effect: ${words[v.effective.others] ?? v.effective.others} (${v.sources.others === "room" ? "this room" : "global default"}) · on below ${v.effective.on_below ?? "the normal threshold"}${v.effective.on_below !== null ? " lx" : ""} (${v.sources.on_below === "room" ? "this room" : "global default"}).</p>
    </section>`;
  }

  private previewCard(m: EditorModel): TemplateResult | typeof nothing {
    if (!this.previewOpen || this.mood === this.all) return nothing;
    const area = this.previewArea ?? this.areaId;
    const p = this.preview;
    return html`<section class="card mo-card" data-testid="mo-preview-card"><header class="card-head">${icon("eye")}<h3>Preview · ${this.profileName(this.profile)} · ${this.mood}</h3>
        <label class="mo-inline"><span class="small muted">Room</span><select @change=${(e: Event) => { this.previewArea = (e.target as HTMLSelectElement).value; void this.loadPreview(); }}>
          ${m.rooms.map((r) => html`<option value=${r.area_id} ?selected=${r.area_id === area}>${r.name}</option>`)}</select></label>
        <button type="button" class="btn btn-small" @click=${() => void this.loadPreview()}>${icon("refresh", 16)}Refresh</button></header>
      <p class="small muted">What each light in the room would get with this profile and mood ${this.profile === "vacant" ? "if the room were vacant now" : "if the room were occupied now"}, with the current global values. A preview never sends anything.</p>
      ${this.previewError ? problemNotice(this.previewError) : !p ? html`<p class="muted" role="status">Loading…</p>` : html`
        <p class="small">${chip(p.counts.change ? "info" : "neutral", `${p.counts.change} would change`)} ${chip("neutral", `${p.counts.unchanged} already so`)}
          ${p.counts.blocked ? chip("warning", `${p.counts.blocked} blocked`) : nothing} ${p.counts.skipped ? chip("neutral", `${p.counts.skipped} skipped`) : nothing}
          ${p.daylight?.text ? html`<span class="muted"> · ${p.daylight.text}</span>` : nothing}${p.note ? html`<span class="muted"> · ${p.note}</span>` : nothing}</p>
        ${p.items.length ? html`<div class="table-wrap"><table class="plain-table mo-preview-table"><thead><tr><th scope="col">Light</th><th scope="col">Now</th><th scope="col">Would get</th><th scope="col">Decision</th></tr></thead>
          <tbody>${p.items.map((i) => { const d = DECISION[i.decision] ?? ["neutral", i.decision]; return html`<tr>
            <td><span class="mo-row-name">${i.name}</span><span class="block small muted">${i.category_name ?? "No light type"}</span></td>
            <td>${stateText(i.current)}</td><td>${stateText(i.requested)}</td>
            <td>${chip(d[0], d[1])}<span class="block small muted">${i.reason}</span>${i.unsupported.length ? html`<span class="block small tone-warning">Not supported: ${i.unsupported.join(", ")}</span>` : nothing}</td></tr>`; })}</tbody></table></div>`
          : html`<p class="muted">No lights in this room.</p>`}`}
      ${this.editable ? html`<div class="mo-test-row"><button type="button" class="btn" data-testid="mo-test" ?disabled=${this.busy || !p} @click=${() => void this.openDialog("test")}>${icon("tap", 18)}Test in this room…</button>
        <span class="small muted">Sends these commands to the real lights (asks first).</span></div>` : nothing}
    </section>`;
  }

  private dialogTemplate(m: EditorModel): TemplateResult | typeof nothing {
    if (!this.dialog) return nothing;
    const kind = this.dialog;
    const where = this.scope === "global" ? "the global level" : this.roomName();
    const target = `${this.profileName(this.profile)} · ${this.mood}`;
    const title = { copy: `Copy into ${target}`, reset: `Reset ${this.profileName(this.profile)} · ${moodLabel(this.mood, this.all)}`, test: `Test ${target} in ${this.model?.rooms.find((r) => r.area_id === (this.previewArea ?? this.areaId))?.name ?? ""}` }[kind];
    const p = this.preview;
    const changes = p?.items.filter((i) => i.decision === "change") ?? [];
    const sameAsTarget = this.copyFrom.profile === this.profile && this.copyFrom.mood === this.mood;
    return html`<dialog class="sheet mo-dialog" aria-labelledby="mo-dialog-title" data-testid="mo-dialog" @close=${() => (this.dialog = null)}>
      <div class="sheet-inner">
        <header class="sheet-head"><h2 id="mo-dialog-title">${title}</h2>
          <button type="button" class="icon-btn" aria-label="Close" @click=${() => this.closeDialog()}>${icon("close")}</button></header>
        <div class="sheet-body">
          ${this.error ? problemNotice(this.error) : nothing}
          ${kind === "copy" ? html`
            <p>Replaces the ${target} entries at ${where} with what the source gives now${this.scope === "global" ? " (its global values)" : " (global and room values together, plus its single-light entries)"}.</p>
            <div class="mo-form-grid">
              <label class="mo-field"><span>From profile</span><select @change=${(e: Event) => (this.copyFrom = { ...this.copyFrom, profile: (e.target as HTMLSelectElement).value as ProfileName })}>
                <option value="occupied" ?selected=${this.copyFrom.profile === "occupied"}>Occupied</option><option value="vacant" ?selected=${this.copyFrom.profile === "vacant"}>Vacant</option></select></label>
              <label class="mo-field"><span>From mood</span><select @change=${(e: Event) => (this.copyFrom = { ...this.copyFrom, mood: (e.target as HTMLSelectElement).value })}>
                ${m.moods.map((x) => html`<option ?selected=${x.name === this.copyFrom.mood}>${x.name}</option>`)}</select></label>
            </div>
            ${sameAsTarget ? html`<p class="small tone-warning">Source and target are the same.</p>` : nothing}`
          : kind === "reset" ? html`
            <p>Removes every ${this.profileName(this.profile)} entry of ${moodLabel(this.mood, this.all)} at ${where}${this.scope === "global" ? "" : " (its light types and single lights)"}. It inherits again${this.scope === "global" ? " from its parent and fallback moods" : " from the global values"}.</p>`
          : html`
            <p class="notice notice-warning">${icon("warning", 20)}<span>This <strong>sends real commands</strong> to the lights now. Every light it changes becomes a <strong>manual override</strong> for ${m.settings.manual_override_minutes ? `${m.settings.manual_override_minutes} minutes` : "as long as the room stays occupied"}${" "}(rooms can have their own duration): automatic lighting does not change those lights until the override ends or is cleared in Lighting.</span></p>
            ${changes.length ? html`<p>${changes.length} light${changes.length === 1 ? "" : "s"} would change:</p>
              <ul class="plain-list small">${changes.map((i) => html`<li><strong>${i.name}</strong>: ${stateText(i.current)} → ${stateText(i.requested)}</li>`)}</ul>`
              : html`<p class="muted">According to the preview no light would change.</p>`}
            <label class="confirm-row"><input type="checkbox" data-testid="mo-understood" .checked=${this.understood} @change=${(e: Event) => (this.understood = (e.target as HTMLInputElement).checked)} />
              <span>I understand that the lights are switched now and become manual overrides.</span></label>`}
        </div>
        <footer class="sheet-foot">
          <button type="button" class="btn" @click=${() => this.closeDialog()}>Cancel</button>
          ${kind === "copy" ? html`<button type="button" class="btn btn-primary" data-testid="mo-confirm" ?disabled=${this.busy || sameAsTarget || !this.copyFrom.mood} @click=${() => void this.copy()}>${icon("copy", 18)}Copy</button>`
            : kind === "reset" ? html`<button type="button" class="btn btn-danger" data-testid="mo-confirm" ?disabled=${this.busy} @click=${() => void this.reset()}>${icon("restore", 18)}Reset</button>`
            : html`<button type="button" class="btn btn-danger" data-testid="mo-confirm" ?disabled=${this.busy || !this.understood} @click=${() => void this.test()}>${icon("tap", 18)}${this.busy ? "Sending…" : "Send to the lights"}</button>`}
        </footer>
      </div>
    </dialog>`;
  }

  protected render(): unknown {
    const m = this.model;
    if (!m) return this.error ? problemNotice(this.error) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading profiles…</span></div>`;
    const insp = this.inspector();
    const tr = this.testResult;
    return html`<div class="mo-profiles ${insp === nothing ? "" : "mo-with-inspector"}">
      ${this.side(m)}
      <section class="mo-center" aria-label="Profile entries">
        ${this.error && !this.dialog ? problemNotice(this.error) : nothing}
        ${this.notice ? html`<p class="notice notice-info" role="status" data-testid="mo-notice">${icon("ok", 20)}<span>${this.notice}</span></p>` : nothing}
        ${tr ? html`<p class="notice ${Object.keys(tr.errors).length ? "notice-warning" : "notice-info"}" role="status" data-testid="mo-test-result">${icon("tap", 20)}<span>Test in ${tr.room}: sent to ${tr.sent.length} light${tr.sent.length === 1 ? "" : "s"}${tr.sent.length ? ` (manual override${tr.override_minutes ? ` for ${tr.override_minutes} min` : " until the room is empty"})` : ""}.
          ${Object.entries(tr.errors).map(([e, msg]) => html`<span class="block">${e}: ${msg}</span>`)}</span></p>` : nothing}
        ${m.mood.manual_mode ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>Manual mode: automatic lighting is paused everywhere. Profiles are still used when it ends.</span></p>` : nothing}
        ${this.mood && this.mood !== this.all && m.moods.find((x) => x.name === this.mood)?.in_main === false
          ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>${this.mood} is not an option of the mood selector, so it cannot become the active mood (see Moods → handling).</span></p>` : nothing}
        <div class="card mo-card">
          ${this.toolbar()}
          ${this.table()}
        </div>
        ${this.vacantCard(m)}
        ${this.previewCard(m)}
      </section>
      ${insp}
      ${insp === nothing ? nothing : html`<div class="mo-scrim" @click=${() => { this.selected = null; this.draftFor = null; }}></div>`}
    </div>
    ${this.dialogTemplate(m)}`;
  }
}

customElements.define("sl-moods-profiles", SlMoodsProfiles);
