// Moods → Light types (Phase A4): the categories that profiles are set for (category/set, category/remove). A light
// type, a function or the RGB tag, usually bound to a Home Assistant label. Saved in the integration's options: a
// change reloads the integration.
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { chip } from "../ui";
import { cmd, lighting } from "./api";
import { LightingElement, problemNotice } from "./components";
import { editor, kindLabel, retrying, type Category, type EditorModel } from "./moods-model";

type Kind = Category["kind"];
interface Draft { key: string | null; name: string; kind: Kind; label_id: string | null }

const KINDS: Array<[Kind, string]> = [
  ["source", "What the light is (Spots, Ceiling lights, LED strips…)"],
  ["function", "What it is used for (Uplight, Reading…)"],
  ["rgb", "Lights that may get colour profiles"],
];

export class SlMoodsTypes extends LightingElement {
  static properties = { ...LightingElement.properties, model: { state: true }, draft: { state: true }, removing: { state: true } };
  declare model: EditorModel | null;
  declare draft: Draft | null;
  declare removing: Category | null;

  constructor() {
    super();
    this.model = null;
    this.draft = null;
    this.removing = null;
  }

  async reload(): Promise<void> {
    try {
      this.model = await retrying(() => editor({}), 6, 1500);
    } catch (err) {
      this.error = err;
    }
  }

  private async save(d: Draft): Promise<void> {
    const ok = await this.run(async () => {
      await lighting(cmd("category/set"), { ...(d.key ? { key: d.key } : {}), name: d.name.trim(), kind: d.kind, label_id: d.label_id || null });
      await new Promise((r) => setTimeout(r, 3000)); // the integration reloads
    }, d.key ? `Saved ${d.name.trim()}; the integration reloaded` : `Added ${d.name.trim()}; the integration reloaded`);
    if (ok) this.draft = null;
  }

  private async removeType(c: Category): Promise<void> {
    const ok = await this.run(async () => {
      await lighting(cmd("category/remove"), { key: c.key });
      await new Promise((r) => setTimeout(r, 3000));
    }, `Removed ${c.name} and its profile entries`);
    this.closeDialog();
    if (ok) this.removing = null;
  }

  private async askRemove(c: Category): Promise<void> {
    this.removing = c;
    this.error = null;
    await this.updateComplete;
    this.querySelector<HTMLDialogElement>("dialog.mo-dialog")?.showModal();
  }

  private closeDialog(): void {
    this.querySelector<HTMLDialogElement>("dialog.mo-dialog")?.close();
  }

  private editor(m: EditorModel, d: Draft): TemplateResult {
    const exists = m.categories.some((c) => c.key !== d.key && c.name.trim().toLowerCase() === d.name.trim().toLowerCase());
    const set = (patch: Partial<Draft>) => (this.draft = { ...d, ...patch });
    const busy = this.busy;
    return html`<form class="mo-type-editor" data-testid="mo-type-editor" @submit=${(e: Event) => { e.preventDefault(); if (d.name.trim() && !exists) void this.save(d); }}>
      <div class="mo-form-grid">
        <label class="mo-field"><span>Name</span><input type="text" maxlength="60" required .value=${d.name} ?disabled=${busy} placeholder="e.g. Spots"
          @input=${(e: Event) => set({ name: (e.target as HTMLInputElement).value })} /></label>
        <label class="mo-field"><span>Kind</span><select ?disabled=${busy} @change=${(e: Event) => set({ kind: (e.target as HTMLSelectElement).value as Kind })}>
          ${KINDS.map(([k]) => html`<option value=${k} ?selected=${d.kind === k}>${kindLabel(k)}</option>`)}</select>
          <span class="small muted">${KINDS.find(([k]) => k === d.kind)?.[1]}</span></label>
        <label class="mo-field"><span>Home Assistant label</span><select ?disabled=${busy} @change=${(e: Event) => set({ label_id: (e.target as HTMLSelectElement).value || null })}>
          <option value="" ?selected=${!d.label_id}>No label</option>
          ${m.labels.map((l) => html`<option value=${l.label_id} ?selected=${l.label_id === d.label_id}>${l.name}</option>`)}</select>
          <span class="small muted">Lights with this label belong to the type.</span></label>
      </div>
      ${exists ? html`<p class="small tone-warning">A light type with this name already exists.</p>` : nothing}
      <div class="mo-foot-actions">
        <span class="small muted">${icon("info", 16)}Saving reloads the integration (a few seconds).</span>
        <button type="button" class="btn" ?disabled=${busy} @click=${() => (this.draft = null)}>Cancel</button>
        <button type="submit" class="btn btn-primary" data-testid="mo-type-save" ?disabled=${busy || !d.name.trim() || exists}>${icon("save", 18)}${busy ? "Saving…" : d.key ? "Save" : "Add"}</button>
      </div>
    </form>`;
  }

  private row(m: EditorModel, c: Category, ed: boolean): TemplateResult {
    if (this.draft?.key === c.key) return html`<li class="mo-type mo-type-editing">${this.editor(m, this.draft)}</li>`;
    return html`<li class="mo-type" data-key=${c.key}>
      <div class="mo-type-main">
        <span class="mo-type-icon">${icon(c.kind === "rgb" ? "palette" : c.kind === "function" ? "tune" : "lightbulb-group", 22)}</span>
        <div class="mo-type-text"><span class="mo-row-name">${c.name}</span>
          <span class="block small muted">${kindLabel(c.kind)} · ${c.label_name ? html`label <strong>${c.label_name}</strong>` : c.label_missing ? html`<span class="tone-warning">label ${c.label_id} is missing</span>` : "no label"}</span></div>
        ${chip(c.lights.length ? "ok" : "neutral", `${c.lights.length} light${c.lights.length === 1 ? "" : "s"}`)}
        ${c.kind !== "rgb" && c.rgb_lights ? chip("info", `${c.rgb_lights} with RGB`) : nothing}
        ${ed ? html`<span class="mo-type-actions">
          <button type="button" class="btn btn-small" ?disabled=${this.busy} @click=${() => (this.draft = { key: c.key, name: c.name, kind: c.kind, label_id: c.label_id })}>${icon("pencil", 16)}Edit</button>
          <button type="button" class="btn btn-small" aria-label="Remove ${c.name}" ?disabled=${this.busy} @click=${() => void this.askRemove(c)}>${icon("delete", 16)}</button></span>` : nothing}
      </div>
      ${c.lights.length ? html`<p class="small mo-type-lights">${c.lights.map((l) => l.name).join(", ")}</p>` : html`<p class="small muted mo-type-lights">No light carries this ${c.label_id ? "label" : "type"} yet.</p>`}
    </li>`;
  }

  protected render(): unknown {
    const m = this.model;
    if (!m) return this.error ? problemNotice(this.error) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`;
    const ed = this.canEdit && (m.can_edit ?? true);
    const groups: Array<[Kind, string]> = [["source", "Light types"], ["function", "Functions"], ["rgb", "RGB tag"]];
    return html`${this.error && !this.removing ? problemNotice(this.error) : nothing}
      ${this.notice ? html`<p class="notice notice-info" role="status" data-testid="mo-notice">${icon("ok", 20)}<span>${this.notice}</span></p>` : nothing}
      <div class="mo-grid">
        <section class="card mo-card mo-span-2" data-testid="mo-types"><header class="card-head">${icon("lightbulb-group")}<h3>Light types</h3>
            ${ed && !this.draft ? html`<button type="button" class="btn btn-small" data-testid="mo-type-add" @click=${() => (this.draft = { key: null, name: "", kind: "source", label_id: null })}>${icon("plus", 16)}Add</button>` : nothing}</header>
          <p class="small muted">Profiles are set per light type. A light can carry several labels: a light type (Spots, Ceiling lights), a function (Uplight) and the RGB tag. Light types are stored in the integration's options, so a change reloads the integration.</p>
          ${this.draft && !this.draft.key ? html`<div class="mo-type mo-type-editing">${this.editor(m, this.draft)}</div>` : nothing}
          ${groups.map(([kind, title]) => {
            const cats = m.categories.filter((c) => c.kind === kind);
            return cats.length ? html`<h4 class="mo-subhead">${title}</h4><ul class="mo-types">${cats.map((c) => this.row(m, c, ed))}</ul>` : nothing;
          })}
          ${m.categories.length ? nothing : html`<p class="muted">No light types yet.</p>`}
        </section>
        <section class="card mo-card" data-testid="mo-labels"><header class="card-head">${icon("filter")}<h3>Labels</h3></header>
          ${m.label_suggestions.length ? html`<p class="small">These labels look like a function or the RGB tag and are not used yet:</p>
            <ul class="mo-suggestions">${m.label_suggestions.map((s) => html`<li><strong>${s.name}</strong> <span class="small muted">${kindLabel(s.kind)}</span>
              ${ed ? html`<button type="button" class="btn btn-small" ?disabled=${this.busy} @click=${() => void this.save({ key: null, name: s.name, kind: s.kind, label_id: s.label_id })}>${icon("plus", 16)}Use</button>` : nothing}</li>`)}</ul>`
            : html`<p class="small muted">No unused label looks like a light function or the RGB tag.</p>`}
          <p class="small muted">${m.labels.length} label${m.labels.length === 1 ? "" : "s"} in Home Assistant${m.labels.length ? `: ${m.labels.map((l) => l.name).join(", ")}` : ""}. Labels are given to lights in Home Assistant (Settings → Labels).</p>
        </section>
      </div>
      ${this.removing ? html`<dialog class="sheet mo-dialog" aria-labelledby="mo-rm-title" data-testid="mo-dialog" @close=${() => (this.removing = null)}>
        <div class="sheet-inner">
          <header class="sheet-head"><h2 id="mo-rm-title">Remove ${this.removing.name}?</h2>
            <button type="button" class="icon-btn" aria-label="Close" @click=${() => this.closeDialog()}>${icon("close")}</button></header>
          <div class="sheet-body">${this.error ? problemNotice(this.error) : nothing}
            <p>The light type and all its profile entries (every mood, Occupied and Vacant, global and per room${this.removing.kind !== "rgb" ? ", including its “+ RGB” part" : ""}) are removed. The Home Assistant label is not changed. The integration reloads.</p></div>
          <footer class="sheet-foot"><button type="button" class="btn" @click=${() => this.closeDialog()}>Cancel</button>
            <button type="button" class="btn btn-danger" data-testid="mo-confirm" ?disabled=${this.busy} @click=${() => void this.removeType(this.removing!)}>${icon("delete", 18)}${this.busy ? "Removing…" : "Remove"}</button></footer>
        </div></dialog>` : nothing}`;
  }
}

customElements.define("sl-moods-types", SlMoodsTypes);
