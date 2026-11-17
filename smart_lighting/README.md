# Smart Occupancy & Lighting (App)

Web interface for Smart Occupancy & Lighting, opened from the Home Assistant
sidebar as **Smart Lighting**.

**Phase A1: foundation, read-only.** The App connects to Home Assistant,
shows its connection and health, detects the existing Smart Occupancy &
Lighting integration and shows the migration status of each module. It does
not control any device and does not change any configuration; the existing
integration keeps doing all the work.

- Opens full-page from the sidebar through Home Assistant's own login (Ingress); no exposed port
- Made for desktops and tablets (including landscape); light and dark. On
  phones, the integration's compact dashboard cards are the everyday controls
- Live connection status and live states of the integration's entities
- Diagnostics with health checks, a searchable entity / device / area lookup and a downloadable report

See the **Documentation** tab for installation, use and known limitations.
