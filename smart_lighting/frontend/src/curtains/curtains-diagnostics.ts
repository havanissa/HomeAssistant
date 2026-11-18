// Curtains → Diagnostics (Phase A5): why windows do what they do, for the whole house - the merged log of
// automatic decisions, operations and manual changes (newest first, with a window filter), safety blocks,
// competing automations with the "allow / revoke" review (``automation/allow``), busy covers, the household log,
// the configuration export and the reviewed import, and the App's backups. Automations are only listed.
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { apiUrl } from "../store";
import { chip, StoreElement } from "../ui";
import { applyImport, cmd, curtains, reviewImport, type CurtainsModel, type ImportReview, type WindowModel } from "./api";
import { closeDialog, fmt, fmtS, modeChip, openDialog, OVERALL_TONE, pct, plural, problemNotice, RESULT_TONE, STATUS_TONE, type CurtainsHost } from "./components";

interface Line {
  at: string;
  window: WindowModel;
  kind: "decision" | "operation" | "manual";
  word: string;
  tone: string;
  text: string;
  detail?: string;
}

interface ImportState {
  file: unknown;
  review: ImportReview | null;
  error: string | null;
  busy: boolean;
}

export class SlCurtainsDiagnostics extends StoreElement {
  static properties = { model: { attribute: false }, host: { attribute: false }, filter: { state: true }, kinds: { state: true }, imp: { state: true }, opOpen: { state: true } };
  declare model: CurtainsModel;
  declare host: CurtainsHost;
  declare filter: string;
  declare kinds: Set<string>;
  declare imp: ImportState | null;
  declare opOpen: string | null;

  constructor() {
    super();
    this.filter = "";
    this.kinds = new Set(["decision", "operation", "manual"]);
    this.imp = null;
    this.opOpen = null;
  }

  private lines(): Line[] {
    const out: Line[] = [];
    for (const w of this.model.windows) {
      if (this.filter && w.window_id !== this.filter) continue;
      for (const x of w.auto?.log ?? []) {
        if (!x.at) continue;
        out.push({ at: x.at, window: w, kind: "decision", word: x.word, tone: STATUS_TONE[x.action] ?? "neutral", text: x.summary ?? x.text,
          detail: [x.condition ? `condition ${x.condition}` : null, x.preset_name ?? x.preset ? `preset ${x.preset_name ?? x.preset}` : null, x.summary && x.summary !== x.text ? x.text : null].filter(Boolean).join(" · ") });
      }
      for (const op of [w.last, ...w.history].filter((o): o is NonNullable<typeof o> => !!o)) {
        if (!op.started) continue;  // the same operation may be both "last" and first in the history: de-duplicated below
        out.push({ at: op.started, window: w, kind: "operation", word: this.model.words.overall[op.status] ?? op.status, tone: OVERALL_TONE[op.status] ?? "neutral", text: `${op.preset_name}: ${op.explanation}`,
          detail: op.items.map((i) => `${i.name} ${i.target === null ? "unchanged" : `${pct(i.start_position)} → ${pct(i.target)} (${this.model.words.results[i.status] ?? i.status})`}`).join(" · ") });
      }
      if (w.manual?.at) out.push({ at: w.manual.at, window: w, kind: "manual", word: "Manual change", tone: "warning", text: `${w.manual.name}: ${pct(w.manual.from)} → ${pct(w.manual.to)}`, detail: w.manual.source === "person" ? "by a person in Home Assistant" : "outside Smart Occupancy" });
    }
    const seen = new Set<string>();
    return out.filter((l) => this.kinds.has(l.kind)).filter((l) => { const k = `${l.kind}|${l.window.window_id}|${l.at}|${l.text}`; if (seen.has(k)) return false; seen.add(k); return true; })
      .sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0)).slice(0, 200);
  }

  private link(w: WindowModel, tab = "log"): TemplateResult {
    return html`<a class="text-link" href=${`#/curtains/windows/${encodeURIComponent(w.window_id)}/${tab}`}>${w.name}</a>`;
  }

  private operation(w: WindowModel): TemplateResult | typeof nothing {
    const op = w.last;
    if (!op) return nothing;
    const open = this.opOpen === w.window_id;
    return html`<li class="cu-impact-row" data-window=${w.window_id}><div class="cu-impact-head">${this.link(w)} ${chip(OVERALL_TONE[op.status] ?? "neutral", this.model.words.overall[op.status] ?? op.status)}
        <span class="small muted">${op.preset_name} · started ${fmt(op.started)}${op.finished ? ` · finished ${fmt(op.finished)}` : ""}</span>
        <button type="button" class="lp-link" @click=${() => (this.opOpen = open ? null : w.window_id)}>${open ? "Hide covers" : "Covers"}</button></div>
      <div class="small">${op.explanation}</div>
      ${open ? html`<div class="table-wrap"><table class="plain-table"><thead><tr><th scope="col">Cover</th><th scope="col">Target</th><th scope="col">Result</th><th scope="col">Detail</th><th scope="col">Start</th><th scope="col">Final</th></tr></thead>
        <tbody>${op.items.map((i) => html`<tr><td>${i.name}<span class="block small muted"><code>${i.entity_id}</code>${i.service ? ` · cover.${i.service}` : ""}</span></td><td>${i.target === null ? "unchanged" : pct(i.target)}</td>
          <td>${chip(RESULT_TONE[i.status] ?? "neutral", this.model.words.results[i.status] ?? i.status)}</td><td class="small">${i.detail}</td><td>${pct(i.start_position)}</td><td>${pct(i.final_position)}</td></tr>`)}</tbody></table></div>` : nothing}
    </li>`;
  }

  private conflicts(): TemplateResult {
    const rows = this.model.windows.flatMap((w) => w.layers.flatMap((l) => l.conflicts.map((c) => ({ w, l, c, allowed: w.allowed_automations.includes(c.automation) }))));
    const can = this.host.canEdit;
    return html`<section class="card wide" data-testid="curtains-conflicts"><header class="card-head">${icon("robot")}<h3>Other automations for these covers</h3>
        ${chip(rows.some((r) => r.c.enabled && !r.allowed) ? "warning" : "ok", `${rows.filter((r) => r.c.enabled && !r.allowed).length} competing`)}
        ${can ? html`<button type="button" class="btn btn-small" data-testid="curtains-rescan" ?disabled=${this.host.busy} @click=${() => void this.host.run(() => curtains(cmd("rescan")), "Automations scanned")}>${icon("refresh", 16)}Rescan automations</button>` : nothing}</header>
      <p class="small muted">Found among ${plural(this.model.automations, "automation")} that move covers. Automations are only listed, never changed, disabled or deleted. While an enabled one is listed (and not allowed), its window cannot be switched to Automatic, and an Automatic window sends nothing. Templated targets cannot be detected.</p>
      ${rows.length ? html`<div class="table-wrap"><table class="plain-table"><thead><tr><th scope="col">Automation</th><th scope="col">Window · cover</th><th scope="col">State</th><th scope="col">Detail</th><th scope="col"></th></tr></thead>
        <tbody>${rows.map(({ w, l, c, allowed }) => html`<tr data-automation=${c.automation}><td><strong>${c.name ?? c.automation}</strong><span class="block small muted"><code>${c.automation}</code></span></td><td>${this.link(w, "overview")} · ${l.name}</td>
          <td>${chip(c.enabled ? (allowed ? "info" : "warning") : "neutral", c.enabled ? (allowed ? "enabled · allowed" : "enabled · blocks Automatic") : "disabled")}</td><td class="small">${c.detail}</td>
          <td class="cell-action">${can ? html`<button type="button" class="btn btn-small" ?disabled=${this.host.busy} @click=${() => void this.host.run(() => curtains(cmd("automation/allow"), { window_id: w.window_id, automation: c.automation, allowed: !allowed }), allowed ? "It blocks Automatic again" : "Allowed with Automatic")}>${allowed ? "Revoke" : "Allow"}</button>` : nothing}</td></tr>`)}</tbody></table></div>`
        : html`<p class="muted">None found.</p>`}
    </section>`;
  }

  // ----------------------------------------------------------------- import / export
  private async pickFile(e: Event): Promise<void> {
    const input = e.target as HTMLInputElement;
    const f = input.files?.[0];
    input.value = "";
    if (!f) return;
    let file: unknown;
    try {
      file = JSON.parse(await f.text());
    } catch {
      this.imp = { file: null, review: null, error: "This is not a JSON export of the App.", busy: false };
      await openDialog(this, "dialog.cu-import");
      return;
    }
    const s: ImportState = { file, review: null, error: null, busy: true };
    this.imp = s;
    await openDialog(this, "dialog.cu-import");
    try {
      s.review = await reviewImport(file);
    } catch (err) {
      s.error = err instanceof Error ? err.message : String(err);
    }
    s.busy = false;
    this.requestUpdate();
  }

  private async doImport(): Promise<void> {
    const s = this.imp;
    if (!s?.review) return;
    const ok = await this.host.run(() => applyImport(s.file, s.review!.revision), "Configuration imported");
    if (ok) { closeDialog(this, "dialog.cu-import"); this.imp = null; }
  }

  private importDialog(): TemplateResult | typeof nothing {
    const s = this.imp;
    if (!s) return nothing;
    const r = s.review;
    return html`<dialog class="sheet sheet-full review-dialog cu-import" aria-labelledby="cu-import-title" data-testid="curtains-import-dialog" @close=${() => (this.imp = null)}>
      <div class="sheet-inner"><header class="sheet-head"><h2 id="cu-import-title">Import a curtain configuration</h2><button type="button" class="icon-btn" aria-label="Close" @click=${() => { closeDialog(this, "dialog.cu-import"); this.imp = null; }}>${icon("close")}</button></header>
        <div class="sheet-body">
          ${s.error ? html`<p class="notice notice-error" role="alert">${icon("error", 20)}<span>${s.error}</span></p>` : nothing}
          ${this.host.error ? problemNotice(this.host.error) : nothing}
          ${s.busy ? html`<div class="loading" role="status"><span class="spinner"></span><span>Reviewing the file…</span></div>` : nothing}
          ${r ? html`
            ${!r.writable ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>The configuration cannot be written now (the App does not control the curtains, or the bridge is down).</span></p>` : nothing}
            <div class="review-block"><h3>${r.same ? "Identical to the current configuration" : plural(r.changes.length, "change")}</h3>
              <p class="small muted">Exported ${fmtS(r.exported_at)} · ${plural(r.windows, "window")} · incoming revision ${r.incoming_revision ?? "—"}, current ${r.revision ?? "—"}</p>
              ${r.changes.length ? html`<ul class="change-list">${r.changes.map((c) => html`<li>${icon("check", 16)}<span>${c}</span></li>`)}</ul>` : nothing}</div>
            ${r.missing_entities.length ? html`<div class="review-block"><h3>Covers not found in Home Assistant (${r.missing_entities.length})</h3><p class="small">${r.missing_entities.map((e) => html`<code>${e}</code> `)}</p></div>` : nothing}
            ${r.dropped_entities.length ? html`<div class="review-block"><h3>Dropped (${r.dropped_entities.length})</h3><p class="small">${r.dropped_entities.map((e) => html`<code>${e}</code> `)}</p></div>` : nothing}
            <p class="small muted">Importing replaces the configuration (presets, assignments, schedule and settings); no cover moves because of it. A backup of the current configuration is kept in the App.</p>` : nothing}
        </div>
        <footer class="sheet-foot"><button type="button" class="btn" @click=${() => { closeDialog(this, "dialog.cu-import"); this.imp = null; }}>Cancel</button>
          <button type="button" class="btn btn-primary" data-testid="curtains-import-confirm" ?disabled=${!r || !r.writable || r.same || this.host.busy} @click=${() => void this.doImport()}>${icon("file-upload", 18)}Import</button></footer></div></dialog>`;
  }

  protected render(): unknown {
    const m = this.model;
    const windows = m.windows;
    const holds = windows.filter((w) => w.auto?.hold);
    const blocked = windows.filter((w) => w.auto?.status?.action === "blocked");
    const failures = windows.filter((w) => w.last && ["failed", "partial", "timeout", "interrupted"].includes(w.last.status));
    const busy = windows.flatMap((w) => w.layers.filter((l) => l.moving).map((l) => ({ w, l })));
    const moving = this.host.status?.moving ?? [];
    const problems = windows.flatMap((w) => w.problems.filter((p) => p.severity === "warning").map((p) => ({ w, p })));
    const lines = this.lines();
    const kinds: Array<[Line["kind"], string]> = [["decision", "Decisions"], ["operation", "Operations"], ["manual", "Manual changes"]];
    return html`<div data-testid="curtains-diagnostics">
      ${this.host.error && !this.imp ? problemNotice(this.host.error) : nothing}
      <div class="stat-tiles">
        <div class="stat"><span class="stat-value ${holds.length ? "tone-warning" : ""}">${holds.length}</span><span class="stat-label">manual holds</span></div>
        <div class="stat"><span class="stat-value ${blocked.length ? "tone-warning" : ""}">${blocked.length}</span><span class="stat-label">prevented now</span></div>
        <div class="stat"><span class="stat-value ${failures.length ? "tone-error" : ""}">${failures.length}</span><span class="stat-label">failed movements</span></div>
        <div class="stat"><span class="stat-value ${busy.length ? "tone-info" : ""}">${busy.length}</span><span class="stat-label">covers moving</span></div>
        <div class="stat"><span class="stat-value ${problems.length ? "tone-warning" : ""}">${problems.length}</span><span class="stat-label">problems</span></div>
      </div>
      <div class="grid engine-grid">
        <section class="card" data-testid="curtains-safety"><header class="card-head">${icon("shield-alert")}<h3>Safety and holds</h3></header>
          <h4>Prevented now (${blocked.length})</h4>${blocked.length ? html`<ul class="cu-rows">${blocked.map((w) => html`<li class="cu-row"><span class="cu-row-main">${this.link(w, "automatic")}<span class="small muted">${w.auto.status?.text}</span></span>${chip("warning", `${w.auto.status?.word}${w.auto.status?.code ? ` · ${w.auto.status.code}` : ""}`)}</li>`)}</ul>` : html`<p class="muted small">None.</p>`}
          <h4>Manual holds (${holds.length})</h4>${holds.length ? html`<ul class="cu-rows">${holds.map((w) => html`<li class="cu-row"><span class="cu-row-main">${this.link(w, "overview")}<span class="small muted">${w.auto.hold?.policy_word} · since ${fmt(w.auto.hold?.created)}${w.auto.hold?.name ? ` · ${w.auto.hold.name}` : ""}</span></span>${modeChip(w.mode, w.mode_word)}</li>`)}</ul>` : html`<p class="muted small">None.</p>`}
          <h4>Busy covers (${busy.length + moving.length})</h4>${busy.length || moving.length ? html`<ul class="cu-rows">${busy.map(({ w, l }) => html`<li class="cu-row"><span class="cu-row-main">${this.link(w, "overview")} · ${l.name}<span class="small muted">${l.state} · ${pct(l.position)}</span></span>${chip("info", "Moving")}</li>`)}
            ${moving.filter((id) => !busy.some((b) => b.w.window_id === id)).map((id) => html`<li class="cu-row"><span class="cu-row-main">${windows.find((w) => w.window_id === id)?.name ?? id}<span class="small muted">an operation is running</span></span>${chip("info", "Busy")}</li>`)}</ul>` : html`<p class="muted small">Every cover is at rest.</p>`}
        </section>
        <section class="card" data-testid="curtains-problems"><header class="card-head">${icon("warning")}<h3>Problems</h3></header>
          ${problems.length ? html`<ul class="cu-rows">${problems.map(({ w, p }) => html`<li class="cu-row"><span class="cu-row-main">${this.link(w, "covers")}<span class="small">${p.text}</span></span>${chip("warning", "warning")}</li>`)}</ul>` : html`<p class="muted small">No problems.</p>`}
          <h4>Failed movements (${failures.length})</h4>${failures.length ? html`<ul class="cu-rows">${failures.map((w) => html`<li class="cu-row"><span class="cu-row-main">${this.link(w)}<span class="small muted">${w.last?.explanation}</span></span>${chip(OVERALL_TONE[w.last?.status ?? ""] ?? "error", m.words.overall[w.last?.status ?? ""] ?? w.last?.status ?? "")}</li>`)}</ul>` : html`<p class="muted small">None.</p>`}
        </section>
        <section class="card wide" data-testid="curtains-operations"><header class="card-head">${icon("blinds")}<h3>Last operation per window</h3></header>
          ${windows.some((w) => w.last) ? html`<ul class="cu-impact">${windows.map((w) => this.operation(w))}</ul>` : html`<p class="muted">No preset applied yet.</p>`}
        </section>
        <section class="card wide" data-testid="curtains-log"><header class="card-head">${icon("history")}<h3>Log</h3><span class="small muted">${lines.length} entries</span></header>
          <div class="cu-filter-row">
            <label class="inline-field"><span class="small muted">Window</span><select data-testid="curtains-log-filter" @change=${(e: Event) => (this.filter = (e.target as HTMLSelectElement).value)}>
              <option value="" ?selected=${!this.filter}>All windows</option>${windows.map((w) => html`<option value=${w.window_id} ?selected=${this.filter === w.window_id}>${w.name}</option>`)}</select></label>
            ${kinds.map(([k, l]) => html`<label class="lp-check"><input type="checkbox" .checked=${this.kinds.has(k)} @change=${(e: Event) => { const s = new Set(this.kinds); if ((e.target as HTMLInputElement).checked) s.add(k); else s.delete(k); this.kinds = s; }} /><span>${l}</span></label>`)}
          </div>
          ${lines.length ? html`<ul class="cu-log">${lines.map((l) => html`<li data-kind=${l.kind}><time datetime=${l.at}>${fmtS(l.at)}</time><span>${chip(l.tone as "ok", l.word)} <strong>${this.link(l.window)}</strong>: ${l.text}${l.detail ? html`<span class="block small muted">${l.detail}</span>` : nothing}</span></li>`)}</ul>` : html`<p class="muted">Nothing yet.</p>`}
        </section>
        ${this.conflicts()}
        <section class="card" data-testid="curtains-house-log"><header class="card-head">${icon("calendar-clock")}<h3>Household log</h3>${chip("neutral", m.schedule.house.word)}</header>
          ${m.schedule.house.log.length ? html`<ul class="cu-log">${m.schedule.house.log.slice(0, 20).map((x) => html`<li><time datetime=${x.at}>${fmt(x.at)}</time><span>${x.text}</span></li>`)}</ul>` : html`<p class="muted small">Nothing yet.</p>`}
        </section>
        <section class="card" data-testid="curtains-data"><header class="card-head">${icon("export")}<h3>Configuration and backups</h3></header>
          <p class="small muted">The export holds the whole curtain configuration and runtime state as JSON. An import is reviewed first (what changes, which covers are missing) and never moves a cover.</p>
          <div class="row-actions"><a class="btn" href=${apiUrl("api/curtains/export")} download data-testid="curtains-export">${icon("download", 18)}Export</a>
            ${this.host.canEdit ? html`<label class="btn file-btn" data-testid="curtains-import">${icon("file-upload", 18)}Import…<input type="file" accept="application/json,.json" @change=${(e: Event) => void this.pickFile(e)} /></label>` : nothing}</div>
          <h4>Backups in the App</h4>
          ${this.host.status?.backups.length ? html`<ul class="backup-list">${this.host.status.backups.slice(0, 10).map((b) => html`<li><code>${b.id}</code><span class="small muted">${Math.round(b.size / 1024)} kB</span></li>`)}</ul>` : html`<p class="muted small">No backups yet (one is written at each change of control and import).</p>`}
        </section>
      </div>
      ${this.importDialog()}
    </div>`;
  }
}

customElements.define("sl-curtains-diagnostics", SlCurtainsDiagnostics);
