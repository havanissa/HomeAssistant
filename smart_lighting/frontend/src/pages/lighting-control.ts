// Lighting → Control (Phase A4): which controller commands the lights, the shadow comparison, the reviewed
// handover, the rollback and the configuration export. The owner shown is the one the integration acknowledges.
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { apiUrl } from "../store";
import { chip, kv, pageHeader } from "../ui";
import { haTime } from "../presence/components";
import { duration } from "../presence/model";
import { handover, ownerLabel, releaseControl, type CommandBrief, type LightingStatus } from "../lighting/api";
import { LightingElement, problemNotice } from "../lighting/components";

const REFRESH_MS = 5000;
const PURPOSES: Record<string, string> = {
  lighting: "Automatic lighting", switch: "Wall switches", test: "Profile tests", supervisor: "Supervisor corrections",
  priority: "Priority Inputs", priority_release: "Priority Input release", preliminary: "Possible occupancy",
  preliminary_end: "Possible occupancy ended", zone_release: "Zone release", vacancy: "Vacancy",
};
const REASONS: Record<string, string> = {
  handover: "Reviewed handover", released: "Returned by the App", returned_by_service: "Returned (service or button)",
  lease_expired: "The App stopped answering", app_restarted: "The App restarted", not_resumed: "The App did not come back after a restart",
  resumed: "The same App resumed after a restart", revoked: "The App could not resume",
};

export class SlLightingControl extends LightingElement {
  static properties = { ...LightingElement.properties, dialog: { state: true }, understood: { state: true }, copy: { state: true } };
  declare dialog: "handover" | "release" | "return" | null;
  declare understood: boolean;
  declare copy: { identical: boolean; differences: string[] } | null;
  private timer: number | undefined;

  constructor() {
    super();
    this.dialog = null;
    this.understood = false;
    this.copy = null;
  }

  connectedCallback(): void {
    super.connectedCallback();
    this.timer = window.setInterval(() => void this.refreshAll(), REFRESH_MS);
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    window.clearInterval(this.timer);
  }

  private async open(kind: "handover" | "release" | "return"): Promise<void> {
    this.dialog = kind;
    this.understood = false;
    this.error = null;
    await this.updateComplete;
    this.querySelector<HTMLDialogElement>("dialog.lighting-dialog")?.showModal();
  }

  private close(): void {
    this.querySelector<HTMLDialogElement>("dialog.lighting-dialog")?.close();
    this.dialog = null;
  }

  private async act(kind: "handover" | "release" | "return"): Promise<void> {
    const ok = await this.run(async () => {
      if (kind === "handover") this.copy = (await handover()).copy;
      else await releaseControl(kind);
    });
    if (ok) {
      this.close();
      await this.refreshAll();
    }
  }

  private name(areaId: string | null): string {
    return areaId ? this.status?.area_names[areaId] ?? areaId : "—";
  }

  private command(c: CommandBrief): TemplateResult {
    const data = Object.entries(c.data).map(([k, v]) => `${k} ${Array.isArray(v) ? v.join(",") : v}`).join(", ");
    return html`<span>${c.service} <strong>${c.entities.join(", ")}</strong>${data ? html` <span class="muted">${data}</span>` : nothing}
      ${c.error ? html` <span class="tone-error">(${c.error})</span>` : nothing}</span>`;
  }

  private hero(d: LightingStatus): TemplateResult {
    const owner = ownerLabel(d.acknowledged_owner);
    const fmt = haTime();
    const integ = d.integration_status;
    return html`<section class="card wide engine-hero engine-${owner.app ? "app" : "integration"}" data-testid="lighting-owner">
      <div class="engine-hero-main">
        <span class="engine-hero-icon">${icon(owner.app ? "lightbulb-auto" : "home-assistant", 36)}</span>
        <div>
          <p class="muted small">Commands the lights now (acknowledged by the integration)</p>
          <h3 class="engine-owner" data-owner=${d.acknowledged_owner ?? "unknown"}>${owner.label}</h3>
          <p class="small">${chip(d.mode === "owner" ? "ok" : d.mode === "shadow" ? "info" : "warning",
            d.mode === "owner" ? "The App controls lighting" : d.mode === "shadow" ? "Shadow: the App compares, sends nothing" : d.mode)}
            ${integ ? html`<span class="muted"> · epoch ${integ.epoch} · integration controller ${integ.controller}</span>` : nothing}</p>
          ${owner.app && integ ? html`<p class="small" data-testid="lighting-freshness">Lease until ${fmt(integ.lease_until)} ·
            ${integ.executed} command(s) executed for the App, ${integ.execute_errors} failed
            ${integ.owner_connected ? nothing : html` <strong class="tone-warning">· the App is not connected</strong>`}</p>` : nothing}
          <p class="small muted">Configuration: ${owner.app ? "stored in the App (mirrored to the integration for a rollback)" : "stored in the integration"}.</p>
        </div>
      </div>
      <div class="engine-hero-actions">
        ${owner.app
          ? html`<button type="button" class="btn btn-danger btn-large" data-testid="lighting-release" ?disabled=${this.busy || !this.canEdit} @click=${() => void this.open("release")}>${icon("undo", 20)}Return control to the integration</button>`
          : html`<button type="button" class="btn btn-primary" data-testid="lighting-handover" ?disabled=${this.busy || !d.readiness.ready || !this.canEdit} @click=${() => void this.open("handover")}>${icon("swap", 20)}Review handover to the App…</button>
             <span class="small muted">${d.readiness.ready ? "Every readiness check passed." : "Available when every readiness check passes."}</span>`}
        <button type="button" class="btn" data-testid="lighting-return" ?disabled=${this.busy || !this.canEdit || !owner.app && d.mode !== "owner"} @click=${() => void this.open("return")} title="Also works when the App lost its connection">${icon("home-assistant", 18)}Force return</button>
        <a class="btn" href=${apiUrl("api/lighting/export")} download data-testid="lighting-export">${icon("download", 18)}Export configuration and state</a>
      </div>
    </section>`;
  }

  private readiness(d: LightingStatus): TemplateResult {
    const t = d.readiness.thresholds;
    const app = ownerLabel(d.acknowledged_owner).app;
    return html`<section class="card" data-testid="lighting-readiness"><header class="card-head">${icon("ok")}<h3>Handover readiness</h3>
        ${app ? chip("neutral", "Handed over") : d.readiness.ready ? chip("ok", "Ready") : chip("warning", "Not ready")}</header>
      <p class="muted small">A handover needs ${duration(t.min_shadow_seconds)} of comparison, ${t.min_commands} matching commands and no command mismatch.</p>
      <ul class="checklist">${d.readiness.checks.map((c) => html`<li class="check check-${c.status}" data-check=${c.key}>
        <span class="tone-${c.status}">${icon(c.status === "ok" ? "ok" : "error", 18)}</span>
        <span><strong>${c.title}</strong><span class="block small muted">${c.detail}</span></span></li>`)}</ul></section>`;
  }

  private facts(d: LightingStatus): TemplateResult {
    const integ = d.integration_status;
    const same = d.fingerprint && integ?.fingerprint === d.fingerprint;
    const rejections = integ ? Object.entries(integ.rejections) : [];
    return html`<section class="card" data-testid="lighting-versions"><header class="card-head">${icon("shield-check")}<h3>Versions and bridge</h3></header>
      ${kv([
        ["Bridge", chip(d.bridge === "connected" ? "ok" : d.bridge === "subscribing" ? "warning" : "error", d.bridge)],
        d.bridge_error ? ["Detail", d.bridge_error] : null,
        ["App", d.app_version],
        ["Integration", integ?.integration_version ?? "—"],
        ["Lighting code", same ? chip("ok", "Identical") : chip("error", "Different")],
        ["Configuration", d.config_revision === d.integration_revision ? chip("ok", `In sync (${d.config_revision ?? "—"})`) : chip("warning", `${d.config_revision ?? "—"} / ${d.integration_revision ?? "—"}`)],
        ["Refused App messages", rejections.length ? rejections.map(([k, n]) => `${k} ×${n}`).join(", ") : "none"],
        ["Mirror updated", haTime()(integ?.mirror_at)],
      ])}</section>`;
  }

  private comparison(d: LightingStatus): TemplateResult {
    const c = d.comparison;
    const purposes = Object.entries(c.commands.by_purpose);
    return html`<section class="card wide" data-testid="lighting-comparison"><header class="card-head">${icon("swap")}<h3>Shadow comparison</h3>
        ${c.running ? chip("ok", "Running") : chip("neutral", "Not running")}</header>
      <p class="muted small">Both controllers run the same code on the same events. A command matches when the same service, targets and data appear on both sides within ${c.window_seconds} s; the App's are never sent.</p>
      <div class="stat-tiles">
        <div class="stat"><span class="stat-value">${duration(c.seconds)}</span><span class="stat-label">compared</span></div>
        <div class="stat"><span class="stat-value" data-testid="lighting-matched">${c.commands.matched}</span><span class="stat-label">matching commands</span></div>
        <div class="stat"><span class="stat-value ${c.commands.mismatches ? "tone-error" : ""}" data-testid="lighting-mismatches">${c.commands.mismatches}</span><span class="stat-label">command mismatches</span></div>
        <div class="stat"><span class="stat-value">${c.decisions.matched}</span><span class="stat-label">matching decisions</span></div>
        <div class="stat"><span class="stat-value ${c.decisions.differences ? "tone-warning" : ""}">${c.decisions.differences}</span><span class="stat-label">decision differences</span></div>
      </div>
      ${purposes.length ? html`<p class="small">${purposes.map(([p, n]) => html`${chip("ok", `${PURPOSES[p] ?? p}: ${n}`)} `)}</p>` : nothing}
      ${this.discrepancyList(d)}
    </section>`;
  }

  private discrepancyList(d: LightingStatus): TemplateResult {
    const fmt = haTime();
    const items = d.comparison.discrepancies;
    const decisions = d.comparison.decision_differences;
    return html`<h4>Command mismatches</h4>
      ${items.length ? html`<ul class="engine-records">${items.map((r) => html`<li class="engine-record record-disagreement">
        <span class="tone-error">${icon("error", 18)}</span>
        <span><strong>${this.name(r.area_id)}</strong> · ${PURPOSES[r.purpose] ?? r.purpose} · only the ${r.only === "app" ? "App" : "integration"} sent ${this.command(r.command)}
          <span class="block small muted">${fmt(r.at)}${r.other_side.length ? html` · the other side: ${r.other_side.map((o) => this.command(o))}` : " · the other side sent nothing for these lights"}</span></span>
      </li>`)}</ul>` : html`<p class="muted">No command mismatch recorded.</p>`}
      <h4>Decision differences</h4>
      ${decisions.length ? html`<ul class="engine-records">${decisions.slice(0, 20).map((r) => html`<li class="engine-record">
        <span class="tone-warning">${icon("warning", 18)}</span>
        <span><strong>${this.name(r.subject.split("#")[0])}</strong> · ${r.action} (${r.kind}, ${r.status}) · only the ${r.only === "app" ? "App" : "integration"}
          <span class="block small muted">${fmt(r.at)} · ${r.explanation}</span></span></li>`)}</ul>` : html`<p class="muted">No decision difference recorded.</p>`}`;
  }

  private history(d: LightingStatus): TemplateResult {
    const fmt = haTime();
    const items = d.integration_status?.history ?? [];
    return html`<section class="card wide" data-testid="lighting-history"><header class="card-head">${icon("history")}<h3>Ownership history</h3></header>
      ${items.length ? html`<div class="table-wrap"><table class="plain-table"><thead><tr><th scope="col">When</th><th scope="col">Controls the lights</th><th scope="col">Why</th><th scope="col">Epoch</th></tr></thead>
        <tbody>${items.map((h) => html`<tr><td>${fmt(h.at)}</td><td>${ownerLabel(h.owner).label}</td><td>${REASONS[h.reason] ?? h.reason}${h.detail ? html`<span class="block small muted">${h.detail}</span>` : nothing}</td><td>${h.epoch}</td></tr>`)}</tbody></table></div>`
        : html`<p class="muted">The integration has always controlled the lights.</p>`}
      ${d.backups.length ? html`<p class="small muted">Backups in the App (configuration and state at each change of control): ${d.backups.slice(0, 5).map((b) => b.id).join(", ")}${d.backups.length > 5 ? " …" : ""}</p>` : nothing}
    </section>`;
  }

  private dialogTemplate(d: LightingStatus): TemplateResult | typeof nothing {
    if (!this.dialog) return nothing;
    const kind = this.dialog;
    const c = d.comparison;
    const title = { handover: "Hand lighting over to the App", release: "Return control to the integration",
      return: "Force the integration to take lighting back" }[kind];
    return html`<dialog class="sheet lighting-dialog" aria-labelledby="lighting-dialog-title" data-testid="lighting-dialog" @close=${() => (this.dialog = null)}>
      <div class="sheet-inner">
        <header class="sheet-head"><h2 id="lighting-dialog-title">${title}</h2>
          <button type="button" class="icon-btn" aria-label="Close" @click=${() => this.close()}>${icon("close")}</button></header>
        <div class="sheet-body">
          ${this.error ? problemNotice(this.error) : nothing}
          ${kind === "handover" ? html`
            <p>The App's lighting controller will command every light, switch, test, supervisor correction, possible-occupancy light and Priority Input, through the integration's fenced executor. The integration's controller stops and its lighting editors become read-only. The lighting configuration moves to the App; the integration keeps a mirror for a return of control.</p>
            <ul class="plain-list small">
              <li>${duration(c.seconds)} compared; ${c.commands.matched} commands matched; ${c.commands.mismatches} mismatches; ${c.decisions.differences} decision differences.</li>
              <li>The App starts from an exact copy of the integration's configuration and state: manual overrides are kept, rooms keep their Automatic or Observing choice, and nothing is switched because of the handover.</li>
              <li>You can return control at any time here, or with the service <code>smart_occupancy.lighting_return_to_integration</code>. If the App stops answering, the integration takes lighting back by itself within about 30 seconds.</li>
            </ul>
            <label class="confirm-row"><input type="checkbox" data-testid="lighting-understood" .checked=${this.understood} @change=${(e: Event) => (this.understood = (e.target as HTMLInputElement).checked)} />
              <span>I have reviewed the comparison and want the App to control lighting.</span></label>`
          : kind === "release" ? html`<p>The App stops at once (nothing more is sent), hands its final configuration and state to the integration, and the integration's controller starts from them. Nothing is switched because of the change; manual overrides are kept. The App returns to shadow mode and does not take control back by itself.</p>`
          : html`<p>The integration takes lighting back from its mirror of the App's last published configuration and state, even if the App cannot hand them over. Use this when the App is not working as it should.</p>`}
        </div>
        <footer class="sheet-foot">
          <button type="button" class="btn" @click=${() => this.close()}>Cancel</button>
          <button type="button" class=${kind === "handover" ? "btn btn-primary" : "btn btn-danger"} data-testid="lighting-confirm" ?disabled=${this.busy || (kind === "handover" && !this.understood)}
            @click=${() => void this.act(kind)}>${icon(kind === "handover" ? "swap" : "undo", 20)}${this.busy ? "Working…" : kind === "handover" ? "Hand over" : "Return control"}</button>
        </footer>
      </div>
    </dialog>`;
  }

  protected render(): unknown {
    const d = this.status;
    const header = pageHeader("Lighting control", "Which controller commands the lights, and how closely the App's follows the integration's.",
      html`<div class="page-actions"><a class="btn" href="#/lighting">${icon("arrow-left", 20)}Lighting</a></div>`);
    if (!d) return html`${header}${this.error ? problemNotice(this.error) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`}`;
    return html`${header}
      ${d.notices.map((n) => html`<p class="notice notice-warning">${icon("warning", 20)}<span>${n.text}</span></p>`)}
      ${this.copy ? html`<p class="notice ${this.copy.identical ? "" : "notice-warning"}" role="status" data-testid="lighting-copy">${icon(this.copy.identical ? "ok" : "warning", 20)}<span>${this.copy.identical
        ? "Handover complete: the App's configuration is identical to the integration's."
        : `Handover complete, but these configuration parts differ: ${this.copy.differences.join(", ")}`}</span></p>` : nothing}
      ${!this.canEdit ? html`<p class="notice" role="status">${icon("info", 20)}<span>Only Home Assistant administrators can hand over or return control.</span></p>` : nothing}
      <div class="grid engine-grid">
        ${this.hero(d)}
        ${this.readiness(d)}
        ${this.facts(d)}
        ${this.comparison(d)}
        ${this.history(d)}
      </div>
      ${this.dialogTemplate(d)}`;
  }
}

customElements.define("sl-lighting-control", SlLightingControl);
