// Phase A4: Switches page, Priority Inputs of one room (``priority/set``, ``priority/preview``, ``priority/remove``,
// ``priority/lock``). An input is saved disabled; arming it needs its preview and an explicit confirmation.
import { html, nothing, type ReactiveController, type TemplateResult } from "lit";

import { icon } from "../icons";
import { haTime } from "../presence/components";
import { chip } from "../ui";
import { cmd, lighting } from "./api";
import { problemNotice } from "./components";
import {
  PRIORITY_STATUS,
  type PriorityInput,
  type PriorityModel,
  type PriorityPreview,
  type SwitchesHost,
} from "./switches-model";

type Draft = Omit<PriorityInput, "id" | "enabled_at" | "enabled_by"> & { id?: string };

const NEW_DRAFT: Omit<Draft, "name"> = {
  entity_id: null, enabled: false, active_when: "on", targets: [], brightness_pct: 100, appearance: "none", kelvin: null,
  debounce: 0, release_delay: 2, unavailable: "hold", grace: 60, override_manual: false, release_manual: "respect",
};

function seconds(n: number): string {
  return n >= 120 ? `${Math.round(n / 60)} min` : `${n} s`;
}

export class PriorityController implements ReactiveController {
  private host: SwitchesHost;
  /** Area of the open draft; a draft of another room is dropped. */
  private area: string | null = null;
  selected: string | null = null;
  draft: Draft | null = null;
  advanced = false;
  preview: PriorityPreview | null = null;
  dialog: "preview" | "delete" | "lock" | "unlock" | null = null;
  dialogError: unknown = null;
  private openDialog = false;

  constructor(host: SwitchesHost) {
    this.host = host;
    host.addController(this);
  }

  hostUpdated(): void {
    if (this.openDialog) {
      this.openDialog = false;
      const el = (this.host as unknown as HTMLElement).querySelector<HTMLDialogElement>("dialog.sw-priority-dialog");
      if (el && !el.open) el.showModal();
    }
  }

  private change(patch: Partial<Draft>): void {
    if (!this.draft) return;
    this.draft = { ...this.draft, ...patch };
    this.host.requestUpdate();
  }

  private show(kind: NonNullable<PriorityController["dialog"]>): void {
    this.dialog = kind;
    this.dialogError = null;
    this.openDialog = true;
    this.host.requestUpdate();
  }

  private close(): void {
    (this.host as unknown as HTMLElement).querySelector<HTMLDialogElement>("dialog.sw-priority-dialog")?.close();
    this.dialog = null;
    this.preview = null;
    this.host.requestUpdate();
  }

  private edit(p: PriorityModel, id: string | null): void {
    this.selected = id;
    const x = id ? p.inputs.find((i) => i.id === id) : null;
    if (x) {
      const { status: _s, entity_name: _n, missing_targets: _m, enabled_at: _a, enabled_by: _b, ...rest } = x;
      this.draft = JSON.parse(JSON.stringify(rest)) as Draft;
    } else {
      this.draft = { ...NEW_DRAFT, targets: [], name: `Priority Input ${p.inputs.length + 1}` };
    }
    this.advanced = false;
    this.host.requestUpdate();
  }

  private message(d: Draft): Record<string, unknown> {
    const out: Record<string, unknown> = {
      name: d.name.trim() || "Priority Input", entity_id: d.entity_id || null, active_when: d.active_when, targets: d.targets,
      brightness_pct: d.brightness_pct ?? null, appearance: d.appearance,
      kelvin: d.appearance === "kelvin" ? (d.kelvin ?? 4000) : null, debounce: d.debounce ?? 0,
      release_delay: d.release_delay ?? 2, unavailable: d.unavailable, grace: d.grace ?? 60,
      override_manual: !!d.override_manual, release_manual: d.release_manual,
    };
    if (d.id) out.id = d.id;
    return out;
  }

  private async save(areaId: string): Promise<void> {
    const d = this.draft;
    if (!d) return;
    let saved: string | null = null;
    const ok = await this.host.run(async () => {
      const res = await lighting<{ input: PriorityInput }>(cmd("priority/set"), { area_id: areaId, input: this.message(d) });
      saved = res.input.id;
    }, d.enabled ? "Saved · the input stays armed · nothing was sent" : "Saved (disabled) · nothing was sent");
    if (ok) {
      this.selected = saved;
      this.draft = null;
    }
  }

  private async review(areaId: string): Promise<void> {
    const d = this.draft;
    if (!d) return;
    this.dialogError = null;
    this.host.busy = true;
    this.host.requestUpdate();
    try {
      this.preview = await lighting<PriorityPreview>(cmd("priority/preview"), { area_id: areaId, input: this.message(d) });
      this.show("preview");
    } catch (err) {
      this.preview = null;
      this.dialogError = err;
      this.show("preview");
    } finally {
      this.host.busy = false;
      this.host.requestUpdate();
    }
  }

  private async arm(areaId: string): Promise<void> {
    const d = this.draft;
    if (!d) return;
    let saved: string | null = null;
    const ok = await this.host.run(async () => {
      const res = await lighting<{ input: PriorityInput }>(cmd("priority/set"),
        { area_id: areaId, input: { ...this.message(d), enabled: true }, confirm: true });
      saved = res.input.id;
    }, "Enabled · it acts when its input becomes active");
    if (ok) {
      this.selected = saved;
      this.draft = null;
      this.close();
    }
  }

  private async disable(areaId: string, id: string): Promise<void> {
    if (await this.host.run(() => lighting(cmd("priority/set"), { area_id: areaId, input: { id, enabled: false } }), "Disabled · it sends nothing")) {
      this.draft = null;
    }
  }

  private async remove(areaId: string, id: string): Promise<void> {
    if (await this.host.run(() => lighting(cmd("priority/remove"), { area_id: areaId, input_id: id }), "Priority Input deleted")) {
      this.selected = null;
      this.draft = null;
      this.close();
    }
  }

  private async lock(value: boolean): Promise<void> {
    if (await this.host.run(() => lighting(cmd("priority/lock"), { lock: value }), value ? "Every Priority Input is locked" : "Priority Inputs unlocked")) {
      this.close();
    }
  }

  // ------------------------------------------------------------ render
  render(areaId: string, roomName: string, p: PriorityModel | null): TemplateResult {
    if (this.area !== areaId) {
      this.area = areaId;
      this.selected = null;
      this.draft = null;
    }
    if (!p) {
      return html`<div class="card empty-state">${icon("info", 32)}<h3>Not available</h3>
        <p class="muted">Priority Inputs exist for enabled rooms only.</p></div>`;
    }
    const canEdit = this.host.canEdit;
    const current = this.draft ? null : p.inputs.find((i) => i.id === this.selected) ?? p.inputs[0] ?? null;
    return html`
      <p class="notice notice-info sw-explain">${icon("shield-alert", 20)}<span><strong>A Priority Input forces lights On while it is active</strong>
        (for example a cupboard door contact for the cupboard light). An armed input's claim wins over every rule for its lights:
        also when the room is vacant, in daylight, in a Manual mood and when the room's automatic lighting is Observing — it is
        sent even then. When it ends, each light gets its current automatic rule. A new input sends nothing until you review and enable it.</span></p>
      ${this.lockCard(p)}
      <div class="sw-split">
        <section class="card sw-list-card" data-testid="priority-list">
          <header class="card-head">${icon("shield-check")}<h3>Inputs in ${roomName}</h3>${chip("neutral", `${p.inputs.length} of ${p.limits.max_inputs}`)}</header>
          ${p.inputs.length
            ? html`<ul class="sw-list">${p.inputs.map((x) => {
                const st = PRIORITY_STATUS[x.status?.status ?? (x.enabled ? "inactive" : "disabled")] ?? { tone: "neutral" as const, label: x.status?.status ?? "—" };
                const selected = (this.draft ? this.draft.id : current?.id) === x.id;
                return html`<li><button type="button" class="sw-row ${selected ? "selected" : ""}" data-input=${x.id} aria-pressed=${selected ? "true" : "false"}
                    @click=${() => { this.draft = null; this.selected = x.id; this.host.requestUpdate(); }}>
                  <span class="sw-row-main"><strong>${x.name}</strong>
                    <span class="small muted block">${x.entity_name || x.entity_id || "no input chosen"} → ${x.targets.length} light${x.targets.length === 1 ? "" : "s"}</span></span>
                  ${chip(st.tone, st.label)}</button></li>`;
              })}</ul>`
            : html`<p class="muted">No Priority Input in this room.</p>`}
          ${canEdit && p.inputs.length < p.limits.max_inputs
            ? html`<button type="button" class="btn" data-testid="priority-add" ?disabled=${this.host.busy} @click=${() => this.edit(p, null)}>${icon("plus", 18)}Add Priority Input</button>`
            : nothing}
        </section>
        <section class="sw-inspector" aria-label="Priority Input">
          ${this.draft ? this.form(areaId, p, this.draft) : current ? this.details(areaId, p, current) : html`<div class="card empty-state">${icon("shield-check", 32)}
            <p class="muted">Choose an input to see what it does${canEdit ? ", or add one" : ""}.</p></div>`}
        </section>
      </div>
      ${this.renderDialog(areaId, p)}`;
  }

  private lockCard(p: PriorityModel): TemplateResult {
    const canEdit = this.host.canEdit;
    return html`<div class="sw-lock ${p.lock ? "sw-lock-on" : ""}" data-testid="priority-lock">
      ${icon(p.lock ? "lock" : "lock-open", 22)}
      <span><strong>${p.lock ? "Safety lock on: no Priority Input sends anything, in any room." : "Safety lock off: armed inputs act."}</strong>
        <span class="block small muted">The lock applies to every room at once.</span></span>
      ${canEdit ? html`<button type="button" class="btn btn-small ${p.lock ? "" : "btn-danger"}" ?disabled=${this.host.busy}
        @click=${() => this.show(p.lock ? "unlock" : "lock")}>${icon(p.lock ? "lock-open" : "lock", 16)}${p.lock ? "Unlock…" : "Lock all…"}</button>` : nothing}
    </div>`;
  }

  private lightName(p: PriorityModel, e: string): string {
    return p.lights.find((l) => l.entity_id === e)?.name ?? e;
  }

  private details(areaId: string, p: PriorityModel, x: PriorityModel["inputs"][number]): TemplateResult {
    const fmt = haTime(false);
    const st = x.status;
    const word = PRIORITY_STATUS[st?.status ?? (x.enabled ? "inactive" : "disabled")] ?? { tone: "neutral" as const, label: st?.status ?? "—" };
    const timer = st?.timer === "release" ? `releasing at ${fmt(st.timer_due)}` : st?.timer === "grace" ? `held until ${fmt(st.timer_due)}` : st?.timer === "entry" ? "confirming" : null;
    const canEdit = this.host.canEdit;
    return html`<div class="card" data-testid="priority-details">
      <header class="card-head">${icon("shield-check")}<h3>${x.name}</h3>${chip(word.tone, word.label)}</header>
      <dl class="kv">
        <dt>Input</dt><dd>${x.entity_name || x.entity_id || "not chosen"}${x.entity_id ? html` <span class="mono small muted">${x.entity_id}</span>` : nothing}</dd>
        <dt>Now</dt><dd>${x.entity_id ? `${st?.input_state ?? "—"} · active when ${x.active_when === "on" ? "on (open)" : "off (normally closed contact)"}` : "—"}${timer ? ` · ${timer}` : ""}</dd>
        <dt>Forces On</dt><dd>${x.targets.length ? x.targets.map((e) => this.lightName(p, e)).join(", ") : "no light"}
          ${x.missing_targets.length ? html`<span class="block tone-warning small">Not lights of this room any more: ${x.missing_targets.join(", ")}</span>` : nothing}</dd>
        <dt>Brightness</dt><dd>${x.brightness_pct != null ? `${x.brightness_pct} % where dimmable` : "the light's own last brightness"}</dd>
        <dt>Colour</dt><dd>${x.appearance === "kelvin" ? `white at ${x.kelvin} K` : x.appearance === "mood" ? "the room mood's colour" : "left as it is"}</dd>
        <dt>Timing</dt><dd>confirm after ${seconds(x.debounce)} · release after ${seconds(x.release_delay)}</dd>
        <dt>If unavailable</dt><dd>${x.unavailable === "hold" ? `hold ${seconds(x.grace)}, then release` : x.unavailable === "keep" ? "keep until it reports again" : "release at once"}</dd>
        <dt>Manual changes</dt><dd>${x.override_manual ? "may override an earlier manual change" : "an earlier manual change is respected"} ·
          a change while active is ${x.release_manual === "respect" ? "kept after release" : "ended at release"}</dd>
        ${x.enabled_at ? html`<dt>${x.enabled ? "Armed" : "Last armed"}</dt><dd>${fmt(x.enabled_at)}</dd>` : nothing}
      </dl>
      ${st?.status === "unavailable" ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>The input is unavailable since ${fmt(st.unavailable_since)}: it is not read as open or closed.
        ${x.unavailable === "hold" ? `An active claim is held ${seconds(x.grace)}, then released.` : x.unavailable === "keep" ? "An active claim is kept until it reports again." : "An active claim was released."}</span></p>` : nothing}
      ${st?.history.length ? html`<h4 class="sw-sub">Activation history</h4><ul class="sw-history">${st.history.slice().reverse().slice(0, 6).map((h) => html`<li><span class="muted small">${fmt(h.at)}</span> ${h.text}</li>`)}</ul>` : nothing}
      ${canEdit ? html`<div class="sw-actions">
        <button type="button" class="btn" ?disabled=${this.host.busy} @click=${() => this.edit(p, x.id)}>${icon("pencil", 18)}Edit</button>
        ${x.enabled
          ? html`<button type="button" class="btn" ?disabled=${this.host.busy} @click=${() => void this.disable(areaId, x.id)}>${icon("power", 18)}Disable</button>`
          : html`<button type="button" class="btn btn-primary" ?disabled=${this.host.busy || !x.entity_id || !x.targets.length}
              @click=${() => { this.edit(p, x.id); void this.review(areaId); }}>${icon("eye", 18)}Review and enable…</button>`}
        <button type="button" class="btn btn-danger" data-testid="priority-delete" ?disabled=${this.host.busy} @click=${() => { this.selected = x.id; this.show("delete"); }}>${icon("delete", 18)}Delete…</button>
      </div>` : nothing}
    </div>`;
  }

  private form(areaId: string, p: PriorityModel, d: Draft): TemplateResult {
    const lights = new Set(p.lights.map((l) => l.entity_id));
    const cands = p.candidates.filter((c) => !lights.has(c.entity_id));
    if (d.entity_id && !cands.some((c) => c.entity_id === d.entity_id)) {
      cands.unshift({ entity_id: d.entity_id, name: d.entity_id, domain: d.entity_id.split(".")[0], device_class: null, in_area: false, state: null, chosen: true, contact: false });
    }
    const num = (e: Event): number | null => {
      const raw = (e.target as HTMLInputElement).value.trim();
      return raw === "" ? null : Number(raw);
    };
    const lim = p.limits;
    const ready = !!d.entity_id && d.targets.length > 0;
    return html`<div class="card" data-testid="priority-form">
      <header class="card-head">${icon("pencil")}<h3>${d.id ? `Edit ${d.name}` : "New Priority Input"}</h3>
        ${chip(d.enabled ? "ok" : "neutral", d.enabled ? "Armed" : "Disabled")}</header>
      <div class="sw-form">
        <label class="field"><span>Name</span><input type="text" maxlength="60" .value=${d.name} @input=${(e: Event) => this.change({ name: (e.target as HTMLInputElement).value })} /></label>
        <label class="field"><span>Input</span>
          <select data-field="entity_id" @change=${(e: Event) => this.change({ entity_id: (e.target as HTMLSelectElement).value || null })}>
            <option value="" ?selected=${!d.entity_id}>Choose an input…</option>
            ${cands.map((c) => html`<option value=${c.entity_id} ?selected=${d.entity_id === c.entity_id}>${c.name} · ${c.entity_id}${c.contact ? " (contact)" : ""}${c.domain === "switch" ? " (switch: only if it is an input)" : ""}${c.in_area ? " · in this room" : ""}</option>`)}
          </select></label>
        <label class="field"><span>Active when</span>
          <select @change=${(e: Event) => this.change({ active_when: (e.target as HTMLSelectElement).value as "on" | "off" })}>
            <option value="on" ?selected=${d.active_when === "on"}>on (door open)</option>
            <option value="off" ?selected=${d.active_when === "off"}>off (normally closed contact)</option>
          </select></label>
        <label class="field"><span>Brightness (%)</span><input type="number" min="1" max="100" placeholder="last" .value=${d.brightness_pct == null ? "" : String(d.brightness_pct)}
          @change=${(e: Event) => this.change({ brightness_pct: num(e) })} /><span class="small muted">dimmable lights only; empty: the light's own last brightness</span></label>
        <label class="field"><span>Colour</span>
          <select @change=${(e: Event) => this.change({ appearance: (e.target as HTMLSelectElement).value as Draft["appearance"] })}>
            <option value="none" ?selected=${d.appearance === "none"}>Leave the colour</option>
            <option value="mood" ?selected=${d.appearance === "mood"}>The room mood's colour</option>
            <option value="kelvin" ?selected=${d.appearance === "kelvin"}>White at a colour temperature</option>
          </select><span class="small muted">only where the light supports it</span></label>
        ${d.appearance === "kelvin" ? html`<label class="field"><span>Colour temperature (K)</span><input type="number" min=${lim.kelvin[0]} max=${lim.kelvin[1]} step="100" placeholder="4000"
          .value=${d.kelvin == null ? "" : String(d.kelvin)} @change=${(e: Event) => this.change({ kelvin: num(e) })} /></label>` : nothing}
      </div>
      <fieldset class="sw-targets"><legend>Lights it forces On</legend>
        ${p.lights.length ? p.lights.map((l) => html`<label class="sw-check"><input type="checkbox" .checked=${d.targets.includes(l.entity_id)} data-target=${l.entity_id}
            @change=${(e: Event) => this.change({ targets: (e.target as HTMLInputElement).checked ? [...d.targets, l.entity_id] : d.targets.filter((t) => t !== l.entity_id) })} />
          <span>${l.name}<span class="block small muted">${l.onoff ? "on/off only" : "dimmable"}${l.claim ? ` · claimed now by ${l.claim.names.join(", ")}` : ""}</span></span></label>`)
          : html`<p class="muted">No light is managed in this room.</p>`}
      </fieldset>
      <button type="button" class="btn btn-small sw-adv-toggle" aria-expanded=${this.advanced ? "true" : "false"} @click=${() => { this.advanced = !this.advanced; this.host.requestUpdate(); }}>
        ${icon(this.advanced ? "chevron-down" : "chevron-right", 16)}Advanced: timing, unavailable input, manual changes</button>
      ${this.advanced ? html`<div class="sw-form sw-advanced">
        <label class="field"><span>Confirm after (s)</span><input type="number" min="0" max=${lim.delay[1]} step="0.1" .value=${String(d.debounce ?? 0)}
          @change=${(e: Event) => this.change({ debounce: num(e) ?? 0 })} /><span class="small muted">0: at once; longer ignores contact bounce</span></label>
        <label class="field"><span>Release after (s)</span><input type="number" min="0" max=${lim.delay[1]} step="0.5" .value=${String(d.release_delay ?? 2)}
          @change=${(e: Event) => this.change({ release_delay: num(e) ?? 0 })} /><span class="small muted">after the input ends</span></label>
        <label class="field"><span>If the input is unavailable</span>
          <select @change=${(e: Event) => this.change({ unavailable: (e.target as HTMLSelectElement).value as Draft["unavailable"] })}>
            <option value="hold" ?selected=${d.unavailable === "hold"}>Hold, then release</option>
            <option value="release" ?selected=${d.unavailable === "release"}>Release at once</option>
            <option value="keep" ?selected=${d.unavailable === "keep"}>Keep until it reports again</option>
          </select></label>
        ${d.unavailable === "hold" ? html`<label class="field"><span>Hold for (s)</span><input type="number" min="0" max=${lim.grace[1]} step="10" .value=${String(d.grace ?? 60)}
          @change=${(e: Event) => this.change({ grace: num(e) ?? 0 })} /></label>` : nothing}
        <label class="field"><span>Change while active</span>
          <select @change=${(e: Event) => this.change({ release_manual: (e.target as HTMLSelectElement).value as Draft["release_manual"] })}>
            <option value="respect" ?selected=${d.release_manual === "respect"}>Kept after release</option>
            <option value="resume" ?selected=${d.release_manual === "resume"}>Ended at release (the current rule takes over)</option>
          </select></label>
        <label class="sw-check sw-wide"><input type="checkbox" .checked=${d.override_manual} @change=${(e: Event) => this.change({ override_manual: (e.target as HTMLInputElement).checked })} />
          <span>May override a manual change made before the input became active</span></label>
      </div>` : nothing}
      <p class="small muted">${d.enabled ? "Armed: saving keeps it armed; it acts when its input becomes active." : "Saving keeps it disabled: it sends nothing until you review and enable it."} Saving never sends a light command.</p>
      <div class="sw-actions">
        <button type="button" class="btn btn-primary" data-testid="priority-save" ?disabled=${this.host.busy} @click=${() => void this.save(areaId)}>${icon("save", 18)}Save</button>
        ${d.enabled ? nothing : html`<button type="button" class="btn" data-testid="priority-review" ?disabled=${this.host.busy || !ready} title=${ready ? "" : "Choose an input and at least one light first"}
          @click=${() => void this.review(areaId)}>${icon("eye", 18)}Review and enable…</button>`}
        <button type="button" class="btn" ?disabled=${this.host.busy} @click=${() => { this.draft = null; this.host.requestUpdate(); }}>Cancel</button>
      </div>
    </div>`;
  }

  private renderDialog(areaId: string, p: PriorityModel): TemplateResult | typeof nothing {
    if (!this.dialog) return nothing;
    const kind = this.dialog;
    const x = p.inputs.find((i) => i.id === this.selected);
    const err = this.dialogError ?? (this.host as unknown as { error: unknown }).error;
    let title = "";
    let body: unknown = nothing;
    let action: unknown = nothing;
    if (kind === "preview") {
      const pv = this.preview;
      const d = this.draft;
      title = `Enable ${d?.name ?? "Priority Input"}?`;
      body = pv ? html`<p>If <strong>${d?.name}</strong> became active now:</p>
        <ul class="sw-preview">${pv.lights.map((l) => l.missing
          ? html`<li>${icon("warning", 18)}<span>${l.entity_id}: not a light of this room</span></li>`
          : html`<li>${icon("lightbulb", 18)}<span><strong>${l.name}</strong>: ${l.forced.decision === "change"
              ? `switched on${l.forced.data.brightness_pct ? ` at ${l.forced.data.brightness_pct} %` : ""}` : l.forced.reason}
              <span class="block small muted">now: ${l.now.rule?.name ?? "—"} · ${l.now.reason}</span></span></li>`)}</ul>
        ${pv.notes.map((n) => html`<p class="notice notice-warning">${icon("info", 20)}<span>${n}</span></p>`)}
        <p class="small muted">Enabling sends nothing now. From then on, while ${d?.entity_id ?? "the input"} is active, its claim wins over every rule for these lights —
          also when the room is vacant, in daylight and while the room's automatic lighting is Observing.</p>` : nothing;
      action = pv ? html`<button type="button" class="btn btn-primary" data-testid="priority-enable" ?disabled=${this.host.busy} @click=${() => void this.arm(areaId)}>${icon("shield-check", 18)}Enable</button>` : nothing;
    } else if (kind === "delete") {
      title = `Delete ${x?.name ?? "this input"}?`;
      body = html`<p>If it is active now, its claim ends and its lights get their current automatic rule. Nothing else changes.</p>`;
      action = html`<button type="button" class="btn btn-danger" ?disabled=${this.host.busy || !x} @click=${() => x && void this.remove(areaId, x.id)}>${icon("delete", 18)}Delete</button>`;
    } else {
      const lock = kind === "lock";
      title = lock ? "Lock every Priority Input?" : "Unlock Priority Inputs?";
      body = html`<p>${lock ? "No Priority Input in any room claims a light or sends anything until you unlock them. Lights they hold now follow their rules again."
        : "Armed Priority Inputs in every room act again: an input that is active now claims its lights at once."}</p>`;
      action = html`<button type="button" class="btn ${lock ? "btn-danger" : "btn-primary"}" ?disabled=${this.host.busy} @click=${() => void this.lock(lock)}>${icon(lock ? "lock" : "lock-open", 18)}${lock ? "Lock all" : "Unlock"}</button>`;
    }
    return html`<dialog class="sheet sheet-full sw-dialog sw-priority-dialog" aria-labelledby="sw-pi-title" @close=${() => { this.dialog = null; this.host.requestUpdate(); }}>
      <div class="sheet-inner">
        <header class="sheet-head"><h2 id="sw-pi-title">${title}</h2><button type="button" class="icon-btn" aria-label="Close" @click=${() => this.close()}>${icon("close")}</button></header>
        <div class="sheet-body">${err ? problemNotice(err) : nothing}${body}</div>
        <footer class="sheet-foot"><button type="button" class="btn" @click=${() => this.close()}>Cancel</button>${action}</footer>
      </div></dialog>`;
  }
}
