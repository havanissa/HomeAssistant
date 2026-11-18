// Moods → Mood source (Phase A4): where the current mood comes from (mood_source/set). The three-helper scheme
// (Auto / Manual / Main Mood), one existing selector, the integration's built-in selector, or none. A change is
// saved in the integration's options and reloads it; profiles are keyed by mood name and are kept.
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { loadLookup, type LookupEntity } from "../lookup";
import { chip, kv } from "../ui";
import { cmd, lighting } from "./api";
import { LightingElement, problemNotice } from "./components";
import { BUILTIN_MOOD_ENTITY, editor, retrying, type EditorModel, type MoodSourceConfig } from "./moods-model";

type Source = "helpers" | "entity" | "builtin" | "none";

const CHOICES: Array<{ key: Source; title: string; text: string }> = [
  { key: "helpers", title: "Auto, Manual and Main Mood", text: "Three helpers: Auto Mood (set by your automations), Manual Mood (set by people, with an option that means “follow Auto”) and Main Mood (the result, which lighting follows)." },
  { key: "entity", title: "One existing selector", text: "One select or input_select you already have is the mood." },
  { key: "builtin", title: "Built-in mood selector", text: "The integration provides the selector select.smart_lighting_mood with the moods you define here." },
  { key: "none", title: "No mood source", text: "Nothing is applied by mood until a source is chosen." },
];
const STORE_KEY = "sl-moods-last-source";
const MAX_MOODS = 50;
const NAME_MAX = 64;

export class SlMoodsSource extends LightingElement {
  static properties = {
    ...LightingElement.properties,
    model: { state: true }, entities: { state: true }, choice: { state: true }, form: { state: true },
    newMood: { state: true }, confirming: { state: true }, reloading: { state: true },
  };
  declare model: EditorModel | null;
  declare entities: LookupEntity[] | null;
  declare choice: Source | null;
  declare form: { auto: string; manual: string; main: string; autoOption: string; entity: string; moods: string[] };
  declare newMood: string;
  declare confirming: boolean;
  declare reloading: boolean;

  constructor() {
    super();
    this.model = null;
    this.entities = null;
    this.choice = null;
    this.form = { auto: "", manual: "", main: "", autoOption: "Auto", entity: "", moods: [] };
    this.newMood = "";
    this.confirming = false;
    this.reloading = false;
  }

  async reload(): Promise<void> {
    try {
      const [model, lookup] = await Promise.all([editor({}), this.entities ? Promise.resolve(null) : loadLookup()]);
      this.model = model;
      if (lookup) this.entities = lookup.entities.filter((e) => e.domain === "select" || e.domain === "input_select")
        .sort((a, b) => a.name.localeCompare(b.name));
      if (this.choice === null) this.prefill();
    } catch (err) {
      this.error = err;
    }
  }

  private lastSet(): MoodSourceConfig | null {
    try {
      const raw = sessionStorage.getItem(STORE_KEY);
      return raw ? (JSON.parse(raw) as MoodSourceConfig) : null;
    } catch {
      return null;
    }
  }

  /** The configured source: reported by the integration when it does, otherwise worked out from the mood model. */
  private detect(m: EditorModel): { source: Source | "selector"; config: MoodSourceConfig | null; certain: boolean } {
    if (m.mood_source) {
      const c = m.mood_source;
      const source: Source = c.source ?? (c.builtin ? "builtin" : c.auto_mood_entity || c.manual_mood_entity ? "helpers" : c.main_mood_entity ? "entity" : "none");
      return { source, config: c, certain: true };
    }
    if (m.mood.source === "not_configured") return { source: "none", config: null, certain: true };
    const last = this.lastSet();
    if (m.moods.some((x) => x.in_auto || x.in_manual)) return { source: "helpers", config: last?.auto_mood_entity ? last : null, certain: true };
    if (last && (last.builtin || last.main_mood_entity)) return { source: last.builtin ? "builtin" : "entity", config: last, certain: false };
    return { source: "selector", config: null, certain: false };
  }

  private guess(word: string): string {
    return this.entities?.find((e) => e.entity_id.includes(word) && e.entity_id.includes("mood"))?.entity_id ?? "";
  }

  private prefill(): void {
    const m = this.model;
    if (!m) return;
    const d = this.detect(m);
    const c = d.config;
    this.choice = d.source === "selector" ? "entity" : d.source;
    this.form = {
      auto: c?.auto_mood_entity ?? this.guess("auto"),
      manual: c?.manual_mood_entity ?? this.guess("manual"),
      main: c?.main_mood_entity && c.main_mood_entity !== BUILTIN_MOOD_ENTITY ? c.main_mood_entity : this.guess("main"),
      autoOption: c?.auto_option ?? "Auto",
      entity: c?.main_mood_entity && c.main_mood_entity !== BUILTIN_MOOD_ENTITY ? c.main_mood_entity : this.guess("main"),
      moods: c?.builtin ?? m.moods.filter((x) => x.in_main).map((x) => x.name),
    };
  }

  private payload(): Record<string, unknown> {
    const f = this.form;
    switch (this.choice) {
      case "helpers": return { source: "helpers", auto_mood_entity: f.auto, manual_mood_entity: f.manual, main_mood_entity: f.main, auto_option: f.autoOption.trim() || "Auto" };
      case "entity": return { source: "entity", main_mood_entity: f.entity };
      case "builtin": return { source: "builtin", moods: f.moods.map((x) => x.trim()).filter(Boolean) };
      default: return { source: "none" };
    }
  }

  private problems(): string[] {
    const f = this.form;
    const out: string[] = [];
    if (this.choice === "helpers") {
      if (!f.auto || !f.manual || !f.main) out.push("Choose all three selectors.");
      else if (new Set([f.auto, f.manual, f.main]).size !== 3) out.push("Auto, Manual and Main Mood must be three different selectors.");
    } else if (this.choice === "entity") {
      if (!f.entity) out.push("Choose the mood selector.");
      if (f.entity === BUILTIN_MOOD_ENTITY) out.push("For the built-in selector choose “Built-in mood selector”.");
    } else if (this.choice === "builtin") {
      const names = f.moods.map((x) => x.trim()).filter(Boolean);
      const manual = (this.model?.settings.manual_option ?? "Manual").toLowerCase();
      if (!names.length) out.push("Define at least one mood.");
      if (new Set(names.map((n) => n.toLowerCase())).size !== names.length) out.push("Every mood needs a different name.");
      if (names.some((n) => n === "*" || n.toLowerCase() === manual)) out.push(`“*” and “${this.model?.settings.manual_option ?? "Manual"}” are reserved.`);
    }
    return out;
  }

  private async apply(): Promise<void> {
    const body = this.payload();
    let answer: { mood: MoodSourceConfig; changed: boolean } | null = null;
    const ok = await this.run(async () => {
      answer = await lighting<{ mood: MoodSourceConfig; changed: boolean }>(cmd("mood_source/set"), body);
    });
    this.closeDialog();
    if (!ok || !answer) return;
    const a = answer as { mood: MoodSourceConfig; changed: boolean };
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify({ ...a.mood, source: this.choice }));
    } catch {
      // per-browser convenience only
    }
    if (!a.changed) {
      this.notice = "Nothing changed: this is already the mood source.";
      return;
    }
    this.reloading = true;
    try {
      await new Promise((r) => setTimeout(r, 3000));
      this.model = await retrying(() => editor({}));
      this.notice = "Mood source saved. The integration reloaded with it.";
    } catch (err) {
      this.error = err;
    } finally {
      this.reloading = false;
    }
  }

  private async openDialog(): Promise<void> {
    this.confirming = true;
    this.error = null;
    await this.updateComplete;
    this.querySelector<HTMLDialogElement>("dialog.mo-dialog")?.showModal();
  }

  private closeDialog(): void {
    this.querySelector<HTMLDialogElement>("dialog.mo-dialog")?.close();
    this.confirming = false;
  }

  // ----------------------------------------------------------------- render
  private entitySelect(value: string, onPick: (v: string) => void, testid: string, label: string): TemplateResult {
    const list = this.entities ?? [];
    const known = !value || list.some((e) => e.entity_id === value);
    return html`<label class="mo-field"><span>${label}</span>
      <select data-testid=${testid} ?disabled=${!this.canEdit || this.busy} @change=${(e: Event) => onPick((e.target as HTMLSelectElement).value)}>
        <option value="" ?selected=${!value}>Choose…</option>
        ${known ? nothing : html`<option value=${value} selected>${value} (not found)</option>`}
        ${list.map((e) => html`<option value=${e.entity_id} ?selected=${e.entity_id === value}>${e.name} · ${e.entity_id}${e.state ? ` (${e.state})` : ""}</option>`)}
      </select></label>`;
  }

  private statusCard(m: EditorModel): TemplateResult {
    const s = m.mood;
    const d = this.detect(m);
    const label = { helpers: "Auto, Manual and Main Mood", entity: "One existing selector", builtin: "Built-in mood selector", none: "No mood source", selector: "One selector (an existing one or the built-in one)" }[d.source];
    const expected = s.expected_main_mood ?? s.expected ?? null;
    return html`<section class="card mo-card" data-testid="mo-source-status"><header class="card-head">${icon("palette")}<h3>Now</h3>
        ${s.manual_mode ? chip("warning", "Manual mode") : s.mood ? chip("ok", s.mood) : chip("neutral", "No mood")}</header>
      ${kv([
        ["Mood source", html`${label}${d.certain ? nothing : html`<span class="block small muted">The integration does not report which entities are configured; this is worked out from the moods it sees.</span>`}`],
        d.config?.main_mood_entity ? ["Main Mood selector", d.config.main_mood_entity] : null,
        ["Lighting uses", s.manual_mode ? "Nothing (Manual mode pauses automatic lighting)" : s.mood ?? "No mood"],
        ["Main Mood", s.main_mood ?? "—"],
        d.source === "helpers" ? ["Auto / Manual give", expected ? `${expected} (following ${s.follows === "auto" ? "Auto Mood" : s.follows === "manual" ? "Manual Mood" : s.follows ?? "—"})` : "—"] : null,
        s.unrepresentable ? ["Cannot be represented", html`<span class="tone-warning">${s.unrepresentable}</span> is not a Main Mood option (handling: Moods tab)`] : null,
        ["Detail", s.detail],
      ])}
      <h4 class="mo-subhead">Moods found</h4>
      ${m.moods.length ? html`<ul class="mo-found">${m.moods.map((x) => html`<li><strong>${x.name}</strong>
          ${x.in_main ? chip("ok", "Main") : chip("warning", "not in Main")}${x.in_auto ? chip("neutral", "Auto") : nothing}${x.in_manual ? chip("neutral", "Manual") : nothing}
          ${x.has_profile ? chip("info", "has profile") : nothing}</li>`)}</ul>` : html`<p class="muted">No moods found.</p>`}
    </section>`;
  }

  private formCard(m: EditorModel): TemplateResult {
    const f = this.form;
    const ed = this.canEdit && !this.busy && !this.reloading;
    const problems = this.problems();
    const set = (patch: Partial<SlMoodsSource["form"]>) => (this.form = { ...this.form, ...patch });
    return html`<section class="card mo-card" data-testid="mo-source-form"><header class="card-head">${icon("tune")}<h3>Choose the mood source</h3></header>
      <fieldset class="mo-choices"><legend class="visually-hidden">Mood source</legend>
        ${CHOICES.map((c) => html`<label class="mo-choice ${this.choice === c.key ? "selected" : ""}">
          <input type="radio" name="mo-source" value=${c.key} .checked=${this.choice === c.key} ?disabled=${!ed} @change=${() => (this.choice = c.key)} />
          <span><strong>${c.title}</strong><span class="block small muted">${c.text}</span></span></label>`)}
      </fieldset>
      ${this.choice === "helpers" ? html`<div class="mo-form-grid">
          ${this.entitySelect(f.auto, (v) => set({ auto: v }), "mo-auto", "Auto Mood")}
          ${this.entitySelect(f.manual, (v) => set({ manual: v }), "mo-manual", "Manual Mood")}
          ${this.entitySelect(f.main, (v) => set({ main: v }), "mo-main", "Main Mood (lighting follows it)")}
          <label class="mo-field"><span>Manual Mood option meaning “follow Auto”</span>
            <input type="text" maxlength=${NAME_MAX} .value=${f.autoOption} ?disabled=${!ed} @input=${(e: Event) => set({ autoOption: (e.target as HTMLInputElement).value })} /></label>
        </div>`
      : this.choice === "entity" ? html`<div class="mo-form-grid">${this.entitySelect(f.entity, (v) => set({ entity: v }), "mo-entity", "Mood selector")}</div>
        <p class="small muted">Its options are the moods. The option “${m.settings.manual_option}” (if present) pauses automatic lighting.</p>`
      : this.choice === "builtin" ? html`<div class="mo-builtin">
          <ol class="mo-builtin-list">${f.moods.map((name, i) => html`<li>
            <input type="text" maxlength=${NAME_MAX} aria-label="Mood ${i + 1}" .value=${name} ?disabled=${!ed}
              @input=${(e: Event) => set({ moods: f.moods.map((x, j) => (j === i ? (e.target as HTMLInputElement).value : x)) })} />
            <button type="button" class="icon-btn" aria-label="Move up" ?disabled=${!ed || i === 0} @click=${() => { const a = [...f.moods]; [a[i - 1], a[i]] = [a[i], a[i - 1]]; set({ moods: a }); }}>${icon("chevron-left", 18)}</button>
            <button type="button" class="icon-btn" aria-label="Remove ${name}" ?disabled=${!ed} @click=${() => set({ moods: f.moods.filter((_, j) => j !== i) })}>${icon("delete", 18)}</button></li>`)}</ol>
          <form class="mo-add-row" @submit=${(e: Event) => { e.preventDefault(); const v = this.newMood.trim(); if (v) { set({ moods: [...f.moods, v] }); this.newMood = ""; } }}>
            <input type="text" maxlength=${NAME_MAX} placeholder="New mood, e.g. Kvöld" .value=${this.newMood} ?disabled=${!ed || f.moods.length >= MAX_MOODS} @input=${(e: Event) => (this.newMood = (e.target as HTMLInputElement).value)} />
            <button type="submit" class="btn btn-small" ?disabled=${!ed || !this.newMood.trim()}>${icon("plus", 18)}Add</button></form>
          <p class="small muted">The selector also gets the option “${m.settings.manual_option}”, which pauses automatic lighting (Manual mode). Profiles are matched by mood name, so keep the names of moods that have profiles.</p>
        </div>`
      : this.choice === "none" ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>Without a mood source lighting applies no mood profile until one is chosen.</span></p>` : nothing}
      ${problems.length ? html`<ul class="plain-list small tone-warning">${problems.map((p) => html`<li>${p}</li>`)}</ul>` : nothing}
      <div class="mo-apply-row">
        <p class="small muted">${icon("info", 16)}Saving reloads the integration (a few seconds; lighting is not changed by the reload). Profiles are keyed by mood name and are kept.</p>
        ${this.canEdit ? html`<button type="button" class="btn btn-primary" data-testid="mo-source-apply" ?disabled=${!ed || problems.length > 0 || !this.choice} @click=${() => void this.openDialog()}>${icon("save", 18)}Review and save…</button>`
          : html`<span class="small muted">Only administrators can change the mood source.</span>`}
      </div>
    </section>`;
  }

  private dialogTemplate(): TemplateResult | typeof nothing {
    if (!this.confirming) return nothing;
    const p = this.payload();
    const choice = CHOICES.find((c) => c.key === this.choice);
    const rows: Array<[string, unknown]> = [["Source", choice?.title ?? this.choice]];
    if (this.choice === "helpers") rows.push(["Auto Mood", p.auto_mood_entity], ["Manual Mood", p.manual_mood_entity], ["Main Mood", p.main_mood_entity], ["“Follow Auto” option", p.auto_option]);
    if (this.choice === "entity") rows.push(["Selector", p.main_mood_entity]);
    if (this.choice === "builtin") rows.push(["Moods", (p.moods as string[]).join(", ")], ["Selector", BUILTIN_MOOD_ENTITY]);
    return html`<dialog class="sheet mo-dialog" aria-labelledby="mo-src-title" data-testid="mo-dialog" @close=${() => (this.confirming = false)}>
      <div class="sheet-inner">
        <header class="sheet-head"><h2 id="mo-src-title">Save the mood source</h2>
          <button type="button" class="icon-btn" aria-label="Close" @click=${() => this.closeDialog()}>${icon("close")}</button></header>
        <div class="sheet-body">
          ${this.error ? problemNotice(this.error) : nothing}
          ${kv(rows)}
          <p>The integration reloads to use it (a few seconds). Lights are not switched by the reload; the next mood change or trigger uses the new source.${this.choice === "none" ? " Until a source is chosen again, no mood profile is applied." : ""}</p>
        </div>
        <footer class="sheet-foot"><button type="button" class="btn" @click=${() => this.closeDialog()}>Cancel</button>
          <button type="button" class="btn btn-primary" data-testid="mo-confirm" ?disabled=${this.busy} @click=${() => void this.apply()}>${icon("save", 18)}${this.busy ? "Saving…" : "Save and reload"}</button></footer>
      </div>
    </dialog>`;
  }

  protected render(): unknown {
    const m = this.model;
    if (!m) return this.error ? problemNotice(this.error) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`;
    return html`${this.error && !this.confirming ? problemNotice(this.error) : nothing}
      ${this.notice ? html`<p class="notice notice-info" role="status" data-testid="mo-notice">${icon("ok", 20)}<span>${this.notice}</span></p>` : nothing}
      ${this.reloading ? html`<p class="notice" role="status">${icon("sync", 20)}<span>The integration is reloading…</span></p>` : nothing}
      <div class="mo-two">${this.formCard(m)}${this.statusCard(m)}</div>
      ${this.dialogTemplate()}`;
  }
}

customElements.define("sl-moods-source", SlMoodsSource);
