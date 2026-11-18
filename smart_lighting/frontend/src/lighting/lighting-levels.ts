// Ambient light categories (0.6.8): the global categories, a room's own, or "Use Global Levels". Every draft is
// previewed by the runtime (``ambient/levels``, no confirm) and saved with ``confirm``; a removed category that
// still has values needs a resolution (move them to another category, or delete them) before it can be saved.
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { StoreElement } from "../ui";
import { cmd, lighting } from "./api";
import { problemNotice } from "./components";
import { chip } from "../ui";
import type { LightingHost, Obj } from "./lighting-model";

interface LevelDraft { id?: string; name: string; start: number | "" }
interface Draft { scope: "room" | "global"; inherit: boolean; levels: LevelDraft[]; resolve: Record<string, string | null>; preview: Obj | null; error: string | null }

export class SlLpLevels extends StoreElement {
  static properties = {
    areaId: { type: String }, roomName: { type: String }, ambient: { attribute: false }, levels: { attribute: false },
    limits: { attribute: false }, current: { type: String }, host: { attribute: false }, canEdit: { type: Boolean }, busy: { type: Boolean },
    draft: { state: true },
  };
  /** The room (null: the global categories only, as on the Settings page). */
  declare areaId: string | null;
  declare roomName: string;
  declare ambient: Obj;
  declare levels: Obj[];
  declare limits: Obj;
  declare current: string | null;
  declare host: LightingHost;
  declare canEdit: boolean;
  declare busy: boolean;
  declare draft: Draft | null;
  private timer: number | undefined;

  constructor() {
    super();
    this.areaId = null;
    this.roomName = "";
    this.levels = [];
    this.current = null;
    this.draft = null;
    this.canEdit = false;
    this.busy = false;
  }

  private start(scope: "room" | "global", inherit = false): void {
    const src: Obj[] = scope === "global" ? this.ambient?.global_levels ?? this.levels : this.levels;
    this.draft = { scope, inherit, levels: src.map((l) => ({ id: l.id ?? l.key, name: l.name, start: l.start })), resolve: {}, preview: null, error: null };
    this.schedule();
  }

  private patch(p: Partial<Draft>): void {
    if (!this.draft) return;
    this.draft = { ...this.draft, ...p };
    if ("levels" in p || "resolve" in p) this.schedule();
  }

  private message(confirm: boolean): Obj {
    const d = this.draft!;
    const msg: Obj = {
      levels: d.inherit ? null : d.levels.map((l, i) => ({ ...(l.id ? { id: l.id } : {}), name: l.name.trim(), start: i === 0 ? 0 : Number(l.start) })),
    };
    if (d.scope === "room" && this.areaId) msg.area_id = this.areaId;
    if (Object.keys(d.resolve).length) msg.resolve = d.resolve;
    if (confirm) msg.confirm = true;
    return msg;
  }

  private schedule(): void {
    window.clearTimeout(this.timer);
    const d = this.draft;
    if (!d) return;
    if (!d.inherit && d.levels.some((l, i) => (i > 0 && (l.start === "" || Number.isNaN(Number(l.start)))) || !l.name.trim())) {
      this.draft = { ...d, preview: null, error: "Every category needs a name and the lux value it starts at." };
      return;
    }
    this.timer = window.setTimeout(async () => {
      try {
        const res = await lighting<Obj>(cmd("ambient/levels"), this.message(false));
        if (this.draft) this.draft = { ...this.draft, preview: res.preview, error: null };
      } catch (err) {
        if (this.draft) this.draft = { ...this.draft, preview: null, error: err instanceof Error ? err.message : String(err) };
      }
    }, 300);
  }

  private async save(): Promise<void> {
    const ok = await this.host.run(() => lighting(cmd("ambient/levels"), this.message(true)), "Categories saved · nothing was sent");
    if (ok) this.draft = null;
  }

  private table(): TemplateResult {
    return html`<div class="table-wrap"><table class="plain-table lp-table"><thead><tr><th scope="col">Category</th><th scope="col">Starts at</th><th scope="col">Range</th></tr></thead>
      <tbody>${this.levels.map((l) => html`<tr class=${l.key === this.current ? "selected" : ""}><th scope="row">${l.name}${l.key === this.current ? html` ${chip("ok", "now")}` : nothing}</th>
        <td>${l.start} lx</td><td class="small muted">${l.range ?? ""}</td></tr>`)}</tbody></table></div>`;
  }

  private editor(d: Draft): TemplateResult {
    const max = this.limits?.levels?.[1] ?? this.ambient?.max_levels ?? 12;
    const min = this.limits?.levels?.[0] ?? 2;
    const p = d.preview;
    const setLevel = (i: number, patch: Partial<LevelDraft>) => this.patch({ levels: d.levels.map((l, j) => (j === i ? { ...l, ...patch } : l)) });
    const changes = (p?.rooms ?? []).filter((x: Obj) => x.removed.length || x.added.length || x.renamed.length || x.moved.length);
    return html`<div class="lp-subcard lp-levels-editor" data-testid="lighting-levels-editor">
      <h4 class="lp-h">${d.inherit ? `Use the global categories in ${this.roomName}` : d.scope === "global" ? "Edit the global categories (every room without its own)" : `Edit ${this.roomName}'s own categories`}</h4>
      ${d.inherit ? html`<p class="small">${this.roomName}'s own categories are dropped; it follows the global ones again.</p>` : html`
        <div class="table-wrap"><table class="plain-table lp-table"><thead><tr><th scope="col">Name</th><th scope="col">Starts at</th><th scope="col">Range</th><th scope="col"><span class="visually-hidden">Remove</span></th></tr></thead>
          <tbody>${d.levels.map((l, i) => {
            const next = d.levels[i + 1];
            const s = i === 0 ? 0 : l.start;
            const range = s === "" ? "—" : next ? (i === 0 ? `below ${next.start} lx` : `${s} to below ${next.start} lx`) : `${s} lx and above`;
            return html`<tr><td><input type="text" maxlength="40" .value=${l.name} aria-label=${`Category ${i + 1} name`} @change=${(e: Event) => setLevel(i, { name: (e.target as HTMLInputElement).value })} /></td>
              <td><span class="lp-input-row"><input type="number" min="0" max=${this.limits?.threshold?.[1] ?? 2000} .value=${String(s)} ?disabled=${i === 0} aria-label=${`${l.name || `Category ${i + 1}`} starts at`}
                @change=${(e: Event) => { const v = (e.target as HTMLInputElement).value; setLevel(i, { start: v === "" ? "" : Number(v) }); }} /> lx</span></td>
              <td class="small muted">${range}</td>
              <td>${d.levels.length > min ? html`<button type="button" class="btn btn-small" aria-label=${`Remove ${l.name}`} @click=${() => this.patch({ levels: d.levels.filter((_, j) => j !== i) })}>${icon("delete", 16)}</button>` : nothing}</td></tr>`;
          })}</tbody></table></div>
        ${d.levels.length < max ? html`<button type="button" class="btn btn-small" @click=${() => { const last = d.levels[d.levels.length - 1]; this.patch({ levels: [...d.levels, { name: "", start: (Number(last?.start) || 0) + 10 }] }); }}>${icon("plus", 16)}Add category</button>
          <span class="small muted">at most ${max}</span>` : html`<p class="small muted">At most ${max} categories.</p>`}`}
      ${d.error ? html`<p class="notice notice-warning" role="alert">${icon("warning", 20)}<span>${d.error}</span></p>` : nothing}
      ${p ? html`<div class="lp-review"><h4 class="lp-h">Review</h4>
        ${changes.length ? html`<ul class="lp-dlist">${changes.map((x: Obj) => html`<li><strong>${x.name}</strong><ul class="lp-dlist">
          ${x.removed.map((rm: Obj) => {
            const cells = (x.cells as Obj[]).filter((c) => c.level === rm.id);
            const cur = d.resolve[rm.id];
            const value = cur === undefined ? "" : cur === null ? "__delete" : cur;
            return html`<li><strong>${rm.name}</strong> is removed${cells.length ? `: ${cells.map((c) => `${c.light} · ${c.column_name ?? c.column} ${c.text}`).join("; ")}` : " (no values)"}
              ${cells.length ? html`<select aria-label=${`What happens to the values of ${rm.name}`} @change=${(e: Event) => {
                const v = (e.target as HTMLSelectElement).value; const r = { ...d.resolve };
                if (v === "") delete r[rm.id]; else r[rm.id] = v === "__delete" ? null : v; this.patch({ resolve: r }); }}>
                <option value="" ?selected=${value === ""}>Choose…</option>
                ${(x.after as Obj[]).map((a) => html`<option value=${a.id} ?selected=${value === a.id}>Move them to ${a.name} (replaces its value)</option>`)}
                <option value="__delete" ?selected=${value === "__delete"}>Delete them</option></select>` : nothing}</li>`;
          })}
          ${x.added.map((n: string) => html`<li>${n} is added: Not configured in every brightness matrix.</li>`)}
          ${x.renamed.map((n: Obj) => html`<li>${n.from} is renamed ${n.to}: its values are kept.</li>`)}
          ${x.moved.map((n: Obj) => html`<li>${n.name} now starts at ${n.to} lx (was ${n.from} lx).</li>`)}
          ${x.fallback ? html`<li>The fallback category changes: ${typeof x.fallback === "object" ? `${x.fallback.from} → ${x.fallback.to}` : x.fallback}.</li>` : nothing}
          </ul></li>`)}</ul>` : html`<p class="small muted">Nothing changes for any brightness matrix.</p>`}
        ${(p.unaffected ?? []).length ? html`<p class="small muted">Keep their own categories: ${p.unaffected.join(", ")}</p>` : nothing}
        ${(p.errors ?? []).length ? html`<p class="small tone-warning">${p.errors.join("; ")}</p>` : nothing}
        ${(p.unresolved ?? []).length ? html`<p class="small tone-warning">Choose what happens to the values of the removed categories first.</p>` : nothing}
        <p class="small muted">Saving sends no command: the next trigger uses the new categories.</p></div>`
        : d.error ? nothing : html`<p class="small muted">Checking the change…</p>`}
      <div class="row-actions lp-row"><button type="button" class="btn" @click=${() => (this.draft = null)}>Cancel</button>
        <button type="button" class="btn btn-primary" data-testid="lighting-levels-save" ?disabled=${this.busy || !p?.ready || !!d.error} @click=${() => void this.save()}>${icon("check", 18)}Save categories</button></div>
    </div>`;
  }

  protected render(): unknown {
    if (this.draft) return html`${this.host?.error && this.draft ? problemNotice(this.host.error) : nothing}${this.editor(this.draft)}`;
    const own = this.ambient?.levels_source === "room";
    const others: string[] = (this.ambient?.global_rooms ?? []).filter((n: string) => n !== this.roomName);
    return html`${this.areaId ? html`<p class="small">${own ? html`<span class="inherit-tag override">Room value</span> ${this.roomName} has its own categories.`
        : html`<span class="inherit-tag">Global</span> ${this.roomName} uses the global categories${others.length ? ` (like ${others.slice(0, 4).join(", ")}${others.length > 4 ? "…" : ""})` : ""}.`}</p>` : nothing}
      ${this.table()}
      <p class="small muted">Ranges are contiguous: each category starts where the one before ends; a reading exactly at a threshold belongs to the brighter one.</p>
      ${this.canEdit ? html`<div class="row-actions lp-row">
        ${this.areaId ? html`<button type="button" class="btn btn-small" @click=${() => this.start("room")}>${icon("pencil", 16)}${own ? "Edit this room's categories" : "Own categories for this room"}</button>` : nothing}
        <button type="button" class="btn btn-small" data-testid="lighting-levels-edit-global" @click=${() => this.start("global")}>${icon("pencil", 16)}Edit global categories</button>
        ${this.areaId && own ? html`<button type="button" class="btn btn-small" @click=${() => this.start("room", true)}>${icon("undo", 16)}Use Global Levels</button>` : nothing}</div>` : nothing}`;
  }
}

customElements.define("sl-lp-levels", SlLpLevels);
