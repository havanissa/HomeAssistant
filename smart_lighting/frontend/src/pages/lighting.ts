// Phase A4: Lighting (being built).
import { html } from "lit";

import { controlBanner, LightingElement } from "../lighting/components";
import { pageHeader } from "../ui";

export class SlLighting extends LightingElement {
  static properties = { ...LightingElement.properties, areaId: { type: String }, tab: { type: String } };
  declare areaId: string;
  declare tab: string;

  protected render(): unknown {
    return html`${pageHeader("Lighting", "Automatic lighting of every room, live decisions and warnings.")}${controlBanner(this.status)}`;
  }
}

customElements.define("sl-lighting", SlLighting);
