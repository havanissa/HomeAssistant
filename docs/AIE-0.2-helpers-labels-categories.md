# Automation Import/Export Manager 0.2: helpers, labels & categories

Backup, restore and a cleanup overview for helpers, labels and categories, in the existing independent App
(`automation_import_export/`, backend package `aie`). Target: Home Assistant 2026.11.1. No other App was changed.

## 1. What Home Assistant 2026.11.1 offers (read from its source)

| What | Read | Created / updated / deleted |
| --- | --- | --- |
| UI helpers (input_boolean, input_number, input_text, input_select, input_datetime, input_button, counter, timer, schedule) | `<domain>/list` (storage collection: the item with `id` and its full configuration) | `<domain>/create` (the id is the slug of the **name**, `_2` when taken), `<domain>/update` (`<domain>_id` + the whole configuration: replaces it), `<domain>/delete` |
| A helper's entity | entity registry (`platform` = domain, `unique_id` = item id) | `config/entity_registry/update` |
| Labels | `config/label_registry/list` | `create` (id = slug of the name), `update`, `delete` – deleting removes the label from every entity, device and area (and from deleted entities) |
| Categories (per scope: automation, script, helpers) | `config/category_registry/list` | `create` (random ULID id), `update`, `delete` – deleting removes it from every entity |
| Label assignments | entity / device / area registry lists (`labels`) | entity, device, area registry `update` |
| Integration helpers (template, group, utility meter …) | `config_entries/get` with `type_filter: ["helper"]`; their options only in `.storage/core.config_entries` | config flows (not done by the App) |

Findings that shaped the design:

- **Exact helper ids on restore**: the id comes from the name, so the App creates the helper with its archived id
  as the name and immediately updates it with the real configuration (the same two calls the helper editor makes).
- When an entity of the same unique id is created again, the entity registry restores the deleted entity's
  settings – helpful, but the App sets and verifies every field anyway (another installation has no deleted entity).
- `search/related` does not look into templates or dashboards, so usage is found by the App itself.

## 2. Design

- `registry.py` – `RegistrySnapshot` (helpers with configuration and registry settings, YAML helpers, integration
  helpers, labels, categories, entity/device/area registries) and the usage search. "Users" are automations
  (from the existing automation snapshot), scripts (`script/config`), scenes (their `entity_id` attribute),
  dashboards (`lovelace/config`; auto-generated ones are skipped), integration helpers (their options) and the
  YAML files of the configuration folder. Entity ids are found in the text (every `a.b` pair of a dotted chain, so
  `states.input_boolean.x.state` counts), registry ids as 32-digit hex. Labels: assignments plus `label_id`
  targets of automations and scripts. Categories: assignments.
- `haconfig.py` – read-only `yaml_texts()` (skips `secrets.yaml`, `.storage`, `custom_components`, `www`, backups,
  symbolic links; at most 1000 files of 2 MB) and `helper_entry_options()` (only the entries Home Assistant lists as
  helpers).
- `regarchive.py` – the archive format `automation-import-export-registry-archive` v1 (manifest with item
  fingerprints and file checksums, `data/{labels,categories,helpers}.json`, `helpers/<domain>.yaml`), read with the
  same `SafeZip` limits as automation archives, and `verify_registry()` (readable, checksums, records, YAML,
  selection, lossless comparison with Home Assistant).
- `regrestore.py` – the read-only plan (create / already there / use the existing one / update or replace on
  collision / settings only; assignment estimates) and the runner: labels → categories → labels and categories
  that restored helpers need → helpers (create + update, then entity settings and exposure) → assignments → a
  verification of every item against the archive.
- `regops.py` – the operations, journalled like the automation ones: backup, Back up & delete (archive, verify,
  preview with usage, `plan_hash`, typed count, archive and in-use acknowledgements, compare-before-delete,
  removal verification, reconciliation after an interruption) and restore (with a backup of everything it updates).
- `ha.py` – the allowlist gains the reads above and the writes; every write needs the item's key
  (`label:<id>`, `category:<scope>:<id>`, `helper:<domain>:<id>`) in the running operation's `WriteScope`. Other
  entities may only get `labels` / `categories`, devices and areas only `labels`.
- Interface: `static/registry.js` – the **Helpers & Labels** page (tabs, tiles, filters, *Where it is used*,
  Select all shown / Deselect all), the Back up & delete page, the restore preview (with Select all / Deselect
  all and the assignment switch) and the archive inspection.

## 3. Results (all executed; HA 2026.11.1, real App image, fresh disposable HA per run)

- Unit tests: 92 passed (12 new for helpers, labels and categories, plus updated allowlist / write-scope tests).
- End-to-end: 297/297 checks (new phase `registry`, 30 checks): usage found in a UI script template, a
  dashboard, a template helper's options, a YAML file; `secrets.yaml` never read; YAML and integration helpers
  read-only; label assignments on entities, a device and an area; backup verified and recognised on upload;
  Back up & delete refused without the in-use acknowledgement and nothing deleted before it; deleted helpers,
  labels and categories gone (label removed from every entity); restore created everything with the helper's
  original id and identical configuration, its entity settings, label, mapped category and exposure, the label
  back on the other entity, the device and the area; a second restore had nothing to do; a changed label was a
  collision skipped by default and updated on request after a backup; deletes only for the confirmed items; only
  allowlisted commands sent. Every 0.1 phase passes unchanged.
- Browser: 187/187 checks at desktop (light, dark), tablet (landscape, portrait) and phone: the page lists every
  helper, used/unused chips, *Where it is used*, label and category tabs, the *Not used* filter, Select all shown /
  Deselect all, no sideways scrolling, 44 px touch targets; on the desktop the whole Back up & delete → restore
  flow of an unused label through the interface.

## 4. Limitations

- Integration helpers and YAML helpers are listed with their uses but cannot be backed up, deleted or restored.
- Not searched: Node-RED / AppDaemon / other Apps, ESPHome outside the configuration folder, external services.
- A helper's value is not restored; restoring a registry archive has no rollback (the pre-restore backup can be
  restored instead).
- A template helper created seconds ago may not be searched yet (Home Assistant writes its config entries file
  shortly after).
