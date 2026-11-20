// Phase A4: Switches. Wall switches and remotes per room (whatever the input path: Zigbee2MQTT through MQTT,
// MQTT device triggers or Home Assistant event entities), their button mappings and learning, manual overrides
// across rooms and Priority Inputs. Every change goes through the integration's own lighting commands.
import "../lighting/switches.css";

import { html, nothing, type PropertyValues, type TemplateResult } from "lit";

import { icon } from "../icons";
import { haTime } from "../presence/components";
import { chip, pageHeader } from "../ui";
import { cmd, lighting } from "../lighting/api";
import { controlBanner, LightingElement, problemNotice } from "../lighting/components";
import { mappingFields } from "../lighting/switches-mapping";
import {
  deviceLine,
  MQTT_WORDS,
  mappingMessage,
  OPERATING,
  OVERRIDE_SOURCES,
  sameMapping,
  SHORT_SOURCE,
  SOURCE_WORDS,
  withDefaults,
  type ButtonRow,
  type CandidatesAnswer,
  type Dimming,
  type EditorAnswer,
  type Mapping,
  type Override,
  type RoomModel,
  type SwitchItem,
  type SwitchesModel,
} from "../lighting/switches-model";
import { PriorityController } from "../lighting/switches-priority";

type View = "switches" | "overrides" | "priority";
type Dialog = "add" | "activate" | "remove" | "base" | "resume-room";

interface SettingsDraft {
  id: string;
  name: string;
  override_minutes: number | null;
  source: string;
  topic: string;
  event_entity: string;
  debounce_ms: number;
  activity_evidence: boolean;
  dimming: Dimming;
}

const LEARN_POLL_MS = 2000;
const LEARN_SECONDS = 60;

export class SlSwitches extends LightingElement {
  static properties = {
    ...LightingElement.properties, areaId: { type: String }, tab: { type: String },
    editor: { state: true }, rooms: { state: true }, loadError: { state: true }, view: { state: true },
    selected: { state: true }, mapDrafts: { state: true }, settings: { state: true }, advanced: { state: true },
    dialog: { state: true }, candidates: { state: true }, candidatesError: { state: true }, ack: { state: true },
    manual: { state: true }, manualOpen: { state: true }, baseTopic: { state: true }, resumeArea: { state: true },
  };
  declare areaId: string;
  declare tab: string;
  declare editor: EditorAnswer | null;
  declare rooms: Record<string, RoomModel>;
  declare loadError: unknown;
  declare view: View;
  declare selected: string | null;
  declare mapDrafts: Record<string, Mapping>;
  declare settings: SettingsDraft | null;
  declare advanced: boolean;
  declare dialog: Dialog | null;
  declare candidates: CandidatesAnswer | null;
  declare candidatesError: unknown;
  declare ack: string[];
  declare manual: { topic: string; event_entity: string; name: string };
  declare manualOpen: boolean;
  declare baseTopic: string;
  declare resumeArea: string | null;
  private priority = new PriorityController(this);
  private poll: number | undefined;
  private openDialog = false;

  constructor() {
    super();
    this.areaId = "";
    this.tab = "";
    this.editor = null;
    this.rooms = {};
    this.loadError = null;
    this.view = "switches";
    this.selected = null;
    this.mapDrafts = {};
    this.settings = null;
    this.advanced = false;
    this.dialog = null;
    this.candidates = null;
    this.candidatesError = null;
    this.ack = [];
    this.manual = { topic: "", event_entity: "", name: "" };
    this.manualOpen = false;
    this.baseTopic = "";
    this.resumeArea = null;
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.stopPoll();
  }

  // ------------------------------------------------------------ data
  /** The room shown: the route's, else the first enabled room. */
  private get area(): string | null {
    const rooms = this.editor?.rooms ?? [];
    if (this.areaId && rooms.some((r) => r.area_id === this.areaId)) return this.areaId;
    return rooms[0]?.area_id ?? null;
  }

  private get room(): RoomModel | null {
    const a = this.area;
    return a ? this.rooms[a] ?? null : null;
  }

  async reload(): Promise<void> {
    try {
      const editor = await lighting<EditorAnswer>(cmd("editor"));
      const rooms: Record<string, RoomModel> = {};
      const answers = await Promise.all(editor.rooms.map((r) => lighting<EditorAnswer>(cmd("editor"), { area_id: r.area_id })));
      answers.forEach((a, i) => { if (a.room) rooms[editor.rooms[i].area_id] = a.room; });
      this.editor = editor;
      this.rooms = rooms;
      this.loadError = null;
    } catch (err) {
      this.loadError = err;
    }
  }

  /** One room only (while learning buttons). */
  private async reloadRoom(areaId: string): Promise<void> {
    try {
      const a = await lighting<EditorAnswer>(cmd("editor"), { area_id: areaId });
      if (a.room) this.rooms = { ...this.rooms, [areaId]: a.room };
    } catch (err) {
      this.loadError = err;
    }
  }

  protected updated(changed: PropertyValues): void {
    super.updated(changed);
    const learning = this.room?.switches.items.some((s) => s.learning) ?? false;
    if (learning && this.poll === undefined) {
      this.poll = window.setInterval(() => { const a = this.area; if (a && !this.busy) void this.reloadRoom(a); }, LEARN_POLL_MS);
    } else if (!learning) this.stopPoll();
    if (this.openDialog) {
      this.openDialog = false;
      const el = this.querySelector<HTMLDialogElement>("dialog.sw-main-dialog");
      if (el && !el.open) el.showModal();
    }
  }

  private stopPoll(): void {
    if (this.poll !== undefined) window.clearInterval(this.poll);
    this.poll = undefined;
  }

  private roomName(areaId: string | null): string {
    if (!areaId) return "—";
    return this.editor?.rooms.find((r) => r.area_id === areaId)?.name ?? this.status?.area_names[areaId] ?? areaId;
  }

  private overrides(room: RoomModel): (Override & { name: string })[] {
    return room.lights.filter((l) => l.override).map((l) => ({ ...(l.override as Override), name: l.name || l.entity_id }));
  }

  // ------------------------------------------------------------ dialogs
  private show(kind: Dialog): void {
    this.dialog = kind;
    this.error = null;
    this.openDialog = true;
  }

  private closeDialog(): void {
    this.querySelector<HTMLDialogElement>("dialog.sw-main-dialog")?.close();
    this.dialog = null;
  }

  private async openAdd(): Promise<void> {
    this.candidates = null;
    this.candidatesError = null;
    this.manual = { topic: "", event_entity: "", name: "" };
    this.manualOpen = false;
    this.show("add");
    await this.loadCandidates();
  }

  private async loadCandidates(): Promise<void> {
    const a = this.area;
    if (!a) return;
    this.candidatesError = null;
    try {
      this.candidates = await lighting<CandidatesAnswer>(cmd("switch/candidates"), { area_id: a });
    } catch (err) {
      this.candidatesError = err;
    }
  }

  private async add(params: Record<string, unknown>): Promise<void> {
    const a = this.area;
    if (!a) return;
    let id: string | null = null;
    const ok = await this.run(async () => {
      id = (await lighting<{ switch_id: string }>(cmd("switch/add"), { area_id: a, ...params })).switch_id;
    }, "Switch added · it starts Observing");
    if (ok) {
      this.selected = id;
      this.settings = null;
      this.closeDialog();
    }
  }

  // ------------------------------------------------------------ switch commands
  private async setMode(x: SwitchItem, mode: "listen" | "control"): Promise<void> {
    const a = this.area;
    if (!a || x.mode === mode) return;
    if (mode === "control") {
      this.ack = [];
      this.show("activate");
      return;
    }
    await this.run(() => lighting(cmd("switch/mode"), { area_id: a, switch_id: x.id, mode: "listen" }), `${x.name} is Observing · it sends nothing`);
  }

  private async activate(x: SwitchItem): Promise<void> {
    const a = this.area;
    if (!a) return;
    const ok = await this.run(() => lighting(cmd("switch/mode"), { area_id: a, switch_id: x.id, mode: "control", confirm: true, acknowledge: this.ack }),
      `${x.name} is Active · its assigned buttons send commands`);
    if (ok) this.closeDialog();
  }

  private async learn(x: SwitchItem, action: "start" | "stop"): Promise<void> {
    const a = this.area;
    if (!a) return;
    await this.run(() => lighting(cmd("switch/learn"), { area_id: a, switch_id: x.id, action, ...(action === "start" ? { seconds: LEARN_SECONDS } : {}) }),
      action === "start" ? "Learning: press each button once · nothing is executed" : "Learning finished");
  }

  private draftKey(x: SwitchItem, raw: string): string {
    return `${x.id}|${raw}`;
  }

  private editMapping(x: SwitchItem, b: ButtonRow, patch: Partial<Mapping>): void {
    const sw = this.room!.switches;
    const key = this.draftKey(x, b.raw);
    const base = this.mapDrafts[key] ?? b.mapping ?? { action: "none", target: "room" };
    const next = withDefaults(base, sw, (this.editor?.moods ?? []).map((m) => m.name), patch);
    const drafts = { ...this.mapDrafts };
    if (sameMapping(next, b.mapping)) delete drafts[key];
    else drafts[key] = next;
    this.mapDrafts = drafts;
  }

  private dropDraft(key: string): void {
    const drafts = { ...this.mapDrafts };
    delete drafts[key];
    this.mapDrafts = drafts;
  }

  private async saveMapping(x: SwitchItem, b: ButtonRow): Promise<void> {
    const a = this.area;
    const key = this.draftKey(x, b.raw);
    const draft = this.mapDrafts[key];
    if (!a || !draft) return;
    if (await this.run(() => lighting(cmd("switch/mapping"), { area_id: a, switch_id: x.id, raw: b.raw, mapping: mappingMessage(draft) }),
      `${b.label}: saved · nothing was sent`)) this.dropDraft(key);
  }

  private async mappingCommand(x: SwitchItem, raw: string, extra: Record<string, unknown>, done: string): Promise<void> {
    const a = this.area;
    if (!a) return;
    if (await this.run(() => lighting(cmd("switch/mapping"), { area_id: a, switch_id: x.id, raw, ...extra }), done)) this.dropDraft(this.draftKey(x, raw));
  }

  private editSettings(x: SwitchItem): SettingsDraft {
    if (this.settings?.id === x.id) return this.settings;
    return { id: x.id, name: x.name, override_minutes: x.override_minutes, source: x.source, topic: x.topic ?? "", event_entity: x.event_entity ?? "",
      debounce_ms: x.debounce_ms, activity_evidence: x.activity_evidence, dimming: { ...x.dimming } };
  }

  private changeSettings(x: SwitchItem, patch: Partial<SettingsDraft>): void {
    this.settings = { ...this.editSettings(x), ...patch };
  }

  private settingsChanges(x: SwitchItem, d: SettingsDraft): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    if (d.name.trim() && d.name.trim() !== x.name) out.name = d.name.trim();
    if (d.override_minutes !== x.override_minutes) out.override_minutes = d.override_minutes;
    if (d.source !== x.source) out.source = d.source;
    if ((d.topic.trim() || null) !== (x.topic || null)) out.topic = d.topic.trim() || null;
    if ((d.event_entity.trim() || null) !== (x.event_entity || null)) out.event_entity = d.event_entity.trim() || null;
    if (d.debounce_ms !== x.debounce_ms) out.debounce_ms = d.debounce_ms;
    if (d.activity_evidence !== x.activity_evidence) out.activity_evidence = d.activity_evidence;
    const dim = Object.fromEntries(Object.entries(d.dimming).filter(([k, v]) => x.dimming[k as keyof Dimming] !== v));
    if (Object.keys(dim).length) out.dimming = dim;
    return out;
  }

  private async saveSettings(x: SwitchItem): Promise<void> {
    const a = this.area;
    const d = this.settings;
    if (!a || !d || d.id !== x.id) return;
    const changes = this.settingsChanges(x, d);
    if (!Object.keys(changes).length) { this.settings = null; return; }
    if (await this.run(() => lighting(cmd("switch/set"), { area_id: a, switch_id: x.id, ...changes }), "Switch settings saved · nothing was sent")) this.settings = null;
  }

  private async removeSwitch(x: SwitchItem): Promise<void> {
    const a = this.area;
    if (!a) return;
    if (await this.run(() => lighting(cmd("switch/remove"), { area_id: a, switch_id: x.id }), `${x.name} removed from ${this.roomName(a)}`)) {
      this.selected = null;
      this.settings = null;
      this.closeDialog();
    }
  }

  private async saveBase(): Promise<void> {
    const topic = this.baseTopic.trim();
    if (!topic) return;
    if (await this.run(() => lighting(cmd("switch/settings"), { mqtt_base_topic: topic }), "MQTT base topic saved")) this.closeDialog();
  }

  private async resume(areaId: string, entityId?: string): Promise<void> {
    const ok = await this.run(() => lighting(cmd("override/clear"), { area_id: areaId, ...(entityId ? { entity_id: entityId } : {}) }),
      entityId ? "Automatic lighting resumes for this light at the next trigger" : `Automatic lighting resumes in ${this.roomName(areaId)} at the next trigger`);
    if (ok && !entityId) this.closeDialog();
  }

  // ------------------------------------------------------------ render
  protected render(): unknown {
    const header = pageHeader("Switches", "Wall switches, remotes, manual overrides and Priority Inputs.",
      html`<button type="button" class="btn" ?disabled=${this.busy} @click=${() => void this.refreshAll()}>${icon("refresh", 18)}Refresh</button>`);
    const top = html`${header}${controlBanner(this.status)}
      ${this.error && !this.dialog ? problemNotice(this.error) : nothing}
      ${this.notice ? html`<p class="notice notice-info" role="status" data-testid="sw-notice">${icon("ok", 20)}<span>${this.notice}</span></p>` : nothing}`;
    if (!this.editor) {
      return html`<div class="sw-page">${top}${this.loadError
        ? html`<div class="card empty-state" role="alert">${icon("error", 36)}<h3>The switches could not be loaded</h3>${problemNotice(this.loadError)}
            <button type="button" class="btn" @click=${() => void this.refreshAll()}>${icon("refresh", 18)}Try again</button></div>`
        : html`<div class="card loading" role="status"><span class="spinner" aria-hidden="true"></span><span>Loading switches…</span></div>`}</div>`;
    }
    const overrides = Object.values(this.rooms).reduce((n, r) => n + this.overrides(r).length, 0);
    const views: [View, string, string, number | null][] = [
      ["switches", "Switches and remotes", "light-switch", null],
      ["overrides", "Manual overrides", "account", overrides],
      ["priority", "Priority Inputs", "shield-check", null],
    ];
    const room = this.room;
    let content: unknown = nothing;
    if (this.view === "overrides") content = this.renderOverrides();
    else if (!room) content = html`<div class="card empty-state">${icon("info", 36)}<h3>No enabled room</h3><p class="muted">Enable a room for lighting first.</p></div>`;
    else if (this.view === "priority") content = html`${this.roomBar()}${this.priority.render(room.area_id, room.name, room.priority)}`;
    else content = html`${this.roomBar()}${this.renderSwitches(room)}`;
    return html`<div class="sw-page">${top}
      ${this.loadError ? problemNotice(this.loadError) : nothing}
      <nav class="tabs sw-tabs" role="tablist" aria-label="Switches sections">
        ${views.map(([key, title, ic, count]) => html`<button type="button" role="tab" class="sw-tab ${this.view === key ? "active" : ""}" aria-selected=${this.view === key ? "true" : "false"}
          data-view=${key} @click=${() => { this.view = key; }}>${icon(ic, 18)}<span>${title}</span>${count ? html`<span class="badge">${count}</span>` : nothing}</button>`)}
      </nav>
      ${content}
      ${this.renderDialog()}
    </div>`;
  }

  private roomBar(): TemplateResult {
    const current = this.area;
    return html`<nav class="sw-rooms" aria-label="Rooms">
      ${(this.editor?.rooms ?? []).map((r) => {
        const m = this.rooms[r.area_id];
        const n = this.view === "priority" ? m?.priority?.inputs.length ?? 0 : m?.switches.items.length ?? 0;
        const active = this.view === "priority" ? m?.priority?.inputs.some((i) => i.status?.active) : m?.switches.items.some((s) => s.mode === "control");
        return html`<a class="sw-room ${r.area_id === current ? "active" : ""}" href="#/switches/${encodeURIComponent(r.area_id)}" aria-current=${r.area_id === current ? "page" : "false"}
          @click=${() => { this.selected = null; this.settings = null; }}>
          ${icon("home", 18)}<span>${r.name}</span><span class="sw-count ${active ? "on" : ""}" title=${this.view === "priority" ? "Priority Inputs" : "Switches"}>${n}</span></a>`;
      })}
    </nav>`;
  }

  // ---------------------------------------------------- switches view
  private renderSwitches(room: RoomModel): TemplateResult {
    const sw = room.switches;
    const items = sw.items;
    const x = items.find((s) => s.id === this.selected) ?? items[0] ?? null;
    const mqtt = MQTT_WORDS[sw.mqtt] ?? { tone: "neutral" as const, label: `MQTT ${sw.mqtt}`, text: "" };
    return html`<div class="sw-split">
      <section class="card sw-list-card" data-testid="switch-list">
        <header class="card-head">${icon("light-switch")}<h3>Switches in ${room.name}</h3>
          ${this.canEdit ? html`<button type="button" class="btn btn-small" data-testid="switch-add" ?disabled=${this.busy} @click=${() => void this.openAdd()}>${icon("plus", 16)}Add</button>` : nothing}</header>
        ${items.length
          ? html`<ul class="sw-list">${items.map((s) => {
              const op = OPERATING[s.operating.state] ?? OPERATING.observing;
              const ev = s.status.last_event;
              return html`<li><button type="button" class="sw-row ${x?.id === s.id ? "selected" : ""}" data-switch=${s.id} aria-pressed=${x?.id === s.id ? "true" : "false"}
                  @click=${() => { this.selected = s.id; }}>
                <span class="sw-row-main"><strong>${s.name}</strong>
                  <span class="small muted block">${deviceLine(s) || s.connection}</span>
                  <span class="small muted block">${ev ? `Last: ${ev.label} · ${haTime(false)(ev.at)}` : "No press yet"}</span></span>
                <span class="sw-row-side">${chip(op.tone, op.label)}${s.learning ? chip("pending", "Learning") : nothing}${s.warnings.length ? html`<span class="tone-warning" title="Needs attention">${icon("warning", 18)}</span>` : nothing}</span>
              </button></li>`;
            })}</ul>`
          : html`<div class="empty-state small">${icon("light-switch", 32)}<p>No switch or remote in this room yet.</p>
              ${this.canEdit ? html`<button type="button" class="btn btn-primary" ?disabled=${this.busy} @click=${() => void this.openAdd()}>${icon("plus", 18)}Add a switch or remote</button>` : nothing}</div>`}
        <div class="sw-inputs" data-testid="switch-inputs">
          <h4 class="sw-sub">Input paths</h4>
          <p class="small">${chip(mqtt.tone, mqtt.label)}</p>
          <p class="small muted">${mqtt.text}</p>
          <p class="small">Zigbee2MQTT base topic <span class="mono">${sw.settings.mqtt_base_topic}</span>
            ${this.canEdit ? html`<button type="button" class="btn btn-small" ?disabled=${this.busy} @click=${() => { this.baseTopic = sw.settings.mqtt_base_topic; this.show("base"); }}>${icon("pencil", 16)}Change</button>` : nothing}</p>
          ${sw.legacy_entities.length ? html`<p class="small muted">Also listening (wall switch entities): ${sw.legacy_entities.join(", ")}</p>` : nothing}
        </div>
      </section>
      <section class="sw-inspector" aria-label="Switch">
        ${x ? this.renderInspector(room, sw, x) : html`<div class="card empty-state">${icon("light-switch", 32)}<p class="muted">Add a switch or remote to configure its buttons.</p></div>`}
      </section>
    </div>`;
  }

  private renderInspector(room: RoomModel, sw: SwitchesModel, x: SwitchItem): TemplateResult {
    const op = OPERATING[x.operating.state] ?? OPERATING.observing;
    const on = x.mode === "control";
    const fmt = haTime();
    return html`<div class="sw-stack" data-switch=${x.id}>
      <section class="card" data-testid="switch-head">
        <header class="card-head">${icon("light-switch")}<h3>${x.name}</h3>${chip(op.tone, op.label)}</header>
        <p class="small muted">${[deviceLine(x), x.z2m_name && x.z2m_name !== x.name ? `Zigbee2MQTT: ${x.z2m_name}` : null, x.event_entity_now].filter(Boolean).join(" · ") || "—"}</p>
        <div class="sw-mode" role="group" aria-label="Operating mode of ${x.name}">
          <button type="button" class="sw-seg ${on ? "" : "on"}" aria-pressed=${on ? "false" : "true"} data-mode="listen" ?disabled=${!this.canEdit || this.busy}
            @click=${() => void this.setMode(x, "listen")}>${icon("eye", 18)}<span><strong>Observing</strong><span class="block small">A press only marks the change as a person's action. Nothing is sent.</span></span></button>
          <button type="button" class="sw-seg ${on ? "on" : ""}" aria-pressed=${on ? "true" : "false"} data-mode="control" ?disabled=${!this.canEdit || this.busy || (!on && !x.can_control)}
            @click=${() => void this.setMode(x, "control")}>${icon("tap", 18)}<span><strong>Active</strong><span class="block small">The assigned button actions are executed: Smart Comfort sends the commands.</span></span></button>
        </div>
        <p class="small ${x.operating.state === "blocked" || x.operating.state === "attention" ? "tone-warning" : "muted"}">${x.operating.text}
          ${on && x.activated_at ? html` Active since ${fmt(x.activated_at)}.` : nothing}
          ${!on && !x.can_control ? html`<span class="block tone-warning">Active is not possible while the switch is bound directly to lights in Zigbee2MQTT.</span>` : nothing}</p>
        ${x.warnings.map((w) => html`<p class="notice notice-warning" data-code=${w.code}>${icon("warning", 20)}<span>${w.text}</span></p>`)}
      </section>
      ${this.renderButtons(sw, x)}
      ${this.renderStatus(sw, x)}
      ${this.renderSettings(room, x)}
    </div>`;
  }

  private renderStatus(sw: SwitchesModel, x: SwitchItem): TemplateResult {
    const st = x.status;
    const fmt = haTime();
    const paths = [...new Set([...st.available_sources, ...st.sources])];
    return html`<section class="card" data-testid="switch-status">
      <header class="card-head">${icon("radar")}<h3>Input and status</h3>
        ${st.problem ? chip("warning", "Not received") : st.sources.length ? chip("ok", "Connected") : chip("neutral", "Not connected")}</header>
      <dl class="kv">
        <dt>Received through</dt><dd>${x.connection}</dd>
        <dt>Input paths</dt><dd><span class="sw-sources">${paths.length ? paths.map((p) => html`<span class="sw-source ${st.sources.includes(p) ? "on" : ""}" title=${SOURCE_WORDS[p] ?? p}>
            ${icon(p === "event_entity" ? "home-assistant" : "radar", 14)}${SHORT_SOURCE[p] ?? p}${st.sources.includes(p) ? " · listening" : " · available"}</span>`) : html`<span class="muted">none found</span>`}</span>
          ${(paths.includes("zigbee2mqtt") || paths.includes("device_trigger")) && sw.mqtt !== "connected" ? html`<span class="block small tone-warning">${MQTT_WORDS[sw.mqtt]?.label ?? sw.mqtt}</span>` : nothing}</dd>
        ${st.topic ? html`<dt>MQTT topic</dt><dd class="mono small">${st.topic}</dd>` : nothing}
        ${st.availability ? html`<dt>Device</dt><dd>${st.availability === "online" ? chip("ok", "online") : chip("warning", st.availability)}</dd>` : nothing}
        <dt>Last event</dt><dd>${st.last_event ? html`<strong>${st.last_event.label}</strong> <span class="mono small muted">${st.last_event.raw}</span> · ${fmt(st.last_event.at)} · ${SHORT_SOURCE[st.last_event.source] ?? st.last_event.source}` : "—"}</dd>
        <dt>Last result</dt><dd>${st.last_result ? html`<span class=${st.last_result_ok === false ? "tone-error" : ""}>${st.last_result}</span>` : "—"}</dd>
        <dt>Ignored events</dt><dd>${st.last_ignored ? html`${st.last_ignored.text} · ${fmt(st.last_ignored.at)}` : "none"}
          ${st.ignored_updates ? html`<span class="block small muted">${st.ignored_updates} status update(s) ignored (battery, link quality)</span>` : nothing}</dd>
        ${st.override ? html`<dt>Manual override</dt><dd>${chip("info", "Set by this switch")}</dd>` : nothing}
        ${st.problem ? html`<dt>Problem</dt><dd class="tone-warning">${st.problem}</dd>` : nothing}
      </dl>
      ${st.history.length ? html`<h4 class="sw-sub">Recent presses</h4><ul class="sw-history">${st.history.slice().reverse().slice(0, 6).map((h) => html`<li>
          <span class="muted small">${fmt(h.at)}</span> <strong>${h.label}</strong> <span class="small ${h.ok === false ? "tone-error" : "muted"}">${h.result}</span></li>`)}</ul>` : nothing}
    </section>`;
  }

  private renderButtons(sw: SwitchesModel, x: SwitchItem): TemplateResult {
    const learning = x.learning;
    const moods = (this.editor?.moods ?? []).map((m) => m.name);
    const fmt = haTime();
    const mapped = x.buttons_list.filter((b) => b.mapping && b.mapping.action !== "none").length;
    return html`<section class="card" data-testid="switch-buttons">
      <header class="card-head">${icon("remote")}<h3>Buttons</h3>${chip(mapped ? "info" : "neutral", `${mapped} of ${x.buttons_list.length} assigned`)}
        ${this.canEdit && !learning ? html`<button type="button" class="btn btn-small" data-testid="switch-learn" ?disabled=${this.busy} @click=${() => void this.learn(x, "start")}>${icon("tap", 16)}Learn buttons</button>` : nothing}</header>
      ${learning ? html`<div class="sw-learning" role="status" data-testid="switch-learning">
          <span class="spinner" aria-hidden="true"></span>
          <div><strong>Learning: press each button of ${x.name} once.</strong> Nothing is executed while learning.
            <span class="block small muted">Until ${fmt(learning.until)} · updated every 2 s</span>
            ${learning.captured.length
              ? html`<ul class="sw-captured">${[...new Map(learning.captured.map((c) => [c.raw, c])).values()].map((c) => html`<li>${icon("check", 16)}<strong>${c.label}</strong>
                  <span class="mono small muted">${c.raw}</span>${c.new ? chip("ok", "new") : chip("neutral", "known")}</li>`)}</ul>`
              : html`<span class="block small">Waiting for a press…</span>`}</div>
          ${this.canEdit ? html`<button type="button" class="btn btn-primary btn-small" ?disabled=${this.busy} @click=${() => void this.learn(x, "stop")}>${icon("check", 16)}Done</button>` : nothing}
        </div>` : nothing}
      ${x.buttons_list.length
        ? html`<ul class="sw-buttons">${x.buttons_list.map((b) => {
            const key = this.draftKey(x, b.raw);
            const draft = this.mapDrafts[key];
            const m = draft ?? b.mapping ?? { action: "none", target: "room" };
            return html`<li class="sw-button ${draft ? "dirty" : ""}" data-raw=${b.raw}>
              <div class="sw-button-name"><strong>${b.label}</strong>
                ${b.raw !== b.label ? html`<span class="block mono small muted">${b.raw}</span>` : nothing}
                <span class="block small muted">${[b.press && b.press !== "single" ? b.press : null, b.source === "discovered" ? "declared by the device" : b.source ? `learned via ${SHORT_SOURCE[b.source] ?? b.source}` : null].filter(Boolean).join(" · ")}</span>
                ${draft ? nothing : html`<span class="block small">${b.describe}</span>`}</div>
              ${mappingFields(m, sw, moods, (patch) => this.editMapping(x, b, patch), !this.canEdit || this.busy)}
              ${this.canEdit ? html`<div class="sw-button-actions">
                ${draft ? html`<button type="button" class="btn btn-primary btn-small" data-testid="mapping-save" ?disabled=${this.busy} @click=${() => void this.saveMapping(x, b)}>${icon("save", 16)}Save</button>
                  <button type="button" class="btn btn-small" ?disabled=${this.busy} @click=${() => this.dropDraft(key)}>${icon("undo", 16)}Revert</button>`
                  : html`<button type="button" class="btn btn-small" title="Forget this button (learn it again later)" ?disabled=${this.busy}
                      @click=${() => void this.mappingCommand(x, b.raw, { forget: true }, `${b.label} forgotten`)}>${icon("delete", 16)}Forget</button>`}
              </div>` : nothing}
            </li>`;
          })}</ul>`
        : html`<p class="muted">No buttons known yet. Use “Learn buttons” and press each button once; an unassigned button does nothing.</p>`}
      ${x.declared.length ? html`<p class="small sw-declared">Also declared by the device:
          ${x.declared.map((raw) => this.canEdit ? html`<button type="button" class="btn btn-small" ?disabled=${this.busy}
            @click=${() => void this.mappingCommand(x, raw, { add: true }, `${raw} added`)}>${icon("plus", 14)}${raw}</button>` : html`<span class="mono">${raw}</span> `)}</p>` : nothing}
      ${x.mode === "listen" && mapped ? html`<p class="small muted">The switch is Observing: these actions are not executed until you set it to Active.</p>` : nothing}
    </section>`;
  }

  private renderSettings(room: RoomModel, x: SwitchItem): TemplateResult {
    const d = this.editSettings(x);
    const dirty = this.settings?.id === x.id && Object.keys(this.settingsChanges(x, d)).length > 0;
    const dis = !this.canEdit || this.busy;
    const num = (e: Event): number | null => {
      const raw = (e.target as HTMLInputElement).value.trim();
      return raw === "" ? null : Number(raw);
    };
    const dim = (key: keyof Dimming, label: string, min: number, max: number, step: number, unit: string) => html`<label class="field"><span>${label} (${unit})</span>
      <input type="number" min=${min} max=${max} step=${step} .value=${String(d.dimming[key])} ?disabled=${dis}
        @change=${(e: Event) => { const v = num(e); if (v !== null) this.changeSettings(x, { dimming: { ...d.dimming, [key]: v } }); }} /></label>`;
    const roomMinutes = room.diagnostics.manual_override_minutes;
    return html`<section class="card" data-testid="switch-settings">
      <header class="card-head">${icon("cog")}<h3>Settings</h3>${dirty ? chip("pending", "Not saved") : nothing}</header>
      <div class="sw-form">
        <label class="field"><span>Name</span><input type="text" maxlength="100" .value=${d.name} ?disabled=${dis}
          @input=${(e: Event) => this.changeSettings(x, { name: (e.target as HTMLInputElement).value })} /></label>
        <label class="field"><span>Manual override (minutes)</span><input type="number" min="0" max="1440" placeholder=${roomMinutes == null ? "room setting" : String(roomMinutes)}
          .value=${d.override_minutes == null ? "" : String(d.override_minutes)} ?disabled=${dis} @change=${(e: Event) => this.changeSettings(x, { override_minutes: num(e) })} />
          <span class="small muted">How long a light changed by this switch is left alone. Empty: the room's setting${roomMinutes == null ? "" : ` (${roomMinutes} min)`}; 0: until the room is empty.</span></label>
      </div>
      <button type="button" class="btn btn-small sw-adv-toggle" aria-expanded=${this.advanced ? "true" : "false"} data-testid="switch-advanced"
        @click=${() => { this.advanced = !this.advanced; }}>${icon(this.advanced ? "chevron-down" : "chevron-right", 16)}Advanced</button>
      ${this.advanced ? html`<div class="sw-advanced">
        <h4 class="sw-sub">Input</h4>
        <div class="sw-form">
          <label class="field"><span>Input source</span><select ?disabled=${dis} @change=${(e: Event) => this.changeSettings(x, { source: (e.target as HTMLSelectElement).value })}>
            ${["auto", "zigbee2mqtt", "device_trigger", "event_entity"].map((s) => html`<option value=${s} ?selected=${d.source === s}>${SOURCE_WORDS[s]}</option>`)}</select>
            <span class="small muted">Listening now: ${x.status.sources.map((s) => SHORT_SOURCE[s] ?? s).join(", ") || "nothing"}</span></label>
          <label class="field"><span>MQTT topic</span><input type="text" maxlength="256" .value=${d.topic} placeholder=${x.topic_now ?? `${room.switches.settings.mqtt_base_topic}/…`} ?disabled=${dis}
            @input=${(e: Event) => this.changeSettings(x, { topic: (e.target as HTMLInputElement).value })} /><span class="small muted">Empty: derived from the Zigbee2MQTT name (follows renames).</span></label>
          <label class="field"><span>Event entity</span><input type="text" .value=${d.event_entity} placeholder=${x.event_entity_now ?? "event.…"} ?disabled=${dis}
            @input=${(e: Event) => this.changeSettings(x, { event_entity: (e.target as HTMLInputElement).value })} /></label>
          <label class="field"><span>Debounce (ms)</span><input type="number" min="0" max="2000" .value=${String(d.debounce_ms)} ?disabled=${dis}
            @change=${(e: Event) => { const v = num(e); if (v !== null) this.changeSettings(x, { debounce_ms: v }); }} /><span class="small muted">Repeats faster than this count once.</span></label>
          <label class="sw-check sw-wide"><input type="checkbox" .checked=${d.activity_evidence} ?disabled=${dis} @change=${(e: Event) => this.changeSettings(x, { activity_evidence: (e.target as HTMLInputElement).checked })} />
            <span>A press counts as activity in the room <span class="block small muted">Keeps an occupied room occupied a little longer; never occupies an empty room.</span></span></label>
        </div>
        <h4 class="sw-sub">Dimming</h4>
        <div class="sw-form">
          ${dim("step", "Step per press", 1, 50, 1, "%")}
          ${dim("hold_step", "Hold-to-dim step", 1, 50, 1, "%")}
          ${dim("interval", "Hold-to-dim every", 0.2, 2, 0.1, "s")}
          ${dim("min_brightness", "Lowest brightness", 1, 100, 1, "%")}
          ${dim("max_brightness", "Highest brightness", 1, 100, 1, "%")}
          ${dim("timeout", "Stop dimming after", 1, 60, 1, "s")}
        </div>
        <h4 class="sw-sub">Conflicts</h4>
        ${x.bindings.length ? html`<ul class="sw-history">${x.bindings.map((b) => html`<li class="tone-warning">${icon("link", 16)} Direct binding: ${b.cluster} → ${b.target}${b.endpoint != null ? ` (endpoint ${b.endpoint})` : ""}</li>`)}</ul>`
          : html`<p class="small muted">No direct Zigbee binding known.</p>`}
        ${x.competing.length ? html`<ul class="sw-history">${x.competing.map((a) => html`<li>${icon("robot", 16)} ${a.name || a.entity_id}
            <span class="small muted">${a.enabled ? "enabled" : "disabled"} · ${a.via}${a.buttons.length ? `: ${a.buttons.join(", ")}` : ""}${x.acknowledged.includes(a.entity_id) ? " · acknowledged" : ""}</span></li>`)}</ul>`
          : html`<p class="small muted">No automation reacts to this switch.</p>`}
      </div>` : nothing}
      ${this.canEdit ? html`<div class="sw-actions">
        <button type="button" class="btn btn-primary" data-testid="switch-settings-save" ?disabled=${this.busy || !dirty} @click=${() => void this.saveSettings(x)}>${icon("save", 18)}Save settings</button>
        ${dirty ? html`<button type="button" class="btn" ?disabled=${this.busy} @click=${() => { this.settings = null; }}>${icon("undo", 18)}Revert</button>` : nothing}
        <span class="sw-spacer"></span>
        <button type="button" class="btn btn-danger" data-testid="switch-remove" ?disabled=${this.busy} @click=${() => { this.selected = x.id; this.show("remove"); }}>${icon("delete", 18)}Remove…</button>
      </div>` : nothing}
    </section>`;
  }

  // ---------------------------------------------------- overrides view
  private renderOverrides(): TemplateResult {
    const fmt = haTime(false);
    const rooms = (this.editor?.rooms ?? []).map((r) => ({ r, list: this.rooms[r.area_id] ? this.overrides(this.rooms[r.area_id]) : [] }));
    const total = rooms.reduce((n, x) => n + x.list.length, 0);
    return html`<p class="notice notice-info sw-explain">${icon("account", 20)}<span>A light changed by a person (in Home Assistant, at the device or with a
      wall switch) is left alone by automatic lighting for a while: a <strong>manual override</strong>. “Resume automatic lighting” ends it; the next trigger
      may change the light again. Resuming sends nothing now.</span></p>
      ${total ? nothing : html`<div class="card empty-state" data-testid="overrides-empty">${icon("ok", 32)}<h3>No manual override</h3><p class="muted">Automatic lighting may change every light.</p></div>`}
      <div class="sw-overrides">
      ${rooms.filter((x) => x.list.length).map(({ r, list }) => html`<section class="card" data-testid="overrides-room" data-area=${r.area_id}>
        <header class="card-head">${icon("home")}<h3>${r.name}</h3>${chip("info", `${list.length} light${list.length === 1 ? "" : "s"}`)}
          ${this.canEdit ? html`<button type="button" class="btn btn-small" ?disabled=${this.busy} @click=${() => { this.resumeArea = r.area_id; this.show("resume-room"); }}>${icon("restore", 16)}Resume all…</button>` : nothing}</header>
        <div class="table-wrap"><table class="plain-table">
          <thead><tr><th>Light</th><th>Changed by</th><th>Since</th><th>Until</th><th></th></tr></thead>
          <tbody>${list.map((o) => html`<tr data-entity=${o.entity_id}>
            <td><strong>${o.name}</strong><span class="block mono small muted">${o.entity_id}</span></td>
            <td>${OVERRIDE_SOURCES[o.source] ?? o.source}${o.switch ? html`<span class="block small muted">${o.switch}${o.action ? ` · ${o.action}` : ""}</span>` : nothing}
              ${o.state ? html`<span class="block small muted">switched ${o.state}</span>` : nothing}</td>
            <td class="nowrap">${fmt(o.since)}</td>
            <td class="nowrap">${o.until ? fmt(o.until) : "until the room is empty"}</td>
            <td class="cell-action">${this.canEdit ? html`<button type="button" class="btn btn-small" ?disabled=${this.busy} @click=${() => void this.resume(r.area_id, o.entity_id)}>${icon("restore", 16)}Resume automatic lighting</button>` : nothing}</td>
          </tr>`)}</tbody></table></div>
      </section>`)}
      </div>`;
  }

  // ---------------------------------------------------- dialogs
  private renderDialog(): TemplateResult | typeof nothing {
    if (!this.dialog) return nothing;
    const room = this.room;
    const x = room?.switches.items.find((s) => s.id === this.selected) ?? room?.switches.items[0] ?? null;
    let title = "";
    let body: unknown = nothing;
    let action: unknown = nothing;
    let wide = false;
    switch (this.dialog) {
      case "add":
        title = `Add a switch or remote to ${room?.name ?? ""}`;
        wide = true;
        body = this.addBody(room);
        action = this.manualOpen ? html`<button type="button" class="btn btn-primary" data-testid="switch-add-manual" ?disabled=${this.busy || !(this.manual.topic.trim() || this.manual.event_entity.trim())}
          @click=${() => void this.add({ topic: this.manual.topic.trim() || undefined, event_entity: this.manual.event_entity.trim() || undefined, name: this.manual.name.trim() || undefined })}>${icon("plus", 18)}Add</button>` : nothing;
        break;
      case "activate": {
        if (!x) break;
        const open = x.competing.filter((a) => a.enabled && !x.acknowledged.includes(a.entity_id));
        const ready = x.can_control && open.every((a) => this.ack.includes(a.entity_id));
        title = `Set ${x.name} to Active?`;
        body = html`<p>While it is Active, each assigned button action is executed: <strong>Smart Comfort sends the light, mood, scene or script commands</strong>
            when the button is pressed, whatever the room's automatic lighting mode. Executed actions count as manual changes.</p>
          <p class="muted">While Observing, a press only marks the light change as a person's action; nothing is sent.</p>
          ${x.buttons_list.some((b) => b.mapping && b.mapping.action !== "none") ? nothing : html`<p class="notice notice-warning">${icon("warning", 20)}<span>No button has an action yet: it will be Active but do nothing.</span></p>`}
          ${!x.can_control ? html`<p class="notice notice-error">${icon("error", 20)}<span>Bound directly to lights in Zigbee2MQTT: remove the binding first, or keep it Observing.</span></p>` : nothing}
          ${open.length ? html`<h3 class="sw-sub">Automations that also react to this switch</h3>
            <p class="small muted">A press could be executed twice. Disable or change them first, or tick each one to acknowledge that both may act.</p>
            ${open.map((a) => html`<label class="confirm-row"><input type="checkbox" data-ack=${a.entity_id} .checked=${this.ack.includes(a.entity_id)}
              @change=${(e: Event) => { this.ack = (e.target as HTMLInputElement).checked ? [...this.ack, a.entity_id] : this.ack.filter((v) => v !== a.entity_id); }} />
              <span><strong>${a.name || a.entity_id}</strong><span class="block small muted">${a.entity_id} · ${a.via}${a.buttons.length ? `: ${a.buttons.join(", ")}` : ""}</span></span></label>`)}`
          : html`<p class="small muted">No enabled automation reacts to this switch.</p>`}`;
        action = html`<button type="button" class="btn btn-primary" data-testid="switch-activate" ?disabled=${this.busy || !ready} @click=${() => void this.activate(x)}>${icon("tap", 18)}Set to Active</button>`;
        break;
      }
      case "remove":
        if (!x) break;
        title = `Remove ${x.name}?`;
        body = html`<p>${x.name} is removed from ${room?.name}. Its learned buttons and assignments are deleted. Direct Zigbee bindings and automations are not changed;
          existing manual overrides expire as usual.</p>`;
        action = html`<button type="button" class="btn btn-danger" data-testid="switch-remove-confirm" ?disabled=${this.busy} @click=${() => void this.removeSwitch(x)}>${icon("delete", 18)}Remove</button>`;
        break;
      case "base":
        title = "Zigbee2MQTT base topic";
        body = html`<p class="muted">The base topic of Zigbee2MQTT, for every switch in every room (usually <span class="mono">zigbee2mqtt</span>).</p>
          <label class="field"><span>Base topic</span><input type="text" maxlength="128" .value=${this.baseTopic} @input=${(e: Event) => { this.baseTopic = (e.target as HTMLInputElement).value; }} /></label>`;
        action = html`<button type="button" class="btn btn-primary" ?disabled=${this.busy || !this.baseTopic.trim()} @click=${() => void this.saveBase()}>${icon("save", 18)}Save</button>`;
        break;
      case "resume-room": {
        const area = this.resumeArea;
        const n = area && this.rooms[area] ? this.overrides(this.rooms[area]).length : 0;
        title = `Resume automatic lighting in ${this.roomName(area)}?`;
        body = html`<p>The manual override of ${n} light${n === 1 ? "" : "s"} ends. Nothing is sent now; the next trigger may change these lights again.</p>`;
        action = html`<button type="button" class="btn btn-primary" ?disabled=${this.busy || !area} @click=${() => area && void this.resume(area)}>${icon("restore", 18)}Resume</button>`;
        break;
      }
    }
    return html`<dialog class="sheet sheet-full sw-dialog sw-main-dialog ${wide ? "sw-dialog-wide" : ""}" aria-labelledby="sw-dialog-title" data-testid="sw-dialog"
        @close=${() => { this.dialog = null; }}>
      <div class="sheet-inner">
        <header class="sheet-head"><h2 id="sw-dialog-title">${title}</h2><button type="button" class="icon-btn" aria-label="Close" @click=${() => this.closeDialog()}>${icon("close")}</button></header>
        <div class="sheet-body">${this.error ? problemNotice(this.error) : nothing}${body}</div>
        <footer class="sheet-foot"><button type="button" class="btn" @click=${() => this.closeDialog()}>${this.dialog === "add" ? "Close" : "Cancel"}</button>${action}</footer>
      </div></dialog>`;
  }

  private addBody(room: RoomModel | null): TemplateResult {
    const c = this.candidates;
    const mqtt = c ? MQTT_WORDS[c.mqtt] ?? { tone: "neutral" as const, label: c.mqtt, text: "" } : null;
    return html`<p class="muted">A new switch starts <strong>Observing</strong>: its presses are registered as a person's changes and nothing is executed
        until you set it to Active. Its declared buttons are added.</p>
      ${mqtt ? html`<p class="small">${chip(mqtt.tone, mqtt.label)} ${c?.mqtt_configured && !c.bridge_seen_at ? html`<span class="muted">Reading Zigbee2MQTT's device list…</span>` : nothing}
        <button type="button" class="btn btn-small" ?disabled=${this.busy} @click=${() => void this.loadCandidates()}>${icon("refresh", 16)}Refresh</button></p>` : nothing}
      ${this.candidatesError ? problemNotice(this.candidatesError) : !c
        ? html`<p class="muted" role="status"><span class="spinner" aria-hidden="true"></span> Looking for switches and remotes…</p>`
        : c.candidates.length
          ? html`<div class="table-wrap"><table class="plain-table sw-candidates" data-testid="switch-candidates">
              <thead><tr><th>Device</th><th>Input paths</th><th>Buttons</th><th></th></tr></thead>
              <tbody>${c.candidates.map((d) => html`<tr data-device=${d.device_id ?? d.ieee ?? d.name}>
                <td><strong>${d.name}</strong><span class="block small muted">${[deviceLine(d), d.area ? `area ${d.area}` : null].filter(Boolean).join(" · ") || "—"}</span>
                  ${d.available ? nothing : html`<span class="block small tone-warning">unavailable</span>`}
                  ${d.bindings ? html`<span class="block small tone-warning">${d.bindings} direct binding${d.bindings === 1 ? "" : "s"} to lights</span>` : nothing}</td>
                <td><span class="sw-sources">${d.sources.map((s) => html`<span class="sw-source">${SHORT_SOURCE[s] ?? s}</span>`)}</span>
                  ${d.event_entities.length ? html`<span class="block mono small muted">${d.event_entities.join(", ")}</span>` : nothing}</td>
                <td><span class="small">${d.actions.length ? d.actions.slice(0, 8).join(", ") + (d.actions.length > 8 ? ` +${d.actions.length - 8}` : "") : "learn them after adding"}</span></td>
                <td class="cell-action">${d.assigned_room
                  ? chip("neutral", d.assigned_room === room?.area_id ? "In this room" : `In ${this.roomName(d.assigned_room)}`)
                  : html`<button type="button" class="btn btn-small btn-primary" ?disabled=${this.busy || !this.canEdit}
                      @click=${() => void this.add({ device_id: d.device_id ?? undefined, ieee: d.ieee ?? undefined })}>${icon("plus", 16)}Add</button>`}</td>
              </tr>`)}</tbody></table></div>`
          : html`<p class="muted">No switch or remote found${c.mqtt_configured ? "" : " (MQTT is not set up: only event entities can be found)"}.</p>`}
      <button type="button" class="btn btn-small sw-adv-toggle" aria-expanded=${this.manualOpen ? "true" : "false"} @click=${() => { this.manualOpen = !this.manualOpen; }}>
        ${icon(this.manualOpen ? "chevron-down" : "chevron-right", 16)}Advanced: add by MQTT topic or event entity</button>
      ${this.manualOpen ? html`<div class="sw-form sw-advanced">
        <label class="field"><span>MQTT topic</span><input type="text" maxlength="256" placeholder=${`${c?.base_topic ?? "zigbee2mqtt"}/living_room_switch`} .value=${this.manual.topic}
          @input=${(e: Event) => { this.manual = { ...this.manual, topic: (e.target as HTMLInputElement).value }; }} /></label>
        <label class="field"><span>or event entity</span><input type="text" placeholder="event.living_room_switch_action" .value=${this.manual.event_entity}
          @input=${(e: Event) => { this.manual = { ...this.manual, event_entity: (e.target as HTMLInputElement).value }; }} /></label>
        <label class="field"><span>Name (optional)</span><input type="text" maxlength="100" .value=${this.manual.name}
          @input=${(e: Event) => { this.manual = { ...this.manual, name: (e.target as HTMLInputElement).value }; }} /></label>
      </div>` : nothing}`;
  }
}

customElements.define("sl-switches", SlSwitches);
