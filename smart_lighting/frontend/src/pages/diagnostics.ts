import { html, nothing } from "lit";

import { formatDuration, formatTime } from "../format";
import { icon } from "../icons";
import { apiUrl, getJson, store } from "../store";
import type { Diagnostics } from "../types";
import "./lookup-card";
import { chip, haConnectionInfo, kv, linkInfo, loading, pageHeader, statusIcon, StoreElement } from "../ui";

function value(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "boolean") return v ? "Yes" : "No";
  return String(v);
}

export class SlDiagnostics extends StoreElement {
  static properties = {
    details: { state: true },
    loadingDetails: { state: true },
    detailsError: { state: true },
    showJson: { state: true },
    copied: { state: true },
  };
  declare details: Diagnostics | null;
  declare showJson: boolean;
  declare copied: string | null;
  declare loadingDetails: boolean;
  declare detailsError: string | null;

  constructor() {
    super();
    this.details = null;
    this.loadingDetails = false;
    this.detailsError = null;
    this.showJson = false;
    this.copied = null;
  }

  private async copyJson(): Promise<void> {
    const text = JSON.stringify(this.details, null, 2);
    try {
      await navigator.clipboard.writeText(text);
      this.copied = "Copied";
    } catch {
      // Clipboard API unavailable (some webviews): select the text instead.
      const area = this.querySelector<HTMLTextAreaElement>("textarea.json-view");
      area?.focus();
      area?.select();
      this.copied = "Selected: use your device's Copy";
    }
    window.setTimeout(() => (this.copied = null), 4000);
  }

  connectedCallback(): void {
    super.connectedCallback();
    void this.loadDetails();
  }

  private async loadDetails(): Promise<void> {
    this.loadingDetails = true;
    try {
      this.details = await getJson<Diagnostics>("api/diagnostics");
      this.detailsError = null;
    } catch (err) {
      this.detailsError = String(err);
    } finally {
      this.loadingDetails = false;
    }
  }

  protected render(): unknown {
    const o = store.overview;
    const actions = html`<div class="actions">
      <button class="btn" @click=${() => void this.loadDetails()} ?disabled=${this.loadingDetails}>
        ${icon("refresh", 18)} Refresh
      </button>
      <button class="btn" @click=${() => (this.showJson = !this.showJson)} aria-expanded=${this.showJson ? "true" : "false"} data-testid="view-diagnostics">
        ${icon("json", 18)} ${this.showJson ? "Hide" : "View"} data
      </button>
      <a class="btn btn-primary" href=${apiUrl("api/diagnostics?download=1")} download data-testid="download-diagnostics">
        ${icon("download", 18)} Download
      </a>
    </div>`;
    if (!o) return html`${pageHeader("Diagnostics", "Health checks and connection details.", actions)} ${loading()}`;

    const conn = o.home_assistant.connection;
    const sup = o.supervisor;
    const d = this.details;
    const link = linkInfo(store.link);

    return html`
      ${pageHeader("Diagnostics", "Health checks and connection details.", actions)}
      ${this.showJson && d
        ? html`<section class="card json-card" data-testid="card-json">
            <header class="card-head">${icon("json")}<h3>Diagnostics data</h3>
              <button class="btn" @click=${() => void this.copyJson()} data-testid="copy-diagnostics">${icon("copy", 18)} ${this.copied ?? "Copy"}</button>
            </header>
            <p class="muted small">Share this when reporting a problem. It contains no passwords or tokens.</p>
            <textarea class="json-view mono" readonly rows="14" aria-label="Diagnostics data">${JSON.stringify(d, null, 2)}</textarea>
          </section>`
        : nothing}
      <div class="grid">
        <section class="card" data-testid="card-checks">
          <header class="card-head">${icon("stethoscope")}<h3>Health checks</h3></header>
          <ul class="checks">
            ${o.checks.map(
              (c) => html`<li data-check=${c.key} data-status=${c.status}>
                ${statusIcon(c.status)}
                <span><strong>${c.title}</strong><br /><span class="muted">${c.detail}</span></span>
              </li>`,
            )}
          </ul>
        </section>

        <section class="card" data-testid="card-safety">
          <header class="card-head">${icon("shield-check")}<h3>Safety</h3>${chip("ok", "Read-only")}</header>
          ${kv([
            ["Service calls sent", String(o.safety.service_calls_sent)],
            ["Device control", o.safety.device_control ? "On" : "Off"],
            ["Automation engine", o.safety.automation_engine ? "Running" : "None (the integration stays in charge)"],
            ["Automatic migration", o.safety.automatic_migration ? "On" : "Off"],
            ["Changes to the integration", o.safety.modifies_integration ? "Yes" : "None"],
          ])}
          <details>
            <summary>Home Assistant requests made</summary>
            <ul class="plain-list mono">
              ${Object.entries(o.safety.commands_sent).map(([cmd, n]) => html`<li>${cmd}: ${n}</li>`)}
            </ul>
            <p class="muted small">Allowed (all read-only): ${o.safety.allowed_commands.join(", ")}.</p>
          </details>
        </section>

        <section class="card" data-testid="card-ha-connection">
          <header class="card-head">${icon("home-assistant")}<h3>Home Assistant connection</h3>
            ${chip(haConnectionInfo(conn).tone, haConnectionInfo(conn).label)}</header>
          ${kv([
            ["State", conn.state],
            ["Version", conn.ha_version],
            ["Connected since", formatTime(conn.connected_since)],
            ["Last disconnect", formatTime(conn.last_disconnect)],
            ["Reconnects", conn.reconnects],
            ["Response time", conn.latency_ms != null ? `${conn.latency_ms} ms` : null],
            ["State changes received", conn.events_total],
            ["Last error", conn.last_error],
            ["Browser link", link.label],
          ])}
        </section>

        <section class="card" data-testid="card-app-details">
          <header class="card-head">${icon("lightbulb-auto")}<h3>App</h3></header>
          ${kv([
            ["Version", o.app.version],
            ["Phase", o.app.phase],
            ["Started", formatTime(o.app.started_at)],
            ["Running for", formatDuration(o.app.uptime_s)],
            sup?.available ? ["Latest version", value(sup.app.version_latest)] : null,
            sup?.available ? ["Architecture", value(sup.app.arch)] : null,
            d ? ["Python", d.runtime.python] : null,
            d ? ["aiohttp", d.runtime.aiohttp] : null,
          ])}
        </section>

        <section class="card" data-testid="card-system">
          <header class="card-head">${icon("info")}<h3>System</h3>
            ${sup?.configured ? chip(sup.available ? "ok" : "warning", sup.available ? "Supervisor" : "Unavailable") : chip("neutral", "Development")}</header>
          ${sup?.available
            ? kv([
                ["Home Assistant", value(sup.system.homeassistant)],
                ["Supervisor", value(sup.system.supervisor)],
                ["Operating system", value(sup.system.operating_system)],
                ["Machine", value(sup.system.machine)],
                ["Update channel", value(sup.system.channel)],
                ["Supported / healthy", `${value(sup.system.supported)} / ${value(sup.system.healthy)}`],
              ])
            : html`<p class="muted">${sup?.error ?? "Supervisor information is not available."}</p>`}
        </section>

        <sl-lookup-card></sl-lookup-card>

        <section class="card wide" data-testid="card-log">
          <header class="card-head">${icon("stethoscope")}<h3>Recent log</h3></header>
          ${this.detailsError ? html`<p class="notice notice-error">Could not load details: ${this.detailsError}</p>` : nothing}
          <details>
            <summary>Show the last ${d ? Math.min(d.log.length, 100) : 0} log entries</summary>
            <div class="log mono">
              ${(d?.log ?? []).slice(-100).reverse().map(
                (entry) => html`<div class="log-line log-${entry.level}">
                  <span class="nowrap">${formatTime(entry.time)}</span> <span class="log-level">${entry.level}</span>
                  <span>${entry.message}</span>
                </div>`,
              )}
            </div>
          </details>
        </section>
      </div>
    `;
  }
}

customElements.define("sl-diagnostics", SlDiagnostics);
