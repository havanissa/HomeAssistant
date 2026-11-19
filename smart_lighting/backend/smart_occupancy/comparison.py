"""Comparison of the engine's decision with an existing occupancy helper.

Pure logic, aggregate counters only (bounded memory, no event history).
The helper itself is only read, never changed.

Episodes
--------
When the helper turns off while the engine still says occupied, an episode
starts. It ends in one of two ways:

* **Premature clear by the helper** - before the engine reaches vacancy,
  the helper turns on again or the engine sees new activity in the room.
  Someone was evidently still there, so the existing automation cleared too
  early.
* **Possible over-retention by the engine** - the engine reaches vacancy
  with no new activity since the helper turned off. The engine may have held
  occupancy longer than needed; the overhang is recorded.

The reverse case (helper on while the engine says vacant) is counted as
``helper_only_occupied``.

A disagreement counts only once it lasts longer than ``grace_seconds``
(the room's correlation window), so the normal reaction delay of the
existing automation is not reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(slots=True)
class ComparisonTracker:
    """Agreement statistics between engine and helper for one room."""

    since: datetime | None = None
    grace_seconds: float = 0.0
    disagreements: int = 0
    disagreement_seconds: float = 0.0
    disagreement_since: datetime | None = None
    premature_clears: int = 0
    over_retentions: int = 0
    over_retention_seconds: float = 0.0
    max_over_retention_seconds: float = 0.0
    helper_only_occupied: int = 0
    _episode_start: datetime | None = None
    _episode_activity: datetime | None = None
    _engine: bool | None = None
    _helper: bool | None = None
    agreement: bool | None = field(default=None)
    _counted: bool = False

    def update(
        self, now: datetime, engine: bool | None, helper: bool | None, last_activity: datetime | None
    ) -> bool:
        """Feed the current values. Returns True if anything visible changed."""
        before = self.as_dict(now)
        if self.since is None:
            self.since = now
        prev_helper = self._helper

        # Episodes ---------------------------------------------------------
        if prev_helper is True and helper is False and engine:
            self._episode_start = now
            self._episode_activity = last_activity
        elif self._episode_start is not None:
            new_activity = (
                last_activity is not None
                and (self._episode_activity is None or last_activity > self._episode_activity)
                and last_activity >= self._episode_start
            )
            if engine and (helper is True or new_activity):
                self.premature_clears += 1
                self._episode_start = None
            elif engine is False:
                overhang = (now - self._episode_start).total_seconds()
                self.over_retentions += 1
                self.over_retention_seconds += overhang
                self.max_over_retention_seconds = max(self.max_over_retention_seconds, overhang)
                self._episode_start = None
        # Agreement --------------------------------------------------------
        agreement = None if engine is None or helper is None else engine == helper
        if agreement is False and self.disagreement_since is None:
            self.disagreement_since = now
            self._counted = False
        elif agreement is not False and self.disagreement_since is not None:
            if self._counted:
                self.disagreement_seconds += (now - self.disagreement_since).total_seconds()
            self.disagreement_since = None
            self._counted = False
        if (
            self.disagreement_since is not None
            and not self._counted
            and now - self.disagreement_since >= timedelta(seconds=self.grace_seconds)
        ):
            self._counted = True
            self.disagreements += 1
            if helper is True and engine is False:
                self.helper_only_occupied += 1
        self.agreement = agreement
        self._engine, self._helper = engine, helper
        return self.as_dict(now) != before

    @property
    def reported_agreement(self) -> bool | None:
        """Agreement as published: a disagreement within the grace still counts as agreeing."""
        if self.agreement is False and not self._counted:
            return True
        return self.agreement

    def next_deadline(self) -> datetime | None:
        """When an ongoing disagreement becomes long enough to count."""
        if self.disagreement_since is not None and not self._counted:
            return self.disagreement_since + timedelta(seconds=self.grace_seconds)
        return None

    @property
    def episode_open(self) -> bool:
        return self._episode_start is not None

    def as_dict(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "agreement": self.reported_agreement,
            "disagreements": self.disagreements,
            "disagreement_since": _iso(self.disagreement_since) if self._counted else None,
            "total_disagreement_seconds": round(self.disagreement_seconds),
            "helper_cleared_prematurely": self.premature_clears,
            "engine_possibly_retained_too_long": self.over_retentions,
            "over_retention_total_seconds": round(self.over_retention_seconds),
            "over_retention_max_seconds": round(self.max_over_retention_seconds),
            "helper_only_occupied": self.helper_only_occupied,
            "episode_open_since": _iso(self._episode_start),
            "statistics_since": _iso(self.since),
        }

    def persist(self) -> dict[str, Any]:
        return {
            "since": _iso(self.since),
            "disagreements": self.disagreements,
            "disagreement_seconds": self.disagreement_seconds,
            "premature_clears": self.premature_clears,
            "over_retentions": self.over_retentions,
            "over_retention_seconds": self.over_retention_seconds,
            "max_over_retention_seconds": self.max_over_retention_seconds,
            "helper_only_occupied": self.helper_only_occupied,
        }

    @classmethod
    def restore(cls, data: dict[str, Any] | None, grace_seconds: float = 0.0) -> ComparisonTracker:
        """Counters survive restarts; open episodes and durations do not."""
        data = data or {}

        def num(key: str) -> float:
            value = data.get(key, 0)
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0

        return cls(
            since=_parse(data.get("since")),
            grace_seconds=grace_seconds,
            disagreements=int(num("disagreements")),
            disagreement_seconds=num("disagreement_seconds"),
            premature_clears=int(num("premature_clears")),
            over_retentions=int(num("over_retentions")),
            over_retention_seconds=num("over_retention_seconds"),
            max_over_retention_seconds=num("max_over_retention_seconds"),
            helper_only_occupied=int(num("helper_only_occupied")),
        )
