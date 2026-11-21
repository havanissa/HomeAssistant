# Automation Builder

Automation Builder lets you build Home Assistant automations as rules, without writing YAML:

> **IF** motion is detected **AND** illuminance is below 100 lux **THEN** turn the lamp on at 70 %.

You choose a device to control and give it as many rules as you like. Automation Builder combines the rules of
one device into **one native Home Assistant automation**, lets Home Assistant validate it, and deploys it only
when you ask. The automations then run inside Home Assistant like any other: Automation Builder does not need to
be running for them to work.

It is independent: no companion integration, no Smart Comfort, no Smart Occupancy Detection.

## Installation

Settings → Apps → App store → ⋮ → Repositories → add `https://github.com/havanissa/HomeAssistant` (if it is not
there yet), install **Automation Builder** and start it. It opens from the sidebar, for administrators.

Home Assistant must load `automations.yaml`, which it does by default (`automation: !include automations.yaml` in
`configuration.yaml`). Tested with Home Assistant Core 2026.11.1.

## The pages

- **Overview**: every managed device with its rules, its automation's status and warnings.
- **Devices**: add and manage devices; each device page has its rules with their execution diagnostics, the
  review and deployment, and **Export as blueprint**.
- **Rule Editor**: every rule, per device; create and edit rules.
- **Import**: the automations that already exist in Home Assistant, to analyse and copy.
- **Settings**: occupancy recheck defaults, backup and restore, how deployment works, diagnostics.

## Devices

A *device* in Automation Builder is **what one automation controls**: one entity, or several entities you chose to
control together. **Add a device** lists Home Assistant's physical devices with their controllable entities, and
the entities that have no device. A physical device can have several outputs (a dual relay, a light with a fan):
tick exactly the ones you mean. Nothing is selected for you, and an area or a whole device is never controlled
implicitly.

Any controllable domain works: lights, switches and relays, fans, covers, climate, humidifiers, locks, valves,
sirens, media players, vacuums, water heaters, numbers, selects, buttons, scenes, scripts and their helper
counterparts. Domains with ready-made actions show them in the editor; any other action can be entered by hand.

## Rules

A rule has three parts. A simple rule needs only the first entries of each list; everything else is under
**More** in the same lists and appears only when you choose it.

**IF** - one or more triggers; the rule starts when any of them happens:

| Trigger | Example |
|---|---|
| State of an entity | the wall switch changes; the mood changes to "Hygge"; the door is open for 5 minutes |
| Number crosses a threshold | the temperature rises above 28; lux drops below 100 and stays there for 2 minutes |
| Time of day | at 23:00, on chosen weekdays; or at the time held by a date/time helper |
| Sunrise or sunset | 30 minutes before sunset |
| Room occupancy | the room becomes occupied or vacant (see below) |
| *More:* Button or remote | an event entity reports "single", "double", "hold" ... (see *Buttons and remotes*) |
| *More:* Device trigger | a trigger Home Assistant itself lists for a device (see *Buttons and remotes*) |
| *More:* Helper changes | a toggle, button, number, dropdown, text, date/time, counter or timer helper |
| *More:* Sun elevation | the sun drops below -4 degrees |
| *More:* Template | a template becomes true (optionally: and stays true for some time) |
| *More:* Event, MQTT message | entered by hand, see *Buttons and remotes* |
| *More:* Time pattern | every 15 minutes |
| *More:* Home Assistant starts or stops | |

State and number triggers and conditions can look at an **attribute** instead of the state ("Use an attribute").

**AND/OR** - optional conditions, all of which (AND), any of which (OR) or none of which (NOT) must hold at that
moment: an entity's state (is / is not, optionally for some time), a number above/below a value, a time range
and weekdays, and under *More*: before/after sunrise or sunset, a template, a condition Home Assistant lists for
a device, and *which trigger started the rule*. A *group* combines conditions the other way round, for example
"dark AND (mood is Hygge OR after 22:00) AND none of (guest mode, door open)". Groups nest up to three levels.

**THEN** - the actions, in order:

- a ready-made action of the controlled entities (turn on with brightness and colour, set a position, set a
  temperature, select an option, ...), **Wait** (a delay), or **Other action** (any Home Assistant action with
  its data as JSON; values may be templates such as `{{ level }}`);
- under *More*:

| Action | What it does |
|---|---|
| If ... then ... else | runs one list of actions or the other |
| Choose between branches | the first branch whose conditions hold runs; optionally an "otherwise" |
| Wait for a trigger | continues when one of the triggers happens |
| Wait until a condition holds | continues at once if it already holds; entity states, numbers and templates can be watched |
| Repeat | a number of times, while conditions hold, or until they hold (those two need a wait inside) |
| Set variables | values, also templates, that later actions of the rule use as `{{ name }}` |
| Send a notification | through a notify action or a notify entity; title and message may be templates |
| Run a script | start it and go on, or wait until it has finished; with variables |
| Activate a scene | |
| Device action | an action Home Assistant lists for the device a controlled entity belongs to |

Both waits take an optional **time limit**, and you choose what happens when it runs out: *continue with the
next action*, or *skip the actions after this wait*. (Home Assistant's own "stop at the timeout" would abort the
whole automation, including the device's other rules; Automation Builder generates the same result for the rule
alone.)

A rule is saved as soon as it is complete; the right-hand column shows its summary, what Automation Builder noticed
about it, which automation it becomes part of, and the generated YAML. Each rule can be disabled without deleting
it. Saving never changes Home Assistant: see *Deploying*. Nothing the editor offers needs hand-written YAML.

### Buttons and remotes

How a button reaches Home Assistant depends on the integration, so the editor offers what Home Assistant knows
and invents nothing:

- **Button or remote (event entity).** Many integrations show a button as an *event* entity (Zigbee2MQTT with
  its default settings, Matter, Philips Hue, newer ZHA devices and others). Choose the entity; the kinds of
  press it reports (single, double, long, release ... whatever the device has) are listed to tick. None ticked
  means any event. Becoming available again after a restart is not a press.
- **Device trigger (from Home Assistant).** Choose the device; the list shows exactly the triggers Home
  Assistant offers for it - for ZHA remotes typically "remote button short press / long press / double press"
  per button, for Zigbee2MQTT devices their actions (through MQTT discovery), for other integrations theirs.
  If the list is empty, the device has no device triggers.
- **Event (entered by hand).** For devices with neither: the event type and, optionally, data it must contain.
  A ZHA remote without device triggers sends `zha_event` with its `device_ieee` and a `command`
  (see Developer tools → Events in Home Assistant to find the values). Automation Builder cannot check that
  such an event exists; that is why this trigger is marked as entered by hand.
- **MQTT message (entered by hand).** A topic and, optionally, the payload - for Zigbee2MQTT without event
  entities: `zigbee2mqtt/<device name>/action` with the action as payload. Needs the MQTT integration.

Neither ZHA nor Zigbee2MQTT is required; a rule can use any of these, or none.

One rule can react to several presses and decide in its actions: *IF the remote reports "single" OR "double"
THEN choose: when started by "single" → toggle; when started by "double" → turn on at 100 %.*

### How rules are combined

All enabled rules of a device become one automation: every distinct trigger once, with a stable id, and one block
per rule that runs when the automation was started by one of that rule's triggers and the rule's conditions hold.
If two rules react to the same trigger, both run, in the order listed on the device page.

**Conflicts.** When two rules can run at the same moment and act on the same entity in ways that depend on the
order (one turns it on and the other off, two toggles, different settings), Automation Builder reports a conflict
and does not deploy until you either change the rules - typically by giving them conditions that exclude each
other - or press **Confirm this order**. It never picks a winner silently. A confirmation belongs to those two
rules in that order.

**Rules that wait.** Rules whose actions finish at once share the device's automation, which always handles
one trigger after the other (mode *queued*). A rule that *waits* - a delay, a wait for a trigger or a
condition, a repeat "while"/"until", a script it waits for - keeps a run alive, and whatever mode its automation
had, that would cancel, ignore, delay or interleave the device's other rules. So a waiting rule always gets
**its own automation**, and you choose what a new trigger does while it waits (Advanced): *start the wait over*
(the default), *ignore it*, *run again afterwards* or *run both at the same time*. The page says which rules
are separate and why. Automation Builder never chooses or changes a mode silently, and never squeezes rules into
one automation at the price of different behaviour.

Where a rule acts on an entity *after* a wait while another rule of the device controls the same entity, the
device page points that out: the later action still follows and has the last word.

Prefer a trigger with "stays so for ..." where you can ("motion is off for 5 minutes → turn off"): it blocks
nothing and is not lost when the trigger repeats. A restart of Home Assistant cancels a running wait: the
actions after it do not run. That is Home Assistant's behaviour for every automation.

## Occupancy (optional)

Any binary sensor that means "occupied" can be used, including Smart Occupancy Detection's
`binary_sensor.sod_<room>_occupied`. Nothing else in Automation Builder depends on occupancy.

When a rule has a **Room occupancy** trigger, its automation also gets *rechecks* - you do not build them:

- the rule runs at once when the room becomes occupied (or vacant);
- the automation looks again 2 seconds and 15 seconds later, and then every 5 minutes while the room stays in
  that state (Settings, or per rule under Advanced; the periodic check can be turned off).

A recheck does **not** replay the actions. It runs them only if, at that moment, all of this is true:

1. the occupancy sensor still shows that state. Not when it is unavailable or unknown, and not when it reports
   itself stale (attribute `fresh: false`, as Smart Occupancy Detection does when its App is away);
2. the rule's own conditions hold now;
3. a controlled entity is available and is not in the state the rule leaves it in;
4. that entity **has not changed since the occupancy change**. If it has, somebody or something switched it
   afterwards - possibly a person - and it is left alone;
5. the optional *manual override signal* (any toggle you choose) is off;
6. the occupancy change happened after the automation's first minutes (*start-up grace*, 5 minutes by default).
   After Home Assistant starts, every entity's "last changed" time is new, so point 4 cannot be judged: a lamp
   someone switched off before the restart must not come back on.

Consequences worth knowing:

- An "occupied → on" rule never turns anything off. For that, add an "vacant → off" rule.
- A rule containing *toggle*, a *Wait* or a hand-entered action gets no rechecks (the page says so): repeating
  such an action could do harm. It still runs when the occupancy changes.
- When a recheck finds a mismatch it must not correct, nothing is forced; the device page lists it under
  *Conflicts and notes*.
- Automation Builder cannot always tell a person from another automation or a device that reports late. It errs on
  the side of leaving things alone. Known limit: an occupancy sensor that becomes *unavailable* and returns as
  occupied looks like a new occupancy period.

The rechecks are triggers of the same automation (`for:` and a time pattern), not waiting sequences, so they block
no other rule; while no occupancy rule's state is current the periodic check ends at the automation's condition.

## Moods (optional)

A mood is an ordinary entity to Automation Builder. Use a dropdown helper (`input_select`) in a state trigger or
condition: *IF Mood changes to "Hygge" THEN turn the lamp on at 30 %, 2400 K*. Only the device the rule belongs to
takes part; nothing is added for other lights, and no moods are assumed to exist.

**Rules from a dropdown helper** (on a device's page) is a shortcut for exactly that: choose a dropdown helper,
pick what this device does for the values you care about, and one ordinary rule per value is created. The
values are whatever the helper has; no naming is expected, no rule is created for a value you leave at "No
rule", and nothing is created for any other device. The rules can be edited or deleted like all others.

## Blueprints

**Export as blueprint** on a device's page turns one of its generated automations into a native Home Assistant
automation blueprint: the same triggers, conditions and actions, with the values you choose replaced by inputs.

1. Give the blueprint a name and a description.
2. Tick what a user of the blueprint may choose. Offered are the controlled entities, the trigger entities, the
   entities conditions look at, numeric thresholds, times of day, durations and delays, brightness, colour
   temperature and the other parameters of the actions, the states a trigger or condition asks for, and the
   device of a device trigger. Everything you do not tick stays fixed, exactly as in your rules.
3. For each input you can change the name and the description; the default is the value your rule has now,
   and the selector fits the value (an entity picker limited to the domain, a number with its unit, a duration,
   a time, a colour temperature ...).
4. The right-hand column shows the validation results and the generated YAML. **Download blueprint** saves the
   file; **Install in Home Assistant** stores it as `blueprints/automation/automation_builder/<device>.yaml`,
   where it appears under Settings → Automations & scenes → Blueprints.

What to know:

- **Stable identifiers.** An entity is one input wherever it is used (`controlled_<name>` for a controlled
  entity, `<domain>_<name>` for others); a value is named after its rule and its place in it
  (`<rule id>_t1_for`, `<rule id>_a1_brightness_pct`). Renaming a rule or changing a value keeps them, so an
  automation made from an earlier export keeps working with a later one. Removing or reordering triggers,
  conditions or actions *inside* a rule changes the identifiers after that position; inputs that no longer
  exist are listed and left out.
- **Exactly the deployed automation.** With every input at its default, the blueprint is the automation the
  device deploys; the App checks that, and lets Home Assistant validate the result. Occupancy rechecks are part
  of it and follow the chosen entities.
- **Independent.** The blueprint contains only native Home Assistant elements; automations made from it run
  without Automation Builder. Exporting or installing never changes the managed device or its automation, and
  never turns it into a blueprint automation.
- **One automation per blueprint.** A device whose waiting rules have their own automations is exported one
  automation at a time (choose it at the top); disabled rules are not part of an automation and so not of a
  blueprint.
- **What stays fixed and is said so:** templates are kept as written (an entity named inside a template is not
  replaced by an input), a device trigger stays bound to its device unless the device is made an input, device
  conditions and device actions stay bound to theirs, a script that is waited for is called by its name.
- **Installing** uses Home Assistant's own blueprint command, restricted by the App to its own folder. An
  installed file is replaced only after you confirm; automations made from it are then reloaded by Home
  Assistant with the new version. Removing a blueprint is done in Home Assistant.

A visual blueprint designer (editing a blueprint's structure after the export) is not part of this version.

## Import

**Import** lists the automations in Home Assistant that Automation Builder does not manage. *Analyse* shows,
for one of them:

- its triggers, conditions and actions as the rule they would become, and the entities it controls;
- **what is not supported**, each with the place and the reason. Examples: an action on a whole area, device or
  label; `stop`, `parallel`, `for_each`; templates where an entity, a duration or an action name is expected;
  trigger types the editor does not have (zone, calendar, webhook ...); automation-level variables; a disabled
  step; an automation made from a blueprint; a state trigger that fires on every attribute change;
- what would be different in the copy (for example a condition between actions becomes "if ... then" around
  the actions after it; the mode of an automation without waits);
- overlaps with rules you already manage (the same trigger and the same entity).

A conversion is offered **only when everything can be represented faithfully**. Nothing is ever left out to
make an automation fit.

*Create the copy* makes a new device with one rule (or adds the rule to an existing device that controls the
same entities, but only if you choose that device). It is a copy:

- the original automation is not changed, not turned off and not deleted - Automation Builder has no means to
  do any of that;
- nothing is deployed by the import;
- **both would act** if you deployed the copy while the original is still turned on. The device page says so,
  and *Review and deploy* refuses until you turn the original off in Home Assistant, or explicitly choose
  *Allow both to run*. When you are satisfied with the copy, delete the original in Home Assistant yourself.

## Deploying

The device page shows the automation's status and a **Review and deploy** button.

1. Automation Builder compiles the rules, checks them for conflicts, asks Home Assistant to validate the triggers,
   conditions and actions, and reads the automation that is in Home Assistant now.
2. You see what will be created, replaced or removed, the differences line by line, and the complete YAML.
3. **Deploy to Home Assistant** writes it, reloads that one automation and waits until Home Assistant runs exactly
   this configuration. If any step fails, the previous automation is put back and the error is shown.

**Changes made outside the App.** Automation Builder remembers exactly what it deployed. If the automation in
Home Assistant differs (someone edited or deleted it there), the device shows it, the review shows the
difference, and deploying replaces it only after you tick an explicit confirmation.

**Other automations.** Automations you made yourself are never changed. If one of them refers to an entity that a
device here controls, the device page mentions it, as a hint that the two may work against each other.

**Earlier versions.** *Discard changes since the last deployment* and *Go back to the deployment before* restore
the device's rules (the automation changes when you deploy). Every change of the configuration is also kept as a
backup in Settings.

**Removing.** Disabling a device, or all its rules, removes its automation at the next deployment and keeps the
rules. Deleting a device deletes its automation from Home Assistant too.

## Diagnostics

Per device: the automation's id and entity, the number of rules and which are enabled, the deployment status
(in sync, changes pending, changed or deleted outside, turned off in Home Assistant), the last deployment and
its result, conflicts, entities that are missing or unavailable, the differences between the rules and what is
deployed (in the review), and the automation's last runs with links to Home Assistant's own traces.

**Per rule** (open *Execution* under a rule on the device page), read from Home Assistant's traces of the
managed automations:

| Shown | Meaning |
|---|---|
| Last triggered | the automation was started by one of this rule's triggers |
| Last blocked by conditions | it was triggered, but a condition did not hold - which one is named |
| Last executed | the rule's actions started (also by an occupancy recheck, marked as such) |
| Last outcome | ran, still running (a wait), stopped early, or failed with Home Assistant's error |
| Action verification | see below |
| Entities | every entity the rule uses, and whether it exists and is available |
| Deployed / changed since | whether Home Assistant runs the rule as it is now |

Each entry links to the trace in Home Assistant. Home Assistant keeps only the last few traces and forgets them
at a restart; Automation Builder stores what it has seen, so the entries survive restarts. A run that happened
while the App was stopped is picked up afterwards as long as Home Assistant still has its trace.

**These are different things, and the page keeps them apart:** a triggered rule did not necessarily execute; an
executed action is a call Home Assistant *accepted*; and an entity's reported state is what its integration
says, not proof of what a physical device did.

**Action verification.** Where an action's result can be read from the entity - on/off, open/closed,
locked/unlocked, a selected option or mode, brightness, colour temperature, cover position, target temperature,
fan speed, humidity, volume, a number - Automation Builder looks at the entity's reported state a few seconds
after the rule ran (longer for a transition) and shows:

- *Reported state matches*;
- *Reported state differs*: Home Assistant accepted the action, the entity reports something else, and nothing
  else has touched the entity since;
- *Changed by something else since*: another automation or a person changed the entity after the action, so
  the action is not judged;
- *Cannot be verified*: toggles, presses, scripts, notifications, hand-entered actions and everything whose
  result is not in the entity's state.

Nothing is corrected from here, and the App issues no commands of its own: a mismatch is information.
Corrections only come from rules you configured (the occupancy rechecks, with their safeguards).

Settings → *Download diagnostics* collects all of this for every device, with the App's log.

## Backup and restore

The rules and settings are one versioned document in the App's data folder, included in Home Assistant backups
of the App. A copy is written before every change (the last 30 are kept; Settings → Automatic backups → Restore).
*Download configuration* exports it; *Import* shows what would change before it replaces anything. A restore or
import changes the rules only: deploy each device afterwards. Automations of devices that are no longer in the
configuration are listed on the Overview, where you can remove them.

## How deployment works, and what the App may do

Automation Builder talks to Home Assistant through the Supervisor's Core proxy (`homeassistant_api`), with a
fixed allowlist enforced in the App before anything is sent (`backend/ab/ha.py`):

| Channel | Used for |
|---|---|
| Websocket, reads | states, the area, device and entity registries, the user list (administrator check) |
| Websocket `validate_config` | Home Assistant's own validation of triggers, conditions and actions (changes nothing) |
| Websocket `automation/config`, `search/related` | what an automation entity runs (also for the import), which automations refer to an entity |
| Websocket `trace/list`, `trace/get` | the runs of the App's **own** automations (ids that start with `automation_builder_`), for the rule diagnostics |
| Websocket `device_automation/{trigger,condition,action}/list`, `get_services` | what Home Assistant offers for a device; the notify actions |
| Websocket `blueprint/list`, `blueprint/save` | install an exported blueprint, only as `automation_builder/<name>.yaml` among the automation blueprints, only when you press Install |
| HTTP `GET/POST/DELETE /api/config/automation/config/<id>` | read, save and delete **one automation**, only for ids that start with `automation_builder_` |

The App cannot call a service, set a state, fire an event, change a registry or turn an automation on or off,
and it has no access to Home Assistant's configuration folder (no `map`). It never controls a device itself:
the deployed automations do. The only things it writes are its own automations and, on request, its own
blueprint files.

**Blueprint installation.** Home Assistant has no documented public API for storing a blueprint file either.
`blueprint/save` is the websocket command its own "Import blueprint" dialog uses; it validates the blueprint and
writes it below `blueprints/<domain>/`. The alternative, write access to the configuration folder (`map`),
would give the App far more than it needs. The automation configuration endpoint is not used for blueprints.
If the command is not available, installing fails with Home Assistant's message and *Download blueprint* still
works: copy the file to `config/blueprints/automation/` yourself.

**Interrupted deployments.** Before the first request of a deployment the App notes what it is about to write.
If the App or Home Assistant stops midway, the next start compares that note with what Home Assistant has:
an automation that arrived is recorded as deployed, one that did not is still "changes not deployed yet", and
anything else is shown as changed outside, to be reviewed as always. The recovery itself writes nothing to
Home Assistant.

**Why this mechanism.** Home Assistant has no documented public API for saving an automation. The endpoint above
is the one its own automation editor has used for years: it validates the automation, stores it in
`automations.yaml` and reloads that single automation. The alternatives were worse: writing `automations.yaml`
from the App needs write access to the whole configuration folder and a full reload, and a companion integration
would add a second thing to install only to do the same writes.

**Because it is not a documented API**, nothing else depends on it: all use is in one class
(`AutomationConfigApi`), and every deployment is verified independently - the automation entity must exist and
run exactly the deployed configuration (`automation/config`). If a future Home Assistant changes the endpoint,
deployments fail with a clear message; rules, records and the automations already deployed are unaffected.

**Limitations.**

- `automations.yaml` must be included in `configuration.yaml` (the default). Otherwise Home Assistant saves the
  automation without loading it; the App reports that and removes it again.
- Saving through this endpoint makes Home Assistant rewrite `automations.yaml`, as its own editor does: comments
  in that file are lost. Automations kept in other YAML files are not affected.
- The App acts with administrator rights (as every App with `homeassistant_api`); the restriction to its own
  automations is enforced by the App.
- Not part of the rule model: `stop`, `parallel`, `repeat for_each`, events fired by a rule, response variables,
  `continue_on_error`, and trigger types such as zone, calendar, tag, webhook and conversation. A hand-entered
  action is passed to Home Assistant exactly as written, never simplified.
- An occupancy rule with branches, waits or other advanced actions gets no automatic rechecks (a recheck would
  run the whole sequence again); the editor says so.
- Device triggers, conditions and actions are shown with a label built from Home Assistant's identifiers
  ("remote button short press turn on"), not with the integration's translated text.
- Diagnostics depend on Home Assistant's traces: a run whose trace was already dropped (more than the last five
  runs of an automation while the App was stopped, or a Home Assistant restart in between) is not seen.
  Verification needs the App to be running within three minutes of the run.

## Testing

Developers: `cd automation_builder && pytest` (rule model, compiler, conflicts, blueprint export, import,
diagnostics, workflow against a fake Home Assistant, web API, packaging); `python scripts/run_tests.py
tests/automation_builder` from the repository root loads the compiler's output - and exported blueprints, and
imported copies next to their originals - into a real Home Assistant (its automation parser and engine) and
drives states and time. `tests/e2e/` runs the real image against a disposable Home Assistant (`stack.sh`,
`prepare.py`, `ha/` with the test configuration, `e2e_checks.py`, `e2e_02_checks.py`, `browser_checks.py`,
`browser_02_checks.py`) and against a Home Assistant OS test system (`haos.py`, `haos_checks.py`,
`sod_checks.py` with the real Smart Occupancy Detection). Details, the Home Assistant OS procedure and results:
`docs/AB-0.2-automation-builder.md` (and `docs/AB-0.1-automation-builder.md` for the first version).
