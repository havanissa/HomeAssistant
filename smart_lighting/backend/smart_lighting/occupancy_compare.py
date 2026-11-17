"""Shadow comparison (Phase A3.2): the App's occupancy engine against the integration's, room by room.

Pure logic with bounded memory. Both engines evaluate the same events; the
integration streams its engine's results over the bridge, the App computes its
own. Per room, the compared key is (state, occupied, reason code).

* A *discrepancy episode* opens when the keys differ and closes when they agree
  again. Closed within :data:`TOLERANCE_SECONDS` it is a **timing difference**
  (the two sides received the event or fired their timer a moment apart);
  longer, it is a **disagreement**. An open episode older than the tolerance
  counts as an open disagreement.
* Each change of the integration's (state, occupied) is a **transition**; it is
  *matched* when the App agrees with it within the tolerance.
* While both wait for vacancy, the vacancy deadlines are compared; a difference
  larger than :data:`DEADLINE_TOLERANCE_SECONDS` is a **deadline difference**.

Every record keeps the room's input events around it (what each engine saw).
Nothing here decides anything: readiness for a handover is computed from these
statistics by the runtime.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

TOLERANCE_SECONDS = 3.0
DEADLINE_TOLERANCE_SECONDS = 2.0
HISTORY_SIZE = 200
EVENTS_PER_ROOM = 30
EVENTS_PER_RECORD = 20
PERSIST_SCHEMA = 1


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="milliseconds") if value else None


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def key_of(wire: dict[str, Any] | None) -> tuple[Any, Any, Any] | None:
    if not wire:
        return None
    return wire.get("state"), wire.get("occupied"), wire.get("reason_code")


def brief(wire: dict[str, Any] | None) -> dict[str, Any] | None:
    if not wire:
        return None
    return {k: wire.get(k) for k in ("state", "occupied", "reason_code", "reason", "vacancy_at", "state_since")}


@dataclass(slots=True)
class RoomComparison:
    area_id: str
    app: dict[str, Any] | None = None
    integration: dict[str, Any] | None = None
    app_at: datetime | None = None
    integration_at: datetime | None = None
    diverged_at: datetime | None = None
    episode: dict[str, Any] | None = None
    pending_transitions: int = 0
    transitions: int = 0
    matched: int = 0
    timing_differences: int = 0
    timing_max_ms: float = 0.0
    timing_total_ms: float = 0.0
    disagreements: int = 0
    deadline_differences: int = 0
    deadline_max_s: float = 0.0
    last_disagreement_at: datetime | None = None
    _deadline_pair: tuple[Any, Any] | None = None
    events: deque = field(default_factory=lambda: deque(maxlen=EVENTS_PER_ROOM))

    @property
    def agree(self) -> bool | None:
        if self.app is None or self.integration is None:
            return None
        return key_of(self.app) == key_of(self.integration)

    def counters(self) -> dict[str, Any]:
        return {
            "transitions": self.transitions, "matched": self.matched,
            "timing_differences": self.timing_differences, "timing_max_ms": round(self.timing_max_ms),
            "timing_avg_ms": round(self.timing_total_ms / self.timing_differences) if self.timing_differences else 0,
            "disagreements": self.disagreements, "deadline_differences": self.deadline_differences,
            "deadline_max_s": round(self.deadline_max_s, 1),
        }


class Comparator:
    """Agreement statistics of the two engines, with a bounded history of discrepancies."""

    def __init__(self) -> None:
        self.rooms: dict[str, RoomComparison] = {}
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY_SIZE)
        self.since: datetime | None = None  # continuous comparison since (a new bridge session restarts it)
        self.sessions = 0
        self.dirty = False
        # Survives new sessions and App restarts: a handover needs a clean period after the last one.
        self.last_disagreement_at: datetime | None = None

    # --------------------------------------------------------------- input
    def start(self, now: datetime, areas: list[str]) -> None:
        """A new reference start (bridge session): both engines are equal now; counting starts again."""
        self.since = now
        self.sessions += 1
        old = self.rooms
        self.rooms = {a: RoomComparison(a, events=old[a].events if a in old else deque(maxlen=EVENTS_PER_ROOM))
                      for a in areas}
        self.dirty = True

    def stop(self) -> None:
        """The comparison is interrupted (no bridge): open episodes end without a verdict."""
        self.since = None
        for room in self.rooms.values():
            room.diverged_at, room.episode, room.pending_transitions = None, None, 0

    def note_event(self, area_id: str, at: datetime, kind: str, **data: Any) -> None:
        room = self.rooms.get(area_id)
        if room is None:
            return
        item = {"at": _iso(at), "kind": kind, **data}
        room.events.append(item)
        if room.episode is not None and len(room.episode["events"]) < EVENTS_PER_RECORD:
            room.episode["events"].append(item)

    def update(self, area_id: str, side: str, wire: dict[str, Any], at: datetime) -> None:
        """A new result of one side ("app" or "integration")."""
        room = self.rooms.get(area_id)
        if room is None or self.since is None:
            return
        if side == "integration":
            before = room.integration
            room.integration, room.integration_at = wire, at
            if before is not None and (before.get("state"), before.get("occupied")) != (
                    wire.get("state"), wire.get("occupied")):
                room.transitions += 1
                room.pending_transitions += 1
                self.dirty = True
        else:
            room.app, room.app_at = wire, at
        self._settle(room, at)

    def _settle(self, room: RoomComparison, at: datetime) -> None:
        agree = room.agree
        if agree is None:
            return
        if not agree:
            if room.diverged_at is None:
                room.diverged_at = at
                room.episode = {"area_id": room.area_id, "opened_at": _iso(at), "app": brief(room.app),
                                "integration": brief(room.integration), "events": list(room.events)[-10:]}
            else:  # still different: the pair that differed first stays, the latest is kept beside it
                room.episode["app_last"], room.episode["integration_last"] = brief(room.app), brief(room.integration)
            return
        if room.diverged_at is not None:
            duration = (at - room.diverged_at).total_seconds()
            episode = room.episode or {}
            episode.update(closed_at=_iso(at), duration_s=round(duration, 3))
            if duration <= TOLERANCE_SECONDS:
                room.timing_differences += 1
                room.timing_max_ms = max(room.timing_max_ms, duration * 1000)
                room.timing_total_ms += duration * 1000
                room.matched += room.pending_transitions
                if duration >= 1.0:
                    self.history.append({"kind": "timing", **episode})
            else:
                room.disagreements += 1
                room.last_disagreement_at = at
                self.last_disagreement_at = at
                self.history.append({"kind": "disagreement", **episode})
            room.diverged_at, room.episode = None, None
        else:
            room.matched += room.pending_transitions
        room.pending_transitions = 0
        self.dirty = True
        self._compare_deadlines(room, at)

    def _compare_deadlines(self, room: RoomComparison, at: datetime) -> None:
        a, i = room.app or {}, room.integration or {}
        if a.get("state") != "possible_vacancy" or not a.get("vacancy_at") or not i.get("vacancy_at"):
            return
        pair = (a.get("vacancy_at"), i.get("vacancy_at"))
        if pair == room._deadline_pair:
            return
        room._deadline_pair = pair
        ta, ti = _parse(pair[0]), _parse(pair[1])
        if ta is None or ti is None:
            return
        delta = abs((ta - ti).total_seconds())
        if delta > DEADLINE_TOLERANCE_SECONDS:
            room.deadline_differences += 1
            room.deadline_max_s = max(room.deadline_max_s, delta)
            self.history.append({"kind": "deadline", "area_id": room.area_id, "opened_at": _iso(at),
                                 "delta_s": round(delta, 3), "app": brief(a), "integration": brief(i),
                                 "events": list(room.events)[-10:]})

    # ------------------------------------------------------------- reading
    def open_disagreements(self, now: datetime) -> list[str]:
        return [a for a, r in self.rooms.items()
                if r.diverged_at is not None and (now - r.diverged_at).total_seconds() > TOLERANCE_SECONDS]

    def last_disagreement(self, now: datetime) -> datetime | None:
        times = [self.last_disagreement_at] if self.last_disagreement_at else []
        times += [r.diverged_at for a, r in self.rooms.items() if a in self.open_disagreements(now)]
        return max(times) if times else None

    def totals(self) -> dict[str, Any]:
        keys = ("transitions", "matched", "timing_differences", "disagreements", "deadline_differences")
        out: dict[str, Any] = {k: sum(getattr(r, k) for r in self.rooms.values()) for k in keys}
        out["timing_max_ms"] = round(max((r.timing_max_ms for r in self.rooms.values()), default=0))
        out["deadline_max_s"] = round(max((r.deadline_max_s for r in self.rooms.values()), default=0), 1)
        return out

    def summary(self, now: datetime) -> dict[str, Any]:
        rooms = []
        for area_id, r in self.rooms.items():
            open_for = (now - r.diverged_at).total_seconds() if r.diverged_at else None
            rooms.append({
                "area_id": area_id, "agree": r.agree,
                "status": ("unknown" if r.agree is None else "agree" if r.agree else
                           "disagree" if open_for is not None and open_for > TOLERANCE_SECONDS else "settling"),
                "open_for_s": round(open_for, 1) if open_for is not None else None,
                "app": brief(r.app), "integration": brief(r.integration),
                "app_at": _iso(r.app_at), "integration_at": _iso(r.integration_at),
                "last_disagreement_at": _iso(r.last_disagreement_at),
                **r.counters(),
            })
        return {
            "running": self.since is not None,
            "since": _iso(self.since),
            "seconds": round((now - self.since).total_seconds()) if self.since else 0,
            "sessions": self.sessions,
            "tolerance_s": TOLERANCE_SECONDS,
            "deadline_tolerance_s": DEADLINE_TOLERANCE_SECONDS,
            "totals": self.totals(),
            "open_disagreements": self.open_disagreements(now),
            "last_disagreement_at": _iso(self.last_disagreement(now)),
            "rooms": rooms,
        }

    def records(self, limit: int = HISTORY_SIZE) -> list[dict[str, Any]]:
        return list(reversed(self.history))[:limit]

    # --------------------------------------------------------- persistence
    def persist(self) -> dict[str, Any]:
        self.dirty = False
        return {
            "schema": PERSIST_SCHEMA,
            "history": list(self.history),
            "last_disagreement_at": _iso(self.last_disagreement_at),
            "rooms": {a: {"last_disagreement_at": _iso(r.last_disagreement_at), **r.counters()}
                      for a, r in self.rooms.items()},
        }

    def restore(self, data: Any) -> None:
        """Keep the discrepancy history across App restarts (the counting itself restarts with a session)."""
        if not isinstance(data, dict) or data.get("schema") != PERSIST_SCHEMA:
            return
        self.last_disagreement_at = _parse(data.get("last_disagreement_at"))
        history = data.get("history")
        if isinstance(history, list):
            self.history.extend(h for h in history[-HISTORY_SIZE:] if isinstance(h, dict))
        rooms = data.get("rooms")
        if isinstance(rooms, dict):
            for area_id, raw in rooms.items():
                if isinstance(area_id, str) and isinstance(raw, dict):
                    room = self.rooms.setdefault(area_id, RoomComparison(area_id))
                    room.last_disagreement_at = _parse(raw.get("last_disagreement_at"))
