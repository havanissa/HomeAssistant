"""UI configuration: initial setup flow."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, label_registry as lr

from custom_components.smart_occupancy.const import DOMAIN

from .conftest import Home, setup_entry, base_options

MOOD_INPUT = {
    "auto_mood_entity": "input_select.auto_mood",
    "manual_mood_entity": "input_select.mood",
    "main_mood_entity": "input_select.main_mood",
    "auto_option": "Auto",
    "mismatch_grace_seconds": 30,
}


def _suggested(result, key):
    for k in result["data_schema"].schema:
        if k == key:
            return (k.description or {}).get("suggested_value")
    raise KeyError(key)


async def test_full_user_flow(hass: HomeAssistant, home: Home, service_calls) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    # The documented mood helpers are discovered and pre-filled.
    assert _suggested(result, "auto_mood_entity") == "input_select.auto_mood"
    assert _suggested(result, "main_mood_entity") == "input_select.main_mood"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], MOOD_INPUT)
    assert result["step_id"] == "categories"
    # Existing labels are matched to categories by label name.
    assert _suggested(result, "downlights") == home.downlight_label
    assert _suggested(result, "lamps") == home.lamp_label
    assert _suggested(result, "led_strips") is None

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"downlights": home.downlight_label, "lamps": home.lamp_label, "create_missing_labels": False},
    )
    assert result["step_id"] == "rooms"
    # Pilot room is pre-selected by area name.
    assert _suggested(result, "rooms") == [home.pilot_area]

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"rooms": [home.pilot_area]})
    assert result["step_id"] == "room_assign"
    assert result["description_placeholders"]["room"] == "Kamar Norður"
    # Discovery by device class: motion->PIR, occupancy->radar, door->door.
    assert _suggested(result, "pir") == [home.pir]
    assert _suggested(result, "radar") == [home.radar]
    assert _suggested(result, "door") == [home.door]
    # Window contacts and diagnostic entities are not pre-assigned.
    assert home.window in result["description_placeholders"]["discovered"]
    assert home.tamper not in result["description_placeholders"]["discovered"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"pir": [home.pir], "radar": [home.radar], "door": [home.door], "lights": [home.group]},
    )
    assert result["step_id"] == "room_roles"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {home.pir: "trigger", home.radar: "maintain", home.door: "confirm_vacancy"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    options = result["result"].options
    room = options["rooms"][home.pilot_area]
    assert room["enabled"] is True
    roles = {s["entity_id"]: (s["kind"], s["role"]) for s in room["sensors"]}
    assert roles == {
        home.pir: ("pir", "trigger"),
        home.radar: ("radar", "maintain"),
        home.door: ("door", "confirm_vacancy"),
    }
    assert room["lights"] == [home.group]
    assert options["operating_mode"] == "observe"
    assert len(options["categories"]) == 7
    await hass.async_block_till_done()
    assert result["result"].state is config_entries.ConfigEntryState.LOADED
    assert service_calls == []


async def test_user_step_errors(hass: HomeAssistant, home: Home) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    bad = {**MOOD_INPUT, "auto_mood_entity": "input_select.does_not_exist"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], bad)
    assert result["errors"] == {"auto_mood_entity": "entity_not_found"}

    bad = {**MOOD_INPUT, "auto_option": "Automatic"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], bad)
    assert result["errors"] == {"auto_option": "auto_option_not_found"}

    bad = {**MOOD_INPUT, "main_mood_entity": "input_select.auto_mood"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], bad)
    assert result["errors"] == {"base": "mood_entities_not_distinct"}


async def test_duplicate_assignment_rejected(hass: HomeAssistant, home: Home) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], MOOD_INPUT)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"create_missing_labels": False})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"rooms": [home.pilot_area]})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pir": [home.radar], "radar": [home.radar]}
    )
    assert result["errors"] == {"radar": "duplicate_assignment"}


async def test_create_missing_labels_only_creates_labels(hass: HomeAssistant, home: Home) -> None:
    ent_reg = er.async_get(hass)
    labels_before = {e.entity_id: set(e.labels) for e in ent_reg.entities.values()}

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], MOOD_INPUT)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"downlights": home.downlight_label, "create_missing_labels": True}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"rooms": []})
    assert result["type"] is FlowResultType.CREATE_ENTRY

    label_reg = lr.async_get(hass)
    cats = {c["key"]: c["label_id"] for c in result["result"].options["categories"]}
    assert cats["downlights"] == home.downlight_label
    # "Lamps" label already existed and is reused rather than duplicated.
    assert cats["lamps"] == home.lamp_label
    assert label_reg.async_get_label(cats["led_strips"]).name == "LED strips"
    # No label was applied to any entity.
    assert {e.entity_id: set(e.labels) for e in ent_reg.entities.values()
            if e.platform != DOMAIN} == labels_before


async def test_single_instance(hass: HomeAssistant, home: Home) -> None:
    await setup_entry(hass, base_options(home))
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
