"""Occupancy forecast entities (0.5.0): observation only, consumed by automations and later modules.

Per enabled room:

* ``sensor.smart_occupancy_<room>_forecast`` - probability (%) that the room
  is occupied at the room's forecast horizon (default 15 min from now);
  attributes carry every horizon. ``unknown`` when there is not enough
  history (status attribute ``insufficient_data``), unavailable while the
  room's own occupancy data is unreliable or forecasts are off for the room.
* ``binary_sensor.smart_occupancy_<room>_predicted`` - on when that
  probability reaches the room's threshold *and* the confidence reaches the
  room's minimum confidence. Never on for "any nonzero probability".
* ``sensor.smart_occupancy_<room>_forecast_confidence`` - insufficient data /
  low / moderate / high, with the evidence behind it.

House wide: ``sensor.smart_occupancy_predicted_next_room`` (the room most
likely entered next after the latest arrival, or ``none``) and
``sensor.smart_occupancy_forecast_learning`` (diagnostic).

Recorder load: a state is written only when the rounded forecast changes
(whole percent for the state, 5 % steps for the other horizons, the
prediction, the status or the confidence level); explanations and other
detail attributes are not recorded. No entity has a state class, so no
long-term statistics are written.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import slugify

from .const import DOMAIN, SIGNAL_FORECAST_UPDATED
from .coordinator import SmartOccupancyCoordinator
from .discovery import area_name
from .entity import SmartOccupancyEntity, global_unique_id, room_entity_unique_id
from .forecast import STATUS_DISABLED, STATUS_OK, STATUS_UNAVAILABLE, RoomForecast
from .forecast_model import CONFIDENCE_LEVELS, HORIZONS

LEARNING_STATES = ["waiting", "learning", "ready", "disabled", "no_recorder", "error"]
# Detail that changes with every recalculation: never written to the Recorder.
UNRECORDED_FORECAST = frozenset({
    "explanation", "factors", "window", "schedule", "entry_probability", "updated", "reasons", "components",
    "candidates", "detail", "last_update", "queries",
})


class ForecastEntity(SmartOccupancyEntity):
    """Base: refreshed by the forecast manager's dispatcher signal; writes only on a changed fingerprint."""

    _attr_should_poll = False
    _unrecorded_attributes = UNRECORDED_FORECAST

    def __init__(self, coordinator: SmartOccupancyCoordinator, unique_id: str) -> None:
        super().__init__(coordinator, unique_id)
        self._published: Any = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_FORECAST_UPDATED, self._write_if_changed))

    @callback
    def _handle_coordinator_update(self) -> None:
        self._write_if_changed()

    def _fingerprint(self) -> Any:
        raise NotImplementedError

    @callback
    def _write_if_changed(self) -> None:
        if self.hass is None:
            return
        fingerprint = (self.available, self._fingerprint())
        if fingerprint != self._published:
            self._published = fingerprint
            self.async_write_ha_state()


class RoomForecastEntity(ForecastEntity):
    _platform_domain: str
    _key: str

    def __init__(self, coordinator: SmartOccupancyCoordinator, area_id: str) -> None:
        super().__init__(coordinator, room_entity_unique_id(area_id, self._key))
        self._area_id = area_id
        room_name = area_name(coordinator.hass, area_id) or area_id
        self._attr_translation_placeholders = {"room": room_name}
        self.entity_id = f"{self._platform_domain}.{DOMAIN}_{slugify(room_name) or slugify(area_id)}_{self._key}"

    @property
    def forecast(self) -> RoomForecast | None:
        return self.coordinator.forecast.room(self._area_id)

    @property
    def available(self) -> bool:
        rf = self.forecast
        return super().available and rf is not None and rf.status not in (STATUS_DISABLED, STATUS_UNAVAILABLE)

    def _fingerprint(self) -> Any:
        rf = self.forecast
        return rf.fingerprint() if rf else None

    def _common(self, rf: RoomForecast) -> dict[str, Any]:
        return {"status": rf.status, "horizon_minutes": rf.horizon, "observation_only": True}


class RoomForecastSensor(RoomForecastEntity, SensorEntity):
    """P(occupied at the forecast horizon), %."""

    _platform_domain = "sensor"
    _key = "forecast"
    _attr_translation_key = "room_forecast"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:crystal-ball"

    @property
    def native_value(self) -> int | None:
        rf = self.forecast
        if rf is None or rf.status != STATUS_OK or rf.probability is None:
            return None
        return round(rf.probability * 100)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rf = self.forecast
        if rf is None:
            return None
        out = self._common(rf)
        for h in HORIZONS:
            p = rf.probabilities.get(h) if rf.probabilities else None
            out[f"probability_{h}_min"] = None if p is None else round(p * 100)
        out["confidence"] = rf.confidence.level if rf.confidence else None
        out["predicted"] = rf.predicted
        out["threshold"] = rf.threshold
        out["explanation"] = rf.explanation
        out["window"] = rf.window.get("text") if rf.window else None
        out["schedule"] = {f"{h}_min": round(v * 100) for h, v in (rf.schedule or {}).items()} or None
        out["entry_probability"] = {f"{h}_min": round(v * 100) for h, v in (rf.entry or {}).items()} or None
        out["factors"] = [f.text for f in rf.factors] or None
        out["updated"] = rf.updated
        return out


class RoomForecastConfidenceSensor(RoomForecastEntity, SensorEntity):
    """How reliable the room's forecasts are, from the evidence behind them."""

    _platform_domain = "sensor"
    _key = "forecast_confidence"
    _attr_translation_key = "room_forecast_confidence"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(CONFIDENCE_LEVELS)
    _attr_icon = "mdi:gauge"

    @property
    def native_value(self) -> str | None:
        rf = self.forecast
        return rf.confidence.level if rf and rf.confidence else None

    def _fingerprint(self) -> Any:
        rf = self.forecast
        return (rf.status, rf.confidence.level if rf.confidence else None,
                rf.confidence.score // 5 if rf.confidence else None) if rf else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rf = self.forecast
        if rf is None or rf.confidence is None:
            return None
        return {**self._common(rf), "score": rf.confidence.score, "reasons": rf.confidence.reasons,
                "components": rf.confidence.components}


class RoomPredictedSensor(RoomForecastEntity, BinarySensorEntity):
    """Predicted occupancy: probability and confidence both reach the room's thresholds."""

    _platform_domain = "binary_sensor"
    _key = "predicted"
    _attr_translation_key = "room_predicted"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_icon = "mdi:crystal-ball"

    @property
    def is_on(self) -> bool | None:
        rf = self.forecast
        return rf.predicted if rf and rf.status == STATUS_OK else None

    def _fingerprint(self) -> Any:
        rf = self.forecast
        return (rf.status, rf.predicted, rf.threshold, rf.horizon, rf.confidence_threshold) if rf else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rf = self.forecast
        if rf is None:
            return None
        p = rf.probability
        return {**self._common(rf), "probability": None if p is None else round(p * 100),
                "threshold": rf.threshold, "confidence": rf.confidence.level if rf.confidence else None,
                "minimum_confidence": rf.confidence_threshold, "explanation": rf.explanation}


class PredictedNextRoomSensor(ForecastEntity, SensorEntity):
    """The room most likely entered next after the latest arrival anywhere in the house."""

    _attr_translation_key = "predicted_next_room"
    _attr_icon = "mdi:walk"

    def __init__(self, coordinator: SmartOccupancyCoordinator) -> None:
        super().__init__(coordinator, global_unique_id("predicted_next_room"))

    def _fingerprint(self) -> Any:
        nr = self.coordinator.forecast.next_room
        return (nr["area_id"], nr["source"], round(nr["probability"] * 20)) if nr else None

    @property
    def native_value(self) -> str:
        nr = self.coordinator.forecast.next_room
        return self.coordinator.forecast.name(nr["area_id"]) if nr else "none"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        nr = self.coordinator.forecast.next_room
        if not nr:
            return {"area_id": None, "observation_only": True}
        name = self.coordinator.forecast.name
        return {"area_id": nr["area_id"], "probability": round(nr["probability"] * 100), "from_area_id": nr["source"],
                "from": name(nr["source"]), "observation_only": True,
                "candidates": [{"area_id": a, "name": name(a), "probability": round(p * 100)}
                               for a, p in nr["candidates"]]}


class ForecastLearningSensor(ForecastEntity, SensorEntity):
    """Learning status of the forecast (diagnostic)."""

    _attr_translation_key = "forecast_learning"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = LEARNING_STATES
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:school-outline"

    def __init__(self, coordinator: SmartOccupancyCoordinator) -> None:
        super().__init__(coordinator, global_unique_id("forecast_learning"))

    def _fingerprint(self) -> Any:
        m = self.coordinator.forecast
        return (m.status.state, len(m.days), m.status.last_update)

    @property
    def native_value(self) -> str:
        return self.coordinator.forecast.status.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        m = self.coordinator.forecast
        st = m.status.as_dict()
        return {"detail": st["detail"], "days_learned": len(m.days), "history_from": st["recorder_oldest"],
                "window_days": m.settings.defaults.window_days, "last_update": st["last_update"],
                "queries": st["queries"], "last_error": st["last_error"], "observation_only": True}
