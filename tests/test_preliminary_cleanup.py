"""Possible occupancy lighting (0.3.10): a preliminary light never stays on when occupancy is not confirmed.

Root cause of the 0.3.9 fault: the stage switched a light off only while the
controller still held its own command for it (``last_commands``), and any
light state change that was not recognised as the controller's own erased
that. Real devices report with a context of their own: a member of a light
group (the Kamar Norður spots), or a light reporting late or with a rounded
brightness. The stage then concluded "changed since" and left the light on.

These lights behave like Zigbee2MQTT devices: a command is accepted, then the
device (and each member of a group) reports its new state with a context of
its own, and the brightness is rounded by the device. Nothing here talks to
real devices.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from custom_components.smart_occupancy.const import VacancyAction
from custom_components.smart_occupancy.lighting_config import RoomVacancy
from custom_components.smart_occupancy.preliminary import ConfirmAction, PreliminaryTarget

from .conftest import Pilot, pilot  # noqa: F401
from .test_daylight_runtime import advance
from .test_lighting_runtime import Lit, lit  # noqa: F401
from .test_preliminary import _ignore, log, motion, pilot_room, presence, setup, state


class ZigbeeLights:
    """light.turn_on / turn_off like Zigbee2MQTT: accepted, then reported by the device with its own context."""

    def __init__(self, hass: HomeAssistant, groups: dict[str, list[str]] | None = None) -> None:
        self.hass = hass
        self.groups = groups or {}
        self.calls: list[ServiceCall] = []
        self.fail: set[str] = set()
        hass.services.async_register("light", "turn_on", self._handle)
        hass.services.async_register("light", "turn_off", self._handle)

    @staticmethod
    def _ids(call: ServiceCall) -> list[str]:
        ids = call.data["entity_id"]
        return [ids] if isinstance(ids, str) else list(ids)

    async def _handle(self, call: ServiceCall) -> None:
        self.calls.append(call)
        ids = self._ids(call)
        if failing := [e for e in ids if e in self.fail]:
            raise HomeAssistantError(f"No response from {failing[0]}")
        on = call.service == "turn_on"
        pct = call.data.get("brightness_pct")
        for e in ids:
            for member in self.groups.get(e, []):
                self.report(member, on, pct)
            self.report(e, on, pct)

    def report(self, entity_id: str, on: bool, pct: float | None = None, *, raw: int | None = None) -> None:
        """The device's own state report: a new context, the brightness rounded up by one step."""
        old = self.hass.states.get(entity_id)
        attrs = dict(old.attributes) if old else {}
        if on:
            if raw is not None:
                attrs["brightness"] = raw
            elif pct is not None:
                attrs["brightness"] = min(255, round(pct * 255 / 100) + 1)
            attrs.setdefault("brightness", 255)
        else:
            attrs.pop("brightness", None)
        self.hass.states.async_set(entity_id, "on" if on else "off", attrs, context=Context())

    def clear(self) -> None:
        self.calls.clear()

    def of(self, entity_id: str) -> list[tuple[str, dict[str, Any]]]:
        return [(c.service, {k: v for k, v in c.data.items() if k != "entity_id"})
                for c in self.calls if entity_id in self._ids(c)]


def zigbee(hass: HomeAssistant, lit: Lit) -> ZigbeeLights:
    return ZigbeeLights(hass, {lit.group: [lit.spot_1, lit.spot_2]})


async def blip(hass: HomeAssistant, lit: Lit, freezer, seconds: float = 3) -> None:
    """A short PIR detection that never becomes confirmed occupancy."""
    await motion(hass, lit, freezer, wait=seconds)
    hass.states.async_set(lit.pir, "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 1)


SPOTS_10 = ("turn_on", {"brightness_pct": 10, "transition": 0.5})
SPOTS_OFF = ("turn_off", {"transition": 0.5})


# ------------------------------------------------------------------ timeout without confirmation
async def test_kamar_nordur_spots_go_off_after_the_timeout(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """Kamar Norður: the spots group at 10 %; the members and the group report back like Zigbee2MQTT, and a
    late, rounded report arrives after 25 s. At 30 s the spots are switched off, once."""
    zb = zigbee(hass, lit)
    _entry, ctrl = await setup(hass, lit, freezer, zb, targets=(PreliminaryTarget(lit.group, 10),))
    await blip(hass, lit, freezer)
    assert zb.of(lit.group) == [SPOTS_10] and hass.states.is_state(lit.group, "on")
    start = log(ctrl, lit.area)[0]
    assert start["action"] == "Preliminary light on" and start["status"] == "executed", start
    assert "otherwise it is switched off after 30 s at " in start["explanation"] and start["details"]["deadline"]
    await advance(hass, freezer, 20)
    zb.report(lit.spot_1, True, raw=27)  # a late report of its own: not a change by anyone
    await hass.async_block_till_done()
    await advance(hass, freezer, 7)  # 31 s after the start
    assert zb.of(lit.group) == [SPOTS_10, SPOTS_OFF]
    assert hass.states.is_state(lit.group, "off")
    d = log(ctrl, lit.area)[0]
    assert (d["action"], d["status"], d["trigger"]) == ("Preliminary light off", "executed", "preliminary_end"), d
    assert d["explanation"] == ("Possible occupancy was not confirmed within 30 s (timeout expired): "
                                "light.kno_spots switched off (preliminary light removed).")
    assert d["details"]["cleanup"] == {"light.kno_spots": "off"}
    # Later the room becomes vacant (exit confirmation): the vacancy action sends the spots nothing more.
    await advance(hass, freezer, 400)
    assert state(ctrl, lit.area) == "vacant"
    assert zb.of(lit.group) == [SPOTS_10, SPOTS_OFF]
    assert ctrl.runtime[lit.area].preliminary is None and ctrl.runtime[lit.area].preliminary_cleanup == {}


async def test_kamar_nordur_pilot_end_to_end(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    """The live pilot's entities (PIR, radar, Nobara PC, door): a PIR blip dims the spots; nothing confirms;
    the spots go off at the timeout although the device reported with its own context."""
    zb = ZigbeeLights(hass)
    ctrl = await pilot_room(hass, pilot, freezer)
    zb.clear()
    hass.states.async_set(pilot.pir, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer, 3)
    hass.states.async_set(pilot.pir, "off")
    await hass.async_block_till_done()
    assert zb.of(pilot.light) == [("turn_on", {"brightness_pct": 10})]
    await advance(hass, freezer, 30)
    assert zb.of(pilot.light) == [("turn_on", {"brightness_pct": 10}), ("turn_off", {})]
    assert hass.states.is_state(pilot.light, "off")


async def test_repeated_motion_does_not_extend_the_timeout(hass: HomeAssistant, lit: Lit, freezer) -> None:
    zb = zigbee(hass, lit)
    _entry, ctrl = await setup(hass, lit, freezer, zb, targets=(PreliminaryTarget(lit.group, 10),))
    for _ in range(5):  # short detections, 5 s apart: one sensor never confirms on its own
        await blip(hass, lit, freezer, 1)
        await advance(hass, freezer, 3)
    assert state(ctrl, lit.area) == "possible_vacancy"
    assert zb.of(lit.group) == [SPOTS_10]  # 25 s: still on
    await advance(hass, freezer, 6)
    assert zb.of(lit.group) == [SPOTS_10, SPOTS_OFF]  # 31 s after the first detection, not after the last
    # More motion in the same visit: no second stage, and the normal lighting still waits for confirmation.
    await blip(hass, lit, freezer, 1)
    await advance(hass, freezer, 40)
    assert zb.of(lit.group) == [SPOTS_10, SPOTS_OFF]
    assert [c for c in zb.calls if c.service == "turn_on"] == zb.calls[:1]


async def test_unavailable_sensors_during_the_stage(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """Both sensors drop out (occupancy uncertain): the timeout still removes the preliminary light."""
    zb = zigbee(hass, lit)
    _entry, ctrl = await setup(hass, lit, freezer, zb, targets=(PreliminaryTarget(lit.group, 10),))
    await motion(hass, lit, freezer, wait=3)
    hass.states.async_set(lit.pir, "unavailable")
    hass.states.async_set(lit.radar, "unavailable")
    await hass.async_block_till_done()
    await advance(hass, freezer, 30)
    assert state(ctrl, lit.area) == "uncertain"
    assert zb.of(lit.group) == [SPOTS_10, SPOTS_OFF]


# ------------------------------------------------------------------------------ confirmation
async def test_confirmation_before_the_timeout(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """Hand over (spots) and Turn off (desk lamp, not in the scene) with device reports; the timeout is cancelled."""
    zb = zigbee(hass, lit)
    targets = (PreliminaryTarget(lit.group, 10), PreliminaryTarget(lit.desk, 20, None, None, ConfirmAction.TURN_OFF))
    _entry, ctrl = await setup(hass, lit, freezer, zb, targets=targets)
    ctrl.store.async_set(ctrl.with_room(lit.area, light_overrides={"Dagur": {lit.desk: _ignore()}}))
    await hass.async_block_till_done()
    await motion(hass, lit, freezer)
    zb.report(lit.desk, True, raw=52)  # the device's own late report
    await hass.async_block_till_done()
    zb.clear()
    await presence(hass, lit, freezer)
    assert state(ctrl, lit.area) == "confirmed_occupancy"
    services = [(c.service, c.data["entity_id"]) for c in zb.calls]
    assert services[-1] == ("turn_off", [lit.desk]), services
    assert zb.of(lit.group) == [("turn_on", {"brightness_pct": 100, "color_temp_kelvin": 3500})]
    d = log(ctrl, lit.area)[0]
    assert "(preliminary timeout cancelled)" in d["explanation"] and "light.kno_spots now follows Dagur" in d["explanation"]
    assert "light.kno_desk switched off (preliminary light)" in d["explanation"]
    zb.clear()
    await advance(hass, freezer, 60)  # the cancelled timeout does nothing
    assert zb.calls == []


async def test_confirmation_before_anything_was_switched_on(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """Bright room: the stage switched nothing on; confirmation notes the cancelled timeout on its entry."""
    zb = zigbee(hass, lit)
    _entry, ctrl = await setup(hass, lit, freezer, zb, lux=400, targets=(PreliminaryTarget(lit.group, 10),))
    await motion(hass, lit, freezer)
    await presence(hass, lit, freezer)
    start = next(d for d in log(ctrl, lit.area) if d["kind"] == "preliminary")
    assert start["details"]["ended"].startswith("Occupancy confirmed after ") and "cancelled" in start["details"]["ended"]


# ------------------------------------------------------------------------------ ownership
async def test_a_light_already_on_is_never_switched_off(hass: HomeAssistant, lit: Lit, freezer) -> None:
    zb = zigbee(hass, lit)
    targets = (PreliminaryTarget(lit.lamp, 10), PreliminaryTarget(lit.group, 10))
    _entry, ctrl = await setup(hass, lit, freezer, zb, targets=targets)
    zb.report(lit.lamp, True, 40)  # on before possible occupancy (e.g. a reading lamp)
    await advance(hass, freezer)
    zb.clear()
    await blip(hass, lit, freezer)
    assert zb.of(lit.lamp) == [] and zb.of(lit.group) == [SPOTS_10]
    await advance(hass, freezer, 30)
    assert zb.of(lit.lamp) == [] and hass.states.is_state(lit.lamp, "on")
    assert zb.of(lit.group) == [SPOTS_10, SPOTS_OFF]


async def test_manual_changes_during_the_stage_are_respected(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """The lamp is dimmed up from the app (a person: a recorded manual override) and stays on. The desk lamp is
    dimmed by something unattributed (not recorded as a person's change in this room): it is still the stage's
    and goes off, like the untouched spots."""
    zb = zigbee(hass, lit)
    targets = (PreliminaryTarget(lit.lamp, 10), PreliminaryTarget(lit.desk, 10), PreliminaryTarget(lit.group, 10))
    _entry, ctrl = await setup(hass, lit, freezer, zb, targets=targets)
    await blip(hass, lit, freezer)
    hass.states.async_set(lit.lamp, "on", {**hass.states.get(lit.lamp).attributes, "brightness": 200},
                          context=Context(user_id="person"))
    zb.report(lit.desk, True, raw=180)
    await hass.async_block_till_done()
    await advance(hass, freezer, 30)
    assert [s for s, _ in zb.of(lit.lamp)] == ["turn_on"] and [s for s, _ in zb.of(lit.desk)] == ["turn_on", "turn_off"]
    assert zb.of(lit.group) == [SPOTS_10, SPOTS_OFF]
    d = log(ctrl, lit.area)[0]
    assert "light.kno_desk and light.kno_spots switched off (preliminary light removed)" in d["explanation"], d
    assert "light.kno_lamp left as it is: changed by a person (manual override)" in d["explanation"]


async def test_vacant_mood_is_restored(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """Vacancy behaviour Vacant mood (Nátt: lamps 10 %, downlights off). The desk lamp is a night light that is on
    before; the lamp is the preliminary light. At the timeout only the lamp goes off (it was off in the vacant
    state); at vacancy Nátt applies to the lit desk lamp and sends the lamp nothing."""
    zb = zigbee(hass, lit)
    _entry, ctrl = await setup(hass, lit, freezer, zb, targets=(PreliminaryTarget(lit.lamp, 30),))
    ctrl.store.async_set(ctrl.with_room(lit.area, vacancy=RoomVacancy(VacancyAction.VACANT_MOOD, "Nátt")))
    zb.report(lit.desk, True, 10)
    await hass.async_block_till_done()
    await advance(hass, freezer)
    zb.clear()
    await blip(hass, lit, freezer)
    await advance(hass, freezer, 30)
    assert zb.of(lit.lamp) == [("turn_on", {"brightness_pct": 30, "transition": 0.5}), ("turn_off", {"transition": 0.5})]
    await advance(hass, freezer, 400)
    assert state(ctrl, lit.area) == "vacant"
    assert len(zb.of(lit.lamp)) == 2 and hass.states.is_state(lit.lamp, "off")
    assert hass.states.is_state(lit.desk, "on") and all(s == "turn_on" for s, _ in zb.of(lit.desk))


# ---------------------------------------------------------------- unavailable, failures, modes, restart
async def test_unavailable_light_is_switched_off_when_it_returns(hass: HomeAssistant, lit: Lit, freezer) -> None:
    zb = zigbee(hass, lit)
    _entry, ctrl = await setup(hass, lit, freezer, zb, targets=(PreliminaryTarget(lit.lamp, 10),))
    await blip(hass, lit, freezer)
    hass.states.async_set(lit.lamp, "unavailable", {})
    await hass.async_block_till_done()
    await advance(hass, freezer, 30)
    assert [s for s, _ in zb.of(lit.lamp)] == ["turn_on"]
    d = log(ctrl, lit.area)[0]
    assert (d["status"], d["action"]) == ("failed", "Preliminary light off"), d
    assert "light.kno_lamp unavailable: switched off when it is available again" in d["explanation"]
    assert lit.lamp in ctrl.runtime[lit.area].preliminary_cleanup
    zb.report(lit.lamp, True, 10)  # back, still at the stage's brightness
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert [s for s, _ in zb.of(lit.lamp)] == ["turn_on", "turn_off"]
    d = log(ctrl, lit.area)[0]
    assert d["explanation"].startswith("A preliminary light that was unavailable is available again: "
                                       "light.kno_lamp switched off"), d
    assert ctrl.runtime[lit.area].preliminary_cleanup == {}


async def test_failed_switch_off_is_retried(hass: HomeAssistant, lit: Lit, freezer) -> None:
    zb = zigbee(hass, lit)
    _entry, ctrl = await setup(hass, lit, freezer, zb, targets=(PreliminaryTarget(lit.lamp, 10),))
    await blip(hass, lit, freezer)
    zb.fail.add(lit.lamp)
    await advance(hass, freezer, 30)
    d = log(ctrl, lit.area)[0]
    assert (d["status"], d["action"]) == ("failed", "Preliminary light off"), d
    assert "switch-off failed for light.kno_lamp: No response from light.kno_lamp (tried again in a minute)" \
        in d["explanation"]
    zb.fail.clear()
    await advance(hass, freezer, 61)
    assert [s for s, _ in zb.of(lit.lamp)] == ["turn_on", "turn_off", "turn_off"]
    assert hass.states.is_state(lit.lamp, "off")
    d = log(ctrl, lit.area)[0]
    assert d["explanation"].startswith("Retrying the switch-off of a preliminary light: light.kno_lamp switched off")
    assert ctrl.runtime[lit.area].preliminary_cleanup == {}


async def test_observing_never_sends_and_the_light_is_removed_in_automatic(hass: HomeAssistant, lit: Lit,
                                                                           freezer) -> None:
    """Set to Observing during the stage: nothing is sent at the timeout (explained); the light is still due and is
    switched off once the room is Automatic again."""
    zb = zigbee(hass, lit)
    _entry, ctrl = await setup(hass, lit, freezer, zb, targets=(PreliminaryTarget(lit.group, 10),))
    await blip(hass, lit, freezer)
    ctrl.store.async_set(ctrl.with_room(lit.area, control_enabled=False))
    await hass.async_block_till_done()
    await advance(hass, freezer, 30)
    await advance(hass, freezer, 400)  # vacant: Observing sends nothing for vacancy either
    assert state(ctrl, lit.area) == "vacant"
    assert zb.of(lit.group) == [SPOTS_10]
    ended = next(d for d in log(ctrl, lit.area) if d["trigger"] == "preliminary_end")
    assert (ended["status"], ended["mode"]) == ("blocked", "observing"), ended
    assert "left on: the room is Observing, so nothing is sent; switched off when the room is set to Automatic " \
           "again" in ended["explanation"]
    ctrl.store.async_set(ctrl.with_room(lit.area, control_enabled=True))
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert zb.of(lit.group) == [SPOTS_10, SPOTS_OFF]
    assert [c for c in zb.calls if c.service == "turn_on"] == zb.calls[:1]  # nothing else switched on


async def test_manual_mood_blocks_until_it_ends(hass: HomeAssistant, lit: Lit, freezer) -> None:
    zb = zigbee(hass, lit)
    _entry, ctrl = await setup(hass, lit, freezer, zb, targets=(PreliminaryTarget(lit.group, 10),))
    await blip(hass, lit, freezer)
    main = "input_select.main_mood"
    options = hass.states.get(main).attributes["options"]
    hass.states.async_set(main, "Manual", {"options": [*options, "Manual"]})
    await hass.async_block_till_done()
    await advance(hass, freezer, 30)
    assert zb.of(lit.group) == [SPOTS_10]
    d = log(ctrl, lit.area)[0]
    assert (d["status"], d["mode"]) == ("blocked", "paused") and "Main Mood is Manual" in d["explanation"], d
    await advance(hass, freezer, 400)
    hass.states.async_set(main, "Dagur", {"options": [*options, "Manual"]})
    await hass.async_block_till_done()
    await advance(hass, freezer)
    assert zb.of(lit.group) == [SPOTS_10, SPOTS_OFF]


async def test_restart_while_the_switch_off_is_due(hass: HomeAssistant, lit: Lit, freezer) -> None:
    zb = zigbee(hass, lit)
    entry, ctrl = await setup(hass, lit, freezer, zb, targets=(PreliminaryTarget(lit.lamp, 10),))
    await blip(hass, lit, freezer)
    zb.fail.add(lit.lamp)
    await advance(hass, freezer, 30)
    assert lit.lamp in ctrl.runtime[lit.area].preliminary_cleanup
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    ctrl = entry.runtime_data.lighting
    assert lit.lamp in ctrl.runtime[lit.area].preliminary_cleanup  # kept across the restart
    zb.fail.clear()
    await advance(hass, freezer, 61)
    assert zb.of(lit.lamp)[-1][0] == "turn_off" and hass.states.is_state(lit.lamp, "off")


async def test_no_double_commands_when_the_room_becomes_vacant(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """A long preliminary timeout: the room becomes vacant first. The stage removes its spots, the vacancy action
    (Turn off) switches off the lamp a person had switched on, and no light gets two commands."""
    zb = zigbee(hass, lit)
    _entry, ctrl = await setup(hass, lit, freezer, zb, targets=(PreliminaryTarget(lit.group, 10),), timeout=600)
    await blip(hass, lit, freezer)
    zb.report(lit.lamp, True, 60)  # a light switched on at the device itself (unattributed)
    await hass.async_block_till_done()
    await advance(hass, freezer, 400)
    assert state(ctrl, lit.area) == "vacant"
    assert zb.of(lit.group) == [SPOTS_10, SPOTS_OFF]
    assert zb.of(lit.lamp) == [("turn_off", {})]
    entries = log(ctrl, lit.area)
    assert any(d["explanation"].startswith("Possible occupancy ended without confirmation (the room is vacant): "
                                           "light.kno_spots switched off") for d in entries)
    zb.clear()
    await advance(hass, freezer, 700)
    assert zb.calls == []


# --------------------------------------------- 0.3.10: a PIR that holds "occupied" (Aqara through Zigbee2MQTT)
async def held_detection(hass: HomeAssistant, lit: Lit, freezer, hold: int = 60) -> None:
    """One movement: the PIR reports occupied and holds it for ``hold`` seconds; nobody stays, the radar sees no one."""
    hass.states.async_set(lit.pir, "on")
    await hass.async_block_till_done()
    for _ in range(hold):
        await advance(hass, freezer, 1)
    hass.states.async_set(lit.pir, "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 1)


async def test_held_pir_single_light_goes_off_at_the_timeout(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """The reported fault: vacant room, light off; one PIR detection held for 60 s; nobody stays.

    Before 0.3.10 the engine confirmed occupancy after 10 s from the held PIR alone, the stage was handed over
    and the light stayed on until the vacancy timeout (10 min after the PIR cleared). Now the PIR (a
    preliminary signal) cannot confirm by itself: the timeout fires, light.turn_off is sent, the light is off."""
    zb = zigbee(hass, lit)
    _entry, ctrl = await setup(hass, lit, freezer, zb, targets=(PreliminaryTarget(lit.lamp, 10),))
    assert state(ctrl, lit.area) == "vacant" and hass.states.is_state(lit.lamp, "off")
    await held_detection(hass, lit, freezer)
    assert zb.of(lit.lamp) == [("turn_on", {"brightness_pct": 10, "transition": 0.5}),
                               ("turn_off", {"transition": 0.5})]
    assert hass.states.is_state(lit.lamp, "off")
    assert state(ctrl, lit.area) == "possible_vacancy"  # never confirmed
    assert not any(c.service == "turn_on" for c in zb.calls[1:])  # nothing switched on again
    trace = [t["event"] for t in ctrl.preliminary_trace(lit.area)]
    for event in ("stage_start", "timeout_scheduled", "activation_plan", "activation_call", "timeout_fired",
                  "stage_end", "cleanup_decision", "cleanup_call"):
        assert event in trace, (event, trace)
    start = next(t for t in ctrl.preliminary_trace(lit.area) if t["event"] == "stage_start")
    assert start["occupancy_before"] == "vacant" and start["trigger"] == [lit.pir]
    assert start["targets"] == {lit.lamp: "off"}
    call = next(t for t in ctrl.preliminary_trace(lit.area) if t["event"] == "cleanup_call")
    assert (call["result"], call["state_after"]) == ("accepted", "off")
    diag = ctrl.room_diagnostics(lit.area)["preliminary"]
    assert diag["trace"] and diag["stage"] is None and diag["cleanup_due"] == {}
    await advance(hass, freezer, 400)  # vacant later: nothing more for the lamp
    assert state(ctrl, lit.area) == "vacant" and len(zb.of(lit.lamp)) == 2


async def test_held_pir_multiple_lights_and_platforms(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """Three platforms at once: a Zigbee lamp (reports with its own context), a light group (members report),
    and a Wi-Fi strip whose switch-on call fails although the strip does switch on. All go off at the timeout."""
    zb = zigbee(hass, lit)

    async def wifi(call: ServiceCall) -> None:
        zb.calls.append(call)
        on = call.service == "turn_on"
        zb.report(lit.strip, on, call.data.get("brightness_pct"))
        if on:
            raise HomeAssistantError("Device did not acknowledge in time")

    original = hass.services.async_services()["light"]["turn_on"].job.target

    async def turn_on(call: ServiceCall) -> None:
        if lit.strip in ZigbeeLights._ids(call):
            await wifi(call)
        else:
            await original(call)

    hass.services.async_register("light", "turn_on", turn_on)
    targets = (PreliminaryTarget(lit.lamp, 10), PreliminaryTarget(lit.group, 10), PreliminaryTarget(lit.strip, 10))
    _entry, ctrl = await setup(hass, lit, freezer, zb, targets=targets)
    await held_detection(hass, lit, freezer)
    for e in (lit.lamp, lit.group, lit.strip):
        assert [s for s, _ in zb.of(e)] == ["turn_on", "turn_off"], (e, zb.of(e))
        assert hass.states.is_state(e, "off"), e
    start = next(d for d in log(ctrl, lit.area) if d["action"] == "Preliminary light on")
    assert start["status"] == "partial" and "light.kno_strip: Device did not acknowledge in time" in start["explanation"]


async def test_held_pir_light_already_on_and_automation_adjustment(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """A light that was on before is never switched off. A light an automation re-dims right after it was switched
    on (e.g. an adaptive lighting system) is still the stage's: only a recorded override would stop the switch-off."""
    zb = zigbee(hass, lit)
    targets = (PreliminaryTarget(lit.lamp, 10), PreliminaryTarget(lit.desk, 10))
    _entry, ctrl = await setup(hass, lit, freezer, zb, targets=targets)
    zb.report(lit.lamp, True, 40)
    await advance(hass, freezer)
    zb.clear()
    hass.states.async_set(lit.pir, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer, 3)
    automation = Context(parent_id=Context().id)  # an automation's action
    hass.states.async_set(lit.desk, "on", {**hass.states.get(lit.desk).attributes, "brightness": 150}, context=automation)
    await hass.async_block_till_done()
    for _ in range(57):
        await advance(hass, freezer, 1)
    assert zb.of(lit.lamp) == [] and hass.states.is_state(lit.lamp, "on")
    assert [s for s, _ in zb.of(lit.desk)] == ["turn_on", "turn_off"] and hass.states.is_state(lit.desk, "off")


async def test_held_pir_then_radar_confirms_and_hands_over(hass: HomeAssistant, lit: Lit, freezer) -> None:
    """Someone does come in: the radar (not a preliminary signal) confirms before the timeout; the lamp is handed
    over to Dagur and is not switched off by the cancelled timeout."""
    zb = zigbee(hass, lit)
    _entry, ctrl = await setup(hass, lit, freezer, zb, targets=(PreliminaryTarget(lit.lamp, 10),))
    hass.states.async_set(lit.pir, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer, 12)
    assert state(ctrl, lit.area) == "possible_occupancy"
    await presence(hass, lit, freezer)
    assert state(ctrl, lit.area) == "confirmed_occupancy"
    await advance(hass, freezer, 60)
    assert [s for s, _ in zb.of(lit.lamp)] == ["turn_on", "turn_on"] and hass.states.is_state(lit.lamp, "on")
    assert "timeout_cancelled" in [t["event"] for t in ctrl.preliminary_trace(lit.area)]


# ---------------------------------- 0.3.12: Turn off on confirmation while the normal lights have not reported yet
class SlowGroupLights(ZigbeeLights):
    """Like Zigbee2MQTT: the service call returns before the device (here: the spots group) reports its new state."""

    def __init__(self, hass: HomeAssistant, slow: set[str], **kw: Any) -> None:
        super().__init__(hass, **kw)
        self.slow = slow

    def report(self, entity_id: str, on: bool, pct: float | None = None, *, raw: int | None = None) -> None:
        if entity_id not in self.slow:
            super().report(entity_id, on, pct, raw=raw)
            return
        from homeassistant.core import callback
        from homeassistant.helpers.event import async_call_later

        @callback
        def later(_now) -> None:
            ZigbeeLights.report(self, entity_id, on, pct, raw=raw)

        async_call_later(self.hass, 0.5, later)


async def test_turn_off_on_confirmation_when_the_scene_lights_report_late(hass: HomeAssistant, lit: Lit,
                                                                         freezer) -> None:
    """The live trace of 2026-11-12 18:38: preliminary lamp and LED strip (Turn off), not controlled by the mood;
    the mood switches the spots group on, and the group reports 'on' only after the call returned. Both
    preliminary lights must go off after the spots are on (0.3.11 kept them on: 'kept_dark')."""
    zb = SlowGroupLights(hass, {lit.group}, groups={lit.group: [lit.spot_1, lit.spot_2]})
    targets = (PreliminaryTarget(lit.lamp, 100, None, None, ConfirmAction.TURN_OFF),
               PreliminaryTarget(lit.strip, 100, None, None, ConfirmAction.TURN_OFF))
    _entry, ctrl = await setup(hass, lit, freezer, zb, targets=targets)
    ctrl.store.async_set(ctrl.with_room(lit.area, light_overrides={"Dagur": {
        lit.lamp: _ignore(), lit.strip: _ignore(), lit.desk: _ignore(), lit.plug: _ignore()}}))
    await hass.async_block_till_done()
    await motion(hass, lit, freezer)
    assert [s for s, _ in zb.of(lit.lamp)] == ["turn_on"] and [s for s, _ in zb.of(lit.strip)] == ["turn_on"]
    await presence(hass, lit, freezer)
    assert state(ctrl, lit.area) == "confirmed_occupancy"
    await advance(hass, freezer)  # the group's own report arrives
    assert hass.states.is_state(lit.group, "on")
    assert [s for s, _ in zb.of(lit.lamp)] == ["turn_on", "turn_off"], zb.of(lit.lamp)
    assert [s for s, _ in zb.of(lit.strip)] == ["turn_on", "turn_off"], zb.of(lit.strip)
    order = [(c.service, c.data["entity_id"]) for c in zb.calls]
    assert order.index(("turn_on", [lit.group])) < order.index(("turn_off", [lit.lamp]))  # no dark moment
    confirmed = next(t for t in ctrl.preliminary_trace(lit.area) if t["event"] == "confirmed")
    assert confirmed["outcome"] == {lit.lamp: "turned_off", lit.strip: "turned_off"}, confirmed
    assert confirmed["scene_switched_on"] == [lit.group]
