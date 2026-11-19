// Curtains (Phase A5): every window's automatic curtains at a glance - mode, covers and positions, the recognised
// and the desired preset, the automatic decision and its condition, holds, manual changes, warnings and the last
// operation - plus the Windows, Global Modes, Global Schedule, Environment and Diagnostics tabs. Every value comes
// from the curtain runtime's model (``smart_occupancy/curtains/model``); a window opens its own page.
import "../lighting/lighting-pages.css";
import "../curtains/curtains.css";
import "../curtains/curtains-windows";
import "../curtains/curtains-modes";
import "../curtains/curtains-schedule";
import "../curtains/curtains-environment";
import "../curtains/curtains-diagnostics";
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { chip, pageHeader } from "../ui";
import { cmd, curtains, type ChangeResult, type CurtainsModel, type LayerModel, type MigrationItem, type WindowModel } from "../curtains/api";
import {
  adminNotice,
  closeDialog,
  COND_ICON,
  conditionChip,
  controlBanner,
  coverStateText,
  CurtainsElement,
  fmt,
  impactList,
  KIND_ICON,
  loadModel,
  modeChip,
  openDialog,
  OVERALL_TONE,
  PHASE_ICON,
  plural,
  POLICY_TEXT,
  positionBar,
  problemNotice,
  windowWarnings,
} from "../curtains/components";

type Tab = "overview" | "windows" | "modes" | "schedule" | "environment" | "diagnostics";
type Filter = "all" | "automatic" | "attention";
const TABS: Array<{ key: Tab; title: string; icon: string }> = [
  { key: "overview", title: "Overview", icon: "view-dashboard" },
  { key: "windows", title: "Windows", icon: "window" },
  { key: "modes", title: "Global Modes", icon: "view-grid" },
  { key: "schedule", title: "Global Schedule", icon: "calendar-clock" },
  { key: "environment", title: "Environment", icon: "weather-sunny" },
  { key: "diagnostics", title: "Diagnostics", icon: "stethoscope" },
];

interface MigrationState {
  choices: Record<string, string>;
  result: ChangeResult | null;
  error: string | null;
  policy: "now" | "next_change" | null;
  busy: boolean;
}

const MIG_GROUPS: Array<[string, string]> = [
  ["conflict", "Hidden Global Modes assignments"], ["inexact", "Cannot be kept exactly"], ["schedule", "Room and window schedules"],
  ["morning", "Windows without a Morning preset"], ["mapping", "Moved to Global Modes unchanged"],
];

export class SlCurtains extends CurtainsElement {
  static properties = { ...CurtainsElement.properties, tab: { type: String }, model: { state: true }, filter: { state: true }, mig: { state: true } };
  declare tab: string;
  declare model: CurtainsModel | null;
  declare filter: Filter;
  declare mig: MigrationState | null;
  private loading = false;

  constructor() {
    super();
    this.tab = "overview";
    this.model = null;
    this.filter = "all";
    this.mig = null;
  }

  async reload(): Promise<void> {
    if (this.loading) return;
    this.loading = true;
    try {
      this.model = await loadModel();
      this.error = null;
    } catch (err) {
      this.error = err;
    } finally {
      this.loading = false;
    }
  }

  // ----------------------------------------------------------------- the migration (settings of an earlier version)
  private async migOpen(): Promise<void> {
    this.mig = { choices: {}, result: null, error: null, policy: null, busy: true };
    await openDialog(this, "dialog.cu-migration");
    await this.migReview();
  }

  private async migReview(): Promise<void> {
    const g = this.mig;
    if (!g) return;
    g.busy = true;
    this.requestUpdate();
    try {
      g.result = await curtains<ChangeResult>(cmd("migration/change"), { choices: g.choices, dry_run: true });
      g.error = null;
    } catch (err) {
      g.error = err instanceof Error ? err.message : String(err);
    }
    g.busy = false;
    this.requestUpdate();
  }

  private async migApply(): Promise<void> {
    const g = this.mig;
    if (!g?.result || g.busy) return;
    if (g.result.missing?.length || (g.result.needs_confirm && !g.policy)) return;
    const ok = await this.run(() => curtains(cmd("migration/change"), { choices: g.choices, confirm: !!g.result?.needs_confirm, policy: g.policy ?? "now" }), "Migration applied");
    if (ok) this.migClose();
  }

  private migClose(): void {
    closeDialog(this, "dialog.cu-migration");
    this.mig = null;
  }

  private migrationDialog(): TemplateResult | typeof nothing {
    const g = this.mig;
    if (!g) return nothing;
    const r = g.result;
    const words = r?.words ?? this.model?.migration.words ?? {};
    const item = (x: MigrationItem) => html`<li class="cu-impact-row" data-item=${x.id}><div class="cu-impact-head"><strong>${x.title}</strong>${x.required ? chip("warning", "choice needed") : nothing}</div>
      <div class="small">${x.detail}</div>
      ${x.choices.length ? html`<div class="cu-radios" role="radiogroup" aria-label=${x.title}>${x.choices.map((c) => html`<label class="lp-check"><input type="radio" name=${`mig-${x.id}`} value=${c} .checked=${(g.choices[x.id] ?? x.default) === c}
        @change=${() => { g.choices = { ...g.choices, [x.id]: c }; void this.migReview(); }} /><span>${words[c] ?? c}</span></label>`)}</div>` : nothing}</li>`;
    const missing = r?.missing?.length ?? 0;
    const blocked = !r || g.busy || missing > 0 || (r.needs_confirm && !g.policy);
    return html`<dialog class="sheet sheet-full review-dialog cu-migration" aria-labelledby="cu-mig-title" data-testid="curtains-migration" @close=${() => (this.mig = null)}>
      <div class="sheet-inner"><header class="sheet-head"><h2 id="cu-mig-title">Move everything to Global Schedule and Global Modes</h2><button type="button" class="icon-btn" aria-label="Close" @click=${() => this.migClose()}>${icon("close")}</button></header>
        <div class="sheet-body">
          <p class="small muted">Each old window, cover or room setting becomes a Global Modes assignment with exactly the same positions (a cover preset is added where one is needed). Afterwards the old settings are removed and only Global Schedule and Global Modes configure the routine. Nothing moves because of the migration itself.</p>
          ${g.error ? html`<p class="notice notice-error" role="alert">${icon("error", 20)}<span>${g.error}</span></p>` : nothing}
          ${this.error ? problemNotice(this.error) : nothing}
          ${g.busy && !r ? html`<div class="loading" role="status"><span class="spinner"></span><span>Working out the migration…</span></div>` : nothing}
          ${r ? html`
            ${MIG_GROUPS.map(([k, title]) => { const list = (r.items ?? []).filter((x) => x.kind === k); return list.length ? html`<div class="review-block"><h3>${title} (${list.length})</h3><ul class="cu-impact">${list.map(item)}</ul></div>` : nothing; })}
            ${missing ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>${missing} item${missing === 1 ? " needs" : "s need"} a choice before the migration can be applied.</span></p>` : nothing}
            ${r.changes.length ? html`<div class="review-block"><h3>What will be saved (${r.changes.length})</h3><ul class="change-list">${r.changes.map((c) => html`<li>${icon("check", 16)}<span>${c}</span></li>`)}</ul></div>` : nothing}
            ${!missing ? html`<div class="review-block"><h3>${r.impact.length ? "Windows that would act differently now" : "No window changes what it does now"}</h3>${impactList(r.impact)}</div>` : nothing}
            ${r.needs_confirm ? html`<div class="review-block"><h3>When to apply</h3><div class="cu-radios" role="radiogroup" aria-label="When to apply">${POLICY_TEXT.map(([k, label, hint]) => html`<label class="lp-check"><input type="radio" name="cu-mig-policy" value=${k} .checked=${g.policy === k} @change=${() => { g.policy = k as "now" | "next_change"; this.requestUpdate(); }} /><span><strong>${label}</strong><span class="block small muted">${hint}</span></span></label>`)}</div></div>` : nothing}` : nothing}
        </div>
        <footer class="sheet-foot"><button type="button" class="btn" ?disabled=${g.busy} @click=${() => this.migClose()}>Cancel</button>
          <button type="button" class="btn btn-primary" data-testid="curtains-migration-apply" ?disabled=${blocked} @click=${() => void this.migApply()}>${icon("swap-bold", 18)}Apply migration</button></footer></div></dialog>`;
  }

  private migrationBanner(m: CurtainsModel): TemplateResult | typeof nothing {
    const mig = m.migration;
    if (!mig.pending) return nothing;
    return html`<p class="notice notice-warning" role="status" data-testid="curtains-migration-banner">${icon("swap-bold", 20)}<span><strong>Settings from an earlier version are still active.</strong> ${mig.text}. Until then every window behaves exactly as before.${mig.required ? ` ${mig.required} need${mig.required === 1 ? "s" : ""} your choice.` : ""}</span>
      ${this.canEdit ? html`<button type="button" class="btn btn-small" data-testid="curtains-migration-open" @click=${() => void this.migOpen()}>Review migration…</button>` : nothing}</p>`;
  }

  // ----------------------------------------------------------------- overview
  private attention(w: WindowModel): number {
    return windowWarnings(w).length + w.layers.filter((l) => l.missing || !l.available).length + (w.last && ["failed", "partial", "timeout"].includes(w.last.status) ? 1 : 0);
  }

  private shown(w: WindowModel): boolean {
    if (this.filter === "automatic") return w.mode === "automatic";
    if (this.filter === "attention") return this.attention(w) > 0;
    return true;
  }

  private desired(w: WindowModel): { name: string; positions: Record<string, number | null> } | null {
    const d = w.auto?.decision;
    if (d?.preset_id && d.target.state === "target") return { name: d.preset_name ?? d.preset_id, positions: d.target.positions };
    const p = w.presets.find((x) => x.preset_id === w.selected_preset);
    return p ? { name: p.name, positions: p.positions } : null;
  }

  private coverRow(l: LayerModel, w: WindowModel): TemplateResult {
    const op = w.last?.status === "running" ? w.last.items.find((i) => i.layer_id === l.layer_id) : null;
    const want = this.desired(w);
    const target = op?.target ?? (want ? want.positions[l.layer_id] : null);
    return html`<div class="cu-cover-row" data-layer=${l.layer_id}>${icon(KIND_ICON[l.kind] ?? "blinds", 20)}
      <span><span class="cu-cover-name"><strong>${l.name}</strong> <span class="small muted">${l.kind_word}</span></span>${positionBar(l.position, l.caps, { target: target !== l.position ? target : null, name: l.name, moving: l.moving })}</span>
      <span class="cu-cover-val">${coverStateText(l)}${target !== null && target !== undefined && (op || target !== l.position) ? html`<span class="block small muted" title="Desired">→ ${target} %</span>` : nothing}</span></div>`;
  }

  private autoBlock(w: WindowModel): TemplateResult {
    const a = w.auto;
    if (w.mode === "off") return html`<div class="cu-auto">${icon("power", 20)}<span class="muted">Off: curtains are not evaluated.</span></div>`;
    const d = a?.decision;
    if (!d) return html`<div class="cu-auto">${icon("pending", 20)}<span class="muted">Evaluating…</span></div>`;
    const what = d.preset_name ? html`<strong>${d.preset_name}</strong> <span class="small muted">(${d.source_word})</span>`
      : d.condition === "manual_override" || d.condition === "waiting" ? "keeps the position"
      : d.target.state === "no_action" ? html`No action <span class="small muted">(${d.source_word})</span>` : "no preset assigned";
    const st = a.status;
    const next = a.schedule?.next?.[0];
    return html`<div class="cu-auto" data-condition=${d.condition}>${icon(COND_ICON[d.condition] ?? "robot", 20)}
      <div><div class="cu-auto-head">${conditionChip(d)} · ${what}${w.mode === "observing" ? chip("info", "Observing") : nothing}</div>
        <div class="small">${d.text}</div>
        ${st && st.text && st.text !== d.text ? html`<div class="small">${chip(st.action === "blocked" || st.action === "failed" ? "warning" : st.action === "moved" ? "ok" : "neutral", st.word)} ${st.text}</div>` : nothing}
        <div class="cu-chips small">
          ${a.hold ? chip("warning", `Manual hold · ${a.hold.policy_word}`) : nothing}
          ${a.sleep.enabled ? chip(a.sleep.active ? "info" : "neutral", `Sleep ${a.sleep.state ?? "—"}`) : nothing}
          ${a.lamp.enabled ? chip(a.lamp.active ? "info" : "neutral", `Lamp ${a.lamp.state ?? "—"}${a.lamp.active ? " · override" : ""}`) : nothing}
          ${chip("neutral", a.light.entity ? `${a.light.value === null ? "light unavailable" : `${Math.round(a.light.value)} lx`}${a.light.daylight === true ? " · daylight" : a.light.daylight === false ? " · dark" : ""}` : a.light.fallback === "sun" ? "no light sensor · sun fallback" : "no light sensor")}
          ${w.solar_protection ? chip(w.exposure.exposed ? "warning" : "neutral", w.exposure.exposed ? "sun in front" : w.exposure.text) : nothing}
        </div>
        <div class="small muted">${a.next_evaluation ? `Next check ${fmt(a.next_evaluation)}` : ""}${next ? ` · next ${next.word} ${next.approx ? "≈ " : ""}${fmt(next.at)}` : ""}</div>
      </div></div>`;
  }

  private windowCard(w: WindowModel): TemplateResult {
    const href = `#/curtains/windows/${encodeURIComponent(w.window_id)}`;
    const warnings = windowWarnings(w);
    const attention = this.attention(w);
    const op = w.last;
    return html`<article class="card lp-room-card cu-window-card cu-mode-${w.mode} ${attention ? "cu-attention" : ""}" data-window=${w.window_id} data-testid="curtains-window-card">
      <header class="lp-room-head">${icon("window", 22)}<h3><a class="text-link" href=${href}>${w.name}</a></h3>${modeChip(w.mode, w.mode_word)}</header>
      <p class="small muted">${w.area_name ?? "No room"} · ${w.compass ? `${w.compass}${w.effective_bearing !== null ? ` ${Math.round(w.effective_bearing)}°` : ""}` : "no direction"}${w.running ? " · applying a preset" : ""}</p>
      ${w.layers.length ? w.layers.map((l) => this.coverRow(l, w)) : html`<p class="muted small">No covers yet.</p>`}
      ${this.autoBlock(w)}
      <dl class="lp-facts">
        <div><dt>Now</dt><dd>${w.current.status === "preset" ? html`<strong>${w.current.text}</strong>` : w.current.text}</dd></div>
        <div><dt>Desired</dt><dd>${w.presets.find((p) => p.preset_id === w.selected_preset)?.name ?? html`<span class="muted">none selected</span>`}</dd></div>
        <div><dt>Manual change</dt><dd>${w.manual ? html`${w.manual.name}: ${w.manual.from ?? "—"} → ${w.manual.to ?? "—"} %<span class="small muted"> · ${fmt(w.manual.at)}, ${w.manual.source === "person" ? "by a person in Home Assistant" : "outside Smart Occupancy"}</span>` : html`<span class="muted">none</span>`}</dd></div>
        <div><dt>Last operation</dt><dd>${op ? html`${chip(OVERALL_TONE[op.status] ?? "neutral", this.model?.words.overall[op.status] ?? op.status)} <span class="small">${op.explanation}</span><span class="small muted"> · ${fmt(op.finished ?? op.started)}</span>` : html`<span class="muted">no preset applied yet</span>`}</dd></div>
      </dl>
      <footer class="lp-room-foot">
        ${warnings.length ? html`<span class="chip chip-warning" title=${warnings.join("\n")}>${icon("warning", 14)}<span class="chip-label">${plural(warnings.length, "warning")}</span></span>` : nothing}
        ${w.layers.some((l) => l.missing || !l.available) ? chip("warning", `${plural(w.layers.filter((l) => l.missing || !l.available).length, "cover")} unavailable`) : nothing}
        ${!warnings.length && !w.layers.some((l) => l.missing || !l.available) ? chip("ok", "No warnings") : nothing}
        <span class="spacer"></span><a class="btn btn-small" href=${href}>Open${icon("chevron-right", 18)}</a>
      </footer>
    </article>`;
  }

  private overview(m: CurtainsModel): TemplateResult {
    const windows = m.windows;
    const auto = windows.filter((w) => w.mode === "automatic").length;
    const holds = windows.filter((w) => w.auto?.hold).length;
    const moving = windows.filter((w) => w.running || w.layers.some((l) => l.moving)).length;
    const problems = windows.filter((w) => this.attention(w) > 0).length;
    const h = m.schedule.house;
    const shown = windows.filter((w) => this.shown(w));
    const filters: Array<[Filter, string]> = [["all", "All windows"], ["automatic", "Automatic"], ["attention", "Needs attention"]];
    return html`<div data-testid="curtains-overview">
      ${this.migrationBanner(m)}
      <section class="card wide lp-summary" data-testid="curtains-summary">
        <div class="cu-summary-row">
          <div class="cu-house">${icon(PHASE_ICON[h.phase] ?? "calendar-clock", 28)}<div><span class="muted small">Household schedule now</span><strong class="cu-big" data-phase=${h.phase}>${h.word}</strong><span class="block small muted">${h.reason}${h.waiting ? ` · waiting: ${h.waiting}` : ""}</span></div></div>
          <div class="stat-tiles lp-tiles">
            <div class="stat"><span class="stat-value">${windows.length}</span><span class="stat-label">windows</span></div>
            <div class="stat"><span class="stat-value">${auto}</span><span class="stat-label">automatic</span></div>
            <div class="stat"><span class="stat-value ${holds ? "tone-warning" : ""}">${holds}</span><span class="stat-label">manual holds</span></div>
            <div class="stat"><span class="stat-value ${moving ? "tone-info" : ""}">${moving}</span><span class="stat-label">moving</span></div>
            <div class="stat"><span class="stat-value ${problems ? "tone-warning" : ""}">${problems}</span><span class="stat-label">need attention</span></div>
          </div>
        </div>
        <p class="small muted">Every window follows the <a class="text-link" href="#/curtains/schedule">Global Schedule</a> and uses its presets from <a class="text-link" href="#/curtains/modes">Global Modes</a>; sleep, the window lamp, the sun and manual moves can take over for a while. Observing windows only say what they would do.</p>
      </section>
      ${windows.length ? html`
        <div class="toolbar lp-toolbar" role="group" aria-label="Show windows">${filters.map(([k, l]) => html`<button type="button" class="btn btn-small ${this.filter === k ? "lp-on" : ""}" aria-pressed=${this.filter === k ? "true" : "false"} @click=${() => (this.filter = k)}>${l}</button>`)}</div>
        ${m.rooms.map((room) => { const ws = room.windows.map((id) => windows.find((w) => w.window_id === id)).filter((w): w is WindowModel => !!w && this.shown(w)); return ws.length ? html`<h3 class="lp-h">${room.name}</h3><div class="lp-room-grid">${ws.map((w) => this.windowCard(w))}</div>` : nothing; })}
        ${!shown.length ? html`<p class="muted">No window matches this filter.</p>` : nothing}`
        : html`<div class="card empty-state"><p><strong>No windows yet.</strong></p><p class="muted">Create one on the Windows tab from the covers Home Assistant knows.</p><a class="btn" href="#/curtains/windows">${icon("window", 20)}Windows</a></div>`}
    </div>`;
  }

  protected render(): unknown {
    const header = pageHeader("Curtains", "Automatic curtains of every window, live decisions and warnings.",
      html`<div class="page-actions"><a class="btn" href="#/curtains/control">${icon("swap", 20)}Control</a>
        <button type="button" class="icon-btn" aria-label="Refresh" title="Refresh" ?disabled=${this.loading} @click=${() => void this.refreshAll()}>${icon("refresh")}</button></div>`);
    const m = this.model;
    const tabs = html`<nav class="tabs lp-tabs" role="tablist" aria-label="Curtains">${TABS.map((t) => html`<a role="tab" href=${`#/curtains${t.key === "overview" ? "" : `/${t.key}`}`} class=${this.tab === t.key ? "active" : ""} aria-selected=${this.tab === t.key ? "true" : "false"} data-tab=${t.key}>${icon(t.icon, 18)}<span>${t.title}</span></a>`)}</nav>`;
    if (!m) return html`${header}${controlBanner(this.status)}${tabs}${this.error ? problemNotice(this.error) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`}`;
    const tab = (TABS.find((t) => t.key === this.tab)?.key ?? "overview") as Tab;
    let content: unknown;
    switch (tab) {
      case "windows": content = html`<sl-curtains-windows .model=${m} .host=${this}></sl-curtains-windows>`; break;
      case "modes": content = html`<sl-curtains-modes .model=${m} .host=${this}></sl-curtains-modes>`; break;
      case "schedule": content = html`<sl-curtains-schedule .model=${m} .host=${this}></sl-curtains-schedule>`; break;
      case "environment": content = html`<sl-curtains-environment .model=${m} .host=${this}></sl-curtains-environment>`; break;
      case "diagnostics": content = html`<sl-curtains-diagnostics .model=${m} .host=${this}></sl-curtains-diagnostics>`; break;
      default: content = this.overview(m);
    }
    return html`${header}${controlBanner(this.status)}
      ${!this.canEdit && this.status ? adminNotice() : nothing}
      ${this.error && tab === "overview" ? problemNotice(this.error) : nothing}
      ${this.notice ? html`<p class="notice" role="status">${icon("ok", 20)}<span>${this.notice}</span></p>` : nothing}
      ${tab !== "overview" ? this.migrationBanner(m) : nothing}
      ${tabs}
      <div class="tab-panel" role="tabpanel">${content}</div>
      ${this.migrationDialog()}`;
  }
}

customElements.define("sl-curtains", SlCurtains);
