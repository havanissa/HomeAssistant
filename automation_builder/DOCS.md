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
- **Devices**: add and manage devices.
- **Rule Editor**: every rule, per device; create and edit rules.
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

A rule has three parts.

**IF** - one or more triggers; the rule starts when any of them happens:

| Trigger | Example |
|---|---|
| State of an entity | the wall switch changes; the mood changes to "Hygge"; the door is open for 5 minutes |
| Number crosses a threshold | the temperature rises above 28; lux drops below 100 and stays there for 2 minutes |
| Time of day | at 23:00, on chosen weekdays |
| Sunrise or sunset | 30 minutes before sunset |
| Room occupancy | the room becomes occupied or vacant (see below) |

**AND/OR** - optional conditions, all of which (AND) or any of which (OR) must hold at that moment: an entity's
state (is / is not, optionally for some time), a number above/below a value, a time range and weekdays. A *group*
combines conditions the other way round, for example "dark AND (mood is Hygge OR after 22:00)".

**THEN** - the actions, in order: a ready-made action of the controlled entities (turn on with brightness and
colour, set a position, set a temperature, select an option, ...), **Wait** (a delay), or **Other action** (any
Home Assistant action with its data as JSON).

A rule is saved as soon as it is complete; the right-hand column shows its summary, what Automation Builder noticed
about it, which automation it becomes part of, and the generated YAML. Each rule can be disabled without deleting
it. Saving never changes Home Assistant: see *Deploying*.

### How rules are combined

All enabled rules of a device become one automation: every distinct trigger once, with a stable id, and one block
per rule that runs when the automation was started by one of that rule's triggers and the rule's conditions hold.
If two rules react to the same trigger, both run, in the order listed on the device page.

**Conflicts.** When two rules can run at the same moment and act on the same entity in ways that depend on the
order (one turns it on and the other off, two toggles, different settings), Automation Builder reports a conflict
and does not deploy until you either change the rules - typically by giving them conditions that exclude each
other - or press **Confirm this order**. It never picks a winner silently. A confirmation belongs to those two
rules in that order.

**Rules that wait.** A rule with a *Wait* keeps its automation busy for the length of the wait. You choose what a
new trigger does meanwhile (Advanced): *start the wait over* (the default), *ignore it*, *run again afterwards* or
*run both at the same time*. The first three would cancel, ignore or delay the device's other rules, so such a
rule gets **its own automation**, and the page says why. Prefer a trigger with "stays so for ..." where you can
("motion is off for 5 minutes → turn off"): it blocks nothing and is not lost when the trigger repeats.

A restart of Home Assistant cancels a running wait: the actions after it do not run. That is Home Assistant's
behaviour for every automation.

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
Settings → *Download diagnostics* collects the same for all devices, with the App's log.

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
| Websocket `automation/config`, `trace/list`, `search/related` | what an automation entity runs, its last runs, which automations refer to an entity |
| HTTP `GET/POST/DELETE /api/config/automation/config/<id>` | read, save and delete **one automation**, only for ids that start with `automation_builder_` |

The App cannot call a service, set a state, fire an event or change a registry, and it has no access to Home
Assistant's configuration folder (no `map`). It never controls a device itself: the deployed automations do.

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
- Native *device actions* (the device-specific action list of the Home Assistant editor) are not offered: rules
  use entity actions, which work for every integration. Templates, `choose`, `repeat` and `wait_for_trigger` are
  not part of the rule model; a hand-entered action is passed to Home Assistant exactly as written, never
  simplified. Existing automations cannot be imported.
- Automation Builder has no runtime of its own: it does not watch rules run or verify actions afterwards (the
  occupancy rechecks are part of the generated automation).

## Blueprints (later)

Automation Builder is meant to become a visual blueprint editor. Every value a blueprint would expose - the
controlled entities, trigger and condition entities, thresholds, durations, action settings - is a separate,
named field of the rule model (`backend/ab/blueprint.py` lists them for a device). Turning a device's rules into
a blueprint replaces those fields with inputs in the same compiler output. Phase 0.1 does not write blueprints.

## Testing

Developers: `cd automation_builder && pytest` (rule model, compiler, conflicts, workflow against a fake Home
Assistant, web API, packaging); `python scripts/run_tests.py tests/automation_builder` from the repository root
loads the compiler's output into a real Home Assistant (its automation parser and engine) and drives states and
time; `tests/e2e/` runs the real image against a disposable Home Assistant (`stack.sh`, `prepare.py`,
`e2e_checks.py`, `browser_checks.py`). Details and results: `docs/AB-0.1-automation-builder.md`.
