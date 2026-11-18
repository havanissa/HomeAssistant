// Moods (Phase A4): mood profiles, the moods and their inheritance, the mood source, light types and the global
// adjustment. Every panel talks to the integration's own lighting commands (api/lighting/command); see
// src/lighting/moods-*.ts. Route: #/moods/<tab>.
import "../lighting/moods.css";
import "../lighting/moods-adjust";
import "../lighting/moods-list";
import "../lighting/moods-profiles";
import "../lighting/moods-source";
import "../lighting/moods-types";

import { html } from "lit";

import { icon } from "../icons";
import { controlBanner, LightingElement } from "../lighting/components";
import { pageHeader } from "../ui";

const TABS = [
  { key: "profiles", title: "Profiles", icon: "lightbulb-group" },
  { key: "moods", title: "Moods", icon: "palette" },
  { key: "source", title: "Mood source", icon: "tune" },
  { key: "types", title: "Light types", icon: "lightbulb" },
  { key: "adjust", title: "Global adjustment", icon: "brightness" },
] as const;

export class SlMoods extends LightingElement {
  static properties = { ...LightingElement.properties, areaId: { type: String }, tab: { type: String } };
  declare areaId: string;
  declare tab: string;

  constructor() {
    super();
    this.areaId = "";
    this.tab = "";
  }

  private panel(tab: string): unknown {
    switch (tab) {
      case "moods": return html`<sl-moods-list></sl-moods-list>`;
      case "source": return html`<sl-moods-source></sl-moods-source>`;
      case "types": return html`<sl-moods-types></sl-moods-types>`;
      case "adjust": return html`<sl-moods-adjust></sl-moods-adjust>`;
      default: return html`<sl-moods-profiles></sl-moods-profiles>`;
    }
  }

  protected render(): unknown {
    const tab = TABS.some((t) => t.key === this.tab) ? this.tab : "profiles";
    return html`<div class="mo-page">
      ${pageHeader("Moods", "Mood profiles, the moods and where they come from, light types and the global adjustment.")}
      ${controlBanner(this.status)}
      <nav class="tabs mo-tabs" aria-label="Moods">
        ${TABS.map((t) => html`<a href="#/moods/${t.key}" class=${t.key === tab ? "active" : ""} aria-current=${t.key === tab ? "page" : "false"} data-tab=${t.key}>${icon(t.icon, 20)}${t.title}</a>`)}
      </nav>
      ${this.panel(tab)}
    </div>`;
  }
}

customElements.define("sl-moods", SlMoods);
