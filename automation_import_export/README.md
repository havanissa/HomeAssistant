# Automation Import/Export Manager

A Home Assistant App that exports, archives, cleans up and restores your automations **together with their
Home Assistant metadata**: labels, categories, areas, entity ids, names, icons, voice aliases, hidden/disabled
flags, assistant exposure and on/off states.

- **Discovery** of every automation: ordinary UI automations, Automation Builder's (protected), automations
  maintained in other YAML files (read-only) and integration-provided ones, with search, filters (area, category,
  label, state) and grouping.
- **Export** one automation, a selection, an area, a category, a label, all ordinary automations, or everything
  (backup-only mode) into a verified ZIP archive with the exact native YAML, the metadata definitions, the
  dependencies, the blueprint files and a versioned manifest with checksums.
- **Export & Delete Legacy Automations**: export → verify (including a lossless comparison with Home Assistant)
  → download → final preview → typed confirmation → delete → verify the removal and that protected automations
  are unchanged → report. Interrupted operations are recognised and reconciled.
- **Import & Restore** archives (from this or another installation) and ordinary automation YAML files: collision
  handling (skip, replace after a backup, restore as new), label / category / area mapping, blueprint
  installation, explicit acknowledgement of every deviation, verification against the archive and rollback.

- **Helpers, labels & categories**: see which helpers, labels and categories are used – and by what (automations,
  scripts, scenes, dashboards, template helpers, YAML files, entities, devices, areas) – back them up, delete the
  unused ones after a verified backup, and restore them with their ids, settings and assignments.

No companion integration; it needs none of the other Apps in this repository and changes none of them. It deletes
helpers, labels and categories only in a confirmed Back up & delete, never areas, scripts or scenes, never touches
Automation Builder's automations, and never uploads anything anywhere.

See [DOCS.md](DOCS.md) for installation and use.
