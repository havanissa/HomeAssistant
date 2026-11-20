// The light inspector (App 0.9.0, Phase A6.2): one shared drawer that answers "What is controlling this light right
// now?" for any light, relay or group, wherever it is clicked (room overview and lights, brightness matrix, zone
// lighting rules, floor plan, device lookup, diagnostics). Everything comes from ``light/inspect`` (reads only);
// Identify is ``light/identify`` (administrators): full brightness for about 2.5 s, then exactly what it was.
import "./light-inspector.css";
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { chip, kv, StoreElement, type Tone } from "../ui";
import { cmd, lighting } from "./api";
import { fmt, stateText, type Obj } from "./lighting-model";

const EVENT = "sl-inspect-light";

/** Open the inspector for a light (``areaId``: the room it is seen in, when it is managed by several). */
export function inspectLight(entityId: string, areaId?: string | null): void {
  window.dispatchEvent(new CustomEvent(EVENT, { detail: { entityId, areaId: areaId ?? null } }));
}

/** A small "inspect" button for a light row (does not select the row). */
export function inspectButton(entityId: string, areaId?: string | null, name?: string): TemplateResult {
  return html`<button type="button" class="icon-btn lp-inspect-btn" data-inspect=${entityId} title="What controls this light?"
    aria-label=${`Inspect ${name ?? entityId}: what controls it`} @click=${(e: Event) => { e.stopPropagation(); inspectLight(entityId, areaId); }}>${icon("information-outline", 18)}</button>`;
}

const OUTCOME: Record<string, [string, Tone]> = {
  restored: ["Restored", "ok"], superseded: ["Ended early · superseded", "info"], interrupted: ["Ended early · changed meanwhile", "info"],
  failed: ["Failed", "error"], stopped: ["Stopped", "warning"],
};
const KIND: Record<string, string> = { zone: "Zone rule", brightness: "Brightness matrix", priority: "Priority Input", occupied: "Mood profile", vacancy: "Vacancy", shared: "Shared space" };
const SUP: Record<string, [string, Tone]> = {
  correct: ["As expected", "ok"], mismatch: ["Differs", "warning"], correcting: ["Correcting", "info"], waiting: ["Waiting", "neutral"],
  external: ["Changed elsewhere", "warning"], conflict: ["Conflict", "error"], failed_device: ["Device failure?", "error"],
  override: ["Manual override", "info"], paused: ["Paused", "neutral"], pending: ["Applied at next trigger", "neutral"], not_supervised: ["Not supervised", "neutral"],
  no_expectation: ["No expectation", "neutral"], unavailable: ["Unavailable", "warning"],
};

function secs(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const s = Math.max(0, Math.round(n));
  return s >= 90 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${s} s`;
}

function colourText(c: Obj | null | undefined): string {
  if (!c) return "—";
  const parts: string[] = [];
  if (c.color_temp_kelvin) parts.push(`${c.color_temp_kelvin} K`);
  if (c.rgb_color) parts.push(`RGB ${c.rgb_color.join(", ")}`);
  else if (c.hs_color) parts.push(`HS ${c.hs_color.map((v: number) => Math.round(v)).join(", ")}`);
  else if (c.xy_color) parts.push(`XY ${c.xy_color.join(", ")}`);
  if (c.effect && !["none", "off"].includes(String(c.effect))) parts.push(`effect ${c.effect}`);
  return parts.join(" · ") || (c.color_mode ? `${c.color_mode} mode` : "—");
}

export class SlLightInspector extends StoreElement {
  static properties = {
    entityId: { state: true }, areaId: { state: true }, data: { state: true }, error: { state: true },
    running: { state: true }, result: { state: true }, groupPick: { state: true }, tick: { state: true },
  };
  declare entityId: string | null;
  declare areaId: string | null;
  declare data: Obj | null;
  declare error: string | null;
  declare running: { entity: string; started: number; whole: boolean } | null;
  declare result: Obj | null;
  declare groupPick: boolean;
  declare tick: number;
  private timer: number | undefined;
  private returnFocus: HTMLElement | null = null;
  private onOpen = (e: Event): void => {
    const d = (e as CustomEvent<{ entityId: string; areaId: string | null }>).detail;
    this.returnFocus = document.activeElement as HTMLElement | null;
    this.entityId = d.entityId;
    this.areaId = d.areaId;
    this.data = null;
    this.error = null;
    this.result = null;
    this.groupPick = false;
    void this.updateComplete.then(() => {
      const dialog = this.querySelector<HTMLDialogElement>("dialog");
      if (dialog && !dialog.open) dialog.showModal();
    });
    void this.load();
    this.schedule();
  };

  constructor() {
    super();
    this.entityId = null;
    this.areaId = null;
    this.data = null;
    this.error = null;
    this.running = null;
    this.result = null;
    this.groupPick = false;
    this.tick = 0;
  }

  connectedCallback(): void {
    super.connectedCallback();
    window.addEventListener(EVENT, this.onOpen);
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    window.removeEventListener(EVENT, this.onOpen);
    window.clearTimeout(this.timer);
  }

  private schedule(): void {
    window.clearTimeout(this.timer);
    if (!this.entityId) return;
    const busy = this.running || this.data?.identify?.active || this.data?.control?.delay?.pending;
    this.timer = window.setTimeout(() => { this.tick++; void this.load().then(() => this.schedule()); }, busy ? 700 : 4000);
  }

  private close(): void {
    window.clearTimeout(this.timer);
    this.querySelector<HTMLDialogElement>("dialog")?.close();
    this.entityId = null;
    this.data = null;
    this.returnFocus?.focus?.();
  }

  private async load(): Promise<void> {
    const entityId = this.entityId;
    if (!entityId) return;
    try {
      const res = await lighting<Obj>(cmd("light/inspect"), { entity_id: entityId, area_id: this.areaId });
      if (this.entityId === entityId) { this.data = res; this.error = null; }
    } catch (err) {
      if (this.entityId === entityId) this.error = err instanceof Error ? err.message : String(err);
    }
  }

  private async identify(entityId: string, whole: boolean): Promise<void> {
    this.groupPick = false;
    this.result = null;
    this.running = { entity: entityId, started: Date.now(), whole };
    this.schedule();
    try {
      this.result = await lighting<Obj>(cmd("light/identify"), { entity_id: entityId, area_id: this.data?.area_id ?? this.areaId, whole_group: whole });
    } catch (err) {
      this.result = { outcome: "failed", reason: err instanceof Error ? err.message : String(err), entity_id: entityId };
    } finally {
      this.running = null;
      void this.load();
    }
  }

  // ------------------------------------------------------------ sections
  private identifySection(d: Obj): TemplateResult {
    const idf = d.identify ?? {};
    const dev = d.device ?? {};
    const active = idf.active;
    const seconds = Number(idf.seconds ?? 2.5);
    const run = this.running;
    const elapsed = run ? (Date.now() - run.started) / 1000 : null;
    const left = run ? Math.max(0, seconds - (elapsed ?? 0)) : active ? Number(active.remaining ?? 0) : null;
    const members: string[] = dev.members ?? [];
    const isGroup = !!idf.group || members.length > 0;
    const res = this.result;
    const can = !!d.can_identify && idf.available !== false;
    void this.tick;
    return html`<section class="inspector-section li-identify" data-testid="light-identify"><h4>Identify</h4>
      <p class="small muted">Switches ${isGroup ? "the chosen light" : "this light"} to full brightness for about ${seconds} s, then back to exactly how it was. It is not a manual change and changes no setting.</p>
      ${run || active ? html`<div class="li-identify-live" role="status">${icon("lightbulb-on", 22)}
          <strong>${(left ?? 0) > 0.05 ? `Identifying · ${left!.toFixed(1)} s` : "Restoring…"}</strong>
          ${active?.snapshot ? html`<span class="small muted">returns to ${Object.values(active.snapshot).join("; ")}</span>` : nothing}</div>` : nothing}
      ${res && !run ? html`<p class="li-identify-result">${chip(OUTCOME[res.outcome]?.[1] ?? "neutral", OUTCOME[res.outcome]?.[0] ?? res.outcome ?? "—")}
          <span class="small">${res.reason ?? ""}</span></p>` : nothing}
      ${!can ? html`<p class="small tone-warning">${d.can_identify === false ? "Only administrators can identify lights." : idf.why ?? "This light cannot be identified now."}</p>`
        : isGroup ? (this.groupPick ? html`<div class="li-group-pick" role="group" aria-label="Choose a light of the group">
              <p class="small">This is a group. Identify one of its lights, or the whole group (every light in it flashes, and each returns to its own state):</p>
              <ul class="plain-list">${members.map((m) => html`<li><button type="button" class="btn btn-small" ?disabled=${!!run} @click=${() => void this.identify(m, false)}>${icon("lightbulb", 16)}${m}</button></li>`)}
                ${!members.length ? html`<li class="small muted">Its members are not known to Home Assistant (a device group): only the whole group can be identified.</li>` : nothing}</ul>
              <div class="row-actions"><button type="button" class="btn btn-small" @click=${() => (this.groupPick = false)}>Cancel</button>
                <button type="button" class="btn btn-small btn-primary" data-testid="identify-group" ?disabled=${!!run} @click=${() => void this.identify(dev.entity_id, true)}>${icon("lightbulb-group", 16)}Identify the whole group</button></div></div>`
            : html`<button type="button" class="btn" data-testid="identify-open-group" ?disabled=${!!run || !!active} @click=${() => (this.groupPick = true)}>${icon("lightbulb-on", 18)}Identify…</button>`)
        : html`<button type="button" class="btn btn-primary" data-testid="identify" ?disabled=${!!run || !!active} @click=${() => void this.identify(dev.entity_id, false)}>${icon("lightbulb-on", 18)}Identify</button>`}
      ${(idf.recent ?? []).length ? html`<p class="small muted">Last: ${fmt(idf.recent[0].at)} · ${OUTCOME[idf.recent[0].outcome]?.[0] ?? idf.recent[0].outcome}${idf.recent[0].reason ? ` · ${idf.recent[0].reason}` : ""}</p>` : nothing}
    </section>`;
  }

  private controlSection(d: Obj): TemplateResult {
    const c = d.control;
    if (!c) return html`<section class="inspector-section"><h4>Control</h4><p class="muted">${d.configured ? "Its room is not enabled." : "Smart Comfort does not control this light: no room manages it."}</p></section>`;
    const w = c.winning;
    const kind = KIND[w?.kind] ?? w?.kind ?? "—";
    const b = w?.brightness;
    const pend = c.delay?.pending;
    const ext = c.last_external;
    const ov = c.override;
    const occ = c.occupancy ?? {};
    const answer = !w ? "No rule decides it now."
      : ov ? `A manual change: automatic lighting leaves it alone${ov.until ? ` until ${fmt(ov.until)}` : ""}.`
      : c.priority?.claim ? `The Priority Input ${c.priority.claim.winner_name} holds it.`
      : c.manual_mood ? "Nothing: Main Mood is Manual (automatic lighting is paused)."
      : c.mode !== "automatic" ? `Nothing is sent: ${c.room} is Observing. The rule below is what it would do.`
      : `${w.rule ?? kind}.`;
    return html`<section class="inspector-section" data-testid="light-control"><h4>What is controlling it now?</h4>
      <p class="li-answer">${answer}</p>
      ${pend ? html`<p class="notice notice-info li-delay" data-testid="light-delay">${icon("timer-outline", 20)}<span><strong>${pend.action === "on" ? "Delay On" : "Delay Off"} pending</strong> · ${secs(pend.remaining)} left
          <span class="block small">${pend.cause} (${pend.condition}, ${pend.seconds} s)</span></span></p>` : nothing}
      <div class="li-three" role="list">
        <div role="listitem"><span class="li-label">Winning rule wants</span><strong>${w ? stateText(w.requested) : "—"}</strong>
          <span class="small muted">${kind}${w?.decision ? ` · ${w.decision === "change" ? "differs now" : w.decision}` : ""}</span></div>
        <div role="listitem"><span class="li-label">Last command sent</span><strong>${c.last_command ? `${c.last_command.service.replace("_", " ")}${c.last_command.data?.brightness_pct !== undefined ? ` ${c.last_command.data.brightness_pct} %` : ""}` : "none recorded"}</strong>
          <span class="small muted">${c.supervisor_command?.at ? `${fmt(c.supervisor_command.at)} · ${String(c.supervisor_command.purpose ?? "").replace("_", " ")}${c.supervisor_command.error ? ` · failed: ${c.supervisor_command.error}` : ""}` : "by Smart Comfort"}</span></div>
        <div role="listitem"><span class="li-label">Device reports</span><strong>${c.reported?.available === false ? "unavailable" : `${c.reported?.state ?? "—"}${c.reported?.brightness_pct !== null && c.reported?.brightness_pct !== undefined ? ` ${c.reported.brightness_pct} %` : ""}`}</strong>
          <span class="small muted">${colourText(c.reported?.colour)}</span></div>
      </div>
      ${kv([
        ["Why", w?.reason ?? "—"],
        b ? ["Brightness cell", `${b.column_name} · ${b.level_name} → ${b.text}${b.delay_on || b.delay_off ? ` (Delay On ${b.delay_on ?? 0} s, Delay Off ${b.delay_off ?? 0} s)` : ""}`] : null,
        (w?.superseded ?? []).length ? ["Won over", w.superseded.join(", ")] : null,
        b?.explanation && (w?.superseded ?? []).length ? ["Arbitration", b.explanation] : null,
        ["Room mode", c.mode === "automatic" ? chip("ok", "Automatic") : chip("neutral", "Observing")],
        ["Mood", `${c.manual_mood ? "Manual (paused)" : c.mood ?? "no mood"}${c.mood_source ? ` · ${c.mood_source}` : ""}`],
        ["Occupancy", `${String(occ.state ?? "—").replace(/_/g, " ")}${occ.reason ? ` · ${occ.reason}` : ""}`],
        ["Active zone", (c.zones ?? []).length ? c.zones.map((z: Obj) => `${z.name} (priority ${z.priority})`).join(", ") : "none"],
        ["Ambient light", `${c.ambient?.label ?? c.ambient?.name ?? "—"}${c.ambient?.enabled ? "" : " · brightness control off"}`],
        ["Authority", c.authority?.text],
      ])}
      ${ov ? html`<p class="small">${icon("tap", 16)} Manual override (${ov.source ?? "manual"}) since ${fmt(ov.since)}${ov.remaining !== null && ov.remaining !== undefined ? ` · ${secs(ov.remaining)} left` : ov.until ? "" : " · until the room is vacant"}. Resume it in the room's lighting.</p>` : nothing}
      ${ext ? html`<p class="small muted">Last change not made by Smart Comfort: ${fmt(ext.at)} → ${ext.state}, by ${ext.who}.</p>` : nothing}
      ${(c.priority?.inputs ?? []).length ? html`<p class="small">Priority Inputs: ${c.priority.inputs.map((p: Obj) => `${p.name} (${p.status})`).join(", ")}${c.priority.locked ? " · locked" : ""}</p>` : nothing}
      ${c.supervisor ? html`<p class="small">Room Supervisor (${c.supervisor.mode ?? "—"}): ${c.supervisor.result ? chip(SUP[c.supervisor.result]?.[1] ?? "neutral", SUP[c.supervisor.result]?.[0] ?? c.supervisor.result) : "not checked yet"} ${c.supervisor.reason ?? ""}</p>` : nothing}
      ${(c.conflicts ?? []).filter((x: Obj) => x.enabled).map((x: Obj) => html`<p class="small ${x.acknowledged ? "muted" : "tone-warning"}">${icon("robot", 16)} ${x.name ?? x.automation} also controls it${x.acknowledged ? " (allowed)" : ": automatic lighting leaves it alone"}.</p>`)}
    </section>`;
  }

  private deviceSection(d: Obj): TemplateResult {
    const dev = d.device ?? {};
    const caps = dev.capabilities ?? {};
    const can = [caps.brightness ? "dimming" : null, caps.color_temp ? `white ${caps.min_kelvin ?? "?"}–${caps.max_kelvin ?? "?"} K` : null, caps.color ? "colour" : null,
      caps.transition ? "fades" : null, (caps.effects ?? []).length ? `${caps.effects.length} effects` : null].filter(Boolean);
    return html`<section class="inspector-section" data-testid="light-device"><h4>Device</h4>
      ${kv([
        ["Entity", html`<span class="mono">${dev.entity_id}</span>`],
        ["Area", dev.area ?? "—"],
        ["Device", dev.device ? [dev.device.name, dev.device.manufacturer, dev.device.model].filter(Boolean).join(" · ") : "—"],
        ["Integration", dev.integration ?? "—"],
        ["Availability", dev.available ? chip("ok", "Available") : chip("warning", "Unavailable")],
        ["State", `${dev.state ?? "—"}${dev.brightness_pct !== null && dev.brightness_pct !== undefined ? ` · ${dev.brightness_pct} %` : ""}`],
        ["Colour", colourText(dev.colour)],
        ["Can do", dev.relay ? "Relay: on and off only" : dev.onoff_only ? "On and off only" : can.join(", ") || "only what it reports"],
        dev.fixture ? ["Fixture type", dev.fixture.text] : null,
        (dev.members ?? []).length ? ["Group of", dev.members.join(", ")] : null,
        (dev.member_of ?? []).length ? ["Member of", dev.member_of.join(", ")] : null,
        dev.covered_by ? ["Controlled through", dev.covered_by] : null,
        dev.excluded ? ["Not managed", dev.excluded] : null,
        (d.rooms ?? []).length > 1 ? ["Rooms", d.rooms.map((r: Obj) => r.name).join(", ")] : null,
      ])}</section>`;
  }

  private historySection(d: Obj): TemplateResult {
    const h: Obj[] = d.history ?? [];
    const delays: Obj[] = d.control?.delay?.history ?? [];
    return html`<section class="inspector-section" data-testid="light-history"><h4>Recent activity</h4>
      ${h.length ? html`<ol class="li-history">${h.map((e) => html`<li><time class="mono small">${fmt(e.at)}</time>
          <span><strong>${e.decision}</strong> ${chip(e.status === "executed" ? "ok" : e.status === "failed" || e.status === "partial" ? "error" : e.status === "blocked" ? "warning" : "neutral", String(e.status).replace("_", " "))}
            <span class="small muted">${e.attribution}${e.trigger ? ` · ${String(e.trigger).replace(/_/g, " ")}` : ""}${e.count > 1 ? ` · ×${e.count}` : ""}</span>
            <span class="block small">${e.explanation}</span>
            ${(e.service ?? []).length ? html`<span class="block small mono muted">${e.service.join("; ")}</span>` : nothing}
            ${e.errors ? html`<span class="block small tone-error">${e.errors}</span>` : nothing}</span></li>`)}</ol>`
        : html`<p class="small muted">No decision about this light is in the room's log yet.</p>`}
      ${delays.length ? html`<h4>Brightness delays</h4><ul class="plain-list small">${delays.map((x) => html`<li>${fmt(x.at)} · ${x.action === "on" ? "Delay On" : "Delay Off"} ${x.event}${x.reason ? ` (${x.reason})` : ""} · ${x.condition ?? ""}</li>`)}</ul>` : nothing}
      <p class="small muted">From the room's decision log and Room Supervisor log (kept while the App runs; the newest ${h.length || 25}).</p>
    </section>`;
  }

  protected render(): unknown {
    const d = this.data;
    const dev = d?.device ?? {};
    return html`<dialog class="sheet sheet-full light-inspector-dialog" aria-label="Light inspector" @close=${() => { this.entityId = null; window.clearTimeout(this.timer); }}
        @click=${(e: MouseEvent) => (e.target as Element).matches("dialog") && this.close()}>
      ${this.entityId ? html`<div class="sheet-inner li-inner" data-testid="light-inspector">
        <header class="inspector-head"><span class="inspector-icon">${icon(dev.relay ? "electric-switch" : (dev.members ?? []).length ? "lightbulb-group" : "lightbulb", 22)}</span>
          <div><h3>${dev.name ?? this.entityId}</h3><span class="muted small mono">${this.entityId}</span>
            ${d ? html`<span class="chips">${chip(dev.available ? (dev.state === "on" ? "ok" : "neutral") : "warning", dev.available ? (dev.state ?? "—") : "unavailable")}
              ${d.control?.override ? chip("info", "manual override") : nothing}${d.control?.delay?.pending ? chip("info", "delay pending") : nothing}</span>` : nothing}</div>
          <button type="button" class="icon-btn" aria-label="Close" @click=${() => this.close()}>${icon("close")}</button></header>
        <div class="li-body">
          ${this.error ? html`<p class="notice notice-error">${icon("error", 20)}<span>${this.error}</span></p>` : nothing}
          ${!d && !this.error ? html`<p class="muted" role="status"><span class="spinner" aria-hidden="true"></span> Loading…</p>` : nothing}
          ${d ? html`${this.controlSection(d)}${this.identifySection(d)}${this.deviceSection(d)}${this.historySection(d)}` : nothing}
        </div></div>` : nothing}
    </dialog>`;
  }
}

customElements.define("sl-light-inspector", SlLightInspector);
