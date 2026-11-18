// Phase A4: Lighting settings (being built).
import { html } from "lit";

import { controlBanner, LightingElement } from "../lighting/components";
import { pageHeader } from "../ui";

export class SlLightingSettings extends LightingElement {
  static properties = { ...LightingElement.properties, areaId: { type: String }, tab: { type: String } };
  declare areaId: string;
  declare tab: string;

  protected render(): unknown {
    return html`${pageHeader("Lighting settings", "Settings for every room.")}${controlBanner(this.status)}`;
  }
}

customElements.define("sl-lighting-settings", SlLightingSettings);
