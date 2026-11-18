// Phase A5: the panels shared by the Predictions house tabs and the room page: a room's forecast, its learned
// patterns, its accuracy and the logs. Every number is rounded to what the data supports; when a forecast has no
// usable numbers its plain explanation is shown instead.
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { chip, kv } from "../ui";
import { haTime } from "../presence/components";
import type { AccuracyStats, CompareRecord, RoomAccuracy, RoomDetail, RoomForecast } from "./api";
import { brierLine, calibrationPlot, horizonBars, hourlyBars, hourlyChart, weeklyHeatmap } from "./charts";
import { confidenceChip, confidenceReasons, decisionList, FACTOR_KIND, fractionText, hasNumbers, headline, probabilityText, statusChip } from "./components";

const MIN_EVALUATED = 100;

function pctOf(v: number | null | undefined, digits = 0): string {
  return v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)} %`;
}

function score(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : v.toFixed(3);
}

function minutes(seconds: number): string {
  if (seconds < 60) return `${seconds} s`;
  const m = Math.round(seconds / 60);
  return m < 90 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

/** The current forecast of one room in full. */
export function forecastPanel(f: RoomForecast | null, opts: { shadow?: RoomForecast | null; source?: "app" | "integration"; comparison?: CompareRecord | null } = {}): TemplateResult {
  const fmt = haTime();
  if (!f) return html`<section class="card wide"><header class="card-head">${icon("crystal-ball")}<h3>Forecast</h3></header><p class="muted">No forecast has been produced for this room yet.</p></section>`;
  const numbers = hasNumbers(f);
  return html`<section class="card wide" data-testid="predictions-forecast">
    <header class="card-head">${icon("crystal-ball")}<h3>Forecast now</h3>${statusChip(f.status)}
      <span class="small muted">${opts.source === "app" ? "produced by the App" : opts.source === "integration" ? "produced by the integration" : ""}${f.updated ? ` · ${fmt(f.updated)}` : ""}</span></header>
    <p class="pr-row">${headline(f)}</p>
    ${f.explanation && numbers ? html`<p>${f.explanation}</p>` : nothing}
    ${numbers ? horizonBars(f) : nothing}
    <div class="grid pr-cal-grid">
      <div>
        <h4>Confidence <span class="small muted">(how reliable this room's forecasts are)</span></h4>
        <p class="pr-row">${confidenceChip(f.confidence?.level, f.confidence?.score)}
          <span class="small muted">needed for a prediction: ${f.confidence_threshold}</span></p>
        ${confidenceReasons(f.confidence)}
        ${f.confidence?.components ? html`<dl class="kv small">${Object.entries(f.confidence.components).map(([k, v]) => html`<dt>${COMPONENT[k] ?? k}</dt><dd>${v === null || v === undefined ? "—" : typeof v === "number" && k === "skill_vs_schedule" ? v.toFixed(3) : String(v)}</dd>`)}</dl>` : nothing}
      </div>
      <div>
        <h4>Why</h4>
        ${f.factors.length ? html`<ul class="pr-factors">${f.factors.map((x) => html`<li class="pr-factor">
          <span class="pr-impact ${x.impact > 0 ? "pr-impact-up" : x.impact < 0 ? "pr-impact-down" : ""}" title="Change of the ${f.horizon}-min probability, percentage points">${x.impact > 0 ? "+" : ""}${x.impact.toFixed(1)} pts</span>
          <span><span class="small muted">${FACTOR_KIND[x.kind] ?? x.kind} · </span>${x.text}</span></li>`)}</ul>`
          : html`<p class="muted small">${numbers ? "The forecast follows the plain schedule." : f.explanation || "No forecast."}</p>`}
        ${f.window ? html`<p class="small"><strong>Likely window:</strong> ${f.window.text} <span class="muted">(${f.window.source === "schedule" ? "from the schedule" : "from the forecast"})</span></p>` : nothing}
        ${f.next_rooms.length ? html`<p class="small"><strong>Likely next rooms:</strong> ${f.next_rooms.map((n) => html`<a href="#/predictions/rooms/${n.area_id}">${n.name}</a> ${probabilityText(n.probability)} `)}</p>` : nothing}
        ${f.occupied !== null ? html`<p class="small muted">Right now the room is ${f.occupied ? "occupied" : "vacant"}${f.state ? ` (${f.state.replace(/_/g, " ")})` : ""}.</p>` : nothing}
      </div>
    </div>
    ${opts.source === "integration" && opts.shadow ? shadowNote(f, opts.shadow) : nothing}
  </section>`;
}

const COMPONENT: Record<string, string> = {
  days_observed: "Days observed", weekdays_observed: "Weekdays observed (of 7)", entries: "Visits seen",
  evaluated_forecasts: "Evaluated forecasts", skill_vs_schedule: "Skill vs schedule", signals_known: "Sensors known (share)",
};

function shadowNote(f: RoomForecast, s: RoomForecast): TemplateResult | typeof nothing {
  const same = f.status === s.status && f.predicted === s.predicted && (f.probability === s.probability
    || (f.probability !== null && s.probability !== null && Math.abs(f.probability - s.probability) <= 5));
  if (same) return nothing;
  return html`<div class="pr-shadow small" data-testid="predictions-shadow">
    <p><strong>The App's shadow computes</strong> ${hasNumbers(s) ? html`${probabilityText(s.probability)} in ${s.horizon} min, ${s.predicted ? "predicted occupied" : "predicted vacant"}, ${confidenceChip(s.confidence?.level, s.confidence?.score)}` : html`${statusChip(s.status)} — ${s.explanation}`}
      <span class="muted"> (nothing it computes is published while the integration produces the forecasts)</span></p>
  </div>`;
}

/** Learned patterns: hourly curves, the weekly heatmap, arrivals and movement. */
export function patternsPanel(d: RoomDetail): TemplateResult {
  const p = d.patterns;
  const m = d.movement;
  const l = d.learning;
  return html`<div class="grid engine-grid" data-testid="predictions-patterns-panel">
    <section class="card wide"><header class="card-head">${icon("history")}<h3>Occupancy by hour</h3>
        ${chip(p.days ? "ok" : "neutral", p.days ? `${p.days} day${p.days === 1 ? "" : "s"} observed` : "No days observed")}</header>
      <p class="muted small">Bars are real data: the share of each hour this room was occupied over the ${p.days} observed day${p.days === 1 ? "" : "s"} (${l.room_first_date ?? "—"} → ${l.room_last_date ?? "—"}, ${l.room_observed_hours} h, ${l.room_entries} visits). Lines are what the model learned from them (recent days weigh more); grey columns were not observed.</p>
      ${p.days ? hourlyChart(p) : html`<p class="muted">Nothing observed yet for this room.</p>`}
    </section>
    <section class="card wide"><header class="card-head">${icon("grid")}<h3>Week pattern</h3></header>
      <p class="muted small">Observed share of each weekday hour. An hour needs at least 30 minutes of data to show a value; the rest is "not observed".</p>
      ${p.days ? weeklyHeatmap(p) : html`<p class="muted">Nothing observed yet.</p>`}
    </section>
    <section class="card"><header class="card-head">${icon("door-open")}<h3>Arrivals per hour</h3></header>
      <p class="muted small">Average number of visits starting in each hour, per observed day.</p>
      ${p.days ? hourlyBars(p.hourly_entries, "Arrivals per hour", "visits per day") : html`<p class="muted">Nothing observed yet.</p>`}
    </section>
    <section class="card"><header class="card-head">${icon("swap")}<h3>Movement</h3><span class="small muted">${m.arrivals} arrivals learned</span></header>
      <h4>Where people go next</h4>
      ${m.next.length ? html`<div class="table-wrap"><table class="plain-table"><thead><tr><th scope="col">Room</th><th scope="col">Probability</th><th scope="col">Moves</th><th scope="col">Median delay</th><th scope="col">By day part</th></tr></thead>
        <tbody>${m.next.map((n) => html`<tr><td><a href="#/predictions/rooms/${n.area_id}/patterns">${n.name}</a></td><td>${fractionText(n.probability)}</td><td>${n.count}</td><td>${minutes(n.median_delay)}</td>
          <td class="small muted">${Object.entries(n.by_part).map(([part, v]) => `${part} ${fractionText(v)}`).join(" · ")}</td></tr>`)}</tbody></table></div>`
        : html`<p class="muted small">No movement out of this room learned yet.</p>`}
      <h4>Where people come from</h4>
      ${m.previous.length ? html`<div class="table-wrap"><table class="plain-table"><thead><tr><th scope="col">Room</th><th scope="col">Share of arrivals</th><th scope="col">Moves</th><th scope="col">Median delay</th></tr></thead>
        <tbody>${m.previous.map((n) => html`<tr><td><a href="#/predictions/rooms/${n.area_id}/patterns">${n.name}</a></td><td>${fractionText(n.share)}</td><td>${n.count}</td><td>${minutes(n.median_delay)}</td></tr>`)}</tbody></table></div>`
        : html`<p class="muted small">No movement into this room learned yet.</p>`}
    </section>
    ${d.comparison ? comparisonRecord(d.comparison) : nothing}
    ${d.contexts.length ? html`<section class="card wide"><header class="card-head">${icon("tune")}<h3>Context effects</h3></header>
      <p class="muted small">How entries and exits change while a context is on, compared with what the schedule alone expects (1.0 = no effect).</p>
      <div class="table-wrap"><table class="plain-table"><thead><tr><th scope="col">Context</th><th scope="col">Now</th><th scope="col">Entries while on</th><th scope="col">Entries while off</th><th scope="col">Exits while on</th><th scope="col">Exits while off</th><th scope="col">Data</th></tr></thead>
        <tbody>${d.contexts.map((c) => html`<tr><td>${c.name}${c.automatic ? html` <span class="pr-inherit">automatic</span>` : nothing}</td><td>${c.state ?? "—"}</td>
          <td>${c.factor ? `×${c.factor.entry_on.toFixed(2)}` : "—"}</td><td>${c.factor ? `×${c.factor.entry_off.toFixed(2)}` : "—"}</td><td>${c.factor ? `×${c.factor.exit_on.toFixed(2)}` : "—"}</td><td>${c.factor ? `×${c.factor.exit_off.toFixed(2)}` : "—"}</td>
          <td class="small muted">${c.factor ? `${c.factor.hours_on} h on, ${c.factor.entries_on} entries` : "not learned yet"}</td></tr>`)}</tbody></table></div></section>` : nothing}
  </div>`;
}

/** Observed vs predicted: the latest comparison record of this room. */
export function comparisonRecord(c: CompareRecord): TemplateResult {
  const fmt = haTime();
  const side = (s: CompareRecord["integration"]) => html`${statusChip(s.status)} ${probabilityText(s.probability)} · ${s.predicted === null ? "no prediction" : s.predicted ? "predicted occupied" : "predicted vacant"} · ${confidenceChip(s.confidence)}`;
  return html`<section class="card wide" data-testid="predictions-room-comparison"><header class="card-head">${icon("swap")}<h3>Integration vs App</h3><span class="small muted">${fmt(c.at)}</span></header>
    ${kv([["Integration", side(c.integration)], ["App (shadow)", side(c.app)]])}</section>`;
}

function accuracyTable(rows: Array<[string, AccuracyStats]>): TemplateResult {
  return html`<div class="table-wrap"><table class="plain-table"><thead><tr><th scope="col">Horizon</th><th scope="col">Evaluated</th><th scope="col">Brier (forecast)</th><th scope="col">Schedule</th><th scope="col">Persistence</th><th scope="col">Skill vs schedule</th><th scope="col">Skill vs persistence</th><th scope="col">Hits / misses at threshold</th></tr></thead>
    <tbody>${rows.map(([h, a]) => {
      const few = a.forecasts < MIN_EVALUATED;
      return html`<tr data-horizon=${h}><td>in ${h} min</td><td>${a.forecasts}${few ? html` <span class="pr-inherit" title="Fewer than ${MIN_EVALUATED} evaluated forecasts: the scores are not yet meaningful">few</span>` : nothing}</td>
        <td>${score(a.brier)}</td><td class="pr-muted-num">${score(a.brier_schedule)}</td><td class="pr-muted-num">${score(a.brier_persistence)}</td>
        <td class=${a.skill_vs_schedule === null ? "" : a.skill_vs_schedule > 0 ? "tone-ok" : "tone-warning"}>${a.skill_vs_schedule === null ? "—" : `${a.skill_vs_schedule > 0 ? "+" : ""}${(a.skill_vs_schedule * 100).toFixed(1)} %`}</td>
        <td class=${a.skill_vs_persistence === null ? "" : a.skill_vs_persistence > 0 ? "tone-ok" : "tone-warning"}>${a.skill_vs_persistence === null ? "—" : `${a.skill_vs_persistence > 0 ? "+" : ""}${(a.skill_vs_persistence * 100).toFixed(1)} %`}</td>
        <td class="small">${a.true_positive + a.true_negative} right · ${a.false_positive} false alarms · ${a.false_negative} missed <span class="muted">(at ${pctOf(a.threshold)})</span></td></tr>`;
    })}</tbody></table></div>`;
}

/** House-wide accuracy by horizon. */
export function houseAccuracyPanel(acc: Record<string, AccuracyStats>): TemplateResult {
  const rows = Object.entries(acc).sort((a, b) => Number(a[0]) - Number(b[0]));
  const total = rows.reduce((n, [, a]) => n + a.forecasts, 0);
  return html`<section class="card wide" data-testid="predictions-house-accuracy"><header class="card-head">${icon("shield-check")}<h3>House-wide accuracy</h3>${chip(total ? "ok" : "neutral", `${total} forecasts evaluated`)}</header>
    <p class="muted small">Brier score: the mean squared gap between a forecast and what happened (0 = perfect, 0.25 = always saying 50 %). Skill: how much better than a baseline (positive is better). Fewer than ${MIN_EVALUATED} evaluated forecasts are not meaningful yet.</p>
    ${rows.length ? accuracyTable(rows) : html`<p class="muted">Nothing evaluated yet: accuracy needs completed days with forecasts.</p>`}
  </section>`;
}

/** One room's accuracy: scores, calibration, daily line. */
export function roomAccuracyPanel(a: RoomAccuracy, threshold: number): TemplateResult {
  const rows = Object.entries(a.by_horizon).sort((x, y) => Number(x[0]) - Number(y[0]));
  const main = a.by_horizon[String(a.horizon)];
  const few = !main || main.forecasts < MIN_EVALUATED;
  return html`<div class="grid engine-grid" data-testid="predictions-accuracy-panel">
    <section class="card wide"><header class="card-head">${icon("shield-check")}<h3>Scores by horizon</h3>${main ? chip(few ? "warning" : "ok", `${main.forecasts} evaluated at ${a.horizon} min`) : nothing}</header>
      <p class="muted small">${a.method}. The room's threshold for "predicted occupied" is ${threshold} %.</p>
      ${few ? html`<p class="notice notice-warning">${icon("warning", 20)}<span>Fewer than ${MIN_EVALUATED} evaluated forecasts at the room's horizon: these scores will move a lot until more days are completed.</span></p>` : nothing}
      ${rows.length ? accuracyTable(rows) : html`<p class="muted">Nothing evaluated yet.</p>`}
    </section>
    <section class="card"><header class="card-head">${icon("fit")}<h3>Calibration (${a.horizon} min)</h3></header>
      <p class="muted small">When the model said "60 %", was the room occupied about 60 % of those times? Points on the diagonal are well calibrated; above it the model is too cautious, below it too sure.</p>
      ${main ? calibrationPlot(main) : html`<p class="muted">Nothing evaluated yet.</p>`}
    </section>
    <section class="card"><header class="card-head">${icon("history")}<h3>Daily score (${a.horizon} min)</h3></header>
      <p class="muted small">Last ${a.daily.length} evaluated day${a.daily.length === 1 ? "" : "s"}: the forecasts against two baselines, the schedule (time-of-week average) and persistence (the room stays as it is). Lower is better.</p>
      ${a.daily.length ? brierLine(a.daily) : html`<p class="muted">No completed day evaluated yet.</p>`}
    </section>
  </div>`;
}

/** The room's log and the house log. */
export function logPanel(d: RoomDetail, name: (id: string) => string): TemplateResult {
  return html`<div class="grid engine-grid" data-testid="predictions-log-panel">
    <section class="card"><header class="card-head">${icon("list")}<h3>${d.name}: forecast log</h3></header>
      <p class="muted small">What the forecast observed and predicted for this room. Nothing in it commanded a device.</p>
      ${decisionList(d.log, { empty: "Nothing recorded for this room yet.", name })}</section>
    <section class="card"><header class="card-head">${icon("home")}<h3>House log</h3></header>
      <p class="muted small">Learning, model rebuilds and predicted movements between rooms.</p>
      ${decisionList(d.house_log, { empty: "Nothing recorded yet.", name })}</section>
  </div>`;
}
