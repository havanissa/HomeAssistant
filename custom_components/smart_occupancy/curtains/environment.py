"""Environmental evidence for the curtains (0.6.3, the user's Phase 4.6.2).

Reads the measurements a window's decisions may use — outdoor and indoor
illuminance, room and outdoor temperature, the weather and cloud cover — from
existing Home Assistant entities, and judges whether each one can be trusted
**now**. Nothing here is a hard-coded entity: candidates are discovered by
domain, device class and unit, and the person chooses (globally, per room or
per window; see ``rules.py``).

Validity of a reading (``status``):

* ``ok`` — a number, reported recently, plausible;
* ``none`` — no entity chosen;
* ``missing`` — the chosen entity does not exist;
* ``unavailable`` — unavailable, unknown or not a number;
* ``stale`` — no report (``last_reported``) for longer than the stale limit;
* ``stuck`` — the *same value* for longer than the stuck limit although it
  should change (an outdoor light sensor in daylight; a constant dark reading
  at night is normal). A stuck light sensor is never read as permanent
  sunshine;
* ``implausible`` — impossible for the moment (thousands of lux while the sun
  is well below the horizon, as ``sensor.pir_taf_illuminance`` read at dawn).

Only ``ok`` readings take part in decisions. Weather and cloud cover are
supplementary: a forecast never overrides a working light sensor.

Temperatures are compared in °C (other units are converted). The temperature
band (cold / comfortable / warm / hot) has a hysteresis and a minimum stable
period, so a room hovering around a threshold does not move the covers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
import re
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from ..occupancy import friendly_name

STATUS_WORDS = {
    "ok": "Valid", "none": "Not chosen", "missing": "Not found", "unavailable": "Unavailable", "stale": "Stale",
    "stuck": "Appears stuck", "implausible": "Implausible",
}
BAND_WORDS = {"cold": "Cold", "comfortable": "Comfortable", "warm": "Warm", "hot": "Too warm"}

# Brightest plausible outdoor reading while the sun is below the horizon (lx). Twilight stays well
# below these; a street lamp shining on the sensor can still pass, which only delays dusk detection.
IMPLAUSIBLE_NIGHT_LUX = ((-6.0, 500.0), (0.0, 2000.0))
WEATHER_STALE_MINUTES = 180            # met.no and similar update about hourly
SUNNY_CONDITIONS = frozenset({"sunny", "partlycloudy"})
OUTDOOR_WORDS = re.compile(r"(outdoor|outside|exterior|uti|úti|ute|terrace|terass|terrass|garden|garð|balcony|"
                           r"framman|weather|veður|roof|tak)", re.I)
INDOOR_WORDS = re.compile(r"(indoor|inside|stova|kamar|køkur|kokur|room|stue|kitchen|bedroom)", re.I)


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def available(state: State | None) -> bool:
    return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)


def reported_at(state: State | None) -> datetime | None:
    if state is None:
        return None
    return getattr(state, "last_reported", None) or state.last_updated


def to_celsius(value: float | None, unit: str | None) -> float | None:
    if value is None:
        return None
    if unit in (None, "", UnitOfTemperature.CELSIUS):
        return value
    if unit in (UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.KELVIN):
        return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
    return None  # not a temperature


def _fmt_age(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {rest} min" if rest else f"{hours} h"
    return f"{hours // 24} days"


def _hhmm(when: datetime | None, now: datetime | None = None) -> str:
    """24-hour local time; with the date when it is not today ("16 Nov 21:23")."""
    if when is None:
        return "—"
    local = dt_util.as_local(when)
    if now is not None and dt_util.as_local(now).date() != local.date():
        return f"{local.day} {local.strftime('%b')} {local.strftime('%H:%M')}"
    return local.strftime("%H:%M")


@dataclass(slots=True)
class Reading:
    """One measurement and whether it can be trusted now."""

    role: str
    entity_id: str | None
    status: str = "none"
    value: float | None = None
    unit: str = ""
    detail: str = ""
    since: datetime | None = None       # when the value last changed
    reported: datetime | None = None    # when the device last reported (also an unchanged value)
    extra: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role, "entity_id": self.entity_id, "status": self.status,
                "status_word": STATUS_WORDS.get(self.status, self.status),
                "value": None if self.value is None else round(self.value, 1), "unit": self.unit,
                "detail": self.detail, "since": self.since.isoformat(timespec="seconds") if self.since else None,
                "reported": self.reported.isoformat(timespec="seconds") if self.reported else None,
                **(self.extra or {})}


# ------------------------------------------------------------------ light
def lux_value(state: State | None) -> float | None:
    if not available(state):
        return None
    value = _float(state.state)
    return value if value is not None and value >= 0 else None


def implausible_lux(value: float, elevation: float | None) -> bool:
    if elevation is None:
        return False
    return any(elevation < below and value > limit for below, limit in IMPLAUSIBLE_NIGHT_LUX)


def judge_lux(entity_id: str | None, state: State | None, *, now: datetime, since: datetime | None,
              elevation: float | None, stale_minutes: float, stuck_minutes: float, dark_lux: float,
              role: str = "outdoor_lux") -> Reading:
    """The validity of an illuminance reading. ``since`` = when its value last changed."""
    reading = Reading(role, entity_id, unit="lx")
    if not entity_id:
        reading.detail = "No light sensor chosen"
        return reading
    if state is None:
        reading.status, reading.detail = "missing", f"{entity_id} does not exist"
        return reading
    reading.reported = reported_at(state)
    value = lux_value(state)
    if value is None:
        reading.status = "unavailable"
        reading.detail = f"{entity_id} is {state.state}" if not available(state) else f"{entity_id} is not a number"
        return reading
    reading.value = value
    reading.since = since or state.last_changed
    if reading.reported is None or now - reading.reported > timedelta(minutes=stale_minutes):
        reading.status = "stale"
        reading.detail = (f"No report from {entity_id} for "
                          f"{_fmt_age(now - reading.reported) if reading.reported else 'too long'}")
        return reading
    if implausible_lux(value, elevation):
        reading.status = "implausible"
        reading.detail = f"{value:.0f} lx while the sun is at {elevation:.1f}°: not possible outdoors"
        return reading
    unchanged = now - reading.since if reading.since else timedelta()
    dark_night = value <= dark_lux and (elevation is None or elevation < 0)
    if stuck_minutes and not dark_night and unchanged >= timedelta(minutes=stuck_minutes):
        reading.status = "stuck"
        reading.detail = (f"{value:.0f} lx unchanged since {_hhmm(reading.since, now)} ({_fmt_age(unchanged)}) "
                          "although daylight always varies")
        return reading
    reading.status = "ok"
    reading.detail = f"{value:.0f} lx"
    return reading


def stuck_deadline(reading: Reading, stuck_minutes: float) -> datetime | None:
    """When an ok light reading would count as stuck if it does not change (so it is re-checked then)."""
    if reading.status != "ok" or not stuck_minutes or reading.since is None:
        return None
    return reading.since + timedelta(minutes=stuck_minutes)


# ------------------------------------------------------------ temperature
def temperature_of(state: State | None, hass_unit: str) -> tuple[float | None, str]:
    """(°C, how it was read) from a sensor, a thermostat (current temperature) or a weather entity."""
    if not available(state):
        return None, ""
    domain = state.entity_id.split(".", 1)[0]
    if domain == "climate":
        return to_celsius(_float(state.attributes.get("current_temperature")), hass_unit), "current temperature"
    if domain == "weather":
        unit = state.attributes.get("temperature_unit") or hass_unit
        return to_celsius(_float(state.attributes.get("temperature")), unit), "weather temperature"
    unit = state.attributes.get("unit_of_measurement")
    if state.attributes.get("device_class") not in (None, "temperature") or unit not in (
            UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.KELVIN):
        return None, "not a temperature"
    return to_celsius(_float(state.state), unit), ""


def judge_temperature(role: str, entity_id: str | None, state: State | None, *, now: datetime, hass_unit: str,
                      stale_minutes: float) -> Reading:
    reading = Reading(role, entity_id, unit="°C")
    if not entity_id:
        reading.detail = "No temperature source chosen"
        return reading
    if state is None:
        reading.status, reading.detail = "missing", f"{entity_id} does not exist"
        return reading
    reading.reported = reported_at(state)
    reading.since = state.last_changed
    value, how = temperature_of(state, hass_unit)
    if value is None:
        reading.status = "unavailable"
        reading.detail = (f"{entity_id} is {state.state}" if not available(state)
                          else f"{entity_id} has no usable temperature ({how or 'not a number'})")
        return reading
    reading.value = value
    limit = WEATHER_STALE_MINUTES if entity_id.startswith("weather.") else stale_minutes
    if reading.reported is None or now - reading.reported > timedelta(minutes=max(limit, stale_minutes)):
        reading.status = "stale"
        reading.detail = f"No report from {entity_id} for {_fmt_age(now - reading.reported) if reading.reported else 'too long'}"
        return reading
    if not -40 <= value <= 60:
        reading.status = "implausible"
        reading.detail = f"{value:.1f} °C is not a plausible {'room' if role == 'room_temp' else 'outdoor'} temperature"
        return reading
    reading.status = "ok"
    reading.detail = f"{value:.1f} °C" + (f" ({how})" if how else "")
    return reading


def thermostat_target(state: State | None, hass_unit: str) -> float | None:
    """The target temperature of a climate entity (the middle of a range) or an input_number, in °C."""
    if not available(state):
        return None
    if state.entity_id.startswith("input_number."):
        return to_celsius(_float(state.state), state.attributes.get("unit_of_measurement") or hass_unit)
    target = _float(state.attributes.get("temperature"))
    if target is None:
        low, high = _float(state.attributes.get("target_temp_low")), _float(state.attributes.get("target_temp_high"))
        target = (low + high) / 2 if low is not None and high is not None else None
    return to_celsius(target, hass_unit)


# ---------------------------------------------------------------- weather
def judge_weather(entity_id: str | None, state: State | None, cloud_entity: str | None, cloud_state: State | None,
                  *, now: datetime) -> Reading:
    """Condition and cloud cover. ``value`` is the cloud cover (%), ``extra`` the condition."""
    reading = Reading("weather", entity_id, unit="%", extra={"condition": None, "cloud_entity": cloud_entity})
    cloud: float | None = None
    if cloud_entity:
        cloud = _float(cloud_state.state) if available(cloud_state) else None
        if cloud is not None and not 0 <= cloud <= 100:
            cloud = None
    if not entity_id and not cloud_entity:
        reading.detail = "No weather entity chosen"
        return reading
    if entity_id and state is None:
        reading.status, reading.detail = "missing", f"{entity_id} does not exist"
        return reading
    if entity_id and not available(state):
        reading.status, reading.detail = "unavailable", f"{entity_id} is {state.state}"
        return reading
    condition = state.state if state is not None else None
    if cloud is None and state is not None:
        cloud = _float(state.attributes.get("cloud_coverage"))
    reading.value = cloud
    reading.extra["condition"] = condition
    ref = cloud_state if cloud_entity and cloud_state is not None else state
    reading.reported = reported_at(ref)
    reading.since = ref.last_changed if ref is not None else None
    if reading.reported is None or now - reading.reported > timedelta(minutes=WEATHER_STALE_MINUTES):
        reading.status = "stale"
        reading.detail = "The weather has not been updated for more than 3 hours"
        return reading
    reading.status = "ok"
    parts = [condition.replace("partlycloudy", "partly cloudy").replace("-", " ")] if condition else []
    if cloud is not None:
        parts.append(f"{cloud:.0f} % cloud")
    reading.detail = ", ".join(parts) or "no condition"
    return reading


def clear_sky(reading: Reading, cloud_max: float) -> bool | None:
    """Does the weather report a sky clear enough for direct sunshine? None = cannot say."""
    if not reading.ok:
        return None
    condition = (reading.extra or {}).get("condition")
    if reading.value is not None:
        return reading.value <= cloud_max and condition not in ("rainy", "pouring", "snowy", "fog", "hail")
    if condition:
        return condition in SUNNY_CONDITIONS
    return None


# --------------------------------------------------------- temperature band
@dataclass(frozen=True, slots=True)
class Comfort:
    low: float       # below: cold (solar heating is welcome)
    high: float      # above: warm (sun protection)
    top: float       # above: too warm (excess heat)
    hysteresis: float
    preferred: float
    source: str      # "setting" or the thermostat entity


def comfort(values: Mapping[str, Any], target: float | None, thermostat: str | None) -> Comfort:
    preferred = target if target is not None else values["preferred_temp"]
    band = values["comfort_band"]
    top = max(values["max_temp"], preferred + band + values["temp_hysteresis"])
    return Comfort(preferred - band, preferred + band, top, values["temp_hysteresis"], preferred,
                   thermostat if target is not None else "setting")


def band_of(value: float, state: str | None, c: Comfort) -> str:
    """The temperature band of a reading, with hysteresis relative to the current band."""
    h = c.hysteresis
    if value >= c.top or (state == "hot" and value > c.top - h):
        return "hot"
    if value >= c.high or (state in ("warm", "hot") and value > c.high - h):
        return "warm"
    if value <= c.low or (state == "cold" and value < c.low + h):
        return "cold"
    return "comfortable"


def band_text(state: str | None, value: float | None, c: Comfort) -> str:
    if state is None or value is None:
        return "Room temperature unknown"
    return {
        "cold": f"Room temperature {value:.1f} °C is below the comfortable range ({c.low:.1f}–{c.high:.1f} °C)",
        "comfortable": f"Room temperature {value:.1f} °C is comfortable ({c.low:.1f}–{c.high:.1f} °C)",
        "warm": f"Room temperature {value:.1f} °C exceeds the configured comfort maximum ({c.high:.1f} °C)",
        "hot": f"Room temperature {value:.1f} °C exceeds the maximum acceptable {c.top:.1f} °C",
    }[state]


def track_band(history, now: datetime, state: str | None, c: Comfort,
               stable: timedelta) -> tuple[str | None, datetime | None]:
    """The band now (a change needs ``stable`` of readings in the new band) and when it may change by time."""
    value = history.value
    if value is None:
        return None, None
    candidate = band_of(value, state, c)
    if state is None:
        return candidate, None   # the first reading is taken as it is: a temperature changes slowly
    if candidate == state:
        return state, None
    since = history.since(lambda v: band_of(v, state, c) == candidate)
    if since is not None and now - since >= stable:
        return candidate, None
    return state, (since + stable) if since is not None else None


# --------------------------------------------------------------- discovery
ROLE_WORDS = {
    "outdoor_lux": "Outdoor light", "indoor_lux": "Indoor light", "outdoor_temp": "Outdoor temperature",
    "room_temp": "Room temperature", "weather": "Weather", "cloud": "Cloud cover", "thermostat": "Thermostat",
}


def _is_lux(state: State) -> bool:
    return state.domain == "sensor" and (state.attributes.get("device_class") == "illuminance"
                                         or state.attributes.get("unit_of_measurement") == "lx")


def _is_temp(state: State) -> bool:
    return state.domain == "sensor" and (state.attributes.get("device_class") == "temperature"
                                         or state.attributes.get("unit_of_measurement") in ("°C", "°F"))


def _outdoor_hint(hass: HomeAssistant, state: State) -> bool:
    text = f"{state.entity_id} {friendly_name(hass, state.entity_id) or ''}"
    return bool(OUTDOOR_WORDS.search(text))


def discover(hass: HomeAssistant, *, chosen: Mapping[str, Iterable[str]], values: Mapping[str, Any],
             elevation: float | None, since_of: Callable[[str], datetime | None]) -> dict[str, list[dict[str, Any]]]:
    """Candidates for every environmental role with their validity now (the sensor-quality summary).

    ``chosen`` maps a role to the entities windows already use (marked ``in_use``). Candidates are
    ranked: in use, then valid, then those whose name suggests outdoors (for outdoor roles)."""
    now = dt_util.utcnow()
    unit = hass.config.units.temperature_unit
    out: dict[str, list[dict[str, Any]]] = {r: [] for r in ROLE_WORDS}

    def add(role: str, reading: Reading, state: State, outdoor: bool) -> None:
        used = reading.entity_id in set(chosen.get(role, ()))
        out[role].append({**reading.as_dict(), "name": friendly_name(hass, state.entity_id) or state.entity_id,
                          "outdoor_hint": outdoor, "in_use": used})

    for state in hass.states.async_all("sensor"):
        if _is_lux(state):
            outdoor = _outdoor_hint(hass, state)
            r = judge_lux(state.entity_id, state, now=now, since=since_of(state.entity_id), elevation=elevation,
                          stale_minutes=values["lux_stale"], stuck_minutes=values["lux_stuck"],
                          dark_lux=values["dusk_lux"])
            add("outdoor_lux", r, state, outdoor)
            add("indoor_lux", Reading("indoor_lux", r.entity_id, r.status, r.value, r.unit, r.detail, r.since,
                                      r.reported), state, not outdoor)
        elif _is_temp(state):
            outdoor = _outdoor_hint(hass, state)
            for role in ("outdoor_temp", "room_temp"):
                add(role, judge_temperature(role, state.entity_id, state, now=now, hass_unit=unit,
                                            stale_minutes=values["temp_stale"]), state,
                    outdoor if role == "outdoor_temp" else not outdoor)
        elif state.attributes.get("unit_of_measurement") == "%" and "cloud" in state.entity_id:
            r = judge_weather(None, None, state.entity_id, state, now=now)
            add("cloud", r, state, True)
    for state in hass.states.async_all("weather"):
        add("weather", judge_weather(state.entity_id, state, None, None, now=now), state, True)
        add("outdoor_temp", judge_temperature("outdoor_temp", state.entity_id, state, now=now, hass_unit=unit,
                                              stale_minutes=values["temp_stale"]), state, True)
    for state in hass.states.async_all("climate"):
        add("room_temp", judge_temperature("room_temp", state.entity_id, state, now=now, hass_unit=unit,
                                           stale_minutes=values["temp_stale"]), state, False)
        target = thermostat_target(state, unit)
        add("thermostat", Reading("thermostat", state.entity_id, "ok" if target is not None else "unavailable",
                                  target, "°C", f"target {target:.1f} °C" if target is not None else "no target"),
            state, False)
    for role, items in out.items():
        items.sort(key=lambda x: (not x["in_use"], x["status"] != "ok", not x["outdoor_hint"],
                                  x["name"].casefold()))
        out[role] = items[:60]
    return out
