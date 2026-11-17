# Smart Occupancy & Lighting

The **Smart Lighting** App is the new home of the Smart Occupancy & Lighting
configuration. It replaces the integration's dashboard cards and options
screens module by module, and only after you approve each step.

## Phase A2: what this version does

- **Rooms** is a full room editor. Every Home Assistant area is listed, with
  its status (Configured, Not configured, Disabled, Problem), current
  occupancy, assigned sensors and lights, device health, who runs the
  automation, and warnings.
- Open a room to see the devices found in its area, assign or remove sensors
  and lights, set each sensor's type and role, mark inverted signals, include
  or exclude sensors, choose a reference occupancy entity and adjacent rooms.
- Every change is a draft until you **review** it. Saving stores it in the
  Smart Occupancy & Lighting integration, the only place the room
  configuration lives; the integration's own *Configure → Rooms* keeps
  working on the same settings.
- The other modules (Presence timing, Lighting, Moods, Switches, Curtains,
  Predictions) are shown read-only or as **Migration pending** and are still
  configured in the integration's dashboard cards.
- Dashboard and Diagnostics: connection, health, the integration's status,
  safety and the migration progress.

It does **not**:

- control lights, covers or any other device, or call any Home Assistant action (service)
- run automations: the integration keeps doing all the work, unchanged
- change Home Assistant's areas, devices or entities, Zigbee bindings or MQTT
- change anything in the integration other than the room settings you save
- migrate anything automatically
- need the internet while it runs

Saving a room needs **Smart Occupancy & Lighting 0.6.9 or newer** (update it
in HACS first). With an older version, Rooms are shown read-only.

## Installation (Home Assistant OS)

Requirements: Home Assistant OS (or a Supervised installation) with Home
Assistant 2026.10 or newer, on a 64-bit system (amd64 or aarch64, for example
a Raspberry Pi 4/5, Home Assistant Green/Yellow or an x86 PC).

1. **Add the repository.** In Home Assistant go to **Settings → Apps → App
   store**, open the **⋮** menu (top right) and choose **Repositories**. Add
   `https://github.com/havanissa/HomeAssistant` and close the dialog.
2. **Install.** Open the **⋮** menu again and choose **Check for updates**
   (or reload the page). Scroll to **Smart Occupancy & Lighting Apps**, open
   **Smart Occupancy & Lighting** and select **Install**. Home Assistant builds
   the App on your device. This needs the internet once and takes a few
   minutes (longer on a Raspberry Pi).
3. **Start.** On the App's **Info** tab, turn on **Show in sidebar** if it is
   off, optionally turn on **Start on boot** and **Watchdog**, then select **Start**.
4. **Open.** Select **Smart Lighting** in the sidebar.

Only administrators see the sidebar entry. The App works the same in the Home
Assistant Companion app on phones and tablets.

### Updating

When a new version is in the repository, Home Assistant offers the update on
the App's page (Settings → Apps). Updating rebuilds the App on your device.

### Uninstalling

Settings → Apps → Smart Occupancy & Lighting → **Uninstall**. The integration
and its configuration are not affected.

## Using the App

The App is made for **desktops and tablets** (portrait and landscape): it is
where you configure, inspect and diagnose. For everyday use on a **phone**,
use the compact Lovelace dashboard cards (status, moods, Automatic /
Observing, overrides, curtain presets); today these are the integration's
cards. The App also opens on phones, with a simpler layout.

- **Desktops and landscape tablets:** a sidebar with every module; wide
  screens show up to three columns of cards.
- **Portrait tablets:** a navigation rail on the left; the menu button at its
  top expands it to show every module by name.
- **Phones:** the main screens are in the bottom bar; **More** lists every module.
- **Status:** the top bar always shows the Home Assistant connection
  (Connected, Reconnecting, …). If the App backend itself cannot be reached,
  it shows "App unreachable" and keeps retrying.
- **Theme:** Settings → Appearance: Automatic (follows your device), Light or
  Dark. It is remembered in that browser.
- **Advanced details:** Settings → Show advanced settings.

## Rooms

**Overview.** Search by room, sensor or light; filter by status; sort by
name, status, occupancy or what needs attention. Select a room to open it.

**Room workspace.** Three panels on wide screens, collapsible with the buttons
at the top right:

- *Left:* the room list and **Available in this room**: sensors and lights
  found in the area that are not assigned. **+** assigns one; **Add sensor** /
  **Add light** search every area (a device from another area gets a note; its
  Home Assistant area is not changed).
- *Centre:* the tabs **Overview** (observe occupancy on/off, occupancy now,
  device health, who runs what, warnings, remove the room configuration),
  **Devices** (every device of the room, assigned or not, with live status),
  **Presence** (sensor type, role, inverted, included; reference entity;
  adjacent rooms; presence timing read-only), **Lighting**, **Switches** and
  **Curtains** (read-only, configured in the integration's cards for now) and
  **Diagnostics** (entity statuses, conflicts, the integration's checks and
  occupancy inputs, the stored configuration).
- *Right:* the **inspector** of the selected device: live state, availability,
  assignment controls, why it was classified as it is, capabilities and device
  identity (manufacturer, model, Zigbee address). On tablets it slides over.

**Statuses.** *Device offline*: every entity of the device is unavailable
(power, battery, radio). *Entity unavailable*: only this entity. *Missing*:
the entity does not exist. *Present, not assigned*: found in the area, not
used. *Conflict*: used by several rooms, or from another area.

**Saving.** Edits show in a bar at the top (**Discard** or **Review
changes**). The review lists what is added, removed and changed, potential
conflicts, missing or unavailable devices, and what saving does. Consequences
such as stopping occupancy for a room, or changes in a room with *Automatic*
lighting, must be ticked before **Save**. Saving reloads the integration (about
a second; no light or cover is switched by it). If the room was changed
elsewhere (for example in *Configure → Rooms*) while you edited, the App says
so and reviews your edits against the current configuration. Leaving the room
with unsaved edits asks first.

**Backups.** Rooms → **Backups**: the room configuration as it was before
each save or restore (latest 30), **Export current configuration**, download a
backup, or **Restore** one (from the list or a file) after reviewing it.

## Migration: one system in control

Modules move from the integration to the App one at a time, only after you
approve each step. For each module the App reports where it is **configured**,
where the configuration is **stored**, and which system **runs** it. In this
version Rooms is configured in the App, stored in the integration and run by
the integration; every other module is configured, stored and run in the
integration. The integration and the App never control the same devices.

## Configuration

| Option | Description |
| --- | --- |
| `log_level` | How much the App writes to its **Log** tab: `debug`, `info` (default), `warning` or `error`. |

## Troubleshooting

- **"Home Assistant: Reconnecting…" stays on.** Open Diagnostics: the Home
  Assistant connection card shows the last error. Restarting Home Assistant
  or the App reconnects automatically.
- **"App unreachable".** The App is stopped or restarting. Check the App's
  **Info** and **Log** tabs.
- **Reporting a problem.** Diagnostics → **View data** (then **Copy**) or
  **Download**. The report contains no passwords or tokens.
- **Opening the App's address directly** (not through the sidebar) is refused
  on purpose: it only accepts Home Assistant's Ingress.

## Security

- Only Home Assistant administrators can save (the App checks the user Home
  Assistant's Ingress reports). Requests must come from the App's own page.
- The App backend sends only a fixed list of commands to Home Assistant: reads,
  and the integration's own validated room commands. It cannot call services,
  write states or change registries. Its token never reaches the browser.

## Known limitations (Phase A2)

- Only the room configuration (rooms, sensor and light assignments) is edited
  here. Presence timing, lighting behaviour, moods, switches, curtains and
  forecasts are configured in the integration's dashboard cards as before.
- Saving needs the integration to be running; while it is not, rooms are shown
  from its stored configuration.
- The theme follows your device (or your choice in Settings), not the Home
  Assistant user theme.
- The interface is in English.
- The first installation and every update need the internet to build the App
  (base image and Python packages). Normal operation does not.
- Live updates cover the integration's entities, the connection status and
  the devices of the open room. The Home Assistant lookup reads states when you
  open it.
- Tested with a simulated Supervisor and Ingress against Home Assistant
  2026.11.1; not yet on a real Home Assistant OS installation.
