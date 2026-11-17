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
unchanged; Phase A3.2 may move the occupancy engine, only after approval.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

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
    kind: str  # "foundation" or "feature"
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
        if self.kind != "feature":  # foundation and tools (the floor plan) migrate nothing
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
        "Occupancy of every room, sensors and their roles, occupancy timing, a live occupancy debugger and "
        "presence diagnostics.",
        "This App (Presence). The integration's room card (Presence tab) and options flow keep working on the same "
        "configuration. Lighting zones and radar distance zones are still edited in the Smart Room Lighting card.",
        scope="Presence configuration: sensor roles, inverted and included signals, global occupancy timing and "
              "room overrides; live engine view, timeline and diagnostics. Stored in the integration; the "
              "integration's occupancy engine keeps deciding occupancy (it moves only in Phase A3.2, after approval).",
        phase="A3.1",
    ),
    Module(
        "lighting", "Lighting", "lightbulb-group", "feature", PENDING,
        "Automatic lighting, vacancy, ambient light levels and brightness rules.",
        "The Smart Room Lighting card.",
    ),
    Module(
        "moods", "Moods", "palette", "feature", PENDING,
        "Moods, light types and Occupied / Vacant lighting profiles.",
        "The Smart Lighting Profiles card.",
    ),
    Module(
        "switches", "Switches", "light-switch", "feature", PENDING,
        "Wall switches, remotes, button actions and Priority Inputs.",
        "The Smart Room Lighting card (switch settings).",
    ),
    Module(
        "curtains", "Curtains", "curtains", "feature", PENDING,
        "Windows, covers, presets, Global Modes and the Global Schedule.",
        "The Smart Curtains card.",
    ),
    Module(
        "predictions", "Predictions", "crystal-ball", "feature", PENDING,
        "Occupancy forecasts and their accuracy (observation only).",
        "The forecast views of the Smart Room Lighting card and the forecast sensors.",
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


def modules() -> list[dict[str, str | None]]:
    return [m.as_dict() for m in MODULES]


def migration() -> dict[str, object]:
    """Migration progress. Only completed work counts; nothing is estimated.

    ``modules_migrated`` counts modules whose *automation* runs in the App (none
    yet); ``interfaces_migrated`` counts modules configured in the App.
    """
    features = [m for m in MODULES if m.kind == "feature"]
    migrated = [m for m in features if m.automation_owner == APP]
    interfaces = [m for m in features if m.gui_owner == APP]
    return {
        "current_phase": "A3.1",
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
