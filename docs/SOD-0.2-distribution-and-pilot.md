# Smart Occupancy Detection 0.2 (Phase 0.2): distribution, HAOS compatibility, pilot readiness

App 0.2.0, companion 0.2.0. The detection rules are unchanged (engine 1.0.0, parity tests unchanged and passing).
Smart Comfort's behaviour is unchanged; the only Smart Comfort file touched in Phase 0.1 was a repository-layout test.

## 1. Distribution (0.2.1)

| | |
|---|---|
| HACS repository | https://github.com/havanissa/smart-occupancy-detection (public, MIT) |
| Source of truth | this repository: `custom_components/smart_occupancy_detection` + `smart_occupancy_detection/companion_dist/` (README, hacs.json, CHANGELOG, .gitignore) + the root LICENSE |
| Generator | `scripts/sod_companion_dist.py` (`build`, `check`, `publish`) |
| Versions | semantic versions; tag `v<version>` = the manifest's version; one GitHub release per tag |

Why the main repository stays authoritative: the companion and the App share `protocol.py` byte for byte, and both
test suites (and the App's parity tests) live here. The HACS repository is a generated artefact.

The generator copies an **allowlist** only (the integration's `*.py`, `manifest.json`, `strings.json`,
`translations/*.json`, `brand/*.png`, the templates and the license) from an exact git ref, then validates:
exactly one integration directory, the manifest keys HACS and hassfest need, `hacs.json`, README/LICENSE/brand icon,
`const.VERSION` = manifest version, `protocol.py` identical to the App's at the same ref, a CHANGELOG section for the
version, no `__pycache__`/hidden files, and a scan for tokens, credentials, local paths and private IP addresses.

`publish` refuses a ref that is not on `origin/main`, a version that differs from the manifest, and a tag that already
exists with other content (an existing identical tag is a no-op). It pushes one commit and an annotated tag, creates
the release, then clones the published tag and compares it with the build. `check` compares any checkout with a ref
(drift detection). Without CI on this repository (GitHub Actions are disabled by choice) the release step is run by
hand after a merge:

```
python scripts/sod_companion_dist.py publish --version 0.2.0 --ref origin/main
```

The package also passes Home Assistant's own `hassfest` validation (`docker run ghcr.io/home-assistant/hassfest`).

Smart Comfort's HACS entry is undisturbed: HACS installs the *first* directory of `custom_components/` in a repository
(`hacs/utils/filters.py: get_first_directory_in_directory`), which in this repository is `smart_occupancy`; a test
(`test_distribution.py`) fails if that ever changes.

Published so far: **v0.1.0** (from d53ebd7, the Phase 0.1 merge). **v0.2.0** is published from `main` after this
phase is merged.

## 2. Installation and compatibility (0.2.2)

App store → App; HACS custom repository → companion; Add integration; Pair in the App; check Settings → Connection.

* The App reads the companion's version and protocol (`manifest/get`, `smart_occupancy_detection/status`) before it
  opens a session. Protocol mismatch → *Incompatible versions* with what to update (the integration in HACS, or the
  App); an older but compatible companion → *Update recommended* (0.1.0 works with App 0.2.0).
* Upgrades keep the configuration (schema unchanged), the decisions, the pairing token and every entity id and
  unique id (tested: App 0.1.0 → 0.2.0 image with the same `/data`, companion v0.1.0 tag → 0.2.0 package, and a
  0.1.0 companion store read by 0.2.0).
* Reloads, restarts and re-adding the integration need no new pairing: after a reload the App opens a new session;
  when the integration lost its pairing (removed and added again) the App pairs again by itself — never after an
  explicit *Unpair* (the App deletes its token then).
* No migration from Smart Comfort's entities.

## 3. Diagnostics (0.2.3)

Settings → Connection: App, integration and engine versions; compatibility; Home Assistant connection; publishing
state; **Fresh / Not fresh** (and how the integration itself sees it); last successful communication; last published
change; recent connection errors (App link and Home Assistant connection). Banners for stale data and recommended
updates. Room pages: the hold and its reason (start-up, reconnect, changed sensors) with its end, each sensor's state,
contribution and data age (time since its last change), the state, confidence and reason.

Download diagnostics (Settings → Diagnostics): versions, settings, connection and its history, runtime, configuration
summary, every room in detail, the timeline, the pilot summary, the last log lines — **redacted** (`redact.py`):
secrets and token-like strings, Home Assistant user ids (pseudonyms), IP addresses, URLs, and personal entities
(`person.*`, `device_tracker.*`, `notify.*`) with their names. Room names and sensor entities are kept. The
companion's own diagnostics redact user and instance ids.

The Phase 0.1 safety guarantees are unchanged and re-tested: no false vacancy from sensor or connection loss, stale
state explicitly marked, start-up and reconnect holds, no saves while Home Assistant is disconnected, no device
control (allowlist unchanged; the App's permissions unchanged: no host network, no privileges, no Supervisor API).

## 4. HAOS compatibility report (0.2.4)

### 4.1 Genuine Home Assistant OS (virtual machine)

VirtualBox 7.2 VM on the development machine, **Home Assistant OS 18.4** (generic x86-64 OVA, the newest x86 image),
**Supervisor 2026.11.0**, **Core pinned to 2026.11.1** (installed 2026.11.2 at first boot, downgraded through the
Supervisor). The App was installed through the real Supervisor from this repository's branch
(`https://github.com/havanissa/HomeAssistant#smart-occupancy-detection-0.2`), built on the VM from its Dockerfile, and
reached through real Ingress (Ingress session cookie). The companion was installed with the exact HACS layout from
the published **v0.1.0 tag**, then upgraded to the 0.2.0 package (the `build` output that `publish` releases).

| Validation item | Result on HAOS |
|---|---|
| Fresh installation (App 0.2.0 + companion 0.1.0, pairing, discovery, rooms, entities, detection) | 29/29 checks pass |
| Version compatibility | App 0.2.0 + companion 0.1.0 → *Update recommended* (works); after the upgrade → compatible |
| Companion upgrade 0.1.0 → 0.2.0 (HACS layout) + Core restart | entity ids, unique ids and configuration kept, no new pairing |
| Integration reload | the App publishes again by itself, no pairing |
| Network interruption: Supervisor restart (the App's Core proxy goes away) | decisions frozen, then reconnect hold, room stayed occupied, fresh again |
| Network interruption: VM network link down 30 s | publishing and detection unaffected |
| App stop / start (Supervisor) | entities keep the last decision, `fresh: false`, connection sensor off; nothing inferred; start-up hold; vacancy later from real evidence |
| Home Assistant restart (Supervisor `core/restart`) | entity back with the last decision, App reconnects and publishes, decisions frozen meanwhile |
| Unavailable sensors | uncertain, never vacant; explained; recovery |
| Configuration export / import | export, no-change import, reviewed change, restore |
| Room deletion and recreation | entities removed, then the same entity ids and unique ids |
| Concurrent Smart Comfort (App 0.11.0 + companion 1.0.0 installed and running on the same HAOS) | both Apps started, both integrations loaded, 11 / 13 entities, no shared entity id |
| Diagnostics download through real Ingress | redacted (no tokens, user ids, addresses) |
| No device control | no service call attributed to the Supervisor's user (the App's identity on HAOS) during the run |
| App permissions as granted by the Supervisor | no host network, no privileges, AppArmor default, no Supervisor API, Ingress, Core API |

Not tested on HAOS (and why):

* **HACS itself**: HACS needs a GitHub device-flow login in a browser. The HACS *package* was tested (the published
  tag's content installed in HACS's layout, and HACS's directory selection verified in its source); the HACS user
  interface and its update notification are in the manual checklist below.
* **App update through the Supervisor (0.1.0 → 0.2.0)**: the Supervisor identifies an App by repository URL and
  branch, so it can only be tested once 0.2.0 is on `main`. Planned right after the merge (the VM keeps a 0.1.0
  installation from `main` for it); tested before with the real 0.1.0 and 0.2.0 images on a simulated Supervisor.
* **Physical hardware** (Raspberry Pi, Yellow, NUC): not available; the image is the same architecture family only
  for amd64. aarch64 builds were not exercised.
* **Smart Comfort's own pilot comparison on HAOS**: Smart Comfort was installed but not set up with rooms on the VM;
  the pilot comparison against a live Smart Comfort was tested on the simulated stack.

### 4.2 Simulated Supervisor (real images, real Core 2026.11.1)

The Phase 0.1 stack (fake Supervisor, real App images, disposable Core 2026.11.1, Smart Comfort's App and companion
running beside it): upgrade cycle App 0.1.0 image + companion v0.1.0 (GitHub tag) → App 0.2.0 + companion 0.2.0,
same data, then reload, network, App outage, Home Assistant restart, transfer, pilot against Smart Comfort's entity,
recreate, diagnostics, and the proof that the App sent only allowlisted commands and never a service call.

### 4.3 Manual acceptance checklist (your HAOS)

1. HACS → ⋮ → Custom repositories → `https://github.com/havanissa/smart-occupancy-detection`, Integration → it shows
   *Smart Occupancy Detection*, version v0.2.0, the README renders.
2. Download → HACS asks for a restart → restart Home Assistant.
3. Settings → Devices & services → Add integration → Smart Occupancy Detection → created without questions.
4. App store → Smart Occupancy Detection 0.2.0 → install, start, open from the sidebar (**Occupancy**).
5. Settings → Pair with Home Assistant → Connection shows *Publishing*, *Fresh*, both versions 0.2.0, compatible.
6. Add one room → its five entities appear (`binary_sensor.sod_<room>_occupied` …) and follow movement.
7. Stop the App → the entities keep their state with `fresh: false`; `binary_sensor.sod_app_connected` turns off;
   start it → fresh again without pairing.
8. Restart Home Assistant → the entities come back with the last state; the App publishes again by itself.
9. Settings → Devices & services → Smart Occupancy Detection → ⋮ → Reload → publishing again within a minute.
10. Later: HACS shows an update when a new version is released; updating keeps the entities.

## 5. Production pilot (0.2.5)

Nothing is deployed automatically. Smart Comfort stays enabled and unchanged; no automation is changed.

### 5.1 Before

1. Full backup of Home Assistant (Settings → System → Backups → *Backup now*). Note its name.
2. In Smart Comfort: download its configuration export (for the optional import of one room).
3. Note which automations use Smart Comfort's occupancy entities (they keep using them during the pilot).

### 5.2 Pilot: one room

1. Install the App and the companion (section 2, checklist steps 1–5).
2. Add **one** room - the one Smart Comfort knows best (for example Kamar Norður): either by hand with the same
   sensors, or Settings → Import from Smart Comfort → select only that room. Keep the default timing first.
3. Settings → Pilot comparison → for that room **Use Smart Comfort's** (its occupancy entity).
4. Let it run for at least 3 days of normal use. Do not change automations.
5. Review daily: the room page (agreement, recent differences) and *Download differences (CSV)*. Every difference has
   both decisions, the reasons, the sensor evidence and an explanation. Classify each:
   * **timing** (one side still waiting for its vacancy timeout) — expected when the timing differs;
   * **sensor set** (one side uses a sensor the other does not) — align the sensors or accept;
   * **unavailable sensor** — check the device;
   * **unexplained** — keep the diagnostics download and the CSV; this is a finding, not something to tune away.
6. Expansion criteria: no unexplained difference for 3 days, every difference shorter than the longer of the two
   vacancy timeouts, no false vacant while someone was present (check the CSV against what you know).
7. Then add the next room (one or two at a time) and repeat 3–6.

### 5.3 Rollback (to the pre-pilot state)

Smart Occupancy Detection never changes Smart Comfort, automations, devices or other entities, so a rollback only
removes what the pilot added:

1. App → Settings → *Unpair* (optional), then Settings → Apps → Smart Occupancy Detection → **Uninstall**
   (its `/data` — rooms, decisions, pilot log — is removed with it; export the configuration first if you may come
   back).
2. Settings → Devices & services → Smart Occupancy Detection → **Delete** (removes its entities and devices and its
   storage `.storage/smart_occupancy_detection.published`).
3. HACS → Smart Occupancy Detection → **Remove**, then remove the custom repository; restart Home Assistant.
4. Check: no `sod_` entities remain (Settings → Entities, search `sod_`); Smart Comfort's entities and automations
   work as before.
5. If anything else changed unexpectedly: restore the backup from 5.1.1.

## 6. Release notes 0.2.0

**App 0.2.0**
- Companion integration distributed through its own HACS repository.
- Version and protocol compatibility check with actionable messages; automatic re-pairing after the integration
  was removed and added again.
- Connection diagnostics: versions, freshness (as the App and as the integration see it), last successful
  communication, recent errors; hold reasons; sensor data age; redacted diagnostics download.
- Changes are saved as they are made.
- Pilot comparison with Smart Comfort (observation only) with a CSV/JSON difference report.

**Companion 0.2.0**
- Published as its own HACS repository; documentation links point there.
- Redacted diagnostics.
- (Already in the 0.1.0 code reviewed in Phase 0.1: freshness only after a publish in the current lease, explicit
  deletions only, recovery after reloads.)

## 7. Test results

See the pull request for the exact numbers of this phase's final run.

## 8. Limitations and next steps

* Release publishing is a manual command after each merge (no CI by choice). It is guarded (main only, identical or
  refused), but it is a step to remember.
* HACS's own UI, physical hardware and aarch64 were not exercised.
* The App upgrade through the Supervisor is tested right after the merge (section 4.1).
* Next (needs your go-ahead): run the production pilot (section 5) and review the difference report together.
