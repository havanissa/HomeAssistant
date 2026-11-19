// Read-only Home Assistant lookup (Diagnostics): find an entity, device or
// area with the searchable selector and see its details. Nothing is changed.
import { html, nothing } from "lit";

import { formatState, formatTime } from "../format";
import { icon } from "../icons";
import { inspectLight } from "../lighting/light-inspector";
import { loadLookup, type LookupData, type LookupKind } from "../lookup";
import "../picker";
import { kv, StoreElement } from "../ui";

const KINDS: Array<{ value: LookupKind; label: string }> = [
  { value: "entity", label: "Entity" },
  { value: "device", label: "Device" },
  { value: "area", label: "Area" },
];

export class SlLookupCard extends StoreElement {
  static properties = { kind: { state: true }, selected: { state: true }, data: { state: true } };
  declare kind: LookupKind;
  declare selected: string | null;
  declare data: LookupData | null;

  constructor() {
    super();
    this.kind = "entity";
    this.selected = null;
    this.data = null;
  }

  private setKind(kind: LookupKind): void {
    this.kind = kind;
    this.selected = null;
  }

  private async onPicked(event: CustomEvent<{ kind: LookupKind; value: string }>): Promise<void> {
    this.selected = event.detail.value;
    this.data = await loadLookup();
  }

  private renderDetails(): unknown {
    const d = this.data;
    if (!d || !this.selected) return nothing;
    if (this.kind === "entity") {
      const e = d.entities.find((x) => x.entity_id === this.selected);
      if (!e) return nothing;
      return html`${kv([
        ["Name", e.name],
        ["Entity ID", html`<code>${e.entity_id}</code>`],
        ["State", formatState(e.state, e.unit)],
        ["Last changed", formatTime(e.last_changed)],
        ["Area", e.area],
        ["Device", e.device],
        ["Integration", e.platform],
      ])}${/^(light|switch)\./.test(e.entity_id) ? html`<button type="button" class="btn btn-small" data-testid="lookup-inspect-light" @click=${() => inspectLight(e.entity_id)}>${icon("information-outline", 16)}What controls this light?</button>` : nothing}`;
    }
    if (this.kind === "device") {
      const dev = d.devices.find((x) => x.id === this.selected);
      if (!dev) return nothing;
      const entities = d.entities.filter((x) => x.device_id === dev.id);
      return html`${kv([
          ["Name", dev.name],
          ["Area", dev.area],
          ["Manufacturer", dev.manufacturer],
          ["Model", dev.model],
          ["Entities", String(entities.length)],
        ])}
        ${entities.length
          ? html`<ul class="plain-list">
              ${entities.slice(0, 30).map((x) => html`<li>${x.name} <span class="muted">· ${formatState(x.state, x.unit)}</span></li>`)}
            </ul>`
          : nothing}`;
    }
    const area = d.areas.find((x) => x.id === this.selected);
    if (!area) return nothing;
    return kv([
      ["Name", area.name],
      ["Area ID", html`<code>${area.id}</code>`],
      ["Devices", String(area.device_count)],
      ["Entities", String(area.entity_count)],
    ]);
  }

  protected render(): unknown {
    return html`<section class="card wide" data-testid="card-lookup">
      <header class="card-head">${icon("search")}<h3>Home Assistant lookup</h3></header>
      <p class="muted">Find an entity, device or area and see how Home Assistant reports it. Read-only.</p>
      <fieldset class="segmented" aria-label="Look up">
        <legend class="visually-hidden">Look up</legend>
        ${KINDS.map(
          (k) => html`<label class=${this.kind === k.value ? "selected" : ""}>
            <input type="radio" name="lookup-kind" value=${k.value} .checked=${this.kind === k.value} @change=${() => this.setKind(k.value)} />
            ${k.label}
          </label>`,
        )}
      </fieldset>
      <div class="lookup-body">
        <sl-picker .kind=${this.kind} .value=${this.selected} label="Find" @value-changed=${this.onPicked}></sl-picker>
        <div class="lookup-details" data-testid="lookup-details">
          ${this.selected ? this.renderDetails() : html`<p class="muted">Choose an item to see its details.</p>`}
        </div>
      </div>
    </section>`;
  }
}

customElements.define("sl-lookup-card", SlLookupCard);
