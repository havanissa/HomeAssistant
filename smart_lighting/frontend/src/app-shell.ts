// The App frame. Desktop and tablet are the primary targets (everyday phone
// use goes through the integration's compact Lovelace cards); phones still work:
//   desktop / landscape tablet (>= 1024 px): sidebar
//   portrait tablet (600-1023): collapsible navigation rail
//   phone   (< 600 px): top bar + bottom navigation; "More" lists every module
import { html, nothing } from "lit";

import { icon } from "./icons";
import { BOTTOM_NAV, leaveWarning, NAV, navigate, routeFromHash, subPath, type NavItem } from "./router";
import { store } from "./store";
import { chip, haConnectionInfo, linkInfo, StoreElement } from "./ui";

import "./pages/climate";
import "./pages/climate-settings";
import "./pages/curtains";
import "./pages/curtains-control";
import "./pages/curtains-window";
import "./pages/dashboard";
import "./pages/diagnostics";
import "./pages/floorplan";
import "./pages/lighting";
import "./pages/lighting-control";
import "./pages/lighting-room";
import "./pages/lighting-settings";
import "./pages/moods";
import "./pages/switches";
import "./pages/predictions";
import "./pages/predictions-control";
import "./pages/predictions-room";
import "./pages/presence";
import "./pages/presence-room";
import "./pages/presence-engine";
import "./presence/components";
import "./lighting/light-inspector";
import "./pages/room-editor";
import "./pages/rooms";
import "./pages/settings";
import "./pages/setup";
import "./pages/system";

export class SlApp extends StoreElement {
  static properties = { route: { state: true }, sub: { state: true }, railExpanded: { state: true }, moreOpen: { state: true } };
  declare route: string;
  declare sub: string[];
  private lastHash = window.location.hash;
  declare railExpanded: boolean;
  declare moreOpen: boolean;

  constructor() {
    super();
    this.route = routeFromHash();
    this.sub = subPath();
    this.railExpanded = false;
    this.moreOpen = false;
  }

  /** Pages with unsaved edits: the room editor ("rooms/<area>"), presence editors and the floor plan (all its modes). */
  private editorKey(route: string, sub: string[]): string | null {
    if (route === "rooms" && sub[0]) return `rooms/${sub[0]}`;
    if (route === "presence" && sub[0] && sub[0] !== "diagnostics" && sub[0] !== "engine") return `presence/${sub[0]}`;
    if (route === "floorplan") return "floorplan";
    // Phase A4: Lighting (a room or the settings), Moods and Switches may hold unsaved drafts.
    if (route === "lighting" && sub[0] && sub[0] !== "control") return `lighting/${sub[0]}`;
    if (route === "moods" || route === "switches") return route;
    // Phase A5: a curtain window's editor, the Global Modes / Schedule drafts, and the Predictions settings.
    if (route === "curtains" && sub[0] && sub[0] !== "control") return `curtains/${sub[0]}/${sub[1] ?? ""}`;
    if (route === "predictions" && sub[0] && sub[0] !== "control") return `predictions/${sub[0]}/${sub[1] ?? ""}`;
    // Phase A7.0: the climate settings hold one draft across their sections.
    if (route === "climate" && sub[0] === "settings") return "climate/settings";
    return null;
  }

  private onHashChange = (): void => {
    const route = routeFromHash();
    const sub = subPath();
    const open = this.editorKey(this.route, this.sub);
    if (open && open !== this.editorKey(route, sub)) {
      const warning = leaveWarning();
      if (warning && !window.confirm(warning)) {
        // Stay: put the previous address back without another navigation.
        history.replaceState(null, "", this.lastHash || `#/${this.route}`);
        return;
      }
    }
    this.lastHash = window.location.hash;
    const pageChanged = route !== this.route || this.editorKey(route, sub) !== open;
    this.sub = sub;
    if (pageChanged) {
      this.route = route;
      window.scrollTo({ top: 0 });
      // Move focus to the new page's heading for screen readers and keyboards.
      void this.updateComplete.then(() => this.querySelector<HTMLElement>(".page-header h2, .editor-title h2, .fp-title h2")?.focus({ preventScroll: true }));
    }
    this.railExpanded = false;
    this.closeMore();
  };

  private onKey = (event: KeyboardEvent): void => {
    if (event.key === "Escape" && this.railExpanded) this.railExpanded = false;
  };

  connectedCallback(): void {
    super.connectedCallback();
    window.addEventListener("hashchange", this.onHashChange);
    window.addEventListener("keydown", this.onKey);
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    window.removeEventListener("hashchange", this.onHashChange);
    window.removeEventListener("keydown", this.onKey);
  }

  private onNav(event: MouseEvent, key: string): void {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(key);
    this.railExpanded = false;
    this.closeMore();
  }

  private async openMore(): Promise<void> {
    this.moreOpen = true;
    await this.updateComplete;
    this.querySelector<HTMLDialogElement>("dialog.more-sheet")?.showModal();
  }

  private closeMore(): void {
    const dialog = this.querySelector<HTMLDialogElement>("dialog.more-sheet");
    if (dialog?.open) dialog.close();
    this.moreOpen = false;
  }

  private navLink(item: NavItem): unknown {
    const isActive = item.key === this.route;
    return html`<a
      href="#/${item.key}"
      class="nav-link ${isActive ? "active" : ""}"
      aria-current=${isActive ? "page" : nothing}
      data-nav=${item.key}
      @click=${(e: MouseEvent) => this.onNav(e, item.key)}
    >
      ${icon(item.icon)}
      <span class="label">${item.title}</span>
    </a>`;
  }

  private renderBottomNav(): unknown {
    const inMore = !BOTTOM_NAV.includes(this.route);
    return html`<nav class="bottom-nav" aria-label="Main">
      ${NAV.filter((n) => BOTTOM_NAV.includes(n.key)).map((item) => {
        const isActive = item.key === this.route;
        return html`<a
          href="#/${item.key}"
          class=${isActive ? "active" : ""}
          aria-current=${isActive ? "page" : nothing}
          data-bottom=${item.key}
          @click=${(e: MouseEvent) => this.onNav(e, item.key)}
          >${icon(item.icon)}<span>${item.title}</span></a
        >`;
      })}
      <button
        type="button"
        class=${inMore ? "active" : ""}
        aria-haspopup="dialog"
        data-bottom="more"
        @click=${() => void this.openMore()}
      >
        ${icon("more")}<span>More</span>
      </button>
    </nav>`;
  }

  private renderMoreSheet(): unknown {
    if (!this.moreOpen) return nothing;
    return html`<dialog
      class="sheet sheet-bottom more-sheet"
      aria-labelledby="more-title"
      @close=${() => (this.moreOpen = false)}
      @click=${(e: MouseEvent) => (e.target as Element).matches("dialog") && this.closeMore()}
    >
      <div class="sheet-inner">
        <header class="sheet-head">
          <h2 id="more-title">All modules</h2>
          <button type="button" class="icon-btn" aria-label="Close" @click=${() => this.closeMore()}>${icon("close")}</button>
        </header>
        <nav class="sheet-nav" aria-label="All modules">${NAV.map((item) => this.navLink(item))}</nav>
      </div>
    </dialog>`;
  }

  private renderPage(): unknown {
    switch (this.route) {
      case "dashboard":
        return html`<sl-dashboard></sl-dashboard>`;
      case "diagnostics":
        return html`<sl-diagnostics></sl-diagnostics>`;
      case "settings":
        // Phase A6: #/settings/system[/<tab>] (Migration & System) and #/settings/setup (a new installation).
        if (this.sub[0] === "system") return html`<sl-system .tab=${this.sub[1] ?? "overview"}></sl-system>`;
        if (this.sub[0] === "setup") return html`<sl-setup></sl-setup>`;
        return html`<sl-settings></sl-settings>`;
      case "floorplan":
        return html`<sl-floorplan .mode=${this.sub[0] ?? ""}></sl-floorplan>`;
      case "presence":
        if (this.sub[0] === "settings") return html`<sl-presence-settings></sl-presence-settings>`;
        if (this.sub[0] === "diagnostics") return html`<sl-presence-diagnostics></sl-presence-diagnostics>`;
        if (this.sub[0] === "engine") return html`<sl-presence-engine></sl-presence-engine>`;
        return this.sub[0] ? html`<sl-presence-room .areaId=${this.sub[0]}></sl-presence-room>` : html`<sl-presence></sl-presence>`;
      case "lighting":
        if (this.sub[0] === "control") return html`<sl-lighting-control></sl-lighting-control>`;
        if (this.sub[0] === "settings") return html`<sl-lighting-settings .tab=${this.sub[1] ?? ""}></sl-lighting-settings>`;
        return this.sub[0]
          ? html`<sl-lighting-room .areaId=${this.sub[0]} .tab=${this.sub[1] ?? "overview"}></sl-lighting-room>`
          : html`<sl-lighting></sl-lighting>`;
      case "moods":
        return html`<sl-moods .tab=${this.sub[0] ?? ""}></sl-moods>`;
      case "switches":
        return html`<sl-switches .areaId=${this.sub[0] ?? ""}></sl-switches>`;
      case "rooms":
        return this.sub[0]
          ? html`<sl-room-editor .areaId=${this.sub[0]} .tab=${this.sub[1] ?? "overview"}></sl-room-editor>`
          : html`<sl-rooms></sl-rooms>`;
      case "curtains":
        // #/curtains (overview) · #/curtains/control · #/curtains/windows/<window_id>/<tab> (one window) ·
        // #/curtains/<tab> for modes | schedule | environment | diagnostics | windows (list).
        if (this.sub[0] === "control") return html`<sl-curtains-control></sl-curtains-control>`;
        if (this.sub[0] === "windows" && this.sub[1])
          return html`<sl-curtains-window .windowId=${this.sub[1]} .tab=${this.sub[2] ?? "overview"}></sl-curtains-window>`;
        return html`<sl-curtains .tab=${this.sub[0] ?? "overview"}></sl-curtains>`;
      case "predictions":
        // #/predictions (house) · #/predictions/control · #/predictions/rooms/<area_id>/<tab> ·
        // #/predictions/<tab> for patterns | accuracy | settings.
        if (this.sub[0] === "control") return html`<sl-predictions-control></sl-predictions-control>`;
        if (this.sub[0] === "rooms" && this.sub[1])
          return html`<sl-predictions-room .areaId=${this.sub[1]} .tab=${this.sub[2] ?? "forecast"}></sl-predictions-room>`;
        return html`<sl-predictions .tab=${this.sub[0] ?? "house"}></sl-predictions>`;
      case "climate":
        // #/climate (overview) · #/climate/rooms · #/climate/rooms/<area_id> · #/climate/settings/<section>.
        if (this.sub[0] === "settings") return html`<sl-climate-settings .section=${this.sub[1] ?? "general"}></sl-climate-settings>`;
        if (this.sub[0] === "rooms" && this.sub[1]) return html`<sl-climate-room .areaId=${this.sub[1]}></sl-climate-room>`;
        return html`<sl-climate .tab=${this.sub[0] === "rooms" ? "rooms" : "overview"}></sl-climate>`;
      default:
        return html`<sl-dashboard></sl-dashboard>`;
    }
  }

  protected render(): unknown {
    const overview = store.overview;
    const active = NAV.find((item) => item.key === this.route) ?? NAV[0];
    const link = linkInfo(store.link);
    const offline = store.link === "offline";
    const ha = offline ? link : haConnectionInfo(overview?.home_assistant.connection);

    return html`
      <a class="skip-link" href="#content" @click=${(e: Event) => (e.preventDefault(), this.querySelector<HTMLElement>("#content")?.focus())}
        >Skip to content</a
      >
      <div class="shell ${this.railExpanded ? "rail-expanded" : ""}">
        <aside class="sidebar" aria-label="Navigation">
          <div class="brand">
            <button
              type="button"
              class="icon-btn rail-toggle"
              aria-label=${this.railExpanded ? "Collapse navigation" : "Expand navigation"}
              aria-expanded=${this.railExpanded ? "true" : "false"}
              @click=${() => (this.railExpanded = !this.railExpanded)}
            >
              ${icon(this.railExpanded ? "menu-open" : "menu")}
            </button>
            <span class="brand-mark">${icon("home-automation")}</span>
            <span class="brand-text">Smart Comfort<span class="brand-sub">${overview?.app.subtitle ?? "Intelligent Home Automation"}</span></span>
          </div>
          <nav class="nav" aria-label="Modules">${NAV.map((item) => this.navLink(item))}</nav>
          <div class="sidebar-foot">
            <span class="muted small">v${overview?.app.version ?? "…"}</span>
          </div>
        </aside>
        <div class="scrim" @click=${() => (this.railExpanded = false)}></div>
        <div class="main">
          <header class="topbar">
            <span class="topbar-mark">${icon("home-automation", 22)}</span>
            <h1 class="page-title">${active.title}</h1>
            <span class="topbar-status" data-testid="connection-pill" title=${`App link: ${link.label}`}>
              ${chip(ha.tone, ha.label)}
              <span class="visually-hidden">${offline ? "" : "Home Assistant connection"}</span>
            </span>
          </header>
          <main class="content" id="content" tabindex="-1">${this.renderPage()}</main>
        </div>
        ${this.renderBottomNav()}
      </div>
      ${this.renderMoreSheet()}
      <sl-light-inspector></sl-light-inspector>
    `;
  }
}

customElements.define("sl-app", SlApp);
