// Curtains → Windows (Phase A5): the window list, discovery proposals and the "New window" dialog
// (``window/create``), deletion with confirmation (``window/remove``). Nothing here moves a cover.
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { chip, StoreElement } from "../ui";
import { cmd, curtains, type Candidate, type CurtainsModel, type Proposal, type WindowModel } from "./api";
import { closeDialog, modeChip, openDialog, pct, plural, problemNotice, windowWarnings, type CurtainsHost } from "./components";

interface Draft {
  name: string;
  area_id: string | null;
  direction: string | null;
  bearing: string;
  layers: { entity_id: string; kind: string }[];
}

export class SlCurtainsWindows extends StoreElement {
  static properties = { model: { attribute: false }, host: { attribute: false }, draft: { state: true }, askDelete: { state: true } };
  declare model: CurtainsModel;
  declare host: CurtainsHost;
  declare draft: Draft | null;
  declare askDelete: WindowModel | null;

  constructor() {
    super();
    this.draft = null;
    this.askDelete = null;
  }

  private startNew(p?: Proposal): void {
    this.draft = {
      name: p?.name ?? "", area_id: p?.area_id ?? null, direction: null, bearing: "",
      layers: (p?.covers ?? []).map((c) => ({ entity_id: c.entity_id, kind: c.kind })),
    };
    void openDialog(this, "dialog.cu-new-window");
  }

  private cancelNew(): void {
    closeDialog(this, "dialog.cu-new-window");
    this.draft = null;
  }

  private async create(): Promise<void> {
    const f = this.draft;
    if (!f || !f.name.trim()) return;
    const params: Record<string, unknown> = { name: f.name.trim(), area_id: f.area_id || null, layers: f.layers };
    const bearing = f.bearing.trim();
    if (bearing !== "") params.bearing = Number(bearing);
    else if (f.direction) params.direction = f.direction;
    const ok = await this.host.run(() => curtains(cmd("window/create"), params), "Window created");
    if (ok) this.cancelNew();
  }

  private async removeWindow(w: WindowModel): Promise<void> {
    const ok = await this.host.run(() => curtains(cmd("window/remove"), { window_id: w.window_id }), `${w.name} deleted`);
    if (ok) {
      closeDialog(this, "dialog.cu-delete-window");
      this.askDelete = null;
    }
  }

  private toggleCover(c: Candidate, on: boolean): void {
    const f = this.draft;
    if (!f) return;
    f.layers = on ? [...f.layers.filter((l) => l.entity_id !== c.entity_id), { entity_id: c.entity_id, kind: c.kind }] : f.layers.filter((l) => l.entity_id !== c.entity_id);
    this.draft = { ...f };
  }

  private newDialog(): TemplateResult | typeof nothing {
    const f = this.draft;
    const m = this.model;
    if (!f) return nothing;
    const chosen = new Map(f.layers.map((l) => [l.entity_id, l]));
    const byName = new Map(m.windows.map((w) => [w.window_id, w.name]));
    const cands = [...m.candidates].sort((a, b) => Number(b.area_id === f.area_id) - Number(a.area_id === f.area_id) || a.name.localeCompare(b.name));
    return html`<dialog class="sheet sheet-full review-dialog cu-new-window" aria-labelledby="cu-new-title" data-testid="curtains-new-window" @close=${() => (this.draft = null)}>
      <div class="sheet-inner">
        <header class="sheet-head"><h2 id="cu-new-title">New window</h2><button type="button" class="icon-btn" aria-label="Close" @click=${() => this.cancelNew()}>${icon("close")}</button></header>
        <div class="sheet-body">
          ${this.host.error ? problemNotice(this.host.error) : nothing}
          <div class="lp-form">
            <label class="lp-field"><span class="lp-label">Name</span><input type="text" maxlength="60" .value=${f.name} placeholder="e.g. Living room south" data-testid="curtains-new-name" @input=${(e: Event) => { f.name = (e.target as HTMLInputElement).value; this.requestUpdate(); }} /></label>
            <label class="lp-field"><span class="lp-label">Room</span><select @change=${(e: Event) => { f.area_id = (e.target as HTMLSelectElement).value || null; this.requestUpdate(); }}>
              <option value="" ?selected=${!f.area_id}>No room</option>${m.areas.map((a) => html`<option value=${a.area_id} ?selected=${a.area_id === f.area_id}>${a.name}</option>`)}</select></label>
            <label class="lp-field"><span class="lp-label">Direction (compass)</span><select ?disabled=${f.bearing.trim() !== ""} @change=${(e: Event) => { f.direction = (e.target as HTMLSelectElement).value || null; this.requestUpdate(); }}>
              <option value="" ?selected=${!f.direction}>Not set yet</option>${m.compass.map((d) => html`<option value=${d} ?selected=${d === f.direction}>${d}</option>`)}</select>
              <span class="small muted">The direction the window faces, seen from inside; needed for sun protection.</span></label>
            <label class="lp-field"><span class="lp-label">Exact bearing</span><span class="lp-input-row"><input type="number" class="lp-num" min="0" max="360" step="0.5" .value=${f.bearing} placeholder="°" @input=${(e: Event) => { f.bearing = (e.target as HTMLInputElement).value; this.requestUpdate(); }} /><span class="muted">° (0 = north, 90 = east, 180 = south; replaces the compass point)</span></span></label>
          </div>
          <h3 class="lp-h">Covers</h3>
          ${cands.length ? html`<ul class="cu-rows">${cands.map((c) => {
            const own = chosen.get(c.entity_id);
            const taken = !!c.assigned_to;
            return html`<li class="cu-row"><label class="lp-check"><input type="checkbox" .checked=${!!own} ?disabled=${taken} @change=${(e: Event) => this.toggleCover(c, (e.target as HTMLInputElement).checked)} />
                <span><strong>${c.name}</strong><span class="block small muted"><code>${c.entity_id}</code> · ${c.area_name ?? "No room"} · ${c.available ? pct(c.position) : "unavailable"} · ${c.caps.position ? "positions" : "open / close only"}${taken ? ` · in ${byName.get(c.assigned_to!) ?? c.assigned_to}` : ""}${c.members.length ? ` · group of ${c.members.length}` : ""}</span></span></label>
              ${own ? html`<select aria-label=${`Type of ${c.name}`} @change=${(e: Event) => { own.kind = (e.target as HTMLSelectElement).value; this.requestUpdate(); }}>${m.kinds.map((k) => html`<option value=${k} ?selected=${k === own.kind}>${m.words.kinds[k]}</option>`)}</select>` : nothing}</li>`;
          })}</ul>` : html`<p class="muted">No cover entities found in Home Assistant.</p>`}
          <p class="small muted">Presets are created from the covers: Fully open / Half open / Fully closed, or five coordinated presets when a window has a sheer and a blackout layer. Every position can be changed afterwards. Creating a window never moves a cover.</p>
        </div>
        <footer class="sheet-foot">
          <button type="button" class="btn" @click=${() => this.cancelNew()}>Cancel</button>
          <button type="button" class="btn btn-primary" data-testid="curtains-new-create" ?disabled=${this.host.busy || !f.name.trim()} @click=${() => void this.create()}>${icon("plus", 18)}Create window</button>
        </footer>
      </div>
    </dialog>`;
  }

  private deleteDialog(): TemplateResult | typeof nothing {
    const w = this.askDelete;
    if (!w) return nothing;
    return html`<dialog class="sheet cu-delete-window" aria-labelledby="cu-del-title" @close=${() => (this.askDelete = null)}>
      <div class="sheet-inner"><header class="sheet-head"><h2 id="cu-del-title">Delete ${w.name}?</h2>
        <button type="button" class="icon-btn" aria-label="Close" @click=${() => { closeDialog(this, "dialog.cu-delete-window"); this.askDelete = null; }}>${icon("close")}</button></header>
        <div class="sheet-body">${this.host.error ? problemNotice(this.host.error) : nothing}
          <p>The window, its ${plural(w.presets.length, "preset")} and its Global Modes assignments are removed. The covers are not moved.</p></div>
        <footer class="sheet-foot"><button type="button" class="btn" @click=${() => { closeDialog(this, "dialog.cu-delete-window"); this.askDelete = null; }}>Cancel</button>
          <button type="button" class="btn btn-danger" data-testid="curtains-delete-confirm" ?disabled=${this.host.busy} @click=${() => void this.removeWindow(w)}>${icon("delete", 18)}Delete window</button></footer></div></dialog>`;
  }

  protected render(): unknown {
    const m = this.model;
    const can = this.host.canEdit;
    return html`<div data-testid="curtains-windows">
      <p class="muted small">A window's settings define its covers, its adjustable presets (also for single covers), its direction and sun protection, sleep protection, the window lamp, manual-move behaviour and its mode (Off / Observing / Automatic). When things happen and which preset is used are set on Global Schedule and Global Modes.</p>
      ${can ? html`<div class="toolbar"><button type="button" class="btn btn-primary" data-testid="curtains-new" ?disabled=${this.host.busy || m.windows.length >= m.limits.windows} @click=${() => this.startNew()}>${icon("plus", 20)}New window</button>
        <span class="small muted">${m.windows.length} of ${m.limits.windows} windows</span></div>` : nothing}
      <div class="grid engine-grid">
        <section class="card wide"><header class="card-head">${icon("window")}<h3>Windows</h3></header>
          ${m.windows.length ? html`<div class="table-wrap"><table class="plain-table" data-testid="curtains-window-list"><thead><tr><th scope="col">Window</th><th scope="col">Room</th><th scope="col">Direction</th><th scope="col">Covers</th><th scope="col">Presets</th><th scope="col">Mode</th><th scope="col"></th></tr></thead>
            <tbody>${m.windows.map((w) => {
              const warn = windowWarnings(w);
              return html`<tr data-window=${w.window_id}><td><a class="text-link" href=${`#/curtains/windows/${encodeURIComponent(w.window_id)}`}><strong>${w.name}</strong></a>${warn.length ? html`<span class="block small tone-warning">${warn[0]}</span>` : nothing}</td>
                <td>${w.area_name ?? html`<span class="muted">No room</span>`}</td><td>${w.compass ? `${w.compass}${w.effective_bearing !== null ? ` ${Math.round(w.effective_bearing)}°` : ""}` : html`<span class="muted">not set</span>`}</td>
                <td>${w.layers.length}</td><td>${w.presets.length}</td><td>${modeChip(w.mode, w.mode_word)}</td>
                <td class="cell-action"><div class="row-actions"><a class="btn btn-small" href=${`#/curtains/windows/${encodeURIComponent(w.window_id)}`}>${icon("pencil", 16)}Open</a>
                  ${can ? html`<button type="button" class="btn btn-small btn-danger" aria-label=${`Delete ${w.name}`} ?disabled=${this.host.busy || w.running} @click=${() => { this.askDelete = w; void openDialog(this, "dialog.cu-delete-window"); }}>${icon("delete", 16)}</button>` : nothing}</div></td></tr>`;
            })}</tbody></table></div>`
          : html`<div class="empty-state"><p><strong>No windows yet.</strong></p><p class="muted">${can ? "Create one, or start from a suggestion below." : "An administrator can create windows."}</p></div>`}
        </section>
        ${m.proposals.length ? html`<section class="card wide" data-testid="curtains-proposals"><header class="card-head">${icon("search")}<h3>Suggested windows</h3>${chip("info", String(m.proposals.length))}</header>
          <p class="muted small">Covers that seem to belong together (same room, same name apart from the layer). Review each one before it is created; nothing moves.</p>
          <ul class="cu-rows">${m.proposals.map((p) => html`<li class="cu-row"><span class="cu-row-main"><strong>${p.name}</strong><span class="small muted">${p.area_name ?? "No room"} · ${p.covers.map((c) => `${c.name} (${m.words.kinds[c.kind] ?? c.kind})`).join(" · ")}</span></span>
            ${can ? html`<button type="button" class="btn btn-small" @click=${() => this.startNew(p)}>${icon("plus", 16)}Create…</button>` : nothing}</li>`)}</ul></section>` : nothing}
      </div>
      ${this.newDialog()}${this.deleteDialog()}
    </div>`;
  }
}

customElements.define("sl-curtains-windows", SlCurtainsWindows);
