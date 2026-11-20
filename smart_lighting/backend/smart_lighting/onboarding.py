"""Fresh installation (Phase A6): the guided setup of a new home, without the monolithic integration.

The setup page lists what a new installation needs, in order, with its state:
the companion (optional, for native entities and device control), the core
configuration (created here with safe defaults: no rooms, no mood source,
nothing automatic), rooms, occupancy sensors, lighting, moods, switches,
optionally curtains and predictions, optionally a floor plan, and the explicit
review that enables automatic control per room. Nothing commands a device
because a step was completed: every room stays Observing until it is switched
to Automatic in Lighting (and a window in Curtains).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .companion_client import CompanionError
from .core_config import CoreConfigError, default_options

if TYPE_CHECKING:
    from .core import AppCore


class OnboardingError(Exception):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class OnboardingService:
    def __init__(self, core: AppCore) -> None:
        self.core = core

    async def start_fresh(self, user_id: str | None) -> dict[str, Any]:
        """Create the App's core configuration for a new installation (safe defaults, no legacy data needed)."""
        core = self.core
        if core.core_config.options is not None:
            raise OnboardingError("exists", "The App already has a configuration", 409)
        report = core.ha.integration if core.ha else None
        if core.companion is not None and core.companion.state == "absent" and report is not None and report.detected:
            raise OnboardingError("legacy_installed", "The monolithic integration (0.9.0) is installed: use "
                                                      "Settings → Migration & System to move its configuration "
                                                      "instead of starting fresh", 409)
        legacy = (core.companion.hello.get("legacy") or {}) if core.companion is not None else {}
        if core.companion is not None and core.companion.present and legacy.get("present"):
            raise OnboardingError("legacy_present", "The companion still holds the configuration of the monolithic "
                                                    "integration: use Settings → Migration & System (Activate) to "
                                                    "take it over, or retire it there, instead of starting fresh", 409)
        try:
            status = await core.core_config.save(default_options(), expected_revision=None, reason="setup",
                                                 source="setup", authority="app")
        except CoreConfigError as err:
            raise OnboardingError(err.code, str(err), err.status) from err
        if core.core_host is not None:
            core.core_host.schedule_snapshot(0)
        return {"created": True, "by": user_id, "config": status}

    async def pair(self, user_id: str | None) -> dict[str, Any]:
        companion = self.core.companion
        if companion is None or not companion.present:
            raise OnboardingError("companion_missing", "The companion integration is not installed or not connected",
                                  503)
        try:
            return await companion.pair(user_id)
        except CompanionError as err:
            raise OnboardingError(err.code, str(err), err.status) from err

    def status(self) -> dict[str, Any]:
        core = self.core
        options = core.core_config.options or {}
        rooms = {a: r for a, r in (options.get("rooms") or {}).items() if isinstance(r, dict)}
        enabled = {a: r for a, r in rooms.items() if r.get("enabled")}
        with_sensors = [a for a, r in enabled.items() if r.get("sensors")]
        with_lights = [a for a, r in enabled.items() if r.get("lights")]
        mood = options.get("mood") or {}
        mood_source = "builtin" if mood.get("builtin") else "entity" if mood.get("main_mood_entity") else "none"
        ctrl = core.lighting.controller if core.lighting else None
        lighting_doc = ctrl.lighting if ctrl is not None else (core.lighting.stored_config() if core.lighting else None)
        automatic = [a for a in enabled if ctrl is not None and ctrl.room_active(a)]
        switches = sum(len(r.switches) for r in lighting_doc.rooms.values()) if lighting_doc is not None else 0
        profiles = len(getattr(lighting_doc, "global_profiles", {}) or {}) if lighting_doc is not None else 0
        mgr = core.curtains.manager if core.curtains else None
        windows = len(mgr.config.windows) if mgr is not None else 0
        fc = core.forecast.manager if core.forecast else None
        companion = core.companion
        fp = core.floorplan.status() if core.floorplan is not None else None
        fp_rooms = (fp or {}).get("summary", {}).get("rooms", 0) if fp else 0
        legacy_installed = bool(core.ha and core.ha.integration and core.ha.integration.detected
                                and companion is not None and companion.state == "absent")
        steps = [
            {"key": "app", "title": "Install the Smart Comfort App", "done": True, "detail": f"Version {core.app_version}"},
            {"key": "companion", "title": "Install and pair the companion integration",
             "done": bool(companion and companion.trusted), "optional": True,
             "detail": ("Paired" if companion and companion.trusted else "Connected, not paired" if companion and
                        companion.present else "Not installed: without it the App has no native entities and "
                        "cannot command devices"),
             "route": "settings/system"},
            {"key": "config", "title": "Create the App's configuration", "done": core.core_config.authoritative,
             "detail": "Created" if core.core_config.authoritative else ("A staged copy exists: finish the migration"
                                                                          if core.core_config.staged else "Not yet")},
            {"key": "rooms", "title": "Configure rooms", "done": bool(enabled), "detail": f"{len(enabled)} enabled room(s)",
             "route": "rooms"},
            {"key": "sensors", "title": "Assign occupancy sensors", "done": bool(with_sensors) and len(with_sensors) == len(enabled),
             "detail": f"{len(with_sensors)} of {len(enabled)} rooms have sensors", "route": "presence"},
            {"key": "lighting", "title": "Set up lighting", "done": bool(with_lights),
             "detail": f"{len(with_lights)} room(s) with lights", "route": "lighting"},
            {"key": "moods", "title": "Create moods", "done": mood_source != "none" and profiles > 0,
             "detail": f"Mood source: {mood_source}; {profiles} mood profile(s)", "route": "moods"},
            {"key": "switches", "title": "Configure switches and remotes", "done": switches > 0, "optional": True,
             "detail": f"{switches} switch(es)", "route": "switches"},
            {"key": "curtains", "title": "Add curtains", "done": windows > 0, "optional": True,
             "detail": f"{windows} window(s)", "route": "curtains"},
            {"key": "predictions", "title": "Enable predictions", "done": fc is not None and fc.status.state in ("learning", "ready"),
             "optional": True, "detail": f"status {fc.status.state}" if fc else "Not running", "route": "predictions"},
            {"key": "floorplan", "title": "Draw a floor plan", "done": fp_rooms > 0, "optional": True,
             "detail": f"{fp_rooms} room(s) drawn", "route": "floorplan"},
            {"key": "automatic", "title": "Review and enable automatic control", "done": bool(automatic),
             "detail": f"{len(automatic)} of {len(enabled)} rooms in Automatic (every room starts Observing)",
             "route": "lighting"},
        ]
        return {"legacy_installed": legacy_installed, "config": core.core_config.status(), "steps": steps,
                "ready": core.core_config.authoritative and bool(enabled),
                "companion": companion.brief() if companion else None}
