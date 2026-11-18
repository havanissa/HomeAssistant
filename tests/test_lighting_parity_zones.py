"""Phase A4 parity scenarios (3): shared spaces and the Room Supervisor (the zone tests' home).

See ``test_lighting_parity.py``: each scenario is recorded and compared with its fixture; the App replays it.
"""

from __future__ import annotations

from dataclasses import replace

from homeassistant.core import Context, HomeAssistant

from custom_components.smart_occupancy.daylight import RoomDaylight
from custom_components.smart_occupancy.models import ProfileSetting
from custom_components.smart_occupancy.supervisor import SupervisorSettings
from custom_components.smart_occupancy.zones import SharedSpace, SpaceVacancy

from . import lighting_parity as lp
from .conftest import setup_entry
from .test_lighting_parity import START, begin, configure, devices, finish, ws_admin  # noqa: F401
from .test_zones import SHOWER, SINK, home, options as zone_options, replace_sensors  # noqa: F401


async def _start(hass, home, **wc):
    entry = await setup_entry(hass, zone_options(home))
    ctrl = entry.runtime_data.lighting
    for area in (home.wc, home.kitchen, home.stova):
        configure(ctrl, area, control_enabled=True, daylight=RoomDaylight.from_dict({"auto_on": "always"}),
                  **(wc if area == home.wc else {}))
    return entry, ctrl


async def test_shared_space(hass: HomeAssistant, home, devices, freezer, hass_ws_client, hass_admin_user) -> None:
    freezer.move_to(START)
    entry, ctrl = await _start(hass, home)
    ws = await ws_admin(hass, hass_ws_client, hass_admin_user)
    for area in (home.kitchen, home.stova):
        configure(ctrl, area, light_overrides={"Dagur": {home.shared: ProfileSetting.from_dict(
            {"action": "on", "brightness_pct": 50 if area == home.kitchen else 90})}})
    ctrl.store.async_set(replace(ctrl.lighting, shared_spaces=(SharedSpace(
        id="living", name="Kitchen and living room", rooms=(home.kitchen, home.stova),
        vacancy=SpaceVacancy.INDEPENDENT),)))
    rec = await begin(hass, freezer, entry, ws, devices, "shared_space",
                      "Two rooms share a pendant: it is not switched off while the other room is occupied")
    hass.states.async_set(home.k_pir, "on")
    await lp.advance(hass, freezer, 12, rec)
    hass.states.async_set(home.s_pir, "on")
    await lp.advance(hass, freezer, 12, rec)
    hass.states.async_set(home.k_pir, "off")
    await lp.advance(hass, freezer, 120, rec, step=2)
    hass.states.async_set(home.s_pir, "off")
    await lp.advance(hass, freezer, 120, rec, step=2)
    finish(rec)


async def test_supervisor_corrections(hass: HomeAssistant, home, devices, freezer, hass_ws_client,
                                      hass_admin_user) -> None:
    freezer.move_to(START)
    zones = tuple(replace_sensors(z, home) for z in (SINK, SHOWER))
    entry, ctrl = await _start(hass, home, zones=zones, supervisor=SupervisorSettings.from_dict(
        {"mode": "automatic", "correct_external": True}))
    ws = await ws_admin(hass, hass_ws_client, hass_admin_user)
    rec = await begin(hass, freezer, entry, ws, devices, "supervisor_corrections",
                      "Automatic supervisor: a light that ignored its command and one an automation changed are "
                      "corrected; a repeat is a conflict")
    devices.ignore[home.down] = 1  # the first command is accepted but not carried out
    hass.states.async_set(home.shower, "on")
    await lp.advance(hass, freezer, 40, rec)
    devices.report(home.down, True, 10, context=Context(parent_id=Context().id))  # an automation dims it
    await lp.advance(hass, freezer, 40, rec)
    devices.report(home.down, True, 10, context=Context(parent_id=Context().id))
    await lp.advance(hass, freezer, 80, rec, step=2)
    finish(rec)
