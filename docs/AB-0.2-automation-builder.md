# Automation Builder 0.2 (Phase 0.2)

Home Assistant OS validation, native blueprint export, advanced triggers, conditions and actions, import of
existing automations and rule-level execution diagnostics. Built on the 0.1 architecture
([AB-0.1-automation-builder.md](AB-0.1-automation-builder.md)); nothing of it was rewritten.

| Part | Where | Version |
|---|---|---|
| App (slug `automation_builder`) | `automation_builder/` | 0.2.0 |
| New backend modules | `backend/ab/importer.py`, `backend/ab/diagnostics.py`; `blueprint.py` rewritten | |
| New interface module | `backend/ab/static/pages.js` (blueprint export, import, dropdown-helper shortcut) | |
| Runtime tests in a real Home Assistant | `tests/automation_builder/` (4 files) | |
| End-to-end, browser and Home Assistant OS checks | `automation_builder/tests/e2e/` | |

Smart Comfort and Smart Occupancy Detection are not changed (no file of theirs is touched). Still no companion
integration, no service calls, no `map`.

## 1. What changed in the architecture

```
ab/model.py        + 7 trigger types, 4 condition types, "not" groups, 10 action types with nested sequences;
                     waits(rule), walk_actions, rule_entities; device["blueprint"] (export choices), rule["imported_from"]
ab/compiler.py     + native output for all of them; _sequence (timeout that stops = "if wait.completed" around
                     the rest); wait_template from a condition tree; Input (a value a blueprint takes from an
                     input) passed through unchanged; blocks (which action belongs to which rule)
ab/conflicts.py    + actions inside branches are "possible" (warning); late_action (a waiting rule acts later)
ab/blueprint.py      candidates (stable identifiers), build (compile with Inputs, check, YAML with !input)
ab/importer.py       analyse: an automation's raw configuration -> rule model, or the list of what cannot be
ab/diagnostics.py    interpret (trace -> per rule: triggered / blocked by / executed / outcome), expectations and
                     verify (reported state against the commanded one), RuleStats (persisted), Observer
ab/ha.py           + allowlisted: trace/get, device_automation/*/list, blueprint/list, blueprint/save;
                     check_params limits traces to own automations and blueprint/save to the App's folder
ab/store.py        + DeployStore: "pending" (journal of a running deployment), "blueprints", "blocks" per record
ab/core.py         + recover (interrupted deployments), blueprint_view / blueprint_install, import_list /
                     import_analyse / import_convert, device_automations, notify_targets, rule views, the
                     duplicate guard in plan
ab/web.py          + routes for the above
static/editor.js     rewritten: nested actions and conditions by path; "More" groups; device and button pickers
static/pages.js      blueprint export screen, import list and analysis, rules from a dropdown helper
```

The configuration document keeps schema 1. Rules of 0.1 keep their meaning and, except as described under
*Consolidation*, compile to the **byte-identical** automation (pinned by a test with the hashes of 0.1.0's
output), so an update does not make deployed devices look changed.

### Consolidation and execution modes

0.1 kept a waiting rule with "run both at the same time" in the shared automation and switched that automation
to `parallel`. That is a mode change caused by one rule and applied to all, and in `parallel` two triggers that
arrive together no longer run in order. 0.2 separates on one criterion only:

* a rule **waits** if its actions contain a delay, a wait for a trigger or a condition, a `repeat` while/until,
  or a script that is waited for - at any depth (`model.sequence_waits`);
* every waiting rule gets its own automation in the mode the user chose for it (restart, single, queued,
  parallel), with the reason in the notes, the plan and the editor;
* everything else shares the device's automation, **always** `queued` (max 20).

Other sources of trouble are reported, not resolved silently: order-dependent results between rules that can run
together stay blocking conflicts (0.1); an action inside a branch that may collide with another rule is a
warning (`possible_overlap`); a waiting rule that acts on an entity after its wait while another rule controls
the same entity is a warning (`late_action`).

A wait whose timeout "stops" is compiled as `continue_on_timeout: true` followed by `if "{{ wait.completed }}"`
around the rest of that list. Home Assistant's `continue_on_timeout: false` aborts the entire run, which in a
shared automation would skip the rules listed after this one; within a rule's own automation the two are
equivalent, and the generated form is the same everywhere.

Trigger checks: two triggers of one rule can compile to one native trigger (two kinds of press of one event
entity; a time with different weekdays). "Which trigger started the rule" therefore compiles to the trigger id
*and* what tells the triggers apart, exactly like the rule's own trigger check.

### Blueprint export

`blueprint.candidates` walks the rules of one generated automation and lists every value that can become an
input, with every place it occurs. `blueprint.build` replaces the chosen values with `compiler.Input` objects in
a copy of the device and runs the ordinary compiler:

* the compiler passes an `Input` through wherever a value goes into the output, and decides everything
  structural (rechecks, separation, the expected state of a recheck) from `Input.value`, the value the rule has
  now - so a blueprint has exactly the shape of the managed automation;
* templates the compiler writes itself (occupancy rechecks, `wait_template`) refer to an input through an
  automation-level variable (`variables: {ab_<key>: !input <key>}`), because `!input` cannot stand inside a
  template; templates the user wrote are kept as written, and the export says so when one names an entity that
  is an input elsewhere;
* the result is checked: with every input at its default it must equal the device's automation (trigger ids
  are compared by content, since an id is a hash of content that now contains `!input`). Home Assistant then
  validates that configuration (`validate_config`).

Identifiers: `controlled_<object id>` / `<domain>_<object id>` for entities (one input per entity, wherever it
is used), `<rule id>_t<n>_<field>`, `<rule id>_c<n>_<field>`, `<rule id>_a<n>[...]_<field>` for values. The
choices (name, description, which inputs, per-input name and description) are stored in the device
(`device["blueprint"]`), so the next export is the same.

Installation was investigated in Home Assistant 2026.11.1 (`components/blueprint/websocket_api.py`,
`models.py`):

| Option | Verdict |
|---|---|
| The automation configuration endpoint | Does not handle blueprint files. Not used. |
| `map: homeassistant_config:rw` and writing the file | Works, but gives the App the whole configuration folder. Rejected. |
| A companion integration | A second thing to install for one file write. Rejected. |
| Websocket `blueprint/save` (`domain`, `path`, `yaml`, `allow_override`) | **Chosen.** The command behind Home Assistant's own "Import blueprint": validates against the blueprint schema, writes `blueprints/<domain>/<path>`, reloads the automations that use it when a file is replaced. Admin-only; available through the Supervisor's websocket proxy. |

Like the automation endpoint it is not a documented public API. It is confined the same way: one place
(`core.blueprint_install`), an allowlist rule that only lets `automation_builder/<name>.yaml` in domain
`automation` through, an explicit button, and a record of what was installed (a file the App did not install
is replaced only with a separate confirmation). Download always works without it.

### Import

`importer.analyse` reads the raw configuration (`automation/config`, already allowlisted for reads). Each
parser accepts exactly the keys it understands (`_Reader.keys`); anything else raises `_Unsupported`, which is
collected with its path and reason - for every trigger, condition and action, not only the first. A conversion
exists only if the list is empty **and** the result passes the ordinary rule validation. Conversions that are
not literal are listed as notes: a condition between actions becomes `if ... then` around the rest (the same
effect inside one rule), lists of entities or states become several triggers or an OR group, `wait_template`
becomes "wait until a condition holds" with that template, the cosmetic `alias` of steps is dropped.

The copy carries `imported_from` (the original's id and alias). `core.plan` finds the original by that id; while
it is turned on, the plan has a blocking reason and `duplicates`, until the user turns the original off in Home
Assistant or sets `allow_both` on the rule. The App cannot touch the original: there is no service call in its
allowlist.

Faithfulness is tested differentially in a real Home Assistant: originals and their imported copies are loaded
side by side and must make the same service calls under the same events
(`tests/automation_builder/test_import_diag_runtime.py`).

### Rule-level diagnostics

The compiler knows which top-level action of an automation is which rule (`blocks`, stored with every
deployment record; derived from the block aliases for records written by 0.1). `diagnostics.interpret` reads a
trace (`trace/get`) by those positions:

| Trace path | Meaning |
|---|---|
| `action/<k>/if/condition/0` true | the run was started by one of the rule's triggers |
| `action/<k>/if/condition/<n>` false | blocked by the rule's n-th condition (described from the deployed rule) |
| `action/<k>/then/...` present | the rule's actions started; service steps carry the rendered call |
| a step with `error`, the trace's `state` / `script_execution` | failed / still running / stopped |

`Observer` reads traces when an own automation's `last_triggered` changes (state events the App already
receives) and when a device page is opened; it never polls. Results go to `/data/diag/rules.json`.

Verification (`expectations`, `verify`): the last call a run made to each entity is turned into an expectation
through `catalog.effect` (the same table the occupancy rechecks and the conflict check use) plus a few
attributes with tolerances (brightness, colour temperature clamped to the lamp's range, position, target
temperature, fan speed, humidity, volume, a number). It is compared with the entity's state a few seconds after
the run (longer for a transition). The run's context id tells "nothing else touched it" (mismatch) from
"something else changed it afterwards" (not judged). A run seen more than three minutes late is not verified.

No supervision loop was added: the observer sends reads only, and there is no code path from a verdict to a
command.

### Interrupted deployments

`DeployStore.begin` writes what a deployment is about to do (per automation: hash, configuration, blocks, or
"delete") before the first request; `finish` removes it. `AppCore.recover` runs when the connection to Home
Assistant is (re-)established and a note exists: an automation whose content in Home Assistant equals the
intended one is recorded as deployed; a removed one is forgotten; anything else is left for the normal review
(shown as pending or as changed outside). Recovery only reads.

## 2. Supported building blocks

| | Types |
|---|---|
| Triggers | state (optionally an attribute, from/to, "for"), numeric threshold (attribute, "for"), time of day or a date/time helper (weekdays), sunrise/sunset with offset, occupancy (with rechecks), **button / remote as event entity** (event types), **device trigger** (discovered), **event** (manual), **MQTT** (manual), **template** ("for"), **time pattern**, **Home Assistant start/shutdown**; helper changes and sun elevation as presets of state / numeric |
| Conditions | state (attribute, is / is not, "for at least"), number (attribute), time range and weekdays, **sun**, **template**, **trigger** (which of the rule's triggers), **device condition** (discovered); groups **all of / any of / none of**, nested three levels, also inside actions |
| Actions | ready-made entity actions and hand-entered actions (templates allowed in data), delay, **if / then / else**, **choose** with default, **wait for trigger**, **wait until condition** (both with timeout: continue or stop), **repeat** (count, while, until), **variables**, **notification** (notify action or notify entity), **script** (start, or run and wait), **scene**, **device action** (discovered, for the devices of the controlled entities); nested four levels, 60 actions per rule |

Not supported (reported by the importer, absent from the editor): `stop`, `parallel`, `sequence`, `repeat
for_each`, fired events, response variables, `continue_on_error`, trigger variables, automation-level
variables, triggers zone / geo / calendar / tag / webhook / conversation / persistent notification, zone
conditions, targets by area, device or label.

## 3. Home Assistant OS test procedure

Reproducible on any Home Assistant OS **test** installation (here: the VirtualBox VM `sod-haos-test`, HAOS
18.4, Supervisor 2026.11.0, Core 2026.11.1, reachable at `127.0.0.1:28080`). Never on a production system.

1. An administrator's long-lived token in a JSON file `{"llat": "..."}`; `export AB_HAOS_TOKENS=<file>`.
2. Test entities: copy `tests/e2e/ha/ab_test_package.yaml` to `/config/packages/ab_test.yaml`, add
   `homeassistant: packages: ab_test: !include packages/ab_test.yaml` to `configuration.yaml`, replace
   `automations.yaml` with `tests/e2e/ha/automations.yaml`, restart Core.
3. Install: add the repository (`python haos.py sup /store/repositories post '{"repository": "<url>#<branch>"}'`),
   then `sup /store/addons/<slug>/install post '{}' 1500` and `sup /addons/<slug>/start post '{}'`;
   `export AB_HAOS_SLUG=<slug>`.
4. Update test: install from a branch that points at 0.1.0, run `haos_checks.py --phase before-update`, move the
   branch to 0.2.0, `sup /store/reload post`, `sup /store/addons/<slug>/update post '{}' 1500`, run
   `haos_checks.py --phase after-update`.
5. `eval "$(python haos.py env)"`, then `e2e_checks.py <tokens> --restart-app "python haos.py app restart"
   --restart-ha "python haos.py core restart"` and `e2e_02_checks.py <tokens> --stop-app "python haos.py app
   stop" --start-app "python haos.py app start && python haos.py wait" --restart-ha "python haos.py core restart"`.
   Checks that need the stand-in Supervisor's hooks print `SKIP` and are not counted.
6. `haos_checks.py --phase main` (installation facts, Ingress, proxy, create / update / remove, reload,
   restarts of App, Core and Supervisor, Supervisor backup and restore, interrupted deployment).
7. With Smart Occupancy Detection installed and paired on the same system: `export AB_HAOS_SOD_SLUG=<slug>`,
   `sod_checks.py` (about 35 minutes; it waits out the start-up grace once).

`e2e_checks.py` expects a fresh installation of the App. Before a repetition: uninstall and install the App
(its data folder goes with it), delete the `automation_builder_*` automations and
`/config/blueprints/automation/automation_builder/` in the test Home Assistant.

## 4. Results (2026-11-21, Home Assistant Core 2026.11.1)

Simulated and genuine environments are listed separately. "Stack" is the real App image behind a stand-in
Supervisor against a real, disposable Home Assistant Core; "HAOS" is the VirtualBox VM with Home Assistant OS
18.4 and Supervisor 2026.11.0.

| Suite | Environment | Result |
|---|---|---|
| `automation_builder`: `pytest` | unit, fake Home Assistant | 152 passed |
| `scripts/run_tests.py tests/automation_builder` | Home Assistant's own parser and engine (test harness) | 40 passed |
| `e2e_checks.py` (the 0.1 scenario) | Stack, fresh installation | 87 of 87 |
| `browser_checks.py` | Stack | 102 of 102 |
| `e2e_02_checks.py` | Stack | 87 of 87 |
| `browser_02_checks.py` | Stack | 121 of 121 |
| `haos_checks.py --phase before-update` / `after-update` | **HAOS**, Supervisor update 0.1.0 → 0.2.0 | 5 of 5 / 15 of 15 |
| `e2e_checks.py` | **HAOS**, real Ingress and Supervisor proxy, fresh installation of 0.2.0 | 80 of 80, 4 skipped |
| `e2e_02_checks.py` | **HAOS** | 81 of 81, 2 skipped |
| `haos_checks.py --phase main` | **HAOS** | 48 of 48 |
| `sod_checks.py` | **HAOS**, real Smart Occupancy Detection 0.2.0 (App + companion) | 38 of 38 |
| Smart Occupancy Detection: App `pytest` / companion suite | regression | 678 passed / 38 passed |
| Smart Comfort: App `pytest` / companion / engine suites | regression | 327 passed, 1 xfailed (as before) / 43 passed / 1261 passed |

Skipped on HAOS (not run there, covered on the stack): a deployment refused by the endpoint and an unreachable
API (they need the stand-in Supervisor's failure hook; on Home Assistant OS Ingress itself runs through Core,
so a failing Core cannot be observed through the App's interface), a non-administrator (the real Supervisor
sets the user), the deterministic interrupted deployment (the stand-in's slow answer) and the Supervisor-side
recording of everything the App sent.

### Home Assistant OS

* **Installation and start**: built on the device by the Supervisor from the repository, starts under S6,
  Ingress only (no host port), sidebar panel for administrators. Supervisor's view of the App:
  `homeassistant_api` only; no `hassio_api`, `auth_api`, host network, privileges, Docker API or full access;
  protected; security rating 7.
* **The automation configuration endpoint through the real Supervisor proxy**: `GET`, `POST` and `DELETE` on
  `http://supervisor/core/api/config/automation/config/automation_builder_*` work with the App's
  `SUPERVISOR_TOKEN` and need no further permission. `validate_config`, `automation/config`, `trace/list`,
  `trace/get`, `search/related`, `device_automation/trigger/list`, `blueprint/list` and `blueprint/save` work
  through the websocket proxy. On Home Assistant OS the App acts as the system user "Supervisor".
* **Create, update, remove, verify**: all confirmed against Home Assistant with the test's own token; the four
  hand-made automations in the same `automations.yaml` were identical afterwards.
* **Home Assistant rejects an update**: a broken template is refused by Home Assistant's validation, reported,
  and nothing is written (the refusal of the write itself is covered on the stack).
* **Reload, restarts**: "reload automations", an App restart, a Core restart and a Supervisor restart each left
  the automations loaded and the devices in sync; after the Supervisor restart a deployment through its proxy
  worked.
* **Backup and restore**: a Supervisor partial backup of the running App, changes afterwards, restore: rules,
  names and deployment records of the backup were back, and the automation in Home Assistant was untouched.
* **Update**: the Supervisor updated 0.1.0 to 0.2.0. Devices, rules, records and automatic backups were kept;
  a device with instant rules was in sync without redeploying (the same automation byte for byte); the device
  with a "run both" waiting rule showed "changes not deployed yet" with the reason, kept running its 0.1
  automation, and got the new layout when deployed. A device made in 0.1.0 exported as a blueprint.
* **Interrupted deployment**: the App was stopped through the Supervisor during deployments. Attempt 1: the
  deployment had finished (answered 200), in sync after the restart. Attempt 2: the request ended with 502 and
  Home Assistant had not received the automation; after the restart the device showed "changes not deployed
  yet" and deployed normally. The case "written, not yet recorded" was produced deterministically on the stack
  (the App killed while Home Assistant's answer was delayed): the next start recorded it as deployed, with a
  message, and wrote nothing.
* **Blueprint**: installed through `blueprint/save` into `blueprints/automation/automation_builder/`; an
  automation created from it with Home Assistant's own endpoint, with another light, another trigger entity
  and other values, ran correctly while Automation Builder was stopped.

### Real Smart Occupancy Detection

Both Apps on the HAOS VM; `binary_sensor.sod_kamar_nordur_occupied` comes from Smart Occupancy Detection's
companion. Only its timing settings were shortened for the test (global and the room's own override) and put
back afterwards. Observed:

* vacant → occupied and occupied → vacant decided by Smart Occupancy Detection, each followed by the rule;
* a rule whose condition did not hold (bright) did nothing when the room became occupied;
* a lost first command corrected by the 2-second recheck (lamp on 2.0 s after the occupancy change), by the
  15-second recheck when the command path was down for 6 s, and by the periodic check when it was down
  through both; afterwards no further commands;
* a lamp switched off by hand in the occupied room stayed off through all rechecks, and the App reported the
  difference instead of forcing it; the next stay turned the lamp on again;
* an unavailable occupancy entity of Smart Occupancy Detection (a room that is not configured) reported as
  unavailable;
* Smart Occupancy Detection stopped: its entity reported `fresh: false`; a lost command was **not** corrected
  during that time (two periodic checks passed); after its restart the entity was fresh again and the lamp
  was reconciled;
* Automation Builder stopped: occupancy and vacancy kept switching the lamp (native automation); after its
  start the device was in sync and the rule diagnostics had caught up from Home Assistant's traces;
* Home Assistant restarted with the room occupied and the lamp switched off by hand: the lamp stayed off
  (start-up grace); afterwards the rules worked as before.

Automation Builder has no occupancy logic: the generated automation reads the entity's state and its `fresh`
attribute, nothing else. Smart Occupancy Detection's code and detection rules were not changed.

### What the App sent (stack, one complete run of all four suites)

Websocket: `subscribe_events`, `get_config`, `get_states`, `get_services`, the three registry lists,
`config/auth/list`, `ping`, `validate_config` (86), `automation/config` (1099), `search/related` (32),
`trace/list` (255), `trace/get` (93), `device_automation/trigger/list` (4), `blueprint/list` (4),
`blueprint/save` (3, all `automation_builder/<device>.yaml`). HTTP: only
`config/automation/config/automation_builder_*` (104 GET, 28 POST, 2 DELETE). No service call, no event, no
registry change; traces only of its own automations.

### Defects found by the tests and fixed in this phase

* Two kinds of press of one button are one native trigger; a "which trigger" condition could not tell them
  apart (now the condition includes what distinguishes them).
* The very first event of a button (state `unknown` before it) was ignored.
* The importer named only the first unsupported action of an automation.
* A scheduled entity search could run after its page was left (a console error; present in 0.1).
* A trace refresh that was rescheduled left a never-awaited coroutine (a warning in the log).

## 5. Known limitations

* Both write paths (the automation configuration endpoint, `blueprint/save`) are internal interfaces of Home
  Assistant, isolated and verified as described; a Home Assistant release may change them.
* Blueprint identifiers of *values* depend on the position inside the rule: removing or reordering triggers,
  conditions or actions within a rule renames the inputs after that position. Templates written by the user are
  not parameterised. A device with waiting rules exports one blueprint per automation. An installed blueprint
  is removed in Home Assistant, not in the App.
* An occupancy rule with advanced actions (branches, waits, variables ...) gets no rechecks.
* Device triggers, conditions and actions are labelled from Home Assistant's identifiers, not with the
  integration's translated texts. ZHA and Zigbee2MQTT hardware was not available: device triggers were
  exercised with Home Assistant's own device automations (demo devices, the test harness), event entities with
  the demo button and simulated state changes, `zha_event`-style triggers with real events fired on the bus.
  MQTT triggers are generated and unit-tested but were not run against a broker.
* The importer converts one automation into one rule and requires at least one controlled entity; it refuses
  rather than approximates (for example a state trigger without `to`/`from`, which also fires on attribute
  changes). Automations defined in packages or made from blueprints are not imported.
* Rule diagnostics depend on Home Assistant's stored traces (the last 5 runs per automation, 20 with periodic
  checks; none across a Home Assistant restart). Runs that fall out while the App is stopped are not seen.
  Verification is a comparison with the *reported* state within three minutes of a run; it is not a
  supervisor and corrects nothing.
* On Home Assistant OS a failing Core cannot be told from a failing Ingress from outside; those error paths are
  tested on the stack only. A real SIGKILL of the App was not possible on Home Assistant OS (no Docker access);
  a Supervisor stop during deployments was used instead.
* Conflict detection is still per device; overlaps with unmanaged automations are listed by entity (and, for
  imports, by trigger and entity), not analysed further.

## 6. Recommended Phase 0.3

1. The visual blueprint designer: edit an exported blueprint's inputs, groups and order; stable identifiers
   that survive reordering inside a rule (ids per trigger, condition and action); update of an installed
   blueprint with a comparison; blueprint import into rules.
2. Buttons on real hardware: ZHA and Zigbee2MQTT remotes, translated labels for device automations, a "press
   the button now" capture that finds the event or entity.
3. Rechecks for rules with simple branches (if/else on a state) and for rules that follow a button.
4. Supervision built on the diagnostics: a per-device history, notification on repeated mismatches, and
   explicitly configured retries with the safeguards of the occupancy rechecks.
5. Import: several rules from one automation (per trigger id), `for_each` and `stop`, "replace the original"
   as an explicit, reversible step.
6. Cross-device and unmanaged-automation overlap analysis.
