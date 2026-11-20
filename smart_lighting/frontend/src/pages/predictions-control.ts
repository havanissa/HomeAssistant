// Predictions → Control (Phase A5): who produces the forecasts, the shadow comparison, the reviewed handover
// (which moves the settings and every learned day), the rollback and the learned-data export. Forecasts are
// observation only on both sides: no device is ever commanded because of them.
import "../predictions/predictions.css";

import { html, nothing, type TemplateResult } from "lit";

import { download } from "../rooms/model";
import { icon } from "../icons";
import { chip, kv, pageHeader } from "../ui";
import { haTime } from "../presence/components";
import { duration } from "../presence/model";
import { handover, ownerLabel, releaseControl, type CompareRecord, type CopyCheck, type PredictionsStatus } from "../predictions/api";
import { confidenceChip, PredictionsElement, probabilityText, problemNotice, statusChip } from "../predictions/components";

const REFRESH_MS = 5000;
const REASONS: Record<string, string> = {
  handover: "Reviewed handover", released: "Returned by the App", returned_by_service: "Returned (service or button)",
  lease_expired: "The App stopped answering", app_restarted: "The App restarted", not_resumed: "The App did not come back after a restart",
  resumed: "The same App resumed after a restart", revoked: "The App could not resume",
};

export class SlPredictionsControl extends PredictionsElement {
  static properties = { ...PredictionsElement.properties, dialog: { state: true }, understood: { state: true }, copy: { state: true } };
  declare dialog: "handover" | "release" | "return" | null;
  declare understood: boolean;
  declare copy: CopyCheck | null;
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
    this.querySelector<HTMLDialogElement>("dialog.pr-dialog")?.showModal();
  }

  private close(): void {
    this.querySelector<HTMLDialogElement>("dialog.pr-dialog")?.close();
    this.dialog = null;
  }

  private async act(kind: "handover" | "release" | "return"): Promise<void> {
    const ok = await this.run(async () => {
      if (kind === "handover") this.copy = (await handover()).copy;
      else await releaseControl(kind);
    });
    if (ok) this.close();
  }

  private hero(d: PredictionsStatus): TemplateResult {
    const owner = ownerLabel(d.acknowledged_owner);
    const fmt = haTime();
    const integ = d.integration;
    return html`<section class="card wide engine-hero engine-${owner.app ? "app" : "integration"}" data-testid="predictions-owner">
      <div class="engine-hero-main">
        <span class="engine-hero-icon">${icon(owner.app ? "crystal-ball" : "home-assistant", 36)}</span>
        <div>
          <p class="muted small">Produces the forecasts now (acknowledged by the integration)</p>
          <h3 class="engine-owner" data-owner=${d.acknowledged_owner ?? "unknown"}>${owner.label}</h3>
          <p class="small">${chip(d.mode === "owner" ? "ok" : d.mode === "shadow" ? "info" : "warning",
            d.mode === "owner" ? "The App produces the forecasts" : d.mode === "shadow" ? "Shadow: the App compares, publishes nothing" : d.mode)}
            ${integ.epoch !== null ? html`<span class="muted"> · epoch ${integ.epoch}</span>` : nothing}</p>
          ${owner.app ? html`<p class="small" data-testid="predictions-freshness">Lease until ${fmt(d.lease_until ?? integ.lease_until)} · ${d.days_learned} day(s) learned in the App · ${d.history_calls} history reads</p>` : nothing}
          <p class="small muted">Settings and learned days: ${owner.app ? "stored in the App (published to the integration as the rollback copy)" : "stored in the integration"}. Forecasts command nothing on either side.</p>
        </div>
      </div>
      <div class="engine-hero-actions">
        ${owner.app
          ? html`<button type="button" class="btn btn-danger btn-large" data-testid="predictions-release" ?disabled=${this.busy || !this.canEdit} @click=${() => void this.open("release")}>${icon("undo", 20)}Return the forecasts to the integration</button>`
          : html`<button type="button" class="btn btn-primary" data-testid="predictions-handover" ?disabled=${this.busy || !d.readiness.ready || !this.canEdit} @click=${() => void this.open("handover")}>${icon("swap", 20)}Review handover to the App…</button>
             <span class="small muted">${d.readiness.ready ? "Every readiness check passed." : "Available when every readiness check passes."}</span>`}
        <button type="button" class="btn" data-testid="predictions-return" ?disabled=${this.busy || !this.canEdit || !owner.app && d.mode !== "owner"} @click=${() => void this.open("return")} title="Also works when the App lost its connection">${icon("home-assistant", 18)}Force return</button>
        <button type="button" class="btn" data-testid="predictions-export" @click=${() => download("api/predictions/export")}>${icon("download", 18)}Export learned data</button>
      </div>
    </section>`;
  }

  private readiness(d: PredictionsStatus): TemplateResult {
    const t = d.readiness.thresholds;
    const app = ownerLabel(d.acknowledged_owner).app;
    return html`<section class="card" data-testid="predictions-readiness"><header class="card-head">${icon("ok")}<h3>Handover readiness</h3>
        ${app ? chip("neutral", "Handed over") : d.readiness.ready ? chip("ok", "Ready") : chip("warning", "Not ready")}</header>
      <p class="muted small">A handover needs ${duration(t.min_shadow_seconds)} of comparison and ${t.min_matches} room forecasts that agreed. Disagreements are a warning to review, not a blocker.</p>
      <ul class="checklist">${d.readiness.checks.map((c) => html`<li class="check check-${c.status}" data-check=${c.key}>
        <span class="tone-${c.status}">${icon(c.status === "ok" ? "ok" : c.status === "warning" ? "warning" : "error", 18)}</span>
        <span><strong>${c.title}</strong><span class="block small muted">${c.detail}</span></span></li>`)}</ul></section>`;
  }

  private facts(d: PredictionsStatus): TemplateResult {
    const integ = d.integration;
    const sameCode = !!d.fingerprint && integ.fingerprint === d.fingerprint;
    const sameSettings = !!d.settings_revision && d.settings_revision === d.integration_settings_revision;
    const sameDays = !!d.learned_revision && d.learned_revision === integ.learned_revision;
    const rejections = Object.entries(integ.rejections ?? {});
    return html`<section class="card" data-testid="predictions-versions"><header class="card-head">${icon("shield-check")}<h3>Versions and bridge</h3></header>
      ${kv([
        ["Bridge", chip(d.bridge === "connected" ? "ok" : d.bridge === "subscribing" ? "warning" : "error", d.bridge)],
        d.bridge_error ? ["Detail", d.bridge_error] : null,
        ["App", d.app_version],
        ["Integration", integ.integration_version ?? "—"],
        ["Forecast code", sameCode ? chip("ok", "Identical") : chip("error", "Different")],
        ["Settings", sameSettings ? chip("ok", `In sync (${d.settings_revision})`) : chip("warning", `App ${d.settings_revision ?? "—"} / integration ${d.integration_settings_revision ?? "—"}`)],
        ["Learned days", html`${sameDays ? chip("ok", "Identical") : chip("warning", "Different")} <span class="small muted">App ${d.days_learned}, integration ${integ.days_learned ?? "—"}</span>`],
        ["Learned revision", html`<span class="small">${d.learned_revision ?? "—"} / ${integ.learned_revision ?? "—"}</span>`],
        ["Recorder", integ.recorder_available === null ? "—" : integ.recorder_available ? `available${integ.recorder_oldest ? ` · oldest ${haTime(false)(integ.recorder_oldest)}` : ""}` : "not available"],
        ["Refused App messages", rejections.length ? rejections.map(([k, n]) => `${k} ×${n}`).join(", ") : "none"],
        d.last_error ? ["Last error", html`<span class="tone-error">${d.last_error.code}: ${d.last_error.detail}</span>`] : null,
      ])}</section>`;
  }

  private side(s: CompareRecord["integration"]): TemplateResult {
    return html`${statusChip(s.status)} <span class="pr-muted-num">${probabilityText(s.probability)}</span> ${s.predicted === null ? html`<span class="muted small">no prediction</span>` : s.predicted ? "occupied" : "vacant"} ${confidenceChip(s.confidence)}`;
  }

  private differs(r: CompareRecord): boolean {
    const a = r.integration, b = r.app;
    const pa = a.probability, pb = b.probability;
    const close = pa === pb || (pa !== null && pb !== null && Math.abs(pa - pb) <= (this.status?.comparison.tolerance_points ?? 5));
    return !(a.status === b.status && a.predicted === b.predicted && close);
  }

  private comparison(d: PredictionsStatus): TemplateResult {
    const c = d.comparison;
    const fmt = haTime();
    const rooms = Object.values(c.rooms).sort((a, b) => this.roomName(a.area_id).localeCompare(this.roomName(b.area_id)));
    return html`<section class="card wide" data-testid="predictions-comparison"><header class="card-head">${icon("swap")}<h3>Shadow comparison</h3>
        ${c.running ? chip("ok", "Running") : chip("neutral", "Not running")}</header>
      <p class="muted small">Both sides run the same forecast code on the same learned days. Each time both publish a view within ${c.pair_seconds} s, every room is compared: it agrees when status and prediction match and the probabilities are within ${c.tolerance_points} points. The App's forecasts are never published while it compares.</p>
      <div class="stat-tiles">
        <div class="stat"><span class="stat-value">${duration(c.seconds)}</span><span class="stat-label">compared</span></div>
        <div class="stat"><span class="stat-value">${c.pairs}</span><span class="stat-label">view pairs</span></div>
        <div class="stat"><span class="stat-value" data-testid="predictions-agreements">${c.agreements}</span><span class="stat-label">room forecasts agreed</span></div>
        <div class="stat"><span class="stat-value ${c.disagreements ? "tone-warning" : ""}" data-testid="predictions-disagreements">${c.disagreements}</span><span class="stat-label">disagreements</span></div>
      </div>
      <p class="small muted">Last agreement ${fmt(c.last_agreement_at)} · last disagreement ${fmt(c.last_disagreement_at)}</p>
      <h4>Rooms, latest pair</h4>
      ${rooms.length ? html`<div class="table-wrap"><table class="plain-table pr-compare"><thead><tr><th scope="col">Room</th><th scope="col">Integration</th><th scope="col">App (shadow)</th><th scope="col">When</th></tr></thead>
        <tbody>${rooms.map((r) => html`<tr data-area=${r.area_id} class=${this.differs(r) ? "differs" : ""}><td><a href="#/predictions/rooms/${r.area_id}">${this.roomName(r.area_id)}</a></td>
          <td>${this.side(r.integration)}</td><td class=${this.differs(r) ? "differs" : ""}>${this.side(r.app)}</td><td class="small muted">${fmt(r.at)}</td></tr>`)}</tbody></table></div>`
        : html`<p class="muted">No pair compared yet.</p>`}
      <h4>Disagreements</h4>
      ${c.log.length ? html`<ul class="engine-records">${c.log.slice(0, 30).map((r) => html`<li class="engine-record pr-log-entry record-disagreement">
        <span class="pr-log-head"><span class="tone-warning">${icon("warning", 18)}</span><strong>${this.roomName(r.area_id)}</strong><span class="small muted">${fmt(r.at)}</span></span>
        <span class="block small">Integration: ${this.side(r.integration)}</span>
        <span class="block small">App: ${this.side(r.app)}</span></li>`)}</ul>` : html`<p class="muted">No disagreement recorded.</p>`}
    </section>`;
  }

  private transfer(d: PredictionsStatus): TemplateResult {
    const copy = d.last_copy;
    const fmt = haTime();
    return html`<section class="card" data-testid="predictions-transfer"><header class="card-head">${icon("export")}<h3>Learned data transfer</h3></header>
      <p class="small">A handover moves the settings and <strong>every learned day</strong> to the App, which continues learning from them. The App publishes its learned data back to the integration after each change, so a return of control (from here, or the service <code>smart_occupancy.forecast_return_to_integration</code>) keeps every day learned meanwhile. Nothing is switched by any of this.</p>
      ${copy ? html`<h4>Last copy check</h4>${kv([
        ["Result", copy.identical ? chip("ok", "Identical on both sides") : chip("warning", `Differs: ${copy.differences.join(", ")}`)],
        ["Days", `${copy.source_days} exported → ${copy.destination_days} loaded`],
        ["Revision", html`<span class="small">${copy.source_revision ?? "—"} → ${copy.destination_revision ?? "—"}</span>`],
      ])}` : html`<p class="small muted">No handover copy checked yet in this App process.</p>`}
      ${d.ownership_log.length ? html`<h4>This App's log</h4><ul class="plain-list small">${d.ownership_log.slice(0, 8).map((e) => html`<li>${fmt(e.at)} · ${e.event}${e.detail ? ` · ${String(e.detail)}` : ""}</li>`)}</ul>` : nothing}
    </section>`;
  }

  private history(d: PredictionsStatus): TemplateResult {
    const fmt = haTime();
    const items = d.integration.history ?? [];
    return html`<section class="card wide" data-testid="predictions-history"><header class="card-head">${icon("history")}<h3>Ownership history</h3></header>
      ${items.length ? html`<div class="table-wrap"><table class="plain-table"><thead><tr><th scope="col">When</th><th scope="col">Produces the forecasts</th><th scope="col">Why</th><th scope="col">Epoch</th></tr></thead>
        <tbody>${items.map((h) => html`<tr><td>${fmt(h.at)}</td><td>${ownerLabel(h.owner).label}</td><td>${REASONS[h.reason] ?? h.reason}${h.detail ? html`<span class="block small muted">${h.detail}</span>` : nothing}</td><td>${h.epoch}</td></tr>`)}</tbody></table></div>`
        : html`<p class="muted">The integration has always produced the forecasts.</p>`}
      ${d.backups.length ? html`<p class="small muted">Backups in the App (learned data at each change of control and import): ${d.backups.slice(0, 5).map((b) => b.id).join(", ")}${d.backups.length > 5 ? " …" : ""}</p>` : nothing}
    </section>`;
  }

  private dialogTemplate(d: PredictionsStatus): TemplateResult | typeof nothing {
    if (!this.dialog) return nothing;
    const kind = this.dialog;
    const c = d.comparison;
    const title = { handover: "Hand the forecasts over to the App", release: "Return the forecasts to the integration",
      return: "Force the integration to produce the forecasts again" }[kind];
    return html`<dialog class="sheet pr-dialog" aria-labelledby="pr-control-dialog-title" data-testid="predictions-dialog" @close=${() => (this.dialog = null)}>
      <div class="sheet-inner">
        <header class="sheet-head"><h2 id="pr-control-dialog-title">${title}</h2>
          <button type="button" class="icon-btn" aria-label="Close" @click=${() => this.close()}>${icon("close")}</button></header>
        <div class="sheet-body">
          ${this.error ? problemNotice(this.error) : nothing}
          ${kind === "handover" ? html`
            <p>The App's forecast runtime will produce every room's forecast, the predicted next room and the learning status, and publish them to the integration, whose entities and cards then show the App's view. The integration's forecast manager goes dormant and its forecast settings become read-only.</p>
            <ul class="plain-list small">
              <li>${duration(c.seconds)} compared; ${c.agreements} room forecasts agreed; ${c.disagreements} disagreements.</li>
              <li>The settings and every learned day (${d.integration.days_learned ?? "—"}) move to the App, which checks the copy against the same model code and continues learning from it.</li>
              <li>No device is ever commanded by forecasts, before or after the handover: nothing is switched because of it.</li>
              <li>You can return the forecasts at any time here, or with the service <code>smart_occupancy.forecast_return_to_integration</code>; every day learned meanwhile comes back with them. If the App stops answering, the integration produces the forecasts again by itself.</li>
            </ul>
            <label class="confirm-row"><input type="checkbox" data-testid="predictions-understood" .checked=${this.understood} @change=${(e: Event) => (this.understood = (e.target as HTMLInputElement).checked)} />
              <span>I have reviewed the comparison and want the App to produce the forecasts.</span></label>`
          : kind === "release" ? html`<p>The App stops producing forecasts at once, hands its final learned data and settings to the integration, and the integration's forecast manager starts from them (no learned day is lost). The App returns to shadow mode and does not take the forecasts back by itself. Nothing is switched: forecasts command nothing.</p>`
          : html`<p>The integration produces the forecasts again from its copy of the App's last published learned data, even if the App cannot hand them over. Use this when the App is not working as it should. Nothing is switched: forecasts command nothing.</p>`}
        </div>
        <footer class="sheet-foot">
          <button type="button" class="btn" @click=${() => this.close()}>Cancel</button>
          <button type="button" class=${kind === "handover" ? "btn btn-primary" : "btn btn-danger"} data-testid="predictions-confirm" ?disabled=${this.busy || (kind === "handover" && !this.understood)}
            @click=${() => void this.act(kind)}>${icon(kind === "handover" ? "swap" : "undo", 20)}${this.busy ? "Working…" : kind === "handover" ? "Hand over" : "Return the forecasts"}</button>
        </footer>
      </div>
    </dialog>`;
  }

  protected render(): unknown {
    const d = this.status;
    const header = pageHeader("Predictions control", "Who produces the occupancy forecasts, and how closely the App's follow the integration's.",
      html`<div class="page-actions"><a class="btn" href="#/predictions">${icon("arrow-left", 20)}Predictions</a></div>`);
    if (!d) return html`${header}${this.error ? problemNotice(this.error) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading…</span></div>`}`;
    return html`${header}
      ${d.notices.map((n) => html`<p class="notice notice-warning">${icon("warning", 20)}<span>${n.text}</span></p>`)}
      ${this.copy ? html`<p class="notice ${this.copy.identical ? "" : "notice-warning"}" role="status" data-testid="predictions-copy">${icon(this.copy.identical ? "ok" : "warning", 20)}<span>${this.copy.identical
        ? `Handover complete: the App loaded ${this.copy.destination_days} of ${this.copy.source_days} learned day(s), identical to the integration's.`
        : `Handover complete, but these parts differ: ${this.copy.differences.join(", ")} (${this.copy.destination_days} of ${this.copy.source_days} day(s) loaded).`}</span></p>` : nothing}
      ${!this.canEdit ? html`<p class="notice" role="status">${icon("info", 20)}<span>Only Home Assistant administrators can hand over or return the forecasts.</span></p>` : nothing}
      ${this.error && !this.dialog ? problemNotice(this.error) : nothing}
      <div class="grid engine-grid">
        ${this.hero(d)}
        ${this.readiness(d)}
        ${this.facts(d)}
        ${this.comparison(d)}
        ${this.transfer(d)}
        ${this.history(d)}
      </div>
      ${this.dialogTemplate(d)}`;
  }
}

customElements.define("sl-predictions-control", SlPredictionsControl);
