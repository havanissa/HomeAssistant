// Curtains → Global Modes (Phase A5): which preset each window or cover uses when the controller selects a mode.
// Edits are collected as a draft of operations, validated on a copy (``modes/change`` with ``dry_run``), reviewed
// (changes, impact per window) and only then saved - with a confirmation and a policy when an Automatic window's
// current position would change. Selecting and editing never move anything.
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { setLeaveGuard } from "../router";
import { chip, StoreElement } from "../ui";
import { cmd, curtains, type ChangeResult, type CurtainsModel, type LayerModel, type MatrixCell, type MatrixRow, type ModeInfo, type ModesModel, type Obj, type WindowModel } from "./api";
import { fmt, mdi, modeChip, plural, problemList, problemNotice, ReviewFlow, SRC_TAG, SRC_TONE, type CurtainsHost } from "./components";

export class SlCurtainsModes extends StoreElement {
  static properties = {
    model: { attribute: false }, host: { attribute: false }, ops: { state: true }, draft: { state: true }, pending: { state: true },
    error: { state: true }, advCols: { state: true }, focusMode: { state: true }, busy: { state: true }, presetsOpen: { state: true },
  };
  declare model: CurtainsModel;
  declare host: CurtainsHost;
  declare ops: Obj[];
  declare draft: ModesModel | null;
  declare pending: ChangeResult | null;
  declare error: { code: string; message: string; op: Obj | null } | null;
  declare advCols: boolean;
  declare focusMode: string | null;
  declare busy: boolean;
  declare presetsOpen: boolean;
  private review = new ReviewFlow(this);

  constructor() {
    super();
    this.ops = [];
    this.draft = null;
    this.pending = null;
    this.error = null;
    this.advCols = false;
    this.focusMode = null;
    this.busy = false;
    this.presetsOpen = false;
  }

  connectedCallback(): void {
    super.connectedCallback();
    setLeaveGuard(() => (this.ops.length ? `Global Modes has ${plural(this.ops.length, "unsaved change")}. Leave without saving?` : null));
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    setLeaveGuard(null);
  }

  private get mm(): ModesModel {
    return this.draft ?? this.model.global_modes;
  }

  /** Add operations to the draft and validate them on a copy (nothing is saved). */
  private async edit(op: Obj | Obj[]): Promise<void> {
    const ops = [...this.ops, ...(Array.isArray(op) ? op : [op])];
    this.busy = true;
    try {
      const res = await curtains<ChangeResult>(cmd("modes/change"), { ops, dry_run: true });
      this.ops = ops;
      this.draft = res.global_modes ?? null;
      this.pending = res;
      this.error = null;
    } catch (err) {
      this.error = { code: (err as { code?: string }).code ?? "error", message: err instanceof Error ? err.message : String(err), op: Array.isArray(op) ? null : op };
    }
    this.busy = false;
  }

  private discard(): void {
    this.ops = [];
    this.draft = null;
    this.pending = null;
    this.error = null;
  }

  private openReview(): void {
    if (!this.ops.length) return;
    void this.review.open("Review Global Modes changes", { type: cmd("modes/change"), ops: this.ops }, () => { this.discard(); void this.host.reload(); });
  }

  private cellChange(row: MatrixRow, mode: string, value: string): void {
    const [kind, v] = [value.slice(0, 1), value.slice(2)];
    if (kind === "r") {
      void this.edit({ op: "set", level: "room", mode, area_id: row.area_id, value: v || null });
    } else if (kind === "o") {
      // clear an old window / cover override (from an earlier version); Global Modes then decides
      void this.edit({ op: "set", level: row.kind === "cover" ? "cover" : "window", mode, window_id: row.window_id, layer_id: row.layer_id ?? null, value: null });
    } else if (v) {
      void this.edit({ op: "assign", mode, window_id: row.window_id, layer_id: row.layer_id ?? null, preset_id: v });
    } else {
      void this.edit({ op: "unassign", mode, window_id: row.window_id, layer_id: row.layer_id ?? null });
    }
  }

  private cell(row: MatrixRow, m: ModeInfo): TemplateResult {
    if (!row.cells) return html`<td></td>`;
    const c: MatrixCell = row.cells[m.key];
    const can = this.host.canEdit;
    const draft = this.ops.some((o) => o.mode === m.key && (o.window_id ?? null) === (row.window_id ?? null) && (o.layer_id ?? null) === (row.layer_id ?? null) && (o.area_id ?? null) === (row.area_id ?? null));
    const tag = c.source ? html`<span class="cu-srctag cu-srctag-${SRC_TONE[c.source] ?? "neutral"}" title=${c.source_word ?? c.source}>${SRC_TAG[c.source] ?? "?"}</span>` : nothing;
    const text = c.text ?? `${c.label}${c.source_word ? ` — ${c.source_word}` : ""}`;
    if (row.kind === "room") {
      return html`<td><div class="cu-cell" data-draft=${draft ? "1" : "0"}>
        <select aria-label=${`${row.name}, ${m.word}`} ?disabled=${!can || this.busy} @change=${(e: Event) => this.cellChange(row, m.key, (e.target as HTMLSelectElement).value)}>
          <option value="r:" ?selected=${!c.value}>Inherit (no room override)</option>
          ${c.value ? html`<option value=${`r:${c.value}`} selected>${c.label} (old room override)</option>` : nothing}</select>
        <span class="cu-cell-text" title=${text}>${tag} ${text}</span></div></td>`;
    }
    return html`<td><div class="cu-cell" data-draft=${draft ? "1" : "0"} data-state=${c.state}>
      <select aria-label=${`${row.kind === "cover" ? `${row.window_name} — ${row.name}` : row.name}, ${m.word}`} ?disabled=${!can || this.busy} @change=${(e: Event) => this.cellChange(row, m.key, (e.target as HTMLSelectElement).value)}>
        <optgroup label=${`Global ${m.word}`}>
          <option value="g:" ?selected=${!c.global}>${row.kind === "cover" ? "Not assigned (follows the window)" : "Not assigned (keeps its position)"}</option>
          ${row.options.map((o) => html`<option value=${`g:${o.preset_id}`} ?selected=${c.global === o.preset_id} title=${o.detail}>${o.name} (${o.detail})</option>`)}
        </optgroup>
        ${c.override ? html`<optgroup label="Old override (earlier version)"><option value="o:" >Clear it: Global Modes decides</option></optgroup>` : nothing}
      </select>
      <span class="cu-cell-text ${c.state === "missing" ? "tone-error" : ""}" title=${text}>${tag} ${text}${c.problems?.length ? html` · <span class="tone-warning">${c.problems[0]}</span>` : nothing}</span>
    </div></td>`;
  }

  private matrix(mm: ModesModel): TemplateResult {
    const modes = mm.modes.filter((m) => (this.focusMode ? m.key === this.focusMode : this.advCols || !m.advanced));
    return html`<div class="table-wrap cu-matrix-wrap"><table class="cu-matrix" data-testid="curtains-modes-matrix">
      <thead><tr><th scope="col">Window / Cover</th>${modes.map((m) => html`<th scope="col" title=${m.hint}>${icon(mdi(m.icon), 18)}${m.word}</th>`)}</tr></thead>
      <tbody>${mm.matrix.map((row) => {
        if (row.kind === "room") return html`<tr class="cu-room"><th scope="row">${row.name}${row.area_id && row.cells ? html` <span class="small">· room override</span>` : nothing}</th>${modes.map((m) => this.cell(row, m))}</tr>`;
        if (row.kind === "window") return html`<tr data-window=${row.window_id ?? ""}><th scope="row"><a class="text-link" href=${`#/curtains/windows/${encodeURIComponent(row.window_id ?? "")}`}>${row.name}</a> ${modeChip(row.mode ?? "off", (row.mode ?? "off")[0].toUpperCase() + (row.mode ?? "off").slice(1))}</th>${modes.map((m) => this.cell(row, m))}</tr>`;
        return html`<tr class="cu-cover"><th scope="row">↳ ${row.name}</th>${modes.map((m) => this.cell(row, m))}</tr>`;
      })}</tbody></table></div>
      <p class="small muted"><span class="cu-srctag cu-srctag-info">G</span> Global Modes${this.model.centralized ? "" : html` · <span class="cu-srctag cu-srctag-warning">R</span> <span class="cu-srctag cu-srctag-warning">W</span> <span class="cu-srctag cu-srctag-warning">C</span> old room / window / cover overrides (until the migration)`}.
        "—" = no assignment (keeps its position); cover rows show the window's target unless the cover is assigned on its own. A window and one of its covers cannot both be assigned in one mode.</p>`;
  }

  private modeCards(mm: ModesModel): TemplateResult {
    const card = (m: ModeInfo) => html`<button type="button" class="cu-mode-card ${this.focusMode === m.key ? "on" : ""}" aria-pressed=${this.focusMode === m.key ? "true" : "false"} title=${m.hint} data-mode=${m.key}
      @click=${() => (this.focusMode = this.focusMode === m.key ? null : m.key)}>${icon(mdi(m.icon), 20)}<strong>${m.word}</strong>
      <span class="small muted">${m.count ? `${m.count} assigned` : "none"}${m.others.length ? ` · ${plural(m.others.length, "override")}` : ""}${m.warnings.length ? html` · <span class="tone-warning">${plural(m.warnings.length, "warning")}</span>` : ""}</span></button>`;
    return html`<div class="cu-mode-cards" role="group" aria-label="Modes">${mm.modes.filter((m) => !m.advanced).map(card)}</div>
      <button type="button" class="collapse-toggle lp-adv" aria-expanded=${this.advCols ? "true" : "false"} @click=${() => (this.advCols = !this.advCols)}>${icon(this.advCols ? "chevron-down" : "chevron-right", 18)}Advanced: ${mm.modes.filter((m) => m.advanced).map((m) => m.word).join(", ")}</button>
      ${this.advCols ? html`<div class="cu-mode-cards">${mm.modes.filter((m) => m.advanced).map(card)}</div>
        <p class="small muted">These need their own preset only where they should differ: Glare and Excessive Heat use a cover's Sun Protection preset when nothing is assigned; Useful solar heating keeps the Morning / Daytime preset. Manual holds and Waiting for daylight never move anything.</p>` : nothing}`;
  }

  private focusDetails(mm: ModesModel): TemplateResult | typeof nothing {
    const m = mm.modes.find((x) => x.key === this.focusMode);
    if (!m) return nothing;
    return html`<section class="card wide" data-testid="curtains-mode-details"><header class="card-head">${icon(mdi(m.icon))}<h3>${m.word}</h3><span class="small muted">${m.hint}</span></header>
      ${m.warnings.length ? problemList(m.warnings) : html`<p class="muted small">No warnings for ${m.word}.</p>`}
      ${m.rows.length ? html`<h4>Assigned (${m.rows.length})</h4><ul class="cu-rows">${m.rows.map((r) => html`<li class="cu-row"><span class="cu-row-main"><strong>${r.name}</strong><span class="small muted">${r.room} · ${r.type_word} · ${r.effective}</span>
        ${r.hidden.map((h) => html`<span class="small tone-warning">${h.word}${h.cover ? ` (${h.cover})` : ""}: ${h.label} hides this setting (from an earlier version; the migration resolves it).</span>`)}
        ${r.conflicts.length ? html`<span class="small tone-warning">Competing automations: ${r.conflicts.join(", ")}</span>` : nothing}</span>
        <span class="cu-chips">${modeChip(r.status.mode, r.status.mode_word)}${r.active ? chip("ok", "Active now") : r.status.condition_word && r.status.mode !== "off" ? chip("neutral", `Now: ${r.status.condition_word}`) : nothing}${r.status.hold ? chip("warning", "Manual hold") : nothing}${r.status.deferred ? chip("warning", "Waits for next condition") : nothing}${r.missing ? chip("error", "Missing preset") : nothing}</span></li>`)}</ul>` : html`<p class="muted small">No window or cover is assigned to ${m.word}: they keep their position when the controller selects ${m.word}.</p>`}
      ${m.others.length ? html`<h4>Still set outside Global Modes (earlier version, ${m.others.length})</h4><ul class="cu-rows">${m.others.map((o) => html`<li class="cu-row"><span class="cu-row-main"><strong>${o.name}</strong><span class="small muted">${o.room}</span></span>${chip("warning", `${o.word}: ${o.label}`)}</li>`)}</ul>
        <p class="small muted">Active until the migration moves them into Global Modes.</p>` : nothing}
    </section>`;
  }

  // ----------------------------------------------------------------- cover presets of layered windows
  private coverPresets(w: WindowModel, l: LayerModel): TemplateResult {
    const can = this.host.canEdit;
    const save = (preset_id: string | null, fields: Obj) => void this.host.run(() => curtains(cmd("cover_preset/save"), { window_id: w.window_id, layer_id: l.layer_id, preset_id, ...fields }), "Cover preset saved");
    return html`<div class="lp-subcard" data-layer=${l.layer_id}><strong>${w.name} — ${l.name}</strong> <span class="small muted">${l.kind_word} · ${plural(l.presets.length, "preset")}</span>
      <div class="cu-cover-presets">${l.presets.map((p) => html`<div class="cu-cover-preset">
        <input type="text" maxlength="60" .value=${p.name} aria-label="Preset name" ?disabled=${!can} @change=${(e: Event) => { const v = (e.target as HTMLInputElement).value.trim(); if (v && v !== p.name) save(p.preset_id, { name: v }); }} />
        <span class="lp-input-row"><input type="number" class="lp-num" min="0" max="100" step="1" .value=${String(p.position)} aria-label=${`${p.name} position in %`} ?disabled=${!can} @change=${(e: Event) => { const v = Number((e.target as HTMLInputElement).value); if (Number.isFinite(v) && v !== p.position) save(p.preset_id, { position: Math.round(v) }); }} /><span class="muted">%</span></span>
        ${can ? html`<button type="button" class="btn btn-small" aria-label=${`Delete ${p.name}`} @click=${() => { if (confirm(`Delete the preset ${p.name} of ${l.name}? Assignments that use it are removed; nothing moves.`)) void this.host.run(() => curtains(cmd("cover_preset/delete"), { window_id: w.window_id, layer_id: l.layer_id, preset_id: p.preset_id }), "Deleted"); }}>${icon("delete", 16)}</button>` : nothing}</div>`)}</div>
      ${can && l.presets.length < 12 ? html`<button type="button" class="btn btn-small lp-gap" @click=${() => save(null, { name: `Preset ${l.presets.length + 1}`, position: l.position ?? 50 })}>${icon("plus", 16)}Add preset</button>` : nothing}
    </div>`;
  }

  protected render(): unknown {
    const mm = this.mm;
    const total = mm.modes.reduce((n, m) => n + m.count, 0);
    const layered = this.model.windows.filter((w) => w.layered);
    return html`<div data-testid="curtains-modes">
      <p class="muted small">Which preset each window or cover uses when the automatic controller selects a mode for it: ${plural(total, "assignment")} over ${plural(this.model.windows.length, "window")}. Each window still decides its own mode; a window without an assignment keeps its position. Selecting and editing never move anything.</p>
      ${this.ops.length ? html`<div class="cu-draftbar" role="status" data-testid="curtains-modes-draft"><span><strong>${plural(this.ops.length, "unsaved change")}</strong> <span class="small muted">${this.pending?.needs_confirm ? "· would move an Automatic window: reviewed before saving" : "· nothing moves until you save"}</span></span>
        <div class="row-actions"><button type="button" class="btn btn-primary btn-small" data-testid="curtains-modes-review" ?disabled=${this.busy} @click=${() => this.openReview()}>${icon("eye", 16)}Review changes…</button>
          <button type="button" class="btn btn-small" @click=${() => this.discard()}>Discard</button></div></div>` : nothing}
      ${this.error ? html`<p class="notice notice-error" role="alert">${icon("error", 20)}<span>${this.error.message}
        ${this.error.code === "overlap" && this.error.op ? html` <button type="button" class="btn btn-small" @click=${() => void this.edit({ ...this.error!.op!, replace: true })}>Replace the existing assignment</button>` : nothing}</span>
        <button type="button" class="icon-btn" aria-label="Close" @click=${() => (this.error = null)}>${icon("close", 18)}</button></p>` : nothing}
      ${this.host.error ? problemNotice(this.host.error) : nothing}
      ${this.modeCards(mm)}
      <div class="grid engine-grid">
        <section class="card wide"><header class="card-head">${icon("view-grid")}<h3>${this.focusMode ? `${mm.modes.find((m) => m.key === this.focusMode)?.word ?? ""} assignments` : "All windows × modes"}</h3>
          ${this.focusMode ? html`<button type="button" class="btn btn-small" @click=${() => (this.focusMode = null)}>Show every mode</button>` : nothing}</header>
          ${this.matrix(mm)}</section>
        ${this.focusDetails(mm)}
        ${layered.length ? html`<section class="card wide" data-testid="curtains-cover-presets"><header class="card-head">${icon("layers")}<h3>Presets of single covers</h3>
            <button type="button" class="btn btn-small" aria-expanded=${this.presetsOpen ? "true" : "false"} @click=${() => (this.presetsOpen = !this.presetsOpen)}>${this.presetsOpen ? "Hide" : "Show"}</button></header>
          <p class="muted small">Used when a cover of a layered window is assigned on its own in Global Modes; that never moves the other covers of the window. Changing a position here changes every assignment that uses the preset.</p>
          ${this.presetsOpen ? layered.map((w) => w.layers.map((l) => this.coverPresets(w, l))) : nothing}</section>` : nothing}
        <section class="card wide" data-testid="curtains-modes-log"><header class="card-head">${icon("history")}<h3>Recent automatic decisions</h3></header>
          ${this.model.global_modes.log.length ? html`<ul class="cu-log">${this.model.global_modes.log.map((x) => html`<li><time datetime=${x.at ?? ""}>${fmt(x.at)}</time><span><strong>${x.window}</strong>: ${x.summary}${x.source_word ? html` <span class="muted">(${x.source_word})</span>` : nothing}</span></li>`)}</ul>` : html`<p class="muted">Nothing yet.</p>`}</section>
      </div>
      ${this.review.render("curtains-modes-review-dialog")}
    </div>`;
  }
}

customElements.define("sl-curtains-modes", SlCurtainsModes);
