// Phase A5: building blocks shared by the Curtains pages. Everything shown comes from the integration's curtain
// models; nothing here simulates a capability a cover does not report, and nothing moves a cover without the
// explicit confirmation dialogs of the pages.
import { html, nothing, type LitElement, type TemplateResult } from "lit";

import { icon } from "../icons";
import { store } from "../store";
import { chip, StoreElement, type Tone } from "../ui";
import { haTime } from "../presence/components";
import {
  ApiError,
  curtains,
  curtainsStatus,
  loadModel,
  ownerLabel,
  type Caps,
  type ChangeResult,
  type CurtainsModel,
  type CurtainsStatus,
  type Decision,
  type Impact,
  type Obj,
  type PlanItem,
  type Reading,
  type Rule,
  type Transition,
  type WindowMode,
  type WindowModel,
} from "./api";

export { loadModel };

// ------------------------------------------------------------------ words and tones (as the legacy card)
export const MODE_TONE: Record<string, Tone> = { off: "neutral", observing: "info", automatic: "ok" };
export const MODE_ICON: Record<string, string> = { off: "power", observing: "observe", automatic: "robot" };
export const READING_TONE: Record<string, Tone> = { ok: "ok", none: "neutral", missing: "error", unavailable: "warning", stale: "warning", stuck: "error", implausible: "error" };
export const DIRECT_TONE: Record<string, Tone> = { confirmed: "warning", likely: "warning", not_detected: "neutral", uncertain: "warning", not_exposed: "neutral", off: "neutral" };
export const DIRECT_WORD: Record<string, string> = {
  confirmed: "Direct sunlight", likely: "Direct sun likely", not_detected: "Diffuse daylight", uncertain: "Cannot be confirmed",
  not_exposed: "Sun not in front", off: "Sun protection off",
};
export const BAND_TONE: Record<string, Tone> = { cold: "info", comfortable: "ok", warm: "warning", hot: "error" };
export const OVERALL_TONE: Record<string, Tone> = { running: "info", succeeded: "ok", nothing_to_do: "ok", partial: "warning", timeout: "warning", failed: "error", interrupted: "warning" };
export const RESULT_TONE: Record<string, Tone> = {
  reached: "ok", already: "ok", unchanged: "neutral", sent: "info", moving: "info", failed: "error", timeout: "warning", unavailable: "warning",
  missing: "error", unsupported: "warning", busy: "warning", interrupted: "warning",
};
export const PLAN_TONE: Record<string, Tone> = { move: "info", already: "ok", unchanged: "neutral", unavailable: "warning", missing: "error", unsupported: "warning", busy: "warning" };
export const STATUS_TONE: Record<string, Tone> = {
  moved: "ok", moving: "info", would_move: "info", at_target: "ok", keep: "neutral", hold: "warning", blocked: "warning", failed: "error", off: "neutral",
  released: "info", hold_started: "warning", enabled: "ok", resumed: "info", mode: "neutral", config: "neutral",
};
export const SRC_TONE: Record<string, Tone> = { global: "info", room: "warning", window: "warning", cover: "warning", mixed: "neutral" };
export const SRC_TAG: Record<string, string> = { global: "G", room: "R", window: "W", cover: "C", mixed: "M" };
export const SOURCE_WORD: Record<string, string> = { window: "this window", room: "the room", global: "all windows", default: "the default" };
export const KIND_ICON: Record<string, string> = { blackout: "curtains-closed", sheer: "curtains", shade: "roller-shade", custom: "blinds" };
export const COND_ICON: Record<string, string> = {
  sleep: "sleep", night: "weather-night", waiting: "timer-sand", morning: "weather-sunset-up", day: "white-balance-sunny",
  evening_privacy: "eye-off", sun: "weather-sunny-alert", lamp: "lamp", manual_override: "hand", glare: "sunglasses",
  heat: "thermometer-high", solar_heating: "sun-thermometer",
};
export const PHASE_ICON: Record<string, string> = { morning: "weather-sunset-up", day: "white-balance-sunny", evening: "weather-sunset-down", night: "weather-night" };
/** The condition a window is in -> the Global Modes column that sets its preset. */
export const MODE_OF: Record<string, string> = {
  morning: "morning", day: "day", solar_heating: "day", evening_privacy: "evening_privacy", night: "night", sleep: "sleep", sun: "sun",
  glare: "glare", heat: "heat", lamp: "lamp",
};
export const ENV_GROUPS = new Set(["environment", "thermal"]);
export const POLICY_TEXT: Array<[string, string, string]> = [
  ["now", "Apply now", "The automatic controller moves them now, after all its normal safety checks (manual holds, movement limits, competing automations)."],
  ["next_change", "At the next change of condition", "They keep their position now; the new preset is used from their next condition (e.g. when Morning becomes Daytime)."],
];

/** "mdi:weather-night" (as the models carry it) -> the bundled icon name. */
export function mdi(name: string | null | undefined, fallback = "puzzle"): string {
  return name ? name.replace(/^mdi:/, "") : fallback;
}

export const fmt = (iso: string | null | undefined): string => (iso ? haTime(false)(iso) : "—");
export const fmtS = (iso: string | null | undefined): string => (iso ? haTime(true)(iso) : "—");
export const pct = (v: number | null | undefined): string => (v === null || v === undefined ? "—" : `${Math.round(v)} %`);
export const deg = (v: number | null | undefined): string => (v === null || v === undefined ? "—" : `${Math.round(v * 10) / 10}°`);
export const num1 = (v: number | null | undefined): string => (v === null || v === undefined ? "—" : `${Math.round(v * 10) / 10}`);
export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}
export const hm = (minutes: number): string => `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;

// ------------------------------------------------------------------ small renderers
export function modeChip(mode: WindowMode | string, word?: string): TemplateResult {
  return html`<span class="chip chip-${MODE_TONE[mode] ?? "neutral"} cu-mode-chip" data-mode=${mode}>${icon(MODE_ICON[mode] ?? "puzzle", 14)}<span class="chip-label">${word ?? mode}</span></span>`;
}

export function conditionChip(d: Decision | null | undefined, mode?: string): TemplateResult {
  if (mode === "off") return chip("neutral", "Off: not evaluated");
  if (!d) return chip("pending", "Evaluating…");
  return html`<span class="chip chip-${d.condition === "manual_override" ? "warning" : d.condition === "waiting" ? "neutral" : "info"} cu-cond-chip" title=${d.text}>${icon(COND_ICON[d.condition] ?? "robot", 14)}<span class="chip-label">${d.condition_word}</span></span>`;
}

/** A cover's position as a bar (0 % closed, 100 % open) with an optional target marker. A cover without position
 *  reporting only knows open / closed, which the bar says. */
export function positionBar(position: number | null | undefined, caps: Caps | null | undefined, opts: { target?: number | null; name?: string; moving?: boolean } = {}): TemplateResult {
  const known = position !== null && position !== undefined;
  const width = known ? Math.max(0, Math.min(100, position)) : 0;
  const label = `${opts.name ?? "Cover"}: ${known ? `${position} % open` : "position unknown"}${caps && !caps.position ? " (open / close only)" : ""}`;
  return html`<span class="cu-bar ${known ? "" : "cu-bar-unknown"} ${opts.moving ? "cu-bar-moving" : ""}" role="img" aria-label=${label} title=${label}>
    <i class="cu-bar-fill" style="width:${width}%"></i>
    ${opts.target !== null && opts.target !== undefined ? html`<i class="cu-bar-target" style="left:${Math.max(0, Math.min(100, opts.target))}%" title="Target ${opts.target} %"></i>` : nothing}
    ${caps && !caps.position ? html`<span class="cu-bar-note">open / close only</span>` : nothing}
  </span>`;
}

export function coverStateText(l: { missing: boolean; available: boolean; state: string | null; position: number | null; moving: boolean }): TemplateResult {
  if (l.missing) return html`<span class="tone-error">Not found</span>`;
  if (!l.available) return html`<span class="tone-warning">Unavailable</span>`;
  return html`<strong>${pct(l.position)}</strong>${l.moving ? html` ${chip("info", l.state === "opening" ? "Opening" : "Closing")}` : nothing}`;
}

/** "620 lx" / "21.4 °C", with the validity when it is not plain valid. */
export function readingText(x: Reading | null | undefined): TemplateResult {
  if (!x) return html`<span class="muted">—</span>`;
  if (x.status === "none") return html`<span class="muted">not chosen</span>`;
  const val = x.value === null || x.value === undefined ? "" : x.unit === "lx" ? `${Math.round(x.value)} lx` : `${num1(x.value)} ${x.unit}`;
  if (x.status === "ok") return html`${val}`;
  return html`${val ? `${val} ` : ""}<span class="chip chip-${READING_TONE[x.status] ?? "warning"}" title=${x.detail || ""}><span class="chip-label">${x.status_word}</span></span>`;
}

export function readingRow(label: string, x: Reading | null | undefined): TemplateResult {
  if (!x) return html`<li class="cu-reading"><span class="cu-reading-main"><strong>${label}</strong><span class="small muted">not evaluated yet</span></span></li>`;
  const val = x.value === null || x.value === undefined ? "" : x.unit === "lx" ? `${Math.round(x.value)} lx` : `${num1(x.value)} ${x.unit}`;
  return html`<li class="cu-reading">
    <span class="cu-reading-main"><strong>${label}</strong>${val ? html`<span>${val}</span>` : nothing}
      <span class="small muted">${x.entity_id ? html`<code>${x.entity_id}</code>` : nothing}${x.since && x.status !== "none" ? ` · value since ${fmt(x.since)}` : ""}${x.reported && x.status !== "none" ? ` · reported ${fmt(x.reported)}` : ""}</span>
      ${x.detail && x.status !== "ok" ? html`<span class="block small">${x.detail}</span>` : nothing}</span>
    ${chip(READING_TONE[x.status] ?? "neutral", x.status_word)}</li>`;
}

/** The per-cover plan of a preview / enable review / impact: target, current position and decision. */
export function planRows(items: PlanItem[], opts: { reasons?: boolean } = {}): TemplateResult {
  if (!items.length) return html`<p class="muted small">No cover is affected.</p>`;
  return html`<ul class="cu-plan">${items.map((i) => html`<li class="cu-plan-row" data-decision=${i.decision}>
    <span class="cu-plan-icon">${icon(KIND_ICON[i.kind] ?? "blinds", 18)}</span>
    <span class="cu-plan-main"><strong>${i.name}</strong>
      <span class="small muted">${i.target === null || i.target === undefined ? "Leave unchanged" : html`${pct(i.current)} → <strong>${pct(i.target)}</strong>`}</span>
      ${opts.reasons !== false && i.decision !== "unchanged" && i.reason ? html`<span class="block small muted">${i.reason}</span>` : nothing}</span>
    ${chip(PLAN_TONE[i.decision] ?? "neutral", i.word)}</li>`)}</ul>`;
}

export function transitionList(list: Transition[] | undefined, max = 5): TemplateResult {
  if (!list || !list.length) return html`<p class="muted small">None scheduled.</p>`;
  return html`<ul class="cu-transitions">${list.slice(0, max).map((t) => html`<li>${icon(PHASE_ICON[t.phase] ?? "calendar-clock", 16)}
    <time datetime=${t.at}>${t.approx ? "≈ " : ""}${fmt(t.at)}</time> <strong>${t.word}</strong><span class="small muted">${t.reason}</span></li>`)}</ul>`;
}

export function problemList(problems: { severity: string; text: string }[] | string[], tone: "warning" | "error" | "info" = "warning"): TemplateResult {
  if (!problems.length) return html`<p class="muted small">No problems.</p>`;
  return html`<ul class="cu-issues">${problems.map((p) => {
    const sev = typeof p === "string" ? tone : p.severity === "warning" ? "warning" : p.severity === "error" ? "error" : "info";
    return html`<li class="cu-issue cu-issue-${sev}"><span class="tone-${sev}">${icon(sev === "info" ? "info" : sev === "error" ? "error" : "warning", 18)}</span><span>${typeof p === "string" ? p : p.text}</span></li>`;
  })}</ul>`;
}

/** The window's own warnings for a list card: problems plus unavailable or missing covers. */
export function windowWarnings(w: WindowModel): string[] {
  const out = w.problems.filter((p) => p.severity === "warning").map((p) => p.text);
  if (w.auto?.status?.action === "blocked") out.push(`Prevented: ${w.auto.status.text}`);
  return [...new Set(out)];
}

/** Who controls the curtains and where edits go: shown on every curtain page. */
export function controlBanner(status: CurtainsStatus | null): TemplateResult {
  if (!status) return html`<p class="notice" role="status">${icon("info", 20)}<span>Checking who controls the curtains…</span></p>`;
  const owner = ownerLabel(status.acknowledged_owner);
  const where = status.commands_go_to === "app" ? "saved in the App" : "saved in the integration (it controls the curtains)";
  const tone: Tone = status.bridge === "connected" ? (owner.app ? "ok" : "info") : "warning";
  return html`<div class="lighting-banner lighting-banner-${owner.app ? "app" : "integration"}" data-testid="curtains-banner">
    ${icon(owner.app ? "curtains" : "home-assistant", 22)}
    <span><strong>${owner.label}</strong> controls the curtains. Changes here are ${where}.
      ${status.bridge !== "connected" ? html`<span class="block small tone-warning">${status.bridge_error ?? `Curtain bridge: ${status.bridge}`}</span>` : nothing}</span>
    ${chip(tone, status.mode === "owner" ? "App in control" : status.mode === "shadow" ? "App comparing" : status.mode)}
    <a class="btn btn-small" href="#/curtains/control" data-testid="curtains-control-link">${icon("swap", 16)}Control</a>
  </div>`;
}

export function problemNotice(err: unknown): TemplateResult {
  const message = err instanceof Error ? err.message : String(err);
  const code = err instanceof ApiError ? err.code : "error";
  return html`<p class="notice notice-error" role="alert" data-code=${code}>${icon("error", 20)}<span>${message}</span></p>`;
}

export const adminNotice = (): TemplateResult => html`<p class="notice" role="status">${icon("info", 20)}<span>Only Home Assistant administrators can change curtains. Everything is shown read-only.</span></p>`;

// ------------------------------------------------------------------ the base element
/** What a tab panel of the Curtains page needs from its page. */
export interface CurtainsHost {
  run(action: () => Promise<unknown>, done?: string): Promise<boolean>;
  reload(): Promise<void>;
  busy: boolean;
  canEdit: boolean;
  error: unknown;
  status: CurtainsStatus | null;
}

/**
 * Base for the curtain pages: the runtime status (who controls, where edits go) and a ``reload()`` that runs when
 * the models change (live) or the owner changes.
 */
export class CurtainsElement extends StoreElement implements CurtainsHost {
  static properties = { status: { state: true }, busy: { state: true }, error: { state: true }, notice: { state: true } };
  declare status: CurtainsStatus | null;
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
    if (store.curtainsModelsVersion !== this.seenModels || store.curtainsVersion !== this.seenOwner) {
      const first = this.seenModels === -1;
      this.seenModels = store.curtainsModelsVersion;
      this.seenOwner = store.curtainsVersion;
      if (!first && !this.busy) void this.refreshAll();
    }
  }

  async refreshAll(): Promise<void> {
    try {
      this.status = await curtainsStatus();
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

/** Open the first matching <dialog> of an element once it is rendered. */
export async function openDialog(el: LitElement, selector: string): Promise<void> {
  await el.updateComplete;
  const d = el.querySelector<HTMLDialogElement>(selector);
  if (d && !d.open) d.showModal();
}

export function closeDialog(el: LitElement, selector: string): void {
  el.querySelector<HTMLDialogElement>(selector)?.close();
}

// ------------------------------------------------------------------ the review of a change (modes / schedule)
export interface ReviewState {
  title: string;
  base: Obj;              // the command without dry_run / confirm / policy
  result: ChangeResult | null;
  error: string | null;
  policy: "now" | "next_change" | null;
  busy: boolean;
}

/** The dry run of ``base`` as a review; ``save`` sends it with confirm and the chosen policy when needed. */
export class ReviewFlow {
  state: ReviewState | null = null;
  private onSaved: (() => void) | null = null;

  constructor(private readonly el: LitElement, private readonly selector = "dialog.cu-review") {}

  async open(title: string, base: Obj, onSaved?: () => void): Promise<void> {
    this.onSaved = onSaved ?? null;
    const s: ReviewState = { title, base, result: null, error: null, policy: null, busy: true };
    this.state = s;
    this.el.requestUpdate();
    await openDialog(this.el, this.selector);
    const { type, ...params } = base;
    try {
      s.result = await curtains<ChangeResult>(type as string, { ...params, dry_run: true });
    } catch (err) {
      s.error = err instanceof Error ? err.message : String(err);
    }
    s.busy = false;
    this.el.requestUpdate();
  }

  async save(): Promise<boolean> {
    const s = this.state;
    if (!s || !s.result || s.busy) return false;
    if (s.result.needs_confirm && !s.policy) return false;
    s.busy = true;
    s.error = null;
    this.el.requestUpdate();
    const { type, ...params } = s.base;
    try {
      await curtains(type as string, { ...params, confirm: !!s.result.needs_confirm, policy: s.policy ?? "now" });
      this.close();
      this.onSaved?.();
      return true;
    } catch (err) {
      s.busy = false;
      s.error = err instanceof Error ? err.message : String(err);
      this.el.requestUpdate();
      return false;
    }
  }

  close(): void {
    closeDialog(this.el, this.selector);
    this.state = null;
    this.el.requestUpdate();
  }

  render(testid = "curtains-review"): TemplateResult | typeof nothing {
    const s = this.state;
    if (!s) return nothing;
    const res = s.result;
    const needs = !!res?.needs_confirm;
    return html`<dialog class="sheet sheet-full review-dialog cu-review" aria-labelledby="cu-review-title" data-testid=${testid} @close=${() => { this.state = null; this.el.requestUpdate(); }}>
      <div class="sheet-inner">
        <header class="sheet-head"><h2 id="cu-review-title">${s.title}</h2>
          <button type="button" class="icon-btn" aria-label="Close" @click=${() => this.close()}>${icon("close")}</button></header>
        <div class="sheet-body">
          ${s.error ? html`<p class="notice notice-error" role="alert">${icon("error", 20)}<span>${s.error}</span></p>` : nothing}
          ${!res ? (s.busy ? html`<div class="loading" role="status"><span class="spinner"></span><span>Working out what the change would do…</span></div>` : nothing) : html`
            <div class="review-block"><h3>${plural(res.changes.length, "change")}</h3>
              <ul class="change-list">${res.changes.map((c) => html`<li>${icon("check", 16)}<span>${c}</span></li>`)}</ul></div>
            ${res.transitions ? html`<div class="review-block"><h3>Next transitions</h3><div class="cu-two">
              <div><p class="small muted">Now</p>${transitionList(res.transitions.before)}</div>
              <div><p class="small muted">Proposed</p>${transitionList(res.transitions.after)}</div></div></div>` : nothing}
            <div class="review-block"><h3>${res.impact.length ? `Affected windows (${res.impact.length})` : "No window's effective preset changes"}</h3>
              ${impactList(res.impact)}</div>
            ${needs ? html`<div class="review-block"><h3>Automatic windows whose current position would change</h3>
              <div class="cu-radios" role="radiogroup" aria-label="When to apply">${POLICY_TEXT.map(([k, label, hint]) => html`<label class="lp-check">
                <input type="radio" name="cu-policy" value=${k} .checked=${s.policy === k} @change=${() => { s.policy = k as "now" | "next_change"; this.el.requestUpdate(); }} />
                <span><strong>${label}</strong><span class="block small muted">${hint}</span></span></label>`)}</div></div>`
              : html`<p class="notice" role="status">${icon("info", 20)}<span>Saving changes the configuration only; nothing moves because of it.</span></p>`}`}
        </div>
        <footer class="sheet-foot">
          <button type="button" class="btn" ?disabled=${s.busy} @click=${() => this.close()}>Cancel</button>
          <button type="button" class="btn btn-primary" data-testid="curtains-review-save" ?disabled=${s.busy || !res || (needs && !s.policy)} @click=${() => void this.save()}>${icon("save", 18)}${needs ? "Save and confirm" : "Save"}</button>
        </footer>
      </div>
    </dialog>`;
  }
}

export function impactStatus(e: Impact): TemplateResult {
  if (e.mode === "off") return chip("neutral", "Off: not evaluated");
  if (e.hold) return chip("warning", "Manual hold: kept");
  if (!e.active) return chip("neutral", "Not active now: no movement");
  if (e.mode === "observing") return chip("info", "Observing: nothing moves");
  if (e.conflicts.length) return chip("warning", "Blocked by automations");
  return e.moves ? chip("warning", `Would move ${plural(e.moves, "cover")}`) : chip("ok", "Already there");
}

export function impactList(impact: Impact[]): TemplateResult | typeof nothing {
  if (!impact.length) return nothing;
  return html`<ul class="cu-impact">${impact.map((e) => html`<li class="cu-impact-row" data-window=${e.window_id}>
    <div class="cu-impact-head"><strong>${e.name}</strong><span class="small muted">${e.room} · ${e.mode}</span>${impactStatus(e)}</div>
    <div class="small">${e.changes.map((c) => html`<span class="block">${c.word}: ${c.before} → <strong>${c.after}</strong></span>`)}</div>
    ${e.condition ? html`<div class="small muted">Now: <strong>${e.condition_word}</strong>${e.before ? ` (${e.before})` : ""}${e.active && e.predicted ? html` → <strong>${e.predicted_word}</strong>: ${e.after ?? "keeps the position"}` : nothing}</div>` : nothing}
    ${e.blocked && e.status ? html`<div class="small tone-warning">Currently prevented: ${e.status}</div>` : nothing}
    ${e.conflicts.length ? html`<div class="small tone-warning">Enabled automations may also move these covers: ${e.conflicts.join(", ")}. Nothing is sent while they are enabled.</div>` : nothing}
    ${e.active && e.items.length ? planRows(e.items.filter((i) => i.decision !== "unchanged"), { reasons: false }) : nothing}
  </li>`)}</ul>`;
}

// ------------------------------------------------------------------ rule settings (generated from rules.schema)
export interface RuleFieldOpts {
  value: unknown;
  source: string;          // window / room / global / default
  scope: string;           // the scope being edited
  disabled?: boolean;
  draft?: boolean;         // unsaved (a draft form)
  entities?: Record<string, { entity_id: string; name: string }[]>;
  weekdays?: string[];
  onChange: (value: unknown) => void;
  onReset?: (() => void) | null;
}

/** One rule as a form field; ``null`` from ``onReset`` removes the value at this scope (inherits again). */
export function ruleField(rule: Rule, o: RuleFieldOpts): TemplateResult {
  const id = `cu-rule-${o.scope}-${rule.key}`;
  const v = o.value;
  const d = !!o.disabled;
  const own = o.source === o.scope;
  const listId = rule.kind === "entity" ? `cu-dl-${rule.entity_list ?? ({ sleep_entity: "sleep", lux_entity: "lux", lamp_entity: "light" } as Record<string, string>)[rule.key] ?? "sleep"}` : null;
  let input: TemplateResult;
  switch (rule.kind) {
    case "bool":
      input = html`<label class="switch-row lp-switch"><input id=${id} type="checkbox" role="switch" .checked=${!!v} ?disabled=${d} @change=${(e: Event) => o.onChange((e.target as HTMLInputElement).checked)} /><span>${v ? "On" : "Off"}</span></label>`;
      break;
    case "int":
    case "float":
      input = html`<span class="lp-input-row"><input id=${id} type="number" inputmode="decimal" class="lp-num" min=${rule.low ?? nothing} max=${rule.high ?? nothing} step=${rule.step ?? (rule.kind === "float" ? 0.5 : 1)}
        .value=${v === null || v === undefined ? "" : String(v)} ?disabled=${d}
        @change=${(e: Event) => { const raw = (e.target as HTMLInputElement).value.trim(); if (raw !== "") o.onChange(rule.kind === "int" ? Math.round(Number(raw)) : Number(raw)); }} />${rule.unit ? html`<span class="muted">${rule.unit}</span>` : nothing}</span>`;
      break;
    case "time":
      // 24-hour time as text: a native time field shows AM/PM in some browser locales.
      input = html`<span class="lp-input-row"><input id=${id} type="text" class="cu-time" inputmode="numeric" maxlength="5" autocomplete="off" spellcheck="false" pattern="([01][0-9]|2[0-3]):[0-5][0-9]" placeholder="HH:mm"
        .value=${typeof v === "string" ? v : ""} ?disabled=${d} @change=${(e: Event) => { const t = (e.target as HTMLInputElement).value.trim(); if (/^([01]\d|2[0-3]):[0-5]\d$/.test(t)) o.onChange(t); else (e.target as HTMLInputElement).value = typeof v === "string" ? v : ""; }} /><span class="small muted">24-hour</span></span>`;
      break;
    case "choice":
      input = html`<select id=${id} ?disabled=${d} @change=${(e: Event) => o.onChange((e.target as HTMLSelectElement).value)}>${rule.choices.map(([c, l]) => html`<option value=${c} ?selected=${c === v}>${l}</option>`)}</select>`;
      break;
    case "entity":
      input = html`<span class="lp-input-row"><input id=${id} type="text" list=${listId ?? nothing} placeholder="none" .value=${typeof v === "string" ? v : ""} ?disabled=${d}
        @change=${(e: Event) => o.onChange((e.target as HTMLInputElement).value.trim())} />
        ${listId && o.entities ? html`<datalist id=${listId}>${(o.entities[listId.replace("cu-dl-", "")] ?? []).map((i) => html`<option value=${i.entity_id}>${i.name}</option>`)}</datalist>` : nothing}</span>`;
      break;
    case "days": {
      const days = Array.isArray(v) ? (v as number[]) : [];
      input = html`<div class="lp-seg cu-days" role="group" aria-label=${rule.label}>${(o.weekdays ?? ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]).map((name, i) => html`<button type="button" class=${days.includes(i) ? "on" : ""} aria-pressed=${days.includes(i) ? "true" : "false"} ?disabled=${d}
        @click=${() => o.onChange(days.includes(i) ? days.filter((x) => x !== i) : [...days, i].sort())}>${name}</button>`)}</div>`;
      break;
    }
    case "set": {
      const set = Array.isArray(v) ? (v as string[]) : [];
      input = html`<div class="cu-checks">${rule.choices.map(([c, l]) => html`<label class="lp-check"><input type="checkbox" .checked=${set.includes(c)} ?disabled=${d}
        @change=${(e: Event) => o.onChange((e.target as HTMLInputElement).checked ? [...set, c] : set.filter((x) => x !== c))} /><span>${l}</span></label>`)}</div>`;
      break;
    }
    case "dayplan":
      input = html`<span class="small muted">Set under Global Schedule → Advanced (per-day times).</span>`;
      break;
    default:
      input = html`<span>${String(v)}</span>`;
  }
  return html`<div class="lp-field cu-rule" data-rule=${rule.key} data-own=${own ? "1" : "0"}>
    <label class="lp-label" for=${id}>${rule.label}${o.draft ? html` ${chip("warning", "unsaved")}` : nothing}</label>
    ${input}
    <span class="small muted">${rule.hint ? html`${rule.hint} · ` : nothing}${own
      ? (o.onReset && !d ? html`set here · <button type="button" class="lp-link" @click=${() => o.onReset?.()}>Use the inherited value</button>` : "set here")
      : `from ${SOURCE_WORD[o.source] ?? o.source}`}</span>
  </div>`;
}

/** The value of a rule at a scope and where it comes from (the legacy card's ``_ruleValue``). */
export function ruleValue(model: CurtainsModel, w: WindowModel | null, scope: string, key: string): { value: unknown; source: string } {
  const r = model.rules;
  if (scope === "window" && w) return { value: w.auto.values[key], source: w.auto.sources[key] ?? "default" };
  const room = scope === "room" && w?.area_id ? (r.rooms[w.area_id] ?? {}) : {};
  if (scope === "room" && key in room) return { value: room[key], source: "room" };
  if (key in r.global) return { value: r.global[key], source: "global" };
  return { value: r.defaults[key], source: "default" };
}

/** Does saving this value need the simulation first (a confirm-flagged rule switched on / changed from default)? */
export function needsSimulation(rule: Rule, value: unknown): boolean {
  if (!rule.confirm) return false;
  return rule.kind === "bool" ? value === true : value !== rule.default;
}
