# Automation Builder 0.1 (Phase 0.1)

An independent Home Assistant App: a device-first, visual IF → AND/OR → THEN rule editor and a compiler that
turns the rules of one controlled device into one native Home Assistant automation.

| Part | Where | Version |
|---|---|---|
| App (slug `automation_builder`, panel "Automation Builder") | `automation_builder/` | 0.1.0 |
| Backend package | `automation_builder/backend/ab/` | |
| Interface (no build step) | `automation_builder/backend/ab/static/` | |
| Runtime tests in a real Home Assistant | `tests/automation_builder/` | |

No companion integration. Smart Comfort (`smart_lighting/`, `custom_components/smart_occupancy`) and Smart
Occupancy Detection (`smart_occupancy_detection/`, `custom_components/smart_occupancy_detection`) are not
changed; the only edits outside the new folders are the list of App folders in
`smart_lighting/tests/test_package.py`, the suite list in `scripts/run_tests.py` and a note in `README.md`.

## 1. Audit: what was reused

| Existing component | Decision |
|---|---|
| App conventions of Smart Occupancy Detection (`config.yaml`, `Dockerfile`, pinned base image and wheels, S6 service, Ingress only, `panel_admin`, `backup: hot`) | **Same conventions**, own files. Added dependency: PyYAML (the YAML preview). |
| `sod/ha.py` (websocket client: reconnect, start-up resync, ping, allowlist) | **Copied** and reduced (no companion protocol); a second, HTTP allowlist added (`AutomationConfigApi`). Never imported across Apps. |
| `sod/settings.py`, `sod/logbuffer.py` | Copied (`SOD_*` → `AB_*`). |
| `sod/config.py` `ConfigStore` (revision, atomic write, `.bak`, dated backups, recovery, export/import) | Copied into `ab/store.py`; a second store holds the deployment records. |
| `sod/web.py` (Ingress-only access, CSRF header, same-origin check, administrator check, CSP, base href) | Copied; reads are administrator-only too (the App shows the home's configuration). |
| SOD interface (`app.css`, helper functions, save-on-change) | Stylesheet reused as the base; the rule editor saves on change, deployment is the one explicit step. |
| SOD e2e stack (`stack.sh`, `fake_supervisor.py`, docker network `sl-hassio-test`) | Copied; the fake Supervisor also proxies `/core/api` and records every HTTP request. Fixed address 172.30.33.251, port 18400, so all three stacks can run side by side. |
| Smart Comfort's lighting, mood, occupancy and supervision engines | **Not used.** Automation Builder generates native automations and runs nothing itself. |

## 2. Deployment mechanism (investigated on Home Assistant 2026.11.1)

Read in the installed source (`homeassistant/components/config/automation.py`, `config/view.py`,
`websocket_api/commands.py`, `automation/__init__.py`, `trace/websocket_api.py`, `search/__init__.py`):

| Need | Mechanism | Notes |
|---|---|---|
| Create / update | `POST /api/config/automation/config/<id>` | Validates with `async_validate_config_item`, writes `automations.yaml` atomically, then calls `automation.reload` for that id only. 400 with the message when invalid. |
| Read | `GET` on the same path | 404 when absent. |
| Delete | `DELETE` on the same path | Also removes the entity from the entity registry. |
| Validate without writing | websocket `validate_config` | Per key (`triggers`, `conditions`, `actions`): valid or the error text. |
| What an entity runs | websocket `automation/config` | The raw configuration of the loaded automation entity. |
| Traces | websocket `trace/list` | Shown per automation; links to `/config/automation/trace/<id>`. |
| Overlaps | websocket `search/related` | Automations that reference an entity. |

There is no websocket command and no documented REST call for saving an automation. Alternatives considered:
writing `automations.yaml` directly (needs `map: homeassistant_config:rw`, a full `automation.reload`, and
the App would edit a user's file with its own YAML writer) and a companion integration (a second installation
that would end up doing the same file write). Both rejected; the config endpoint is what Home Assistant's own
editor uses, needs no extra permission beyond `homeassistant_api`, validates server-side and reloads one
automation.

Because the endpoint is internal, it is isolated and verified:

* the only code that knows it is `ab/ha.py: AutomationConfigApi`, which refuses every id that does not start
  with `automation_builder_` before a request is made;
* every write is confirmed independently: the automation entity must exist, must not be `unavailable`, and
  `automation/config` must return exactly the deployed configuration; otherwise the deployment is undone;
* a missing `automation: !include automations.yaml` (the endpoint then saves without loading) is detected by
  that confirmation and reported in those words.

Permissions: `homeassistant_api: true`, `hassio_api: false`, no `map`, Ingress only, `panel_admin`.

## 3. Architecture

```
ab/model.py       the rule model and its validation (canonical form); registries of trigger/condition/action types
ab/catalog.py     controllable domains, ready-made actions, the effect of an action (state / idempotent / not)
ab/compiler.py    device rules -> native automation configuration (deterministic), YAML preview
ab/conflicts.py   contradictions between rules (blocking until resolved or confirmed)
ab/describe.py    readable "IF ... AND ... THEN ..." summaries
ab/blueprint.py   the reusable inputs of a device's rules (blueprint readiness)
ab/store.py       ConfigStore (rules, settings, backups) and DeployStore (what was deployed: ownership, history)
ab/discovery.py   entity and device discovery from states and registries
ab/ha.py          websocket client and automation configuration endpoint, both allowlisted
ab/core.py        analysis, deployment plan, deploy / confirm / undo, restore, diagnostics
ab/web.py         Ingress-only HTTP API and pages
ab/static/        ui.js (helpers, dialogs, entity picker), app.js (pages), editor.js (rule editor), app.css
```

**Rule definitions and generated automations are separate.** The configuration document holds devices
(`d_<8 hex>`), each with explicit target entities and rules (`r_<8 hex>`). The automation is derived:
`automation_builder_<device id>` (and `..._<rule id>` for a rule that must run separately). The deployment
records hold, per automation id, the exact configuration written, its hash, when, by whom, and the device as it
was (for restore); they are the App's proof of ownership.

### Compilation

One automation per device: every distinct trigger once (id = kind + hash of its content, so ids do not move when
other rules change), no top-level condition, and one `if` block per rule in the listed order:
`if [started by one of this rule's triggers, rule conditions] then [actions]`. `choose` is deliberately not
used: it runs only the first matching branch, which would be a silent priority between rules that share a
trigger. Extra conditions that belong to one trigger (weekdays of a time trigger) are attached to that trigger's
alternative only.

Execution mode: `queued` (max 20) when no rule waits; a rule with a delay and retrigger behaviour `parallel`
stays in the shared automation, which then runs `parallel`; a delay rule with `restart`, `single` or `queued`
gets its own automation in that mode, with the reason shown, because that mode would cancel, ignore or block the
other rules.

Conflicts (`conflicts.py`): two enabled rules that can fire together (identical or overlapping triggers) and
whose conditions do not provably exclude each other are compared per controlled entity: opposite resulting
states, a toggle against anything, two toggles, the same action with different data, an exact duplicate.
Order-dependent results are **blocking** until the rules change or the user confirms the listed order for that
pair. Nothing is prioritised silently.

### Occupancy rechecks

Generated only for rules with an occupancy trigger, into the same automation, as triggers:

* `state` `from: off, to: on` (immediate; the rule's normal block);
* `state` `to: on, for: 2 s` and `for: 15 s`;
* `time_pattern` every N minutes, with a top-level condition that ends a periodic run while no occupancy rule's
  state is current (and 20 stored traces instead of 5, so periodic runs do not push real runs out).

The recheck block runs the rule's actions only if: the occupancy entity is in the rule's state now (not
unavailable/unknown), `fresh` is not `false`, the occupancy change happened at least the start-up grace after
the automation was loaded, the optional override entity is `off`, the rule's conditions hold, and a controlled
entity is available, not in the state the actions leave it in, and has not changed since the occupancy change.

Design decisions behind this:

* **No waiting sequences.** `for:` triggers are cancelled by Home Assistant when the state changes and are
  simply not fired across a restart; nothing blocks the other rules.
* **Only safe actions are repeated.** Rules whose actions are not all idempotent and verifiable (toggle, delay,
  hand-entered actions, nothing to verify) get no recheck triggers at all, with a note.
* **Manual changes.** Home Assistant cannot say who changed an entity. The rule "an entity that changed after
  the occupancy change is left alone" never overrides a person, at the price of not correcting a device that
  bounced. The mismatch is reported in the App (`core._mismatches`), not forced.
* **Restarts.** After a restart every `last_changed` is new, so the comparison above is meaningless: the
  start-up grace (default 300 s after the automation was loaded) disables corrections for occupancy changes that
  early. An earlier design with a `homeassistant: start` recheck was dropped for this reason.

### Safe publishing

`plan` → shown → `deploy(plan_hash, overwrite)` → confirm → record; see `core.py`. The plan hash covers the
configuration revision, every operation, the hash of what would be written and the hash of what is in Home
Assistant, so a deployment is refused when anything changed since the user looked. External changes are
classified `modified`, `missing` or `foreign` (exists with the App's id prefix but without a record); `modified`
and `foreign` need an explicit overwrite. A failed step restores what the deployment had already replaced.

### Blueprint readiness

`blueprint.inputs_of(device)` enumerates every value a blueprint would expose (controlled entities, trigger and
condition entities, thresholds, durations, states, action data) with a stable path and a selector kind. The
compiler reads exactly these fields, so a blueprint export is a substitution of `!input` references in its
output plus an `input:` section; no change of the model is needed.

## 4. Tests

| Suite | Command | What |
|---|---|---|
| App unit and workflow | `cd automation_builder && pytest` | model, compiler, conflicts, summaries, the whole workflow against a fake Home Assistant, HTTP API, packaging |
| Runtime in Home Assistant | `python scripts/run_tests.py tests/automation_builder` | the compiler's output loaded by Home Assistant 2026.11.1's automation parser and run by its engine, with states and time driven by the test |
| End to end | `automation_builder/tests/e2e/` | the real image through a fake Supervisor (Ingress, Core proxy) against a disposable Home Assistant 2026.11.1 |
| Browser | `tests/e2e/browser_checks.py` | every page at desktop (light, dark), tablet, phone; a device and rules created, deployed and deleted in the interface |
| Regression | the other Apps' and integrations' suites | unchanged and passing |

Coverage of the required scenarios:

| # | Requirement | Where |
|---|---|---|
| 1 | Fresh installation and connection | e2e 1 |
| 2 | Device and entity discovery | `test_core.py`, e2e 2, browser flow |
| 3 | Basic IF/THEN | runtime `test_basic_if_then`, e2e 3, browser flow |
| 4 | Several rules in one automation | `test_compiler.py`, runtime `test_five_rules_in_one_automation`, e2e 4 |
| 5 | Editing and disabling rules | `test_core.py`, runtime, e2e 5 |
| 6 | Conflict detection | `test_compiler.py`, `test_core.py`, e2e 6 |
| 7 | Non-idempotent actions | runtime `test_toggle_is_never_repeated`, e2e 7 |
| 8 | Rule execution in genuine Home Assistant | runtime suite, e2e 3-11 |
| 9 | Occupancy triggers and rechecks | runtime (11 tests), e2e 9 |
| 10 | Stale / unavailable occupancy | runtime `test_no_correction_without_trustworthy_occupancy`, e2e 10 |
| 11 | Works without occupancy or mood systems | runtime `test_ordinary_rules_work_without_occupancy_or_mood`; the e2e Home Assistant has neither Smart Comfort nor Smart Occupancy Detection installed |
| 12 | External modification | `test_core.py`, e2e 12 |
| 13 | Failed deployments and recovery | `test_core.py`, e2e 13 |
| 14 | Backup and restore | `test_core.py`, e2e 14 |
| 15 | Desktop, tablet, phone | browser checks |
| 16 | Smart Comfort and Smart Occupancy Detection unaffected | their suites, `test_package.py`, e2e 16 (the hand-made automation is byte-identical) |

### Disposable Home Assistant for the e2e run

A Home Assistant Core 2026.11.1 in a virtual environment, bound to 127.0.0.1 and 172.30.32.1 on port 18123,
with `default_config`, `demo`, `automation: !include automations.yaml` (containing one hand-made automation with
id `1700000000001` that toggles `light.bed_light` on `input_button.wall_switch`), and test drivers:
`input_boolean.test_motion` → `binary_sensor.test_motion`; `input_number.test_lux` → `sensor.test_lux`;
`input_select.mood` (Dagur, Hygge, Nátt); `input_boolean.occ_kamar` / `occ_available` / `occ_fresh` →
template `binary_sensor.sod_kamar_occupied` (state, availability, attribute `fresh`); a template
`light.flaky_lamp` whose commands are dropped while `input_boolean.drop_commands` is on (a lost command).
`tests/e2e/prepare.py` onboards it and creates the App's own administrator; `stack.sh up` builds and starts the
image; `e2e_checks.py <tokens.json> --restart-ha "<stop && start>"`; then `browser_checks.py <dir>`.

## 5. Known limitations

* The deployment endpoint is internal to Home Assistant (section 2). `automations.yaml` must be included in
  `configuration.yaml`; saving rewrites that file as Home Assistant's own editor does (comments are lost).
* No native device actions, templates, `choose`, `repeat`, `wait_for_trigger`, or import of existing
  automations. A hand-entered action is passed through unchanged and is never repeated automatically.
* Conflict detection compares rules of the same device only, and proves exclusivity only for simple cases (same
  entity with different states, non-overlapping numeric ranges, different weekdays); anything else that fires
  together is reported for the user to decide. Overlaps with unmanaged automations are listed by entity, not
  analysed.
* Occupancy rechecks: an occupancy sensor that becomes unavailable and returns as occupied looks like a new
  occupancy period; corrections are off during the start-up grace and after each deployment until the next
  occupancy change; a manual change and a device that reports late cannot be told apart, so both are left alone.
* A running delay is cancelled by a Home Assistant restart (Home Assistant's behaviour).
* The App verifies deployments, not executions: there is no runtime supervision in 0.1.
* Not installed on Home Assistant OS in this phase; the image runs under S6 with a stand-in Supervisor.

## 6. Recommended Phase 0.2

1. Installation on genuine Home Assistant OS (the existing VirtualBox VM), real Ingress and Supervisor backup.
2. Blueprint export of a device's rules (inputs from `blueprint.inputs_of`), then a blueprint editor.
3. More building blocks: templates as conditions, `wait_for_trigger` with timeout, scenes, notifications with a
   friendly form, native device triggers (buttons and remotes).
4. Import of an existing simple automation into rules, refusing whatever cannot be represented.
5. Rule-aware supervision: per-rule "last ran / last blocked by" from traces, and action verification.
6. Mood conveniences (a per-device mood table) on top of the ordinary `input_select` rules.
7. Deeper overlap analysis with unmanaged automations (same trigger and same target).

## 7. Results of the final run (2026-11-21, Home Assistant Core 2026.11.1)

| Suite | Result |
|---|---|
| `automation_builder`: `pytest` | 86 passed |
| `scripts/run_tests.py tests/automation_builder` (real Home Assistant parser and engine) | 24 passed |
| `tests/e2e/e2e_checks.py` (real image, real Home Assistant, incl. App and Home Assistant restarts) | 86 of 86, twice (two fresh installations) |
| `tests/e2e/browser_checks.py` | 102 of 102 |
| Smart Occupancy Detection: App `pytest` / companion suite | 678 passed / 38 passed |
| Smart Comfort: App `pytest` / companion / engine suites | 327 passed, 1 xfailed (as before) / 43 passed / 1261 passed |

What the App sent to Home Assistant during one complete e2e and browser run, recorded by the stand-in Supervisor:
websocket `subscribe_events`, `get_config`, `get_states`, the three registry lists, `config/auth/list`, `ping`,
`validate_config` (41), `automation/config` (287), `search/related` (19), `trace/list` (37); HTTP only
`config/automation/config/automation_builder_*` (66 GET, 17 POST, 2 DELETE). No service call, no event, no other
path. The hand-made automation in the same `automations.yaml` was byte-identical afterwards.

Observed in the real Home Assistant, among others: a rule turning the demo lamp on at exactly 70 % only while the
lux helper was below 100; four rules in one automation; a lost command corrected by the 2-second recheck and
nothing sent afterwards; a lamp switched off by hand left off through the 15-second and periodic rechecks, also
across a Home Assistant restart with the room still occupied; no correction while the occupancy entity was
`unavailable` or reported `fresh: false`, and reconciliation by the periodic check once it was trustworthy
again; a toggle run exactly once; an edit and a deletion made through Home Assistant's own endpoint detected
and not overwritten without confirmation; a refused deployment leaving the previous automation in place.
