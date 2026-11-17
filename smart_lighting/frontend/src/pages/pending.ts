// Screen of a module that has not been migrated yet. Deliberately shows no
// controls: nothing here can change the setup.
import { html, nothing } from "lit";

import { icon } from "../icons";
import { NAV } from "../router";
import { store } from "../store";
import { chip, integrationInfo, pageHeader, StoreElement } from "../ui";

export class SlPendingModule extends StoreElement {
  static properties = { moduleKey: { type: String } };
  declare moduleKey: string;

  protected render(): unknown {
    const nav = NAV.find((n) => n.key === this.moduleKey);
    const module = store.overview?.modules.find((m) => m.key === this.moduleKey);
    const title = module?.title ?? nav?.title ?? this.moduleKey;
    const integration = store.overview?.integration;
    const status = integrationInfo(integration);
    const running = integration?.status === "loaded";

    return html`
      ${pageHeader(title, module?.summary ?? "", chip("pending", "Migration pending"))}
      <section class="card pending-card" data-testid="pending-${this.moduleKey}">
        <div class="pending-hero">
          <span class="pending-icon">${icon(module?.icon ?? nav?.icon ?? "puzzle", 40)}</span>
          <div>
            <h3>${title} has not been migrated to the App yet</h3>
            <p class="muted">
              This screen has no settings yet, and nothing on it can change your setup. The module moves to the App
              in a later phase, and only after you approve it.
            </p>
          </div>
        </div>
        <dl class="kv">
          <dt>Will cover</dt>
          <dd>${module?.summary ?? "—"}</dd>
          <dt>Configure it today in</dt>
          <dd>${module?.handled_by ?? "The existing integration's dashboard cards."}</dd>
          <dt>Existing integration</dt>
          <dd>
            ${chip(status.tone, status.label)}
            ${running ? html`<span class="muted">It keeps handling ${title.toLowerCase()} unchanged.</span>` : nothing}
          </dd>
        </dl>
      </section>
    `;
  }
}

customElements.define("sl-pending-module", SlPendingModule);
