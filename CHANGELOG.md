# Changelog

## 0.3.12 — Fix: Turn off on confirmation kept the preliminary lights on

### Fixed

- **Preliminary lights set to *Turn off* stayed on after occupancy was confirmed.** Found from the live diagnostics trace of 2026-11-12 18:38 (Kamar Norður). Occupancy was confirmed by the radar and Nobara 22 s into the stage. The normal lighting (Kvøld) switched the spots group on. Then both preliminary lights got the outcome `kept_dark` and no command was sent. Root cause, in `_confirm_preliminary`: *Turn off* keeps a preliminary light on when no other light of the room is on, so the room is never left dark, and it checked that against Home Assistant's light states right after the normal lighting's `light.turn_on`. The spots group (Zigbee2MQTT) reports its new state only a moment after the call returns, so it still read "off". Lights the normal lighting has just switched on successfully now count as on. The trace's `confirmed` step lists `scene_switched_on` and `other_lights_on`.

## 0.3.11 — Fix: possible occupancy lights stayed on with a PIR that holds "occupied"

### Fixed

- **Preliminary lights did not go off when nobody stayed** (reproduced with a Zigbee lamp and a Wi-Fi LED strip, alone and together). Root cause, in the occupancy engine (`engine.RoomEngine._corroborated`): "a trigger active for the whole correlation window" confirms occupancy. A PIR that reports *occupied* for a hold time (the Aqara PIR through Zigbee2MQTT: about 60 s after one movement) stays active longer than the 10 s window after every single movement. So 10 s after the detection the engine reported **confirmed occupancy** from that detection alone. The controller then did what confirmation means: it cancelled the preliminary timeout (`_close_preliminary`), applied the normal lighting and handed the preliminary lights over (`_confirm_preliminary`). The timeout cleanup never ran. The lights stayed on until the vacancy timeout, 10 min after the PIR cleared, or for good with *Leave unchanged* or *Vacant mood*. The 0.3.9 and 0.3.10 tests released the PIR after 1–3 s, so they never reached the window. It was not device-specific: the lights were never commanded off.
- Now, in a room with possible occupancy lighting, the signals that start it cannot confirm occupancy by themselves: not by staying active, and not together with each other. Another signal must agree (radar, PC activity, a door that is not selected, ...). Rooms without a preliminary light keep the engine's previous rules. The card lists the signals that can confirm, and warns when there is none.
- **Ownership is now ended only by a recorded change by a person** (a manual override). A brightness change by an automation or an adaptive lighting system no longer makes the stage give up a light it switched on (0.3.10 did).
- **A light whose switch-on call failed or timed out is still owned:** it was commanded on from off, so it is switched off at the end if it came on (for example a slow Wi-Fi device). Each preliminary light now gets its own command, so one failing light cannot hide another.

### Added

- Developer trace of the possible occupancy pipeline per room. It is in the diagnostics download (`preliminary.trace`) and in the debug log (`preliminary_trace`), bounded and in memory, with no sensor events logged. It records: occupancy before, the trigger, each target's state, the activation calls, results and states after, the timeout scheduled, fired or cancelled, the cleanup decision and what blocks it, the `light.turn_off` calls, results and states after, and any switch-on after a cleanup with its source. The occupancy diagnostics show each room's `preliminary_signals`.

### Upgrade

- The engine change applies only to rooms that have a preliminary light and have it enabled. A Home Assistant restart is needed after updating, because the integration's Python code is loaded at start; a browser reload is needed for the card.

## 0.3.10 — Fix: preliminary lights stayed on when possible occupancy was not confirmed

### Fixed

- **A light switched on for possible occupancy could stay on after the timeout.** Root cause: the stage switched a light off only while the controller still held its own last command for it. Every light state change that was not recognised as the controller's own erased that record, and so did a real device's own state report: a **member of a light group** (the Kamar Norður spots) reporting through Zigbee2MQTT with a context of its own, or a light reporting late (after 20 s) or with a rounded brightness. The timeout fired and the cleanup ran, but it concluded "changed since" and left the light on. Then nothing else switched it off: a door-only stage never makes the room occupied, and *Leave unchanged* or a *Vacant mood* leave lights on at vacancy. The 0.3.9 tests used simulated lights that reported back with the controller's own context, so they never hit this.
- The ownership rule is now explicit. The stage owns the lights it switched on (never one that was already on). It lets go only when someone else took the light: a manual override, a change attributed to a person or an automation, or a brightness more than 5 points away from what it set. Device reports are not such evidence. The same rule applies to *Turn off* on confirmation, which had the same fault.
- **A switch-off that could not be sent was dropped**: the room set to *Observing* or Main Mood *Manual* when the timeout came, an unavailable light, or a Home Assistant error. It is now kept as due (persisted) and sent when the room is Automatic again, when Manual ends, when the light is back, or on a retry every minute (up to 5 times). Observing still never sends anything. It is dropped when occupancy is confirmed, because the light then belongs to the room. The vacancy action leaves such lights to it, so no light gets two commands.

### Added

- Decision log: the start entry gives the timeout deadline. The confirmation entry says how long the room waited and that the timeout was cancelled (also noted on the start entry when nothing was switched on). The end entry gives each light's cleanup result: switched off (verified), failed (retried), already off, left (manual override / changed by a person or an automation / brightness changed), or blocked (Observing, Manual) with when it will be done. Retries get their own entries.
- Room card (*Settings → Automatic → Possible occupancy*): *Still to switch off: …* while a switch-off is due. Room diagnostics: `preliminary.cleanup_due`.

### Tests

- New `tests/test_preliminary_cleanup.py` with lights that behave like Zigbee2MQTT devices (own-context reports, group members, rounded and late brightness reports, failures). It covers: the Kamar Norður spots and pilot entities timing out, repeated motion, unavailable sensors, confirmation before the timeout, a light already on, manual changes, Vacant mood, an unavailable light, a failed switch-off, Observing, Manual mood, a restart while due, and no double commands at vacancy. 9 of these 15 tests fail on 0.3.9.

## 0.3.9 — Settings submenu and possible occupancy lighting

### Added

- **Possible occupancy lighting** (per room, *Settings → Automatic → Possible occupancy*; `preliminary.py`). When the occupancy engine reports **possible occupancy** from a selected signal (default: the room's Trigger/PIR sensors; radar, PC activity and doors can be ticked), chosen lights that are off are switched on dimly: brightness, optional colour temperature, transition, per light. The normal lighting waits for confirmation. This replaces the first-motion switch-on in that room. On **confirmation** the normal lighting is applied first, then each preliminary light is **handed over** or **switched off**. The occupied scene has precedence: a light it switches on is never switched off. A light it would not switch on because of daylight is switched off. *Turn off* keeps the light when nothing else in the room is on. **Without confirmation** the lights it switched on are switched off after the timeout (default 30 s, not extended by more motion) or when the room is vacant again, whichever is first. Lights a person or another automation changed meanwhile are left alone. One stage per visit; a room that confirms at once skips it.
- **Daylight for the preliminary light**: the room's daylight policy, sensors and fallback for unknown readings, and by default its threshold. An optional preliminary threshold (room, or global default) inherits the normal value when empty. Decided at the start of the stage and again if the room becomes dark during it. Nothing is switched off because of daylight.
- **Global defaults** (enabled, brightness, transition, timeout, light level, confirmation behaviour) with room and per-light overrides, shown as *Room override* / *Global default* with *Use global default*.
- **Decision log**: *Preliminary light on* / *Keep preliminary light off* (daylight, uncertain daylight, manual override, a person switched the room off, Manual mood, unavailable or failing light, Observing), *Preliminary light handed over* / *Preliminary light off* on confirmation, and *Preliminary light off* when possible occupancy was not confirmed.
- Websocket: `smart_occupancy/lighting/preliminary/set` (admin; with `area_id` a room's values, `null` = global default; without it the global defaults). The `editor` room model has `preliminary` (settings, global defaults, effective values with their sources, candidate lights, signals, the running stage). Room diagnostics include the effective settings and the stage.

### Changed

- **Room card navigation**: the main bar is **Overview · Log · Lights · Overrides · Preview · Settings** (gear). Settings opens a compact second row, **Automatic · Presence · Switches · Advanced**, with an icon and name each, the selected one highlighted, a **Back** button, tooltips, arrow keys / Home / End, and Escape back to the gear. It remembers the last section. Nothing wraps out of sight on narrow cards.
- The **Overview** shows the room's **Observing / Automatic** control (double-click the status, or the selector), and every wall switch's **Observing / Active** control. Switch settings stay in *Settings → Switches*, with the same controls. It also shows *Occupancy now* (with a running possible occupancy stage) and *Possible occupancy*.
- While a preliminary light is on, gradual global-adjustment updates and mood changes leave it alone until occupancy is confirmed. The vacancy action leaves it to its own stage, so no light gets two commands.

### Upgrade and compatibility

- The lighting store is migrated from 1.4 to 1.5: global defaults with possible occupancy lighting **disabled**, and every room without a preliminary light. **No room's behaviour changes** until a preliminary light is chosen and the setting is enabled. A 0.3.8 install reads a 1.5 store and ignores the new keys.
- Tested with simulated sensors and lights only; not verified on real devices.

## 0.3.8 — Vacancy lighting, Presence tab, icon tabs

### Fixed

- **Lights stayed on after a room became vacant.** Root cause: there was no vacancy action at all. The controller cleared manual overrides on vacancy and logged *"Smart Occupancy does not switch lights off when a room becomes vacant"*; vacancy shutdown had been left for a later phase. No setting was being ignored, no override or profile was holding the lights, and it affected every room, not only Kamar Norður.
- **A presence sensor could show "reports a state that is not understood" until it was removed and re-added** (seen with the Nobara PC activity sensor). Root cause: each signal's state was read when the occupancy engines loaded, but the state subscription started only later, after the lighting store and the first refresh. A signal that changed in between (typical while Home Assistant starts and a PC bridge connects) kept its old value in the engine (*unknown* while still unavailable) until it changed again. Home Assistant meanwhile showed it as available, so the health check flagged it. Removing and re-adding it reloaded the integration, which hid the problem. The engines now read every signal again when the subscription starts. Binary sensors were always interpreted from their raw `on` / `off` / `unknown` / `unavailable` state; display labels are never used for decisions.

### Added

- **When the room becomes vacant** (per room, *Automatic* tab): **Turn off** (lights that are on are switched off), **Vacant mood** (a chosen mood's profile is applied to the lights that are on: On with its brightness and colour, Off, or left unchanged per light type; no light is switched on in an empty room), or **Leave unchanged**. The action runs once, when the occupancy engine confirms vacancy. Its timeout is the room's existing vacancy timeout (*inactivity timeout*, or *exit confirmation* with exit evidence), so there is no second timer. Presence that returns during the timeout cancels it, and nothing is switched off and on again. Ambient (persistent) lights keep following the mood. Manual mood, Observing, unacknowledged automation conflicts, exclusions and groups apply as for every mood command.
- **Manual overrides at vacancy**: an override made while the room was occupied ends with the vacancy, as before, and the vacancy action then applies to that light. An override made after the room went quiet (a person changed a light in the empty room, e.g. from the app) is respected until it expires; then the vacancy action runs for that light if the room is still vacant. A light that was unavailable at vacancy is handled when it comes back.
- **Presence tab** (room card, after Automatic): every signal assigned to the room, with its name, entity id, kind, role, live state (labelled by device class, e.g. *Detected / Clear*, *Open / Closed*, with the raw state beside it), what it contributes now (*Active, Clear, Not used here, Unavailable, Unexpected state, Stuck*), and when it last changed. A switch per signal **includes or excludes it from this room's occupancy** without deleting it, changing the entity, or touching other rooms. The room's **presence settings** (vacancy timeout, fast vacancy timeout, occupancy confirmation, radar dropout bridge; more under *More presence settings*) are each marked **Global default** or **Room override**, with *Use global default* per setting, *Use global defaults for all*, and the **global defaults** (all rooms) in the same tab.
- **Decision log**: *Vacancy timeout started* (only when lights are on and the action would change them), updated in place to *Vacancy cancelled* (presence returned, with how long the room was quiet) or *Vacancy timeout expired*; then the vacancy result: *Lights off*, *Apply vacant mood …* or *Leave lights unchanged*, with the result *Executed*, *Sent · not confirmed*, *Partly failed*, *Failed* (unavailable or missing lights, Home Assistant errors), *Blocked* (manual override, Manual mood, no mood), *Observed only*, *No change needed*, or the new **Not done (setting)** for Leave unchanged (*"Lights left unchanged: room vacancy behavior is configured to Leave Unchanged."*). Repeated interruptions of the timeout fold into one counted entry. A deferred action that runs later (override ended, light back) is logged with that trigger.
- Websocket: `room/set` takes `vacancy_action` and `vacancy_mood`; `presence/sensor` (include/exclude) and `presence/timing` (global or room values, `null` = use the default, `reset_all`); the `editor` room model has `presence` and `vacancy`. All writes are admin only.

### Changed

- **Room card tabs are compact icons** (Overview · Log · Lights · Automatic · **Presence** · Switches · Overrides · Preview · Advanced, same order). The name is shown in a tooltip on hover and keyboard focus, is the accessible label, and is written beside the selected icon (below the bar on narrow cards). Arrow keys, Home and End move between tabs. The bar wraps instead of scrolling, so tabs after *Overrides* are no longer cut off.
- Presence settings changed in the card are applied at once, without reloading the integration. The engines keep every signal's value and re-evaluate, so there is no start-up hold and the quiet period is not restarted. Changes made in *Configure* still reload, as before.
- An excluded signal no longer counts for room health or the "no occupancy signals" check.

### Upgrade and compatibility

- The lighting store is migrated from 1.3 to 1.4: every room gets **Turn off** as its vacancy action (there was no setting to keep). Everything else is unchanged. **Rooms in Automatic will now switch their lights off when they become vacant.** Choose *Leave unchanged* in the Automatic tab to keep the old behaviour. Ambient lights are never switched off by vacancy, and Observing rooms send nothing.
- Switching a vacant room to Automatic does not switch anything off. The action runs when a room *becomes* vacant.
- Options: a signal's `enabled: false` is written only for excluded signals, so options saved before 0.3.8 keep their exact shape. 0.3.7 ignores the key after a downgrade (an excluded signal would be used again) and ignores the store's `vacancy` key.

## 0.3.7 — Decision log: what Smart Occupancy decided, why, and whether it worked

Observability only: automatic lighting, occupancy, daylight, overrides, switch mappings and operating modes behave as in 0.3.6. No setting is changed by the upgrade; the decision log starts empty.

### Added

- **Decision log** (`decisions.py`, `lighting_decisions.py`): a structured record (id, timezone-aware time, module, room or switch, trigger, kind, action, explanation, mode at that time, result, target, details) for each meaningful decision, built by the backend from the inputs it actually used (plan and per-light decisions, occupancy result and its inputs, daylight gate and light level, mood, overrides, commands sent, Home Assistant's errors). At most 10 per room and 10 per switch.
- **Results**: `executed` (accepted, and the targets reported the requested state; checked at once and again after 5 s plus any transition), `dispatched` (accepted, not confirmed), `partial`, `failed`, `observed`, `blocked`, `no_change`, `no_action`, `noted`. Nothing is shown as executed only because it was sent.
- **Observing rooms** also log what a mood change or a room mood would do (evaluated only, nothing is sent).
- **Room card**: *Last decision* at the top of Overview; a new **Log** tab (after Overview) with the last 10 decisions and their details on demand (keyboard accessible). **Switch cards**: *Last decision* and a **Log** of the switch's last 10 presses. Updates live in every open dashboard.
- Websocket: the `editor` room model has `decisions` (`latest`, `entries`, `size`), and each switch item too.

### Logging rules

- Logged: lighting applied or evaluated (visit, darkness, mood, room mood, profile change, activation, reconnection), gradual updates, manual overrides (also from switches), vacancy with lights left on or overrides cleared, a queued change dropped by Manual mood, Observing being set, switch presses and learning.
- Folded: the same decision with the same result updates the newest entry (count, time; within 15 min; gradual updates always; button repeats within 1 min); a confirmed occupancy after the first motion of the same visit adds nothing; several state reports of one manual change are one entry; a learning session is one entry.

### Storage

- Kept in the existing lighting state store (`smart_occupancy.lighting_state`, key `decisions`), written by its delayed, atomic save. No entity, helper or Recorder history; decisions are not entity attributes. A removed switch's log is removed with it.

### Changed

- Room card tabs: Overview · **Log** · Lights · Automatic · Switches · Overrides · Preview · Advanced. The switch card's *Result* row became *Last decision* (the old row is shown until the first logged press).

## 0.3.6 — Operating modes on the main screens; wall switch actions no longer suppressed by Observe

### Fixed

- **Active wall switches did nothing in rooms whose mood lighting was not enabled.** In 0.3.5 every switch action had two gates: the switch's own mode (*Smart Occupancy control*) **and** the room's mood-lighting activation, which starts in Observe and could only be changed under *Advanced*. Presses were received and logged ("the room is in Observe: no command is sent"), but nothing was executed, while the switch card still showed *Smart Occupancy control* as if it worked. Switch actions are now gated by the switch's own mode only (`assert_switch_control_permitted`), checked again when a queued press runs; the room's automatic lighting mode no longer blocks them. Automatic lighting in an Observing room still sends nothing.

### Added

- **Automatic → Automatic lighting** shows the room's operating mode first: **Observing** / **Automatic** (also *Automatic · paused* by Manual mood and *Automatic · blocked*, with the reason). Double-click the status to switch between Observing and Automatic (tooltip), or use the **Observing / Automatic** selector. It uses the existing `room/activate` / `room/deactivate` commands, so the change is the real, persisted backend state; the full review stays under *Advanced*.
- **Switches → every switch card** shows its mode with the same controls (**Observing** / **Active**): double-click the status or use the selector, without opening *Configure*. Enabled automations that also react to the switch are acknowledged right on the card; a directly bound switch cannot be set to Active. Mappings and buttons are never changed by a mode change.
- The switch model has `operating` (`mode`, effective `state`: observing / active / blocked / attention, `blocked_by`, `text`): a switch is never shown as plainly *Active* while its actions are suppressed (room not enabled) or cannot arrive (no input).
- **Press outcomes**: `observed`, `not_mapped`, `blocked`, `succeeded`, `failed` in the switch status (`last_outcome`, persisted over restarts) and in each history entry; shown as *Observed / Not assigned / Blocked / Executed / Failed* on the card and in *Recent presses*. Dispatches are logged at debug level, executed / failed / blocked presses at info, observed presses at debug only (no new log noise, no Recorder events).

### Changed

- Wording: *Listen only* → **Observing**, *Smart Occupancy control* → **Active** (switches); *Observe / Active* → **Observing / Automatic** (room). Stored values (`listen` / `control`, `control_enabled`) are unchanged: no migration, no mode is changed by the upgrade.
- `LightingController.async_switch_lights` takes the `switch_id` and checks that switch (`switch_active`), not the room's activation.

## 0.3.5 — Native wall switches, remotes and Zigbee2MQTT button control

Wall switches and remotes become part of Smart Occupancy: assign a switch to a room, learn its buttons, give each button an action, and every press counts as a manual change that automatic lighting respects. The upgrade sends no command and keeps every setting; every new switch starts in **Listen only**. Tested with simulated Zigbee2MQTT messages, device triggers and event entities through Home Assistant's MQTT integration; **not tested with physical Wiser hardware**.

### Added

- **Switches tab** in Smart Room Lighting (Overview · Lights · Automatic · **Switches** · Overrides · Preview · Advanced): assigned switches with connection, availability, last button and result, what they control, buttons configured and an active manual override; *Configure*, *Learn buttons* and *Remove*. The button table lists the device's actual button events with an action each (unassigned buttons do nothing). *Advanced switch settings*: input source, MQTT topic, event entity, manual override time, dimming, debounce, activity evidence, the Zigbee2MQTT base topic, conflicts and listener diagnostics.
- **Device selection** instead of typing entity IDs: Home Assistant devices with Zigbee2MQTT identities, MQTT device triggers or event entities, and Zigbee2MQTT devices from its device list, with name, manufacturer, model, sources, availability, known button actions, direct bindings and the room they are assigned to. Manual MQTT topics (exact topics only, no wildcards) or event entities under *Advanced*.
- **Input sources** (`switch_inputs.py`), one authoritative per switch:
  - **Zigbee2MQTT** through Home Assistant's MQTT integration: the `action` of the device topic (`zigbee2mqtt/avb_ksu`), base topic configurable (default `zigbee2mqtt`); retained messages, battery and link-quality updates, cleared actions and malformed or oversized payloads are ignored; the availability topic is read; Zigbee2MQTT's retained device list gives declared actions, bindings and renames (followed by IEEE address). Subscriptions to the assigned switches' topics only; subscribed again after an MQTT reconnect or reload; nothing is published.
  - **MQTT device triggers**, attached like automation triggers (replays right after attaching are ignored).
  - **Event entities**: the action is the `event_type` (or `button` + `event_type`); the state is a timestamp; restored and stale events are ignored.
- **Normalised button events** (device, source, raw action, button, press type, time, reference) and **deduplication**: the same press through raw MQTT, a device trigger and an event entity is executed once; deliberate repeated presses are kept; repeats within the debounce time (default 100 ms) count once.
- **Learn buttons**: listens on every available path for 60 s (cancel with *Done*), shows the detected action, saves learned buttons at once (persisted), and never executes anything while learning. Press types are taken from the device, never synthesised. Buttons the device declares are added when the switch is assigned; recognisable ON / OFF / toggle / brightness buttons get a suggested action.
- **Button actions**: Turn lights on (current mood, last used or fixed values) · off · toggle · increase / decrease brightness · start dimming up / down · stop dimming · set brightness · set colour temperature · select mood (this room, optionally for N minutes, or all rooms) · return to Auto · activate scene · run script · activate WLED preset · no action. Targets: the room's managed lights (through managed groups) or one light or group. Capabilities are respected. Only these fixed actions on validated entities; no templates or arbitrary services.
- **Listen only / Smart Occupancy control** per switch. Control is an explicit, confirmed activation; it is refused while Zigbee2MQTT reports a direct binding of the switch to lights or groups, and while an enabled automation that reacts to the switch (device trigger, event entity or MQTT topic) is not acknowledged. Bindings and automations are never changed.
- **Manual overrides from switches** (`switch_control.py`): every press marks the room's next light changes as a person's (also for directly bound switches in Listen only); executed commands register the override directly (switch, action, reason) for the room's or the switch's override time, so occupancy, moods and the global adjustment do not undo the choice. ON is not gated by daylight. Overrides are shown in *Advanced → Manual overrides* and in the switch's card.
- **Manual mood**: wall switches keep working while Main Mood is `Manual` (automatic lighting stays paused).
- **Mood buttons**: a room mood uses the existing room mood override; *all rooms* selects the option on the Manual Mood selector and lets your Main Mood automation resolve it. Main Mood is never written. Moods are discovered from the selectors.
- **Hold-to-dim**: start/stop dimming with one paced brightness command per interval (default 8 % every 0.4 s), waiting for each command, stopping on STOP/release, at the minimum or maximum brightness, or after a safety timeout (default 10 s); brightness steps per press for remotes that repeat messages. Dimmed brightness is kept as an override.
- **Activity evidence** (optional, off by default): a press refreshes the activity of an occupied room in the existing occupancy engine; it never occupies an empty room.
- **Websocket commands** (admin only): `smart_occupancy/lighting/switch/candidates`, `…/add`, `…/set`, `…/mapping`, `…/mode`, `…/learn`, `…/remove`, `…/settings`. The room model of `smart_occupancy/lighting/editor` has `switches`.
- `scripts/component_requirements.py`: installs the libraries a Home Assistant release declares for the components the tests start (used by CI on 2026.11.1).

### Changed

- The integration now lists `mqtt` and `device_automation` as after-dependencies (optional: without MQTT, event entities still work).
- Diagnostics include the switch inputs (MQTT state, sources, last events and results; never raw payloads).

### Configuration

- **Lighting store 1.2 → 1.3**: `switches` per room (empty after the migration) and the global `switch_settings` (base topic). Every other setting is kept unchanged. The wall switch entities of earlier versions (`manual_switches`) are kept and stay listen-only sources; none is converted into a command source. 0.3.4 can still read a 1.3 store.
- **State store**: the last event and result of each switch and temporary room moods.

## 0.3.4 — Room lighting card cleanup

A dashboard-only release. Lighting behaviour, the occupancy engine, the websocket API and every saved setting (including the chosen light sensors) are unchanged.

### Changed

- **Smart Room Lighting** tabs: **Overview · Lights · Automatic · Overrides · Preview · Advanced**. The tabs are the only navigation: the *Review lights*, *Configure overrides*, *Preview room* and *Advanced* buttons at the bottom of Overview are gone.
- **Automatic** tab (new): the automatic lighting settings moved here from Overview (turn on when occupied and dark, indoor light sensor, the inherited outdoor light, darkness threshold, daylight now, natural light, last decision). Sensor priority, contamination, calibration, hysteresis and diagnostics are in the collapsible *Advanced daylight settings* in the same tab, and no longer in the general Advanced tab.
- **Overview** is compact: mood, mode, occupancy and lighting health, managed lights, last applied, the global adjustment, a one-line daylight summary and warnings. No settings forms.
- **Outdoor light sensor** is shown as one global setting. It is configured in the Smart Lighting card (*Advanced → Settings → Outdoor light (all rooms)*); a room's Automatic tab shows it as inherited, with *change for all rooms*, which edits the same global setting.
- Slightly narrower tabs on phones.

## 0.3.3 — Daylight-aware occupancy activation

Occupancy switches a room's mood lighting on when someone arrives **and it is dark**. The daylight decision says whether lights are switched on; the mood profile still says what they look like. Nothing is switched off because of daylight, and no brightness or colour is derived from lux. The upgrade sends no command and keeps every setting.

### Added

- **Automatic lighting** in the Smart Room Lighting card (Overview): *Turn on when occupied and dark*, an indoor light sensor picker (illuminance sensors, the room's own first), the inherited outdoor sensor, a darkness threshold slider, and *Daylight now* (*Dark* / *Bright* / *Uncertain*) with a reason and how natural light was determined (measured or estimated). *Last decision* says what the last arrival or darkness change did and why.
- **Daylight decision per room** (`daylight.py`, `daylight_monitor.py`):
  - evidence: sun after civil dusk; indoor readings with the lights off and settled; "at most" readings with lights on that are still dark; outdoor darkness; the most recent indoor evidence; outdoor light × the room's daylight factor; twilight
  - **lamp contamination**: managed lights, group members, lights in the area and configured extra lights or switches mark indoor readings as not usable for daylight while on, and for a settling time after they turn off. A reading counts only if it was *taken* after that (a changed value by default, or every report). Bright readings with lights on are never used; the last trustworthy reading and its time are kept
  - several indoor sensors per room in priority order (preferred + fallbacks), each with a maximum age, a report mode and a reliability
  - optional global outdoor sensor, smoothed with a time-weighted median (spikes are ignored)
  - learned daylight factor (median of trusted indoor/outdoor pairs) or a factor set by the user
  - hysteresis (turn on below 50 lx, not needed from 100 lx) and a stability period (90 s) for changes while occupied; arrivals use the latest evidence at once
  - `unknown` / `unavailable` / invalid readings are never 0 lx
- **Switch-on triggers**: the first motion of a Trigger sensor (PIR) starts a visit at once (*Advanced → Switch on at → Confirmed occupancy* for fewer false triggers); radar and other Maintain sensors hold occupancy; an occupied room becoming dark; mood changes and the other existing triggers.
- **Policies** per room: *When occupied and dark* (default), *When occupied, regardless of daylight*, *Never automatically*; *When uncertain*: *Turn lights on* (default) or *Leave lights off*. Ambient (persistent) settings are never gated.
- **Advanced → Daylight**: policy, timing, fallback, thresholds, stability, settling, sensor priority and freshness, influencing lights, calibration (learned/set factor, reset), the global outdoor sensor, and daylight diagnostics.
- **Entity** `sensor.smart_occupancy_<room>_daylight` (`dark` / `bright` / `uncertain`) with the natural-light text, rounded lux, confidence, basis, contamination, sensor, outdoor lux, sun elevation, reason and the last activation. Attributes are not recorded; it is written only when the rounded summary changes.
- **Daylight health** in the room health (separate from occupancy and lighting) and problems: light sensor missing / not usable (with the fallback in use), outdoor sensor missing / not usable, influencing light missing, a sensor that reads bright after dusk (a light not listed as influencing).
- Plans and previews report the daylight decision; blocked lights say why (`daylight`, `daylight_uncertain`, `auto_on_never`). The mood lighting sensor has an `auto_on` attribute (policy, last activation and its reason).
- Websocket commands `smart_occupancy/lighting/room/daylight` and `smart_occupancy/lighting/daylight/set` (admin only).

### Changed

- Rooms switch on at the first PIR motion instead of waiting for confirmed occupancy, except rooms that were already activated (see Configuration).

### Configuration

- **Lighting store 1.1 → 1.2** (`.storage/smart_occupancy.lighting`): `daylight` per room and globally. Rooms with mood lighting already enabled keep switching on at confirmed occupancy (`on_first_motion: false`). Everything else is kept. 0.3.2 can still read a 1.2 store.
- **State store**: the last trusted indoor readings and the calibration samples per room.
- Without a light sensor nothing changes in practice: daylight is uncertain (or dark by the sun) and the default fallback switches lights on as before.

## 0.3.2 — Global light adjustment and Manual mode

Moods, light types and single lights can follow your **existing** adaptive brightness and colour-temperature system. Smart Occupancy has no adaptive-lighting engine of its own: it reads the values your system publishes. The upgrade changes no setting and sends no command. Every room keeps its Observe/Active state.

### Added

- **Global light adjustment** (Smart Lighting → *Global light adjustment*):
  - an optional brightness source and an optional colour-temperature source, chosen independently
  - live values and a status: *Connected*, *Unavailable* or *Not configured*
  - supported sources: sensors, `number` and `input_number` helpers, lights, and Adaptive Lighting switches (`brightness_pct`, `color_temp_kelvin` / `color_temp_mired` attributes)
  - unit handling: % or 0–255, and K or mired, detected automatically or chosen under *Advanced*
  - values arrive through state-change subscriptions, not polling
  - an unavailable source is never read as 0: the last good value is used (it is persisted), or the property is skipped and reported
- **Per-profile modes for brightness and for colour temperature**, independent of each other: *Follow global*, *Global + offset* (percentage points / K), *Fixed*, *Ignore*. Not set means inherit. Results are clamped to 1–100 % and 1000–10000 K, then to the light's own range.
- **Every mood** entries (pseudo mood `*`), for global light types, room light types and single lights or groups. They apply in every mood unless a mood sets its own value, and they never switch lights on or off.
- **Colour and effect precedence**: an RGB/HS colour or an effect is never overwritten by the global white temperature. RGB lights can follow the global brightness. Colour and temperature are never sent together.
- **Capability awareness**: from `supported_color_modes`, brightness-only strips follow brightness only, colour-only lights get no global temperature, and on/off lights get neither (reported as information).
- **Gradual updates** when a global value changes:
  - only for lights that are already on; a global change never switches a light on
  - only meaningful changes (default 3 % / 100 K)
  - at most one command per light per interval (default 60 s), with coalescing so the newest value wins
  - optional transition (default 10 s) where supported
  - only the properties that follow the global value are sent
  - the usual protections apply: manual overrides, exclusions, groups, availability, occupancy, conflicts, Observe
  - a light switched on later by something other than a person gets the current values right away
- **Competing controllers**: an Adaptive Lighting switch used as a source, and enabled automations or scripts that use a source entity and command lights, are detected. Their lights get no global updates from Smart Occupancy, and a warning explains why. Nothing is disabled or changed.
- **Manual mode**: Main Mood = `Manual` (configurable) pauses all automatic lighting.
  - No command is sent for moods, occupancy, activation, profile edits or global updates.
  - Queued applications and updates are dropped and never replayed.
  - Manual light changes are not corrected or recorded.
  - Occupancy, diagnostics and editing continue.
  - Leaving Manual applies the newly selected mood with the current values, in activated rooms only.
  - Manual beats every mood, room mood and adjustment setting. It needs no profile and is not listed as a mood.
- **Preview**: each light shows its global-adjustment state (*will/would follow*, *up to date*, *queued*, *blocked* with the reason, *not adjusted*).
- **Room card**: one line per light, *Brightness: Global · Temperature: Global · Effective now: 62 % / 3200 K*, and a *Global adjustment* line in the Overview.
- Websocket command `smart_occupancy/lighting/adaptive/set` (admin only), and `manual_option` in `lighting/settings/set`.
- Room lighting sensor state `paused`; attributes `manual_mode`, `global_adjustment`, `last_adaptive(_at)`, `adaptive_conflicts`. The room status sensor has `lighting_paused`.
- Problems:
  - `global_source_not_configured`
  - `global_source_unavailable`
  - `adaptive_controller_conflict`
  - `lighting_manual_mode` (info)

### Changed

- The room card says *default* (instead of *follows global*) for light types without a room override, so it can't be confused with *Follow global* brightness.
- A light's own-change window now includes the transition time, so a light still fading after our command is not mistaken for a manual change.

### Configuration

- **Store**: `.storage/smart_occupancy.lighting` keeps version 1.
  - New keys: `adaptive`, `manual_option`.
  - Profile entries can have `brightness_mode`, `brightness_offset`, `color_temp_mode`, `color_temp_offset`.
  - A value without a mode stays fixed, so every 0.3.1 profile behaves exactly as before.
- **State store**: the last good global values.

## 0.3.1 — Simple dashboards

A usability redesign of the 0.3 cards: simple by default, advanced on request. No lighting behaviour changes; Observe stays the default and nothing is applied by the upgrade.

### Changed

- **Smart Lighting Profiles** (Global Mood Editor):
  - Pick a mood and see one line per light type ("Loft ljós — 40 % · 2700 K", "Uppljós + RGB — Amber · 30 %"), with a colour dot.
  - Types with nothing set are one line of chips. Types without lights are hidden. No blocks of empty categories or "Ignore", and no validation dump.
  - **Focused editor** for one mood and one light type:
    - On / Off / Leave unchanged
    - brightness
    - white presets, colour swatches, effects
  - Only the controls the lights support are shown. Transition, ambient and the exact inherited values are behind *Advanced* in the editor.
  - Preview is a compact per-room list. Inheritance, light types, settings and validation are behind **Advanced**.
- **Smart Room Lighting** (Room Mood Lighting), tabs **Overview · Lights · Overrides · Preview · Advanced**:
  - The **Overview** shows mood, mode, health, managed lights and one line per issue.
  - Lights are grouped by light type; single lights appear only when expanded.
  - Overrides: per type *follow global* / *overridden*, and single lights.
  - Activation, automation conflicts, manual overrides and diagnostics are under Advanced.
- **Room Status card:** three health chips, one short line per warning (*Details* for the full text), and a one-line helper comparison.
- **Short health reasons:**
  - The *Lighting* health lists "2 lighting conflicts (…)", "Some settings not supported by …" and similar short lines.
  - The full explanations are in `details`.
- Lights with several category labels are normal now; the *Light in several categories* warning is gone.

### Added

- **Light type / function / RGB tag** (category *kind*).
  - A light gets the settings of all its labels, merged field by field (types < functions < RGB tag < "type + RGB" < "function + RGB").
  - **"type + RGB" targets** apply only to the RGB-tagged lights of a type, e.g. Veistla *Uppljós + RGB* purple.
- **Uppljós** default function category for new installations.
- **Label suggestions:** existing *Uppljós*/*Rgb*-style labels are offered with one click.
- **Group question:** *As a group* / *Individually* / *Ignore for now*. A warning you ignore becomes a note under Advanced (stored per room in `dismissed`).
- Websocket commands `lighting/room/group_choice`, `lighting/category/set`, `lighting/category/remove` (admin only).

### Configuration

- **Store:** lighting store version unchanged; rooms get an optional `dismissed` list.
- **Categories:** they get a `kind`, derived for existing ones, and are written to the options only when a light type is changed.

## 0.3.0 — Mood lighting, dashboard editors and controlled activation

Every room still starts in **Observe** mode. Light commands are sent only in rooms where an administrator explicitly enabled mood lighting. Existing automations and the mood selectors are never changed.

### Added

- **Generic mood lighting executor.** One executor works for every mood. Hygge is an ordinary mood: there is no special function or button.
  - It follows Main Mood, or a per-room mood.
  - It applies on a mood change, on newly confirmed occupancy (Phase 0.2 engine), on activation, on profile changes (only the affected lights), and when an offline light comes back.
  - Nothing is applied on start-up or restart.
  - Identical commands are batched, and commands already in effect are not repeated.
  - Every decision has a recorded reason.
- **Occupancy rules for lighting:**
  - Vacant and uncertain rooms are not changed.
  - Lights are not switched off on unconfirmed occupancy.
  - *Ambient* settings follow the mood even while a room is vacant.
- **Global mood profiles** per light category, with:
  - On / Off / Inherit / Ignore
  - brightness 1–100 %
  - colour temperature, RGB or HS
  - effect, transition and *ambient*
- **Mood inheritance**: a parent mood per mood, plus a fallback mood. Loops are refused.
- **Three-level inheritance**, merged field by field: global → room override → individual light/group override. Values are adapted to each light's capabilities, and what can't be applied is reported.
- **Moods are discovered** from the three selectors. New moods appear without code changes, and moods missing from Main Mood are explained.
- **Configurable handling of moods Main Mood cannot represent** (e.g. `Onki`, which is not treated as Auto): follow Main Mood (default), no mood, or another mood. A warning names the missing option.
- **Label-based discovery**: labelled lights and groups in a room's area are managed automatically. Excluding is possible per room or with an exclusion label.
- **Group de-duplication**: a managed Home Assistant light group is commanded instead of its members. Groups that hide their members (Zigbee2MQTT, Hue) are reported.
- **Per-room activation** in the Room Mood Lighting card:
  - a review of the lights, the exclusions, the groups and the existing automations that appear to control the same lights
  - explicit two-step confirmation, administrators only
  - lights of enabled automations that you have not acknowledged are left alone
- **Automation conflict detection** (read only). It follows the automations' actions through entity, group, member, device, area, floor and label targets, scenes and scripts.
- **Manual overrides**:
  - App/UI changes, and wall-switch changes (configured switch entities), are respected for a configurable time (default 60 min; 0 = until vacant).
  - Overrides are cleared on vacancy or from the card.
  - Our own commands, including MQTT echoes, and other automations are not mistaken for manual changes. Changes without any context are not manual by default.
- **Dashboard cards** (served by the integration, with visual editors):
  - **Global Mood Editor** (`custom:smart-occupancy-mood-editor-card`)
  - **Room Mood Lighting** (`custom:smart-occupancy-room-lighting-card`)

  Both have:
  - inherited/overridden display with sources
  - inline validation and immediate saving
  - a live preview that never sends commands
  - light/dark themes and phone layout
  - no polling (one subscription)
- **Websocket API** (`smart_occupancy/lighting/*`): authenticated, and every write is `require_admin`.
- **Per-room sensor** `sensor.smart_occupancy_<room>_mood_lighting` (`observe` / `active` / `blocked`). Its attributes hold:
  - the global and room mood
  - managed, offline and unsupported lights
  - conflicts and manual overrides
  - the last applied profile, with its time, result and reason
- **New problems:**
  - mood not in Main Mood, mood cannot be represented, unknown or looping parent moods
  - managed light in another area, possible duplicate group commands, automation conflict
  - unsupported or adapted light settings, failed command
  - missing wall switch, room mood unknown

### Changed

- **Mood profiles are stored in `.storage/smart_occupancy.lighting`**, owned by the integration and saved without a reload. On the first start the 0.2 profiles are copied there. The config entry options are not changed (a downgrade still loads).
- The options-flow *Mood profiles* step edits the same store, without reloading.
- **Managed lights** of a room are now the configured lights plus the labelled lights of its area.
- **Health split:** mood-lighting problems are shown under *Lighting*, never under *Occupancy*.
- A group and its member in the **same** room is now an info note (de-duplicated automatically). In **different** rooms it is still a warning.
- Profile brightness is 1–100 % (use *Off* instead of 0). Several colours in one entry are reported as `several_colors`.
- The Room Status card shows *Mood lighting* instead of *Observe only* for activated rooms.

### Recorder and efficiency

- No polling.
- One state subscription for:
  - the mood helpers
  - managed lights, and the members of managed groups
  - wall switches
- Registry-derived data (labels, areas, categories) is cached and invalidated by registry events. Automation references are cached and refreshed on reloads and on enable/disable.
- Every attribute of the new sensor is excluded from the Recorder.
- Runtime state (overrides, last applied profile) is kept in `.storage/smart_occupancy.lighting_state`, with delayed writes.

## 0.2.0 — Occupancy engine and Room Status card

Still **Observe only**: no light is controlled, no automation or helper is changed, nothing acts on the new occupancy.

### Added

- **Occupancy engine** for every enabled room, using the configured sensors and their roles:
  - *Trigger* (PIR): immediate occupancy; a trigger that stays on longer than *Trigger stuck after* (30 min) is ignored as stuck.
  - *Maintain* (radar, PC activity, bed): while active, vacancy can't be confirmed. Clears shorter than the *dropout grace* (30 s) are bridged.
  - *Confirm vacancy*: like Maintain, and its cleared state counts as vacancy evidence (vacancy after *exit confirmation* instead of the *inactivity timeout*).
  - *Door contacts* count only as movement: never occupancy on their own, but a door movement followed by nothing (optionally with movement in an *adjacent room*) is exit evidence.
  - `unknown` and `unavailable` are never treated as "off". A presence sensor that drops out while reporting presence holds the room for an extra inactivity timeout. If every sensor is unavailable the room is `uncertain` and keeps its last state.
- **States:** `vacant`, `possible_occupancy`, `confirmed_occupancy`, `possible_vacancy`, `uncertain`. The full rules are in the README.
- **Timing**, configurable globally (*Configure → Occupancy timing*) and per room (*Configure → Rooms → room → Occupancy*):
  - correlation window: 10 s
  - exit confirmation: 300 s
  - inactivity timeout: 600 s
  - dropout grace: 30 s
  - trigger stuck after: 1800 s
  - presence stuck after: off
  - start-up hold: 120 s
- **Per room entities:**
  - `binary_sensor.smart_occupancy_<room>_occupied`
  - `sensor.smart_occupancy_<room>_state`, with the evidence in its attributes
  - `sensor.smart_occupancy_<room>_confidence`
  - `sensor.smart_occupancy_<room>_last_activity`
  - `sensor.smart_occupancy_<room>_reason`
  - `sensor.smart_occupancy_<room>_helper_comparison`, when an existing occupancy helper is configured
- **Comparison with the existing helper:**
  - agreement
  - the number and duration of disagreements, counted only when they last longer than the correlation window
  - premature clears by the helper
  - possible over-retention by the engine, with its duration
  - helper-only occupancy

  Counters are kept in `.storage`. The helper is only read.
- **Health split** per room into *occupancy*, *lighting* and *configuration*, built on the Phase 0.1.1 problems. An offline light is a lighting warning; it does not make occupancy look degraded.
- **Smart Occupancy Room Status card** (`custom:smart-occupancy-room-card`), served by the integration and loaded automatically. It is read only and responsive, and has a visual editor with room selection.
- **Inverted signals**, for sensors that mean activity when they're off (e.g. a "PC idle" sensor).
- `prediction.OccupancyHistory`: an interface that receives every occupancy transition, for the future learning engine. Phase 0.2 discards them.
- Brand icon (`brand/icon.png`, `icon@2x.png`) and an MIT `LICENSE`.

### Changed

- **Configuration version 1.2** (options `config_version` 2). It is migrated automatically and every existing setting is kept: mood helpers, categories, labels, lights, roles, profiles and reference helpers. Version 0.1.1 can still load a 1.2 entry after a downgrade.
- *Confirm vacancy* now counts as an occupancy role for room health.
- New problems:
  - *Only door contacts* (warning)
  - *Adjacent room not enabled* (info)
- Motion (plain on/off changes of sensors) no longer rebuilds the diagnostics snapshot. Only availability, name, mood and light-capability changes do.

### Recorder and efficiency

- No polling. There's one state subscription for all rooms and at most one timer per room, set only for a pending deadline. A pending vacancy is a single timer, not a countdown: the card counts down in the browser.
- Entities write state only when their state or attributes change. Evidence attributes are excluded from the Recorder. There are no `state_class` values, so no statistics.
- Engine state is kept in `.storage/smart_occupancy.occupancy_state` and is written only on transitions, coalesced over 15 s.

## 0.1.1 — Diagnostic improvements and automatic recovery

Still Observe only. No lights are controlled. Existing configuration is used unchanged (`config_version` 1), and all entity unique IDs are kept.

### Changed

- **Status classification.** *Status* and every *Room observation* sensor now report:
  - `healthy`
  - `warning`
  - `config_error`
  - `unavailable` (the room cannot be observed)

  These replace `ok` / `warning` / `error` and `ok` / `degraded` / `not_configured` / `area_missing`.
- **Optional parts don't make a room unavailable.** A room is `unavailable` only when every Trigger and Maintain signal is unavailable. An offline light or a single offline sensor makes it a `warning`, and the `usable` attribute stays `true`.
- **Operational problems are kept apart from configuration problems.** An unavailable device is an operational problem, so it no longer counts as a configuration problem. A deleted, disabled or wrong-type configured entity is a configuration error.
- **Entity health is more precise.** It now distinguishes:
  - `missing` (deleted)
  - `disabled`
  - `not_provided` (registered, but its integration no longer provides it)
  - `unavailable`
  - `unknown` (no value reported)
  - `pending` (still loading during start-up)

  Before 0.1.1, *disabled* and *not provided* entities appeared as plain "unavailable", with no hint that they never recover by themselves.

### Added

- **Structured problem details on the existing sensors.** Each problem has:
  - type, title, classification, severity and category (configuration / operational)
  - a plain-language explanation and a recommended action
  - the room, the entity ID and its friendly name
  - `first_detected` and `last_checked`

  The new attributes are:
  - *Unavailable entities* and *Missing entities*: `items` (one readable line per entity) and `entities`
  - *Configuration problems*: `problems` and `notes`
  - *Status*: `summary` and `problems`
- **Room diagnostics.** Each *Room observation* sensor now exposes:
  - `configuration_valid`, `usable` and `operating_mode`
  - `reasons`
  - `sensors_available` / `sensors_unavailable`, `lights_available` / `lights_unavailable`, `pending`
  - `problems`
- **Persistent problem history.** `first_detected` survives reloads and restarts. It is stored in `.storage/smart_occupancy.problem_history`, not in the Recorder, and is written only when the set of problems changes.
- **Start-up grace period.** For 120 s after Home Assistant has started, entities whose integration hasn't loaded yet are `pending`, not reported. They're reported afterwards if they still haven't appeared.
- **Re-check diagnostics button.** It re-evaluates immediately. It reads states only.

### Recorder

- All new list attributes and `last_checked` are excluded from the Recorder.
- While problems exist, `last_checked` is republished at most every 15 minutes. A re-evaluation that finds nothing changed writes no state.
- Nothing polls: evaluations still happen only on events.
