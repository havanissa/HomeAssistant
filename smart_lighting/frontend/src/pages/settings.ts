import { html, nothing } from "lit";

import { icon } from "../icons";
import { getAdvanced, getTheme, setAdvanced, setTheme, type ThemePref } from "../prefs";
import { getJson, store } from "../store";
import type { Diagnostics } from "../types";
import { kv, linkInfo, pageHeader, StoreElement } from "../ui";

const THEMES: Array<{ value: ThemePref; label: string }> = [
  { value: "auto", label: "Automatic" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

export class SlSettings extends StoreElement {
  static properties = { theme: { state: true }, advanced: { state: true }, settings: { state: true } };
  declare theme: ThemePref;
  declare advanced: boolean;
  declare settings: Record<string, unknown> | null;

  constructor() {
    super();
    this.theme = getTheme();
    this.advanced = getAdvanced();
    this.settings = null;
  }

  connectedCallback(): void {
    super.connectedCallback();
    if (this.advanced) void this.loadSettings();
  }

  private async loadSettings(): Promise<void> {
    try {
      this.settings = (await getJson<Diagnostics>("api/diagnostics")).settings;
    } catch {
      this.settings = null;
    }
  }

  private chooseTheme(value: ThemePref): void {
    this.theme = value;
    setTheme(value);
  }

  private toggleAdvanced(event: Event): void {
    this.advanced = (event.target as HTMLInputElement).checked;
    setAdvanced(this.advanced);
    if (this.advanced && !this.settings) void this.loadSettings();
  }

  private renderAdvanced(): unknown {
    const o = store.overview;
    const s = this.settings;
    const ingress = new URL(document.baseURI).pathname;
    return html`<section class="card wide" data-testid="advanced-settings">
      <header class="card-head">${icon("cog")}<h3>Advanced</h3></header>
      <p class="muted">
        Technical details, for troubleshooting. App options (such as the log level) are changed in Home Assistant:
        Settings → Apps → Smart Occupancy &amp; Lighting → Configuration.
      </p>
      ${kv([
        ["Log level", s?.log_level],
        ["Home Assistant API", s?.home_assistant_websocket],
        ["Supervisor API", s?.supervisor_api],
        ["API credentials", s?.token],
        ["Accepted clients", Array.isArray(s?.allowed_clients) ? (s?.allowed_clients as string[]).join(", ") : null],
        ["Interface path", ingress],
        ["Live updates", linkInfo(store.link).label],
        ["Development mode", o?.app.dev_mode ? "On" : "Off"],
      ])}
    </section>`;
  }

  protected render(): unknown {
    const o = store.overview;
    return html`
      ${pageHeader("Settings", "Appearance and information about the App.")}
      <div class="grid grid-2">
        <section class="card" data-testid="appearance">
          <header class="card-head">${icon("palette")}<h3>Appearance</h3></header>
          <fieldset class="segmented" aria-label="Theme">
            <legend>Theme</legend>
            ${THEMES.map(
              (t) => html`<label class=${this.theme === t.value ? "selected" : ""}>
                <input
                  type="radio"
                  name="theme"
                  value=${t.value}
                  .checked=${this.theme === t.value}
                  @change=${() => this.chooseTheme(t.value)}
                />
                ${t.label}
              </label>`,
            )}
          </fieldset>
          <p class="muted small">Automatic follows your device's light or dark setting. Saved in this browser.</p>
        </section>

        <section class="card" data-testid="about">
          <header class="card-head">${icon("lightbulb-auto")}<h3>About</h3></header>
          ${kv([
            ["App", o?.app.name ?? "Smart Occupancy & Lighting"],
            ["Version", o?.app.version],
            ["Phase", o ? `${o.app.phase} (foundation, read-only)` : null],
            ["Feature settings", "Not in the App yet: they are migrated module by module"],
          ])}
        </section>

        <section class="card wide">
          <label class="switch-row">
            <input type="checkbox" role="switch" .checked=${this.advanced} @change=${this.toggleAdvanced} data-testid="advanced-toggle" />
            <span>
              <strong>Show advanced settings</strong><br />
              <span class="muted small">Technical details for troubleshooting.</span>
            </span>
          </label>
        </section>
        ${this.advanced ? this.renderAdvanced() : nothing}
      </div>
    `;
  }
}

customElements.define("sl-settings", SlSettings);
