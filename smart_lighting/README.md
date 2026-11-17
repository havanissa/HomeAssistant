# Smart Occupancy & Lighting (App)

Web interface for Smart Occupancy & Lighting, opened from the Home Assistant
sidebar as **Smart Lighting**.

**Phase A3.2: the occupancy engine.** The App runs the occupancy engine
(the same, version-checked code as the integration's) in shadow mode, compares
it with the integration's room by room, and, after a reviewed handover that the
integration acknowledges, decides room occupancy. The integration keeps
publishing the same entities and takes over by itself if the App stops. One
click returns control. Presence (A3.1), Rooms (A2) and the floor plan (A2.1)
stay. The configuration is saved in the Smart Occupancy & Lighting integration
(0.7.0 or newer), which keeps running lighting and every other automation. The
App never controls a device.

- Opens full-page from the sidebar through Home Assistant's own login (Ingress); no exposed port
- Made for desktops and tablets (including landscape); light and dark. On
  phones, the integration's compact dashboard cards are the everyday controls
- Rooms: discovery, assignments, review before saving, backups and live room diagnostics
- Presence: live occupancy of every room, sensor roles, global and per-room timing, the engine's
  decision explained, a timeline from the Recorder and the integration's log, diagnostics
- Engine migration (Phase A3.2): shadow comparison, readiness checks, reviewed handover,
  automatic fallback and "Return control to integration"
- Floor plan (optional, Phase A2.1): draw floors and rooms, import a plan image, link areas,
  place devices, suggested adjacent rooms, and a live read-only view of the home with a Presence overlay
- Live connection status and live states
- Diagnostics with health checks, a searchable entity / device / area lookup and a downloadable report

See the **Documentation** tab for installation, use and known limitations.
