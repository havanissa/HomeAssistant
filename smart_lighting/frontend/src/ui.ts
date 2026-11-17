// Shared building blocks: base element, status chips, key/value lists.
import { html, LitElement, nothing, type TemplateResult } from "lit";

import { icon } from "./icons";
import { store, type LinkState } from "./store";
import type { CheckStatus, Connection, Integration } from "./types";

/** Light-DOM element that re-renders whenever the store changes. */
export class StoreElement extends LitElement {
  private unsubscribe: (() => void) | null = null;

  protected createRenderRoot(): HTMLElement {
    return this;
  }

  connectedCallback(): void {
    super.connectedCallback();
    this.unsubscribe = store.subscribe(() => this.requestUpdate());
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.unsubscribe?.();
    this.unsubscribe = null;
  }
}

export type Tone = CheckStatus | "pending" | "neutral";

export function chip(tone: Tone, label: string): TemplateResult {
  return html`<span class="chip chip-${tone}"><span class="dot" aria-hidden="true"></span><span class="chip-label">${label}</span></span>`;
}

export function statusIcon(status: CheckStatus): TemplateResult {
  return html`<span class="status-icon tone-${status}" role="img" aria-label=${status}>${icon(status, 20)}</span>`;
}

export function kv(rows: Array<[string, unknown] | null | false>): TemplateResult {
  return html`<dl class="kv">
    ${rows.map((row) =>
      row
        ? html`<dt>${row[0]}</dt>
            <dd>${row[1] === null || row[1] === undefined || row[1] === "" ? "—" : row[1]}</dd>`
        : nothing,
    )}
  </dl>`;
}

export function pageHeader(title: string, subtitle: string, extra: unknown = nothing): TemplateResult {
  return html`<div class="page-header">
    <div>
      <h2 tabindex="-1">${title}</h2>
      <p class="muted">${subtitle}</p>
    </div>
    ${extra}
  </div>`;
}

export function linkInfo(link: LinkState): { tone: Tone; label: string } {
  switch (link) {
    case "live":
      return { tone: "ok", label: "Live" };
    case "polling":
      return { tone: "warning", label: "Updating every 10 s" };
    case "offline":
      return { tone: "error", label: "App unreachable" };
    default:
      return { tone: "neutral", label: "Connecting…" };
  }
}

export function haConnectionInfo(connection: Connection | undefined): { tone: Tone; label: string } {
  switch (connection?.state) {
    case "connected":
      return { tone: "ok", label: "Connected" };
    case "connecting":
      return { tone: "warning", label: "Connecting…" };
    case "disconnected":
      return { tone: "warning", label: "Reconnecting…" };
    case "auth_failed":
      return { tone: "error", label: "Access refused" };
    case "not_configured":
      return { tone: "error", label: "No credentials" };
    default:
      return { tone: "neutral", label: "Unknown" };
  }
}

export function integrationInfo(integration: Integration | null | undefined): { tone: Tone; label: string } {
  switch (integration?.status) {
    case "loaded":
      return { tone: "ok", label: "Detected · running" };
    case "installed":
      return { tone: "info", label: "Installed · not set up" };
    case "not_running":
      return { tone: "warning", label: "Set up · not running" };
    case "not_installed":
      return { tone: "neutral", label: "Not installed" };
    default:
      return { tone: "neutral", label: "Not checked yet" };
  }
}

export function loading(): TemplateResult {
  return html`<div class="card loading" role="status">
    <span class="spinner" aria-hidden="true"></span>
    ${store.link === "offline"
      ? html`<span>The App backend cannot be reached. Retrying…</span>`
      : html`<span>Loading…</span>`}
  </div>`;
}
