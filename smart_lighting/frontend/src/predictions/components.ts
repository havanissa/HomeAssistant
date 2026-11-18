// Phase A5: building blocks shared by the Predictions pages. Two different things are always labelled apart:
// the probability (how likely a room is to be occupied) and the confidence (how reliable the model's forecasts
// for that room are). Neither is ever shown with false precision.
import { html, nothing, type PropertyValues, type TemplateResult } from "lit";

import { icon } from "../icons";
import { store } from "../store";
import { chip, StoreElement, type Tone } from "../ui";
import { problemNotice } from "../lighting/components";
import { ownerLabel, predictionsStatus, type Confidence, type Decision, type DecisionLog, type PredictionsStatus, type RoomForecast } from "./api";
import { haTime } from "../presence/components";

export { problemNotice };

export const CONFIDENCE: Record<string, { label: string; tone: Tone; help: string }> = {
  insufficient_data: { label: "Insufficient data", tone: "neutral", help: "Too little history to say how reliable the forecasts are" },
  low: { label: "Low confidence", tone: "warning", help: "Few days or visits; the forecasts may be off" },
  moderate: { label: "Moderate confidence", tone: "info", help: "A fair amount of history; forecasts usually hold" },
  high: { label: "High confidence", tone: "ok", help: "Two weeks or more, and forecasts that beat the plain schedule" },
};

export const STATUS: Record<string, { label: string; tone: Tone }> = {
  ok: { label: "Forecasting", tone: "ok" },
  learning: { label: "Learning", tone: "pending" },
  insufficient_data: { label: "Not enough data", tone: "neutral" },
  unavailable: { label: "Sensors unavailable", tone: "warning" },
  disabled: { label: "Disabled", tone: "neutral" },
};

export const LEARNING: Record<string, { label: string; tone: Tone }> = {
  waiting: { label: "Waiting", tone: "pending" },
  learning: { label: "Learning", tone: "pending" },
  ready: { label: "Ready", tone: "ok" },
  disabled: { label: "Disabled", tone: "neutral" },
  no_recorder: { label: "No Recorder", tone: "error" },
  error: { label: "Error", tone: "error" },
};

export const FACTOR_KIND: Record<string, string> = {
  schedule: "Schedule", current: "Current state", movement: "Movement", context: "Context", connected: "Connected room",
};

export function confidenceLabel(level: string | null | undefined): string {
  return level ? CONFIDENCE[level]?.label ?? level : "Confidence unknown";
}

export function confidenceChip(level: string | null | undefined, score?: number | null): TemplateResult {
  const info = level ? CONFIDENCE[level] : null;
  const label = info ? info.label : level ?? "Confidence unknown";
  const withScore = info && level !== "insufficient_data" && typeof score === "number" ? `${label} · ${score}/100` : label;
  return html`<span title=${info?.help ?? ""}>${chip(info?.tone ?? "neutral", withScore)}</span>`;
}

export function statusChip(status: string | null | undefined): TemplateResult {
  const info = status ? STATUS[status] : null;
  return chip(info?.tone ?? "neutral", info?.label ?? (status ?? "No forecast"));
}

export function learningChip(state: string | null | undefined): TemplateResult {
  const info = state ? LEARNING[state] : null;
  return chip(info?.tone ?? "neutral", info?.label ?? (state ?? "Unknown"));
}

/** A probability in whole percent, or a dash. ``p`` is 0..100 (the API's percentages). */
export function probabilityText(p: number | null | undefined): string {
  return p === null || p === undefined || !Number.isFinite(p) ? "—" : `${Math.round(p)} %`;
}

/** A fraction 0..1 as whole percent. */
export function fractionText(p: number | null | undefined): string {
  return p === null || p === undefined || !Number.isFinite(p) ? "—" : `${Math.round(p * 100)} %`;
}

/** Whether a forecast carries numbers worth showing (otherwise show its explanation only). */
export function hasNumbers(f: RoomForecast | null | undefined): boolean {
  return !!f && f.status === "ok" && f.probability !== null && f.confidence?.level !== "insufficient_data";
}

/** The headline of a room: "72 % · likely occupied" or the plain explanation. */
export function headline(f: RoomForecast | null | undefined): TemplateResult {
  if (!f) return html`<span class="muted">No forecast yet</span>`;
  if (!hasNumbers(f)) return html`<span class="muted">${f.explanation || STATUS[f.status]?.label || "No forecast"}</span>`;
  return html`<strong class="pr-prob">${probabilityText(f.probability)}</strong>
    <span class="muted"> likely occupied in ${f.horizon} min · ${f.predicted ? "predicted occupied" : "predicted vacant"} (threshold ${f.threshold} %)</span>`;
}

export function confidenceReasons(c: Confidence | null | undefined): TemplateResult | typeof nothing {
  if (!c?.reasons?.length) return nothing;
  return html`<ul class="plain-list small muted pr-reasons">${c.reasons.map((r) => html`<li>${r}</li>`)}</ul>`;
}

export function dayWord(count: number): string {
  return `${count} ${count === 1 ? "day" : "days"}`;
}

/** A decision log (the forecast's observations: nothing in it commanded a device). */
export function decisionList(log: DecisionLog | null | undefined, opts: { empty?: string; limit?: number; name?: (id: string) => string } = {}): TemplateResult {
  const fmt = haTime();
  const entries = (log?.entries ?? []).slice(0, opts.limit ?? 50);
  if (!entries.length) return html`<p class="muted">${opts.empty ?? "Nothing recorded yet."}</p>`;
  return html`<ul class="engine-records pr-log">${entries.map((d: Decision) => html`<li class="engine-record pr-log-entry" data-kind=${d.kind}>
    <span class="pr-log-head"><strong>${d.action}</strong>${chip("neutral", d.status)}${d.count > 1 ? html`<span class="small muted">×${d.count}</span>` : nothing}</span>
    <span class="block small">${d.explanation}</span>
    <span class="block small muted">${fmt(d.at)}${d.target && opts.name ? ` · ${opts.name(d.target)}` : d.target ? ` · ${d.target}` : ""} · ${d.trigger}</span>
  </li>`)}</ul>`;
}

export function observationNotice(): TemplateResult {
  return html`<p class="notice" role="note" data-testid="predictions-observation-only">${icon("info", 20)}<span>Forecasts are observation only: they describe what is likely, and command nothing. No light, curtain or other device is ever switched because of a prediction.</span></p>`;
}

/** Who produces the forecasts and where settings are saved: shown on every predictions page. */
export function controlBanner(status: PredictionsStatus | null): TemplateResult {
  if (!status) return html`<p class="notice" role="status">${icon("info", 20)}<span>Checking who produces the forecasts…</span></p>`;
  const owner = ownerLabel(status.acknowledged_owner);
  const where = status.commands_go_to === "app" ? "saved in the App" : "saved in the integration (it produces the forecasts)";
  const tone: Tone = status.bridge === "connected" ? (owner.app ? "ok" : "info") : "warning";
  return html`<div class="lighting-banner lighting-banner-${owner.app ? "app" : "integration"}" data-testid="predictions-banner">
    ${icon(owner.app ? "crystal-ball" : "home-assistant", 22)}
    <span><strong>${owner.label}</strong> produces the forecasts (observation only). Settings here are ${where}.
      ${status.bridge !== "connected" ? html`<span class="block small tone-warning">${status.bridge_error ?? `Forecast bridge: ${status.bridge}`}</span>` : nothing}</span>
    ${chip(tone, status.mode === "owner" ? "App produces" : status.mode === "shadow" ? "App comparing" : status.mode)}
    <a class="btn btn-small" href="#/predictions/control" data-testid="predictions-control-link">${icon("swap", 16)}Control</a>
  </div>`;
}

/**
 * Base for the predictions pages: the predictions status (who produces the forecasts, where settings go) and a
 * ``reload()`` that runs when the models change (live) or the owner changes.
 */
export class PredictionsElement extends StoreElement {
  static properties = { status: { state: true }, busy: { state: true }, error: { state: true }, notice: { state: true } };
  declare status: PredictionsStatus | null;
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

  /** The name of a room as Home Assistant knows it. */
  roomName(areaId: string | null | undefined): string {
    if (!areaId) return "—";
    return this.status?.house?.rooms.find((r) => r.area_id === areaId)?.name ?? this.status?.area_names[areaId] ?? areaId;
  }

  protected willUpdate(_changed?: PropertyValues): void {
    if (store.predictionsModelsVersion !== this.seenModels || store.predictionsVersion !== this.seenOwner) {
      const first = this.seenModels === -1;
      this.seenModels = store.predictionsModelsVersion;
      this.seenOwner = store.predictionsVersion;
      if (!first && !this.busy) void this.refreshAll();
    }
  }

  async refreshAll(): Promise<void> {
    try {
      this.status = await predictionsStatus();
      this.error = null;
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
      await this.refreshAll();
      return true;
    } catch (err) {
      this.error = err;
      return false;
    } finally {
      this.busy = false;
    }
  }
}

/** What a sub-panel needs from its page. */
export interface PredictionsHost {
  run(action: () => Promise<unknown>, done?: string): Promise<boolean>;
  busy: boolean;
  canEdit: boolean;
}

export function adminNotice(): TemplateResult {
  return html`<p class="notice" role="status">${icon("info", 20)}<span>Only Home Assistant administrators can change predictions settings. Everything here can be read.</span></p>`;
}
