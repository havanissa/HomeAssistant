"""Ambient light level runtime (0.6.7): each room's committed ambient category.

Not a second daylight monitor: there is no sensor subscription and no
estimator here. ``DaylightMonitor.evaluate`` passes every evaluation of a room
(its natural light estimate, the chosen indoor sensor and its contamination)
to ``observe``; the category follows that evidence through the hysteresis and
stability rules of ``ambient.py``. When the committed category of a room with
ambient brightness control changes, the lighting controller is told
(``LightingController.async_ambient_changed``); nothing here sends a command.

The committed category of every room is kept in the lighting state store, so
after a restart a room holds its last reliable category until new evidence
arrives (instead of jumping on a lamp-lit reading).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .ambient import LEVEL_KEYS, AmbientStatus, AmbientTracker, classify, status as ambient_status

if TYPE_CHECKING:
    from .controller import LightingController
    from .daylight_monitor import DaylightStatus

_HISTORY = 20


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


class AmbientLevels:
    """Ambient categories of every enabled room."""

    def __init__(self, ctrl: LightingController) -> None:
        self.ctrl = ctrl
        self.trackers: dict[str, AmbientTracker] = {}
        self._signature: dict[str, Any] = {}
        self._status: dict[str, AmbientStatus] = {}
        self._held: dict[str, bool] = {}
        self._history: dict[str, deque[dict[str, Any]]] = {}

    # ------------------------------------------------------------ lifecycle
    def restore(self, data: Any) -> None:
        rooms = data.get("rooms") if isinstance(data, dict) and isinstance(data.get("rooms"), dict) else {}
        for area_id, item in rooms.items():
            if isinstance(item, dict):
                self.trackers[area_id] = AmbientTracker.restore(item)
                sig = item.get("signature")
                self._signature[area_id] = tuple(sig) if isinstance(sig, list) else None

    def persist(self) -> dict[str, Any]:
        return {"rooms": {a: {**t.persist(), "signature": list(self._signature.get(a) or ())}
                          for a, t in self.trackers.items() if t.committed is not None}}

    # ----------------------------------------------------------- evaluation
    def _settings_signature(self, area_id: str) -> tuple[Any, ...]:
        s = self.ctrl.lighting.ambient_for(area_id)
        return (*(s.thresholds or ()), s.hysteresis, s.fallback.value if s.fallback else None, s.fallback_level)

    def observe(self, area_id: str, daylight: DaylightStatus, now: datetime) -> AmbientStatus:
        """One daylight evaluation of a room: update its category (may tell the controller it changed)."""
        lighting = self.ctrl.lighting
        room = lighting.room(area_id)
        settings = lighting.ambient_for(area_id)
        signature = self._settings_signature(area_id)
        tracker = self.trackers.get(area_id)
        if tracker is None or self._signature.get(area_id) != signature:
            # New thresholds or rules: classified again from the current evidence, without a change event
            # (editing the levels never sends a command; the new category is used at the next trigger).
            tracker = self.trackers[area_id] = AmbientTracker(
                committed=None if tracker is None or self._signature.get(area_id) is not None else tracker.committed,
                since=None if tracker is None else tracker.since, reliable_at=None if tracker is None else
                tracker.reliable_at)
            self._signature[area_id] = signature
        ev, old = classify(tracker, daylight.estimate, settings, now)
        st = ambient_status(tracker, ev, settings, enabled=room.ambient.enabled)
        chosen = daylight.chosen
        st.measured_lux = chosen.value if chosen else None
        st.sensor = chosen.entity_id if chosen else None
        st.sensor_name = chosen.name if chosen else None
        st.contaminated = daylight.contamination.contaminated
        st.contamination = daylight.contamination.text
        st.confidence = daylight.estimate.confidence
        self._status[area_id] = st
        if old is not None:
            self._history.setdefault(area_id, deque(maxlen=_HISTORY)).append({
                "at": _iso(now), "from": LEVEL_KEYS[old], "to": st.key, "basis": st.basis, "lux": st.lux_text()})
            self.ctrl.mark_dirty()
        held = st.basis == "held" and bool(ev.held)
        was_held = self._held.get(area_id, False)
        self._held[area_id] = held
        if room.ambient.enabled:
            if old is not None:
                self.ctrl.async_ambient_changed(area_id, LEVEL_KEYS[old], st)
            elif held and not was_held:
                self.ctrl.async_ambient_held(area_id, st)
        return st

    def status(self, area_id: str) -> AmbientStatus:
        """The room's category now (evaluated through the daylight monitor when it has none)."""
        st = self._status.get(area_id)
        if st is None or self._signature.get(area_id) != self._settings_signature(area_id):
            self.ctrl.daylight.status(area_id)  # (its evaluation calls observe)
            st = self._status.get(area_id)
            if st is None or self._signature.get(area_id) != self._settings_signature(area_id):
                st = self.observe(area_id, self.ctrl.daylight.evaluate(area_id, notify=False), dt_util.utcnow())
        return st

    def due(self, area_id: str) -> datetime | None:
        """When the room's pending category may be committed (the daylight monitor re-evaluates then)."""
        tracker = self.trackers.get(area_id)
        if tracker is None:
            return None
        return tracker.due(self.ctrl.lighting.ambient_for(area_id).stable_seconds or 0)

    def history(self, area_id: str) -> list[dict[str, Any]]:
        return list(self._history.get(area_id, ()))

    def diagnostics(self) -> dict[str, Any]:
        return {a: {**self.status(a).as_dict(), "history": self.history(a)}
                for a in sorted(self.ctrl.config.enabled_rooms)}
