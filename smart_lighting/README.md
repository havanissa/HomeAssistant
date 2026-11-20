# Smart Comfort (App)

**Smart Comfort — Intelligent Home Automation.** The App formerly called *Smart Occupancy & Lighting*, opened from
the Home Assistant sidebar as **Smart Comfort**. (Its slug stays `smart_lighting`, so an installed App, its data and
its backups carry on unchanged.)

The App runs every subsystem — room occupancy, lighting (moods, switches, daylight, ambient rules), curtains,
predictions and, since 0.10.0, **climate observation** (and since 0.11.0 thermal learning) — and holds every configuration. The *Smart Occupancy &
Lighting* integration **1.0.0** is its companion: native entities, a fenced executor for the App's light and cover
commands, ownership records and the compact dashboard cards. Nothing is migrated automatically:
**Settings → Migration & System** moves an existing 0.9.0 installation in eight reviewed steps, and
**Settings → Setup** guides a new home. While the App is stopped, nothing is switched and nothing is inferred.

- Opens full-page from the sidebar through Home Assistant's own login (Ingress); no exposed port
- Made for desktops and tablets (including landscape); light and dark. On phones, the companion's compact
  dashboard cards are the everyday controls
- Dashboard: the house now (occupancy, lighting, mood, curtains, predictions, devices, health, who runs what)
- Rooms, Presence, Lighting, Moods, Switches, Curtains, Predictions: full editors, live diagnostics,
  decision explanations, backups per module
- **Climate** (Phase A7.0, observing only): room temperatures, thermostats and what they report, windows and
  doors, thermal zones, history, and the observations predictive heating will learn from. No thermostat is
  controlled
- Floor plan (optional): draw floors and rooms, place devices, a live view of the home
- Settings → Migration & System and Settings → Setup; Diagnostics with health checks, a searchable lookup and a
  downloadable report

See the **Documentation** tab for installation, use, the migration and known limitations.
