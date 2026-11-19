"""Temperature conversion (``homeassistant.util.unit_conversion.TemperatureConverter`` subset, same results)."""

from __future__ import annotations

from ..const import UnitOfTemperature
from ..exceptions import HomeAssistantError


class TemperatureConverter:
    """Converts between °C, °F and K exactly as Home Assistant does (through Celsius)."""

    UNIT_CLASS = "temperature"
    VALID_UNITS = {UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.KELVIN}

    @classmethod
    def convert(cls, value: float, from_unit: str, to_unit: str) -> float:
        if from_unit == to_unit:
            return value
        if from_unit not in cls.VALID_UNITS or to_unit not in cls.VALID_UNITS:
            raise HomeAssistantError(f"{from_unit} or {to_unit} is not a recognized temperature unit")
        if from_unit == UnitOfTemperature.FAHRENHEIT:
            celsius = (value - 32.0) / 1.8
        elif from_unit == UnitOfTemperature.KELVIN:
            celsius = value - 273.15
        else:
            celsius = value
        if to_unit == UnitOfTemperature.FAHRENHEIT:
            return celsius * 1.8 + 32.0
        if to_unit == UnitOfTemperature.KELVIN:
            return celsius + 273.15
        return celsius
