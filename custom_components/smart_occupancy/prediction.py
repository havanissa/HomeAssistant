"""Predictive occupancy - interface only.

Not implemented in Phase 0.1. The interface exists so the runtime can be
wired to a real predictor in a later phase without restructuring.
"""

from __future__ import annotations

from typing import Protocol


class OccupancyPredictor(Protocol):
    """Predicts the probability that a room becomes occupied soon."""

    def predict(self, area_id: str) -> float | None:
        """Probability 0..1, or None when no prediction is available."""


class NullPredictor:
    """Phase 0.1 predictor: never predicts anything."""

    def predict(self, area_id: str) -> float | None:
        return None
