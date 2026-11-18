// Phase A4: building blocks shared by the Lighting, Moods and Switches pages.
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { store } from "../store";
import { chip, StoreElement } from "../ui";
import { ApiError, lightingStatus, ownerLabel, type LightingStatus } from "./api";

/** A ProfileSetting as the integration's models carry it (``profiles.ProfileSetting.as_dict``). */
export interface Setting {
  action?: "on" | "off" | "inherit" | "ignore" | null;
  brightness_pct?: number | null;
  color_temp_kelvin?: number | null;
  rgb_color?: [number, number, number] | null;
  hs_color?: [number, number] | null;
  effect?: string | null;
  transition?: number | null;
  persistent?: boolean | null;
  brightness_mode?: "fixed" | "global" | "offset" | "ignore" | null;
  brightness_offset?: number | null;
  color_temp_mode?: "fixed" | "global" | "offset" | "ignore" | null;
  color_temp_offset?: number | null;
  daylight?: "dark" | "always" | null;
}

/** What a target can do (``targets.capabilities``). Unknown capabilities are shown as such, never simulated. */
export interface Capabilities {
  known?: boolean;
  on_off?: boolean;
  brightness?: boolean;
  color_temp?: boolean;
  color?: boolean;
  effect?: boolean;
  transition?: boolean;
  min_kelvin?: number | null;
  max_kelvin?: number | null;
  effects?: string[];
  [key: string]: unknown;
}

export function describeSetting(s: Setting | null | undefined): string {
  if (!s) return "Not set (inherits)";
  if (s.action === "off") return "Off";
  if (s.action === "ignore") return "Left unchanged";
  if (s.action === "inherit" && !hasValues(s)) return "Inherits";
  const parts: string[] = [s.action === "on" ? "On" : "Inherited on/off"];
  if (s.brightness_mode === "global") parts.push("global brightness" + (s.brightness_offset ? ` ${signed(s.brightness_offset)} %` : ""));
  else if (s.brightness_mode === "offset") parts.push(`global ${signed(s.brightness_offset ?? 0)} %`);
  else if (s.brightness_pct !== null && s.brightness_pct !== undefined) parts.push(`${s.brightness_pct} %`);
  if (s.color_temp_mode === "global") parts.push("global colour temperature");
  else if (s.color_temp_mode === "offset") parts.push(`global ${signed(s.color_temp_offset ?? 0)} K`);
  else if (s.color_temp_kelvin) parts.push(`${s.color_temp_kelvin} K`);
  if (s.rgb_color) parts.push(`RGB ${s.rgb_color.join(", ")}`);
  if (s.hs_color) parts.push(`HS ${s.hs_color.join(", ")}`);
  if (s.effect) parts.push(`effect ${s.effect}`);
  if (s.transition) parts.push(`${s.transition} s`);
  if (s.daylight === "always") parts.push("also in daylight");
  return parts.join(" · ");
}

function hasValues(s: Setting): boolean {
  return ["brightness_pct", "color_temp_kelvin", "rgb_color", "hs_color", "effect", "transition", "brightness_mode",
    "color_temp_mode"].some((k) => (s as Record<string, unknown>)[k] !== null && (s as Record<string, unknown>)[k] !== undefined);
}

function signed(n: number): string {
  return n > 0 ? `+${n}` : String(n);
}

/** Swatch of a setting's colour (colour temperature as a warm-to-cool tint, RGB as itself). */
export function swatch(s: Setting | null | undefined): TemplateResult | typeof nothing {
  if (!s || s.action === "off" || s.action === "ignore") return nothing;
  let color: string | null = null;
  if (s.rgb_color) color = `rgb(${s.rgb_color.join(",")})`;
  else if (s.hs_color) color = `hsl(${s.hs_color[0]}, ${s.hs_color[1]}%, 50%)`;
  else if (s.color_temp_kelvin) {
    const t = Math.min(1, Math.max(0, (s.color_temp_kelvin - 2000) / 4500));
    color = `rgb(255, ${Math.round(170 + 75 * t)}, ${Math.round(90 + 165 * t)})`;
  }
  const level = s.brightness_pct ?? 100;
  return color ? html`<span class="setting-swatch" style="background:${color};opacity:${0.35 + level / 154}" aria-hidden="true"></span>` : nothing;
}

/**
 * An editor for one profile entry. ``onChange`` receives the new setting (or null to remove the entry).
 * Fields the target cannot use are disabled and say why (nothing is simulated).
 */
export function settingEditor(value: Setting | null, caps: Capabilities | null, onChange: (s: Setting | null) => void,
                              opts: { allMoods?: boolean; vacant?: boolean; disabled?: boolean } = {}): TemplateResult {
  const s: Setting = value ? { ...value } : { action: "inherit" };
  const set = (patch: Partial<Setting>) => onChange({ ...s, ...patch });
  const num = (e: Event): number | null => {
    const raw = (e.target as HTMLInputElement).value.trim();
    return raw === "" ? null : Number(raw);
  };
  const can = (k: keyof Capabilities) => !caps || caps.known === false || caps[k] !== false;
  const why = (k: keyof Capabilities) => (can(k) ? nothing : html`<span class="small muted">not supported by this light</span>`);
  const d = opts.disabled ?? false;
  return html`<div class="setting-editor" data-testid="setting-editor">
    <label class="field"><span>Action</span>
      <select ?disabled=${d} @change=${(e: Event) => set({ action: (e.target as HTMLSelectElement).value as Setting["action"] })}>
        ${(opts.allMoods ? ["inherit", "ignore"] : ["inherit", "on", "off", "ignore"]).map((a) => html`<option value=${a} ?selected=${s.action === a}>${{ inherit: "Inherit", on: "On", off: "Off", ignore: "Leave unchanged" }[a]}</option>`)}
      </select></label>
    ${s.action === "off" || s.action === "ignore" ? nothing : html`
      <label class="field"><span>Brightness</span>
        <select ?disabled=${d || !can("brightness")} @change=${(e: Event) => set({ brightness_mode: ((e.target as HTMLSelectElement).value || null) as Setting["brightness_mode"] })}>
          ${[["", "Inherit"], ["fixed", "Fixed"], ["global", "Follow global"], ["offset", "Global + offset"], ["ignore", "Leave"]].map(([v, l]) => html`<option value=${v} ?selected=${(s.brightness_mode ?? "") === v}>${l}</option>`)}
        </select>${why("brightness")}</label>
      ${s.brightness_mode === "offset"
        ? html`<label class="field"><span>Offset (%)</span><input type="number" min="-99" max="99" .value=${String(s.brightness_offset ?? "")} ?disabled=${d} @change=${(e: Event) => set({ brightness_offset: num(e) })} /></label>`
        : s.brightness_mode !== "global" && s.brightness_mode !== "ignore"
          ? html`<label class="field"><span>Brightness (%)</span><input type="number" min="1" max="100" data-field="brightness_pct" .value=${String(s.brightness_pct ?? "")} ?disabled=${d || !can("brightness")} @change=${(e: Event) => set({ brightness_pct: num(e), brightness_mode: s.brightness_mode ?? (num(e) === null ? null : "fixed") })} /></label>`
          : nothing}
      <label class="field"><span>Colour temperature</span>
        <select ?disabled=${d || !can("color_temp")} @change=${(e: Event) => set({ color_temp_mode: ((e.target as HTMLSelectElement).value || null) as Setting["color_temp_mode"] })}>
          ${[["", "Inherit"], ["fixed", "Fixed"], ["global", "Follow global"], ["offset", "Global + offset"], ["ignore", "Leave"]].map(([v, l]) => html`<option value=${v} ?selected=${(s.color_temp_mode ?? "") === v}>${l}</option>`)}
        </select>${why("color_temp")}</label>
      ${s.color_temp_mode === "offset"
        ? html`<label class="field"><span>Offset (K)</span><input type="number" min="-5000" max="5000" step="50" .value=${String(s.color_temp_offset ?? "")} ?disabled=${d} @change=${(e: Event) => set({ color_temp_offset: num(e) })} /></label>`
        : s.color_temp_mode !== "global" && s.color_temp_mode !== "ignore"
          ? html`<label class="field"><span>Kelvin</span><input type="number" min="1000" max="10000" step="50" data-field="color_temp_kelvin" .value=${String(s.color_temp_kelvin ?? "")} ?disabled=${d || !can("color_temp")} @change=${(e: Event) => set({ color_temp_kelvin: num(e), color_temp_mode: s.color_temp_mode ?? (num(e) === null ? null : "fixed"), rgb_color: null, hs_color: null })} /></label>`
          : nothing}
      <label class="field"><span>RGB</span><input type="color" .value=${rgbHex(s.rgb_color)} ?disabled=${d || !can("color")} @change=${(e: Event) => set({ rgb_color: hexRgb((e.target as HTMLInputElement).value), color_temp_kelvin: null, color_temp_mode: null, hs_color: null })} />${s.rgb_color ? html`<button type="button" class="btn btn-small" ?disabled=${d} @click=${() => set({ rgb_color: null })}>Clear</button>` : nothing}${why("color")}</label>
      ${opts.allMoods ? nothing : html`<label class="field"><span>Effect</span><input type="text" maxlength="64" list="effects" .value=${s.effect ?? ""} ?disabled=${d || !can("effect")} @change=${(e: Event) => set({ effect: (e.target as HTMLInputElement).value.trim() || null })} />${why("effect")}</label>`}
      <label class="field"><span>Transition (s)</span><input type="number" min="0" max="300" step="0.5" .value=${String(s.transition ?? "")} ?disabled=${d} @change=${(e: Event) => set({ transition: num(e) })} /></label>
      ${opts.allMoods ? nothing : html`<label class="field"><span>Daylight</span>
        <select ?disabled=${d} @change=${(e: Event) => set({ daylight: ((e.target as HTMLSelectElement).value || null) as Setting["daylight"] })}>
          ${[["", "Room rules"], ["dark", "Only when dark"], ["always", "Also in daylight"]].map(([v, l]) => html`<option value=${v} ?selected=${(s.daylight ?? "") === v}>${l}</option>`)}
        </select></label>`}
      ${opts.vacant || opts.allMoods ? nothing : html`<label class="field field-check"><input type="checkbox" .checked=${!!s.persistent} ?disabled=${d} @change=${(e: Event) => set({ persistent: (e.target as HTMLInputElement).checked || null })} /><span>Ambient (kept on when the room is vacant)</span></label>`}`}
    <div class="setting-actions"><span class="small muted">${swatch(s)} ${describeSetting(s)}</span>
      ${value ? html`<button type="button" class="btn btn-small" ?disabled=${d} @click=${() => onChange(null)}>${icon("delete", 16)}Remove entry</button>` : nothing}</div>
  </div>`;
}

function rgbHex(rgb: [number, number, number] | null | undefined): string {
  return rgb ? "#" + rgb.map((v) => v.toString(16).padStart(2, "0")).join("") : "#ffffff";
}

function hexRgb(hex: string): [number, number, number] {
  return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)) as [number, number, number];
}

/** Who controls lighting and where edits go: shown on every lighting page. */
export function controlBanner(status: LightingStatus | null): TemplateResult {
  if (!status) return html`<p class="notice" role="status">${icon("info", 20)}<span>Checking who controls lighting…</span></p>`;
  const owner = ownerLabel(status.acknowledged_owner);
  const where = status.commands_go_to === "app" ? "saved in the App" : "saved in the integration (it controls lighting)";
  const tone = status.bridge === "connected" ? (owner.app ? "ok" : "info") : "warning";
  return html`<div class="lighting-banner lighting-banner-${owner.app ? "app" : "integration"}" data-testid="lighting-banner">
    ${icon(owner.app ? "lightbulb-auto" : "home-assistant", 22)}
    <span><strong>${owner.label}</strong> controls the lights. Changes here are ${where}.
      ${status.bridge !== "connected" ? html`<span class="block small tone-warning">${status.bridge_error ?? `Lighting bridge: ${status.bridge}`}</span>` : nothing}</span>
    ${chip(tone, status.mode === "owner" ? "App in control" : status.mode === "shadow" ? "App comparing" : status.mode)}
    <a class="btn btn-small" href="#/lighting/control" data-testid="lighting-control-link">${icon("swap", 16)}Control</a>
  </div>`;
}

export function problemNotice(err: unknown): TemplateResult {
  const message = err instanceof Error ? err.message : String(err);
  const code = err instanceof ApiError ? err.code : "error";
  return html`<p class="notice notice-error" role="alert" data-code=${code}>${icon("error", 20)}<span>${message}</span></p>`;
}

/**
 * Base for the lighting pages: the lighting status (who controls, where edits go) and a ``reload()`` that runs when
 * the models change (live) or the owner changes.
 */
export class LightingElement extends StoreElement {
  static properties = { status: { state: true }, busy: { state: true }, error: { state: true }, notice: { state: true } };
  declare status: LightingStatus | null;
  declare busy: boolean;
  declare error: unknown;
  declare notice: string | null;
  private seenModels = -1;
  private seenOwner = -1;

  constructor() {
    super();
    this.status = null;
    this.busy = false;
    this.error = null;
    this.notice = null;
  }

  connectedCallback(): void {
    super.connectedCallback();
    void this.refreshAll();
  }

  get canEdit(): boolean {
    return this.status?.user.is_admin ?? false;
  }

  protected willUpdate(): void {
    if (store.lightingModelsVersion !== this.seenModels || store.lightingVersion !== this.seenOwner) {
      const first = this.seenModels === -1;
      this.seenModels = store.lightingModelsVersion;
      this.seenOwner = store.lightingVersion;
      if (!first && !this.busy) void this.refreshAll();
    }
  }

  async refreshAll(): Promise<void> {
    try {
      this.status = await lightingStatus();
    } catch (err) {
      this.error = err;
    }
    await this.reload();
  }

  /** Load the page's models (override). */
  async reload(): Promise<void> {}

  /** Run a change; show its error, then reload. */
  async run(action: () => Promise<unknown>, done?: string): Promise<boolean> {
    this.busy = true;
    this.error = null;
    this.notice = null;
    try {
      await action();
      this.notice = done ?? null;
      await this.reload();
      return true;
    } catch (err) {
      this.error = err;
      return false;
    } finally {
      this.busy = false;
    }
  }
}
