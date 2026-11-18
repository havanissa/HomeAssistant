// Curtains → Control (Phase A5): which controller commands the covers, the shadow comparison, the reviewed
// handover, the rollback and the configuration export. The owner shown is the one the integration acknowledges.
// Whoever controls the curtains, a cover is only ever moved through the integration's fenced executor.
import "../curtains/curtains.css";
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { apiUrl } from "../store";
import { chip, kv, pageHeader } from "../ui";
import { haTime } from "../presence/components";
import { duration } from "../presence/model";
import { handover, ownerLabel, releaseControl, type CoverCommandBrief, type CurtainsStatus } from "../curtains/api";
import { CurtainsElement, fmt, problemNotice, STATUS_TONE } from "../curtains/components";

const REFRESH_MS = 5000;
const PURPOSES: Record<string, string> = {
  automatic: "Automatic curtains", manual: "Confirmed presets", manual_confirmed: "Confirmed presets", retry: "Retries",
  resume: "Resumed windows", schedule: "Schedule transitions",
};
const REASONS: Record<string, string> = {
  handover: "Reviewed handover", released: "Returned by the App", returned_by_service: "Returned (service or button)",
  lease_expired: "The App stopped answering", app_restarted: "The App restarted", not_resumed: "The App did not come back after a restart",
  resumed: "The same App resumed after a restart", revoked: "The App could not resume",
};

export class SlCurtainsControl extends CurtainsElement {
  static properties = { ...CurtainsElement.properties, dialog: { state: true }, understood: { state: true }, copy: { state: true } };
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
    this.querySelector<HTMLDialogElement>("dialog.cu-control-dialog")?.showModal();
  }

  private close(): void {
    this.querySelector<HTMLDialogElement>("dialog.cu-control-dialog")?.close();
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

  private windowName(id: string | null): string {
    return id ?? "—";
  }

  private command(c: CoverCommandBrief): TemplateResult {
    return html`<span>${c.service} <strong>${c.entity_id ?? "—"}</strong>${c.position !== null && c.position !== undefined ? html` <span class="muted">position ${c.position} %</span>` : nothing}
      ${c.error ? html` <span class="tone-error">(${c.error})</span>` : nothing}</span>`;
  }

  private hero(d: CurtainsStatus): TemplateResult {
    const owner = ownerLabel(d.acknowledged_owner);
    const t = haTime();
    const integ = d.integration;
    const moving = d.moving.length ? d.moving : integ.moving ?? [];
    return html`<section class="card wide engine-hero engine-${owner.app ? "app" : "integration"}" data-testid="curtains-owner">
      <div class="engine-hero-main">
        <span class="engine-hero-icon">${icon(owner.app ? "curtains" : "home-assistant", 36)}</span>
        <div>
          <p class="muted small">Commands the covers now (acknowledged by the integration)</p>
          <h3 class="engine-owner" data-owner=${d.acknowledged_owner ?? "unknown"}>${owner.label}</h3>
          <p class="small">${chip(d.mode === "owner" ? "ok" : d.mode === "shadow" ? "info" : "warning",
            d.mode === "owner" ? "The App controls the curtains" : d.mode === "shadow" ? "Shadow: the App compares, sends nothing" : d.mode)}
            ${integ.epoch !== null && integ.epoch !== undefined ? html`<span class="muted"> · epoch ${integ.epoch} · integration manager ${integ.manager ?? "—"}</span>` : nothing}</p>
          ${owner.app ? html`<p class="small" data-testid="curtains-freshness">Lease until ${t(d.lease_until ?? integ.lease_until)} ·
            ${integ.executed ?? 0} cover command(s) executed for the App, ${integ.execute_errors ?? 0} failed</p>` : nothing}
          <p class="small muted">Configuration: ${owner.app ? "stored in the App (mirrored to the integration for a rollback)" : "stored in the integration"}.
            Whoever controls the curtains, the integration's fenced executor is the only path to a cover.</p>
          ${moving.length ? html`<p class="small tone-warning" data-testid="curtains-moving">${icon("warning", 16)} Moving now: ${moving.join(", ")}. A change of control is refused while a cover is moving.</p>` : nothing}
        </div>
      </div>
      <div class="engine-hero-actions">
        ${owner.app
          ? html`<button type="button" class="btn btn-danger btn-large" data-testid="curtains-release" ?disabled=${this.busy || !this.canEdit} @click=${() => void this.open("release")}>${icon("undo", 20)}Return control to the integration</button>`
          : html`<button type="button" class="btn btn-primary" data-testid="curtains-handover" ?disabled=${this.busy || !d.readiness.ready || !this.canEdit} @click=${() => void this.open("handover")}>${icon("swap", 20)}Review handover to the App…</button>
             <span class="small muted">${d.readiness.ready ? "Every readiness check passed." : "Available when every readiness check passes."}</span>`}
        <button type="button" class="btn" data-testid="curtains-return" ?disabled=${this.busy || !this.canEdit || !owner.app && d.mode !== "owner"} @click=${() => void this.open("return")} title="Also works when the App lost its connection">${icon("home-assistant", 18)}Force return</button>
        <a class="btn" href=${apiUrl("api/curtains/export")} download data-testid="curtains-export">${icon("download", 18)}Export configuration and state</a>
      </div>
    </section>`;
  }

  private readiness(d: CurtainsStatus): TemplateResult {
    const t = d.readiness.thresholds;
    const app = ownerLabel(d.acknowledged_owner).app;
    return html`<section class="card" data-testid="curtains-readiness"><header class="card-head">${icon("ok")}<h3>Handover readiness</h3>
        ${app ? chip("neutral", "Handed over") : d.readiness.ready ? chip("ok", "Ready") : chip("warning", "Not ready")}</header>
      <p class="muted small">A handover needs ${duration(t.min_shadow_seconds)} of comparison, ${t.min_decisions} matching window decisions, no cover command mismatch and no cover moving.</p>
      <ul class="checklist">${d.readiness.checks.map((c) => html`<li class="check check-${c.status}" data-check=${c.key}>
        <span class="tone-${c.status}">${icon(c.status === "ok" ? "ok" : c.status === "warning" ? "warning" : "error", 18)}</span>
        <span><strong>${c.title}</strong><span class="block small muted">${c.detail}</span></span></li>`)}</ul></section>`;
  }

  private facts(d: CurtainsStatus): TemplateResult {
    const integ = d.integration;
    const same = d.fingerprint && integ.fingerprint === d.fingerprint;
    const rejections = Object.entries(integ.rejections ?? {});
    return html`<section class="card" data-testid="curtains-versions"><header class="card-head">${icon("shield-check")}<h3>Versions and bridge</h3></header>
      ${kv([
        ["Bridge", chip(d.bridge === "connected" ? "ok" : d.bridge === "subscribing" ? "warning" : "error", d.bridge)],
        d.bridge_error ? ["Detail", d.bridge_error] : null,
        ["App", d.app_version],
        ["Integration", integ.integration_version ?? "—"],
        ["Curtain code", same ? chip("ok", "Identical") : chip("error", "Different")],
        ["Occupancy engine", d.core_fingerprint && integ.core_fingerprint === d.core_fingerprint ? chip("ok", "Identical") : chip("warning", "Different or unknown")],
        ["Configuration", d.config_revision === d.integration_revision ? chip("ok", `In sync (${d.config_revision ?? "—"})`) : chip("warning", `${d.config_revision ?? "—"} / ${d.integration_revision ?? "—"}`)],
        ["Windows (integration)", integ.windows ?? "—"],
        ["Refused App messages", rejections.length ? rejections.map(([k, n]) => `${k} ×${n}`).join(", ") : "none"],
        ["Owner changed", haTime()(integ.changed_at)],
        d.last_error ? ["Last error", `${d.last_error.code}: ${d.last_error.detail} (${fmt(d.last_error.at)})`] : null,
      ])}</section>`;
  }

  private comparison(d: CurtainsStatus): TemplateResult {
    const c = d.comparison;
    const purposes = Object.entries(c.commands.by_purpose);
    return html`<section class="card wide" data-testid="curtains-comparison"><header class="card-head">${icon("swap")}<h3>Shadow comparison</h3>
        ${c.running ? chip("ok", "Running") : chip("neutral", "Not running")}</header>
      <p class="muted small">Both controllers run the same curtain code on the same events. A cover command matches when the same service, cover and position appear on both sides within ${c.window_seconds} s; a window decision matches within ${c.decision_window_seconds} s. The App's commands are never sent.</p>
      <div class="stat-tiles">
        <div class="stat"><span class="stat-value">${duration(c.seconds)}</span><span class="stat-label">compared</span></div>
        <div class="stat"><span class="stat-value" data-testid="curtains-matched">${c.commands.matched}</span><span class="stat-label">matching cover commands</span></div>
        <div class="stat"><span class="stat-value ${c.commands.mismatches ? "tone-error" : ""}" data-testid="curtains-mismatches">${c.commands.mismatches}</span><span class="stat-label">command mismatches</span></div>
        <div class="stat"><span class="stat-value" data-testid="curtains-decisions">${c.decisions.matched}</span><span class="stat-label">matching decisions</span></div>
        <div class="stat"><span class="stat-value ${c.decisions.differences ? "tone-warning" : ""}">${c.decisions.differences}</span><span class="stat-label">decision differences</span></div>
      </div>
      ${purposes.length ? html`<p class="small">${purposes.map(([p, n]) => html`${chip("ok", `${PURPOSES[p] ?? p}: ${n}`)} `)}</p>` : nothing}
      ${this.discrepancyList(d)}
    </section>`;
  }

  private discrepancyList(d: CurtainsStatus): TemplateResult {
    const t = haTime();
    const items = d.comparison.discrepancies;
    const decisions = d.comparison.decision_differences;
    return html`<h4>Cover command mismatches</h4>
      ${items.length ? html`<ul class="engine-records">${items.map((r) => html`<li class="engine-record record-disagreement">
        <span class="tone-error">${icon("error", 18)}</span>
        <span><strong>${this.windowName(r.window_id)}</strong> · ${PURPOSES[r.purpose] ?? r.purpose} · only the ${r.only === "app" ? "App" : "integration"} sent ${this.command(r.command)}
          <span class="block small muted">${t(r.at)}${r.other_side.length ? html` · the other side: ${r.other_side.map((o) => this.command(o))}` : " · the other side sent nothing for this cover"}</span></span>
      </li>`)}</ul>` : html`<p class="muted">No cover command mismatch recorded.</p>`}
      <h4>Decision differences</h4>
      ${decisions.length ? html`<ul class="engine-records">${decisions.slice(0, 20).map((r) => html`<li class="engine-record">
        <span class="tone-warning">${icon("warning", 18)}</span>
        <span><strong>${r.window ?? this.windowName(r.window_id)}</strong> · ${r.word ?? r.action ?? r.kind ?? "—"}${r.condition ? ` (${r.condition})` : ""}${r.preset_name ? ` → ${r.preset_name}` : ""} · only the ${r.only === "app" ? "App" : "integration"}
          <span class="block small muted">${t(r.at)} · ${r.summary ?? r.text ?? ""}${r.code ? ` · ${r.code}` : ""}</span></span></li>`)}</ul>` : html`<p class="muted">No decision difference recorded.</p>`}
      ${d.comparison.recent.app.length || d.comparison.recent.integration.length ? html`<h4>Recent cover commands</h4>
        <div class="cu-two">${(["integration", "app"] as const).map((side) => html`<div><p class="small muted">${side === "app" ? "The App (not sent)" : "The integration"}</p>
          ${d.comparison.recent[side].length ? html`<ul class="plain-list small">${d.comparison.recent[side].slice(0, 8).map((c) => html`<li>${this.command(c)} <span class="muted">· ${PURPOSES[c.purpose ?? ""] ?? c.purpose ?? ""}</span></li>`)}</ul>` : html`<p class="muted small">Nothing yet.</p>`}</div>`)}</div>` : nothing}`;
  }

  private history(d: CurtainsStatus): TemplateResult {
    const t = haTime();
    const items = d.integration.history ?? [];
    return html`<section class="card wide" data-testid="curtains-history"><header class="card-head">${icon("history")}<h3>Ownership history</h3></header>
      ${items.length ? html`<div class="table-wrap"><table class="plain-table"><thead><tr><th scope="col">When</th><th scope="col">Controls the curtains</th><th scope="col">Why</th><th scope="col">Epoch</th></tr></thead>
        <tbody>${items.map((h) => html`<tr><td>${t(h.at)}</td><td>${ownerLabel(h.owner).label}</td><td>${REASONS[h.reason] ?? h.reason}${h.detail ? html`<span class="block small muted">${h.detail}</span>` : nothing}</td><td>${h.epoch}</td></tr>`)}</tbody></table></div>`
        : html`<p class="muted">The integration has always controlled the curtains.</p>`}
      ${d.ownership_log.length ? html`<h4>The App's own log</h4><ul class="cu-log">${d.ownership_log.slice(-10).reverse().map((e) => html`<li><time datetime=${e.at}>${t(e.at)}</time><span>${chip(STATUS_TONE[e.event] ?? "neutral", e.event)} ${Object.entries(e).filter(([k]) => k !== "at" && k !== "event").map(([k, v]) => `${k} ${String(v)}`).join(" · ")}</span></li>`)}</ul>` : nothing}
      ${d.backups.length ? html`<p class="small muted" data-testid="curtains-backups">Backups in the App (configuration and state at each change of control): ${d.backups.slice(0, 5).map((b) => b.id).join(", ")}${d.backups.length > 5 ? " …" : ""}</p>` : nothing}
    </section>`;
  }

  private dialogTemplate(d: CurtainsStatus): TemplateResult | typeof nothing {
    if (!this.dialog) return nothing;
    const kind = this.dialog;
    const c = d.comparison;
    const title = { handover: "Hand the curtains over to the App", release: "Return control to the integration",
      return: "Force the integration to take the curtains back" }[kind];
    return html`<dialog class="sheet cu-control-dialog" aria-labelledby="cu-control-title" data-testid="curtains-dialog" @close=${() => (this.dialog = null)}>
      <div class="sheet-inner">
        <header class="sheet-head"><h2 id="cu-control-title">${title}</h2>
          <button type="button" class="icon-btn" aria-label="Close" @click=${() => this.close()}>${icon("close")}</button></header>
        <div class="sheet-body">
          ${this.error ? problemNotice(this.error) : nothing}
          ${kind === "handover" ? html`
            <p>The App's curtain controller will decide every automatic movement, confirmed preset and resumed window, and move each cover only through the integration's fenced executor. The integration's controller stops and its curtain card becomes read-only. The curtain configuration moves to the App; the integration keeps a mirror for a return of control.</p>
            <ul class="plain-list small">
              <li>${duration(c.seconds)} compared; ${c.decisions.matched} window decisions matched; ${c.commands.matched} cover commands matched; ${c.commands.mismatches} mismatches; ${c.decisions.differences} decision differences.</li>
              <li>The App starts from an exact copy of the integration's configuration and state: manual holds are kept, windows keep their Off, Observing or Automatic choice, and no cover moves because of the handover. The handover is refused while a cover is moving.</li>
              <li>You can return control at any time here, or with the service <code>smart_occupancy.curtains_return_to_integration</code>. If the App stops answering, the integration takes the curtains back by itself within about 30 seconds.</li>
            </ul>
            <label class="confirm-row"><input type="checkbox" data-testid="curtains-understood" .checked=${this.understood} @change=${(e: Event) => (this.understood = (e.target as HTMLInputElement).checked)} />
              <span>I have reviewed the comparison and want the App to control the curtains.</span></label>`
          : kind === "release" ? html`<p>The App stops at once (nothing more is sent), hands its final configuration and state to the integration, and the integration's controller starts from them. No cover moves because of the change; manual holds are kept. The App returns to shadow mode and does not take control back by itself. The return is refused while a cover is moving.</p>`
          : html`<p>The integration takes the curtains back from its mirror of the App's last published configuration and state, even if the App cannot hand them over. Use this when the App is not working as it should. The same happens with the service <code>smart_occupancy.curtains_return_to_integration</code>.</p>`}
        </div>
        <footer class="sheet-foot">
          <button type="button" class="btn" @click=${() => this.close()}>Cancel</button>
          <button type="button" class=${kind === "handover" ? "btn btn-primary" : "btn btn-danger"} data-testid="curtains-confirm" ?disabled=${this.busy || (kind === "handover" && !this.understood)}
            @click=${() => void this.act(kind)}>${icon(kind === "handover" ? "swap" : "undo", 20)}${this.busy ? "Working…" : kind === "handover" ? "Hand over" : "Return control"}</button>
        </footer>
      </div>
    </dialog>`;
  }

  protected render(): unknown {
    const d = this.status;
    const header = pageHeader("Curtain control", "Which controller commands the covers, and how closely the App's follows the integration's.",
      html`<div class="page-actions"><a class="btn" href="#/curtains">${icon("arrow-left", 20)}Curtains</a></div>`);
    if (!d) return html`${header}${this.error ? problemNotice(this.error) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`}`;
    return html`${header}
      ${d.notices.map((n) => html`<p class="notice notice-warning">${icon("warning", 20)}<span>${n.text}</span></p>`)}
      ${this.copy ? html`<p class="notice ${this.copy.identical ? "" : "notice-warning"}" role="status" data-testid="curtains-copy">${icon(this.copy.identical ? "ok" : "warning", 20)}<span>${this.copy.identical
        ? "Handover complete: the App's curtain configuration is identical to the integration's."
        : `Handover complete, but these configuration parts differ: ${this.copy.differences.join(", ")}`}</span></p>` : nothing}
      ${!this.canEdit ? html`<p class="notice" role="status">${icon("info", 20)}<span>Only Home Assistant administrators can hand over or return control.</span></p>` : nothing}
      ${this.error && !this.dialog ? problemNotice(this.error) : nothing}
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

customElements.define("sl-curtains-control", SlCurtainsControl);
