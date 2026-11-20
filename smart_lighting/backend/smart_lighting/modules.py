"""The App's modules and their migration status.

Three separate questions per feature module, because they migrate at
different times:

* ``gui_owner`` - where the module is configured: the existing integration's
  dashboard cards / options (``integration``) or this App (``app``).
* ``config_source`` - where the authoritative configuration is stored. Today
  this is always the integration (its config entry options and stores); the
  App edits it through the integration's own validated commands.
* ``automation_owner`` / ``controlled_by`` - which system executes the
  automation and controls devices. Exactly one at a time: a module's
  automation moves to the App only after its replacement is validated and the
  matching integration function is switched off, so both never control the
  same devices. In Phase A2 this is the integration for every module.

Phase A2 moved the Rooms configuration interface (room, sensor and light
assignments) to the App. Phase A3.1 moves the Presence interface: occupancy
sensors and their roles, occupancy timing, the live occupancy debugger and
presence diagnostics. Room occupancy itself (the engine and its entities),
lighting, switches, curtains and predictions keep running in the integration,
unchanged.

Phase A3.2 adds the occupancy engine to the App: it runs in shadow mode next to
the integration's, and decides room occupancy only after a reviewed handover
that the integration acknowledges. ``modules()`` and ``migration()`` take that
acknowledged owner, so Presence counts as migrated only while the App's engine
really publishes occupancy.

Phase A4 moves Lighting, Moods and Switches together (one tightly coupled
subsystem, one lighting owner): their interfaces are in the App; the
integration's lighting runtime runs in the App in shadow mode and, after a
reviewed handover the integration acknowledges, controls the lights and owns the
lighting configuration. The three count as migrated only while that is so.

Phase A7.0 adds Climate, the first *native* module (kind ``native``): it was never part of the integration, so
it has no migration status and does not count in the migration progress. It observes only.

Phase A5 moves Curtains and Predictions the same way, each with its own
ownership record: the curtain runtime commands covers only after its reviewed
handover (through the integration's fenced executor); the forecast runtime
produces the forecasts only after its handover (observation only, no device
command on either side).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

AVAILABLE = "available"  # the App is in control (automation migrated)
GUI = "gui"  # the configuration interface is in the App; the integration still executes everything
PENDING = "pending"

INTEGRATION = "integration"
APP = "app"


@dataclass(frozen=True)
class Module:
    key: str
    title: str
    icon: str  # mdi icon name, resolved by the frontend
    kind: str  # "foundation", "feature", "tool" or "native"
    status: str
    summary: str
    handled_by: str | None = None  # where this is configured today (text)
    controlled_by: str | None = None  # INTEGRATION or APP: who executes the automation
    gui_owner: str | None = None
    config_source: str | None = None
    automation_owner: str | None = None
    scope: str | None = None  # what the App's interface covers (GUI modules)
    phase: str | None = None  # the App phase that moved its interface

    def __post_init__(self) -> None:
        if self.kind != "feature":  # foundation, tools (the floor plan) and native modules (Climate) migrate nothing
            return
        automation = APP if self.status == AVAILABLE else INTEGRATION
        object.__setattr__(self, "automation_owner", self.automation_owner or automation)
        object.__setattr__(self, "controlled_by", self.controlled_by or self.automation_owner)
        object.__setattr__(self, "gui_owner", self.gui_owner or (APP if self.status in (AVAILABLE, GUI) else INTEGRATION))
        object.__setattr__(self, "config_source", self.config_source or INTEGRATION)

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


MODULES: tuple[Module, ...] = (
    Module(
        "dashboard", "Dashboard", "view-dashboard", "foundation", AVAILABLE,
        "Connection, App health, integration detection and migration progress.",
    ),
    Module(
        "rooms", "Rooms", "floor-plan", "feature", GUI,
        "Rooms, their areas, devices, sensor and light assignments, and live room diagnostics.",
        "This App (Rooms). The integration's own room editor (Configure → Rooms) keeps working on the same "
        "configuration.",
        scope="Room configuration: enabled rooms, sensor and light assignments, sensor kind and role, inverted "
              "signals, reference occupancy entity and adjacent rooms. Stored in the integration; the integration "
              "keeps running room occupancy.",
        phase="A2",
    ),
    Module(
        "floorplan", "Floor plan", "home-map-marker", "tool", AVAILABLE,
        "Draw floors and rooms, link them to areas, place devices and watch them live (Phase A2.1).",
        "This App. Stored in the App's own storage; it is not part of the integration's configuration and changes "
        "no automation.",
    ),
    Module(
        "presence", "Presence", "motion-sensor", "feature", GUI,
        "Occupancy of every room, sensors and their roles, zones (parts of a room with their own detection, radar "
        "distance zones included), occupancy timing, a live occupancy debugger and presence diagnostics.",
        "This App (Presence). Zones are edited in Presence → a room → Zones since App 0.9.0; which lights respond "
        "to them is set in Lighting → a room → Zone lighting.",
        scope="Presence configuration: sensor roles, inverted and included signals, global occupancy timing and "
              "room overrides; live engine view, timeline and diagnostics; the occupancy engine (Phase A3.2: shadow "
              "comparison, reviewed handover and rollback). Stored in the integration; the integration publishes "
              "the room occupancy entities.",
        phase="A3.2",
    ),
    Module(
        "lighting", "Lighting", "lightbulb-group", "feature", GUI,
        "Automatic lighting, vacancy, daylight, ambient light levels and brightness rules (with Delay On / Delay "
        "Off per cell), zone lighting rules, shared spaces, the Room Supervisor and the light inspector (Identify).",
        "This App (Lighting). Until the handover the integration's cards keep working on the same configuration; "
        "afterwards they are read-only.",
        scope="Lighting configuration and live decisions; the lighting runtime itself (Phase A4: shadow comparison, "
              "reviewed handover and rollback).",
        phase="A4",
    ),
    Module(
        "moods", "Moods", "palette", "feature", GUI,
        "Moods and their source, light types and Occupied / Vacant lighting profiles.",
        "This App (Moods).",
        scope="Mood source (helpers, one selector or the built-in selector), mood inheritance and fallback, light "
              "types, Occupied and Vacant profiles, previews and explicit tests.",
        phase="A4",
    ),
    Module(
        "switches", "Switches", "light-switch", "feature", GUI,
        "Wall switches, remotes, button actions and Priority Inputs.",
        "This App (Switches).",
        scope="Wall switches and remotes (event entities, MQTT, device triggers), button mappings, Observing / "
              "Active, manual overrides and Priority Inputs.",
        phase="A4",
    ),
    Module(
        "curtains", "Curtains", "curtains", "feature", GUI,
        "Windows, covers, presets, Global Modes, the Global Schedule, the environment and curtain diagnostics.",
        "This App (Curtains). Until the handover the Smart Curtains card keeps working on the same configuration; "
        "afterwards it is read-only.",
        scope="Curtain configuration and live decisions; the curtain runtime itself (Phase A5: shadow comparison, "
              "reviewed handover and rollback; covers are moved only through the integration's fenced executor).",
        phase="A5",
    ),
    Module(
        "predictions", "Predictions", "crystal-ball", "feature", GUI,
        "Occupancy forecasts, learned patterns, accuracy and their settings (observation only).",
        "This App (Predictions). Until the handover the integration learns and publishes; afterwards its views are "
        "read-only and its forecast sensors show what the App publishes.",
        scope="Forecast settings, room connections, learning, patterns and accuracy; the forecast runtime itself "
              "(Phase A5: shadow comparison, reviewed handover with the learned data, rollback). No device is ever "
              "commanded.",
        phase="A5",
    ),
    Module(
        "climate", "Climate", "thermometer", "native", AVAILABLE,
        "Room temperatures, thermostats and their reported activity, windows and doors, thermal zones and the "
        "observations predictive heating will learn from (Phase A7.0: observing only, no thermostat is controlled).",
        "This App (Climate). Stored in the App's own storage; it never existed in the integration, so nothing "
        "migrates.",
        scope="Climate configuration (thermostats, room temperature sensors, windows and doors, the outdoor source, "
              "comfort and setback preferences, safe bounds, thermal zones), live monitoring and the observation "
              "history. Observing only: Smart Comfort sends no thermostat command.",
        phase="A7.0",
    ),
    Module(
        "diagnostics", "Diagnostics", "stethoscope", "foundation", AVAILABLE,
        "Health checks, connection details, safety status and recent log.",
    ),
    Module(
        "settings", "Settings", "cog", "foundation", AVAILABLE,
        "Appearance and App information.",
    ),
)


LIGHTING_MODULES = ("lighting", "moods", "switches")


def _current(occupancy_owner: str, lighting_owner: str = INTEGRATION, curtains_owner: str = INTEGRATION,
             forecast_owner: str = INTEGRATION, config_authority: str = INTEGRATION) -> tuple[Module, ...]:
    """The modules with the automation owners as the integration (or the companion) acknowledges them."""
    occupancy = APP if occupancy_owner == APP else INTEGRATION
    lighting = APP if lighting_owner == APP else INTEGRATION
    curtains = APP if curtains_owner == APP else INTEGRATION
    forecast = APP if forecast_owner == APP else INTEGRATION
    core = APP if config_authority == APP else INTEGRATION
    out = []
    for m in MODULES:
        if m.key == "presence":
            m = replace(m, automation_owner=occupancy, controlled_by=occupancy, config_source=core)
        elif m.key == "rooms":
            m = replace(m, config_source=core)
        elif m.key in LIGHTING_MODULES:
            # One owner for the whole lighting subsystem; its configuration moves with it.
            m = replace(m, automation_owner=lighting, controlled_by=lighting, config_source=lighting)
        elif m.key == "curtains":
            m = replace(m, automation_owner=curtains, controlled_by=curtains, config_source=curtains)
        elif m.key == "predictions":
            m = replace(m, automation_owner=forecast, controlled_by=forecast, config_source=forecast)
        if m.kind == "feature" and m.automation_owner == APP:
            m = replace(m, status=AVAILABLE)
        out.append(m)
    return tuple(out)


def modules(occupancy_owner: str = INTEGRATION, lighting_owner: str = INTEGRATION, curtains_owner: str = INTEGRATION,
            forecast_owner: str = INTEGRATION, config_authority: str = INTEGRATION) -> list[dict[str, str | None]]:
    return [m.as_dict() for m in _current(occupancy_owner, lighting_owner, curtains_owner, forecast_owner,
                                          config_authority)]


def migration(occupancy_owner: str = INTEGRATION, lighting_owner: str = INTEGRATION, curtains_owner: str = INTEGRATION,
              forecast_owner: str = INTEGRATION, config_authority: str = INTEGRATION) -> dict[str, object]:
    """Migration progress. Only completed work counts; nothing is estimated.

    ``modules_migrated`` counts modules whose *automation* runs in the App;
    ``interfaces_migrated`` counts modules configured in the App.
    """
    features = [m for m in _current(occupancy_owner, lighting_owner, curtains_owner, forecast_owner, config_authority)
                if m.kind == "feature"]
    migrated = [m for m in features if m.automation_owner == APP]
    interfaces = [m for m in features if m.gui_owner == APP]
    return {
        "current_phase": "A6",
        "config_authority": config_authority,
        "foundation_complete": True,
        "modules_total": len(features),
        "modules_migrated": len(migrated),
        "interfaces_migrated": len(interfaces),
        "percent": round(100 * len(migrated) / len(features)) if features else 0,
        "interface_percent": round(100 * len(interfaces) / len(features)) if features else 0,
        "automatic_migration": False,
        "controlled_by_integration": sum(1 for m in features if m.controlled_by == INTEGRATION),
        "controlled_by_app": sum(1 for m in features if m.controlled_by == APP),
        "items": [
            {"key": "foundation", "title": "App foundation", "status": "done", "phase": "A1", "controlled_by": None,
             "gui_owner": None, "config_source": None, "automation_owner": None},
            *(
                {"key": m.key, "title": m.title, "status": m.status, "phase": m.phase if m.status == GUI else None,
                 "controlled_by": m.controlled_by, "gui_owner": m.gui_owner, "config_source": m.config_source,
                 "automation_owner": m.automation_owner}
                for m in features
            ),
        ],
    }
