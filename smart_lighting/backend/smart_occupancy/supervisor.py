"""Room Supervisor (0.6.0): does every room do what Smart Occupancy decided?

The supervisor is not a second lighting controller. For each room it asks the
controller for the **effective plan** - the same plan the controller executes,
with the winning rule of every target (``arbitration.py``) - and compares it
with what Home Assistant reports:

* **desired state**: what the winning rule requests,
* **actual state**: the state Home Assistant reports for the device (which may
  differ from the physical output: a mismatch is an *observation*; only a
  device that keeps not reaching the state after the allowed corrections is
  reported as a suspected device failure).

Only attributes the device supports are compared (a relay: on/off), with
tolerances for brightness and colour temperature, after a verification delay
(plus the command's transition) so slow devices and late reports are not
mismatches.

What is checked
---------------
A target is *supervised* when the winning rule wants a state **and Smart
Occupancy commanded it**: the plan says "already sent" (the command was
accepted, the device does not report it) or the command failed and the rule
still wants it. The removal of a preliminary light or of a light a zone
switched on is verified the same way. A target that differs from its rule
for another reason is not corrected by the supervisor:

* a person's manual change (manual override) - respected, never corrected,
* a change by an automation or another integration - reported (*conflict
  suspected* when it happens again); corrected once only when the room allows
  it (``correct_external``),
* a rule change that the normal triggers have not applied yet - *pending*
  (the supervisor never applies new lighting itself, so it changes no room's
  behaviour),
* daylight, occupancy, Manual mood, Observing - whatever the plan says.

Corrections (mode Automatic, room Automatic, not Manual mood)
-------------------------------------------------------------
At most ``max_retries`` corrective commands per mismatch episode, the first
after ``retry_delay`` and each further one after twice the previous delay, at
most ``max_per_hour`` per device, never while a command or transition is
pending, never twice for the same external change. Identical commands are not
repeated faster than that. After the last attempt the target is reported as a
suspected device failure and left alone until its state, its rule or its
availability changes (a device that comes back online is verified again).

In mode **Observing** (the default) everything is evaluated and reported, and
nothing is ever sent. Mode **Off** stops the checks.

Timing: one timer for all rooms, set to the next moment something is due -
a command verification (seconds after a command), a room reconciliation
(``reconcile_interval``), a health evaluation of sensors and devices
(``health_interval``) and the extended diagnostics (``diagnostics_interval``,
metrics only). Reconciliation reads states only; there is no periodic
service call. Routine successful checks are counted, not logged: the decision
log records mismatches, corrections and their results, conflicts, failures,
sensor availability changes and the return to a healthy state.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import OccupancyState
from .decisions import BLOCKED as D_BLOCKED, EXECUTED, FAILED, MODULE_LIGHTING, NOTED, OBSERVED
from .arbitration import releases
from .engine import SignalStatus
from .planner import CHANGE, SKIPPED, UNCHANGED, PlanItem
from .targets import capabilities, domain, is_relay
from .zones import SupervisorMode, SupervisorSettings

if TYPE_CHECKING:
    from .controller import LightingController

_LOGGER = logging.getLogger(__name__)

# Per-target results.
CORRECT = "correct"
MISMATCH = "mismatch"
WAITING = "waiting"
CORRECTING = "correcting"
FAILED_DEVICE = "device_failure"
OVERRIDE = "override"
UNAVAILABLE = "unavailable"
NOT_SUPERVISED = "not_supervised"
EXTERNAL = "external"
CONFLICT = "conflict"
PENDING = "pending"
PAUSED = "paused"
NO_EXPECTATION = "no_expectation"

# Room statuses, most important first.
ROOM_STATUSES = ("device_failure", "conflict", "correcting", "mismatch", "waiting", "degraded", "unknown",
                 "observing", "healthy", "off")
STATUS_WORDS = {
    "healthy": "Healthy", "observing": "Observing", "waiting": "Waiting for verification",
    "mismatch": "Mismatch detected", "correcting": "Correcting", "degraded": "Degraded sensor availability",
    "device_failure": "Device failure", "conflict": "Conflict suspected", "unknown": "Not checked yet",
    "off": "Supervisor off",
}
# Removal commands that are verified like a rule's state: what Smart Occupancy switched on itself.
_REMOVALS = frozenset({"preliminary_end", "zone_release", "priority_release"})
_EXTERNAL_SOURCES = frozenset({"automation", "unattributed"})


def _iso(value: datetime | None = None) -> str:
    return (value or dt_util.utcnow()).isoformat(timespec="seconds")


@dataclass(slots=True)
class TargetCheck:
    entity_id: str
    result: str = NOT_SUPERVISED
    reason: str = ""
    rule: dict[str, Any] | None = None
    desired: dict[str, Any] | None = None
    actual: dict[str, Any] | None = None
    last_command: dict[str, Any] | None = None
    last_verification: dict[str, Any] | None = None
    last_correction: dict[str, Any] | None = None
    attempts: int = 0
    next_retry: float = 0.0
    pending_until: float = 0.0
    corrections: deque[float] = field(default_factory=lambda: deque(maxlen=60))
    episode: str | None = None          # the desired command a mismatch episode is about
    logged: str | None = None           # what was logged for the episode (no repeats)
    external_fixed: str | None = None   # the desired command an external change was corrected for (once)
    error: str | None = None

    def as_dict(self, hass: HomeAssistant, names: dict[str, str]) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id, "name": names.get(self.entity_id, self.entity_id),
            "type": "relay" if is_relay(self.entity_id) else "light", "result": self.result, "reason": self.reason,
            "rule": self.rule, "desired": self.desired, "actual": self.actual, "last_command": self.last_command,
            "last_verification": self.last_verification, "last_correction": self.last_correction,
            "attempts": self.attempts, "error": self.error,
        }


@dataclass(slots=True)
class RoomSupervision:
    targets: dict[str, TargetCheck] = field(default_factory=dict)
    status: str = "unknown"
    reason: str = ""
    last_check: str | None = None
    last_ok: str | None = None
    next_reconcile: float = 0.0
    next_health: float = 0.0
    next_extended: float = 0.0
    health: dict[str, Any] = field(default_factory=dict)
    unavailable_sensors: frozenset[str] = frozenset()
    metrics: dict[str, int] = field(default_factory=lambda: {
        "checks": 0, "mismatches": 0, "corrections": 0, "verified": 0, "failures": 0, "conflicts": 0})
    extended: dict[str, Any] = field(default_factory=dict)
    was_unhealthy: bool = False
    signature: tuple | None = None


def actual_state(hass: HomeAssistant, entity_id: str) -> dict[str, Any] | None:
    state = hass.states.get(entity_id)
    if state is None:
        return None
    out: dict[str, Any] = {"state": state.state}
    if state.state == "on" and not is_relay(entity_id):
        b = state.attributes.get("brightness")
        if isinstance(b, (int, float)) and not isinstance(b, bool):
            out["brightness_pct"] = round(b / 255 * 100)
        if state.attributes.get("color_mode") == "color_temp" and state.attributes.get("color_temp_kelvin"):
            out["color_temp_kelvin"] = state.attributes.get("color_temp_kelvin")
    return out


def reaches(hass: HomeAssistant, entity_id: str, service: str, data: dict[str, Any],
            settings: SupervisorSettings) -> bool:
    """Does Home Assistant report the requested state (supported attributes, with the supervisor's tolerances)?"""
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown"):
        return False
    if service == "turn_off":
        return state.state == "off"
    if state.state != "on":
        return False
    caps = capabilities(hass, entity_id)
    a = state.attributes
    if "brightness_pct" in data and caps.brightness:
        b = a.get("brightness")
        if isinstance(b, (int, float)) and abs(b / 255 * 100 - data["brightness_pct"]) > (settings.brightness_tolerance or 5):
            return False
    if "color_temp_kelvin" in data and caps.color_temp and a.get("color_mode") == "color_temp":
        k = a.get("color_temp_kelvin")
        if isinstance(k, (int, float)) and abs(k - data["color_temp_kelvin"]) > (settings.kelvin_tolerance or 150):
            return False
    return True


def _want(service: str | None, data: dict[str, Any]) -> str:
    return f"{service}|{sorted((k, str(v)) for k, v in data.items() if k != 'transition')}"


def describe(service: str | None, data: dict[str, Any] | None) -> str:
    if service == "turn_off":
        return "off"
    data = data or {}
    parts = [f"{data['brightness_pct']} %"] if "brightness_pct" in data else ["on"]
    if "color_temp_kelvin" in data:
        parts.append(f"{data['color_temp_kelvin']} K")
    return ", ".join(parts)


def describe_actual(actual: dict[str, Any] | None) -> str:
    if not actual:
        return "missing"
    if actual.get("state") != "on":
        return str(actual.get("state"))
    parts = [f"{actual['brightness_pct']} %"] if "brightness_pct" in actual else ["on"]
    if "color_temp_kelvin" in actual:
        parts.append(f"{actual['color_temp_kelvin']} K")
    return ", ".join(parts)


class Supervisor:
    """Room Supervisor for every enabled room (see the module docstring)."""

    def __init__(self, hass: HomeAssistant, ctrl: LightingController) -> None:
        self.hass = hass
        self.ctrl = ctrl
        self.rooms: dict[str, RoomSupervision] = {}
        self._timer: CALLBACK_TYPE | None = None
        self._due: float | None = None
        self._running = False
        self._busy: set[str] = set()

    # ------------------------------------------------------------ lifecycle
    @callback
    def async_start(self) -> None:
        self._running = True
        now = time.monotonic()
        for area_id in self.ctrl.config.enabled_rooms:
            room = self.rooms.setdefault(area_id, RoomSupervision())
            # The first check after a (re)start waits one verification delay: states settle first.
            room.next_reconcile = now + float(self.settings(area_id).verify_delay or 4)
            room.next_health = room.next_reconcile
            room.next_extended = now + float(self.settings(area_id).diagnostics_interval or 300)
        self._reschedule()

    @callback
    def async_stop(self) -> None:
        self._running = False
        if self._timer:
            self._timer()
            self._timer = None

    def settings(self, area_id: str) -> SupervisorSettings:
        return self.ctrl.lighting.supervisor_for(area_id)

    def room(self, area_id: str) -> RoomSupervision:
        return self.rooms.setdefault(area_id, RoomSupervision())

    def _check(self, area_id: str, entity_id: str) -> TargetCheck:
        return self.room(area_id).targets.setdefault(entity_id, TargetCheck(entity_id))

    @callback
    def async_config_changed(self) -> None:
        if self._running:
            for area_id in self.ctrl.config.enabled_rooms:
                self.room(area_id).next_reconcile = time.monotonic() + 1
            self._reschedule()

    # ---------------------------------------------------------------- hooks
    @callback
    def note_command(self, area_id: str, service: str, data_by_entity: dict[str, dict[str, Any]],
                     errors: dict[str, str], purpose: str) -> None:
        """Smart Occupancy sent a command: verify it after the delay (plus its transition)."""
        if not self._running or area_id not in self.ctrl.config.enabled_rooms:
            return
        settings = self.settings(area_id)
        now = time.monotonic()
        room = self.room(area_id)
        for entity_id, data in data_by_entity.items():
            tc = self._check(area_id, entity_id)
            tc.last_command = {"at": _iso(), "service": f"{domain(entity_id)}.{service}", "data": dict(data),
                               "purpose": purpose, "error": errors.get(entity_id), "want": _want(service, data)}
            tc.error = errors.get(entity_id)
            tc.pending_until = now + float(settings.verify_delay or 4) + float(data.get("transition") or 0)
            if purpose != "supervisor" and tc.episode != _want(service, data):
                tc.attempts, tc.episode, tc.logged, tc.next_retry = 0, None, None, 0.0
                tc.external_fixed = None
            room.next_reconcile = min(room.next_reconcile or tc.pending_until, tc.pending_until + 0.1)
        self._reschedule()

    @callback
    def note_external(self, area_id: str, entity_id: str, source: str) -> None:
        """A target changed and it was not Smart Occupancy: verify the room again soon."""
        if not self._running or area_id not in self.rooms:
            return
        room = self.room(area_id)
        room.next_reconcile = min(room.next_reconcile, time.monotonic() + float(self.settings(area_id).verify_delay or 4))
        self._reschedule()

    @callback
    def note_available(self, area_id: str, entity_id: str) -> None:
        """A device came back online: it starts over (no failure carried over) and is verified soon."""
        if not self._running or area_id not in self.rooms:
            return
        tc = self._check(area_id, entity_id)
        if tc.result == FAILED_DEVICE:
            tc.result = WAITING
        tc.attempts, tc.next_retry, tc.episode, tc.logged = 0, 0.0, None, None
        tc.pending_until = time.monotonic() + float(self.settings(area_id).verify_delay or 4)
        room = self.room(area_id)
        room.next_reconcile = min(room.next_reconcile, tc.pending_until + 0.1)
        self._reschedule()

    # --------------------------------------------------------------- timing
    def _reschedule(self) -> None:
        if not self._running:
            return
        dues = [t for r in self.rooms.values() for t in (r.next_reconcile, r.next_health, r.next_extended) if t]
        if not dues:
            return
        due = min(dues)
        if self._timer is not None and self._due is not None and self._due <= due:
            return
        if self._timer:
            self._timer()
        self._due = due
        self._timer = async_call_later(self.hass, max(0.2, due - time.monotonic()), self._fire)

    @callback
    def _fire(self, _now: datetime) -> None:
        self._timer = None
        self._due = None
        if not self._running:
            return
        now = time.monotonic()
        for area_id in list(self.ctrl.config.enabled_rooms):
            room = self.room(area_id)
            settings = self.settings(area_id)
            if room.next_health and room.next_health <= now:
                room.next_health = now + float(settings.health_interval or 60)
                self._health(area_id)
            if room.next_reconcile and room.next_reconcile <= now:
                room.next_reconcile = now + float(settings.reconcile_interval or 30)
                self.reconcile(area_id)
            if room.next_extended and room.next_extended <= now:
                room.next_extended = now + float(settings.diagnostics_interval or 300)
                self._extended(area_id)
        self._reschedule()

    # -------------------------------------------------------- reconciliation
    @callback
    def reconcile(self, area_id: str) -> dict[str, Any]:
        """Compare desired and actual states of a room; start the corrections that are authorized."""
        ctrl = self.ctrl
        room = self.room(area_id)
        settings = self.settings(area_id)
        if settings.mode == SupervisorMode.OFF:
            room.status, room.reason = "off", "The Room Supervisor is off for this room"
            return self.model(area_id)
        if area_id in self._busy:
            return self.model(area_id)  # a correction is being sent: checked again after it
        now = time.monotonic()
        plan = ctrl.effective_plan(area_id, "supervisor")
        rt = ctrl.runtime_for(area_id)
        automatic = (settings.mode == SupervisorMode.AUTOMATIC and ctrl.room_active(area_id) and not ctrl.manual_mode)
        seen: set[str] = set()
        corrections: list[PlanItem] = []
        for item in plan.items:
            entity_id = item.entity_id
            seen.add(entity_id)
            tc = self._check(area_id, entity_id)
            tc.rule = item.rule
            tc.actual = actual_state(self.hass, entity_id)
            if ctrl.identify.ops and ctrl.identify.covering({entity_id}):
                # Identify (App 0.9.0) holds it for a few seconds and restores it itself: not checked meanwhile.
                tc.result, tc.reason = PAUSED, "Being identified: checked again after the restore"
                continue
            result, reason, desired = self._classify(area_id, item, tc, rt, settings, now)
            previous = tc.result
            tc.result, tc.reason, tc.desired = result, reason, desired
            if result in (MISMATCH, EXTERNAL):
                due = self._correctable(area_id, item, tc, settings, now, external=result == EXTERNAL)
                if due is None and automatic:
                    if result == EXTERNAL:
                        tc.external_fixed = _want(item.service, item.data)  # corrected once; never again for this
                    tc.result = CORRECTING
                    corrections.append(item)
                elif due is not None:
                    tc.reason = f"{reason} · {due}"
                    if tc.next_retry > now:
                        room.next_reconcile = min(room.next_reconcile or tc.next_retry + 0.1, tc.next_retry + 0.1)
                self._log_mismatch(area_id, item, tc, automatic, previous)
            elif result == CONFLICT and tc.episode:
                self._log_mismatch(area_id, item, tc, automatic, previous)
            elif result == CORRECT and previous in (MISMATCH, CORRECTING, WAITING, FAILED_DEVICE, EXTERNAL) \
                    and tc.episode:
                self._log_verified(area_id, item, tc)
                tc.episode = tc.logged = None
                tc.attempts = 0
            elif result in (OVERRIDE,) and previous in (MISMATCH, CORRECTING) and tc.episode:
                self._log(area_id, "override", f"{self._name(entity_id)}: a manual change was made; the supervisor "
                          "does not correct it.", D_BLOCKED, key=("sup", entity_id, "override"))
                tc.episode = tc.logged = None
            if result in (CORRECT, NOT_SUPERVISED, NO_EXPECTATION, PENDING, OVERRIDE, PAUSED):
                if tc.episode and result != CORRECT:
                    tc.episode = tc.logged = None
                    tc.attempts = 0
        for gone in set(room.targets) - seen:
            del room.targets[gone]
        room.metrics["checks"] += 1
        room.last_check = _iso()
        self._room_status(area_id, plan)
        if corrections:
            self._busy.add(area_id)
            ctrl.create_task(self._correct(area_id, corrections))
        signature = (room.status, tuple((t.entity_id, t.result) for t in room.targets.values()))
        if signature != room.signature:
            room.signature = signature
            ctrl.notify()  # cards refetch only when something changed (routine checks write nothing)
        return self.model(area_id)

    def _classify(self, area_id: str, item: PlanItem, tc: TargetCheck, rt, settings: SupervisorSettings,
                  now: float) -> tuple[str, str, dict[str, Any] | None]:
        hass = self.hass
        code = item.reason_code
        cmd = tc.last_command or {}
        want = _want(item.service, item.data) if item.service else None
        if item.decision == SKIPPED and code in ("excluded", "covered_by_group", "zone", "shared_owner"):
            return NOT_SUPERVISED, item.reason, None
        if code == "manual_override":
            return OVERRIDE, "Manual override: respected, not corrected", None
        if code == "offline":
            return UNAVAILABLE, item.reason, None
        if code == "manual_mode":
            return PAUSED, "Manual mood: automatic lighting is paused", None
        if code == "conflict":
            return CONFLICT, item.reason, None
        desired = item.requested or None
        # Removals Smart Occupancy itself started (a preliminary light, a light a zone switched on): off is expected.
        result = self.ctrl.coordinator.occupancy.result(area_id)
        unconfirmed = result is not None and result.state != OccupancyState.CONFIRMED_OCCUPANCY
        removal_due = releases(item) or item.service == "turn_off" or (
            cmd.get("purpose") == "preliminary_end" and unconfirmed)  # the stage's switch-off holds until confirmation
        if (cmd.get("purpose") in _REMOVALS and cmd.get("service", "").endswith("turn_off") and not cmd.get("error")
                and entity_id_not_changed(rt, tc) and removal_due):
            if now < tc.pending_until:
                return WAITING, "Waiting for the device to report off", {"state": "off"}
            if reaches(hass, item.entity_id, "turn_off", {}, settings):
                return CORRECT, f"Off as expected ({cmd['purpose'].replace('_', ' ')})", {"state": "off"}
            tc.episode = tc.episode or "turn_off|[]"
            return MISMATCH, f"Expected off, actual {describe_actual(tc.actual)}", {"state": "off"}
        if item.decision == SKIPPED:
            return NOT_SUPERVISED, item.reason, None
        delay = (item.rule or {}).get("delay")
        if delay or code in ("delay_on", "delay_off"):
            # A matrix cell's Delay On / Delay Off (App 0.9.0): the light keeps its state until it ends.
            word = "Delay On" if (delay or {}).get("action", "on") == "on" and code != "delay_off" else "Delay Off"
            left = (delay or {}).get("remaining")
            return WAITING, f"{word} pending{f' ({round(left)} s left)' if left is not None else ''}", None
        if item.decision == UNCHANGED and code in ("already", "zone_unchanged", "vacant_ignore", "vacant_unset",
                                                  "stays_off", "dead_band"):
            return CORRECT, item.reason, desired
        if item.decision == UNCHANGED and code == "already_sent":
            if now < tc.pending_until:
                return WAITING, "Waiting for the device to report the command", desired
            if reaches(hass, item.entity_id, item.service or "", item.data, settings):
                return CORRECT, "Within tolerance of the requested state", desired
            tc.episode = tc.episode or want
            return MISMATCH, f"Expected {describe(item.service, item.data)}, actual {describe_actual(tc.actual)}", desired
        if item.decision == CHANGE:
            ext = rt.last_external.get(item.entity_id)
            if ext and ext.get("source") in _EXTERNAL_SOURCES and (not cmd or (ext.get("at") or "") >= cmd.get("at", "")):
                if tc.external_fixed == want:
                    return CONFLICT, (f"Changed again by {ext['source']} after a correction: another controller is "
                                      "suspected; not corrected again"), desired
                tc.episode = tc.episode or want
                return EXTERNAL, f"Changed by {ext['source']} at {ext.get('at')}", desired
            if want and cmd.get("want") == want:
                # Sent (failed, or accepted without the light following) and the rule still wants it.
                if now < tc.pending_until:
                    return WAITING, "Waiting for the device to report the command", desired
                tc.episode = tc.episode or want
                why = f"the command failed: {cmd['error']}" if cmd.get("error") else "not reported by the device"
                return MISMATCH, (f"Expected {describe(item.service, item.data)}, actual "
                                  f"{describe_actual(tc.actual)} ({why})"), desired
            return PENDING, "Differs from its rule; applied at the next lighting trigger (not by the supervisor)", desired
        return NO_EXPECTATION, item.reason, None

    def _correctable(self, area_id: str, item: PlanItem, tc: TargetCheck, settings: SupervisorSettings,
                     now: float, *, external: bool = False) -> str | None:
        """None when a correction may be sent now; otherwise why not (shown with the mismatch)."""
        if external and not settings.correct_external:
            return "changed by something other than Smart Occupancy: reported, not corrected (room setting)"
        if settings.mode != SupervisorMode.AUTOMATIC:
            return "Observing: not corrected"
        if not self.ctrl.room_active(area_id):
            return "the room is Observing: not corrected"
        if self.ctrl.manual_mode:
            return "Manual mood: not corrected"
        if area_id in self.ctrl.pending_rooms:
            return "a lighting change is queued"
        if tc.attempts >= int(settings.max_retries or 0):
            tc.result = FAILED_DEVICE
            return (f"no correction left ({tc.attempts} of {int(settings.max_retries or 0)}): device failure "
                    "suspected (Home Assistant's reported state may differ from the physical output)")
        if now < tc.next_retry:
            return f"next correction in {round(tc.next_retry - now)} s"
        hour = now - 3600
        while tc.corrections and tc.corrections[0] < hour:
            tc.corrections.popleft()
        if len(tc.corrections) >= int(settings.max_per_hour or 6):
            return f"rate limit: {len(tc.corrections)} corrections in the last hour"
        return None

    async def _correct(self, area_id: str, items: list[PlanItem]) -> None:
        try:
            settings = self.settings(area_id)
            for item in items:
                tc = self._check(area_id, item.entity_id)
                service, data = (item.service or "turn_on", item.data)
                removal = tc.desired == {"state": "off"} and (tc.last_command or {}).get("purpose") in _REMOVALS
                if removal:
                    service, data = "turn_off", {}
                tc.attempts += 1
                now = time.monotonic()
                tc.corrections.append(now)
                tc.next_retry = now + float(settings.retry_delay or 5) * (2 ** (tc.attempts - 1))
                purpose = (tc.last_command or {}).get("purpose", "lighting") if removal else "supervisor"
                errors = await self.ctrl.async_supervisor_correct(area_id, item, service, data, purpose=purpose)
                error = errors.get(item.entity_id)
                tc.last_correction = {"at": _iso(), "service": f"{domain(item.entity_id)}.{service}", "data": data,
                                      "attempt": tc.attempts, "result": error or "sent"}
                self.room(area_id).metrics["corrections"] += 1
                self._log(area_id, "correct", (f"{self._name(item.entity_id)}: expected {describe(service, data)}, "
                          f"actual {describe_actual(tc.actual)}. Corrective {domain(item.entity_id)}.{service} issued "
                          f"(attempt {tc.attempts} of {int(settings.max_retries or 0)})"
                          + (f"; it failed: {error}." if error else ".")),
                          FAILED if error else NOTED, key=("sup", item.entity_id, "correct", tc.episode),
                          details={"target": item.entity_id, "service": f"{domain(item.entity_id)}.{service}",
                                   "data": data, "attempt": tc.attempts, "rule": (item.rule or {}).get("name")})
        finally:
            self._busy.discard(area_id)
            self.ctrl.notify()

    # ---------------------------------------------------------------- health
    def _health(self, area_id: str) -> None:
        ctrl = self.ctrl
        room = self.room(area_id)
        result = ctrl.coordinator.occupancy.result(area_id)
        unavailable: set[str] = set()
        if result is not None:
            unavailable |= {v.track.entity_id for v in result.signals if v.track.used and v.status == SignalStatus.UNKNOWN}
        for z in ctrl.zones.status(area_id):
            unavailable |= {s["entity_id"] for s in z["sensors"] if s["used"] and not s["available"]}
        controlled = ctrl.managed_room(area_id).controlled
        offline = sorted(e for e in controlled if (s := self.hass.states.get(e)) is None
                         or s.state in ("unavailable", "unknown"))
        new, back = unavailable - room.unavailable_sensors, room.unavailable_sensors - unavailable
        if new:
            self._log(area_id, "sensor", f"Sensor unavailable: {', '.join(self._name(e) for e in sorted(new))}. "
                      "It is not used as evidence of vacancy.", NOTED, key=("sup", "sensor", *sorted(new)))
        if back and not new:
            self._log(area_id, "sensor", f"Sensor available again: {', '.join(self._name(e) for e in sorted(back))}.",
                      NOTED, key=("sup", "sensor_back", *sorted(back)))
        room.unavailable_sensors = frozenset(unavailable)
        room.health = {"at": _iso(), "unavailable_sensors": sorted(unavailable), "unavailable_devices": offline,
                       "occupancy": result.state.value if result else None}

    def _extended(self, area_id: str) -> None:
        room = self.room(area_id)
        by: dict[str, int] = {}
        for tc in room.targets.values():
            by[tc.result] = by.get(tc.result, 0) + 1
        room.extended = {"at": _iso(), "results": by, "metrics": dict(room.metrics)}

    # ---------------------------------------------------------------- status
    def _room_status(self, area_id: str, plan) -> None:
        room = self.room(area_id)
        settings = self.settings(area_id)
        results = [tc.result for tc in room.targets.values()]
        result = self.ctrl.coordinator.occupancy.result(area_id)
        n = lambda *rs: sum(1 for r in results if r in rs)  # noqa: E731
        if n(FAILED_DEVICE):
            status, reason = "device_failure", f"{n(FAILED_DEVICE)} device(s) did not reach the requested state"
        elif n(CONFLICT):
            status, reason = "conflict", "Another controller appears to change these lights"
        elif n(CORRECTING):
            status, reason = "correcting", f"Correcting {n(CORRECTING)} device(s)"
        elif n(MISMATCH, EXTERNAL):
            status, reason = "mismatch", f"{n(MISMATCH, EXTERNAL)} discrepanc{'y' if n(MISMATCH, EXTERNAL) == 1 else 'ies'}"
        elif n(WAITING):
            status, reason = "waiting", "Waiting for devices to report a command"
        elif room.unavailable_sensors or n(UNAVAILABLE):
            parts = []
            if room.unavailable_sensors:
                parts.append(f"{len(room.unavailable_sensors)} sensor(s) unavailable")
            if n(UNAVAILABLE):
                parts.append(f"{n(UNAVAILABLE)} device(s) unavailable")
            status, reason = "degraded", ", ".join(parts)
        elif result is None or result.state == OccupancyState.UNCERTAIN:
            status, reason = "unknown", "Occupancy is not known: not reported as healthy"
        elif settings.mode == SupervisorMode.OBSERVING or not self.ctrl.room_active(area_id):
            status, reason = "observing", "No discrepancy found · " + (
                "Observing: the supervisor reports, it does not correct" if settings.mode == SupervisorMode.OBSERVING
                else "the room's automatic lighting is Observing")
        else:
            status, reason = "healthy", "Every supervised device reports what its rule requests"
        bad = status in ("device_failure", "conflict", "mismatch", "correcting")
        if status == "healthy" and room.was_unhealthy:
            self._log(area_id, "healthy", "Room Supervisor: every device reports its requested state again. "
                      "Supervisor healthy.", EXECUTED, key=("sup", "healthy"))
        if bad:
            room.was_unhealthy = True
        elif status in ("healthy", "observing"):
            room.was_unhealthy = False
        if status in ("healthy", "observing"):
            room.last_ok = _iso()
        room.status, room.reason = status, reason

    # ------------------------------------------------------------------ logs
    def _name(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        return (state.attributes.get("friendly_name") if state else None) or entity_id

    def _log(self, area_id: str, what: str, text: str, status: str, *, key: tuple, details: dict | None = None) -> None:
        explain = self.ctrl.explain
        self.ctrl.decisions.record(
            log_subject(area_id), module=MODULE_LIGHTING, trigger="supervisor", kind="supervisor",
            action={"mismatch": "Mismatch detected", "correct": "Correction requested", "verified": "Verified",
                    "failed": "Device failure suspected", "conflict": "Conflict suspected",
                    "override": "Manual override respected", "sensor": "Sensor availability",
                    "healthy": "Room healthy"}[what],
            explanation=f"{self._room_name(area_id)}: {text}", mode=explain.mode(area_id), status=status,
            details=details or {}, key=key,
        )

    def _room_name(self, area_id: str) -> str:
        from .discovery import area_name

        return area_name(self.hass, area_id) or area_id

    def _log_mismatch(self, area_id: str, item: PlanItem, tc: TargetCheck, automatic: bool, previous: str) -> None:
        name = self._name(item.entity_id)
        if tc.result == FAILED_DEVICE:
            if tc.logged != "failed":
                tc.logged = "failed"
                self.room(area_id).metrics["failures"] += 1
                self._log(area_id, "failed", f"{name} did not reach {describe(item.service, item.data) if item.service else 'its state'} "
                          f"after {tc.attempts} correction(s): device failure suspected. Home Assistant reports "
                          f"{describe_actual(tc.actual)}; its reported state may differ from the physical output. "
                          "Not corrected again until the device changes or comes back online.", FAILED,
                          key=("sup", item.entity_id, "failed"))
            return
        if tc.result == CONFLICT:
            if tc.logged != "conflict":
                tc.logged = "conflict"
                self.room(area_id).metrics["conflicts"] += 1
                self._log(area_id, "conflict", f"{name}: {tc.reason}.", D_BLOCKED, key=("sup", item.entity_id, "conflict"))
            return
        if tc.logged is None:
            tc.logged = "mismatch"
            self.room(area_id).metrics["mismatches"] += 1
            rule = (item.rule or {}).get("name")
            tail = "Correction requested." if tc.result == CORRECTING else f"Not corrected: {tc.reason.split(' · ')[-1]}."
            self._log(area_id, "mismatch", f"{name} expected {describe_desired(tc.desired)}, actual "
                      f"{describe_actual(tc.actual)}{f' ({rule})' if rule else ''}. {tail}",
                      NOTED if automatic else OBSERVED, key=("sup", item.entity_id, "mismatch", tc.episode),
                      details={"target": item.entity_id, "rule": rule, "desired": tc.desired, "actual": tc.actual})

    def _log_verified(self, area_id: str, item: PlanItem, tc: TargetCheck) -> None:
        if tc.last_correction is None:
            return  # it reached the state by itself (a late report): nothing was corrected
        self.room(area_id).metrics["verified"] += 1
        tc.last_verification = {"at": _iso(), "ok": True}
        self._log(area_id, "verified", f"{self._name(item.entity_id)} verified at {describe_desired(tc.desired)}.",
                  EXECUTED, key=("sup", item.entity_id, "verified"))

    # ----------------------------------------------------------------- model
    def model(self, area_id: str, *, details: bool = False) -> dict[str, Any]:
        room = self.room(area_id)
        settings = self.settings(area_id)
        names = {e: self._name(e) for e in room.targets}
        discrepancies = sum(1 for t in room.targets.values() if t.result in (MISMATCH, EXTERNAL, CORRECTING,
                                                                               FAILED_DEVICE, CONFLICT))
        out: dict[str, Any] = {
            "status": room.status, "status_word": STATUS_WORDS.get(room.status, room.status), "reason": room.reason,
            "mode": settings.mode.value if settings.mode else None, "discrepancies": discrepancies,
            "last_check": room.last_check, "last_ok": room.last_ok, "health": room.health,
            "metrics": dict(room.metrics),
            "settings": settings.as_dict(), "room_settings": self.ctrl.lighting.room(area_id).supervisor.as_dict(),
            "global_settings": self.ctrl.lighting.supervisor.as_dict(),
        }
        out["targets"] = [t.as_dict(self.hass, names) for t in room.targets.values()] if details else [
            {"entity_id": t.entity_id, "name": names[t.entity_id], "result": t.result, "rule": (t.rule or {}).get("name"),
             "desired": t.desired, "actual": t.actual, "type": "relay" if is_relay(t.entity_id) else "light"}
            for t in room.targets.values()]
        if details:
            out["extended"] = room.extended
        out["decisions"] = self.ctrl.decisions.model(log_subject(area_id))
        return out


def log_subject(area_id: str) -> str:
    """The supervisor's own decision log of a room (the room's lighting log stays readable)."""
    return f"{area_id}#supervisor"


def describe_desired(desired: dict[str, Any] | None) -> str:
    if not desired:
        return "—"
    if desired.get("state") == "off":
        return "off"
    return describe("turn_on", desired)


def entity_id_not_changed(rt, tc: TargetCheck) -> bool:
    """No change by anyone else (person or automation) since Smart Occupancy's command."""
    ext = rt.last_external.get(tc.entity_id)
    cmd = tc.last_command or {}
    return not ext or (ext.get("at") or "") < (cmd.get("at") or "")
