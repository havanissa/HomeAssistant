"""Delay On / Delay Off of brightness matrix cells at runtime (App 0.9.0, Phase A6.2).

The configuration is ``brightness.LightMatrix.delays``; this module holds the
**pending** delayed actions of the lighting controller. There is at most one
pending action per controlled target (light, group or relay output) and it is
never a second executor: the controller's planning and arbitration decide
*what* a light gets, exactly as without delays, and only its two executors
(``async_apply`` and ``async_zones``) consult :meth:`DelayTracker.gate` right
before they send. When a delay ends, the controller plans again, revalidates
the winning decision through the same gate and sends only what is still due.

Semantics
---------
* **Delay On** (the winning cell's): the cell requests an Off -> On switch-on
  (``turn_on`` while the light is off). The switch-on waits; nothing is sent
  meanwhile. If the light is no longer requested on when the delay ends (the
  zone ended, the room is vacant, a manual change, a Priority Input, another
  rule ...) the pending switch-on is cancelled. If another cell wins while it
  waits and still requests On, the wait continues from the same start with the
  new cell's Delay On (0 s: at once) and its brightness: a stale brightness is
  never applied.
* **Delay Off** (the cell that kept the light on, "the lit cell"): the
  resolved behaviour of an occupied room switches the light off (a ``turn_off``
  of its plan, or the release of a zone light) after the cell that kept it on
  stopped applying. The light keeps its state for that long; if a rule wants it
  on again meanwhile (the zone is occupied again, another cell wins with a
  value) the pending switch-off is cancelled and that rule applies.
* A change between two brightness values (On -> On) is never delayed.
* Never delayed, and superseding a pending action at once: a vacancy action,
  a manual change, a wall switch press, a Priority Input, Manual mood, the room
  set to Observing, the room switched to Automatic, a light that comes back
  online, the end of a manual override (*Resume*) and Identify.
* Restart: pending actions are not persisted; they are dropped and never
  replayed. The lit cell of each light is kept (a reference, not an action), so
  a switch-off decided after a restart still waits for that cell's Delay Off
  from the moment it is decided.

Times are UTC datetimes from ``homeassistant.util.dt`` (the host clock in the
App, the frozen clock in tests) and timers are point-in-time timers, like the
zone engines'; a timer that fires marks its action as due (``fired``), so an
early test timer never leaves an action waiting.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from .planner import PlanItem

HISTORY_SIZE = 12
ON, OFF = "on", "off"
#: Triggers whose application is never delayed (and supersede a pending action).
NO_DELAY_TRIGGERS = frozenset({"activated", "resumed", "reconnected", "identified"})
#: Command purposes that do not supersede a pending action of the light (global appearance updates, Identify).
KEEPS_PENDING = frozenset({"adaptive", "identify", "identify_restore", "identify_recovery"})
HELD_CODES = {ON: "delay_on", OFF: "delay_off"}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


@dataclass(slots=True, frozen=True)
class Cell:
    """A matrix cell of one light as the plan resolved it (from ``rule["brightness"]``)."""

    column: str
    column_name: str
    level: str
    level_name: str
    value: int
    delay_on: int = 0
    delay_off: int = 0

    @classmethod
    def of(cls, item: PlanItem | None) -> Cell | None:
        b = ((item.rule or {}).get("brightness") if item is not None else None) or None
        if not isinstance(b, dict) or not isinstance(b.get("value"), int):
            return None
        return cls(str(b.get("column") or ""), str(b.get("column_name") or ""), str(b.get("level") or ""),
                   str(b.get("level_name") or b.get("level") or ""), int(b["value"]),
                   int(b.get("delay_on") or 0), int(b.get("delay_off") or 0))

    @property
    def label(self) -> str:
        return f"{self.column_name} · {self.level_name}"

    def as_dict(self) -> dict[str, Any]:
        return {"column": self.column, "column_name": self.column_name, "level": self.level,
                "level_name": self.level_name, "value": self.value, "delay_on": self.delay_on,
                "delay_off": self.delay_off}

    @classmethod
    def from_dict(cls, data: Any) -> Cell | None:
        if not isinstance(data, dict) or not isinstance(data.get("value"), int):
            return None
        try:
            return cls(str(data["column"]), str(data.get("column_name") or data["column"]), str(data["level"]),
                       str(data.get("level_name") or data["level"]), int(data["value"]),
                       int(data.get("delay_on") or 0), int(data.get("delay_off") or 0))
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(slots=True)
class Pending:
    """One delayed switch-on or switch-off of a target."""

    area_id: str
    entity_id: str
    action: str                 # "on" | "off"
    cell: Cell                  # Delay On: the winning cell; Delay Off: the cell that stopped applying
    seconds: int
    started: datetime
    due: datetime
    generation: int             # the lighting configuration revision when it started
    cause: str
    release: bool = False       # a Delay Off of a zone light's release (not a turn_off of its plan)
    fired: bool = False
    cancel: CALLBACK_TYPE | None = None

    def remaining(self, now: datetime) -> float:
        return max(0.0, (self.due - now).total_seconds())

    def as_dict(self, now: datetime) -> dict[str, Any]:
        return {"area_id": self.area_id, "entity_id": self.entity_id, "action": self.action,
                "cell": self.cell.as_dict(), "condition": self.cell.label, "seconds": self.seconds,
                "started": _iso(self.started), "due": _iso(self.due), "remaining": round(self.remaining(now), 1),
                "generation": self.generation, "cause": self.cause, "release": self.release, "fired": self.fired}


@dataclass(slots=True)
class _Light:
    lit: Cell | None = None
    history: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=HISTORY_SIZE))


class DelayTracker:
    """The pending delayed actions of one lighting controller (see the module docstring)."""

    def __init__(self, hass: HomeAssistant, *, revision: Callable[[], int],
                 on_due: Callable[[str, str], None], on_change: Callable[[], None],
                 current: Callable[[str, str, str, str], tuple[int, int] | None] | None = None) -> None:
        self.hass = hass
        self._revision = revision
        # (area, light, column, category) -> that cell's (Delay On, Delay Off) now; None: the cell no longer exists.
        self._current = current
        self._on_due = on_due
        self._on_change = on_change
        self.pending: dict[tuple[str, str], Pending] = {}
        self._lights: dict[tuple[str, str], _Light] = {}
        self._running = True

    # ------------------------------------------------------------ storage
    def persist(self) -> dict[str, Any]:
        """Only each light's lit cell (a reference): pending actions are never persisted or replayed."""
        return {"lit": {f"{a}|{e}": light.lit.as_dict() for (a, e), light in self._lights.items() if light.lit}}

    def restore(self, data: Any) -> None:
        lit = data.get("lit") if isinstance(data, dict) and isinstance(data.get("lit"), dict) else {}
        for key, item in lit.items():
            if not isinstance(key, str) or "|" not in key or (cell := Cell.from_dict(item)) is None:
                continue
            area_id, entity_id = key.split("|", 1)
            self._light(area_id, entity_id).lit = cell

    @callback
    def async_stop(self) -> None:
        """Stop: timers cancelled, pending actions forgotten (never replayed)."""
        self._running = False
        for pend in self.pending.values():
            if pend.cancel:
                pend.cancel()
        self.pending.clear()

    # --------------------------------------------------------------- read
    def get(self, area_id: str, entity_id: str) -> Pending | None:
        return self.pending.get((area_id, entity_id))

    def lit(self, area_id: str, entity_id: str) -> Cell | None:
        light = self._lights.get((area_id, entity_id))
        return light.lit if light else None

    def status(self, area_id: str, entity_id: str) -> dict[str, Any]:
        now = dt_util.utcnow()
        pend = self.pending.get((area_id, entity_id))
        light = self._lights.get((area_id, entity_id))
        return {"pending": pend.as_dict(now) if pend else None,
                "lit": light.lit.as_dict() if light and light.lit else None,
                "history": list(reversed(light.history)) if light else []}

    def room(self, area_id: str) -> list[dict[str, Any]]:
        now = dt_util.utcnow()
        return [p.as_dict(now) for (a, _e), p in sorted(self.pending.items()) if a == area_id]

    def diagnostics(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        return {"pending": [p.as_dict(now) for p in self.pending.values()],
                "lit": {f"{a}|{e}": light.lit.as_dict() for (a, e), light in self._lights.items() if light.lit}}

    def annotate(self, area_id: str, items: Iterable[PlanItem]) -> None:
        """Show pending actions in a plan (``rule["delay"]``). Pure: never starts, ends or sends anything."""
        now = dt_util.utcnow()
        for item in items:
            pend = self.pending.get((area_id, item.entity_id))
            if pend is not None:
                item.rule = {**(item.rule or {}), "delay": pend.as_dict(now)}

    # -------------------------------------------------------------- write
    def _light(self, area_id: str, entity_id: str) -> _Light:
        return self._lights.setdefault((area_id, entity_id), _Light())

    def _note(self, area_id: str, entity_id: str, event: str, pend: Pending | None, reason: str | None = None,
              **extra: Any) -> None:
        entry = {"at": _iso(dt_util.utcnow()), "event": event, **({
            "action": pend.action, "condition": pend.cell.label, "seconds": pend.seconds, "cause": pend.cause,
        } if pend else {}), **({"reason": reason} if reason else {}), **extra}
        self._light(area_id, entity_id).history.append(entry)

    def _start(self, area_id: str, entity_id: str, action: str, cell: Cell, seconds: int, cause: str, now: datetime,
               *, release: bool = False) -> Pending:
        pend = Pending(area_id, entity_id, action, cell, seconds, now, now + timedelta(seconds=seconds),
                       self._revision(), cause, release=release)
        self.pending[(area_id, entity_id)] = pend
        self._schedule(pend)
        self._note(area_id, entity_id, "started", pend)
        self._on_change()
        return pend

    def _schedule(self, pend: Pending) -> None:
        if pend.cancel:
            pend.cancel()
            pend.cancel = None
        if not self._running:
            return

        @callback
        def fire(_now: datetime) -> None:
            pend.cancel = None
            if self.pending.get((pend.area_id, pend.entity_id)) is not pend or not self._running:
                return
            pend.fired = True
            self._on_due(pend.area_id, pend.entity_id)

        pend.cancel = async_track_point_in_utc_time(self.hass, fire, pend.due)

    def end(self, area_id: str, entity_id: str, event: str, reason: str | None = None) -> Pending | None:
        """End a pending action: ``executed``, ``cancelled`` (no longer requested) or ``superseded``."""
        pend = self.pending.pop((area_id, entity_id), None)
        if pend is None:
            return None
        if pend.cancel:
            pend.cancel()
            pend.cancel = None
        self._note(area_id, entity_id, event, pend, reason)
        self._on_change()
        return pend

    def supersede(self, area_id: str | None, entity_ids: Iterable[str], reason: str) -> None:
        """Something else commands or changed these targets (in a room, or in any room): their actions end."""
        ids = set(entity_ids)
        for key in [k for k in self.pending if k[1] in ids and (area_id is None or k[0] == area_id)]:
            self.end(*key, "superseded", reason)

    def cancel_room(self, area_id: str, reason: str) -> None:
        for key in [k for k in self.pending if k[0] == area_id]:
            self.end(*key, "cancelled", reason)

    def cancel_all(self, reason: str) -> None:
        for key in list(self.pending):
            self.end(*key, "cancelled", reason)

    def forget(self, keep: Callable[[str, str], bool]) -> None:
        """Lights that no longer have a matrix (or left the room): their pending action and lit cell go."""
        for key in [k for k in self.pending if not keep(*k)]:
            self.end(*key, "cancelled", "its brightness matrix was removed or disabled")
        for key in [k for k, light in self._lights.items() if light.lit and not keep(*k)]:
            self._lights[key].lit = None

    # --------------------------------------------------------------- gate
    def gate(self, area_id: str, items: Iterable[PlanItem], *, trigger: str, occupied: bool,
             releases: Iterable[str] = ()) -> set[str]:
        """Before an executor sends: start, keep, end or let through each item's delayed on/off.

        Mutates the plan items it holds back (decision ``unchanged``, reason ``delay_on`` / ``delay_off``) and
        returns their entity ids; the caller sends nothing for them. ``releases``: zone lights the caller would
        switch off because no rule keeps them (held like a ``turn_off``). Every other item is left exactly as
        planned. Only for an executing controller (never for previews or Observing rooms).
        """
        from .planner import CHANGE, UNCHANGED  # noqa: PLC0415 - (planner imports the lighting models)

        now = dt_util.utcnow()
        releases = set(releases)
        held: set[str] = set()
        for item in items:
            entity_id = item.entity_id
            key = (area_id, entity_id)
            pend = self.pending.get(key)
            cell = Cell.of(item)
            on_now = item.current.get("state") == "on"
            if trigger in NO_DELAY_TRIGGERS:
                if pend is not None:
                    self.end(area_id, entity_id, "superseded", f"applied at once ({trigger.replace('_', ' ')})")
                self._learn(area_id, item, cell, on_now)
                continue
            if item.decision == CHANGE and item.service == "turn_on" and not on_now and cell and cell.value > 0:
                # Off -> On requested by a matrix cell: its Delay On.
                if pend is not None and pend.action == ON:
                    if pend.cell != cell:
                        # Another cell wins while waiting (still On): same start, its delay and value.
                        self._note(area_id, entity_id, "changed", pend, f"now {cell.label}")
                        pend.cell, pend.seconds = cell, cell.delay_on
                        pend.due = pend.started + timedelta(seconds=cell.delay_on)
                        if pend.due > now:
                            pend.fired = False  # (a longer delay of the new cell is served in full)
                            self._schedule(pend)
                    if pend.fired or pend.due <= now:
                        self.end(area_id, entity_id, "executed")
                        self._learn(area_id, item, cell, on_now)
                        continue
                    self._hold(item, pend, now, UNCHANGED)
                    held.add(entity_id)
                    continue
                if pend is not None:
                    self.end(area_id, entity_id, "superseded", "switched on by its rule instead")
                if cell.delay_on > 0:
                    level = item.data.get("brightness_pct")
                    pend = self._start(area_id, entity_id, ON, cell, cell.delay_on,
                                       f"{cell.label} requests {'On' if level is None else f'{level} %'}", now)
                    self._hold(item, pend, now, UNCHANGED)
                    held.add(entity_id)
                    continue
                self._learn(area_id, item, cell, on_now)
                continue
            release = entity_id in releases
            if on_now and (release or (item.decision == CHANGE and item.service == "turn_off")):
                # On -> Off: the Delay Off of the cell that kept it on (only while the room stays occupied).
                if pend is not None and pend.action == OFF:
                    if pend.fired or pend.due <= now:
                        self.end(area_id, entity_id, "executed")
                        self._light(area_id, entity_id).lit = None
                        continue
                    self._hold(item, pend, now, UNCHANGED)
                    held.add(entity_id)
                    continue
                if pend is not None:
                    self.end(area_id, entity_id, "superseded", "now requested off")
                lit = self.lit(area_id, entity_id)
                if lit is not None and self._current is not None:
                    # The lit cell's Delay Off as configured now (a removed zone column: none, at once).
                    pair = self._current(area_id, entity_id, lit.column, lit.level)
                    lit = None if pair is None else Cell(lit.column, lit.column_name, lit.level, lit.level_name,
                                                         lit.value, pair[0], pair[1])
                if occupied and lit is not None and lit.delay_off > 0 and lit != cell:
                    pend = self._start(area_id, entity_id, OFF, lit, lit.delay_off, f"{lit.label} no longer applies",
                                       now, release=release)
                    self._hold(item, pend, now, UNCHANGED)
                    held.add(entity_id)
                    continue
                self._light(area_id, entity_id).lit = None
                continue
            if pend is not None:
                self.end(area_id, entity_id, "cancelled", self._why_cancelled(item, pend))
            self._learn(area_id, item, cell, on_now)
        return held

    @staticmethod
    def _why_cancelled(item: PlanItem, pend: Pending) -> str:
        if item.reason_code == "manual_override":
            return "a manual change is respected"
        if item.reason_code == "offline":
            return "the light is unavailable"
        if pend.action == ON:
            return f"no longer requested on ({item.reason})" if item.reason else "no longer requested on"
        return f"kept on: {item.reason}" if item.reason else "a rule keeps it on"

    def _hold(self, item: PlanItem, pend: Pending, now: datetime, unchanged: str) -> None:
        left = round(pend.remaining(now))
        word = "Delay On" if pend.action == ON else "Delay Off"
        item.decision = unchanged
        item.reason_code = HELD_CODES[pend.action]
        item.reason = f"{word}: {'switched on' if pend.action == ON else 'switched off'} in {left} s ({pend.cause})"
        item.rule = {**(item.rule or {}), "delay": pend.as_dict(now)}

    def _learn(self, area_id: str, item: PlanItem, cell: Cell | None, on_now: bool) -> None:
        """Which cell keeps the light on now (the source of a later Delay Off)."""
        from .planner import CHANGE, UNCHANGED  # noqa: PLC0415

        light = self._light(area_id, item.entity_id)
        turning_on = item.decision == CHANGE and item.service == "turn_on"
        if cell is not None and cell.value > 0 and (turning_on or (on_now and item.decision == UNCHANGED)):
            light.lit = cell
        elif (item.decision == CHANGE and item.service == "turn_off") or (not on_now and not turning_on):
            light.lit = None
        elif cell is None and turning_on:
            light.lit = None  # switched on by a rule that is not a matrix cell
