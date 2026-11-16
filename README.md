# Smart Occupancy & Lighting

A Home Assistant custom integration that will eventually provide intelligent occupancy detection, predictive room occupancy, mood-based lighting and lighting supervision.

**This is Phase 0.3.5: mood-based lighting with simple dashboard cards (Smart Lighting Profiles, Smart Room Lighting, Room Status), on top of the Phase 0.2 occupancy engine (see [CHANGELOG.md](CHANGELOG.md)).** Phase 0.3.5 adds **native wall switches and remotes** (Zigbee2MQTT, MQTT device triggers and event entities): assign a switch to a room, learn its buttons, give each button an action, and let presses count as manual changes that automatic lighting respects (see [Wall switches and remotes](#wall-switches-and-remotes-phase-035)). Phase 0.3.3 switches a room's mood lighting on when someone arrives **and it is dark**: indoor light sensors (protected against being lit by the room's own lamps), an optional outdoor sensor and the sun decide whether artificial light is needed. Phase 0.3.2 lets moods, light types and single lights follow your **existing global brightness and colour temperature adjustment**, and adds the **Manual** pause. The interface stays simple by default; everything else is under *Advanced*.

**0.3.8** adds what happens when a room becomes **vacant** (*Turn off*, a *Vacant mood*, or *Leave unchanged*; see [Vacancy](#vacancy-038)), a **Presence** tab with the room's occupancy sensors, their live states, a switch to use each one or not, and the presence settings as global defaults with room overrides (see [Presence tab](#presence-tab-038)), and compact icon tabs in the room card.

**0.3.9** adds **possible occupancy lighting**: a dim, temporary light at the first sign of someone (for example PIR motion) while occupancy is being confirmed, handed over to the normal lighting or switched off on confirmation, and removed again if occupancy is not confirmed (see [Possible occupancy](#possible-occupancy-039)). The room card's configuration sections moved into a **Settings** submenu (gear icon); the Overview keeps the Observing / Automatic control and every wall switch's Observing / Active control.

**0.4.0** adds **Occupied and Vacant lighting profiles**. The mood profiles you already have are the room's *Occupied* profile. Each room (and the global defaults) can also have a *Vacant* profile per mood: what each light type or single light does while the room is empty, for example the LEGO cabinet at 20 % and the floor lamps off in *Kvøld*. It re-applies when the mood or the daylight changes while the room stays empty, and goes straight from one profile to the other without switching everything off first. Editing a profile no longer changes any light: changes are used at the next trigger, or when you explicitly **Test** a profile (see [Occupied and Vacant profiles](#occupied-and-vacant-profiles-040)).

**0.5.0 (Phase 4)** adds an **occupancy forecast**: learned from your Recorder history, per room, 5–60 minutes ahead, with confidence, explanations, movement between rooms and measured accuracy, in **observation mode only**: no forecast ever controls anything (see [Occupancy forecast](#occupancy-forecast-phase-4-050)).

**0.6.0 (Phase 4.5)** adds **multi-zone lighting and the Room Supervisor**: optional zones inside a room (a sink and a shower, each with its own sensors and lights, the shower taking precedence), one rule engine that gives every light exactly one winning rule, shared open-plan spaces (Køkur / Stova), switches explicitly designated as relay-controlled lights, and a Room Supervisor that compares what each light should do with what Home Assistant reports and, only when you allow it, corrects devices that did not follow a command (see [Multi-zone lighting and the Room Supervisor](#multi-zone-lighting-and-the-room-supervisor-phase-45-060)).

**0.6.1 (Phase 4.6.0)** adds a **window and curtain manager**: windows per room with one or more cover layers (blackout, semitransparent / sheer, shade, custom), a direction, an editable preset library per window, and **explicitly confirmed manual preset control** with per-cover verification (see [Curtains](#curtains-phase-460-061)).

**0.6.2 (Phase 4.6.1)** adds **autonomous curtain control per window**. A window you switch to **Automatic** (after reviewing its intended preset, safety conditions and conflicts) decides on its own between Sleep, Night, Evening privacy, Waiting for daylight, Sun protection, Window lamp, Morning and Day, and moves all its covers together without asking again. Measured outdoor light, not the clock, opens a window; sleep keeps sleep-protected rooms closed; manual moves are kept; every decision is explained. Every other window stays in Observing until you switch it (see [Automatic curtains](#automatic-curtains-phase-461-062)).

**0.6.3 (Phase 4.6.2)** adds **solar and thermal curtain control** and **24-hour time everywhere**. Each window judges direct sunshine from the sun's geometry against its own direction (with obstructions and an optional overhang) *and* measured outdoor light; stuck, implausible, stale or missing sensors are recognised and never open or shade a window. Optional, per window: temperature-aware sun (a cold room keeps the sun's warmth, a warm room gets sun protection, a too-warm room a stronger preset), glare-sensitive windows, and weather evidence for windows without a light sensor. Every new behaviour is off until you switch it on, except stuck-sensor detection, which can only keep a window where it is; you see what a new feature would do before it is saved. Every time shown by the cards is 24-hour `HH:mm` in Home Assistant's time zone (see [Solar and thermal curtains](#solar-and-thermal-curtains-phase-462-063)).

**0.6.4 (Phase 4.6.3)** adds **Global Curtain Modes**: Curtains → Global Modes shows Morning, Daytime, Evening, Night, Sleep and Sun Protection (Window Lamp, Glare and Excessive Heat under Advanced) and, for each, which preset every window or single cover uses when the automatic controller selects that mode for it. It is not a second scheduler: each window still decides its own mode, and all its safety rules apply. A mode without an assignment leaves the covers where they are (see [Global Curtain Modes](#global-curtain-modes-phase-463-064)).

**0.6.5 (Phase 4.6.4)** adds the **Global Curtain Schedule**: Curtains → Global Schedule sets when Morning, Daytime, Evening and Night begin for the whole house (fixed times, sunset or measured dusk, “whichever comes first / last”, normal and free days, late evenings, per-day times), with the household phase now, the next transition and a daily timeline. A phase only makes a mode eligible: sleep, missing daylight and every other protection still apply, and each window still decides on its own (see [Global Curtain Schedule](#global-curtain-schedule-phase-464-065)).

Every room starts in **Observe** mode. In Observe the integration plans and previews mood lighting, but sends no light command. Mood lighting runs only in rooms where an administrator explicitly enabled it in the Smart Room Lighting card (the pilot is Kamar Norður). The integration never:

- changes, disables or triggers existing automations, scripts or scenes (a wall switch button you explicitly assigned to a scene or script activates it)
- changes the mood selectors or any other helper (they are only *read*), except when you press a wall switch button that you assigned to *Select mood (all rooms)*: that selects the option on your Manual Mood selector. Main Mood is never written
- takes over a wall switch, changes Zigbee bindings, or publishes MQTT messages
- controls lights in a room that was not explicitly activated
- switches lights off because a room became vacant, unless the room is in **Automatic** and its vacancy behaviour is *Turn off* or the *Vacant profile* (0.3.8, 0.4.0); ambient lights are never switched off by vacancy
- switches a light on in an empty room, unless the room is in **Automatic**, its vacancy behaviour is the *Vacant profile*, and that profile switches the light on in the current mood (only when it is dark, unless you made that light an exception) (0.4.0)
- changes a light because you edited a profile; only an explicit, confirmed **Test** sends commands (0.4.0)
- switches a light on for *possible* occupancy, unless the room is in **Automatic** and that light was chosen as its preliminary light (0.3.9); it switches off only a preliminary light it switched on itself and nobody changed since
- switches lights off, or changes their brightness or colour, because of daylight or a lux reading, except a light that a room's Vacant profile wants *only when dark*: in an empty room it is switched off again when daylight is enough (0.4.0)
- switches a light on because the global brightness or colour temperature changed
- sends any automatic lighting command while Main Mood is **Manual**
- calculates its own adaptive lighting: it only reads the values your existing system publishes
- polls, uses the internet, or writes to the Recorder database directly (0.5.0 reads the Recorder's history, read only)
- controls a light, heating or any other device because of an occupancy forecast, or changes an occupancy state because of one (0.5.0: forecasts are observation only)
- treats a `switch.*` entity as a light unless you ticked it as a relay lighting output of a room (0.6.0)
- sends a corrective command unless the room's Room Supervisor is set to **Automatic** (default: Observing) and the room is in Automatic; it never corrects a person's change and corrects another automation's change at most once, only if you allow it (0.6.0)
- activates a zone from a forecast: zones follow sensor states only (0.6.0)
- moves a cover unless an administrator applied a preset in the Curtains card and confirmed the listed targets (0.6.1), or the window was explicitly switched to **Automatic** (0.6.2); never in Observing or Off, never during the first two minutes after a start, never at discovery or a configuration change of an Observing window, never replays an interrupted movement, and never because of a lighting mood
- opens a curtain in an Automatic window while sleep is on in a sleep-protected room, or without measured daylight (unless you explicitly allow the sun fallback for a window without a light sensor); a clock time can only delay an opening (0.6.2)
- undoes a manual cover move: it is kept until its release policy ends it or you press **Resume** (0.6.2)
- changes, disables or deletes a competing curtain automation: it is listed, and Automatic is refused for its covers until you disable it or explicitly allow it (0.6.2)
- treats a stuck, implausible, stale or missing light reading as sunshine or daylight, or lets a weather forecast override a working light sensor (0.6.3)
- lets temperature, glare or solar heating override sleep, night, evening privacy or a manual hold, or controls heating equipment (0.6.3: a thermostat is only read)
- shows a time in 12-hour notation (0.6.3)
- moves a cover because a Global Modes card was selected or a mapping was saved; an Automatic window whose current target changes is moved only after the change was reviewed and confirmed, and then only through its normal safety checks (0.6.4)

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
- a light that was offline during an application comes back

Editing a profile sends nothing (0.4.0): the change is used at the next of these triggers, or when you **Test** the profile.

When the room **becomes vacant**, its vacancy action runs once (see [Vacancy](#vacancy-038)). With the *Vacant profile* it runs again whenever the mood or the daylight changes while the room stays empty.

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

**Smart Room Lighting** (`custom:smart-occupancy-room-lighting-card`, pick the room in the visual editor). The main bar has compact icons for **Overview · Log · Lights · Overrides · Preview** and a **Settings** gear (0.3.9). Settings opens a second row with **Automatic · Presence · Switches · Advanced**, each with its icon and name, the selected one highlighted, and a **Back** button to the main sections. You can move between the settings sections without returning to the Overview, and the gear reopens the last one. Only **Overview** is open by default. A main tab's name appears as a tooltip on hover and on keyboard focus, is read by screen readers, and is written beside the selected icon (below the icons on narrow cards). Arrow keys, Home and End move within a bar; in the Settings row, Escape returns to the gear. Both bars wrap rather than hiding anything.

- **Overview**: the room's **Observing / Automatic** control (double-click the status, or use the selector), the last decision, occupancy now (with a running possible occupancy stage), mood, possible occupancy, when vacant, health, managed lights, last applied, the daylight decision in one line, every wall switch with its **Observing / Active** control, then one line per issue: a group question with its three buttons, "2 lighting conflicts · Review", short health lines.
- **Lights**: one line per light type ("Spottar · 6 lights · follows global · Off"); tap to see its lights, or tick *Show every light*. *Managed* toggles exclusion.
- **Settings → Automatic**: the room's **operating mode** first (see [Operating modes](#operating-modes-036)), then automatic switch-on when occupied and dark (see [Daylight](#daylight-aware-automatic-lighting-phase-033)), then **Possible occupancy** (see [Possible occupancy](#possible-occupancy-039)), then **When the room becomes vacant** (see [Vacancy](#vacancy-038)), with the daylight details collapsed under *Advanced daylight settings*.
- **Settings → Presence**: the room's occupancy sensors, their live states, a *Use* switch per sensor, and the presence settings (see [Presence tab](#presence-tab-038)).
- **Settings → Switches**: the room's wall switches and remotes, their buttons and actions (see [Wall switches](#wall-switches-and-remotes-phase-035)).
- **Overrides**: room mood; per type *follows global* or *overridden* (tap to override, the same editor with *Follow global*); single-light overrides.
- **Preview**: what this room would do in a mood.
- **Settings → Advanced**: activation with the full review (Observing / Automatic), automations that also control the lights (allow both or leave them alone), group decisions, manual overrides, wall-switch entities, discovery, last decision and the full diagnostics.

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

### Decision log (0.3.7)

Smart Occupancy records why it did (or did not) do something, while it decides. You never need Home Assistant's traces or logs to find out.

- **Overview → Last decision**: what was decided, the result, one sentence why, and when. Example: *Apply Kvøld · Executed — Mood changed to Kvøld: Ceiling Lights and Kitchen Lights switched on (Kvøld).* Before the first decision: *No decisions recorded yet.*
- **Log tab** (Overview · **Log** · Lights · …): the room's last 10 decisions, newest first. Select an entry (mouse, or Tab + Enter) for its details: trigger, triggering entity and state, occupancy and its inputs, mood, daylight and light level, manual overrides, every light's decision, the requested service call, the result and any error. Only what the integration actually used is shown.
- **Switches → each switch card**: *Last decision* with its result, and **Log** with the switch's last 10 presses (button, assigned action, why, result).

Results (always a word with an icon, never colour alone): **Executed** (Home Assistant accepted the command and the lights reported the requested state), **Sent** / **Sent · not confirmed** (accepted, but the lights did not report the requested state within a few seconds; scenes and scripts cannot be confirmed), **Partly failed**, **Failed** (with Home Assistant's error), **Observed only** (Observing: what would have been done; nothing was sent), **Blocked** (a condition prevented it: manual override, Manual mood, automatic switch-on off, uncertain daylight, an automation conflict, an offline light), **No change needed**, **No action** (a button without an action), **Not done (setting)** (0.3.8: switched off by a setting, e.g. *Leave unchanged* when vacant), **Noted** (e.g. a person's change that automatic lighting now respects).

What is logged: lighting applied or evaluated for a visit, a mood change, a room mood, a profile change, Automatic being switched on, a light coming back; gradual updates of the global adjustment; manual overrides; a vacancy timeout that starts while lights are on (updated in place to *cancelled* or *expired*) and what the vacancy action did (0.3.8; *Not done (setting)* for Leave unchanged); a queued change dropped because Manual mood started; Observing being set; every switch press and learning session. Not logged: sensor updates that change no decision. Repeats are folded: occupancy confirmed after the first motion of the same visit adds nothing, and the same decision again (same result) updates the newest entry with a count (×3) instead of adding one; global adjustment updates are one entry, button repeats within a minute too.

The log is kept in the integration's existing state file (`.storage/smart_occupancy.lighting_state`, written with its usual delayed save; survives restarts and mode changes). It creates no entity, no helper and no Recorder history. Removing a switch removes its log.

### Vacancy (0.3.8)

**Automatic → When the room becomes vacant**, per room:

| Choice | What happens when the room becomes vacant |
|---|---|
| **Turn off** (default) | Lights that are on are switched off. |
| **Vacant profile** (0.4.0; *Vacant mood* before) | The room's [Vacant profile](#occupied-and-vacant-profiles-040) of the current mood is applied: per light type or light On (brightness, colour, transition), Off, or left unchanged. Lights it does not set are switched off or left unchanged. It runs again when the mood or the daylight changes while the room stays empty. |
| **Leave unchanged** | Nothing is changed. The log says so: *Lights left unchanged: room vacancy behavior is configured to Leave Unchanged.* |

- **When.** The action runs when the occupancy engine confirms vacancy, after the room's **vacancy timeout** (see [Presence tab](#presence-tab-038)). There is no extra timer. Presence that returns during the timeout cancels it: nothing is switched off, so nothing has to be switched on again. The log shows *Vacancy timeout started* and then *Vacancy cancelled* or *Vacancy timeout expired*.
- **Protections.** Only in **Automatic**: Observing logs what would happen, Manual mood blocks it. **Ambient** lights keep following the mood. Exclusions, groups and unacknowledged automation conflicts apply as for every mood command. Without an effective mood nothing is switched off (ambient lights could not be told apart).
- **Manual overrides.** An override made while the room was occupied ends with the vacancy (as before), so that light is switched off too. An override made after the room went quiet (someone changed a light in the empty room, e.g. from the app) is respected until it expires. If the room is still vacant then, the vacancy action runs for that light.
- **Unavailable lights** are reported (*Partly failed*, *Failed*) and handled when they come back, if the room is still vacant.
- Switching a vacant room to Automatic switches nothing off. The action runs when a room *becomes* vacant.

### Occupied and Vacant profiles (0.4.0)

The mood profiles of earlier releases are the **Occupied** profile. The **Vacant** profile is what a room's lights do while it is empty and its vacancy behaviour is *Vacant profile*. Both are edited the same way, per mood, in the **Overrides** tab of the room card and in the **Smart Lighting** card (global defaults): pick **Occupied** or **Vacant** above the light types.

Example, Stova · Vacant · Kvøld:

| Light | Action | Brightness | Temperature |
|---|---|---|---|
| Floor lamps | Off | — | — |
| LEGO cabinet | On | 20 % | 2200 K |
| Dining lights | Off (not set: *Lights it does not set* = Turn off) | — | — |

- **Per light.** Each light type or single light is *On* (brightness, colour temperature, RGB colour or effect where the light supports it, transition), *Off*, *Leave unchanged*, or not set. Only the capabilities the lights have are offered. Groups work as everywhere else. A light can still be removed from automatic lighting entirely in the Lights tab.
- **Lights it does not set** follow the room's setting under *Settings → Automatic → When the room becomes vacant*: **Turn off** (default) or **Leave unchanged**, as a room value or the global default. A room that used the 0.3.8 *Vacant mood* keeps exactly its old behaviour after the upgrade: the lights it does not set get that mood's normal settings while they are on (*Like a mood*). Set them in the Vacant profile, or choose *Turn off* or *Leave unchanged*, to change that.
- **Inheritance.** Light (mood, then *every mood*) → room (mood, then *every mood*) → global (the mood, its parent and fallback moods, then *every mood*). Each value shows where it comes from: *overridden* for a room value, *from global Kvøld* for an inherited one. *Use the inherited settings* removes a room's values for one mood. A Vacant value never comes from the Occupied profile. *Ambient* belongs to the Occupied profile: an ambient light keeps following the Occupied profile in an empty room, so the two profiles never control the same light.
- **Daylight.** By default a profile switches an off light on only when the room needs light, by the room's daylight rules (sensor, outdoor light, sun, fallback when a reading is unknown, unavailable or stale). Per light type or light, **Switch on: Also in daylight** makes an exception, for example a window lamp. The Vacant profile can have its own **light level** (room value or global default; empty: the room's). It keeps the room's hysteresis (back above twice the level, by default, counts as daylight) and stability period, so it does not flicker around the threshold. While the room is empty a light the Vacant profile wants *only when dark* is switched on when it becomes dark and off again when daylight is enough. In an occupied room nothing is switched off because of daylight.
- **Mood changes.** When Main Mood (or the room mood) changes, an empty room gets the new mood's Vacant profile right away, without waiting for a sensor event. Identical commands are not sent again.
- **Transitions.** Occupied → vacant (after the vacancy timeout) and vacant → occupied (on confirmed occupancy) set every light directly to its new setting. A light that is on in both is adjusted, never switched off and on again. A queued change of the other profile finds the room changed and does nothing.
- **Manual changes** are respected as everywhere: a light a person changed in the empty room is left alone until the override ends.
- **Copy, reset, preview and test** (under the light types): *Copy from* copies another mood or the other profile into the one you are editing (for example *Occupied · Kvøld* into *Vacant · Kvøld* as a starting point). It copies what the source gives each light type of this room now, global and room values together, plus single-light values. *Use the inherited settings* removes the room's values. *Preview* shows the plan as if the room were occupied or vacant and never changes a light. **Test on the lights…** asks first, then sends the profile to the room's lights now, whatever the occupancy and daylight, also in Observing and in Manual mood. The tested lights are then a manual change (for the room's manual-override time), so automatic lighting leaves them alone. *Resume automatic lighting for these lights* ends that. The Preview tab has the same profile choice and Test button.
- **Log.** *Apply Vacant profile (Kvøld)* entries say what was switched on, off or adjusted and why: room vacant, mood changed while the room is vacant, it became dark, daylight is enough. *Test vacant profile Kvøld* records a test. The diagnostics show the effective Vacant settings, whether the Vacant profile is in charge now, and its daylight decision (`lighting.rooms.<room>.vacancy`).
- **Existing automations** that also control a room's lights are never disabled or changed. They are reported as conflicts (Advanced → activation review), and those lights are left alone until you acknowledge them.

### Possible occupancy (0.3.9)

**Settings → Automatic → Possible occupancy**, per room. The occupancy engine already distinguishes **possible occupancy** (new, uncorroborated evidence: one PIR event, a presence sensor that has just turned on, a door movement) from **confirmed occupancy** (a second signal agrees, or the evidence lasts for the *occupancy confirmation* window, 10 s by default). This setting gives the possible stage its own dim light:

1. A selected signal reports the first evidence (default: the room's motion sensors with the *Trigger* role; tick radar, PC activity or a door to use them too). The room is in possible occupancy. **The selected signals cannot confirm occupancy by themselves** (0.3.11): not by staying active, and not together with each other. Another signal of the room must agree (for Kamar Norður: the radar, the Nobara PC or the door). This matters because a PIR such as the Aqara reports "occupied" for about 60 s after a single movement. Without the rule, that one movement would count as confirmed occupancy after the 10 s confirmation window, and the preliminary light could never expire. The card lists the signals that can confirm, and warns when there is none. Rooms without a preliminary light keep the previous rules.
2. Each **preliminary light** that is **off** is switched on at its own brightness (default 10 %), optional colour temperature and transition. A light that is already on is left alone. The normal lighting waits for confirmation; in a room with a preliminary light this replaces switching on at the first motion.
3. **Confirmed**: the normal occupied lighting is applied first. Then each preliminary light is either **handed over** (the occupied scene decides its final level; a light the scene does not control stays on and the vacancy action handles it later) or **switched off**. The occupied scene has precedence: a preliminary light that the scene switches on keeps the scene's setting and is never switched off. *Turn off* switches the preliminary light off only once another light of the room *reports* on. It waits up to 10 s (plus transition) for the normal lighting to report on, and keeps the preliminary light on if nothing does, so the room is not left dark (0.3.13).
4. **Not confirmed**: after the **timeout** (default 30 s, counted from the start; more motion does not extend it), or as soon as the room is vacant again, the preliminary lights are switched off. Only lights this stage switched on are switched off: a light that was already on is never touched. A light is left alone only if a person's change was recorded: a manual override (app, wall switch, or an unattributed change when unattributed changes are treated as manual). Device reports, brightness corrections and automations do not count (0.3.11). A light whose switch-on call reported an error is still switched off if it came on.
5. **Never forgotten** (0.3.10): if the switch-off cannot be sent now, it stays due and is sent as soon as possible. That covers the room being *Observing* (nothing is ever sent in Observing; it runs when the room is set to Automatic again), Main Mood being *Manual* (it runs when Manual ends), an unavailable light (when it is back), or a Home Assistant error (retried every minute, up to 5 times; the vacancy action remains the fallback). It is kept across restarts and dropped once occupancy is confirmed, because the light then belongs to the room. The card shows *Still to switch off: …*.

A room that confirms occupancy at once (two sensors together) skips the stage: the normal lighting is applied directly. The stage runs once per visit.

- **Daylight.** The room's own daylight rules apply: its automatic switch-on policy, light sensor, and fallback when the reading is unknown, stale or unavailable (such a reading is never taken as darkness). By default the normal threshold applies too. A **light level** of its own (room, or a global default) can be set for the preliminary light only. The decision is taken when the stage starts, and again if the room becomes dark during it. Nothing is switched off because of daylight. On confirmation, a preliminary light that the normal lighting would not switch on (daylight is enough at the normal threshold) is switched off.
- **Manual changes.** A preliminary light with a manual override is not switched. If a person switched any light of the room off (an active override), no preliminary light comes on.
- **Modes.** *Observing* evaluates and logs what would happen; nothing is sent. *Automatic* switches. Main Mood = *Manual* sends nothing. Without an effective mood nothing changes.
- **Defaults and overrides.** *Global defaults (all rooms)*: enabled, brightness, transition, timeout, light level, and what happens on confirmation. A room value is marked *Room override* (or *this light*), with *Use global default*. Changing a global default never changes a room's own values. A room changes nothing until it has a preliminary light, so enabling the global default alone does not change any room.
- **Log.** *Preliminary light on* (or *Keep preliminary light off*: daylight, uncertain daylight, manual override, Manual mood, unavailable light), with the timeout deadline. On confirmation, *Preliminary light handed over* / *Preliminary light off*, noting that the timeout was cancelled. When possible occupancy was not confirmed, *Preliminary light off* gives each light's cleanup result: switched off (verified), failed, already off, left because someone changed it, or blocked (Observing, Manual) with when it will be done. Retries are logged too. Every entry says why and has the details: evidence, light level, deadline, settings and the commands sent.
- **Restart.** A stage that was running when Home Assistant stopped still ends: its lights are switched off at the original deadline unless the room is occupied by then. A switch-off that was due is tried again after the restart.

**Diagnostics** (0.3.11): *Download diagnostics* (Settings → Devices & services → Smart Occupancy & Lighting) contains, per room, `lighting.rooms.<room>.preliminary.trace`. That is a step-by-step record of the last stages, kept in memory and lost at restart: occupancy before, the triggering signal, each light's state, the activation calls and their results, the deadline, the timeout firing, the cleanup decision, the `light.turn_off` calls with their results and the state afterwards, and any command that switches a light on again after the cleanup. `occupancy.<room>.preliminary_signals` shows the signals that cannot confirm alone. The same steps are written at debug level as `preliminary_trace` (`logger: custom_components.smart_occupancy.controller: debug`).

Not included: movement in an *adjacent* room as preliminary evidence. The engine uses it only as exit evidence, and using it would switch on a light in a room nobody has entered.

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

A light changed by a person is respected for a while (default 60 min; per room or globally; 0 = until the room is vacant). Overrides are cleared when the room becomes vacant, or from the card. One made after the room went quiet is kept until it expires (see [Vacancy](#vacancy-038)). How a change is classified:

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

### Presence tab (0.3.8)

The room card's **Presence** tab shows, for every signal assigned to the room:
- its name, entity id, kind and role (roles and assignments stay in *Configure → Rooms*);
- its current state, labelled by device class (*Detected / Clear*, *Open / Closed*, *On / Off*), with the raw state beside it. The engine decides on the raw state (`on`, `off`, `unknown`, `unavailable`), never on the label;
- what it contributes now: **Active**, **Clear**, **Not used here** (excluded), **Unavailable**, **Unexpected state** (available, but its state is neither active nor clear, so it is not evidence), **Stuck**;
- when it last changed;
- a **Use** switch. Off excludes the signal from this room's occupancy only. Its assignment, kind and role are kept, so switching it on restores it exactly. The entity, other rooms and automations are not changed, and its state is still shown.

Below are the room's **presence settings** (the timing below; *Vacancy timeout* is the inactivity timeout). Each is marked **Global default** or **Room override**:
- typing a value overrides it for this room;
- **Use global default** returns one setting to the global value; **Use global defaults for all** returns every setting;
- **Global defaults (all rooms)** edits the global values. Rooms that follow a global value change with it; room overrides stay.

Changes apply at once without a reload or restart. The engines keep every signal's value and re-evaluate, so a pending vacancy uses the new timeout and is not restarted. Values are validated before saving (the ranges below; *stuck after* is 0 or at least 60 s). They are stored in the integration's existing options, the same values as *Configure → Occupancy timing* and *Configure → Rooms → Occupancy*.

### Timing

Every value can be set globally and overridden per room (in the Presence tab, or in *Configure*).

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

## Multi-zone lighting and the Room Supervisor (Phase 4.5, 0.6.0)

Optional and per room. A room without zones, relay outputs or a shared space behaves exactly as in 0.5.0, and the Room Supervisor starts in **Observing**: it reports, it never sends a command until you set it to Automatic.

### Audit (before implementation)

What existed and was extended (nothing was duplicated):

| Part | Before | 0.6.0 |
|---|---|---|
| Occupancy engine (`engine.py`, `occupancy_manager.py`) | one engine per room | unchanged rules; each **zone** runs the same engine over its subset of the room's signals (`zone_runtime.py`); an occupied zone can **hold** its room occupied (`RoomEngine.set_hold`) |
| Lighting controller (`controller.py`) | Occupied plan, vacancy plan, preliminary plan, gradual updates, wall switches | every plan passes **one arbitration step** (`arbitration.py`) before it is executed or shown; zone changes are applied by `async_zones`; commands go to `light.*` or `switch.*` by domain |
| Planner (`planner.py`) | per-light decisions | each item names its winning **rule**; lights a zone switched on count as off for the daylight rules once the zone ends |
| Profiles (`profiles.py`), Vacant profile, possible occupancy | — | unchanged; they are rules 5–7 of the arbitration |
| Light index (`lighting.py`) | `light.*` only | also indexes the switches you designated as relay outputs (labels, categories) |
| "Supervision" (`supervision.py`) | permission gates only (Observe / Automatic / Active switch) | unchanged; the new **Room Supervisor** (`supervisor.py`) uses those gates and the controller's own plan |
| Decision log (`decisions.py`, `lighting_decisions.py`) | per room and per switch | zone decisions in the room log; supervisor decisions in their own log per room (`<area>#supervisor`) |
| Room relationships | adjacent rooms (Phase 0.2), room connections (Phase 4) | adjacent rooms are suggested when you create a **shared space** |
| Lighting store | 1.6 | 1.7: `zones`, `relays`, `supervisor` per room; global `supervisor`, `shared_spaces` |

### Zones

Settings → **Presence** → **Zones**. A zone is a logical part of a room (sink, shower, kitchen island); Home Assistant's areas are not changed. For each zone:

- **Presence sensors**: some of the room's own sensors (kind, role and "inverted" come from the room; a sensor you switched off in the Presence tab is off in its zones too).
- **Occupied**: at the *first detection* (one PIR movement) or *when presence is confirmed*.
- **Timing** (blank = the room's value): confirmation window, *hold after the last detection* (the zone stays occupied that long after its sensors went quiet, so a brief interruption never dims the shower), exit confirmation, dropout grace.
- **If its sensors are unavailable**: *keep its last state* (default), *treat as vacant*, or *occupied while the room is occupied*. Unavailable is never simply taken as vacant, and an unavailable zone never holds the room occupied.
- **Keeps the room occupied**: while the zone is occupied the room is not declared vacant (the room's quiet period starts when the zone ends). A zone becoming vacant never makes the room vacant; the room is still decided by its own sensors.
- **Lights**: any light or relay output the room controls, with **On** (brightness, colour temperature, transition, only what the light supports), **Off**, or **Unchanged** (the zone claims the light and keeps it as it is; lower rules do not change it).
- **Priority** (1–100, higher wins between zones), **Manual changes** (*respected*, or *zone wins over earlier ones*), **Switch on in daylight** (*only when the room needs light*, *always*, or *below its own light level*), **When the zone ends** (*room rules take over*: a light the zone switched on that no remaining rule sets is switched off; or *lights stay as they are*).

Zones are active from sensor states only. A zone is never active while its room is vacant, and a forecast or *predicted* occupancy never activates one.

**WC Neðra example.** *Sink*: sink PIR, priority 40, downlights 30 %, mirror light on. *Shower*: shower radar, priority 80, hold 3 min, downlights 100 %, *Switch on in daylight: always*, optionally *zone wins over earlier ones*. Someone at the sink: downlights 30 %, mirror on. Steps into the shower: downlights 100 % (the shower wins; the mirror stays on, the sink still claims it). A mood change meanwhile changes nothing the shower controls. Leaves the shower but stays at the sink: after the hold, downlights back to 30 %. Leaves the room: the room's vacancy action.

### The lighting rule engine (one winning rule per light)

For every light the rule engine decides before anything is sent, in this order:

1. **Protection**: Manual mood (everything paused), an unavailable device, an excluded light, a member controlled through its group, an unacknowledged automation conflict.
2. **Zones set to *zone wins over earlier ones***: they win over a manual override made *before* the zone became active. A change a person makes while the zone is active is respected.
3. **Manual overrides**.
4. **Zones** (higher priority first, then the order in the list).
5. **Occupied profile** (the room's mood).
6. **Possible occupancy** (preliminary lights that no zone claims).
7. **Vacant profile** / vacancy action.

Each light gets at most one command, only when it is not already in the requested state; the decision log and the Supervisor show the winning rule and what it took precedence over ("Downlights set to 100 %, overriding Sink zone"). A zone can only target lights the room controls; a member of a managed group is controlled through the group, so a group and its members never get contradictory commands.

### Shared (open-plan) spaces

Settings → Presence → **Shared space**: group rooms such as Køkur and Stova. Each room keeps its own occupancy, zones, lights and daylight rules. The space adds:

- a **shared mood** (optional; rooms without their own room mood use it),
- **shared lights**: a light managed by two member rooms is never switched off by one room while the other is occupied, and is commanded by one room at a time (the first occupied room in the space's list),
- **When a room is vacant**: *it runs its own vacancy action* (default) or *it waits until the whole space is vacant*.

### Relay-controlled lights

Settings → **Advanced** → **Relay lighting outputs**: tick the `switch.*` entities that switch a light. Only those are lights; no switch is ever treated as a light automatically, and wall switches, buttons and switch inputs stay what they are. A relay output takes part in everything a light does (Occupied and Vacant profiles per single light or by its labels, possible occupancy, vacancy, zones, manual overrides, the Supervisor), with on/off only: it is switched with `switch.turn_on` / `switch.turn_off`, never with light attributes, and the editors show only on/off for it. Automations that call `switch.*` on it are found as conflicts.

### Room Supervisor

Settings → Advanced → **Room Supervisor**: *Observing* (default), *Automatic* or *Off*, per room or as the global default; the Overview shows its status.

It compares the **desired state** (what the winning rule requests) with the **actual state** (what Home Assistant reports; the physical output may differ), for the attributes the device supports, with tolerances (brightness ±5 %, colour temperature ±150 K by default) and after a verification delay plus the command's transition. It never applies new lighting itself, so it changes no room's behaviour; it checks what Smart Occupancy commanded:

- a command that was accepted but not reported, or that failed, while the rule still wants it: **mismatch**; in Automatic it is **corrected** (at most 3 times per mismatch: the first once the verification delay has passed, then after 5 and 10 s, each delay doubling; at most 6 times per device per hour; never while a command or transition is pending or a lighting change is queued);
- the removal of a preliminary light, or of a light a zone switched on, is verified the same way;
- a **manual change** is never corrected;
- a change by **an automation or another integration** is reported (*Changed elsewhere*); with *Changes by automations: correct once* it is corrected once, and if it is changed again the supervisor reports **Conflict suspected** and stops;
- after the last allowed correction: **Device failure** (suspected: Home Assistant still reports the old state), left alone until the device changes, the rule changes, or the device comes back online.

**Timing**: command verification a few seconds after each command; a reconciliation of each room every 30 s; sensor and device health every 60 s; extended metrics every 5 min. One timer for all rooms; reconciliation only reads states (no periodic service call), and cards are refreshed only when something changed.

**Statuses**: Healthy · Observing · Waiting for verification · Mismatch detected · Correcting · Degraded sensor availability · Device failure · Conflict suspected · Not checked yet · Supervisor off. *Healthy* only when occupancy is known, every sensor and device is available and nothing differs.

**Overview → Room Supervisor**: status, mode, active zones, discrepancies, last successful check, sensor and device health, and on demand each light's winning rule, expected and actual state, last command, verification, correction and error, plus the supervisor log.

### Decision log examples

- *Shower zone activated. light.wc_downlights set to 100 %, overriding Sink zone.* (room log)
- *Shower zone ended. light.wc_downlights set to 30 %, …* (room log)
- *WC Neðra: light.wc_downlights expected 100 %, actual 30 % (Shower zone). Correction requested.* (supervisor log)
- *WC Neðra: light.wc_downlights: expected 100 %, actual 30 %. Corrective light.turn_on issued (attempt 1 of 3).*
- *WC Neðra: light.wc_downlights verified at 100 %.* · *Room Supervisor: every device reports its requested state again. Supervisor healthy.*
- *Stova: switch.stova_relay: expected off, actual on. Corrective switch.turn_off issued (attempt 1 of 3).*
- *Room vacant, but Stova in the shared space Køkur/Stova is still occupied: this room's lights stay as they are until the whole space is vacant.*

Routine successful checks are counted (Overview metrics, diagnostics), not logged.

### Migrating a room from Home Assistant automations

Smart Occupancy never disables or changes your automations. Room by room:

1. **Read the existing automations** of the room (Settings → Automations): their triggers (sensors), conditions (lux, mood, time) and light actions. Settings → Advanced in the room card lists the automations that also control its lights.
2. **Recreate the behaviour as zones**: one zone per sensor area, with the same lights and levels; the shower's 100 % as a higher-priority zone; lux conditions as *Switch on in daylight*.
3. **Compare in Observing**: leave the room's automatic lighting Observing. Each zone change is logged as *would* ("would be set to 100 %"), next to what your automation actually does. Settings → Presence → Zones → *Which rule controls each light now* shows the winning rule per light.
4. **Resolve the overlap**: when the zone rules match, disable the old automation yourself, or acknowledge it as a co-controller (Advanced) if both should run.
5. **Switch the room to Automatic**, then optionally the Supervisor to Automatic.

### Live-testing checklist

**WC Neðra**: (1) at the sink: downlights at the sink level, mirror on; (2) in the shower: downlights 100 %; (3) both: the shower wins (log: "overriding Sink zone"); (4) a short radar dropout in the shower does not dim; (5) leave the shower, stay at the sink: back to the sink level after the hold; (6) leave the room: the vacancy action; (7) unplug the shower radar: the fail-safe you chose (Presence → Zones shows "Sensor unavailable").

**Køkur / Stova**: (1) activity in the kitchen lights the kitchen only; (2) in the living room only the living room; (3) both occupied: each keeps its own levels, a shared light gets one command; (4) leave the kitchen: its own lights follow its vacancy action, a shared light stays on while Stova is occupied; (5) with *wait until the whole space is vacant*: the kitchen keeps its lights until Stova is vacant too.

**Relay output**: (1) tick the switch in Advanced → Relay lighting outputs; (2) give it a zone or a per-light profile entry: it is switched with `switch.turn_on/turn_off` only; (3) the Overview's supervisor table shows its expected and actual on/off; (4) with the Supervisor in Automatic, block the relay (or unplug it while on): the supervisor corrects at most 3 times, then reports *Device failure*; plug it back: verified again; (5) switch it by hand: *Manual change*, never corrected.

## Curtains (Phase 4.6.0, 0.6.1)

A window manager with adjustable presets, explicitly initiated manual control (0.6.1) and, per window, autonomous control (0.6.2, see [Automatic curtains](#automatic-curtains-phase-461-062)). Every window starts in **Observing**: it decides and explains, but nothing moves on its own.

### Add the card

**Edit dashboard → Add card → Smart Curtains** (`custom:smart-occupancy-curtains-card`). Optional settings: `title`, and `area` to show only one room's windows. No YAML is needed for anything else.

### Windows

- **Add window**: a name, a room (any Home Assistant area), a direction and its covers. **Suggestions** proposes windows from the covers no window uses yet: same area, same entity id apart from the layer word (`gardinur_kokur_vindeyga_ljost` + `…_myrk` = one window, `…_eystur_myrk` = another; `ljos`/`ljost` = sheer, `myrk` = blackout). Review corrects the name, room, covers and their types before anything is created.
- One room can have several windows, and each window has its own direction: covers in one room are never assumed to face the same way.
- **One cover belongs to one window.** A cover (or a cover group containing it, or a member of a group) already used by another window cannot be added.
- Windows, layers and presets have stable ids. Renaming a window, preset or cover entity keeps every preset and future rule pointing at the right thing.
- **Direction**: N, NE, E, SE, S, SW, W, NW, or an exact angle (0 = north, 90 = east, 180 = south).
- **Mode**: *Off* (not evaluated; presets cannot be applied), *Observing* (default; decides and explains, nothing moves on its own, a person may still apply a preset) or *Automatic* (moves on its own; switched on per window with its own review and confirmation).
- **Sleep mode**, **Evening privacy** and **Sun protection** are switched on and off per window (Automatic tab).

### Presets

Every window has its own preset library, stored separately from lighting moods (`.storage/smart_occupancy.curtains`).

| Window | Starting presets (every value editable) |
| --- | --- |
| One cover, or covers of one type | Fully open 100 %, Half open 50 %, Fully closed 0 % |
| Sheer + blackout (e.g. Stova Suður) | Fully open 100/100, Half shade 50/100, Full shade 0/100, Evening privacy 0/60, Full blackout 100/0 (sheer/blackout) |

Positions are percent **open** (0 % = closed). Create, rename, edit, duplicate and delete presets; set each cover independently (slider, number or 0/25/50/75/100) or **Leave unchanged**. Positions must be whole numbers from 0 to 100 %. Templates are created only for a window that has no presets yet; nothing existing is overwritten. A cover added later is *Leave unchanged* in the existing presets until you set it.

The card shows, per window, which preset the covers match now (within the position tolerance, default 3 points) or **Custom position**, and *Matches now* on each preset.

### Applying a preset

1. Choose the preset (the window's *selected* preset; selecting never moves anything).
2. **Preview** shows each cover's current position, target and what would happen: *Will move*, *Already there*, *Leave unchanged*, *Unavailable*, *Not supported* or *Busy*. Nothing is sent.
3. **Apply…** shows the same list and asks for confirmation. Only **Move N covers** sends commands, and only for exactly the targets you confirmed (if the preset changed in between, it is refused).

The integration then sends one command per cover, all together, as one operation: `cover.set_cover_position` when the cover supports positions, `cover.open_cover` / `cover.close_cover` for open/close-only covers (only 0 % or 100 %). A cover already within the tolerance gets no command. It then follows each cover's reported position until it is at its target (not moving), the verification time passes (default 120 s) or the cover becomes unavailable, and records a result per cover: *Reached*, *Already there*, *Left unchanged*, *Failed* (the command was rejected), *Timed out*, *Unavailable*, *Not supported*. The operation is *Succeeded*, *Already there*, *Partly succeeded*, *Timed out* or *Failed*. A cover in a running operation is busy: a second preset for it is refused rather than contradicting the first. Commands are never repeated.

An operation interrupted by a restart is shown as *Interrupted* and is **not** resumed or sent again.

### Manual changes

A position change Smart Occupancy did not command (the NSPanel, a device button, another automation, a person in Home Assistant) is shown as the window's **manual change**, with the time and whether a person in Home Assistant made it. Reconnects and restarts are not counted. Our own commands are recognised while the covers are busy in an operation and for 90 s after it (late Zigbee2MQTT reports), and after a restart for the rest of an interrupted movement. In Observing and Automatic a manual change becomes a **manual hold** (see below).

### Sun protection

- The window's direction, the current sun azimuth and elevation (`sun.sun`, or two sensors chosen under the card's ⚙ settings) and **Potentially exposed to direct sunshine** when the sun is above the window's minimum elevation (default 3°), within ± the exposure width (default 70°) of its direction, and not behind an **obstruction**.
- **Obstructions** model blocked sun, such as an east-facing window behind a hill: between two directions, the sun must be higher than a given elevation to reach the window.
- `sensor.sun_angle` (0° = the sun faces the south-facing windows) is shown as the house's calibrated reference. Its sign and formula are **not assumed**: the integration compares it with the standard azimuth over the day and reports the relation (for example *sun_angle ≈ azimuth − 113°*, and the azimuth at which it reads 0°) only once readings over at least 30° of sun movement fit within 2°. Until then it is *not validated*. It is never used to move a cover.
- Exposure is geometry only (no clouds or brightness). It never proves sunshine on its own: Automatic sun protection also needs measured strong light (see below).

### Diagnostics

Per window: actual versus desired (the automatic preset, or the selected preset) position per cover, state and supported features, the recognised preset, the manual change, the last command with each cover's service, reported position and result, earlier commands, and **other automations that may move these covers**. An automation is listed when its actions call a cover service (`cover.*`, `homeassistant.turn_*`, a scene or a script) on the cover, its group, device, area or label, directly or through a scene or script. Enabled ones are warnings on the window card. They are only listed, never changed, disabled or deleted. Templated targets cannot be detected. Home Assistant's integration diagnostics include a `curtains` section.

### Safe alongside the legacy curtain system

The legacy automations (`auto_gardinur`, `cover_*_sudur`, `inngongd_nspanel` …) keep working as before; the integration only reads their configuration to report them. `gardinur_state`, `input_boolean.sol` and the mood helpers are not read or written. A window cannot be switched to Automatic while an enabled automation may also move its covers (directly, through a group, device, area, label, scene, or a script, also a script called as an action); disable that automation yourself, or mark it in Diagnostics as *does not compete*. If such an automation is enabled later, the Automatic window stops sending and says why.

## Automatic curtains (Phase 4.6.1, 0.6.2)

### Enabling one window

1. Configure the window: its covers and presets, which preset each condition uses (Automatic tab), and the outdoor light sensor (Automatic tab → Daylight, usually for **All windows**).
2. General → Mode → **Automatic…** shows what would happen now: the condition, its preset, which covers move, anything that **must be resolved first** (an enabled competing automation, a missing cover, contradictory settings) and what is **good to know** (no light sensor, an unavailable sleep source, conditions without a preset, an active manual hold), plus the order of precedence.
3. **Enable Automatic**. From then on the window moves on its own, without confirmations. Other windows are not touched. Switch back to Observing or Off at any time.

### Conditions

Each window chooses one condition at a time and applies that condition's preset to **all its covers together** (one command per cover, sheer and blackout in one operation). Positions come only from the window's presets; nothing is hard-coded.

| Condition | When |
| --- | --- |
| **Sleep** | Sleep mode is on for the window and its sleep source (default `input_boolean.sleep`, or any input boolean / binary sensor) is on |
| **Night** | Inside the household night: before a workday 22:30–06:30, before a free day 23:30–08:00 (free days: Sat, Sun; all editable) |
| **Evening privacy** | Privacy is on and it is dark in the evening: outdoor light at or below 50 lx for 10 minutes, or the sun below −4° (works without a light sensor) |
| **Waiting for daylight** | Nothing above, but no evidence allows opening: daylight not confirmed, the light sensor stale or unavailable, or the sleep source unavailable. **The covers keep their position** |
| **Sun protection** | Sun protection is on, the sun is in front of the window (direction ± exposure width, above the minimum elevation, not behind an obstruction) **and** outdoor light has been at least 12 000 lx for 10 minutes; released below 8 000 lx for 20 minutes or when the sun leaves the sector (3° margin) |
| **Morning** | The first time daylight is confirmed before noon (after the night and after sleep): 120 minutes, or until a time of day (10:00 workdays, 11:00 free days) |
| **Normal daytime** | Daylight: at least 150 lx for 10 minutes and the sun at least −2° |

**Daylight is measured, not assumed.** A clock time only delays an opening (the end of the night); it never opens a window by itself, and never while sleep is on. Missing or stale evidence (no report for 30 minutes) never opens a window. Without a light sensor a window does not open automatically, unless you allow the conservative **sun fallback** (open once the sun is at least 3°). Night and evening closing work without a light sensor. After a restart the light history starts again, so daylight must be stable again before anything opens.

The 11-12 / 11-13 failure (curtains opened at 07:00 at −10.2°, 0 lx, sleep on) is replayed in the tests: Sleep keeps the bedroom closed, the other windows wait for daylight, and the morning starts only once sleep has ended *and* daylight is measured, e.g. *“Waiting for daylight. Sleep mode ended, but outdoor light is 12 lx (daylight needs 150 lx for 10 min). Morning preset not yet applied”*.

**When sleep ends**: start the morning once there is daylight (default), go straight to the current condition, or keep the sleep position until you resume. *Opening after sleep needs daylight* can be switched off for a window.

### Window lamp

Optional per window (Automatic → Window lamp): the lamp, its **Lamp On** preset, a release delay (default 120 s) and the conditions that win over the lamp (default Sleep, Night and Evening privacy; Sun protection can be added). While the lamp is on its preset holds; after it switches off and the delay passes, the window applies its **current** condition, never the position from before the lamp. A condition that wins over the lamp applies its own preset while the lamp stays on.

### Manual changes and Resume

A manual move (or a preset you apply yourself) is kept as a **manual hold**:

- *Until the next automatic phase* (default), e.g. a move during the day is kept until evening privacy, night or sleep;
- *For a number of minutes* (default 120);
- *Until the room has been empty* for a number of minutes (from the room's occupancy; a room without occupancy uses the next phase);
- *Until I resume*.

**Resume automatic curtains** (window card) releases the hold and applies the current condition at once; the next manual move starts a new hold. Holds survive restarts. Sleep, Night or Evening privacy can be set to end a hold, but only when they start *after* the manual move: a move made during sleep is respected.

### Precedence

1. Safety: unavailable or missing covers, a competing automation, a running movement, the movement limits
2. Manual hold (with the configured exceptions)
3. Sleep
4. Night, Evening privacy
5. Window lamp
6. Sun protection
7. Morning, Normal daytime
8. Waiting for daylight (keeps the position)

Conditions chosen to win over the lamp rank above it, the others below. The window's exact order is shown in the enable review and in Diagnostics.

### Movement safety

- Covers already within the position tolerance (dead band, default 3 points) get no command, and the same targets are never sent twice.
- A running movement is never contradicted; an unavailable or missing cover blocks the window until it reports again.
- **Minimum time between moves** (default 10 minutes) for Morning, Day and Sun protection; Sleep, Night, Evening privacy, the lamp, leaving them, Resume and retries are not delayed.
- **Automatic moves per day** (default 12); then only Sleep and Night still move.
- A failed or timed-out movement is retried at most twice (after 1 and 5 minutes), then reported and left until the condition changes or you resume.
- Late device reports are verified as before (default 120 s); a restart never replays an interrupted movement and sends nothing for the first two minutes.

### Settings: all windows, room, window

Every setting (Automatic tab → *Settings for*) can be set for **all windows**, for a **room**, or for **one window**; the first that sets it wins and each field shows where its value comes from. Groups: Sleep mode, Window lamp (per window), Daylight, Night and morning, Strong sunshine, Manual changes, Movement safety. Everything is edited in the card; no YAML.

### What the card shows

Each window shows Off / Observing / Automatic, the current condition and preset, why (the decision sentence), the status (*Moved*, *Would move*, *Already there*, *Prevented* with the reason, *Manual hold*, *Failed*), desired and actual positions of every cover, sleep, lamp, manual hold, outdoor light and sun, **Resume**, and the last movement with its verification. Diagnostics adds the evidence, precedence, moves today, next check and a decision log (40 entries, kept across restarts); Home Assistant's diagnostics include it under `curtains.automatic`.

### Limitations

- One outdoor light sensor per window (set once for all windows, or per room / window). Weather and temperature: see [Solar and thermal curtains](#solar-and-thermal-curtains-phase-462-063) (0.6.3).
- A morning starts only before noon; after a later wake-up the window goes to Normal daytime.
- A manual move reported within 90 s after an automatic movement of the same window is treated as part of that movement (Zigbee2MQTT reports late); it is then not corrected, but not held either.

## Solar and thermal curtains (Phase 4.6.2, 0.6.3)

Builds on the automatic windows above; nothing changes for a window until you switch a new option on.

### Direct sunshine: four separate questions

1. **Can the sun reach the glass?** (geometry) The standard solar azimuth and elevation from `sun.sun` against the window's direction: within ± the exposure width, above the minimum elevation (low winter sun), not behind an obstruction (e.g. east windows behind a hill need a higher morning sun) and, new, not above an optional **overhang** limit (eaves or a balcony shade the glass from a high summer sun). The card shows the sun's angle *from the window* and its incidence on the glass.
2. **How bright is it outside?** (measurement) The outdoor light sensor is the primary evidence.
3. **Is it direct sunshine?** Only *measured* strong light for its stable period confirms it; geometry alone never does, and a clock never does. Without a light sensor, and only if you choose it, a clear sky from the weather entity may confirm it for sun protection (never for opening). If that weather evidence disappears while the window shades, it keeps its position. A forecast never overrides a working light sensor.
4. **Glare or heat?** Optional, see below.

`sensor.sun_angle` is the house's compensated angle (0° = the sun faces the south-facing windows); it is **not** an azimuth. Once its calibration is validated, the card also shows each window's angle read through it, as a cross-check; it never drives a decision.

### Measurement validity

Every reading is **Valid**, **Not chosen**, **Not found**, **Unavailable**, **Stale** (no report for too long), **Appears stuck** (the same light value for 2 hours, adjustable, while it should vary; a constant dark reading at night is normal) or **Implausible** (e.g. thousands of lux while the sun is below the horizon, as `sensor.pir_taf_illuminance` read at dawn). Only valid readings count. A stuck or implausible sensor keeps the window where it is, with an explanation such as *“The outdoor illuminance sensor … appears stuck … Sun protection not activated because direct sunshine cannot be confirmed”*; once it reports real values again, daylight and sunshine need their full stable periods again. Stuck detection is the one new default that is on: it can only keep a window where it is.

The **sensor-quality summary** (gear on the overview) lists every candidate found by type and unit — outdoor and indoor light, outdoor and room temperature, weather, cloud cover, thermostats — with its validity now, and offers **Use for all windows**. Nothing is hard-coded; `sensor.pir_tfr_illuminance`, `sensor.min_ljos_terassa_framman` and `weather.forecast_home` are only candidates.

### Temperature-aware sun (optional, per window, room or all windows)

With a room temperature (a sensor or a thermostat's current temperature) the room is **Cold** (below the comfortable range), **Comfortable**, **Warm** (above it) or **Too warm** (above the maximum acceptable). A band changes only after the temperature has been in it for the stable period (10 min) and leaves it only past the hysteresis (0.5 °C). With direct sunshine:

| Room | Decision | Preset |
| --- | --- | --- |
| Too warm | Excess solar heat | its own, else Direct sunlight |
| Warm | Direct sunlight (sun protection) | Direct sunlight |
| Glare-sensitive window | Glare protection | its own, else Direct sunlight |
| Cold (warmth welcome, not warm outside) | Useful solar heating | keeps Morning / Normal daytime |
| Comfortable, or temperature unknown | Direct sunlight | Direct sunlight |

Settings: preferred temperature (or a thermostat's target, which is only read), comfortable ±, maximum acceptable, solar heat gain (welcome / not), “warm outside from” (outdoor temperature at which solar heating is not needed), hysteresis, stable period, glare-sensitive window, glare only while the room is occupied, and an optional indoor-light threshold for glare. The calculations are explained in words; no heat power is estimated.

### Precedence

Safety → manual hold → sleep → night → evening privacy → window lamp → excess solar heat → direct sunlight → glare → useful solar heating → morning → normal daytime → waiting. *Direct sunlight* in “wins over the lamp” also covers glare and excess heat. Nothing thermal ever bypasses sleep, night, privacy or a manual hold. The window's precedence list shows only the rows that are switched on.

### Switching a feature on

Switching on temperature-aware sun, a glare-sensitive window or weather evidence first shows, for every affected window, what it would decide now and how many covers would move (`auto/simulate`); it is saved only when you confirm.

### Testing in Observing

Leave the window in **Observing**, open its **Environment** tab, choose the sensors and check that each says *Valid*. Switch on temperature-aware sun (look at the prediction), then watch the overview's *Direct sun*, *Temperature* and *Glare / heat* rows and the decision log over a sunny day: each entry reads like *“11:35 — Direct sunlight detected (25000 lx). Room temperature 23.8 °C exceeds the configured comfort maximum (22.5 °C)”* with *Would move …*. Nothing moves until you enable Automatic.

### 24-hour time

Every time the cards show — curtains, lighting, presence, forecasts, the Room Supervisor, decision logs, manual-override expiry and the next check — is `HH:mm` (00:00–23:59) in Home Assistant's time zone (or your own, if your profile says so), never AM/PM and independent of the browser language. Time settings are text fields that accept `HH:mm` (also `7:30`, `0730`, `7.30`, stored as `07:30`) and refuse anything else; stored times and their meaning are unchanged. Timestamps stay ISO 8601 with a time zone.

### Storage

Curtain store minor version 3: window `solar.max_elevation`, condition mappings `glare` and `heat`, and the new settings (all defaults when absent). The state store adds `sensors` (when each light sensor's value last changed, so a restart does not hide a stuck sensor) and per window `thermal` and `last_phase`. 0.6.2 data loads unchanged.

## Global Curtain Modes (Phase 4.6.3, 0.6.4)

Open the Smart Curtains card and switch from **Windows** to **Global Modes** (or add the card with `page: modes`).

### Modes view

Six mode cards (Morning, Daytime, Evening, Night, Sleep, Sun Protection) show how many windows or covers are assigned; Window Lamp, Glare and Excessive Heat are under **Advanced**. **Selecting a card only changes the view; it never moves anything.** Below the cards, the selected mode lists its assignments: window or cover, room, *whole window* / *single cover*, a preset dropdown, its status (Automatic, Observing, Off, *Active now*, manual hold) and a remove button. **Add Window / Cover** adds a window, or one cover of a layered window, with one of *its own* presets.

Edits are a draft (validated at once, nothing saved) until **Review & save**. The review lists every change and, per affected window, the previous and new preset, its current condition, a manual hold, a block or competing automations, and whether covers would move now. If an **Automatic** window's *current* target changes, saving needs a confirmation and a choice: **Apply now** (the controller moves it through all its normal safety checks: holds, minimum interval, daily budget, competing automations) or **At the next change of condition** (it keeps its position until its condition changes). Observing and Off windows never move because of a mapping.

### Presets come from the target

Mappings store **stable preset ids**, never positions. A whole-window assignment uses the window's preset library (for a layered window a preset sets every layer, e.g. *Half shade* = sheer 50 %, blackout 100 %). A single-cover assignment uses that cover's own presets (Fully open 100 % / Half open 50 % / Fully closed 0 % by default; rename, change or add them in the window's **Covers** tab) and changes **only that cover**. Changing *Half shade* from 50 % to 35 % in Window settings changes every assignment that uses it. A window and one of its covers cannot both be assigned in one mode (you are offered to replace the existing one), and one physical cover is never controlled twice in a mode. Deleting a preset removes the assignments that used it; renaming windows, presets or cover entities keeps every reference.

### Where a preset comes from

For each cover, the most specific setting wins:

1. **Cover override** (Window settings → Automatic → Single covers)
2. **Window override** (Window settings → Automatic → Presets per mode; this is where 0.6.3 stored its mapping)
3. **Room override** (All windows view, room rows: a preset every window of the room has, or No action)
4. **Global Modes** assignment
5. nothing: **no action**, the cover keeps its position.

Every level has three states: *Inherit* (“Use Global Setting”), a preset, or **No action** (explicitly “this mode does not move it”, which also hides less specific settings). The card shows the effective result and its source, e.g. *Half shade — Global Morning*, *Full blackout — Window override*, *No action — Explicit window override*. A Global Modes row that an override hides says so and offers **Use Global Setting**.

Glare and Excessive Heat still use a cover's Sun Protection preset when nothing is assigned for them; Useful solar heating keeps the Morning / Daytime preset. No other mode ever falls back to a lower one: when the winning mode (e.g. Night) has no assignment, the covers stay where they are; it never opens the window instead. Morning set to *No action* keeps Morning as a phase that changes nothing (with nothing at all for Morning, the day starts directly, as before).

### All windows view

A matrix (windows and their covers × modes, rooms as header rows) shows each cell's preset, *Inherit*, *No action* or “—” with its source (G global, R room, W window, C cover override). Click a cell to change it. It scrolls sideways on a phone. Both views edit the same configuration.

### Upgrading from 0.6.3

Nothing is switched on and no mode changes: Off, Observing and Automatic windows stay as they were, and every window decides and moves exactly as before. Its 0.6.3 mapping is kept as **window overrides** (a mapping that was “not set” stays “no action”). The Global Modes page shows them under each mode; **Move these to Global Modes…** (per mode, per window, or for all) turns an override into the window's Global Modes assignment after a review, without changing what it does. New windows start without any mapping.

### Decision log

Every entry names where its preset came from, in 24-hour time: *07:35 — Morning. Stova Suður → Half shade (Global Mode).*, *16:20 — Sun Protection. Køkur Suður has no assignment; leaving positions unchanged.*, *22:35 — Lamp override active. Night is configured to bypass lamp control. …*. Global Modes → *Recent automatic decisions* shows the latest entries of every window. Warnings cover missing presets, removed or unavailable covers, overrides that hide a global setting, competing automations and assigned windows that are Observing or Off.

## Global Curtain Schedule (Phase 4.6.4, 0.6.5)

Open the Smart Curtains card → **Global Schedule** (or add the card with `page: schedule`). The top shows the **household phase now** and why, what it waits for, the **next transition** (time and reason) and a 24-hour bar for today. Below, one row per phase; click a row to edit it. Edits are a draft until **Review & save**.

| Phase | Rules | Default (as before 0.6.5) |
| --- | --- | --- |
| **Morning** | Earliest time (normal / free days); opening always needs confirmed daylight and no sleep; what happens after sleep | 06:30 / 08:00 |
| **Daytime** | N minutes after Morning began · at a time · at a time but not before Morning has lasted N minutes | 120 minutes |
| **Evening** | at a time · at sunset / measured dusk · time or dusk, whichever comes **first** · whichever comes **last** | 0.6.4 behaviour (dark after 12:00) |
| **Night** | normal evenings / late evenings; late = evenings before a free day, or chosen evenings | 22:30 / 23:30 |

**A phase only makes a mode eligible.** 07:00 never opens a curtain in the dark, while someone sleeps in a sleep-protected room, or when the light sensor is unavailable: the window keeps its position and the log says *“Morning is eligible, but daylight has not been confirmed. Holding current positions.”* Night and evening closing need no light sensor. Sleep, manual holds, the window lamp and Sun Protection / Glare / Excessive Heat keep their priority; when an override ends, a window returns to the **current** phase. A Morning that could not begin (dark, asleep) is not started late, and opening still needs daylight in Daytime. Each window applies its own preset for the phase from **Global Modes**; a window without an assignment keeps its position.

**Evening** applies the Evening preset to windows with evening privacy on. Once it has started it lasts until Night, so a passing cloud does not end it. Dusk is the existing measured dusk (outdoor light below the dark level for its stable period, or the sun below the dark elevation without a working sensor). When neither light nor sun data is available, a time rule still starts Evening at its time; *at sunset / dusk* waits and says why. Until you choose another Evening rule, Evening keeps the 0.6.4 behaviour (dark after 12:00, not kept); the page says so.

**Days, explicitly.** A free day (Mon–Sun, your choice) uses the free-day Morning, Daytime and Evening times. **Night belongs to the evening it starts on**: Friday 23:55 is Friday evening; a time after midnight (00:30) still belongs to the evening before. Night ends at the next day's Morning time. Example: *21:30 Sunday–Thursday, 23:55 Friday–Saturday* = Night 21:30 (normal evenings), 23:55 (late evenings = evenings before a free day, with Sat and Sun free). **Advanced → Per-day times** sets single times for single weekdays (e.g. a later Saturday Morning).

**Rooms and windows.** The same settings can be set for a room or a single window (a window's settings → Automatic → Settings for: Room / This window → Schedule); the most specific value wins and its source is shown. The Global Schedule page lists every room and window schedule with **Use Global Schedule**.

**Preview.** Before a change is saved, the review shows the changes, the next transitions now and proposed, and per affected window its current and predicted condition and preset, whether covers would move now, a manual hold, a block or competing automations. A change that moves an Automatic window needs a confirmation and **Apply now** (through every safety check) or **At the next change of condition** (e.g. the next scheduled transition). Opening a row, typing a time or previewing never moves a curtain, and nothing switches a window to Automatic.

**Times** are 24-hour `HH:mm` everywhere (fields, timeline, transitions, logs, diagnostics), stored as wall-clock times and evaluated in Home Assistant's time zone, so daylight saving and restarts keep them. Text such as “7:45 PM” is refused. After a restart the current phase and the next transition are worked out again; past transitions are not replayed.

## Occupancy forecast (Phase 4, 0.5.0)

The integration learns, from your Recorder history, when each room is usually used and how people move between rooms, and forecasts each room's occupancy 5, 15, 30 and 60 minutes ahead.

**Observation only.** Forecasts are published as entities, shown in the room card, explained, logged and scored against what actually happened. Nothing acts on them: no light, heating or other device is ever controlled by a forecast. A forecast never changes a room's occupancy state, never starts *possible occupancy* lighting, and never overrides radar, PIR or a manual change. Automatic lighting, possible occupancy, Vacant profiles, manual overrides and wall switches work exactly as before.

*Predicted* occupancy is a separate concept from the occupancy states. **Possible occupancy** means a sensor has just seen preliminary evidence that someone may be in the room *now*. **Predicted occupancy** means the history says someone is likely to be in the room *soon*.

### Audit (before implementation)

- **"History Forecast" devices** (manufacturer *Local Occupancy Analytics*): not part of this integration. Nothing in this repository or its git history creates them, so they come from another integration on your Home Assistant. They could not be inspected, because the HA connection was unavailable while this was built. A different integration's code cannot be extended from here. This forecast therefore lives in Smart Occupancy and uses its own room configuration. If those devices duplicate it, you can remove them; nothing here depends on them.
- **Existing prediction code:** `prediction.py` held only interfaces (`NullPredictor`, `NullHistory`) from Phase 0.2. The forecast is new code that reuses the existing parts: the room sensor configuration, the occupancy engine (`engine.RoomEngine`), the adjacent rooms, the shared decision log and the Presence tab's *global default / room override* pattern.

### How it learns

1. **History from the Recorder.** For each local day, the integration asks the Recorder for the states of the configured room signals (PIR, radar, door, PC activity, bed …) and of the *influences* (see below). This is read only, through `recorder.history`, in the Recorder's own executor, and the database is never written. The oldest state the Recorder still has bounds the backfill, so a 90-day window with 10 days of history learns those 10 days, and nothing is invented for the missing ones.
2. **Replay through the occupancy engine.** The recorded states are fed through the same engine that runs live, with the same timing and adjacency, so the learned occupancy is what this integration would have reported, including stuck-sensor handling, radar dropout bridging and vacancy confirmation. Time when every signal of a room was unavailable counts as *not observed*: it is neither occupied nor vacant.
3. **Daily summaries.** Each day becomes aggregates per room and local hour: occupied and vacant seconds, entries and exits (by visit age and vacancy age), the same under each influence, arrivals, and counts and delays of movements between rooms. No sequence of individual movements is stored. One day can count at most 12 entries per room and hour, and at most 40 moves per pair of rooms, so an unusual day (a party, a faulty sensor) cannot dominate.
4. **Incremental.** After the first backfill only today (from midnight) and newly completed days are read. Learned days are kept in `.storage/smart_occupancy.forecast_days` for the learning window, beyond the Recorder's purge, so learning keeps growing as days pass. After a restart the stored days are reused, not queried again.

### The model

Each room is a two-state chain (vacant ↔ occupied) stepped forward one minute at a time:

- **Entry rate** = usual rate for this weekday, day type and hour × vacancy-age factor × influences × recent movement.
- **Exit rate** = usual rate for this weekday, day type and hour × visit-age factor × influences.

*Usual rates* are hierarchical: one weekday's hour is shrunk towards the same hour on that day type (workday or day off), then towards the same hour on every day, then towards neighbouring hours. Little data therefore means "like this room usually is", never 0 % or 100 %. The age factors capture that people often come back right after leaving and that short visits end sooner. *Influences* (sleep, PC active, home/away, "a connected room is occupied") are learned by comparing entries and exits while they were on with what the time of day alone would expect. They are never assumed. *Movement*: after new activity in room A, the probability that room B is the next room entered within 3 minutes, and how long that usually takes, by part of day and day type. Recent days count more (the *half-life* setting), so changed routines take over gradually.

Each room gets:

- **P(occupied *n* minutes from now)** for every horizon (the entity state uses the room's horizon). This is about the future: an occupied room late in its usual visit is forecast to empty, and a vacant room before its usual time is forecast to fill.
- For a vacant room, **P(entered within *n* minutes)**.
- **Usual occupancy** at that time (the schedule baseline), the **likely window** ("Likely occupied within 4 min", "Usually occupied 16:00–19:00") and the **most likely next rooms** after an arrival.
- **Confidence** and an **explanation**: the pattern, the current visit or vacancy, movement and influences, each with its effect in percentage points.

**Several people** are expected: nothing assumes one occupant, and nobody is identified. A phone that is away is only an influence whose effect is learned; it is never taken as proof that the house is empty. When several people move at once, some moves are attributed to the wrong pair of rooms; the statistics show what was seen.

**Open plan and shared sensors.** Two rooms whose signals belong to the same device (for example one FP2 radar with zones in Stova and Køkur), or whose connection is *Open plan* or *Shared transition area*, often report one person at the same moment. Arrivals within 3 seconds in such rooms count as one detection, not as a movement.

### Room connections

Room connections live in the room card: Settings → Presence → Occupancy forecast → *Room connections*. A connection is *Through a door*, *Open plan (no door)* or *Shared transition area (hall, stairs)*. Until you edit them, connections are suggested from the configuration: the *adjacent rooms* (door) and rooms whose signals share a device (open plan). Movements learned between rooms that are not connected are offered as suggestions. Nothing is derived from entity names, and no house layout is built in. For your home, sensible choices are: Bakhurð, Trappa and Inngongd as *Shared transition area* with each other; Køkur and Stova *Open plan*; the other rooms *Through a door* to their hall.

### Confidence

| Level | When |
|---|---|
| Insufficient data | Fewer than *Minimum history* days (default 3) or fewer than *Minimum visits* (default 10) observed, or the room's occupancy sensors are unavailable now. **No probability is published** (state `unknown`, status `insufficient_data`). |
| Low / Moderate / High | Score 0–100 = 45 % days observed (up to 21) + 15 % weekdays covered + 20 % visits (up to 50) + 20 % measured skill against the schedule (needs ≥ 100 scored forecasts), × the share of the room's signals available now. *Moderate* needs ≥ 40 and a week of history; *High* needs ≥ 70, 14 days and forecasts that proved better than the schedule. |

### Accuracy (no look-ahead)

Each completed day is scored *before* it is learned from (prequential evaluation). A forecast is issued every 15 minutes of that day with a model built from the earlier days only, using only what was known at that moment, and is compared with the replayed occupancy 5/15/30/60 minutes later. Per room and horizon the integration keeps:

- the **Brier score** of the forecast;
- two **baselines**: the *schedule* (usual occupancy at that hour) and *persistence* (the room stays as it is);
- the **skill** (1 − Brier / schedule Brier);
- **calibration bins**, giving correct predictions, false alarms, misses and correct non-predictions at any threshold;
- a **Brier score per day**.

These scores are the evidence that the forecast beats a plain schedule; they are not assumed. A day whose earlier history is still too short is not scored.

### Entities (per enabled room, plus two house-wide)

| Entity | State | Notes |
|---|---|---|
| `sensor.smart_occupancy_<room>_forecast` | 0–100 % or unknown | P(occupied at the room's horizon, default 15 min). Attributes: `probability_5_min` … `probability_60_min`, `status` (`ok` / `insufficient_data` / `learning`), `confidence`, `predicted`, `threshold`, `horizon_minutes`, `observation_only: true`, and (not recorded) `explanation`, `factors`, `window`, `schedule`, `entry_probability`. Unavailable while the room's own sensors are, or when forecasts are off for the room. |
| `binary_sensor.smart_occupancy_<room>_predicted` | on / off / unknown | On when the probability reaches the room's **threshold** (default 60 %) **and** the confidence reaches the **minimum confidence** (default Moderate). Never on just because a probability is above zero. |
| `sensor.smart_occupancy_<room>_forecast_confidence` | insufficient_data / low / moderate / high | `score`, `reasons`, `components` (days, weekdays, visits, scored forecasts, skill, signals available). |
| `sensor.smart_occupancy_predicted_next_room` | room name or `none` | After an arrival anywhere: the room most likely entered next (≥ 25 %), `from`, `probability`, `candidates`. |
| `sensor.smart_occupancy_forecast_learning` | waiting / learning / ready / disabled / no_recorder / error | Diagnostic: days learned, history start, last update, Recorder queries. |

**For other modules** (and Phase 5): `coordinator.forecast.room(area_id)` returns the room's `RoomForecast` (probabilities per horizon, entry probabilities, schedule, predicted, confidence, explanation, window). The websocket command `smart_occupancy/forecast/room` returns the same with history, accuracy and settings. Longer horizons need only a different `horizons` argument: the model works for any horizon.

### Settings

They are in the room card: Settings → Presence → Occupancy forecast → *Forecast settings*. Room values override the global defaults, or follow them (*Use global default*). Every setting is used by the backend.

| Setting | Default | Room override |
|---|---|---|
| Learn and forecast occupancy | on | on / off |
| Learning window (also how long learned days are kept) | 90 days | — |
| Adapt to changes (half-life; 0 = every day counts the same) | 30 days | — |
| Read new history every | 60 min | — |
| Forecast horizon | 15 min | ✓ |
| Probability threshold | 60 % | ✓ |
| Minimum confidence | Moderate | ✓ |
| Minimum history / Minimum visits | 3 days / 10 | — |
| Influences | Automatic: the bed and PC activity signals of the configured rooms | — |
| Workday sensor | Automatic: a *Workday* integration sensor if there is one, else Monday–Friday | — |

Add *Sleep* (`input_boolean.sleep`), `person.*` or a home/away group as influences if you want them learned. Influences, connections and workday changes apply to newly learned days. **Re-learn from history** applies them to everything the Recorder still has; days it no longer has are lost.

### Inspecting it

- **Overview** (room card): probability per horizon, confidence, predicted yes/no and the reason.
- **Settings → Presence → Occupancy forecast**: the forecast with every factor, learning status and history coverage. The collapsible sections are usual occupancy by hour and a weekday × hour heatmap (learned days only; empty cells mean no data), movement into and out of the room, accuracy against both baselines with a daily Brier chart and calibration, learned influences, settings, connections and the forecast log. *Update now* reads new history at once.
- **Forecast log** (shared decision log, own subjects, at most 10 per room and 10 house-wide, repeats merged). It records:
  - a prediction crossing its threshold, and whether it came true (*Prediction confirmed* / *did not materialise*);
  - the predicted next room changing, and whether that movement was confirmed;
  - a forecast becoming unavailable because the room's sensors are;
  - confidence rising or falling;
  - a learned pattern that changed by 15 points or more.

  Recalculating the same forecast logs nothing. Forecast entries never push automatic lighting decisions out of a room's own Log tab.
- **Download diagnostics**: section `forecast` (settings, status, learned dates, connections, current forecasts).

### Performance and Recorder impact

- **Queries.** One Recorder query per learned day plus one for today at each refresh (default hourly), for the configured signals only. No full-history queries are repeated.
- **Processing.** Runs in an executor thread. Measured on a simulated house of 15 rooms, 60 signals and about 5,500 state changes a day:
  - replay: about 0.2 s per day;
  - scoring a day: about 1.5 s;
  - live recalculation of every room: about 20 ms (every 5 minutes, and after an arrival or occupancy change).
- **Storage.** Learned days take about 1.5 kB per room and day, so 15 rooms over 90 days is about 2 MB. They are written at most about once an hour, with a delay. Settings are a separate small file.
- **Recorder writes.** An entity writes a state only when the rounded forecast changes: whole percent for the state, 5 % steps for the other horizons, the prediction, the status or the confidence level. Explanations and other detail attributes are not recorded, and there is no `state_class`, so no long-term statistics are created.
- **Privacy.** Everything runs locally; no history leaves Home Assistant.

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
- vacancy and presence (0.3.8):
  - in Home Assistant with simulated lights: Turn off after the timeout (and not before), presence returning before the timeout (no off/on, one counted log entry), Vacant mood (Off / On with values / left unchanged, never switching on), Leave unchanged, ambient lights, manual overrides ending with the vacancy or kept when made in the empty room (then applied when they expire), Manual mood, Observing set during the timeout, unavailable and missing lights handled when they come back, failed commands, the store migration and the websocket settings
  - presence: live states and device-class labels, binary sensor normalisation, unexpected states, include/exclude without reload (configuration kept, other rooms untouched, kept by the options flow), global defaults with room overrides, reset per setting and for all, validation and persistence, a shorter timeout applied to a pending vacancy, and the start-up race (a sensor that changed before the subscription existed)
  - Kamar Norður scenarios with the pilot's entity IDs: enter (PIR), working at the PC while radar and PIR clear, leaving, returning before the timeout, a wall switch change not undone, the PC bridge disconnecting
  - the room card's icon tabs (labels, keyboard, wrapping), Presence tab and vacancy controls
- card serving through the real frontend component

Observe rooms are asserted never to call a light service. Any test that logs an ERROR, or in which Home Assistant reports deprecated usage, fails. CI (`.github/workflows/validate.yml`) runs hassfest, HACS validation and the tests on Home Assistant 2026.10.4 and 2026.11.1.

## Upgrading

**From 0.5.0.** Nothing to configure, and nothing changes in any room. The lighting store is migrated from 1.6 to 1.7: every room gets no zone, no relay output and no supervisor override; there is no shared space; the global Room Supervisor mode is **Observing** (it compares and reports, and never sends a command). The supervisor's own log appears under Overview → Room Supervisor. The room mood-lighting sensor gets a few unrecorded attributes (`active_zones`, `supervisor`, `supervisor_discrepancies`, `relays`, `shared_space`). 0.5.0 can be reinstalled; it ignores the new keys.

**From 0.4.0.** Nothing to configure, and nothing changes in occupancy or lighting. New entities appear (forecast, predicted occupancy and forecast confidence per room; predicted next room and forecast learning). About 90 seconds after Home Assistant has started, the forecast reads the Recorder's history (once per available day), then refreshes hourly; until then the forecast entities show `learning`. Two new store files: `smart_occupancy.forecast_settings` and `smart_occupancy.forecast_days`. To switch forecasts off: Settings → Presence → Occupancy forecast → Forecast settings → Global defaults → *Learn and forecast occupancy*. 0.4.0 can be reinstalled; it ignores the new files (remove the new entities by hand if you go back).

**From 0.3.7.** Nothing to configure. What changes:
- The lighting store is migrated from 1.3 to 1.4. Every room gets the vacancy action **Turn off**, the default (there was no setting before). **Rooms in Automatic now switch their lights off when they become vacant.** Choose *Leave unchanged* in the Automatic tab to keep the previous behaviour. Ambient lights are never switched off, and Observing rooms still send nothing.
- Sensors, roles, timing, moods, profiles, overrides, switches and the decision log are kept. No signal is excluded until you switch one off in the Presence tab.
- 0.3.7 can still read the store and the options after a downgrade: it ignores the vacancy action, and an excluded signal is used again.

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

### From 0.6.0 (zones, relays, shared spaces, Room Supervisor)

- **Not verified on the production Home Assistant** (the HA connection was unavailable while this was built): WC Neðra, Køkur / Stova and relay outputs were tested with simulated sensors, Zigbee-like lights (own-context reports, failures, ignored commands, late reports) and a simulated relay. The cards were rendered in a headless browser with a mocked Home Assistant at desktop and phone widths, not in the real frontend.
- **Your existing WC Neðra / Køkur automations were not inspected** (same reason). Use the migration procedure above; no automation is changed or disabled by Smart Occupancy.
- **A change in the same direction within 20 s of a Smart Occupancy command** (for example an automation switching on a light Smart Occupancy has just switched on, at another level) cannot be told apart from the device's own late report. It is then treated as the device's report: the supervisor may correct it, within its retry limits.
- **Home Assistant's state is not the physical output**: a relay that reports *on* but whose lamp is broken looks healthy; *Device failure* means Home Assistant kept reporting the old state.
- **Supervisor history is in memory**: retry counters, verification times and metrics start over after a restart (its log is kept). Zone states, zone ownership and the shared-space vacancy wait are kept across restarts.
- **Zones use the room's sensors**: a sensor must be assigned to the room (Configure → Rooms) before a zone can use it, and its kind and role are the room's.
- **Shared lights in a shared space** follow the first occupied room in the space's list; there is no per-light choice of the owning room yet.

### From 0.5.0 (occupancy forecast)

- **Not verified on the production Home Assistant, and no real forecasts have been produced yet**: the HA connection was unavailable while this was built. Every number in the tests and in the screenshots used during development comes from simulated histories.
- **Downtime looks like quiet time.** The Recorder does not expose when Home Assistant was off, so a period without any state change is replayed as the last known states. Sensors that report *unavailable* after a restart are handled correctly (not observed).
- **Configuration changes apply to newly learned days.** Days learned with other sensors, influences or connections keep their summaries until *Re-learn from history*, which can only re-learn what the Recorder still has. A room added later learns from that day on (or from the Recorder's history after a re-learn).
- **Several people moving at the same time** can mix up movement statistics (no person is identified).
- **Pending outcome checks of predictions** (for the log) are kept in memory and are lost on restart; the accuracy statistics are not affected.

### From 0.3.8

- **Not verified on the production Home Assistant.** Vacancy, the Presence tab and the icon tabs were tested with the automated suite (simulated sensors and lights that report their state back) and rendered in a headless browser at desktop and phone widths with a mocked Home Assistant connection. Not tested with real lights, real presence sensors, or the real Home Assistant frontend.
- **The vacancy action runs on the transition to vacant.** A room that is already vacant when it is switched to Automatic, or after a restart, is not switched off until it is next occupied and vacated.
- **The cause of the Nobara "state not understood" message was found in the code** (a start-up race, now fixed and covered by a test). It could not be confirmed on the live system, where the PC bridge's entity and its timing were not visible.

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
- **No automatic switch-off when it gets bright** (vacancy switch-off was added in 0.3.8).

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
| Prediction | 0.5.0: learned forecasts per room (observation only), confidence, prequential accuracy, `ForecastManager.room()` API | Phase 5: predictive heating may read forecasts behind its own explicit opt-in |
| Safety | `assert_room_control_permitted()` gates every command; integration-wide mode stays Observe | Supervise / Full Control modes with their own tests |
