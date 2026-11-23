# Changelog

## 0.2.0

- **Helpers, labels & categories** – a new page that lists every helper, label and category with what uses it,
  so unused ones are easy to find and clean up:
  - helpers (Settings → Helpers: toggles, numbers, texts, dropdowns, dates/times, buttons, counters, timers,
    schedules): used by automations, scripts, scenes, dashboards, integration helpers (templates, groups …) and
    YAML files, found by entity id (also inside templates) or by the entity registry id that device triggers use.
    Helpers defined in YAML and integration helpers are listed read-only, with their uses;
  - labels: the entities, devices and areas that carry them, and automations and scripts that target them;
  - categories (automations, scripts, helpers): the entities that carry them;
  - filters (not used / used, type, scope), search, *Where it is used*, Select all shown / Deselect all.
- **Back up** selected or all helpers, labels and categories into a verified archive that also records where every
  label and category was assigned.
- **Back up & delete**: archive and verify first, then the final list with the uses of every item, a typed
  confirmation and an extra confirmation for items that are still in use. Each item is compared with the archive
  right before it is deleted; one that changed is skipped.
- **Restore** helpers with their original ids, configuration and entity settings (entity id, name, icon, area,
  labels, categories, aliases, hidden/disabled, assistant exposure), labels and categories (same id, else same
  name, else created) and put labels and categories back on the entities, devices and areas that carried them.
  Items that exist with other settings are skipped unless you choose to update them (after a backup).
- Archives of helpers, labels and categories are listed with the automation archives; they can be downloaded,
  inspected, verified again, uploaded and restored on another installation.

## 0.1.2

- **Select all / Deselect all** on the restore and import preview: start from nothing and tick only the
  automations you want, or tick everything at once, instead of unticking each one. A counter shows how many of the
  archive's automations are selected.

## 0.1.1

- **Fix: the interface did not load on installations with many automations** ("NetworkError when attempting to
  fetch resource"). Home Assistant's automation editor endpoint re-reads the whole `automations.yaml` for every
  single automation, one request at a time, so listing a few hundred automations took a minute or more. The
  Overview and Export pages now read `automations.yaml` once (read-only, with the fast YAML parser): 0.2 s instead
  of 61 s for 415 automations. The editor endpoint is still used for every automation an operation exports,
  verifies, deletes or restores, so archives stay verified against Home Assistant itself.
- A clearer message when the App does not answer.

## 0.1.0

First version (Phase 0.1): an independent App for exporting, archiving, cleaning up and restoring Home Assistant
automations with their metadata.

- Discovery of every automation with its source (automations.yaml, other YAML files, integrations), owner
  (ordinary, Automation Builder, ambiguous), labels, categories, area, blueprint, state and dependencies; filters,
  search and grouping by category, area, label or source.
- Versioned ZIP archives (format version 1): exact native YAML per automation and as one list, registry metadata,
  label / category / area / floor definitions, dependencies, blueprint files, manifest with inventory, exact
  associations, Home Assistant version, source installation, warnings, compatibility and SHA-256 checksums.
- Verification of every archive: readability, checksums, complete configurations and metadata, YAML parsed back
  and compared exactly, identities, dependencies, no omission, protected automations excluded, and a lossless
  comparison with Home Assistant.
- Export & Delete Legacy Automations with a deletion preview, typed confirmation, per-automation change detection
  right before each deletion, removal verification, protected-automation check, report and a durable journal
  (interrupted deletions are reconciled and can be continued or restored).
- Restore from this App's archives and from ordinary automation YAML files: staged plan, collisions (skip /
  replace after a backup / new), label, category and area mapping or recreation, entity ids, enabled and disabled
  states, hidden flags, aliases, assistant exposure, missing blueprints installed from the archive, verification
  of the result (an incomplete restoration is reported as such) and rollback.
- Ingress-only interface for administrators (Overview, Export, Import, Archives) for desktop, tablet and phone,
  light and dark theme.
