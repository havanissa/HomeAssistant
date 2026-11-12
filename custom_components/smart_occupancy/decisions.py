"""Decision log: what automatic lighting and wall switches decided, why, and whether it worked (0.3.7).

Every *meaningful* decision of the lighting controller and of each wall switch
is kept as one structured record, generated where the decision is made (never
reconstructed later, never by the dashboard):

* who decided (``module``: ``lighting`` / ``switch``; ``subject``: the room's
  area id, or ``<area>/<switch id>``), on what (``trigger``, ``kind``),
* what was intended (``action``, ``target``) and why (``explanation``, one
  plain sentence built from the actual decision inputs),
* the operating mode at that moment (``mode``) and what happened (``status``),
* optional ``details`` (triggering entity and state, occupancy, mood, daylight,
  overrides, targets, service, result, errors), only what the controller
  actually used.

Statuses:

* ``executed``   - sent, Home Assistant accepted it, and the targets reported
  the requested state (or the action was internal, e.g. a room mood);
* ``dispatched`` - sent and accepted, but the result could not be confirmed
  (the targets did not report the requested state in time, or it cannot be
  observed: scenes, scripts);
* ``partial``    - some targets failed;
* ``failed``     - Home Assistant reported an error (or the target is unavailable);
* ``observed``   - evaluated in Observing: what would have been done; nothing sent;
* ``blocked``    - a condition prevented an otherwise valid action;
* ``no_change``  - nothing needed to change;
* ``no_action``  - nothing is configured for this (a button without an action);
* ``noted``      - a recorded fact that changes later decisions (a manual override).

Bounded and quiet: at most ``DECISION_LOG_SIZE`` records per room or switch.
A decision that repeats the newest record of its subject (same ``key``, same
kind of result, within the merge window) updates that record (time, count)
instead of adding one. Records are kept in the existing lighting state store
(``.storage/smart_occupancy.lighting_state``), written with its delayed,
atomic save; there is no entity, no Recorder history and no extra write path.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Any
import uuid

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import BRIGHTNESS_TOLERANCE_PCT, DECISION_LOG_SIZE, DECISION_MERGE_SECONDS, DECISION_VERIFY_SECONDS

_LOGGER = logging.getLogger(__name__)

MODULE_LIGHTING = "lighting"
MODULE_SWITCH = "switch"

EXECUTED = "executed"
DISPATCHED = "dispatched"
PARTIAL = "partial"
FAILED = "failed"
OBSERVED = "observed"
BLOCKED = "blocked"
NO_CHANGE = "no_change"
NO_ACTION = "no_action"
NOTED = "noted"
STATUSES = (EXECUTED, DISPATCHED, PARTIAL, FAILED, OBSERVED, BLOCKED, NO_CHANGE, NO_ACTION, NOTED)

# Results that are the same "kind of outcome" for merging (a dispatched record may become executed).
_GROUPS = {EXECUTED: "sent", DISPATCHED: "sent"}

_MAX_TEXT = 400
_MAX_LIST = 12
_OFFLINE = (STATE_UNAVAILABLE, STATE_UNKNOWN)


def now_iso(value: datetime | None = None) -> str:
    """Timezone-aware ISO time (UTC), as every record stores it."""
    return (value or dt_util.utcnow()).isoformat(timespec="seconds")


def _clip(value: Any, depth: int = 0) -> Any:
    """Keep details small and JSON-safe: short strings, short lists, shallow dicts."""
    if isinstance(value, str):
        return value if len(value) <= _MAX_TEXT else value[: _MAX_TEXT - 1] + "…"
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        if depth > 2:
            return None
        return {str(k): _clip(v, depth + 1) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out = [_clip(v, depth + 1) for v in items[:_MAX_LIST]]
        if len(items) > _MAX_LIST:
            out.append(f"… {len(items) - _MAX_LIST} more")
        return out
    return _clip(str(value), depth)


@dataclass(slots=True)
class Check:
    """What a target should report after a command: its state, and the brightness if one was requested."""

    entity_id: str
    state: str
    brightness_pct: int | None = None

    def reached(self, hass: HomeAssistant) -> bool:
        current = hass.states.get(self.entity_id)
        if current is None or current.state in _OFFLINE or current.state != self.state:
            return False
        if self.brightness_pct is None or self.state != "on":
            return True
        brightness = current.attributes.get("brightness")
        if not isinstance(brightness, (int, float)) or isinstance(brightness, bool):
            return True  # the light does not report brightness: its state is all that can be confirmed
        # One step of 255 is ~0.4 %; allow a little more than the planner's tolerance for rounding.
        return abs(brightness / 255 * 100 - self.brightness_pct) <= BRIGHTNESS_TOLERANCE_PCT + 1


def light_checks(service: str, data_by_entity: dict[str, dict[str, Any]]) -> list[Check]:
    """Checks for a light command that Home Assistant accepted."""
    if service == "turn_off":
        return [Check(e, "off") for e in data_by_entity]
    return [Check(e, "on", int(d["brightness_pct"]) if isinstance(d.get("brightness_pct"), (int, float)) else None)
            for e, d in data_by_entity.items()]


@dataclass(slots=True)
class Decision:
    """One decision record (see the module docstring)."""

    id: str
    at: str
    module: str
    subject: str
    trigger: str
    kind: str
    action: str
    explanation: str
    mode: str
    status: str
    target: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    count: int = 1
    first_at: str | None = None
    key: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "at": self.at, "first_at": self.first_at, "count": self.count,
            "module": self.module, "subject": self.subject, "trigger": self.trigger, "kind": self.kind,
            "action": self.action, "explanation": self.explanation, "mode": self.mode, "status": self.status,
            "target": self.target, "details": self.details,
        }

    def persist(self) -> dict[str, Any]:
        return {**self.as_dict(), "key": self.key}

    @classmethod
    def restore(cls, data: Any) -> Decision | None:
        if not isinstance(data, dict):
            return None
        try:
            at = str(data["at"])
            if dt_util.parse_datetime(at) is None:
                return None
            status = str(data["status"])
            return cls(
                id=str(data["id"]), at=at, module=str(data["module"]), subject=str(data["subject"]),
                trigger=str(data.get("trigger") or ""), kind=str(data.get("kind") or ""),
                action=str(data.get("action") or ""), explanation=str(data.get("explanation") or ""),
                mode=str(data.get("mode") or ""), status=status if status in STATUSES else NOTED,
                target=data.get("target") if isinstance(data.get("target"), str) else None,
                details=data.get("details") if isinstance(data.get("details"), dict) else {},
                count=max(1, int(data.get("count") or 1)),
                first_at=data.get("first_at") if isinstance(data.get("first_at"), str) else None,
                key=data.get("key") if isinstance(data.get("key"), str) else None,
            )
        except (KeyError, TypeError, ValueError):
            return None


class DecisionLog:
    """Bounded decision history per room and per switch, persisted with the lighting state."""

    def __init__(self, hass: HomeAssistant, on_change: Callable[[], None]) -> None:
        self.hass = hass
        self._on_change = on_change
        self._logs: dict[str, deque[Decision]] = {}
        self._verify: dict[str, CALLBACK_TYPE] = {}
        self._stopped = False

    # ------------------------------------------------------------- storage
    def restore(self, data: Any) -> None:
        logs = data.get("logs") if isinstance(data, dict) and isinstance(data.get("logs"), dict) else {}
        for subject, items in logs.items():
            restored = [d for d in (Decision.restore(i) for i in items or []) if d is not None]
            restored.sort(key=lambda d: d.at)
            if restored:
                self._logs[str(subject)] = deque(restored[-DECISION_LOG_SIZE:], maxlen=DECISION_LOG_SIZE)

    def persist(self) -> dict[str, Any]:
        return {"version": 1, "logs": {s: [d.persist() for d in log] for s, log in self._logs.items() if log}}

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        for cancel in self._verify.values():
            cancel()
        self._verify.clear()

    @callback
    def prune(self, keep: Callable[[str], bool]) -> None:
        """Forget the history of subjects that no longer exist (a removed switch)."""
        gone = [s for s in self._logs if not keep(s)]
        for subject in gone:
            del self._logs[subject]
        if gone:
            self._on_change()

    # ---------------------------------------------------------------- read
    def entries(self, subject: str) -> list[dict[str, Any]]:
        """Newest first."""
        return [d.as_dict() for d in reversed(self._logs.get(subject, ()))]

    def latest(self, subject: str) -> dict[str, Any] | None:
        log = self._logs.get(subject)
        return log[-1].as_dict() if log else None

    def model(self, subject: str) -> dict[str, Any]:
        entries = self.entries(subject)
        return {"latest": entries[0] if entries else None, "entries": entries, "size": DECISION_LOG_SIZE}

    def repeats(self, subject: str, key: Iterable[Hashable]) -> bool:
        """The newest decision of a subject has this key."""
        log = self._logs.get(subject)
        return bool(log) and log[-1].key == "|".join(str(k) for k in key)

    def get(self, subject: str, decision_id: str) -> Decision | None:
        return next((d for d in self._logs.get(subject, ()) if d.id == decision_id), None)

    # --------------------------------------------------------------- write
    @callback
    def record(self, subject: str, *, module: str, trigger: str, kind: str, action: str, explanation: str,
               mode: str, status: str, target: str | None = None, details: dict[str, Any] | None = None,
               key: Iterable[Hashable] | None = None, merge_seconds: float | None = DECISION_MERGE_SECONDS,
               same_seconds: float = 0, checks: list[Check] | None = None,
               verify_after: float = DECISION_VERIFY_SECONDS) -> Decision:
        """Add a decision, or fold it into the newest one when it repeats it.

        ``key`` identifies "the same decision" (without times or numbers); without
        a key every record is new. ``merge_seconds`` None merges regardless of
        age; a repeat within ``same_seconds`` is the same decision reported again
        (not counted). ``checks`` (with status ``dispatched``) are verified against the
        targets' states: at once, and again after ``verify_after`` seconds.
        """
        if status not in STATUSES:
            raise ValueError(f"unknown decision status {status}")
        at = now_iso()
        key_text = "|".join(str(k) for k in key) if key is not None else None
        log = self._logs.setdefault(subject, deque(maxlen=DECISION_LOG_SIZE))
        newest = log[-1] if log else None
        decision: Decision
        if (newest is not None and key_text is not None and newest.key == key_text
                and _GROUPS.get(newest.status, newest.status) == _GROUPS.get(status, status)
                and (merge_seconds is None or _age(newest.at) <= merge_seconds)):
            decision = newest
            if same_seconds <= 0 or _age(newest.at) > same_seconds:
                decision.count += 1
                decision.first_at = decision.first_at or decision.at
                decision.at = at
            decision.trigger, decision.action, decision.explanation = trigger, action, explanation
            decision.mode, decision.status, decision.target = mode, status, target
            decision.details = _clip(details or {})
        else:
            decision = Decision(
                id=uuid.uuid4().hex[:12], at=at, module=module, subject=subject, trigger=trigger, kind=kind,
                action=action, explanation=explanation, mode=mode, status=status, target=target,
                details=_clip(details or {}), key=key_text,
            )
            log.append(decision)
        self._cancel_verify(decision.id)
        if checks and status == DISPATCHED:
            self._check(subject, decision.id, checks, final=False)
            if decision.status == DISPATCHED and not self._stopped:
                self._schedule_verify(subject, decision.id, checks, verify_after)
        _LOGGER.debug("decision subject=%s kind=%s action=%s status=%s count=%d", subject, kind, action,
                      decision.status, decision.count)
        self._on_change()
        return decision

    def _schedule_verify(self, subject: str, decision_id: str, checks: list[Check], delay: float) -> None:
        @callback
        def fire(_now: datetime) -> None:
            self._verify.pop(decision_id, None)
            if self._check(subject, decision_id, checks, final=True):
                self._on_change()

        self._verify[decision_id] = async_call_later(self.hass, max(0.5, delay), fire)

    def _cancel_verify(self, decision_id: str) -> None:
        if cancel := self._verify.pop(decision_id, None):
            cancel()

    def _check(self, subject: str, decision_id: str, checks: list[Check], *, final: bool) -> bool:
        """Confirm a dispatched decision from the targets' states. Returns True when the record changed."""
        decision = self.get(subject, decision_id)
        if decision is None or decision.status != DISPATCHED:
            return False
        missing = [c.entity_id for c in checks if not c.reached(self.hass)]
        if not missing:
            decision.status = EXECUTED
            decision.details = {**decision.details, "verification": "Every target reported the requested state."}
            return True
        if final:
            n = len(checks)
            decision.details = _clip({
                **decision.details,
                "verification": (f"{n - len(missing)} of {n} target{'s' if n != 1 else ''} reported the requested "
                                 f"state; not confirmed: {', '.join(missing)}"),
            })
            return True
        return False


def _age(iso: str) -> float:
    at = dt_util.parse_datetime(iso)
    return (dt_util.utcnow() - at).total_seconds() if at else float("inf")


def minutes_text(minutes: int | None) -> str:
    return f"for {minutes} min" if minutes else "until the room is vacant"
