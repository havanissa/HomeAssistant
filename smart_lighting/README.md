# Smart Occupancy & Lighting (App)

Web interface for Smart Occupancy & Lighting, opened from the Home Assistant
sidebar as **Smart Lighting**.

**Phase A2: Rooms.** A full-page room editor: every Home Assistant area, its
devices, and the room's sensor and light assignments, reviewed before they are
saved. The configuration is saved in the existing Smart Occupancy & Lighting
integration (0.6.9 or newer), which keeps running every automation. The App
never controls a device; every other module is still configured in the
integration.

- Opens full-page from the sidebar through Home Assistant's own login (Ingress); no exposed port
- Made for desktops and tablets (including landscape); light and dark. On
  phones, the integration's compact dashboard cards are the everyday controls
- Rooms: discovery, assignments, review before saving, backups and live room diagnostics
- Floor plan (optional, Phase A2.1): draw floors and rooms, import a plan image, link areas,
  place devices, suggested adjacent rooms, and a live read-only view of the home
- Live connection status and live states
- Diagnostics with health checks, a searchable entity / device / area lookup and a downloadable report

See the **Documentation** tab for installation, use and known limitations.
