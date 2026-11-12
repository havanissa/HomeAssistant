# Smart Occupancy & Lighting

A Home Assistant custom integration that will eventually provide intelligent occupancy detection, predictive room occupancy, mood-based lighting and lighting supervision.

**This is Phase 0.3.5: mood-based lighting with simple dashboard cards (Smart Lighting Profiles, Smart Room Lighting, Room Status), on top of the Phase 0.2 occupancy engine (see [CHANGELOG.md](CHANGELOG.md)).** Phase 0.3.5 adds **native wall switches and remotes** (Zigbee2MQTT, MQTT device triggers and event entities): assign a switch to a room, learn its buttons, give each button an action, and let presses count as manual changes that automatic lighting respects (see [Wall switches and remotes](#wall-switches-and-remotes-phase-035)). Phase 0.3.3 switches a room's mood lighting on when someone arrives **and it is dark**: indoor light sensors (protected against being lit by the room's own lamps), an optional outdoor sensor and the sun decide whether artificial light is needed. Phase 0.3.2 lets moods, light types and single lights follow your **existing global brightness and colour temperature adjustment**, and adds the **Manual** pause. The interface stays simple by default; everything else is under *Advanced*.

Every room starts in **Observe** mode. In Observe the integration plans and previews mood lighting, but sends no light command. Mood lighting runs only in rooms where an administrator explicitly enabled it in the Smart Room Lighting card (the pilot is Kamar Norður). The integration never:

- changes, disables or triggers existing automations, scripts or scenes (a wall switch button you explicitly assigned to a scene or script activates it)
- changes the mood selectors or any other helper (they are only *read*), except when you press a wall switch button that you assigned to *Select mood (all rooms)*: that selects the option on your Manual Mood selector. Main Mood is never written
- takes over a wall switch, changes Zigbee bindings, or publishes MQTT messages
- controls lights in a room that was not explicitly activated
- switches lights off because a room became vacant (that is Phase 0.4)
- switches lights off, or changes their brightness or colour, because of daylight or a lux reading
- switches a light on because the global brightness or colour temperature changed
- sends any automatic lighting command while Main Mood is **Manual**
- calculates its own adaptive lighting: it only reads the values your existing system publishes
- polls, uses the internet, or writes to the Recorder database directly

## Requirements

- Home Assistant 2026.10 or newer. Tested against **2026.11.1** (with its frontend build 20261028.5) and 2026.10.4.
- No Python dependencies

## Installation

### HACS (custom repository)

1. In HACS: ⋮ → **Custom repositories** → add `https://github.com/havanissa/HomeAssistant`, type **Integration**.
2. Install **Smart Occupancy & Lighting** and restart Home Assistant.

Updating from 0.1.x or 0.2.x keeps your configuration. See [Upgrading](#upgrading).

### Manual

1. Copy `custom_components/smart_occupancy/` to `/config/custom_components/smart_occupancy/`.
2. Restart Home Assistant.

## Configuration

**Settings → Devices & services → Add integration → Smart Occupancy & Lighting.** Only one instance can be added; everything else is done in its options.

### Initial setup

1. **Mood system.** Select Auto Mood, Manual Mood and Main Mood. `input_select.auto_mood`, `input_select.mood` and `input_select.main_mood` are pre-filled if they exist. Set the Manual Mood option that means "Auto" (default `Auto`). Set the mismatch grace period (default 30 s) to cover the time your automations need to copy the mood.
2. **Light categories.** Map each of the seven initial categories to a Home Assistant label. An existing label with the same name as the category is pre-selected. Tick *Create labels for categories left empty* to create the missing labels. This only creates the labels; you apply them to lights yourself.
3. **Rooms.** Select the areas to enable. An area named **Kamar Norður** is pre-selected as the pilot room.
4. **Per room: assign signals and lights.** Fields are pre-filled from the devices discovered in the area, using device class, never names:
   - `motion` → PIR
   - `occupancy` and `presence` → radar
   - `door` → door contact

   Window and opening contacts are listed but not pre-assigned.

   **Check these suggestions.** Many Zigbee PIR and bed sensors report `occupancy` just like radars. Move them to the correct field.

   You can also select managed lights or groups, and an optional *existing occupancy helper*. That helper is only read, to compare it with this integration's occupancy.
5. **Per room: signal roles.** Choose *Trigger occupancy*, *Maintain occupancy*, *Confirm vacancy* or *Ignore* for each signal (see [Occupancy engine](#occupancy-engine)). The defaults are:
   - PIR and door: trigger
   - radar, PC and bed: maintain
   - other: ignore

   You can also mark signals as *inverted*, for sensors that mean activity when they're off.

### Options (⚙ Configure)

| Menu | What it does |
|---|---|
| Mood helpers and operating mode | Change the mood entities, the Auto option and the grace period. Supervise and Full Control are listed but rejected in this phase. |
| Occupancy timing | Global timing defaults for every room (see [Timing](#timing)). |
| Rooms | Pick any area (✓ marks enabled rooms). Then enable or disable it, change its assignments, roles and inverted signals, and finally its *Occupancy* settings: per-room timing overrides (empty = global) and adjacent rooms. *Remove this room's configuration* deletes the room's stored settings. Disabling keeps the configuration. |
| Light categories | Add, rename or re-label, and remove categories, or *Create missing labels*. A category's internal key stays stable when you rename it. Removing a category also removes its profile entries. |
| Mood profiles | A form-based fallback for the dashboard editors: edit one profile entry at the **Global**, **Room override** or **Individual light override** level. It is saved in the lighting store at once and does not reload the integration. |

Each options run makes one change and saves it. The integration then reloads with the new configuration.

## Mood lighting (Phase 0.3)

### Moods: the three selectors

The existing mood system stays in charge, and nothing in it is changed:

| Selector | Role |
|---|---|
| `input_select.auto_mood` | Set by your automations (time, sunset, sleep, weather …). It keeps updating while a manual mood is selected. |
| `input_select.mood` | The manual choice, or `Auto` |
| `input_select.main_mood` | The effective mood: Manual Mood, or Auto Mood when Manual is `Auto` (set by your automation) |

Lighting follows **Main Mood**. A room can have its own **room mood** instead (Smart Room Lighting card → Overrides → *Room mood*).

**Moods are discovered, not coded.** Every option of the three selectors is a mood, and so is every mood that has a profile. Add *Hygge*, *Work* or anything else to a selector and it appears in the editors. Nothing about Hygge is special: there is no Hygge function or button, it is a mood like any other. If a mood is offered by Auto or Manual Mood but not by Main Mood, the editor says so and tells you to add it to the Main Mood helper. Until you do, it can only be a room mood or a parent/fallback mood.

**Moods Main Mood cannot represent (`Onki`).** `Onki` is in Auto Mood but not in Main Mood. It is *not* treated as `Auto`. While Auto (or Manual) Mood is set to such a mood, Main Mood keeps its previous value, and a *Mood cannot be represented by Main Mood* warning names the missing option. What lighting does then is configurable per mood (Smart Lighting → select the mood → Advanced):

- *Keep following Main Mood* (default; the existing behaviour)
- *No mood: change nothing*
- *Use another mood*

The selectors themselves are never rewritten.

### Light types: Home Assistant labels

Lights are configured by **type**, not one by one. Each light type is bound to a Home Assistant label; a light or group belongs to it when the label is on the entity or on its device. Entity names are never used.

A light can carry **several labels**. There are three kinds, and the cards combine them for you:

| Kind | Examples | Meaning |
|---|---|---|
| **Light type** | Spottar, Loft ljós, LED strips, Downlights, Lamps | What the light is |
| **Function** | Uppljós, Decorative, Task, Path, Outdoor | What it is used for |
| **RGB tag** | Rgb | Lights that may get colours. RGB is not a type: not every LED strip is RGB, and not every RGB light is a strip. |

Without an RGB tag configured, "RGB" means "supports colour".

A light gets the settings of **all** its labels, merged field by field from the least to the most specific: types, then functions, then the RGB tag, then "type + RGB", then "function + RGB". For example, a kitchen light labelled *Spottar*, *Loft ljós* and *Rgb*:
- With *Spottar: Off* and *Loft ljós + RGB: purple 70 %*, it glows purple.
- A plain spot next to it stays off.

The default categories of a new installation include **Uppljós** (function). Categories made before 0.3.1 get their kind from their name: *Rgb* becomes the RGB tag, and *Uppljós*, *Decorative*, *Task*, *Path* and *Outdoor* become functions. Change kinds, labels and types under **Smart Lighting → Advanced → Light types** (the card also suggests existing labels such as *Uppljós* or *Rgb* that aren't used yet), or in *Configure → Light categories*. Changing light types reloads the integration; nothing is applied on reload.

**Discovery.** A light or group in a room's area that carries a category label is managed by that room automatically. Lights configured explicitly in *Configure → Rooms* are managed too. When a light gets the label, moves to another area, or is renamed, the room follows on the next registry update. A configured light that has moved to another area is reported (*Managed light is in another area*).

**Exclusion.** A light or group is never controlled when:
- it is unticked (*managed*) in the room card, or
- it carries the **exclusion label** chosen under Smart Lighting → Advanced.

**Groups.** When a group and its lights are both managed in a room, the room card asks one question: **As a group** (the member lights are no longer managed), **Individually** (the group is no longer managed), or **Ignore for now** (keeps things as they are; the warning becomes a note under Advanced). Until you answer, a Home Assistant light group is commanded instead of its members, so no light gets duplicate commands. Zigbee2MQTT groups (device model *Group*) and Hue groups don't expose their members: lights of the same type in the room are treated as possible members.

### Profiles and inheritance

A profile entry sets, for one mood:

| Field | Values |
|---|---|
| Action | **On**, **Off**, **Inherit**, or **Ignore** (send nothing) |
| Brightness | 1–100 % |
| Colour | colour temperature (K), *or* RGB, *or* hue/saturation |
| Effect | any effect the light offers |
| Transition | 0–300 s |
| Ambient | **Yes**: follow the mood even while the room is vacant (e.g. decorative or outdoor lights) |

Three levels, merged **field by field**; the most specific configured value wins:

1. **Global** – mood × light type (Smart Lighting card)
2. **Room override** – room × mood × light type (Smart Room Lighting → Overrides)
3. **Individual light override** – room × mood × light or group (same tab)

Example: global *Hygge / Lamps* = 70 %, 2700 K; Kamar Norður *Hygge / Lamps* = 50 %; the desk lamp = 80 %. The desk lamp gets 80 % and 2700 K; the other lamps in Kamar Norður get 50 % and 2700 K.

**Mood inheritance.** A mood's global profile can inherit from a **parent mood** (e.g. *Hygge* → *Kvøld*). After the parent chain comes the **fallback mood** (e.g. *Default*). Inheritance loops are refused when you save, and cut off if they appear anyway. The chain applies at the global level; room and light overrides belong to one mood.

Rules:
- *Inherit*, or an empty field, takes the less specific value. If no level sets an action, the result is *Ignore*.
- Colour temperature, RGB and HS replace each other.
- *Off* drops brightness, colour and effect.

The editors show for every field whether it is set at this level or inherited, and from where (for example *60 % (inherited from Kvøld)*).

**Light capabilities.** Settings are adapted to each light, never failing the whole profile:
- Brightness on an on/off light, colour on a white-only light, or an effect the light doesn't offer is **omitted** and reported (*Unsupported light settings*, warning).
- A colour temperature outside the light's range is clamped. On a colour light without white channels Home Assistant approximates it. A transition the light doesn't support is skipped. These are noted as *Light settings adapted* (info).

### When profiles are applied

One shared executor for every mood. In an activated room it applies the room's effective mood when:
- the effective mood changes (Main Mood, or the room mood)
- occupancy is **newly confirmed** (a new visit; Phase 0.2 engine, no second occupancy engine)
- the room is activated
- a profile entry changes; only the lights whose resolved setting changed get a command
- a light that was offline during an application comes back

It sends nothing when:
- the light already is in the requested state, or the same command was sent and nobody changed the light since
- the room is **vacant**: applied when occupancy is next confirmed. *Ambient* settings are the exception.
- occupancy is **uncertain**
- the light should switch **off** but occupancy is not confirmed (no shutoffs on uncertain evidence)
- a **manual override**, an unacknowledged **automation conflict**, an exclusion, or an *Ignore* applies
- Home Assistant is starting or restarting

Commands with identical data are batched into one `light.turn_on`/`turn_off` call. The reason for every decision is recorded: card, `sensor.smart_occupancy_<room>_mood_lighting`, debug log.

### Dashboard cards

The integration serves the cards itself: there is no resource to add. After updating, reload the browser once. Existing dashboards keep working (the card types are unchanged). **Edit dashboard → Add card**:

**Smart Lighting Profiles** (`custom:smart-occupancy-mood-editor-card`). Pick a mood (Dagur, Hygge, Arbeiði, Nátt, Veistla …) and see one line per light type, with a colour dot:

```
Smart Lighting                      Now: Hygge
[Dagur] [Hygge] [Arbeiði] [Nátt] [Veistla]
●  Spottar          Off
●  Loft ljós        40 % · 2700 K
●  LED strips       25 %
●  Uppljós + RGB    Amber · 30 %
Not set — tap to set: [Task lighting]
[Preview] [Advanced]
```

- Only types with a setting in this mood are rows. Types with lights but no setting are one line of chips. Types without any light are hidden ("Show unused light types").
- **Edit** (tap a row) opens one focused editor: *On / Off / Leave unchanged*, brightness, white (Warm / Soft / Neutral / Cool and a slider), colour swatches and effects. Only the controls the type's lights support are shown: a brightness-only LED strip shows just brightness; colours appear for RGB targets. **Only the RGB-tagged lights of this type** turns the edit into "type + RGB". Veistla: pick the mood, tap *Uppljós*, tick *Only RGB*, tap a colour. Done.
- Changes save immediately. *Advanced* inside the editor has transition, ambient, the exact values and where each comes from, and per-field reset.
- **Preview** shows what each room would do; a preview never changes a light.
- **Advanced** (card): mood inheritance (parent and fallback mood), handling of moods Main Mood cannot represent, light types (kind, label, add/remove, suggestions), exclusion label, manual-override duration, the **outdoor light sensor (all rooms)**, and the full validation list.

**Smart Room Lighting** (`custom:smart-occupancy-room-lighting-card`, pick the room in the visual editor). Six tabs (**Overview · Lights · Automatic · Overrides · Preview · Advanced**), the only navigation; only **Overview** is open by default:

- **Overview**: mood (follows global or room mood), automatic lighting mode (Observing / Automatic / Paused / Blocked), occupancy and lighting health, managed lights, last applied, the daylight decision in one line, then one line per issue: a group question with its three buttons, "2 lighting conflicts · Review", short health lines. No settings forms.
- **Lights**: one line per light type ("Spottar · 6 lights · follows global · Off"); tap to see its lights, or tick *Show every light*. *Managed* toggles exclusion.
- **Automatic**: the room's **operating mode** first (see [Operating modes](#operating-modes-036)), then automatic switch-on when occupied and dark (see [Daylight](#daylight-aware-automatic-lighting-phase-033)), with the details collapsed under *Advanced daylight settings*.
- **Overrides**: room mood; per type *follows global* or *overridden* (tap to override, the same editor with *Follow global*); single-light overrides.
- **Preview**: what this room would do in a mood.
- **Advanced**: activation with the full review (Observing / Automatic), automations that also control the lights (allow both or leave them alone), group decisions, manual overrides, wall-switch entities, discovery, last decision and the full diagnostics.

**Room Status** (`custom:smart-occupancy-room-card`): occupancy state, evidence quality, active sensors, three health chips (Occupancy / Lighting / Configuration), one short line per warning (*Details* for the full explanations), and the existing-helper comparison in one line.

```yaml
type: custom:smart-occupancy-mood-editor-card
---
type: custom:smart-occupancy-room-lighting-card
area: kamar_nordur        # area id of an enabled room
---
type: custom:smart-occupancy-room-card
area: kamar_nordur
```

**How the cards behave:**
- **Storage.** The integration owns the configuration (`.storage/smart_occupancy.lighting`), so it works with no dashboard open.
- **Network.** No polling: one subscription, and a refetch only when something changes.
- **Theme and layout.** Light and dark themes, phone to wide dashboards.
- **Permissions.** Only administrators can change settings.

### Operating modes (0.3.6)

Both modes are shown and changed on the main screens, without opening a configuration page. The status always shows what the integration really does, and every change is saved by the integration (it survives reloads and restarts, and other open dashboards update).

| Where | Modes | Change it |
|---|---|---|
| **Automatic → Automatic lighting** (room) | **Observing**: monitors events and calculates decisions, controls no light · **Automatic**: applies moods and switches on as configured. Also shown: *Automatic · paused* (Main Mood is `Manual`) and *Automatic · blocked* (every managed light is held by an unacknowledged automation, or no light is managed), with the reason. | **double-click the status** (tooltip: *Double-click to switch between Observing and Automatic.*), or the **Observing / Automatic** selector |
| **Switches → each switch card** | **Observing**: presses are registered and logged, nothing is executed · **Active**: the assigned button actions are executed | **double-click the status**, or the **Observing / Active** selector |

A single click on a status never changes anything. Double-tap works the same on phones; keyboard users use the selector buttons. Only administrators can change modes.

**The two modes are independent.** A switch set to **Active** executes its actions also while the room's automatic lighting is **Observing** (and while Main Mood is `Manual`). A switch in Observing never sends anything. The one thing that blocks an Active switch is a room that is not enabled in the integration's options: its card then shows **Active · blocked** with that reason, never plain *Active*. A switch card also warns when presses cannot arrive (no input) or no button has an action yet.

Setting a switch to Active is refused while Zigbee2MQTT reports a direct binding to lights, and asks you to acknowledge enabled automations that also react to the switch (right on the card), so a press is never executed twice. Changing the mode never changes the switch's buttons or actions.

Each press ends with one outcome, shown on the card (*Result*) and in *Advanced switch settings → Recent presses*: **Observed** (Observing), **Not assigned** (the button has no action), **Blocked** (Active, but the room is not enabled, too many presses queued, or the switch was set to Observing before the press ran), **Executed** or **Failed** (with Home Assistant's error, or *No light to control (2 lights offline)*). Executed, failed and blocked presses are logged at info level (`switch_press … outcome=…`); observed ones only at debug level.

### Activation (pilot: Kamar Norður)

1. Open the Smart Room Lighting card for Kamar Norður → **Advanced**. The review shows:
   - which lights and groups will be controlled
   - which are excluded or controlled through a group
   - which groups hide their members
   - which **existing automations appear to control the same lights**, enabled or disabled, and how (directly, through a group, its device, area, floor, label, a scene or a script)
   - what would happen right now
2. For each enabled automation, decide:
   - change or disable it yourself, or
   - exclude the light, or
   - tick it (*acknowledged*: both may control the light).

   **Lights affected by an enabled, unacknowledged automation are left alone** while mood lighting is active.
3. **Switch Kamar Norður to Automatic → Confirm** (in the same tab), or use the mode selector in **Automatic → Automatic lighting** (see [Operating modes](#operating-modes-036)). Only administrators can do this. The current mood is applied if the room is occupied.

**Return to Observing** stops control immediately, and lights are left as they are. Other rooms stay in Observe until you activate them the same way. Existing automations are never disabled or modified.

### Manual overrides

A light changed by a person is respected for a while (default 60 min; per room or globally; 0 = until the room is vacant). Overrides are cleared when the room becomes vacant, or from the card. How a change is classified:

| Change | Treated as |
|---|---|
| From the app / UI / voice (context has a user) | **manual** |
| From an automation triggered by a configured **wall-switch entity**, or within 3 s after it changed | **manual** |
| From another automation or script | not manual (recorded as an external change) |
| Our own command, including MQTT devices reporting back | ours |
| A press of an assigned wall switch (0.3.5), or a light change within 3 s after it | **manual** (also for directly bound switches in Observing) |
| No context at all (e.g. a directly bound Zigbee switch) | **not** manual by default; configurable |

Opening a dashboard never applies anything.

### Diagnostics

`sensor.smart_occupancy_<room>_mood_lighting` has the state `observe`, `active`, `blocked` or `paused` (Main Mood is Manual). Its attributes (not recorded) are:
- the effective global mood and room mood, and whether the room follows the global mood
- control mode and reason
- managed, discovered and excluded lights, and lights controlled through a group
- offline lights and unsupported settings
- automation conflicts, with which ones block
- active manual overrides
- the last applied profile and time, the last trigger, result and reason, and errors
- `manual_mode`, `global_adjustment` (connected / unavailable / not_configured), the last global-adjustment update, and lights the existing adjustment system also controls (`adaptive_conflicts`)

The room cards show the same information as three health rows:
- **Occupancy health** is affected by sensors only.
- **Lighting health** covers offline lights, conflicts, unsupported settings and failed commands.
- **Configuration health** covers the configuration.

An unplugged light warns under *Lighting* without making occupancy look degraded.

## Global light adjustment (Phase 0.3.2)

You probably already have something that calculates a brightness and a colour temperature for the time of day (an Adaptive Lighting switch, template sensors, `input_number` helpers set by automations, …). Smart Occupancy **does not calculate these values**. It reads them, and each mood, light type or light decides how to use them.

### Connect the sources

In **Smart Lighting → Global light adjustment** choose:

- **Brightness source**: an entity with the target brightness, for example a `sensor`, `number` or `input_number` in % or 0–255. A `switch` with a `brightness_pct` attribute (Adaptive Lighting) or a light also work.
- **Temperature source**: an entity with the target colour temperature in K or mired. A `switch` with `color_temp_kelvin` / `color_temp_mired` attributes also works.

Each source is optional and independent. The section shows the live values, for example *62 % · 3200 K · Connected*. Likely entities are suggested; your own measurement sensors (battery, humidity, …) are not. Under **Advanced** you can choose:
- the attribute to read
- the unit (automatic, % or 0–255; automatic, K or mired)
- the update settings (see below)

The values come from state-change events, not polling. If a source is unavailable, unknown or not a number, its value is **never taken as 0**. The last good value is used (status *Unavailable (last value used)*), and it survives restarts. Without any earlier value, that property is left unchanged and a *Global source unavailable* warning is shown.

### Per mood: Follow global · Global + offset · Fixed · Ignore

In the focused editor (one mood, one light type), **Brightness** and **Temperature** each have four choices. The two are completely independent.

| Choice | Brightness | Colour temperature |
|---|---|---|
| Follow global | the global brightness | the global temperature |
| Global + offset | global ± percentage points (slider) | global ± K (slider) |
| Fixed | the mood's own value (slider) | the mood's own value (white presets / slider) |
| Ignore | left as it is | left as it is |

Inheritance (reset a value so it comes from the parent mood, the global profile or the room) is under the editor's *Advanced*. Results are clamped:
- brightness to 1–100 %, so a global value never switches a light off
- colour temperature to 1000–10000 K, and then to the light's own range

Examples:
- **Dagur**: follow both.
- **Kvøld**: global −20 points and −300 K. This gives a continuous evening gradient without separate *Morning/Evening* moods.
- **Hygge**: fixed warm.
- **Arbeiði**: fixed 100 % with the global temperature.
- **Veistla**: an effect or RGB colour with the global brightness.

**Colour and effects win over the global temperature.** While an RGB/HS colour or an effect sets a light's colour, the global white temperature is not sent. Colour and temperature are never sent together. An RGB light can still follow the global brightness.

**Capabilities come from the light's `supported_color_modes`:**
- A brightness-only LED strip follows the brightness only.
- A colour-only light without a white mode does not get the global temperature.
- An on/off light gets neither.

These cases are listed as information, not as warnings.

### Every mood: attach the adjustment to types and single lights

Choose **Every mood** (the chip after the moods, or *Edit mood → Every mood* in the room card). Settings made there apply in every mood unless a mood sets its own value. They work for:
- a light type for all rooms
- a light type in one room
- a single light or group (Home Assistant or Zigbee2MQTT group)

Order, most specific last:
- global *every mood*, then the global mood (with its parent and fallback moods)
- room *every mood*, then the room mood
- light *every mood*, then the light's mood entry

*Every mood* entries never switch lights on or off: they have no On/Off, effect or ambient setting. Examples:
- *Kamar Norður · Spottar*: brightness and temperature follow global.
- *Living room · Pendant (Uppljós + RGB)*: brightness global −20, fixed amber colour.
- *Bedroom · LED strip*: brightness follows global, temperature *Ignore*.

The room card shows one line per light, for example: *Brightness: Global · Temperature: Global · Effective now: 62 % / 3200 K*.

### Smooth updates, never a light on

When a source value changes, lights that follow it get a **gradual update**:
- only lights that are **already on**: a global change never switches a light on
- in activated (Active) rooms only, through the same rules as mood lighting: manual overrides, exclusions, group de-duplication, availability, occupancy, automation conflicts
- only when the change is meaningful (default 3 percentage points or 100 K, compared with what was last sent or with the light's state)
- at most once per light per interval (default 60 s); rapid changes are coalesced and **the newest value wins**
- only the properties that follow the global value are sent, with a transition (default 10 s) where the light supports one; set the transition to 0 if your adjustment already changes in small steps
- a light whose current colour mode is RGB/HS, or that runs an effect, keeps its colour

A light switched on later by something other than a person (another automation, an unattributed change) gets the current global values right away. A light switched on by a person is a manual override and is left alone. Mood changes, arrivals and activation are not throttled: they use the current global values immediately. Nothing is sent on start-up or reload.

### Existing controllers

Smart Occupancy finds parts of your existing system that already command lights:
- an Adaptive Lighting switch used as a source (while it is on, its `configuration.lights` are its own)
- enabled automations (directly, or through a script) that use a source entity and control lights

Smart Occupancy never sends global updates to those lights. A warning (*Existing adjustment also controls this light*) and the preview explain why. To let Smart Occupancy follow the global values for such a light, stop the other system from commanding it. Nothing is changed or disabled automatically. The usual automation-conflict rules still apply to mood changes.

### Preview

The preview lists every light, its current state, the mood's result (for example *55 % global · 5000 K global*), and a **Global adjustment** line for the next gradual update:
- *will/would follow* with the values
- *up to date*, *queued* (throttled), *blocked* (with the reason) or *not adjusted*

A preview never changes a light.

## Manual mode

When **Main Mood** is `Manual`, all automatic lighting is paused. `Manual` is an operating mode, not a mood:
- **No automatic light command at all.** Mood changes, arrivals, room activation, profile edits, global adjustment updates, reconnecting lights: nothing is sent in any room.
- **Queued applications and updates are dropped** and never replayed.
- **Manual light changes are not corrected** and are not recorded as overrides.
- **Occupancy, diagnostics and editing continue.**
- The cards show **Manual control — automatic lighting paused**. Active rooms report `paused`, and the Room Status card shows *Paused (Manual)*.
- `Manual` needs no lighting profile, and it is not listed as a mood.

**Wall switches keep working in Manual** (0.3.5): an Active switch still executes its button actions; see [Wall switches while Main Mood is Manual](#wall-switches-while-main-mood-is-manual).

Manual takes precedence over every mood, room mood, profile and global adjustment setting. When Main Mood changes to anything else, automatic lighting resumes only in explicitly activated rooms, with the newly selected mood and the current sensor and global values.

The option name is configurable under *Smart Lighting → Advanced → Manual option* (default `Manual`). The selectors, their options and your automations are not changed. `Auto` stays a Manual Mood option as before.

## Wall switches and remotes (Phase 0.3.5)

Physical wall switches and remotes stay the simple, reliable way to operate a room; Smart Occupancy handles the intelligence behind them. A switch is assigned to a room in the **Switches** tab of the Smart Room Lighting card. No MQTT topic or YAML is needed for normal configuration.

The examples use a Schneider Electric Wiser battery wireless switch paired with Zigbee2MQTT as `avb_ksu` (topic `zigbee2mqtt/avb_ksu`, payload `{"action": "on_right", "battery": 100, "linkquality": 120}`). Its button names are only an illustration: the buttons of every device are discovered from the device or learned by pressing them, never assumed.

### Input sources

| Source | How presses arrive | Discovery |
|---|---|---|
| **Zigbee2MQTT** (preferred) | the `action` of the device topic `<base>/<friendly name>` (base topic `zigbee2mqtt`, configurable), through Home Assistant's MQTT integration | Zigbee2MQTT's retained device list (`<base>/bridge/devices`): name, model, the values of the `action` expose, bindings, renames |
| **MQTT device triggers** | Home Assistant MQTT device triggers of the device (attached like an automation trigger) | the device's triggers (Zigbee2MQTT creates them after a button was first used) |
| **Event entity** | an `event` entity; its state is the time of the event, the action is `event_type` (or `button` + `event_type`) | the entity's `event_types` |

Smart Occupancy never opens its own MQTT connection, stores no credentials, never subscribes to wildcards and never publishes anything (no Zigbee management commands). It subscribes only to the topics of the switches you assigned (plus their availability topic and the bridge device list). Experimental Zigbee2MQTT event entities are **not** required.

One source per switch is authoritative: *Automatic* prefers raw MQTT, then device triggers, then the event entity (*Advanced switch settings → Input source*). The same press seen through several paths is executed once; two separate presses of the same button are both kept (a repeat within the debounce time, default 100 ms, counts once).

### Assign a switch (example: Kamar Suður, `avb_ksu`)

1. Open **Smart Room Lighting** for the room (`area: kamar_sudur`) → **Switches** → **Add a switch or remote**.
2. Pick **avb_ksu** (Schneider Electric · Wiser wireless switch · Zigbee2MQTT · 10 button actions known) → **Assign to Kamar Suður**.
3. The switch starts in **Observing** with the buttons the device declares. Recognisable buttons get a suggested action (ON → *Turn lights on*, OFF → *Turn lights off*, *brightness move up/down* → *Start dimming up/down*, *brightness stop* → *Stop dimming*); every other button stays unassigned.

A device that cannot be discovered can be added under *Add a switch → Advanced* with its exact MQTT topic (no `+` or `#`) or its event entity.

### Learn buttons

1. **Configure** the switch (or use **Learn buttons** in its card).
2. Click **Learn buttons** and press a button on the switch.
3. The card shows the detected action (*Detected: ON right `on_right`*). Learned buttons are saved at once and survive restarts.
4. Assign its action in the button table; repeat for the other buttons; click **Done** (learning also ends after 60 s).

While learning, nothing is executed (no light is switched, even when the switch is Active) and every available input path listens. Retained MQTT messages, battery and link-quality updates, malformed messages, restored or stale event-entity states and triggers replayed right after attaching are not learned. Single, double, hold and release presses are learned exactly as the device names them; no press type is synthesised.

### Assign actions

Each learned button event has its own action (or none):

| Group | Actions |
|---|---|
| Lights | Turn lights on · Turn lights off · Toggle lights · Increase / Decrease brightness (one step per press) · Start dimming up / down · Stop dimming · Set brightness · Set colour temperature |
| Moods | Select mood (this room, optionally for N minutes; or all rooms) · Return to Auto mood |
| Advanced | Activate scene · Run script · Activate WLED preset (WLED preset selector) |

Light actions control **Room lights** (every managed light of the room, through managed groups instead of their members) or one chosen light or group. **Turn lights on** uses the **current mood** (lights the mood switches on get its brightness and colour; lights it leaves off stay off; with no mood, Manual, or a mood that switches none of them on, the lights come on with their own last values), the lights' **last used** values, or **fixed** brightness / colour temperature. Values a light cannot take are not sent (a brightness-only strip never gets a colour temperature; colour temperatures are clamped to each light's range). Moods are the discovered options (`Morgun`, `Dagur`, `Kvøld`, `Nátt`, `Hygge`, `Arbeiði`, `Veitsla`, …); option names are never changed.

Only these fixed actions are available. Every entity is validated (scene, script, WLED preset, light), and no template, shell command or arbitrary service call can be configured.

### Observing and Active (switch modes)

Earlier versions called these *Listen only* and *Smart Occupancy control*; the stored values (`listen`, `control`) are unchanged.

| | Observing (default) | Active |
|---|---|---|
| Light commands | **never** | the assigned actions |
| Presses count as manual changes | yes: light changes in the room in the next 3 s are a person's | yes, and the lights it switches get a manual override directly |
| Suitable for | switches bound directly to lights (Zigbee binding) or handled by your own automations | switches that only Smart Occupancy acts on |
| Activation | — | explicit: the selector or a double-click on the switch's status (see [Operating modes](#operating-modes-036)) |

Smart Occupancy never takes over a switch by itself, never removes or changes Zigbee bindings and never disables automations. Before activating control:

- **Direct Zigbee binding**: Zigbee2MQTT reports a binding of the switch to lights or a group (`genOnOff`, `genLevelCtrl`, …; the normal binding to the coordinator is ignored). Control is **refused** while it exists, so a press is never executed twice. Keep the switch Observing, or remove the binding in Zigbee2MQTT (*Devices → avb_ksu → Bind*) and set it to Active.
- **Competing automations**: automations whose triggers use the switch (its device triggers, its event entity or its MQTT topic) are listed. Disable or change them first, or tick them to acknowledge that both may act.

An Active switch works whatever the room's automatic lighting mode is (0.3.6; in 0.3.5 a room in Observe silently suppressed every switch action).

### Manual overrides

A press of an Active switch is a person's explicit request and wins over automatic lighting:

1. the press is recorded (*Last button: OFF right*),
2. the mapped command is sent (to the managed group, not to each bulb),
3. the lights it changed get a **manual override** (*source: wall switch avb_ksu, reason: Physical switch OFF requested*), for the room's manual override time (or the switch's own, *Advanced → Manual override*; 0 = until the room is empty),
4. occupancy, mood changes and the global adjustment leave those lights alone until the override expires or the room becomes vacant.

Example: the room is occupied and Smart Occupancy switched the ceiling lights on. You press **OFF**: they switch off, the override is registered, and they are **not** switched back on just because the room is occupied and dark. Pressing **ON** switches them on even when the daylight sensor says the room is bright: daylight gates only *automatic* switch-on. Overrides are listed in *Advanced → Manual overrides* (with the switch and the reason) and in the switch's card (*Manual override: Active*).

Hold-to-dim and brightness steps keep their brightness as an override until its normal expiry, so the global adjustment does not immediately correct it.

### Wall switches while Main Mood is Manual

**Manual pauses automatic lighting, never your wall switches.** While Main Mood is `Manual`, Smart Occupancy sends no automatic command, but an Active switch still executes its actions (there is no mood then, so *Turn lights on* uses the lights' own last values), and its overrides are recorded so that your choice is still respected when Manual ends.

### Moods from a switch

- **This room**: sets the room's mood (the same room mood as *Overrides → Room mood*); *Return to Auto* makes the room follow the global mood again. Optionally for a number of minutes, after which the room returns to automatic selection.
- **All rooms**: selects the option on your **mood selector (Manual Mood)**, `input_select.mood`; your existing automation then sets Main Mood, as when you change it in the app. *Return to Auto* selects `Auto`. **Main Mood (`input_select.main_mood`) is never written.**

### Dimming

- *Increase / Decrease brightness*: one step per press (default 10 %), for remotes that send one message per press or repeat messages while held.
- *Start dimming up / down* + *Stop dimming*: for switches that send a start and a stop message (for example `brightness_move_up_left` … `brightness_stop_left`; nothing assumes that a held button repeats its message). Smart Occupancy then sends one brightness command per interval (default 8 % every 0.4 s, with a matching transition), waits for each command to finish, stops when STOP/release arrives (subject to the Zigbee and MQTT latency), at the maximum or minimum brightness, or after the safety timeout (default 10 s) if STOP never arrives.
- Step, hold step, interval, minimum and maximum brightness and the timeout are set per switch under *Advanced switch settings → Dimming*. Lights without brightness are not dimmed; an off light is never dimmed *down*, and dimming *up* switches it on at the minimum.

### Optional: presses as room activity

*Advanced switch settings → A press counts as activity in the room* (off by default): a press refreshes the activity of an **occupied** room in the existing occupancy engine, so it is vacated one quiet period later. A press never makes an empty room occupied and never keeps a room occupied by itself.

### Diagnose MQTT button messages

The switch's card shows the connection, availability (Zigbee2MQTT `…/availability`), the last button and time, the result (*Room lights (5 lights) switched off*), what it controls and how many buttons are configured. *Advanced switch settings → Listener diagnostics* shows the MQTT state, the authoritative and available inputs, the topic, the last command, the last ignored message and why (*retained message*, *the same press through another input path*, *malformed message*, …), the number of ignored status updates, and the recent presses. Raw payloads are never shown or logged. To watch the messages yourself: Zigbee2MQTT → *Logs*, or Home Assistant → *Settings → Devices & services → MQTT → Configure → Listen to a topic* `zigbee2mqtt/avb_ksu`, and press a button.

If nothing arrives: check that MQTT is connected (the Switches tab says so), that the device topic matches (*Advanced → MQTT topic*; the base topic is a global setting), and whether the device is online. A Zigbee2MQTT rename is followed automatically (by IEEE address).

### Remove or replace a switch

*Remove* in the switch's card deletes its learned buttons and assignments and ends its subscriptions; overrides it created expire as usual. Bindings, automations and the device are not changed. To replace a switch, remove the old one and assign the new device (learn its buttons again). The wall switch entities of earlier versions (*Advanced → Wall switch entities*) are unchanged and keep working as listen-only sources.

## Daylight-aware automatic lighting (Phase 0.3.3)

When a room becomes occupied, its mood lights are switched on only if **artificial light is needed**. The daylight decision says *whether* lights are switched on; the mood profile still says *what they look like* (brightness, colour temperature, colour, global adjustment). Smart Occupancy does not calculate brightness or colour from lux.

### In the Smart Room Lighting card (Automatic tab)

- **Turn on when occupied and dark**: on by default.
- **Indoor light sensor**: pick any illuminance sensor (the room's own are listed first). No sensor: outdoor light and the sun decide.
- **Outdoor light**: the global outdoor sensor, shown as inherited. It is one setting for every room: change it with *change for all rooms* here, or in the Smart Lighting card under *Advanced → Settings*.
- **Darkness threshold**: lights turn on below this (default 50 lx).
- **Daylight now**: *Dark*, *Bright* or *Uncertain*, with a short reason and how natural light was determined, for example:
  - *Natural light: 35 lx — measured indoors, lights off*
  - *Natural light: at most 10 lx — measured indoors with lights on*
  - *Natural light: Low — estimated from outdoor darkness*
  - *Natural light: ≈ 80 lx — estimated from outdoor light (… learned factor)* (an estimate, not a measured value)
  - *Natural light: Uncertain — no trustworthy reading*
- **Last decision**: what the last arrival (or darkness change) did and why.

*Advanced daylight settings* (collapsed, in the same tab): switch-on policy and timing, the fallback, hysteresis and stability, sensor priority and freshness, contamination (lights that reach the sensors), calibration, and daylight diagnostics.

Values are rounded (1 lx below 10, 5 lx below 100, then two significant digits); sensors are not more precise than that.

### When lights are switched on

- **A new visit**: the first motion of a *Trigger* sensor (PIR) switches on at once, without waiting for confirmation. Radar, PC activity and other *Maintain* sensors keep the room occupied but never start a visit alone. Under *Advanced daylight settings → Switch on at*, choose *Confirmed occupancy* for fewer false triggers.
- **The room becomes dark while occupied** (after the stability period, default 90 s).
- **A mood change**, room activation, profile change or a light coming back: off lights are switched on only if it is dark.

Only **off** lights the mood switches **on** are gated. Lights that are already on still follow the mood, *Off* is never affected, *Leave unchanged* lights are not touched, and **ambient** settings (window lamps, decorative lighting that follows the mood whether or not the room is occupied) are never gated. Nothing is switched off because daylight became sufficient (that may become a separate option later).

All existing protections still apply first: exclusions, groups (one command per group), offline lights, manual overrides, unacknowledged automation conflicts, occupancy (vacant/uncertain rooms), Observe and Manual. Commands are batched, an identical command is not repeated, and noise around the threshold causes no repeated commands (hysteresis plus the stability period).

*Advanced daylight settings → Automatic switch-on* offers: *When occupied and dark* (default), *When occupied, regardless of daylight* (the behaviour before 0.3.3), *Never automatically*. *When uncertain*: *Turn lights on* (default, as before 0.3.3) or *Leave lights off*.

### How natural light is determined

Evidence, strongest first:
1. **After civil dusk** (sun more than 6° below the horizon) there is no natural light, whatever a sensor reads.
2. **An indoor reading with the lights off**: taken at least the settling time (default 60 s) after the last influencing light turned off, with none on. Only these readings are trusted as natural light, and the last one is kept (with its time) while lights are on.
3. **An indoor reading with lights on that is still below the threshold**: lamps only add light, so natural light is *at most* that much. It is dark. A bright reading with lights on is **never** used: it may be the lamp itself.
4. **Outdoor darkness** (outdoor sensor at or below 50 lx, smoothed).
5. **The most recent indoor evidence** (default up to 30 min old), scaled by the change in outdoor light if both are known.
6. **Outdoor light × the room's daylight factor.** Outside being bright never makes every room bright: the factor is learned from trusted indoor/outdoor pairs (at least 3, outdoor ≥ 200 lx, 10 min apart; the median is used), or set under *Advanced daylight settings → Calibration*.
7. **After sunset** (twilight): low natural light, medium confidence.

Otherwise daylight is *Uncertain* and the room's fallback applies. `unknown`, `unavailable` and non-numeric readings are never treated as 0 lx.

**Contamination by the room's own lights.** Every managed light (and every member of a managed group) counts as influencing the sensor, plus every light in the area (*Every light in this area*, on by default) and lights you add (*Also*: window lamps, lights of an adjacent open-plan area; lights or switches). While one is on, readings are not used as daylight. After they all turn off, a reading counts only if it was taken after the settling time. With the default *New measurement: changed value*, a sensor that re-publishes its cached value (common with Zigbee PIR sensors) does not produce a "fresh" measurement just because the lamp went off; choose *every report* for sensors that report often (e.g. FP2). An unavailable influencing light lowers the confidence instead of being ignored.

**Freshness and reliability** per sensor (*Advanced daylight settings → Light sensors*): a maximum age (default none: a stable value is not a failed sensor), what counts as a new measurement, and *Reliable* / *Less reliable* (lower confidence). The first sensor is preferred; the others are fallbacks, in order.

**Outdoor sensor** (optional, one global setting: Smart Lighting card → *Advanced → Settings*, or *change for all rooms* in a room's Automatic tab): smoothed with a time-weighted median over 5 minutes, so short spikes or shadows do not change decisions.

**Darkness decision.** Below the turn-on threshold (default 50 lx) it is *Dark*; at or above the light-not-needed threshold (default 100 lx) it is *Bright*; in between the previous decision stays (hysteresis). An arrival uses the latest evidence at once; a change while the room is occupied counts only after it held for the stability period.

**Extension point.** Blinds and curtains are not read yet; the estimator has a shading input reserved for them.

### Entities and diagnostics

- `sensor.smart_occupancy_<room>_daylight`: `dark` / `bright` / `uncertain`, with `natural_light`, `lux` (rounded), `estimated`, `confidence`, `basis`, `contaminated`, `sensor`, `outdoor_lux`, `sun_elevation`, `reason`, `last_activation_at` and `activation` (why the last arrival did or did not switch lights on). Attributes are not recorded, and the entity is written only when the rounded summary changes.
- The room card's *Automatic → Advanced daylight settings → Daylight diagnostics*: chosen sensor, last trustworthy reading, whether indoor readings are affected by lights, outdoor value, sun, confidence, last switch-on and errors.
- Daylight sensing has its own health (*Daylight*), separate from occupancy and lighting: an unavailable light sensor never makes occupancy look degraded.

## Occupancy engine

Each enabled room has its own state machine, fed by state-change events of its signals. There is no polling, and timers run only while something is pending.

### Sensor roles

| Role | While active | When cleared |
|---|---|---|
| **Trigger** (PIR) | Immediate occupancy. Ignored once it has been on longer than *trigger stuck after*, so a stuck PIR can't hold a room forever. | Nothing |
| **Maintain** (radar, PC activity, bed) | Holds occupancy: vacancy can't be confirmed | Not evidence of vacancy by itself. Clears shorter than the *dropout grace* are bridged. |
| **Confirm vacancy** | Holds occupancy, like Maintain | **Vacancy evidence**: vacancy needs only *exit confirmation* of quiet instead of the *inactivity timeout* |
| **Ignore** | — | — |

**Door contacts** count only as movement, whatever their role (except Ignore):
- A door movement alone never makes a room occupied. It shows as *possible occupancy* for the correlation window, with *occupied* staying off.
- A door movement followed by nothing new in the room is exit evidence. Movement in an *adjacent room* right after it strengthens that evidence.
- A door that stays open or closed means nothing.

**Unknown and unavailable are never "off".**
- An unavailable signal is neither presence nor vacancy evidence, and while any presence signal is unavailable the fast exit path is disabled.
- A presence sensor that drops out *while reporting presence* holds the room for one extra inactivity timeout.
- When it reconnects, the quiet period restarts from the reconnect.
- When every occupancy sensor is unavailable, the room is `uncertain` and keeps its last occupied value.

### States

| State | Occupied | Evidence required |
|---|---|---|
| `vacant` | off | No activity for the required quiet time, or never occupied |
| `possible_occupancy` | on (off for a door alone) | New, uncorroborated evidence: one trigger event, a presence signal that just turned on, or a door movement |
| `confirmed_occupancy` | on | A presence signal active for the correlation window, two different signals within the window, or a trigger active for the whole window. Stays confirmed while any evidence remains. |
| `possible_vacancy` | on | It was occupied, and no evidence remains. Vacancy is confirmed once the room has been quiet for *exit confirmation* (if there is positive vacancy evidence, or occupancy was never confirmed) or the *inactivity timeout* (otherwise). Any new activity returns to occupancy. |
| `uncertain` | last value | No occupancy sensor has a known state, or none is configured |

Occupied → vacant always needs at least *exit confirmation* of quiet, and presence dropouts are bridged, so the output doesn't toggle when sensors briefly disagree. Events older than a signal's current value (out of order) are ignored.

**Restart and reload.** The previous decision is restored from `.storage`. An occupied room stays occupied during the *start-up hold*, and its quiet period starts again from the restart, because nothing is known about the time Home Assistant was down. So a restart can't cause a false vacancy.

### Timing

Every value can be set globally and overridden per room.

| Parameter | Default | Meaning |
|---|---|---|
| Movement correlation window | 10 s | Signals within this window corroborate each other. A single signal must last this long to confirm occupancy on its own. |
| Exit confirmation | 300 s | Quiet time before vacancy when there is positive vacancy evidence |
| Normal inactivity timeout | 600 s | Quiet time before vacancy otherwise |
| Radar dropout grace period | 30 s | Bridges short clears of Maintain and Confirm-vacancy signals |
| Trigger stuck after | 1800 s | Trigger signals active longer than this are ignored (0 = off) |
| Presence sensor stuck after | off | The same for Maintain and Confirm-vacancy signals. Off by default, because people can sit still for hours. |
| Start-up hold | 120 s | Minimum time the restored state is kept after start-up or reload |

### Confidence

`sensor.…_confidence` is an **evidence-quality score**, not a predicted probability or a measured accuracy. It is calculated as:

> 100 × *evidence strength* × (0.5 + 0.5 × *sensor coverage*)

- *Sensor coverage* is the share of the room's occupancy sensors that report a usable, non-stuck state.
- *Evidence strength* depends on the state:
  - `confirmed_occupancy`: 0.6, plus 0.2 if a presence sensor is active, plus 0.2 if two or more signals are active
  - `possible_occupancy`: 0.4 (0.25 for a door alone)
  - `possible_vacancy`: 0.4 to 0.5
  - `vacant`: 0.8 (1.0 when every presence sensor reports clear)
  - `uncertain`: 0

Both factors are in the sensor's attributes.

### Pilot: Kamar Norður

1. Open **Configure → Rooms → Kamar Norður** and check the assignments:
   - PIR `binary_sensor.pir_kan_occupancy`
   - radar `binary_sensor.rdr_kno_occupancy`
   - PC activity: your Nobara PC sensor
   - door `binary_sensor.swh_kno_contact`
   - existing occupancy helper `input_boolean.tilstadar_kamar_nord`

   The Aqara PIR reports device class `occupancy`, so discovery suggests it as a radar. Move it to *PIR sensors*.
2. Keep the default roles: PIR = Trigger, radar = Maintain, PC = Maintain, door = Trigger. Set the radar to *Confirm vacancy* if you trust its clear state enough for the faster exit path.
3. Optionally, on the room's *Occupancy* step, choose adjacent rooms such as the hallway.

With that:
- PIR motion gives immediate occupancy.
- The radar and PC activity hold it, so a radar clear never ends occupancy while the PC is active.
- A stuck PIR is ignored after 30 min.
- An unavailable sensor never leads to vacancy.

The existing lighting automation and helper are not touched. Compare them with `sensor.smart_occupancy_kamar_nordur_helper_comparison`.

## Room Status card

The integration serves and loads the card itself: there is no resource to add and nothing to install. After updating, reload the browser once.

Add it with **Edit dashboard → Add card → Smart Occupancy Room Status**, and pick the room in the visual editor. Or use YAML:

```yaml
type: custom:smart-occupancy-room-card
area: kamar_nordur        # the area id of the room
# entity: sensor.smart_occupancy_kamar_nordur_state   # alternative to area
# title: Kamar Norður     # optional
```

The card is read-only. It shows the occupancy state, evidence quality, active sensors, a pending vacancy with a live countdown, and the *Observe* or *Mood lighting* badge. Health is three chips: **Occupancy** (sensors only), **Lighting** and **Configuration**. Each warning is one short line; *Details* shows the full explanations. If a helper is configured, its comparison is one line. An unplugged light shows under *Lighting* only. The layout adapts from phone width to wide dashboards and follows the theme.

## Entities

All entities belong to one service device, *Smart Occupancy & Lighting*. Unique IDs are stable: room entities are keyed by `area_id`, so they survive area renames.

| Entity | State | Notes |
|---|---|---|
| Status | healthy / warning / config_error / unavailable | One-line `summary`, counts per classification, every problem with details |
| Operating mode | observe | The integration-wide mode stays Observe. `lighting_control` is true when a room has mood lighting enabled. `mood_lighting_rooms` lists those rooms. |
| Effective mood | Main Mood value | Attributes: `auto_mood`, `manual_mood`, `expected_main_mood`, `follows` (auto/manual), `consistent` |
| Mood mismatch | on / off / unknown | On only if Main Mood disagrees with the rule for longer than the grace period. Unknown when a helper is unavailable. |
| Configured rooms | count | Room → status |
| Light categories | count | Per category: label and number of classified lights |
| Discovered sensors | count | Candidate signals found in enabled rooms |
| Missing entities | count | Configured entities that are deleted or disabled. `items` and `entities` hold the details. |
| Unavailable entities | count | Configured entities that are unavailable, have no value, or are not provided by their integration. `items` and `entities` hold the details. |
| Configuration problems | count (configuration errors + warnings) | `problems` (actionable) and `notes` (informational). Offline devices are **not** counted here. |
| *Room* observation | healthy / warning / config_error / unavailable | Health of the room's signals and lights. **Not** an occupancy state. |
| Re-check diagnostics (button) | — | Re-evaluates immediately; reads states only |

**Per room (Phase 0.2).** IDs are suggested from the area name when the entities are created. They stay the same when the area is renamed: unique IDs are keyed by `area_id`.

| Entity | State | Notes |
|---|---|---|
| `binary_sensor.smart_occupancy_<room>_occupied` | on / off | Device class occupancy. Keeps its last value while the room is `uncertain`. |
| `sensor.smart_occupancy_<room>_state` | `vacant` / `possible_occupancy` / `confirmed_occupancy` / `possible_vacancy` / `uncertain` | The evidence is in its attributes: `active_sensors`, `unavailable_sensors`, `stuck_sensors`, `recent_door_movement`, `pending_vacancy`, `vacancy_confirm_at`, `quiet_since`, `required_quiet_seconds`, `vacancy_evidence`, `reason`, `signals` (per-signal detail), `health` (occupancy / lighting / configuration), `comparison`, `timing`. These attributes are not recorded. |
| `sensor.smart_occupancy_<room>_confidence` | 0–100 % | Evidence quality (see [Confidence](#confidence)) |
| `sensor.smart_occupancy_<room>_last_activity` | timestamp | Start of the latest motion, presence or door movement. `source` is the entity. |
| `sensor.smart_occupancy_<room>_reason` | text | Why the room has its current state |
| `sensor.smart_occupancy_<room>_mood_lighting` | `observe` / `active` / `blocked` | Phase 0.3. Mood lighting state and diagnostics of the room (see [Diagnostics](#diagnostics)). Attributes are not recorded. |
| `sensor.smart_occupancy_<room>_daylight` | `dark` / `bright` / `uncertain` | Phase 0.3.3. Whether occupancy may switch lights on, and how natural light was determined (see [Daylight](#daylight-aware-automatic-lighting-phase-033)). Attributes are not recorded. |
| `sensor.smart_occupancy_<room>_helper_comparison` | agree / disagree / unknown | Only with an existing occupancy helper. Attributes: `disagreements`, `disagreement_since`, `total_disagreement_seconds`, `helper_cleared_prematurely`, `engine_possibly_retained_too_long`, `over_retention_total_seconds` / `_max_seconds`, `helper_only_occupied`, `statistics_since`. A disagreement counts only after it has lasted longer than the correlation window. |

**How the comparison counts.** When the helper turns off while the engine still says occupied, an episode starts. It ends in one of two ways:
- *Premature clear*: the helper turns on again, or new activity is seen, before the engine reaches vacancy.
- *Possible over-retention*: the engine reaches vacancy without new activity. The overhang is recorded.

Only these aggregate counters are kept, in `.storage`, never in Recorder tables.

### Reading problems in the UI

Open any diagnostic entity and expand **Attributes**, or go to **Developer tools → States**. You don't need the log.

- **`items`** has one line per affected entity, for example `light.x (Spot 3) — Kamar Norður — Light unavailable`.
- **`entities`**, `problems` and `notes` hold structured details for each problem:

  | Field | What it holds |
  |---|---|
  | `type`, `title` | Problem type and its short name |
  | `classification` | `config_error`, `unavailable`, `warning` or `info` |
  | `severity` | How serious it is |
  | `category` | `configuration` or `operational` |
  | `explanation` | What is wrong, in plain language |
  | `action` | What to do about it |
  | `room`, `entity_id`, `name` | Where it is, and the entity's friendly name |
  | `first_detected`, `last_checked` | When it was first seen and last re-evaluated |

**Room observation attributes**

| Attribute | Meaning |
|---|---|
| `configuration_valid` | No configuration error in this room |
| `usable` | At least one Trigger/Maintain signal is available |
| `reasons` | Why the room is not healthy |
| `sensors_available` / `sensors_unavailable` | Sensors by health |
| `lights_available` / `lights_unavailable` | Lights by health |
| `pending` | Entities still loading during start-up |
| `operating_mode` | The mode the integration is running in |

**Room status**

| Status | When |
|---|---|
| `config_error` | The room's configuration points at something unusable: a deleted area, a deleted or disabled entity, or something that isn't a light |
| `unavailable` | Every Trigger/Maintain signal is unavailable, so occupancy can't be observed |
| `warning` | Anything else that needs attention. An offline optional light only makes the room a warning. |
| `healthy` | No problems |

**Entity health**

| Health | Meaning | Clears by itself? |
|---|---|---|
| `unavailable` | The device is offline | Yes, when it reconnects |
| `unknown` | No value reported yet | Yes, on the next report |
| `not_provided` | Registered, but its integration doesn't provide it (integration not loaded, or device removed) | No |
| `disabled` | Disabled in Home Assistant | No: enable it or remove it from the room |
| `missing` | Deleted | No: select its replacement or remove it |
| `pending` | Still loading during start-up. It gets a 120 s grace period after start-up and is not reported. | — |

**When problems clear**

Problems clear automatically: every evaluation rebuilds the full picture. An evaluation runs on each of these events:
- a configured entity changes state
- an area, device, entity or label registry change, such as an entity being added, removed, renamed or disabled
- an options change
- a reload
- Home Assistant start-up

`first_detected` survives reloads and restarts.

### Classification

A light belongs to a category when the category's label is on the light entity or on its device. This matches how Home Assistant resolves `label_id` targets. Names are never used.

### Problems detected

- Missing, unavailable or not-loaded entities
- Deleted areas and labels
- Mood helpers with the wrong domain or missing
- Auto option not found among the Manual Mood options
- Auto Mood options that Main Mood lacks
- Categories without a label, or sharing a label
- Managed lights with no category, or with several categories
- Lights managed by several rooms
- **A group and its member both selected for independent control**
- Invalid profile values: several colours, *Off* with attributes, values out of range
- Profile entries for an unknown mood, category or light, and moods missing from Main Mood
- Unknown parent or fallback moods, and mood inheritance loops
- A mood that Main Mood cannot represent (e.g. `Onki`), naming the missing option
- RGB, colour temperature, brightness or effect requested on a light that doesn't support it
- Managed lights that moved to another area, groups that hide their members, a group and its member in different rooms
- Existing automations that appear to control managed lights
- Failed light commands
- Profiles that follow a global source that is not configured, and global sources that are unavailable (with the last value in use)
- Lights that the existing adjustment system (an Adaptive Lighting switch, or an automation using a source) also adjusts
- Manual mode active (information)
- Light sensors that are missing, unavailable, invalid or stale (with the fallback in use), a missing or unusable outdoor sensor, missing influencing lights, and a light sensor that reads bright after dusk with the lights off (a light not listed as influencing)

### Recorder and logging

- No sensor has a `state_class`, so no long-term statistics are created.
- List-valued attributes and `last_checked` are excluded from the Recorder.
- While problems exist, `last_checked` is republished at most every 15 minutes.
- Problem history is kept in `.storage`, not in the Recorder.
- States change only when the observed situation changes. Occupancy entities write only when their state or attributes change. A pending vacancy is one scheduled callback: nothing is written while waiting, because the card counts down in the browser.
- Plain on/off changes of sensors go to the occupancy engine only. They don't rebuild the diagnostics snapshot.
- Logs are structured `key=value` lines (problem details are in entity attributes, so the log is optional):
  - `problem_detected` (warning, once per problem)
  - `problem_resolved` (info)
  - `entity_renamed` (info)
  - `observe_summary` (info, once after the start-up grace period)
  - `observe_snapshot` (debug)
- No problems are logged until the start-up grace period has passed. Integrations that are still loading would otherwise look like missing entities.

To see debug logs:

```yaml
logger:
  logs:
    custom_components.smart_occupancy: debug
```

**Download diagnostics** (integration page → ⋮) gives you:

- the full configuration and snapshot
- discovery results for every area
- all labels
- group memberships
- live active signals per role
- the resolved-profile preview

### Robustness

- **Event driven.** The integration listens only to state changes of configured entities and to updates of the area, device, entity and label registries. Updates are debounced.
- **Renames.** When an entity ID is renamed in the entity registry, the stored configuration follows it automatically.
- **Removed entities.** Removed entities are reported, not deleted from the configuration, so they recover if they come back.
- **Not yet loaded.** An entity that is in the registry but has no state yet counts as unavailable, not missing.
- **Mode lock.** A hand-edited `operating_mode` other than `observe` still runs as Observe and is reported as an error.

## Development and tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
```

To test against a Home Assistant release newer than the harness (2026.11.1 at the time of writing), install it over the harness, install the libraries that release declares for the components the tests start (MQTT and the Supervisor client its config flow imports), and use the runner, which aliases one helper that moved in 2026.11:

```bash
.venv/bin/pip install homeassistant==2026.11.1 home-assistant-frontend==20261028.5
.venv/bin/pip install $(.venv/bin/python scripts/component_requirements.py mqtt hassio)
.venv/bin/python scripts/run_tests.py
```

The suite covers:
- configuration persistence and migration (0.1 → 0.2 → 0.3), the UI flows, discovery, mood observation and label classification
- every occupancy rule: pure engine tests with explicit time, and tests in Home Assistant with the pilot's entity IDs
- mood lighting:
  - profile inheritance (pure tests)
  - command planning per light capability (pure tests)
  - execution in a running Home Assistant with mocked light services: three-selector resolution, `Onki`, new moods, labels and custom categories, group de-duplication, overrides, vacancy and ambient lights, manual overrides, automation conflicts, offline lights, restart, unload
  - websocket permissions and validation
- global light adjustment (0.3.2):
  - source normalisation (%, 0–255, K, mired, Adaptive Lighting attributes, unavailable values)
  - modes, offsets, clamping, every-mood entries and colour/effect precedence (pure tests)
  - gradual updates with simulated lights: thresholds, throttling, coalescing, transitions, never switching on, group de-duplication, RGB, brightness-only and colour-only lights, manual overrides, Observe, competing automations and Adaptive Lighting switches, restart and persistence, upgrade from a 0.3.1 store
- Manual mode: no command during occupancy changes, global updates, profile and room edits, reloads and start-up; queued commands dropped; resume without replay
- daylight (0.3.3):
  - pure tests: readings (`unknown`/`unavailable` never 0 lx, freshness, report modes), contamination and settling, re-published PIR values, every estimation step, outdoor spike smoothing, calibration, hysteresis, stability, the gate and the store migration
  - in Home Assistant with simulated lights: dark and bright arrivals, prompt PIR and conservative confirmation, radar holding, lamp-lit readings rejected, settling and fresh samples, outdoor darkness, bright outside with a dark room, frequent and infrequent sensors, stale/unknown/unavailable and mixed sensors, outdoor spikes, hysteresis and becoming dark, occupancy uncertainty, On/Off/Ignore per mood, global brightness/temperature, Manual, Observe, manual overrides, automation conflicts, ambient lighting, groups, websocket persistence, reload/restart, and the upgrade from a 0.3.2 store
- wall switches (0.3.5):
  - pure tests: action names and press types, suggested defaults, Zigbee2MQTT payloads (battery/link-quality updates, cleared and malformed actions, oversized messages), event-entity actions, topic validation (no wildcards, no bridge topics), deduplication across paths with deliberate repeats, the bridge device list and bindings, the model's bounds and the store migration
  - in Home Assistant with the real MQTT integration (mocked broker) and simulated Wiser `avb_ksu` events: discovery and assignment, learning (retained, status, malformed), persistence over restarts, ON/OFF of the room group with manual overrides, occupancy and mood changes not undoing a manual OFF, ON despite the daylight gate, Manual mood, Observe, Listen only with a direct binding, MQTT device triggers, event entities (split and stale events), one press through three paths, deliberate repeats and debounce, availability, MQTT reconnects, Zigbee2MQTT renames and removal, competing automations, hold-to-dim with STOP, without STOP, limits and pacing, brightness steps and capabilities, room and global moods, temporary room moods, websocket validation and permissions, listener cleanup on reload and unload, activity evidence
- card serving through the real frontend component

Observe rooms are asserted never to call a light service. Any test that logs an ERROR, or in which Home Assistant reports deprecated usage, fails. CI (`.github/workflows/validate.yml`) runs hassfest, HACS validation and the tests on Home Assistant 2026.10.4 and 2026.11.1.

## Upgrading

**From 0.3.5.** Nothing to do, and no mode is changed by the upgrade: rooms that were Observing stay Observing, switches keep their mode, buttons and actions. There is no store migration. What changes in behaviour: a switch that was already in *Smart Occupancy control* (now *Active*) in a room whose mood lighting was not enabled now **executes its actions** (before, they were silently suppressed). Set it to Observing if you do not want that.

**From 0.3.4.** Nothing to do, and nothing is applied by the upgrade:
- The lighting store is migrated from 1.2 to 1.3: every room gets an empty list of switches. Rooms, moods, profiles, manual overrides, daylight settings, light groups, activation and the global adjustment are kept unchanged.
- The **wall switch entities** of earlier versions (*Advanced → Wall switch entities*) are kept and keep working as listen-only sources; none is converted into a command source.
- Every switch you add starts in **Listen only**. Smart Occupancy control is an explicit activation per switch.
- The Switches tab appears between Automatic and Overrides; the other tabs are unchanged.
- 0.3.4 can still read a 1.3 store after a downgrade (same major version); it ignores the switches and drops them the next time it saves.

**From 0.3.2.** Nothing to do, and nothing is applied by the upgrade:
- The lighting store is migrated from 1.1 to 1.2. Every setting is kept.
- **Rooms where mood lighting was already enabled keep their timing**: they switch on at confirmed occupancy, as in 0.3.2 (*Automatic → Advanced daylight settings → Switch on at*). Every other room switches on at the first motion of a PIR.
- Every room uses *When occupied and dark*, with *Turn lights on* when uncertain. Until you choose a light sensor (or an outdoor sensor and a daylight factor), daylight is usually uncertain or dark by the sun, so lights switch on as before.
- A `…_daylight` sensor is added per room.
- 0.3.2 can still read the store after a downgrade (same major version); it ignores the daylight settings and drops them the next time it saves.

**From 0.3.1.** Nothing to do, and nothing is applied by the upgrade:
- The lighting store keeps its version. New keys (`adaptive`, `manual_option`, the adjustment modes) are added only when you change something.
- Every existing profile value stays **fixed**: a stored brightness or colour temperature without a mode is fixed, and is never converted to "follow global".
- No global source is configured until you choose one. Until then, everything behaves as in 0.3.1.
- If Main Mood already offers `Manual`, selecting it now pauses all automatic lighting. Before, it was treated as an ordinary mood: with no profile it changed nothing, but a configured fallback mood was applied.

**From 0.3.0.** Nothing to do. Profiles, room settings, activation, acknowledgements and overrides are kept. Light categories get a kind derived from their name (*Rgb* → RGB tag; *Uppljós*, *Decorative*, *Task*, *Path*, *Outdoor* → function; others → light type); the options are not rewritten until you change a light type. Existing dashboards keep working: the card types are unchanged, only their layout is new.

**From 0.2.x.** Nothing to do. On the first start:
- The mood profiles stored in the options (global, room and light overrides) are copied once into the new lighting store (`.storage/smart_occupancy.lighting`). The options themselves are not changed, so 0.2 still loads after a downgrade. From then on the store is used; profile edits made in 0.2 after a downgrade are not re-imported.
- Every room starts with mood lighting **disabled** (Observe).
- All entity unique IDs are kept. A `…_mood_lighting` sensor is added per room.
- Brightness in profiles is now 1–100 %. A stored 0 is reported as invalid: use *Off*.

**From 0.1.x.** Configuration version 1.1 is migrated to 1.2 on the first start, keeping everything you configured; then the 0.2 step above applies.

## Known limitations (Phase 0.3.5)

- **Not tested with a physical Wiser switch.** The wall switch support was tested with the automated suite on Home Assistant 2026.10.4 and 2026.11.1, with the real MQTT integration, a mocked broker and simulated Zigbee2MQTT messages, device triggers and event entities. Not hardware verified; not deployed to the production Home Assistant.
- **Zigbee bindings are known only from Zigbee2MQTT's device list.** Bindings made another way (Touchlink, a different coordinator) are invisible: if lights react to a switch by themselves, keep it Observing.
- **Competing automations are found by their triggers** (device triggers, event entity, the MQTT topic). Node-RED, AppDaemon and templated triggers are invisible.
- **Device triggers appear after first use** in Zigbee2MQTT; raw MQTT does not need them.
- **Hold-to-dim is a sequence of brightness steps**, not a native Zigbee *move/stop*: dimming continues until the next interval after STOP at most, plus the Zigbee and MQTT latency.
- **Learning, dimming and the presses waiting per switch are in memory**: a restart ends learning and dimming; learned buttons are saved at once.

### From Phase 0.3.3

- **Not verified on the production Home Assistant.** Tested with the automated suite on 2026.10.4 and 2026.11.1, and on an isolated 2026.11.1 instance with simulated light sensors and demo lights.
- **Lights not listed as influencing** (a lamp in another area that reaches the sensor, a TV) can make a reading look like daylight. After dusk this is detected and reported; during the day add such lights under *Automatic → Advanced daylight settings → Contamination*.
- **After a restart**, every light and sensor state is new, so indoor readings count again only after a new value arrives (or a report, for *every report* sensors). Until then the last trusted reading (kept across restarts, up to 30 min), the outdoor light and the sun are used.
- **The sun thresholds are fixed**: dark after civil dusk (−6°), low light after sunset. In Faroese summer nights the sun does not reach −6°, so an outdoor or indoor sensor decides then.
- **The daylight factor needs an outdoor sensor** and at least three trusted samples in daylight (outdoor ≥ 200 lx). It is a single number per room: it does not model the sun's direction or blinds.
- **A manual change while the room is empty** is respected for the override time, as since 0.3.0, so a person switching lights off from the app in an empty room also keeps them off at the next arrival within that time.
- **No automatic switch-off when it gets bright**, and no vacancy shutdown: Phase 0.4.

### From Phase 0.3.2

- **The existing adjustment system was not inspected on the production Home Assistant** (no access during development). Choose its output entities yourself in *Global light adjustment*. Competing controllers are found only through the configured source entities: an Adaptive Lighting switch used as a source, or automations/scripts that mention a source entity. Systems that command lights some other way (Node-RED, AppDaemon, a script with no reference to the source) are not detected.
- **Gradual updates follow the room's mood plan**: lights in a room that is vacant or whose occupancy is uncertain are not adjusted (ambient settings excepted) until the room is occupied again.
- **Throttling is per light or group**, in memory: after a restart the first update may follow sooner than the interval.
- **Manual mode is recognised on Main Mood only.** While Manual Mood is `Manual` but Main Mood has not followed yet, lighting still follows Main Mood.

### From Phase 0.3.1

- **No vacancy shutdown and no periodic correction.** A room's lights are not switched off when it becomes vacant, and lights changed by other automations are not corrected. That is Phase 0.4.
- **Conflict detection reads automation actions.** It sees direct, group, device, area, floor and label targets, scenes and scripts (three levels). Templated targets, blueprints with templated inputs, Node-RED and AppDaemon are invisible.
- **Manual changes without any context** (directly bound Zigbee switches, Hue dimmers) cannot be attributed. Configure the switch's action/event entity as a *wall switch* in the room, or choose *Treat as a manual change* globally.
- **Groups that hide their members** (Zigbee2MQTT, Hue) cannot be de-duplicated automatically; a warning asks you to exclude one side.
- **Room and light overrides do not follow mood inheritance**; only global profiles inherit from parent and fallback moods.
- **Colour names are approximate**: summaries name the nearest of eight swatches (Amber, Orange, Red, Pink, Purple, Blue, Cyan, Green), or show RGB values.
- **Changing light types reloads the integration** (they are part of the options). Nothing is applied on reload.
- **The cards are in English only.** The options-flow strings are translatable.
- **"Already in this state" is approximate**: brightness ±2 %, colour temperature ±60 K. Colour-only lights given a colour temperature are compared with the last command instead.

### From Phase 0.2

- **Confidence is not a probability.** It describes the quality of the current evidence, not how often the engine is right.
- **Non-binary PC sensors.** States are understood as active/inactive by a fixed list. Numeric sensors, such as idle seconds, are `unknown`: wrap them in a template binary sensor.
- **Door sequences need the door to change state.** Adjacency needs the adjacent room to be enabled.
- **The comparison counters are cumulative.** Open episodes are not restored after a restart, and there is no reset button yet.

### From Phase 0.1

- **The PIR, radar and bed split is only a suggestion.** Device class alone can't tell these apart when all of them report `occupancy`.
- **One change per options run**, and dynamically created form fields can't be translated.
- **Problems don't appear in Repairs.** They show up in the entities, the cards, the log and the diagnostics.
- **Recreated entities are not remapped**, and **area renames reload the integration**.

## Next-phase readiness

| Area | Ready now | Next step (Phase 0.4) |
|---|---|---|
| Occupancy | Engine per room with explainable states, evidence and helper comparison | Vacancy-based shutdown, using the same results |
| Lighting | Plans per room with a reason for every light; per-room activation; manual overrides with expiry; conflict detection; daylight-aware switch-on with recorded reasons | Light supervision: compare plans with actual states periodically, correct misfires, expand override handling |
| Configuration | Lighting store (versioned) owned by the integration; websocket API with admin-only writes | Add fields with defaults and a store migration |
| Dashboard | Smart Lighting Profiles, Smart Room Lighting and Room Status cards (simple by default, Advanced on request) | Supervision views |
| Prediction | `OccupancyHistory` receives every transition (`NullHistory`) | Phase 0.5 learning |
| Safety | `assert_room_control_permitted()` gates every command; integration-wide mode stays Observe | Supervise / Full Control modes with their own tests |
