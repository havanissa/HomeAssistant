// Presence → Engine (Phase A3.2): which engine decides room occupancy, the shadow comparison, the reviewed
// handover and the rollback. The owner shown is the one the integration acknowledges (read from it), never
// only what the App asked for. No device is controlled from here.
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { store } from "../store";
import { ApiError, getRooms, postRooms } from "../rooms/model";
import { chip, kv, pageHeader, StoreElement } from "../ui";
import { haTime, stateChip } from "../presence/components";
import { duration } from "../presence/model";
import {
  eventText,
  matchedPercent,
  modeLabel,
  ownerInfo,
  reasonText,
  recordTitle,
  secondsBetween,
  type EngineRecord,
  type EngineStatus,
} from "../presence/engine";
import { apiProblem } from "./presence";

const REFRESH_MS = 5000;

export class SlPresenceEngine extends StoreElement {
  static properties = {
    data: { state: true }, records: { state: true }, error: { state: true }, busy: { state: true },
    dialog: { state: true }, actionError: { state: true }, understood: { state: true }, open: { state: true },
  };
  declare data: EngineStatus | null;
  declare records: EngineRecord[];
  declare error: { message: string; code: string } | null;
  declare busy: boolean;
  declare dialog: "handover" | "release" | null;
  declare actionError: string | null;
  declare understood: boolean;
  declare open: Set<number>;
  private timer: number | undefined;
  private seen = -1;

  constructor() {
    super();
    this.data = null;
    this.records = [];
    this.error = null;
    this.busy = false;
    this.dialog = null;
    this.actionError = null;
    this.understood = false;
    this.open = new Set();
  }

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
    this.timer = window.setInterval(() => void this.load(), REFRESH_MS);
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    window.clearInterval(this.timer);
  }

  protected willUpdate(): void {
    if (store.engineVersion !== this.seen) {
      const first = this.seen === -1;
      this.seen = store.engineVersion;
      if (!first) void this.load();
    }
  }

  async load(): Promise<void> {
    try {
      const [data, records] = await Promise.all([
        getRooms<EngineStatus>("api/engine"),
        getRooms<{ records: EngineRecord[] }>("api/engine/comparison?limit=50"),
      ]);
      this.data = data;
      this.records = records.records;
      this.error = null;
    } catch (err) {
      this.error = { message: err instanceof Error ? err.message : String(err), code: err instanceof ApiError ? err.code : "error" };
    }
  }

  private name(areaId: string): string {
    return this.data?.area_names[areaId] ?? areaId;
  }

  private entityName(entityId: string): string {
    return entityId;
  }

  private async openDialog(kind: "handover" | "release"): Promise<void> {
    this.dialog = kind;
    this.actionError = null;
    this.understood = false;
    await this.updateComplete;
    this.querySelector<HTMLDialogElement>("dialog.engine-dialog")?.showModal();
  }

  private closeDialog(): void {
    this.querySelector<HTMLDialogElement>("dialog.engine-dialog")?.close();
    this.dialog = null;
  }

  private async act(kind: "handover" | "release"): Promise<void> {
    this.busy = true;
    this.actionError = null;
    try {
      await postRooms(`api/engine/${kind}`, kind === "handover" ? { confirm: true } : {});
      this.closeDialog();
      await this.load();
    } catch (err) {
      this.actionError = err instanceof Error ? err.message : String(err);
    } finally {
      this.busy = false;
    }
  }

  // ------------------------------------------------------------- render
  private hero(d: EngineStatus): TemplateResult {
    const owner = ownerInfo(d.acknowledged_owner);
    const fmt = haTime();
    const integ = d.integration_status;
    const lastAccepted = integ?.last_accepted_at ?? null;
    const age = secondsBetween(lastAccepted, d.now);
    const since = integ?.changed_at ?? integ?.history[0]?.at ?? null;
    return html`<section class="card wide engine-hero engine-${owner.app ? "app" : "integration"}" data-testid="engine-owner">
      <div class="engine-hero-main">
        <span class="engine-hero-icon">${icon(owner.app ? "lightbulb-auto" : "home-assistant", 36)}</span>
        <div>
          <p class="muted small">Decides room occupancy now (acknowledged by the integration)</p>
          <h3 class="engine-owner" data-owner=${d.acknowledged_owner}>${owner.label}</h3>
          <p class="small">${chip(d.mode === "authoritative" ? "ok" : d.mode === "shadow" ? "info" : "warning", modeLabel(d.mode))}
            ${d.epoch !== null && d.epoch !== undefined ? html`<span class="muted"> · epoch ${d.epoch}</span>` : nothing}
            ${since ? html`<span class="muted"> · since ${fmt(since)}</span>` : nothing}</p>
          ${owner.app ? html`<p class="small" data-testid="engine-freshness">Last accepted App result ${age === null ? "—" : `${duration(Math.max(0, age))} ago`};
            lease until ${fmt(d.lease_until)}${integ && !integ.owner_connected ? html` <strong class="tone-warning">· the App is not connected</strong>` : nothing}</p>` : nothing}
        </div>
      </div>
      <div class="engine-hero-actions">
        ${owner.app
          ? html`<button type="button" class="btn btn-danger btn-large" data-testid="engine-release" ?disabled=${this.busy} @click=${() => void this.openDialog("release")}>${icon("undo", 20)}Return control to integration</button>`
          : html`<button type="button" class="btn btn-primary" data-testid="engine-handover" ?disabled=${this.busy || !d.readiness.ready} @click=${() => void this.openDialog("handover")}>${icon("swap", 20)}Review handover to the App…</button>
             <span class="small muted">${d.readiness.ready ? "Every readiness check passed." : "Available when every readiness check passes."}</span>`}
      </div>
    </section>`;
  }

  private facts(d: EngineStatus): TemplateResult {
    const v = d.versions;
    const sameEngine = v.fingerprint && v.fingerprint === v.integration_fingerprint;
    const integ = d.integration_status;
    const fmt = haTime();
    const rejections = integ ? Object.entries(integ.rejections) : [];
    return html`
      <section class="card" data-testid="engine-connection"><header class="card-head">${icon("sync")}<h3>Connection</h3></header>
        ${kv([
          ["Bridge", html`${chip(d.bridge === "connected" ? "ok" : d.bridge === "unsupported" || d.bridge === "subscribing" ? "warning" : "error", d.bridge)}`],
          d.bridge_error ? ["Detail", d.bridge_error] : null,
          ["Home Assistant", store.overview?.home_assistant.connection.state ?? "—"],
          ["App instance", d.instance],
          ["Session", d.session ?? "—"],
        ])}</section>
      <section class="card" data-testid="engine-versions"><header class="card-head">${icon("shield-check")}<h3>Versions</h3></header>
        ${kv([
          ["App", String(v.app ?? "—")],
          ["Integration", String(v.integration ?? "—")],
          ["Engine (App / integration)", `${v.core ?? "—"} / ${v.integration_core ?? "—"}`],
          ["Same engine", sameEngine ? chip("ok", "Match") : chip("error", "Different")],
          ["Bridge protocol", `${v.protocol} / ${v.integration_protocol ?? "—"}`],
        ])}</section>
      <section class="card" data-testid="engine-config"><header class="card-head">${icon("tune")}<h3>Configuration</h3></header>
        ${kv([
          ["Synchronised", d.config.in_sync ? chip("ok", "Same revision") : chip("warning", "Different")],
          ["App", d.config.app_revision ?? "—"],
          ["Integration", d.config.integration_revision ?? "—"],
          ["Source", "The integration (the App keeps no copy)"],
        ])}</section>
      <section class="card" data-testid="engine-results"><header class="card-head">${icon("history")}<h3>Published results</h3></header>
        ${kv([
          ["Last accepted from the App", fmt(integ?.last_accepted_at)],
          ["Accepted messages", integ ? String(integ.accepted_total) : "—"],
          ["Refused messages", rejections.length ? rejections.map(([k, n]) => `${k} ×${n}`).join(", ") : "none"],
          ["Self-test", d.self_test ? html`${d.self_test.passed ? chip("ok", "Passed") : chip("error", "Failed")}<span class="block small muted">${d.self_test.scenarios} scenarios, ${d.self_test.transitions} transitions</span>` : "—"],
        ])}</section>`;
  }

  private readiness(d: EngineStatus): TemplateResult {
    const t = d.readiness.thresholds;
    const appDecides = ownerInfo(d.acknowledged_owner).app;
    return html`<section class="card" data-testid="engine-readiness"><header class="card-head">${icon("ok")}<h3>Migration readiness</h3>
        ${appDecides ? chip("neutral", "Handed over") : d.readiness.ready ? chip("ok", "Ready") : chip("warning", "Not ready")}</header>
      ${appDecides ? html`<p class="small">The App already decides occupancy. These checks apply before a handover; after a return of control they must pass again.</p>` : nothing}
      <p class="muted small">A handover needs ${duration(t.min_shadow_seconds)} of uninterrupted comparison, ${t.min_transitions} matched transitions and no disagreement in that time.</p>
      <ul class="checklist">${d.readiness.checks.map((c) => html`<li class="check check-${c.status}" data-check=${c.key}>
        <span class="tone-${c.status}">${icon(c.status === "ok" ? "ok" : c.status === "warning" ? "warning" : "error", 18)}</span>
        <span><strong>${c.title}</strong><span class="block small muted">${c.detail}</span></span></li>`)}</ul></section>`;
  }

  private comparison(d: EngineStatus): TemplateResult {
    const c = d.comparison;
    const t = c.totals;
    const pct = matchedPercent(t);
    return html`<section class="card wide" data-testid="engine-comparison"><header class="card-head">${icon("swap")}<h3>Shadow comparison</h3>
        ${c.running ? chip("ok", "Running") : chip("warning", "Not running")}</header>
      <p class="muted small">Both engines evaluate the same events. A difference that settles within ${c.tolerance_s} s is a timing difference; a longer one is a disagreement.</p>
      <div class="stat-tiles">
        <div class="stat"><span class="stat-value">${duration(c.seconds)}</span><span class="stat-label">compared without interruption</span></div>
        <div class="stat"><span class="stat-value">${t.matched}/${t.transitions}</span><span class="stat-label">transitions matched${pct !== null ? ` (${pct} %)` : ""}</span></div>
        <div class="stat"><span class="stat-value">${t.timing_differences}</span><span class="stat-label">timing differences (max ${t.timing_max_ms} ms)</span></div>
        <div class="stat"><span class="stat-value ${t.disagreements ? "tone-error" : ""}">${t.disagreements}</span><span class="stat-label">disagreements</span></div>
        <div class="stat"><span class="stat-value ${t.deadline_differences ? "tone-warning" : ""}">${t.deadline_differences}</span><span class="stat-label">vacancy deadline differences</span></div>
      </div>
      ${c.rooms.length ? html`<div class="table-wrap"><table class="plain-table engine-rooms"><thead><tr><th scope="col">Room</th><th scope="col">App engine</th><th scope="col">Integration engine</th><th scope="col">Now</th><th scope="col">Matched</th><th scope="col">Disagreements</th></tr></thead>
        <tbody>${c.rooms.map((r) => html`<tr data-room=${r.area_id} class=${r.status === "disagree" ? "changed" : ""}>
          <td>${this.name(r.area_id)}</td><td>${stateChip(r.app?.state)}</td><td>${stateChip(r.integration?.state)}</td>
          <td>${chip(r.status === "agree" ? "ok" : r.status === "disagree" ? "error" : r.status === "settling" ? "pending" : "neutral",
            r.status === "agree" ? "Agree" : r.status === "disagree" ? `Differ ${r.open_for_s} s` : r.status === "settling" ? "Settling" : "Unknown")}</td>
          <td>${r.matched}/${r.transitions}</td><td>${r.disagreements}</td></tr>`)}</tbody></table></div>`
        : html`<p class="muted">No room is compared yet.</p>`}
    </section>`;
  }

  private discrepancies(): TemplateResult {
    const fmt = haTime();
    return html`<section class="card wide" data-testid="engine-discrepancies"><header class="card-head">${icon("alert-octagon")}<h3>Discrepancies</h3>
        <span class="muted small">${this.records.length} recent (bounded history)</span></header>
      ${this.records.length ? html`<ul class="engine-records">${this.records.map((r, i) => html`<li class="engine-record record-${r.kind}">
        <button type="button" class="record-head" aria-expanded=${this.open.has(i) ? "true" : "false"} @click=${() => { const next = new Set(this.open); if (next.has(i)) next.delete(i); else next.add(i); this.open = next; }}>
          <span class="tone-${r.kind === "disagreement" ? "error" : r.kind === "deadline" ? "warning" : "info"}">${icon(r.kind === "disagreement" ? "error" : r.kind === "deadline" ? "warning" : "info", 18)}</span>
          <span><strong>${this.name(r.area_id)}</strong> · ${recordTitle(r)}<span class="block small muted">${fmt(r.opened_at)} · App ${r.app?.state ?? "—"} / integration ${r.integration?.state ?? "—"}</span></span>
          ${icon(this.open.has(i) ? "chevron-down" : "chevron-right", 18)}</button>
        ${this.open.has(i) ? html`<div class="record-body">
          <p class="small">App: <strong>${r.app?.reason_code ?? "—"}</strong> ${r.app?.reason ?? ""}<br />Integration: <strong>${r.integration?.reason_code ?? "—"}</strong> ${r.integration?.reason ?? ""}</p>
          ${r.events.length ? html`<ol class="record-events">${r.events.map((e) => html`<li><span class="muted small">${fmt(e.at)}</span> ${eventText(e, (id) => this.entityName(id))}</li>`)}</ol>` : html`<p class="muted small">No input events recorded.</p>`}
        </div>` : nothing}</li>`)}</ul>` : html`<p class="muted">No discrepancy recorded.</p>`}
    </section>`;
  }

  private historyCard(d: EngineStatus): TemplateResult {
    const fmt = haTime();
    const items = d.integration_status?.history ?? [];
    return html`<section class="card wide" data-testid="engine-history"><header class="card-head">${icon("history")}<h3>Ownership history</h3></header>
      ${items.length ? html`<div class="table-wrap"><table class="plain-table"><thead><tr><th scope="col">When</th><th scope="col">Owner</th><th scope="col">Why</th><th scope="col">Epoch</th></tr></thead>
        <tbody>${items.map((h) => html`<tr><td>${fmt(h.at)}</td><td>${ownerInfo(h.owner).label}</td><td>${reasonText(h.reason)}${h.detail ? html`<span class="block small muted">${h.detail}</span>` : nothing}</td><td>${h.epoch}</td></tr>`)}</tbody></table></div>`
        : html`<p class="muted">The integration has always decided occupancy.</p>`}
    </section>`;
  }

  private dialogTemplate(d: EngineStatus): TemplateResult | typeof nothing {
    if (!this.dialog) return nothing;
    const handover = this.dialog === "handover";
    const t = d.comparison.totals;
    return html`<dialog class="sheet engine-dialog" aria-labelledby="engine-dialog-title" data-testid="engine-dialog" @close=${() => (this.dialog = null)}>
      <div class="sheet-inner">
        <header class="sheet-head"><h2 id="engine-dialog-title">${handover ? "Hand occupancy over to the App" : "Return control to the integration"}</h2>
          <button type="button" class="icon-btn" aria-label="Close" @click=${() => this.closeDialog()}>${icon("close")}</button></header>
        <div class="sheet-body">
          ${this.actionError ? html`<p class="notice notice-error" role="alert">${icon("error", 20)}<span>${this.actionError}</span></p>` : nothing}
          ${handover ? html`
            <p>The App's engine will decide room occupancy. The integration keeps publishing the same occupancy entities, and lighting, curtains and forecasts keep reading them. The integration's own engine keeps running in the background and takes over by itself if the App stops answering (within about 30 seconds).</p>
            <ul class="plain-list small">
              <li>${duration(d.comparison.seconds)} compared; ${t.matched} of ${t.transitions} transitions matched; ${t.disagreements} disagreements.</li>
              <li>The first decision is an exact copy of the current one: no room changes because of the handover.</li>
              <li>You can return control at any time from this page, or with the service <code>smart_occupancy.engine_return_to_integration</code>.</li>
            </ul>
            <label class="confirm-row"><input type="checkbox" data-testid="engine-understood" .checked=${this.understood} @change=${(e: Event) => (this.understood = (e.target as HTMLInputElement).checked)} />
              <span>I have reviewed the comparison and want the App to decide occupancy.</span></label>`
          : html`<p>The integration's engine decides room occupancy again, at once. An occupied room stays occupied; nothing is switched because of the change. The App returns to shadow mode and does not take control back by itself.</p>`}
        </div>
        <footer class="sheet-foot">
          <button type="button" class="btn" @click=${() => this.closeDialog()}>Cancel</button>
          <button type="button" class=${handover ? "btn btn-primary" : "btn btn-danger"} data-testid="engine-confirm" ?disabled=${this.busy || (handover && !this.understood)}
            @click=${() => void this.act(handover ? "handover" : "release")}>${icon(handover ? "swap" : "undo", 20)}${this.busy ? "Working…" : handover ? "Hand over" : "Return control"}</button>
        </footer>
      </div>
    </dialog>`;
  }

  protected render(): unknown {
    const d = this.data;
    const header = pageHeader("Engine migration", "Which engine decides room occupancy, and how closely the App's engine follows the integration's.",
      html`<div class="page-actions"><a class="btn" href="#/presence">${icon("arrow-left", 20)}Presence</a></div>`);
    if (!d) return html`${header}${this.error ? apiProblem(this.error.message, this.error.code) : html`<div class="card loading" role="status"><span class="spinner"></span><span>Loading the engine…</span></div>`}`;
    return html`${header}
      ${this.error ? html`<p class="notice notice-warning" role="status">${icon("warning", 20)}<span>Not refreshed: ${this.error.message}</span></p>` : nothing}
      ${d.notices.map((n) => html`<p class="notice notice-warning">${icon("warning", 20)}<span>${n.text}</span></p>`)}
      <div class="grid engine-grid">
        ${this.hero(d)}
        ${this.facts(d)}
        ${this.readiness(d)}
        ${this.comparison(d)}
        ${this.discrepancies()}
        ${this.historyCard(d)}
      </div>
      ${this.dialogTemplate(d)}`;
  }
}

customElements.define("sl-presence-engine", SlPresenceEngine);
