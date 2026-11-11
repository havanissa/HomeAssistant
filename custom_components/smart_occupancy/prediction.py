"""Predictive occupancy - interfaces only.

Not implemented in Phase 0.2 (no historical learning). The occupancy engine
already reports every state transition to an ``OccupancyHistory`` sink, so
a later phase can plug in a recorder of transitions and a predictor without
restructuring the runtime.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .const import OccupancyState


class OccupancyPredictor(Protocol):
    """Predicts the probability that a room becomes occupied soon."""

    def predict(self, area_id: str) -> float | None:
        """Probability 0..1, or None when no prediction is available."""


class OccupancyHistory(Protocol):
    """Receives occupancy transitions (input for a future learning engine)."""

    def record(self, area_id: str, state: OccupancyState, occupied: bool | None, at: datetime) -> None:
        """Called once per state/occupied transition of a room. Must be cheap."""


class NullPredictor:
    """Phase 0.2 predictor: never predicts anything."""

    def predict(self, area_id: str) -> float | None:
        return None


class NullHistory:
    """Phase 0.2 history sink: keeps nothing."""

    def record(self, area_id: str, state: OccupancyState, occupied: bool | None, at: datetime) -> None:
        return None
