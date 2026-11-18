// Phase A4: Moods (being built).
import { html } from "lit";

import { controlBanner, LightingElement } from "../lighting/components";
import { pageHeader } from "../ui";

export class SlMoods extends LightingElement {
  static properties = { ...LightingElement.properties, areaId: { type: String }, tab: { type: String } };
  declare areaId: string;
  declare tab: string;

  protected render(): unknown {
    return html`${pageHeader("Moods", "Moods, their source, light types and profiles.")}${controlBanner(this.status)}`;
  }
}

customElements.define("sl-moods", SlMoods);
