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

RESULTS_PLACEHOLDER
