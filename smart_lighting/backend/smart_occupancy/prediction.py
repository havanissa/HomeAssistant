"""Predictive occupancy - interfaces from Phase 0.2.

The occupancy forecast itself is implemented in 0.5.0 (``forecast.py``,
``forecast_model.py``, ``forecast_history.py``). It does not need the
``OccupancyHistory`` sink below: it learns from the Recorder's history by
replaying the configured signals through the same occupancy engine, and reads
the live engines through their listeners. ``ForecastManager.room(area_id)``
is the API for other modules. These interfaces stay for compatibility.
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
