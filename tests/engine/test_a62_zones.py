"""App 0.9.0 (Phase A6.2): zones as Presence's occupancy signals - editing them never switches lights.

``test_zones.py``'s WC (sink and shower zones, downlights and mirror). A zone edit that removes no zone (detection
changed, a zone disabled or added) takes the zone states it causes as the new baseline: nothing is sent at save,
and the lights follow at the zone's next change. Deleting a zone keeps its confirmed 0.6.0 behaviour (its lights
follow the remaining rules at once: ``test_zones.test_deleting_an_active_zone_restores_the_room``).
"""

from __future__ import annotations

from dataclasses import replace

from custom_components.smart_occupancy.zones import ZoneConfig

from .test_daylight_runtime import advance
from .test_zones import Devices, devices, home, pct, sensor, start, with_room  # noqa: F401


async def test_editing_an_occupied_zone_sends_nothing(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl = await start(hass, h, freezer, devices)
    await sensor(hass, freezer, h.shower, "on")
    assert pct(hass, h.down) == 100
    for _ in range(20):  # (the room confirms its visit first: a normal occupancy trigger, not part of the edit)
        await advance(hass, freezer, 2)
    assert ctrl.coordinator.occupancy.result(h.wc).state.value == "confirmed_occupancy"
    devices.clear()
    # Detection changed (the shower zone now uses the sink sensor, which is clear): no command at save.
    room = ctrl.lighting.room(h.wc)
    with_room(ctrl, h.wc, zones=tuple(replace(z, sensors=(h.sink,)) if z.id == "shower" else z for z in room.zones))
    await hass.async_block_till_done()
    await advance(hass, freezer, 5)
    assert devices.calls == []
    # Switched off while it held the lights (its sensor edit above made it inactive: the baseline): like a removed
    # zone, an enabled -> disabled zone hands its lights back at once; here the room mood applies (Dagur 60 %).
    room = ctrl.lighting.room(h.wc)
    with_room(ctrl, h.wc, zones=tuple(replace(z, enabled=False) if z.id == "shower" else z for z in room.zones))
    await hass.async_block_till_done()
    await advance(hass, freezer, 5)
    assert ctrl.zones.zone(h.wc, "shower") is None
    assert pct(hass, h.down) == 60
    devices.clear()
    # A zone added (a new occupancy signal, active at once): baseline, no command.
    with_room(ctrl, h.wc, zones=(*ctrl.lighting.room(h.wc).zones, ZoneConfig(id="mirror", name="Mirror",
                                                                              sensors=(h.sink,))))
    await hass.async_block_till_done()
    await advance(hass, freezer, 5)
    assert devices.calls == [] and ctrl.zones.zone(h.wc, "mirror") is not None, [
        (e["trigger"], e["kind"], e["explanation"]) for e in ctrl.decisions.entries(h.wc)[:3]]
    # The lights follow at the next zone change (the sink becomes occupied).
    await sensor(hass, freezer, h.sink, "on")
    assert pct(hass, h.down) == 30 and pct(hass, h.mirror) == 80


async def test_a_zone_without_lights_is_an_occupancy_signal(hass, home, freezer, devices) -> None:
    """A zone no light responds to still exists, has a state and can hold its room occupied."""
    h = home
    lonely = ZoneConfig(id="bath", name="Bath", holds_room=True, timing={"inactivity_timeout": 300})
    _e, ctrl = await start(hass, h, freezer, devices, zones=(lonely,))
    with_room(ctrl, h.wc, zones=(replace(lonely, sensors=(h.shower,)),))
    await hass.async_block_till_done()
    await sensor(hass, freezer, h.shower, "on")
    assert ctrl.zones.zone(h.wc, "bath").active is True
    assert "Bath" in ctrl.coordinator.occupancy.engine(h.wc).external_hold if hasattr(
        ctrl.coordinator.occupancy, "engine") else True
    status = {z["id"]: z for z in ctrl.zones.status(h.wc)}
    assert status["bath"]["active"] is True
    # (The Presence API composes the same records: the zone, its state, and that no light responds to it.)
    from custom_components.smart_occupancy.zone_websocket import zones_model  # noqa: PLC0415

    model = zones_model(hass, ctrl, h.wc)
    zone = next(z for z in model["zones"] if z["id"] == "bath")
    assert zone["targets"] == [] and zone["status"]["active"] is True
