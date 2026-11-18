// Moods → Moods (Phase A4): every mood the integration knows (from the mood source and from profiles), which mood
// each inherits from (mood/set), the fallback mood and Manual option (settings/set), and what lighting does while
// the expected mood is not an option of Main Mood (settings/set unrepresentable).
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { cmd, lighting } from "./api";
import { LightingElement, problemNotice } from "./components";
import { editor, moodChain, type EditorModel, type MoodInfo } from "./moods-model";

export class SlMoodsList extends LightingElement {
  static properties = { ...LightingElement.properties, model: { state: true }, manualOption: { state: true }, useMood: { state: true } };
  declare model: EditorModel | null;
  declare manualOption: string | null;
  /** A mood whose handling is being switched to "use another mood" (the mood is chosen next). */
  declare useMood: string | null;

  constructor() {
    super();
    this.model = null;
    this.manualOption = null;
    this.useMood = null;
  }

  async reload(): Promise<void> {
    try {
      this.model = await editor({});
    } catch (err) {
      this.error = err;
    }
  }

  private setParent(mood: string, parent: string | null): Promise<boolean> {
    return this.run(() => lighting(cmd("mood/set"), { mood, parent }), parent ? `${mood} now inherits from ${parent}` : `${mood} no longer inherits from another mood`);
  }

  private settings(patch: Record<string, unknown>, done: string): Promise<boolean> {
    return this.run(() => lighting(cmd("settings/set"), patch), done);
  }

  private setHandling(mood: string, mode: string, other: string | null = null): void {
    if (mode === "use_mood" && !other) {
      this.useMood = mood;
      return;
    }
    this.useMood = null;
    void this.settings({ unrepresentable: { [mood]: mode === "follow_main" ? null : { mode, mood: other } } }, `Saved how ${mood} is handled`);
  }

  private handling(m: EditorModel, x: MoodInfo, ed: boolean): TemplateResult {
    if (x.in_main) return html`<span class="muted small">Main Mood option</span>`;
    const h = x.handling ?? { mode: "follow_main", mood: null };
    const mode = this.useMood === x.name ? "use_mood" : h.mode;
    const others = m.moods.filter((o) => o.name !== x.name && o.in_main);
    return html`<div class="mo-handling">
      <select aria-label="While ${x.name} is expected" ?disabled=${!ed} @change=${(e: Event) => this.setHandling(x.name, (e.target as HTMLSelectElement).value)}>
        <option value="follow_main" ?selected=${mode === "follow_main"}>Keep following Main Mood</option>
        <option value="no_mood" ?selected=${mode === "no_mood"}>No mood: change nothing</option>
        <option value="use_mood" ?selected=${mode === "use_mood"}>Use another mood</option></select>
      ${mode === "use_mood" ? html`<select aria-label="Mood to use for ${x.name}" ?disabled=${!ed} @change=${(e: Event) => this.setHandling(x.name, "use_mood", (e.target as HTMLSelectElement).value || null)}>
        <option value="" ?selected=${!h.mood}>Choose…</option>${others.map((o) => html`<option ?selected=${o.name === h.mood}>${o.name}</option>`)}</select>` : nothing}
    </div>`;
  }

  private moodsCard(m: EditorModel): TemplateResult {
    const ed = this.canEdit && (m.can_edit ?? true) && !this.busy;
    const fallback = m.settings.fallback_mood;
    return html`<section class="card mo-card wide" data-testid="mo-moods"><header class="card-head">${icon("palette")}<h3>Moods</h3>
        <span class="small muted">${m.moods.length} mood${m.moods.length === 1 ? "" : "s"}</span></header>
      <p class="small muted">A mood without its own value for a light type inherits from its parent mood, then from the fallback mood. Moods are matched by name.</p>
      <div class="table-wrap"><table class="plain-table mo-table mo-moods-table">
        <thead><tr><th scope="col">Mood</th><th scope="col">Offered by</th><th scope="col">Inherits from</th><th scope="col">Profile</th><th scope="col">If Main Mood cannot show it</th></tr></thead>
        <tbody>${m.moods.map((x) => {
          const chain = moodChain(m.moods, x.name, fallback);
          return html`<tr data-mood=${x.name}>
            <td><span class="mo-row-name">${x.name}</span>${x.name === m.mood.mood ? html` <span class="mo-now">now</span>` : nothing}${x.name === fallback ? html` <span class="inherit-tag">fallback</span>` : nothing}</td>
            <td class="small">${[x.in_main ? "Main" : null, x.in_auto ? "Auto" : null, x.in_manual ? "Manual" : null].filter(Boolean).join(" · ") || "profiles only"}
              ${x.in_main ? nothing : html`<span class="block tone-warning">not a Main Mood option</span>`}</td>
            <td><select aria-label="${x.name} inherits from" ?disabled=${!ed} @change=${(e: Event) => void this.setParent(x.name, (e.target as HTMLSelectElement).value || null)}>
              <option value="" ?selected=${!x.parent}>${fallback && fallback !== x.name ? `Nothing (then fallback ${fallback})` : "Nothing"}</option>
              ${m.moods.filter((o) => o.name !== x.name).map((o) => html`<option ?selected=${o.name === x.parent}>${o.name}</option>`)}</select>
              ${chain.length > 1 ? html`<span class="block small muted mo-chain-text">${chain.join(" → ")}</span>` : nothing}</td>
            <td>${x.has_profile ? html`<a class="inherit-tag override" href="#/moods/profiles">${icon("pencil", 14)}global entries</a>` : html`<span class="inherit-tag">no global entries</span>`}</td>
            <td>${this.handling(m, x, ed)}</td>
          </tr>`;
        })}</tbody></table></div>
    </section>`;
  }

  private settingsCard(m: EditorModel): TemplateResult {
    const ed = this.canEdit && (m.can_edit ?? true) && !this.busy;
    const s = m.settings;
    const option = this.manualOption ?? s.manual_option;
    return html`<section class="card mo-card" data-testid="mo-mood-settings"><header class="card-head">${icon("tune")}<h3>Fallback and Manual</h3></header>
      <label class="mo-field"><span>Fallback mood</span>
        <select data-testid="mo-fallback" ?disabled=${!ed} @change=${(e: Event) => void this.settings({ fallback_mood: (e.target as HTMLSelectElement).value || null }, "Saved the fallback mood")}>
          <option value="" ?selected=${!s.fallback_mood}>None</option>
          ${m.moods.map((x) => html`<option ?selected=${x.name === s.fallback_mood}>${x.name}</option>`)}</select>
        <span class="small muted">Used last by every mood: what a light type gets when neither the mood nor its parents set it.</span></label>
      <form class="mo-field" @submit=${(e: Event) => { e.preventDefault(); if (option.trim() && option.trim() !== s.manual_option) void this.settings({ manual_option: option.trim() }, "Saved the Manual option").then(() => (this.manualOption = null)); }}>
        <span>Manual option</span>
        <div class="mo-add-row"><input type="text" maxlength="100" aria-label="Manual option" .value=${option} ?disabled=${!ed} @input=${(e: Event) => (this.manualOption = (e.target as HTMLInputElement).value)} />
          <button type="submit" class="btn btn-small" ?disabled=${!ed || !option.trim() || option.trim() === s.manual_option}>${icon("save", 16)}Save</button></div>
        <span class="small muted">The mood selector option that pauses all automatic lighting (Manual mode). It is not a mood.</span></form>
    </section>`;
  }

  private explainCard(m: EditorModel): TemplateResult {
    const s = m.mood;
    const unrep = m.moods.filter((x) => !x.in_main);
    return html`<section class="card mo-card" data-testid="mo-mood-now"><header class="card-head">${icon("info")}<h3>Moods Main Mood cannot represent</h3></header>
      <p class="small">When Auto or Manual Mood asks for a mood that is not an option of Main Mood, Main Mood keeps its old value. Choose per mood what lighting does then.</p>
      ${s.unrepresentable ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>${s.detail}</span></p>` : nothing}
      ${unrep.length ? html`<p class="small muted">Not Main Mood options: ${unrep.map((x) => x.name).join(", ")}.</p>` : html`<p class="small muted">Every mood is a Main Mood option.</p>`}
    </section>`;
  }

  protected render(): unknown {
    const m = this.model;
    if (!m) return this.error ? problemNotice(this.error) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`;
    return html`${this.error ? problemNotice(this.error) : nothing}
      ${this.notice ? html`<p class="notice notice-info" role="status" data-testid="mo-notice">${icon("ok", 20)}<span>${this.notice}</span></p>` : nothing}
      ${!this.canEdit ? html`<p class="notice" role="status">${icon("info", 20)}<span>Only Home Assistant administrators can change moods.</span></p>` : nothing}
      <div class="mo-grid">${this.moodsCard(m)}${this.settingsCard(m)}${this.explainCard(m)}</div>`;
  }
}

customElements.define("sl-moods-list", SlMoodsList);
