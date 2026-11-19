// Settings → Setup (Phase A6): the guided setup of a new installation - what a new home needs, in order, with
// its state, linking to the module that configures it. Completing a step never commands a device: every room
// stays Observing until it is switched to Automatic in Lighting.
import { html, nothing } from "lit";

import { icon } from "../icons";
import { navigatePath } from "../router";
import { store } from "../store";
import { ApiError } from "../rooms/model";
import { chip, pageHeader, StoreElement } from "../ui";
import { companionText, getSetup, pairCompanion, startFresh, type SetupStatus } from "../system/api";

export class SlSetup extends StoreElement {
  static properties = { data: { state: true }, error: { state: true }, busy: { state: true }, notice: { state: true } };
  declare data: SetupStatus | null;
  declare error: string | null;
  declare busy: boolean;
  declare notice: string | null;
  private seen = -1;

  constructor() {
    super();
    this.data = null;
    this.error = null;
    this.busy = false;
    this.notice = null;
  }

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }

  protected willUpdate(): void {
    if (store.systemVersion !== this.seen || store.roomsVersion !== this.seen) {
      const first = this.seen === -1;
      this.seen = store.systemVersion + store.roomsVersion;
      if (!first && !this.busy) void this.load();
    }
  }

  async load(): Promise<void> {
    try {
      this.data = await getSetup();
      this.error = null;
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
    }
  }

  private async act(action: () => Promise<unknown>, done: string): Promise<void> {
    this.busy = true;
    this.error = null;
    this.notice = null;
    try {
      await action();
      this.notice = done;
    } catch (err) {
      this.error = err instanceof ApiError ? `${err.message} (${err.code})` : err instanceof Error ? err.message : String(err);
    } finally {
      this.busy = false;
      await this.load();
    }
  }

  private go(route: string | undefined): void {
    if (!route) return;
    navigatePath(...route.split("/"));
  }

  protected render(): unknown {
    const d = this.data;
    const c = d ? companionText(d.companion) : null;
    const done = d ? d.steps.filter((s) => s.done).length : 0;
    return html`
      ${pageHeader("Setup", "A new installation, step by step. Nothing is controlled until you enable automatic control per room.")}
      ${this.error ? html`<p class="notice notice-error" data-testid="setup-error">${icon("error", 20)}<span>${this.error}</span></p>` : nothing}
      ${this.notice ? html`<p class="notice notice-info" data-testid="setup-notice">${icon("ok", 20)}<span>${this.notice}</span></p>` : nothing}
      ${!d ? html`<div class="card loading" role="status"><span class="spinner" aria-hidden="true"></span><span>Loading…</span></div>` : html`
        <div class="grid">
          <section class="card wide" data-testid="setup-progress">
            <header class="card-head">${icon("check")}<h3>Progress</h3>${chip(d.ready ? "ok" : "info", `${done} of ${d.steps.length} steps`)}</header>
            <div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax=${d.steps.length} aria-valuenow=${done}><span style="width: ${Math.round((100 * done) / d.steps.length)}%"></span></div>
            ${d.legacy_installed ? html`<p class="notice notice-warning">${icon("warning", 18)}<span>The monolithic integration (0.9.0) is installed here. Use <a href="#/settings/system/migration">Migration &amp; System</a> to move its configuration into the App instead of starting fresh.</span></p>` : nothing}
            ${!d.config.authoritative && !d.config.staged && !d.legacy_installed
              ? html`<p>The App has no configuration yet. Create it with safe defaults: no rooms, no mood source, nothing automatic.</p>
                <button type="button" class="btn btn-primary" data-testid="setup-start" ?disabled=${this.busy} @click=${() => void this.act(startFresh, "Configuration created. Continue with the rooms.")}>${icon("plus", 18)}Create the configuration</button>`
              : nothing}
            ${d.companion?.state === "present" && !d.companion.trusted
              ? html`<p class="small">${chip(c!.tone, c!.label)} The companion integration is installed but not paired. Pairing lets the App publish native entities and command devices through the companion's fenced executor.</p>
                <button type="button" class="btn btn-primary" data-testid="setup-pair" ?disabled=${this.busy} @click=${() => void this.act(pairCompanion, "Paired. The App claims occupancy, lighting, curtains and predictions.")}>${icon("link", 18)}Pair the companion</button>`
              : nothing}
            ${d.companion?.state === "absent" || d.companion?.state === "unknown"
              ? html`<p class="notice notice-info">${icon("info", 18)}<span>Without the companion integration (Smart Occupancy &amp; Lighting 1.0.0, installed with HACS) the App can configure everything but has no native entities and cannot command lights or covers. Install it, add it in Settings → Devices &amp; services, then pair it here.</span></p>`
              : nothing}
          </section>
          <section class="card wide" data-testid="setup-steps">
            <header class="card-head">${icon("list")}<h3>Steps</h3></header>
            <ol class="wizard">
              ${d.steps.map((s, i) => html`<li class="wizard-step ${s.done ? "wizard-done" : ""}" data-step=${s.key}>
                <div class="wizard-head">
                  <span class="wizard-number ${s.done ? "tone-ok" : "tone-neutral"}">${s.done ? icon("ok", 18) : i + 1}</span>
                  <div class="wizard-title"><strong>${s.title}</strong>${s.optional ? html` <span class="muted small">(optional)</span>` : nothing}
                    <span class="block small muted">${s.detail}</span></div>
                  ${s.route ? html`<button type="button" class="btn btn-small" data-testid=${`setup-go-${s.key}`} @click=${() => this.go(s.route)}>${icon("arrow-right", 16)}Open</button>` : nothing}
                </div>
              </li>`)}
            </ol>
            <p class="small muted">Capabilities the App needs are checked where they are used: a room without an area can be configured (areas are optional), a mood source can be the built-in selector, and a feature that Home Assistant does not provide (for example the Recorder for predictions) is reported on its page instead of being skipped silently.</p>
          </section>
        </div>`}
    `;
  }
}

customElements.define("sl-setup", SlSetup);
