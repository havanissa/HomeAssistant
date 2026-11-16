"""Phase 4.6.2 (0.6.3): solar and thermal curtain control, environmental evidence, 24-hour times.

Pure tests cover the sun geometry (south, east, west and obstructed windows, low
winter and high summer sun, the compensated ``sensor.sun_angle``), the validity
of measurements (stale, stuck, implausible, missing), the temperature band, the
arbitration of glare / heat / useful solar heating against sleep, night,
privacy, the lamp and manual holds, an equivalence check against the frozen
0.6.2 decision logic, and schedule times at midnight and across daylight-saving
changes. The runtime tests drive the simulated Faroese house of
``test_curtains_auto`` (Zigbee2MQTT-like covers, minute-by-minute light reports).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import random
from typing import Any

import pytest

from homeassistant.core import Context, HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.smart_occupancy.curtains import environment as envmod, logic, rules, solar
from custom_components.smart_occupancy.curtains.config import (
    CurtainConfig,
    CurtainConfigError,
    Obstruction,
    SolarSettings,
)

from . import legacy_curtain_logic_062 as legacy
from .test_curtains import (  # noqa: F401  (fixtures)
    STOVA_BLACKOUT,
    STOVA_SHEER,
    covers,
    house,
    set_cover,
)
from .test_curtains_auto import (  # noqa: F401  (fixtures)
    FAROE,
    LUX,
    Run,
    admin_ws,
    begin,
    enable,
    faroe,
    stova,
    utc,
    values,
)

TZ = dt_util.get_time_zone(FAROE)
ROOM_T = "sensor.stova_temperature"
OUT_T = "sensor.uti_tempratur"
WEATHER = "weather.forecast_home"
TAF = "sensor.pir_taf_illuminance"
MIN_LJOS = "sensor.min_ljos_terassa_framman"
STOVA_CONDS = {"morning": "p_half_shade", "day": "p_open", "sun": "p_full_shade", "glare": None, "heat": None,
               "evening_privacy": "p_evening_privacy", "night": "p_blackout", "sleep": "p_blackout",
               "lamp": "p_half_shade", "manual_override": None}


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def sun(azimuth: float | None, elevation: float | None, angle: float | None = None) -> solar.SunNow:
    return solar.SunNow(azimuth, elevation, angle)


# ===================================================================== geometry
def test_south_east_west_and_obstructed_windows() -> None:
    plain = SolarSettings()
    # South (180°): the low Faroese winter sun at noon is in front; below 3° it is too low.
    assert solar.exposure(180, sun(178, 8.5), plain)["status"] == "exposed"
    assert solar.exposure(180, sun(178, 2.0), plain)["status"] == "too_low"
    assert solar.exposure(180, sun(260, 8.0), plain)["status"] == "outside_sector"   # 80° off, width ± 70°
    assert solar.exposure(180, sun(0, -30), plain)["status"] == "below_horizon"
    # East (90°) behind a hill between 60° and 130° that hides the sun below 12°.
    hill = SolarSettings(obstructions=(Obstruction(60, 130, 12),))
    assert solar.exposure(90, sun(100, 6), hill)["status"] == "obstructed"
    assert solar.exposure(90, sun(110, 14), hill)["status"] == "exposed"
    assert solar.exposure(90, sun(140, 8), hill)["status"] == "exposed"              # past the hill
    # West (270°): afternoon sun.
    assert solar.exposure(270, sun(250, 20), plain)["status"] == "exposed"
    assert solar.exposure(270, sun(150, 20), plain)["status"] == "outside_sector"
    # High summer sun under eaves: above 45° the overhang shades the glass; the winter sun still enters.
    eaves = SolarSettings(max_elevation=45.0)
    assert solar.exposure(180, sun(180, 52), eaves)["status"] == "too_high"
    assert solar.exposure(180, sun(180, 52), plain)["status"] == "exposed"            # 0.6.2: no limit
    assert solar.exposure(180, sun(180, 9), eaves)["status"] == "exposed"
    # Sun right of / left of the window's direction and the incidence on the glass.
    e = solar.exposure(180, sun(200, 10), plain)
    assert e["offset"] == 20.0 and 21 < e["incidence"] < 23
    assert solar.exposure(180, sun(180, 0.5), plain)["incidence"] == pytest.approx(0.5, abs=0.01)
    assert solar.exposure(None, sun(180, 10), plain)["status"] == "no_direction"
    assert SolarSettings.from_dict({"exposure_width": 70}).max_elevation is None
    assert SolarSettings.from_dict({"max_elevation": 120}).max_elevation == 90.0


def test_compensated_sun_angle_is_not_an_azimuth() -> None:
    """``sensor.sun_angle`` reads 0° when the sun faces the south-facing windows. Once the calibration has
    fitted it, it gives the same angle-from-the-window as the azimuth; without that it is never used."""
    samples: list[tuple[float, float]] = []
    for az in range(120, 245, 5):
        solar.add_sample(samples, float(az), az - 180.0)          # the house's template: azimuth − 180
    cal = solar.calibrate(samples)
    assert cal["status"] == "validated" and cal["sign"] == 1 and cal["zero_azimuth"] == 180.0
    for bearing, az in ((180.0, 200.0), (90.0, 110.0), (270.0, 240.0)):
        via = solar.window_angle_from_sun_angle(az - 180.0, cal, bearing)
        assert via == pytest.approx(solar.angle_diff(az, bearing), abs=0.05), (bearing, az)
    # The east window's suspected relation from the investigation (sun_angle = azimuth − 113°).
    east = []
    for az in range(80, 200, 5):
        solar.add_sample(east, float(az), az - 113.0)
    cal_e = solar.calibrate(east)
    assert cal_e["zero_azimuth"] == 113.0
    assert solar.window_angle_from_sun_angle(0.0, cal_e, 90.0) == 23.0   # sun at 113°: 23° right of an E window
    # Not calibrated: the sun angle is not turned into a direction at all.
    assert solar.window_angle_from_sun_angle(10.0, solar.calibrate(samples[:3]), 180.0) is None


# ==================================================================== evidence
class _State:
    def __init__(self, entity_id: str, state: str, attributes: dict[str, Any] | None = None,
                 changed: datetime | None = None, reported: datetime | None = None) -> None:
        self.entity_id, self.state, self.attributes = entity_id, state, attributes or {}
        self.domain = entity_id.split(".", 1)[0]
        self.last_changed = changed or utc("2026-11-19T12:00:00+00:00")
        self.last_updated = self.last_changed
        self.last_reported = reported or self.last_changed


def test_light_sensor_validity_stale_stuck_implausible() -> None:
    original = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(TZ)                                # times in explanations are local
    try:
        _light_sensor_validity()
    finally:
        dt_util.set_default_time_zone(original)


def _light_sensor_validity() -> None:
    now = utc("2026-11-19T14:30:00+00:00")
    t0 = utc("2026-11-19T12:00:00+00:00")

    def judge(value: str, *, changed=t0, reported=None, elevation=9.0, stuck=120):
        state = _State(TAF, value, {"unit_of_measurement": "lx"}, changed, reported or now - timedelta(minutes=1))
        return envmod.judge_lux(TAF, state, now=now, since=None, elevation=elevation, stale_minutes=30,
                                stuck_minutes=stuck, dark_lux=50)

    stuck = judge("6229")
    assert stuck.status == "stuck" and "6229 lx unchanged since 12:00 (2 h 30 min)" in stuck.detail
    assert judge("6229", changed=now - timedelta(minutes=30)).status == "ok"
    assert judge("6229", stuck=0).status == "ok"                                  # detection switched off
    assert judge("0", elevation=-20).status == "ok"                               # a dark night is constant
    assert judge("0", elevation=12).status == "stuck"                              # a dead sensor in daylight
    implausible = judge("6229", changed=now, elevation=-5.0)
    assert implausible.status == "implausible" and "while the sun is at -5.0°" in implausible.detail
    assert judge("300", changed=now, elevation=-8).status == "ok"                  # a lamp near the sensor
    assert judge("1900", reported=now - timedelta(minutes=45)).status == "stale"
    assert judge("unavailable").status == "unavailable"
    assert envmod.judge_lux(TAF, None, now=now, since=None, elevation=9, stale_minutes=30, stuck_minutes=120,
                            dark_lux=50).status == "missing"
    assert envmod.judge_lux(None, None, now=now, since=None, elevation=9, stale_minutes=30, stuck_minutes=120,
                            dark_lux=50).status == "none"


def test_temperature_and_weather_readings() -> None:
    now = utc("2026-11-19T12:30:00+00:00")
    t = envmod.judge_temperature("room_temp", ROOM_T, _State(ROOM_T, "21.3", {"unit_of_measurement": "°C",
                                 "device_class": "temperature"}, reported=now), now=now, hass_unit="°C",
                                 stale_minutes=180)
    assert t.ok and t.value == 21.3
    f = envmod.judge_temperature("room_temp", ROOM_T, _State(ROOM_T, "70.0", {"unit_of_measurement": "°F"},
                                 reported=now), now=now, hass_unit="°C", stale_minutes=180)
    assert f.ok and f.value == pytest.approx(21.1, abs=0.05)                       # converted to °C
    climate = _State("climate.stova", "heat", {"current_temperature": 20.5, "temperature": 21.5}, reported=now)
    assert envmod.judge_temperature("room_temp", "climate.stova", climate, now=now, hass_unit="°C",
                                    stale_minutes=180).value == 20.5
    assert envmod.thermostat_target(climate, "°C") == 21.5
    stale = envmod.judge_temperature("room_temp", ROOM_T, _State(ROOM_T, "21", {"unit_of_measurement": "°C"},
                                     reported=now - timedelta(hours=4)), now=now, hass_unit="°C", stale_minutes=180)
    assert stale.status == "stale"
    assert envmod.judge_temperature("room_temp", ROOM_T, None, now=now, hass_unit="°C",
                                    stale_minutes=180).status == "missing"
    w = envmod.judge_weather(WEATHER, _State(WEATHER, "cloudy", {"cloud_coverage": 91, "temperature": 2.5},
                                             reported=now), None, None, now=now)
    assert w.ok and w.detail == "cloudy, 91 % cloud" and envmod.clear_sky(w, 30) is False
    clear = envmod.judge_weather(WEATHER, _State(WEATHER, "sunny", {"cloud_coverage": 8}, reported=now), None, None,
                                 now=now)
    assert envmod.clear_sky(clear, 30) is True
    assert envmod.clear_sky(envmod.judge_weather(None, None, None, None, now=now), 30) is None


def test_temperature_band_hysteresis_and_stability() -> None:
    c = envmod.comfort(values(), None, None)        # 21 ± 1.5, max 25, hysteresis 0.5
    assert (c.low, c.high, c.top) == (19.5, 22.5, 25.0)
    assert envmod.band_of(19.4, None, c) == "cold" and envmod.band_of(19.8, "cold", c) == "cold"
    assert envmod.band_of(20.1, "cold", c) == "comfortable"
    assert envmod.band_of(22.6, "comfortable", c) == "warm" and envmod.band_of(22.1, "warm", c) == "warm"
    assert envmod.band_of(21.9, "warm", c) == "comfortable"
    assert envmod.band_of(25.0, "warm", c) == "hot" and envmod.band_of(24.6, "hot", c) == "hot"
    assert envmod.band_of(24.4, "hot", c) == "warm"
    # A thermostat's target replaces the preferred temperature.
    assert envmod.comfort(values(), 22.0, "climate.stova").high == 23.5
    # Ten minutes in the new band before it counts; flapping across a threshold never does.
    t0 = utc("2026-11-19T12:00:00+00:00")
    h = logic.History()
    h.add(t0, 21.0)
    assert envmod.track_band(h, t0, None, c, timedelta(minutes=10)) == ("comfortable", None)
    for minute in range(1, 30):
        h.add(t0 + timedelta(minutes=minute), 22.6 if minute % 4 else 22.4)
        state, _ = envmod.track_band(h, t0 + timedelta(minutes=minute), "comfortable", c, timedelta(minutes=10))
        assert state == "comfortable"
    h.add(t0 + timedelta(minutes=30), 23.0)                           # warm since minute 29 (22.6, then 23.0)
    assert envmod.track_band(h, t0 + timedelta(minutes=38), "comfortable", c, timedelta(minutes=10)) == (
        "comfortable", t0 + timedelta(minutes=39))
    assert envmod.track_band(h, t0 + timedelta(minutes=39), "comfortable", c, timedelta(minutes=10))[0] == "warm"


# ================================================================== arbitration
def _flags(*, sleep=False, privacy=True, solar_on=True, conds=None) -> logic.Flags:
    return logic.Flags(sleep, privacy, solar_on, {**STOVA_CONDS, **(conds or {})})


def _sunny(**changes: Any) -> logic.Evidence:
    ev = logic.Evidence(now=at("2026-11-19T12:30"), lux=25000, lux_status="ok", lux_entity=LUX, daylight=True,
                        elevation=9.0, exposed=True, exposure_text="Potentially exposed to direct sunshine",
                        strong=True)
    for key, value in changes.items():
        setattr(ev, key, value)
    return ev


def _decide(flags: logic.Flags, v: dict[str, Any], ev: logic.Evidence, hold=None) -> logic.Decision:
    return logic.decide(flags, v, ev, hold=hold, active_since={}, morning_since=None)


THERMAL_TEXT = {
    "cold": "Room temperature 18.5 °C is below the comfortable range (19.5–22.5 °C)",
    "comfortable": "Room temperature 21.0 °C is comfortable (19.5–22.5 °C)",
    "warm": "Room temperature 23.8 °C exceeds the configured comfort maximum (22.5 °C)",
    "hot": "Room temperature 26.1 °C exceeds the maximum acceptable 25.0 °C",
}


def _band(band: str | None, **changes: Any) -> logic.Evidence:
    return _sunny(**{"thermal": band, "thermal_text": THERMAL_TEXT.get(band, "Room temperature unknown"), **changes})


def test_useful_solar_heat_versus_overheating_and_glare() -> None:
    v = values(thermal_enabled=True)
    f = _flags()
    warm = _decide(f, v, _band("warm"))
    assert (warm.condition, warm.preset_id) == ("sun", "p_full_shade")
    assert warm.text == ("Direct sunlight detected (25000 lx). Room temperature 23.8 °C exceeds the configured comfort "
                         "maximum (22.5 °C)")
    cold = _decide(f, v, _band("cold"))
    assert (cold.condition, cold.preset_id, cold.phase) == ("solar_heating", "p_open", "day")
    assert cold.text.endswith("Room temperature 18.5 °C is below the comfortable range (19.5–22.5 °C). Solar heating "
                              "is beneficial; keeping the Normal daytime preset")
    assert cold.direct_sun["status"] == "confirmed" and cold.thermal["state"] == "cold"
    hot = _decide(f, v, _band("hot"))
    assert (hot.condition, hot.preset_id) == ("heat", "p_full_shade")              # not set: Direct sunlight
    assert _decide(_flags(conds={"heat": "p_evening_privacy"}), v, _band("hot")).preset_id == "p_evening_privacy"
    assert _decide(f, v, _band("comfortable")).condition == "sun"
    # Warm outside: the sun's warmth is not needed, even in a cold room.
    warm_out = _decide(f, v, _band("cold", outdoor_warm=True, outdoor_text="Outdoor temperature 21.0 °C"))
    assert warm_out.condition == "sun" and "warm outside (outdoor temperature 21.0 °C)" in warm_out.text
    assert _decide(f, values(thermal_enabled=True, heat_gain="ignore"), _band("cold")).condition == "sun"
    # Glare wins over useful solar heating, not over a warm room's sun protection.
    glare = _decide(f, v, _band("cold", glare=True))
    assert (glare.condition, glare.preset_id) == ("glare", "p_full_shade")
    assert "Glare-sensitive window" in glare.text and "glare protection wins" in glare.text
    assert _decide(_flags(conds={"glare": "p_half_shade"}), v, _band("cold", glare=True)).preset_id == "p_half_shade"
    assert _decide(f, v, _band("warm", glare=True)).condition == "sun"
    # Temperature unknown: normal sun protection, exactly as without the option.
    unknown = _decide(f, v, _band(None, thermal_text="Room temperature unknown (sensor.x is unavailable)"))
    assert unknown.condition == "sun" and "normal sun protection applies" in unknown.text
    assert unknown.thermal["state"] == "unknown"
    # Option off: the temperature is not looked at; a glare-sensitive window uses the glare preset.
    assert _decide(f, values(), _band("cold")).condition == "sun"
    assert _decide(f, values(glare_sensitive=True), _sunny(glare=True)).condition == "glare"
    # Sun protection off for the window: nothing solar happens at all.
    off = _decide(_flags(solar_on=False), v, _band("hot", glare=True))
    assert off.condition == "day" and off.direct_sun["status"] == "off"
    # Mild light with the sun in front: no adjustment.
    mild = _decide(f, v, _band("hot", strong=False, lux=3000))
    assert mild.condition == "day" and mild.direct_sun["status"] == "not_detected"


def test_sleep_night_privacy_lamp_and_manual_holds_win_over_the_sun() -> None:
    v = values(thermal_enabled=True)
    assert _decide(_flags(sleep=True), v, _band("hot", sleep="on", sleep_entity="input_boolean.sleep")).condition \
        == "sleep"
    night = _decide(_flags(), v, _band("hot", now=at("2026-11-19T23:00")))
    assert night.condition == "night" and night.text.startswith("Night protection active until 06:30")
    dusk = _band("hot", now=at("2026-11-19T15:45"), elevation=-5.0, dusk_measured=True, lux=20)
    assert _decide(_flags(), v, dusk).condition == "evening_privacy"
    # A manual hold (released only by the next phase) is never bypassed by heat, glare or solar heating.
    hold = {"source": "manual", "created": utc("2026-11-19T12:00:00+00:00"), "policy": "until_released",
            "phase": "day"}
    assert _decide(_flags(), v, _band("hot"), hold=hold).condition == "manual_override"
    # Useful solar heating keeps the day phase: a "next phase" hold made in daytime stays.
    nxt = {**hold, "policy": "next_phase"}
    assert _decide(_flags(), v, _band("cold"), hold=nxt).condition == "manual_override"
    assert _decide(_flags(), v, _band("warm"), hold=nxt).hold_release is not None   # Direct sunlight is a new phase
    # The window lamp wins over heat unless "sun" is set to win over the lamp.
    lamp = _band("hot", lamp="on")
    assert _decide(_flags(), v, lamp).condition == "lamp"
    over = _decide(_flags(), values(thermal_enabled=True, lamp_bypass=("night", "sun")), lamp)
    assert over.condition == "heat" and "Lamp override bypassed: Excess solar heat" in over.text
    lamp_night = _decide(_flags(), v, _band(None, lamp="on", now=at("2026-11-19T22:30")))
    assert lamp_night.text == ("Night protection active until 06:30. Lamp override bypassed: Night is set to win over "
                               "the lamp")


def test_invalid_light_evidence_keeps_the_position() -> None:
    f = _flags()
    stuck = _sunny(lux=6229, lux_status="stuck", lux_entity=TAF, strong=None,
                   lux_detail="6229 lx unchanged since 12:05 (2 h 15 min) although daylight always varies",
                   now=at("2026-11-19T14:20"))
    d = _decide(f, values(), stuck)
    assert d.condition == "waiting" and d.preset_id is None
    assert d.text == ("The outdoor illuminance sensor sensor.pir_taf_illuminance appears stuck (6229 lx unchanged "
                      "since 12:05 (2 h 15 min) although daylight always varies). Sun protection not activated because "
                      "direct sunshine cannot be confirmed. The covers keep their position")
    assert d.direct_sun["status"] == "uncertain"
    dawn = _sunny(lux=6229, lux_status="implausible", lux_entity=TAF, strong=None, elevation=-5.0, exposed=False,
                  lux_detail="6229 lx while the sun is at -5.0°: not possible outdoors", now=at("2026-11-19T07:40"))
    d = _decide(_flags(privacy=False), values(), dawn)
    assert d.condition == "waiting" and "implausible value" in d.text
    stale = _decide(f, values(), _sunny(lux_status="stale", strong=None, now=at("2026-11-19T13:00")))
    assert stale.condition == "waiting" and "Sun protection not activated" in stale.text


def test_weather_is_supplementary_and_never_opens() -> None:
    v = values(sun_evidence="weather", fallback="sun")
    base = dict(lux=None, lux_status="none", lux_entity=None, daylight=None, strong=None, fallback_daylight=True,
                weather_text="clear sky: sunny, 8 % cloud")
    likely = _decide(_flags(), v, _sunny(weather_sun=True, **base))
    assert likely.condition == "sun" and likely.direct_sun["status"] == "likely"
    assert _decide(_flags(), values(fallback="sun"), _sunny(weather_sun=True, **base)).condition == "day"
    # The weather drops out while shading: the window keeps its sun position (it does not open).
    lost = _decide(_flags(), v, _sunny(weather_sun=None, in_sun=True, **{**base, "weather_text": ""}))
    assert lost.condition == "waiting" and lost.sun_hold and "keep their sun position" in lost.text
    # A working light sensor decides; the forecast is only shown.
    measured = _decide(_flags(), values(sun_evidence="weather"), _sunny(weather_sun=False, strong=True,
                                                                        weather_text="cloudy, 91 % cloud"))
    assert measured.condition == "sun" and "Weather: cloudy, 91 % cloud" in measured.evidence


def test_precedence_lists_heat_glare_and_solar_heating_when_enabled() -> None:
    assert [r["key"] for r in logic.precedence(values(thermal_enabled=True, glare_sensitive=True))] == [
        "safety", "manual_override", "sleep", "night", "evening_privacy", "lamp", "heat", "sun", "glare",
        "solar_heating", "morning", "day", "waiting"]
    assert [r["key"] for r in logic.precedence(values(thermal_enabled=True, lamp_bypass=("sleep", "sun")))] == [
        "safety", "manual_override", "sleep", "heat", "sun", "lamp", "night", "evening_privacy", "solar_heating",
        "morning", "day", "waiting"]


# ============================================================ upgrade: 0.6.2 equality
def test_defaults_decide_exactly_like_0_6_2() -> None:
    """Thousands of random situations: with every 0.6.3 option at its default, the condition, the preset,
    hold releases, the next check and whether opening is blocked equal the frozen 0.6.2 logic."""
    rng = random.Random(4629)
    presets = ["p_open", "p_half_shade", "p_full_shade", "p_evening_privacy", "p_blackout", None]
    for _ in range(4000):
        now = at(f"2026-11-{rng.choice([19, 20, 21, 22])}T{rng.randrange(24):02d}:{rng.randrange(60):02d}")
        v = values(
            manual_bypass=tuple(x for x in ("sleep", "night", "evening_privacy") if rng.random() < 0.3),
            lamp_bypass=tuple(x for x in rules.LAMP_BYPASS if rng.random() < 0.4),
            fallback=rng.choice(["none", "sun"]), morning_mode=rng.choice(["duration", "until_time"]),
            sleep_end=rng.choice(["morning", "resume", "hold"]), sleep_require_daylight=rng.random() < 0.5,
            manual_policy=rng.choice(["next_phase", "duration", "vacant", "until_released"]))
        conds = {c: rng.choice(presets) for c in ("morning", "day", "sun", "evening_privacy", "night", "sleep", "lamp")}
        flags = (rng.random() < 0.5, rng.random() < 0.5, rng.random() < 0.6)
        lux_status = rng.choice(["ok", "ok", "stale", "unavailable", "none"])
        common = dict(
            now=now, sleep=rng.choice([None, "on", "off", "unknown"]), sleep_entity="input_boolean.sleep",
            sleep_ended=rng.choice([None, now - timedelta(minutes=rng.randrange(600))]),
            lamp=rng.choice([None, "on", "releasing", "off"]),
            lamp_release_at=now + timedelta(minutes=2),
            lux=rng.choice([0.0, 30.0, 400.0, 3000.0, 25000.0]) if lux_status in ("ok", "stale") else None,
            lux_status=lux_status, lux_entity=LUX,
            daylight=rng.choice([None, True, False]) if lux_status == "ok" else None,
            dusk_measured=rng.random() < 0.3 if lux_status == "ok" else False,
            elevation=rng.choice([None, -15.0, -5.0, -1.0, 2.0, 5.0, 12.0]),
            exposed=rng.random() < 0.5, exposure_text="x",
            strong=rng.choice([None, True, False]) if lux_status == "ok" else None,
            fallback_daylight=rng.choice([None, True, False]) if lux_status == "none" else None,
            vacant_since=rng.choice([None, now - timedelta(minutes=rng.randrange(120))]),
            occupancy_known=rng.random() < 0.5,
            deadlines=[now + timedelta(minutes=rng.randrange(1, 200))])
        hold = None
        if rng.random() < 0.4:
            hold = {"source": rng.choice(["manual", "preset", "sleep_end"]),
                    "created": now - timedelta(minutes=rng.randrange(1, 400)),
                    "policy": rng.choice(["next_phase", "duration", "vacant", "until_released"]),
                    "phase": rng.choice(["day", "morning", "sun", "night", "sleep", "evening_privacy", None])}
        active = {c: now - timedelta(minutes=rng.randrange(1, 300)) for c in ("sleep", "night", "evening_privacy")
                  if rng.random() < 0.3}
        morning = rng.choice([None, now - timedelta(minutes=rng.randrange(1, 300))])
        new = logic.decide(logic.Flags(*flags, conds), v, logic.Evidence(**common), hold=hold, active_since=active,
                           morning_since=morning)
        old = legacy.decide(legacy.Flags(*flags, conds), v, legacy.Evidence(**common), hold=hold, active_since=active,
                            morning_since=morning)
        situation = (common, flags, conds, hold)
        assert (new.condition, new.base, new.preset_id) == (old.condition, old.base, old.preset_id), situation
        assert (new.hold_release is None) == (old.hold_release is None), situation
        assert new.next_check == old.next_check and new.morning_start == old.morning_start, situation
        assert (new.opening_blocked is None) == (old.opening_blocked is None), situation


# ============================================================ 24-hour schedule times
def test_time_settings_are_24_hour_hh_mm() -> None:
    for text in ("00:00", "12:00", "23:59", "07:00", "21:30"):
        assert rules.validate("night_start_workday", text, "global") == text
    for bad in ("24:00", "23:60", "7:30", "7:30 PM", "11:30am", "", "2230", 1230):
        with pytest.raises(CurtainConfigError):
            rules.validate("night_start_workday", bad, "global")
    # Stored 0.6.2 times load unchanged (their meaning never changes).
    assert rules.load({"night_start_workday": "22:30", "day_from_free": "11:00"}, "global") == {
        "night_start_workday": "22:30", "day_from_free": "11:00"}


def test_night_at_midnight_and_noon_boundaries() -> None:
    v = values(night_start_workday="00:00", night_end_workday="06:30")
    assert not logic.night_now(at("2026-11-19T23:59"), v)[0]
    assert logic.night_now(at("2026-11-20T00:00"), v)[0]                 # Thursday night, after midnight
    assert logic.night_now(at("2026-11-20T06:29"), v)[0] and not logic.night_now(at("2026-11-20T06:30"), v)[0]
    v = values(night_start_workday="23:59")
    assert logic.night_now(at("2026-11-19T23:59"), v)[0] and not logic.night_now(at("2026-11-19T23:58"), v)[0]
    v = values(night_start_workday="12:00", night_end_workday="06:30")
    assert logic.night_now(at("2026-11-19T12:00"), v)[0] and not logic.night_now(at("2026-11-19T11:59"), v)[0]


@pytest.mark.parametrize("zone", ["Atlantic/Faroe", "Europe/Copenhagen"])
def test_night_across_daylight_saving_changes(zone: str) -> None:
    """Schedule times are wall-clock times: 08:00 stays 08:00 on the days the clocks change."""
    tz = dt_util.get_time_zone(zone)
    v = values()                                  # free-day night 23:30 → 08:00 (Sat/Sun)

    def local(utc_text: str) -> datetime:
        return datetime.fromisoformat(utc_text).astimezone(tz)

    # Spring forward: Sunday 29 March 2026 (01:00 UTC).
    offset = 1 if zone == "Atlantic/Faroe" else 2                  # summer offset from UTC
    end_utc = datetime(2026, 3, 29, 8 - offset, 0, tzinfo=dt_util.UTC)
    assert logic.night_now(local((end_utc - timedelta(minutes=1)).isoformat()), v)[0]
    inside, change, _ = logic.night_now(local((end_utc - timedelta(minutes=1)).isoformat()), v)
    assert dt_util.as_utc(change) == end_utc and change.strftime("%H:%M") == "08:00"
    assert not logic.night_now(local(end_utc.isoformat()), v)[0]
    # Fall back: Sunday 25 October 2026.
    winter = 0 if zone == "Atlantic/Faroe" else 1
    end_utc = datetime(2026, 10, 25, 8 - winter, 0, tzinfo=dt_util.UTC)
    inside, change, _ = logic.night_now(local((end_utc - timedelta(minutes=1)).isoformat()), v)
    assert inside and dt_util.as_utc(change) == end_utc
    # The night before started at 23:30 local on Saturday, before the change.
    start_local = logic.night_bounds(date(2026, 10, 24), v, tz)[0]
    assert start_local.strftime("%H:%M") == "23:30"


# ================================================================ runtime (HA)
class SolarRun(Run):
    """The scenario driver of test_curtains_auto plus temperature sensors that report every minute."""

    def __init__(self, base: Run) -> None:
        super().__init__(base.hass, base.freezer, base.api, base.mgr, base.covers)
        self.lux_value = base.lux_value
        self.temps: dict[str, float] = {}

    def temp(self, entity_id: str, value: float) -> None:
        self.temps[entity_id] = value
        self.hass.states.async_set(entity_id, str(value), {"unit_of_measurement": "°C", "device_class": "temperature",
                                                           "friendly_name": entity_id.split(".")[1]})

    async def walk(self, minutes: float, step: float = 60) -> None:
        for _ in range(max(1, round(minutes * 60 / step))):
            for entity_id, value in self.temps.items():
                self.temp(entity_id, value)
            await super().walk(step / 60, step)


async def solar_begin(hass, pilot, freezer, covers, admin_ws, house, *, lux=3000, elevation=9.0, azimuth=178.0,
                      room=18.5, when="2026-11-19T12:05:00+00:00", **window_settings):
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 100)
    _e, base = await begin(hass, pilot, freezer, covers, admin_ws, when, lux=lux, elevation=elevation,
                           azimuth=azimuth)
    r = SolarRun(base)
    r.temp(ROOM_T, room)
    r.temp(OUT_T, 4.0)
    s = await stova(r.api, house, solar_protection=True)
    await r.api.ok("auto/settings", scope="window", window_id=s,
                   values={"min_interval": 0, "room_temp_entity": ROOM_T, "outdoor_temp_entity": OUT_T,
                           **window_settings})
    await r.settle()
    await r.walk(11)
    return _e, r, s


async def test_south_window_temperature_aware_sun_with_layered_covers(hass, pilot, house, covers, admin_ws,
                                                                       freezer, faroe) -> None:
    _e, r, s = await solar_begin(hass, pilot, freezer, covers, admin_ws, house)
    await enable(r.api, s)
    await hass.async_block_till_done()
    assert r.condition(s) == "day" and covers.calls == []
    # The predicted result is shown before temperature-aware sun is switched on; nothing is saved or moved.
    log_before = list(r.auto(s)["log"])
    sim = await r.api.ok("auto/simulate", scope="window", window_id=s, values={"thermal_enabled": True})
    assert [w["predicted"]["condition"] for w in sim["windows"]] == ["day"] and not sim["windows"][0]["changes"]
    assert r.auto(s)["values"]["thermal_enabled"] is False and r.auto(s)["log"] == log_before
    await r.api.ok("auto/settings", scope="window", window_id=s, values={"thermal_enabled": True})
    # 12:16 — direct sunshine, but the room is cold (18.5 °C): its warmth is welcome, Fully open stays.
    r.lux(25000)
    await r.walk(11)
    d = r.auto(s)["decision"]
    assert d["condition"] == "solar_heating" and d["preset_id"] == "p_open" and covers.calls == []
    assert "Room temperature 18.5 °C" in d["text"] and "Solar heating is beneficial; keeping the Normal daytime" \
        in d["text"]
    env = r.auto(s)["environment"]
    assert env["direct_sun"]["status"] == "confirmed" and env["band"] == "cold" and env["sun"]["offset"] == -2.0
    assert env["readings"]["room_temp"]["status"] == "ok" and env["readings"]["outdoor_lux"]["status"] == "ok"
    assert r.auto(s)["next_evaluation"] is not None
    # The room warms above the comfortable range: after 10 stable minutes, Full shade (the sheer only).
    r.temp(ROOM_T, 23.8)
    await r.walk(9)
    assert r.condition(s) == "solar_heating" and covers.calls == []
    await r.walk(2)
    d = r.auto(s)["decision"]
    assert d["condition"] == "sun" and "exceeds the configured comfort maximum (22.5 °C)" in d["text"]
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (0, 100) and covers.services() == [
        ("set_cover_position", STOVA_SHEER, 0)]
    assert r.mgr.last[s]["status"] == "succeeded"                     # every cover verified
    moved = r.auto(s)["log"][0]
    assert moved["action"] == "moved" and "Applying Full shade (Direct sunlight): moving 1 cover" in moved["text"]
    # Too warm: Excess solar heat, at first the Direct sunlight preset (no move), then its own preset.
    r.temp(ROOM_T, 26.1)
    await r.walk(11)
    d = r.auto(s)["decision"]
    assert (d["condition"], d["preset_id"]) == ("heat", "p_full_shade") and len(covers.calls) == 1
    await r.api.ok("modes/change", ops=[{"op": "assign", "mode": "heat", "window_id": s,
                                         "preset_id": "p_evening_privacy"}], confirm=True, policy="now")
    await r.walk(1)
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (0, 60) and len(covers.calls) == 2
    # Hysteresis: 24.7 °C is still too warm (until below 24.5 °C); then Direct sunlight again.
    r.temp(ROOM_T, 24.7)
    await r.walk(15)
    assert r.condition(s) == "heat" and len(covers.calls) == 2
    r.temp(ROOM_T, 24.2)
    await r.walk(11)
    assert r.condition(s) == "sun" and (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (0, 100)
    # Cold again, then the window is marked glare-sensitive (predicted first): glare protection wins.
    r.temp(ROOM_T, 18.0)
    await r.walk(11)
    assert r.condition(s) == "solar_heating" and (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (100, 100)
    calls = len(covers.calls)
    sim = await r.api.ok("auto/simulate", scope="window", window_id=s, values={"glare_sensitive": True})
    p = sim["windows"][0]
    assert p["predicted"]["condition"] == "glare" and p["preset_name"] == "Full shade" and p["moves"] == 1
    assert p["changes"] and len(covers.calls) == calls
    await r.api.ok("auto/settings", scope="window", window_id=s, values={"glare_sensitive": True})
    await r.walk(1)
    d = r.auto(s)["decision"]
    assert d["condition"] == "glare" and "glare protection wins" in d["text"] and r.pos(STOVA_SHEER) == 0
    await r.api.ok("modes/change", ops=[{"op": "assign", "mode": "glare", "window_id": s,
                                         "preset_id": "p_half_shade"}], confirm=True, policy="now")
    await r.walk(1)
    assert r.pos(STOVA_SHEER) == 50
    keys = [x["key"] for x in r.auto(s)["precedence"]]
    assert keys.index("heat") < keys.index("sun") < keys.index("glare") < keys.index("solar_heating")


async def test_room_temperature_hovering_at_a_threshold_does_not_move(hass, pilot, house, covers, admin_ws,
                                                                      freezer, faroe) -> None:
    _e, r, s = await solar_begin(hass, pilot, freezer, covers, admin_ws, house, lux=25000, room=21.0,
                                 thermal_enabled=True)
    await enable(r.api, s)
    await hass.async_block_till_done()
    assert r.condition(s) == "sun" and len(covers.calls) == 1       # comfortable: normal sun protection
    for minute in range(90):                                          # 22.4 / 22.6 around 22.5 every 4 minutes
        r.temp(ROOM_T, 22.6 if (minute // 4) % 2 else 22.4)
        await r.walk(1)
    assert r.condition(s) == "sun" and len(covers.calls) == 1
    for minute in range(60):                                          # 19.4 / 19.7 around 19.5
        r.temp(ROOM_T, 19.4 if (minute // 5) % 2 else 19.7)
        await r.walk(1)
    assert r.condition(s) == "sun" and len(covers.calls) == 1


async def test_clouds_passing_quickly_do_not_move_the_curtains(hass, pilot, house, covers, admin_ws, freezer,
                                                               faroe) -> None:
    _e, r, s = await solar_begin(hass, pilot, freezer, covers, admin_ws, house)
    await enable(r.api, s)
    await hass.async_block_till_done()
    for _cycle in range(20):                                          # 3 min sun, 3 min cloud: 2 hours
        r.lux(25000)
        await r.walk(3)
        r.lux(4000)
        await r.walk(3)
    assert r.condition(s) == "day" and covers.calls == []
    for _cycle in range(8):                                           # mostly sunny with short clouds
        r.lux(26000)
        await r.walk(12)
        r.lux(5000)
        await r.walk(4)
    assert r.condition(s) == "sun" and len(covers.calls) == 1          # shaded once, kept through the clouds
    r.lux(3000)
    await r.walk(21)
    assert r.condition(s) == "day" and len(covers.calls) == 2
    assert r.auto(s)["moves_today"] == 2


async def test_stuck_light_sensor_is_not_permanent_sunshine(hass, pilot, house, covers, admin_ws, freezer,
                                                            faroe) -> None:
    _e, r, s = await solar_begin(hass, pilot, freezer, covers, admin_ws, house, lux=25000)
    r.jitter = False                                                  # the same 25000 lx, report after report
    await enable(r.api, s)
    await hass.async_block_till_done()
    assert r.condition(s) == "sun" and len(covers.calls) == 1
    await r.walk(110)
    assert r.condition(s) == "sun"
    await r.walk(12)                                                  # 2 hours unchanged in daylight
    d = r.auto(s)["decision"]
    assert d["condition"] == "waiting" and "appears stuck" in d["text"]
    assert "Sun protection not activated because direct sunshine cannot be confirmed" in d["text"]
    assert r.auto(s)["environment"]["readings"]["outdoor_lux"]["status"] == "stuck"
    assert len(covers.calls) == 1                                     # kept where it is
    r.sun(9, 260)                                                     # the sun leaves the window: still no opening
    await r.walk(5)
    assert r.condition(s) == "waiting" and len(covers.calls) == 1
    review = await r.api.ok("auto/preview", window_id=s)
    assert any("not trusted now" in x for x in review["warnings"])
    # The sensor reports changing values again: daylight must be stable again before anything opens.
    r.jitter = True
    r.lux(3000)
    await r.walk(9)
    assert len(covers.calls) == 1
    await r.walk(2)
    assert r.condition(s) == "day" and (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (100, 100)


async def test_missing_temperature_and_weather_sources(hass, pilot, house, covers, admin_ws, freezer,
                                                       faroe) -> None:
    _e, r, s = await solar_begin(hass, pilot, freezer, covers, admin_ws, house, lux=25000, thermal_enabled=True,
                                 room_temp_entity="sensor.stova_nowhere_temperature")
    await enable(r.api, s)
    await hass.async_block_till_done()
    d = r.auto(s)["decision"]
    assert d["condition"] == "sun" and d["thermal"]["state"] == "unknown"
    assert "sensor.stova_nowhere_temperature does not exist" in d["thermal"]["text"]
    assert "normal sun protection applies" in d["text"]
    review = await r.api.ok("auto/preview", window_id=s)
    assert any("room temperature is not usable" in x for x in review["warnings"])
    r.lux(3000)                                                       # clouds: back to Normal daytime
    await r.walk(21)
    assert r.condition(s) == "day" and r.pos(STOVA_SHEER) == 100
    # No light sensor: the sun fallback opens; the weather may confirm sunshine (opt-in), never open.
    await r.api.ok("auto/settings", scope="global", values={"lux_entity": None})
    await r.api.ok("auto/settings", scope="window", window_id=s,
                   values={"fallback": "sun", "sun_evidence": "weather", "weather_entity": WEATHER,
                           "thermal_enabled": False})
    await r.walk(1)
    d = r.auto(s)["decision"]
    assert d["condition"] == "day" and d["direct_sun"]["status"] == "uncertain"      # weather entity missing
    calls = len(covers.calls)
    hass.states.async_set(WEATHER, "sunny", {"cloud_coverage": 8, "temperature": 3.0})
    await r.walk(9)
    assert r.condition(s) == "day"
    await r.walk(2)
    d = r.auto(s)["decision"]
    assert d["condition"] == "sun" and d["direct_sun"]["status"] == "likely" and len(covers.calls) == calls + 1
    hass.states.async_set(WEATHER, "unavailable", {})                 # the weather drops out: kept shaded
    await r.walk(5)
    d = r.auto(s)["decision"]
    assert d["condition"] == "waiting" and "keep their sun position" in d["text"] and len(covers.calls) == calls + 1
    hass.states.async_set(WEATHER, "cloudy", {"cloud_coverage": 95, "temperature": 3.0})
    await r.walk(19)
    assert r.condition(s) == "waiting"
    await r.walk(3)
    assert r.condition(s) == "day" and len(covers.calls) == calls + 2


async def test_environment_discovery_and_sensor_quality(hass, pilot, house, covers, admin_ws, freezer,
                                                        faroe) -> None:
    freezer.move_to(utc("2026-11-19T12:00:00+00:00"))
    hass.states.async_set(TAF, "6229", {"unit_of_measurement": "lx", "device_class": "illuminance",
                                        "friendly_name": "PIR TAF illuminance"})
    hass.states.async_set(MIN_LJOS, "2776", {"unit_of_measurement": "lx", "friendly_name": "Minst ljós terassa"})
    hass.states.async_set(WEATHER, "cloudy", {"cloud_coverage": 91, "temperature": 2.5, "temperature_unit": "°C"})
    hass.states.async_set("climate.stova", "heat", {"current_temperature": 21.0, "temperature": 21.5})
    _e, base = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T12:00:00+00:00", lux=1920,
                           elevation=8.0, azimuth=180.0)
    r = SolarRun(base)
    for _ in range(26):                                                # 130 minutes; TAF never changes
        r.temp(OUT_T, 2.5)
        r.temp(ROOM_T, 21.3)
        hass.states.async_set(MIN_LJOS, str(2776 + _ % 3), {"unit_of_measurement": "lx",
                                                           "friendly_name": "Minst ljós terassa"})
        hass.states.async_set(TAF, "6229", {"unit_of_measurement": "lx", "device_class": "illuminance",
                                            "friendly_name": "PIR TAF illuminance"})
        hass.states.async_set(WEATHER, "cloudy", {"cloud_coverage": 91, "temperature": 2.5,
                                                  "temperature_unit": "°C"}, force_update=True)
        await base.walk(5)
    model = await r.api.ok("model")
    roles = {x["role"]: x for x in model["environment"]["roles"]}
    lux = {c["entity_id"]: c for c in roles["outdoor_lux"]["candidates"]}
    assert lux[TAF]["status"] == "stuck" and "6229 lx unchanged since 12:00" in lux[TAF]["detail"]
    assert lux[LUX]["status"] == "ok" and lux[LUX]["in_use"] and roles["outdoor_lux"]["candidates"][0]["entity_id"] \
        == LUX
    assert lux[MIN_LJOS]["status"] == "ok" and lux[MIN_LJOS]["outdoor_hint"]
    weather = {c["entity_id"]: c for c in roles["weather"]["candidates"]}
    assert weather[WEATHER]["status"] == "ok" and weather[WEATHER]["detail"] == "cloudy, 91 % cloud"
    outdoor = {c["entity_id"]: c for c in roles["outdoor_temp"]["candidates"]}
    assert outdoor[OUT_T]["status"] == "ok" and outdoor[OUT_T]["outdoor_hint"] and outdoor[WEATHER]["value"] == 2.5
    room = {c["entity_id"]: c for c in roles["room_temp"]["candidates"]}
    assert room["climate.stova"]["value"] == 21.0 and room[ROOM_T]["status"] == "ok"
    assert {c["entity_id"]: c for c in roles["thermostat"]["candidates"]}["climate.stova"]["value"] == 21.5
    # "Use for all windows" is an ordinary global setting.
    await r.api.ok("auto/settings", scope="global", values={"outdoor_temp_entity": OUT_T, "weather_entity": WEATHER})
    model = await r.api.ok("model")
    assert model["environment"]["global"]["outdoor_temp_entity"] == OUT_T
    assert model["time_zone"] == FAROE
    entities = model["entities"]
    assert OUT_T in {x["entity_id"] for x in entities["temperature"]}
    assert WEATHER in {x["entity_id"] for x in entities["weather"]}
    assert "climate.stova" in {x["entity_id"] for x in entities["thermostat"]}
    # A light reading that is impossible for the sun's position is flagged.
    r.sun(-8.0, 300)
    hass.states.async_set(MIN_LJOS, "5000", {"unit_of_measurement": "lx", "friendly_name": "Minst ljós terassa"})
    model = await r.api.ok("model")
    roles = {x["role"]: x for x in model["environment"]["roles"]}
    assert {c["entity_id"]: c for c in roles["outdoor_lux"]["candidates"]}[MIN_LJOS]["status"] == "implausible"
    assert covers.calls == []


async def test_restart_keeps_settings_temperature_band_and_stuck_detection(hass, pilot, house, covers, admin_ws,
                                                                          freezer, faroe) -> None:
    entry, r, s = await solar_begin(hass, pilot, freezer, covers, admin_ws, house, lux=25000, room=23.8,
                                    thermal_enabled=True, glare_sensitive=True, glare_when="occupied")
    await r.api.ok("window/update", window_id=s, solar={"max_elevation": 45})
    await r.api.ok("modes/change", ops=[{"op": "assign", "mode": "glare", "window_id": s, "preset_id": "p_half_shade"},
                                        {"op": "assign", "mode": "heat", "window_id": s,
                                         "preset_id": "p_evening_privacy"}])
    await enable(r.api, s)
    await hass.async_block_till_done()
    assert r.condition(s) == "sun" and r.mgr.auto.runtimes[s].thermal == "warm"
    r.jitter = False                                                  # from now on the sensor is stuck
    await r.walk(1)
    since = r.mgr.auto.value_since[LUX][1]
    await r.walk(59)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    mgr = entry.runtime_data.curtains
    r.mgr = mgr
    w = mgr.config.windows[s]
    assert w.solar.max_elevation == 45.0 and mgr.config.assignment("glare", s, None).preset_id == "p_half_shade"
    assert mgr.config.assignment("heat", s, None).preset_id == "p_evening_privacy"
    v, _ = mgr.config.rules_for(w)
    assert v["thermal_enabled"] and v["glare_sensitive"] and v["glare_when"] == "occupied"
    assert v["room_temp_entity"] == ROOM_T and mgr.auto.runtimes[s].thermal == "warm"
    # A restart gives the sensor a new last_changed; the stuck detection keeps the original start.
    hass.states.async_remove(LUX)
    r.lux(25000)
    assert mgr.auto.value_since[LUX][1].replace(microsecond=0) == since.replace(microsecond=0)
    await r.walk(62)
    assert r.auto(s)["environment"]["readings"]["outdoor_lux"]["status"] == "stuck"
    await mgr.async_flush()


async def test_upgrade_from_0_6_2_keeps_every_window_as_it_was(hass, pilot, house, covers, admin_ws, freezer,
                                                               faroe, hass_storage) -> None:
    """A 0.6.2 store (minor version 2) loads unchanged: the same window in Automatic decides and moves exactly
    as before (no temperature, glare or weather behaviour) and is saved again with only the new defaults."""
    window = {"window_id": "w_stova", "name": "Stova Suður", "area_id": house.stova,
              "layers": [{"layer_id": "l_sheer", "entity_id": STOVA_SHEER, "kind": "sheer", "label": None},
                         {"layer_id": "l_dark", "entity_id": STOVA_BLACKOUT, "kind": "blackout", "label": None}],
              "direction": "S", "bearing": None, "mode": "automatic", "solar_protection": True, "privacy": True,
              "sleep_protection": False,
              "solar": {"exposure_width": 70, "min_elevation": 3.0, "obstructions": []},
              "presets": [{"preset_id": "p_open", "name": "Fully open", "positions": {"l_sheer": 100, "l_dark": 100}},
                          {"preset_id": "p_full_shade", "name": "Full shade",
                           "positions": {"l_sheer": 0, "l_dark": 100}}],
              "selected_preset": None,
              "conditions": {"morning": None, "day": "p_open", "sun": "p_full_shade", "evening_privacy": None,
                             "night": None, "sleep": None, "lamp": None, "manual_override": None},
              "auto": {"min_interval": 0}, "allowed_automations": []}
    stored = {"windows": [window], "settings": {"tolerance": 3, "verify_timeout": 120,
                                                "sun_angle_entity": "sensor.sun_angle", "azimuth_entity": "sun.sun",
                                                "elevation_entity": "sun.sun"},
              "defaults": {"lux_entity": LUX, "night_start_workday": "22:30"}, "rooms": {}}
    hass_storage["smart_occupancy.curtains"] = {"version": 1, "minor_version": 2, "key": "smart_occupancy.curtains",
                                                "data": stored}
    hass_storage["smart_occupancy.curtains_state"] = {
        "version": 1, "minor_version": 1, "key": "smart_occupancy.curtains_state",
        "data": {"last": {}, "history": {}, "manual": {}, "samples": [],
                 "auto": {"w_stova": {"hold": None, "active_since": {}, "last_base": "day", "applied": None,
                                      "moves": {}, "status": {}, "log": []}}}}
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 100)
    _e, base = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T12:05:00+00:00", lux=25000,
                           elevation=9.0, azimuth=178.0)
    r = SolarRun(base)
    hass.states.async_set(ROOM_T, "18.0", {"unit_of_measurement": "°C"})   # present, but not chosen
    w = r.mgr.config.windows["w_stova"]
    v, sources = r.mgr.config.rules_for(w)
    assert w.mode == "automatic" and not v["thermal_enabled"] and not v["glare_sensitive"]
    assert v["sun_evidence"] == "measured" and v["room_temp_entity"] is None and sources["thermal_enabled"] == "default"
    assert w.conditions["glare"] is None and w.conditions["heat"] is None and w.solar.max_elevation is None
    await r.settle()
    await r.walk(11)
    d = r.auto("w_stova")["decision"]
    assert (d["condition"], d["preset_id"]) == ("sun", "p_full_shade")       # as 0.6.2 decided
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (0, 100) and len(covers.calls) == 1
    assert [x["key"] for x in r.auto("w_stova")["precedence"]] == [
        "safety", "manual_override", "sleep", "night", "evening_privacy", "lamp", "sun", "morning", "day", "waiting"]
    await r.mgr.async_flush()
    saved = hass_storage["smart_occupancy.curtains"]
    assert saved["minor_version"] == 4                                 # stored again, data as it was
    new = CurtainConfig.from_dict(saved["data"]).as_dict()["windows"][0]
    assert new["conditions"] == {**window["conditions"], "glare": None, "heat": None}
    assert new["solar"] == {**window["solar"], "max_elevation": None}
    # 0.6.4: every cover gets its own starting presets; no cover override and no Global Modes assignment.
    assert [{k: v for k, v in x.items() if k != "presets"} for x in new["layers"]] == window["layers"]
    assert all([p["position"] for p in x["presets"]] == [100, 50, 0] for x in new["layers"])
    assert new["cover_modes"] == {} and not saved["data"].get("modes") and not saved["data"].get("room_modes")
    skip = ("conditions", "solar", "layers", "cover_modes")
    assert {k: v for k, v in new.items() if k not in skip} == {k: v for k, v in window.items() if k not in skip}
    assert saved["data"]["defaults"] == stored["defaults"] and saved["data"]["settings"] == stored["settings"]
    assert covers.calls[0].data == {"entity_id": STOVA_SHEER, "position": 0}
