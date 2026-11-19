"""Comparison of the App's shadow forecasts with the integration's published ones (Phase A5).

Both managers run the same model code on the same learned days and the same
live occupancy. Each side's *view* (every room's forecast) is recomputed on the
same events and on a five-minute tick. Whenever both sides have a fresh view
(recomputed within :data:`PAIR_SECONDS` of each other) the rooms are compared:
the same status, the same prediction and a probability within
:data:`TOLERANCE_POINTS` percentage points count as an *agreement*; anything
else is a *disagreement* and is kept with both values. Forecasts never command
anything, so disagreements are reported but do not block a handover by
themselves; the readiness checks ask for enough agreements and show them.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from typing import Any

PAIR_SECONDS = 20.0
TOLERANCE_POINTS = 2
HISTORY = 100


def _now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


class ForecastComparator:
    def __init__(self) -> None:
        self.running = False
        self.started_at: datetime | None = None
        self.agreements = 0
        self.disagreements = 0
        self.pairs = 0
        self.log: deque[dict[str, Any]] = deque(maxlen=HISTORY)
        self._latest: dict[str, tuple[datetime, dict[str, Any]]] = {}
        self._compared: dict[str, tuple[datetime, datetime] | None] = {"app": None}
        self.last_agreement_at: datetime | None = None
        self.last_disagreement_at: datetime | None = None
        self.rooms: dict[str, dict[str, Any]] = {}

    def start(self, now: datetime | None = None) -> None:
        self.running = True
        self.started_at = now or _now()
        self._latest.clear()

    def stop(self) -> None:
        self.running = False
        self._latest.clear()

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]

    def view(self, who: str, view: dict[str, Any], now: datetime | None = None) -> None:
        if not self.running:
            return
        now = now or _now()
        self._latest[who] = (now, view)
        other = self._latest.get("integration" if who == "app" else "app")
        if other is None or (now - other[0]).total_seconds() > PAIR_SECONDS:
            return
        self._compare(other[1] if who == "app" else view, view if who == "app" else other[1], now)
        self._latest.clear()

    def _compare(self, integration: dict[str, Any], app: dict[str, Any], now: datetime) -> None:
        self.pairs += 1
        rooms_i = integration.get("rooms") or {}
        rooms_a = app.get("rooms") or {}
        for area_id in sorted(set(rooms_i) | set(rooms_a)):
            a, b = rooms_i.get(area_id) or {}, rooms_a.get(area_id) or {}
            pa, pb = a.get("probability"), b.get("probability")
            same_status = a.get("status") == b.get("status")
            same_prediction = a.get("predicted") == b.get("predicted")
            close = pa == pb or (isinstance(pa, (int, float)) and isinstance(pb, (int, float))
                                 and abs(pa - pb) <= TOLERANCE_POINTS)
            record = {"at": iso(now), "area_id": area_id, "integration": {"status": a.get("status"), "probability": pa,
                                                                            "predicted": a.get("predicted"),
                                                                            "confidence": (a.get("confidence") or {}).get("level")},
                      "app": {"status": b.get("status"), "probability": pb, "predicted": b.get("predicted"),
                              "confidence": (b.get("confidence") or {}).get("level")}}
            self.rooms[area_id] = record
            if same_status and same_prediction and close:
                self.agreements += 1
                self.last_agreement_at = now
            else:
                self.disagreements += 1
                self.last_disagreement_at = now
                self.log.append(record)

    def report(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or _now()
        return {
            "running": self.running, "started_at": iso(self.started_at),
            "seconds": round((now - self.started_at).total_seconds()) if self.running and self.started_at else 0,
            "pair_seconds": PAIR_SECONDS, "tolerance_points": TOLERANCE_POINTS,
            "pairs": self.pairs, "agreements": self.agreements, "disagreements": self.disagreements,
            "last_agreement_at": iso(self.last_agreement_at), "last_disagreement_at": iso(self.last_disagreement_at),
            "rooms": dict(self.rooms), "log": list(reversed(self.log)),
        }
