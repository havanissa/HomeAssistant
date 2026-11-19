# Smart Occupancy & Lighting (App)

Web interface and engine of Smart Occupancy & Lighting, opened from the Home
Assistant sidebar as **Smart Lighting**.

**Phase A6: the App is the product.** The App runs every subsystem — room
occupancy, lighting (moods, switches, daylight, ambient rules), curtains and
predictions — and holds every configuration. The *Smart Occupancy & Lighting*
integration **1.0.0** is its companion: native entities, a fenced executor for
the App's light and cover commands, ownership records and the compact dashboard
cards. Nothing is migrated automatically: **Settings → Migration & System**
moves an existing 0.9.0 installation in eight reviewed steps, and
**Settings → Setup** guides a new home. While the App is stopped, nothing is
switched and nothing is inferred; the App claims control again when it is back.

- Opens full-page from the sidebar through Home Assistant's own login (Ingress); no exposed port
- Made for desktops and tablets (including landscape); light and dark. On
  phones, the companion's compact dashboard cards are the everyday controls
- Dashboard: the house now (occupancy, lighting, mood, curtains, predictions, devices, health, who runs what)
- Rooms, Presence, Lighting, Moods, Switches, Curtains, Predictions: full editors, live diagnostics,
  decision explanations, backups per module
- Floor plan (optional): draw floors and rooms, place devices, a live view of the home
- Settings → Migration & System: owners, configuration authority, companion, the migration wizard,
  outage behaviour; Settings → Setup: a new installation step by step
- Diagnostics with health checks, a searchable entity / device / area lookup and a downloadable report

See the **Documentation** tab for installation, use, the migration and known limitations.
