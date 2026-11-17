// Searchable Home Assistant entity / device / area selector.
//
// A full-width field opens a native <dialog>: full screen on phones (search
// at the top, so it stays visible above the on-screen keyboard), a centred
// dialog on larger screens. Touch, mouse and keyboard (arrows, Enter, Escape)
// all work; nothing depends on hover.
import { html, nothing } from "lit";

import { icon } from "./icons";
import { filterOptions, loadLookup, options, type LookupKind, type LookupOption } from "./lookup";
import { StoreElement } from "./ui";

const LABELS: Record<LookupKind, { title: string; placeholder: string; search: string }> = {
  entity: { title: "Choose an entity", placeholder: "Choose an entity…", search: "Search name, entity ID or area" },
  device: { title: "Choose a device", placeholder: "Choose a device…", search: "Search name, area or model" },
  area: { title: "Choose an area", placeholder: "Choose an area…", search: "Search areas" },
};
const LIMIT = 60;

let counter = 0;

export class SlPicker extends StoreElement {
  static properties = {
    kind: { type: String },
    value: { type: String },
    label: { type: String },
    open: { state: true },
    query: { state: true },
    active: { state: true },
    all: { state: true },
    error: { state: true },
  };
  declare kind: LookupKind;
  declare value: string | null;
  declare label: string;
  declare open: boolean;
  declare query: string;
  declare active: number;
  declare all: LookupOption[] | null;
  declare error: string | null;
  private readonly uid = `picker-${++counter}`;
  private loadedKind: LookupKind | null = null;

  constructor() {
    super();
    this.kind = "entity";
    this.value = null;
    this.label = "";
    this.open = false;
    this.query = "";
    this.active = 0;
    this.all = null;
    this.error = null;
  }

  private get dialog(): HTMLDialogElement | null {
    return this.querySelector("dialog");
  }

  private async load(force = false): Promise<void> {
    try {
      const data = await loadLookup(force);
      this.all = options(data, this.kind);
      this.loadedKind = this.kind;
      this.error = null;
    } catch (err) {
      this.error = `Could not load the list (${String(err)}).`;
    }
  }

  protected willUpdate(changed: Map<string, unknown>): void {
    if (changed.has("kind") && this.loadedKind && this.loadedKind !== this.kind) {
      this.all = null;
      void this.load();
    }
  }

  private async show(): Promise<void> {
    this.query = "";
    this.active = 0;
    this.open = true;
    await this.updateComplete;
    this.dialog?.showModal();
    this.querySelector<HTMLInputElement>(".picker-search input")?.focus();
    await this.load(true);
  }

  private close(): void {
    this.dialog?.close();
  }

  private onClosed(): void {
    this.open = false;
    this.querySelector<HTMLButtonElement>(".picker-field")?.focus();
  }

  private choose(option: LookupOption): void {
    this.value = option.id;
    this.dispatchEvent(
      new CustomEvent("value-changed", { detail: { kind: this.kind, value: option.id }, bubbles: true, composed: true }),
    );
    this.close();
  }

  private onInput(event: Event): void {
    this.query = (event.target as HTMLInputElement).value;
    this.active = 0;
  }

  private onKey(event: KeyboardEvent, items: LookupOption[]): void {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      this.active = Math.max(0, Math.min(items.length - 1, this.active + step));
      this.querySelector(`#${this.uid}-opt-${this.active}`)?.scrollIntoView({ block: "nearest" });
    } else if (event.key === "Enter" && items[this.active]) {
      event.preventDefault();
      this.choose(items[this.active]);
    }
  }

  private selectedLabel(): string | null {
    if (!this.value) return null;
    const found = this.all?.find((o) => o.id === this.value);
    return found ? found.primary : this.value;
  }

  protected render(): unknown {
    const text = LABELS[this.kind] ?? LABELS.entity;
    const result = this.all ? filterOptions(this.all, this.query, LIMIT) : { items: [], total: 0 };
    const items = result.items;
    const selected = this.selectedLabel();
    const listId = `${this.uid}-list`;

    return html`
      <span class="field-label" id="${this.uid}-label">${this.label || text.title}</span>
      <button
        type="button"
        class="picker-field"
        aria-haspopup="dialog"
        aria-labelledby="${this.uid}-label ${this.uid}-value"
        @click=${() => void this.show()}
        data-testid="picker-field"
      >
        <span id="${this.uid}-value" class=${selected ? "" : "muted"}>${selected ?? text.placeholder}</span>
        ${icon("chevron-right", 20)}
      </button>
      ${this.open
        ? html`<dialog class="sheet sheet-full" aria-labelledby="${this.uid}-title" @close=${this.onClosed}
            @click=${(e: MouseEvent) => e.target === this.dialog && this.close()}>
            <div class="sheet-inner">
              <header class="sheet-head">
                <h2 id="${this.uid}-title">${text.title}</h2>
                <button type="button" class="icon-btn" aria-label="Close" @click=${this.close}>${icon("close")}</button>
              </header>
              <div class="picker-search">
                ${icon("search", 20)}
                <input
                  type="search"
                  inputmode="search"
                  enterkeyhint="search"
                  autocomplete="off"
                  autocapitalize="off"
                  spellcheck="false"
                  role="combobox"
                  aria-expanded="true"
                  aria-controls=${listId}
                  aria-activedescendant=${items.length ? `${this.uid}-opt-${this.active}` : nothing}
                  aria-label=${text.search}
                  placeholder=${text.search}
                  .value=${this.query}
                  @input=${this.onInput}
                  @keydown=${(e: KeyboardEvent) => this.onKey(e, items)}
                />
              </div>
              <div class="picker-results">
                ${this.error
                  ? html`<p class="notice notice-error">${this.error}</p>`
                  : !this.all
                    ? html`<p class="muted pad">Loading…</p>`
                    : !items.length
                      ? html`<p class="muted pad">Nothing matches “${this.query}”.</p>`
                      : html`<ul id=${listId} role="listbox" aria-label=${text.title}>
                          ${items.map(
                            (o, i) => html`<li
                              id="${this.uid}-opt-${i}"
                              role="option"
                              aria-selected=${o.id === this.value ? "true" : "false"}
                              class=${i === this.active ? "active" : ""}
                              data-value=${o.id}
                              @click=${() => this.choose(o)}
                            >
                              <span class="opt-primary">${o.primary}</span>
                              <span class="opt-secondary">${o.secondary}</span>
                            </li>`,
                          )}
                        </ul>
                        ${result.total > items.length
                          ? html`<p class="muted small pad">
                              Showing ${items.length} of ${result.total}. Type more to narrow the list.
                            </p>`
                          : nothing}`}
              </div>
            </div>
          </dialog>`
        : nothing}
    `;
  }
}

customElements.define("sl-picker", SlPicker);
