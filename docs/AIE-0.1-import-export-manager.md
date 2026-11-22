# Automation Import/Export Manager 0.1 (Phase 0.1)

An independent App (`automation_import_export/`, backend package `aie`) that exports, archives, deletes and
restores Home Assistant automations **with** their organisational metadata. Target: Home Assistant 2026.11.1.
No companion integration, no runtime dependency on Smart Comfort, Smart Occupancy Detection, Automation Builder or
Smart Predictive Climate, and none of them was changed.

## 1. How Home Assistant 2026.11.1 stores what must survive

Read from the 2026.11.1 source (`components/config/automation.py`, `config/view.py`, `helpers/entity_registry.py`,
`config/{entity,label,category,area}_registry.py`, `components/automation/*`, `components/blueprint/websocket_api.py`,
`components/homeassistant/exposed_entities.py`):

| What | Where | How the App reads it | How the App restores it |
| --- | --- | --- | --- |
| Configuration (alias, description, triggers, conditions, actions, variables, mode/max, trace, `use_blueprint`, unknown keys, legacy singular keys) | `automations.yaml` | `GET /api/config/automation/config/<id>` (the editor endpoint): the entry exactly as in the file, nothing normalised | `POST` to the same endpoint (validates, writes, reloads that id) |
| Configuration id | `id` in the entry; the entity's `unique_id` | registry + state attribute `id` + the file | the URL key; the body's `id` is set to it (the endpoint lets a body `id` override the URL) |
| Entity id, name, icon, area, labels, categories (per scope), voice aliases, hidden/disabled by user, options | entity registry | `config/entity_registry/list` + `get_entries` (extended) | `config/entity_registry/update` (`new_entity_id`, …) |
| Enabled (on/off) | restore state of the entity | `get_states` | `automation.turn_on` / `turn_off` |
| Label definitions (id, name, icon, colour, description) | label registry | `config/label_registry/list` | existing id → name match → `config/label_registry/create` |
| Category definitions (scope, id, name, icon) | category registry | `config/category_registry/list` per scope | existing id → name match → `create` (new random id, mapped) |
| Area definitions (+ floor) | area / floor registry | `config/area_registry/list`, `floor_registry/list` | existing id → name match → `create` |
| Assistant exposure | registry options `<assistant>.should_expose` | registry options | `homeassistant/expose_entity` |
| Blueprint | `blueprints/automation/<path>` | read-only folder map (no API returns blueprint files) | `blueprint/save` when missing (never overwriting) |
| External YAML automations | other files / packages | `automation/config` (what the entity runs) | read-only; restorable only as a copy (acknowledged) |

Findings that shaped the design:

- **`DELETE` on the editor endpoint removes the entity registry entry** (its post-write hook). Labels, categories,
  area, name … are therefore captured *before* any deletion, and restored explicitly. Home Assistant keeps the
  removed entry as a *deleted entity* and gives its settings back when the same unique id returns – helpful on the
  same installation, but the App never relies on it: it sets every field and verifies it.
- The editor endpoint writes `id` first and then the posted dict as is; `POST` validates with the same validator
  the editor uses (a refused configuration is a 400 with Home Assistant's message).
- Categories get random ids (ULID) when created; labels and areas get ids derived from their names. Category and
  area names must be unique (normalised), so "create" is refused in the plan when the name exists.
- Home Assistant writes default `should_expose` values into registry options by itself; options are therefore not
  part of the change detection, and exposure is compared explicitly.
- Home Assistant installs its default blueprints only when `blueprints/automation` does not exist yet.
- `automation/config` returns the substituted configuration for blueprint automations and resolves includes and
  secrets for external files; it is used only for external YAML (read-only).

Undocumented / internal interfaces used (all in `backend/aie/ha.py`, documented in DOCS.md):
`/api/config/automation/config/<id>` (not in the documented REST API; the frontend's), and the frontend's
websocket registry commands. Their behaviour was verified on a real 2026.11.1; a future change would show up in
the e2e checks and, at run time, in the App's verification (nothing is reported as restored unless it is).

## 2. Architecture

```
automation_import_export/
  config.yaml          ingress, panel_admin, homeassistant_api, map homeassistant_config (read-only), backup hot
  backend/aie/
    ha.py              websocket client + editor endpoint, allowlist, per-operation WriteScope
    haconfig.py        read-only: blueprint files, automations.yaml scan (duplicate / non-text / missing ids)
    inventory.py       Snapshot (fresh per operation) and classification (source, owner, blocking problems)
    deps.py            references (entities, scripts, scenes, helpers, devices, areas, floors, labels, services,
                       blueprint, template entities) and missing-dependency findings
    canonical.py       lossless comparison (types, list order; mapping key order not significant)
    yamlio.py          YAML dump/load (exact round trip, alias-bomb guard, tags kept as markers)
    archive.py         build, SafeZip (limits, unsafe names/members), Archive, verify (all 0.1.4 checks)
    restore.py         build_plan (staged, hashed) + RestoreRunner (metadata, writes, registry, state, verify)
    store.py           archives (0600, index), operation journal, installation id
    core.py            operations: export, Export & Delete, reconcile/resume, upload, YAML import, restore, rollback
    web.py             Ingress-only JSON API + static interface; admin, CSRF header, Origin, size limits
    static/            app.js (router, Overview, Archives, operations), export.js, restore.js, ui.js, app.css
  tests/               unit tests against a faithful fake of Home Assistant 2026.11
  tests/e2e/           real image + disposable Home Assistant: stack.sh, fake_supervisor.py, prepare.py,
                       fixtures.py, hactl.py, e2e_checks.py, browser_checks.py, ha/ (test configuration)
```

Safety properties enforced in code (and tested):

- The client refuses every command outside the allowlist and every write outside the running operation's scope
  (`check_params`); the editor endpoint refuses `POST`/`DELETE` for `automation_builder_*` ids in any case.
- Cleanup deletes only automations that are in `automations.yaml`, ordinary (or ambiguous *and* explicitly
  included), not blocked, in a verified archive, confirmed with the plan hash, the typed count and the archive
  acknowledgement – and unchanged when read again right before their own deletion.
- Labels, categories, areas, blueprints, scripts, scenes and helpers are never deleted.
- Every operation is journalled before and after each step; interrupted operations are marked at start-up and
  reconciled against Home Assistant before anything else can continue.
- A restore is verified field by field against the archive; anything missing makes it *incomplete*.

## 3. Archive format (version 1)

See DOCS.md, "The archive". The manifest holds: format and version, archive id, creation time, App version,
purpose and selection, Home Assistant version / location / time zone / country / language, the source installation
(App installation id, location, a fingerprint of the area ids – no credentials), the inventory (per automation:
ids, source, owner, protection, restorability, file, SHA-256 of configuration and registry entry, blueprint,
problem codes, completeness), the exact associations (entity id, labels, categories, area, name, icon, aliases,
flags, state), counts, warnings, compatibility notes and the SHA-256 and size of every file.

## 4. Tests

- Unit tests (`cd automation_import_export && pytest`): **78 passed** – archive safety (traversal, absolute paths,
  backslashes, links, encryption, sizes, ratio, member count, lying sizes, YAML alias bombs), exact YAML round trips,
  every verification check incl. tampering and live changes, classification edge cases (duplicate / non-text ids,
  unknown definitions, integration and orphan entries, unaddressable ids), cleanup guards and change detection,
  interrupted export / deletion (reconcile, resume), restore collisions, mapping, acknowledgements, blueprints,
  partial failures, rollback, YAML import, allowlist and write scope, web layer, packaging and independence.
- End-to-end against a real Home Assistant 2026.11.1 with the real App image (`tests/e2e/`): all phases passed –
  setup, **round trip** (10 ordinary automations exported, verified, deleted, restored: configuration, entity id,
  labels incl. four on one automation, category, area, name, icon, aliases, hidden / disabled flags, assistant
  exposure and on/off state identical; Automation Builder's, the ambiguous and the external automations unchanged),
  metadata recreation (deleted label, category and area recreated and mapped), missing blueprint installed from the
  archive, change between export and deletion skipped, collisions (metadata only / skip / replace with backup / new
  id / rollback), 11 malicious or corrupted archives refused, YAML import, duplicate ids blocked, interrupted export,
  deletion and restore (App killed mid-operation, reconciled, continued), refused write → incomplete + rollback,
  App restart, security, allowlist and "no write to Automation Builder".
- Browser checks (Playwright through the Ingress panel): 122 checks at desktop light/dark, tablet landscape and
  portrait, phone – no console errors, no sideways scrolling, 44 px touch targets, the full Export & Delete and
  restore workflow in the interface, and Home Assistant's own automation dashboard showing the restored categories,
  labels, areas and custom names.
- Repository regression: Automation Builder 201, Smart Occupancy Detection 678, Smart Predictive Climate 154,
  integration suites (automation_builder 58, companion 43, engine 1261, sod_companion 38, spc_companion 71) passed; Smart Comfort 326 passed, 1 failed – the
  expected App-list assertion described in section 5.

## 5. Known limitations

- `homeassistant: customize:` (YAML) cannot be read or written through the API; registry names/icons are restored.
- External YAML automations are exported read-only (as Home Assistant runs them) and never deleted; entries of
  `automations.yaml` without an id or with a non-text id cannot be addressed by the editor endpoint.
- "Last triggered" and traces are not restored; registry options other than assistant exposure are archived only.
- Same-installation restores benefit from Home Assistant's deleted-entity records; a cross-installation restore
  was simulated by deleting the label, category and area definitions (Home Assistant also removes them from its
  deleted-entity records), not with a second Home Assistant.
- Smart Comfort's repository test `smart_lighting/tests/test_package.py::test_only_one_app_config_in_repository`
  lists every App's `config.yaml`; earlier App PRs updated that list, this phase was told not to modify Smart
  Comfort, so that one assertion needs the new App added (a one-line test change) – see the PR.
- Not installed on Home Assistant OS or production in this phase.
