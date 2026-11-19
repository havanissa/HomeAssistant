"""Identify a light (App 0.9.0, Phase A6.2): full brightness for about 2.5 s, then exactly what it was.

An administrator presses *Identify* in the light inspector. The lighting
controller (the only executor) then:

1. takes a temporary, in-memory identification hold on that one target (a
   light, a relay output, or - only when explicitly chosen - a whole group),
2. captures its state and the attributes it supports,
3. switches it on at 100 % (brightness only where it is supported; an on/off
   light or relay is just switched on),
4. waits ``IDENTIFY_SECONDS``,
5. restores the captured state - unless something newer superseded it,
6. releases the hold. Normal automation continues from its latest winning rule.

Safety
------
* Every command goes through the controller's ``_send`` (the fenced command
  path: refused while fenced, the companion's executor checks the lease) with
  its own context, so the light's reports are recognised as ours: no manual
  override, no change of mood, occupancy, rules or brightness matrices, no
  Supervisor activity (its commands are not reported to the Room Supervisor,
  which skips the target while the hold lasts).
* While the hold lasts, the controller plans with the captured state (the
  light "as it was"), so a rule that wants something else sends it - and that
  newer command **supersedes** the identification: no restore over it. A
  person (Home Assistant, a wall switch, a remote reported with its own user)
  or a Priority Input supersedes it the same way. A global adjustment update
  waits for the restore (per-target serialisation) instead.
* Before restoring, the light must still show what Identify set (on, full
  brightness where supported); otherwise someone changed it and nothing is
  restored.
* A group is identified only when that is explicitly chosen; a group's known
  members are restored one by one to their own captured states.
* Crash or loss of authority: a small journal (what to restore, until when) is
  saved with the controller's state at the start. A controller that starts
  within ``RECOVERY_SECONDS`` restores a light only if it still shows what
  Identify set and has not changed since the identification ended; otherwise
  the journal is dropped. Nothing is suppressed after a restart.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .fixtures import restrict
from .lighting import light_capabilities
from .targets import is_relay

if TYPE_CHECKING:
    from .controller import LightingController

_LOGGER = logging.getLogger(__name__)

IDENTIFY_SECONDS = 2.5
RECOVERY_SECONDS = 120     # a journal older than this is never restored
RECOVERY_SETTLE_SECONDS = 10
MAX_CONCURRENT = 4
FULL = 250                 # brightness (0-255) that counts as "still at full brightness"
PURPOSES = frozenset({"identify", "identify_restore", "identify_recovery"})
#: Command purposes that wait for the restore instead of superseding the identification.
WAITS = frozenset({"adaptive"})
_COLOUR_ATTRS = {"color_temp": "color_temp_kelvin", "hs": "hs_color", "rgb": "rgb_color", "rgbw": "rgbw_color",
                 "rgbww": "rgbww_color", "xy": "xy_color"}
_NO_EFFECT = (None, "", "none", "None", "off", "Off", "solid", "Solid")


class IdentifyError(Exception):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def snapshot(state: State | None, *, onoff: bool = False) -> dict[str, Any] | None:
    """What can be restored of a light: on/off, and while on, its brightness, colour and effect (if supported).

    ``onoff``: a Dumb Light (``fixtures.restrict``): only on/off is ever sent to it.
    """
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    attrs = state.attributes
    out: dict[str, Any] = {"state": state.state}
    if state.state != "on" or state.entity_id.startswith("switch.") or onoff:
        return out
    modes = set(attrs.get("supported_color_modes") or [])
    if isinstance(attrs.get("brightness"), (int, float)) and modes - {"onoff"}:
        out["brightness"] = int(attrs["brightness"])
    mode = attrs.get("color_mode")
    key = _COLOUR_ATTRS.get(mode or "")
    if key and mode in modes and attrs.get(key) is not None:
        value = attrs[key]
        out[key] = list(value) if isinstance(value, (list, tuple)) else value
    effect = attrs.get("effect")
    if effect not in _NO_EFFECT and effect in (attrs.get("effect_list") or []):
        out["effect"] = effect
    return out


def restore_command(entity_id: str, snap: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The one command that returns a light to a snapshot (only attributes it reported, i.e. supports)."""
    if snap.get("state") != "on":
        return "turn_off", {}
    if is_relay(entity_id):
        return "turn_on", {}
    data = {k: v for k, v in snap.items() if k != "state"}
    return "turn_on", data


def describe(snap: dict[str, Any] | None) -> str:
    if not snap:
        return "unknown"
    if snap.get("state") != "on":
        return "off"
    parts = ["on"]
    if isinstance(snap.get("brightness"), int):
        parts.append(f"{round(snap['brightness'] / 255 * 100)} %")
    if snap.get("color_temp_kelvin"):
        parts.append(f"{snap['color_temp_kelvin']} K")
    for key, word in (("rgb_color", "RGB"), ("hs_color", "HS"), ("xy_color", "XY"), ("rgbw_color", "RGBW"),
                      ("rgbww_color", "RGBWW")):
        if snap.get(key):
            parts.append(f"{word} {','.join(str(round(v, 3) if isinstance(v, float) else v) for v in snap[key])}")
    if snap.get("effect"):
        parts.append(f"effect {snap['effect']}")
    return ", ".join(parts)


@dataclass(slots=True)
class Operation:
    area_id: str
    target: str                         # the light, relay or group commanded
    restore: dict[str, dict[str, Any]]  # entity -> its snapshot (a group: its known members, else the target)
    group: bool
    user_id: str | None
    started: datetime
    until: datetime
    phase: str = "starting"             # starting | on | restoring | done
    outcome: str | None = None          # restored | superseded | interrupted | failed | stopped
    reason: str | None = None
    expect_full: bool = True
    target_snapshot: dict[str, Any] | None = None  # the commanded entity as it was (a group: for planning only)
    groups: dict[str, dict[str, Any] | None] = field(default_factory=dict)  # a member's managed group (planning)
    look: dict[str, Any] = field(default_factory=dict)  # colour attributes the light reported right after "on"
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def entities(self) -> set[str]:
        return {self.target, *self.restore, *self.groups}

    def as_dict(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        return {"area_id": self.area_id, "entity_id": self.target, "group": self.group,
                "members": sorted(e for e in self.restore if e != self.target), "phase": self.phase,
                "outcome": self.outcome, "reason": self.reason, "started": _iso(self.started),
                "until": _iso(self.until), "remaining": round(max(0.0, (self.until - now).total_seconds()), 1),
                "snapshot": {e: describe(s) for e, s in self.restore.items()}, "seconds": IDENTIFY_SECONDS}


class IdentifyManager:
    """The identification holds of one lighting controller (see the module docstring)."""

    def __init__(self, hass: HomeAssistant, ctrl: LightingController) -> None:
        self.hass = hass
        self.ctrl = ctrl
        self.ops: dict[str, Operation] = {}        # target -> operation
        self._covers: dict[str, Operation] = {}    # every entity of an operation -> it
        self.journal: dict[str, dict[str, Any]] = {}
        self.recent: dict[str, deque[dict[str, Any]]] = {}
        self._recovery: Any = None

    # ------------------------------------------------------------ storage
    def persist(self) -> dict[str, Any]:
        return dict(self.journal)

    def restore(self, data: Any) -> None:
        self.journal = {e: dict(j) for e, j in (data or {}).items()
                        if isinstance(e, str) and isinstance(j, dict) and isinstance(j.get("restore"), dict)} \
            if isinstance(data, dict) else {}

    # --------------------------------------------------------------- read
    def covering(self, entity_ids) -> list[Operation]:
        """Operations whose hold includes any of these entities (a group's members included)."""
        seen: dict[int, Operation] = {}
        for entity_id in entity_ids:
            if (op := self._covers.get(entity_id)) is not None and op.phase != "done":
                seen[id(op)] = op
        return list(seen.values())

    def held(self, entity_id: str) -> Operation | None:
        op = self._covers.get(entity_id)
        return op if op is not None and op.phase != "done" else None

    def status(self, entity_id: str) -> dict[str, Any]:
        op = self.held(entity_id)
        return {"active": op.as_dict() if op else None, "recent": list(self.recent.get(entity_id, ()))[::-1],
                "seconds": IDENTIFY_SECONDS}

    def snapshot_for_plan(self, entity_id: str) -> dict[str, Any] | None:
        """The state the controller plans with while the hold lasts (the light as it was before)."""
        op = self.held(entity_id)
        if op is None or op.phase == "starting":
            return None
        if entity_id in op.restore:
            return op.restore[entity_id]
        if entity_id in op.groups:
            return op.groups[entity_id]
        return op.target_snapshot if entity_id == op.target else None

    def diagnostics(self) -> dict[str, Any]:
        return {"active": [op.as_dict() for op in self.ops.values()], "journal": dict(self.journal)}

    # -------------------------------------------------------- the operation
    def _members(self, area_id: str, entity_id: str) -> tuple[bool, list[str]]:
        managed = self.ctrl.managed_room(area_id)
        if entity_id in managed.members:
            return True, sorted(managed.members[entity_id])
        if entity_id in managed.opaque_groups:
            return True, []
        state = self.hass.states.get(entity_id)
        members = state.attributes.get("entity_id") if state else None
        if isinstance(members, (list, tuple)) and members and entity_id.startswith("light."):
            return True, sorted(m for m in members if isinstance(m, str))
        return False, []

    def check(self, area_id: str, entity_id: str, *, whole_group: bool = False) -> dict[str, Any]:
        """Whether Identify can run now (raises IdentifyError with a code the interface explains)."""
        ctrl = self.ctrl
        if area_id not in ctrl.config.enabled_rooms:
            raise IdentifyError("not_configured", "The room is not configured")
        managed = ctrl.managed_room(area_id)
        known = set(managed.targets) | {m for ms in managed.members.values() for m in ms}
        if entity_id not in known:
            raise IdentifyError("not_configured", f"{entity_id} is not a light Smart Occupancy controls in this room")
        if entity_id in managed.excluded and entity_id not in managed.covered_by:
            raise IdentifyError("excluded", f"{entity_id} is excluded in this room")
        if ctrl.fenced or ctrl.dormant or getattr(ctrl, "_stopped", False):
            raise IdentifyError("not_permitted", "Lighting control is changing owner: try again in a moment")
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            raise IdentifyError("unavailable", f"{entity_id} is unavailable: it cannot be identified")
        is_group, members = self._members(area_id, entity_id)
        if is_group and not whole_group:
            raise IdentifyError("group", "This is a group: choose one of its lights, or identify the whole group",
                                members=members)
        if self.covering({entity_id, *members}):
            raise IdentifyError("busy", "This light is being identified already")
        if len(self.ops) >= MAX_CONCURRENT:
            raise IdentifyError("busy", f"At most {MAX_CONCURRENT} lights are identified at a time")
        return {"group": is_group, "members": members}

    async def async_identify(self, area_id: str, entity_id: str, *, whole_group: bool = False,
                             user_id: str | None = None) -> dict[str, Any]:
        """Run one identification; returns its outcome (after the restore)."""
        info = self.check(area_id, entity_id, whole_group=whole_group)
        fixtures = self.ctrl.lighting.room(area_id).fixtures

        def dumb(e: str) -> bool:
            f = fixtures.get(e)
            return bool(f is not None and f.dumb)

        restore: dict[str, dict[str, Any]] = {}
        for e in (info["members"] if info["group"] and info["members"] else [entity_id]):
            snap = snapshot(self.hass.states.get(e), onoff=dumb(e))
            if snap is not None:
                restore[e] = snap
        if not restore:
            raise IdentifyError("unavailable", "Nothing to restore afterwards: the light reports no usable state")
        caps = restrict(light_capabilities(self.hass, entity_id), fixtures.get(entity_id)) \
            if not is_relay(entity_id) else None
        bright = bool(caps and caps.brightness)
        now = dt_util.utcnow()
        op = Operation(area_id, entity_id, restore, info["group"], user_id, now,
                       now + timedelta(seconds=IDENTIFY_SECONDS), expect_full=bright,
                       target_snapshot=snapshot(self.hass.states.get(entity_id), onoff=dumb(entity_id)))
        # A member's managed group is held too (plans and the Supervisor see it as before; its reports are ours).
        managed = self.ctrl.managed_room(area_id)
        if (group := managed.covered_by.get(entity_id)) is not None and not info["group"]:
            op.groups = {group: snapshot(self.hass.states.get(group), onoff=dumb(group))}
        self.ops[entity_id] = op
        for e in op.entities:
            self._covers[e] = op
        try:
            self.journal[entity_id] = {"area_id": area_id, "started": _iso(now), "until": _iso(op.until),
                                       "restore": restore, "full": bright}
            await self.ctrl.async_save_state_now()  # (the journal must survive a crash during the next seconds)
            await self._run(op, {"brightness_pct": 100} if bright else {})
        except asyncio.CancelledError:
            op.outcome, op.reason = "stopped", "the lighting controller stopped (the journal restores it on start)"
            raise
        finally:
            op.phase = "done"
            op.done.set()
            self.ops.pop(entity_id, None)
            for e in op.entities:
                if self._covers.get(e) is op:
                    del self._covers[e]
            if op.outcome != "stopped" and not (op.outcome == "failed" and op.reason and op.reason.startswith(
                    "restore failed")):
                self.journal.pop(entity_id, None)
            self._remember(op)
            if op.outcome != "stopped":
                self.ctrl.explain_identify(op)
            self.ctrl.mark_dirty()
            self.ctrl.notify()
        return op.as_dict()

    async def _run(self, op: Operation, data: dict[str, Any]) -> None:
        ctrl = self.ctrl
        ctrl.notify()
        if op.group:
            ctrl.expect_reports(op.restore, "turn_on")
        if op.groups:
            ctrl.expect_reports({g: {"state": "on"} for g in op.groups}, "turn_on")
        errors = await ctrl.async_identify_send(op.area_id, "turn_on", {op.target: data}, purpose="identify")
        if errors.get(op.target):
            op.outcome, op.reason = "failed", f"the light did not accept the command: {errors[op.target]}"
            return
        op.phase = "on"
        op.until = dt_util.utcnow() + timedelta(seconds=IDENTIFY_SECONDS)
        if (now_state := self.hass.states.get(op.target)) is not None:
            op.look = {k: now_state.attributes.get(k) for k in (*_COLOUR_ATTRS.values(), "color_mode", "effect")}
        ctrl.notify()
        if not op.stop.is_set():
            # Home Assistant's timer (the host's clock in the App; the frozen clock in tests), interruptible.
            cancel = async_call_later(self.hass, IDENTIFY_SECONDS, callback(lambda _now: op.stop.set()))
            try:
                await op.stop.wait()
            finally:
                cancel()
        if op.outcome in ("superseded", "interrupted"):
            return  # (something newer owns the light now: never restored over it)
        op.phase = "restoring"
        changed = self._changed_since(op)
        if changed:
            op.outcome, op.reason = "interrupted", changed
            return
        if op.group:
            ctrl.expect_reports(op.restore, None)
        if op.groups:
            ctrl.expect_reports({g: s or {"state": "off"} for g, s in op.groups.items()}, None)
        failed = []
        for entity_id, snap in op.restore.items():
            service, payload = restore_command(entity_id, snap)
            errors = await ctrl.async_identify_send(op.area_id, service, {entity_id: payload},
                                                    purpose="identify_restore")
            if errors.get(entity_id):
                failed.append(f"{entity_id}: {errors[entity_id]}")
        if failed:
            op.outcome, op.reason = "failed", "restore failed: " + "; ".join(failed)
        else:
            op.outcome = "restored"
            op.reason = "returned to " + "; ".join(describe(s) for s in op.restore.values())

    def _changed_since(self, op: Operation) -> str | None:
        """Why the light no longer shows what Identify set (None: it does)."""
        state = self.hass.states.get(op.target)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return "the light became unavailable"
        if state.state != "on":
            return "it was switched off meanwhile"
        if op.expect_full and isinstance(state.attributes.get("brightness"), (int, float)) \
                and state.attributes["brightness"] < FULL:
            return "its brightness was changed meanwhile"
        if op.look and any(state.attributes.get(k) != v for k, v in op.look.items() if v is not None):
            return "its colour was changed meanwhile"
        return None

    def _remember(self, op: Operation) -> None:
        entry = {"at": _iso(dt_util.utcnow()), "started": _iso(op.started), "outcome": op.outcome,
                 "reason": op.reason, "group": op.group, "user_id": op.user_id}
        self.recent.setdefault(op.target, deque(maxlen=8)).append(entry)
        _LOGGER.info("lighting_identify area=%s light=%s outcome=%s reason=%s", op.area_id, op.target, op.outcome,
                     op.reason)

    # -------------------------------------------------- competing commands
    async def before_command(self, entity_ids, purpose: str) -> None:
        """Another command for a light on hold: it supersedes the identification (or waits, for WAITS)."""
        for op in self.covering(entity_ids):
            if purpose not in WAITS and op.outcome is None:
                op.outcome = "superseded"
                op.reason = f"a newer {purpose.replace('_', ' ')} command took over (not restored over it)"
                op.stop.set()
            try:
                async with asyncio.timeout(IDENTIFY_SECONDS + 20):
                    await op.done.wait()
            except TimeoutError:
                _LOGGER.warning("lighting_identify_wait_timeout light=%s", op.target)

    @callback
    def note_change(self, entity_id: str, source: str) -> None:
        """A light on hold changed and it was not us: a person (or something else) took over."""
        op = self.held(entity_id)
        if op is None or op.outcome is not None:
            return
        op.outcome = "interrupted"
        op.reason = f"changed {source} during the identification (not restored over it)"
        op.stop.set()

    @callback
    def async_stop(self) -> None:
        if self._recovery:
            self._recovery()
            self._recovery = None
        for op in list(self.ops.values()):
            op.stop.set()

    # ------------------------------------------------------------ recovery
    @callback
    def async_started(self) -> None:
        """After a restart: journals of an interrupted identification are checked once things settled."""
        if not self.journal:
            return

        @callback
        def fire(_now: datetime) -> None:
            self._recovery = None
            self.ctrl.create_task(self.async_recover())

        self._recovery = async_call_later(self.hass, RECOVERY_SETTLE_SECONDS, fire)

    async def async_recover(self) -> list[dict[str, Any]]:
        """Restore what an interrupted identification left at full brightness - only if nothing changed since."""
        out = []
        now = dt_util.utcnow()
        for target, entry in list(self.journal.items()):
            self.journal.pop(target, None)
            started = dt_util.parse_datetime(entry.get("started") or "")
            until = dt_util.parse_datetime(entry.get("until") or "")
            area_id = entry.get("area_id")
            result = {"entity_id": target, "restored": [], "skipped": {}}
            if started is None or until is None or (now - started).total_seconds() > RECOVERY_SECONDS \
                    or area_id not in self.ctrl.config.enabled_rooms:
                result["skipped"][target] = "too old: never replayed"
                out.append(result)
                continue
            state = self.hass.states.get(target)
            why = None
            if state is None or state.state != "on":
                why = "not on any more"
            elif entry.get("full") and isinstance(state.attributes.get("brightness"), (int, float)) \
                    and state.attributes["brightness"] < FULL:
                why = "its brightness changed"
            elif state.last_updated > until + timedelta(seconds=2):
                why = "it changed after the identification"
            if why:
                result["skipped"][target] = why
                out.append(result)
                continue
            for entity_id, snap in (entry.get("restore") or {}).items():
                if not isinstance(snap, dict):
                    continue
                service, payload = restore_command(entity_id, snap)
                errors = await self.ctrl.async_identify_send(area_id, service, {entity_id: payload},
                                                             purpose="identify_recovery")
                if errors.get(entity_id):
                    result["skipped"][entity_id] = errors[entity_id]
                else:
                    result["restored"].append(entity_id)
            self.recent.setdefault(target, deque(maxlen=8)).append({
                "at": _iso(dt_util.utcnow()), "started": entry.get("started"),
                "outcome": "restored" if result["restored"] else "failed",
                "reason": "restored after a restart of the lighting controller", "group": False, "user_id": None})
            out.append(result)
        self.ctrl.mark_dirty()
        self.ctrl.notify()
        _LOGGER.info("lighting_identify_recovery %s", out)
        return out

