"""App 0.9.0 (Phase A6.2): Identify a light, and the light inspector.

The Phase 4.7 Kamar Norður of ``test_brightness.py``: simulated devices that accept a command and report with a
context of their own (like Zigbee2MQTT), here also for HA's ``brightness`` attribute (the restore sends the exact
0-255 brightness it captured).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.smart_occupancy import light_inspector
from custom_components.smart_occupancy.brightness import LightMatrix
from custom_components.smart_occupancy.light_identify import IDENTIFY_SECONDS, IdentifyError
from custom_components.smart_occupancy.priority import PriorityInput
from custom_components.smart_occupancy.zones import SupervisorSettings

from .test_a62_delays import DELAYED
from .test_brightness import CT, DIM, ONOFF, RGB, Devices, home, occupy, sensor, start, wait  # noqa: F401
from .test_brightness import with_room  # noqa: F401


class Reporting(Devices):
    """Devices that also take Home Assistant's ``brightness`` (0-255), ``hs_color`` and ``effect``."""

    def report(self, entity_id: str, on: bool, data: dict[str, Any] | None = None, context: Context | None = None):
        data = dict(data or {})
        raw = data.pop("brightness", None)
        super().report(entity_id, on, data, context)
        if on and raw is not None:
            state = self.hass.states.get(entity_id)
            self.hass.states.async_set(entity_id, "on", {**state.attributes, "brightness": raw},
                                       context=context or Context())


@pytest.fixture
def devices(hass: HomeAssistant) -> Reporting:
    return Reporting(hass)


def calls(devices, entity_id: str) -> list[tuple[str, dict]]:
    return devices.of(entity_id)


async def identify(hass, freezer, ctrl, area: str, entity_id: str, *, after=None, sim=None, **kw) -> dict:
    """Run Identify while the clock moves (``after``: called once the light is at full brightness)."""
    # (A background task: async_block_till_done must not wait for its timer on the frozen clock.)
    task = hass.async_create_background_task(ctrl.async_identify(area, entity_id, user_id="admin", **kw), "identify")
    await wait(hass, freezer, 1, sim)
    if after is not None:
        await after()
    await wait(hass, freezer, 4, sim)
    return await task


def no_manual_trace(ctrl, area: str, entity_id: str) -> None:
    rt = ctrl.runtime[area]
    assert entity_id not in rt.overrides
    assert entity_id not in rt.last_external


async def test_off_light_flashes_and_goes_off_again(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices={})
    out = await identify(hass, freezer, ctrl, h.area, h.floor, sim=sim)
    assert out["outcome"] == "restored", out
    assert calls(devices, h.floor) == [("light.turn_on", {"brightness_pct": 100}), ("light.turn_off", {})]
    assert hass.states.get(h.floor).state == "off"
    no_manual_trace(ctrl, h.area, h.floor)
    entry = ctrl.decisions.entries(h.area)[0]
    assert entry["kind"] == "identify" and entry["status"] == "executed"
    assert "not a manual change" in entry["details"]["attribution"]
    assert not ctrl.identify.ops and not ctrl.identify.journal
    # Nothing else changed: no mood, occupancy or Supervisor activity.
    assert ctrl.mood_status().mood == "Hygge"
    assert ctrl.supervisor.room(h.area).targets.get(h.floor) is None or \
        ctrl.supervisor.room(h.area).targets[h.floor].last_command is None


async def test_dimmed_warm_white_returns_to_its_brightness_and_colour(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices={})
    hass.states.async_set(h.floor, "on", {**CT, "brightness": 77, "color_mode": "color_temp",
                                          "color_temp_kelvin": 2200})
    await wait(hass, freezer, 2, sim)
    ctrl.async_clear_override(h.area, h.floor)  # (that state was set by the test as a person would)
    ctrl.runtime[h.area].last_external.pop(h.floor, None)
    devices.clear()
    out = await identify(hass, freezer, ctrl, h.area, h.floor, sim=sim)
    assert out["outcome"] == "restored", out
    assert calls(devices, h.floor) == [("light.turn_on", {"brightness_pct": 100}),
                                       ("light.turn_on", {"brightness": 77, "color_temp_kelvin": 2200})]
    state = hass.states.get(h.floor)
    assert state.attributes["brightness"] == 77 and state.attributes["color_temp_kelvin"] == 2200
    no_manual_trace(ctrl, h.area, h.floor)


async def test_rgb_onoff_and_relay_get_only_what_they_support(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices={}, relays=(h.relay,))
    hass.states.async_set(h.strip, "on", {**RGB, "brightness": 128, "color_mode": "rgb", "rgb_color": [255, 0, 40]})
    await wait(hass, freezer, 2, sim)
    devices.clear()
    assert (await identify(hass, freezer, ctrl, h.area, h.strip, sim=sim))["outcome"] == "restored"
    assert calls(devices, h.strip)[-1] == ("light.turn_on", {"brightness": 128, "rgb_color": [255, 0, 40]})
    assert (await identify(hass, freezer, ctrl, h.area, h.onoff, sim=sim))["outcome"] == "restored"
    assert calls(devices, h.onoff) == [("light.turn_on", {}), ("light.turn_off", {})]  # no brightness
    assert (await identify(hass, freezer, ctrl, h.area, h.relay, sim=sim))["outcome"] == "restored"
    assert calls(devices, h.relay) == [("switch.turn_on", {}), ("switch.turn_off", {})]


async def test_manual_change_during_identify_is_never_restored_over(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices={})

    async def person() -> None:
        hass.states.async_set(h.floor, "on", {**CT, "brightness": 30, "color_mode": "color_temp"},
                              context=Context(user_id="person"))
        await hass.async_block_till_done()

    out = await identify(hass, freezer, ctrl, h.area, h.floor, after=person, sim=sim)
    assert out["outcome"] == "interrupted" and "Home Assistant" in out["reason"], out
    assert calls(devices, h.floor) == [("light.turn_on", {"brightness_pct": 100})]  # no restore
    assert hass.states.get(h.floor).attributes["brightness"] == 30
    assert ctrl.runtime[h.area].overrides[h.floor]["source"] == "ui"  # the person's change, as always


async def test_physical_dim_without_a_user_is_detected_before_restoring(hass, home, freezer, devices) -> None:
    """A remote reported with a context of its own looks like our echo: the state check before restoring sees it."""
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices={})

    async def remote() -> None:
        hass.states.async_set(h.floor, "on", {**CT, "brightness": 90, "color_mode": "color_temp"})
        await hass.async_block_till_done()

    out = await identify(hass, freezer, ctrl, h.area, h.floor, after=remote, sim=sim)
    assert out["outcome"] == "interrupted" and "brightness" in out["reason"], out
    assert calls(devices, h.floor) == [("light.turn_on", {"brightness_pct": 100})]


async def test_wall_switch_and_priority_input_supersede_identify(hass, home, freezer, devices) -> None:
    h = home
    hass.states.async_set("binary_sensor.cupboard", "off")
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices={}, priority_inputs=(PriorityInput(
        id="cupboard", name="Cupboard", entity_id="binary_sensor.cupboard", enabled=True, targets=(h.dimmer,),
        brightness_pct=60, debounce=0, release_delay=0),))
    await wait(hass, freezer, 12, sim)

    async def press() -> None:
        await ctrl._send_for_person(h.area, "turn_on", {h.floor: {"brightness_pct": 40}}, Context(), purpose="switch")

    out = await identify(hass, freezer, ctrl, h.area, h.floor, after=press, sim=sim)
    assert out["outcome"] == "superseded", out
    assert calls(devices, h.floor) == [("light.turn_on", {"brightness_pct": 100}),
                                       ("light.turn_on", {"brightness_pct": 40})]  # never restored over it

    async def cupboard() -> None:
        hass.states.async_set("binary_sensor.cupboard", "on")
        await hass.async_block_till_done()
        await wait(hass, freezer, 2, sim)

    out = await identify(hass, freezer, ctrl, h.area, h.dimmer, after=cupboard, sim=sim)
    assert out["outcome"] == "superseded", out
    assert calls(devices, h.dimmer)[-1] == ("light.turn_on", {"brightness_pct": 60})
    assert hass.states.get(h.dimmer).attributes["brightness"] == 153


async def test_a_newer_automatic_command_wins_and_nothing_is_sent_twice(hass, home, freezer, devices) -> None:
    """Identify the floor lamp (15 %, Room occupied); the Desk zone starts meanwhile: its 45 % is sent and is the
    final state - no restore of 15 % over it, no second command."""
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices)
    await occupy(hass, h, freezer, sim)
    assert hass.states.get(h.floor).attributes["brightness"] == 38  # 15 %
    devices.clear()

    async def desk() -> None:
        await sensor(hass, freezer, h.desk, "on", settle=2, sim=sim)

    out = await identify(hass, freezer, ctrl, h.area, h.floor, after=desk, sim=sim)
    assert out["outcome"] == "superseded", out
    assert [(c, d.get("brightness_pct")) for c, d in calls(devices, h.floor)] == [
        ("light.turn_on", 100), ("light.turn_on", 45)]
    await wait(hass, freezer, 10, sim)
    assert len(calls(devices, h.floor)) == 2 and round(hass.states.get(h.floor).attributes["brightness"] / 2.55) == 45
    no_manual_trace(ctrl, h.area, h.floor)


async def test_plans_during_identify_see_the_light_as_it_was(hass, home, freezer, devices) -> None:
    """A trigger while the lamp flashes (a mood re-applied) that wants what the lamp had: no command; restored."""
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices)
    await occupy(hass, h, freezer, sim)
    devices.clear()

    async def trigger() -> None:
        item = next(i for i in ctrl.effective_plan(h.area).items if i.entity_id == h.floor)
        assert item.current["state"] == "on" and item.decision == "unchanged", item  # (15 % as before)
        await ctrl.async_apply(h.area, "profile_change")

    out = await identify(hass, freezer, ctrl, h.area, h.floor, after=trigger, sim=sim)
    assert out["outcome"] == "restored", out
    assert [c for c, _d in calls(devices, h.floor)] == ["light.turn_on", "light.turn_on"]
    assert hass.states.get(h.floor).attributes["brightness"] == 38


async def test_unavailable_unconfigured_and_busy_lights_are_refused(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices={})
    hass.states.async_set(h.dimmer, "unavailable", DIM)
    await wait(hass, freezer, 1, sim)
    with pytest.raises(IdentifyError) as err:
        await ctrl.async_identify(h.area, h.dimmer)
    assert err.value.code == "unavailable"
    with pytest.raises(IdentifyError) as err:
        await ctrl.async_identify(h.area, "light.somewhere_else")
    assert err.value.code == "not_configured"
    task = hass.async_create_background_task(ctrl.async_identify(h.area, h.floor), "identify")
    await wait(hass, freezer, 1, sim)
    with pytest.raises(IdentifyError) as err:
        await ctrl.async_identify(h.area, h.floor)
    assert err.value.code == "busy"
    await wait(hass, freezer, 4, sim)
    assert (await task)["outcome"] == "restored"
    assert devices.of(h.dimmer) == []


async def test_a_group_needs_an_explicit_choice(hass, home, freezer, devices) -> None:
    h = home
    entry = er.async_get(hass).async_get_or_create("light", "test", "uid_kno_group", suggested_object_id="kno_group")
    er.async_get(hass).async_update_entity(entry.entity_id, area_id=h.area, labels={h.labels["lamps"]})
    group = entry.entity_id
    hass.states.async_set(group, "off", {**DIM, "entity_id": [h.dimmer, h.onoff]})
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices={})
    hass.states.async_set(h.onoff, "on", ONOFF)
    await wait(hass, freezer, 2, sim)
    devices.clear()
    with pytest.raises(IdentifyError) as err:
        await ctrl.async_identify(h.area, group)
    assert err.value.code == "group" and err.value.details["members"] == sorted([h.dimmer, h.onoff])
    # One physical member: only that light is commanded.
    assert (await identify(hass, freezer, ctrl, h.area, h.dimmer, sim=sim))["outcome"] == "restored"
    assert devices.of(group) == [] and devices.of(h.onoff) == []
    devices.clear()
    # The whole group (a reviewed choice): one command to the group, each member back to its own state.
    out = await identify(hass, freezer, ctrl, h.area, group, whole_group=True, sim=sim)
    assert out["outcome"] == "restored" and out["group"], out
    assert devices.of(group) == [("light.turn_on", {"brightness_pct": 100})]
    assert devices.of(h.dimmer)[-1] == ("light.turn_off", {}) and devices.of(h.onoff)[-1] == ("light.turn_on", {})


async def test_manual_mood_and_supervisor(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices={},
                                supervisor=SupervisorSettings.from_dict({"mode": "automatic", "reconcile_interval": 5}))
    hass.states.async_set("input_select.main_mood", "Manual", {"options": ["Hygge", "Arbeiði", "Dagur", "Manual"]})
    await wait(hass, freezer, 2, sim)

    async def check() -> None:
        model = ctrl.supervisor.reconcile(h.area)
        assert next(t for t in model["targets"] if t["entity_id"] == h.floor)["result"] == "paused"

    out = await identify(hass, freezer, ctrl, h.area, h.floor, after=check, sim=sim)
    assert out["outcome"] == "restored"  # Identify is an administrator's explicit request: also in Manual mood
    await wait(hass, freezer, 30, sim)
    assert calls(devices, h.floor) == [("light.turn_on", {"brightness_pct": 100}), ("light.turn_off", {})]


async def test_a_pending_delay_survives_identify(hass, home, freezer, devices) -> None:
    h = home
    matrices = {h.floor: replace(DELAYED[h.floor], delays={"desk": {"low": (20, 0)}})}
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices=matrices)
    await occupy(hass, h, freezer, sim)
    await sensor(hass, freezer, h.desk, "on", settle=2, sim=sim)
    assert ctrl.delays.get(h.area, h.floor) is not None
    assert (await identify(hass, freezer, ctrl, h.area, h.floor, sim=sim))["outcome"] == "restored"
    assert ctrl.delays.get(h.area, h.floor) is not None  # (Identify changes no rule and no pending action)
    await wait(hass, freezer, 20, sim)
    assert round(hass.states.get(h.floor).attributes["brightness"] / 2.55) == 45


async def test_lost_authority_then_restart_restores_conservatively(hass, home, freezer, devices, hass_storage) -> None:
    h = home
    entry, ctrl, sim = await start(hass, h, freezer, devices, matrices={})

    async def fence() -> None:
        ctrl.fenced = True  # (the lease is lost: no command may be sent)

    out = await identify(hass, freezer, ctrl, h.area, h.floor, after=fence, sim=sim)
    assert out["outcome"] == "failed" and "restore failed" in out["reason"], out
    assert hass.states.get(h.floor).state == "on" and h.floor in ctrl.identify.journal
    await ctrl.async_save_state_now() if not ctrl.fenced else None
    ctrl.fenced = False
    await ctrl.async_flush()
    devices.clear()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    ctrl = entry.runtime_data.lighting
    assert h.floor in ctrl.identify.journal
    await wait(hass, freezer, 14, sim)
    assert devices.of(h.floor) == [("light.turn_off", {})] and not ctrl.identify.journal


async def test_restart_never_restores_a_light_that_changed(hass, home, freezer, devices) -> None:
    h = home
    entry, ctrl, sim = await start(hass, h, freezer, devices, matrices={})

    async def fence() -> None:
        ctrl.fenced = True

    await identify(hass, freezer, ctrl, h.area, h.floor, after=fence, sim=sim)
    ctrl.fenced = False
    await ctrl.async_flush()
    hass.states.async_set(h.floor, "on", {**CT, "brightness": 60, "color_mode": "color_temp"},
                          context=Context(user_id="person"))
    await wait(hass, freezer, 2, sim)
    devices.clear()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    ctrl = entry.runtime_data.lighting
    await wait(hass, freezer, 14, sim)
    assert devices.of(h.floor) == [] and not ctrl.identify.journal


async def test_too_old_journal_is_dropped(hass, home, freezer, devices) -> None:
    h = home
    entry, ctrl, sim = await start(hass, h, freezer, devices, matrices={})

    async def fence() -> None:
        ctrl.fenced = True

    await identify(hass, freezer, ctrl, h.area, h.floor, after=fence, sim=sim)
    ctrl.fenced = False
    await ctrl.async_flush()
    await wait(hass, freezer, 130, sim)  # (longer than the recovery window)
    devices.clear()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await wait(hass, freezer, 14, sim)
    assert devices.of(h.floor) == []


async def test_inspector_explains_rule_command_and_state(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices)
    await occupy(hass, h, freezer, sim)
    await sensor(hass, freezer, h.desk, "on", settle=2, sim=sim)
    await identify(hass, freezer, ctrl, h.area, h.floor, sim=sim)
    out = light_inspector.inspect(hass, ctrl, h.floor)
    c = out["control"]
    assert out["configured"] and out["area_id"] == h.area and out["device"]["capabilities"]["color_temp"]
    assert c["mode"] == "automatic" and c["mood"] == "Hygge" and c["occupancy"]["occupied"]
    assert [z["id"] for z in c["zones"]] == ["desk"] and c["ambient"]["level"] == "low"
    assert c["winning"]["kind"] == "zone" and c["winning"]["brightness"]["value"] == 45
    assert "Room occupied" in c["winning"]["superseded"]
    assert c["last_command"]["data"]["brightness_pct"] == 45  # the last command of the lighting rules
    assert c["reported"]["state"] == "on" and c["reported"]["brightness_pct"] == 45
    assert c["override"] is None and c["last_external"] is None  # Identify left no manual trace
    kinds = [e["kind"] for e in out["history"]]
    assert "identify" in kinds and "zone" in kinds and out["history"][0]["attribution"] == "identify"
    assert out["identify"]["available"] is True and out["identify"]["recent"][0]["outcome"] == "restored"
    # An unattributed change is shown as such, never guessed (after the 20 s in which a report is our echo).
    await wait(hass, freezer, 25, sim)
    hass.states.async_set(h.floor, "on", {**CT, "brightness": 100, "color_mode": "color_temp"})
    await wait(hass, freezer, 2, sim)
    out = light_inspector.inspect(hass, ctrl, h.floor)
    assert "did not say who changed it" in out["control"]["last_external"]["who"]
    # A light no room manages: shown, not controlled, not identifiable.
    out = light_inspector.inspect(hass, ctrl, h.shared, h.area)
    assert out["area_id"] != h.area or out["control"] is None or out["control"]["target"] == h.shared
    none = light_inspector.inspect(hass, ctrl, "light.nowhere")
    assert none["control"] is None and none["identify"]["available"] is False


async def test_inspect_and_identify_websocket_commands(hass, home, freezer, devices, hass_ws_client,
                                                       hass_admin_user, hass_read_only_user) -> None:
    from homeassistant.components import websocket_api  # noqa: PLC0415

    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices={})
    for handler in light_inspector.INSPECTOR_HANDLERS:
        websocket_api.async_register_command(hass, handler)
    refresh = await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/app")
    ws = await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(refresh))
    await ws.send_json({"id": 1, "type": "smart_occupancy/lighting/light/inspect", "entity_id": h.floor})
    res = await ws.receive_json()
    assert res["success"] and res["result"]["can_identify"] is True
    await ws.send_json({"id": 2, "type": "smart_occupancy/lighting/light/identify", "entity_id": h.floor})
    for _ in range(50):  # (let the server read the message before the clock moves)
        if ctrl.identify.ops:
            break
        await asyncio.sleep(0)  # (a yield: the frozen clock never lets a timed sleep end)
    assert ctrl.identify.ops
    await wait(hass, freezer, 5, sim)
    res = await ws.receive_json()
    assert res["success"] and res["result"]["outcome"] == "restored", res
    assert IDENTIFY_SECONDS == 2.5


# ===================================================================== review findings (A6.2 independent review)
async def test_a_global_adjustment_update_waits_for_the_restore(hass, home, freezer, devices) -> None:
    """A colour temperature update of the global adjustment during the hold is sent after the restore (it does not
    supersede the identification, which would leave the lamp at full brightness)."""
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices={})
    hass.states.async_set(h.floor, "on", {**CT, "brightness": 102, "color_mode": "color_temp",
                                          "color_temp_kelvin": 2700})
    await wait(hass, freezer, 25, sim)
    ctrl.async_clear_override(h.area, h.floor)
    devices.clear()
    task = None

    async def adaptive() -> None:
        nonlocal task
        task = hass.async_create_background_task(ctrl._send(h.area, "turn_on", {h.floor: {"color_temp_kelvin": 3000}},
                                                            adaptive=True), "adaptive")

    out = await identify(hass, freezer, ctrl, h.area, h.floor, after=adaptive, sim=sim)
    assert out["outcome"] == "restored", out
    assert await task == {}
    assert calls(devices, h.floor) == [("light.turn_on", {"brightness_pct": 100}),
                                       ("light.turn_on", {"brightness": 102, "color_temp_kelvin": 2700}),
                                       ("light.turn_on", {"color_temp_kelvin": 3000})]
    assert hass.states.get(h.floor).attributes["brightness"] == 102


async def test_a_dumb_light_is_only_switched_on_and_off(hass, home, freezer, devices) -> None:
    from custom_components.smart_occupancy.fixtures import Fixture  # noqa: PLC0415

    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices={}, fixtures={h.dimmer: Fixture(h.dimmer)})
    hass.states.async_set(h.dimmer, "on", {**DIM, "brightness": 90, "color_mode": "brightness"})
    await wait(hass, freezer, 25, sim)
    ctrl.async_clear_override(h.area, h.dimmer)
    devices.clear()
    assert (await identify(hass, freezer, ctrl, h.area, h.dimmer, sim=sim))["outcome"] == "restored"
    assert calls(devices, h.dimmer) == [("light.turn_on", {}), ("light.turn_on", {})]  # never a brightness


async def test_a_colour_change_without_a_user_is_not_restored_over(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices={})
    hass.states.async_set(h.floor, "on", {**CT, "brightness": 102, "color_mode": "color_temp",
                                          "color_temp_kelvin": 2700})
    await wait(hass, freezer, 25, sim)
    ctrl.async_clear_override(h.area, h.floor)
    devices.clear()

    async def remote() -> None:
        state = hass.states.get(h.floor)
        hass.states.async_set(h.floor, "on", {**state.attributes, "color_temp_kelvin": 5000})
        await hass.async_block_till_done()

    out = await identify(hass, freezer, ctrl, h.area, h.floor, after=remote, sim=sim)
    assert out["outcome"] == "interrupted" and "colour" in out["reason"], out
    assert calls(devices, h.floor) == [("light.turn_on", {"brightness_pct": 100})]
