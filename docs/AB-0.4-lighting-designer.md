# Automation Builder 0.4: Lighting Designer 2

Truth and Brightness Matrices, Colour & Mood, Switches & Remotes, Advanced Rules and an explicit, property-specific
priority - compiled into native Home Assistant automations that always apply the *currently* calculated state, so a
light returns to its matrix baseline by itself when an override or a conditional rule ends. Built on 0.3
([AB-0.3-automation-builder.md](AB-0.3-automation-builder.md)): the designer framework, presence sources,
compiler, conflict check, deployment, simulation and UI matrix editor were reused; no separate lighting engine and no
runtime supervisor were added.

| Part | Where |
|---|---|
| Document, plan, native compilation, Python twin, simulation, conversion, templates | `backend/ab/designers/lighting2.py` |
| Switch / remote discovery (classification, room → device → entity) | `backend/ab/designers/switches.py` |
| Version dispatch, helper targets, generators | `backend/ab/designers/__init__.py` |
| Generated-only rule-model extensions (larger limits, templated light data, fire-event action) | `backend/ab/model.py`, `compiler.py` |
| One automation per light (rule groups) | `compiler.compile_device` |
| Override helpers: plan, create, record, delete, undo | `core.py` (`_helper_plan`, `_create_helper`, `_delete_helper`), `store.DeployStore.helpers`, `ha.py` allowlist |
| Live "who controls", simulation, conversion, rule adoption, discovery routes | `core.py`, `web.py` |
| Interface | `static/lighting.js` (version 2), `static/designer.js` (version 1 + conversion card) |
| Tests | `tests/test_lighting2.py`, `tests/automation_builder/test_lighting2_runtime.py`, `tests/e2e/e2e_05_checks.py`, `tests/e2e/browser_05_checks.py` |

## 1. Model (designer document, version 2)

```
lights                     the rows (explicitly ticked)
truth.matrices[]           columns + cells ON/OFF/NO ACTION/USE DEFAULT (+ row/matrix defaults), policy, ranks
truth.combine              priority | on_wins | off_wins           (required when two matrices decide a light)
brightness.matrices[]      columns + cells 0-100 %/NO ACTION/USE DEFAULT, policy, ranks
brightness.combine         priority | max | min
brightness.zero            off | minimum | unchanged               (required as soon as a cell is 0 %)
mood                       selector, values, cells {ct | colour, brightness, effect, transition}, auto mode, defaults
switches[]                 kind latched|momentary, interactions[{kind, label, trigger}], mappings[{interaction, lights, action, override}]
override                   policy until_resumed|duration|until_vacant|until_event, minutes, source, release[], manual
advanced[]                 "while" {conditions, set{truth, brightness, colour, effect, transition; each value|keep|unset}}
                           "when"  {triggers, conditions, actions[]}
priority                   order of advanced, override, mood, brightness, truth (default in that order)
reconcile_minutes, on_available
```

Columns: `presence` (shared presence source + state), `state` (any entity, optional attribute, held-for), `numeric`
(entity, `from` inclusive, `below` exclusive, delay), `time` (after/before/weekdays), `always`. A switch's state, a
mood selector, a media player, a proximity sensor, a person … are all columns of these kinds, so presence and lux
are optional sources of either matrix type and nothing is forced into one of them.

Unset/default/no action are the 0.3 rules (`common.resolve`). Values carried by the evaluation: truth `on`/`off`/
`keep` (NO ACTION), brightness a number/`keep`/`off` (0 % with *switch off*), `None` = undecided.

## 2. Resolution semantics

Per light and per property (on/off, brightness, colour, effect, transition), walk the layers in the configured order;
a layer competes only for what it can set (truth: on/off; brightness matrix: brightness; mood: brightness, colour,
effect, transition; Advanced "while" rules: all; manual override: all but transition, as *keep*). The first layer
with a value or *keep* wins; *unset* asks the next. Then **truth gating**:

| Resolved on/off | Light now | Sent |
|---|---|---|
| OFF | on | `light.turn_off` (with transition) |
| OFF | off | nothing |
| ON | any | `light.turn_on` with the resolved attributes, if anything differs |
| undecided / keep | on | `light.turn_on` with the resolved attributes, if anything differs |
| undecided / keep | off | nothing (brightness and colour never switch a light on) |
| any | unavailable / unknown | nothing (no retries to a powerless bulb) |

"Differs" uses tolerances (brightness ±3/255, colour temperature ±60 K after clamping to the lamp's range, hue/
saturation ±4, effect by name), so an evaluation of a light that already is right sends nothing - re-evaluations
are idempotent, and there is no feedback loop (the evaluation is never triggered by its own light, except the opt-in
"manual change" detector, which ignores changes carrying an automation's context).

Matrix columns that can hold together (anything but the certain exclusions: opposite states of one source, different
states of one entity, disjoint numeric ranges or time windows) and want different values need a policy: an explicit
**ranking** (stored separately from the column order; every clashing column must be ranked), **ON/OFF wins**,
**highest/lowest**, or **nothing while they disagree**. Without one, deployment is blocked; the table order never
decides.

**0 %** is never an OFF by itself: any 0 % cell blocks until the user chooses *switch off*, *minimum (1 %)* or
*unchanged*.

## 3. Native compilation

Each light gets **its own automation** (`automation_builder_<device>_l_<hash>`, mode `queued`):

```
triggers   every column's start and end (presence entered/ended with delays, numeric crossings held for the delay,
           states to/from, times), the mood selector, the entities of "while" conditions (state/numeric/time/sun/
           template, both directions), the override helper, the re-evaluation event
           automation_builder_lighting {light}, optional time_pattern (reconciliation), optional "available again"
actions    variables  t_<matrix>: null, b_<matrix>: null, ab_mood: {}, a_<rule>: false
           choose     per matrix: branches in the explicit order -> variables [value, "by"]   (first match wins)
           choose     mood value -> ab_mood {ct|rgb+hs|hs, bri, effect, tr, by}
           if         per "while" rule: its native conditions -> a_<rule>: true
           variables  ab_ovr, ab_truth/ab_bright (combination), ab_on/ab_bri/ab_col/ab_fx/ab_tr (priority walk)
           variables  ab_cmd (the comparison template: service + exactly the resolved data)
           if         ab_cmd.service == turn_off -> light.turn_off data "{{ ab_cmd.data }}"
           if         ab_cmd.service == turn_on  -> light.turn_on  data "{{ ab_cmd.data }}"
```

Verified in Home Assistant 2026.11.1's source and engine: a `variables` action inside a `choose` branch assigns to
the enclosing scope, variables render in order, and service `data` may be a template rendering a mapping. The
variables are visible in Home Assistant's traces ("by" names the layer that decided).

The light's automation also holds its override release rules (timer finished; vacancy; chosen events; at start an
expired timer). Switch mappings live in the device's shared automation (`queued`); hold-to-dim waits and gets an
automation of its own (`restart`). "Resume" turns the helper off (which re-evaluates) or, without an active override,
fires `automation_builder_lighting` for the light. Everything is ordinary rules of the rule model (validated each
time); only generated rules may use the larger limits, a templated `data` and the fire-event action.

### Override helpers (durable state)

`input_boolean.ab_<device>_<light>_override` (on = override active) for every light a mapping can override (or all
lights with manual-change detection), and `timer.ab_<device>_<light>_override` (`restore: true`) for a timed
override. Lifecycle:

* **Created at deployment**, before the automations, through Home Assistant's helper-editor commands
  (`input_boolean/create`, `timer/create`); the App verifies the id and the entity, and **records** each one
  (`deploy/state.json` → `helpers`, diagnostics). A failed deployment deletes the helpers it created.
* **Kept** while needed; the review lists them (*create / keep / delete*).
* **Deleted** at the next deployment when no longer needed (after the automations stopped using them), and when the
  device is removed. Only recorded helpers are ever deleted.
* A helper of that name that the App did not create blocks deployment ("rename or delete it") and is never touched.
* The allowlist limits create to names `AB <8 hex> <object id> override` and delete to ids
  `ab_<8 hex>_<object id>_override`.

They live in Home Assistant's storage, survive restarts (an override and its timer continue; a timer that expired
during a restart is ended at start) and need no App.

## 4. Switches & Remotes

Discovery (`switches.py`): device triggers (`device_automation/trigger/list`: ZHA, MQTT/Zigbee2MQTT, Hue …),
event entities' `event_types`, latched switches' `off→on` / `on→off`; classification of single/double/triple/long/
release/up/down/on/off from Home Assistant's own words; manual `zha_event`, MQTT and event mappings. Latched and
momentary controls are distinct kinds; an event entity is never a matrix source. Roles: matrix source (a state
column), one-shot action, action + manual override, resume, run a one-shot Advanced Rule, select a mood, explicitly
*No action* (vs. *UNSET* = nothing assigned). Ordinary button rules of the device that a mapping expresses exactly
can be **moved** into Switches & Remotes (the rule is replaced in the same save: nothing runs twice). Two mappings
that toggle a light on the same press are a blocking conflict (the ordinary conflict check).

## 5. Simulation and live view

`evaluate_plan` replays the light's plan - the same branches with the same rule-model conditions, evaluated by the
simulation's three-valued evaluator - and the Python twins of the combination, priority walk and command templates.
Used by the Simulate tab (states + an optional press as an event; per light the matrices with holding columns, mood,
rules, override and its end, the winning layer per property, the command), by *Priority & Resolution → who controls
each light now* (Home Assistant's live states, held-for from `last_changed`) and by diagnostics.

## 6. Backward compatibility

* Version 1 designers (0.3) validate, compile and deploy exactly as before (golden hashes of 0.1 and every 0.3 test
  unchanged); the designer module dispatches by version. New designers and templates are version 2.
* **Conversion** is explicit (designer page → *Convert to version 2*), with the list of what changes; refused
  (nothing converted) when a part has no exact equivalent: permissions (gates), "both when compatible", schedule
  windows, colours/effects in situation cells, settings in defaults, hysteresis, a mood matrix that decides ON/OFF,
  OFF cells in the mood palette. Converted: presence/light-level deciding matrices (truth + brightness, ranking from
  the explicit 0.3 column priority), mood palette, timed entries (one-shot rules), override switch (a top "while"
  rule that keeps everything), manual pause (timed manual override with detection). Ids of lights, sources, entities
  and the designer are kept; the deployed automation keeps running until the user deploys.
* Ordinary rules (including 0.2 "from a dropdown helper" mood rules and button rules) are untouched unless the user
  moves a button rule into the designer.

## 7. Test results (2026-11-22, Home Assistant Core 2026.11.1)

"Stack" = the real App image behind the stand-in Supervisor, against a real disposable Home Assistant Core 2026.11.1
(test package of 0.3 + demo lights and event entity). All ten e2e and browser suites ran in order on a fresh stack;
the new suites ran again after the last interface fix.

| Suite | Environment | Result |
|---|---|---|
| `automation_builder`: `pytest` (incl. `test_lighting2.py`, 31 new) | unit, fake Home Assistant | 232 passed |
| `scripts/run_tests.py tests/automation_builder` (incl. `test_lighting2_runtime.py`, 82 new) | Home Assistant's parser and engine, real `input_boolean`, `timer`, `input_select`, MQTT | 140 passed |
| `e2e_checks.py` / `browser_checks.py` (0.1) | stack | 87 of 87 / 103 of 103 |
| `e2e_02_checks.py` / `browser_02_checks.py` (0.2) | stack | 87 of 87 / 121 of 121 |
| `e2e_03_checks.py` / `browser_03_checks.py` (0.2.1) | stack | 39 of 39 / 93 of 93 |
| `e2e_04_checks.py` / `browser_04_checks.py` (0.3, version 1 designers seeded as 0.3 saved them) | stack | 42 of 43 (see below) / 150 of 150 |
| `e2e_05_checks.py` (Lighting Designer 2) | stack | 41 of 41 |
| `browser_05_checks.py` (desktop light and dark, tablet landscape and portrait, phone) | stack | 195 of 195 |
| Smart Occupancy Detection: App / companion | regression | 678 passed / 38 passed |
| Smart Adaptive & Predictive Climate: App / companion | regression | 154 passed / 71 passed |
| Smart Comfort: App / companion / engine | regression | 325 passed, 1 xfailed, 2 known failures (below) / 43 passed / 1261 passed |
| Automation Import/Export Manager: App | regression | 79 passed, 1 environmental (below) |

Not regressions: e2e_04 check 56 needs Home Assistant's real sun above -10° and the run was after midnight in the
test system's time zone (it passed in the daytime runs of 0.3; the Cover Designer is unchanged). Smart Comfort:
`test_forecast_runtime::test_shadow_learns_through_history_and_compares` fails after 23:01 UTC and
`test_package::test_only_one_app_config_in_repository` has failed since the fifth App was added (both known, files
unchanged). The Import/Export Manager's `test_the_other_apps_are_untouched` asserts that no other App has uncommitted
changes in the working tree; it passes once this change is merged.

Runtime tests in Home Assistant's engine: the specification's four-light example (Truth + Brightness, two areas
occupied at once decided by an explicit ranking, nothing sent when nothing changes); 0 % is never an OFF; an Advanced
Rule (TV playing → OFF) ending while the zone and the mood changed → the light returns to the *current* baseline in
one command; mood changes colour only while on; a ZHA remote toggling exactly once with a timed override in real
`input_boolean`/`timer` helpers, the matrices ignored during it, and expiry restoring the current baseline; resume
(event entity) at once and without an override; a one-shot press undone by the next evaluation; hold-to-dim until
the release; override until vacancy; an override whose timer expired during a restart ended at start; manual-change
detection by context (attribute reports and the designer's own commands do not count); an unavailable light gets no
commands; switch and occupancy events at the same moment; a latched switch as matrix source and as an action (and
"back from unavailable" is no press); Zigbee2MQTT over MQTT; a switch selecting a mood; an Advanced Rule against a
switch override in both priority orders; and the Python twin compared with the engine for all 64 combinations of two
zones, TV, mood, override and the lights' initial state (identical commands, then nothing more to send).

End-to-end against the real Home Assistant: helpers created, owned, kept, deleted after the automations stopped
using them and removed with the device; three automations (one per light + switches); truth/brightness/mood/ranking;
the Advanced Rule releasing to the current baseline; the remote with a one-minute override and the resume button;
the live "who controls" view; a simulation prediction confirmed by Home Assistant; everything working with the App
stopped; an override and its timer surviving a Home Assistant restart and releasing afterwards; a 0.3 configuration
imported with a version 1 designer, kept, then converted on request.

Found and fixed by the tests: the start trigger of Home Assistant 2026.11 fires on `homeassistant_started`; a light is
evaluated only when an input changes (documented); labels must not reach variable values as template syntax
(sanitized); the interface saved switch actions before their mood/rule/release was chosen (now held until complete).

## 8. Limitations

* A light is evaluated when one of its inputs changes (or at the optional reconciliation), not at the moment of
  deployment or at a Home Assistant start (a start trigger would re-light lamps switched off by hand before a
  restart).
* Numeric columns have a delay but no hysteresis; checks at other moments use the plain limits.
* Delayed situations (`for:`) need the delay held: conditions use Home Assistant's own `for`.
* A manual override holds the light as it is; it has no values of its own to re-apply after a higher-ranked rule
  ends during it.
* Manual-change detection sees on/off changes without an automation context; brightness-only changes by hand and a
  bulb powered back by its wall switch (unavailable → on) are not detected.
* "While" rules with device conditions have no trigger of their own (re-evaluated at other changes or the
  reconciliation); templates with statements are noticed when they become true.
* Blueprint export of a version 2 designer: one blueprint per light automation; its values stay fixed (the
  generated templates are named in the warnings).
* Not run on Home Assistant OS in this phase (helpers are created with the same websocket commands the frontend
  uses; the rest uses no new interface).

## 9. Recommended next steps

1. A Home Assistant OS run with real Zigbee remotes (ZHA and Zigbee2MQTT) and a switch in detached mode.
2. "Press now" capture for interactions (listen for the next event of a device).
3. Override values (an override that re-applies its own brightness after a higher-ranked rule ends).
4. Hysteresis for numeric columns using the light's own state as memory.
5. Climate and ventilation designers on the same plan/twin structure.
