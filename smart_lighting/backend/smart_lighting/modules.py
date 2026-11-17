"""The App's modules and their migration status.

Phase A1 delivers only the foundation (Dashboard, Diagnostics, Settings).
Every feature module is *pending*: its screen says so and offers no controls.
Until a module is migrated and its replacement explicitly approved, the
existing integration (custom_components/smart_occupancy) keeps doing the work.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

AVAILABLE = "available"
PENDING = "pending"

# Which system is in control of a feature module. Exactly one at a time: a
# module moves to the App only after its replacement is validated and the
# matching integration function is switched off, so the integration and the
# App never control the same devices simultaneously.
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
    handled_by: str | None = None  # where this is configured today
    controlled_by: str | None = None  # INTEGRATION or APP for feature modules

    def __post_init__(self) -> None:
        if self.kind == "feature" and self.controlled_by is None:
            # Until a module is migrated and validated, the integration is in control.
            object.__setattr__(self, "controlled_by", APP if self.status == AVAILABLE else INTEGRATION)

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


MODULES: tuple[Module, ...] = (
    Module(
        "dashboard", "Dashboard", "view-dashboard", "foundation", AVAILABLE,
        "Connection, App health, integration detection and migration progress.",
    ),
    Module(
        "rooms", "Rooms", "floor-plan", "feature", PENDING,
        "Rooms, their areas and room status.",
        "The integration's Room Status card and its options "
        "(Settings → Devices & services → Smart Occupancy & Lighting).",
    ),
    Module(
        "presence", "Presence", "motion-sensor", "feature", PENDING,
        "Occupancy sensors, presence settings, zones and radar distance zones.",
        "The Smart Room Lighting card (Presence and zone settings).",
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
    """Migration progress. Only completed work counts; nothing is estimated."""
    features = [m for m in MODULES if m.kind == "feature"]
    migrated = [m for m in features if m.status == AVAILABLE]
    return {
        "current_phase": "A1",
        "foundation_complete": True,
        "modules_total": len(features),
        "modules_migrated": len(migrated),
        "percent": round(100 * len(migrated) / len(features)) if features else 0,
        "automatic_migration": False,
        "controlled_by_integration": sum(1 for m in features if m.controlled_by == INTEGRATION),
        "controlled_by_app": sum(1 for m in features if m.controlled_by == APP),
        "items": [
            {"key": "foundation", "title": "App foundation", "status": "done", "phase": "A1", "controlled_by": None},
            *(
                {"key": m.key, "title": m.title, "status": m.status, "phase": None, "controlled_by": m.controlled_by}
                for m in features
            ),
        ],
    }
