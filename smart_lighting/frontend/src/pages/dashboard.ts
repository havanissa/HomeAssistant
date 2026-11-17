import { html, nothing } from "lit";

import { navigate } from "../router";
import { formatDuration, formatState, formatTime, plural } from "../format";
import { icon } from "../icons";
import { store } from "../store";
import type { Overview } from "../types";
import { chip, haConnectionInfo, integrationInfo, kv, linkInfo, loading, pageHeader, StoreElement, type Tone } from "../ui";

const HEALTH: Record<string, { tone: Tone; label: string }> = {
  ok: { tone: "ok", label: "Healthy" },
  warning: { tone: "warning", label: "Degraded" },
  error: { tone: "error", label: "Problem" },
};

export class SlDashboard extends StoreElement {
  private renderConnection(o: Overview): unknown {
    const conn = o.home_assistant.connection;
    const info = o.home_assistant.info;
    const status = haConnectionInfo(conn);
    const link = linkInfo(store.link);
    return html`<section class="card" data-testid="card-connection">
      <header class="card-head">
        ${icon("home-assistant")}
        <h3>Home Assistant</h3>
        ${chip(status.tone, status.label)}
      </header>
      ${conn.state !== "connected" && conn.last_error
        ? html`<p class="notice notice-${status.tone === "error" ? "error" : "warning"}">${conn.last_error}</p>`
        : nothing}
      ${kv([
        ["Home", info?.location_name],
        ["Version", info?.version ?? conn.ha_version],
        ["Connected since", formatTime(conn.connected_since)],
        ["Response time", conn.latency_ms != null ? `${conn.latency_ms} ms` : null],
        ["Entities / areas", info ? `${info.entity_count ?? "—"} / ${info.area_count ?? "—"}` : null],
        ["State changes", conn.connected ? `${conn.events_per_minute ?? 0} in the last minute` : null],
        ["Live updates", link.label],
      ])}
    </section>`;
  }

  private renderApp(o: Overview): unknown {
    const health = HEALTH[o.app.health] ?? HEALTH.warning;
    const problems = o.checks.filter((c) => c.status === "error" || c.status === "warning");
    return html`<section class="card" data-testid="card-app">
      <header class="card-head">
        ${icon("lightbulb-auto")}
        <h3>App</h3>
        ${chip(health.tone, health.label)}
      </header>
      ${kv([
        ["Version", o.app.version],
        ["Phase", `${o.app.phase} · Rooms configuration`],
        ["Running for", formatDuration(o.app.uptime_s)],
        ["Health checks", problems.length ? plural(problems.length, "needs attention", "need attention") : "All passed"],
      ])}
      ${problems.length
        ? html`<ul class="plain-list">
            ${problems.map((c) => html`<li>${chip(c.status, c.title)} <span class="muted">${c.detail}</span></li>`)}
          </ul>`
        : nothing}
      <a class="text-link" href="#/diagnostics" @click=${(e: Event) => (e.preventDefault(), navigate("diagnostics"))}
        >Open Diagnostics ${icon("chevron-right", 18)}</a
      >
    </section>`;
  }

  private renderIntegration(o: Overview): unknown {
    const integration = o.integration;
    const status = integrationInfo(integration);
    const entry = integration?.entries[0];
    const domains = Object.entries(integration?.entities_by_domain ?? {})
      .map(([domain, n]) => `${n} ${domain.replace("_", " ")}`)
      .join(", ");
    return html`<section class="card" data-testid="card-integration">
      <header class="card-head">
        ${icon("puzzle")}
        <h3>Existing integration</h3>
        ${chip(status.tone, status.label)}
      </header>
      <p class="muted">
        Smart Occupancy & Lighting custom integration (<code>${integration?.domain ?? "smart_occupancy"}</code>).
        ${integration?.detected
          ? "It keeps running all automation. The App edits only its room configuration, through the integration's own validated commands."
          : "When it is installed, the App shows its status here. Nothing is installed or changed automatically."}
      </p>
      ${integration?.detected
        ? kv([
            ["Version", integration.version],
            ["Configuration", entry ? `${entry.title ?? "—"} (${entry.state ?? "unknown"})` : "Not set up"],
            ["Entities", integration.entity_count ? `${integration.entity_count} (${domains})` : "None"],
          ])
        : nothing}
      ${o.integration_entities.length
        ? html`<details class="entities">
            <summary>Live entity states (${o.integration_entities.length})</summary>
            <ul class="entity-list">
              ${o.integration_entities.map(
                (e) => html`<li data-entity=${e.entity_id}>
                  <span class="entity-main">
                    <span class="entity-name">${e.name}</span>
                    <span class="entity-id">${e.entity_id}</span>
                  </span>
                  <span class="entity-state">
                    <span class="state">${formatState(e.state, e.unit)}</span>
                    <span class="muted small">${formatTime(e.last_changed)}</span>
                  </span>
                </li>`,
              )}
            </ul>
          </details>`
        : nothing}
    </section>`;
  }

  private renderModules(o: Overview): unknown {
    return html`<section class="card wide" data-testid="card-modules">
      <header class="card-head">
        ${icon("view-dashboard")}
        <h3>Modules</h3>
      </header>
      <div class="tiles">
        ${o.modules.map(
          (m) => html`<a
            class="tile"
            href="#/${m.key}"
            data-module=${m.key}
            @click=${(e: Event) => (e.preventDefault(), navigate(m.key))}
          >
            <span class="tile-icon">${icon(m.icon)}</span>
            <span class="tile-body">
              <span class="tile-title">${m.title}</span>
              ${m.status === "available" ? chip("ok", "Available") : m.status === "gui" ? chip("info", "Configured in the App") : chip("pending", "Pending")}
            </span>
          </a>`,
        )}
      </div>
    </section>`;
  }

  private renderMigration(o: Overview): unknown {
    const m = o.migration;
    return html`<section class="card wide" data-testid="card-migration">
      <header class="card-head">
        ${icon("pending")}
        <h3>Migration progress</h3>
        ${chip("info", `${m.interfaces_migrated ?? 0} of ${m.modules_total} configured in the App`)}
        ${chip(m.modules_migrated === m.modules_total ? "ok" : "pending", `${m.modules_migrated} of ${m.modules_total} automations moved`)}
      </header>
      <div
        class="progress"
        role="progressbar"
        aria-label="Configuration interfaces in the App"
        aria-valuemin="0"
        aria-valuemax=${m.modules_total}
        aria-valuenow=${m.interfaces_migrated ?? 0}
      >
        <span style="width: ${m.interface_percent ?? 0}%"></span>
      </div>
      <p class="muted">
        Phase ${m.current_phase}: the Rooms configuration interface is in the App; the configuration itself stays in
        the integration, which also keeps running every automation. Modules move one at a time, only after you
        approve it. Each module's automation is run by exactly one system: today the integration runs
        ${m.controlled_by_integration} of ${m.modules_total}, the App ${m.controlled_by_app}.
      </p>
      <ol class="steps">
        ${m.items.map(
          (item) => html`<li class="step step-${item.status}">
            <span class="step-mark" aria-hidden="true">${icon(item.status === "done" ? "ok" : "pending", 18)}</span>
            <span class="step-title"
              >${item.title}
              ${item.controlled_by
                ? html`<span class="muted small block"
                    >Configured in: ${item.gui_owner === "app" ? "App" : "integration"} · stored in:
                    ${item.config_source === "app" ? "App" : "integration"} · automation:
                    ${item.controlled_by === "app" ? "App" : "integration"}</span
                  >`
                : nothing}</span
            >
            ${item.status === "done" ? chip("ok", "Done") : item.status === "gui" ? chip("info", "Interface in App") : chip("pending", "Pending")}
          </li>`,
        )}
      </ol>
    </section>`;
  }

  protected render(): unknown {
    const o = store.overview;
    return html`
      ${pageHeader("Dashboard", "Status of the App, its Home Assistant connection and the migration.")}
      <p class="notice notice-info" data-testid="readonly-notice">
        ${icon("shield-check", 20)}
        <span
          >Phase A2: Rooms can be configured here. Changes are reviewed before saving and stored in the Smart
          Occupancy integration, which keeps running every automation. The App never controls devices.</span
        >
      </p>
      ${o
        ? html`<div class="grid">
            ${this.renderConnection(o)} ${this.renderApp(o)} ${this.renderIntegration(o)} ${this.renderModules(o)}
            ${this.renderMigration(o)}
          </div>`
        : loading()}
    `;
  }
}

customElements.define("sl-dashboard", SlDashboard);
