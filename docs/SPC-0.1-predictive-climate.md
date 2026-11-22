# Smart Adaptive & Predictive Climate 0.1 (Phase 0.1)

A fourth, independent App (`smart_predictive_climate/`, slug `smart_predictive_climate`) that observes rooms,
learns an interpretable thermal model per room from Recorder history and live data, predicts room temperatures,
preheat starts and solar gain, and publishes them as native Home Assistant entities through its own companion
integration (`custom_components/smart_predictive_climate`). **It recommends only: neither the App nor the
companion can control a device** (section 6).

It needs neither Smart Comfort, Smart Occupancy Detection nor Automation Builder and changes none of them.
User documentation: [smart_predictive_climate/DOCS.md](../smart_predictive_climate/DOCS.md).

## 1. What was reused

| From | What | How |
|---|---|---|
| Smart Comfort A7.1 (`smart_lighting/backend/smart_lighting/climate_*`) | thermal model, fitting (NNLS, term selection, rolling folds, held-out validation), observation store, data preparation, model store | copied into `spc/` with imports renamed; Smart Comfort's files are untouched. Extended: 6-hour validation horizon, window area and shading, an own sun position (no `sun.sun`), weather-entity cloud coverage, new observation kinds |
| Smart Occupancy Detection 0.2 | App infrastructure (Supervisor/Ingress frame, websocket client with allowlist, configuration store with backups, companion protocol and publisher with pairing / epoch / lease / sequence fencing, distribution script) | copied and renamed (`spc`, `X-SPC`, `spc_*` entities) |
| Automation Builder 0.2.1 | room → device → entity selection pattern, base CSS | adapted |

## 2. Architecture

```
Home Assistant (Supervisor Core proxy, allowlisted reads only)
   │  states, registries, history/history_during_period, recorder statistics, weather/subscribe_forecast
   ▼
history.Acquisition ── /data/history (own observation store, coverage, gaps, progress.json)
   │
engine.Engine ── prep (5-min grid, sun geometry, windows × covers × shading, neighbours, closed doors)
   │            fit (first-order grey-box model, validation 1/3/6 h)  → /data/models
   │            current (room sensor ≠ thermostat reading, freshness, contradictions)
   ▼
forecast.predict ── trajectories (heating as the thermostat runs it now / without heating), band,
   │                rates, warm-up, latest + cautious preheat start, solar gain, explanation, input bases
   ▼
core.AppCore ── UI/API (Ingress, CSRF, administrators only) and snapshot → companion → sensor.spc_<room>_*
```

* **Model:** `dT/dt = k(T_ref − T) + b·u_f(lag, tau) + a·solar + n·night + Σ c(T_j − T) + d` on a 5-minute grid,
  exact-step simulation. Each term is kept only when it improves held-out predictions.
* **Backfill:** bounded (12-hour windows, 10 entities per call, pause between calls), resumable from
  `progress.json`, downtime first, then holes, then older history backwards up to `history_days` (90 by
  default). UTC timestamps kept; gaps stay gaps. Long-term statistics are shown, never learned from.
* **Solar:** NOAA sun position from Home Assistant's latitude/longitude. Window orientation **0° = South**,
  clockwise (90° West, 180° North, 270° East); compass = (orientation + 180) mod 360. Sky basis:
  `geometry`, `geometry+cloud` (weather `cloud_coverage`, an estimate) or `geometry+measured` (lux/irradiance).
* **Zones:** one model per room; a room belongs to at most one zone; a connection (weak/normal/strong/open) is
  used only when the data shows exchange; a closed door sensor makes the neighbour term zero.
* **"Heating as now":** a thermostat that reports heating heats until its setpoint; the projection follows that
  (an approximation, since a thermostat regulates on its own measurement). Without a setpoint the heating is
  held for the whole horizon and a warning says so.
* **Recommendations** are published only from the confidence chosen in Settings (moderate by default); below
  it they are `unknown`, never "off". When the App is not fresh every room entity is `unavailable`.

## 3. Distribution decision

The companion is a separate integration and HACS installs one integration per repository, so it is meant to be
published as its own repository `havanissa/smart-predictive-climate`, built by `scripts/spc_companion_dist.py`
(same pattern as Smart Occupancy Detection). **That repository has not been created or published in this phase**;
until then the companion is copied by hand into `custom_components/`.

## 4. Validation against a known truth (synthetic rooms)

`smart_predictive_climate/tests/validation.py` simulates rooms with known coefficients on a 1-minute step,
reports them like Home Assistant sensors (resolution, noise, irregular intervals, dropouts), learns from 21 days
and predicts the following days. MAE in °C with perfect weather knowledge; "no change" = persistence.

| Scenario | Model | k learned / true | MAE 1 h | 3 h | 6 h | 24 h | No change, 24 h | Preheat reached | Start error |
|---|---|---|---|---|---|---|---|---|---|
| First-order room | high | 0.039 / 0.04 | 0.05 | 0.07 | 0.05 | 0.05 | 0.39 | 6/6 days | +2 min |
| South window, covers closed at night | high | 0.041 / 0.04 | 0.02 | 0.03 | 0.04 | 0.07 | 0.36 | 6/6 | −3 min |
| South window, clouds from a weather entity | high | 0.038 / 0.04 | 0.04 | 0.05 | 0.05 | 0.08 | 1.40 | 6/6 | +1 min |
| Two thermal masses, moderate | high | **0.009 / 0.04** | 0.09 | 0.19 | 0.32 | **0.42** | **0.19** | 6/6 | −14 min (early) |
| Two thermal masses, strong | **unidentifiable** | – | – | – | – | – | – | no recommendation | – |
| Open-plan neighbour (coupling 0.255 / 0.25) | high | 0.04 / 0.04 | 0.003 | 0.005 | 0.006 | 0.007 | 0.50 | 6/6 | −23 min (early) |

* A forecast that is 2 °C too warm gives errors of about 0.1 / 0.2 / 0.4 / 1.2 °C at 1 / 3 / 6 / 24 h: the
  forecast's own error is not modelled.
* The 80 % band covered 58–100 % of the cases depending on horizon and scenario (the band comes from the held-out
  errors and is extrapolated beyond 6 h, which the App says).
* **Honest limits shown by the table:** a room with substantial structural mass is approximated by a single
  mass. Its heat loss is underestimated, its 24-hour prediction is worse than "no change", and it can still be
  rated *high* confidence, because confidence comes from the 1–6 h held-out errors. With a very dominant mass the
  model is refused as unidentifiable rather than given a wrong one. Early preheat starts (14–23 min) are the
  intended direction of error.
* No real house's data was available for this phase; the room's own held-out validation is the measure there.

## 5. Tests

| Suite | Result |
|---|---|
| `smart_predictive_climate/tests` (thermal learning 49, forecast 13, App in-process end-to-end 8, API 3, package, infrastructure, distribution) | 153 passed, 1 skipped |
| `tests/spc_companion` (companion in a real Home Assistant test harness) | 71 passed |
| Regression: harness suites companion 43, engine 1261, sod_companion 38, automation_builder 45; Smart Occupancy Detection App 678; Automation Builder App 160; Smart Comfort App 327 (1 xfailed) | all passed |

**Real Home Assistant 2026.11.1** (disposable instance on port 18123, real App image, fake Supervisor;
`tests/e2e/`: `prepare.py`, `seed.py`, `stack.sh`, `e2e_checks.py`, `browser_checks.py`). The App's store was
seeded with 21 simulated days under the instance's real entity ids; the remaining minutes were read from the
real Recorder. 37 checks in 8 phases, all passing:

* setup: Ingress, Home Assistant version, area/device discovery with nothing pre-selected, CSRF refusal,
  pairing, room and settings saved, Recorder read in windows, model trained (high confidence);
* entities: all `sensor.spc_kamar_sudur_*` and `binary_sensor.spc_kamar_sudur_preheat_recommended` present with
  units, device classes and unique ids from the area id;
* unknown: room sensor unavailable → predicted temperature and preheat recommendation `unknown` (never off),
  numbers again when it returns;
* simulation: runs and sends nothing;
* App restart: entities `unavailable` and `binary_sensor.spc_app_connected` off while stopped; published again,
  model kept;
* Home Assistant restart: reconnects, entity ids and unique ids unchanged;
* backup: review and restore of a configuration backup keeps observations and the model;
* commands: every message the App sent recorded by the fake Supervisor - only reads, the two subscriptions and its
  companion's commands; no service call, state write or registry change; the companion registers no services.

Browser (headless Chromium): every page and room tab at desktop, tablet (both orientations) and phone, light and
dark - no console errors, no horizontal overflow, the colour scheme applied; on the desktop a simulation chart,
the window editor with the 0° = South convention and keyboard navigation with visible focus. 43 checks, all
passing.

Smart Comfort's and Smart Occupancy Detection's companions were installed in the same instance; Home Assistant
loaded all three.

### Found and fixed during end-to-end testing

* "Heating as now" held full heating for 24 hours (a demo heat pump that always reports *heating* projected
  40 °C, and the published 1-hour value was 2.5 °C too warm). The projection now follows the thermostat's
  setpoint.
* A backfill requested while the previous one was still finishing (flushing after an interruption) was dropped;
  history could stay unread until the next restart. Such a request now queues one more pass (regression test).
* Phone tables were clipped; they are now shown as labelled rows.

## 6. Cannot control devices

* App: the websocket allowlist (`spc/ha.py`) admits only reads, `history/history_during_period`,
  `recorder/statistics_during_period`, `recorder/list_statistic_ids`, `manifest/get`, `subscribe_events`
  for an allowlisted set of event types (state changes, registry updates, start-up),
  `weather/subscribe_forecast` and its companion's commands; anything else is refused
  before it is sent (unit-tested, and confirmed by the recorded commands above). No `map` in `config.yaml`.
* Companion: no services, no platforms other than read-only `sensor` / `binary_sensor`; it only displays the
  snapshot.
* Simulation is computed in the App and sends nothing.

## 7. Known limitations

* One thermal mass per room (section 4).
* Heating is the reported `hvac_action` share of the room's thermostats; heater power is unknown. Without
  `hvac_action` it is inferred from setpoints. The setpoint-limited projection compares the setpoint with the
  room sensor, not the thermostat's own reading.
* Humidity, energy, heating-system state and wind are stored but not used by the model.
* Weather forecast errors are not modelled.
* Validated on synthetic rooms only; not yet on Home Assistant OS or a real house.

## 8. Recommended Phase 0.2

1. **Pilot on real data:** run beside a real house for 2–4 weeks; compare the room's held-out validation and
   preheat outcomes with the synthetic table; publish the measured errors.
2. **Two-mass option:** an optional second (structural) mass, selected only when it beats the first-order model
   on 24-hour held-out errors; until then, base confidence on the worst horizon actually used for decisions
   (include 24 h), so a room like the moderate two-mass case is not rated *high*.
3. **Home Assistant OS validation** of the image and the companion, and the HACS repository publication.
4. **Heating power and setpoint feedback:** use energy/power sensors where available, and the thermostat's own
   measurement for the setpoint-limited projection.
5. **Forecast uncertainty:** widen the band with the weather forecast's lead time.
6. **Automation Builder hand-off:** a documented recipe (no new controller) for using
   `binary_sensor.spc_<room>_preheat_recommended` with window and manual-override conditions.
