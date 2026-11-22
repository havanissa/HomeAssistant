# Smart Adaptive & Predictive Climate

The App observes your rooms' temperatures, thermostats and weather, **learns how each room warms and cools**,
predicts its temperature and tells you **when heating should start** to reach a comfort temperature in time -
including how much the sun will help. It publishes this as ordinary Home Assistant sensors.

**It never controls anything.** It does not set thermostats, change HVAC modes, open valves, switch boilers,
move covers or touch your automations. Heating is controlled by your own automations (for example built with
Automation Builder), which can use the App's sensors - and which stay responsible for their own safety
conditions, manual overrides and fallbacks.

It works on its own: it needs neither Smart Comfort, Smart Occupancy Detection nor Automation Builder, and
changes none of them.

## Installation

1. Add this repository to the App store and install **Smart Adaptive & Predictive Climate**. Start it and open
   it from the sidebar (administrators only).
2. For the Home Assistant entities, install the companion integration **Smart Predictive Climate** through HACS
   from the repository `havanissa/smart-predictive-climate` (a separate repository: HACS installs one integration
   per repository), restart Home Assistant and add it under Settings → Devices & services. Then press
   **Pair** in the App's Settings.
   The companion only shows what the App publishes; it has no services and can control nothing. Without it the
   App works fully, it just publishes no entities.

## Adding a room

**Add a room** asks three short questions:

1. **Which room?** The areas of Home Assistant, with how many temperature sensors and thermostats each has.
2. **Which devices?** The devices of that area that have something climate-related. Nothing is ticked for you.
3. **What is each entity for?** Each entity of the chosen devices gets a role - room temperature, thermostat,
   window sensor, humidity, sunlight, cover... - or "not used". Suggestions are marked; only what you choose is
   used. Then the comfort temperature and period.

**Required:** a room temperature - preferably an **independent sensor you choose deliberately**. A thermostat
or TRV measures next to the radiator and can be degrees off; if a room only has a thermostat, its own reading is
used and the room says so. The two readings are kept apart: the thermostat's measurement is never mixed into
the room temperature.

**Everything else is optional**, and none of it is needed to finish the set-up: thermostats (a room without one
is *observation only*: its passive behaviour is still learned and predicted), outdoor temperature, weather
forecast, window and door sensors, windows (orientation, area, shading, obstructions, covers), sunlight
sensors, humidity, occupancy, heating-system state, energy, neighbouring rooms (thermal zones), an "away"
entity. The room's **Overview** tab lists what is required, what is suggested and what is missing.

Changing a room's inputs never deletes its observations or its model: the history stays, and the model is
retrained with the new inputs (the old one is used until then, with a note).

## Comfort targets

Per room: a comfort temperature during the comfort period (weekdays and weekends separately), optionally a
lower temperature outside it ("no requirement" by default) and an optional **away** entity with an away
temperature (a person or device tracker counts as away when not `home`; a toggle, binary sensor or switch when
`on`).

**Occupancy is optional.** You can choose occupancy entities (including Smart Occupancy Detection's) and let
the App learn the room's weekday and weekend usage from their history. Only if you also tick *Comfort only when
usually occupied* does that pattern narrow the comfort period - the schedule still sets the outer limits.
Occupancy never overrides your schedule otherwise, and the App detects nothing itself.

## Windows and the sun

For each window: the direction it faces, its glass area, fixed shading (eaves, trees: the share of direct sun
always blocked), obstructions (a hillside or building that hides the sun in some directions below some height)
and the covers in front of it.

**Direction convention: 0° = South**, clockwise as seen from above: 90° = West, 180° = North, 270° = East.
(Home Assistant's compass uses 0° = North; the App converts.)

The sun's position is computed for your home's location (Home Assistant's latitude and longitude). The direct
sun on each window comes from its geometry, its covers' positions (observed only - never moved) and the sky:

* with a sunlight or irradiance sensor: the measured brightness;
* with a weather entity: its cloud coverage (now, and forecast) - an **estimate**, never treated as a measurement;
* with neither: geometry only, and the learned effect is an average over all weather.

How much the sun actually warms the room is **learned from the room's own temperatures**. A south-facing room
that will warm in the afternoon sun gets a shorter preheat recommendation, or none.

## Thermal zones

Rooms that exchange heat - an open-plan living area, a hall with a staircase, rooms joined without a door - can
be grouped into a **thermal zone**, with the connections between them (weak, normal, strong, open plan) and,
where there is one, the sensor of the door between them.

* Each room keeps its own temperature, model, target and prediction.
* For each connection the App tests whether the neighbour measurably influences the room, and uses that only
  where the data shows it. Without measurable exchange the rooms are modelled independently - the conservative
  assumption - and the room says so. Connected rooms are never assumed to have the same temperature.
* A closed door (door sensor `off`) is a barrier: no exchange while it is closed.
* A room belongs to one zone: one room, one model - no competing models of the same space.

## How it learns

**Data.** For every input the App keeps its own compact history (`/data/history`), so learning goes on after
Home Assistant's Recorder has purged its detailed records. When a room is added, and after every restart, it
reads what the Recorder has, up to the configured number of days (90 by default), in bounded windows (12 hours,
10 entities per request, with a pause in between), resuming where it stopped. Every record keeps its original
UTC timestamp; nothing is interpolated: time without observations stays a gap. Recorder retention is never
changed. Long-term statistics (hourly means) of the temperature sensors before the detailed history are kept and
shown in the history graph, but not used for learning: a 30-minute warming rate cannot be read from hourly
means.

**The model** is an interpretable first-order thermal model per room, on a 5-minute grid:

    dT/dt = k·(T_outdoor − T)      heat loss to outdoors
          + b·heating              heating, with a learned delay and radiator inertia
          + a·sun                  direct sun on the windows
          + n·night                a different baseline at night, if the data shows one
          + Σ c·(T_neighbour − T)   exchange with connected rooms, if measurable
          + d                      baseline gain (other rooms, people, appliances)

Each term is kept only if the data supports it (it must improve held-out predictions); a term that is not
identified is left out and the room says why - correlation is not taken as proof of a cause. Time with an open
window is left out of the fit; the open-window effect is estimated separately from the room's own data (needs at
least two hours in three openings), otherwise the simulator uses an assumed value and says so.

**Quality.** Every model is tested on the last quarter of the data, which the fit did not see: 1, 3 and 6 hours
ahead, against "the temperature stays as it is". The room's **Thermal model** tab shows the learned values with
their uncertainty and kind (*estimated from data*, *assumed*), the validation errors, the confidence, the data
coverage, and in plain numbers: how fast the room warms from different start temperatures at different outdoor
temperatures, how fast it cools, how much it keeps rising after the heating stops, what full sun adds.

* About a day of usable data gives a first model; below the minimum history (14 days by default) it is
  **preliminary**. Neither is proof of accuracy: only the held-out validation is.
* Recommendations are published only from the confidence chosen in Settings (moderate by default). Below it the
  recommendation entities stay *unknown* - never "off" - while the predictions are still shown in the App.
* Training runs after the history is read and every six hours; *Learn again now* on the Thermal model tab
  starts it at once.

## Predictions and recommendations

Every minute, for every room: the temperature over the next 24 hours (configurable up to 72) with the heating as
it is now and without heating, with an 80 % uncertainty band from the validation. "As it is now" means as the
thermostats run it: heating while the room is below their setpoint, off above it (an approximation: a thermostat
regulates on its own measurement); if they report no setpoint, the heating is assumed to continue and the room
says so. Also: the heating and cooling rate
now; the warm-up time to the coming target; the expected solar gain; and, for the next time the comfort target
rises, the **recommended preheat start**: the latest start at which the room is predicted to reach the target,
moved earlier by the model's uncertainty (at most 90 minutes). Every number says what it is based on: *measured*,
*forecast*, *assumed* (held because nothing better is known), *model*.

Outdoor temperature comes from the weather forecast where it reaches, otherwise the present value is assumed to
hold (and the room says so). Forecasts are never extended beyond what the weather entity or the model supports.

## Simulation

The **Simulation** tab answers "what if": start heating earlier or later, heating off or on, a colder or warmer
day, more or fewer clouds, a window open for a while, a different start temperature or target. It uses the
room's learned model and shows the scenario next to the room "as now", with the uncertainty band and heating
periods. **It is hypothetical and sends nothing to any device.**

## Entities

Per room (ids from the room name; unique ids from the area id, so renaming a room keeps them):

| Entity | Meaning |
|---|---|
| `sensor.spc_<room>_predicted_temperature` | in one hour, with the heating as the thermostats run it now (°C; band and 3 h / 6 h values as attributes) |
| `sensor.spc_<room>_heating_rate` | warming with full heating, now (°C/h) |
| `sensor.spc_<room>_cooling_rate` | change without heating, now (°C/h, negative = cooling) |
| `sensor.spc_<room>_estimated_warmup_minutes` | to the coming target with full heating from now |
| `sensor.spc_<room>_recommended_preheat_start` | timestamp: start heating then (recommendation) |
| `sensor.spc_<room>_recommended_target_temperature` | the comfort target for the coming period |
| `sensor.spc_<room>_solar_gain` | how much the sun is expected to add (an estimate, °C) |
| `sensor.spc_<room>_prediction_confidence` | none / low / moderate / high |
| `binary_sensor.spc_<room>_preheat_recommended` | on = heating should run now to meet the coming target |
| `sensor.spc_<room>_model_status` | collecting, preliminary, low / moderate / high confidence, unidentifiable ... |
| `binary_sensor.spc_app_connected` | the App is connected and publishing |

Attributes give the calculation time, the reason in words and what the inputs were.

**Unknown and unavailable.** A value the App cannot compute is `unknown`, never a guess; a recommendation below
the configured confidence is `unknown`, never "off"; when the App is not running every room entity is
`unavailable` (a stale forecast never looks current). An automation must therefore treat "unknown" and
"unavailable" as "no recommendation", not as "do not heat".

Example (an automation you build yourself, for example with Automation Builder - the App creates none):
*IF `binary_sensor.spc_stova_preheat_recommended` turns on AND the window is closed THEN set the thermostat to
`sensor.spc_stova_recommended_target_temperature`.*

## Settings, backup and diagnostics

Settings: the outdoor temperature source, the weather entity, how many days of history to learn from, the
minimum history before a model is more than preliminary, the forecast horizon, and from which confidence
recommendations are published. The configuration is versioned with automatic backups (the last 30), export
and import; it is part of Home Assistant's backups of the App. Restoring or importing never deletes
observations or models. Settings also shows the history acquisition, per-entity coverage and gaps, and
*Download diagnostics*.

## What the App may do in Home Assistant

Through the Supervisor's Core proxy, with an allowlist enforced before anything is sent: read states and the
area, device and entity registries, read history (`history/history_during_period`) and long-term statistics
(`recorder/statistics_during_period`, `recorder/list_statistic_ids`), subscribe to state changes and to a weather
entity's hourly forecast, and talk to its own companion. No service call, no state write, no registry change,
no access to Home Assistant's configuration folder.

## Limitations (version 0.1)

* One thermal mass per room. Rooms whose walls and floors store a lot of heat are approximated; the validation
  (on synthetic rooms, see `docs/SPC-0.1-predictive-climate.md`) shows larger errors and a heat loss that is
  underestimated in that case, and a room with a very dominant structural mass may be reported as
  "unidentifiable" rather than given a wrong model.
* Heating is the reported heating activity of the room's thermostats (`hvac_action`), as a share of them; the
  power of the heater is not known. Without `hvac_action` it is inferred from setpoints, which is less precise.
* Humidity, energy, heating-system state and wind are observed and kept, but not used by this version's model.
* The weather forecast is taken as given; its errors are not modelled (the validation shows their effect).
* Validated on synthetic rooms with a known truth; no real house's data was available for this version. The
  room's own held-out validation is the measure for your house.
* Confidence comes from the 1, 3 and 6 hour held-out errors; a room with much structural mass can be rated high
  confidence and still be less accurate than "no change" a day ahead (see the validation in the document above).
