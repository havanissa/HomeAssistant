"""Pure unit tests of the occupancy engine and the helper comparison.

Time is explicit, so every rule is tested deterministically. The scenarios
use the Kamar Norður pilot layout: PIR (trigger), radar (maintain), PC
activity (maintain), door (trigger) and an adjacent hallway.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.smart_occupancy.comparison import ComparisonTracker
from custom_components.smart_occupancy.const import OccupancyState as S, SensorKind, SensorRole
from custom_components.smart_occupancy.engine import RoomEngine, SignalStatus, Value, interpret
from custom_components.smart_occupancy.models import OccupancyTiming, SensorAssignment

T0 = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
PIR, RADAR, PC, DOOR, BED = (
    "binary_sensor.pir_kan_occupancy", "binary_sensor.rdr_kno_occupancy",
    "binary_sensor.nobara_pc_active", "binary_sensor.swh_kno_contact", "binary_sensor.bed",
)
ON, OFF, UNK = Value.ACTIVE, Value.INACTIVE, Value.UNKNOWN


def t(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def pilot(radar_role: SensorRole = SensorRole.MAINTAIN, **timing) -> RoomEngine:
    sensors = (
        SensorAssignment(PIR, SensorKind.PIR, SensorRole.TRIGGER),
        SensorAssignment(RADAR, SensorKind.RADAR, radar_role),
        SensorAssignment(PC, SensorKind.PC_ACTIVITY, SensorRole.MAINTAIN),
        SensorAssignment(DOOR, SensorKind.DOOR, SensorRole.TRIGGER),
    )
    engine = RoomEngine("kamar_nordur", sensors, OccupancyTiming(startup_hold=0, **timing), adjacent=("hall",))
    for entity_id in (PIR, RADAR, PC, DOOR):
        engine.observe(entity_id, OFF, t(-3600))
    engine.start(t(-3600))
    assert run(engine, -3600).state == S.VACANT
    return engine


def run(engine: RoomEngine, at: float):
    """Evaluate at ``at``, first replaying every timer deadline before it.

    This is exactly what OccupancyManager does: it evaluates after each
    event and schedules a callback at ``next_deadline``.
    """
    now = t(at)
    last = getattr(engine, "_test_now", None)
    while last is not None and (deadline := engine.next_deadline(last)) is not None and deadline < now:
        engine.evaluate(deadline)
        last = deadline
    engine._test_now = now
    return engine.evaluate(now)


def feed(engine: RoomEngine, entity_id: str, value: Value, at: float):
    """An event: observe, then evaluate (as the manager does)."""
    if getattr(engine, "_test_now", None) and t(at) > engine._test_now:
        run(engine, at)  # timers that fire before this event
    engine.observe(entity_id, value, t(at))
    return run(engine, at)


# ----------------------------------------------------------------- values
@pytest.mark.parametrize(("state", "inverted", "expected"), [
    ("on", False, ON), ("off", False, OFF), ("unavailable", False, UNK), ("unknown", False, UNK),
    (None, False, UNK), ("playing", False, ON), ("idle", False, OFF), ("Active", False, ON),
    ("42.5", False, UNK), ("weird", False, UNK), ("on", True, OFF), ("off", True, ON),
    ("unavailable", True, UNK), ("home", False, ON), ("not_home", False, OFF),
])
def test_interpret(state, inverted, expected) -> None:
    assert interpret(state, inverted=inverted) == expected


# ------------------------------------------------------------ pilot rules
def test_1_pir_gives_immediate_possible_then_confirmed_occupancy() -> None:
    e = pilot()
    feed(e, PIR, ON, 0)
    r = run(e, 0)
    assert (r.state, r.occupied) == (S.POSSIBLE_OCCUPANCY, True)
    assert e.next_deadline(t(0)) == t(10)  # correlation window
    r = run(e, 10)
    assert r.state == S.CONFIRMED_OCCUPANCY  # sustained motion


def test_pir_and_radar_within_window_confirm_at_once() -> None:
    e = pilot()
    feed(e, PIR, ON, 0)
    feed(e, RADAR, ON, 3)
    r = run(e, 3)
    assert r.state == S.CONFIRMED_OCCUPANCY
    assert r.confidence.score == 100  # two signals, presence active, all sensors healthy


def test_2_radar_maintains_after_pir_clears() -> None:
    e = pilot()
    feed(e, PIR, ON, 0); feed(e, RADAR, ON, 2)
    feed(e, PIR, OFF, 60)
    r = run(e, 3600)
    assert r.state == S.CONFIRMED_OCCUPANCY and r.vacancy_at is None


def test_3_4_pc_activity_blocks_vacancy_while_radar_clears() -> None:
    e = pilot()
    feed(e, PIR, ON, 0); feed(e, RADAR, ON, 1); feed(e, PC, ON, 30)
    feed(e, PIR, OFF, 90); feed(e, RADAR, OFF, 300)
    for at in (300, 331, 1200, 7200):
        r = run(e, at)
        assert (r.state, r.occupied) == (S.CONFIRMED_OCCUPANCY, True), at
        assert r.reason.startswith("Occupied: ") and "nobara" in r.reason
    assert [v.track.entity_id for v in r.views(SignalStatus.ACTIVE)] == [PC]


def test_5_6_vacancy_after_maintain_signals_end_respecting_timeouts() -> None:
    e = pilot()
    feed(e, PIR, ON, 0); feed(e, RADAR, ON, 1)
    feed(e, PIR, OFF, 60); feed(e, RADAR, OFF, 100)
    r = run(e, 110)
    assert r.state == S.CONFIRMED_OCCUPANCY  # radar dropout grace (30 s)
    r = run(e, 130)
    assert (r.state, r.occupied) == (S.POSSIBLE_VACANCY, True)
    assert r.quiet_since == t(100) and r.required_quiet == 600
    assert r.vacancy_at == t(700)
    # A recent trigger restarts the quiet period.
    feed(e, PIR, ON, 400); feed(e, PIR, OFF, 460)
    r = run(e, 460)
    assert r.state == S.CONFIRMED_OCCUPANCY or r.vacancy_at == t(1060)
    r = run(e, 461)
    assert r.state == S.POSSIBLE_VACANCY and r.vacancy_at == t(1060)
    assert run(e, 1059).occupied is True
    r = run(e, 1060)
    assert (r.state, r.occupied) == (S.VACANT, False)


def test_7_stuck_pir_does_not_hold_occupancy_forever() -> None:
    e = pilot(trigger_stuck_after=1800)
    feed(e, PIR, ON, 0)
    assert run(e, 1799).state == S.CONFIRMED_OCCUPANCY
    r = run(e, 1800)
    assert r.state == S.POSSIBLE_VACANCY
    assert r.names(SignalStatus.STUCK) == [PIR]
    assert "stuck" in r.reason
    assert r.vacancy_at == t(1800 + 600)
    r = run(e, 2400)
    assert (r.state, r.occupied) == (S.VACANT, False)
    # The PIR clearing and triggering again works normally afterwards.
    feed(e, PIR, OFF, 2500); feed(e, PIR, ON, 2600)
    assert run(e, 2600).occupied is True


def test_8_unavailable_is_not_negative_evidence() -> None:
    e = pilot()
    feed(e, PIR, ON, 0); feed(e, RADAR, ON, 1); feed(e, PIR, OFF, 60)
    feed(e, RADAR, UNK, 100)  # radar drops off the network while reporting presence
    r = run(e, 200)
    assert r.state == S.POSSIBLE_VACANCY and r.occupied is True
    # Held for an extra inactivity timeout, then the normal (long) quiet period.
    assert r.vacancy_at == t(100 + 600 + 600)
    assert "became unavailable while reporting presence" in r.reason
    assert run(e, 1299).occupied is True
    assert run(e, 1300).occupied is False


def test_presence_reconnecting_clear_restarts_quiet_period() -> None:
    e = pilot()
    feed(e, RADAR, ON, 0)
    feed(e, RADAR, UNK, 100)
    r = feed(e, RADAR, OFF, 400)  # reconnects, reports clear
    assert r.occupied is True
    r = run(e, 431)
    assert r.state == S.POSSIBLE_VACANCY and r.quiet_since == t(400) and r.vacancy_at == t(1000)


def test_unavailable_presence_forces_long_path_even_with_confirmers() -> None:
    e = pilot(radar_role=SensorRole.CONFIRM_VACANCY)
    feed(e, PIR, ON, 0); feed(e, PIR, OFF, 60)
    feed(e, PC, UNK, 5)
    r = run(e, 61)
    assert r.required_quiet == 600 and r.vacancy_evidence == ()


def test_all_sensors_unavailable_keeps_last_state() -> None:
    e = pilot()
    feed(e, PIR, ON, 0); feed(e, RADAR, ON, 1)
    run(e, 20)
    for entity_id in (PIR, RADAR, PC):
        feed(e, entity_id, UNK, 30)
    for at in (30, 5000, 86400):
        r = run(e, at)
        assert (r.state, r.occupied, r.usable) == (S.UNCERTAIN, True, False)
    assert r.confidence.score == 0
    # Recovery: quiet period restarts at recovery, so the outage cannot cause instant vacancy.
    for entity_id in (PIR, RADAR, PC):
        feed(e, entity_id, OFF, 90000)
    assert run(e, 90000).occupied is True  # the reconnected radar gets its dropout grace
    r = run(e, 90031)
    assert r.state == S.POSSIBLE_VACANCY and r.vacancy_at == t(90000 + 600)


def test_radar_dropouts_do_not_toggle() -> None:
    e = pilot()
    feed(e, RADAR, ON, 0)
    states = []
    for i in range(20):  # radar flickers off for 5-20 s every minute
        feed(e, RADAR, OFF, 60 * i + 30); states.append(run(e, 60 * i + 30).state)
        feed(e, RADAR, ON, 60 * i + 30 + 5 + i % 15); states.append(run(e, 60 * i + 35 + i % 15).state)
    assert set(states[1:]) == {S.CONFIRMED_OCCUPANCY}


def test_confirm_vacancy_role_gives_fast_exit() -> None:
    e = pilot(radar_role=SensorRole.CONFIRM_VACANCY)
    feed(e, RADAR, ON, 0); feed(e, PIR, ON, 1); feed(e, PIR, OFF, 60)
    feed(e, RADAR, OFF, 120)
    r = run(e, 150)
    assert r.state == S.POSSIBLE_VACANCY and r.required_quiet == 300
    assert r.vacancy_evidence and "cleared" in r.vacancy_evidence[0]
    assert run(e, 420).occupied is False


def test_door_alone_never_occupies() -> None:
    e = pilot()
    feed(e, DOOR, ON, 0)
    r = run(e, 0)
    assert (r.state, r.occupied) == (S.POSSIBLE_OCCUPANCY, False)
    assert e.next_deadline(t(0)) == t(10)
    r = run(e, 10)
    assert (r.state, r.occupied) == (S.VACANT, False)


def test_door_then_pir_confirms_entry() -> None:
    e = pilot()
    feed(e, DOOR, ON, 0); feed(e, PIR, ON, 3)
    assert run(e, 3).state == S.CONFIRMED_OCCUPANCY


def test_door_exit_with_adjacent_motion_uses_exit_confirmation() -> None:
    e = pilot()
    feed(e, PIR, ON, 0); feed(e, RADAR, ON, 1)
    feed(e, PIR, OFF, 600); feed(e, PIR, ON, 900)   # walks to the door
    feed(e, DOOR, ON, 905)                                 # opens the door and leaves
    e.note_neighbour_motion("hall", t(908))
    feed(e, RADAR, OFF, 910); feed(e, PIR, OFF, 960)
    r = run(e, 991)
    assert r.state == S.POSSIBLE_VACANCY and r.required_quiet == 300
    assert "adjacent room" in r.vacancy_evidence[0]
    assert run(e, 960 + 300).occupied is False


def test_activity_after_door_cancels_exit_evidence() -> None:
    e = pilot()
    feed(e, PIR, ON, 0); feed(e, RADAR, ON, 1)
    feed(e, DOOR, ON, 100)
    feed(e, PIR, OFF, 160); feed(e, PIR, ON, 200); feed(e, PIR, OFF, 260)
    feed(e, RADAR, OFF, 300)
    r = run(e, 331)
    assert r.required_quiet == 600 and r.vacancy_evidence == ()


def test_uncorroborated_blip_expires_after_exit_confirmation() -> None:
    e = pilot()
    feed(e, PIR, ON, 0); feed(e, PIR, OFF, 2)
    r = run(e, 2)
    assert r.state == S.POSSIBLE_VACANCY and r.required_quiet == 300
    assert "never confirmed" in r.vacancy_evidence[-1]
    assert run(e, 302).occupied is False


def test_multiple_presence_sensors_any_active_holds() -> None:
    sensors = (
        SensorAssignment(RADAR, SensorKind.RADAR, SensorRole.MAINTAIN),
        SensorAssignment("binary_sensor.radar2", SensorKind.RADAR, SensorRole.MAINTAIN),
        SensorAssignment(BED, SensorKind.BED, SensorRole.MAINTAIN),
    )
    e = RoomEngine("r", sensors, OccupancyTiming(startup_hold=0))
    for s in sensors:
        feed(e, s.entity_id, OFF, -100)
    feed(e, RADAR, ON, 0); feed(e, "binary_sensor.radar2", ON, 1); feed(e, BED, ON, 2)
    assert run(e, 20).state == S.CONFIRMED_OCCUPANCY
    feed(e, RADAR, OFF, 30); feed(e, "binary_sensor.radar2", OFF, 31)
    assert run(e, 3600).state == S.CONFIRMED_OCCUPANCY  # bed holds


def test_inverted_signal() -> None:
    idle = SensorAssignment("binary_sensor.pc_idle", SensorKind.PC_ACTIVITY, SensorRole.MAINTAIN, inverted=True)
    e = RoomEngine("r", (idle,), OccupancyTiming(startup_hold=0))
    e.observe(idle.entity_id, interpret("off", inverted=True), t(0))
    assert run(e, 15).state == S.CONFIRMED_OCCUPANCY


def test_out_of_order_observation_is_ignored() -> None:
    e = pilot()
    feed(e, RADAR, ON, 100)
    assert e.observe(RADAR, OFF, t(50)) is False  # older than the current value
    assert e.tracks[RADAR].value == ON
    assert run(e, 200).state == S.CONFIRMED_OCCUPANCY


def test_restart_restores_occupied_conservatively() -> None:
    e = pilot()
    feed(e, RADAR, ON, 0); run(e, 20)
    saved = e.persist()
    assert saved["occupied"] is True

    # Restart 2 h later; sensors still loading.
    restarted = pilot()
    restarted.timing = OccupancyTiming(startup_hold=120)
    for entity_id in (PIR, RADAR, PC, DOOR):
        restarted.tracks[entity_id].value = UNK
        restarted.tracks[entity_id].since = None
    restarted.restore(saved, t(7200))
    r = restarted.evaluate(t(7200))
    assert (r.state, r.occupied) == (S.UNCERTAIN, True)
    # Sensors report "off": still no vacancy before the hold and a full quiet period from restart.
    for entity_id in (PIR, RADAR, PC, DOOR):
        feed(restarted, entity_id, OFF, 7230)
    r = restarted.evaluate(t(7230))
    assert r.state == S.POSSIBLE_VACANCY and r.starting_up
    assert r.vacancy_at == t(7230 + 600)
    assert restarted.evaluate(t(7829)).occupied is True
    assert restarted.evaluate(t(7830)).occupied is False


def test_restart_vacant_room_stays_vacant() -> None:
    e = pilot()
    saved = e.persist()
    restarted = pilot()
    restarted.restore(saved, t(100))
    assert restarted.evaluate(t(100)).state == S.VACANT


def test_next_deadline_covers_all_timers() -> None:
    e = pilot()
    assert e.next_deadline(t(0)) is None  # vacant and quiet: no timer at all
    feed(e, RADAR, ON, 0); run(e, 0)
    assert e.next_deadline(t(0)) == t(10)
    run(e, 10)
    assert e.next_deadline(t(10)) is None  # occupied by presence: nothing time-based pending
    feed(e, RADAR, OFF, 100); run(e, 100)
    assert e.next_deadline(t(100)) == t(130)  # dropout grace
    run(e, 130)
    assert e.next_deadline(t(130)) == t(700)  # vacancy confirmation


def test_reason_is_bounded() -> None:
    sensors = tuple(
        SensorAssignment(f"binary_sensor.{'x' * 60}_{i}", SensorKind.RADAR, SensorRole.MAINTAIN) for i in range(10)
    )
    e = RoomEngine("r", sensors, OccupancyTiming(startup_hold=0))
    for s in sensors:
        feed(e, s.entity_id, ON, 0)
    assert len(run(e, 20).reason) <= 255


# ------------------------------------------------------------- comparison
def test_comparison_premature_clear_and_agreement() -> None:
    c = ComparisonTracker()
    c.update(t(0), True, True, t(0))
    assert c.agreement is True and c.disagreements == 0
    c.update(t(60), True, False, t(0))     # helper clears, engine still occupied
    assert c.agreement is False and c.disagreements == 1 and c.episode_open
    assert c.reported_agreement is False
    c.update(t(120), True, False, t(110))  # new activity: helper was premature
    assert c.premature_clears == 1 and not c.episode_open
    c.update(t(180), True, True, t(110))
    assert c.agreement is True
    assert c.as_dict()["total_disagreement_seconds"] == 120


def test_comparison_over_retention() -> None:
    c = ComparisonTracker()
    c.update(t(0), True, True, t(0))
    c.update(t(100), True, False, t(0))
    c.update(t(700), False, False, t(0))   # engine vacates without new activity
    assert c.over_retentions == 1 and c.max_over_retention_seconds == 600
    assert c.agreement is True


def test_comparison_brief_lag_is_not_a_disagreement() -> None:
    c = ComparisonTracker(grace_seconds=10)
    c.update(t(0), False, False, None)
    c.update(t(1), True, False, t(1))     # engine reacts first...
    assert c.next_deadline() == t(11)
    c.update(t(1.2), True, True, t(1))    # ...the automation follows 200 ms later
    assert c.disagreements == 0 and c.next_deadline() is None
    c.update(t(100), True, False, t(1))
    c.update(t(110), True, False, t(1))   # timer at the deadline
    assert c.disagreements == 1 and c.as_dict()["disagreement_since"] == t(100).isoformat()


def test_comparison_helper_only_and_unknown() -> None:
    c = ComparisonTracker()
    c.update(t(0), False, True, None)
    assert c.helper_only_occupied == 1 and c.agreement is False
    c.update(t(10), False, None, None)
    assert c.agreement is None and c.disagreement_since is None


def test_comparison_persistence_round_trip() -> None:
    c = ComparisonTracker()
    c.update(t(0), True, True, t(0)); c.update(t(10), True, False, t(0)); c.update(t(20), True, True, t(15))
    restored = ComparisonTracker.restore(c.persist())
    assert restored.premature_clears == c.premature_clears == 1
    assert restored.disagreements == 1 and restored.since == c.since
    assert ComparisonTracker.restore({"disagreements": "x"}).disagreements == 0
