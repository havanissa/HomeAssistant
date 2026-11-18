// Phase A4: Room lighting (being built).
import { html } from "lit";

import { controlBanner, LightingElement } from "../lighting/components";
import { pageHeader } from "../ui";

export class SlLightingRoom extends LightingElement {
  static properties = { ...LightingElement.properties, areaId: { type: String }, tab: { type: String } };
  declare areaId: string;
  declare tab: string;

  protected render(): unknown {
    return html`${pageHeader("Room lighting", "One room's lighting.")}${controlBanner(this.status)}`;
  }
}

customElements.define("sl-lighting-room", SlLightingRoom);
