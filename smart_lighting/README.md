# Smart Occupancy & Lighting (App)

Web interface for Smart Occupancy & Lighting, opened from the Home Assistant
sidebar as **Smart Lighting**.

**Phase A5: Curtains and Predictions.** The App now runs every automation
subsystem of the integration — occupancy (A3.2), lighting (A4), and the curtain
and forecast runtimes (A5) — as byte-identical, version-checked copies, each in
shadow mode until a reviewed handover that the integration acknowledges. Covers
are moved only through the integration's fenced executor; forecasts command
nothing. The integration takes a subsystem back by itself if the App stops; one
click returns control. Needs the Smart Occupancy & Lighting integration 0.9.0 or
newer.

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
- Curtains (Phase A5): windows, covers and presets, Global Modes, the Global Schedule, the environment,
  diagnostics; curtain control with shadow comparison, reviewed handover (refused while a cover moves) and rollback
- Predictions (Phase A5): learning and coverage, every room's forecast with confidence and explanations,
  patterns, accuracy, settings; forecast control with the learned data moved and verified (observation only)
- Floor plan (optional, Phase A2.1): draw floors and rooms, import a plan image, link areas,
  place devices, suggested adjacent rooms, a live read-only view of the home with a Presence overlay, and
  (A5) curtain windows linked to drawn windows with live cover positions
- Live connection status and live states
- Diagnostics with health checks, a searchable entity / device / area lookup and a downloadable report

See the **Documentation** tab for installation, use and known limitations.
