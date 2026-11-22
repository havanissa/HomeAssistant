# Automation Import/Export Manager

Export, archive, clean up and restore Home Assistant automations with their labels, categories, areas, entity ids
and enabled states. Everything that changes Home Assistant is previewed first and needs your explicit
confirmation; every archive is verified; Automation Builder's automations are never deleted or written.

## Installation

1. Home Assistant → Settings → Apps → App store → ⋮ → Repositories: add
   `https://github.com/havanissa/HomeAssistant` (once, it also holds this repository's other Apps).
2. Install **Automation Import/Export Manager** and start it. It appears in the sidebar as **Automation Archive**
   (administrators only).

Requirements: Home Assistant 2026.10 or newer (tested with 2026.11.1). The App reads Home Assistant's
configuration folder **read-only** (`map: homeassistant_config`) for two things only: blueprint files (so archives
include them) and `automations.yaml` (to detect duplicate or missing ids). Without that folder the App still
works; it then says what it cannot know.

## The pages

- **Overview** – how many automations there are (ordinary, protected, ambiguous, external YAML), stored
  archives, recent operations, anything interrupted or waiting for your confirmation, and the main action
  **Export & Delete Legacy Automations**.
- **Export** – every automation with its name, entity id, configuration id, on/off state, area, categories,
  labels, source, blueprint, owner and whether it can be deleted. Search, filter by area / category / label /
  state / kind, group by category / area / label / source, select and **Export selected**, **Export & Delete
  selected…** or **Back up everything**. *Details* shows what an automation refers to and every finding.
- **Import** – upload an archive of this App or an ordinary automation YAML file; both lead to the restore preview.
- **Archives** – stored archives with date, kind, number of automations, Home Assistant version, verification
  and size: **Download**, **Inspect** (contents, warnings, files, verify again with a live comparison),
  **Restore…** and **Delete** (with confirmation).

## Kinds of automations

| Kind | What it means | Export | Deleted by cleanup | Restored to its own id |
| --- | --- | --- | --- | --- |
| Ordinary | In `automations.yaml`, editable in Home Assistant's UI | yes | yes | yes |
| Automation Builder | Id starts with `automation_builder_` (any case) | backup mode | **never** | never (copy only, acknowledged) |
| Ownership unclear | Carries Automation Builder's description, name suffix or blueprint folder, but not its id namespace | yes | only when included explicitly | yes |
| External YAML | Loaded from another file (a package, `automation name: !include …`) or without an id | read-only copy as Home Assistant runs it | **never** | never (copy only, acknowledged) |
| Integration / registry only | An entity of another integration, or a registry entry without an automation | metadata only | **never** | – |

An automation is also **blocked** from deletion when its id occurs twice in `automations.yaml`, its id is not
text, its registry entry is missing, or one of its labels, categories or areas cannot be read: its restoration
could not be guaranteed. Missing *dependencies* (an entity, script, device … it refers to) are warnings only.

## Export & Delete Legacy Automations

1. **Inventory and exclusions** – all eligible ordinary automations are preselected; untick what you want to keep.
   Ambiguous ones must be ticked one by one (with a confirmation). Excluded automations are listed with the reason.
2. **Analyse, export and verify** – dependencies are analysed, the archive is written to the App's storage and
   verified: archive readable, checksums, every configuration complete, every YAML file parsed back and compared
   exactly, metadata and definitions present, identities match the manifest, dependencies present or identified,
   no automation omitted, protected automations excluded, and a lossless comparison with what Home Assistant has
   now. An automation that fails any of its checks stays out of the deletion.
3. **Download** the archive (recommended) – it is also kept in the App and in Home Assistant's backups of the App.
4. **Final deletion preview** – the exact list. Tick that the archive is kept and type the number of automations.
5. **Delete** – right before each deletion the automation is read again; if its configuration or registry
   metadata changed since the export, it is skipped. Each deletion is journalled before and after.
6. **Verify and report** – removals are checked in Home Assistant, every protected or excluded automation is
   compared with its state before the operation, and the report lists what was deleted, skipped, failed and what
   remains active (external YAML automations stay; they are not deleted here).

Nothing is ever deleted automatically after an export. Labels, categories, areas, blueprints, scripts, scenes and
helpers are never deleted.

### Interruptions

Every operation keeps a journal in `/data/operations`. If the App stops while exporting, deleting or restoring, the
operation shows as **interrupted** at the next start. *Check the current state* compares Home Assistant with the
journal and records what really happened: an interrupted export leaves no archive; an interrupted deletion shows
which automations are gone and offers **Verify again and continue the deletion** or **Restore the deleted ones**;
an interrupted restore is reported as incomplete – run the restore again and it finishes (automations that are
already there only get their metadata).

## Restore

Open an archive (Archives → Restore…, or after an upload). The App compares it with Home Assistant and shows the
plan:

- **Automations** – *Create* (missing), *Already there: restore its metadata* (identical configuration),
  *Skip* by default when an automation with the same id exists and differs, with the differences; choose
  *Replace (after a backup)* or *Restore as a new automation* (a new id; Home Assistant assigns a new entity id).
  Untick automations you do not want.
- **Labels, categories, areas** – the same id is used when it exists; otherwise one with the same name (a name is
  never duplicated); otherwise it is recreated (new ids for categories, mapped automatically). You can choose
  another existing one, *Create*, or *Do not restore* (needs an acknowledgement).
- **Blueprints** – installed ones are kept; a missing one is installed from the archive; if the archive has none,
  the automation stays unavailable until you install it (needs an acknowledgement).
- **Deviations** – every difference from an exact restoration (a dropped label, an entity id that is taken, a copy
  of a protected automation, a missing blueprint, a configuration Home Assistant reports as invalid) must be
  ticked before **Restore** is possible.

The restore writes each automation through Home Assistant's automation editor endpoint, waits for its entity,
restores the entity id, name, icon, area, labels, categories, voice aliases, hidden flag, assistant exposure,
on/off state and disabled flag, and then compares everything with the archive. A restoration that has the YAML
but lacks a label, a category or anything else is reported as **incomplete**, never as a success. Before anything
is replaced, the current version is archived (*Backup before replacing*). **Roll back this restore** removes what
the restore created (unless it changed since) and puts replaced automations back from that backup; labels,
categories and areas it created are kept.

### Archives from another installation

The archive records the App installation it came from. From another installation the plan says so: check the
mapping of labels, categories and areas (ids differ between installations; names are matched). Entity ids are
restored when they are free.

### Ordinary YAML files

**Import → Home Assistant automation YAML**: a list of automations (as in `automations.yaml`), one automation, or
an `automation:` section. Entries without an id get one; entries with `!secret`, `!include` or other tags,
values Home Assistant's editor cannot store (dates, binary), duplicate ids or no triggers are refused with the
reason. Such files have no labels, categories or areas, so only the configurations are restored.

## The archive

A ZIP file (format version 1):

```
manifest.json                     format, ids, Home Assistant version, source installation, inventory,
                                  exact associations, counts, warnings, compatibility, SHA-256 of every file
README.txt                        what it is, and that it may contain private data
automations.yaml                  all exported configurations, ready for Home Assistant
automations/0001_<id>.yaml        each configuration exactly as it is in automations.yaml
data/automations.json             per automation: configuration, registry entry, state, metadata, references
data/labels.json, categories.json, areas.json, floors.json, blueprints.json
blueprints/automation/<path>      the blueprint files used
```

Configurations are stored exactly: unknown fields, value types, list order and legacy keys are kept; only the
order of keys in a mapping is not significant. **Treat archives as sensitive**: automations can contain addresses,
phone numbers, tokens or passwords. Archives never leave your installation unless you download them.

Uploaded archives are checked before anything is read: at most 64 MB, 5000 files, 16 MB per file and 256 MB in
total, no suspicious compression ratio, no absolute paths, `..`, backslashes, links, duplicate or encrypted
entries; unknown format versions are refused.

## What the App may do in Home Assistant

The App talks to Home Assistant only through the Supervisor's API proxy, and the backend enforces an allowlist
before sending anything (`backend/aie/ha.py`):

- **Reads**: states, the entity / label / category / area / floor / device registries, the user list (to check
  that you are an administrator), `automation/config`, `blueprint/list`, `validate_config`, and Home Assistant's
  automation editor endpoint `GET /api/config/automation/config/<id>`.
- **Writes, only within a confirmed operation and only for its automations**: `POST` (restore) and `DELETE`
  (cleanup) on the editor endpoint, `config/entity_registry/update` of those automation entities,
  `automation.turn_on` / `turn_off` / `reload` of those automations, `homeassistant/expose_entity` for them,
  and creating a missing label, automation category, area or blueprint that the plan lists. Ids in Automation
  Builder's namespace are refused for every write.

There is no way through this App to call any other service, change other entities, or delete a label, category,
area, blueprint, script, scene or helper. `.storage` is never read or written.

### Home Assistant interfaces used, and their limits

- `/api/config/automation/config/<id>` is the endpoint of Home Assistant's own automation editor. It is not part of
  the documented REST API and may change between Home Assistant versions; everything that depends on it is in
  `AutomationConfigApi`. A `DELETE` also removes the entity's registry entry; Home Assistant keeps it as a
  "deleted entity" and gives some settings back when the same id is created again – the App sets and verifies
  every field anyway.
- `automation/config` (websocket) returns what an automation entity runs: for external YAML it has `!include` and
  `!secret` resolved and a blueprint substituted, so such exports are read-only copies.
- Registry websocket commands (`config/*_registry/*`, `homeassistant/expose_entity`) are those of Home Assistant's
  frontend; categories get random ids when created, labels and areas ids derived from their names.
- `blueprint/save` installs a missing blueprint (never overwriting one).

## Known limitations

- `homeassistant: customize:` settings in YAML cannot be read or written through Home Assistant's API; registry
  names and icons (what the UI sets) are restored.
- Automations from other YAML files cannot be deleted here (by design), and `automations.yaml` entries without an
  id cannot be addressed by Home Assistant's editor endpoint (Home Assistant gives them an id at the next save in
  its editor).
- A restored automation's "last triggered" time and its traces are not restored.
- Registry options other than assistant exposure are archived but not restored.
- Archives from another installation map labels, categories and areas by name; entities, devices and scripts the
  automations refer to are only reported when missing.

## Backup

Archives (`/data/archives`), the operation journal (`/data/operations`) and the installation id are in the App's
`/data` folder, included in Home Assistant's backups of the App.

## Testing

`cd automation_import_export && pytest` runs the unit tests against a faithful fake of Home Assistant 2026.11.
`tests/e2e/` holds the end-to-end checks against a real, disposable Home Assistant with the real App image
(`stack.sh`, `prepare.py`, `e2e_checks.py`, `browser_checks.py`); see docs/AIE-0.1-import-export-manager.md.
