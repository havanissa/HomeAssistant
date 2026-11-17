# Changelog

## 0.1.1

Design direction update (still Phase A1, read-only).

- Desktop and tablet (including landscape) are the primary targets. Card
  columns now follow the available width: up to three on wide screens and
  landscape tablets, two on portrait tablets. The Home Assistant lookup shows
  the selector and the details side by side on desktops. The phone layout
  stays as a simpler fallback; everyday phone use is through Lovelace cards.
- Each module shows which system is **in control** (the integration for
  every module in this phase). Migration moves one module at a time and never
  lets the integration and the App control the same devices together.

## 0.1.0

Phase A1: App foundation (read-only).

- New Home Assistant App "Smart Occupancy & Lighting", installable from this
  repository. Opens from the sidebar as **Smart Lighting**
  (`mdi:lightbulb-auto`) through Ingress, for administrators. No host port.
- Backend (Python, aiohttp) connected to the Home Assistant websocket API with
  the Supervisor-provided token, which never reaches the browser. Only a fixed
  list of read-only commands can be sent; automatic reconnects; live state
  updates; health checks.
- Web interface (Lit, TypeScript), designed phone-first: bottom navigation on
  phones, a collapsible rail on tablets, a sidebar on desktops; light / dark /
  automatic theme; touch targets of at least 44 px; text scaling and safe areas.
- Dashboard: Home Assistant connection, App health, detection of the existing
  integration (version, state, live entity states), modules and migration progress.
- Rooms, Presence, Lighting, Moods, Switches, Curtains and Predictions:
  "Migration pending" screens that say where each is configured today. No controls.
- Diagnostics: health checks, safety status (service calls sent: 0),
  connection details, system information, searchable entity / device / area
  lookup, recent log, diagnostics data to view, copy or download.
- Settings: theme; advanced (technical) details on request.
