// Curtains → Global Schedule (Phase A5): the household phase now, today's timeline, the next transitions, and the
// schedule settings of every window (generated from ``rules.schema``) edited as a draft that is reviewed
// (``schedule/change`` with ``dry_run``: changes, transitions before / after, impact per window) and then saved.
// A phase only makes a mode eligible: sleep, missing daylight and every other protection still apply.
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { setLeaveGuard } from "../router";
import { chip, StoreElement } from "../ui";
import { cmd, type CurtainsModel, type HouseModel, type Obj, type Rule } from "./api";
import { fmt, hm, PHASE_ICON, plural, problemList, problemNotice, ReviewFlow, ruleField, transitionList, type CurtainsHost } from "./components";

const PHASES: Array<[string, string]> = [["morning", "Morning"], ["day", "Daytime"], ["evening", "Evening"], ["night", "Night"]];
const PHASE_KEYS: Record<string, string[]> = {
  morning: ["morning_enabled", "night_end_workday", "night_end_free", "sleep_end", "sleep_require_daylight"],
  day: ["morning_mode", "morning_minutes", "day_from_workday", "day_from_free"],
  evening: ["evening_rule", "evening_event", "evening_time_workday", "evening_time_free"],
  night: ["night_enabled", "night_start_workday", "night_start_free", "late_evenings", "late_days"],
};
const PHASE_NOTES: Record<string, string> = {
  morning: "Morning makes the Morning preset eligible from this time. It is applied only once confirmed daylight is measured and nobody sleeps in a sleep-protected room; in the dark, with a missing sensor or while asleep the covers keep their position. Night ends at this time too. The sleep settings apply to windows with sleep protection on.",
  day: "A window whose Morning could not start (dark, asleep) is not opened by Daytime either: opening always needs daylight. A finished Morning never starts again that day. Sun Protection, Glare and Excessive Heat still win over Daytime.",
  evening: "Evening applies the Evening preset to windows with evening privacy on. Once started it lasts until Night: a passing cloud does not end it. Dusk needs the outdoor light below the dark level for its stable period (or the sun below the dark elevation without a sensor). When neither is available, a time rule still starts Evening at its time; otherwise the reason it waits is shown.",
  night: "Night belongs to the evening it starts on: Friday 23:55 is Friday evening. A time after midnight (e.g. 00:30) still belongs to the evening before. Night ends at the next day's Morning time (that day's own profile).",
};
const DAYPLAN: Array<[string, string]> = [["morning", "Morning"], ["day", "Daytime"], ["evening", "Evening"], ["night", "Night (that evening)"]];

export class SlCurtainsSchedule extends StoreElement {
  static properties = { model: { attribute: false }, host: { attribute: false }, values: { state: true }, open: { state: true }, adv: { state: true } };
  declare model: CurtainsModel;
  declare host: CurtainsHost;
  declare values: Obj;
  declare open: string | null;
  declare adv: boolean;
  private review = new ReviewFlow(this);

  constructor() {
    super();
    this.values = {};
    this.open = null;
    this.adv = false;
  }

  connectedCallback(): void {
    super.connectedCallback();
    setLeaveGuard(() => (Object.keys(this.values).length ? `The Global Schedule has ${plural(Object.keys(this.values).length, "unsaved change")}. Leave without saving?` : null));
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    setLeaveGuard(null);
  }

  private rule(key: string): Rule | undefined {
    return this.model.rules.schema.find((r) => r.key === key);
  }

  private val(key: string): unknown {
    return key in this.values ? this.values[key] : this.model.schedule.values[key];
  }

  private set(key: string, value: unknown): void {
    const next = { ...this.values };
    if (JSON.stringify(value) === JSON.stringify(this.model.schedule.values[key])) delete next[key];
    else next[key] = value;
    this.values = next;
  }

  private openReview(): void {
    if (!Object.keys(this.values).length) return;
    void this.review.open("Review the Global Schedule", { type: cmd("schedule/change"), scope: "global", values: { ...this.values } }, () => { this.values = {}; void this.host.reload(); });
  }

  /** Remove a room / window schedule override ("Use Global Schedule"), reviewed like every schedule change. */
  private clearOverride(o: { scope: string; area_id?: string; window_id?: string; name: string; keys: { key: string }[] }): void {
    const values: Obj = {};
    for (const k of o.keys) values[k.key] = null;
    const base: Obj = { type: cmd("schedule/change"), scope: o.scope, values };
    if (o.scope === "room") base.area_id = o.area_id;
    else base.window_id = o.window_id;
    void this.review.open(`${o.name}: use the Global Schedule`, base, () => void this.host.reload());
  }

  private summary(phase: string): { rule: string; time: string } {
    const v = (k: string) => this.val(k) as string | number | boolean | number[] | undefined;
    const diff = (a: string, b: string) => (v(a) !== v(b) ? ` · free days ${v(b)}` : "");
    const days = this.model.rules.weekdays;
    if (phase === "morning") return v("morning_enabled") ? { rule: `From ${v("night_end_workday")}${diff("night_end_workday", "night_end_free")} · needs confirmed daylight, nobody asleep`, time: String(v("night_end_workday")) } : { rule: "Off: Daytime starts directly when there is daylight", time: "—" };
    if (phase === "day") {
      const m = v("morning_mode");
      if (m === "duration") return { rule: `${v("morning_minutes")} min after Morning begins`, time: `+${v("morning_minutes")} min` };
      return { rule: `At ${v("day_from_workday")}${diff("day_from_workday", "day_from_free")}${m === "not_before" ? `, not before Morning has lasted ${v("morning_minutes")} min` : ""}`, time: String(v("day_from_workday")) };
    }
    if (phase === "evening") {
      const r = v("evening_rule") as string;
      const ev = v("evening_event") === "sunset" ? "sunset" : "dusk";
      const t = `${v("evening_time_workday")}${diff("evening_time_workday", "evening_time_free")}`;
      const rule = ({ legacy: "When it is dark after 12:00 (0.6.4 behaviour, not kept)", time: `At ${t}`, event: `At ${ev}`, first: `${t} or ${ev}, whichever comes first`, last: `${t} or ${ev}, whichever comes last` } as Record<string, string>)[r] ?? r;
      return { rule, time: r === "legacy" || r === "event" ? ev : String(v("evening_time_workday")) };
    }
    if (!v("night_enabled")) return { rule: "Off", time: "—" };
    const late = v("late_evenings") === "custom" ? ((v("late_days") as number[]) ?? []).map((d) => days[d]).join(", ") || "none" : "evenings before a free day";
    return { rule: `${v("night_start_workday")} · late evenings (${late}) ${v("night_start_free")}`, time: String(v("night_start_workday")) };
  }

  private field(key: string): TemplateResult | typeof nothing {
    const rule = this.rule(key);
    if (!rule) return nothing;
    const sources = this.model.schedule.sources;
    return ruleField(rule, {
      value: this.val(key), source: sources[key] ?? "default", scope: "global", disabled: !this.host.canEdit, draft: key in this.values,
      weekdays: this.model.rules.weekdays, entities: this.model.entities, onChange: (v) => this.set(key, v),
      onReset: sources[key] === "global" ? () => this.set(key, null) : null,
    });
  }

  private phaseRow(phase: string, label: string, h: HouseModel): TemplateResult {
    const open = this.open === phase;
    const s = this.summary(phase);
    const keys = PHASE_KEYS[phase].filter((k) => !(phase === "night" && k === "late_days" && this.val("late_evenings") !== "custom"));
    return html`<div class="cu-phase ${h.phase === phase ? "cu-now" : ""}" data-phase=${phase}>
      <button type="button" class="cu-phase-head" aria-expanded=${open ? "true" : "false"} @click=${() => (this.open = open ? null : phase)}>
        ${icon(PHASE_ICON[phase], 20)}<span><strong>${label}</strong>${h.phase === phase ? html` ${chip("ok", "now")}` : nothing}<span class="block cu-phase-rule">${s.rule}</span></span>
        <span class="cu-phase-time">${s.time}</span>${icon(open ? "chevron-down" : "chevron-right", 20)}</button>
      ${open ? html`<div class="cu-phase-body"><div class="lp-form">${keys.map((k) => this.field(k))}</div><p class="small muted">${PHASE_NOTES[phase]}</p></div>` : nothing}
    </div>`;
  }

  private timeline(h: HouseModel): TemplateResult {
    const now = new Date();
    const nowMin = now.getHours() * 60 + now.getMinutes();
    const segs = h.timeline ?? [];
    return html`<div class="cu-timeline" role="img" aria-label=${`Today: ${segs.map((s) => `${s.word} from ${hm(s.from)}`).join(", ")}`}>
        ${segs.map((s) => html`<i class="cu-tl-seg cu-ph-${s.phase}" style="left:${(s.from / 14.4).toFixed(2)}%;width:${((s.to - s.from) / 14.4).toFixed(2)}%" title=${`${s.word} ${hm(s.from)}–${hm(s.to)}`}></i>`)}
        <i class="cu-tl-now" style="left:${(nowMin / 14.4).toFixed(2)}%" title="Now (browser time)"></i></div>
      <div class="cu-tl-axis" aria-hidden="true"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
      <div class="cu-tl-legend">${PHASES.map(([k, l]) => html`<span><i class="cu-ph-${k}"></i>${l}</span>`)}<span class="small muted">≈ Morning and Evening may wait for daylight or dusk · times in ${this.model.schedule.time_zone}</span></div>`;
  }

  private dayplan(): TemplateResult {
    const plan = (this.val("day_overrides") as Record<string, Record<string, string>> | undefined) ?? {};
    const days = this.model.rules.weekdays;
    const free = (this.val("free_days") as number[]) ?? [];
    const can = this.host.canEdit;
    const prof = (i: number, key: string): string => {
      const isFree = free.includes(i);
      if (key === "morning") return String(this.val(isFree ? "night_end_free" : "night_end_workday"));
      if (key === "day") return String(this.val(isFree ? "day_from_free" : "day_from_workday"));
      if (key === "evening") return String(this.val(isFree ? "evening_time_free" : "evening_time_workday"));
      const late = this.val("late_evenings") === "custom" ? ((this.val("late_days") as number[]) ?? []).includes(i) : free.includes((i + 1) % 7);
      return String(this.val(late ? "night_start_free" : "night_start_workday"));
    };
    const change = (i: number, key: string, raw: string) => {
      const t = raw.trim();
      if (t && !/^([01]\d|2[0-3]):[0-5]\d$/.test(t)) return;
      const next: Record<string, Record<string, string>> = {};
      for (const [d, times] of Object.entries(plan)) next[d] = { ...times };
      const day = { ...(next[String(i)] ?? {}) };
      if (t) day[key] = t; else delete day[key];
      if (Object.keys(day).length) next[String(i)] = day; else delete next[String(i)];
      this.set("day_overrides", next);
    };
    return html`<div class="table-wrap"><table class="plain-table cu-dayplan" data-testid="curtains-dayplan"><thead><tr><th scope="col">Day</th>${DAYPLAN.map(([, l]) => html`<th scope="col">${l}</th>`)}</tr></thead>
      <tbody>${days.map((d, i) => html`<tr><th scope="row">${d} <span class="small muted">${free.includes(i) ? "free" : "normal"}</span></th>
        ${DAYPLAN.map(([k, l]) => html`<td><input type="text" class="cu-time" inputmode="numeric" maxlength="5" placeholder=${prof(i, k)} aria-label=${`${d} ${l} (blank = ${prof(i, k)})`} ?disabled=${!can}
          .value=${plan[String(i)]?.[k] ?? ""} @change=${(e: Event) => change(i, k, (e.target as HTMLInputElement).value)} /></td>`)}</tr>`)}</tbody></table></div>
      <p class="small muted">Blank = the normal / free-day time shown in grey. "Night" on Friday is the Night that starts on Friday evening (00:30 there = Saturday 00:30). "Morning" on Saturday is Saturday's earliest Morning and ends Friday's night. All times 24-hour HH:mm.</p>`;
  }

  protected render(): unknown {
    const m = this.model.schedule;
    const h = m.house;
    const next = h.next[0];
    const drafts = Object.keys(this.values).length;
    const groups = Object.fromEntries(this.model.rules.groups.map(([k, title, desc]) => [k, { title, desc }]));
    return html`<div data-testid="curtains-schedule">
      <div class="grid engine-grid">
        <section class="card wide" data-testid="curtains-house"><header class="card-head">${icon("calendar-clock")}<h3>Household phase now</h3><span class="small muted">${m.implementation} · ${m.time_zone}</span></header>
          <div class="cu-summary-row">
            <div class="cu-house">${icon(PHASE_ICON[h.phase] ?? "calendar-clock", 32)}<div><span class="small muted">Now</span><strong class="cu-big" data-phase=${h.phase}>${h.word}</strong>${h.since ? html`<span class="small muted">since ${fmt(h.since)}</span>` : nothing}</div></div>
            <div><p>${h.reason}</p>${h.waiting ? html`<p class="small tone-warning">Waiting: ${h.waiting}</p>` : nothing}
              ${next ? html`<p class="small">Next: <strong>${next.word}</strong> ${next.approx ? "≈ " : ""}${fmt(next.at)} <span class="muted">— ${next.reason}</span></p>` : nothing}
              <div class="cu-chips">${chip("info", m.lux_entity ? `Opening needs daylight from ${m.lux_entity}` : "No outdoor light sensor")}${chip("neutral", "Sleep keeps sleep-protected rooms closed")}
                ${h.sun_events.sunset ? chip("neutral", `Sunset ${fmt(h.sun_events.sunset)}`) : nothing}${h.sun_events.dusk ? chip("neutral", `Dusk ${fmt(h.sun_events.dusk)}`) : nothing}</div></div>
          </div>
          ${this.timeline(h)}
          <p class="small muted">${groups.schedule?.desc ?? "A phase only makes a mode eligible."} Each window still decides on its own (sleep, daylight, sun, lamp, manual holds) and uses its own presets from Global Modes; a window without an assignment keeps its position.</p>
          ${m.warnings.length ? problemList(m.warnings) : nothing}
        </section>
        <section class="card wide" data-testid="curtains-schedule-form"><header class="card-head">${icon("tune")}<h3>When the phases begin</h3>
            ${drafts ? chip("warning", plural(drafts, "unsaved change")) : nothing}</header>
          ${!this.host.canEdit ? html`<p class="notice" role="status">${icon("info", 20)}<span>Only Home Assistant administrators can change the schedule.</span></p>` : nothing}
          ${this.host.error ? problemNotice(this.host.error) : nothing}
          ${drafts ? html`<div class="cu-draftbar" role="status" data-testid="curtains-schedule-draft"><span><strong>${plural(drafts, "unsaved change")}</strong> <span class="small muted">· nothing changes until you review and save</span></span>
            <div class="row-actions"><button type="button" class="btn btn-primary btn-small" data-testid="curtains-schedule-review" @click=${() => this.openReview()}>${icon("eye", 16)}Review & save…</button><button type="button" class="btn btn-small" @click=${() => (this.values = {})}>Discard</button></div></div>` : nothing}
          ${PHASES.map(([k, l]) => this.phaseRow(k, l, h))}
          <h4 class="lp-h">Days</h4>
          <div class="lp-form">${this.field("free_days")}</div>
          <p class="small muted">A free day uses the free-day Morning, Daytime and Evening times. Its night before follows the late evening schedule only when "Late evenings" is "Evenings before a free day".</p>
          <button type="button" class="collapse-toggle lp-adv" aria-expanded=${this.adv ? "true" : "false"} @click=${() => (this.adv = !this.adv)}>${icon(this.adv ? "chevron-down" : "chevron-right", 18)}Advanced: per-day times</button>
          ${this.adv ? this.dayplan() : nothing}
        </section>
        <section class="card" data-testid="curtains-schedule-overrides"><header class="card-head">${icon("layers")}<h3>Room and window schedules</h3></header>
          ${m.overrides.length ? html`<p class="small muted">Values from an earlier version that a room or window still sets. They stay in force until removed here or by the migration.</p>
            <ul class="cu-rows">${m.overrides.map((o) => html`<li class="cu-row"><span class="cu-row-main"><strong>${o.name}</strong><span class="small muted">${o.scope === "room" ? "Room" : "Window"} · ${o.keys.map((k) => `${k.label}: ${k.value}`).join(" · ")}</span></span>
              ${this.host.canEdit ? html`<button type="button" class="btn btn-small" @click=${() => this.clearOverride(o)}>Use Global Schedule…</button>` : nothing}</li>`)}</ul>`
            : html`<p class="muted">Every room and window follows this page. Windows still differ through their presets in Global Modes and their own protections (sleep, lamp, sun, manual moves).</p>`}
        </section>
        <section class="card" data-testid="curtains-schedule-next"><header class="card-head">${icon("history")}<h3>Upcoming and recent</h3></header>
          <h4>Upcoming transitions</h4>${transitionList(h.next, 8)}
          <h4>Recent phases</h4>${h.log.length ? html`<ul class="cu-log">${h.log.slice(0, 12).map((x) => html`<li><time datetime=${x.at}>${fmt(x.at)}</time><span>${x.text}</span></li>`)}</ul>` : html`<p class="muted small">Nothing yet.</p>`}
        </section>
      </div>
      ${this.review.render("curtains-schedule-review-dialog")}
    </div>`;
  }
}

customElements.define("sl-curtains-schedule", SlCurtainsSchedule);
