"""Daylight logic (pure): readings, contamination, estimation, hysteresis, gate, configuration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from homeassistant.core import State

from custom_components.smart_occupancy.const import AutoOn, Darkness, Reliability, ReportMode, UncertainPolicy
from custom_components.smart_occupancy.daylight import (
    BRIGHT,
    DARK,
    GATE_NEVER,
    GATE_NOT_NEEDED,
    GATE_UNCERTAIN,
    HIGH,
    MEDIUM,
    NO_OUTDOOR,
    UNCERTAIN,
    Calibration,
    DarknessTracker,
    DaylightConfig,
    DaylightGate,
    IndoorSensor,
    LightEvidence,
    OutdoorFilter,
    OutdoorReading,
    RoomDaylight,
    SunInfo,
    TrustedSample,
    classify,
    contamination,
    estimate_daylight,
    light_evidence,
    parse_lux,
    read_sensor,
    round_lux,
)
from custom_components.smart_occupancy.preliminary import PreliminaryDefaults
from custom_components.smart_occupancy.lighting_config import LightingConfig, migrate_store

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
SENSOR = "sensor.fp2_light"
PIR = "sensor.pir_illuminance"
DAY = SunInfo(30.0)
NIGHT = SunInfo(-20.0)
TWILIGHT = SunInfo(-3.0)
NO_SUN = SunInfo()


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def lux_state(value, *, changed: float = 0, reported: float | None = None, entity: str = SENSOR) -> State:
    return State(entity, str(value), {"friendly_name": entity.split(".")[1], "unit_of_measurement": "lx"},
                 last_changed=at(changed), last_reported=at(reported if reported is not None else changed),
                 last_updated=at(changed))


def light(entity: str, state: str, changed: float = 0) -> tuple[str, State]:
    return entity, State(entity, state, {}, last_changed=at(changed), last_updated=at(changed))


def outdoor(value: float | None) -> OutdoorReading:
    if value is None:
        return NO_OUTDOOR
    return OutdoorReading("sensor.outdoor", value, value, "ok", f"{value} lx")


def run(room: RoomDaylight, states: list[State | None], lights: LightEvidence | None = None, *, now: float,
        sun: SunInfo = DAY, out: OutdoorReading = NO_OUTDOOR, trusted: TrustedSample | None = None,
        factor: float | None = None):
    readings = [read_sensor(s, cfg, i, at(now)) for i, (s, cfg) in enumerate(zip(states, room.sensors, strict=True))]
    return estimate_daylight(room=room, glob=DaylightConfig(), readings=readings, lights=lights or LightEvidence(),
                             outdoor=out, sun=sun, trusted=trusted, learned_factor=factor, now=at(now))


ROOM = RoomDaylight(sensors=(IndoorSensor(SENSOR),))


# ---------------------------------------------------------------- readings
@pytest.mark.parametrize(("raw", "status"), [
    ("unknown", "unknown"), ("unavailable", "unavailable"), ("abc", "invalid"), ("-3", "invalid"),
    ("900000", "invalid"), ("nan", "invalid"),
])
def test_unusable_states_are_never_zero_lux(raw: str, status: str) -> None:
    value, got, _ = parse_lux(State(SENSOR, raw))
    assert value is None and got == status
    assert parse_lux(None)[1] == "missing"
    assert parse_lux(State(SENSOR, "0"))[0] == 0.0


def test_report_mode_decides_when_a_value_was_measured() -> None:
    state = lux_state(40, changed=0, reported=500)
    changes = read_sensor(state, IndoorSensor(SENSOR), 0, at(600))
    reports = read_sensor(state, IndoorSensor(SENSOR, report_mode=ReportMode.REPORTS), 0, at(600))
    assert changes.sample_at == at(0) and reports.sample_at == at(500)
    assert changes.reported_at == reports.reported_at == at(500)


def test_stable_value_is_not_stale_without_a_limit_but_is_with_one() -> None:
    state = lux_state(12, changed=0, reported=0)
    assert read_sensor(state, IndoorSensor(SENSOR), 0, at(8 * 3600)).usable  # stable for 8 h, still fine
    stale = read_sensor(state, IndoorSensor(SENSOR, max_age=60), 0, at(2 * 3600))
    assert stale.status == "stale" and not stale.usable and "120 min" in stale.detail
    # A recent report of an unchanged value keeps it fresh.
    assert read_sensor(lux_state(12, changed=0, reported=7000), IndoorSensor(SENSOR, max_age=60), 0, at(7200)).usable


def test_light_evidence_and_contamination() -> None:
    ev = light_evidence([light("light.a", "off", 10), light("light.b", "off", 50), ("light.gone", None)])
    assert not ev.lit and ev.off_since == at(50)
    assert contamination(ev, 60, at(100)).state == "settling"
    assert contamination(ev, 60, at(111)).state == "clear"
    ev = light_evidence([light("light.a", "on", 10), light("light.b", "off", 50)])
    assert ev.lit and contamination(ev, 60, at(500)).state == "lit"
    ev = light_evidence([light("light.a", "unavailable", 10)])
    c = contamination(ev, 60, at(500))
    assert c.state == "possible" and not c.contaminated and "cannot be ruled out" in c.text


# ---------------------------------------------------------- contamination
def test_dark_room_lights_off_measured_indoors() -> None:
    r = run(ROOM, [lux_state(35, changed=100)], light_evidence([light("light.a", "off", 0)]), now=200)
    assert r.estimate.basis == "indoor" and r.estimate.measured and r.estimate.confidence == HIGH
    assert r.estimate.text == "Natural light: 35 lx — measured indoors, lights off"
    assert r.accepted == TrustedSample(SENSOR, 35.0, at(100), None)
    assert classify(r.estimate, ROOM, None) == DARK


def test_bright_reading_with_the_lights_on_is_never_daylight() -> None:
    lights = light_evidence([light("light.a", "on", 50)])
    r = run(ROOM, [lux_state(450, changed=60)], lights, now=120)
    assert r.contamination.state == "lit" and r.accepted is None
    assert r.estimate.lux is None and classify(r.estimate, ROOM, None) == UNCERTAIN
    assert "Indoor reading not used as daylight: Lights on" in " ".join(r.estimate.notes)


def test_contaminated_reading_falls_back_to_last_trusted_sample() -> None:
    lights = light_evidence([light("light.a", "on", 50)])
    trusted = TrustedSample(SENSOR, 30.0, at(0))
    r = run(ROOM, [lux_state(450, changed=60)], lights, now=600, trusted=trusted)
    assert r.estimate.basis == "recent_indoor" and r.estimate.lux == 30.0
    assert "measured indoors 10 min ago, lights off" in r.estimate.text
    # ... and to the outdoor light when it is too old.
    r = run(ROOM, [lux_state(450, changed=60)], lights, now=3 * 3600, trusted=trusted, out=outdoor(20))
    assert r.estimate.basis == "outdoor_dark"


def test_dark_reading_with_lights_on_proves_darkness() -> None:
    lights = light_evidence([light("light.a", "on", 50)])
    r = run(ROOM, [lux_state(20, changed=60)], lights, now=120)
    assert r.estimate.basis == "indoor_bound" and r.estimate.upper_bound
    assert "at most 20 lx — measured indoors with lights on" in r.estimate.text
    assert classify(r.estimate, ROOM, BRIGHT) == DARK
    assert r.accepted is None  # never a trusted sample


def test_settling_after_lights_off_and_fresh_sample_rule() -> None:
    off = light_evidence([light("light.a", "off", 100)])
    # The lamp went off at 100 s; the reading changed at 101 s (still fading): not trusted.
    r = run(ROOM, [lux_state(300, changed=101)], off, now=120)
    assert r.contamination.state == "settling" and r.accepted is None and r.estimate.lux is None
    # After the settling period the same old value is still not a new measurement.
    r = run(ROOM, [lux_state(300, changed=101)], off, now=400)
    assert r.contamination.state == "clear" and r.accepted is None
    assert "before the lights had been off long enough" in " ".join(r.estimate.notes)
    # A new value measured after the settling period is.
    r = run(ROOM, [lux_state(25, changed=200)], off, now=400)
    assert r.accepted == TrustedSample(SENSOR, 25.0, at(200), None) and r.estimate.basis == "indoor"


def test_stale_pir_lux_does_not_become_fresh_when_the_lamp_turns_off() -> None:
    """An infrequently reporting PIR re-publishes its cached lux: only a new value counts (default mode)."""
    room = RoomDaylight(sensors=(IndoorSensor(PIR),))
    off = light_evidence([light("light.lamp", "off", 1000)])
    # Lux 180 was measured with the lamp on (at 900 s) and reported again, unchanged, at 1100 s.
    state = lux_state(180, changed=900, reported=1100, entity=PIR)
    r = run(room, [state], off, now=1200)
    assert r.accepted is None and r.estimate.lux is None
    # With "every report counts" a report after settling would be accepted (for frequently reporting sensors).
    room_reports = RoomDaylight(sensors=(IndoorSensor(PIR, report_mode=ReportMode.REPORTS),))
    assert run(room_reports, [state], off, now=1200).accepted is not None


def test_unknown_influencing_light_lowers_confidence() -> None:
    ev = light_evidence([light("light.a", "unavailable", 0)])
    r = run(ROOM, [lux_state(300, changed=100)], ev, now=200)
    assert r.estimate.basis == "indoor" and r.estimate.confidence == MEDIUM
    assert any("cannot be ruled out" in n for n in r.estimate.notes)


def test_low_reliability_sensor_and_fallback_sensor() -> None:
    room = RoomDaylight(sensors=(IndoorSensor(SENSOR), IndoorSensor(PIR, reliability=Reliability.LOW)))
    r = run(room, [State(SENSOR, "unavailable"), lux_state(300, changed=10, entity=PIR)], now=100)
    assert r.chosen.entity_id == PIR and r.estimate.confidence == MEDIUM
    assert "Preferred sensor not usable; using fallback" in r.estimate.notes[0]


# ---------------------------------------------------------- outdoor & sun
def test_after_dusk_there_is_no_daylight_whatever_the_sensor_reads() -> None:
    r = run(ROOM, [lux_state(400, changed=10)], now=100, sun=NIGHT)
    assert r.estimate.basis == "sun_dark" and r.estimate.lux == 0 and r.estimate.confidence == HIGH
    assert r.estimate.text == "Natural light: Low — estimated from the sun (after dusk)"
    assert any("not listed as influencing" in n for n in r.estimate.notes)
    assert r.accepted is None


def test_outdoor_darkness_fallback() -> None:
    room = RoomDaylight()
    r = run(room, [], now=0, out=outdoor(10), sun=NO_SUN)
    assert r.estimate.basis == "outdoor_dark" and r.estimate.text.endswith("estimated from outdoor darkness")
    assert classify(r.estimate, room, None) == DARK


def test_outdoor_brightness_does_not_make_every_room_bright() -> None:
    room = RoomDaylight()
    r = run(room, [], now=0, out=outdoor(20000))
    assert r.estimate.lux is None and "share is not known" in r.estimate.text
    assert classify(r.estimate, room, None) == UNCERTAIN
    # With a daylight factor (learned or set) it is an estimate; a dim room stays dark.
    r = run(room, [], now=0, out=outdoor(20000), factor=0.001)
    assert r.estimate.basis == "outdoor_factor" and r.estimate.lux == pytest.approx(20)
    assert "≈ 20 lx" in r.estimate.text and not r.estimate.measured
    assert classify(r.estimate, room, None) == DARK
    r = run(RoomDaylight(factor=0.02), [], now=0, out=outdoor(20000))
    assert r.estimate.lux == pytest.approx(400) and "set factor" in r.estimate.text


def test_trusted_sample_is_scaled_by_the_outdoor_change() -> None:
    lights = light_evidence([light("light.a", "on", 50)])
    trusted = TrustedSample(SENSOR, 80.0, at(0), 8000.0)
    r = run(ROOM, [lux_state(500, changed=60)], lights, now=600, out=outdoor(2000), trusted=trusted)
    assert r.estimate.basis == "recent_indoor" and r.estimate.lux == pytest.approx(20)


def test_twilight_without_other_evidence_is_dark_with_medium_confidence() -> None:
    r = run(RoomDaylight(), [], now=0, sun=TWILIGHT)
    assert r.estimate.basis == "sun_down" and r.estimate.confidence == MEDIUM


def test_mixed_availability() -> None:
    room = RoomDaylight(sensors=(IndoorSensor(SENSOR), IndoorSensor(PIR)))
    # Both indoor sensors unavailable, outdoor known.
    r = run(room, [State(SENSOR, "unavailable"), State(PIR, "unknown")], now=0, out=outdoor(15))
    assert r.chosen is None and r.estimate.basis == "outdoor_dark"
    # Indoor fine, outdoor unavailable: indoor decides.
    r = run(room, [lux_state(200), lux_state(5, entity=PIR)], now=100,
            out=OutdoorReading("sensor.outdoor", None, None, "unavailable", "unavailable"))
    assert r.chosen.entity_id == SENSOR and r.estimate.lux == 200
    # Nothing at all.
    r = run(room, [State(SENSOR, "unavailable"), None], now=0, sun=NO_SUN)
    assert r.estimate.lux is None and "no trustworthy reading" in r.estimate.text


def test_outdoor_spikes_are_smoothed() -> None:
    f = OutdoorFilter()
    f.add(at(0), 10000)
    f.add(at(250), 90000)  # spike
    f.add(at(260), 10100)
    assert f.value(at(300), 300) == 10000
    f.add(at(290), 95000)  # a spike right now
    assert f.value(at(295), 300) == 10000
    # A lasting change wins after about half the window.
    f.add(at(400), 500)
    assert f.value(at(500), 300) != 500
    assert f.value(at(560), 300) == 500
    # Out-of-order samples are ignored; an invalid current value gives no value.
    f.add(at(10), 1)
    f.add(at(600), None)
    assert f.value(at(601), 300) is None
    assert OutdoorFilter().value(at(0), 300) is None


def test_calibration_learns_a_median_factor() -> None:
    c = Calibration()
    assert not c.add(TrustedSample(SENSOR, 50, at(0), 100))  # outdoor too dark to calibrate
    assert c.add(TrustedSample(SENSOR, 100, at(0), 10000))
    assert not c.add(TrustedSample(SENSOR, 900, at(60), 10000))  # too soon after the previous one
    assert c.factor is None  # needs three samples
    c.add(TrustedSample(SENSOR, 200, at(700), 10000))
    c.add(TrustedSample(SENSOR, 9000, at(1400), 10000))  # outlier
    assert c.factor == pytest.approx(0.02)
    assert Calibration.restore(c.persist()).ratios == c.ratios


# --------------------------------------------------------------- decision
def test_hysteresis_between_thresholds() -> None:
    room = RoomDaylight(on_below=50, off_above=100)

    def est(lux):
        return run(room, [], now=0, out=outdoor(lux / 0.01), factor=0.01).estimate

    assert classify(est(30), room, None) == DARK
    assert classify(est(70), room, DARK) == DARK      # between: keeps dark
    assert classify(est(70), room, BRIGHT) == BRIGHT  # between: keeps bright
    assert classify(est(70), room, None) == BRIGHT    # first decision: not dark enough
    assert classify(est(120), room, DARK) == BRIGHT


def test_stability_delays_committed_changes() -> None:
    t = DarknessTracker()
    assert t.update(BRIGHT, at(0), 90) is None and t.committed == BRIGHT  # first decision commits at once
    assert t.update(DARK, at(10), 90) is None and t.committed == BRIGHT
    assert t.due(90) == at(100)
    assert t.update(BRIGHT, at(30), 90) is None and t.due(90) is None  # flicker back: reset
    t.update(DARK, at(40), 90)
    assert t.update(DARK, at(129), 90) is None
    assert t.update(DARK, at(130), 90) == BRIGHT and t.committed == DARK


def test_gate() -> None:
    def gate(policy, decision, uncertain=UncertainPolicy.TURN_ON):
        return DaylightGate(policy, decision, uncertain, "Natural light: x").blocks()

    assert gate(AutoOn.DARK, Darkness.DARK) is None
    assert gate(AutoOn.DARK, Darkness.BRIGHT)[0] == GATE_NOT_NEEDED
    assert gate(AutoOn.DARK, Darkness.UNCERTAIN) is None
    assert gate(AutoOn.DARK, Darkness.UNCERTAIN, UncertainPolicy.STAY_OFF)[0] == GATE_UNCERTAIN
    assert gate(AutoOn.ALWAYS, Darkness.BRIGHT) is None
    assert gate(AutoOn.NEVER, Darkness.DARK)[0] == GATE_NEVER


# ---------------------------------------------------------- configuration
def test_room_daylight_config_is_validated() -> None:
    cfg = RoomDaylight.from_dict({
        "sensors": [{"entity_id": SENSOR, "max_age": 99999}, SENSOR, {"entity_id": "bad"}, {"entity_id": PIR}],
        "on_below": 120, "off_above": 80, "stable_seconds": -5, "factor": 7, "auto_on": "nonsense",
        "influencing": ["light.a", "light.a", "x"], "uncertain": "stay_off",
    })
    assert [s.entity_id for s in cfg.sensors] == [SENSOR, PIR]
    assert cfg.sensors[0].max_age == 1440
    assert cfg.off_above == 240 and cfg.stable_seconds == 0 and cfg.factor == 1.0
    assert cfg.auto_on == AutoOn.DARK and cfg.uncertain == UncertainPolicy.STAY_OFF
    assert cfg.influencing == ("light.a",)
    assert RoomDaylight.from_dict(cfg.as_dict()) == cfg
    assert RoomDaylight.from_dict(None) == RoomDaylight()
    assert DaylightConfig.from_dict({"outdoor_entity": "nope"}).outdoor_entity is None


def test_store_migration_keeps_activated_rooms_on_confirmed_occupancy() -> None:
    data = {"rooms": {"active": {"control_enabled": True}, "observe": {"control_enabled": False},
                      "done": {"control_enabled": True, "daylight": {"on_first_motion": True}}}}
    migrated = migrate_store(1, 1, data)
    config = LightingConfig.from_dict(migrated)
    assert config.room("active").daylight.on_first_motion is False
    assert config.room("observe").daylight.on_first_motion is True
    assert config.room("done").daylight.on_first_motion is True
    assert config.room("active").daylight.auto_on == AutoOn.DARK
    # Later minors only add keys (1.5, 0.3.9: possible occupancy lighting, disabled).
    assert migrate_store(1, 2, {"rooms": {}}) == {"rooms": {}, "preliminary": PreliminaryDefaults().as_dict()}
    with pytest.raises(NotImplementedError):
        migrate_store(2, 1, {})


def test_round_lux() -> None:
    assert [round_lux(v) for v in (3.4, 37, 437, 4370)] == [3, 35, 440, 4400]
    assert round_lux(None) is None


def test_newer_dark_reading_with_lights_on_beats_an_older_bright_sample() -> None:
    """Found on a real instance: a lamp lit the sensor while it got dark; more lamps then raised the reading.

    The older, bright trusted sample must not come back: the newer "at most" reading proves darkness.
    """
    lights = light_evidence([light("light.kitchen", "on", 100)])
    old_bright = TrustedSample(SENSOR, 300.0, at(0))
    r = run(ROOM, [lux_state(12, changed=200)], lights, now=210, trusted=old_bright)
    assert r.estimate.basis == "indoor_bound" and r.bound == TrustedSample(SENSOR, 12.0, at(200), None)
    # More lamps on: the sensor now reads 450 lx.
    r = run(ROOM, [lux_state(450, changed=260)], lights, now=300, trusted=old_bright, out=outdoor(20000))
    assert r.estimate.basis == "recent_indoor"  # without the bound, the old sample would win (bright)
    r = run_with_bound(lights, now=300, trusted=old_bright, bound=TrustedSample(SENSOR, 12.0, at(200)))
    assert r.estimate.basis == "recent_bound" and r.estimate.upper_bound
    assert "at most 10 lx — measured indoors 1 min ago with lights on" in r.estimate.text
    assert classify(r.estimate, ROOM, DARK) == DARK
    # A newer trusted sample (lights off again) takes over.
    r = run_with_bound(light_evidence([light("light.kitchen", "on", 100)]), now=900,
                       trusted=TrustedSample(SENSOR, 200.0, at(800)), bound=TrustedSample(SENSOR, 12.0, at(200)))
    assert r.estimate.basis == "recent_indoor" and r.estimate.lux == 200


def run_with_bound(lights, *, now, trusted, bound):
    readings = [read_sensor(lux_state(450, changed=260), ROOM.sensors[0], 0, at(now))]
    return estimate_daylight(room=ROOM, glob=DaylightConfig(), readings=readings, lights=lights, outdoor=NO_OUTDOOR,
                             sun=DAY, trusted=trusted, learned_factor=None, now=at(now), bound=bound)
