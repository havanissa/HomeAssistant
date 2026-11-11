"""UI configuration: options flow (rooms, categories, profiles, mode)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import Home, base_options, setup_entry
from .test_config_flow import MOOD_INPUT


async def _open(hass: HomeAssistant, entry, menu_choice: str):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": menu_choice})


async def test_only_observe_mode_allowed(hass: HomeAssistant, home: Home) -> None:
    entry = await setup_entry(hass, base_options(home))
    result = await _open(hass, entry, "general")
    for mode in ("supervise", "full_control"):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**MOOD_INPUT, "operating_mode": mode}
        )
        assert result["errors"] == {"operating_mode": "mode_not_available"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**MOOD_INPUT, "mismatch_grace_seconds": 5, "operating_mode": "observe"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["mood"]["mismatch_grace_seconds"] == 5
    # Options change reloads the entry with the new configuration.
    assert entry.runtime_data.config.mood.mismatch_grace_seconds == 5


async def test_enable_disable_and_reset_room(hass: HomeAssistant, home: Home) -> None:
    entry = await setup_entry(hass, base_options(home))

    # Enable a second room with the lamp.
    result = await _open(hass, entry, "rooms")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"area": home.other_area})
    assert result["step_id"] == "room_edit"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enabled": True, "lights": [home.lamp], "reset": False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["rooms"][home.other_area]["enabled"] is True
    assert entry.options["rooms"][home.other_area]["lights"] == [home.lamp]

    # Disable the pilot room: configuration is kept.
    result = await _open(hass, entry, "rooms")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"area": home.pilot_area})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enabled": False, "radar": [home.radar], "reset": False}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {home.radar: "maintain"})
    assert result["step_id"] == "room_occupancy"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    pilot = entry.options["rooms"][home.pilot_area]
    assert pilot["enabled"] is False and pilot["sensors"][0]["entity_id"] == home.radar

    # Reset removes the stored room configuration entirely.
    result = await _open(hass, entry, "rooms")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"area": home.pilot_area})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"enabled": False, "reset": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert home.pilot_area not in entry.options["rooms"]


async def test_room_edit_keeps_existing_roles(hass: HomeAssistant, home: Home) -> None:
    opts = base_options(home)
    opts["rooms"][home.pilot_area]["sensors"][0]["role"] = "confirm_vacancy"  # radar
    entry = await setup_entry(hass, opts)
    result = await _open(hass, entry, "rooms")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"area": home.pilot_area})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enabled": True, "radar": [home.radar], "pir": [home.pir], "reset": False}
    )
    schema = {str(k): k.default() for k in result["data_schema"].schema}
    assert schema[home.radar] == "confirm_vacancy"
    assert schema[home.pir] == "trigger"


async def test_category_add_edit_remove(hass: HomeAssistant, home: Home) -> None:
    opts = base_options(home)
    opts["global_profiles"] = {"Dagur": {"led_strips": {"action": "on", "brightness_pct": 40}}}
    entry = await setup_entry(hass, opts)

    result = await _open(hass, entry, "categories")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "category_add"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"name": "Lamps"})
    assert result["errors"] == {"name": "category_exists"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Cabinet lights", "label": home.lamp_label}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert {"key": "cabinet_lights", "name": "Cabinet lights", "label_id": home.lamp_label} in entry.options["categories"]

    result = await _open(hass, entry, "categories")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "category_select"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"category": "cabinet_lights"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"name": "Cabinet"})
    await hass.async_block_till_done()
    cab = next(c for c in entry.options["categories"] if c["key"] == "cabinet_lights")
    # Key is stable across rename; clearing the label unmaps it.
    assert cab == {"key": "cabinet_lights", "name": "Cabinet", "label_id": None}

    result = await _open(hass, entry, "categories")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "category_remove"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"categories": ["led_strips", "cabinet_lights"]}
    )
    await hass.async_block_till_done()
    keys = [c["key"] for c in entry.options["categories"]]
    assert keys == ["downlights", "lamps"]
    # Profile entries of removed categories are cleaned up.
    assert entry.options["global_profiles"] == {}


async def test_profiles_global_room_light(hass: HomeAssistant, home: Home, service_calls) -> None:
    entry = await setup_entry(hass, base_options(home))

    async def edit(scope: str, mood: str, room: str | None, target: dict, values: dict):
        result = await _open(hass, entry, "profiles")
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"scope": scope, "mood": mood})
        if room:
            result = await hass.config_entries.options.async_configure(result["flow_id"], {"room": room})
        result = await hass.config_entries.options.async_configure(result["flow_id"], target)
        assert result["step_id"] == "profile_edit"
        return await hass.config_entries.options.async_configure(result["flow_id"], values)

    result = await edit("global", "Kvøld", None, {"category": "downlights"},
                        {"action": "on", "brightness_pct": 75, "color_temp_kelvin": 3000, "transition": 2, "remove": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["global_profiles"]["Kvøld"]["downlights"] == {
        "action": "on", "brightness_pct": 75, "color_temp_kelvin": 3000, "transition": 2.0,
    }

    result = await edit("room", "Kvøld", home.pilot_area, {"category": "downlights"},
                        {"action": "inherit", "brightness_pct": 50, "remove": False})
    await hass.async_block_till_done()
    assert entry.options["rooms"][home.pilot_area]["mood_overrides"]["Kvøld"]["downlights"] == {
        "action": "inherit", "brightness_pct": 50,
    }

    # Validation: colour temperature and RGB together are rejected.
    result = await edit("light", "Kvøld", home.pilot_area, {"light": home.group},
                        {"action": "on", "color_temp_kelvin": 2700, "rgb_color": [255, 0, 0], "remove": False})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"rgb_color": "kelvin_and_rgb"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "on", "rgb_color": [255, 0, 0], "effect": "candle", "remove": False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["rooms"][home.pilot_area]["light_overrides"]["Kvøld"][home.group] == {
        "action": "on", "rgb_color": [255, 0, 0], "effect": "candle",
    }

    # Remove the light override again.
    result = await edit("light", "Kvøld", home.pilot_area, {"light": home.group}, {"action": "on", "remove": True})
    await hass.async_block_till_done()
    assert entry.options["rooms"][home.pilot_area]["light_overrides"] == {}
    # Preparing profiles never touches a light.
    assert service_calls == []


async def test_profile_off_with_brightness_rejected(hass: HomeAssistant, home: Home) -> None:
    entry = await setup_entry(hass, base_options(home))
    result = await _open(hass, entry, "profiles")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"scope": "global", "mood": "Nátt"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"category": "lamps"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "off", "brightness_pct": 20, "remove": False}
    )
    assert result["errors"] == {"action": "off_with_attributes"}
