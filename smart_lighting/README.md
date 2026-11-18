# Smart Occupancy & Lighting (App)

Web interface for Smart Occupancy & Lighting, opened from the Home Assistant
sidebar as **Smart Lighting**.

**Phase A4: Lighting, Moods and Switches.** The App runs the integration's own
lighting code (copied byte for byte and version-checked) in shadow mode,
compares every command it would send with the integration's, and, after a
reviewed handover that the integration acknowledges, controls the lights and
owns the lighting configuration — sending each command through the
integration's fenced executor. The integration takes the lights back by itself
if the App stops; one click returns control. Occupancy (A3.2), Presence (A3.1),
Rooms (A2) and the floor plan (A2.1) stay. Needs the Smart Occupancy & Lighting
integration 0.8.0 or newer.

- Opens full-page from the sidebar through Home Assistant's own login (Ingress); no exposed port
- Made for desktops and tablets (including landscape); light and dark. On
  phones, the integration's compact dashboard cards are the everyday controls
- Rooms: discovery, assignments, review before saving, backups and live room diagnostics
- Presence: live occupancy of every room, sensor roles, global and per-room timing, the engine's
  decision explained, a timeline from the Recorder and the integration's log, diagnostics
- Engine migration (Phase A3.2): shadow comparison, readiness checks, reviewed handover,
  automatic fallback and "Return control to integration"
- Lighting, Moods and Switches (Phase A4): every room's automatic lighting and live decisions,
  moods (also without helpers) and Occupied / Vacant profiles, wall switches, remotes and
  Priority Inputs; lighting control with shadow comparison, reviewed handover and rollback
- Floor plan (optional, Phase A2.1): draw floors and rooms, import a plan image, link areas,
  place devices, suggested adjacent rooms, and a live read-only view of the home with a Presence overlay
- Live connection status and live states
- Diagnostics with health checks, a searchable entity / device / area lookup and a downloadable report

See the **Documentation** tab for installation, use and known limitations.
