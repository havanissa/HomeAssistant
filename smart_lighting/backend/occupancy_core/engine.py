"""Occupancy engine: evidence fusion and the per-room state machine.

Shared engine package (Phase A3.2): the integration and the Smart Lighting App
run this same file (byte-identical copies, see ``occupancy_core/__init__.py``).
The rules are those of integration 0.6.10, unchanged.

Pure logic with no Home Assistant dependency: the caller feeds it signal
values with timestamps and asks for an evaluation at a given time. That keeps
every rule testable without timers, and makes the engine deterministic.

Signal values
-------------
Every assigned signal is reduced to ``active``, ``inactive`` or ``unknown``.
``unknown`` covers unavailable, unknown, not yet loaded and unrecognised
states. It is never treated as "off": it is neither evidence of presence nor
evidence of vacancy.

Roles
-----
Trigger (PIR, other)
    Becoming active is an activity event and gives immediate (possible)
    occupancy. A trigger counts as evidence only while it is "fresh": one
    that stays active longer than ``trigger_stuck_after`` is treated as stuck
    and ignored, so a stuck PIR cannot hold a room occupied forever.
Maintain (radar, PC activity, bed)
    While active, the room stays occupied and vacancy cannot be confirmed.
    A clear shorter than ``dropout_grace`` is bridged (radar dropouts).
Confirm vacancy
    Like Maintain while active. When it has cleared (past the dropout grace),
    that is positive evidence of vacancy, so vacancy needs only
    ``exit_confirmation`` of quiet instead of ``inactivity_timeout``.
Door contacts (any role except Ignore)
    Only transitions count. A door movement is activity, but on its own it
    never makes a room occupied. A door movement after which nothing new
    starts in the room is exit evidence (fast vacancy path); movement in an
    adjacent room right after it strengthens that evidence.
Ignore
    Kept in the configuration, not used. A signal excluded in the Presence
    tab (``enabled: false``, 0.3.8) is treated the same way while it is
    excluded; its value is still tracked for diagnostics.

States and transitions
----------------------
``uncertain``
    No occupancy-capable signal has a known value (all unavailable/unknown),
    or none is configured. The occupied output keeps its last value: an
    outage never causes vacancy.
``possible_occupancy``
    New, uncorroborated evidence: one trigger event, a presence signal that
    has just turned on, or (with occupied still off) a door movement alone.
``confirmed_occupancy``
    Corroborated evidence: a presence signal active for at least
    ``correlation_window``, two different signals within the window, or a
    trigger active for the whole window. Stays confirmed while any evidence
    remains.

    Preliminary signals (0.3.11): in a room with possible occupancy lighting,
    the signals that start the preliminary light cannot confirm occupancy by
    themselves - neither by staying active (a PIR that reports "occupied" for
    60 s after one movement would otherwise confirm every passing movement
    after the window) nor together with each other. Another signal must
    agree (radar, PC activity, a door that is not a preliminary signal, ...).
``possible_vacancy``
    The room was occupied and no evidence remains. Vacancy is confirmed once
    the room has been quiet for the required time:
    ``exit_confirmation`` when there is positive vacancy evidence (a
    Confirm-vacancy signal cleared, or a door exit) or the visit was never
    confirmed; otherwise ``inactivity_timeout``. Unknown presence signals
    always force the long path. Nothing is confirmed during ``startup_hold``.
``vacant``
    Quiet for the required time, or never occupied.

Zone holds (0.6.0): an occupied zone of the room whose ``holds_room`` is set
keeps an *occupied* room occupied (``external_hold``), like an active Maintain
signal. A hold never makes a vacant or uncertain room occupied; when the last
hold ends, the room's quiet period starts then.

Any new evidence during ``possible_vacancy`` returns to occupancy. Because
occupied -> vacant always needs at least ``exit_confirmation`` of quiet, and
presence dropouts are bridged, the output does not toggle when sensors
briefly disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from .types import OccupancyState, OccupancyTiming, SensorAssignment, SensorKind, SensorRole

# State strings (lower-cased) that mean active / inactive for non-binary
# entities. Anything else is "unknown", never "inactive".
ACTIVE_STATES = frozenset({
    "on", "home", "playing", "paused", "buffering", "active", "unlocked",
    "detected", "occupied", "open", "true", "in_use", "busy",
})
INACTIVE_STATES = frozenset({
    "off", "not_home", "away", "idle", "standby", "inactive", "locked",
    "clear", "closed", "false", "unoccupied", "not_in_use", "available",
})
UNKNOWN_STATES = frozenset({"unknown", "unavailable", "none", ""})

PRESENCE_ROLES = frozenset({SensorRole.MAINTAIN, SensorRole.CONFIRM_VACANCY})

MAX_REASON_LENGTH = 255


class Value(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class SignalStatus(StrEnum):
    """What one signal contributes right now (shown in diagnostics)."""

    ACTIVE = "active"          # counts as evidence of presence/activity
    GRACE = "grace"            # presence cleared moments ago, still bridged
    CLEARED = "cleared"        # known inactive
    RECENT = "recent"          # door: moved within the correlation window
    IDLE = "idle"              # door: no recent movement
    STUCK = "stuck"            # active for too long; ignored
    UNKNOWN = "unknown"        # unavailable / unknown; not evidence either way
    IGNORED = "ignored"        # role Ignore


def interpret(state: str | None, *, inverted: bool = False) -> Value:
    """Reduce a Home Assistant state string to a signal value."""
    if state is None:
        return Value.UNKNOWN
    text = str(state).strip().casefold()
    if text in UNKNOWN_STATES:
        return Value.UNKNOWN
    if text in ACTIVE_STATES:
        value = Value.ACTIVE
    elif text in INACTIVE_STATES:
        value = Value.INACTIVE
    else:
        return Value.UNKNOWN
    if inverted:
        return Value.INACTIVE if value == Value.ACTIVE else Value.ACTIVE
    return value


def _later(*times: datetime | None) -> datetime | None:
    known = [t for t in times if t is not None]
    return max(known) if known else None


@dataclass(slots=True)
class SignalTrack:
    """Everything the engine remembers about one signal. Bounded: O(1)."""

    assignment: SensorAssignment
    name: str | None = None
    value: Value = Value.UNKNOWN
    since: datetime | None = None             # when the current value started
    active_start: datetime | None = None      # start of the latest active period
    active_end: datetime | None = None        # end of the latest active period
    last_transition: datetime | None = None   # latest change between known values
    lost_while_active: bool = False           # became unknown while reporting presence

    @property
    def entity_id(self) -> str:
        return self.assignment.entity_id

    @property
    def role(self) -> SensorRole:
        return self.assignment.role

    @property
    def kind(self) -> SensorKind:
        return self.assignment.kind

    @property
    def is_door(self) -> bool:
        return self.kind == SensorKind.DOOR

    @property
    def is_presence(self) -> bool:
        return not self.is_door and self.role in PRESENCE_ROLES

    @property
    def is_trigger(self) -> bool:
        return not self.is_door and self.role == SensorRole.TRIGGER

    @property
    def used(self) -> bool:
        """Takes part in occupancy (role is not Ignore and the signal is not excluded in the Presence tab)."""
        return self.assignment.participates

    @property
    def capable(self) -> bool:
        """Can detect that someone is inside (doors cannot)."""
        return self.used and not self.is_door

    def update(self, value: Value, at: datetime) -> bool:
        """Apply a new value observed at ``at``. Returns True if it changed.

        Out-of-order (older) observations are ignored, so a late event can
        never move the signal back in time.
        """
        if self.since is not None and at < self.since:
            return False
        if value == self.value and self.since is not None:
            return False
        previous = self.value
        if previous == Value.UNKNOWN and self.lost_while_active and value == Value.INACTIVE:
            # Reconnected as clear after dropping out while reporting presence:
            # conservatively, the presence lasted until the reconnect.
            self.active_end = at
        self.lost_while_active = value == Value.UNKNOWN and (
            previous == Value.ACTIVE or (previous == Value.UNKNOWN and self.lost_while_active)
        )
        if value == Value.ACTIVE:
            self.active_start = at
            self.active_end = None
        elif previous == Value.ACTIVE:
            self.active_end = at
        if previous != Value.UNKNOWN and value != Value.UNKNOWN and self.since is not None:
            self.last_transition = at
        self.value = value
        self.since = at
        return True

    def label(self) -> str:
        return self.name or self.entity_id


@dataclass(slots=True)
class SignalView:
    """Evaluated contribution of one signal at a point in time."""

    track: SignalTrack
    status: SignalStatus

    def as_dict(self) -> dict[str, Any]:
        t = self.track
        return {
            "entity_id": t.entity_id,
            "name": t.name,
            "kind": t.kind.value,
            "role": t.role.value,
            "inverted": t.assignment.inverted,
            "enabled": t.assignment.enabled,
            "value": t.value.value,
            "status": self.status.value,
            "since": t.since.isoformat(timespec="seconds") if t.since else None,
        }


@dataclass(slots=True, frozen=True)
class Confidence:
    """Transparent evidence-quality score. Not a predicted probability."""

    score: int
    evidence: float
    coverage: float
    basis: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_strength": round(self.evidence, 2),
            "sensor_coverage": round(self.coverage, 2),
            "basis": self.basis,
            "formula": "100 × evidence_strength × (0.5 + 0.5 × sensor_coverage)",
        }


@dataclass(slots=True, frozen=True)
class RoomResult:
    """Outcome of one evaluation."""

    state: OccupancyState
    occupied: bool | None
    reason: str
    reason_code: str
    confidence: Confidence
    signals: tuple[SignalView, ...]
    usable: bool
    last_activity: datetime | None
    last_activity_source: str | None
    quiet_since: datetime | None
    vacancy_at: datetime | None
    required_quiet: int | None
    vacancy_evidence: tuple[str, ...]
    starting_up: bool
    state_since: datetime | None = field(compare=False, default=None)

    def names(self, *statuses: SignalStatus) -> list[str]:
        return [v.track.label() for v in self.signals if v.status in statuses]

    def views(self, *statuses: SignalStatus) -> list[SignalView]:
        return [v for v in self.signals if v.status in statuses]


class RoomEngine:
    """Occupancy state machine of one room."""

    def __init__(
        self,
        area_id: str,
        sensors: tuple[SensorAssignment, ...],
        timing: OccupancyTiming,
        *,
        adjacent: tuple[str, ...] = (),
    ) -> None:
        self.area_id = area_id
        self.timing = timing
        self.adjacent = adjacent
        self.tracks: dict[str, SignalTrack] = {s.entity_id: SignalTrack(s) for s in sensors}
        self.state = OccupancyState.UNCERTAIN
        self.occupied: bool | None = None
        self.state_since: datetime | None = None
        self.visit_confirmed = False
        self.last_activity: datetime | None = None
        self.last_activity_source: str | None = None
        self._activity_floor: datetime | None = None
        self._startup_until: datetime | None = None
        self._last_new_activity: datetime | None = None  # latest *start* inside the room
        self._neighbour_motion: dict[str, datetime] = {}
        self.result: RoomResult | None = None
        # Signals that start possible occupancy lighting: they cannot confirm occupancy on their own (0.3.11).
        self.preliminary: frozenset[str] = frozenset()
        # Occupied zones that keep this (occupied) room occupied (0.6.0): their names.
        self.external_hold: frozenset[str] = frozenset()

    def set_hold(self, names: frozenset[str], now: datetime) -> bool:
        """Zones holding the room occupied. Returns True if it changed. The quiet period restarts when one ends."""
        if names == self.external_hold:
            return False
        if self.external_hold - names and self.occupied:
            self._activity_floor = _later(self._activity_floor, now)
        self.external_hold = names
        return True

    def reconfigure(self, sensors: tuple[SensorAssignment, ...], timing: OccupancyTiming) -> None:
        """Apply changed signal assignments (enabled, role) and timing without losing what is known.

        Every signal keeps its value and history, so the room is evaluated again
        at once with the new settings: no restart hold, no new quiet period.
        """
        self.timing = timing
        tracks: dict[str, SignalTrack] = {}
        for s in sensors:
            track = self.tracks.get(s.entity_id) or SignalTrack(s)
            track.assignment = s
            tracks[s.entity_id] = track
        self.tracks = tracks

    # ------------------------------------------------------------ inputs
    def observe(self, entity_id: str, value: Value, at: datetime, name: str | None = None) -> bool:
        """Record a signal value. Returns True if the signal changed.

        Contract: call ``evaluate`` after every observation and again at
        ``next_deadline``. The state machine advances one evaluation at a
        time (e.g. a visit counts as confirmed once an evaluation saw it).
        """
        track = self.tracks.get(entity_id)
        if track is None:
            return False
        if name:
            track.name = name
        previous_start = track.active_start
        previous_transition = track.last_transition
        if not track.update(value, at):
            return False
        if not track.used:
            return True
        if track.is_door:
            if track.last_transition != previous_transition and track.last_transition is not None:
                self._note_activity(at, track)
        elif track.active_start != previous_start and track.active_start is not None:
            self._note_activity(at, track)
            self._last_new_activity = _later(self._last_new_activity, at)
        return True

    def note_neighbour_motion(self, area_id: str, at: datetime) -> None:
        """Movement detected in an adjacent room (for door-exit sequences)."""
        if area_id in self.adjacent:
            previous = self._neighbour_motion.get(area_id)
            if previous is None or at > previous:
                self._neighbour_motion[area_id] = at

    def note_activity_evidence(self, at: datetime, source: str) -> bool:
        """Activity that is not a sensor signal (a wall switch press, 0.3.5).

        Only refreshes an occupied room: its quiet period starts again from
        ``at``, so it is vacated one inactivity timeout (or exit confirmation)
        later. It never makes a vacant or uncertain room occupied and never
        holds a room occupied by itself. Returns True if anything changed.
        """
        if not self.occupied or self.state == OccupancyState.UNCERTAIN:
            return False
        self._activity_floor = _later(self._activity_floor, at)
        if self.last_activity is None or at >= self.last_activity:
            self.last_activity = at
            self.last_activity_source = source
        return True

    def restore(self, data: dict[str, Any], now: datetime) -> None:
        """Conservative start: a room that was occupied stays occupied.

        The previous decision is held for ``startup_hold`` and the quiet
        period starts again from now, because nothing is known about the
        time Home Assistant was not running.
        """
        try:
            state = OccupancyState(data.get("state"))
        except ValueError:
            state = OccupancyState.UNCERTAIN
        occupied = data.get("occupied")
        self.occupied = occupied if isinstance(occupied, bool) else None
        self.state = state
        self.visit_confirmed = bool(data.get("visit_confirmed", False))
        if self.occupied:
            self._activity_floor = now
        if (last := data.get("last_activity")) and isinstance(last, str):
            try:
                self.last_activity = datetime.fromisoformat(last)
            except ValueError:
                self.last_activity = None
        self.last_activity_source = data.get("last_activity_source")
        self.start(now)

    def start(self, now: datetime) -> None:
        self._startup_until = now + timedelta(seconds=self.timing.startup_hold)

    def persist(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "occupied": self.occupied,
            "visit_confirmed": self.visit_confirmed,
            "last_activity": self.last_activity.isoformat() if self.last_activity else None,
            "last_activity_source": self.last_activity_source,
        }

    # ------------------------------------------------- state transfer (A3.2)
    def export_state(self) -> dict[str, Any]:
        """Everything this engine knows, exactly (for a copy on another engine with the same configuration).

        Unlike ``persist`` (what survives a restart), this includes every
        signal's history, the timers' bases and the adjacent-room evidence, so
        a copy evaluates exactly like the original from then on. Times keep
        their microseconds.
        """
        return {
            "schema": STATE_SCHEMA,
            "area_id": self.area_id,
            "state": self.state.value,
            "occupied": self.occupied,
            "state_since": _iso_full(self.state_since),
            "visit_confirmed": self.visit_confirmed,
            "last_activity": _iso_full(self.last_activity),
            "last_activity_source": self.last_activity_source,
            "activity_floor": _iso_full(self._activity_floor),
            "startup_until": _iso_full(self._startup_until),
            "last_new_activity": _iso_full(self._last_new_activity),
            "neighbour_motion": {a: _iso_full(at) for a, at in sorted(self._neighbour_motion.items())},
            "preliminary": sorted(self.preliminary),
            "external_hold": sorted(self.external_hold),
            "tracks": {
                entity_id: {
                    "name": t.name,
                    "value": t.value.value,
                    "since": _iso_full(t.since),
                    "active_start": _iso_full(t.active_start),
                    "active_end": _iso_full(t.active_end),
                    "last_transition": _iso_full(t.last_transition),
                    "lost_while_active": t.lost_while_active,
                }
                for entity_id, t in self.tracks.items()
            },
        }

    def import_state(self, data: Any) -> None:
        """Become an exact copy of an exported engine state. Raises ``StateError`` and changes nothing if invalid.

        The signal assignments and timing stay this engine's own (both sides
        must run the same configuration revision); the signals must be the
        same ones.
        """
        parsed = _parse_state(data, self)
        (self.state, self.occupied, self.state_since, self.visit_confirmed, self.last_activity,
         self.last_activity_source, self._activity_floor, self._startup_until, self._last_new_activity,
         self._neighbour_motion, self.preliminary, self.external_hold, tracks) = parsed
        for entity_id, values in tracks.items():
            track = self.tracks[entity_id]
            (track.name, track.value, track.since, track.active_start, track.active_end, track.last_transition,
             track.lost_while_active) = values
        self.result = None

    def decision(self) -> dict[str, Any]:
        """The decision part of the state (what another engine may adopt)."""
        return {
            "state": self.state.value,
            "occupied": self.occupied,
            "visit_confirmed": self.visit_confirmed,
            "state_since": _iso_full(self.state_since),
            "last_activity": _iso_full(self.last_activity),
            "last_activity_source": self.last_activity_source,
        }

    def adopt(self, decision: Any, now: datetime) -> bool:
        """Take over another engine's decision when ownership changes. Returns True if this decision changed.

        Conservative: when the decisions already agree (state and occupied),
        nothing changes, so a pending vacancy keeps its own deadline. Otherwise
        this engine takes the other decision; a room the other engine had
        occupied stays occupied and its quiet period starts again at ``now``,
        so an ownership change can never cause vacancy by itself. The signal
        values are kept: they are this engine's own observations, and the next
        evaluation applies them (current activity keeps a room occupied).
        Raises ``StateError`` for an invalid decision.
        """
        if not isinstance(decision, dict):
            raise StateError("decision must be an object")
        state = _enum(OccupancyState, decision.get("state"), "state")
        occupied = decision.get("occupied")
        if occupied is not None and not isinstance(occupied, bool):
            raise StateError("occupied must be true, false or null")
        visit_confirmed = decision.get("visit_confirmed", False)
        if not isinstance(visit_confirmed, bool):
            raise StateError("visit_confirmed must be a boolean")
        state_since = _time(decision.get("state_since"), "state_since")
        last_activity = _time(decision.get("last_activity"), "last_activity")
        source = decision.get("last_activity_source")
        if source is not None and (not isinstance(source, str) or len(source) > MAX_REASON_LENGTH):
            raise StateError("last_activity_source must be a short string")
        if (state, occupied) == (self.state, self.occupied):
            return False
        if occupied:
            self._activity_floor = _later(self._activity_floor, now)
        elif occupied is False:
            self._activity_floor = None
        self.state = state
        self.occupied = occupied
        self.visit_confirmed = visit_confirmed and bool(occupied)
        self.state_since = state_since or now
        if last_activity is not None and (self.last_activity is None or last_activity > self.last_activity):
            self.last_activity, self.last_activity_source = last_activity, source
        return True

    def _note_activity(self, at: datetime, track: SignalTrack) -> None:
        if self.last_activity is None or at >= self.last_activity:
            self.last_activity = at
            self.last_activity_source = track.entity_id

    # -------------------------------------------------------- evaluation
    def _status(self, t: SignalTrack, now: datetime) -> SignalStatus:
        timing = self.timing
        if not t.used:
            return SignalStatus.IGNORED
        if t.value == Value.UNKNOWN:
            return SignalStatus.UNKNOWN
        if t.is_door:
            if t.last_transition and now - t.last_transition < timedelta(seconds=timing.correlation_window):
                return SignalStatus.RECENT
            return SignalStatus.IDLE
        if t.value == Value.ACTIVE:
            limit = timing.trigger_stuck_after if t.is_trigger else timing.presence_stuck_after
            if limit and t.since and now - t.since >= timedelta(seconds=limit):
                return SignalStatus.STUCK
            return SignalStatus.ACTIVE
        if (
            t.is_presence
            and t.active_end is not None
            and now - t.active_end < timedelta(seconds=timing.dropout_grace)
        ):
            return SignalStatus.GRACE
        return SignalStatus.CLEARED

    def _evidence_end(self, t: SignalTrack, status: SignalStatus) -> datetime | None:
        """When this signal last provided evidence (for the quiet period)."""
        if t.is_door:
            return t.last_transition
        if status == SignalStatus.UNKNOWN and t.lost_while_active and t.active_end is not None:
            # Unavailable while reporting presence: someone may still be there.
            # Hold for one extra inactivity timeout before the quiet period starts.
            return t.active_end + timedelta(seconds=self.timing.inactivity_timeout)
        if status == SignalStatus.STUCK and t.since is not None:
            limit = self.timing.trigger_stuck_after if t.is_trigger else self.timing.presence_stuck_after
            return t.since + timedelta(seconds=limit)
        return t.active_end

    def evaluate(self, now: datetime) -> RoomResult:
        timing = self.timing
        window = timedelta(seconds=timing.correlation_window)
        views = tuple(SignalView(t, self._status(t, now)) for t in self.tracks.values())
        capable = [v for v in views if v.track.capable]
        known_capable = [v for v in capable if v.status != SignalStatus.UNKNOWN]
        presence = [v for v in views if v.track.is_presence]
        presence_unknown = [v for v in presence if v.status == SignalStatus.UNKNOWN]
        holding = [v for v in views if v.track.capable and v.status in (SignalStatus.ACTIVE, SignalStatus.GRACE)]
        doors_recent = [v for v in views if v.status == SignalStatus.RECENT]
        starting_up = self._startup_until is not None and now < self._startup_until
        prev_state, prev_occupied = self.state, self.occupied

        quiet_since: datetime | None = None
        vacancy_at: datetime | None = None
        required: int | None = None
        vacancy_evidence: list[str] = []

        if not capable or not known_capable:
            state = OccupancyState.UNCERTAIN
            occupied = self.occupied
            if not capable:
                code = "no_signals"
                reason = (
                    "No occupancy signal is configured (door contacts cannot detect presence); "
                    "assign a Trigger, Maintain or Confirm vacancy sensor."
                )
            else:
                code = "all_signals_unknown"
                reason = (
                    f"All occupancy sensors are unavailable ({', '.join(v.track.label() for v in capable)}); "
                    f"keeping the last state ({_occupied_word(occupied)}). An outage never causes vacancy."
                )
        elif holding or (self.external_hold and prev_occupied):
            occupied = True
            corroborated = bool(holding) and self._corroborated(views, now, window)
            if not holding:
                # Only zones keep it occupied: the quiet period starts when the last of them ends.
                self._activity_floor = _later(self._activity_floor, now)
            what = [*(v.track.label() for v in holding), *(f"zone {z}" for z in sorted(self.external_hold))]
            if corroborated or (self.visit_confirmed and prev_occupied):
                state = OccupancyState.CONFIRMED_OCCUPANCY
                self.visit_confirmed = True
                code = "occupied" if holding else "zone_hold"
                reason = f"Occupied: {_join(what)} active."
            else:
                state = OccupancyState.POSSIBLE_OCCUPANCY
                code = "possible_occupancy"
                reason = (
                    f"Possible occupancy: {_join(what)}; "
                    "waiting for confirmation by a second signal or sustained presence."
                )
        else:
            if prev_state == OccupancyState.UNCERTAIN and prev_occupied:
                # Recovering from a full outage: nothing is known about the
                # outage period, so the quiet period starts now.
                self._activity_floor = now
            ends = [self._evidence_end(v.track, v.status) for v in views if v.track.used]
            quiet_since = _later(*ends, self._activity_floor)
            if quiet_since is not None and quiet_since > now:
                quiet_since_future = quiet_since
            else:
                quiet_since_future = None
            if not prev_occupied:
                occupied = False
                if doors_recent:
                    state = OccupancyState.POSSIBLE_OCCUPANCY
                    code = "door_only"
                    reason = (
                        f"Door movement ({_join(v.track.label() for v in doors_recent)}) but no presence "
                        "detected; a door alone does not prove that someone is inside."
                    )
                else:
                    state = OccupancyState.VACANT
                    code = "vacant"
                    reason = "Vacant: no occupancy signal active."
                    self.visit_confirmed = False
            else:
                fast, vacancy_evidence = self._fast_exit(views, presence, presence_unknown)
                required = timing.exit_confirmation if fast else timing.inactivity_timeout
                if quiet_since is None:
                    quiet_since = now
                vacancy_at = quiet_since + timedelta(seconds=required)
                if quiet_since_future is not None:
                    quiet_since = None  # still held by a presence sensor that went unavailable
                if starting_up and self._startup_until is not None:
                    vacancy_at = max(vacancy_at, self._startup_until)
                if now >= vacancy_at:
                    state = OccupancyState.VACANT
                    occupied = False
                    self.visit_confirmed = False
                    vacancy_at = None
                    code = "vacated"
                    reason = f"Vacant: no activity for {_duration(required)}"
                    reason += f" ({_join(vacancy_evidence)})." if vacancy_evidence else "."
                else:
                    state = OccupancyState.POSSIBLE_VACANCY
                    occupied = True
                    code = "confirming_vacancy"
                    reason = f"No presence signal active; vacancy is confirmed after {_duration(required)} without activity"
                    if vacancy_evidence:
                        reason += f" ({_join(vacancy_evidence)})"
                    reason += "."
                    if starting_up:
                        reason += " Holding the previous state while Home Assistant starts."
                    if held := [v for v in presence_unknown if v.track.lost_while_active]:
                        reason += (
                            f" {_join(v.track.label() for v in held)} became unavailable while reporting "
                            "presence: occupancy is held longer."
                        )
                    elif presence_unknown:
                        reason += (
                            f" {_join(v.track.label() for v in presence_unknown)} unavailable: "
                            "not used as vacancy evidence."
                        )

        if state != prev_state or self.state_since is None:
            self.state_since = now
        self.state = state
        self.occupied = occupied
        if occupied is False:
            self._activity_floor = None

        stuck = [v for v in views if v.status == SignalStatus.STUCK]
        if stuck and state != OccupancyState.UNCERTAIN:
            reason += f" Ignored as stuck: {_join(v.track.label() for v in stuck)}."
        reason = reason if len(reason) <= MAX_REASON_LENGTH else reason[: MAX_REASON_LENGTH - 1] + "…"

        self.result = RoomResult(
            state=state,
            occupied=occupied,
            reason=reason,
            reason_code=code,
            confidence=self._confidence(state, views, capable, holding, presence, vacancy_evidence),
            signals=views,
            usable=bool(known_capable),
            last_activity=self.last_activity,
            last_activity_source=self.last_activity_source,
            quiet_since=quiet_since,
            vacancy_at=vacancy_at,
            required_quiet=required,
            vacancy_evidence=tuple(vacancy_evidence),
            starting_up=starting_up,
            state_since=self.state_since,
        )
        return self.result

    def _corroborated(self, views: tuple[SignalView, ...], now: datetime, window: timedelta) -> bool:
        """Is the current evidence strong enough to confirm occupancy?"""
        recent: set[str] = set()
        for v in views:
            t = v.track
            if v.status == SignalStatus.ACTIVE and t.since is not None:
                if now - t.since >= window and t.entity_id not in self.preliminary:
                    return True  # sustained presence or sustained motion
                recent.add(t.entity_id)
            elif v.status == SignalStatus.GRACE:
                recent.add(t.entity_id)
            elif t.capable and t.active_start is not None and now - t.active_start < window:
                recent.add(t.entity_id)
            elif v.status == SignalStatus.RECENT:
                recent.add(t.entity_id)
        non_door = {e for e in recent if not self.tracks[e].is_door}
        return len(recent) >= 2 and bool(non_door) and bool(recent - self.preliminary)

    def _fast_exit(
        self,
        views: tuple[SignalView, ...],
        presence: list[SignalView],
        presence_unknown: list[SignalView],
    ) -> tuple[bool, list[str]]:
        """Is there positive evidence of vacancy? Returns (fast, evidence)."""
        if presence_unknown:
            return False, []  # an unavailable presence sensor is never "off"
        evidence: list[str] = []
        confirmers = [v for v in presence if v.track.role == SensorRole.CONFIRM_VACANCY]
        if confirmers and all(v.status == SignalStatus.CLEARED for v in confirmers):
            evidence.append(f"{_join(v.track.label() for v in confirmers)} cleared")
        doors = [v.track for v in views if v.track.is_door and v.track.used and v.track.last_transition]
        if doors:
            door = max(doors, key=lambda t: t.last_transition)  # type: ignore[arg-type, return-value]
            moved = door.last_transition
            if moved is not None and (self._last_new_activity is None or moved >= self._last_new_activity):
                window = timedelta(seconds=self.timing.correlation_window)
                towards = [
                    a for a, at in self._neighbour_motion.items()
                    if moved <= at <= moved + window
                ]
                if towards:
                    evidence.append(f"door exit via {door.label()} with movement in an adjacent room")
                else:
                    evidence.append(f"door movement via {door.label()} with no activity since")
        if not self.visit_confirmed:
            evidence.append("occupancy was never confirmed")
        return bool(evidence), evidence

    def _confidence(self, state, views, capable, holding, presence, vacancy_evidence) -> Confidence:
        usable = [v for v in capable if v.status not in (SignalStatus.UNKNOWN, SignalStatus.STUCK)]
        coverage = len(usable) / len(capable) if capable else 0.0
        active_presence = [v for v in holding if v.track.is_presence and v.status == SignalStatus.ACTIVE]
        if state == OccupancyState.UNCERTAIN:
            evidence, basis = 0.0, "no usable occupancy sensor"
        elif state == OccupancyState.CONFIRMED_OCCUPANCY:
            evidence = 0.6 + (0.2 if active_presence else 0.0) + (0.2 if len(holding) >= 2 else 0.0)
            basis = f"{len(holding)} active signal(s), presence sensor active: {bool(active_presence)}"
        elif state == OccupancyState.POSSIBLE_OCCUPANCY:
            evidence = 0.4 if holding else 0.25
            basis = "uncorroborated activity" if holding else "door movement only"
        elif state == OccupancyState.POSSIBLE_VACANCY:
            evidence = 0.5 if vacancy_evidence else 0.4
            basis = "no current activity; " + ("vacancy evidence present" if vacancy_evidence else "waiting for timeout")
        else:  # vacant
            known_clear = presence and all(v.status == SignalStatus.CLEARED for v in presence)
            evidence = 1.0 if known_clear else 0.8
            basis = "all presence sensors report clear" if known_clear else "no activity (timeout only)"
        evidence = min(evidence, 1.0)
        score = round(100 * evidence * (0.5 + 0.5 * coverage))
        return Confidence(score=score, evidence=evidence, coverage=coverage, basis=basis)

    def next_deadline(self, now: datetime) -> datetime | None:
        """Earliest future time at which an evaluation could change by itself."""
        timing = self.timing
        candidates: list[datetime] = []
        add = candidates.append
        for t in self.tracks.values():
            if not t.used or t.since is None:
                continue
            if t.is_door:
                if t.last_transition:
                    add(t.last_transition + timedelta(seconds=timing.correlation_window))
                continue
            if t.value == Value.ACTIVE:
                add(t.since + timedelta(seconds=timing.correlation_window))
                limit = timing.trigger_stuck_after if t.is_trigger else timing.presence_stuck_after
                if limit:
                    add(t.since + timedelta(seconds=limit))
            elif t.is_presence and t.active_end is not None:
                add(t.active_end + timedelta(seconds=timing.dropout_grace))
            if t.active_start is not None:
                add(t.active_start + timedelta(seconds=timing.correlation_window))
        if self.result and self.result.vacancy_at:
            add(self.result.vacancy_at)
        if self._startup_until:
            add(self._startup_until)
        future = [c for c in candidates if c > now]
        return min(future) if future else None


# ------------------------------------------------------- state transfer helpers
STATE_SCHEMA = 1
MAX_NAME_LENGTH = 255


class StateError(ValueError):
    """An exported engine state or decision is not valid for this engine."""


def _iso_full(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _time(value: Any, key: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise StateError(f"{key} must be an ISO time")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise StateError(f"{key} must be an ISO time") from err
    if parsed.tzinfo is None:
        raise StateError(f"{key} must include a time zone")
    return parsed


def _enum(enum_cls: type, value: Any, key: str) -> Any:
    try:
        return enum_cls(value)
    except ValueError as err:
        raise StateError(f"{key} is not valid") from err


def _names(value: Any, key: str, limit: int = 100) -> frozenset[str]:
    if not isinstance(value, list) or len(value) > limit or not all(
        isinstance(v, str) and 0 < len(v) <= MAX_NAME_LENGTH for v in value
    ):
        raise StateError(f"{key} must be a list of names")
    return frozenset(value)


def _parse_state(data: Any, engine: RoomEngine) -> tuple:
    if not isinstance(data, dict):
        raise StateError("state must be an object")
    if data.get("schema") != STATE_SCHEMA:
        raise StateError(f"unsupported state schema {data.get('schema')!r}")
    if data.get("area_id") != engine.area_id:
        raise StateError("state belongs to another room")
    occupied = data.get("occupied")
    if occupied is not None and not isinstance(occupied, bool):
        raise StateError("occupied must be true, false or null")
    visit = data.get("visit_confirmed")
    if not isinstance(visit, bool):
        raise StateError("visit_confirmed must be a boolean")
    source = data.get("last_activity_source")
    if source is not None and (not isinstance(source, str) or len(source) > MAX_NAME_LENGTH):
        raise StateError("last_activity_source must be a short string")
    neighbours = data.get("neighbour_motion")
    if not isinstance(neighbours, dict) or len(neighbours) > 100:
        raise StateError("neighbour_motion must be an object")
    motion = {}
    for area, at in neighbours.items():
        if not isinstance(area, str) or area not in engine.adjacent:
            raise StateError("neighbour_motion names a room that is not adjacent")
        if (parsed := _time(at, "neighbour_motion")) is not None:
            motion[area] = parsed
    raw_tracks = data.get("tracks")
    if not isinstance(raw_tracks, dict) or set(raw_tracks) != set(engine.tracks):
        raise StateError("the signals differ from this engine's signals")
    tracks = {}
    for entity_id, raw in raw_tracks.items():
        if not isinstance(raw, dict):
            raise StateError("track must be an object")
        name = raw.get("name")
        if name is not None and (not isinstance(name, str) or len(name) > MAX_NAME_LENGTH):
            raise StateError("track name must be a short string")
        lost = raw.get("lost_while_active")
        if not isinstance(lost, bool):
            raise StateError("lost_while_active must be a boolean")
        tracks[entity_id] = (
            name, _enum(Value, raw.get("value"), "value"), _time(raw.get("since"), "since"),
            _time(raw.get("active_start"), "active_start"), _time(raw.get("active_end"), "active_end"),
            _time(raw.get("last_transition"), "last_transition"), lost,
        )
    return (
        _enum(OccupancyState, data.get("state"), "state"), occupied, _time(data.get("state_since"), "state_since"),
        visit, _time(data.get("last_activity"), "last_activity"), source,
        _time(data.get("activity_floor"), "activity_floor"), _time(data.get("startup_until"), "startup_until"),
        _time(data.get("last_new_activity"), "last_new_activity"), motion,
        _names(data.get("preliminary"), "preliminary"), _names(data.get("external_hold"), "external_hold"), tracks,
    )


def _join(items) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _duration(seconds: int) -> str:
    if seconds % 60 == 0 and seconds >= 60:
        minutes = seconds // 60
        return f"{minutes} min"
    return f"{seconds} s"


def _occupied_word(occupied: bool | None) -> str:
    return {True: "occupied", False: "vacant", None: "unknown"}[occupied]
