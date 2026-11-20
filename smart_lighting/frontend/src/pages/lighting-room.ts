// Room lighting workspace (Phase A4): one room's automatic lighting. Overview (live lights, what each one gets
// and why, decisions, problems), Automatic (mode with a reviewed activation, switch-on and daylight, vacancy,
// manual changes, possible occupancy), Lights (discovery, exclusions, relays, fixture types, groups, competing
// automations), Brightness (ambient levels and matrices), Zone lighting (which lights respond to the room's zones;
// the zones themselves are Presence's since App 0.9.0), Supervisor
// and Log. Every value comes from the lighting runtime (``smart_occupancy/lighting/editor``); every change is one
// of its commands. Nothing here simulates a capability a light does not report.
import "../lighting/lighting-pages.css";
import "../lighting/lighting-brightness";
import "../lighting/lighting-zones";
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { chip, kv } from "../ui";
import { cmd, lighting } from "../lighting/api";
import { controlBanner, LightingElement, problemNotice } from "../lighting/components";
import { inspectButton, inspectLight } from "../lighting/light-inspector";
import {
  advancedToggle,
  AUTO_ON,
  capsList,
  card,
  DAYLIGHT,
  decisionLine,
  fmt,
  fmtSecs,
  inheritTag,
  loadEditor,
  modeChip,
  numberField,
  occChip,
  plural,
  problemItem,
  problemLevel,
  roomMode,
  segments,
  selectField,
  settingLine,
  sourceTag,
  stateText,
  SUP_LIMITS,
  SUP_MODE,
  SUP_RESULT,
  SUP_TONE,
  summary,
  toggleField,
  VACANCY,
  type EditorModel,
  type Obj,
  type Resolved,
  type RoomLight,
  type RoomModel,
} from "../lighting/lighting-model";

type Tab = "overview" | "automatic" | "lights" | "brightness" | "zones" | "supervisor" | "log";
const TABS: Array<{ key: Tab; title: string; icon: string }> = [
  { key: "overview", title: "Overview", icon: "view-dashboard" },
  { key: "automatic", title: "Automatic", icon: "lightbulb-auto" },
  { key: "lights", title: "Lights", icon: "lightbulb-group" },
  { key: "brightness", title: "Brightness", icon: "brightness" },
  { key: "zones", title: "Zone lighting", icon: "layers" },
  { key: "supervisor", title: "Supervisor", icon: "shield-check" },
  { key: "log", title: "Log", icon: "history" },
];

const CONFIRM_WORDS: Array<[string, string, string]> = [["handover", "Hand over", "swap"], ["turn_off", "Turn off", "power"]];

export class SlLightingRoom extends LightingElement {
  static properties = {
    ...LightingElement.properties, areaId: { type: String }, tab: { type: String },
    model: { state: true }, selected: { state: true }, openDecision: { state: true }, adv: { state: true },
    review: { state: true }, reviewAck: { state: true }, reviewOk: { state: true }, dialog: { state: true },
    sup: { state: true }, supSel: { state: true }, relayAll: { state: true }, fixtureDraft: { state: true },
  };
  declare areaId: string;
  declare tab: string;
  declare model: EditorModel | null;
  declare selected: string | null;
  declare openDecision: string | null;
  declare adv: Set<string>;
  declare review: Obj | null;
  declare reviewAck: Set<string>;
  declare reviewOk: boolean;
  declare dialog: "activate" | "deactivate" | null;
  declare sup: Obj | null;
  declare supSel: string | null;
  declare relayAll: boolean;
  declare fixtureDraft: Obj | null;
  private loadedArea: string | null = null;

  constructor() {
    super();
    this.areaId = "";
    this.tab = "overview";
    this.model = null;
    this.selected = null;
    this.openDecision = null;
    this.adv = new Set();
    this.review = null;
    this.reviewAck = new Set();
    this.reviewOk = false;
    this.dialog = null;
    this.sup = null;
    this.supSel = null;
    this.relayAll = false;
    this.fixtureDraft = null;
  }

  protected willUpdate(changed?: Map<PropertyKey, unknown>): void {
    super.willUpdate();
    if (!changed) return;
    if (changed.has("areaId") && this.loadedArea !== null && this.loadedArea !== this.areaId) {
      this.model = null;
      this.selected = null;
      this.sup = null;
      void this.reload();
    }
    if (changed.has("tab") && this.tab === "supervisor" && this.model) void this.loadSupervisor();
  }

  async reload(): Promise<void> {
    if (!this.areaId) return;
    const area = this.areaId;
    try {
      const model = await loadEditor(area);
      if (area !== this.areaId) return;
      this.model = model;
      this.loadedArea = area;
      if (this.tab === "supervisor") void this.loadSupervisor();
    } catch (err) {
      this.error = err;
      this.loadedArea = area;
    }
  }

  private get room(): RoomModel | null {
    return this.model?.room ?? null;
  }

  private go(tab: Tab): void {
    window.location.hash = `/lighting/${encodeURIComponent(this.areaId)}/${tab}`;
  }

  private send(name: string, params: Obj, done = "Saved"): Promise<boolean> {
    return this.run(() => lighting(cmd(name), { area_id: this.areaId, ...params }), done);
  }

  private toggleAdv(key: string): void {
    const n = new Set(this.adv);
    if (n.has(key)) n.delete(key);
    else n.add(key);
    this.adv = n;
  }

  private nameOf(entityId: string): string {
    return this.room?.lights.find((l) => l.entity_id === entityId)?.name ?? entityId;
  }

  // ---------------------------------------------------------------- header
  private renderHead(r: RoomModel): TemplateResult {
    const d = r.diagnostics ?? {};
    const rooms = this.model?.rooms ?? [];
    return html`<header class="editor-head">
      <a class="btn back-btn" href="#/lighting" data-testid="back-to-lighting">${icon("arrow-left", 20)}<span>Lighting</span></a>
      <div class="editor-title">
        <h2 tabindex="-1">${r.name}</h2>
        <div class="chips">${modeChip(!!r.settings?.control_enabled, d.control_mode)} ${occChip(r.occupancy?.state)}
          ${chip("neutral", d.manual_mode ? "Mood: Manual (paused)" : `Mood: ${r.now_mood ?? "none"}${d.follows_global ? "" : " (room)"}`)}
          ${chip(r.now_profile === "vacant" ? "info" : "neutral", r.now_profile === "vacant" ? "Vacant profile" : "Occupied profile")}</div>
      </div>
      <div class="editor-actions lp-head-actions">
        ${rooms.length > 1 ? html`<label class="visually-hidden" for="lp-room-switch">Room</label>
          <select id="lp-room-switch" data-testid="lighting-room-switch" @change=${(e: Event) => (window.location.hash = `/lighting/${encodeURIComponent((e.target as HTMLSelectElement).value)}/${this.tab}`)}>
            ${rooms.map((x) => html`<option value=${x.area_id} ?selected=${x.area_id === r.area_id}>${x.name}</option>`)}</select>` : nothing}
        <a class="btn" href=${`#/rooms/${encodeURIComponent(r.area_id)}`} data-testid="open-rooms-editor">${icon("floor-plan", 20)}<span>Rooms editor</span></a>
        <button type="button" class="icon-btn" aria-label="Refresh" title="Refresh" ?disabled=${this.busy} @click=${() => void this.refreshAll()}>${icon("refresh")}</button>
      </div>
    </header>`;
  }

  private renderTabs(r: RoomModel): TemplateResult {
    const warn = (r.problems ?? []).filter((p) => problemLevel(p) !== "info").length + (r.group_issues ?? []).filter((g) => !g.dismissed).length;
    const supWarn = r.supervisor?.discrepancies ?? 0;
    return html`<nav class="tabs lp-tabs" role="tablist" aria-label="Room lighting">${TABS.map((t) => html`<a role="tab" href=${`#/lighting/${encodeURIComponent(r.area_id)}/${t.key}`}
        class=${this.tab === t.key ? "active" : ""} aria-selected=${this.tab === t.key ? "true" : "false"} data-tab=${t.key}>${icon(t.icon, 18)}<span>${t.title}</span>
        ${t.key === "overview" && warn ? html`<span class="tab-badge">${warn}</span>` : nothing}
        ${t.key === "supervisor" && supWarn ? html`<span class="tab-badge">${supWarn}</span>` : nothing}</a>`)}</nav>`;
  }

  // -------------------------------------------------------------- overview
  private lightFlags(l: RoomLight, r: RoomModel): TemplateResult[] {
    const out: TemplateResult[] = [];
    const offline = (r.diagnostics?.offline_lights ?? []).includes(l.entity_id);
    if (l.missing) out.push(chip("neutral", "not managed"));
    else if (l.excluded) out.push(chip("neutral", "excluded"));
    if (l.covered_by) out.push(chip("neutral", `via ${this.nameOf(l.covered_by)}`));
    if (offline) out.push(chip("warning", "offline"));
    if (l.override) out.push(chip("info", "manual override"));
    if ((l.conflicts ?? []).some((c) => c.enabled && !c.acknowledged)) out.push(chip("warning", "automation conflict"));
    return out;
  }

  private typeLabel(l: RoomLight): string {
    if (l.is_group) return l.opaque_group ? "Group" : `Group of ${l.members?.length ?? 0}`;
    return l.type === "relay" ? "Relay" : "Light";
  }

  private lightsTable(r: RoomModel, opts: { testid: string; columns: "now" | "config" }): TemplateResult {
    const lights = r.lights;
    if (!lights.length) return html`<div class="empty-state"><p class="muted">No managed lights in ${r.name}.</p>
      <a class="btn" href=${`#/rooms/${encodeURIComponent(r.area_id)}`}>${icon("floor-plan", 20)}Assign lights in the Rooms editor</a></div>`;
    const fixtures: Record<string, Obj> = Object.fromEntries((r.priority?.lights ?? []).map((x: Obj) => [x.entity_id, x]));
    return html`<div class="table-wrap"><table class="plain-table lp-table lp-select-table" data-testid=${opts.testid}>
      <thead><tr><th scope="col">Light</th><th scope="col">Type</th><th scope="col">State</th>
        ${opts.columns === "now" ? html`<th scope="col">Requested now</th><th scope="col">Mood profile</th>` : html`<th scope="col">Can do</th><th scope="col">Fixture type</th>`}
        <th scope="col">Notes</th></tr></thead>
      <tbody>${lights.map((l) => {
        const src = l.now?.sources?.action;
        const fx = fixtures[l.entity_id]?.fixture;
        const rule = (r.zones?.rules ?? []).find((x: Obj) => x.entity_id === l.entity_id);
        return html`<tr class=${`${this.selected === l.entity_id ? "selected" : ""} ${l.excluded || l.missing ? "lp-dim" : ""}`} tabindex="0" data-entity=${l.entity_id}
            @click=${() => (this.selected = this.selected === l.entity_id ? null : l.entity_id)}
            @keydown=${(e: KeyboardEvent) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); this.selected = l.entity_id; } }}>
          <td><span class="cell-main">${icon(l.type === "relay" ? "electric-switch" : l.is_group ? "lightbulb-group" : "lightbulb", 20)}
            <span><span class="cell-name">${l.name}</span><span class="block small muted mono">${l.entity_id}</span></span>${l.missing ? nothing : inspectButton(l.entity_id, r.area_id, l.name)}</span></td>
          <td>${this.typeLabel(l)}</td>
          <td><span class="lp-state lp-state-${l.state ?? "unknown"}">${l.state ?? "—"}</span></td>
          ${opts.columns === "now"
            ? html`<td>${l.excluded || l.missing ? html`<span class="muted">not controlled</span>` : rule ? html`<strong>${stateText(rule.requested)}</strong><span class="block small muted">${rule.rule ?? ""}</span>` : "—"}</td>
                <td>${l.excluded || l.missing || !l.now ? "—" : html`${settingLine(l.now)} ${sourceTag(src, r.now_mood)}`}</td>`
            : html`<td>${l.missing ? "—" : capsList(l.capabilities, l.type)}</td><td>${fx ? fx.text : html`<span class="muted">Not classified</span>`}</td>`}
          <td><span class="chips">${this.lightFlags(l, r)}</span></td>
        </tr>`;
      })}</tbody></table></div>`;
  }

  private statusStrip(r: RoomModel): TemplateResult {
    const d = r.diagnostics ?? {};
    const mode = roomMode(!!r.settings?.control_enabled, d.control_mode);
    const dl = r.daylight?.status;
    const amb = r.brightness?.ambient?.status;
    const on = r.lights.filter((l) => !l.excluded && !l.missing && l.state === "on").length;
    const managed = r.lights.filter((l) => !l.excluded && !l.missing).length;
    const tile = (label: string, value: unknown, sub: unknown) => html`<div class="lp-tile"><span class="lp-tile-label">${label}</span><span class="lp-tile-value">${value}</span><span class="small muted">${sub}</span></div>`;
    return html`<section class="card wide lp-strip" data-testid="lighting-room-status">
      ${tile("Automatic lighting", chip(mode.tone, mode.label), d.control_mode_reason)}
      ${tile("Occupancy", occChip(r.occupancy?.state), r.occupancy?.reason ?? "")}
      ${tile("Mood", d.manual_mode ? "Manual" : r.now_mood ?? "No mood", d.follows_global ? "follows the global mood" : "this room's mood")}
      ${tile("Lights on", `${on} / ${managed}`, d.last_applied_profile ? `last applied ${d.last_applied_profile} · ${fmt(d.last_applied_at)}` : "nothing applied yet")}
      ${tile("Daylight", dl ? chip(DAYLIGHT[dl.decision]?.[1] ?? "neutral", DAYLIGHT[dl.decision]?.[0] ?? dl.decision) : "—", dl?.reason ?? "")}
      ${tile("Ambient level", amb?.name ?? "—", amb ? `${amb.lux_text ?? ""}${r.brightness?.ambient?.enabled ? "" : " · control off"}` : "")}
    </section>`;
  }

  private renderOverview(r: RoomModel): TemplateResult {
    const dec = r.decisions?.entries ?? [];
    const problems = r.problems ?? [];
    const groups = (r.group_issues ?? []).filter((g) => !g.dismissed);
    const blocking: string[] = r.diagnostics?.blocking_automations ?? [];
    return html`<div class="lp-split ${this.selected ? "lp-has-inspector" : ""}">
      <div class="lp-main">
        ${this.statusStrip(r)}
        ${card("Lights now", "lightbulb-group", html`<p class="small muted">What each light is asked for now and by which rule (a zone, a brightness matrix or the mood), next to its ${r.now_mood ?? "—"} ${r.now_profile === "vacant" ? "Vacant" : "Occupied"} profile and where that comes from (global, room or this light). Select a light for the details.</p>
          ${this.lightsTable(r, { testid: "lighting-lights-now", columns: "now" })}`, { wide: true })}
        <div class="lp-two">
          ${card("Latest decisions", "history", html`${dec.length ? html`<ul class="lp-decisions">${dec.slice(0, 5).map((d) => decisionLine(d, {
              open: this.openDecision === d.id, onToggle: () => (this.openDecision = this.openDecision === d.id ? null : d.id) }))}</ul>`
            : html`<p class="muted">No decision recorded yet.</p>`}
            <a class="text-link" href=${`#/lighting/${encodeURIComponent(r.area_id)}/log`}>Full log ${icon("chevron-right", 18)}</a>`, { testid: "lighting-room-decisions" })}
          ${card("Warnings", "warning", html`
            ${groups.map((g) => html`<div class="notice notice-warning lp-issue">${icon("lightbulb-group", 20)}<span><strong>${this.nameOf(g.group)}</strong> and ${g.members.length} of ${g.members_known ? "its" : "possibly its"} lights are both managed. How should they be controlled?
              ${this.canEdit ? html`<span class="row-actions lp-row">${this.groupButtons(g.group)}</span>` : nothing}</span></div>`)}
            ${blocking.length ? html`<div class="notice notice-warning lp-issue">${icon("robot", 20)}<span><strong>${plural(blocking.length, "automation")} also control${blocking.length === 1 ? "s" : ""} lights here</strong>${r.settings?.control_enabled ? ": those lights are left alone" : ""}.
              <button type="button" class="lp-link" @click=${() => this.go("lights")}>Review</button></span></div>` : nothing}
            ${problems.length ? html`<ul class="lp-problems">${problems.map(problemItem)}</ul>` : nothing}
            ${!groups.length && !blocking.length && !problems.length ? html`<p class="tone-ok">${icon("ok", 18)} No warnings.</p>` : nothing}`, { testid: "lighting-room-problems" })}
        </div>
      </div>
      ${this.selected ? this.lightInspector(r, this.selected) : nothing}
    </div>`;
  }

  private groupButtons(group: string): TemplateResult {
    const pick = (choice: string, done: string) => () => void this.send("room/group_choice", { group, choice }, done);
    return html`<button type="button" class="btn btn-small" ?disabled=${this.busy} @click=${pick("group", "Controlled as a group")}>As a group</button>
      <button type="button" class="btn btn-small" ?disabled=${this.busy} @click=${pick("individual", "Controlled one by one")}>Individually</button>
      <button type="button" class="btn btn-small" ?disabled=${this.busy} @click=${pick("dismiss", "Warning ignored")}>Ignore</button>`;
  }

  private valueRows(s: Resolved | null | undefined, mood: string | null): TemplateResult {
    if (!s) return html`<p class="muted">No mood: nothing is applied.</p>`;
    const src = s.sources ?? {};
    const rows: Array<[string, unknown, unknown]> = [["On / off", s.action === "on" ? "On" : s.action === "off" ? "Off" : summary(s), sourceTag(src.action, mood)]];
    if (s.action === "on") {
      rows.push(["Brightness", s.brightness_pct !== null && s.brightness_pct !== undefined ? `${s.brightness_pct} %${s.brightness_mode === "global" || s.brightness_mode === "offset" ? " (global value)" : ""}` : "unchanged",
        sourceTag(src.brightness_pct ?? src.brightness_mode, mood)]);
      const colour = s.rgb_color ? `RGB ${s.rgb_color.join(", ")}` : s.hs_color ? `HS ${s.hs_color.join(", ")}` : s.color_temp_kelvin ? `${s.color_temp_kelvin} K` : "unchanged";
      rows.push(["Colour", colour, sourceTag(src.rgb_color ?? src.hs_color ?? src.color_temp_kelvin ?? src.color_temp_mode, mood)]);
      if (s.effect) rows.push(["Effect", s.effect, sourceTag(src.effect, mood)]);
      if (s.transition) rows.push(["Transition", `${s.transition} s`, sourceTag(src.transition, mood)]);
    }
    return html`<table class="plain-table lp-values"><tbody>${rows.map(([k, v, t]) => html`<tr><th scope="row">${k}</th><td>${v}</td><td>${t}</td></tr>`)}</tbody></table>`;
  }

  private lightInspector(r: RoomModel, entityId: string): TemplateResult {
    const l = r.lights.find((x) => x.entity_id === entityId);
    if (!l) return nothing as unknown as TemplateResult;
    const rule = (r.zones?.rules ?? []).find((x: Obj) => x.entity_id === entityId);
    const sup = (r.supervisor?.targets ?? []).find((x: Obj) => x.entity_id === entityId);
    const ov = l.override;
    const conflicts = l.conflicts ?? [];
    return html`<aside class="inspector lp-inspector" aria-label="Light details" data-testid="lighting-light-inspector">
      <header class="inspector-head"><span class="inspector-icon">${icon(l.type === "relay" ? "electric-switch" : "lightbulb", 22)}</span>
        <div><h3>${l.name}</h3><span class="muted small mono">${l.entity_id}</span></div>
        <button type="button" class="icon-btn" aria-label="Close" @click=${() => (this.selected = null)}>${icon("close")}</button></header>
      <section class="inspector-section"><h4>Now</h4>
        ${kv([["State", l.state ?? "—"], ["Type", this.typeLabel(l)], ["Light type", l.category_name ?? (l.labels?.length ? l.labels.join(", ") : "no category label")],
          l.excluded ? ["Not managed", l.excluded] : null, l.covered_by ? ["Controlled through", this.nameOf(l.covered_by)] : null])}
        <div class="lp-caps">${capsList(l.capabilities, l.type)}</div>
        ${l.capabilities?.known === false ? html`<p class="small tone-warning">The light reports no capabilities yet: only what it reports is used.</p>` : nothing}
      </section>
      ${l.excluded || l.missing ? nothing : html`<section class="inspector-section"><h4>Requested now</h4>
        ${rule ? html`<p><strong>${stateText(rule.requested)}</strong> · ${rule.rule ?? "—"}${rule.superseded?.length ? html` <span class="muted small">over ${rule.superseded.join(", ")}</span>` : nothing}
          <span class="block small muted">${rule.reason}</span></p>` : html`<p class="muted small">No rule requests anything now.</p>`}
        ${sup ? html`<p class="small">Supervisor: expected ${stateText(sup.desired)}, actual ${stateText(sup.actual)} ${chip(SUP_RESULT[sup.result]?.[1] ?? "neutral", SUP_RESULT[sup.result]?.[0] ?? sup.result)}</p>` : nothing}
      </section>
      <section class="inspector-section"><h4>Mood profile · ${r.now_mood ?? "no mood"} (${r.now_profile === "vacant" ? "Vacant" : "Occupied"})</h4>
        ${this.valueRows(l.now, r.now_mood)}
        ${l.own || (this.model?.selected_mood && this.model.selected_mood !== r.now_mood) ? html`<p class="small muted">Profile for ${this.model?.selected_mood}: ${summary(l.effective)} (without this light's own entry: ${summary(l.inherited)}). Profiles are edited on the Moods page.</p>` : nothing}
      </section>`}
      ${ov ? html`<section class="inspector-section lp-override" data-testid="lighting-override"><h4>Manual override</h4>
        <p>${ov.switch ? `Wall switch ${ov.switch}: ${ov.reason ?? ov.action ?? ""}` : `Changed by ${ov.source ?? "a person"}`} · respected ${ov.until ? `until ${fmt(ov.until)}` : "until the room is vacant"}.</p>
        ${this.canEdit ? html`<button type="button" class="btn btn-primary" ?disabled=${this.busy} data-testid="lighting-resume"
          @click=${() => void this.send("override/clear", { entity_id: l.entity_id }, "Automatic lighting resumed")}>${icon("lightbulb-auto", 20)}Resume automatic lighting</button>` : nothing}</section>` : nothing}
      ${conflicts.length ? html`<section class="inspector-section"><h4>Automations that also control it</h4>
        <ul class="plain-list">${conflicts.map((c) => html`<li><label class="lp-check"><input type="checkbox" .checked=${c.acknowledged} ?disabled=${!this.canEdit || this.busy}
            @change=${(e: Event) => void this.send("room/acknowledge", { automation: c.automation, acknowledged: (e.target as HTMLInputElement).checked }, "Saved")} />
          <span><strong>${c.name ?? c.automation}</strong> <span class="small muted">${c.enabled ? "enabled" : "disabled"}${c.via ? ` · ${c.via}` : ""}</span>
            <span class="block small ${c.enabled && !c.acknowledged ? "tone-warning" : "muted"}">${c.acknowledged ? "Allowed: both control this light." : c.enabled ? "Not allowed: automatic lighting leaves this light alone. Tick to allow both." : "Disabled: no conflict."}</span></span></label></li>`)}</ul></section>` : nothing}
      <section class="inspector-section"><button type="button" class="btn btn-small btn-primary" data-testid="lighting-open-inspector" @click=${() => inspectLight(l.entity_id, r.area_id)}>${icon("information-outline", 18)}What controls it? · Identify</button>
        <button type="button" class="btn btn-small" @click=${() => this.go("lights")}>${icon("tune", 18)}Classify or exclude (Lights)</button>
        <button type="button" class="btn btn-small" @click=${() => this.go("brightness")}>${icon("brightness", 18)}Brightness matrix</button></section>
    </aside>`;
  }

  // ------------------------------------------------------------- automatic
  private async openActivation(): Promise<void> {
    this.review = null;
    this.reviewOk = false;
    this.dialog = "activate";
    this.error = null;
    await this.updateComplete;
    this.querySelector<HTMLDialogElement>("dialog.lp-dialog")?.showModal();
    try {
      this.review = await lighting<Obj>(cmd("room/activation"), { area_id: this.areaId });
      this.reviewAck = new Set((this.review.automations ?? []).filter((a: Obj) => a.acknowledged).map((a: Obj) => a.automation));
    } catch (err) {
      this.error = err;
    }
  }

  private async openDeactivate(): Promise<void> {
    this.dialog = "deactivate";
    this.error = null;
    await this.updateComplete;
    this.querySelector<HTMLDialogElement>("dialog.lp-dialog")?.showModal();
  }

  private closeDialog(): void {
    this.querySelector<HTMLDialogElement>("dialog.lp-dialog")?.close();
    this.dialog = null;
  }

  private async confirmDialog(): Promise<void> {
    const ok = this.dialog === "activate"
      ? await this.send("room/activate", { confirm: true, acknowledge: [...this.reviewAck] }, "Automatic: the room's lights are controlled")
      : await this.send("room/deactivate", {}, "Observing: nothing is controlled in this room");
    if (ok) this.closeDialog();
  }

  private dialogTemplate(r: RoomModel): TemplateResult | typeof nothing {
    if (!this.dialog) return nothing;
    const activate = this.dialog === "activate";
    const rv = this.review;
    const items: Obj[] = rv?.plan?.items ?? [];
    const changes = items.filter((i) => i.decision === "change");
    return html`<dialog class="sheet sheet-full review-dialog lp-dialog" aria-labelledby="lp-dialog-title" data-testid="lighting-activation-dialog" @close=${() => (this.dialog = null)}>
      <div class="sheet-inner">
        <header class="sheet-head"><h2 id="lp-dialog-title">${activate ? `Switch ${r.name} to Automatic` : `Return ${r.name} to Observing`}</h2>
          <button type="button" class="icon-btn" aria-label="Close" @click=${() => this.closeDialog()}>${icon("close")}</button></header>
        <div class="sheet-body lp-sheet-body">
          ${this.error ? problemNotice(this.error) : nothing}
          ${!activate ? html`<p>Automatic lighting stops controlling the lights of ${r.name}. Lights are left exactly as they are; decisions are still calculated and logged (Observing). Wall switches set to Active keep working.</p>`
          : !rv ? html`<div class="loading" role="status"><span class="spinner"></span><span>Checking what Automatic would control…</span></div>`
          : html`
            <p>Automatic lighting will control ${plural(rv.managed.length, "light")} and apply the current mood${rv.plan?.mood ? ` (${rv.plan.mood})` : ""} at once. Existing automations are not changed.</p>
            <h3 class="lp-h">What changes now</h3>
            <div class="table-wrap"><table class="plain-table lp-table"><thead><tr><th scope="col">Light</th><th scope="col">Now</th><th scope="col">Will be</th><th scope="col">Why</th></tr></thead>
              <tbody>${items.map((i) => html`<tr class=${i.decision === "change" ? "lp-change" : ""}><td>${i.name ?? i.entity_id}</td><td>${stateText(i.current)}</td>
                <td>${i.decision === "change" ? html`<strong>${stateText(i.requested)}</strong>` : i.decision === "unchanged" ? "unchanged" : html`<span class="muted">${i.decision}</span>`}</td>
                <td class="small muted">${i.rule?.name ? html`${i.rule.name} · ` : nothing}${i.reason}</td></tr>`)}</tbody></table></div>
            <p class="small muted">${changes.length ? `${plural(changes.length, "light")} would be switched or adjusted.` : "No light needs a command right now."}
              ${rv.plan?.daylight?.text ? ` Daylight: ${rv.plan.daylight.text}.` : ""}${rv.plan?.note ? ` ${rv.plan.note}` : ""}</p>
            ${rv.excluded?.length ? html`<p class="small">Not managed: ${rv.excluded.map((x: Obj) => `${x.name ?? x.entity_id} (${x.reason})`).join(", ")}</p>` : nothing}
            <h3 class="lp-h">Automations that also control these lights</h3>
            ${rv.automations?.length ? html`<ul class="plain-list">${rv.automations.map((a: Obj) => html`<li><label class="lp-check">
                <input type="checkbox" .checked=${this.reviewAck.has(a.automation)} @change=${(e: Event) => {
                  const n = new Set(this.reviewAck); if ((e.target as HTMLInputElement).checked) n.add(a.automation); else n.delete(a.automation); this.reviewAck = n; }} />
                <span><strong>${a.name ?? a.automation}</strong> <span class="small muted">${a.enabled ? "enabled" : "disabled"} · ${a.lights.map((x: Obj) => this.nameOf(x.entity_id)).join(", ")}</span>
                  <span class="block small muted">Ticked: both may control these lights. Not ticked: automatic lighting leaves them alone.</span></span></label></li>`)}</ul>`
              : html`<p class="muted">None found.</p>`}
            <label class="confirm-row"><input type="checkbox" data-testid="lighting-activate-understood" .checked=${this.reviewOk} @change=${(e: Event) => (this.reviewOk = (e.target as HTMLInputElement).checked)} />
              <span>I have reviewed what Automatic will control in ${r.name}.</span></label>`}
        </div>
        <footer class="sheet-foot">
          <button type="button" class="btn" @click=${() => this.closeDialog()}>Cancel</button>
          <button type="button" class=${activate ? "btn btn-primary" : "btn btn-danger"} data-testid="lighting-activate-confirm"
            ?disabled=${this.busy || (activate && (!rv || !this.reviewOk))} @click=${() => void this.confirmDialog()}>
            ${icon(activate ? "lightbulb-auto" : "eye", 20)}${this.busy ? "Working…" : activate ? "Switch to Automatic" : "Return to Observing"}</button>
        </footer>
      </div></dialog>`;
  }

  private modeCard(r: RoomModel): TemplateResult {
    const d = r.diagnostics ?? {};
    const on = !!r.settings?.control_enabled;
    const mode = roomMode(on, d.control_mode);
    const n = d.managed_lights ?? 0;
    return card("Automatic lighting", "lightbulb-auto", html`
      ${segments<"observe" | "automatic">(on ? "automatic" : "observe", [["observe", "Observing", "eye"], ["automatic", "Automatic", "lightbulb-auto"]],
        (v) => void (v === "automatic" ? this.openActivation() : this.openDeactivate()), { disabled: !this.canEdit || this.busy, label: "Automatic lighting mode", testid: "lighting-mode" })}
      <p class="${mode.tone === "warning" ? "tone-warning" : "muted"} small">${on
        ? `${d.control_mode_reason}. Controls ${plural(n, "light")}: follows the mood and switches on as set below.${d.activated_at ? ` Automatic since ${fmt(d.activated_at)}.` : ""}`
        : `Observing: decisions are calculated and logged; no light is controlled.${n ? ` Automatic would control ${plural(n, "light")} (reviewed first).` : ""}`}</p>
      <p class="small muted">Wall switches set to Active work in both modes.</p>`, { testid: "lighting-mode-card" });
  }

  private daylightCard(r: RoomModel): TemplateResult {
    const dl = r.daylight;
    if (!dl) return card("Switch-on and daylight", "brightness", html`<p class="muted">Daylight information is not available for this room.</p>`);
    const c = dl.config;
    const st = dl.status;
    const dis = !this.canEdit || this.busy;
    const set = (change: Obj, done = "Saved") => void this.send("room/daylight", change, done);
    const cands: Obj[] = dl.candidates ?? [];
    const sensors: Obj[] = c.sensors ?? [];
    const preferred = sensors[0]?.entity_id ?? "";
    const open = this.adv.has("daylight");
    const f = st.factor ?? {};
    return card("Switch-on and daylight", "brightness", html`<div class="lp-two">
      <div class="lp-form lp-form-1">
        ${selectField({ label: "Turn lights on", value: c.auto_on, options: AUTO_ON as Array<[string, string]>, disabled: dis, onChange: (v) => set({ auto_on: v }) })}
        ${numberField({ label: "Darkness threshold", value: c.on_below, unit: "lx", min: 1, max: 5000, disabled: dis, help: "Lights turn on below this level.", onChange: (v) => v !== null && set({ on_below: v }) })}
        ${selectField({ label: "Indoor light sensor", value: preferred, disabled: dis,
          options: [["", "None — outdoor light and sun only"], ...(preferred && !cands.some((x) => x.entity_id === preferred) ? [[preferred, `${preferred} (not found)`] as [string, string]] : []),
            ...cands.filter((x) => !x.outdoor || x.in_room).map((x) => [x.entity_id, `${x.name}${x.value !== null && x.value !== undefined ? ` · ${x.value} lx` : ""}${x.in_room ? "" : x.area ? ` (${x.area})` : ""}`] as [string, string])],
          onChange: (v) => set({ sensors: v ? [v, ...sensors.map((s) => s.entity_id).filter((e) => e !== v)].map((e) => ({ entity_id: e }))
            : sensors.slice(1).map((s) => ({ entity_id: s.entity_id })) }) })}
      </div>
      <div class="lp-now-box">
        <span>${chip(DAYLIGHT[st.decision]?.[1] ?? "neutral", DAYLIGHT[st.decision]?.[0] ?? st.decision)} <strong>${st.reason}</strong></span>
        <span class="small muted">${st.natural_light}${st.estimated ? " (an estimate, not a measured value)" : ""}</span>
        ${st.contaminated && st.contamination ? html`<span class="small muted">Indoor readings are not used as daylight now · ${st.contamination}</span>` : nothing}
        <span class="small muted">Outdoor light: ${dl.global?.outdoor_entity ? `${dl.global.outdoor_name ?? dl.global.outdoor_entity}${st.outdoor?.value !== null && st.outdoor?.value !== undefined ? ` · ${st.outdoor.value} lx` : ""}` : "no outdoor sensor (sun position)"} — global setting
          (<a href="#/lighting/settings/daylight">Lighting settings</a>).</span>
        ${dl.activation ? html`<span class="small muted">Last switch-on decision: ${dl.activation.text} · ${fmt(dl.activation.at)}</span>` : nothing}
      </div></div>
      ${advancedToggle(open, () => this.toggleAdv("daylight"), "Advanced daylight settings")}
      ${open ? html`<div class="lp-form">
        ${selectField({ label: "Switch on at", value: c.on_first_motion ? "motion" : "confirmed", disabled: dis,
          options: [["motion", "First motion (prompt)"], ["confirmed", "Confirmed occupancy (fewer false triggers)"]], onChange: (v) => set({ on_first_motion: v === "motion" }) })}
        ${selectField({ label: "When daylight is uncertain", value: c.uncertain, disabled: dis, options: [["turn_on", "Turn lights on"], ["stay_off", "Leave lights off"]], onChange: (v) => set({ uncertain: v }) })}
        ${numberField({ label: "Light not needed above", value: c.off_above, unit: "lx", min: 2, max: 10000, disabled: dis, help: `Between ${c.on_below} lx and this the decision stays as it was.`, onChange: (v) => v !== null && set({ off_above: v }) })}
        ${numberField({ label: "Stable for", value: c.stable_seconds, unit: "s", min: 0, max: 1800, disabled: dis, help: "Before a change in daylight counts.", onChange: (v) => v !== null && set({ stable_seconds: v }) })}
        ${numberField({ label: "Settle after lights off", value: c.settle_seconds, unit: "s", min: 0, max: 900, disabled: dis, onChange: (v) => v !== null && set({ settle_seconds: v }) })}
        ${numberField({ label: "Last reading valid for", value: c.sample_max_age, unit: "min", min: 1, max: 240, disabled: dis, help: "While the lights are on.", onChange: (v) => v !== null && set({ sample_max_age: v }) })}
        ${numberField({ label: "Daylight factor", value: c.factor, step: 0.001, min: 0.0001, max: 1, placeholder: f.learned ?? "learned", disabled: dis,
          help: `Indoor ÷ outdoor light. Learned: ${f.learned !== null && f.learned !== undefined ? `${f.learned} (${f.samples} samples)` : `not yet (${f.samples ?? 0} of 3 samples)`}. Empty: learned value.`, onChange: (v) => set({ factor: v }) })}
      </div>
      ${toggleField({ label: "Every light in this area can reach the sensors", checked: !!c.area_lights, disabled: dis, help: `${plural((dl.influencing ?? []).length, "light")} counted; while one is on, indoor readings are not used as daylight.`, onChange: (v) => set({ area_lights: v }) })}
      <h4 class="lp-h">Light sensors in priority order</h4>
      ${sensors.length ? html`<ol class="lp-sensors">${sensors.map((s, i) => {
        const live = (st.sensors ?? [])[i] ?? {};
        return html`<li><span><strong>${live.name ?? s.entity_id}</strong> ${chip(i ? "neutral" : "info", i ? `fallback ${i}` : "preferred")}
            <span class="block small ${live.status === "ok" ? "muted" : "tone-warning"}">${live.status === "ok" ? `${live.value} lx · reported ${fmt(live.reported_at)}` : live.detail ?? live.status ?? ""}</span></span>
          ${dis ? nothing : html`<span class="row-actions">${i ? html`<button type="button" class="btn btn-small" @click=${() => set({ sensors: [s, ...sensors.filter((x) => x !== s)].map((x) => ({ entity_id: x.entity_id })) })}>Prefer</button>` : nothing}
            <button type="button" class="btn btn-small" @click=${() => set({ sensors: sensors.filter((x) => x !== s).map((x) => ({ entity_id: x.entity_id })) })}>${icon("delete", 16)}Remove</button></span>`}</li>`;
      })}</ol>` : html`<p class="muted small">No indoor light sensor.</p>`}
      ${dis ? nothing : html`<label class="lp-field"><span class="lp-label">Add a fallback sensor</span><select @change=${(e: Event) => { const v = (e.target as HTMLSelectElement).value; if (v) set({ sensors: [...sensors.map((x) => ({ entity_id: x.entity_id })), { entity_id: v }] }); }}>
          <option value="">Choose a sensor…</option>${cands.filter((x) => !sensors.some((s) => s.entity_id === x.entity_id)).map((x) => html`<option value=${x.entity_id}>${x.name}${x.value !== null && x.value !== undefined ? ` · ${x.value} lx` : ""}${x.area ? ` (${x.area})` : ""}</option>`)}</select></label>
        <button type="button" class="btn btn-small" @click=${() => set({ reset_calibration: true }, "Calibration reset")}>${icon("undo", 16)}Reset calibration</button>`}` : nothing}`, { testid: "lighting-daylight", wide: true });
  }

  private vacancyCard(r: RoomModel): TemplateResult {
    const v = r.vacancy ?? { action: "turn_off" };
    const timing = (r as Obj).presence?.timing?.find((t: Obj) => t.key === "inactivity_timeout");
    const explain: Record<string, string> = {
      turn_off: "Lights that are on are switched off when the vacancy timeout ends. Ambient lights keep following the mood.",
      vacant_mood: `The room's Vacant profile of the current mood${r.vacant?.mood ? ` (now ${r.vacant.mood})` : ""} is applied when the vacancy timeout ends, and again when the mood or the daylight changes while the room stays empty.`,
      leave: "Lights are left as they are when the room becomes vacant.",
    };
    return card("When the room becomes vacant", "door-open", html`
      ${segments(v.action, VACANCY.map(([a, l, i]) => [a, l, i] as [string, string, string]), (a) => void this.send("room/set", { vacancy_action: a }, "Saved"),
        { disabled: !this.canEdit || this.busy, label: "When the room becomes vacant", testid: "lighting-vacancy" })}
      <p class="small muted">${explain[v.action] ?? ""}${timing ? ` Vacancy timeout ${fmtSecs(timing.value)} (${timing.overridden ? "this room" : "global default"}; Presence).` : ""}</p>
      ${v.action === "vacant_mood" ? html`<p class="small">What each light does while the room is empty is the Vacant profile: <a href="#/moods">edit it on the Moods page</a>.</p>` : nothing}
      ${v.last ? html`<p class="small muted">Last vacancy: ${v.last.text} · ${fmt(v.last.at)}</p>` : nothing}`, { testid: "lighting-vacancy-card" });
  }

  private manualCard(r: RoomModel): TemplateResult {
    const s = r.settings ?? {};
    const glob = this.model?.settings?.manual_override_minutes;
    const dis = !this.canEdit || this.busy;
    const moods = this.model?.moods ?? [];
    return card("Manual changes and room mood", "tap", html`
      ${numberField({ label: "Respect a manual change for", value: s.manual_override_minutes, placeholder: glob, unit: "min", min: 0, max: 1440, disabled: dis,
        tag: inheritTag(s.manual_override_minutes !== null && s.manual_override_minutes !== undefined, `${glob} min`, dis ? null : () => void this.send("room/set", { manual_override_minutes: null }, "Uses the global default")),
        help: "A light changed by a person or another automation is left alone this long (0: until the room is vacant). Empty: the global value.",
        onChange: (v) => void this.send("room/set", { manual_override_minutes: v }) })}
      ${selectField({ label: "Room mood", value: s.mood_override ?? "", disabled: dis,
        options: [["", `Global mood (now ${r.diagnostics?.global_mood ?? "none"})`], ...moods.map((m) => [m.name, m.name] as [string, string])],
        tag: s.mood_override ? html`<span class="inherit-tag override">Room value</span>` : html`<span class="inherit-tag">Global</span>`,
        onChange: (v) => void this.send("room/set", { mood_override: v || null }) })}
      ${(r.diagnostics?.manual_overrides ?? []).length ? html`<h4 class="lp-h">Active manual overrides</h4>
        <ul class="plain-list">${r.diagnostics.manual_overrides.map((o: Obj) => html`<li class="lp-row-line"><span><strong>${this.nameOf(o.entity_id)}</strong>
          <span class="small muted">${o.switch ? `wall switch ${o.switch}` : o.source ?? ""} · ${o.until ? `until ${fmt(o.until)}` : "until the room is vacant"}</span></span>
          ${dis ? nothing : html`<button type="button" class="btn btn-small" @click=${() => void this.send("override/clear", { entity_id: o.entity_id }, "Automatic lighting resumed")}>Resume</button>`}</li>`)}</ul>
        ${dis ? nothing : html`<button type="button" class="btn btn-small" @click=${() => void this.send("override/clear", {}, "Automatic lighting resumed for every light")}>${icon("lightbulb-auto", 16)}Resume all</button>`}` : nothing}`, { testid: "lighting-manual" });
  }

  private preliminaryCard(r: RoomModel): TemplateResult {
    const p = r.preliminary;
    if (!p) return nothing as unknown as TemplateResult;
    const e = p.effective;
    const set = p.settings;
    const g = p.global;
    const dis = !this.canEdit || this.busy;
    const save = (change: Obj, done = "Saved") => void this.send("preliminary/set", change, done);
    const targets: Obj[] = p.targets ?? [];
    const own = (k: string) => e.sources?.[k] === "room";
    const stored = () => (set.targets ?? []).map((t: Obj) => ({ ...t }));
    const setTarget = (entityId: string, patch: Obj) => save({ targets: stored().map((t: Obj) => (t.entity_id === entityId ? { ...t, ...patch } : t)) });
    const free = (p.candidates ?? []).filter((c: Obj) => !targets.some((t) => t.entity_id === c.entity_id));
    return card("Possible occupancy", "motion-sensor", html`
      <p class="small muted">A dim light at the first sign of someone (for example PIR motion) while occupancy is being confirmed. When confirmed the normal lighting takes over; if not, the light goes off again.</p>
      <div class="lp-inline">${toggleField({ label: "Dim light on possible occupancy", checked: !!e.enabled, disabled: dis, onChange: (v) => save({ enabled: v }) })}
        ${inheritTag(own("enabled"), g.enabled ? "on" : "off", dis ? null : () => save({ enabled: null }, "Uses the global default"))}</div>
      ${e.enabled ? html`
        ${p.auto_on === "never" ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>Automatic switch-on is off for this room: no preliminary light is switched on.</span></p>` : nothing}
        ${!targets.length ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>Choose a light: until then nothing changes in this room.</span></p>` : nothing}
        <h4 class="lp-h">Preliminary lights</h4>
        ${targets.map((t) => {
          const caps = t.capabilities ?? {};
          const ownT = t.settings ?? {};
          const eff = t.effective ?? {};
          return html`<div class="lp-subcard">
            <div class="lp-row-line"><strong>${t.name}</strong><span class="small muted">${t.state ?? ""}</span><span class="spacer"></span>
              ${dis ? nothing : html`<button type="button" class="btn btn-small" @click=${() => save({ targets: stored().filter((x: Obj) => x.entity_id !== t.entity_id) })}>${icon("delete", 16)}Remove</button>`}</div>
            ${t.managed ? nothing : html`<p class="small tone-warning">Not a controlled light of this room any more: it is not switched.</p>`}
            <div class="lp-form">
              ${caps.known && !caps.brightness && t.type !== "relay" ? html`<p class="small muted">On/off only: no brightness is sent.</p>` : t.type === "relay" ? html`<p class="small muted">Relay: switched on and off only.</p>`
                : numberField({ label: "Brightness", value: ownT.brightness_pct, placeholder: eff.brightness_pct, unit: "%", min: 1, max: 100, disabled: dis,
                  tag: html`<span class="inherit-tag ${ownT.brightness_pct !== null && ownT.brightness_pct !== undefined ? "override" : ""}">${ownT.brightness_pct !== null && ownT.brightness_pct !== undefined ? "this light" : "default"}</span>`,
                  onChange: (v) => setTarget(t.entity_id, { brightness_pct: v }) })}
              ${caps.color_temp || caps.known === false ? numberField({ label: "Colour temperature", value: ownT.color_temp_kelvin, placeholder: "keeps its colour", unit: "K",
                  min: caps.min_kelvin ?? p.limits.kelvin[0], max: caps.max_kelvin ?? p.limits.kelvin[1], step: 50, disabled: dis, onChange: (v) => setTarget(t.entity_id, { color_temp_kelvin: v }) }) : nothing}
              <div class="lp-field"><span class="lp-label">When confirmed</span>${segments(eff.on_confirm ?? "handover", CONFIRM_WORDS, (v) => setTarget(t.entity_id, { on_confirm: v }),
                { disabled: dis, label: `${t.name}: when occupancy is confirmed` })}</div>
            </div>
            ${t.scene ? html`<p class="small muted">${t.scene.switches_on ? `In ${t.scene.mood} the normal lighting switches it on: on confirmation it takes ${t.scene.mood}'s setting.`
              : t.scene.action === "off" ? `In ${t.scene.mood} it is Off: the normal lighting switches it off on confirmation.`
              : `${t.scene.mood} does not control it: Hand over leaves it on; Turn off switches it off once the normal lighting is on.`}</p>` : nothing}
          </div>`;
        })}
        ${dis || !free.length ? nothing : html`<label class="lp-field"><span class="lp-label">Add a light</span><select data-testid="lighting-prelim-add" @change=${(ev: Event) => {
          const v = (ev.target as HTMLSelectElement).value; if (v) save({ targets: [...stored(), { entity_id: v }] }, "Light added"); }}>
          <option value="">Choose a light…</option>${free.map((c: Obj) => html`<option value=${c.entity_id}>${c.name}${c.state === "on" ? " (on now)" : ""}</option>`)}</select></label>`}
        <h4 class="lp-h">Started by</h4>
        <p class="small muted">${e.signals_default ? "Default: the room's motion (Trigger) sensors." : "Custom selection."}
          ${!e.signals_default && !dis ? html`<button type="button" class="lp-link" @click=${() => save({ signals: null }, "Uses the default signals")}>Use the default</button>` : nothing}</p>
        <ul class="plain-list">${(p.sensors ?? []).map((x: Obj) => html`<li><label class="lp-check"><input type="checkbox" .checked=${x.selected} ?disabled=${dis}
            @change=${(ev: Event) => { const on = (ev.target as HTMLInputElement).checked; const cur = (p.sensors ?? []).filter((s: Obj) => s.selected).map((s: Obj) => s.entity_id);
              save({ signals: on ? [...cur, x.entity_id] : cur.filter((s: string) => s !== x.entity_id) }); }} />
          <span>${x.name} <span class="small muted">${x.kind_name}${x.default ? " · default" : ""}${x.used ? "" : " · not used for occupancy"}</span></span></label></li>`)}</ul>
        ${(p.confirmed_by ?? []).length ? html`<p class="small muted">Confirmed by: ${p.confirmed_by.join(", ")}.</p>`
          : html`<p class="notice notice-warning">${icon("warning", 20)}<span>No other signal can confirm occupancy here: the light goes off after the timeout and the normal lighting never switches on. Keep at least one sensor unticked.</span></p>`}
        <div class="lp-form">
          ${numberField({ label: "Timeout", value: set.timeout, placeholder: e.timeout, unit: "s", min: p.limits.timeout[0], max: p.limits.timeout[1], disabled: dis,
            tag: inheritTag(own("timeout"), fmtSecs(g.timeout), dis ? null : () => save({ timeout: null }, "Uses the global default")),
            help: e.timeout <= p.correlation_window ? html`<span class="tone-warning">Shorter than the confirmation window (${fmtSecs(p.correlation_window)}).</span>` : "Switched off this long after it started unless confirmed.",
            onChange: (v) => save({ timeout: v }) })}
          ${numberField({ label: "On below", value: set.on_below, placeholder: e.on_below ?? p.normal_on_below, unit: "lx", min: p.limits.lux[0], max: p.limits.lux[1], disabled: dis,
            tag: inheritTag(own("on_below"), g.on_below === null || g.on_below === undefined ? "same as normal" : `${g.on_below} lx`, dis ? null : () => save({ on_below: null }, "Uses the global default")),
            help: `Empty: ${g.on_below === null || g.on_below === undefined ? `the normal threshold (${p.normal_on_below} lx)` : `the global default (${g.on_below} lx)`}.`, onChange: (v) => save({ on_below: v }) })}
        </div>
        ${(p.cleanup ?? []).length ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>Still to switch off: ${p.cleanup.map((c: Obj) => `${c.name} (${c.reason})`).join(", ")}.</span></p>` : nothing}
        ${p.stage ? html`<p class="notice notice-info">${icon("info", 20)}<span>Now: possible occupancy since ${fmt(p.stage.started)} (${p.stage.evidence.join(", ")})${p.stage.lights.length ? ` · ${p.stage.lights.join(", ")} on` : ""} · ends ${fmt(p.stage.deadline)} unless confirmed.</span></p>` : nothing}` : nothing}
      <p class="small muted">Global defaults (brightness, fade, timeout) are in <a href="#/lighting/settings/possible">Lighting settings</a>.</p>`, { testid: "lighting-preliminary", wide: true });
  }

  private renderAutomatic(r: RoomModel): TemplateResult {
    return html`<div class="tab-grid lp-tab-grid">
      ${this.modeCard(r)}${this.vacancyCard(r)}${this.manualCard(r)}${this.daylightCard(r)}${this.preliminaryCard(r)}</div>`;
  }

  // ---------------------------------------------------------------- lights
  private lightsInspector(r: RoomModel, entityId: string): TemplateResult {
    const l = r.lights.find((x) => x.entity_id === entityId);
    if (!l) return nothing as unknown as TemplateResult;
    const pl: Obj | undefined = (r.priority?.lights ?? []).find((x: Obj) => x.entity_id === entityId);
    const excluded: string[] = r.settings?.excluded ?? [];
    const dis = !this.canEdit || this.busy;
    const fx = pl?.fixture;
    const d = this.fixtureDraft && this.fixtureDraft.entity_id === entityId ? this.fixtureDraft
      : { entity_id: entityId, kind: fx?.kind ?? "dumb", label: fx?.label ?? "", ha_label: fx?.ha_label ?? "", name: fx?.name ?? "" };
    const patch = (p: Obj) => (this.fixtureDraft = { ...d, ...p });
    const narrowed = fx?.kind === "dumb" && (pl?.entity_capabilities?.brightness || pl?.entity_capabilities?.color);
    return html`<aside class="inspector lp-inspector" aria-label="Light settings" data-testid="lighting-light-settings">
      <header class="inspector-head"><span class="inspector-icon">${icon(l.type === "relay" ? "electric-switch" : "lightbulb", 22)}</span>
        <div><h3>${l.name}</h3><span class="muted small mono">${l.entity_id}</span></div>
        <button type="button" class="icon-btn" aria-label="Close" @click=${() => (this.selected = null)}>${icon("close")}</button></header>
      <section class="inspector-section"><h4>Managed</h4>
        ${toggleField({ label: "Managed by automatic lighting", checked: !excluded.includes(entityId), disabled: dis || !!l.missing && !excluded.includes(entityId),
          help: l.excluded && !excluded.includes(entityId) ? `Not managed: ${l.excluded}` : l.discovered ? "Discovered from its area and light-type label." : l.explicit ? "Assigned in the Rooms editor." : undefined,
          onChange: (on) => void this.send("room/set", { excluded: on ? excluded.filter((x) => x !== entityId) : [...excluded, entityId] }, on ? "Managed again" : "Excluded in this room") })}
      </section>
      <section class="inspector-section"><h4>What it can do</h4>${capsList(pl?.capabilities ?? l.capabilities, l.type)}
        ${pl?.entity_capabilities && fx ? html`<p class="small muted">The entity itself reports:</p>${capsList(pl.entity_capabilities, l.type)}` : nothing}
        ${narrowed ? html`<p class="small">As a Dumb Light it is only switched on and off, although the entity could do more.</p>` : nothing}
        <p class="small muted">A classification never gives a light more than it reports; nothing is simulated.</p></section>
      ${l.missing || !r.priority ? nothing : html`<section class="inspector-section"><h4>Fixture type</h4>
        <p class="small">${fx ? fx.text : "Not classified"}</p>
        ${dis ? nothing : html`<div class="lp-form lp-form-1">
          ${selectField({ label: "Fixture type", value: d.kind, options: (r.priority.kinds ?? []).map((k: Obj) => [k.id, k.name] as [string, string]), onChange: (v) => patch({ kind: v }) })}
          <label class="lp-field"><span class="lp-label">${d.kind === "custom" ? "Label (required)" : "Extra label"}</span><input type="text" maxlength="60" .value=${d.label ?? ""} placeholder=${d.kind === "custom" ? "e.g. Cupboard light" : "optional"} @input=${(e: Event) => patch({ label: (e.target as HTMLInputElement).value })} /></label>
          <label class="lp-field"><span class="lp-label">Display name</span><input type="text" maxlength="60" .value=${d.name ?? ""} placeholder=${l.name} @input=${(e: Event) => patch({ name: (e.target as HTMLInputElement).value })} /></label>
          ${selectField({ label: "Home Assistant label (descriptive)", value: d.ha_label ?? "", options: [["", "None"], ...(r.priority.labels ?? []).map((x: Obj) => [x.label_id, x.name] as [string, string])], onChange: (v) => patch({ ha_label: v }) })}
        </div>
        <div class="row-actions lp-row"><button type="button" class="btn btn-primary btn-small" ?disabled=${d.kind === "custom" && !String(d.label ?? "").trim()}
            @click=${async () => { if (await this.send("fixture/set", { entity_id: entityId, kind: d.kind, label: d.label?.trim() || null, ha_label: d.ha_label || null, name: d.name?.trim() || null }, "Classified · nothing was sent")) this.fixtureDraft = null; }}>${icon("save", 16)}Save classification</button>
          ${fx ? html`<button type="button" class="btn btn-small btn-danger" @click=${() => void this.send("fixture/remove", { entity_id: entityId }, "Classification removed")}>${icon("delete", 16)}Remove</button>` : nothing}</div>`}
      </section>`}
    </aside>`;
  }

  private renderLights(r: RoomModel): TemplateResult {
    const z = r.zones;
    const dis = !this.canEdit || this.busy;
    const relays: string[] = z?.relays ?? r.settings?.relays ?? [];
    const cands: Obj[] = (z?.relay_candidates ?? []).filter((c: Obj) => this.relayAll || c.chosen || c.in_area);
    const conflicts: Obj[] = r.diagnostics?.conflicts ?? [];
    const groups = r.group_issues ?? [];
    return html`<div class="lp-split ${this.selected ? "lp-has-inspector" : ""}">
      <div class="lp-main">
        ${card("Lights of this room", "lightbulb-group", html`
          <p class="small muted">Lights are assigned to the room in the <a href=${`#/rooms/${encodeURIComponent(r.area_id)}`}>Rooms editor</a>; with discovery on, lights in this area that carry a light-type label are managed too. Select a light to exclude it or set its fixture type.</p>
          ${this.lightsTable(r, { testid: "lighting-lights-config", columns: "config" })}
          ${toggleField({ label: "Manage labelled lights in this area automatically", checked: !!r.settings?.auto_discover, disabled: dis,
            help: this.model?.settings?.exclude_label ? `Lights with the label "${this.model.labels.find((x) => x.label_id === this.model?.settings.exclude_label)?.name ?? this.model.settings.exclude_label}" are never managed.` : undefined,
            onChange: (v) => void this.send("room/set", { auto_discover: v }) })}`, { wide: true, testid: "lighting-lights-card" })}
        <div class="tab-grid">
          ${card("Relay outputs", "electric-switch", html`
            <p class="small muted">Switch entities that switch a lamp. Only those ticked here are lights of ${r.name}: they get on/off only (no brightness, colour or fade). Other switches are never treated as lights.</p>
            <ul class="plain-list">${cands.map((c) => html`<li><label class="lp-check"><input type="checkbox" .checked=${relays.includes(c.entity_id)} ?disabled=${dis}
                @change=${(e: Event) => void this.send("relay/set", { relays: (e.target as HTMLInputElement).checked ? [...relays, c.entity_id] : relays.filter((x) => x !== c.entity_id) }, "Relay outputs saved")} />
              <span>${c.name} <span class="small muted mono">${c.entity_id}</span>${c.in_area ? html` <span class="small muted">in this room</span>` : nothing}${c.missing ? html` <span class="small tone-warning">missing</span>` : nothing}</span></label></li>`)}
              ${!cands.length ? html`<li class="muted small">No switch entity in this room.</li>` : nothing}</ul>
            <button type="button" class="btn btn-small" @click=${() => (this.relayAll = !this.relayAll)}>${this.relayAll ? "Only this room's switches" : "Show every switch"}</button>`, { testid: "lighting-relays" })}
          ${card("Groups", "lightbulb-group", html`
            ${groups.length ? html`<ul class="plain-list">${groups.map((g) => html`<li class="lp-subcard"><strong>${this.nameOf(g.group)}</strong> · ${plural(g.members.length, "light")} ${g.members_known ? "" : "(possibly)"} also managed one by one${g.dismissed ? " · warning ignored" : ""}
                <span class="block small muted">${g.members.map((m) => this.nameOf(m)).join(", ")}</span>
                ${dis ? nothing : html`<span class="row-actions lp-row">${this.groupButtons(g.group)}</span>`}</li>`)}</ul>` : html`<p class="muted small">No overlapping groups.</p>`}
            ${(r.settings?.excluded ?? []).length ? html`<p class="small">Excluded here: ${r.settings.excluded.map((e: string) => this.nameOf(e)).join(", ")}</p>
              ${dis ? nothing : html`${(r.settings.excluded as string[]).filter((e) => r.lights.find((l) => l.entity_id === e)?.is_group).map((e) => html`<button type="button" class="btn btn-small" @click=${() => void this.send("room/group_choice", { group: e, choice: "restore" }, "Group restored")}>${icon("undo", 16)}Restore ${this.nameOf(e)}</button>`)}`}` : nothing}`, { testid: "lighting-groups" })}
          ${card("Automations that also control these lights", "robot", html`
            ${conflicts.length ? html`<ul class="plain-list">${conflicts.map((c) => html`<li><label class="lp-check"><input type="checkbox" .checked=${c.acknowledged} ?disabled=${dis}
                @change=${(e: Event) => void this.send("room/acknowledge", { automation: c.automation, acknowledged: (e.target as HTMLInputElement).checked })} />
              <span><strong>${c.name ?? c.automation}</strong> <span class="small muted">${c.enabled ? "enabled" : "disabled"} · ${this.nameOf(c.light)}</span>
                <span class="block small ${c.enabled && !c.acknowledged ? "tone-warning" : "muted"}">${c.acknowledged ? "Allowed: both control this light." : c.enabled ? "Not allowed: automatic lighting leaves this light alone." : "Disabled: no conflict."}</span></span></label></li>`)}</ul>`
              : html`<p class="muted small">None found.</p>`}`, { testid: "lighting-conflicts" })}
        </div>
      </div>
      ${this.selected ? this.lightsInspector(r, this.selected) : nothing}
    </div>`;
  }

  // ------------------------------------------------------------ supervisor
  private async loadSupervisor(check = false): Promise<void> {
    try {
      this.sup = await lighting<Obj>(cmd("supervisor/room"), { area_id: this.areaId, ...(check ? { check: true } : {}) });
      if (check) this.notice = "Checked";
    } catch (err) {
      this.error = err;
    }
  }

  private renderSupervisor(r: RoomModel): TemplateResult {
    const s = this.sup ?? r.supervisor;
    if (!s) return html`<p class="muted">The Room Supervisor is not available for this room.</p>`;
    const own = s.room_settings ?? {};
    const glob = s.global_settings ?? {};
    const dis = !this.canEdit || this.busy;
    const set = (change: Obj, done = "Saved") => void this.run(async () => { await lighting(cmd("supervisor/set"), { area_id: this.areaId, ...change }); await this.loadSupervisor(); }, done);
    const targets: Obj[] = s.targets ?? [];
    const sel = targets.find((t) => t.entity_id === this.supSel);
    const h = s.health ?? {};
    const open = this.adv.has("supervisor");
    const entries = s.decisions?.entries ?? [];
    return html`<div class="lp-stack"><div class="lp-two">
      ${card("Room Supervisor", "shield-check", html`
        <p>${chip(SUP_TONE[s.status] ?? "neutral", s.status_word ?? s.status)} <span class="muted">${s.reason}</span></p>
        ${kv([["Mode", SUP_MODE[s.mode] ?? s.mode], ["Discrepancies", s.discrepancies], ["Last check", fmt(s.last_check)], ["Last check OK", fmt(s.last_ok)],
          ["Sensors", h.at ? ((h.unavailable_sensors ?? []).length ? `${h.unavailable_sensors.length} unavailable` : "all available") : "—"],
          ["Devices", h.at ? ((h.unavailable_devices ?? []).length ? `${h.unavailable_devices.length} unavailable` : "all available") : "—"],
          s.metrics ? ["Since start", `${s.metrics.checks} checks · ${s.metrics.mismatches} mismatches · ${s.metrics.corrections} corrections`] : null])}
        ${this.canEdit ? html`<button type="button" class="btn btn-small" ?disabled=${this.busy} data-testid="lighting-sup-check" @click=${() => void this.run(() => this.loadSupervisor(true), "Checked")}>${icon("sync", 16)}Check now</button>` : nothing}`, { testid: "lighting-supervisor" })}
      ${card("Supervisor settings", "tune", html`
        <p class="small muted">Observing reports differences only. Automatic also corrects a device that did not follow a command (at most ${glob.max_retries} times, at most ${glob.max_per_hour} times an hour per device); a person's change is never corrected.</p>
        ${segments<string>(own.mode ?? glob.mode, [["observing", "Observing", "eye"], ["automatic", "Automatic", "sync"], ["off", "Off", "power"]], (v) => set({ mode: v }), { disabled: dis, label: "Room Supervisor mode", testid: "lighting-sup-mode" })}
        <p>${inheritTag(own.mode !== null && own.mode !== undefined, SUP_MODE[glob.mode] ?? glob.mode, dis ? null : () => set({ mode: null }, "Uses the global default"))}</p>
        ${advancedToggle(open, () => this.toggleAdv("supervisor"), "More supervisor settings")}
        ${open ? html`<div class="lp-form">${SUP_LIMITS.map(([k, label, unit]) => numberField({ label, value: own[k], placeholder: glob[k], unit, disabled: dis,
            tag: inheritTag(own[k] !== null && own[k] !== undefined, `${glob[k]}${unit ? ` ${unit}` : ""}`, dis ? null : () => set({ [k]: null }, "Uses the global default")), onChange: (v) => set({ [k]: v }) }))}
          ${selectField({ label: "Changes by automations", value: own.correct_external === null || own.correct_external === undefined ? "" : String(own.correct_external), disabled: dis,
            options: [["", `Global default (${glob.correct_external ? "correct once" : "report only"})`], ["false", "Report only"], ["true", "Correct once, then report a conflict"]],
            onChange: (v) => set({ correct_external: v === "" ? null : v === "true" }) })}</div>
          <p class="small muted">Global defaults: <a href="#/lighting/settings/supervisor">Lighting settings</a>.</p>` : nothing}`, { testid: "lighting-sup-settings" })}</div>
      ${card("Lights: winning rule, expected and actual", "lightbulb-group", html`
        ${targets.length ? html`<div class="table-wrap"><table class="plain-table lp-table lp-select-table"><thead><tr><th scope="col">Light</th><th scope="col">Rule</th><th scope="col">Expected</th><th scope="col">Actual</th><th scope="col">Status</th></tr></thead>
          <tbody>${targets.map((t) => html`<tr class=${this.supSel === t.entity_id ? "selected" : ""} tabindex="0" data-entity=${t.entity_id} @click=${() => (this.supSel = this.supSel === t.entity_id ? null : t.entity_id)}>
            <td>${t.name}${t.type === "relay" ? html` <span class="small muted">relay</span>` : nothing}</td><td>${typeof t.rule === "string" ? t.rule : t.rule?.name ?? "—"}</td>
            <td>${stateText(t.desired)}</td><td>${stateText(t.actual)}</td><td>${chip(SUP_RESULT[t.result]?.[1] ?? "neutral", SUP_RESULT[t.result]?.[0] ?? t.result)}</td></tr>`)}</tbody></table></div>`
          : html`<p class="muted">No light checked yet.</p>`}
        ${sel ? html`<div class="lp-subcard">${kv([["Entity", html`<span class="mono">${sel.entity_id}</span>`], ["Type", sel.type === "relay" ? "Relay output (on/off)" : "Light"],
          ["Winning rule", html`${sel.rule?.name ?? sel.rule ?? "—"}${sel.rule?.superseded?.length ? html` <span class="muted">over ${sel.rule.superseded.join(", ")}</span>` : nothing}`],
          ["Why", sel.reason ?? "—"], ["Last command", sel.last_command ? `${sel.last_command.service} · ${fmt(sel.last_command.at)}${sel.last_command.error ? ` · ${sel.last_command.error}` : ""}` : "—"],
          ["Last verification", sel.last_verification ? `${sel.last_verification.ok ? "verified" : "not reached"} · ${fmt(sel.last_verification.at)}` : "—"],
          ["Last correction", sel.last_correction ? `attempt ${sel.last_correction.attempt} · ${sel.last_correction.result} · ${fmt(sel.last_correction.at)}` : "—"],
          ["Error", sel.error ?? "—"]])}</div>` : nothing}
        <p class="small muted">Expected is what the winning rule requests; actual is what Home Assistant reports.</p>`, { wide: true, testid: "lighting-sup-targets" })}
      ${card("Supervisor log", "history", entries.length ? html`<ul class="lp-decisions">${entries.map((d: Obj) => decisionLine(d as never, {
          open: this.openDecision === d.id, onToggle: () => (this.openDecision = this.openDecision === d.id ? null : d.id) }))}</ul>`
        : html`<p class="muted">Nothing logged yet (routine successful checks are counted, not logged).</p>`, { wide: true })}
    </div>`;
  }

  // ------------------------------------------------------------------- log
  private renderLog(r: RoomModel): TemplateResult {
    const entries = r.decisions?.entries ?? [];
    return card("Decision log", "history", html`<p class="small muted">The last ${r.decisions?.size ?? 10} decisions of automatic lighting in ${r.name}, newest first, as recorded by the lighting runtime. Select an entry for everything it recorded.</p>
      ${entries.length ? html`<ul class="lp-decisions lp-log" data-testid="lighting-log">${entries.map((d) => decisionLine(d, {
        open: this.openDecision === d.id, onToggle: () => (this.openDecision = this.openDecision === d.id ? null : d.id) }))}</ul>`
        : html`<p class="muted">No decision recorded yet.</p>`}`, { wide: true });
  }

  // ---------------------------------------------------------------- render
  protected render(): unknown {
    const r = this.room;
    if (!r) {
      return html`${controlBanner(this.status)}${this.error ? html`${problemNotice(this.error)}<a class="btn" href="#/lighting">${icon("arrow-left", 20)}Lighting</a>`
        : html`<div class="card loading" role="status"><span class="spinner" aria-hidden="true"></span><span>Loading the room…</span></div>`}`;
    }
    const tab = (TABS.some((t) => t.key === this.tab) ? this.tab : "overview") as Tab;
    let content: unknown;
    switch (tab) {
      case "automatic": content = this.renderAutomatic(r); break;
      case "lights": content = this.renderLights(r); break;
      case "brightness": content = html`<sl-lp-brightness .room=${r} .host=${this} .busy=${this.busy} .canEdit=${this.canEdit}></sl-lp-brightness>`; break;
      case "zones": content = html`<sl-lp-zones mode="lighting" .room=${r} .model=${this.model} .host=${this} .busy=${this.busy} .canEdit=${this.canEdit}></sl-lp-zones>`; break;
      case "supervisor": content = this.renderSupervisor(r); break;
      case "log": content = this.renderLog(r); break;
      default: content = this.renderOverview(r);
    }
    return html`<div class="room-editor lp-room" data-area=${r.area_id}>
      ${this.renderHead(r)}
      ${controlBanner(this.status)}
      ${!r.enabled ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>This room is not enabled for Smart Comfort: enable it in the Rooms editor.</span></p>` : nothing}
      ${r.diagnostics?.manual_mode ? html`<p class="notice notice-warning">${icon("warning", 20)}<span><strong>Manual control — automatic lighting paused.</strong> Main Mood is Manual. Choose another mood to resume.</span></p>` : nothing}
      ${!this.canEdit ? html`<p class="notice" role="status">${icon("info", 20)}<span>Read only: only Home Assistant administrators can change lighting.</span></p>` : nothing}
      ${this.error && !this.dialog ? problemNotice(this.error) : nothing}
      ${this.notice ? html`<p class="notice notice-info" role="status" data-testid="lighting-saved">${icon("ok", 20)}<span>${this.notice}</span></p>` : nothing}
      ${this.renderTabs(r)}
      <div class="tab-panel" role="tabpanel">${content}</div>
      ${this.dialogTemplate(r)}
    </div>`;
  }
}

customElements.define("sl-lighting-room", SlLightingRoom);
