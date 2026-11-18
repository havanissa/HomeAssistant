// Moods → Global adjustment (Phase A4): the existing system's global brightness and colour temperature outputs
// that profiles can follow ("Follow global" in a profile entry). Read only on those entities (adaptive/set).
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { haTime } from "../presence/components";
import { chip, kv } from "../ui";
import { cmd, lighting } from "./api";
import { LightingElement, problemNotice } from "./components";
import { editor, type AdaptiveModel, type EditorModel } from "./moods-model";

type Config = AdaptiveModel["config"];
const STATUS: Record<string, ["ok" | "warning" | "neutral" | "error", string]> = {
  connected: ["ok", "Connected"], unavailable: ["warning", "Unavailable"], not_configured: ["neutral", "Not configured"],
  invalid: ["error", "Value not understood"], missing: ["error", "Entity missing"], stale: ["warning", "Stale"],
};
const FIELDS: Array<[keyof Config, string]> = [
  ["brightness_entity", "Brightness source"], ["color_temp_entity", "Colour temperature source"],
  ["brightness_attribute", "Brightness attribute"], ["color_temp_attribute", "Colour temperature attribute"],
  ["brightness_scale", "Brightness units"], ["color_temp_scale", "Colour temperature units"],
  ["min_interval", "Update at most every (s)"], ["brightness_step", "Smallest brightness change (%)"],
  ["kelvin_step", "Smallest colour temperature change (K)"], ["transition", "Transition (s)"],
];

export class SlMoodsAdjust extends LightingElement {
  static properties = { ...LightingElement.properties, model: { state: true }, draft: { state: true }, advanced: { state: true } };
  declare model: EditorModel | null;
  declare draft: Config | null;
  declare advanced: boolean;

  constructor() {
    super();
    this.model = null;
    this.draft = null;
    this.advanced = false;
  }

  async reload(): Promise<void> {
    try {
      this.model = await editor({});
      if (!this.changes().length) this.draft = { ...this.model.adaptive.config };
    } catch (err) {
      this.error = err;
    }
  }

  private changes(): Array<keyof Config> {
    const base = this.model?.adaptive.config;
    if (!base || !this.draft) return [];
    return FIELDS.map(([k]) => k).filter((k) => (this.draft![k] ?? null) !== (base[k] ?? null));
  }

  private async save(): Promise<void> {
    const d = this.draft!;
    const body: Record<string, unknown> = {};
    for (const k of this.changes()) body[k] = typeof d[k] === "string" ? ((d[k] as string).trim() || null) : d[k];
    const ok = await this.run(() => lighting(cmd("adaptive/set"), body), "Saved the global adjustment");
    if (ok && this.model) this.draft = { ...this.model.adaptive.config };
  }

  private source(a: AdaptiveModel, kind: "brightness" | "color_temp", ed: boolean): TemplateResult {
    const key = `${kind}_entity` as const;
    const v = a.values[kind];
    const cands = a.candidates?.[kind] ?? [];
    const unit = kind === "brightness" ? " %" : " K";
    const st = STATUS[v.status] ?? ["neutral", v.status];
    const value = this.draft?.[key] ?? "";
    const list = `mo-cand-${kind}`;
    return html`<div class="mo-source">
      <label class="mo-field"><span>${kind === "brightness" ? "Brightness" : "Colour temperature"} source</span>
        <input type="text" list=${list} placeholder="None (not followed)" spellcheck="false" .value=${value} ?disabled=${!ed}
          @input=${(e: Event) => (this.draft = { ...this.draft!, [key]: (e.target as HTMLInputElement).value || null })} />
        <datalist id=${list}>${cands.map((c) => html`<option value=${c.entity_id}>${c.name} · ${c.value}${unit}</option>`)}</datalist></label>
      ${ed && cands.some((c) => c.likely && c.entity_id !== value) ? html`<div class="mo-suggest small"><span class="muted">Suggested:</span>
        ${cands.filter((c) => c.likely && c.entity_id !== value).slice(0, 4).map((c) => html`<button type="button" class="btn btn-small" @click=${() => (this.draft = { ...this.draft!, [key]: c.entity_id })}>${c.name} · ${c.value}${unit}</button>`)}</div>` : nothing}
      <p class="small">${chip(st[0], st[1])} ${v.entity_id ? html`<strong>${v.value !== null ? `${Math.round(v.value)}${unit}` : "—"}</strong> <span class="muted">from ${v.entity_id}${v.updated_at ? ` · ${haTime()(v.updated_at)}` : ""}</span>` : nothing}
        ${v.status !== "connected" && v.status !== "not_configured" ? html`<span class="block muted">${v.detail}</span>` : nothing}</p>
    </div>`;
  }

  private num(k: keyof Config, label: string, attrs: { min: number; max: number; step?: number }, ed: boolean): TemplateResult {
    return html`<label class="mo-field"><span>${label}</span><input type="number" min=${attrs.min} max=${attrs.max} step=${attrs.step ?? 1} .value=${String(this.draft?.[k] ?? "")} ?disabled=${!ed}
      @change=${(e: Event) => { const x = (e.target as HTMLInputElement).value; if (x !== "") this.draft = { ...this.draft!, [k]: Number(x) }; }} /></label>`;
  }

  protected render(): unknown {
    const m = this.model;
    if (!m || !this.draft) return this.error ? problemNotice(this.error) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`;
    const a = m.adaptive;
    const ed = this.canEdit && (m.can_edit ?? true) && !this.busy;
    const changes = this.changes();
    const d = this.draft;
    const active = a.controllers.filter((c) => c.active);
    const words = Object.fromEntries(FIELDS);
    return html`${this.error ? problemNotice(this.error) : nothing}
      ${this.notice ? html`<p class="notice notice-info" role="status" data-testid="mo-notice">${icon("ok", 20)}<span>${this.notice}</span></p>` : nothing}
      <div class="mo-grid">
        <section class="card mo-card mo-span-2" data-testid="mo-adjust"><header class="card-head">${icon("brightness")}<h3>Global light adjustment</h3>
            ${chip((STATUS[a.status] ?? ["neutral"])[0], (STATUS[a.status] ?? ["neutral", a.status])[1])}</header>
          <p class="small muted">The App only reads these values from your existing adjustment (for example Adaptive Lighting); it does not calculate them. Profile entries choose whether to follow them (Brightness or Colour temperature: “Follow global”).</p>
          <div class="mo-form-grid mo-form-2">${this.source(a, "brightness", ed)}${this.source(a, "color_temp", ed)}</div>
          ${active.length ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>Your existing adjustment also commands lights itself: ${active.map((c) => c.name ?? c.entity_id).join(", ")}. Global updates are never sent to lights this system controls, so the two do not compete.</span></p>` : nothing}
          <button type="button" class="btn btn-small" aria-expanded=${this.advanced} @click=${() => (this.advanced = !this.advanced)}>${icon(this.advanced ? "chevron-down" : "chevron-right", 16)}Advanced</button>
          ${this.advanced ? html`<div class="mo-form-grid mo-advanced">
            ${this.num("min_interval", "Update at most every (s, per light)", { min: 0, max: 3600 }, ed)}
            ${this.num("brightness_step", "Smallest brightness change (%)", { min: 1, max: 50 }, ed)}
            ${this.num("kelvin_step", "Smallest colour temperature change (K)", { min: 10, max: 2000, step: 10 }, ed)}
            ${this.num("transition", "Transition (s)", { min: 0, max: 300 }, ed)}
            <label class="mo-field"><span>Brightness attribute</span><input type="text" placeholder="automatic" .value=${d.brightness_attribute ?? ""} ?disabled=${!ed}
              @input=${(e: Event) => (this.draft = { ...d, brightness_attribute: (e.target as HTMLInputElement).value || null })} /></label>
            <label class="mo-field"><span>Colour temperature attribute</span><input type="text" placeholder="automatic" .value=${d.color_temp_attribute ?? ""} ?disabled=${!ed}
              @input=${(e: Event) => (this.draft = { ...d, color_temp_attribute: (e.target as HTMLInputElement).value || null })} /></label>
            <label class="mo-field"><span>Brightness units</span><select ?disabled=${!ed} @change=${(e: Event) => (this.draft = { ...d, brightness_scale: (e.target as HTMLSelectElement).value })}>
              ${[["auto", "Automatic"], ["percent", "0–100 %"], ["byte", "0–255"]].map(([v, l]) => html`<option value=${v} ?selected=${d.brightness_scale === v}>${l}</option>`)}</select></label>
            <label class="mo-field"><span>Colour temperature units</span><select ?disabled=${!ed} @change=${(e: Event) => (this.draft = { ...d, color_temp_scale: (e.target as HTMLSelectElement).value })}>
              ${[["auto", "Automatic"], ["kelvin", "Kelvin"], ["mired", "Mired"]].map(([v, l]) => html`<option value=${v} ?selected=${d.color_temp_scale === v}>${l}</option>`)}</select></label>
          </div>` : nothing}
          ${this.canEdit ? html`<div class="mo-apply-row">
            <p class="small ${changes.length ? "" : "muted"}" data-testid="mo-review">${changes.length ? html`<strong>Will save:</strong> ${changes.map((k) => `${words[k]}: ${d[k] ?? "none"}`).join(" · ")}` : "No unsaved changes."}</p>
            <button type="button" class="btn" ?disabled=${!changes.length || this.busy} @click=${() => (this.draft = { ...a.config })}>${icon("undo", 18)}Discard</button>
            <button type="button" class="btn btn-primary" data-testid="mo-adjust-save" ?disabled=${!changes.length || this.busy} @click=${() => void this.save()}>${icon("save", 18)}Save</button>
          </div>` : html`<p class="small muted">Only administrators can change the global adjustment.</p>`}
        </section>
        <section class="card mo-card"><header class="card-head">${icon("info")}<h3>Existing system</h3></header>
          ${a.controllers.length ? kv(a.controllers.map((c) => [c.name ?? c.entity_id, `${c.kind === "automation" ? "Automation" : "Adaptive Lighting"} · ${c.active ? "active" : "off"}`] as [string, unknown]))
            : html`<p class="small muted">No part of the existing system that commands lights was found.</p>`}
        </section>
      </div>`;
  }
}

customElements.define("sl-moods-adjust", SlMoodsAdjust);
