# Smart Occupancy & Lighting

The **Smart Lighting** App is the new home of the Smart Occupancy & Lighting
configuration. It replaces the integration's dashboard cards and options
screens module by module, and only after you approve each step.

## Phase A1: what this version does

This is the foundation. It is **read-only**:

- It connects to Home Assistant and shows the connection, the App's health,
  and the existing Smart Occupancy & Lighting integration (version, state, and
  the live states of its entities).
- It lists the modules (Rooms, Presence, Lighting, Moods, Switches, Curtains,
  Predictions) and their migration status. Each one is **Migration pending**:
  its screen tells you where that part is configured today, and has no settings.
- Diagnostics shows health checks, the connection, a searchable lookup of
  entities, devices and areas, and the recent log, and it can show, copy or
  download a diagnostics report.

It does **not**:

- control lights, covers or any other device, or call any Home Assistant action (service)
- change the integration, its options, entities, helpers or automations
- run a second automation engine: the integration keeps doing all the work
- migrate anything automatically
- need the internet while it runs

You can install, use and uninstall the App without any effect on your lighting.

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

## Migration: one system in control

Modules move from the integration to the App one at a time, only after you
approve each step. The App's version of a module is validated first, and
only then is the matching integration function switched off, so the
integration and the App never control the same devices at the same time.
Every module screen shows which system is **in control**; in this version
that is always the integration.

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

## Known limitations (Phase A1)

- Read-only: no module has been migrated yet. Configure everything as before,
  in the integration's dashboard cards and options.
- The theme follows your device (or your choice in Settings), not the Home
  Assistant user theme.
- The interface is in English.
- The first installation and every update need the internet to build the App
  (base image and Python packages). Normal operation does not.
- Live updates on the Dashboard cover the integration's entities and the
  connection status. The Home Assistant lookup reads states when you open it.
