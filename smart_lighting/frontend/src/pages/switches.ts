// Phase A4: Switches (being built).
import { html } from "lit";

import { controlBanner, LightingElement } from "../lighting/components";
import { pageHeader } from "../ui";

export class SlSwitches extends LightingElement {
  static properties = { ...LightingElement.properties, areaId: { type: String }, tab: { type: String } };
  declare areaId: string;
  declare tab: string;

  protected render(): unknown {
    return html`${pageHeader("Switches", "Wall switches, remotes and Priority Inputs.")}${controlBanner(this.status)}`;
  }
}

customElements.define("sl-switches", SlSwitches);
