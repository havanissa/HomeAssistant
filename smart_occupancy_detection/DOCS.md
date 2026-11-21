# Smart Occupancy Detection

Smart Occupancy Detection decides whether each room is occupied, using the sensors already in Home Assistant,
and shows the result as native entities that ordinary automations, scripts and blueprints can use.

It **only detects occupancy**. It never controls lights, covers, switches, thermostats, scenes or any other
device: the App has no way to call a Home Assistant service.

## Installation

1. **The App.** Add this repository to the App store (Settings → Apps → App store → ⋮ → Repositories) if it is not
   there yet, install **Smart Occupancy Detection** and start it. It opens from the sidebar (**Occupancy**).
2. **The companion integration** (for the native entities). Copy the folder
   `custom_components/smart_occupancy_detection` from this repository into the `custom_components` folder of your
   Home Assistant configuration (for example with the File editor or Samba App), restart Home Assistant, then add
   it: Settings → Devices & services → Add integration → **Smart Occupancy Detection**. There is nothing to fill in.
   (HACS installs only one integration per repository, and this repository's HACS entry is Smart Comfort's
   integration, so this one is copied by hand.)
3. **Pair.** In the App, open Settings and press **Pair with Home Assistant**. Only an administrator can do this.
   From then on the App publishes the occupancy of every room, also after restarts.

Smart Comfort is not affected: both Apps and both integrations can be installed at the same time. They use
different slugs, domains, entity ids, unique ids and storage, and neither reads the other's data.

## Adding a room

A room is a Home Assistant **area** (create areas in Home Assistant first). Rooms → **Add a room**:

1. choose the area;
2. the App lists the binary sensors found in that area, with a suggested type. Nothing is selected for you: add the
   sensors that should take part (or **Add all suggested**). **Add another sensor** finds any entity, in any area,
   for example a PC activity helper, a bed sensor or a motion sensor shared with the next room;
3. check each sensor's type and role, then **Review changes** and **Save room**.

The review lists what changes and whether the room's decision would change **immediately** (for example "stays
Occupied: no immediate change", or "changes immediately: Vacant → Occupied"). An invalid configuration cannot be
saved; the review says why.

### Sensor types and roles

| Type | Suggested role | What it does |
|---|---|---|
| Motion (PIR) | Triggers occupancy | A movement makes the room (possibly) occupied. A motion sensor that stays on far too long is ignored as stuck. |
| Presence (mmWave radar) | Maintains occupancy | While it reports presence the room stays occupied; short dropouts are bridged. |
| Door contact | Movement / exit evidence | Opening or closing is activity, but never proves that someone is inside. A door movement with nothing new afterwards is evidence that someone left (faster vacancy), stronger when there is movement in an adjacent room right after. |
| PC activity | Maintains occupancy | An active PC keeps the room occupied. |
| Bed occupancy | Maintains occupancy | Someone in bed keeps the room occupied. |

Roles can be changed: **Triggers**, **Maintains**, **Maintains, and confirms vacancy when clear** (a sensor that
reliably sees everyone, so its "clear" allows the fast exit timeout), or **Ignored**. **Inverted** means the sensor
is "active" when it is off (for example a "PC idle" sensor). A sensor can be excluded temporarily (unticked) and
keeps its settings.

**Advanced** (in a room's editor): **adjacent rooms** (movement there right after a door movement here counts as
exit evidence) and **activity entities** - entities such as wall switches, remotes or buttons whose change keeps
an *occupied* room occupied for longer. They never make a room occupied on their own.

## How occupancy is decided

| State | Meaning | Binary sensor |
|---|---|---|
| `vacant` | Quiet for long enough, or never occupied | off |
| `possible_occupancy` | New activity that is not confirmed yet (one movement) | on |
| `confirmed_occupancy` | Two sensors agree, or presence lasted the confirmation time | on |
| `possible_vacancy` | Nothing active any more; waiting for the vacancy timeout | on |
| `uncertain` | No usable sensor (all unavailable): the last value is kept | last value |

A door movement alone shows `possible_occupancy` with the binary sensor **off**.

### Timing (Settings, and per room)

| Setting | Default | |
|---|---|---|
| Occupancy confirmation | 10 s | How long movement must last, or within how long two sensors must agree |
| Vacancy timeout | 10 min | Quiet time before a room becomes vacant (normal case) |
| Fast exit timeout | 5 min | Quiet time that is enough when there is evidence that everyone left |
| Advanced: radar dropout grace | 30 s | A presence sensor clearing for less than this still counts |
| Advanced: motion sensor stuck after | 30 min | (0 = never) |
| Advanced: presence sensor stuck after | off | (0 = never) |
| Advanced: start-up hold | 2 min | After a start, or after the connection to Home Assistant was interrupted, no room is made vacant for this long |

Rooms use the global values unless a value is set in the room.

### Outages and restarts

- An **unavailable sensor** is never "off": a presence sensor that drops out while reporting presence holds the
  room for longer; when every sensor of a room is unavailable the room is `uncertain` and keeps its last value.
- When **the App restarts**, every room starts from its previous decision: an occupied room stays occupied, its
  vacancy timer starts again and nothing becomes vacant during the start-up hold.
- While the **connection to Home Assistant** is down, decisions are frozen. When it is back, every sensor is read
  again and the start-up hold applies (nothing is known about the gap).
- When **the App is away** (stopped, updating, crashed), the entities keep the last decision - an absent App never
  makes a room vacant. Their attribute `fresh` becomes `false`, the reason says that the App is not connected, and
  `binary_sensor.sod_app_connected` turns off. After 30 s without a message from the App the results count as not
  fresh.

## Entities

For a room whose area is "Kamar Suður":

| Entity | |
|---|---|
| `binary_sensor.sod_kamar_sudur_occupied` | **The primary output**: occupied (on) / vacant (off) |
| `sensor.sod_kamar_sudur_state` | `vacant`, `possible_occupancy`, `confirmed_occupancy`, `possible_vacancy`, `uncertain` |
| `sensor.sod_kamar_sudur_confidence` | Evidence quality, 0-100 % (not a probability) |
| `sensor.sod_kamar_sudur_reason` | Why, in words |
| `sensor.sod_kamar_sudur_last_activity` | When something last happened in the room |
| `binary_sensor.sod_app_connected` | The App is connected and its results are fresh |

The entity ids are chosen once, from the area's name. The room's identity is the area id, so renaming the area
later renames the device and the friendly names but keeps every entity id and its history. A room whose detection
is turned off keeps its entities, unavailable. Deleting a room in the App deletes its entities. Entities are never
deleted for any other reason: if a room is missing for another reason (a reset or reinstalled App, an import
without it), its entities stay, unavailable, until the room is configured again or you delete its device in
Home Assistant.

Example automation:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.sod_kamar_sudur_occupied
    to: "off"
    for: "00:02:00"
actions:
  - action: light.turn_off
    target:
      area_id: kamar_sudur
```

(The automation switches the light - Smart Occupancy Detection never does.)

## Settings

- **Connection**: Home Assistant and the companion integration; pairing.
- **Detection timing**: the global defaults.
- **Backups and transfer**: export the configuration, import an export (reviewed), restore one of the automatic
  backups (one is made before every change; the last 30 are kept), and **Import from Smart Comfort**.
- **Diagnostics**: the engine's self-test, counters, and a downloadable diagnostics file.

### Import from Smart Comfort

In Smart Comfort, download its configuration export (or a rooms backup or migration backup), then upload it here.
The review lists the rooms with their sensors, roles, inversion, timing and adjacent rooms; lights, moods,
curtains, switches and climate are ignored. Choose the rooms, optionally the global timing, preview, import.
Smart Comfort is not changed and keeps running. Importing is optional: a fresh setup works without it.

## Recovery

The configuration (`/data/config/config.json`) is versioned and written atomically; the previous version is kept
as `config.json.bak` and in `backups/`. If the file is ever damaged, the App uses the previous copy (or the newest
backup), keeps the damaged file aside and shows a notice. The occupancy decisions (`/data/state/`) are restored at
start; if they cannot be read the App starts from the sensors.

## Future: replacing Smart Comfort's occupancy entities (not done in 0.1)

0.1 runs beside Smart Comfort with its own entities. When you want existing automations to use these results, the
safe procedure is:

1. run both side by side and compare `binary_sensor.sod_<room>_occupied` with Smart Comfort's room occupancy
   entities for some days (the Recorder history shows both);
2. change your automations to the `sod_` entities one at a time (or use the Home Assistant entity settings to give
   the `sod_` entities the ids your automations use, after renaming the old ones - never two entities with one id);
3. only then remove the room from Smart Comfort.

No automatic handover exists in 0.1, and Smart Comfort's entities are never touched.

## Privacy and security

Everything runs locally; there is no cloud connection. The interface is reachable only through Home Assistant's
Ingress, and only administrators can change the configuration. The App reads states and registries and talks to
its companion integration; it cannot call services, write states or fire events. The companion accepts results
only from the paired App session holding the current lease (session, epoch, lease token, increasing sequence).
