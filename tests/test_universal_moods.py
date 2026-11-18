"""Phase A4: universal moods - no helper, name or language is required.

* The three-helper scheme keeps working unchanged.
* One existing ``select`` / ``input_select`` can be the mood source (no Auto/Manual helpers, no problem reported).
* The built-in mood selector: moods defined by the user become a ``select`` entity of the integration (plus the
  Manual option); lighting follows it; the selected mood survives a restart.
* ``mood_source/set`` validates every source; profiles are keyed by name and untouched.
* A new installation can finish the config flow without any mood helper.
"""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.smart_occupancy.const import BUILTIN_MOOD_ENTITY, DOMAIN

from . import lighting_parity as lp
from .conftest import setup_entry
from .test_lighting_bridge import admin_client
from .test_lighting_runtime import lit, options  # noqa: F401
from .test_lighting_parity import devices  # noqa: F401

TIMING = {"correlation_window": 10, "exit_confirmation": 30, "inactivity_timeout": 60, "startup_hold": 0}


class WS:
    def __init__(self, client) -> None:
        self.client = client

    async def __call__(self, **msg):
        await self.client.send_json_auto_id(msg)
        reply = await self.client.receive_json()
        while reply.get("type") == "event":
            reply = await self.client.receive_json()
        return reply


async def _start(hass, lit, freezer, mood: dict):
    freezer.move_to("2026-06-15 10:00:00+00:00")
    opts = options(lit, timing=TIMING)
    opts["mood"] = mood
    entry = await setup_entry(hass, opts)
    ctrl = entry.runtime_data.lighting
    ctrl.store.async_set(ctrl.with_room(lit.area, control_enabled=True))
    hass.states.async_set(lit.pir, "on")
    hass.states.async_set(lit.radar, "on")
    await lp.advance(hass, freezer, 12)
    return entry


SOURCE_PROBLEMS = ("mood_entity_not_configured", "mood_entity_wrong_domain", "entity_not_provided", "entity_missing")


def problems(entry) -> list[str]:
    """Problems of the mood source itself (profile and option warnings are reported as before)."""
    return [p.code for p in entry.runtime_data.data.problems if p.code in SOURCE_PROBLEMS]


async def test_single_selector_is_a_complete_mood_source(hass: HomeAssistant, lit, devices, freezer) -> None:
    hass.states.async_set("select.house_mood", "Dagur", {"options": ["Dagur", "Hygge", "Nátt"]})
    entry = await _start(hass, lit, freezer, {"main_mood_entity": "select.house_mood"})
    ctrl = entry.runtime_data.lighting
    assert entry.runtime_data.config.mood.source == "entity"
    assert ctrl.mood_status().mood == "Dagur" and problems(entry) == []
    devices.clear()
    hass.states.async_set("select.house_mood", "Hygge", {"options": ["Dagur", "Hygge", "Nátt"]})
    await lp.advance(hass, freezer, 5)
    assert devices.calls  # the mood change is applied
    assert {m["name"] for m in ctrl.discovered_moods()} >= {"Dagur", "Hygge", "Nátt"}


async def test_builtin_selector(hass: HomeAssistant, lit, devices, freezer, hass_ws_client, hass_admin_user) -> None:
    hass.states.async_set("select.house_mood", "Dagur", {"options": ["Dagur", "Hygge"]})
    entry = await _start(hass, lit, freezer, {"main_mood_entity": "select.house_mood"})
    ws = WS(await admin_client(hass, hass_ws_client, hass_admin_user))
    for msg, error in (
        ({"source": "builtin", "moods": []}, "Define at least one mood"),
        ({"source": "builtin", "moods": ["Calm", "*"]}, "reserved"),
        ({"source": "builtin", "moods": ["Calm", "Manual"]}, "reserved"),
        ({"source": "helpers", "main_mood_entity": "select.house_mood"}, "three different selectors"),
        ({"source": "entity"}, "Choose the mood selector"),
    ):
        reply = await ws(type="smart_occupancy/lighting/mood_source/set", **msg)
        assert not reply["success"] and error in reply["error"]["message"], (msg, reply)
    profiles_before = entry.runtime_data.lighting.lighting.global_profiles
    reply = await ws(type="smart_occupancy/lighting/mood_source/set", source="builtin",
                     moods=["Morning", "Dagur", "Hygge", "Dagur"])
    assert reply["success"] and reply["result"]["changed"], reply
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.options["mood"]["builtin"] == ["Morning", "Dagur", "Hygge"]
    state = hass.states.get(BUILTIN_MOOD_ENTITY)
    assert state is not None and state.attributes["options"] == ["Morning", "Dagur", "Hygge", "Manual"]
    ctrl = entry.runtime_data.lighting
    assert ctrl.lighting.global_profiles == profiles_before  # profiles are keyed by name: unchanged
    assert problems(entry) == []
    await lp.advance(hass, freezer, 130, step=5)  # (the reload's occupancy start-up hold)
    devices.clear()
    await hass.services.async_call("select", "select_option", {"entity_id": BUILTIN_MOOD_ENTITY, "option": "Hygge"},
                                   blocking=True)
    await lp.advance(hass, freezer, 5)
    assert ctrl.mood_status().mood == "Hygge" and devices.calls
    await hass.services.async_call("select", "select_option", {"entity_id": BUILTIN_MOOD_ENTITY, "option": "Manual"},
                                   blocking=True)
    await lp.advance(hass, freezer, 2)
    assert ctrl.mood_status().manual
    # Restart: the selection is kept.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(BUILTIN_MOOD_ENTITY).state == "Manual"
    # Back to one existing selector: the built-in entity is removed. (A new connection: long time jumps end one.)
    ws = WS(await admin_client(hass, hass_ws_client, hass_admin_user))
    reply = await ws(type="smart_occupancy/lighting/mood_source/set", source="entity",
                     main_mood_entity="select.house_mood")
    assert reply["success"], reply
    await hass.async_block_till_done()
    assert hass.states.get(BUILTIN_MOOD_ENTITY) is None or hass.states.get(BUILTIN_MOOD_ENTITY).state == "unavailable"


async def test_three_helper_scheme_unchanged(hass: HomeAssistant, lit, devices, freezer) -> None:
    from .test_lighting_runtime import set_moods  # noqa: PLC0415

    set_moods(hass, "Dagur", "Auto", "Dagur")
    entry = await _start(hass, lit, freezer, options(lit)["mood"])
    assert entry.runtime_data.config.mood.source == "helpers" and problems(entry) == []
    hass.states.async_remove("input_select.auto_mood")
    await lp.advance(hass, freezer, 40)
    await entry.runtime_data.async_refresh()
    assert "mood_entity_not_configured" not in problems(entry)  # (missing, not unconfigured)


async def test_config_flow_needs_no_mood_helper(hass: HomeAssistant, lit) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"auto_option": "Auto",
                                                                                 "mismatch_grace_seconds": 30})
    assert result["type"] is FlowResultType.FORM and result["step_id"] != "user", result
