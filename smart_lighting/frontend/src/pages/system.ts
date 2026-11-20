// Settings → Migration & System (Phase A6): one place for every owner, the configuration authority, the
// companion connection, engine health, command authorization and synchronization - and the eight-step wizard
// that moves an existing installation from the monolithic integration to the App with the minimal companion.
// Nothing here happens on its own: every step is started and confirmed by an administrator.
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { navigate, navigatePath } from "../router";
import { apiUrl, store } from "../store";
import { ApiError, download } from "../rooms/model";
import { chip, kv, pageHeader, StoreElement } from "../ui";
import { haTime } from "../presence/components";
import {
  claimDomain,
  companionText,
  DOMAIN_TITLES,
  getSystem,
  nextStep,
  ownerText,
  pairCompanion,
  releaseDomain,
  revokeApp,
  runStep,
  STEP_TITLES,
  stepTone,
  type Check,
  type MigrationStep,
  type SystemStatus,
} from "../system/api";

const REFRESH_MS = 5000;
const DOMAINS = ["occupancy", "lighting", "curtains", "forecast"];

const STEP_HELP: Record<string, string> = {
  inspect: "Reads versions, which integration is installed, who runs each subsystem, configuration revisions, entities, device availability, runtime health, backups and blockers. Changes nothing.",
  backup: "Writes one complete file into the App's storage: configuration, lighting and curtain state, learned forecast data, overrides and holds, the floor plan, and the integration's legacy data when the companion exposes it. No credentials.",
  validate: "Checks the handover readiness of every subsystem still run by the integration, the configuration's integrity, the App's storage, the companion's compatibility, the device targets, that no two owners exist and that no cover is moving.",
  transfer_config: "Moves the integration's remaining configuration (rooms, sensors, lights, light types, mood source, occupancy timing) into the App. You see what changes before it is applied.",
  transfer_control: "Hands each subsystem over to the App, in a safe order: occupancy, lighting, curtains, predictions. Uses the reviewed handovers of the earlier phases; nothing is switched or moved by a handover.",
  activate: "Once the companion integration (1.0.0 or newer) replaced the monolithic one in Home Assistant: compares the staged configuration with the companion's copy, makes the App the authority and claims every domain.",
  verify: "Checks presence decisions, native entities, ownership, switch inputs, manual overrides, curtains, forecasts, configuration persistence and restart recovery, and reports anything degraded.",
  retire: "The companion backs the legacy data up inside Home Assistant and removes it from the active installation. Never deletes the App's backups or history. Reversible with the restore in the companion.",
};

export class SlSystem extends StoreElement {
  static properties = {
    data: { state: true }, error: { state: true }, busy: { state: true }, notice: { state: true }, result: { state: true },
    tab: { type: String }, confirmStep: { state: true }, understood: { state: true },
  };
  declare data: SystemStatus | null;
  declare error: string | null;
  declare busy: string | null;
  declare notice: string | null;
  declare result: { step: string; data: Record<string, unknown> } | null;
  declare tab: string;
  declare confirmStep: string | null;
  declare understood: boolean;
  private timer: number | undefined;
  private seen = -1;

  constructor() {
    super();
    this.data = null;
    this.error = null;
    this.busy = null;
    this.notice = null;
    this.result = null;
    this.tab = "overview";
    this.confirmStep = null;
    this.understood = false;
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
    if (store.systemVersion !== this.seen) {
      const first = this.seen === -1;
      this.seen = store.systemVersion;
      if (!first && !this.busy) void this.load();
    }
  }

  async load(): Promise<void> {
    try {
      this.data = await getSystem();
      this.error = null;
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
    }
  }

  private async act(label: string, action: () => Promise<unknown>, done?: string): Promise<boolean> {
    this.busy = label;
    this.error = null;
    this.notice = null;
    try {
      const outcome = await action();
      this.notice = done ?? null;
      if (outcome && typeof outcome === "object" && "result" in (outcome as Record<string, unknown>)) {
        this.result = { step: label, data: (outcome as { result: Record<string, unknown> }).result };
      }
      await this.load();
      return true;
    } catch (err) {
      this.error = err instanceof ApiError ? `${err.message} (${err.code})` : err instanceof Error ? err.message : String(err);
      await this.load();
      return false;
    } finally {
      this.busy = null;
    }
  }

  // ------------------------------------------------------------ overview
  private hero(d: SystemStatus): TemplateResult {
    const c = companionText(d.companion);
    const authority = d.config_authority === "app";
    const owned = DOMAINS.filter((k) => d.owners[k] === "app").length;
    const mode = d.migration?.mode ?? "unknown";
    return html`<section class="card wide engine-hero ${authority && owned === DOMAINS.length ? "engine-app" : ""}" data-testid="system-hero">
      <div class="engine-hero-main">
        <span class="engine-hero-icon">${icon(authority ? "lightbulb-auto" : "home-assistant", 36)}</span>
        <div>
          <p class="muted small">Architecture</p>
          <h3 class="engine-owner" data-authority=${d.config_authority}>
            ${mode === "companion" ? "App + companion" : mode === "legacy" ? "Monolithic integration 0.9.0 (migration pending)" : mode === "none" ? "App only (no integration installed)" : "Checking…"}
          </h3>
          <p class="small">
            ${chip(authority ? "ok" : "info", authority ? "Configuration: the App" : d.config.staged ? "Configuration: staged copy" : "Configuration: the integration")}
            ${chip(c.tone, `Companion: ${c.label}`)}
            ${chip(owned === DOMAINS.length ? "ok" : owned ? "warning" : "info", `${owned} of ${DOMAINS.length} subsystems run by the App`)}
          </p>
        </div>
      </div>
      <div class="engine-hero-actions">
        ${d.companion?.state === "present" && !d.companion.trusted
          ? html`<button type="button" class="btn btn-primary" data-testid="system-pair" ?disabled=${!!this.busy} @click=${() => void this.act("pair", pairCompanion, "Paired: the App may now claim control")}>${icon("link", 18)}Pair the App with the companion</button>`
          : nothing}
        ${mode === "legacy" || (d.companion?.legacy?.present && !d.migration?.completed_at)
          ? html`<button type="button" class="btn btn-primary" data-testid="system-open-migration" @click=${() => (this.tab = "migration")}>${icon("swap", 18)}Open the migration wizard</button>`
          : nothing}
        ${mode === "none" && !d.config.authoritative
          ? html`<button type="button" class="btn btn-primary" @click=${() => navigatePath("settings", "setup")}>${icon("plus", 18)}Set up a new installation</button>`
          : nothing}
      </div>
    </section>`;
  }

  private owners(d: SystemStatus): TemplateResult {
    const fmt = haTime();
    const rows = DOMAINS.map((key) => {
      const rt = d.runtimes[key];
      const owner = ownerText(d.owners[key]);
      const cm = rt?.companion;
      const record = d.companion?.ownership?.[key];
      return html`<tr data-domain=${key}>
        <th scope="row">${DOMAIN_TITLES[key]}</th>
        <td>${chip(owner.tone, owner.label)}</td>
        <td>${rt ? html`${rt.mode}${rt.bridge !== "disconnected" ? html` <span class="muted">· ${rt.bridge}</span>` : nothing}` : "—"}
          ${cm?.error ? html`<span class="block small tone-warning">${cm.error}</span>` : nothing}</td>
        <td>${record ? html`epoch ${record.epoch}${record.lease_valid ? "" : " · lease expired"}<span class="block small muted">${record.executed} command(s), ${record.execute_errors} failed · published ${fmt(record.published_at)}</span>` : "—"}</td>
        <td class="actions-cell">
          ${d.companion?.state === "present" && d.companion.trusted
            ? cm?.owner
              ? html`<button type="button" class="btn btn-small" ?disabled=${!!this.busy} @click=${() => void this.act(`release ${key}`, () => releaseDomain(key), `${DOMAIN_TITLES[key]}: the App released control`)}>Stop</button>`
              : html`<button type="button" class="btn btn-small btn-primary" ?disabled=${!!this.busy || !d.config.authoritative} @click=${() => void this.act(`claim ${key}`, () => claimDomain(key), `${DOMAIN_TITLES[key]}: control taken`)}>Take control</button>`
            : nothing}
        </td>
      </tr>`;
    });
    return html`<section class="card wide" data-testid="system-owners">
      <header class="card-head">${icon("shield-check")}<h3>Who runs what</h3></header>
      <p class="muted small">The owner shown is the one the companion (or the 0.9.0 integration) acknowledges. Exactly one controller may command a domain's devices; the companion executes a command only for the session that holds the domain's lease.</p>
      <div class="table-wrap"><table class="plain-table"><thead><tr><th scope="col">Subsystem</th><th scope="col">Runs it</th><th scope="col">App runtime</th><th scope="col">Lease</th><th scope="col"></th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      ${d.companion?.state === "present" ? html`<p class="small"><button type="button" class="btn btn-small btn-danger" ?disabled=${!!this.busy} @click=${() => void this.act("revoke", () => revokeApp(null), "Every lease was revoked; the App claims again only after it was paired")}>${icon("shield-alert", 16)}Revoke the App's control (emergency stop)</button>
        <span class="muted"> Nothing is switched or moved by a revoke: the published states are kept and marked as not fresh.</span></p>` : nothing}
    </section>`;
  }

  private companionCard(d: SystemStatus): TemplateResult {
    const c = d.companion;
    const fmt = haTime();
    const t = companionText(c);
    return html`<section class="card" data-testid="system-companion">
      <header class="card-head">${icon("puzzle")}<h3>Companion integration</h3>${chip(t.tone, t.label)}</header>
      ${kv([
        ["Companion version", c?.companion_version],
        ["Protocol", c?.protocol != null ? `${c.protocol}` : null],
        ["Home Assistant", c?.ha_version],
        ["Paired", c ? (c.paired ? "Yes" : "No") : null],
        ["Pairing token stored in the App", c ? (c.token_stored ? "Yes" : "No") : null],
        ["Native entities", c?.entities != null ? `${c.entities}` : null],
        ["Legacy data", c?.legacy ? (c.legacy.present ? `Present (${c.legacy.rooms} rooms, revision ${c.legacy.options_revision})` : "Retired") : null],
        c?.error ? ["Detail", c.error] : null,
      ])}
      ${c?.log?.length ? html`<details><summary class="small">Session log</summary><ul class="plain-list small">${c.log.slice(0, 10).map((e) => html`<li>${fmt(e.at)} · ${e.event}${e.reason ? ` (${e.reason})` : ""}</li>`)}</ul></details>` : nothing}
    </section>`;
  }

  private configCard(d: SystemStatus): TemplateResult {
    const cfg = d.config;
    const fmt = haTime();
    return html`<section class="card" data-testid="system-config">
      <header class="card-head">${icon("save")}<h3>Configuration authority</h3>${chip(cfg.authoritative ? "ok" : cfg.staged ? "warning" : "info", cfg.authoritative ? "The App" : cfg.staged ? "Staged copy" : "The integration")}</header>
      ${kv([
        ["Revision", cfg.revision],
        ["Updated", fmt(cfg.updated_at)],
        ["Source", cfg.source],
        ["Rooms", cfg.rooms != null ? `${cfg.enabled_rooms} enabled of ${cfg.rooms}` : null],
        ["Backups in the App", `${cfg.backups}`],
        ["Diagnostics snapshot", d.core_host ? `${d.core_host.status ?? "—"} · ${d.core_host.problems} problem(s) · ${fmt(d.core_host.snapshot_at)}` : null],
        ["Published to the companion", d.core_host?.published_at ? fmt(d.core_host.published_at) : d.core_host?.publish_error ?? "—"],
      ])}
      ${cfg.notices.map((n) => html`<p class="notice notice-warning">${n.text}</p>`)}
      <p class="small"><button type="button" class="btn btn-small" @click=${() => download("api/config/export")}>${icon("download", 16)}Export configuration</button></p>
    </section>`;
  }

  // ----------------------------------------------------------- migration
  private checks(items: Check[] | undefined): TemplateResult | typeof nothing {
    if (!items?.length) return nothing;
    return html`<ul class="checklist">${items.map((c) => html`<li class="check check-${c.status}" data-check=${c.key}>
      <span class="tone-${c.status}">${icon(c.status === "ok" ? "ok" : c.status === "info" ? "info" : c.status === "warning" ? "warning" : "error", 18)}</span>
      <span><strong>${c.title}</strong><span class="block small muted">${c.detail}</span></span></li>`)}</ul>`;
  }

  private stepResult(step: MigrationStep): TemplateResult | typeof nothing {
    const r = step.result as Record<string, unknown> | null;
    if (!r) return nothing;
    const fmt = haTime();
    switch (step.key) {
      case "inspect": {
        const v = (r.versions ?? {}) as Record<string, unknown>;
        const blockers = (r.blockers ?? []) as string[];
        const owners = (r.owners ?? {}) as Record<string, { owner: string; source: string }>;
        return html`${kv([
          ["Installed", r.mode === "companion" ? "The companion" : r.mode === "legacy" ? "The monolithic integration" : String(r.mode)],
          ["Versions", `App ${v.app ?? "—"} · integration ${v.integration ?? "—"} · companion ${v.companion ?? "—"} · Home Assistant ${v.home_assistant ?? "—"}`],
          ["Owners", DOMAINS.map((k) => `${DOMAIN_TITLES[k]}: ${owners[k]?.owner ?? "—"}`).join(" · ")],
          ["Legacy configuration", `${(r.legacy as Record<string, unknown>)?.rooms ?? 0} rooms · revision ${(r.legacy as Record<string, unknown>)?.options_revision ?? "—"}`],
          ["Unavailable devices", ((r.unavailable_devices ?? []) as string[]).join(", ") || "none"],
        ])}
        ${blockers.length ? html`<ul class="plain-list">${blockers.map((b) => html`<li class="tone-error">${b}</li>`)}</ul>` : html`<p class="small tone-ok">No blockers.</p>`}`;
      }
      case "backup":
        return html`${kv([["File", String(r.id)], ["Size", `${Math.round(Number(r.size) / 1024)} kB`], ["Created", fmt(String(r.created_at))]])}
          <p class="small">${Object.entries((r.contents ?? {}) as Record<string, boolean>).map(([k, v]) => chip(v ? "ok" : "neutral", `${k}: ${v ? "included" : "none"}`))}</p>
          <p class="small"><a class="btn btn-small" href=${apiUrl(`api/migration/backups/${String(r.id)}`)} download>${icon("download", 16)}Download</a></p>`;
      case "validate":
      case "verify":
        return html`${this.checks(r.checks as Check[])}${(r.degraded as string[] | undefined)?.length ? html`<p class="small tone-warning">Degraded: ${(r.degraded as string[]).join(", ")}</p>` : nothing}`;
      case "transfer_config": {
        const review = (r.review ?? {}) as Record<string, unknown>;
        const changes = (review.changes ?? []) as Array<{ type: string; area_id?: string }>;
        const missing = (review.missing_entities ?? []) as string[];
        return html`${kv([["Source", String(r.source)], ["Source revision", String(r.source_revision)], ["Becomes", r.authority === "staged" ? "a staged copy (the integration's options still count until the companion is active)" : "the App's authority"], ["Applied", r.applied ? "Yes" : "Not yet (review)"]])}
          <h4>Changes (${changes.length})</h4>
          ${changes.length ? html`<ul class="plain-list small">${changes.slice(0, 40).map((c) => html`<li>${c.type}${c.area_id ? ` · ${c.area_id}` : ""}</li>`)}</ul>` : html`<p class="small muted">Identical to the App's current configuration.</p>`}
          ${missing.length ? html`<p class="small tone-warning">Referenced entities that do not exist in Home Assistant: ${missing.join(", ")}</p>` : nothing}`;
      }
      case "transfer_control": {
        if (r.skipped) return html`<p class="small muted">Nothing to hand over: the companion grants control to the App.</p>`;
        const domains = (r.domains ?? {}) as Record<string, { at: string; by: string | null }>;
        return html`<ul class="plain-list small">${DOMAINS.map((k) => html`<li>${chip(domains[k] ? "ok" : "pending", DOMAIN_TITLES[k])} ${domains[k] ? html`<span class="muted">handed over ${fmt(domains[k].at)}</span>` : nothing}</li>`)}</ul>`;
      }
      case "activate": {
        const claims = (r.claims ?? {}) as Record<string, { claimed?: boolean; error?: string }>;
        return html`${r.activated ? html`<p class="small tone-ok">The App is the configuration authority.</p>` : html`<p class="small tone-warning">Not activated: the staged configuration (${String(r.staged_revision)}) differs from the companion's legacy copy (${String(r.legacy_revision)}). Import it again below.</p>`}
          <ul class="plain-list small">${Object.entries(claims).map(([k, v]) => html`<li>${chip(v.claimed ? "ok" : "warning", DOMAIN_TITLES[k] ?? k)} ${v.error ? html`<span class="muted">${v.error}</span>` : nothing}</li>`)}</ul>`;
      }
      case "retire":
        return r.already ? html`<p class="small muted">There was no legacy data left to retire.</p>` : html`${kv([["Retired", fmt(String((r.outcome as Record<string, unknown>)?.retired_at))], ["Stores removed", String(((r.outcome as Record<string, unknown>)?.stores_removed as string[] | undefined)?.length ?? 0)], ["Backup inside Home Assistant", String((r.outcome as Record<string, unknown>)?.backup)]])}`;
      default:
        return html`<pre class="json-view small">${JSON.stringify(r, null, 1)}</pre>`;
    }
  }

  private stepActions(d: SystemStatus, step: MigrationStep, current: boolean): TemplateResult | typeof nothing {
    const m = d.migration;
    if (!m || !current) return nothing;
    const busy = !!this.busy;
    const run = (body: Record<string, unknown> = {}, done?: string) => this.act(step.key, () => runStep(step.key, body), done);
    switch (step.key) {
      case "inspect":
      case "backup":
      case "validate":
      case "verify":
        return html`<button type="button" class="btn btn-primary" data-testid=${`migration-${step.key}`} ?disabled=${busy} @click=${() => void run()}>${icon("play-circle", 18)}${step.status === "pending" ? "Run" : "Run again"}</button>`;
      case "transfer_config":
        return html`<button type="button" class="btn" ?disabled=${busy} data-testid="migration-config-review" @click=${() => void run({ apply: false })}>${icon("eye", 18)}Review</button>
          <button type="button" class="btn btn-primary" ?disabled=${busy || step.status === "pending"} data-testid="migration-config-apply" @click=${() => void run({ apply: true }, "Configuration transferred")}>${icon("import", 18)}Apply the reviewed transfer</button>`;
      case "transfer_control":
        if (m.mode === "companion")
          return html`<button type="button" class="btn btn-primary" ?disabled=${busy} data-testid="migration-control-skip" @click=${() => void run({}, "Nothing to hand over: continue with Activate")}>${icon("arrow-right", 18)}Continue (nothing to hand over)</button>
            <span class="small muted block">With the companion there are no 0.9.0 bridges to hand over from: the App takes each subsystem itself once it holds the configuration, or at Activate.</span>`;
        return html`${DOMAINS.map((k) => {
          const done = m.owners[k]?.owner === "app" || m.owners[k]?.owner === "app_away";
          return html`<button type="button" class="btn ${done ? "" : "btn-primary"}" ?disabled=${busy || done} data-testid=${`migration-control-${k}`} @click=${() => void run({ domain: k }, `${DOMAIN_TITLES[k]} handed over`)}>${icon(done ? "ok" : "swap", 18)}${DOMAIN_TITLES[k]}${done ? " · done" : ""}</button>`;
        })}
          <span class="small muted block">Safe order: occupancy, lighting, curtains, predictions. Each handover uses the readiness checks of its Control page.</span>`;
      case "activate":
        return html`${m.mode !== "companion" ? html`<p class="notice notice-info">${icon("info", 18)}<span>Now update <strong>Smart Occupancy &amp; Lighting</strong> in Home Assistant (HACS) to version 1.0.0 or newer, the minimal companion, and restart Home Assistant. The App keeps running meanwhile; nothing is switched. Come back here when the companion is connected.</span></p>` : nothing}
          <button type="button" class="btn btn-primary" ?disabled=${busy || m.mode !== "companion"} data-testid="migration-activate" @click=${() => void run({}, "Companion activated")}>${icon("power", 18)}Activate</button>
          <button type="button" class="btn" ?disabled=${busy || m.mode !== "companion"} data-testid="migration-reimport" @click=${() => void run({ reimport: true }, "Configuration re-imported and activated")}>${icon("import", 18)}Re-import the configuration, then activate</button>`;
      case "retire":
        return html`<label class="confirm-row"><input type="checkbox" data-testid="migration-retire-understood" .checked=${this.understood} @change=${(e: Event) => (this.understood = (e.target as HTMLInputElement).checked)} />
            <span>I verified the installation and want the legacy data removed from the active installation (a backup stays inside Home Assistant).</span></label>
          <button type="button" class="btn btn-danger" ?disabled=${busy || !this.understood} data-testid="migration-retire" @click=${() => void run({ confirm: true }, "The monolithic integration has been retired")}>${icon("delete", 18)}Retire</button>`;
      default:
        return nothing;
    }
  }

  private migration(d: SystemStatus): TemplateResult {
    const m = d.migration;
    if (!m) return html`<section class="card wide"><p class="muted">The migration wizard is starting.</p></section>`;
    const fmt = haTime();
    const next = nextStep(m.steps);
    const completed = !!m.completed_at;
    return html`<section class="card wide" data-testid="migration-wizard">
      <header class="card-head">${icon("swap")}<h3>Migration wizard</h3>
        ${completed ? chip("ok", "Completed: the monolithic integration is retired") : m.started_at ? chip("info", `In progress since ${fmt(m.started_at)}`) : chip("neutral", "Not started")}</header>
      <p class="muted small">Eight reviewed steps. Nothing starts on its own, nothing is switched or moved by a step, and the previous version can be restored from the backups at any point.</p>
      ${m.mode === "none" ? html`<p class="notice notice-info">${icon("info", 18)}<span>No Smart Occupancy &amp; Lighting integration is installed in this Home Assistant. For a new home use <a href="#/settings/setup">Setup</a>; the wizard is for installations that used the monolithic integration.</span></p>` : nothing}
      <ol class="wizard">
        ${m.steps.map((step, i) => {
          const current = step.key === next || (step.status !== "done" && step.key === next);
          const tone = stepTone(step.status);
          return html`<li class="wizard-step wizard-${step.status} ${current ? "wizard-current" : ""}" data-step=${step.key}>
            <div class="wizard-head">
              <span class="wizard-number tone-${tone === "pending" ? "neutral" : tone}">${step.status === "done" ? icon("ok", 18) : i + 1}</span>
              <div class="wizard-title"><strong>${STEP_TITLES[step.key] ?? step.key}</strong>
                <span class="block small muted">${STEP_HELP[step.key] ?? ""}</span></div>
              ${chip(tone, step.status === "pending" ? "Pending" : step.status === "done" ? `Done ${fmt(step.at)}` : step.status)}
            </div>
            ${step.result ? html`<div class="wizard-result">${this.stepResult(step)}</div>` : nothing}
            ${current && !completed ? html`<div class="wizard-actions">${this.stepActions(d, step, true)}</div>` : nothing}
            ${!current && step.status !== "pending" && step.key !== "retire" && !completed ? html`<div class="wizard-actions small">${this.stepActions(d, step, true)}</div>` : nothing}
          </li>`;
        })}
      </ol>
      ${this.result && this.result.step === "abort" ? html`<p class="notice notice-warning">${icon("undo", 18)}<span>Aborted: ${((this.result.data.actions ?? []) as string[]).join("; ") || "nothing to undo"}</span></p>` : nothing}
      <footer class="wizard-foot">
        <button type="button" class="btn" ?disabled=${!!this.busy || !m.started_at} data-testid="migration-abort" @click=${() => void this.act("abort", () => runStep("abort", { restore_legacy: false }), "Migration aborted")}>${icon("undo", 18)}Abort (give control back)</button>
        ${m.mode === "companion" ? html`<button type="button" class="btn" ?disabled=${!!this.busy} data-testid="migration-restore" @click=${() => void this.act("abort", () => runStep("abort", { restore_legacy: true }), "Legacy data restored: you can downgrade the integration to 0.9.0")}>${icon("restore", 18)}Restore the legacy data for a rollback to 0.9.0</button>` : nothing}
        <a class="btn" href="#/settings/system/help" @click=${(e: Event) => { e.preventDefault(); this.tab = "help"; }}>${icon("help", 18)}What works during an App outage</a>
      </footer>
    </section>`;
  }

  private help(): TemplateResult {
    return html`<section class="card wide" data-testid="system-help">
      <header class="card-head">${icon("help")}<h3>Operational safety policy</h3></header>
      <h4>While the App is disconnected or stopped</h4>
      <ul class="plain-list">
        <li>No light is switched off and no cover is moved because the App went away. The companion runs no automation of its own.</li>
        <li>Room occupancy, lighting state and forecast entities keep their last published values and report <code>fresh: false</code>. No vacancy is inferred from missing data.</li>
        <li>Every command of a stale App session is refused: wrong epoch, expired lease or a repeated sequence number never reach a device.</li>
        <li>Physical switches keep working through their own integrations; the App's wall-switch mappings resume when it is back.</li>
        <li>When the App reconnects it opens a fresh session, claims each subsystem again with a new epoch, re-reads Home Assistant's states and starts its engines as after a restart: nothing in flight is replayed, and the start-up settle periods pass before any rule may send.</li>
      </ul>
      <h4>Compared with the earlier hot standby</h4>
      <p class="small">Until 0.9.0 the integration's own engines could take over when the App stopped. Those engines are gone: an App outage now means <strong>no automatic lighting, curtain or occupancy decisions until the App is back</strong>. That is a reduced capability, not a hidden fallback. Keep the App's watchdog enabled in Home Assistant (Settings → Apps → Smart Comfort) and check this page after an update.</p>
      <h4>Rollback</h4>
      <p class="small">A rollback to 0.9.0 is a deliberate software restoration: restore the legacy data here (or with the companion's <code>smart_occupancy/companion/legacy/restore</code> command), then downgrade the integration in HACS and restart Home Assistant. The App's own backups (Settings → System → Export, and every module's backups) are never deleted by the migration.</p>
    </section>`;
  }

  protected render(): unknown {
    const d = this.data;
    return html`
      ${pageHeader("Migration & System", "Owners, the configuration authority, the companion, engine health and the migration wizard.",
        html`<nav class="tabs" aria-label="System sections">
          ${(["overview", "migration", "help"] as const).map((t) => html`<a href="#/settings/system/${t}" class="tab ${this.tab === t ? "active" : ""}" data-tab=${t} @click=${(e: Event) => { e.preventDefault(); this.tab = t; }}>${t === "overview" ? "Overview" : t === "migration" ? "Migration wizard" : "Outage behaviour"}</a>`)}
        </nav>`)}
      ${this.error ? html`<p class="notice notice-error" data-testid="system-error">${icon("error", 20)}<span>${this.error}</span></p>` : nothing}
      ${this.notice ? html`<p class="notice notice-info" data-testid="system-notice">${icon("ok", 20)}<span>${this.notice}</span></p>` : nothing}
      ${!d ? html`<div class="card loading" role="status"><span class="spinner" aria-hidden="true"></span><span>Loading…</span></div>`
        : this.tab === "migration" ? html`<div class="grid">${this.hero(d)}${this.migration(d)}</div>`
        : this.tab === "help" ? html`<div class="grid">${this.help()}</div>`
        : html`<div class="grid">${this.hero(d)}${this.owners(d)}${this.companionCard(d)}${this.configCard(d)}
            <section class="card wide"><p class="small muted">Module controls (handovers, releases, exports and backups) stay on each module's <em>Control</em> page: <a href="#/presence/engine" @click=${(e: Event) => (e.preventDefault(), navigate("presence"))}>Presence</a>, <a href="#/lighting/control">Lighting</a>, <a href="#/curtains/control">Curtains</a>, <a href="#/predictions/control">Predictions</a>.</p></section>
          </div>`}
    `;
  }
}

customElements.define("sl-system", SlSystem);
