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

- **Overview**: the managed devices grouped by room, each with its rules, its automation's status and warnings.
- **Devices**: add and manage devices; each device page has its rules with their execution diagnostics, the
  optional default, the review and deployment, and **Export as blueprint**.
- **Rule Editor**: every rule, per device; create and edit rules.
- **Presence**: the presence sources the designers use (rooms, named zones, position zones, persons).
- **Light Level**: light-level templates, default Truth/Brightness Matrices and the light level of each room (its
  sensor, inherited limits with the room's own overrides, an optional ambient estimate), with diagnostics.
- **Heating**: heating templates (modes with temperatures) and room heating profiles (the rooms × modes matrix).
- **Import**: the automations that already exist in Home Assistant, to analyse and copy.
- **Settings**: occupancy recheck defaults, backup and restore, how deployment works, diagnostics.

## Rooms

Rooms are the **areas of Home Assistant**. Automation Builder uses them to keep lists short and to organise the
Overview; they are a convenience, not separate engines. A device has one automation (plus one per waiting rule)
whatever room it is listed in, and a rule may use sensors and helpers of any room.

- A managed device is listed under the room of its controlled entities (an entity's own area, or the area of
  its Home Assistant device).
- Entities without an area are under **Unassigned**.
- A device whose entities are in more than one room is listed once, under **Multiple rooms**, with the rooms
  named. It is not duplicated and keeps its one automation.
- Each room group can be collapsed and has **+ Add device**, which starts in that room.

## Devices

A *device* in Automation Builder is **what one automation controls**: one entity, or several entities you chose to
control together. **Add a device** asks three short questions instead of showing everything at once:

1. **Which room?** The areas of Home Assistant with how much there is to control in each, and *Unassigned*.
   Rooms can be searched. (Skipped when you start from a room's **+ Add device**.)
2. **Which device?** Only the devices of that room, with manufacturer and model and the kinds of entities they
   have. Entities that belong to no physical device (helpers, groups, scripts ...) are one entry, *Entities
   without a device*.
3. **What should be controlled?** The controllable entities of that device, by name, with the entity id as
   secondary text. A physical device can have several outputs (a dual relay, a speaker with switches): tick
   exactly the ones you mean. Nothing is ticked for you, and an area or a whole device is never controlled
   implicitly.

The path (room › device › entities) is shown at the top and each part can be clicked to go back. Going back
keeps what you ticked, so entities of two devices - or two rooms - can be combined on purpose. After **Add
device** an empty rule opens straight away; it is saved once it is complete, and nothing reaches Home Assistant
before you deploy.

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

**Choosing entities.** Every entity picker in the rule editor has **Only show entities from this room** when
the device belongs to exactly one room, and it is ticked to begin with: the list then holds the entities of
that area and those that inherit it from their device. Untick it to see everything - it stays unticked while
you edit that rule. Helpers and other entities without an area are not assumed to belong to a room, so they
appear only without the filter. The entity that is chosen at present is always listed (marked "chosen now"),
also when it is in another room, so an existing rule can always be edited.

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

### The default (optional)

Below a device's rules you can add one **Default**: actions for the case that *one of the device's rules was
triggered and none of the rules matched*.

> IF motion changes AND motion is on AND lux is below 100 → lamp on at 80 %
> IF motion changes AND motion is on AND it is late → lamp on at 10 %
> DEFAULT → lamp off

- It has no trigger and no conditions of its own. "Triggered" means: a trigger of any enabled rule of the
  device fired. It never runs by itself, never periodically, and no state is enforced: when conditions change
  without a trigger, nothing happens.
- It never runs in addition to a rule. Whether any rule matches is decided once, before any action runs (an
  action can change the very states the conditions look at). When several rules match, they all run in their
  listed order, as always, and the default stays out.
- A waiting rule (which has its own automation) counts too: when it matches, the default stays out.
- Occupancy rechecks and the periodic occupancy check never call for the default.
- **Manual override signal** (optional): while the chosen entity is on, the default does nothing - for a helper
  you switch on when you have set the device by hand.
- The default's actions use the same editor as a rule's, with one limit: they must finish at once (no delay,
  wait or open-ended repeat), because the default lives in the device's shared automation.
- It can be switched off (kept, but not part of the automation) or removed. A device without an enabled
  default generates exactly the automation it would without the feature.

In Home Assistant it is plain native logic inside the device's automation: *if any rule matches → the rule
blocks as before; else → if the run was started by a rule's trigger → the default actions*.

## Specialised designers (lights and covers)

For lights and covers there are graphical designers next to the rule editor (the Lighting Designer also maps
switches, buttons and remotes). They are another way to write the
rules of a device - not a separate engine: what a designer configures becomes ordinary rules of that device,
compiled with its other rules into its **one** native automation. Nothing of a designer runs inside the App.

Adding a device with lights or covers opens the **template gallery**; the device page has a card *Specialised
designers* with **Create** and **Templates…**. The rule list of such a device is called *Advanced rules*; both
act, and neither has priority by itself (when they can act at the same moment, the conflict check asks you).

### Unset, default, no action

Every matrix cell is one of:

| Shown as | Meaning |
|---|---|
| `UNSET` | Nothing is configured. If you configured a default (for the light/cover, or for the matrix), it applies; otherwise nothing happens. |
| `DEFAULT: 30%` | The cell is unset (or says *Use default*) and takes a default; the small text says which one. |
| `NO ACTION` | You decided that nothing happens. No default applies. |
| `OFF`, `CLOSE`, `40% · 2700K` … | A command. Off and Closed are commands like any other. |

Order: the cell itself, then the default of its light or cover (*Row default*), then the *Matrix default*. A
default is optional and never created for you. *Use default* without a default is an error. A source you have
not chosen (a mood selector, a sensor, a zone's entity) is *unset*: it blocks deployment, with the reason, until
you choose it - nothing similar-sounding is picked for you. A chosen entity that is *unavailable* simply makes
the conditions that read it fail: nothing happens.

### Matrices

Tap cells to select them (tap again to deselect), tap a row or column name to select the row or column. The
editor below the matrix changes every selected cell. **Copy** and **Paste** work on any selection (a whole mood
column onto another, the rows of some lights onto others, one cell into many); **Set to Unset** clears. Controls
appear only for what the lights or covers can do (Home Assistant reports it): no colour temperature for a lamp
without one, no tilt for a blind without one.

### Lighting Designer

Tabs: **Lights**, **Truth Matrix**, **Brightness Matrix**, **Colour & Mood**, **Switches & Remotes**,
**Advanced Rules**, **Priority & Resolution**, **Simulate**, **YAML**.

**How a light is controlled.** Each light has its own native automation. It evaluates the light from the current
states every time one of its inputs changes (a matrix source, the mood, an Advanced Rule's condition, the manual
override), and resolves every *property* on its own - on/off, brightness, colour, effect, transition - by asking
the layers in the priority order (default, highest first):

1. **Advanced Rules** (“while …” rules)
2. **Switch & remote overrides** (a manual override that a switch started)
3. **Colour & Mood**
4. **Brightness Matrix**
5. **Truth Matrix**

The first layer that *decides* a property wins: a value, or *NO ACTION* (leave it as it is). *UNSET* means “this
layer does not decide”, so the next one is asked. Then **truth gating** applies: a resolved OFF switches the light
off (brightness and colour are not sent); a resolved ON switches it on with the resolved settings; otherwise an ON
light gets the resolved brightness and colour and an OFF light stays off - brightness and colour never switch a
light on by themselves. Nothing is sent when the light already is what the evaluation wants, and nothing at all
while it is unavailable (a smart bulb whose power a wall switch cut gets no endless retries). Example: Truth ON,
Brightness 40 %, mood Hygge = 2700 K → ON at 40 %, 2700 K; if Hygge also sets 25 %, the mood's 25 % wins (it ranks
above the Brightness Matrix) - unless you move the Brightness Matrix higher.

Because the evaluation always starts from the current states, **the light returns to the currently calculated
state by itself** when an Advanced Rule's conditions stop holding or a manual override ends - no rule of its own
is needed, and no old brightness is restored when the inputs changed meanwhile. A light is evaluated when one of
its inputs changes, not at the moment you deploy; *Reconciliation* (Lights tab) can also evaluate it every few
minutes (it changes nothing that is already right).

- **Lights.** Tick the lights that take part. Optional: periodic reconciliation; *apply the calculated state when an
  unavailable light is available again* (off by default: a bulb that comes back after its wall switch was turned on
  is left as it comes back).
- **Truth Matrix.** Lights × columns, each cell ON, OFF, NO ACTION, USE DEFAULT or UNSET (no percentages). Columns
  can be any of: a presence situation (*Sofa occupied*, *Room vacant* …, from the presence sources), a range of a
  number (a light level: *below 50*, *50–300*, *from 300*, with a delay against flicker), an entity's state (a
  switch *is on*, a mood selector *is Hygge*, a media player *is playing* …), a time of day with weekdays, or
  *Always*. When two columns can hold at the same time (two zones occupied at once) and want different things for a
  light, the matrix needs an explicit policy: **an explicit ranking** (you rank the columns under Priority &
  Resolution - the order of the columns in the table decides nothing), **ON wins**, **OFF wins**, or **nothing
  while they disagree**. Several Truth Matrices are combined explicitly (an order you set, ON wins, or OFF wins).
- **Brightness Matrix.** The same columns, each cell 0-100 %, NO ACTION, USE DEFAULT or UNSET. Brightness never
  overrides an OFF and never switches a light on. **0 % is never silently an OFF**: as soon as a cell is 0 % you
  choose what it means - switch the light off, the lowest brightness (1 %), or leave the brightness as it is.
  Policies for overlapping columns: a ranking, the highest, the lowest, nothing while they disagree.
- **Colour & Mood** (optional). A mood source you choose (an `input_select` or any entity whose state is the mood),
  the moods (columns), and per light and mood: colour temperature or a colour, brightness, effect, transition -
  each field empty means unchanged. A mood applies while the light is on; it never switches a light on.
- **Switches & Remotes.** Room → device → entity or event: the device triggers Home Assistant offers for the
  device (ZHA, Zigbee2MQTT, Hue …), the event types of an event entity, or the on/off states of a switch. A
  **latched** switch (it stays on or off) reports state changes, and can also be a matrix source (a column *is
  on*); a **momentary** button or remote reports presses - its state is never read as “pressed”. Anything Home
  Assistant does not list (a `zha_event` command, an MQTT payload) can be mapped by hand (*Manual event*). For each
  interaction you choose the action - toggle, on, off, set or change brightness, apply a mood, start a manual
  override, resume automatic control, run a one-shot Advanced Rule, dim while held (until the release), or a light
  action with data - and the lights it acts on. The switch's card is a list of **interactions** (event → action →
  lights): press **+ Add Interaction** as often as needed. The same event and the same light can appear in any
  number of lines (single press → Light1 ON, double press → Light1 OFF, long press → dim, release → stop, triple
  press → resume); each line can be switched off, moved, deleted on its own, and given *only when …* conditions.
  *Find more events of this device* adds further buttons or presses to an existing switch. Two lines that
  contradict each other on the very same event (ON and OFF on one press) are a conflict to resolve; different
  presses never are. A latched switch can *ignore changes made by an automation* (when it follows the light).
  Nothing is assigned for you. Without *start a manual override* an
  action is **one-shot**: it happens once, and the next evaluation applies the matrices again (a switch gets no
  priority just because it is physical).
  **Manual override**: while it lasts, the layers below it in the priority change nothing for that light. It lasts
  *until resumed* (a resume action, or turning the helper off in Home Assistant), *a time*, *until a room or zone
  is vacant*, or *until an event*; a resume action always ends it, and the light then returns to the calculated
  state. Optionally a change *by hand* (the light switched on or off by something that is not an automation - the
  app, voice, the device itself) also starts one; Home Assistant's context tells them apart (attribute reports do
  not count, and a bulb without power reports *unavailable* instead, which is not detected).
  The override state is kept in Home Assistant **helpers** that the App creates when you deploy and owns:
  `input_boolean.ab_<device>_<light>_override` (on = override active) and, for a timed override,
  `timer.ab_<device>_<light>_override` (restored across restarts). They survive restarts and do not need the App.
  The review before deploying lists them; a helper of that name that the App did not create blocks deployment and
  is never touched; helpers no longer needed are deleted after the automations stopped using them, and removing
  the device removes them. A light that a wall switch powers cannot be switched on by software while the wall
  switch is off; not every Zigbee switch has a detached (decoupled) mode.
- **Advanced Rules.** **While …** rules set properties (ON/OFF, brightness, colour, effect, or NO ACTION to keep
  one) while their conditions hold, and release them by themselves (“while the TV plays → OFF”: when it stops, the
  light returns to what the matrices say now). **When …** rules act once at their moment (a time, or run from a
  switch). Their order (1 = highest) is their priority among themselves. The device's ordinary rules (the generic
  IF / AND-OR / THEN editor) are listed there too; they act on their own, outside this priority, and the conflict
  check reports when both can act at the same moment.
- **Priority & Resolution.** The order of the five layers (move them up or down), how several matrices combine,
  the rankings of columns, and **who controls each light now**: per light the value and the layer of every
  property, whether an override is active, and what its automation would send - calculated from Home Assistant's
  states with the same plan the automation uses. Unresolved conflicts (overlapping columns without a policy, 0 %
  without a meaning, an override without a policy …) are listed and block deployment until resolved.

**Designers made by 0.3** (version 1) keep working and compile exactly as before. Their page offers **Convert to
version 2**: you see what changes (for example: the lights now follow the matrices continuously; “priority by
column order” becomes an explicit ranking in the same order; “pause after a change by hand” becomes a timed manual
override). Parts that have no exact equivalent - permissions (gates), “both when compatible”, schedule windows,
colours inside situation cells, hysteresis - are named and nothing is converted until you change them or rebuild
by hand. Converting saves the configuration only; the automation deployed now keeps running until you deploy.

### Cover Designer

Tabs: **Covers & controls**, **Sun Position**, **Cover Moods**, **Normal Mood Mapping**, **Presence / Zones**,
**Schedule**, **Overrides**, **Simulate**, **YAML**, *Advanced Rules*.

- **Orientation: 0° = South**, +90° = West, -90° = East, ±180° = North. Home Assistant's `sun.sun` counts from
  North (180° = South); Automation Builder converts (`HA azimuth = orientation + 180°`) and shows both. Tap the
  compass to turn a window, or type the angle; *fine adjustment* adds an offset.
- **Sector**: how far counter-clockwise and clockwise of the window direction sunlight comes in (walls, eaves);
  **sun elevation** from (required, 0° = horizon) and to (optional). The compass shows today's sun path and the
  sun now; the preview says whether and when the sun shines in today.
- **Response**: a fixed position (or *Close* for covers that only open and close), or a position that follows
  the sun - linear between two points of the **sun elevation** or of the **profile angle** (the elevation seen at
  right angles to the window, which tells how deep the sun reaches in when it shines at a slant). Tilt the same
  way, for covers that tilt. A following position needs a *minimum change* (and, for covers that do not report
  their position, a *minimum time between movements*), so tiny angle changes never move a cover.
- **When the sun has gone**: unset (nothing), no action, open, close or a position. *Hysteresis* (degrees)
  decides when the sun counts as gone.
- Optional conditions: light level above, outdoor temperature above, weather conditions (the current condition
  of a weather entity), presence (Presence / Zones tab), time windows (Schedule).
- **Unavailable data** moves nothing, unless you set *When sun or sensor data are unavailable*.
- **Weather-sensitive covers** (awnings - Home Assistant's device class *awning* is recognised - and other
  exterior covers you mark): sun shading needs a wind sensor and limit (and optionally a rain sensor), which then
  retract the cover and keep it from extending, or your statement that it has its own independent protection.
- **Cover Moods**: modes you name (*Daylight*, *Privacy* … are only suggestions) in a matrix of covers × modes.
  A mode is active when a cover-mode selector shows its name, or - **Normal Mood Mapping** - when a household mood
  you mapped to it is active (unmapped moods change nothing). Without either, modes are applied by timed entries.
  Per mode you choose what happens when the sun shading also wants the cover: *the mode wins* (no sun shading
  while it is active) or *sun shading wins* (the mode's position returns when the sun has gone). This choice is
  required; nothing is decided for you.

### Presence sources

The **Presence** page lists the sources by room. A source reads an entity; it detects nothing itself (Smart
Occupancy Detection, a radar, a phone … do that). Smart Occupancy Detection is not required.

- **Room presence**: an occupancy/presence entity (on = occupied), optionally a second entity and state for
  “possibly occupied” where the source supplies one.
- **Named zone**: a zone inside a room with its own on/off entity (for example a radar's zone entity). A zone
  becoming vacant does not make the room vacant; several zones can be occupied at once.
- **Position zone**: a rectangle or polygon drawn on the coordinates a sensor reports (x and y per target,
  as states or attributes, up to three targets). Document the coordinate system and units in the notes. Optional:
  ignore readings of exactly (0, 0) (“no target”), ignore readings older than N seconds. Positions are anonymous:
  a zone never knows who is in it. Sensors report x and y separately, so give position zones an entry and exit
  delay of a second or two (the designer reminds you). A basic motion sensor cannot be a position zone.
- **Person or tracker**: a person or device tracker at a geographic zone of Home Assistant (`home` or another
  zone) - kept apart from the indoor zones; identity-based rules are entirely opt-in.
- Entry and exit delays per source; *Nothing acts while the entity reports `fresh: false`* for sources that
  report staleness (Smart Occupancy Detection does).

Everything compiles into native triggers and conditions (position zones into a template that Home Assistant
evaluates by itself), so nothing depends on the App at run time.

### Templates

Lighting: *Presence lighting* and *Light-level lighting* (a Truth Matrix), *Brightness by situation* (a Brightness
Matrix), *Colour & mood*, *Wall switch or remote*. Covers: *Sun-Angle Shading*, *Privacy Presets*, *Cover Mood
Matrix*, *Mood-Linked Covers*, *Scheduled Covers*. A template adds what it is about and lists what it needs; it
chooses no entity, value or default. Designers for climate, ventilation and fans are planned; use the rule editor
for those devices.

### Simulation

*Simulate* shows what the device's automation would do: choose what just happened (one of its triggers), the
time, and the states - Home Assistant's live values, or values you type (marked *test value*). You see each rule
that the trigger starts, which of its conditions fail, the command each light or cover would receive, conflicts,
and inputs that are missing from the scenario. It evaluates the same rules that are deployed. Nothing is sent to
any device; a hand-written template or a sun condition is reported as *cannot be evaluated offline*. It is a
prediction, not a test of the deployed automation.

For the Lighting Designer the simulation shows, per light: each matrix's value and which columns hold, the mood,
the active Advanced Rules, whether an override is active and when it ends, the winning layer of every property,
and the command (or why nothing is sent). *What just happened* can be a press of a switch or remote - an event,
kept apart from the states: you see what the mapping does (and that a one-shot action lasts only until the next
evaluation, or that an override starts).

### Light Level Manager

Light levels are configured once and shared: **Light Level** in the top bar.

- **Global Templates**: named light levels (for example Very dark, Dark, Dim, Bright) from dark to bright, each but
  the brightest with an upper limit in lux - no gaps, no overlaps; a **hysteresis** (the reading must be past a
  limit by this much), **stabilisation** delays for darker and for brighter (it must stay there that long), and
  what happens without a reading (after a time the level is *no reading* and the lights are left as they are; unset
  keeps the last level). A level keeps its id when renamed. Templates can be added, duplicated, edited and deleted
  (not while a room uses them); each change raises the template's version.
- **Matrix Defaults**: Truth and Brightness Matrices over a template's levels. They are starting values a room
  adopts on purpose - never commands to every light.
- **Room Light Levels**: one per room (Home Assistant area). Choose the room's **light sensor** (its own sensors
  are offered first; another room's sensor only when you ask, to share it deliberately), its template and default
  matrices. Every inherited value shows where it comes from (*80 lx — Global*, *95 lx — Room override*); clear a
  room value or press *reset* to inherit again. A template change never replaces a room's own values; the room
  shows *changes not deployed* and its review shows exactly what changes.
- In Home Assistant each room's level is one helper, `input_text.ab_<device>_<room>_light_level` (its state is the
  level's id), kept by one automation of the room's light level device - deploy it from the room (*Review &
  deploy*). Lights use it through **Light Level matrices** in their Lighting Designer (Truth or Brightness Matrix
  tab → *Add a Light Level matrix*): the columns are the room's levels and each cell is inherited (global default
  matrix → room → the light's own value) until the light sets its own. All lights of a room read the same level.
- **Ambient estimate** (optional) against lamps lighting their own sensor: *a reference sensor* the lamps do not
  reach (× a factor); *sampled while the lights are off* (a number helper `input_number.ab_<device>_<room>_ambient_lux`
  holds the last sample taken after the chosen lights were off for the settle time; it is valid for a set time,
  then the level has no reading); or *reading minus the lamps' calibrated contribution* (lux at full brightness per
  light, entered or taken over from observations: the App notes how much the reading moved when a light changed by
  itself - it never switches a light to calibrate). Estimates are labelled *estimated*, with their freshness or
  calibration; a normal sensor cannot tell daylight from lamplight, and without data the level is *no reading*.
- **Ambient Lux Sources** and **Diagnostics** list every room's source, reading, last update, measured or
  estimated, the level now and the level the reading calls for, and warnings such as a light that lifts the reading
  by more than the hysteresis (the level could switch back and forth).

### Heating Designer

For thermostats (climate entities): ordinary heating automation, without Smart Adaptive & Predictive Climate and
without any prediction. **Heating** in the top bar holds the **heating templates** (modes such as Comfort,
Setback, Night, Away with a temperature, and a frost-protection temperature) and the **room profiles**: the rooms ×
modes matrix (Custom Heating Matrix) where a room sets its own values. A thermostat's designer chooses a room
profile and may override single values (a temperature, *NO ACTION*, or one of the device's presets): global → room
→ thermostat, and unset stays unset (that mode then decides nothing).

Tabs: **Thermostats** (rows with their discovered capabilities, the room profile, what happens to a thermostat that
is OFF, an optional room temperature sensor shown in the preview, built-in templates: basic schedule, day/night,
presence, window, away/home, manual override), **Schedule & modes** (a weekly schedule - one for every day or
weekdays and weekend; an entry sets a mode or a temperature - or a helper whose state names the mode),
**Presence** (vacant for N minutes → a mode), **Windows & doors** (open for N seconds → a mode, frost protection,
OFF or nothing; an explicit choice for unavailable sensors), **Away / Home** (nobody home for N minutes → Away; an
optional return mode for a while after coming home), **Manual override** (a setpoint changed by hand - not by an
automation, as Home Assistant's context tells - suspends the automatic setpoint until the next scheduled change, a
time, *resume* or an event), **Priority** (default: windows, override, away, presence, schedule), **Preview** (what
each thermostat's rules decide now or in a simulated situation, and the next scheduled change - a rule preview, not
a thermal prediction) and **YAML**.

Each thermostat gets one automation that is evaluated whenever an input can change; it sends one command only when
something differs, clamped to the device's minimum/maximum and rounded to its step, and only the HVAC modes the
device offers are used. When a window closes or an override ends, the setpoint is what applies *now*. Manual
override state lives in the same kind of helpers as the Lighting Designer's.

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
| Websocket `input_boolean/{list,create,delete}`, `timer/{list,create,delete}` | the manual-override helpers of the Lighting and Heating Designers, only named `AB <device> <light or thermostat> override` (ids `ab_<device>_<entity>_override`), created at deployment; only helpers the App created and recorded are ever deleted |
| Websocket `input_text/{list,create,delete}`, `input_number/{list,create,delete}` | a room's light level (`AB <device> <room> light level`) and its sampled ambient light (`AB <device> <room> ambient lux`), the same rules |
| HTTP `GET/POST/DELETE /api/config/automation/config/<id>` | read, save and delete **one automation**, only for ids that start with `automation_builder_` |

The App cannot call a service, set a state, fire an event, change a registry or turn an automation on or off,
and it has no access to Home Assistant's configuration folder (no `map`). It never controls a device itself:
the deployed automations do. The only things it writes are its own automations, the override helpers its
Lighting Designers need (at deployment) and, on request, its own blueprint files.

**Override helpers.** `input_boolean/create` and `timer/create` are the commands Home Assistant's own helper
editor uses; helpers made this way are editable in *Settings → Helpers* and kept in Home Assistant's storage, so
they survive restarts and work without the App. The App records each helper it created (in its deployment
records, and in diagnostics); a helper of the same name that it did not create is reported and never changed or
deleted.

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
- Not part of the rule model: `stop`, `parallel`, `repeat for_each`, events fired by a rule (only the Lighting
  Designer's generated rules fire their own re-evaluation event `automation_builder_lighting`), response variables,
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
`browser_02_checks.py`, `e2e_03_checks.py`, `browser_03_checks.py`, for the designers `e2e_04_checks.py`,
`browser_04_checks.py`, and for the Lighting Designer 2 `e2e_05_checks.py`, `browser_05_checks.py`) and against a
Home Assistant OS test system (`haos.py`, `haos_checks.py`, `sod_checks.py` with the real Smart Occupancy
Detection). Details and results: `docs/AB-0.4-lighting-designer.md` (Lighting Designer 2),
`docs/AB-0.3-automation-builder.md` (designers), `docs/AB-0.2-automation-builder.md` (with the Home Assistant OS
procedure) and `docs/AB-0.1-automation-builder.md`.
